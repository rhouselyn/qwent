# -*- coding: utf-8 -*-
"""QQ 机器人消息留存（QQ 模式「消息」页的数据源）。

QQ 官方机器人只能收到「发给它的私聊 / 群里 @ 它」的消息，所以 QQ 模式不
解密本地 QQ 数据库，而是把网关收到的每一条消息与回复落到本机 knowledge.db
的独立表里：既能回看，也不会和微信知识库的消息混在一起。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

MAX_ROWS = 8000

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS qq_message(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         conv TEXT NOT NULL,
         scene TEXT NOT NULL DEFAULT 'c2c',
         openid TEXT DEFAULT '',
         group_openid TEXT DEFAULT '',
         sender_name TEXT DEFAULT '',
         direction INTEGER NOT NULL DEFAULT 0,   -- 0=收到 1=助手回复
         content TEXT DEFAULT '',
         media TEXT DEFAULT '',                  -- 附件 JSON（图片/语音/文件/视频）
         msg_id TEXT DEFAULT '',
         ts INTEGER NOT NULL
       )""",
    "CREATE INDEX IF NOT EXISTS idx_qq_msg_conv ON qq_message(conv, id)",
    "CREATE INDEX IF NOT EXISTS idx_qq_msg_ts ON qq_message(ts)",
    """CREATE TABLE IF NOT EXISTS qq_conv(
         conv TEXT PRIMARY KEY,
         name TEXT NOT NULL,
         updated INTEGER NOT NULL
       )""",
]

_ready = False
_writes = 0


def ensure_schema(con: sqlite3.Connection) -> None:
    global _ready
    if _ready:
        return
    for sql in SCHEMA:
        try:
            con.execute(sql)
        except sqlite3.OperationalError:
            pass
    # 旧库升级：media 列用于展示 QQ 图片 / 语音 / 文件
    cols = {r[1] for r in con.execute("PRAGMA table_info(qq_message)")}
    if "media" not in cols:
        try:
            con.execute("ALTER TABLE qq_message ADD COLUMN media TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    con.commit()
    _ready = True



def _clean(s, cap=120) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:cap]


def conv_of(t: str, d: dict) -> tuple[str, str, str, str, str]:
    """返回 (conv, scene, openid, group_openid, sender_name)。"""
    is_group = t != "C2C_MESSAGE_CREATE"
    author = d.get("author") or {}
    group_openid = str(d.get("group_openid") or "")
    openid = str((author.get("member_openid") if is_group
                  else author.get("user_openid")) or "")
    scene = "group" if is_group else "c2c"
    conv = f"group:{group_openid}" if is_group else f"c2c:{openid}"
    name = str(author.get("nickname") or author.get("username") or "")
    if not name:
        name = (f"QQ群 {group_openid[:6]}" if is_group else f"QQ用户 {openid[:6]}")
    return conv, scene, openid, group_openid, _clean(name, 40)


def record_from_event(con: sqlite3.Connection, t: str, d: dict, content: str,
                      direction: int, msg_id: str = "",
                      media: list | None = None) -> None:
    """direction=0 用户发来；direction=1 助手回复。异常绝不影响网关收发。"""
    global _writes
    try:
        ensure_schema(con)
        conv, scene, openid, group_openid, name = conv_of(t, d)
        if not conv or conv.endswith(":"):
            return
        ts = int(time.time())
        try:
            ts = int(d.get("timestamp") or 0) // 1000 or ts
        except Exception:
            pass
        con.execute(
            "INSERT INTO qq_message(conv,scene,openid,group_openid,sender_name,"
            "direction,content,media,msg_id,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (conv, scene, openid, group_openid,
             name if direction == 0 else "助手", int(direction),
             str(content or "")[:8000],
             json.dumps(media, ensure_ascii=False) if media else "",
             str(msg_id or "")[:80], ts))
        con.commit()
        _writes += 1
        if _writes % 100 == 0:
            prune(con)
    except Exception:
        pass


def enrich(con: sqlite3.Connection, msg_id: str, content: str,
           media: list | None = None) -> None:
    """附件下载/OCR/转写完成后补写同一行；先落原文，避免下载失败丢消息。"""
    if not msg_id:
        return
    try:
        ensure_schema(con)
        row = con.execute(
            "SELECT id FROM qq_message WHERE msg_id=? AND direction=0 "
            "ORDER BY id DESC LIMIT 1", (str(msg_id)[:80],)).fetchone()
        if not row:
            return
        con.execute(
            "UPDATE qq_message SET content=?, media=? WHERE id=?",
            (str(content or "")[:8000],
             json.dumps(media, ensure_ascii=False) if media else "",
             row[0]))
        con.commit()
    except Exception:
        pass



