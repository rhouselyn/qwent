# -*- coding: utf-8 -*-
"""data/ 目录备份与恢复 —— 换电脑 / 新目录的一比一迁移。

data/ 里的 knowledge.db(3.7GB)是解密后的私人聊天记录,既不能也不应上传 GitHub;
要在另一台电脑/新目录获得与当前完全一致的环境,用它打一个备份包,恢复时解回即可。

用法:
  python backend/scripts/backup_data.py D:\\backup\\wechat_data.zip        # 备份
  python backend/scripts/backup_data.py --dry-run                          # 只看会打包什么
  python backend/scripts/backup_data.py --no-media D:\\backup\\small.zip   # 跳过媒体缓存(62MB)
  python backend/scripts/backup_data.py --restore D:\\backup\\wechat_data.zip  # 恢复(覆盖同名)

说明:
  - knowledge.db 用 SQLite backup API 做一致性快照,服务运行中也可备份
  - 备份内容:knowledge.db + all_keys.json(库密钥)+ media_cache;不含日志/临时库
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
DATA = PROJECT / "data"
DB = DATA / "knowledge.db"


def _human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def collect(with_media: bool) -> list[Path]:
    # knowledge.db 不在此列:它经 SQLite backup API 快照后单独写入
    files: list[Path] = [DATA / "all_keys.json"]
    if with_media:
        files += list((DATA / "media_cache").rglob("*"))
    return [p for p in files if p.is_file()]


def dry_run(with_media: bool) -> None:
    print(f"备份源: {DATA}")
    extra = collect(with_media)
    total = sum(p.stat().st_size for p in extra) + (DB.stat().st_size if DB.exists() else 0)
    print(f"  knowledge.db          {_human(DB.stat().st_size) if DB.exists() else '缺失!'}")
    for p in extra[:5]:
        print(f"  {p.relative_to(DATA)}  {_human(p.stat().st_size)}")
    if len(extra) > 5:
        print(f"  ... 另有 {len(extra) - 5} 个 media_cache 文件")
    print(f"合计约 {_human(total)}(sqlite 压缩后通常更小)")


def backup(dest: Path, with_media: bool) -> None:
    if not DB.exists():
        raise SystemExit(f"[!] 未找到 {DB},请先跑解密入库管线")
    dest.parent.mkdir(parents=True, exist_ok=True)
    print("[1/2] 生成 knowledge.db 一致性快照 ...")
    tmp = Path(tempfile.mktemp(prefix="knowledge_snapshot_", suffix=".db", dir=str(DATA)))
    try:
        src = sqlite3.connect(str(DB))
        dst = sqlite3.connect(str(tmp))
        with dst:
            src.backup(dst)
        dst.close()
        src.close()

        print(f"[2/2] 写入 {dest} ...")
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp, "knowledge.db")
            for p in collect(with_media):
                arc = p.relative_to(DATA)
                print(f"  + {arc}  {_human(p.stat().st_size)}")
                z.write(p, str(arc))
    finally:
        tmp.unlink(missing_ok=True)
    print(f"完成: {dest}  {_human(dest.stat().st_size)}")


def restore(src: Path, yes: bool) -> None:
    if not src.exists():
        raise SystemExit(f"[!] 备份包不存在: {src}")
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        print(f"备份包含 {len(names)} 个文件:")
        for n in names[:8]:
            print(f"  {n}")
        if len(names) > 8:
            print(f"  ... 另有 {len(names) - 8} 个")
        if not yes:
            ans = input(f"将覆盖恢复到 {DATA},同名文件会被替换。继续? [y/N] ")
            if ans.strip().lower() != "y":
                print("已取消")
                return
        DATA.mkdir(parents=True, exist_ok=True)
        z.extractall(str(DATA))
    print(f"完成:data/ 已恢复为备份时状态。之后 python backend/scripts/doctor.py 验证。")


def main() -> None:
    ap = argparse.ArgumentParser(description="data 目录备份/恢复(一比一迁移)")
    ap.add_argument("dest", nargs="?", help="备份输出 .zip 路径")
    ap.add_argument("--restore", metavar="SRC", help="从备份包恢复到 data/")
    ap.add_argument("--no-media", action="store_true", help="跳过 media_cache 媒体缓存")
    ap.add_argument("--dry-run", action="store_true", help="只显示将打包的内容")
    ap.add_argument("--yes", action="store_true", help="恢复时跳过确认")
    a = ap.parse_args()

    if a.restore:
        restore(Path(a.restore), a.yes)
    elif a.dry_run:
        dry_run(not a.no_media)
    elif a.dest:
        backup(Path(a.dest), not a.no_media)
    else:
        ap.print_help()


if __name__ == "__main__":
    sys.exit(main())
