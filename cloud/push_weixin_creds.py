# -*- coding: utf-8 -*-
"""把本机扫码得到的微信渠道凭据一键推到服务器（换电脑 / 凭据失效时用）。

官方 openclaw-weixin 只有扫码登录一种方式；扫码后凭据以文件形式落在
``~/.openclaw/openclaw-weixin/``。本脚本把该目录打包上传，备份服务器旧凭据，
解压后重启 Gateway，让服务器**免扫码**重新上线。

用法（在项目根目录执行）：

    python cloud/push_weixin_creds.py                    # dry-run，只看会做什么
    python cloud/push_weixin_creds.py --apply            # 备份 + 覆盖 + 重启 + 探测
    python cloud/push_weixin_creds.py --apply --no-restart   # 只推凭据，不重启

安全约定：
* 默认 dry-run，不写服务器；``--apply`` 才执行。
* 覆盖前把服务器原目录改名为 ``openclaw-weixin.bak-<时间戳>``，可随时回滚。
* 只打印文件名与大小，**不打印任何 token / 凭据内容**。
* 同一微信账号不能两端同时在线：推送完成后请保持本机微信通道关闭
  （本地 ``cloud.enabled=true`` 时 Qwent 启动会自动停本机 Gateway）。
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SRC = Path.home() / ".openclaw" / "openclaw-weixin"
REMOTE_DIR = "/root/.openclaw/openclaw-weixin"
SKIP_SUFFIX = ("-wal", "-shm", ".tmp", ".log", ".lock")


def _ssh_conf() -> tuple[list[str], str]:
    conf = json.loads((PROJECT / "backend" / "config.json").read_text(encoding="utf-8"))
    c = conf.get("cloud") or {}
    host = str(c.get("endpoint") or "").strip()
    user = str(c.get("scp_user") or "root").strip()
    key = str(c.get("scp_key") or "").strip()
    if not host or not key:
        raise SystemExit("backend/config.json 的 cloud.endpoint / cloud.scp_key 未配置")
    base = ["ssh", "-i", key, "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new", f"{user}@{host}"]
    return base, host


def _pack() -> tuple[bytes, list[str]]:
    """把凭据目录打成 tar.gz，排除 SQLite 临时文件和日志。"""
    if not SRC.is_dir():
        raise SystemExit(f"本机没有微信凭据目录：{SRC}\n"
                         "请先在本地扫码登录：openclaw channels login --channel openclaw-weixin")
    if not (SRC / "accounts.json").is_file():
        raise SystemExit(f"凭据不完整：缺少 {SRC / 'accounts.json'}")
    buf = io.BytesIO()
    names: list[str] = []
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in sorted(SRC.rglob("*")):
            if not p.is_file() or p.name.endswith(SKIP_SUFFIX):
                continue
            rel = p.relative_to(SRC).as_posix()
            names.append(rel)
            tf.add(p, arcname=rel, recursive=False)
    return buf.getvalue(), names


def main() -> int:
    ap = argparse.ArgumentParser(description="推送本机微信渠道凭据到服务器")
    ap.add_argument("--apply", action="store_true", help="实际执行（默认 dry-run）")
    ap.add_argument("--no-restart", action="store_true", help="不重启 OpenClaw Gateway")
    args = ap.parse_args()

    ssh, host = _ssh_conf()
    blob, names = _pack()
    accounts = [n for n in names if n.startswith("accounts/")]
    print(f"目标服务器 : {host}:{REMOTE_DIR}")
    print(f"本机凭据   : {SRC}")
    print(f"文件数     : {len(names)}（账号态文件 {len(accounts)} 个）")
    print(f"打包大小   : {len(blob):,} bytes")
    for n in names:
        print("  -", n)
    if not args.apply:
        print("\n[dry-run] 未写入服务器。确认无误后加 --apply 执行。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    tmp = f"/tmp/qwent-weixin-creds-{stamp}.tar.gz"
    script = f"""
set -e
mkdir -p /root/.openclaw
if [ -d "{REMOTE_DIR}" ]; then
  mv "{REMOTE_DIR}" "{REMOTE_DIR}.bak-{stamp}"
  echo "BACKUP: {REMOTE_DIR}.bak-{stamp}"
fi
mkdir -p "{REMOTE_DIR}"
tar xzf {tmp} -C "{REMOTE_DIR}"
find "{REMOTE_DIR}" -type f \\( -name '*-wal' -o -name '*-shm' \\) -delete
rm -f {tmp}
echo "FILES: $(find "{REMOTE_DIR}" -type f | wc -l)"
""".strip()
    # 第一步：通过 stdin 传打包内容到服务器临时文件
    up = subprocess.run(ssh + [f"cat > {tmp}"], input=blob, capture_output=True)
    if up.returncode:
        sys.stderr.write(up.stderr.decode("utf-8", "ignore")[-800:])
        raise SystemExit("上传凭据失败")
    # 第二步：备份 + 解压
    run = subprocess.run(ssh + ["bash -s"], input=script.encode(), capture_output=True)
    out = run.stdout.decode("utf-8", "ignore").strip()
    err = run.stderr.decode("utf-8", "ignore").strip()
    print("\n=== 推送结果 ===")
    if out:
        print(out)
    if run.returncode:
        sys.stderr.write(err[-800:] + "\n")
        raise SystemExit("替换凭据失败，服务器旧目录已保留为 .bak-*")
    if args.no_restart:
        print("已跳过 Gateway 重启。")
        return 0
    # 第三步：重启并探测
    for label, cmd in (("重启 Gateway", "openclaw gateway restart"),
                       ("渠道探测", "openclaw channels status --probe")):
        p = subprocess.run(ssh + [cmd], capture_output=True)
        text = (p.stdout.decode("utf-8", "ignore") + p.stderr.decode("utf-8", "ignore"))
        print(f"\n=== {label} ===")
        print("\n".join(text.strip().splitlines()[-12:]))
    print("\n请确认输出里有 openclaw-weixin <账号>: running；"
          "并保持本机微信通道关闭，避免两端互踢。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
