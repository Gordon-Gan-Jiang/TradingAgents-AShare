#!/usr/bin/env bash
set -euo pipefail

# 获取脚本所在根目录
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${DEPLOY_ENV_FILE:-$ROOT_DIR/deploy/deploy.env}"

# 读取配置
if [[ -f "$ENV_FILE" ]]; then
  source "$ENV_FILE"
else
  echo "Error: Missing deploy env: $ENV_FILE"
  exit 1
fi

# 核心变量
REMOTE_TARGET="${REMOTE_USER:-box}@${REMOTE_HOST:-192.168.1.101}"
CONTAINER_NAME="tradingagents-api"
IMAGE="ghcr.io/kylinmountain/tradingagents-ashare:latest"
DOCKER_DATA_DIR="/home/box/tradingagents-docker"
OLD_APP_DIR="/home/box/tradingagents-ashare"

echo "[deploy-docker] 正在连接到 $REMOTE_TARGET 部署最新 Docker 镜像..."

# 1. 停止旧的 systemd 服务 (首次迁移需要)
ssh "$REMOTE_TARGET" "echo '${REMOTE_SUDO_PASSWORD:-}' | sudo -S systemctl stop tradingagents-api 2>/dev/null || true"

# 2. 远程执行 Docker 逻辑
ssh "$REMOTE_TARGET" bash << EOF
  set -e
  echo "[1/3] 正在拉取镜像: $IMAGE"
  docker pull $IMAGE

  echo "[2/3] 清理旧容器..."
  docker stop $CONTAINER_NAME 2>/dev/null || true
  docker rm $CONTAINER_NAME 2>/dev/null || true

  echo "[3/3] 启动新容器 (前后端合一)..."
  docker run -d \\
    --name $CONTAINER_NAME \\
    --restart always \\
    -p 8000:8000 \\
    -v $DOCKER_DATA_DIR/tradingagents.db:/app/tradingagents.db \\
    -v $OLD_APP_DIR/tradingagents/dataflows/data_cache:/app/tradingagents/dataflows/data_cache \\
    -v $OLD_APP_DIR/eval_results:/app/eval_results \\
    --env-file $DOCKER_DATA_DIR/.env \\
    $IMAGE

  echo "✅ Docker 部署成功！"
  docker ps -f name=$CONTAINER_NAME
EOF
