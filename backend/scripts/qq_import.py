# -*- coding: utf-8 -*-
"""导入 QQ NT 本地聊天记录 / 联系人 / 文件 / 链接到 knowledge.db。

用法：
    python backend/scripts/qq_import.py              # 自动选最近登录的 QQ
    python backend/scripts/qq_import.py --qq 123456
    python backend/scripts/qq_import.py --stats      # 只查看已导入统计

QQ 必须处于登录状态（密钥只在运行中的 QQ 进程内存里）。导入只读本机数据，
解密文件放系统临时目录，结束后删除；不会修改 QQ 数据库。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend" / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import qq_local  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qq", default="", help="QQ 号；默认自动选择")
    ap.add_argument("--rebuild", action="store_true", default=True,
                    help="清空并重新导入（默认）")
    ap.add_argument("--no-rebuild", action="store_false", dest="rebuild",
                    help="保留已有记录，增量 upsert")
    ap.add_argument("--decrypt-dir", default="", help=argparse.SUPPRESS)
    ap.add_argument("--stats", action="store_true", help="只输出当前统计")
    args = ap.parse_args()

    con = sqlite3.connect(str(ROOT / "data" / "knowledge.db"), timeout=60)
    try:
        qq_local.ensure_schema(con)
        if args.stats:
            print(json.dumps(qq_local.stats(con), ensure_ascii=False, indent=2))
            return 0
        print("QQ 本地记录导入")
        print("数据目录:", qq_local.QQ_ROOT)
        res = qq_local.import_all(
            con, qq=args.qq or None, rebuild=args.rebuild,
            decrypt_dir=args.decrypt_dir or None,
            progress=lambda s: print("  " + s, flush=True))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        print("当前统计:", json.dumps(qq_local.stats(con), ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print("[失败]", exc, file=sys.stderr)
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
