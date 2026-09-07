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
import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import date
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


def _vault_root(vault: str) -> Path:
    root = (VAULTS_ROOT / vault).resolve()
    if root.parent != VAULTS_ROOT or not root.is_dir():
        raise ValueError(f"unknown vault: {vault!r}")
    return root


def _all_notes(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md") if ".obsidian" not in p.parts)


def _rel(root: Path, p: Path) -> str:
    return p.resolve().relative_to(root).as_posix()


def _note_path(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes vault: {rel!r}")
    return p


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _split_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        data = {}
    return (data if isinstance(data, dict) else {}), text[m.end() :]


def _refresh_updated(text: str) -> tuple[str, bool]:
    """Bump `updated:` in the frontmatter block. Returns (text, changed)."""
    m = FRONTMATTER_RE.match(text)
    if not m or "updated:" not in m.group(1):
        return text, False
    new_block = UPDATED_RE.sub(
        f"updated: {date.today().isoformat()}", m.group(1), count=1
    )
    return text[: m.start(1)] + new_block + text[m.end(1) :], True


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
    description="List directories and notes under a vault path (vault-root-relative, '' for root)."
)
def list_notes(vault: str, path: str = "") -> dict:
    root = _vault_root(vault)
    base = _note_path(root, path or ".")
    if not base.is_dir():
        raise ValueError(f"not a directory: {path!r}")
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
def read_note(vault: str, path: str) -> dict:
    root = _vault_root(vault)
    p = _note_path(root, path)
    if not p.is_file():
        raise ValueError(f"note not found: {path!r}")
    text = _read_text(p)
    frontmatter, _ = _split_frontmatter(text)
    links, backlinks, unresolved = _link_info(root, p.resolve(), text)
    return {
        "vault": vault,
        "path": _rel(root, p),
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
    p.parent.mkdir(parents=True, exist_ok=True)
    previous = _read_text(p) if p.is_file() else None
    previous_sha256 = _sha256(previous) if previous is not None else None
    if expected_sha256 is not None and expected_sha256 != previous_sha256:
        raise ValueError(
            "note has changed since the supplied expected_sha256; read it again before writing"
        )
    content, refreshed = _refresh_updated(content)
    _atomic_write(p, content)
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
def append_note(
    vault: str, path: str, text: str, expected_sha256: str | None = None
) -> dict:
    root = _vault_root(vault)
    p = _note_path(root, path)
    if p.suffix != ".md":
        raise ValueError("note path must end in .md")
    with _note_lock(p):
        current = _read_text(p) if p.is_file() else ""
        if current and not current.endswith("\n"):
            current += "\n"
        return _write_note_locked(root, p, current + text, expected_sha256)


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
    description="Search note contents with ripgrep across one vault or all vaults."
)
def search_notes(
    query: str,
    vault: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = 50,
) -> dict:
    roots = [_vault_root(vault)] if vault else [Path(v["path"]) for v in list_vaults()]
    rg = shutil.which("rg")
    args = ["rg", "--json", "-n", "--no-heading", "--glob", "!**/.obsidian/**"]
    if not regex:
        args.append("--fixed-strings")
    if not case_sensitive:
        args.append("--ignore-case")
    args += ["--max-count", "20", "--", query]
    hits: list[dict] = []
    if rg:
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
    else:  # fallback: plain Python scan
        flags = 0 if case_sensitive else re.IGNORECASE
        pat = re.compile(query if regex else re.escape(query), flags)
        per_file_limit = 20
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
                        if file_hits >= per_file_limit:
                            break
                        if len(hits) >= limit:
                            break
                if len(hits) >= limit:
                    break
    return {"query": query, "matches": hits[:limit]}


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
