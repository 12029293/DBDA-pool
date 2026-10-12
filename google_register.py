# -*- coding: utf-8 -*-
"""Google 账号注册骨架（自建农场号产线）。

做什么
------
用 patchright 反检测浏览器走 Google 官方注册页，产出 **「用自备邮箱注册的 Google 账号」**
（即 `wx0e8f@izqz.kdns.fr` 那种：非 Gmail，本地部分随机，域名是自有 catch-all 域）。

链路
----
    accounts.google.com/signup
      -> name           填姓名
      -> birthdaygender 填生日(day/year input + month/gender 自定义下拉) + 性别
      -> username       点「Use your existing email」-> 填 <随机前缀>@<DOMAIN>
      -> emailcode      邮件验证码（mail_catcher 从 catch-all 收件后端取）
      -> password       设密码
      -> （可选）绑 2FA，抓 otpauth secret 当 TOTP
      -> 产出 `email----password----totp`（= 号池文件格式）

用法
----
    # 1) 干跑：只校验配置 + 生成样例，不启动浏览器
    DOMAIN="izqz.kdns.fr" python google_register.py --dry-run

    # 2) 真实注册
    DOMAIN="izqz.kdns.fr" PROXY="http://user:pass@住宅IP:port" COUNT=3 \
    MAIL_BACKEND=imap MAIL_IMAP_HOST=imap.example.com MAIL_IMAP_USER=box@my.com \
    MAIL_IMAP_PASS=xxxxxx python google_register.py

环境变量
--------
    DOMAIN         (必填) 邮箱域名，需配好 catch-all（任意前缀都能收信）
    PROXY          (必填) 出口代理；**必须住宅/移动**，机房 IP 基本必被强制手机验证
    COUNT          注册数量，默认 1
    PASSWORD       固定密码，留空则随机生成
    PREFIX_LEN     邮箱随机前缀长度，默认 8
    HEADLESS       1=无头（Google 反自动化强，建议 0 有头）
    GOOGLE_2FA     1=注册后尝试绑 2FA 并抓 TOTP（默认 1）
    GACC_OUT       产物文件，默认 谷歌账号_产出.txt
    GACC_PROFILE   每号 profile 根目录，默认 _gacc_profiles/
    GACC_KEEP_PROFILE  1=保留 profile（默认保留，便于排错）
    GACC_STEP_WAIT_MS  每步轮询间隔，默认 2500
    GACC_LANG      界面语言，默认 en-US（选择器最稳）
    MAIL_*         收件后端配置，见 mail_catcher.py

实测边界（2026-10-11）
---------------------
* **全链路已实测**（`_diag_gacc_full_e2e.py`，收件走 tempmail.plus）：
  name -> birthdaygender -> username/emailsignup -> verifyemail（取码回填成功）
  -> password（提交成功）-> `/signup/error`。
  即**六步全部走通，最后一步被 Google 服务端拒绝**。
* **阻断点判定**：`Sorry, we could not create your Google Account.` = 出口 IP 信誉。
  实测出口 Clash 7890 = `85.234.83.246` / **G-Core Labs（机房 ASN，机场共享）**，
  同 IP 当日已多次注册 → 必被拦。换住宅/移动出口 + 放慢节奏是唯一出路。
* 收件后端：`tempmail`（mailto.plus）**发码与收码均实测成功**（Google 接受该域并发码）；
  password 之后的拒绝与邮箱域无关。
* 手机验证：若 Google 强制要求手机号，抛 NeedPhoneVerification（未接接码平台）。
* 代理链路坑：`proxy_fwd.SocksForwarder` 的默认噪音名单含 google.com / gstatic.com，
  目标是 Google 自家站点时必须传 `noise_domains=()`，否则表现为 ERR_SOCKS_CONNECTION_FAILED。
* 本脚本只做注册；产出的 Google 号要变成 dola 号，再走 `ck_weblogin.py` / `add_account.py`。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import string
import sys
import time
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mail_catcher import MailCatcher          # noqa: E402

WORKDIR = Path(__file__).parent.absolute()

SIGNUP_URL = ("https://accounts.google.com/signup/v2/createaccount"
              "?flowName=GlifWebSignIn&flowEntry=SignUp")

# —— 注册前预热（社区实测有效：fresh 指纹直接进注册页是 QR 风控页的高危信号，
#    先以普通用户身份访问 google.com 并搜索一次，种下 NID/SOCS 等 Cookie 后，
#    注册页判定为「有历史行为的真实浏览器」，QR 变体页出现率明显下降）——
WARMUP_QUERIES = ["weather today", "news", "youtube", "maps directions",
                  "translate english", "gmail login", "sports scores",
                  "currency exchange", "google docs", "nearby restaurants"]


async def warm_up(page) -> bool:
    """打开 google.com 搜索一次养 Cookie。失败不影响主流程。"""
    if os.environ.get("GACC_WARMUP", "1") == "0":
        return False
    try:
        await page.goto("https://www.google.com/?hl=en&gl=us",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(random.randint(1500, 3500))
        q = random.choice(WARMUP_QUERIES)
        box = page.locator('textarea[name="q"], input[name="q"]').first
        await box.click(timeout=8000)
        await box.type(q, delay=random.randint(60, 140))
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(random.randint(2500, 5000))
        await page.mouse.move(random.randint(200, 800), random.randint(200, 500))
        await page.wait_for_timeout(random.randint(1000, 2500))
        say("[warmup] 预热完成（搜索：%s）" % q)
        return True
    except Exception as e:                              # noqa: BLE001
        say("[warmup] 预热跳过（不影响注册）：%s" % str(e)[:80])
        return False


# —— 注册入口：产品侧入口（YouTube/Drive）携带 service 上下文，Google 按
#    「该产品的新用户」对待，风控判定比直链 signup/v2 宽松（社区实测）——
def _entry_urls(entry: str, lang: str) -> list[str]:
    hl = (lang.split("-")[0] or "en")
    return {
        "youtube": ["https://accounts.google.com/SignUp?service=youtube"
                    "&continue=https%3A%2F%2Fwww.youtube.com%2F&hl=" + hl],
        "drive": ["https://accounts.google.com/SignUp?service=wise"
                  "&continue=https%3A%2F%2Fdrive.google.com%2F&hl=" + hl],
        "classic": [SIGNUP_URL + "&hl=" + hl],
    }[entry]


def _entry_order(entry: str) -> list[str]:
    """auto = youtube -> classic；youtube/drive 失败回退 classic。"""
    entry = (entry or "auto").strip().lower()
    return {
        "youtube": ["youtube", "classic"],
        "drive": ["drive", "classic"],
        "classic": ["classic"],
    }.get(entry, ["youtube", "classic"])


def _on_signup_form(url: str) -> bool:
    u = (url or "").lower()
    return ("accounts.google.com" in u
            and ("/createaccount" in u or "/signup" in u or "/signup?" in u))


async def _goto_retry(page, url: str, attempts: int = 3, timeout: int = 90000):
    """goto 带重试：UDeal 等动态出口对并发连接偶发 RST/半传输，
    NS_ERROR_NET_PARTIAL_TRANSFER / NS_ERROR_ABORT 重试一次通常即过。"""
    last = None
    for i in range(attempts):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return
        except Exception as e:                              # noqa: BLE001
            last = e
            await page.wait_for_timeout(1500 + 1000 * i)
    raise last


async def open_signup_entry(ctx, page, entry: str, lang: str = "en-US") -> str:
    """按入口链打开注册表单，返回实际使用的入口名。全部失败抛 RegisterError。

    youtube 入口优先走真实 UI（youtube.com → 登录 → 创建账号），
    点击定位不到就用直链兜底——两条路都带 service=youtube 上下文。
    """
    last_err = ""
    for e in _entry_order(entry):
        try:
            if e == "youtube":
                await _goto_retry(page, "https://www.youtube.com/?hl=en&gl=us",
                                  timeout=60000)
                await page.wait_for_timeout(random.randint(2000, 4000))
                clicked = False
                for txt in ("Sign in", "登录", "登入"):
                    loc = page.get_by_role("link", name=txt)
                    try:
                        if await loc.count():
                            await loc.first.click()
                            clicked = True
                            break
                    except Exception:                       # noqa: PERF203
                        continue
                if not clicked:
                    await _goto_retry(page, _entry_urls("youtube", lang)[0],
                                      timeout=60000)
                else:
                    await page.wait_for_timeout(random.randint(2500, 4500))
                    for txt in ("Create account", "创建账号", "建立帳戶", "注册"):
                        try:
                            loc = page.get_by_text(txt, exact=False)
                            if await loc.count():
                                await loc.first.click()
                                break
                        except Exception:                   # noqa: PERF203
                            continue
                    else:
                        await _goto_retry(page, _entry_urls("youtube", lang)[0],
                                          timeout=60000)
            else:
                await _goto_retry(page, _entry_urls(e, lang)[0])
            await page.wait_for_timeout(4000)
            if _on_signup_form(page.url):
                say("[entry] 入口=%s -> %s" % (e, page.url[:110]))
                return e
            last_err = "入口 %s 未落到注册表单: %s" % (e, page.url[:90])
            say("[entry] warn: %s" % last_err)
        except Exception as ex:                             # noqa: BLE001
            last_err = "入口 %s 异常: %s: %s" % (e, type(ex).__name__, str(ex)[:90])
            say("[entry] warn: %s" % last_err)
    raise RegisterError("所有入口都未能打开注册表单（%s）" % last_err)

# ================================ 实测校准过的选择器（2026-10-11）========================
# name 步
SEL_FIRST = 'input[name="firstName"]'                  # 实测命中 1
SEL_LAST = 'input[name="lastName"]'                    # 实测命中 1
# birthdaygender 步（**注意：没有原生 <select>**）
SEL_DAY = "#day"                                       # <input type=tel id=day maxlength=2>
SEL_YEAR = "#year"                                     # <input type=tel id=year maxlength=4>
SEL_MONTH = "#month"                                   # <div id=month> 内含 [role=combobox]
SEL_GENDER = "#gender"                                 # <div id=gender> 内含 [role=listbox]
# username 步 / 邮箱模式
SEL_USERNAME = 'input[name="Username"]'                # 切换后 id=identifierId, aria-label="Email address"
TXT_USE_EXISTING = "Use your existing email"           # 实测：是 <button>，文本就是这个（不是 "current email address"）
# 后续步（2026-10-11 实测命中；多候选兜底）
SELS_CODE = ['input[name="code"]', "#code",       # 实测：<input type=tel id=code name=code aria-label="Enter code">
             'input[type="text"][maxlength="1"]',
             'input[inputmode="numeric"][maxlength="1"]']
SELS_PASSWD = ['input[name="Passwd"]', "#passwd"]        # 实测命中（无 id，只有 name）
SELS_PASSWD2 = ['input[name="PasswdAgain"]', 'input[name="ConfirmPasswd"]', "#confirm-passwd"]  # 实测命中 PasswdAgain

NEXT_SELS = ["#accountDetailsNext", "#birthdaygenderNext", "#usernameNext",
             "#verifyCodeNext", "#passwordNext",
             'button:has-text("Next")', 'button:has-text("次へ")',
             'button:has-text("下一步")']

BIRTHDAY_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                   "August", "September", "October", "November", "December"]
GENDER_TEXT = {"1": "Female", "2": "Male", "3": "Rather not say"}   # 实测 DOM 文本（无 "to"）

SKIP_TEXTS = ["Not now", "Skip", "Skip for now", "I'll do it later",
              "今はしない", "後で", "スキップ", "暂时跳过", "以后再说", "稍后"]
AGREE_TEXTS = ["I agree", "同意する", "同意", "Agree", "続行", "Continue"]
# =====================================================================================


# ------------------------------------------------------------------ 工具
def say(*a):
    print(*a, flush=True)


def _proxy_kwargs(raw: str) -> dict | None:
    """把代理 URL 解析成 Playwright proxy 参数（支持 user:pass@host:port）。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    from urllib.parse import urlsplit, unquote
    p = urlsplit(raw)
    if not p.hostname:
        return None
    kw = {"server": "%s://%s:%d" % (p.scheme, p.hostname, p.port or 80)}
    if p.username:
        kw["username"] = unquote(p.username)
    if p.password:
        kw["password"] = unquote(p.password)
    return kw


