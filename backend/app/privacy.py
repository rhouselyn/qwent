# -*- coding: utf-8 -*-
"""出站脱敏：发给云端模型前替换手机号/身份证/银行卡/邮箱。

只处理“发出去的请求”，不改动本地入库数据；规则可在设置页逐项开关。
占位符固定为 [手机号] 这类短标签，不改变句子结构，也不影响回答里的 [n] 引用编号。
"""
from __future__ import annotations

import re

DEFAULTS = {
    "enabled": True,
    "phone": True,
    "idcard": True,
    "bankcard": True,
    "email": True,
}

# 顺序：身份证先于银行卡（18 位数字两者都可能匹配），手机号独立。
# 边界用 (?<!\d)/(?!\d)，避免把一长串数字截成半段。
_PATTERNS = [
    ("idcard", re.compile(r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}"
                          r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")),
    ("bankcard", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("phone", re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")),
    ("email", re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?!\w)")),
]

_LABELS = {"phone": "手机号", "idcard": "身份证", "bankcard": "银行卡", "email": "邮箱"}


def options(conf: dict | None) -> dict:
    """读取有效规则；配置缺失时按需求默认开启全部规则。"""
    raw = (conf or {}).get("privacy") or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in raw:
            out[k] = bool(raw[k])
    return out


def redact_text(text: str, conf: dict | None = None) -> str:
    if not text:
        return text
    opt = options(conf)
    if not opt["enabled"]:
        return text
    for key, pattern in _PATTERNS:
        if opt.get(key):
            text = pattern.sub(f"[{_LABELS[key]}]", text)
    return text


def _walk(value, conf):
    if isinstance(value, str):
        return redact_text(value, conf)
    if isinstance(value, list):
        return [_walk(v, conf) for v in value]
    if isinstance(value, tuple):
        return tuple(_walk(v, conf) for v in value)
    if isinstance(value, dict):
        return {k: _walk(v, conf) for k, v in value.items()}
    return value


def redact_messages(conf: dict | None, messages: list) -> list:
    """递归脱敏 messages（含 tool_calls 参数与 role=tool 内容）。"""
    if not options(conf)["enabled"]:
        return messages
    return [_walk(m, conf) for m in messages]
