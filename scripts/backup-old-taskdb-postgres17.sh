#!/usr/bin/env bash
# =============================================================================
# 备份【旧任务系统】的 PostgreSQL 17 数据库（Docker 部署版）
#
# 适用环境：内网 Ubuntu 22，postgres17 用 Docker 跑，执行账号非 root。
# 只要当前账号能执行 docker 命令即可（若提示 permission denied，
# 让管理员把账号加入 docker 组：sudo usermod -aG docker 你的用户名，
# 重新登录生效；或临时用 sudo bash 本脚本）。
#
# 原理：调用容器【内部】自带的 pg_dump（版本必然匹配），走容器内本地连接，
# 通常无需密码。备份文件落在宿主机 OUT_DIR。
#
# ---------- 使用前只需确认/修改下面 5 个变量 ----------
CONTAINER="postgres17"          # 容器名，用 docker ps 查看 NAMES 列
DB_USER="postgres"              # 数据库用户，Docker 部署一般就是 postgres
DB_NAME="renwuguanli"           # 旧系统库名，不确定可先执行:
                                #   docker exec postgres17 psql -U postgres -l
OUT_DIR="$HOME/backup-old-taskdb"   # 备份存放目录（非 root 可写的位置）
KEEP=30                         # 保留最近多少份
# ------------------------------------------------------
#
# 手动执行：      bash scripts/backup-old-taskdb-postgres17.sh
# 每天 03:00 自动：crontab -e 加一行（日志追加到备份目录）：
#   0 3 * * * /bin/bash /完整路径/backup-old-taskdb-postgres17.sh >> $HOME/backup-old-taskdb/backup.log 2>&1
#
# 恢复示例（把某份备份恢复回同名库，会先清空重建对象，谨慎操作）：
#   docker exec -i postgres17 pg_restore -U postgres -d renwuguanli --clean --if-exists < 备份文件.dump
# =============================================================================
set -euo pipefail

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="$OUT_DIR/old_taskdb_${DB_NAME}_${STAMP}.dump"

mkdir -p "$OUT_DIR"

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "错误: 找不到容器 $CONTAINER，用 docker ps 确认容器名后改脚本开头的 CONTAINER 变量" >&2
  exit 1
fi

echo "开始备份: 容器=$CONTAINER 库=$DB_NAME -> $(basename "$OUT_FILE")"

# -Fc 自定义压缩格式，配合 pg_restore 使用；失败时删掉半截文件
if ! docker exec "$CONTAINER" pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$OUT_FILE"; then
  rm -f "$OUT_FILE"
  echo "错误: pg_dump 失败，未产生备份文件。常见原因: DB_USER/DB_NAME 不对" >&2
  exit 1
fi

# 空文件视为失败
if [[ ! -s "$OUT_FILE" ]]; then
  rm -f "$OUT_FILE"
  echo "错误: 备份文件为空，请检查库名是否正确" >&2
  exit 1
fi

echo "备份完成: $(basename "$OUT_FILE") 大小: $(du -h "$OUT_FILE" | cut -f1)"

# 只保留最近 KEEP 份（按文件名里的时间戳排序，新的在前）
ls -1t "$OUT_DIR"/old_taskdb_"${DB_NAME}"_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do
  rm -f "$f"
  echo "已删除过期备份: $(basename "$f")"
done

echo "当前备份份数: $(ls -1 "$OUT_DIR"/old_taskdb_"${DB_NAME}"_*.dump 2>/dev/null | wc -l)/$KEEP"
