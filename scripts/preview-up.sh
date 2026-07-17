#!/usr/bin/env bash
# 构建并启动 Docker 预览环境（主应用 + 模拟 OA）+ 自动冒烟
# 与正式 5002 服务隔离；不读取正式 .env；默认保留 preview 数据卷
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

IMAGE_NAME="collab-review-system:preview"
NETWORK="collab-preview-net"
APP_NAME="collab-review-preview"
MOCK_NAME="collab-review-mock-oa"
VOL_DATA="collab_preview_data"
VOL_UPLOADS="collab_preview_uploads"
export PREVIEW_PORT="${PREVIEW_PORT:-5010}"
MOCK_PORT_INTERNAL=5099
APP_PORT_INTERNAL=5002
FORMAL_NAME="collab-review-system"
FORMAL_PORT=5002

COMPOSE_FILE="docker-compose.preview.yml"

log() { echo "[preview-up] $*"; }
err() { echo "[preview-up] ERROR: $*" >&2; }

detect_compose() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
    return 0
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    echo "docker-compose"
    return 0
  fi
  echo ""
}

wait_http() {
  local url="$1"
  local name="$2"
  local max="${3:-40}"
  local i=0
  while [ "$i" -lt "$max" ]; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      log "健康检查通过: $name"
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  err "健康检查超时: $name ($url)"
  return 1
}

port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -lnt 2>/dev/null | awk '{print $4}' | grep -E "[:.]${port}$" >/dev/null 2>&1
    return $?
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -lnt 2>/dev/null | awk '{print $4}' | grep -E "[:.]${port}$" >/dev/null 2>&1
    return $?
  fi
  # 回退：尝试绑定检测
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$port" <<'PY'
import socket, sys
p = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", p))
except OSError:
    sys.exit(0)  # in use
else:
    s.close()
    sys.exit(1)  # free
PY
    return $?
  fi
  return 1
}

preview_owns_port() {
  # 当前预览主容器是否占用 PREVIEW_PORT
  docker ps --filter "name=^/${APP_NAME}$" --format '{{.Ports}}' 2>/dev/null \
    | grep -E "(^|[^0-9])${PREVIEW_PORT}->" >/dev/null 2>&1
}

check_preview_port() {
  if [ "$PREVIEW_PORT" = "$FORMAL_PORT" ]; then
    err "PREVIEW_PORT 不能使用正式服务端口 ${FORMAL_PORT}"
    exit 1
  fi
  if ! port_in_use "$PREVIEW_PORT"; then
    log "预览端口 ${PREVIEW_PORT} 可用"
    return 0
  fi
  if preview_owns_port; then
    log "端口 ${PREVIEW_PORT} 由旧预览容器占用，将强制重建"
    return 0
  fi
  err "端口 ${PREVIEW_PORT} 已被其他进程/容器占用，请更换 PREVIEW_PORT 或释放端口"
  err "不会删除无关容器；正式服务端口 ${FORMAL_PORT} 不受影响"
  exit 1
}

if ! command -v docker >/dev/null 2>&1; then
  err "未找到 docker 命令，无法启动预览环境"
  exit 1
fi

log "工作目录: $ROOT"
log "PREVIEW_PORT=${PREVIEW_PORT}"
check_preview_port

log "构建镜像 $IMAGE_NAME （强制最新代码）"
docker build -t "$IMAGE_NAME" "$ROOT"
IMAGE_ID="$(docker image inspect -f '{{.Id}}' "$IMAGE_NAME" 2>/dev/null | sed 's/^sha256://' | cut -c1-12 || true)"
log "镜像: $IMAGE_NAME  ID=${IMAGE_ID:-unknown}"

COMPOSE_CMD="$(detect_compose)"

if [ -n "$COMPOSE_CMD" ]; then
  log "使用 $COMPOSE_CMD 启动预览栈（export PREVIEW_PORT=${PREVIEW_PORT}）"
  # shellcheck disable=SC2086
  $COMPOSE_CMD -f "$COMPOSE_FILE" up -d --force-recreate
