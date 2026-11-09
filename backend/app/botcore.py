# -*- coding: utf-8 -*-
"""机器人通道共用内核（与具体平台无关）。

微信 ClawBot（wxagent）与 QQ 官方机器人（qqagent）共用同一套：
  * 指令表 / 别名 / 数字快捷键
  * 中文帮助、现状、状态文案
  * 对话状态读写（各自独立文件）
  * 上下文构建与问答（都落到 main.api_ask）
  * 引用清洗、Markdown 转纯文本、长消息分片

本模块**不 import qqbot / wxagent / qqagent**，由调用方注入配置，避免循环依赖。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
DATA = PROJECT / "data"

log = logging.getLogger("botcore")

MAX_TURNS = 80
TIMELINE_BUDGET = 18000


# ---------------------------------------------------------------- 指令表
# (动作, 别名列表, 说明, 菜单名)。别名里既有中文全名，也有单字/数字/英文缩写，
# 目的就是「输入简单」：/1、1、/快速、快速、fast 都能命中；不带斜杠也认。
COMMAND_SPECS: list[tuple[str, list[str], str, str]] = [
    ("help",      ["帮助", "help", "h", "指令", "菜单", "?", "？", "1"],
     "查看功能、现状与操作指引", "帮助"),
    ("fast",      ["快速", "快速问答", "fast", "f", "k", "2"],
     "快速问答：少轮检索，回答更快", "快速问答"),
    ("explore",   ["探索", "探索模式", "explore", "e", "3"],
     "探索模式：多轮检索，适合统计与排查", "探索模式"),
    ("think_on",  ["思考", "深度思考", "开思考", "开深度思考", "think", "t", "4"],
     "开启深度思考：切推理模型，更准但更慢", "开深度思考"),
    ("think_off", ["关思考", "关闭思考", "关闭深度思考", "不思考", "nothink", "5"],
     "关闭深度思考", "关深度思考"),
    ("new",       ["新对话", "新会话", "清空", "重置", "reset", "new", "n", "6"],
     "开启新对话，清空上下文", "新对话"),
    ("pause",     ["暂停", "停止回复", "pause", "p", "7"],
     "暂停回答（指令仍可回复）", "暂停"),
    ("resume",    ["继续", "恢复", "恢复回复", "resume", "r", "8"],
     "恢复回答", "继续"),
    ("status",    ["状态", "现状", "status", "s", "9"],
     "查看当前设置与运行状态", "状态"),
]

ALIASES: dict[str, str] = {}
for _act, _names, _desc, _menu in COMMAND_SPECS:
    for _n in _names:
        ALIASES[_n.lower()] = _act
ACTION_DESC = {a: d for a, _n, d, _m in COMMAND_SPECS}
ACTION_MENU = {a: m for a, _n, _d, m in COMMAND_SPECS}


def parse_command(text: str) -> str | None:
    """命中指令返回动作名，普通问题返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    key = raw.lstrip("/#").strip().lower()
    # 只认「整条消息就是指令」，避免把 "1+1 等于几" 之类的正常问题吞掉
    return ALIASES.get(key)


def command_list_lines() -> list[str]:
    """给 /帮助 用的短指令表，按动作顺序输出，含数字快捷键。"""
    short = {
        "help": "/1 /帮助", "fast": "/2 /快速", "explore": "/3 /探索",
        "think_on": "/4 /思考", "think_off": "/5 /关思考", "new": "/6 /新对话",
        "pause": "/7 /暂停", "resume": "/8 /继续", "status": "/9 /状态",
    }
    return [f"{short[a]:<12} {ACTION_DESC[a]}" for a, *_ in COMMAND_SPECS]


