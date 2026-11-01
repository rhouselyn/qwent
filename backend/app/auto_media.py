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
_last_err = ""      # 最近一次被跳过的媒体处理异常（留痕便于诊断，裸 except 会把问题藏起来）


def _note_err(exc):
    global _last_err
    _last_err = f"{type(exc).__name__}: {exc}"[:300]


def _setup():
    global _ocr, _asr, _PROJECT, media_lookup
    app_dir = __file__.rsplit(os.sep, 1)[0] if os.sep in __file__ else os.path.dirname(__file__)
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
    import main  # noqa: F401  (需要其常量)

    _PROJECT = main.PROJECT
    scripts_dir = str(main.PROJECT / "backend" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import media_lookup as _ml
    media_lookup = _ml          # 提升为模块全局：process_new_media 里按模块函数调用

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


def _asr_samples(engine, kind: str, rate: int, samples) -> str:
    """把 16k 单声道 float32 采样交给转写引擎。"""
    if kind != "sherpa":
        return ""
    stream = engine.create_stream()
    stream.accept_waveform(rate, samples)
    engine.decode_stream(stream)
    return (stream.result.text or "").strip()


def process_new_media(con, ocr_cap: int = 30, asr_cap: int = 10, video_cap: int = 10,
                      since: int = 0) -> dict:
    """处理尚未 OCR/转写的图片、语音与视频消息（按时间新→旧，限量）。

    since：只处理该时刻之后的消息。实时同步应传入（如最近 48h），把历史积压
    留给设置页的批量任务——否则每轮都会在实时同步里全速跑 OCR/ASR，把 CPU
    吃满，整个程序（包括"读取最近记录并作答"）都跟着变慢。
    """
    if not _setup():
        return {"ocr": 0, "asr": 0, "skipped": True}
    con.execute("CREATE TABLE IF NOT EXISTS media_text("
                "message_id INTEGER PRIMARY KEY, kind TEXT, text TEXT, ts INTEGER)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5("
                "text, message_id UNINDEXED, tokenize='trigram')")

    stats = {"ocr": 0, "asr": 0}
    since_cond = "AND m.ts>=?" if since else ""
    since_params = [since] if since else []
    # ---- 图片 OCR ----
    rows = con.execute(f"""
        SELECT m.id FROM message m
        WHERE m.type=3 AND NOT EXISTS
              (SELECT 1 FROM media_text t WHERE t.message_id=m.id) {since_cond}
        ORDER BY m.ts DESC LIMIT ?""", (*since_params, ocr_cap)).fetchall()
    for (mid,) in rows:
        try:
            img = media_lookup.image_bytes(con, mid)
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
        except Exception as exc:
            _note_err(exc)
            continue

    # ---- 语音转写 ----
    rows = con.execute(f"""
        SELECT m.id FROM message m
        WHERE m.type=34 AND NOT EXISTS
              (SELECT 1 FROM media_text t WHERE t.message_id=m.id) {since_cond}
        ORDER BY m.ts DESC LIMIT ?""", (*since_params, asr_cap)).fetchall()
    for (mid,) in rows:
        try:
            wav = media_lookup.voice_wav(con, mid)
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
                con.execute("UPDATE message SET noise=0 WHERE id=? AND IFNULL(noise,0)<>0",
                            (mid,))
                stats["asr"] += 1
        except Exception as exc:
            _note_err(exc)
            continue

    # ---- 视频语音转写（msg/video 下的明文 mp4，PyAV 解音轨后走同一引擎） ----
    # video_cap 独立限额：历史视频积压量大，实时同步里少量消化，批量补跑走 run_asr
    if video_cap > 0:
        rows = con.execute(f"""
            SELECT m.id FROM message m
            WHERE m.type=43 AND NOT EXISTS
                  (SELECT 1 FROM media_text t WHERE t.message_id=m.id) {since_cond}
            ORDER BY m.ts DESC LIMIT ?""", (*since_params, video_cap)).fetchall()
        for (mid,) in rows:
            try:
                mp4 = media_lookup.video_mp4(con, mid)
                samples = media_lookup.audio_samples_16k(mp4) if mp4 else None
                if samples is None or not len(samples):
                    con.execute("INSERT OR REPLACE INTO media_text VALUES(?,?,?,?)",
                                (mid, "asr", "", int(time.time())))
                    continue
                engine, kind = _asr
                text = _asr_samples(engine, kind, 16000, samples)
                con.execute("INSERT OR REPLACE INTO media_text VALUES(?,?,?,?)",
                            (mid, "asr", text[:4000], int(time.time())))
                if text:
                    con.execute("DELETE FROM media_fts WHERE message_id=?", (mid,))
                    con.execute("INSERT INTO media_fts(text, message_id) VALUES(?,?)",
                                (text[:4000], mid))
                    # 转写成功的视频才从"未处理媒体"里放出来，检索（retrieve 过滤 noise）可见
                    con.execute("UPDATE message SET noise=0 WHERE id=? AND IFNULL(noise,0)<>0",
                                (mid,))
                    stats["asr"] += 1
            except Exception as exc:
                _note_err(exc)
                continue
    con.commit()        # 调用方（实时同步循环）不替我们提交，不 commit 等于白做
    return stats
