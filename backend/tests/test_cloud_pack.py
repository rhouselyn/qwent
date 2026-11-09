# -*- coding: utf-8 -*-
"""增量包 / 基线分片：加解密 roundtrip、篡改检测、会话表导出导回。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import cloud_pack  # noqa: E402

KEY = bytes(range(32))


class TestPack(unittest.TestCase):
    def test_delta_roundtrip_with_bytes(self):
        changes = [{"lsn": 1, "tbl": "message", "pk": "7", "op": "I",
                    "row": {"id": 7, "content": "报价", "blob": b"\x00\xff\x10"}}]
        blob, manifest = cloud_pack.pack_delta(1, changes, 1, 1, "sig", KEY)
        self.assertEqual(manifest["rows"], 1)
        out = cloud_pack.unpack_delta(blob, KEY, manifest["sha256"])
        self.assertEqual(out["changes"][0]["row"]["blob"], b"\x00\xff\x10")
        self.assertEqual(out["schema_sig"], "sig")

    def test_tamper_and_wrong_key_rejected(self):
        changes = [{"lsn": 1, "tbl": "chat", "pk": "u", "op": "U", "row": {}}]
        blob, m = cloud_pack.pack_delta(2, changes, 1, 1, "sig", KEY)
        with self.assertRaises(ValueError):
            cloud_pack.unpack_delta(blob, KEY, "0" * 64)
        bad = blob[:-1] + bytes([blob[-1] ^ 1])
        with self.assertRaises(Exception):
            cloud_pack.unpack_delta(bad, KEY)
        with self.assertRaises(Exception):
            cloud_pack.unpack_delta(blob, bytes(32), m["sha256"])

    def test_baseline_parts_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = tmp / "src.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
            con.executemany("INSERT INTO t VALUES(?,?)",
                            [(i, "x" * 2000) for i in range(500)])
            con.commit()
            con.close()
            out = tmp / "out"
            m = cloud_pack.pack_baseline(db, out, KEY, chunk_mb=1)
            self.assertGreater(len(m["parts"]), 0)
            self.assertFalse((out / "baseline.snapshot.db").exists())
            dst = tmp / "restored.db"
            r = cloud_pack.restore_baseline(out, dst, KEY, m)
            self.assertEqual(r["check"], "ok")
            c2 = sqlite3.connect(dst)
            self.assertEqual(c2.execute("SELECT COUNT(*) FROM t").fetchone()[0], 500)
            c2.close()

    def test_session_dump_roundtrip(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE ai_qa(id INTEGER PRIMARY KEY, question TEXT)")
        con.execute("INSERT INTO ai_qa VALUES(1,'云端提的问题')")
        dump = cloud_pack.export_tables(con, ["ai_qa"])
        con.execute("DELETE FROM ai_qa")
        self.assertEqual(cloud_pack.import_tables(con, dump), 1)
        self.assertEqual(con.execute("SELECT question FROM ai_qa").fetchone()[0],
                         "云端提的问题")
        con.close()


if __name__ == "__main__":
    unittest.main()
