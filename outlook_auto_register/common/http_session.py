from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from outlook_auto_register.config.constants import DEFAULT_UA
from outlook_auto_register.model.entity.register_models import SignupSession
from outlook_auto_register.service.resource.proxy.proxy_utils import proxy_for_requests
from outlook_auto_register.service.risk.px_cookies import apply_px_solution, cookie_map, get_px_cookies

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = int(os.environ.get("OUTLOOK_REG_HTTP_TIMEOUT", "90") or 90)
_RISK_TIMEOUT = int(os.environ.get("OUTLOOK_REG_RISK_TIMEOUT", "120") or 120)


class OutlookHttpSession:
    """带 canary 链管理的 HTTP 会话。"""

    def __init__(self, proxy: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": DEFAULT_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        # 住宅代理偶发 SSL EOF / 连接被重置 → 对「连接层」瞬时错误自动重试（GET/POST 均可）。
        # 只重试连接/读取错误（请求未真正送达服务端，安全）；不按状态码重试，
        # 避免对 CreateAccount(504) 这类非幂等 POST 重复提交造成重复注册。
        retry = Retry(
            total=3, connect=3, read=2, status=0, redirect=0,
            backoff_factor=1.0,
            allowed_methods=frozenset({"GET", "POST", "HEAD", "OPTIONS"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.proxies = proxy_for_requests(proxy)
        self.proxy = proxy
        self.signup_ctx: Optional[SignupSession] = None

    def get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("proxies", self.proxies)
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        return self.session.get(url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("proxies", self.proxies)
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        return self.session.post(url, **kwargs)

    def post_risk(self, url: str, **kwargs) -> requests.Response:
        """risk/verify 走住宅代理时偶发慢响应，单独放宽超时。"""
        kwargs.setdefault("proxies", self.proxies)
        kwargs.setdefault("timeout", _RISK_TIMEOUT)
        return self.session.post(url, **kwargs)

    def api_headers(self, ctx: SignupSession, *, origin: str = "https://signup.live.com") -> dict[str, str]:
        return {
            "canary": ctx.canary,
            "correlationId": ctx.uaid,
            "client-request-id": ctx.uaid,
            "hpgid": str(ctx.hpgid),
            "hpgact": "0",
            "Origin": origin,
            "Referer": ctx.signup_page_url,
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }

    def update_canary(self, ctx: SignupSession, resp_json: dict[str, Any]) -> None:
        new_canary = resp_json.get("apiCanary")
        if new_canary:
            ctx.canary = new_canary
            logger.debug("canary 已更新")

    def cookie_names(self) -> list[str]:
        return list(self.session.cookies.keys())

    def cookies_dict(self) -> dict[str, str]:
        return cookie_map(self.session)

    def px_cookies(self) -> dict[str, str]:
        return get_px_cookies(self.session)

    def apply_px_tokens(self, solution: dict[str, Any], *, preserve_vid: str = "") -> dict[str, str]:
        apply_px_solution(self.session, solution, preserve_vid=preserve_vid)
        return self.px_cookies()
