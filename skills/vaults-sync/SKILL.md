---
name: vaults-sync
description: Turns conversation output into durable, structured vault notes — resolving the right vault, surveying and reading existing notes, then writing/append/move with optimistic concurrency and frontmatter. Use when the user asks to save, write, sync, or structure notes, document something, put content in their vault, or types /vault-sync.
metadata:
  version: "0.2.1"
  mcp-server: vaults-hub
---

# Vaults Sync

Workflow for persisting knowledge into `~/.vaults` via the `vaults` MCP server.
Tool reference (arguments, returns, edge cases): see `skills/vaults-hub/SKILL.md`; do not duplicate it here.

## Workflow

1. **Resolve the vault.** `list_vaults`. Match by cwd basename, then git remote repo name. No match → `create_vault(name=<project-name>)`. Vault name must equal the project identity, not the topic.
2. **Survey before writing.** `list_notes(vault=..., path="", recursive=True)` to see the existing tree; `search_notes(query=<topic>, vault=...)` to find related notes. Reuse an existing note over creating a near-duplicate.
3. **Read before write — every time.** Any note you intend to touch gets `read_note` first and you capture `sha256`. Never self-hash; never overwrite without passing that sha back.
4. **Choose the operation:**
   | Situation | Call |
   |-----------|------|
   | New concept / new topic | `write_note` to a new path |
   | Extending an existing note | `append_note` with `expected_sha256` |
   | Renaming / re-homing | `move_note` (needs `src_path`, `dst_path`, `expected_sha256`) |
5. **After writing**, re-`read_note` and confirm the returned `sha256` matches the mutation's response.

```python
vaults = list_vaults()
notes = list_notes(vault="myproj", path="", recursive=True)
existing = read_note(vault="myproj", path="runbooks/deploy.md")   # keep existing["sha256"]
w = append_note(vault="myproj", path="runbooks/deploy.md",
                content="\n## Rollback\n...", expected_sha256=existing["sha256"])
check = read_note(vault="myproj", path="runbooks/deploy.md")       # check["sha256"] == w["sha256"]
```
## Frontmatter

Every new note that warrants discoverability gets:

```markdown
---
title: <human title>
tags: [<project>/<topic>, <project>/<status>]
created: YYYY-MM-DD
updated: YYYY-MM-DD
---
```

`updated` auto-refreshes only if the block already exists — set it on creation, never hand-set it on later writes.

## Wiki-links and tags

- Cross-reference in-vault with name-based `[[Note Name]]`. Use path-based `[[dir/Note]]` only on name collision. Never `[[../Outside]]` — the vault is the boundary.
- Tags follow `<project-name>/<topic>` (e.g. `vaults-hub/runbook`). Run `list_tags(vault=...)` before inventing new ones and reuse what fits.
## Note templates

```markdown
# ADR — adr-<NNNN>-<slug>.md
## Context
## Decision
## Consequences
## Alternatives Considered

# Runbook — how-to-<slug>.md
## When to use
## Prerequisites
## Steps
## Verify
## Rollback

# Spec — spec-<slug>.md
## Purpose
## Interface   (inputs, outputs, errors)
## Behavior    (happy path, edge cases)
## Open questions
```
## Failure handling

- `commit_error` in a mutation response: the disk write succeeded. Do not retry. Report the write and surface the error verbatim.
- Sha mismatch on the post-write read: investigate with `history(vault, path)`, then `restore(vault, path, rev=...)` to roll back.
- Numbered steps are repeatable; skipping step 3 is the #1 mistake — read first, never self-hash.
