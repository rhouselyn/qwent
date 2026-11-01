# -*- coding: utf-8 -*-
"""朋友圈(SNS)数据：解析、永久留存、检索与媒体定位。

数据源是 ChatTrace 解密产物 DECRYPTED_DIR/sns/sns.db：
  - SnsTimeLine(tid, user_name, content)：content 是 <SnsDataItem><TimelineObject> XML
  - SnsMessage_tmp3(feed_id, type, from_*, to_*, content, create_time)：评论与点赞，
    feed_id 对应 SnsTimeLine.tid（注意：不是 XML 里的 <id>，两套 id 体系）

设计原则：**只增不删**。朋友圈一旦同步进知识库就永久留存，作者之后删除、
改成"三天可见"或服务器侧不再下发，都不影响本地已有数据；同步只做
INSERT OR IGNORE / UPDATE，永不 DELETE。

媒体：本地缓存在 xwechat_files/<account>/cache/<YYYY-MM>/Sns/Img/<key[:2]>/<key[2:]>，
key = md5(f"{xml_id}_{media_id}_{media_type}")，视频把 media_type 换成 3。
缓存图与聊天图片同为 V2 dat 加密，用现成 decode_dat + ImageKeys 解密；
无本地缓存的图/视频按需拉 CDN（token 鉴权 + WxIsaac64 流解密，解密后落
data/media_cache/sns/），实现用 WeFlow 的 WASM（refs/WeChatDataAnalysis/，无
LICENSE 不能入库，由常驻 node 守护进程生成 keystream）。
分享链接（公众号/音乐/B站等）从 ContentObject.contentUrl 与非 CDN 的 media url
提取到 links_json，详情页可跳转复用已有链接解析管线，AI 可 read_link。
"""
from __future__ import annotations

import glob
import hashlib
import html
import json
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

