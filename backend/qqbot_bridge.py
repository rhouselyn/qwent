# -*- coding: utf-8 -*-
"""QQ Bot <-> 本机微信知识库桥接。

服务器上的 OpenClaw/qqbot 只负责收 QQ 消息、把最终文本发回 QQ；
本服务负责真正的大脑侧工作：

  * 接收 OpenAI 兼容的 /v1/chat/completions 请求
  * 把 QQ 对话轮次和本机微信近期上下文按时间拼成提示词
  * 展开合并转发（parsed.record）、链接、文件、图片 OCR、语音转写
  * 调 127.0.0.1:8721/api/ask，让本机微信 agent 回答
  * 把回答里 [n] 对应的微信原文附在下方，并给出本机深链

启动：
    python backend/qqbot_bridge.py

配置：backend/qqbot_bridge.json（首次启动自动生成，含随机 token）
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "qqbot_bridge.json"
STATE_PATH = BASE_DIR / "qqbot_bridge_state.json"
LOG_PATH = BASE_DIR / "qqbot_bridge.log"

DEFAULT_CONFIG = {
    "port": 18821,
    "token": "",
    "wechat_base": "http://127.0.0.1:8721",
    # None = 全局模式（最近多个会话）；填 wxid / xxx@chatroom 则只取该会话
    "wechat_chat": None,
    "context_chats": 5,
    "context_messages": 12,
    "max_context_chars": 18000,
    "message_chars": 400,
    "record_items": 20,
    "max_citations": 5,
    "citation_chars": 400,
    "total_chars": 4800,
    # QQ 渠道默认不要引用块/编号；需要时改成 true
    "citations": False,
    "ask_timeout": 300,
    "agent": True,
    "deep_link_base": "http://127.0.0.1:8721",
    "session_name": "QQ Bot",
}

KIND_LABEL = {
    "image": "图片",
    "voice": "语音",
    "video": "视频",
    "file": "文件",
    "link": "链接",
    "record": "合并转发",
    "card": "名片",
    "location": "位置",
    "quote": "引用",
    "emoji": "表情",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("qqbot-bridge")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        cfg = dict(DEFAULT_CONFIG)
        cfg["token"] = uuid.uuid4().hex + uuid.uuid4().hex[:8]
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("created %s token=%s", CONFIG_PATH, cfg["token"])
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


CFG = load_config()
app = FastAPI(title="QQBot WeChat Bridge", version="1.0")


# ---------------------------------------------------------------- 微信 API


def wget(path: str, **params):
    r = requests.get(CFG["wechat_base"] + path, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def wpost(path: str, payload: dict, timeout: float | None = None):
    r = requests.post(
        CFG["wechat_base"] + path,
        json=payload,
        timeout=timeout or CFG["ask_timeout"],
    )
    r.raise_for_status()
    return r.json()


def fmt_ts(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts or 0)))
    except (ValueError, OSError):
        return ""


# ---------------------------------------------------------------- 消息渲染


def render_record(parsed: dict, limit: int) -> str:
    """把合并转发 parsed.record 展开成可读文本。"""
    rec = parsed.get("record") or {}
    items = rec.get("items") or []
    title = parsed.get("title") or "聊天记录"
    lines = [f"[合并转发《{title}》共 {rec.get('count') or len(items)} 条]"]
    info = re.sub(r"\s+", " ", str(rec.get("info") or "")).strip()
    if info:
        lines.append(info[:400])
    for it in items[:limit]:
        who = str(it.get("name") or "")
        t = str(it.get("time") or "")
        body = it.get("text") or it.get("title") or it.get("url") or ""
        body = re.sub(r"\s+", " ", str(body)).strip()
        lines.append((f"{t} {who}: {body}" if (t or who) else body)[:300])
    if len(items) > limit:
        lines.append(f"……其余 {len(items) - limit} 条未展开")
    return "\n".join(x for x in lines if x)


def msg_text(msg: dict, max_chars: int | None = None) -> str:
    """把一条 /api/messages 消息渲染成文本。"""
    parsed = msg.get("parsed") or {}
    if not isinstance(parsed, dict):
        parsed = {}
    if parsed.get("record"):
        return render_record(parsed, int(CFG.get("record_items") or 20))
    text = parsed.get("text") or parsed.get("system") or ""
    kind = parsed.get("kind") or ""
    if not text:
        if kind == "file":
            text = f"[文件] {parsed.get('title') or ''}"
        elif kind == "link":
            text = f"[链接] {parsed.get('title') or ''} {parsed.get('url') or ''}"
        elif kind == "quote":
            ref = parsed.get("refer") or {}
            text = f"[引用 {ref.get('who') or ''}] {parsed.get('title') or ''}"
        elif kind:
            text = f"[{KIND_LABEL.get(kind, kind)}]"
    if parsed.get("ocr_text"):
        text = f"{text}（图片文字: {parsed['ocr_text'][:300]}）"
    if parsed.get("asr_text"):
        text = f"{text}（语音转写: {parsed['asr_text'][:300]}）"
    if parsed.get("url") and parsed["url"] not in text:
        text = f"{text} {parsed['url']}"
    if parsed.get("des") and parsed["des"] not in text:
        text = f"{text} {parsed['des'][:200]}"
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[: max_chars or int(CFG.get("message_chars") or 400)]


# ---------------------------------------------------------------- 上下文


def chat_name(chat: str) -> str:
    try:
        sessions = wget("/api/sessions")
        for s in sessions:
            if s.get("username") == chat:
                return s.get("name") or chat
    except Exception:
        pass
    return chat


def fetch_context() -> list[tuple[str, str, list[dict]]]:
    """返回 [(chat, chat_name, messages), ...]。"""
    chat = CFG.get("wechat_chat")
    if chat:
        rows = wget(
            "/api/messages",
            chat=chat,
            limit=int(CFG.get("context_messages") or 12) * 2,
        ).get("messages") or []
        return [(chat, chat_name(chat), rows)]

    sessions = wget("/api/sessions")
    if not isinstance(sessions, list):
        return []
    sessions = [s for s in sessions if s.get("cnt")]
    sessions.sort(key=lambda s: int(s.get("last_ts") or 0), reverse=True)
    out = []
    for s in sessions[: int(CFG.get("context_chats") or 5)]:
        try:
            rows = wget(
                "/api/messages",
                chat=s["username"],
                limit=int(CFG.get("context_messages") or 12),
            ).get("messages") or []
            out.append((s["username"], s.get("name") or s["username"], rows))
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch chat %s failed: %s", s.get("username"), exc)
    return out


def wechat_timeline() -> list[dict]:
    """返回可排序的微信来源项；msg 保留原始消息，引用块直接复用。"""
    out: list[dict] = []
    for chat, name, rows in fetch_context():
        for m in rows:
            ts = int(m.get("ts") or 0)
            who = "我" if m.get("is_sender") else (m.get("sender_name") or "")
            text = msg_text(m)
            if not text:
                continue
            out.append(
                {
                    "ts": ts,
                    "line": f"[{fmt_ts(ts)}] 微信·{name} {who}: {text}",
                    "msg": m,
                    "chat": chat,
                    "chat_name": name,
                    "who": who,
                }
            )
    out.sort(key=lambda x: x["ts"])
    return out


# ---------------------------------------------------------------- QQ 轮次状态


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("save state failed: %s", exc)


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(str(p.get("text") or ""))
                elif p.get("type") == "image_url":
                    parts.append("[图片]")
                elif p.get("type") in ("input_audio", "audio"):
                    parts.append("[语音]")
        return "\n".join(x for x in parts if x)
    return ""


def extract_turns(messages: list[dict]) -> list[dict]:
    turns = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = content_text(m.get("content")).strip()
        if text:
            turns.append({"role": role, "content": text})
    return turns


def state_key(messages: list[dict]) -> str:
    system = ""
    first_user = ""
    for m in messages:
        if m.get("role") == "system" and not system:
            system = content_text(m.get("content"))[:300]
        if m.get("role") == "user" and not first_user:
            first_user = content_text(m.get("content"))[:300]
    raw = f"{system}|{first_user}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def sync_turns(state: dict, key: str, turns: list[dict]) -> dict:
    ent = state.setdefault(key, {"turns": [], "session_id": None})
    stored = ent.get("turns") or []
    recent = {(t.get("role"), t.get("content")) for t in stored[-60:]}
    now = int(time.time())
    for i, t in enumerate(turns):
        sig = (t["role"], t["content"])
        if sig in recent:
            continue
        # 首次请求会把历史轮次一起补进来，用递减时间近似真实先后
        ts = now - (len(turns) - 1 - i) * 30
        stored.append({"role": t["role"], "content": t["content"], "ts": ts})
        recent.add(sig)
    ent["turns"] = stored[-200:]
    save_state(state)
    return ent


def ensure_session(state: dict, key: str) -> int | None:
    ent = state.setdefault(key, {"turns": [], "session_id": None})
    sid = ent.get("session_id")
    if sid:
        return sid
    try:
        scope = CFG.get("wechat_chat") or "assistant"
        r = wpost(
            "/api/ai/sessions",
            {"scope": scope, "name": CFG.get("session_name") or "QQ Bot"},
        )
        sid = r.get("id")
        ent["session_id"] = sid
        save_state(state)
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure session failed: %s", exc)
        sid = None
    return sid


# ---------------------------------------------------------------- 提示词


def build_prompt(messages: list[dict]) -> tuple[str, int | None, dict[int, dict]]:
    key = state_key(messages)
    state = load_state()
    turns = extract_turns(messages)
    ent = sync_turns(state, key, turns)
    sid = ensure_session(state, key)

    current = ""
    if turns and turns[-1]["role"] == "user":
        current = turns[-1]["content"]

    stored = list(ent.get("turns") or [])
    # 当前问题单独放，时间线里删掉它，避免重复
    if current:
        for i in range(len(stored) - 1, -1, -1):
            if stored[i].get("role") == "user" and stored[i].get("content") == current:
                del stored[i]
                break

    qq_lines: list[dict] = []
    for t in stored[-40:]:
        who = "用户" if t.get("role") == "user" else "助手"
        ts = int(t.get("ts") or 0)
        qq_lines.append(
            {
                "ts": ts,
                "line": f"[{fmt_ts(ts)}] QQ·{who}: {str(t.get('content') or '')[:600]}",
            }
        )

    merged = sorted(qq_lines + wechat_timeline(), key=lambda x: x["ts"])
    max_chars = int(CFG.get("max_context_chars") or 18000)
    picked: list[dict] = []
    total = 0
    for item in reversed(merged):
        line = item["line"]
        if total + len(line) > max_chars:
            break
        picked.append(item)
        total += len(line)
    picked.reverse()

    # 需要引用时给微信来源编 W1/W2…，回答里引用后由本服务直接映射回原文
    sources: dict[int, dict] = {}
    if CFG.get("citations"):
        wi = 0
        for item in picked:
            if "msg" in item:
                wi += 1
                item["line"] = f"[W{wi}] " + item["line"]
                sources[wi] = item

    timeline = "\n".join(x["line"] for x in picked) if picked else "（无近期上下文）"
    if CFG.get("citations"):
        cite_instruction = (
            "时间线里以 [W1]、[W2]… 开头的是微信来源；回答中引用时间线内容时，"
            "请在相应句子后标注对应的 [W编号]。"
            "如果需要更多细节，可以调用工具检索本机微信记录（聊天、图片 OCR、语音转写、"
            "文件、链接、合并转发都可查）；工具结果里的引用仍用 [n] 标注。"
        )
    else:
        cite_instruction = (
            "回答里不要标注 [W1]、[n] 之类的引用编号，也不要输出引用来源清单；"
            "直接给出结论。需要更多细节时可以调用工具检索本机微信记录。"
        )
    prompt = (
        "你是通过 QQ 回答用户问题的助手，后端是本机微信知识库。\n"
        "下面是本机微信账号的近期上下文（按时间正序，跨会话）和之前的 QQ 对话。\n"
        f"{cite_instruction}\n"
        "找不到就直说，不要编造。\n\n"
        f"【近期时间线】\n{timeline}\n\n"
        f"【当前问题】\n{current or '（用户没有输入文字）'}"
    )
    return prompt, sid, sources


# ---------------------------------------------------------------- 引用块


def deep_link(chat: str, msg_id: Any) -> str:
    if not chat or not msg_id:
        return ""
    return (
        f"{CFG.get('deep_link_base') or 'http://127.0.0.1:8721'}"
        f"/?chat={quote(str(chat), safe='')}&msg={int(msg_id)}"
    )


def message_citation(ref: dict, budget: int) -> str:
    mid = ref.get("id")
    chat = ref.get("chat") or ""
    name = ref.get("chat_name") or chat
    who = ref.get("sender_name") or ""
    ts = ref.get("ts") or 0
    text = ref.get("snippet") or ""
    if mid:
        try:
            data = wget("/api/messages/context", id=int(mid), around=0)
            rows = data.get("messages") or []
            m = next((x for x in rows if x.get("id") == mid), rows[0] if rows else None)
            if m:
                name = m.get("chat_name") or name
                who = m.get("sender_name") or who
                ts = m.get("ts") or ts
                text = msg_text(m, max_chars=max(120, budget))
        except Exception as exc:  # noqa: BLE001
            log.warning("citation message %s failed: %s", mid, exc)
    link = deep_link(chat, mid)
    body = f"{name} {fmt_ts(ts)} {who}\n原文：{text[:budget]}"
    if link:
        body += f"\n跳转：{link}"
    return body


def link_citation(ref: dict, budget: int) -> str:
    url = ref.get("url") or ""
    title = ref.get("chat_name") or ref.get("title") or "网页文章"
    text = ref.get("snippet") or ""
    if url:
        try:
            d = wget("/api/link/text", url=url)
            title = d.get("title") or title
            text = (d.get("text") or text)[:budget]
        except Exception as exc:  # noqa: BLE001
            log.warning("citation link %s failed: %s", url, exc)
    return f"《{title}》\n{url}\n摘要：{text[:budget]}"


def file_citation(ref: dict, budget: int) -> str:
    params = {
        "chat": ref.get("chat") or "",
        "local_id": int(ref.get("local_id") or 0),
        "ts": int(ref.get("ts") or 0),
        "name": ref.get("title") or ref.get("name") or "",
    }
    name = params["name"]
    size = int(ref.get("size") or 0)
    msg_id = None
    try:
        meta = wget("/api/file/meta", **params)
        name = meta.get("name") or name
        size = int(meta.get("size") or size)
        msg_id = meta.get("msg_id")
    except Exception as exc:  # noqa: BLE001
        log.warning("citation file meta failed: %s", exc)
    text = ""
    try:
        d = wget("/api/file/text", **params, limit=max(200, budget))
        text = (d.get("text") or "")[:budget]
    except Exception:
        pass
    link = deep_link(params["chat"], msg_id)
    body = f"文件《{name}》{size / 1024:.0f} KB {fmt_ts(params['ts'])}"
    if text:
        body += f"\n摘要：{text}"
    if link:
        body += f"\n跳转：{link}"
    return body


def moment_citation(ref: dict, budget: int) -> str:
    tid = ref.get("tid")
    if not tid:
        return ""
    try:
        d = wget(f"/api/sns/post/{tid}")
        who = d.get("author_name") or d.get("who") or "朋友圈"
        text = (d.get("text") or "")[:budget]
        return f"朋友圈 {who} {fmt_ts(d.get('ts'))}\n{text}"
    except Exception as exc:  # noqa: BLE001
        log.warning("citation moment %s failed: %s", tid, exc)
        return f"朋友圈 {ref.get('chat_name') or ''}\n{ref.get('snippet') or ''}"


def timeline_citation(src: dict, budget: int) -> str:
    """把时间线里已经抓好的来源直接渲染成引用块，不再二次请求。"""
    m = src.get("msg") or {}
    text = msg_text(m, max_chars=max(120, budget))
    link = deep_link(src.get("chat") or "", m.get("id"))
    body = (
        f"{src.get('chat_name')} {fmt_ts(src.get('ts'))} "
        f"{src.get('who') or ''}\n原文：{text[:budget]}"
    )
    if link:
        body += f"\n跳转：{link}"
    return body


def build_citations(
    answer: str,
    sources: dict[int, dict],
    refs: list[dict],
    budget: int,
) -> str:
    """先处理回答里的 [Wn]，再补工具检索的 [n]，统一放在回答下方。"""
    if budget <= 0:
        return ""
    parts: list[str] = []
    used = 0
    seen_ids: set[int] = set()
    seen_urls: set[str] = set()

    for n in sorted({int(x) for x in re.findall(r"\[W(\d{1,3})\]", answer or "")}):
        src = sources.get(n)
        if not src:
            continue
        per = min(int(CFG.get("citation_chars") or 400), max(120, budget - used))
        if per <= 0:
            break
        part = f"[W{n}] {timeline_citation(src, per)}"
        if used + len(part) > budget:
            break
        parts.append(part)
        used += len(part)
        mid = (src.get("msg") or {}).get("id")
        if mid:
            seen_ids.add(int(mid))

    for ref in refs[: int(CFG.get("max_citations") or 5)]:
        if used >= budget:
            break
        mid = ref.get("id")
        url = ref.get("url") or ""
        if (mid and int(mid) in seen_ids) or (url and url in seen_urls):
            continue
        kind = ref.get("kind")
        per = min(int(CFG.get("citation_chars") or 400), max(120, budget - used))
        if per <= 0:
            break
        if kind == "file":
            body = file_citation(ref, per)
        elif kind == "link":
            body = link_citation(ref, per)
        elif kind == "moment":
            body = moment_citation(ref, per)
        else:
            body = message_citation(ref, per)
            if mid:
                seen_ids.add(int(mid))
        if url:
            seen_urls.add(url)
        part = f"[{ref.get('n')}] {body}"
        if used + len(part) > budget:
            break
        parts.append(part)
        used += len(part)

    return "\n\n—— 引用 ——\n" + "\n\n".join(parts) if parts else ""


def citation_block(refs: list[dict], budget: int) -> str:
    if not refs or budget <= 0:
        return ""
    parts: list[str] = []
    used = 0
    for ref in refs[: int(CFG.get("max_citations") or 5)]:
        kind = ref.get("kind")
        per = min(int(CFG.get("citation_chars") or 400), max(120, budget - used))
        if per <= 0:
            break
        if kind == "file":
            body = file_citation(ref, per)
        elif kind == "link":
            body = link_citation(ref, per)
        elif kind == "moment":
            body = moment_citation(ref, per)
        else:
            body = message_citation(ref, per)
        if not body:
            continue
        part = f"[{ref.get('n')}] {body}"
        if used + len(part) > budget:
            break
        parts.append(part)
        used += len(part)
    return "\n\n—— 引用 ——\n" + "\n\n".join(parts) if parts else ""


# ---------------------------------------------------------------- 主处理


_CITE_W_RE = re.compile(r"\[W\d{1,3}\]", re.I)
# 只清独立的 [12]/[3]；不动 ![1] 图片和 [文字](url) Markdown 链接
_CITE_N_RE = re.compile(r"(?<!!)\[(\d{1,3})\](?!\()")


def strip_citation_markers(text: str) -> str:
    """QQ 渠道不需要引用编号：去掉 [W3]、[3] 这类标记并清理残留空格。"""
    out = _CITE_W_RE.sub("", text or "")
    out = _CITE_N_RE.sub("", out)
    out = re.sub(r"[ \t]+([，。！？；：,.!?;:])", r"\1", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"（\s*）|\(\s*\)", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def answer_question(messages: list[dict]) -> tuple[str, int]:
    started = time.time()
    prompt, sid, sources = build_prompt(messages)
    payload = {
        "question": prompt,
        "chat": CFG.get("wechat_chat") or None,
        "history": [],
        "agent": bool(CFG.get("agent", True)),
    }
    if sid:
        payload["session_id"] = sid
    data = wpost("/api/ask", payload)
    answer = str(data.get("answer") or "").strip()
    refs = data.get("refs") or []
    if CFG.get("citations"):
        budget = max(0, int(CFG.get("total_chars") or 4800) - len(answer))
        final = answer + build_citations(answer, sources, refs, budget)
    else:
        final = strip_citation_markers(answer)
    log.info(
        "ask done elapsed=%.1fs prompt=%d answer=%d refs=%d sources=%d final=%d",
        time.time() - started,
        len(prompt),
        len(answer),
        len(refs),
        len(sources),
        len(final),
    )
    return final, int(time.time())


# ---------------------------------------------------------------- HTTP


def check_token(authorization: str | None) -> None:
    token = str(CFG.get("token") or "")
    if token and authorization != f"Bearer {token}":
        raise HTTPException(401, "invalid token")


@app.get("/health")
def health():
    try:
        return {"ok": True, "wechat": wget("/api/status")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.get("/v1/models")
def models(authorization: str | None = Header(None)):
    check_token(authorization)
    return {
        "object": "list",
        "data": [
            {
                "id": "wechat-agent",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "local-wechat",
            }
        ],
    }


@app.post("/v1/chat/completions")
def chat_completions(
    payload: dict,
    authorization: str | None = Header(None),
):
    check_token(authorization)
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        raise HTTPException(400, "messages must be a list")
    model = payload.get("model") or "wechat-agent"
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    log.info(
        "request model=%s stream=%s keys=%s user=%r",
        model,
        bool(payload.get("stream")),
        sorted(payload.keys()),
        payload.get("user"),
    )
    try:
        final, created = answer_question(messages)
    except Exception as exc:  # noqa: BLE001
        log.exception("ask failed")
        final = f"[本机微信桥接失败：{str(exc)[:300]}]"
        created = int(time.time())

    if not payload.get("stream"):
        return JSONResponse(
            {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": final},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )

    def gen():
        first = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": final},
                    "finish_reason": None,
                }
            ],
        }
        last = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield "data: " + json.dumps(first, ensure_ascii=False) + "\n\n"
        yield "data: " + json.dumps(last, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    log.info("bridge listening on 127.0.0.1:%s", CFG["port"])
    uvicorn.run(app, host="127.0.0.1", port=int(CFG["port"]), log_level="warning")
