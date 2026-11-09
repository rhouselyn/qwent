# -*- coding: utf-8 -*-
"""把微信渠道片段安全合并进服务器已有的 ~/.openclaw/openclaw.json。

服务器上已经跑着 QQ Bot（agent=main，模型 deepseek-flash），本脚本：
  * 备份原配置；
  * 只新增插件启用 / wechatkb provider / qwent agent / 渠道占位；
  * **不改** agents.defaults.model.primary，也不动 channels.qqbot；
  * 默认 dry-run，加 --apply 才真正写入。

用法：python3 merge_openclaw.py [--apply] [--api-key wk-xxx]
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import time
from pathlib import Path

PATCH = Path(__file__).with_name("openclaw.json.example")
TARGET = Path.home() / ".openclaw" / "openclaw.json"


def deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        elif isinstance(v, list) and isinstance(dst.get(k), list):
            for x in v:
                if x not in dst[k]:
                    dst[k].append(x)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--api-key", default="")
    args = ap.parse_args()

    if not TARGET.is_file():
        raise SystemExit(f"找不到 {TARGET}，请先在服务器安装 OpenClaw")
    original = json.loads(TARGET.read_text(encoding="utf-8"))
    patch = json.loads(PATCH.read_text(encoding="utf-8"))
    if args.api_key:
        patch["models"]["providers"]["wechatkb"]["apiKey"] = args.api_key
    merged = deep_merge(copy.deepcopy(original), patch)

    print("改动摘要：")
    print("  新增插件:", "openclaw-weixin" in merged.get("plugins", {}).get("entries", {}))
    print("  provider:", list(merged.get("models", {}).get("providers", {}).keys()))
    print("  agent   :", list(merged.get("agents", {}).get("entries", {}).keys()))
    print("  主模型  :", merged.get("agents", {}).get("defaults", {}).get("model", {}).get("primary"))
    print("  QQ 渠道保留:", "qqbot" in merged.get("channels", {}))
    if not args.apply:
        print("\n[dry-run] 未写入。确认后加 --apply")
        return
    bak = TARGET.with_name(f"openclaw.json.bak-qwent-{int(time.time())}")
    shutil.copy2(TARGET, bak)
    TARGET.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {TARGET}；备份：{bak}")
    print("接下来：openclaw plugins install \"@tencent-weixin/openclaw-weixin\" 后重启 Gateway")


if __name__ == "__main__":
    main()
