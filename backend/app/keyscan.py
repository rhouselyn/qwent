# -*- coding: utf-8 -*-
"""从运行中的微信进程内存直接提取数据库主密钥(无需退出微信)。

原理(参考 WeChatDataAnalysis key_v4 的锚点思路,纯 Python 重写,不加 yara 依赖):
微信 4.x 在内存中维护一段密钥描述结构,布局为
    [8B 指向密钥的指针][10B 0][u64=32][u64=47]
其中 u64=32 是密钥长度。扫描所有已提交的私有内存页定位该锚点,
读出指针处的 32 字节候选密钥,再用 ChatTrace 的 SQLCipher4 HMAC 校验
(PBKDF2-SHA512 256000 轮 -> mac_key -> 页 HMAC)确认,命中后写入
ChatTrace 的 DPAPI 密钥库,后续解密/实时同步流程零改动。

扫描阶段 A:MMKV 配置文件中的 64 位 hex 字符串候选(秒级)。
扫描阶段 B:定位 Weixin.dll 中内置的"内部密钥"(静态模式扫描,秒级)。
扫描阶段 C:全进程内存锚点扫描,候选与内部密钥 XOR 后再做 PBKDF2-HMAC
校验(锚点收集约 5-30 秒,校验视候选数 1~3 分钟)。

注意:内存里存放的不是主密钥本身,而是 raw = 主密钥 XOR 内部密钥 的形态;
所以必须先从 Weixin.dll 提取内部密钥再异或还原(参考 WeChatDataAnalysis
dll_key_scan 的思路,纯 Python 重写)。
"""
from __future__ import annotations

import ctypes
import json
import re
import struct
import threading
import time
from pathlib import Path

import psutil

ANCHOR = b"\x20\x00\x00\x00\x00\x00\x00\x00\x2f\x00\x00\x00\x00\x00\x00\x00"
KEY_LEN = 32
# Weixin.dll 代码段里内部密钥的指令模式:连续 4 条 mov rdx, imm64 + test rax,rax
DLL_PATTERN = re.compile(
    b"\x48\xBA(.{8})" b".{3,8}?" b"\x48\xBA(.{8})" b".{3,8}?"
    b"\x48\xBA(.{8})" b".{3,8}?" b"\x48\xBA(.{8})" b".{3,8}?"
    b"\x48\x85\xC0", re.DOTALL)
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
READABLE_PROTECTS = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
READ_CHUNK = 4 * 1024 * 1024
MAX_REGION = 1024 ** 3
HEX64_RE = re.compile(rb"[0-9a-fA-F]{64}")

_scan_lock = threading.Lock()


def is_windows() -> bool:
    return ctypes.sizeof(ctypes.c_void_p) == 8 and __import__("os").name == "nt"


def _k32():
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.ReadProcessMemory.restype = ctypes.c_bool
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    return k32


def _open_process(pid: int):
    return _k32().OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)


def _read_mem(handle, addr: int, size: int) -> bytes:
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    if _k32().ReadProcessMemory(ctypes.c_void_p(handle), ctypes.c_void_p(addr),
                                buf, size, ctypes.byref(read)) and read.value:
        return buf.raw[:read.value]
    return b""


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64),
        ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", ctypes.c_uint32),
        ("_align1", ctypes.c_uint32),
        ("RegionSize", ctypes.c_uint64),
        ("State", ctypes.c_uint32),
        ("Protect", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("_align2", ctypes.c_uint32),
    ]


