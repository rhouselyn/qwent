# -*- coding: utf-8 -*-
"""M0-① 把解密后的微信 4.x 库导入 knowledge.db（项目自己的知识库）。

微信 4.0 schema 要点（来自 decrypt 后库的实际结构）：
  message_N.db: 每个会话一张表 Msg_{md5(username)}，列 local_id/local_type/
                create_time/message_content[，sender 等]；Name2Id 表反查 username
  contact.db:   contact(username, nick_name, remark, ...)
  session.db:   SessionTable(username, unread_count, summary, last_timestamp, ...)

用法：
  python m0_import.py [--decrypted-dir ../../data/decrypted] [--out ../../data/knowledge.db]
"""
import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time
import zlib

import zstandard

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
try:
    from textutil import display_text as _display_text, search_text as _search_text
except Exception:      # 兜底：脚本独立运行时退回原始内容
    def _display_text(t, c):
        return c or ""

    def _search_text(t, c):
        return c or ""

MSG_DBS = [f"message/message_{i}.db" for i in range(8)] + [f"message_{i}.db" for i in range(8)]
XML_TITLE_RE = re.compile(r"<title><!\[CDATA\[(.*?)\]\]></title>", re.S)
XML_DES_RE = re.compile(r"<des><!\[CDATA\[(.*?)\]\]></des>", re.S)
# 群文本消息正文以 "<wxid>:\n" 前缀标注实际发送人（比 Name2Id rowid 更可靠）
SENDER_PREFIX_RE = re.compile(r"^([^:\n]{4,64}):\r?\n")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_ZSTD = zstandard.ZstdDecompressor()


def decode_content(raw) -> str:
    """微信 4.x 的 message_content 多为 zstd 压缩（CT 标志=4），解压后才是真实内容。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if raw[:4] == ZSTD_MAGIC:
        try:
            raw = _ZSTD.decompress(raw)
        except Exception:
            try:
                raw = _ZSTD.decompressobj().decompress(raw)
            except Exception:
                return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "ignore")
    return str(raw)


def ensure_schema(con: sqlite3.Connection):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS contact(
        username TEXT PRIMARY KEY, nick_name TEXT, remark TEXT, alias TEXT,
        pin_yin_initial TEXT DEFAULT '', quan_pin TEXT DEFAULT '',
        local_type INTEGER DEFAULT 0, flag INTEGER DEFAULT 0,
        delete_flag INTEGER DEFAULT 0, initial_letter TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS chat(
        username TEXT PRIMARY KEY, is_group INTEGER, display_name TEXT,
        last_ts INTEGER, summary TEXT);
    CREATE TABLE IF NOT EXISTS message(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_user TEXT NOT NULL, is_sender INTEGER, sender TEXT,
        ts INTEGER, type INTEGER, content TEXT,
        src_db TEXT, src_table TEXT, src_local_id INTEGER);
    CREATE INDEX IF NOT EXISTS idx_msg_chat_ts ON message(chat_user, ts);
    CREATE INDEX IF NOT EXISTS idx_msg_ts ON message(ts);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
        content, chat_user UNINDEXED, is_group UNINDEXED, tokenize='trigram');
    """)


def import_contacts(dec_dir: str, con: sqlite3.Connection) -> int:
    p = os.path.join(dec_dir, "contact", "contact.db")
    if not os.path.exists(p):
        p = os.path.join(dec_dir, "contact.db")
    if not os.path.exists(p):
        print("[!] 缺 contact.db，跳过联系人")
        return 0
    src = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    cols = {r[1] for r in src.execute("PRAGMA table_info(contact)")}
    extra = [c for c in ("alias", "pin_yin_initial", "quan_pin", "local_type", "flag",
                        "delete_flag") if c in cols]
    sel = "username, nick_name, remark"
    rows = src.execute(f"SELECT {sel}, {', '.join(extra) or 'NULL'} FROM contact").fetchall()
    exp = {c: i + 3 for i, c in enumerate(extra)}
    n = 0
    for r in rows:
        uname = r[0]
        if not uname:
            continue
        g = lambda c: r[exp[c]] if c in exp else None
        con.execute(
            "INSERT OR REPLACE INTO contact(username, nick_name, remark, alias, "
            "pin_yin_initial, quan_pin, local_type, flag, delete_flag) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (uname, r[1], r[2], g("alias"), g("pin_yin_initial") or "",
             g("quan_pin") or "", g("local_type") or 0, g("flag") or 0,
             g("delete_flag") or 0))
        n += 1
    src.close()
    return n


