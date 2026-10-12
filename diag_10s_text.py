"""诊断 10s 任务被误判 duration_split：只读拉取账号最近会话，dump 机器人原文 + 规则打标。

用法: python diag_10s_text.py acc12
不提交任何生成请求，只调 /im/chain/recent_conv + /im/chain/single。
"""
import sys
import json

import config
from proxy_store import requests_proxy_url
from protocol import dola_pure_api as dola


def main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc12"
    proxy = requests_proxy_url(account)
    print(f"[diag] account={account} proxy={proxy[:60]}...")
    session, ctx, headers = dola.setup_session(
        region=config.PURE_API_REGION,
        pc_version=config.PURE_API_PC_VERSION,
        proxy=proxy or "",
        login_pure=True,
        login_state_file=rf"C:\Users\Administrator\Desktop\dola-pool\accounts\{account}\cookie_state.json",
    )
    recent = dola.fetch_recent_conversations(session, ctx)
    conv_ids = []
    for item in dola.walk(recent):
        if isinstance(item, dict):
            cid = item.get("conversation_id")
            if isinstance(cid, str) and cid.isdigit() and cid not in conv_ids:
                conv_ids.append(cid)
    print(f"[diag] recent conversations: {conv_ids[:5]}")
    if not conv_ids:
        print(json.dumps(recent, ensure_ascii=False)[:2000])
        return
    data = dola.fetch_single_chain(session, ctx, conv_ids[0])
    summary = dola.collect_reply_signals(data, include_text_blocks=True)
    print(f"[diag] conversation {conv_ids[0]}")
    print(f"[diag] creation_statuses: {summary['creation_statuses']}")
    print(f"[diag] errors: {summary['errors']}")
    print(f"[diag] briefs: {summary['briefs']}")
    for i, t in enumerate(summary["texts"]):
        tag = dola.tag_chain_text(t)
        confirm = dola.looks_like_duration_confirm(t)
        shorter = dola.looks_like_duration_declared_shorter(t)
        accepted = dola.looks_like_generation_accepted(t)
        print(f"--- text[{i}] tag={tag} confirm={confirm} shorter={shorter} accepted={accepted}")
        print(f"    {t[:400]}")


if __name__ == "__main__":
    main()
