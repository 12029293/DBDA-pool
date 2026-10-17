# -*- coding: utf-8 -*-
"""素材库安全断言：素材**绝不能**被既有的参考图清理逻辑删掉。

这是本方案（创作台 + 素材库）最关键的一条回归测试。背景（方案文档 §3.1）：

  media.cleanup_local_references()   任务终态删 refs/ 下的文件（server.py:826）
  media.sweep_stale_references()     服务启动删 refs/ 下 24h+ 的文件（server.py:999）

素材库把用户素材放在 config.ASSET_DIR（assets/）下。若哪天有人把
is_local_reference 改成"也认 assets/"，或让清理函数扫到 assets/，
**用户的素材会在任务结束时被静默删除** —— 这是数据损失级的事故，
且很难在人工测试中发现（要正好有个带素材的任务跑到终态）。

所以这里用最直接的方式钉死：
  1. 建一个 assets/ 下的素材文件；
  2. 跑 cleanup_local_references([素材路径]) → 断言文件**仍在**；
  3. 把 mtime 改成 48 小时前，跑 sweep_stale_references() → 断言文件**仍在**；
  4. 反向断言：refs/ 下的临时产物**确实会被删**（保证清理逻辑本身没被改坏）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import media  # noqa: E402


def _run(coro):
    # 与 tests/test_reference_materialize.py 同款：不用 asyncio.run
    # （它会关掉主线程默认事件循环，带崩其它用 get_event_loop 的测试）
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    """把素材目录与参考图目录都指到临时目录，避免污染真实工作区。"""
    asset_dir = tmp_path / "assets"
    ref_dir = tmp_path / "refs"
    thumb_dir = tmp_path / "thumbs"
    asset_dir.mkdir()
    ref_dir.mkdir()
    thumb_dir.mkdir()
    monkeypatch.setattr(config, "ASSET_DIR", str(asset_dir), raising=False)
    monkeypatch.setattr(config, "REFERENCE_FILE_DIR", str(ref_dir), raising=False)
    monkeypatch.setattr(config, "THUMB_DIR", str(thumb_dir), raising=False)
    return {"asset": asset_dir, "ref": ref_dir, "thumb": thumb_dir}


def _write(path: Path, payload: bytes = b"fake-image-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


# --------------------------------------------------------------- 谓词 --

def test_asset_and_local_predicates_are_disjoint(dirs):
    """素材与临时产物必须被两个谓词**各自**识别，不能互相认。"""
    asset = _write(dirs["asset"] / "img" / "aa" / "x.png")
    ref = _write(dirs["ref"] / "ref_y.png")

    assert media.is_asset_reference(str(asset)) is True
    assert media.is_local_reference(str(asset)) is False, "素材不能被当成临时物化产物"

    assert media.is_local_reference(str(ref)) is True
    assert media.is_asset_reference(str(ref)) is False, "临时产物不能被当成素材"


def test_asset_predicate_rejects_outside_and_missing(dirs):
    """素材谓词只认 assets/ 之内、且真实存在的文件。"""
    outside = _write(dirs["asset"].parent / "outside.png")
    assert media.is_asset_reference(str(outside)) is False

    missing = dirs["asset"] / "img" / "gone.png"
    assert media.is_asset_reference(str(missing)) is False
    # 但"形如素材但文件不在"要被幽灵判定认出来，好给出明确错误
    assert media._is_ghost_asset_ref(str(missing)) is True


# --------------------------------------------------- 清理逻辑（核心）--

def test_cleanup_does_not_touch_assets(dirs):
    """★ 任务终态清理：素材必须**原样保留**。"""
    asset = _write(dirs["asset"] / "img" / "keep.png")

    media.cleanup_local_references([str(asset)])

    assert asset.is_file(), "素材被 cleanup_local_references 删掉了（数据损失级 Bug）"


def test_cleanup_still_removes_refs(dirs):
    """反向断言：refs/ 下的临时产物仍然会被删（清理逻辑本身没被改坏）。"""
    ref = _write(dirs["ref"] / "ref_tmp.png")

    media.cleanup_local_references([str(ref)])

    assert not ref.exists(), "refs/ 临时产物应被清理"


def test_sweep_does_not_touch_assets(dirs):
    """★ 服务启动清理：48 小时前的素材**依然不能删**。"""
    asset = _write(dirs["asset"] / "img" / "old.png")
    old = time.time() - 48 * 3600
    os.utime(asset, (old, old))

    media.sweep_stale_references(max_age_seconds=24 * 3600)

    assert asset.is_file(), "素材被 sweep_stale_references 删掉了（数据损失级 Bug）"


def test_sweep_still_removes_stale_refs(dirs):
    """反向断言：refs/ 下超期的临时产物仍会被清。"""
    ref = _write(dirs["ref"] / "ref_old.png")
    old = time.time() - 48 * 3600
    os.utime(ref, (old, old))

    removed = media.sweep_stale_references(max_age_seconds=24 * 3600)

    assert not ref.exists()
    assert removed >= 1


# ------------------------------------------------------- 校验放行 --

def test_validate_reference_urls_allows_assets(dirs):
    """素材路径必须能被 validate_reference_urls 放行。

    否则「创作台引用素材 → 提交上游」这条链路第一步就会被拒成
    "参考图片只支持 http/https 公网 URL"。
    """
    asset = _write(dirs["asset"] / "img" / "ok.png")

    out = _run(media.validate_reference_urls([str(asset)]))

    assert len(out) == 1
    assert Path(out[0]).resolve() == asset.resolve()


def test_validate_reports_deleted_asset_clearly(dirs):
    """素材文件被硬删后，报错要说"素材已删除"，而不是含糊的格式问题。"""
    ghost = dirs["asset"] / "img" / "ghost.png"

    with pytest.raises(ValueError, match="素材文件已删除"):
        _run(media.validate_reference_urls([str(ghost)]))


def test_download_reference_images_reuses_assets_in_place(dirs):
    """素材要**原地复用**：不复制进临时目录，返回的 root 为 None。

    这条保证调用方的 cleanup（rmtree 临时目录 / unlink refs 物化产物）
    碰不到素材文件。
    """
    asset = _write(dirs["asset"] / "img" / "reuse.png")

    root, paths = _run(media.download_reference_images([str(asset)], "t001"))

    assert root is None, "素材全部为本地文件时不应建临时目录"
    assert [Path(p).resolve() for p in paths] == [asset.resolve()]
    assert asset.is_file()


# ------------------------------------------------- 单元级：入库不删 --

def test_asset_store_ingest_keeps_file_and_dedups(tmp_path, monkeypatch):
    """素材入库：文件真实落盘、同内容去重、解析回绝对路径。"""
    import asset_store as A

    monkeypatch.setattr(config, "ASSET_DIR", str(tmp_path / "assets"), raising=False)
    monkeypatch.setattr(config, "ASSET_THUMB_DIR",
                        str(tmp_path / "assets" / ".thumbs"), raising=False)
    monkeypatch.setattr(config, "ASSET_DB_PATH", str(tmp_path / "assets.db"), raising=False)
    monkeypatch.setattr(A, "DB_PATH", tmp_path / "assets.db", raising=False)
    A._ready_paths.clear()
    A.ensure_dirs()

    payload = b"\x89PNG\r\n\x1a\n" + b"z" * 32
    first = A.ingest_bytes(payload, filename="a.png", kind="image", name="甲")
    again = A.ingest_bytes(payload, filename="a.png", kind="image", name="乙")

    assert again["id"] == first["id"], "同内容应命中去重"
    assert again["deduped"] is True

    path = A.abs_path(first["rel_path"])
    assert path.is_file(), "素材文件必须真实落盘"

    # 解析出的路径必须能被 media 放行（打通提交上游的第一步）
    assert media.is_asset_reference(str(path)) is True

    # 软删后文件仍在（可恢复）；hard purge 才删文件
    assert A.delete_asset(first["id"])["ok"] is True
    assert path.is_file(), "软删除不得删文件"
    assert A.delete_asset(first["id"], purge=True)["ok"] is True
    assert not path.exists(), "purge 应删除文件"


def test_asset_store_rejects_path_escape(tmp_path, monkeypatch):
    """rel_path 越界必须被拒绝（防把任意本地文件当素材上传）。"""
    import asset_store as A

    monkeypatch.setattr(config, "ASSET_DIR", str(tmp_path / "assets"), raising=False)
    monkeypatch.setattr(A, "DB_PATH", tmp_path / "assets.db", raising=False)
    A._ready_paths.clear()
    A.ensure_dirs()

    with pytest.raises(ValueError):
        A.abs_path("../../etc/passwd")
