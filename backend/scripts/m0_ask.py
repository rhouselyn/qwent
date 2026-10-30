# -*- coding: utf-8 -*-
"""M0-③ 问答 demo：检索 → 组装上下文 → 云端 LLM（OpenAI 兼容接口）。

用法：
  python m0_ask.py "产品群最终定的上线时间是什么时候？" [--chat 产品群]
API 配置在 backend/config.json（首次运行生成模板），支持任意 OpenAI 兼容服务
（DeepSeek / GLM / Kimi / SiliconFlow 等）。未配置 API Key 时自动退化为"仅检索"模式，
打印将发给 LLM 的上下文，方便验证检索质量。
"""
import argparse
import json
import os
import sqlite3
import time
import urllib.request

CONF = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config.json"))

DEFAULT_CONF = {
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat"
    },
    "embedding": {
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "model": "BAAI/bge-m3"
    },
    "qa": {"max_context_messages": 30, "temperature": 0.3}
}


def load_conf():
    if not os.path.exists(CONF):
        with open(CONF, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONF, f, ensure_ascii=False, indent=2)
        print(f"[!] 已生成配置模板 {CONF}，请填入 llm.api_key 后重跑（当前为仅检索模式）")
    with open(CONF, encoding="utf-8") as f:
        return json.load(f)


def retrieve(con, query: str, chat: str | None, k: int = 30):
    """混合召回：整句 + 滑动 3-gram（trigram 索引）+ 短词 LIKE。"""
    chat_cond = ""
    chat_params: list = []
    if chat:
        chat_cond = "AND m.chat_user IN (SELECT username FROM chat WHERE display_name LIKE ?)"
        chat_params = [f"%{chat}%"]

    def fts_query(terms):
        return " OR ".join(f'"{t}"' for t in terms if len(t) >= 3)

    words = [w for w in query.split() if w] or [query]
    terms: list[str] = []
    for w in words:
        if len(w) >= 3:
            terms.append(w)
        if len(w) > 3:  # 自然语言整句：滑窗 3-gram 提升召回
            grams = [w[i:i + 3] for i in range(min(len(w) - 2, 12))]
            terms.extend(grams)
    all_rows: dict = {}
    if terms:
        q = fts_query(terms)
        try:
            rows = con.execute(f"""
                SELECT m.id, m.chat_user, m.is_sender, m.sender, m.ts, m.type, m.content
                FROM message_fts f JOIN message m ON m.id = f.rowid
                WHERE message_fts MATCH ? {chat_cond}
                ORDER BY bm25(message_fts) LIMIT ?""", [q] + chat_params + [k]).fetchall()
            for r in rows:
                all_rows[r[0]] = r
        except sqlite3.OperationalError:
            pass
    short = [w for w in words if 0 < len(w) < 3]
    if short:
        lconds = " AND ".join("m.content LIKE ?" for _ in short)
        rows = con.execute(f"""
            SELECT m.id, m.chat_user, m.is_sender, m.sender, m.ts, m.type, m.content
            FROM message m WHERE {lconds} {chat_cond}
            ORDER BY ts DESC LIMIT ?""", [f"%{w}%" for w in short] + chat_params + [k]).fetchall()
        for r in rows:
            all_rows[r[0]] = r
    return sorted(all_rows.values(), key=lambda r: -r[4])[:k]


def build_context(con, rows):
    def disp(u):
        r = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat WHERE username=?",
                        (u,)).fetchone()
        return r[0] if r else u

    def who(chat_user, sender, is_sender):
        if is_sender == 1 or not sender:
            return "我"
        r = con.execute("SELECT COALESCE(remark, nick_name, username) FROM contact "
                        "WHERE username=?", (sender,)).fetchone()
        return r[0] if r else (sender or "??")

    lines = []
    for mid, chat_user, is_sender, sender, ts, typ, content in rows:
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts or 0))
        lines.append(f"[{disp(chat_user)}] {t} {who(chat_user, sender, is_sender)}: "
                     f"{content[:500]}")
    return "\n".join(lines), lines


def call_llm(conf, question: str, context: str) -> str:
    llm = conf["llm"]
    sys_prompt = (
        "你是用户的个人微信记录助手。仅依据给出的聊天记录上下文回答问题；"
        "引用信息时标注[会话名 时间 发言人]；上下文没有答案就直说没找到，不要编造。")
    body = json.dumps({
        "model": llm["model"],
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"聊天记录上下文：\n{context}\n\n问题：{question}"},
        ],
        "temperature": conf["qa"]["temperature"],
    }).encode()
    req = urllib.request.Request(
        llm["base_url"].rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {llm['api_key']}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--chat", default=None)
    args = ap.parse_args()
    conf = load_conf()

    db = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data",
                                      "knowledge.db"))
    con = sqlite3.connect(db)
    rows = retrieve(con, args.question, args.chat,
                    conf["qa"]["max_context_messages"])
    if not rows:
        print("[-] 检索无结果，无法回答")
        return
    context, lines = build_context(con, rows)
    print(f"[*] 召回 {len(lines)} 条消息（时间跨度 "
          f"{time.strftime('%m-%d', time.localtime(rows[-1][4]))} ~ "
          f"{time.strftime('%m-%d', time.localtime(rows[0][4]))}）")

    if conf["llm"]["api_key"]:
        print("[*] 调用 LLM...")
        answer = call_llm(conf, args.question, context)
        print("\n=== AI 回答 ===\n" + answer)
    else:
        print("\n=== 仅检索模式（未配置 llm.api_key），以下为将发给 LLM 的上下文 ===\n")
        print(context)


if __name__ == "__main__":
    main()
