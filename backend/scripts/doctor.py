# -*- coding: utf-8 -*-
"""就绪自检:一条命令确认"克隆 + 准备之后是否直接可用"。

用法:python backend/scripts/doctor.py
只读检查,不改任何东西;每项给出 ✓/✗ 和对应修复命令。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "backend" / "app"))
sys.path.insert(0, str(PROJECT / "backend" / "scripts"))

OK, BAD = "[✓]", "[✗]"

# 运行必需的第三方包(模块名);setup.py 复用此列表做安装结果判定
DEPS = ["fastapi", "uvicorn", "requests", "numpy", "cryptography", "zstandard",
        "psutil", "sqlite_vec", "transformers", "torch", "sherpa_onnx",
        "av", "rapidocr_onnxruntime", "docx", "openpyxl"]


def check(name: str, ok: bool, fix: str = "", note: str = "") -> bool:
    print(f"{OK if ok else BAD} {name}" + (f" — {note}" if note else ""))
    if not ok and fix:
        print(f"     ↳ 修复: {fix}")
    return ok


def main() -> int:
    results: list[bool] = []
    print(f"== 微信知识助手就绪自检 ==  ({PROJECT})\n")

    # 1. Python 版本
    v = sys.version_info
    results.append(check("Python ≥ 3.11", (v.major, v.minor) >= (3, 11),
                         "安装 Python 3.11+", note=sys.version.split()[0]))

    # 2. 依赖(find_spec 只查存在性,不真正 import,秒回)
    missing = [d for d in DEPS if importlib.util.find_spec(d) is None]
    results.append(check("pip 依赖已安装", not missing,
                         "pip install -r requirements.txt",
                         f"缺少: {', '.join(missing)}" if missing else f"{len(DEPS)} 个全部就绪"))

    # 3. 配置文件
    cfg_path = PROJECT / "backend" / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text("utf-8"))
            key = (cfg.get("llm") or {}).get("api_key", "")
            bad_key = (not key) or key.startswith("sk-在这里") or "你的" in key
            results.append(check("backend/config.json 且 API key 已填", not bad_key,
                                 "编辑 backend/config.json 填入 DeepSeek api_key"))
        except Exception as e:
            results.append(check("backend/config.json 可解析", False,
                                 "重新复制 config.example.json", str(e)))
    else:
        results.append(check("backend/config.json 存在", False,
                             "copy backend\\config.example.json backend\\config.json 后填 key"))

    # 4. 微信账号目录
    try:
        from paths import ACCOUNT_DIR, ACCOUNT_ID, DECRYPTED_DIR
        results.append(check("微信账号目录已定位", True,
                             note=f"{ACCOUNT_DIR}"))
        results.append(check("解密输出目录(ChatTrace)存在", DECRYPTED_DIR.is_dir(),
                             "python backend/scripts/m0_decrypt.py(需先抓密钥,见项目文档)",
                             str(DECRYPTED_DIR)))
    except SystemExit as e:
        results.append(check("微信账号目录已定位", False,
                             "backend/config.json 的 account.account_dir 手动指定", str(e)))

    # 5. 知识库
    db = PROJECT / "data" / "knowledge.db"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            n = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
            con.close()
            results.append(check("knowledge.db 可读", True, note=f"{n:,} 条消息"))
        except Exception as e:
            results.append(check("knowledge.db 可读", False,
                                 "python backend/scripts/m0_import.py", str(e)))
    else:
        results.append(check("knowledge.db 存在", False,
                             "python backend/scripts/m0_decrypt.py && python backend/scripts/m0_import.py"))

    # 6. 模型
    try:
        from download_models import status
        st = status()
        for name, ok in st.items():
            fix = "python backend/scripts/download_models.py --bge" if "向量" in name else \
                  "python backend/scripts/download_models.py --asr" if "语音" in name else \
                  "python backend/scripts/download_models.py --chattrace"
            results.append(check(f"模型/依赖: {name}", ok, fix))
    except Exception as e:
        results.append(check("模型状态检查", False, "", str(e)))

    print()
    if all(results):
        print("全部就绪,可直接启动:python backend/server.py 8721")
        return 0
    print(f"有 {results.count(False)} 项未就绪,按上面 ↳ 提示处理后重跑本命令。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
