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

_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|token|cookie|authorization|session|secret|credential)",
    re.I,
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


def _join_url(base: str, path: str) -> str:
    base = (base or "").rstrip("/") + "/"
    path = (path or "").lstrip("/")
    return urljoin(base, path)


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


def sanitize_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """过滤疑似敏感字段后再落库。"""
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if _SENSITIVE_KEY_RE.search(str(k)):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        else:
            # 嵌套结构只保留可序列化摘要
            try:
                json.dumps(v, ensure_ascii=False)
                out[k] = v
            except TypeError:
                out[k] = str(v)[:200]
    return out


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
    # 空串便于唯一键；对外仍可用 or None 展示
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
        # 可能是登录页
        if "login" in lower or "j_security" in lower or "用户" in text[:500]:
            raise OAAuthError("OA 会话失效，请重新登录")
        # 有些环境 content-type 标 html 但 body 是 JSON
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
) -> tuple[list[OAFetchedItem], int | None]:
    """拉取单模块单页。返回 (items, totalCount)。"""
    cfg = OA_WORK_MODULES.get(module_code)
    if not cfg:
        logger.info("unknown OA module_code=%s skipped", module_code)
        return [], None

    settings = get_settings()
    base = (settings.oa_base_url or "").strip()
    if not base:
        raise OAAuthUnavailable("未配置 OA_BASE_URL")

    url = _join_url(base, settings.oa_list_path)
    query = dict(cfg.get("query") or {})
    form_tpl = dict(cfg.get("form") or {})
    form = {k: (v.replace("{page}", str(page)) if isinstance(v, str) else v) for k, v in form_tpl.items()}

    resp = client.post(url, params=query, data=form)
    logger.info(
        "OA list module=%s page=%s status=%s",
        module_code,
        page,
        resp.status_code,
    )
    if resp.status_code >= 500:
        raise OAAuthUnavailable(f"OA 列表服务异常({module_code})")
    if resp.status_code in (401, 403):
        raise OAAuthError("OA 会话无效或无权访问列表")
    if resp.status_code >= 400:
        raise OAAuthUnavailable(f"OA 列表请求失败 status={resp.status_code}")

    data = _response_to_json(resp)
    rows = data.get("result") or []
    if not isinstance(rows, list):
        rows = []
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
    return items, total_i


def fetch_oa_work_items(
    client: httpx.Client,
    modules: list[str] | None = None,
    max_pages: int | None = None,
) -> list[OAFetchedItem]:
    """按配置拉取多个模块多页列表。"""
    settings = get_settings()
    mods = modules or settings.oa_sync_module_list or list(OA_WORK_MODULES.keys())
    pages = max_pages if max_pages is not None else settings.oa_sync_max_pages
    pages = max(1, min(int(pages), 20))

    all_items: list[OAFetchedItem] = []
    for code in mods:
        if code not in OA_WORK_MODULES:
            continue
        for page in range(1, pages + 1):
            batch, total = fetch_oa_module_page(client, code, page)
            all_items.extend(batch)
            if not batch:
                break
            if total is not None and page * max(len(batch), 1) >= total:
                break
            # 若本页不足一页体量，认为没有下一页
            if len(batch) < max(1, settings.oa_sync_page_size // 2):
                break
    return all_items
