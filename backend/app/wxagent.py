# -*- coding: utf-8 -*-
"""微信 Agent（ClawBot / OpenClaw 通道）。

只负责微信入口：指令、帮助、现状、对话状态、问答。
状态单独存 data/wxagent_state.json，不再和 QQ 机器人共用 qqbot_state.json。
首次运行会把旧文件里 "openclaw" 这个键迁移过来（旧文件保留不删）。
"""
from __future__ import annotations

from pathlib import Path

import botcore as C

PROJECT = Path(__file__).resolve().parent.parent.parent
DATA = PROJECT / "data"
STATE_PATH = DATA / "wxagent_state.json"
LEGACY_PATH = DATA / "qqbot_state.json"
MAIN_OPENID = "weixin-main"          # 微信通道只有扫码登录的那个账号

PLATFORM = "微信"


def _legacy(raw: dict) -> dict:
    ent = raw.get("openclaw")
    return {MAIN_OPENID: ent} if isinstance(ent, dict) else {}


STORE = C.Store(STATE_PATH, [(LEGACY_PATH, _legacy)])


def conf() -> dict:
    """微信通道独立配置段（wxagent），缺省项与 QQ 通道保持同形。"""
    try:
        import main as M
        cfg = (M.load_conf().get("wxagent") or {})
    except Exception:
        cfg = {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "default_explorer": bool(cfg.get("default_explorer", False)),
        "default_thinking": bool(cfg.get("default_thinking", False)),
        "citations": False,
        "wechat_chat": None,
    }


def entity() -> dict:
    _s, ent = STORE.entity(MAIN_OPENID, conf())
    return ent


def command(text: str):
    """命中指令返回中文回复；普通问题返回 None。"""
    act = C.parse_command(text)
    if not act:
        return None
    return C.execute_command(
        act, openid=MAIN_OPENID, store=STORE, conf=conf(),
        platform=PLATFORM,
        extra_status="入口：微信 ClawBot · 指令由本机直接处理，不消耗模型调用。")


def help_text() -> str:
    return C.help_text(entity(), platform=PLATFORM)


def status_text() -> str:
    return C.status_text(entity(), platform=PLATFORM,
                         extra="入口：微信 ClawBot · 指令由本机直接处理。")


def paused() -> bool:
    return bool(entity().get("paused"))


def ask(question: str, history: list | None = None) -> str:
    return C.ask(MAIN_OPENID, question, store=STORE, conf=conf(),
                 channel="weixin", history=history)


# 兼容别名（旧代码若有引用不会炸）；帮助文本按「当前状态」动态生成
channel_command = command
ask_wechat = ask
