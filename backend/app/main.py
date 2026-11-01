# -*- coding: utf-8 -*-
"""一期后端：微信镜像 UI 的全部 API。

运行：python server.py  （默认 http://127.0.0.1:8721）
"""
from __future__ import annotations

import html
import json
import os
import re
import queue
import sqlite3
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

PROJECT = Path(__file__).resolve().parent.parent.parent   # C:/Users/14815/Projects/wechat
CHATTRACE_SRC = PROJECT / "refs" / "ChatTrace" / "src"
DATA = PROJECT / "data"
KNOWLEDGE_DB = DATA / "knowledge.db"
CONFIG = PROJECT / "backend" / "config.json"
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

from paths import ACCOUNT_DIR, ACCOUNT_ID, DECRYPTED_DIR  # noqa: E402 自动定位微信账号目录

if CHATTRACE_SRC.is_dir():
    sys.path.insert(0, str(CHATTRACE_SRC))

from chattrace.service.media import MediaService, decode_dat  # noqa: E402
from textutil import display_text as clean_display, search_text as clean_search  # noqa: E402
import vector  # noqa: E402
import agent as agent_mod  # noqa: E402
import sns as sns_mod  # noqa: E402

_vec_local = threading.local()


def vconn():
    """每线程一个带 sqlite-vec 扩展的只读连接。"""
    con = getattr(_vec_local, "con", None)
    if con is None:
        con = vector.connect(str(KNOWLEDGE_DB))
        _vec_local.con = con
    return con
from chattrace.service.image_key import ImageKeyResolver  # noqa: E402

app = FastAPI(title="WeChat Knowledge Assistant")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path.endswith((".js", ".css", ".html")) or path == "/":
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

_media: MediaService | None = None
_media_lock = threading.Lock()
_sync_state = {"running": False, "last": "", "log": "尚未运行"}
_pin_ready = False

# 朋友圈后台同步状态
_sns_state = {"running": False, "last": "", "error": "", "booted": False}

# 数据库主密钥的真实有效性（HMAC 实测，不信 24h 时间戳——密钥账号级稳定，可长期使用）
_key_status = {"ts": 0.0, "ok": False, "detail": ""}


def _stored_key_ok(max_age: float = 600) -> bool:
    """存量密钥能否打开当前消息库（HMAC 实测，10 分钟缓存）。跨平台(keyvault)。"""
    now = time.time()
    if now - _key_status["ts"] < max_age:
        return _key_status["ok"]
    ok, detail = False, "no key"
    try:
        import keyvault
        pw = keyvault.load_key(ACCOUNT_ID)
        if pw:
            ok, detail = keyvault.validate_key(pw, ACCOUNT_DIR / "db_storage" / "message" / "message_0.db")
            detail = detail[:150]
        else:
            detail = "no stored key"
    except Exception as exc:      # noqa: BLE001
        detail = str(exc)[:150]
    _key_status.update(ts=now, ok=ok, detail=detail)
    return ok


def _ensure_key_usable() -> bool:
    """密钥失效时的自动恢复：Windows 扫运行中微信内存（免退出），mac 提示手动导入。"""
    if _stored_key_ok():
        return True
    try:
        if os.name != "nt":
            _realtime["keynote"] = "密钥失效：mac 上请手动导入（keyvault.py --import）"
            return False
        import keyscan
        result = keyscan.capture_and_store(ACCOUNT_DIR,
                                           ACCOUNT_DIR / "db_storage" / "message" / "message_0.db")
        _key_status["ts"] = 0.0        # 强制下次重新实测
        if result.get("ok"):
            _realtime["keynote"] = "内存扫描重新取得密钥"
            return True
        _realtime["keynote"] = f"内存扫描未命中: {result.get('detail', '')}"
    except Exception as exc:           # noqa: BLE001
        _realtime["keynote"] = f"内存扫描异常: {exc}"
    return _stored_key_ok(0)


def get_media() -> MediaService:
    global _media
    with _media_lock:
        if _media is None:
            try:
                resolver = ImageKeyResolver(account_id=ACCOUNT_ID, account_dir=ACCOUNT_DIR)
            except Exception:
                try:
                    resolver = ImageKeyResolver(account_id=ACCOUNT_ID,
                                                account_dir=str(ACCOUNT_DIR))
                except Exception:
                    resolver = None
            _media = MediaService(ACCOUNT_ID, ACCOUNT_DIR, DECRYPTED_DIR,
                                  cache_dir=DATA / "media_cache",
                                  image_key_resolver=resolver)
    return _media


def ensure_chat_pin(con: sqlite3.Connection):
    cols = {r[1] for r in con.execute("PRAGMA table_info(chat)")}
    if "pinned" not in cols:
        con.execute("ALTER TABLE chat ADD COLUMN pinned INTEGER DEFAULT 0")
        # 默认置顶：文件传输助手 + 自己
        con.execute("UPDATE chat SET pinned=1 WHERE username IN ('filehelper', ?)",
                    (ACCOUNT_ID.rsplit("_", 1)[0],))


def db() -> sqlite3.Connection:
    global _pin_ready
    con = sqlite3.connect(KNOWLEDGE_DB, timeout=60, check_same_thread=False)
    con.execute("PRAGMA busy_timeout=60000")
    con.row_factory = sqlite3.Row
    if not _pin_ready:
        try:
            ensure_chat_pin(con)
            con.commit()
        except sqlite3.OperationalError:
            pass
        _pin_ready = True
    return con


def load_conf() -> dict:
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps({
            "llm": {"base_url": "https://api.deepseek.com/v1", "api_key": "",
                    "model": "deepseek-chat", "thinking": False,
                    "thinking_model": "deepseek-reasoner"},
            "qa": {"max_context": 30, "temperature": 0.3},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 消息解析
XML_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
XML_DES = re.compile(r"<des>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</des>", re.S)
XML_URL = re.compile(r"<url>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</url>", re.S)
XML_ATTR = re.compile(r'([A-Za-z_][\w:.-]*)\s*=\s*"([^"]*)"')
XML_REFER = re.compile(r"<refermsg>(.*?)</refermsg>", re.S)
XML_DISPLAYNAME = re.compile(r"<displayname>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</displayname>", re.S)
XML_FILENAME = re.compile(r"<(?:filename|title)>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</(?:filename|title)>", re.S)
XML_TOTALEN = re.compile(r"<totallen>(\d+)</totallen>")
XML_VOICelen = re.compile(r'voicelength="(\d+)"')
_SENDER_PREFIX_XML = re.compile(r"^(?:wxid_[0-9A-Za-z]+|[0-9A-Za-z_\-]{6,40}):\s*")
# refermsg 的 <content>：贪婪到块内最后一个 </content>，引用套引用时取最外层正文
XML_REFER_CONTENT = re.compile(r"<content>(.*)</content>", re.S)
XML_QUOTE_MEDIA = re.compile(r"<(img|voicemsg|videomsg|emoji|voipmsg|location)[\s>/]")
_QUOTE_MEDIA_LABEL = {"img": "[图片]", "voicemsg": "[语音]", "videomsg": "[视频]",
                      "emoji": "[表情]", "voipmsg": "[通话]", "location": "[位置]"}


def _xml_unescape(s: str) -> str:
    """反转义到稳定为止：部分老消息的 refermsg.content 被多重转义，一层解不开。"""
    for _ in range(4):
        d = html.unescape(s)
        if d == s:
            return d
        s = d
    return s


def _quote_oneline(s: str) -> str:
    """引用预览压成单行，避免界面出现多行转义残渣。"""
    return re.sub(r"\s+", " ", s or "").strip()[:200]


def _refer_text(ref: str) -> str:
    """refermsg 的人可读正文：优先 <title>，否则取 <content>。

    不能对整个 refermsg 块去标签——那会把 type/svrid/fromusr/msgsource 等
    字段一起倒出来，界面上就是一串乱码。content 本身还可能是内嵌 XML
    （引用了图片/语音/链接/另一条引用），按形状转成简短说明。
    """
    # 部分消息的 refermsg 里带 <msgsource>（内层已转义、可能双重转义），
    # 不剥掉的话兜底去标签时会把 sequence_id/pua 等字段倒出来
    ref = re.sub(r"<msgsource>.*?</msgsource>", "", ref, flags=re.S)
    mt = XML_TITLE.search(ref)
    if mt and mt.group(1).strip():
        return _quote_oneline(_xml_unescape(mt.group(1)))
    mc = XML_REFER_CONTENT.search(ref)
    if not mc:
        return ""
    c = _SENDER_PREFIX_XML.sub("", _xml_unescape(mc.group(1).strip()))
    if not c.startswith("<"):
        # openim 等账号形如 "xxx@openim: " 的发送者前缀：仅当后面跟着 XML 才剥，
        # 避免误伤 "注意: xxx" 这类以冒号开头的正文
        m2 = re.match(r"^[^\s<>:]{1,64}:\s*", c)
        if m2 and c[m2.end():].startswith("<"):
            c = c[m2.end():]
    if c.startswith("<"):
        mt = XML_TITLE.search(c)
        if mt and mt.group(1).strip():
            return _quote_oneline(_xml_unescape(mt.group(1)))
        m = XML_QUOTE_MEDIA.search(c)
        if m:
            return _QUOTE_MEDIA_LABEL[m.group(1)]
        # 彻底去标签后再压行：内层可能还有双重转义的标签，解不净就当未知消息
        plain = _quote_oneline(re.sub(r"<[^>]+>", " ", _xml_unescape(c)))
        if "<" in plain or "&lt;" in plain:
            return "[消息]"
        return plain
    return _quote_oneline(c)


def has_xml_text(content: str) -> bool:
    c = _SENDER_PREFIX_XML.sub("", (content or "").lstrip()) if (content or "").startswith("wxid_") else (content or "").lstrip()
    return c.startswith(("<?xml", "<msg", "<appmsg"))


def _appmsg_dict(c: str) -> dict:
    """把一段 appmsg XML 解析成 {kind, title, des, url}。"""
    sub = re.search(r"<type>(\d+)</type>", c)
    subtype = sub.group(1) if sub else ""
    mt, md_, mu = XML_TITLE.search(c), XML_DES.search(c), XML_URL.search(c)
    title = mt.group(1) if mt else ""
    if subtype == "62":
        return {"text": title or "[拍了拍]"}
    if subtype in ("57",):
        ref = XML_REFER.search(c)
        rdisp = rtitle = ""
        if ref:
            x = XML_DISPLAYNAME.search(ref.group(1))
            rdisp = _quote_oneline(_xml_unescape(x.group(1))) if x else ""
            rtitle = _refer_text(ref.group(1))
        return {"kind": "quote", "title": title, "refer": {"who": rdisp, "title": rtitle}}
    return {"kind": "link", "title": title or "[消息]",
            "des": (md_.group(1) if md_ else "")[:200],
            "url": (mu.group(1) if mu else "")}
SENDER_PREFIX = re.compile(r"^([^:\n]{4,64}):\r?\n")


def parse_message(m: sqlite3.Row) -> dict:
    t, c = (m["type"] or 0) & 4294967295, m["content"] or ""
    out: dict[str, Any] = {}
    if t == 1:
        if has_xml_text(c):
            out.update(_appmsg_dict(c))
        else:
            out["text"] = c
    elif t in (10000, 10002):
        if t == 10000 and "revokemsg" in c:
            # 撤回通知只取 <content>：整段去标签会把 <revoketime>0 的尾巴带出来
            mc = re.search(r"<content>(.*?)</content>", c, re.S)
            out["system"] = _quote_oneline(_xml_unescape(mc.group(1))) if mc \
                else re.sub(r"<[^>]+>", "", c)[:300]
        else:
            out["system"] = re.sub(r"<[^>]+>", "", c)[:300]
    elif t == 3:
        attrs = dict(XML_ATTR.findall(c[:2000]))
        out["kind"] = "image"
        out["md5"] = attrs.get("md5", "")
    elif t == 34:
        n = XML_VOICelen.search(c)
        out["kind"] = "voice"
        out["voice_ms"] = int(n.group(1)) if n else 0
    elif t == 43:
        attrs = dict(XML_ATTR.findall(c[:4000]))
        out["kind"] = "video"
        if attrs.get("playlength", "").isdigit():
            out["duration_s"] = int(attrs["playlength"])
        if attrs.get("md5"):
            out["md5"] = attrs["md5"]
    elif t == 47:
        out["kind"] = "emoji"
    elif t == 62:                       # 拍一拍
        mt = XML_TITLE.search(clean_display(62, c))
        out["text"] = clean_display(62, c) or "[拍了拍]"
    elif t == 42:
        out["kind"] = "card"
    elif t == 48:
        label = XML_ATTR.findall(c)
        out["kind"] = "location"
        out["text"] = (XML_TITLE.search(c).group(1) if XML_TITLE.search(c) else "[位置]")
    elif t == 50:
        out["kind"] = "voip"
    elif t == 49:
        root = re.search(r"<type>(\d+)</type>", c)      # appmsg 内部 type 可能出现在
        subtype = root.group(1) if root else ""          # title 之后，需全串匹配
        title = (XML_TITLE.search(c) or [None, ""])[1] if XML_TITLE.search(c) else ""
        des = (XML_DES.search(c) or [None, ""])[1] if XML_DES.search(c) else ""
        url = (XML_URL.search(c) or [None, ""])[1] if XML_URL.search(c) else ""
        if subtype == "62":                          # 拍一拍
            out["text"] = clean_display(62, c) or "[拍了拍]"
        elif subtype == "57":
            ref = XML_REFER.search(c)
            rdisp = rtitle = ""
            if ref:
                x = XML_DISPLAYNAME.search(ref.group(1))
                rdisp = _quote_oneline(_xml_unescape(x.group(1))) if x else ""
                rtitle = _refer_text(ref.group(1))
            out["kind"] = "quote"
            out["title"] = _quote_oneline(_xml_unescape(title))
            out["refer"] = {"who": rdisp, "title": rtitle}
        elif subtype in ("5", "6"):
            fn = XML_FILENAME.search(c)
            total = XML_TOTALEN.search(c)
            out["kind"] = "file" if (total or subtype == "6") else "link"
            out["title"] = title
            out["url"] = url
            out["size"] = int(total.group(1)) if total else 0
            out["des"] = des[:200]
        else:
            out["kind"] = "link"
            out["title"] = title or "[消息]"
            out["url"] = url
            out["des"] = des[:200]
    else:
        # 4.1.15 里部分 XML 消息（小程序/转发等）以普通文本类型落库
        body = SENDER_PREFIX.sub("", c).lstrip()
        if body.startswith(("<?xml", "<msg", "<appmsg")):
            mt, md_, mu = XML_TITLE.search(body), XML_DES.search(body), XML_URL.search(body)
            if mt or mu:
                out["kind"] = "link"
                out["title"] = mt.group(1) if mt else "[消息]"
                out["des"] = (md_.group(1) if md_ else "")[:200]
                out["url"] = mu.group(1) if mu else ""
                return out
            out["kind"] = "raw"
            out["text"] = "[XML 消息]"
            return out
        out["text"] = c[:500] if c else f"[未知消息类型 {t}]"
        out["kind"] = out.get("kind", "raw")
    return out


def sender_name(con, chat_user, sender, is_sender):
    if is_sender == 1:
        return "我"
    if sender:
        r = con.execute(
            "SELECT COALESCE(NULLIF(remark,''), NULLIF(nick_name,''), username) "
            "FROM contact WHERE username=?", (sender,)).fetchone()
        if r:
            return r[0]
    r = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat "
                    "WHERE username=?", (chat_user,)).fetchone()
    return (r[0] if r else chat_user) if not sender else (sender or "")


def _media_text(con, message_id: int, kind: str) -> str:
    try:
        r = con.execute("SELECT text FROM media_text WHERE message_id=? AND kind=?",
                        (message_id, kind)).fetchone()
        return (r[0] or "") if r else ""
    except sqlite3.OperationalError:
        return ""


def _asr_text(con, message_id: int) -> str:
    return _media_text(con, message_id, "asr")


def msg_row(m: sqlite3.Row, con) -> dict:
    disp = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat "
                       "WHERE username=?", (m["chat_user"],)).fetchone()
    parsed = parse_message(m)
    if m["type"] in (34, 43):       # 语音/视频消息带上转写文本
        asr = _media_text(con, m["id"], "asr")
        if asr:
            parsed["asr_text"] = asr
    elif m["type"] == 3:            # 图片消息带上 OCR 文本
        ocr = _media_text(con, m["id"], "ocr")
        if ocr:
            parsed["ocr_text"] = ocr
    return {
        "id": m["id"], "ts": m["ts"], "type": m["type"],
        "is_sender": m["is_sender"], "sender": m["sender"],
        "sender_name": sender_name(con, m["chat_user"], m["sender"], m["is_sender"]),
        "chat": m["chat_user"],
        "chat_name": disp[0] if disp else m["chat_user"],
        "src_local_id": m["src_local_id"],
        "parsed": parsed,
    }


# ---------------------------------------------------------------- 撤回还原
RECALL_WINDOW = 600   # 撤回通知与原消息的最大间隔(秒)：自己撤回限2分钟，管理员撤回可更久


def _recall_name(content: str) -> str | None:
    """撤回通知 content → 被撤回者的名字；自己撤回返回 ""，非撤回通知返回 None。"""
    if "revokemsg" not in (content or ""):
        return None
    mc = re.search(r"<content>(.*?)</content>", content, re.S)
    txt = _quote_oneline(_xml_unescape(mc.group(1))) if mc else ""
    nm = re.match(r'^"(.+?)"\s*撤回', txt)
    return nm.group(1) if nm else ""


def _recall_target(nrow: sqlite3.Row, con) -> sqlite3.Row | None:
    """还原撤回通知对应的原消息。

    微信 4.0 撤回时会把原消息行从自己的库里删掉，只有实时同步先于删除
    抓到过原消息，knowledge.db 里才有。找不到就返回 None（界面退回显示
    "xx 撤回了一条消息"）。同一发送者连撤多条时，通知按时间顺序依次认领
    "当时最新"的未认领候选，先模拟掉更早通知的认领再轮到本通知。
    """
    name = _recall_name(nrow["content"])
    if name is None:
        return None
    T = nrow["ts"] or 0
    cands = con.execute(
        "SELECT * FROM message WHERE chat_user=? AND ts>=? AND ts<? "
        "AND type NOT IN (10000,10002) AND (noise=0 OR noise IS NULL) "
        "ORDER BY ts DESC, id DESC LIMIT 50",
        (nrow["chat_user"], T - RECALL_WINDOW, T)).fetchall()
    if name:
        cands = [c for c in cands
                 if sender_name(con, nrow["chat_user"], c["sender"], c["is_sender"]) == name]
    else:
        cands = [c for c in cands if c["is_sender"] == 1]
    used: set[int] = set()
    for e in con.execute(
            "SELECT * FROM message WHERE chat_user=? AND type=10000 "
            "AND ts>=? AND ts<? AND id<? ORDER BY ts, id",
            (nrow["chat_user"], T - RECALL_WINDOW, T, nrow["id"])):
        if _recall_name(e["content"]) != name:
            continue
        for c in cands:                       # cands 新→旧，取 e 当时能认领的最新一条
            if c["ts"] < e["ts"] and c["id"] not in used:
                used.add(c["id"])
                break
    for c in cands:
        if c["id"] not in used:
            return c
    return None


