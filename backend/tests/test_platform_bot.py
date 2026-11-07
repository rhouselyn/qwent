# -*- coding: utf-8 -*-
"""平台文案 / 双通道指令 / QQ 消息留存的回归测试（纯标准库，不连真实知识库）。

运行：python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import botcore  # noqa: E402
import platform_text  # noqa: E402
import qq_messages  # noqa: E402
import qq_local  # noqa: E402


class TestSystemText(unittest.TestCase):
    def test_group_rename(self):
        wx, qq = platform_text.system_pair('"张三"修改群名为"产品讨论群"')
        self.assertEqual(wx, '"张三"修改群名为"产品讨论群"')
        self.assertEqual(qq, "张三修改了群名称：产品讨论群")

    def test_invite(self):
        wx, qq = platform_text.system_pair('你邀请"李四"加入了群聊')
        self.assertIn("加入了群聊", qq)
        self.assertNotIn('"', qq)
        self.assertEqual(wx, '你邀请"李四"加入了群聊')

    def test_recall_and_pat(self):
        _, qq = platform_text.system_pair('"王五"撤回了一条消息')
        self.assertEqual(qq, "王五撤回了一条消息")
        _, qq2 = platform_text.system_pair('"A"拍了拍"B"')
        self.assertEqual(qq2, "A戳了戳B")

    def test_add_friend(self):
        _, qq = platform_text.system_pair('你已添加了"小明"，现在可以开始聊天了')
        self.assertIn("好友", qq)

    def test_sysmsg_xml_template(self):
        raw = ('<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
               '<content_template><plain><![CDATA["张三"邀请"李四"加入了群聊]]></plain>'
               '</content_template></sysmsgtemplate></sysmsg>')
        wx, qq = platform_text.system_pair(raw)
        self.assertIn("加入了群聊", wx)
        self.assertNotIn("sysmsgtemplate", wx)
        self.assertIn("加入了群聊", qq)

    def test_recall_xml_no_tail(self):
        raw = ('<sysmsg type="revokemsg"><revokemsg><content>'
               '<![CDATA["张三"撤回了一条消息]]></content>'
               '<revoketime>0</revoketime></revokemsg></sysmsg>')
        wx, _ = platform_text.system_pair(raw, recall=True)
        self.assertEqual(wx, '"张三"撤回了一条消息')

    def test_localize_terms(self):
        self.assertEqual(platform_text.localize("链接 / 公众号 朋友圈", "qq"), "链接 QQ空间")
        self.assertEqual(platform_text.localize("微信知识助手", "qq"), "QQ知识助手")
        self.assertEqual(platform_text.localize("微信知识助手", "wechat"), "微信知识助手")


class TestCommands(unittest.TestCase):
    def test_aliases_and_shortcuts(self):
        cases = {
            "/帮助": "help", "1": "help", "help": "help", "菜单": "help",
            "/2": "fast", "快速": "fast", "f": "fast",
            "/3": "explore", "探索": "explore",
            "/4": "think_on", "思考": "think_on",
            "/5": "think_off", "关思考": "think_off",
            "/6": "new", "新对话": "new",
            "/7": "pause", "暂停": "pause",
            "/8": "resume", "继续": "resume",
            "/9": "status", "状态": "status", "现状": "status",
        }
        for text, act in cases.items():
            self.assertEqual(botcore.parse_command(text), act, text)

    def test_normal_questions_not_commands(self):
        for text in ["1+1 等于几", "最近群里聊了什么", "帮我找一下报价单", "", "2 月份的会议记录"]:
            self.assertIsNone(botcore.parse_command(text), text)

    def test_help_has_three_sections(self):
        old = botcore.kb_brief
        botcore.kb_brief = lambda: "本机记录约 1,000 条 · 模型 test（已配置）"
        try:
            text = botcore.help_text(
                {"explorer": False, "thinking": False, "paused": False,
                 "turns": [], "session_id": None}, platform="微信")
            for word in ["能做什么", "现在的状态", "怎么用", "/1", "/9", "小提示"]:
                self.assertIn(word, text)
            self.assertIn("本机记录约 1,000 条", text)
            qq = botcore.help_text(None, platform="QQ", menu_hint=True)
            self.assertIn("菜单", qq)
        finally:
            botcore.kb_brief = old

    def test_command_execution_and_state_isolation(self):
        old = botcore.kb_brief
        botcore.kb_brief = lambda: "数据源：测试库"
        try:
            with tempfile.TemporaryDirectory() as d:
                a = botcore.Store(Path(d) / "wx.json")
                b = botcore.Store(Path(d) / "qq.json")
                conf = {"default_explorer": False, "default_thinking": False}
                self.assertIn("探索模式", botcore.execute_command(
                    "explore", openid="me", store=a, conf=conf, platform="微信"))
                self.assertIn("深度思考", botcore.execute_command(
                    "think_on", openid="me", store=a, conf=conf, platform="微信"))
                st = botcore.status_text(a.entity("me")[1], platform="微信")
                self.assertIn("探索模式", st)
                # 另一个通道的状态不受影响
                self.assertFalse(b.entity("me")[1]["explorer"])
                self.assertIn("已暂停", botcore.execute_command(
                    "pause", openid="me", store=a, conf=conf, platform="微信"))
                self.assertTrue(a.entity("me")[1]["paused"])
                self.assertIn("已恢复", botcore.execute_command(
                    "resume", openid="me", store=a, conf=conf, platform="微信"))
        finally:
            botcore.kb_brief = old

    def test_legacy_migration(self):
        with tempfile.TemporaryDirectory() as d:
            legacy = Path(d) / "qqbot_state.json"
            legacy.write_text('{"openclaw": {"session_id": 41}, '
                              '"ABCD1234": {"session_id": 39}}',
                              encoding="utf-8")
            wx = botcore.Store(Path(d) / "wxagent.json", [
                (legacy, lambda raw: {"openclaw": raw["openclaw"]}
                 if "openclaw" in raw else {})])
            qq = botcore.Store(Path(d) / "qqagent.json", [(legacy, "drop_openclaw")])
            self.assertIn("openclaw", wx.load())
            self.assertNotIn("ABCD1234", wx.load())
            self.assertNotIn("openclaw", qq.load())
            self.assertIn("ABCD1234", qq.load())
            self.assertTrue(legacy.exists(), "旧状态文件必须保留")

    def test_text_tools(self):
        self.assertEqual(botcore.strip_citation_markers("答案 [W1] 和 [2] 。"), "答案 和。")
        self.assertIn("·", botcore.md_to_plain("- 第一\n- 第二"))
        parts = botcore.chunk_text("第一行\n第二行", 4, 2, "（略）")
        self.assertTrue(parts)
        self.assertTrue(all(len(p) <= 12 for p in parts))


class TestQQMenu(unittest.TestCase):
    def test_menu_matches_command_table(self):
        import qqagent
        names = []
        for item in qqagent.MENU_ITEMS:
            self.assertIn(item["type"], ("send_message", "menu"))
            if item["type"] == "menu":
                for sub in item["sub_menu_items"]:
                    names.append(sub["send_message"])
            else:
                names.append(item["send_message"])
        for n in names:
            self.assertIsNotNone(botcore.parse_command(n), n)
        for key in ("帮助", "快速", "探索", "思考", "关思考", "新对话", "暂停", "继续", "状态"):
            self.assertIn(key, names)


class TestQQMessages(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        qq_messages._ready = False

    def tearDown(self):
        self.con.close()

    def _event(self, openid="ABCDEF123456", group=False):
        t = "GROUP_AT_MESSAGE_CREATE" if group else "C2C_MESSAGE_CREATE"
        d = {"id": "msg-1", "timestamp": "1762000000000",
             "author": {"user_openid": openid, "member_openid": openid}}
        if group:
            d["group_openid"] = "GROUP9876"
        return t, d

    def test_record_and_list(self):
        t, d = self._event()
        qq_messages.record_from_event(self.con, t, d, "你好", 0, "msg-1")
        qq_messages.record_from_event(self.con, t, d, "我在", 1, "msg-1")
        convs = qq_messages.conversations(self.con)
        self.assertEqual(len(convs), 1)
        self.assertEqual(convs[0]["cnt"], 2)
        self.assertEqual(convs[0]["scene"], "c2c")
        msgs = qq_messages.messages(self.con, convs[0]["conv"])
        self.assertEqual([m["text"] for m in msgs], ["你好", "我在"])
        self.assertFalse(msgs[0]["is_self"])
        self.assertTrue(msgs[1]["is_self"])

    def test_rename(self):
        t, d = self._event(group=True)
        qq_messages.record_from_event(self.con, t, d, "群消息", 0)
        conv = qq_messages.conversations(self.con)[0]["conv"]
        name = qq_messages.rename(self.con, conv, "产品讨论群")
        self.assertEqual(name, "产品讨论群")
        self.assertEqual(qq_messages.conversations(self.con)[0]["name"], "产品讨论群")
        with self.assertRaises(ValueError):
            qq_messages.rename(self.con, conv, "   ")

    def test_media_and_enrich(self):
        t, d = self._event()
        media = [{"kind": "image", "name": "m1_0.png", "filename": "截图.png",
                  "text": "报销单"}]
        qq_messages.record_from_event(self.con, t, d, "", 0, "msg-2")
        qq_messages.enrich(self.con, "msg-2", "[图片]\n报销单", media)
        conv = qq_messages.conversations(self.con)[0]["conv"]
        msg = qq_messages.messages(self.con, conv)[0]
        self.assertIn("报销单", msg["text"])
        self.assertEqual(msg["media"][0]["kind"], "image")
        self.assertEqual(msg["media"][0]["filename"], "截图.png")

    def test_search(self):
        t, d = self._event()
        qq_messages.record_from_event(self.con, t, d, "周末去爬山吗", 0, "msg-3")
        qq_messages.record_from_event(self.con, t, d, "周一开会", 0, "msg-4")
        hits = qq_messages.search(self.con, "爬山")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["snippet"], "周末去爬山吗")
        self.assertTrue(hits[0]["conv"].startswith("c2c:"))
        self.assertEqual(qq_messages.search(self.con, ""), [])

    def test_overview_empty(self):
        self.assertEqual(qq_messages.overview(self.con)["messages"], 0)


class TestChannelAgents(unittest.TestCase):
    def test_qq_and_wechat_ask_are_separate_agents(self):
        """两套 Agent 的提问载荷必须带各自 platform，避免共用一套会话/提示词。"""
        import types
        fake = types.ModuleType("main")
        fake.api_sessions = lambda: []
        fake.api_messages = lambda *a, **k: {"messages": []}
        fake.api_ai_session_new = lambda payload: {"id": 7}
        fake.claim_session_platform = lambda sid, platform: None
        captured = {}
        fake.api_ask = lambda payload: (captured.update(payload) or {"answer": "好"})
        old = sys.modules.get("main")
        sys.modules["main"] = fake
        try:
            with tempfile.TemporaryDirectory() as d:
                store = botcore.Store(Path(d) / "state.json", [])
                botcore.ask("qq-user", "你好", store=store, conf={}, channel="qq")
                self.assertEqual(captured["platform"], "qq")
                self.assertIn("QQ", captured["question"])
                botcore.ask("wx-main", "你好", store=store, conf={}, channel="weixin")
                self.assertEqual(captured["platform"], "wechat")
                self.assertIn("微信", captured["question"])
                self.assertIn("ClawBot", captured["question"])
        finally:
            if old is not None:
                sys.modules["main"] = old
            else:
                sys.modules.pop("main", None)



class TestQQAgentTools(unittest.TestCase):
    def test_qq_only_tools(self):
        import agent
        names = [x["function"]["name"] for x in agent._tool_schemas("qq")]
        self.assertEqual(set(names), {"search_qq", "search_qq_contacts", "search_qq_files"})
        self.assertNotIn("rag_search", names)
        self.assertNotIn("search_moments", names)

    def test_both_platform_merges_tools(self):
        """跨平台提问（微信+QQ）时两套工具都要可用。"""
        import agent
        names = {x["function"]["name"] for x in agent._tool_schemas("both")}
        self.assertTrue({"search_qq", "rag_search"} <= names, names)


class TestQQLocalParse(unittest.TestCase):
    @staticmethod
    def _vi(n):
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def _fld(self, num, wire, val):
        key = self._vi((num << 3) | wire)
        if wire == 0:
            return key + self._vi(int(val))
        if wire == 2:
            val = val if isinstance(val, bytes) else str(val).encode()
            return key + self._vi(len(val)) + val
        raise ValueError(wire)

    def _blob(self, *fields):
        body = b"".join(fields)
        return self._fld(40800, 2, body)

    def test_text(self):
        blob = self._blob(self._fld(45002, 0, 1), self._fld(45101, 2, "你好"))
        got = qq_local._parse_message(blob, 2)
        self.assertEqual(got["kind"], "text")
        self.assertEqual(got["text"], "你好")

    def test_image_and_dedupe(self):
        seg = (self._fld(45002, 0, 2) + self._fld(45402, 2, "a.png")
               + self._fld(45405, 0, 123) + self._fld(45406, 2, bytes.fromhex("abcdef"))
               + self._fld(45411, 0, 10) + self._fld(45412, 0, 20))
        blob = self._fld(40800, 2, seg) + self._fld(40800, 2, seg)
        got = qq_local._parse_message(blob, 2)
        self.assertEqual(got["kind"], "image")
        self.assertEqual(got["text"], "[图片]")
        self.assertEqual(len(got["media"]), 1)
        self.assertEqual(got["media"][0]["md5"], "abcdef")

    def test_forward_card(self):
        xml = ('<msg brief="[聊天记录]" action="viewMultiMsg" tSum="2">'
               '<item><title>群聊的聊天记录</title><title>张三: 你好</title>'
               '<summary>查看2条转发消息</summary></item></msg>')
        blob = self._blob(self._fld(45002, 0, 16), self._fld(48602, 2, xml))
        got = qq_local._parse_message(blob, 8)
        self.assertEqual(got["kind"], "forward")
        self.assertIn("群聊的聊天记录", got["text"])
        self.assertIn("2条", got["text"])

    def test_schema_and_search(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            qq_local.ensure_schema(con)
            con.execute("INSERT INTO qq_chat(conv,name,is_group,last_ts,cnt,last_text) "
                        "VALUES('group:1','测试群',1,100,2,'毕业之前能喝上吗')")
            con.execute("INSERT INTO qq_local_message(conv,msg_id,ts,sender_name,is_sender,text) "
                        "VALUES('group:1',1,100,'张三',0,'毕业之前能喝上吗')")
            con.commit()
            self.assertEqual(qq_local.sessions(con)[0]["name"], "测试群")
            hits = qq_local.search(con, "毕业")
            self.assertEqual(hits[0]["conv_name"], "测试群")
            self.assertEqual(qq_local.messages(con, "group:1")[0]["sender_name"], "张三")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
