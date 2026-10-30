# -*- coding: utf-8 -*-
"""新消息媒体自动处理：实时同步后，把新图片 OCR、新语音转写，结果进 media_fts。

供 main.py 的实时同步线程调用，可设置每轮上限避免拖慢同步。
"""
from __future__ import annotations

import os
import sys
import time

_PROJECT = None
_ocr = None
_asr = None
_media = None


def _setup():
    global _ocr, _asr, _media, _PROJECT
    app_dir = __file__.rsplit(os.sep, 1)[0] if os.sep in __file__ else os.path.dirname(__file__)
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
    import main  # noqa: F401  (需要其常量)

    _PROJECT = main.PROJECT
    scripts_dir = str(main.PROJECT / "backend" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    if _media is None:
        import media_lookup
        _media = media_lookup.media()

    if _ocr is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ocr = RapidOCR()
        except Exception:
            _ocr = False

    if _asr is None:
        try:
            from run_asr import build_sherpa
            _asr = build_sherpa()          # (engine, kind)
        except Exception:
            _asr = False

    return True


def process_new_media(con, ocr_cap: int = 30, asr_cap: int = 10) -> dict:
    """处理尚未 OCR/转写的图片与语音消息（按时间新→旧，限量）。"""
    if not _setup():
        return {"ocr": 0, "asr": 0, "skipped": True}
    con.execute("CREATE TABLE IF NOT EXISTS media_text("
                "message_id INTEGER PRIMARY KEY, kind TEXT, text TEXT, ts INTEGER)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5("
                "text, message_id UNINDEXED, tokenize='trigram')")

    stats = {"ocr": 0, "asr": 0}
    # ---- 图片 OCR ----
    rows = con.execute("""
        SELECT m.id FROM message m
        WHERE m.type=3 AND NOT EXISTS
              (SELECT 1 FROM media_text t WHERE t.message_id=m.id)
        ORDER BY m.ts DESC LIMIT ?""", (ocr_cap,)).fetchall()
    for (mid,) in rows:
        try:
            img = _media.image_bytes(con, mid)
            if not img:
                con.execute("INSERT OR REPLACE INTO media_text VALUES(?,?,?,?)",
                            (mid, "ocr", "", int(time.time())))
                continue
            res, _ = _ocr(img)
            text = "\n".join(x[1] for x in res) if res else ""
            con.execute("INSERT OR REPLACE INTO media_text VALUES(?,?,?,?)",
                        (mid, "ocr", text[:4000], int(time.time())))
            if text.strip():
                con.execute("DELETE FROM media_fts WHERE message_id=?", (mid,))
                con.execute("INSERT INTO media_fts(text, message_id) VALUES(?,?)",
                            (text[:4000], mid))
                stats["ocr"] += 1
        except Exception:
            continue

    # ---- 语音转写 ----
    rows = con.execute("""
        SELECT m.id FROM message m
        WHERE m.type=34 AND NOT EXISTS
              (SELECT 1 FROM media_text t WHERE t.message_id=m.id)
        ORDER BY m.ts DESC LIMIT ?""", (asr_cap,)).fetchall()
    for (mid,) in rows:
        try:
            wav = _media.voice_wav(con, mid)
            if not wav:
                con.execute("INSERT OR REPLACE INTO media_text VALUES(?,?,?,?)",
                            (mid, "asr", "", int(time.time())))
                continue
            engine, kind = _asr
            if kind == "sherpa":
                import wave as _wave
                import tempfile as _tf
                with _tf.NamedTemporaryFile(suffix=".wav", delete=False) as fobj:
                    fobj.write(wav)
                    path = fobj.name
                stream = engine.create_stream()
                with _wave.open(path, "rb") as w:
                    rate = w.getframerate()
                    samples = w.readframes(w.getnframes())
                import numpy as np
                stream.accept_waveform(rate, np.frombuffer(samples, np.int16)
                                       .astype("float32") / 32768.0)
                engine.decode_stream(stream)
                text = (stream.result.text or "").strip()
                os.unlink(path)
            else:
                text = ""
            con.execute("INSERT OR REPLACE INTO media_text VALUES(?,?,?,?)",
                        (mid, "asr", text[:4000], int(time.time())))
            if text:
                con.execute("DELETE FROM media_fts WHERE message_id=?", (mid,))
                con.execute("INSERT INTO media_fts(text, message_id) VALUES(?,?)",
                            (text[:4000], mid))
                stats["asr"] += 1
        except Exception:
            continue
    return stats