def _recall_pass(rows: list[sqlite3.Row], con, drop_noise: bool = True) -> list[dict]:
    """消息列表渲染前的撤回处理：通知能还原出原消息时跳过通知、给原消息打标。

    drop_noise 与各端点既有行为对齐：主列表一直显示未转写语音(noise=1)，
    实时流/增量刷新则一直隐藏。
    """
    targets: set[int] = set()
    skip: set[int] = set()
    for r in rows:
        if (r["type"] or 0) == 10000 and "revokemsg" in (r["content"] or ""):
            t = _recall_target(r, con)
            if t is not None:
                targets.add(t["id"])
                skip.add(r["id"])
    out = []
    for r in rows:
        if r["id"] in skip or (drop_noise and r["noise"]):
            continue
        d = msg_row(r, con)
        if r["id"] in targets:
            d["recalled"] = True
        out.append(d)
    return out


# ---------------------------------------------------------------- API: 会话/消息
@app.get("/api/status")
def api_status():
    con = db()
    conf = load_conf()
    return {
        "messages": con.execute("SELECT COUNT(*) FROM message").fetchone()[0],
        "chats": con.execute("SELECT COUNT(*) FROM chat").fetchone()[0],
        "contacts": con.execute("SELECT COUNT(*) FROM contact").fetchone()[0],
        "llm": "configured" if conf["llm"]["api_key"] else "missing",
        "llm_model": conf["llm"]["model"],
        "sync": _sync_state,
        "key_fp": "a3045c56",
    }


@app.get("/api/me")
def api_me():
    """自己的 wxid（用于消息气泡右侧显示本人头像）。"""
    return {"username": ACCOUNT_ID.rsplit("_", 1)[0], "account": ACCOUNT_ID}


@app.get("/api/sessions")
def api_sessions():
    con = db()
    rows = con.execute("""
        SELECT c.username, COALESCE(NULLIF(c.display_name,''), c.username) AS name,
               c.is_group, c.last_ts, IFNULL(c.pinned,0) AS pinned,
               (SELECT COUNT(*) FROM message m WHERE m.chat_user = c.username) AS cnt,
               (SELECT substr(content, 1, 120) FROM message m
                WHERE m.chat_user = c.username AND m.noise = 0
                ORDER BY ts DESC LIMIT 1) AS preview_raw,
               (SELECT type FROM message m
                WHERE m.chat_user = c.username AND m.noise = 0
                ORDER BY ts DESC LIMIT 1) AS last_type
        FROM chat c ORDER BY IFNULL(c.pinned,0) DESC, c.last_ts DESC""").fetchall()
    out = []
    from textutil import display_text as _cd
    TYPE_LABEL = {3: "🖼 图片", 34: "🎤 语音", 43: "🎬 视频", 47: "😊 表情",
                  42: "👤 名片", 50: "📞 通话", 49: "🔗 链接"}
    for r in rows:
        d = dict(r)
        preview = d.pop("preview_raw", "") or ""
        lt = d.pop("last_type", 1) or 1
        if lt in TYPE_LABEL:
            preview = TYPE_LABEL[lt]
        else:
            preview = _cd(lt, preview).replace(chr(10), " ")[:60]
        d["summary"] = preview if preview else f"{d['cnt']} 条消息"
        out.append(d)
    return out


@app.post("/api/sessions/pin")
def api_session_pin(payload: dict):
    con = db()
    ensure_chat_pin(con)
    username = payload.get("username")
    if not username:
        raise HTTPException(400, "缺少 username")
    pinned = 1 if payload.get("pinned") else 0
    con.execute("UPDATE chat SET pinned=? WHERE username=?", (pinned, username))
    con.commit()
    return {"ok": True, "username": username, "pinned": pinned}


@app.get("/api/messages")
def api_messages(chat: str, before: int = 0, limit: int = 40):
    con = db()
    if before:
        rows = con.execute(
            "SELECT * FROM message WHERE chat_user=? AND id < ? ORDER BY id DESC LIMIT ?",
            (chat, before, limit)).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM message WHERE chat_user=? ORDER BY id DESC LIMIT ?",
            (chat, limit)).fetchall()
    rows = _recall_pass(list(reversed(rows)), con, drop_noise=False)
    has_more = 1 if rows and con.execute(
        "SELECT 1 FROM message WHERE chat_user=? AND id < ? LIMIT 1",
        (chat, rows[0]["id"])).fetchone() else 0
    return {"messages": rows, "has_more": has_more}


@app.get("/api/messages/context")
def api_context(id: int, around: int = 15):
    con = db()
    m = con.execute("SELECT * FROM message WHERE id=?", (id,)).fetchone()
    if not m:
        raise HTTPException(404)
    rows = con.execute(
        "SELECT * FROM message WHERE chat_user=? AND id BETWEEN ? AND ? ORDER BY id",
        (m["chat_user"], id - around, id + around)).fetchall()
    chat = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat "
                       "WHERE username=?", (m["chat_user"],)).fetchone()
    return {"chat": m["chat_user"],
            "chat_name": chat[0] if chat else m["chat_user"],
            "messages": _recall_pass(rows, con), "focus": id}


@app.get("/api/search")
def api_search(q: str, chat: str = "", n: int = 30):
    con = db()
    chat_cond, params = "", []
    if chat:
        chat_cond = "AND m.chat_user IN (SELECT username FROM chat WHERE display_name LIKE ? OR username LIKE ?)"
        params = [f"%{chat}%", f"%{chat}%"]
    terms: list[str] = []
    words = [w for w in q.split() if w] or [q]
    for w in words:
        if len(w) >= 3:
            terms.append(w)
            if len(w) > 3:
                terms.extend(w[i:i + 3] for i in range(min(len(w) - 2, 12)))
        # 2 字及以下走 LIKE
    out: dict[int, dict] = {}
    if terms:
        match = " OR ".join(f'"{t}"' for t in terms)
        rows = con.execute(f"""
            SELECT m.*, bm25(message_fts) AS rank FROM message_fts f
            JOIN message m ON m.id = f.rowid
            WHERE message_fts MATCH ? {chat_cond}
            ORDER BY rank LIMIT ?""", [match] + params + [n]).fetchall()
        for r in rows:
            out[r["id"]] = {"msg": r, "rank": r["rank"]}
    short = [w for w in words if 0 < len(w) < 3]
    if short:
        lconds = " AND ".join("m.content LIKE ?" for _ in short)
        rows = con.execute(f"SELECT m.* FROM message m WHERE {lconds} AND m.noise=0 {chat_cond} "
                           "ORDER BY ts DESC LIMIT ?", [f"%{w}%" for w in short] + params + [n]).fetchall()
        for r in rows:
            out.setdefault(r["id"], {"msg": r, "rank": 99})
    res = sorted(out.values(), key=lambda x: -x["msg"]["ts"])[:n]
    return [{"id": r["msg"]["id"], "chat": r["msg"]["chat_user"], "ts": r["msg"]["ts"],
             "sender_name": sender_name(con, r["msg"]["chat_user"], r["msg"]["sender"], r["msg"]["is_sender"]),
             "chat_name": (con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat WHERE username=?",
                                       (r["msg"]["chat_user"],)).fetchone() or [r["msg"]["chat_user"]])[0],
             "snippet": (clean_display(r["msg"]["type"], r["msg"]["content"]) or "")[:200]}
            for r in res]


@app.get("/api/contacts")
def api_contacts(q: str = "", limit: int = 3000):
    """通讯录：只用有真实名字的联系人，按拼音首字母分组排序。"""
    con = db()
    rows = con.execute("""
        SELECT c.username,
               TRIM(COALESCE(NULLIF(TRIM(c.remark),''), NULLIF(TRIM(c.nick_name),''), '')) AS name,
               COALESCE(c.initial_letter, '') AS letter
        FROM contact c
        WHERE IFNULL(c.local_type,0) IN (1, 2, 6) AND IFNULL(c.delete_flag,0)=0
          AND TRIM(COALESCE(NULLIF(TRIM(c.remark),''), NULLIF(TRIM(c.nick_name),''), '')) <> ''
          AND (? = '' OR c.username LIKE ? OR c.nick_name LIKE ? OR c.remark LIKE ?)
        ORDER BY CASE WHEN INSTR('ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                                 IFNULL(c.initial_letter,'')) > 0 THEN 0
                      WHEN INSTR('0123456789', IFNULL(c.initial_letter,'')) > 0 THEN 1
                      ELSE 2 END,
                 c.initial_letter, name
        LIMIT ?""", (q, f"%{q}%", f"%{q}%", f"%{q}%", limit)).fetchall()
    out = []
    for r in rows:
        letter = r["letter"] or "#"
        out.append({"username": r["username"], "name": r["name"], "initial": letter,
                    "is_group": int("@chatroom" in r["username"])})
    return out


# ---------------------------------------------------------------- API: 问答
def _keyword_hits(con, query: str, chat: str | None, k: int) -> list[int]:
    """关键词召回：FTS trigram + 短词 LIKE 兜底，返回按相关度排序的 message.id。"""
    chat_cond, chat_params = "", []
    if chat:
        chat_cond = "AND m.chat_user = ?"
        chat_params = [chat]
    words = [w for w in query.split() if w] or [query]
    terms = []
    for w in words:
        if len(w) >= 3:
            terms.append(w)
            if len(w) > 3:
                terms.extend(w[i:i + 3] for i in range(min(len(w) - 2, 12)))
    ids: list[int] = []
    if terms:
        match = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = con.execute(f"""
                SELECT m.id FROM message_fts f JOIN message m ON m.id = f.rowid
                WHERE message_fts MATCH ? {chat_cond}
                ORDER BY bm25(message_fts) LIMIT ?""", [match] + chat_params + [k]).fetchall()
            ids.extend(r[0] for r in rows)
        except sqlite3.OperationalError:
            pass
    short = [w for w in words if 0 < len(w) < 3]
    if short:
        lconds = " AND ".join("m.content LIKE ?" for _ in short)
        rows = con.execute(f"SELECT m.id FROM message m WHERE {lconds} AND m.noise=0 "
                           f"{chat_cond} ORDER BY ts DESC LIMIT ?",
                           [f"%{w}%" for w in short] + chat_params + [k]).fetchall()
        ids.extend(r[0] for r in rows if r[0] not in ids)
    # 媒体文本（OCR/ASR 结果）也参与召回
    if terms:
        match = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = con.execute("""SELECT media_fts.message_id FROM media_fts
                WHERE media_fts MATCH ? LIMIT ?""", (match, k)).fetchall()
            ids.extend(r[0] for r in rows if r[0] not in ids)
        except sqlite3.OperationalError:
            pass
    return ids[:k * 2]


_vec_cache: dict[tuple[str, str], tuple[float, list]] = {}


def retrieve(con, query, chat=None, k=30, use_vector=True):
    """混合检索：关键词(BM25) + 向量(余弦) **并行** → RRF 融合（准确率优先）。"""
    vec: list[tuple[int, float]] = []
    key = (query, chat or "")
    cached = _vec_cache.get(key)
    if cached and time.time() - cached[0] < 600:
        vec = cached[1]
        kw = _keyword_hits(con, query, chat, k)
    else:
        box: dict = {}

        def run_kw():
            c2 = db()           # 线程内独立连接（SQLite 连接不可跨线程）
            try:
                box["kw"] = _keyword_hits(c2, query, chat, k)
            except Exception:
                box["kw"] = []
            finally:
                c2.close()

        def run_vec():
            try:
                box["vec"] = vector.search(vconn(), query, k=max(20, k * 2), chat=chat)
            except Exception:
                box["vec"] = []

        th1 = threading.Thread(target=run_kw)
        th2 = threading.Thread(target=run_vec)
        th1.start(); th2.start()
        th1.join(); th2.join()
        kw = box.get("kw", [])
        vec = box.get("vec", [])
        _vec_cache[key] = (time.time(), vec)
        if len(_vec_cache) > 300:
            _vec_cache.clear()
    scores: dict[int, float] = {}
    for rank, mid in enumerate(kw):
        scores[mid] = scores.get(mid, 0) + 1.0 / (60 + rank)
    for rank, (mid, sim) in enumerate(vec):
        scores[mid] = scores.get(mid, 0) + 1.0 / (60 + rank)
    ordered = sorted(scores.items(), key=lambda x: -x[1])[:k]
    ids = [m for m, _ in ordered]
    if not ids:
        return []
    rows = con.execute("SELECT * FROM message WHERE id IN (%s)" % ",".join("?" * len(ids)),
                       ids).fetchall()
    by_id = {r["id"]: r for r in rows}
    hits = [by_id[i] for i in ids if i in by_id]
    return [r for r in hits if not r["noise"]]


def retrieve_links(con, query: str, k: int = 3) -> list[dict]:
    """在已索引的公众号/网页正文中检索，返回 [{url,title,text}]。"""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    words = [w for w in q.split() if w] or [q]
    out: list[dict] = []
    seen: set[str] = set()

    titles_seen: set[str] = set()

    def add(rows):
        for r in rows:
            t = (r[1] or "").strip()
            if r[0] in seen or t in titles_seen or len(out) >= k:
                continue
            seen.add(r[0])
            if t:
                titles_seen.add(t)
            out.append({"url": r[0], "title": t, "text": r[2] or "",
                        "received": (r[3] if len(r) > 3 else 0) or 0})

    # 1) FTS 全文（trigram 需要 ≥3 字）
    terms = [w for w in words if len(w) >= 3]
    if len(q) >= 3 and len(terms) == 0:
        terms = [q]
    if terms:
        match = " OR ".join('"' + t + '"' for t in terms)
        try:
            add(con.execute("""SELECT c.url, c.title, c.text, c.msg_ts FROM link_fts f
                JOIN link_cache c ON c.url = f.url
                WHERE link_fts MATCH ? LIMIT ?""", (match, k)).fetchall())
        except sqlite3.OperationalError:
            pass
    # 2) 逐词 LIKE 兜底（覆盖 2 字词/短查询）
    if len(out) < k:
        for w in sorted(set(words), key=len, reverse=True)[:3]:
            if len(w) < 2:
                continue
            try:
                add(con.execute("""SELECT url, title, text, msg_ts FROM link_cache
                    WHERE title LIKE ? OR text LIKE ?
                    ORDER BY ts DESC LIMIT ?""", (f"%{w}%", f"%{w}%", k)).fetchall())
            except sqlite3.OperationalError:
                break
            if len(out) >= k:
                break
    return out[:k]


def expand_context(con, rows, around: int = 3) -> list[dict]:
    """给每条命中消息取同会话前后 N 条，合并重叠块，返回 [{hit, block, block_ids}]。"""
    blocks: list[dict] = []
    seen_ranges: list[tuple[str, int, int]] = []
    for r in rows:
        chat, mid = r["chat_user"], r["id"]
        lo, hi = mid - around, mid + around
        overlap = next((b for b in blocks
                        if b["chat"] == chat and lo <= b["hi"] and hi >= b["lo"]), None)
        if overlap:
            overlap["ids"].add(mid)
            continue
        ctx_rows = con.execute(
            "SELECT id, is_sender, sender, ts, type, content FROM message "
            "WHERE chat_user=? AND id BETWEEN ? AND ? ORDER BY id",
            (chat, lo, hi)).fetchall()
        blocks.append({
            "chat": chat, "lo": lo, "hi": hi, "hit": mid, "ids": {mid},
            "msgs": [dict(x) for x in ctx_rows],
        })
    return blocks


