# -*- coding: utf-8 -*-
"""i504 shared helpers: protocol-level registration (independent jar/ttwid per account),
domain-tagged cookie export, external-client probes (login/keepalive/out-片 submit).

Design notes (per facts 499/497/490):
  * one account == one cookie jar == one server-issued ttwid  (never share a jar)
  * exported cookie is the ONLY thing the external client may use -> never re-load a page
  * all requests go through plain `requests` with the exported cookie header
"""
import os, re, sys, json, time, uuid, random, string
import http.cookiejar, urllib.request, urllib.parse, urllib.error
from urllib.parse import urlencode
import requests

sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://www.dola.com"
AID = "495671"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BP = {"aid": AID, "account_sdk_source": "web",
      "sdk_version": "2.2.11-doubao.0", "device_platform": "web"}
TEMPMAIL = "https://tempmail.plus/api/mails"
_SELF = os.path.dirname(os.path.abspath(__file__))


def _find_data_root(_start, _depth=4):
    """数据根 = 含 DOLA-pool-main\\ 或 tasks.txt 的那一级（向上最多找 4 层）。

    本文件可能就在数据根，也可能在其下的 engine\\ 子目录 —— 两种布局都定位到
    同一份 acc/ out/ pool/ tasks.txt/ STOP，不会各建一套空数据。
    """
    _cur = _start
    for _ in range(_depth):
        if (os.path.isdir(os.path.join(_cur, "DOLA-pool-main"))
                or os.path.exists(os.path.join(_cur, "tasks.txt"))):
            return _cur
        _parent = os.path.dirname(_cur)
        if _parent == _cur:
            break
        _cur = _parent
    return _start


HERE = _find_data_root(_SELF)
POOL = os.path.join(HERE, "pool")
os.makedirs(POOL, exist_ok=True)


# ---------------- cookie helpers ----------------
def ex(c, k):
    m = re.search(re.escape(k) + r"=([^;]+)", c or "")
    return m.group(1) if m else ""


def parse_ck(raw):
    d = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def qparams(c=None, region="JP"):
    p = {"aid": AID, "channel": "g", "device_platform": "web", "language": "zh-Hant",
         "region": region, "sys_region": region, "samantha_web": "1",
         "use-olympus-account": "1", "version_code": "20800",
         "web_platform": "browser", "web_tab_id": str(uuid.uuid4())}
    mst = ex(c, "msToken")
    fp = ex(c, "s_v_web_id")
    if mst:
        p["msToken"] = mst
    if fp:
        p["fp"] = fp
    return p


def headers(c, referer=None, ct="application/json"):
    h = {"Accept": "*/*", "Accept-Encoding": "identity",
         "Accept-Language": "en,ja;q=0.9,en-US;q=0.8,en;q=0.7",
         "Cache-Control": "no-cache", "Origin": BASE, "Pragma": "no-cache",
         "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
         "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": '"Windows"',
         "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
         "User-Agent": UA, "Content-Type": ct, "agw-js-conv": "str, str",
         "Referer": referer or (BASE + "/chat/")}
    if c:
        h["Cookie"] = c
    return h


