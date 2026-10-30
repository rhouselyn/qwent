# -*- coding: utf-8 -*-
"""建立消息向量索引（可断点续跑）。

用法：python build_vectors.py [--batch 64] [--limit 0]
  重复运行只补未嵌入的消息。
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import vector  # noqa: E402
from textutil import search_text  # noqa: E402

DB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                  "data", "knowledge.db"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（调试用）")
    args = ap.parse_args()

    if not vector.available():
        print("[!] 未找到模型目录 models/bge-small-zh")
        sys.exit(1)

    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".build_vectors.lock")
    try:
        import msvcrt
        lock_fh = open(lock_path, "w")
        msvcrt.locking(lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
    except Exception:
        print("[!] 已有一个向量索引任务在运行，退出")
        return
    con = vector.connect(args.db, timeout=60.0)
    con.execute("PRAGMA busy_timeout=60000")
    vector.ensure_table(con)
    done = set(r[0] for r in con.execute("SELECT message_id FROM msg_vec"))
    print(f"[*] 已嵌入 {len(done)} 条，开始补齐...")

    todo = con.execute(
        "SELECT id, type, content FROM message WHERE noise=0 ORDER BY id").fetchall()
    todo = [(mid, typ, content) for mid, typ, content in todo
            if mid not in done and len(search_text(typ, content).strip()) >= 4]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[*] 待嵌入 {len(todo)} 条")

    t0 = time.time()
    inserted = 0
    for i in range(0, len(todo), args.batch):
        chunk = todo[i:i + args.batch]
        texts = [search_text(typ, content) for _, typ, content in chunk]
        vecs = vector.embed(texts, batch_size=args.batch)
        ids = [chunk[j][0] for j in range(len(chunk))]
        con.executemany("DELETE FROM msg_vec WHERE message_id=?", [(i,) for i in ids])
        con.executemany("INSERT INTO msg_vec(message_id, embedding) VALUES(?,?)",
                        [(ids[j], vecs[j].tobytes()) for j in range(len(chunk))])
        con.commit()
        inserted += len(chunk)
        if inserted % 5000 < args.batch:
            rate = inserted / max(1e-6, time.time() - t0)
            eta = (len(todo) - inserted) / max(1e-6, rate) / 60
            print(f"  [进度] {inserted}/{len(todo)}  {rate:.0f} 条/秒  剩余约 {eta:.1f} 分钟",
                  flush=True)
    con.close()
    print(f"[+] 完成：新增 {inserted} 条，用时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
