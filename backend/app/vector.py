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
