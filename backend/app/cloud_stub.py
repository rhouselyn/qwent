# -*- coding: utf-8 -*-
"""云端最小包里 ChatTrace 解密组件的无副作用替身。

云端不打包 refs/ChatTrace/src（也不该打包：密钥、Windows 解密、DPAPI 都不适用），
但 main.py 顶层 import 需要这三个符号存在。真正的媒体解密只在本地 Web UI 用，
云端机器人走纯文本问答，因此这里全部返回空值即可。

本地不会加载本模块（ChatTrace 存在时优先走真实实现）。
"""
from __future__ import annotations


class _Noop:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            return None
        return _call

    def __call__(self, *args, **kwargs):
        return None


class MediaService(_Noop):
    def image_keys(self):
        return {}

    def decrypt_image(self, *args, **kwargs):
        return b""


class ImageKeyResolver(_Noop):
    pass


def decode_dat(*args, **kwargs) -> bytes:
    return b""
