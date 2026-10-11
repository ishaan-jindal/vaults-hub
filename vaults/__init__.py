"""Vaults hub: local stdio MCP server exposing every project vault at once.

Each project has its own Obsidian vault under the vaults root (default
~/.vaults/<project>/). This package operates directly on the markdown files,
so opencode can read/write/search all vaults with no Obsidian windows, ports,
or API keys involved. Spawned per-session by opencode over stdio.
"""

__version__ = "0.2.0"
