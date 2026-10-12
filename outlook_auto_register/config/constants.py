"""Microsoft 注册流程常量（来自 outlook注册.har + 26.7.11 注册机分析）。"""

# Outlook Web OAuth 客户端（入口）
OUTLOOK_CLIENT_ID = "9199bf20-a13f-4107-85dc-02114787ef48"
OUTLOOK_REDIRECT_URI = "https://outlook.live.com/mail/"
OUTLOOK_SCOPE = "https://outlook.office.com/.default openid profile offline_access"

# 注册页专用客户端
SIGNUP_CLIENT_ID = "00000000487A244A"

# 注册后邮件 OAuth（exe 同款 client_id = Thunderbird 公共客户端，无需自建 App/密钥）
MAIL_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
MAIL_REDIRECT_URI = "https://login.microsoftonline.com/common/oauth2/nativeclient"

# ── 邮件令牌模式（关键结论）───────────────────────────────────────────────
# 实测（见 check_imap / 交付说明）：新注册 outlook.com 邮箱 IMAP/POP 协议开关默认关闭，
# 且新号短期内无法开启（SetConsumerMailbox 返回 412）；故 IMAP 令牌虽能拿到，
# XOAUTH2 登录会「User is authenticated but not connected」→ 不可用。
# 参考工具「微软综合工具」的可用四段式实为 **Graph 令牌**（Mail.ReadWrite/Mail.Send/User.Read）
# 或 **Outlook REST 令牌**（outlook.office.com/Mail.Read 等），走 Graph / REST API 读信，
# 完全绕开 IMAP 开关，注册完立即可用。三者复用同一个 Thunderbird client_id。
#
#   graph        → 走 https://graph.microsoft.com/v1.0/me/messages（推荐，注册完即用）
#   outlook_rest → 走 https://outlook.office.com/api/v2.0/me/messages（同样绕开 IMAP 开关）
#   imap         → 传统 IMAP（仅老号/已开启 IMAP 的号可用）
#   dual         → 双令牌：token#1=Graph 收码/读信 + token#2=登录授权（第三方 SSO），
#                  同一次注册里做两次 code→token 交换，产出 6 段式 combo（cid----graph_rt----login_cid----login_rt）。
#   login_exe / recovery → login.exe 同款：Thunderbird IMAP/POP/SMTP scope + proofs 绑定恢复邮箱，
#                  产出 6 段式 email----pwd----cid----rt----recovery_email----recovery_password。
#                  与 dual 不同：第 5/6 段是恢复邮箱账号，不是第二枚 refresh_token。
import os as _os  # noqa: E402

MAIL_TOKEN_MODE = _os.environ.get("OUTLOOK_MAIL_TOKEN_MODE", "graph").strip().lower()

# Graph 邮件 scope（参考工具 1000outlook.txt 实测同款，注册完即可 Graph 读信）
GRAPH_MAIL_SCOPE = (
    "https://graph.microsoft.com/Mail.ReadWrite "
    "https://graph.microsoft.com/Mail.Send "
    "https://graph.microsoft.com/User.Read "
    "offline_access openid profile"
)

# Outlook REST 邮件 scope（走 outlook.office.com/api/v2.0，绕开 IMAP 协议开关）
OUTLOOK_REST_SCOPE = (
    "https://outlook.office.com/Mail.ReadWrite "
    "https://outlook.office.com/Mail.Send "
    "offline_access openid profile"
)

# 传统 IMAP/POP/SMTP scope（严格集：mail_reader 用它对「老号」走 XOAUTH2 收码，
# 老号当初只同意了 IMAP/POP/SMTP，这里绝不能加 Mail.Read/EWS，否则老号 refresh 会被
# AADSTS70000 拒 → 收码链断。仅供 mail_reader 及 mode=imap 使用。）
IMAP_MAIL_SCOPE = (
    "https://outlook.office.com/IMAP.AccessAsUser.All "
    "https://outlook.office.com/POP.AccessAsUser.All "
    "https://outlook.office.com/SMTP.Send "
    "offline_access"
)

# 卖家出货 token 的完整 Thunderbird scope（实测卖家 9e5f94bc token 同款）：
# 在 IMAP/POP/SMTP 之外还带 EWS + Mail.Read + Mail.Send。关键差别——注册【同意】时就把
# Mail.Read/EWS 一起授权，账号才能在新号阶段直接用 Outlook REST 读信；只授 IMAP/POP/SMTP
# 的新号 IMAP 协议默认关、REST 又缺 Mail.Read → 注册完读不了信（事后 refresh 加不上）。
THUNDERBIRD_MAIL_SCOPE = (
    "https://outlook.office.com/IMAP.AccessAsUser.All "
    "https://outlook.office.com/POP.AccessAsUser.All "
    "https://outlook.office.com/SMTP.Send "
    "https://outlook.office.com/EWS.AccessAsUser.All "
    "https://outlook.office.com/Mail.Read "
    "https://outlook.office.com/Mail.Send "
    "offline_access"
)

