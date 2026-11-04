# -*- coding: utf-8 -*-
"""一键启动：python server.py  → http://127.0.0.1:8721"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "refs" / "ChatTrace" / "src"))

import socket  # noqa: E402
import sys  # noqa: E402
import uvicorn  # noqa: E402


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8721
    if not _port_free(port):
        print(f"[!] 端口 {port} 已被占用：助手可能已经在运行，"
              f"直接访问 http://127.0.0.1:{port} 即可；如确认异常，请先结束旧进程。")
        sys.exit(2)
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")
