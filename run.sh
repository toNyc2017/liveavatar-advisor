#!/usr/bin/env bash
# Convenience launcher. Creates the venv on first run, installs deps, starts uvicorn.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    echo "→ creating venv"
    python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "→ starting on http://localhost:8000"
exec uvicorn advisor_backend:app --host 0.0.0.0 --port 8000 --reload
