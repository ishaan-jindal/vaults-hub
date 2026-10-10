"""End-to-end smoke test for the vaults hub over a temporary stdio MCP server."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"

EXPECTED_TOOLS = [
    "append_note",
    "create_vault",
    "delete_note",
    "delete_vault",
    "history",
    "list_notes",
    "list_tags",
    "list_vaults",
    "move_note",
    "read_note",
    "restore",
    "search_notes",
    "server_info",
    "write_note",
]

READ_ONLY_TOOLS = {
    "list_vaults",
    "list_notes",
    "read_note",
    "list_tags",
    "search_notes",
    "history",
    "server_info",
}

DESTRUCTIVE_TOOLS = {
    "write_note",
    "append_note",
    "move_note",
    "delete_note",
    "delete_vault",
    "restore",
}


def out(res):
    data = (
        res.structuredContent
        if res.structuredContent is not None
        else json.loads(res.content[0].text)
    )
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def err_text(res):
    return res.content[0].text if res.content else ""


def _path_without_rg(scratch: Path) -> str:
    """A PATH with ripgrep hidden, so the server falls back to pure-Python search.

    PATH is directory-grained, so when rg lives alongside git (both are in
    /usr/bin on ubuntu runners) the whole dir must go; git is then kept
    reachable via one symlink in a scratch dir. Only `rg`/`git` matter here:
    the server is spawned via an absolute sys.executable, so nothing else on
    PATH is needed by this leg.
    """
    rg = shutil.which("rg")
    if rg is None:
        return os.environ.get("PATH", "")
    # Every PATH dir holding an executable `rg` must go: which() only reports
    # the first, but the server would happily find the second.
    rg_dirs = {
        d
        for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and os.path.isfile(os.path.join(d, "rg")) and os.access(os.path.join(d, "rg"), os.X_OK)
    }
    kept = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and d not in rg_dirs]
    git = shutil.which("git")
    if git is not None and str(Path(git).parent) in rg_dirs:
        scratch.mkdir(parents=True, exist_ok=True)
        link = scratch / "git"
        if not link.exists():
            os.symlink(git, link)
        kept.insert(0, str(scratch))
    return os.pathsep.join(kept)


async def _write_search_corpus(session) -> None:
    await session.call_tool("create_vault", {"vault": "Searchvault"})
    corpus = [
        ("alpha.md", "the quick brown fox\njumps over the lazy dog\nfox again here\n"),
        ("beta.md", "FOX in capitals\nnothing relevant\n"),
        ("gamma.md", "regex target abc123\nanother abc456 line\nplain filler\n"),
    ]
    for path, content in corpus:
        res = await session.call_tool(
            "write_note", {"vault": "Searchvault", "path": path, "content": content}
        )
        assert not res.isError, err_text(res)


async def _collect_search_sets(session) -> dict:
    """Full (untruncated) match sets plus the per-backend truncation contract.

    Only the full sets are parity-compared across backends: match ORDER is
    backend-dependent, so which single hit survives a limit cut can differ and
    only the (limit/truncated/count) contract is pinned for those queries.
    """
    sets = {}
    queries = {
        "plain": {"query": "fox", "vault": "Searchvault"},
        "regex": {"query": r"abc\d+", "vault": "Searchvault", "regex": True},
        "casesens": {"query": "FOX", "vault": "Searchvault", "case_sensitive": True},
    }
    for key, kwargs in queries.items():
        res = out(await session.call_tool("search_notes", kwargs))
        assert res["truncated"] is False, res
        sets[key] = sorted((m["vault"], m["path"], m["line"], m["text"]) for m in res["matches"])
    assert len(sets["plain"]) == 3, sets  # alpha x2 (any case) + beta x1
    assert len(sets["regex"]) == 2, sets
    assert len(sets["casesens"]) == 1, sets
    for limit in (1, 2):
        cut = out(
            await session.call_tool(
                "search_notes", {"query": "fox", "vault": "Searchvault", "limit": limit}
            )
        )
        assert cut["limit"] == limit, cut
        assert len(cut["matches"]) == limit, cut
        assert cut["truncated"] is True, cut
    return sets


async def _search_fallback_leg(vaults: Path, rg_sets: dict, scratch: Path) -> None:
    """Re-run the search probes with rg hidden; results must equal the rg path."""
    doctored = _path_without_rg(scratch)
    fake_home = vaults / ".fakehome-search"
    (fake_home / ".cache").mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        env={
            "VAULTS_ROOT": str(vaults),
            "PYTHONUNBUFFERED": "1",
            "PATH": doctored,
            "HOME": str(fake_home),
            "XDG_CACHE_HOME": str(fake_home / ".cache"),
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as fallback:
            await fallback.initialize()
            fb_info = out(await fallback.call_tool("server_info", {}))
            # Fail loudly if the PATH surgery no-ops: otherwise this leg would
            # silently exercise the rg path a second time instead of the fallback.
            assert fb_info["ripgrep_available"] is False, fb_info
            fb_sets = await _collect_search_sets(fallback)
            assert fb_sets == rg_sets, (fb_sets, rg_sets)
    print("search fallback parity OK")


def _whole_line(line: str) -> bool:
    """True for a complete Race.md line; a torn atomic write would leave a fragment."""
    if line in ("seed", "from-A", "from-B"):
        return True
    return len(line) > 2 and line[:2] in ("A-", "B-") and line[2:].isdigit()


async def _concurrency_leg(vaults: Path) -> None:
    """Two server PROCESSES racing on one note via optimistic concurrency.

    asyncio.gather drives two separate stdio subprocesses (not threads sharing
    one server): the flock + atomic-write + expected_sha256 chain is exercised
    across real process boundaries. Phase A is exactly-one-winner deterministic;
    phase B hammers appends and pins no-torn-writes with a weak-but-stable
    whole-line check (which hammer lines landed depends on scheduling).
    """
    fake_home = vaults / ".fakehome-concurrency"
    (fake_home / ".cache").mkdir(parents=True, exist_ok=True)
    env = {
        "VAULTS_ROOT": str(vaults),
        "PYTHONUNBUFFERED": "1",
        "HOME": str(fake_home),
        "XDG_CACHE_HOME": str(fake_home / ".cache"),
    }
    params_a = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=dict(env))
    params_b = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=dict(env))
    async with stdio_client(params_a) as (read_a, write_a):
        async with ClientSession(read_a, write_a) as sess_a:
            await sess_a.initialize()
            async with stdio_client(params_b) as (read_b, write_b):
                async with ClientSession(read_b, write_b) as sess_b:
                    await sess_b.initialize()
                    seed = out(
                        await sess_a.call_tool(
                            "write_note",
                            {"vault": "Termchat", "path": "Race.md", "content": "seed\n"},
                        )
                    )
                    s0 = seed["sha256"]

                    # Phase A: both write from the same stale sha; one wins.
                    res_a, res_b = await asyncio.gather(
                        sess_a.call_tool(
                            "write_note",
                            {
                                "vault": "Termchat",
                                "path": "Race.md",
                                "content": "from-A\n",
                                "expected_sha256": s0,
                            },
                        ),
                        sess_b.call_tool(
                            "write_note",
                            {
                                "vault": "Termchat",
                                "path": "Race.md",
                                "content": "from-B\n",
                                "expected_sha256": s0,
                            },
                        ),
                    )
                    winners = [r for r in (res_a, res_b) if not r.isError]
                    losers = [r for r in (res_a, res_b) if r.isError]
                    assert len(winners) == 1 and len(losers) == 1, (
                        err_text(res_a),
                        err_text(res_b),
                    )
                    loser_text = err_text(losers[0])
                    assert "note has changed" in loser_text, loser_text
                    assert "Traceback" not in loser_text, loser_text
                    # The winner's sha describes the bytes on disk right now.
                    raw = (vaults / "Termchat" / "Race.md").read_bytes()
                    assert out(winners[0])["sha256"] == hashlib.sha256(raw).hexdigest()

                    # Phase B: hammer appends; losers re-read and retry.
                    async def _hammer(session, tag: str) -> list:
                        shas = []
                        for i in range(10):
                            for _ in range(30):
                                cur = out(
                                    await session.call_tool(
                                        "read_note",
                                        {"vault": "Termchat", "path": "Race.md"},
                                    )
                                )
                                res = await session.call_tool(
                                    "append_note",
                                    {
                                        "vault": "Termchat",
                                        "path": "Race.md",
                                        "text": f"{tag}-{i}\n",
                                        "expected_sha256": cur["sha256"],
                                    },
                                )
                                if not res.isError:
                                    shas.append(out(res)["sha256"])
                                    break
                                assert "note has changed" in err_text(res), err_text(res)
                                assert "Traceback" not in err_text(res), err_text(res)
                            else:
                                raise AssertionError(f"{tag} append starved under contention")
                        return shas

                    hammer_shas = await asyncio.gather(_hammer(sess_a, "A"), _hammer(sess_b, "B"))
                    final_raw = (vaults / "Termchat" / "Race.md").read_bytes()
                    final_sha = hashlib.sha256(final_raw).hexdigest()
                    final_text = final_raw.decode("utf-8")  # torn write = undecodable
                    assert final_text.endswith("\n") and "\x00" not in final_text
                    for line in final_text.splitlines():
                        assert _whole_line(line), repr(line)
                    # No lost appends: every hammer marker from both writers is
                    # present exactly once (order-independent, so no flake).
                    winner_line = "from-A" if winners[0] is res_a else "from-B"
                    expected = sorted(
                        [winner_line]
                        + [f"A-{i}" for i in range(10)]
                        + [f"B-{i}" for i in range(10)]
                    )
                    assert sorted(final_text.splitlines()) == expected, (
                        sorted(final_text.splitlines()),
                        expected,
                    )
                    final_read = out(
                        await sess_a.call_tool(
                            "read_note", {"vault": "Termchat", "path": "Race.md"}
                        )
                    )
                    assert final_read["sha256"] == final_sha, final_read
                    # The final bytes match one sha a process reported: never a
                    # torn intermediate no process ever saw.
                    seen = set(hammer_shas[0]) | set(hammer_shas[1])
                    seen.add(out(winners[0])["sha256"])
                    assert final_sha in seen, final_sha
    print("concurrency OK")


async def _error_log_leg(vaults: Path, temp_dir: str) -> None:
    """The debug log exists by default: a failing tool call must create it.

    _logged records every tool failure at ERROR with a traceback (ValueError
    client errors included), so any isError call through a server whose HOME
    points at a scratch dir must leave fakehome/.cache/vaults-hub/debug.log
    with a traceback. The non-ValueError sanitization wording is pinned by the
    separate vaults.config handler/level probe below.
    """
    fake_home = Path(temp_dir) / "fakehome"
    (fake_home / ".cache").mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        env={
            "VAULTS_ROOT": str(vaults),
            "PYTHONUNBUFFERED": "1",
            "HOME": str(fake_home),
            "XDG_CACHE_HOME": str(fake_home / ".cache"),
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            missing = await session.call_tool(
                "read_note", {"vault": "Termchat", "path": "NoSuch.md"}
            )
            assert missing.isError
    log = fake_home / ".cache" / "vaults-hub" / "debug.log"
    assert log.is_file(), f"debug.log was not created under doctored HOME {fake_home}"
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "Traceback" in text and "failed" in text, text[-1000:]

    # Handler attached unconditionally at WARNING (default level): probed in a
    # child process so the real ~/.cache is never touched by this suite.
    probe_home = Path(temp_dir) / "fakehome-probe"
    probe_home.mkdir(parents=True, exist_ok=True)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging; from vaults import config; "
            "h = [x for x in config._logger.handlers]; "
            "assert h, 'no log handler attached'; "
            "assert config._logger.getEffectiveLevel() == logging.WARNING, "
            "config._logger.getEffectiveLevel(); "
            "assert config._logger.propagate is False, config._logger.propagate",
        ],
        capture_output=True,
        text=True,
        cwd=str(HERE),
        env={
            **os.environ,
            "HOME": str(probe_home),
            "XDG_CACHE_HOME": str(probe_home / ".cache"),
        },
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr[-1000:]
    print("error log OK")


async def _rg_fallback_only() -> int:
    """Standalone entry for the CI ripgrep-absent step (same leg, own root)."""
    with tempfile.TemporaryDirectory(prefix="vaults-hub-norg-") as temp_dir:
        vaults = Path(temp_dir)
        fake_home = Path(temp_dir) / ".fakehome-rgonly"
        (fake_home / ".cache").mkdir(parents=True, exist_ok=True)
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER)],
            env={
                "VAULTS_ROOT": str(vaults),
                "PYTHONUNBUFFERED": "1",
                "HOME": str(fake_home),
                "XDG_CACHE_HOME": str(fake_home / ".cache"),
            },
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await _write_search_corpus(session)
                rg_sets = await _collect_search_sets(session)
        await _search_fallback_leg(vaults, rg_sets, Path(temp_dir) / "norgbin")
    print("SMOKE OK (rg-fallback only)")
    return 0


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
        fake_home_main = Path(temp_dir) / ".smoke-home"
        (fake_home_main / ".cache").mkdir(parents=True, exist_ok=True)
        fake_env = {
            "VAULTS_ROOT": str(vaults),
            "PYTHONUNBUFFERED": "1",
            "HOME": str(fake_home_main),
            "XDG_CACHE_HOME": str(fake_home_main / ".cache"),
        }
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER)],
            env=dict(fake_env),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                print("initialized")
                assert init.serverInfo.name == "vaults", init.serverInfo
                assert init.serverInfo.version == "0.2.0", init.serverInfo

                listed_tools = (await session.list_tools()).tools
                tools = sorted(tool.name for tool in listed_tools)
                assert tools == EXPECTED_TOOLS, tools
                by_name = {tool.name: tool for tool in listed_tools}
                for name in EXPECTED_TOOLS:
                    assert by_name[name].annotations is not None, name
                    assert by_name[name].annotations.title, name
                    assert by_name[name].annotations.openWorldHint is False, name
                    for hint in (
                        by_name[name].annotations.readOnlyHint,
                        by_name[name].annotations.destructiveHint,
                        by_name[name].annotations.idempotentHint,
                        by_name[name].annotations.openWorldHint,
                    ):
                        assert hint is not None, name
                        assert isinstance(hint, bool), name
                for name in READ_ONLY_TOOLS:
                    assert by_name[name].annotations.readOnlyHint is True, name
                    assert by_name[name].annotations.destructiveHint is False, name
                    assert by_name[name].annotations.idempotentHint is True, name
                for name in DESTRUCTIVE_TOOLS:
                    assert by_name[name].annotations.destructiveHint is True, name
                    assert by_name[name].annotations.readOnlyHint is False, name
                    assert by_name[name].annotations.idempotentHint is False, name
                made_ann = by_name["create_vault"].annotations
                assert made_ann.idempotentHint is True, made_ann
                assert made_ann.destructiveHint is False, made_ann
                assert made_ann.readOnlyHint is False, made_ann
                assert made_ann.openWorldHint is False, made_ann
                assert sorted(by_name["delete_vault"].inputSchema["required"]) == [
                    "confirm",
                    "vault",
                ]

                # JSON schema advertises the numeric bounds.
                limit_schema = by_name["list_notes"].inputSchema["properties"]["limit"]
                assert limit_schema.get("minimum") == 1, limit_schema
                assert limit_schema.get("maximum") == 500, limit_schema
                assert by_name["list_notes"].inputSchema["properties"]["offset"].get("minimum") == 0
                search_schema = by_name["search_notes"].inputSchema["properties"]
                assert search_schema["limit"].get("maximum") == 200, search_schema["limit"]
                assert search_schema["before"].get("maximum") == 10, search_schema["before"]
                assert search_schema["after"].get("maximum") == 10, search_schema["after"]
                assert by_name["history"].inputSchema["properties"]["limit"].get("maximum") == 200
                print("tool registry OK")

                listed = out(await session.call_tool("list_vaults", {}))
                assert {item["name"]: item["notes"] for item in listed} == {
                    "Runnix": 1,
                    "Termchat": 3,
                }

                info = out(await session.call_tool("server_info", {}))
                assert set(info) == {
                    "version",
                    "python",
                    "platform",
                    "git_available",
                    "ripgrep_available",
                    "git_enabled",
                    "vaults_root",
                    "vault_count",
                }, info
                assert info["version"] == "0.2.0", info
                assert info["git_enabled"] is True, info
                assert info["vaults_root"] == str(vaults), info
                assert info["vault_count"] == len(listed), info

                made = out(await session.call_tool("create_vault", {"vault": "Newvault"}))
                assert made["created"] is True, made
                assert (vaults / "Newvault").is_dir()
                again = out(await session.call_tool("create_vault", {"vault": "Newvault"}))
                assert again["created"] is False, again
                bad_name = await session.call_tool("create_vault", {"vault": "../evil"})
                assert bad_name.isError, "vault traversal was not rejected"
                relisted = out(await session.call_tool("list_vaults", {}))
                assert "Newvault" in {item["name"] for item in relisted}, relisted

                # delete_vault: whole-tree removal gated on exact confirmation.
                await session.call_tool("create_vault", {"vault": "Delvault"})
                await session.call_tool(
                    "write_note",
                    {"vault": "Delvault", "path": "a.md", "content": "a\n"},
                )
                await session.call_tool(
                    "write_note",
                    {"vault": "Delvault", "path": "b.md", "content": "b\n"},
                )
                removed = out(
                    await session.call_tool(
                        "delete_vault", {"vault": "Delvault", "confirm": "Delvault"}
                    )
                )
                assert removed["deleted"] is True, removed
                assert removed["notes_removed"] == 2, removed
                assert not (vaults / "Delvault").exists()
                fresh_listed = out(await session.call_tool("list_vaults", {}))
                assert "Delvault" not in {item["name"] for item in fresh_listed}

                await session.call_tool("create_vault", {"vault": "Confirmvault"})
                mismatch = await session.call_tool(
                    "delete_vault", {"vault": "Confirmvault", "confirm": "wrong"}
                )
                assert mismatch.isError, "confirm mismatch was not rejected"
                assert "confirm" in err_text(mismatch), err_text(mismatch)
                assert (vaults / "Confirmvault").is_dir()
                confirm_cleanup = out(
                    await session.call_tool(
                        "delete_vault",
                        {"vault": "Confirmvault", "confirm": "Confirmvault"},
                    )
                )
                assert confirm_cleanup["deleted"] is True, confirm_cleanup

                traversal_del = await session.call_tool(
                    "delete_vault", {"vault": "../evil", "confirm": "../evil"}
                )
                assert traversal_del.isError, "vault traversal was not rejected"
                assert not (vaults / "evil").exists()

                no_such = await session.call_tool(
                    "delete_vault",
                    {"vault": "NoSuchVault", "confirm": "NoSuchVault"},
                )
                assert no_such.isError, "missing vault was not rejected"
                assert "unknown vault" in err_text(no_such), err_text(no_such)

                near_miss = await session.call_tool(
                    "delete_vault",
                    {"vault": "Termchatt", "confirm": "Termchatt"},
                )
                assert near_miss.isError, "near-miss vault name was not rejected"
                assert "did you mean" in err_text(near_miss), err_text(near_miss)
                assert (vaults / "Termchat").is_dir(), "near-miss delete touched a real vault"

                outer = Path(tempfile.mkdtemp(prefix="vaults-hub-outer-"))
                (outer / "sentinel.txt").write_text("keep\n", encoding="utf-8")
                os.symlink(outer, vaults / "Aliasvault")
                alias_del = await session.call_tool(
                    "delete_vault",
                    {"vault": "Aliasvault", "confirm": "Aliasvault"},
                )
                assert alias_del.isError, "symlink alias was not rejected"
                assert (vaults / "Aliasvault").is_symlink(), "alias symlink was followed"
                assert (outer / "sentinel.txt").is_file(), "symlink target was harmed"
                (vaults / "Aliasvault").unlink()
                (outer / "sentinel.txt").unlink()
                outer.rmdir()

                # Wiki-link path escape is unresolved (not an existence oracle).
                # Outward note symlink is skipped by list_notes; read_note still rejects.
                confine_outer = Path(tempfile.mkdtemp(prefix="vaults-hub-confine-"))
                (confine_outer / "Outside.md").write_text("secret\n", encoding="utf-8")
                await session.call_tool("create_vault", {"vault": "Confine"})
                (vaults / "Confine" / "subdir").mkdir()
                (vaults / "Confine" / "subdir" / "Note.md").write_text("inside\n", encoding="utf-8")
                await session.call_tool(
                    "write_note",
                    {
                        "vault": "Confine",
                        "path": "Links.md",
                        "content": "[[../Outside.md]] and [[subdir/Note]]\n",
                    },
                )
                os.symlink(confine_outer / "Outside.md", vaults / "Confine" / "evil.md")
                (vaults / "Confine" / "ok.md").write_text("ok\n", encoding="utf-8")
                links_out = out(
                    await session.call_tool("read_note", {"vault": "Confine", "path": "Links.md"})
                )
                assert "subdir/Note" in links_out["links"], links_out
                assert "../Outside.md" in links_out["unresolved_links"], links_out
                assert "../Outside.md" not in links_out["links"], links_out
                listed = out(
                    await session.call_tool("list_notes", {"vault": "Confine", "path": ""})
                )
                assert "evil.md" not in listed["notes"], listed
                assert "ok.md" in listed["notes"], listed
                assert "Links.md" in listed["notes"], listed
                listed_rec = out(
                    await session.call_tool(
                        "list_notes",
                        {"vault": "Confine", "path": "", "recursive": True},
                    )
                )
                assert "evil.md" not in listed_rec["notes"], listed_rec
                assert "ok.md" in listed_rec["notes"], listed_rec
                evil_read = await session.call_tool(
                    "read_note", {"vault": "Confine", "path": "evil.md"}
                )
                assert evil_read.isError, "escaping note symlink must be rejected"
                (vaults / "Confine" / "evil.md").unlink()
                shutil.rmtree(confine_outer)

                recreated = out(await session.call_tool("create_vault", {"vault": "Delvault"}))
                assert recreated["created"] is True, recreated
                relisted_after = out(await session.call_tool("list_vaults", {}))
                assert {item["name"]: item["notes"] for item in relisted_after}["Delvault"] == 0, (
                    relisted_after
                )

                await session.call_tool("create_vault", {"vault": "Gitvault"})
                await session.call_tool(
                    "write_note",
                    {"vault": "Gitvault", "path": "a.md", "content": "hi\n"},
                )
                had_git = (vaults / "Gitvault" / ".git").is_dir()
                git_del = out(
                    await session.call_tool(
                        "delete_vault", {"vault": "Gitvault", "confirm": "Gitvault"}
                    )
                )
                if had_git:
                    assert git_del["git_history"] == "removed", git_del
                    assert not (vaults / "Gitvault" / ".git").exists()
                else:
                    assert git_del["git_history"] == "none", git_del
                await session.call_tool("create_vault", {"vault": "Emptyvault"})
                empty_del = out(
                    await session.call_tool(
                        "delete_vault",
                        {"vault": "Emptyvault", "confirm": "Emptyvault"},
                    )
                )
                assert empty_del["git_history"] == "none", empty_del

                # Unknown vault names get a client-safe error with valid choices.
                unknown = await session.call_tool("read_note", {"vault": "Termcha", "path": "x.md"})
                assert unknown.isError, "unknown vault was not rejected"
                unknown_text = err_text(unknown)
                assert "valid:" in unknown_text, unknown_text
                assert "did you mean" in unknown_text, unknown_text

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
                # sha256 is over RAW FILE BYTES, even for non-UTF-8 notes.
                broken_out = out(broken)
                broken_raw = (vaults / "Termchat" / "Broken.md").read_bytes()
                assert broken_out["sha256"] == hashlib.sha256(broken_raw).hexdigest()
                lossy = broken_raw.decode("utf-8", errors="replace").encode("utf-8")
                assert broken_out["sha256"] != hashlib.sha256(lossy).hexdigest(), (
                    "sha must not be the hash of the lossy-decoded text"
                )

                # CRLF: content is universal-newline normalized, sha is raw bytes.
                (vaults / "Termchat" / "Crlf.md").write_bytes(b"line1\r\nline2\r\n")
                crlf = out(
                    await session.call_tool("read_note", {"vault": "Termchat", "path": "Crlf.md"})
                )
                assert crlf["content"] == "line1\nline2\n", repr(crlf["content"])
                assert "\r" not in crlf["content"]
                assert crlf["sha256"] == hashlib.sha256(b"line1\r\nline2\r\n").hexdigest()
                # CRLF read-then-append round trip: the append's OCC base sha
                # must agree with the read sha (both derive from raw bytes).
                crlf_append_res = await session.call_tool(
                    "append_note",
                    {
                        "vault": "Termchat",
                        "path": "Crlf.md",
                        "text": "line3\n",
                        "expected_sha256": crlf["sha256"],
                    },
                )
                assert not crlf_append_res.isError, err_text(crlf_append_res)
                crlf_appended = out(crlf_append_res)
                assert crlf_appended["previous_sha256"] == crlf["sha256"], crlf_appended
                crlf_after = out(
                    await session.call_tool("read_note", {"vault": "Termchat", "path": "Crlf.md"})
                )
                assert crlf_after["content"] == "line1\nline2\nline3\n", repr(crlf_after["content"])

                # Second Runnix match so truncation can be exercised below.
                await session.call_tool(
                    "write_note",
                    {"vault": "Runnix", "path": "more.md", "content": "Runnix again.\n"},
                )
                hits = out(
                    await session.call_tool("search_notes", {"query": "Runnix", "vault": "Runnix"})
                )
                assert hits["matches"], "expected fixture search hit"
                assert hits["limit"] == 50, hits
                assert hits["truncated"] is False, hits
                plain_hit = hits["matches"][0]
                assert "before_lines" not in plain_hit, plain_hit
                assert "match_line" not in plain_hit, plain_hit
                assert "after_lines" not in plain_hit, plain_hit

                trunc = out(
                    await session.call_tool(
                        "search_notes", {"query": "Runnix", "vault": "Runnix", "limit": 1}
                    )
                )
                assert trunc["limit"] == 1, trunc
                assert len(trunc["matches"]) == 1, trunc
                assert trunc["truncated"] is True, trunc

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

                # read_note sha chaining: read -> write with the fresh sha.
                current = out(
                    await session.call_tool(
                        "read_note", {"vault": "Termchat", "path": "Scratch.md"}
                    )
                )
                chained = out(
                    await session.call_tool(
                        "write_note",
                        {
                            "vault": "Termchat",
                            "path": "Scratch.md",
                            "content": current["content"] + "chained\n",
                            "expected_sha256": current["sha256"],
                        },
                    )
                )
                assert chained["previous_sha256"] == current["sha256"], chained

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

                # NUL bytes are never valid note content.
                nul = await session.call_tool(
                    "write_note",
                    {"vault": "Termchat", "path": "Nul.md", "content": "a\x00b\n"},
                )
                assert nul.isError, "NUL byte content was not rejected"

                # Writes preserve the existing file mode (POSIX only).
                if os.name == "posix":
                    await session.call_tool(
                        "write_note",
                        {"vault": "Termchat", "path": "Mode.md", "content": "v1\n"},
                    )
                    os.chmod(vaults / "Termchat" / "Mode.md", 0o644)
                    await session.call_tool(
                        "write_note",
                        {"vault": "Termchat", "path": "Mode.md", "content": "v2\n"},
                    )
                    mode = os.stat(vaults / "Termchat" / "Mode.md").st_mode & 0o777
                    assert mode == 0o644, oct(mode)

                # append_note creates a missing note.
                await session.call_tool(
                    "append_note",
                    {"vault": "Termchat", "path": "BrandNew.md", "text": "fresh\n"},
                )
                brand_new = out(
                    await session.call_tool(
                        "read_note", {"vault": "Termchat", "path": "BrandNew.md"}
                    )
                )
                assert "fresh" in brand_new["content"], brand_new

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
                assert flat["total"] == len(flat["dirs"]) + len(flat["notes"]), flat
                assert flat["offset"] == 0 and flat["limit"] == 200, flat
                assert flat["truncated"] is False and flat["next_offset"] is None, flat
                rec = out(
                    await session.call_tool(
                        "list_notes",
                        {"vault": "Termchat", "path": "", "recursive": True},
                    )
                )
                assert rec["dirs"] == [], rec
                assert "Sub/inner.md" in rec["notes"], rec

                # list_notes pagination over a dedicated vault.
                await session.call_tool("create_vault", {"vault": "Pagevault"})
                for name in ("a.md", "b.md", "c.md"):
                    await session.call_tool(
                        "write_note",
                        {"vault": "Pagevault", "path": name, "content": f"{name}\n"},
                    )
                page1 = out(
                    await session.call_tool(
                        "list_notes", {"vault": "Pagevault", "path": "", "limit": 2}
                    )
                )
                assert page1["total"] == 3, page1
                assert page1["offset"] == 0 and page1["limit"] == 2, page1
                assert page1["truncated"] is True and page1["next_offset"] == 2, page1
                assert len(page1["notes"]) == 2, page1
                page2 = out(
                    await session.call_tool(
                        "list_notes",
                        {"vault": "Pagevault", "path": "", "offset": 2, "limit": 2},
                    )
                )
                assert page2["truncated"] is False and page2["next_offset"] is None, page2
                assert len(page2["notes"]) == 1, page2
                assert page1["notes"] + page2["notes"] == sorted(["a.md", "b.md", "c.md"]), (
                    page1,
                    page2,
                )
                past_end = await session.call_tool(
                    "list_notes", {"vault": "Pagevault", "path": "", "offset": 250}
                )
                assert past_end.isError, "offset past total was not rejected"
                assert "250" in err_text(past_end), err_text(past_end)

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
                assert ctx["truncated"] is False, ctx
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
                assert list((vaults / "Termchat").glob("*.tmp")) == [], list(
                    (vaults / "Termchat").glob("*.tmp")
                )
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
                assert hist["truncated"] is False, hist
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
                assert hist2["truncated"] is False, hist2
                hist_page = out(
                    await session.call_tool(
                        "history", {"vault": "Termchat", "path": "Vcs.md", "limit": 1}
                    )
                )
                assert len(hist_page["history"]) == 1, hist_page
                assert hist_page["truncated"] is True, hist_page
                fresh_hist = out(
                    await session.call_tool(
                        "history", {"vault": "Termchat", "path": "NeverWritten.md"}
                    )
                )
                assert fresh_hist["history"] == [] and fresh_hist["untracked"] is True, fresh_hist
                assert fresh_hist["truncated"] is False, fresh_hist
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
                assert restored["restored_from"] == first_sha, restored
                assert "previous_sha256" in restored, restored
                bad_rev = await session.call_tool(
                    "restore",
                    {"vault": "Termchat", "path": "Vcs.md", "rev": "deadbeef" * 5},
                )
                assert bad_rev.isError, "bogus rev was not rejected"
                # restore rejects option-injection / pathspec revs client-side.
                for evil_rev in ("--output=pwned:note.md", "--help", "HEAD:other.md"):
                    evil = await session.call_tool(
                        "restore",
                        {"vault": "Termchat", "path": "Vcs.md", "rev": evil_rev},
                    )
                    assert evil.isError, evil_rev
                    evil_text = err_text(evil)
                    assert "invalid rev" in evil_text, evil_text
                    assert "fatal" not in evil_text.lower(), evil_text
                    assert "Traceback" not in evil_text, evil_text
                assert list((vaults / "Termchat").glob("*pwned*")) == []
                assert not (vaults / "Termchat" / "pwned:note.md").exists()
                assert [p for p in vaults.rglob("*pwned*") if ".git" not in p.parts] == []
                # OCC chain works on non-UTF-8 notes: the fresh raw-bytes sha
                # passes the guard, so a bogus rev fails as unknown-revision,
                # never as "note has changed" (the old permanent failure).
                broken_now = out(
                    await session.call_tool("read_note", {"vault": "Termchat", "path": "Broken.md"})
                )
                occ_probe = await session.call_tool(
                    "restore",
                    {
                        "vault": "Termchat",
                        "path": "Broken.md",
                        "rev": first_sha,
                        "expected_sha256": broken_now["sha256"],
                    },
                )
                assert occ_probe.isError, "untracked Broken.md restore should fail"
                occ_text = err_text(occ_probe)
                assert "note has changed" not in occ_text, occ_text
                assert "unknown revision" in occ_text, occ_text
                # history takes no git pathspec magic: a magic-looking path is
                # an (untracked) literal, and leaks no other note's entries.
                magic = await session.call_tool(
                    "history", {"vault": "Termchat", "path": ":(glob)**/*.md"}
                )
                assert not magic.isError, err_text(magic)
                magic_out = out(magic)
                assert magic_out["history"] == [] and magic_out["untracked"] is True, magic_out
                vcs_shas = {entry["sha"] for entry in hist2["history"]}
                assert not vcs_shas & {entry["sha"] for entry in magic_out["history"]}, magic_out
                # Git pathspec: a note literally named `:(glob)*.md` must not
                # sweep other notes into its commit (regression for _pathspec).
                await session.call_tool("create_vault", {"vault": "Specvault"})
                spec_normal = out(
                    await session.call_tool(
                        "write_note",
                        {"vault": "Specvault", "path": "normal.md", "content": "normal v1\n"},
                    )
                )
                assert spec_normal["versioning"] == "ok", spec_normal
                spec_magic = out(
                    await session.call_tool(
                        "write_note",
                        {
                            "vault": "Specvault",
                            "path": ":(glob)*.md",
                            "content": "magic v1\n",
                        },
                    )
                )
                assert spec_magic["versioning"] == "ok", spec_magic
                assert (vaults / "Specvault" / ".git").is_dir()
                (vaults / "Specvault" / "normal.md").write_text(
                    "dirty uncommitted\n", encoding="utf-8"
                )
                spec_magic2 = out(
                    await session.call_tool(
                        "write_note",
                        {
                            "vault": "Specvault",
                            "path": ":(glob)*.md",
                            "content": "magic v2\n",
                        },
                    )
                )
                assert spec_magic2["versioning"] == "ok", spec_magic2
                tip = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(vaults / "Specvault"),
                        "show",
                        "--name-only",
                        "--pretty=format:",
                        "HEAD",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert tip.returncode == 0, tip.stderr[-1000:]
                touched = sorted(line for line in tip.stdout.splitlines() if line.strip())
                assert touched == [":(glob)*.md"], touched
                assert (vaults / "Specvault" / "normal.md").read_text(
                    encoding="utf-8"
                ) == "dirty uncommitted\n"
                dirty = subprocess.run(
                    ["git", "-C", str(vaults / "Specvault"), "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert dirty.returncode == 0, dirty.stderr[-1000:]
                assert "normal.md" in dirty.stdout, dirty.stdout
                print("VCS operations OK")

        # I: VAULTS_HUB_GIT=0 disables versioning (separate server process)
        params_off = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER)],
            env={
                **fake_env,
                "VAULTS_HUB_GIT": "0",
            },
        )
        async with stdio_client(params_off) as (read, write):
            async with ClientSession(read, write) as off:
                await off.initialize()
                off_info = out(await off.call_tool("server_info", {}))
                assert off_info["git_enabled"] is False, off_info
                assert off_info["vaults_root"] == str(vaults), off_info
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

        # Search corpus for the rg/fallback parity leg (same root, own vault).
        params_search = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER)],
            env=dict(fake_env),
        )
        async with stdio_client(params_search) as (read, write):
            async with ClientSession(read, write) as search_session:
                await search_session.initialize()
                await _write_search_corpus(search_session)
                rg_sets = await _collect_search_sets(search_session)
        await _search_fallback_leg(vaults, rg_sets, Path(temp_dir) / "norgbin")

        # Core data-integrity chain under real multiprocess contention. Bounded
        # so CI fails loudly instead of hanging (well under timeout-minutes).
        await asyncio.wait_for(_concurrency_leg(vaults), timeout=120)

        await _error_log_leg(vaults, temp_dir)

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1:] == ["--only=rg-fallback"]:
            raise SystemExit(asyncio.run(_rg_fallback_only()))
        raise SystemExit(f"usage: {sys.argv[0]} [--only=rg-fallback]")
    raise SystemExit(asyncio.run(main()))