def rand_email(domain: str, n: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    head = random.choice(string.ascii_lowercase)
    return head + "".join(random.choices(alphabet, k=max(1, n - 1))) + "@" + domain


def rand_password(n: int = 12) -> str:
    """Google 要求至少 8 位；这里给够复杂度。"""
    pool = string.ascii_letters + string.digits + "!@#$%"
    while True:
        p = "".join(random.choices(pool, k=n))
        if (re.search(r"[a-z]", p) and re.search(r"[A-Z]", p)
                and re.search(r"\d", p) and re.search(r"[!@#$%]", p)):
            return p


_FIRST = ["james", "oliver", "liam", "noah", "emma", "olivia", "ava", "mia",
          "lucas", "ethan", "sofia", "amelia", "harper", "ella", "logan"]
_LAST = ["smith", "johnson", "brown", "taylor", "anderson", "thomas", "lee",
         "walker", "hall", "allen", "young", "king", "wright", "scott"]


def rand_identity():
    return {
        "first": random.choice(_FIRST).capitalize(),
        "last": random.choice(_LAST).capitalize(),
        "year": str(random.randint(1990, 2002)),
        "month": str(random.randint(1, 12)),
        "day": str(random.randint(1, 28)),
        "gender": random.choice(["1", "2", "3"]),
    }


# ------------------------------------------------------------------ 异常
class RegisterError(RuntimeError):
    pass


class NeedPhoneVerification(RegisterError):
    """Google 强制要求手机号 —— 需接码平台才能继续。"""


class StoppedAtStep(RegisterError):
    """联调用：GACC_STOP_AT 命中，主动停在某步（**不完成注册**）。"""


# ------------------------------------------------------------------ 页面辅助
class UI:
    def __init__(self, page, step_wait_ms=2500):
        self.page = page
        self.wait = step_wait_ms

    async def text(self) -> str:
        try:
            return await self.page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 4000)")
        except Exception:
            return ""

    async def vis(self, sel: str) -> bool:
        try:
            loc = self.page.locator(sel).first
            return bool(await loc.count()) and await loc.is_visible()
        except Exception:
            return False

    async def first_vis(self, sels: list[str]) -> str:
        for s in sels:
            if await self.vis(s):
                return s
        return ""

    async def wait_stable(self, tries: int = 20):
        """Google 各步是 SPA；body 里 'Loading' 消失才表示 React 已水合、吃得进输入。"""
        for _ in range(tries):
            if "Loading" not in (await self.text()):
                return
            await self.page.wait_for_timeout(700)

    async def dump(self, tag: str):
        try:
            html = await self.page.content()
            p = WORKDIR / ("_gacc_dump_%s.html" % tag)
            p.write_text(html, encoding="utf-8", errors="replace")
            await self.page.screenshot(path=str(WORKDIR / ("_gacc_dump_%s.png" % tag)))
            say("[dump] 未识别页面已存 %s / .png" % p.name)
        except Exception as e:
            say("[dump] 失败: %s" % str(e)[:80])


