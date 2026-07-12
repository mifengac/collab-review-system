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

当前最近提交：

- `73295de Add OA work item synchronization`
- `4e584ee Add optional OA authentication adapter`
- `e7b2f05 Close assignment and creation permission gaps`
- `85bc9b7 Add office assignment and supervision workflow`
- `f9e20b5 Harden item permissions and upload validation`
- `2ef8aa2 Initial MVP for collaboration review system`

当前测试结果：

- `pytest -q`
- 结果：`49 passed`

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

## 6. 当前审查发现的问题

以下是最近一次代码审查发现，需要下一轮优先修：

### 6.1 高优先级：手动同步可串 OA 账号数据

位置：

- `app/routers/oa.py`
- `POST /api/oa/sync`

问题：

- 接口允许前端传 `username`。
- 后端发现 OA 返回账号和当前登录账号不一致时，没有拒绝，而是直接放行。
- 这可能导致当前用户用别人的 OA 账号密码，把别人的 OA 公文同步进自己的公文池。

建议修复：

- 手动同步不允许传任意 username。
- 默认只使用当前登录用户 `user.username`。
- 如果 OA 返回的 `profile.username != user.username`，直接返回 403。
- 如果单位存在“系统账号和 OA 编号不同”的情况，应新增正式账号映射字段或映射表，由管理员维护。

建议新增测试：

- 当前用户 `handler1` 调用 `/api/oa/sync`，mock OA 返回 `profile.username="other_user"`，应返回 403。
- 确认没有写入 `OAWorkItem`。

### 6.2 中高优先级：手动同步密码输入不应使用 prompt

位置：

- `frontend/oa_items.html`

问题：

- 当前用 `window.prompt()` 输入 OA 密码。
- prompt 是普通文本输入，不适合密码场景。
- 在办公室、投屏、旁人经过时容易暴露密码。

建议修复：

- 改为页面内弹窗。
- 使用 `<input type="password">`。
- 提交后立即清空输入框和变量。
- 不写入 `localStorage`、`sessionStorage`、URL。

### 6.3 中优先级：登录后自动同步入库失败可能影响登录

位置：

- `app/routers/auth.py`
- `_login_oa()`

问题：

- OA 列表拉取失败已经做了“不影响登录”的处理。
- 但 `sync_oa_work_items()` 入库失败没有兜底。
- 如果数据库锁、字段异常、唯一键异常，可能导致 OA 登录直接 500。

建议修复：

- 对 `sync_oa_work_items()` 加 `try/except`。
- 失败时 `db.rollback()`。
- 仍然返回登录成功和 JWT。
- `oa_sync` 中返回 `success=false` 和简短错误。
- 日志只记录异常类型，不输出密码、cookie、token。

建议新增测试：

- mock `authenticate_and_fetch_oa()` 成功返回列表。
- mock `sync_oa_work_items()` 抛异常。
- 登录仍应 200，`oa_sync.success=false`。

### 6.4 低优先级：离线构建脚本默认输出目录不通用

位置：

- `scripts/build-and-export.sh`

问题：

- 默认输出目录是个人 Windows 路径。
- 在内网 Ubuntu 上可能不存在或不可写。

建议修复：

- 默认输出到仓库内 `dist/`。
- 需要桌面目录时，手动传参数。

## 7. 推荐给 Grok 的下一轮开发提示词

可以直接复制下面提示词给 Grok：

```text
你现在继续开发 GitHub 仓库 mifengac/collab-review-system，本地目录是 /home/longshao/project/collab-review-system。

目标：修复 OA 公文同步上线前的安全和可靠性问题。不要重构无关代码，不要提交 .env、数据库、上传文件、真实 OA 账号密码、cookie、HAR。

请完成以下修改：

1. 修复手动 OA 同步串号风险
   - 接口：POST /api/oa/sync
   - 不允许普通用户通过请求体指定任意 OA username。
   - 手动同步默认只能使用当前登录用户 user.username。
   - 如果 authenticate_and_fetch_oa 返回的 profile.username 与当前登录用户 user.username 不一致，返回 403，提示“OA 账号与当前登录用户不一致，禁止同步他人公文”。
   - 不要把不匹配账号的数据写入 OAWorkItem。
   - 如果未来要支持账号映射，请只在代码注释或 TODO 中说明，不要现在放行。

2. 修改前端 OA 密码输入
   - 文件：frontend/oa_items.html
   - 不要再用 window.prompt 输入 OA 密码。
   - 改为页面内弹窗或简洁 modal，使用 <input type="password">。
   - 提交后立即清空输入框和 JS 变量。
   - 密码不得进入 localStorage、sessionStorage、URL、日志。
   - 保持现有中文界面风格。

3. 修复登录后自动同步入库失败会影响登录的问题
   - 文件：app/routers/auth.py
   - 在 _login_oa() 中，对 sync_oa_work_items() 单独 try/except。
   - 如果 OA 登录成功、列表拉取成功，但入库失败：
     - db.rollback()
     - 仍然登录成功并签发 JWT
     - oa_sync.enabled=true
     - oa_sync.success=false
     - oa_sync.error 返回“OA 登录成功但公文入库失败，请稍后重试或联系管理员”
   - 日志只能记录异常类型，不要输出密码、cookie、token。

4. 调整离线构建脚本默认输出目录
   - 文件：scripts/build-and-export.sh
   - 默认 OUT_DIR 改为仓库内 dist/。
   - 保留传入第一个参数覆盖输出目录的能力。
   - README 或部署说明同步更新。

5. 补充测试
   - 当前用户 handler1 手动同步时，mock OA 返回 profile.username=other_user，应返回 403，且 OAWorkItem 不新增。
   - 登录后自动同步时，mock sync_oa_work_items 抛异常，登录仍返回 200，oa_sync.success=false。
   - 确认正常手动同步仍可用。

完成后运行：
pytest -q

提交并推送到 GitHub main 分支，提交信息建议：
Harden OA sync account binding and failure handling

最后汇报：
- 修改了哪些文件
- 修复了哪些风险
- 新增了哪些测试
- pytest 结果
- 是否已 push
```

## 8. 后续路线建议

短期优先：

1. 先修第 6 节三个安全/可靠性问题。
2. 在内网用真实 OA 账号测试五类公文池是否都有数据。
3. 如果某一类公文不同步，抓取该模块 HAR，对比 `/hmoa/s` 的 `service`、query、form 参数。
4. 增加“同步日志”页面，方便看每次同步成功、失败、拉取数量、错误摘要。

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
