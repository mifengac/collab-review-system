#!/usr/bin/env bash
# 仅停止/删除预览容器与预览网络；默认保留 preview 数据卷
# 使用 --purge 才删除 preview 数据卷
# 绝不动正式 5002 容器 collab-review-system 或正式数据卷
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="collab-review-preview"
MOCK_NAME="collab-review-mock-oa"
OO_NAME="collab-review-onlyoffice"
NETWORK="collab-preview-net"
VOL_DATA="collab_preview_data"
VOL_UPLOADS="collab_preview_uploads"
COMPOSE_FILE="docker-compose.preview.yml"
PURGE=0

for arg in "$@"; do
  if [ "$arg" = "--purge" ]; then
    PURGE=1
  fi
done

log() { echo "[preview-down] $*"; }
err() { echo "[preview-down] ERROR: $*" >&2; }

if ! command -v docker >/dev/null 2>&1; then
  err "未找到 docker"
  exit 1
fi

# 安全：绝不操作正式容器名
if [ "$APP_NAME" = "collab-review-system" ] || [ "$MOCK_NAME" = "collab-review-system" ]; then
  err "内部安全检查失败"
  exit 1
fi

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

COMPOSE_CMD="$(detect_compose)"
if [ -n "$COMPOSE_CMD" ] && [ -f "$COMPOSE_FILE" ]; then
  log "使用 $COMPOSE_CMD down（不删卷）"
  # shellcheck disable=SC2086
  $COMPOSE_CMD -f "$COMPOSE_FILE" down --remove-orphans || true
else
  log "停止并删除预览容器"
  docker rm -f "$APP_NAME" "$MOCK_NAME" "$OO_NAME" 2>/dev/null || true
  if docker network inspect "$NETWORK" >/dev/null 2>&1; then
    log "删除预览网络 $NETWORK"
    docker network rm "$NETWORK" 2>/dev/null || true
  fi
fi

# 再次确保预览容器已删（compose 可能未装）
docker rm -f "$APP_NAME" "$MOCK_NAME" "$OO_NAME" 2>/dev/null || true
if docker network inspect "$NETWORK" >/dev/null 2>&1; then
  # 若仍有网络且无容器占用则删
  docker network rm "$NETWORK" 2>/dev/null || true
fi

if [ "$PURGE" -eq 1 ]; then
  log "按 --purge 删除预览数据卷"
  docker volume rm "$VOL_DATA" "$VOL_UPLOADS" 2>/dev/null || true
else
  log "已保留预览数据卷（$VOL_DATA / $VOL_UPLOADS）；清除请加 --purge"
fi

# 确认正式容器仍在（若存在）
if docker ps -a --format '{{.Names}}' | grep -qx 'collab-review-system'; then
  log "正式容器 collab-review-system 未受影响"
fi

log "预览环境已停止"
exit 0
