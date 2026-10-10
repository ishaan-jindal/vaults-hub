"""FastMCP stdio server: thin @mcp.tool wrappers over the vaults package."""

import functools
import platform
import shutil
import sys
from collections.abc import Callable
from typing import Annotated, Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from vaults import __version__ as SERVER_VERSION
from vaults import config
from vaults import indexes as _indexes
from vaults import notes as _notes
from vaults import search as _search
from vaults import versioning as _versioning
from vaults.config import _logger, _parse_args, _resolve_vaults_root

mcp = FastMCP(
    "vaults",
    instructions=(
        "Start with list_vaults to discover the available vaults, then list_notes "
        "to find notes. Always read_note before mutating a note, and pass "
        "expected_sha256 from the read to avoid clobbering concurrent edits. "
        "Use history and restore to recover earlier versions of a note."
    ),
)
# The installed SDK (mcp==1.30.0) exposes the protocol name/version as plain
# attributes on the low-level server, and create_initialization_options()
# reads self.version, so this surfaces SERVER_VERSION in the handshake.
mcp._mcp_server.version = SERVER_VERSION  # noqa: SLF001 - FastMCP's protocol server


async def _run_off_loop(tool_name: str, fn: Callable, /, **kwargs: Any) -> Any:
    """Run a blocking core call in a worker thread; log it and sanitize unexpected errors.

    Only vault and path are logged, never content. ValueError passes through
    unchanged (core ValueErrors are client-safe, including the expected_sha256
    conflict wording). Anything else becomes a ValueError with no paths or
    internals; every failure's full traceback goes to the server log.
    """
    path = kwargs.get("path", kwargs.get("src_path"))
    _logger.info("vaults.%s called vault=%r path=%r", tool_name, kwargs.get("vault"), path)
    try:
        result = await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))
    except Exception as exc:
        _logger.error("vaults.%s failed", tool_name, exc_info=True)
        if isinstance(exc, ValueError):
            raise
        raise ValueError(
            f"{tool_name} failed: {type(exc).__name__}: check the server log for details"
        ) from exc
    _logger.info("vaults.%s ok", tool_name)
    return result