def kb_brief() -> str:
    """知识库现状：只读一条 MAX(id)，不对 3.9GB 库做 COUNT(*)。"""
    try:
        import main as M
        try:
            con = M.db()
            try:
                n = int(con.execute("SELECT COALESCE(MAX(id),0) FROM message").fetchone()[0] or 0)
            finally:
                con.close()
        except Exception:
            n = 0
        llm = (M.load_conf().get("llm") or {})
        model = str(llm.get("model") or "未设置")
        ready = "已配置" if llm.get("api_key") else "未配置 API Key"
        try:
            import cloud_conf as _cc
            label = "云端镜像" if _cc.IS_CLOUD else "本机记录"
        except Exception:
            label = "本机记录"
        return f"{label}约 {n:,} 条 · 模型 {model}（{ready}）"
    except Exception as exc:  # noqa: BLE001
        log.warning("kb_brief failed: %s", exc)
        return "本机知识库状态暂时读取失败（不影响提问）"


def mode_line(ent: dict) -> str:
    return (f"提问方式：{'探索模式' if ent.get('explorer') else '快速问答'}"
            f" · 深度思考：{'开' if ent.get('thinking') else '关'}"
            f" · 回答：{'已暂停' if ent.get('paused') else '正常'}")


def status_text(ent: dict, platform: str = "微信", extra: str = "") -> str:
    """`/状态` / `/现状`：把设置、上下文、数据源一次说清。"""
    turns = len(ent.get("turns") or [])
    ctx = (f"继续上次对话（已记 {turns} 轮）" if ent.get("session_id")
           else "新对话（尚未建立上下文）")
    lines = [
        f"【{platform}知识助手 · 当前状态】",
        "",
        mode_line(ent),
        f"上下文：{ctx}",
        f"数据源：{kb_brief()}",
    ]
    if extra:
        lines.append(extra)
    lines += ["", "发 /1 或 /帮助 看完整说明；直接发问题就能提问。"]
    return "\n".join(lines)


def help_text(ent: dict | None = None, platform: str = "微信",
              menu_hint: bool = False) -> str:
    """中文、可读、含「功能 / 现状 / 指引」三段；QQ 与微信共用同一份能力清单。"""
    what = " QQ 里" if platform == "QQ" else "微信里"
    lines = [
        f"【{platform}知识助手 · 使用说明】",
        "",
        "✦ 能做什么",
        f"在{what}直接问本机聊天记录里的事，我会检索后回答，查不到会直说：",
        "  · 上周产品群讨论的上线时间定了吗",
        "  · 那份报价单在哪 / 某篇链接讲了什么",
        "  · 谁最早提过 XX，按时间线整理一下",
        "  文字消息、图片文字、语音转写、文件、链接、动态都能查。",
        "",
        "✦ 现在的状态",
    ]
    if ent is not None:
        lines.append("  " + mode_line(ent))
        lines.append("  上下文：" + (f"继续上次对话（已记 {len(ent.get('turns') or [])} 轮）"
                                  if ent.get("session_id") else "新对话"))
    lines.append("  " + kb_brief())
    lines += [
        "",
        "✦ 怎么用（指令越短越好，斜杠可省略）",
    ]
    lines += ["  " + x for x in command_list_lines()]
    lines += [
        "",
        "✦ 小提示",
        "  直接发「2」「思考」「状态」也认；问题带上人名、群名或时间会更准。",
    ]
    if menu_hint:
        lines.append("  也可以点聊天窗口下方的菜单按钮，功能和上面一一对应。")
    else:
        lines.append("  /帮助 不消耗模型调用，随时可以发。")
    text = "\n".join(lines)
    if platform == "QQ":
        # QQ 界面里不该冒出「聊天记录 / 微信」这类说法
        import platform_text
        text = platform_text.localize(text, "qq")
    return text


