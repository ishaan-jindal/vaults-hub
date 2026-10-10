#!/usr/bin/env node
// vaults-hub launcher: thin Node wrapper around the proven Python MCP server.
//
// `npx -y vaults-hub` lands here. This script finds a Python >= 3.10, makes
// sure the server's pinned dependencies are importable (bootstrapping them
// into a cached venv on first run), then exec-style spawns the real
// server.py with stdio inherited so the MCP client talks to Python directly.
//
// Node builtins only — zero npm dependencies. ESM, Node >= 18.
//
// Critical invariant: NEVER write to stdout. The MCP client owns the
// child's stdin/stdout as a JSON-RPC pipe; even one stray byte on our
// stdout corrupts the session. All diagnostics go to stderr.

import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Package layout: bin/vaults-hub.mjs sits next to ../server.py,
// ../requirements.txt and ../vaults/. Resolving via import.meta.url keeps
// this working from a global install, an npx cache dir, or a local checkout.
const HERE = dirname(fileURLToPath(import.meta.url));
const PACKAGE_ROOT = dirname(HERE);
const SERVER_PY = join(PACKAGE_ROOT, "server.py");
const REQUIREMENTS_TXT = join(PACKAGE_ROOT, "requirements.txt");

const MIN_PYTHON = [3, 10];
// Distribution -> importable module for the pinned requirements. Versions
// are NOT pinned here: requirements.txt is the single source of truth
// (parsed below), so bumping a pin there flows into the probe on both the
// fast path and the post-install verify. This map only records names, and
// stays correct for future pins via the normalized fallback in buildProbe.
const IMPORT_BY_DIST = { PyYAML: "yaml", anyio: "anyio", mcp: "mcp" };

