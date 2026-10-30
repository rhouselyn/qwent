# -*- coding: utf-8 -*-
"""把 msg_vec 迁移为按 chat_user 分区的索引（复制向量，无需重新嵌入）。

分区后：会话内检索只扫描该会话的向量（毫秒级），全局检索仍全量。
用法：python migrate_vec_partition.py
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

DB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                  "data", "knowledge.db"))


def main():
    import sqlite_vec
    con = sqlite3.connect(DB, timeout=120)
    con.execute("PRAGMA busy_timeout=120000")
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)

    try:
        n = con.execute("SELECT COUNT(*) FROM msg_vec_p").fetchone()[0]
        print(f"[*] msg_vec_p 已存在（{n} 条），继续增量复制")
    except sqlite3.OperationalError:
        con.execute("""CREATE VIRTUAL TABLE msg_vec_p USING vec0(
            message_id integer primary key,
            embedding float[512],
            chat_user text partition key)""")
        con.commit()

    done = set(r[0] for r in con.execute("SELECT message_id FROM msg_vec_p"))
    print(f"[*] 已迁移 {len(done)} 条")
    t0 = time.time()
    batch = []
    copied = 0
    cur = con.execute("""SELECT v.message_id, v.embedding, m.chat_user
                         FROM msg_vec v JOIN message m ON m.id = v.message_id""")
    for mid, emb, chat in cur:
        if mid in done:
            continue
        batch.append((mid, emb, chat))
        if len(batch) >= 2000:
            con.executemany("INSERT INTO msg_vec_p(message_id, embedding, chat_user) "
                            "VALUES(?,?,?)", batch)
            con.commit()
            copied += len(batch)
            batch.clear()
            print(f"  [进度] {copied} 条 / {time.time()-t0:.0f}s", flush=True)
    if batch:
        con.executemany("INSERT INTO msg_vec_p(message_id, embedding, chat_user) "
                        "VALUES(?,?,?)", batch)
        con.commit()
        copied += len(batch)
    con.close()
    print(f"[+] 迁移完成：新增 {copied}，用时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
