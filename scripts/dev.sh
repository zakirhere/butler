#!/usr/bin/env bash
# Run the server locally for development (binds to localhost, auto-reloads).
set -euo pipefail
cd "$(dirname "$0")/.."
uvicorn butler.main:app --reload --host 127.0.0.1 --port "${BUTLER_PORT:-8787}"