def prune(con: sqlite3.Connection) -> None:
    try:
        n = con.execute("SELECT COUNT(*) FROM qq_message").fetchone()[0]
        if n > MAX_ROWS:
            con.execute("DELETE FROM qq_message WHERE id IN "
                        "(SELECT id FROM qq_message ORDER BY id LIMIT ?)",
                        (n - MAX_ROWS,))
            con.commit()
    except Exception:
        pass


def conversations(con: sqlite3.Connection) -> list[dict]:
    ensure_schema(con)
    rows = con.execute(
        "SELECT m.conv AS conv, MAX(m.scene) AS scene, MAX(m.ts) AS last_ts, "
        "COUNT(*) AS cnt, "
        "(SELECT content FROM qq_message x WHERE x.conv=m.conv "
        " ORDER BY x.id DESC LIMIT 1) AS last, "
        "(SELECT direction FROM qq_message x WHERE x.conv=m.conv "
        " ORDER BY x.id DESC LIMIT 1) AS last_dir "
        "FROM qq_message m GROUP BY m.conv ORDER BY last_ts DESC LIMIT 200").fetchall()
    names = {r[0]: r[1] for r in con.execute("SELECT conv,name FROM qq_conv")}
    out = []
    for r in rows:
        conv = r["conv"]
        scene = r["scene"] or "c2c"
        default = (f"QQ群 {conv.split(':', 1)[-1][:6]}" if scene == "group"
                   else f"QQ用户 {conv.split(':', 1)[-1][:6]}")
        out.append({
            "conv": conv, "scene": scene,
            "name": names.get(conv) or default,
            "custom": conv in names,
            "last_ts": r["last_ts"] or 0, "cnt": r["cnt"] or 0,
            "last": _clean(r["last"], 100),
            "last_dir": r["last_dir"] or 0,
        })
    return out


def rename(con: sqlite3.Connection, conv: str, name: str) -> str:
    ensure_schema(con)
    clean = _clean(name, 40)
    if not clean:
        raise ValueError("名称不能为空")
    con.execute("INSERT INTO qq_conv(conv,name,updated) VALUES(?,?,?) "
                "ON CONFLICT(conv) DO UPDATE SET name=excluded.name, "
                "updated=excluded.updated", (conv, clean, int(time.time())))
    con.commit()
    return clean


def messages(con: sqlite3.Connection, conv: str, limit: int = 200) -> list[dict]:
    ensure_schema(con)
    rows = con.execute(
        "SELECT id, sender_name, direction, content, media, ts FROM qq_message "
        "WHERE conv=? ORDER BY id DESC LIMIT ?", (conv, max(1, min(limit, 500)))).fetchall()
    out = []
    for r in reversed(rows):
        try:
            media = json.loads(r["media"] or "[]")
        except Exception:
            media = []
        out.append({"id": r["id"], "who": r["sender_name"] or "",
                    "is_self": bool(r["direction"]), "text": r["content"] or "",
                    "media": media if isinstance(media, list) else [],
                    "ts": r["ts"] or 0})
    return out


def search(con: sqlite3.Connection, q: str, limit: int = 50) -> list[dict]:
    """QQ 消息全文（LIKE）搜索；返回带会话名的消息列表。"""
    ensure_schema(con)
    q = str(q or "").strip()
    if not q:
        return []
    rows = con.execute(
        "SELECT m.id, m.conv, m.scene, m.sender_name, m.direction, m.content, "
        "m.media, m.ts FROM qq_message m WHERE m.content LIKE ? "
        "ORDER BY m.id DESC LIMIT ?",
        (f"%{q}%", max(1, min(limit, 200)))).fetchall()
    names = {r[0]: r[1] for r in con.execute("SELECT conv,name FROM qq_conv")}
    out = []
    for r in rows:
        conv = r["conv"]
        default = (f"QQ群 {conv.split(':', 1)[-1][:6]}" if r["scene"] == "group"
                   else f"QQ用户 {conv.split(':', 1)[-1][:6]}")
        try:
            media = json.loads(r["media"] or "[]")
        except Exception:
            media = []
        out.append({"id": r["id"], "conv": conv, "scene": r["scene"] or "c2c",
                    "conv_name": names.get(conv) or default,
                    "who": r["sender_name"] or ("助手" if r["direction"] else ""),
                    "is_self": bool(r["direction"]),
                    "snippet": _clean(r["content"], 160),
                    "ts": r["ts"] or 0, "media": media})
    return out


def overview(con: sqlite3.Connection) -> dict:
    ensure_schema(con)
    try:
        total = con.execute("SELECT COUNT(*) FROM qq_message").fetchone()[0] or 0
        convs = con.execute("SELECT COUNT(DISTINCT conv) FROM qq_message").fetchone()[0] or 0
        last = con.execute("SELECT MAX(ts) FROM qq_message").fetchone()[0] or 0
    except Exception:
        total, convs, last = 0, 0, 0
    return {"messages": total, "conversations": convs, "last_ts": last}
