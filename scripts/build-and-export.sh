#!/usr/bin/env bash
# 构建镜像（国内源优先）并导出 tar，供内网 docker load
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

IMAGE_NAME="${IMAGE_NAME:-collab-review-system}"
IMAGE_TAG="${IMAGE_TAG:-1.0.0}"
FULL_IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"
# 默认导出到仓库内 dist/；可用第一个参数覆盖，例如：./scripts/build-and-export.sh /path/to/out
OUT_DIR="${1:-$ROOT/dist}"
TAR_NAME="${IMAGE_NAME}-${IMAGE_TAG}.tar"

mkdir -p "$OUT_DIR"

echo "==> 1) 拉取基础镜像（国内镜像优先，失败回退官方）"
BASE_MIRRORS=(
  "docker.m.daocloud.io/library/python:3.11-slim"
  "dockerproxy.net/library/python:3.11-slim"
  "python:3.11-slim"
)
BASE_OK=""
for m in "${BASE_MIRRORS[@]}"; do
  echo "    try: $m"
  if docker pull "$m"; then
    if [[ "$m" != "python:3.11-slim" ]]; then
      docker tag "$m" python:3.11-slim
    fi
    BASE_OK="python:3.11-slim"
    echo "    OK: $m"
    break
  fi
done
if [[ -z "$BASE_OK" ]]; then
  echo "ERROR: 无法拉取 python:3.11-slim" >&2
  exit 1
fi

echo "==> 2) 构建业务镜像 $FULL_IMAGE"
docker build \
  --build-arg BASE_IMAGE=python:3.11-slim \
  -t "$FULL_IMAGE" \
  -t "${IMAGE_NAME}:latest" \
  .

echo "==> 3) 导出镜像 -> ${OUT_DIR}/${TAR_NAME}"
docker save -o "${OUT_DIR}/${TAR_NAME}" "$FULL_IMAGE"
# 可选 gzip 减小体积（内网 load 时用 gunzip -c | docker load）
gzip -kf "${OUT_DIR}/${TAR_NAME}"
ls -lh "${OUT_DIR}/${TAR_NAME}" "${OUT_DIR}/${TAR_NAME}.gz" 2>/dev/null || true

echo "==> 4) 复制离线部署文件"
cp -f docker-compose.yml "$OUT_DIR/"
cp -f .env.example "$OUT_DIR/"
# 生成一份可直接改名的 .env 模板（无密钥真实值）
cp -f .env.example "$OUT_DIR/.env.template"
cat > "$OUT_DIR/内网部署说明.txt" << 'EOF'
材料协同办理系统 — 内网 Docker 部署说明
========================================

一、准备文件（本目录应有）
  - collab-review-system-1.0.0.tar      （或 .tar.gz）
  - docker-compose.yml
  - .env.example  /  .env.template

二、加载镜像
  # 未压缩
  docker load -i collab-review-system-1.0.0.tar

  # 若使用 gzip
  gunzip -c collab-review-system-1.0.0.tar.gz | docker load

  # 确认
  docker images | grep collab-review-system

三、配置环境变量
  cp .env.example .env
  # 用记事本/vim 编辑 .env：
  #   - 修改 SECRET_KEY、ADMIN_PASSWORD
  #   - 内网 OA 时设置 AUTH_MODE=mixed 或 oa，并填写 OA_BASE_URL
  #   - SEED_DEMO_USERS 生产请保持 false

四、启动
  docker compose up -d
  docker compose ps
  docker compose logs -f

五、访问
  http://服务器IP:5009/login.html
  默认管理员见 .env 中 ADMIN_USERNAME / ADMIN_PASSWORD

六、停止 / 卸载
  docker compose down          # 保留数据卷
  docker compose down -v       # 删除数据卷（清空库与上传文件）

七、数据持久化
  默认使用 Docker 命名卷：
    collab_data     -> 数据库
    collab_uploads  -> 上传文件
  备份示例：
    docker run --rm -v collab-review-system_collab_data:/data -v $(pwd):/backup alpine \
      tar czf /backup/collab-data-backup.tgz -C /data .

八、注意
  - 内网无互联网，不要执行 docker compose build
  - 不要提交含真实密码的 .env 到公开仓库
  - 镜像标签必须为 collab-review-system:1.0.0（与 compose 中 image 一致）
EOF

echo "==> 完成"
echo "    镜像: $FULL_IMAGE"
echo "    目录: $OUT_DIR"
ls -lh "$OUT_DIR"
