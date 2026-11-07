# -*- coding: utf-8 -*-
"""QQ Agent（QQ 官方机器人通道）。

QQ 侧的全部「人」的逻辑都在这里：指令、帮助、现状、菜单、对话状态、问答。
状态单独存 data/qqagent_state.json，首次运行把旧 qqbot_state.json 中
除 "openclaw"（微信通道）以外的用户迁移过来，旧文件保留不删。

qqbot.py 只保留 WebSocket 网关与收发消息，不再掺微信通道代码。
"""
from __future__ import annotations

import logging
from pathlib import Path

import requests

import botcore as C

PROJECT = Path(__file__).resolve().parent.parent.parent
DATA = PROJECT / "data"
STATE_PATH = DATA / "qqagent_state.json"
LEGACY_PATH = DATA / "qqbot_state.json"

PLATFORM = "QQ"
log = logging.getLogger("qqagent")

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

STORE = C.Store(STATE_PATH, [(LEGACY_PATH, "drop_openclaw")])

# 菜单与 /帮助 的能力一一对应；send_message 点击后填入输入框，用户发出后走
# 同一套 parse_command（这些文案都不带斜杠，照样能命中）。
MENU_ITEMS = [
    {"type": "send_message", "name": C.ACTION_MENU["help"],
     "send_message": "帮助"},
    {"type": "menu", "name": "提问方式", "sub_menu_items": [
        {"type": "send_message", "name": C.ACTION_MENU["fast"], "send_message": "快速"},
        {"type": "send_message", "name": C.ACTION_MENU["explore"], "send_message": "探索"},
    ]},
    {"type": "menu", "name": "思考深度", "sub_menu_items": [
        {"type": "send_message", "name": C.ACTION_MENU["think_on"], "send_message": "思考"},
        {"type": "send_message", "name": C.ACTION_MENU["think_off"], "send_message": "关思考"},
    ]},
    {"type": "menu", "name": "更多", "sub_menu_items": [
        {"type": "send_message", "name": C.ACTION_MENU["new"], "send_message": "新对话"},
        {"type": "send_message", "name": C.ACTION_MENU["pause"], "send_message": "暂停"},
        {"type": "send_message", "name": C.ACTION_MENU["resume"], "send_message": "继续"},
        {"type": "send_message", "name": C.ACTION_MENU["status"], "send_message": "状态"},
    ]},
]


def entity(openid: str, conf: dict | None = None) -> dict:
    _s, ent = STORE.entity(openid, conf or {})
    return ent


def _bot_status() -> str:
    try:
        import qqbot as Q
        st = Q.status()
        state = ("已连接" if st.get("connected") else
                 "连接中" if st.get("running") else "未运行")
        return f"QQ 机器人：{state}" + (f" · {st['error']}" if st.get("error") else "")
    except Exception:
        return ""


def command(openid: str, text: str, conf: dict | None = None):
    """命中指令返回中文回复；普通问题返回 None。"""
    act = C.parse_command(text)
    if not act:
        return None
    return C.execute_command(
        act, openid=openid, store=STORE, conf=conf or {},
        platform=PLATFORM, menu_hint=True,
        extra_status=_bot_status())


def help_text(openid: str, conf: dict | None = None) -> str:
    return C.help_text(entity(openid, conf), platform=PLATFORM, menu_hint=True)


def status_text(openid: str, conf: dict | None = None) -> str:
    return C.status_text(entity(openid, conf), platform=PLATFORM,
                         extra=_bot_status())


def paused(openid: str, conf: dict | None = None) -> bool:
    return bool(entity(openid, conf).get("paused"))


def ask(openid: str, question: str, conf: dict | None = None) -> str:
    return C.ask(openid, question, store=STORE, conf=conf or {}, channel="qq")


# ---------------------------------------------------------------- 菜单推送
def _req(method: str, url: str, **kw):
    """优先按系统代理请求；代理没开（ProxyError）时自动直连。

    Windows 的「Internet 选项」代理经常残留成死端口，而 QQ 官方接口本身
    在境内可直连，不应该因为代理失效就让机器人整条掉线。
    """
    try:
        return requests.request(method, url, **kw)
    except requests.exceptions.ProxyError:
        s = requests.Session()
        s.trust_env = False
        try:
            return s.request(method, url, **kw)
        finally:
            s.close()


def _fetch_token(conf_q: dict) -> str:
    r = _req("POST", TOKEN_URL, timeout=15, json={
        "appId": conf_q.get("app_id"), "clientSecret": conf_q.get("app_secret")})
    r.raise_for_status()
    d = r.json()
    if not d.get("access_token"):
        raise RuntimeError(f"获取 access_token 失败: {d}")
    return d["access_token"]


def set_menu(conf_q: dict) -> dict:
    """把菜单推到 QQ（等价于控制台手工配置）。"""
    try:
        r = _req("PUT", f"{API_BASE}/v2/menu", timeout=15,
                 json={"menu": {"items": MENU_ITEMS}},
                 headers={"Authorization": f"QQBot {_fetch_token(conf_q)}"})
        ok = r.status_code < 400
        log.info("set_menu -> %s %s", r.status_code, r.text[:200])
        return {"ok": ok, "status": r.status_code, "error": "" if ok else r.text[:300]}
    except Exception as exc:  # noqa: BLE001
        log.warning("set_menu failed: %s", exc)
        return {"ok": False, "status": 0, "error": str(exc)[:300]}


def get_menu(conf_q: dict) -> dict:
    try:
        r = _req("GET", f"{API_BASE}/v2/menu", timeout=15,
                 headers={"Authorization": f"QQBot {_fetch_token(conf_q)}"})
        return {"ok": r.status_code < 400, "status": r.status_code,
                "body": r.json() if r.status_code < 400 else r.text[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 0, "body": str(exc)[:300]}
