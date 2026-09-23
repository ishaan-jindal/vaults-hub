"""Vaults hub: local stdio MCP server exposing every project vault at once.

Each project has its own Obsidian vault under VAULTS_ROOT (default
~/Dev/vaults/<project>/). This server operates directly on the markdown files,
so opencode can read/write/search all vaults with no Obsidian windows, ports,
or API keys involved. Spawned per-session by opencode over stdio.
"""

import asyncio
import contextlib
import fcntl
import fnmatch
import functools
import json
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import anyio
import mcp.types as mcp_types
import yaml
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage

VAULTS_ROOT = Path(
    os.environ.get("VAULTS_ROOT", str(Path.home() / "Dev" / "vaults"))
).resolve()

mcp = FastMCP("vaults")

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
WIKI_LINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
UPDATED_RE = re.compile(r"(?m)^updated:.*$")

_logger = logging.getLogger("vaults")


def _setup_logging() -> None:
    """Enable debug file logging when VAULTS_HUB_DEBUG=1. No-op otherwise."""
    if os.environ.get("VAULTS_HUB_DEBUG") != "1":
        _logger.disabled = True
        return
    _logger.disabled = False
    try:
        log_dir = Path.home() / ".cache" / "vaults-hub"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "debug.log", maxBytes=1_000_000, backupCount=3
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        _logger.addHandler(handler)
        _logger.setLevel(logging.DEBUG)
    except OSError:
        pass


_setup_logging()


def _logged(fn):
    """Log tool entry/exit at INFO and failures at ERROR with traceback."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        vault = kwargs.get("vault", args[0] if args else None)
        path = kwargs.get("path", kwargs.get("src_path"))
        _logger.info("vaults.%s called vault=%r path=%r", fn.__name__, vault, path)
        try:
            result = fn(*args, **kwargs)
        except Exception:
            _logger.error("vaults.%s failed", fn.__name__, exc_info=True)
            raise
        _logger.info("vaults.%s ok", fn.__name__)
        return result

    return wrapper


# ---------------------------------------------------------------------------
# Per-vault indexes (lanes A + B). Process-local, mtime-validated, never persisted.
# ---------------------------------------------------------------------------

# vault name -> {relpath: set(backlink relpaths)}
_BACKLINK_INDEX: dict[str, dict[str, set[str]]] = {}
# vault name -> {relpath: normalized frontmatter dict}
_FRONTMATTER_INDEX: dict[str, dict[str, dict]] = {}
# vault name -> {relpath: mtime seen at index time}
_INDEX_MTIMES: dict[str, dict[str, float]] = {}
# vault name -> cached sorted note list (lane A: rebuild on missing/invalidate)
_NOTES_CACHE: dict[str, list[Path]] = {}


def _scan_notes(root: Path) -> list[Path]:
    """Uncached recursive scan, skipping `.obsidian` trees."""
    return sorted(p for p in root.rglob("*.md") if ".obsidian" not in p.parts)


def _invalidate_vault(vault: str) -> None:
    """Drop all cached indexes for a vault. Called by every writer."""
    _INDEX_MTIMES.pop(vault, None)
    _BACKLINK_INDEX.pop(vault, None)
    _FRONTMATTER_INDEX.pop(vault, None)
    _NOTES_CACHE.pop(vault, None)


def _vault_root(vault: str) -> Path:
    root = (VAULTS_ROOT / vault).resolve()
    if root.parent != VAULTS_ROOT or not root.is_dir():
        raise ValueError(f"unknown vault: {vault!r}")
    return root


def _all_notes(root: Path) -> list[Path]:
    """Cached note listing. Rebuilt when a cached entry goes missing.

    Writers invalidate explicitly via _invalidate_vault, so a new path
    written through write/append/delete/move always triggers a rescan.
    Returns a copy so callers cannot mutate the cache.
    """
    vault = root.name
    cached = _NOTES_CACHE.get(vault)
    if cached is not None:
        try:
            if all(p.is_file() for p in cached):
                return list(cached)
        except OSError:
            pass
    fresh = _scan_notes(root)
    _NOTES_CACHE[vault] = list(fresh)
    return list(fresh)


def _rel(root: Path, p: Path) -> str:
    return p.resolve().relative_to(root).as_posix()


def _note_path(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes vault: {rel!r}")
    return p


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ensure_utf8(content: str) -> None:
    """Reject lone surrogates. Raises ValueError, never UnicodeEncodeError."""
    try:
        content.encode("utf-8", errors="strict")
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("note content must be valid UTF-8") from exc


def _read_text(path: Path) -> str:
    """Read a note as UTF-8, replacing invalid bytes instead of crashing."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_bytes().decode("utf-8", errors="replace")


