"""OA 公文列表拉取与字段归一化（不落库 cookie/密码）。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from app.config import get_settings
from app.services.oa_auth import OAAuthError, OAAuthUnavailable

logger = logging.getLogger(__name__)

# 模块请求策略（可按新 HAR 调整）
OA_WORK_MODULES: dict[str, dict[str, Any]] = {
    "todo": {
        "name": "待办公文",
        "service": "flowDealingList",
        "query": {"noReportLog": "1", "service": "flowDealingList"},
        "form": {
            "page": "{page}",
            "flowInstName": "",
            "showOnlyMe": "false",
            "orderOption": "1",
        },
    },
    "unread": {
        "name": "待阅公文",
        "service": "flowUnreadList",
        "query": {"noReportLog": "1", "service": "flowUnreadList"},
        "form": {
            "page": "{page}",
            "flowInstName": "",
            "showOnlyMe": "false",
            "orderOption": "1",
        },
    },
    "done": {
        "name": "已办公文",
        "service": "flowDealingList",
        "query": {"noReportLog": "1", "service": "flowDealingList", "taskType": "1"},
        "form": {"page": "{page}", "showOnlyMe": "false", "orderOption": "1"},
    },
    "read_done": {
        "name": "已阅公文",
        "service": "flowUnreadList",
        "query": {
            "noReportLog": "1",
            "service": "flowUnreadList",
            "taskType": "3",
            "readFlag": "1",
        },
        "form": {
            "page": "{page}",
            "flowInstName": "",
            "showOnlyMe": "false",
            "orderOption": "1",
        },
    },
    "running": {
        "name": "流转中公文",
        "service": "flowDealingList",
        "query": {
            "noReportLog": "1",
            "service": "flowDealingList",
            "taskType": "-1",
            "readFlag": "0",
        },
        "form": {"page": "{page}", "showOnlyMe": "false", "orderOption": "1"},
    },
}

# 敏感字段名（含子串匹配用）
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "cookie",
        "token",
        "authorization",
        "session",
        "secret",
        "credential",
        "j_password",
        "access_token",
        "refresh_token",
        "j_username",  # 账号也不写入 raw_json
    }
)
_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|pwd|cookie|token|authorization|session|secret|credential|j_password|access_token|refresh_token)",
    re.I,
)

# OA 列表行业务字段白名单（优先只保留这些）
_OA_ITEM_WHITELIST = frozenset(
    {
        "fileSrc",
        "docseq",
        "recedate",
        "finiFlag",
        "dealindx",
        "instCrda",
        "isurge",
        "dealMan",
        "fileSrcId",
        "readFlag",
        "stepname",
        "deal_man",
        "periname",
        "flowinid",
        "finsname",
        "worklist_itemex1",
        "stepinco",
        "worklist_itemex4",
        "hasattach",
        "worklist_itemex5",
        "worklist_itemex2",
        "worklist_itemex3",
        "instCrea",
        "flowname",
        "sysurge",
        "starLevel",
        "openDate",
    }
)


@dataclass
class OAFetchedItem:
    module_code: str
    module_name: str
    raw: dict[str, Any] = field(repr=False)
    flowinid: str
    stepinco: str | None = None
    dealindx: str | None = None
    title: str = ""
    doc_no: str | None = None
    source_unit: str | None = None
    flow_name: str | None = None
    step_name: str | None = None
    handler_name: str | None = None
    received_at: datetime | None = None
    open_date: datetime | None = None
    has_attach: bool = False
    read_flag: int | None = None
    fini_flag: int | None = None
    urgency: int | None = None

    @property
    def external_key(self) -> str:
        return "|".join(
            [
                self.module_code,
                self.flowinid or "",
                self.stepinco or "",
                self.dealindx or "",
            ]
        )


@dataclass
class OAModuleFetchResult:
    module_code: str
    module_name: str
    success: bool
    fetched: int = 0
    pages: int = 0
    imported: int = 0
    updated: int = 0
    deactivated: int = 0
    complete: bool = False
    truncated: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_code": self.module_code,
            "module_name": self.module_name,
            "success": self.success,
            "fetched": self.fetched,
            "pages": self.pages,
            "imported": self.imported,
            "updated": self.updated,
            "deactivated": self.deactivated,
            "complete": self.complete,
            "truncated": self.truncated,
            "error": self.error,
        }


@dataclass
class OAFetchReport:
    items: list[OAFetchedItem] = field(default_factory=list)
    module_results: list[OAModuleFetchResult] = field(default_factory=list)
    status: str = "failed"  # success | partial | failed
    error_summary: str | None = None


def _join_url(base: str, path: str) -> str:
    base = (base or "").rstrip("/") + "/"
    path = (path or "").lstrip("/")
    return urljoin(base, path)


def is_sensitive_key(key: Any) -> bool:
    k = str(key or "").strip().lower()
    if not k:
        return False
    if k in _SENSITIVE_KEY_NAMES:
        return True
    return bool(_SENSITIVE_KEY_RE.search(k))


def sanitize_value(value: Any) -> Any:
    """递归清洗任意结构中的敏感字段。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if is_sensitive_key(k):
                continue
            out[str(k)] = sanitize_value(v)
        return out
    if isinstance(value, list):
        return [sanitize_value(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(type(value).__name__)


def sanitize_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """
    列表行落库前清洗：优先业务白名单，并递归去除敏感键。
    """
    if not isinstance(raw, dict):
        return {}
    # 白名单优先
    filtered: dict[str, Any] = {}
    for k, v in raw.items():
        if k in _OA_ITEM_WHITELIST and not is_sensitive_key(k):
            if isinstance(v, (dict, list)):
                filtered[k] = sanitize_value(v)
            elif isinstance(v, (str, int, float, bool)) or v is None:
                filtered[k] = v
    return filtered


def safe_error_text(
    exc: BaseException | str | None = None,
    *,
    default: str = "OA 模块同步失败",
) -> str:
    """
    生成可落库/可返回前端的简短中文错误。
    - 已知 OAAuth* 使用受控 message
    - 未知异常不使用 str(exc)
    """
    if isinstance(exc, (OAAuthError, OAAuthUnavailable)):
        msg = (exc.message or default).strip() or default
    elif isinstance(exc, str):
        msg = exc.strip() or default
    elif exc is None:
        msg = default
    else:
        # 未知异常：不暴露 str(exc)
        msg = default

    if "<" in msg and ">" in msg:
        return default
    low = msg.lower()
    for bad in (
        "password",
        "passwd",
        "cookie",
        "token",
        "authorization",
        "bearer ",
        "j_password",
        "secret",
    ):
        if bad in low:
            return "同步过程出现错误（已隐藏敏感细节）"
    if len(msg) > 120:
        msg = msg[:120] + "…"
    return msg


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T")[:19])
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in {"1", "true", "yes", "y", "是"}


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def normalize_oa_item(
    module_code: str, module_name: str, raw: dict[str, Any]
) -> OAFetchedItem | None:
    """将 OA 列表行转为内部结构；缺 flowinid 则跳过。"""
    if not isinstance(raw, dict):
        return None
    flowinid = _str_or_none(raw.get("flowinid"))
    if not flowinid:
        return None

    title = (
        _str_or_none(raw.get("finsname"))
        or _str_or_none(raw.get("worklist_itemex1"))
        or f"OA公文-{flowinid}"
    )
    doc_no = _str_or_none(raw.get("docseq"))
    source_unit = _str_or_none(raw.get("fileSrc")) or _str_or_none(raw.get("worklist_itemex3"))
    stepinco = _str_or_none(raw.get("stepinco")) or ""
    dealindx = _str_or_none(raw.get("dealindx")) or ""
    flow_name = _str_or_none(raw.get("flowname"))
    step_name = _str_or_none(raw.get("stepname"))
    handler_name = _str_or_none(raw.get("periname")) or _str_or_none(raw.get("dealMan"))
    received_at = _parse_dt(raw.get("recedate"))
    open_date = _parse_dt(raw.get("openDate") or raw.get("worklist_itemex4"))
    has_attach = _as_bool(raw.get("hasattach"))
    read_flag = _as_int(raw.get("readFlag"))
    fini_flag = _as_int(raw.get("finiFlag"))
    urgency = _as_int(raw.get("sysurge"))

    return OAFetchedItem(
        module_code=module_code,
        module_name=module_name,
        raw=sanitize_raw(raw),
        flowinid=flowinid,
        stepinco=stepinco,
        dealindx=dealindx,
        title=title[:500],
        doc_no=doc_no,
        source_unit=source_unit,
        flow_name=flow_name,
        step_name=step_name,
        handler_name=handler_name,
        received_at=received_at,
        open_date=open_date,
        has_attach=has_attach,
        read_flag=read_flag,
        fini_flag=fini_flag,
        urgency=urgency,
    )


def _response_to_json(resp: httpx.Response) -> dict[str, Any]:
    text = resp.text or ""
    stripped = text.lstrip()
    lower = stripped.lower()
    if lower.startswith("<!doctype") or lower.startswith("<html"):
        if "login" in lower or "j_security" in lower or "用户" in text[:500]:
            raise OAAuthError("OA 会话失效，请重新登录")
    try:
        data = resp.json()
    except Exception:
        try:
            data = json.loads(text)
        except Exception as exc:
            raise OAAuthUnavailable("OA 列表响应不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise OAAuthUnavailable("OA 列表响应格式异常")
    return data


def fetch_oa_module_page(
    client: httpx.Client,
    module_code: str,
    page: int,
) -> tuple[list[OAFetchedItem], int | None, int]:
    """
    拉取单模块单页。
    返回 (normalized_items, totalCount, raw_row_count)。
    raw_row_count 用于分页完整性判断，避免归一化失败误判。
    """
    cfg = OA_WORK_MODULES.get(module_code)
    if not cfg:
        logger.info("unknown OA module_code=%s skipped", module_code)
        return [], None, 0

    settings = get_settings()
    base = (settings.oa_base_url or "").strip()
    if not base:
        raise OAAuthUnavailable("未配置 OA_BASE_URL")

    url = _join_url(base, settings.oa_list_path)
    query = dict(cfg.get("query") or {})
    form_tpl = dict(cfg.get("form") or {})
    form = {k: (v.replace("{page}", str(page)) if isinstance(v, str) else v) for k, v in form_tpl.items()}

    resp = client.post(url, params=query, data=form)
    # 仅记录模块、页码、状态码，不记录 body/url 参数细节
    logger.info(
        "OA list module=%s page=%s status=%s",
        module_code,
        page,
        resp.status_code,
    )
    if resp.status_code >= 500:
        raise OAAuthUnavailable("OA 列表服务异常")
    if resp.status_code in (401, 403):
        raise OAAuthError("OA 会话失效或无权访问该模块")
    if resp.status_code >= 400:
        raise OAAuthUnavailable(f"OA 列表请求失败（HTTP {resp.status_code}）")

    data = _response_to_json(resp)
    rows = data.get("result") or []
    if not isinstance(rows, list):
        rows = []
    raw_count = len(rows)
    total = data.get("totalCount")
    try:
        total_i = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_i = None

    module_name = str(cfg.get("name") or module_code)
    items: list[OAFetchedItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = normalize_oa_item(module_code, module_name, row)
        if item:
            items.append(item)
    return items, total_i, raw_count


def _fetch_one_module(
    client: httpx.Client,
    code: str,
    max_pages: int,
    page_size: int,
) -> tuple[list[OAFetchedItem], OAModuleFetchResult]:
    cfg = OA_WORK_MODULES[code]
    module_name = str(cfg.get("name") or code)
    items: list[OAFetchedItem] = []
    pages_done = 0
    total_count: int | None = None
    cumulative_raw = 0
    complete = False
    truncated = False

    try:
        for page in range(1, max_pages + 1):
            batch, total, raw_count = fetch_oa_module_page(client, code, page)
            pages_done += 1
            items.extend(batch)
            cumulative_raw += raw_count
            if total is not None:
                total_count = total

            # 空列表：模块确实为空或已到末页
            if raw_count == 0:
                complete = True
                break

            # totalCount 存在：用累计原始行数判断是否拉完
            if total_count is not None and cumulative_raw >= total_count:
                complete = True
                break

            # 无 totalCount：本页原始行数明显少于页大小，视为最后一页
            if total_count is None and raw_count < page_size:
                complete = True
                break

            # 达到最大页数仍未确认末页
            if page >= max_pages:
                truncated = True
                complete = False
                break

        return items, OAModuleFetchResult(
            module_code=code,
            module_name=module_name,
            success=True,
            fetched=len(items),
            pages=pages_done,
            complete=complete,
            truncated=truncated,
        )
    except Exception as exc:
        err = safe_error_text(exc, default="OA 模块同步失败")
        logger.info("OA module fetch failed code=%s err_type=%s", code, type(exc).__name__)
        return items, OAModuleFetchResult(
            module_code=code,
            module_name=module_name,
            success=False,
            fetched=len(items),
            pages=pages_done,
            complete=False,
            truncated=False,
            error=err,
        )


def _compute_report_status(module_results: list[OAModuleFetchResult]) -> str:
    if not module_results:
        return "failed"
    ok = sum(1 for m in module_results if m.success)
    fail = sum(1 for m in module_results if not m.success)
    if fail == 0:
        return "success"
    if ok == 0:
        return "failed"
    return "partial"


def fetch_oa_work_items_report(
    client: httpx.Client,
    modules: list[str] | None = None,
    max_pages: int | None = None,
) -> OAFetchReport:
    """按模块独立拉取；单模块失败不中断其他模块。"""
    settings = get_settings()
    mods = modules or settings.oa_sync_module_list or list(OA_WORK_MODULES.keys())
    pages = max_pages if max_pages is not None else settings.oa_sync_max_pages
    pages = max(1, min(int(pages), 20))
    page_size = max(1, int(settings.oa_sync_page_size or 20))

    all_items: list[OAFetchedItem] = []
    results: list[OAModuleFetchResult] = []
    for code in mods:
        if code not in OA_WORK_MODULES:
            results.append(
                OAModuleFetchResult(
                    module_code=code,
                    module_name=code,
                    success=False,
                    complete=False,
                    error="未知模块编码",
                )
            )
            continue
        items, result = _fetch_one_module(client, code, pages, page_size)
        all_items.extend(items)
        results.append(result)

    status = _compute_report_status(results)
    errors = [m.error for m in results if m.error]
    summary = None
    if status == "failed":
        summary = "；".join(errors[:3]) if errors else "全部模块同步失败"
    elif status == "partial":
        failed_names = [m.module_name for m in results if not m.success]
        summary = "部分模块同步失败：" + "、".join(failed_names)
    truncated_names = [m.module_name for m in results if m.success and m.truncated]
    if truncated_names:
        extra = "分页未拉完（未清理旧记录）：" + "、".join(truncated_names)
        summary = f"{summary}；{extra}" if summary else extra

    return OAFetchReport(
        items=all_items,
        module_results=results,
        status=status,
        error_summary=summary,
    )


def fetch_oa_work_items(
    client: httpx.Client,
    modules: list[str] | None = None,
    max_pages: int | None = None,
) -> list[OAFetchedItem]:
    """兼容旧接口：仅返回已拉取条目列表。"""
    return fetch_oa_work_items_report(client, modules=modules, max_pages=max_pages).items
