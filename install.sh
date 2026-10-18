#!/usr/bin/env bash
# install.sh - register the `vaults` MCP server and the `vaults-sync` skill with
# every supported MCP client installed on this machine.
#
# Dry-run by default; pass --apply to actually write:
#   ./install.sh            # show what would happen
#   ./install.sh --apply    # merge configs, symlink the skill, drop hints
set -eu

# --- guards ----------------------------------------------------------------

# Piped execution means nobody read this script before running it.
if [ -z "${BASH_SOURCE[0]:-}" ] || [ "${BASH_SOURCE[0]}" = /dev/stdin ]; then
  printf '%s\n' 'refuse to run from pipe; download and inspect first' >&2
  exit 1
fi

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
SKILL_SRC="$REPO_ROOT/skills/vaults-sync"

usage() {
  printf '%s\n' \
    'usage: install.sh [--apply]' \
    '' \
    'Detect installed MCP clients (OpenCode, Claude Code, Claude Desktop, Cursor,' \
    'Codex CLI). For each one: register the `vaults` MCP server, symlink the' \
    'vaults-sync skill into the shared skill dirs, and drop an auto-load hint.' \
    '' \
    'Default is a dry run: nothing is written. Pass --apply to apply.' >&2
}

apply=0
for arg in "$@"; do
  case "$arg" in
    --apply) apply=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$arg" >&2; usage; exit 1 ;;
  esac
done

if [ ! -d "$SKILL_SRC" ]; then
  printf 'skill source not found: %s\n' "$SKILL_SRC" >&2
  exit 1
fi

# --- helpers ---------------------------------------------------------------

