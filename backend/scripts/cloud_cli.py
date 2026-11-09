# -*- coding: utf-8 -*-
"""云端分流统一命令行（本地与云端通用）。

    python backend/scripts/cloud_cli.py test
    python backend/scripts/cloud_cli.py triggers-install
    python backend/scripts/cloud_cli.py baseline
    python backend/scripts/cloud_cli.py sync
    python backend/scripts/cloud_cli.py status
    # 云端：
    QWENT_CLOUD=1 python backend/scripts/cloud_cli.py restore
    QWENT_CLOUD=1 python backend/scripts/cloud_cli.py apply
    QWENT_CLOUD=1 python backend/scripts/cloud_cli.py serve
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend" / "app"))


def _p(title, obj):
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_test(args):
    import cloud_conf
    import cloud_transport
    try:
        cloud_conf.encrypt_key()
        print("加密密钥：OK")
    except Exception as e:
        _p("加密密钥", {"ok": False, "error": str(e)})
        return 1
    _p("传输", cloud_transport.get_transport().test())
    return 0


def _local_db():
    import cloud_conf
    return sqlite3.connect(str(cloud_conf.data_dir() / "knowledge.db"), timeout=60)


def cmd_triggers(args):
    import cloud_cdc
    con = _local_db()
    try:
        if args.action == "drop":
            n = cloud_cdc.drop_triggers(con, keep_tables=False)
            _p("删除", {"triggers": n, "tables_dropped": True})
        else:
            n = cloud_cdc.install_triggers(con)
            _p("安装", {"triggers": n,
                        "tables": list(cloud_cdc.SYNC_TABLES),
                        "schema_sig": cloud_cdc.schema_sig(con)})
    finally:
        con.close()
    return 0


def cmd_sync(args):
    import cloud_sync_local
    _p("手动增量同步", cloud_sync_local.sync_now(force=True))
    return 0


def cmd_baseline(args):
    import cloud_sync_local
    print("生成全量基线（4.25GB 快照 + 加密分片 + 上传），可能耗时数分钟…")
    _p("基线", cloud_sync_local.baseline(force=True))
    return 0


def cmd_apply(args):
    import cloud_apply
    if args.loop:
        cloud_apply.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            cloud_apply.stop()
        return 0
    _p("应用增量", cloud_apply.apply_next())
    return 0


def cmd_restore(args):
    import cloud_apply
    _p("恢复基线", cloud_apply.restore_baseline())
    return 0


def cmd_status(args):
    import cloud_conf
    mod = "cloud_apply" if cloud_conf.IS_CLOUD else "cloud_sync_local"
    m = __import__(mod)
    _p("状态", m.status())
    _p("云端配置（掩码）", cloud_conf.mask_conf(cloud_conf.cloud_conf()))
    return 0


def cmd_verify(args):
    """本地关键表行数与 CDC 水位，用于和云端对账。"""
    import cloud_cdc
    con = _local_db()
    try:
        out = {"schema_sig": cloud_cdc.schema_sig(con)}
        for t in ("message", "qq_local_message", "chat", "contact",
                  "message_fts", "vec_done"):
            try:
                out[t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.OperationalError:
                out[t] = None
        try:
            out["cdc_pending"] = con.execute(
                "SELECT COUNT(*) FROM _cloud_cdc").fetchone()[0]
            out["last_sent_lsn"] = cloud_cdc.get_state(con, "last_sent_lsn", "0")
            out["last_sent_seq"] = cloud_cdc.get_state(con, "last_sent_seq", "0")
            out["epoch"] = cloud_cdc.get_state(con, "epoch", "")
        except sqlite3.OperationalError:
            out["cdc_pending"] = "触发器未安装"
    finally:
        con.close()
    _p("本地对账", out)
    return 0


def cmd_switch(args):
    import cloud_conf
    conf = cloud_conf.load_conf()
    c = dict(cloud_conf.cloud_conf(conf))
    if args.action == "on":
        c["enabled"] = True
        if args.host:
            c.update({"transport": "scp", "endpoint": args.host,
                      "scp_user": args.user or "root",
                      "scp_key": args.key or "",
                      "scp_dir": args.dir or "/opt/qwent/inbox"})
    else:
        c["enabled"] = False
    conf["cloud"] = c
    cloud_conf.save_conf(conf)
    _p("开关", cloud_conf.mask_conf(c))
    return 0


def cmd_serve(args):
    import uvicorn
    os.environ["QWENT_CLOUD"] = "1"
    sys.path.insert(0, str(ROOT / "backend" / "app"))
    uvicorn.run("main:app", host="127.0.0.1", port=args.port,
                log_level="warning")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Qwent 云端分流命令行")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("test")
    p = sub.add_parser("triggers-install"); p.set_defaults(action="install")
    p = sub.add_parser("triggers-drop"); p.set_defaults(action="drop")
    sub.add_parser("sync")
    sub.add_parser("baseline")
    p = sub.add_parser("apply"); p.add_argument("--loop", action="store_true")
    sub.add_parser("restore")
    sub.add_parser("status")
    sub.add_parser("verify")
    p = sub.add_parser("on")
    p.set_defaults(action="on")
    p.add_argument("--host"); p.add_argument("--user"); p.add_argument("--key")
    p.add_argument("--dir")
    p = sub.add_parser("off"); p.set_defaults(action="off")
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8721)

    args = ap.parse_args()
    fn = {
        "test": cmd_test, "triggers-install": cmd_triggers,
        "triggers-drop": cmd_triggers, "sync": cmd_sync,
        "baseline": cmd_baseline, "apply": cmd_apply, "restore": cmd_restore,
        "status": cmd_status, "verify": cmd_verify,
        "on": cmd_switch, "off": cmd_switch, "serve": cmd_serve,
    }[args.cmd]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
