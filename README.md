# 材料协同办理系统（collab-review-system）

公安内网材料协同审核 MVP：替代「打印材料 → 领导手写修改 → 办事员改稿 → 再打印」的反复纸质流转。

> 第一版目标：**事项创建、文件上传与版本留痕、A/B 领导审核流转** 能完整跑通。  
> 不做 OA 回写、不做真正在线 Office（仅预留接口），不替代现有 OA。

## 功能概览

| 模块 | 说明 |
|------|------|
| 登录鉴权 | JWT Bearer；密码 bcrypt 哈希存储 |
| 工作台 | 我的待办 / 我发起的 / 即将逾期（3 日内） |
| 协同事项 | 标题、OA 文号、来文单位、承办大队、业务标签、紧急程度、截止时间、承办人、A/B 领导、备注 |
| 状态机 | 草稿 → 承办中 → A领导审核中 ⇄ A领导退回 → B领导审核中 ⇄ B领导退回 → 已定稿 → 已归档 / 已作废 |
| 文件版本 | 主材料 docx；附件 docx/xlsx/pdf/jpg/png；每次上传新版本；记录 sha256，历史不可覆盖 |
| 操作留痕 | 创建/上传/下载/提交/通过/退回/定稿/归档 等时间线 |
| 字典 | 四个大队 + 15 个业务标签预置 |
| ONLYOFFICE 预留 | `GET /api/documents/{id}/editor-config`、`POST /api/office/callback/{document_id}` |
| OA 预留 | 表字段 + `GET /api/oa/inbox`、`POST /api/oa/sync`（mock） |

## 技术栈

- 后端：Python 3.11 + FastAPI + SQLAlchemy 2.x
- 数据库：默认 **SQLite**（本地零依赖）；`DATABASE_URL` 可切 PostgreSQL / Kingbase（兼容协议）
- 前端：纯静态 HTML + CSS + 原生 JS（无 React/Vue/Node 构建，无外网 CDN）
- 部署：Docker + docker-compose，默认端口 **5009**

## 目录结构

