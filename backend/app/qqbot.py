# -*- coding: utf-8 -*-
"""QQ 机器人（本机直连版）。

微信助手内置的 QQ 官方机器人接入：WebSocket 网关出站连接（无需公网 IP），
REST API 被动回复；大脑直接复用 main.api_ask 的微信 agent。

  * 上下文 = 微信近期时间线（跨会话）+ 该 QQ 用户的对话轮次，按时间合并
  * 合并转发在本地展开；QQ 渠道默认不附引用块（qqbot.citations 可开）
  * 内置指令：帮助 / 新对话 / 暂停 / 继续

配置存 backend/config.json 的 "qqbot" 键：
  {"enabled": false, "app_id": "", "app_secret": "", "group_reply": false,
   "citations": false, "max_msg_chars": 700, "max_chunks": 4}
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from pathlib import Path

import requests
import websocket

from paths import PROJECT

log = logging.getLogger("qqbot")

if not log.handlers:                  # 独立文件日志，方便排查收发问题
    _h = logging.FileHandler(PROJECT / "backend" / "qqbot.log", encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
FALLBACK_GATEWAY = "wss://api.sgroup.qq.com/websocket"
INTENTS = 1 << 25                     # GROUP_AND_C2C（私聊 + 群@）

OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY, OP_RESUME = 0, 1, 2, 6
OP_RECONNECT, OP_INVALID, OP_HELLO, OP_HEARTBEAT_ACK = 7, 9, 10, 11

RECONNECT_DELAYS = [1, 2, 5, 10, 30, 60]
MAX_MSG_CHARS = 700                   # 官方无明确值，超限报 40054007，按社区经验值分片
MAX_CHUNKS = 4                        # 被动回复同一 msg_id 上限 5 次留余量

STATE_PATH = PROJECT / "data" / "qqbot_state.json"

DEFAULT_CONF = {
    "enabled": False,
    "app_id": "",
    "app_secret": "",
    "group_reply": False,
    "citations": False,
    "wechat_chat": None,              # 锁定某个微信会话为默认上下文；None = 全局
    "default_explorer": False,        # 新用户默认提问方式：探索模式
    "default_thinking": False,        # 新用户默认深度思考
    "max_msg_chars": MAX_MSG_CHARS,
    "max_chunks": MAX_CHUNKS,
    "allow_openids": [],              # 空 = 不限制
}

STATUS: dict = {
    "running": False, "connected": False, "username": "", "error": "",
    "last_event": "", "users": 0, "started_at": "",
    "menu": "", "menu_error": "",
}

_runner: "QQBotRunner | None" = None
_apply_lock = threading.Lock()


def conf_of(main_conf: dict) -> dict:
    q = dict(DEFAULT_CONF)
    q.update(main_conf.get("qqbot") or {})
    return q


def status() -> dict:
    return dict(STATUS)


def apply_config(main_conf: dict) -> None:
    """按最新配置启动/停止/重连 bot（配置保存后与进程启动时调用）。"""
    global _runner
    with _apply_lock:
        q = conf_of(main_conf)
        want = bool(q.get("enabled") and q.get("app_id") and q.get("app_secret"))
        if _runner and not want:
            _runner.stop()
            _runner = None
        if want:
            if _runner and _runner.signature(q) and _runner.is_alive():
                _runner.refresh_conf(q)     # 仅运行参数变化，不重启连接
            else:
                if _runner:
                    _runner.stop()
                _runner = QQBotRunner(q)
                _runner.start()


def shutdown() -> None:
    """服务退出时断开网关，避免托盘退出后残留连接线程。"""
    global _runner
    with _apply_lock:
        if _runner:
            _runner.stop()
            _runner = None


# ================================================================ 对话状态


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("save state failed: %s", exc)


# ================================================================ 文本渲染


def fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts or 0)))
    except (ValueError, OSError):
        return ""


def render_record(parsed: dict, limit: int = 20) -> str:
    rec = parsed.get("record") or {}
    items = rec.get("items") or []
    title = parsed.get("title") or "聊天记录"
    lines = [f"[合并转发《{title}》共 {rec.get('count') or len(items)} 条]"]
    info = re.sub(r"\s+", " ", str(rec.get("info") or "")).strip()
    if info:
        lines.append(info[:400])
    for it in items[:limit]:
        who, when = it.get("name") or "", it.get("time") or ""
        body = re.sub(r"\s+", " ", str(it.get("text") or it.get("title")
                                       or it.get("url") or "")).strip()
        lines.append((f"{when} {who}: {body}" if (when or who) else body)[:300])
    if len(items) > limit:
        lines.append(f"……其余 {len(items) - limit} 条未展开")
    return "\n".join(x for x in lines if x)


def msg_text(msg: dict, cap: int = 400) -> str:
    parsed = msg.get("parsed") if isinstance(msg.get("parsed"), dict) else {}
    if parsed.get("record"):
        return render_record(parsed)
    text = str(parsed.get("text") or parsed.get("system") or "")
    kind = parsed.get("kind") or ""
    if not text:
        if kind == "file":
            text = f"[文件] {parsed.get('title') or ''}"
        elif kind == "link":
            text = f"[链接] {parsed.get('title') or ''}"
        elif kind == "quote":
            ref = parsed.get("refer") or {}
            text = f"[引用 {ref.get('who') or ''}] {parsed.get('title') or ''}"
        elif kind:
            text = f"[{kind}]"
    if parsed.get("ocr_text"):
        text += f"（图片文字: {parsed['ocr_text'][:300]}）"
    if parsed.get("asr_text"):
        text += f"（语音转写: {parsed['asr_text'][:300]}）"
    if parsed.get("url") and parsed["url"] not in text:
        text += f" {parsed['url']}"
    if parsed.get("des") and parsed["des"] not in text:
        text += f" {parsed['des'][:200]}"
    # appmsg 解析失败时会剩下整段 XML，直接塞进时间线会污染上下文
    if "<msg" in text or "<?xml" in text:
        mt = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", text, re.S)
        md = re.search(r"<des>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</des>", text, re.S)
        label = (mt.group(1).strip() if mt else "") or (md.group(1).strip() if md else "")
        text = ("[消息] " + label) if label else re.sub(r"<[^>]{1,200}>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:cap]


def strip_citation_markers(text: str) -> str:
    out = re.sub(r"\[W\d{1,3}\]", "", text or "", flags=re.I)
    out = re.sub(r"(?<!!)\[(\d{1,3})\](?!\()", "", out)
    out = re.sub(r"[ \t]+([，。！？；：,.!?;:])", r"\1", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"（\s*）|\(\s*\)", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def md_to_qq(text: str) -> str:
    """微信 agent 回答是 Markdown；QQ 纯文本消息会把符号原样显示，做个轻转换。"""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "", flags=re.S)
    t = re.sub(r"__(.+?)__", r"\1", t, flags=re.S)
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*]\s+", "· ", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "▎", t, flags=re.M)
    return t.strip()


def chunk_text(text: str, limit: int, max_chunks: int) -> list[str]:
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    paras, parts, cur = text.split("\n"), [], ""
    for p in paras:
        while len(p) > limit:              # 超长单行硬切
            parts.append(p[:limit])
            p = p[limit:]
        if len(cur) + len(p) + 1 > limit and cur:
            parts.append(cur)
            cur = p
        else:
            cur = f"{cur}\n{p}" if cur else p
    if cur:
        parts.append(cur)
    if len(parts) > max_chunks:
        parts = parts[:max_chunks]
        parts[-1] += "\n……（内容过长已截断，可在微信助手网页查看全文）"
    return parts


# ================================================================ 提示词


def build_prompt(openid: str, question: str, channel: str = "qq",
                 history: list | None = None) -> tuple[str, int | None, dict, dict]:
    import main as M                     # 延迟导入避免循环依赖
    conf_q = conf_of(M.load_conf())
    state = _load_state()
    ent = state.setdefault(openid, {"session_id": None, "turns": [], "paused": False})
    # 老状态补齐两个模式开关（取设置页默认值）
    ent.setdefault("explorer", bool(conf_q.get("default_explorer")))
    ent.setdefault("thinking", bool(conf_q.get("default_thinking")))
    stored = list(ent.get("turns") or [])
    now = int(time.time())
    if question:
        stored.append({"role": "user", "content": question, "ts": now})
    ent["turns"] = stored[-80:]
    _save_state(state)

    weixin = channel == "weixin"
    qq_lines: list[dict] = []
    if not weixin:
        for t in stored[-40:]:
            who = "用户" if t.get("role") == "user" else "助手"
            qq_lines.append({"ts": int(t.get("ts") or 0),
                             "line": f"[{fmt_ts(t.get('ts'))}] QQ·{who}: "
                                     f"{str(t.get('content') or '')[:600]}"})

    wx_items: list[dict] = []
    try:
        sessions = {s["username"]: s for s in M.api_sessions() if s.get("cnt")}

        def _emit(username: str, name: str, limit: int) -> None:
            rows = M.api_messages(username, limit=limit).get("messages") or []
            for m in rows:
                text = msg_text(m)
                if not text:
                    continue
                who = "我" if m.get("is_sender") else (m.get("sender_name") or "")
                wx_items.append({
                    "ts": int(m.get("ts") or 0),
                    "line": f"[{fmt_ts(m.get('ts'))}] 微信·{name} {who}: {text}",
                    "msg": m, "chat": username, "chat_name": name, "who": who,
                })

        locked = str(conf_q.get("wechat_chat") or "").strip()
        if locked:
            # 锁定会话：默认上下文只读这一个微信会话（条数更多）
            s = sessions.get(locked)
            _emit(locked, (s or {}).get("name") or locked, 30)
        else:
            # 全局模式：最近活跃的 5 个会话各取最近几条
            for u, s in sorted(sessions.values(),
                               key=lambda x: int(x.get("last_ts") or 0),
                               reverse=True)[:5]:
                _emit(u, s.get("name") or u, 12)
    except Exception as exc:  # noqa: BLE001
        log.warning("timeline failed: %s", exc)

    merged = sorted(qq_lines + wx_items, key=lambda x: x["ts"])
    picked, total = [], 0
    for item in reversed(merged):
        if total + len(item["line"]) > 18000:
            break
        picked.append(item)
        total += len(item["line"])
    picked.reverse()

    sources: dict[int, dict] = {}
    if conf_q.get("citations") and not weixin:
        wi = 0
        for item in picked:
            if "msg" in item:
                wi += 1
                item["line"] = f"[W{wi}] " + item["line"]
                sources[wi] = item

    timeline = "\n".join(x["line"] for x in picked) if picked else "（无近期上下文）"
    if weixin:
        hist = ""
        if history:
            rows = []
            for m in history[-10:]:
                if not isinstance(m, dict):
                    continue
                role = "用户" if m.get("role") == "user" else "助手"
                c = m.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c
                                 if isinstance(x, dict) and x.get("type") == "text")
                text = str(c or "").strip()
                if text:
                    rows.append(f"{role}：{text[:500]}")
            if rows:
                hist = "\n\n【对话记录】\n" + "\n".join(rows)
        prompt = (
            "你是通过微信（ClawBot）回答用户问题的助手，后端是本机微信知识库。\n"
            "回答会直接发到微信聊天窗口：用纯文本、口语化，默认 2–5 句、能短则短；"
            "除非用户明确要求，不要长篇大论、不要 Markdown 表格/代码块、不要输出 [W1] 或 [n] 引用编号。\n"
            "需要更多细节时可调用工具检索本机微信记录（聊天、图片 OCR、语音转写、文件、链接、朋友圈、"
            "合并转发都可查）；找不到就直说，不要编造。\n\n"
            f"【近期时间线】\n{timeline}"
            f"{hist}\n\n"
            f"【当前问题】\n{question or '（用户没有输入文字）'}"
        )
    else:
        if conf_q.get("citations"):
            cite_rule = ("时间线里 [W1]、[W2]… 是微信来源，引用时标注对应 [W编号]；"
                         "工具结果的引用用 [n] 标注。")
        else:
            cite_rule = "回答里不要输出 [W1]、[n] 之类的引用编号，直接给结论。"
        prompt = (
            "你是通过 QQ 回答用户问题的助手，后端是本机微信知识库。\n"
            "下面是本机微信账号的近期上下文（按时间正序，跨会话）和之前的 QQ 对话。\n"
            f"{cite_rule}\n"
            "需要更多细节时可调用工具检索本机微信记录（聊天、图片 OCR、语音转写、"
            "文件、链接、朋友圈、合并转发都可查）。找不到就直说，不要编造。\n\n"
            f"【近期时间线】\n{timeline}\n\n"
            f"【当前问题】\n{question or '（用户没有输入文字）'}"
        )
    sid = ent.get("session_id")
    if not sid:
        try:
            sid = M.api_ai_session_new(
                {"scope": conf_q.get("wechat_chat") or "assistant",
                 "name": ("微信·助手" if weixin else f"QQ·{openid[:8]}")})["id"]
            ent["session_id"] = sid
            _save_state(state)
        except Exception as exc:  # noqa: BLE001
            log.warning("create session failed: %s", exc)
    return prompt, sid, sources, ent


CHANNEL_HELP = """可用指令：
/帮助  查看这份说明
/快速问答 或 /快速  少轮检索，回答更快
/探索模式 或 /探索  多轮检索，适合统计和排查
/深度思考 或 /思考  使用推理模型，更准但更慢
/关闭思考  关闭推理模型
/新对话  清空上下文，重新开始
/暂停 /继续  暂停或恢复回答
/状态  查看当前设置

