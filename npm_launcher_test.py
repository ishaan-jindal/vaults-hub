"""Integration test for the npm launcher (bin/vaults-hub.mjs).

Same no-framework, exit-nonzero-on-failure style as smoke_test.py: plain
asserts, traceback on failure, zero on success. Needs Node; skips gracefully
when `npm` is unavailable so a contributor without Node can still run the
Python suite.
"""

import ast
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from mcp.types import LATEST_PROTOCOL_VERSION

HERE = Path(__file__).resolve().parent

HINT_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def expected_version() -> str:
    return json.loads((HERE / "package.json").read_text(encoding="utf-8"))["version"]


def expected_tool_names() -> list[str]:
    """Read the tool list from smoke_test.py so a new tool cannot silently drift."""
    tree = ast.parse((HERE / "smoke_test.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "EXPECTED_TOOLS" for t in node.targets
        ):
            return sorted(ast.literal_eval(node.value))
    raise AssertionError("EXPECTED_TOOLS not found in smoke_test.py")


def parse_requirements_txt(path: Path) -> dict[str, str]:
    """Parse only real `name==version` pins; ignore blanks, comments, non-== lines."""
    pins: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, rest = line.partition("==")
        version = re.split(r"[\s;#]", rest.strip(), maxsplit=1)[0]
        if name.strip() and version:
            pins[name.strip()] = version
    return pins


def generated_probe(launcher_path: Path, requirements_path: Path) -> str:
    """Execute the launcher's real probe builder against the given files.

    Copies both into a temp package layout (bin/vaults-hub.mjs plus
    requirements.txt) so the launcher's import.meta.url self-location keeps
    working, neutralizes the entrypoint to print the probe instead of
    starting the server, and runs it.
    """
    src = launcher_path.read_text(encoding="utf-8")
    assert "function depsProbe()" in src, f"{launcher_path} must expose depsProbe()"
    assert src.count("main();") == 1, f"{launcher_path} entrypoint changed; update the guard"
    harness = src.replace("main();", "console.log(depsProbe());")
    with tempfile.TemporaryDirectory(prefix="vaults-hub-probe-") as pkg:
        pkg_path = Path(pkg)
        (pkg_path / "bin").mkdir()
        (pkg_path / "bin" / "vaults-hub.mjs").write_text(harness, encoding="utf-8")
        shutil.copy(requirements_path, pkg_path / "requirements.txt")
        proc = subprocess.run(
            ["node", str(pkg_path / "bin" / "vaults-hub.mjs")],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert proc.returncode == 0, f"probe builder failed: {proc.stderr.strip()}"
    probe = proc.stdout.strip()
    assert probe, "probe builder printed nothing"
    # The probe must be valid Python in its own right (compound `if`
    # statements cannot follow `;` in `python -c`; compile catches that
    # without needing the dependencies installed).
    compile_proc = subprocess.run(
        [sys.executable, "-c", "import sys; compile(sys.stdin.read(), '<probe>', 'exec')"],
        input=probe,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert compile_proc.returncode == 0, (
        f"generated probe does not compile: {compile_proc.stderr.strip()}"
    )
    return probe


def check_launcher_probe_pins() -> None:
    """The launcher's real probe must check every requirements.txt pin.

    Explicit raises only: `assert` would be stripped under python -O.
    """
    requirements_path = HERE / "requirements.txt"
    wanted = parse_requirements_txt(requirements_path)
    assert wanted, f"no name==version pins parsed from {requirements_path}"
    probe = generated_probe(HERE / "bin" / "vaults-hub.mjs", requirements_path)
    assert "assert" not in probe, f"generated probe must not rely on assert: {probe!r}"
    assert "\nimport " in probe, f"generated probe must import the modules: {probe!r}"
    for name, version in wanted.items():
        assert name in probe, f"generated probe never checks {name!r}: {probe!r}"
        assert version in probe, (
            f"generated probe never checks version {version!r} for {name!r}: {probe!r}"
        )
    pins = ", ".join(f"{k}=={v}" for k, v in sorted(wanted.items()))
    print(f"launcher probe pins OK ({pins})")


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
    check_launcher_probe_pins()

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
                                "protocolVersion": LATEST_PROTOCOL_VERSION,
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
