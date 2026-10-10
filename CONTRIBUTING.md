# Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

# Checks

```bash
python smoke_test.py   # end-to-end, no framework; must exit 0
```

`smoke_test.py` remains the primary suite and needs no Node.

With Node installed, also run the launcher integration test:

```bash
python npm_launcher_test.py   # packs + installs the tarball, drives the launcher over stdio
```

If you have [ruff](https://docs.astral.sh/ruff/) installed, keep the diff clean:

```bash
ruff check vaults server.py smoke_test.py
ruff format --check vaults server.py smoke_test.py
```

# CI

CI runs on every push to main and every pull request, with two independent jobs:

- `python`: matrix over 3.10 / 3.12 / 3.14 running `ruff check`, `ruff format --check`, and `python smoke_test.py`.
- `npm`: syntax-checks the launcher (`node --check bin/vaults-hub.mjs`), asserts the exact tarball file list (a broken package must never be published), and runs `python npm_launcher_test.py`.

The two jobs are independent on purpose, so one failure does not hide the other's result.

# Release

Release procedure, in order:

1. Run the suite locally: `python smoke_test.py` (must exit 0) and, with Node installed, `python npm_launcher_test.py`.
2. Bump the six version literals together (`pyproject.toml`, `package.json`, `vaults/server.py` `SERVER_VERSION`, `vaults/__init__.py` `__version__`, and the two version asserts in `smoke_test.py`). The release workflow fails if the tag disagrees with `package.json`.
3. Commit + push to main.
4. Push the `v<version>` tag from your machine (`git push origin v<version>`). Tags are pushed from your machine, never from CI.
5. CI takes over: the `verify` job re-runs the whole CI workflow (both jobs above); the `publish` job publishes to npm via OIDC trusted publishing; the `release` job creates the GitHub Release with notes extracted from the `CHANGELOG.md` section for that version.

One-time manual bootstrap: `0.2.0` must be published by hand with `npm publish` from a laptop, because npm's OIDC trusted publishing cannot create a new package name. Afterwards, configure the Trusted Publisher on npmjs.com for the `vaults-hub` package: user `ishaan-jindal`, repository `vaults-hub`, workflow filename `release.yml`, environment blank, and explicitly allow `npm publish` (new configs default to stage-only). Every release from `0.3.0` onward is then fully automated.

# PR flow

1. Keep the `vaults/` package + `smoke_test.py` as the source of truth (root `server.py` is a thin shim); update docs when behavior changes.
2. Run `python smoke_test.py` and paste the result in the PR.
3. Small, focused diffs — one concern per PR.
