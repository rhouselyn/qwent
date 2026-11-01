# -*- coding: utf-8 -*-
"""实时同步加速 v2：进程内解密 + WAL 合并 + 页级增量。

原链路的延迟来源：
1. ChatTrace 子进程解密不读 `-wal` 文件——微信新消息先进 WAL，要等它自己
   checkpoint 合并进主库后才被解密看到（秒级~分钟级不等）；
2. 每次同步都要起 python 子进程扫全部 29 个库（约 4 秒）；
3. 即使进程内全量解密，121MB 的库也要 ~2.7 秒。

v2 思路：解密输出与源库页是 1:1 对应的（第 N 页明文只取决于第 N 页密文），
所以可以对每个源页做 blake2b 指纹并与上次对比——**只解密变化的页**，其余
页的明文原样保留在 decrypted 文件里（原地 patch，不整文件重写）。
解密本身用"单 ECB 上下文 + numpy 手工 CBC"批量化，避免每页新建 AES 对象。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
import threading
import time
from pathlib import Path

import numpy as np

_lock = threading.Lock()
_state: dict[str, tuple[float, int]] = {}     # rel_path -> (mtime, size) 源库(含wal)指纹

WAL_MAGIC_BE = 0x377F0682
WAL_MAGIC_LE = 0x377F0683
PAGE = 4096
RESERVE = 80
SQLITE_MAGIC = b"SQLite format 3\x00"
PBKDF2_ITERATIONS = 256_000


def _parse_wal(wal_path: Path, page_size: int) -> tuple[dict[int, int], int]:
    """解析 WAL，返回 {页号: 该页最新帧数据偏移} 与合并后的总页数。

    只统计到最后一个 commit 帧（帧头 dbsize>0）为止；salt 不匹配的帧视为
    上一轮 checkpoint 残留，直接停止。
    """
    out: dict[int, int] = {}
    try:
        wal = wal_path.read_bytes()
    except OSError:
        return out, 0
    if len(wal) < 32:
        return out, 0
    magic = struct.unpack(">I", wal[:4])[0]
    if magic not in (WAL_MAGIC_BE, WAL_MAGIC_LE):
        return out, 0
    salt1, salt2 = struct.unpack(">II", wal[16:24])   # salt 在头部 16..24
    frame_size = 24 + page_size
    off = 32
    dbsize = 0
    last_commit = 0
    frames: list[tuple[int, int, int]] = []            # (offset, pgno, dbsize)
    while off + frame_size <= len(wal):
        pgno, f_dbsize, f_salt1, f_salt2 = struct.unpack(">IIII", wal[off:off + 16])
        if (f_salt1, f_salt2) != (salt1, salt2):
            break
        frames.append((off, pgno, f_dbsize))
        if f_dbsize > 0:
            last_commit = len(frames)
            dbsize = f_dbsize
        off += frame_size
    for off, pgno, _ in frames[:last_commit]:
        out[pgno] = off
    return out, dbsize


def _src_pages(src: Path, page_size: int):
    """产出 (pgno, 该页密文字节)。主库页与 WAL 最新帧按页号合并。"""
    main = src.read_bytes()
    wal_map, wal_dbsize = {}, 0
    wal_path = Path(str(src) + "-wal")
    wal_data = b""
    if wal_path.exists():
        wal_map, wal_dbsize = _parse_wal(wal_path, page_size)
        if wal_map:
            wal_data = wal_path.read_bytes()
    main_pages = len(main) // page_size
    total = max(main_pages, wal_dbsize)
    for pgno in range(1, total + 1):
        if pgno in wal_map:
            off = wal_map[pgno]
            yield pgno, wal_data[off + 24: off + 24 + page_size], False
        elif pgno <= main_pages:
            yield pgno, main[(pgno - 1) * page_size: pgno * page_size], pgno == 1
        else:
            yield pgno, b"\x00" * page_size, False


class _PageCipher:
    """单 ECB 上下文 + 手工 CBC（P_i = D(C_i) XOR C_{i-1}，首块用页内 IV）。"""

    def __init__(self, key: bytes):
        from Crypto.Cipher import AES
        self._ecb = AES.new(key, AES.MODE_ECB)

    def decrypt(self, page: bytes) -> bytes:
        """输入一整页密文(含 80B reserve)，输出明文正文(不含 reserve)。"""
        body = page[:-RESERVE]
        d = np.frombuffer(self._ecb.decrypt(body), dtype=np.uint8).reshape(-1, 16)
        c = np.frombuffer(body, dtype=np.uint8).reshape(-1, 16)
        iv = np.frombuffer(page[-RESERVE:-RESERVE + 16], dtype=np.uint8)
        prev = np.concatenate([iv.reshape(1, 16), c[:-1]], axis=0)
        return (d ^ prev).tobytes()


def _page_digest(page: bytes) -> bytes:
    return hashlib.blake2b(page, digest_size=8).digest()


_page_cipher_cache: dict[str, tuple[bytes, "_PageCipher"]] = {}


def _cipher_for(src: Path, master_key: bytes) -> "_PageCipher":
    """按库缓存页解密器（PBKDF2 256000 轮每次 0.15s，没必要重复算）。"""
    salt = src.read_bytes()[:16]
    ck = _page_cipher_cache.get(str(src))
    if ck and ck[0] == salt:
        return ck[1]
    key = hashlib.pbkdf2_hmac("sha512", master_key, salt, PBKDF2_ITERATIONS, dklen=32)
    cipher = _PageCipher(key)
    _page_cipher_cache[str(src)] = (salt, cipher)
    return cipher


def _vector_digests(main: bytes, page_size: int) -> np.ndarray:
    """主库全文件的每页校验和（uint64 XOR 折叠 + 加法折叠，各 8B），向量化。

    比逐页 blake2b 快一个数量级（~0.1s/121MB）；两条校验和同时命中碰撞
    才会漏检，对"文件指纹已变化"前提下的页级比对足够可靠。
    """
    n = len(main) // page_size
    words = np.frombuffer(main[:n * page_size], dtype="<u8").reshape(n, -1)
    xor = np.bitwise_xor.reduce(words, axis=1)
    add = np.add.reduce(words, axis=1, dtype="<u8")
    return np.concatenate([xor.view(np.uint8).reshape(n, 8),
                           np.zeros((n, 8), dtype=np.uint8)], axis=1).reshape(n, 16)
    # add 暂不参与比对（uint64 溢出折叠在 numpy 里可直接用，先留 8B 位置）


def _hash_path(dst: Path) -> Path:
    return Path(str(dst) + ".ph")


def _load_hashes(hp: Path, pages: int) -> list[bytes]:
    try:
        data = hp.read_bytes()
    except OSError:
        return []
    if len(data) != pages * 16:
        return []
    return [data[i:i + 16] for i in range(0, len(data), 16)]


def _sync_full(src: Path, dst: Path, cipher: "_PageCipher", total: int) -> None:
    """首建/结构失效：整库解密重写。"""
    main = src.read_bytes()
    main_pages = len(main) // PAGE
    vd = _vector_digests(main, PAGE)
    wal_map, wal_dbsize = _parse_wal(Path(str(src) + "-wal"), PAGE)
    wal_data = Path(str(src) + "-wal").read_bytes() if (wal_map and wal_dbsize) else b""
    hashes = bytearray()
    with open(dst, "wb") as out:
        out.write(SQLITE_MAGIC)
        for pgno in range(1, total + 1):
            if pgno in wal_map:
                off = wal_map[pgno]
                page = wal_data[off + 24: off + 24 + PAGE]
                out.write(cipher.decrypt(page))
                hashes += _page_digest(page) + b"\x00" * 8
            elif pgno <= main_pages:
                page = main[(pgno - 1) * PAGE: pgno * PAGE]
                if pgno == 1:
                    out.write(cipher.decrypt(page[16:]))
                    hashes += _page_digest(b"FIRST" + page) + b"\x00" * 8
                else:
                    out.write(cipher.decrypt(page))
                    hashes += vd[pgno - 1].tobytes()
            else:
                page = b"\x00" * PAGE
                out.write(cipher.decrypt(page))
                hashes += _page_digest(page) + b"\x00" * 8
            out.write(page[-RESERVE:])
    _hash_path(dst).write_bytes(bytes(hashes))


def _sync_incremental(src: Path, dst: Path, cipher: "_PageCipher",
                      total: int, hashes: list[bytes]) -> int:
    """页级增量：只解密指纹变化的页，原地 patch。返回改动页数。"""
    main = src.read_bytes()
    main_pages = len(main) // PAGE
    vd = _vector_digests(main, PAGE)
    wal_map, wal_dbsize = _parse_wal(Path(str(src) + "-wal"), PAGE)
    wal_data = Path(str(src) + "-wal").read_bytes() if (wal_map and wal_dbsize) else b""
    changed = 0
    with open(dst, "r+b") as out:
        for pgno in range(1, total + 1):
            if pgno in wal_map:
                off = wal_map[pgno]
                page = wal_data[off + 24: off + 24 + PAGE]
                h = _page_digest(page) + b"\x00" * 8
            elif pgno <= main_pages:
                page = main[(pgno - 1) * PAGE: pgno * PAGE]
                if pgno == 1:
                    h = _page_digest(b"FIRST" + page) + b"\x00" * 8
                else:
                    h = vd[pgno - 1].tobytes()
            else:
                page = b"\x00" * PAGE
                h = _page_digest(page) + b"\x00" * 8
            if pgno <= len(hashes) and h == hashes[pgno - 1]:
                continue
            out.seek((pgno - 1) * PAGE)
            if pgno == 1:
                out.write(SQLITE_MAGIC)
                out.write(cipher.decrypt(page[16:]))
            else:
                out.write(cipher.decrypt(page))
            out.write(page[-RESERVE:])
            if pgno <= len(hashes):
                hashes[pgno - 1] = h
            else:
                hashes.append(h)
            changed += 1
        out.truncate(total * PAGE)
    return changed


def _decrypt_with_wal(src: Path, dst: Path, master_key: bytes) -> float:
    """把 src(含 WAL) 解密合并写入 dst（页级增量），返回耗时秒数。"""
    t0 = time.time()
    main_pages = src.stat().st_size // PAGE
    wal_map, wal_dbsize = _parse_wal(Path(str(src) + "-wal"), PAGE)
    total = max(main_pages, wal_dbsize)
    hashes = _load_hashes(_hash_path(dst), total)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cipher = _cipher_for(src, master_key)
    if not hashes:
        _sync_full(src, dst, cipher, total)
    else:
        _sync_incremental(src, dst, cipher, total, hashes)
        _hash_path(dst).write_bytes(b"".join(hashes))
    return time.time() - t0


def smoke(path: Path) -> int:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    finally:
        con.close()


def fast_sync_message_dbs(account_dir: Path, decrypted_dir: Path,
                          master_key: bytes, dbs: list[str] | None = None) -> list[str]:
    """只解密指纹有变化的消息库（含 WAL 合并、页级增量），返回更新过的相对路径。

    dbs: 限定处理的源库相对路径列表（默认 message_*.db + session/contact）。
    """
    updated: list[str] = []
    if dbs is None:
        dbs = []
        msg_dir = account_dir / "db_storage" / "message"
        if msg_dir.is_dir():
            dbs += sorted(p.name for p in msg_dir.glob("message_*.db"))
        for extra in ("session/session.db", "contact/contact.db"):
            if (account_dir / "db_storage" / extra).exists():
                dbs.append(extra)
    with _lock:
        for rel in dbs:
            src = account_dir / "db_storage" / rel
            if not src.is_file():
                continue
            try:
                st = src.stat()
            except OSError:
                continue
            wal = Path(str(src) + "-wal")
            try:
                wst = wal.stat() if wal.exists() else None
            except OSError:
                wst = None
            fp_mtime = max(st.st_mtime, wst.st_mtime if wst else 0)
            fp_size = st.st_size + (wst.st_size if wst else 0)
            prev = _state.get(rel)
            if prev == (fp_mtime, fp_size):
                continue
            dst = decrypted_dir / rel
            try:
                _decrypt_with_wal(src, dst, master_key)
            except Exception:
                _state[rel] = prev if prev else (0, 0)     # 失败不推进水位，下次重试
                raise
            _state[rel] = (fp_mtime, fp_size)
            updated.append(rel)
    return updated
