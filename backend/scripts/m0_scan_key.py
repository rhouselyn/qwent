# -*- coding: utf-8 -*-
"""M0-① 密钥提取：从运行中的微信进程内存 / MMKV 配置中定位数据库密钥。

原理：微信 4.x 用 SQLCipher v4 加密本地库。page1 的 salt 后是 AES-CBC 密文，
明文页 1 以标准 SQLite 头 "SQLite format 3\\x00" 开始。由于 P0 = AES_Dec(C0) XOR IV，
对候选密钥只需做一次单块 AES 解密再与固定值比对，开销 ~10us/候选，
可以对全量进程内存做滑窗扫描。

先扫 MMKV（磁盘、秒级），再扫进程内存（多进程并行，约 1-5 分钟）。

用法：
  python m0_scan_key.py --account-dir "C:/Users/14815/xwechat_files/wxid_xxx_965a" \
      [--db message/message_0.db] [--out ../../data/keys.json] [--step 8]
"""
import argparse
import ctypes
import json
import os
import re
import sys
import time
import ctypes.wintypes as wt
from multiprocessing import Process, Queue

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m0lib import (RESERVE_CANDIDATES, HEADER_FINGERPRINT,  # noqa: E402
                   check_raw_key, load_page1)

import psutil  # noqa: E402
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
READABLE_PROTECTS = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
READ_CHUNK = 4 * 1024 * 1024


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64),
        ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD),
        ("_align1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("_align2", wt.DWORD),
    ]


def _k32():
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.ReadProcessMemory.restype = ctypes.c_bool
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    return k32


def open_process(pid: int):
    return _k32().OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)


