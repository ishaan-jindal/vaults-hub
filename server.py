"""Vaults hub: local stdio MCP server exposing every project vault at once.

Each project has its own Obsidian vault under VAULTS_ROOT (default
~/.vaults/<project>/). This server operates directly on the markdown files,
so opencode can read/write/search all vaults with no Obsidian windows, ports,
or API keys involved. Spawned per-session by opencode over stdio.
"""

import argparse
import asyncio
import contextlib

try:
    import fcntl  # POSIX-only; flock-based note locks have no Windows equivalent.
except ImportError as exc:
    raise RuntimeError("vaults-hub requires POSIX fcntl (unavailable on Windows)") from exc
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

VAULTS_ROOT = Path(os.environ.get("VAULTS_ROOT", str(Path.home() / ".vaults"))).resolve()

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
        handler = RotatingFileHandler(log_dir / "debug.log", maxBytes=1_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
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


# ---------------------------------------------------------------------------
# Per-vault git versioning. One repo per vault, local-only (never push/fetch).
# ---------------------------------------------------------------------------

# Per-invocation identity so we never touch the user's gitconfig.
_GIT_IDENTITY = [
    "-c",
    "user.name=vaults-hub",
    "-c",
    "user.email=vaults-hub@localhost",
    "-c",
    "commit.gpgsign=false",
]
# Written to $GIT_DIR/info/exclude (never a visible .gitignore).
_GIT_EXCLUDES = [".obsidian/", ".*.lock", ".*.tmp"]
# vault name -> "ours" | "external" | "disabled" (process-local cache).
_GIT_MODE: dict[str, str] = {}


def _git_enabled() -> bool:
    """Opt-out switch: VAULTS_HUB_GIT=0 disables all versioning."""
    return os.environ.get("VAULTS_HUB_GIT", "1") != "0"


def _git_run(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run git in the vault root with per-invocation identity. Never raises."""
    try:
        return subprocess.run(
            ["git", *_GIT_IDENTITY, *args],
            cwd=root,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(args), 127, b"", str(exc).encode())


def _git_ensure_excludes(root: Path) -> None:
    """Append our exclude patterns to $GIT_DIR/info/exclude if missing."""
    exclude = root / ".git" / "info" / "exclude"
    try:
        text = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    except OSError:
        return
    missing = [p for p in _GIT_EXCLUDES if p not in text.splitlines()]
    if not missing:
        return
    try:
        with exclude.open("a", encoding="utf-8") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write("".join(p + "\n" for p in missing))
    except OSError:
        pass


def _git_mode(root: Path, vault: str) -> str:
    """Return 'ours', 'external', or 'disabled', lazily initing one repo per vault.

    Call on mutation paths while holding the note's fcntl lock. Only the
    process-local cache touches _INDEX_LOCK; never held across git subprocesses.
    """
    if not _git_enabled() or shutil.which("git") is None:
        return "disabled"
    with _INDEX_LOCK:
        cached = _GIT_MODE.get(vault)
    if cached is not None:
        return cached
    mode = _git_detect(root)
    with _INDEX_LOCK:
        _GIT_MODE[vault] = mode
    return mode


def _git_detect(root: Path) -> str:
    """Adopt a vault-root .git, defer to an outer repo, or lazily init ours."""
    if (root / ".git").exists():
        _git_ensure_excludes(root)
        return "ours"
    if _git_run(root, "rev-parse", "--git-dir").returncode == 0:
        return "external"  # vault nested in someone else's repo: hands off.
    init = _git_run(root, "init", "-b", "main")
    if init.returncode != 0:  # pre-2.28 git has no -b; retry plainly.
        init = _git_run(root, "init")
    if init.returncode != 0:
        _logger.warning(
            "vaults git init failed for %s: %s",
            root,
            init.stderr.decode("utf-8", errors="replace")[:200],
        )
        return "disabled"
    _git_ensure_excludes(root)
    return "ours"


def _git_commit(root: Path, rels: list[str], subject: str, sha_hex: str | None) -> str | None:
    """Path-scoped `git add` + `git commit --only`. None on success, error on failure.

    Skips the commit when there is nothing to commit. Fail-open by contract:
    callers surface the error string; the file write always stands.
    """
    add = _git_run(root, "add", "--", *rels)
    if add.returncode != 0:
        return add.stderr.decode("utf-8", errors="replace").strip()[:300]
    status = _git_run(root, "status", "--porcelain", "--", *rels)
    if status.returncode != 0:
        return status.stderr.decode("utf-8", errors="replace").strip()[:300] or "git status failed"
    if not status.stdout.strip():
        return None
    msg = subject if sha_hex is None else f"{subject}\n\nSha256: {sha_hex}"
    commit = _git_run(root, "commit", "--only", "-m", msg, "--", *rels)
    if commit.returncode != 0:
        err = (
            commit.stderr.decode("utf-8", errors="replace")
            + commit.stdout.decode("utf-8", errors="replace")
        ).strip()
        if "nothing to commit" in err:
            return None
        return err[:300] or "git commit failed"
    return None


def _git_result(root: Path, vault: str, rels: list[str], subject: str, sha_hex: str | None) -> dict:
    """One versioning step for a mutation. Never raises (fail open)."""
    try:
        mode = _git_mode(root, vault)
    except Exception as exc:
        _logger.warning("vaults git versioning failed for %s: %s", vault, exc)
        return {"versioning": "disabled", "commit_error": str(exc)[:200]}
    if mode != "ours":
        return {"versioning": mode}
    err = _git_commit(root, rels, subject, sha_hex)
    out: dict = {"versioning": "ok"}
    if err:
        out["commit_error"] = err
    return out


def _git_tracked(root: Path, rel: str) -> bool:
    """True if rel is in the vault repo index. Call only when mode is 'ours'."""
    return _git_run(root, "ls-files", "--error-unmatch", "--", rel).returncode == 0


def _git_track_before_delete(root: Path, vault: str, rel: str, sha_hex: str | None) -> str | None:
    """Commit an untracked note before delete so restore can recover it.

    Error string on failure, None when already tracked or versioning is off.
    Never raises (fail open).
    """
    try:
        if _git_mode(root, vault) != "ours" or _git_tracked(root, rel):
            return None
        return _git_commit(root, [rel], f"vaults: track before delete {rel}", sha_hex)
    except Exception as exc:
        _logger.warning("vaults pre-delete track failed for %s: %s", rel, exc)
        return str(exc)[:200]


def _git_read_mode(root: Path, vault: str) -> str:
    """Read-only mode probe for history: never inits a repo ('none' if absent)."""
    if not _git_enabled():
        return "disabled"
    if shutil.which("git") is None:
        return "missing"
    with _INDEX_LOCK:
        cached = _GIT_MODE.get(vault)
    if cached is not None:
        return cached
    if (root / ".git").exists():
        return "ours"
    if _git_run(root, "rev-parse", "--git-dir").returncode == 0:
        return "external"
    return "none"


def _git_mode_error(mode: str) -> str:
    if mode == "disabled":
        return "versioning is disabled (VAULTS_HUB_GIT=0); re-run with it unset to enable"
    if mode == "missing":
        return "git binary not found on PATH; install git to use history/restore"
    return "vault lives inside an external git repo; versioning is hands-off"


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


def _link_info(root: Path, here: Path, text: str) -> tuple[list[str], list[str], list[str]]:
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


@mcp.tool(description="Read a note: frontmatter, content, wiki-links, backlinks, unresolved links.")
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
    root: Path, p: Path, content: str, expected_sha256: str | None, op: str = "write"
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


@mcp.tool(
    description=(
        "Create or atomically overwrite a note (vault-root-relative path). "
        "Pass sha256 from a prior read/write as expected_sha256 to prevent "
        "overwriting a changed note. Refreshes frontmatter `updated:` date."
    )
)
@_logged
def write_note(vault: str, path: str, content: str, expected_sha256: str | None = None) -> dict:
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


@mcp.tool(
    description=(
        "Show version history for a note (or the whole vault) from the local "
        "git repo: [{sha, date, message}]. An untracked path returns an empty "
        "list with untracked:true."
    )
)
@_logged
def history(vault: str, path: str | None = None, limit: int = 50) -> dict:
    limit = max(1, min(200, int(limit)))
    root = _vault_root(vault)
    if path is not None:
        _note_path(root, path)  # validate: stays inside the vault
    mode = _git_read_mode(root, vault)
    if mode in ("disabled", "missing", "external"):
        raise ValueError(_git_mode_error(mode))
    if path is not None and (mode == "none" or not _git_tracked(root, path)):
        return {"vault": vault, "path": path, "limit": limit, "history": [], "untracked": True}
    args = ["log", "--no-decorate", f"--max-count={limit}", "--pretty=format:%H%x00%aI%x00%s"]
    if path is not None:
        args += ["--follow", "--", path]
    proc = _git_run(root, *args)
    if proc.returncode != 0:
        raise ValueError(
            "git log failed: " + proc.stderr.decode("utf-8", errors="replace").strip()[:200]
        )
    entries = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\x00")
        if len(parts) != 3 or not parts[0]:
            continue
        entries.append({"sha": parts[0], "date": parts[1], "message": parts[2]})
    out: dict = {"vault": vault, "path": path, "limit": limit, "history": entries}
    if path is not None:
        out["untracked"] = False
    return out


@mcp.tool(
    description=(
        "Restore a note's content from a past revision (git rev, e.g. a history "
        "sha). Snapshots dirty worktree state first; recreates deleted notes. "
        "Honors expected_sha256 against current content."
    )
)
@_logged
def restore(vault: str, path: str, rev: str, expected_sha256: str | None = None) -> dict:
    root = _vault_root(vault)
    p = _note_path(root, path)
    if p.suffix != ".md":
        raise ValueError("note path must end in .md")
    if not rev or not rev.strip():
        raise ValueError("rev must be a non-empty git revision")
    rev = rev.strip()
    with _note_lock(p):
        try:
            mode = _git_mode(root, vault)
        except Exception as exc:
            raise ValueError(f"versioning unavailable: {exc}") from exc
        if mode != "ours":
            raise ValueError(_git_mode_error(mode))
        rel = _rel(root, p)
        try:
            current = p.read_bytes() if p.is_file() else None
        except OSError as exc:
            raise ValueError(f"note not found: {path!r}") from exc
        current_sha = hashlib.sha256(current).hexdigest() if current is not None else None
        if expected_sha256 is not None and expected_sha256 != current_sha:
            raise ValueError(
                "note has changed since the supplied expected_sha256; read it again before restoring"
            )
        status = _git_run(root, "status", "--porcelain", "--", rel)
        if status.returncode == 0 and status.stdout.strip():
            snap_err = _git_commit(root, [rel], "vaults: snapshot before restore", current_sha)
            if snap_err:
                _logger.warning("vaults pre-restore snapshot failed for %s: %s", rel, snap_err)
        show = _git_run(root, "show", f"{rev}:{rel}")
        if show.returncode != 0:
            raise ValueError(f"unknown revision or path not in revision: {rev!r} for {path!r}")
        data = show.stdout
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_bytes(data)
        except OSError as exc:
            raise ValueError(f"cannot restore {path!r}: {exc}") from exc
        new_sha = hashlib.sha256(data).hexdigest()
        versioning = _git_result(root, vault, [rel], f"vaults: restore {rel} from {rev}", new_sha)
        _invalidate_vault(vault)
        return {
            "vault": vault,
            "path": rel,
            "rev": rev,
            "bytes": len(data),
            "previous_sha256": current_sha,
            "sha256": new_sha,
            **versioning,
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
    limit = max(1, min(200, int(limit)))
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
            proc = subprocess.run(args + [str(root)], capture_output=True, text=True, timeout=30)
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
            try:
                abspath = Path(d["path"]["text"]).resolve()
                rel = abspath.relative_to(root).as_posix()
                text = d["lines"]["text"].strip()
            except (KeyError, ValueError, OSError, AttributeError, TypeError):
                # Non-UTF-8 paths arrive as {"bytes": "<base64>"} with no
                # "text" key; skip rather than crash the tool.
                continue
            hits.append(
                {
                    "vault": root.name,
                    "path": rel,
                    "line": d["line_number"],
                    "text": text,
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
    read_sender, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](32)
    write_sender, write_stream = anyio.create_memory_object_stream[SessionMessage](32)

    async def write_stdout() -> None:
        async with write_stream:
            async for session_message in write_stream:
                payload = (
                    session_message.message.model_dump_json(by_alias=True, exclude_none=True) + "\n"
                )
                _write_stdout(payload)

    async with anyio.create_task_group() as task_group:
        _start_stdin_bridge(asyncio.get_running_loop(), read_sender)
        task_group.start_soon(write_stdout)
        try:
            # Pinned to mcp==1.30.0 (see requirements.txt): this uses the
            # private mcp._mcp_server protocol server plus the custom stdin
            # bridge above, because this runtime's AnyIO build hangs iterating
            # a wrapped stdin file. Keep the pin; upgrading the SDK may change
            # or remove this private API.
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


def _parse_args(argv=None):
    """CLI args parsed in __main__ only; precedence: flag > env > default."""
    parser = argparse.ArgumentParser(
        description="Serve Obsidian-style Markdown vaults over stdio (MCP)."
    )
    parser.add_argument(
        "--vaults-root",
        default=None,
        help="Root dir holding one subdir per vault. Overrides VAULTS_ROOT env; "
        "defaults to ~/.vaults. Created on startup if missing.",
    )
    return parser.parse_args(argv)


def _resolve_vaults_root(cli_value: str | None) -> Path:
    """Resolve the vaults root, creating it on startup when missing."""
    raw = cli_value or os.environ.get("VAULTS_ROOT") or str(Path.home() / ".vaults")
    root = Path(raw).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"error: cannot create vaults root {root}: {exc}") from exc
    if not root.is_dir():
        raise SystemExit(f"error: vaults root is not a directory: {root}")
    return root


if __name__ == "__main__":
    VAULTS_ROOT = _resolve_vaults_root(_parse_args().vaults_root)
    anyio.run(_serve_stdio)
