-- =============================================================================
-- 旧任务系统人员花名册 → 材料协同办理系统 users 表（生成器 SQL）
-- 文件：scripts/20260718_migrate_users_from_old_system.sql
-- =============================================================================
--
-- 【背景】
-- 本系统 users 平时靠 OA 登录自动建号；办公室收文员需要给「还没登录过的人」
-- 分派事项，因此从旧任务系统（PostgreSQL 17 / Prisma）一次性导入人员。
--
-- 【旧库真实结构】（表名、字段名大小写敏感，SQL 必须加双引号）
--   "User"        : "badgeNo"(警号), "name"(姓名), "departmentId"(可空 FK), "role"(旧系统角色，不迁移)
--   "Department"  : "id", "name"(部门名)
-- 旧库 passwordHash **不迁移**（新系统走 OA 认证，迁密码是安全隐患）。
--
-- 【新库 role 列存储格式（已验证）】
--   SQLAlchemy `Enum(UserRole)` 在 PostgreSQL / 人大金仓上为原生枚举类型 `userrole`，
--   存的是枚举 **值**（小写字符串），不是枚举名大写：
--     admin | office_clerk | supervisor | handler | leader_a | leader_b | viewer
--   INSERT 时写 'viewer'::userrole 最稳妥（金仓 V8R6 / PG 均支持）。
--   SQLite 测试环境无原生枚举，存 VARCHAR，直接写 'viewer' 即可（本文件仅给金仓用）。
--
-- 【password_hash】
--   下方常量为合法 bcrypt 哈希，对应一个 32 字节随机串的哈希结果；随机串已丢弃且不记录。
--   导入账号因此无法用本地密码登录，只能走 OA。
--
-- 【执行前必做】
--   1. 抽查若干 "badgeNo" 是否与 OA 工号（userCode，如 270378 六位数字）同一套编号。
--      badgeNo 即 OA 登录后匹配本地账号的唯一依据；不一致则 **停止导入** 并反馈。
--   2. 在金仓确认 users.username 有 UNIQUE 约束（本项目 create_all 默认有）。
--
-- =============================================================================
-- 两步操作说明
-- =============================================================================
--
-- 第一步：在【旧库 postgres17】生成 INSERT 文本文件
--   （把连接串换成实际值；-t -A 去掉表头与对齐，只输出纯 SQL 行）
--
--   psql "postgresql://USER:PASS@HOST:5432/OLD_DB" \
--     -v ON_ERROR_STOP=1 \
--     -t -A \
--     -f scripts/20260718_migrate_users_from_old_system.sql \
--     -o users_import.sql
--
-- 第二步：检查 users_import.sql 行数与抽样内容后，在【金仓 / 新库】执行：
--
--   psql "postgresql://USER:PASS@HOST:54321/NEW_DB" \
--     -v ON_ERROR_STOP=1 \
--     -f users_import.sql
--
-- 重复执行安全：ON CONFLICT (username) DO NOTHING，不会覆盖已有账号
-- （含已通过 OA 登录自建、或管理员已改过角色的账号）。
--
-- =============================================================================
-- 以下 SELECT 仅在旧库执行，输出的是「给新库执行的 INSERT 语句」
-- =============================================================================

SELECT format(
  $fmt$INSERT INTO users (username, password_hash, display_name, role, unit, is_active, created_at)
VALUES (%L, %L, %L, 'viewer'::userrole, %L, true, now())
ON CONFLICT (username) DO NOTHING;$fmt$,
  u."badgeNo",
  -- 固定 bcrypt：随机明文已丢弃，导入用户只能 OA 登录
  '$2b$12$D3FpwHyTr1Jsmt6d0Mzfe.DRwiRTVTEdX8jhK7Wk8KzIDF5oot2fG',
  u."name",
  -- %L 对 NULL 输出 NULL（无引号），对含单引号的部门名会正确转义
  NULLIF(btrim(d."name"), '')
)
FROM "User" u
LEFT JOIN "Department" d ON d."id" = u."departmentId"
WHERE u."badgeNo" IS NOT NULL
  AND btrim(u."badgeNo") <> ''
  AND u."name" IS NOT NULL
  AND btrim(u."name") <> ''
ORDER BY u."badgeNo";