_MAIL_SCOPE_BY_MODE = {
    "graph": GRAPH_MAIL_SCOPE,
    "outlook_rest": OUTLOOK_REST_SCOPE,
    "imap": IMAP_MAIL_SCOPE,
    # dual：token#1（收码/读信）默认走 Graph
    "dual": GRAPH_MAIL_SCOPE,
    # login.exe / 恢复邮箱六段式：对齐卖家 = Thunderbird 完整 scope（含 Mail.Read/EWS，
    # 注册同意时授权，产出 token 立即可 REST 读信）。
    "recovery": THUNDERBIRD_MAIL_SCOPE,
    "login_exe": THUNDERBIRD_MAIL_SCOPE,
}

# 对外别名（webapp _apply_token_mode / UI 产出格式）
MAIL_SCOPE_BY_MODE = _MAIL_SCOPE_BY_MODE

# 兼容旧引用：默认按 MAIL_TOKEN_MODE 选择（默认 graph = 注册完即用）
MAIL_SCOPE = _MAIL_SCOPE_BY_MODE.get(MAIL_TOKEN_MODE, GRAPH_MAIL_SCOPE)


def normalize_token_mode(mode: str) -> str:
    """产出格式别名：login.exe / login_exe → login_exe；graph_recovery → graph_recovery。"""
    m = (mode or "graph").strip().lower().replace("-", "_").replace(".", "_")
    if m in ("login_exe", "loginexe", "login_exe_recovery"):
        return "login_exe"
    if m in ("graph_recovery", "graph_6", "graph6", "graph_six"):
        return "graph_recovery"
    return m or "graph"


def is_recovery_mode(mode: str = "") -> bool:
    """login.exe 六段式（IMAP scope + 恢复邮箱），不是 dual 双令牌。"""
    return normalize_token_mode(mode or MAIL_TOKEN_MODE) in ("recovery", "login_exe")


def is_graph_recovery_mode(mode: str = "") -> bool:
    """Graph 六段式：第 4 段仍是 Graph refresh_token，附带 proofs 恢复邮箱。"""
    return normalize_token_mode(mode or MAIL_TOKEN_MODE) == "graph_recovery"


def is_dual_mode(mode: str = "") -> bool:
    return normalize_token_mode(mode or MAIL_TOKEN_MODE) == "dual"

# ── 双令牌 token#2：登录授权（第三方 “用微软账号登录” SSO）──────────────────
# 市场上的“双令牌 outlook”通常是「收码令牌 + 登录令牌」两个可用 refresh_token：
#   token#1  Graph/REST 读信（我们已在产，见上）
#   token#2  登录授权，scope 形如 openid profile offline_access（+可选 User.Read）
# 仓库内未找到强制的字段标准（*_cursor_6field.txt 是 Cursor 专用，非本用途），
# 故实现一个合理且可配置的 6 段式；client_id/scope 均可用环境变量覆盖：
#   OUTLOOK_LOGIN_CLIENT_ID  token#2 客户端（默认复用 Thunderbird 公共客户端，无需自建 App）
#   OUTLOOK_LOGIN_SCOPE      token#2 scope（默认 openid profile offline_access User.Read）
LOGIN_CLIENT_ID = _os.environ.get("OUTLOOK_LOGIN_CLIENT_ID", MAIL_CLIENT_ID).strip()


def _normalize_login_scope(raw: str) -> str:
    """token#2 必须含 offline_access（否则不返 refresh_token，只有 id_token/access_token）；
    并保证含 openid profile（拿 id_token 做 SSO 身份）。缺则补齐、去重保序。"""
    parts = [p for p in (raw or "").split() if p]
    for required in ("openid", "profile", "offline_access"):
        if required not in parts:
            parts.append(required)
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return " ".join(out)


LOGIN_SCOPE = _normalize_login_scope(
    _os.environ.get(
        "OUTLOOK_LOGIN_SCOPE",
        "openid profile offline_access https://graph.microsoft.com/User.Read",
    ).strip()
)

# token#2 换 token 失败时的重试次数（每次会重走一遍 authorize）
LOGIN_TOKEN_ATTEMPTS = int(_os.environ.get("OUTLOOK_LOGIN_TOKEN_ATTEMPTS", "3"))

# 是否启用双令牌（供 register/post_register 判定）
DUAL_TOKEN = MAIL_TOKEN_MODE == "dual"

# Cobrand
COBRAND_ID = "ab0455a0-8d03-46b9-b18b-df2f57b9e44c"

