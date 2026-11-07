# -*- coding: utf-8 -*-
"""Agent：把知识库能力包装成工具，交给 LLM 自主规划调用（OpenAI function calling）。

工具覆盖：当前时间、联系人/群匹配、会话列表、按人+时间范围取聊天记录、
消息上下文、文件列表与文件正文、群活跃度统计。
每一步调用都记录到 trace，前端可展开查看，保证可审计。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta

MAX_STEPS_CHAT = 4
MAX_STEPS_GLOBAL = 6
MAX_REFS = 120   # 兜底上限；返回前会先按答案实际引用的编号过滤，不会截掉被引用的来源

import privacy  # noqa: E402  出站脱敏，设置页可逐项关闭


def _cited_refs(answer: str, seen: dict) -> list[dict]:
    """只返回答案里真正引用了的 [n] 对应的来源，保持原编号。

    之前按前 MAX_REFS 条截断，模型引用靠后的编号（如 [121]）在前端找不到，
    点击无效——必须按引用过滤而不是按顺序截断。
    """
    cited = {int(x) for x in re.findall(r"\[(\d{1,3})\]", answer or "")}
    out = [r for r in seen.values() if r.get("n") in cited]
    out.sort(key=lambda r: r["n"])
    return out[:MAX_REFS]


# ------------------------------------------------- DSML 兜底（模型不支持原生 FC）
# deepseek-reasoner / deepseek-flash 等不支持 function calling 的模型会把调用意图
# 以 DSML 文本原样输出（如 <｜｜DSML｜｜ invoke name="read_chat">…），这里解析成真调用。
_DSML_BAR = "[｜|]"
_DSML_INVOKE_RE = re.compile(
    "<" + r"/?" + _DSML_BAR + r"{1,2}DSML" + _DSML_BAR + r"{1,2}\s*invoke\s+name=\"([^\"]+)\"\s*>"
    r"([\s\S]*?)"
    "<" + r"/?" + _DSML_BAR + r"{1,2}DSML" + _DSML_BAR + r"{1,2}\s*/?\s*invoke\s*>")
_DSML_PARAM_RE = re.compile(
    "<" + r"/?" + _DSML_BAR + r"{1,2}DSML" + _DSML_BAR + r"{1,2}\s*parameter\s+name=\"([^\"]+)\"([^>]*)>"
    r"([\s\S]*?)"
    "<" + r"/?" + _DSML_BAR + r"{1,2}DSML" + _DSML_BAR + r"{1,2}\s*/?\s*parameter\s*>")
_DSML_TAG_RE = re.compile(r"</?\s*[｜|]{0,2}\s*DSML[｜|]{0,2}[^>]*>")


def strip_dsml(text: str) -> str:
    """去掉模型泄漏的 DSML 标签，只留正文。"""
    return _DSML_TAG_RE.sub("", str(text or "")).strip()


def parse_dsml_calls(text: str) -> list[dict]:
    """把 DSML 文本解析成 OpenAI tool_calls 结构；解析不出返回空列表。"""
    calls = []
    for i, m in enumerate(_DSML_INVOKE_RE.finditer(text or "")):
        name = m.group(1).strip()
        args = {}
        for pm in _DSML_PARAM_RE.finditer(m.group(2)):
            pname = pm.group(1).strip()
            attrs = pm.group(2) or ""
            raw = pm.group(3)
            if 'string="true"' in attrs:
                args[pname] = raw
            else:
                s = raw.strip()
                try:
                    args[pname] = json.loads(s)      # 数字/布尔/JSON
                except Exception:
                    args[pname] = raw
        calls.append({"id": f"call_dsml_{i}_{int(time.time() * 1000)}",
                      "type": "function",
                      "function": {"name": name,
                                   "arguments": json.dumps(args, ensure_ascii=False)}})
    return calls


# ---------------------------------------------------------------- 工具实现
def _db():
    from main import KNOWLEDGE_DB
    con = sqlite3.connect(KNOWLEDGE_DB, check_same_thread=False, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    con.row_factory = sqlite3.Row
    return con


def _ts(value) -> int | None:
    """把 '2026-10-01' / '2026-10-01 12:00' / '3天前' 解析为 epoch。"""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    m = re.match(r"(\d+)\s*天前", s)
    if m:
        return int((datetime.now() - timedelta(days=int(m.group(1)))).timestamp())
    return None


def _chat_name(con, username: str) -> str:
    r = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat "
                    "WHERE username=?", (username,)).fetchone()
    return r[0] if r else username


def _sender(con, chat, sender, is_sender) -> str:
    if is_sender == 1:
        return "我"
    if sender:
        r = con.execute("SELECT COALESCE(NULLIF(remark,''), NULLIF(nick_name,''), username) "
                        "FROM contact WHERE username=?", (sender,)).fetchone()
        if r:
            return r[0]
    return sender or _chat_name(con, chat)


def _fmt(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts or 0))


def _msg_brief(con, row) -> dict:
    from textutil import display_text
    text = (display_text((row["type"] or 0) & 0xFFFFFFFF, row["content"]) or "")[:600]
    # 合并转发/文件/链接：display_text 只给标题，这里补上模型真正需要的内容。
    # parse_message 在 main.py 里，延迟导入避免循环依赖。
    try:
        import main as M
        parsed = M.parse_message(row)
        if isinstance(parsed, dict):
            rec = parsed.get("record")
            if rec:
                lines = [f"[合并转发《{parsed.get('title') or '聊天记录'}》"
                         f"共 {rec.get('count') or 0} 条]"]
                info = (rec.get("info") or "").strip()
                if info:
                    lines.append(info[:400])
                for it in (rec.get("items") or [])[:20]:
                    who = it.get("name") or ""
                    when = it.get("time") or ""
                    body = it.get("text") or it.get("title") or it.get("url") or ""
                    body = " ".join(str(body).split())
                    lines.append((f"{when} {who}: {body}" if (when or who) else body)[:300])
                if len(rec.get("items") or []) > 20:
                    lines.append(f"……其余 {len(rec['items']) - 20} 条未展开")
                text = "\n".join(x for x in lines if x)[:1500]
            elif parsed.get("kind") == "link" and parsed.get("url"):
                title = parsed.get("title") or text or "[链接]"
                text = f"{title} {parsed['url']}"[:800]
            elif parsed.get("kind") == "file" and parsed.get("title"):
                text = f"[文件] {parsed['title']}"[:600]
    except Exception:  # noqa: BLE001 解析失败不影响原消息
        pass
    # 媒体消息附上 OCR/转写文本：工具检索到图片/语音/视频时模型才能看到其内容
    try:
        t = con.execute("SELECT kind, text FROM media_text WHERE message_id=?",
                        (row["id"],)).fetchone()
        if t and t[1]:
            tag = "图片文字" if t[0] == "ocr" else "转写"
            text = (text or "[媒体消息]") + f"（{tag}: {t[1][:300]}）"
    except sqlite3.OperationalError:
        pass
    return {
        "id": row["id"], "chat": row["chat_user"], "chat_name": _chat_name(con, row["chat_user"]),
        "ts": row["ts"], "time": _fmt(row["ts"]),
        "who": _sender(con, row["chat_user"], row["sender"], row["is_sender"]),
        "text": text,
    }


# ---- 具体工具
def t_now(_args):
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    return {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "weekday": f"星期{week}",
            "epoch": int(now.timestamp()),
            "this_month_start": now.replace(day=1).strftime("%Y-%m-%d"),
            "today": now.strftime("%Y-%m-%d")}


def t_find_sessions(args):
    """按名字模糊匹配会话（群或好友）。"""
    q = (args.get("query") or "").strip()
    limit = min(int(args.get("limit") or 20), 50)
    con = _db()
    rows = con.execute("""
        SELECT c.username, COALESCE(NULLIF(c.display_name,''), c.username) AS name, c.is_group,
               c.last_ts, (SELECT COUNT(*) FROM message m WHERE m.chat_user=c.username) cnt
        FROM chat c
        WHERE (? = '' OR c.display_name LIKE ? OR c.username LIKE ?)
        ORDER BY c.last_ts DESC LIMIT ?""",
        (q, f"%{q}%", f"%{q}%", limit)).fetchall()
    con.close()
    return {"sessions": [{"username": r["username"], "name": r["name"],
                          "is_group": bool(r["is_group"]),
                          "last_time": _fmt(r["last_ts"]), "message_count": r["cnt"]}
                         for r in rows]}


def t_find_contacts(args):
    q = (args.get("query") or "").strip()
    limit = min(int(args.get("limit") or 20), 60)
    con = _db()
    rows = con.execute("""
        SELECT c.username,
               TRIM(COALESCE(NULLIF(TRIM(c.remark),''), NULLIF(TRIM(c.nick_name),''), '')) AS name,
               c.local_type
        FROM contact c
        WHERE c.local_type IN (1, 2)
          AND TRIM(COALESCE(NULLIF(TRIM(c.remark),''), NULLIF(TRIM(c.nick_name),''), '')) <> ''
          AND (? = '' OR c.nick_name LIKE ? OR c.remark LIKE ? OR c.quan_pin LIKE ?)
        LIMIT ?""", (q, f"%{q}%", f"%{q}%", f"%{q}%", limit)).fetchall()
    con.close()
    return {"contacts": [{"username": r["username"], "name": r["name"],
                          "is_group": r["local_type"] == 2} for r in rows]}


def t_search_messages(args):
    """关键词/语义检索消息，可限定会话与时间范围。"""
    from main import retrieve
    q = (args.get("query") or "").strip()
    chat = args.get("chat") or None
    con = _db()
    rows = retrieve(con, q, chat, int(args.get("limit") or 10))
    since, until = _ts(args.get("since")), _ts(args.get("until"))
    out = []
    for r in rows:
        if since and r["ts"] < since:
            continue
        if until and r["ts"] > until:
            continue
        out.append(_msg_brief(con, r))
    con.close()
    return {"messages": out, "count": len(out)}


def t_read_chat(args):
    """读取某会话在时间范围内的聊天记录（按时间正序）。"""
    chat = args.get("chat") or ""
    con = _db()
    if not chat:
        r = con.execute("SELECT username FROM chat WHERE display_name LIKE ? "
                        "ORDER BY last_ts DESC LIMIT 1", (f"%{args.get('chat_name','')}%",)).fetchone()
        chat = r[0] if r else ""
    if not chat:
        con.close()
        return {"error": "未找到该会话"}
    since, until = _ts(args.get("since")), _ts(args.get("until"))
    limit = min(int(args.get("limit") or 60), 200)
    conds, params = ["chat_user=?"], [chat]
    if since:
        conds.append("ts >= ?"); params.append(since)
    if until:
        conds.append("ts <= ?"); params.append(until)
    rows = con.execute(f"SELECT * FROM message WHERE {' AND '.join(conds)} "
                       f"ORDER BY ts DESC LIMIT ?", params + [limit]).fetchall()
    rows = list(reversed(rows))
    data = [_msg_brief(con, r) for r in rows if not r["noise"]]
    con.close()
    return {"chat": chat, "chat_name": data[0]["chat_name"] if data else chat,
            "messages": data, "count": len(data)}


def t_message_context(args):
    """取某条消息前后的上下文。"""
    mid = int(args.get("message_id") or 0)
    before = min(int(args.get("before") or 5), 20)
    after = min(int(args.get("after") or 5), 20)
    con = _db()
    m = con.execute("SELECT * FROM message WHERE id=?", (mid,)).fetchone()
    if not m:
        con.close()
        return {"error": "消息不存在"}
    rows = con.execute("SELECT * FROM message WHERE chat_user=? AND id BETWEEN ? AND ? "
                       "ORDER BY id", (m["chat_user"], mid - before, mid + after)).fetchall()
    data = [_msg_brief(con, r) for r in rows if not r["noise"]]
    con.close()
    return {"chat": m["chat_user"], "focus_id": mid, "messages": data}


def _file_text_info(con, chat: str, local_id) -> tuple[bool, int]:
    """(file_text 是否有记录, 正文字数)；无记录=还没处理，空记录=未下载/解析失败。"""
    try:
        r = con.execute("SELECT LENGTH(IFNULL(text,'')) FROM file_text WHERE key=?",
                        (f"{chat}:{int(local_id or 0)}",)).fetchone()
    except Exception:      # noqa: BLE001
        return False, 0
    if not r:
        return False, 0
    return True, int(r[0] or 0)


def _file_status(con, chat: str, local_id) -> dict:
    exists, chars = _file_text_info(con, chat, local_id)
    if chars > 0:
        return {"status": "parsed", "chars": chars}
    if exists:
        return {"status": "missing", "chars": 0}
    return {"status": "unprocessed", "chars": 0}


def t_list_files(args):
    """列出收发的文件：优先用微信资源映射表（真实文件名），可按会话/名字/时间过滤。"""
    import main as M
    rdb = M.resource_db()
    if rdb is None:
        return {"files": []}
    con = _db()
    chat_ids = {r[0]: r[1] for r in rdb.execute("SELECT rowid, user_name FROM ChatName2Id")}
    chat = args.get("chat") or ""
    q = (args.get("query") or "").strip()
    since = _ts(args.get("since"))
    limit = min(int(args.get("limit") or 20), 60)
    rows = rdb.execute("""
        SELECT i.chat_id, i.message_local_id, i.message_create_time, d.size, d.packed_info
        FROM MessageResourceInfo i JOIN MessageResourceDetail d ON d.message_id = i.message_id
        WHERE i.message_local_type & 4294967295 = 49 AND length(d.packed_info) > 2
        ORDER BY i.message_create_time DESC LIMIT 4000""").fetchall()
    out = []
    for r in rows:
        username = chat_ids.get(r["chat_id"], "")
        if chat and username != chat:
            continue
        ts = r["message_create_time"] or 0
        if since and ts < since:
            continue
        fields = M.pb_fields(r["packed_info"])
        name = ""
        if fields:
            inner = M.pb_fields(fields[0][1])
            for tag, val in (inner or fields):
                if tag == 1 and val:
                    name = val.decode("utf-8", "ignore")
                    break
        if not name or (q and q.lower() not in name.lower()):
            continue
        st = _file_status(con, username, r["message_local_id"])
        out.append({"chat": username, "chat_name": _chat_name(con, username),
                    "local_id": r["message_local_id"], "ts": ts, "time": _fmt(ts),
                    "title": name, "size": r["size"], **st})
        if len(out) >= limit:
            break
    con.close()
    return {"files": out}


def t_read_file(args):
    """读取某个文件的正文（PDF/Word/Excel/文本）。"""
    from main import locate_file, extract_text
    p = locate_file(args.get("chat", ""), int(args.get("local_id") or 0),
                    int(args.get("ts") or 0), args.get("name", ""))
    if not p:
        return {"status": "missing",
                "error": "文件未下载到本机（可能已被微信清理或尚未下载），无法读取正文"}
    text = extract_text(p, min(int(args.get("max_chars") or 12000), 30000))
    if text.startswith("[解析失败"):
        return {"status": "parse_failed", "error": "文件已下载但无法解析正文",
                "detail": text[:200]}
    if not text.strip():
        return {"status": "empty", "error": "文件已下载，但没有可提取的文字"}
    return {"name": p.name, "size": p.stat().st_size, "chars": len(text),
            "status": "parsed", "text": text}


def t_overview(args):
    """全局概览：会话/群/好友数量、消息总量、时间跨度、最活跃会话。"""
    con = _db()
    groups = con.execute("SELECT COUNT(*) FROM chat WHERE is_group=1").fetchone()[0]
    privates = con.execute("SELECT COUNT(*) FROM chat WHERE is_group=0").fetchone()[0]
    friends = con.execute("SELECT COUNT(*) FROM contact WHERE local_type=1").fetchone()[0]
    msgs = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    lo, hi = con.execute("SELECT MIN(ts), MAX(ts) FROM message").fetchone()
    files = 0
    try:
        import main as M
        rdb = M.resource_db()
        if rdb is not None:
            files = rdb.execute("SELECT COUNT(*) FROM MessageResourceInfo "
                                "WHERE message_local_type & 4294967295 = 49").fetchone()[0]
    except Exception:
        pass
    top = con.execute("""SELECT chat_user, COUNT(*) c FROM message GROUP BY chat_user
                         ORDER BY c DESC LIMIT 5""").fetchall()
    top_chats = [{"chat_name": _chat_name(con, r[0]), "count": r[1]}
                 for r in top]
    con.close()
    return {"groups": groups, "private_chats": privates, "friends": friends,
            "messages": msgs, "files": files,
            "range": {"from": _fmt(lo), "to": _fmt(hi)},
            "top_chats": top_chats}


def t_chat_stats(args):
    """会话统计：消息量、活跃成员、时间跨度。"""
    con = _db()
    chat = args.get("chat") or ""
    since, until = _ts(args.get("since")), _ts(args.get("until"))
    conds, params = ["chat_user=?"], [chat]
    if since:
        conds.append("ts >= ?"); params.append(since)
    if until:
        conds.append("ts <= ?"); params.append(until)
    where = " AND ".join(conds)
    total = con.execute(f"SELECT COUNT(*), MIN(ts), MAX(ts) FROM message WHERE {where}",
                        params).fetchone()
    top = con.execute(f"""SELECT sender, is_sender, COUNT(*) c FROM message
        WHERE {where} AND is_sender=0 GROUP BY sender ORDER BY c DESC LIMIT 8""",
        params).fetchall()
    top_senders = [{"who": _sender(con, chat, r[0], r[1]), "count": r[2]}
                   for r in top]
    name = _chat_name(con, chat)
    con.close()
    return {"chat": chat, "chat_name": name,
            "total": total[0], "from": _fmt(total[1]), "to": _fmt(total[2]),
            "top_senders": top_senders}


def _read_link_blocking(url: str, wait: int = 60, max_chars: int = 12000) -> dict:
    """读取链接正文；平台链接首次解析慢（OCR/转写），最多等待 wait 秒。"""
    import main as M
    d = M.fetch_article(url)
    if d.get("pending"):
        import link_media
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(2)
            st = link_media.job(url)
            if st.get("state") in ("done", "error"):
                d = M.fetch_article(url)
                break
    return d


def t_recent_links(args):
    """最近收到的链接及其正文（自动等待解析，含小红书 OCR / B站转写）。

    专供"总结最近发过什么/群里转的链接讲了什么"这类问题：一次调用同时返回
    链接清单和已解析正文，避免模型只看到标题。
    """
    since = _ts(args.get("since"))
    until = _ts(args.get("until"))
    limit = min(int(args.get("limit") or 5), 8)
    wait = min(int(args.get("wait_seconds") or 25), 60)
    con = _db()
    try:
        rows = con.execute("SELECT url, title, msg_ts, chat, platform FROM chat_link "
                           "WHERE msg_ts >= ? AND msg_ts <= ? ORDER BY msg_ts DESC LIMIT ?",
                           (since or 0, until or 10 ** 11, limit)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    out = []
    for r in rows:
        u = r["url"]
        d = _read_link_blocking(u, wait=wait, max_chars=6000)
        item = {"url": u, "title": d.get("title") or r["title"] or "",
                "received": _fmt(r["msg_ts"]), "chat_name": _chat_name(con, r["chat"])}
        if d.get("error") or d.get("pending"):
            item["error"] = d.get("error") or "仍在解析"
        else:
            item["chars"] = d.get("chars", 0)
            item["platform"] = d.get("platform", "") or (r["platform"] or "")
            item["text"] = (d.get("text") or "")[:6000]
        out.append(item)
    con.close()
    return {"links": out, "count": len(out),
            "note": "text 字段已是正文（含图片 OCR / 视频语音转写），可直接据此总结"}


def t_read_link(args):
    """读取链接正文（公众号/网页/小红书/B站）。平台链接首次解析较慢（OCR/转写），
    这里最多等待 wait_seconds 秒拿到真实内容，而不是立刻回一个"正在解析"。"""
    url = (args.get("url") or "").strip()
    if not url:
        return {"error": "缺少 url"}
    wait = min(int(args.get("wait_seconds") or 60), 150)
    d = _read_link_blocking(url, wait)
    if d.get("pending"):
        return {"error": "链接内容仍在解析（图片 OCR / 视频转写较慢），"
                         "可先回答其它部分，或稍后改用更短的问题再读一次"}
    if d.get("error"):
        return {"error": d["error"]}
    limit = min(int(args.get("max_chars") or 12000), 30000)
    out = {"url": url, "title": d.get("title", ""), "chars": d.get("chars", 0),
           "text": (d.get("text") or "")[:limit]}
    # 平台链接：附带来源与媒体信息，便于回答时说清"这是小红书/视频"
    if d.get("kind") in ("xhs", "bili"):
        out.update({"platform": d.get("platform", ""), "kind": d.get("kind"),
                    "images": d.get("images") or [], "owner": d.get("owner", ""),
                    "has_ocr": bool((d.get("parts") or {}).get("ocr")),
                    "has_transcript": bool((d.get("parts") or {}).get("subtitle")
                                           or (d.get("parts") or {}).get("asr"))})
    return out


def t_find_links(args):
    """在已索引的公众号/网页正文里检索（FTS 全文 + 标题兜底）。"""
    import main as M
    q = (args.get("query") or "").strip()
    limit = min(int(args.get("limit") or 8), 20)
    con = _db()
    hits = M.retrieve_links(con, q, k=limit)
    con.close()
    return {"links": [{"url": h["url"], "title": h["title"],
                       "received": _fmt(h.get("received") or 0) if h.get("received") else "",
                       "preview": (h["text"] or "")[:300]} for h in hits],
            "count": len(hits)}


_LINK_BLOCK = ("stodownload", "wxapp.", "mmbiz_", "res.wx.qq.com", "qpic.cn",
               "szmmsns", "vweixinf", "/cdn/", "cdn.", "douyin.com", "bilibili.com/video",
               "y.qq.com", ".m3u8", "/static/", "/assets/", "captcha",
               "work.weixin.qq.com", "wwfile", "unsupported", "biz.r.qq.com")


def _is_article_link(u: str) -> bool:
    ul = u.lower()
    if any(k in ul for k in _LINK_BLOCK):
        return False
    path = ul.split("://")[-1].split("?")[0]
    if path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mp3",
                      ".zip", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")):
        return False
    if "mp.weixin.qq.com/s" in ul:
        return True
    return "/" in path and path[-1] not in ("/",)


def t_list_chat_links(args):
    """列出某个会话里收到过的公众号/网页链接（含接收日期），可选关键词过滤。"""
    import main as M
    con = _db()
    chat = args.get("chat") or ""
    q = (args.get("query") or "").strip()
    since = _ts(args.get("since"))
    limit = min(int(args.get("limit") or 20), 60)
    conds = ["content LIKE '%http%'"]
    params: list = []
    if chat:
        conds.append("chat_user=?")
        params.append(chat)
    if since:
        conds.append("ts >= ?")
        params.append(since)
    rows = con.execute(f"SELECT id, chat_user, ts, content FROM message "
                       f"WHERE {' AND '.join(conds)} ORDER BY ts DESC LIMIT 3000",
                       params).fetchall()
    import html as _h
    import re as _re
    cached = {}
    try:
        for u, t, mt in con.execute("SELECT url, title, msg_ts FROM link_cache"):
            cached[u] = (t, mt)
    except Exception:
        pass
    out = []
    for r in rows:
        content = _h.unescape(r["content"] or "")
        urls = _re.findall(r"https?://[^\s\"'<>()\[\]]+", content)
        title = ""
        mt = _re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", content, _re.S)
        if mt:
            title = mt.group(1).strip()
        for u in urls:
            if not _is_article_link(u):
                continue
            ct, cmt = cached.get(u, ("", 0))
            name = (ct or title or u[:60]).strip()
            if name.startswith("当前版本不支持") or name in ("", "[链接]"):
                continue
            if q and q not in name and q not in content:
                continue
            out.append({"url": u, "title": name[:80],
                        "received": _fmt(r["ts"]), "msg_id": r["id"],
                        "chat": r["chat_user"], "chat_name": _chat_name(con, r["chat_user"]),
                        "indexed": bool(ct)})
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    con.close()
    return {"links": out, "count": len(out)}


_FILE_CACHE: dict = {"ts": 0, "rows": []}


def _vcon_for_agent():
    import main as M
    return M.vconn()


def _file_catalog(limit: int = 3000) -> list[dict]:
    """缓存文件清单（每个文件：chat/local_id/ts/title/size），60 秒刷新一次。"""
    import main as M
    if time.time() - _FILE_CACHE["ts"] < 60 and _FILE_CACHE["rows"]:
        return _FILE_CACHE["rows"]
    rows_out: list[dict] = []
    rdb = M.resource_db()
    if rdb is not None:
        try:
            chat_ids = {r[0]: r[1] for r in
                        rdb.execute("SELECT rowid, user_name FROM ChatName2Id")}
            rows = rdb.execute("""SELECT i.chat_id, i.message_local_id,
                    i.message_create_time, d.size, d.packed_info
                FROM MessageResourceInfo i
                JOIN MessageResourceDetail d ON d.message_id = i.message_id
                WHERE i.message_local_type & 4294967295 = 49
                AND length(d.packed_info) > 2
                ORDER BY i.message_create_time DESC LIMIT ?""", (limit,)).fetchall()
            for r in rows:
                fields = M.pb_fields(r["packed_info"])
                name = ""
                if fields:
                    inner = M.pb_fields(fields[0][1])
                    for tag, val in (inner or fields):
                        if tag == 1 and val:
                            name = val.decode("utf-8", "ignore"); break
                if name:
                    rows_out.append({"chat": chat_ids.get(r["chat_id"], ""),
                                     "local_id": r["message_local_id"],
                                     "ts": r["message_create_time"],
                                     "title": name, "size": r["size"]})
        except Exception:
            rows_out = []
    _FILE_CACHE.update(ts=time.time(), rows=rows_out)
    return rows_out


def t_rag_search(args):
    """Agent 自主 RAG：自选语料范围（消息/文件/公众号/全部）+ 自造多组关键词。

    args:
      queries: 2~4 组关键词（同义/近义/相关词），会分别检索后聚合
      scope: messages | files | links | all
      chat / since / until: 可选过滤
      k: 每组召回条数
    """
    import main as M
    qs = [x for x in (args.get("queries") or []) if x][:4]
    if not qs:
        q = (args.get("query") or "").strip()
        if not q:
            return {"error": "缺少 queries"}
        qs = [q]
    scope_ = (args.get("scope") or "messages").lower()
    chat = args.get("chat") or None
    since, until = _ts(args.get("since")), _ts(args.get("until"))
    k = min(int(args.get("k") or 8), 20)
    con = _db()

    msgs: dict[int, dict] = {}
    links: dict[str, dict] = {}
    files: list[dict] = []
    per_query = []

    # 1) 多组关键词**并行**召回（每组独立连接，避免跨线程问题）
    from concurrent.futures import ThreadPoolExecutor
    scores: dict[int, float] = {}

    def kw_hits(qq: str) -> list[int]:
        c2 = _db()
        try:
            return M._keyword_hits(c2, qq, chat, k)
        except Exception:
            return []
        finally:
            c2.close()

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(qs)))) as ex:
        kw_sets = list(ex.map(kw_hits, qs))
    for q, ids in zip(qs, kw_sets):
        for rank, mid in enumerate(ids):
            scores[mid] = scores.get(mid, 0) + 1.0 / (60 + rank)
        per_query.append({"query": q, "keyword_hits": len(ids)})

    if scope_ in ("messages", "all"):
        try:
            import vector as V
            key = (" ".join(qs), chat or "")
            vcache = getattr(V, "_last_cache", None)
            vec = V.search(_vcon_for_agent(), " ".join(qs), k=max(20, k * 2), chat=chat)
            for rank, (mid, sim) in enumerate(vec):
                scores[mid] = scores.get(mid, 0) + 1.0 / (60 + rank)
        except Exception:
            pass

    if scores:
        ids = [m for m, _ in sorted(scores.items(), key=lambda x: -x[1])[:k * 3]]
        rows = con.execute("SELECT * FROM message WHERE id IN (%s)"
                           % ",".join("?" * len(ids)), ids).fetchall()
        by_id = {r["id"]: r for r in rows}
        for mid in ids:
            r = by_id.get(mid)
            if not r or r["noise"]:
                continue
            ts = r["ts"] or 0
            if since and ts < since:
                continue
            if until and ts > until:
                continue
            msgs[mid] = _msg_brief(con, r)

    # 2) 文章（同样并行）
    if scope_ in ("links", "all"):
        def link_hits(qq: str) -> list[dict]:
            c2 = _db()
            try:
                return M.retrieve_links(c2, qq, k=max(2, k // 2))
            except Exception:
                return []
            finally:
                c2.close()

        with ThreadPoolExecutor(max_workers=min(4, max(1, len(qs)))) as ex:
            link_sets = list(ex.map(link_hits, qs))
        for hs in link_sets:
            for h in hs:
                links.setdefault(h["url"], {
                    "url": h["url"], "title": h["title"],
                    "received": _fmt(h.get("received") or 0) if h.get("received") else "",
                    "preview": (h["text"] or "")[:800]})

    # 3) 文件（走缓存清单）
    if scope_ in ("files", "all"):
        cat = _file_catalog()
        seen_names = set()
        for q in qs:
            ql = q.lower()
            for f in cat:
                if chat and f["chat"] != chat:
                    continue
                if since and (f["ts"] or 0) < since:
                    continue
                if until and (f["ts"] or 0) > until:
                    continue
                if ql in f["title"].lower() and f["title"] not in seen_names:
                    seen_names.add(f["title"])
                    st = _file_status(con, f["chat"], f["local_id"])
                    files.append({**f, "chat_name": _chat_name(con, f["chat"]), **st})
                    if len(files) >= k:
                        break
    # 4) 朋友圈（正文 + 图片 OCR 文字；FTS + 语义兜底）
    moments: list[dict] = []
    if scope_ in ("moments", "all"):
        import sns as sns_mod
        seen_tids: set[str] = set()
        for q in qs:
            try:
                hits = sns_mod.search_moments(con, q, limit=max(2, k // 2))
            except Exception:
                hits = []
            for h in hits:
                if h["tid"] in seen_tids:
                    continue
                seen_tids.add(h["tid"])
                moments.append({"tid": h["tid"],
                                "who": h.get("who") or h.get("author_name") or h.get("author") or "朋友圈",
                                "ts": h.get("ts", 0),
                                "ts_str": _fmt(h.get("ts") or 0) if h.get("ts") else "",
                                "text": (h.get("text") or "")[:400],
                                "n_img": h.get("n_img", 0), "kind": "moment"})
        moments.sort(key=lambda m: -(m.get("ts") or 0))
        moments = moments[:max_links]
    # 聚合上限：消息按相关度(RRF)排序取前 2k（最多 20）；文章/文件按时间新→旧
    max_msg = min(k * 2, 20)
    max_links = min(k, 6)
    max_files = min(k, 8)
    msg_ids = list(scores.keys())
    msg_ranked = sorted([msgs[m] for m in msg_ids if m in msgs],
                        key=lambda m: (-scores[m["id"]], -m["ts"]))[:max_msg]
    link_list = sorted(links.values(), key=lambda x: x.get("received") or "",
                       reverse=True)[:max_links]
    file_list = sorted(files, key=lambda f: -(f.get("ts") or 0))[:max_files]
    # ② 命中消息附带前后各 4 条上下文：连发簇命中任意一条即可让模型看到整簇。
    #   短句常缺类目词（如「先用安提普拉明」全句不含"药"），靠邻句补上词面缺口。
    #   预算：命中 ≤12 条、总条目 ≤34、ctx 文本压到 120 字，配合 agent 循环里
    #   rag 类工具 12000 字符的结果上限。
    if msgs and scope_ in ("messages", "all"):
        ranked_ids = [m for m in ids if m in msgs][:12]
        hit_set = set(ranked_ids)
        PER_CTX, MAX_TOTAL = 4, 40
        by_chat: dict[str, list[int]] = {}
        for mid in ranked_ids:
            by_chat.setdefault(msgs[mid]["chat"], []).append(mid)
        ctx_rows: dict[int, Any] = {}
        for chat, mids in by_chat.items():
            try:
                rows = con.execute(
                    "SELECT * FROM message WHERE chat_user=? AND id BETWEEN ? AND ? "
                    "AND noise=0 AND type NOT IN (10000, 10002) ORDER BY id",
                    (chat, min(mids) - PER_CTX, max(mids) + PER_CTX)).fetchall()
            except sqlite3.OperationalError:
                continue
            for r in rows:
                if r["id"] not in msgs:
                    ctx_rows[r["id"]] = r
        entries: list[dict] = []
        emitted: set[int] = set()
        for mid in ranked_ids:
            chat = msgs[mid]["chat"]
            block = [mid] + [x["id"] for x in ctx_rows.values()
                             if x["chat_user"] == chat and abs(x["id"] - mid) <= PER_CTX]
            for bid in sorted(set(block)):
                if bid in emitted or len(entries) >= MAX_TOTAL:
                    continue
                emitted.add(bid)
                b = dict(msgs.get(bid) or _msg_brief(con, ctx_rows[bid]))
                b["hit"] = bid in hit_set
                if not b["hit"]:
                    b["text"] = b["text"][:120]
                entries.append(b)
        if entries:
            msg_ranked = entries


    con.close()
    return {"scope": scope_, "queries_used": qs, "per_query": per_query,
            "messages": msg_ranked, "links": link_list, "files": file_list,
            "moments": moments,
            "counts": {"messages": len(msg_ranked), "links": len(link_list),
                       "files": len(file_list), "moments": len(moments)},
            "limits": {"messages": max_msg, "links": max_links, "files": max_files},
            "note": "messages 按簇给出并带 hit/ctx 标记：hit=检索命中，ctx=该命中前后文"
                    "（连发短句会整段出现）；受篇幅限制只展示一部分，枚举/统计类问题"
                    "如有遗漏请换关键词再检索或用 read_chat 通读"}


def t_recent_messages(args):
    """取某会话最近 N 条（或最近 N 小时）的真实消息，按时间正序。"""
    con = _db()
    chat = args.get("chat") or ""
    if not chat:
        chat = _last_chat()
    hours = int(args.get("hours") or 0)
    limit = min(int(args.get("limit") or 60), 200)
    if hours:
        since = int((datetime.now() - timedelta(hours=hours)).timestamp())
        rows = con.execute("SELECT * FROM message WHERE chat_user=? AND ts>=? AND noise=0 "
                           "ORDER BY ts DESC LIMIT ?", (chat, since, limit)).fetchall()
    else:
        rows = con.execute("SELECT * FROM message WHERE chat_user=? AND noise=0 "
                           "ORDER BY ts DESC LIMIT ?", (chat, limit)).fetchall()
    rows = list(reversed(rows))
    data = [_msg_brief(con, r) for r in rows]
    name = _chat_name(con, chat) if data else chat
    con.close()
    return {"chat": chat, "chat_name": name,
            "hours": hours, "messages": data, "count": len(data)}


def t_search_moments(args):
    """检索朋友圈（正文关键词 + OCR 图片文字 + 语义向量），支持作者/天数过滤。

    query 留空时按时间倒序返回最近的朋友圈，用于「最近朋友圈发生了啥」。
    """
    import sns as sns_mod
    query = str(args.get("query") or "")
    con = _db()
    try:
        items = sns_mod.search_moments(con, query,
                                       author=str(args.get("author") or ""),
                                       days=int(args.get("days") or 0),
                                       limit=min(int(args.get("limit") or 10), 30))
    finally:
        con.close()
    for it in items:
        it["text"] = (it.get("text") or "")[:400]
    return {"count": len(items), "moments": items}


def t_get_moment(args):
    """读取单条朋友圈全文，含图片/视频信息、图片 OCR 文字、评论与点赞。"""
    import sns as sns_mod
    tid = str(args.get("tid") or "")
    if not tid:
        return {"error": "需要 tid（来自 search_moments 结果）"}
    con = _db()
    try:
        post = sns_mod.get_moment(con, tid)
    finally:
        con.close()
    if post is None:
        return {"error": f"朋友圈 {tid} 不存在（可能是尚未同步的新朋友圈）"}
    post["media"] = [{"i": m.get("i", 0), "video": m.get("video", False),
                      "dur": m.get("dur", 0), "desc": (m.get("desc") or "")[:200],
                      "ocr": (m.get("ocr") or "")[:800], "local": m.get("local", False)}
                     for m in post.get("media", [])]
    post["who"] = post.get("author_name") or post.get("author") or "朋友圈"
    return post


# ---------------------------------------------------------------- 工具注册
TOOLS = {
    "get_current_time": (t_now, "获取当前日期时间、星期，用于理解「今天/上周/上个月」等相对时间。",
                         {"type": "object", "properties": {}}),
    "find_sessions": (t_find_sessions, "按名称模糊查找会话（群聊或好友），返回用户名、是否群、消息数、最近时间。",
                      {"type": "object", "properties": {
                          "query": {"type": "string", "description": "会话名关键词，空串表示最近会话"},
                          "limit": {"type": "integer"}}, "required": ["query"]}),
    "find_contacts": (t_find_contacts, "按名字/拼音查找联系人，得到 username。",
                      {"type": "object", "properties": {
                          "query": {"type": "string"}, "limit": {"type": "integer"}},
                       "required": ["query"]}),
    "search_messages": (t_search_messages,
                        "在聊天记录中检索（关键词+语义混合），可限定会话 username 与时间范围。",
                        {"type": "object", "properties": {
                            "query": {"type": "string"},
                            "chat": {"type": "string", "description": "会话 username，可省略表示全局"},
                            "since": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                            "until": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                            "limit": {"type": "integer"}}, "required": ["query"]}),
    "read_chat": (t_read_chat,
                  "按会话与时间范围读取聊天记录（正序），用于「某段时间群里聊了什么」。",
                  {"type": "object", "properties": {
                      "chat": {"type": "string", "description": "会话 username"},
                      "chat_name": {"type": "string", "description": "会话名（不知道 username 时用）"},
                      "since": {"type": "string"}, "until": {"type": "string"},
                      "limit": {"type": "integer"}}}),
    "get_message_context": (t_message_context, "取某条消息前后若干条上下文。",
                            {"type": "object", "properties": {
                                "message_id": {"type": "integer"},
                                "before": {"type": "integer"}, "after": {"type": "integer"}},
                             "required": ["message_id"]}),
    "list_files": (t_list_files, "列出会话中收发的文件（名字可过滤），返回 local_id/ts/name 供 read_file 使用。",
                   {"type": "object", "properties": {
                       "chat": {"type": "string"}, "query": {"type": "string"},
                       "since": {"type": "string"}, "limit": {"type": "integer"}}}),
    "read_file": (t_read_file, "读取文件正文（PDF/Word/Excel/文本），用于针对文件内容回答。",
                  {"type": "object", "properties": {
                      "chat": {"type": "string"}, "local_id": {"type": "integer"},
                      "ts": {"type": "integer"}, "name": {"type": "string"},
                      "max_chars": {"type": "integer"}},
                   "required": ["chat", "local_id", "ts", "name"]}),
    "overview_stats": (t_overview,
                       "全局统计：有多少个群/私聊/好友、消息总量、时间跨度、最活跃会话。"
                       "问「一共多少」「总量」类问题必须先调用它，不要估算。",
                       {"type": "object", "properties": {}}),
    "chat_stats": (t_chat_stats, "统计会话消息量、时间跨度、发言最多的人。",
                   {"type": "object", "properties": {
                       "chat": {"type": "string"}, "since": {"type": "string"},
                       "until": {"type": "string"}}, "required": ["chat"]}),
    "read_link": (t_read_link, "读取链接正文（公众号/网页/小红书/B站）。平台链接首次需要"
                               "OCR 图片或转写视频，工具会自动等待完成再返回全文；"
                               "用于回答'这篇讲了什么/链接里说了什么'。",
                  {"type": "object", "properties": {
                      "url": {"type": "string"},
                      "max_chars": {"type": "integer"},
                      "wait_seconds": {"type": "integer",
                                       "description": "最多等待解析的秒数，默认 60"}},
                   "required": ["url"]}),
    "recent_links": (t_recent_links,
                     "列出某时间段内收到的链接并同时返回正文（已含图片 OCR / 视频语音转写）。"
                     "**问「最近一周/这段时间发过什么」「群里转的链接都讲了什么」时优先用它**，"
                     "一次就能拿到清单和内容，不必逐个 read_link。",
                     {"type": "object", "properties": {
                         "since": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                         "until": {"type": "string"},
                         "limit": {"type": "integer", "description": "最多返回几条，默认 5"},
                         "wait_seconds": {"type": "integer"}},
                      "required": []}),
    "rag_search": (t_rag_search,
                   "主动 RAG：自选语料范围（messages=聊天记录 / files=文件 / links=公众号网页 / "
                   "moments=朋友圈(含图片OCR文字) / all=全部），并传入你自行生成的 2~4 组关键词"
                   "做多路检索后聚合。可加 chat/since/until 限定时段与会话。找不到再换关键词重试。",
                   {"type": "object", "properties": {
                       "queries": {"type": "array", "items": {"type": "string"},
                                   "description": "2~4 组检索词（同义/近义/相关）"},
                       "scope": {"type": "string",
                                 "enum": ["messages", "files", "links", "moments", "all"]},
                       "chat": {"type": "string"}, "since": {"type": "string"},
                       "until": {"type": "string"}, "k": {"type": "integer"}},
                    "required": ["queries"]}),
    "recent_messages": (t_recent_messages,
                        "取某会话最近 N 条/最近 N 小时的真实消息（按时间正序），"
                        "用于「最近聊了什么」这类问题。",
                        {"type": "object", "properties": {
                            "chat": {"type": "string"}, "hours": {"type": "integer"},
                            "limit": {"type": "integer"}}}),
    "list_chat_links": (t_list_chat_links,
                        "列出发在聊天里的公众号/网页链接（含收到日期、是否已索引正文），"
                        "可按会话与时间过滤；要读正文再调 read_link。",
                        {"type": "object", "properties": {
                            "chat": {"type": "string"}, "query": {"type": "string"},
                            "since": {"type": "string"},
                            "limit": {"type": "integer"}}}),
    "find_links": (t_find_links,
                   "在已索引的公众号/网页文章正文中按关键词检索，返回 url 与标题；"
                   "之后用 read_link(url) 取全文。",
                   {"type": "object", "properties": {
                       "query": {"type": "string"}, "limit": {"type": "integer"}}}),
    "search_moments": (t_search_moments,
                       "检索朋友圈（关键词匹配正文与图片 OCR 文字，弱结果时语义向量兜底），"
                       "可按 author（名字/微信号）与 days（最近 N 天）过滤；"
                       "query 留空则按时间倒序返回最近的朋友圈。"
                       "回答「最近朋友圈发生了什么/某人朋友圈发过什么/谁提到过某事」用它。",
                       {"type": "object", "properties": {
                           "query": {"type": "string", "description": "关键词，可留空看最近"},
                           "author": {"type": "string", "description": "作者名关键词，可省略"},
                           "days": {"type": "integer", "description": "限定最近 N 天，可省略"},
                           "limit": {"type": "integer"}}}),
    "get_moment": (t_get_moment,
                   "读取单条朋友圈全文：正文、每张图的 OCR 文字、视频时长、评论与点赞数。"
                   "tid 来自 search_moments。",
                   {"type": "object", "properties": {
                       "tid": {"type": "string"}}, "required": ["tid"]}),
}

# QQ 模式专用工具：只检索 QQ NT 本地库，不碰微信表
def t_qq_search(args: dict) -> dict:
    import main as M
    import qq_local
    query = str(args.get("query") or "").strip()
    conv = str(args.get("conv") or "").strip()
    limit = max(1, min(int(args.get("limit") or 20), 80))
    con = M.db()
    try:
        items = qq_local.search(con, query, limit, conv)
    finally:
        con.close()
    # messages（而非 items）：与微信工具同结构，agent 主循环才能登记 ref 编号，
    # 前端据此渲染可点击的 [n] 引用并跳回 QQ 原文
    msgs = []
    for x in items:
        mid = x.get("id")
        text = (x.get("snippet") or x.get("text") or "").strip()
        if not mid or not text:
            continue
        msgs.append({"id": mid,
                     "chat": x.get("conv") or "",
                     "chat_name": x.get("conv_name") or x.get("conv") or "",
                     "who": x.get("who") or x.get("sender_name") or "",
                     "ts": x.get("ts") or 0,
                     "text": text[:1200]})
    return {"query": query, "count": len(msgs), "messages": msgs}


def t_qq_contacts(args: dict) -> dict:
    import main as M
    import qq_local
    query = str(args.get("query") or "").strip()
    kind = str(args.get("kind") or "").strip()
    con = M.db()
    try:
        items = qq_local.contacts(con, query, kind, max(1, min(int(args.get("limit") or 30), 200)))
    finally:
        con.close()
    return {"query": query, "count": len(items),
            "items": [{"kind": x.get("kind"), "name": x.get("name"),
                       "uin": x.get("uin"), "uid": x.get("uid"),
                       "messages": x.get("cnt") or 0} for x in items]}


def t_qq_files(args: dict) -> dict:
    import main as M
    import qq_local
    query = str(args.get("query") or "").strip()
    con = M.db()
    try:
        items = qq_local.files(con, query, "", "", max(1, min(int(args.get("limit") or 20), 100)))
    finally:
        con.close()
    return {"query": query, "count": len(items),
            "items": [{"name": x.get("name"), "chat": x.get("conv_name") or x.get("conv"),
                       "who": x.get("sender_name"), "ts": x.get("ts"),
                       "size": x.get("size"), "kind": x.get("kind")} for x in items]}


QQ_TOOLS = {
    "search_qq": (t_qq_search,
                  "检索本机 QQ 聊天记录（消息正文），返回 QQ 群/好友、发言人、时间与原文。"
                  "问 QQ 群/好友/某段 QQ 聊天时优先用它；query 可留空看最近消息。",
                  {"type": "object", "properties": {
                      "query": {"type": "string", "description": "关键词，可留空看最近"},
                      "conv": {"type": "string",
                               "description": "限定 QQ 会话（group:... / c2c:...）；在某个群里提问时由系统给出"},
                      "limit": {"type": "integer"}}}),
    "search_qq_contacts": (t_qq_contacts,
                           "检索本机 QQ 联系人 / 群，返回名称、QQ 号、消息数；kind 可填 friend 或 group。",
                           {"type": "object", "properties": {
                               "query": {"type": "string"},
                               "kind": {"type": "string"},
                               "limit": {"type": "integer"}}}),
    "search_qq_files": (t_qq_files,
                        "检索本机 QQ 聊天文件，返回文件名、群/好友、发送者、时间与大小。",
                        {"type": "object", "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer"}}}),
}

QQ_SYSTEM_PROMPT = """你是用户的 QQ 记录分析助手，数据来自本机 QQ 数据库。
你有三个检索工具：search_qq 查聊天内容，search_qq_contacts 查 QQ 联系人/群，
search_qq_files 查 QQ 聊天文件。按用户问题生成 1~3 组关键词检索，
必要时可用空 query 查看最近消息；不要使用微信工具，也不要假装看过没检索到的记录。
回答用纯文本、口语化；找不到就直说。
如果用户问的是微信记录，提示他切换到微信界面或使用微信 Agent。

