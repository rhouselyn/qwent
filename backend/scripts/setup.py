# -*- coding: utf-8 -*-
"""一键配置:从零克隆到可运行的全部准备步骤,带分步进度。

用法:
  python backend/scripts/setup.py                 # 交互式(缺 key 时询问;无知识库时选择恢复备份或自动建库)
  python backend/scripts/setup.py --yes           # 非交互,只报告不询问
  python backend/scripts/setup.py --index-url URL # 自定义 pip 源(默认清华源)

步骤:
  [1/6] Python 环境     版本 ≥ 3.11
  [2/6] 依赖安装        pip install -r requirements.txt
  [3/6] 本机配置        生成 config.json,缺失 API key 时询问填写
  [4/6] 知识库          已存在→跳过;缺失→恢复备份包 或 从本机微信自动建库(解密→导入)
  [5/6] 模型与组件      bge 向量模型 / paraformer 语音 / ChatTrace(内置) + 向量索引检查
  [6/6] 就绪自检        doctor.py 全项检查,全 ✓ 即可直接启动

脚本可重复运行:已完成的步骤自动跳过。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
PROJECT = BACKEND.parent
sys.path.insert(0, str(HERE))

TSINGHUA = "https://pypi.tuna.tsinghua.edu.cn/simple"
_state = {"n": 0, "total": 6, "fails": []}


def step(title: str) -> None:
    _state["n"] += 1
    bar = "=" * 64
    print(f"\n{bar}\n[{_state['n']}/{_state['total']}] {title}\n{bar}")


def _mark(ok: bool, label: str) -> None:
    if not ok:
        _state["fails"].append(label)
    print(f"{'[OK] ' if ok else '[失败]'} {label}")


def _chattrace(argv: list[str]) -> subprocess.CompletedProcess:
    """以流式输出调用 ChatTrace CLI(解密/抓密钥),进度实时可见。"""
    src = PROJECT / "refs" / "ChatTrace" / "src"
    code = ("import sys; sys.path.insert(0, r'%s'); "
            "sys.argv=['chattrace']+%r; "
            "from chattrace.cli import main; sys.exit(main() or 0)" % (src, argv))
    return subprocess.run([sys.executable, "-c", code])


def _vec_coverage() -> tuple[int, int]:
    db = PROJECT / "data" / "knowledge.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        total = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        # msg_vec_p 是 vec0 虚表,未加载扩展时不能直接 COUNT,数其 rowids 影子表
        try:
            nvec = con.execute("SELECT COUNT(*) FROM msg_vec_p_rowids").fetchone()[0]
        except sqlite3.OperationalError:
            nvec = 0
    finally:
        con.close()
    return nvec, total


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


def _stored_key_validates() -> bool:
    """实测存量密钥是否仍能打开消息库（HMAC 校验，不看 24h 时间戳——密钥账号级稳定）。"""
    try:
        sys.path.insert(0, str(HERE.parent / "refs" / "ChatTrace" / "src"))
        sys.path.insert(0, str(HERE.parent / "app"))
        from chattrace.keyagent import keystore
        from chattrace.keyagent.account import resolve_account
        from chattrace.keyagent.verify import read_db_header, verify_hmac_sweep
        account = resolve_account(Path(_account()[0]))
        info = keystore.load_key(account.account_id)
        ok, _ = verify_hmac_sweep(info.password, read_db_header(account.message_db))
        return ok
    except Exception:
        return False


def _memory_scan_key() -> bool:
    """微信运行中直接扫进程内存取钥（免退出）；失败返回 False 走交互流程。"""
    try:
        sys.path.insert(0, str(HERE.parent / "app"))
        import keyscan
        acc, _ = _account()
        r = keyscan.capture_and_store(Path(acc),
                                      Path(acc) / "db_storage" / "message" / "message_0.db")
        return bool(r.get("ok"))
    except Exception:
        return False


def keyvault_manual_prompt() -> bool:
    """非 Windows：交互读取用户粘贴的 hex 密钥并导入。"""
    try:
        sys.path.insert(0, str(HERE.parent / "app"))
        import keyvault
        acc, _ = _account()
        k = input("  粘贴 64 位 hex 密钥后回车: ").strip()
        if not k:
            return False
        r = keyvault.import_manual_key(Path(acc), k)
        if r.get("ok"):
            print(f"  {r['detail']}")
            return True
        print(f"  导入失败: {r['detail']}")
        return False
    except Exception as exc:
        print(f"  导入异常: {exc}")
        return False


def _ensure_key() -> bool:
    """确保数据库密钥可用：HMAC 实测通过直接用 → 失效先试内存扫描（免退出）→ 交互抓取。"""
    if os.name != "nt":
        print("  macOS/Linux：无自动抓钥方案，请手动导入一次密钥（密钥账号级稳定，导一次长期有效）：")
        print("    python backend/app/keyvault.py --account-dir <微信账号目录> --import <64位hex密钥>")
        print("  （密钥可用任意取钥工具获取；导入前会自动做 HMAC 校验）")
        r = keyvault_manual_prompt()
        if r:
            _mark(True, "密钥导入成功（HMAC 校验通过）")
            return True
        _mark(False, "密钥未导入 — 可稍后手动执行上面的命令后重跑")
        return False
    print("  检查数据库密钥 ...")
    if _stored_key_validates():
        _mark(True, "存量密钥实测有效（HMAC 通过），无需重新抓取")
        return True
    print("  存量密钥无法打开消息库（或不存在），尝试从运行中的微信内存扫描（约 1-2 分钟）...")
    if _memory_scan_key():
        _mark(True, "已从运行中的微信内存取得新密钥，微信无需退出")
        return True
    print("  内存扫描未命中（微信未运行或库早已打开时会这样），回退交互式抓取：")
    input("  请先完全退出微信(托盘右键退出),然后回车继续 ...")
    print("  正在启动微信并捕获密钥,微信窗口会自动弹出,请勿操作 ...")
    if _chattrace(["keyagent", "run", "--account-dir",
                   str(_account()[0]), "--store"]).returncode != 0:
        _mark(False, "密钥捕获失败 — 请按项目文档.md 手动执行 ChatTrace keyagent")
        return False
    return True


def _account():
    from paths import ACCOUNT_DIR, DECRYPTED_DIR
    return ACCOUNT_DIR, DECRYPTED_DIR


def _build_from_wechat() -> bool:
    """从本机微信全自动建库:解密 → 导入。成功返回 True。"""
    try:
        ACCOUNT_DIR, DECRYPTED_DIR = _account()
    except SystemExit as e:
        _mark(False, str(e))
        return False
    print(f"  账号目录: {ACCOUNT_DIR}")

    print("  [1/2] 增量解密微信本地库(首次约几分钟,请勿关闭窗口)...")
    r = _chattrace(["data", "decrypt", "--account-dir", str(ACCOUNT_DIR)])
    if r.returncode != 0 or not (DECRYPTED_DIR / "message").is_dir():
        if not _ensure_key():
            return False
        print("  [1/2] 重新增量解密 ...")
        if _chattrace(["data", "decrypt", "--account-dir",
                       str(ACCOUNT_DIR)]).returncode != 0:
            _mark(False, "解密失败 — 请检查微信是否已登录过本机")
            return False
    _mark(True, "解密完成")

    print("  [2/2] 解析消息导入 knowledge.db(几十万条约几分钟)...")
    r = subprocess.run([sys.executable, "m0_import.py",
                        "--decrypted-dir", str(DECRYPTED_DIR),
                        "--out", str(PROJECT / "data" / "knowledge.db")],
                       cwd=str(HERE))
    if r.returncode != 0:
        _mark(False, "导入失败 — 重跑本脚本可断点续传")
        return False
    _mark(True, "建库完成(向量索引在下一步处理)")
    # 新人首批自动解析开关:服务启动后按顺序后台执行
    # (文字/语音/文件=全量历史,图片/公众号/朋友圈=最近一年;其余设置页续跑)
    try:
        kdb = sqlite3.connect(str(PROJECT / "data" / "knowledge.db"))
        kdb.execute("INSERT OR REPLACE INTO meta VALUES('auto_backlog', '1')")
        kdb.commit()
        kdb.close()
        print("  已登记自动后台解析:语音/文件(全量) + 图片/公众号/朋友圈(最近一年),"
              "启动服务后自动进行;其余可在设置页随时继续")
    except Exception:
        pass
    return True


def step_data(yes: bool) -> None:
    step("知识库(data/knowledge.db)")
    if (PROJECT / "data" / "knowledge.db").exists():
        _mark(True, "知识库已存在")
        return
    if yes:
        _mark(False, "知识库不存在 — 交互模式可选择自动建库,或用 backup_data.py --restore 恢复备份")
        return

    print("未检测到知识库(你的历史聊天记录)。选择获取方式:")
    print("  1) 恢复旧电脑的备份包(backup_data.py 生成的 zip)")
    print("  2) 从本机微信自动建库(解密 → 导入,全程自动)")
    print("  3) 跳过,稍后处理")
    c = input("输入 1/2/3 [默认 2]: ").strip() or "2"
    if c == "1":
        p = input("备份包 zip 路径(可直接拖入窗口): ").strip().strip('"').strip("'")
        if not p:
            _mark(False, "未提供备份包路径")
            return
        try:
            from backup_data import restore
            restore(Path(p), yes=True)
        except SystemExit as e:
            _mark(False, f"恢复失败: {e}")
    elif c == "2":
        _build_from_wechat()
    else:
        _mark(False, "已跳过 — 启动前请重跑本脚本完成建库")


def step_models(yes: bool) -> None:
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

    # 向量索引:只报告,不阻塞(服务运行后可在设置页后台续跑)
    db = PROJECT / "data" / "knowledge.db"
    if db.exists():
        try:
            nvec, total = _vec_coverage()
            if nvec >= total * 0.9:
                _mark(True, f"向量索引覆盖 {nvec:,}/{total:,}")
            elif yes:
                print(f"[!] 向量索引覆盖 {nvec:,}/{total:,} — "
                      f"服务运行后在设置页「索引任务」后台续跑,或 python backend/scripts/build_vectors.py")
            else:
                ans = input(f"语义向量尚未构建完成({nvec:,}/{total:,}),"
                            f"现在构建吗?消息量大时耗时较长 [Y/n]: ").strip().lower()
                if ans in ("", "y", "yes"):
                    subprocess.run([sys.executable, "build_vectors.py"], cwd=str(HERE))
                else:
                    print("[!] 跳过 — 服务运行后在设置页「索引任务」后台续跑")
        except Exception as e:
            print(f"[!] 向量索引检查失败: {e}")


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
        step_data(args.yes)
    except Exception as e:
        _mark(False, f"知识库步骤异常: {e}")
    try:
        step_models(args.yes)
    except Exception as e:
        _mark(False, f"模型步骤异常: {e}")
    try:
        # 朋友圈 CDN 解密组件(可选,失败不影响其它功能)
        r = subprocess.run([sys.executable, "setup_sns_wasm.py"], cwd=str(HERE),
                           capture_output=True, text=True, timeout=180)
        tail = (r.stdout or r.stderr or "").strip().splitlines()
        _mark(r.returncode == 0, "朋友圈媒体组件 " + (tail[-1] if tail else ""))
    except Exception as e:
        _mark(False, f"朋友圈媒体组件: {e}")
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
