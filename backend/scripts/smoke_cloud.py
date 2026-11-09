# -*- coding: utf-8 -*-
"""云端服务验收（服务已启动时运行）。

    python backend/scripts/smoke_cloud.py --url http://127.0.0.1:8721
    # 在服务器上：cd /opt/qwent && .venv/bin/python backend/scripts/smoke_cloud.py

只做只读探测，不写库。API Key 从 --key 或环境变量 QWENT_AGENT_KEY 读取。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 120


def req(url, method="GET", body=None, key=""):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8721")
    ap.add_argument("--key", default=os.environ.get("QWENT_AGENT_KEY", ""))
    args = ap.parse_args()
    base = args.url.rstrip("/")
    results = []

    def check(name, fn):
        try:
            detail = fn()
            results.append((name, True, detail))
            print(f"[OK]   {name}: {detail}")
        except Exception as exc:      # noqa: BLE001
            results.append((name, False, str(exc)[:200]))
            print(f"[FAIL] {name}: {str(exc)[:200]}")

    def health():
        code, d = req(base + "/cloud/v1/health")
        assert code == 200 and d.get("ok"), d
        assert d.get("is_cloud") is True, "不是云端模式（QWENT_CLOUD 未生效）"
        return f"side={d.get('side')} applied_seq={d.get('applied_seq')}"

    def status():
        _, d = req(base + "/api/status")
        assert int(d.get("messages") or 0) > 0, "知识库为空，基线可能未恢复"
        return f"messages={d['messages']} chats={d['chats']} contacts={d['contacts']}"

    def models():
        code, d = req(base + "/v1/models", key=args.key)
        assert code == 200, d
        return "models=" + ",".join(x["id"] for x in d.get("data", []))

    def command():
        code, d = req(base + "/v1/chat/completions", "POST",
                      {"model": "wechat-kb", "stream": False,
                       "messages": [{"role": "user", "content": "/状态"}]},
                      key=args.key)
        assert code == 200, d
        text = d["choices"][0]["message"]["content"]
        assert len(text.strip()) > 20, f"回答过短：{text!r}"
        assert "云端" in text or "记录" in text or "模型" in text, text[:200]
        return "回答 " + str(len(text)) + " 字：" + text[:60].replace("\n", " ")

    check("健康检查", health)
    check("知识库状态", status)
    check("模型列表", models)
    check("微信通道指令问答", command)
    bad = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} 项通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
