#!/usr/bin/env bash
#
# start-deepseek-proxy.sh
#
# Launcher for the DeepSeek Cursor proxy (1M context fork) on macOS/Linux.
# Starts the proxy (which starts the ngrok tunnel) and prints the public Base URL
# that must be pasted into Cursor -> Settings -> Models -> API Keys.
#
# Keep this terminal open while you work in Cursor.
# Press Ctrl+C to stop the proxy.
#
# Usage:
#   ./start-deepseek-proxy.sh [PROXY_DIR]

set -uo pipefail

PROXY_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
LOG_FILE="$PROXY_DIR/.proxy-stdout.log"
ERR_FILE="$PROXY_DIR/.proxy-stderr.log"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-90}"

NGROK_API_URLS=("http://127.0.0.1:4040/api/endpoints" "http://127.0.0.1:4040/api/tunnels")
LOCAL_HEALTH="http://127.0.0.1:9000/v1/models"

if [ ! -d "$PROXY_DIR" ]; then
    echo "Proxy folder not found: $PROXY_DIR" >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Install it: https://astral.sh/uv" >&2
    exit 1
fi

cd "$PROXY_DIR" || exit 1

# --- Helpers -------------------------------------------------------------

get_tunnel_url() {
    local api raw url
    for api in "${NGROK_API_URLS[@]}"; do
        raw="$(curl -s --max-time 4 "$api" 2>/dev/null || true)"
        [ -z "$raw" ] && continue
        url="$(printf '%s' "$raw" | grep -o 'https://[^"\\]*' | head -n1 || true)"
        if [ -n "$url" ]; then
            printf '%s/v1' "${url%/}"
            return 0
        fi
    done
    return 1
}

get_url_from_log() {
    local url
    [ -f "$LOG_FILE" ] || return 1
    url="$(grep -o 'api_base_url:[[:space:]]*[^[:space:]]*' "$LOG_FILE" \
        | tail -n1 | awk '{print $2}' || true)"
    [ -n "$url" ] && printf '%s' "$url"
}

proxy_is_up() {
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "$LOCAL_HEALTH" 2>/dev/null || true)"
    [ "$code" = "200" ]
}

show_banner() {
    local url="$1"
    printf '\n'
    printf '  ================================================================\n'
    printf '   DeepSeek proxy is READY\n'
    printf '  ================================================================\n\n'
    printf '   Cursor Base URL:\n\n'
    printf '   %s\n\n' "$url"
    printf '   Paste into: Settings -> Models -> API Keys -> Override OpenAI Base URL\n'
    printf '   For 1M context: select GPT-5.6 Sol (or your deepseek-flash)\n'
    printf '   Model names:   GPT-5.6 Sol  |  deepseek-flash\n'
    printf '   Toggle custom API:  Cmd+Shift+0 (macOS) / Ctrl+Shift+0 (Linux)\n\n'
    printf '   ------------------------------------------------------------\n'
    printf '   Keep this terminal open while working in Cursor.\n'
    printf '   Press Ctrl+C to stop the proxy.\n'
    printf '   ------------------------------------------------------------\n\n'
}

# --- Main ----------------------------------------------------------------

if proxy_is_up; then
    url="$(get_tunnel_url || true)"
    [ -z "$url" ] && url="$(get_url_from_log || true)"
    if [ -n "$url" ]; then
        show_banner "$url"
    else
        echo "Proxy is already running but no tunnel URL was found."
    fi
    exit 0
fi

export PYTHONUNBUFFERED=1
export UV_NO_PROGRESS=1

rm -f "$LOG_FILE" "$ERR_FILE"

# Stop a leftover ngrok agent so port 4040 is free.
pkill -f 'ngrok http' 2>/dev/null || true

echo "Starting DeepSeek proxy + ngrok tunnel..."

args=(run deepseek-cursor-proxy)
if [ -n "${DCP_NGROK_URL:-}" ]; then
    args+=(--ngrok-url "$DCP_NGROK_URL")
fi

uv "${args[@]}" >"$LOG_FILE" 2>"$ERR_FILE" &
proxy_pid=$!

cleanup() {
    printf '\nStopping proxy...\n'
    kill "$proxy_pid" 2>/dev/null || true
    pkill -f 'ngrok http' 2>/dev/null || true
}
trap cleanup EXIT INT TERM

url=""
deadline=$((SECONDS + TIMEOUT_SECONDS))
while [ "$SECONDS" -lt "$deadline" ]; do
    url="$(get_url_from_log || true)"
    [ -z "$url" ] && url="$(get_tunnel_url || true)"
    if [ -n "$url" ]; then
        show_banner "$url"
        break
    fi
    if ! kill -0 "$proxy_pid" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

if [ -z "$url" ]; then
    echo "Could not obtain the tunnel URL. Last log lines:" >&2
    tail -n 15 "$ERR_FILE" 2>/dev/null >&2 || true
    tail -n 15 "$LOG_FILE" 2>/dev/null >&2 || true
    exit 1
fi

wait "$proxy_pid"
