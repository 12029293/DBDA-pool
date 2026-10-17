# -*- coding: utf-8 -*-
"""核对「发起出片」的等待链路：队列快照、tasks.txt、看门狗、run_seconds。

只读诊断，不改数据。重点回答：
  1) 快照里 pending 行的 created_at 从哪来（任务中心靠它排「已等 X 分」）
  2) 引擎是否真的常驻（run_seconds=0）等窗口
  3) 任务中心的 engine_rows 合并是否把刚提交的行带出来
"""
import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

RUN = Path(__file__).resolve().parent
BASE = os.environ.get("PANEL_BASE", "http://127.0.0.1:8012")


def _get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"{exc!r}"}


def main():
    print("== 队列快照（out/queue_state.json）==")
    sp = RUN / "out" / "queue_state.json"
    if sp.is_file():
        d = json.loads(sp.read_text(encoding="utf-8"))
        age = time.time() - sp.stat().st_mtime
        print(f"  generated_at={d.get('generated_at')!r}  age={age:.1f}s  count={d.get('count')}")
        print(f"  counts={d.get('counts')}")
        rows = d.get("rows") or []
        print(f"  行数={len(rows)}")
        for r in rows[:20]:
            print(f"    {r.get('tid')} {str(r.get('status')):<10} prio={int(bool(r.get('prio')))} "
                  f"account={r.get('account')} prompt={(r.get('prompt') or '')[:36]!r}")
        pend = [r for r in rows if str(r.get("status")) in ("pending", "flying")]
        print(f"  → 待处理/在飞: {len(pend)} 条")
        if not pend:
            print("  → 队列里没有排队中的任务（要么已被受理/收割，要么已失败）")
    else:
        print("  无快照（引擎没跑 / 从未写过）")

    print("\n== tasks.txt 磁盘投影 ==")
    tp = RUN / "tasks.txt"
    n = 0
    if tp.is_file():
        for raw in tp.read_text(encoding="utf-8", errors="replace").splitlines():
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            n += 1
            print(f"    {n:>3} prio={int(s.startswith('@@PRIO@@'))} {s[:60]!r}")
    print(f"  共 {n} 条")

    print("\n== 引擎参数（run_seconds 是关键）==")
    st = _get("/api/admin/pool-engine/status?log_lines=1")
    if "__error__" in st:
        print(f"  请求失败: {st['__error__']}")
    else:
        p = st.get("params") or {}
        print(f"  running={st.get('running')} alive_beat={st.get('alive_beat')} "
              f"elapsed={st.get('elapsed')} pid={st.get('pid')}")
        print(f"  run_seconds={p.get('run_seconds')!r}  ← 0=常驻等窗口；非 0 = 到点自杀")
        print(f"  pool_target={p.get('pool_target')} max_per_acc={p.get('max_per_acc')}")
        print(f"  tasks={st.get('tasks')}  last_window={st.get('last_window')!r}")

    print("\n== 任务中心 /api/admin/tasks ==")
    t = _get("/api/admin/tasks?limit=200")
    if "__error__" in t:
        print(f"  请求失败: {t['__error__']}")
    else:
        rows = t.get("tasks") or []
        print(f"  engine_rows={t.get('engine_rows')} 返回 {len(rows)} 行")
        eng = [r for r in rows if r.get("source") == "engine-queue"]
        db = [r for r in rows if r.get("source") != "engine-queue"]
        print(f"  引擎队列行 {len(eng)} 条 / 库内行 {len(db)} 条")
        for r in eng:
            print(f"    {r.get('id'):<12} {str(r.get('status')):<10} created={r.get('created_at')} "
                  f"account={r.get('account')}")

    print("\n== tasks.db 里的引擎成片 ==")
    dbp = RUN / "tasks.db"
    if dbp.is_file():
        c = sqlite3.connect(dbp)
        try:
            rows = c.execute(
                "SELECT id,status,created_at,conversation_id FROM tasks "
                "WHERE id LIKE 'eng_%' ORDER BY created_at DESC LIMIT 20").fetchall()
            print(f"  引擎成片记录 {len(rows)} 条")
            for r in rows:
                print(f"    {r[0]:<30} {r[1]:<10} created={r[2]} conv={r[3]}")
        finally:
            c.close()


if __name__ == "__main__":
    main()
