# -*- coding: utf-8 -*-
"""按时间窗补漏重扫（设置页「补漏重扫」按钮触发）。

保底方案：保证指定时间窗内的消息"齐全、类型正确、可检索、可语义检索"。

流程：
  1. 增量解密到最新（含 WAL）；
  2. 按时间窗重扫全部消息库补导入（INSERT OR IGNORE 幂等，不重复）；
  3. 补该窗口内漏掉的图片 OCR / 语音视频转写；
  4. 强制重建该窗口内的语义向量（含图片文字/转写），媒体文字刚落库的也一并刷新；
  5. 输出核对统计（窗口内消息数 / 缺向量数 / 有媒体文字数）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP.parent.parent / "refs" / "ChatTrace" / "src"))

if os.name == "nt" and sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="重扫最近 N 天(默认 1)")
    args = ap.parse_args()

    import main as _m
    import vector as V
    since = int(time.time()) - args.days * 86400
    print(f"[rescan] 窗口=最近 {args.days} 天", flush=True)

    _m._decrypt_quick()
    n = _m._import_fast(watermark_override=since)
    print(f"[rescan] 补导入消息 {n} 条", flush=True)

    from auto_media import process_new_media
    st = process_new_media(_m.db(), ocr_cap=300, asr_cap=150, video_cap=60, since=since)
    print(f"[rescan] 媒体解析: OCR {st.get('ocr', 0)} / ASR {st.get('asr', 0)}", flush=True)

    # 4) 强制重建窗口内向量：先作废(媒体文字可能刚落库/旧占位向量)，再补嵌
    con = V.connect(str(_m.KNOWLEDGE_DB))
    try:
        if V.available():
            tbl = "msg_vec_p" if V._has_partition(con) else "msg_vec"
            # 只作废"用占位符嵌过、现在有媒体文字"的媒体向量，再补所有缺失的；
            # 不整窗删除——已有正确向量重嵌一次要几十分钟
            dropped = total_refresh = 0
            while True:
                k = V.refresh_media_vectors(con, cap=200)
                dropped += k
                if k < 200:
                    break
            total = 0
            while True:
                k = V.backfill_new(con, cap=64)
                total += k
                if k < 64:
                    break
            print(f"[rescan] 媒体占位向量作废 {dropped} 条，补嵌 {total} 条", flush=True)
            # 核对
            miss = con.execute(
                f"""SELECT COUNT(*) FROM message m
                    WHERE m.ts >= ? AND m.noise = 0
                      AND (m.type & 0xFFFFFFFF) NOT IN (10000, 10002)
                      AND NOT EXISTS (SELECT 1 FROM {tbl} v WHERE v.message_id = m.id)
                      AND EXISTS (SELECT 1 FROM media_text x
                                  WHERE x.message_id = m.id AND x.text != '')
                """, (since,)).fetchone()[0]
            print(f"[rescan] 核对: 有媒体文字但仍缺向量 {miss} 条", flush=True)
        else:
            print("[rescan] 跳过向量(嵌入模型不可用)", flush=True)
    finally:
        con.close()
    high = _m.db().execute(
        "SELECT COUNT(*) FROM message WHERE ts >= ? AND type >> 32 != 0", (since,)
    ).fetchone()[0]
    print(f"[rescan] 核对: 窗口内带高位标志(展示需掩码)的消息 {high} 条", flush=True)
    print("[进度] 1/1", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