引用规则（必须遵守）：
- search_qq 返回的每条消息都带有 ref 编号（如 "ref": 3）；回答里引用任何一条
  具体消息，都要在对应句子后标注 [3]。同一来源永远用同一个编号，不要自己重排。
- 引用时可以同时写清群名/好友名与时间（如「计系小群3.0 · David 11-05」），
  但 [n] 标记不能省——界面上的 [n] 就是可点击的原文链接。
- **禁止在答案末尾输出「引用来源 / 参考资料」清单**：正文里的 [n] 已经可点击，
  末尾清单是多余的，不要写。

检索建议：
- 「最近/刚才/今天」先 search_qq(query="", limit=30) 看最近消息。
- 问某个群/某人：把群名或人名放进 query。
- 问具体事件：换同义词、简称、相关概念多查一轮。
- 统计类问题只能基于检索到的样本回答，并注明「基于检索到的消息，可能有遗漏」。
"""

SYSTEM_PROMPT = """你是用户的微信记录分析助手，可以调用工具主动检索本机历史数据。

检索策略（重要）：
1. 先判断范围：
   - 用户正在某个会话里提问 → 默认该会话。**若系统已给出【最近记录】且足以回答，
     直接作答，不要调用任何工具**（这是最快路径）。
   - 全局提问 → 自己决定检索什么：聊天记录、文件、公众号/网页，或全部。
   - 涉及朋友圈（「某人发过什么朋友圈」「朋友圈里提到过…」）→ search_moments；
     命中后用 get_moment 看单条全文（含图片 OCR 文字与评论）。
