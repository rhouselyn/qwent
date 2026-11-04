# -*- coding: utf-8 -*-
"""小红书 / B站 链接深度解析：图文取 OCR，视频取字幕或语音转写。

设计要点：
  - 只复用项目内已有引擎（rapidocr_onnxruntime 做 OCR，sherpa-paraformer 做 ASR，
    PyAV 自带的 ffmpeg 做音频解码），不引入需要系统装的 ffmpeg/yt-dlp。
  - 解析耗时（OCR 多图 / ASR 长视频）放到后台线程，前端轮询进度，避免请求卡死。
  - 结果统一写成「【正文】/【图片文字】/【视频字幕】」分段文本，直接进 link_cache，
    现有 FTS、retrieve_links、Agent read_link 全部自动生效。
  - 平台字段来自真实抓包验证（2026-10）：
      小红书分享页 window.__INITIAL_STATE__.noteData.data.noteData
      小红书 explore 页 window.__INITIAL_STATE__.note.noteDetailMap[noteId].note
      B站 api.bilibili.com/x/web-interface/view + x/player/v2(字幕) + x/player/playurl(音频)
"""
from __future__ import annotations

import io
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.parse

import requests

UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

MAX_IMAGES = 12           # 单篇最多 OCR 的图片数
MAX_VIDEO_SECONDS = 900   # 视频最多转写前 15 分钟
MAX_DOWNLOAD = 260 * 1024 * 1024
HTTP_TIMEOUT = 25

_ocr_engine = None
_asr_engine = None
_engine_lock = threading.Lock()


# ------------------------------------------------------------------ 平台识别
def normalize_url(url: str) -> str:
    """统一 URL 形式：反转义 HTML 实体（微信消息里是 &amp;），去首尾空白。

    消息正文里的链接带 &amp;，而入库/缓存键用的是反转义后的形式，
    不统一就会"明明解析过、从消息点进去却查不到缓存"。
    """
    import html as _html
    u = _html.unescape(str(url or "")).strip()
    # 微信卡片里偶尔夹带零宽字符
    return u.replace("\u200b", "").replace("\ufeff", "")


def _validate_remote_url(url: str) -> str:
    """拒绝本机/内网目标；链接内容来自聊天记录，不能默认可信。"""
    u = urllib.parse.urlparse(str(url or "").strip())
    if u.scheme not in {"http", "https"} or not u.hostname:
        raise ValueError("只支持 http/https 链接")
    host = u.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("不允许访问本机地址")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, u.port, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("链接域名无法解析") from exc
    # 198.18.0.0/15 是 Clash 系代理 fake-IP DNS 段：TUN 模式下所有域名都解析成它，
    # 不能按保留地址拒绝（否则开着代理时全部外链抓取都会失败）。
    fake_nets = [ipaddress.ip_network("198.18.0.0/15")]
    for a in addresses:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        if not ip.is_global and not any(ip in net for net in fake_nets):
            raise ValueError("不允许访问内网或保留地址")
    return u.geturl()


def _safe_get(url: str, **kwargs):
    """请求外部资源时逐跳校验重定向，避免短链绕过地址检查。"""
    current = _validate_remote_url(url)
    for _ in range(4):
        r = requests.get(current, allow_redirects=False, **kwargs)
        if r.status_code not in {301, 302, 303, 307, 308}:
            return r
        target = r.headers.get("Location")
        r.close()
        if not target:
            return r
        current = _validate_remote_url(urllib.parse.urljoin(current, target))
    raise ValueError("链接重定向次数过多")


def detect(url: str) -> str:
    u = normalize_url(url).lower()
    if any(k in u for k in ("xiaohongshu.com", "xhslink.com", "xhscdn.com")):
        return "xhs"
    if any(k in u for k in ("bilibili.com", "b23.tv", "bili2233.cn")):
        return "bili"
    return "generic"