def call_llm(conf, messages_payload):
    import urllib.request
    llm = conf["llm"]
    body = json.dumps({"model": llm["model"], "messages": messages_payload,
                       "temperature": conf.get("qa", {}).get("temperature", 0.3)}).encode()
    req = urllib.request.Request(
        llm["base_url"].rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {llm['api_key']}"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())["choices"][0]["message"]["content"]


def ensure_qa_table(con):
    con.execute("CREATE TABLE IF NOT EXISTS ai_qa(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "chat_id TEXT, question TEXT, answer TEXT, cited_refs TEXT, ts INTEGER)")
    cols = {r[1] for r in con.execute("PRAGMA table_info(ai_qa)")}
    if "scope" not in cols:
        con.execute("ALTER TABLE ai_qa ADD COLUMN scope TEXT DEFAULT 'chat'")
    if "hidden" not in cols:
        con.execute("ALTER TABLE ai_qa ADD COLUMN hidden INTEGER DEFAULT 0")
    if "trace" not in cols:
        con.execute("ALTER TABLE ai_qa ADD COLUMN trace TEXT DEFAULT ''")
    # AI 会话（一次连续对话）；scope 为 'assistant'、chat_user、file:<chat>:<local_id>、link:<hash>
    con.execute("""CREATE TABLE IF NOT EXISTS ai_session(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        title TEXT DEFAULT '',
        created INTEGER, updated INTEGER)""")
    scols = {r[1] for r in con.execute("PRAGMA table_info(ai_session)")}
    if "name" not in scols:      # 作用域的显示名（群名 / 文件名 / 文章标题）
        con.execute("ALTER TABLE ai_session ADD COLUMN name TEXT DEFAULT ''")
    if "session_id" not in cols:
        con.execute("ALTER TABLE ai_qa ADD COLUMN session_id INTEGER")
        _migrate_sessions(con)
    con.execute("CREATE INDEX IF NOT EXISTS idx_qa_session ON ai_qa(session_id)")


def _migrate_sessions(con):
    """把历史问答按 chat_id 各归入一个会话。"""
    rows = con.execute("SELECT chat_id, COUNT(*), MIN(ts), MAX(ts) FROM ai_qa "
                       "WHERE session_id IS NULL GROUP BY chat_id").fetchall()
    for chat_id, _n, t0, t1 in rows:
        cur = con.execute("INSERT INTO ai_session(scope,title,created,updated) "
                          "VALUES(?,?,?,?)", (chat_id, "历史对话", t0 or 0, t1 or 0))
        con.execute("UPDATE ai_qa SET session_id=? WHERE chat_id=? AND session_id IS NULL",
                    (cur.lastrowid, chat_id))
    con.commit()


def get_or_create_session(con, scope: str, new: bool = False, name: str = "") -> int:
    if not new:
        r = con.execute("SELECT id FROM ai_session WHERE scope=? ORDER BY id DESC LIMIT 1",
                        (scope,)).fetchone()
        if r:
            if name:
                con.execute("UPDATE ai_session SET name=? WHERE id=? AND IFNULL(name,'')=''",
                            (name[:60], r[0]))
            return r[0]
    cur = con.execute("INSERT INTO ai_session(scope,title,created,updated,name) "
                      "VALUES(?,?,?,?,?)",
                      (scope, "", int(time.time()), int(time.time()), (name or "")[:60]))
    return cur.lastrowid


def touch_session(con, sid: int, title: str | None = None):
    con.execute("UPDATE ai_session SET updated=? WHERE id=?", (int(time.time()), sid))
    if title:
        con.execute("UPDATE ai_session SET title=? WHERE id=? AND IFNULL(title,'')=''",
                    (title[:40], sid))


@app.post("/api/ask")
def api_ask(payload: dict):
    q = (payload.get("question") or "").strip()
    chat = payload.get("chat") or None          # None = 助手全局
    history = payload.get("history") or []      # [{q, a}]
    fdoc = payload.get("file") or None          # {chat, local_id, ts, name} 指定文件问答
    quoted_ids = payload.get("quoted") or []    # 被引用的消息 id 列表
    use_agent = payload.get("agent", True)
    want_new_session = bool(payload.get("new_session"))
    session_id = payload.get("session_id")
    if not q:
        raise HTTPException(400, "空问题")
    con = db()
    conf0 = load_conf()
    if "thinking" in payload:
        conf0["llm"] = dict(conf0["llm"], thinking=bool(payload["thinking"]))

    # 引用消息 + Agent 模式（需要 API Key）
    if quoted_ids and conf0["llm"].get("api_key") and use_agent and not fdoc:
        quoted = []
        for qid in quoted_ids[:10]:
            r = con.execute("SELECT * FROM message WHERE id=?", (int(qid),)).fetchone()
            if r:
                b = agent_mod._msg_brief(con, r)
                quoted.append({"id": b["id"], "ts": b["ts"], "who": b["who"], "text": b["text"]})
        chat_name = None
        if chat:
            row = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat "
                              "WHERE username=?", (chat,)).fetchone()
            chat_name = row[0] if row else chat
        res = agent_mod.run(q, conf0, chat_ctx=chat, chat_name=chat_name,
                            quoted=quoted, history=history)
        ans, refs, trace = res["answer"], res["refs"], res["trace"]
        ensure_qa_table(con)
        sid = session_id or get_or_create_session(con, chat or "assistant", new=want_new_session)
        con.execute("INSERT INTO ai_qa(chat_id, question, answer, cited_refs, ts, scope, "
                    "trace, session_id) VALUES(?,?,?,?,?,?,?,?)",
                    (chat or "assistant", q, ans, json.dumps(refs, ensure_ascii=False),
                     int(time.time()), "quote", json.dumps(trace, ensure_ascii=False), sid))
        touch_session(con, sid, q)
        con.commit()
        return {"mode": "agent", "answer": ans, "refs": refs, "trace": trace,
                "session_id": sid}

    # Agent 模式（无引用，有 Key）：让模型自主调工具
    if conf0["llm"].get("api_key") and use_agent and not fdoc:
        chat_name = None
        if chat:
            row = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat "
                              "WHERE username=?", (chat,)).fetchone()
            chat_name = row[0] if row else chat
        try:
            res = agent_mod.run(q, conf0, chat_ctx=chat, chat_name=chat_name,
                                history=history)
            if res.get("answer"):
                ensure_qa_table(con)
                sid = session_id or get_or_create_session(con, chat or "assistant",
                                                          new=want_new_session)
                con.execute("INSERT INTO ai_qa(chat_id, question, answer, cited_refs, ts, "
                            "scope, trace, session_id) VALUES(?,?,?,?,?,?,?,?)",
                            (chat or "assistant", q, res["answer"],
                             json.dumps(res["refs"], ensure_ascii=False),
                             int(time.time()), "agent",
                             json.dumps(res["trace"], ensure_ascii=False), sid))
                touch_session(con, sid, q)
                con.commit()
                return {"mode": "agent", "answer": res["answer"],
                        "refs": res["refs"], "trace": res["trace"], "session_id": sid}
        except Exception:
            pass      # 失败时退回检索式回答

    # 指定文件问答：文件正文直接作为上下文，不做消息检索
    if fdoc:
        path = locate_file(fdoc.get("chat", ""), int(fdoc.get("local_id", 0)),
                           int(fdoc.get("ts", 0)), fdoc.get("name", ""))
        if not path:
            return {"mode": "empty", "refs": [],
                    "answer": f"文件「{fdoc.get('name','')}」不在本地磁盘上（可能已被微信清理或未下载）。"}
        text = extract_text(path, 24000)
        conf = load_conf()
        mode = "llm" if conf["llm"]["api_key"] else "retrieval"
        refs = [{"n": 1, "id": 0, "kind": "file", "url": "",
                 "chat": fdoc.get("chat", ""), "chat_name": "",
                 "local_id": int(fdoc.get("local_id", 0) or 0), "ts": fdoc.get("ts", 0),
                 "title": path.name, "size": path.stat().st_size,
                 "sender_name": "文件", "snippet": path.name}]
        body = "文件《%s》（%.0f KB）内容如下：%s%s" % (
            path.name, path.stat().st_size / 1024, chr(10), text)
        if mode == "llm":
            msgs = [{"role": "system", "content":
                     "你是用户的文件助手。仅依据给出的文件内容回答问题；"
                     "引用时标注页码/工作表；文件中没有答案就直说没找到，不要编造。"}]
            for h in history[-4:]:
                msgs.append({"role": "user", "content": h.get("q", "")})
                if h.get("a"):
                    msgs.append({"role": "assistant", "content": h["a"][:2000]})
            msgs.append({"role": "user", "content": body + chr(10) + chr(10) + "问题：" + q})
            try:
                ans = call_llm(conf, msgs)
            except Exception as e:
                mode = "retrieval"
                ans = "[调用模型失败：%s]%s%s" % (str(e)[:120], chr(10), chr(10)) + body[:3000]
        else:
            ans = "**[未配置 API Key —— 以下为文件提取内容]**" + chr(10) + chr(10) + body[:4000]
        ensure_qa_table(con)
        con.execute("INSERT INTO ai_qa(chat_id, question, answer, cited_refs, ts, scope) "
                    "VALUES(?,?,?,?,?,?)",
                    (fdoc.get("chat") or "assistant", q, ans,
                     json.dumps(refs, ensure_ascii=False), int(time.time()), "file"))
        con.commit()
        return {"mode": mode, "answer": ans, "refs": refs, "session_id": sid}

    conf = load_conf()
    qa_conf = conf.get("qa", {})
    rows = [r for r in retrieve(con, q, chat, int(qa_conf.get("max_context", 30)))
            if not r["noise"]]
    if not rows:
        ans = "没有在" + ("该会话" if chat else "全部记录") + "中检索到相关内容，试试换个说法或切换全局范围。"
        return {"mode": "empty", "answer": ans, "refs": []}
    around = int(qa_conf.get("context_around", 3))
    blocks = expand_context(con, rows, around=around)
    link_hits = retrieve_links(con, q, k=3)
    refs, ctx_lines = [], []
    for i, b in enumerate(blocks, 1):
        name = (con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat "
                            "WHERE username=?", (b["chat"],)).fetchone() or [b["chat"]])[0]
        ctx_msgs = []
        for x in b["msgs"]:
            who = sender_name(con, b["chat"], x["sender"], x["is_sender"])
            txt = clean_display(x["type"], x["content"]) or ""
            ctx_msgs.append({"id": x["id"], "who": who,
                             "ts": x["ts"], "text": txt[:400],
                             "is_hit": x["id"] in b["ids"]})
            ctx_lines.append(f"[{i}] ({name}) "
                             f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(x['ts'] or 0))} "
                             f"{who}: {txt[:500]}")
        hit_rows = [x for x in b["msgs"] if x["id"] in b["ids"]]
        anchor = hit_rows[0] if hit_rows else b["msgs"][0]
        anchor_who = sender_name(con, b["chat"], anchor["sender"], anchor["is_sender"])
        refs.append({"n": i, "id": anchor["id"], "chat": b["chat"], "chat_name": name,
                     "ts": anchor["ts"], "sender_name": anchor_who,
                     "snippet": (clean_display(anchor["type"], anchor["content"]) or "")[:160],
                     "ctx": ctx_msgs})
    for lk in link_hits:
        idx = len(refs) + 1
        refs.append({"n": idx, "id": 0, "kind": "link", "url": lk["url"],
                     "chat": "", "chat_name": lk["title"] or "网页文章",
                     "ts": 0, "sender_name": "公众号/网页",
                     "snippet": (lk["text"] or "")[:160], "ctx": []})
        ctx_lines.append(f"[{idx}] （公众号/网页《{lk['title']}》）{(lk['text'] or '')[:1500]}")
    context = "\n".join(ctx_lines)
    mode = "llm" if conf["llm"]["api_key"] else "retrieval"
    if mode == "llm":
        sys_p = ("你是用户的微信记录助手。仅依据给出的聊天记录上下文回答，"
                 "引用时标注 [编号]；标注为公众号/网页的内容也要引用其编号；"
                 "上下文没有答案就直说没找到，不要编造。"
                 "上下文格式：[编号] (会话名) 时间 发言人: 内容")
        msgs = [{"role": "system", "content": sys_p}]
        for h in history[-4:]:
            msgs.append({"role": "user", "content": h.get("q", "")})
            if h.get("a"):
                msgs.append({"role": "assistant", "content": h["a"][:2000]})
        scope = "当前会话" if chat else "全部聊天记录"
        msgs.append({"role": "user", "content":
                     f"聊天记录上下文（范围：{scope}）：\n{context}\n\n问题：{q}"})
        try:
            ans = call_llm(conf, msgs)
        except Exception as e:
            mode = "retrieval"
            ans = "[调用模型失败：" + str(e)[:120] + "]\n\n" + context[:3000]
    else:
        # 检索模式：只给前 5 段摘录，完整内容交给「引用来源」折叠区
        ans = ("**检索模式（未配置 API Key）：命中 %d 段对话**，以下为前 5 段摘录。"
               "完整内容与前后文请展开下方「引用来源」。\n\n%s"
               % (len(blocks), "\n".join(ctx_lines[:5])))
    ensure_qa_table(con)
    sid = session_id or get_or_create_session(con, chat or "assistant", new=want_new_session)
    con.execute("INSERT INTO ai_qa(chat_id, question, answer, cited_refs, ts, scope, "
                "session_id) VALUES(?,?,?,?,?,?,?)",
                (chat or "assistant", q, ans,
                 json.dumps(refs, ensure_ascii=False), int(time.time()),
                 "chat" if chat else "global", sid))
    touch_session(con, sid, q)
    con.commit()
    return {"mode": mode, "answer": ans, "refs": refs, "session_id": sid}


@app.get("/api/ai/sessions")
def api_ai_sessions():
    """AI 会话树：按作用域分组（全局 / 各群聊·私聊 / 各文件 / 各链接），互不混用。"""
    con = db()
    ensure_qa_table(con)
    rows = con.execute("""
        SELECT s.id, s.scope, s.title, s.created, s.updated, s.name,
               (SELECT COUNT(*) FROM ai_qa q WHERE q.session_id=s.id
                 AND IFNULL(q.hidden,0)=0) cnt
        FROM ai_session s
        ORDER BY s.updated DESC""").fetchall()
    groups: dict[str, dict] = {}
    for r in rows:
        scope = r["scope"]
        g = groups.setdefault(scope, {"scope": scope, "sessions": [], "name": r["name"] or ""})
        g["sessions"].append({"id": r["id"], "title": r["title"] or "新对话",
                              "ts": r["updated"], "count": r["cnt"]})
    out = []
    for scope, g in groups.items():
        kind = "assistant"
        is_group = False
        name = g["name"]
        if scope == "assistant":
            name = name or "全局助手"
        elif scope.startswith("file:"):
            kind = "file"
            if not name:
                _, chat, lid = (scope.split(":", 2) + ["", ""])[:3]
                name = "文件问答"
        elif scope.startswith("link:"):
            kind = "link"
            name = name or "链接问答"
        else:
            row = con.execute("SELECT COALESCE(NULLIF(display_name,''), username), is_group "
                              "FROM chat WHERE username=?", (scope,)).fetchone()
            name = name or (row[0] if row else scope)
            is_group = bool(row[1]) if row else False
            kind = "group" if is_group else "chat"
        out.append({"scope": scope, "name": name, "is_group": is_group, "kind": kind,
                    "sessions": [x for x in g["sessions"]
                                 if x["count"] > 0 or len(g["sessions"]) == 1],
                    "count": len(g["sessions"])})
    out.sort(key=lambda x: (x["scope"] != "assistant",
                            -max([y["ts"] for y in x["sessions"]] or [0])))
    return out


@app.post("/api/ai/sessions")
def api_ai_session_new(payload: dict):
    con = db()
    ensure_qa_table(con)
    scope = payload.get("scope") or "assistant"
    name = (payload.get("name") or "").strip()
    sid = get_or_create_session(con, scope, new=True, name=name)
    con.commit()
    return {"id": sid, "scope": scope, "name": name}


@app.get("/api/ai/session/{sid}")
def api_ai_session_get(sid: int):
    con = db()
    ensure_qa_table(con)
    s = con.execute("SELECT * FROM ai_session WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(404, "会话不存在")
    rows = con.execute("SELECT * FROM ai_qa WHERE session_id=? AND IFNULL(hidden,0)=0 "
                       "ORDER BY id", (sid,)).fetchall()
    return {"id": s["id"], "scope": s["scope"], "title": s["title"] or "新对话",
            "turns": [{"id": r["id"], "question": r["question"], "answer": r["answer"],
                       "refs": json.loads(r["cited_refs"] or "[]"), "ts": r["ts"],
                       "mode": r["scope"],
                       "trace": json.loads((r["trace"] or "[]")
                                           if "trace" in r.keys() else "[]")}
                      for r in rows]}


@app.post("/api/ai/session/{sid}/delete")
def api_ai_session_delete(sid: int):
    con = db()
    ensure_qa_table(con)
    con.execute("UPDATE ai_qa SET hidden=1 WHERE session_id=?", (sid,))
    con.execute("DELETE FROM ai_session WHERE id=?", (sid,))
    con.commit()
    return {"ok": True}


@app.get("/api/qa")
def api_qa(chat: str = "", limit: int = 50):
    con = db()
    ensure_qa_table(con)
    rows = con.execute("SELECT * FROM ai_qa WHERE chat_id=? AND IFNULL(hidden,0)=0 "
                       "ORDER BY id DESC LIMIT ?", (chat or "assistant", limit)).fetchall()
    return [{"id": r["id"], "question": r["question"], "answer": r["answer"],
             "refs": json.loads(r["cited_refs"] or "[]"), "ts": r["ts"],
             "trace": json.loads((r["trace"] or "[]") if "trace" in r.keys() else "[]"),
             "scope": r["scope"] if "scope" in r.keys() else "chat"}
            for r in reversed(rows)]


@app.post("/api/qa/hide")
def api_qa_hide(payload: dict):
    con = db()
    ensure_qa_table(con)
    ids = payload.get("ids") or ([payload["id"]] if payload.get("id") else [])
    con.executemany("UPDATE ai_qa SET hidden=1 WHERE id=?", [(i,) for i in ids])
    con.commit()
    return {"ok": True, "hidden": len(ids)}


def recent_context(con, chat: str, hours: int = 24, max_msgs: int = 80) -> dict | None:
    """会话最近记录（按时间正序），作为默认上下文注入。"""
    since = int(time.time()) - hours * 3600
    rows = con.execute("SELECT * FROM message WHERE chat_user=? AND ts>=? AND noise=0 "
                       "ORDER BY ts DESC LIMIT ?", (chat, since, max_msgs)).fetchall()
    if len(rows) < 3:      # 24h 太少就退到最近 N 条
        rows = con.execute("SELECT * FROM message WHERE chat_user=? AND noise=0 "
                           "ORDER BY ts DESC LIMIT ?", (chat, 40)).fetchall()
    rows = list(reversed(rows))
    if not rows:
        return None
    name = (con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat "
                        "WHERE username=?", (chat,)).fetchone() or [chat])[0]
    lines, ctx = [], []
    for r in rows:
        who = sender_name(con, chat, r["sender"], r["is_sender"])
        txt = clean_display(r["type"], r["content"]) or ""
        if not txt:
            continue
        lines.append(f"{time.strftime('%m-%d %H:%M', time.localtime(r['ts'] or 0))} "
                     f"{who}: {txt[:300]}")
        ctx.append({"id": r["id"], "who": who, "ts": r["ts"], "text": txt[:300],
                    "is_hit": False})
    if not lines:
        return None
    span = (f"最近 {hours} 小时" if len(rows) >= 3 else "最近消息")
    return {"n": 0, "kind": "recent", "chat": chat, "chat_name": name,
            "ts": rows[-1]["ts"], "sender_name": "最近记录",
            "snippet": f"{span}（{len(ctx)} 条）",
            "text": "【最近记录】" + span + chr(10) + chr(10).join(lines[-60:]),
            "ctx": ctx[-60:], "label": span}


def quick_retrieve(con, query: str, chat: str | None, since: int | None,
                   limit: int = 8, hours: int = 24):
    """一次性检索：返回 (hits, refs)。refs 带前后文，供快速作答。

    会话内提问时，第 0 条固定注入"最近记录"，让模型先看到今/昨天的上下文。
    """
    rows = retrieve(con, query, chat, limit)
    if since:
        rows = [r for r in rows if (r["ts"] or 0) >= since][:limit]
    if not rows:
        return [], []
    blocks = expand_context(con, rows, around=2)
    refs = []
    if chat and not since:
        rc = recent_context(con, chat, hours)
        if rc:
            refs.append(rc)
    for i, b in enumerate(blocks, 1):
        name = (con.execute("SELECT COALESCE(NULLIF(display_name,''), username) "
                            "FROM chat WHERE username=?", (b["chat"],)).fetchone() or [b["chat"]])[0]
        ctx_msgs, lines = [], []
        for x in b["msgs"]:
            who = sender_name(con, b["chat"], x["sender"], x["is_sender"])
            txt = clean_display(x["type"], x["content"]) or ""
            ctx_msgs.append({"id": x["id"], "who": who, "ts": x["ts"],
                             "text": txt[:400], "is_hit": x["id"] in b["ids"]})
            lines.append(f"[{i}] ({name}) "
                         f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(x['ts'] or 0))} "
                         f"{who}: {txt[:500]}")
        hit = next((x for x in b["msgs"] if x["id"] in b["ids"]), b["msgs"][0])
        refs.append({"n": i, "id": hit["id"], "chat": b["chat"], "chat_name": name,
                     "ts": hit["ts"],
                     "sender_name": sender_name(con, b["chat"], hit["sender"], hit["is_sender"]),
                     "snippet": (clean_display(hit["type"], hit["content"]) or "")[:160],
                     "text": chr(10).join(lines), "ctx": ctx_msgs})
    return refs, refs


