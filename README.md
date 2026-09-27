# vaults-hub

[![M8ven Score](https://m8ven.ai/badge/mcp/ishaan-jindal/vaults-hub)](https://m8ven.ai/mcp/ishaan-jindal/vaults-hub)

A Python [MCP](https://modelcontextprotocol.io) server that exposes your
[Obsidian](https://obsidian.md)-style Markdown vaults to MCP clients (e.g. OpenCode).

Keep each project's docs as plain Markdown on disk, and let an agent
read and maintain them through a small, predictable tool API — no Obsidian
plugins, windows, ports, or API keys. Every change is auto-versioned with
local git, so nothing is ever truly lost.

## Quickstart

The fastest way — no clone, no venv:

```bash
npx -y vaults-hub
```

This needs Python 3.10+ on `PATH` (or `VAULTS_HUB_PYTHON` set to its
path). On first run it bootstraps the pinned dependencies from
`requirements.txt` into a cached venv (one-time, ~30s; later runs start
instantly); see [Configuration](#configuration) for the cache location.

From source (offline/dev alternative) — prereqs are Python 3.10+
(developed/tested on 3.14), a POSIX platform (Linux/macOS — locking uses
`fcntl`), and optionally [ripgrep](https://github.com/BurntSushi/ripgrep)
(`rg`) for faster search (a pure-Python fallback is used when `rg` is
absent):

```bash
git clone https://github.com/ishaan-jindal/vaults-hub.git vaults-hub
cd vaults-hub
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py                        # stdio MCP server (no HTTP port)
```

Vaults live in `~/.vaults` by default (each subdir is one vault, e.g.
`~/.vaults/MyProject/`), auto-created on startup when missing. To use a
different location:

```bash
export VAULTS_ROOT="$HOME/my-notes"
python server.py --vaults-root "$HOME/my-notes"   # flag wins over env var
```

### Register with OpenCode

Add to your `opencode.jsonc` (adjust paths to your machine — do **not**
copy any hardcoded home directory). The npx form is recommended:

```jsonc
{
  "mcp": {
    "vaults": {
      "type": "local",
      "command": "npx",
      "args": ["-y", "vaults-hub"],
      // Pick ONE way to point at your vaults (flag wins over env var):
      "environment": {
        "VAULTS_ROOT": "<path-to-your-vaults>"
      }
    }
  }
}
```

The local-venv form is the offline/dev alternative (replace with the
actual paths on your machine):

```jsonc
{
  "mcp": {
    "vaults": {
      "type": "local",
      // Replace with the actual paths on your machine:
      "command": ["<path-to-vaults-hub>/.venv/bin/python", "<path-to-vaults-hub>/server.py"],
      // Pick ONE way to point at your vaults (flag wins over env var):
      "args": ["--vaults-root", "<path-to-your-vaults>"],
      "environment": {
        // ...or via env var instead of "args" above:
        "VAULTS_ROOT": "<path-to-your-vaults>"
      }
    }
  }
}
```

## Usage examples

Vault layout: each project is a directory directly under `VAULTS_ROOT`
containing `.md` files, e.g. `<VAULTS_ROOT>/MyProject/MyProject.md`.

With the server registered as `vaults`:

- List vaults: call `list_vaults` → `[{"name": "MyProject", "path": "...", "notes": 12}]`
- Read a note: `read_note(vault="MyProject", path="MyProject.md")` → frontmatter, content, wiki-links, backlinks
- Write safely: `read_note` → edit → `write_note(..., expected_sha256="<sha from read>")` to avoid clobbering concurrent edits
- Search: `search_notes(query="build command", vault="MyProject", before=1, after=1)`
- Recover a note: `history(vault="MyProject", path="Notes.md")` → pick a `sha` → `restore(vault="MyProject", path="Notes.md", rev="<sha>")`

## Tools

14 tools (see `vaults/server.py` for exact descriptions). Read-only tools are
marked **R**, mutating tools **M** (every note mutation honors `expected_sha256`
for optimistic concurrency — read first, then pass the sha back; `delete_vault`
is gated on `confirm` instead):

| Tool | R/M | What it does |
| ---- | --- | ------------ |
| `list_vaults` | R | List all project vaults with note counts |
| `server_info` | R | Server version, Python/platform, git + ripgrep availability, `git_enabled`, vaults root, vault count |
| `create_vault` | M | Create a new vault (`created=False` when it already exists; idempotent) |
| `delete_vault` | M | Delete a whole vault including git history (irreversible; `confirm` must equal the vault name) |
| `list_notes` | R | List dirs/notes under a vault path (`recursive=True` for flat listing); paginated with `offset`/`limit` (default 200, max 500) → `total`, `truncated`, `next_offset` |
| `read_note` | R | Read a note: frontmatter, content, wiki-links, backlinks, unresolved links |
| `write_note` | M | Create or atomically overwrite a note (`expected_sha256` for optimistic concurrency) |
| `append_note` | M | Append text to a note (created if missing), atomic write, optional `expected_sha256` |
| `delete_note` | M | Delete a note (`missing_ok=True` to tolerate absence); cleans up sibling tmp files (lock files are left in place) |
| `move_note` | M | Move/rename a note (copy+delete, not atomic); errors if src missing or dst exists; honors `expected_sha256`; refreshes `updated:` |
| `list_tags` | R | Tag counts per vault from cached frontmatter (one vault or all) |
| `search_notes` | R | Full-text search via ripgrep (or Python fallback) across one vault or all, with before/after context lines; `limit` 1–200 with a `truncated` flag |
| `history` | R | Version history for a note (`[{sha, date, message}]`) or whole vault from the local git repo, with a `truncated` flag |
| `restore` | M | Restore a note from a past revision (snapshots dirty state first; recreates deleted notes); echoes the revision as `restored_from` |

Also built in: YAML-frontmatter parsing with auto-refreshed `updated:` dates,
file locking (`fcntl`), atomic writes via temp-file + rename, backlink/tag
indexes with mtime invalidation, and optional debug logging to file.

### Tool annotations

Every tool declares all four MCP `ToolAnnotations` hints explicitly, so
hosts that gate on them (OpenAI's tool directory rejects a tool where any
hint is missing or non-boolean) accept the whole set:

| Hint | Meaning here | Value |
| ---- | ------------- | ----- |
| `readOnlyHint` | does not modify the vault | `true` for the 7 read tools, `false` for the rest |
| `destructiveHint` | may destroy or overwrite existing data | `true` for `write_note`, `append_note`, `move_note`, `delete_note`, `delete_vault`, `restore`; `false` elsewhere |
| `idempotentHint` | repeat calls add no further effect | `true` for the 7 read tools and `create_vault`; `false` otherwise |
| `openWorldHint` | touches entities outside the vaults root | `false` for every tool — this server is closed-world by construction |

The hints are hints, not guarantees: a client must not make trust decisions
from them alone. They matter because the stdio writer serializes tool
payloads with `exclude_none=True`, so an unset hint is dropped from
`tools/list` entirely rather than sent as `null`. `smoke_test.py` asserts
all four are present booleans on all 14 tools, so a new tool cannot
silently reintroduce the gap.

## Reliability

How the server avoids losing or corrupting notes:

- **Atomic writes.** Every note write goes to a temp file in the same
  directory (`fsync`ed, then `os.replace`), with the parent directory
  `fsync`ed afterwards — readers never see a half-written note. Existing
  file permissions are preserved.
- **Locking.** Mutations serialize per note (`flock` read-modify-write
  cycles across processes) and git operations serialize per vault, with a
  consistent lock order everywhere (note lock first, git lock inside it),
  so concurrent writes and restores cannot deadlock. Read-only `history`
  takes no locks and never blocks writers; truncation flags are computed
  by the core library, not the tool boundary.
- **Optimistic concurrency.** Every mutation (`write`/`append`/`move`/
  `delete`/`restore`) accepts `expected_sha256`; a stale sha is rejected
  instead of clobbering someone else's edit. `delete_vault` is instead gated
  on `confirm` exactly equaling the vault name.
- **Fail-open versioning.** The write itself is the source of truth — a
  missing git binary or failed commit warns (surfaced as `commit_error`)
  but never blocks the write.
- **Search parity.** `search_notes` prefers `rg --json` and falls back to
  pure Python when ripgrep is absent; both sides skip dotfiles and
  `.obsidian/` so hidden files never leak into results.
- **No surprise frontmatter.** Notes without a frontmatter block stay that
  way; `updated:` is only refreshed (or inserted) when a block exists.
- **Fresh indexes.** Backlink/tag indexes are invalidated by mtime, so
  external edits are picked up on the next read.
- **Symlink confinement.** Symlink targets resolving outside the vault are
  rejected — a link can never pull reads or writes out of the vault.

## Configuration

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `VAULTS_ROOT` | `~/.vaults` | Root dir; each immediate subdir is one vault |
| `VAULTS_HUB_DEBUG` | unset | Set to `1` to enable debug file logging; anything else disables it |
| `VAULTS_HUB_GIT` | unset (versioning on) | Set to `0` to disable git versioning; `history`/`restore` then return errors |

Precedence for the vaults root: `--vaults-root <dir>` flag > `VAULTS_ROOT`
env var > default `~/.vaults`. Whatever wins is auto-created on startup
when missing.

Notes:

- **ripgrep is optional.** If `rg` is on `PATH`, `search_notes` uses `rg --json`; otherwise a pure-Python fallback (with minimal `.gitignore` handling) is used. No configuration needed either way.
- **POSIX-only.** File locking uses `fcntl`, so Windows is not supported.
- **npx launcher.** `VAULTS_HUB_PYTHON` overrides which Python the
  `vaults-hub` bin uses (else `python3`, then `python` on `PATH`;
  must be 3.10+). When that interpreter lacks the pinned dependencies,
  the bin installs them once into a cached venv under
  `$XDG_CACHE_HOME/vaults-hub` (else `~/.cache/vaults-hub`), keyed by
  Python minor version, and reuses it silently while the
  `requirements.txt` hash matches.

## Versioning

Each vault is a local git repo (one per vault root), maintained automatically:

- **Default-on.** The repo is lazily `git init -b main` on the first mutation;
  every write/append/delete/move commits path-scoped with a `Sha256:` trailer
  (e.g. `vaults: write Notes.md`). The write itself is the source of truth —
  a missing git binary or failed commit warns but never blocks the write.
- **Local-only, no push.** Versioning never pushes, pulls, or fetches; identity
  is per-invocation (`vaults-hub`), never written to gitconfig. Lock/tmp files
  and `.obsidian/` are excluded via `$GIT_DIR/info/exclude` (no visible
  `.gitignore`). A vault already nested inside an external repo is left alone.
- **Opt-out.** Set `VAULTS_HUB_GIT=0` to disable init/commits; `history` and
  `restore` then return clear errors.
- **`history(vault, path?, limit?)`** lists `[{sha, date, message}]` (newest
  first, limit clamped 1–200) plus a `truncated` flag when more commits exist
  beyond the page; an untracked path returns `[]` with
  `untracked:true`. **`restore(vault, path, rev, expected_sha256?)`** writes
  `git show rev:path` back to disk atomically (snapshotting dirty state
  first), echoes the revision as `restored_from`, and can
  recreate deleted notes.

## Testing

No test framework — one end-to-end smoke test over a temporary stdio server:

```bash
source .venv/bin/activate
python smoke_test.py
```

It creates fixture vaults in a temp dir, exercises all tools (including
versioning and its opt-out), and exits non-zero with a traceback on failure.

## Architecture (brief)

```
opencode (MCP client, stdio) <-> vaults/server.py (FastMCP "vaults", 14 tools) <-> ~/.vaults/<project>/*.md
```

- `vaults/` is the whole server: `config.py` (vaults root, logging, CLI),
  `notes.py` (path validation, UTF-8 checks, `fcntl` locks, atomic writes,
  SHA-256 optimistic concurrency, frontmatter/wiki-link helpers, note CRUD,
  vault creation),
  `indexes.py` (in-memory backlink/frontmatter indexes with mtime-based
  invalidation), `search.py` (ripgrep + Python-fallback search),
  `versioning.py` (per-vault git layer), and `server.py` (FastMCP app, 14
  tools, stdio bridge). Root `server.py` is a thin shim so `python
  server.py` keeps working.
- `server.py` keeps the event loop responsive: every tool runs its blocking
  library call in a worker thread via `anyio.to_thread`. Errors are
  sanitized at the tool boundary (client-safe `ValueError`s pass through;
  anything else becomes a plain failure notice) while the server log keeps
  the full traceback; request logging records only vault/path.
- `smoke_test.py` spawns `server.py` over stdio with `VAULTS_ROOT` pointed at a temp dir.

## Limitations

- **Private SDK pin:** dependencies are pinned in `requirements.txt`
  (`anyio==4.9.0`, `mcp==1.30.0`, `PyYAML==6.0.3`); bump deliberately and re-run `python smoke_test.py`.
- **Move is copy + delete, not one atomic rename:** `move_note` writes the
  destination atomically, then unlinks the source — a crash between the two
  steps can leave both copies behind. A failed move cleans up the partial
  destination. It also refuses to overwrite an
  existing destination.
- **Wiki-link resolution is name-based:** bare `[[Name]]` links resolve via a
  stem index (shortest path wins on collision); only `[[path/with/slash]]`
  links resolve as paths. Self-links are excluded from backlinks.
- **POSIX-only** (`fcntl` locking); no Windows support.
- **No authentication:** the stdio transport trusts the local client; do not
  expose vault contents beyond your machine without adding your own access
  controls.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, and the PR flow.