say() { printf '%s\n' "$*" >&2; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

rows=()
add_row() { rows+=("$(printf '%-18s %s' "$1" "$2")"); }

HINT_BODY='# vaults-hub MCP

The `vaults` MCP server is registered. When the user asks to save, sync, write, or structure notes, load the `vaults-sync` skill before acting.'

# Drop the hint into an existing AGENTS.md/CLAUDE.md, or create a 5-line one.
# Appends rather than clobbering a user-authored config-root file.
ensure_hint() {
  hint_file=$1
  if [ -f "$hint_file" ] && grep -qF '# vaults-hub MCP' "$hint_file"; then
    say "  ok: auto-load hint already in $hint_file"
    return 0
  fi
  if [ "$apply" != 1 ]; then
    say "  would add auto-load hint to $hint_file"
    return 0
  fi
  mkdir -p "$(dirname "$hint_file")"
  wrote=0
  if [ -f "$hint_file" ]; then
    { cat "$hint_file"; printf '\n%s\n' "$HINT_BODY"; } > "$hint_file.tmp" && wrote=1
  else
    printf '%s\n' "$HINT_BODY" > "$hint_file.tmp" && wrote=1
  fi
  if [ "$wrote" = 1 ] && mv "$hint_file.tmp" "$hint_file"; then
    say "  auto-load hint written to $hint_file"
    return 0
  fi
  rm -f "$hint_file.tmp"
  warn "$hint_file: could not write auto-load hint"
  return 1
}

# Symlink the skill dir into a client skills dir. Sets LINK_STATE for the summary.
ensure_link() {
  link_dest=$1
  if [ -L "$link_dest" ]; then
    link_now=$(readlink "$link_dest")
    if [ "$link_now" = "$SKILL_SRC" ]; then
      LINK_STATE='already linked'
      say "  ok: $link_dest -> $SKILL_SRC"
    else
      LINK_STATE='conflict'
      warn "$link_dest points to $link_now, not $SKILL_SRC - leaving it untouched"
      return 1
    fi
    return 0
  fi
  if [ -e "$link_dest" ]; then
    LINK_STATE='conflict'
    warn "$link_dest exists and is not a symlink - leaving it untouched"
    return 1
  fi
  if [ "$apply" != 1 ]; then
    LINK_STATE='would link'
    say "  would link $link_dest -> $SKILL_SRC"
    return 0
  fi
  if mkdir -p "$(dirname "$link_dest")" && ln -s "$SKILL_SRC" "$link_dest"; then
    LINK_STATE='linked'
    say "  linked $link_dest -> $SKILL_SRC"
    return 0
  fi
  LINK_STATE='failed'
  warn "$link_dest: could not create symlink"
  return 1
}

# Strip // line comments and trailing commas from a .jsonc stream so jq/python3
# can parse it. Writes cleaned JSON to stdout. Used only when the config file
# ends in .jsonc; .json files pass through unchanged.
jsonc_stdin() {
  python3 - "$1" <<'PY'
import re, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    raw = handle.read()
# Drop // line comments that are not inside a string. Conservative: skip
# strings first, strip comments outside them, then reassemble. JSONC has
# no block comments per the OpenCode / VS Code spec we follow.
def strip(line):
    out, i, in_str, esc = [], 0, False, False
    while i < len(line):
        ch = line[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
            break
        out.append(ch)
        i += 1
    return "".join(out)
cleaned = "\n".join(strip(ln) for ln in raw.splitlines())
# Remove trailing commas before } or ].
cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
sys.stdout.write(cleaned)
PY
}

# Merge the `vaults` entry into a JSON config without touching anything else.
# $1 = file, $2 = shape: "opencode" (.mcp.servers.vaults) or "mcpServers".
ensure_json() {
  json_file=$1
  shape=$2
  case "$shape" in
    opencode)
      json_key='mcp'
      json_subkey='servers'
      json_entry='{"type":"local","command":"npx","args":["-y","vaults-hub"]}'
      ;;
    *)
      json_key='mcpServers'
      json_subkey=''
      json_entry='{"command":"npx","args":["-y","vaults-hub"]}'
      ;;
  esac

  is_jsonc=0
  case "$json_file" in
    *.jsonc) is_jsonc=1 ;;
  esac

  # jq path expression to the vaults entry: mcp.servers.vaults vs mcpServers.vaults.
  if [ -n "$json_subkey" ]; then
    jq_path=".$json_key.$json_subkey.vaults"
  else
    jq_path=".$json_key.vaults"
  fi

  merged=''
  if command -v jq >/dev/null 2>&1; then
    if [ -s "$json_file" ]; then
      if [ "$is_jsonc" = 1 ]; then
        if jsonc_stdin "$json_file" | jq -e "$jq_path == $json_entry" >/dev/null 2>&1; then
          say "  ok: vaults already registered in $json_file"
          return 0
        fi
        if jsonc_stdin "$json_file" | jq -e "$jq_path != null" >/dev/null 2>&1; then
          warn "$json_file: $jq_path already present (different value) - skipping to avoid duplicate"
          return 1
        fi
        if ! merged=$(jsonc_stdin "$json_file" | jq ".$json_key.$json_subkey.vaults = $json_entry" 2>/dev/null); then
          warn "$json_file: not valid JSONC (or not an object) - skipping"
          return 1
        fi
      else
        if jq -e "$jq_path == $json_entry" "$json_file" >/dev/null 2>&1; then
          say "  ok: vaults already registered in $json_file"
          return 0
        fi
        if jq -e "$jq_path != null" "$json_file" >/dev/null 2>&1; then
          warn "$json_file: $jq_path already present (different value) - skipping to avoid duplicate"
          return 1
        fi
        if ! merged=$(jq ".$json_key.vaults = $json_entry" "$json_file" 2>/dev/null); then
          warn "$json_file: not valid JSON (or not an object) - skipping"
          return 1
        fi
      fi
    else
      if [ -n "$json_subkey" ]; then
        merged=$(printf '{}\n' | jq ".$json_key.$json_subkey.vaults = $json_entry")
      else
        merged=$(printf '{}\n' | jq ".$json_key.vaults = $json_entry")
      fi
    fi
  elif command -v python3 >/dev/null 2>&1; then
    json_rc=0
    merged=$(python3 - "$json_file" "$shape" "$is_jsonc" "$json_subkey" <<'PY'
import json, re, sys

path, shape, is_jsonc, subkey = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
if shape == "opencode":
    key, entry = "mcp", {"type": "local", "command": "npx", "args": ["-y", "vaults-hub"]}
else:
    key, entry = "mcpServers", {"command": "npx", "args": ["-y", "vaults-hub"]}

try:
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()
except FileNotFoundError:
    raw = ""

if is_jsonc and raw:
    def strip_comments(line):
        out, i, in_str, esc = [], 0, False, False
        while i < len(line):
            ch = line[i]
            if in_str:
                out.append(ch)
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                i += 1
                continue
            if ch == '"':
                in_str = True
                out.append(ch)
                i += 1
                continue
            if ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                break
            out.append(ch)
            i += 1
        return "".join(out)
    raw = "\n".join(strip_comments(ln) for ln in raw.splitlines())
    raw = re.sub(r",(\s*[}\]])", r"\1", raw)

if not raw.strip():
    doc = {}
else:
    try:
        doc = json.loads(raw)
    except (OSError, ValueError) as exc:
        print("WARNING: %s: unreadable JSON (%s) - skipping" % (path, exc), file=sys.stderr)
        sys.exit(1)

if not isinstance(doc, dict):
    print("WARNING: %s: top-level JSON is not an object - skipping" % path, file=sys.stderr)
    sys.exit(1)
container = doc.setdefault(key, {})
if subkey:
    if not isinstance(container, dict):
        print("WARNING: %s: %s is not an object - skipping" % (path, key), file=sys.stderr)
        sys.exit(1)
    container = container.setdefault(subkey, {})
if not isinstance(container, dict):
    print("WARNING: %s: container is not an object - skipping" % path, file=sys.stderr)
    sys.exit(1)
if container.get("vaults") == entry:
    sys.exit(3)
if "vaults" in container:
    print("WARNING: %s: vaults entry already present (different shape) - skipping to avoid duplicate" % path, file=sys.stderr)
    sys.exit(2)
container["vaults"] = entry
if is_jsonc:
    print("NOTE: %s is JSONC; the merged output is JSON (comments stripped)." % path, file=sys.stderr)
print(json.dumps(doc, indent=2))
PY
    ) || json_rc=$?
    if [ "$json_rc" -eq 3 ]; then
      say "  ok: vaults already registered in $json_file"
      return 0
    fi
    if [ "$json_rc" -eq 2 ]; then
      return 1 # python already warned on stderr
    fi
    if [ "$json_rc" -ne 0 ]; then
      return 1 # python already warned on stderr
    fi
  else
    warn "neither jq nor python3 available - cannot edit $json_file"
    return 1
  fi

  if [ -z "$merged" ]; then
    warn "$json_file: empty or unparsable JSON - skipping"
    return 1
  fi

  if [ "$apply" != 1 ]; then
    say "  would register vaults in $json_file"
    return 0
  fi
  mkdir -p "$(dirname "$json_file")"
  # ponytail: no .bak snapshot - tmp+mv is atomic already; add copies if users
  # need local rollback.
  # ponytail: no post-validate of $merged - jq/python output is valid JSON by
  # construction; re-parse if these filters ever get hand-edited.
  if printf '%s\n' "$merged" > "$json_file.tmp" && mv "$json_file.tmp" "$json_file"; then
    say "  registered vaults in $json_file"
    return 0
  fi
  rm -f "$json_file.tmp"
  warn "$json_file: write failed - skipping"
  return 1
}

