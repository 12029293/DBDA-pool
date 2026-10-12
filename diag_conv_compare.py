"""对照成功/失败会话的链路原文：拉取指定账号的指定会话（或最新几个会话），
dump 机器人文本 / briefs / errors / video_info / 关键 ext 字段。只读。

用法:
  python diag_conv_compare.py acc10 38418051707691281 [38418051765807633 ...]
  python diag_conv_compare.py acc9 --recent 5
"""
import sys
import json

import config
from proxy_store import requests_proxy_url
from protocol import dola_pure_api as dola

RUN_DIR = r"C:\Users\Administrator\Desktop\dola-pool"


def fetch_chain(account, conv_id):
    import time as _t
    from proxy_store import rotate_session, get_account_proxy
    last_exc = None
    for attempt in range(3):
        try:
            proxy = requests_proxy_url(account, rotate=bool(attempt))
            session, ctx, headers = dola.setup_session(
                region=config.PURE_API_REGION,
                pc_version=config.PURE_API_PC_VERSION,
                proxy=proxy or "",
                login_pure=True,
                login_state_file=rf"{RUN_DIR}\accounts\{account}\cookie_state.json",
            )
            if conv_id == "--recent":
                n = int(sys.argv[3]) if len(sys.argv) > 3 else 5
                recent = dola.fetch_recent_conversations(session, ctx)
                conv_ids = []
                for item in dola.walk(recent):
                    if isinstance(item, dict):
                        cid = item.get("conversation_id")
                        if isinstance(cid, str) and cid.isdigit() and cid not in conv_ids:
                            conv_ids.append(cid)
                print(f"[{account}] recent convs: {conv_ids[:n]}")
                return [(cid, dola.fetch_single_chain(session, ctx, cid)) for cid in conv_ids[:n]]
            return [(conv_id, dola.fetch_single_chain(session, ctx, conv_id))]
        except Exception as exc:
            last_exc = exc
            print(f"[{account}] attempt {attempt+1} failed: {str(exc)[:120]}, rotate proxy & retry")
            try:
                info = get_account_proxy(account)
                if info:
                    rotate_session(account, info["id"])
            except Exception:
                pass
            _t.sleep(2)
    raise last_exc


def dump_chain(account, conv_id, data):
    summary = dola.collect_reply_signals(data, include_text_blocks=True)
    print(f"\n===== [{account}] conv={conv_id} =====")
    print(f"  creation_statuses: {summary['creation_statuses']}")
    print(f"  errors: {summary['errors']}")
    for b in summary["briefs"]:
        print(f"  brief: {str(b)[:300]}")
    for i, t in enumerate(summary["texts"]):
        print(f"  text[{i}] tag={dola.tag_chain_text(t)}: {str(t)[:260]}")
    # video_info / creation 参数等 ext 线索
    seen_ext = set()
    for item in dola.walk(data):
        if not isinstance(item, dict):
            continue
        ext = item.get("ext")
        if isinstance(ext, dict):
            keys = {k: ext.get(k) for k in (
                "has_video_gen", "use_creation", "auto_create_creation",
                "video_gen_duration", "duration", "creation_duration",
                "model", "ability_param", "chat_ability",
            ) if ext.get(k) not in (None, "", "0")}
            if keys:
                ks = json.dumps(keys, ensure_ascii=False)
                if ks not in seen_ext:
                    seen_ext.add(ks)
                    print(f"  ext: {ks[:300]}")
        if "video_info" in item and isinstance(item.get("video_info"), list):
            for vi in item["video_info"]:
                if isinstance(vi, dict):
                    print(f"  video_info: duration={vi.get('video_duration')} vid={vi.get('video_id')}")


def main():
    account = sys.argv[1]
    targets = sys.argv[2:]
    if targets and targets[0] == "--recent":
        chains = fetch_chain(account, "--recent")
    else:
        chains = []
        for conv_id in targets:
            chains.extend(fetch_chain(account, conv_id))
    for conv_id, data in chains:
        dump_chain(account, conv_id, data)


if __name__ == "__main__":
    main()
