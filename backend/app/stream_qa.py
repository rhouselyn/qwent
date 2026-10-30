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
                    "第一", "最多", "最少", "对比", "归纳", "整理", "总结", "时间线")

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


def _need_tools(question: str, hits: int) -> bool:
    q = question.lower()
    if hits < 2:
        return True
    return any(k in q for k in TOOLISH_KEYWORDS)


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

            # ---- 指定文件问答：直接读文件正文 ----
            if fdoc:
                path = deps["locate_file"](fdoc.get("chat", ""), int(fdoc.get("local_id", 0)),
                                           int(fdoc.get("ts", 0)), fdoc.get("name", ""))
                if not path:
                    yield _sse({"type": "error", "message": "文件不在本地磁盘"})
                    return
                yield _sse({"type": "status", "text": "正在读取文件正文…"})
                text = deps["extract_text"](path, 24000)
                crow = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) "
                                   "FROM chat WHERE username=?",
                                   (fdoc.get("chat", ""),)).fetchone()
                cname = crow[0] if crow else fdoc.get("chat", "")
                refs = [{"n": 1, "id": 0, "kind": "file", "url": "",
                         "chat": fdoc.get("chat", ""), "chat_name": cname,
                         "local_id": int(fdoc.get("local_id", 0) or 0),
                         "ts": int(fdoc.get("ts", 0) or 0),
                         "title": path.name, "size": path.stat().st_size,
                         "sender_name": "文件", "snippet": path.name, "ctx": []}]
                yield _sse({"type": "refs", "refs": refs})
                yield from _answer(conf, deps, q, "", [f"文件《{path.name}》正文：\n{text}"],
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
            if chat and conf["llm"].get("api_key"):
                import agent as agent_mod
                rrefs: list = []
                rc = None
                try:
                    rc, rrefs = agent_mod._recent_block_numbered(chat, hours=hours, limit=60)
                except Exception:
                    rc, rrefs = None, []
                if rc:
                    yield _sse({"type": "refs", "refs": rrefs})   # 先发引用，流式期间即可点击
                    yield _sse({"type": "status", "text": "读取最近记录并作答…"})
                    cname = (con.execute("SELECT COALESCE(NULLIF(display_name,''), username) "
                                         "FROM chat WHERE username=?", (chat,)).fetchone()
                             or [chat])[0]
                    msgs = [
                        {"role": "system", "content":
                         "你是微信记录助手。系统会给出该会话最近的真实发言，每行行首 [n] 是引用编号。"
                         "如果这些记录足以回答：用简洁 Markdown 作答，提到具体消息时标注对应的 [n]；"
                         "**不要输出「引用来源」清单**（界面会自动把 [n] 渲染成可点击引用）。"
                         "如果不足（例如问题是关于更早的时间、需要统计、需要文件或文章），"
                         "只输出一行 `__NEED_TOOLS__`，不要编造答案。"},
                        {"role": "system", "content": f"当前时间：{_now_text()}；"
                                                      f"会话：{cname}"},
                        {"role": "user", "content":
                         "最近记录：" + chr(10) + rc + chr(10) + chr(10) + "问题：" + q},
                    ]
                    acc = []
                    need_tools = False
                    for kind, val in agent_mod._stream_llm(conf, msgs):
                        if kind != "delta":
                            continue
                        acc.append(val)
                        head = "".join(acc)
                        if "__NEED_TOOLS__" in head:
                            need_tools = True
                            break
                        if len(head) > 30:      # 已明确在作答，直接流式输出
                            yield _sse({"type": "delta", "text": val})
                    if not need_tools:
                        answer = strip_ref_footer(agent_mod.strip_dsml("".join(acc)))
                        sid = deps["save_qa"](con, scope_key, q, answer, rrefs, "fast",
                                              None, session_id, new_session, scope_name)
                        yield _sse({"type": "done", "session_id": sid, "mode": "fast"})
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
                        quoted=qn, history=history, on_event=on_event))
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
                yield _sse({"type": "error", "message": result_holder["error"]})
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
