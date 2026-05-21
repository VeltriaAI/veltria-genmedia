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
  # IMPORTANT: prompt text MUST go to stderr — these helpers are called via
  # VAR=$(prompt "…"), and command substitution captures stdout. If the
  # question text went to stdout it would be silently swallowed into VAR
  # and the user would just see a blinking cursor with no prompt.
  local question="$1"
  local default="${2:-}"
  local hint=""
  [ -n "$default" ] && hint=" ${DIM}[$default]${RESET}"
  printf "%s?%s %s%s " "$YELLOW" "$RESET" "$question" "$hint" >&2
  local answer
  read -r answer
  echo "${answer:-$default}"
}

prompt_secret() {
  local question="$1"
  printf "%s?%s %s " "$YELLOW" "$RESET" "$question" >&2
  local answer
  read -rs answer
  echo >&2
  echo "$answer"
}

# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight
# ─────────────────────────────────────────────────────────────────────────────
banner

# ─────────────────────────────────────────────────────────────────────────────
# Prerequisite auto-install — handle a fresh laptop with no Homebrew,
# missing jq, system Python that's too old, etc. Skip via GENMEDIA_SKIP_PREREQS=1.
# Runs before the bootstrap clone because the clone needs `git`.
# ─────────────────────────────────────────────────────────────────────────────
detect_os() {
  case "$(uname -s)" in
    Darwin) echo "macos" ;;
    Linux)
      if   command -v apt-get >/dev/null 2>&1; then echo "linux-apt"
      elif command -v dnf     >/dev/null 2>&1; then echo "linux-dnf"
      elif command -v yum     >/dev/null 2>&1; then echo "linux-yum"
      else echo "linux-unknown"
      fi
      ;;
    *) echo "unknown" ;;
  esac
}

_python_satisfies() {
  # $1 = python binary path/name
  command -v "$1" >/dev/null 2>&1 \
    && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null \
    && "$1" -c 'import venv' 2>/dev/null
}

# Resolve PYTHON_BIN — the python3.X binary to use for venv + MCP server.
# Homebrew's python@3.X is keg-only: it installs python3.X (versioned) but
# does NOT touch /opt/homebrew/bin/python3. So `python3` alone often resolves
# to system /usr/bin/python3 (3.9 on macOS) even when a newer brewed Python
# is installed. Probe versioned names AND known brew prefixes.
resolve_python_bin() {
  # Already resolved + still valid?
  [ -n "${PYTHON_BIN:-}" ] && _python_satisfies "$PYTHON_BIN" && return 0

  # 1. plain python3 (good if it's 3.11+)
  if _python_satisfies python3; then
    PYTHON_BIN="$(command -v python3)"
    return 0
  fi
  # 2. versioned names on PATH — try newest first
  for v in 3.15 3.14 3.13 3.12 3.11; do
    if _python_satisfies "python$v"; then
      PYTHON_BIN="$(command -v "python$v")"
      return 0
    fi
  done
  # 3. known brew keg-only locations
  for p in /opt/homebrew/opt/python@3.15/libexec/bin/python3 \
           /opt/homebrew/opt/python@3.14/libexec/bin/python3 \
           /opt/homebrew/opt/python@3.13/libexec/bin/python3 \
           /opt/homebrew/opt/python@3.12/libexec/bin/python3 \
           /opt/homebrew/opt/python@3.11/libexec/bin/python3 \
           /usr/local/opt/python@3.15/libexec/bin/python3 \
           /usr/local/opt/python@3.14/libexec/bin/python3 \
           /usr/local/opt/python@3.13/libexec/bin/python3 \
           /usr/local/opt/python@3.12/libexec/bin/python3 \
           /usr/local/opt/python@3.11/libexec/bin/python3; do
    if [ -x "$p" ] && _python_satisfies "$p"; then
      PYTHON_BIN="$p"
      return 0
    fi
  done
  return 1
}

python_ok() {
  resolve_python_bin
}

