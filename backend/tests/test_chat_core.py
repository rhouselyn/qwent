# -*- coding: utf-8 -*-
"""共用内核回归测试：分页/排序/分类/置顶/定位/SSE 只有一份实现，
微信与 QQ 两个 Source 都必须走它。纯内存 sqlite，不连真实知识库。

运行（用项目自带的 Anaconda Python，需 fastapi）：
    C:/Users/14815/anaconda3/python.exe -m unittest discover -s backend/tests
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

APP = Path(__file__).resolve().parent.parent / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import chat_core  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class TestSharedHelpers(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(chat_core.clamp(0, 1, 500), 1)
        self.assertEqual(chat_core.clamp(99999, 1, 500), 500)
        self.assertEqual(chat_core.clamp("30", 1, 500), 30)
        self.assertEqual(chat_core.clamp("bad", 1, 500), 1)
        self.assertEqual(chat_core.clamp(None, 0, 10**9), 0)

    def test_since_ts_and_paging(self):
        self.assertEqual(chat_core.since_ts(7, now=1_000_000), 1_000_000 - 7 * 86400)
        self.assertEqual(chat_core.since_ts(0, now=1_000_000), 1_000_000)
        items = [1, 2, 3, 4, 5]
        self.assertEqual(chat_core.slice_items(items, 1, 2), [2, 3])

    def test_sort_sql_is_whitelisted(self):
        mapping = {"old": "ts ASC", "size": "size DESC"}
        self.assertEqual(chat_core.sort_sql("old", mapping, "ts DESC"), "ts ASC")
        self.assertEqual(chat_core.sort_sql("'; DROP TABLE", mapping, "ts DESC"),
                         "ts DESC")
        rows = [{"ts": 3, "size": 1}, {"ts": 1, "size": 9}]
        self.assertEqual([r["ts"] for r in chat_core.sort_items(rows, "old")], [1, 3])
        self.assertEqual([r["size"] for r in chat_core.sort_items(rows, "size")], [9, 1])

    def test_file_kind_both_taxonomies(self):
        # 微信按用途分类，QQ 按扩展名分类；判定逻辑同一个
        self.assertEqual(chat_core.file_kind("a.pdf", chat_core.WECHAT_KINDS), "doc")
        self.assertEqual(chat_core.file_kind("a.xlsx", chat_core.WECHAT_KINDS), "sheet")
        self.assertEqual(chat_core.file_kind("a.xyz", chat_core.WECHAT_KINDS), "other")
        self.assertEqual(chat_core.file_kind("a.pdf", chat_core.QQ_KINDS, "file"), "pdf")
        self.assertEqual(chat_core.file_kind("a.mp4", chat_core.QQ_KINDS, "file"), "video")
        self.assertEqual(chat_core.file_kind("a.xyz", chat_core.QQ_KINDS, "file"), "file")

    def test_set_pinned_uses_prepare_and_commits(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE chat (username TEXT)")   # 故意不带 pinned
        con.execute("INSERT INTO chat VALUES ('a')")

        def prepare(c):
            cols = {r[1] for r in c.execute("PRAGMA table_info(chat)")}
            if "pinned" not in cols:
                c.execute("ALTER TABLE chat ADD COLUMN pinned INTEGER DEFAULT 0")

        out = chat_core.set_pinned(con, "chat", "username", "a", True, prepare)
        self.assertEqual(out, {"ok": True, "username": "a", "pinned": 1})
        self.assertEqual(con.execute("SELECT pinned FROM chat").fetchone()[0], 1)
        self.assertFalse(con.in_transaction)          # 已提交，不停在事务里
        chat_core.set_pinned(con, "chat", "username", "a", False, prepare)
        self.assertEqual(con.execute("SELECT pinned FROM chat").fetchone()[0], 0)
        con.close()

    def test_reveal_missing_and_found(self):
        with self.assertRaises(HTTPException) as ctx:
            chat_core.reveal_path("/definitely/missing/file.pdf")
        self.assertEqual(ctx.exception.status_code, 404)
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            p = f.name
        try:
            with patch.object(chat_core.subprocess, "Popen") as popen:
                self.assertEqual(chat_core.reveal_path(p), {"ok": True})
                popen.assert_called_once()
        finally:
            Path(p).unlink(missing_ok=True)


class TestSources(unittest.TestCase):
    def test_subclasses_declare_only_differences(self):
        wx, qq = chat_core.WeChatSource(), chat_core.QQLocalSource()
        self.assertEqual((wx.platform, wx.chat_table, wx.chat_key),
                         ("wechat", "chat", "username"))
        self.assertEqual((qq.platform, qq.chat_table, qq.chat_key),
                         ("qq", "qq_chat", "conv"))
        self.assertEqual(wx.file_kind("a.pdf"), "doc")
        self.assertEqual(qq.file_kind("a.pdf"), "pdf")
        self.assertNotEqual(wx.sse_idle, qq.sse_idle)

    def test_source_pin_delegates_to_shared_code(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE qq_chat (conv TEXT, pinned INTEGER DEFAULT 0)")
        con.execute("INSERT INTO qq_chat VALUES ('c2c:1', 0)")
        out = chat_core.QQLocalSource().pin(con, "c2c:1", True)
        self.assertEqual(out, {"ok": True, "conv": "c2c:1", "pinned": 1})
        con.close()

    def test_sse_frames_are_identical_for_both_platforms(self):
        calls = []

        def fetch(last, limit):
            calls.append(last)
            if len(calls) == 1:
                return [{"id": 7, "text": "hi"}], 7
            return [], last

        frames = []
        for source in (chat_core.WeChatSource(), chat_core.QQLocalSource()):
            calls.clear()
            self.assertTrue(hasattr(source.sse(0, fetch), "body_iterator"))
            g = chat_core.sse_events(fetch, 0, poll=0, idle_limit=2, batch=40)
            frames.append([next(g), next(g), next(g)])
        self.assertEqual(frames[0], frames[1])            # 两个平台同一份帧格式
        self.assertEqual(frames[0][0], "retry: 3000\n\n")
        self.assertEqual(json.loads(frames[0][1][6:].strip()),
                         {"messages": [{"id": 7, "text": "hi"}], "max_id": 7})
        self.assertEqual(calls, [0, 7, 7])               # 水位推进，不重查旧行

    def test_sse_version_change_emits_chats_event(self):
        state = {"v": 1}

        def fetch(last, limit):
            return [], last

        g = chat_core.sse_events(fetch, 0, version=lambda: state["v"],
                                 poll=0, idle_limit=2)
        self.assertEqual(next(g), "retry: 3000\n\n")
        state["v"] = 2
        frame = next(g)
        self.assertEqual(json.loads(frame[6:].strip()), {"chats": True})


if __name__ == "__main__":
    unittest.main()
