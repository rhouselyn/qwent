# -*- coding: utf-8 -*-
"""只重建 FTS 索引（不清空消息表），用于清洗规则变更后快速生效。

用法：python rebuild_fts.py [--db ../../data/knowledge.db]
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from textutil import search_text  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="../../data/knowledge.db")
    args = ap.parse_args()
    db = args.db if os.path.isabs(args.db) else \
        os.path.abspath(os.path.join(os.path.dirname(__file__), args.db))
    con = sqlite3.connect(db)
    t0 = time.time()
    con.execute("DELETE FROM message_fts")
    total, kept = 0, 0
    batch = []
    rows = con.execute("SELECT id, type, content, chat_user, "
                       "(SELECT is_group FROM chat WHERE chat.username = message.chat_user) "
                       "FROM message").fetchall()
    for mid, typ, content, chat_user, is_group in rows:
        total += 1
        st = search_text(typ, content)
        if len(st.strip()) < 2:
            continue
        batch.append((mid, st[:20000], chat_user, is_group or 0))
        kept += 1
        if len(batch) >= 5000:
            con.executemany("INSERT INTO message_fts(rowid, content, chat_user, is_group) "
                            "VALUES(?,?,?,?)", batch)
            batch.clear()
    if batch:
        con.executemany("INSERT INTO message_fts(rowid, content, chat_user, is_group) "
                        "VALUES(?,?,?,?)", batch)
    con.commit()
    print(f"[+] FTS 重建完成：{kept}/{total} 条可检索，{time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
