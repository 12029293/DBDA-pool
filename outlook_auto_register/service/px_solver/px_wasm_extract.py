#!/usr/bin/env python3
"""从 PX captcha.js 提取 WASM / PoW 线索（纯协议逆向 Phase 2）。

用法：
  python px_solver/px_wasm_extract.py
  python px_solver/px_wasm_extract.py --har outlook.har
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAPTCHA_JS = HERE / "live_captcha.js"
OUT_DIR = HERE / "wasm_dump"
WASM_MAGIC = b"\x00asm"
POW_SEG = re.compile(r"oIIooIoo\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|([0-9a-f]{32,64})", re.I)


def _scan_js(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    info = {
        "path": str(path),
        "size": len(text),
        "webassembly_count": text.count("WebAssembly"),
        "module_count": text.count("Module"),
        "instantiate_count": text.count("instantiate"),
        "wasm_b64_hits": [],
    }
    for m in re.finditer(r"[A-Za-z0-9+/]{200,}={0,2}", text):
        raw = m.group()
        if len(raw) < 300:
            continue
        try:
            pad = "=="[: (4 - len(raw) % 4) % 4]
            dec = base64.b64decode(raw + pad)
            if dec[:4] == WASM_MAGIC:
                info["wasm_b64_hits"].append({"offset": m.start(), "bytes": len(dec)})
        except Exception:
            continue
        if len(info["wasm_b64_hits"]) >= 20:
            break
    return info


def _extract_pow_from_har(har_path: Path) -> list[dict]:
    sys.path.insert(0, str(HERE))
    from px_ob_decode import decode_ob

    tag = "YjIYfyxJHRR9"
    har = json.loads(har_path.read_text(encoding="utf-8", errors="replace"))
    hits: list[dict] = []
    for i, e in enumerate((har.get("log") or {}).get("entries") or []):
        resp = ((e.get("response") or {}).get("content") or {}).get("text") or ""
        try:
            ob = json.loads(resp).get("ob") or ""
        except Exception:
            continue
        if not ob:
            continue
        for seg in decode_ob(ob, tag):
            m = POW_SEG.search(seg)
            if m:
                hits.append({
                    "har_index": i,
                    "target_a": m.group(1),
                    "target_b": m.group(2),
                    "iterations": m.group(3),
                    "round": m.group(4),
                    "hash": m.group(5),
                })
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--har", type=Path, help="从 HAR ob 段提取 PoW 样本")
    ap.add_argument("--write", action="store_true", help="写出 wasm 二进制到 wasm_dump/")
    args = ap.parse_args()

    if CAPTCHA_JS.is_file():
        info = _scan_js(CAPTCHA_JS)
        print(json.dumps(info, indent=2))
        if args.write and info.get("wasm_b64_hits"):
            OUT_DIR.mkdir(exist_ok=True)
            text = CAPTCHA_JS.read_text(encoding="utf-8", errors="replace")
            for idx, hit in enumerate(info["wasm_b64_hits"][:5]):
                pos = hit["offset"]
                m = re.search(r"[A-Za-z0-9+/]{200,}={0,2}", text[pos : pos + 500000])
                if not m:
                    continue
                raw = m.group()
                pad = "=="[: (4 - len(raw) % 4) % 4]
                dec = base64.b64decode(raw + pad)
                if dec[:4] == WASM_MAGIC:
                    out = OUT_DIR / f"module_{idx}_{len(dec)}b.wasm"
                    out.write_bytes(dec)
                    print(f"wrote {out}")
    else:
        print(f"missing {CAPTCHA_JS}")

    if args.har and args.har.is_file():
        print("\nPoW segments from HAR ob:")
        print(json.dumps(_extract_pow_from_har(args.har), indent=2))

    print(
        "\nNote: wasm_b64_hits=0 时 WASM 为运行时合成；"
        "下一步在 Node WebAssembly.instantiate hook 处 dump Module bytes。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
