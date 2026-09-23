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

If you have [ruff](https://docs.astral.sh/ruff/) installed, keep the diff clean:

```bash
ruff check server.py smoke_test.py
ruff format --check server.py smoke_test.py
```

# PR flow

1. Keep `server.py` + `smoke_test.py` as the source of truth; update docs when behavior changes.
2. Run `python smoke_test.py` and paste the result in the PR.
3. Small, focused diffs — one concern per PR.
