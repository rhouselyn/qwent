# -*- coding: utf-8 -*-
"""QQ NT 本地记录导入与查询。

QQ 9.9（NT 架构）把聊天记录放在
  %USERPROFILE%\\Documents\\Tencent Files\\<QQ>\\nt_qq\\nt_db\\*.db
这些库是 SQLCipher 4，文件前 1024 字节是 QQ 自定义头，密钥只存在于正在
运行的 QQ 进程内存里。本模块只读取自己的本机数据：

1. 在 wrapper.node 进程内存中按已知 salt 找到 x'<64hex key><32hex salt>'；
2. PBKDF2-SHA512 派生密钥，AES-256-CBC 解密每个 4096 页；
3. 解析 nt_msg / profile_info / group_info / files_in_chat / file_assistant，
   写入 knowledge.db 的 qq_local_* 表，供 QQ 模式界面和 QQ Agent 使用。

解密在临时目录完成，导入后删除；密钥不落盘。QQ 必须处于登录状态才能取钥。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac as hmac_mod
import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent.parent.parent
DATA = PROJECT / "data"
DB_PATH = DATA / "knowledge.db"
QQ_ROOT = Path(os.path.expandvars(r"%USERPROFILE%\Documents\Tencent Files"))

EXT_HEADER = 1024
PAGE_SIZE = 4096
SALT_SIZE = 16
KEY_SIZE = 32
IV_SIZE = 16
HMAC_SIZE = 20
RESERVE = 48
HMAC_MASK = 0x3A
KDF_ITER = 4000
FAST_ITER = 2

TARGET_DBS = ("nt_msg.db", "profile_info.db", "group_info.db",
              "files_in_chat.db", "file_assistant.db")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS qq_local_message(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         conv TEXT NOT NULL,
         msg_id INTEGER,
         ts INTEGER NOT NULL,
         sender_uid TEXT DEFAULT '',
         sender_uin INTEGER DEFAULT 0,
         sender_name TEXT DEFAULT '',
         is_sender INTEGER DEFAULT 0,
         mtype INTEGER DEFAULT 0,
         ctype INTEGER DEFAULT 0,
         text TEXT DEFAULT '',
         parsed TEXT DEFAULT ''
       )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_qq_local_msgid ON qq_local_message(msg_id)",
    "CREATE INDEX IF NOT EXISTS idx_qq_local_conv_ts ON qq_local_message(conv, ts)",
    "CREATE INDEX IF NOT EXISTS idx_qq_local_ts ON qq_local_message(ts)",
    """CREATE TABLE IF NOT EXISTS qq_chat(
         conv TEXT PRIMARY KEY,
         name TEXT DEFAULT '',
         is_group INTEGER DEFAULT 0,
         uin INTEGER DEFAULT 0,
         uid TEXT DEFAULT '',
         last_ts INTEGER DEFAULT 0,
         cnt INTEGER DEFAULT 0,
         last_text TEXT DEFAULT '',
         last_is_sender INTEGER DEFAULT 0,
         pinned INTEGER DEFAULT 0
       )""",
    """CREATE TABLE IF NOT EXISTS qq_contact(
         key TEXT PRIMARY KEY,
         kind TEXT DEFAULT 'friend',
         uid TEXT DEFAULT '',
         uin INTEGER DEFAULT 0,
         name TEXT DEFAULT '',
         nickname TEXT DEFAULT '',
         conv TEXT DEFAULT '',
         last_ts INTEGER DEFAULT 0,
         cnt INTEGER DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_qq_contact_kind ON qq_contact(kind, name)",
    """CREATE TABLE IF NOT EXISTS qq_file(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         file_key TEXT UNIQUE,
         conv TEXT DEFAULT '',
         msg_id INTEGER DEFAULT 0,
         name TEXT DEFAULT '',
         size INTEGER DEFAULT 0,
         ts INTEGER DEFAULT 0,
         path TEXT DEFAULT '',
         kind TEXT DEFAULT '',
         sender_name TEXT DEFAULT ''
       )""",
    "CREATE INDEX IF NOT EXISTS idx_qq_file_ts ON qq_file(ts)",
    "CREATE INDEX IF NOT EXISTS idx_qq_file_conv ON qq_file(conv, ts)",
    """CREATE TABLE IF NOT EXISTS qq_file_text(
         file_id INTEGER PRIMARY KEY,
         name TEXT DEFAULT '',
         text TEXT DEFAULT '',
         chars INTEGER DEFAULT 0,
         error TEXT DEFAULT '',
         ts INTEGER DEFAULT 0
       )""",
    """CREATE TABLE IF NOT EXISTS qq_link(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         link_key TEXT UNIQUE,
         url TEXT DEFAULT '',
         title TEXT DEFAULT '',
         conv TEXT DEFAULT '',
         msg_id INTEGER DEFAULT 0,
         ts INTEGER DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_qq_link_ts ON qq_link(ts)",
]

READABLE_PROTS = {0x02, 0x04, 0x06, 0x08, 0x20, 0x40, 0x60, 0x80}
MAX_REGION = 256 * 1024 * 1024
HEX_CHARS = set(b"0123456789abcdefABCDEF")
URL_RE = re.compile(r"https?://[^\s<>\"'）)】]+", re.I)


# ---------------------------------------------------------------- Windows API
class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260)]


class _MODULEENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("GlblcntUsage", wt.DWORD),
                ("ProccntUsage", wt.DWORD),
                ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
                ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
                ("szModule", ctypes.c_char * 256), ("szExePath", ctypes.c_char * 260)]


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def find_qq_pid() -> int:
    """找加载了 wrapper.node 的 QQ 主进程。"""
    k32 = _kernel32()
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snap in (-1, ctypes.c_void_p(-1).value):
        return 0
    pe = _PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(pe)
    pids = []
    try:
        if k32.Process32First(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.decode("utf-8", "ignore").lower() == "qq.exe":
                    pids.append(pe.th32ProcessID)
                if not k32.Process32Next(snap, ctypes.byref(pe)):
                    break
    finally:
        k32.CloseHandle(snap)
    for pid in pids:
        snap = k32.CreateToolhelp32Snapshot(0x00000008 | 0x00000010, pid)
        if snap in (-1, ctypes.c_void_p(-1).value, 0):
            continue
        me = _MODULEENTRY32()
        me.dwSize = ctypes.sizeof(me)
        try:
            if k32.Module32First(snap, ctypes.byref(me)):
                while True:
                    if me.szModule.decode("utf-8", "ignore").lower() == "wrapper.node":
                        return pid
                    if not k32.Module32Next(snap, ctypes.byref(me)):
                        break
        finally:
            k32.CloseHandle(snap)
    return pids[0] if pids else 0


def _read_mem(handle, addr: int, size: int) -> bytes | None:
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    k32 = _kernel32()
    if k32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
        return buf.raw[:n.value]
    return None


def _scan_keys(pid: int, salts: list[str]) -> dict[str, str]:
    """按 salt 字符串扫描进程内存，返回 salt -> key_hex。"""
    k32 = _kernel32()
    h = k32.OpenProcess(0x0010 | 0x0400, False, pid)  # VM_READ | QUERY_INFORMATION
    if not h:
        raise RuntimeError(f"无法读取 QQ 进程 {pid}，请确认 QQ 已登录且权限一致")
    needles = [(s.encode("ascii"), s) for s in salts]
    found: dict[str, str] = {}
    mbi = _MBI()
    addr = 0
    try:
        while True:
            if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                      ctypes.sizeof(mbi)):
                break
            base = mbi.BaseAddress or 0
            size = int(mbi.RegionSize)
            if (mbi.State == 0x1000 and (mbi.Protect & 0xFF) in READABLE_PROTS
                    and 0 < size <= MAX_REGION):
                data = _read_mem(h, base, size)
                if data:
                    for needle, salt_hex in needles:
                        if salt_hex in found:
                            continue
                        off = 0
                        while True:
                            idx = data.find(needle, off)
                            if idx < 0:
                                break
                            if (idx >= 66 and idx + 33 <= len(data)
                                    and data[idx - 66:idx - 64] == b"x'"
                                    and data[idx + 32:idx + 33] == b"'"
                                    and all(c in HEX_CHARS for c in data[idx - 64:idx])):
                                found[salt_hex] = data[idx - 64:idx].decode("ascii").lower()
                                break
                            off = idx + 1
            nxt = base + size
            if nxt <= addr or nxt >= 0x7FFFFFFFFFFF:
                break
            addr = nxt
    finally:
        k32.CloseHandle(h)
    return found


# ---------------------------------------------------------------- SQLCipher
def _derive_key(passphrase: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ITER, KEY_SIZE)


def _derive_hmac(enc_key: bytes, salt: bytes) -> bytes:
    hmac_salt = bytes(b ^ HMAC_MASK for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, hmac_salt, FAST_ITER, KEY_SIZE)


def _verify_page1(page1: bytes, enc_key: bytes) -> bool:
    salt = page1[:SALT_SIZE]
    content = page1[SALT_SIZE:PAGE_SIZE - RESERVE]
    iv = page1[PAGE_SIZE - RESERVE:PAGE_SIZE - RESERVE + IV_SIZE]
    stored = page1[PAGE_SIZE - RESERVE + IV_SIZE:
                   PAGE_SIZE - RESERVE + IV_SIZE + HMAC_SIZE]
    calc = hmac_mod.new(_derive_hmac(enc_key, salt), content + iv +
                        struct.pack("<I", 1), hashlib.sha1).digest()
    return hmac_mod.compare_digest(calc, stored)


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return dec.update(data) + dec.finalize()
    except ImportError:
        from Crypto.Cipher import AES
        return AES.new(key, AES.MODE_CBC, iv).decrypt(data)


def decrypt_db(src: Path, dst: Path, enc_key: bytes) -> None:
    raw = src.read_bytes()
    sc = raw[EXT_HEADER:]
    if len(sc) < PAGE_SIZE:
        raise ValueError(f"{src.name} 太短")
    pages = len(sc) // PAGE_SIZE
    out = bytearray()
    for pg in range(pages):
        page = sc[pg * PAGE_SIZE:(pg + 1) * PAGE_SIZE]
        skip = SALT_SIZE if pg == 0 else 0
        body = page[skip:]
        enc = body[:len(body) - RESERVE]
        iv = body[len(body) - RESERVE:len(body) - RESERVE + IV_SIZE]
        plain = _aes_cbc_decrypt(enc_key, iv, enc)
        if pg == 0:
            full = bytearray(b"SQLite format 3\x00" + plain)
            full[16:18] = struct.pack(">H", PAGE_SIZE)
            full[20] = 0
            full = full[:PAGE_SIZE]
            if len(full) < PAGE_SIZE:
                full += b"\x00" * (PAGE_SIZE - len(full))
            out.extend(full)
        else:
            out.extend(plain)
            if len(plain) < PAGE_SIZE:
                out.extend(b"\x00" * (PAGE_SIZE - len(plain)))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(out)


def accounts() -> list[Path]:
    found = []
    if not QQ_ROOT.is_dir():
        return found
    for d in QQ_ROOT.iterdir():
        if (d / "nt_qq" / "nt_db" / "nt_msg.db").is_file():
            found.append(d)
    found.sort(key=lambda p: (p / "nt_qq" / "nt_db" / "nt_msg.db").stat().st_mtime,
               reverse=True)
    return found


def pick_account(qq: str | None = None) -> Path:
    if qq:
        p = QQ_ROOT / str(qq)
        if (p / "nt_qq" / "nt_db" / "nt_msg.db").is_file():
            return p
        raise RuntimeError(f"找不到 QQ 账号目录：{p}")
    # 优先登录标记文件对应的账号
    login = QQ_ROOT / "nt_qq" / "global" / "nt_data" / "Login"
    if login.is_dir():
        for f in login.iterdir():
            if f.name.startswith(".") and f.name[1:].isdigit():
                p = QQ_ROOT / f.name[1:]
                if (p / "nt_qq" / "nt_db" / "nt_msg.db").is_file():
                    return p
    found = accounts()
    if not found:
        raise RuntimeError(f"找不到 QQ NT 数据库：{QQ_ROOT}")
    return found[0]


_KEY_CACHE: dict[str, str] = {}     # salt_hex -> key_hex（QQ 运行期间密钥不变，重启后校验失败会重扫）


def decrypt_targets(account: Path, progress=None, only: tuple = (),
                    use_cache: bool = True) -> Path:
    """解密需要的 DB 到临时目录，返回目录。调用方负责删除。

    only：只解密指定文件（实时同步只需 nt_msg.db，5~6 秒）。
    use_cache：优先用上次的密钥（先用 page1 校验，失败才重扫进程内存）。
    """
    dbdir = account / "nt_qq" / "nt_db"
    want = set(only) if only else set(TARGET_DBS)
    srcs = {n: dbdir / n for n in TARGET_DBS if (dbdir / n).is_file() and n in want}
    if not srcs:
        raise RuntimeError("QQ 数据库不存在")
    salts = {}
    for n, p in srcs.items():
        with p.open("rb") as f:
            f.seek(EXT_HEADER)
            salts[n] = f.read(SALT_SIZE).hex()
    keys: dict[str, str] = {}
    if use_cache:
        for n, salt_hex in salts.items():
            k = _KEY_CACHE.get(salt_hex)
            if not k:
                continue
            with srcs[n].open("rb") as f:
                f.seek(EXT_HEADER)
                page1 = f.read(EXT_HEADER + PAGE_SIZE)[EXT_HEADER:]
            if _verify_page1(page1, bytes.fromhex(k)):
                keys[salt_hex] = k
    if set(keys.values()) != set(salts.values()):
        pid = find_qq_pid()
        if not pid:
            raise RuntimeError("QQ 未运行：请先登录 QQ，再导入本地记录")
        if progress:
            progress("正在从 QQ 进程读取数据库密钥…")
        keys.update(_scan_keys(pid, [s for s in salts.values() if s not in keys]))
    tmp = Path(tempfile.mkdtemp(prefix="qq_decrypt_"))
    try:
        for n, p in srcs.items():
            salt_hex = salts[n]
            key_hex = keys.get(salt_hex)
            if not key_hex:
                if n == "nt_msg.db":
                    raise RuntimeError("未能取得 nt_msg.db 的密钥（QQ 版本可能已变化）")
                if progress:
                    progress(f"跳过 {n}（未取到密钥）")
                continue
            with p.open("rb") as f:
                page1 = f.read(EXT_HEADER + PAGE_SIZE)[EXT_HEADER:]
            enc_key = bytes.fromhex(key_hex)
            if not _verify_page1(page1, enc_key):
                if n == "nt_msg.db":
                    raise RuntimeError(f"{n} 密钥校验失败")
                continue
            _KEY_CACHE[salt_hex] = key_hex
            if progress:
                progress(f"解密 {n}…")
            decrypt_db(p, tmp / n, enc_key)
        return tmp
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# ---------------------------------------------------------------- Protobuf
def _varint(b: bytes, i: int) -> tuple[int, int]:
    x = s = 0
    while i < len(b):
        c = b[i]
        i += 1
        x |= (c & 0x7F) << s
        if c < 0x80:
            return x, i
        s += 7
    raise ValueError("bad varint")


def _fields(b: bytes) -> list[tuple[int, int, object]]:
    i = 0
    out = []
    while i < len(b):
        try:
            key, i = _varint(b, i)
        except ValueError:
            break
        fn, wt = key >> 3, key & 7
        try:
            if wt == 0:
                v, i = _varint(b, i)
            elif wt == 1:
                v, i = b[i:i + 8], i + 8
            elif wt == 2:
                n, i = _varint(b, i)
                v, i = b[i:i + n], i + n
            elif wt == 5:
                v, i = b[i:i + 4], i + 4
            else:
                break
        except ValueError:
            break
        out.append((fn, wt, v))
    return out


def _dec(v) -> str:
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except Exception:
            return ""
    return str(v or "")


def _loose_text(v, limit: int = 300) -> str:
    """从二进制里捞可读 UTF-8 片段（卡片/红包等无 schema 字段用）。"""
    if not isinstance(v, bytes) or not v:
        return ""
    s = v.decode("utf-8", "ignore")
    chunks = re.findall(
        r"[\u4e00-\u9fff\u3000-\u303fA-Za-z0-9，。！？：；、,.!?:;()（）\[\]【】\\s]{3,}", s)
    out = [x.strip() for x in chunks if x.strip()]
    return " ".join(out)[:limit]


def _nested_text(v, depth=0) -> str:
    """从嵌套 protobuf 中提取可读文本（长度有限，避免把二进制当文字）。"""
    if not isinstance(v, bytes) or depth > 4 or not v:
        return ""
    parts = []
    for fn, wt, x in _fields(v):
        if wt == 2:
            s = _dec(x)
            if s and len(s) <= 300 and sum(ch.isprintable() or ch in "\n\t" for ch in s) >= len(s) * .85:
                parts.append(s)
            else:
                sub = _nested_text(x, depth + 1)
                if sub:
                    parts.append(sub)
    return "\n".join(parts).strip()


def _segments(blob: bytes) -> list[dict]:
    out = []
    for fn, wt, v in _fields(blob):
        if fn != 40800 or wt != 2:
            continue
        d = defaultdict(list)
        for f, w, x in _fields(v):
            d[f].append(x)
        def one(k, default=None):
            vals = d.get(k) or []
            return vals[0] if vals else default
        out.append({"d": d, "one": one})
    return out


def _hexb(v) -> str:
    return v.hex() if isinstance(v, bytes) else ""


def _parse_message(blob: bytes, mtype: int) -> dict:
    segs = _segments(blob)
    if not segs:
        return {"kind": "text", "text": "", "segments": []}
    media = []
    texts = []
    refer = None
    kind = "text"
    for seg in segs:
        one = seg["one"]
        ct = int(one(45002, 0) or 0)
        text = _dec(one(45101, b"")) or ""
        filename = _dec(one(45402, b"")) or ""
        size = int(one(45405, 0) or 0)
        duration = int(one(45415, 0) or 0)
        md5 = _hexb(one(45406, b""))
        path = _dec(one(45812, b"")) or _dec(one(45814, b"")) or ""
        path = re.sub(r"^::NTOS(?:Full)?::", "", path)
        if ct == 1:
            if text and (not texts or texts[-1] != text):
                texts.append(text)
            continue
        if ct == 2:
            if duration or int(one(47601, 0) or 0):
                kind = "video"; texts.append("[视频]")
                media.append({"kind": "video", "filename": filename, "size": size,
                              "duration": duration, "path": path, "md5": md5})
            else:
                kind = "image"; texts.append("[图片]")
                media.append({"kind": "image", "filename": filename, "size": size,
                              "path": path, "md5": md5})
        elif ct == 3:
            kind = "file"; texts.append("[文件] " + (filename or "未命名文件"))
            media.append({"kind": "file", "filename": filename, "size": size,
                          "path": path, "md5": md5})
        elif ct == 4:
            kind = "voice"; texts.append("[语音]")
            media.append({"kind": "voice", "filename": filename, "size": size,
                          "duration": duration, "path": path, "md5": md5})
        elif ct == 5:
            kind = "video"; texts.append("[视频]")
            media.append({"kind": "video", "filename": filename, "size": size,
                          "duration": duration, "path": path})
        elif ct == 6:
            kind = "sticker"; texts.append("[表情]")
        elif ct == 7:
            kind = "reply"
            ref = _nested_text((seg["d"].get(5) or [b""])[0])
            if ref:
                refer = {"who": "", "text": ref}
            if text:
                texts.append(text)
            elif ref:
                texts.append(ref)
        elif ct == 8:
            kind = "reply"
            ref = _dec(one(47713, b"")) or _nested_text((seg["d"].get(5) or [b""])[0])
            if ref:
                refer = {"who": "", "text": ref}
            if text or ref:
                texts.append(text or ref)
        elif ct == 9:
            kind = "card"
            t = (_nested_text((seg["d"].get(48403) or [b""])[0])
                 or _loose_text((seg["d"].get(48403) or [b""])[0]) or text)
            texts.append("[卡片] " + (t[:80] if t else ""))
        elif ct == 10:
            # 40800/10 = 结构化卡片（JSON）：分享链接 / 小程序 / 聊天记录转发
            meta = _dec(one(47901, b""))
            title = desc = jump = tag = ""
            try:
                j = json.loads(meta) if meta else {}
                m = (j.get("meta") or {})
                news = m.get("news") or m.get("detail") or {}
                title = str(news.get("title") or "")
                desc = str(news.get("desc") or "")
                jump = str(news.get("jumpUrl") or news.get("url") or "")
                tag = str(news.get("tag") or j.get("app") or "")
                if not title:
                    title = str(desc or "")
            except Exception:
                pass
            is_forward = bool(re.search(r"viewMultiMsg|聊天记录|multi_msg", meta or ""))
            if is_forward:
                kind = "forward"
                texts.append("[聊天记录]" + ((" " + title) if title else ""))
            else:
                # 分享链接 / 小程序：保留标题与跳转地址，前端渲染成卡片
                kind = "card"
                label = tag.strip() or "分享"
                texts.append(f"[{label}] {title}".strip())
                card = {"kind": "card", "title": title, "desc": desc,
                        "url": jump, "tag": label}
                media.append(card)
        elif ct == 11:
            kind = "system"
            t = (_dec(one(80900, b"")) or _nested_text(one(80903, b""))
                 or _loose_text(one(80903, b"")))
            if t:
                texts.append(t)
        elif ct == 16:
            kind = "card"
            xml = _dec(one(48602, b""))
            titles = [re.sub(r"\s+", " ", x).strip() for x in
                      re.findall(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                                 xml, re.S)]
            titles = [x for x in titles if x]
            if "viewMultiMsg" in xml or "聊天记录" in xml:
                kind = "forward"
                n = re.search(r'tSum="(\d+)"', xml)
                head = titles[0] if titles else "聊天记录"
                preview = " / ".join(titles[1:4])
                texts.append("[聊天记录] " + head + (f"（{n.group(1)}条）" if n else "")
                             + (("：" + preview) if preview else ""))
            else:
                t = titles[0] if titles else ""
                if not t:
                    m = re.search(r"<summary[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</summary>", xml, re.S)
                    t = m.group(1).strip() if m else ""
                texts.append("[卡片]" + ((" " + t[:120]) if t else ""))
        elif ct == 17:
            kind = "system"
            t = _nested_text(one(48751, b"")) or _loose_text(one(48751, b"")) or text
            if t:
                texts.append(t)
        elif ct == 21:
            kind = "call"
            t = _dec(one(48153, b"")) or "通话"
            texts.append("[通话] " + t)
        else:
            if text:
                texts.append(text)
            else:
                texts.append("[消息]")
    if mtype == 3 and kind == "text":
        kind = "file"
    if mtype == 17:
        kind = "system"
    if mtype == 19:
        kind = "call"
    if not texts:
        texts.append("[消息]")
    dedup = []
    for t in texts:
        if not dedup or dedup[-1] != t:
            dedup.append(t)
    texts = dedup
    if media:
        seen_m = set()
        uniq_media = []
        for m in media:
            key = (m.get("kind"), m.get("filename"), m.get("md5"), m.get("path"))
            if key in seen_m:
                continue
            seen_m.add(key)
            uniq_media.append(m)
        media = uniq_media
    parsed = {"kind": kind, "text": "\n".join(texts)[:4000]}
    if media:
        parsed["media"] = media
    if refer:
        parsed["refer"] = refer
    return parsed


# ---------------------------------------------------------------- 读取解密库
def _rows_chunked(con, table: str, cols: list[str], chunk: int = 1000,
                  where: str = "", args: list | None = None):
    """按 rowid 分块读；坏页/坏行跳过，不中断整表。"""
    try:
        sql_ids = f'SELECT rowid FROM "{table}"'
        if where:
            sql_ids += " WHERE " + where
        ids = [r[0] for r in con.execute(sql_ids, tuple(args or ()))]
    except sqlite3.DatabaseError:
        ids = []
    select = ",".join(f'"{c}"' for c in cols)
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        sql = (f'SELECT rowid,{select} FROM "{table}" WHERE rowid IN '
               f'({",".join("?" * len(part))})')
        try:
            yield from con.execute(sql, part).fetchall()
        except sqlite3.DatabaseError:
            for rid in part:
                try:
                    r = con.execute(f'SELECT rowid,{select} FROM "{table}" WHERE rowid=?',
                                    (rid,)).fetchone()
                    if r:
                        yield r
                except sqlite3.DatabaseError:
                    continue


def _contacts(dbdir: Path, progress=None) -> tuple[dict, dict, dict, set]:
    """返回 (uid->资料, uin->资料, groups code->group, 好友 uid 集合)。"""
    by_uid, by_uin, groups, friend_uids = {}, {}, {}, set()
    p = dbdir / "profile_info.db"
    if p.is_file():
        con = sqlite3.connect(str(p))
        try:
            for row in _rows_chunked(con, "buddy_list", ["1000"]):
                if row[1]:
                    friend_uids.add(str(row[1]))
            for row in _rows_chunked(con, "profile_info_v6",
                                     ["1000", "1002", "20002", "20009", "20011"]):
                _rid, uid, uin, nick, remark, sign = row
                if not uid:
                    continue
                nick = _dec(nick).strip()
                remark = _dec(remark).strip()
                name = remark or nick or str(uin or "")
                by_uid[uid] = {"uid": uid, "uin": int(uin or 0),
                               "name": name, "nickname": nick}
                if uin:
                    by_uin[int(uin)] = by_uid[uid]
        finally:
            con.close()
    p = dbdir / "group_info.db"
    if p.is_file():
        con = sqlite3.connect(str(p))
        try:
            for row in _rows_chunked(con, "group_list", ["60001", "60007", "60011"]):
                _rid, code, name, cnt = row
                if not code:
                    continue
                groups[int(code)] = {"code": int(code), "name": _dec(name).strip() or str(code),
                                     "cnt": int(cnt or 0)}
        finally:
            con.close()
    # 群昵称补进联系人（用于发送者显示）
    if p.is_file():
        con = sqlite3.connect(str(p))
        try:
            for row in _rows_chunked(con, "group_member3",
                                     ["60001", "1000", "1002", "64003", "20002"]):
                _rid, code, uid, uin, card, nick = row
                if not uid:
                    continue
                name = _dec(card).strip() or _dec(nick).strip()
                if not name:
                    continue
                rec = {"uid": uid, "uin": int(uin or 0), "name": name,
                       "nickname": _dec(nick).strip()}
                by_uid.setdefault(uid, rec)
                if uin:
                    by_uin.setdefault(int(uin), rec)
        finally:
            con.close()
    if not friend_uids:
        friend_uids = set(by_uid)
    return by_uid, by_uin, groups, friend_uids


def _file_kind(name: str) -> str:
    ext = Path(name or "").suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return "image"
    if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        return "video"
    if ext in (".mp3", ".wav", ".amr", ".silk", ".m4a"):
        return "audio"
    if ext in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
               ".txt", ".md", ".csv", ".zip", ".rar", ".7z"):
        return ext.lstrip(".")
    return "file"


def _conv_for(uid: str, uin: int, group: int, by_uid: dict, by_uin: dict) -> tuple[str, str]:
    if group:
        return f"group:{group}", "group"
    if uin:
        return f"c2c:{uin}", "c2c"
    if uid:
        u = by_uid.get(uid) or {}
        if u.get("uin"):
            return f"c2c:{u['uin']}", "c2c"
        return f"c2c:{uid}", "c2c"
    return "", ""


_MEDIA_IDX_CACHE = {"key": "", "ts": 0.0, "idx": {}}


def _media_index_cached(account: Path, ttl: int = 600, allow_empty: bool = False) -> dict:
    """轻量同步用：媒体索引 10 分钟内复用（全量导入仍现扫，保证准确）。

    allow_empty=True（实时同步）：缓存未热时不在同步路径里扫 6 万个文件
    （实测 14 秒），直接返回空索引并后台补建——图片/文件路径下一轮就有，
    不拖慢新消息入库。
    """
    key = str(account)
    now = time.time()
    if _MEDIA_IDX_CACHE["key"] == key and now - _MEDIA_IDX_CACHE["ts"] < ttl:
        return _MEDIA_IDX_CACHE["idx"]
    if allow_empty:
        if not _MEDIA_IDX_CACHE.get("building"):
            _MEDIA_IDX_CACHE["building"] = True
            def _build():
                try:
                    idx = _media_index(account)
                    _MEDIA_IDX_CACHE.update({"key": key, "ts": time.time(),
                                             "idx": idx, "building": False})
                except Exception:
                    _MEDIA_IDX_CACHE["building"] = False
            import threading
            threading.Thread(target=_build, daemon=True).start()
        return {}
    idx = _media_index(account)
    _MEDIA_IDX_CACHE.update({"key": key, "ts": now, "idx": idx})
    return idx


def _media_index(account: Path) -> dict:
    """文件名/哈希 -> 本机路径，用于把 QQ 消息里的图片/文件关联到缓存。"""
    roots = [account / "Image",
             account / "nt_qq" / "nt_data" / "Pic",
             account / "FileRecv",
             account / "nt_qq" / "nt_data" / "File",
             account / "Video",
             account / "Audio",
             account / "nt_qq" / "nt_data" / "Ptt"]
    idx = {}
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                try:
                    stem = p.stem.lower()
                    idx.setdefault(stem, str(p))
                    idx.setdefault(p.name.lower(), str(p))
                    # 缩略图命名是 <md5>_720 / <md5>_0，也登记基础哈希，
                    # 这样只有缩略图（Ori 未下载）时仍能显示
                    for suffix in ("_720", "_0"):
                        if stem.endswith(suffix) and len(stem) - len(suffix) == 32:
                            idx.setdefault(stem[:-len(suffix)], str(p))
                            break
                except OSError:
                    continue
        except OSError:
            continue
    return idx


def _ctype_of(parsed: dict) -> int:
    return {"text": 1, "image": 2, "file": 3, "voice": 4, "video": 5,
            "sticker": 6, "reply": 7, "card": 9, "forward": 10,
            "system": 17, "call": 21}.get(str(parsed.get("kind") or ""), 0)


def _qq_insert_msgs(con: sqlite3.Connection, batch: list[tuple],
                    incremental: bool = False) -> None:
    """写入消息。增量模式用 DO NOTHING：库里的原文永不被撤回改写覆盖（防撤回核心）。"""
    sql = ("INSERT INTO qq_local_message(conv,msg_id,ts,sender_uid,"
           "sender_uin,sender_name,is_sender,mtype,ctype,text,parsed) "
           "VALUES(?,?,?,?,?,?,?,?,?,?,?)")
    if incremental:
        sql += " ON CONFLICT(msg_id) DO NOTHING"
    else:
        sql += " ON CONFLICT(msg_id) DO UPDATE SET text=excluded.text,"               "parsed=excluded.parsed,ctype=excluded.ctype"
    con.executemany(sql, batch)


_RECALL_TIP = re.compile(r"^(.*?)(?:\((\d{5,})\))?\s*撤回了一条消息")


def _match_recalls(con: sqlite3.Connection, tips: list[tuple]) -> None:
    """撤回提示 → 给同会话同一发送者的最近原消息打 recalled 标（保留原文）。"""
    for conv, msg_id, ts, sender_uin, is_sender, text in tips[-200:]:
        m = _RECALL_TIP.match((text or "").strip())
        if not m:
            continue
        who = (m.group(1) or "").strip()
        uin = int(m.group(2) or 0)
        con.execute(
            "UPDATE qq_local_message SET recalled=1, recall_who=? WHERE id=("
            "SELECT id FROM qq_local_message WHERE conv=? AND recalled=0 AND id<>? "
            "AND ts<=? AND ts>=? "
            "AND (CASE WHEN ?>0 THEN sender_uin=? ELSE (is_sender=1 OR sender_uin=?) END) "
            "AND IFNULL(text,'')<>'' AND text NOT LIKE '%撤回了一条消息%' "
            "ORDER BY ts DESC LIMIT 1)",
            (who[:40], conv, msg_id, ts, ts - 900, uin,
             uin, sender_uin))
    con.commit()


def live_update(con: sqlite3.Connection, live_dir: Path, account: Path | None = None,
                progress=None) -> dict:
    """实时同步核心：维护一份解密副本 data/qq_live/nt_msg.db，只增量解密新内容。

    - 首次/失效（换号/QQ 重装修复）：整库解密一次（约 6 秒）自举；
    - 主文件变大：只解密新增页；
    - 新提交先落 nt_msg.db-wal：按 WAL 帧（24 字节头 + 4096 页）逐帧解密，
      打补丁进副本——每帧一次 4KB AES，毫秒级。
    返回 {"bootstrapped": bool, "patched": int}。之后调用方对该副本走
    import_all(light=True, decrypt_dir=live_dir) 读新消息。
    """
    account = account or pick_account()
    dbdir = account / "nt_qq" / "nt_db"
    src = dbdir / "nt_msg.db"
    wal = dbdir / "nt_msg.db-wal"
    with src.open("rb") as f:
        f.seek(EXT_HEADER)
        salt = f.read(SALT_SIZE).hex()
    key_hex = _KEY_CACHE.get(salt)
    if not key_hex:
        pid = find_qq_pid()
        if not pid:
            raise RuntimeError("QQ 未运行")
        if progress:
            progress("正在读取 QQ 密钥…")
        keys = _scan_keys(pid, [salt])
        key_hex = keys.get(salt)
        if not key_hex:
            raise RuntimeError("未取得 nt_msg.db 密钥")
        _KEY_CACHE[salt] = key_hex
    key = bytes.fromhex(key_hex)
    live_dir.mkdir(parents=True, exist_ok=True)
    plain = live_dir / "nt_msg.db"
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")

    def mget(k, d=""):
        r = con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return r[0] if r else d

    def mset(k, v):
        con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) "
                    "DO UPDATE SET value=excluded.value", (k, str(v)))

    st = src.stat()
    boot = False
    if (mget("qq_live_salt") != salt or not plain.is_file()
            or int(mget("qq_live_main_size", "0")) > st.st_size):
        if progress:
            progress("重建实时解密副本…")
        decrypt_db(src, plain, key)
        mset("qq_live_salt", salt)
        mset("qq_live_main_size", st.st_size)
        wst = wal.stat() if wal.exists() else None
        mset("qq_live_wal_off", wst.st_size if wst else 0)
        return {"bootstrapped": True, "patched": 0}
    patched = 0

    def write_page(pgno: int, plain_bytes: bytes) -> None:
        with open(plain, "r+b") as f:
            f.seek((pgno - 1) * PAGE_SIZE)
            f.write(plain_bytes[:PAGE_SIZE].ljust(PAGE_SIZE, b"\x00"))

    # 1) 主文件尾部新增页（QQ 很少直接写主库，通常走 WAL）
    old_size = int(mget("qq_live_main_size"))
    if st.st_size > old_size:
        with src.open("rb") as f:
            f.seek(old_size)
            data = f.read(st.st_size - old_size)
        base = old_size // PAGE_SIZE
        for i in range(len(data) // PAGE_SIZE):
            page = data[i * PAGE_SIZE:(i + 1) * PAGE_SIZE]
            skip = SALT_SIZE if base + i == 0 else 0
            body = page[skip:]
            enc = body[:len(body) - RESERVE]
            iv = body[len(body) - RESERVE:len(body) - RESERVE + IV_SIZE]
            write_page(base + i + 1, _aes_cbc_decrypt(key, iv, enc))
            patched += 1
        mset("qq_live_main_size", st.st_size)
    # 2) WAL 新帧（新消息基本都在这里）
    if wal.exists():
        wst = wal.stat()
        off = int(mget("qq_live_wal_off", "0"))
        if wst.st_size < off:
            off = 0                    # checkpoint 后 WAL 重置，重放幂等
        if wst.st_size > off:
            with wal.open("rb") as f:
                f.seek(off)
                blob = f.read(wst.st_size - off)
            frame = PAGE_SIZE + 24
            nfull = len(blob) // frame
            for i in range(nfull):
                fr = blob[i * frame:(i + 1) * frame]
                pgno = struct.unpack(">I", fr[:4])[0]
                if pgno == 0 or pgno * PAGE_SIZE > st.st_size:
                    continue
                page = fr[24:24 + PAGE_SIZE]
                enc = page[:PAGE_SIZE - RESERVE]
                iv = page[PAGE_SIZE - RESERVE:PAGE_SIZE - RESERVE + IV_SIZE]
                write_page(pgno, _aes_cbc_decrypt(key, iv, enc))
                patched += 1
            mset("qq_live_wal_off", off + nfull * frame)
    return {"bootstrapped": False, "patched": patched}


def reparse_cards(con: sqlite3.Connection, decrypt_dir: Path, limit: int = 0,
                  progress=None) -> dict:
    """重新解析历史卡片消息（ctype=10）：以前一律当成「聊天记录」，现在按
    JSON 元数据区分分享链接/小程序（带 title/url）与真正的聊天记录转发。
    """
    con_msg = sqlite3.connect(str(Path(decrypt_dir) / "nt_msg.db"))
    patched = 0
    try:
        rows = con.execute(
            "SELECT id, msg_id, ctype, text FROM qq_local_message "
            "WHERE ctype=10 ORDER BY ts DESC" + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()
        targets = {int(r[1]): (int(r[0]), r[3] or "") for r in rows if r[1]}
        if not targets:
            return {"scanned": 0, "patched": 0}
        for table in ("c2c_msg_table", "group_msg_table"):
            for row in _rows_chunked(con_msg, table,
                                     ["40001", "40011", "40800"], chunk=500):
                _rid, msg_id, mtype, blob = row
                mid = int(msg_id or 0)
                if mid not in targets:
                    continue
                try:
                    parsed = _parse_message(blob or b"", int(mtype or 0))
                except Exception:
                    continue
                if (parsed.get("kind") or "") != "card":
                    continue
                con.execute(
                    "UPDATE qq_local_message SET text=?, parsed=?, ctype=? WHERE id=?",
                    (parsed.get("text") or targets[mid][1], json.dumps(parsed, ensure_ascii=False),
                     _ctype_of(parsed), targets[mid][0]))
                patched += 1
                if progress and patched % 200 == 0:
                    progress(f"已重解析 {patched} 条卡片…")
        con.commit()
    finally:
        con_msg.close()
    return {"scanned": len(targets), "patched": patched}


def db_fingerprints(qq: str | None = None) -> dict:
    """QQ NT 库指纹 {文件名: (mtime, size)}：变化说明有新消息/新文件，值得同步一次。"""
    try:
        account = pick_account(qq)
    except Exception:
        return {}
    dbdir = account / "nt_qq" / "nt_db"
    out = {}
    for name in TARGET_DBS:
        p = dbdir / name
        if not p.is_file():
            continue
        st = p.stat()
        try:
            wst = (dbdir / (name + "-wal")).stat()
            wal = (int(wst.st_mtime), wst.st_size)
        except OSError:
            wal = (0, 0)
        # SQLite 新提交先落 -wal，主文件 mtime 可能不动，必须一起盯
        out[name] = (int(st.st_mtime), st.st_size, wal[0], wal[1])
    return out


def import_all(con: sqlite3.Connection, qq: str | None = None, rebuild: bool = True,
               decrypt_dir: str | None = None, progress=None,
               since_ts: int = 0, light: bool = False,
               after_rowid: dict | None = None) -> dict:
    """导入 QQ 本地记录；返回统计。decrypt_dir 仅供调试（跳过解密）。

    since_ts>0 时为增量同步：只读取该时间之后的消息，会话聚合计数按受影响会话
    重算，可安全重复执行。
    light=True（实时同步）：只解密 nt_msg.db（~6 秒），联系人/群映射直接取
    知识库现表，跳过联系人落库与文件助手库——新消息/文件/链接照常入库。
    """
    ensure_schema(con)
    if rebuild:
        for t in ("qq_local_message", "qq_chat", "qq_contact", "qq_file", "qq_link"):
            con.execute(f"DELETE FROM {t}")
        con.commit()
    account = pick_account(qq)
    tmp = None
    try:
        if decrypt_dir:
            dbdir = Path(decrypt_dir)
        elif light:
            tmp = decrypt_targets(account, progress, only=("nt_msg.db",))
            dbdir = tmp
        else:
            tmp = decrypt_targets(account, progress)
            dbdir = tmp
        media_idx = (_media_index_cached(account, allow_empty=True)
                     if light else _media_index(account))
        if light:
            # 轻量模式：不碰 profile_info/group_info，名称映射用知识库现表
            by_uid: dict = {}
            by_uin: dict = {}
            groups: dict = {}
            for key, kind, uid, uin, name in con.execute(
                    "SELECT key,kind,uid,uin,name FROM qq_contact"):
                if kind == "group":
                    if uin:
                        groups[int(uin)] = {"name": name or f"QQ群 {uin}"}
                else:
                    if uid:
                        by_uid[str(uid)] = {"uin": uin or 0, "name": name}
                    if uin:
                        by_uin[int(uin)] = {"name": name}
            friend_uids = set(by_uid.keys())
            contacts = []
        else:
            if progress:
                progress("正在读取联系人与群…")
            by_uid, by_uin, groups, friend_uids = _contacts(dbdir, progress)

        # 联系人/群写入 qq_contact，便于联系页直接查
        contacts = []
        self_uin = int(account.name) if str(account.name).isdigit() else 0
        for uid, c in by_uid.items():
            if uid not in friend_uids:
                continue
            if self_uin and int(c.get("uin") or 0) == self_uin:
                continue
            conv = f"c2c:{c['uin']}" if c.get("uin") else f"c2c:{uid}"
            contacts.append((f"c2c:{uid}", "friend", uid, c.get("uin") or 0,
                             c.get("name") or "", c.get("nickname") or "", conv))
        for code, g in groups.items():
            contacts.append((f"group:{code}", "group", "", code, g["name"], "", f"group:{code}"))
        con.executemany(
            "INSERT OR REPLACE INTO qq_contact(key,kind,uid,uin,name,nickname,conv) "
            "VALUES(?,?,?,?,?,?,?)", contacts)
        con.commit()

        if progress:
            progress("正在读取 QQ 聊天记录…")
        msg_db = dbdir / "nt_msg.db"
        con_msg = sqlite3.connect(str(msg_db))
        conv_agg = {}
        total = 0
        inserted = 0
        max_rowid: dict[str, int] = {}
        files = []
        links = []
        recall_tips: list[tuple] = []
        for table, is_group in (("c2c_msg_table", False), ("group_msg_table", True)):
            cols = ["40001", "40050", "40013", "40020", "40033", "40021",
                    "40030", "40011", "40800", "40090"]
            batch = []
            row_where, row_args = "", []
            if after_rowid is not None:
                # 游标增量：直接走 rowid 主键索引，只读新行（毫秒级），不扫全表
                row_where = "rowid>?"
                row_args = [int(after_rowid.get(table) or 0)]
            elif since_ts:
                row_where = '"40050">=?'
                row_args = [int(since_ts)]
            for row in _rows_chunked(con_msg, table, cols, chunk=800,
                                     where=row_where, args=row_args):
                (_rid, msg_id, ts, direction, sender_uid, sender_uin,
                 peer_uid, peer_uin, mtype, blob, sname) = row
                total += 1
                if _rid and _rid > max_rowid.get(table, 0):
                    max_rowid[table] = int(_rid)
                if not msg_id:
                    continue
                direction = int(direction or 0)
                is_sender = 1 if direction in (1, 2) else 0
                group = 0
                if is_group:
                    group = int(peer_uin or 0)
                    conv = f"group:{group}" if group else ""
                    target_name = (groups.get(group) or {}).get("name") or (f"QQ群 {group}" if group else "")
                else:
                    conv = f"c2c:{int(peer_uin or 0)}" if peer_uin else (
                        f"c2c:{peer_uid}" if peer_uid else "")
                    target_name = ""
                if not conv:
                    continue
                if is_group:
                    sender_name = (_dec(sname).strip() or (by_uid.get(str(sender_uid)) or {}).get("name") or "")[:40]
                else:
                    sender_name = "我" if is_sender else (
                        (by_uin.get(int(sender_uin or 0)) or {}).get("name")
                        or (by_uid.get(str(sender_uid)) or {}).get("name") or "")
                try:
                    parsed = _parse_message(blob or b"", int(mtype or 0))
                except Exception:
                    parsed = {"kind": "text", "text": "[消息]"}
                for _m in (parsed.get("media") or []):
                    if _m.get("path"):
                        continue
                    key = (_m.get("md5") or "").lower()
                    if key:
                        _m["path"] = media_idx.get(key, "")
                    if not _m.get("path") and _m.get("filename"):
                        _m["path"] = media_idx.get(_m["filename"].lower(), "")
                text = parsed.get("text") or ""
                kind = parsed.get("kind") or "text"
                if not text and not parsed.get("media"):
                    continue
                batch.append((conv, int(msg_id), int(ts or 0), str(sender_uid or ""),
                              int(sender_uin or 0), sender_name, is_sender,
                              int(mtype or 0), _ctype_of(parsed), text,
                              json.dumps(parsed, ensure_ascii=False)))
                agg = conv_agg.setdefault(conv, {"name": target_name, "is_group": 1 if is_group else 0,
                                                 "uin": group if is_group else int(peer_uin or 0),
                                                 "uid": "" if is_group else str(peer_uid or ""),
                                                 "last_ts": 0, "cnt": 0, "last_text": "",
                                                 "last_is_sender": 0})
                if int(ts or 0) >= agg["last_ts"]:
                    agg["last_ts"] = int(ts or 0)
                    agg["last_text"] = text[:120]
                    agg["last_is_sender"] = is_sender
                agg["cnt"] += 1
                # 文件附件 & 链接顺手收集
                for m in (parsed.get("media") or []):
                    if m.get("kind") == "file" and m.get("filename"):
                        files.append((f"msg:{msg_id}:{m.get('filename')}", conv, int(msg_id),
                                      int(ts or 0), m.get("filename") or "",
                                      int(m.get("size") or 0), m.get("path") or "",
                                      _file_kind(m.get("filename") or ""), sender_name))
                card_url = ""
                for _m in (parsed.get("media") or []):
                    if _m.get("kind") == "card" and _m.get("url"):
                        card_url = _m["url"]
                        break
                for u in URL_RE.findall(text) + ([card_url] if card_url else []):
                    if len(u) > 8:
                        links.append((f"{conv}:{msg_id}:{u}", u, "", conv,
                                      int(msg_id), int(ts or 0)))
                if "撤回了一条消息" in text:
                    recall_tips.append((conv, int(msg_id), int(ts or 0),
                                        int(sender_uin or 0), is_sender, text))
                if len(batch) >= 1000:
                    _qq_insert_msgs(con, batch, incremental=bool(since_ts))
                    inserted += len(batch)
                    batch.clear()
                    if progress and inserted % 50000 < 1000:
                        progress(f"已解析 {inserted} 条…")
            if batch:
                _qq_insert_msgs(con, batch, incremental=bool(since_ts))
                inserted += len(batch)
        # 撤回提示 → 回标同会话同一人的最近一条原消息（原文已入库，只打标）
        _match_recalls(con, recall_tips)
        con_msg.close()
        con.commit()

        # 文件助手 + 聊天文件表
        if progress:
            progress("正在读取 QQ 文件…")
        fa = dbdir / "file_assistant.db"
        if fa.is_file():
            con_f = sqlite3.connect(str(fa))
            try:
                for row in _rows_chunked(con_f, "file_assistant_v2",
                                         ["200002", "200005", "200009", "200011",
                                          "1000", "40021", "40010", "60001", "20002"]):
                    _rid, name, size, ts, path, uid, peer_uid, chattype, group, sname = row
                    name = _dec(name).strip()
                    if not name:
                        continue
                    conv, _k = _conv_for(str(peer_uid or ""), 0, int(group or 0), by_uid, by_uin)
                    if not conv:
                        conv, _k = _conv_for(str(uid or ""), 0, 0, by_uid, by_uin)
                    if not conv:
                        continue
                    path = re.sub(r"^(?:::NTOS(?:Full)?::|\s)+", "", _dec(path).strip())
                    if not path:
                        path = media_idx.get(name.lower(), "")
                    files.append((f"fa:{row[0]}:{name}", conv, 0, int(ts or 0), name,
                                  int(size or 0), path, _file_kind(name), _dec(sname).strip()))
            finally:
                con_f.close()
        fic = dbdir / "files_in_chat.db"
        if fic.is_file():
            con_i = sqlite3.connect(str(fic))
            try:
                for row in _rows_chunked(con_i, "files_in_chat_table",
                                         ["40001", "45402", "45405", "40050"]):
                    _rid, msg_id, name, size, ts = row
                    name = _dec(name).strip()
                    if not name:
                        continue
                    hit = con.execute("SELECT conv,is_sender,sender_name FROM qq_local_message "
                                      "WHERE msg_id=?", (int(msg_id or 0),)).fetchone()
                    conv = hit[0] if hit else ""
                    if not conv:
                        continue
                    files.append((f"fic:{msg_id}:{name}", conv, int(msg_id or 0),
                                  int(ts or 0), name, int(size or 0),
                                  media_idx.get(name.lower(), ""),
                                  _file_kind(name), hit[2] if hit else ""))
            finally:
                con_i.close()
        if files:
            con.executemany(
                "INSERT OR IGNORE INTO qq_file(file_key,conv,msg_id,ts,name,size,path,kind,sender_name) "
                "VALUES(?,?,?,?,?,?,?,?,?)", files)
        if links:
            con.executemany(
                "INSERT OR IGNORE INTO qq_link(link_key,url,title,conv,msg_id,ts) "
                "VALUES(?,?,?,?,?,?)", links)

        # 会话聚合（名称优先用联系人/群备注）
        for conv, a in conv_agg.items():
            if a["is_group"]:
                g = groups.get(a["uin"]) or {}
                a["name"] = g.get("name") or a["name"] or f"QQ群 {a['uin']}"
            else:
                c = by_uin.get(a["uin"]) or by_uid.get(a["uid"]) or {}
                a["name"] = c.get("name") or a["name"] or str(a["uin"] or a["uid"])
            con.execute(
                "INSERT INTO qq_chat(conv,name,is_group,uin,uid,last_ts,cnt,last_text,last_is_sender) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(conv) DO UPDATE SET "
                "name=excluded.name,is_group=excluded.is_group,uin=excluded.uin,"
                "uid=excluded.uid,last_ts=excluded.last_ts,cnt=excluded.cnt,"
                "last_text=excluded.last_text,last_is_sender=excluded.last_is_sender",
                (conv, a["name"], a["is_group"], a["uin"], a["uid"],
                 a["last_ts"], a["cnt"], a["last_text"][:200], a["last_is_sender"]))
        # 增量模式：窗口内计数只是增量，受影响会话按全表重算，保证可重复执行
        if since_ts and conv_agg:
            affected = list(conv_agg.keys())
            for i in range(0, len(affected), 200):
                part = affected[i:i + 200]
                ph = ",".join("?" * len(part))
                for conv, cnt, last_ts in con.execute(
                        f"SELECT conv,COUNT(*),MAX(ts) FROM qq_local_message "
                        f"WHERE conv IN ({ph}) GROUP BY conv", part).fetchall():
                    top = con.execute(
                        "SELECT text,is_sender FROM qq_local_message WHERE conv=? "
                        "ORDER BY ts DESC,id DESC LIMIT 1", (conv,)).fetchone()
                    con.execute(
                        "UPDATE qq_chat SET cnt=?,last_ts=?,last_text=?,last_is_sender=? "
                        "WHERE conv=?",
                        (cnt, last_ts, (top[0] or "")[:200] if top else "",
                         (top[1] if top else 0), conv))
        # 联系人列表带上最近会话
        con.execute("""UPDATE qq_contact SET
            last_ts=COALESCE((SELECT c.last_ts FROM qq_chat c WHERE c.conv=qq_contact.conv),0),
            cnt=COALESCE((SELECT c.cnt FROM qq_chat c WHERE c.conv=qq_contact.conv),0)""")
        con.commit()
        return {"account": account.name, "messages": inserted, "scanned": total,
                "chats": len(conv_agg), "contacts": len(contacts),
                "files": len(files), "links": len(links),
                "max_rowid": max_rowid}
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 查询
def ensure_schema(con: sqlite3.Connection) -> None:
    for sql in SCHEMA:
        con.execute(sql)
    # 旧库升级：会话置顶
    cols = {r[1] for r in con.execute("PRAGMA table_info(qq_chat)")}
    if "pinned" not in cols:
        try:
            con.execute("ALTER TABLE qq_chat ADD COLUMN pinned INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
    # 旧库升级：防撤回（保留原文 + 标记）
    mcols = {r[1] for r in con.execute("PRAGMA table_info(qq_local_message)")}
    if "recalled" not in mcols:
        try:
            con.execute("ALTER TABLE qq_local_message ADD COLUMN recalled INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
    if "recall_who" not in mcols:
        try:
            con.execute("ALTER TABLE qq_local_message ADD COLUMN recall_who TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    con.commit()


def self_info(con: sqlite3.Connection) -> dict:
    """自己的 QQ 号与昵称（从自己发的消息里取最近的非「我」昵称）。"""
    ensure_schema(con)
    row = con.execute(
        "SELECT sender_uin, sender_name FROM qq_local_message "
        "WHERE is_sender=1 AND sender_uin>0 AND IFNULL(sender_name,'')<>'' "
        "AND sender_name<>'我' ORDER BY ts DESC LIMIT 1").fetchone()
    uin = int(row[0]) if row and row[0] else 0
    name = (row[1] or "") if row else ""
    if not uin:
        r2 = con.execute("SELECT sender_uin FROM qq_local_message WHERE is_sender=1 "
                         "AND sender_uin>0 ORDER BY ts DESC LIMIT 1").fetchone()
        uin = int(r2[0]) if r2 and r2[0] else 0
    return {"uin": uin, "name": name or str(uin or "")}


def stats(con: sqlite3.Connection) -> dict:
    ensure_schema(con)
    out = {}
    for key, table in (("messages", "qq_local_message"), ("chats", "qq_chat"),
                       ("contacts", "qq_contact"), ("files", "qq_file"),
                       ("links", "qq_link")):
        try:
            out[key] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            out[key] = 0
    out["last_ts"] = con.execute("SELECT MAX(ts) FROM qq_local_message").fetchone()[0] or 0
    return out


def sessions(con: sqlite3.Connection, q: str = "", limit: int = 300) -> list[dict]:
    ensure_schema(con)
    sql = ("SELECT conv,name,is_group,uin,uid,last_ts,cnt,last_text,last_is_sender,"
           "IFNULL(pinned,0) FROM qq_chat")
    args = []
    if q.strip():
        sql += " WHERE name LIKE ?"
        args.append(f"%{q.strip()}%")
    sql += " ORDER BY IFNULL(pinned,0) DESC, last_ts DESC LIMIT ?"
    args.append(max(1, min(limit, 1000)))
    keys = ["conv", "name", "is_group", "uin", "uid", "last_ts", "cnt",
            "last_text", "last_is_sender", "pinned"]
    return [dict(zip(keys, r)) for r in con.execute(sql, args).fetchall()]


def set_pinned(con: sqlite3.Connection, conv: str, pinned: bool) -> None:
    ensure_schema(con)
    con.execute("UPDATE qq_chat SET pinned=? WHERE conv=?",
                (1 if pinned else 0, conv))
    con.commit()


def _lookup_media(md5: str, filename: str = "", account: Path | None = None) -> str:
    """按 md5/文件名在 QQ 缓存目录里找一张图（含 Ori/Thumb/OriTemp）。

    消息里存的 path 是导入那一刻的快照；QQ 之后才下载的图不会被写进去
    （增量导入 DO NOTHING 不覆盖），所以读的时候要再查一次。
    """
    md5 = (md5 or "").lower()
    if not md5 and not filename:
        return ""
    cache = _MEDIA_IDX_CACHE.get("idx") or {}
    if cache:
        hit = (cache.get(md5) if md5 else "") or (
            cache.get(filename.lower()) if filename else "")
        if hit:
            return hit
    try:
        acct = account or pick_account()
    except Exception:
        return ""
    base = acct / "nt_qq" / "nt_data" / "Pic"
    if not base.is_dir():
        return ""
    from datetime import datetime as _dt
    recent = []
    try:
        for d in sorted(base.iterdir(), reverse=True)[:6]:
            if not d.is_dir():
                continue
            for sub in ("Ori", "Thumb", "OriTemp", "ThumbTemp"):
                q = d / sub
                if q.is_dir():
                    recent.append(q)
    except OSError:
        return ""
    for d in recent:
        try:
            hit = None
            for f in d.iterdir():
                name = f.name.lower()
                if md5 and name.startswith(md5):
                    # 优先原图（Ori），缩略图次之
                    if d.name == "Ori" or d.name == "OriTemp":
                        return str(f)
                    hit = hit or str(f)
                elif filename and name == filename.lower():
                    hit = hit or str(f)
            if hit:
                return hit
        except OSError:
            continue
    return ""


def hydrate_media(rows: list[dict], account: Path | None = None) -> list[dict]:
    """用媒体索引补齐 parsed.media 里缺失的本地路径。

    实时同步启用的瞬间索引还没建好（异步 15 秒），那批消息入库时 path 为空；
    增量导入是 DO NOTHING（防撤回不能覆盖），所以只能读取时补。索引全局缓存，
    这里只是字典查询，开销可忽略。
    """
    # 缓存未就绪（刚启动、异步索引还在建）：等它建（一次约 50 秒），别返回空
    cache = _MEDIA_IDX_CACHE.get("idx") or {}
    if not cache:
        # 启动时后台拉的 CDN 缓存也纳入索引（否则新拉的图读不到）
        fetched = DB_PATH.parent / "qq_fetched"
        if fetched.is_dir():
            try:
                for f in fetched.iterdir():
                    if f.is_file():
                        cache.setdefault(f.stem.lower(), str(f))
                        cache.setdefault(f.name.lower(), str(f))
            except OSError:
                pass
    if not cache:
        if _MEDIA_IDX_CACHE.get("building"):
            deadline = time.time() + 90
            while _MEDIA_IDX_CACHE.get("building") and time.time() < deadline:
                time.sleep(0.5)
            cache = _MEDIA_IDX_CACHE.get("idx") or {}
        if not cache:
            try:
                cache = _media_index_cached(account or pick_account(), ttl=600,
                                            allow_empty=True)
            except Exception:
                cache = {}
    for r in rows:
        parsed = r.get("parsed")
        if not isinstance(parsed, dict):
            continue
        for m in (parsed.get("media") or []):
            if m.get("path") and Path(m["path"]).is_file():
                continue
            if m.get("path"):
                m["path"] = ""          # 快照路径已失效，重新查
            key = (m.get("md5") or "").lower()
            if key and key in cache:
                m["path"] = cache[key]
                continue
            fn = (m.get("filename") or "").lower()
            if fn:
                hit = cache.get(fn) or cache.get(fn.rsplit(".", 1)[0])
                if hit:
                    m["path"] = hit
            if not m.get("path") and m.get("kind") in ("image", "video", "voice"):
                # 索引没命中但图片可能已经下载到缓存里（QQ 之后才拉下来）：
                # 按 md5 直接扫最近几个月的 Ori/Thumb 目录
                hit = _lookup_media(m.get("md5") or "", m.get("filename") or "",
                                    account)
                if hit:
                    m["path"] = hit
            if not m.get("path"):
                m["_missing"] = True
    return rows


def messages(con: sqlite3.Connection, conv: str, limit: int = 200,
             before: int = 0, around: int = 0, after: int = 0) -> list[dict]:
    ensure_schema(con)
    cols = ("id,conv,msg_id,ts,sender_uid,sender_uin,sender_name,is_sender,"
            "mtype,ctype,text,parsed,recalled,recall_who")
    sql = f"SELECT {cols} FROM qq_local_message WHERE conv=?"
    args = [conv]
    ascending = False
    if after:
        # 实时轮询：取比 after 新的消息，时间正序
        rows = con.execute(sql + " AND id>? ORDER BY id ASC LIMIT ?",
                           args + [int(after), max(1, min(limit, 200))]).fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols.split(","), r))
            try:
                d["parsed"] = json.loads(d.get("parsed") or "{}")
            except Exception:
                d["parsed"] = {"kind": "text", "text": d.get("text") or ""}
            out.append(d)
        return hydrate_media(out)
    if around:
        # 引用跳转：以目标消息为中心取前后各一半，老消息也能定位
        anchor = con.execute(
            "SELECT ts FROM qq_local_message WHERE id=? AND conv=?",
            (int(around), conv)).fetchone()
        if anchor:
            half = max(20, max(1, min(limit, 500)) // 2)
            head = con.execute(sql + " AND ts<=? ORDER BY ts DESC, id DESC LIMIT ?",
                               args + [int(anchor[0]), half]).fetchall()
            tail = con.execute(sql + " AND ts>? ORDER BY ts ASC, id ASC LIMIT ?",
                               args + [int(anchor[0]), half]).fetchall()
            rows = list(reversed(head)) + list(tail)   # 时间正序
            ascending = True
            args = None
    if args is not None:
        if before:
            sql += " AND ts<?"
            args.append(int(before))
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        rows = con.execute(sql, args).fetchall()
    out = []
    for r in (rows if ascending else reversed(rows)):
        d = dict(r) if isinstance(r, sqlite3.Row) else {
            "id": r[0], "conv": r[1], "msg_id": r[2], "ts": r[3],
            "sender_uid": r[4], "sender_uin": r[5], "sender_name": r[6],
            "is_sender": r[7], "mtype": r[8], "ctype": r[9], "text": r[10],
            "parsed": r[11], "recalled": r[12] if len(r) > 12 else 0,
            "recall_who": r[13] if len(r) > 13 else ""}
        try:
            d["parsed"] = json.loads(d.get("parsed") or "{}")
        except Exception:
            d["parsed"] = {"kind": "text", "text": d.get("text") or ""}
        out.append(d)
    return hydrate_media(out)


def contacts(con: sqlite3.Connection, q: str = "", kind: str = "",
             limit: int = 500) -> list[dict]:
    ensure_schema(con)
    sql = ("SELECT key,kind,uid,uin,name,nickname,conv,last_ts,cnt FROM qq_contact")
    where, args = [], []
    if q.strip():
        where.append("(name LIKE ? OR nickname LIKE ? OR CAST(uin AS TEXT) LIKE ?)")
        args += [f"%{q.strip()}%"] * 3
    if kind:
        where.append("kind=?")
        args.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_ts DESC, name LIMIT ?"
    args.append(max(1, min(limit, 2000)))
    keys = ["key", "kind", "uid", "uin", "name", "nickname", "conv", "last_ts", "cnt"]
    return [dict(zip(keys, r)) for r in con.execute(sql, args).fetchall()]


def files(con: sqlite3.Connection, q: str = "", kind: str = "", conv: str = "",
          limit: int = 200, offset: int = 0, sort: str = "new", days: int = 0,
          state: str = "") -> dict:
    """QQ 文件中心：筛选 / 排序 / 分页 + 解析统计。返回 {files, stats, hasMore}。"""
    ensure_schema(con)
    where, args = [], []
    if q.strip():
        where.append("f.name LIKE ?")
        args.append(f"%{q.strip()}%")
    if kind:
        where.append("f.kind=?")
        args.append(kind)
    if conv:
        where.append("f.conv=?")
        args.append(conv)
    if days:
        where.append("f.ts>=?")
        args.append(int(time.time()) - int(days) * 86400)
    if state == "missing":
        where.append("IFNULL(f.path,'')=''")
    elif state == "parsed":
        where.append("EXISTS(SELECT 1 FROM qq_file_text t WHERE t.file_id=f.id "
                     "AND IFNULL(t.error,'')='')")
    elif state == "unparsed":
        where.append("IFNULL(f.path,'')<>''")
        where.append("NOT EXISTS(SELECT 1 FROM qq_file_text t WHERE t.file_id=f.id "
                     "AND IFNULL(t.error,'')='')")
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    order = {"old": "f.ts ASC", "size": "f.size DESC"}.get(sort, "f.ts DESC")
    sql = ("SELECT f.id,f.conv,c.name AS conv_name,f.msg_id,f.name,f.size,f.ts,"
           "f.path,f.kind,f.sender_name FROM qq_file f "
           "LEFT JOIN qq_chat c ON c.conv=f.conv" + cond +
           f" ORDER BY {order} LIMIT ? OFFSET ?")
    page_args = args + [max(1, min(limit, 500)), max(0, offset)]
    keys = ["id", "conv", "conv_name", "msg_id", "name", "size", "ts", "path",
            "kind", "sender_name"]
    out = []
    for r in con.execute(sql, page_args).fetchall():
        d = dict(zip(keys, r))
        d["exists"] = bool(d.get("path") and Path(d["path"]).is_file())
        t = con.execute("SELECT chars FROM qq_file_text WHERE file_id=?",
                        (d["id"],)).fetchone()
        d["parsed"] = bool(t and (t[0] or 0) > 0)
        d["chars"] = (t[0] or 0) if t else 0
        out.append(d)
    # 统计（不受 state 筛选影响，供工具栏显示）
    total = con.execute("SELECT COUNT(*) FROM qq_file").fetchone()[0] or 0
    parsed_n = con.execute(
        "SELECT COUNT(*) FROM qq_file f WHERE EXISTS(SELECT 1 FROM qq_file_text t "
        "WHERE t.file_id=f.id AND IFNULL(t.error,'')='' AND t.chars>0)").fetchone()[0] or 0
    local_n = con.execute(
        "SELECT COUNT(*) FROM qq_file WHERE IFNULL(path,'')<>''").fetchone()[0] or 0
    kinds = {}
    for k, n, p in con.execute(
            "SELECT f.kind, COUNT(*), SUM(EXISTS(SELECT 1 FROM qq_file_text t "
            "WHERE t.file_id=f.id AND IFNULL(t.error,'')='' AND t.chars>0)) "
            "FROM qq_file f GROUP BY f.kind"):
        kinds[k or "file"] = {"total": n, "parsed": p or 0}
    return {"files": out, "hasMore": len(out) >= max(1, min(limit, 500)),
            "stats": {"total": total, "parsed": parsed_n, "local": local_n,
                      "missing": total - local_n, "kinds": kinds}}


def file_text(con: sqlite3.Connection, file_id: int, force: bool = False) -> dict:
    """提取 QQ 文件正文（带缓存）。返回 {name, text, chars, found, error}。"""
    ensure_schema(con)
    row = con.execute("SELECT name,path,size FROM qq_file WHERE id=?",
                      (int(file_id),)).fetchone()
    if not row:
        return {"found": False, "error": "文件记录不存在"}
    name, path, size = row
    if not path or not Path(path).is_file():
        return {"found": False, "name": name,
                "error": "文件不在本机（可能已被 QQ 清理或未下载）"}
    if not force:
        hit = con.execute("SELECT text,chars FROM qq_file_text WHERE file_id=?",
                          (int(file_id),)).fetchone()
        if hit and (hit[1] or 0) > 0:
            return {"found": True, "name": name, "text": hit[0] or "",
                    "chars": hit[1] or 0}
    import main as M                     # 延迟导入，extract_text 在 main 里
    try:
        text = M.extract_text(Path(path), 24000)
    except Exception as exc:  # noqa: BLE001
        text = ""
        con.execute("INSERT OR REPLACE INTO qq_file_text(file_id,name,text,chars,"
                    "error,ts) VALUES(?,?,?,?,?,?)",
                    (int(file_id), name, "", 0, str(exc)[:200], int(time.time())))
        con.commit()
        return {"found": True, "name": name, "text": "", "chars": 0,
                "error": f"解析失败：{exc}"[:200]}
    chars = len(text or "")
    con.execute("INSERT OR REPLACE INTO qq_file_text(file_id,name,text,chars,"
                "error,ts) VALUES(?,?,?,?,?,?)",
                (int(file_id), name, text or "", chars, "", int(time.time())))
    con.commit()
    return {"found": True, "name": name, "text": text or "", "chars": chars}


def files_to_parse(con: sqlite3.Connection, limit: int = 50) -> list[int]:
    """待解析（本机有文件、还没有正文）的 QQ 文件 id，按时间倒序。"""
    ensure_schema(con)
    rows = con.execute(
        "SELECT f.id FROM qq_file f WHERE IFNULL(f.path,'')<>'' "
        "AND NOT EXISTS(SELECT 1 FROM qq_file_text t WHERE t.file_id=f.id "
        "AND IFNULL(t.error,'')='' AND t.chars>0) "
        "ORDER BY f.ts DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
    return [r[0] for r in rows]


def _link_platform(url: str) -> str:
    try:
        from link_media import detect
        return detect(url) or "generic"
    except Exception:
        return "generic"


def links(con: sqlite3.Connection, q: str = "", conv: str = "", limit: int = 100,
          offset: int = 0, sort: str = "new", days: int = 0,
          platform: str = "", state: str = "") -> dict:
    """QQ 链接中心：筛选 / 排序 / 分页 + 平台 + 状态（复用 link_cache）。"""
    ensure_schema(con)
    where, args = [], []
    if q.strip():
        where.append("(l.url LIKE ? OR l.title LIKE ?)")
        args += [f"%{q.strip()}%"] * 2
    if conv:
        where.append("l.conv=?")
        args.append(conv)
    if days:
        where.append("l.ts>=?")
        args.append(int(time.time()) - int(days) * 86400)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    order = "l.ts ASC" if sort == "old" else "l.ts DESC"
    sql = ("SELECT l.id,l.url,l.title,l.conv,c.name AS conv_name,l.msg_id,l.ts "
           "FROM qq_link l LEFT JOIN qq_chat c ON c.conv=l.conv" + cond +
           f" ORDER BY {order} LIMIT ? OFFSET ?")
    page_args = args + [max(1, min(limit, 500)), max(0, offset)]
    # 已解析 URL 集合（link_cache 里的 url 是规范化后的）
    try:
        from link_media import normalize_url
    except Exception:
        normalize_url = lambda u: u  # noqa: E731
    parsed_urls = set()
    try:
        for (u,) in con.execute("SELECT url FROM link_cache").fetchall():
            parsed_urls.add(normalize_url(u))
    except Exception:
        parsed_urls = set()
    keys = ["id", "url", "title", "conv", "conv_name", "msg_id", "ts"]
    out = []
    for r in con.execute(sql, page_args).fetchall():
        d = dict(zip(keys, r))
        try:
            d["parsed"] = normalize_url(d["url"]) in parsed_urls
        except Exception:
            d["parsed"] = False
        d["state"] = 1 if d["parsed"] else 0
        d["platform_key"] = _link_platform(d["url"])
        d["platform"] = {"xhs": "小红书", "bili": "B站",
                         "article": "文章", "generic": "网页"}.get(
                             d["platform_key"], "网页")
        if platform and d["platform_key"] != platform:
            continue
        out.append(d)
    # 平台 / 状态筛选在 Python 侧完成，分页游标仍按原始页推进
    if platform:
        out = [x for x in out if x["platform_key"] == platform]
    if state == "parsed":
        out = [x for x in out if x["parsed"]]
    elif state == "pending":
        out = [x for x in out if not x["parsed"]]
    total = con.execute("SELECT COUNT(*) FROM qq_link").fetchone()[0] or 0
    return {"links": out, "hasMore": len(out) >= max(1, min(limit, 500)),
            "stats": {"total": total, "parsed": len(parsed_urls)}}


def links_to_parse(con: sqlite3.Connection, limit: int = 20) -> list[tuple[int, str]]:
    """待解析 QQ 链接（没有正文缓存的），按时间倒序。"""
    ensure_schema(con)
    try:
        from link_media import normalize_url
    except Exception:
        normalize_url = lambda u: u  # noqa: E731
    parsed_urls = set()
    try:
        for (u,) in con.execute("SELECT url FROM link_cache").fetchall():
            parsed_urls.add(normalize_url(u))
    except Exception:
        return []
    rows = con.execute("SELECT id,url FROM qq_link ORDER BY ts DESC LIMIT 2000").fetchall()
    out = []
    for rid, url in rows:
        try:
            if normalize_url(url) not in parsed_urls:
                out.append((rid, url))
        except Exception:
            continue
        if len(out) >= max(1, min(limit, 100)):
            break
    return out


def search(con: sqlite3.Connection, q: str, limit: int = 50,
           conv: str = "") -> list[dict]:
    ensure_schema(con)
    q = (q or "").strip()
    where, args = ["m.text LIKE ?"], [f"%{q}%"]
    if conv:
        where.append("m.conv=?")
        args.append(conv)
    if q:
        sql = ("SELECT m.id,m.conv,c.name,m.sender_name,m.is_sender,m.text,m.ts,m.parsed "
               "FROM qq_local_message m LEFT JOIN qq_chat c ON c.conv=m.conv "
               "WHERE " + " AND ".join(where) + " ORDER BY m.ts DESC LIMIT ?")
        args.append(max(1, min(limit, 200)))
        rows = con.execute(sql, args).fetchall()
    else:
        return recent(con, limit, conv)
    keys = ["id", "conv", "conv_name", "who", "is_self", "snippet", "ts", "parsed"]
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        try:
            d["parsed"] = json.loads(d.get("parsed") or "{}")
        except Exception:
            d["parsed"] = {}
        out.append(d)
    return out


def recent(con: sqlite3.Connection, limit: int = 60, conv: str = "") -> list[dict]:
    ensure_schema(con)
    sql = ("SELECT m.id,m.conv,c.name,m.sender_name,m.is_sender,m.text,m.ts "
           "FROM qq_local_message m LEFT JOIN qq_chat c ON c.conv=m.conv")
    args = []
    if conv:
        sql += " WHERE m.conv=?"
        args.append(conv)
    sql += " ORDER BY m.ts DESC LIMIT ?"
    args.append(max(1, min(limit, 300)))
    rows = con.execute(sql, args).fetchall()
    keys = ["id", "conv", "conv_name", "who", "is_self", "text", "ts"]
    return [dict(zip(keys, r)) for r in rows]


def context_for(con: sqlite3.Connection, question: str, limit: int = 20) -> dict:
    """给 QQ Agent 的简易检索：关键词命中 + 近期记录。"""
    hits = search(con, question, limit)
    return {"hits": hits, "recent": recent(con, 30)}


def media_path(path: str) -> Path | None:
    """校验允许读取的 QQ 本地文件路径。"""
    if not path:
        return None
    p = Path(path)
    try:
        p = p.resolve()
    except Exception:
        return None
    roots = []
    for a in accounts():
        roots += [a, a / "nt_qq" / "nt_data"]
    roots += [Path.home() / "Downloads", Path.home() / "Documents"]
    for r in roots:
        try:
            p.relative_to(r.resolve())
            return p if p.is_file() else None
        except Exception:
            continue
    return None