def save_qa(con, chat, question, answer, refs, scope, trace, session_id, new_session,
            scope_name=None):
    ensure_qa_table(con)
    key = chat or "assistant"
    sid = session_id or get_or_create_session(con, key, new=new_session,
                                              name=scope_name or "")
    if scope_name:
        con.execute("UPDATE ai_session SET name=? WHERE id=? AND IFNULL(name,'')=''",
                    (scope_name[:60], sid))
    con.execute("INSERT INTO ai_qa(chat_id, question, answer, cited_refs, ts, scope, "
                "trace, session_id) VALUES(?,?,?,?,?,?,?,?)",
                (key, question, answer,
                 json.dumps(refs, ensure_ascii=False), int(time.time()), scope,
                 json.dumps(trace or [], ensure_ascii=False), sid))
    touch_session(con, sid, question)
    con.commit()
    return sid


def _warm_models():
    """后台预热：向量模型 + 图片密钥，避免首次提问卡几十秒。"""
    try:
        if vector.available():
            vector.embed(["预热"])
    except Exception:
        pass
    try:
        get_media().image_keys()
    except Exception:
        pass


@app.on_event("startup")
def _on_startup():
    threading.Thread(target=_warm_models, daemon=True).start()
    # 链接后台解析：历史回填 + 常驻 worker（新收到的链接即时解析入库）
    def _links_boot():
        try:
            _backfill_chat_links()
        except Exception:
            pass
    threading.Thread(target=_links_boot, daemon=True).start()
    if _LINK_WORKER["thread"] is None:
        tw = threading.Thread(target=_link_worker_loop, daemon=True)
        tw.start()
        _LINK_WORKER["thread"] = tw
    if _auto_sync["thread"] is None:
        t = threading.Thread(target=_auto_sync_loop, daemon=True)
        t.start()
        _auto_sync["thread"] = t
    if _realtime["thread"] is None:
        t2 = threading.Thread(target=_realtime_loop, daemon=True)
        t2.start()
        _realtime["thread"] = t2
    if not _sns_state["booted"]:
        _sns_state["booted"] = True
        threading.Thread(target=_sns_boot, daemon=True).start()
        threading.Thread(target=_sns_loop, daemon=True).start()
    threading.Thread(target=_auto_backlog_boot, daemon=True).start()


def _register_sns_links(con) -> int:
    """朋友圈分享链接与消息链接同等待遇：登记 chat_link + 入解析队列。

    同步后调用：新出现的链接自动进后台解析（正文/图片 OCR/视频转写入
    link_cache），链接中心可见（来源显示"朋友圈"），AI 可 read_link。
    INSERT OR IGNORE 以 url 为键——聊天里已登记过的同一链接不会重复解析。
    """
    _ensure_link_tables(con)
    n = 0
    try:
        rows = con.execute("SELECT tid, ts, links_json FROM sns_post "
                           "WHERE links_json IS NOT NULL AND links_json != '[]'").fetchall()
    except sqlite3.OperationalError:
        return 0
    for tid, ts, lj in rows:
        try:
            links = json.loads(lj or "[]")
        except (TypeError, ValueError):
            continue
        for l in links:
            u = (l.get("url") or "").strip()
            if not u:
                continue
            try:
                cur = con.execute(
                    "INSERT OR IGNORE INTO chat_link(url,chat,msg_id,msg_ts,title,platform) "
                    "VALUES(?,?,?,?,?,?)",
                    (u, "sns", 0, int(ts or 0), (l.get("title") or "")[:120],
                     _refresh_platform(u)))
            except sqlite3.OperationalError:
                continue
            if cur.rowcount:
                n += 1
                _LINK_QUEUE.put(u)
    if n:
        con.commit()
    return n


def _sns_boot():
    """启动时朋友圈首次同步：sns.db 没解密过就补一次增量解密。"""
    try:
        sns_mod.ensure_schema(db())
        st = sns_mod.sync_sns(db())
        if not st.get("ok") and "不存在" in (st.get("detail") or ""):
            _decrypt_quick()
            st = sns_mod.sync_sns(db())
        _sns_state["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
        sns_mod.warm_isaac()          # 必须先于 OCR：daemon 冷启动 ~20s
        try:
            _register_sns_links(db())  # 分享链接自动归档解析(与消息同款)
        except Exception:             # noqa: BLE001
            pass
        try:
            sns_mod.build_sns_vec()
        except Exception:             # noqa: BLE001 向量失败不影响文字检索
            pass
        sns_mod.process_sns_ocr(db(), cap=120)
    except Exception as exc:          # noqa: BLE001
        _sns_state["error"] = str(exc)[:150]
    except Exception as exc:          # noqa: BLE001
        _sns_state["error"] = str(exc)[:150]


def _sns_loop():
    """朋友圈低频增量同步（微信朋友圈写入不会动 message_*.db，需独立轮询）。"""
    while True:
        try:
            interval = max(60, int(load_conf().get("sync", {}).get("sns_interval_sec", 120)))
        except Exception:             # noqa: BLE001
            interval = 120
        time.sleep(interval)
        if _sns_state["running"]:
            continue
        _sns_state["running"] = True
        try:
            st = sns_mod.sync_sns(db())
            if st.get("ok") and (st.get("new_posts") or st.get("upd_posts")):
                try:
                    sns_mod.build_sns_vec()
                except Exception:     # noqa: BLE001
                    pass
            if st.get("ok"):
                try:
                    _register_sns_links(db())
                except Exception:     # noqa: BLE001
                    pass
            sns_mod.process_sns_ocr(db(), cap=12)
            _sns_state["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _sns_state["error"] = ""
        except Exception as exc:      # noqa: BLE001
            _sns_state["error"] = str(exc)[:150]
        finally:
            _sns_state["running"] = False


@app.post("/api/ask/stream")
def api_ask_stream(payload: dict):
    from fastapi.responses import StreamingResponse
    import stream_qa
    deps = {"conf": load_conf, "db": db,
            "quick_retrieve": quick_retrieve, "save_qa": save_qa,
            "locate_file": locate_file, "extract_text": extract_text,
            "fetch_article": fetch_article, "file_text_cached": file_text_cached}
    return StreamingResponse(stream_qa.make_stream(payload, deps),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- API: 模型配置
@app.get("/api/config")
def api_config_get():
    conf = load_conf()
    key = conf["llm"].get("api_key", "")
    masked = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已配置" if key else "")
    return {"base_url": conf["llm"].get("base_url", ""),
            "model": conf["llm"].get("model", ""),
            "thinking": bool(conf["llm"].get("thinking")),
            "thinking_model": conf["llm"].get("thinking_model", "deepseek-reasoner"),
            "has_key": bool(key), "key_masked": masked}


@app.post("/api/config")
def api_config_set(payload: dict):
    conf = load_conf()
    llm = conf.setdefault("llm", {})
    for k in ("base_url", "model"):
        v = (payload.get(k) or "").strip()
        if v:
            llm[k] = v
    if payload.get("api_key"):
        llm["api_key"] = payload["api_key"].strip()
    if "thinking" in payload:
        llm["thinking"] = bool(payload["thinking"])
    if payload.get("thinking_model"):
        llm["thinking_model"] = payload["thinking_model"].strip()
    CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "has_key": bool(llm.get("api_key"))}


@app.post("/api/config/test")
def api_config_test():
    conf = load_conf()
    if not conf["llm"].get("api_key"):
        raise HTTPException(400, "尚未填写 API Key")
    try:
        ans = call_llm(conf, [{"role": "user", "content": "只回复两个字：你好"}])
    except Exception as e:
        raise HTTPException(400, f"连接失败：{e}")
    return {"ok": True, "reply": ans[:60], "model": conf["llm"]["model"]}


# ---------------------------------------------------------------- API: 文件预览与文件问答
FILE_EXTS_TEXT = {".txt", ".md", ".csv", ".log", ".json", ".py", ".js", ".ts", ".html",
                  ".xml", ".yml", ".yaml", ".ini", ".sql", ".java", ".c", ".cpp", ".h"}


def locate_file(chat: str, local_id: int, ts: int, name: str) -> Path | None:
    """在 msg/file/<YYYY-MM>/ 下按月定位收到的文件（同名取最近修改）。"""
    from datetime import datetime, timedelta
    fdir = ACCOUNT_DIR / "msg" / "file"
    if not fdir.is_dir():
        return None
    t = datetime.fromtimestamp(ts or 0)
    months = [(t + timedelta(days=d)).strftime("%Y-%m") for d in (0, -1, 1)]
    safe = name.replace("/", "_")
    cands: list[Path] = []
    for mth in months:
        p = fdir / mth / safe
        if p.exists():
            cands.append(p)
    if not cands:      # 兜底：全目录按文件名搜索（只扫有该月的少量目录）
        for sub in sorted(fdir.iterdir(), reverse=True)[:36]:
            if sub.is_dir():
                p = sub / safe
                if p.exists():
                    cands.append(p)
                    break
    if not cands:
        # 模糊匹配：文件名包含 title 关键词
        for mth in months:
            fdir_m = fdir / mth
            if not fdir_m.is_dir():
                continue
            for f in fdir_m.iterdir():
                if not f.is_file():
                    continue
                # 去掉扩展名后做包含匹配
                stem = f.stem.lower()
                if len(name) > 3 and (name[:8].lower() in stem or stem[:8] in name.lower()):
                    cands.append(f)
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def extract_text(path: Path, limit: int = 20000) -> str:
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            parts = []
            for i, page in enumerate(reader.pages[:40]):
                parts.append(f"[第{i+1}页]\n{(page.extract_text() or '').strip()}")
                if sum(len(x) for x in parts) > limit:
                    break
            return "\n".join(parts)[:limit]
        if ext in (".docx", ".doc"):
            import docx as _docx
            d = _docx.Document(str(path))
            txt = "\n".join(p.text for p in d.paragraphs)
            for tbl in d.tables[:5]:
                for row in tbl.rows[:30]:
                    txt += "\n| " + " | ".join(c.text.strip() for c in row.cells) + " |"
            return txt[:limit]
        if ext in (".xlsx", ".xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            out = []
            for ws in wb.worksheets[:3]:
                out.append(f"[工作表 {ws.title}]")
                for row in ws.iter_rows(max_row=200, values_only=True):
                    vals = [str(v) for v in row if v is not None]
                    if vals:
                        out.append(" | ".join(vals))
                if sum(len(x) for x in out) > limit:
                    break
            wb.close()
            return "\n".join(out)[:limit]
        if ext in FILE_EXTS_TEXT:
            for enc in ("utf-8", "gbk", "utf-16"):
                try:
                    return path.read_text(encoding=enc)[:limit]
                except UnicodeDecodeError:
                    continue
            return path.read_bytes()[:limit].decode("utf-8", "ignore")
    except Exception as e:
        return f"[解析失败：{e}]"
    return ""


def _ensure_file_text(con):
    con.execute("""CREATE TABLE IF NOT EXISTS file_text(
        key TEXT PRIMARY KEY, name TEXT DEFAULT '', text TEXT DEFAULT '',
        ext TEXT DEFAULT '', ts INTEGER DEFAULT 0)""")


def file_text_cached(con, chat: str, local_id: int, ts: int, name: str, limit: int = 24000):
    """文件正文缓存（附件不可变，按「会话:src_local_id」只抽一次）。返回 (path, name, text)。

    命中 → path 为 None（调用方用缓存里的 name 即可）；未命中且文件在本地 →
    抽取并写缓存；文件不在本地/解析失败 → 尽量返回已提取的文本（不缓存失败结果）。
    """
    _ensure_file_text(con)
    key = f"{chat}:{int(local_id or 0)}"
    try:
        r = con.execute("SELECT name, text FROM file_text WHERE key=?", (key,)).fetchone()
        cached_name, cached_text = r[0], (r[1] or "")
    except Exception:
        cached_name, cached_text = None, ""
    if cached_text.strip() and not cached_text.startswith("[解析失败"):
        return None, (cached_name or name), cached_text[:limit]
    p = locate_file(chat, local_id, ts, name)
    if not p:
        return None, name, cached_text[:limit]
    text = extract_text(p, max(limit, 24000))
    if text and not text.startswith("[解析失败"):
        try:
            con.execute("INSERT OR REPLACE INTO file_text(key,name,text,ext,ts) VALUES(?,?,?,?,?)",
                        (key, p.name, text, p.suffix.lower(), int(time.time())))
            con.commit()
        except Exception:
            pass
    return p, p.name, text


def warm_file_texts(con, cap: int = 3, days: int = 7):
    """后台预热：把最近几天的文件消息正文提前抽进 file_text（收到即提取，对齐链接的
    「收到即解析」）。文件未下载到本地/解析失败 → 写空文本标记，48 小时内每轮重试
    （等微信下载），过后永久放弃，避免每轮重扫。"""
    from textutil import appmsg_subtype
    _ensure_file_text(con)
    since = int(time.time()) - days * 86400
    try:
        rows = con.execute(
            "SELECT chat_user, src_local_id, ts, content FROM message "
            "WHERE ts>=? AND noise=0 AND (type & 0xFFFFFFFF)=49 AND src_local_id IS NOT NULL "
            "ORDER BY ts DESC LIMIT 60", (since,)).fetchall()
    except Exception:
        return 0
    now = int(time.time())
    done = 0

    def _mark(key, name, text):
        con.execute("INSERT OR REPLACE INTO file_text(key,name,text,ext,ts) VALUES(?,?,?,?,?)",
                    (key, name, text, "", now))

    for r in rows:
        if done >= cap:
            break
        try:
            if appmsg_subtype(r["content"] or "") != "6":
                continue
            m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                          r["content"] or "", re.S)
            name = m.group(1).strip() if m else ""
            if not name:
                continue
            key = f"{r['chat_user']}:{r['src_local_id']}"
            hit = con.execute("SELECT text, ts FROM file_text WHERE key=?", (key,)).fetchone()
            if hit:
                if (hit[0] or "").strip():
                    continue                     # 已有正文
                if (hit[1] or 0) and now - hit[1] > 48 * 3600:
                    continue                     # 标记超过 48h，放弃
            p = locate_file(r["chat_user"], r["src_local_id"], r["ts"], name)
            if not p:
                _mark(key, name, "")             # 还没下载，下轮再试
                continue
            text = extract_text(p, 24000)
            if text and not text.startswith("[解析失败"):
                con.execute("INSERT OR REPLACE INTO file_text(key,name,text,ext,ts) "
                            "VALUES(?,?,?,?,?)",
                            (key, p.name, text, p.suffix.lower(), now))
                done += 1
            else:
                _mark(key, name, "")             # 解析失败也标记，避免每轮重抽
            try:
                con.commit()      # PDF/Office 抽取可达数十秒,逐条提交防写锁长持
            except Exception:
                pass
        except Exception:
            continue
    try:
        con.commit()
    except Exception:
        pass
    return done


@app.get("/api/file/meta")
def api_file_meta(chat: str, local_id: int, ts: int, name: str):
    # 文件缺失时前端要能退回"跳转到这条文件消息"，所以无论如何都解析一次全局消息 id
    msg_id = None
    try:
        con = db()
        r = con.execute("SELECT id FROM message WHERE chat_user=? AND src_local_id=? "
                        "ORDER BY ts DESC LIMIT 1", (chat, local_id)).fetchone()
        msg_id = r["id"] if r else None
        con.close()
    except Exception:
        msg_id = None
    p = locate_file(chat, local_id, ts, name)
    if not p:
        return {"found": False, "name": name, "msg_id": msg_id}
    return {"found": True, "name": p.name, "path": str(p),
            "size": p.stat().st_size, "ext": p.suffix.lower(), "msg_id": msg_id,
            "previewable": p.suffix.lower() in FILE_EXTS_TEXT | {".pdf"} |
                           {".png", ".jpg", ".jpeg", ".gif", ".webp"}}


@app.get("/api/file/raw")
def api_file_raw(chat: str, local_id: int, ts: int, name: str):
    p = locate_file(chat, local_id, ts, name)
    if not p:
        raise HTTPException(404, "文件不在本地（可能已被清理或未下载）")
    ext = p.suffix.lower()
    media = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
             ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
             ".csv": "text/plain; charset=utf-8"}.get(ext, "application/octet-stream")
    return FileResponse(str(p), media_type=media,
                        filename=p.name, content_disposition_type="inline")


@app.get("/api/file/text")
def api_file_text(chat: str, local_id: int, ts: int, name: str, limit: int = 20000):
    con = db()
    try:
        p, fname, text = file_text_cached(con, chat, local_id, ts, name, max(limit, 24000))
    finally:
        con.close()
    if not text:
        raise HTTPException(404, "文件不在本地（可能已被清理或未下载）")
    return {"name": fname, "text": text[:limit]}


# ---------------------------------------------------------------- API: 媒体
def varint(data: bytes, i: int):
    v = 0; shift = 0
    while i < len(data):
        b = data[i]; i += 1
        v |= (b & 0x7F) << shift
        if not b & 0x80:
            return v, i
        shift += 7
    return v, i


def pb_fields(data: bytes) -> list[tuple[int, bytes]]:
    """极简 protobuf 字段解析（wiretype 2 只取长度前缀字段）。"""
    out, i = [], 0
    try:
        while i < len(data):
            tag, i = varint(data, i)
            wt = tag & 7
            if wt == 2:
                ln, i = varint(data, i)
                out.append((tag >> 3, data[i:i + ln])); i += ln
            elif wt == 0:
                _, i = varint(data, i)
            else:
                break
    except Exception:
        pass
    return out


def resource_db() -> sqlite3.Connection | None:
    """每次请求新建只读连接（FastAPI 线程池跨线程复用连接会报错）。"""
    p = DECRYPTED_DIR / "message" / "message_resource.db"
    if not p.exists():
        return None
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def chat_resource_id(con, chat_user: str) -> int | None:
    rdb = resource_db()
    if not rdb:
        return None
    r = rdb.execute("SELECT rowid FROM ChatName2Id WHERE user_name=?", (chat_user,)).fetchone()
    return r[0] if r else None


# 磁盘文件 md5（Img/Video 文件名）只存在源库 packed_info_data 里，与消息 XML 的 md5 属性不同
_PACKED_MD5_CACHE: dict[tuple[str, str, int], str] = {}
_SRC_TABLE_RE = re.compile(r"^Msg_[0-9A-Za-z]{32}$")


def packed_md5(src_db: str, src_table: str, src_local_id) -> str:
    """源库消息行的磁盘文件指纹：取不到返回空串。"""
    if not (src_db and src_table and src_local_id):
        return ""
    if not _SRC_TABLE_RE.match(src_table) or not re.match(r"^message_\d+\.db$", src_db):
        return ""
    key = (src_db, src_table, int(src_local_id))
    hit = _PACKED_MD5_CACHE.get(key)
    if hit is not None:
        return hit
    md5 = ""
    p = DECRYPTED_DIR / "message" / src_db
    if p.exists():
        try:
            src = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
            row = src.execute(f"SELECT packed_info_data FROM [{src_table}] WHERE local_id=?",
                              (key[2],)).fetchone()
            src.close()
            if row and row[0]:
                from chattrace.service.media import md5_hex32
                md5 = md5_hex32(row[0]) or ""
        except Exception:
            md5 = ""
    if len(_PACKED_MD5_CACHE) > 5000:
        _PACKED_MD5_CACHE.clear()
    _PACKED_MD5_CACHE[key] = md5
    return md5


def resource_sizes(chat_user: str, local_id: int) -> list[dict]:
    """返回该消息的资源明细 [{type, size}]（来自微信自己的映射表）。"""
    rdb = resource_db()
    if not rdb:
        return []
    cid = chat_resource_id(db(), chat_user)
    if cid is None:
        return []
    rows = rdb.execute("""
        SELECT d.type, d.size, d.packed_info FROM MessageResourceInfo i
        JOIN MessageResourceDetail d ON d.message_id = i.message_id
        WHERE i.chat_id=? AND i.message_local_id=?""", (cid, local_id)).fetchall()
    return [{"type": r["type"], "size": r["size"], "packed": r["packed_info"]} for r in rows]


def find_dat_by_size(chat_user: str, ts: int, target_sizes: list[int],
                     name_must_not: str | None = None) -> Path | None:
    """按月份目录 + 精确字节大小定位 .dat（微信 4.1.15 文件名 md5 与 XML 不一致）。"""
    import hashlib
    from datetime import datetime, timedelta
    base = ACCOUNT_DIR / "msg" / "attach" / hashlib.md5(chat_user.encode()).hexdigest()
    if not base.is_dir():
        return None
    t = datetime.fromtimestamp(ts or 0)
    months = [(t + timedelta(days=d)).strftime("%Y-%m") for d in (-1, 0, 1)]
    for size in sorted(target_sizes, reverse=True):
        for month in months:
            imgdir = base / month / "Img"
            if not imgdir.is_dir():
                continue
            for f in imgdir.glob("*.dat"):
                if name_must_not and name_must_not in f.name:
                    continue
                if abs(f.stat().st_size - size) <= 64:
                    return f
    return None


@app.get("/api/debug/keys")
def api_debug_keys():
    ms = get_media()
    k = ms.image_keys()
    res = ms._image_key_resolver
    return {"keys": k.aes_key_ascii if k else None,
            "reason": (res.reason if res else "no resolver")}


def image_candidates(chat: str, ts: int, full_sizes: list[int],
                     thumb_sizes: list[int], xml_md5: str = "") -> list[Path]:
    """收集按优先级排序的候选 .dat：全图尺寸 > 缩略图尺寸，月目录 ±1。"""
    import hashlib
    from datetime import datetime, timedelta
    cands: list[Path] = []
    seen: set[str] = set()
    base = ACCOUNT_DIR / "msg" / "attach" / hashlib.md5(chat.encode()).hexdigest()
    t = datetime.fromtimestamp(ts or 0)
    months = [(t + timedelta(days=d)).strftime("%Y-%m") for d in (-1, 0, 1)]
    targets = [(s, "full") for s in full_sizes if s > 0] +               [(s, "thumb") for s in thumb_sizes if s > 0]
    for size, prio in sorted(targets, key=lambda x: 0 if x[1] == "full" else 1):
        for month in months:
            imgdir = base / month / "Img"
            if not imgdir.is_dir():
                continue
            for f in imgdir.glob("*.dat"):
                if abs(f.stat().st_size - size) <= 64 and f.name not in seen:
                    seen.add(f.name)
                    cands.append(f)
    return cands


@app.get("/api/media/image")
def api_image(chat: str, id: int):
    con = db()
    m = con.execute("SELECT * FROM message WHERE id=? AND chat_user=?", (id, chat)).fetchone()
    if not m:
        raise HTTPException(404)
    p = parse_message(m)
    cands: list[Path] = []
    # 1) 源库 packed_info_data 的磁盘 md5：文件名精确匹配（4.1.15 的 Img 文件名
    #    与 XML md5 不一致，按尺寸匹配会拿到别的图——"新图片显示成别的图"的根源）
    disk_md5 = packed_md5(m["src_db"], m["src_table"], m["src_local_id"])
    if disk_md5:
        try:
            cands = get_media().image_dat_candidates(chat, disk_md5, m["ts"] or 0)
        except Exception:
            cands = []
    # 2) 旧版 md5 命名兜底（XML md5 与文件名一致的老账号）
    if not cands and p.get("md5", ""):
        try:
            item = get_media().image_item(chat, p["md5"], m["ts"] or 0)
            if item and item.disk_path and item.disk_path.exists():
                cands = [item.disk_path, *item.alternates]
        except Exception:
            pass
    # 3) 最后才按字节尺寸模糊匹配
    if not cands:
        full_sizes, thumb_sizes = [], []
        try:
            for r in resource_sizes(chat, m["src_local_id"]):
                # 0x40001(262145)=缩略图，0x20001(131073)=全图
                if r["type"] == 262145:
                    thumb_sizes.append(r["size"])
                else:
                    full_sizes.append(r["size"])
        except Exception:
            pass
        cands = image_candidates(chat, m["ts"] or 0, full_sizes, thumb_sizes)
    if not cands:
        raise HTTPException(404, "图片文件不存在（可能已被微信清理）")
    keys = get_media().image_keys()
    last_err = None
    for path in cands[:6]:
        try:
            fmt, blob = decode_dat(path.read_bytes(), keys)
        except Exception as e:
            last_err = e
            continue
        media_type = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "gif": "image/gif"}.get(fmt)
        if media_type:
            return Response(content=blob, media_type=media_type,
                            headers={"Cache-Control": "private, max-age=86400"})
    raise HTTPException(415, f"图片无法解码（{last_err}）")


