# -*- coding: utf-8 -*-
"""dola 渠道【全局窗口嗅探 + 整池爆发提交】引擎（goal 612 渠道改造核心件）。

背景（实测结论，见 fact 620/621，与 i615 独立引擎 dola_engine.py 同源）：
  * POST /chat/completion?aid=495671 (ability_type=17) 对每个账号是【单一全局网关】，
    非窗口期一律秒返 710022002「目前服務使用量過大，請稍後重試」；
  * 该闸门【周期性开启】—— 实测 ≈15~23 窗口/h、每次窗口约 2~12s，窗口开时整池同刻放行
    （拿到 conversation_id = SSE_ACK）；
  * 旧 DBDA-pool 的错误处置是 classify_upstream_failure→transient→「换号重试」，同一时刻
    把整池都试一遍、全失败 → 判死。正确做法：710022002 只代表【窗口关】，应把该次提交
    交回【全局窗口嗅探器】排队，等窗口命中再统一爆发。

本模块把 i615/dola_engine.py 已验证的 SOP 接进 DBDA-pool 出片链路：
  1) DolaProtocolSubmitter —— 只做「一个 cookie 号打一发提交」这一段，复用
     protocol/dola_pure_api.py，并把结果严格分类；**710022002 绝不抛异常**。
  2) WindowSniffer —— 全局哨兵线程：周期只用一个探针号打一发；710022002=窗关（继续嗅），
     拿到 conversation_id=窗开（set 事件放行全体）。
  3) BurstScheduler —— 窗口一开，对排队中的任务并发爆发提交；任一 worker 收到 710022002
     会立刻 note_closed()，避免整池无效空打。

所有窗口/提交事实写 jsonl，便于验收。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import config

log = logging.getLogger("dola.window")

# ---- 上游结果标识（与 i615/dola_engine.py 完全对齐）----
GATE_CLOSED_CODE = "710022002"      # 窗口关（全局闸门）
LOGIN_INVALID_CODE = "710012001"    # 登录态失效
COUNTRY_CODE = "710022003"          # 出口地区受限（不是窗口问题！）
QUOTA_TEXT = "今天的生成次數已經達到上限"
QUOTA_TEXT_S = "今天的生成次数已经达到上限"

OUT_RECEIVED = "RECEIVED"
OUT_WINDOW_CLOSED = "WINDOW_CLOSED"
OUT_LOGIN_INVALID = "LOGIN_INVALID"
OUT_QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
OUT_COUNTRY = "COUNTRY_RESTRICTED"
OUT_EXC = "EXC"
OUT_OTHER = "OTHER"


def _now() -> float:
    return time.time()


def _tstr(t: float | None = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t if t is not None else time.time()))


# ---------------------------------------------------------------------------
# 1) 单次窗口提交（协议层适配）
# ---------------------------------------------------------------------------
class SubmitOutcome:
    """一次窗口提交的结果（提交与等待严格分离，710022002 不是异常）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self) -> str:
        return "SubmitOutcome(%s)" % json.dumps(self.__dict__, ensure_ascii=False)[:300]

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class DolaProtocolSubmitter:
    """用 DBDA-pool 自带 protocol/dola_pure_api.py 把「一个 cookie 号」打一发提交。

    与 pure_api_gen.generate_pure_video 的区别：**只做提交这一段**，不做下游轮询/下载，
    并且把结果分类返回（不抛异常）。调用方据此决定「排队等窗口」还是「真失败换号」。
    """

    def __init__(self, *, region: str | None = None, pc_version: str | None = None,
                 proxy: str | None = None, prompt: str = "一只猫在草地上追逐蝴蝶",
                 report_path: str | os.PathLike[str] | None = None):
        self.region = region or config.PURE_API_REGION
        self.pc_version = pc_version or config.PURE_API_PC_VERSION
        # proxy=None  -> 跟随 config.PROXY（本机 Clash 7897）；proxy="" -> 强制直连。
        # 注意：protocol 层 session.trust_env=False，直连出口会 country restricted(710022003)，
        # 所以默认必须走 config.PROXY，否则提交全部空 SSE（会被误判为 OTHER）。见 fact 625/i630。
        self.proxy = (config.PROXY or "") if proxy is None else proxy
        self.prompt = prompt
        self.report_path = str(report_path) if report_path else None
        self._dola = None
        # ---- 常驻 session 缓存（fact 644 根因修复）----
        # 旧实现每次 submit 都重跑 setup_session(含 warmup + get_web_anon_id)，实测 ~35s
        # 级固定耗时 → 爆发动作比 6s 一次的探针晚 ~37s、必错过仅约 12s 的窗口。
        # 现改为：启动时 prepare() 预建常驻 (session, ctx, headers)，窗口一开只发最后一个
        # POST /chat/completion（~1s），与 i615/dola_engine.py 已验证出 9 个 MP4 的范式一致。
        self._sessions: dict[str, tuple] = {}   # key -> (session, ctx, headers, proxy)
        self._slock = threading.Lock()
        self._prepare_locks: dict[str, threading.Lock] = {}
        # ---- 参考图预上传缓存（创作台参考素材）----
        # 上游图片上传（prepare_upload → ApplyImageUpload → PUT TOS → Commit）走的是
        # **账号自己的 session**，所以每个号都要各自上传一遍；单张实测数秒。
        # 而窗口只有约 12 秒 —— 若在爆发那一刻才上传，50 号 × 每号 1~2 张会直接把
        # 窗口吃光（提交必失败）。因此把上传提前到 session 预热阶段（prepare_many），
        # 窗口里只发最后一个 POST。这与"常驻 session"是同一个思路。
        # key(绝对路径) -> {"sig": 素材签名, "images": [uploaded_image_dict, ...]}
        self._assets: dict[str, dict] = {}
        self._alock = threading.Lock()

    # -- 参考素材签名/缓存 --

    @staticmethod
    def _asset_sig(paths) -> tuple:
        """素材集合的签名：路径 + 大小 + mtime。变了就要重新上传。"""
        sig = []
        for p in list(paths or []):
            try:
                st = os.stat(str(p))
                sig.append((os.path.abspath(str(p)), int(st.st_size), int(st.st_mtime)))
            except OSError:
                sig.append((os.path.abspath(str(p)), -1, -1))
        return tuple(sig)

    def prefetch_assets(self, cookie_state_path, paths, *, proxy: str | None = None,
                        session=None, ctx=None, headers=None) -> dict:
        """把参考图预先上传到该号，并缓存返回的 image 字典列表。

        幂等：同一号 + 同一素材集合（签名一致）直接命中缓存，零成本。
        返回 {"ok": bool, "images": [...], "cached": bool, "error": str}。

        session/ctx/headers 可传可不传：传入时复用调用方的常驻 session
        （启动预上传阶段与 submit 内的惰性补传都走这条，避免同一个号被
        建两份 session）；不传时才自己 prepare 一次。
        """
        paths = [str(p) for p in (paths or []) if p]
        if not paths:
            return {"ok": True, "images": [], "cached": True, "error": ""}
        key = self._key(cookie_state_path)
        sig = self._asset_sig(paths)
        with self._alock:
            ent = self._assets.get(key)
            if ent and ent.get("sig") == sig:
                return {"ok": True, "images": list(ent.get("images") or []),
                        "cached": True, "error": ""}
        lk = self._lock_for(key)          # 与 prepare 共用锁：session 不能被并发用
        with lk:
            with self._alock:
                ent = self._assets.get(key)
                if ent and ent.get("sig") == sig:
                    return {"ok": True, "images": list(ent.get("images") or []),
                            "cached": True, "error": ""}
            try:
                if session is None:
                    session, ctx, headers = self.prepare(cookie_state_path, proxy=proxy)
                dola = self._proto()
                images = []
                for path in paths:
                    info = dola.upload_image(session, ctx, headers, path)
                    if info:
                        images.append(info)
                with self._alock:
                    self._assets[key] = {"sig": sig, "images": images}
                self._log({"kind": "asset_prefetch", "account": key,
                           "n": len(images)})
                return {"ok": True, "images": images, "cached": False, "error": ""}
            except Exception as exc:  # noqa: BLE001
                self._log({"kind": "asset_prefetch_fail", "account": key,
                           "exc": repr(exc)[:220]})
                return {"ok": False, "images": [], "cached": False,
                        "error": repr(exc)[:220]}

    def invalidate_assets(self, cookie_state_path) -> None:
        """丢弃某号的参考图缓存（上传失败/素材变更后强制重传）。"""
        key = self._key(cookie_state_path)
        with self._alock:
            self._assets.pop(key, None)

    # -- 延迟加载协议层（import 时才真正装 bdms 签名器）--
    def _proto(self):
        if self._dola is None:
            from pure_signer import install
            install()
            from protocol import dola_pure_api as module
            module.set_native_duration_max(config.NATIVE_DURATION_MAX)
            self._dola = module
        return self._dola

    # -- 常驻 session 预建/复用 --
    def _key(self, cookie_state_path) -> str:
        return os.path.abspath(str(cookie_state_path))

    def _lock_for(self, key: str) -> threading.Lock:
        with self._slock:
            lk = self._prepare_locks.get(key)
            if lk is None:
                lk = self._prepare_locks[key] = threading.Lock()
            return lk

    def prepare(self, cookie_state_path: str | os.PathLike[str], *,
                proxy: str | None = None, timeout_sec: int = 30) -> tuple:
        """预建（或复用）一个常驻 session：setup_session + warmup + get_web_anon_id。

        返回 (session, ctx, headers)。幂等：同号同代理只建一次（并发安全）。
        """
        key = self._key(cookie_state_path)
        px = (self.proxy if proxy is None else proxy) or ""
        with self._slock:
            ent = self._sessions.get(key)
            if ent is not None and ent[3] == px:
                return ent[:3]
        lk = self._lock_for(key)
        with lk:
            with self._slock:
                ent = self._sessions.get(key)
                if ent is not None and ent[3] == px:
                    return ent[:3]
            dola = self._proto()
            session, ctx, headers = dola.setup_session(
                region=self.region, pc_version=self.pc_version, proxy=px,
                login_pure=True, login_state_file=str(cookie_state_path))
            try:
                dola.warmup_login_session(session, headers)
            except Exception:  # noqa: BLE001
                pass
            try:
                dola.dola_api_post_json(session, ctx, "/alice/user/get_web_anon_id", {},
                                        label="get_web_anon_id")
            except Exception:  # noqa: BLE001
                pass
            ent = (session, ctx, headers, px)
            with self._slock:
                self._sessions[key] = ent
            return ent[:3]

    def prepare_many(self, paths, *, proxy: str | None = None, workers: int = 16,
                     assets: list | None = None) -> dict:
        """并发预建一池常驻 session（启动时调用）。返回 {state_path: ok_bool}。

        assets：参考素材路径列表。**顺手在这里把参考图预上传到每个号**——
        上传走账号自己的 session、单张数秒，而窗口只有约 12 秒；放到爆发时做
        会把窗口吃光。预热阶段做则完全不影响窗口（见 submit 的说明）。
        预上传失败**不影响** session 预建结果（出片时还会惰性重试一次）。
        """
        from concurrent.futures import ThreadPoolExecutor
        out: dict[str, bool] = {}
        lock = threading.Lock()
        want_assets = [str(a) for a in (assets or []) if a]

        def _one(p):
            try:
                self.prepare(p, proxy=proxy)
                ok = True
            except Exception as exc:  # noqa: BLE001
                ok = False
                self._log({"kind": "prepare_fail", "state": str(p), "exc": repr(exc)[:200]})
            if ok and want_assets:
                try:
                    self.prefetch_assets(p, want_assets, proxy=proxy)
                except Exception as exc:  # noqa: BLE001
                    self._log({"kind": "prepare_asset_fail", "state": str(p),
                               "exc": repr(exc)[:200]})
            with lock:
                out[str(p)] = ok

        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as tp:
            list(tp.map(_one, list(paths)))
        return out

    def warm_resident(self) -> int:
        with self._slock:
            return len(self._sessions)

    def _log(self, rec: dict) -> None:
        rec.setdefault("tstr", _tstr())
        if not self.report_path:
            return
        try:
            with open(self.report_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def invalidate(self, cookie_state_path: str | os.PathLike[str]) -> None:
        """丢弃某号的常驻 session（登录态失效/网络异常时调用），下次会重建。"""
        key = self._key(cookie_state_path)
        with self._slock:
            ent = self._sessions.pop(key, None)
        if ent is not None:
            try:
                ent[0].close()
            except Exception:  # noqa: BLE001
                pass

    def submit(self, *, cookie_state_path: str | os.PathLike[str], prompt: str | None = None,
               ratio: str | None = "9:16", duration: int = 5, model: str = "seedance-2.0",
               timeout_sec: int = 60, proxy: str | None = None,
               asset_paths: list | None = None) -> SubmitOutcome:
        """提交一发；返回分类好的 SubmitOutcome（不抛业务异常）。

        【fact 644 根因修复】优先复用常驻 session（prepare() 预建，或此处惰性建一次并缓存），
        窗口一开只发最后一个 POST /chat/completion。**成功/窗关都不关 session**，
        以便同一窗口/下一窗口继续复用（省掉 ~35s 的 setup+warmup+anon）。
        仅当登录态失效或异常时才 invalidate 重建。

        asset_paths：参考素材本地路径（创作台 @ 引用的素材）。走 prefetch_assets
        的缓存 —— 正常情况下启动预上传阶段已经传好，窗口里只做一次 dict 查询；
        缓存未命中（例如启动后才新增的号）则在此惰性上传一次。
        """
        dola = self._proto()
        prompt = prompt or self.prompt
        px = (self.proxy if proxy is None else proxy) or ""
        t0 = _now()
        out: dict[str, Any] = {
            "ts": round(t0, 3), "tstr": _tstr(t0), "duration": duration, "model": model,
            "ratio": ratio, "account": os.path.basename(os.path.dirname(str(cookie_state_path))),
        }
        resident = False
        # 参考素材：命中预上传缓存则零成本；未命中在此惰性上传一次（复用下方
        # 已建好的常驻 session，不重复建号）。
        # 顺序说明：这里在拿 session 之前调 prefetch，是为了让「窗口里才第一次
        # 遇到素材」的情况也能走同一条上传链路；prefetch 内部会自己去取 session
        # 并加同一把锁，因此不会与下面的 prepare 并发抢 session。
        images: list = []
        asset_paths = [str(p) for p in (asset_paths or []) if p]
        if asset_paths:
            out["asset_count"] = len(asset_paths)
            try:
                pf = self.prefetch_assets(cookie_state_path, asset_paths, proxy=proxy)
                images = list(pf.get("images") or [])
                out["assets_cached"] = bool(pf.get("cached"))
                if not pf.get("ok"):
                    out["asset_error"] = pf.get("error") or ""
            except Exception as exc:  # noqa: BLE001
                out["asset_error"] = repr(exc)[:200]
                images = []
        try:
            # 常驻 session：命中缓存则 0 成本；未命中则先建一次（此后一直复用）。
            key = self._key(cookie_state_path)
            with self._slock:
                ent = self._sessions.get(key)
                hit = ent is not None and ent[3] == px
            if hit:
                session, ctx, headers = ent[:3]
                resident = True
            else:
                t_prep = _now()
                session, ctx, headers = self.prepare(cookie_state_path, proxy=px)
                out["prep_sec"] = round(_now() - t_prep, 2)
                resident = False

            pure_model = (dola.SEEDANCE_25 if int(duration or 0) not in tuple(config.V20_DURATIONS)
                          else (model or "seedance-2.0"))
            # ★ 参考图：多图时给提示词补"参考图"意图前缀（与 pure_api_gen 同款），
            #   单图/无图保持原样。旧实现在这里是硬编码 images=[]，参考图会被静默丢弃。
            submit_prompt = prompt
            if images:
                try:
                    fn = getattr(dola, "enrich_multi_image_prompt", None)
                    if callable(fn) and len(images) > 1:
                        submit_prompt = fn(prompt, len(images))
                except Exception:  # noqa: BLE001
                    submit_prompt = prompt
            t_post = _now()
            res = dola.submit_chat_completion(
                session, ctx, headers, images=images, audios=[], prompt=submit_prompt,
                duration=int(duration), model=pure_model, ratio=ratio,
                timeout_sec=int(timeout_sec), video_ability=True)
            out["post_sec"] = round(_now() - t_post, 2)
            out["resident"] = resident
            out["images_sent"] = len(images)

            err = str(getattr(res, "error", "") or "")
            code = str(getattr(res, "error_code", "") or "")
            conv = str(getattr(res, "conversation_id", "") or "")
            texts = " ".join(str(x) for x in (getattr(res, "reply_texts", None) or []))
            blob = err + " " + texts
            out.update({"conv": conv, "error": err[:200], "error_code": code,
                        "reply_texts": [str(x)[:160] for x in (getattr(res, "reply_texts", None) or [])][:3],
                        "elapsed": round(_now() - t0, 2)})
            if conv:
                out["outcome"] = OUT_RECEIVED
            elif code == GATE_CLOSED_CODE or GATE_CLOSED_CODE in blob:
                out["outcome"] = OUT_WINDOW_CLOSED        # 窗关：排队等下一次窗口
            elif code == LOGIN_INVALID_CODE:
                out["outcome"] = OUT_LOGIN_INVALID
            elif code == COUNTRY_CODE or "country restricted" in blob.lower():
                out["outcome"] = OUT_COUNTRY
            elif QUOTA_TEXT in blob or QUOTA_TEXT_S in blob:
                out["outcome"] = OUT_QUOTA_EXHAUSTED
            else:
                out["outcome"] = OUT_OTHER
            # 仅在【登录态失效 / 出口受限】时丢弃常驻 session（下轮重建）。
            # 窗关(710022002)与配额耗尽都是「正常结果」，session 必须保留复用。
            if out["outcome"] == OUT_LOGIN_INVALID:
                self.invalidate(cookie_state_path)
        except Exception as exc:  # noqa: BLE001
            out.update({"outcome": OUT_EXC, "exc": repr(exc)[:200],
                        "elapsed": round(_now() - t0, 2)})
            # 网络/协议异常：常驻 session 可能已坏，丢弃重建。
            try:
                self.invalidate(cookie_state_path)
            except Exception:  # noqa: BLE001
                pass
        self._log({"kind": "submit", **out})
        return SubmitOutcome(**out)


# ---------------------------------------------------------------------------
# 2) 全局窗口嗅探器
# ---------------------------------------------------------------------------
class WindowSniffer:
    """全局哨兵线程：周期探测全局闸门，窗口一开立即通知所有等待者。

    - 只用【一个探针号】打一发（省配额），命中即 set 事件；
    - 任何 worker 自己拿到 710022002 时会 note_closed()，避免整池无效空打；
    - 探针命中（拿到 conversation_id）本身是一次合法提交，通过 on_received 回调
      交回调用方去收割（不浪费这 1 个额度）。
    """

    def __init__(self, submitter: DolaProtocolSubmitter, *, probe_state_path: str | os.PathLike[str],
                 period_sec: float | None = None, enabled: bool | None = None,
                 on_received: Callable[[SubmitOutcome], Any] | None = None,
                 report_path: str | os.PathLike[str] | None = None):
        self.submitter = submitter
        self.probe_state_path = str(probe_state_path)
        self.period_sec = float(config.WINDOW_SNIFF_PERIOD_SEC if period_sec is None else period_sec)
        self.enabled = config.WINDOW_SNIFF_ENABLED if enabled is None else bool(enabled)
        self.on_received = on_received
        self.report_path = str(report_path) if report_path else None
        self._open_evt = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.probes = 0
        self.windows = 0
        self.last: dict[str, Any] = {}
        self.window_history: list[dict[str, Any]] = []

    # -- 状态 --
    def probe_account(self) -> str:
        return os.path.basename(os.path.dirname(self.probe_state_path))

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": self.enabled,
                    "running": bool(self._thread and self._thread.is_alive()),
                    "open": self._open_evt.is_set(), "probes": self.probes, "windows": self.windows,
                    "period_sec": self.period_sec, "probe_account": self.probe_account(),
                    "last": dict(self.last), "windows_recent": self.window_history[-10:]}

    def is_open(self) -> bool:
        return self._open_evt.is_set()

    def note_closed(self) -> None:
        """任意 worker 收到 710022002 → 判定窗关，清掉「开」标记。"""
        self._open_evt.clear()

    def note_open(self) -> None:
        self._open_evt.set()

    def wait_until_open(self, timeout: float) -> bool:
        """阻塞等待窗口开启；True=已开，False=超时。"""
        if self._open_evt.is_set():
            return True
        return self._open_evt.wait(timeout=timeout)

    # -- 生命周期 --
    def start(self) -> "WindowSniffer":
        if not self.enabled:
            log.info("window sniffer disabled（DOLA_WINDOW_SNIFF=0）")
            return self
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dola-window-sniffer", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _log(self, rec: dict) -> None:
        rec.setdefault("tstr", _tstr())
        if not self.report_path:
            return
        try:
            with open(self.report_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            r0 = _now()
            try:
                out = self.submitter.submit(
                    cookie_state_path=self.probe_state_path, prompt="窗口探測",
                    ratio="9:16", duration=5, timeout_sec=30)
            except Exception as exc:  # noqa: BLE001
                out = SubmitOutcome(outcome=OUT_EXC, exc=repr(exc)[:160], elapsed=0)
            oc = out.__dict__.get("outcome")
            with self._lock:
                self.probes += 1
                self.last = {"outcome": oc, "errcode": out.__dict__.get("error_code"),
                             "conv": out.__dict__.get("conv"), "tstr": _tstr(r0)}
            if oc == OUT_RECEIVED:
                with self._lock:
                    self.windows += 1
                    self.window_history.append({"tstr": _tstr(r0), "conv": out.__dict__.get("conv")})
                self._log({"kind": "window_open", "probe_conv": out.__dict__.get("conv"),
                           "tstr": _tstr(r0)})
                print(f"[window] {_tstr()} 闸门开启（探针拿到 conversation_id={out.__dict__.get('conv')}），"
                      f"通知整池爆发", flush=True)
                self._open_evt.set()
                if self.on_received:
                    try:
                        self.on_received(out)
                    except Exception:  # noqa: BLE001
                        pass
            elif oc == OUT_WINDOW_CLOSED:
                self._open_evt.clear()
                self._log({"kind": "sniff", "gate": "CLOSED", "tstr": _tstr(r0)})
            else:
                self._log({"kind": "sniff", "gate": oc, "errcode": out.__dict__.get("error_code"),
                           "exc": out.__dict__.get("exc"), "tstr": _tstr(r0)})
            dt = _now() - r0
            if dt < self.period_sec:
                self._stop.wait(self.period_sec - dt)


# ---------------------------------------------------------------------------
# 3) 整池爆发调度
# ---------------------------------------------------------------------------
class BurstJob:
    """一次「等窗口 → 爆发提交」的任务。"""

    __slots__ = ("job_id", "account", "state_path", "prompt", "ratio", "duration", "model",
                 "deadline", "result", "done", "event", "t0")

    def __init__(self, account: str, state_path: str, prompt: str, ratio: str | None,
                 duration: int, model: str, deadline: float):
        self.job_id = uuid.uuid4().hex[:12]
        self.account = account
        self.state_path = state_path
        self.prompt = prompt
        self.ratio = ratio
        self.duration = duration
        self.model = model
        self.deadline = deadline
        self.result: SubmitOutcome | None = None
        self.done = False
        self.event = threading.Event()
        self.t0 = _now()


class BurstScheduler:
    """窗口命中即对池内「当日未耗尽配额」的号整池同刻并发提交。"""

    def __init__(self, submitter: DolaProtocolSubmitter, sniffer: WindowSniffer, *,
                 burst_concurrency: int | None = None, quota_threshold: int | None = None,
                 report_path: str | os.PathLike[str] | None = None):
        self.submitter = submitter
        self.sniffer = sniffer
        self.burst_concurrency = int(config.WINDOW_BURST_CONCURRENCY if burst_concurrency is None
                                     else burst_concurrency) or 32
        self.quota_threshold = int(config.WINDOW_QUOTA_THRESHOLD if quota_threshold is None
                                   else quota_threshold)
        self.report_path = str(report_path) if report_path else None
        self._queue: list[BurstJob] = []
        self._qlock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=self.burst_concurrency,
                                        thread_name_prefix="dola-burst")
        self.bursts = 0
        self.received = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._quota_fn: Callable[[str], int] = lambda _a: int(config.DAILY_LIMIT)
        self._proxy_fn: Callable[[str], str] = lambda _a: ""

    # -- 配额账：当日未耗尽配额的号才入池 --
    def set_quota_fn(self, fn: Callable[[str], int]) -> None:
        self._quota_fn = fn

    def set_proxy_fn(self, fn: Callable[[str], str]) -> None:
        self._proxy_fn = fn

    def quota_left(self, account: str) -> int:
        try:
            return int(self._quota_fn(account))
        except Exception:  # noqa: BLE001
            return int(config.DAILY_LIMIT)

    def eligible(self, account: str) -> bool:
        """配额阈值开关：仅当剩余额度 >= WINDOW_QUOTA_THRESHOLD 才参与爆发。"""
        return self.quota_left(account) >= self.quota_threshold

    # -- 提交排队 --
    def enqueue(self, account: str, state_path: str, prompt: str, ratio: str | None,
                duration: int, model: str, deadline: float) -> BurstJob:
        job = BurstJob(account, state_path, prompt, ratio, duration, model, deadline)
        with self._qlock:
            self._queue.append(job)
        self._wake.set()
        return job

    def _log(self, rec: dict) -> None:
        rec.setdefault("tstr", _tstr())
        if not self.report_path:
            return
        try:
            with open(self.report_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # -- 爆发一轮 --
    def burst(self, jobs: list[BurstJob]) -> None:
        """对给定任务整池同刻并发提交（窗口命中瞬间调用）。"""
        if not jobs:
            return
        self.bursts += 1
        t0 = _now()
        self._log({"kind": "burst", "n": len(jobs), "tstr": _tstr(t0),
                   "accounts": [j.account for j in jobs]})
        print(f"[burst] {_tstr(t0)} 窗口命中 → 整池爆发提交 {len(jobs)} 个号", flush=True)
        futs = [self._pool.submit(self._fire, j) for j in jobs]
        for fut, j in zip(futs, jobs):
            timeout = max(5, int(config.WINDOW_SUBMIT_TIMEOUT_SEC) + 15)
            try:
                fut.result(timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                if not j.done:
                    j.result = SubmitOutcome(outcome=OUT_EXC, exc=repr(exc)[:200],
                                             account=j.account)
                    j.done = True
                    j.event.set()

    def _fire(self, job: BurstJob) -> None:
        try:
            proxy = self._proxy_fn(job.account) or ""
        except Exception:  # noqa: BLE001
            proxy = ""
        out = self.submitter.submit(
            cookie_state_path=job.state_path, prompt=job.prompt, ratio=job.ratio,
            duration=job.duration, model=job.model,
            timeout_sec=config.WINDOW_SUBMIT_TIMEOUT_SEC, proxy=proxy)
        job.result = out
        job.done = True
        job.event.set()
        if out.__dict__.get("outcome") == OUT_RECEIVED:
            self.received += 1
        elif out.__dict__.get("outcome") == OUT_WINDOW_CLOSED:
            # 有人证明窗已关 → 立刻让哨兵与其它等待者停止空打
            self.sniffer.note_closed()

    # -- 等待窗口并按需提交（单任务路径）--
    def wait_and_submit(self, account: str, state_path: str, prompt: str, ratio: str | None,
                        duration: int, model: str, deadline: float) -> SubmitOutcome:
        """等窗口 → 提交；窗关则重新排队，直到 deadline。"""
        last: SubmitOutcome | None = None
        while _now() < deadline:
            timeout = max(1.0, min(deadline - _now(), max(1.0, self.sniffer.period_sec * 4)))
            if not self.sniffer.wait_until_open(timeout):
                continue
            job = self.enqueue(account, state_path, prompt, ratio, duration, model, deadline)
            self.burst([job])
            if not job.event.wait(timeout=max(5, int(config.WINDOW_SUBMIT_TIMEOUT_SEC) + 20)):
                continue
            last = job.result
            oc = last.__dict__.get("outcome") if last else None
            if oc == OUT_RECEIVED:
                return last
            if oc == OUT_WINDOW_CLOSED:
                continue           # 窗口已关：交回嗅探器继续排队
            return last            # 其余结果如实返回
        return last or SubmitOutcome(outcome=OUT_EXC, exc="window wait deadline exceeded",
                                     account=account)


# ---------------------------------------------------------------------------
# 4) 输出闸门（pure_api_gen 的接入点）
# ---------------------------------------------------------------------------
class WindowClosedError(Exception):
    """710022002＝窗口关。**不是瞬时限流、不是账号问题**：
    调用方不应换号重试，而应把这次提交交回窗口嗅探器排队。

    注：pure_api_gen 会再派生一个「同时继承 WindowClosedError 与 PureApiError」的类，
    好让既有 `except PureApiError` 分支也能接住它。"""


class WindowClosedBase(WindowClosedError):
    """给下游派生用的别名（语义同上）。"""


class WindowGate:
    """把「等窗口 → 同时提交」做成一扇闸门，供 generate_pure_video 直接挂。

    为什么这样就是「整池爆发」：所有 worker 协程都阻塞在【同一个】`wait_for_window()`
    上（共用一条全局哨兵线程探测的窗口事件）。窗口一命中，事件 set 会让全部等待者
    【同一瞬间】返回，各自带着自己那个号的 cookie 并发打出 POST /chat/completion——
    这正是 fact 528/616 实测的「窗口开时整池同刻放行」。

    - `wait_for_window(deadline)`：窗口关时阻塞等待，直到窗口开或超时；
    - 任意 worker 提交时收到 710022002 → 调 `note_closed()`，让哨兵与其它等待者停打；
    - 提交成功拿到 conversation_id → 调 `note_received(account, conv)` 记账。
    """

    def __init__(self, submitter: DolaProtocolSubmitter, *, probe_state_path: str | os.PathLike[str],
                 report_path: str | os.PathLike[str] | None = None,
                 on_received: Callable[[SubmitOutcome], Any] | None = None):
        self.sniffer = WindowSniffer(submitter, probe_state_path=probe_state_path,
                                     on_received=on_received, report_path=report_path)
        self.report_path = str(report_path) if report_path else None
        self.submitter = submitter
        self._lock = threading.Lock()
        self.bursts = 0
        self.received = 0
        self.window_closed_events = 0

    # -- 生命周期 --
    def start(self) -> "WindowGate":
        self.sniffer.start()
        return self

    def stop(self) -> None:
        self.sniffer.stop()

    def enabled(self) -> bool:
        return bool(getattr(self.sniffer, "enabled", False))

    def probe_account(self) -> str:
        return self.sniffer.probe_account()

    def state(self) -> dict[str, Any]:
        st = self.sniffer.state()
        st.update({"bursts": self.bursts, "received": self.received,
                   "window_closed_events": self.window_closed_events})
        return st

    def _log(self, rec: dict) -> None:
        rec.setdefault("tstr", _tstr())
        if not self.report_path:
            return
        try:
            with open(self.report_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # -- 等窗口 --
    def wait_for_window(self, deadline: float | None = None, account: str = "") -> bool:
        """阻塞直到全局窗口开启；True=已开可直接提交，False=超时（应由调用方如实返回）。"""
        if not self.enabled():
            return True                      # 引擎关：退回旧行为，直接提交
        while True:
            timeout = max(1.0, self.sniffer.period_sec * 4)
            if deadline is not None:
                left = deadline - _now()
                if left <= 0:
                    return False
                timeout = max(1.0, min(left, timeout))
            if self.sniffer.wait_until_open(timeout):
                with self._lock:
                    self.bursts += 1
                self._log({"kind": "gate_open", "account": account, "tstr": _tstr()})
                return True
            if deadline is not None and _now() >= deadline:
                return False

    def note_received(self, account: str, conversation_id: str = "") -> None:
        with self._lock:
            self.received += 1
        self._log({"kind": "received", "account": account, "conv": conversation_id, "tstr": _tstr()})

    def note_closed(self) -> None:
        with self._lock:
            self.window_closed_events += 1
        self.sniffer.note_closed()

    def classify_submit_error(self, error_code: str, message: str) -> str:
        """把提交层错误码/文案再分一类：window / quota / login / other。"""
        blob = f"{error_code} {message}".lower()
        if error_code == GATE_CLOSED_CODE or GATE_CLOSED_CODE in blob:
            return "window"
        if QUOTA_TEXT in blob or QUOTA_TEXT_S in blob:
            return "quota"
        if error_code == LOGIN_INVALID_CODE:
            return "login"
        return "other"


# ---------------------------------------------------------------------------
# 单例装配
# ---------------------------------------------------------------------------
_SNIFTER: WindowSniffer | None = None
_SCHED: BurstScheduler | None = None
_GATE: WindowGate | None = None
_LOCK = threading.Lock()


def build(submitter: DolaProtocolSubmitter, *, probe_state_path: str | os.PathLike[str],
          report_path: str | os.PathLike[str] | None = None,
          on_received: Callable[[SubmitOutcome], Any] | None = None
          ) -> tuple[WindowSniffer, BurstScheduler]:
    global _SNIFTER, _SCHED
    with _LOCK:
        _SNIFTER = WindowSniffer(submitter, probe_state_path=probe_state_path,
                                 on_received=on_received, report_path=report_path)
        _SCHED = BurstScheduler(submitter, _SNIFTER, report_path=report_path)
        return _SNIFTER, _SCHED


def build_gate(submitter: DolaProtocolSubmitter, *, probe_state_path: str | os.PathLike[str],
               report_path: str | os.PathLike[str] | None = None,
               on_received: Callable[[SubmitOutcome], Any] | None = None) -> WindowGate:
    """装配【全局唯一】的窗口闸门（供 pure_api_gen.generate_pure_video 挂载）。"""
    global _GATE, _SNIFTER, _SCHED
    with _LOCK:
        if _GATE is None:
            _GATE = WindowGate(submitter, probe_state_path=probe_state_path,
                               report_path=report_path, on_received=on_received)
            _SNIFTER = _GATE.sniffer
            _SCHED = None
    return _GATE


def _default_report_path() -> str:
    if config.WINDOW_REPORT_PATH:
        return config.WINDOW_REPORT_PATH
    return str(Path(__file__).resolve().parent / "logs" / "dola_window.jsonl")


def ensure_gate_for_account(cookie_state_path: str | os.PathLike[str],
                            *, proxy: str = "") -> WindowGate | None:
    """按「第一个 cookie 号」懒装配全局窗口闸门并启动哨兵（幂等）。

    这是 pure_api_gen 的接入点：generate_pure_video 每次提交前调它拿闸门，
    随后 `gate.wait_for_window(deadline)` → 提交 → 命中 710022002 就 `note_closed()`。
    """
    global _GATE
    if not config.WINDOW_SNIFF_ENABLED:
        return None
    if _GATE is not None:
        return _GATE
    with _LOCK:
        if _GATE is None:
            sub = DolaProtocolSubmitter(proxy=proxy or config.PROXY or "",
                                        report_path=_default_report_path())
            _GATE = WindowGate(sub, probe_state_path=str(cookie_state_path),
                               report_path=_default_report_path())
            _GATE.start()
            print(f"[window] 全局窗口闸门已启动：探针号={_GATE.probe_account()} "
                  f"周期={sub and config.WINDOW_SNIFF_PERIOD_SEC}s 阈值={config.WINDOW_QUOTA_THRESHOLD}",
                  flush=True)
    return _GATE


def start_for_pool(accounts: list[str], *, cookie_state_of: Callable[[str], str | None],
                   report_path: str | os.PathLike[str] | None = None,
                   proxy: str = "") -> WindowGate | None:
    """给 server/BrowserPool 用的显式装配入口（选第一个 cookie 号当哨兵探针）。"""
    if not config.WINDOW_SNIFF_ENABLED:
        return None
    probe = ""
    for name in accounts or []:
        st = None
        try:
            st = cookie_state_of(name)
        except Exception:  # noqa: BLE001
            st = None
        if st:
            probe = st
            break
    if not probe:
        print("[window] 池中没有 cookie 号，窗口闸门未启动（退回旧行为）", flush=True)
        return None
    return ensure_gate_for_account(probe, proxy=proxy)


def get_gate() -> WindowGate | None:
    return _GATE


def get_sniffer() -> WindowSniffer | None:
    return _SNIFTER


def get_scheduler() -> BurstScheduler | None:
    return _SCHED


def ensure_started(pool, *, report_path: str | os.PathLike[str] | None = None) -> tuple[WindowSniffer | None, BurstScheduler | None]:
    """从 BrowserPool 实例装配并启动全局窗口引擎（幂等）。

    pool 需提供：
      * accounts() -> list[name]
      * cookie_state_path(name) -> path   （cookie 源号的 state 文件）
      * quota_left(name) -> int           （当日剩余配额）
      * proxy_url_for(name) -> str        （可选）
    """
    global _SNIFTER, _SCHED
    if not config.WINDOW_SNIFF_ENABLED:
        return None, None
    if _SNIFTER is not None and _SCHED is not None:
        return _SNIFTER, _SCHED
    with _LOCK:
        if _SNIFTER is not None and _SCHED is not None:
            return _SNIFTER, _SCHED
        sub = DolaProtocolSubmitter(proxy=config.PROXY or "", report_path=report_path)
        probe = pool.cookie_state_path(pool.accounts()[0]) if pool.accounts() else ""
        _SNIFTER = WindowSniffer(sub, probe_state_path=probe, report_path=report_path)
        _SCHED = BurstScheduler(sub, _SNIFTER, report_path=report_path)
        try:
            _SCHED.set_quota_fn(pool.quota_left)
        except Exception:  # noqa: BLE001
            pass
        try:
            _SCHED.set_proxy_fn(pool.proxy_url_for)
        except Exception:  # noqa: BLE001
            pass
        _SNIFTER.start()
        return _SNIFTER, _SCHED
