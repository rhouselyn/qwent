# -*- coding: utf-8 -*-
"""向量检索支撑：本地 bge-small-zh-v1.5 嵌入 + sqlite-vec 索引。

模型：models/bge-small-zh（ModelScope 下载，512 维，中文优化，CPU/GPU 均可跑）
索引：message 表旁挂 msg_vec 虚表（vec0，float[512]），message_id 对齐 message.id
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault("USE_TF", "0")   # 避免 transformers 导入损坏的 TF 绑定
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")

PROJECT = Path(__file__).resolve().parent.parent.parent
MODEL_DIR = PROJECT / "models" / "bge-small-zh"
DIM = 512
MAX_LEN = 128          # 消息很短，128 token 足够，显著提速

_model = None
_tokenizer = None
_device = None
_dl_tried = False


def available() -> bool:
    global _dl_tried
    if (MODEL_DIR / "config.json").exists():
        return True
    if _dl_tried:
        return False
    _dl_tried = True
    try:                                   # 首次使用自动下载(约 95MB,走 hf-mirror)
        sys.path.insert(0, str(PROJECT / "backend" / "scripts"))
        from download_models import ensure_bge
        ensure_bge()
    except Exception as e:
        print(f"[vector] 自动下载 bge-small-zh 失败: {e}\n"
              f"        可手动执行 python backend/scripts/download_models.py --bge")
    return (MODEL_DIR / "config.json").exists()


def _load():
    global _model, _tokenizer, _device
    if _model is not None:
        return _model, _tokenizer, _device
    import torch
    from transformers import AutoModel, AutoTokenizer
    _device = "cuda" if torch.cuda.is_available() else "cpu"
    _tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    _model = AutoModel.from_pretrained(str(MODEL_DIR)).to(_device).eval()
    return _model, _tokenizer, _device


def embed(texts: list[str], batch_size: int = 64) -> np.ndarray:
    """返回 L2 归一化后的 float32 向量 (n, 512)。"""
    import torch
    model, tok, device = _load()
    out = []
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            batch = [t[:512] if t else " " for t in texts[i:i + batch_size]]
            enc = tok(batch, padding=True, truncation=True, max_length=MAX_LEN,
                      return_tensors="pt").to(device)
            hidden = model(**enc).last_hidden_state
            mask = enc["attention_mask"].unsqueeze(-1).float()
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-6)
            pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
            out.append(pooled.cpu().numpy().astype(np.float32))
    return np.vstack(out) if out else np.zeros((0, DIM), dtype=np.float32)


def connect(db_path: str, timeout: float = 30.0) -> sqlite3.Connection:
    """带 sqlite-vec 扩展的连接（向量查询必须）。"""
    import sqlite_vec
    con = sqlite3.connect(db_path, check_same_thread=False, timeout=timeout)
    con.row_factory = sqlite3.Row
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute("PRAGMA busy_timeout = 30000")
    return con


SCHEMA = "CREATE VIRTUAL TABLE IF NOT EXISTS msg_vec USING vec0(message_id INTEGER PRIMARY KEY, embedding float[512])"


def ensure_table(con: sqlite3.Connection):
    con.execute(SCHEMA)


def embedded_count(con: sqlite3.Connection) -> int:
    try:
        return con.execute("SELECT COUNT(*) FROM msg_vec").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def _has_partition(con: sqlite3.Connection) -> bool:
    try:
        con.execute("SELECT 1 FROM msg_vec_p LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


_vec_done_ready = False
_media_synced_ready = False


def _ensure_vec_done(con: sqlite3.Connection):
    """已尝试向量化标记表(普通表):无可检索文本的消息记一次,避免每轮重扫堵队列。"""
    global _vec_done_ready, _media_synced_ready
    if not _vec_done_ready:
        con.execute("CREATE TABLE IF NOT EXISTS vec_done("
                    "message_id INTEGER PRIMARY KEY)")
        con.commit()
        _vec_done_ready = True
    if not _media_synced_ready:
        con.execute("CREATE TABLE IF NOT EXISTS vec_media_synced("
                    "message_id INTEGER PRIMARY KEY)")
        con.commit()
        _media_synced_ready = True


def refresh_media_vectors(con, cap: int = 40) -> int:
    """作废"用占位符嵌过、后来才拿到 OCR/转写文字"的媒体消息向量。

    早期嵌入媒体消息时只用了内容占位 XML，向量没有语义价值；这些记录已经在
    向量表里，普通补嵌永远挑不到。这里逐条删掉向量，随后 backfill_new 会用
    媒体文字重嵌，并记入 vec_media_synced 不再重复作废。返回作废条数。
    """
    if not available():
        return 0
    _ensure_vec_done(con)
    tbl = "msg_vec_p" if _has_partition(con) else "msg_vec"
    ids = [r[0] for r in con.execute(
        f"""SELECT m.id FROM message m
            JOIN media_text x ON x.message_id = m.id AND x.text != ''
            JOIN {tbl} v ON v.message_id = m.id
            WHERE m.noise = 0
              AND (m.type & 0xFFFFFFFF) IN (3, 34, 43)
              AND NOT EXISTS (SELECT 1 FROM vec_media_synced s
                              WHERE s.message_id = m.id)
            LIMIT ?""", (cap,)).fetchall()]
    if not ids:
        return 0
    con.executemany(f"DELETE FROM {tbl} WHERE message_id=?", [(i,) for i in ids])
    con.commit()
    return len(ids)


def message_text(con, mid: int, type_id: int, content: str) -> str:
    """消息的可检索文本 = 自身文本 + 其图片 OCR / 语音视频转写文字。

    媒体消息（图/语音/视频）的 content 只是占位 XML，真正的内容在 media_text 表里；
    不合并进来，这些消息的向量就是无意义的占位符，语义检索永远搜不到。
    """
    from textutil import search_text
    base = search_text(type_id, content).strip()
    try:
        r = con.execute("SELECT text FROM media_text WHERE message_id=? AND text!=''",
                        (mid,)).fetchone()
        extra = (r[0] or "").strip() if r else ""
    except sqlite3.OperationalError:
        extra = ""
    return (base + "\n" + extra).strip() if extra else base


def drop_vector(con, mid: int):
    """删掉某条消息的向量（OCR/转写完成后调用，下一轮 backfill 会用新文字重嵌）。"""
    for tbl in ("msg_vec_p", "msg_vec"):
        try:
            con.execute(f"DELETE FROM {tbl} WHERE message_id=?", (mid,))
        except sqlite3.OperationalError:
            continue
    try:
        con.execute("DELETE FROM vec_done WHERE message_id=?", (mid,))
    except sqlite3.OperationalError:
        pass
    con.commit()


def backfill_new(con, cap: int = 30) -> int:
    """给尚未向量化且有可检索文本的新消息补 embedding(实时同步每轮调用)。

    con 必须是 vector.connect() 的连接(vec0 虚表查询需要扩展)。
    无可检索文本的消息记入 vec_done 跳过;媒体消息的文字在其 OCR/ASR 落库后,
    由设置页批量任务或全量重建覆盖。
    """
    if not available():
        return 0
    from textutil import search_text
    _ensure_vec_done(con)
    tbl = "msg_vec_p" if _has_partition(con) else "msg_vec"
    rows = con.execute(
        f"SELECT m.id, m.type, m.content, m.chat_user FROM message m WHERE m.noise=0 "
        f"AND NOT EXISTS (SELECT 1 FROM {tbl} v WHERE v.message_id = m.id) "
        f"AND (NOT EXISTS (SELECT 1 FROM vec_done d WHERE d.message_id = m.id) "
        f"     OR EXISTS (SELECT 1 FROM media_text x WHERE x.message_id = m.id "
        f"                AND x.text != '')) "
        f"ORDER BY m.id DESC LIMIT ?", (cap * 3,)).fetchall()
    if not rows:
        return 0
    todo, skipped = [], []
    for mid, typ, content, chat_user in rows:
        text = message_text(con, mid, typ, content)
        if len(text) >= 4:
            todo.append((mid, text, chat_user))
            if len(todo) >= cap:
                break
        else:
            skipped.append((mid,))
    if not todo and not skipped:
        return 0
    if skipped:
        con.executemany("INSERT OR IGNORE INTO vec_done(message_id) VALUES(?)", skipped)
    n = 0
    for i in range(0, len(todo), 16):
        chunk = todo[i:i + 16]
        vecs = embed([text for _, text, _ in chunk])
        # vec0 虚表不支持 INSERT OR REPLACE,必须先删再插(否则主键冲突)
        ids = [c[0] for c in chunk]
        if tbl == "msg_vec_p":
            con.executemany("DELETE FROM msg_vec_p WHERE message_id=?", [(i,) for i in ids])
            con.executemany(
                "INSERT INTO msg_vec_p(message_id, embedding, chat_user) VALUES(?,?,?)",
                [(mid, vecs[j].tobytes(), chat_user)
                 for j, (mid, _, chat_user) in enumerate(chunk)])
        else:
            con.executemany("DELETE FROM msg_vec WHERE message_id=?", [(i,) for i in ids])
            con.executemany("INSERT INTO msg_vec(message_id, embedding) VALUES(?,?)",
                            [(mid, vecs[j].tobytes()) for j, (mid, _, _) in enumerate(chunk)])
        # 含媒体文字的消息记入 vec_media_synced,避免 refresh_media_vectors 重复作废
        mids = [c[0] for c in chunk]
        ph = ",".join("?" * len(mids))
        try:
            has_media = [r[0] for r in con.execute(
                f"SELECT DISTINCT message_id FROM media_text WHERE text != '' "
                f"AND message_id IN ({ph})", mids)]
            if has_media:
                con.executemany("INSERT OR IGNORE INTO vec_media_synced(message_id) "
                                "VALUES(?)", [(i,) for i in has_media])
        except sqlite3.OperationalError:
            pass
        n += len(chunk)
    con.commit()
    return n


def search(con: sqlite3.Connection, query: str, k: int = 60,
           chat: str | None = None) -> list[tuple[int, float]]:
    """向量检索，返回 [(message_id, similarity)]。

    有分区表且指定会话时只扫描该会话向量（毫秒级）；否则全量扫描。
    """
    if not available():
        return []
    qv = embed([query])[0].tobytes()
    out: list[tuple[int, float]] = []
    if chat and _has_partition(con):
        try:
            rows = con.execute(
                "SELECT message_id, distance FROM msg_vec_p "
                "WHERE embedding MATCH ? AND k = ? AND chat_user = ?",
                (qv, max(k, 20), chat)).fetchall()
            for mid, dist in rows:
                d = float(dist)
                out.append((mid, 1.0 - (d * d) / 2.0))
            return out[:k]
        except sqlite3.OperationalError:
            out = []
    tbl = "msg_vec_p" if _has_partition(con) else "msg_vec"
    try:
        rows = con.execute(
            f"SELECT message_id, distance FROM {tbl} "
            "WHERE embedding MATCH ? AND k = ?", (qv, max(k * 3, 60))).fetchall()
    except sqlite3.OperationalError:
        return []
    for mid, dist in rows:
        d = float(dist)
        out.append((mid, 1.0 - (d * d) / 2.0))
    if chat:
        ids = [m for m, _ in out]
        keep = {r[0] for r in con.execute(
            "SELECT id FROM message WHERE chat_user=? AND id IN (%s)"
            % ",".join("?" * len(ids)), [chat] + ids)} if ids else set()
        out = [(m, s) for m, s in out if m in keep]
    return out[:k]
