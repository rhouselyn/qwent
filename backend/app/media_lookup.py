# -*- coding: utf-8 -*-
"""媒体（图片/语音）取字节：复用 ChatTrace 的图片定位与语音转码逻辑。

供 run_ocr.py / run_asr.py 调用（脚本环境，不走 FastAPI）。
"""
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
_app_dir = PROJECT / "backend" / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))
sys.path.insert(0, str(PROJECT / "refs" / "ChatTrace" / "src"))

from paths import ACCOUNT_DIR, ACCOUNT_ID, DECRYPTED_DIR  # noqa: E402 自动定位微信账号目录

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


_PACKED_MD5: dict[int, str] = {}


def packed_md5_of(con, message_id: int) -> str:
    """源库 packed_info_data 里的磁盘文件 md5（4.1.15 的 Img/Video 文件名用它，
    与消息 XML 的 md5 属性不同）。取不到返回空串。"""
    hit = _PACKED_MD5.get(message_id)
    if hit is not None:
        return hit
    md5 = ""
    try:
        row = con.execute("SELECT src_db, src_table, src_local_id FROM message WHERE id=?",
                          (message_id,)).fetchone()
        if row and row[0] and row[1] and row[2]:
            import re as _re
            if _re.match(r"^message_\d+\.db$", row[0]) and _re.match(r"^Msg_[0-9A-Za-z]{32}$", row[1]):
                p = DECRYPTED_DIR / "message" / row[0]
                if p.exists():
                    import sqlite3 as _s
                    src = _s.connect(f"file:{p}?mode=ro", uri=True)
                    r2 = src.execute(f"SELECT packed_info_data FROM [{row[1]}] WHERE local_id=?",
                                     (int(row[2]),)).fetchone()
                    src.close()
                    if r2 and r2[0]:
                        from chattrace.service.media import md5_hex32
                        md5 = md5_hex32(r2[0]) or ""
    except Exception:
        md5 = ""
    if len(_PACKED_MD5) > 5000:
        _PACKED_MD5.clear()
    _PACKED_MD5[message_id] = md5
    return md5


def image_bytes(con, message_id: int) -> bytes | None:
    """返回解码后的图片字节（全图解不了时退缩略图）。"""
    from chattrace.service.media import decode_dat
    row = con.execute("SELECT chat_user, ts, src_local_id, type, content "
                      "FROM message WHERE id=?", (message_id,)).fetchone()
    if not row:
        return None
    chat, ts, lid, typ, content = row
    cands = []
    # 1) 源库磁盘 md5：文件名精确匹配（按尺寸匹配在新版会命中别人的图）
    md5 = packed_md5_of(con, message_id)
    if md5:
        try:
            cands = media().image_dat_candidates(chat, md5, ts or 0)
        except Exception:
            cands = []
    # 2) 旧版 md5 命名兜底
    if not cands:
        sizes, thumbs = [], []
        rdb = _resource_con()
        if rdb is not None:
            cid_row = rdb.execute("SELECT rowid FROM ChatName2Id WHERE user_name=?", (chat,)).fetchone()
            if cid_row:
                for r in rdb.execute("""SELECT d.type, d.size FROM MessageResourceInfo i
                       JOIN MessageResourceDetail d ON d.message_id=i.message_id
                       WHERE i.chat_id=? AND i.message_local_id=?""", (cid_row[0], lid)):
                    (thumbs if r["type"] == 262145 else sizes).append(r["size"])
        if sizes or thumbs:
            p = locate_dat(chat, ts, sizes + thumbs)
            if p:
                cands.append(p)
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


def video_mp4(con, message_id: int) -> Path | None:
    """视频消息对应的明文 mp4（msg/video/<yyyy-MM>/<md5>.mp4）。"""
    row = con.execute("SELECT chat_user, ts FROM message WHERE id=?", (message_id,)).fetchone()
    if not row:
        return None
    md5 = packed_md5_of(con, message_id)
    if not md5:
        import re as _re
        c = con.execute("SELECT content FROM message WHERE id=?", (message_id,)).fetchone()
        m = _re.search(r'md5="([0-9a-f]{32})"', (c[0] if c else "") or "")
        if not m:
            return None
        md5 = m.group(1)
    mp4, _thumb = media().video_file(row["chat_user"], md5, row["ts"] or 0)
    return mp4


def video_thumb(con, message_id: int) -> Path | None:
    row = con.execute("SELECT chat_user, ts FROM message WHERE id=?", (message_id,)).fetchone()
    if not row:
        return None
    md5 = packed_md5_of(con, message_id)
    if not md5:
        return None
    _mp4, thumb = media().video_file(row["chat_user"], md5, row["ts"] or 0)
    return thumb if (thumb and thumb.exists()) else None


def audio_samples_16k(mp4_path: Path, max_seconds: int = 600):
    """用 PyAV 把 mp4 音轨解成 16k 单声道 float32（给 sherpa-onnx）。失败返回 None。"""
    try:
        import numpy as np
        import av
    except Exception:
        return None
    try:
        box = av.open(str(mp4_path))
    except Exception:
        return None
    try:
        streams = box.streams.audio
        if not streams:
            return None
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        chunks: list = []
        total = 0
        cap = max_seconds * 16000
        for frame in box.decode(streams[0]):
            for rf in resampler.resample(frame):
                arr = rf.to_ndarray()
                if arr.ndim > 1:
                    arr = arr[0]
                chunks.append(arr)
                total += len(arr)
                if total >= cap:
                    break
            if total >= cap:
                break
        if not chunks:
            return None
        pcm = np.concatenate(chunks)[:cap]
        return pcm.astype("float32") / 32768.0
    except Exception:
        return None
    finally:
        try:
            box.close()
        except Exception:
            pass


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
