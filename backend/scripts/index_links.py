# -*- coding: utf-8 -*-
"""批量索引聊天里的链接/公众号文章：抓正文 → 存 link_cache → 建 link_fts。

用法：python index_links.py [--limit 50] [--recent-days 0] [--force]
  可重复运行：已缓存且已入索引的链接会跳过。
"""
import argparse
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

DB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                  "data", "knowledge.db"))
URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]，。；：！？、]+")


def ensure(con):
    con.execute("CREATE TABLE IF NOT EXISTS link_cache("
                "url TEXT PRIMARY KEY, title TEXT, text TEXT, ts INTEGER)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS link_fts USING fts5("
                "text, title UNINDEXED, url UNINDEXED, tokenize='trigram')")


SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
            ".mp4", ".mov", ".mp3", ".m4a", ".wav", ".amr", ".zip", ".rar", ".7z",
            ".exe", ".apk", ".css", ".js", ".woff", ".ttf", ".pdf", ".doc", ".docx",
            ".xls", ".xlsx", ".ppt", ".pptx")


BLOCK_KEYWORDS = ("stodownload", "wxapp.", "mmbiz_", "res.wx.qq.com", "qpic.cn",
                   "szmmsns", "wxapp.tc.qq.com", "/cdn/", "cdn.", "douyin.com",
                   "bilibili.com/video", "y.qq.com", "mp4", "m3u8", "/image", "/img",
                   "static.", "/static/", "/assets/", ".js?v=", "captcha", "login")


def is_article_url(u: str) -> bool:
    low = u.lower().split("?")[0]
    if low.endswith(SKIP_EXT):
        return False
    ul = u.lower()
    if any(k in ul for k in BLOCK_KEYWORDS):
        return False
    if "mp.weixin.qq.com/s" in ul:
        return True
    # 其余只保留看起来像文章页的（有路径层级或 html/php 结尾）
    path = low.split("://")[-1]
    if "/" not in path:
        return False
    return any(path.endswith(ext) for ext in ("", "/", ".html", ".htm", ".php", ".shtml"))


def extract_urls(content: str) -> list[str]:
    import html as _h
    if not content:
        return []
    c = _h.unescape(content)
    out = []
    for u in URL_RE.findall(c):
        u = u.rstrip(".,;:!?)\\]")
        if "weixin.qq.com" in u or "mp.weixin" in u or u.startswith("http"):
            out.append(u)
    # 微信 appmsg 的 <url> / <lowurl> 也可能被 CDATA 包裹
    out.extend(re.findall(r"<url>\s*(?:<!\[CDATA\[)?(https?://[^\]<]+)", c))
    return list(dict.fromkeys(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--recent-days", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    import main as M

    con = sqlite3.connect(args.db, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    ensure(con)

    q = ("SELECT id, ts, content FROM message WHERE (type=49 OR content LIKE '%http%')")
    if args.recent_days:
        q += f" AND ts >= {int(time.time()) - args.recent_days * 86400}"
    q += " ORDER BY ts DESC LIMIT 20000"
    rows = con.execute(q).fetchall()

    urls: list[tuple[str, int]] = []
    seen: set[str] = set()
    for _mid, ts, content in rows:
        for u in extract_urls(content):
            if u not in seen and is_article_url(u):
                seen.add(u)
                urls.append((u, ts))
    print(f"[*] 发现 {len(urls)} 个不重复链接")

    urls.sort(key=lambda x: (0 if "mp.weixin.qq.com/s" in x[0] else 1, -x[1]))
    indexed = {r[0] for r in con.execute(
        "SELECT url FROM link_cache WHERE length(text) > 50")}
    titles = {r[0] for r in con.execute(
        "SELECT title FROM link_cache WHERE length(title) > 6")}
    todo = [(u, ts) for u, ts in urls if args.force or u not in indexed]
    deduped = []
    for u, ts in todo:
        # 同一标题只抓一次（微信同一文章常有多个带追踪参数的链接）
        guess = ""
        for _mid, t2, content in ():
            pass
        deduped.append((u, ts))
    todo = deduped
    if args.limit:
        todo = todo[:args.limit]
    print(f"[*] 已索引 {len(indexed)}，本轮处理 {len(todo)}")

    ok = fail = 0
    t0 = time.time()
    for n, (u, ts) in enumerate(todo, 1):
        d = M.fetch_article(u, force=args.force)
        if d.get("error") or not (d.get("text") or "").strip():
            fail += 1
            print(f"  [跳过] {u[:70]} {d.get('error') or '空正文'}")
        else:
            title = (d.get("title") or "").strip()
            if title and title in titles:
                fail += 1
                print(f"  [重复] {title[:40]}")
                con.execute("INSERT OR REPLACE INTO link_cache(url,title,text,ts) "
                            "VALUES(?,?,?,?)", (u, title, d["text"], int(time.time())))
                con.commit()
                continue
            titles.add(title)
            con.execute("UPDATE link_cache SET msg_ts=? WHERE url=? AND IFNULL(msg_ts,0)=0",
                        (ts or 0, u))
            con.execute("DELETE FROM link_fts WHERE url=?", (u,))
            con.execute("INSERT INTO link_fts(text, title, url) VALUES(?,?,?)",
                        (d["text"][:40000], title, u))
            con.commit()
            ok += 1
        if n % 5 == 0:
            print(f"  [进度] {n}/{len(todo)} 成功 {ok} 失败 {fail}",
                  flush=True)
        time.sleep(1.2)          # 对站点友好，避免被封
    con.commit()
    con.close()
    print(f"[+] 完成：成功 {ok}，失败 {fail}，用时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