def step_of(url: str) -> str:
    m = re.search(r"/signup/([A-Za-z_]+)", url or "")
    return m.group(1) if m else ""


async def fill_verified(page, sel: str, value: str, tries: int = 3) -> bool:
    """填完读回校验 —— React 受控输入偶尔会吞掉首填。"""
    for _ in range(tries):
        try:
            await page.fill(sel, value)
            if (await page.input_value(sel)).strip() == value:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(400)
    return False


def _mdc_matches(val: str, text: str) -> bool:
    """下拉「读回」校验：**词边界**匹配。

    实测坑：用 `text in val` 会把 `Male` 判成命中 `Female`
    （"fe**male**" 含 "male"），于是选错项还报成功。
    """
    if not val or not text:
        return False
    return re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(text), val, re.I) is not None


async def _mdc_value_text(page, root: str) -> str:
    """读回 Material 下拉当前**选中值**（确认选择是否真的生效）。

    优先取 `aria-selected=true` 的 option（最准），否则回落 root 的 innerText。
    """
    try:
        sel = page.locator('%s [role="option"][aria-selected="true"]' % root)
        if await sel.count():
            t = " ".join((await sel.first.inner_text() or "").split())
            if t:
                return t
    except Exception:
        pass
    try:
        return " ".join((await page.locator(root).first.inner_text() or "").split())
    except Exception:
        return ""


