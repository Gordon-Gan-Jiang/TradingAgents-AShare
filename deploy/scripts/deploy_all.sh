#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "[deploy-all] deploying remote backend"
BACKEND_PUBLIC_URL="$("$ROOT_DIR/deploy/scripts/deploy_remote.sh" | tail -n 1)"
export BACKEND_PUBLIC_URL

echo "[deploy-all] deploying frontend with backend $BACKEND_PUBLIC_URL"
"$ROOT_DIR/deploy/scripts/deploy_frontend.sh"

echo "[deploy-all] done"
echo "backend:  $BACKEND_PUBLIC_URL"
