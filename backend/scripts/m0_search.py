# -*- coding: utf-8 -*-
"""M0-② 检索 demo：FTS5 关键词检索（BM25），后续叠加向量检索。

用法：
  python m0_search.py "报销口径" [--chat 群名或用户名] [--days 30] [--n 10]
  python m0_search.py "上线时间 定了" --context 2   # 显示命中消息前后各2条
"""
import argparse
import os
import sqlite3
import time

from m0_import import ensure_schema  # noqa: F401  (保证同构 schema)


def display_name(con, username):
    r = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat WHERE username=?",
                    (username,)).fetchone()
    return r[0] if r else username


def sender_name(con, chat_user, sender, is_sender):
    if is_sender == 1 or not sender:
        return "我"
    r = con.execute("SELECT COALESCE(remark, nick_name, username) FROM contact WHERE username=?",
                    (sender,)).fetchone()
    return r[0] if r else (sender or "??")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--chat", default=None, help="限定会话（昵称/群名/用户名，模糊匹配）")
    ap.add_argument("--days", type=int, default=None, help="限定最近 N 天")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--context", type=int, default=0, help="显示命中前后各 N 条")
    args = ap.parse_args()

    db = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data",
                                      "knowledge.db"))
    con = sqlite3.connect(db)

    chat_filter, params = "", []
    if args.chat:
        chat_filter = "AND m.chat_user IN (SELECT username FROM chat WHERE display_name LIKE ?)"
        params.append(f"%{args.chat}%")
    if args.days:
        chat_filter += f" AND m.ts >= {int(time.time()) - args.days * 86400}"

    words = args.query.split() or [args.query]
    fts_words = [w for w in words if len(w) >= 3]
    like_words = [w for w in words if 0 < len(w) < 3]
    rows = []
    if fts_words:
        q = " OR ".join(f'"{w}"' for w in fts_words)
        sql = f"""
        SELECT m.id, m.chat_user, m.is_sender, m.sender, m.ts, m.type, m.content
        FROM message_fts f JOIN message m ON m.id = f.rowid
        WHERE message_fts MATCH ? {chat_filter}
        ORDER BY bm25(message_fts) LIMIT ?
        """
        try:
            rows = con.execute(sql, [q] + params + [args.n]).fetchall()
        except sqlite3.OperationalError as e:
            print(f"[!] FTS 查询失败: {e}")
    if like_words:
        lconds = [f"m.content LIKE ?" for _ in like_words]
        sql = f"""
        SELECT m.id, m.chat_user, m.is_sender, m.sender, m.ts, m.type, m.content
        FROM message m WHERE {' AND '.join(lconds)} {chat_filter}
        ORDER BY ts DESC LIMIT ?
        """
        rows += con.execute(sql, [f"%{w}%" for w in like_words] + params + [args.n]).fetchall()
    seen, uniq = set(), []
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0])
            uniq.append(r)
    rows = uniq[:args.n]

    print(f"[检索] 「{args.query}」 命中 {len(rows)} 条\n")
    for mid, chat_user, is_sender, sender, ts, typ, content in rows:
        who = sender_name(con, chat_user, sender, is_sender)
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts or 0))
        print(f"[{display_name(con, chat_user)}] {t} {who}: "
              f"{content[:120].replace(chr(10), ' ')}")
        if args.context:
            for c in con.execute(
                """SELECT is_sender, sender, ts, content FROM message
                   WHERE chat_user=? AND id BETWEEN ? AND ? AND id != ?
                   ORDER BY id""",
                    (chat_user, mid - args.context, mid + args.context, mid)):
                ct = time.strftime("%H:%M", time.localtime(c[2] or 0))
                print(f"    └ {ct} {sender_name(con, chat_user, c[1], c[0])}: "
                      f"{c[3][:80].replace(chr(10), ' ')}")
        print()


if __name__ == "__main__":
    main()