async def _mdc_open(page, root: str) -> bool:
    """点开 Material 下拉；成功判据 = 有可见的 [role=option]。

    打开方式 4 种兜底（内含 combobox / listbox / button / 容器本身），
    命中就用 `wait_for_selector` **显式等待**选项，不再只靠固定 sleep。
    """
    for opener in ('%s [role="combobox"]' % root, '%s [role="listbox"]' % root,
                   '%s [role="button"]' % root, root):
        try:
            loc = page.locator(opener).first
            if not await loc.count():
                continue
        except Exception:
            continue
        for _attempt in (1, 2):          # 已选过的下拉「首点会关闭」，所以点两次兜底
            try:
                try:
                    await loc.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                await loc.click(timeout=6000)
            except Exception:
                try:
                    await loc.evaluate("e => e.click()")
                except Exception:
                    break
            try:
                # 注意：页面里 `[role=option]` 有 **上百个**（Material 为每步预渲染），
                # 第一个往往是隐藏的 → 不能用 wait_for_selector(state="visible")
                # （它只认首个匹配），必须按「可见 option 计数」判定。
                for _ in range(10):
                    if await page.locator('[role="option"]:visible').count():
                        return True
                    await page.wait_for_timeout(250)
            except Exception:
                pass
    return False


async def pick_mdc(page, root: str, text: str) -> bool:
    """Google Material 自定义下拉：点开 -> 选 role=option，并用**读回**确认生效。

    2026-10-11 四轮实测加固（有头/无头都踩过）：
      * 匹配**先精确后词边界**，绝不用裸子串 —— `has_text="Male"` 会命中
        `Female` 且它排在前，`.first` 就点到 Female（实测踩过，选错还报成功）；
      * 读回校验也用**词边界**（`Male` ⊂ `Female` 的假阳性）；
      * 打开方式 4 种兜底；判「已打开」按**可见 option 计数**，
        不能用 `wait_for_selector(state="visible")`（页面有一百多个隐藏 option，
        它只认首个匹配，永远等不到）；
      * 选择方式：root 内 option → 全局 option → `get_by_role("option")`；
      * **键盘兜底**：首字母 type-ahead + Enter（Material 原生支持）；
      * 每轮结束**读回选中值**校验；未生效就重开再试一轮。
    """
    for _round in range(2):
        if _mdc_matches(await _mdc_value_text(page, root), text):
            return True                                   # 已是目标值
        if not await _mdc_open(page, root):
            continue
        # 匹配分两级：**先精确**（允许首尾空白），再词边界。
        # 不能只用子串 —— `filter(has_text="Male")` 会命中 "Female" 且它排在前面，
        # `.first` 就点到 Female（实测踩过）。
        pats = [re.compile(r"^\s*%s\s*$" % re.escape(text), re.I),
                re.compile(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(text), re.I)]
        picked = False
        for pat in pats:
            for cand in (page.locator('%s [role="option"]:visible' % root).filter(has_text=pat),
                         page.locator('[role="option"]:visible').filter(has_text=pat),
                         page.get_by_role("option", name=pat)):
                try:
                    if not await cand.count():
                        continue
                    try:
                        await cand.first.click(timeout=5000)
                    except Exception:
                        await cand.first.evaluate("e => e.click()")
                    await page.wait_for_timeout(450)
                    picked = True
                    break
                except Exception:
                    continue
            if picked:
                break
        if not picked:
            try:                                          # 键盘兜底：首字母 + Enter
                await page.keyboard.type(text[:1], delay=100)
                await page.wait_for_timeout(450)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(450)
                picked = True
            except Exception:
                pass
        if picked and _mdc_matches(await _mdc_value_text(page, root), text):
            return True
        try:                                              # 关掉可能残留的弹层再重试
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.wait_for_timeout(300)

    # 最后一档兜底：取第一个 option（Material 下拉多为必填，取首项远好于卡死）
    try:
        if await _mdc_open(page, root):
            opts = page.locator('[role="option"]:visible')
            if await opts.count():
                await opts.first.click(timeout=5000)
                await page.wait_for_timeout(400)
                return True
    except Exception:
        pass
    return False


async def click_next(page) -> str:
    for s in NEXT_SELS:
        try:
            loc = page.locator(s).first
            if await loc.count() and await loc.is_visible():
                try:
                    await loc.click(timeout=6000)
                except Exception:
                    await loc.evaluate("e => e.click()")
                return s
        except Exception:
            continue
    return ""


async def _maybe_phone_gate(ui: UI) -> None:
    t = (await ui.text()).lower()
    if any(k in t for k in ["verify your phone number", "enter a phone number",
                            "確認コードを送信"]) \
            and await ui.first_vis(["input#phoneNumberId", "input[type='tel']"]):
        raise NeedPhoneVerification("Google 要求手机验证（需接码平台）")