# ---------------------------------------------------------------- 对话状态
class Store:
    """按平台隔离的状态文件；首次使用时从旧文件迁移，旧文件不删除。"""

    def __init__(self, path: Path, legacy: list[tuple[Path, object]] | None = None):
        self.path = path
        self.legacy = legacy or []
        self._migrated = False

    def load(self) -> dict:
        try:
            if self.path.exists():
                return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("load %s failed: %s", self.path.name, exc)
        return self._from_legacy()

    def _from_legacy(self) -> dict:
        out: dict = {}
        if self._migrated:
            return out
        for lp, flt in self.legacy:
            try:
                if not lp.exists():
                    continue
                raw = json.loads(lp.read_text(encoding="utf-8")) or {}
                if callable(flt):
                    raw = flt(raw)
                elif isinstance(flt, (list, tuple, set)):
                    raw = {k: v for k, v in raw.items() if k in flt}
                elif isinstance(flt, str) and flt == "drop_openclaw":
                    raw = {k: v for k, v in raw.items() if k != "openclaw"}
                out.update(raw)
            except Exception as exc:  # noqa: BLE001
                log.warning("migrate %s failed: %s", lp.name, exc)
        self._migrated = True
        if out:
            self.save(out)
        return out

    def save(self, state: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 原子写，避免网关线程与设置线程并发写坏 JSON
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as exc:  # noqa: BLE001
            log.warning("save %s failed: %s", self.path.name, exc)

    def entity(self, openid: str, conf: dict | None = None) -> tuple[dict, dict]:
        state = self.load()
        ent = state.setdefault(openid, {"session_id": None, "turns": [], "paused": False})
        ent.setdefault("session_id", None)
        ent.setdefault("turns", [])
        ent.setdefault("paused", False)
        ent.setdefault("explorer", bool((conf or {}).get("default_explorer")))
        ent.setdefault("thinking", bool((conf or {}).get("default_thinking")))
        return state, ent


# ---------------------------------------------------------------- 文本工具
def fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts or 0)))
    except (ValueError, OSError, TypeError):
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


def md_to_plain(text: str) -> str:
    """聊天窗口里 Markdown 符号会原样显示，做一次轻转换。"""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "", flags=re.S)
    t = re.sub(r"__(.+?)__", r"\1", t, flags=re.S)
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*]\s+", "· ", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "▎", t, flags=re.M)
    return t.strip()


def chunk_text(text: str, limit: int, max_chunks: int, tail_hint: str) -> list[str]:
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    paras, parts, cur = text.split("\n"), [], ""
    for p in paras:
        while len(p) > limit:
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
        parts[-1] += "\n……" + tail_hint
    return parts