# Record a client row and count it as a failure when its config was skipped.
finish_client() {
  client_name=$1
  client_ok=$2
  client_note=${3:-}
  if [ "$client_ok" != 1 ]; then
    add_row "$client_name" 'skipped (bad config)'
    failures=$((failures + 1))
    return 0
  fi
  status='ok'
  [ -n "$client_note" ] && status="ok ($client_note)"
  [ "$apply" = 1 ] || status="$status (dry run)"
  add_row "$client_name" "$status"
}

# --- clients ---------------------------------------------------------------

do_opencode() {
  opencode_dir="$HOME/.config/opencode"
  if [ ! -d "$opencode_dir" ]; then add_row opencode 'not detected'; return 0; fi
  detected=$((detected + 1))
  say ''
  say "OpenCode: $opencode_dir"
  opencode_cfg="$opencode_dir/opencode.json"
  if [ ! -f "$opencode_cfg" ] && [ -f "$opencode_dir/opencode.jsonc" ]; then
    opencode_cfg="$opencode_dir/opencode.jsonc"
  fi
  opencode_ok=1
  ensure_json "$opencode_cfg" opencode || opencode_ok=0
  ensure_hint "$opencode_dir/AGENTS.md" || failures=$((failures + 1))
  need_agents_skills=1
  finish_client opencode "$opencode_ok"
}

do_claude_code() {
  claude_dir="$HOME/.claude"
  if [ ! -d "$claude_dir" ]; then add_row 'claude-code' 'not detected'; return 0; fi
  detected=$((detected + 1))
  say ''
  say "Claude Code: $claude_dir"
  claude_ok=1
  ensure_json "$claude_dir/settings.json" mcpServers || claude_ok=0
  ensure_hint "$claude_dir/CLAUDE.md" || failures=$((failures + 1))
  need_claude_skills=1
  finish_client 'claude-code' "$claude_ok"
}

