"""豆包域（www.doubao.com）frontierSign 签名器封装 —— 独立于 dola 版。

与 pure_signer.py 的差别：
  - 脚本换成 protocol/js/bdms_sign_doubao.js（豆包 webmssdk、aid=497858）
  - 环境变量用 DOUBAO_SIGNER_DAEMON
  - 其余（长驻进程、按 id 配对回包、超时隔离、一次性兜底）全部复用同一套实现

用法：
    from doubao_signer import get_doubao_signer
    signer = get_doubao_signer()
    signed_url = signer(url, method="POST", headers=h, body=body_json,
                        cookies=self.cookies)
    # signed_url 里已带 a_bogus=真签名
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

# 复用 pure_signer 的持久化签名核心（不改其源码，只换脚本与环境变量）
_DP = Path(__file__).resolve().parent
if str(_DP) not in sys.path:
    sys.path.insert(0, str(_DP))

from pure_signer import PersistentSigner, SignerPool, resolve_node  # noqa: E402

SIGN_TIMEOUT_SECONDS = float(os.getenv("DOUBAO_SIGN_TIMEOUT", "15"))
SIGN_RETRIES = 2
DEFAULT_PROCESSES = max(1, min(4, (os.cpu_count() or 4) // 2))


def doubao_signer_script() -> Path:
    """返回豆包域签名器 JS 路径。"""
    root = Path(__file__).resolve().parent / "protocol"
    for cand in (
        root / "js" / "bdms_sign_doubao.js",
        root / "bdms_sign_doubao.js",
    ):
        if cand.exists():
            return cand
    return root / "js" / "bdms_sign_doubao.js"


class _DoubaoPersistentSigner(PersistentSigner):
    """把 daemon 环境变量换成豆包专用的。"""

    def _start(self) -> None:
        import queue
        env = os.environ.copy()
        env["DOUBAO_SIGNER_DAEMON"] = "1"
        env["DOLA_SIGNER_DAEMON"] = "1"
        env["DOLA_SIGNER_PARENT_PID"] = str(os.getpid())   # 孤儿自尽（hint855）
        self._queue = queue.Queue()
        self._stderr_tail.clear()
        self._generation += 1
        # 与 pure_signer 同理：抑制每个 node 长驻签名进程的控制台弹窗。
        # 仅 CREATE_NO_WINDOW 在父进程已有控制台时会被忽略，必须叠加 DETACHED_PROCESS。
        _creationflags = getattr(pure_signer, "spawn_flags", lambda: 0)()
        self._proc = subprocess.Popen(
            [resolve_node(), str(self.script), "--daemon"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(self.script.parent), text=True, encoding="utf-8",
            errors="replace", bufsize=1, env=env,
            creationflags=_creationflags,
        )
        threading.Thread(target=self._read_stdout,
                         args=(self._proc, self._queue, self._generation),
                         daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self._proc,),
                         daemon=True).start()


class DoubaoSignerPool(SignerPool):
    def __init__(self, script: Path, size: int = DEFAULT_PROCESSES):
        size = max(1, int(size))
        self._script = Path(script)
        self._signers = [
            _DoubaoPersistentSigner(self._script, name=f"dbsigner-{i + 1}")
            for i in range(size)
        ]
        self._lock = threading.Lock()
        self._turn = 0


_SIGNER: DoubaoSignerPool | None = None
_LOCK = threading.Lock()
_AVAILABLE: bool | None = None


def get_doubao_signer() -> DoubaoSignerPool:
    global _SIGNER
    with _LOCK:
        if _SIGNER is None:
            _SIGNER = DoubaoSignerPool(doubao_signer_script())
        return _SIGNER


def signer_available() -> bool:
    """探测签名器是否可用（node 存在 + 脚本存在），失败不抛异常。"""
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE
    try:
        script = doubao_signer_script()
        node = resolve_node()
        ok = script.exists() and (Path(node).exists() or shutil.which(node))
        if not ok:
            _AVAILABLE = False
            return False
        # 真跑一次最小签名，确认 SDK 能加载
        s = get_doubao_signer()
        out = s("https://www.doubao.com/chat/completion?aid=497858",
                method="POST", headers={}, body="{}", cookies={})
        _AVAILABLE = "a_bogus=" in out
    except Exception:
        _AVAILABLE = False
    return bool(_AVAILABLE)


def sign_url(url: str, *, method: str = "POST", headers: dict | None = None,
             body: str = "{}", cookies: dict | None = None) -> str:
    """给 URL 加真 a_bogus，返回签名后的完整 URL。

    ★ 重要：豆包域**只加 a_bogus，不加 msToken**。
      实测伪 a_bogus → 710022002；无 a_bogus → 正常；真 a_bogus → 正常且过风控。
    """
    if not url:
        raise ValueError("url 不能为空")
    signer = get_doubao_signer()
    last = None
    for _ in range(SIGN_RETRIES):
        try:
            return signer(url, method=method, headers=headers or {},
                          body=body if isinstance(body, str)
                          else json.dumps(body, ensure_ascii=False,
                                          separators=(",", ":")),
                          cookies=cookies or {})
        except Exception as e:
            last = e
    raise RuntimeError(f"豆包域签名失败: {last}")


__all__ = ["get_doubao_signer", "DoubaoSignerPool", "doubao_signer_script",
           "signer_available", "sign_url"]
