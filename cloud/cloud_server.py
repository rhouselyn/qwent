# -*- coding: utf-8 -*-
"""云端最小运行单元入口：QWENT_CLOUD=1 + 只监听 127.0.0.1。

OpenClaw Gateway 与Assistant 同机，因此 baseUrl 仍是 http://127.0.0.1:8721/v1，
服务器无需开放任何公网入站端口（同步走 SSH/SFTP 出站）。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ["QWENT_CLOUD"] = "1"
os.environ.setdefault("QWENT_DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("QWENT_MODELS_DIR", str(ROOT / "models"))
sys.path.insert(0, str(ROOT / "backend" / "app"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8721
    uvicorn.run("main:app", host="127.0.0.1", port=port, log_level="warning")
