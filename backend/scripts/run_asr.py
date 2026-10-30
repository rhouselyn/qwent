# -*- utf-8 -*-
"""语音消息转文字：silk → wav → ASR → 写入 media_text(kind='asr') 并进 media_fts。

引擎（按可用性自动选择）：
  1) sherpa-onnx（推荐，离线自包含）：
     需模型目录 models/para-zh/，包含 model.onnx / tokens.txt 等
     下载（网络可用时）：
       https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/
       sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2
  2) faster-whisper：需 models/fw-small/（HF 可能不可达）
用法：python run_asr.py [--limit 0] [--recent-days 0] [--engine auto|sherpa|whisper]
"""
import argparse
import os
import sqlite3
import sys
import tempfile
import time
import wave

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import media_lookup  # noqa: E402

DB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                  "data", "knowledge.db"))
PROJECT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PARA_DIR = os.path.join(PROJECT, "models", "para-zh")
FW_DIR = os.path.join(PROJECT, "models", "fw-small")


def ensure_tables(con):
    con.execute("CREATE TABLE IF NOT EXISTS media_text("
                "message_id INTEGER PRIMARY KEY, kind TEXT, text TEXT, ts INTEGER)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5("
                "text, message_id UNINDEXED, tokenize='trigram')")


def build_sherpa():
    import sherpa_onnx
    files = os.listdir(PARA_DIR) if os.path.isdir(PARA_DIR) else []
    model = next((f for f in files if f.endswith(".onnx")), None)
    tokens = "tokens.txt" if "tokens.txt" in files else None
    if not model or not tokens:
        raise SystemExit(
            "[!] 缺少 sherpa-onnx 模型（models/para-zh 下需有 *.onnx 与 tokens.txt）。\n"
            "    下载途径（任选其一）：\n"
            "      a) GitHub: k2-fsa/sherpa-onnx → releases → asr-models → "
            "sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2\n"
            "      b) 已解压后放到 models/para-zh/ 再重跑")
    return sherpa_onnx.OfflineRecognizer.from_paraformer(
        paraformer=os.path.join(PARA_DIR, model),
        tokens=os.path.join(PARA_DIR, tokens),
        num_threads=2, sample_rate=16000, feature_dim=80, decoding_method="greedy_search"), "sherpa"


def build_whisper():
    from faster_whisper import WhisperModel
    if not os.path.isdir(FW_DIR):
        raise SystemExit("[!] faster-whisper 模型缺失（models/fw-small）")
    return WhisperModel(FW_DIR, device="cuda", compute_type="float16"), "whisper"


def wav_to_samples(wav_bytes: bytes):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        path = f.name
    try:
        with wave.open(path, "rb") as w:
            import numpy as np
            frames = w.readframes(w.getnframes())
            rate = w.getframerate()
            data = np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0
        return data, rate, path
    except Exception:
        return None, 0, path


def transcribe(engine, kind: str, wav_bytes: bytes) -> str:
    samples, rate, path = wav_to_samples(wav_bytes)
    try:
        if samples is None:
            return ""
        if kind == "sherpa":
            stream = engine.create_stream()
            stream.accept_waveform(rate if rate == 16000 else 16000, samples)
            engine.decode_stream(stream)
            return (stream.result.text or "").strip()
        segments, _ = engine.transcribe(path, language="zh", beam_size=5)
        return "".join(s.text for s in segments).strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--recent-days", type=int, default=0)
    ap.add_argument("--engine", default="auto", choices=["auto", "sherpa", "whisper"])
    args = ap.parse_args()

    con = sqlite3.connect(args.db, check_same_thread=False, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    ensure_tables(con)
    done = {r[0] for r in con.execute("SELECT message_id FROM media_text WHERE kind='asr'")}
    q = "SELECT id FROM message WHERE type=34"
    if args.recent_days:
        q += f" AND ts >= {int(time.time()) - args.recent_days * 86400}"
    q += " ORDER BY ts DESC"
    ids = [r[0] for r in con.execute(q)]
    todo = [i for i in ids if i not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[*] 语音消息 {len(ids)} 条，待转写 {len(todo)} 条", flush=True)

    kind = args.engine
    if kind in ("auto", "sherpa"):
        try:
            engine, k = build_sherpa()
        except SystemExit as e:
            print(str(e))
            if kind == "sherpa":
                return
            engine, k = build_whisper()
    else:
        engine, k = build_whisper()
    kind = k
    print(f"[*] 引擎就绪: {kind}", flush=True)

    ok = empty = 0
    t0 = time.time()
    for n, mid in enumerate(todo, 1):
        try:
            wav = media_lookup.voice_wav(con, mid)
            if not wav:
                continue
            text = transcribe(engine, kind, wav)
            con.execute("INSERT OR REPLACE INTO media_text(message_id,kind,text,ts) "
                        "VALUES(?,?,?,?)", (mid, "asr", text[:4000], int(time.time())))
            con.execute("DELETE FROM media_fts WHERE message_id=?", (mid,))
            if text:
                con.execute("INSERT INTO media_fts(text, message_id) VALUES(?,?)",
                            (text[:4000], mid))
                ok += 1
            else:
                empty += 1
        except Exception as e:
            print(f"  [!] msg {mid}: {e}")
        if n % 10 == 0:
            con.commit()
            rate = n / max(1e-6, time.time() - t0)
            print(f"  [进度] {n}/{len(todo)}  有文字 {ok} / 空白 {empty}  {rate:.2f} 条/秒",
                  flush=True)
    con.commit()
    con.close()
    print(f"[+] 完成：有文字 {ok}，空白 {empty}，用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
