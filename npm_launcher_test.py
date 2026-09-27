"""Integration test for the npm launcher (bin/vaults-hub.mjs).

Same no-framework, exit-nonzero-on-failure style as smoke_test.py: plain
asserts, traceback on failure, zero on success. Needs Node; skips gracefully
when `npm` is unavailable so a contributor without Node can still run the
Python suite.
"""

import ast
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent

HINT_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def expected_version() -> str:
    return json.loads((HERE / "package.json").read_text(encoding="utf-8"))["version"]


def expected_tool_names() -> list[str]:
    """Read the tool list from the repo so a new tool cannot silently drift."""
    try:
        tree = ast.parse((HERE / "smoke_test.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EXPECTED_TOOLS" for t in node.targets
            ):
                names = ast.literal_eval(node.value)
                assert (
                    isinstance(names, list) and names and all(isinstance(n, str) for n in names)
                ), names
                return sorted(names)
    except (OSError, SyntaxError, ValueError, AssertionError):
        pass
    # Fallback: count the @mcp.tool wrappers in the server source.
    names = []
    for line in (HERE / "vaults" / "server.py").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("async def ") or stripped.startswith("def "):
            name = stripped.split()[1].split("(")[0].rstrip(":")
            if name not in ("main", "_collect_server_info", "_serve_stdio"):
                names.append(name)
    # Keep only plausible tool names (called out explicitly in smoke_test.py).
    assert len(names) >= 14, names
    return sorted(names)


def protocol_version() -> str:
    try:
        import mcp.types as mcp_types  # type: ignore

        return str(mcp_types.LATEST_PROTOCOL_VERSION)
    except Exception:
        return "2025-06-18"


def send(proc: subprocess.Popen, obj: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(proc: subprocess.Popen, stdout_lines: list[str], timeout: float = 30.0) -> dict:
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    assert ready, "timed out waiting for a JSON-RPC response from the server"
    line = proc.stdout.readline()
    assert line, "server closed stdout while a response was pending"
    stdout_lines.append(line)
    msg = json.loads(line)
    assert msg.get("jsonrpc") == "2.0", msg
    return msg


def main() -> int:
    npm = shutil.which("npm")
    if npm is None:
        print("SKIP: npm not found on PATH; install Node to run the launcher test")
        return 0
    if shutil.which("node") is None:
        print("SKIP: node not found on PATH; install Node to run the launcher test")
        return 0

    want_version = expected_version()
    want_tools = expected_tool_names()
    print(f"expecting version {want_version} with {len(want_tools)} tools")

    with tempfile.TemporaryDirectory(prefix="vaults-hub-pack-") as pack_dir:
        pack = subprocess.run(
            [npm, "pack", "--pack-destination", pack_dir],
            cwd=HERE,
            capture_output=True,
            text=True,
            check=False,
        )
        assert pack.returncode == 0, pack.stderr or pack.stdout
        tarball_name = pack.stdout.strip().splitlines()[-1].strip()
        tarball = Path(pack_dir) / tarball_name
        assert tarball.is_file(), pack.stdout

        with tempfile.TemporaryDirectory(prefix="vaults-hub-install-") as install_dir:
            inst = subprocess.run(
                [npm, "install", str(tarball)],
                cwd=install_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            assert inst.returncode == 0, inst.stderr or inst.stdout
            launcher = Path(install_dir) / "node_modules" / ".bin" / "vaults-hub"
            assert launcher.exists(), f"missing launcher shim: {launcher}"

            with tempfile.TemporaryDirectory(prefix="vaults-hub-launcher-") as vaults_root:
                env = dict(os.environ)
                # Fast path: the running interpreter already has the pinned deps,
                # so the launcher must not touch the network or bootstrap a venv.
                env["VAULTS_HUB_PYTHON"] = sys.executable
                env["VAULTS_ROOT"] = vaults_root
                env["PYTHONUNBUFFERED"] = "1"

                proc = subprocess.Popen(
                    [str(launcher)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    env=env,
                )
                assert proc.stdin is not None and proc.stdout is not None
                assert proc.stderr is not None
                stderr_lines: list[str] = []

                def drain_stderr() -> None:
                    assert proc.stderr is not None
                    for err_line in proc.stderr:
                        stderr_lines.append(err_line)

                stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
                stderr_thread.start()
                stdout_lines: list[str] = []
                try:
                    req_id = 1
                    send(
                        proc,
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": protocol_version(),
                                "capabilities": {},
                                "clientInfo": {"name": "launcher-test", "version": "0.0.0"},
                            },
                        },
                    )
                    init = recv(proc, stdout_lines)
                    assert init.get("id") == req_id, init
                    assert "error" not in init, init
                    server_info = init["result"]["serverInfo"]
                    assert server_info["name"] == "vaults", server_info
                    assert server_info["version"] == want_version, server_info
                    print(f"initialized: vaults {server_info['version']}")

                    send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

                    req_id += 1
                    send(proc, {"jsonrpc": "2.0", "id": req_id, "method": "tools/list"})
                    listed = recv(proc, stdout_lines)
                    assert listed.get("id") == req_id, listed
                    assert "error" not in listed, listed
                    tools = listed["result"]["tools"]
                    assert sorted(t["name"] for t in tools) == want_tools, sorted(
                        t["name"] for t in tools
                    )
                    assert len(tools) == len(want_tools) == 14, [t["name"] for t in tools]
                    for tool in tools:
                        annotations = tool.get("annotations")
                        assert annotations is not None, tool["name"]
                        for key in HINT_KEYS:
                            assert key in annotations, (tool["name"], annotations)
                            assert isinstance(annotations[key], bool), (
                                tool["name"],
                                key,
                                annotations[key],
                            )
                    print("tool annotations OK")

                    req_id += 1
                    send(
                        proc,
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "method": "tools/call",
                            "params": {"name": "list_vaults", "arguments": {}},
                        },
                    )
                    called = recv(proc, stdout_lines)
                    assert called.get("id") == req_id, called
                    assert "error" not in called, called
                    result = called["result"]
                    assert not result.get("isError", False), result
                    print("tools/call list_vaults OK")
                finally:
                    try:
                        if proc.stdin is not None:
                            proc.stdin.close()
                    except BrokenPipeError:
                        pass
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=15)
                    stderr_thread.join(timeout=10)

                # The MCP pipe owns stdout: every line must be JSON-RPC. The
                # launcher's own diagnostics (if any) belong on stderr.
                assert stdout_lines, "server produced no stdout"
                for line in stdout_lines:
                    assert line.strip(), "blank line on stdout corrupts the JSON-RPC pipe"
                    assert "vaults-hub:" not in line, line
                    msg = json.loads(line)
                    assert msg.get("jsonrpc") == "2.0", msg
                for err_line in stderr_lines:
                    # Match the quoted JSON key, not the bare word: a
                    # human-readable log line may legitimately say "jsonrpc",
                    # but an actual protocol frame must never land on stderr.
                    assert '"jsonrpc"' not in err_line, f"JSON-RPC on stderr: {err_line!r}"

    print("LAUNCHER OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