# ------------------------------------------------------------------ 状态机
async def signup_flow(ctx, page, email: str, password: str,
                      catcher: MailCatcher, want_totp: bool,
                      step_wait_ms: int, stop_at: str = "") -> str:
    """按 URL 里的 /signup/<step> 驱动；成功返回 TOTP 密钥（可能为空串）。"""
    ui = UI(page, step_wait_ms)
    ident = rand_identity()
    code_seen: set = set()
    switched = False
    last_step, stuck = "", 0

    for step in range(80):
        await ui.wait_stable()
        url = page.url
        if "accounts.google.com" not in url:
            say("[flow] 已离开 Google 域: %s" % url[:110])
            break
        st = step_of(url)
        if stop_at and st == stop_at:
            say("[flow] 命中 GACC_STOP_AT=%s，主动停止（未完成注册）" % st)
            raise StoppedAtStep("已到达 %s 步" % st)
        stuck = stuck + 1 if st == last_step else 0
        last_step = st
        await _maybe_phone_gate(ui)

        # 建号失败页**必须在其它分支之前**判定：
        # 实测设密码提交后 URL 变 /signup/error，但那一刻 DOM 还残留密码页，
        # 会被 E 分支（ui.vis('input[name=Passwd]')）抢先 → 反复重试空转。
        _low = (await ui.text()).lower()
        if st.startswith("error") or \
                "could not create your google account" in _low or \
                "couldn't create your google account" in _low:
            await page.wait_for_timeout(3000)
            await ui.dump("signup_error")
            raise RegisterError(
                "Google 拒绝建号：Sorry, we could not create your Google Account."
                "（服务端风控 —— 出口 IP 信誉 / 同一出口高频注册所致，"
                "不是表单填写问题；换住宅/移动出口并放慢节奏后重试）")

        # 卡死护栏：同一步原地打转说明交互没生效，早停早报，别空转 80 轮
        if stuck >= 8:
            await ui.dump("stall_%s" % (st or "none"))
            raise RegisterError("同一步 %r 重复 %d 次仍未推进（见 _gacc_dump_stall_*.html）"
                                % (st, stuck))

        # --- A. 姓名 ---
        if st == "name" or await ui.vis(SEL_FIRST):
            await fill_verified(page, SEL_FIRST, ident["first"])
            try:
                await page.fill(SEL_LAST, ident["last"])
            except Exception:
                pass
            say("[flow] 姓名 -> %s %s | Next: %s"
                % (ident["first"], ident["last"], await click_next(page) or "未找到"))
            await page.wait_for_timeout(step_wait_ms)

        # --- B. 生日 + 性别（无原生 select，走 Material 下拉）---
        elif st in ("birthdaygender", "birthday-gender"):
            ok_d = await fill_verified(page, SEL_DAY, ident["day"])
            ok_y = await fill_verified(page, SEL_YEAR, ident["year"])
            ok_m = await pick_mdc(page, SEL_MONTH,
                                  BIRTHDAY_MONTHS[int(ident["month"]) - 1])
            ok_g = await pick_mdc(page, SEL_GENDER,
                                  GENDER_TEXT.get(ident["gender"], "Male"))
            say("[flow] 生日性别 day=%s year=%s month=%s gender=%s | Next: %s"
                % (ok_d, ok_y, ok_m, ok_g, await click_next(page) or "未找到"))
            await page.wait_for_timeout(step_wait_ms)

        # --- C. 用户名 / 邮箱 ---
        elif st in ("username", "emailsignup") or await ui.vis(SEL_USERNAME):
            if not switched:
                try:
                    await page.click('button:has-text("%s")' % TXT_USE_EXISTING,
                                     timeout=6000)
                    say("[flow] 点「%s」" % TXT_USE_EXISTING)
                    switched = True
                    await page.wait_for_timeout(1800)
                    continue
                except Exception:
                    say("[flow] ⚠ 未找到「%s」按钮" % TXT_USE_EXISTING)
                    switched = True
            ok = (await fill_verified(page, SEL_USERNAME, email)
                  or await fill_verified(page, "#identifierId", email))
            say("[flow] 填邮箱 %s -> %s | Next: %s"
                % (email, ok, await click_next(page) or "未找到"))
            await page.wait_for_timeout(step_wait_ms)

        # --- D. 邮箱验证码（未实测）---
        elif st in ("emailcode", "emailverification", "verify", "code") \
                or await ui.first_vis(SELS_CODE):
            say("[flow] 验证码页：等待邮件…")
            try:
                code = catcher.wait_code(
                    email,
                    timeout=int(os.environ.get("GACC_CODE_TIMEOUT", "180") or 180),
                    seen=code_seen)
            except Exception as e:
                raise RegisterError("收码失败: %s" % e)
            say("[flow] 收到验证码 %s" % code)
            boxes = page.locator('input[maxlength="1"]')
            if await boxes.count() >= 4:
                for i, ch in enumerate(code):
                    try:
                        await boxes.nth(i).fill(ch)
                    except Exception:
                        break
            else:
                for s in SELS_CODE:
                    if await fill_verified(page, s, code):
                        break
            say("[flow] Next: %s" % (await click_next(page) or "未找到"))
            await page.wait_for_timeout(step_wait_ms)

        # --- E. 密码（未实测）---
        elif st == "password" or await ui.vis(SELS_PASSWD[0]):
            await fill_verified(page, SELS_PASSWD[0], password)
            for s in SELS_PASSWD2:
                try:
                    if await page.locator(s).count():
                        await page.fill(s, password)
                except Exception:
                    pass
            say("[flow] 设置密码 | Next: %s" % (await click_next(page) or "未找到"))
            await page.wait_for_timeout(step_wait_ms)

        # --- F. 完成判定 ---
        elif st in ("done", "complete", "welcome", "success"):
            say("[flow] ✓ 注册流程完成（step=%s）" % st)
            break

        # --- G. 跳过恢复信息页 ---
        # 注意：不能在生成器表达式里 await（会被编译成 async generator，
        # any() 迭代即 TypeError）。复用本轮 443 行已取样的 _low（已小写）。
        elif any(k in _low for k in
                 ["recovery", "add recovery", "復元", "恢复信息"]):
            hit = 0
            for t in SKIP_TEXTS:
                try:
                    loc = page.get_by_text(t, exact=False).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=5000)
                        hit = t
                        break
                except Exception:
                    continue
            say("[flow] 跳过恢复信息页: %r" % hit)
            await page.wait_for_timeout(1500)

        # --- I. Google 建号失败页（/signup/error，实测：设密码提交后出现）---
        elif st.startswith("error"):
            await page.wait_for_timeout(3500)      # URL 先变、DOM 后渲染
            await ui.dump("signup_error")
            raise RegisterError(
                "Google 拒绝建号：Sorry, we could not create your Google Account."
                "（服务端风控 —— 出口 IP 信誉 / 同一出口高频注册所致，"
                "不是表单填写问题；换住宅/移动出口并放慢节奏后重试）")

        # --- J. 风控变体页快停 ---
        # 注意用 path 判断，不能用整条 URL —— 正常注册 URL 的 query 里就带
        # flowName=GlifWebSignIn，按整条 URL 匹配 "signin" 会误杀正常流程。
        _path = urlsplit(url or "").path.lower()
        if "/signin" in _path or "qr" in _path \
                or "scan the qr code" in _low or "二维码" in _low:
            await ui.dump("signup_qr_variant")
            raise RegisterError(
                "Google 把本次注册切到了「扫码/登录」变体页（出口 IP 信誉触发服务端风控，"
                "与本地程序无关）——换一个住宅出口重试即可；页面已存 "
                "_gacc_dump_signup_qr_variant.html/.png")
        if "unable to connect" in _low or "problem loading page" in _low:
            raise RegisterError(
                "浏览器连不上 Google（出口不通或没走代理）——检查代理设置；"
                "大陆网络直连不可用，必须走住宅代理")

        # --- H. 兜底 ---
        else:
            hit = await click_next(page)
            if hit:
                say("[flow] step%d 兜底 Next (%s)" % (step, hit))
                await page.wait_for_timeout(step_wait_ms)
                continue
            say("[flow] step%d 未识别页面 url=%s" % (step, url[:110]))
            if stuck in (2, 5):
                await ui.dump("unknown%d" % step)
            await page.wait_for_timeout(step_wait_ms)

    # ---- 确认已登录 ----
    try:
        await page.goto("https://myaccount.google.com/", timeout=45000)
        await page.wait_for_timeout(3000)
        if "myaccount.google.com" not in page.url:
            raise RegisterError("注册后未进入 myaccount（url=%s）" % page.url[:110])
        say("[flow] ✓ 已确认登录 myaccount.google.com")
    except RegisterError:
        raise
    except Exception as e:
        raise RegisterError("登录确认失败: %s" % str(e)[:110])

    secret = ""
    if want_totp:
        try:
            secret = await enroll_2fa(page, ui)
        except Exception as e:
            say("[2fa] 跳过: %s" % str(e)[:110])
    return secret


