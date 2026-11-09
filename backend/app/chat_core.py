# -*- coding: utf-8 -*-
"""微信 / QQ 共用的消息业务内核。

平台差异（解密方式、数据表、字段、皮肤、Agent 提示词、通道凭据）留在各自的
Source 子类；两边都要用的逻辑只有这一份：SSE 实时更新、置顶、文件定位、
文件分类、分页/排序/时间窗。修一处，微信与 QQ 同时生效。

导入方向：本模块只依赖 fastapi 与标准库，绝不 import main / qq_local，
避免和 main.py 的顶层导入形成循环。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

# 文件类型表：两个平台口径不同（微信按用途分类，QQ 按扩展名分类），
# 分类逻辑共用一个 file_kind()，差异只在这两张表里。
WECHAT_KINDS = {
    "doc": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt"},
    "sheet": {".xls", ".xlsx", ".xlsm", ".csv"},
    "slide": {".ppt", ".pptx", ".pptm"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "media": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".amr",
              ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".silk"},
}
QQ_KINDS = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"},
    "video": {".mp4", ".mov", ".avi", ".mkv", ".webm"},
    "audio": {".mp3", ".wav", ".amr", ".silk", ".m4a"},
    "pdf": {".pdf"}, "doc": {".doc", ".docx"}, "xls": {".xls", ".xlsx"},
    "ppt": {".ppt", ".pptx"}, "txt": {".txt"}, "md": {".md"},
    "csv": {".csv"}, "zip": {".zip"}, "rar": {".rar"}, "7z": {".7z"},
}


def clamp(value, lo: int = 1, hi: int = 200, default: int | None = None) -> int:
    """分页/条数钳制：两边所有 limit/offset/n 走同一个边界，别各写一遍。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = lo if default is None else default
    return max(lo, min(v, hi))


def since_ts(days, now: float | None = None) -> int:
    """最近 N 天 → Unix 时间戳；两边筛选共用同一个时间基准。"""
    return int(now if now is not None else time.time()) - max(0, int(days or 0)) * 86400


def sort_sql(sort: str, mapping: dict, default: str) -> str:
    """排序白名单 → SQL ORDER BY；未知值一律回落默认，绝不拼进用户输入。"""
    return mapping.get(sort, default)


def sort_items(items: list, sort: str, ts_key: str = "ts",
               size_key: str = "size") -> list:
    """Python 侧排序（微信文件/链接中心）；与 sort_sql 同一套语义。"""
    if sort == "old":
        items.sort(key=lambda x: x.get(ts_key) or 0)
    elif sort == "size":
        items.sort(key=lambda x: x.get(size_key) or 0, reverse=True)
    return items


def slice_items(items: list, offset: int, n: int) -> list:
    return items[max(0, offset):max(0, offset) + max(0, n)]


def file_kind(name_or_ext: str, taxonomy: dict, default: str = "other") -> str:
    """按扩展名分类；name 与 ".ext" 两种入参都接受。"""
    s = str(name_or_ext or "").lower()
    ext = s if s.startswith(".") else Path(s).suffix.lower()
    for kind, exts in taxonomy.items():
        if ext in exts:
            return kind
    return default


def set_pinned(con, table: str, key_col: str, key: str, pinned: bool,
               prepare: Callable | None = None) -> dict:
    """会话置顶：两个平台只有表名/键名不同，迁移钩子由调用方注入。"""
    if prepare:
        prepare(con)
    val = 1 if pinned else 0
    con.execute(f"UPDATE {table} SET pinned=? WHERE {key_col}=?", (val, key))
    con.commit()
    return {"ok": True, key_col: key, "pinned": val}


def reveal_path(path, missing: str = "文件不在本地（可能已被清理或未下载）") -> dict:
    """在资源管理器定位文件：两边只差一句 404 文案。"""
    p = str(path or "")
    if not p or not Path(p).is_file():
        raise HTTPException(404, missing)
    try:
        subprocess.Popen(["explorer.exe", f"/select,{p}"])
    except Exception as e:      # noqa: BLE001
        raise HTTPException(500, f"打开所在位置失败：{e}")
    return {"ok": True}


def sse_events(fetch, after_id: int = 0, *, version=None,
               poll: float = 0.4, idle_limit: int = 10, batch: int = 40):
    """两平台唯一的 SSE 事件生成器（同步生成器，便于直接测试）。

    fetch(last, limit) -> (要推送的消息列表, 新水位)。消息列表可以为空，
    水位仍要推进（微信撤回/噪声消息被过滤后不该反复重查同一批）。
    version() 可选：返回值变化时额外推 {"chats": True}（QQ 用它刷新会话列表）。
    """
    last = int(after_id or 0)
    seen = version() if version else None
    idle = 0
    nn = "\n\n"
    yield "retry: 3000" + nn
    while True:
        try:
            rows, new_last = fetch(last, batch)
            rows = list(rows or [])
            last = max(last, int(new_last or 0))
            changed = False
            if version:
                cur = version()
                if cur != seen:
                    seen, changed = cur, True
            if rows:
                payload = json.dumps({"messages": rows, "max_id": last},
                                     ensure_ascii=False)
                yield "data: " + payload + nn
                idle = 0
            if changed:
                yield "data: " + json.dumps({"chats": True},
                                           ensure_ascii=False) + nn
                idle = 0
            if not rows and not changed:
                idle += 1
                if idle >= idle_limit:
                    idle = 0
                    yield ": ping" + nn
        except Exception:
            yield ": error" + nn
        time.sleep(poll)


def sse_response(fetch, after_id: int = 0, *, version=None,
                 poll: float = 0.4, idle_limit: int = 10, batch: int = 40):
    """包装成 FastAPI StreamingResponse；帧逻辑全部在 sse_events。"""
    return StreamingResponse(sse_events(fetch, after_id, version=version,
                                        poll=poll, idle_limit=idle_limit,
                                        batch=batch),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


class ChatSource:
    """平台基类。子类只声明差异；共用行为全部继承。"""

    platform = ""
    chat_table = ""
    chat_key = ""
    taxonomy = WECHAT_KINDS
    kind_default = "other"
    sse_poll = 0.4
    sse_idle = 10
    reveal_missing = "文件不在本地（可能已被清理或未下载）"

    def __init__(self, prepare: Callable | None = None):
        self.prepare = prepare

    def pin(self, con, key: str, pinned: bool) -> dict:
        return set_pinned(con, self.chat_table, self.chat_key, key, pinned,
                          self.prepare)

    def sse(self, after_id, fetch, *, version=None, batch: int = 40):
        return sse_response(fetch, after_id, version=version,
                            poll=self.sse_poll, idle_limit=self.sse_idle,
                            batch=batch)

    def reveal(self, path) -> dict:
        return reveal_path(path, self.reveal_missing)

    def file_kind(self, name_or_ext: str) -> str:
        return file_kind(name_or_ext, self.taxonomy, self.kind_default)


class WeChatSource(ChatSource):
    platform = "wechat"
    chat_table = "chat"
    chat_key = "username"


class QQLocalSource(ChatSource):
    platform = "qq"
    chat_table = "qq_chat"
    chat_key = "conv"
    taxonomy = QQ_KINDS
    kind_default = "file"
    sse_poll = 0.5
    sse_idle = 30
    reveal_missing = "文件不在本机（可能已被清理或未下载）"