def resolve(url: str) -> str:
    """跟随短链（xhslink / b23.tv）到真实地址。"""
    try:
        r = _safe_get(url, headers={"User-Agent": UA_MOBILE}, timeout=HTTP_TIMEOUT,
                      stream=True)
        final = r.url or url
        r.close()
        return final
    except Exception:
        return url


# ------------------------------------------------------------------ OCR / ASR
def _ocr(img_bytes: bytes) -> str:
    global _ocr_engine
    with _engine_lock:
        if _ocr_engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                _ocr_engine = RapidOCR()
            except Exception:
                _ocr_engine = False
    if not _ocr_engine:
        return ""
    try:
        res, _ = _ocr_engine(img_bytes)
        return chr(10).join(x[1] for x in res) if res else ""
    except Exception:
        return ""


def _build_asr():
    """复用 scripts/run_asr.py 的 sherpa 构建逻辑。"""
    global _asr_engine
    with _engine_lock:
        if _asr_engine is not None:
            return _asr_engine
        try:
            import sys
            from pathlib import Path
            import main as M
            scripts = str(Path(M.PROJECT) / "backend" / "scripts")
            if scripts not in sys.path:
                sys.path.insert(0, scripts)
            from run_asr import build_sherpa
            _asr_engine = build_sherpa()
        except Exception:
            _asr_engine = False
    return _asr_engine


def transcribe(media_path: str, max_seconds: int = MAX_VIDEO_SECONDS) -> tuple[str, float]:
    """解码音视频 → 16k 单声道 → sherpa 分块识别。返回 (文本, 实际转写秒数)。"""
    eng = _build_asr()
    if not eng:
        return "", 0.0
    import numpy as np
    import av
    try:
        container = av.open(media_path)
    except Exception:
        return "", 0.0
    stream = next((s for s in container.streams if s.type == "audio"), None)
    if stream is None:
        container.close()
        return "", 0.0
    # 统一重采样到 sherpa 要求的 16k / 单声道 / s16
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    pcm = []
    total = 0.0
    try:
        for frame in container.decode(stream):
            # 秒数 = samples / 采样率（曾误写成 duration / time_base，导致首帧就超限 break）
            rate = float(getattr(frame, "sample_rate", 0) or stream.rate or 16000)
            total += float(frame.samples or 0) / rate if rate else 0.0
            for out in resampler.resample(frame):
                pcm.append(out.to_ndarray().reshape(-1))
            if total > max_seconds + 30:
                break
    except Exception:
        pass
    finally:
        try:
            container.close()
        except Exception:
            pass
    if not pcm:
        return "", 0.0
    samples = np.concatenate(pcm).astype("float32") / 32768.0
    limit = int(max_seconds * 16000)
    if len(samples) > limit:
        samples = samples[:limit]
    engine, kind = eng
    if kind != "sherpa":
        return "", 0.0
    chunk = 16000 * 30          # 30 秒一块，控制内存
    texts = []
    for i in range(0, len(samples), chunk):
        seg = samples[i:i + chunk]
        if len(seg) < 1600:      # 太短跳过
            continue
        try:
            s = engine.create_stream()
            s.accept_waveform(16000, seg)
            engine.decode_stream(s)
            t = (s.result.text or "").strip()
            if t:
                texts.append(t)
        except Exception:
            continue
    return " ".join(texts), round(len(samples) / 16000.0, 1)


# ------------------------------------------------------------------ 小红书
def _xhs_state(html: str) -> dict | None:
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})</script>", html, re.S)
    if not m:
        return None
    raw = m.group(1).replace("undefined", "null")
    try:
        return json.loads(raw)
    except Exception:
        return None


def _xhs_note(state: dict) -> dict | None:
    # 分享页
    nd = (state.get("noteData") or {})
    data = nd.get("data") or {}
    if isinstance(data, dict) and isinstance(data.get("noteData"), dict):
        n = data["noteData"]
        if n.get("noteId") or n.get("title") or n.get("desc"):
            return n
    # explore / 详情页
    note_map = ((state.get("note") or {}).get("noteDetailMap") or {})
    for v in note_map.values():
        n = (v or {}).get("note") if isinstance(v, dict) else None
        if isinstance(n, dict) and (n.get("title") or n.get("desc") or n.get("imageList")):
            return n
    return None


