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

: "${REMOTE_HOST:?REMOTE_HOST is required}"
: "${REMOTE_USER:?REMOTE_USER is required}"
: "${REMOTE_APP_DIR:?REMOTE_APP_DIR is required}"
: "${REMOTE_API_SERVICE:?REMOTE_API_SERVICE is required}"
: "${REMOTE_TUNNEL_SERVICE:?REMOTE_TUNNEL_SERVICE is required}"

REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-$REMOTE_APP_DIR/.env.production}"
TARBALL="/tmp/tradingagents-ashare-deploy.tgz"

SSH_OPTS=(-p "$REMOTE_PORT" -o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no)
SCP_OPTS=(-P "$REMOTE_PORT" -o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no)

run_ssh() {
  local attempt
  for attempt in 1 2 3; do
    if [[ -n "${REMOTE_PASSWORD:-}" ]]; then
      if SSHPASS="$REMOTE_PASSWORD" sshpass -e ssh -T "${SSH_OPTS[@]}" "$REMOTE_USER@$REMOTE_HOST" "$@"; then
        return 0
      fi
    else
      if ssh -T "${SSH_OPTS[@]}" "$REMOTE_USER@$REMOTE_HOST" "$@"; then
        return 0
      fi
    fi
    if [[ "$attempt" -lt 3 ]]; then
      echo "[deploy-remote] ssh attempt $attempt failed, retrying..." >&2
      sleep 2
    fi
  done
  return 1
}

run_scp() {
  local attempt
  for attempt in 1 2 3; do
    if [[ -n "${REMOTE_PASSWORD:-}" ]]; then
      if SSHPASS="$REMOTE_PASSWORD" sshpass -e scp "${SCP_OPTS[@]}" "$@"; then
        return 0
      fi
    else
      if scp "${SCP_OPTS[@]}" "$@"; then
        return 0
      fi
    fi
    if [[ "$attempt" -lt 3 ]]; then
      echo "[deploy-remote] scp attempt $attempt failed, retrying..." >&2
      sleep 2
    fi
  done
  return 1
}

run_remote_sudo() {
  local cmd="$1"
  local sudo_password="${REMOTE_SUDO_PASSWORD:-${REMOTE_PASSWORD:-}}"
  if [[ -n "$sudo_password" ]]; then
    run_ssh "printf '%s\n' '$sudo_password' | sudo -S -p '' bash -lc \"$cmd\""
  else
    run_ssh "sudo bash -lc \"$cmd\""
  fi
}

echo "[deploy-remote] packaging workspace"
tar \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='frontend/node_modules' \
  --exclude='frontend/dist' \
  --exclude='**/__pycache__' \
  --exclude='.DS_Store' \
  --exclude='tradingagents.db' \
  -czf "$TARBALL" \
  -C "$ROOT_DIR" .

echo "[deploy-remote] uploading archive"
run_scp "$TARBALL" "$REMOTE_USER@$REMOTE_HOST:/tmp/tradingagents-ashare-deploy.tgz"

echo "[deploy-remote] extracting on remote host"
run_ssh "mkdir -p '$REMOTE_APP_DIR' && tar -xzf /tmp/tradingagents-ashare-deploy.tgz -C '$REMOTE_APP_DIR'"

if [[ "${SYNC_REMOTE_ENV:-0}" == "1" ]]; then
  : "${LOCAL_REMOTE_ENV_FILE:?LOCAL_REMOTE_ENV_FILE is required when SYNC_REMOTE_ENV=1}"
  echo "[deploy-remote] syncing remote env file"
  run_scp "$LOCAL_REMOTE_ENV_FILE" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_ENV_FILE"
fi

echo "[deploy-remote] bootstrapping remote app"
run_ssh "APP_DIR='$REMOTE_APP_DIR' bash '$REMOTE_APP_DIR/deploy/backend/bootstrap_remote.sh'"

echo "[deploy-remote] restarting services"
run_remote_sudo "systemctl restart '$REMOTE_API_SERVICE' '$REMOTE_TUNNEL_SERVICE'"

echo "[deploy-remote] waiting for service health"
sleep 4
run_remote_sudo "systemctl is-active '$REMOTE_API_SERVICE' '$REMOTE_TUNNEL_SERVICE'"

BACKEND_PUBLIC_URL=""
for attempt in 1 2 3 4 5 6; do
  BACKEND_PUBLIC_URL="$(
    run_remote_sudo "journalctl -u '$REMOTE_TUNNEL_SERVICE' -n 120 --no-pager | grep -o 'https://[-a-z0-9]\\+\\.trycloudflare\\.com' | tail -n 1" \
      | tr -d '\r'
  )"

  if [[ -n "$BACKEND_PUBLIC_URL" ]] && curl -fsSL --max-time 10 "$BACKEND_PUBLIC_URL/healthz" >/dev/null 2>&1; then
    break
  fi

  echo "[deploy-remote] tunnel url not ready on attempt $attempt, waiting..." >&2
  sleep 3
done

if [[ -z "$BACKEND_PUBLIC_URL" ]] || ! curl -fsSL --max-time 10 "$BACKEND_PUBLIC_URL/healthz" >/dev/null 2>&1; then
  echo "[deploy-remote] failed to discover a reachable cloudflare quick tunnel URL"
  exit 1
fi

echo "[deploy-remote] backend public url: $BACKEND_PUBLIC_URL"
echo "$BACKEND_PUBLIC_URL"
