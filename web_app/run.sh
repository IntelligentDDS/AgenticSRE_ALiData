#!/bin/bash
# AgenticSRE Web Dashboard Launcher
set -e

cd "$(dirname "$0")/.."
export PYTHONPATH="$(pwd):$PYTHONPATH"

echo "🚀 Starting AgenticSRE Dashboard on port 8080..."
python -m uvicorn web_app.app:app --host 0.0.0.0 --port 8080 --reload
