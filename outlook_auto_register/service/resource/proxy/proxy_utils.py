from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse


@dataclass
class ProxyConfig:
    url: str
    scheme: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None


def parse_proxy(proxy: Optional[str]) -> Optional[ProxyConfig]:
    if not proxy:
        return None
    proxy = proxy.strip()
    if not proxy:
        return None

    # host:port:user:pass
    m = re.match(r"^([^:]+):(\d+):([^:]+):(.+)$", proxy)
    if m and "://" not in proxy:
        host, port, user, pwd = m.groups()
        scheme = "http"
        url = f"{scheme}://{user}:{pwd}@{host}:{port}"
        return ProxyConfig(url=url, scheme=scheme, host=host, port=int(port), username=user, password=pwd)

    if "://" not in proxy:
        proxy = f"http://{proxy}"

    parsed = urlparse(proxy)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = parsed.port or (1080 if scheme.startswith("socks") else 80)
    return ProxyConfig(
        url=proxy,
        scheme=scheme,
        host=host,
        port=port,
        username=parsed.username,
        password=parsed.password,
    )


def proxy_for_requests(proxy: Optional[str]) -> Optional[dict[str, str]]:
    cfg = parse_proxy(proxy)
    if not cfg:
        return None
    return {"http": cfg.url, "https": cfg.url}


def preflight_proxy(proxy: Optional[str], *, timeout: int = 15) -> tuple[bool, str]:
    """快速验活：代理能否 HTTPS CONNECT 出网。

    返回 (ok, 说明)。ok=False 时说明含失败原因（如 403 auth fail），
    便于在注册/打码前直接判定代理是否可用，而非绕一圈报 captcha 失败。
    captcha.run PxCaptcha2 强制要求可用代理，代理不通则 silent/press 必然 Fail。

    探测 URL 优先 ipinfo / myip（可解析出口 IP）。
    不用 google generate_204 作首选：个别网络/代理下浏览器能访问 Google，
    但该 204 探测仍会失败，导致预检误报。
    """
    import requests

    cfg = parse_proxy(proxy)
    if not cfg:
        return True, "(直连，无代理)"
    proxies = {"http": cfg.url, "https": cfg.url}
    last = ""
    # (url, mode)  mode: ip_text | ip_json | status_only
    probes = (
        ("https://ipinfo.io/ip", "ip_text"),
        ("https://api.myip.com/", "ip_json"),
        ("https://www.google.com/generate_204", "status_only"),
    )
    for url, mode in probes:
        try:
            r = requests.get(url, proxies=proxies, timeout=timeout)
            if r.status_code >= 400:
                last = f"HTTP {r.status_code}: {r.text[:80]}"
                continue
            ip = ""
            if mode == "ip_text":
                ip = (r.text or "").strip().splitlines()[0][:64]
            elif mode == "ip_json":
                try:
                    ip = str(r.json().get("ip", "") or "")
                except Exception:
                    ip = ""
            return True, f"出口={ip or 'ok'} via {cfg.host}:{cfg.port}"
        except Exception as exc:  # noqa: BLE001
            last = repr(exc)[:160]
    return False, f"代理不可用 {cfg.host}:{cfg.port} user={cfg.username} → {last}"


def _probe_exit_once(proxy_url: str, *, timeout: int) -> tuple[str, str]:
    """单次探测出口 IP + 国家。每次用全新 Session + Connection: close 强制新建连接，
    否则 keep-alive 会复用同一条隧道，看不出轮换代理的真实行为。"""
    import requests

    proxies = {"http": proxy_url, "https": proxy_url}
    with requests.Session() as s:
        s.trust_env = False
        r = s.get(
            "https://ipinfo.io/json",
            proxies=proxies,
            timeout=timeout,
            headers={"Connection": "close"},
        )
        r.raise_for_status()
        data = r.json()
    return str(data.get("ip", "") or ""), str(data.get("country", "") or "")