install_prereqs_macos() {
  if ! command -v brew >/dev/null 2>&1; then
    say "Homebrew is required to install the missing tools."
    say "It's the standard macOS package manager — takes ~5 minutes and will ask for your Mac password."
    if [ "${HEADLESS:-0}" != "1" ]; then
      printf "%s?%s Install Homebrew now? [Y/n] " "$YELLOW" "$RESET"
      read -r yn </dev/tty || true
      case "$yn" in [Nn]*) err "Cannot continue without Homebrew. Install it manually from https://brew.sh and re-run."; exit 1 ;; esac
    fi
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
      || { err "Homebrew install failed."; exit 1; }
    if   [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -x /usr/local/bin/brew    ]; then eval "$(/usr/local/bin/brew shellenv)"
    fi
  fi

  local brew_pkgs=()
  for m in "$@"; do
    case "$m" in
      curl) ;;                         # ships with macOS
      jq)   brew_pkgs+=(jq) ;;
      git)  brew_pkgs+=(git) ;;
      ffmpeg) brew_pkgs+=(ffmpeg) ;;
      python3) brew_pkgs+=(python@3.12) ;;
    esac
  done

  if [ ${#brew_pkgs[@]} -gt 0 ]; then
    say "Installing via Homebrew: ${brew_pkgs[*]}"
    brew install "${brew_pkgs[@]}" || { err "brew install failed"; exit 1; }
  fi
  # re-probe after brew finished so we pick up the freshly-installed python
  PYTHON_BIN=""
  resolve_python_bin || true
}

install_prereqs_apt() {
  local pkgs=()
  for m in "$@"; do
    case "$m" in
      curl) pkgs+=(curl) ;;
      jq)   pkgs+=(jq) ;;
      git)  pkgs+=(git) ;;
      ffmpeg) pkgs+=(ffmpeg) ;;
      python3) pkgs+=(python3 python3-venv python3-pip) ;;
    esac
  done
  [ ${#pkgs[@]} -eq 0 ] && return 0
  say "Installing via apt-get (sudo required): ${pkgs[*]}"
  sudo apt-get update -qq
  sudo apt-get install -y "${pkgs[@]}" || { err "apt install failed"; exit 1; }
}

install_prereqs_dnf() {
  local pkgs=() pm
  pm=$(command -v dnf || command -v yum)
  for m in "$@"; do
    case "$m" in
      curl) pkgs+=(curl) ;;
      jq)   pkgs+=(jq) ;;
      git)  pkgs+=(git) ;;
      ffmpeg) pkgs+=(ffmpeg) ;;
      python3) pkgs+=(python3 python3-virtualenv python3-pip) ;;
    esac
  done
  [ ${#pkgs[@]} -eq 0 ] && return 0
  say "Installing via $pm (sudo required): ${pkgs[*]}"
  sudo "$pm" install -y "${pkgs[@]}" || { err "$pm install failed"; exit 1; }
}

bootstrap_prereqs() {
  [ "${GENMEDIA_SKIP_PREREQS:-0}" = "1" ] && return 0

  local missing=()
  command -v curl   >/dev/null 2>&1 || missing+=("curl")
  command -v jq     >/dev/null 2>&1 || missing+=("jq")
  command -v git    >/dev/null 2>&1 || missing+=("git")
  command -v ffmpeg >/dev/null 2>&1 || missing+=("ffmpeg")
  python_ok                         || missing+=("python3")

  [ ${#missing[@]} -eq 0 ] && { ok "Prerequisites OK"; return 0; }

  warn "Missing or outdated prerequisites: ${missing[*]}"
  local os; os=$(detect_os)
  case "$os" in
    macos)     install_prereqs_macos "${missing[@]}" ;;
    linux-apt) install_prereqs_apt   "${missing[@]}" ;;
    linux-dnf|linux-yum) install_prereqs_dnf "${missing[@]}" ;;
    *) err "Unsupported OS for automatic prereq install ($os). Install manually: ${missing[*]}"; exit 1 ;;
  esac

  # Re-check after install
  local still_missing=()
  command -v curl   >/dev/null 2>&1 || still_missing+=("curl")
  command -v jq     >/dev/null 2>&1 || still_missing+=("jq")
  command -v git    >/dev/null 2>&1 || still_missing+=("git")
  command -v ffmpeg >/dev/null 2>&1 || still_missing+=("ffmpeg")
  python_ok                         || still_missing+=("python3 (3.11+ with venv)")
  if [ ${#still_missing[@]} -gt 0 ]; then
    err "After install attempt, still missing: ${still_missing[*]}"
    err "Install manually and re-run."
    exit 1
  fi
  ok "Prerequisites installed and verified"
}

# Detect --headless early
HEADLESS=0
[ "${1:-}" = "--headless" ] && HEADLESS=1

# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap — when run via `curl | bash`, bash is reading THIS SCRIPT from
# its own stdin (the curl pipe). We can't do any stdin redirection here
# without bash starting to read commands interactively from the terminal,
# which looks like a hang. So: clone the repo and exec from the on-disk
# copy first. After exec, bash is reading from a real file and everything
# else (including the tty reattach below) is safe.
# ─────────────────────────────────────────────────────────────────────────────
REPO_URL="${GENMEDIA_REPO_URL:-https://github.com/VeltriaAI/veltria-genmedia.git}"
REPO_BRANCH="${GENMEDIA_REPO_BRANCH:-main}"
INSTALL_DIR="${GENMEDIA_INSTALL_DIR:-$HOME/skills/veltria-genmedia}"

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR_RAW="$(cd "$(dirname "$SCRIPT_PATH")" 2>/dev/null && pwd || echo "")"

if [ -z "$SCRIPT_DIR_RAW" ] || [ ! -f "$SCRIPT_DIR_RAW/../mcp/server.py" ]; then
  say "Bootstrap — cloning $REPO_URL → $INSTALL_DIR"
  if ! command -v git >/dev/null 2>&1; then
    err "git not found. Install it first (macOS: \`xcode-select --install\`; Ubuntu: \`sudo apt install git\`), then re-run."
    exit 1
  fi
  if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" fetch --quiet origin "$REPO_BRANCH"
    git -C "$INSTALL_DIR" checkout --quiet "$REPO_BRANCH"
    git -C "$INSTALL_DIR" pull --quiet --ff-only origin "$REPO_BRANCH"
    ok "Repo refreshed at $INSTALL_DIR"
  else
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
    ok "Repo cloned to $INSTALL_DIR"
  fi
  say "Re-executing installer from clone…"
  exec bash "$INSTALL_DIR/scripts/install.sh" "$@"
fi

SCRIPT_DIR="$SCRIPT_DIR_RAW"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
say "Running from: $REPO_ROOT"

# ─────────────────────────────────────────────────────────────────────────────
# Reattach stdin to the controlling terminal — needed because bash inherits
# whatever stdin it was launched with (curl pipe, file, etc.) and the wizard
# wants to read interactively. Safe to do now: bash is reading the script
# from a real file on disk, not from stdin, so redirecting stdin doesn't
# touch the script source. Defensive against CI / sandboxes with no TTY.
# ─────────────────────────────────────────────────────────────────────────────
if [ ! -t 0 ] && [ -c /dev/tty ] && ( exec </dev/tty ) 2>/dev/null; then
  exec </dev/tty
fi

bootstrap_prereqs

PYV=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
ok "Using Python $PYV at $PYTHON_BIN"

# ─────────────────────────────────────────────────────────────────────────────
# Inputs (interactive or headless)  — HEADLESS already set above
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Detect previous answers so re-runs default to "keep what you had"
# ─────────────────────────────────────────────────────────────────────────────
PREV_CFG="$HOME/.config/veltria-genmedia/gateway.env"

# Read a single key=value from a sourced-style env file (no exec, just grep+cut).
get_cfg() {
  local key="$1" file="${2:-$PREV_CFG}"
  [ -f "$file" ] || return 0
  grep -E "^${key}=" "$file" 2>/dev/null | head -1 | cut -d= -f2- || true
}

prev_url=$(get_cfg GATEWAY_BASE_URL)
prev_image_dir=$(get_cfg DEFAULT_IMAGE_OUTPUT_DIR)
prev_video_dir=$(get_cfg DEFAULT_VIDEO_OUTPUT_DIR)
prev_brand=$(get_cfg BRAND_PRESET)
prev_image_model=$(get_cfg DEFAULT_IMAGE_MODEL)
prev_video_model=$(get_cfg DEFAULT_VIDEO_MODEL)
prev_text_model=$(get_cfg DEFAULT_TEXT_MODEL)
prev_key_set=0
[ -n "$(get_cfg GATEWAY_API_KEY)" ] && prev_key_set=1

# Figure out which Claude clients already have us wired so we default to the
# same answer next time. (Desktop has a different config dir on macOS vs Linux.)
DESKTOP_CFG_MAC="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
DESKTOP_CFG_LIN="$HOME/.config/Claude/claude_desktop_config.json"
CODE_CFG="$HOME/.claude.json"
prev_desktop_wired=0
prev_code_wired=0
for c in "$DESKTOP_CFG_MAC" "$DESKTOP_CFG_LIN"; do
  [ -f "$c" ] && jq -e '.mcpServers."veltria-genmedia"' "$c" >/dev/null 2>&1 && prev_desktop_wired=1
done
[ -f "$CODE_CFG" ] && jq -e '.mcpServers."veltria-genmedia"' "$CODE_CFG" >/dev/null 2>&1 && prev_code_wired=1
if   [ "$prev_desktop_wired" = "1" ] && [ "$prev_code_wired" = "1" ]; then prev_client="both"
elif [ "$prev_desktop_wired" = "1" ]; then prev_client="desktop"
elif [ "$prev_code_wired"    = "1" ]; then prev_client="code"
else prev_client="both"
fi

# Per-field defaults (cli env > previous config > shipped default).
def_url="${GENMEDIA_GATEWAY_URL:-$prev_url}"
def_client="${GENMEDIA_CLIENT:-$prev_client}"
def_image_dir="${GENMEDIA_IMAGE_DIR:-${prev_image_dir:-$HOME/Pictures/genmedia}}"
def_video_dir="${GENMEDIA_VIDEO_DIR:-${prev_video_dir:-$HOME/Movies/genmedia}}"
def_brand="${GENMEDIA_BRAND_PRESET:-$prev_brand}"
# Default model names — empty unless caller / previous config provides one.
# Wizard prompts for each so a fresh install isn't stuck with blank defaults.
def_image_model="${GENMEDIA_DEFAULT_IMAGE_MODEL:-$prev_image_model}"
def_video_model="${GENMEDIA_DEFAULT_VIDEO_MODEL:-$prev_video_model}"
def_text_model="${GENMEDIA_DEFAULT_TEXT_MODEL:-$prev_text_model}"

if [ "$HEADLESS" = "1" ]; then
  GATEWAY_URL="$def_url"
  API_KEY="${GENMEDIA_API_KEY:-$(get_cfg GATEWAY_API_KEY)}"
  CLIENT="$def_client"
  IMAGE_DIR="$def_image_dir"
  VIDEO_DIR="$def_video_dir"
  BRAND_PRESET="$def_brand"
  IMAGE_MODEL="$def_image_model"
  VIDEO_MODEL="$def_video_model"
  TEXT_MODEL="$def_text_model"
  if [ -z "$GATEWAY_URL" ] || [ -z "$API_KEY" ]; then
    err "--headless requires GENMEDIA_GATEWAY_URL and GENMEDIA_API_KEY (or a previous config to reuse)"
    exit 1
  fi
else
  echo
  if [ -f "$PREV_CFG" ]; then
    say "Found existing config at $PREV_CFG — re-using previous answers as defaults."
    say "Press Enter to keep, or type a new value to override."
  else
    say "I'll ask a few questions. Defaults in brackets — press Enter to accept."
  fi
  echo

  GATEWAY_URL=$(prompt "Gateway base URL (e.g. https://your-gateway.example.com)" "$def_url")
  if [ -z "$GATEWAY_URL" ]; then
    err "Gateway URL is required. Get it from your gateway admin and re-run."
    exit 1
  fi

  if [ "$prev_key_set" = "1" ]; then
    API_KEY=$(prompt_secret "Virtual API key (Enter to keep existing, paste new to rotate):")
    [ -z "$API_KEY" ] && API_KEY=$(get_cfg GATEWAY_API_KEY)
  else
    API_KEY=$(prompt_secret "Virtual API key (will not echo):")
  fi
  if [ -z "$API_KEY" ]; then
    err "No key. Get one from your gateway admin and re-run."
    exit 1
  fi

  echo
  CLIENT=$(prompt "Wire which Claude client? (desktop / code / both)" "$def_client")
  case "$CLIENT" in
    desktop|code|both) ;;
    *) err "Unknown client '$CLIENT'. Must be desktop / code / both."; exit 1 ;;
  esac

  echo
  IMAGE_DIR=$(prompt "Save generated images to" "$def_image_dir")
  VIDEO_DIR=$(prompt "Save generated videos to" "$def_video_dir")

  # Default model names — saved to gateway.env so callers don't need to pass
  # `model=...` on every request. Sensible suggestions for InfraX-style
  # LiteLLM setups; users with different gateway names can override.
  echo
  say "Default model names per category (Enter to use the suggestion if blank):"
  IMAGE_MODEL=$(prompt "Default IMAGE model" "${def_image_model:-nano-banana}")
  VIDEO_MODEL=$(prompt "Default VIDEO model" "${def_video_model:-veo-2}")
  TEXT_MODEL=$(prompt "Default TEXT  model" "${def_text_model:-gemini-flash}")

  echo
  BRAND_PRESET=$(prompt "Brand preset (free-form, prepended to every image prompt — Enter to skip)" "$def_brand")
