# -*- coding: utf-8 -*-
"""本地主库的逻辑变更捕获（CDC）。

背景：knowledge.db 里现有的 live_rid:* / qq_rt_rid_* / sync_watermark 只描述
「微信/QQ 源库 → 本地库」的入站进度，无法用于「本地库 → 云端」。这里给 18 张
知识表加 AFTER trigger，统一记入 _cloud_cdc(lsn)，作为出站同步的唯一水位。

虚表（fts5 / vec0）不建 trigger —— 云端按逻辑行重建索引。
ai_qa / ai_session / qq_message / qq_conv 属云端独立会话，明确排除。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

# 表 -> 主键列（顺序即 pk 字符串拼接顺序）。已按真实 DDL 核对。
SYNC_TABLES: dict[str, tuple[str, ...]] = {
    "message": ("id",),
    "chat": ("username",),
    "contact": ("username",),
    "media_text": ("message_id",),
    "file_text": ("key",),
    "chat_link": ("url",),
    "link_cache": ("url",),
    "qq_local_message": ("id",),
    "qq_chat": ("conv",),
    "qq_contact": ("key",),
    "qq_file": ("id",),
    "qq_file_text": ("file_id",),
    "qq_link": ("id",),
    "sns_post": ("tid",),
    "sns_comment": ("id",),
    "sns_media_ocr": ("tid", "media_idx", "kind"),
    "vec_done": ("message_id",),
    "vec_media_synced": ("message_id",),
}

DDL = [
    """CREATE TABLE IF NOT EXISTS _cloud_cdc(
         lsn INTEGER PRIMARY KEY AUTOINCREMENT,
         tbl TEXT NOT NULL,
         pk  TEXT NOT NULL,
         op  TEXT NOT NULL,
         ts  INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_cloud_cdc_lsn ON _cloud_cdc(lsn)",
    """CREATE TABLE IF NOT EXISTS _cloud_sync_state(
         k TEXT PRIMARY KEY,
         v TEXT NOT NULL)""",
]


def _pk_expr(cols: tuple[str, ...], row: str) -> str:
    parts = [f"CAST({row}.{c} AS TEXT)" for c in cols]
    return " || '|' || ".join(parts) if len(parts) > 1 else parts[0]


def _trigger_sql(table: str, cols: tuple[str, ...]) -> list[str]:
    out = []
    for op, when, row in (("ai", "INSERT", "NEW"), ("au", "UPDATE", "NEW"),
                          ("ad", "DELETE", "OLD")):
        letter = {"ai": "I", "au": "U", "ad": "D"}[op]
        out.append(
            f"CREATE TRIGGER IF NOT EXISTS cdc_{table}_{op} "
            f"AFTER {when} ON {table} BEGIN "
            f"INSERT INTO _cloud_cdc(tbl,pk,op,ts) "
            f"VALUES('{table}', {_pk_expr(cols, row)}, '{letter}', "
            f"strftime('%s','now')); END")
    return out


def install_triggers(con: sqlite3.Connection) -> int:
    for stmt in DDL:
        con.execute(stmt)
    n = 0
    for table, cols in SYNC_TABLES.items():
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        for stmt in _trigger_sql(table, cols):
            con.execute(stmt)
            n += 1
    con.commit()
    return n


def drop_triggers(con: sqlite3.Connection, keep_tables: bool = True) -> int:
    n = 0
    for table in SYNC_TABLES:
        for op in ("ai", "au", "ad"):
            con.execute(f"DROP TRIGGER IF EXISTS cdc_{table}_{op}")
            n += 1
    if not keep_tables:
        con.execute("DROP TABLE IF EXISTS _cloud_cdc")
        con.execute("DROP TABLE IF EXISTS _cloud_sync_state")
    con.commit()
    return n


def get_state(con: sqlite3.Connection, key: str, default: str = "") -> str:
    r = con.execute("SELECT v FROM _cloud_sync_state WHERE k=?", (key,)).fetchone()
    return str(r[0]) if r else default


def set_state(con: sqlite3.Connection, key: str, value) -> None:
    con.execute("INSERT OR REPLACE INTO _cloud_sync_state(k,v) VALUES(?,?)",
                (key, str(value)))
    con.commit()


def schema_sig(con: sqlite3.Connection) -> str:
    """白名单表结构指纹：本地建表/加列后云端必须重建基线，不能盲目 apply。"""
    h = hashlib.sha256()
    for table in sorted(SYNC_TABLES):
        cols = con.execute(f'PRAGMA table_info("{table}")').fetchall()
        h.update(table.encode())
        h.update(json.dumps([(c[1], c[2], c[5]) for c in cols],
                            ensure_ascii=False).encode())
    return h.hexdigest()[:32]


def _row_by_pk(con: sqlite3.Connection, table: str, pk: str):
    cols = SYNC_TABLES[table]
    where = " AND ".join(f'"{c}"=?' for c in cols)
    vals = pk.split("|", len(cols) - 1) if len(cols) > 1 else [pk]
    # 整数主键用字符串比较在 SQLite 里也能命中（亲和转换），显式转换更稳
    conv = []
    for c, v in zip(cols, vals):
        try:
            conv.append(int(v))
        except ValueError:
            conv.append(v)
    cur = con.execute(f'SELECT * FROM "{table}" WHERE {where}', conv)
    r = cur.fetchone()
    if r is None:
        return None
    return dict(zip([d[0] for d in cur.description], r))


def collect(con: sqlite3.Connection, after_lsn: int,
            limit: int = 20000) -> tuple[list[dict], int]:
    """按 lsn 导出变更行；同一主键多次变更只保留最后一次（按包大小收敛）。"""
    events = con.execute(
        "SELECT lsn,tbl,pk,op FROM _cloud_cdc WHERE lsn>? ORDER BY lsn LIMIT ?",
        (after_lsn, limit)).fetchall()
    if not events:
        return [], after_lsn
    last_lsn = int(events[-1][0])
    latest: dict[tuple[str, str], tuple[int, str]] = {}
    for lsn, tbl, pk, op in events:
        latest[(tbl, str(pk))] = (int(lsn), op)
    changes = []
    for (tbl, pk), (lsn, op) in sorted(latest.items(), key=lambda x: x[1][0]):
        if tbl not in SYNC_TABLES:
            continue
        changes.append({
            "lsn": lsn, "tbl": tbl, "pk": pk, "op": op,
            "row": None if op == "D" else _row_by_pk(con, tbl, pk),
        })
    return changes, last_lsn


def prune(con: sqlite3.Connection, upto_lsn: int) -> int:
    cur = con.execute("DELETE FROM _cloud_cdc WHERE lsn<=?", (upto_lsn,))
    con.commit()
    return cur.rowcount
