# -*- coding: utf-8 -*-
"""在微信数据目录中寻找落盘的密钥线索：
1) DPAPI 加密块（magic 01 00 00 00 D0 8C 9D DF）→ CryptUnprotectData 解密 →
   对解密结果做 32 字节滑窗，用 page1 AES 指纹校验
2) 二进制盐值出现在哪些文件（密钥通常存放在盐值附近）
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m0lib import RESERVE_CANDIDATES, HEADER_FINGERPRINT, check_raw_key, load_page1  # noqa: E402
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

DPAPI_MAGIC = bytes.fromhex("01000000d08c9ddf")


def dpapi_decrypt(data: bytes) -> bytes | None:
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.c_void_p)]

    buf = ctypes.create_string_buffer(data, len(data))
    in_blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None,
                                      None, 0, ctypes.byref(out_blob)):
        return None
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(out_blob.pbData))


def try_key_window(buf: bytes, page1: bytes) -> tuple[int, int] | None:
    """对 buf 做 32B 滑窗（step1），返回 (offset, reserve) 或 None。"""
    enc_head = page1[16:32]
    ivs = {r: page1[4096 - r:4096 - r + 16] for r in RESERVE_CANDIDATES}
    for o in range(0, max(0, len(buf) - 32)):
        key = buf[o:o + 32]
        for r, iv in ivs.items():
            try:
                dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
                raw = dec.update(enc_head)
            except Exception:
                continue
            if all(raw[p] == (fp ^ iv[p]) for p, fp in HEADER_FINGERPRINT.items()):
                return (o, r)
    return None


def main():
    account_dir = sys.argv[1] if len(sys.argv) > 1 else \
        r"C:\Users\14815\xwechat_files\wxid_hs7bm9yapf5x12_965a"
    target_db = sys.argv[2] if len(sys.argv) > 2 else \
        os.path.join(account_dir, "db_storage", "message", "message_0.db")
    page1 = load_page1(target_db)
    salt = page1[:16]
    print(f"[*] 目标库 {target_db} salt={salt.hex()}")

    hits_dpapi, hits_salt = [], []
    scanned = 0
    for root, dirs, files in os.walk(account_dir):
        dirs[:] = [d for d in dirs if d not in ("msg", "resource", "cache", "temp")]
        for name in files:
            p = os.path.join(root, name)
            try:
                if os.path.getsize(p) > 20 * 1024 * 1024:
                    continue
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            scanned += 1
            off = 0
            while True:
                i = data.find(DPAPI_MAGIC, off)
                if i < 0:
                    break
                hits_dpapi.append((p, i))
                off = i + 1
            off = 0
            while True:
                i = data.find(salt, off)
                if i < 0:
                    break
                hits_salt.append((p, i))
                off = i + 1

    print(f"[*] 扫描 {scanned} 个文件: DPAPI块 {len(hits_dpapi)} 个, 盐值出现 {len(hits_salt)} 处")
    for p, i in hits_dpapi[:20]:
        print("    DPAPI:", p, "@", i)
    for p, i in hits_salt[:20]:
        print("    SALT :", p, "@", i)

    # 解密 DPAPI 块并测试其中是否藏有密钥
    for p, i in hits_dpapi:
        with open(p, "rb") as f:
            data = f.read()
        blob = data[i:i + 4000]
        try:
            plain = dpapi_decrypt(blob)
        except Exception as e:
            print(f"[!] DPAPI 解密失败 {p}@{i}: {e}")
            continue
        if not plain:
            continue
        print(f"[*] DPAPI 解密成功 {p}@{i} ({len(plain)}B)，测试滑窗...")
        hit = try_key_window(plain, page1)
        if hit:
            o, r = hit
            print(f"[+] 找到密钥! {plain[o:o+32].hex()} (reserve={r}) 来自 {p}@{i}")
            with open(os.path.join(os.path.dirname(__file__), "..", "..", "data",
                                   "keys.json"), "w", encoding="utf-8") as f:
                import json
                json.dump({"key_hex": plain[o:o + 32].hex(), "reserve": r,
                           "kind": "dpapi-disk", "source": f"{p}@{i}"}, f, indent=2)
            return
    print("[-] DPAPI 路线未命中")


if __name__ == "__main__":
    main()
