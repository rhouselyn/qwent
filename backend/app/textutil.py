# -*- coding: utf-8 -*-
"""消息文本清洗：把 XML 富媒体消息转成可读文本，并给出用于检索的干净文本。

微信 4.x 的 message_content 里塞着 XML（appmsg/pat/emoji 等），直接拿去显示或
建索引会让"你好"这类查询命中满屏 <msg><appmsg ...>，必须清洗。
"""
from __future__ import annotations

import re

_TAG = re.compile(r"<[^>]+>")
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_DES = re.compile(r"<des>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</des>", re.S)
_PAT_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_VOICE_LEN = re.compile(r'voicelength="(\d+)"')
# 搜索时需要跳过的消息类型：系统/拍一拍/表情等低信息量内容
NOISE_TYPES = {62, 47, 48, 50, 10000, 10002, 66}


_SENDER_PREFIX = re.compile(r"^(?:wxid_[0-9A-Za-z]+|[0-9A-Za-z_\-]{6,40}):\s*")
_LEADING_TAG = re.compile(r"^<[^>]+>\s*")


def normalize(content: str) -> str:
    """去掉群消息里的 "<wxid>:" 发送人前缀。"""
    c = (content or "").lstrip()
    m = _SENDER_PREFIX.match(c)
    if m and (m.group(0).startswith("wxid_") or len(m.group(0)) > 8):
        c = c[m.end():].lstrip()
    return c


def has_xml(content: str) -> bool:
    s = normalize(content)
    return s.startswith(("<?xml", "<msg", "<appmsg"))


def strip_xml(content: str) -> str:
    """去掉标签，留下人可读文本（保留标题/描述）。"""
    if not content:
        return ""
    title = _TITLE.search(content)
    if title:
        t = _CDATA.sub(r"\1", title.group(1)).strip()
        if t:
            return t
    cdata = _CDATA.search(content)
    if cdata:
        return cdata.group(1).strip()
    plain = _TAG.sub(" ", content)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain


_BASE64ISH = re.compile(r"^[A-Za-z0-9+/=]{60,}$")
_B64_JSON = re.compile(r"^eyJ[A-Za-z0-9+/=_-]{40,}")

# 富媒体消息（正文全是 XML 属性，剥完是空的）：给界面/引用一个统一可读标签
_MEDIA_LABEL = {3: "[图片]", 34: "[语音]", 43: "[视频]"}


def is_payload_dump(content: str) -> bool:
    """部分消息体是 base64(JSON/protobuf) 载荷，既不可读也无检索价值。"""
    c = (content or "").strip()
    if not c:
        return False
    return bool(_B64_JSON.match(c)) or bool(_BASE64ISH.match(c))


def display_text(type_id: int, content: str) -> str:
    """给界面/引用用的可读文本。"""
    if not content:
        return ""
    content = normalize(content)
    if is_payload_dump(content):
        return "[数据载荷]"
    if type_id in _MEDIA_LABEL and has_xml(content):
        return _MEDIA_LABEL[type_id]
    if type_id == 62:
        t = _TITLE.search(content)
        return _CDATA.sub(r"\1", t.group(1)).strip() if t else strip_xml(content)
    if has_xml(content):
        return strip_xml(content)
    return content


_APPMSG_TYPE = re.compile(r"<type>(\d+)</type>")
# appmsg 内部子类型也算噪声：62=拍一拍、3=音乐、33/36=小程序卡片等无正文信息
_NOISE_APPMSG_TYPES = {"62", "3", "2000", "2001"}


def appmsg_subtype(content: str) -> str:
    m = _APPMSG_TYPE.search(content or "")
    return m.group(1) if m else ""


def is_pat(content: str) -> bool:
    """拍一拍：外层常是 appmsg(type=49)，内部 <type>62</type>。"""
    if appmsg_subtype(content) == "62":
        return True
    return "拍了拍" in (content or "") and len(content) < 4000 and has_xml(content)


def search_text(type_id: int, content: str) -> str:
    """给全文索引用：噪声类型返回空串，XML 只保留标题/描述。"""
    if not content or type_id in NOISE_TYPES:
        return ""
    content = normalize(content)
    if appmsg_subtype(content) in _NOISE_APPMSG_TYPES:
        return ""
    if has_xml(content):
        title = _TITLE.search(content)
        des = _DES.search(content)
        parts = []
        if title:
            parts.append(_CDATA.sub(r"\1", title.group(1)).strip())
        if des:
            d = _CDATA.sub(r"\1", des.group(1)).strip()
            if d:
                parts.append(d)
        return " ".join(p for p in parts if p) or strip_xml(content)
    return content