# ---------------------------------------------------------------- 上下文与问答
def build_prompt(openid: str, question: str, *, store: Store, conf: dict,
                 channel: str = "qq", history: list | None = None):
    """组装「本机近期记录 + 本通道对话轮次」时间线；返回 (prompt, sid, sources, ent)。"""
    import main as M                     # 延迟导入避免循环依赖
    state, ent = store.entity(openid, conf)
    now = int(time.time())
    if question:
        turns = list(ent.get("turns") or [])
        turns.append({"role": "user", "content": question, "ts": now})
        ent["turns"] = turns[-MAX_TURNS:]
        store.save(state)

    weixin = channel == "weixin"
    chat_lines: list[dict] = []
    if not weixin:
        for t in (ent.get("turns") or [])[-40:]:
            who = "用户" if t.get("role") == "user" else "助手"
            chat_lines.append({"ts": int(t.get("ts") or 0),
                               "line": f"[{fmt_ts(t.get('ts'))}] QQ·{who}: "
                                       f"{str(t.get('content') or '')[:600]}"})

    wx_items: list[dict] = []
    if not weixin:
        # QQ Agent 只看 QQ 本地记录，不把微信时间线混进来
        try:
            import qq_local
            con = M.db()
            try:
                rows = qq_local.recent(con, 80)
            finally:
                con.close()
            for r in reversed(rows):
                who = "我" if r.get("is_self") else (r.get("who") or "")
                chat_lines.append({
                    "ts": int(r.get("ts") or 0),
                    "line": f"[{fmt_ts(r.get('ts'))}] QQ·{r.get('conv_name')} {who}: "
                            f"{(r.get('text') or '')[:600]}",
                })
        except Exception as exc:  # noqa: BLE001
            log.warning("qq timeline failed: %s", exc)
    else:
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
                        "line": f"[{fmt_ts(m.get('ts'))}] 记录·{name} {who}: {text}",
                        "msg": m, "chat": username, "chat_name": name, "who": who,
                    })

            locked = str(conf.get("wechat_chat") or "").strip()
            if locked:
                s = sessions.get(locked)
                _emit(locked, (s or {}).get("name") or locked, 30)
            else:
                for u, s in sorted(sessions.values(),
                                   key=lambda x: int(x.get("last_ts") or 0),
                                   reverse=True)[:5]:
                    _emit(u, s.get("name") or u, 12)
        except Exception as exc:  # noqa: BLE001
            log.warning("timeline failed: %s", exc)

    merged = sorted(chat_lines + wx_items, key=lambda x: x["ts"])
    picked, total = [], 0
    for item in reversed(merged):
        if total + len(item["line"]) > TIMELINE_BUDGET:
            break
        picked.append(item)
        total += len(item["line"])
    picked.reverse()

    sources: dict[int, dict] = {}
    if conf.get("citations") and not weixin:
        wi = 0
        for item in picked:
            if "msg" in item:
                wi += 1
                item["line"] = f"[W{wi}] " + item["line"]
                sources[wi] = item

    timeline = "\n".join(x["line"] for x in picked) if picked else "（无近期上下文）"
    app = "微信（ClawBot）" if weixin else "QQ"
    platform_word = "QQ空间" if not weixin else "朋友圈"
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
            f"你是通过{app}回答用户问题的助手，后端是本机聊天知识库。\n"
            "回答会直接发到聊天窗口：用纯文本、口语化，默认 2–5 句、能短则短；"
            "除非用户明确要求，不要长篇大论、不要 Markdown 表格/代码块、不要输出 [W1] 或 [n] 引用编号。\n"
            "需要更多细节时可调用工具检索本机记录（聊天、图片 OCR、语音转写、文件、链接、"
            f"{platform_word}、合并转发都可查）；找不到就直说，不要编造。\n\n"
            f"【近期时间线】\n{timeline}"
            f"{hist}\n\n"
            f"【当前问题】\n{question or '（用户没有输入文字）'}"
        )
    else:
        if conf.get("citations"):
            cite_rule = ("时间线里 [W1]、[W2]… 是本机记录来源，引用时标注对应 [W编号]；"
                         "工具结果的引用用 [n] 标注。")
        else:
            cite_rule = "回答里不要输出 [W1]、[n] 之类的引用编号，直接给结论。"
        prompt = (
            f"你是通过{app}回答用户问题的助手，后端是本机聊天知识库。\n"
            "下面是本机账号的近期上下文（按时间正序，跨会话）和之前的对话。\n"
            f"{cite_rule}\n"
            "需要更多细节时可调用工具检索本机记录（聊天、图片 OCR、语音转写、"
            f"文件、链接、{platform_word}、合并转发都可查）。找不到就直说，不要编造。\n"
            + ("QQ 群/好友聊天记录请用 search_qq（本机 QQ 数据库），"
               "不要用微信工具的结果冒充 QQ 记录。\n" if not weixin else "")
            + f"\n【近期时间线】\n{timeline}\n\n"
            f"【当前问题】\n{question or '（用户没有输入文字）'}"
        )
    sid = ent.get("session_id")
    if sid:
        try:
            M.claim_session_platform(sid, "wechat" if weixin else "qq")
        except Exception as exc:  # noqa: BLE001
            log.debug("claim session platform skipped: %s", exc)
    if not sid:
        try:
            sid = M.api_ai_session_new(
                {"scope": conf.get("wechat_chat") or "assistant",
                 "name": ("微信·助手" if weixin else f"QQ·{openid[:8]}"),
                 "platform": "wechat" if weixin else "qq"})["id"]
            ent["session_id"] = sid
            store.save(state)
        except Exception as exc:  # noqa: BLE001
            log.warning("create session failed: %s", exc)
    return prompt, sid, sources, ent


