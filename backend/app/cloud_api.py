# -*- coding: utf-8 -*-
"""云端分流控制面：设置页开关、状态、手动同步、基线、连通性自检。"""
from __future__ import annotations

import base64
import os
import secrets
import time

from fastapi import APIRouter, HTTPException

import cloud_conf

router = APIRouter()


def _side_module():
    if cloud_conf.IS_CLOUD:
        import cloud_apply as m
    else:
        import cloud_sync_local as m
    return m


@router.get("/api/cloud/status")
def api_cloud_status():
    try:
        extra = _side_module().status()
    except Exception as exc:      # noqa: BLE001
        extra = {"error": str(exc)[:200]}
    return {"ok": True, "is_cloud": cloud_conf.IS_CLOUD,
            "enabled": cloud_conf.enabled(), "takeover": cloud_conf.takeover(),
            **extra}


@router.get("/api/cloud/config")
def api_cloud_config():
    return {"ok": True, "config": cloud_conf.mask_conf(cloud_conf.cloud_conf())}


@router.post("/api/cloud/config")
def api_cloud_config_set(payload: dict):
    if cloud_conf.IS_CLOUD:
        raise HTTPException(403, "云端配置请用命令行修改，本地设置页才是唯一的开关入口")
    conf = cloud_conf.load_conf()
    c = dict(cloud_conf.cloud_conf(conf))
    for k, v in (payload or {}).items():
        if k not in cloud_conf.DEFAULT_CLOUD:
            continue
        if isinstance(v, str) and v == "********":
            continue          # 掩码回显，表示不修改
        if k == "encrypt_key" and not str(v or "").strip():
            continue          # 密钥自动生成，空值不能把已有的那把冲掉
        c[k] = v
    # 首次开启时自动生成 32 字节对称密钥并落盘；用户无需手动填写。
    # 注意：换服务器 / 重新部署时要把这把密钥复制到云端 config.json，否则解不开包。
    if c.get("enabled") and not (c.get("encrypt_key")
                                 or os.environ.get("QWENT_CLOUD_KEY")):
        c["encrypt_key"] = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    conf["cloud"] = c
    cloud_conf.save_conf(conf)
    try:
        import cloud_sync_local
        if c.get("enabled") and c.get("auto", True):
            cloud_sync_local.start()
        else:
            cloud_sync_local.stop()
    except Exception:
        pass
    # 开启且接管机器人时，立刻停本机微信网关：同一凭据双端在线会互踢。
    # 关闭开关时不自动拉起，用户可在「机器人设置」里手动启用本地通道。
    if c.get("enabled") and c.get("takeover_bots", True):
        try:
            import main
            main._kill_gateways()
        except Exception:      # noqa: BLE001
            pass
        try:
            import qqbot
            qqbot.apply_config(cloud_conf.effective_bot_conf(conf))
        except Exception:      # noqa: BLE001
            pass
    return {"ok": True, "config": cloud_conf.mask_conf(c)}


@router.post("/api/cloud/test")
def api_cloud_test():
    try:
        cloud_conf.encrypt_key()
        import cloud_transport
        tr = cloud_transport.get_transport()
        return {"ok": True, **tr.test()}
    except Exception as exc:      # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}


@router.post("/api/cloud/sync")
def api_cloud_sync(payload: dict | None = None):
    if cloud_conf.IS_CLOUD:
        import cloud_apply
        return cloud_apply.apply_next()
    import cloud_sync_local
    return cloud_sync_local.sync_now(force=bool((payload or {}).get("force")))


@router.post("/api/cloud/baseline")
def api_cloud_baseline():
    if cloud_conf.IS_CLOUD:
        raise HTTPException(403, "全量基线由本地生成，云端请用 restore 命令")
    import cloud_sync_local
    return cloud_sync_local.baseline()


@router.get("/api/cloud/logs")
def api_cloud_logs():
    st = cloud_conf.read_state()
    return {"ok": True, "state": st, "status": _side_module().status()}


@router.get("/cloud/v1/health")
def api_cloud_health():
    """云端健康检查：切换机器人归属前必须先拿到 ok=true。"""
    try:
        extra = _side_module().status()
        return {"ok": True, "is_cloud": cloud_conf.IS_CLOUD,
                "ts": int(time.time()), **extra}
    except Exception as exc:      # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
