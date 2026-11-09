# -*- coding: utf-8 -*-
"""端到端：CDC → 加密增量包 → 按序重放 → FTS 重建，以及乱序/会话保护/接管判定。"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import cloud_apply  # noqa: E402
import cloud_cdc  # noqa: E402
import cloud_conf  # noqa: E402
import cloud_sync_local  # noqa: E402
import cloud_transport  # noqa: E402

KEY_B64 = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="  # base64('A'*32)


class TestCloudSync(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qwent-sync-"))
        (self.tmp / "inbox").mkdir()
        os.environ["QWENT_CLOUD_KEY"] = KEY_B64
        self.conf = {"cloud": {
            "enabled": True, "auto": True, "transport": "file",
            "endpoint": str(self.tmp / "inbox"), "interval_min": 10,
            "encrypt_key": KEY_B64,
        }}
        db = self.tmp / "knowledge.db"
        con = sqlite3.connect(db)
        con.executescript("""
          CREATE TABLE message(id INTEGER PRIMARY KEY, chat_user TEXT, content TEXT,
                              type INTEGER DEFAULT 1, is_sender INTEGER DEFAULT 0,
                              sender TEXT DEFAULT '', ts INTEGER DEFAULT 0,
                              src_db TEXT DEFAULT '', src_table TEXT DEFAULT '',
                              src_local_id INTEGER DEFAULT 0, noise INTEGER DEFAULT 0);
          CREATE TABLE media_text(message_id INTEGER PRIMARY KEY, kind TEXT, text TEXT, ts INTEGER);
          CREATE TABLE ai_qa(id INTEGER PRIMARY KEY, question TEXT);
          CREATE VIRTUAL TABLE message_fts USING fts5(content, chat_user UNINDEXED,
                              is_group UNINDEXED, tokenize='trigram');
          CREATE VIRTUAL TABLE media_fts USING fts5(text, message_id UNINDEXED,
                              tokenize='trigram');
        """)
        cloud_cdc.install_triggers(con)
        con.close()
        self._patchers = [
            mock.patch.object(cloud_conf, "data_dir", return_value=self.tmp),
            mock.patch.object(cloud_conf, "load_conf", return_value=self.conf),
            mock.patch.object(cloud_conf, "save_conf"),
            mock.patch.object(cloud_conf, "IS_CLOUD", False),
        ]
        for p in self._patchers:
            p.start()
        cloud_sync_local._triggers_ready = False
        cloud_sync_local._status.update(rounds=0, error="")
        cloud_apply._status.update(error="", applied_seq=0)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        os.environ.pop("QWENT_CLOUD_KEY", None)

    def test_full_roundtrip(self):
        con = sqlite3.connect(self.tmp / "knowledge.db")
        con.execute("INSERT INTO message(id,chat_user,content) VALUES(1,'u1','上线定在周五')")
        con.execute("INSERT INTO media_text VALUES(1,'ocr','截图报价 1200',0)")
        con.commit()
        con.execute("UPDATE message SET content='上线改到下周一' WHERE id=1")
        con.commit()
        con.close()

        r1 = cloud_sync_local.sync_now(force=True)
        self.assertTrue(r1["ok"], r1)
        self.assertEqual(r1["rows"], 2)
        tr = cloud_transport.get_transport(self.conf)
        self.assertIn("delta/00000001.json", tr.list("delta"))

        r2 = cloud_apply.apply_next()
        self.assertTrue(r2["ok"], r2)
        con = sqlite3.connect(self.tmp / "knowledge.db")
        self.assertEqual(con.execute(
            "SELECT content FROM message WHERE id=1").fetchone()[0], "上线改到下周一")
        self.assertTrue(con.execute(
            "SELECT 1 FROM message_fts WHERE message_fts MATCH '下周一'").fetchone())
        self.assertTrue(con.execute(
            "SELECT 1 FROM media_fts WHERE media_fts MATCH '1200'").fetchone())
        con.close()
        self.assertTrue(cloud_apply.apply_next()["ok"])  # 重复调用幂等

    def test_session_table_not_touched(self):
        con = sqlite3.connect(self.tmp / "knowledge.db")
        con.execute("INSERT INTO ai_qa VALUES(9,'云端会话')")
        con.commit()
        con.close()
        r = cloud_apply.apply_next()   # 无增量包时也不该动会话
        self.assertTrue(r["ok"])
        con = sqlite3.connect(self.tmp / "knowledge.db")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM ai_qa").fetchone()[0], 1)
        con.close()

    def test_takeover_conf(self):
        base = {"qqbot": {"enabled": True, "app_id": "x"},
                "agent_api": {"enabled": True, "api_key": "wk-abc"},
                "cloud": {"enabled": True, "takeover_bots": True}}
        got = cloud_conf.effective_bot_conf(base)
        self.assertFalse(got["qqbot"]["enabled"])
        self.assertEqual(got["agent_api"]["api_key"], "wk-abc")  # 钥匙保留
        self.assertEqual(got["_gateway_owned_by"], "cloud")
        off = dict(base, cloud={"enabled": False})
        self.assertTrue(cloud_conf.effective_bot_conf(off)["qqbot"]["enabled"])


if __name__ == "__main__":
    unittest.main()
