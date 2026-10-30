# -*- coding: utf-8 -*-
"""图片 OCR：把聊天图片里的文字提取出来写入 media_text 并参与检索。

依赖：rapidocr-onnxruntime（离线，中文效果好，CPU 即可）
用法：python run_ocr.py [--limit 0] [--recent-days 0]
  重复运行自动跳过已处理的消息。
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import media_lookup  # noqa: E402

DB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                  "data", "knowledge.db"))


def ensure_tables(con):
    con.execute("CREATE TABLE IF NOT EXISTS media_text("
                "message_id INTEGER PRIMARY KEY, kind TEXT, text TEXT, ts INTEGER)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5("
                "text, message_id UNINDEXED, tokenize='trigram')")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--recent-days", type=int, default=0, help="只处理最近 N 天的图片")
    args = ap.parse_args()

    from rapidocr_onnxruntime import RapidOCR
    ocr = RapidOCR()

    con = sqlite3.connect(args.db, check_same_thread=False)
    ensure_tables(con)
    done = {r[0] for r in con.execute("SELECT message_id FROM media_text WHERE kind='ocr'")}
    q = "SELECT id FROM message WHERE type=3"
    if args.recent_days:
        q += f" AND ts >= {int(time.time()) - args.recent_days * 86400}"
    q += " ORDER BY ts DESC"
    ids = [r[0] for r in con.execute(q)]
    todo = [i for i in ids if i not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[*] 图片 {len(ids)} 张，待 OCR {len(todo)} 张")

    ok = empty = 0
    t0 = time.time()
    for n, mid in enumerate(todo, 1):
        try:
            img = media_lookup.image_bytes(con, mid)
            if not img:
                continue
            res, _ = ocr(img)
            text = "\n".join(x[1] for x in res) if res else ""
            if not text.strip():
                empty += 1
                con.execute("INSERT OR REPLACE INTO media_text(message_id,kind,text,ts) "
                            "VALUES(?,?,?,?)", (mid, "ocr", "", int(time.time())))
                continue
            con.execute("INSERT OR REPLACE INTO media_text(message_id,kind,text,ts) "
                        "VALUES(?,?,?,?)", (mid, "ocr", text[:4000], int(time.time())))
            con.execute("DELETE FROM media_fts WHERE message_id=?", (mid,))
            con.execute("INSERT INTO media_fts(text, message_id) VALUES(?,?)", (text[:4000], mid))
            ok += 1
        except Exception as e:
            print(f"  [!] msg {mid}: {e}")
        if n % 20 == 0:
            con.commit()
            rate = n / max(1e-6, time.time() - t0)
            print(f"  [进度] {n}/{len(todo)}  有文字 {ok} / 空白 {empty}  {rate:.1f} 张/秒",
                  flush=True)
    con.commit()
    con.close()
    print(f"[+] OCR 完成：有文字 {ok}，空白 {empty}，用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
