# vaults-hub

Stdio MCP server (Python, FastMCP) exposing Obsidian-style Markdown vaults under `VAULTS_ROOT` (default `~/.vaults`), plus an npm launcher (`npx -y vaults-hub`). This file overrides global agent config on conflict. Be concise, factual, no sycophancy.

## Layout

- `vaults/` is the whole server and the source of truth:
  - `config.py`: vaults root, logging, CLI
  - `notes.py`: path validation, locks, atomic writes, sha256, frontmatter, CRUD
  - `indexes.py`: backlink/tag indexes with mtime invalidation
  - `search.py`: `rg --json` plus a pure-Python fallback
  - `versioning.py`: per-vault local git
  - `server.py`: 14 `@mcp.tool` wrappers on FastMCP's stock stdio transport
- Root `server.py` is a compatibility shim; never put logic there.
- `bin/vaults-hub.mjs` is the npm launcher. It finds Python 3.10+ and bootstraps the pinned deps into a cached venv.
- `smoke_test.py` is the end-to-end suite and `npm_launcher_test.py` is the launcher integration test. There is no test framework, so don't add pytest or fixtures.

## Commands

```bash
source .venv/bin/activate                 # or: python -m venv .venv && pip install -r requirements.txt
python smoke_test.py                      # must exit 0, prints SMOKE OK
python npm_launcher_test.py               # needs Node; prints LAUNCHER OK
uvx ruff@0.15.22 check vaults server.py smoke_test.py
uvx ruff@0.15.22 format --check vaults server.py smoke_test.py
node --check bin/vaults-hub.mjs
```

Ruff isn't installed in `.venv`, so use `uvx` with the CI-pinned version. Run the smoke test and both ruff commands after any change under `vaults/`. If you touch `bin/`, `package.json`, or packaging, also run the launcher test. Never claim a change is done without running these checks.

## Invariants (don't break)

- **Pinned deps.** `requirements.txt` and `pyproject.toml` pin `anyio`, `mcp`, and `PyYAML` to exact versions. Bump them deliberately, in both files, and re-run both suites.
- **Python 3.10+ only, POSIX only.** CI covers 3.10, 3.12, and 3.14. Locking uses `fcntl`, so don't add 3.11+-only syntax or Windows code paths.
- **Writes are atomic.** Write a temp file in the same directory, `fsync` it, `os.replace` it, then `fsync` the parent directory, and preserve existing permissions. Lock order is always the note lock first, then the git lock inside it.
- **Hashing.** `expected_sha256` is computed over the raw bytes on disk, not decoded text.
- **Versioning fails open.** The file write is the source of truth. A git failure surfaces as `commit_error` and never blocks the write. Versioning never pushes, pulls, or fetches.
- **Tool boundary.** Every tool runs its blocking call via `_run_off_loop` (`anyio.to_thread`). Only client-safe `ValueError`s reach the client, and everything else is sanitized; tracebacks go to the log file only.
- **Stdout belongs to JSON-RPC.** Never `print` to stdout from the server. The `vaults` logger writes only to `~/.cache/vaults-hub/debug.log`; don't add stream handlers to it.
- **Search parity.** The `rg` path and the Python fallback must return the same results, and both skip dotfiles and hidden folders (`.obsidian/`, `.trash/`). The backlink/tag indexes skip them too.
- **Symlink confinement.** Reject any symlink target that resolves outside the vault.
- **Packaging.** The npm tarball must not include `smoke_test.py`, `__pycache__`, or dotfiles. `ci.yml` asserts the exact file list, and `release.yml` reuses `ci.yml` as its verify job.

## Adding or changing a tool

1. Add it in `vaults/server.py` with all four `ToolAnnotations` hints set explicitly as booleans. `openWorldHint` is always `False`.
2. Put the logic in the relevant `vaults/*.py` module and keep the wrapper thin.
3. Update `EXPECTED_TOOLS` in `smoke_test.py`, the hard-coded `14` in `npm_launcher_test.py`, and the tool count and tables in `README.md`.
4. Add a smoke-test assertion that covers the new behavior.
5. Add a `CHANGELOG.md` entry.
6. MCP clients cache `tools/list` at initialize, so restart them to see schema changes.

If you add a module under `vaults/`, also add it to the tarball file list in `.github/workflows/ci.yml`.

## Release

1. Bump all three version literals together: `pyproject.toml`, `package.json`, and `vaults/__init__.py` `__version__` (`SERVER_VERSION` and the smoke test derive from these).
2. Add a `## [x.y.z]` section to `CHANGELOG.md`.
3. Commit, then push the tag `v<version>` from the local machine. CI then verifies, publishes to npm via OIDC, and creates the GitHub Release.
4. Agents never create or push tags without explicit approval.

## Git

- Never commit or push without asking. Commit with `git commit -s -S`.
- Use Conventional Commits with the narrowest accurate scope, as in the existing history: `fix(vaults): ...`, `fix(launcher): ...`, `build(packaging): ...`, `test(vaults): ...`, `docs(readme): ...`, `ci: ...`.
- Write an imperative, lowercase subject of 72 characters or fewer. Add a body only when the diff can't explain why.
- Never add agent attribution. No `Co-authored-by:` trailers for any AI, agent, or tool (Cursor, Claude, OpenCode, Codex, etc.), and no "Generated with" or "Made with" lines in commits, PR or issue titles and bodies, or comments. Strip any that a tool inserts automatically.
- Keep diffs small and focused on one concern. Don't flatten intentional design (e.g. `# WHY:` and `# ponytail:` comments explain deliberate trade-offs).

## Code style

- Ruff with line length 100, type hints, and docstrings on public functions.
- Imports that are deferred inside a function carry a `# deferred: avoids X<->Y cycle` comment. Keep that pattern rather than restructuring modules.
- Comments explain *why* (constraints, trade-offs), never what the next line does.
- When behavior changes, update `README.md` and `CONTRIBUTING.md` in the same change.
