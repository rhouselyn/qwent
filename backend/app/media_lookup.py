# -*- coding: utf-8 -*-
"""媒体（图片/语音）取字节：复用 ChatTrace 的图片定位与语音转码逻辑。

供 run_ocr.py / run_asr.py 调用（脚本环境，不走 FastAPI）。
"""
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "refs" / "ChatTrace" / "src"))

ACCOUNT_DIR = Path(os.path.expandvars(r"%USERPROFILE%\xwechat_files\wxid_hs7bm9yapf5x12_965a"))
DECRYPTED_DIR = Path(os.environ["LOCALAPPDATA"]) / "ChatTrace" / "accounts" / \
    "wxid_hs7bm9yapf5x12_965a" / "decrypted"
ACCOUNT_ID = "wxid_hs7bm9yapf5x12_965a"

_media = None
_res = None


def media():
    global _media
    if _media is None:
        from chattrace.service.image_key import ImageKeyResolver
        from chattrace.service.media import MediaService
        try:
            resolver = ImageKeyResolver(account_dir=ACCOUNT_DIR, account_id=ACCOUNT_ID)
        except Exception:
            resolver = None
        _media = MediaService(ACCOUNT_ID, ACCOUNT_DIR, DECRYPTED_DIR,
                              cache_dir=PROJECT / "data" / "media_cache",
                              image_key_resolver=resolver)
    return _media


def _resource_con():
    global _res
    if _res is None:
        p = DECRYPTED_DIR / "message" / "message_resource.db"
        _res = None
        if p.exists():
            import sqlite3
            _res = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
            _res.row_factory = sqlite3.Row
    return _res


def locate_dat(chat: str, ts: int, sizes: list[int]) -> Path | None:
    import hashlib
    from datetime import datetime, timedelta
    base = ACCOUNT_DIR / "msg" / "attach" / hashlib.md5(chat.encode()).hexdigest()
    if not base.is_dir():
        return None
    t = datetime.fromtimestamp(ts or 0)
    months = [(t + timedelta(days=d)).strftime("%Y-%m") for d in (-1, 0, 1)]
    for size in sorted(sizes, reverse=True):
        for month in months:
            imgdir = base / month / "Img"
            if not imgdir.is_dir():
                continue
            for f in imgdir.glob("*.dat"):
                if abs(f.stat().st_size - size) <= 64:
                    return f
    return None


def image_bytes(con, message_id: int) -> bytes | None:
    """返回解码后的图片字节（全图解不了时退缩略图）。"""
    from chattrace.service.media import decode_dat
    row = con.execute("SELECT chat_user, ts, src_local_id, type, content "
                      "FROM message WHERE id=?", (message_id,)).fetchone()
    if not row:
        return None
    chat, ts, lid, typ, content = row
    sizes, thumbs = [], []
    rdb = _resource_con()
    if rdb is not None:
        cid_row = rdb.execute("SELECT rowid FROM ChatName2Id WHERE user_name=?", (chat,)).fetchone()
        if cid_row:
            for r in rdb.execute("""SELECT d.type, d.size FROM MessageResourceInfo i
                   JOIN MessageResourceDetail d ON d.message_id=i.message_id
                   WHERE i.chat_id=? AND i.message_local_id=?""", (cid_row[0], lid)):
                (thumbs if r["type"] == 262145 else sizes).append(r["size"])
    cands = []
    if sizes or thumbs:
        p = locate_dat(chat, ts, sizes + thumbs)
        if p:
            cands.append(p)
    # 旧版 md5 命名兜底
    import re
    m = re.search(r'md5="([0-9a-f]{32})"', content or "")
    if m:
        try:
            item = media().image_item(chat, m.group(1), ts or 0)
            if item and item.disk_path:
                cands.insert(0, item.disk_path)
        except Exception:
            pass
    keys = media().image_keys()
    for p in cands[:4]:
        try:
            fmt, blob = decode_dat(p.read_bytes(), keys)
            if fmt in ("jpeg", "jpg", "png", "gif") and len(blob) > 100:
                return blob
        except Exception:
            continue
    return None


def voice_wav(con, message_id: int) -> bytes | None:
    row = con.execute("SELECT chat_user, ts, src_local_id FROM message WHERE id=?",
                      (message_id,)).fetchone()
    if not row:
        return None
    chat, ts, lid = row
    try:
        return media().voice_wav(chat, lid, ts or 0)
    except Exception:
        return None