def ask(openid: str, question: str, *, store: Store, conf: dict,
        channel: str = "qq", history: list | None = None) -> str:
    """两通道唯一问答入口，落点仍是 main.api_ask（与网页端同一套 Agent）。"""
    import main as M
    _state, ent0 = store.entity(openid, conf)
    if ent0.get("paused"):
        return "已暂停回答，发 /继续（或 8）恢复。"
    prompt, sid, sources, ent = build_prompt(
        openid, question, store=store, conf=conf, channel=channel, history=history)
    payload = {"question": prompt, "chat": None, "history": [], "agent": True,
               "platform": "wechat" if channel == "weixin" else "qq",
               "explorer": False if channel == "weixin" else bool(ent.get("explorer")),
               "thinking": False if channel == "weixin" else bool(ent.get("thinking"))}
    if sid:
        payload["session_id"] = sid
    data = M.api_ask(payload)
    answer = str(data.get("answer") or "").strip()
    if channel != "weixin" and conf.get("citations") and sources:
        parts = []
        for n in sorted({int(x) for x in re.findall(r"\[W(\d{1,3})\]", answer)}):
            src = sources.get(n)
            if not src:
                continue
            parts.append(f"[W{n}] {src['chat_name']} {fmt_ts(src['ts'])} "
                         f"{src['who']}\n原文：{msg_text(src['msg'], 300)}")
        if parts:
            answer += "\n\n—— 引用 ——\n" + "\n\n".join(parts)
    else:
        answer = strip_citation_markers(answer)
    # 记一轮回答，供下次上下文与 /状态 使用（只存摘要，别把整篇塞回状态文件）
    state, ent2 = store.entity(openid, conf)
    turns = list(ent2.get("turns") or [])
    turns.append({"role": "assistant", "content": answer[:600],
                  "ts": int(time.time())})
    ent2["turns"] = turns[-MAX_TURNS:]
    store.save(state)
    log.info("ask channel=%s openid=%s prompt=%d answer=%d",
             channel, openid[:8], len(prompt), len(answer))
    return answer


def execute_command(action: str, *, openid: str, store: Store, conf: dict,
                    platform: str = "微信", menu_hint: bool = False,
                    extra_status: str = "") -> str:
    """统一执行指令并返回中文回复；两边文案一致。"""
    state, ent = store.entity(openid, conf)
    if action == "help":
        return help_text(ent, platform=platform, menu_hint=menu_hint)
    if action == "status":
        return status_text(ent, platform=platform, extra=extra_status)
    if action == "fast":
        ent["explorer"] = False
        reply = "已切换为快速问答（少轮检索，回答更快）。"
    elif action == "explore":
        ent["explorer"] = True
        reply = "已切换为探索模式（多轮检索，适合统计与排查，会慢一些）。"
    elif action == "think_on":
        ent["thinking"] = True
        reply = "已开启深度思考（推理模型，更准但更慢）。"
    elif action == "think_off":
        ent["thinking"] = False
        reply = "已关闭深度思考，恢复普通模型。"
    elif action == "new":
        ent["session_id"] = None
        ent["turns"] = []
        reply = "已开启新对话，之前的上下文已清空。"
    elif action == "pause":
        ent["paused"] = True
        reply = "已暂停回答。发 /继续（或 8）恢复，指令仍会回复。"
    elif action == "resume":
        ent["paused"] = False
        reply = "已恢复回答，直接发问题就行。"
    else:
        return ""
    store.save(state)
    return f"{reply}\n{mode_line(ent)}"


DATA_DIR = DATA