else
  log "未检测到 docker compose / docker-compose，使用 docker run 回退"

  # 清理旧预览容器（绝不碰正式 collab-review-system）
  docker rm -f "$APP_NAME" "$MOCK_NAME" 2>/dev/null || true

  docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK"
  docker volume inspect "$VOL_DATA" >/dev/null 2>&1 || docker volume create "$VOL_DATA"
  docker volume inspect "$VOL_UPLOADS" >/dev/null 2>&1 || docker volume create "$VOL_UPLOADS"

  docker run -d \
    --name "$MOCK_NAME" \
    --network "$NETWORK" \
    --network-alias mock-oa \
    --restart unless-stopped \
    --health-cmd "curl -fsS http://127.0.0.1:${MOCK_PORT_INTERNAL}/api/health || exit 1" \
    --health-interval 10s \
    --health-timeout 5s \
    --health-retries 6 \
    --health-start-period 10s \
    "$IMAGE_NAME" \
    uvicorn app.mock_oa:app --host 0.0.0.0 --port "$MOCK_PORT_INTERNAL"

  if ! docker run -d \
    --name "$APP_NAME" \
    --network "$NETWORK" \
    --restart unless-stopped \
    -p "${PREVIEW_PORT}:${APP_PORT_INTERNAL}" \
    -v "${VOL_DATA}:/app/data" \
    -v "${VOL_UPLOADS}:/app/uploads" \
    -e APP_NAME="材料协同办理系统（预览）" \
    -e APP_HOST=0.0.0.0 \
    -e APP_PORT=5002 \
    -e SECRET_KEY=preview-only-not-for-production \
    -e ACCESS_TOKEN_EXPIRE_MINUTES=480 \
    -e DEBUG=true \
    -e DATABASE_URL=sqlite:////app/data/collab.db \
    -e ADMIN_USERNAME=admin \
    -e ADMIN_PASSWORD=Admin@123456 \
    -e ADMIN_DISPLAY_NAME=系统管理员 \
    -e SEED_DEMO_USERS=true \
    -e UPLOAD_DIR=/app/uploads \
    -e AUTH_MODE=oa \
    -e OA_BASE_URL=http://mock-oa:5099 \
    -e OA_LOGIN_PATH=/hportal/j_security_check \
    -e OA_PROFILE_PATH=/hportal/view/GetModuleTree.do \
    -e OA_LOGIN_TIMEOUT_SECONDS=8 \
    -e OA_DEFAULT_ROLE=handler \
    -e OA_VERIFY_TLS=false \
    -e OA_PRECHECK_ENABLED=false \
    -e OA_SYNC_ON_LOGIN=true \
    -e OA_SYNC_MAX_PAGES=3 \
    -e OA_SYNC_PAGE_SIZE=10 \
    -e OA_SYNC_MODULES=todo,unread,done,read_done,running \
    -e OA_LIST_PATH=/hmoa/s \
    -e OA_MOCK_ENABLED=true \
    --health-cmd "curl -fsS http://127.0.0.1:5002/api/health || exit 1" \
    --health-interval 10s \
    --health-timeout 5s \
    --health-retries 8 \
    --health-start-period 25s \
    "$IMAGE_NAME"
  then
    err "主预览容器启动失败（可能端口 ${PREVIEW_PORT} 占用）"
    docker rm -f "$APP_NAME" 2>/dev/null || true
    exit 1
  fi
fi

log "等待容器就绪…"
sleep 3

if ! docker exec "$MOCK_NAME" curl -fsS "http://127.0.0.1:${MOCK_PORT_INTERNAL}/api/health" >/dev/null 2>&1; then
  ok=0
  for _i in $(seq 1 30); do
    if docker exec "$MOCK_NAME" curl -fsS "http://127.0.0.1:${MOCK_PORT_INTERNAL}/api/health" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 1
  done
  if [ "$ok" -ne 1 ]; then
    err "模拟 OA 健康检查失败"
    docker ps -a --filter "name=${MOCK_NAME}" --filter "name=${APP_NAME}"
    docker logs --tail 40 "$MOCK_NAME" 2>/dev/null || true
    exit 1
  fi
fi
log "模拟 OA 健康检查通过"

wait_http "http://127.0.0.1:${PREVIEW_PORT}/api/health" "主应用 /api/health" 45
wait_http "http://127.0.0.1:${PREVIEW_PORT}/login.html" "login.html" 15
wait_http "http://127.0.0.1:${PREVIEW_PORT}/oa_items.html" "oa_items.html" 15

log "执行预览冒烟验收 scripts/preview-smoke.py"
SMOKE_PY="${ROOT}/.venv/bin/python"
if [ ! -x "$SMOKE_PY" ]; then
  SMOKE_PY="python3"
fi
if ! "$SMOKE_PY" "${ROOT}/scripts/preview-smoke.py" --base-url "http://127.0.0.1:${PREVIEW_PORT}"; then
  err "预览冒烟测试失败，预览环境未就绪"
  exit 1
fi

APP_STATUS="$(docker inspect -f '{{.State.Status}}' "$APP_NAME" 2>/dev/null || echo unknown)"
MOCK_STATUS="$(docker inspect -f '{{.State.Status}}' "$MOCK_NAME" 2>/dev/null || echo unknown)"
APP_HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$APP_NAME" 2>/dev/null || echo n/a)"
MOCK_HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$MOCK_NAME" 2>/dev/null || echo n/a)"
FORMAL_STATUS="$(docker inspect -f '{{.State.Status}}' "$FORMAL_NAME" 2>/dev/null || echo n/a)"

echo ""
echo "======== 预览环境已就绪 ========"
echo "镜像名称: $IMAGE_NAME"
echo "镜像 ID:  ${IMAGE_ID:-unknown}"
echo "主容器:   $APP_NAME  status=$APP_STATUS health=$APP_HEALTH"
echo "模拟 OA:  $MOCK_NAME  status=$MOCK_STATUS health=$MOCK_HEALTH"
echo "正式容器: $FORMAL_NAME status=$FORMAL_STATUS （未改动）"
echo "预览地址: http://127.0.0.1:${PREVIEW_PORT}/login.html"
echo "API 健康: http://127.0.0.1:${PREVIEW_PORT}/api/health"
echo "正式地址: http://127.0.0.1:${FORMAL_PORT}/login.html"
echo ""
echo "模拟登录账号（公开演示密码，见 README）："
echo "  handler1 / leader_a / leader_b / office1 / supervisor1"
echo "  admin（管理员演示密码，见 README）"
echo "说明: AUTH_MODE=oa，登录后自动同步模拟公文池；冒烟已通过"
echo "停止: bash scripts/preview-down.sh"
echo "================================"
exit 0