也可以直接发问题，例如「上周产品群讨论的上线时间」；我会检索本机微信记录后回答。"""


def channel_command(openid: str, text: str):
    """微信通道指令；命中返回回复文本，普通问题返回 None。"""
    raw = (text or "").strip()
    key = raw.lstrip("/").strip().lower()
    if not key:
        return None
    aliases = {
        "帮助": "help", "help": "help", "指令": "help", "菜单": "help",
        "快速问答": "fast", "快速": "fast", "fast": "fast",
        "探索模式": "explore", "探索": "explore", "explore": "explore",
        "深度思考": "think_on", "思考": "think_on",
        "关闭思考": "think_off", "关闭深度思考": "think_off",
        "新对话": "new", "新会话": "new", "new": "new", "清空": "new",
        "暂停": "pause", "pause": "pause",
        "继续": "resume", "resume": "resume",
        "状态": "status", "status": "status",
    }
    act = aliases.get(key)
    if not act:
        return None
    state = _load_state()
    ent = state.setdefault(openid, {"session_id": None, "turns": [], "paused": False})
    ent.setdefault("explorer", False)
    ent.setdefault("thinking", False)
    ent.setdefault("paused", False)
    if act == "help":
        return CHANNEL_HELP
    if act == "fast":
        ent["explorer"] = False
        _save_state(state)
        return "已切换为快速问答（少轮检索）。"
    if act == "explore":
        ent["explorer"] = True
        _save_state(state)
        return "已切换为探索模式（多轮检索）。"
    if act == "think_on":
        ent["thinking"] = True
        _save_state(state)
        return "已开启深度思考（推理模型，回答更准但更慢）。"
    if act == "think_off":
        ent["thinking"] = False
        _save_state(state)
        return "已关闭深度思考。"
    if act == "new":
        ent["session_id"] = None
        ent["turns"] = []
        _save_state(state)
        return "已开启新对话，之前的上下文已清空。"
    if act == "pause":
        ent["paused"] = True
        _save_state(state)
        return "已暂停回答。需要恢复时发送 /继续。"
    if act == "resume":
        ent["paused"] = False
        _save_state(state)
        return "已恢复回答，直接提问即可。"
    if act == "status":
        return ("当前状态：\n"
                f"提问方式：{'探索模式' if ent.get('explorer') else '快速问答'}\n"
                f"深度思考：{'开启' if ent.get('thinking') else '关闭'}\n"
                f"暂停：{'是' if ent.get('paused') else '否'}\n"
                f"上下文：{'新对话' if not ent.get('session_id') else '继续上次对话'}")
    return None


def ask_wechat(openid: str, question: str, channel: str = "qq",
               history: list | None = None) -> str:
    import main as M
    ent0 = _load_state().get(openid) or {}
    if ent0.get("paused"):
        return "已暂停回答。需要恢复时发送 /继续。"
    prompt, sid, sources, ent = build_prompt(openid, question, channel, history)
    payload = {"question": prompt, "chat": None, "history": [], "agent": True,
               "explorer": False if channel == "weixin" else bool(ent.get("explorer")),
               "thinking": False if channel == "weixin" else bool(ent.get("thinking"))}
    if sid:
        payload["session_id"] = sid
    data = M.api_ask(payload)
    answer = str(data.get("answer") or "").strip()
    conf_q = conf_of(M.load_conf())
    if channel != "weixin" and conf_q.get("citations") and sources:
        parts = []
        for n in sorted({int(x) for x in re.findall(r"\[W(\d{1,3})\]", answer)}):
            src = sources.get(n)
            if not src:
                continue
            m = src["msg"]
            parts.append(f"[W{n}] {src['chat_name']} {fmt_ts(src['ts'])} "
                         f"{src['who']}\n原文：{msg_text(m, 300)}")
        if parts:
            answer += "\n\n—— 引用 ——\n" + "\n\n".join(parts)
    else:
        answer = strip_citation_markers(answer)
    log.info("ask channel=%s openid=%s prompt=%d answer=%d",
             channel, openid[:8], len(prompt), len(answer))
    return answer


# ================================================================ 自定义菜单

# PUT /v2/menu：body = {"menu": {"items": [...]}}；type ∈ switch / send_message /
# link / menu（子菜单≤5 不嵌套）；name 顶层≤10 字符、子菜单≤14；send_message
# 点击后自动填入聊天输入框，用户发送后走本地指令处理
MENU_ITEMS = [
    {"type": "send_message", "name": "帮助", "send_message": "帮助"},
    {"type": "menu", "name": "提问方式", "sub_menu_items": [
        {"type": "send_message", "name": "快速问答", "send_message": "快速"},
        {"type": "send_message", "name": "探索模式", "send_message": "探索"},
    ]},
    {"type": "menu", "name": "思考深度", "sub_menu_items": [
        {"type": "send_message", "name": "开深度思考", "send_message": "思考"},
        {"type": "send_message", "name": "关深度思考", "send_message": "关闭思考"},
    ]},
    {"type": "send_message", "name": "新对话", "send_message": "新对话"},
]


def _fetch_token(conf_q: dict) -> str:
    r = requests.post(TOKEN_URL, timeout=15, json={
        "appId": conf_q.get("app_id"), "clientSecret": conf_q.get("app_secret")})
    r.raise_for_status()
    d = r.json()
    if not d.get("access_token"):
        raise RuntimeError(f"获取 access_token 失败: {d}")
    return d["access_token"]


def set_menu(conf_q: dict) -> dict:
    """一键把菜单推到 QQ（等价于控制台手工配置）。"""
    try:
        r = requests.put(f"{API_BASE}/v2/menu", timeout=15,
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
        r = requests.get(f"{API_BASE}/v2/menu", timeout=15,
                         headers={"Authorization": f"QQBot {_fetch_token(conf_q)}"})
        return {"ok": r.status_code < 400, "status": r.status_code,
                "body": r.json() if r.status_code < 400 else r.text[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 0, "body": str(exc)[:300]}


# ================================================================ 网关客户端


class QQBotRunner(threading.Thread):
    """一个机器人账户：WebSocket 收事件，REST 发回复。"""

    def __init__(self, q: dict):
        super().__init__(daemon=True, name="qqbot-gateway")
        self.q = q
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._token, self._token_exp = "", 0.0
        self.session_id, self.last_seq = None, None
        self._seq_lock = threading.Lock()
        self._msg_seq: dict[str, int] = {}
        self._work: "queue.Queue[tuple]" = queue.Queue()
        self._hb_interval = 30.0
        self._last_ack = time.time()
        self._ws: websocket.WebSocket | None = None

    # ---- 生命周期 ----
    def signature(self, q: dict) -> bool:
        return (self.q.get("app_id") == q.get("app_id")
                and self.q.get("app_secret") == q.get("app_secret"))

    def refresh_conf(self, q: dict) -> None:
        self.q = q

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:  # noqa: BLE001
            pass

    def is_alive(self) -> bool:  # noqa: A003
        return threading.Thread.is_alive(self)

    def run(self) -> None:
        STATUS.update({"running": True, "started_at": fmt_ts(time.time()),
                       "error": ""})
        worker = threading.Thread(target=self._worker_loop, daemon=True)
        worker.start()
        delay_idx = 0
        while not self._stop.is_set():
            try:
                self._session()
                delay_idx = 0
            except Exception as exc:  # noqa: BLE001
                log.warning("gateway session ended: %s", exc)
                STATUS["error"] = str(exc)[:200]
                STATUS["connected"] = False
            if self._stop.is_set():
                break
            delay = RECONNECT_DELAYS[min(delay_idx, len(RECONNECT_DELAYS) - 1)]
            delay_idx += 1
            self._stop.wait(delay)
        STATUS.update({"running": False, "connected": False})

    # ---- 连接 ----
    def _token_get(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._token_exp:
                return self._token
            r = requests.post(TOKEN_URL, timeout=15, json={
                "appId": self.q["app_id"],
                "clientSecret": self.q["app_secret"]})
            r.raise_for_status()
            d = r.json()
            if not d.get("access_token"):
                raise RuntimeError(f"获取 access_token 失败: {d}")
            self._token = d["access_token"]
            try:
                ttl = int(d.get("expires_in") or 7200)
            except (TypeError, ValueError):
                ttl = 7200
            self._token_exp = time.time() + ttl - 300
            log.info("access token refreshed, ttl=%ds", ttl)
            return self._token

    def _gateway_url(self) -> str:
        try:
            r = requests.get(f"{API_BASE}/gateway", timeout=15, headers={
                "Authorization": f"QQBot {self._token_get()}"})
            r.raise_for_status()
            url = r.json().get("url")
            if url:
                return url
        except Exception as exc:  # noqa: BLE001
            log.warning("get gateway failed (%s), use fallback", exc)
        return FALLBACK_GATEWAY

    def _session(self) -> None:
        ws = websocket.WebSocket()
        self._ws = ws
        ws.settimeout(5)
        url = self._gateway_url()
        log.info("connecting %s", url)
        ws.connect(url, timeout=15)
        hello = json.loads(ws.recv())
        if hello.get("op") != OP_HELLO:
            raise RuntimeError(f"expected HELLO, got op={hello.get('op')}")
        self._hb_interval = max(5.0, (hello.get("d") or {}).get(
            "heartbeat_interval", 30000) / 1000.0)
        token = self._token_get()
        if self.session_id and self.last_seq is not None:
            ws.send(json.dumps({"op": OP_RESUME, "d": {
                "token": f"QQBot {token}",
                "session_id": self.session_id, "seq": self.last_seq}}))
            log.info("resume sent session=%s seq=%s", self.session_id, self.last_seq)
        else:
            ws.send(json.dumps({"op": OP_IDENTIFY, "d": {
                "token": f"QQBot {token}", "intents": INTENTS, "shard": [0, 1]}}))
            log.info("identify sent intents=%d", INTENTS)

        hb_stop = threading.Event()
        hb = threading.Thread(target=self._heartbeat_loop,
                              args=(ws, hb_stop), daemon=True)
        hb.start()
        try:
            while not self._stop.is_set():
                try:
                    frame = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    if time.time() - self._last_ack > self._hb_interval * 3:
                        raise RuntimeError("heartbeat ACK timeout")
                    continue
                op = frame.get("op")
                if frame.get("s") is not None:
                    self.last_seq = frame["s"]
                if op == OP_DISPATCH:
                    self._on_dispatch(frame.get("t") or "", frame.get("d") or {})
                elif op == OP_HEARTBEAT_ACK:
                    self._last_ack = time.time()
                elif op == OP_HEARTBEAT:            # 服务端请求心跳
                    self._send(ws, {"op": OP_HEARTBEAT, "d": self.last_seq})
                elif op == OP_RECONNECT:
                    raise RuntimeError("gateway requested reconnect")
                elif op == OP_INVALID:
                    if not frame.get("d"):
                        self.session_id, self.last_seq = None, None
                    raise RuntimeError("invalid session")
        finally:
            hb_stop.set()
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _heartbeat_loop(self, ws: websocket.WebSocket, stop_evt: threading.Event):
        while not stop_evt.wait(self._hb_interval):
            try:
                self._send(ws, {"op": OP_HEARTBEAT, "d": self.last_seq})
            except Exception:  # noqa: BLE001
                return

    def _send(self, ws: websocket.WebSocket, payload: dict) -> None:
        with self._lock:
            ws.send(json.dumps(payload))

    # ---- 事件 ----
    def _on_dispatch(self, t: str, d: dict) -> None:
        if t == "READY":
            self.session_id = d.get("session_id")
            user = d.get("user") or {}
            STATUS.update({"connected": True, "username": user.get("username", ""),
                           "error": ""})
            log.info("READY bot=%s(%s)", user.get("username"), user.get("id"))
            return
        if t == "RESUMED":
            STATUS["connected"] = True
            log.info("RESUMED")
            return
        if t in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            STATUS["last_event"] = fmt_ts(time.time())
            self._work.put((t, d))

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                t, d = self._work.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._handle_message(t, d)
            except Exception as exc:  # noqa: BLE001
                log.exception("handle message failed")
                try:
                    self._reply(t, d, f"[处理失败：{str(exc)[:150]}]")
                except Exception:  # noqa: BLE001
                    pass

    # ---- 消息处理 ----
    def _handle_message(self, t: str, d: dict) -> None:
        conf_q = self.q
        is_group = t != "C2C_MESSAGE_CREATE"
        if is_group and not conf_q.get("group_reply"):
            return
        group_openid = d.get("group_openid") or ""
        author = d.get("author") or {}
        openid = (author.get("user_openid") if not is_group
                  else author.get("member_openid")) or ""
        if not openid:
            return
        allow = conf_q.get("allow_openids") or []
        if allow and openid not in allow:
            log.info("openid %s… not in allow list, ignored", openid[:8])
            return
        content = re.sub(r"<@!?\d+>", "", str(d.get("content") or "")).strip()
        msg_id = d.get("id") or ""
        if not content:
            self._reply(t, d, "收到图片/语音啦，不过我目前只看得懂文字～")
            return

        state = _load_state()
        ent = state.setdefault(openid, {"session_id": None, "turns": [],
                                        "paused": False})
        conf_q = conf_of(self.q) if isinstance(self.q, dict) else dict(DEFAULT_CONF)
        ent.setdefault("explorer", bool(conf_q.get("default_explorer")))
        ent.setdefault("thinking", bool(conf_q.get("default_thinking")))

        def _save():
            _save_state(state)

        def _mode_line() -> str:
            return (f"提问方式：{'探索模式' if ent.get('explorer') else '快速问答'} · "
                    f"深度思考：{'开' if ent.get('thinking') else '关'}")

        cmd = content.lstrip("/").strip()
        if cmd in ("帮助", "help", "菜单"):
            self._reply(t, d, "\n".join([
                "我是本机微信知识助手的 QQ 入口，可以直接问我微信里的事：",
                "· 「最近群里聊了什么」",
                "· 「XX 发过的那个链接讲了什么」",
                "· 「帮我找一下那个报价单」",
                "",
                "提问方式：/快速问答 少轮检索 · /探索模式 多轮检索",
                "思考深度：/深度思考 开推理模型 · /关闭思考 关推理模型",
                "其他：/新对话 清空上下文 · /暂停 不再回复 · /继续 恢复回复 · /状态",
                "",
                f"当前：{_mode_line()}"]))
            return
        if cmd in ("快速", "快速问答"):
            ent["explorer"] = False
            _save()
            self._reply(t, d, f"已切换为快速问答（少轮检索）。{_mode_line()}")
            return
        if cmd in ("探索", "探索模式"):
            ent["explorer"] = True
            _save()
            self._reply(t, d, f"已切换为探索模式（多轮检索，回答更慢更全）。{_mode_line()}")
            return
        if cmd in ("思考", "深度思考", "开启思考"):
            ent["thinking"] = True
            _save()
            self._reply(t, d, f"已开启深度思考（推理模型，更准更慢）。{_mode_line()}")
            return
        if cmd in ("关闭思考", "关闭深度思考"):
            ent["thinking"] = False
            _save()
            self._reply(t, d, f"已关闭深度思考。{_mode_line()}")
            return
        if cmd in ("新对话", "新会话", "reset"):
            import main as M
            ent["turns"] = []
            if ent.get("session_id"):
                try:
                    M.api_ai_session_new({"scope": "assistant", "name": "QQ"})
                except Exception:  # noqa: BLE001
                    pass
                ent["session_id"] = None
            _save()
            self._reply(t, d, "已开启新对话，上下文已清空。")
            return
        if cmd in ("暂停", "停止回复", "pause"):
            ent["paused"] = True
            _save()
            self._reply(t, d, "已暂停回复，发送 /继续 恢复。")
            return
        if cmd in ("继续", "恢复回复", "resume"):
            ent["paused"] = False
            _save()
            self._reply(t, d, "已恢复回复。")
            return
        if ent.get("paused"):
            return
        if cmd in ("状态", "status"):
            self._reply(t, d, f"运行中 · {_mode_line()} · "
                              f"最近消息 {STATUS.get('last_event') or '—'}")
            return

        answer = ask_wechat(openid, content)
        for piece in chunk_text(md_to_qq(answer),
                                int(conf_q.get("max_msg_chars") or MAX_MSG_CHARS),
                                int(conf_q.get("max_chunks") or MAX_CHUNKS)):
            self._reply(t, d, piece)

    # ---- 发送 ----
    def _next_seq(self, msg_id: str) -> int:
        with self._seq_lock:
            n = self._msg_seq.get(msg_id, 0) + 1
            self._msg_seq[msg_id] = n
            if len(self._msg_seq) > 500:
                for k in list(self._msg_seq.keys())[:250]:
                    self._msg_seq.pop(k, None)
            return n

    def _reply(self, t: str, d: dict, text: str) -> None:
        if not text:
            return
        is_group = t != "C2C_MESSAGE_CREATE"
        target = d.get("group_openid") if is_group else (
            (d.get("author") or {}).get("user_openid") or "")
        path = (f"/v2/groups/{target}/messages" if is_group
                else f"/v2/users/{target}/messages")
        msg_id = d.get("id") or ""
        body = {"content": text, "msg_type": 0, "msg_seq": self._next_seq(msg_id)}
        if msg_id:
            body["msg_id"] = msg_id
        r = requests.post(f"{API_BASE}{path}", timeout=20, json=body, headers={
            "Authorization": f"QQBot {self._token_get()}"})
        if r.status_code == 401:
            r = requests.post(f"{API_BASE}{path}", timeout=20, json=body, headers={
                "Authorization": f"QQBot {self._token_get(force=True)}"})
        if r.status_code >= 400:
            log.error("send failed %s %s", r.status_code, r.text[:200])
            r.raise_for_status()
        log.info("replied %s len=%d", path, len(text))
