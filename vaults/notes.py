"""Note file operations: paths, locks, atomic writes, frontmatter, links.

Plain functions only (no MCP decorators); thin @mcp.tool wrappers live in
vaults.server. Cross-module index/git calls are imported lazily inside the
functions that need them so notes <-> indexes has no import cycle.
"""

import contextlib
import hashlib
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path

try:
    import fcntl  # POSIX-only; flock-based note locks have no Windows equivalent.
except ImportError as exc:
    raise RuntimeError("vaults-hub requires POSIX fcntl (unavailable on Windows)") from exc

import yaml

from vaults import config

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
WIKI_LINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
UPDATED_RE = re.compile(r"(?m)^updated:.*$")


def _vault_root(vault: str) -> Path:
    root = (config.VAULTS_ROOT / vault).resolve()
    if root.parent != config.VAULTS_ROOT or not root.is_dir():
        raise ValueError(f"unknown vault: {vault!r}")
    return root


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
            out[key] = [v.isoformat() if isinstance(v, (datetime, date)) else v for v in val]
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
    new_block = UPDATED_RE.sub(f"updated: {date.today().isoformat()}", m.group(1), count=1)
    return text[: m.start(1)] + new_block + text[m.end(1) :], True


def _resolve_link_fast(root: Path, target: str, by_stem: dict[str, Path]) -> Path | None:
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


def _resolve_link(root: Path, target: str) -> Path | None:
    from vaults.indexes import _all_notes  # deferred: avoids notes<->indexes cycle

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


def _link_info(root: Path, here: Path, text: str) -> tuple[list[str], list[str], list[str]]:
    from vaults.indexes import _BACKLINK_INDEX, _all_notes, _ensure_indexes

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


def list_vaults() -> list[dict]:
    from vaults.indexes import _all_notes  # deferred: avoids notes<->indexes cycle

    if not config.VAULTS_ROOT.is_dir():
        return []
    return [
        {
            "name": d.name,
            "path": str(d.resolve()),
            "notes": len(_all_notes(d.resolve())),
        }
        for d in sorted(config.VAULTS_ROOT.iterdir())
        if d.is_dir() and not d.name.startswith(".")
    ]


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


def read_note(vault: str, path: str) -> dict:
    from vaults.indexes import _BACKLINK_INDEX, _FRONTMATTER_INDEX, _ensure_indexes

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
    root: Path, p: Path, content: str, expected_sha256: str | None, op: str = "write"
) -> dict:
    """Write under an acquired lock. Callers must hold _note_lock(p)."""
    from vaults.indexes import _invalidate_vault  # deferred: avoids notes<->indexes cycle
    from vaults.versioning import _git_result  # deferred: avoids notes<->versioning cycle

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
    rel = _rel(root, p)
    new_sha = _sha256(content)
    versioning = _git_result(root, root.name, [rel], f"vaults: {op} {rel}", new_sha)
    _invalidate_vault(root.name)
    return {
        "vault": root.name,
        "path": rel,
        "bytes": len(content.encode()),
        "previous_sha256": previous_sha256,
        "sha256": new_sha,
        "updated_refreshed": refreshed,
        **versioning,
    }


def write_note(vault: str, path: str, content: str, expected_sha256: str | None = None) -> dict:
    root = _vault_root(vault)
    p = _note_path(root, path)
    with _note_lock(p):
        return _write_note_locked(root, p, content, expected_sha256)


def append_note(vault: str, path: str, text: str, expected_sha256: str | None = None) -> dict:
    root = _vault_root(vault)
    p = _note_path(root, path)
    if p.suffix != ".md":
        raise ValueError("note path must end in .md")
    _ensure_utf8(text)
    with _note_lock(p):
        current = _read_text(p) if p.is_file() else ""
        if current and not current.endswith("\n"):
            current += "\n"
        return _write_note_locked(root, p, current + text, expected_sha256, op="append")


def delete_note(vault: str, path: str, missing_ok: bool = False) -> dict:
    from vaults.indexes import _invalidate_vault  # deferred: avoids notes<->indexes cycle
    from vaults.versioning import (  # deferred: avoids notes<->versioning cycle
        _git_result,
        _git_track_before_delete,
    )

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
        rel = _rel(root, p)
        track_err = _git_track_before_delete(root, vault, rel, sha_before)
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
        versioning = _git_result(root, vault, [rel], f"vaults: delete {rel}", sha_before)
        if track_err and "commit_error" not in versioning:
            versioning["commit_error"] = track_err
        _invalidate_vault(vault)
    # NOTE: lock files are intentionally left in place. Unlinking the sibling
    # lock after unlock races with a concurrent process that just opened (or
    # is about to flock) the same path, breaking mutual exclusion.
    return {"vault": vault, "path": path, "sha256_before": sha_before, **versioning}


def move_note(
    vault: str,
    src_path: str,
    dst_path: str,
    expected_sha256: str | None = None,
) -> dict:
    from vaults.indexes import (  # deferred: avoids notes<->indexes cycle
        _ensure_indexes,
        _invalidate_vault,
    )
    from vaults.versioning import _git_result  # deferred: avoids notes<->versioning cycle

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
        src_rel = _rel(root, src)
        dst_rel = _rel(root, dst)
        new_sha = _sha256(content)
        versioning = _git_result(
            root, vault, [dst_rel, src_rel], f"vaults: move {src_rel} -> {dst_rel}", new_sha
        )
        _invalidate_vault(vault)
        _ensure_indexes(root, vault)
        return {
            "vault": vault,
            "src_path": src_path,
            "dst_path": dst_rel,
            "sha256": new_sha,
            **versioning,
        }
