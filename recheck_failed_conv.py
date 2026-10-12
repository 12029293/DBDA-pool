"""只读复查：30s 失败任务的会话里上游到底出没出片。

用法: python recheck_failed_conv.py acc18 acc24 ...
不提交任何新请求，只拉会话列表 + 单次 inspect_video_once。
"""
import sys
import time

import dola_client  # noqa: F401  (确保 sys.path)
from protocol import dola_pure_api as dola

PROMPT_KEY = "汉服美女"


def main(accounts: list[str]) -> None:
    for name in accounts:
        state_file = f"accounts/{name}/cookie_state.json"
        print(f"===== {name} =====", flush=True)
        try:
            session, ctx, headers = dola.setup_session(
                region="JP", login_pure=True, login_state_file=state_file)
        except Exception as exc:
            print(f"  session 失败: {str(exc)[:150]}", flush=True)
            continue
        try:
            convs = dola.fetch_recent_conversations(session, ctx, limit=10,
                                                    message_count_per_conv=4)
        except Exception as exc:
            print(f"  拉会话列表失败: {str(exc)[:150]}", flush=True)
            continue
        items = convs.get("conversations") or convs.get("items") or []
        target_id = ""
        for c in items:
            cid = c.get("conversation_id") or c.get("id") or ""
            title = str(c.get("title") or c.get("name") or "")
            texts = " ".join(str(m.get("text") or m.get("content") or "")
                             for m in (c.get("messages") or []))
            blob = title + " " + texts
            if PROMPT_KEY in blob or "赛博朋克" in blob:
                target_id = cid
                print(f"  命中会话: {cid} title={title[:40]!r}", flush=True)
                break
            print(f"  - {cid} {title[:36]!r}", flush=True)
        if not target_id:
            print("  未找到目标会话", flush=True)
            continue
        try:
            pr = dola.inspect_video_once(session, ctx, target_id)
        except Exception as exc:
            print(f"  inspect 失败: {str(exc)[:200]}", flush=True)
            continue
        print(f"  status={pr.status}", flush=True)
        print(f"  urls={[u[:90] for u in pr.urls]}", flush=True)
        print(f"  vids={pr.vids}", flush=True)
        print(f"  creation_statuses={pr.creation_statuses}", flush=True)
        print(f"  failure_reasons={pr.failure_reasons}", flush=True)
        for t in (pr.texts or [])[-4:]:
            print(f"  text| {str(t)[:120]}", flush=True)


if __name__ == "__main__":
    args = sys.argv[1:] or ["acc18", "acc24"]
    t0 = time.time()
    main(args)
    print(f"done in {time.time()-t0:.0f}s", flush=True)