async def enroll_2fa(page, ui: UI) -> str:
    """进入 Google 账号 2FA 设置，选 Authenticator app，抓 otpauth secret。**未实测**。"""
    await page.goto("https://myaccount.google.com/signinoptions/two-step-verification",
                    timeout=60000)
    await ui.wait_stable()
    for t in ["Get started", "開始", "开始使用", "始める"]:
        try:
            loc = page.get_by_text(t, exact=False).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=6000)
                break
        except Exception:
            continue
    await page.wait_for_timeout(2500)
    if await ui.vis('input[name="Passwd"]'):
        say("[2fa] 需要重输密码 —— 骨架未实现（跳过 2FA）")
        return ""
    for t in ["Authenticator", "認証アプリ", "身份验证器"]:
        try:
            loc = page.get_by_text(t, exact=False).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=6000)
                break
        except Exception:
            continue
    await page.wait_for_timeout(2500)
    for t in ["Set up", "設定", "设置", "Continue", "次へ"]:
        try:
            loc = page.get_by_text(t, exact=False).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=6000)
                break
        except Exception:
            continue
    await page.wait_for_timeout(3000)

    js = r"""() => {
      const html = document.documentElement.innerHTML;
      let m = html.match(/secret=([A-Z2-7]{16,64})/i);
      if (m) return m[1];
      m = html.match(/otpauth:\/\/totp[^"'\s]+/i);
      if (m) { const k = m[0].match(/secret=([A-Z2-7]+)/i); if (k) return k[1]; }
      const t = document.body.innerText || '';
      const g = t.match(/\b([A-Z2-7]{4}\s+){3,7}[A-Z2-7]{4}\b/);
      if (g) return g[0].replace(/\s+/g, '');
      return '';
    }"""
    try:
        secret = await page.evaluate(js)
    except Exception:
        secret = ""
    if secret:
        say("[2fa] ✓ 抓到 TOTP 密钥（%d 字符）" % len(secret))
    else:
        say("[2fa] 未抓到密钥（页面结构可能已变）")
        await ui.dump("2fa")
    return secret or ""


