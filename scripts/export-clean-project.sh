#!/usr/bin/env bash
# Export a clean, runnable copy of AlphaPilot-A-Share without local DB, caches, or secrets.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FORCE=0
DEST=""

for arg in "$@"; do
  case "$arg" in
    -f|--force) FORCE=1 ;;
    -h|--help)
      echo "Usage: $0 [--force] [DEST_DIR]"
      echo "  DEST_DIR  default: ../AlphaPilot-A-Share-clean"
      echo "  --force   remove existing destination before export"
      exit 0
      ;;
    *)
      if [[ -z "$DEST" ]]; then
        DEST="$arg"
      fi
      ;;
  esac
done

DEST="${DEST:-${ROOT%/AlphaPilot-A-Share}/AlphaPilot-A-Share-clean}"
DEST="${DEST/#\~/$HOME}"

if [[ -e "$DEST" ]]; then
  if [[ "$FORCE" -eq 1 ]]; then
    echo "Removing existing destination: $DEST"
    rm -rf "$DEST"
  elif [[ -n "$(ls -A "$DEST" 2>/dev/null || true)" ]]; then
    echo "Destination already exists and is not empty: $DEST"
    echo "Use --force to overwrite: $0 --force \"$DEST\""
    exit 1
  fi
fi

mkdir -p "$DEST"

rsync -a \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='.worktrees/' \
  --exclude='frontend/node_modules/' \
  --exclude='frontend/dist/' \
  --exclude='.vite/' \
  --exclude='__pycache__/' \
  --exclude='*.py[cod]' \
  --exclude='.pytest_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='.mypy_cache/' \
  --exclude='htmlcov/' \
  --exclude='.coverage' \
  --exclude='*.egg-info/' \
  --exclude='.idea/' \
  --exclude='.vscode/' \
  --exclude='.DS_Store' \
  --exclude='*.log' \
  --exclude='tradingagents.db' \
  --exclude='tradingagents.db-*' \
  --exclude='graph_checkpoints.db*' \
  --exclude='db.sqlite3*' \
  --exclude='**/data_cache/' \
  --exclude='eval_results/' \
  --exclude='reports/' \
  --exclude='results/' \
  --exclude='deploy/' \
  --exclude='docs/' \
  --exclude='gap_analysis.html' \
  --exclude='.env' \
  --exclude='.env.*.local' \
  --exclude='.env copy.example' \
  --exclude='deploy.env' \
  --exclude='.vercel/' \
  --exclude='.playwright-cli/' \
  --exclude='uv.lock.cp313' \
  "$ROOT/" "$DEST/"

# Sanitized environment template (no real keys / endpoint IDs)
cat > "$DEST/.env.example" <<'EOF'
# --- Core LLM (OpenAI-compatible) ---
TA_API_KEY=your-api-key-here
TA_BASE_URL=https://api.openai.com/v1
TA_LLM_PROVIDER=openai
TA_LLM_QUICK=gpt-4o-mini
TA_LLM_DEEP=gpt-4o
# TA_LLM_TEMPERATURE=0

# --- Security (required in production) ---
# openssl rand -base64 32
# TA_APP_SECRET_KEY=

# --- Optional ---
# DATABASE_URL=sqlite:///./data/tradingagents.db
# TA_LANGUAGE=zh
# TA_TRACE=1
# TA_MAX_DEBATE=1
# TA_MAX_RISK=1

# --- VLM (portfolio screenshot import) ---
# TA_VLM_API_KEY=
# TA_VLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
# TA_VLM_MODEL=glm-4.6v-flash

# --- Email OTP login ---
# MAIL_HOST=smtp.example.com
# MAIL_PORT=465
# MAIL_USER=
# MAIL_PASS=
# MAIL_FROM=AlphaPilot
# MAIL_SSL=1

# FINSKILLS_ROOT=/path/to/finskills
# STOCK_ANALYSIS_TEAM_ROOT=/path/to/stock-analysis-team
EOF

cat > "$DEST/SETUP.md" <<'EOF'
# Clean export — first-time setup

This folder is a **sanitized copy** of AlphaPilot A-Share: no local SQLite DB, no `node_modules`, no `.venv`, no `.env` secrets.

## Quick start

```bash
# 1. Environment
cp .env.example .env
# Edit .env with your LLM API key and base URL

# 2. Backend
uv sync
mkdir -p data
export DATABASE_URL="sqlite:///./data/tradingagents.db"
uv run python -m uvicorn api.main:app --host 0.0.0.0 --port 8000

# 3. Frontend (another terminal)
cd frontend && nvm use && npm install && npm run dev
```

Open http://localhost:5173 (frontend) and http://localhost:8000 (API).

The database file is created on first API start under `./data/` when `DATABASE_URL` points there.

See [README.md](README.md) for full configuration.
EOF

echo "Clean export written to: $DEST"
echo "Next: cd $DEST && cp .env.example .env && uv sync"