def _xhs_image_url(im: dict) -> str | None:
    info = im.get("infoList") or []
    for x in info:
        u = x.get("url")
        if u:
            return u.replace("http://", "https://", 1) if u.startswith("http://") else u
    u = im.get("url")
    return u.replace("http://", "https://", 1) if u and u.startswith("http://") else u


def _xhs_video_url(note: dict) -> str | None:
    video = note.get("video") or {}
    stream = ((video.get("media") or {}).get("stream") or {})
    for codec in ("h264", "h265", "av1", "h266"):
        arr = stream.get(codec) or []
        for item in arr:
            u = item.get("masterUrl") or (item.get("backupUrls") or [None])[0]
            if u:
                return u
    return note.get("video", {}).get("url")


def parse_xhs(url: str, progress=None) -> dict:
    url = normalize_url(url)
    def emit(step: str, **kw):
        if progress:
            try:
                progress(step, **kw)
            except Exception:
                pass

    final = resolve(url) if any(k in url.lower() for k in ("xhslink.com",)) else url
    emit("正在打开小红书笔记…")
    try:
        r = _safe_get(final, headers={"User-Agent": UA_MOBILE}, timeout=HTTP_TIMEOUT)
        html = r.text
    except Exception as e:
        return {"error": f"打开笔记失败：{type(e).__name__} {str(e)[:60]}"}
    state = _xhs_state(html)
    if not state:
        return {"error": "无法解析笔记数据（可能需要登录或笔记已删除）"}
    note = _xhs_note(state)
    if not note:
        # 尝试用 explore 路径重取（app_share 链接有时只返回壳）
        m = re.search(r"(?:item|explore)/([0-9a-f]{24})", final)
        if m:
            tok = urllib.parse.parse_qs(urllib.parse.urlparse(final).query).get("xsec_token", [""])[0]
            alt = f"https://www.xiaohongshu.com/explore/{m.group(1)}"
            if tok:
                alt += "?xsec_token=" + urllib.parse.quote(tok) + "&xsec_source=pc_share"
            try:
                st2 = _xhs_state(_safe_get(alt, headers={"User-Agent": UA_DESKTOP},
                                            timeout=HTTP_TIMEOUT).text)
                note = _xhs_note(st2) if st2 else None
            except Exception:
                note = None
    if not note:
        return {"error": "笔记数据为空（可能已删除或需要登录）"}

    title = (note.get("title") or "").strip()
    desc = (note.get("desc") or "").strip()
    ntype = note.get("type") or ("video" if note.get("video") else "normal")
    images = [u for u in (_xhs_image_url(im) for im in (note.get("imageList") or [])) if u]
    vurl = _xhs_video_url(note) if ntype == "video" or note.get("video") else None

    parts = {"desc": desc}
    ocr_texts = []
    imgs = images[:MAX_IMAGES]
    if imgs:
        emit("正在 OCR 图片…", total=len(imgs), done=0)
        for i, iu in enumerate(imgs, 1):
            try:
                rb = _safe_get(iu, headers={"User-Agent": UA_MOBILE,
                                             "Referer": "https://www.xiaohongshu.com/"},
                               timeout=HTTP_TIMEOUT)
                if rb.ok and rb.content[:4] != b"\x00\x00\x00\x00":
                    t = _ocr(rb.content).strip()
                    if t:
                        ocr_texts.append(f"[图{i}] {t}")
            except Exception:
                pass
            emit("正在 OCR 图片…", total=len(imgs), done=i)
    parts["ocr"] = chr(10).join(ocr_texts)

    asr_text = ""
    if vurl:
        emit("正在下载视频…")
        try:
            tmp = _download(vurl, {"User-Agent": UA_MOBILE, "Referer": "https://www.xiaohongshu.com/"})
            if tmp:
                emit("正在语音转写视频…")
                asr_text, _ = transcribe(tmp)
                os.unlink(tmp)
        except Exception:
            pass
    parts["asr"] = asr_text

    return {"kind": "xhs", "title": title or "小红书笔记", "parts": parts,
            "images": images[:MAX_IMAGES], "video": {"url": vurl} if vurl else {},
            "platform": "小红书", "url": final}


