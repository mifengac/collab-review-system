"""
纯虚构 OA 模拟服务（仅开发预览容器使用）。

- 不挂载到正式主应用路由
- 不复制真实 HAR 中的账号、人员、单位、文号、Cookie、Token
- 数据全部为「测试公文 / 测试单位 / 模拟承办人」等虚构内容
- 日志不打印密码、Cookie、Token 或完整请求体
- 列表参数与 OA_WORK_MODULES 严格一致，拒绝宽松猜测
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, Form, Query, Request, Response
from fastapi.responses import JSONResponse

from app.services.oa_client import OA_WORK_MODULES

logger = logging.getLogger(__name__)

# 公开演示账号（与主系统 SEED_DEMO_USERS 一致；密码不写入日志）
_DEMO_PASSWORD = "Demo@123456"
_ADMIN_PASSWORD = "Admin@123456"
MOCK_ACCOUNTS: dict[str, dict[str, str]] = {
    "handler1": {
        "password": _DEMO_PASSWORD,
        "userName": "模拟承办员甲",
        "departmentName": "测试单位一",
        "departmentCode": "MOCK-DEPT-01",
        "positionName": "模拟岗位",
    },
    "leader_a": {
        "password": _DEMO_PASSWORD,
        "userName": "模拟领导甲",
        "departmentName": "测试单位一",
        "departmentCode": "MOCK-DEPT-01",
        "positionName": "模拟领导岗",
    },
    "leader_b": {
        "password": _DEMO_PASSWORD,
        "userName": "模拟领导乙",
        "departmentName": "测试单位二",
        "departmentCode": "MOCK-DEPT-02",
        "positionName": "模拟领导岗",
    },
    "office1": {
        "password": _DEMO_PASSWORD,
        "userName": "模拟办公室员",
        "departmentName": "测试办公室",
        "departmentCode": "MOCK-DEPT-OFF",
        "positionName": "模拟文员",
    },
    "supervisor1": {
        "password": _DEMO_PASSWORD,
        "userName": "模拟督办员",
        "departmentName": "测试办公室",
        "departmentCode": "MOCK-DEPT-OFF",
        "positionName": "模拟督办",
    },
    "admin": {
        "password": _ADMIN_PASSWORD,
        "userName": "模拟管理员",
        "departmentName": "测试系统管理",
        "departmentCode": "MOCK-DEPT-SYS",
        "positionName": "模拟管理",
    },
}

# 五类模块数量（覆盖多页 / 截断场景）
MODULE_COUNTS: dict[str, int] = {
    "todo": 23,  # 待办：3 页可完整（10+10+3）
    "unread": 12,  # 待阅：2 页
    "done": 18,  # 已办：2 页
    "read_done": 7,  # 已阅：1 页
    "running": 35,  # 流转中：max_pages=3 时截断（30/35）
}

MODULE_NAMES: dict[str, str] = {
    "todo": "待办公文",
    "unread": "待阅公文",
    "done": "已办公文",
    "read_done": "已阅公文",
    "running": "流转中公文",
}

PAGE_SIZE = 10
_SESSION_COOKIE = "MOCK_OA_SESS"
# session_id -> username
_sessions: dict[str, str] = {}

app = FastAPI(
    title="Mock OA (dev only)",
    description="纯虚构 OA 模拟服务，仅用于开发预览",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)


def _base_time() -> datetime:
    return datetime(2026, 6, 1, 9, 0, 0)


def _build_item(module_code: str, index: int) -> dict[str, Any]:
    """生成单条纯虚构公文行（字段名与 OA 列表对齐）。"""
    n = index + 1
    base = _base_time() + timedelta(hours=index)
    flowinid = f"MOCK-{module_code.upper()}-{n:04d}"
    stepinco = f"MOCK-STEP-{module_code}-{n:03d}"
    dealindx = str((index % 5) + 1)
    title = f"测试公文-{MODULE_NAMES.get(module_code, module_code)}-{n:03d}"
    units = ["测试单位一", "测试单位二", "测试单位三", "模拟来文机关"]
    handlers = ["模拟承办人甲", "模拟承办人乙", "模拟承办人丙"]
    steps = {
        "todo": "模拟承办",
        "unread": "模拟待阅",
        "done": "模拟已办结",
        "read_done": "模拟已阅",
        "running": "模拟流转中",
    }
    read_flag = 1 if module_code in ("read_done", "done") else 0
    if module_code == "unread":
        read_flag = 0
    fini_flag = 1 if module_code == "done" else 0

    return {
        "flowinid": flowinid,
        "stepinco": stepinco,
        "dealindx": dealindx,
        "finsname": title,
        "docseq": f"测文〔2026〕{n:03d}号",
        "fileSrc": units[index % len(units)],
        "recedate": base.strftime("%Y-%m-%d %H:%M:%S"),
        "flowname": f"模拟流程-{MODULE_NAMES.get(module_code, module_code)}",
        "stepname": steps.get(module_code, "模拟步骤"),
        "periname": handlers[index % len(handlers)],
        "hasattach": "1" if index % 3 == 0 else "0",
        "readFlag": read_flag,
        "finiFlag": fini_flag,
        "sysurge": 1 if index % 7 == 0 else 0,
        "openDate": (base + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _dataset() -> dict[str, list[dict[str, Any]]]:
    data: dict[str, list[dict[str, Any]]] = {}
    for code, count in MODULE_COUNTS.items():
        data[code] = [_build_item(code, i) for i in range(count)]
    return data


_DATA = _dataset()


def _norm(value: str | None) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s != "" else None


def resolve_module_strict(
    service: str | None,
    task_type: str | None,
    read_flag: str | None,
) -> str | None:
    """
    严格按 OA_WORK_MODULES.query 匹配模块。
    缺少必要 taskType/readFlag 时不猜测，返回 None。
    不要求客户端传 noReportLog（仅业务键 service/taskType/readFlag）。
    """
    svc = _norm(service)
    tt = _norm(task_type)
    rf = _norm(read_flag)
    if not svc:
        return None

    for code, cfg in OA_WORK_MODULES.items():
        q = cfg.get("query") or {}
        req_service = _norm(str(q.get("service") or ""))
        if svc != req_service:
            continue
        # 模块定义中的 taskType / readFlag 必须全部精确匹配
        if "taskType" in q:
            if tt != _norm(str(q["taskType"])):
                continue
        elif tt is not None:
            # 定义未要求 taskType 时，不允许附带其他 taskType（当前五类均有 taskType）
            continue
        if "readFlag" in q:
            if rf != _norm(str(q["readFlag"])):
                continue
        # 定义无 readFlag 时：忽略客户端是否传 readFlag（done/todo 不依赖它）
        return code
    return None


def _param_error_response() -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "success": False,
            "message": "模拟 OA 请求参数不匹配",
        },
    )


def _session_user(request: Request) -> str | None:
    sid = request.cookies.get(_SESSION_COOKIE)
    if not sid:
        return None
    return _sessions.get(sid)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "mock-oa",
        "modules": {k: len(v) for k, v in _DATA.items()},
    }


@app.post("/hportal/j_security_check")
async def j_security_check(
    response: Response,
    j_username: str = Form(default=""),
    j_password: str = Form(default=""),
    remember: str = Form(default=""),
):
    username = (j_username or "").strip()
    password = j_password or ""
    # 不记录密码
    logger.info("mock OA login attempt user=%s", username)

    account = MOCK_ACCOUNTS.get(username)
    if not account or account["password"] != password:
        logger.info("mock OA login failed user=%s", username)
        return Response(content="login failed", media_type="text/plain", status_code=200)

    sid = secrets.token_urlsafe(24)
    _sessions[sid] = username
    response = Response(content="ok", media_type="text/plain", status_code=200)
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=sid,
        httponly=True,
        samesite="lax",
        path="/",
    )
    logger.info("mock OA login ok user=%s", username)
    return response


@app.post("/hportal/view/GetModuleTree.do")
async def get_module_tree(request: Request):
    username = _session_user(request)
    if not username:
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "未登录"},
        )
    account = MOCK_ACCOUNTS.get(username)
    if not account:
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "未登录"},
        )
    return {
        "success": True,
        "userInfo": {
            "userCode": username,
            "userName": account["userName"],
            "departmentName": account["departmentName"],
            "departmentCode": account["departmentCode"],
            "positionName": account["positionName"],
        },
    }


@app.post("/hmoa/s")
async def list_work_items(
    request: Request,
    service: str | None = Query(default=None),
    taskType: str | None = Query(default=None),
    readFlag: str | None = Query(default=None),
    noReportLog: str | None = Query(default=None),
    page: str = Form(default="1"),
    flowInstName: str = Form(default=""),
    showOnlyMe: str = Form(default="false"),
    orderOption: str = Form(default="1"),
):
    username = _session_user(request)
    if not username:
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "会话无效"},
        )

    page_raw = page
    try:
        form = await request.form()
        if "page" in form:
            page_raw = str(form.get("page") or page_raw)
    except Exception:
        pass

    try:
        page_i = max(1, int(str(page_raw).strip() or "1"))
    except (TypeError, ValueError):
        page_i = 1

    q = request.query_params
    svc = service if service is not None else q.get("service")
    tt = taskType if taskType is not None else q.get("taskType")
    rf = readFlag if readFlag is not None else q.get("readFlag")

    module = resolve_module_strict(svc, tt, rf)
    if not module:
        # 仅记录安全元信息，不打印 body / cookie
        logger.info(
            "mock OA list param mismatch service=%s taskType=%s readFlag=%s page=%s status=400",
            _norm(svc),
            _norm(tt),
            _norm(rf),
            page_i,
        )
        return _param_error_response()

    rows = _DATA.get(module) or []
    total = len(rows)
    start = (page_i - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    batch = rows[start:end]
    logger.info(
        "mock OA list service=%s taskType=%s readFlag=%s page=%s status=200",
        _norm(svc),
        _norm(tt),
        _norm(rf),
        page_i,
    )
    return {"result": batch, "totalCount": total}


def get_module_counts() -> dict[str, int]:
    """测试辅助：返回各模块条数。"""
    return {k: len(v) for k, v in _DATA.items()}


def clear_sessions() -> None:
    """测试辅助。"""
    _sessions.clear()
