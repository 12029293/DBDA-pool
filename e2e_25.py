# -*- coding: utf-8 -*-
"""P3 实验：走代理换 IP，用 seedance_v2.5 出片

同时验证两件事：
  1) 710022002 限流是否 IP 级（换 IP 能否绕过）
  2) seedance_v2.5 是否真需要会员（协议层是否校验）
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, r"C:/Users/Administrator/Desktop/dola-pool")
from doubao_cn_video import DoubaoVideoClient  # noqa: E402

CK = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:/Users/Administrator/Desktop/纯算注册/doubao_ck/doubao_7006284f659f416a_20261008_045605.txt"
PROXY = sys.argv[2] if len(sys.argv) > 2 else "socks5://127.0.0.1:7897"
MODEL = sys.argv[3] if len(sys.argv) > 3 else "seedance_v2.5"


async def main():
    ck = Path(CK).read_text(encoding="utf-8").strip()
    cli = DoubaoVideoClient(ck, proxy=PROXY)
    print("proxy:", PROXY, "| model:", MODEL, flush=True)
    print("check_login:", await cli.check_login(), flush=True)
    try:
        url = await cli.generate_video("一只柴犬在雪地里奔跑，阳光明媚",
                                       ratio="9:16", duration=5, model=MODEL, timeout=300)
        print("\n★ 出片成功:", (url or "")[:160], flush=True)
    except Exception as e:
        print("\n✗ 失败:", type(e).__name__, str(e)[:400], flush=True)


asyncio.run(main())