# ---------------- registration (pure wire, own jar) ----------------
def _cli():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Accept", "*/*")]
    return op, cj


def _req(op, url, method="GET", payload=None):
    data = urllib.parse.urlencode(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        resp = op.open(r, timeout=25)
        return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "ERR %s %s" % (type(e).__name__, e)


def _code_of(t):
    m = re.findall(r"\b(\d{6})\b", t or "")
    return m[0] if m else None


def poll_code(email, wait=180):
    op, cj = _cli()
    seen = set()
    dl = time.time() + wait
    while time.time() < dl:
        _, b = _req(op, "%s?email=%s&limit=8" % (TEMPMAIL, urllib.parse.quote(email)))
        try:
            ml = (json.loads(b) or {}).get("mail_list") or []
        except Exception:
            ml = []
        for m in ml:
            mid = m.get("mail_id") or m.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            c = _code_of(m.get("subject"))
            if not c:
                _, b2 = _req(op, "%s/%s?email=%s" % (TEMPMAIL, mid, urllib.parse.quote(email)))
                try:
                    j2 = json.loads(b2)
                    c = _code_of((j2.get("text") or "") + " " + (j2.get("subject") or ""))
                except Exception:
                    c = _code_of(b2)
            if c:
                return c
        time.sleep(3)
    return None


def rand_email(domain="mailto.plus"):
    tag = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    return tag + "@" + domain


def register_one(domain="mailto.plus", password=None, wait=180):
    """Register one account in a *dedicated* jar. Returns (row, err).

    The jar is created fresh here -> GET /chat/ mints a brand-new server ttwid,
    so this account is natively bound to its own device cookie (fact 499 rule).
    """
    op, cj = _cli()
    email = rand_email(domain)
    _req(op, BASE + "/chat/")                                    # fresh ttwid for THIS jar
    st, b = _req(op, BASE + "/passport/web/email/send_code/?" +
                 urllib.parse.urlencode(dict(BP, email=email, type="16")))
    if "success" not in (b or ""):
        return None, "send_code: " + (b or "")[:150]
    code = poll_code(email, wait)
    if not code:
        return None, "no verification code"
    pl = dict(BP); pl.update({"email": email, "type": "16", "code": code})
    if password:
        pl["password"] = password
    st, b = _req(op, BASE + "/passport/web/email/register_verify_login/", "POST", pl)
    try:
        dat = (json.loads(b) or {}).get("data") or {}
    except Exception:
        dat = {}
    uid = dat.get("user_id_str")
    if not uid:
        return None, "register: " + (b or "")[:200]
    _req(op, BASE + "/"); _req(op, BASE + "/chat/")              # settle cookies (same jar)
    ck = {c.name: c.value for c in cj}
    ckstr = "; ".join("%s=%s" % (k, v) for k, v in ck.items())
    row = {"email": email, "user_id": uid, "password": password or "",
           "cookie": ckstr, "n_cookie": len(ck), "ttwid": ck.get("ttwid", ""),
           "t_reg": time.time(), "t_reg_str": time.strftime("%Y-%m-%d %H:%M:%S")}
    return row, None


# ---------------- domain-tagged export ----------------
def export_account(label, row):
    """Write three artifacts for external reuse: json(with domain) / cookie header / netscape."""
    pairs = parse_ck(row["cookie"])
    recs = [{"name": k, "value": v, "domain": ".dola.com", "path": "/",
             "secure": True, "httpOnly": False, "sameSite": "Lax", "expires": -1}
            for k, v in pairs.items()]
    jp = os.path.join(POOL, "%s.json" % label)
    json.dump({"label": label, "email": row["email"], "uid": row["user_id"],
               "ttwid": row.get("ttwid", ""), "t_reg": row["t_reg"],
               "t_reg_str": row.get("t_reg_str", ""),
               "cookie_header": row["cookie"], "cookies": recs},
              open(jp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    open(os.path.join(POOL, "%s.cookie.txt" % label), "w", encoding="utf-8").write(row["cookie"])
    lines = ["# Netscape HTTP Cookie File"]
    for r_ in recs:
        lines.append("\t".join([r_["domain"], "TRUE", "/", "TRUE", "-1", r_["name"], r_["value"]]))
    open(os.path.join(POOL, "%s.netscape.txt" % label), "w", encoding="utf-8").write("\n".join(lines))
    return jp


def load_external(path):
    """The ONLY entry point for the external client: read an exported artifact.

    Accepts .json (preferred) or .cookie.txt / netscape. Returns (label, ck_header).
    """
    if path.endswith(".json"):
        d = json.load(open(path, encoding="utf-8"))
        if d.get("cookie_header"):
            return d.get("label", os.path.basename(path)), d["cookie_header"]
        pairs = [(c["name"], c["value"]) for c in d.get("cookies", [])]
        return d.get("label", ""), "; ".join("%s=%s" % kv for kv in pairs)
    raw = open(path, encoding="utf-8", errors="replace").read().strip()
    if raw.startswith("# Netscape"):
        out = []
        for ln in raw.splitlines():
            if ln.startswith("#") or not ln.strip():
                continue
            f = ln.split("\t")
            if len(f) >= 7:
                out.append("%s=%s" % (f[5], f[6]))
        return os.path.basename(path), "; ".join(out)
    return os.path.basename(path), raw


# ---------------- external-client probes ----------------
def login_check(ck, region="JP", timeout=20):
    url = BASE + "/passport/account/info/v2/?" + urlencode(qparams(ck, region))
    t0 = time.time()
    try:
        r = requests.get(url, headers=headers(ck), timeout=timeout)
        j = r.json()
        d = j.get("data") or j
        return {"http": r.status_code, "code": j.get("code"), "msg": (j.get("message") or "")[:60],
                "user_id": d.get("user_id") or d.get("uid"),
                "name": d.get("screen_name") or d.get("name"),
                "secs": round(time.time() - t0, 2)}
    except Exception as e:
        return {"exc": repr(e), "secs": round(time.time() - t0, 2)}


BOT_ID = "7339470689562525703"


def build_video_body(prompt, c, ratio="9:16", duration=5, conversation_id="", need_create=True):
    now_ms = int(time.time() * 1000); now_sec = now_ms // 1000
    return {
        "client_meta": {"local_conversation_id": "local_%d" % now_ms,
                        "conversation_id": conversation_id or "", "bot_id": BOT_ID,
                        "last_section_id": "", "last_message_index": None},
        "messages": [{"local_message_id": str(uuid.uuid4()),
                      "content_block": [{"block_type": 10000,
                                         "content": {"text_block": {"text": "生成影片：%s，%s" % (prompt, ratio),
                                                                    "icon_url": "", "icon_url_dark": "", "summary": ""},
                                                     "pc_event_block": ""},
                                         "block_id": str(uuid.uuid4()), "parent_id": "",
                                         "meta_info": [], "append_fields": []}],
                      "message_status": 0}],
        "option": {"create_time_ms": now_ms, "need_create_conversation": need_create,
                   "conversation_init_option": {"need_ack_conversation": True},
                   "unique_key": str(uuid.uuid4()),
                   "sse_recv_event_options": {"support_chunk_delta": True}, "is_old_user": False,
                   "message_from": 0,
                   "recovery_option": {"is_recovery": False, "req_create_time_sec": now_sec,
                                       "append_sse_event_scene": 0}},
        "chat_ability": {"ability_type": 17,
                         "ability_param": json.dumps({"ratio": ratio, "model": "seedance_v2.0",
                                                      "duration": duration})},
        "user_context": [],
        "ext": {"fp": ex(c, "s_v_web_id"), "use_deep_think": "0", "sub_conv_firstmet_type": "1",
                "conversation_init_option": '{"need_ack_conversation":true}',
                "commerce_credit_config_enable": "0"},
    }


def submit(ck, label="", region="JP", prompt="一只猫在草地上追逐蝴蝶", read_wait=18, timeout=60):
    """One POST /chat/completion (ability_type=17). Returns outcome dict."""
    url = BASE + "/chat/completion?" + urlencode(qparams(ck, region))
    t0 = time.time()
    out = {"label": label, "ts": round(t0, 3), "tstr": time.strftime("%H:%M:%S", time.localtime(t0))}
    try:
        r = requests.post(url, headers=headers(ck), json=build_video_body(prompt, ck),
                          stream=True, timeout=timeout)
    except Exception as e:
        out["outcome"] = "EXC"; out["exc"] = repr(e); out["elapsed"] = round(time.time() - t0, 2)
        return out
    out["http"] = r.status_code
    out["logid"] = r.headers.get("X-TT-LOGID", "")
    conv = ""; errcode = ""; errmsg = ""; buf = ""; n_ev = 0
    try:
        for chunk in r.iter_content(chunk_size=None, decode_unicode=False):
            if not chunk:
                continue
            buf += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                name = ""; dl = []
                for line in raw.split("\n"):
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        dl.append(line[5:].strip())
                ds = "\n".join(dl); n_ev += 1
                if name == "SSE_ACK":
                    try:
                        conv = (json.loads(ds).get("ack_client_meta") or {}).get("conversation_id", "") or conv
                    except Exception:
                        pass
                m = re.search(r'"error_code"\s*:\s*(\d+)', ds)
                if m and not errcode:
                    errcode = m.group(1)
                m2 = re.search(r'"error_msg"\s*:\s*"([^"]*)"', ds)
                if m2 and not errmsg:
                    errmsg = m2.group(1)
            if conv or errcode or (time.time() - t0 > read_wait):
                break
    except Exception as e:
        out["stream_exc"] = repr(e)
    out["elapsed"] = round(time.time() - t0, 2)
    out["conv"] = conv; out["errcode"] = errcode; out["errmsg"] = errmsg[:60]; out["n_ev"] = n_ev
    out["outcome"] = "SUCCESS" if (conv and not errcode) else ("E" + errcode if errcode else "NOACK")
    return out


# ---------------- downstream: receipt -> vid -> play info -> MP4 ----------------
def pull_chain(ck, conv, timeout=30):
    url = BASE + "/im/chain/single?" + urlencode(qparams(ck))
    body = {"cmd": 3100, "uplink_body": {"pull_singe_chain_uplink_body": {
                "conversation_id": conv, "anchor_index": 9007199254740991, "conversation_type": 3,
                "direction": 1, "limit": 30, "ext": {}, "filter": {"index_list": []},
                "evaluate_ab_params": "", "evaluate_common_params": ""}},
            "sequence_id": str(uuid.uuid4()), "channel": 2, "version": "1"}
    r = requests.post(url, headers=headers(ck, "%s/chat/%s" % (BASE, conv),
                                           "application/json; encoding=utf-8"),
                      json=body, timeout=timeout)
    return r.text


def get_play_info(ck, vid, timeout=30):
    url = BASE + "/samantha/video/get_play_info?" + urlencode(qparams(ck))
    r = requests.post(url, headers=headers(ck), json={"vid": vid}, timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


def download(url, path, limit=30 * 1024 * 1024):
    with requests.get(url, headers={"User-Agent": UA, "Referer": BASE},
                      stream=True, timeout=180) as resp:
        data = b""
        for ch in resp.iter_content(1024 * 256):
            if ch:
                data += ch
            if len(data) > limit:
                break
        open(path, "wb").write(data)
        return resp.status_code, len(data), data[:12].decode("latin1")


QUOTA_RE = re.compile(r"今日剩餘\s*(\d+)\s*個影片生成額度")


def collect_once(ck, conv, outdir, tag="ext", wait_done=95, poll=10):
    """Wait for the generation to finish, then pull receipt->vid->play->MP4."""
    t0 = time.time()
    rec = {"conv": conv, "tag": tag}
    while time.time() - t0 < wait_done:
        raw = pull_chain(ck, conv)
        vids = re.findall(r'"vid"\s*:\s*"([^"]+)"', raw)
        qm = QUOTA_RE.search(raw)
        if qm:
            rec["remaining_quota"] = int(qm.group(1))
        rec["generating_done"] = "影片生成完畢" in raw
        rec["failed"] = "影片生成失敗" in raw
        if vids and (rec["generating_done"] or rec["failed"] or time.time() - t0 > 40):
            rec["vids"] = vids
            vid = vids[0]
            http, j = get_play_info(ck, vid)
            d = (j or {}).get("data") or {}
            pi = (d.get("play_infos") or [{}])[0]
            main = pi.get("main") or pi.get("backup") or ""
            rec.update({"play_http": http, "play_code": (j or {}).get("code"),
                        "view": d.get("view_count"), "url_len": len(main)})
            if main:
                fn = os.path.join(outdir, "%s_%s.mp4" % (tag, vid[-8:]))
                try:
                    dh, nb, mg = download(main, fn)
                    rec.update({"dl_http": dh, "bytes": nb, "magic": mg,
                                "file": os.path.basename(fn)})
                except Exception as e:
                    rec["dl_exc"] = repr(e)
            break
        if rec["failed"]:
            break
        time.sleep(poll)
    return rec