def import_sessions(dec_dir: str, con: sqlite3.Connection) -> int:
    p = os.path.join(dec_dir, "session", "session.db")
    if not os.path.exists(p):
        p = os.path.join(dec_dir, "session.db")
    if not os.path.exists(p):
        print("[!] 缺 session.db，跳过会话列表")
        return 0
    src = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = src.execute(
            "SELECT username, summary, last_timestamp FROM SessionTable").fetchall()
    except sqlite3.OperationalError as e:
        print(f"[!] SessionTable 读取失败: {e}")
        return 0
    contacts = {u: n for u, n in con.execute(
        "SELECT username, COALESCE(NULLIF(TRIM(remark),''), NULLIF(TRIM(nick_name),''), username) "
        "FROM contact")}
    n = 0
    for uname, summary, ts in rows:
        if not uname:
            continue
        con.execute("INSERT OR REPLACE INTO chat(username,is_group,display_name,last_ts,summary)"
                    " VALUES(?,?,?,?,?)",
                    (uname, int("@chatroom" in uname or "@openim" in uname),
                     contacts.get(uname, uname), ts or 0, summary))
        n += 1
    src.close()
    return n


def _read_content(row_vals, cols_idx: dict):
    # 注意：WCDB_CT_message_content 是压缩标志列(0/4)，不是内容本体
    return decode_content(row_vals[cols_idx["message_content"]])


def detect_own_wxid(dec_dir: str) -> str:
    """自己的 wxid：从账号目录名 wxid_xxx_yyyy 中截取。"""
    base = os.path.basename(os.path.dirname(os.path.abspath(dec_dir)))
    m = re.match(r"(wxid_[0-9a-z]+)_", base)
    return m.group(1) if m else base


def import_messages(dec_dir: str, con: sqlite3.Connection, own_wxid: str = "") -> int:
    """微信 4.x 消息表没有 is_sender 列：真实发送人靠 real_sender_id →
    本分片 Name2Id 的 rowid 反查 username，再与自己的 wxid 比较。"""
    total = 0
    for name in MSG_DBS:
        p = os.path.join(dec_dir, name)
        if not os.path.exists(p):
            continue
        src = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        tables = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
        name2id = {}
        sender_map = {}
        try:
            for rid, uname in src.execute("SELECT rowid, user_name FROM Name2Id"):
                name2id[f"Msg_{hashlib.md5(uname.encode()).hexdigest()}"] = uname
                sender_map[rid] = uname
                for token in (uname or "").split(","):
                    sender_map[rid] = sender_map.get(rid) or token
            # Name2Id 中可能混入 "wxid_a,wxid_b" 这种复合值（历史迁移），
            # 逐 token 建索引，便于按 real_sender_id 精确命中
            for rid, uname in src.execute("SELECT rowid, user_name FROM Name2Id"):
                sender_map[rid] = uname
        except sqlite3.OperationalError:
            pass
        for t in tables:
            chat_user = name2id.get(t, "")
            if not chat_user:
                continue
            cols = [r[1] for r in src.execute(f"PRAGMA table_info('{t}')")]
            want = [c for c in ("local_id", "local_type", "create_time",
                                "message_content", "real_sender_id") if c in cols]
            idx = {c: i for i, c in enumerate(want)}
            sel = ", ".join(f"[{c}]" for c in want)
            try:
                rows = src.execute(f"SELECT {sel} FROM [{t}] ORDER BY create_time").fetchall()
            except sqlite3.Error as e:
                print(f"    ! {t}: {e}")
                continue
            is_group = int("@chatroom" in chat_user)
            batch = []
            for rv in rows:
                content = _read_content(rv, idx)
                if not content:
                    continue
                sender = sender_map.get(rv[idx["real_sender_id"]]) if "real_sender_id" in idx else ""
                sender = (sender or "").split(",")[0].strip()
                is_sender = 1 if (sender and sender == own_wxid) else 0
                # 群消息正文常自带 "<wxid>:" 前缀，补偿 real_sender_id 缺失的情况
                if is_group and rv[idx["local_type"]] == 1:
                    m = SENDER_PREFIX_RE.match(content)
                    if m:
                        prefix = m.group(1).strip()
                        if prefix.startswith("wxid_") or len(prefix) > 12:
                            if not is_sender:
                                sender = sender or prefix
                            content = content[m.end():]
                if rv[idx["local_type"]] == 49 and content.startswith("<"):
                    m = XML_TITLE_RE.search(content) or XML_DES_RE.search(content)
                    if m:
                        content = m.group(1)
                batch.append((chat_user, is_sender,
                              "" if is_sender else sender,
                              rv[idx["create_time"]], rv[idx["local_type"]],
                              content[:20000], name, t, rv[idx["local_id"]]))
            con.executemany("INSERT INTO message(chat_user,is_sender,sender,ts,type,content,"
                            "src_db,src_table,src_local_id) VALUES(?,?,?,?,?,?,?,?,?)", batch)
            total += len(batch)
        src.close()
        print(f"    {name}: 累计 {total} 条")
    return total


