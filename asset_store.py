# -*- coding: utf-8 -*-
"""asset_store.py —— 素材库存储层（创作台的参考素材 + 生成产物入库）。

为什么独立成一个库（assets.db）而不是并进 tasks.db
--------------------------------------------------------------------------
1. `store.py` 的 tasks 表是**任务流水**，任务过保留期就被 prune_finished 删
   （config.TASK_RETENTION_DAYS，默认 14 天）。素材是**用户的长期资产**，
   生命周期与任务完全不同，混在一张表里迟早会被保留期逻辑误删。
2. `store.py` 被 42 个既有测试覆盖，动它风险高。本模块沿用 proxy_store.py /
   user_store.py 的「独立模块 + 独立库 + _init_schema」套路，零侵入。

与 refs/ 的关键区别（★ 方案文档 §3.1）
--------------------------------------------------------------------------
REFERENCE_FILE_DIR（refs/）里的是**临时物化产物**：
  · media.cleanup_local_references() 在任务终态删它（server.py:826）
  · media.sweep_stale_references() 在服务启动删 24h+ 的它（server.py:999）
素材放在 config.ASSET_DIR（assets/）下，**永不被这两个函数触碰**（谓词分离，
见 media.is_asset_reference）。这是"素材库不丢文件"的根本保证。

数据模型
--------------------------------------------------------------------------
assets      素材元数据（rel_path 只存相对路径 → 整个 assets/ 可整体搬迁）
asset_refs  素材 ↔ 任务的引用关系（回答"这条成片用了哪些素材"，也是
            「再次生成」能复现的前提）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import config

try:
    import _safe_sqlite
except Exception:  # pragma: no cover
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _safe_sqlite

DB_PATH = Path(getattr(config, "ASSET_DB_PATH", "assets.db"))

KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KINDS = (KIND_IMAGE, KIND_VIDEO, KIND_AUDIO)

SOURCE_UPLOAD = "upload"
SOURCE_GENERATED = "generated"
SOURCE_HARVESTED = "harvested"
SOURCES = (SOURCE_UPLOAD, SOURCE_GENERATED, SOURCE_HARVESTED)

# 每种素材的扩展名白名单（上传校验用；与 media._ALLOWED_IMAGE_FORMATS 对齐）
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".webm")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg")

# 生成产物（audit 用）：任务号里可能带路径分隔符，落盘前统一清洗
_UNSAFE = '<>:"/\\|?*'


# ------------------------------------------------------------------ 路径 --

def assets_root() -> Path:
    """素材根目录的绝对路径（每次读 config，便于测试夹具改配置）。"""
    return Path(getattr(config, "ASSET_DIR", "assets")).resolve()


def thumbs_root() -> Path:
    return Path(getattr(config, "ASSET_THUMB_DIR",
                        os.path.join(getattr(config, "ASSET_DIR", "assets"), ".thumbs"))).resolve()


def abs_path(rel_path: str) -> Path:
    """把库里的 rel_path 解析成绝对路径，并**防目录穿越**。

    素材路径会一路传到引擎、再传到上游上传逻辑，若被 ../ 逃逸出去就等于
    把任意本地文件当素材上传。这里做硬校验：解析结果必须落在 assets/ 之内。
    """
    root = assets_root()
    candidate = (root / str(rel_path or "")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"素材路径越界: {rel_path!r}") from exc
    return candidate


def rel_of(path: Path) -> str:
    """绝对路径 → 库里存的相对路径（统一正斜杠，跨平台可搬迁）。"""
    rel = Path(path).resolve().relative_to(assets_root())
    return rel.as_posix()


def _safe_name(name: str, fallback: str = "asset") -> str:
    cleaned = "".join("_" if c in _UNSAFE else c for c in str(name or "")).strip()
    return cleaned or fallback


def _now() -> float:
    return time.time()


# ------------------------------------------------------------------ 库 --

# 已建表的库路径（同 proxy_store：热路径不碰 DDL，避免 database is locked）
_ready_paths: set[str] = set()


def _connect() -> sqlite3.Connection:
    conn = _safe_sqlite.connect(str(DB_PATH), check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        if str(DB_PATH) not in _ready_paths:
            _init_schema(conn)
            _ready_paths.add(str(DB_PATH))
    except Exception:
        conn.close()
        raise
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assets (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            name TEXT,
            rel_path TEXT NOT NULL,
            thumb TEXT,
            mime TEXT,
            bytes INTEGER,
            width INTEGER,
            height INTEGER,
            duration REAL,
            sha256 TEXT,
            tags TEXT DEFAULT '[]',
            group_name TEXT,
            note TEXT,
            channel TEXT,
            prompt TEXT,
            origin_task_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL,
            deleted_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS asset_refs (
            task_id TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            position INTEGER DEFAULT 0,
            created_at REAL,
            PRIMARY KEY (task_id, asset_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets(kind, deleted_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_sha256 ON assets(sha256)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_created ON assets(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_asset_refs_asset ON asset_refs(asset_id)")
    conn.commit()


def _row(row) -> dict | None:
    if not row:
        return None
    data = dict(row)
    raw = data.get("tags")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "[]")
        except (TypeError, ValueError):
            parsed = []
        data["tags"] = parsed if isinstance(parsed, list) else []
    elif raw is None:
        data["tags"] = []
    data["deleted"] = bool(data.get("deleted_at"))
    return data


# ------------------------------------------------------------------ CRUD --

def find_by_sha256(sha256: str, kind: str | None = None) -> dict | None:
    """按内容哈希找未删除的同素材（去重用）。"""
    if not sha256:
        return None
    sql = "SELECT * FROM assets WHERE sha256=? AND deleted_at IS NULL"
    args: list[Any] = [sha256]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY created_at ASC LIMIT 1"
    conn = _connect()
    try:
        return _row(conn.execute(sql, args).fetchone())
    finally:
        conn.close()


def create_asset(
    *,
    kind: str,
    source: str,
    name: str,
    rel_path: str,
    thumb: str | None = None,
    mime: str = "",
    bytes_: int = 0,
    width: int | None = None,
    height: int | None = None,
    duration: float | None = None,
    sha256: str = "",
    tags: list[str] | None = None,
    group_name: str = "",
    note: str = "",
    channel: str = "",
    prompt: str = "",
    origin_task_id: str = "",
    asset_id: str | None = None,
) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 {KINDS}")
    if source not in SOURCES:
        raise ValueError(f"source 只能是 {SOURCES}")
    aid = asset_id or ("ast_" + uuid.uuid4().hex)
    now = _now()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO assets (id,kind,source,name,rel_path,thumb,mime,bytes,"
            "width,height,duration,sha256,tags,group_name,note,channel,prompt,"
            "origin_task_id,created_at,updated_at,deleted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (aid, kind, source, _safe_name(name, aid), rel_path, thumb, mime,
             int(bytes_ or 0), width, height, duration, sha256 or "",
             json.dumps(list(tags or []), ensure_ascii=False), group_name, note,
             channel, prompt[:2000] if prompt else "", origin_task_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_asset(aid) or {}


def get_asset(asset_id: str) -> dict | None:
    conn = _connect()
    try:
        return _row(conn.execute(
            "SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone())
    finally:
        conn.close()


def list_assets(
    *,
    kind: str | None = None,
    q: str | None = None,
    tag: str | None = None,
    group_name: str | None = None,
    source: str | None = None,
    channel: str | None = None,
    include_deleted: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """素材列表 + 总数（面板网格墙用）。过滤在 SQL 层做，避免整表读进内存。"""
    where: list[str] = []
    args: list[Any] = []
    if not include_deleted:
        where.append("deleted_at IS NULL")
    if kind:
        where.append("kind=?")
        args.append(kind)
    if source:
        where.append("source=?")
        args.append(source)
    if channel:
        where.append("channel=?")
        args.append(channel)
    if group_name:
        where.append("group_name=?")
        args.append(group_name)
    if tag:
        # tags 是 JSON 数组字符串；用 LIKE 做包含匹配（素材量级下足够，且能命中小索引）
        where.append("tags LIKE ?")
        args.append(f'%"{tag}"%')
    if q:
        needle = f"%{q}%"
        where.append("(name LIKE ? OR prompt LIKE ? OR note LIKE ? OR group_name LIKE ?)")
        args.extend([needle, needle, needle, needle])
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    conn = _connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM assets{clause}", args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM assets{clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        return {"assets": [_row(r) for r in rows], "total": int(total or 0),
                "limit": limit, "offset": offset}
    finally:
        conn.close()


def update_asset(asset_id: str, **fields) -> dict | None:
    """改名 / 标签 / 分组 / 备注（只允许这几个字段，防误改路径与哈希）。"""
    allowed = {"name", "tags", "group_name", "note"}
    sets, args = [], []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "tags":
            value = json.dumps(list(value), ensure_ascii=False)
        elif key == "name":
            value = _safe_name(str(value), asset_id)
        sets.append(f"{key}=?")
        args.append(value)
    if not sets:
        return get_asset(asset_id)
    sets.append("updated_at=?")
    args.append(_now())
    args.append(asset_id)
    conn = _connect()
    try:
        conn.execute(f"UPDATE assets SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()
    finally:
        conn.close()
    return get_asset(asset_id)


def delete_asset(asset_id: str, *, purge: bool = False) -> dict:
    """删除素材。默认**软删除**（保留文件，历史任务仍能显示"素材已删除"）。

    purge=True 时连文件一起删 —— 但若仍被任务引用则拒绝，避免把历史
    「再次生成」所依赖的素材静默抽走。
    """
    asset = get_asset(asset_id)
    if not asset:
        return {"ok": False, "reason": "not_found"}
    refs = refs_for_asset(asset_id)
    if purge and refs:
        return {"ok": False, "reason": "referenced", "refs": refs}
    conn = _connect()
    try:
        if purge:
            conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            conn.execute("DELETE FROM asset_refs WHERE asset_id=?", (asset_id,))
        else:
            conn.execute("UPDATE assets SET deleted_at=?, updated_at=? WHERE id=?",
                         (_now(), _now(), asset_id))
        conn.commit()
    finally:
        conn.close()
    removed_files = 0
    if purge:
        removed_files = _purge_files(asset)
    return {"ok": True, "id": asset_id, "purged": bool(purge),
            "removed_files": removed_files, "refs": refs}


def _purge_files(asset: dict) -> int:
    """硬删时移除素材文件与预览图；文件不存在也算成功（幂等）。

    内容寻址的坑：同一 sha256 的素材可能被多条记录共享（去重命中时不会重复落盘），
    所以删文件前要先确认**没有别的未删记录**还在引用同一个 rel_path。
    """
    removed = 0
    rel = str(asset.get("rel_path") or "")
    if rel:
        conn = _connect()
        try:
            shared = conn.execute(
                "SELECT COUNT(*) AS c FROM assets WHERE rel_path=? AND id<>?"
                " AND deleted_at IS NULL", (rel, asset.get("id"))).fetchone()["c"]
        finally:
            conn.close()
        if not shared:
            try:
                path = abs_path(rel)
                if path.is_file():
                    path.unlink()
                    removed += 1
            except (ValueError, OSError):
                pass
    thumb = thumb_path(asset.get("thumb"))
    if thumb is not None:
        try:
            if thumb.is_file():
                thumb.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def bulk_update(asset_ids: list[str], **fields) -> int:
    """批量打标签/分组（tags 传 'add' 语义时由调用方先算好最终值）。"""
    allowed = {"tags", "group_name", "note"}
    sets, args = [], []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "tags":
            value = json.dumps(list(value), ensure_ascii=False)
        sets.append(f"{key}=?")
        args.append(value)
    if not sets or not asset_ids:
        return 0
    sets.append("updated_at=?")
    args.append(_now())
    marks = ",".join("?" for _ in asset_ids)
    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE assets SET {', '.join(sets)} WHERE id IN ({marks}) AND deleted_at IS NULL",
            [*args, *asset_ids])
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


# ------------------------------------------------------------------ 引用 --

def link_refs(task_id: str, asset_ids: list[str]) -> int:
    """登记「这个任务引用了哪些素材」（幂等，position 保留选择顺序）。"""
    if not task_id or not asset_ids:
        return 0
    now = _now()
    conn = _connect()
    try:
        n = 0
        for pos, aid in enumerate(asset_ids):
            cur = conn.execute(
                "INSERT OR IGNORE INTO asset_refs (task_id,asset_id,position,created_at)"
                " VALUES (?,?,?,?)", (task_id, aid, pos, now))
            n += int(cur.rowcount or 0)
        conn.commit()
        return n
    finally:
        conn.close()


def refs_for_asset(asset_id: str, limit: int = 50) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT task_id,position,created_at FROM asset_refs WHERE asset_id=?"
            " ORDER BY created_at DESC LIMIT ?", (asset_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def refs_for_task(task_id: str) -> list[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT asset_id FROM asset_refs WHERE task_id=? ORDER BY position",
            (task_id,)).fetchall()
        return [r["asset_id"] for r in rows]
    finally:
        conn.close()


def resolve_paths(asset_ids: list[str]) -> tuple[list[str], list[str]]:
    """素材 id 列表 → 本地绝对路径列表。

    返回 (paths, missing)：missing 是找不到/已删除/文件不存在的 id。
    ★ 这是"把素材同步提交给上游"的入口：返回的路径会进
      reference_images → media.validate_reference_urls（由 is_asset_reference 放行）
      → 引擎/号池上传到上游。
    """
    paths: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    for aid in asset_ids or []:
        aid = str(aid or "").strip()
        if not aid or aid in seen:
            continue
        seen.add(aid)
        asset = get_asset(aid)
        if not asset or asset.get("deleted"):
            missing.append(aid)
            continue
        try:
            path = abs_path(asset["rel_path"])
        except ValueError:
            missing.append(aid)
            continue
        if not path.is_file():
            missing.append(aid)
            continue
        paths.append(str(path))
    return paths, missing


# ------------------------------------------------------------------ 统计 --

def stats() -> dict:
    """素材库概览：总数/分类/已引用/未引用/占用空间（面板 KPI 卡片）。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(bytes),0) AS bytes"
            " FROM assets WHERE deleted_at IS NULL").fetchone()
        by_kind = {r["kind"]: r["c"] for r in conn.execute(
            "SELECT kind, COUNT(*) AS c FROM assets WHERE deleted_at IS NULL"
            " GROUP BY kind").fetchall()}
        referenced = conn.execute(
            "SELECT COUNT(DISTINCT asset_id) AS c FROM asset_refs").fetchone()["c"]
        deleted = conn.execute(
            "SELECT COUNT(*) AS c FROM assets WHERE deleted_at IS NOT NULL").fetchone()["c"]
    finally:
        conn.close()
    total = int(row["total"] or 0)
    referenced = int(referenced or 0)
    return {
        "total": total,
        "bytes": int(row["bytes"] or 0),
        "max_bytes": int(getattr(config, "ASSET_MAX_BYTES", 0) or 0),
        "by_kind": {k: int(by_kind.get(k, 0)) for k in KINDS},
        "referenced": referenced,
        "unreferenced": max(0, total - referenced),
        "deleted": int(deleted or 0),
    }


