# -*- coding: utf-8 -*-
"""出站脱敏规则测试（纯标准库，python -m unittest 可跑）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import privacy  # noqa: E402


class PrivacyTest(unittest.TestCase):
    def test_phone(self):
        out = privacy.redact_text("电话 13812345678，备用 +86 13900001111", {})
        self.assertNotIn("13812345678", out)
        self.assertNotIn("13900001111", out)
        self.assertEqual(out.count("[手机号]"), 2)

    def test_idcard_checksum_shape(self):
        out = privacy.redact_text("身份证 110105199003071234", {})
        self.assertIn("[身份证]", out)
        self.assertNotIn("110105199003071234", out)

    def test_bankcard(self):
        out = privacy.redact_text("卡号 6222021234567890123", {})
        self.assertIn("[银行卡]", out)
        self.assertNotIn("6222021234567890123", out)

    def test_email(self):
        out = privacy.redact_text("邮箱 alice.wang@example.com.cn", {})
        self.assertIn("[邮箱]", out)
        self.assertNotIn("alice.wang@example.com.cn", out)

    def test_short_numbers_untouched(self):
        text = "订单 123456，时间 2024-01-02，消息 id 88"
        self.assertEqual(privacy.redact_text(text, {}), text)

    def test_master_switch(self):
        conf = {"privacy": {"enabled": False}}
        text = "13812345678"
        self.assertEqual(privacy.redact_text(text, conf), text)

    def test_per_rule_switch(self):
        conf = {"privacy": {"phone": False, "bankcard": False, "idcard": False, "email": True}}
        text = "13812345678 / a@b.com"
        out = privacy.redact_text(text, conf)
        self.assertIn("13812345678", out)
        self.assertIn("[邮箱]", out)

    def test_nested_tool_messages(self):
        conf = {}
        messages = [{"role": "system", "content": "联系 13812345678"},
                    {"role": "assistant", "content": None,
                     "tool_calls": [{"id": "1", "type": "function",
                                     "function": {"name": "read_chat",
                                                  "arguments": '{"who":"a@b.com"}'}}]},
                    {"role": "tool", "content": "身份证 110105199003071234"}]
        out = privacy.redact_messages(conf, messages)
        self.assertIn("[手机号]", out[0]["content"])
        self.assertIn("[邮箱]", out[1]["tool_calls"][0]["function"]["arguments"])
        self.assertIn("[身份证]", out[2]["content"])
        # 原对象不被修改
        self.assertEqual(messages[0]["content"], "联系 13812345678")

    def test_placeholders_not_double_replaced(self):
        out = privacy.redact_text("[手机号] [身份证] [银行卡] [邮箱]", {})
        self.assertEqual(out, "[手机号] [身份证] [银行卡] [邮箱]")


if __name__ == "__main__":
    unittest.main()