_ocr = None
_img_roots_cache: list[Path] | None = None
_video_roots_cache: list[Path] | None = None
_path_cache: dict[str, str | None] = {}

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sns_post(
        tid TEXT PRIMARY KEY,
        xml_id TEXT,
        author TEXT,
        author_name TEXT,
        ts INTEGER,
        content_desc TEXT,
        location TEXT,
        media_json TEXT,
        raw_xml TEXT,
        imported_at INTEGER)""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS sns_fts USING fts5(
        content_desc, author_name UNINDEXED, tokenize='trigram')""",
    """CREATE TABLE IF NOT EXISTS sns_comment(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        feed_tid TEXT,
        ctype INTEGER,
        from_un TEXT,
        from_nick TEXT,
        to_un TEXT,
        to_nick TEXT,
        ts INTEGER,
        content TEXT)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_sns_comment_dedupe
        ON sns_comment(feed_tid, from_un, ts, substr(content,1,64))""",
    """CREATE TABLE IF NOT EXISTS sns_media_ocr(
        tid TEXT, media_idx INTEGER, kind TEXT, text TEXT, ts INTEGER,
        PRIMARY KEY(tid, media_idx, kind))""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS sns_media_fts USING fts5(
        text, tid UNINDEXED, tokenize='trigram')""",
    """ALTER TABLE sns_post ADD COLUMN links_json TEXT""",
]

VEC_SCHEMA = ("CREATE VIRTUAL TABLE IF NOT EXISTS sns_vec USING vec0("
              "post_rowid INTEGER PRIMARY KEY, embedding float[512])")


def ensure_schema(con) -> None:
    for ddl in SCHEMA:
        try:
            con.execute(ddl)      # ALTER 重复列会报错，属预期
        except Exception:
            pass
    con.commit()


# ---------------------------------------------------------------- XML 解析

_LINK_HOST_BLOCK = ("qpic.cn", "qlogo.cn", "video.qq.com", "tc.qq.com", "wechat.com",
                    "support.weixin.qq.com", "dldir1v6.qq.com")


def _is_share_link(url: str) -> bool:
    """分享链接 = 非微信媒体/资源域名的 http(s) 链接。"""
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")) or "pages/image_detail" in u:
        return False
    try:
        host = u.split("/", 3)[2].lower().removeprefix("www.")
    except IndexError:
        return False
    return not any(host == d or host.endswith("." + d) or host.endswith(d)
                   for d in _LINK_HOST_BLOCK)


def _parse_post(tid, user_name: str, content_xml: str) -> dict | None:
    try:
        root = ET.fromstring(content_xml or "")
    except ET.ParseError:
        return None
    to = root.find("TimelineObject")
    if to is None:
        return None
    media = []
    links: list[dict] = []
    seen_links: set[str] = set()

    def _add_link(url: str, title: str) -> None:
        import html as _html
        u = _html.unescape((url or "").strip()).replace("&amp;", "&")
        if not u or u in seen_links or not _is_share_link(u):
            return
        seen_links.add(u)
        links.append({"url": u, "title": _html.unescape(title or "").strip()[:120]})

    for m in to.iter("media"):
        u = m.find("url")
        size = m.find("size")
        entry = {
            "i": len(media),
            "t": m.findtext("type") or "",
            "mid": m.findtext("id") or "",
            "md5": (u.get("md5") if u is not None else "") or "",
            "thumb": (m.findtext("thumb") or "").strip(),
            "url": (u.text or "").strip() if u is not None else "",
            "key": (u.get("key") if u is not None else "") or "",
            "token": (u.get("token") if u is not None else "") or "",
            "enc": m.findtext("enc") or "0",
            "w": int(size.get("width") or 0) if size is not None else 0,
            "h": int(size.get("height") or 0) if size is not None else 0,
            "size": int(size.get("totalSize") or 0) if size is not None else 0,
            "dur": m.findtext("videoDuration") or 0,
            "desc": (m.findtext("description") or "").strip(),
        }
        if not _media_is_real(entry):
            continue          # 视频号直播等空占位
        media.append(entry)
        if entry["url"]:
            _add_link(entry["url"], entry["desc"])
    co = to.find("ContentObject")
    if co is not None:
        _add_link(co.findtext("contentUrl") or "", co.findtext("title") or "")
    loc = to.find("location")
    poiname = ""
    if loc is not None:
        poiname = loc.get("poiname") or loc.get("title") or ""
    extra = root.find("LocalExtraInfo")
    nickname = (extra.findtext("nickname") if extra is not None else "") or ""
    try:
        ts = int(to.findtext("createTime") or 0)
    except ValueError:
        ts = 0
    for m in media:
        try:
            m["dur"] = int(float(m.get("dur") or 0))
        except (TypeError, ValueError):
            m["dur"] = 0
    return {
        "tid": str(tid),
        "xml_id": to.findtext("id") or "",
        "author": to.findtext("username") or user_name or "",
        "author_name": nickname,
        "ts": ts,
        "content_desc": (to.findtext("contentDesc") or "").strip(),
        "location": poiname,
        "media": media,
        "links": links,
        "raw_xml": content_xml or "",
    }


def _media_is_video(m: dict) -> bool:
    return str(m.get("t")) == "6" or int(m.get("dur") or 0) > 0


def _media_is_real(m: dict) -> bool:
    """过滤空占位 media（如视频号直播 type34 的空 mediaList 元素）。"""
    return bool(m.get("url") or m.get("mid"))


# ---------------------------------------------------------------- 同步(只增不删)

def sync_sns(con) -> dict:
    """解析解密后的 sns.db 并合入知识库。返回 {posts, new_posts, comments}。"""
    import main as _m
    ensure_schema(con)
    src = Path(_m.DECRYPTED_DIR) / "sns" / "sns.db"
    if not src.exists():
        return {"ok": False, "detail": f"sns.db 不存在: {src}"}
    src_con = _open_readonly(src)
    now = int(time.time())
    stats = {"ok": True, "posts": 0, "new_posts": 0, "upd_posts": 0, "comments": 0,
             "parse_errors": 0}
    for tid, user_name, content in src_con.execute(
            "SELECT tid, user_name, content FROM SnsTimeLine"):
        stats["posts"] += 1
        p = _parse_post(tid, user_name, content)
        if p is None:
            stats["parse_errors"] += 1
            continue
        row = con.execute("SELECT rowid, content_desc FROM sns_post WHERE tid=?",
                          (p["tid"],)).fetchone()
        media_json = json.dumps(p["media"], ensure_ascii=False)
        links_json = json.dumps(p["links"], ensure_ascii=False)
        if row is None:
            cur = con.execute(
                "INSERT INTO sns_post(tid, xml_id, author, author_name, ts, content_desc,"
                " location, media_json, raw_xml, imported_at, links_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (p["tid"], p["xml_id"], p["author"], p["author_name"], p["ts"],
                 p["content_desc"], p["location"], media_json, p["raw_xml"], now,
                 links_json))
            con.execute("INSERT INTO sns_fts(rowid, content_desc, author_name) VALUES(?,?,?)",
                        (cur.lastrowid, p["content_desc"], p["author_name"]))
            stats["new_posts"] += 1
        elif row[1] != p["content_desc"]:
            con.execute("UPDATE sns_post SET content_desc=?, media_json=?, author_name=?,"
                        " links_json=? WHERE tid=?",
                        (p["content_desc"], media_json, p["author_name"], links_json,
                         p["tid"]))
            con.execute("DELETE FROM sns_fts WHERE rowid=?", (row[0],))
            con.execute("INSERT INTO sns_fts(rowid, content_desc, author_name) VALUES(?,?,?)",
                        (row[0], p["content_desc"], p["author_name"]))
            stats["upd_posts"] += 1
        else:
            con.execute("UPDATE sns_post SET media_json=?, links_json=? WHERE tid=?",
                        (media_json, links_json, p["tid"]))
    for fid, ctype, fun, fnick, tun, tnick, ts, text in src_con.execute(
            "SELECT feed_id, type, from_username, from_nickname, to_username,"
            " to_nickname, create_time, content FROM SnsMessage_tmp3"):
        cur = con.execute(
            "INSERT OR IGNORE INTO sns_comment(feed_tid, ctype, from_un, from_nick,"
            " to_un, to_nick, ts, content) VALUES(?,?,?,?,?,?,?,?)",
            (str(fid), ctype, fun or "", fnick or "", tun or "", tnick or "",
             int(ts or 0), text or ""))
        stats["comments"] += cur.rowcount
    con.commit()
    src_con.close()
    con.execute("INSERT INTO meta(key, value) VALUES('sns_sync_at', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
    con.commit()
    return stats


def _open_readonly(path: Path):
    con = __import__("sqlite3").connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = None
    return con


# ---------------------------------------------------------------- 媒体定位与解码

def _sns_media_roots(kind: str) -> list[Path]:
    """cache/<YYYY-MM>/Sns/Img(或 Video) 的月份根目录列表，带缓存。"""
    global _img_roots_cache, _video_roots_cache
    if kind == "img" and _img_roots_cache is not None:
        return _img_roots_cache
    if kind == "video" and _video_roots_cache is not None:
        return _video_roots_cache
    import main as _m
    roots: list[Path] = []
    cache_root = Path(_m.ACCOUNT_DIR) / "cache"
    if cache_root.is_dir():
        for mdir in sorted(cache_root.iterdir()):
            p = mdir / "Sns" / ("Img" if kind == "img" else "Video")
            if p.is_dir():
                roots.append(p)
    if kind == "img":
        _img_roots_cache = roots
    else:
        _video_roots_cache = roots
    return roots


def resolve_media_path(entry: dict, xml_id: str) -> str | None:
    """按微信缓存命名规则定位朋友圈图片/视频本地文件。

    key = md5(f"{xml_id}_{media_id}_{media_type}")，视频的 media_type 固定为 3。
    """
    mid = entry.get("mid") or ""
    if not xml_id or not mid:
        return None
    is_video = _media_is_video(entry)
    if is_video:
        keys = [hashlib.md5(f"{xml_id}_{mid}_3".encode()).hexdigest()]
        roots = _sns_media_roots("video")
        exts = ("", ".mp4", ".tmp")
    else:
        keys = [hashlib.md5(f"{xml_id}_{mid}_{entry.get('t') or 2}".encode()).hexdigest()]
        if entry.get("md5"):
            keys.append(str(entry["md5"]).lower())
        roots = _sns_media_roots("img")
        exts = ("",)
    for key in keys:
        ck = f"{key[:2]}/{key[2:]}"
        hit = _path_cache.get(ck, "?")
        if hit != "?":
            return hit or None
        for root in roots:
            for ext in exts:
                p = root / key[:2] / (key[2:] + ext)
                if p.is_file():
                    _path_cache[ck] = str(p)
                    return str(p)
        _path_cache[ck] = ""
    return None


def image_bytes(entry: dict, xml_id: str) -> tuple[bytes, str] | None:
    """朋友圈图片字节：本地缓存 dat 解密优先，否则拉 CDN（ISAAC64 解密）。

    返回 (bytes, mime)；两侧都不可得返回 None。
    """
    path = resolve_media_path(entry, xml_id)
    if path:
        import main as _m
        try:
            from chattrace.service.media import decode_dat
        except ImportError:
            return None
        try:
            raw = Path(path).read_bytes()
            keys = _m.get_media().image_keys()
            fmt, blob = decode_dat(raw, keys)
            return blob, ("image/png" if fmt == "png" else "image/jpeg")
        except Exception:
            pass
    return cdn_media(entry)


# ---------------------------------------------------------------- CDN 拉取 + ISAAC64 解密
#
# 微信朋友圈 CDN（shmmsns/szmmsns.qpic.cn、*.video.qq.com）的图与视频下载时
# 需带 token（拼 ?token=..&idx=1、UA=MircroMessenger Client），且 x-enc=1 的
# 载荷用 WxIsaac64 流加密：keystream = reverse(WASM generate(对齐到8字节的大小))。
# 权威实现来自 WeFlow 的 wasm（无 LICENSE，不能入库），放 refs/WeChatDataAnalysis/
# 由常驻 node 守护进程按需生成。

_wasm_dir = Path(__file__).resolve().parents[2] / "refs" / "WeChatDataAnalysis"
_isaac_proc = None
_isaac_lock = threading.Lock()
_isaac_dead = False
_cdn_fail: set[str] = set()


def _isaac_keystream(key: str, size: int) -> bytes | None:
    """向 node 守护进程请求 WxIsaac64 原始 keystream（调用方负责反转）。"""
    global _isaac_proc, _isaac_dead
    if _isaac_dead or not key:
        return None
    with _isaac_lock:
        try:
            if _isaac_proc is None or _isaac_proc.poll() is not None:
                _isaac_proc = subprocess.Popen(
                    ["node", str(_wasm_dir / "ks_daemon.js")],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)
            req = f"{key} {size}\n".encode()
            _isaac_proc.stdin.write(req)
            _isaac_proc.stdin.flush()
            header = b""
            while not header.endswith(b"\n"):
                ch = _isaac_proc.stdout.read(1)
                if not ch:
                    raise RuntimeError("daemon closed")
                header += ch
            parts = header.decode().strip().split()
            if parts[0] != "OK":
                raise RuntimeError(header.decode().strip()[:100])
            n = int(parts[1])
            buf = b""
            while len(buf) < n:
                chunk = _isaac_proc.stdout.read(n - len(buf))
                if not chunk:
                    raise RuntimeError("daemon short read")
                buf += chunk
            return buf
        except Exception:
            # 守护进程状态不可信，重启一次
            try:
                if _isaac_proc:
                    _isaac_proc.kill()
            except Exception:
                pass
            _isaac_proc = None
            if os.environ.get("SNS_NO_ISAAC"):
                _isaac_dead = True
            return None


def _fix_cdn_url(url: str, token: str) -> str:
    u = html.unescape((url or "").strip())
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    if token:
        u += (("&" if "?" in u else "?") + f"token={token}&idx=1")
    return u


def _sniff_mime(data: bytes, is_video: bool) -> str | None:
    if is_video:
        return "video/mp4" if data[4:8] == b"ftyp" else None
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def cdn_media(entry: dict, is_video: bool | None = None) -> tuple[bytes, str] | None:
    """按需拉取并解密一条朋友圈 CDN 媒体，落盘缓存后返回 (bytes, mime)。"""
    import urllib.request
    import ssl
    url = (entry.get("url") or "").strip()
    if not url or url in _cdn_fail:
        return None
    is_video = _media_is_video(entry) if is_video is None else is_video
    if _is_share_link(url):      # 分享外链不是媒体
        return None
    import main as _m
    cache_dir = Path(_m.DATA) / "media_cache" / "sns"
    fixed = _fix_cdn_url(url, entry.get("token") or "")
    cache_key = hashlib.md5(fixed.encode()).hexdigest()
    cache_path = cache_dir / (cache_key + (".mp4" if is_video else ".img"))
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        data = cache_path.read_bytes()
        mime = _sniff_mime(data, is_video)
        if mime:
            return data, mime
    try:
        ctx = ssl.create_default_context()
        try:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE      # 部分 CDN 主机证书域名不匹配
        except Exception:
            pass
        req = urllib.request.Request(fixed, headers={
            "User-Agent": "MicroMessenger Client", "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            raw = r.read()
            # x-enc 响应头是加密与否的权威标志（XML enc 属性常为 0 但载荷仍加密）
            x_enc_hdr = (r.headers.get("x-enc") or "").strip()
    except Exception:
        _cdn_fail.add(url)
        return None
    if len(raw) < 16:
        _cdn_fail.add(url)
        return None
    need_decrypt = x_enc_hdr == "1" or entry.get("enc") == "1" or is_video
    if need_decrypt:
        n = min(len(raw), 131072) if is_video else len(raw)
        aligned = (n + 7) // 8 * 8
        ks = _isaac_keystream(entry.get("key") or "", aligned)
        if not ks:
            _cdn_fail.add(url)
            return None
        ks = ks[::-1]
        buf = bytearray(raw)
        for i in range(min(n, len(ks))):
            buf[i] ^= ks[i]
        raw = bytes(buf)
    mime = _sniff_mime(raw, is_video)
    if not mime:
        _cdn_fail.add(url)
        return None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(raw)
    except OSError:
        pass
    return raw, mime


def warm_isaac() -> None:
    """预热 ISAAC64 node 守护进程（加载 wasm），失败静默。"""
    try:
        _isaac_keystream("0", 8)
    except Exception:
        pass


def video_bytes(entry: dict) -> tuple[bytes, str] | None:
    """朋友圈/视频号视频：仅 CDN 一路（本地无缓存），解密前 128KB 后返回。

    视频号视频的 key 属性通常是 "0"，其流加密种子无法从 key 还原（实测），
    这类直接返回 None，前端显示占位。
    """
    if not _media_is_video(entry):
        return None
    key = str(entry.get("key") or "")
    if not key or key == "0":
        return None
    return cdn_media(entry, is_video=True)


def media_display(con, post_row) -> list[dict]:
    """给前端/工具用的媒体列表：附本地缓存可用性与 OCR 文本。"""
    try:
        media = json.loads(post_row["media_json"] or "[]")
    except (TypeError, ValueError):
        media = []
    ocr_rows = {r[0]: r[1] for r in con.execute(
        "SELECT media_idx, text FROM sns_media_ocr WHERE tid=?", (post_row["tid"],))}
    out = []
    for m in media:
        if not _media_is_real(m):
            continue
        local = resolve_media_path(m, post_row["xml_id"] or "") is not None
        video = _media_is_video(m)
        key = str(m.get("key") or "")
        out.append({
            "i": m.get("i", 0), "video": video, "w": m.get("w", 0),
            "h": m.get("h", 0), "dur": m.get("dur", 0), "desc": m.get("desc", ""),
            "local": local, "ocr": ocr_rows.get(m.get("i", 0), ""),
            # 图片：本地缓存或 CDN 皆可；视频：实测 key="0"（视频号）的流加密无法用
            # key 属性解开，只有带真实 key 的才尝试
            "playable": ((not video) and (local or bool(m.get("url")))
                         or (video and bool(key) and key != "0")),
        })
    return out


# ---------------------------------------------------------------- OCR

def _get_ocr():
    global _ocr
    if _ocr is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ocr = RapidOCR()
        except Exception:
            _ocr = False
    return _ocr or None


def process_sns_ocr(con, cap: int = 8) -> int:
    """给有本地缓存或可 CDN 拉取的朋友圈图片补 OCR（每轮限量，供后台循环调用）。

    空文本占位行 6 小时后允许重试（早期失败可能是暂时的：组件未就绪/网络抖动）。
    行的 ts 记录的是**尝试时间**而非发帖时间。
    """
    ocr = _get_ocr()
    if ocr is None:
        return 0
    ensure_schema(con)
    done = 0
    now = time.time()
    rows = con.execute(
        "SELECT tid, xml_id, media_json, ts FROM sns_post"
        " WHERE media_json LIKE '%\"t\": \"2\"%' OR media_json LIKE '%\"t\":\"2\"%'"
        " ORDER BY ts DESC LIMIT 400").fetchall()
    for tid, xml_id, media_json, ts in rows:
        if done >= cap:
            break
        try:
            media = json.loads(media_json or "[]")
        except (TypeError, ValueError):
            continue
        have = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
            "SELECT media_idx, text, kind, ts FROM sns_media_ocr WHERE tid=?", (tid,))}
        for m in media:
            if done >= cap:
                break
            if _media_is_video(m):
                continue
            prev = have.get(m.get("i", 0))
            if prev:
                kind0, ts0 = prev[1], prev[2]
                if kind0 == "ocr0" or (prev[0] or "").strip():
                    continue                    # 已处理:有文字 / 确认无文字(风景图)
                if now - (ts0 or 0) < 6 * 3600:
                    continue                    # 拉取失败 6h 内,暂不重试
                con.execute("DELETE FROM sns_media_ocr WHERE tid=? AND media_idx=?",
                            (tid, m.get("i", 0)))
            done += 1          # 无论成败都占额度,避免坏图反复重试
            img = image_bytes(m, xml_id or "")
            text = ""
            if img:
                try:
                    res, _ = ocr(img[0])
                    text = "\n".join(x[1] for x in res if x and len(x) > 1)[:4000]
                except Exception:
                    text = ""
            # kind 语义: 'ocr'=有文字; 'ocr0'=已抓到图但无文字(风景图,终态不重试);
            # 'ocr'+空文本=拉取失败占位(6h 后可重试)
            kind = "ocr" if text.strip() else ("ocr0" if img else "ocr")
            con.execute("INSERT OR REPLACE INTO sns_media_ocr VALUES(?,?,?,?,?)",
                        (tid, m.get("i", 0), kind, text, int(now)))
            if text.strip():
                con.execute("DELETE FROM sns_media_fts WHERE tid=?", (tid,))
                con.execute("INSERT INTO sns_media_fts(text, tid) VALUES(?,?)",
                            (text, tid))
    con.commit()
    return done


# ---------------------------------------------------------------- 向量索引

def build_sns_vec(batch: int = 128) -> int:
    """给没有向量的朋友圈补 embedding（正文为空的朋友圈跳过）。

    注意 vec0 虚表必须走 vector.connect()（加载了 sqlite-vec 扩展的连接）。
    """
    try:
        import main as _m
        from vector import available, connect, embed
    except Exception:
        return 0
    if not available():
        return 0
    vcon = connect(str(_m.DATA / "knowledge.db"))
    total = 0
    try:
        vcon.execute(VEC_SCHEMA)
        while total < 2000:
            rows = vcon.execute(
                """SELECT p.rowid, p.content_desc,
                          (SELECT group_concat(o.text, char(10)) FROM sns_media_ocr o
                            WHERE o.tid = p.tid AND o.text != '') AS ocr
                   FROM sns_post p
                   WHERE p.rowid NOT IN (SELECT post_rowid FROM sns_vec)
                     AND (p.content_desc != '' OR EXISTS(
                          SELECT 1 FROM sns_media_ocr o2
                          WHERE o2.tid = p.tid AND o2.text != ''))
                   LIMIT ?""", (batch,)).fetchall()
            if not rows:
                break
            texts = [((r[1] or "").strip() + "\n" + (r[2] or "").strip()).strip()
                     for r in rows]
            vecs = embed(texts)
            vcon.execute("BEGIN")
            vcon.executemany(
                "INSERT OR REPLACE INTO sns_vec(post_rowid, embedding) VALUES(?,?)",
                [(r[0], vecs[i].tobytes()) for i, r in enumerate(rows)])
            vcon.commit()
            total += len(rows)
    finally:
        vcon.close()
    return total


def rebuild_sns_vec(batch: int = 128) -> int:
    """清空朋友圈向量后按最新数据(含图片 OCR 文字)重建。"""
    import main as _m
    from vector import connect
    vcon = connect(str(_m.DATA / "knowledge.db"))
    try:
        vcon.execute(VEC_SCHEMA)
        vcon.execute("DELETE FROM sns_vec")
        vcon.commit()
    finally:
        vcon.close()
    return build_sns_vec(batch)


# ---------------------------------------------------------------- 查询

def _post_brief(con, r) -> dict:
    try:
        media = json.loads(r["media_json"] or "[]")
    except (TypeError, ValueError):
        media = []
    try:
        links = json.loads(r["links_json"] if "links_json" in r.keys() else "[]")
    except (TypeError, ValueError):
        links = []
    n_img = sum(1 for m in media if not _media_is_video(m) and _media_is_real(m))
    n_vid = sum(1 for m in media if _media_is_video(m) and _media_is_real(m))
    n_local = sum(1 for m in media
                  if not _media_is_video(m) and _media_is_real(m)
                  and (resolve_media_path(m, r["xml_id"] or "") is not None
                       or bool(m.get("url"))))
    n_comments = con.execute("SELECT COUNT(*) FROM sns_comment WHERE feed_tid=?",
                             (r["tid"],)).fetchone()[0]
    first_img = next((m["i"] for m in media
                      if not _media_is_video(m) and _media_is_real(m)
                      and (m.get("url") or m.get("mid"))), None)
    return {"tid": r["tid"], "author": r["author"], "author_name": r["author_name"],
            "ts": r["ts"], "text": r["content_desc"] or "", "location": r["location"] or "",
            "n_img": n_img, "n_vid": n_vid, "n_local": n_local,
            "n_links": len(links), "n_comments": n_comments, "first_img": first_img}


def list_posts(con, offset: int = 0, limit: int = 50, q: str = "",
               author: str = "") -> dict:
    ensure_schema(con)
    if q.strip():
        rows = _fts_posts(con, q, limit, offset)
        return {"total": len(rows), "items": [_post_brief(con, r) for r in rows]}
    cond, params = "", []
    if author:
        cond = "WHERE author LIKE ? OR author_name LIKE ?"
        params = [f"%{author}%", f"%{author}%"]
    rows = con.execute(f"SELECT * FROM sns_post {cond} ORDER BY ts DESC"
                       " LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    total = con.execute(f"SELECT COUNT(*) FROM sns_post {cond}", params).fetchone()[0]
    return {"total": total, "items": [_post_brief(con, r) for r in rows]}


def _fts_terms(q: str) -> tuple[list[str], list[str]]:
    """长词(>=3字)走 FTS trigram(长词再切 3-gram)，短词留给 LIKE 兜底。"""
    words = [w for w in q.replace('"', " ").split() if w] or [q]
    terms: list[str] = []
    short: list[str] = []
    for w in words:
        if len(w) >= 3:
            terms.append(w)
            if len(w) > 3:
                terms.extend(w[i:i + 3] for i in range(min(len(w) - 2, 12)))
        elif w:
            short.append(w)
    return terms, short


def _fts_posts(con, q: str, limit: int, offset: int = 0):
    """按关键词查朋友圈：sns_fts + sns_media_fts(OCR 文字)，短词 LIKE 兜底。"""
    terms, short = _fts_terms(q)
    out: dict[str, any] = {}
    if terms:
        match = " OR ".join(f'"{t}"' for t in terms)
        for tab in ("sns_fts", "sns_media_fts"):
            try:
                if tab == "sns_fts":
                    rows = con.execute(
                        "SELECT p.* FROM sns_fts f JOIN sns_post p ON p.rowid=f.rowid"
                        " WHERE sns_fts MATCH ? ORDER BY bm25(sns_fts) LIMIT ?",
                        (match, limit)).fetchall()
                else:
                    rows = con.execute(
                        "SELECT p.* FROM sns_media_fts f JOIN sns_post p"
                        " ON p.tid=f.tid WHERE sns_media_fts MATCH ? LIMIT ?",
                        (match, limit)).fetchall()
                for r in rows:
                    out.setdefault(r["tid"], r)
            except Exception:
                continue
    if short:
        lconds = " AND ".join("content_desc LIKE ?" for _ in short)
        rows = con.execute(
            f"SELECT * FROM sns_post WHERE {lconds} ORDER BY ts DESC LIMIT ?",
            [f"%{w}%" for w in short] + [limit]).fetchall()
        for r in rows:
            out.setdefault(r["tid"], r)
    ordered = sorted(out.values(), key=lambda r: r["ts"], reverse=True)
    return ordered[offset:offset + limit]


def get_moment(con, tid: str) -> dict | None:
    ensure_schema(con)
    r = con.execute("SELECT * FROM sns_post WHERE tid=?", (str(tid),)).fetchone()
    if r is None:
        return None
    comments = [{"nick": c[0], "ts": c[1], "text": c[2], "ctype": c[3]}
                for c in con.execute(
                    "SELECT COALESCE(from_nick, from_un), ts, content, ctype"
                    " FROM sns_comment WHERE feed_tid=? ORDER BY ts", (r["tid"],))]
    likes = con.execute("SELECT COUNT(*) FROM sns_comment WHERE feed_tid=? AND ctype=1",
                        (r["tid"],)).fetchone()[0]
    try:
        links = json.loads(r["links_json"] or "[]")
    except (TypeError, ValueError):
        links = []
    return {**_post_brief(con, r), "xml_id": r["xml_id"], "media": media_display(con, r),
            "links": links, "comments": comments, "n_likes": likes,
            "raw_text": r["content_desc"] or ""}


def search_moments(con, query: str, author: str = "", days: int = 0,
                   limit: int = 20) -> list[dict]:
    """混合检索：FTS(trigram) 为主，语义向量兜底。返回带 tid 的条目。"""
    ensure_schema(con)
    conds, params = [], []
    if author:
        conds.append("(p.author LIKE ? OR p.author_name LIKE ?)")
        params += [f"%{author}%", f"%{author}%"]
    if days > 0:
        conds.append("p.ts >= ?")
        params.append(int(time.time()) - days * 86400)
    where = (" AND " + " AND ".join(conds)) if conds else ""
    out: dict[str, dict] = {}
    if query.strip():
        terms, short = _fts_terms(query)
        if terms:
            match = " OR ".join(f'"{t}"' for t in terms)
            # 正文(sns_fts) + 图片 OCR 文字(sns_media_fts) 双路
            for tab, join in (("sns_fts", "p.rowid=f.rowid"),
                              ("sns_media_fts", "p.tid=f.tid")):
                try:
                    fts_rows = con.execute(
                        f"SELECT p.* FROM {tab} f JOIN sns_post p ON {join}"
                        f" WHERE {tab} MATCH ?" + where +
                        " LIMIT ?", (match, *params, limit)).fetchall()
                except Exception:
                    fts_rows = []
                for r in fts_rows:
                    if r["tid"] in out:
                        continue
                    d = _post_brief(con, r)
                    d["via"] = "ocr" if tab == "sns_media_fts" else "fts"
                    out[r["tid"]] = d
        if short:
            lconds = " AND ".join("p.content_desc LIKE ?" for _ in short)
            rows = con.execute(
                "SELECT p.* FROM sns_post p WHERE " + lconds + where.replace("p.", "p.") +
                " ORDER BY p.ts DESC LIMIT ?",
                [f"%{w}%" for w in short] + params + [limit]).fetchall()
            for r in rows:
                d = _post_brief(con, r)
                d["via"] = "like"
                out.setdefault(r["tid"], d)
    if len(out) < limit:
        sem = moments_semantic(con, query, limit=limit, author=author, days=days) \
            if query.strip() else []
        for d in sem:
            if d["tid"] not in out:
                d["via"] = "vec"
                out[d["tid"]] = d
            if len(out) >= limit:
                break
    if not query.strip():
        rows = con.execute("SELECT * FROM sns_post p" + where +
                           " ORDER BY ts DESC LIMIT ?", (*params, limit)).fetchall()
        out = {}
        for r in rows:
            out[r["tid"]] = _post_brief(con, r)
    items = sorted(out.values(), key=lambda x: x["ts"], reverse=True)[:limit]
    for it in items:
        it["who"] = it["author_name"] or it["author"]
        it["kind"] = "moment"
    return items


def moments_semantic(con, query: str, limit: int = 10, author: str = "",
                     days: int = 0) -> list[dict]:
    if not query.strip():
        return []
    try:
        import main as _m
        from vector import available, connect, embed
    except Exception:
        return []
    if not available():
        return []
    try:
        qv = embed([query])[0].tobytes()
        vcon = connect(str(_m.DATA / "knowledge.db"))
        try:
            rows = vcon.execute(
                "SELECT post_rowid, distance FROM sns_vec"
                " WHERE embedding MATCH ? AND k = ?", (qv, max(limit * 3, 30))).fetchall()
        finally:
            vcon.close()
    except Exception:
        return []
    cands = sorted(((r[0], 1.0 - (float(r[1]) ** 2) / 2.0) for r in rows),
                   key=lambda x: -x[1])
    out = []
    for rowid, sim in cands:
        r = con.execute("SELECT * FROM sns_post WHERE rowid=?", (rowid,)).fetchone()
        if r is None:
            continue
        if author and author not in (r["author"] or "") and \
                author not in (r["author_name"] or ""):
            continue
        if days > 0 and r["ts"] < int(time.time()) - days * 86400:
            continue
        d = _post_brief(con, r)
        d["score"] = round(sim, 4)
        out.append(d)
        if len(out) >= limit:
            break
    return out


def stats(con) -> dict:
    ensure_schema(con)
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    try:
        vec_n = q("SELECT COUNT(*) FROM sns_vec")
    except Exception:      # 普通连接没加载 vec 扩展时换 vconn 数
        try:
            import main as _m
            vec_n = _m.vconn().execute("SELECT COUNT(*) FROM sns_vec").fetchone()[0]
        except Exception:
            vec_n = 0
    last = ""
    try:
        row = con.execute("SELECT value FROM meta WHERE key='sns_sync_at'").fetchone()
        last = row[0] if row else ""
    except Exception:
        pass
    return {
        "posts": q("SELECT COUNT(*) FROM sns_post"),
        "comments": q("SELECT COUNT(*) FROM sns_comment"),
        "media": q("SELECT COUNT(*) FROM sns_post WHERE media_json LIKE '%\"mid\"%'"),
        "ocr_done": q("SELECT COUNT(*) FROM sns_media_ocr WHERE kind='ocr' AND text!=''"),
        "vec": vec_n,
        "last_sync": last,
    }
