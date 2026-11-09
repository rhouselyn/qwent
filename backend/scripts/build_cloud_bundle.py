# -*- coding: utf-8 -*-
"""构建云端最小运行包 dist/qwent-cloud.zip。

关键原则：**不 fork 问答代码**。从 backend/app/ 原样白名单拷贝同一份 .py，
用 QWENT_CLOUD=1 环境变量区分运行模式，因此「云端功能与本地一致」由构建
过程保证，而不是靠人工同步两份代码。

不含：frontend/、refs/ChatTrace（由 cloud_stub 兜底）、models/、data/、
desktop/、tests/、以及所有解密/扫钥/导入脚本。

用法：python backend/scripts/build_cloud_bundle.py
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP = ROOT / "backend" / "app"
CLOUD = ROOT / "cloud"
OUT = ROOT / "dist"

# 原样拷贝的源码（同一份文件，绝不改写）
BUNDLE_PY = [
    "agent.py", "botcore.py", "chat_core.py", "cloud_api.py", "cloud_apply.py",
    "cloud_cdc.py", "cloud_conf.py", "cloud_pack.py", "cloud_stub.py",
    "cloud_sync_local.py",
    "cloud_transport.py", "link_media.py", "main.py", "media_lookup.py",
    "paths.py", "platform_text.py", "privacy.py", "qq_attach.py",
    "qq_local.py", "qq_messages.py", "qqagent.py", "qqbot.py", "sns.py",
    "stream_qa.py", "textutil.py", "vector.py", "wxagent.py",
]

BUNDLE_SCRIPTS = ["cloud_cli.py", "smoke_cloud.py"]

EXTRA_FILES = [
    (CLOUD / "cloud_server.py", "cloud_server.py"),
    (CLOUD / "requirements-cloud.txt", "requirements-cloud.txt"),
    (CLOUD / "config-cloud.example.json", "backend/config.json"),
    (CLOUD / "deploy.sh", "deploy.sh"),
    (CLOUD / "qwent-cloud.service", "qwent-cloud.service"),
    (CLOUD / "openclaw.json.example", "openclaw.json.example"),
    (CLOUD / "merge_openclaw.py", "merge_openclaw.py"),
    (CLOUD / "README.md", "README.md"),
]


def build() -> Path:
    stage = OUT / "qwent-cloud"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "backend" / "app").mkdir(parents=True)
    (stage / "backend" / "scripts").mkdir(parents=True)
    (stage / "data").mkdir()
    (stage / "models").mkdir()
    (stage / "inbox" / "delta").mkdir(parents=True)
    (stage / "inbox" / "base").mkdir()

    missing = []
    for name in BUNDLE_PY:
        src = APP / name
        if not src.is_file():
            missing.append(name)
            continue
        shutil.copy2(src, stage / "backend" / "app" / name)
    for name in BUNDLE_SCRIPTS:
        shutil.copy2(ROOT / "backend" / "scripts" / name,
                     stage / "backend" / "scripts" / name)
    for src, rel in EXTRA_FILES:
        if not src.is_file():
            missing.append(str(src.name))
            continue
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    if missing:
        raise RuntimeError(f"打包缺少文件：{missing}")

    for d in ("data", "models", "inbox"):
        (stage / d / ".gitkeep").write_text("", encoding="utf-8")

    zip_path = OUT / "qwent-cloud.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, str(p.relative_to(stage)))
    print(f"[bundle] {zip_path}  ({zip_path.stat().st_size:,} bytes, "
          f"{len(BUNDLE_PY)} 个源码文件)")
    print(f"[bundle] 目录：{stage}")
    return zip_path


if __name__ == "__main__":
    build()
