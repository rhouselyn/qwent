# -*- coding: utf-8 -*-
"""QQ 机器人附件解析：图片 OCR、语音/视频转写、文件留存。

QQ 官方网关把图片、语音、视频、文件放在事件的 attachments 数组里，url 有
时效。这里在收到消息时立即下载到本机 data/qq_media/，能做 OCR/ASR 的顺手
解析成文字，随消息一起落库——即使之后 CDN 链接过期或对方撤回，消息仍在。
"""
from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path

import requests

PROJECT = Path(__file__).resolve().parent.parent.parent
MEDIA_DIR = PROJECT / "data" / "qq_media"

MAX_KEEP_BYTES = 80 * 1024 * 1024          # 单附件落盘上限
MAX_OCR_CHARS = 1000
MAX_ASR_CHARS = 1200
MAX_ATTACHMENTS = 4
MAX_VIDEO_SECONDS = 600

log = logging.getLogger("qqbot")

_EXT_BY_TYPE = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "voice": ".wav", "audio/amr": ".amr", "audio/silk": ".silk",
    "audio/wav": ".wav", "audio/mpeg": ".mp3",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    "file": "", "application/pdf": ".pdf",
}


def _kind(content_type: str, filename: str) -> str:
    ct = (content_type or "").lower().strip()
    fn = (filename or "").lower()
    if ct.startswith("image/") or ct in ("image", "png", "jpg", "jpeg", "gif", "webp"):
        return "image"
    if "voice" in ct or "audio" in ct or ct in ("silk", "amr", "wav"):
        return "voice"
    if ct.startswith("video/") or "video" in ct:
        return "video"
    if fn.startswith("image") or fn.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    if fn.endswith((".silk", ".amr", ".wav", ".mp3", ".m4a")):
        return "voice"
    if fn.endswith((".mp4", ".mov", ".webm")):
        return "video"
    return "file"


def _ext(content_type: str, filename: str, fallback: str) -> str:
    ct = (content_type or "").lower().strip()
    if ct in _EXT_BY_TYPE and _EXT_BY_TYPE[ct]:
        return _EXT_BY_TYPE[ct]
    suffix = Path(filename or "").suffix.lower()
    if suffix and len(suffix) <= 8 and re.fullmatch(r".[a-z0-9]+", suffix):
        return suffix
    if ct:
        guessed = mimetypes.guess_extension(ct.split(";", 1)[0].strip())
        if guessed:
            return guessed
    return fallback


def _safe_id(msg_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", str(msg_id or ""))[:60] or "msg"


def _download(url: str, cap: int = MAX_KEEP_BYTES) -> bytes:
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return b""
    try:
        with requests.get(url, timeout=(6, 15), stream=True) as r:
            if r.status_code >= 400:
                log.warning("attachment download %s %s", r.status_code, url[:80])
                return b""
            buf = bytearray()
            for chunk in r.iter_content(64 * 1024):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > cap:
                    log.warning("attachment over cap (%s bytes), skipped", cap)
                    return b""
            return bytes(buf)
    except Exception as exc:  # noqa: BLE001
        log.warning("attachment download failed: %s", exc)
        return b""


def _ocr(data: bytes) -> str:
    if not data:
        return ""
    try:
        import link_media
        return (link_media._ocr(data) or "").strip()[:MAX_OCR_CHARS]
    except Exception as exc:  # noqa: BLE001
        log.debug("attachment ocr skipped: %s", exc)
        return ""


def _transcribe(path: Path, max_seconds: int = MAX_VIDEO_SECONDS) -> str:
    try:
        import link_media
        text, _ = link_media.transcribe(str(path), max_seconds)
        return (text or "").strip()[:MAX_ASR_CHARS]
    except Exception as exc:  # noqa: BLE001
        log.debug("attachment asr skipped: %s", exc)
        return ""


def _text_file(path: Path) -> str:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return ""
        raw = path.read_bytes()
        return re.sub(r"\s+", " ", raw.decode("utf-8", "ignore")).strip()[:1500]
    except Exception:
        return ""


def parse(msg_id: str, attachments: list | None) -> tuple[str, list[dict]]:
    """下载并解析附件，返回 (追加文本, 供前端展示的 media 列表)。"""
    if not attachments:
        return "", []
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    out: list[dict] = []
    for i, a in enumerate(list(attachments)[:MAX_ATTACHMENTS]):
        if not isinstance(a, dict):
            continue
        ctype = str(a.get("content_type") or "")
        filename = Path(str(a.get("filename") or "")).name[:120]
        kind = _kind(ctype, filename)
        url = str(a.get("voice_wav_url") or "") if kind == "voice" else ""
        url = url or str(a.get("url") or "")
        ext = _ext(ctype, filename, {"image": ".jpg", "voice": ".wav",
                                     "video": ".mp4", "file": ""}[kind])
        name = f"{_safe_id(msg_id)}_{i}{ext}"
        path = MEDIA_DIR / name
        data = _download(url, MAX_KEEP_BYTES)
        saved = False
        if data:
            try:
                path.write_bytes(data)
                saved = True
            except Exception as exc:  # noqa: BLE001
                log.warning("attachment write failed: %s", exc)

        if kind == "image":
            ocr = _ocr(data)
            label = "[图片]" + (f"\n{ocr}" if ocr else "")
            out.append({"kind": "image", "name": name if saved else "",
                        "filename": filename or "图片", "text": ocr})
        elif kind == "voice":
            asr = str(a.get("asr_refer_text") or "").strip()[:MAX_ASR_CHARS]
            if not asr and saved:
                asr = _transcribe(path)
            label = "[语音]" + (f"\n{asr}" if asr else "")
            out.append({"kind": "voice", "name": name if saved else "",
                        "filename": filename or "语音", "text": asr})
        elif kind == "video":
            asr = _transcribe(path) if saved else ""
            label = "[视频]" + (f"\n{asr}" if asr else "")
            out.append({"kind": "video", "name": name if saved else "",
                        "filename": filename or "视频", "text": asr})
        else:
            text = _text_file(path) if saved and not filename.lower().endswith(
                (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")) else ""
            label = f"[文件] {filename or '未命名文件'}"
            if text:
                label += f"\n{text}"
            out.append({"kind": "file", "name": name if saved else "",
                        "filename": filename or "未命名文件", "text": text})
        lines.append(label)
    return "\n".join(lines)[:4000], out
