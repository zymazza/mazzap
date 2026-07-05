#!/usr/bin/env bash
set -euo pipefail

# Local-chat launcher: serve VEIL with Ollama settings and open the viewer in an
# integrated-GPU Firefox profile so the NVIDIA card stays available for the
# local model. Set VEIL_OPEN_BROWSER=0 to keep the historical "server only"
# behavior.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_VALUE="${HOST:-127.0.0.1}"
PORT_VALUE="${PORT:-4173}"
URL="http://${HOST_VALUE}:${PORT_VALUE}/"
OPEN_BROWSER="${VEIL_OPEN_BROWSER:-1}"

export CHAT_PROVIDER="${CHAT_PROVIDER:-ollama}"
export OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-gpt-oss:20b}"
export OLLAMA_NUM_CTX="${OLLAMA_NUM_CTX:-98304}"
export HOST="$HOST_VALUE"
export PORT="$PORT_VALUE"

# The chat MCP server needs the geospatial stack (pyproj/GDAL/numpy). Point it at
# the project's dedicated venv so the GUI-profile python3 (which lacks the stack)
# is never used. An explicit VEIL_MCP_PYTHON from the environment always wins.
if [[ -z "${VEIL_MCP_PYTHON:-}" && -x "$ROOT/.venv-mcp/bin/python" ]]; then
  export VEIL_MCP_PYTHON="$ROOT/.venv-mcp/bin/python"
fi

if [[ "$OPEN_BROWSER" != "0" ]]; then
  (
    for _ in $(seq 1 80); do
      if curl -fsS "$URL" >/dev/null 2>&1; then
        exec "$ROOT/scripts/open-veil-integrated-firefox.sh" "$URL"
      fi
      sleep 0.25
    done
    echo "Warning: VEIL server was not reachable at $URL; browser not opened" >&2
  ) &
  echo "Local chat will open $URL on integrated graphics (set VEIL_OPEN_BROWSER=0 to disable)."
else
  echo "Local chat browser auto-open disabled; open $URL manually."
fi

echo "Starting VEIL local chat server on $URL with $OLLAMA_MODEL via $OLLAMA_HOST (ctx=$OLLAMA_NUM_CTX)"
exec node "$ROOT/server.js"