# ------------------------------------------------------------------ 单号注册
async def _open_context(engine: str, p, prof: Path, proxy_kw: dict | None,
                        headless: bool, lang: str, launcher_args: list[str]):
    """按引擎建立浏览器上下文，返回 (context, 需 teardown 的上下文管理器或 None)。

    engine="camoufox"（默认）—— Firefox **引擎层**反检测，C++ 级指纹伪装。
        关键：**不传 locale / timezone_id**，交给 AsyncNewContext 按代理出口 IP
        自动对齐（timezone_id / WebRTC IP / locale），避免「时区写死」自相矛盾。
        实测（_diag_stealth_compare.py，2026-10-11）：无头模式仅 1 项软信号；
        而 patchright 无头有 5 项硬伤（HeadlessChrome UA / 无 window.chrome /
        plugins=0 / WebGL=SwiftShader / languages 过窄）。

        指纹来源（GACC_FP，2026-10-12）：默认 "preset" 走包内**真实指纹库**
        （get_random_preset，40 次采样 26 个不同组合，分辨率都是 1920x1080 /
        1600x900 这类真实值）；"gen" 回退 fpgen 合成（无限唯一，但实测会产出
        1707x1067 / 2176x1224 等不常见分辨率，自然度差）。

    engine="patchright" —— Chromium 驱动层补丁（回落方案）。
        ⚠️ 无头模式会泄漏 `HeadlessChrome/...` UA，Google 一眼识破；
        非要用就必须 headless=False（有头）。
    """
    eng = (engine or "camoufox").strip().lower()
    if eng == "camoufox":
        from camoufox.async_api import AsyncCamoufox, AsyncNewContext
        # humanize：光标移动/输入的人类化节奏（行为层反检测，默认开；GACC_HUMANIZE=0 关）
        _hum = os.environ.get("GACC_HUMANIZE", "1").strip().lower() not in ("0", "false", "off", "")
        cm = AsyncCamoufox(headless=headless, os="windows", humanize=(True if _hum else False))
        browser = await cm.__aenter__()
        try:
            ckw: dict = {"os": "windows"}
            # 指纹策略（2026-10-12）：默认走**真实指纹库** preset，比 fpgen 合成更接近真实
            # 设备分布（fpgen 实测会产出 1707x1067 / 2176x1224 这类不常见分辨率）。
            # 用 GACC_FP=gen 可回退 fpgen 合成（无限唯一但分布不自然）。
            fp_mode = os.environ.get("GACC_FP", "preset").strip().lower()
            preset = None
            if fp_mode != "gen":
                try:
                    from camoufox.fingerprints import get_random_preset
                    preset = get_random_preset(os="windows")
                except Exception as exc:
                    say("[reg] preset 取用失败，回落 fpgen：%s" % str(exc)[:80])
            if preset:
                ckw["preset"] = preset
            if proxy_kw:
                ckw["proxy"] = proxy_kw      # 触发 geoip：时区/WebRTC 自动对齐
            ctx = await AsyncNewContext(browser, **ckw)
        except BaseException:
            await cm.__aexit__(None, None, None)
            raise
        say("[reg] 引擎=camoufox（Firefox 引擎级伪装，指纹=%s%s%s）"
            % ("真实preset" if preset else "fpgen合成",
               "，geoip 已按出口自动对齐时区" if proxy_kw else "，未配代理",
               "，humanize 行为伪装开" if _hum else ""))
        return ctx, cm

    # ---- patchright（Chromium）回落 ----
    kw: dict = {"headless": headless, "args": launcher_args}
    if lang:
        kw["locale"] = lang
    tz = os.environ.get("GACC_TZ", "").strip()
    if tz:
        kw["timezone_id"] = tz           # 不写死；留空则用宿主时区
    if proxy_kw:
        kw["proxy"] = proxy_kw
    ctx = await p.chromium.launch_persistent_context(str(prof), **kw)
    say("[reg] 引擎=patchright%s" % ("（⚠️ 无头会泄漏 HeadlessChrome UA）"
                                     if headless else ""))
    return ctx, None


