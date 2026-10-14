---
name: vaults-hub
description: Manages project knowledge-base notes in Obsidian Markdown vaults (~/.vaults) via the vaults-hub MCP server. Use when reading, writing, appending, moving, deleting, searching, tagging, or restoring vault notes, frontmatter, wiki-links, and backlinks.
metadata:
  version: "0.2.1"
  mcp-server: vaults-hub
---

# Vaults Hub

Manages Obsidian-style Markdown vaults under `VAULTS_ROOT` (default `~/.vaults`).

## Tools

| Tool | Purpose |
|------|---------|
| `list_vaults` | List all vaults + note counts |
| `create_vault` | Create a new vault |
| `delete_vault` | Delete vault; requires `confirm==name`, irreversible |
| `list_notes` | Browse notes; `path` (''=root), `recursive`, `offset`/`limit` → `total`/`truncated`/`next_offset`; `dirs=[]` when `recursive=True` |
| `read_note` | Read note → `frontmatter`, `content`, `sha256`, `links`, `backlinks`, `unresolved_links` |
| `write_note` | Create/update note; `expected_sha256` for guarded write |
| `append_note` | Append text; `expected_sha256` for guarded write |
| `delete_note` | Delete note; unguarded, no sha required; `missing_ok=True` to skip if absent |
| `move_note` | Move/rename; args `src_path`, `dst_path`, `expected_sha256`; copy+delete, not atomic, errors if dst exists |
| `history` | List revisions → `[{sha,date,message}]`, truncated; `untracked` if uncommitted; omit `path` for whole vault; no `rev` param |
| `restore` | Restore `path` to git `rev` (sha from `history`); optional `expected_sha256` |
| `list_tags` | List tags with counts; `vault` optional (omit = all vaults) |
| `search_notes` | Full-text search; `regex`, `case_sensitive`, `limit`, `before`/`after`; `vault` optional (omit = all vaults) |
| `server_info` | Diagnostic only (version, roots, paths) |

## Happy path

```python
notes = list_notes(vault="myproj", path="", recursive=True)
note = read_note(vault="myproj", path="todo.md")  # save note["sha256"]
w = write_note(vault="myproj", path="todo.md", content="...", expected_sha256=note["sha256"])
h = history(vault="myproj", path="todo.md")
restore(vault="myproj", path="todo.md", rev=h["history"][0]["sha"], expected_sha256=w["sha256"])
search_notes(query="todo", vault="myproj", regex=False)
```

## Rules

- Paths vault-root-relative (''=root), never absolute, no escaping symlinks. Every note path arg must end in `.md` (`move_note` takes `src_path`/`dst_path`).
- Optimistic concurrency: pass `expected_sha256` on existing notes for write/append/move/restore using `sha256` from latest read or mutation (never self-hash; `updated` auto-refresh + CRLF change bytes). Omit `expected_sha256` only when creating new note or restoring deleted note.
- Versioning: mutations return versioning `ok`/`external`/`disabled`/`missing` + optional `commit_error`. `commit_error` means disk write succeeded — do NOT retry `append_note`. `history` returns `untracked:true` with empty list if uncommitted; raises `ValueError` if `VAULTS_HUB_GIT=0`.
- Search: `regex=True` avoid lookarounds/backrefs (rg Rust vs Python `re`). Capped 20 hits/file, `limit` ≤200, `truncated` flag. Skips dotfiles/`.obsidian/`/`.trash/`/symlinks.
- Frontmatter `title`/`tags`/`created`/`updated` YYYY-MM-DD; `updated` auto-refreshes only if block exists. Name-based `[[wiki-links]]`.
- Vault name = cwd basename or git repo name; `create_vault` first if unknown (mutations raise unknown vault). Resolve vault each call.
