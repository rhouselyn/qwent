# -*- coding: utf-8 -*-
"""M0-① 打印解密后的库 schema 和样本数据，用于编写导入器。"""
import argparse
import os
import sqlite3
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decrypted-dir", default="../../data/decrypted")
    ap.add_argument("--db", default=None, help="只看某个库")
    ap.add_argument("--sample", type=int, default=3)
    args = ap.parse_args()

    d = args.decrypted_dir if os.path.isabs(args.decrypted_dir) else \
        os.path.abspath(os.path.join(os.path.dirname(__file__), args.decrypted_dir))
    files = [args.db] if args.db else sorted(
        f for f in os.listdir(d) if f.endswith(".db"))

    for name in files:
        p = os.path.join(d, name)
        if not os.path.exists(p):
            print(f"[!] 不存在: {p}")
            continue
        print(f"\n{'='*70}\n== {name} ({os.path.getsize(p)/1048576:.1f} MB)\n{'='*70}")
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        except Exception as e:
            print(f"    打开失败: {e}")
            continue
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        print("tables:", tables)
        for t in tables:
            try:
                cols = [r[1] for r in con.execute(f"PRAGMA table_info('{t}')")]
                cnt = con.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
                print(f"\n  -- {t} ({cnt} rows): {', '.join(cols)}")
                if cnt and any(k in t.lower() for k in ("msg", "session", "contact")):
                    for row in con.execute(f"SELECT * FROM '{t}' LIMIT {args.sample}"):
                        s = str(row)
                        print("     ", s[:400])
            except Exception as e:
                print(f"     (error: {e})")
        con.close()


if __name__ == "__main__":
    main()
