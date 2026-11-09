# -*- coding: utf-8 -*-
"""云端分流公共配置与运行模式判定。

本地与云端**共用同一份源码**，靠环境变量 QWENT_CLOUD=1 区分：
  * 本地：完整功能，机器人可在本机或由云端接管；
  * 云端：最小运行单元，只跑问答 + 两个机器人网关 + 增量应用。

本模块只依赖标准库，禁止 import main / agent / botcore，避免循环依赖。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from pathlib import Path

IS_CLOUD = os.environ.get("QWENT_CLOUD", "").strip() == "1"

PROJECT = Path(__file__).resolve().parent.parent.parent

DEFAULT_CLOUD = {
    "enabled": False,
    "takeover_bots": True,
    "auto": True,
    "interval_min": 10,
    "transport": "file",
    "endpoint": "",
    "bucket": "",
    "region": "",
    "access_key": "",
    "secret_key": "",
    "encrypt_key": "",
    "prefix": "qwent/",
    "max_pack_mb": 64,
    "keep_deltas": 200,
    "baseline_chunk_mb": 64,
    # SSH 直传参数；必须列在 DEFAULT_CLOUD 里，设置页 POST 才能保存
    "scp_user": "root",
    "scp_key": "",
    "scp_dir": "/opt/qwent/inbox",
    "scp_port": 22,
}

_STATE_LOCK = threading.Lock()


def data_dir() -> Path:
    d = os.environ.get("QWENT_DATA_DIR")
    return Path(d) if d else PROJECT / "data"


def models_dir() -> Path:
    d = os.environ.get("QWENT_MODELS_DIR")
    return Path(d) if d else PROJECT / "models"


def config_path() -> Path:
    d = os.environ.get("QWENT_CONFIG")
    return Path(d) if d else PROJECT / "backend" / "config.json"


def load_conf() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_conf(conf: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def cloud_conf(conf: dict | None = None) -> dict:
    c = dict(DEFAULT_CLOUD)
    c.update((conf if conf is not None else load_conf()).get("cloud") or {})
    return c


def enabled(conf: dict | None = None) -> bool:
    return bool(cloud_conf(conf).get("enabled"))


def takeover(conf: dict | None = None) -> bool:
    c = cloud_conf(conf)
    return bool(c.get("enabled")) and bool(c.get("takeover_bots", True))


def effective_bot_conf(conf: dict) -> dict:
    """机器人归属的唯一判定入口。

    云端：按原配置正常启动两个通道。
    本地且开关开启：强制下线本机 qqbot / OpenClaw Gateway（同一凭据双端会互踢），
    但**保留 agent_api.api_key** —— 云端用同一把钥匙校验 /v1/chat/completions。
    """
    c = {k: (dict(v) if isinstance(v, dict) else v) for k, v in conf.items()}
    if IS_CLOUD:
        return c
    if takeover(conf):
        c.setdefault("qqbot", {})["enabled"] = False
        c["_gateway_owned_by"] = "cloud"
    return c


def encrypt_key(conf: dict | None = None) -> bytes:
    """32 字节对称密钥：优先环境变量，其次配置。缺省报错而非静默用弱密钥。"""
    raw = (os.environ.get("QWENT_CLOUD_KEY")
           or cloud_conf(conf).get("encrypt_key") or "").strip()
    if not raw:
        raise ValueError("未配置 cloud.encrypt_key / QWENT_CLOUD_KEY")
    try:
        k = base64.b64decode(raw, validate=True)
    except Exception:
        k = raw.encode("utf-8")
    return k if len(k) == 32 else hashlib.sha256(k).digest()


def state_file() -> Path:
    return data_dir() / "cloud_state.json"


def read_state() -> dict:
    p = state_file()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(patch: dict) -> dict:
    with _STATE_LOCK:
        st = read_state()
        st.update(patch)
        p = state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return st


def mask_conf(c: dict) -> dict:
    """给前端回显用：任何密钥字段只显示是否已配置。"""
    out = {}
    for k, v in c.items():
        # scp_key 是本机私钥的「路径」，不是密钥内容，回显出来才能在设置页编辑
        if k == "scp_key":
            out[k] = v
        elif any(t in k.lower() for t in ("key", "secret", "token", "password")):
            out[k] = "********" if v else ""
        else:
            out[k] = v
    return out