def _regions(handle) -> list[tuple[int, int]]:
    k32 = _k32()
    out: list[tuple[int, int]] = []
    addr, mbi = 0, _MBI()
    while addr < 0x7FFFFFFEFFFF:
        if not k32.VirtualQueryEx(ctypes.c_void_p(handle), ctypes.c_void_p(addr),
                                  ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        if (mbi.State == MEM_COMMIT
                and mbi.Type in (MEM_PRIVATE, 0x40000)
                and mbi.Protect in READABLE_PROTECTS
                and 4096 <= mbi.RegionSize <= MAX_REGION):
            out.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return out


def weixin_pids() -> list[int]:
    try:
        return [p.pid for p in psutil.process_iter(["name"])
                if (p.info["name"] or "").lower() in ("weixin.exe", "wechat.exe")]
    except Exception:
        return []


def _is_plausible_key(k: bytes) -> bool:
    if len(k) != KEY_LEN or len(set(k)) < 15:
        return False
    return sum(32 <= b <= 126 for b in k) <= 24


def collect_mmkv_hex_candidates(account_dir: Path) -> list[bytes]:
    mmkv_dir = Path(account_dir) / "db_storage" / "MMKV"
    if not mmkv_dir.is_dir():
        return []
    out: list[bytes] = []
    seen: set[bytes] = set()
    for p in sorted(mmkv_dir.iterdir()):
        try:
            if not p.is_file() or p.stat().st_size > 8 * 1024 * 1024:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        for m in HEX64_RE.finditer(data):
            try:
                raw = bytes.fromhex(m.group().decode())
            except ValueError:
                continue
            if raw not in seen:
                seen.add(raw)
                out.append(raw)
    return out


def find_weixin_dll() -> Path | None:
    """定位本机 Weixin.dll(版本子目录里)。"""
    try:
        from chattrace.keyagent import wechat_state
        exe = wechat_state.find_weixin_exe()
        if exe is not None:
            base = exe.parent
            cands = sorted(base.glob("Weixin.dll")) + sorted(base.glob("*/Weixin.dll"))
            if cands:
                return max(cands, key=lambda p: p.stat().st_size)
    except Exception:
        pass
    import glob as _glob
    hits = _glob.glob(r"C:\Program Files\Tencent\Weixin\**\Weixin.dll", recursive=True)
    return max((Path(h) for h in hits), key=lambda p: p.stat().st_size) if hits else None


def dll_internal_keys(dll_path: Path | None = None) -> list[bytes]:
    """从 Weixin.dll 静态提取内置的 32 字节内部密钥(用于 XOR 还原主密钥)。"""
    dll = dll_path or find_weixin_dll()
    if not dll or not dll.is_file():
        return []
    out: list[bytes] = []
    seen: set[bytes] = set()
    try:
        data = dll.read_bytes()
    except OSError:
        return []
    for m in DLL_PATTERN.finditer(data):
        k = m.group(1) + m.group(2) + m.group(3) + m.group(4)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def collect_anchor_candidates(pids: list[int]) -> list[bytes]:
    """锚点扫描:定位密钥描述结构,读出指针处的 32 字节候选。"""
    k32 = _k32()
    out: list[bytes] = []
    seen: set[bytes] = set()
    for pid in pids:
        handle = _open_process(pid)
        if not handle:
            continue
        try:
            for base, size in _regions(handle):
                data = b""
                off = 0
                while off < size:
                    n = min(READ_CHUNK, size - off)
                    chunk = _read_mem(handle, base + off, n)
                    data += chunk if chunk else b"\x00" * n
                    off += n
                pos = data.find(ANCHOR)
                while pos != -1:
                    if pos >= 16 and data[pos - 10:pos] == b"\x00" * 10:
                        ptr = struct.unpack_from("<Q", data, pos - 16)[0]
                        if 0x10000 < ptr < 0x7FFFFFFEFFFF:
                            key = _read_mem(handle, ptr, KEY_LEN)
                            if key and key not in seen:
                                seen.add(key)
                                out.append(key)
                    pos = data.find(ANCHOR, pos + 1)
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    return out


def validate_candidates(candidates, db_header,
                        internal_keys: list[bytes] | None = None) -> bytes | None:
    """用 ChatTrace 的 SQLCipher4 HMAC 校验筛选候选,返回命中的主密钥。

    内存中的形态是 raw = 主密钥 XOR 内部密钥,所以每个候选先试原样,
    再试与各内部密钥异或后的结果。
    """
    from chattrace.keyagent.verify import verify_hmac_sweep

    ik_list = [k for k in (internal_keys or []) if len(k) == KEY_LEN]
    seen: set[bytes] = set()
    for key in candidates:
        if not _is_plausible_key(key):
            continue
        variants = [key] + [bytes(a ^ b for a, b in zip(key, ik)) for ik in ik_list]
        for v in variants:
            if v in seen:
                continue
            seen.add(v)
            try:
                ok, _ = verify_hmac_sweep(v, db_header)
            except Exception:
                continue
            if ok:
                return v
    return None


def scan_master_key(account_dir: str | Path, message_db: str | Path) -> tuple[bytes | None, str]:
    """扫描运行中的微信,返回 (32B主密钥|None, 过程说明)。"""
    if not is_windows():
        return None, "仅支持 Windows"
    pids = weixin_pids()
    if not pids:
        return None, "微信未运行,无法内存扫描"
    from chattrace.keyagent.verify import read_db_header

    try:
        header = read_db_header(Path(message_db))
    except Exception as exc:
        return None, f"无法读取消息库: {exc}"

    notes = []
    ik = dll_internal_keys()
    notes.append(f"DLL 内部密钥 {len(ik)}")
    mkv = collect_mmkv_hex_candidates(Path(account_dir))
    notes.append(f"MMKV 候选 {len(mkv)}")
    hit = validate_candidates(mkv, header, ik)
    if hit:
        return hit, "; ".join(notes) + " (MMKV 命中)"
    anchors = collect_anchor_candidates(pids)
    notes.append(f"内存锚点候选 {len(anchors)}")
    hit = validate_candidates(anchors, header, ik)
    if hit:
        return hit, "; ".join(notes) + " (内存锚点命中)"
    return None, "; ".join(notes) + " (未命中)"


def store_key(account_dir: str | Path, key: bytes, wechat_version: str = "memory-scan") -> Path:
    """把主密钥写入 ChatTrace 的 DPAPI 密钥库,供解密/实时同步直接使用。"""
    from chattrace.keyagent.account import resolve_account
    from chattrace.keyagent import keystore
    from chattrace.models import KeyInfo

    account = resolve_account(Path(account_dir))
    info = KeyInfo(
        account_id=account.account_id,
        wechat_version=wechat_version,
        password=key,
        source="memory-scan",
        verified=True,
    )
    return keystore.store_key(info)


def capture_and_store(account_dir: str | Path, message_db: str | Path) -> dict:
    """扫描 + 校验 + 入库一步完成。返回 {ok, key_hex, detail, stored_at}。"""
    with _scan_lock:
        key, detail = scan_master_key(account_dir, message_db)
        if not key:
            return {"ok": False, "detail": detail}
        target = store_key(account_dir, key)
        return {
            "ok": True,
            "key_hex": key.hex(),
            "detail": detail,
            "stored_at": str(target),
            "captured_at": time.time(),
        }


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "refs" / "ChatTrace" / "src"))
    ap = argparse.ArgumentParser(description="扫描运行中的微信进程提取数据库密钥")
    ap.add_argument("--account-dir", required=True)
    ap.add_argument("--db", default=None, help="消息库路径,默认 account_dir/db_storage/message/message_0.db")
    args = ap.parse_args()
    acc = Path(args.account_dir)
    db = Path(args.db) if args.db else acc / "db_storage" / "message" / "message_0.db"
    t0 = time.time()
    result = capture_and_store(acc, db)
    if result["ok"]:
        print(f"[+] 命中并已入库 ({time.time()-t0:.0f}s): {result['detail']}")
        print(f"    -> {result['stored_at']}")
    else:
        print(f"[-] 失败 ({time.time()-t0:.0f}s): {result['detail']}")
        sys.exit(1)
