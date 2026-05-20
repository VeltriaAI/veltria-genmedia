#!/usr/bin/env bash
#
# veltria-genmedia — install wizard
#
# Interactive: prompts for gateway URL + virtual API key + chosen Claude
# client(s), then sets up the MCP server, configures Claude Desktop /
# Claude Code, and verifies end-to-end against the gateway.
#
# Usage:
#   ./scripts/install.sh             # interactive
#   ./scripts/install.sh --headless  # uses env vars, no prompts (CI/automation)
#
# Env vars (for --headless or override):
#   GENMEDIA_GATEWAY_URL  — base URL of your OpenAI-compatible gateway
#   GENMEDIA_API_KEY      — virtual key issued by the gateway admin
#   GENMEDIA_CLIENT       — desktop | code | both
#   GENMEDIA_IMAGE_DIR    — image output dir
#   GENMEDIA_VIDEO_DIR    — video output dir
#   GENMEDIA_BRAND_PRESET — optional brand-cue string prepended to image prompts

set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Cosmetic helpers
# ─────────────────────────────────────────────────────────────────────────────
BOLD=$'\033[1m'; DIM=$'\033[2m'; BLUE=$'\033[1;34m'; GREEN=$'\033[1;32m'
YELLOW=$'\033[1;33m'; RED=$'\033[1;31m'; RESET=$'\033[0m'

say()  { printf "%s[veltria-genmedia]%s %s\n" "$BLUE"  "$RESET" "$*"; }
ok()   { printf "%s[veltria-genmedia]%s %s\n" "$GREEN" "$RESET" "$*"; }
warn() { printf "%s[veltria-genmedia]%s %s\n" "$YELLOW" "$RESET" "$*"; }
err()  { printf "%s[veltria-genmedia]%s %s\n" "$RED"   "$RESET" "$*" >&2; }

banner() {
  printf "\n%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" "$BLUE" "$RESET"
  printf "%s  veltria-genmedia · install wizard%s\n" "$BOLD" "$RESET"
  printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n\n" "$BLUE" "$RESET"
}

prompt() {
  local question="$1"
  local default="${2:-}"
  local hint=""
  [ -n "$default" ] && hint=" ${DIM}[$default]${RESET}"
  printf "%s?%s %s%s " "$YELLOW" "$RESET" "$question" "$hint"
  local answer
  read -r answer
  echo "${answer:-$default}"
}

prompt_secret() {
  local question="$1"
  printf "%s?%s %s " "$YELLOW" "$RESET" "$question"
  local answer
  read -rs answer
  echo
  echo "$answer"
}

# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight
# ─────────────────────────────────────────────────────────────────────────────
banner

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
say "Running from: $REPO_ROOT"

# Prereqs
say "Checking prerequisites…"
missing=()
for cmd in curl jq python3; do
  command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
if [ ${#missing[@]} -gt 0 ]; then
  err "Missing prerequisites: ${missing[*]}"
  err "Install with your OS package manager (brew on macOS, apt/yum on Linux)"
  exit 1
fi

PYV=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 11)))')
if [ "$PY_OK" != "1" ]; then
  err "Python ${PYV} found. Need Python 3.11+."
  exit 1
fi
ok "Prereqs OK (Python $PYV)"

# ─────────────────────────────────────────────────────────────────────────────
# Inputs (interactive or headless)
# ─────────────────────────────────────────────────────────────────────────────
HEADLESS=0
[ "${1:-}" = "--headless" ] && HEADLESS=1

if [ "$HEADLESS" = "1" ]; then
  GATEWAY_URL="${GENMEDIA_GATEWAY_URL:-}"
  API_KEY="${GENMEDIA_API_KEY:-}"
  CLIENT="${GENMEDIA_CLIENT:-both}"
  IMAGE_DIR="${GENMEDIA_IMAGE_DIR:-$HOME/Pictures/genmedia}"
  VIDEO_DIR="${GENMEDIA_VIDEO_DIR:-$HOME/Movies/genmedia}"
  BRAND_PRESET="${GENMEDIA_BRAND_PRESET:-}"
  if [ -z "$GATEWAY_URL" ] || [ -z "$API_KEY" ]; then
    err "--headless requires GENMEDIA_GATEWAY_URL and GENMEDIA_API_KEY env vars"
    exit 1
  fi
else
  echo
  say "I'll ask a few questions. Defaults in brackets — press Enter to accept."
  echo

  GATEWAY_URL=$(prompt "Gateway base URL (e.g. https://your-gateway.example.com)" "")
  if [ -z "$GATEWAY_URL" ]; then
    err "Gateway URL is required. Get it from your gateway admin and re-run."
    exit 1
  fi

  API_KEY=$(prompt_secret "Virtual API key (will not echo):")
  if [ -z "$API_KEY" ]; then
    err "No key entered. Get one from your gateway admin and re-run."
    exit 1
  fi

  echo
  CLIENT=$(prompt "Wire which Claude client? (desktop / code / both)" "both")
  case "$CLIENT" in
    desktop|code|both) ;;
    *) err "Unknown client '$CLIENT'. Must be desktop / code / both."; exit 1 ;;
  esac

  echo
  IMAGE_DIR=$(prompt "Save generated images to" "$HOME/Pictures/genmedia")
  VIDEO_DIR=$(prompt "Save generated videos to" "$HOME/Movies/genmedia")

  echo
  BRAND_PRESET=$(prompt "Brand preset (free-form, prepended to every image prompt — Enter to skip)" "")