do_claude_desktop() {
  if [ "$(uname -s)" = 'Darwin' ]; then
    desktop_dir="$HOME/Library/Application Support/Claude"
  else
    desktop_dir="$HOME/.config/Claude"
  fi
  if [ ! -d "$desktop_dir" ]; then add_row 'claude-desktop' 'not detected'; return 0; fi
  detected=$((detected + 1))
  say ''
  say "Claude Desktop: $desktop_dir"
  warn 'Claude Desktop MCP config is unofficial; path or format may change'
  desktop_ok=1
  ensure_json "$desktop_dir/claude_desktop_config.json" mcpServers || desktop_ok=0
  say '  Claude Desktop has no skills or AGENTS.md mechanism - config only'
  finish_client 'claude-desktop' "$desktop_ok" 'unofficial'
}

do_cursor() {
  cursor_dir="$HOME/.cursor"
  if [ ! -d "$cursor_dir" ]; then add_row cursor 'not detected'; return 0; fi
  detected=$((detected + 1))
  say ''
  say "Cursor: $cursor_dir"
  cursor_ok=1
  ensure_json "$cursor_dir/mcp.json" mcpServers || cursor_ok=0
  ensure_hint "$cursor_dir/AGENTS.md" || failures=$((failures + 1))
  need_agents_skills=1
  finish_client cursor "$cursor_ok"
}

do_codex() {
  codex_dir="$HOME/.codex"
  if [ ! -d "$codex_dir" ]; then add_row 'codex' 'not detected'; return 0; fi
  detected=$((detected + 1))
  say ''
  say "Codex CLI: $codex_dir"
  codex_cfg="$codex_dir/config.toml"
  toml_block='[mcp_servers.vaults]
command="npx"
args=["-y","vaults-hub"]'
  codex_ok=1
  if [ ! -e "$codex_cfg" ]; then
    if [ "$apply" = 1 ]; then
      mkdir -p "$codex_dir"
      if printf '%s\n' "$toml_block" > "$codex_cfg.tmp" && mv "$codex_cfg.tmp" "$codex_cfg"; then
        say "  created $codex_cfg with [mcp_servers.vaults]"
      else
        rm -f "$codex_cfg.tmp"
        warn "$codex_cfg: write failed - skipping"
        codex_ok=0
      fi
    else
      say "  would register vaults in $codex_cfg"
    fi
  elif [ ! -r "$codex_cfg" ]; then
    warn "$codex_cfg: not readable - skipping"
    codex_ok=0
  elif grep -q '^\[mcp_servers\.vaults\]' "$codex_cfg"; then
    say "  ok: [mcp_servers.vaults] already present in $codex_cfg"
  elif [ "$apply" = 1 ]; then
    if { cat "$codex_cfg"; printf '\n%s\n' "$toml_block"; } > "$codex_cfg.tmp" \
      && mv "$codex_cfg.tmp" "$codex_cfg"; then
      say "  appended [mcp_servers.vaults] to $codex_cfg"
    else
      rm -f "$codex_cfg.tmp"
      warn "$codex_cfg: write failed - skipping"
      codex_ok=0
    fi
  else
    say "  would append [mcp_servers.vaults] to $codex_cfg"
  fi
  ensure_hint "$codex_dir/AGENTS.md" || failures=$((failures + 1))
  need_agents_skills=1
  finish_client codex "$codex_ok"
}

# --- main ------------------------------------------------------------------

main() {
  failures=0
  detected=0
  need_agents_skills=0
  need_claude_skills=0

  if [ "$apply" = 1 ]; then
    say 'vaults-hub installer: APPLY'
  else
    say 'vaults-hub installer: DRY RUN (nothing will be written; pass --apply to write)'
  fi
  say "repo: $REPO_ROOT"

  do_opencode
  do_claude_code
  do_claude_desktop
  do_cursor
  do_codex

  # ~/.agents/skills serves OpenCode, Cursor and Codex; ~/.claude/skills is
  # Claude Code's. Claude Desktop has no skills system.
  if [ "$need_agents_skills" = 1 ]; then
    ensure_link "$HOME/.agents/skills/vaults-sync" || failures=$((failures + 1))
    add_row '~/.agents/skills' "$LINK_STATE"
  fi
  if [ "$need_claude_skills" = 1 ]; then
    ensure_link "$HOME/.claude/skills/vaults-sync" || failures=$((failures + 1))
    add_row '~/.claude/skills' "$LINK_STATE"
  fi

  say ''
  say 'summary:'
  for row in "${rows[@]}"; do
    printf '%s\n' "$row" >&2
  done

  if [ "$detected" = 0 ]; then
    say 'no supported MCP clients detected - nothing to do'
    exit 1
  fi
  if [ "$failures" != 0 ]; then
    say "$failures problem(s); see warnings above"
    exit 1
  fi
  say 'done'
}

main
