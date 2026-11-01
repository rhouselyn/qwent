# -*- coding: utf-8 -*-
"""下载朋友圈 CDN 媒体解密所需的 WeFlow WASM 组件（可选，供朋友圈图片功能）。

微信朋友圈 CDN 的图/视频是 WxIsaac64 流加密。权威解密实现来自开源项目
WeFlow 的 wasm（该仓库无 LICENSE，故不能随本项目分发，运行时按需下载到
本地 refs/WeChatDataAnalysis/ ——该目录已被 .gitignore 排除）。

一键配置脚本会在启用朋友圈功能时自动调用；也可手动运行：
  python backend/scripts/setup_sns_wasm.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEST = HERE.parent.parent / "refs" / "WeChatDataAnalysis"

FILES = {
    "wasm_video_decode.js": [
        "https://raw.githubusercontent.com/hicccc77/WeFlow/main/electron/assets/wasm/wasm_video_decode.js",
        "https://ghproxy.net/https://raw.githubusercontent.com/hicccc77/WeFlow/main/electron/assets/wasm/wasm_video_decode.js",
        "https://cdn.jsdelivr.net/gh/hicccc77/WeFlow@main/electron/assets/wasm/wasm_video_decode.js",
    ],
    "wasm_video_decode.wasm": [
        "https://raw.githubusercontent.com/hicccc77/WeFlow/main/electron/assets/wasm/wasm_video_decode.wasm",
        "https://ghproxy.net/https://raw.githubusercontent.com/hicccc77/WeFlow/main/electron/assets/wasm/wasm_video_decode.wasm",
        "https://cdn.jsdelivr.net/gh/hicccc77/WeFlow@main/electron/assets/wasm/wasm_video_decode.wasm",
    ],
}
# 同目录的 ks_daemon.js 是本项目自有文件，随仓库分发；这里只补 wasm 两个外件。
DAEMON_SRC = HERE.parent / "app" / "sns_ks_daemon.js"


def main() -> int:
    import urllib.request
    DEST.mkdir(parents=True, exist_ok=True)
    # 守护进程脚本：随仓库分发（backend/app/sns_ks_daemon.js），复制到运行目录
    if DAEMON_SRC.exists() and not (DEST / "ks_daemon.js").exists():
        (DEST / "ks_daemon.js").write_text(DAEMON_SRC.read_text("utf-8"), encoding="utf-8")
        print(f"[ok] ks_daemon.js -> {DEST}")
    fail = []
    for name, urls in FILES.items():
        dest = DEST / name
        if dest.is_file() and dest.stat().st_size > 1000:
            print(f"[skip] {name} 已存在")
            continue
        ok = False
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=60).read()
                if len(data) > 1000:
                    dest.write_bytes(data)
                    print(f"[ok] {name} <- {url} ({len(data)} bytes)")
                    ok = True
                    break
            except Exception as exc:
                print(f"[..] {name}: {url} 失败 {exc}")
        if not ok:
            fail.append(name)
    if fail:
        print(f"[!!] 下载失败: {fail} —— 朋友圈图片将只能显示本地缓存，可手动从 "
              f"https://github.com/hicccc77/WeFlow (electron/assets/wasm/) 放入 {DEST}")
        return 1
    print("[ok] 朋友圈 CDN 解密组件就绪（需要本机装有 Node.js）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
