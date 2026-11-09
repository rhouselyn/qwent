# -*- coding: utf-8 -*-
"""增量包 / 全量基线的编解码：zstd + AES-GCM + 分片校验。

传输与静态存储都加密；密钥只存两端，永不随包上传、不写日志。
每个 delta 序号对应 delta/NNNNNNNN.bin + 同名 .json（明文 manifest，不含数据），
云端按序号幂等应用，天然支持断点续传与乱序拒绝。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import zstandard as zstd
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"QW1"
_TAG = "__qwent_bytes__"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _enc(key: bytes, raw: bytes) -> bytes:
    comp = zstd.ZstdCompressor(level=3).compress(raw)
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, comp, None)


def _dec(key: bytes, blob: bytes) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("增量包魔数不匹配（文件损坏或密钥错误）")
    comp = AESGCM(key).decrypt(blob[3:15], blob[15:], None)
    return zstd.ZstdDecompressor().decompress(comp)


def _encode(o):
    if isinstance(o, bytes):
        return {_TAG: base64.b64encode(o).decode("ascii")}
    if isinstance(o, dict):
        return {k: _encode(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_encode(x) for x in o]
    return o


def _decode(o):
    if isinstance(o, dict):
        if _TAG in o and len(o) == 1:
            return base64.b64decode(o[_TAG])
        return {k: _decode(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_decode(x) for x in o]
    return o


def delta_name(seq: int, ext: str = "bin") -> str:
    return f"delta/{seq:08d}.{ext}"


def pack_delta(seq: int, changes: list[dict], lsn_from: int, lsn_to: int,
               schema_sig: str, key: bytes) -> tuple[bytes, dict]:
    payload = {"v": 1, "seq": seq, "lsn_from": lsn_from, "lsn_to": lsn_to,
               "schema_sig": schema_sig, "created": int(time.time()),
               "changes": _encode(changes)}
    blob = _enc(key, json.dumps(payload, ensure_ascii=False,
                                separators=(",", ":")).encode("utf-8"))
    manifest = {"type": "delta", "seq": seq, "prev_seq": seq - 1,
                "lsn_from": lsn_from, "lsn_to": lsn_to, "rows": len(changes),
                "schema_sig": schema_sig, "created": int(time.time()),
                "file": delta_name(seq), "sha256": sha256(blob),
                "size": len(blob)}
    return blob, manifest


def unpack_delta(blob: bytes, key: bytes, expect_sha256: str = "") -> dict:
    if expect_sha256 and sha256(blob) != expect_sha256:
        raise ValueError("增量包 sha256 校验失败")
    payload = json.loads(_dec(key, blob).decode("utf-8"))
    if payload.get("v") != 1:
        raise ValueError("不支持的增量包版本")
    payload["changes"] = _decode(payload.get("changes") or [])
    return payload


def pack_baseline(db_path: Path, out_dir: Path, key: bytes,
                  chunk_mb: int = 64, progress=None) -> dict:
    """整库基线：sqlite3.backup() 一致性快照 → 分块 zstd + AES-GCM。

    不用 sqlite3.Connection.backup 直接写压缩流，是因为加密分片需要块边界；
    先落到临时文件再分块，磁盘峰值约 4.25GB（与主库同盘，需预留）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "base").mkdir(parents=True, exist_ok=True)
    snap = out_dir / "baseline.snapshot.db"
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(snap))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    size = snap.stat().st_size
    parts: list[dict] = []
    chunk = max(4, int(chunk_mb)) * 1024 * 1024
    with open(snap, "rb") as f:
        idx = 0
        while True:
            raw = f.read(chunk)
            if not raw:
                break
            blob = _enc(key, raw)
            name = f"base/{idx:05d}.part"
            (out_dir / name).write_bytes(blob)
            parts.append({"name": name, "sha256": sha256(blob),
                          "size": len(blob), "raw_size": len(raw)})
            idx += 1
            if progress:
                progress(idx, len(raw))
    snap.unlink(missing_ok=True)
    manifest = {"type": "baseline", "db_size": size, "parts": parts,
                "chunk_mb": chunk_mb, "created": int(time.time())}
    (out_dir / "baseline.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def restore_baseline(parts_dir: Path, out_db: Path, key: bytes,
                     manifest: dict) -> dict:
    """按分片解密还原 knowledge.db，并做大小与 quick_check 校验。"""
    out_db.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_db.with_suffix(".restoring")
    total = 0
    with open(tmp, "wb") as o:
        for p in manifest["parts"]:
            fp = parts_dir / p["name"]
            blob = fp.read_bytes()
            if sha256(blob) != p["sha256"]:
                raise ValueError(f"基线分片校验失败：{p['name']}")
            raw = _dec(key, blob)
            o.write(raw)
            total += len(raw)
    if total != int(manifest["db_size"]):
        raise ValueError(f"基线还原大小不符：{total} != {manifest['db_size']}")
    con = sqlite3.connect(str(tmp))
    try:
        result = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    if str(result).lower() != "ok":
        raise ValueError(f"基线 SQLite 校验失败：{result}")
    tmp.replace(out_db)
    return {"db_size": total, "parts": len(manifest["parts"]), "check": result}


def export_tables(con: sqlite3.Connection, tables: list[str]) -> dict:
    """基线恢复前导出云端独立会话，恢复后导回（D6：不拆库、不覆盖会话）。"""
    out = {}
    for t in tables:
        try:
            cur = con.execute(f'SELECT * FROM "{t}"')
            out[t] = {"cols": [d[0] for d in cur.description],
                      "rows": [list(r) for r in cur.fetchall()]}
        except sqlite3.OperationalError:
            out[t] = {"cols": [], "rows": []}
    return out


def import_tables(con: sqlite3.Connection, dump: dict) -> int:
    n = 0
    for t, d in dump.items():
        cols = d.get("cols") or []
        if not cols:
            continue
        ph = ",".join("?" for _ in cols)
        collist = ",".join(f'"{c}"' for c in cols)
        con.execute(f'DELETE FROM "{t}"')
        con.executemany(f'INSERT OR REPLACE INTO "{t}" ({collist}) VALUES ({ph})',
                        d.get("rows") or [])
        n += len(d.get("rows") or [])
    con.commit()
    return n
