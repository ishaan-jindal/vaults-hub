"""Per-vault backlink/tag indexes. Process-local, mtime-validated, never persisted."""

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

# Single process-wide lock serializing access to the process-local indexes above.
# RLock (not Lock) because _ensure_indexes calls _rebuild_indexes while held.
_INDEX_LOCK = threading.RLock()


def _all_notes(root: Path) -> list[Path]:
    """Sorted .md notes under root, skipping hidden entries (`.git`, `.obsidian`, `.trash`).

    Not cached: a walk that prunes hidden dirs costs about what validating a
    cache of it would, and is never stale.
    """
    notes = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath, name)
            if name.endswith(".md") and not name.startswith(".") and _under_vault(root, p):
                notes.append(p)
    return sorted(notes)


def _invalidate_vault(vault: str) -> None:
    """Drop all cached indexes for a vault. Called by every writer."""
    with _INDEX_LOCK:
        _INDEX_MTIMES.pop(vault, None)
        _BACKLINK_INDEX.pop(vault, None)
        _FRONTMATTER_INDEX.pop(vault, None)


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


def _ensure_indexes(root: Path, vault: str) -> None:
    """Rebuild the vault's indexes unless its note set and every note mtime are unchanged.

    Comparing {relpath: mtime} catches added, removed, and edited notes,
    including changes made by another process.
    """
    # ponytail: mtime validation can miss same-timestamp edits on filesystems
    # with coarse granularity; content hashing or inotify is the upgrade path.
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
