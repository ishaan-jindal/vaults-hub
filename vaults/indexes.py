"""Per-vault indexes (lanes A + B). Process-local, mtime-validated, never persisted."""

import threading
from pathlib import Path

from vaults.notes import (
    WIKI_LINK_RE,
    _normalize_tags,
    _read_text,
    _rel,
    _resolve_link_fast,
    _split_frontmatter,
    _vault_root,
    list_vaults,
)

# vault name -> {relpath: set(backlink relpaths)}
_BACKLINK_INDEX: dict[str, dict[str, set[str]]] = {}
# vault name -> {relpath: normalized frontmatter dict}
_FRONTMATTER_INDEX: dict[str, dict[str, dict]] = {}
# vault name -> {relpath: mtime seen at index time}
_INDEX_MTIMES: dict[str, dict[str, float]] = {}
# vault name -> cached sorted note list (lane A: rebuild on missing/invalidate)
_NOTES_CACHE: dict[str, list[Path]] = {}

# Single process-wide lock serializing access to the process-local indexes
# above (_NOTES_CACHE / _BACKLINK_INDEX / _FRONTMATTER_INDEX / _INDEX_MTIMES).
# RLock (not Lock) because _ensure_indexes calls _rebuild_indexes while held.
_INDEX_LOCK = threading.RLock()


def _scan_notes(root: Path) -> list[Path]:
    """Uncached recursive scan, skipping `.obsidian` trees."""
    return sorted(p for p in root.rglob("*.md") if ".obsidian" not in p.parts)


def _invalidate_vault(vault: str) -> None:
    """Drop all cached indexes for a vault. Called by every writer."""
    with _INDEX_LOCK:
        _INDEX_MTIMES.pop(vault, None)
        _BACKLINK_INDEX.pop(vault, None)
        _FRONTMATTER_INDEX.pop(vault, None)
        _NOTES_CACHE.pop(vault, None)


def _all_notes(root: Path) -> list[Path]:
    """Cached note listing. Rebuilt when a cached entry goes missing.

    Writers invalidate explicitly via _invalidate_vault, so a new path
    written through write/append/delete/move always triggers a rescan.
    Returns a copy so callers cannot mutate the cache.
    """
    vault = root.name
    with _INDEX_LOCK:
        cached = _NOTES_CACHE.get(vault)
        if cached is not None:
            try:
                if all(p.is_file() for p in cached):
                    return list(cached)
            except OSError:
                pass
    fresh = _scan_notes(root)
    with _INDEX_LOCK:
        _NOTES_CACHE[vault] = list(fresh)
    return list(fresh)


def _rebuild_indexes(root: Path, vault: str, scan: list[Path]) -> None:
    """Full rebuild of backlink + frontmatter indexes from one scan."""
    with _INDEX_LOCK:
        by_stem: dict[str, Path] = {}
        for p in sorted(scan, key=lambda q: (len(q.parts), q.as_posix())):
            by_stem.setdefault(p.stem, p)
        mtimes: dict[str, float] = {}
        fm_index: dict[str, dict] = {}
        targets_map: dict[str, set[Path]] = {}
        for p in scan:
            try:
                rel = _rel(root, p)
            except ValueError:
                continue
            try:
                mtimes[rel] = p.stat().st_mtime
            except OSError:
                continue
            try:
                text = _read_text(p)
            except OSError:
                fm_index[rel] = {}
                targets_map[rel] = set()
                continue
            try:
                raw, _ = _split_frontmatter(text)
                fm_index[rel] = raw
            except Exception:
                fm_index[rel] = {}
            hits: set[Path] = set()
            for t in WIKI_LINK_RE.findall(text):
                hit = _resolve_link_fast(root, t.strip(), by_stem)
                if hit is not None:
                    hits.add(hit)
            targets_map[rel] = hits
        backlinks: dict[str, set[str]] = {rel: set() for rel in mtimes}
        for src_rel, tgts in targets_map.items():
            for tgt in tgts:
                try:
                    tgt_rel = _rel(root, tgt)
                except ValueError:
                    continue
                if tgt_rel in backlinks and tgt_rel != src_rel:
                    backlinks[tgt_rel].add(src_rel)
        _BACKLINK_INDEX[vault] = backlinks
        _FRONTMATTER_INDEX[vault] = fm_index
        _INDEX_MTIMES[vault] = mtimes
        _NOTES_CACHE[vault] = list(scan)


def _ensure_indexes(root: Path, vault: str) -> None:
    """Validate caches by mtime; rebuild the vault on any mismatch.

    One fresh rglob detects new/deleted files; per-file mtimes detect
    edits. First read pays the full O(N^2) build, later reads reuse the
    cached backlink sets.
    """
    scan = _scan_notes(root)
    with _INDEX_LOCK:
        mt = _INDEX_MTIMES.get(vault)
        cached_bl = _BACKLINK_INDEX.get(vault)
        cached_fm = _FRONTMATTER_INDEX.get(vault)
        if mt is not None and cached_bl is not None and cached_fm is not None:
            try:
                scan_rels = {_rel(root, p) for p in scan}
            except ValueError:
                scan_rels = set()
            if set(mt.keys()) == scan_rels:
                valid = True
                for p in scan:
                    try:
                        rel = _rel(root, p)
                        cur = p.stat().st_mtime
                    except (OSError, ValueError):
                        valid = False
                        break
                    if mt.get(rel) != cur:
                        valid = False
                        break
                if valid:
                    _NOTES_CACHE[vault] = list(scan)
                    return
    _rebuild_indexes(root, vault, scan)


def list_tags(vault: str | None = None) -> dict:
    names = [vault] if vault is not None else [v["name"] for v in list_vaults()]
    result: dict[str, dict[str, int]] = {}
    for name in names:
        root = _vault_root(name)
        _ensure_indexes(root, name)
        counts: dict[str, int] = {}
        for fm in _FRONTMATTER_INDEX.get(name, {}).values():
            tags = fm.get("tags", [])
            if isinstance(tags, str):
                tags = _normalize_tags(tags)
            if not isinstance(tags, list):
                continue
            for tag in tags:
                key = str(tag)
                counts[key] = counts.get(key, 0) + 1
        result[name] = dict(sorted(counts.items()))
    return result
