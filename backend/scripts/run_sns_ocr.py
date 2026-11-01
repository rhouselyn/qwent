# -*- coding: utf-8 -*-
"""朋友圈图片 OCR 批量任务（设置页「索引任务」可启动，可断点续跑）。

每轮处理有本地缓存或可 CDN 拉取的朋友圈图片 → OCR 入 sns_media_ocr/sns_media_fts，
全部完成后自动用"正文+OCR 文字"重建朋友圈语义向量。
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
    ap.add_argument("--limit", type=int, default=0, help="最多处理的图片张数,0=直到无待处理")
    ap.add_argument("--cap", type=int, default=40, help="每轮处理张数(默认 40)")
    args = ap.parse_args()

    import main as _m   # noqa: F401  需要 DATA/ACCOUNT_DIR 等常量
    import sns

    con = _m.db()
    done = 0
    t0 = time.time()
    while True:
        n = sns.process_sns_ocr(con, cap=args.cap)
        done += n
        print(f"[snsocr] +{n} (累计 {done}, {time.time()-t0:.0f}s)", flush=True)
        if n < args.cap or (args.limit and done >= args.limit):
            break
    st = sns.stats(con)
    print(f"[snsocr] 完成: OCR 文字 {st['ocr_done']} 张", flush=True)
    n = sns.rebuild_sns_vec()
    print(f"[snsocr] 向量重建: {n} 条", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
