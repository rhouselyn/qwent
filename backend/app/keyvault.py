# -*- coding: utf-8 -*-
"""跨平台数据库密钥存储与导入。

- Windows: 复用 ChatTrace 的 DPAPI 密钥库（%LOCALAPPDATA%/ChatTrace/keys）。
- macOS:   Keychain（`security add-generic-password`，服务名 wechat-knowledge-assistant）；
           `security` 不可用时退回 ~/.local/state 下 0600 权限的密钥文件。
- 密钥格式: 64 hex 字符（32 字节主密钥）。

macOS 没有可用的进程内存扫描/DLL 内部钥方案（那两个都是 Windows 专属），
取钥走"一次性手动导入"：用任意取钥工具拿到 hex 密钥后
  python backend/app/keyvault.py --account-dir <账号目录> --import <64hex>
导入前会用 HMAC 对消息库实测校验；又因密钥账号级稳定（跨重启不变），
导一次即可长期使用，服务端会自动做 HMAC 自愈检测。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SERVICE = "wechat-knowledge-assistant"


def is_mac() -> bool:
    return sys.platform == "darwin"


def is_windows() -> bool:
    return os.name == "nt"


# ---------------------------------------------------------------- 平台后端

def _keychain_store(account_id: str, key_hex: str) -> Path | None:
    r = subprocess.run(
        ["security", "-i", "add-generic-password", "-U", "-s", SERVICE,
         "-a", account_id, "-w", key_hex],
        capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return None
    return _keychain_path()


def _keychain_load(account_id: str) -> bytes | None:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE,
         "-a", account_id, "-w"],
        capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return None
    try:
        return bytes.fromhex(r.stdout.strip())
    except ValueError:
        return None


def _fallback_dir() -> Path:
    d = Path.home() / ".local" / "state" / "wechat-knowledge-assistant"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _keychain_path() -> Path:
    return _fallback_dir() / "keychain-via-security.flag"


def _file_store(account_id: str, key_hex: str) -> Path:
    p = _fallback_dir() / f"{account_id}.key"
    p.write_text(key_hex, encoding="ascii")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def _file_load(account_id: str) -> bytes | None:
    p = _fallback_dir() / f"{account_id}.key"
    if not p.is_file():
        return None
    try:
        return bytes.fromhex(p.read_text("ascii").strip())
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------- 对外接口

def store_key(account_id: str, password: bytes, source: str = "manual") -> Path:
    """存 32 字节主密钥；返回存储位置。Windows 走 ChatTrace DPAPI。"""
    key_hex = password.hex()
    if is_windows():
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "refs" / "ChatTrace" / "src"))
        from chattrace.keyagent import keystore
        from chattrace.models import KeyInfo
        info = KeyInfo(account_id=account_id, wechat_version=source,
                       password=password, source=source, verified=True)
        return keystore.store_key(info)
    if is_mac():
        p = _keychain_store(account_id, key_hex)
        if p:
            return p
    return _file_store(account_id, key_hex)


def load_key(account_id: str) -> bytes | None:
    """读取主密钥；没有则 None。与 store_key 的后端选择一致。"""
    if is_windows():
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "refs" / "ChatTrace" / "src"))
            from chattrace.keyagent import keystore
            return keystore.load_key(account_id).password
        except Exception:
            return None
    if is_mac():
        pw = _keychain_load(account_id)
        if pw:
            return pw
    return _file_load(account_id)


def validate_key(password: bytes, message_db: Path) -> tuple[bool, str]:
    """HMAC 实测密钥能否打开消息库。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "refs" / "ChatTrace" / "src"))
    from chattrace.keyagent.verify import test_key_against_db
    return test_key_against_db(password, message_db)


def import_manual_key(account_dir: str | Path, key_hex: str) -> dict:
    """校验并入库手动导入的密钥。返回 {ok, detail}。"""
    account_dir = Path(account_dir)
    try:
        key = bytes.fromhex((key_hex or "").strip())
    except ValueError:
        return {"ok": False, "detail": "密钥不是合法 hex"}
    if len(key) != 32:
        return {"ok": False, "detail": f"需要 32 字节(64 hex)，得到 {len(key)}"}
    msg_db = account_dir / "db_storage" / "message" / "message_0.db"
    if not msg_db.exists():
        return {"ok": False, "detail": f"找不到消息库 {msg_db}"}
    ok, detail = validate_key(key, msg_db)
    if not ok:
        return {"ok": False, "detail": f"HMAC 校验失败: {detail}"}
    target = store_key(account_dir.name, key, source="manual-import")
    return {"ok": True, "detail": detail, "stored_at": str(target),
            "imported_at": time.time()}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="跨平台数据库密钥导入/查询")
    ap.add_argument("--account-dir", required=True)
    ap.add_argument("--import", dest="key_hex", default=None,
                    help="64 hex 主密钥（mac 上从任意取钥工具获取后粘贴）")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    acc = Path(args.account_dir)
    if args.key_hex:
        r = import_manual_key(acc, args.key_hex)
        print(("导入成功: " if r["ok"] else "导入失败: ") + r["detail"])
        sys.exit(0 if r["ok"] else 1)
    pw = load_key(acc.name)
    if args.status or True:
        if not pw:
            print(f"[{acc.name}] 无已存密钥")
        else:
            db = acc / "db_storage" / "message" / "message_0.db"
            if db.exists():
                ok, detail = validate_key(pw, db)
                print(f"[{acc.name}] 密钥存在, HMAC {'通过' if ok else '失效'} ({detail})")
            else:
                print(f"[{acc.name}] 密钥存在(长度 {len(pw)}B)，未找到消息库无法校验")
