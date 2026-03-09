#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/box/tradingagents-ashare}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$APP_DIR"

if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools
python -m pip install -r requirements.txt

if [ ! -f ".env.production" ]; then
  cp .env.example .env.production
fi

python -m py_compile api/main.py
echo "Bootstrap complete: $APP_DIR"