```
collab-review-system/
├── app/
│   ├── main.py           # 入口
│   ├── config.py         # 配置
│   ├── database.py       # 引擎 / 会话
│   ├── models.py         # ORM
│   ├── schemas.py        # Pydantic
│   ├── auth.py           # 密码哈希 + JWT
│   ├── routers/          # API
│   └── services/         # 种子数据、流转、文件
├── frontend/             # 静态页面
├── uploads/              # 上传文件（按事项 ID 分子目录）
├── tests/                # pytest smoke
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

## 默认账号

| 用户名 | 默认密码 | 角色 | 说明 |
|--------|----------|------|------|
| `admin` | `Admin@123456` | 管理员 | 可通过 `.env` 修改（仅首次初始化生效） |
| `office1` | `Demo@123456` | 办公室收文员 | **仅** `SEED_DEMO_USERS=true` |
| `supervisor1` | `Demo@123456` | 督办人员 | **仅** `SEED_DEMO_USERS=true` |
| `handler1` | `Demo@123456` | 承办人 | **仅** `SEED_DEMO_USERS=true` |
| `leader_a` | `Demo@123456` | A 领导 | **仅** `SEED_DEMO_USERS=true` |
| `leader_b` | `Demo@123456` | B 领导 | **仅** `SEED_DEMO_USERS=true` |

- `SEED_DEMO_USERS` 默认 `false`，**仅用于本地演示**，生产环境务必关闭。
- 密码均 bcrypt 哈希存储。请勿写入真实 OA 账号/cookie/token。

## 角色与权限

| 角色 | 查看范围 | 新建/编辑/上传 | 分派 | 审批 | 督办 | 用户管理 |
|------|----------|----------------|------|------|------|----------|
| `admin` | 全部 | 是 | 是 | 可代操作 | 是 | 增删改 |
| `office_clerk` | 全部 | 是（未终态） | 是 | 否（除非本人是指定领导） | 是 | 只读列表 |
| `supervisor` | 全部 | 否 | 否 | 否 | 是 | 否 |
| `handler` | 参与事项 | 自己承办的 | 否 | 提交 A | 否 | 否 |
| `leader_a` / `leader_b` | 参与事项 | 否 | 否 | 仅指定给自己的 | 否 | 否 |
| `viewer` | 参与事项 | 否 | 否 | 否 | 否 | 否 |

- **可创建事项的角色**：仅 `admin` / `office_clerk` / `handler`。督办、领导、只读不可创建。
- **参与人调整**：必须走 `POST /api/items/{id}/assign`。`PUT /api/items/{id}` 只改基本字段；若携带 `handler_id` / `leader_*_id` 返回 400。
- **编辑/上传**：仅 admin、办公室，或 **角色为 handler 且为该事项承办人**。督办即使误被设为 `handler_id` 也不可编辑/上传。
- **分派校验**：目标用户须启用；承办人必须是 `handler`，A 领导须 `leader_a`，B 领导须 `leader_b`。
- **终态**（已定稿/已归档/已作废）：禁止编辑、上传、分派。
- **作废**：须填写原因；允许 admin / office_clerk / 承办人（创建人或承办）。
- **A/B 审核**：非管理员必须是事项上指定的 `leader_a_id` / `leader_b_id`。
- **今日定稿统计**：按当日 `定稿` 操作日志计数，而非按归档时间。

## 办公室分派流程

1. 办公室收文员（`office_clerk`）登录 → **新建事项**（可暂不上传主材料）。
2. 在事项详情点击 **分派/调整参与人**，或创建时直接指定承办人与 A/B 领导。
3. 接口：`POST /api/items/{id}/assign`  
   - 承办中 / A·B 退回：可调承办人、A、B  
   - A 审核中：仅可调 A 领导  
   - B 审核中：仅可调 B 领导  
   - 终态：不可调  
   - 目标用户角色与启用状态会被校验  
4. 承办人处理材料并 **提交 A 领导** → A 通过 → B 定稿。
5. 调整操作写入时间线（`分派调整`），记录调整前后人员。
6. 编辑事项表单 **不会** 通过 PUT 提交参与人字段。

## 督办催办

- 接口：`POST /api/items/{id}/supervise`，body：`{"comment":"..."}`（必填）。
- 角色：`admin` / `office_clerk` / `supervisor`。
- 只写时间线，**不改变事项状态**。
- 详情页对上述角色显示「督办催办」按钮。

## 用户管理

- `GET /api/auth/user-options`：登录用户可选人（精简字段）。
- `GET /api/auth/users`：admin 看全部；office_clerk 看启用用户；其他人 403。
- `POST/PATCH /api/auth/users`：仅 admin。
- 前端「系统设置」页：管理员可新增用户、改角色/单位/启用、重置密码。

## 本地运行

环境：Ubuntu 22.04+，Python 3.11 推荐。

```bash
cd collab-review-system

# 1. 虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置
cp .env.example .env
# 按需修改 SECRET_KEY、管理员密码等

# 3. 启动（端口 5009）
uvicorn app.main:app --host 0.0.0.0 --port 5009 --reload
```

浏览器访问：http://127.0.0.1:5009/login.html

- 数据库文件：`./data/collab.db`（自动创建）
- 上传目录：`./uploads/`（或 `.env` 中 `UPLOAD_DIR`）
- API 文档：http://127.0.0.1:5009/docs

### 本地测试

```bash
source .venv/bin/activate
pytest -q
```

## Docker 运行

```bash
cd collab-review-system
cp .env.example .env
# 务必修改 SECRET_KEY 与 ADMIN_PASSWORD
# docker-compose.yml 通过 env_file: .env 读取该文件；environment 中的默认值作兜底

docker compose up -d --build
```

访问：http://服务器IP:5009/login.html

说明：

- `docker-compose.yml` 已启用 `env_file: .env`，请先复制 `.env.example` 再启动。
- 同名变量优先级：compose `environment` 插值 / 宿主机环境变量 与 `env_file` 共同注入容器；请在 `.env` 中配置生产密钥。
- `SEED_DEMO_USERS` 生产请保持 `false`。

数据与上传文件使用命名卷：

- `collab_data` → 容器内 `/app/data`
- `collab_uploads` → 容器内 `/app/uploads`

查看日志：

```bash
docker compose logs -f
```

停止：

```bash
docker compose down
```

## 切换 PostgreSQL / Kingbase

修改 `.env`：

```env
# PostgreSQL
DATABASE_URL=postgresql://user:password@host:5432/collab_review

