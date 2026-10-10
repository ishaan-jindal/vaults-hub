# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

- Wiki-link resolution is confined to the vault: path targets that escape it
  (e.g. `[[../Outside.md]]`) and name links to notes resolving outside it are
  treated as unresolved instead of being followed.
- The backlink/tag index scan skips notes whose real path is outside the vault.
- `list_notes` skips symlink entries that resolve outside the vault instead of
  failing the whole listing.
- Name links to an in-vault symlinked note now resolve to the symlink's target
  file, so backlinks are attributed to the real note.

## [0.2.0] - 2026-09-27

- npm package (`vaults-hub` bin): run via `npx -y vaults-hub`; the launcher
  finds Python 3.10+ and bootstraps the pinned deps into a cached venv.

- Local git versioning per vault (default-on, opt-out via `VAULTS_HUB_GIT=0`):
  lazy `git init` on first mutation, path-scoped commits with `Sha256:`
  trailers, new `history` and `restore` tools. Local-only, never pushes.
- Vaults root (`~/.vaults` default) is auto-created on startup when missing.
- Every tool now declares all four MCP annotation hints explicitly
  (readOnlyHint/destructiveHint/idempotentHint/openWorldHint).
- New `delete_vault` tool: removes the whole vault tree including per-vault
  git history, gated on `confirm` matching the vault name. Irreversible.

## [0.1.0] - 2026-09-24

- Initial release: single-file stdio MCP server (`server.py`) with 9 vault tools
  (list_vaults, list_notes, read_note, write_note, append_note, delete_note,
  move_note, list_tags, search_notes), `smoke_test.py`, and open-source
  packaging (README, LICENSE, pyproject.toml, CONTRIBUTING, CHANGELOG).
