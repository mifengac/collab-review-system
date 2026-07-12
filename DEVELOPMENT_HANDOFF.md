# 材料协同办理系统开发交接记录

更新时间：2026-07-12

## 1. 项目背景

本项目用于公安局治安管理支队内部材料协同办理。目标是替代“办事员打印材料给 A 领导改、改完再给 B 领导改、反复打印流转”的线下流程。

核心目标：

- 收到 OA 公文后，可转为系统内协同事项。
- 承办人上传主材料 docx 和附件。
- A 领导、B 领导在线审核、退回、定稿。
- 系统记录谁在什么时候做了什么，保留材料版本和流转日志。
- 后续接入 ONLYOFFICE，实现 docx 在线编辑和痕迹保留。

单位组织背景：

- 信息工作大队（办公室）
- 基层基础及人口管理大队
- 巡警特警及维稳工作大队
- 治安管理行动大队

部署环境：

- 内网 Ubuntu 22 虚拟机
- Docker 部署
- 端口规划当前使用 5009
- 内网无互联网，镜像需要外网打包后导入

仓库：

- GitHub：<https://github.com/mifengac/collab-review-system>
- 本地目录：`/home/longshao/project/collab-review-system`

## 2. 当前实现概况

技术栈：

- 后端：FastAPI、SQLAlchemy、SQLite
- 前端：静态 HTML/CSS/JS
- 登录：JWT、本地账号、可选 OA 账号适配
- 部署：Dockerfile、docker-compose.yml

主要能力：

- 登录鉴权
- 用户和组织管理
- 工作台
- 事项创建、编辑、分派
- 承办人、A 领导、B 领导流转
- 办公室分派
- 督办催办
- 主材料和附件上传
- 文件版本号、sha256、不可覆盖
- 事项日志和时间线
- OA 登录适配
- OA 公文池同步
- 从 OA 公文池创建协同事项

当前基线：以 `main` 最新为准（本轮：旧库 migrate 失败硬停 + 模拟 OA Docker 预览 + 分页默认 10 / truncated→partial）。

近期相关提交：

- （本轮）Fix OA sync reconciliation and sensitive data handling
- `9f60610 Add OA sync diagnostics and module-level tracking`
- `ffc3ddf Update handoff after OA sync hardening`
- `73295de Add OA work item synchronization`

交接文档约定：

- **唯一**开发交接文件：`DEVELOPMENT_HANDOFF.md`
- 禁止再创建 `collab-review-system-development-handoff.md`

当前测试结果：

- `.venv/bin/python -m pytest -q -s`
- 结果：`70 passed`
- 说明：真实 OA 联调 **待内网验证**（本机仅 mock 测试）。

## 3. 角色设计

当前角色：

- `admin`：管理员，全量权限，可维护用户。
- `office_clerk`：办公室收文员，可创建事项、分派承办人和 A/B 领导，可查看全部。
- `supervisor`：督办人员，可查看全部和催办，不可审批。
- `handler`：承办人，可创建事项、上传材料、提交 A 领导。
- `leader_a`：A 领导，仅能审核指定给自己的事项。
- `leader_b`：B 领导，仅能审核指定给自己的事项。
- `viewer`：只读用户。

关键权限原则：

- 普通用户只能看自己参与的事项。
- A/B 领导必须是事项指定人，不能只靠角色审批。
- 分派只能走 `/api/items/{id}/assign`，不能通过普通编辑接口改参与人。
- 督办只能催办和查看，不能编辑、上传、审批。
- 终态事项不可继续上传、编辑、分派。

## 4. OA 登录适配逻辑

认证模式：

- `AUTH_MODE=local`：只用本系统账号。
- `AUTH_MODE=oa`：只用 OA 账号密码。
- `AUTH_MODE=mixed`：优先 OA；OA 服务不可用时，仅本地 admin 可回落维护登录。

OA 登录流程：

