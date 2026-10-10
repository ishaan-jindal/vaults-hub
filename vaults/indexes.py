"""Per-vault indexes (lanes A + B). Process-local, mtime-validated, never persisted."""

import os
import threading
from collections import Counter
from pathlib import Path

from vaults.notes import (
    WIKI_LINK_RE,
    _read_text,
    _rel,
    _resolve_link_fast,
    _split_frontmatter,
    _stem_index,
    _under_vault,
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
# vault name -> {dir relpath: st_mtime_ns} covering every visible directory
# under the vault; a changed signature forces a rescan so files added or
# removed by another process (e.g. Obsidian) are noticed without a
# server-side write.
_DIR_SIG: dict[str, dict[str, int]] = {}

# Single process-wide lock serializing access to the process-local indexes
# above (_NOTES_CACHE / _BACKLINK_INDEX / _FRONTMATTER_INDEX / _INDEX_MTIMES).
# RLock (not Lock) because _ensure_indexes calls _rebuild_indexes while held.
_INDEX_LOCK = threading.RLock()


def _scan_notes(root: Path) -> list[Path]:
    """Uncached recursive scan, skipping `.obsidian` trees and vault escapes."""
    return sorted(
        p
        for p in root.rglob("*.md")
        if ".obsidian" not in p.parts and _under_vault(root, p) is not None
    )


def _dir_signature(root: Path) -> dict[str, int]:
    """Map every visible directory under root to its st_mtime_ns.

    Hidden dirs (.git, .obsidian, .*) are pruned: they never contain indexed
    notes, and including .git would force a rescan on every commit.
    """
    sig: dict[str, int] = {}
    try:
        sig["."] = root.stat().st_mtime_ns
    except OSError:
        return sig
    try:
        for dirpath, dirnames, _ in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            try:
                rel = Path(dirpath).relative_to(root).as_posix()
            except ValueError:
                continue
            if rel == ".":
                continue
            try:
                sig[rel] = Path(dirpath).stat().st_mtime_ns
            except OSError:
                continue
    except OSError:
        pass
    return sig


def _invalidate_vault(vault: str) -> None:
    """Drop all cached indexes for a vault. Called by every writer."""
    with _INDEX_LOCK:
        _INDEX_MTIMES.pop(vault, None)
        _BACKLINK_INDEX.pop(vault, None)
        _FRONTMATTER_INDEX.pop(vault, None)
        _NOTES_CACHE.pop(vault, None)
        _DIR_SIG.pop(vault, None)


def _all_notes(root: Path) -> list[Path]:
    """Cached note listing. Rebuilt when the directory signature changes.

    Writers invalidate explicitly via _invalidate_vault, so a new path
    written through write/append/delete/move always triggers a rescan; files
    added or removed by another process change a directory mtime, which the
    per-directory signature notices. Returns a copy so callers cannot mutate
    the cache.
    """
    # ponytail: mtime-based invalidation can miss same-nanosecond edits (and
    # filesystems with coarse timestamp granularity); an OS watcher
    # (inotify/FSEvents) or content-hash validation is the upgrade path if
    # that ever matters.
    vault = root.name
    with _INDEX_LOCK:
        cached = _NOTES_CACHE.get(vault)
        sig_cached = _DIR_SIG.get(vault)
    if cached is not None and sig_cached is not None:
        try:
            if _dir_signature(root) == sig_cached and all(p.is_file() for p in cached):
                return list(cached)
        except OSError:
            pass
    fresh = _scan_notes(root)
    with _INDEX_LOCK:
        _NOTES_CACHE[vault] = list(fresh)
        try:
            _DIR_SIG[vault] = _dir_signature(root)
        except OSError:
            _DIR_SIG.pop(vault, None)
    return list(fresh)


def _rebuild_indexes(root: Path, vault: str, scan: list[Path]) -> None:
    """Full rebuild of backlink + frontmatter indexes from one scan."""
    with _INDEX_LOCK:
        by_stem = _stem_index(scan)
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
        try:
            _DIR_SIG[vault] = _dir_signature(root)
        except OSError:
            _DIR_SIG.pop(vault, None)


def _ensure_indexes(root: Path, vault: str) -> None:
    """Rebuild the vault's indexes unless its note set and every note mtime are unchanged.

    _all_notes notices added/removed notes via the directory signature; the
    per-file mtimes catch in-place edits, including ones by another process.
    """
    notes = _all_notes(root)
    with _INDEX_LOCK:
        mt = _INDEX_MTIMES.get(vault)
        indexed = vault in _BACKLINK_INDEX and vault in _FRONTMATTER_INDEX
    if mt is not None and indexed:
        try:
            if {_rel(root, p): p.stat().st_mtime for p in notes} == mt:
                return
        except (OSError, ValueError):
            pass
    _rebuild_indexes(root, vault, notes)


def list_tags(vault: str | None = None) -> dict:
    names = [vault] if vault is not None else [v["name"] for v in list_vaults()]
    result: dict[str, dict[str, int]] = {}
    for name in names:
        root = _vault_root(name)
        _ensure_indexes(root, name)
        counts = Counter(
            tag for fm in _FRONTMATTER_INDEX.get(name, {}).values() for tag in fm.get("tags", [])
        )
        result[name] = dict(sorted(counts.items()))
    return result
