#!/usr/bin/env bash
# Build the frontend if needed, then start Project OS on http://127.0.0.1:8765
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ ! -f frontend/dist/index.html ]; then
  (cd frontend && npm install && npm run build)
fi

cd backend
exec ../.venv/bin/python -m project_os serve "$@"