async def register_one(p, email: str, password: str, catcher: MailCatcher,
                       proxy_kw: dict | None, headless: bool, lang: str,
                       want_totp: bool, step_wait_ms: int, keep_profile: bool,
                       launcher_args: list[str], stop_at: str = "",
                       engine: str = "camoufox", entry: str = "auto") -> dict:
    email_tag = email.split("@")[0]
    prof = WORKDIR / "_gacc_profiles" / email_tag
    if not keep_profile:
        # 一次性环境：独立带时间戳的目录，跑完即删，绝不复用旧 cookie/缓存
        prof = WORKDIR / "_gacc_profiles" / ("%s__%s" % (email_tag, time.strftime("%Y%m%d%H%M%S")))
    prof.mkdir(parents=True, exist_ok=True)
    say("[reg] 初始化（收件基线 + 启动浏览器）…")
    # 刷新收件基线：本号只认「此刻之后」到达的邮件，避免抽到历史旧码
    if catcher is not None:
        try:
            catcher.prime(email)
        except Exception as e:
            say("[reg] prime warn: %s" % str(e)[:80])
    res = {"email": email, "password": password, "totp": "", "ok": False,
           "error": "", "stopped": False, "entry": (entry or "auto")}
    ctx, _cm = await _open_context(engine, p, prof, proxy_kw, headless, lang,
                                   launcher_args)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        entry_used = ""
        if (entry or "auto").strip().lower() in ("", "classic"):
            # 经典直链入口：先预热养 Cookie，再进注册页（降低 QR 风控变体页触发率）
            await warm_up(page)
            try:
                await page.goto(SIGNUP_URL + "&hl=" + lang.split("-")[0],
                                wait_until="domcontentloaded", timeout=90000)
            except Exception as e:
                say("[reg] goto warn: %s" % str(e)[:110])
        else:
            # 产品侧入口（youtube/drive/auto）：自带预热效果，并带失败回退链
            entry_used = await open_signup_entry(ctx, page, entry, lang)
            res["entry"] = entry_used
        await page.wait_for_timeout(5000)
        say("[reg] 注册页 url=%s" % page.url[:120])
        try:
            res["totp"] = await signup_flow(ctx, page, email, password, catcher,
                                            want_totp, step_wait_ms, stop_at)
            res["ok"] = True
        except StoppedAtStep as e:
            res["stopped"] = True
            res["error"] = str(e)
            say("[reg] ⏸ 按 GACC_STOP_AT 停止：%s" % e)
        except Exception as e:
            res["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
            say("[reg] ✗ %s" % res["error"])
            try:
                import traceback
                say("[reg] traceback:\n%s" % traceback.format_exc()[-1800:])
            except Exception:
                pass
            try:
                await page.screenshot(
                    path=str(WORKDIR / ("_gacc_fail_%s.png" % email.split("@")[0])))
            except Exception:
                pass
    finally:
        try:
            if _cm is not None:
                await _cm.__aexit__(None, None, None)   # camoufox：关浏览器+playwright
            else:
                await ctx.close()
        except Exception:
            pass
        if not keep_profile:
            import shutil
            try:
                shutil.rmtree(str(prof), ignore_errors=True)
            except BaseException:            # 沙箱 shim 可能抛 SystemExit
                say("[reg] profile 未删除（受保护）：%s" % prof)
    return res


# ------------------------------------------------------------------ 主流程
def load_env():
    return {
        "domain": os.environ.get("DOMAIN", "").strip(),
        "proxy": os.environ.get("PROXY", os.environ.get("GACC_PROXY", "")).strip(),
        "count": int(os.environ.get("COUNT", "1")),
        "password": os.environ.get("PASSWORD", "").strip(),
        "prefix_len": int(os.environ.get("PREFIX_LEN", "8")),
        "headless": os.environ.get("HEADLESS", "0") == "1",
        "want_totp": os.environ.get("GOOGLE_2FA", "1") == "1",
        "out": os.environ.get("GACC_OUT", str(WORKDIR / "谷歌账号_产出.txt")),
        "keep_profile": os.environ.get("GACC_KEEP_PROFILE", "1") == "1",
        "step_wait_ms": int(os.environ.get("GACC_STEP_WAIT_MS", "2500")),
        "lang": os.environ.get("GACC_LANG", "en-US"),
        "stop_at": os.environ.get("GACC_STOP_AT", "").strip(),
        "engine": os.environ.get("GACC_ENGINE", "camoufox").strip().lower(),
    }


@asynccontextmanager
async def _maybe_playwright(engine: str):
    """camoufox 自带 playwright 生命周期（AsyncCamoufox 本身就是上下文管理器），
    不需要外层再起一个驱动；只有 patchright 引擎才需要。"""
    if (engine or "").strip().lower() == "camoufox":
        yield None
        return
    from patchright.async_api import async_playwright
    async with async_playwright() as p:
        yield p


async def run(cfg):
    try:
        from browser import LAUNCH_ARGS
    except Exception:
        LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled",
                       "--no-first-run", "--no-default-browser-check",
                       "--disable-background-networking"]

    # 联调模式（stop_at）不会走到收码步，允许收件后端缺省
    catcher = None
    if not cfg["stop_at"]:
        catcher = MailCatcher.from_env()
    proxy_kw = _proxy_kwargs(cfg["proxy"])
    out_path = Path(cfg["out"])
    say("=" * 68)
    say("域名=%s | 代理=%s | 数量=%d | 有头=%s | 引擎=%s | 收件后端=%s | 2FA=%s%s"
        % (cfg["domain"], cfg["proxy"][:40] or "(无)",
           cfg["count"], not cfg["headless"],
           cfg.get("engine", "camoufox"),
           catcher.name if catcher else "(联调跳过)",
           cfg["want_totp"],
           " | STOP_AT=%s" % cfg["stop_at"] if cfg["stop_at"] else ""))
    say("=" * 68)

    results = []
    async with _maybe_playwright(cfg.get("engine", "camoufox")) as p:
        for i in range(cfg["count"]):
            email = rand_email(cfg["domain"], cfg["prefix_len"])
            password = cfg["password"] or rand_password()
            say("\n----- [%d/%d] %s -----" % (i + 1, cfg["count"], email))
            r = await register_one(p, email, password, catcher, proxy_kw,
                                   cfg["headless"], cfg["lang"], cfg["want_totp"],
                                   cfg["step_wait_ms"], cfg["keep_profile"],
                                   LAUNCH_ARGS, cfg["stop_at"],
                                   cfg.get("engine", "camoufox"))
            results.append(r)
            if r["ok"]:
                line = "----".join([r["email"], r["password"], r["totp"]])
                with open(out_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                say("[save] ✓ %s" % line)
            else:
                say("[save] ✗ 未产出（%s）" % r["error"][:90])

    ok = sum(1 for r in results if r["ok"])
    stop_hit = any(r["stopped"] for r in results)
    say("\n==== 汇总 ====")
    say("成功 %d / %d → %s%s"
        % (ok, len(results), out_path, "（含联调停止）" if stop_hit else ""))
    for r in results:
        tag = "✓" if r["ok"] else ("⏸" if r["stopped"] else "✗")
        say("  %s %s %s" % (tag, r["email"],
                            ("totp=" + r["totp"][:8] + "…") if r["totp"] else ""))
    (WORKDIR / "_gacc_last_run.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return ok, stop_hit


def dry_run(cfg):
    say("[dry-run] 配置检查")
    say("  DOMAIN      : %s" % (cfg["domain"] or "❌ 未设置"))
    say("  PROXY       : %s" % (cfg["proxy"][:50] or "❌ 未设置（真实注册必填）"))
    say("  COUNT       : %d" % cfg["count"])
    say("  PASSWORD    : %s" % ("(固定)" if cfg["password"] else "(随机生成)"))
    try:
        mb = MailCatcher.from_env()
        say("  收件后端    : %s ✓" % mb.name)
    except Exception as e:
        say("  收件后端    : ❌ %s" % e)
    say("  样例账号     :")
    for _ in range(min(3, max(1, cfg["count"]))):
        say("    %s----%s" % (rand_email(cfg["domain"] or "example.com", cfg["prefix_len"]),
                              cfg["password"] or rand_password()))


def main():
    ap = argparse.ArgumentParser(description="Google 账号注册骨架")
    ap.add_argument("--dry-run", action="store_true", help="只校验配置，不启动浏览器")
    args = ap.parse_args()
    cfg = load_env()
    if args.dry_run:
        dry_run(cfg)
        return
    if not cfg["domain"]:
        raise SystemExit("必须设置 DOMAIN（catch-all 邮箱域名）")
    if not cfg["proxy"]:
        raise SystemExit("必须设置 PROXY（能访问 accounts.google.com 的住宅/移动代理）")
    n, stopped = asyncio.run(run(cfg))
    os._exit(0 if (n or stopped) else 1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        say("✗ %r" % e)
        sys.exit(1)
