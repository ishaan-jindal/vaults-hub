"""Full-text search: ripgrep when available, pure-Python fallback otherwise."""

import fnmatch
import json
import re
import shutil
import subprocess
from pathlib import Path

from vaults.indexes import _all_notes
from vaults.notes import _note_path, _read_text, _rel, _vault_root, list_vaults


def _gitignore_patterns(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if not gi.is_file():
        return []
    try:
        return [
            ln.strip()
            for ln in gi.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    except OSError:
        return []


def _gitignored(root: Path, p: Path, patterns: list[str]) -> bool:
    """Minimal .gitignore matching for the Python search fallback."""
    rel = _rel(root, p)
    for raw in patterns:
        if raw.startswith("!"):
            continue
        anchored = raw.startswith("/")
        pat = raw.lstrip("/")
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        if dir_only:
            if rel.startswith(pat + "/"):
                return True
            continue
        if "*" not in pat:
            if anchored:
                if rel == pat or rel.startswith(pat + "/"):
                    return True
            elif pat in rel.split("/"):
                return True
        elif anchored:
            if fnmatch.fnmatch(rel, pat):
                return True
        elif any(fnmatch.fnmatch(part, pat) for part in rel.split("/")):
            return True
    return False


def search_notes(
    query: str,
    vault: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = 50,
    before: int = 0,
    after: int = 0,
) -> dict:
    before = max(0, min(10, int(before)))
    after = max(0, min(10, int(after)))
    limit = max(1, min(200, int(limit)))
    roots = [_vault_root(vault)] if vault else [Path(v["path"]) for v in list_vaults()]
    hits = _collect_hits(roots, query, regex, case_sensitive, limit)
    _attach_context(hits, roots, before, after)
    return {"query": query, "matches": hits}


def _collect_hits(roots, query, regex, case_sensitive, limit) -> list[dict]:
    """Collect raw matches via rg, or the Python fallback when rg is absent."""
    if shutil.which("rg"):
        return _search_rg(roots, query, regex, case_sensitive, limit)
    return _search_fallback(roots, query, regex, case_sensitive, limit)


def _search_rg(roots, query, regex, case_sensitive, limit) -> list[dict]:
    args = ["rg", "--json", "-n", "--no-heading", "--glob", "!**/.obsidian/**"]
    if not regex:
        args.append("--fixed-strings")
    if not case_sensitive:
        args.append("--ignore-case")
    args += ["--max-count", "20", "--", query]
    hits: list[dict] = []
    for root in roots:
        try:
            proc = subprocess.run(args + [str(root)], capture_output=True, text=True, timeout=30)
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
            try:
                abspath = Path(d["path"]["text"]).resolve()
                rel = abspath.relative_to(root).as_posix()
                text = d["lines"]["text"].strip()
            except (KeyError, ValueError, OSError, AttributeError, TypeError):
                # Non-UTF-8 paths arrive as {"bytes": "<base64>"} with no
                # "text" key; skip rather than crash the tool.
                continue
            hits.append(
                {
                    "vault": root.name,
                    "path": rel,
                    "line": d["line_number"],
                    "text": text,
                }
            )
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    return hits[:limit]


def _search_fallback(roots, query, regex, case_sensitive, limit) -> list[dict]:
    flags = 0 if case_sensitive else re.IGNORECASE
    pat = re.compile(query if regex else re.escape(query), flags)
    hits: list[dict] = []
    for root in roots:
        ignore = _gitignore_patterns(root)
        for p in _all_notes(root):
            if _gitignored(root, p, ignore):
                continue
            try:
                lines = _read_text(p).splitlines()
            except OSError:
                continue
            file_hits = 0
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
                    file_hits += 1
                    if file_hits >= 20 or len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
    return hits[:limit]


def _attach_context(hits, roots, before, after) -> None:
    """Add before_lines/match_line/after_lines by rereading each file once."""
    if not hits:
        return
    vault_to_root = {r.name: r for r in roots}
    cache: dict[tuple[str, str], list[str]] = {}
    for hit in hits:
        key = (hit["vault"], hit["path"])
        if key not in cache:
            base = vault_to_root.get(hit["vault"])
            try:
                cache[key] = _read_text(_note_path(base, hit["path"])).splitlines()
            except (OSError, ValueError, AttributeError):
                cache[key] = []
        lines = cache[key]
        idx = hit["line"] - 1
        match = lines[idx].strip() if 0 <= idx < len(lines) else hit["text"]
        lo = max(0, idx - before)
        hi = min(len(lines), idx + 1 + after)
        hit["match_line"] = match
        hit["before_lines"] = [ln.strip() for ln in lines[lo:idx] if idx >= 0]
        hit["after_lines"] = [ln.strip() for ln in lines[idx + 1 : hi]]
