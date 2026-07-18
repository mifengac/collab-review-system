#!/usr/bin/env bash
# =============================================================================
# 数据库备份：金仓 / PostgreSQL（pg_dump 或 sys_dump）+ SQLite 文件复制
# 用法：
#   export DATABASE_URL='postgresql://user:pass@host:54321/collab_review'
#   bash scripts/backup-db.sh
# 可选环境变量：
#   BACKUP_DIR          备份目录，默认 <仓库>/backups
#   BACKUP_KEEP         保留份数，默认 30
#   PG_DUMP_BIN         pg_dump 可执行文件路径（默认在 PATH 中找 pg_dump）
#   SYS_DUMP_BIN        金仓 sys_dump 路径；若设置则优先于 pg_dump
#   DATABASE_URL        必填（也可从仓库根目录 .env 读取，不会 echo 密码）
#
# crontab 示例（每天 02:15，以部署用户身份；勿把密码写进 crontab，用 .env）：
#   15 2 * * * cd /path/to/collab-review-system && \
#     set -a && . ./.env && set +a && \
#     /bin/bash scripts/backup-db.sh >>logs/backup.log 2>&1
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-30}"
STAMP="$(date +%Y%m%d_%H%M%S)_$$"

mkdir -p "$BACKUP_DIR"

# 从 .env 加载 DATABASE_URL（若未在环境中设置）；不打印文件内容
if [[ -z "${DATABASE_URL:-}" && -f "$ROOT/.env" ]]; then
  # 只取 DATABASE_URL 一行，支持 export 前缀与引号
  _line="$(grep -E '^[[:space:]]*(export[[:space:]]+)?DATABASE_URL=' "$ROOT/.env" | tail -n1 || true)"
  if [[ -n "$_line" ]]; then
    _line="${_line#export }"
    _line="${_line#DATABASE_URL=}"
    _line="${_line#\"}"
    _line="${_line%\"}"
    _line="${_line#\'}"
    _line="${_line%\'}"
    DATABASE_URL="$_line"
  fi
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "错误: 未设置 DATABASE_URL（环境变量或 .env）" >&2
  exit 1
fi

# 不把完整 URL 打到日志（可能含密码）；只显示方案与目标文件名
_scheme="${DATABASE_URL%%:*}"
echo "开始备份 scheme=${_scheme} keep=${BACKUP_KEEP} dir=${BACKUP_DIR}"

# ---------- SQLite ----------
if [[ "$DATABASE_URL" == sqlite:* ]]; then
  # sqlite:////abs/path 或 sqlite:///./rel 或 sqlite:////app/data/collab.db
  DB_PATH="$(
    DATABASE_URL="$DATABASE_URL" python3 - <<'PY'
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

url = os.environ["DATABASE_URL"]
# SQLAlchemy: sqlite:////abs → path /abs；sqlite:///rel → rel
if url.startswith("sqlite:////"):
    path = unquote(url[len("sqlite:///"):])  # /abs/...
elif url.startswith("sqlite:///"):
    path = unquote(url[len("sqlite:///"):])
else:
    # sqlite:///:memory: 等
    path = urlparse(url).path or ""
print(path)
PY
  )"
  if [[ -z "$DB_PATH" || "$DB_PATH" == ":memory:" ]]; then
    echo "错误: 无法从 DATABASE_URL 解析 SQLite 文件路径，或为内存库" >&2
    exit 1
  fi
  # 相对路径相对仓库根
  if [[ "$DB_PATH" != /* ]]; then
    DB_PATH="$ROOT/$DB_PATH"
  fi
  if [[ ! -f "$DB_PATH" ]]; then
    echo "错误: SQLite 库文件不存在: ${DB_PATH##*/}" >&2
    exit 1
  fi
  OUT="$BACKUP_DIR/collab_sqlite_${STAMP}.db"
  cp -a "$DB_PATH" "$OUT"
  echo "SQLite 备份完成: $(basename "$OUT")"
else
  # ---------- PostgreSQL / Kingbase ----------
  # 解析连接信息到临时环境，密码走 PGPASSWORD，绝不 echo
  eval "$(
    DATABASE_URL="$DATABASE_URL" python3 - <<'PY'
import os
import shlex
from urllib.parse import unquote, urlparse

raw = os.environ["DATABASE_URL"]
# 兼容 postgresql+psycopg2://
if raw.startswith("postgresql+"):
    raw = "postgresql://" + raw.split("://", 1)[1]
u = urlparse(raw)
if u.scheme not in ("postgresql", "postgres"):
    raise SystemExit(f"unsupported_scheme:{u.scheme}")
host = u.hostname or "127.0.0.1"
port = u.port or 5432
user = unquote(u.username or "")
password = unquote(u.password or "")
db = unquote((u.path or "/").lstrip("/") or "postgres")
if not user:
    raise SystemExit("missing_user")
# 导出给 bash（密码单独）
print(f"export PGHOST={shlex.quote(host)}")
print(f"export PGPORT={shlex.quote(str(port))}")
print(f"export PGUSER={shlex.quote(user)}")
print(f"export PGDATABASE={shlex.quote(db)}")
print(f"export PGPASSWORD={shlex.quote(password)}")
PY
  )"

  OUT="$BACKUP_DIR/collab_pg_${STAMP}.dump"
  DUMP_BIN=""
  if [[ -n "${SYS_DUMP_BIN:-}" && -x "${SYS_DUMP_BIN}" ]]; then
    DUMP_BIN="$SYS_DUMP_BIN"
  elif [[ -n "${PG_DUMP_BIN:-}" ]]; then
    DUMP_BIN="$PG_DUMP_BIN"
  elif command -v pg_dump >/dev/null 2>&1; then
    DUMP_BIN="$(command -v pg_dump)"
  else
    echo "错误: 未找到 pg_dump；请安装客户端或设置 PG_DUMP_BIN / SYS_DUMP_BIN" >&2
    unset PGPASSWORD
    exit 1
  fi

  # 自定义格式，便于 pg_restore；-f 写文件
  # 注意：失败时仍要清掉 PGPASSWORD
  set +e
  "$DUMP_BIN" -Fc -f "$OUT" >/dev/null
  rc=$?
  set -e
  unset PGPASSWORD
  if [[ $rc -ne 0 ]]; then
    rm -f "$OUT"
    echo "错误: 备份失败（exit=$rc），未写入有效文件" >&2
    exit "$rc"
  fi
  echo "PostgreSQL/Kingbase 备份完成: $(basename "$OUT") tool=$(basename "$DUMP_BIN")"
fi

# ---------- 只保留最近 BACKUP_KEEP 份 ----------
# 匹配本脚本生成的两类文件
mapfile -t _files < <(
  find "$BACKUP_DIR" -maxdepth 1 -type f \( \
    -name 'collab_pg_*.dump' -o -name 'collab_sqlite_*.db' \
  \) -printf '%T@ %p\n' 2>/dev/null | sort -nr | awk '{print $2}'
)

if ((${#_files[@]} > BACKUP_KEEP)); then
  for ((i = BACKUP_KEEP; i < ${#_files[@]}; i++)); do
    rm -f "${_files[$i]}"
    echo "已删除过期备份: $(basename "${_files[$i]}")"
  done
fi

echo "备份结束，当前份数: $(
  find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'collab_pg_*.dump' -o -name 'collab_sqlite_*.db' \) | wc -l
)/${BACKUP_KEEP}"