1. 前端提交账号密码到本系统 `/api/auth/login`。
2. 后端按 `AUTH_MODE` 分流。
3. OA 模式下，后端用 `httpx.Client` 临时请求 OA。
4. 登录接口默认路径：`/hportal/j_security_check`。
5. 表单字段：`j_username`、`j_password`、`remember`。
6. 登录后请求用户信息接口：`/hportal/view/GetModuleTree.do`。
7. 从 `userInfo` 中解析 `userCode`、`userName`、`departmentName` 等字段。
8. 用 OA `userCode` 查找或创建本地用户。
9. 已有用户只更新姓名、单位等信息，不覆盖本地角色。
10. 新 OA 用户默认角色由 `OA_DEFAULT_ROLE` 控制，建议先设为 `viewer`。
11. 本系统签发自己的 JWT。

安全原则：

- 不保存 OA 密码。
- 不保存 OA cookie。
- OA 密码只用于本次 HTTP 请求。
- cookie 只存在于内存中的 `httpx.Client`，请求结束即销毁。
- 不要把真实 OA 账号、密码、cookie、HAR 抓包提交到仓库。

## 5. OA 公文同步设计

用户希望：登录系统后，自动把 OA 里的以下模块列表同步进来，作为系统内工作事项入口。

OA 模块：

- 待办公文
- 待阅公文
- 已办公文
- 已阅公文
- 流转中公文

HAR 中观察到的模块编号：

- 待办公文：`DashboardID=882`
- 待阅公文：`DashboardID=883`
- 已办公文：`DashboardID=884`
- 已阅公文：`DashboardID=885`
- 流转中公文：`DashboardID=886`

HAR 中观察到的列表入口：

- 页面入口：`/hwf/worklist/TaskList.do`
- 列表数据接口：`/hmoa/s`

已观察到的服务名：

- `service=flowDealingList`
- `service=flowUnreadList`

已观察到的常见字段：

- `flowinid`
- `stepinco`
- `dealindx`
- `finsname`
- `docseq`
- `fileSrc`
- `recedate`
- `flowname`
- `stepname`
- `periname`
- `hasattach`
- `readFlag`
- `finiFlag`
- `sysurge`
- `openDate`

当前实现：

- 新增 `OAWorkItem` 表，保存每个用户自己的 OA 公文池。
- 登录后可选自动同步：`OA_SYNC_ON_LOGIN=true`。
- 手动同步接口：`POST /api/oa/sync`。
- 公文池列表接口：`GET /api/oa/items`。
- 公文池统计接口：`GET /api/oa/stats`。
- 从公文池创建协同事项：`POST /api/oa/items/{id}/create-collab`。
- 前端页面：`/oa_items.html`。

建议使用方式：

- 开发调试阶段可用 `AUTH_MODE=mixed`。
- 配好 `OA_BASE_URL` 后，先测试 OA 登录。
- 再启用 `OA_SYNC_ON_LOGIN=true`。
- 登录后进入“OA 公文池”，确认五类列表数据是否同步。
- 点“进入协同办理”后，生成系统内事项，再分派承办人、A/B 领导。

## 6. 2026-07-12 晚本轮修复和审查记录

本轮目标是修复 OA 公文同步上线前的安全和可靠性问题。已完成、已审查、已推送到 GitHub `main`。

相关提交：

- `643711a Harden OA sync account binding and failure handling`
- `13010b9 Clear OA sync password variable before request`

### 6.1 已修复：手动同步可串 OA 账号数据

位置：

- `app/routers/oa.py`
- `POST /api/oa/sync`

修复结果：

- 后端忽略请求体里的 `username`，统一使用当前登录用户 `user.username` 去登录 OA。
- 如果 OA 返回的 `profile.username` 与当前登录用户不一致，直接返回 403。
- 403 提示：`OA 账号与当前登录用户不一致，禁止同步他人公文`。
- 不匹配账号的数据不会写入 `OAWorkItem`。
- 代码里保留 TODO：未来如果确实存在“本地账号和 OA 编号不同”，应由管理员维护正式映射表后再放行。

新增测试：

- 当前用户 `handler1` 即使请求体传 `username=other_user`，后端仍用 `handler1` 调 OA。
- mock OA 返回 `profile.username=other_user` 时，接口返回 403，且 `OAWorkItem` 不新增。
- 正常手动同步仍可用。

### 6.2 已修复：手动同步密码输入不应使用 prompt

位置：

- `frontend/oa_items.html`

修复结果：