@app.get("/api/media/voice")
def api_voice(chat: str, id: int):
    con = db()
    m = con.execute("SELECT * FROM message WHERE id=? AND chat_user=?", (id, chat)).fetchone()
    if not m:
        raise HTTPException(404)
    try:
        wav = get_media().voice_wav(m["chat_user"], m["src_local_id"], m["ts"] or 0)
        if wav:
            return Response(content=wav, media_type="audio/wav")
    except Exception as e:
        raise HTTPException(500, f"转码失败: {e}")
    raise HTTPException(404, "voice not found")


def _video_files(chat_user: str, m: sqlite3.Row) -> tuple[Path | None, Path | None]:
    """(mp4, 封面图)。md5 优先取源库 packed_info_data（磁盘文件名），XML md5 兜底。"""
    md5 = packed_md5(m["src_db"], m["src_table"], m["src_local_id"])
    if not md5:
        md5 = (parse_message(m) or {}).get("md5", "")
    if not md5:
        return None, None
    try:
        return get_media().video_file(chat_user, md5, m["ts"] or 0)
    except Exception:
        return None, None


@app.get("/api/media/video")
def api_video(chat: str, id: int, request: Request):
    """视频消息流式播放（支持 Range，浏览器才能拖进度条）。"""
    con = db()
    m = con.execute("SELECT * FROM message WHERE id=? AND chat_user=?", (id, chat)).fetchone()
    con.close()
    if not m:
        raise HTTPException(404)
    mp4, _thumb = _video_files(chat, m)
    if not mp4:
        raise HTTPException(404, "原视频不在本地（可能已被微信清理或未下载）")
    size = mp4.stat().st_size
    rng = (request.headers.get("range") or "").strip()
    mt = re.match(r"bytes=(\d*)-(\d*)$", rng)
    if mt:
        start = int(mt.group(1)) if mt.group(1) else 0
        if mt.group(2):
            end = min(int(mt.group(2)), size - 1)
        else:
            end = min(size - 1, start + 4 * 1024 * 1024 - 1)   # 无尾值时分块返回
        if start >= size or start > end:
            return Response(status_code=416,
                            headers={"Content-Range": f"bytes */{size}"})
        with open(mp4, "rb") as f:
            f.seek(start)
            blob = f.read(end - start + 1)
        return Response(content=blob, status_code=206, media_type="video/mp4",
                        headers={"Content-Range": f"bytes {start}-{end}/{size}",
                                 "Accept-Ranges": "bytes",
                                 "Cache-Control": "private, max-age=86400"})
    return FileResponse(str(mp4), media_type="video/mp4",
                        headers={"Accept-Ranges": "bytes",
                                 "Cache-Control": "private, max-age=86400"})


@app.get("/api/media/video/thumb")
def api_video_thumb(chat: str, id: int):
    con = db()
    m = con.execute("SELECT * FROM message WHERE id=? AND chat_user=?", (id, chat)).fetchone()
    con.close()
    if not m:
        raise HTTPException(404)
    _mp4, thumb = _video_files(chat, m)
    if not thumb or not thumb.exists():
        raise HTTPException(404, "视频封面不存在")
    return Response(content=thumb.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/media/asr")
def api_media_asr(chat: str, id: int):
    """单条媒体消息的转写文本（视频预览页用；实时转写是异步的，打开时再取一次）。"""
    con = db()
    try:
        return {"text": _asr_text(con, id)}
    finally:
        con.close()


# ---------------------------------------------------------------- API: 文件中心
_LINKS_CACHE: dict = {"ts": 0.0, "rows": []}


def _extract_links(content: str) -> list[tuple[str, str]]:
    """从消息文本中提取可读链接 [(url, 卡片标题)]，过滤资源/附件类 URL。"""
    import html as _h
    from link_media import detect as _detect
    c = _h.unescape(content or "")
    title = ""
    mt = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", c, re.S)
    if mt:
        title = mt.group(1).strip()
    out = []
    for u in re.findall(r"https?://[^\s\"'<>()\[\]]+", c):
        u = _h.unescape(u).strip(".,'\"")
        kind = _detect(u)
        if kind == "generic":
            low = u.lower()
            if any(k in low for k in ("stodownload", "qpic.cn", "qlogo.cn", "mmhead",
                                      "/cdn/", "cdn.", "mmbiz_", "res.wx.qq.com",
                                      "work.weixin.qq.com", "unsupported", "wxapp.",
                                      "biz.r.qq.com")):
                continue
            path = low.split("://")[-1].split("?")[0]
            if path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mp3",
                              ".zip", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
                              ".ppt", ".pptx")):
                continue
            if "/" not in path or path.endswith("/"):
                continue
        out.append((u, title))
    return out


