# -*- coding: utf-8 -*-
"""豆包域出片 · 端到端测试 / 校验 / 号池体检

用法（子命令与文档对齐，2026-10-08 补齐）：
    python test_doubao_video.py                          # 默认 CK 跑端到端
    python test_doubao_video.py <ck文件>                  # 显式 CK 跑端到端（兼容旧用法）
    python test_doubao_video.py check  <ck文件>           # 只校验登录态 + 会员权限
    python test_doubao_video.py gen    <ck文件> [9:16] [5] [seedance_v2.0] [300]
    python test_doubao_video.py pool   [--dir <ck目录>]   # 号池体检（调度层视角）

说明：
    - 输出全部 flush，便于长任务观察进度（此前管道缓冲导致「像卡住」）。
    - gen 成功后把 download_url 写 last_video_url.txt，并下载到 out_doubao_<ts>.mp4。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:/Users/Administrator/Desktop/dola-pool")

import aiohttp  # noqa: E402
from doubao_cn_video import (  # noqa: E402
    DoubaoVideoClient, DoubaoLoginError, DoubaoCreditError, CN_DOMAIN,
)

DEFAULT_CK = r"C:/Users/Administrator/Desktop/扫码测试/扫码CK.txt"

_t0 = time.time()


def log(*a):
    print(f"[{time.time() - _t0:7.1f}s]", *a, flush=True)


def _read_ck(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


async def _subscription(cli: DoubaoVideoClient) -> dict:
    url = f"https://{CN_DOMAIN}/alice/commerce/sale/subscription/entry/config/"
    try:
        async with aiohttp.ClientSession(**cli._sess_kw()) as s:
            async with s.post(url, params=cli._params(), json={},
                              headers={**cli._headers(), "agw-js-conv": "str, str"},
                              timeout=aiohttp.ClientTimeout(total=15)) as r:
                return await r.json(content_type=None)
    except Exception as e:
        return {"_err": repr(e)}


async def cmd_check(ck_file: str) -> int:
    cli = DoubaoVideoClient(_read_ck(ck_file))
    log("CK:", ck_file, "| 域:", cli.domain, "| sessionid:", cli.sessionid[:16])
    ok = await cli.check_login()
    log("登录态 (/alice/user/get_account_info):", ok)
    info = await cli.account_info()
    base = (info or {}).get("base_account_info") or {}
    log("手机号:", base.get("mobile"), "| user_id:", (info or {}).get("user_id"))
    sub = await _subscription(cli)
    d = sub.get("data") if isinstance(sub, dict) else None
    if isinstance(d, dict):
        log("会员: has_active_subscription=%s subs_status=%s name=%r is_gift=%s"
            % (d.get("has_active_subscription"), d.get("subs_status"),
               d.get("membership_display_name"),
               ((d.get("subscription") or {}).get("is_gift"))))
        log("提示: 非会员仍可出片（消耗每日免费额度），见交接文档 5.1")
    else:
        log("会员接口返回异常:", json.dumps(sub, ensure_ascii=False)[:200])
    return 0 if ok else 1


async def cmd_gen(ck_file: str, ratio="9:16", duration=5,
                  model="seedance_v2.0", timeout=300) -> int:
    cli = DoubaoVideoClient(_read_ck(ck_file))
    log("CK:", ck_file, "| 域:", cli.domain, "| is_valid:", cli.is_valid)
    log("STEP1 check_login ...", "→", await cli.check_login())
    log(f"STEP2 提交 /chat/completion  ratio={ratio} duration={duration} model={model}")
    try:
        url = await cli.generate_video(
            "一只白色小猫在草地上追蝴蝶", ratio=ratio, duration=duration,
            model=model, timeout=timeout)
    except DoubaoLoginError as e:
        log("❌ 登录/风控类失败:", e)
        return 2
    except DoubaoCreditError as e:
        log("❌ 额度不足:", e)
        return 3
    except Exception as e:
        log("❌ 其他失败:", repr(e))
        return 1
    log("✅ 出片成功:", url[:140])
    Path("last_video_url.txt").write_text(url, encoding="utf-8")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=300)) as r:
                data = await r.read()
        out = Path(f"out_doubao_{time.strftime('%Y%m%d_%H%M%S')}.mp4")
        out.write_bytes(data)
        log("已下载", out, f"{len(data)} bytes, magic={data[4:12]!r}")
    except Exception as e:
        log("下载失败（不影响出片结论）:", repr(e))
    return 0


async def cmd_pool(ck_dir: str | None) -> int:
    from doubao_scheduler import DoubaoVideoScheduler
    extra = [ck_dir] if ck_dir else None
    s = DoubaoVideoScheduler.from_default_dirs(extra_dirs=extra)
    log("发现账号:", s.accounts)
    log("available:", s.available)
    for row in s.account_status():
        log("  ", json.dumps(row, ensure_ascii=False))
    return 0


def _split_args(argv: list[str]):
    """把 [--key value] 与位置参数分开"""
    opts, pos = {}, []
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            k = argv[i][2:]
            v = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else "1"
            opts[k] = v
            i += 2
        else:
            pos.append(argv[i])
            i += 1
    return opts, pos


def main() -> int:
    opts, pos = _split_args(sys.argv[1:])
    sub = pos[0] if pos and pos[0] in ("check", "gen", "pool") else "gen"
    rest = pos[1:] if sub == pos[0] and pos and pos[0] in ("check", "gen", "pool") else pos

    if sub == "check":
        return asyncio.run(cmd_check(rest[0] if rest else DEFAULT_CK))
    if sub == "pool":
        return asyncio.run(cmd_pool(opts.get("dir")))
    ck = rest[0] if rest else DEFAULT_CK
    ratio = rest[1] if len(rest) > 1 else "9:16"
    duration = int(rest[2]) if len(rest) > 2 else 5
    model = rest[3] if len(rest) > 3 else "seedance_v2.0"
    timeout = int(rest[4]) if len(rest) > 4 else 300
    return asyncio.run(cmd_gen(ck, ratio, duration, model, timeout))


if __name__ == "__main__":
    sys.exit(main())
