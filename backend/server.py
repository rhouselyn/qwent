# -*- coding: utf-8 -*-
"""一键启动：python server.py  → http://127.0.0.1:8721"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "refs" / "ChatTrace" / "src"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8721
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")