# Kingbase（多数环境兼容 PostgreSQL 协议驱动）
DATABASE_URL=postgresql://user:password@host:54321/collab_review
```

应用使用标准 SQLAlchemy URL，模型未绑定 SQLite 专有语法（外键、枚举等在 PG 上可用）。生产建议再引入 Alembic 管理迁移。

## 审核流转说明

```
创建 → 承办中
      ↓ 承办人「提交 A 领导」
  A领导审核中 ──退回(必填意见)──→ A领导退回 ──再提交──┐
      ↓ 通过                                         │
  B领导审核中 ──退回(必填意见)──→ B领导退回 ──────────┘
      ↓ 定稿
    已定稿 → 归档
未终态可「作废」（须填原因）
```

每次操作写入 `action_logs`，事项详情页「流转时间线」展示。

### 建议试用路径

1. `SEED_DEMO_USERS=true` 启动后，用 `office1` 登录 → 新建事项 → 分派 handler1 / leader_a / leader_b。  
2. `handler1` 上传 docx → 提交 A 领导。  
3. `supervisor1` 查看事项 → 写督办催办 → 确认不能审批/上传。  
4. `leader_a` 通过 → `leader_b` 定稿。  
5. `admin` 在系统设置中新增用户、调整角色。

## 在线文档预留（ONLYOFFICE）

当前**未集成** ONLYOFFICE。已预留：

- `GET /api/documents/{id}/editor-config` — 返回 reserved 配置骨架
- `POST /api/office/callback/{document_id}` — 回调占位，返回 ok
- 前端按钮「在线编辑（预留）」

后续步骤建议：

1. 内网单独部署 ONLYOFFICE Docs Document Server  
2. 实现真实 `editor-config`（文档下载 URL、JWT、callback）  
3. 在 callback 中拉取编辑后文件并 `save_upload` 生成新版本  

## OA 预留

表 `items` 字段：`oa_flow_id`、`oa_step_id`、`oa_deal_index`、`oa_raw_title`、`oa_raw_doc_no`。

接口：

- `GET /api/oa/inbox` — 当前返回一条 mock 待办  
- `POST /api/oa/sync` — 返回成功提示，不写库  

**不要**提交真实 OA 抓包、cookie、账号密码。对接时建议独立 `services/oa_client.py`，凭据仅放运行环境变量。

## 安全与内网部署注意

- 不依赖公网 CDN / 外网字体  
- `.env` 已在 `.gitignore`，勿提交  
- 修改默认 `SECRET_KEY` 与管理员密码  
- 上传目录仅服务端读写，文件名做了安全处理  
- 反向代理（nginx）可按需加 HTTPS / 内网 IP 白名单  

## 主要 API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录 |
| GET | `/api/auth/me` | 当前用户 |
| GET | `/api/auth/user-options` | 选人列表（登录即可） |
| GET/POST/PATCH | `/api/auth/users` | 用户管理（创建/改仅 admin） |
| GET | `/api/items/dashboard` | 工作台 + 统计 |
| GET/POST | `/api/items` | 列表 / 新建 |
| GET/PUT | `/api/items/{id}` | 详情 / 更新 |
| POST | `/api/items/{id}/assign` | 分派/调整参与人 |
| POST | `/api/items/{id}/supervise` | 督办催办 |
| POST | `/api/items/{id}/submit-a` 等 | 流转动作 |
| GET | `/api/items/{id}/timeline` | 时间线 |
| POST | `/api/items/{id}/upload` | 上传文件 |
| GET | `/api/versions/{id}/download` | 下载指定版本 |
| GET | `/api/dict/departments` | 大队字典 |
| GET | `/api/dict/tags` | 业务标签 |
| GET | `/api/oa/inbox` | OA 收件箱（预留） |
| POST | `/api/oa/sync` | OA 同步（预留） |
| GET | `/api/health` | 健康检查 |

## License

仅供内网办公演示与二次开发，请按单位规范部署与审计。
