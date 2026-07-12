"""OA 身份验证适配（不落库 cookie/密码，不记录敏感信息）。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class OAAuthError(Exception):
    """账号密码错误或 OA 明确拒绝登录。"""

    def __init__(self, message: str = "OA 账号或密码错误"):
        self.message = message
        super().__init__(message)


class OAAuthUnavailable(Exception):
    """OA 服务不可达、超时或响应异常。"""

    def __init__(self, message: str = "OA 服务暂不可用"):
        self.message = message
        super().__init__(message)


@dataclass
class OAUserProfile:
    username: str
    display_name: str
    unit: str | None
    department_code: str | None = None
    position_name: str | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


def _join_url(base: str, path: str) -> str:
    base = (base or "").rstrip("/") + "/"
    path = (path or "").lstrip("/")
    return urljoin(base, path)


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    ctype = (resp.headers.get("content-type") or "").lower()
    text = resp.text or ""
    if "html" in ctype or text.lstrip().lower().startswith("<!doctype") or text.lstrip().startswith("<html"):
        raise OAAuthError("OA 未返回有效用户信息，可能登录失败")
    try:
        data = resp.json()
    except Exception as exc:
        raise OAAuthUnavailable("OA 用户信息响应不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise OAAuthUnavailable("OA 用户信息响应格式异常")
    return data


def _parse_profile(data: dict[str, Any], fallback_username: str) -> OAUserProfile:
    user_info = data.get("userInfo")
    if not isinstance(user_info, dict) or not user_info:
        # 部分失败响应可能带 success=false
        if data.get("success") is False:
            raise OAAuthError("OA 登录失败")
        raise OAAuthError("OA 未返回用户信息")

    user_code = (user_info.get("userCode") or fallback_username or "").strip()
    if not user_code:
        raise OAAuthError("OA 用户编码为空")
    display = (user_info.get("userName") or user_code).strip()
    unit = user_info.get("departmentName")
    if isinstance(unit, str):
        unit = unit.strip() or None
    else:
        unit = None

    # 仅保留必要字段副本，避免把整棵模块树塞进内存日志
    slim = {
        "userCode": user_info.get("userCode"),
        "userName": user_info.get("userName"),
        "departmentName": user_info.get("departmentName"),
        "departmentCode": user_info.get("departmentCode"),
        "positionName": user_info.get("positionName"),
    }
    return OAUserProfile(
        username=str(user_code),
        display_name=str(display),
        unit=unit,
        department_code=str(user_info["departmentCode"]) if user_info.get("departmentCode") is not None else None,
        position_name=str(user_info["positionName"]) if user_info.get("positionName") is not None else None,
        raw=slim,
    )


def _optional_precheck(client: httpx.Client, base: str, username: str) -> None:
    """可选：部分 OA 环境登录前需探测用户编号（不校验密码）。"""
    settings = get_settings()
    if not settings.oa_precheck_enabled:
        return
    try:
        pki = _join_url(base, settings.oa_pki_path)
        client.post(pki, params={"userCode": username})
        num = _join_url(base, settings.oa_user_num_path)
        client.post(num, params={"userNum": username})
    except httpx.RequestError:
        # 预检失败不阻断，真正成败以 j_security_check + profile 为准
        logger.info("OA precheck skipped due to network error")


def authenticate_oa_user(username: str, password: str) -> OAUserProfile:
    """
    使用 OA 账号密码验证身份并拉取用户信息。
    - 不落库 cookie / token / 密码
    - 不向日志输出密码或 Cookie
    """
    settings = get_settings()
    base = (settings.oa_base_url or "").strip()
    if not base:
        raise OAAuthUnavailable("未配置 OA_BASE_URL")

    username = (username or "").strip()
    if not username or not password:
        raise OAAuthError("请输入 OA 账号和密码")

    timeout = httpx.Timeout(settings.oa_login_timeout_seconds)
    login_url = _join_url(base, settings.oa_login_path)
    profile_url = _join_url(base, settings.oa_profile_path)

    try:
        with httpx.Client(
            timeout=timeout,
            verify=settings.oa_verify_tls,
            follow_redirects=True,
        ) as client:
            _optional_precheck(client, base, username)

            login_resp = client.post(
                login_url,
                data={
                    "j_username": username,
                    "j_password": password,
                    "remember": "on",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            # 不记录 body / cookie；仅记状态码
            logger.info("OA login request finished status=%s", login_resp.status_code)

            if login_resp.status_code >= 500:
                raise OAAuthUnavailable("OA 登录服务异常")

            # 登录后通常应有会话 cookie；无 cookie 且后续 profile 失败则视为失败
            if not client.cookies:
                logger.info("OA login produced no cookies")

            profile_resp = client.post(profile_url)
            logger.info("OA profile request finished status=%s", profile_resp.status_code)

            if profile_resp.status_code >= 500:
                raise OAAuthUnavailable("OA 用户信息接口异常")
            if profile_resp.status_code in (401, 403):
                raise OAAuthError("OA 登录失败")
            if profile_resp.status_code >= 400:
                raise OAAuthError("OA 登录失败或无权访问")

            data = _safe_json(profile_resp)
            return _parse_profile(data, fallback_username=username)

    except (OAAuthError, OAAuthUnavailable):
        raise
    except httpx.TimeoutException as exc:
        raise OAAuthUnavailable("OA 登录超时") from exc
    except httpx.RequestError as exc:
        raise OAAuthUnavailable("无法连接 OA 服务") from exc
    except Exception as exc:
        # 兜底：不暴露内部细节与敏感信息
        logger.exception("OA auth unexpected error: %s", type(exc).__name__)
        raise OAAuthUnavailable("OA 认证过程发生异常") from exc
