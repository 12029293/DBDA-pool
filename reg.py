# -*- coding: utf-8 -*-
r"""DOLA 批量注册 —— 产出可直接喂给 run.py 的账号（DBDA cookie_state.json 格式）

用法:
    python reg.py 3              # 注册 3 个账号，批次名默认 i713a
    python reg.py 8 mybatch      # 注册 8 个，批次名 mybatch

产出:
    acc/<批次>_00/cookie_state.json ...    ← run.py 直接读这个目录
    pool/<批次>_00.json / .cookie.txt / .netscape.txt   ← 便于人工查看

机制（fact 399/499）:
  * 每个账号一个独立 jar；先 GET /chat/ 让服务端铸新 ttwid（一号一 ttwid，绝不共享 jar）
  * /passport/web/email/send_code/?type=16 无验证码；一次性邮箱（mailto.plus/tempmail.plus）收码
  * /passport/web/email/register_verify_login/ 建号并下发完整登录态 cookie
  * 注册走系统代理（你机器 = Clash 7897；直连会被 1206 限流）
"""
import os
import sys
import json
import time

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
sys.path.insert(0, _SELF)
import dc  # 本目录下的 dc.py（自包含）

ACC = os.path.join(HERE, "acc")
POOL = os.path.join(HERE, "pool")
os.makedirs(ACC, exist_ok=True)
os.makedirs(POOL, exist_ok=True)
dc.POOL = POOL

# ★ 作为模块被 run_pool.py import 时绝不触发注册主流程；
#   N/BATCH 仅在命令行直跑时从 argv 读取（import 时 argv 可能不是注册参数）。
N = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 3
BATCH = sys.argv[2] if len(sys.argv) > 2 else "i713a"
_is_cli = os.path.basename(sys.argv[0] or "").lower().startswith("reg")


def to_dbda_state(label, row):
    """把 dc.register_one 的 row 转成 DBDA 引擎能吃的 cookie_state.json。"""
    pairs = dc.parse_ck(row["cookie"])
    recs = [{"name": k, "value": v, "domain": ".dola.com", "path": "/",
             "secure": True, "httpOnly": False, "sameSite": "Lax", "expires": -1}
            for k, v in pairs.items()]
    return {"label": label, "email": row["email"], "uid": row["user_id"],
            "ttwid": row.get("ttwid", ""), "t_reg": row["t_reg"],
            "t_reg_str": row.get("t_reg_str", ""),
            "cookie_header": row["cookie"], "cookies": recs}


def one(i):
    label = "%s_%02d" % (BATCH, i)
    t0 = time.time()
    row, err = dc.register_one(wait=220)
    if not row:
        r = {"label": label, "ok": False, "err": err, "secs": round(time.time() - t0, 1)}
        print(json.dumps(r, ensure_ascii=False), flush=True)
        return r
    row["label"] = label
    # 1) 通用导出（json/cookie/netscape）
    dc.export_account(label, row)
    # 2) DBDA 引擎格式：acc/<label>/cookie_state.json
    d = os.path.join(ACC, label)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "cookie_state.json"), "w", encoding="utf-8") as fh:
        json.dump(to_dbda_state(label, row), fh, ensure_ascii=False, indent=2)
    # 3) 存活校验
    chk = dc.login_check(row["cookie"])
    r = {"label": label, "ok": True, "email": row["email"], "uid": row["user_id"],
         "n_cookie": row["n_cookie"], "ttwid": row["ttwid"][:24] + "...",
         "secs": round(time.time() - t0, 1),
         "login_ok": bool(chk.get("user_id") and not chk.get("code")),
         "login": chk.get("user_id") or chk.get("code")}
    print(json.dumps(r, ensure_ascii=False), flush=True)
    return r


if __name__ == "__main__" or _is_cli:
    from concurrent.futures import ThreadPoolExecutor
    rows = []
    with ThreadPoolExecutor(max_workers=3) as tp:
        for r in tp.map(one, range(N)):
            rows.append(r)
    ok = sum(1 for r in rows if r.get("ok"))
    print("=== registered %d/%d -> acc/<%s>_NN/cookie_state.json ===" % (ok, N, BATCH), flush=True)