def probe_exit_stability(
    proxy: Optional[str],
    *,
    samples: int = 3,
    timeout: int = 15,
) -> tuple[bool, str, list[tuple[str, str]]]:
    """判定代理是不是「每请求轮换」——注册流程的致命伤。

    注册一个号要对 login.live.com / login.microsoftonline.com /
    collector-*.hsprotect.net 等多个域名开几十条 TCP 连接。若代理按连接轮换出口，
    PX 会在 A 国签发 ``_px3``、verify 却从 B 国提交，微软必然回
    ``AADSTS7005106 riskBlock``。这种代理必须配 sticky 会话参数才能用。

    返回 ``(sticky, 说明, [(ip, country), ...])``。
    sticky=False 表示多次探测拿到不同出口 IP。
    """
    cfg = parse_proxy(proxy)
    if not cfg:
        return True, "(直连，无代理)", []

    seen: list[tuple[str, str]] = []
    errors: list[str] = []
    for _ in range(max(2, samples)):
        try:
            seen.append(_probe_exit_once(cfg.url, timeout=timeout))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc)[:80])

    if not seen:
        return False, f"探测全部失败: {'; '.join(errors[:2])}", []

    ips = {ip for ip, _ in seen if ip}
    countries = {cc for _, cc in seen if cc}
    trail = ", ".join(f"{ip}/{cc or '?'}" for ip, cc in seen)

    if len(ips) <= 1:
        return True, f"出口稳定 {trail}", seen

    detail = f"每请求换出口（{len(ips)} 个 IP / {len(countries)} 个国家）: {trail}"
    return False, detail, seen


def proxy_for_capsolver(proxy: Optional[str]) -> Optional[dict[str, Any]]:
    cfg = parse_proxy(proxy)
    if not cfg:
        return None
    payload: dict[str, Any] = {
        "proxyType": "socks5" if cfg.scheme.startswith("socks") else "http",
        "proxyAddress": cfg.host,
        "proxyPort": cfg.port,
    }
    if cfg.username:
        payload["proxyLogin"] = cfg.username
    if cfg.password:
        payload["proxyPassword"] = cfg.password
    return payload


_COUNTRY_TIMEZONE = {
    "US": "America/New_York",
    "CA": "America/Toronto",
    "GB": "Europe/London",
    "UK": "Europe/London",
    "AU": "Australia/Sydney",
    "SG": "Asia/Singapore",
    "PH": "Asia/Manila",
    "HK": "Asia/Hong_Kong",
    "JP": "Asia/Tokyo",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
}

# ISO 3166-1 alpha-2，用于从代理模板 username 推断出口国家
_VALID_COUNTRY_CODES = frozenset(_COUNTRY_TIMEZONE.keys())

_COUNTRY_INFER_PATTERNS = [
    re.compile(r"(?:^|[-_.])([A-Z]{2})(?:[-_.]|session|residential|mobile|static|$)", re.I),
    re.compile(r"(?:country|region|geo|area)[-_]([A-Z]{2})\b", re.I),
    re.compile(r"residential[-_]([A-Z]{2})\b", re.I),
]

_IPWO_ZONE_SELECTOR_RE = re.compile(
    r"(?i)(?P<prefix>(?:^|[_-])(?:custom[_-])?zone[_-])"
    r"(?P<value>global|[a-z]{2})(?=$|[_-])"
)


def rewrite_ipwo_zone_country(proxy_line: str, country: str) -> str:
    """Rewrite IPWO ``custom_zone_GLOBAL`` username to ``custom_zone_US`` etc.

    GLOBAL sticky 出口国家随机，与 ``--countries`` 无关，微软易报
    ``We ran into a problem``。按本轮国家重写 zone 使 IP 与 locale 一致。
    """
    cc = (country or "US").strip().upper()
    if len(cc) != 2:
        return proxy_line
    cfg = parse_proxy((proxy_line or "").strip())
    if not cfg or not cfg.username or not cfg.password:
        return proxy_line

    def _replace_zone(match: re.Match[str]) -> str:
        current = match.group("value")
        selected = cc if current.isupper() else cc.lower()
        return f"{match.group('prefix')}{selected}"

    new_user, count = _IPWO_ZONE_SELECTOR_RE.subn(_replace_zone, cfg.username, count=1)
    if count == 0 or new_user == cfg.username:
        return proxy_line
    return f"{cfg.host}:{cfg.port}:{new_user}:{cfg.password}"


def infer_country_from_template(template: str) -> str:
    """从代理模板 username 段推断国家代码（如 US、SG）。"""
    template = (template or "").strip()
    if not template:
        return ""
    user_part = template
    m = re.match(r"^([^:]+):(\d+):([^:]+):", template)
    if m and "://" not in template:
        user_part = m.group(3)
    elif "@" in template and "://" in template:
        user_part = urlparse(template).username or template
    for pat in _COUNTRY_INFER_PATTERNS:
        hit = pat.search(user_part)
        if hit:
            cc = hit.group(1).upper()
            if cc in _VALID_COUNTRY_CODES:
                return cc
    return ""


