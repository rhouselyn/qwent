# -*- coding: utf-8 -*-
"""M0 公共库：微信 4.x SQLCipher 参数、密钥校验、整库解密。

参数依据（公开资料，微信 4.0+ Windows）：
  SQLCipher v4：page_size=4096，PBKDF2-HMAC-SHA256 256000 次迭代，
  每页尾部 reserve = IV(16) + HMAC(32)（HMAC-SHA256）= 48 字节。
  页 1 明文以标准 SQLite 头 "SQLite format 3\\x00" 开始——这是密钥校验的判据。

两种密钥模型都支持（M0 自动判别）：
  raw 模型：内存中的 32 字节即 AES 派生密钥（跳过 PBKDF2）
  pass 模型：内存中的 32 字节是口令，需先 PBKDF2 派生
"""
import hashlib
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SIZE = 4096
IV_LEN = 16
SQLITE_MAGIC = b"SQLite format 3\x00"
KDF_ITER = 256000
# 每页尾部 reserve = IV(16) + HMAC 长度。HMAC-SHA256 -> 48（4.0 资料值），
# HMAC-SHA512 -> 80；另留 16/32/64/96 兜底（如 HMAC 关闭）。扫描时逐一尝试。
RESERVE_CANDIDATES = (48, 16, 80, 32, 64, 96)

# 解密后页 1 首块 = SQLite 头偏移 16..31：
#   [0:2]=page_size 大端 0x1000(4096)，[5:8]=64/32/32（max/min payload、leaf 空闲比）
# 注意：SQLCipher 把文件头 16 字节魔数替换为盐值，"SQLite format 3" 不在解密块里！
HEADER_FINGERPRINT = {0: 0x10, 1: 0x00, 5: 0x40, 6: 0x20, 7: 0x20}


def derive_cipher_key(passphrase: bytes, salt: bytes, kdf_iter: int = KDF_ITER) -> bytes:
    """口令 -> AES 密钥（SQLCipher v4: PBKDF2-HMAC-SHA256）。"""
    return hashlib.pbkdf2_hmac("sha256", passphrase, salt, kdf_iter, dklen=32)


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


def _head_ok(plain_head: bytes) -> bool:
    return plain_head[:16] == SQLITE_MAGIC


def check_raw_key(key32: bytes, page1: bytes, reserve: int) -> bool:
    """候选密钥按“已是 AES 密钥”校验（开销 ~10us，用于内存扫描）。"""
    if len(key32) != 32:
        return False
    iv = page1[PAGE_SIZE - reserve:PAGE_SIZE - reserve + IV_LEN]
    try:
        return _head_ok(aes_cbc_decrypt(key32, iv, page1[16:16 + 32]))
    except Exception:
        return False


def check_passphrase(pw32: bytes, page1: bytes, reserve: int, kdf_iter: int = KDF_ITER) -> bool:
    """候选密钥按“口令”校验（需一次 PBKDF2，开销 ~0.2s，仅用于少量候选）。"""
    salt = page1[:16]
    return check_raw_key(derive_cipher_key(pw32, salt, kdf_iter), page1, reserve)


def load_page1(db_path: str) -> bytes:
    with open(db_path, "rb") as f:
        return f.read(PAGE_SIZE)


def decrypt_db(src: str, dst: str, key32: bytes, reserve: int = 48,
               page_size: int = PAGE_SIZE) -> str:
    """整库解密为普通 SQLite 文件。key32 为 AES 密钥（raw）或已派生密钥。"""
    size = os.path.getsize(src)
    n_pages = size // page_size
    out = bytearray()
    with open(src, "rb") as f:
        for i in range(n_pages):
            page = f.read(page_size)
            if len(page) < page_size:
                break
            start = 16 if i == 0 else 0
            enc = page[start:page_size - reserve]
            iv = page[page_size - reserve:page_size - reserve + IV_LEN]
            pt = aes_cbc_decrypt(key32, iv, enc)
            out += pt
            out += b"\x00" * reserve  # 补齐页尾，保持 4096 对齐
    with open(dst, "wb") as f:
        f.write(out)
    return dst