2. **关键词由你自己生成**：把用户问题改写成 2~4 组互补的检索词
   （同义词、别名、相关概念、更具体的说法），一次性传给 rag_search。
   例：「上线时间」→ ["上线","发布日期","什么时候上线","排期"]。
3. 用 rag_search 主动检索，可带 chat（限定联系人/群）与 since/until（限定时间）。
   命中不足时换一组关键词再试；仍不足则如实说没找到。
4. 「最近/刚才/今天」类问题用 recent_messages（或直接用【最近记录】），
   不要用 retrieve 去找几个月前的内容。
5. 需要精确统计（多少个群/多少条消息/谁发言最多）必须用 overview_stats 或 chat_stats。
6. 需要文件内容：rag_search(scope="files") 找文件 → read_file 读正文；
   文件条目带 status：parsed=正文已提取，missing=未下载或解析失败，unprocessed=还没处理。
   **status 不是 parsed 时不要假装读过内容**；read_file 返回 missing/parse_failed/empty
   就如实告诉用户「文件未下载/无法解析」，不要编造正文。
   单个文件超过 5MB 时只读前 8000 字，不要反复整文件读取。
7. 需要公众号文章：rag_search(scope="links") 或 find_links → read_link 取全文；
   想知道"群里发过哪些文章/什么时候发的"用 list_chat_links。
   **凡是要"总结/归纳/复盘某段时间发的内容"，若消息里有链接（小红书/B站/公众号），
   必须先用 list_chat_links 或 rag_search 找到链接，再对其中相关的几个调 read_link
   读出正文（它会自动等待 OCR/转写完成），绝不能只凭链接标题就下结论。**
   若 read_link 返回"仍在解析"，可以先用 wait_seconds 重新读一次，或明确告诉用户
   "其中一个链接内容正在解析"。小红书/B站正文已包含图片 OCR 与视频语音转写。
