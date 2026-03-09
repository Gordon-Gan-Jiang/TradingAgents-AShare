#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${DEPLOY_ENV_FILE:-$ROOT_DIR/deploy/deploy.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing deploy env: $ENV_FILE"
  echo "Copy deploy/deploy.env.example to deploy/deploy.env and fill it first."
  exit 1
fi

source "$ENV_FILE"

FRONTEND_DIR="${FRONTEND_DIR:-frontend}"
FRONTEND_PATH="$ROOT_DIR/$FRONTEND_DIR"
BACKEND_PUBLIC_URL="${BACKEND_PUBLIC_URL:-}"

if [[ -z "$BACKEND_PUBLIC_URL" ]]; then
  echo "BACKEND_PUBLIC_URL is required. Pass it in the environment or use deploy_all.sh."
  exit 1
fi

echo "[deploy-frontend] rewriting vercel.json target to $BACKEND_PUBLIC_URL"
cp "$FRONTEND_PATH/vercel.json" "$FRONTEND_PATH/vercel.json.bak"
cleanup() {
  if [[ -f "$FRONTEND_PATH/vercel.json.bak" ]]; then
    mv "$FRONTEND_PATH/vercel.json.bak" "$FRONTEND_PATH/vercel.json"
  fi
}
trap cleanup EXIT

python3 - <<PY
import json
from pathlib import Path

path = Path("$FRONTEND_PATH/vercel.json")
data = json.loads(path.read_text())
for rule in data.get("rewrites", []):
    src = rule.get("source", "")
    if src.startswith("/v1/"):
      rule["destination"] = "$BACKEND_PUBLIC_URL/v1/\$1"
    elif src == "/healthz":
        rule["destination"] = "$BACKEND_PUBLIC_URL/healthz"
    elif src == "/openapi.json":
        rule["destination"] = "$BACKEND_PUBLIC_URL/openapi.json"
    elif src == "/docs":
        rule["destination"] = "$BACKEND_PUBLIC_URL/docs"
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
PY

echo "[deploy-frontend] deploying to vercel"
(cd "$FRONTEND_PATH" && npx vercel --prod --yes)

if [[ -n "${FRONTEND_SITE_URL:-}" ]]; then
  echo "[deploy-frontend] verifying $FRONTEND_SITE_URL/healthz"
  verified=0
  for attempt in 1 2 3 4 5 6; do
    if curl -fsSL --max-time 15 "$FRONTEND_SITE_URL/healthz"; then
      echo
      verified=1
      break
    fi
    echo "[deploy-frontend] healthz check failed on attempt $attempt, retrying..." >&2
    sleep 3
  done
  if [[ "$verified" != "1" ]]; then
    echo "[deploy-frontend] failed to verify $FRONTEND_SITE_URL/healthz"
    exit 1
  fi
fi