def init_letters(con: sqlite3.Connection) -> int:
    """为每个联系人算拼音首字母（pypinyin），供通讯录分组。"""
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        print("[!] 未安装 pypinyin，跳过首字母")
        return 0
    rows = con.execute("""SELECT username,
           TRIM(COALESCE(NULLIF(TRIM(remark),''), NULLIF(TRIM(nick_name),''), ''))
        FROM contact""").fetchall()
    n = 0
    for uname, name in rows:
        letter = ""
        if name:
            first = lazy_pinyin(name[0])[0][:1].upper()
            letter = first if first.isalnum() else "#"
        con.execute("UPDATE contact SET initial_letter=? WHERE username=?", (letter, uname))
        n += 1
    return n


def build_fts(con: sqlite3.Connection):
    """建 FTS：跳过噪声类型（拍一拍/系统/表情），XML 只索引标题描述。"""
    con.execute("DELETE FROM message_fts")
    rows = con.execute("SELECT id, type, content, chat_user, "
                       "(SELECT is_group FROM chat WHERE chat.username = message.chat_user) "
                       "FROM message").fetchall()
    batch = []
    for mid, typ, content, chat_user, is_group in rows:
        st = _search_text(typ, content)
        if len(st.strip()) < 2:
            continue
        batch.append((mid, st[:20000], chat_user, is_group or 0))
        if len(batch) >= 5000:
            con.executemany("INSERT INTO message_fts(rowid, content, chat_user, is_group) "
                            "VALUES(?,?,?,?)", batch)
            batch.clear()
    if batch:
        con.executemany("INSERT INTO message_fts(rowid, content, chat_user, is_group) "
                        "VALUES(?,?,?,?)", batch)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decrypted-dir", default="../../data/decrypted")
    ap.add_argument("--out", default="../../data/knowledge.db")
    args = ap.parse_args()
    d = args.decrypted_dir if os.path.isabs(args.decrypted_dir) else \
        os.path.abspath(os.path.join(os.path.dirname(__file__), args.decrypted_dir))
    out = args.out if os.path.isabs(args.out) else \
        os.path.abspath(os.path.join(os.path.dirname(__file__), args.out))
    t0 = time.time()
    con = sqlite3.connect(out)
    ensure_schema(con)
    nc = import_contacts(d, con)
    print(f"[+] 联系人 {nc}")
    ns = import_sessions(d, con)
    print(f"[+] 会话 {ns}")
    own = detect_own_wxid(d)
    print(f"[+] 本人 wxid {own}")
    nm = import_messages(d, con, own)
    print(f"[+] 消息 {nm}")
    init_letters(con)
    print("[*] 建 FTS 索引...")
    build_fts(con)
    con.commit()
    con.execute("INSERT OR REPLACE INTO meta VALUES('imported_at', ?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"),))
    con.commit()
    print(f"[+] knowledge.db 就绪: {out} ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
