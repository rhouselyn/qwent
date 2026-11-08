# -*- coding: utf-8 -*-
"""一期后端：微信镜像 UI 的全部 API。

运行：python server.py  （默认 http://127.0.0.1:8721）
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import re
import queue
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
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
import privacy  # noqa: E402
import sns as sns_mod  # noqa: E402
import platform_text  # noqa: E402  平台文案（系统消息微信/QQ 双版本）
import qq_messages  # noqa: E402    QQ 机器人消息留存
import qq_local  # noqa: E402       QQ NT 本地记录（消息/联系人/文件）

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
    # 全项目有 20+ 处 SQL 依赖 message.noise，但历史建表脚本没写这一列；
    # 从零初始化（换电脑恢复备份 / 新建库）时会整片报错，这里幂等补上。
    mcols = {r[1] for r in con.execute("PRAGMA table_info(message)")}
    if mcols and "noise" not in mcols:
        try:
            con.execute("ALTER TABLE message ADD COLUMN noise INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass


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


def _quote_oneline(s: str, cap: int = 200) -> str:
    """引用预览压成单行，避免界面出现多行转义残渣。"""
    return re.sub(r"\s+", " ", s or "").strip()[:cap]


def _refer_text(ref: str, cap: int = 200) -> str:
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
        return _quote_oneline(_xml_unescape(mt.group(1)), cap)
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
            return _quote_oneline(_xml_unescape(mt.group(1)), cap)
        m = XML_QUOTE_MEDIA.search(c)
        if m:
            return _QUOTE_MEDIA_LABEL[m.group(1)]
        # 彻底去标签后再压行：内层可能还有双重转义的标签，解不净就当未知消息
        plain = _quote_oneline(re.sub(r"<[^>]+>", " ", _xml_unescape(c)), cap)
        if "<" in plain or "&lt;" in plain:
            return "[消息]"
        return plain
    return _quote_oneline(c, cap)


def has_xml_text(content: str) -> bool:
    c = _SENDER_PREFIX_XML.sub("", (content or "").lstrip()) if (content or "").startswith("wxid_") else (content or "").lstrip()
    return c.startswith(("<?xml", "<msg", "<appmsg"))


_RECORD_HINT = re.compile(r"support\.weixin\.qq\.com/.*(readtemplate|unsupport)")


def parse_record(c: str) -> dict | None:
    """解析合并转发的聊天记录（appmsg subtype 19 的 <recorditem>）。

    结构：<recorditem><![CDATA[<recordinfo><info>摘要</info>
          <datalist count=N><dataitem datatype=".."><sourcename/><datadesc/>
          <link/><title/>...
    返回 {count, info, items:[{type,name,time,text,title,url}]}；解析失败返回 None。
    """
    m = re.search(r"<recorditem>(.*?)</recorditem>", c, re.S)
    if not m:
        return None
    raw = m.group(1)
    cd = re.search(r"<!\[CDATA\[(.*?)\]\]>", raw, re.S)
    inner = cd.group(1) if cd else raw
    inner = _xml_unescape(inner)
    if "<recordinfo" not in inner:
        return None
    info_m = re.search(r"<info>(.*?)</info>", inner, re.S)
    info = ""
    if info_m:
        rows = [x.strip() for x in _xml_unescape(info_m.group(1)).splitlines() if x.strip()]
        info = chr(10).join(rows)[:600]
    items = []
    for it in re.finditer(r"<dataitem([^>]*)>(.*?)</dataitem>", inner, re.S):
        attrs, body = it.group(1), it.group(2)
        dt = re.search(r'datatype="(\d+)"', attrs)
        dtype = int(dt.group(1)) if dt else 0

        def pick(tag, _body=body):
            mm = re.search(r"<" + tag + r">(.*?)</" + tag + r">", _body, re.S)
            if not mm:
                return ""
            v = re.sub(r"<!\[CDATA\[(.*?)\]\]>", lambda x: x.group(1),
                       mm.group(1), flags=re.S)
            return _xml_unescape(v).strip()

        item = {
            "type": dtype,
            "name": pick("sourcename")[:40],
            "time": pick("sourcetime")[:24],
            "text": pick("datadesc")[:400],
            "title": pick("title")[:200],
            "url": pick("link")[:800],
        }
        # datatype: 1=文本 3=图片 5=链接 19=嵌套聊天记录 34=语音 43=视频 37/47=表情
        if not (item["text"] or item["title"] or item["url"]):
            continue
        items.append(item)
    if not items:
        return None
    return {"count": len(items), "info": info, "items": items[:200]}


def _appmsg_dict(c: str) -> dict:
    """把一段 appmsg XML 解析成 {kind, title, des, url}。"""
    sub = re.search(r"<type>(\d+)</type>", c)
    subtype = sub.group(1) if sub else ""
    mt, md_, mu = XML_TITLE.search(c), XML_DES.search(c), XML_URL.search(c)
    title = mt.group(1) if mt else ""
    if subtype == "19":
        rec = parse_record(c)
        if rec:
            return {"kind": "record", "title": title or "聊天记录",
                    "des": (rec["info"] or "")[:200], "record": rec}
    if subtype == "62":
        return {"text": title or "[拍了拍]"}
    if subtype in ("57",):
        ref = XML_REFER.search(c)
        rdisp = rtitle = rlong = ""
        if ref:
            x = XML_DISPLAYNAME.search(ref.group(1))
            rdisp = _quote_oneline(_xml_unescape(x.group(1))) if x else ""
            rtitle = _refer_text(ref.group(1))
            rlong = _refer_text(ref.group(1), 500)
        return {"kind": "quote", "title": title,
                "title_full": _xml_unescape(title).strip()[:1000],
                "refer": {"who": rdisp, "title": rtitle, "text": rlong or rtitle}}
    link_url = _xml_unescape(mu.group(1)) if mu else ""
    if _RECORD_HINT.search(link_url or ""):
        link_url = ""      # 微信"不支持查看"的占位地址,不是真实内容
    return {"kind": "link", "title": title or "[消息]",
            "des": (md_.group(1) if md_ else "")[:200],
            "url": link_url}
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
        # 系统提示（改群名 / 邀请入群 / 撤回 / 加好友…）按真实微信措辞清洗，
        # 同时给出 QQ 版本，前端按当前平台选用，避免 QQ 界面冒出微信文案。
        recall = t == 10000 and "revokemsg" in c
        wt, qt = platform_text.system_pair(c, recall=recall)
        out["system"] = wt or "系统消息"
        out["system_qq"] = qt or "系统消息"
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
        if subtype == "19":                          # 合并转发聊天记录
            rec = parse_record(c)
            if rec:
                out["kind"] = "record"
                out["title"] = _quote_oneline(_xml_unescape(title)) or "聊天记录"
                out["des"] = (rec.get("info") or "")[:200]
                out["record"] = rec
                return out
        if subtype == "62":                          # 拍一拍
            out["text"] = clean_display(62, c) or "[拍了拍]"
        elif subtype == "57":
            ref = XML_REFER.search(c)
            rdisp = rtitle = rlong = ""
            if ref:
                x = XML_DISPLAYNAME.search(ref.group(1))
                rdisp = _quote_oneline(_xml_unescape(x.group(1))) if x else ""
                rtitle = _refer_text(ref.group(1))
                rlong = _refer_text(ref.group(1), 500)
            out["kind"] = "quote"
            out["title"] = _quote_oneline(_xml_unescape(title))
            out["title_full"] = _xml_unescape(title).strip()[:1000]
            out["refer"] = {"who": rdisp, "title": rtitle,
                            "text": rlong or rtitle}
        elif subtype in ("5", "6"):
            fn = XML_FILENAME.search(c)
            total = XML_TOTALEN.search(c)
            out["kind"] = "file" if (total or subtype == "6") else "link"
            out["title"] = title
            out["url"] = url
            out["size"] = int(total.group(1)) if total else 0
            out["des"] = des[:200]
        else:
            link_url = _xml_unescape(url)
            if _RECORD_HINT.search(link_url or ""):
                link_url = ""     # 微信"不支持查看该内容"的占位地址
            out["kind"] = "link"
            out["title"] = title or "[消息]"
            out["url"] = link_url
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
    # type 列是 64 位组合值(LikeFlag 等标志在高位),必须掩码取低 32 位才是真实
    # 消息类型——否则前端 switch(type) 匹配不到 49(链接/文件/合并转发)、
    # 媒体判断也全部失效,整条消息渲染空白。
    mtype = (m["type"] or 0) & 0xFFFFFFFF
    if mtype in (34, 43):           # 语音/视频消息带上转写文本
        asr = _media_text(con, m["id"], "asr")
        if asr:
            parsed["asr_text"] = asr
    elif mtype == 3:                # 图片消息带上 OCR 文本
        ocr = _media_text(con, m["id"], "ocr")
        if ocr:
            parsed["ocr_text"] = ocr
    return {
        "id": m["id"], "ts": m["ts"], "type": mtype,
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
    # 按真实时间排序(ts,id 决胜),不能按 id:补漏重扫会把旧消息以新 id 入库,
    # 按 id 排会让它们占住列表尾部、把真正最新的消息挤出第一屏。
    con = db()
    if before:
        b = con.execute("SELECT ts FROM message WHERE id=?", (before,)).fetchone()
        bts = b["ts"] if b else 0
        rows = con.execute(
            "SELECT * FROM message WHERE chat_user=? "
            "AND (ts < ? OR (ts = ? AND id < ?)) ORDER BY ts DESC, id DESC LIMIT ?",
            (chat, bts, bts, before, limit)).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM message WHERE chat_user=? ORDER BY ts DESC, id DESC LIMIT ?",
            (chat, limit)).fetchall()
    rows = _recall_pass(list(reversed(rows)), con, drop_noise=False)
    if rows:
        r0 = rows[0]
        has_more = 1 if con.execute(
            "SELECT 1 FROM message WHERE chat_user=? "
            "AND (ts < ? OR (ts = ? AND id < ?)) LIMIT 1",
            (chat, r0["ts"], r0["ts"], r0["id"])).fetchone() else 0
    else:
        has_more = 0
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
    """通讯录：只用有真实名字的联系人，按拼音首字母分组排序。

    搜索同时匹配备注/昵称/别名/微信号，以及微信自带的拼音全拼与首字母
    （输入 wangzixin 或 wzx 也能找到人）。
    """
    con = db()
    like = f"%{q}%"
    rows = con.execute("""
        SELECT c.username,
               TRIM(COALESCE(NULLIF(TRIM(c.remark),''), NULLIF(TRIM(c.nick_name),''),
                             NULLIF(TRIM(c.alias),''), '')) AS name,
               COALESCE(c.initial_letter, '') AS letter
        FROM contact c
        WHERE IFNULL(c.local_type,0) IN (1, 2, 6) AND IFNULL(c.delete_flag,0)=0
          AND TRIM(COALESCE(NULLIF(TRIM(c.remark),''), NULLIF(TRIM(c.nick_name),''),
                            NULLIF(TRIM(c.alias),''), '')) <> ''
          AND (? = '' OR c.username LIKE ? OR c.nick_name LIKE ? OR c.remark LIKE ?
               OR c.alias LIKE ? OR c.quan_pin LIKE ? OR c.pin_yin_initial LIKE ?)
        ORDER BY CASE WHEN INSTR('ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                                 IFNULL(c.initial_letter,'')) > 0 THEN 0
                      WHEN INSTR('0123456789', IFNULL(c.initial_letter,'')) > 0 THEN 1
                      ELSE 2 END,
                 c.initial_letter, name
        LIMIT ?""", (q, like, like, like, like, like, like, limit)).fetchall()
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


def _model_error_text(exc) -> str:
    """把模型调用异常翻译成用户能看懂的原因，避免把 SSL/DNS 原文抛给用户。"""
    s = str(exc)
    low = s.lower()
    if "ssl" in low or "unexpected_eof" in low or "certificate" in low:
        return ("模型接口连不上：TLS 握手失败。通常是代理/VPN 未连通、节点失效，"
                "或公司网络拦截了外网 HTTPS；请检查网络后重试。")
    if "timed out" in low or "timeout" in low:
        return "模型接口响应超时。网络较慢或模型服务繁忙，请稍后重试。"
    if "401" in low or "403" in low or "unauthorized" in low or "forbidden" in low:
        return "模型接口拒绝访问：请检查设置页的 API Key 和模型权限。"
    if "429" in low or "rate limit" in low:
        return "模型接口限流：请稍等片刻，或降低提问频率。"
    if "getaddrinfo" in low or "name or service not known" in low or "nodename" in low:
        return "模型服务域名解析失败：当前 DNS/代理不可用，请检查网络设置。"
    if "connection refused" in low or "connection reset" in low or "econnreset" in low:
        return "模型接口连接被拒绝或重置：请检查代理/VPN 是否正常。"
    return "模型接口调用失败：" + s.replace("urlopen error ", "")[:120]


def _question_core(text: str) -> str:
    """从送给 Agent 的完整 prompt 里取出真正的问题（离线兜底判断意图用）。"""
    m = re.search(r"【当前问题】\s*(.+)$", text or "", re.S)
    return (m.group(1) if m else text or "").strip()


def _offline_answer(question: str, ctx_lines: list[str], con, exc) -> str:
    """模型不可用时的离线兜底：解释原因，并给本地检索结果（朋友圈优先）。"""
    head = "[模型暂时不可用] " + _model_error_text(exc)
    core = _question_core(question)
    if any(k in core for k in ("朋友圈", "动态", "moments", "moment")):
        try:
            import sns as sns_mod
            items = sns_mod.search_moments(con, "", days=30, limit=8)
            if items:
                rows = []
                for it in items:
                    ts = time.strftime("%m-%d %H:%M", time.localtime(it.get("ts") or 0))
                    who = it.get("who") or it.get("author_name") or "朋友圈"
                    text = re.sub(r"\s+", " ", it.get("text") or "")[:160]
                    rows.append(f"• {ts} {who}：{text}")
                return (head + "\n\n以下为本地检索到的最近朋友圈（未经 AI 归纳）：\n"
                        + "\n".join(rows))
        except Exception:      # noqa: BLE001
            pass
    if ctx_lines:
        rows = []
        for line in ctx_lines[:5]:
            clean = re.sub(r"\s+", " ", line)
            rows.append("• " + clean[:300])
        return (head + "\n\n以下为本地检索到的相关记录（未经 AI 归纳）：\n"
                + "\n".join(rows))
    return head + "\n\n没有检索到可用的本地记录；请检查网络/代理后重试。"


def call_llm(conf, messages_payload):
    import urllib.request
    llm = conf["llm"]
    messages_payload = privacy.redact_messages(conf, messages_payload)
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
    # platform 区分微信 / QQ：两套 Agent 的会话历史互不混用
    con.execute("""CREATE TABLE IF NOT EXISTS ai_session(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        title TEXT DEFAULT '',
        platform TEXT DEFAULT 'wechat',
        created INTEGER, updated INTEGER)""")
    scols = {r[1] for r in con.execute("PRAGMA table_info(ai_session)")}
    if "name" not in scols:      # 作用域的显示名（群名 / 文件名 / 文章标题）
        con.execute("ALTER TABLE ai_session ADD COLUMN name TEXT DEFAULT ''")
    if "platform" not in scols:
        con.execute("ALTER TABLE ai_session ADD COLUMN platform TEXT DEFAULT 'wechat'")
        con.execute("UPDATE ai_session SET platform='wechat' WHERE IFNULL(platform,'')=''")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ai_session_platform ON ai_session(platform)")
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


def get_or_create_session(con, scope: str, new: bool = False, name: str = "",
                          platform: str = "wechat") -> int:
    platform = platform if platform in ("wechat", "qq") else "wechat"
    if not new:
        r = con.execute("SELECT id FROM ai_session WHERE scope=? "
                        "AND IFNULL(platform,'wechat')=? ORDER BY id DESC LIMIT 1",
                        (scope, platform)).fetchone()
        if r:
            if name:
                con.execute("UPDATE ai_session SET name=? WHERE id=? AND IFNULL(name,'')=''",
                            (name[:60], r[0]))
            return r[0]
    cur = con.execute("INSERT INTO ai_session(scope,title,platform,created,updated,name) "
                      "VALUES(?,?,?,?,?,?)",
                      (scope, "", platform, int(time.time()), int(time.time()),
                       (name or "")[:60]))
    return cur.lastrowid


def touch_session(con, sid: int, title: str | None = None):
    con.execute("UPDATE ai_session SET updated=? WHERE id=?", (int(time.time()), sid))
    if title:
        con.execute("UPDATE ai_session SET title=? WHERE id=? AND IFNULL(title,'')=''",
                    (title[:40], sid))


def claim_session_platform(sid: int, platform: str) -> None:
    """把通道自己持有的旧会话归到微信 / QQ 名下（升级前建的会话默认是微信）。"""
    platform = platform if platform in ("wechat", "qq") else "wechat"
    con = db()
    try:
        ensure_qa_table(con)
        con.execute("UPDATE ai_session SET platform=? WHERE id=? "
                    "AND IFNULL(platform,'wechat')<>?", (platform, int(sid), platform))
        con.commit()
    finally:
        con.close()


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
    platform = (payload.get("platform")
                if payload.get("platform") in ("wechat", "qq", "both") else "wechat")
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
            raw = str(qid)
            if raw.startswith("qq-"):
                raw = raw[3:]
            try:
                qid = int(raw)
            except (TypeError, ValueError):
                continue
            if platform == "qq":
                # QQ 引用 id 指向 qq_local_message，不能查微信 message 表
                r = con.execute("SELECT id,ts,sender_name,is_sender,text "
                                "FROM qq_local_message WHERE id=?", (qid,)).fetchone()
                if r:
                    quoted.append({"id": r[0], "ts": r[1],
                                   "who": "我" if r[3] else (r[2] or ""),
                                   "text": (r[4] or "")[:400]})
                continue
            r = con.execute("SELECT * FROM message WHERE id=?", (qid,)).fetchone()
            if r:
                b = agent_mod._msg_brief(con, r)
                quoted.append({"id": b["id"], "ts": b["ts"], "who": b["who"], "text": b["text"]})
        chat_name = None
        if chat:
            if platform == "qq":
                row = con.execute("SELECT name FROM qq_chat WHERE conv=?", (chat,)).fetchone()
            else:
                row = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat "
                                  "WHERE username=?", (chat,)).fetchone()
            chat_name = row[0] if row else chat
        res = agent_mod.run(q, conf0, chat_ctx=chat, chat_name=chat_name,
                            quoted=quoted, history=history,
                            explorer=bool(payload.get("explorer")), platform=platform)
        ans, refs, trace = res["answer"], res["refs"], res["trace"]
        ensure_qa_table(con)
        sid = session_id or get_or_create_session(con, chat or "assistant",
                                                  new=want_new_session, platform=platform)
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
            if platform == "qq":
                row = con.execute("SELECT name FROM qq_chat WHERE conv=?", (chat,)).fetchone()
            else:
                row = con.execute("SELECT COALESCE(NULLIF(display_name,''), username) FROM chat "
                                  "WHERE username=?", (chat,)).fetchone()
            chat_name = row[0] if row else chat
        try:
            res = agent_mod.run(q, conf0, chat_ctx=chat, chat_name=chat_name,
                                history=history,
                                explorer=bool(payload.get("explorer")), platform=platform)
            if res.get("answer"):
                ensure_qa_table(con)
                sid = session_id or get_or_create_session(con, chat or "assistant",
                                                          new=want_new_session,
                                                          platform=platform)
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
        # QQ 文件中心：fdoc 直接带本机路径
        if fdoc.get("path"):
            path = Path(str(fdoc["path"]))
            if not path.is_file():
                path = None
        else:
            path = locate_file(fdoc.get("chat", ""), int(fdoc.get("local_id", 0)),
                               int(fdoc.get("ts", 0)), fdoc.get("name", ""))
        if not path:
            return {"mode": "empty", "refs": [],
                    "answer": f"文件「{fdoc.get('name','')}」不在本地磁盘上（可能已被清理或未下载）。"}
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
                ans = ("[模型暂时不可用] " + _model_error_text(e) +
                       chr(10) + chr(10) +
                       "文件已读取，但暂时无法生成回答；可在预览面板直接查看正文。")
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
        sys_p = agent_mod._platform_text(sys_p, platform)
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
            ans = _offline_answer(q, ctx_lines, con, e)
    else:
        # 检索模式：只给前 5 段摘录，完整内容交给「引用来源」折叠区
        ans = ("**检索模式（未配置 API Key）：命中 %d 段对话**，以下为前 5 段摘录。"
               "完整内容与前后文请展开下方「引用来源」。\n\n%s"
               % (len(blocks), "\n".join(ctx_lines[:5])))
    ensure_qa_table(con)
    sid = session_id or get_or_create_session(con, chat or "assistant",
                                              new=want_new_session, platform=platform)
    con.execute("INSERT INTO ai_qa(chat_id, question, answer, cited_refs, ts, scope, "
                "session_id) VALUES(?,?,?,?,?,?,?)",
                (chat or "assistant", q, ans,
                 json.dumps(refs, ensure_ascii=False), int(time.time()),
                 "chat" if chat else "global", sid))
    touch_session(con, sid, q)
    con.commit()
    return {"mode": mode, "answer": ans, "refs": refs, "session_id": sid}


@app.get("/api/ai/sessions")
def api_ai_sessions(platform: str = "wechat"):
    """AI 会话树：按作用域分组（全局 / 各群聊·私聊 / 各文件 / 各链接），互不混用。
    platform 区分微信 / QQ 两套 Agent，默认微信（兼容旧调用）。"""
    platform = platform if platform in ("wechat", "qq") else "wechat"
    con = db()
    ensure_qa_table(con)
    rows = con.execute("""
        SELECT s.id, s.scope, s.title, s.created, s.updated, s.name,
               (SELECT COUNT(*) FROM ai_qa q WHERE q.session_id=s.id
                 AND IFNULL(q.hidden,0)=0) cnt
        FROM ai_session s
        WHERE IFNULL(s.platform,'wechat')=?
        ORDER BY s.updated DESC""", (platform,)).fetchall()
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
                    "sessions": g["sessions"],
                    "count": len(g["sessions"])})
    out.sort(key=lambda x: (x["scope"] != "assistant",
                            -max([y["ts"] for y in x["sessions"]] or [0])))
    return out


def _ai_scope_hash(text: str) -> str:
    """与前端 hashStr 一致：djb2 + 0x7fffffff，按 UTF-16 单元计算后转 base36。"""
    h = 5381
    for ch in str(text or ""):
        cp = ord(ch)
        if cp > 0xFFFF:
            cp -= 0x10000
            units = (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF))
        else:
            units = (cp,)
        for u in units:
            h = ((h << 5) + h + u) & 0x7fffffff
    if not h:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while h:
        h, r = divmod(h, 36)
        out = digits[r] + out
    return out


@app.get("/api/ai/scope/context")
def api_ai_scope_context(scope: str):
    """把 AI 会话作用域还原成提问所需的 chat / file / link 上下文。

    前端在 AI 助手里选中"会话/文件/链接"后，只靠 scope 字符串无法带上文件正文
    或链接地址，这里统一在服务端解析，避免把两套规则散落在多个页面上。
    """
    scope = (scope or "").strip()
    if not scope or scope == "assistant" or scope.startswith("moment:"):
        return {}
    con = db()
    if scope.startswith("file:"):
        parts = scope.split(":", 2)
        chat = parts[1] if len(parts) > 1 else ""
        try:
            lid = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            lid = 0
        if not chat or lid <= 0:
            return {}
        # QQ 文件中心：file:qqfile:<id>，直接给出本机路径
        if chat == "qqfile":
            row = con.execute("SELECT name,ts,path,size FROM qq_file WHERE id=?",
                              (lid,)).fetchone()
            if not row:
                return {}
            return {"file": {"chat": "qqfile", "local_id": lid, "ts": row[1] or 0,
                             "name": row[0] or "文件", "path": row[2] or "",
                             "size": row[3] or 0}}
        name, ts = "", 0
        try:
            _ensure_file_text(con)
            row = con.execute("SELECT name FROM file_text WHERE key=?",
                              (f"{chat}:{lid}",)).fetchone()
            if row and row[0]:
                name = row[0]
        except Exception:
            pass
        row = con.execute(
            "SELECT ts, content FROM message WHERE chat_user=? AND src_local_id=? "
            "AND (type & 0xFFFFFFFF)=49 ORDER BY ts DESC LIMIT 1", (chat, lid)).fetchone()
        if row:
            ts = int(row[0] or 0)
            if not name:
                m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                              row[1] or "", re.S)
                name = m.group(1).strip() if m else ""
        return {"file": {"chat": chat, "local_id": lid, "ts": ts,
                         "name": name or "文件"}}
    if scope.startswith("link:"):
        target = scope.split(":", 1)[1]
        try:
            from link_media import normalize_url
        except Exception:
            normalize_url = lambda x: x
        for url, title in con.execute("SELECT url, title FROM chat_link").fetchall():
            u = normalize_url(url)
            if _ai_scope_hash(u) == target:
                return {"link": {"url": u, "title": title or "链接"}}
        return {}
    return {"chat": scope}


@app.post("/api/ai/sessions")
def api_ai_session_new(payload: dict):
    con = db()
    ensure_qa_table(con)
    scope = payload.get("scope") or "assistant"
    name = (payload.get("name") or "").strip()
    platform = (payload.get("platform")
                if payload.get("platform") in ("wechat", "qq", "both") else "wechat")
    sid = get_or_create_session(con, scope, new=True, name=name, platform=platform)
    con.commit()
    return {"id": sid, "scope": scope, "name": name, "platform": platform}


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
            "platform": s["platform"] or "wechat",
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
            scope_name=None, platform: str = "wechat"):
    ensure_qa_table(con)
    key = chat or "assistant"
    sid = session_id or get_or_create_session(con, key, new=new_session,
                                              name=scope_name or "",
                                              platform=platform)
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
    # 后台预热任务总数（文件/朋友圈要逐条解析，可能几秒），索引页打开即有 n/m
    threading.Thread(target=_job_totals_bg, daemon=True).start()
    # 启动先同步一次联系人/会话：修复历史漏导（新增好友/会话没进列表）
    def _dirs_boot():
        try:
            _refresh_dirs()
        except Exception:      # noqa: BLE001
            pass
    threading.Thread(target=_dirs_boot, daemon=True).start()
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
    threading.Thread(target=_vec_loop, daemon=True).start()
    threading.Thread(target=_auto_backlog_boot, daemon=True).start()
    # QQ 自动同步：启动先增量入库一次，之后盯 NT 库指纹变化（新消息/文件/链接）
    threading.Thread(target=_qq_sync_loop, daemon=True).start()
    # 开机自启：首次运行自动登记（可在设置里关闭）
    threading.Thread(target=_autostart_ensure_default, daemon=True).start()
    # QQ 实时同步：文件通知 + WAL 帧补丁 + rowid 游标，新消息约 0.5~1 秒可查
    threading.Thread(target=_qq_rt_loop, daemon=True).start()
    # 媒体索引（6 万个文件，约 15 秒）启动就在后台建好，图片/文件路径读取时即补
    def _qq_media_boot():
        try:
            qq_local._media_index_cached(qq_local.pick_account(), allow_empty=True)
        except Exception:
            pass
    threading.Thread(target=_qq_media_boot, daemon=True).start()
    try:
        import qqbot as qqbot_mod
        qqbot_mod.apply_config(load_conf())
    except Exception as exc:      # noqa: BLE001 QQ 机器人配置异常不影响主服务
        print(f"[qqbot] startup failed: {exc}")


@app.on_event("shutdown")
def _on_shutdown():
    """退出时收尾：结束后台解析子进程、断开 QQ 网关、关闭常驻连接。

    各巡检线程是 daemon，进程退出即结束；这里只处理会留下外部进程/连接的资源。
    """
    for name, proc in list(_job_procs.items()):
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
        except Exception:      # noqa: BLE001
            pass
    for con in list(_live_conns.values()):
        try:
            con.close()
        except Exception:      # noqa: BLE001
            pass
    _live_conns.clear()
    _live_schema.clear()
    try:
        import qqbot as qqbot_mod
        qqbot_mod.shutdown()
    except Exception:      # noqa: BLE001
        pass


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


def _vec_loop():
    """新消息语义向量补嵌（独立线程，慢活不阻塞实时同步）。

    每 20 秒补一批（8 条）；嵌入 8 条约 2-3 秒。带 sqlite busy 重试。
    """
    con = None
    while True:
        try:
            conf = load_conf()
            if not conf.get("sync", {}).get("vec_realtime", True):
                time.sleep(30)
                continue
            if not vector.available():
                time.sleep(120)
                continue
            if con is None:
                con = vector.connect(str(KNOWLEDGE_DB))
            n = vector.refresh_media_vectors(con, cap=40)   # 先作废占位向量
            n += vector.backfill_new(con, cap=8)             # 再用媒体文字重嵌
            _realtime["vec"] = n
            _realtime["vec_err"] = ""
            time.sleep(2 if n else 20)
        except Exception as exc:          # noqa: BLE001
            _realtime["vec_err"] = str(exc)[:100]
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass
            con = None
            time.sleep(20)


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
            sns_mod.build_sns_vec()          # 无待嵌入时是廉价空查询,每轮跑保证收敛
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


# ---------------------------------------------------------------- API: 出站脱敏
@app.get("/api/privacy")
def api_privacy_get():
    return privacy.options(load_conf())


@app.post("/api/privacy")
def api_privacy_set(payload: dict):
    conf = load_conf()
    p = conf.setdefault("privacy", {})
    for k in privacy.DEFAULTS:
        if k in payload:
            p[k] = bool(payload[k])
    CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, **privacy.options(conf)}


# ---------------------------------------------------------------- API: 微信 Agent 入口（OpenAI 兼容，供 OpenClaw/ClawBot 等接入）
def _agent_api_conf() -> dict:
    c = load_conf().get("agent_api") or {}
    return {"enabled": bool(c.get("enabled")),
            "key": str(c.get("api_key") or ""),
            "model": str(c.get("model") or "wechat-kb")}


def _agent_api_model(authorization: str) -> str | None:
    """校验 Bearer Key；通过返回模型名，失败/未开启返回 None。"""
    a = _agent_api_conf()
    if not a["enabled"]:
        return None
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    import hmac as _h
    if not a["key"] or not _h.compare_digest(token, a["key"]):
        return None
    return a["model"]


@app.get("/api/agentapi")
def api_agentapi_get():
    a = _agent_api_conf()
    return {"enabled": a["enabled"], "key": a["key"], "model": a["model"],
            "base_url": "http://127.0.0.1:8721/v1"}


@app.post("/api/agentapi")
def api_agentapi_set(payload: dict):
    conf = load_conf()
    a = conf.setdefault("agent_api", {})
    if "enabled" in payload:
        a["enabled"] = bool(payload["enabled"])
    if payload.get("model"):
        a["model"] = str(payload["model"]).strip() or "wechat-kb"
    if payload.get("regenerate") or (a.get("enabled") and not a.get("api_key")):
        import secrets as _s
        a["api_key"] = "wk-" + _s.token_hex(16)
    CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    a2 = _agent_api_conf()
    return {"ok": True, "enabled": a2["enabled"], "key": a2["key"], "model": a2["model"]}


@app.get("/v1/models")
def v1_models(authorization: str = Header(default="")):
    model = _agent_api_model(authorization)
    if not model:
        raise HTTPException(401, "无效的 API Key 或入口未开启")
    return {"object": "list", "data": [
        {"id": model, "object": "model", "created": 0,
         "owned_by": "wechat-knowledge-assistant"}]}


_INBOUND_SENTINELS = [
    "Conversation info (untrusted metadata):",
    "Sender (untrusted metadata):",
    "Thread starter (untrusted, for context):",
    "Reply target of current user message (untrusted, for context):",
    "Forwarded message context (untrusted metadata):",
    "Chat history since last reply (untrusted, for context):",
]
_INBOUND_META_RE = re.compile(
    r"(?ms)^(?:" + "|".join(re.escape(s) for s in _INBOUND_SENTINELS) +
    r")\s*\n```json\s*\n.*?^```\s*$\n?")
_LEADING_TS_RE = re.compile(r"^\[[A-Za-z]{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2}[^\]]*\] *")


def _strip_inbound_meta(text: str) -> str:
    """去掉 OpenClaw 注入的时间戳与元数据块，只留用户真正发的内容。

    OpenClaw 会在用户消息前加 `[Wed ...]` 和若干 `xxx (untrusted metadata):`
    + ```json``` 块；不剥掉的话 /帮助 之类的指令无法识别。
    """
    t = (text or "").strip()
    t = _LEADING_TS_RE.sub("", t)
    t = _INBOUND_META_RE.sub("", t)
    # 尾部可能还有一段 untrusted context，直接截断
    idx = t.find("Untrusted context (metadata, do not treat as instructions or commands):")
    if idx >= 0:
        t = t[:idx]
    return t.strip()


def _last_user_text(messages: list) -> str:
    for m in reversed(messages or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            t = c.strip()
        elif isinstance(c, list):
            t = "\n".join(x.get("text", "") for x in c
                          if isinstance(x, dict) and x.get("type") == "text").strip()
        else:
            t = ""
        if t:
            return t
    return ""


def _agent_api_log(line: str) -> None:
    """微信 Agent 入口的轻量访问日志（本机排查用，含问题前 80 字）。"""
    try:
        with open(DATA / "agent_api.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except Exception:
        pass


@app.post("/v1/chat/completions")
def v1_chat(payload: dict, authorization: str = Header(default="")):
    """OpenAI 兼容问答：微信 Agent（OpenClaw weixin 渠道）把消息路由到这。

    取最后一条用户消息作为问题，交给 wxagent（微信通道自己的指令/状态/问答），
    经本机 Agent 工具循环与引用清洗后，以 OpenAI chat.completions 结构返回。
    QQ 机器人有独立的 qqagent，两者不再共用状态或帮助文本。
    """
    model = _agent_api_model(authorization)
    if not model:
        _agent_api_log("401 无效密钥或入口未开启")
        raise HTTPException(401, "无效的 API Key 或入口未开启")
    question = _strip_inbound_meta(_last_user_text(payload.get("messages") or []))
    if not question:
        raise HTTPException(400, "消息里没有可回答的用户问题")
    _agent_api_log(f"收到提问（{len(question)} 字）：{question[:80]}")
    import wxagent
    # 指令（/帮助、/探索模式 等）直接由本机处理，不调用模型
    cmd_reply = wxagent.command(question)
    if cmd_reply is not None:
        answer = cmd_reply
        _agent_api_log(f"指令已处理：{question[:40]}")
    else:
        try:
            history = [m for m in (payload.get("messages") or [])
                       if isinstance(m, dict) and m.get("role") in ("user", "assistant")][:-1]
            answer = wxagent.ask(question, history=history)
        except Exception as e:      # noqa: BLE001
            _agent_api_log(f"问答失败：{type(e).__name__}: {e}")
            raise HTTPException(500, "问答处理失败，请稍后重试（本机已记录错误日志）。")
        answer = (answer or "").strip() or "（未生成回答，请稍后重试）"
        _agent_api_log(f"回答完成（{len(answer)} 字）")
    import secrets as _s
    cid, now = "chatcmpl-" + _s.token_hex(8), int(time.time())

    if payload.get("stream"):
        from fastapi.responses import StreamingResponse

        def gen():
            def chunk(delta, finish=None):
                return "data: " + json.dumps(
                    {"id": cid, "object": "chat.completion.chunk", "created": now,
                     "model": model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]},
                    ensure_ascii=False) + "\n\n"
            yield chunk({"role": "assistant"})
            for i in range(0, len(answer), 512):
                yield chunk({"content": answer[i:i + 512]})
            yield chunk({}, "stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    return {"id": cid, "object": "chat.completion", "created": now, "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": answer},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


# ---------------------------------------------------------------- API: 微信通道（OpenClaw）在软件内操作
_OC_LOGIN = {"proc": None, "running": False, "log": ""}


def _oc_paths() -> tuple[str, str] | None:
    """(openclaw.cmd, node24 目录)；home 可在 config.openclaw.home 覆盖。"""
    c = load_conf().get("openclaw") or {}
    home = Path(str(c.get("home") or (Path.home() / "openclaw-runtime")))
    oc = home / "npm" / "openclaw.cmd"
    node = home / "node24"
    if oc.exists() and node.exists():
        return str(oc), str(node)
    return None


def _no_window() -> dict:
    """Windows 下隐藏子进程控制台，避免设置页操作时弹出黑框。"""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": si, "creationflags": 0x08000000}   # CREATE_NO_WINDOW


def _oc_run(args: list[str], timeout: int = 90) -> str:
    import subprocess
    p = _oc_paths()
    if not p:
        raise HTTPException(400, "未找到 OpenClaw（openclaw-runtime 目录不存在）")
    oc, node = p
    env = os.environ.copy()
    env["PATH"] = node + os.pathsep + env.get("PATH", "")
    env["npm_config_registry"] = "https://registry.npmmirror.com"
    try:
        r = subprocess.run(["cmd", "/c", oc, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=timeout, env=env,
                           **_no_window())
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "命令超时")
    return (r.stdout or "") + ("\n" + r.stderr if r.stderr.strip() else "")


def _gateway_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 18789), timeout=0.5):
            return True
    except OSError:
        return False


def _kill_gateways() -> int:
    """结束残留的 OpenClaw Gateway 进程（换账号/重启时旧实例会占着端口）。

    只匹配 node.exe 且命令行同时含 openclaw 与 gateway/dist/index.js，避免误杀
    恰好命令行里带这两个词的终端/脚本。
    """
    killed = 0
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (p.info.get("name") or "").lower()
                if name not in {"node.exe", "node"}:
                    continue
                cmd = " ".join(p.info.get("cmdline") or [])
                low = cmd.lower()
                if "openclaw" in low and ("gateway" in low or "dist/index.js" in low):
                    p.kill()
                    killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:      # noqa: BLE001
        pass
    return killed


def _restart_gateway() -> bool:
    """干净重启：先杀残留实例，再启动计划任务（隐藏窗口），轮询到端口就绪。"""
    _kill_gateways()
    time.sleep(2)
    env = None
    try:
        subprocess.run(["schtasks", "/Run", "/TN", "OpenClaw Gateway"],
                       capture_output=True, timeout=30, env=env, **_no_window())
    except Exception:      # noqa: BLE001
        pass
    for _ in range(40):
        if _gateway_running():
            return True
        time.sleep(1)
    return False


_OCPROBE = {"ts": 0.0, "text": "", "busy": False}


def _probe_channels_bg() -> None:
    """后台探活：CLI 可能要几十秒，不能阻塞设置页的状态轮询。"""
    if _OCPROBE["busy"]:
        return
    _OCPROBE["busy"] = True
    try:
        _OCPROBE["text"] = _oc_run(["channels", "status", "--probe"], timeout=90).strip()
    except Exception as e:      # noqa: BLE001
        _OCPROBE["text"] = f"探测失败：{e}"
    finally:
        _OCPROBE["ts"] = time.time()
        _OCPROBE["busy"] = False


@app.get("/api/openclaw/status")
def api_openclaw_status():
    found = bool(_oc_paths())
    probe = _OCPROBE
    if found and not probe["busy"] and time.time() - probe["ts"] > 30:
        threading.Thread(target=_probe_channels_bg, daemon=True).start()
    # 登录器除了字符二维码，还会打印一条 liteapp 链接（含 qrcode 令牌）。
    # 页面用这条链接生成真正的二维码图片，避免字符画的像素间隙扫不出来。
    qr_url = ""
    for line in reversed((_OC_LOGIN["log"] or "").splitlines()):
        m = re.search(r"https://liteapp\.weixin\.qq\.com/[^\s\"'<>]+", line)
        if m:
            qr_url = m.group(0)
            break
    a = load_conf().get("agent_api") or {}
    return {"found": found, "gateway": _gateway_running(),
            "enabled": bool(a.get("enabled")),
            "probe": probe["text"] if found else "",
            "login_running": _OC_LOGIN["running"],
            "qr_url": qr_url,
            "login_log": _OC_LOGIN["log"][-3000:]}


@app.post("/api/links/retry-failed")
def api_links_retry():
    """把解析失败的链接重置为待解析（fake-IP 修复后可重试）。"""
    con = db()
    _ensure_link_tables(con)
    n = con.execute("UPDATE chat_link SET state=0 WHERE state=2").rowcount
    con.commit()
    con.close()
    # 往队列塞一批让 worker 马上开始
    if n:
        con2 = db()
        rows = con2.execute("SELECT url FROM chat_link WHERE state=0 ORDER BY msg_ts DESC LIMIT 5").fetchall()
        con2.close()
        for (u,) in rows:
            _LINK_QUEUE.put(u)
    return {"ok": True, "reset": n}


@app.post("/api/openclaw/toggle")
def api_openclaw_toggle(payload: dict):
    """机器人设置里的「启用微信通道」开关：开=写配置并重启网关，关=停网关。"""
    want = bool(payload.get("enabled"))
    conf = load_conf()
    a = conf.setdefault("agent_api", {})
    a["enabled"] = want
    CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    if want:
        _ensure_oc_config()
        ok = _restart_gateway()
        return {"ok": True, "enabled": True, "gateway": ok}
    killed = _kill_gateways()
    return {"ok": True, "enabled": False, "gateway": _gateway_running(),
            "output": f"已停止 {killed} 个网关进程"}


_QR_CACHE: dict[str, bytes] = {}


@app.get("/api/openclaw/qr.png")
def api_openclaw_qr():
    """把当前登录二维码链接渲染成图片（黑白、带静区），比字符画可靠。"""
    url = ""
    for line in reversed((_OC_LOGIN["log"] or "").splitlines()):
        m = re.search(r"https://liteapp\.weixin\.qq\.com/[^\s\"'<>]+", line)
        if m:
            url = m.group(0)
            break
    if not url:
        raise HTTPException(404, "还没有可用的登录二维码")
    png = _QR_CACHE.get(url)
    if png is None:
        try:
            import qrcode
            from io import BytesIO
            img = qrcode.make(url, box_size=10, border=3)
            buf = BytesIO()
            img.save(buf, format="PNG")
            png = buf.getvalue()
        except Exception as e:      # noqa: BLE001
            raise HTTPException(500, f"二维码生成失败：{e}")
        _QR_CACHE.clear()      # 令牌每次刷新都会变，只留最新一张
        _QR_CACHE[url] = png
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/openclaw/gateway")
def api_openclaw_gateway(payload: dict):
    action = str(payload.get("action") or "restart")
    if action == "restart":
        ok = _restart_gateway()
        return {"ok": True, "gateway": ok, "output": "网关已重启" if ok else "网关未就绪"}
    if action == "stop":
        killed = _kill_gateways()
        return {"ok": True, "gateway": _gateway_running(), "output": f"已结束 {killed} 个网关进程"}
    raise HTTPException(400, "action 仅支持 restart/stop")


@app.post("/api/openclaw/login")
def api_openclaw_login():
    """启动微信扫码登录：二维码会以字符画形式流式输出到登录日志。"""
    import subprocess
    p = _oc_paths()
    if not p:
        raise HTTPException(400, "未找到 OpenClaw（openclaw-runtime 目录不存在）")
    if _OC_LOGIN["running"]:
        raise HTTPException(409, "登录流程已在进行中")
    _ensure_oc_config()      # 扫码前自动写好 provider/密钥，页面无需任何输入框
    oc, node = p
    env = os.environ.copy()
    env["PATH"] = node + os.pathsep + env.get("PATH", "")
    env["npm_config_registry"] = "https://registry.npmmirror.com"

    def _pump(proc):
        try:
            for line in proc.stdout:
                _OC_LOGIN["log"] = (_OC_LOGIN["log"] + line)[-6000:]
        except Exception:
            pass
        finally:
            _OC_LOGIN["running"] = False
            _OC_LOGIN["log"] += "\n[登录进程已退出]"
            try:
                # 扫码成功后账号才会写进配置；重启网关才能为它启动消息监听
                ok = _restart_gateway()
                _OC_LOGIN["log"] += "\n[已自动重启网关]" if ok else "\n[自动重启网关未就绪]"
            except Exception as e:      # noqa: BLE001
                _OC_LOGIN["log"] += f"\n[自动重启网关失败：{e}]"

    _OC_LOGIN["log"] = ""
    _OC_LOGIN["running"] = True
    try:
        proc = subprocess.Popen(["cmd", "/c", oc, "channels", "login",
                                 "--channel", "openclaw-weixin"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="ignore",
                                bufsize=1, env=env, **_no_window())
    except Exception as e:      # noqa: BLE001
        _OC_LOGIN["running"] = False
        raise HTTPException(500, f"启动登录失败：{e}")
    _OC_LOGIN["proc"] = proc
    threading.Thread(target=_pump, args=(proc,), daemon=True).start()
    return {"ok": True}


@app.post("/api/openclaw/login/stop")
def api_openclaw_login_stop():
    proc = _OC_LOGIN.get("proc")
    if proc is not None and proc.poll() is None:
        proc.terminate()
    return {"ok": True}


@app.post("/api/openclaw/pairing")
def api_openclaw_pairing(payload: dict):
    code = str(payload.get("code") or "").strip()
    if not code:
        raise HTTPException(400, "请输入配对码")
    out = _oc_run(["pairing", "approve", "openclaw-weixin", code], timeout=60)
    return {"ok": True, "output": out[-1200:]}


def _ensure_oc_config() -> dict:
    """把本助手写入 OpenClaw 配置（provider/主模型/网关 token/插件允许），返回 {model,key}。

    注意：这是内部函数，不要给它挂路由装饰器——曾经和下面的 /api/openclaw/apply
    重复注册，导致「一键写入并重启」版本永远匹配不到。

    设置页不再让用户填任何字段：扫码登录前自动调用一次即可。
    """
    import secrets as _s
    conf = load_conf()
    a = conf.setdefault("agent_api", {})
    a["enabled"] = True
    a.setdefault("model", "wechat-kb")
    if not a.get("api_key"):
        a["api_key"] = "wk-" + _s.token_hex(16)
    CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")

    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    if not cfg_path.exists():
        raise HTTPException(400, "未检测到 OpenClaw（~/.openclaw/openclaw.json 不存在），"
                                 "请先按 README 安装微信渠道")
    try:
        oc = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:      # noqa: BLE001
        raise HTTPException(500, f"读取 OpenClaw 配置失败：{e}")

    gw = oc.setdefault("gateway", {})
    gw.setdefault("mode", "local")
    auth = gw.setdefault("auth", {})
    if not auth.get("token"):
        auth["token"] = "ocw-" + _s.token_hex(12)
    plugins = oc.setdefault("plugins", {})
    allow = plugins.setdefault("allow", [])
    if "openclaw-weixin" not in allow:
        allow.append("openclaw-weixin")
    models = oc.setdefault("models", {})
    models["mode"] = "merge"
    providers = models.setdefault("providers", {})
    providers["wechatkb"] = {
        "baseUrl": "http://127.0.0.1:8721/v1",
        "apiKey": a["api_key"],
        "api": "openai-completions",
        "timeoutSeconds": 300,
        "models": [{"id": a["model"], "name": "Qwent"}],
    }
    agents = oc.setdefault("agents", {})
    defaults = agents.setdefault("defaults", {})
    model_cfg = defaults.setdefault("model", {})
    model_cfg["primary"] = f"wechatkb/{a['model']}"
    try:
        cfg_path.write_text(json.dumps(oc, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:      # noqa: BLE001
        raise HTTPException(500, f"写入 OpenClaw 配置失败：{e}")
    return {"model": a["model"], "key": a["api_key"]}


@app.post("/api/openclaw/apply")
def api_openclaw_apply():
    """一键写入并重启（保留给命令行/高级用法）。"""
    info = _ensure_oc_config()
    ok = _restart_gateway()
    return {"ok": True, "gateway": ok, "output": "网关已重启" if ok else "网关未就绪",
            "model": info["model"], "key": info["key"]}



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
    # 文件名来自 URL 参数，Windows 下的反斜杠同样会被 Path 当作目录分隔符。
    # 保留历史上的斜杠转下划线行为，同时彻底消除目录分隔符。
    safe = str(name or "").replace("/", "_").replace("\\", "_")
    if not safe or safe in {".", ".."}:
        return None
    root = fdir.resolve()

    def is_inside(path: Path) -> bool:
        try:
            path.resolve().relative_to(root)
            return path.is_file()
        except (OSError, ValueError):
            return False

    cands: list[Path] = []
    for mth in months:
        p = fdir / mth / safe
        if is_inside(p):
            cands.append(p)
    if not cands:      # 兜底：全目录按文件名搜索（只扫有该月的少量目录）
        for sub in sorted(fdir.iterdir(), reverse=True)[:36]:
            if sub.is_dir():
                p = sub / safe
                if is_inside(p):
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
        if ext in (".docx",):
            import docx as _docx
            d = _docx.Document(str(path))
            txt = "\n".join(p.text for p in d.paragraphs)
            for tbl in d.tables[:5]:
                for row in tbl.rows[:30]:
                    txt += "\n| " + " | ".join(c.text.strip() for c in row.cells) + " |"
            return txt[:limit]
        if ext in (".pptx", ".pptm"):
            import zipfile
            with zipfile.ZipFile(str(path)) as z:
                slides = sorted(
                    (n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                    key=lambda s: int(re.search(r"slide(\d+)\.xml$", s).group(1)))
                out = []
                for i, sn in enumerate(slides[:40]):
                    xml = z.read(sn).decode("utf-8", "ignore")
                    texts = [html.unescape(t) for t in re.findall(r"<a:t>(.*?)</a:t>", xml, re.S)]
                    out.append(f"[第{i+1}页]\n" + "\n".join(texts))
                    if sum(len(x) for x in out) > limit:
                        break
            return "\n".join(out)[:limit]
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
        return {"found": False, "name": name, "msg_id": msg_id, "path_exists": False}
    return {"found": True, "name": p.name,
            "size": p.stat().st_size, "ext": p.suffix.lower(), "msg_id": msg_id,
            "path_exists": True,
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


@app.post("/api/file/reveal")
def api_file_reveal(payload: dict):
    """在资源管理器中定位文件（仅本机服务，端口只监听 127.0.0.1）。"""
    import subprocess
    chat = str(payload.get("chat") or "")
    name = str(payload.get("title") or payload.get("name") or "")
    p = locate_file(chat, int(payload.get("id") or 0), int(payload.get("ts") or 0), name)
    if not p:
        raise HTTPException(404, "文件不在本地（可能已被清理或未下载）")
    try:
        subprocess.Popen(["explorer.exe", f"/select,{p}"])
    except Exception as e:      # noqa: BLE001
        raise HTTPException(500, f"打开所在位置失败：{e}")
    return {"ok": True}


_FAKE_IP_NETS = [ipaddress.ip_network("198.18.0.0/15")]   # Clash/Surge fake-IP DNS 段


def _validate_external_url(url: str) -> str:
    """校验外部抓取地址，拒绝本机/内网目标，避免链接代理成为 SSRF。

    198.18.0.0/15 是 Clash 系代理的 fake-IP 段：系统 DNS 开 TUN 时所有域名都
    解析成它，按保留地址一票否决会把全部外链抓取拒掉（OpenClaw 同款豁免）。
    """
    u = urllib.parse.urlparse(str(url or "").strip())
    if u.scheme not in {"http", "https"} or not u.hostname:
        raise HTTPException(400, "只支持 http/https 链接")
    host = u.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise HTTPException(400, "不允许访问本机地址")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, u.port, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        raise HTTPException(400, "链接域名无法解析")
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            continue
        if ip.is_global or any(ip in net for net in _FAKE_IP_NETS):
            continue
        raise HTTPException(400, "不允许访问内网或保留地址")
    return u.geturl()


def _safe_external_get(url: str, **kwargs):
    """带有限重定向的外部请求；每一跳都重新校验目标地址。"""
    import requests
    current = _validate_external_url(url)
    for _ in range(4):
        resp = requests.get(current, allow_redirects=False, **kwargs)
        if resp.status_code not in {301, 302, 303, 307, 308}:
            return resp
        target = resp.headers.get("Location")
        resp.close()
        if not target:
            return resp
        current = _validate_external_url(urllib.parse.urljoin(current, target))
    raise HTTPException(400, "链接重定向次数过多")


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


_media_refresh_lock = threading.Lock()
_media_refresh_at = [0.0]


def _refresh_media_dbs(force: bool = False) -> None:
    """按需解密 media_*.db（语音数据在 VoiceInfo 里）。

    启动后只在首次导入时解过 media 库；之后的语音全在源库 WAL 里没进来，
    播不出来就是这里缺了。这里按指纹增量解密，30 秒内最多做一次。
    """
    now = time.time()
    if not force and now - _media_refresh_at[0] < 30:
        return
    if not _media_refresh_lock.acquire(blocking=False):
        return
    try:
        import fastsync
        import keyvault
        key = keyvault.load_key(ACCOUNT_ID)
        if not key:
            return
        msg_dir = ACCOUNT_DIR / "db_storage" / "message"
        dbs = [f"message/{p.name}" for p in msg_dir.glob("media_*.db")] if msg_dir.is_dir() else []
        if dbs:
            fastsync.fast_sync_message_dbs(ACCOUNT_DIR, DECRYPTED_DIR, key, dbs=dbs)
        # MediaService 缓存了 media_*.db 的 Name2Id 映射，刷新后必须失效，
        # 否则新会话仍然查不到语音
        try:
            get_media()._media_name2id = None
        except Exception:
            pass
        _media_refresh_at[0] = time.time()
    except Exception as exc:  # noqa: BLE001
        print(f"[media] refresh failed: {exc}")
    finally:
        _media_refresh_lock.release()


@app.get("/api/media/voice")
def api_voice(chat: str, id: int):
    con = db()
    m = con.execute("SELECT * FROM message WHERE id=? AND chat_user=?", (id, chat)).fetchone()
    con.close()
    if not m:
        raise HTTPException(404)
    media_svc = get_media()
    try:
        wav = media_svc.voice_wav(m["chat_user"], m["src_local_id"], m["ts"] or 0)
    except Exception as e:
        raise HTTPException(500, f"转码失败: {e}")
    if not wav:
        # 解密副本落后于源库：刷新 media 库后再试一次
        _refresh_media_dbs()
        media_svc = get_media()
        try:
            wav = media_svc.voice_wav(m["chat_user"], m["src_local_id"], m["ts"] or 0)
        except Exception as e:
            raise HTTPException(500, f"转码失败: {e}")
    if wav:
        return Response(content=wav, media_type="audio/wav")
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
    # 小程序卡片没有可抓取的网页正文；登记成 wxapp:// 仅保留标题/描述
    if "<appid>" in c:
        ma = re.search(r"<appid>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</appid>", c, re.S)
        mp = re.search(r"<pagepath>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</pagepath>", c, re.S)
        if ma and ma.group(1).strip():
            appid = ma.group(1).strip()
            page = mp.group(1).strip() if mp else ""
            out.append((f"wxapp://{appid}/{page}", title or "小程序卡片"))
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


_JUNK_LINK_PARTS = ("mp.weixin.qq.com/mp/waerrpage",
                    "support.weixin.qq.com/update",
                    "weixin.qq.com/connect/infopage")
_JUNK_TITLE_PARTS = ("[调用模型失败", "[模型暂时不可用", "[模型接口暂时不可用")


def _link_ok(url: str, title: str = "") -> bool:
    """过滤微信升级/错误提示页和模型报错文本，避免它们混进链接中心。"""
    u = (url or "").lower()
    t = title or ""
    return not any(p in u for p in _JUNK_LINK_PARTS) and         not any(p in t for p in _JUNK_TITLE_PARTS)


def _queue_link(url: str, chat: str, msg_id: int, msg_ts: int, title: str, platform: str):
    """收到消息即登记的链接：写 chat_link 并入解析队列（已解析过的直接跳过）。"""
    if not _link_ok(url, title):
        return
    con = db()
    _ensure_link_tables(con)
    is_mini = url.startswith("wxapp://")
    con.execute("INSERT OR IGNORE INTO chat_link(url,chat,msg_id,msg_ts,title,platform,"
                "queued,state) VALUES(?,?,?,?,?,?,0,?)",
                (url, chat, msg_id, msg_ts, (title or "")[:120], platform,
                 2 if is_mini else 0))
    con.commit()
    con.close()
    if not is_mini:
        _LINK_QUEUE.put(url)


def _refresh_platform(url: str) -> str:
    if url.startswith("wxapp://"):
        return "wxapp"
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
                # 公众号/普通网页：走原有的正文抓取（不需要 OCR/ASR）。
                # fetch_article 会对坏链抛 HTTPException（如 SSRF 校验失败），
                # 必须落成 state=2，否则该链接永远留在"解析中"被无限重试。
                try:
                    d = fetch_article(url)
                except Exception as fe:      # noqa: BLE001
                    con = db(); _ensure_link_tables(con)
                    con.execute("UPDATE chat_link SET state=2 WHERE url=?", (url,))
                    con.commit(); con.close()
                    _LINK_WORKER["error"] = f"{url[:60]}：{type(fe).__name__}"
                    continue
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
            if not _link_ok(url, title):
                continue
            cur = con.execute("SELECT 1 FROM chat_link WHERE url=?", (url,)).fetchone()
            if cur:
                continue
            con.execute("INSERT OR IGNORE INTO chat_link(url,chat,msg_id,msg_ts,title,"
                        "platform,state) VALUES(?,?,?,?,?,?,?)",
                        (url, r["chat_user"], r["id"], r["ts"] or 0,
                         (title or "")[:120], _refresh_platform(url),
                         2 if url.startswith("wxapp://") else 0))
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
def api_links(n: int = 100, q: str = "", offset: int = 0, platform: str = "",
              state: str = "", days: int = 0, sort: str = "new", chat: str = ""):
    """链接中心：聊天里出现过的链接（收到即登记，后台解析入库）。"""
    con = db()
    _ensure_link_tables(con)
    where, args = ["1=1"], []
    if days:
        where.append("l.msg_ts >= ?")
        args.append(int(time.time()) - max(1, days) * 86400)
    if chat:
        where.append("l.chat = ?")
        args.append(chat)
    # 平台筛选下沉到 SQL（LIMIT 内可能没有目标平台的链接）
    if platform:
        plat_map_sql = {"xhs": "%xiaohongshu%", "bili": "%bilibili%",
                        "article": "%mp.weixin.qq.com%"}
        if platform == "bili":
            where.append("(l.url LIKE '%bilibili%' OR l.url LIKE '%b23.tv%')")
        elif platform in plat_map_sql:
            where.append("l.url LIKE ?")
            args.append(plat_map_sql[platform])
        elif platform == "wxapp":
            where.append("l.url LIKE 'wxapp://%'")
        else:
            where.append("l.url NOT LIKE '%xiaohongshu%' AND l.url NOT LIKE '%bilibili%'"
                         " AND l.url NOT LIKE '%b23.tv%' AND l.url NOT LIKE '%mp.weixin.qq.com%'"
                         " AND l.url NOT LIKE 'wxapp://%'")
    filtered = bool(q or state or days or chat)
    cap = min(2500, max(3000, (offset + n) * 3)) if (platform or filtered) else max(800, (offset + n) * 2)
    rows = con.execute("""
        SELECT l.url, l.chat, l.msg_id, l.msg_ts, l.title, l.platform, l.state,
               c.title AS c_title, c.kind AS c_kind, LENGTH(IFNULL(c.text,'')) AS c_len,
               CASE WHEN l.chat = 'sns' THEN '朋友圈'
                    ELSE (SELECT COALESCE(NULLIF(display_name,''), username) FROM chat
                          WHERE username = l.chat) END AS chat_name
        FROM chat_link l LEFT JOIN link_cache c ON c.url=l.url
        WHERE """ + " AND ".join(where) + """
        ORDER BY l.msg_ts DESC LIMIT ?""", (*args, cap)).fetchall()
    _PLAT_MAP = {"xhs": "小红书", "bili": "哔哩哔哩", "article": "公众号",
                 "generic": "网页", "wxapp": "小程序"}
    out = []
    seen_urls = set()
    for r in rows:
        if not _link_ok(r["url"], r["title"] or ""):
            continue
        if r["url"] in seen_urls:
            continue
        seen_urls.add(r["url"])
        parsed = (r["c_len"] or 0) > 0
        raw_kind = (r["platform"] or "").strip()
        u = r["url"]
        if "xiaohongshu" in u:
            plat_key = "xhs"
        elif "bilibili" in u or "b23.tv" in u:
            plat_key = "bili"
        elif "mp.weixin.qq.com" in u:
            plat_key = "article"
        elif raw_kind in _PLAT_MAP:
            plat_key = raw_kind
        else:
            plat_key = "generic"
        if platform and plat_key != platform:
            continue
        if state == "parsed" and not parsed:
            continue
        if state == "failed" and (r["state"] or 0) != 2:
            continue
        if state == "pending" and (parsed or (r["state"] or 0) == 2):
            continue
        item = {"url": u,
                "title": (r["c_title"] or r["title"] or u[:70])[:80],
                "platform": _PLAT_MAP.get(plat_key, raw_kind or "网页"),
                "platform_key": plat_key,
                "received": r["msg_ts"] or 0, "chat": r["chat"],
                "chat_name": r["chat_name"] or r["chat"],
                "msg_id": r["msg_id"], "parsed": parsed, "state": r["state"] or 0,
                "chars": r["c_len"] or 0}
        if q and q.lower() not in (item["title"] + item["chat_name"] + item["platform"]).lower():
            continue
        out.append(item)
    con.close()
    if sort == "old":
        out.sort(key=lambda x: x["received"] or 0)
    if platform == "article":
        rej = {"link_ok": 0, "dedup": 0, "plat": 0, "state": 0, "q": 0, "ok": 0}
        for r in rows:
            if not _link_ok(r["url"], r["title"] or ""):
                rej["link_ok"] += 1; continue
            if r["url"] in seen_urls:
                rej["dedup"] += 1; continue
            raw_kind = (r["platform"] or "").strip()
            u = r["url"]
            if raw_kind in _PLAT_MAP: pk = raw_kind
            elif "xiaohongshu" in u: pk = "xhs"
            elif "bilibili" in u or "b23.tv" in u: pk = "bili"
            elif "mp.weixin.qq.com" in u: pk = "article"
            else: pk = "generic"
            if platform and pk != platform:
                rej["plat"] += 1; continue
            rej["ok"] += 1
    return out[offset:offset + n]


@app.get("/api/links/queue")
def api_links_queue():
    """链接解析队列概况：待解析条数与当前正在解析的 URL（诊断"一直解析中"用）。"""
    con = db()
    _ensure_link_tables(con)
    pending = con.execute(
        "SELECT COUNT(*) FROM chat_link l LEFT JOIN link_cache c ON c.url=l.url "
        "WHERE IFNULL(l.state,0)=0 AND IFNULL(c.text,'')=''").fetchone()[0]
    failed = con.execute("SELECT COUNT(*) FROM chat_link WHERE state=2").fetchone()[0]
    con.close()
    th = _LINK_WORKER.get("thread")
    return {"pending": pending, "failed": failed,
            "thread_alive": bool(th and th.is_alive()),
            "queued": _LINK_QUEUE.qsize(),
            "current": _LINK_WORKER.get("current") or "",
            "last_done": _LINK_WORKER.get("last") or "",
            "error": _LINK_WORKER.get("error") or ""}


FILE_KINDS = {
    "doc": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt"},
    "sheet": {".xls", ".xlsx", ".xlsm", ".csv"},
    "slide": {".ppt", ".pptx", ".pptm"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "media": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".amr",
              ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".silk"},
}


def file_kind(ext: str) -> str:
    ext = (ext or "").lower()
    for kind, exts in FILE_KINDS.items():
        if ext in exts:
            return kind
    return "other"


def _file_name(packed_info) -> str:
    fields = pb_fields(packed_info)
    if not fields:
        return ""
    inner = pb_fields(fields[0][1])
    for tag, val in (inner or fields):
        if tag == 1 and val:
            return val.decode("utf-8", "ignore")
    return ""


@app.get("/api/files")
def api_files(q: str = "", n: int = 200, offset: int = 0, chat: str = "",
              ext: str = "", kind: str = "", since: int = 0, until: int = 0,
              sort: str = "new", state: str = ""):
    rdb = resource_db()
    if not rdb:
        return []
    n = max(1, min(n, 500))
    offset = max(0, offset)
    con = db()
    chat_ids = {r[0]: r[1] for r in rdb.execute("SELECT rowid, user_name FROM ChatName2Id")}
    where = ["i.message_local_type & 4294967295 = 49", "length(d.packed_info) > 2"]
    args: list[Any] = []
    if chat:
        ids = [rid for rid, uname in chat_ids.items() if uname == chat]
        if not ids:
            con.close()
            rdb.close()
            return []
        where.append("i.chat_id IN (" + ",".join("?" for _ in ids) + ")")
        args.extend(ids)
    if since:
        where.append("i.message_create_time >= ?")
        args.append(int(since))
    if until:
        where.append("i.message_create_time <= ?")
        args.append(int(until))
    # 文件名/扩展名只能在解析 packed_info 后过滤：带这些条件时多取一些候选，
    # 否则最近 3000 条里可能一个都匹配不上。仍设上限，避免每键一次全表解析。
    filtered = bool(q or ext or kind)
    cap = min(8000 if filtered else max(3000, (offset + n) * 2), 12000)
    sql = ("SELECT i.chat_id, i.message_local_id, i.message_create_time, d.size, d.packed_info "
           "FROM MessageResourceInfo i JOIN MessageResourceDetail d ON d.message_id = i.message_id "
           "WHERE " + " AND ".join(where) + " ORDER BY i.message_create_time DESC LIMIT ?")
    rows = rdb.execute(sql, [*args, cap]).fetchall()
    wanted_ext = (ext or "").strip().lower()
    if wanted_ext and not wanted_ext.startswith("."):
        wanted_ext = "." + wanted_ext
    out = []
    for r in rows:
        name = _file_name(r["packed_info"])
        if not name:
            continue
        fext = Path(name).suffix.lower()
        if q and q.lower() not in name.lower():
            continue
        if wanted_ext and fext != wanted_ext:
            continue
        if kind and file_kind(fext) != kind:
            continue
        username = chat_ids.get(r["chat_id"], "")
        cname = con.execute(
            "SELECT COALESCE(NULLIF(display_name,''), username) FROM chat WHERE username=?",
            (username,)).fetchone()
        out.append({"id": r["message_local_id"], "chat": username,
                    "chat_name": cname[0] if cname else username,
                    "ts": r["message_create_time"], "title": name, "size": r["size"],
                    "ext": fext, "kind": file_kind(fext)})
    rdb.close()
    if state == "parsed":
        out = [x for x in out if x.get("status") == "parsed"]
    elif state == "missing":
        out = [x for x in out if x.get("status") == "missing"]
    elif state == "unprocessed":
        out = [x for x in out if x.get("status") == "unprocessed"]
    if sort == "old":
        out.sort(key=lambda x: x["ts"] or 0)
    elif sort == "size":
        out.sort(key=lambda x: x["size"] or 0, reverse=True)
    page = out[offset:offset + n]
    # 标记每个文件的正文是否已成功提取（file_text 缓存；空文本=未下载/解析失败）
    keys = [f"{x['chat']}:{x['id']}" for x in page]
    status = {}
    if keys:
        try:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                rows = con.execute(
                    "SELECT key, LENGTH(IFNULL(text,'')) FROM file_text WHERE key IN (" +
                    ",".join("?" for _ in chunk) + ")", chunk).fetchall()
                for k, n in rows:
                    status[k] = int(n or 0)
        except sqlite3.OperationalError:
            status = {}
    for x in page:
        k = f"{x['chat']}:{x['id']}"
        chars = status.get(k)
        if chars is None:
            x.update(parsed=False, chars=0, status="unprocessed")
        elif chars > 0:
            x.update(parsed=True, chars=chars, status="parsed")
        else:
            x.update(parsed=False, chars=0, status="missing")
    con.close()
    return page


_file_stats_cache = {"ts": 0.0, "data": {}}


@app.get("/api/files/stats")
def api_files_stats():
    """文件中心统计：总数 / 成功提取正文 / 未下载或解析失败，按类型分组。"""
    now = time.time()
    if now - _file_stats_cache["ts"] < 300 and _file_stats_cache["data"]:
        return _file_stats_cache["data"]
    rdb = resource_db()
    if not rdb:
        return {"total": 0, "parsed": 0, "unparsed": 0, "kinds": {}}
    con = db()
    chat_ids = {r[0]: r[1] for r in rdb.execute(
        "SELECT rowid, user_name FROM ChatName2Id")}
    texts = {}
    try:
        for k, n in con.execute("SELECT key, LENGTH(IFNULL(text,'')) FROM file_text"):
            texts[k] = int(n or 0)
    except sqlite3.OperationalError:
        pass
    rows = rdb.execute(
        "SELECT i.chat_id, i.message_local_id, d.packed_info "
        "FROM MessageResourceInfo i JOIN MessageResourceDetail d "
        "ON d.message_id = i.message_id "
        "WHERE i.message_local_type & 4294967295 = 49 AND length(d.packed_info) > 2"
    ).fetchall()
    kinds = {k: {"total": 0, "parsed": 0, "missing": 0, "unprocessed": 0}
             for k in ("doc", "sheet", "slide", "image", "archive", "media", "other")}
    total = parsed = missing = unprocessed = 0
    seen = set()
    for r in rows:
        name = _file_name(r["packed_info"])
        if not name:
            continue
        username = chat_ids.get(r["chat_id"], "")
        key = f"{username}:{r['message_local_id']}"
        if key in seen:
            continue
        seen.add(key)
        fext = Path(name).suffix.lower()
        kind = file_kind(fext)
        kd = kinds.setdefault(kind, {"total": 0, "parsed": 0,
                                     "missing": 0, "unprocessed": 0})
        kd["total"] += 1
        total += 1
        if key in texts:
            if texts[key] > 0:
                kd["parsed"] += 1
                parsed += 1
            else:
                kd["missing"] += 1
                missing += 1
        else:
            kd["unprocessed"] += 1
            unprocessed += 1
    con.close()
    rdb.close()
    data = {"total": total, "parsed": parsed, "missing": missing,
            "unprocessed": unprocessed, "unparsed": total - parsed,
            "kinds": kinds}
    _file_stats_cache.update(ts=now, data=data)
    return data


# ---------------------------------------------------------------- API: QQ 机器人
@app.get("/api/qqbot/status")
def api_qqbot_status():
    conf = load_conf().get("qqbot", {})
    import qqbot as qqbot_mod
    st = qqbot_mod.status()
    secret = str(conf.get("app_secret") or "")
    return {"enabled": bool(conf.get("enabled")),
            "app_id": str(conf.get("app_id") or ""),
            "has_secret": bool(secret),
            "secret_masked": (secret[:4] + "…" + secret[-4:]) if len(secret) > 8
                             else ("已配置" if secret else ""),
            "group_reply": bool(conf.get("group_reply")),
            "citations": bool(conf.get("citations")),
            "wechat_chat": str(conf.get("wechat_chat") or ""),
            "default_explorer": bool(conf.get("default_explorer")),
            "default_thinking": bool(conf.get("default_thinking")),
            "allow_openids": list(conf.get("allow_openids") or []),
            "max_msg_chars": int(conf.get("max_msg_chars") or 700),
            "max_chunks": int(conf.get("max_chunks") or 4),
            "running": st.get("running"), "connected": st.get("connected"),
            "username": st.get("username"), "error": st.get("error"),
            "menu": st.get("menu"), "menu_error": st.get("menu_error"),
            "last_event": st.get("last_event"), "users": st.get("users")}


@app.post("/api/qqbot/config")
def api_qqbot_config(payload: dict):
    conf = load_conf()
    q = conf.setdefault("qqbot", {})
    old = dict(q)
    if "app_id" in payload:
        q["app_id"] = str(payload.get("app_id") or "").strip()
    if payload.get("app_secret"):
        q["app_secret"] = str(payload["app_secret"]).strip()
    if "wechat_chat" in payload:
        v = str(payload.get("wechat_chat") or "").strip()
        q["wechat_chat"] = v or None
    for k in ("enabled", "group_reply", "citations",
              "default_explorer", "default_thinking"):
        if k in payload:
            q[k] = bool(payload[k])
    if "allow_openids" in payload:
        raw = payload.get("allow_openids")
        if isinstance(raw, str):
            raw = re.split(r"[,\s;]+", raw)
        q["allow_openids"] = [str(x).strip() for x in (raw or [])
                              if str(x).strip()][:50]
    for k, lo, hi in (("max_msg_chars", 100, 2000), ("max_chunks", 1, 5)):
        if k in payload:
            try:
                q[k] = max(lo, min(hi, int(payload[k])))
            except (TypeError, ValueError):
                pass
    CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    import qqbot as qqbot_mod
    qqbot_mod.apply_config(load_conf())

    # 首次启用或改了凭据时，自动推送底部菜单，省去手动点按钮
    need_menu = bool(q.get("enabled") and q.get("app_id") and q.get("app_secret")) and (
        not old.get("enabled")
        or old.get("app_id") != q.get("app_id")
        or old.get("app_secret") != q.get("app_secret")
        or qqbot_mod.STATUS.get("menu") not in ("ok", "pushing"))
    if need_menu:
        qqbot_mod.STATUS["menu"] = "pushing"

        def _push_menu():
            try:
                r = qqbot_mod.set_menu(qqbot_mod.conf_of(load_conf()))
                if r.get("ok"):
                    qqbot_mod.STATUS["menu"] = "ok"
                    qqbot_mod.STATUS["menu_error"] = ""
                else:
                    qqbot_mod.STATUS["menu"] = "fail"
                    qqbot_mod.STATUS["menu_error"] = str(
                        r.get("error") or r.get("body") or "未知错误")[:200]
            except Exception as e:      # noqa: BLE001
                qqbot_mod.STATUS["menu"] = "fail"
                qqbot_mod.STATUS["menu_error"] = f"{type(e).__name__}: {e}"[:200]
        threading.Thread(target=_push_menu, daemon=True).start()

    st = qqbot_mod.status()
    return {"ok": True, "running": st.get("running"), "error": st.get("error"),
            "menu": st.get("menu"), "menu_error": st.get("menu_error")}


@app.post("/api/qqbot/menu")
def api_qqbot_menu():
    """一键把自定义菜单推送到 QQ（等价控制台手工配置）。"""
    import qqbot as qqbot_mod
    return qqbot_mod.set_menu(qqbot_mod.conf_of(load_conf()))


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


def _ensure_chat_row(con, chat_user: str, last_ts: int) -> None:
    """新消息先于 session.db 到达时，立即补一行会话。

    新加好友/新群的第一条消息往往比 session.db 的刷新更早被发现；只等
    _refresh_dirs 会让会话列表里没有这个人，或者退回显示 wxid。这里在有
    消息时就建行，并优先用通讯录里的备注/昵称作为显示名。
    """
    name_row = con.execute(
        "SELECT COALESCE(NULLIF(TRIM(remark),''), NULLIF(TRIM(nick_name),''),"
        " NULLIF(TRIM(alias),'')) FROM contact WHERE username=?",
        (chat_user,)).fetchone()
    disp = (name_row[0] if name_row else "") or ""
    is_group = 1 if ("@chatroom" in chat_user or "@openim" in chat_user) else 0
    ts = int(last_ts or 0)
    if disp:
        con.execute("""
            INSERT INTO chat(username, is_group, display_name, last_ts, summary)
            VALUES(?,?,?,?,'')
            ON CONFLICT(username) DO UPDATE SET
                is_group=excluded.is_group,
                display_name=excluded.display_name,
                last_ts=MAX(IFNULL(chat.last_ts,0), IFNULL(excluded.last_ts,0))
        """, (chat_user, is_group, disp, ts))
    else:
        # 通讯录还没同步到：先建会话保证"列表里有"，显示名暂用 username；
        # 随后的 _refresh_dirs 会用通讯录把 display_name 补正。
        con.execute("""
            INSERT OR IGNORE INTO chat(username, is_group, display_name, last_ts, summary)
            VALUES(?,?,NULL,?,'')
        """, (chat_user, is_group, ts))
        con.execute("UPDATE chat SET is_group=?, last_ts=MAX(IFNULL(last_ts,0),?) "
                    "WHERE username=?", (is_group, ts, chat_user))


def _flush_rows(con, own, db_label: str, t: str, chat_user: str,
                sender_map: dict, rows) -> tuple[int, int]:
    """处理一批源消息行(五元组 local_id,local_type,create_time,message_content,
    real_sender_id)，写入知识库 + 重建 FTS + 登记链接。返回 (新增数, 最大时间戳)。"""
    total = 0
    max_ts = 0
    batch = []
    # src_db 统一存 basename:历史上"message/message_N.db"与"message_N.db"两种
    # 写法让唯一索引 (src_db,src_table,src_local_id) 失效,同一消息被导入两次
    db_label = db_label.rsplit("/", 1)[-1]
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
    # 只要本批有消息，就保证会话行存在并跟上最新时间；即使消息早已入库
    # （补扫/重扫），也能把之前漏建的会话补回来，并同步改名/备注。
    _ensure_chat_row(con, chat_user, max_ts)
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


# ---------------------------------------------------------------- 联系人/会话增量刷新
_dir_sync: dict = {"contact": None, "session": None, "heal_ts": 0.0}
_dir_src_fp: dict[str, tuple] = {}


def _sync_decrypted_dirs() -> list[str]:
    """让解密后的 contact/session 跟上微信源库（页级增量，只解变化的库）。

    返回本次实际解密的相对路径，供 _refresh_dirs 立即导入，避免"文件刚
    更新、指纹还没被重新比较"导致新好友要等下一轮甚至更久才出现。
    """
    import fastsync
    import keyvault
    wanted: list[str] = []
    fps: dict[str, tuple] = {}
    for rel in ("contact/contact.db", "session/session.db"):
        src = ACCOUNT_DIR / "db_storage" / rel
        try:
            st = src.stat()
        except OSError:
            continue
        wal = Path(str(src) + "-wal")
        try:
            wst = wal.stat() if wal.exists() else None
        except OSError:
            wst = None
        fp = (st.st_mtime, st.st_size,
              wst.st_mtime if wst else 0, wst.st_size if wst else 0)
        if _dir_src_fp.get(rel) != fp:
            wanted.append(rel)
            fps[rel] = fp
    if not wanted:
        return []
    key = keyvault.load_key(ACCOUNT_ID)
    if not key:
        return []                   # 没密钥不标记，等拿到密钥后重试
    updated = fastsync.fast_sync_message_dbs(ACCOUNT_DIR, DECRYPTED_DIR, key, dbs=wanted)
    _dir_src_fp.update(fps)
    return updated or []


def _decrypted_path(rel: str) -> Path:
    p = DECRYPTED_DIR / rel
    return p if p.exists() else DECRYPTED_DIR / Path(rel).name


def _import_contacts(con, p: Path) -> int:
    """contact.db → contact 表。

    原来 contact 表只在首次导入时写一次，之后新加的好友/改备注永远不会
    出现在通讯录里（消息能导进来，人却搜不到）。这里做增量 upsert。
    """
    src = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in src.execute("PRAGMA table_info(contact)")}
        sel = ["username", "nick_name", "remark"]
        for c in ("alias", "pin_yin_initial", "quan_pin",
                  "local_type", "flag", "delete_flag"):
            sel.append(c if c in cols else f"NULL AS {c}")
        rows = src.execute(f"SELECT {','.join(sel)} FROM contact "
                           "WHERE username IS NOT NULL AND username != ''").fetchall()
    finally:
        src.close()
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        lazy_pinyin = None
    letters: dict[str, str] = {}

    def initial(name: str) -> str:
        ch = (name or "").strip()[:1]
        if not ch:
            return "#"
        if ch not in letters:
            letter = lazy_pinyin(ch)[0][:1].upper() if lazy_pinyin else ch.upper()
            letters[ch] = letter if letter.isalnum() else "#"
        return letters[ch]

    vals = []
    for r in rows:
        name = ((r["remark"] or "").strip() or (r["nick_name"] or "").strip()
                or (r["alias"] or "").strip())
        vals.append((r["username"] or "", r["nick_name"], r["remark"], r["alias"],
                     r["pin_yin_initial"] or "", r["quan_pin"] or "",
                     int(r["local_type"] or 0), int(r["flag"] or 0),
                     int(r["delete_flag"] or 0), initial(name)))
    con.executemany("""
        INSERT INTO contact(username, nick_name, remark, alias, pin_yin_initial,
                            quan_pin, local_type, flag, delete_flag, initial_letter)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(username) DO UPDATE SET
            nick_name=excluded.nick_name, remark=excluded.remark,
            alias=excluded.alias, pin_yin_initial=excluded.pin_yin_initial,
            quan_pin=excluded.quan_pin, local_type=excluded.local_type,
            flag=excluded.flag, delete_flag=excluded.delete_flag,
            initial_letter=excluded.initial_letter
    """, vals)
    return len(vals)


def _import_sessions(con, p: Path) -> int:
    """session.db → chat 表：新增会话补行，已有会话保留置顶/摘要只更新名称。"""
    src = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute("SELECT username, summary, last_timestamp "
                           "FROM SessionTable").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        src.close()
    names = {r[0]: r[1] for r in con.execute(
        "SELECT username, COALESCE(NULLIF(TRIM(remark),''), NULLIF(TRIM(nick_name),''),"
        " NULLIF(TRIM(alias),''), username) FROM contact")}
    n = 0
    for r in rows:
        u = r["username"]
        if not u:
            continue
        name = names.get(u) or u
        is_group = int("@chatroom" in u or "@openim" in u)
        cur = con.execute("""
            INSERT INTO chat(username, is_group, display_name, last_ts, summary)
            VALUES(?,?,?,?,?)
            ON CONFLICT(username) DO UPDATE SET
                is_group=excluded.is_group,
                display_name=CASE WHEN excluded.display_name != excluded.username
                                  THEN excluded.display_name ELSE chat.display_name END,
                last_ts=MAX(IFNULL(chat.last_ts,0), IFNULL(excluded.last_ts,0)),
                summary=CASE WHEN IFNULL(excluded.summary,'') != ''
                             THEN excluded.summary ELSE chat.summary END
        """, (u, is_group, name, int(r["last_timestamp"] or 0), r["summary"] or ""))
        n += cur.rowcount
    return n


def _refresh_dirs(force: bool = False) -> dict:
    """联系人/会话增量刷新（通讯录搜不到人、会话列表漏新聊天都靠这里兜住）。

    调用点：实时同步每轮、补漏重扫、启动时。按解密产物指纹跳过未变化的库，
    开销只有 stat；contact.db 变了才会做 1.8 万行 upsert。
    """
    out = {"contacts": 0, "chats": 0}
    updated: list[str] = []
    try:
        updated = _sync_decrypted_dirs() or []
    except Exception as exc:      # noqa: BLE001 解密失败时仍用已有产物导入
        _realtime["dir_err"] = str(exc)[:120]
    con = db()
    try:
        p = _decrypted_path("contact/contact.db")
        fp = (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
        if fp and (force or _dir_sync["contact"] != fp
                   or "contact/contact.db" in updated):
            out["contacts"] = _import_contacts(con, p)
            _dir_sync["contact"] = fp
        p = _decrypted_path("session/session.db")
        fp = (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
        if fp and (force or _dir_sync["session"] != fp
                   or "session/session.db" in updated):
            out["chats"] = _import_sessions(con, p)
            _dir_sync["session"] = fp
        changed = bool(out["contacts"] or out["chats"] or force)
        healed = False
        # 有消息但没有会话行的（首导后新开的聊天/历史漏导）：10 分钟查一次
        if changed or time.time() - _dir_sync["heal_ts"] > 600:
            _dir_sync["heal_ts"] = time.time()
            if con.execute("SELECT 1 FROM message m WHERE NOT EXISTS "
                           "(SELECT 1 FROM chat c WHERE c.username=m.chat_user) "
                           "LIMIT 1").fetchone():
                con.execute("""
                    INSERT OR IGNORE INTO chat(username, is_group, display_name,
                                               last_ts, summary)
                    SELECT m.chat_user,
                           MAX(CASE WHEN INSTR(m.chat_user,'@chatroom')>0
                                     OR INSTR(m.chat_user,'@openim')>0 THEN 1 ELSE 0 END),
                           COALESCE((SELECT COALESCE(NULLIF(TRIM(c.remark),''),
                                                     NULLIF(TRIM(c.nick_name),''),
                                                     NULLIF(TRIM(c.alias),''),
                                                     m.chat_user)
                                     FROM contact c WHERE c.username=m.chat_user),
                                    m.chat_user),
                           MAX(m.ts), ''
                    FROM message m LEFT JOIN chat ch ON ch.username=m.chat_user
                    WHERE ch.username IS NULL GROUP BY m.chat_user
                """)
                healed = True
        if changed or healed:
            # 改名/改备注后会话列表立即跟着变
            con.execute("""
                UPDATE chat SET display_name = (
                    SELECT COALESCE(NULLIF(TRIM(c.remark),''), NULLIF(TRIM(c.nick_name),''),
                                    NULLIF(TRIM(c.alias),''), chat.display_name)
                    FROM contact c WHERE c.username = chat.username)
                WHERE EXISTS (SELECT 1 FROM contact c WHERE c.username = chat.username
                              AND COALESCE(NULLIF(TRIM(c.remark),''),
                                           NULLIF(TRIM(c.nick_name),''),
                                           NULLIF(TRIM(c.alias),''), '') != '')
            """)
            con.execute("""UPDATE chat SET last_ts =
                (SELECT MAX(ts) FROM message m WHERE m.chat_user = chat.username)
                WHERE EXISTS (SELECT 1 FROM message m WHERE m.chat_user = chat.username
                              AND m.ts > chat.last_ts)""")
        con.commit()
    finally:
        con.close()
    return out


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
    try:
        _refresh_dirs()
    except Exception as exc:      # noqa: BLE001 目录刷新失败不影响消息导入
        _realtime["dir_err"] = str(exc)[:120]
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
    try:
        _refresh_dirs()
    except Exception as exc:      # noqa: BLE001 目录刷新失败不影响消息导入
        _realtime["dir_err"] = str(exc)[:120]
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
    try:
        _refresh_dirs()
    except Exception as exc:      # noqa: BLE001 目录刷新失败不影响消息导入
        _realtime["dir_err"] = str(exc)[:120]
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



def _fetch_error_text(exc) -> str:
    """把抓取异常翻成可读原因（和模型错误一样，不把 SSL 原文丢给用户）。"""
    s = str(exc)
    low = s.lower()
    if "ssl" in low or "unexpected_eof" in low or "certificate" in low:
        return "抓取失败：TLS 握手失败，通常是代理/VPN 不通或站点被网络拦截"
    if "timed out" in low or "timeout" in low:
        return "抓取失败：连接超时"
    if "getaddrinfo" in low or "name or service not known" in low:
        return "抓取失败：域名解析失败（DNS/代理异常）"
    if "connection" in low and ("refused" in low or "reset" in low or "aborted" in low):
        return "抓取失败：连接被拒绝或重置"
    return f"抓取失败：{type(exc).__name__} {s[:80]}"


def fetch_article(url: str, force: bool = False) -> dict:
    """抓取公众号/网页正文（带缓存）。小红书/B站走深度解析（OCR/字幕/ASR）。"""
    from bs4 import BeautifulSoup
    # 统一 URL 形式：消息里是 &amp;，与缓存键必须一致，否则"解析过却查不到"
    # 入口也做校验：该函数既被 HTTP API 调用，也会被 Agent/后台链接线程直接调用。
    url = _validate_external_url(url)
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
        resp = _safe_external_get(url, timeout=20, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
        if resp.status_code >= 400:
            con.close()
            return {"url": url, "title": "", "text": "", "chars": 0,
                    "error": f"目标站点返回 HTTP {resp.status_code}"
                             "（可能需要登录或触发了反爬）"}
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
        return {"url": url, "error": _fetch_error_text(e),
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
        text = ""
        # 1) trafilatura：通用正文抽取最稳，顺便拿标题/日期
        try:
            import trafilatura
            text = trafilatura.extract(html, url=url, include_comments=False,
                                       include_tables=True, favor_precision=False) or ""
            if not title:
                meta = trafilatura.extract_metadata(html, default_url=url)
                title = (getattr(meta, "title", "") or "") if meta else ""
        except Exception:      # noqa: BLE001
            text = ""
        # 2) GNE：中文新闻/公众号类页面兜底
        if len(text.strip()) < 80:
            try:
                from gne import GeneralNewsExtractor
                g = GeneralNewsExtractor().extract(html, with_body_html=False)
                gtext = (g.get("content") or "").strip()
                if len(gtext) > len(text.strip()):
                    text = gtext
                    title = title or (g.get("title") or "")
            except Exception:      # noqa: BLE001
                pass
        # 3) 最后才退回标签密度最简单的 body 文本
        if len(text.strip()) < 80:
            for bad in soup(["script", "style", "nav", "header", "footer",
                             "aside", "noscript"]):
                bad.decompose()
            body = (soup.select_one("article") or soup.select_one("main")
                    or soup.body or soup)
            text = body.get_text(chr(10), strip=True)

    text = re.sub(chr(10) + "{3,}", chr(10) + chr(10), text).strip()[:60000]
    title = (title or "").strip()[:120]
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
    if d.get("title") and len(d["title"]) > 60:
        d["title"] = d["title"][:120] + "…"
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
    from fastapi.responses import Response
    u = _validate_external_url(u)
    host = urllib.parse.urlparse(u).netloc
    ref = "https://www.bilibili.com/" if "bili" in host or "hdslb" in host else \
          ("https://www.xiaohongshu.com/" if "xhscdn" in host else "https://" + host + "/")
    headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/120 Safari/537.36"),
               "Referer": ref}
    try:
        with _safe_external_get(u, headers=headers, timeout=20, stream=True) as r:
            if not r.ok:
                raise HTTPException(502, f"图片 {r.status_code}")
            content_type = (r.headers.get("content-type") or "").split(";", 1)[0].lower()
            if content_type and not content_type.startswith("image/"):
                raise HTTPException(400, "目标不是图片")
            content = r.raw.read(12 * 1024 * 1024 + 1)
            if len(content) > 12 * 1024 * 1024:
                raise HTTPException(413, "图片过大")
            return Response(content=content, media_type=content_type or "image/jpeg",
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
            encoding="utf-8", errors="ignore", bufsize=1, **_no_window())
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
    """message_*.db 及其 -wal 的 (mtime, size) 指纹，用于检测"微信刚写了新消息"。

    联系人/会话库也一起盯：加好友、改备注、新开会话不写消息库，只靠消息
    文件变化的话这些变化要等下次有人发消息才会被发现。
    """
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
    for rel in ("contact/contact.db", "contact/contact.db-wal",
                "session/session.db", "session/session.db-wal"):
        p = ACCOUNT_DIR / "db_storage" / rel
        try:
            st = p.stat()
            fp[rel] = (int(st.st_mtime), st.st_size)
        except OSError:
            continue
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
                    # 向量补嵌已移到独立线程 _vec_loop(嵌入 30 条约 10 秒,
                    # 放在这里会把实时同步拖慢十几秒)
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
                        # 只处理最近 7 天的新消息：实时同步只管近期(离线几天再
                        # 上线的消息 ts 也在窗口内)，更早的历史积压交给设置页
                        # 批量任务，否则每轮全速 OCR/ASR 会吃满 CPU 拖慢整个程序
                        stats = process_new_media(
                            db(), ocr_cap=30 if rt.get("media_ocr", True) else 0,
                            asr_cap=10 if rt.get("media_asr", True) else 0,
                            video_cap=2 if rt.get("media_asr", True) else 0,
                            since=int(time.time()) - 7 * 86400)
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
            "fast_realtime": bool(st.get("fast_realtime", True)),
            "vec_realtime": bool(st.get("vec_realtime", True)),
            "media_on": bool(st.get("media_ocr", True)),
            "asr_on": bool(st.get("media_asr", True)),
            "last": _realtime["last"], "added": _realtime["added"],
            "media_ocr": media.get("ocr", 0), "media_asr": media.get("asr", 0),
            "error": _realtime["error"],
            "live_err": _realtime.get("live_err"),
            "fast_err": _realtime.get("fast_err"),
            "selfheal": _realtime.get("selfheal"),
            "tick": _realtime["tick"]}


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
    if "fast_realtime" in payload:
        st["fast_realtime"] = bool(payload["fast_realtime"])
    if "vec_realtime" in payload:
        st["vec_realtime"] = bool(payload["vec_realtime"])
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


_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = "WechatKnowledgeAssistant"


def _autostart_target() -> str:
    """开机自启要执行的命令：优先 exe，其次 bat，最后 pythonw server.py。"""
    exe = PROJECT / "Qwent.exe"
    if exe.is_file():
        return f'"{exe}"'
    bat = PROJECT / "启动助手.bat"
    if bat.is_file():
        return f'"{bat}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.is_file():
        pyw = Path(sys.executable)
    return f'"{pyw}" "{PROJECT / "backend" / "server.py"}" 8721'


def _autostart_get() -> dict:
    if os.name != "nt":
        return {"supported": False, "enabled": False, "target": ""}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as k:
            val, _ = winreg.QueryValueEx(k, _AUTOSTART_NAME)
        return {"supported": True, "enabled": True, "target": str(val)}
    except FileNotFoundError:
        return {"supported": True, "enabled": False, "target": ""}
    except Exception as exc:  # noqa: BLE001
        return {"supported": True, "enabled": False, "target": "", "error": str(exc)[:120]}


def _autostart_set(enabled: bool) -> dict:
    if os.name != "nt":
        return {"ok": False, "error": "仅支持 Windows"}
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  _autostart_target())
            else:
                try:
                    winreg.DeleteValue(k, _AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return {"ok": True, **_autostart_get()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}


def _dir_size(path: Path, limit_files: int = 200000) -> tuple[int, int]:
    """目录大小与文件数（有上限，避免超大目录卡住接口）。"""
    total = n = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                    n += 1
                except OSError:
                    continue
                if n >= limit_files:
                    return total, n
    except OSError:
        pass
    return total, n


@app.get("/api/storage")
def api_storage():
    """本机数据占用总览：knowledge.db + 各媒体/缓存目录，微信与 QQ 分开统计。"""
    con = db()
    try:
        def _count(sql):
            try:
                return int(con.execute(sql).fetchone()[0] or 0)
            except sqlite3.OperationalError:
                return 0
        wx_rows = {
            "messages": _count("SELECT COUNT(*) FROM message"),
            # 有效消息（排除公众号/系统提示等噪音），与「知识库概览」口径一致
            "messages_real": _count("SELECT COUNT(*) FROM message WHERE noise=0"),
            "chats": _count("SELECT COUNT(*) FROM chat"),
            "contacts": _count("SELECT COUNT(*) FROM contact"),
            "files": _count("SELECT COUNT(*) FROM file_text"),
            "links": _count("SELECT COUNT(*) FROM link_cache"),
        }
        qq_rows = {
            "messages": _count("SELECT COUNT(*) FROM qq_local_message"),
            "chats": _count("SELECT COUNT(*) FROM qq_chat"),
            "contacts": _count("SELECT COUNT(*) FROM qq_contact"),
            "files": _count("SELECT COUNT(*) FROM qq_file"),
            "links": _count("SELECT COUNT(*) FROM qq_link"),
        }
    finally:
        con.close()

    def _db_file(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError:
            return 0

    db_total = sum(_db_file(Path(str(KNOWLEDGE_DB) + ext))
                   for ext in ("", "-wal", "-shm"))
    wx_media = DATA / "media_cache"
    wx_dec = DATA / "decrypted"
    qq_live = DATA / "qq_live"
    qq_media = DATA / "qq_media"
    qq_av = DATA / "qq_avatar"
    qq_th = DATA / "qq_thumbs"
    qq_fetched = DATA / "qq_fetched"
    dirs = {}
    for name, path in (("media_cache", wx_media), ("decrypted", wx_dec),
                       ("qq_live", qq_live), ("qq_media", qq_media),
                       ("qq_avatar", qq_av), ("qq_thumbs", qq_th),
                       ("qq_fetched", qq_fetched)):
        size, n = _dir_size(path)
        dirs[name] = {"bytes": size, "files": n, "path": str(path)}
    wx_media_bytes = dirs["media_cache"]["bytes"] + dirs["decrypted"]["bytes"]
    qq_media_bytes = (dirs["qq_live"]["bytes"] + dirs["qq_media"]["bytes"]
                      + dirs["qq_avatar"]["bytes"] + dirs["qq_thumbs"]["bytes"]
                      + dirs["qq_fetched"]["bytes"])
    # 数据库是两边共用的一个文件，按消息条数比例分摊估算各自占用
    wx_n = max(0, wx_rows["messages"])
    qq_n = max(0, qq_rows["messages"])
    share = (wx_n / (wx_n + qq_n)) if (wx_n + qq_n) else 0.5
    wx_db = int(db_total * share)
    qq_db = db_total - wx_db
    wx_bytes = wx_media_bytes + wx_db
    qq_bytes = qq_media_bytes + qq_db
    return {
        "db": {"bytes": db_total, "path": str(KNOWLEDGE_DB),
               "size": _db_file(KNOWLEDGE_DB), "main": _db_file(KNOWLEDGE_DB)},
        "wechat": {"bytes": wx_bytes, "media_bytes": wx_media_bytes,
                   "db_bytes": wx_db, "share": round(share, 4), "rows": wx_rows,
                   "dirs": {"media_cache": dirs["media_cache"],
                            "decrypted": dirs["decrypted"]}},
        "qq": {"bytes": qq_bytes, "media_bytes": qq_media_bytes,
               "db_bytes": qq_db, "rows": qq_rows, "dirs": {
            "qq_live": dirs["qq_live"], "qq_media": dirs["qq_media"],
            "qq_avatar": dirs["qq_avatar"], "qq_thumbs": dirs["qq_thumbs"],
            "qq_fetched": dirs["qq_fetched"]}},
        "total": {"bytes": db_total + wx_media_bytes + qq_media_bytes,
                  "wechat": wx_bytes, "qq": qq_bytes,
                  "db_bytes": db_total,
                  "media_bytes": wx_media_bytes + qq_media_bytes},
    }


def _autostart_ensure_default():
    """首次启动默认登记开机自启（用户关掉后不再自动恢复）。"""
    try:
        conf = load_conf()
        st = conf.setdefault("app", {})
        if st.get("autostart_initialized"):
            return
        st["autostart_initialized"] = True
        CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
        if not _autostart_get().get("enabled"):
            _autostart_set(True)
    except Exception as exc:  # noqa: BLE001
        print(f"[autostart] init failed: {exc}")


@app.get("/api/autostart")
def api_autostart_get():
    return _autostart_get()


@app.post("/api/autostart")
def api_autostart_set(payload: dict):
    res = _autostart_set(bool(payload.get("enabled")))
    if not res.get("ok"):
        raise HTTPException(500, res.get("error") or "设置失败")
    return res


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


_job_totals_cache = {"ts": 0.0, "data": {}, "computing": False}


def _job_totals_bg():
    """后台算总数：文件/朋友圈要逐条解析内容，可能耗时数秒，不能堵住接口。"""
    _job_totals_cache["computing"] = True
    con = None
    try:
        con = db()
        _job_totals_compute(con)
    except Exception:      # noqa: BLE001
        pass
    finally:
        _job_totals_cache["computing"] = False
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def _job_totals(con) -> dict:
    """返回缓存的总数；过期则后台重算（不阻塞请求），暂无数据时返回空。"""
    now = time.time()
    if now - _job_totals_cache["ts"] >= 3600 and not _job_totals_cache["computing"]:
        threading.Thread(target=_job_totals_bg, daemon=True).start()
    return _job_totals_cache["data"]


def _job_totals_compute(con) -> dict:
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
    _job_totals_cache.update(ts=time.time(), data=data)
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


# ---------------------------------------------------------------- API: 平台 / 双通道设置
@app.get("/api/platform")
def api_platform_get():
    """界面平台偏好（微信 / QQ）。前端同时记 localStorage，这里做跨设备兜底。"""
    ui = load_conf().get("ui") or {}
    p = ui.get("platform")
    return {"platform": p if p in ("wechat", "qq") else "wechat",
            "supported": ["wechat", "qq"]}


@app.post("/api/platform")
def api_platform_set(payload: dict):
    p = str(payload.get("platform") or "").strip().lower()
    if p not in ("wechat", "qq"):
        raise HTTPException(400, "platform 只能是 wechat 或 qq")
    conf = load_conf()
    conf.setdefault("ui", {})["platform"] = p
    CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "platform": p}


@app.get("/api/wxagent/config")
def api_wxagent_config_get():
    import wxagent
    a = (load_conf().get("agent_api") or {})
    w = (load_conf().get("wxagent") or {})
    return {"enabled": bool(a.get("enabled")),
            "model": str(a.get("model") or "wechat-kb"),
            "has_key": bool(a.get("api_key")),
            "key": str(a.get("api_key") or ""),
            "base_url": "http://127.0.0.1:8721/v1",
            "default_explorer": bool(w.get("default_explorer")),
            "default_thinking": bool(w.get("default_thinking"))}


@app.post("/api/wxagent/config")
def api_wxagent_config_set(payload: dict):
    """微信通道独立设置：开关、接口密钥、默认提问方式。不再包含任何 QQ 字段。"""
    conf = load_conf()
    w = conf.setdefault("wxagent", {})
    a = conf.setdefault("agent_api", {})
    for k in ("default_explorer", "default_thinking"):
        if k in payload:
            w[k] = bool(payload[k])
    if "enabled" in payload:
        a["enabled"] = bool(payload["enabled"])
    if payload.get("model"):
        a["model"] = str(payload["model"]).strip() or "wechat-kb"
    if payload.get("regenerate") or (a.get("enabled") and not a.get("api_key")):
        import secrets as _s
        a["api_key"] = "wk-" + _s.token_hex(16)
    CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "enabled": bool(a.get("enabled")),
            "key": str(a.get("api_key") or ""), "model": a.get("model")}


@app.get("/api/wxagent/help")
def api_wxagent_help():
    """设置页预览：微信 ClawBot 里 /帮助 会看到的完整内容。"""
    import wxagent
    return {"help": wxagent.help_text(), "status": wxagent.status_text()}


@app.get("/api/qqagent/help")
def api_qqagent_help():
    """设置页预览：QQ 机器人 /帮助 与菜单定义（两者能力一致）。"""
    import qqagent
    import qqbot as qqbot_mod
    import botcore
    conf = qqbot_mod.conf_of(load_conf())
    return {"help": qqagent.help_text("__preview__", conf),
            "menu": qqagent.MENU_ITEMS,
            "commands": [{"key": k, "action": v}
                         for k, v in sorted(botcore.ALIASES.items())]}


# ---------------------------------------------------------------- API: QQ 消息（qqbot 接入）
@app.get("/api/qq/conversations")
def api_qq_conversations():
    con = db()
    try:
        return {"conversations": qq_messages.conversations(con)}
    finally:
        con.close()


@app.get("/api/qq/messages")
def api_qq_messages(conv: str = "", limit: int = 200):
    if not conv:
        raise HTTPException(400, "缺少 conv")
    con = db()
    try:
        items = qq_messages.messages(con, conv, limit)
        names = {x["conv"]: x["name"] for x in qq_messages.conversations(con)}
        return {"conv": conv, "name": names.get(conv) or conv, "messages": items}
    finally:
        con.close()


@app.post("/api/qq/conversations/rename")
def api_qq_conversation_rename(payload: dict):
    conv = str(payload.get("conv") or "")
    if not conv.startswith(("c2c:", "group:")):
        raise HTTPException(400, "无效的会话")
    con = db()
    try:
        name = qq_messages.rename(con, conv, payload.get("name"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        con.close()
    return {"ok": True, "conv": conv, "name": name}


@app.get("/api/qq/overview")
def api_qq_overview():
    con = db()
    try:
        data = qq_messages.overview(con)
        try:
            data["local"] = qq_local.stats(con)
        except Exception:
            data["local"] = {}
    finally:
        con.close()
    try:
        import qqbot as qqbot_mod
        st = qqbot_mod.status()
        data["bot_connected"] = bool(st.get("connected"))
        data["bot_running"] = bool(st.get("running"))
        data["bot_name"] = st.get("username") or ""
    except Exception:
        data["bot_connected"] = False
    return data


@app.get("/api/qq/media/{name}")
def api_qq_media(name: str):
    """QQ 消息附件（图片/语音/文件）；只允许读取 data/qq_media 下的文件名。"""
    safe = Path(name).name
    path = DATA / "qq_media" / safe
    if not safe or not path.is_file():
        raise HTTPException(404, "附件不存在")
    return FileResponse(path)


@app.get("/api/qq/search")
def api_qq_search(q: str = "", n: int = 50):
    con = db()
    try:
        local = qq_local.search(con, q, n)
        for x in local:
            x["source"] = "local"
        bot = qq_messages.search(con, q, n)
        for x in bot:
            x["source"] = "bot"
            x["conv_name"] = x.get("conv_name") or x.get("conv") or ""
        out = sorted(local + bot, key=lambda x: x.get("ts") or 0, reverse=True)[:n]
        return {"items": out}
    finally:
        con.close()


# ---------------------------------------------------------------- QQ 自动同步（NT 数据库指纹 → 增量导入 → 自动解析）
_qq_sync_state = {"running": False, "last": "", "last_result": None, "error": "",
                  "progress": "", "finger": {}, "last_ok": 0.0, "booted": False}
_qq_sync_wake = threading.Event()


def _qq_sync_conf() -> dict:
    conf = load_conf()
    st = conf.get("qq_sync", {})
    return {"auto": bool(st.get("auto", True)),
            "realtime": bool(st.get("realtime", True)),
            "interval_min": max(5, int(st.get("interval_min", 10)))}


def _qq_sync_once(progress=None, light: bool = False) -> dict:
    """增量同步一次：库里没数据则全量导入；否则只导最近窗口。

    light=True（实时触发）：只解密 nt_msg.db（约 6 秒），窗口缩到 2h，
    新消息从出现到入库一般在十几秒内。
    """
    con = db()
    try:
        n = con.execute("SELECT COUNT(*) FROM qq_local_message").fetchone()[0]
        if n == 0:
            if progress:
                progress("首次导入，正在解密全库…")
            return qq_local.import_all(con, rebuild=True, progress=progress)
        if progress:
            progress("准备解密最近记录…")
        since = int(time.time()) - ((2 if light else 49) * 3600)
        return qq_local.import_all(con, rebuild=False, since_ts=since,
                                   light=light, progress=progress)
    finally:
        con.close()


def _qq_auto_parse_after_sync():
    """同步后补解析新文件/链接（与导入后的自动解析同一套 worker）。"""
    try:
        if not _qq_file_parse_state["running"]:
            _qq_file_parse_state.update({"running": True, "done": 0, "total": 0,
                                         "progress": "自动解析文件…", "error": "", "stage": ""})
            _qq_file_parse_worker(80)
        if not _qq_link_parse_state["running"]:
            _qq_link_parse_state.update({"running": True, "done": 0, "total": 0,
                                         "progress": "自动解析链接…", "error": "", "last": ""})
            _qq_link_parse_worker(10)
    except Exception as exc:  # noqa: BLE001
        print(f"[qq_sync] auto parse failed: {exc}")


def _qq_sync_run(reason: str = "", light: bool = False):
    """执行一次同步（增量优先），成功后记录指纹并补解析。"""
    if _qq_sync_state["running"]:
        return
    _qq_sync_state["running"] = True
    _qq_sync_state["error"] = ""
    try:
        def _p(s):
            _qq_sync_state["progress"] = s
        result = _qq_sync_once(progress=_p, light=light)
        _qq_sync_state["last_result"] = result
        _qq_sync_state["progress"] = "同步完成"
        _qq_sync_state["last"] = time.strftime("%m-%d %H:%M")
        _qq_sync_state["last_ok"] = time.time()
        _qq_sync_state["finger"] = qq_local.db_fingerprints()
        if result.get("messages") or result.get("files") or result.get("links"):
            threading.Thread(target=_qq_auto_parse_after_sync, daemon=True).start()
        if reason:
            print(f"[qq_sync] {reason}: +{result.get('messages')} messages, "
                  f"+{result.get('files')} files, +{result.get('links')} links")
    except Exception as exc:  # noqa: BLE001
        _qq_sync_state["error"] = str(exc)[:200]
        _qq_sync_state["progress"] = "同步失败"
    finally:
        _qq_sync_state["running"] = False


_QQ_WATCH = {"handle": None}


def _qq_watcher_wait(timeout_ms: int = 1000) -> bool:
    """Windows 文件系统通知：静默等待目录变化（零 CPU），不用轮询。

    FindFirstChangeNotificationW 监听 nt_db 目录的写入/改名/大小变化，
    返回 True 表示有变化（随即重置），False 为超时。
    """
    import ctypes
    from ctypes import wintypes
    if os.name != "nt":
        time.sleep(timeout_ms / 1000.0)
        return True
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        FILE_NOTIFY_CHANGE_FILE_NAME = 0x1
        FILE_NOTIFY_CHANGE_SIZE = 0x8
        FILE_NOTIFY_CHANGE_LAST_WRITE = 0x10
        flags = (FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_SIZE
                 | FILE_NOTIFY_CHANGE_LAST_WRITE)
        h = _QQ_WATCH["handle"]
        if not h:
            account = qq_local.pick_account()
            dbdir = str(account / "nt_qq" / "nt_db")
            h = k32.FindFirstChangeNotificationW(dbdir, False, flags)
            if h in (None, 0, wintypes.HANDLE(-1).value):
                _QQ_WATCH["handle"] = None
                time.sleep(1)
                return True
            _QQ_WATCH["handle"] = h
        r = k32.WaitForSingleObject(h, timeout_ms)
        if r == 0:                       # WAIT_OBJECT_0：有变化
            k32.FindNextChangeNotification(h)
            return True
        return False
    except Exception:
        time.sleep(timeout_ms / 1000.0)
        return True


def _qq_rt_loop():
    """实时同步 v2（WAL 帧级增量）：

    每 2 秒看 nt_msg.db + -wal 指纹（0.002s）→ 变化抖动 1 秒 →
    live_update 只解密 WAL 新帧（毫秒级）打补丁进本地解密副本 →
    对副本走轻量导入（只解析重叠 15 分钟窗口）。
    新消息从 QQ 落库到本助手可查约 2~5 秒。
    任何一步失败回退整库轻量同步（约 6 秒）。
    """
    last_st = ("", 0)
    live_dir = KNOWLEDGE_DB.parent / "qq_live"
    while True:
        try:
            conf = _qq_sync_conf()
            if not (conf["auto"] and conf["realtime"]):
                time.sleep(5)
                continue
            if _qq_sync_state["running"]:
                time.sleep(2)
                continue
            try:
                account = qq_local.pick_account()
            except Exception:
                time.sleep(15)
                continue
            p = account / "nt_qq" / "nt_db" / "nt_msg.db"
            if not p.is_file():
                time.sleep(15)
                continue
            wal_p = Path(str(p) + "-wal")
            try:
                wst0 = wal_p.stat()
            except OSError:
                wst0 = None
            st0 = p.stat()
            cur = (int(st0.st_mtime), st0.st_size,
                   int(wst0.st_mtime) if wst0 else 0, wst0.st_size if wst0 else 0)
            first_rt = not _qq_sync_state.get("rt_booted")
            if not first_rt and (cur == last_st or cur == _qq_sync_state["finger"].get("nt_msg.db")):
                # 等文件系统通知（零 CPU）而不是定时轮询；超时 5 秒兜底
                _qq_watcher_wait(5000)
                continue
            # 抖动 0.3 秒：等 QQ 写完这一波帧（再短会读到写了一半的 WAL）
            time.sleep(0.3)
            try:
                wst1 = wal_p.stat()
            except OSError:
                wst1 = None
            st1 = p.stat()
            cur1 = (int(st1.st_mtime), st1.st_size,
                    int(wst1.st_mtime) if wst1 else 0, wst1.st_size if wst1 else 0)
            if cur1 != cur:
                continue
            last_st = cur1
            if _qq_sync_state["running"]:
                continue
            _qq_sync_state["rt_booted"] = True
            _qq_sync_state["running"] = True
            _qq_sync_state["error"] = ""
            _t0 = time.time()
            try:
                def _p(s):
                    _qq_sync_state["progress"] = s
                con = db()
                try:
                    r = qq_local.live_update(con, live_dir, progress=_p)
                    # 游标增量：只读 rowid > 上次的最大 rowid（走主键索引，不扫全表）
                    cursors = {}
                    for k, v in con.execute(
                            "SELECT key,value FROM meta WHERE key LIKE 'qq_rt_rid_%'"):
                        if str(v).isdigit():
                            cursors[k[len('qq_rt_rid_'):]] = int(v)
                    result = qq_local.import_all(
                        con, rebuild=False, since_ts=0 if cursors else int(time.time()) - 49 * 3600,
                        light=True, decrypt_dir=live_dir, progress=_p,
                        after_rowid=cursors if cursors else None)
                    for tbl, rid in (result.get("max_rowid") or {}).items():
                        con.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                    ("qq_rt_rid_" + tbl, str(rid)))
                    con.commit()
                finally:
                    con.close()
                _qq_sync_state["last_result"] = result
                _qq_sync_state["progress"] = "同步完成"
                _qq_sync_state["last_ms"] = int((time.time() - _t0) * 1000)
                _qq_sync_state["last"] = time.strftime("%m-%d %H:%M:%S")
                _qq_sync_state["last_ok"] = time.time()
                _qq_sync_state["finger"] = qq_local.db_fingerprints()
                if result.get("messages") or result.get("files") or result.get("links"):
                    print(f"[qq_rt] +{result.get('messages')} msgs, "
                          f"+{result.get('files')} files, +{result.get('links')} links "
                          f"(patched={r.get('patched')})")
                    _qq_bump_version()
                    threading.Thread(target=_qq_auto_parse_after_sync, daemon=True).start()
            except Exception as exc:  # noqa: BLE001
                _qq_sync_state["error"] = str(exc)[:200]
                _qq_sync_state["progress"] = "同步失败"
                print(f"[qq_rt] fallback to full light sync: {exc}")
                _qq_sync_state["running"] = False
                _qq_sync_run("rt-fallback", light=True)
                time.sleep(10)
                continue
            finally:
                _qq_sync_state["running"] = False
            _qq_watcher_wait(1000)       # 喂掉本轮自己写入触发的通知
        except Exception as exc:  # noqa: BLE001
            print(f"[qq_rt] loop error: {exc}")
            time.sleep(15)


def _qq_sync_loop():
    """每分钟看一眼 QQ 库指纹：变化且到间隔才解密导入（启动时先同步一次）。"""
    while True:
        try:
            conf = _qq_sync_conf()
            if not conf["auto"]:
                _qq_sync_wake.wait(timeout=60)
                _qq_sync_wake.clear()
                continue
            finger = qq_local.db_fingerprints()
            changed = bool(finger) and finger != _qq_sync_state["finger"]
            due = time.time() - _qq_sync_state["last_ok"] >= conf["interval_min"] * 60
            first = not _qq_sync_state["booted"]
            err_retry = bool(_qq_sync_state["error"]) and                 time.time() - _qq_sync_state["last_ok"] >= 300
            if _qq_sync_state["running"]:
                time.sleep(5)
                continue
            if first or (changed and due) or err_retry:
                _qq_sync_state["booted"] = True
                if not finger:
                    # QQ 未登录/未安装：标记错误，等下轮重试
                    _qq_sync_state["error"] = "QQ 未运行或未找到本地数据库，登录 QQ 后自动重试"
                    _qq_sync_wake.wait(timeout=120)
                    _qq_sync_wake.clear()
                    continue
                _qq_sync_run("startup" if first else "changed")
            _qq_sync_wake.wait(timeout=60)
            _qq_sync_wake.clear()
        except Exception as exc:  # noqa: BLE001
            print(f"[qq_sync] loop error: {exc}")
            time.sleep(60)


# ---------------------------------------------------------------- API: QQ 本地记录（NT 数据库导入）
_qq_import_lock = threading.Lock()
_qq_import_state = {"running": False, "progress": "", "result": None, "error": ""}


def _qq_import_worker(qq: str = "", rebuild: bool = True):
    con = None
    try:
        con = db()
        def _p(s):
            _qq_import_state["progress"] = s
        result = qq_local.import_all(con, qq=qq or None, rebuild=rebuild, progress=_p)
        _qq_import_state["result"] = result
        _qq_import_state["progress"] = "导入完成"
        # 导入后自动补解析：本机文件正文 + 链接正文（与微信的自动解析对齐）
        if result.get("files") or result.get("links"):
            threading.Thread(target=_qq_auto_parse_after_import, daemon=True).start()
        # 手动全量导入后刷新指纹，避免自动同步立刻重复跑
        _qq_sync_state["finger"] = qq_local.db_fingerprints()
        _qq_sync_state["last"] = time.strftime("%m-%d %H:%M")
        _qq_sync_state["last_ok"] = time.time()
        if result.get("messages") or result.get("files") or result.get("links"):
            _qq_bump_version()
    except Exception as exc:  # noqa: BLE001
        _qq_import_state["error"] = str(exc)[:300]
        _qq_import_state["progress"] = "导入失败"
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        _qq_import_state["running"] = False


def _qq_auto_parse_after_import():
    """导入完成后串行补解析，避免两个 worker 同时写库。"""
    try:
        if not _qq_file_parse_state["running"]:
            _qq_file_parse_state.update({"running": True, "done": 0, "total": 0,
                                         "progress": "自动解析文件…", "error": ""})
            _qq_file_parse_worker(300)
        if not _qq_link_parse_state["running"]:
            _qq_link_parse_state.update({"running": True, "done": 0, "total": 0,
                                         "progress": "自动解析链接…", "error": "",
                                         "last": ""})
            _qq_link_parse_worker(20)
    except Exception as exc:  # noqa: BLE001
        log.warning("qq auto parse after import failed: %s", exc)


_qq_data_version = 0


def _qq_bump_version():
    global _qq_data_version
    _qq_data_version += 1


@app.get("/api/qq/local/stream")
def api_qq_local_stream(conv: str, after_id: int = 0):
    """SSE 推送 QQ 新消息（与微信 /api/messages/stream 同款机制）。

    服务端每 0.5 秒比对 id 水位，有新增立即下发；QQ 实时同步导入任何会话的
    新内容时附带 chats 事件，前端借此刷新会话列表（别的会话来消息也能看到）。
    无新增每 15 秒心跳；EventSource 断线自动重连。
    """
    from fastapi.responses import StreamingResponse

    def gen():
        global _qq_data_version
        last = int(after_id or 0)
        seen_version = _qq_data_version
        idle = 0
        nn = chr(10) * 2                    # SSE 事件以空行结尾
        yield "retry: 3000" + nn
        while True:
            try:
                con = db()
                rows = []
                if conv:
                    rows = qq_local.messages(con, conv, 40, after=last)
                version_changed = _qq_data_version != seen_version
                if rows:
                    last = max(last, max(r["id"] for r in rows))
                    payload = json.dumps({"messages": rows, "max_id": last},
                                         ensure_ascii=False)
                    yield "data: " + payload + nn
                    idle = 0
                if version_changed:
                    seen_version = _qq_data_version
                    yield "data: " + json.dumps({"chats": True}, ensure_ascii=False) + nn
                    idle = 0
                if not rows and not version_changed:
                    idle += 1
                    if idle >= 30:
                        idle = 0
                        yield ": ping" + nn
                con.close()
            except Exception:
                yield ": error" + nn
            time.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


_QQ_AV_DIR = KNOWLEDGE_DB.parent / "qq_avatar"
_QQ_AV_SEM = threading.Semaphore(8)          # 头像下载限并发，别拖垮别的接口
_QQ_AV_FAIL: dict[str, float] = {}           # 失败缓存：1 小时内不再重试（避免反复打 CDN）


def _qq_avatar_fetch(kind: str, key: str, size: int = 100):
    """QQ 头像：好友走 qlogo 号接口，群走群头像接口。列表用 100px（省带宽），详情 640。"""
    import qqagent
    big = "640" if size >= 200 else "100"
    urls = ([f"https://p.qlogo.cn/gh/{key}/{key}/{big}"]
            if kind == "group" else
            [f"https://q1.qlogo.cn/g?b=qq&nk={key}&s={big}"])
    for url in urls:
        try:
            r = qqagent._req("GET", url, timeout=8,
                             headers={"User-Agent": "Mozilla/5.0",
                                      "Referer": "https://q.qq.com/"})
            if r.status_code == 200 and len(r.content) > 500:
                return r.content
        except Exception:
            continue
    return None


@app.get("/api/qq/avatar")
def api_qq_avatar(kind: str = "friend", key: str = "", size: int = 100):
    """QQ 好友/群头像（本地缓存，离线可用）。key 可为 QQ 号/群号，也接受 uid（自动反查）。"""
    key = (key or "").strip()
    if not key:
        raise HTTPException(404, "no key")
    if not key.isdigit():
        con = db()
        try:
            row = con.execute("SELECT uin FROM qq_contact WHERE uid=? OR key=? LIMIT 1",
                              (key, f"c2c:{key}")).fetchone()
        finally:
            con.close()
        key = str(row[0]) if row and row[0] else ""
        if not key:
            raise HTTPException(404, "no avatar")
    _QQ_AV_DIR.mkdir(parents=True, exist_ok=True)
    fp = _QQ_AV_DIR / f"{kind}_{key}_{'640' if size >= 200 else '100'}.img"
    if fp.is_file() and fp.stat().st_size > 500:
        return FileResponse(str(fp), headers={"Cache-Control": "public, max-age=86400"})
    ck = f"{kind}:{key}"
    if _QQ_AV_FAIL.get(ck, 0) > time.time() - 3600:
        raise HTTPException(404, "no avatar")
    with _QQ_AV_SEM:
        data = _qq_avatar_fetch(kind, key, size)
    if not data:
        _QQ_AV_FAIL[ck] = time.time()
        raise HTTPException(404, "no avatar")
    fp.write_bytes(data)
    return FileResponse(str(fp), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/qq/local/sync/status")
def api_qq_local_sync_status():
    con = db()
    try:
        st = qq_local.stats(con)
    finally:
        con.close()
    return {"state": {k: v for k, v in _qq_sync_state.items() if k != "finger"},
            "config": _qq_sync_conf(), "stats": st}


@app.post("/api/qq/local/sync")
def api_qq_local_sync_now():
    """立即同步一次（增量），后台执行。"""
    if _qq_sync_state["running"]:
        raise HTTPException(409, "QQ 同步正在进行")
    threading.Thread(target=_qq_sync_run, args=("manual",), daemon=True).start()
    return {"ok": True, "started": True}


@app.post("/api/qq/local/sync/config")
def api_qq_local_sync_config(payload: dict):
    conf = load_conf()
    st = conf.setdefault("qq_sync", {})
    if "auto" in payload:
        st["auto"] = bool(payload["auto"])
    if "realtime" in payload:
        st["realtime"] = bool(payload["realtime"])
    if payload.get("interval_min"):
        st["interval_min"] = max(5, int(payload["interval_min"]))
    CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    if st.get("auto"):
        _qq_sync_wake.set()          # 重新开启时立刻检查一轮
    return {"ok": True, "auto": bool(st.get("auto", True)),
            "realtime": bool(st.get("realtime", True)),
            "interval_min": int(st.get("interval_min", 10))}


@app.post("/api/qq/local/import")
def api_qq_local_import(payload: dict | None = None):
    if _qq_import_state["running"]:
        raise HTTPException(409, "QQ 本地记录正在导入")
    req = payload or {}
    _qq_import_state.update({"running": True, "progress": "准备中…",
                             "result": None, "error": ""})
    th = threading.Thread(target=_qq_import_worker,
                          args=(str(req.get("qq") or ""), bool(req.get("rebuild", True))),
                          daemon=True)
    th.start()
    return {"ok": True, "started": True}


@app.get("/api/qq/local/import/status")
def api_qq_local_import_status():
    con = db()
    try:
        st = qq_local.stats(con)
    finally:
        con.close()
    return {"state": dict(_qq_import_state), "stats": st}


@app.get("/api/qq/local/sessions")
def api_qq_local_sessions(q: str = "", limit: int = 300):
    con = db()
    try:
        return {"sessions": qq_local.sessions(con, q, limit)}
    finally:
        con.close()


@app.post("/api/qq/local/session/pin")
def api_qq_local_session_pin(payload: dict):
    conv = str(payload.get("conv") or "")
    if not conv.startswith(("c2c:", "group:")):
        raise HTTPException(400, "无效的会话")
    con = db()
    try:
        qq_local.set_pinned(con, conv, bool(payload.get("pinned")))
        # 留痕：置顶被意外改动时能从日志追溯是谁改的
        try:
            import traceback
            caller = " / ".join(
                f"{f.name}:{f.lineno}" for f in traceback.extract_stack()[-4:-1])
        except Exception:
            caller = ""
        print(f"[qq_pin] {conv} -> {bool(payload.get('pinned'))} ({caller})")
    finally:
        con.close()
    return {"ok": True, "conv": conv, "pinned": bool(payload.get("pinned"))}


@app.get("/api/qq/local/messages")
def api_qq_local_messages(conv: str = "", limit: int = 200, before: int = 0,
                          around: int = 0, after: int = 0):
    if not conv:
        raise HTTPException(400, "缺少 conv")
    con = db()
    try:
        name = con.execute("SELECT name FROM qq_chat WHERE conv=?", (conv,)).fetchone()
        return {"conv": conv, "name": (name[0] if name else conv),
                "messages": qq_local.messages(con, conv, limit, before, around, after)}
    finally:
        con.close()


@app.get("/api/qq/local/contacts")
def api_qq_local_contacts(q: str = "", kind: str = "", limit: int = 500):
    con = db()
    try:
        return {"contacts": qq_local.contacts(con, q, kind, limit)}
    finally:
        con.close()


@app.get("/api/qq/local/files")
def api_qq_local_files(q: str = "", kind: str = "", conv: str = "", limit: int = 200,
                       offset: int = 0, sort: str = "new", days: int = 0,
                       state: str = ""):
    con = db()
    try:
        return qq_local.files(con, q, kind, conv, limit, offset, sort, days, state)
    finally:
        con.close()


@app.post("/api/qq/local/file/parse")
def api_qq_local_file_parse(payload: dict):
    """提取单个 QQ 文件正文（预览时调用，带缓存）。"""
    try:
        fid = int(payload.get("id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "缺少文件 id")
    con = db()
    try:
        return qq_local.file_text(con, fid, bool(payload.get("force")))
    finally:
        con.close()


@app.post("/api/qq/local/file/reveal")
def api_qq_local_file_reveal(payload: dict):
    """在资源管理器中定位 QQ 文件。"""
    import subprocess
    con = db()
    try:
        row = con.execute("SELECT path FROM qq_file WHERE id=?",
                          (int(payload.get("id") or 0),)).fetchone()
    finally:
        con.close()
    if not row or not row[0] or not Path(row[0]).is_file():
        raise HTTPException(404, "文件不在本机（可能已被清理或未下载）")
    try:
        subprocess.Popen(["explorer.exe", f"/select,{row[0]}"])
    except Exception as e:      # noqa: BLE001
        raise HTTPException(500, f"打开所在位置失败：{e}")
    return {"ok": True}


_qq_file_parse_state = {"running": False, "done": 0, "total": 0, "progress": "",
                        "error": ""}


def _qq_file_parse_worker(limit: int):
    con = None
    try:
        _qq_file_parse_state["stage"] = "before_db"
        con = db()
        _qq_file_parse_state["stage"] = "after_db"
        ids = qq_local.files_to_parse(con, limit)
        _qq_file_parse_state["stage"] = "after_ids"
        _qq_file_parse_state["total"] = len(ids)
        for i, fid in enumerate(ids):
            _qq_file_parse_state["progress"] = f"解析 {i + 1}/{len(ids)}…"
            qq_local.file_text(con, fid)
            _qq_file_parse_state["done"] = i + 1
        _qq_file_parse_state["progress"] = "解析完成"
        _qq_file_parse_state["stage"] = "done"
    except Exception as exc:  # noqa: BLE001
        _qq_file_parse_state["stage"] = "error"
        _qq_file_parse_state["error"] = str(exc)[:300]
        _qq_file_parse_state["progress"] = "解析失败"
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        _qq_file_parse_state["running"] = False


@app.post("/api/qq/local/files/parse")
def api_qq_local_files_parse(payload: dict | None = None):
    if _qq_file_parse_state["running"]:
        raise HTTPException(409, "QQ 文件正文正在解析")
    req = payload or {}
    _qq_file_parse_state.update({"running": True, "done": 0, "total": 0,
                                 "progress": "准备中…", "error": "", "stage": ""})
    threading.Thread(target=_qq_file_parse_worker,
                     args=(max(1, min(int(req.get("limit") or 50), 300)),),
                     daemon=True).start()
    return {"ok": True, "started": True}


@app.get("/api/qq/local/files/parse/status")
def api_qq_local_files_parse_status():
    return dict(_qq_file_parse_state)


_qq_link_parse_state = {"running": False, "done": 0, "total": 0, "progress": "",
                        "error": "", "last": ""}


def _qq_link_parse_worker(limit: int):
    con = None
    try:
        con = db()
        items = qq_local.links_to_parse(con, limit)
        _qq_link_parse_state["total"] = len(items)
        for i, (_rid, url) in enumerate(items):
            _qq_link_parse_state["progress"] = f"解析 {i + 1}/{len(items)}…"
            _qq_link_parse_state["last"] = url[:80]
            try:
                res = fetch_article(url) or {}
                if res.get("error"):
                    # 记失败原因：列表里显示「解析失败」而不是永远「解析中」
                    qq_local.mark_link_state(con, _rid, "failed", res["error"])
                elif res.get("pending"):
                    pass      # 平台解析还在后台跑，下一轮再判定
                elif res.get("text") or res.get("cached"):
                    qq_local.mark_link_state(con, _rid, "ok", "")
                else:
                    qq_local.mark_link_state(con, _rid, "failed", "没有抓到正文")
            except Exception as exc:  # noqa: BLE001
                log.warning("qq link parse failed: %s %s", url[:60], exc)
                try:
                    qq_local.mark_link_state(con, _rid, "failed", str(exc)[:240])
                except Exception:
                    pass
        _qq_link_parse_state["progress"] = "解析完成"
    except Exception as exc:  # noqa: BLE001
        _qq_link_parse_state["error"] = str(exc)[:300]
        _qq_link_parse_state["progress"] = "解析失败"
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        _qq_link_parse_state["running"] = False


@app.post("/api/qq/local/links/parse")
def api_qq_local_links_parse(payload: dict | None = None):
    if _qq_link_parse_state["running"]:
        raise HTTPException(409, "QQ 链接正文正在解析")
    req = payload or {}
    _qq_link_parse_state.update({"running": True, "done": 0, "total": 0,
                                 "progress": "准备中…", "error": "", "last": ""})
    threading.Thread(target=_qq_link_parse_worker,
                     args=(max(1, min(int(req.get("limit") or 10), 50)),),
                     daemon=True).start()
    return {"ok": True, "started": True}


@app.get("/api/qq/local/links/parse/status")
def api_qq_local_links_parse_status():
    return dict(_qq_link_parse_state)


@app.get("/api/qq/local/links")
def api_qq_local_links(q: str = "", conv: str = "", limit: int = 100,
                       offset: int = 0, sort: str = "new", days: int = 0,
                       platform: str = "", state: str = ""):
    con = db()
    try:
        return qq_local.links(con, q, conv, limit, offset, sort, days,
                              platform, state)
    finally:
        con.close()


@app.post("/api/qq/local/reparse-cards")
def api_qq_local_reparse_cards():
    """把历史卡片消息按新规则重新解析（分享链接/小程序卡片 vs 聊天记录转发）。"""
    live_dir = DATA / "qq_live"
    if not (live_dir / "nt_msg.db").is_file():
        raise HTTPException(409, "QQ 实时副本尚未就绪，请稍后再试")
    con = db()
    try:
        res = qq_local.reparse_cards(con, live_dir)
    finally:
        con.close()
    return {"ok": True, **res}


@app.post("/api/qq/local/reparse-placeholders")
def api_qq_local_reparse_placeholders():
    """把历史「[消息]」占位重新解析（40800/8 的拍一拍现在能还原成「拍了拍 xxx」）。

    现解一份最新的 nt_msg.db（data/qq_live 那份可能落后好几天），用完即删。
    """
    import shutil
    try:
        acct = qq_local.pick_account()
        tmp = qq_local.decrypt_targets(acct, only=("nt_msg.db",))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, f"QQ 数据库解密失败：{exc}")
    con = db()
    try:
        res = qq_local.reparse_placeholders(con, tmp)
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": True, **res}


@app.get("/api/qq/local/self")
def api_qq_local_self():
    con = db()
    try:
        return qq_local.self_info(con)
    finally:
        con.close()


@app.get("/api/qq/local/overview")
def api_qq_local_overview():
    con = db()
    try:
        return qq_local.stats(con)
    finally:
        con.close()


_QQ_FETCHING: dict = {}


def _qq_fetch_remote(url: str, md5: str = "", kind: str = "image"):
    """按 md5 从 CDN 拉一张 QQ 图并落盘到本地缓存目录。

    QQ 只缓存用户看过的图片；这里用消息里的 md5 去下载，历史图片也能显示。
    返回落盘后的本地绝对路径（字符串），失败返回 None。
    """
    if not url:
        return None
    out_dir = KNOWLEDGE_DB.parent / "qq_fetched"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 目标文件名优先 md5，其次 url 末段
    if md5:
        name = md5.lower()
        for ext in (".jpg", ".png", ".gif", ".webp"):
            hit = next((f for f in out_dir.iterdir() if f.stem == name and f.suffix == ext), None)
            if hit:
                return str(hit)
    if not url:
        # 只有 md5：走 multimedia CDN 的通用拼接口径（不保证成功）
        url = f"https://multimedia.nt.qq.com.cn/download?ef=img&fileid={md5}"
    import requests as _rq
    try:
        r = _rq.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0",
                                              "Referer": "https://qun.qq.com/"})
        if r.status_code != 200 or len(r.content) < 500:
            return None
    except Exception:
        return None
    ext = ".jpg"
    ct = (r.headers.get("Content-Type") or "").lower()
    if "png" in ct:
        ext = ".png"
    elif "gif" in ct:
        ext = ".gif"
    elif "webp" in ct:
        ext = ".webp"
    fp = out_dir / (name + ext)
    fp.write_bytes(r.content)
    return str(fp)


@app.get("/api/qq/local/media/fetch")
def api_qq_local_media_fetch(url: str = "", md5: str = "", kind: str = "image"):
    """按需从 CDN 拉一张 QQ 图（QQ 未缓存的媒体）。

    消息里只有 md5 时走 multimedia CDN 通用拼接口径；有 url 时优先 url。
    拉到后落盘 data/qq_fetched/，并立刻返回可用路径。
    """
    fp = _qq_fetch_remote(url, md5, kind)
    if not fp:
        raise HTTPException(404, "无法从 CDN 拉取")
    return {"path": fp}


@app.get("/api/qq/local/media/thumb")
def api_qq_local_media_thumb(path: str = "", w: int = 200):
    """QQ 图片缩略图：优先给 <200px 的 Thumb（几百 KB），没有才回退原图。

    原图常有几 MB，直接塞进 <img src> 首屏要好几秒才出图；缩略图快得多，
    对聊天气泡的展示尺寸完全够用。
    """
    p = qq_local.media_path(path)
    if not p:
        raise HTTPException(404, "no media")
    # 优先用 QQ 自带的 Thumb（已是 720px 缓存，省掉一次 PIL 编码）
    stem = p.stem.lower()
    for suffix in ("_720", "_0"):
        if stem.endswith(suffix) and len(stem) - len(suffix) == 32:
            base = p.parent.parent / stem[:-len(suffix)]
            cand = [base.with_suffix(".jpg"), base.with_suffix(".png")]
            for c in cand:
                if c.is_file():
                    p = c
                    break
            break
    # 只有 QQ 自带的 Thumb（_720/_0）才用它；没有说明没有缩略图缓存，
    # 用 Ori 原图走 PIL 缩放，否则大图会被整张拉下来再缩
    is_thumb = stem.endswith(("_720", "_0"))
    try:
        w = max(60, min(int(w or 200), 640))
    except (TypeError, ValueError):
        w = 200
    # 缩略图磁盘缓存：同一张图 + 同一宽度只编码一次（首次 ~0.8s，之后几 ms）
    try:
        st = p.stat()
        ck = hashlib.sha1(f"{p}|{st.st_mtime_ns}|{st.st_size}|{w}".encode()).hexdigest()[:24]
        cache_dir = KNOWLEDGE_DB.parent / "qq_thumbs"
        cache_fp = cache_dir / (ck + ".jpg")
        if cache_fp.is_file():
            return FileResponse(str(cache_fp), media_type="image/jpeg",
                                headers={"Cache-Control": "private, max-age=86400"})
    except Exception:
        cache_fp = None
    try:
        from PIL import Image as _PILImage
        import io as _io
        with _PILImage.open(str(p)) as im:
            if im.width <= w and is_thumb:
                return FileResponse(str(p), media_type="image/png",
                                    headers={"Cache-Control": "private, max-age=86400"})
            ratio = w / im.width
            im = im.convert("RGB").resize((w, max(1, int(im.height * ratio))))
            buf = _io.BytesIO()
            im.save(buf, format="JPEG", quality=72, optimize=True)
            data = buf.getvalue()
        if len(data) >= os.path.getsize(str(p)):
            # 编码后不比原图小（罕见）：退回原文件
            return FileResponse(str(p), media_type="image/png",
                                headers={"Cache-Control": "private, max-age=86400"})
        if cache_fp is not None:
            try:
                cache_fp.parent.mkdir(parents=True, exist_ok=True)
                cache_fp.write_bytes(data)
            except Exception:
                pass
        return Response(data, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})
    except Exception:
        return FileResponse(str(p), media_type="image/png",
                            headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/qq/local/media")
def api_qq_local_media(path: str = ""):
    p = qq_local.media_path(path)
    if not p:
        raise HTTPException(404, "文件不存在或不在允许目录")
    return FileResponse(p)


# ---------------------------------------------------------------- 静态前端
if FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="ui")
