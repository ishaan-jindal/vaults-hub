"""End-to-end smoke test for the vaults hub over a temporary stdio MCP server."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"


def out(res):
    data = (
        res.structuredContent
        if res.structuredContent is not None
        else json.loads(res.content[0].text)
    )
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def write_fixture(root: Path) -> None:
    (root / "Termchat").mkdir()
    (root / "Runnix").mkdir()
    (root / "Termchat" / "Termchat.md").write_text(
        "---\ntitle: Termchat Home\nupdated: 2000-01-01\n---\n"
        "\n[[Architecture]] and [[Missing]]\n",
        encoding="utf-8",
    )
    (root / "Termchat" / "Architecture.md").write_text(
        "# Architecture\n\n[[Termchat]]\n", encoding="utf-8"
    )
    (root / "Termchat" / "Broken.md").write_bytes(b"\xff\xfe broken bytes \x00\x01")
    (root / "Runnix" / "notes.md").write_text("Runnix is ready.\n", encoding="utf-8")


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="vaults-hub-") as temp_dir:
        vaults = Path(temp_dir)
        write_fixture(vaults)
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER)],
            env={"VAULTS_ROOT": str(vaults), "PYTHONUNBUFFERED": "1"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("initialized")

                tools = sorted(tool.name for tool in (await session.list_tools()).tools)
                assert tools == [
                    "append_note",
                    "list_notes",
                    "list_vaults",
                    "read_note",
                    "search_notes",
                    "write_note",
                ], tools

                listed = out(await session.call_tool("list_vaults", {}))
                assert {item["name"]: item["notes"] for item in listed} == {
                    "Runnix": 1,
                    "Termchat": 3,
                }

                home = out(
                    await session.call_tool(
                        "read_note", {"vault": "Termchat", "path": "Termchat.md"}
                    )
                )
                assert home["frontmatter"]["title"] == "Termchat Home"
                assert home["links"] == ["Architecture"]
                assert home["backlinks"] == ["Architecture.md"]
                assert home["unresolved_links"] == ["Missing"]

                broken = await session.call_tool(
                    "read_note", {"vault": "Termchat", "path": "Broken.md"}
                )
                assert not broken.isError, "non-UTF-8 note must not crash read_note"

                hits = out(
                    await session.call_tool(
                        "search_notes", {"query": "Runnix", "vault": "Runnix"}
                    )
                )
                assert hits["matches"], "expected fixture search hit"

                body = "---\ntitle: Hub Smoke\nupdated: 2000-01-01\n---\n\nsmoke\n"
                created = out(
                    await session.call_tool(
                        "write_note",
                        {"vault": "Termchat", "path": "Scratch.md", "content": body},
                    )
                )
                assert created["previous_sha256"] is None
                assert len(created["sha256"]) == 64
                assert "updated: 2000-01-01" not in (
                    vaults / "Termchat" / "Scratch.md"
                ).read_text(encoding="utf-8")

                appended = out(
                    await session.call_tool(
                        "append_note",
                        {
                            "vault": "Termchat",
                            "path": "Scratch.md",
                            "text": "more\n",
                            "expected_sha256": created["sha256"],
                        },
                    )
                )
                assert appended["previous_sha256"] == created["sha256"]

                await session.call_tool(
                    "write_note",
                    {
                        "vault": "Termchat",
                        "path": "Scratch.md",
                        "content": "external\n",
                    },
                )
                conflict = await session.call_tool(
                    "write_note",
                    {
                        "vault": "Termchat",
                        "path": "Scratch.md",
                        "content": "stale\n",
                        "expected_sha256": appended["sha256"],
                    },
                )
                assert conflict.isError, "stale write was not rejected"
                assert not list((vaults / "Termchat").glob(".Scratch.md.*.tmp"))

                traversal = await session.call_tool(
                    "read_note", {"vault": "Termchat", "path": "../outside.md"}
                )
                assert traversal.isError, "path traversal was not rejected"
                print("MCP operations OK")

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
