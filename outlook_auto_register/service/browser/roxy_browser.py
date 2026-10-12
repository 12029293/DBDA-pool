"""RoxyBrowser local API client and Playwright CDP session helpers."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import requests

from outlook_auto_register.config.constants import locale_for_country
from outlook_auto_register.service.resource.proxy.proxy_utils import infer_country_from_template, parse_proxy, timezone_for_country


DEFAULT_ROXY_API = "http://127.0.0.1:50000"


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RoxyProfile:
    id: str
    name: str
    remark: str = ""


def proxy_info_from_line(proxy_line: str) -> dict[str, Any]:
    """Build Roxy ``proxyInfo`` from host:port:user:pass or URL."""
    cfg = parse_proxy((proxy_line or "").strip())
    if not cfg or not cfg.host or not cfg.port:
        return {
            "moduleId": 0,
            "proxyMethod": "custom",
            "proxyCategory": "noproxy",
            "ipType": "IPV4",
        }
    category = "HTTP"
    if cfg.scheme.startswith("socks"):
        category = "SOCKS5"
    elif cfg.scheme == "https":
        category = "HTTPS"
    return {
        "moduleId": 0,
        "proxyMethod": "custom",
        "proxyCategory": category,
        "ipType": "IPV4",
        "host": cfg.host,
        "port": str(cfg.port),
        "proxyUserName": cfg.username or "",
        "proxyPassword": cfg.password or "",
        "checkChannel": "IPRust.io",
    }


def infer_zone_country(proxy_line: str) -> str:
    """Parse IPWO-style ``custom_zone_XX`` from proxy username."""
    cfg = parse_proxy((proxy_line or "").strip())
    user = cfg.username if cfg else ""
    m = re.search(r"custom_zone_([A-Za-z]{2,8})", user or "", re.I)
    if not m:
        return infer_country_from_template(proxy_line)
    zone = m.group(1).upper()
    if zone in {"GLOBAL", "WORLD", "ALL"}:
        return ""
    if len(zone) == 2:
        return zone
    aliases = {"USA": "US", "UK": "GB", "PHILIPPINES": "PH"}
    return aliases.get(zone, zone[:2] if len(zone) >= 2 else "")


def _bcp47_language(mkt: str) -> str:
    """Roxy expects BCP-47 tags like ``en-US``, not ``EN-US``."""
    mkt = (mkt or "EN-US").strip()
    if "-" in mkt:
        lang, region = mkt.split("-", 1)
        return f"{lang.lower()}-{region.upper()}"
    return mkt.lower()


def finger_info_for_native_ui(country: str) -> dict[str, Any]:
    """Fingerprint for RoxyNativeUI batch: English UI + proxy-country timezone.

    Native keyboard coordinates and title matching assume English signup pages.
    Proxy exit still follows ``country``; only display language is pinned to
    ``en-US`` so DE/FR sticky IPs do not render German/French forms.
    """
    cc = (country or "US").strip().upper()
    return {
        "isLanguageBaseIp": False,
        "isDisplayLanguageBaseIp": False,
        "language": "en-US",
        "displayLanguage": "en-US",
        "isTimeZone": True,
        "isPositionBaseIp": True,
        "timeZone": timezone_for_country(cc),
    }


def finger_info_for_country(country: str, *, follow_ip: bool = False) -> dict[str, Any]:
    """Build Roxy ``fingerInfo`` for a country.

    IPWO ``GLOBAL`` 代理出口国家随机，``follow_ip=True`` 会把浏览器语言切成乌兹别克语等，
    导致原生键盘脚本无法识别英文标题。批量注册应使用 ``follow_ip=False``（默认）并按
    ``--countries`` 显式设置 ``en-US`` / ``en-PH`` 等 BCP-47 语言。
    """
    if follow_ip:
        return {
            "isLanguageBaseIp": True,
            "isDisplayLanguageBaseIp": True,
            "isTimeZone": True,
            "isPositionBaseIp": True,
        }
    cc = (country or "US").strip().upper()
    mkt, _lc = locale_for_country(cc)
    lang = _bcp47_language(mkt)
    return {
        "isLanguageBaseIp": False,
        "isDisplayLanguageBaseIp": False,
        "language": lang,
        "displayLanguage": lang,
        "isTimeZone": True,
        "isPositionBaseIp": False,
        "timeZone": timezone_for_country(cc),
    }


class RoxyBrowserClient:
    """Small client for Roxy's local browser API.

    Roxy profiles own their fingerprint and proxy configuration. This client
    deliberately does not overwrite those fields.
    """

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        workspace_id: int = 0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("ROXY_API") or DEFAULT_ROXY_API).rstrip("/")
        self.token = (token or os.environ.get("ROXY_TOKEN") or os.environ.get("ROXY_API_KEY") or "").strip()
        self.workspace_id = int(workspace_id or _env_int("ROXY_WORKSPACE_ID"))
        self.session = requests.Session()
        self.session.trust_env = False
        self.last_error = ""

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        if not self.token:
            raise ValueError("Roxy API token 未配置（ROXY_TOKEN / ROXY_API_KEY）")
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        headers = {"token": self.token}
        if body is not None:
            headers["content-type"] = "application/json"
        response = self.session.request(
            method.upper(),
            url,
            params=params,
            json=body,
            headers=headers,
            timeout=timeout,
            proxies={"http": None, "https": None},
        )
        response.raise_for_status()
        if not response.content:
            return {}
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Roxy {path} 返回了非对象响应")
        if data.get("code") not in (0, None):
            detail = data.get("msg") or json.dumps(data, ensure_ascii=False)[:300]
            raise RuntimeError(f"Roxy {path} 失败: {detail}")
        return data

    def health(self) -> bool:
        try:
            self._request("GET", "/health", timeout=5)
            self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False

    def resolve_workspace_id(self) -> int:
        if self.workspace_id > 0:
            return self.workspace_id
        data = self._request("GET", "/browser/workspace", timeout=15)
        rows = (data.get("data") or {}).get("rows") or data.get("data") or []
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            raise RuntimeError("未找到 Roxy workspace，请设置 ROXY_WORKSPACE_ID")
        workspace_id = int(rows[0].get("id") or 0)
        if workspace_id <= 0:
            raise RuntimeError("Roxy workspace 返回了无效 id")
        self.workspace_id = workspace_id
        return workspace_id

    def list_profiles(self, *, page_size: int = 200) -> list[RoxyProfile]:
        data = self._request(
            "GET",
            "/browser/list",
            params={
                "workspaceId": self.resolve_workspace_id(),
                "pageIndex": 1,
                "pageSize": page_size,
            },
            timeout=15,
        )
        rows = (data.get("data") or {}).get("rows") or []
        profiles: list[RoxyProfile] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            profile_id = str(row.get("dirId") or "").strip()
            if profile_id:
                profiles.append(
                    RoxyProfile(
                        id=profile_id,
                        name=str(row.get("windowName") or "").strip() or profile_id[:8],
                        remark=str(row.get("remark") or "").strip(),
                    )
                )
        return profiles

    def open(self, profile_id: str) -> str:
        profile_id = str(profile_id or "").strip()
        if not profile_id:
            raise ValueError("Roxy profile id 不能为空")
        data = self._request(
            "POST",
            "/browser/open",
            body={
                "workspaceId": self.resolve_workspace_id(),
                "dirId": profile_id,
                # 注册自动化不应被 Chrome 原生“Save password?”气泡遮挡。
                "args": [
                    "--remote-allow-origins=*", "--lang=en-US",
                    "--disable-save-password-bubble",
                    "--disable-features=PasswordManagerOnboarding,PasswordLeakDetection",
                    "--password-store=basic",
                ],
                "headless": False,
            },
            timeout=180,
        )
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        ws = str((payload or {}).get("ws") or "").strip()
        if not ws:
            raise RuntimeError(f"Roxy open 未返回 CDP ws: {json.dumps(data, ensure_ascii=False)[:300]}")
        return ws

    def close(self, profile_id: str) -> None:
        try:
            self._request("POST", "/browser/close", body={"dirId": profile_id}, timeout=30)
        except Exception:  # noqa: BLE001
            pass

    def clear_profile_cookies(self, profile_id: str) -> None:
        """清空 profile 的本地/云端 Cookie，保留指纹与代理配置。"""
        workspace_id = self.resolve_workspace_id()
        self._request(
            "POST", "/browser/clear_local_cache",
            body={"dirIds": [profile_id], "type": "cloud", "workspaceId": workspace_id},
            timeout=60,
        )
        for path, body in (
            ("/browser/clear_server_cache", {"workspaceId": workspace_id, "dirIds": [profile_id]}),
            ("/browser/mdf", {"workspaceId": workspace_id, "dirId": profile_id, "cookie": []}),
        ):
            try:
                self._request("POST", path, body=body, timeout=30)
            except Exception:  # noqa: BLE001
                # 本地清理成功即可；不同 Roxy 版本可能不提供这两个辅助端点。
                pass

    def update_profile_proxy_native(
        self,
        profile_id: str,
        proxy_line: str,
        *,
        country: str = "",
    ) -> None:
        """Apply sticky proxy + English UI fingerprint for RoxyNativeUI batch."""
        profile_id = str(profile_id or "").strip()
        if not profile_id:
            raise ValueError("Roxy profile id 不能为空")
        cc = (country or infer_zone_country(proxy_line) or "US").strip().upper()
        workspace_id = self.resolve_workspace_id()
        body: dict[str, Any] = {
            "workspaceId": workspace_id,
            "dirId": profile_id,
            "proxyInfo": proxy_info_from_line(proxy_line),
            "fingerInfo": finger_info_for_native_ui(cc),
        }
        try:
            self._request("POST", "/browser/mdf", body=body, timeout=60)
        except RuntimeError:
            body.pop("fingerInfo", None)
            self._request("POST", "/browser/mdf", body=body, timeout=60)

    def update_profile_proxy(
        self,
        profile_id: str,
        proxy_line: str,
        *,
        country: str = "",
        sync_fingerprint: bool = True,
    ) -> None:
        """Write a new sticky proxy into the Roxy profile.

        Fingerprint locale/timezone follow the proxy exit IP by default. Explicit
        ``language`` values (e.g. ``EN-US``) trigger Roxy ``language参数值错误``.
        """
        profile_id = str(profile_id or "").strip()
        if not profile_id:
            raise ValueError("Roxy profile id 不能为空")
        workspace_id = self.resolve_workspace_id()
        body: dict[str, Any] = {
            "workspaceId": workspace_id,
            "dirId": profile_id,
            "proxyInfo": proxy_info_from_line(proxy_line),
        }
        if sync_fingerprint:
            cc = (country or infer_zone_country(proxy_line) or "US").strip().upper()
            # GLOBAL 代理不能 follow IP，locale 跟 --countries 走
            body["fingerInfo"] = finger_info_for_country(cc, follow_ip=False)
        try:
            self._request("POST", "/browser/mdf", body=body, timeout=60)
        except RuntimeError as exc:
            if sync_fingerprint and "fingerInfo" in body:
                body.pop("fingerInfo", None)
                self._request("POST", "/browser/mdf", body=body, timeout=60)
                return
            raise exc


@contextmanager
def roxy_cdp_session(
    profile_id: str,
    *,
    client: Optional[RoxyBrowserClient] = None,
    close_on_exit: bool = True,
) -> Iterator[tuple[Any, Any]]:
    """Open an existing Roxy profile and yield ``(browser, context)``."""
    from playwright.sync_api import sync_playwright

    roxy = client or RoxyBrowserClient()
    if not roxy.health():
        raise RuntimeError(f"Roxy 本地 API 不可用（{roxy.base_url}）: {roxy.last_error}")
    ws = roxy.open(profile_id)
    playwright_cm = sync_playwright()
    playwright = playwright_cm.__enter__()
    browser = None
    try:
        browser = playwright.chromium.connect_over_cdp(ws)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        yield browser, context
    finally:
        if browser is not None and close_on_exit:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            playwright_cm.__exit__(None, None, None)
        finally:
            if close_on_exit:
                roxy.close(profile_id)