def _ensure_link_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS chat_link(
        url TEXT PRIMARY KEY, chat TEXT, msg_id INTEGER, msg_ts INTEGER,
        title TEXT DEFAULT '', platform TEXT DEFAULT '', queued INTEGER DEFAULT 0,
        state INTEGER DEFAULT 0)""")   # 0 待解析 / 1 已解析 / 2 不支持或失败
    cols = {r[1] for r in con.execute("PRAGMA table_info(chat_link)")}
    if "state" not in cols:
        con.execute("ALTER TABLE chat_link ADD COLUMN state INTEGER DEFAULT 0")
    con.execute("CREATE INDEX IF NOT EXISTS idx_chat_link_ts ON chat_link(msg_ts)")


def _queue_link(url: str, chat: str, msg_id: int, msg_ts: int, title: str, platform: str):
    """收到消息即登记的链接：写 chat_link 并入解析队列（已解析过的直接跳过）。"""
    con = db()
    _ensure_link_tables(con)
    con.execute("INSERT OR IGNORE INTO chat_link(url,chat,msg_id,msg_ts,title,platform,queued) "
                "VALUES(?,?,?,?,?,?,0)", (url, chat, msg_id, msg_ts, (title or "")[:120], platform))
    con.commit()
    con.close()
    _LINK_QUEUE.put(url)


def _refresh_platform(url: str) -> str:
    from link_media import detect
    return detect(url)


# ---------------------------------------------------------------- 链接后台解析
_LINK_QUEUE: "queue.Queue[str]" = queue.Queue()   # 链接解析队列（收到即排队）
_LINK_WORKER = {"thread": None, "last": "", "done": 0, "error": "", "current": ""}


def _link_worker_loop():
    """后台把排队中的链接解析成正文（图文 OCR / 视频转写），写入 link_cache。

    队列空时自动捞"待解析"的历史链接继续处理（每轮 5 条，新→旧），
    这样链接栏里不会有永远挂着"解析中"的条目。
    """
    from link_media import parse, detect
    while True:
        try:
            url = _LINK_QUEUE.get(timeout=15)
        except Exception:
            # 队列空闲：补捞还没解析过的（state=0 且无缓存正文）
            try:
                con = db()
                _ensure_link_tables(con)
                rows = con.execute("""
                    SELECT l.url FROM chat_link l
                    LEFT JOIN link_cache c ON c.url=l.url
                    WHERE IFNULL(l.state,0)=0 AND IFNULL(c.text,'')=''
                    ORDER BY l.msg_ts DESC LIMIT 5""").fetchall()
                con.close()
                for (u,) in rows:
                    _LINK_QUEUE.put(u)
                if not rows:
                    time.sleep(20)
            except Exception:
                time.sleep(20)
            continue
        try:
            # 已解析过（有缓存正文）就跳过
            con = db()
            r = con.execute("SELECT text FROM link_cache WHERE url=? AND IFNULL(text,'')<>''",
                            (url,)).fetchone()
            con.close()
            if r:
                continue
            _LINK_WORKER["current"] = url[:80]
            kind = detect(url)
            if kind == "generic":
                # 公众号/普通网页：走原有的正文抓取（不需要 OCR/ASR）
                d = fetch_article(url)
                ok = bool((d.get("text") or "").strip()) and not d.get("error")
                con = db(); _ensure_link_tables(con)
                con.execute("UPDATE chat_link SET state=? WHERE url=?", (1 if ok else 2, url))
                if ok and d.get("title"):
                    con.execute("UPDATE chat_link SET title=? WHERE url=? AND IFNULL(title,'')=''",
                                (d["title"][:120], url))
                con.commit(); con.close()
                if ok:
                    _LINK_WORKER["done"] += 1
                    _LINK_WORKER["last"] = time.strftime("%H:%M:%S")
                continue
            res = parse(url)
            if res.get("error"):
                _LINK_WORKER["error"] = f"{url[:60]}：{res['error']}"
                con = db(); _ensure_link_tables(con)
                con.execute("UPDATE chat_link SET state=2 WHERE url=?", (url,))
                con.commit(); con.close()
                continue
            con = _link_con()
            _save_link(con, url, res.get("title", ""), res.get("text", ""), kind,
                       {k: res.get(k) for k in ("parts", "images", "video", "platform",
                                                "owner", "page", "pages", "truncated")})
            # 回填 chat_link 的平台与标题
            con.execute("UPDATE chat_link SET platform=?, state=1, "
                        "title=CASE WHEN IFNULL(title,'')='' THEN ? ELSE title END WHERE url=?",
                        (res.get("platform", ""), (res.get("title") or "")[:120], url))
            con.commit()
            con.close()
            _LINK_WORKER["done"] += 1
            _LINK_WORKER["last"] = time.strftime("%H:%M:%S")
        except Exception as e:      # noqa: BLE001
            _LINK_WORKER["error"] = f"{url[:60]}：{type(e).__name__}"
        finally:
            _LINK_WORKER["current"] = ""


def _backfill_chat_links(limit_msgs: int = 8000, enqueue_newest: int = 30):
    """一次性：把历史消息里的链接登记进 chat_link，并排队解析最新的若干条。"""
    con = db()
    _ensure_link_tables(con)
    meta = con.execute("SELECT value FROM meta WHERE key='links_backfilled'").fetchone()
    if meta and meta[0] == "1":
        con.close()
        return 0
    rows = con.execute("SELECT id, chat_user, ts, content FROM message "
                       "WHERE content LIKE '%http%' ORDER BY ts DESC LIMIT ?",
                       (limit_msgs,)).fetchall()
    n = 0
    for r in rows:
        for url, title in _extract_links(r["content"]):
            cur = con.execute("SELECT 1 FROM chat_link WHERE url=?", (url,)).fetchone()
            if cur:
                continue
            con.execute("INSERT OR IGNORE INTO chat_link(url,chat,msg_id,msg_ts,title,platform)"
                        " VALUES(?,?,?,?,?,?)",
                        (url, r["chat_user"], r["id"], r["ts"] or 0,
                         (title or "")[:120], _refresh_platform(url)))
            n += 1
    con.execute("INSERT OR REPLACE INTO meta VALUES('links_backfilled','1')")
    con.commit()
    # 只把最新的若干条放进队列（避免一次把几百条视频全排进去）
    todo = con.execute("SELECT url FROM chat_link ORDER BY msg_ts DESC LIMIT ?",
                       (enqueue_newest,)).fetchall()
    unparsed = []
    for (u,) in todo:
        rr = con.execute("SELECT 1 FROM link_cache WHERE url=? AND IFNULL(text,'')<>''",
                         (u,)).fetchone()
        if not rr:
            unparsed.append(u)
    con.close()
    for u in unparsed:
        _LINK_QUEUE.put(u)
    return n


@app.get("/api/avatar")
def api_avatar(u: str):
    """联系人真实头像（微信 head_image.db）。找不到返回 404，前端回落字母头像。"""
    name = (u or "").strip()
    if not name:
        raise HTTPException(404, "no username")
    p = DECRYPTED_DIR / "head_image" / "head_image.db"
    if not p.exists():
        raise HTTPException(404, "no avatar db")
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
        con.execute("PRAGMA busy_timeout=10000")
        row = con.execute("SELECT image_buffer FROM head_image WHERE username=? "
                          "ORDER BY update_time DESC LIMIT 1", (name,)).fetchone()
        con.close()
    except sqlite3.OperationalError:
        raise HTTPException(404, "avatar db busy")
    if not row or not row[0]:
        raise HTTPException(404, "no avatar")
    buf = bytes(row[0])
    media = "image/jpeg"
    if buf[:8] == b"\x89PNG\r\n\x1a\n":
        media = "image/png"
    elif buf[:6] in (b"GIF87a", b"GIF89a"):
        media = "image/gif"
    elif buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        media = "image/webp"
    return Response(content=buf, media_type=media,
                    headers={"Cache-Control": "max-age=604800"})


@app.get("/api/links")
def api_links(n: int = 200, q: str = ""):
    """链接中心：聊天里出现过的链接（收到即登记，后台解析入库）。"""
    con = db()
    _ensure_link_tables(con)
    rows = con.execute("""
        SELECT l.url, l.chat, l.msg_id, l.msg_ts, l.title, l.platform, l.state,
               c.title AS c_title, c.kind AS c_kind, LENGTH(IFNULL(c.text,'')) AS c_len,
               CASE WHEN l.chat = 'sns' THEN '朋友圈'
                    ELSE (SELECT COALESCE(NULLIF(display_name,''), username) FROM chat
                          WHERE username = l.chat) END AS chat_name
        FROM chat_link l LEFT JOIN link_cache c ON c.url=l.url
        ORDER BY l.msg_ts DESC LIMIT ?""", (max(50, min(n, 500)),)).fetchall()
    out = []
    _PLAT_MAP = {"xhs": "小红书", "bili": "哔哩哔哩", "article": "公众号", "generic": "网页"}
    for r in rows:
        parsed = (r["c_len"] or 0) > 0
        raw_kind = (r["platform"] or "").strip()
        plat = _PLAT_MAP.get(raw_kind)
        if not plat:
            u = r["url"]
            plat = ("小红书" if "xiaohongshu" in u else
                    "哔哩哔哩" if ("bilibili" in u or "b23.tv" in u) else
                    "公众号" if "mp.weixin.qq.com" in u else (raw_kind or "网页"))
        out.append({"url": r["url"],
                    "title": (r["c_title"] or r["title"] or r["url"][:70])[:80],
                    "platform": plat,
                    "received": r["msg_ts"] or 0, "chat": r["chat"],
                    "chat_name": r["chat_name"] or r["chat"],
                    "msg_id": r["msg_id"], "parsed": parsed, "state": r["state"] or 0,
                    "chars": r["c_len"] or 0})
    con.close()
    if q:
        ql = q.lower()
        out = [x for x in out
               if ql in (x["title"] + x["chat_name"] + x["platform"]).lower()]
    return out


@app.get("/api/files")
def api_files(q: str = "", n: int = 200):
    rdb = resource_db()
    if not rdb:
        return []
    con = db()
    chat_ids = {r[0]: r[1] for r in rdb.execute("SELECT rowid, user_name FROM ChatName2Id")}
    rows = rdb.execute("""
        SELECT i.chat_id, i.message_local_id, i.message_create_time, d.size, d.packed_info
        FROM MessageResourceInfo i JOIN MessageResourceDetail d ON d.message_id = i.message_id
        WHERE i.message_local_type & 4294967295 = 49 AND length(d.packed_info) > 2
        ORDER BY i.message_create_time DESC LIMIT 3000""").fetchall()
    out = []
    for r in rows:
        fields = pb_fields(r["packed_info"])
        name = ""
        if fields:
            inner = pb_fields(fields[0][1])
            for tag, val in (inner or fields):
                if tag == 1 and val:
                    name = val.decode("utf-8", "ignore")
                    break
        if not name or (q and q.lower() not in name.lower()):
            continue
        username = chat_ids.get(r["chat_id"], "")
        cname = con.execute(
            "SELECT COALESCE(NULLIF(display_name,''), username) FROM chat WHERE username=?",
            (username,)).fetchone()
        out.append({"id": r["message_local_id"], "chat": username,
                    "chat_name": cname[0] if cname else username,
                    "ts": r["message_create_time"], "title": name, "size": r["size"]})
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- API: 同步
def _ensure_src_unique(con) -> None:
    """确保 (src_db,src_table,src_local_id) 唯一索引存在——否则 INSERT OR IGNORE
    完全不去重，每次同步都会把最近窗口再插一遍（曾造成"隔一会重复几条"）。

    idx_msg_src 可能是历史遗留的普通索引；普通索引不会被 CREATE UNIQUE INDEX IF NOT
    EXISTS 替换，必须先删掉、清存量重复、再建唯一索引。
    """
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_msg_src'"
                      ).fetchone()
    if row and "UNIQUE" in (row[0] or "").upper():
        return
    try:
        con.execute("DROP INDEX IF EXISTS idx_msg_src")
        for _try in range(3):
            # 保留每组 (src_db,src_table,src_local_id) 的最小 rowid，删除其余重复
            # （注意不能用 SELECT rowid ... GROUP BY ... HAVING COUNT(*)>1：每组只返回一行）
            con.execute("DELETE FROM message WHERE rowid NOT IN ("
                        "SELECT MIN(rowid) FROM message "
                        "GROUP BY src_db, src_table, src_local_id)")
            con.commit()
            try:
                con.execute("CREATE UNIQUE INDEX idx_msg_src "
                            "ON message(src_db, src_table, src_local_id)")
                con.commit()
                break
            except sqlite3.IntegrityError:
                con.rollback()      # 并发同步又插入重复：再清一轮
    except sqlite3.OperationalError:
        pass


def _name2id_maps(src):
    """Name2Id → (sender_map, name2id)；src 是任意可 .execute 的连接/游标。"""
    import hashlib as _h
    sender_map, name2id = {}, {}
    try:
        for rid, uname in src.execute("SELECT rowid, user_name FROM Name2Id"):
            name2id["Msg_" + _h.md5((uname or "").encode()).hexdigest()] = uname
            sender_map[rid] = uname or ""
    except sqlite3.OperationalError:
        pass
    return sender_map, name2id


def _flush_rows(con, own, db_label: str, t: str, chat_user: str,
                sender_map: dict, rows) -> tuple[int, int]:
    """处理一批源消息行(五元组 local_id,local_type,create_time,message_content,
    real_sender_id)，写入知识库 + 重建 FTS + 登记链接。返回 (新增数, 最大时间戳)。"""
    total = 0
    max_ts = 0
    batch = []
    for local_id, local_type, create_time, message_content, real_sender_id in rows:
        max_ts = max(max_ts, create_time or 0)
        try:
            from m0_import import decode_content
            content = decode_content(message_content)
        except Exception:
            content = message_content if isinstance(message_content, str) else ""
        if not content:
            continue
        sender = (sender_map.get(real_sender_id) or "").split(",")[0].strip()
        is_sender = 1 if sender == own else 0
        if "@chatroom" in chat_user and local_type == 1:
            mm = SENDER_PREFIX.match(content)
            if mm:
                prefix = mm.group(1).strip()
                if prefix.startswith("wxid_") or len(prefix) > 12:
                    if not is_sender:
                        sender = sender or prefix
                    content = content[mm.end():]
        batch.append((chat_user, is_sender, "" if is_sender else sender,
                      create_time, local_type, content[:20000],
                      db_label, t, local_id))
    if not batch:
        return 0, max_ts
    prev = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO message(chat_user,is_sender,sender,ts,type,content,"
        "src_db,src_table,src_local_id) VALUES(?,?,?,?,?,?,?,?,?)", batch)
    added = con.total_changes - prev     # 仅消息插入（此刻还没动 FTS）
    total += added
    if added:
        # 只重建"本次新插入"的这几条：撤回后微信会改写源库内容，
        # 已入库的 message.content 不会被覆盖（历史照旧显示原文），
        # 也不能让 FTS 被撤回提示覆盖。
        lids = [b[8] for b in batch]
        ph = ",".join("?" * len(lids))
        rows2 = con.execute(
            "SELECT id, content, chat_user, ts FROM message WHERE src_db=? "
            "AND src_table=? AND src_local_id IN (%s)" % ph,
            [db_label, t] + lids).fetchall()
        for mid, cnt, cu, mts in rows2:
            st = clean_search(1, cnt)
            if len(st.strip()) < 2:
                continue
            con.execute("DELETE FROM message_fts WHERE rowid=?", (mid,))
            con.execute("INSERT INTO message_fts(rowid, content, chat_user, is_group) "
                        "VALUES(?,?,?,?)",
                        (mid, st[:20000], cu, int("@chatroom" in cu)))
            # 收到消息即登记链接并排队解析（下次打开直接有正文）
            if "http" in (cnt or ""):
                _ensure_link_tables(con)
                for _u, _t in _extract_links(cnt):
                    con.execute(
                        "INSERT OR IGNORE INTO chat_link(url,chat,msg_id,msg_ts,"
                        "title,platform) VALUES(?,?,?,?,?,?)",
                        (_u, cu, mid, mts or 0, (_t or "")[:120],
                         _refresh_platform(_u)))
                    _LINK_QUEUE.put(_u)
    return total, max_ts


def _import_fast(decrypted_dir=None, only: set[str] | None = None,
                 watermark_override: int | None = None) -> int:
    """按时间窗轮询新消息（秒级）：只扫描 create_time >= 上次水位 - 30s 的行。

    (src_db,src_table,src_local_id) 唯一索引保证幂等；已入库的原始内容不会被撤回覆盖。
    only: 只扫这些源库名（如 {"message_0.db"}），None=全扫。
    watermark_override: 临时把扫描窗口起点前移（补漏重扫用），不回写水位。
    """
    from pathlib import Path as _P
    d = _P(decrypted_dir) if decrypted_dir else DECRYPTED_DIR
    con = db()
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5("
                "content, chat_user UNINDEXED, is_group UNINDEXED, tokenize='trigram')")
    _ensure_src_unique(con)
    r = con.execute("SELECT value FROM meta WHERE key='sync_watermark'").fetchone()
    watermark = int(r[0]) if r and str(r[0]).isdigit() else 0
    if watermark_override is not None:
        watermark = min(watermark, int(watermark_override)) if watermark else int(watermark_override)
    elif not watermark:
        r2 = con.execute("SELECT MAX(ts) FROM message").fetchone()
        watermark = int(r2[0] or 0) - 120
        con.execute("INSERT OR REPLACE INTO meta VALUES('sync_watermark', ?)", (str(watermark),))
        con.commit()
    own = ACCOUNT_ID.rsplit("_", 1)[0]
    total = 0
    newest = watermark
    for i in range(8):
        p = d / "message" / ("message_%d.db" % i)
        if not p.exists():
            continue
        if only is not None and p.name not in only:
            continue
        src = sqlite3.connect("file:%s?mode=ro" % p, uri=True, check_same_thread=False)
        src.row_factory = sqlite3.Row
        sender_map, name2id = _name2id_maps(src)
        tables = [x[0] for x in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
        for t in tables:
            chat_user = name2id.get(t, "")
            if not chat_user:
                continue
            cols = {x[1] for x in src.execute("PRAGMA table_info('%s')" % t)}
            if "create_time" not in cols:
                continue
            rs_col = "real_sender_id" if "real_sender_id" in cols else "NULL AS real_sender_id"
            try:
                rows = src.execute(
                    "SELECT local_id, local_type, create_time, message_content, %s "
                    "FROM [%s] WHERE create_time >= ? ORDER BY create_time"
                    % (rs_col, t), (watermark - 30,)).fetchall()
            except sqlite3.OperationalError:
                continue
            if not rows:
                continue
            added, mts = _flush_rows(con, own, "message_%d.db" % i, t, chat_user,
                                     sender_map,
                                     [(r["local_id"], r["local_type"], r["create_time"],
                                       r["message_content"], r["real_sender_id"])
                                      for r in rows])
            total += added
            newest = max(newest, mts)
        src.close()
    con.execute("INSERT OR REPLACE INTO meta VALUES('sync_watermark', ?)",
                (str(max(watermark, newest)),))
    # 更新会话 last_ts（保证列表按最新消息排序）
    con.execute("""UPDATE chat SET last_ts =
        (SELECT MAX(ts) FROM message m WHERE m.chat_user = chat.username)
        WHERE EXISTS (SELECT 1 FROM message m WHERE m.chat_user = chat.username
                      AND m.ts > chat.last_ts)""")
    con.commit()
    con.close()
    return total


_live_enc_keys: dict[str, tuple] = {}
_live_conns: dict[str, object] = {}
_live_schema: dict[str, tuple] = {}   # db path -> (Msg表数量, [(t,chat_user,rs_col)], sender_map)
_live_cursors: dict[str, int] = {}    # "db:table" -> rowid 游标(内存),写 meta 仅在变化时
_live_dirty: dict[str, tuple] = {}    # db path -> (mtime,size,wal_mtime,wal_size) 上次 sweep 指纹


def _live_conn(p: Path, enc_key: str):
    """打开(并缓存)一个加密库的只读 SQLCipher 连接——跨 tick 复用，
    只读连接会自动看到 WAL 里的新提交。"""
    import sqlcipher3
    key = str(p)
    con = _live_conns.get(key)
    if con is not None:
        try:
            con.execute("SELECT 1").fetchone()
            return con
        except Exception:          # noqa: BLE001 连接失效,重建
            try:
                con.close()
            except Exception:      # noqa: BLE001
                pass
            _live_conns.pop(key, None)
    con = sqlcipher3.connect(f"file:{p.as_posix()}?mode=ro", uri=True,
                             check_same_thread=False, timeout=3)
    cur = con.cursor()
    cur.execute("PRAGMA key = \"x'%s'\"" % enc_key)
    _live_conns[key] = con
    return con


def _import_live() -> int:
    """WeFlow 式实时导入：sqlcipher3 直接打开**加密中的** message_*.db（原生读
    WAL，新消息无需等 checkpoint、无需先解密成文件），按 rowid 游标增量导入。

    返回新增条数；sqlcipher3 不可用返回 -1（调用方回退 fastsync 路径）。
    游标存 knowledge.db 的 meta（live_rid:<db>:<table>），首次运行初始化为
    当前 MAX(rowid) 并兼做一次时间窗补漏（幂等，不漏不重）。
    """
    try:
        import sqlcipher3
    except ImportError:
        return -1
    import hashlib
    import keyvault
    con = db()
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5("
                "content, chat_user UNINDEXED, is_group UNINDEXED, tokenize='trigram')")
    _ensure_src_unique(con)
    r = con.execute("SELECT value FROM meta WHERE key='sync_watermark'").fetchone()
    watermark = int(r[0]) if r and str(r[0]).isdigit() else 0
    own = ACCOUNT_ID.rsplit("_", 1)[0]
    total = 0
    newest = watermark
    try:
        master = keyvault.load_key(ACCOUNT_ID)
        if not master:
            con.close()
            raise RuntimeError("live: no stored key")
    except Exception as exc:      # noqa: BLE001
        con.close()
        raise RuntimeError(f"live: no key ({exc})")
    for i in range(8):
        p = ACCOUNT_DIR / "db_storage" / "message" / ("message_%d.db" % i)
        if not p.exists():
            continue
        try:
            stat = p.stat()
            wal_p = Path(str(p) + "-wal")
            try:
                wstat = wal_p.stat() if wal_p.exists() else None
            except OSError:
                wstat = None
            fp = (stat.st_mtime, stat.st_size,
                  wstat.st_mtime if wstat else 0, wstat.st_size if wstat else 0)
            first_run = str(p) not in _live_schema
            if not first_run and _live_dirty.get(str(p)) == fp:
                continue                     # 该库自上次 sweep 无变化
            _live_dirty[str(p)] = fp
        except OSError:
            continue
        try:
            ck = _live_enc_keys.get(str(p))
            salt = p.read_bytes()[:16]
            if not ck or ck[0] != salt:
                enc_key = hashlib.pbkdf2_hmac("sha512", master, salt, 256000, dklen=32).hex()
                _live_enc_keys[str(p)] = (salt, enc_key)
            else:
                enc_key = ck[1]
            src = _live_conn(p, enc_key)
            cur = src.cursor()
            cur.row_factory = None
            cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                        "AND name LIKE 'Msg_%'")
            tcnt = cur.fetchone()[0]
            cached = _live_schema.get(str(p))
            if cached and cached[0] == tcnt:
                table_infos, sender_map = cached[1], cached[2]
            else:
                sender_map, name2id = _name2id_maps(cur)
                table_infos = []
                for t in [x[0] for x in cur.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name LIKE 'Msg_%'")]:
                    chat_user = name2id.get(t, "")
                    if not chat_user:
                        continue
                    cols = {x[1] for x in cur.execute("PRAGMA table_info('%s')" % t)}
                    if "create_time" not in cols:
                        continue
                    rs_col = ("real_sender_id" if "real_sender_id" in cols
                              else "NULL AS real_sender_id")
                    table_infos.append((t, chat_user, rs_col))
                _live_schema[str(p)] = (tcnt, table_infos, sender_map)
            for t, chat_user, rs_col in table_infos:
                rkey = "live_rid:%s:%s" % (p.name, t)
                ckey = "%s:%s" % (p.name, t)
                rrow = _live_cursors.get(ckey)
                if rrow is None:
                    dbrow = con.execute("SELECT value FROM meta WHERE key=?",
                                        (rkey,)).fetchone()
                    rrow = int(dbrow[0]) if dbrow else None
                    _live_cursors[ckey] = rrow
                cur.execute("SELECT MAX(rowid) FROM [%s]" % t)
                mx = cur.fetchone()[0] or 0
                if rrow is None:
                    # 首次:初始化游标 + 时间窗补漏(幂等)
                    _live_cursors[ckey] = mx
                    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (rkey, str(mx)))
                    rows = cur.execute(
                        "SELECT local_id, local_type, create_time, message_content, %s "
                        "FROM [%s] WHERE create_time >= ? ORDER BY rowid"
                        % (rs_col, t), (watermark - 30,)).fetchall()
                    added, mts = _flush_rows(con, own, p.name, t, chat_user, sender_map, rows)
                    total += added
                    newest = max(newest, mts)
                elif mx > rrow:
                    rows = cur.execute(
                        "SELECT local_id, local_type, create_time, message_content, %s "
                        "FROM [%s] WHERE rowid > ? ORDER BY rowid" % (rs_col, t),
                        (rrow,)).fetchall()
                    added, mts = _flush_rows(con, own, p.name, t, chat_user, sender_map, rows)
                    _live_cursors[ckey] = mx
                    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (rkey, str(mx)))
                    total += added
                    newest = max(newest, mts)
                    if added:
                        try:
                            con.commit()   # 尽早落库:SSE 立即可见,也缩短写锁持有
                        except Exception:  # noqa: BLE001
                            pass
            # 连接缓存复用，不 close
        except Exception as exc:  # noqa: BLE001 单库失败不影响其它库
            _realtime["live_err"] = f"{p.name}: {exc}"[:120]
            continue
    con.execute("INSERT OR REPLACE INTO meta VALUES('sync_watermark', ?)",
                (str(max(watermark, newest)),))
    con.execute("""UPDATE chat SET last_ts =
        (SELECT MAX(ts) FROM message m WHERE m.chat_user = chat.username)
        WHERE EXISTS (SELECT 1 FROM message m WHERE m.chat_user = chat.username
                      AND m.ts > chat.last_ts)""")
    con.commit()
    con.close()
    return total


def _run_sync():
    import subprocess
    try:
        _sync_state.update(running=True, log="增量解密中（ChatTrace）...")
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, r'%s'); "
             "sys.argv=['chattrace','data','decrypt','--account-dir', r'%s']; "
             "from chattrace.cli import main; sys.exit(main() or 0)"
             % (CHATTRACE_SRC, ACCOUNT_DIR)],
            capture_output=True, text=True, timeout=1200)
        _sync_state["log"] = (r.stdout or "")[-400:] + (r.stderr or "")[-200:]
        _sync_state["log"] += "\n增量导入中..."
        n = _import_incremental()
        _sync_state["log"] += f"\n新增 {n} 条"
        _sync_state["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        _sync_state["log"] += f"\n失败: {e}"
    finally:
        _sync_state["running"] = False


def _import_incremental() -> int:
    """按 (src_db, src_table, src_local_id) 幂等增量导入新消息。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
    from m0_import import decode_content  # 复用解压逻辑
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS ai_qa(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "chat_id TEXT, question TEXT, answer TEXT, cited_refs TEXT, ts INTEGER)")
    con.execute("DELETE FROM message WHERE rowid NOT IN ("
                "SELECT MIN(rowid) FROM message GROUP BY src_db, src_table, src_local_id)")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_src ON message(src_db, src_table, src_local_id)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(content, "
                "chat_user UNINDEXED, is_group UNINDEXED, tokenize='trigram')")
    _ensure_src_unique(con)
    total = 0
    own_wxid = ACCOUNT_ID.rsplit("_", 1)[0]
    for i in range(8):
        p = DECRYPTED_DIR / "message" / f"message_{i}.db"
        if not p.exists():
            continue
        src = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        sender_map: dict[int, str] = {}
        try:
            rows_n2 = src.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
            name2id = {f"Msg_{hashlib_md5(r[1])}": r[1] for r in rows_n2}
            sender_map = {r[0]: (r[1] or "") for r in rows_n2}
        except sqlite3.OperationalError:
            name2id = {}
        tables = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
        for t in tables:
            cols = [r[1] for r in src.execute(f"PRAGMA table_info('{t}')")]
            if "message_content" not in cols or "create_time" not in cols:
                continue
            rs_col = "real_sender_id" if "real_sender_id" in cols else "NULL AS real_sender_id"
            for row in src.execute(f"SELECT local_id, local_type, create_time, message_content, "
                                   f"{rs_col} FROM [{t}]"):
                if row["message_content"] is None:
                    continue
                content = decode_content(row["message_content"])
                if not content:
                    continue
                chat_user = name2id.get(t, "")
                if not chat_user:
                    continue
                sender = sender_map.get(row["real_sender_id"]) or ""
                sender = sender.split(",")[0].strip()
                is_sender = 1 if (sender and sender == own_wxid) else 0
                if "@chatroom" in chat_user and row["local_type"] == 1:
                    mm = SENDER_PREFIX.match(content)
                    if mm:
                        prefix = mm.group(1).strip()
                        if prefix.startswith("wxid_") or len(prefix) > 12:
                            if not is_sender:
                                sender = sender or prefix
                            content = content[mm.end():]
                cur = con.execute(
                    "INSERT OR IGNORE INTO message(chat_user,is_sender,sender,ts,type,content,"
                    "src_db,src_table,src_local_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (chat_user, is_sender, "" if is_sender else sender, row["create_time"],
                     row["local_type"], content[:20000], f"message_{i}.db", t, row["local_id"]))
                if cur.rowcount:
                    rid = cur.lastrowid
                    con.execute("INSERT INTO message_fts(rowid, content, chat_user, is_group) "
                                "VALUES(?,?,?,?)",
                                (rid, content[:20000], chat_user,
                                 int("@chatroom" in chat_user)))
                    total += 1
        src.close()
    con.commit()
    # 刷新会话时间线
    con.execute("UPDATE chat SET last_ts = (SELECT MAX(ts) FROM message m "
                "WHERE m.chat_user = chat.username) WHERE last_ts < "
                "(SELECT COALESCE(MAX(ts),0) FROM message m WHERE m.chat_user = chat.username)")
    con.commit()
    return total