def enumerate_regions(pid: int):
    k32 = _k32()
    h = open_process(pid)
    if not h:
        return []
    regions, addr = [], 0
    mbi = MBI()
    while addr < 0x7FFFFFFEFFFF:
        if not k32.VirtualQueryEx(ctypes.c_void_p(h), ctypes.c_void_p(addr),
                                  ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        if (mbi.State == MEM_COMMIT
                and mbi.Type in (MEM_PRIVATE, 0x40000)  # 私有 + 映射节
                and mbi.Protect in READABLE_PROTECTS and mbi.RegionSize >= 4096):
            regions.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    k32.CloseHandle(ctypes.c_void_p(h))
    return regions


def read_region(pid: int, base: int, size: int) -> bytes:
    k32 = _k32()
    h = open_process(pid)
    if not h:
        return b""
    buf = bytearray()
    read = ctypes.c_size_t(0)
    off = 0
    while off < size:
        n = min(READ_CHUNK, size - off)
        chunk = ctypes.create_string_buffer(n)
        if k32.ReadProcessMemory(ctypes.c_void_p(h), ctypes.c_void_p(base + off),
                                 chunk, n, ctypes.byref(read)) and read.value:
            buf += chunk.raw[:read.value]
        else:
            buf += b"\x00" * n
        off += n
    k32.CloseHandle(ctypes.c_void_p(h))
    return bytes(buf)


def make_checker(page1: bytes):
    """返回 (checker, ivs)。checker(key32) 做一次单块 AES 解密 + 指纹比对。

    P0 = AES_Dec(C0) XOR IV。注意：SQLCipher 把文件头 16 字节魔数替换为盐值，
    解密后的首块是 SQLite 头偏移 16..31（page_size=0x1000 + 0x40/0x20/0x20 常量位），
    不是 "SQLite format 3" 字符串。"""
    enc_head = page1[16:32]
    ivs = {r: page1[4096 - r:4096 - r + 16] for r in RESERVE_CANDIDATES}
    conds = [(r, [(p, fp ^ iv[p]) for p, fp in HEADER_FINGERPRINT.items()])
             for r, iv in ivs.items()]

    def checker(key32: bytes):
        try:
            dec = Cipher(algorithms.AES(key32), modes.ECB()).decryptor()
            raw = dec.update(enc_head)
        except Exception:
            return None
        for r, cond in conds:
            if all(raw[p] == e for p, e in cond):
                return r
        return None

    return checker, ivs


def scan_buffer(buf: bytes, checker, step: int):
    for o in range(0, len(buf) - 32, step):
        r = checker(buf[o:o + 32])
        if r:
            return buf[o:o + 32].hex(), r
    return None


def _worker(tasks, page1: bytes, step: int, out_q):
    checker, _ = make_checker(page1)
    try:
        for pid, base, size in tasks:
            data = read_region(pid, base, size)
            if not data:
                continue
            hit = scan_buffer(data, checker, step)
            if hit:
                out_q.put(("mem", f"pid={pid} base=0x{base:X}", hit[0], hit[1]))
                return
    except Exception:
        pass


def collect_mmkv_candidates(account_dir: str):
    cands, hex_cands = set(), set()
    mmkv_dir = os.path.join(account_dir, "db_storage", "MMKV")
    if not os.path.isdir(mmkv_dir):
        return cands, hex_cands
    hex_re = re.compile(rb"[0-9a-fA-F]{64}")
    for name in os.listdir(mmkv_dir):
        p = os.path.join(mmkv_dir, name)
        if not os.path.isfile(p) or os.path.getsize(p) > 8 * 1024 * 1024:
            continue
        with open(p, "rb") as f:
            data = f.read()
        for i in range(0, max(0, len(data) - 32)):
            cands.add(data[i:i + 32])
        for m in hex_re.finditer(data):
            try:
                hex_cands.add(bytes.fromhex(m.group().decode()))
            except ValueError:
                pass
    return cands, hex_cands


def benchmark(checker) -> float:
    """用本进程一段内存测单候选校验耗时，返回微秒/候选。"""
    import time as _t
    buf = os.urandom(256 * 1024)
    t0 = _t.perf_counter()
    scan_buffer(buf, checker, 8)
    return (_t.perf_counter() - t0) / (256 * 1024 / 8) * 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-dir", required=True)
    ap.add_argument("--db", default="db_storage/message/message_0.db")
    ap.add_argument("--out", default="../../data/keys.json")
    ap.add_argument("--step", type=int, default=8)
    args = ap.parse_args()

    target = os.path.join(args.account_dir, args.db)
    page1 = load_page1(target)
    checker, _ = make_checker(page1)
    print(f"[*] 目标库: {target}")
    print(f"[*] salt={page1[:16].hex()}  reserve 候选={list(RESERVE_CANDIDATES)}")

    # ---- 阶段 A：MMKV 磁盘候选（raw 模型，秒级）----
    print("[*] 阶段 A：扫描 MMKV 配置文件...")
    raw_cands, hex_cands = collect_mmkv_candidates(args.account_dir)
    print(f"    原始候选 {len(raw_cands)}, hex 候选 {len(hex_cands)}")
    for c in list(raw_cands) + list(hex_cands):
        r = next((rr for rr in RESERVE_CANDIDATES if check_raw_key(c, page1, rr)), None)
        if r:
            _report("mmkv", "MMKV", c.hex(), r, args)
            _verify_model(c, args.account_dir)
            return
    print("    阶段 A 未命中（正常，密钥大概率只在内存中）")

    # ---- 阶段 B：进程内存扫描 ----
    pids = [p.pid for p in psutil.process_iter(["name"])
            if (p.info["name"] or "").lower() == "weixin.exe"]
    if not pids:
        print("[!] 未找到运行中的 Weixin.exe，请先登录微信")
        sys.exit(1)
    tasks = []
    total_mb = 0.0
    for pid in pids:
        for b, s in enumerate_regions(pid):
            if s <= 1024 ** 3:
                tasks.append((pid, b, s))
                total_mb += s / 1048576
    print(f"[*] 阶段 B：扫描 {len(pids)} 个微信进程，共 {total_mb:.0f} MB 可扫内存")
    rate = benchmark(checker)
    est = total_mb * 1048576 / args.step * rate / 1e6 / max(2, os.cpu_count() or 4)
    print(f"    校验速率 {rate:.1f} us/候选，预计 {est:.1f} 分钟（{os.cpu_count()} 核并行）")

    out_q = Queue()
    n_workers = max(2, min(12, os.cpu_count() or 4))
    buckets = [[] for _ in range(n_workers)]
    for i, t in enumerate(sorted(tasks, key=lambda x: -x[2])):
        buckets[i % n_workers].append(t)
    workers = []
    for b in buckets:
        if b:
            workers.append(Process(target=_worker, args=(b, page1, args.step, out_q)))
    t0 = time.time()
    for w in workers:
        w.start()

    found = None
    while found is None and any(w.is_alive() for w in workers):
        try:
            found = out_q.get(timeout=1.0)
        except Exception:
            pass
    for w in workers:
        if w.is_alive():
            w.terminate()
    for w in workers:
        w.join(timeout=2)

    if found:
        kind, src, key_hex, reserve = found
        print(f"[+] 命中! 位置={src} reserve={reserve} 耗时 {time.time()-t0:.0f}s")
        _report(kind, src, key_hex, reserve, args)
        _verify_model(bytes.fromhex(key_hex), args.account_dir)
    else:
        print(f"[-] 阶段 B 未命中（耗时 {time.time()-t0:.0f}s）。")
        print("    下一步可试: --step 4 或 --step 1；确认微信已登录、主界面聊天记录可见。")


def _report(kind, src, key_hex, reserve, args):
    print(f"[+] 密钥: {key_hex}")
    out = args.out if os.path.isabs(args.out) else \
        os.path.abspath(os.path.join(os.path.dirname(__file__), args.out))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"target_db": args.db, "key_hex": key_hex, "reserve": reserve,
                   "kind": kind, "source": src,
                   "found_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False, indent=2)
    print(f"[+] 已保存 {out}")


def _verify_model(key: bytes, account_dir: str):
    for rel in ["db_storage/contact/contact.db", "db_storage/session/session.db",
                "db_storage/message/message_1.db", "db_storage/message/media_0.db"]:
        p = os.path.join(account_dir, rel)
        if not os.path.exists(p):
            continue
        p1 = load_page1(p)
        for r in RESERVE_CANDIDATES:
            if check_raw_key(key, p1, r):
                print(f"[+] 同一密钥适用于 {rel} (reserve={r}) -> 全库共享同一密钥")
                return
    print("[*] 该密钥不直接适用于其他库 -> 若为 per-db 派生模型，解密其他库需分别提取")


if __name__ == "__main__":
    main()