fi

# Normalize gateway URL — strip trailing slash
GATEWAY_URL="${GATEWAY_URL%/}"

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — install the Python MCP server deps
# ─────────────────────────────────────────────────────────────────────────────
echo
say "Step 1/5 — Installing MCP server (Python deps)…"

if ! python3 -c "import httpx, mcp" >/dev/null 2>&1; then
  python3 -m pip install --user --quiet --upgrade httpx 'mcp>=1.2.0' 2>&1 | tail -5 || {
    err "Failed to install httpx + mcp. Try manually:  python3 -m pip install --user httpx mcp"
    exit 1
  }
fi
ok "MCP server deps installed"

chmod +x "$REPO_ROOT/mcp/server.py"

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — write ~/.config/veltria-genmedia/gateway.env
# ─────────────────────────────────────────────────────────────────────────────
echo
say "Step 2/5 — Writing config to ~/.config/veltria-genmedia/gateway.env…"

CONFIG_DIR="$HOME/.config/veltria-genmedia"
CONFIG_FILE="$CONFIG_DIR/gateway.env"
mkdir -p "$CONFIG_DIR"

if [ -f "$CONFIG_FILE" ]; then
  cp "$CONFIG_FILE" "$CONFIG_FILE.backup-$(date +%Y%m%d-%H%M%S)"
  warn "Existing config backed up."
fi

cat > "$CONFIG_FILE" <<EOF
# veltria-genmedia config — generated by install.sh on $(date)
# Rotate the key by overwriting GATEWAY_API_KEY. MCP server picks it up on next call.

GATEWAY_BASE_URL=$GATEWAY_URL
GATEWAY_API_KEY=$API_KEY
AUTH_HEADER=x-api-key

DEFAULT_IMAGE_MODEL=
DEFAULT_VIDEO_MODEL=
DEFAULT_TEXT_MODEL=

DEFAULT_IMAGE_OUTPUT_DIR=$IMAGE_DIR
DEFAULT_VIDEO_OUTPUT_DIR=$VIDEO_DIR

BRAND_PRESET=$BRAND_PRESET

IMAGE_TIMEOUT=120
VIDEO_TIMEOUT=600
MAX_RETRIES=2
EOF
chmod 600 "$CONFIG_FILE"

mkdir -p "$IMAGE_DIR" "$VIDEO_DIR"
ok "Config written (chmod 600, output dirs created)"

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — wire Claude client(s)
# ─────────────────────────────────────────────────────────────────────────────
echo
say "Step 3/5 — Wiring Claude client(s) [$CLIENT]…"

MCP_DIR="$REPO_ROOT/mcp"

wire_claude_desktop() {
  local CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
  if [ ! -d "$(dirname "$CFG")" ]; then
    # Linux Claude Desktop path (if/when supported)
    CFG="$HOME/.config/Claude/claude_desktop_config.json"
  fi
  mkdir -p "$(dirname "$CFG")"
  [ -f "$CFG" ] || echo '{}' > "$CFG"
  cp "$CFG" "$CFG.backup-$(date +%Y%m%d-%H%M%S)"
  local new
  new=$(jq --arg dir "$MCP_DIR" '
    .mcpServers = ((.mcpServers // {}) + {
      "veltria-genmedia": {
        "command": "python3",
        "args": [($dir + "/server.py")]
      }
    })
  ' "$CFG")
  echo "$new" > "$CFG"
  ok "Claude Desktop config updated: $CFG"
}

wire_claude_code() {
  local CFG="$HOME/.claude.json"
  [ -f "$CFG" ] || echo '{}' > "$CFG"
  cp "$CFG" "$CFG.backup-$(date +%Y%m%d-%H%M%S)"
  local new
  new=$(jq --arg dir "$MCP_DIR" '
    .mcpServers = ((.mcpServers // {}) + {
      "veltria-genmedia": {
        "type": "stdio",
        "command": "python3",
        "args": [($dir + "/server.py")]
      }
    })
  ' "$CFG")
  echo "$new" > "$CFG"
  ok "Claude Code config updated: $CFG"
}

case "$CLIENT" in
  desktop) wire_claude_desktop ;;
  code)    wire_claude_code ;;
  both)    wire_claude_desktop; wire_claude_code ;;
esac

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — verify gateway connectivity
# ─────────────────────────────────────────────────────────────────────────────
echo
say "Step 4/5 — Verifying gateway connectivity…"

if "$SCRIPT_DIR/verify.sh"; then
  ok "Gateway reachable and key works"
else
  err "Gateway verification failed. Check URL + key + network and re-run."
  exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — done
# ─────────────────────────────────────────────────────────────────────────────
echo
ok "Install complete."
echo
printf "%sNext steps:%s\n" "$BOLD" "$RESET"
case "$CLIENT" in
  desktop|both)
    printf "  • Quit Claude Desktop fully (Cmd+Q) then reopen\n"
    ;;
esac
case "$CLIENT" in
  code|both)
    printf "  • Restart Claude Code (or open a new session)\n"
    ;;
esac
echo
printf "  • New chat → ask:  ${BOLD}Generate a 1:1 image of a forest cabin at sunrise${RESET}\n"
printf "  • Output will land in: ${DIM}$IMAGE_DIR${RESET}\n"
echo
printf "%sNeed help?%s  See ${DIM}docs/TROUBLESHOOTING.md${RESET}\n" "$BOLD" "$RESET"