// Parse only real `name==version` pins from requirements.txt; ignore blank
// lines, comments, and anything without `==`. Distribution names are kept
// verbatim: importlib.metadata normalizes them (PyYAML works as-is).
function parseRequirementPins() {
  let text;
  try {
    text = readFileSync(REQUIREMENTS_TXT, "utf8");
  } catch (e) {
    err(`cannot read requirements.txt at ${REQUIREMENTS_TXT}: ${e.message}`);
    process.exit(1);
  }
  const pins = [];
  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("==");
    if (eq < 0) continue;
    const name = line.slice(0, eq).trim();
    const version = line.slice(eq + 2).trim().split(/[\s;#]/)[0];
    if (!name || !version) continue;
    pins.push([name, version]);
  }
  return pins;
}

// Build the `python -c` probe from parsed pins. Explicit `raise SystemExit`
// checks (never `assert`) so `python -O` cannot strip the guard; the
// trailing import proves the modules actually import, not just their
// metadata. Statements are newline-joined: `if` is a compound statement and
// cannot follow `;` in a `python -c` one-liner.
function buildProbe(pins) {
  const lines = ["import importlib.metadata as _m"];
  for (const [name, version] of pins) {
    lines.push(
      `if _m.version('${name}')!='${version}': raise SystemExit('${name}==${version} required')`
    );
  }
  const imports = pins.map(
    ([name]) => IMPORT_BY_DIST[name] ?? name.toLowerCase().replace(/-/g, "_")
  );
  lines.push(`import ${imports.join(", ")}`);
  return lines.join("\n");
}

function depsProbe() {
  return buildProbe(parseRequirementPins());
}

function err(msg) {
  process.stderr.write(`vaults-hub: ${msg}\n`);
}

// Run a command synchronously, capturing output (never inheriting stdio:
// nothing here may touch stdout).
function runCapture(cmd, args) {
  try {
    const res = spawnSync(cmd, args, { encoding: "utf8" });
    return {
      ok: res.status === 0,
      status: res.status,
      stdout: res.stdout ?? "",
      stderr: res.stderr ?? "",
      spawnError: res.error ?? null,
    };
  } catch (e) {
    return { ok: false, status: null, stdout: "", stderr: "", spawnError: e };
  }
}

// Resolve the Python interpreter: VAULTS_HUB_PYTHON wins when set,
// otherwise the first usable `python3` / `python` on PATH. Each candidate
// must exist AND report >= 3.10.
function resolvePython() {
  const override = process.env.VAULTS_HUB_PYTHON;
  const candidates = override ? [override] : ["python3", "python"];
  const tried = [];
  for (const cmd of candidates) {
    const probe = runCapture(cmd, [
      "-c",
      "import sys; print(sys.version_info[0]); print(sys.version_info[1])",
    ]);
    if (probe.spawnError || !probe.ok) {
      tried.push(cmd);
      continue;
    }
    const parts = probe.stdout.trim().split(/\s+/).map(Number);
    if (parts.length < 2 || parts.some(Number.isNaN)) {
      tried.push(cmd);
      continue;
    }
    const [major, minor] = parts;
    if (major > MIN_PYTHON[0] || (major === MIN_PYTHON[0] && minor >= MIN_PYTHON[1])) {
      return { cmd, major, minor };
    }
    err(`ignoring ${cmd} (Python ${major}.${minor}, need >= 3.10)`);
    tried.push(`${cmd} (${major}.${minor})`);
  }
  err(
    `no usable Python found (tried: ${tried.join(", ") || "none"}). ` +
      `Install Python 3.10+ and make sure it is on PATH, or set VAULTS_HUB_PYTHON to its path.`
  );
  process.exit(1);
}

function probeDeps(pythonCmd) {
  return runCapture(pythonCmd, ["-c", depsProbe()]);
}

function depsImportable(pythonCmd) {
  return probeDeps(pythonCmd).ok;
}

// Cache root for bootstrapped venvs. XDG_CACHE_HOME wins when set,
// otherwise ~/.cache. Keyed per interpreter minor version below so a
// 3.11-built venv is never reused by 3.14.
function cacheRoot() {
  if (process.env.XDG_CACHE_HOME) return join(process.env.XDG_CACHE_HOME, "vaults-hub");
  return join(homedir(), ".cache", "vaults-hub");
}

function sha256File(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

// Ensure the pinned deps are importable, bootstrapping a cached venv only
// when the system interpreter lacks them. Returns the interpreter to spawn.
function ensureInterpreter(py) {
  // Fast path: system interpreter already has everything. Zero side effects.
  // On mismatch, surface the probe's own diagnostic (which pin differed)
  // before the generic bootstrap message below.
  const fast = probeDeps(py.cmd);
  if (fast.ok) return py.cmd;
  const fastDetail = (fast.stderr || fast.stdout).trim().split("\n").pop().trim();
  if (fastDetail) err(fastDetail);

  // Bootstrap path: reuse or rebuild a cached venv for this minor version.
  const root = cacheRoot();
  const venvDir = join(root, `venv-py${py.major}.${py.minor}`);
  const venvPy = join(venvDir, "bin", "python");
  // Marker sibling of the venv dir holding the hex sha256 of the
  // requirements.txt this venv was built from; a stale marker forces rebuild.
  const markerPath = join(root, `requirements-py${py.major}.${py.minor}.sha256`);
  let wanted = null;
  try {
    wanted = sha256File(REQUIREMENTS_TXT);
  } catch (e) {
    err(`cannot read requirements.txt at ${REQUIREMENTS_TXT}: ${e.message}`);
    process.exit(1);
  }

  let marker = null;
  try {
    marker = readFileSync(markerPath, "utf8").trim();
  } catch {
    marker = null;
  }

  // Silent reuse: venv exists, was built from these exact requirements,
  // and still imports the deps.
  if (marker === wanted && existsSync(venvPy) && depsImportable(venvPy)) return venvPy;

  // Rebuild. Every message goes to stderr; the first run downloads and
  // installs the pins (~15s), so say plainly it is a one-time cost.
  // Built in a per-process dir and renamed into place, so concurrent first
  // launches never share a half-built venv.
  const buildDir = `${venvDir}.tmp-${process.pid}`;
  const buildPy = join(buildDir, "bin", "python");
  err(`dependencies missing for ${py.cmd}; setting up a cached venv (one-time, ~15s)...`);
  err(`creating venv at ${venvDir}`);
  try {
    mkdirSync(root, { recursive: true });
    if (existsSync(venvDir)) rmSync(venvDir, { recursive: true, force: true });
    rmSync(buildDir, { recursive: true, force: true });
  } catch (e) {
    err(`cannot prepare venv directory ${venvDir}: ${e.message}`);
    process.exit(1);
  }
  const venvRes = runCapture(py.cmd, ["-m", "venv", buildDir]);
  if (!venvRes.ok) {
    err(`failed to create venv at ${buildDir}.`);
    if (venvRes.spawnError) err(String(venvRes.spawnError.message ?? venvRes.spawnError));
    if (venvRes.stderr.trim()) err(venvRes.stderr.trim());
    rmSync(buildDir, { recursive: true, force: true });
    process.exit(1);
  }
  err(`installing pinned dependencies from requirements.txt (one-time cost; reused afterwards)...`);
  const pipRes = runCapture(buildPy, [
    "-m",
    "pip",
    "install",
    "--quiet",
    "--disable-pip-version-check",
    "-r",
    REQUIREMENTS_TXT,
  ]);
  if (!pipRes.ok) {
    err(`pip install failed for the cached venv.`);
    if (pipRes.stderr.trim()) err(pipRes.stderr.trim());
    if (pipRes.stdout.trim()) err(pipRes.stdout.trim());
    if (pipRes.spawnError) err(String(pipRes.spawnError.message ?? pipRes.spawnError));
    rmSync(buildDir, { recursive: true, force: true });
    process.exit(1);
  }
  try {
    renameSync(buildDir, venvDir);
  } catch {
    // Another launcher renamed its venv into place first; the probe below vets it.
    rmSync(buildDir, { recursive: true, force: true });
  }
  const verify = runCapture(venvPy, ["-c", depsProbe()]);
  if (!verify.ok) {
    err(`venv python at ${venvPy} still cannot import the dependencies.`);
    const detail = (verify.stderr || verify.stdout).trim();
    if (detail) err(detail);
    else err("no further error detail from the interpreter probe.");
    if (verify.spawnError) err(String(verify.spawnError.message ?? verify.spawnError));
    process.exit(1);
  }
  try {
    writeFileSync(markerPath, wanted + "\n", "utf8");
  } catch (e) {
    err(`warning: could not write marker ${markerPath}: ${e.message}`);
  }
  err(`dependencies ready; starting server.`);
  return venvPy;
}

function main() {
  const py = resolvePython();
  const interpreter = ensureInterpreter(py);

  // Forward every CLI arg (e.g. --vaults-root /path). Env vars
  // (VAULTS_ROOT, VAULTS_HUB_GIT, PYTHONUNBUFFERED, ...) pass through
  // untouched via the inherited environment.
  const args = [SERVER_PY, ...process.argv.slice(2)];
  let child;
  try {
    // "inherit" is the point: the MCP client must reach Python's
    // stdin/stdout directly, byte for byte.
    child = spawn(interpreter, args, { stdio: "inherit" });
  } catch (e) {
    err(`failed to start server: ${e.message}`);
    process.exit(1);
  }
  child.on("error", (e) => {
    err(`failed to start server: ${e.message}`);
    process.exit(1);
  });
  // Relay signals so Ctrl-C / client teardown stops Python, not just us.
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => {
      try {
        child.kill(sig);
      } catch {
        // Child already gone; its exit handler below finishes the job.
      }
    });
  }
  child.on("exit", (code, signal) => {
    // Mirror the child's fate: its exit code, or 1 when killed by a signal.
    process.exit(signal ? 1 : (code ?? 1));
  });
}

main();