@contextlib.contextmanager
def _note_lock(path: Path):
    """Serialize read-modify-write across processes via an flock'd lock file.

    The note itself is replaced by _atomic_write, so locking the note's inode
    would not survive the swap; a sibling ``.<name>.lock`` file avoids that.
    """
    lock_path = path.parent / f".{path.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def _note_locks_two(first: Path, second: Path):
    """Hold two note locks in lexicographic order to avoid deadlock.

    Ordering rule: the lock whose resolved string sorts first is acquired
    first, regardless of src/dst roles, so concurrent opposite-direction
    moves cannot deadlock. Re-entrant on the same path (single lock).
    """
    ordered = sorted([first.resolve(), second.resolve()], key=lambda p: p.as_posix())
    if ordered[0] == ordered[1]:
        with _note_lock(ordered[0]):
            yield
        return
    with _note_lock(ordered[0]):
        with _note_lock(ordered[1]):
            yield


def _atomic_write(path: Path, content: str) -> None:
    """Replace a note without exposing a partially written file."""
    fd, temporary_path = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def _normalize_tags(value) -> list[str]:
    """Coerce frontmatter `tags` to a sorted list[str].

    Accepts a YAML string or list (documents the string form explicitly):
    - plain string with no comma/brackets (e.g. ``d``) -> ``["d"]``
    - comma-separated or bracketed string -> split on commas
    - list -> coerce each item to str
    Empty/missing -> ``[]``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        if "," not in s and "[" not in s and "]" not in s:
            return [s]
        inner = s.strip().strip("[]")
        parts = [p.strip().strip("'\"") for p in inner.split(",")]
        return sorted(p for p in parts if p)
    if isinstance(value, list):
        out = []
        for item in value:
            t = str(item).strip()
            if t:
                out.append(t)
        return sorted(out)
    s = str(value).strip()
    return [s] if s else []


def _normalize_frontmatter(data: dict) -> dict:
    """Normalize tags + stringify dates so output is JSON-serializable."""
    out = dict(data)
    if "tags" in out:
        out["tags"] = _normalize_tags(out["tags"])
    for key, val in list(out.items()):
        if isinstance(val, (datetime, date)):
            out[key] = val.isoformat()
        elif isinstance(val, list):
            out[key] = [
                v.isoformat() if isinstance(v, (datetime, date)) else v for v in val
            ]
    return out


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter; tags normalized to list[str], dates to str."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        return {}, text[m.end() :]
    return _normalize_frontmatter(data), text[m.end() :]


def _refresh_updated(text: str) -> tuple[str, bool]:
    """Bump `updated:` in the frontmatter block. Returns (text, changed)."""
    m = FRONTMATTER_RE.match(text)
    if not m or "updated:" not in m.group(1):
        return text, False
    new_block = UPDATED_RE.sub(
        f"updated: {date.today().isoformat()}", m.group(1), count=1
    )
    return text[: m.start(1)] + new_block + text[m.end(1) :], True


def _resolve_link_fast(
    root: Path, target: str, by_stem: dict[str, Path]
) -> Path | None:
    """Resolve a wiki-link target using a prebuilt stem index."""
    target = target.strip()
    if not target:
        return None
    if "/" in target:
        cand = (root / target).resolve()
        if cand.is_file():
            return cand
        cand_md = cand.with_suffix(".md") if cand.suffix != ".md" else cand
        return cand_md if cand_md.is_file() else None
    return by_stem.get(target)


def _rebuild_indexes(root: Path, vault: str, scan: list[Path]) -> None:
    """Full rebuild of backlink + frontmatter indexes from one scan."""
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


def _resolve_link(root: Path, target: str) -> Path | None:
    target = target.strip()
    if not target:
        return None
    if "/" in target:
        cand = (root / target).resolve()
        if cand.is_file():
            return cand
        cand_md = cand.with_suffix(".md") if cand.suffix != ".md" else cand
        return cand_md if cand_md.is_file() else None
    matches = [p for p in _all_notes(root) if p.stem == target]
    if not matches:
        return None
    # Obsidian resolves duplicate note names to the shortest path.
    return sorted(matches, key=lambda p: (len(p.parts), p.as_posix()))[0]


def _link_info(
    root: Path, here: Path, text: str
) -> tuple[list[str], list[str], list[str]]:
    targets = [t.strip() for t in WIKI_LINK_RE.findall(text) if t.strip()]
    links, unresolved = [], []
    for t in targets:
        hit = _resolve_link(root, t)
        (links if hit else unresolved).append(t)
    links, unresolved = sorted(set(links)), sorted(set(unresolved))
    vault = root.name
    try:
        _ensure_indexes(root, vault)
        rel = _rel(root, here)
        backlinks = sorted(_BACKLINK_INDEX.get(vault, {}).get(rel, set()))
    except ValueError:
        backlinks = sorted(
            {
                _rel(root, p)
                for p in _all_notes(root)
                if p != here and here in _link_targets(root, p)
            }
        )
    return links, backlinks, unresolved


def _link_targets(root: Path, p: Path) -> set[Path]:
    try:
        text = _read_text(p)
    except OSError:
        return set()
    out = set()
    for t in WIKI_LINK_RE.findall(text):
        hit = _resolve_link(root, t.strip())
        if hit:
            out.add(hit)
    return out


@mcp.tool(description="List all project vaults with their note counts.")
@_logged
def list_vaults() -> list[dict]:
    if not VAULTS_ROOT.is_dir():
        return []
    return [
        {
            "name": d.name,
            "path": str(d.resolve()),
            "notes": len(_all_notes(d.resolve())),
        }
        for d in sorted(VAULTS_ROOT.iterdir())
        if d.is_dir() and not d.name.startswith(".")
    ]


@mcp.tool(
    description="List directories and notes under a vault path (vault-root-relative, '' for root). Pass recursive=True to list all descendant notes flat (dirs=[])."
)
@_logged
def list_notes(vault: str, path: str = "", recursive: bool = False) -> dict:
    root = _vault_root(vault)
    base = _note_path(root, path or ".")
    if not base.is_dir():
        raise ValueError(f"not a directory: {path!r}")
    if recursive:
        notes = []
        for p in sorted(base.rglob("*.md")):
            try:
                parts = p.resolve().relative_to(root).parts
            except ValueError:
                continue
            if ".obsidian" in parts:
                continue
            if any(part.startswith(".") for part in parts):
                continue
            notes.append(p.resolve().relative_to(root).as_posix())
        return {"vault": vault, "path": path, "dirs": [], "notes": notes}
    dirs, notes = [], []
    for p in sorted(base.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            dirs.append(_rel(root, p) + "/")
        elif p.suffix == ".md":
            notes.append(_rel(root, p))
    return {"vault": vault, "path": path, "dirs": dirs, "notes": notes}


@mcp.tool(
    description="Read a note: frontmatter, content, wiki-links, backlinks, unresolved links."
)
@_logged
def read_note(vault: str, path: str) -> dict:
    root = _vault_root(vault)
    p = _note_path(root, path)
    if not p.is_file():
        raise ValueError(f"note not found: {path!r}")
    _ensure_indexes(root, vault)
    text = _read_text(p)
    rel = _rel(root, p)
    cached_fm = _FRONTMATTER_INDEX.get(vault, {}).get(rel)
    if cached_fm is None:
        frontmatter, _ = _split_frontmatter(text)
    else:
        frontmatter = dict(cached_fm)
    targets = [t.strip() for t in WIKI_LINK_RE.findall(text) if t.strip()]
    links, unresolved = [], []
    for t in targets:
        hit = _resolve_link(root, t)
        (links if hit else unresolved).append(t)
    links, unresolved = sorted(set(links)), sorted(set(unresolved))
    backlinks = sorted(_BACKLINK_INDEX.get(vault, {}).get(rel, set()))
    return {
        "vault": vault,
        "path": rel,
        "frontmatter": frontmatter,
        "content": text,
        "sha256": _sha256(text),
        "links": links,
        "backlinks": backlinks,
        "unresolved_links": unresolved,
    }


def _write_note_locked(
    root: Path, p: Path, content: str, expected_sha256: str | None
) -> dict:
    """Write under an acquired lock. Callers must hold _note_lock(p)."""
    if p.suffix != ".md":
        raise ValueError("note path must end in .md")
    if p.is_dir():
        raise ValueError(f"note path is a directory: {p.name!r}")
    _ensure_utf8(content)
    p.parent.mkdir(parents=True, exist_ok=True)
    previous = _read_text(p) if p.is_file() else None
    previous_sha256 = _sha256(previous) if previous is not None else None
    if expected_sha256 is not None and expected_sha256 != previous_sha256:
        raise ValueError(
            "note has changed since the supplied expected_sha256; read it again before writing"
        )
    content, refreshed = _refresh_updated(content)
    _atomic_write(p, content)
    _invalidate_vault(root.name)
    return {
        "vault": root.name,
        "path": _rel(root, p),
        "bytes": len(content.encode()),
        "previous_sha256": previous_sha256,
        "sha256": _sha256(content),
        "updated_refreshed": refreshed,
    }


@mcp.tool(
    description=(
        "Create or atomically overwrite a note (vault-root-relative path). "
        "Pass sha256 from a prior read/write as expected_sha256 to prevent "
        "overwriting a changed note. Refreshes frontmatter `updated:` date."
    )
)
@_logged
def write_note(
    vault: str, path: str, content: str, expected_sha256: str | None = None
) -> dict:
    root = _vault_root(vault)
    p = _note_path(root, path)
    with _note_lock(p):
        return _write_note_locked(root, p, content, expected_sha256)


@mcp.tool(
    description=(
        "Append text to a note (created if missing) via an atomic write. "
        "Optionally pass expected_sha256 to prevent appending to a changed note. "
        "Refreshes frontmatter `updated:` date."
    )
)
@_logged
def append_note(
    vault: str, path: str, text: str, expected_sha256: str | None = None
) -> dict:
    root = _vault_root(vault)
    p = _note_path(root, path)
    if p.suffix != ".md":
        raise ValueError("note path must end in .md")
    _ensure_utf8(text)
    with _note_lock(p):
        current = _read_text(p) if p.is_file() else ""
        if current and not current.endswith("\n"):
            current += "\n"
        return _write_note_locked(root, p, current + text, expected_sha256)


@mcp.tool(
    description=(
        "Delete a note file. Errors when missing unless missing_ok=True. "
        "Cleans up sibling .lock/.tmp files best-effort."
    )
)
@_logged
def delete_note(vault: str, path: str, missing_ok: bool = False) -> dict:
    root = _vault_root(vault)
    p = _note_path(root, path)
    if p.suffix != ".md":
        raise ValueError("note path must end in .md")
    with _note_lock(p):
        if not p.is_file():
            if not missing_ok:
                raise ValueError(f"note not found: {path!r}")
            return {"vault": vault, "path": path, "sha256_before": None}
        try:
            sha_before = _sha256(_read_text(p))
        except OSError:
            sha_before = None
        try:
            p.unlink()
        except FileNotFoundError:
            if not missing_ok:
                raise ValueError(f"note not found: {path!r}")
            return {"vault": vault, "path": path, "sha256_before": None}
        for tmp in p.parent.glob(f".{p.name}.*.tmp"):
            try:
                tmp.unlink()
            except OSError:
                pass
        _invalidate_vault(vault)
    lock_sibling = p.parent / f".{p.name}.lock"
    try:
        lock_sibling.unlink()
    except OSError:
        pass
    return {"vault": vault, "path": path, "sha256_before": sha_before}


@mcp.tool(
    description=(
        "Move/rename a note atomically. Errors if src is missing or dst "
        "exists. Honors expected_sha256 against src. Refreshes `updated:`."
    )
)
@_logged
def move_note(
    vault: str,
    src_path: str,
    dst_path: str,
    expected_sha256: str | None = None,
) -> dict:
    root = _vault_root(vault)
    src = _note_path(root, src_path)
    dst = _note_path(root, dst_path)
    if src.resolve() == dst.resolve():
        raise ValueError("src and dst are the same note")
    if dst.suffix != ".md":
        raise ValueError("note path must end in .md")
    with _note_locks_two(src, dst):
        if not src.is_file():
            raise ValueError(f"note not found: {src_path!r}")
        if dst.exists():
            raise ValueError(f"destination exists: {dst_path!r}")
        try:
            previous = _read_text(src)
        except OSError as exc:
            raise ValueError(f"note not found: {src_path!r}") from exc
        if expected_sha256 is not None and _sha256(previous) != expected_sha256:
            raise ValueError(
                "note has changed since the supplied expected_sha256; read it again before moving"
            )
        _ensure_utf8(previous)
        dst.parent.mkdir(parents=True, exist_ok=True)
        content, _ = _refresh_updated(previous)
        _atomic_write(dst, content)
        try:
            src.unlink()
        except FileNotFoundError as exc:
            raise ValueError(f"note not found: {src_path!r}") from exc
        for tmp in src.parent.glob(f".{src.name}.*.tmp"):
            try:
                tmp.unlink()
            except OSError:
                pass
        _invalidate_vault(vault)
        _ensure_indexes(root, vault)
        return {
            "vault": vault,
            "src_path": src_path,
            "dst_path": _rel(root, dst),
            "sha256": _sha256(content),
        }


@mcp.tool(
    description=(
        "List tag counts per vault from cached frontmatter. "
        "Pass vault for one vault, or omit for all vaults."
    )
)
@_logged
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


def _gitignore_patterns(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if not gi.is_file():
        return []
    try:
        return [
            ln.strip()
            for ln in gi.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    except OSError:
        return []


def _gitignored(root: Path, p: Path, patterns: list[str]) -> bool:
    """Minimal .gitignore matching for the Python search fallback."""
    rel = _rel(root, p)
    for raw in patterns:
        if raw.startswith("!"):
            continue
        anchored = raw.startswith("/")
        pat = raw.lstrip("/")
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        if dir_only:
            if rel.startswith(pat + "/"):
                return True
            continue
        if "*" not in pat:
            if anchored:
                if rel == pat or rel.startswith(pat + "/"):
                    return True
            elif pat in rel.split("/"):
                return True
        elif anchored:
            if fnmatch.fnmatch(rel, pat):
                return True
        elif any(fnmatch.fnmatch(part, pat) for part in rel.split("/")):
            return True
    return False


@mcp.tool(
    description=(
        "Search note contents with ripgrep across one vault or all vaults. "
        "before/after (0..10) add context lines; each hit keeps text "
        "(= match_line) plus before_lines/match_line/after_lines."
    )
)
@_logged
def search_notes(
    query: str,
    vault: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = 50,
    before: int = 0,
    after: int = 0,
) -> dict:
    before = max(0, min(10, int(before)))
    after = max(0, min(10, int(after)))
    roots = [_vault_root(vault)] if vault else [Path(v["path"]) for v in list_vaults()]
    hits = _collect_hits(roots, query, regex, case_sensitive, limit)
    _attach_context(hits, roots, before, after)
    return {"query": query, "matches": hits}


def _collect_hits(roots, query, regex, case_sensitive, limit) -> list[dict]:
    """Collect raw matches via rg, or the Python fallback when rg is absent."""
    if shutil.which("rg"):
        return _search_rg(roots, query, regex, case_sensitive, limit)
    return _search_fallback(roots, query, regex, case_sensitive, limit)


def _search_rg(roots, query, regex, case_sensitive, limit) -> list[dict]:
    args = ["rg", "--json", "-n", "--no-heading", "--glob", "!**/.obsidian/**"]
    if not regex:
        args.append("--fixed-strings")
    if not case_sensitive:
        args.append("--ignore-case")
    args += ["--max-count", "20", "--", query]
    hits: list[dict] = []
    for root in roots:
        try:
            proc = subprocess.run(
                args + [str(root)], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "match":
                continue
            d = ev["data"]
            abspath = Path(d["path"]["text"]).resolve()
            hits.append(
                {
                    "vault": root.name,
                    "path": abspath.relative_to(root).as_posix(),
                    "line": d["line_number"],
                    "text": d["lines"]["text"].strip(),
                }
            )
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    return hits[:limit]


def _search_fallback(roots, query, regex, case_sensitive, limit) -> list[dict]:
    flags = 0 if case_sensitive else re.IGNORECASE
    pat = re.compile(query if regex else re.escape(query), flags)
    hits: list[dict] = []
    for root in roots:
        ignore = _gitignore_patterns(root)
        for p in _all_notes(root):
            if _gitignored(root, p, ignore):
                continue
            try:
                lines = _read_text(p).splitlines()
            except OSError:
                continue
            file_hits = 0
            for i, ln in enumerate(lines, 1):
                if pat.search(ln):
                    hits.append(
                        {
                            "vault": root.name,
                            "path": _rel(root, p),
                            "line": i,
                            "text": ln.strip(),
                        }
                    )
                    file_hits += 1
                    if file_hits >= 20 or len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
    return hits[:limit]


def _attach_context(hits, roots, before, after) -> None:
    """Add before_lines/match_line/after_lines by rereading each file once."""
    if not hits:
        return
    vault_to_root = {r.name: r for r in roots}
    cache: dict[tuple[str, str], list[str]] = {}
    for hit in hits:
        key = (hit["vault"], hit["path"])
        if key not in cache:
            base = vault_to_root.get(hit["vault"])
            try:
                cache[key] = _read_text(_note_path(base, hit["path"])).splitlines()
            except (OSError, ValueError, AttributeError):
                cache[key] = []
        lines = cache[key]
        idx = hit["line"] - 1
        match = lines[idx].strip() if 0 <= idx < len(lines) else hit["text"]
        lo = max(0, idx - before)
        hi = min(len(lines), idx + 1 + after)
        hit["match_line"] = match
        hit["before_lines"] = [ln.strip() for ln in lines[lo:idx] if idx >= 0]
        hit["after_lines"] = [ln.strip() for ln in lines[idx + 1 : hi]]


async def _serve_stdio() -> None:
    """Run MCP over stdio without AnyIO's broken wrapped-file iterator.

    The project runtime's AnyIO version blocks forever when iterating an
    ``anyio.wrap_file(TextIOWrapper(sys.stdin.buffer))`` stream. The official
    MCP stdio adapter relies on that operation, so we bridge blocking stdio
    reads through a standard-library thread while leaving MCP protocol handling
    to the SDK.
    """
    read_sender, read_stream = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](32)
    write_sender, write_stream = anyio.create_memory_object_stream[SessionMessage](32)

    async def write_stdout() -> None:
        async with write_stream:
            async for session_message in write_stream:
                payload = (
                    session_message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    + "\n"
                )
                _write_stdout(payload)

    async with anyio.create_task_group() as task_group:
        _start_stdin_bridge(asyncio.get_running_loop(), read_sender)
        task_group.start_soon(write_stdout)
        try:
            await mcp._mcp_server.run(  # noqa: SLF001 - FastMCP's protocol server
                read_stream,
                write_sender,
                mcp._mcp_server.create_initialization_options(),
            )
        finally:
            await write_sender.aclose()
            task_group.cancel_scope.cancel()


def _write_stdout(payload: str) -> None:
    sys.stdout.write(payload)
    sys.stdout.flush()


def _start_stdin_bridge(
    event_loop: asyncio.AbstractEventLoop,
    sender: anyio.abc.ObjectSendStream[SessionMessage | Exception],
) -> None:
    """Forward stdin in a stdlib thread, since AnyIO worker threads hang here."""

    def forward() -> None:
        try:
            for line in sys.stdin.buffer:
                try:
                    item: SessionMessage | Exception = SessionMessage(
                        mcp_types.JSONRPCMessage.model_validate_json(line)
                    )
                except Exception as exc:
                    item = exc
                asyncio.run_coroutine_threadsafe(sender.send(item), event_loop).result()
        finally:
            try:
                asyncio.run_coroutine_threadsafe(sender.aclose(), event_loop).result()
            except RuntimeError:
                pass

    threading.Thread(target=forward, name="mcp-stdin", daemon=True).start()


if __name__ == "__main__":
    anyio.run(_serve_stdio)