fi

# Normalize gateway URL — strip trailing slash
GATEWAY_URL="${GATEWAY_URL%/}"

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — install the Python MCP server deps into an isolated venv
# (system-Python `pip install --user` is blocked on PEP-668 environments,
# i.e. modern macOS Homebrew Python 3.12+ and Ubuntu 24.04+. A repo-local
# venv side-steps that and keeps deps out of the user's system Python.)
# ─────────────────────────────────────────────────────────────────────────────
echo
say "Step 1/5 — Installing MCP server (Python deps into local venv)…"

VENV_DIR="$REPO_ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"

if [ ! -x "$VENV_PY" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR" || {
    err "Failed to create venv at $VENV_DIR."
    err "On Debian/Ubuntu you may need:  sudo apt install python3-venv"
    exit 1
  }
fi

say "    (downloading httpx + mcp + deps — usually 30-60s on first run)"
"$VENV_PY" -m pip install --disable-pip-version-check --upgrade --quiet pip || true
# Run pip *without* piping to tail so each line of progress streams live —
# otherwise the user sees a blank terminal for 30-60s and thinks it's stuck.
if ! "$VENV_PY" -m pip install --disable-pip-version-check --progress-bar off --upgrade httpx 'mcp>=1.2.0'; then
  err "Failed to install httpx + mcp into $VENV_DIR."
  err "Try manually:  $VENV_PY -m pip install httpx mcp"
  exit 1
fi
"$VENV_PY" -c "import httpx, mcp" 2>&1 || {
  err "Post-install import check failed — httpx/mcp not importable from $VENV_PY"
  exit 1
}
ok "MCP server deps installed in $VENV_DIR"

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

DEFAULT_IMAGE_MODEL=$IMAGE_MODEL
DEFAULT_VIDEO_MODEL=$VIDEO_MODEL
DEFAULT_TEXT_MODEL=$TEXT_MODEL

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
  new=$(jq --arg py "$VENV_PY" --arg dir "$MCP_DIR" '
    .mcpServers = ((.mcpServers // {}) + {
      "veltria-genmedia": {
        "command": $py,
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
  new=$(jq --arg py "$VENV_PY" --arg dir "$MCP_DIR" '
    .mcpServers = ((.mcpServers // {}) + {
      "veltria-genmedia": {
        "type": "stdio",
        "command": $py,
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