8. 引用具体消息时先 get_message_context 看前后文。
9. 回答必须基于工具返回的真实内容，找不到就明说，绝不编造。
   工具结果里每条消息/文件/文章都带有 **ref 编号**（如 "ref": 3）；
   引用时直接标注它，写作 [3]。同一来源永远用同一个编号，不要自己另编顺序。
   文件（PDF/Word/Excel/图片等）同样用它的 ref 编号标注。
10. **禁止在答案末尾输出「引用来源 / 参考资料」清单**：正文里的 [n] 在界面上
    本身就是可点击链接（点击跳原文/打开源文件），末尾清单是多余的，不要写。
11. 步数感知：工具结果前会标注 [第X/N轮]（N=本问题允许的总步数，探索模式下更大）。信息足够就立刻作答；
    不足就继续检索（换关键词/换语料范围/加时间过滤）。[最后一步] 时必须直接给出最终答案。
    判断标准是「信息是否足够回答用户的问题」，而不是命中条数——3 条高质量命中
    足以回答简单问题；复杂问题（统计、跨群对比）命中再多也可能需要再查。
12. **列举/统计类问题**（"有哪些/哪些/统计一下所有X"）：你的清单只能来自检索命中的样本，
    而检索永远可能漏（消息措辞不含检索词的情况很常见）。即使已多轮检索，
    也必须在清单末尾用一句话注明「以上基于检索到的消息，可能有遗漏」；
    不要宣称"全部/完整/一共就这些"。只有检索后确实零命中才能说"没有"。"""




def _recent_line_budget(text: str) -> int:
    """媒体消息带 OCR/转写内容，行预算放宽，模型才能读到完整转写。"""
    return 400 if text.startswith(("[图片]", "[语音]", "[视频]")) else 220


def _recent_block(chat: str, hours: int = 24, limit: int = 60) -> str:
    """取会话最近记录，拼成文本（供 Agent 系统提示预注入；无编号，避免干扰顺序引用）。"""
    con = _db()
    since = int((datetime.now() - timedelta(hours=hours)).timestamp())
    rows = con.execute("SELECT * FROM message WHERE chat_user=? AND ts>=? AND noise=0 "
                       "ORDER BY ts DESC LIMIT ?", (chat, since, limit)).fetchall()
    if len(rows) < 3:
        rows = con.execute("SELECT * FROM message WHERE chat_user=? AND noise=0 "
                           "ORDER BY ts DESC LIMIT ?", (chat, 30)).fetchall()
    rows = list(reversed(rows))
    out = []
    for r in rows:
        b = _msg_brief(con, r)
        txt = b["text"].replace(chr(10), " ")[:_recent_line_budget(b["text"])]
        if not txt:
            continue
        out.append(f"{_fmt(r['ts'])} {b['who']}: {txt}")
    con.close()
    return chr(10).join(out[-50:])


def _qq_recent_block(conv: str, limit: int = 80) -> str:
    """QQ 会话最近记录（供 QQ Agent 预注入；无编号，引用需用 search_qq 取 ref）。"""
    import main as M
    import qq_local
    con = M.db()
    try:
        rows = qq_local.messages(con, conv, limit)
    finally:
        con.close()
    out = []
    for r in rows:
        text = (r.get("text") or "").replace(chr(10), " ").strip()
        if not text:
            continue
        who = "我" if r.get("is_sender") else (r.get("sender_name") or "?")
        out.append(f"{_fmt(r.get('ts') or 0)} {who}: {text[:220]}")
    return chr(10).join(out)


def _recent_block_numbered(chat: str, hours: int = 24, limit: int = 60):
    """最近记录（每行行首 [n] 为引用编号），返回 (文本, refs)。

    供会话内快速作答：refs 与文本行一一对应，前端点击 [n] 可跳到对应消息。
    """
    con = _db()
    since = int((datetime.now() - timedelta(hours=hours)).timestamp())
    rows = con.execute("SELECT * FROM message WHERE chat_user=? AND ts>=? AND noise=0 "
                       "ORDER BY ts DESC LIMIT ?", (chat, since, limit)).fetchall()
    if len(rows) < 3:
        rows = con.execute("SELECT * FROM message WHERE chat_user=? AND noise=0 "
                           "ORDER BY ts DESC LIMIT ?", (chat, 30)).fetchall()
    rows = list(reversed(rows))
    out, refs = [], []
    for r in rows:
        b = _msg_brief(con, r)
        txt = b["text"].replace(chr(10), " ")[:_recent_line_budget(b["text"])]
        if not txt:
            continue
        n = len(refs) + 1
        out.append(f"[{n}] {_fmt(r['ts'])} {b['who']}: {txt}")
        refs.append({"n": n, "id": b["id"], "chat": b["chat"], "chat_name": b["chat_name"],
                     "ts": b["ts"], "sender_name": b["who"], "snippet": b["text"][:160],
                     "ctx": []})
    con.close()
    return chr(10).join(out[-50:]), refs[-50:]


def _pick_model(conf):
    llm = conf.get("llm", {})
    if llm.get("thinking"):
        return llm.get("thinking_model") or llm.get("model")
    return llm.get("model")


def friendly_err(exc) -> str:
    """把模型接口的原始报错转成用户能看懂、知道该干嘛的提示。"""
    s = str(exc).replace("urlopen error ", "").replace("<", "").replace(">", "")
    low = s.lower()
    if "401" in low or "403" in low or "unauthorized" in low:
        return "模型接口拒绝访问：请检查 API Key 和模型权限。"
    if "429" in low or "rate limit" in low:
        return "模型接口限流：请稍等片刻后重试，或降低提问频率。"
    if "timed out" in low or "timeout" in low:
        return "模型接口响应超时：可稍后重试，复杂问题也可关闭探索模式。"
    return "模型接口调用失败：" + s[:160]


def _call_llm(conf, messages, tools):
    llm = conf["llm"]
    messages = privacy.redact_messages(conf, messages)
    payload = {"model": _pick_model(conf), "messages": messages,
               "temperature": conf.get("qa", {}).get("temperature", 0.2)}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    last_err = None
    for attempt in range(3):        # Agent 每步都会调它，网络抖一下不能直接废掉整轮
        req = urllib.request.Request(
            llm["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {llm['api_key']}"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read())["choices"][0]["message"]
        except Exception as exc:      # noqa: BLE001
            last_err = exc
            time.sleep(1.0 * (attempt + 1))
    raise last_err


def _stream_llm(conf, messages, tools=None):
    """流式调用，逐段 yield ("delta", text)；结束时 yield ("done", full_text)。"""
    llm = conf["llm"]
    messages = privacy.redact_messages(conf, messages)
    payload = {"model": _pick_model(conf), "messages": messages,
               "temperature": conf.get("qa", {}).get("temperature", 0.2),
               "stream": True}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    full = []
    last_err = None
    for attempt in range(4):        # 1/2/4 秒退避：网络抖一下能扛过去，节点挂了也不会秒失败
        try:
            # 每次重试都新建 Request，避免复用已被底层读取过的请求对象。
            req = urllib.request.Request(
                llm["base_url"].rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {llm['api_key']}",
                         "Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        full.append(piece)
                        yield ("delta", piece)
            last_err = None
            break
        except Exception as exc:      # noqa: BLE001
            last_err = exc
            if full:                  # 已经输出了一部分就不再重试，避免重复
                break
            time.sleep(1.0 * (attempt + 1))
    if last_err is not None and not full:
        # 流式失败 → 退回非流式一次性调用
        try:
            msg = _call_llm(conf, messages, tools)
            text = msg.get("content") or ""
            full.append(text)
            yield ("delta", text)
            last_err = None
        except Exception:
            raise last_err
    yield ("done", "".join(full))


def _platform_text(text: str, platform: str = "wechat") -> str:
    """QQ 模式的人设与术语：同一套工具，措辞换成 QQ 侧说法。"""
    if platform != "qq" or not text:
        return text
    for a, b in (("微信记录分析助手", "QQ 记录分析助手"),
                 ("朋友圈", "QQ空间"), ("公众号/网页", "网页"),
                 ("公众号文章", "网页文章"), ("公众号", "网页")):
        text = text.replace(a, b)
    return text


def _tool_schemas(platform: str = "wechat"):
    out = []
    all_tools = QQ_TOOLS if platform == "qq" else TOOLS
    for name, (fn, desc, params) in all_tools.items():
        out.append({"type": "function",
                    "function": {"name": name,
                                 "description": _platform_text(desc, platform),
                                 "parameters": params}})
    return out


def run(question: str, conf: dict, chat_ctx: str | None = None,
        chat_name: str | None = None, quoted: list[dict] | None = None,
        history: list[dict] | None = None, on_event=None,
        explorer: bool = False, platform: str = "wechat") -> dict:
    """执行 agent 循环，返回 {answer, trace, refs}。

    on_event(dict) 可选，用于实时推送进度（tool_call / tool_result / refs / status）。
    explorer：探索模式——工具步数翻倍（会话内 4→8、全局 6→12），适合统计/枚举/
    跨时段排查这类需要多轮检索的问题。
    """
    def emit(payload):
        if on_event:
            try:
                on_event(payload)
            except Exception:
                pass

    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    time_line = (f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')} 星期{week}"
                 f"（时区本地）。本月 1 号是 {now.replace(day=1).strftime('%Y-%m-%d')}。")
    if explorer:
        budget = 12 if not chat_ctx else 8
        time_line += (
            f"\n【探索模式已开启】本问题工具调用上限提升到 {budget} 轮（普通为"
            f"{'6' if not chat_ctx else '4'}）。枚举/统计/排查类问题请多轮深挖："
            "换 2~4 组互补关键词、对命中密集的日期用 read_chat(since, until) 按天补读、"
            "发现新实体名就追查一轮；宁可多查一步，不要早停。"
            "普通聊天问题该快还是要快，探索模式不是让你每问必查满。")
    base_prompt = QQ_SYSTEM_PROMPT if platform == "qq" else _platform_text(SYSTEM_PROMPT, platform)
    messages = [{"role": "system", "content": base_prompt + chr(10) + time_line}]
    ctx_bits = []
    if chat_ctx:
        if platform == "qq":
            ctx_bits.append(
                f"用户正在浏览 QQ 会话「{chat_name or chat_ctx}」（conv={chat_ctx}）。"
                f"默认先在该会话内回答：调用 search_qq 时把 conv 设为 {chat_ctx}；"
                "只有用户明确要求跨群/全局时才省略 conv。")
            try:
                rc = _qq_recent_block(chat_ctx, limit=80)
                if rc:
                    ctx_bits.append(
                        "【最近 QQ 记录】（该会话最近的真实发言，时间正序，未编号；"
                        "引用具体消息时请用 search_qq 检索取得 ref 编号）：" + chr(10) + rc)
            except Exception:
                pass
        else:
            ctx_bits.append(
                f"用户正在浏览会话「{chat_name or chat_ctx}」（username={chat_ctx}）。"
                "默认在该会话范围内回答，不需要 find_sessions；"
                "只有用户明确要求跨会话/全局时才扩大范围。")
            # 预注入最近记录：常见问题可零检索直接回答
            try:
                rc = _recent_block(chat_ctx, hours=24, limit=60)
                if rc:
                    ctx_bits.append("【最近记录】（该会话最近的真实发言，时间正序）：" + chr(10) + rc)
            except Exception:
                pass
    if quoted:
        parts = []
        for q in quoted[:10]:
            parts.append(f"- id={q.get('id')} 时间={_fmt(q.get('ts'))} "
                         f"发言人={q.get('who','')}: {(q.get('text') or '')[:400]}")
        ctx_bits.append("用户引用了以下消息：\n" + "\n".join(parts))
    if ctx_bits:
        messages.append({"role": "system", "content": "\n".join(ctx_bits)})
    for h in (history or [])[-3:]:
        if h.get("q"):
            messages.append({"role": "user", "content": h["q"]})
        if h.get("a"):
            messages.append({"role": "assistant", "content": h["a"][:1500]})
    messages.append({"role": "user", "content": question})

    trace: list[dict] = []
    seen_msgs: dict[int, dict] = {}
    call_cache: dict[str, dict] = {}
    read_urls: set[str] = set()        # 本轮已读过的链接（自动补正文用，避免重复）

    schemas = _tool_schemas(platform)
    if chat_ctx:
        # 会话已确定：不需要"再找会话"这个工具
        schemas = [x for x in schemas if x["function"]["name"] != "find_sessions"]
    if explorer:
        max_steps = 12 if not chat_ctx else 8
    else:
        max_steps = MAX_STEPS_GLOBAL if not chat_ctx else MAX_STEPS_CHAT
    for step in range(max_steps):
        last_step = step >= max_steps - 1
        msg = _call_llm(conf, messages, None if last_step else schemas)
        calls = msg.get("tool_calls") or []
        raw_content = msg.get("content") or ""
        if not calls and "DSML" in raw_content:
            # 不支持原生 function calling 的模型（deepseek-reasoner/flash 等）
            # 会把调用意图以 DSML 文本输出 → 解析成真调用继续执行
            calls = parse_dsml_calls(raw_content)
        if not calls:
            answer = strip_dsml(raw_content)
            return {"answer": answer, "trace": trace,
                    "refs": _cited_refs(answer, seen_msgs), "steps": step + 1}
        messages.append({"role": "assistant", "content": strip_dsml(raw_content),
                         "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            emit({"type": "tool", "tool": name, "args": args})
            ckey = name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False)
            fn = (QQ_TOOLS if platform == "qq" else TOOLS).get(name, (None,))[0]
            if ckey in call_cache:
                result = call_cache[ckey]
                result["_note"] = "同一调用已执行过，结果见上，请直接用，不要重复检索"
            elif not fn:
                result = {"error": f"未知工具 {name}"}
            else:
                try:
                    result = fn(args)
                except Exception as e:
                    result = {"error": str(e)[:200]}
                call_cache[ckey] = result
            # 收集消息/文件/文章作为可点击引用，并把编号写回结果条目（模型看到 "ref": n，
            # 引用时直接用 [n]，保证点击跳转的目标准确）
            def _reg_msg(item: dict) -> None:
                ent = seen_msgs.get(item["id"])
                if ent is None:
                    ent = {"n": len(seen_msgs) + 1, "id": item["id"],
                           "chat": item.get("chat", ""), "chat_name": item.get("chat_name", ""),
                           "ts": item.get("ts", 0), "sender_name": item.get("who", ""),
                           "snippet": (item.get("text") or item.get("title") or "")[:160]}
                    seen_msgs[item["id"]] = ent
                item["ref"] = ent["n"]

            def _reg_file(item: dict):
                title = (item.get("title") or item.get("name") or "").strip()
                if not title:
                    return None
                fkey = f"file:{item.get('chat','')}:{item.get('local_id',0)}:{title}"
                ent = seen_msgs.get(fkey)
                if ent is None:
                    ent = {"n": len(seen_msgs) + 1, "id": 0, "kind": "file",
                           "chat": item.get("chat", ""),
                           "local_id": item.get("local_id", 0),
                           "ts": item.get("ts", 0),
                            "chat_name": item.get("chat_name") or item.get("chat", ""),
                           "title": title, "size": item.get("size", 0),
                           "snippet": title[:160]}
                    seen_msgs[fkey] = ent
                item["ref"] = ent["n"]
                return ent

            def _reg_link(item: dict) -> None:
                url = item.get("url") or ""
                if not url:
                    return
                ent = seen_msgs.get(url)
                if ent is None:
                    ent = {"n": len(seen_msgs) + 1, "id": 0, "kind": "link", "url": url,
                           "chat": "", "chat_name": item.get("title") or "网页文章",
                           "ts": 0, "sender_name": "公众号/网页",
                           "snippet": (item.get("preview") or item.get("text") or "")[:160]}
                    seen_msgs[url] = ent
                item["ref"] = ent["n"]

            def _reg_moment(item: dict) -> None:
                tid = str(item.get("tid") or "")
                if not tid:
                    return
                ent = seen_msgs.get("moment:" + tid)
                if ent is None:
                    ent = {"n": len(seen_msgs) + 1, "id": 0, "kind": "moment", "tid": tid,
                           "chat": "", "chat_name": item.get("who")
                           or item.get("author_name") or "朋友圈",
                           "ts": item.get("ts", 0), "sender_name": "朋友圈",
                           "snippet": (item.get("text") or "")[:160]}
                    seen_msgs["moment:" + tid] = ent
                item["ref"] = ent["n"]

            for key in ("messages", "files"):
                items = result.get(key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if key == "files":
                        _reg_file(item)
                    elif item.get("id"):
                        _reg_msg(item)
            for item in (result.get("moments") if isinstance(result.get("moments"), list) else []):
                if isinstance(item, dict):
                    _reg_moment(item)
            # get_moment 读过的朋友圈本身也是一条可点击引用
            if name == "get_moment" and "error" not in result:
                _reg_moment(result)
            links = result.get("links")
            for item in (links if isinstance(links, list) else []):
                if isinstance(item, dict):
                    _reg_link(item)
            # read_file 读过的文件本身也是一条可点击引用
            if name == "read_file" and "error" not in result:
                ent = _reg_file({"chat": args.get("chat", ""),
                                 "local_id": args.get("local_id", 0),
                                 "ts": args.get("ts", 0),
                                 "title": result.get("name") or args.get("name", ""),
                                 "size": result.get("size", 0)})
                if ent:
                    result["ref"] = ent["n"]
            _links = result.get("links")
            for item in (_links if isinstance(_links, list) else []):
                url = item.get("url") or ""
                if url and url not in seen_msgs:
                    seen_msgs[url] = {
                        "n": len(seen_msgs) + 1, "id": 0, "kind": "link", "url": url,
                        "chat": "", "chat_name": item.get("title") or "网页文章",
                        "ts": 0, "sender_name": "公众号/网页",
                        "snippet": (item.get("preview") or item.get("text") or "")[:160]}

            # 确定性补正文：检索结果里的链接自动读取前几条（含小红书 OCR / B站转写），
            # 避免模型只看标题就总结。每轮最多 3 条，单条最多等 30 秒；读过的会缓存。
            if isinstance(_links, list) and _links:
                import link_media
                contents, budget = [], 3
                for item in _links[:3]:
                    if budget <= 0:
                        break
                    u = item.get("url") or ""
                    if not u or u in read_urls:
                        continue
                    if link_media.detect(u) == "generic" and not item.get("indexed"):
                        # 普通网页也有缓存价值；平台链接优先
                        pass
                    read_urls.add(u)
                    budget -= 1
                    emit({"type": "tool", "tool": "read_link",
                          "args": {"url": u, "auto": True}})
                    dd = _read_link_blocking(u, wait=30)
                    if dd.get("pending") or dd.get("error"):
                        item["read_error"] = dd.get("error") or "仍在解析"
                        continue
                    item["chars"] = dd.get("chars", 0)
                    item["platform"] = dd.get("platform", "")
                    contents.append({"url": u, "title": dd.get("title", ""),
                                     "chars": dd.get("chars", 0),
                                     "text": (dd.get("text") or "")[:6000]})
                if contents:
                    result["link_contents"] = contents   # 直接给模型看
            summary = _summarize(name, result)
            trace.append({"tool": name, "args": args, "summary": summary})
            emit({"type": "tool_result", "tool": name, "summary": summary,
                  "refs": len(seen_msgs)})
            # rag/通读类工具结果自带上下文、体量大，放宽截断（默认 6000 会把附带的
            # 前后文整段切掉，模型反而看不到簇）
            tool_cap = 12000 if name in ("rag_search", "read_chat", "recent_messages") else 6000
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": "[第" + str(step + 1) + "/" + str(max_steps)
                                        + "轮] "
                                        + json.dumps(result, ensure_ascii=False)[:tool_cap]})
    # 步数用尽：禁用工具，要求模型基于已有信息给出最终答案
    try:
        messages.append({"role": "user", "content":
                         "工具调用次数已达上限。请立刻基于上面已经获得的信息给出最终回答；"
                         "信息不足就说明缺什么。不要再请求调用工具。"})
        final = _call_llm(conf, messages, None)
        answer = strip_dsml(final.get("content") or "").strip()
    except Exception:
        answer = ""
    if not answer:
        answer = "（信息较多，已附上检索到的相关消息，可展开引用查看）"
    return {"answer": answer, "trace": trace,
            "refs": _cited_refs(answer, seen_msgs), "steps": max_steps}


def _summarize(name: str, result: dict) -> str:
    if "error" in result:
        return f"失败：{result['error']}"
    if name == "find_sessions":
        items = result.get("sessions") or []
        return f"匹配 {len(items)} 个会话：" + "、".join(
            f"{x['name']}({x['message_count']}条)" for x in items[:5])
    if name == "find_contacts":
        items = result.get("contacts") or []
        return f"匹配 {len(items)} 个联系人：" + "、".join(x["name"] for x in items[:6])
    if name == "search_messages":
        return f"命中 {result.get('count', 0)} 条消息"
    if name == "read_chat":
        return f"读取 {result.get('count', 0)} 条消息（{result.get('chat_name','')}）"
    if name == "get_message_context":
        return f"上下文 {len(result.get('messages') or [])} 条"
    if name == "rag_search":
        c = result.get("counts") or {}
        return (f"检索{result.get('scope')}：消息 {c.get('messages',0)} 条 / "
                f"文章 {c.get('links',0)} 篇 / 文件 {c.get('files',0)} 个 / "
                f"朋友圈 {c.get('moments',0)} 条")
    if name == "recent_messages":
        return f"最近 {result.get('count', 0)} 条消息"
    if name == "list_chat_links":
        return f"会话内链接 {result.get('count', 0)} 条"
    if name == "find_links":
        return f"文章命中 {result.get('count', 0)} 篇"
    if name == "search_moments":
        return f"命中 {result.get('count', 0)} 条朋友圈"
    if name == "get_moment":
        n_media = len(result.get("media") or [])
        return (f"读取朋友圈（{result.get('who', '')}）：{n_media} 个媒体，"
                f"{result.get('n_comments', 0)} 条评论 / {result.get('n_likes', 0)} 赞")
    if name == "list_files":
        return f"文件 {len(result.get('files') or [])} 个"
    if name == "read_file":
        return f"读取《{result.get('name','')}》{result.get('chars',0)} 字"
    if name == "overview_stats":
        return f"群 {result.get('groups')} 个 / 私聊 {result.get('private_chats')} 个 / "                f"消息 {result.get('messages')} 条"
    if name == "chat_stats":
        return f"共 {result.get('total',0)} 条，{result.get('from','')} ~ {result.get('to','')}"
    if name == "get_current_time":
        return f"{result.get('now')} {result.get('weekday')}"
    return "完成"
