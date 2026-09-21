#!/usr/bin/env bash
# =============================================================================
# AlphaPilot A-Share 一键启动/停止/重启（源码模式，macOS / Linux）
#
# 用法：
#   ./scripts/dev.sh start        启动后端 + 前端 Vite 开发服（热更新，推荐）
#   ./scripts/dev.sh preview      启动后端 + 编译前端后 vite preview
#   ./scripts/dev.sh stop         停止前后端
#   ./scripts/dev.sh restart      按上次模式重启
#   ./scripts/dev.sh status       查看运行状态（含 HTTP 探活）
#   ./scripts/dev.sh logs [f|b]   查看日志（f=前端 b=后端，默认后端）
#   ./scripts/dev.sh dev          start 的别名
#
# 环境变量（可选覆盖）：
#   PORT_BACKEND   后端端口（默认 8000）
#   PORT_FRONTEND  前端端口（start/dev 默认 5173，preview 默认 4173）
#   VITE_API_URL   仅 preview 编译时可选；默认留空，走同源 Vite 代理
#   VITE_PROXY_TARGET  Vite 代理目标（默认 http://127.0.0.1:${PORT_BACKEND}）
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${ROOT}/.dev"
PID_BACKEND="${RUN_DIR}/backend.pid"
PID_FRONTEND="${RUN_DIR}/frontend.pid"
LOG_BACKEND="${RUN_DIR}/backend.log"
LOG_FRONTEND="${RUN_DIR}/frontend.log"
MODE_FILE="${RUN_DIR}/mode"

PORT_BACKEND="${PORT_BACKEND:-8000}"
VITE_BIN="${ROOT}/frontend/node_modules/.bin/vite"

mkdir -p "${RUN_DIR}"

log() { echo "[dev] $*"; }

die() { log "错误: $*"; exit 1; }

_pid_of() { cat "$1" 2>/dev/null || true; }

