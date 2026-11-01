# -*- coding: utf-8 -*-
"""文件正文批量抽取（设置页「索引任务」可启动，可断点续跑）。

遍历全部 type=49/ subtype=6 文件消息，把本地已下载文件的正文抽进 file_text
（未下载/失败的写空标记，48h 内可重试），供检索与 AI 读取。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP.parent.parent / "refs" / "ChatTrace" / "src"))

if os.name == "nt" and sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent-days", type=int, default=0, help="只处理最近 N 天,0=全量")
    args = ap.parse_args()

    import main as _m
    from textutil import appmsg_subtype

    con = _m.db()
    _m._ensure_file_text(con)
    since = int(time.time()) - args.recent_days * 86400 if args.recent_days else 0
    rows = con.execute(
        "SELECT chat_user, src_local_id, ts, content FROM message "
        "WHERE ts>=? AND noise=0 AND (type & 0xFFFFFFFF)=49 AND src_local_id IS NOT NULL "
        "ORDER BY ts DESC", (since,)).fetchall()
    total = sum(1 for r in rows
                if appmsg_subtype(r["content"] or "") == "6")
    print(f"[进度] 0/{total}", flush=True)
    now = int(time.time())
    done = 0
    idx = 0

    def _mark(key, name, text):
        con.execute("INSERT OR REPLACE INTO file_text(key,name,text,ext,ts) VALUES(?,?,?,?,?)",
                    (key, name, text, "", now))

    for r in rows:
        if appmsg_subtype(r["content"] or "") != "6":
            continue
        idx += 1
        try:
            m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                          r["content"] or "", re.S)
            name = m.group(1).strip() if m else ""
            if not name:
                continue
            key = f"{r['chat_user']}:{r['src_local_id']}"
            hit = con.execute("SELECT text, ts FROM file_text WHERE key=?", (key,)).fetchone()
            if hit and (hit[0] or "").strip():
                continue                     # 已有正文
            if hit and (hit[1] or 0) and now - hit[1] > 48 * 3600:
                continue                     # 标记超过 48h，放弃
            p = _m.locate_file(r["chat_user"], r["src_local_id"], r["ts"], name)
            if not p:
                _mark(key, name, "")         # 还没下载
                continue
            text = _m.extract_text(p, 24000)
            if text and not text.startswith("[解析失败"):
                con.execute("INSERT OR REPLACE INTO file_text(key,name,text,ext,ts) "
                            "VALUES(?,?,?,?,?)",
                            (key, p.name, text, p.suffix.lower(), now))
                done += 1
            else:
                _mark(key, name, "")
        except Exception:
            continue
        finally:
            if idx % 20 == 0:
                print(f"[进度] {idx}/{total}", flush=True)
                con.commit()
    con.commit()
    print(f"[进度] {total}/{total}", flush=True)
    print(f"[files] 完成: 抽取正文 {done} 个文件", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
