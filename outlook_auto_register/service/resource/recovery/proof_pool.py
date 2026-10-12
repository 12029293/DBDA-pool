"""「自产自销收码池」：一批可控老号（Graph 四段式），注册撞到 proofs 加安全信息时，
拿池里老号邮箱当**恢复邮箱**，微软把 OTT 验证码发到老号收件箱，再用老号 refresh_token
走 Graph 读回验证码完成 VerifyProof。

池文件每行四段式：`email----password----client_id----refresh_token`
默认路径：仓库根 `1000outlook.txt`；可用环境变量 `OUTLOOK_PROOF_POOL_FILE` 覆盖。
**只读使用、轮换选取，绝不改写池文件。**
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_DEFAULT_POOL_CANDIDATES = (
    "1000outlook.txt",
    "../1000outlook.txt",
    "../../1000outlook.txt",
)

_lock = threading.Lock()
_cursor = 0  # 轮换游标（进程内共享，避免每次都从头选同一个老号）


@dataclass
class ProofAccount:
    email: str
    password: str
    client_id: str
    refresh_token: str

    def masked(self) -> str:
        name, _, dom = self.email.partition("@")
        head = name[:2] if len(name) > 2 else name[:1]
        return f"{head}***@{dom}"


def pool_path() -> Optional[Path]:
    env = os.environ.get("OUTLOOK_PROOF_POOL_FILE", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    for cand in _DEFAULT_POOL_CANDIDATES:
        p = Path(cand)
        if p.exists():
            return p
    return None


def load_pool() -> list[ProofAccount]:
    p = pool_path()
    if not p:
        logger.warning("收码池文件不存在（OUTLOOK_PROOF_POOL_FILE 或 1000outlook.txt）")
        return []
    out: list[ProofAccount] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) < 4:
            continue
        email, password, client_id, refresh = parts[0], parts[1], parts[2], parts[3]
        if "@" in email and refresh:
            out.append(ProofAccount(email.strip(), password.strip(), client_id.strip(), refresh.strip()))
    logger.info("收码池载入 %d 个可控老号（来源 %s）", len(out), p)
    return out


def iter_accounts(limit: int = 8) -> Iterator[ProofAccount]:
    """从轮换游标开始产出至多 limit 个老号（只读，不改写文件）。"""
    global _cursor
    pool = load_pool()
    if not pool:
        return
    with _lock:
        start = _cursor % len(pool)
        _cursor = (_cursor + 1) % len(pool)
    n = min(limit, len(pool))
    for i in range(n):
        yield pool[(start + i) % len(pool)]


def pool_enabled() -> bool:
    """是否启用收码池满足 proofs（默认启用；池文件缺失或显式关闭则否）。"""
    if os.environ.get("OUTLOOK_PROOF_POOL", "1") == "0":
        return False
    return pool_path() is not None
