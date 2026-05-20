#!/usr/bin/env bash
#
# verify.sh — health-check the gateway + the user's virtual key
#
# Usage:
#   ./scripts/verify.sh                 # basic ping + models
#   ./scripts/verify.sh --image         # also do a real image gen test (uses budget!)

set -uo pipefail

GREEN=$'\033[1;32m'; RED=$'\033[1;31m'; YELLOW=$'\033[1;33m'; DIM=$'\033[2m'; RESET=$'\033[0m'
ok()   { printf "%s[verify]%s %s\n" "$GREEN" "$RESET" "$*"; }
fail() { printf "%s[verify]%s %s\n" "$RED" "$RESET" "$*" >&2; }
warn() { printf "%s[verify]%s %s\n" "$YELLOW" "$RESET" "$*"; }

CFG="$HOME/.config/veltria-genmedia/gateway.env"
if [ ! -f "$CFG" ]; then
  fail "Config not found at $CFG. Run scripts/install.sh first."
  exit 1
fi

# Source the env file safely
set -a
# shellcheck disable=SC1090
. "$CFG"
set +a

: "${GATEWAY_BASE_URL:?missing in $CFG}"
: "${GATEWAY_API_KEY:?missing in $CFG}"
: "${AUTH_HEADER:=x-api-key}"

ok "Config loaded: $CFG"
ok "Gateway: $GATEWAY_BASE_URL"

# 1. DNS / TCP reachable
if ! curl -sk --connect-timeout 5 --max-time 10 "$GATEWAY_BASE_URL" -o /dev/null; then
  fail "Cannot reach $GATEWAY_BASE_URL (DNS, network, or unresponsive server)"
  exit 1
fi
ok "Gateway reachable"

# 2. Auth check via /v1/models
# Lowercase via tr so we work on macOS bash 3.2 (no ${var,,} support).
AUTH_HEADER_LOWER=$(printf '%s' "$AUTH_HEADER" | tr '[:upper:]' '[:lower:]')
case "$AUTH_HEADER_LOWER" in
  authorization)
    AUTH_ARGS=(-H "Authorization: Bearer ${GATEWAY_API_KEY}")
    ;;
  *)
    AUTH_ARGS=(-H "${AUTH_HEADER}: ${GATEWAY_API_KEY}")
    ;;
esac

# --max-time so a wedged gateway can't hang the wizard forever.
http_code=$(curl -sk -o /tmp/genmedia-verify.out -w "%{http_code}" \
  --connect-timeout 10 --max-time 30 \
  "${AUTH_ARGS[@]}" \
  "${GATEWAY_BASE_URL%/}/v1/models" || echo "000")

case "$http_code" in
  200)
    n_models=$(jq -r '.data | length' < /tmp/genmedia-verify.out 2>/dev/null || echo "?")
    ok "Auth works · $n_models models available"
    if command -v jq >/dev/null 2>&1; then
      printf "${DIM}    Models:${RESET} %s\n" "$(jq -r '.data[].id' < /tmp/genmedia-verify.out 2>/dev/null | tr '\n' ' ')"
    fi
    ;;
  401|403)
    fail "Auth failed (HTTP $http_code) — virtual key invalid or revoked."
    fail "Get a fresh key from your gateway admin and re-run install.sh."
    exit 1
    ;;
  000)
    fail "Connection failed entirely. Check network."
    exit 1
    ;;
  *)
    fail "Unexpected response (HTTP $http_code). Body:"
    head -20 /tmp/genmedia-verify.out >&2
    exit 1
    ;;
esac

# 3. Optional: real image gen test
if [ "${1:-}" = "--image" ]; then
  warn "Running real image gen test — this consumes budget"
  if [ -z "${DEFAULT_IMAGE_MODEL:-}" ]; then
    fail "DEFAULT_IMAGE_MODEL not set in gateway.env — set it or pass a model explicitly."
    exit 1
  fi
  payload=$(jq -nc --arg m "$DEFAULT_IMAGE_MODEL" '{model:$m,prompt:"a simple smiley face on white background",n:1,response_format:"b64_json"}')
  http_code=$(curl -sk -o /tmp/genmedia-verify-img.out -w "%{http_code}" \
    --connect-timeout 10 --max-time 120 \
    "${AUTH_ARGS[@]}" \
    -H "Content-Type: application/json" \
    -X POST \
    -d "$payload" \
    "${GATEWAY_BASE_URL%/}/v1/images/generations" || echo "000")
  case "$http_code" in
    200)
      size=$(wc -c < /tmp/genmedia-verify-img.out)
      ok "Image gen test passed (response $size bytes)"
      ;;
    *)
      fail "Image gen test failed (HTTP $http_code)"
      head -50 /tmp/genmedia-verify-img.out >&2
      exit 1
      ;;
  esac
fi

ok "All checks passed."
