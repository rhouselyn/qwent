# -*- coding: utf-8 -*-
"""流式问答接口（SSE over POST）：先快速检索+流式回答，检索不足时才走 Agent 工具。

速度策略：
  1) 会话内提问默认只看「最近 N 小时」（默认 24h），一次检索即可，绝大多数问题
     命中后直接流式作答（1 次 LLM 调用）。
  2) 会话内 hit 不足 → 扩大到全部历史再检索一次。
  3) 仍然不足或问题明显需要工具（统计/文件/公众号/排行等）→ 交给 Agent。
事件：
  status     正在做什么
  question   回显问题（前端已在本地显示，这里用于多端同步）
  refs       引用列表（含上下文）
  tool       工具开始调用
  tool_result 工具返回摘要
  delta      回答增量文本
  done       结束（含 session_id / mode / 完整回答）
  error      错误
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

TOOLISH_KEYWORDS = ("一共", "总共", "多少", "统计", "排行", "排第", "谁最", "最活跃",
                    "文件", "文档", "pdf", "excel", "word", "公众号", "文章", "链接",
                    "推文", "群成员", "拉群", "加群", "多少个", "几号", "哪天",
                    "第一", "最多", "最少", "对比", "归纳", "整理", "总结", "时间线",
                    # 列举/查找类：最近 24h 记录根本答不了，必须靠全库检索
                    "哪些", "有哪些", "提到", "说过什么", "找出", "找一下", "找找",
                    "搜索", "查一下", "查查", "列举", "清单")

# 模型在答案末尾自带的「引用来源：[1]…」清单——正文 [n] 本身就是可点击链接，直接剥掉
_REF_FOOTER_RE = re.compile(
    r"\n+[ \t]*(?:\*\*)?(?:引用来源|参考来源|参考资料|参考消息|出处|来源)(?:\*\*)?"
    r"[ \t]*[:：]?[ \t]*\n(?:[ \t]*(?:[-*+][ \t]*)?\[\d{1,3}\][^\n]*\n?)*\s*$")


def strip_ref_footer(text: str) -> str:
    t = str(text or "")
    for _ in range(3):
        m = _REF_FOOTER_RE.search(t)
        if not m:
            break
        if not t[:m.start()].strip():
            break
        t = t[:m.start()]
    return t.rstrip()


def _sse(event: dict) -> bytes:
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")


def _need_tools(question: str) -> bool:
    """列举/查找/统计类问题必须走 Agent 全库检索：最近记录试都不用试，
    否则模型会拿 24h 窗口回答「没有提到」这种错误结论。"""
    q = (question or "").lower()
    return any(k in q for k in TOOLISH_KEYWORDS)


def _link_excerpts(con, chat: str, hours: int, start_n: int = 1,
                   max_links: int = 2, max_chars: int = 1000):
    """最近窗口内已解析的文章，返回 {"text": 带编号摘录行, "refs": 引用列表}，无则 None。

    start_n：编号起点，必须接在最近记录的 [n] 之后，正文行与 refs 编号才一致。
    只取 link_cache 已有正文的（纯缓存读，零抓取开销，不拖慢快速回答）；
    文件/PDF 不塞——内容长、按题抽取慢，需要时由 Agent 的 read_file 读取。
    """
    try:
        since = int(time.time()) - hours * 3600
        rows = con.execute(
            "SELECT l.url, l.title, c.text, l.msg_ts FROM chat_link l "
            "JOIN link_cache c ON c.url = l.url "
            "WHERE l.chat=? AND l.msg_ts>=? AND l.state=1 AND IFNULL(c.text,'')<>'' "
            "ORDER BY l.msg_ts DESC LIMIT ?", (chat, since, max_links)).fetchall()
    except Exception:
        return None
    out, refs = [], []
    for url, title, text, ts in rows:
        title = (title or url)[:80]
        body = (text or "").strip().replace(chr(10), " ")[:max_chars]
        if not body:
            continue
        n = start_n + len(refs)
        out.append(f"[{n}] 《{title}》正文摘录：{body}…")
        refs.append({"n": n, "id": 0, "kind": "link", "url": url,
                     "chat": chat, "chat_name": title, "ts": int(ts or 0),
                     "sender_name": "文章", "snippet": f"《{title}》", "ctx": []})
    if not out:
        return None
    return {"text": chr(10).join(out), "refs": refs}


def _file_excerpts(con, chat: str, hours: int, start_n: int, deps: dict,
                   max_files: int = 2, max_chars: int = 1000):
    """最近窗口内的文件（appmsg 附件），返回 {"text": 带编号摘录行, "refs": 引用列表}。

    **只吃 file_text 预热缓存**（warm_file_texts 随实时同步"收到即提取"），
    不现场抽取——PDF/Office 解析可能好几秒，会拖住"读取最近记录并作答"。
    缓存没有的文件跳过，需要时由 Agent 的 read_file 按需读取。
    """
    from textutil import appmsg_subtype
    try:
        since = int(time.time()) - hours * 3600
        rows = con.execute(
            "SELECT id, src_local_id, ts, content FROM message "
            "WHERE chat_user=? AND ts>=? AND noise=0 AND (type & 0xFFFFFFFF)=49 "
            "AND src_local_id IS NOT NULL ORDER BY ts DESC LIMIT 40",
            (chat, since)).fetchall()
    except Exception:
        return None
    out, refs = [], []
    for r in rows:
        if len(refs) >= max_files:
            break
        try:
            if appmsg_subtype(r["content"] or "") != "6":
                continue
            m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                          r["content"] or "", re.S)
            name = (m.group(1).strip() if m else "") or "文件"
            hit = con.execute("SELECT name, text FROM file_text WHERE key=?",
                              (f"{chat}:{int(r['src_local_id'] or 0)}",)).fetchone()
            if not hit:
                continue
            cname, text = (hit[0] or name), (hit[1] or "").strip()
            if not text or text.startswith("[解析失败"):
                continue
        except Exception:
            continue
        n = start_n + len(refs)
        out.append(f"[{n}] 文件《{cname}》内容摘录：{text[:max_chars]}…")
        refs.append({"n": n, "id": r["id"], "kind": "file",
                     "chat": chat, "chat_name": cname,
                     "local_id": int(r["src_local_id"] or 0), "ts": int(r["ts"] or 0),
                     "title": cname, "sender_name": "文件", "snippet": f"文件《{cname}》",
                     "ctx": []})
    if not out:
        return None
    return {"text": chr(10).join(out), "refs": refs}



def make_stream(payload: dict, deps: dict):
    """deps 由 main.py 注入（避免循环 import）。"""
    q = (payload.get("question") or "").strip()
    chat = payload.get("chat") or None
    session_id = payload.get("session_id")
    new_session = bool(payload.get("new_session"))
    hours = int(payload.get("hours") or deps["conf"]().get("qa", {}).get("recent_hours", 24))
    use_agent_ok = payload.get("agent", True)
    history = payload.get("history") or []
    thinking = payload.get("thinking")
    fdoc = payload.get("file") or None
    ldoc = payload.get("link") or None
    quoted = payload.get("quoted") or []
    explorer = bool(payload.get("explorer"))   # 探索模式：跳过快速回答，工具步数翻倍
    # 作用域键：不同群聊 / 文件 / 链接各自独立会话，绝不与其他上下文混用
    #   assistant / <chat username> / file:<chat>:<local_id> / link:<hash>
    scope_key = payload.get("scope") or chat or "assistant"
    scope_name = payload.get("scope_name") or ""

    conf = deps["conf"]()
    if thinking is not None:
        conf["llm"] = dict(conf["llm"], thinking=bool(thinking))

    def gen():
        con = deps["db"]()
        try:
            if not q:
                yield _sse({"type": "error", "message": "空问题"})
                return
            yield _sse({"type": "question", "question": q})
            yield _sse({"type": "status", "text": "正在检索聊天记录…"})

            # ---- 针对当前打开的链接/文章提问：直接用解析正文作答 ----
            if ldoc and not fdoc:
                url = ldoc.get("url") or ""
                if url:
                    yield _sse({"type": "status", "text": "正在读取文章正文…"})
                    d = deps["fetch_article"](url)
                    if d.get("pending"):
                        yield _sse({"type": "error",
                                    "message": "这篇文章/视频还在解析中，请稍等几秒再问"})
                        return
                    if d.get("error"):
                        yield _sse({"type": "error", "message": d["error"]})
                        return
                    refs = [{"n": 1, "id": 0, "kind": "link", "url": url,
                             "chat": "", "chat_name": d.get("title") or ldoc.get("title") or "网页",
                             "ts": 0, "sender_name": d.get("platform") or "网页",
                             "snippet": d.get("title") or "", "ctx": []}]
                    yield _sse({"type": "refs", "refs": refs})
                    body = "《%s》正文：\n%s" % (d.get("title") or "", (d.get("text") or "")[:26000])
                    yield from _answer(conf, deps, q, "", [body],
                                       refs, history, new_session, session_id, scope_key, "link",
                                       scope_name=scope_name)
                    return

            # ---- 指定文件问答：直接读文件正文（走 file_text 缓存，重复提问秒回） ----
            if fdoc:
                getter = deps.get("file_text_cached")
                if getter:
                    p, fname, text = getter(con, fdoc.get("chat", ""),
                                            int(fdoc.get("local_id", 0) or 0),
                                            int(fdoc.get("ts", 0) or 0), fdoc.get("name", ""))
                else:
                    p = deps["locate_file"](fdoc.get("chat", ""), int(fdoc.get("local_id", 0) or 0),
                                            int(fdoc.get("ts", 0) or 0), fdoc.get("name", ""))
                    fname = p.name if p else (fdoc.get("name") or "文件")
                    text = deps["extract_text"](p, 24000) if p else ""
                if not (text or "").strip():
                    yield _sse({"type": "error", "message": "文件不在本地磁盘"})
                    return
                yield _sse({"type": "status", "text": "正在读取文件正文…"})
                crow = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) "
                                   "FROM chat WHERE username=?",
                                   (fdoc.get("chat", ""),)).fetchone()
                cname = crow[0] if crow else fdoc.get("chat", "")
                refs = [{"n": 1, "id": 0, "kind": "file", "url": "",
                         "chat": fdoc.get("chat", ""), "chat_name": cname,
                         "local_id": int(fdoc.get("local_id", 0) or 0),
                         "ts": int(fdoc.get("ts", 0) or 0),
                         "title": fname, "size": (p.stat().st_size if p else 0),
                         "sender_name": "文件", "snippet": fname, "ctx": []}]
                yield _sse({"type": "refs", "refs": refs})
                yield from _answer(conf, deps, q, "", [f"文件《{fname}》正文：\n{text}"],
                                  [], history, new_session, session_id, scope_key, "file",
                                  scope_name=scope_name)
                return

            # ---- 统一走 Agent：检索范围/关键词由模型自己决定 ----
            # 会话内提问时，Agent 已预注入【最近记录】，常见问题一步即可作答；
            # 全局提问时 Agent 自行调用 rag_search(scope=...) 并自造关键词。
            if not conf["llm"].get("api_key"):
                # 无 Key：退化为一次性检索 + 原文展示
                since = int(time.time()) - hours * 3600 if (chat and hours) else None
                hits, refs = deps["quick_retrieve"](con, q, chat, since, hours=hours)
                yield _sse({"type": "refs", "refs": refs})
                body = "**[检索模式：未配置 API Key]**" + chr(10) + chr(10) + (chr(10) + chr(10)).join(
                    r.get("text", "")[:600] for r in refs[:4])
                sid = deps["save_qa"](con, scope_key, q, body, refs, "retrieval", None,
                                      session_id, new_session, scope_name)
                yield _sse({"type": "delta", "text": body})
                yield _sse({"type": "done", "session_id": sid, "mode": "retrieval"})
                return

            # ---- 阶段一：会话内先用「最近记录」做一次快速流式作答 ----
            # 列举/查找/统计类问题（_need_tools）跳过快速路径：最近记录不足以回答，
            # 试探只会得到「没有提到」的错误答案，直接让 Agent 全库检索。
            # 探索模式（explorer）同样跳过：用户明确要多轮深挖
            if chat and conf["llm"].get("api_key") and not _need_tools(q) and not explorer:
                import agent as agent_mod
                rrefs: list = []
                rc = None
                try:
                    rc, rrefs = agent_mod._recent_block_numbered(chat, hours=hours, limit=60)
                except Exception:
                    rc, rrefs = None, []
                # 最近窗口里已解析的文章 + 文件：正文摘录一并给模型（各 ≤2 份 × 1000 字），
                # 编号接在最近记录之后，点击 [n] 打开文章/文件预览。
                # 起点必须取现有最大编号+1（_recent_block_numbered 先编号后切片，最大 n 可能大于条数）
                next_n = max((r["n"] for r in rrefs), default=0) + 1
                lblock = _link_excerpts(con, chat, hours, start_n=next_n)
                if lblock:
                    rrefs = rrefs + lblock["refs"]
                    next_n += len(lblock["refs"])
                try:
                    fblock = _file_excerpts(con, chat, hours, next_n, deps)
                except Exception:
                    fblock = None
                if fblock:
                    rrefs = rrefs + fblock["refs"]
                if rc:
                    yield _sse({"type": "refs", "refs": rrefs})   # 先发引用，流式期间即可点击
                    # 最近记录（毫秒级）此时已读完；这一阶段实际是等模型首字，
                    # 慢在 LLM API 的排队/网络，不是本地读取
                    yield _sse({"type": "status", "text": "正在生成回答…"})
                    cname = (con.execute("SELECT COALESCE(NULLIF(display_name,''), username) "
                                         "FROM chat WHERE username=?", (chat,)).fetchone()
                             or [chat])[0]
                    extra = ""
                    if lblock:
                        extra += (chr(10) + chr(10) + "最近分享的文章（已解析，正文摘录）："
                                 + chr(10) + lblock["text"])
                    if fblock:
                        extra += (chr(10) + chr(10) + "最近分享的文件（内容摘录）："
                                 + chr(10) + fblock["text"])
                    msgs = [
                        {"role": "system", "content":
                         "你是微信记录助手。系统会给出该会话最近的真实发言（含图片OCR/语音视频转写），"
                         "每行行首 [n] 是引用编号"
                         "（可能还附有已解析文章/文件的内容摘录，同样有编号，可引用）。"
                         "如果这些材料足以回答：用简洁 Markdown 作答，提到具体消息/文章/文件时标注对应的 [n]；"
                         "**不要输出「引用来源」清单**（界面会自动把 [n] 渲染成可点击引用）。"
                         "**文章/文件的摘录只有开头约 1000 字**：读完已够就直接回答；"
                         "如果材料不足以回答——最近记录里没出现问的东西、问的事发生在更早、"
                         "需要按关键词搜索、需要统计——"
                         "**必须只输出一行 `__NEED_TOOLS__`，由检索工具去全库（含更早记录和媒体文字）找。"
                         "绝对不要凭最近记录回答「没有提到/没有找到」——你没有检索过全库，"
                         "那不是你能下的结论**。"
                         "列举类问题（问有哪些）只能基于最近记录作答时，"
                         "注明「基于最近记录，更早内容未检索」。"},
                        {"role": "system", "content": f"当前时间：{_now_text()}；"
                                                      f"会话：{cname}"},
                        {"role": "user", "content":
                         "最近记录：" + chr(10) + rc + extra + chr(10) + chr(10) + "问题：" + q},
                    ]
                    acc = []
                    need_tools = False
                    started = False
                    try:
                        for kind, val in agent_mod._stream_llm(conf, msgs):
                            if kind != "delta":
                                continue
                            acc.append(val)
                            head = "".join(acc)
                            if "__NEED_TOOLS__" in head:
                                need_tools = True
                                break
                            if len(head) > 30:      # 已明确在作答，直接流式输出
                                # 首段必须补发全部已累积文本：否则前 30 字从未下发，客户端开头缺失
                                yield _sse({"type": "delta", "text": head if not started else val})
                                started = True
                    except Exception as exc:      # noqa: BLE001
                        yield _sse({"type": "error",
                                    "message": agent_mod.friendly_err(exc)})
                        return
                    if not need_tools:
                        answer = strip_ref_footer(agent_mod.strip_dsml("".join(acc)))
                        sid = deps["save_qa"](con, scope_key, q, answer, rrefs, "fast",
                                              None, session_id, new_session, scope_name)
                        # done 带完整答案：客户端以其为准，短答案/丢增量也保证显示完整
                        yield _sse({"type": "done", "session_id": sid, "mode": "fast",
                                    "answer": answer})
                        return
                    yield _sse({"type": "reset"})     # 丢弃试探性输出
                    yield _sse({"type": "status", "text": "最近记录不足，开始检索…"})

            yield _sse({"type": "status", "text": "正在规划检索范围与关键词…"})
            events: list[dict] = []

            def on_event(ev):
                events.append(ev)

            import agent as agent_mod
            qname = None
            if chat:
                row = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) "
                                  "FROM chat WHERE username=?", (chat,)).fetchone()
                qname = row[0] if row else chat
            qn = []
            for qid in quoted[:10]:
                r = con.execute("SELECT * FROM message WHERE id=?", (int(qid),)).fetchone()
                if r:
                    b = agent_mod._msg_brief(con, r)
                    qn.append({"id": b["id"], "ts": b["ts"], "who": b["who"],
                               "text": b["text"]})
            import threading
            result_holder: dict = {}

            def worker():
                try:
                    result_holder.update(agent_mod.run(
                        q, conf, chat_ctx=chat, chat_name=qname,
                        quoted=qn, history=history, on_event=on_event,
                        explorer=explorer))
                except Exception as exc:      # noqa: BLE001
                    result_holder["error"] = str(exc)

            th = threading.Thread(target=worker, daemon=True)
            th.start()
            sent = 0
            while th.is_alive() or sent < len(events):
                while sent < len(events):
                    ev = events[sent]
                    sent += 1
                    if ev.get("type") == "tool":
                        yield _sse({"type": "tool", "tool": ev.get("tool")})
                    elif ev.get("type") == "tool_result":
                        yield _sse({"type": "tool_result", "tool": ev.get("tool"),
                                    "summary": ev.get("summary", ""),
                                    "refs": ev.get("refs", 0)})
                time.sleep(0.05)

            if result_holder.get("error"):
                yield _sse({"type": "error",
                            "message": agent_mod.friendly_err(result_holder["error"])})
                return
            answer = strip_ref_footer(result_holder.get("answer") or "")
            trefs = result_holder.get("refs") or []
            yield _sse({"type": "refs", "refs": trefs})
            sid = deps["save_qa"](con, scope_key, q, answer, trefs, "agent",
                                  result_holder.get("trace"), session_id, new_session,
                                  scope_name)
            # 非流式产出也逐段吐出，保持"打字机"观感
            step = 60
            for i in range(0, len(answer), step):
                yield _sse({"type": "delta", "text": answer[i:i + step]})
            yield _sse({"type": "done", "session_id": sid, "mode": "agent",
                        "refs_count": len(trefs)})
            return
        finally:
            try:
                con.close()
            except Exception:
                pass

    return gen()


def _answer(conf, deps, question, chat, ctx_lines, refs, history, new_session, session_id,
            chat_key, mode, scope_name=None):
    """流式生成回答并落库。"""
    import agent as agent_mod
    con = deps["db"]()
    answer_parts: list[str] = []
    if not conf["llm"].get("api_key"):
        body = "**[检索模式：未配置 API Key]**\n\n" + "\n\n".join(c[:500] for c in ctx_lines[:5])
        yield _sse({"type": "delta", "text": body})
        sid = deps["save_qa"](con, chat_key, question, body, refs, "retrieval", None,
                              session_id, new_session, scope_name)
        yield _sse({"type": "done", "session_id": sid, "mode": "retrieval"})
        con.close()
        return

    scope_txt = "当前会话" if chat else "全部聊天记录"
    now_txt = agent_mod.SYSTEM_PROMPT.splitlines()[0]
    sys_content = ("你是用户的微信记录助手。仅依据给出的聊天记录上下文回答；"
                   "引用时标注 [编号]；**不要在答案末尾输出「引用来源」清单**"
                   "（正文里的 [n] 会被界面渲染成可点击引用）；"
                   "没有答案就直说没找到，不要编造。"
                   "用简洁的 Markdown（列表/表格）组织答案。")
    msgs = [{"role": "system", "content": sys_content},
            {"role": "system", "content": f"当前时间：{_now_text()}；检索范围：{scope_txt}"}]
    for h in (history or [])[-3:]:
        if h.get("q"):
            msgs.append({"role": "user", "content": h["q"]})
        if h.get("a"):
            msgs.append({"role": "assistant", "content": h["a"][:1500]})
    numbered = []
    for i, c in enumerate(ctx_lines, 1):
        numbered.append(f"[{i}] {c[:800]}")
    msgs.append({"role": "user", "content":
                 "聊天记录上下文：\n" + "\n".join(numbered) +
                 f"\n\n问题：{question}"})
    try:
        for kind, val in agent_mod._stream_llm(conf, msgs):
            if kind == "delta":
                answer_parts.append(val)
                yield _sse({"type": "delta", "text": val})
    except Exception as exc:      # noqa: BLE001
        yield _sse({"type": "error", "message": f"模型调用失败：{exc}"})
    full = "".join(answer_parts)
    sid = deps["save_qa"](con, chat_key, question, full, refs, mode, None,
                          session_id, new_session, scope_name)
    yield _sse({"type": "done", "session_id": sid, "mode": mode})
    con.close()


def _now_text() -> str:
    from datetime import datetime
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} 星期{week}"
