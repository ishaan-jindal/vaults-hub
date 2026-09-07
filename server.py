"""Vaults hub: local stdio MCP server exposing every project vault at once.

Each project has its own Obsidian vault under VAULTS_ROOT (default
~/Dev/vaults/<project>/). This server operates directly on the markdown files,
so opencode can read/write/search all vaults with no Obsidian windows, ports,
or API keys involved. Spawned per-session by opencode over stdio.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import yaml
from mcp.server.mcpserver import MCPServer

VAULTS_ROOT = Path(
    os.environ.get("VAULTS_ROOT", str(Path.home() / "Dev" / "vaults"))
).resolve()

mcp = MCPServer("vaults")

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
WIKI_LINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
UPDATED_RE = re.compile(r"(?m)^updated:.*$")


def _vault_root(vault: str) -> Path:
    root = (VAULTS_ROOT / vault).resolve()
    if root.parent != VAULTS_ROOT or not root.is_dir():
        raise ValueError(f"unknown vault: {vault!r}")
    return root


def _all_notes(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md") if ".obsidian" not in p.parts)


def _rel(root: Path, p: Path) -> str:
    return p.resolve().relative_to(root).as_posix()


def _note_path(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes vault: {rel!r}")
    return p


def _split_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        data = {}
    return (data if isinstance(data, dict) else {}), text[m.end() :]


def _refresh_updated(text: str) -> tuple[str, bool]:
    """Bump `updated:` in the frontmatter block. Returns (text, changed)."""
    m = FRONTMATTER_RE.match(text)
    if not m or "updated:" not in m.group(1):
        return text, False
    new_block = UPDATED_RE.sub(
        f"updated: {date.today().isoformat()}", m.group(1), count=1
    )
    return text[: m.start(1)] + new_block + text[m.end(1) :], True


def _resolve_link(root: Path, target: str) -> Path | None:
    target = target.strip()
    if not target:
        return None
    if "/" in target:
        cand = (root / target).resolve()
        if cand.is_file():
            return cand
        cand_md = cand.with_suffix(".md") if cand.suffix != ".md" else cand
        return cand_md if cand_md.is_file() else None
    matches = [p for p in _all_notes(root) if p.stem == target]
    if not matches:
        return None
    # Obsidian resolves duplicate note names to the shortest path.
    return sorted(matches, key=lambda p: (len(p.parts), p.as_posix()))[0]


def _link_info(
    root: Path, here: Path, text: str
) -> tuple[list[str], list[str], list[str]]:
    targets = [t.strip() for t in WIKI_LINK_RE.findall(text) if t.strip()]
    links, unresolved = [], []
    for t in targets:
        hit = _resolve_link(root, t)
        (links if hit else unresolved).append(t)
    links, unresolved = sorted(set(links)), sorted(set(unresolved))
    backlinks = sorted(
        {
            _rel(root, p)
            for p in _all_notes(root)
            if p != here and here in _link_targets(root, p)
        }
    )
    return links, backlinks, unresolved


def _link_targets(root: Path, p: Path) -> set[Path]:
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return set()
    out = set()
    for t in WIKI_LINK_RE.findall(text):
        hit = _resolve_link(root, t.strip())
        if hit:
            out.add(hit)
    return out


@mcp.tool(description="List all project vaults with their note counts.")
def list_vaults() -> list[dict]:
    if not VAULTS_ROOT.is_dir():
        return []
    return [
        {
            "name": d.name,
            "path": str(d.resolve()),
            "notes": len(_all_notes(d.resolve())),
        }
        for d in sorted(VAULTS_ROOT.iterdir())
        if d.is_dir() and not d.name.startswith(".")
    ]


@mcp.tool(
    description="List directories and notes under a vault path (vault-root-relative, '' for root)."
)
def list_notes(vault: str, path: str = "") -> dict:
    root = _vault_root(vault)
    base = _note_path(root, path or ".")
    if not base.is_dir():
        raise ValueError(f"not a directory: {path!r}")
    dirs, notes = [], []
    for p in sorted(base.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            dirs.append(_rel(root, p) + "/")
        elif p.suffix == ".md":
            notes.append(_rel(root, p))
    return {"vault": vault, "path": path, "dirs": dirs, "notes": notes}


@mcp.tool(
    description="Read a note: frontmatter, content, wiki-links, backlinks, unresolved links."
)
def read_note(vault: str, path: str) -> dict:
    root = _vault_root(vault)
    p = _note_path(root, path)
    if not p.is_file():
        raise ValueError(f"note not found: {path!r}")
    text = p.read_text(encoding="utf-8")
    frontmatter, _ = _split_frontmatter(text)
    links, backlinks, unresolved = _link_info(root, p.resolve(), text)
    return {
        "vault": vault,
        "path": _rel(root, p),
        "frontmatter": frontmatter,
        "content": text,
        "links": links,
        "backlinks": backlinks,
        "unresolved_links": unresolved,
    }


@mcp.tool(
    description="Create or overwrite a note (vault-root-relative path). Refreshes frontmatter `updated:` date."
)
def write_note(vault: str, path: str, content: str) -> dict:
    root = _vault_root(vault)
    p = _note_path(root, path)
    if p.suffix != ".md":
        raise ValueError("note path must end in .md")
    p.parent.mkdir(parents=True, exist_ok=True)
    content, refreshed = _refresh_updated(content)
    p.write_text(content, encoding="utf-8")
    return {
        "vault": vault,
        "path": _rel(root, p),
        "bytes": len(content.encode()),
        "updated_refreshed": refreshed,
    }


@mcp.tool(
    description="Append text to a note (created if missing). Refreshes frontmatter `updated:` date."
)
def append_note(vault: str, path: str, text: str) -> dict:
    root = _vault_root(vault)
    p = _note_path(root, path)
    if p.suffix != ".md":
        raise ValueError("note path must end in .md")
    current = p.read_text(encoding="utf-8") if p.is_file() else ""
    if current and not current.endswith("\n"):
        current += "\n"
    return write_note(vault, path, current + text)


@mcp.tool(
    description="Search note contents with ripgrep across one vault or all vaults."
)
def search_notes(
    query: str,
    vault: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = 50,
) -> dict:
    roots = [_vault_root(vault)] if vault else [Path(v["path"]) for v in list_vaults()]
    rg = shutil.which("rg")
    args = ["rg", "--json", "-n", "--no-heading", "--glob", "!**/.obsidian/**"]
    if not regex:
        args.append("--fixed-strings")
    if not case_sensitive:
        args.append("--ignore-case")
    args += ["--max-count", "20", "--", query]
    hits: list[dict] = []
    if rg:
        for root in roots:
            try:
                proc = subprocess.run(
                    args + [str(root)], capture_output=True, text=True, timeout=30
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            for line in proc.stdout.splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "match":
                    continue
                d = ev["data"]
                abspath = Path(d["path"]["text"]).resolve()
                hits.append(
                    {
                        "vault": root.name,
                        "path": abspath.relative_to(root).as_posix(),
                        "line": d["line_number"],
                        "text": d["lines"]["text"].strip(),
                    }
                )
                if len(hits) >= limit:
                    break
            if len(hits) >= limit:
                break
    else:  # fallback: plain Python scan
        flags = 0 if case_sensitive else re.IGNORECASE
        pat = re.compile(query if regex else re.escape(query), flags)
        for root in roots:
            for p in _all_notes(root):
                try:
                    lines = p.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                for i, ln in enumerate(lines, 1):
                    if pat.search(ln):
                        hits.append(
                            {
                                "vault": root.name,
                                "path": _rel(root, p),
                                "line": i,
                                "text": ln.strip(),
                            }
                        )
                        if len(hits) >= limit:
                            break
                if len(hits) >= limit:
                    break
    return {"query": query, "matches": hits[:limit]}


if __name__ == "__main__":
    mcp.run()