@mcp.tool(
    description="List all project vaults with their note counts.",
    annotations=ToolAnnotations(
        title="List vaults",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def list_vaults() -> list[dict]:
    return await _run_off_loop(
        "list_vaults",
        _notes.list_vaults,
    )


@mcp.tool(
    description="Create a new vault (one subdirectory of the vaults root). "
    "Idempotent: returns created=False when the vault already exists.",
    annotations=ToolAnnotations(
        title="Create vault",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def create_vault(vault: Annotated[str, Field(description="Vault name")]) -> dict:
    return await _run_off_loop("create_vault", _notes.create_vault, vault=vault)


@mcp.tool(
    description="Delete a whole vault: removes every note, lock file and the git history. "
    "Irreversible with no undo in this server; confirm must exactly equal the vault name.",
    annotations=ToolAnnotations(
        title="Delete vault",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def delete_vault(
    vault: Annotated[str, Field(description="Vault name")],
    confirm: Annotated[str, Field(description="Must exactly equal the vault name")],
) -> dict:
    return await _run_off_loop("delete_vault", _notes.delete_vault, vault=vault, confirm=confirm)


@mcp.tool(
    description="List directories and notes under a vault path (vault-root-relative, "
    "'' for root). Pass recursive=True to list all descendant notes flat (dirs=[]). "
    "Paginate with offset/limit (default limit 200, max 500); the response carries "
    "total (full match count), truncated, and next_offset (None when done).",
    annotations=ToolAnnotations(
        title="List notes",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def list_notes(
    vault: Annotated[str, Field(description="Vault name")],
    path: Annotated[str, Field(description="Vault-root-relative dir; '' for root")] = "",
    recursive: Annotated[bool, Field(description="Flat recursive listing (dirs=[])")] = False,
    offset: Annotated[int, Field(ge=0, description="Skip this many entries")] = 0,
    limit: Annotated[int, Field(ge=1, le=500, description="Max entries (default 200)")] = 200,
) -> dict:
    return await _run_off_loop(
        "list_notes",
        _notes.list_notes,
        vault=vault,
        path=path,
        recursive=recursive,
        offset=offset,
        limit=limit,
    )


@mcp.tool(
    description="Read a note: frontmatter, content, wiki-links, backlinks, unresolved links.",
    annotations=ToolAnnotations(
        title="Read note",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def read_note(
    vault: Annotated[str, Field(description="Vault name")],
    path: Annotated[str, Field(description="Vault-root-relative note path")],
) -> dict:
    return await _run_off_loop("read_note", _notes.read_note, vault=vault, path=path)


@mcp.tool(
    description=(
        "Create or atomically overwrite a note (vault-root-relative path). "
        "Pass sha256 from a prior read/write as expected_sha256 to prevent "
        "overwriting a changed note. Refreshes the frontmatter `updated:` date "
        "(inserted when a frontmatter block exists without one); notes without "
        "a frontmatter block stay that way."
    ),
    annotations=ToolAnnotations(
        title="Write note",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def write_note(
    vault: Annotated[str, Field(description="Vault name")],
    path: Annotated[str, Field(description="Vault-root-relative note path")],
    content: Annotated[str, Field(description="Full new note content")],
    expected_sha256: Annotated[
        str | None, Field(description="SHA-256 from a prior read/write; rejects stale writes")
    ] = None,
) -> dict:
    return await _run_off_loop(
        "write_note",
        _notes.write_note,
        vault=vault,
        path=path,
        content=content,
        expected_sha256=expected_sha256,
    )


@mcp.tool(
    description=(
        "Append text to a note (created if missing) via an atomic write. "
        "Optionally pass expected_sha256 to prevent appending to a changed note. "
        "Refreshes the frontmatter `updated:` date (inserted when a frontmatter "
        "block exists without one)."
    ),
    annotations=ToolAnnotations(
        title="Append to note",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def append_note(
    vault: Annotated[str, Field(description="Vault name")],
    path: Annotated[str, Field(description="Vault-root-relative note path")],
    text: Annotated[str, Field(description="Text to append")],
    expected_sha256: Annotated[
        str | None, Field(description="SHA-256 from a prior read/write; rejects stale appends")
    ] = None,
) -> dict:
    return await _run_off_loop(
        "append_note",
        _notes.append_note,
        vault=vault,
        path=path,
        text=text,
        expected_sha256=expected_sha256,
    )


@mcp.tool(
    description=(
        "Delete a note file. Errors when missing unless missing_ok=True. "
        "Sibling tmp files are cleaned up best-effort; lock files are "
        "intentionally left in place."
    ),
    annotations=ToolAnnotations(
        title="Delete note",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def delete_note(
    vault: Annotated[str, Field(description="Vault name")],
    path: Annotated[str, Field(description="Vault-root-relative note path")],
    missing_ok: Annotated[bool, Field(description="Return success when already absent")] = False,
) -> dict:
    return await _run_off_loop(
        "delete_note", _notes.delete_note, vault=vault, path=path, missing_ok=missing_ok
    )


@mcp.tool(
    description=(
        "Move/rename a note via copy+delete (not atomic): the destination is "
        "written atomically, then the source is unlinked, and a partial "
        "destination is cleaned up on failure. Errors if src is missing or dst "
        "exists. Honors expected_sha256 against src. Refreshes `updated:`."
    ),
    annotations=ToolAnnotations(
        title="Move note",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def move_note(
    vault: Annotated[str, Field(description="Vault name")],
    src_path: Annotated[str, Field(description="Existing note path")],
    dst_path: Annotated[str, Field(description="New note path (must not exist)")],
    expected_sha256: Annotated[
        str | None, Field(description="SHA-256 of src from a prior read; rejects stale moves")
    ] = None,
) -> dict:
    return await _run_off_loop(
        "move_note",
        _notes.move_note,
        vault=vault,
        src_path=src_path,
        dst_path=dst_path,
        expected_sha256=expected_sha256,
    )


@mcp.tool(
    description=(
        "Show version history for a note (or the whole vault) from the local "
        "git repo: [{sha, date, message}] with a truncated flag when more "
        "commits exist beyond limit (clamped 1-200). An untracked path returns "
        "an empty list with untracked:true."
    ),
    annotations=ToolAnnotations(
        title="Note history",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def history(
    vault: Annotated[str, Field(description="Vault name")],
    path: Annotated[str, Field(description="Note path; omit for whole-vault history")]
    | None = None,
    limit: Annotated[int, Field(ge=1, le=200, description="Max commits (default 50)")] = 50,
) -> dict:
    return await _run_off_loop("history", _versioning.history, vault=vault, path=path, limit=limit)


@mcp.tool(
    description=(
        "Restore a note's content from a past revision (git rev, e.g. a history "
        "sha); the response echoes the revision as restored_from. Snapshots "
        "dirty worktree state first; recreates deleted notes. "
        "Honors expected_sha256 against current content."
    ),
    annotations=ToolAnnotations(
        title="Restore note",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def restore(
    vault: Annotated[str, Field(description="Vault name")],
    path: Annotated[str, Field(description="Vault-root-relative note path")],
    rev: Annotated[str, Field(description="Git revision (e.g. a history sha)")],
    expected_sha256: Annotated[
        str | None, Field(description="SHA-256 of current content; rejects stale restores")
    ] = None,
) -> dict:
    return await _run_off_loop(
        "restore",
        _versioning.restore,
        vault=vault,
        path=path,
        rev=rev,
        expected_sha256=expected_sha256,
    )


@mcp.tool(
    description=(
        "List tag counts per vault from cached frontmatter. "
        "Pass vault for one vault, or omit for all vaults."
    ),
    annotations=ToolAnnotations(
        title="List tags",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def list_tags(
    vault: Annotated[str | None, Field(description="Vault name; omit for all vaults")] = None,
) -> dict:
    return await _run_off_loop("list_tags", _indexes.list_tags, vault=vault)


@mcp.tool(
    description=(
        "Search .md note contents with ripgrep across one vault or all vaults. "
        "regex=True uses ripgrep's Rust syntax (Python re when ripgrep is absent), "
        "so lookarounds and backreferences are not portable. "
        "limit is clamped 1-200 and the response carries a truncated flag; "
        "before/after (0-10) add context lines. With context each hit keeps "
        "text (= match_line) plus before_lines/match_line/after_lines, "
        "otherwise only text."
    ),
    annotations=ToolAnnotations(
        title="Search notes",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def search_notes(
    query: Annotated[str, Field(description="Search text (or regex with regex=True)")],
    vault: Annotated[str | None, Field(description="Vault name; omit to search all")] = None,
    regex: Annotated[bool, Field(description="Treat query as a regular expression")] = False,
    case_sensitive: Annotated[bool, Field(description="Case-sensitive matching")] = False,
    limit: Annotated[int, Field(ge=1, le=200, description="Max matches (default 50)")] = 50,
    before: Annotated[int, Field(ge=0, le=10, description="Context lines before each hit")] = 0,
    after: Annotated[int, Field(ge=0, le=10, description="Context lines after each hit")] = 0,
) -> dict:
    return await _run_off_loop(
        "search_notes",
        _search.search_notes,
        query=query,
        vault=vault,
        regex=regex,
        case_sensitive=case_sensitive,
        limit=limit,
        before=before,
        after=after,
    )


@mcp.tool(
    description=(
        "Report server version, Python/platform, git and ripgrep availability, "
        "whether git versioning is enabled, the local vaults root path, and "
        "the vault count. This is a local stdio server; the vaults root path "
        "is intentionally exposed."
    ),
    annotations=ToolAnnotations(
        title="Server info",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def server_info() -> dict:
    return await _run_off_loop("server_info", _collect_server_info)


def _collect_server_info() -> dict:
    """Gather local server facts (runs in a worker thread via server_info)."""
    return {
        "version": SERVER_VERSION,
        "python": platform.python_version(),
        "platform": sys.platform,
        "git_available": shutil.which("git") is not None,
        "ripgrep_available": shutil.which("rg") is not None,
        "git_enabled": config._git_enabled(),
        "vaults_root": str(config.VAULTS_ROOT),
        "vault_count": len(_notes.list_vaults()),
    }


def main(argv=None) -> None:
    """Entry point: resolve the vaults root, then serve MCP over stdio."""
    config.VAULTS_ROOT = _resolve_vaults_root(_parse_args(argv).vaults_root)
    mcp.run()


if __name__ == "__main__":
    main()
