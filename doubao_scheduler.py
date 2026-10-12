# -*- coding: utf-8 -*-
"""豆包域出片 · 生产调度适配层（dola-pool 接入用）
================================================================

定位：把 `doubao_cn_video.DoubaoVideoPool` 包装成与 dola-pool `BrowserPool`
      **同形**的调度接口，让上层（server.py / 任务队列）无需改动调用方式即可
      把豆包域当作第二条出片通道。

为什么不直接改 browser_pool.py：
      BrowserPool 管理的是 **浏览器 profile 号池**（login 源），豆包域是
      **Cookie/HTTP 号池**（doubao_ck/ 目录）。两者账号载体、额度模型、
      风控形态都不同，混在一个类里会污染现网 178 个 dola 号的调度逻辑。
      故这里做**平行通道**，由上层按域分流。

对外接口（与 browser_pool.BrowserPool.generate_video 对齐）：
    sched = DoubaoVideoScheduler.from_default_dirs()
    sched.available                     # 是否还有可调度账号
    sched.account_status()              # 健康检查用
    result = await sched.generate_video("一只猫在跑", ratio="9:16", duration=5)
    # result = {"video_url","local_path","conversation_id","account"}

额度/冷却模型（豆包域实测特征）：
    - **非会员也有每日免费额度**（实测 2026-10-08：新号 free 状态端到端出片成功，
      SSE 原文「本次生成将消耗每日免费额度」）。
    - 单号每日默认按 `daily_limit` 计数（默认 1，保守）；用满 → 冷却到次日。
    - `DoubaoLoginError`（含 710022002/710022004 风控、710012000 登录失效）
      → 该号冷却 `cooldown_seconds`（默认 1800s）。
    - `DoubaoCreditError`（额度不足）→ 当日耗尽。

状态落盘：`doubao_state.json`（默认与本文件同目录），结构
    { "<sessionid前16位>": {"day":"2026-10-08","used":1,
                            "cooldown_until":0.0,"last_err":"..."} }
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp

from doubao_cn_video import (
    DoubaoVideoClient,
    DoubaoVideoPool,
    DoubaoLoginError,
    DoubaoCreditError,
)

try:  # dola-pool 环境内可直接用其配置
    import config as _cfg  # type: ignore
except Exception:  # pragma: no cover - 独立运行时的兜底
    _cfg = None


def _cfg_get(name: str, default):
    return getattr(_cfg, name, default) if _cfg else default


def _local_day() -> str:
    return time.strftime("%Y-%m-%d")


def _key_of(ck_text: str) -> str:
    """账号稳定标识 = sessionid 前 16 位（CK 轮换但 sessionid 不变）"""
    cli = DoubaoVideoClient(ck_text)
    return (cli.sessionid or "unknown")[:16]


class DoubaoVideoScheduler:
    """豆包域出片调度器（平行于 BrowserPool）"""

    def __init__(self, cks: list[str], proxy: str = None,
                 state_path: str | None = None,
                 daily_limit: int = 1,
                 cooldown_seconds: int = 1800,
                 video_timeout: int = 300,
                 download_dir: str | None = None,
                 download_direct: bool | None = None,
                 max_attempts: int = 3):
        self.proxy = proxy if proxy is not None else _cfg_get("PROXY", "http://127.0.0.1:7890")
        self.state_path = Path(state_path or Path(__file__).with_name("doubao_state.json"))
        self.daily_limit = int(daily_limit)
        self.cooldown_seconds = int(cooldown_seconds)
        self.video_timeout = int(video_timeout)
        self.download_dir = Path(download_dir or _cfg_get("DOWNLOAD_DIR", "downloads"))
        self.download_direct = (
            _cfg_get("DOWNLOAD_DIRECT", True) if download_direct is None else download_direct)
        self.max_attempts = int(max_attempts)

        self._cks = [c for c in cks if c and "sessionid=" in c]
        self._by_key: dict[str, str] = {_key_of(c): c for c in self._cks}
        self._state: dict = self._load_state()

    # ---------- 构造 ----------

    @classmethod
    def from_default_dirs(cls, extra_dirs: list[str] | None = None, **kw) -> "DoubaoVideoScheduler":
        """自动发现豆包 CK（纯算注册机 doubao_ck/ + dola-pool/doubao_ck/）"""
        cands = list(extra_dirs or [])
        env = os.environ.get("DOLA_DOUBAO_CK_DIR")
        if env:
            cands.append(env)
        cands += [
            r"C:\Users\Administrator\Desktop\纯算注册\doubao_ck",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "doubao_ck"),
        ]
        seen, cks = set(), []
        for d in cands:
            if not d or not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if not name.endswith(".txt"):
                    continue
                try:
                    with open(os.path.join(d, name), encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and "sessionid=" in line and line not in seen:
                                seen.add(line)
                                cks.append(line)
                except Exception:
                    continue
        return cls(cks, **kw)

    # ---------- 状态 ----------

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.state_path)   # 原子写
        except Exception:
            pass

    def _st(self, key: str) -> dict:
        s = self._state.setdefault(key, {"day": _local_day(), "used": 0,
                                         "cooldown_until": 0.0, "last_err": ""})
        if s.get("day") != _local_day():       # 跨天自动清零
            s.update(day=_local_day(), used=0, cooldown_until=0.0, last_err="")
        return s

    def _usable(self, key: str) -> bool:
        s = self._st(key)
        return not s["cooldown_until"] > time.time() and s["used"] < self.daily_limit

    # ---------- 查询 ----------

    @property
    def accounts(self) -> list[str]:
        return list(self._by_key.keys())

    @property
    def available(self) -> bool:
        return any(self._usable(k) for k in self._by_key) and bool(self._by_key)

    def account_status(self) -> list[dict]:
        out = []
        now = time.time()
        for k in self._by_key:
            s = self._st(k)
            cd = max(0.0, s["cooldown_until"] - now)
            out.append({
                "account": k,
                "group": ("cooling" if cd > 0 else
                          "day_exhausted" if s["used"] >= self.daily_limit else "idle"),
                "used_today": s["used"],
                "daily_limit": self.daily_limit,
                "cooldown_seconds": int(cd),
                "last_err": s["last_err"][:120],
            })
        return out

    # ---------- 出片 ----------

    async def _download(self, url: str, account: str) -> Path:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        fname = self.download_dir / f"doubao_{account}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        proxy = None if self.download_direct else self.proxy
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, proxy=proxy) as r:
                r.raise_for_status()
                with open(fname, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 16):
                        f.write(chunk)
        return fname

    async def generate_video(self, prompt: str, ratio: str = "9:16",
                             duration: int = 5, model: str = "seedance_v2.0",
                             *, download: bool = True,
                             timeout: int | None = None,
                             on_account_try=None) -> dict:
        """挑一个可调度账号出片；失败自动换号，额度/风控自动冷却记账。

        返回与 browser_pool 同形：
            {"video_url","local_path","conversation_id","account"}
        """
        if not self._by_key:
            raise RuntimeError("豆包域号池为空（doubao_ck/ 下没有可用 CK）")
        timeout = timeout or self.video_timeout

        order = [k for k in self._by_key if self._usable(k)]
        if not order:
            raise DoubaoCreditError("豆包域所有账号均已冷却或当日额度用尽")

        last_err = None
        tried = 0
        for key in order:
            if tried >= self.max_attempts:
                break
            tried += 1
            if on_account_try:
                try:
                    on_account_try(key, tried)
                except Exception:
                    pass
            cli = DoubaoVideoClient(self._by_key[key], proxy=self.proxy)
            try:
                url = await cli.generate_video(prompt, ratio=ratio, duration=duration,
                                               model=model, timeout=timeout)
            except DoubaoCreditError as e:
                self._st(key).update(used=self.daily_limit, last_err=str(e)[:200])
                self._save_state()
                last_err = e
                continue
            except DoubaoLoginError as e:
                self._st(key).update(cooldown_until=time.time() + self.cooldown_seconds,
                                     last_err=str(e)[:200])
                self._save_state()
                last_err = e
                continue
            except Exception as e:
                self._st(key).update(last_err=str(e)[:200])
                self._save_state()
                last_err = e
                continue

            # 成功：记账
            self._st(key).update(used=self._st(key)["used"] + 1, last_err="")
            self._save_state()
            out = {"video_url": url, "conversation_id": cli._local_conv_id,
                   "account": key, "local_path": None}
            if download:
                try:
                    out["local_path"] = str(await self._download(url, key))
                except Exception as e:      # 下载失败不否定出片成功
                    out["download_error"] = repr(e)[:160]
            return out

        raise RuntimeError(f"豆包域全部账号尝试失败: {last_err}")


__all__ = ["DoubaoVideoScheduler"]
