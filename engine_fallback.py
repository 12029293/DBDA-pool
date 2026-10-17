# -*- coding: utf-8 -*-
"""engine_fallback.py —— dola 引擎未出片时的豆包降级器。

背景 / 为什么需要它
--------------------------------------------------------------------------
出片主链路是 dola 引擎（run_pool.py）：探针嗅 710 窗口 → 整池爆发 → 收割。
但这条链路有两个「不是 bug 但会空手而归」的现实：

  1. 710 窗口由上游**随机**开启（实测 ≈15~23 窗/小时、每窗约 12 秒）。
     任务挂着等窗口是常态，等不到就可能整批 pending。
  2. 号池额度用尽 / 出口被打热 / 账号批量风控时，窗口来了也提交不上去。

用户的要求是「**全部都不行再降级使用豆包**」。所以本模块只做一件事：

    引擎跑完（退出）→ 读 summary_pool.json → 挑出**没出片**的任务
        → 串行交给豆包通道（doubao_pool.generate_video）
        → 每条都落任务库，于是「任务列表」和「查看视频」里自然能看到

为什么串行而不是并发
--------------------------------------------------------------------------
豆包池的现实约束（见 doubao_pool.py）：
  - 单号每日配额 `DOUBAO_DAILY_LIMIT`（默认 1 条），用满进【冷却】等跨日重置；
  - 池里通常只有个位数账号；
  - 新号还有预热期。
所以并发只会让它们互相抢冷却窗口、成批失败。串行 + 换号重试最稳，
且降级本来就是「兜底」路径，不追求吞吐。

为什么不在引擎里做
--------------------------------------------------------------------------
引擎（run_pool.py）是纯 dola 协议的独立进程，不认识豆包库、也不该认识 ——
把豆包塞进引擎会让那条已验证的链路多一堆耦合。降级放在面板进程里，
用现成的 doubao_pool / store，互不干扰，还能单独开关。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

# 只有这些「终态且没出片」的状态才值得降级。
# pending 说明引擎是被手动停掉的（用户主动中断），不该偷偷再生产 —— 尊重用户意图。
FALLBACK_STATUSES = {"failed"}

# 单次降级最多处理多少条：兜底路径不该失控刷爆豆包配额。
DEFAULT_MAX_ITEMS = 20


def unfinished_prompts(summary: dict | None,
                       statuses: Iterable[str] = FALLBACK_STATUSES,
                       limit: int = DEFAULT_MAX_ITEMS) -> list[dict]:
    """从引擎 summary_pool.json 里挑出「终态但没出片」的任务。

    summary.tasks_detail 形如：
        [{"tid":"t001","status":"failed","account":"","conv":"","mp4":"","prompt":"..."}]

    只认 failed：pending 是被用户停掉的，done/done_watermarked 是已出片。
    mp4 非空的即使 status 不是 done 也跳过（磁盘有成片就是事实）。
    """
    if not isinstance(summary, dict):
        return []
    want = set(statuses)
    out: list[dict] = []
    for t in (summary.get("tasks_detail") or []):
        if not isinstance(t, dict):
            continue
        st = str(t.get("status") or "")
        if st not in want:
            continue
        if str(t.get("mp4") or "").strip():
            continue
        prompt = str(t.get("prompt") or "").strip()
        if not prompt:
            continue
        out.append({"tid": t.get("tid") or "", "prompt": prompt,
                    "attempts": t.get("attempts") or 0})
        if len(out) >= max(1, limit):
            break
    return out


class DoubaoFallback:
    """豆包降级器：把 dola 没出片的提示词串行交给豆包通道。

    依赖全部由调用方注入（避免与 server.py 循环 import）：
      - doubao       ：doubao_pool 的池对象（有 generate_video / accounts）
      - create_task  ：async (task_id, *, model, prompt, ratio, duration, channel) -> None
                       必须用**关键字**传：store.create 的第 6 个位置参数是 account，
                       按位置传会把 "doubao" 写进 account 字段（历史踩过的坑）。
      - update_task  ：async (task_id, **fields) -> None
      - log          ：可选，(msg) -> None，用于打印进度
    """

    def __init__(self, doubao, create_task: Callable, update_task: Callable,
                 log: Optional[Callable[[str], None]] = None):
        self.doubao = doubao
        self._create = create_task
        self._update = update_task
        self._log = log or (lambda m: None)
        self._running = False
        self._state: dict = {"running": False, "total": 0, "done": 0,
                             "ok": 0, "failed": 0, "current": "",
                             "started_at": 0.0, "finished_at": 0.0,
                             "last_error": "", "items": []}

    # ------------------------------------------------------------------ 状态
    @property
    def running(self) -> bool:
        return self._running

    def state(self) -> dict:
        return dict(self._state)

    # ------------------------------------------------------------------ 主流程
    def run(self, summary: dict | None, *, model: str = "seedance-2.0",
            ratio: str = "16:9", duration: int = 5,
            max_items: int = DEFAULT_MAX_ITEMS) -> dict:
        """同步入口（调用方自行放到线程里跑）。

        返回本次降级的汇总；没有任何待降级任务时返回 {"skipped": True}。
        """
        if self._running:
            return {"skipped": True, "reason": "降级任务进行中"}
        items = unfinished_prompts(summary, limit=max_items)
        if not items:
            return {"skipped": True, "reason": "没有需要降级的任务"}
        if not getattr(self.doubao, "accounts", None):
            self._log("[fallback] 豆包号池为空，跳过降级")
            return {"skipped": True, "reason": "豆包号池为空"}

        self._running = True
        self._state.update({
            "running": True, "total": len(items), "done": 0, "ok": 0,
            "failed": 0, "current": "", "started_at": time.time(),
            "finished_at": 0.0, "last_error": "",
            "items": [{"tid": it["tid"], "prompt": it["prompt"][:80],
                       "status": "pending", "error": ""} for it in items],
        })
        self._log(f"[fallback] 开始豆包降级：{len(items)} 条（串行，避免互抢冷却）")
        try:
            for idx, it in enumerate(items):
                self._state["current"] = it["tid"]
                self._state["items"][idx]["status"] = "running"
                try:
                    res = self._one(it, model=model, ratio=ratio, duration=duration)
                except Exception as exc:  # noqa: BLE001
                    res = {"ok": False, "error": repr(exc)[:200]}
                if res.get("ok"):
                    self._state["ok"] += 1
                    self._state["items"][idx]["status"] = "done"
                    self._log(f"[fallback] {it['tid']} 豆包出片成功 → {res.get('local_path','')}")
                else:
                    self._state["failed"] += 1
                    self._state["items"][idx]["status"] = "failed"
                    self._state["items"][idx]["error"] = str(res.get("error") or "")[:160]
                    self._state["last_error"] = str(res.get("error") or "")[:200]
                    self._log(f"[fallback] {it['tid']} 豆包出片失败：{self._state['last_error']}")
                self._state["done"] += 1
        finally:
            self._running = False
            self._state["running"] = False
            self._state["current"] = ""
            self._state["finished_at"] = time.time()
            self._log(f"[fallback] 降级结束：成功 {self._state['ok']} / 失败 {self._state['failed']}"
                      f"（共 {self._state['total']}）")
        return dict(self._state)

    def _one(self, item: dict, *, model: str, ratio: str, duration: int) -> dict:
        """单条降级：建任务行 → 跑豆包出片 → 回写结果。"""
        tid = item["tid"] or ("fb_" + str(int(time.time() * 1000)))
        task_id = "fb_" + tid + "_" + str(int(time.time()))[-6:]
        prompt = item["prompt"]

        # 建任务行（channel=doubao）→ 自动出现在「任务列表」和「查看视频」
        try:
            asyncio.run(self._create(task_id, model=model, prompt=prompt,
                                     ratio=ratio, duration=duration, channel="doubao"))
        except Exception as exc:  # noqa: BLE001
            # 建行失败不致命：仍去出片，只是任务库里看不到
            self._log(f"[fallback] 建任务行失败（继续出片）: {exc}")
        try:
            asyncio.run(self._update(task_id, status="processing", started_at=time.time()))
        except Exception:  # noqa: BLE001
            pass

        try:
            result = asyncio.run(self.doubao.generate_video(
                prompt, ratio=ratio, duration=duration, model=model))
        except Exception as exc:  # noqa: BLE001
            err = str(exc)[:300]
            self._safe_update(task_id, status="failed", error=err, finished_at=time.time())
            return {"ok": False, "error": err}

        if not result or not result.get("local_path"):
            err = "豆包未产出本地视频文件"
            self._safe_update(task_id, status="failed", error=err, finished_at=time.time())
            return {"ok": False, "error": err}

        local = str(result["local_path"])
        if not Path(local).exists():
            err = "豆包产出文件不存在"
            self._safe_update(task_id, status="failed", error=err, finished_at=time.time())
            return {"ok": False, "error": err}

        self._safe_update(task_id, status="completed",
                          video_url=result.get("video_url"),
                          account=result.get("account") or "",
                          finished_at=time.time())
        return {"ok": True, "local_path": local,
                "video_url": result.get("video_url"),
                "account": result.get("account") or ""}

    def _safe_update(self, task_id: str, **fields) -> None:
        try:
            asyncio.run(self._update(task_id, **fields))
        except Exception:  # noqa: BLE001
            pass
