# -*- coding: utf-8 -*-
"""M0-① 解密微信数据库。

用法：
  python m0_decrypt.py --account-dir <...> --key <hex> [--reserve 48] \
      [--dbs db_storage/message/message_0.db ...] [--out-dir ../../data/decrypted]
不指定 --dbs 时解密 message_0..4、contact、session、media_0..1。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m0lib import RESERVE_CANDIDATES, check_raw_key, decrypt_db, load_page1  # noqa: E402

DEFAULT_DBS = [
    "db_storage/message/message_0.db",
    "db_storage/message/message_1.db",
    "db_storage/message/message_2.db",
    "db_storage/message/message_3.db",
    "db_storage/message/message_4.db",
    "db_storage/contact/contact.db",
    "db_storage/session/session.db",
    "db_storage/message/media_0.db",
    "db_storage/message/media_1.db",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-dir", required=True)
    ap.add_argument("--key", help="32 字节密钥 hex；不填则读 keys.json")
    ap.add_argument("--keys-file", default="../../data/keys.json")
    ap.add_argument("--reserve", type=int, default=None)
    ap.add_argument("--dbs", nargs="*", default=DEFAULT_DBS)
    ap.add_argument("--out-dir", default="../../data/decrypted")
    args = ap.parse_args()

    key_hex, reserve = args.key, args.reserve
    if not key_hex:
        kf = args.keys_file if os.path.isabs(args.keys_file) else \
            os.path.abspath(os.path.join(os.path.dirname(__file__), args.keys_file))
        with open(kf, encoding="utf-8") as f:
            info = json.load(f)
        key_hex, reserve = info["key_hex"], info.get("reserve") or 48
        print(f"[*] 使用 keys.json 的密钥 (reserve={reserve}, 来源={info.get('kind')})")
    key = bytes.fromhex(key_hex)

    out_dir = args.out_dir if os.path.isabs(args.out_dir) else \
        os.path.abspath(os.path.join(os.path.dirname(__file__), args.out_dir))
    os.makedirs(out_dir, exist_ok=True)

    for rel in args.dbs:
        src = os.path.join(args.account_dir, rel)
        if not os.path.exists(src):
            print(f"[!] 跳过（不存在）: {rel}")
            continue
        p1 = load_page1(src)
        rs = [reserve] if reserve in RESERVE_CANDIDATES else list(RESERVE_CANDIDATES)
        if not any(check_raw_key(key, p1, r) for r in rs):
            print(f"[!] 密钥不适用于 {rel}，跳过")
            continue
        r = next(r for r in rs if check_raw_key(key, p1, r))
        dst = os.path.join(out_dir, os.path.basename(rel))
        t0 = time.time()
        decrypt_db(src, dst, key, reserve=r)
        print(f"[+] {rel} -> {dst} ({os.path.getsize(dst)/1048576:.1f} MB, "
              f"reserve={r}, {time.time()-t0:.1f}s)")
        # 快速校验
        import sqlite3
        try:
            con = sqlite3.connect(dst)
            row = con.execute("PRAGMA quick_check(1)").fetchone()
            con.close()
            print(f"    quick_check: {row[0]}")
        except Exception as e:
            print(f"    quick_check 失败: {e}")


if __name__ == "__main__":
    main()