# ------------------------------------------------------------------ B站
def parse_bili(url: str, progress=None) -> dict:
    url = normalize_url(url)
    def emit(step: str, **kw):
        if progress:
            try:
                progress(step, **kw)
            except Exception:
                pass

    final = resolve(url) if any(k in url.lower() for k in ("b23.tv", "bili2233.cn")) else url
    m = re.search(r"(BV[0-9A-Za-z]{10})", final) or re.search(r"av(\d+)", final)
    if not m:
        return {"error": "未能从链接识别视频号"}
    bvid = m.group(1) if m.group(1).startswith("BV") else None
    aid = None if bvid else m.group(1)
    page = 1
    pm = re.search(r"[?&]p=(\d+)", final)
    if pm:
        page = int(pm.group(1))
    H = {"User-Agent": UA_DESKTOP, "Referer": "https://www.bilibili.com/"}
    emit("正在读取视频信息…")
    try:
        q = {"bvid": bvid} if bvid else {"aid": aid}
        d = requests.get("https://api.bilibili.com/x/web-interface/view",
                         params=q, headers=H, timeout=HTTP_TIMEOUT).json()
    except Exception as e:
        return {"error": f"读取视频信息失败：{type(e).__name__}"}
    if d.get("code") != 0:
        return {"error": f"接口返回 {d.get('code')}：{d.get('message')}"}
    info = d["data"]
    bvid = info.get("bvid")
    pages = info.get("pages") or []
    cid = (pages[page - 1].get("cid") if len(pages) >= page else None) or info.get("cid")
    aid = info.get("aid")
    title = (info.get("title") or "").strip()
    desc = (info.get("desc") or "").strip()
    dur = info.get("duration") or 0
    owner = ((info.get("owner") or {}).get("name") or "")

    parts = {"desc": desc}
    # 1) 优先官方/AI 字幕（有则免下载免转写）
    sub_text = ""
    emit("正在检查视频字幕…")
    try:
        pv = requests.get("https://api.bilibili.com/x/player/v2",
                          params={"aid": aid, "cid": cid, "bvid": bvid},
                          headers=H, timeout=HTTP_TIMEOUT).json()
        subs = (((pv.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
        # 中文优先
        subs.sort(key=lambda s: 0 if str(s.get("lan", "")).lower().startswith("zh") else 1)
        if subs:
            su = subs[0].get("subtitle_url") or ""
            if su.startswith("//"):
                su = "https:" + su
            j = _safe_get(su, headers=H, timeout=HTTP_TIMEOUT).json()
            sub_text = "".join((b.get("content") or "") for b in (j.get("body") or []))
    except Exception:
        sub_text = ""
    parts["subtitle"] = sub_text.strip()

    # 2) 无字幕 → 下载音频转写
    asr_text = ""
    if not parts["subtitle"]:
        emit("正在获取音频流…")
        try:
            pl = requests.get("https://api.bilibili.com/x/player/playurl",
                              params={"bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1},
                              headers=H, timeout=HTTP_TIMEOUT).json()
            dash_audio = (((pl.get("data") or {}).get("dash") or {}).get("audio") or [])
            if dash_audio:
                best = max(dash_audio, key=lambda a: a.get("bandwidth") or 0)
                au = best.get("baseUrl") or (best.get("backupUrl") or [None])[0]
                if au:
                    emit("正在下载音频…")
                    tmp = _download(au, H)
                    if tmp:
                        emit("正在语音转写…")
                        asr_text, _ = transcribe(tmp)
                        os.unlink(tmp)
        except Exception:
            pass
    parts["asr"] = asr_text

    cover = info.get("pic") or ""
    if cover.startswith("http://"):
        cover = cover.replace("http://", "https://", 1)
    return {"kind": "bili", "title": title or "B站视频", "parts": parts,
            "images": [cover] if cover else [], "video": {"url": final, "duration": dur},
            "platform": "哔哩哔哩", "url": final,
            "owner": owner, "page": page, "pages": len(pages),
            "truncated": bool(asr_text)}


# ------------------------------------------------------------------ 通用工具
def _download(url: str, headers: dict) -> str | None:
    """流式下载到临时文件，超过上限中止。返回路径。"""
    import tempfile
    try:
        with _safe_get(url, headers=headers, timeout=HTTP_TIMEOUT, stream=True) as r:
            if not r.ok:
                return None
            suffix = ".mp4" if "video/mp4" in (r.headers.get("content-type") or "") else ".m4a"
            fd, path = tempfile.mkstemp(suffix=suffix)
            size = 0
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(1024 * 256):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        break
                    f.write(chunk)
            if size < 1024:
                os.unlink(path)
                return None
            return path
    except Exception:
        return None


def compose_text(res: dict) -> str:
    """把分段结果拼成可检索文本。"""
    parts = res.get("parts") or {}
    out = []
    if parts.get("desc"):
        out.append("【正文】" + chr(10) + parts["desc"])
    if parts.get("ocr"):
        out.append("【图片文字】" + chr(10) + parts["ocr"])
    if parts.get("subtitle"):
        out.append("【视频字幕】" + chr(10) + parts["subtitle"])
    if parts.get("asr"):
        head = "【视频语音转写】"
        if res.get("truncated"):
            head += "（仅前 %d 分钟）" % (MAX_VIDEO_SECONDS // 60)
        out.append(head + chr(10) + parts["asr"])
    if not out:
        out.append("（未提取到正文内容）")
    return (chr(10) + chr(10)).join(out).strip()


def parse(url: str, progress=None) -> dict:
    """解析入口：按平台分发，统一返回 {kind,title,text,parts,images,...}。"""
    url = _validate_remote_url(normalize_url(url))
    kind = detect(url)
    if kind == "xhs":
        res = parse_xhs(url, progress)
    elif kind == "bili":
        res = parse_bili(url, progress)
    else:
        return {"error": "unsupported"}
    if res.get("error"):
        return res
    res["text"] = compose_text(res)
    res["chars"] = len(res["text"])
    return res


# ------------------------------------------------------------------ 后台任务
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def job(url: str) -> dict:
    with _JOBS_LOCK:
        return dict(_JOBS.get(url) or {"state": "none"})


def start_job(url: str) -> dict:
    """启动后台解析（同一 url 只跑一次），返回当前任务状态。"""
    with _JOBS_LOCK:
        cur = _JOBS.get(url)
        if cur and cur.get("state") in ("running", "done"):
            return dict(cur)
        _JOBS[url] = {"state": "running", "step": "排队中…", "progress": 0,
                      "started": int(time.time()), "result": None}
    def _run():
        def cb(step, **kw):
            with _JOBS_LOCK:
                j = _JOBS.get(url)
                if not j:
                    return
                j["step"] = step
                total, done = kw.get("total"), kw.get("done")
                if total:
                    j["progress"] = int(done / max(1, total) * 95)
        try:
            res = parse(url, cb)
            with _JOBS_LOCK:
                _JOBS[url] = {**( _JOBS.get(url) or {}), "state": "done",
                              "step": "完成", "progress": 100, "result": res,
                              "finished": int(time.time())}
        except Exception as e:
            with _JOBS_LOCK:
                _JOBS[url] = {**( _JOBS.get(url) or {}), "state": "error",
                              "step": f"失败：{type(e).__name__} {str(e)[:60]}",
                              "progress": 100, "result": {"error": str(e)[:200]},
                              "finished": int(time.time())}
    threading.Thread(target=_run, daemon=True).start()
    return job(url)
