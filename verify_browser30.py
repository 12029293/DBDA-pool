"""独立验证浏览器 30s 通道（不重启服务、不动号池库）。

用法: python verify_browser30.py <account> [prompt]
跑完打印：hook 是否确认 patched→30、成片时长、本地文件。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import proxy_store  # noqa: E402
from browser30 import generate_video_30  # noqa: E402

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "acc24"
PROMPT = sys.argv[2] if len(sys.argv) > 2 else "一只橘猫在雪地里奔跑，电影感镜头，16:9"


async def main():
    state = Path("accounts") / ACCOUNT / "cookie_state.json"
    if not state.exists():
        print(f"[FAIL] 没有 cookie 状态文件: {state}")
        return 2
    proxy = proxy_store.proxy_url_for(ACCOUNT)
    print(f"account={ACCOUNT} proxy={proxy[:38]}... model=seedance-2.5 duration=30", flush=True)

    def beat(now, *_a):
        print(f"   [poll] {time.strftime('%H:%M:%S')}", flush=True)

    t0 = time.time()
    try:
        result = await generate_video_30(
            ACCOUNT, PROMPT, model="seedance-2.5",
            cookie_state_path=str(state), proxy_url=proxy,
            on_poll=beat, max_wait=config.BROWSER_30S_MAX_WAIT)
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {str(exc)[:400]}", flush=True)
        return 1
    print(f"[OK] 耗时 {time.time() - t0:.0f}s", flush=True)
    print(json.dumps({k: str(v)[:200] for k, v in result.items()},
                     ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