def total_bytes() -> int:
    return int(stats().get("bytes") or 0)


# ------------------------------------------------------------------ 入库 --

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _image_meta(path: Path) -> dict:
    """读图片宽高/格式；失败返回空（不阻断入库）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return {"width": int(im.width), "height": int(im.height),
                    "mime": f"image/{str(im.format or '').lower()}"}
    except Exception:
        return {}


def _video_meta(path: Path) -> dict:
    """读视频时长/宽高；ffprobe 缺失或失败时返回空（不阻断）。"""
    import subprocess
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=20, creationflags=flags)
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return {}
    stream = next((s for s in data.get("streams", [])
                   if s.get("codec_type") == "video"), None)
    out: dict = {}
    if stream:
        try:
            out["width"] = int(stream.get("width") or 0) or None
            out["height"] = int(stream.get("height") or 0) or None
        except (TypeError, ValueError):
            pass
    fmt = data.get("format") or {}
    dur = (stream or {}).get("duration") or fmt.get("duration")
    try:
        out["duration"] = float(dur) if dur else None
    except (TypeError, ValueError):
        pass
    return {k: v for k, v in out.items() if v is not None}


def make_thumb(src: Path, asset_id: str) -> str | None:
    """生成 512px 预览图（面板网格墙用），返回文件名。

    ★ 不能复用 media.save_reference_thumbnails：那套是 160px 且
      REFERENCE_THUMB_COUNT 默认只存 1 张，且跟随任务清理 —— 素材要长期可用。
    """
    try:
        from PIL import Image
    except Exception:
        return None
    if not src.is_file():
        return None
    max_px = max(64, int(getattr(config, "ASSET_THUMB_MAX_PX", 512)))
    quality = min(95, max(30, int(getattr(config, "ASSET_THUMB_QUALITY", 82))))
    try:
        root = thumbs_root()
        root.mkdir(parents=True, exist_ok=True)
        name = f"{asset_id}.jpg"
        with Image.open(src) as im:
            thumb = im.convert("RGB")
            thumb.thumbnail((max_px, max_px))
            thumb.save(root / name, "JPEG", quality=quality, optimize=True)
        return name
    except Exception:
        return None


def thumb_path(name: str | None) -> Path | None:
    """预览图文件名 → 绝对路径（防穿越）。"""
    if not name:
        return None
    try:
        root = thumbs_root()
        candidate = (root / Path(str(name)).name).resolve()
        candidate.relative_to(root)
    except (ValueError, OSError):
        return None
    return candidate


def _check_quota(incoming: int) -> None:
    """总量配额：超限**拒绝**（绝不自动删用户素材）。"""
    limit = int(getattr(config, "ASSET_MAX_BYTES", 0) or 0)
    if limit <= 0:
        return
    used = total_bytes()
    if used + int(incoming or 0) > limit:
        raise ValueError(
            "素材库空间已满（%.2f GB / %.2f GB），请先删除部分素材"
            % (used / 1073741824, limit / 1073741824))


def ingest_bytes(
    data: bytes,
    *,
    filename: str = "",
    kind: str = KIND_IMAGE,
    source: str = SOURCE_UPLOAD,
    name: str = "",
    tags: list[str] | None = None,
    group_name: str = "",
    prompt: str = "",
    channel: str = "",
    origin_task_id: str = "",
) -> dict:
    """把一段字节存进素材库（上传路径）。按 sha256 内容寻址 + 去重。

    返回素材行；若命中去重则返回既有行并带 deduped=True。
    """
    if not data:
        raise ValueError("空文件")
    kind = kind or KIND_IMAGE
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 {KINDS}")
    digest = _sha256_bytes(data)
    if getattr(config, "ASSET_DEDUP", True):
        existing = find_by_sha256(digest, kind)
        if existing:
            existing["deduped"] = True
            return existing
    _check_quota(len(data))

    ext = Path(filename or "").suffix.lower()
    if not ext or ext not in (*IMAGE_EXTS, *VIDEO_EXTS, *AUDIO_EXTS):
        ext = _default_ext(kind)
    root = assets_root()
    dest = root / _kind_dir(kind) / digest[:2] / f"{digest}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.is_file():
        dest.write_bytes(data)

    meta: dict = {}
    if kind == KIND_IMAGE:
        meta = _image_meta(dest)
    elif kind == KIND_VIDEO:
        meta = _video_meta(dest)
    aid = "ast_" + uuid.uuid4().hex
    thumb = make_thumb(dest, aid) if kind in (KIND_IMAGE, KIND_VIDEO) else None
    asset = create_asset(
        kind=kind, source=source, name=name or filename or dest.name,
        rel_path=rel_of(dest), thumb=thumb, mime=meta.get("mime") or _guess_mime(ext),
        bytes_=len(data), width=meta.get("width"), height=meta.get("height"),
        duration=meta.get("duration"), sha256=digest, tags=tags,
        group_name=group_name, prompt=prompt, channel=channel,
        origin_task_id=origin_task_id, asset_id=aid,
    )
    asset["deduped"] = False
    return asset


def ingest_file(src: str | Path, **kwargs) -> dict:
    """把已有文件登记进素材库。

    上传素材 → 复制进 assets/<kind>/<sha2>/（内容寻址，可去重）；
    生成产物 → 用 task_id 命名放 assets/out/（便于回溯，不做去重）。
    """
    src_path = Path(src)
    if not src_path.is_file():
        raise ValueError(f"文件不存在: {src_path}")
    data = src_path.read_bytes()
    source = kwargs.get("source") or SOURCE_UPLOAD
    if source == SOURCE_UPLOAD:
        return ingest_bytes(data, filename=src_path.name, **kwargs)

    digest = _sha256_bytes(data)
    _check_quota(len(data))
    kind = kwargs.get("kind") or KIND_VIDEO
    ext = src_path.suffix.lower() or _default_ext(kind)
    task_id = _safe_name(kwargs.get("origin_task_id") or "", "gen")
    root = assets_root()
    dest = root / "out" / f"{task_id}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 同名不同内容（重投同一 task_id）：加哈希后缀，避免互相覆盖
    if dest.is_file() and _sha256_bytes(dest.read_bytes()) != digest:
        dest = dest.with_name(f"{task_id}_{digest[:8]}{ext}")
    if not dest.is_file():
        shutil.copy2(src_path, dest)

    meta: dict = {}
    if kind == KIND_IMAGE:
        meta = _image_meta(dest)
    elif kind == KIND_VIDEO:
        meta = _video_meta(dest)
    aid = "ast_" + uuid.uuid4().hex
    thumb = make_thumb(dest, aid) if kind in (KIND_IMAGE, KIND_VIDEO) else None
    return create_asset(
        kind=kind, source=source,
        name=kwargs.get("name") or src_path.name, rel_path=rel_of(dest),
        thumb=thumb, mime=meta.get("mime") or _guess_mime(ext), bytes_=len(data),
        width=meta.get("width"), height=meta.get("height"),
        duration=meta.get("duration"), sha256=digest, tags=kwargs.get("tags"),
        group_name=kwargs.get("group_name") or "", prompt=kwargs.get("prompt") or "",
        channel=kwargs.get("channel") or "", origin_task_id=task_id or "",
        asset_id=aid,
    )


def _kind_dir(kind: str) -> str:
    return {KIND_IMAGE: "img", KIND_VIDEO: "vid", KIND_AUDIO: "aud"}.get(kind, "other")


def _default_ext(kind: str) -> str:
    return {KIND_IMAGE: ".png", KIND_VIDEO: ".mp4", KIND_AUDIO: ".mp3"}.get(kind, ".bin")


def _guess_mime(ext: str) -> str:
    ext = (ext or "").lower()
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext == ".mp4":
        return "video/mp4"
    if ext == ".webm":
        return "video/webm"
    if ext == ".mp3":
        return "audio/mpeg"
    if ext == ".wav":
        return "audio/wav"
    return "application/octet-stream"


def ensure_dirs() -> None:
    """启动时建好目录（静态挂载前必须存在）。"""
    assets_root().mkdir(parents=True, exist_ok=True)
    thumbs_root().mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        (assets_root() / _kind_dir(kind)).mkdir(parents=True, exist_ok=True)
    (assets_root() / "out").mkdir(parents=True, exist_ok=True)
