# -*- coding: utf-8 -*-
"""CDC 触发器：INSERT / UPDATE / DELETE 捕获、排除表、去重与清理。"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import cloud_cdc  # noqa: E402


def make_db():
    con = sqlite3.connect(":memory:")
    con.executescript("""
      CREATE TABLE message(id INTEGER PRIMARY KEY, chat_user TEXT, content TEXT);
      CREATE TABLE chat(username TEXT PRIMARY KEY, display_name TEXT, pinned INTEGER DEFAULT 0);
      CREATE TABLE media_text(message_id INTEGER PRIMARY KEY, kind TEXT, text TEXT, ts INTEGER);
      CREATE TABLE sns_media_ocr(tid TEXT, media_idx INTEGER, kind TEXT, text TEXT,
                                 ts INTEGER, PRIMARY KEY(tid, media_idx, kind));
      CREATE TABLE ai_qa(id INTEGER PRIMARY KEY, question TEXT);
    """)
    return con


class TestCDC(unittest.TestCase):
    def setUp(self):
        self.con = make_db()
        self.n = cloud_cdc.install_triggers(self.con)

    def tearDown(self):
        self.con.close()

    def test_install_only_existing_tables(self):
        self.assertEqual(self.n, 12)  # 4 张存在的表 × 3
        self.assertIn("message", cloud_cdc.SYNC_TABLES)
        self.assertNotIn("ai_qa", cloud_cdc.SYNC_TABLES)

    def test_capture_insert_update_delete(self):
        c = self.con
        c.execute("INSERT INTO message VALUES(1,'u1','原文')")
        c.execute("INSERT INTO chat VALUES('x','群',0)")
        c.commit()
        c.execute("UPDATE message SET content='改后' WHERE id=1")
        c.execute("UPDATE chat SET pinned=1 WHERE username='x'")
        c.commit()
        c.execute("DELETE FROM message WHERE id=1")
        c.commit()
        changes, last = cloud_cdc.collect(c, 0)
        self.assertEqual(last, 5)
        # 同一主键只保留最后一次（DELETE）
        msgs = [x for x in changes if x["tbl"] == "message"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["op"], "D")
        self.assertIsNone(msgs[0]["row"])
        # 非自增主键表的 UPDATE 也要抓到，且带最新行内容
        chats = [x for x in changes if x["tbl"] == "chat"]
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["op"], "U")
        self.assertEqual(chats[0]["row"]["pinned"], 1)

    def test_excluded_tables_do_not_emit(self):
        self.con.execute("INSERT INTO ai_qa VALUES(1,'问题')")
        self.con.commit()
        changes, _ = cloud_cdc.collect(self.con, 0)
        self.assertFalse([x for x in changes if x["tbl"] == "ai_qa"])

    def test_composite_pk_and_row_payload(self):
        c = self.con
        c.execute("INSERT INTO sns_media_ocr VALUES('t1',0,'img','识别文字',1)")
        c.commit()
        changes, _ = cloud_cdc.collect(c, 0)
        ch = [x for x in changes if x["tbl"] == "sns_media_ocr"][0]
        self.assertEqual(ch["pk"], "t1|0|img")
        self.assertEqual(ch["row"]["text"], "识别文字")

    def test_prune_and_schema_sig(self):
        c = self.con
        c.execute("INSERT INTO message VALUES(1,'u1','a')")
        c.commit()
        _, last = cloud_cdc.collect(c, 0)
        sig1 = cloud_cdc.schema_sig(c)
        self.assertEqual(len(sig1), 32)
        self.assertEqual(cloud_cdc.prune(c, last), 1)
        self.assertEqual(cloud_cdc.collect(c, 0)[0], [])
        c.execute("ALTER TABLE message ADD COLUMN extra TEXT")
        self.assertNotEqual(cloud_cdc.schema_sig(c), sig1)


if __name__ == "__main__":
    unittest.main()