def hashlib_md5(s: str) -> str:
    import hashlib
    return hashlib.md5(s.encode()).hexdigest()


@app.post("/api/sync")
def api_sync():
    if _sync_state["running"]:
        return {"ok": False, "msg": "同步进行中"}
    threading.Thread(target=_run_sync, daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------- API: 网页/公众号文章
def _link_con():
    con = sqlite3.connect(str(KNOWLEDGE_DB), timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("CREATE TABLE IF NOT EXISTS link_cache("
                "url TEXT PRIMARY KEY, title TEXT, text TEXT, ts INTEGER)")
    cols = {r[1] for r in con.execute("PRAGMA table_info(link_cache)")}
    if "msg_ts" not in cols:          # 链接在聊天里收到的日期
        con.execute("ALTER TABLE link_cache ADD COLUMN msg_ts INTEGER DEFAULT 0")
    if "kind" not in cols:            # article / xhs / bili
        con.execute("ALTER TABLE link_cache ADD COLUMN kind TEXT DEFAULT 'article'")
    if "extra" not in cols:           # 平台额外信息（图片、视频、分段文字）JSON
        con.execute("ALTER TABLE link_cache ADD COLUMN extra TEXT DEFAULT ''")
    return con


def _save_link(con, url: str, title: str, text: str, kind: str = "article",
               extra: dict | None = None):
    """写 link_cache + link_fts（FTS 表可能不存在，忽略即可）。"""
    for _try in range(5):
        try:
            con.execute("INSERT OR REPLACE INTO link_cache(url,title,text,ts,kind,extra) "
                        "VALUES(?,?,?,?,?,?)",
                        (url, title, text, int(time.time()), kind,
                         json.dumps(extra or {}, ensure_ascii=False)))
            try:
                con.execute("DELETE FROM link_fts WHERE url=?", (url,))
                con.execute("INSERT INTO link_fts(text, title, url) VALUES(?,?,?)",
                            (text, title, url))
            except sqlite3.OperationalError:
                pass
            con.commit()
            return
        except sqlite3.OperationalError:
            time.sleep(1.0)


def fetch_platform_link(url: str, force: bool = False) -> dict:
    """小红书/B站深度解析（图文 OCR、视频字幕或语音转写），带缓存与后台任务。"""
    from link_media import detect, start_job, job
    kind = detect(url)
    con = _link_con()
    if not force:
        r = con.execute("SELECT title, text, extra FROM link_cache WHERE url=? AND kind=?",
                        (url, kind)).fetchone()
        if r and (r[1] or "").strip():
            con.close()
            extra = {}
            try:
                extra = json.loads(r[2] or "{}")
            except Exception:
                extra = {}
            return {"url": url, "title": r[0] or "", "text": r[1], "chars": len(r[1]),
                    "kind": kind, "cached": True, **extra}
    st = start_job(url)
    if st.get("state") == "done" and st.get("result"):
        res = st["result"]
        if res.get("error"):
            con.close()
            return {"url": url, "title": "", "text": "", "chars": 0,
                    "kind": kind, "error": res["error"]}
        extra = {k: res.get(k) for k in ("parts", "images", "video", "platform",
                                          "owner", "page", "pages", "truncated")}
        _save_link(con, url, res.get("title", ""), res.get("text", ""), kind, extra)
        con.close()
        return {"url": url, "title": res.get("title", ""), "text": res.get("text", ""),
                "chars": res.get("chars", 0), "kind": kind, "cached": False, **extra}
    con.close()
    return {"url": url, "title": "", "text": "", "chars": 0, "kind": kind,
            "pending": True, "step": st.get("step", ""), "progress": st.get("progress", 0)}



def fetch_article(url: str, force: bool = False) -> dict:
    """抓取公众号/网页正文（带缓存）。小红书/B站走深度解析（OCR/字幕/ASR）。"""
    import requests
    from bs4 import BeautifulSoup
    # 统一 URL 形式：消息里是 &amp;，与缓存键必须一致，否则"解析过却查不到"
    try:
        from link_media import detect, normalize_url
        url = normalize_url(url)
        _kind = detect(url)
    except Exception:
        _kind = "generic"
    if _kind in ("xhs", "bili"):
        return fetch_platform_link(url, force)
    con = _link_con()
    if not force:
        r = con.execute("SELECT title, text FROM link_cache WHERE url=?", (url,)).fetchone()
        if r and (r[1] or "").strip():
            con.close()
            return {"url": url, "title": r[0] or "", "text": r[1],
                    "chars": len(r[1]), "cached": True}
    try:
        resp = requests.get(url, timeout=20, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")})
        # 编码：优先页面声明的 charset，其次响应头，最后 utf-8
        # （apparent_encoding 在微信文章上会猜成 Windows-1254 导致乱码）
        head_bytes = resp.content[:3000].decode("latin-1", "ignore")
        declared = None
        _i = head_bytes.lower().find("charset=")
        if _i >= 0:
            _tail = head_bytes[_i + 8:_i + 48]
            _m = re.match("[" + chr(34) + chr(39) + " ]*([A-Za-z0-9_-]+)", _tail)
            if _m:
                declared = _m.group(1)
        resp.encoding = declared or resp.encoding or "utf-8"
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype and not any(k in ctype for k in ("text/html", "application/xhtml",
                                                  "text/plain", "application/json")):
            con.close()
            return {"url": url, "error": f"非网页内容（{ctype.split(';')[0]}）",
                    "title": "", "text": "", "chars": 0}
        html = resp.text
    except Exception as e:
        con.close()
        return {"url": url, "error": f"抓取失败：{type(e).__name__} {str(e)[:80]}",
                "title": "", "text": "", "chars": 0}

    soup = BeautifulSoup(html, "lxml")
    title = ""
    if "mp.weixin.qq.com" in url:
        t = soup.select_one("#activity-name") or soup.select_one("h1")
        title = (t.get_text(strip=True) if t else "")
        body = soup.select_one("#js_content")
        if body is not None:
            for bad in body.select("script,style"):
                bad.decompose()
            text = body.get_text(chr(10), strip=True)
        else:
            text = ""
    else:
        og = soup.select_one('meta[property="og:title"]')
        title = (og.get("content") if og else "") or (soup.title.get_text(strip=True)
                                                      if soup.title else "")
        for bad in soup(["script", "style", "nav", "header", "footer", "aside", "noscript"]):
            bad.decompose()
        body = (soup.select_one("article") or soup.select_one("main") or soup.body or soup)
        text = body.get_text(chr(10), strip=True)

    text = re.sub(chr(10) + "{3,}", chr(10) + chr(10), text).strip()[:60000]
    # 质量校验：替换符/控制字符占比过高说明是二进制或编码错误
    _bad = sum(1 for ch in text[:4000] if ch == chr(65533) or (ord(ch) < 9))
    if text.lstrip().startswith(("{", "[")) or len(text.strip()) < 200:
        con.close()
        return {"url": url, "error": "正文过短或为接口返回，跳过",
                "title": "", "text": "", "chars": 0}
    if _bad / max(1, min(len(text), 4000)) > 0.02:
        con.close()
        return {"url": url, "error": "正文疑似二进制/乱码，已跳过",
                "title": "", "text": "", "chars": 0}
    if not title:
        head = text.split(chr(10), 1)[0][:80]
        title = head
    _save_link(con, url, title, text, "article")
    con.close()
    return {"url": url, "title": title, "text": text, "chars": len(text), "cached": False}


@app.get("/api/link/meta")
def api_link_meta(url: str, force: int = 0):
    d = fetch_article(url, bool(force))
    return {"url": d["url"], "title": d.get("title", ""), "chars": d.get("chars", 0),
            "cached": d.get("cached", False), "error": d.get("error")}


@app.get("/api/link/text")
def api_link_text(url: str, force: int = 0):
    d = fetch_article(url, bool(force))
    if d.get("pending"):
        return {"pending": True, "step": d.get("step", ""), "progress": d.get("progress", 0),
                "kind": d.get("kind", ""), "title": "", "text": "", "chars": 0}
    if d.get("error"):
        raise HTTPException(400, d["error"])
    return {"title": d["title"], "text": d["text"][:40000], "chars": d["chars"],
            "kind": d.get("kind", "article"), "platform": d.get("platform", ""),
            "images": d.get("images") or [], "video": d.get("video") or {},
            "parts": d.get("parts") or {}, "owner": d.get("owner", ""),
            "truncated": d.get("truncated", False)}


@app.get("/api/link/job")
def api_link_job(url: str):
    """平台解析进度（前端轮询）；完成时顺手落库（link_cache + link_fts）。"""
    from link_media import job, detect
    st = job(url)
    out = {"state": st.get("state", "none"), "step": st.get("step", ""),
           "progress": st.get("progress", 0)}
    res = st.get("result") or {}
    if st.get("state") == "done" and not res.get("error"):
        out.update({"title": res.get("title", ""), "text": res.get("text", ""),
                    "chars": res.get("chars", 0), "kind": res.get("kind", ""),
                    "platform": res.get("platform", ""), "images": res.get("images") or [],
                    "video": res.get("video") or {}, "parts": res.get("parts") or {},
                    "owner": res.get("owner", ""), "truncated": res.get("truncated", False)})
        # 落库一次（幂等，避免每次轮询重复写）
        if not st.get("saved"):
            try:
                kind = detect(url)
                con = _link_con()
                _save_link(con, url, res.get("title", ""), res.get("text", ""), kind,
                           {k: res.get(k) for k in ("parts", "images", "video", "platform",
                                                    "owner", "page", "pages", "truncated")})
                con.close()
                from link_media import _JOBS, _JOBS_LOCK
                with _JOBS_LOCK:
                    if url in _JOBS:
                        _JOBS[url]["saved"] = True
            except Exception:
                pass
    elif st.get("state") == "error":
        out["error"] = (res or {}).get("error") or st.get("step", "解析失败")
    return out


@app.get("/api/link/img")
def api_link_img(u: str):
    """图片代理：避免防盗链/混合内容，统一转 https 并带 Referer。"""
    import requests as _rq
    from fastapi.responses import Response
    u = (u or "").strip()
    if not u.lower().startswith("http"):
        raise HTTPException(400, "bad url")
    host = urllib.parse.urlparse(u).netloc
    ref = "https://www.bilibili.com/" if "bili" in host or "hdslb" in host else \
          ("https://www.xiaohongshu.com/" if "xhscdn" in host else "https://" + host + "/")
    headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/120 Safari/537.36"),
               "Referer": ref}
    try:
        r = _rq.get(u, headers=headers, timeout=20)
        if not r.ok:
            raise HTTPException(502, f"图片 {r.status_code}")
        return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"),
                        headers={"Cache-Control": "max-age=86400"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"图片获取失败：{type(e).__name__}")


# ---------------------------------------------------------------- API: 索引/后台任务
_jobs: dict[str, dict] = {
    "vectors": {"running": False, "log": "", "count": 0},
    "ocr": {"running": False, "log": "", "count": 0},
    "asr": {"running": False, "log": "", "count": 0},
    "links": {"running": False, "log": "", "count": 0},
    "snsocr": {"running": False, "log": "", "count": 0},
    "files": {"running": False, "log": "", "count": 0},
    "rescan": {"running": False, "log": "", "count": 0},
}
_job_procs: dict[str, object] = {}

# 新人首次配置后的自动后台解析清单（setup 写 meta 开关，服务启动时按顺序执行；
# 文字/语音/文件=全量历史，图片/公众号/朋友圈=最近一年，其余在设置页手动续跑）
_BACKLOG_JOBS: list[tuple[str, str, list[str]]] = [
    ("asr", "run_asr.py", []),
    ("files", "run_files.py", []),
    ("ocr", "run_ocr.py", ["--recent-days", "365"]),
    ("links", "index_links.py", ["--recent-days", "365"]),
    ("snsocr", "run_sns_ocr.py", []),
]


def _run_job(name: str, script: str, extra: list[str] | None = None):
    import subprocess
    st = _jobs[name]
    st.update(running=True, log="启动中…")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / script)]
            + (extra or []),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="ignore", bufsize=1)
        _job_procs[name] = proc
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                st["log"] = (st["log"] + chr(10) + line)[-2000:]
            import re as _re
            m = _re.search(r"\[进度\]\s*(\d+)/(\d+)", line)
            if m:
                st["count"] = int(m.group(1))
        proc.wait()
        st["log"] = (st["log"] + chr(10) + "完成（退出码 " + str(proc.returncode) + "）")[-2000:]
    except Exception as e:  # noqa: BLE001
        st["log"] = (st["log"] + chr(10) + "失败: " + str(e))[-2000:]
    finally:
        st["running"] = False


def _auto_backlog_boot():
    """meta['auto_backlog']='1' 时（setup 建库后写入），按顺序自动跑首批解析任务。"""
    try:
        con = db()
        r = con.execute("SELECT value FROM meta WHERE key='auto_backlog'").fetchone()
        con.close()
    except Exception:
        return
    if not r or str(r[0]) != "1":
        return
    try:
        con = db()
        con.execute("INSERT OR REPLACE INTO meta VALUES('auto_backlog', 'started')")
        con.commit()
        con.close()
    except Exception:
        pass
    for name, script, extra in _BACKLOG_JOBS:
        if _jobs[name]["running"]:
            continue
        try:
            con = db()
            r2 = con.execute("SELECT value FROM meta WHERE key='auto_backlog'").fetchone()
            con.close()
        except Exception:
            r2 = None
        if not r2 or str(r2[0]) != "started":
            return          # 用户在设置页停止过 → 取消剩余自动序列
        _run_job(name, script, extra)      # 顺序执行,避免多任务互相抢 CPU


_auto_sync = {"thread": None, "last": "", "next": ""}
_realtime = {"thread": None, "last": "", "added": 0, "error": "", "tick": ""}


def _decrypt_quick() -> str:
    """调用 ChatTrace 增量解密（只重解有变化的库）。

    Windows 走子进程 CLI；mac/Linux 用进程内 DecryptService（其解密器是
    纯 Python，跨平台；CLI 里的抓钥部分才是 Windows 专属）。
    """
    try:
        _ensure_key_usable()
    except Exception:                 # noqa: BLE001 自检失败不阻塞解密
        pass
    if os.name != "nt":
        import keyvault
        from chattrace.service.decryptor import DecryptService
        pw = keyvault.load_key(ACCOUNT_ID)
        if not pw:
            return "no stored key (import via keyvault.py --import)"
        svc = DecryptService(ACCOUNT_DIR / "db_storage", pw, DECRYPTED_DIR)
        svc.run(incremental=True)
        return "in-process decrypt done"
    import subprocess
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s'); "
         "sys.argv=['chattrace','data','decrypt','--account-dir', r'%s']; "
         "from chattrace.cli import main; sys.exit(main() or 0)"
         % (CHATTRACE_SRC, ACCOUNT_DIR)],
        capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=600)
    return ((r.stdout or "") + (r.stderr or ""))[-300:]


def _db_fingerprint() -> dict:
    """message_*.db 及其 -wal 的 (mtime, size) 指纹，用于检测"微信刚写了新消息"。"""
    fp = {}
    msg_dir = ACCOUNT_DIR / "db_storage" / "message"
    try:
        for p in msg_dir.glob("message_*.db*"):
            try:
                st = p.stat()
                fp[p.name] = (int(st.st_mtime), st.st_size)
            except OSError:
                continue
    except OSError:
        pass
    return fp


