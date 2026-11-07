# -*- coding: utf-8 -*-
"""状态隔离验收：在 backend/ 目录执行 `python scripts/smoke_state.py`。
用真实 wxagent/qqagent 模块跑一遍指令副作用，把两个 STORE 重定向到临时目录，
绝不改动用户真实状态文件；结束时会断言真实状态文件内容未变。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "app"))
sys.path.insert(0, str(ROOT / "refs" / "ChatTrace" / "src"))

import main  # noqa: E402,F401
import botcore  # noqa: E402
import qqagent  # noqa: E402
import qqbot  # noqa: E402
import wxagent  # noqa: E402

DATA = ROOT / "data"
real_before = {p: p.read_bytes() if p.exists() else None
               for p in (DATA / "wxagent_state.json", DATA / "qqagent_state.json")}

RESULTS = []


def ck(name, cond, extra=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)[:100]) if extra else ""))


with tempfile.TemporaryDirectory() as d:
    wxagent.STORE = botcore.Store(Path(d) / "wx.json")
    qqagent.STORE = botcore.Store(Path(d) / "qq.json")
    conf = qqbot.conf_of(main.load_conf())

    r = wxagent.command("4")
    ck("微信 /4 开启深度思考", wxagent.entity()["thinking"] is True, r[:60])
    ck("QQ 侧思考开关未被牵连", not qqagent.entity("TEST", conf).get("thinking"))

    r = qqagent.command("TEST", "7", conf)
    ck("QQ /7 暂停", qqagent.entity("TEST", conf)["paused"] is True, r[:60])
    ck("微信侧仍正常回答", wxagent.entity()["paused"] is False)

    paused_reply = qqagent.ask("TEST", "最近群里聊了什么", conf)
    ck("暂停时问答返回中文提示（不调模型）", "已暂停" in paused_reply, paused_reply)

    r = qqagent.command("TEST", "8", conf)
    ck("QQ /8 恢复", qqagent.entity("TEST", conf)["paused"] is False, r[:60])

    st = wxagent.status_text()
    ck("微信 /状态 含入口说明与当前模式", "微信 ClawBot" in st and "深度思考：开" in st,
       st.replace("\n", " / ")[:110])

    # 造一点上下文再 /6，验证清空
    s, ent = wxagent.STORE.entity("weixin-main", wxagent.conf())
    ent["session_id"] = 123
    ent["turns"] = [{"q": "a", "a": "b"}] * 3
    wxagent.STORE.save(s)
    r = wxagent.command("/6")
    ck("微信 /6 清上下文", not wxagent.entity().get("session_id")
       and not wxagent.entity().get("turns"), r[:60])

    r = wxagent.command("1")
    ck("微信 /1 帮助含现状段", "现在的状态" in r and "怎么用" in r, r[:50].replace("\n", " "))

    r = qqagent.help_text("TEST", conf)
    ck("QQ 帮助为 QQ 措辞且提示菜单", "QQ知识助手" in r and "菜单" in r and "微信里" not in r,
       r[:50].replace("\n", " "))

real_after = {p: p.read_bytes() if p.exists() else None
              for p in (DATA / "wxagent_state.json", DATA / "qqagent_state.json")}
ck("真实状态文件完全未被写入", real_before == real_after)

print(f"\nTOTAL {len(RESULTS)}  PASS {sum(RESULTS)}  FAIL {len(RESULTS)-sum(RESULTS)}")
sys.exit(0 if all(RESULTS) else 1)
