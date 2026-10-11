"""Integration test for the npm launcher (bin/vaults-hub.mjs).

Same no-framework style as smoke_test.py: plain asserts, traceback on failure,
zero on success. Packs and installs the tarball, then drives the installed bin
over stdio with the MCP client. Skips when Node is unavailable so a contributor
without it can still run the Python suite.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from smoke_test import EXPECTED_TOOLS, VERSION

HERE = Path(__file__).resolve().parent


async def drive(launcher: Path, vaults_root: Path, errlog) -> None:
    """Initialize, list tools, and call one tool through the installed bin."""
    bad_stdout: list[Exception] = []

    async def on_message(message) -> None:
        if isinstance(message, Exception):
            bad_stdout.append(message)

    params = StdioServerParameters(
        command=str(launcher),
        env={
            **os.environ,
            # Fast path: this interpreter already has the pinned deps, so the
            # launcher must not touch the network or bootstrap a venv.
            "VAULTS_HUB_PYTHON": sys.executable,
            "VAULTS_ROOT": str(vaults_root),
            "PYTHONUNBUFFERED": "1",
        },
    )
    async with stdio_client(params, errlog=errlog) as (read, write):
        async with ClientSession(read, write, message_handler=on_message) as session:
            init = await session.initialize()
            assert init.serverInfo.name == "vaults", init.serverInfo
            assert init.serverInfo.version == VERSION, init.serverInfo
            tools = sorted(t.name for t in (await session.list_tools()).tools)
            assert tools == EXPECTED_TOOLS, tools
            res = await session.call_tool("list_vaults", {})
            assert not res.isError, res
    # The MCP pipe owns stdout: any non-JSON-RPC line, such as a launcher
    # diagnostic, reaches the client as a parse error.
    assert not bad_stdout, bad_stdout


def main() -> int:
    npm = shutil.which("npm")
    if npm is None or shutil.which("node") is None:
        print("SKIP: npm/node not found on PATH; install Node to run the launcher test")
        return 0
    with tempfile.TemporaryDirectory(prefix="vaults-hub-launcher-") as tmp:
        tmp_path = Path(tmp)
        pack = subprocess.run(
            [npm, "pack", "--pack-destination", tmp],
            cwd=HERE,
            capture_output=True,
            text=True,
            check=False,
        )
        assert pack.returncode == 0, pack.stderr or pack.stdout
        tarball = tmp_path / pack.stdout.strip().splitlines()[-1].strip()
        assert tarball.is_file(), pack.stdout

        install_dir = tmp_path / "install"
        install_dir.mkdir()
        inst = subprocess.run(
            [npm, "install", str(tarball)],
            cwd=install_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        assert inst.returncode == 0, inst.stderr or inst.stdout
        launcher = install_dir / "node_modules" / ".bin" / "vaults-hub"
        assert launcher.exists(), f"missing launcher shim: {launcher}"

        with tempfile.TemporaryFile("w+") as errlog:
            asyncio.run(drive(launcher, tmp_path / "vaults", errlog))
            errlog.seek(0)
            stderr = errlog.read()
        # Log text may mention jsonrpc; a quoted key means a protocol frame leaked.
        assert '"jsonrpc"' not in stderr, stderr
    print("LAUNCHER OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