is_pid_alive() {
  local pid="${1:-}"
  [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null
}

is_running() { # <pidfile>
  is_pid_alive "$(_pid_of "$1")"
}

# 递归结束进程树（npm 会留下 node 子进程占端口）
kill_tree() {
  local pid="${1:-}"
  [ -n "${pid}" ] || return 0
  local child
  for child in $(pgrep -P "${pid}" 2>/dev/null || true); do
    kill_tree "${child}"
  done
  kill "${pid}" 2>/dev/null || true
}

listen_pids() { # <port>
  lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true
}

free_port() { # <port>
  local port="$1"
  local pids
  pids="$(listen_pids "${port}")"
  [ -n "${pids}" ] || return 0
  log "释放端口 ${port}（pid ${pids}）"
  # shellcheck disable=SC2086
  kill ${pids} 2>/dev/null || true
  sleep 0.4
  pids="$(listen_pids "${port}")"
  if [ -n "${pids}" ]; then
    # shellcheck disable=SC2086
    kill -9 ${pids} 2>/dev/null || true
    sleep 0.2
  fi
}

wait_gone() { # <pidfile>
  local pid
  pid="$(_pid_of "$1")"
  rm -f "$1"
  [ -n "${pid}" ] || return 0
  local i
  for i in $(seq 1 20); do
    is_pid_alive "${pid}" || return 0
    sleep 0.2
  done
  kill_tree "${pid}"
  kill -9 "${pid}" 2>/dev/null || true
  sleep 0.2
}

wait_http() { # <url> <tries> <sleep>
  local url="$1"
  local tries="${2:-40}"
  local pause="${3:-0.4}"
  local i
  for i in $(seq 1 "${tries}"); do
    if curl -sf -m 2 "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep "${pause}"
  done
  return 1
}

# 独立会话拉起进程，避免脚本退出时把前后端一起杀掉（Cursor/CI 常见）
spawn_daemon() { # <pidfile> <logfile> <cwd> <cmd...>
  local pidfile="$1" logfile="$2" cwd="$3"
  shift 3
  "${ROOT}/.venv/bin/python" -c '
import os, sys
pidfile, logfile, cwd, *argv = sys.argv[1:]
logfd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
pid = os.fork()
if pid > 0:
    with open(pidfile, "w", encoding="utf-8") as fh:
        fh.write(str(pid))
    os.close(logfd)
    raise SystemExit(0)
os.setsid()
os.chdir(cwd)
os.dup2(logfd, 1)
os.dup2(logfd, 2)
if logfd > 2:
    os.close(logfd)
os.execvp(argv[0], argv)
' "${pidfile}" "${logfile}" "${cwd}" "$@"
}

dump_log_tail() { # <file>
  local file="$1"
  if [ -f "${file}" ]; then
    log "---- ${file} 末尾 ----"
    tail -n 40 "${file}" || true
    log "----"
  fi
}

use_node() {
  local nvmrc="${ROOT}/frontend/.nvmrc"
  [ -f "${nvmrc}" ] || nvmrc="${ROOT}/.nvmrc"
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  if [ -s "${NVM_DIR}/nvm.sh" ] && [ -f "${nvmrc}" ]; then
    # shellcheck disable=SC1090
    . "${NVM_DIR}/nvm.sh"
    nvm use "$(cat "${nvmrc}")" >/dev/null 2>&1 || nvm use >/dev/null 2>&1 || true
  fi
}

check_prereqs() {
  command -v uv >/dev/null 2>&1 || die "未找到 uv，请先安装：curl -LsSf https://astral.sh/uv/install.sh | sh"
  command -v npm >/dev/null 2>&1 || die "未找到 npm，请先安装 Node.js ≥18"
  command -v lsof >/dev/null 2>&1 || die "未找到 lsof，无法检测端口占用"
  command -v curl >/dev/null 2>&1 || die "未找到 curl，无法做健康检查"
  [ -d "${ROOT}/.venv" ] || die "未找到 .venv，请先执行：uv sync"
  [ -x "${ROOT}/.venv/bin/python" ] || die ".venv/bin/python 不可执行，请重新 uv sync"
  [ -d "${ROOT}/frontend/node_modules" ] || die "未找到 frontend/node_modules，请先执行：cd frontend && npm install"
  [ -x "${VITE_BIN}" ] || die "未找到 ${VITE_BIN}，请先执行：cd frontend && npm install"
  use_node
}

# ── 后端 ────────────────────────────────────────────────────────

start_backend() {
  if is_running "${PID_BACKEND}"; then
    if curl -sf -m 2 "http://127.0.0.1:${PORT_BACKEND}/healthz" >/dev/null 2>&1; then
      log "后端已在运行 pid=$(_pid_of "${PID_BACKEND}") → http://127.0.0.1:${PORT_BACKEND}"
      return 0
    fi
    log "后端 PID 仍在但健康检查失败，准备重启"
    stop_backend
  fi
  free_port "${PORT_BACKEND}"
  : > "${LOG_BACKEND}"
  log "启动后端（uvicorn :${PORT_BACKEND}）..."
  spawn_daemon "${PID_BACKEND}" "${LOG_BACKEND}" "${ROOT}" \
    "${ROOT}/.venv/bin/python" -m uvicorn api.main:app --host 127.0.0.1 --port "${PORT_BACKEND}"

  if wait_http "http://127.0.0.1:${PORT_BACKEND}/healthz" 50 0.4; then
    log "后端就绪 ✓ http://127.0.0.1:${PORT_BACKEND}（文档 /docs，日志 ${LOG_BACKEND}）"
    return 0
  fi
  dump_log_tail "${LOG_BACKEND}"
  if ! is_running "${PID_BACKEND}"; then
    die "后端进程已退出。常见原因：端口被占用、依赖缺失、.env 配置错误"
  fi
  die "后端 ${PORT_BACKEND} 已监听但 /healthz 未就绪，查看 ${LOG_BACKEND}"
}

stop_backend() {
  if is_running "${PID_BACKEND}"; then
    log "停止后端 pid=$(_pid_of "${PID_BACKEND}") ..."
    kill_tree "$(_pid_of "${PID_BACKEND}")"
    wait_gone "${PID_BACKEND}"
  else
    rm -f "${PID_BACKEND}"
  fi
  free_port "${PORT_BACKEND}"
  log "后端已停止"
}

# ── 前端 ────────────────────────────────────────────────────────

frontend_port() {
  local mode="${1:-dev}"
  if [ -n "${PORT_FRONTEND:-}" ]; then
    echo "${PORT_FRONTEND}"
    return
  fi
  if [ "${mode}" = "preview" ]; then
    echo "4173"
  else
    echo "5173"
  fi
}

start_frontend_dev() {
  local port
  port="$(frontend_port dev)"
  if is_running "${PID_FRONTEND}"; then
    if curl -sf -m 2 "http://127.0.0.1:${port}/" >/dev/null 2>&1; then
      log "前端已在运行 pid=$(_pid_of "${PID_FRONTEND}") → http://127.0.0.1:${port}"
      return 0
    fi
    stop_frontend
  fi
  free_port "${port}"
  : > "${LOG_FRONTEND}"
  log "启动前端 Vite dev（热更新）:${port} ..."
  export VITE_PROXY_TARGET="http://127.0.0.1:${PORT_BACKEND}"
  unset VITE_API_URL || true
  spawn_daemon "${PID_FRONTEND}" "${LOG_FRONTEND}" "${ROOT}/frontend" \
    "${VITE_BIN}" --host 127.0.0.1 --port "${port}" --strictPort
  echo "dev" > "${MODE_FILE}"
  if wait_http "http://127.0.0.1:${port}/" 40 0.25; then
    log "前端就绪 ✓ http://127.0.0.1:${port}（日志 ${LOG_FRONTEND}）"
    return 0
  fi
  dump_log_tail "${LOG_FRONTEND}"
  die "前端启动失败（端口 ${port}）。查看 ${LOG_FRONTEND}"
}

build_frontend() {
  log "编译前端（tsc + vite build）..."
  (
    cd "${ROOT}/frontend"
    # 不注入 VITE_API_URL，运行时走 preview 同源代理，避免 localhost→::1 超时
    unset VITE_API_URL
    export VITE_PROXY_TARGET="http://127.0.0.1:${PORT_BACKEND}"
    npm run build
  )
  log "前端编译完成 ✓"
}

start_frontend_preview() {
  local port
  port="$(frontend_port preview)"
  if is_running "${PID_FRONTEND}"; then
    stop_frontend
  fi
  build_frontend
  free_port "${port}"
  : > "${LOG_FRONTEND}"
  log "启动前端（vite preview :${port}）..."
  export VITE_PROXY_TARGET="http://127.0.0.1:${PORT_BACKEND}"
  spawn_daemon "${PID_FRONTEND}" "${LOG_FRONTEND}" "${ROOT}/frontend" \
    "${VITE_BIN}" preview --host 127.0.0.1 --port "${port}" --strictPort
  echo "preview" > "${MODE_FILE}"
  if wait_http "http://127.0.0.1:${port}/" 30 0.3; then
    log "前端就绪 ✓ http://127.0.0.1:${port}（日志 ${LOG_FRONTEND}）"
    return 0
  fi
  dump_log_tail "${LOG_FRONTEND}"
  die "前端 preview 启动失败。查看 ${LOG_FRONTEND}"
}

stop_frontend() {
  if is_running "${PID_FRONTEND}"; then
    log "停止前端 pid=$(_pid_of "${PID_FRONTEND}") ..."
    kill_tree "$(_pid_of "${PID_FRONTEND}")"
    wait_gone "${PID_FRONTEND}"
  else
    rm -f "${PID_FRONTEND}"
  fi
  free_port "$(frontend_port dev)"
  free_port "$(frontend_port preview)"
  log "前端已停止"
}

# ── 子命令 ──────────────────────────────────────────────────────

start_all() {
  check_prereqs
  start_backend
  start_frontend_dev
  log "全部就绪：前端 http://127.0.0.1:$(frontend_port dev)  →  后端 http://127.0.0.1:${PORT_BACKEND}"
}

start_preview_all() {
  check_prereqs
  start_backend
  start_frontend_preview
  log "全部就绪：前端 http://127.0.0.1:$(frontend_port preview)  →  后端 http://127.0.0.1:${PORT_BACKEND}"
}

stop_all() {
  stop_frontend
  stop_backend
  log "已全部停止"
}

restart_all() {
  local mode="dev"
  if [ -f "${MODE_FILE}" ]; then
    mode="$(cat "${MODE_FILE}")"
  fi
  log "===== 重启（${mode}） ====="
  stop_all
  if [ "${mode}" = "preview" ]; then
    start_preview_all
  else
    start_all
  fi
}

status_all() {
  check_prereqs
  local backend_http="down" frontend_http="down"
  if curl -sf -m 2 "http://127.0.0.1:${PORT_BACKEND}/healthz" >/dev/null 2>&1; then
    backend_http="ok"
  fi
  if curl -sf -m 2 "http://127.0.0.1:$(frontend_port dev)/" >/dev/null 2>&1; then
    frontend_http="ok"
  elif curl -sf -m 2 "http://127.0.0.1:$(frontend_port preview)/" >/dev/null 2>&1; then
    frontend_http="ok"
  fi
  if is_running "${PID_BACKEND}"; then
    log "后端: 运行中 pid=$(_pid_of "${PID_BACKEND}") healthz=${backend_http} → http://127.0.0.1:${PORT_BACKEND}"
  else
    log "后端: 未运行（healthz=${backend_http}）"
  fi
  if is_running "${PID_FRONTEND}"; then
    log "前端: 运行中 pid=$(_pid_of "${PID_FRONTEND}") http=${frontend_http} mode=$(cat "${MODE_FILE}" 2>/dev/null || echo unknown)"
  else
    log "前端: 未运行（http=${frontend_http}）"
  fi
}

logs() {
  local which="${1:-b}"
  case "${which}" in
    f|frontend) tail -f "${LOG_FRONTEND}" ;;
    *) tail -f "${LOG_BACKEND}" ;;
  esac
}

case "${1:-}" in
  start|dev) start_all ;;
  preview)   start_preview_all ;;
  stop)      stop_all ;;
  restart)   restart_all ;;
  status)    status_all ;;
  logs)      logs "${2:-b}" ;;
  *)
    echo "用法: $0 {start|stop|restart|status|logs [f|b]|preview|dev}"
    echo "  start     启动后端 + 前端 Vite 热更新（http://127.0.0.1:5173）"
    echo "  preview   启动后端 + 编译后 preview（http://127.0.0.1:4173）"
    echo "  stop      停止前后端并释放端口"
    echo "  restart   按上次模式重启"
    echo "  status    查看 PID 与 HTTP 探活"
    echo "  logs f|b  跟踪前端/后端日志"
    echo "  dev       start 的别名"
    exit 1
    ;;
esac