def timezone_for_country(country: str) -> str:
    code = (country or "US").strip().upper()
    return _COUNTRY_TIMEZONE.get(code, "America/New_York")


def random_sid(length: int = 8) -> str:
    """rapidproxy sticky 会话 ID（数字）。"""
    import random

    return "".join(random.choice("0123456789") for _ in range(length))


def expand_proxy_template(raw: str, *, count: int = 1) -> list[str]:
    """把含 `{sid}` 的代理模板展开成 count 条随机 sticky 会话。

    例：gate.example.com:5001:myuser-residential-US-session-{sid}-stime-10:mypass
    每条用不同随机 sid → 分到不同住宅 IP。无 `{sid}` 则原样返回一条。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    if "{sid}" not in raw:
        return [raw]
    return [raw.replace("{sid}", random_sid()) for _ in range(max(1, count))]


def has_sid_template(raw: Optional[str]) -> bool:
    """代理串是否含 `{sid}` 会话占位符（决定批量注册能否做到一号一 IP）。"""
    return bool(raw) and "{sid}" in raw


def expand_proxy_unique(raw: Optional[str], count: int) -> list[str]:
    """展开成 count 条【互不相同】的 sticky 会话代理，用于「一号一 IP」批量注册。

    - 含 `{sid}`：填入 count 个互不重复的随机 sid → count 条各异代理串，
      每号一个独立 sticky 会话 → 出口 IP 互不相同（同 IP 批量是最强封号信号）。
    - 不含 `{sid}`：返回 count 条相同串（无法区分会话），调用方应据
      `has_sid_template` 提前警告「全批共用同一 IP，封号风险高」。
    - 空串：返回空列表。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    count = max(1, int(count))
    if "{sid}" not in raw:
        return [raw] * count
    seen: set[str] = set()
    out: list[str] = []
    guard = 0
    while len(out) < count and guard < count * 50:
        guard += 1
        sid = random_sid()
        if sid in seen:
            continue
        seen.add(sid)
        out.append(raw.replace("{sid}", sid))
    # 极端兜底：sid 空间过小未凑满时用加长 sid 补齐，仍保证唯一
    while len(out) < count:
        out.append(raw.replace("{sid}", random_sid(12)))
    return out


def parse_proxy_pool(raw: Optional[str] = None, *, template_count: int = 8) -> list[str]:
    """解析代理池：逗号/换行分隔，或 HTTP_PROXY_POOL 环境变量。

    支持 `{sid}` 模板：单条含 `{sid}` 会展开成 template_count 条随机 sticky 会话。
    """
    import os

    text = raw if raw is not None else os.environ.get("HTTP_PROXY_POOL", "")
    if not text:
        single = os.environ.get("HTTP_PROXY", "").strip()
        return expand_proxy_template(single, count=template_count) if single else []
    items: list[str] = []
    for part in text.replace("\n", ",").split(","):
        p = part.strip()
        if not p:
            continue
        items.extend(expand_proxy_template(p, count=template_count))
    return items


def proxy_for_captcha_run(
    proxy: Optional[str],
    *,
    user_agent: str = "",
    uuid: str = "",
    vid: str = "",
    country: str = "US",
    timezone: str = "America/New_York",
    developer: str = "",
) -> dict[str, Any]:
    """
    exe 26.7.11 同款 captcha.run 代理字段（扁平 JSON，非 URL 字符串）。
    字符串还原自 VMProtect exe 中 `login/password/port/host/uuid/vid` 片段。
    """
    from outlook_auto_register.config.constants import CAPTCHA_RUN_DEVELOPER_ID

    cfg = parse_proxy(proxy)
    dev = (developer or CAPTCHA_RUN_DEVELOPER_ID or "").strip()
    payload: dict[str, Any] = {
        **({"developer": dev} if dev else {}),
        "country": country,
        "timezone": timezone,
        "uuid": uuid,
        "vid": vid,
    }
    if user_agent:
        payload["userAgent"] = user_agent
    if cfg:
        payload.update({
            "login": cfg.username or "",
            "password": cfg.password or "",
            "port": cfg.port,
            "host": cfg.host,
        })
    return payload
