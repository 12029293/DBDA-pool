# -*- coding: utf-8 -*-
"""核对「创作台提交的短视频生成任务」是否进了任务中心的数据源。

现象：创作台/账号管理提交的短视频生成任务不出现在任务中心。
本脚本只读，不改任何数据：
  1) 读 tasks.txt + tasks.meta.jsonl（引擎队列的磁盘投影）
  2) 读 out/queue_state.json（引擎内存快照）
  3) 调 /api/admin/tasks（任务中心的真实数据源）
  4) 调 /api/admin/studio/feed（创作台自己的时间线）
  5) 对比上面四者的 tid/条数，指出断点在哪一环。
"""
import json
import os
import time
import urllib.request
from pathlib import Path

RUN = Path(__file__).resolve().parent
BASE = os.environ.get("PANEL_BASE", "http://127.0.0.1:8012")


def _get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"{exc!r}"}


def read_tasks_file():
    p = RUN / "tasks.txt"
    out = []
    if p.is_file():
        for i, raw in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines()):
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            prio = s.startswith("@@PRIO@@")
            if prio:
                s = s[len("@@PRIO@@"):].strip()
            out.append({"idx": i, "prompt": s, "prio": prio})
    return out


def read_meta_file():
    p = RUN / "tasks.meta.jsonl"
    out = []
    if p.is_file():
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = raw.strip()
            try:
                out.append(json.loads(s) if s else {})
            except (TypeError, ValueError):
                out.append({})
    return out


def main():
    tf = read_tasks_file()
    mf = read_meta_file()
    print("== 磁盘投影 ==")
    print(f"  tasks.txt        {len(tf)} 条")
    print(f"  tasks.meta.jsonl {len(mf)} 条")
    if len(tf) != len(mf):
        print(f"  !! 条数不一致（{len(tf)} vs {len(mf)}）—— 引擎会自愈，但任务可能丢参数")
    for t in tf:
        print(f"    t{t['idx'] + 1:03d} prio={int(t['prio'])} {t['prompt'][:48]!r}")

    snap_path = RUN / "out" / "queue_state.json"
    snap = {}
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
        age = time.time() - snap_path.stat().st_mtime
    except Exception as exc:  # noqa: BLE001
        age, snap = None, {}
        print(f"  (queue_state.json 读取失败: {exc!r})")
    print("\n== 引擎内存快照 ==")
    if snap:
        print(f"  generated_at={snap.get('generated_at')!r} age={age:.1f}s count={snap.get('count')}")
        print(f"  counts={snap.get('counts')}")
        for r in snap.get("rows") or []:
            print(f"    {r.get('tid')} status={r.get('status')} account={r.get('account')} "
                  f"asset_count={r.get('asset_count')} prompt={(r.get('prompt') or '')[:40]!r}")
    else:
        print("  （无快照）")

    tasks_api = _get("/api/admin/tasks?limit=200")
    print("\n== 任务中心 /api/admin/tasks ==")
    if "__error__" in tasks_api:
        print(f"  请求失败: {tasks_api['__error__']}")
    else:
        rows = tasks_api.get("tasks") or []
        print(f"  engine_rows={tasks_api.get('engine_rows')} 返回 {len(rows)} 行")
        for r in rows:
            print(f"    {r.get('id'):<28} {r.get('status'):<10} src={r.get('source') or 'db'} "
                  f"created={r.get('created_at')}")

    feed = _get("/api/admin/studio/feed?limit=200")
    print("\n== 创作台 /api/admin/studio/feed ==")
    if "__error__" in feed:
        print(f"  请求失败: {feed['__error__']}")
    else:
        items = feed.get("items") or []
        print(f"  total={feed.get('total')} engine={feed.get('engine')}")
        for it in items[:30]:
            print(f"    {it.get('id'):<10} {str(it.get('status')):<10} account={it.get('account')}")

    # ---- 交叉核对 ----
    print("\n== 交叉核对 ==")
    snap_tids = {r.get("tid") for r in (snap.get("rows") or [])}
    api_ids = {r.get("id") for r in (tasks_api.get("tasks") or [])}
    feed_ids = {it.get("id") for it in (feed.get("items") or [])}
    if snap_tids:
        miss_api = {t for t in snap_tids if ("eng_" + str(t)) not in api_ids}
        print(f"  快照里的 tid 未出现在任务中心: {sorted(miss_api) or '无'}")
        miss_feed = {t for t in snap_tids if str(t) not in feed_ids}
        print(f"  快照里的 tid 未出现在创作台:   {sorted(miss_feed) or '无'}")
    else:
        print("  快照为空，无法交叉核对")


if __name__ == "__main__":
    main()