# MSA 租户（risk/verify）
MSA_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"

# PerimeterX
PX_APP_ID = "PXzC5j78di"
PX_COLLECTOR_BASE = "https://collector-pxzc5j78di.hsprotect.net"

# Arkose
ARKOSE_PUBLIC_KEY = "B7D8911C-5CC8-A9A3-35B0-554ACEE604DA"

# captcha.run
CAPTCHA_RUN_API_BASE = "https://apicn.captcha.run"
CAPTCHA_RUN_API_BASE_GLOBAL = "https://api.captcha-run.com"
# captcha.run 推荐分成 ID（非 API Key）；留空则不传 developer 字段
CAPTCHA_RUN_DEVELOPER_ID = _os.environ.get("CAPTCHA_RUN_DEVELOPER_ID", "").strip()

# offcaptcha.com（PX invisible + press-and-hold）
OFFCAPTCHA_API_BASE = "https://api.offcaptcha.com/v1"
# softID 为 OffCaptcha 推荐分成 ID（非密钥），默认内置
OFFCAPTCHA_SOFT_ID = _os.environ.get(
    "OFFCAPTCHA_SOFT_ID", "641b6196-5a20-4af4-be86-525f768282a3"
).strip()

# API 路径
RISK_INITIALIZE_PATH = f"/{MSA_TENANT_ID}/api/v1.0/risk/initialize"
RISK_VERIFY_PATH = f"/{MSA_TENANT_ID}/api/v1.0/risk/verify"

SIGNUP_API_BASE = "https://signup.live.com/API"
LOGIN_BASE = "https://login.live.com"
LOGIN_MS_BASE = "https://login.microsoftonline.com"
ACCOUNT_BASE = "https://account.live.com"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_MKT = "EN-US"
DEFAULT_LC = "1033"

# country → (mkt, lc)，与代理地区一致，降低批量指纹
COUNTRY_LOCALE: dict[str, tuple[str, str]] = {
    "US": ("EN-US", "1033"),
    "CA": ("EN-CA", "4105"),
    "GB": ("EN-GB", "2057"),
    "AU": ("EN-AU", "3081"),
    "SG": ("EN-SG", "18441"),
    "PH": ("EN-PH", "34313"),
    "DE": ("DE-DE", "1031"),
    "FR": ("FR-FR", "1036"),
    "JP": ("JA-JP", "1041"),
    "KR": ("KO-KR", "1042"),
    "CN": ("ZH-CN", "2052"),
    "TW": ("ZH-TW", "1028"),
    "HK": ("ZH-HK", "3076"),
}


def locale_for_country(country: str) -> tuple[str, str]:
    cc = (country or "US").strip().upper()
    return COUNTRY_LOCALE.get(cc, (DEFAULT_MKT, DEFAULT_LC))


def signup_url_for_country(country: str) -> str:
    """Microsoft signup URL with explicit ``mkt`` — avoids Uzbek/随机 IP locale。"""
    mkt, _lc = locale_for_country(country)
    return f"https://signup.live.com/signup?lic=1&mkt={mkt}"


def signup_url_for_country(country: str) -> str:
    """Microsoft signup URL with explicit ``mkt`` — avoids Uzbek/随机 IP locale。"""
    mkt, _lc = locale_for_country(country)
    return f"https://signup.live.com/signup?lic=1&mkt={mkt}"


def signup_url_for_country(country: str) -> str:
    """Microsoft signup URL with explicit ``mkt`` — avoids Uzbek/随机 IP locale。"""
    mkt, _lc = locale_for_country(country)
    return f"https://signup.live.com/signup?lic=1&mkt={mkt}"

# Web 注册页国家下拉（ISO2 → 英文名；未在 COUNTRY_LOCALE 的仍可用默认 EN-US locale）
REGISTRATION_COUNTRY_NAMES: dict[str, str] = {
    "AE": "United Arab Emirates",
    "AR": "Argentina",
    "AT": "Austria",
    "AU": "Australia",
    "BE": "Belgium",
    "BR": "Brazil",
    "CA": "Canada",
    "CH": "Switzerland",
    "CL": "Chile",
    "CN": "China",
    "CO": "Colombia",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "EG": "Egypt",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "GB": "United Kingdom",
    "GR": "Greece",
    "HK": "Hong Kong",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IN": "India",
    "IT": "Italy",
    "JP": "Japan",
    "KR": "South Korea",
    "LV": "Latvia",
    "MX": "Mexico",
    "MY": "Malaysia",
    "NL": "Netherlands",
    "NO": "Norway",
    "NZ": "New Zealand",
    "PE": "Peru",
    "PH": "Philippines",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "RU": "Russia",
    "SA": "Saudi Arabia",
    "SE": "Sweden",
    "SG": "Singapore",
    "SK": "Slovakia",
    "TH": "Thailand",
    "TR": "Turkey",
    "TW": "Taiwan",
    "UA": "Ukraine",
    "US": "United States",
    "VN": "Vietnam",
    "ZA": "South Africa",
}

