"""Vaults hub: local stdio MCP server exposing every project vault at once.

Each project has its own Obsidian vault under the vaults root (default
~/.vaults/<project>/). This package operates directly on the markdown files,
so opencode can read/write/search all vaults with no Obsidian windows, ports,
or API keys involved. Spawned per-session by opencode over stdio.
"""

__version__ = "0.1.0"

__all__ = ["__version__", "main", "mcp"]


def __getattr__(name: str):
    """Lazily re-export server globals so `import vaults` stays light.

    vaults.server is imported on first attribute access instead of at
    package import time; this also keeps `python -m vaults.server` free
    of duplicate-import warnings.
    """
    if name in ("main", "mcp"):
        from vaults import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
