# -*- coding: utf-8 -*-
"""按时间窗补漏重扫（设置页「补漏重扫」按钮触发）。

怀疑有消息没显示出来时的兜底：重新扫描指定窗口内的全部消息源库补导入
（INSERT OR IGNORE 幂等，不会重复），并补该窗口内漏掉的图片/语音/视频解析。
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
    since = int(time.time()) - args.days * 86400
    print(f"[rescan] 重扫最近 {args.days} 天 (since={since})", flush=True)

    _m._decrypt_quick()          # 增量解密到最新(含 wal)
    n = _m._import_fast(watermark_override=since)
    print(f"[rescan] 补导入消息 {n} 条", flush=True)

    from auto_media import process_new_media
    st = process_new_media(_m.db(), ocr_cap=300, asr_cap=150, video_cap=60,
                           since=since)
    print(f"[rescan] 媒体补解析: OCR {st.get('ocr', 0)} / ASR {st.get('asr', 0)}",
          flush=True)
    print(f"[进度] 1/1", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
