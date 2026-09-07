"""End-to-end smoke test for the vaults hub: spawns server.py over stdio."""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
SCRATCH = ("Termchat", "08-Changelog/_hub-smoke.md")


def out(res):
    data = (
        res.structured_content
        if res.structured_content is not None
        else json.loads(res.content[0].text)
    )
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            tools = sorted(t.name for t in (await s.list_tools()).tools)
            print("tools:", tools)
            assert tools == [
                "append_note",
                "list_notes",
                "list_vaults",
                "read_note",
                "search_notes",
                "write_note",
            ], tools

            vaults = out(await s.call_tool("list_vaults", {}))
            print("vaults:", vaults)
            by_name = {v["name"]: v["notes"] for v in vaults}
            expected = {
                d.name: len([p for p in d.rglob("*.md") if ".obsidian" not in p.parts])
                for d in (HERE.parent / "vaults").iterdir()
                if d.is_dir()
            }
            assert by_name == expected, (by_name, expected)

            home = out(
                await s.call_tool(
                    "read_note", {"vault": "Termchat", "path": "Termchat.md"}
                )
            )
            assert home["frontmatter"].get("title") == "Termchat Home", home[
                "frontmatter"
            ]
            assert "Architecture" in home["links"], home["links"]
            assert home["unresolved_links"] == [], home["unresolved_links"]
            disk = (HERE.parent / "vaults" / "Termchat" / "Termchat.md").read_text()
            assert home["content"] == disk, "hub content must byte-match disk"

            hits = out(
                await s.call_tool(
                    "search_notes", {"query": "Runnix", "vault": "Runnix"}
                )
            )
            assert hits["matches"], "expected search hits in Runnix"
            print("sample hit:", hits["matches"][0])

            body = "---\ntitle: Hub Smoke\nupdated: 2000-01-01\n---\n\nsmoke\n"
            w = out(
                await s.call_tool(
                    "write_note",
                    {"vault": SCRATCH[0], "path": SCRATCH[1], "content": body},
                )
            )
            assert w["updated_refreshed"] is True, w
            back = out(
                await s.call_tool(
                    "read_note", {"vault": SCRATCH[0], "path": SCRATCH[1]}
                )
            )
            assert "updated: 2000-01-01" not in back["content"], (
                "updated date must be refreshed"
            )
            a = out(
                await s.call_tool(
                    "append_note",
                    {"vault": SCRATCH[0], "path": SCRATCH[1], "text": "more\n"},
                )
            )
            assert a["bytes"] > w["bytes"], (a, w)
            (HERE.parent / "vaults" / SCRATCH[0] / SCRATCH[1]).unlink()
            print("write/append round-trip ok (scratch removed)")

            res = await s.call_tool(
                "read_note", {"vault": "Termchat", "path": "../x.md"}
            )
            print("is_error:", res.is_error)
            print("content:", res.content)
            assert res.is_error, "path traversal was NOT rejected"
            print("traversal guard ok")

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
