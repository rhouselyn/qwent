# -*- coding: utf-8 -*-
"""自动定位微信 4.x 账号数据目录。

优先级:
  1. backend/config.json 的 account.account_dir(手动指定)
  2. 环境变量 WECHAT_ACCOUNT_DIR
  3. 扫描 %USERPROFILE%\\xwechat_files\\ 下含 db_storage/message 或 msg/attach
     的账号目录,取最近使用的(多账号时打印全部,推荐手动指定)

用法(独立运行可自检):
  python backend/app/paths.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
CONFIG = PROJECT / "backend" / "config.json"


def _cfg_account() -> dict:
    try:
        return (json.loads(CONFIG.read_text("utf-8")).get("account") or {})
    except Exception:
        return {}


def _looks_like_account(d: Path) -> bool:
    if not d.is_dir():
        return False
    return (d / "db_storage" / "message").is_dir() or (d / "msg" / "attach").is_dir()


def _recency(d: Path) -> float:
    """目录最近使用时间:优先看消息库文件,退化用目录自身 mtime。"""
    try:
        mdb = d / "db_storage" / "message"
        if mdb.is_dir():
            return max(f.stat().st_mtime for f in mdb.glob("*.db"))
    except Exception:
        pass
    return d.stat().st_mtime


def discover_accounts(root: Path | None = None) -> list[Path]:
    base = root or Path(os.path.expandvars(r"%USERPROFILE%\xwechat_files"))
    if not base.is_dir():
        return []
    found = [d for d in base.iterdir() if _looks_like_account(d)]
    found.sort(key=_recency, reverse=True)
    return found


def resolve() -> tuple[Path, str]:
    """返回 (account_dir, account_id);找不到时给出可操作的报错。"""
    cfg = _cfg_account()
    override = cfg.get("account_dir") or os.environ.get("WECHAT_ACCOUNT_DIR")
    if override:
        p = Path(os.path.expandvars(str(override)))
        if not _looks_like_account(p):
            raise SystemExit(
                f"[paths] 配置的微信账号目录无效(缺少 db_storage/message 或 msg/attach): {p}")
        return p, p.name

    root = Path(os.path.expandvars(str(cfg["data_root"]))) if cfg.get("data_root") else None
    cands = discover_accounts(root)
    if not cands:
        raise SystemExit(
            "[paths] 未找到微信账号目录。请确认微信 4.x 已在本机登录过,然后:\n"
            '  1. backend/config.json 增加 "account": {"account_dir": "C:\\\\Users\\\\你\\\\xwechat_files\\\\wxid_xxx"}\n'
            "  2. 或设置环境变量 WECHAT_ACCOUNT_DIR 指向账号目录")
    if len(cands) > 1:
        print(f"[paths] 发现 {len(cands)} 个微信账号,使用最近使用的 {cands[0].name};"
              f"如需切换请在 config.json 的 account.account_dir 指定")
        for d in cands[1:]:
            print(f"        其他账号: {d}")
    return cands[0], cands[0].name


ACCOUNT_DIR, ACCOUNT_ID = resolve()
DECRYPTED_DIR = Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "ChatTrace" / \
    "accounts" / ACCOUNT_ID / "decrypted"

if __name__ == "__main__":
    print(f"account_dir = {ACCOUNT_DIR}")
    print(f"account_id  = {ACCOUNT_ID}")
    print(f"decrypted   = {DECRYPTED_DIR}  (存在: {DECRYPTED_DIR.is_dir()})")
