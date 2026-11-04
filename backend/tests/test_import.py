# -*- coding: utf-8 -*-
"""m0_import 冒烟测试：临时构造一份很小的解密库，验证幂等、断点、进度与 dry-run。"""
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "m0_import.py"


def _make_source(root: Path, rows: int = 3) -> Path:
    """root/wxid_test_1234/decrypted/{contact,session,message}"""
    dec = root / "wxid_test_1234" / "decrypted"
    (dec / "contact").mkdir(parents=True)
    (dec / "session").mkdir(parents=True)
    (dec / "message").mkdir(parents=True)

    c = sqlite3.connect(dec / "contact" / "contact.db")
    c.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)")
    c.execute("INSERT INTO contact VALUES('wxid_test','本人','')")
    c.execute("INSERT INTO contact VALUES('wxid_friend','朋友','')")
    c.commit(); c.close()

    s = sqlite3.connect(dec / "session" / "session.db")
    s.execute("CREATE TABLE SessionTable(username TEXT, summary TEXT, last_timestamp INTEGER)")
    s.execute("INSERT INTO SessionTable VALUES('wxid_friend','hi',1700000000)")
    s.commit(); s.close()

    m = sqlite3.connect(dec / "message" / "message_0.db")
    m.execute("CREATE TABLE Name2Id(user_name TEXT)")
    m.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(1,'wxid_test')")
    m.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(2,'wxid_friend')")
    user = "wxid_friend"
    table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
    m.execute(f"CREATE TABLE [{table}] (local_id INTEGER, local_type INTEGER, "
              "create_time INTEGER, message_content BLOB, real_sender_id INTEGER)")
    m.execute(f"INSERT INTO [{table}] VALUES(1,1,1700000001,'你好',2)")
    m.execute(f"INSERT INTO [{table}] VALUES(2,1,1700000002,'<msg><title>周报.pdf</title></msg>',2)")
    m.execute(f"INSERT INTO [{table}] VALUES(3,1,1700000003,'',2)")
    m.commit(); m.close()
    return dec


def _run(dec: Path, out: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT),
                           "--decrypted-dir", str(dec), "--out", str(out), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="ignore")


class ImportTest(unittest.TestCase):
    def test_import_is_idempotent_and_resumable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dec = _make_source(root)
            out = root / "knowledge.db"
            first = _run(dec, out)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("[进度]", first.stdout)
            con = sqlite3.connect(out)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM message").fetchone()[0], 2)
            self.assertGreaterEqual(con.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0], 1)
            con.close()

            again = _run(dec, out)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("新增 0 条", again.stdout)

    def test_resume_from_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dec = _make_source(root)
            out = root / "knowledge.db"
            first = _run(dec, out, "--limit", "1")
            self.assertEqual(first.returncode, 0, first.stderr)
            con = sqlite3.connect(out)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM message").fetchone()[0], 1)
            con.close()
            second = _run(dec, out)
            self.assertEqual(second.returncode, 0, second.stderr)
            con = sqlite3.connect(out)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM message").fetchone()[0], 2)
            con.close()

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dec = _make_source(root)
            out = root / "knowledge.db"
            r = _run(dec, out, "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("[dry-run]", r.stdout)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
