# -*- coding: utf-8 -*-
"""一键配置:从零克隆到可运行的全部准备步骤,带分步进度。

用法:
  python backend/scripts/setup.py                 # 交互式(缺 key 时询问;无知识库时询问恢复备份)
  python backend/scripts/setup.py --yes           # 非交互,只报告不询问
  python backend/scripts/setup.py --index-url URL # 自定义 pip 源(默认清华源)

步骤:
  [1/5] Python 环境     版本 ≥ 3.11
  [2/5] 依赖安装        pip install -r requirements.txt
  [3/5] 本机配置        生成 config.json,缺失 API key 时询问填写
  [4/5] 模型与组件      bge 向量模型 / paraformer 语音 / ChatTrace(内置,缺失才补拉)
  [5/5] 就绪自检        doctor.py 全项检查,全 ✓ 即可直接启动

脚本可重复运行:已完成的步骤自动跳过。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
PROJECT = BACKEND.parent
sys.path.insert(0, str(HERE))

TSINGHUA = "https://pypi.tuna.tsinghua.edu.cn/simple"
_state = {"n": 0, "total": 5, "fails": []}


def step(title: str) -> None:
    _state["n"] += 1
    bar = "=" * 64
    print(f"\n{bar}\n[{_state['n']}/{_state['total']}] {title}\n{bar}")


def _mark(ok: bool, label: str) -> None:
    if not ok:
        _state["fails"].append(label)
    print(f"{'[OK] ' if ok else '[失败]'} {label}")


def step_env() -> None:
    step("Python 环境")
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 11)
    _mark(ok, f"Python {v.major}.{v.minor}.{v.micro}" +
          ("" if ok else "  (需要 3.11+,请安装后用新 Python 重跑)"))


def step_deps(index_url: str) -> None:
    step("依赖安装(pip install -r requirements.txt)")
    cmd = [sys.executable, "-m", "pip", "install", "-r",
           str(PROJECT / "requirements.txt"), "-i", index_url]
    print("+ " + " ".join(cmd))
    t0 = time.time()
    r = subprocess.run(cmd)
    # 以"依赖实际可导入"为准:pip 对本机其他包的历史版本冲突会报 ERROR 并返回非 0,但不影响本项目
    import importlib.util
    import doctor
    missing = [d for d in doctor.DEPS if importlib.util.find_spec(d) is None]
    if not missing:
        note = f"{len(doctor.DEPS)} 个依赖全部就绪,用时 {time.time() - t0:.0f}s"
        if r.returncode != 0:
            note += "(pip 提示与本机其他包存在版本冲突,不影响本项目运行)"
        _mark(True, note)
    else:
        _mark(False, f"依赖缺失: {', '.join(missing)} — "
                     f"检查网络/代理后重跑,或用 --index-url 换 pip 源")


def step_config(yes: bool) -> None:
    step("本机配置(backend/config.json)")
    cfg, tmpl = BACKEND / "config.json", BACKEND / "config.example.json"
    if not cfg.exists():
        shutil.copy(tmpl, cfg)
        print(f"已从模板创建 {cfg}")
    data = json.loads(cfg.read_text("utf-8"))
    key = (data.get("llm") or {}).get("api_key", "")
    if key and "你的" not in key and not key.startswith("sk-在这里"):
        _mark(True, "API key 已配置")
        return
    if yes:
        _mark(False, "API key 未填写 — 启动前请编辑 backend/config.json")
        return
    print("需要 DeepSeek API key(https://platform.deepseek.com 申请)")
    k = input("粘贴后回车(直接回车=跳过,稍后自己填): ").strip().strip('"').strip("'")
    if k:
        data.setdefault("llm", {})["api_key"] = k
        cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        _mark(True, "API key 已写入 config.json")
    else:
        _mark(False, "API key 未填写 — 启动前请编辑 backend/config.json")

    # 无知识库时,顺手提供备份恢复入口
    db = PROJECT / "data" / "knowledge.db"
    if db.exists():
        print("[OK] 知识库已存在(换机场景可跳过)")
        return
    print("\n未检测到 data/knowledge.db(旧电脑的聊天记录库)。")
    p = input("如有备份包,拖入 zip 后回车恢复;直接回车=跳过: ").strip().strip('"').strip("'")
    if p:
        try:
            from backup_data import restore
            restore(Path(p), yes=True)
        except SystemExit as e:
            print(f"[!] 恢复失败: {e}")


def step_models() -> None:
    step("模型与组件下载")
    from download_models import ensure_bge, ensure_chattrace, ensure_para
    for fn, label in ((ensure_bge, "向量模型 bge-small-zh (~91MB)"),
                      (ensure_para, "语音模型 para-zh (~78MB)"),
                      (ensure_chattrace, "ChatTrace(已内置,缺失才补拉)")):
        try:
            fn()
            _mark(True, label)
        except Exception as e:
            _mark(False, f"{label}: {e}")


def step_check() -> None:
    step("就绪自检")
    import doctor
    ok = doctor.main() == 0
    _mark(ok, "doctor.py 全项自检")


def main() -> int:
    ap = argparse.ArgumentParser(description="一键配置(可重复运行,自动跳过已完成步骤)")
    ap.add_argument("--yes", action="store_true", help="非交互:不询问,只报告")
    ap.add_argument("--index-url", default=TSINGHUA, help=f"pip 源(默认 {TSINGHUA})")
    args = ap.parse_args()

    print("微信知识助手 · 一键配置")
    print(f"项目目录: {PROJECT}")

    step_env()
    try:
        step_deps(args.index_url)
    except Exception as e:
        _mark(False, f"依赖安装异常: {e}")
    try:
        step_config(args.yes)
    except Exception as e:
        _mark(False, f"配置步骤异常: {e}")
    try:
        step_models()
    except Exception as e:
        _mark(False, f"模型步骤异常: {e}")
    try:
        step_check()
    except Exception as e:
        _mark(False, f"自检异常: {e}")

    print("\n" + "=" * 64)
    if _state["fails"]:
        print(f"配置未完成,{len(_state['fails'])} 项待处理:")
        for f in _state["fails"]:
            print(f"  - {f}")
        print("处理后重新运行本脚本即可(已完成的步骤会自动跳过)。")
        return 1
    print("全部就绪!双击 启动助手.bat,或执行: python backend/server.py 8721")
    print("浏览器访问 http://127.0.0.1:8721")
    return 0


if __name__ == "__main__":
    sys.exit(main())
