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
        "---\ntitle: Termchat Home\nupdated: 2000-01-01\n---\n\n[[Architecture]] and [[Missing]]\n",
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
                    "create_vault",
                    "delete_note",
                    "history",
                    "list_notes",
                    "list_tags",
                    "list_vaults",
                    "move_note",
                    "read_note",
                    "restore",
                    "search_notes",
                    "write_note",
                ], tools

                listed = out(await session.call_tool("list_vaults", {}))
                assert {item["name"]: item["notes"] for item in listed} == {
                    "Runnix": 1,
                    "Termchat": 3,
                }

                made = out(await session.call_tool("create_vault", {"vault": "Newvault"}))
                assert made["created"] is True, made
                assert (vaults / "Newvault").is_dir()
                again = out(await session.call_tool("create_vault", {"vault": "Newvault"}))
                assert again["created"] is False, again
                bad_name = await session.call_tool("create_vault", {"vault": "../evil"})
                assert bad_name.isError, "vault traversal was not rejected"
                relisted = out(await session.call_tool("list_vaults", {}))
                assert "Newvault" in {item["name"] for item in relisted}, relisted

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
                    await session.call_tool("search_notes", {"query": "Runnix", "vault": "Runnix"})
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
                assert "updated: 2000-01-01" not in (vaults / "Termchat" / "Scratch.md").read_text(
                    encoding="utf-8"
                )

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

                # F: recursive list_notes
                await session.call_tool(
                    "write_note",
                    {
                        "vault": "Termchat",
                        "path": "Sub/inner.md",
                        "content": "inner\n",
                    },
                )
                flat = out(await session.call_tool("list_notes", {"vault": "Termchat", "path": ""}))
                assert "Sub/inner.md" not in flat["notes"], flat
                rec = out(
                    await session.call_tool(
                        "list_notes",
                        {"vault": "Termchat", "path": "", "recursive": True},
                    )
                )
                assert rec["dirs"] == [], rec
                assert "Sub/inner.md" in rec["notes"], rec

                # D + G: tags (list form + string form, normalized to list)
                await session.call_tool(
                    "write_note",
                    {
                        "vault": "Termchat",
                        "path": "TagList.md",
                        "content": "---\ntags: [a/b, c]\n---\nbody\n",
                    },
                )
                await session.call_tool(
                    "write_note",
                    {
                        "vault": "Termchat",
                        "path": "TagStr.md",
                        "content": "---\ntags: d\n---\nbody\n",
                    },
                )
                tag_counts = out(await session.call_tool("list_tags", {"vault": "Termchat"}))
                assert tag_counts["Termchat"].get("a/b") == 1, tag_counts
                assert tag_counts["Termchat"].get("c") == 1, tag_counts
                assert tag_counts["Termchat"].get("d") == 1, tag_counts
                tag_str = out(
                    await session.call_tool("read_note", {"vault": "Termchat", "path": "TagStr.md"})
                )
                assert tag_str["frontmatter"]["tags"] == ["d"], tag_str
                tag_list = out(
                    await session.call_tool(
                        "read_note", {"vault": "Termchat", "path": "TagList.md"}
                    )
                )
                assert tag_list["frontmatter"]["tags"] == ["a/b", "c"], tag_list

                # E: search context
                ctx_body = "l1\nl2\nl3 target\nl4\nl5\n"
                await session.call_tool(
                    "write_note",
                    {
                        "vault": "Termchat",
                        "path": "Context.md",
                        "content": ctx_body,
                    },
                )
                ctx = out(
                    await session.call_tool(
                        "search_notes",
                        {
                            "query": "l3 target",
                            "vault": "Termchat",
                            "before": 2,
                            "after": 2,
                        },
                    )
                )
                assert ctx["matches"], "expected context search hit"
                hit = next(m for m in ctx["matches"] if m["path"] == "Context.md")
                assert hit["before_lines"] == ["l1", "l2"], hit
                assert hit["match_line"] == "l3 target", hit
                assert hit["after_lines"] == ["l4", "l5"], hit
                assert hit["text"] == hit["match_line"], hit

                # C: delete_note + move_note
                await session.call_tool(
                    "write_note",
                    {
                        "vault": "Termchat",
                        "path": "ToDelete.md",
                        "content": "bye\n",
                    },
                )
                deleted = out(
                    await session.call_tool(
                        "delete_note",
                        {"vault": "Termchat", "path": "ToDelete.md"},
                    )
                )
                assert len(deleted["sha256_before"]) == 64, deleted
                gone = await session.call_tool(
                    "read_note", {"vault": "Termchat", "path": "ToDelete.md"}
                )
                assert gone.isError, "deleted note still readable"
                missing = await session.call_tool(
                    "delete_note",
                    {"vault": "Termchat", "path": "ToDelete.md"},
                )
                assert missing.isError, "missing delete was not rejected"
                missing_ok = out(
                    await session.call_tool(
                        "delete_note",
                        {
                            "vault": "Termchat",
                            "path": "ToDelete.md",
                            "missing_ok": True,
                        },
                    )
                )
                assert missing_ok["sha256_before"] is None, missing_ok
                # backlinks follow a move (source path updates on target)
                await session.call_tool(
                    "write_note",
                    {
                        "vault": "Termchat",
                        "path": "Linker.md",
                        "content": "hello\n",
                    },
                )
                await session.call_tool(
                    "write_note",
                    {
                        "vault": "Termchat",
                        "path": "OldName.md",
                        "content": "[[Linker]]\n",
                    },
                )
                linker_before = out(
                    await session.call_tool("read_note", {"vault": "Termchat", "path": "Linker.md"})
                )
                assert linker_before["backlinks"] == ["OldName.md"], linker_before
                moved = out(
                    await session.call_tool(
                        "move_note",
                        {
                            "vault": "Termchat",
                            "src_path": "OldName.md",
                            "dst_path": "NewName.md",
                        },
                    )
                )
                assert moved["dst_path"] == "NewName.md", moved
                assert len(moved["sha256"]) == 64, moved
                old_gone = await session.call_tool(
                    "read_note", {"vault": "Termchat", "path": "OldName.md"}
                )
                assert old_gone.isError, "move source still readable"
                new = out(
                    await session.call_tool(
                        "read_note", {"vault": "Termchat", "path": "NewName.md"}
                    )
                )
                assert new["links"] == ["Linker"], new
                linker_after = out(
                    await session.call_tool("read_note", {"vault": "Termchat", "path": "Linker.md"})
                )
                assert linker_after["backlinks"] == ["NewName.md"], linker_after
                bad_src = await session.call_tool(
                    "move_note",
                    {
                        "vault": "Termchat",
                        "src_path": "Nope.md",
                        "dst_path": "Elsewhere.md",
                    },
                )
                assert bad_src.isError, "missing move source not rejected"
                bad_dst = await session.call_tool(
                    "move_note",
                    {
                        "vault": "Termchat",
                        "src_path": "NewName.md",
                        "dst_path": "Termchat.md",
                    },
                )
                assert bad_dst.isError, "existing destination not rejected"
                print("MCP operations OK")

                # H: git versioning (history + restore)
                v1 = out(
                    await session.call_tool(
                        "write_note",
                        {"vault": "Termchat", "path": "Vcs.md", "content": "v1\n"},
                    )
                )
                assert v1["versioning"] == "ok", v1
                assert (vaults / "Termchat" / ".git").is_dir(), "vault repo not initialized"
                hist = out(
                    await session.call_tool("history", {"vault": "Termchat", "path": "Vcs.md"})
                )
                assert hist["untracked"] is False, hist
                assert len(hist["history"]) == 1, hist
                assert "write" in hist["history"][0]["message"], hist
                assert len(hist["history"][0]["sha"]) == 40, hist
                await session.call_tool(
                    "write_note",
                    {"vault": "Termchat", "path": "Vcs.md", "content": "v2\n"},
                )
                hist2 = out(
                    await session.call_tool("history", {"vault": "Termchat", "path": "Vcs.md"})
                )
                assert len(hist2["history"]) == 2, hist2
                assert hist2["history"][0]["date"], hist2
                fresh_hist = out(
                    await session.call_tool(
                        "history", {"vault": "Termchat", "path": "NeverWritten.md"}
                    )
                )
                assert fresh_hist["history"] == [] and fresh_hist["untracked"] is True, fresh_hist
                await session.call_tool("delete_note", {"vault": "Termchat", "path": "Vcs.md"})
                assert not (vaults / "Termchat" / "Vcs.md").exists()
                first_sha = hist2["history"][-1]["sha"]
                restored = out(
                    await session.call_tool(
                        "restore",
                        {"vault": "Termchat", "path": "Vcs.md", "rev": first_sha},
                    )
                )
                assert (vaults / "Termchat" / "Vcs.md").read_text(encoding="utf-8") == "v1\n", (
                    restored
                )
                assert restored["sha256"] == v1["sha256"], restored
                bad_rev = await session.call_tool(
                    "restore",
                    {"vault": "Termchat", "path": "Vcs.md", "rev": "deadbeef" * 5},
                )
                assert bad_rev.isError, "bogus rev was not rejected"
                print("VCS operations OK")

        # I: VAULTS_HUB_GIT=0 disables versioning (separate server process)
        params_off = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER)],
            env={
                "VAULTS_ROOT": str(vaults),
                "VAULTS_HUB_GIT": "0",
                "PYTHONUNBUFFERED": "1",
            },
        )
        async with stdio_client(params_off) as (read, write):
            async with ClientSession(read, write) as off:
                await off.initialize()
                w = out(
                    await off.call_tool(
                        "write_note",
                        {"vault": "Runnix", "path": "Off.md", "content": "x\n"},
                    )
                )
                assert w["versioning"] == "disabled", w
                assert (vaults / "Runnix" / "Off.md").is_file()
                h = await off.call_tool("history", {"vault": "Runnix", "path": "notes.md"})
                assert h.isError, "history with VAULTS_HUB_GIT=0 must error"
                r = await off.call_tool(
                    "restore", {"vault": "Runnix", "path": "notes.md", "rev": "HEAD"}
                )
                assert r.isError, "restore with VAULTS_HUB_GIT=0 must error"
                print("VCS opt-out OK")

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
