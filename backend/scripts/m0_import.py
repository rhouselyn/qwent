# -*- coding: utf-8 -*-
"""M0-① 把解密后的微信 4.x 库导入 knowledge.db（项目自己的知识库）。

微信 4.0 schema 要点（来自 decrypt 后库的实际结构）：
  message_N.db: 每个会话一张表 Msg_{md5(username)}，列 local_id/local_type/
                create_time/message_content[，sender 等]；Name2Id 表反查 username
  contact.db:   contact(username, nick_name, remark, ...)
  session.db:   SessionTable(username, unread_count, summary, last_timestamp, ...)

50 万条级导入要点：
  - 唯一索引 (src_db,src_table,src_local_id) + INSERT OR IGNORE：重跑不重复
  - 游标流式读取 + 每 2000 条提交：内存不随最大分片增长
  - meta.import_ck:<db>:<table> 记录已处理 local_id：中断后从断点继续
  - 每批输出 [进度] n/total，可直接对接服务端任务日志
  - FTS 按 message.id 区间分块建，已建部分自动跳过

用法：
  python m0_import.py [--decrypted-dir ../../data/decrypted] [--out ../../data/knowledge.db]
                      [--dry-run] [--reset] [--rebuild-fts] [--limit N]
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

# 只保留一种写法(路径用于定位,写库时统一取 basename)——两种写法曾导致重复导入
MSG_DBS = [f"message/message_{i}.db" for i in range(8)]
XML_TITLE_RE = re.compile(r"<title><!\[CDATA\[(.*?)\]\]></title>", re.S)
XML_DES_RE = re.compile(r"<des><!\[CDATA\[(.*?)\]\]></des>", re.S)
# 群文本消息正文以 "<wxid>:\n" 前缀标注实际发送人（比 Name2Id rowid 更可靠）
SENDER_PREFIX_RE = re.compile(r"^([^:\n]{4,64}):\r?\n")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_ZSTD = zstandard.ZstdDecompressor()
BATCH = 2000
FTS_BATCH = 20000


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


def _set_meta(con, key: str, value) -> None:
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))


def _get_meta(con, key: str, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def ensure_schema(con: sqlite3.Connection) -> None:
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
    _ensure_src_unique(con)


def _ensure_src_unique(con: sqlite3.Connection) -> None:
    """确保唯一索引存在；历史库可能已有重复行，先按最小 rowid 清理。

    与 server 的 _ensure_src_unique 保持同一索引名和列，二者互不冲突。
    """
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_msg_src'"
                      ).fetchone()
    if row and "UNIQUE" in (row[0] or "").upper():
        return
    con.execute("DROP INDEX IF EXISTS idx_msg_src")
    for _try in range(3):
        con.execute("DELETE FROM message WHERE rowid NOT IN ("
                    "SELECT MIN(rowid) FROM message "
                    "GROUP BY src_db, src_table, src_local_id)")
        con.commit()
        try:
            con.execute("CREATE UNIQUE INDEX idx_msg_src "
                        "ON message(src_db, src_table, src_local_id)")
            con.commit()
            return
        except sqlite3.IntegrityError:
            con.rollback()      # 并发写入又产生重复：再清一轮


def _src_conn(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def import_contacts(dec_dir: str, con: sqlite3.Connection) -> int:
    p = os.path.join(dec_dir, "contact", "contact.db")
    if not os.path.exists(p):
        p = os.path.join(dec_dir, "contact.db")
    if not os.path.exists(p):
        print("[!] 缺 contact.db，跳过联系人")
        return 0
    src = _src_conn(p)
    cols = {r[1] for r in src.execute("PRAGMA table_info(contact)")}
    extra = [c for c in ("alias", "pin_yin_initial", "quan_pin", "local_type", "flag",
                        "delete_flag") if c in cols]
    exp = {c: i + 3 for i, c in enumerate(extra)}
    batch = []
    n = 0
    cur = src.execute("SELECT username, nick_name, remark, " +
                      (", ".join(extra) if extra else "NULL,NULL,NULL,NULL,NULL,NULL") +
                      " FROM contact")
    for r in cur:
        uname = r[0]
        if not uname:
            continue
        g = lambda c: r[exp[c]] if c in exp else None
        batch.append((uname, r[1], r[2], g("alias"), g("pin_yin_initial") or "",
                      g("quan_pin") or "", g("local_type") or 0, g("flag") or 0,
                      g("delete_flag") or 0))
        if len(batch) >= BATCH:
            con.executemany(
                "INSERT OR REPLACE INTO contact(username, nick_name, remark, alias, "
                "pin_yin_initial, quan_pin, local_type, flag, delete_flag) "
                "VALUES(?,?,?,?,?,?,?,?,?)", batch)
            con.commit()
            n += len(batch)
            batch.clear()
    if batch:
        con.executemany(
            "INSERT OR REPLACE INTO contact(username, nick_name, remark, alias, "
            "pin_yin_initial, quan_pin, local_type, flag, delete_flag) "
            "VALUES(?,?,?,?,?,?,?,?,?)", batch)
        con.commit()
        n += len(batch)
    src.close()
    return n


def import_sessions(dec_dir: str, con: sqlite3.Connection) -> int:
    p = os.path.join(dec_dir, "session", "session.db")
    if not os.path.exists(p):
        p = os.path.join(dec_dir, "session.db")
    if not os.path.exists(p):
        print("[!] 缺 session.db，跳过会话列表")
        return 0
    src = _src_conn(p)
    try:
        rows = src.execute(
            "SELECT username, summary, last_timestamp FROM SessionTable").fetchall()
    except sqlite3.OperationalError as e:
        print(f"[!] SessionTable 读取失败: {e}")
        src.close()
        return 0
    contacts = {u: n for u, n in con.execute(
        "SELECT username, COALESCE(NULLIF(TRIM(remark),''), NULLIF(TRIM(nick_name),''), username) "
        "FROM contact")}
    batch = []
    for uname, summary, ts in rows:
        if not uname:
            continue
        batch.append((uname, int("@chatroom" in uname or "@openim" in uname),
                      contacts.get(uname, uname), ts or 0, summary))
    if batch:
        con.executemany("INSERT OR REPLACE INTO chat(username,is_group,display_name,last_ts,summary)"
                        " VALUES(?,?,?,?,?)", batch)
        con.commit()
    src.close()
    return len(batch)


def _read_content(row_vals, cols_idx: dict):
    # 注意：WCDB_CT_message_content 是压缩标志列(0/4)，不是内容本体
    return decode_content(row_vals[cols_idx["message_content"]])


def detect_own_wxid(dec_dir: str) -> str:
    """自己的 wxid：从账号目录名 wxid_xxx_yyyy 中截取。"""
    base = os.path.basename(os.path.dirname(os.path.abspath(dec_dir)))
    m = re.match(r"(wxid_[0-9a-z]+)_", base)
    return m.group(1) if m else base


def _table_plan(src: sqlite3.Connection, table: str):
    cols = [r[1] for r in src.execute(f"PRAGMA table_info('{table}')")]
    want = [c for c in ("local_id", "local_type", "create_time",
                        "message_content", "real_sender_id") if c in cols]
    idx = {c: i for i, c in enumerate(want)}
    return want, idx


def _source_tables(dec_dir: str):
    """枚举 (db 文件名, 表名, username, 列信息, sender 映射)，统计/导入/dry-run 共用。"""
    import hashlib as _h
    for name in MSG_DBS:
        p = os.path.join(dec_dir, name)
        if not os.path.exists(p):
            continue
        src = _src_conn(p)
        try:
            tables = [r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            name2id, sender_map = {}, {}
            try:
                for rid, uname in src.execute("SELECT rowid, user_name FROM Name2Id"):
                    name2id["Msg_" + _h.md5((uname or "").encode()).hexdigest()] = uname
                    sender_map[rid] = uname or ""
            except sqlite3.OperationalError:
                pass
            for t in sorted(tables):
                chat_user = name2id.get(t, "")
                if not chat_user:
                    continue
                want, idx = _table_plan(src, t)
                if "local_id" not in idx or "message_content" not in idx:
                    continue
                yield name, t, chat_user, want, idx, sender_map
        finally:
            src.close()


def _count_rows(dec_dir: str, db_name: str, table: str) -> int:
    try:
        src = _src_conn(os.path.join(dec_dir, db_name))
        try:
            return int(src.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0])
        finally:
            src.close()
    except sqlite3.Error:
        return 0


def _iter_rows(src, table: str, want: list, local_id: int, limit: int | None):
    sel = ", ".join(f"[{c}]" for c in want)
    sql = f"SELECT {sel} FROM [{table}] WHERE local_id > ? ORDER BY local_id"
    cur = src.execute(sql, (local_id,))
    n = 0
    for rv in cur:
        yield rv
        n += 1
        if limit and n >= limit:
            break


def _decode_row(rv, idx, chat_user, own_wxid, sender_map):
    content = _read_content(rv, idx)
    if not content:
        return None
    sender = sender_map.get(rv[idx["real_sender_id"]]) if "real_sender_id" in idx else ""
    sender = (sender or "").split(",")[0].strip()
    is_sender = 1 if (sender and sender == own_wxid) else 0
    is_group = int("@chatroom" in chat_user)
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
    return (chat_user, is_sender, "" if is_sender else sender,
            rv[idx["create_time"]], rv[idx["local_type"]],
            content[:20000], rv[idx["local_id"]])


def import_messages(dec_dir: str, con: sqlite3.Connection, own_wxid: str,
                    limit: int | None = None) -> tuple[int, int]:
    """返回 (本次处理行数, 新增消息数)。断点按 src_db/src_table 记录。"""
    plan = list(_source_tables(dec_dir))
    total = sum(_count_rows(dec_dir, n, t) for n, t, *_rest in plan)
    processed = 0
    before = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    for db_name, table, chat_user, want, idx, sender_map in plan:
        src = _src_conn(os.path.join(dec_dir, db_name))
        ck_key = f"import_ck:{db_name}:{table}"
        ck = int(_get_meta(con, ck_key, 0) or 0)
        batch, last_id = [], ck
        try:
            for rv in _iter_rows(src, table, want, ck, limit):
                last_id = rv[idx["local_id"]]
                row = _decode_row(rv, idx, chat_user, own_wxid, sender_map)
                processed += 1
                if row is not None:
                    # message 列序：chat_user,is_sender,sender,ts,type,content,src_db,src_table,src_local_id
                    batch.append((*row[:6], db_name, table, row[6]))
                if len(batch) >= BATCH:
                    con.executemany(
                        "INSERT OR IGNORE INTO message(chat_user,is_sender,sender,ts,type,content,"
                        "src_db,src_table,src_local_id) VALUES(?,?,?,?,?,?,?,?,?)", batch)
                    _set_meta(con, ck_key, last_id)
                    con.commit()
                    batch.clear()
                    print(f"[进度] {processed}/{total}", flush=True)
            if batch:
                con.executemany(
                    "INSERT OR IGNORE INTO message(chat_user,is_sender,sender,ts,type,content,"
                    "src_db,src_table,src_local_id) VALUES(?,?,?,?,?,?,?,?,?)", batch)
                batch.clear()
            _set_meta(con, ck_key, last_id)
            con.commit()
            print(f"[进度] {processed}/{total}", flush=True)
        except sqlite3.Error as e:
            print(f"    ! {db_name}/{table}: {e}", flush=True)
        finally:
            src.close()
    after = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    return processed, after - before


def dry_run(dec_dir: str, own_wxid: str, limit: int | None = None) -> None:
    t0 = time.time()
    plan = list(_source_tables(dec_dir))
    total = sum(_count_rows(dec_dir, n, t) for n, t, *_rest in plan)
    processed = decoded = 0
    for db_name, table, chat_user, want, idx, sender_map in plan:
        src = _src_conn(os.path.join(dec_dir, db_name))
        try:
            for rv in _iter_rows(src, table, want, 0, limit):
                processed += 1
                if _decode_row(rv, idx, chat_user, own_wxid, sender_map) is not None:
                    decoded += 1
                if processed % 2000 == 0:
                    print(f"[进度] {processed}/{total}", flush=True)
        except sqlite3.Error as e:
            print(f"    ! {db_name}/{table}: {e}", flush=True)
        finally:
            src.close()
    cost = time.time() - t0
    est = (cost / processed * total) if processed else 0
    print(f"[dry-run] 源消息 {total} 条，试读 {processed} 条，可解析 {decoded} 条")
    print(f"[dry-run] 试读耗时 {cost:.1f}s，按此估算全量约 {est/60:.1f} 分钟（不含 FTS 构建）")
    print("[dry-run] 未写入 knowledge.db")


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
    batch = []
    for uname, name in rows:
        letter = ""
        if name:
            first = lazy_pinyin(name[0])[0][:1].upper()
            letter = first if first.isalnum() else "#"
        batch.append((letter, uname))
    if batch:
        con.executemany("UPDATE contact SET initial_letter=? WHERE username=?", batch)
        con.commit()
    return len(batch)


def build_fts(con: sqlite3.Connection, rebuild: bool = False) -> int:
    """按 message.id 区间分块建 FTS；已建部分自动跳过。

    跳过噪声类型（拍一拍/系统/表情），XML 只索引标题描述。
    """
    if rebuild:
        con.execute("DELETE FROM message_fts")
        con.commit()
    start = 0
    if not rebuild:
        r = con.execute("SELECT MAX(rowid) FROM message_fts").fetchone()
        start = int((r and r[0]) or 0)
    top = con.execute("SELECT MAX(id) FROM message").fetchone()
    top = int((top and top[0]) or 0)
    if top <= start:
        print(f"[+] FTS 已是最新（message.id <= {start}）")
        return start
    groups = {u: g for u, g in con.execute("SELECT username, is_group FROM chat")}
    done = start
    while done < top:
        end = min(done + FTS_BATCH, top)
        rows = con.execute(
            "SELECT id, type, content, chat_user FROM message "
            "WHERE id > ? AND id <= ? ORDER BY id", (done, end)).fetchall()
        batch = []
        for mid, typ, content, chat_user in rows:
            st = _search_text(typ, content)
            if len(st.strip()) < 2:
                continue
            batch.append((mid, st[:20000], chat_user, groups.get(chat_user) or 0))
        if batch:
            con.executemany("INSERT INTO message_fts(rowid, content, chat_user, is_group) "
                            "VALUES(?,?,?,?)", batch)
        con.commit()
        done = end
        print(f"[进度] {done}/{top}", flush=True)
    _set_meta(con, "fts_built_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    con.commit()
    return top


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decrypted-dir", default="../../data/decrypted")
    ap.add_argument("--out", default="../../data/knowledge.db")
    ap.add_argument("--dry-run", action="store_true", help="只试读统计，不写库")
    ap.add_argument("--reset", action="store_true", help="清空导入断点（数据仍靠唯一索引去重）")
    ap.add_argument("--rebuild-fts", action="store_true", help="删掉并重建全文索引")
    ap.add_argument("--limit", type=int, default=0, help="每张源表最多处理多少条（调试用）")
    args = ap.parse_args()
    d = args.decrypted_dir if os.path.isabs(args.decrypted_dir) else \
        os.path.abspath(os.path.join(os.path.dirname(__file__), args.decrypted_dir))
    out = args.out if os.path.isabs(args.out) else \
        os.path.abspath(os.path.join(os.path.dirname(__file__), args.out))
    own = detect_own_wxid(d)
    print(f"[+] 本人 wxid {own}")
    if args.dry_run:
        dry_run(d, own, args.limit or None)
        return

    t0 = time.time()
    con = sqlite3.connect(out)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-65536")
    ensure_schema(con)
    if args.reset:
        con.execute("DELETE FROM meta WHERE key LIKE 'import_ck:%'")
        con.commit()
    nc = import_contacts(d, con)
    print(f"[+] 联系人 {nc}")
    ns = import_sessions(d, con)
    print(f"[+] 会话 {ns}")
    processed, added = import_messages(d, con, own, args.limit or None)
    print(f"[+] 消息处理 {processed} 条，新增 {added} 条")
    init_letters(con)
    print("[*] 建 FTS 索引...")
    build_fts(con, args.rebuild_fts)
    _set_meta(con, "imported_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    con.commit()
    con.close()
    print(f"[+] knowledge.db 就绪: {out} ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