def _realtime_loop():
    """实时同步（WeFlow 式）：监听微信数据库文件变化即时触发，另加兜底定时轮询。

    流程：检测到 message_*.db / -wal 变化 → 解抖 3 秒 → 增量解密 + 快速导入
    （+ 自动 OCR/转写）。没有变化时只做低频兜底（默认 120 秒）。
    """
    last_fp: dict = {}
    last_sync = 0.0
    while True:
        try:
            conf = load_conf()
            rt = conf.get("sync", {})
            if not rt.get("realtime"):
                time.sleep(10)
                continue
            interval = max(15, int(rt.get("realtime_sec", 45)))
            now = time.time()

            changed = _db_fingerprint() != last_fp and bool(last_fp)
            due = (now - last_sync) >= interval
            # 文件一有变化就 2 秒后同步（原来是 20 秒，用户反馈"等半天才出来"）
            if (changed and now - last_sync >= 0.3) or due:
                if changed:
                    _realtime["trigger"] = "文件变化"
                elif due:
                    _realtime["trigger"] = "定时兜底"
                    _realtime["due_n"] = _realtime.get("due_n", 0) + 1
                if _sync_state["running"]:
                    time.sleep(5)
                    continue
                _sync_state["running"] = True
                try:
                    _realtime["tick"] = time.strftime("%H:%M:%S")
                    n = -1
                    # 自愈：每 10 次定时兜底做一次 24h 窗口重扫（幂等），防漏消息
                    if not changed and _realtime.get("due_n", 0) % 10 == 0:
                        try:
                            n = _import_fast(
                                watermark_override=int(time.time()) - 86400)
                            _realtime["selfheal"] = f"{n} 条 @ " + time.strftime("%H:%M")
                        except Exception as exc:      # noqa: BLE001
                            _realtime["live_err"] = str(exc)[:120]
                            n = -1
                    if n < 0:
                        # 最快路径：sqlcipher3 直连加密库(原生读 WAL,免 checkpoint、
                        # 免解密成文件)按 rowid 游标增量导入——WeFlow 同款思路
                        if rt.get("fast_realtime", True):
                            try:
                                n = _import_live()
                            except Exception as exc:      # noqa: BLE001
                                _realtime["live_err"] = str(exc)[:120]
                                n = -1
                    if n < 0:
                        # 回退：进程内页级增量解密(fastsync)+按需导入，再不行子进程
                        updated: list[str] = []
                        try:
                            import fastsync
                            import keyvault
                            info = keyvault.load_key(ACCOUNT_ID)
                            if info:
                                updated = fastsync.fast_sync_message_dbs(
                                    ACCOUNT_DIR, DECRYPTED_DIR, info)
                        except Exception as exc:      # noqa: BLE001 快路径失败兜底
                            _realtime["fast_err"] = str(exc)[:100]
                            updated = []
                        if updated:
                            n = _import_fast(only={Path(r).name for r in updated
                                                   if r.startswith("message/")})
                        else:
                            _decrypt_quick()
                            n = _import_fast()
                    _realtime["added"] += n
                    if n > 0 and rt.get("fast_realtime", True):
                        # live 路径导入极快但不产出解密文件——媒体定位/OCR 需要，
                        # 顺手刷新 dirty 库（页级增量，仅变化页），不阻塞消息可见
                        try:
                            import fastsync
                            import keyvault
                            info = keyvault.load_key(ACCOUNT_ID)
                            if info:
                                fastsync.fast_sync_message_dbs(
                                    ACCOUNT_DIR, DECRYPTED_DIR, info)
                        except Exception:     # noqa: BLE001
                            pass
                    if rt.get("media_ocr", True) or rt.get("media_asr", True):
                        from auto_media import process_new_media
                        # 只处理最近 48h 的新消息：历史积压交给设置页批量任务，
                        # 否则每轮全速 OCR/ASR 会吃满 CPU，拖慢整个程序
                        stats = process_new_media(
                            db(), ocr_cap=30 if rt.get("media_ocr", True) else 0,
                            asr_cap=10 if rt.get("media_asr", True) else 0,
                            video_cap=2 if rt.get("media_asr", True) else 0,
                            since=int(time.time()) - 48 * 3600)
                        _realtime["media"] = stats
                    try:      # 文件正文预热：收到即提取进 file_text，快速回答可直接引用
                        warm_file_texts(db(), cap=3)
                    except Exception:
                        pass
                    _realtime["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    _realtime["error"] = ""
                    last_fp = _db_fingerprint()
                    last_sync = time.time()
                except Exception as exc:      # noqa: BLE001
                    _realtime["error"] = str(exc)[:120]
                finally:
                    _sync_state["running"] = False
                time.sleep(2)
                continue
            # 首轮先记录指纹
            if not last_fp:
                last_fp = _db_fingerprint()
            time.sleep(0.35)
        except Exception:
            time.sleep(30)


@app.get("/api/messages/stream")
def api_messages_stream(chat: str, after_id: int = 0):
    """SSE 推送：有新消息立即下发（客户端保持一条连接，不做轮询）。

    取数方式：服务端每 1.5 秒比对一次 id 水位（本地 SQLite，开销极小），
    有新增就推；无新增每 15 秒发一次心跳。前端 EventSource 断线会自动重连。
    """
    from fastapi.responses import StreamingResponse

    def gen():
        last = int(after_id or 0)
        idle = 0
        yield "retry: 3000\n\n"
        while True:
            try:
                con = db()
                rows = con.execute("SELECT * FROM message WHERE chat_user=? AND id > ? "
                                   "ORDER BY id LIMIT 40", (chat, last)).fetchall()
                if rows:
                    out = _recall_pass(rows, con)
                    last = max(last, max(r["id"] for r in rows))
                    if out:
                        payload = json.dumps({"messages": out, "max_id": last},
                                             ensure_ascii=False)
                        yield "data: " + payload + "\n\n"
                        idle = 0
                else:
                    idle += 1
                    if idle >= 10:
                        idle = 0
                        yield ": ping\n\n"
                con.close()
            except Exception:
                yield ": error\n\n"
            time.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/messages/new")
def api_messages_new(chat: str, after_id: int = 0, limit: int = 60):
    """聊天视图增量刷新：返回比 after_id 新的消息（按 id 正序），用于秒级显示新消息。"""
    con = db()
    rows = con.execute("SELECT * FROM message WHERE chat_user=? AND id > ? "
                       "ORDER BY id LIMIT ?", (chat, after_id, min(limit, 200))).fetchall()
    out = _recall_pass(rows, con)
    # 若该会话最新消息没变多，也返回最新 id 便于前端对齐
    top = con.execute("SELECT MAX(id) FROM message WHERE chat_user=?", (chat,)).fetchone()
    last_ts = con.execute("SELECT MAX(ts) FROM message WHERE chat_user=?", (chat,)).fetchone()
    con.close()
    return {"messages": out, "max_id": int((top and top[0]) or 0),
            "last_ts": int((last_ts and last_ts[0]) or 0)}


@app.get("/api/sync/realtime")
def api_realtime_get():
    conf = load_conf()
    st = conf.get("sync", {})
    media = _realtime.get("media") or {}
    return {"realtime": bool(st.get("realtime")),
            "realtime_sec": int(st.get("realtime_sec", 60)),
            "last": _realtime["last"], "added": _realtime["added"],
            "media_ocr": media.get("ocr", 0), "media_asr": media.get("asr", 0),
            "error": _realtime["error"], "tick": _realtime["tick"]}


@app.post("/api/sync/realtime")
def api_realtime_set(payload: dict):
    conf = load_conf()
    st = conf.setdefault("sync", {})
    if "realtime" in payload:
        st["realtime"] = bool(payload["realtime"])
    if payload.get("realtime_sec"):
        st["realtime_sec"] = max(15, int(payload["realtime_sec"]))
    if "media_ocr" in payload:
        st["media_ocr"] = bool(payload["media_ocr"])
    if "media_asr" in payload:
        st["media_asr"] = bool(payload["media_asr"])
    CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "realtime": bool(st.get("realtime")),
            "realtime_sec": int(st.get("realtime_sec", 60))}


def _auto_sync_loop():
    while True:
        try:
            conf = load_conf()
            st = conf.get("sync", {})
            if not st.get("auto"):
                time.sleep(60)
                continue
            interval = max(5, int(st.get("interval_min", 30))) * 60
            if not _sync_state["running"]:
                _run_sync()
                _auto_sync["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _auto_sync["next"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + interval))
            for _ in range(interval // 10):
                st2 = load_conf().get("sync", {})
                if not st2.get("auto"):
                    break
                time.sleep(10)
        except Exception:
            time.sleep(60)


@app.get("/api/sync/auto")
def api_sync_auto_get():
    conf = load_conf()
    st = conf.get("sync", {})
    return {"auto": bool(st.get("auto")), "interval_min": int(st.get("interval_min", 30)),
            "last": _auto_sync["last"], "next": _auto_sync["next"]}


@app.post("/api/sync/auto")
def api_sync_auto_set(payload: dict):
    conf = load_conf()
    st = conf.setdefault("sync", {})
    if "auto" in payload:
        st["auto"] = bool(payload["auto"])
    if payload.get("interval_min"):
        st["interval_min"] = max(5, int(payload["interval_min"]))
    CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "auto": bool(st.get("auto")),
            "interval_min": int(st.get("interval_min", 30))}


def _count_or_zero(con, sql: str) -> int:
    try:
        return con.execute(sql).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


_job_totals_cache = {"ts": 0.0, "data": {}}


def _job_totals(con) -> dict:
    """批量任务的总数提示（文件/朋友圈总数需要 Python 侧解析内容，300s 缓存）。"""
    now = time.time()
    if now - _job_totals_cache["ts"] < 300:
        return _job_totals_cache["data"]
    data: dict[str, int] = {}
    try:
        data["asr"] = con.execute(
            "SELECT COUNT(*) FROM message WHERE type IN (34, 43)").fetchone()[0]
        try:
            from textutil import appmsg_subtype
            data["files"] = sum(
                1 for (content,) in con.execute("SELECT content FROM message WHERE type=49")
                if appmsg_subtype(content or "") == "6")
        except Exception:      # noqa: BLE001
            data["files"] = 0
        try:
            import json as _json
            import sns as sns_mod
            data["snsocr"] = sum(
                1 for (mj,) in con.execute("SELECT media_json FROM sns_post")
                for m in _json.loads(mj or "[]")
                if sns_mod._media_is_real(m) and not sns_mod._media_is_video(m))
        except Exception:      # noqa: BLE001
            data["snsocr"] = 0
    except Exception:          # noqa: BLE001
        return _job_totals_cache.get("data") or {}
    _job_totals_cache.update(ts=now, data=data)
    return data


@app.get("/api/jobs")
def api_jobs():
    con = db()
    vec_count = 0
    try:
        vec_count = vector.embedded_count(vconn())
    except Exception:
        pass
    total = con.execute("SELECT COUNT(*) FROM message WHERE noise=0").fetchone()[0]
    ocr_total = con.execute("SELECT COUNT(*) FROM message WHERE type=3").fetchone()[0]
    try:
        ocr_done = con.execute("SELECT COUNT(*) FROM media_text WHERE kind='ocr'").fetchone()[0]
        asr_done = con.execute("SELECT COUNT(*) FROM media_text WHERE kind='asr'").fetchone()[0]
    except sqlite3.OperationalError:
        ocr_done = asr_done = 0
    jt = _job_totals(con)
    return {"vector": {**_jobs["vectors"], "done": vec_count, "total": total,
                       "model": vector.available()},
            "ocr": {**_jobs["ocr"], "done": ocr_done, "total": ocr_total},
            "asr": {**_jobs["asr"], "done": asr_done, "total": jt.get("asr", 0)},
            "links": {**_jobs["links"],
                      "done": _count_or_zero(con, "SELECT COUNT(*) FROM link_cache "
                                                  "WHERE length(text)>50"),
                      "total": _count_or_zero(con, "SELECT COUNT(*) FROM link_cache")},
            "files": {**_jobs["files"],
                      "done": _count_or_zero(con, "SELECT COUNT(*) FROM file_text "
                                                  "WHERE length(text)>50"),
                      "total": jt.get("files", 0)},
            "snsocr": {**_jobs["snsocr"],
                       "done": _count_or_zero(con, "SELECT COUNT(*) FROM sns_media_ocr"),
                       "total": jt.get("snsocr", 0)},
            "rescan": {**_jobs["rescan"], "done": _jobs["rescan"].get("count", 0)},
            "context_around": int(load_conf().get("qa", {}).get("context_around", 3))}


@app.post("/api/jobs/{name}/start")
def api_job_start(name: str, payload: dict | None = None):
    if name not in _jobs:
        raise HTTPException(404, "未知任务")
    if _jobs[name]["running"]:
        return {"ok": False, "msg": "任务进行中"}
    script = {"vectors": "build_vectors.py", "ocr": "run_ocr.py",
              "asr": "run_asr.py", "links": "index_links.py",
              "snsocr": "run_sns_ocr.py", "files": "run_files.py",
              "rescan": "rescan.py"}[name]
    extra = []
    if payload and payload.get("limit"):
        extra = ["--limit", str(int(payload["limit"]))]
    if payload and payload.get("days"):
        flag = "--days" if name == "rescan" else "--recent-days"
        extra = [flag, str(int(payload["days"]))]
    threading.Thread(target=_run_job, args=(name, script, extra), daemon=True).start()
    return {"ok": True}


@app.post("/api/jobs/{name}/stop")
def api_job_stop(name: str):
    proc = _job_procs.get(name)
    if proc and _jobs[name]["running"]:
        try:
            proc.terminate()
        except Exception:
            pass
        _jobs[name]["running"] = False
        # 停掉自动解析序列中的任一任务 = 取消剩余自动序列(之后可在设置页手动逐个开)
        if any(name == b[0] for b in _BACKLOG_JOBS):
            try:
                con = db()
                con.execute("INSERT OR REPLACE INTO meta VALUES('auto_backlog', 'stopped')")
                con.commit()
                con.close()
            except Exception:
                pass
        return {"ok": True}
    return {"ok": False, "msg": "未在运行"}


# ---------------------------------------------------------------- API: 朋友圈
@app.get("/api/sns/posts")
def api_sns_posts(offset: int = 0, limit: int = 50, q: str = "", author: str = ""):
    con = db()
    try:
        data = sns_mod.list_posts(con, offset=offset, limit=min(limit, 200),
                                  q=q, author=author)
    finally:
        con.close()
    return data


@app.get("/api/sns/post/{tid}")
def api_sns_post(tid: str):
    con = db()
    try:
        post = sns_mod.get_moment(con, tid)
    finally:
        con.close()
    if post is None:
        raise HTTPException(404, "朋友圈不存在")
    return post


@app.get("/api/sns/image")
def api_sns_image(tid: str, idx: int = 0):
    con = db()
    try:
        row = con.execute("SELECT xml_id, media_json FROM sns_post WHERE tid=?",
                          (tid,)).fetchone()
    finally:
        con.close()
    if row is None:
        raise HTTPException(404, "朋友圈不存在")
    try:
        media = json.loads(row["media_json"] or "[]")
    except (TypeError, ValueError):
        media = []
    entry = next((m for m in media if int(m.get("i", -1)) == idx), None)
    if entry is None:
        raise HTTPException(404, "媒体不存在")
    if sns_mod._media_is_video(entry):
        raise HTTPException(404, "视频请用 /api/sns/video")
    img = sns_mod.image_bytes(entry, row["xml_id"] or "")
    if not img:
        raise HTTPException(404, "图片不可得（本地无缓存且 CDN 拉取失败）")
    blob, mime = img
    return Response(content=blob, media_type=mime,
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/sns/video")
def api_sns_video(tid: str, idx: int = 0):
    """朋友圈/视频号视频：本地无缓存，按需从 CDN 拉取并解密（ISAAC64 前 128KB）。"""
    con = db()
    try:
        row = con.execute("SELECT media_json FROM sns_post WHERE tid=?", (tid,)).fetchone()
    finally:
        con.close()
    if row is None:
        raise HTTPException(404, "朋友圈不存在")
    try:
        media = json.loads(row["media_json"] or "[]")
    except (TypeError, ValueError):
        media = []
    entry = next((m for m in media if int(m.get("i", -1)) == idx), None)
    if entry is None:
        raise HTTPException(404, "媒体不存在")
    vid = sns_mod.video_bytes(entry)
    if not vid:
        raise HTTPException(404, "视频不可得（CDN 拉取失败或链接已过期）")
    blob, mime = vid
    return Response(content=blob, media_type=mime,
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/sns/status")
def api_sns_status():
    con = db()
    try:
        st = sns_mod.stats(con)
    finally:
        con.close()
    st["sync_running"] = _sns_state["running"]
    st["last_loop_sync"] = _sns_state["last"]
    st["loop_error"] = _sns_state["error"]
    return st


@app.post("/api/sns/sync")
def api_sns_sync():
    if _sns_state["running"]:
        return {"ok": False, "msg": "同步进行中"}
    _sns_state["running"] = True

    def _run():
        try:
            st = sns_mod.sync_sns(db())
            if st.get("ok"):
                try:
                    sns_mod.build_sns_vec()
                except Exception:     # noqa: BLE001
                    pass
                try:
                    _register_sns_links(db())
                except Exception:     # noqa: BLE001
                    pass
                sns_mod.process_sns_ocr(db(), cap=60)
            _sns_state["error"] = "" if st.get("ok") else (st.get("detail") or "")
            _sns_state["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as exc:      # noqa: BLE001
            _sns_state["error"] = str(exc)[:150]
        finally:
            _sns_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "同步已开始"}


# ---------------------------------------------------------------- API: 数据库密钥
@app.get("/api/key/status")
def api_key_status():
    ok = _stored_key_ok(0)
    import keyscan
    return {"hmac_ok": ok, "detail": _key_status["detail"],
            "wechat_running": bool(keyscan.weixin_pids())}


@app.post("/api/key/scan")
def api_key_scan():
    """对运行中的微信做内存扫钥（免退出）；微信刚启动时命中率最高。Windows 专属。"""
    if os.name != "nt":
        raise HTTPException(400, "内存扫钥仅支持 Windows；mac 请用 /api/key/import 手动导入")
    import keyscan
    result = keyscan.capture_and_store(
        ACCOUNT_DIR, ACCOUNT_DIR / "db_storage" / "message" / "message_0.db")
    _key_status["ts"] = 0.0
    return result


@app.post("/api/key/import")
def api_key_import(payload: dict):
    """手动导入 64 hex 主密钥（mac 主路径）：HMAC 实测通过后入库。"""
    import keyvault
    r = keyvault.import_manual_key(ACCOUNT_DIR, str(payload.get("key_hex") or ""))
    _key_status["ts"] = 0.0
    return r


# ---------------------------------------------------------------- 静态前端
if FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="ui")
