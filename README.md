# vaults-hub

A single-file Python [MCP](https://modelcontextprotocol.io) server that exposes your
[Obsidian](https://obsidian.md)-style Markdown vaults to MCP clients (e.g. OpenCode).

Keep each project's docs as plain Markdown on disk, and let an agent
read and maintain them through a small, predictable tool API — no Obsidian
plugins, windows, ports, or API keys. Every change is auto-versioned with
local git, so nothing is ever truly lost.

## Quickstart

Prereqs: Python 3.10+ (developed/tested on 3.14), a POSIX platform
(Linux/macOS — locking uses `fcntl`), and optionally
[ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) for faster search
(a pure-Python fallback is used when `rg` is absent).

```bash
git clone <repo-url> vaults-hub
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
copy any hardcoded home directory):

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

11 tools (see `server.py` for exact descriptions):

| Tool | What it does |
| ---- | ------------ |
| `list_vaults` | List all project vaults with note counts |
| `list_notes` | List dirs/notes under a vault path (`recursive=True` for flat listing) |
| `read_note` | Read a note: frontmatter, content, wiki-links, backlinks, unresolved links |
| `write_note` | Create or atomically overwrite a note (`expected_sha256` for optimistic concurrency) |
| `append_note` | Append text to a note (created if missing), atomic write, optional `expected_sha256` |
| `delete_note` | Delete a note (`missing_ok=True` to tolerate absence); cleans up sibling tmp files (lock files are left in place) |
| `move_note` | Move/rename a note; errors if src missing or dst exists; honors `expected_sha256`; refreshes `updated:` |
| `list_tags` | Tag counts per vault from cached frontmatter (one vault or all) |
| `search_notes` | Full-text search via ripgrep (or Python fallback) across one vault or all, with before/after context lines |
| `history` | Version history for a note (`[{sha, date, message}]`) or whole vault from the local git repo |
| `restore` | Restore a note from a past revision (snapshots dirty state first; recreates deleted notes) |

Also built in: YAML-frontmatter parsing with auto-refreshed `updated:` dates,
file locking (`fcntl`), atomic writes via temp-file + rename, backlink/tag
indexes with mtime invalidation, and optional debug logging to file.

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
  first, limit clamped 1–200); an untracked path returns `[]` with
  `untracked:true`. **`restore(vault, path, rev, expected_sha256?)`** writes
  `git show rev:path` back to disk (snapshotting dirty state first) and can
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
opencode (MCP client, stdio) <-> server.py (FastMCP "vaults", 11 tools) <-> ~/.vaults/<project>/*.md
```

- `server.py` is the whole server: path validation, UTF-8 checks, `fcntl`
  shared/exclusive locks, atomic writes (temp file + `os.replace`), SHA-256
  optimistic concurrency, in-memory backlink/frontmatter indexes keyed by
  vault with mtime-based invalidation, link/tag/search helpers, and the
  per-vault git versioning layer.
- `smoke_test.py` spawns `server.py` over stdio with `VAULTS_ROOT` pointed at a temp dir.

## Limitations

- **Private SDK pin:** dependencies are pinned in `requirements.txt`
  (`anyio==4.9.0`, `mcp==1.30.0`, `PyYAML==6.0.3`); bump deliberately and re-run `python smoke_test.py`.
- **Move is copy + delete, not one atomic rename:** `move_note` writes the
  destination atomically, then unlinks the source — a crash between the two
  steps can leave both copies behind. It also refuses to overwrite an
  existing destination.
- **Wiki-link resolution is name-based:** bare `[[Name]]` links resolve via a
  stem index (shortest path wins on collision); only `[[path/with/slash]]`
  links resolve as paths. Self-links are excluded from backlinks.
- **POSIX-only** (`fcntl` locking); no Windows support.
- **No authentication:** the stdio transport trusts the local client; do not
  expose vault contents beyond your machine without adding your own access
  controls.
