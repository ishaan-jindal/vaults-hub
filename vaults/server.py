"""FastMCP stdio server: thin @mcp.tool wrappers over the vaults package."""

import asyncio
import sys
import threading

import anyio
import mcp.types as mcp_types
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage

from vaults import config
from vaults import indexes as _indexes
from vaults import notes as _notes
from vaults import search as _search
from vaults import versioning as _versioning
from vaults.config import _logged, _parse_args, _resolve_vaults_root

mcp = FastMCP("vaults")


@mcp.tool(description="List all project vaults with their note counts.")
@_logged
def list_vaults() -> list[dict]:
    return _notes.list_vaults()


@mcp.tool(
    description="List directories and notes under a vault path (vault-root-relative, '' for root). Pass recursive=True to list all descendant notes flat (dirs=[])."
)
@_logged
def list_notes(vault: str, path: str = "", recursive: bool = False) -> dict:
    return _notes.list_notes(vault, path, recursive)


@mcp.tool(description="Read a note: frontmatter, content, wiki-links, backlinks, unresolved links.")
@_logged
def read_note(vault: str, path: str) -> dict:
    return _notes.read_note(vault, path)


@mcp.tool(
    description=(
        "Create or atomically overwrite a note (vault-root-relative path). "
        "Pass sha256 from a prior read/write as expected_sha256 to prevent "
        "overwriting a changed note. Refreshes frontmatter `updated:` date."
    )
)
@_logged
def write_note(vault: str, path: str, content: str, expected_sha256: str | None = None) -> dict:
    return _notes.write_note(vault, path, content, expected_sha256)


@mcp.tool(
    description=(
        "Append text to a note (created if missing) via an atomic write. "
        "Optionally pass expected_sha256 to prevent appending to a changed note. "
        "Refreshes frontmatter `updated:` date."
    )
)
@_logged
def append_note(vault: str, path: str, text: str, expected_sha256: str | None = None) -> dict:
    return _notes.append_note(vault, path, text, expected_sha256)


@mcp.tool(
    description=(
        "Delete a note file. Errors when missing unless missing_ok=True. "
        "Cleans up sibling .lock/.tmp files best-effort."
    )
)
@_logged
def delete_note(vault: str, path: str, missing_ok: bool = False) -> dict:
    return _notes.delete_note(vault, path, missing_ok)


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
    return _notes.move_note(vault, src_path, dst_path, expected_sha256)


@mcp.tool(
    description=(
        "Show version history for a note (or the whole vault) from the local "
        "git repo: [{sha, date, message}]. An untracked path returns an empty "
        "list with untracked:true."
    )
)
@_logged
def history(vault: str, path: str | None = None, limit: int = 50) -> dict:
    return _versioning.history(vault, path, limit)


@mcp.tool(
    description=(
        "Restore a note's content from a past revision (git rev, e.g. a history "
        "sha). Snapshots dirty worktree state first; recreates deleted notes. "
        "Honors expected_sha256 against current content."
    )
)
@_logged
def restore(vault: str, path: str, rev: str, expected_sha256: str | None = None) -> dict:
    return _versioning.restore(vault, path, rev, expected_sha256)


@mcp.tool(
    description=(
        "List tag counts per vault from cached frontmatter. "
        "Pass vault for one vault, or omit for all vaults."
    )
)
@_logged
def list_tags(vault: str | None = None) -> dict:
    return _indexes.list_tags(vault)


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
    return _search.search_notes(query, vault, regex, case_sensitive, limit, before, after)


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


def main(argv=None) -> None:
    """Entry point: resolve the vaults root, then serve MCP over stdio."""
    config.VAULTS_ROOT = _resolve_vaults_root(_parse_args(argv).vaults_root)
    anyio.run(_serve_stdio)


if __name__ == "__main__":
    main()
