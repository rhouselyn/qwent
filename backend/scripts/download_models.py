# -*- coding: utf-8 -*-
"""模型权重与 ChatTrace 一键下载(开箱即用)。

下载内容:
  1. models/bge-small-zh          中文向量模型(约 91MB,优先 hf-mirror.com 镜像)
  2. models/para-zh               sherpa-onnx 中文语音识别(约 78MB,GitHub releases)
  3. refs/ChatTrace               媒体解密依赖 —— 已内置在仓库中;仅当缺失时才补 clone

用法:
  python backend/scripts/download_models.py           # 全部缺的都下载
  python backend/scripts/download_models.py --check   # 只看状态不下载
  python backend/scripts/download_models.py --bge     # 只下向量模型
  python backend/scripts/download_models.py --asr     # 只下语音模型
  python backend/scripts/download_models.py --chattrace

也可不手动跑:首次启动服务或首次用到语音转写时会自动触发对应下载。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
MODELS = PROJECT / "models"
BGE_DIR = MODELS / "bge-small-zh"
PARA_DIR = MODELS / "para-zh"
REFS = PROJECT / "refs"
CHATTRACE_DIR = REFS / "ChatTrace"

CHATTRACE_URL = "https://github.com/qiaodogbear/ChatTrace.git"
PARA_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2")

# bge-small-zh-v1.5 仓库里真正需要的文件(跳过 pytorch_model.bin 可省一半体积)
BGE_PATTERNS = [
    "config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "vocab.txt", "modules.json",
    "config_sentence_transformers.json", "sentence_bert_config.json",
    "1_Pooling/*",
]


def _hf_download(repo_id: str, local_dir: Path, patterns: list[str] | None) -> None:
    """huggingface_hub 下载,先走 hf-mirror.com(国内可达),失败回退官方源。"""
    from huggingface_hub import constants as hf_const
    from huggingface_hub import snapshot_download
    last: Exception | None = None
    for endpoint in ("https://hf-mirror.com", "https://huggingface.co"):
        try:
            os.environ["HF_ENDPOINT"] = endpoint
            hf_const.ENDPOINT = endpoint     # 模块已导入时也强制生效
            print(f"[bge] 从 {endpoint} 下载 {repo_id} ...")
            kwargs = dict(repo_id=repo_id, local_dir=str(local_dir))
            if patterns:
                kwargs["allow_patterns"] = patterns
            snapshot_download(**kwargs)
            return
        except Exception as e:
            last = e
            print(f"[bge] 该源失败: {e}")
    raise RuntimeError(f"所有下载源均失败: {last};请检查网络/代理后重试")


def bge_ready() -> bool:
    has_weights = ((BGE_DIR / "model.safetensors").exists()
                   or (BGE_DIR / "pytorch_model.bin").exists())
    has_tokenizer = (BGE_DIR / "tokenizer.json").exists() or (BGE_DIR / "vocab.txt").exists()
    return (BGE_DIR / "config.json").exists() and has_weights and has_tokenizer


def ensure_bge() -> Path:
    """向量模型;已存在则直接返回,缺失则自动下载。"""
    if bge_ready():
        return BGE_DIR
    BGE_DIR.mkdir(parents=True, exist_ok=True)
    _hf_download("BAAI/bge-small-zh-v1.5", BGE_DIR, BGE_PATTERNS)
    if not bge_ready():                      # 仓库结构变化时退回全量下载
        print("[bge] 关键文件缺失,改为全量下载 ...")
        _hf_download("BAAI/bge-small-zh-v1.5", BGE_DIR, None)
    if not bge_ready():
        raise RuntimeError(f"下载完成但 {BGE_DIR} 仍不完整")
    print(f"[bge] 完成 -> {BGE_DIR}")
    return BGE_DIR


def _download(url: str, dest: Path) -> None:
    import requests
    print(f"[asr] 下载 {url}")
    with requests.get(url, stream=True, timeout=60, allow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r[asr] {done / 1e6:.0f}/{total / 1e6:.0f} MB", end="")
    print()


def para_ready() -> bool:
    if not PARA_DIR.is_dir():
        return False
    files = os.listdir(PARA_DIR)
    return any(f.endswith(".onnx") for f in files) and "tokens.txt" in files


def ensure_para() -> Path:
    """sherpa-onnx paraformer 中文小模型;已存在则直接返回。"""
    if para_ready():
        return PARA_DIR
    PARA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PARA_DIR.parent / "_para_download.tar.bz2"
    try:
        try:
            _download(PARA_URL, tmp)
        except Exception as e:
            print(f"[asr] 直连失败({e}),尝试 ghproxy 镜像 ...")
            _download("https://ghproxy.net/" + PARA_URL, tmp)
        print(f"[asr] 解压到 {PARA_DIR} ...")
        with tarfile.open(tmp, "r:bz2") as tf:
            tf.extractall(MODELS)                # 顶层目录 sherpa-onnx-paraformer-zh-small-...
        top = next(MODELS.glob("sherpa-onnx-paraformer*"))
        for f in top.iterdir():
            if f.is_file() and not (PARA_DIR / f.name).exists():
                shutil.move(str(f), str(PARA_DIR / f.name))
        top.rmdir()
    finally:
        tmp.unlink(missing_ok=True)
    if not para_ready():
        raise RuntimeError(f"下载/解压后 {PARA_DIR} 仍缺少 onnx 或 tokens.txt,请手动下载: {PARA_URL}")
    print(f"[asr] 完成 -> {PARA_DIR}")
    return PARA_DIR


def chattrace_ready() -> bool:
    return (CHATTRACE_DIR / "src").is_dir()


def ensure_chattrace() -> Path:
    """媒体解密依赖 ChatTrace(公开 MIT 仓库);已存在则直接返回。"""
    if chattrace_ready():
        return CHATTRACE_DIR
    REFS.mkdir(parents=True, exist_ok=True)
    print(f"[chattrace] git clone {CHATTRACE_URL}")
    try:
        subprocess.run(["git", "clone", "--depth", "1", CHATTRACE_URL,
                        str(CHATTRACE_DIR)], check=True)
    except Exception as e:
        print(f"[chattrace] clone 失败: {e}\n"
              f"            可手动: git clone {CHATTRACE_URL} refs/ChatTrace\n"
              f"            (没有它仅影响图片/.dat 媒体解码,其余功能正常)")
        return CHATTRACE_DIR
    print(f"[chattrace] 完成 -> {CHATTRACE_DIR}")
    return CHATTRACE_DIR


def status() -> dict[str, bool]:
    return {
        "bge(向量模型)": bge_ready(),
        "para-zh(语音识别)": para_ready(),
        "ChatTrace(媒体解码)": chattrace_ready(),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="只显示状态,不下载")
    ap.add_argument("--bge", action="store_true", help="只处理向量模型")
    ap.add_argument("--asr", action="store_true", help="只处理语音模型")
    ap.add_argument("--chattrace", action="store_true", help="只处理 ChatTrace")
    args = ap.parse_args()

    if args.check:
        for k, ok in status().items():
            print(f"{'[OK] ' if ok else '[缺] '}{k}")
        return

    jobs = []
    if args.bge or args.asr or args.chattrace:
        if args.bge:
            jobs.append(ensure_bge)
        if args.asr:
            jobs.append(ensure_para)
        if args.chattrace:
            jobs.append(ensure_chattrace)
    else:
        jobs = [ensure_bge, ensure_para, ensure_chattrace]
    for job in jobs:
        try:
            job()
        except Exception as e:
            print(f"[!] {job.__name__} 失败: {e}")


if __name__ == "__main__":
    sys.exit(main())