- 移除 `window.prompt()`。
- 改为页面内 modal。
- 密码框使用 `<input type="password">`。
- 点击确认后立即清空输入框。
- 构造请求体后马上清空 JS 变量 `pwd`，网络请求只使用已经构造好的 `payload`。
- 密码不进入 `localStorage`、`sessionStorage`、URL、日志。

二次审查发现过一个小问题：第一次修复时 `pwd` 变量在 `await api(...)` 返回后才清空。已通过提交 `13010b9` 修正为发请求前清空。

### 6.3 已修复：登录后自动同步入库失败可能影响登录

位置：

- `app/routers/auth.py`
- `_login_oa()`

修复结果：

- 对 `sync_oa_work_items()` 单独加 `try/except`。
- 入库失败时执行 `db.rollback()`。
- OA 登录成功后，即使公文入库失败，仍返回登录成功并签发 JWT。
- `oa_sync.enabled=true`。
- `oa_sync.success=false`。
- 返回给前端的错误为通用文案：`OA 登录成功但公文入库失败，请稍后重试或联系管理员`。
- 日志只记录异常类型，不输出密码、cookie、token 或完整请求内容。

新增测试：

- mock `authenticate_and_fetch_oa()` 成功返回列表。
- mock `sync_oa_work_items()` 抛异常。
- 登录仍返回 200，`oa_sync.success=false`，且敏感异常内容不会返回给前端。

### 6.4 已修复：离线构建脚本默认输出目录不通用

位置：

- `scripts/build-and-export.sh`
- `README.md`

修复结果：

- 默认 `OUT_DIR` 改为仓库内 `dist/`。
- 仍支持通过第一个参数指定输出目录。
- README 增加离线构建导出说明。
- `.gitignore` 已包含 `dist/`。

### 6.5 已修复：README 旧 OA 描述

位置：

- `README.md`

修复结果：

- 删除“OA sync 是 mock、不写库”的旧描述。
- 改为当前真实状态：OA 登录适配已实现，OA 公文池同步写入 `oa_work_items`。
- 说明主要接口：`/api/oa/items`、`/api/oa/stats`、`/api/oa/sync`、`/api/oa/items/{id}/create-collab`。
- 说明第一版限制：不下载附件、不读正文、不回写 OA。

## 7. OA 同步：诊断、过期清理与安全

### 7.1 能力摘要

- 表 `oa_sync_logs`：login/manual 同步诊断。
- 模块独立失败隔离；状态 success / partial / failed。
- **`is_active` 当前有效记录**：列表/统计只显示 active。
- **仅模块完整同步成功**（`success && complete && !truncated`）才停用本轮未出现的旧记录。
- 失败/截断模块：**不停用**旧数据。
- 已关联 `linked_item_id` 的记录只改 inactive，**不删事项**。
- 启动时 `migrate_schema(bind=engine)` 幂等补齐旧库 `is_active` + 常用查询索引（无需删 collab.db）。
- **is_active 升级失败**：日志仅中文说明 + 异常类型；ALTER 后复查；仍无字段则 `RuntimeError`，**容器启动失败**（禁止带坏结构继续跑）。
- 手动同步入库异常：**先 rollback** 再写 failed 日志。
- `sanitize_raw` 业务白名单 + 递归去敏感键；未知异常用通用中文；OA 路径不用 `logger.exception`。
- 真实旧库升级测试：`tests/test_migrate_schema.py`（临时 SQLite 建无 is_active 表 → 迁移 → 数据保留 → 失败硬停）。

### 7.2 模块 complete / truncated

- 空列表成功 → complete=true（可清空该模块 active）
- totalCount 存在：累计原始行数 >= totalCount → complete
- 无 totalCount：本页 raw_count < page_size → complete
- 达 max pages 未确认末页 → truncated=true, complete=false，**不清理**
- 任意页失败 → success=false, complete=false，**不清理**
- **总体状态**：存在 truncated 且无失败模块时仍为 **partial**（不可标 success）
- 默认 `OA_SYNC_PAGE_SIZE=10`（与 OA 每页 10 条一致）；`OA_SYNC_MAX_PAGES=3` 限制登录同步量

### 7.3 模拟 OA 预览环境（本轮已实现）

