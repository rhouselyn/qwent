# -*- coding: utf-8 -*-
"""平台文案：系统消息与界面术语在「微信 / QQ」两套说法之间对齐。

微信 4.x 的 sysmsg 内容本来就是客户端渲染好的中文（或带占位符的模板 XML），
代码只做反转义 + 去标签。QQ 的同类系统提示措辞不同（改群名、入群、撤回、
加好友…），这里按规则转换，前端按当前平台选 parsed.system / parsed.system_qq。
"""
from __future__ import annotations

import html
import re

_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_SENDER_PREFIX = re.compile(r"^(?:wxid_[0-9A-Za-z]+|[0-9A-Za-z_\-]{6,40}):\s*")
_TAG = re.compile(r"<[^>]+>")
_PLAIN = re.compile(r"<(?:plain|content|title|template)>(.*?)</(?:plain|content|title|template)>", re.S)
_LINK_NAME = re.compile(r"<link[^>]*>\s*<name>(.*?)</name>", re.S)
_PLACEHOLDER = re.compile(r"%s|\$\$|#\w+#")

# 微信 → QQ：先处理整句模板，再兜底替换术语与引号
_QQ_RULES: list[tuple[re.Pattern, object]] = [
    (re.compile(r'你邀请\s*"?([^"\s]{1,32})"?\s*加入(?:了)?群聊'), r"你邀请\1加入了群聊"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*(?:修改群名为|把群名(?:称)?改为|更改群名为)\s*["“]?([^"”]{1,60})["”]?'),
     r"\1修改了群名称：\2"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*修改(?:了)?群名(?:称)?'), r"\1修改了群名称"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*邀请\s*"?([^"\s]{1,32})"?\s*加入(?:了)?群聊'), r"\1邀请\2加入了群聊"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*通过(?:扫描)?二维码加入(?:了)?群聊'), r"\1通过二维码加入了群聊"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*加入(?:了)?群聊'), r"\1加入了群聊"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*退出(?:了)?群聊'), r"\1退出了群聊"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*(?:被|已)?移出(?:了)?群聊'), r"\1已被移出群聊"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*撤回(?:了)?一条消息'), r"\1撤回了一条消息"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*拍了拍\s*"?([^"\s]{1,32})"?'), r"\1戳了戳\2"),
    (re.compile(r'你已添加(?:了)?\s*"?([^"\s]{1,32})"?\s*[，,]?\s*现在可以开始聊天了'),
     r"你已添加\1为好友，开始聊天吧"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*邀请你加入(?:了)?群聊'), r"\1邀请你加入了群聊"),
    (re.compile(r'你已被\s*"?([^"\s]{1,32})"?\s*移出(?:了)?群聊'), r"你已被\1移出群聊"),
    (re.compile(r'你撤回(?:了)?一条消息'), r"你撤回了一条消息"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*领取了你的红包'), r"\1领取了你的红包"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*(?:发布|修改)了?群公告'), r"\1更新了群公告"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*已将?群主(?:身份)?转让给\s*"?([^"\s]{1,32})"?'), r"\1已将群主转让给\2"),
    (re.compile(r'"?([^"\s]{1,32})"?\s*开启了?群(?:聊)?邀请确认'), r"\1开启了群邀请确认"),
]

_TERMS = [
    ("链接 / 公众号", "链接"), ("链接/公众号", "链接"),
    ("公众号文章", "网页文章"), ("公众号正文", "网页正文"),
    ("朋友圈", "QQ空间"), ("公众号", "网页"), ("微信支付", "QQ钱包"),
    ("微信", "QQ"), ("通讯录", "联系人"), ("聊天记录", "消息记录"),
    ("小程序", "应用"), ("视频号", "短视频"), ("微信群", "QQ群"),
]


def _unescape(s: str) -> str:
    for _ in range(4):
        d = html.unescape(s)
        if d == s:
            break
        s = d
    return s


def _one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def sysmsg_wechat(content: str, recall: bool = False) -> str:
    """从 sysmsg XML / 纯文本还原微信客户端显示的那句中文。"""
    c = _SENDER_PREFIX.sub("", (content or "").lstrip())
    c = _CDATA.sub(lambda m: m.group(1), c)
    c = _unescape(c)
    if recall:
        mc = re.search(r"<content>(.*?)</content>", c, re.S)
        if mc:
            c = _CDATA.sub(lambda m: m.group(1), mc.group(1))
            c = _unescape(c)
        return _one_line(_TAG.sub("", c))[:300]
    if "<sysmsg" in c or "<appmsg" in c:
        # 模板型系统消息：<plain> 是带占位符的句子，<link><name> 是人名/群名
        plains = [_one_line(_TAG.sub("", _unescape(x))) for x in _PLAIN.findall(c)]
        plains = [x for x in plains if x]
        if plains:
            sentence = plains[0]
            names = [_one_line(_TAG.sub("", _unescape(n)))
                     for n in _LINK_NAME.findall(c)]
            names = [n for n in names if n]
            for name in names:
                if _PLACEHOLDER.search(sentence):
                    sentence = _PLACEHOLDER.sub(lambda _m, v=name: v, sentence, count=1)
                elif name not in sentence:
                    sentence += " " + name
            text = sentence
        else:
            text = _one_line(_TAG.sub(" ", c))
    else:
        text = _one_line(_TAG.sub("", c))
    # 去掉 XML 属性残渣 / 撤回时间戳尾巴
    text = re.sub(r"\b\w+=\S+", " ", text)
    text = re.sub(r"revoketime\s*\d*", " ", text, flags=re.I)
    text = re.sub(r"(?:<|&lt;)/?\w+[^>]*>", " ", text)
    return _one_line(text)[:300]


def sysmsg_qq(text: str) -> str:
    """把微信系统提示转成 QQ 的说法；不匹配任何规则时只做术语替换。"""
    t = _one_line(text)
    if not t:
        return t
    for pat, repl in _QQ_RULES:
        new = pat.sub(repl, t)
        if new != t:
            t = new
            break
    # 微信习惯用双引号包昵称，QQ 系统消息不带
    t = re.sub(r'"([^"\s]{1,32})"', r"\1", t)
    for a, b in _TERMS:
        t = t.replace(a, b)
    return _one_line(t)[:300]


def system_pair(content: str, recall: bool = False) -> tuple[str, str]:
    wx = sysmsg_wechat(content, recall)
    return wx, sysmsg_qq(wx)


def localize(text: str, platform: str = "wechat") -> str:
    """给界面文案用的术语替换（微信术语 → QQ 术语）。"""
    if platform != "qq" or not text:
        return text or ""
    out = str(text)
    for a, b in _TERMS:
        out = out.replace(a, b)
    return out