REGISTRATION_COUNTRY_NAMES_ZH: dict[str, str] = {
    "AE": "阿联酋",
    "AR": "阿根廷",
    "AT": "奥地利",
    "AU": "澳大利亚",
    "BE": "比利时",
    "BR": "巴西",
    "CA": "加拿大",
    "CH": "瑞士",
    "CL": "智利",
    "CN": "中国",
    "CO": "哥伦比亚",
    "CZ": "捷克",
    "DE": "德国",
    "DK": "丹麦",
    "EG": "埃及",
    "ES": "西班牙",
    "FI": "芬兰",
    "FR": "法国",
    "GB": "英国",
    "GR": "希腊",
    "HK": "香港",
    "HU": "匈牙利",
    "ID": "印尼",
    "IE": "爱尔兰",
    "IL": "以色列",
    "IN": "印度",
    "IT": "意大利",
    "JP": "日本",
    "KR": "韩国",
    "LV": "拉脱维亚",
    "MX": "墨西哥",
    "MY": "马来西亚",
    "NL": "荷兰",
    "NO": "挪威",
    "NZ": "新西兰",
    "PE": "秘鲁",
    "PH": "菲律宾",
    "PL": "波兰",
    "PT": "葡萄牙",
    "RO": "罗马尼亚",
    "RU": "俄罗斯",
    "SA": "沙特",
    "SE": "瑞典",
    "SG": "新加坡",
    "SK": "斯洛伐克",
    "TH": "泰国",
    "TR": "土耳其",
    "TW": "台湾",
    "UA": "乌克兰",
    "US": "美国",
    "VN": "越南",
    "ZA": "南非",
}

# Microsoft 官方支持的 Outlook 国家后缀 + 通用域（共 34 项，见 signup 各国入口）
OUTLOOK_EMAIL_DOMAINS = [
    "@outlook.com",
    "@hotmail.com",
    "@outlook.com.ar",
    "@outlook.com.au",
    "@outlook.at",
    "@outlook.be",
    "@outlook.com.br",
    "@outlook.cl",
    "@outlook.cz",
    "@outlook.dk",
    "@outlook.fr",
    "@outlook.de",
    "@outlook.com.gr",
    "@outlook.co.il",
    "@outlook.in",
    "@outlook.co.id",
    "@outlook.ie",
    "@outlook.it",
    "@outlook.hu",
    "@outlook.jp",
    "@outlook.kr",
    "@outlook.lv",
    "@outlook.my",
    "@outlook.co.nz",
    "@outlook.com.pe",
    "@outlook.ph",
    "@outlook.pt",
    "@outlook.sa",
    "@outlook.sg",
    "@outlook.sk",
    "@outlook.es",
    "@outlook.co.th",
    "@outlook.com.tr",
    "@outlook.co.uk",
    "@outlook.com.vn",
]

OUTLOOK_EMAIL_DOMAIN_LABELS: dict[str, str] = {
    "@outlook.com": "Outlook 国际",
    "@hotmail.com": "Hotmail 国际",
    "@outlook.com.ar": "阿根廷",
    "@outlook.com.au": "澳大利亚",
    "@outlook.at": "奥地利",
    "@outlook.be": "比利时",
    "@outlook.com.br": "巴西",
    "@outlook.cl": "智利",
    "@outlook.cz": "捷克",
    "@outlook.dk": "丹麦",
    "@outlook.fr": "法国",
    "@outlook.de": "德国",
    "@outlook.com.gr": "希腊",
    "@outlook.co.il": "以色列",
    "@outlook.in": "印度",
    "@outlook.co.id": "印尼",
    "@outlook.ie": "爱尔兰",
    "@outlook.it": "意大利",
    "@outlook.hu": "匈牙利",
    "@outlook.jp": "日本",
    "@outlook.kr": "韩国",
    "@outlook.lv": "拉脱维亚",
    "@outlook.my": "马来西亚",
    "@outlook.co.nz": "新西兰",
    "@outlook.com.pe": "秘鲁",
    "@outlook.ph": "菲律宾",
    "@outlook.pt": "葡萄牙",
    "@outlook.sa": "沙特",
    "@outlook.sg": "新加坡",
    "@outlook.sk": "斯洛伐克",
    "@outlook.es": "西班牙",
    "@outlook.co.th": "泰国",
    "@outlook.com.tr": "土耳其",
    "@outlook.co.uk": "英国",
    "@outlook.com.vn": "越南",
}