| 项 | 说明 |
|----|------|
| 模拟服务 | `app/mock_oa.py`（独立 uvicorn，**不挂主应用路由**） |
| Compose | `docker-compose.preview.yml`（不读正式 `.env`） |
| 启动/停止 | `bash scripts/preview-up.sh` / `bash scripts/preview-down.sh`（`--purge` 才删预览卷） |
| 主容器 | `collab-review-preview`，宿主机 **5010** |
| 模拟 OA 容器 | `collab-review-mock-oa`，**仅内部网络**，不映射办公网端口 |
| 正式服务 | 仍为 `collab-review-system` **5009**，预览脚本不得停止正式容器 |
| 镜像 | `collab-review-system:preview`（preview-up 每次重新 build） |
| 配置 | `DEBUG=true` `SEED_DEMO_USERS=true` `AUTH_MODE=oa` `OA_SYNC_ON_LOGIN=true` `OA_MOCK_ENABLED=true` `OA_BASE_URL=http://mock-oa:5099` |
| 条数 | todo23 / unread12 / done18 / read_done7 / running35 |
| 安全 | `OA_MOCK_ENABLED` 默认 false；`DEBUG=false` 且 mock=true 时拒绝启动；页面非弹窗标识 |

演示账号：`handler1` 等公开演示密码（见 README），**不得**使用真实 OA 密码。数据全部虚构，**禁止**提交 `oa.har`。

### 7.4 真实 OA 联调检查表（待内网验证）

> 本机未连接真实 OA，**不得声称已完成真实联调**。

1. 分别确认五类模块是否成功：todo / unread / done / read_done / running。
2. 记录每类返回数量（与 OA 端肉眼核对）。
3. 检查分页：`OA_SYNC_MAX_PAGES` 是否拉全；truncated 时前端提示未清理旧记录。
4. 同一公文重复同步：只更新、不重复新增。
5. 公文从待办到已办：待办列表不再显示，已办可见。
6. 已创建协同事项的 `linked_item_id` 不被覆盖；inactive 后事项仍可访问。
7. 若某模块失败：记录模块名称、HTTP 状态、同步记录短中文错误（勿提交响应/HAR）。
8. 严禁提交：真实 OA 账号密码、Cookie、Token、完整响应、HAR。
9. 调整 `OA_WORK_MODULES` 须以**内网最新 HAR** 为依据并补 mock 测试。

### 7.5 继续开发优先文件

- `app/services/oa_client.py` / `oa_sync.py` / `oa_auth.py`
- `app/database.py`（`migrate_schema`）
- `app/mock_oa.py` / `docker-compose.preview.yml` / `scripts/preview-*.sh`
- `app/routers/oa.py` / `auth.py`
- `frontend/oa_items.html` / `login.html`
- `tests/test_oa_sync.py` / `test_migrate_schema.py` / `test_mock_oa.py`

## 8. 后续路线建议

短期优先：

1. 按 7.4 检查表完成内网真实 OA 联调（预览栈已可本地验证五类公文与 truncated）。
2. 若某类公文不同步，抓 HAR 对比 `/hmoa/s` 参数后有依据地改 `OA_WORK_MODULES`。
3. 梳理本地账号与 OA `userCode` 是否一致；不一致则做管理员映射表，禁止任意 username。
4. 后台增量同步（不要在登录时一次拉上万条历史）。

中期建议：

1. 接入 ONLYOFFICE 文档编辑。
2. 主材料 docx 在线编辑、留痕、版本归档。
3. 从 OA 公文详情页同步附件或正文。
4. 支持办公室批量分派。
5. 支持事项按大队、标签、期限、领导节点筛选。

长期建议：

1. SQLite 换 PostgreSQL 或国产数据库。
2. 增加定时同步任务。
3. 增加全文检索。
4. 增加审计日志导出。
5. 与 OA 正式 SSO 或统一身份认证对接。

## 9. 敏感信息提醒

不要提交以下内容：

- 真实 OA 账号
- 真实 OA 密码
- 真实 cookie
- token
- 内网完整 HAR 抓包
- `.env`
- 数据库文件
- 上传文件

如需分析 HAR，应放在本机非仓库目录，或确保 `.gitignore` 已覆盖。
