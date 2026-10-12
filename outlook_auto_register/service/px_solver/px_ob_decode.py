#!/usr/bin/env python3
"""PerimeterX collector `ob` / sensor payload 解码（unobpx 算法，纯本地）。

用法：
  python px_solver/px_ob_decode.py --har path/to/outlook.har
"""
from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path
from urllib.parse import parse_qs


def tag_xor_key(tag: str) -> int:
    e = 0
    for ch in tag:
        e = (31 * e + ord(ch)) % 2147483647
    return ((e % 900) + 100) % 128


def decode_ob(ob_b64: str, tag: str) -> list[str]:
    key = tag_xor_key(tag)
    raw = base64.b64decode(ob_b64 + "==")
    plain = bytes(b ^ key for b in raw)
    # OB 是 binary，按 latin-1 保字节再 split
    text = plain.decode("latin-1", "replace")
    return [p for p in text.split("~~~~") if p]


def xor_str(s: str, key: int) -> str:
    return "".join(chr(ord(c) ^ key) for c in s)


def b64e(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def compute_indices(key_str: str, payload_len: int, uuid: str) -> list[int]:
    r = xor_str(b64e(uuid), 10)
    max_val = -1
    for i in range(len(key_str)):
        row = i // len(r) + 1
        col = i % len(r)
        product = ord(r[col]) * ord(r[row % len(r)] if row >= len(r) else r[row])
        # Go: int(r[row]) — r is string, row can exceed len; Go would panic.
        # PX uses r[row] with row = i/len(r)+1, so row is 1.. and must be < len(r).
        if row < len(r):
            product = ord(r[col]) * ord(r[row])
        else:
            product = ord(r[col]) * ord(r[row % len(r)])
        if product > max_val:
            max_val = product
    positions: list[int] = []
    for i in range(len(key_str)):
        row = i // len(r) + 1
        col = i % len(r)
        if row < len(r):
            pos = ord(r[col]) * ord(r[row])
        else:
            pos = ord(r[col]) * ord(r[row % len(r)])
        if pos >= payload_len:
            pos = int(((pos - 0) / max(max_val, 1)) * (payload_len - 1))
        while pos in positions:
            pos += 1
        positions.append(pos)
    return sorted(positions)


from typing import Any


def decode_sensor(encoded: str, uuid: str, sts: str = "1604064986000") -> str:
    key = xor_str(b64e(sts), 10)
    key_len = len(key)
    indices = compute_indices(key, len(encoded) - key_len, uuid)
    chars = list(encoded)
    for pos in sorted((i - 1 for i in indices), reverse=True):
        if 0 <= pos < len(chars):
            chars.pop(pos)
    b64_payload = "".join(chars)
    clean = "".join(c for c in b64_payload if c.isalnum() or c in "+/=")
    pad = (-len(clean)) % 4
    raw = base64.b64decode(clean + ("=" * pad))
    return bytes(x ^ 50 for x in raw).decode("utf-8", "replace")


def extract_sts_from_sid(sid: str) -> str:
    """sid 可能嵌入 sts（Unicode 变体选择符分隔）；否则用 PX 默认首包 sts。"""
    if not sid:
        return "1604064986000"
    if sid.isdigit() and len(sid) >= 10:
        return sid
    digits = "".join(ch for ch in sid if ch.isdigit())
    if len(digits) >= 13:
        return digits[:13]
    return "1604064986000"


def decode_payload_form(params: dict[str, str]) -> dict[str, Any]:
    """从 bundle form 字段解码 payload（需 uuid + sid 内嵌 sts）。"""
    pl = params.get("payload") or ""
    uid = params.get("uuid") or ""
    if not pl or not uid:
        return {"ok": False, "error": "missing payload or uuid"}
    sts = extract_sts_from_sid(params.get("sid") or "")
    try:
        text = decode_sensor(pl, uid, sts)
        preview = text[:400] if text else ""
        return {"ok": True, "sts": sts, "preview": preview, "len": len(text)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "sts": sts}


def xor50_preview(b64_payload: str, max_len: int = 8000) -> str:
    """payload 前段 XOR50 预览（不解 interleave）。"""
    clean = "".join(c for c in b64_payload[:max_len] if c.isalnum() or c in "+/=")
    pad = (-len(clean)) % 4
    raw = base64.b64decode(clean + ("=" * pad))
    return bytes(x ^ 50 for x in raw[:120]).decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--har", required=True)
    args = ap.parse_args()
    har = json.loads(Path(args.har).read_text(encoding="utf-8", errors="replace"))
    found_px3 = 0
    for i, e in enumerate((har.get("log") or {}).get("entries") or []):
        req = e.get("request") or {}
        url = req.get("url") or ""
        if "hsprotect" not in url or req.get("method") != "POST":
            continue
        post = req.get("postData") or {}
        params = {}
        if post.get("params"):
            for p in post["params"]:
                params[p.get("name", "")] = p.get("value") or ""
        elif post.get("text"):
            qs = parse_qs(post["text"], keep_blank_values=True)
            params = {k: (v[0] if v else "") for k, v in qs.items()}
        tag = params.get("tag") or ""
        resp = ((e.get("response") or {}).get("content") or {}).get("text") or ""
        ob = ""
        try:
            j = json.loads(resp)
            ob = j.get("ob") or ""
        except Exception:
            pass
        if not ob or not tag:
            continue
        segs = decode_ob(ob, tag)
        interesting = []
        for seg in segs:
            if "_px" in seg or "px3" in seg.lower() or "captcha" in seg.lower() or "bake" in seg.lower():
                interesting.append(seg[:240])
            elif re.search(r"[0-9a-f-]{36}", seg) and ("31536000" in seg or seg.count("|") >= 2):
                interesting.append(seg[:180])
        print(f"\n#{i} {url[-50:]} xor={tag_xor_key(tag)} segs={len(segs)}")
        print("  first_seg", segs[0][:120].replace("\n", " ") if segs else "")
        for s in interesting[:8]:
            print("  *", s.replace("\n", " ")[:200])
            if "_px3" in s or "px3" in s:
                found_px3 += 1
        # payload xor50 预览
        pl = params.get("payload") or ""
        if pl:
            try:
                dec = decode_payload_form(params)
                if dec.get("ok"):
                    print("  payload_decode sts=%s len=%s" % (dec.get("sts"), dec.get("len")))
                    print("  preview", (dec.get("preview") or "")[:120])
                else:
                    prev = xor50_preview(pl)
                    print("  xor50_preview", prev[:120], "| decode_err:", dec.get("error"))
            except Exception as exc:
                print("  payload skip", exc)
    print(f"\n_px3-like segs in ob: {found_px3}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
