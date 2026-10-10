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
- `append_note`, `move_note` and `restore` refuse notes that are not valid
  UTF-8 instead of silently replacing bytes, and keep CRLF line endings.
- `search_notes` returns the same results with or without ripgrep: only `.md`
  notes, no symlinks, and `.gitignore` is no longer honored by either backend.
- Vault directories that are symlinks are no longer listed, searched across,
  or counted by `list_tags`; previously one broke `list_tags` for all vaults.
- `updated_refreshed` no longer reports true when only a look-alike key such
  as `last_updated:` exists; `updated:` is now inserted.
- `read_note` resolves wiki-links with one note scan instead of one per link.
- Rejected `write_note` paths and no-op `delete_note(missing_ok=True)` calls
  no longer create directories or lock files.
- npm launcher: concurrent first runs no longer share a half-built venv.
- Notes in hidden folders such as Obsidian's `.trash/` no longer contribute
  backlinks or tag counts, matching `list_notes` and `search_notes`.
- The server uses the MCP SDK's stock stdio transport instead of a custom
  stdin bridge.
- The release workflow runs the full CI (all Python versions and the
  launcher test) before publishing.
- Contributors: `smoke_test.py --only=rg-fallback` is gone, since the main run
  already covers the ripgrep-absent leg, and a release now bumps three version
  literals instead of six.

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
