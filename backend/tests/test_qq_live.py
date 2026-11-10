import sqlite3
import struct
import sys
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import qq_local as q


class QQLiveTest(unittest.TestCase):
    def test_live_fetch_skips_reinitializing_and_empty_media_scan(self):
        con = sqlite3.connect(':memory:')
        q.ensure_schema(con)
        with patch.object(q, 'ensure_schema') as prepare, patch.object(q, 'hydrate_media') as hydrate:
            self.assertEqual(q.messages(con, 'c2c:test', after=1, schema_ready=True), [])
            prepare.assert_not_called()
            hydrate.assert_not_called()
        con.close()

    def test_unexpected_schema_disables_rowid_shortcut(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'source.db'
            source = sqlite3.connect(path)
            source.execute('CREATE TABLE c2c_msg_table ("40001" TEXT PRIMARY KEY)')
            source.execute('CREATE TABLE group_msg_table ("40001" INTEGER PRIMARY KEY)')
            source.commit()
            source.close()
            self.assertFalse(q._rowid_compatible(path))

    def test_wal_change_imports_new_message_without_scanning_history(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            account = root / '123'
            dbdir = account / 'nt_qq' / 'nt_db'
            dbdir.mkdir(parents=True)
            sqlite_path = root / 'source.db'
            source = sqlite3.connect(sqlite_path)
            cols = ['40001', '40050', '40013', '40020', '40033', '40021',
                    '40030', '40011', '40800', '40090']
            schema = ','.join('"' + c + '" ' + ('INTEGER PRIMARY KEY' if c == '40001' else '') for c in cols)
            for table in ('c2c_msg_table', 'group_msg_table'):
                source.execute(f'CREATE TABLE {table} ({schema})')
            row = lambda rid: (rid, 1700000000, 1, '', 0, '', 123, 1, b'hello', '')
            source.execute('INSERT INTO c2c_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)', row(10))
            source.commit()
            original = sqlite_path.read_bytes()
            salt = b's' * q.SALT_SIZE
            (dbdir / 'nt_msg.db').write_bytes(b'\0' * q.EXT_HEADER + salt + original[16:])
            knowledge = sqlite3.connect(':memory:')
            q.ensure_schema(knowledge)
            def decrypt(_src, dst, _key):
                dst.write_bytes(original)
            with patch.dict(q._KEY_CACHE, {salt.hex(): '00' * 32}), \
                 patch.object(q, 'RESERVE', 0), \
                 patch.object(q, 'decrypt_db', side_effect=decrypt), \
                 patch.object(q, '_aes_cbc_decrypt', side_effect=lambda _k, _i, data: data), \
                 patch.object(q, 'pick_account', return_value=account), \
                 patch.object(q, '_media_index_cached', return_value={}), \
                 patch.object(q, '_parse_message', return_value={'kind': 'text', 'text': 'hello'}):
                live = root / 'live'
                first = q.live_update(knowledge, live, account)
                self.assertIsNone(first['rowids'])
                q.import_all(knowledge, rebuild=False, light=True, since_ts=1, decrypt_dir=live)
                source.execute('INSERT INTO c2c_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)', row(5))
                source.commit()
                updated = sqlite_path.read_bytes()
                changed = [(i // q.PAGE_SIZE + 1, updated[i:i + q.PAGE_SIZE])
                           for i in range(0, len(updated), q.PAGE_SIZE)
                           if original[i:i + q.PAGE_SIZE] != updated[i:i + q.PAGE_SIZE]]
                wal_salt = b'12345678'
                header = struct.pack('>IIII', 0x377f0682, 3007000, q.PAGE_SIZE, 0) + wal_salt + b'\0' * 8
                frames = b''.join(struct.pack('>II', pgno, len(updated) // q.PAGE_SIZE if n == len(changed) - 1 else 0)
                                  + wal_salt + b'\0' * 8 + page for n, (pgno, page) in enumerate(changed))
                (dbdir / 'nt_msg.db-wal').write_bytes(header + frames)
                delta = q.live_update(knowledge, live, account)
                self.assertIn(5, delta['rowids'])
                result = q.import_all(knowledge, rebuild=False, light=True, since_ts=1,
                                      decrypt_dir=live, changed_rowids=delta['rowids'])
                self.assertEqual((result['messages'], result['scanned']), (1, 1))
                self.assertEqual({r[0] for r in knowledge.execute('SELECT msg_id FROM qq_local_message')}, {5, 10})
            source.close()
            knowledge.close()

    def test_changed_leaf_page_finds_its_rowids(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'source.db'
            source = sqlite3.connect(path)
            source.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT)')
            ids = [-4, 5, 7706145741287867160]
            source.executemany('INSERT INTO messages VALUES (?,?)', [(i, 'x') for i in ids])
            source.commit()
            root = source.execute("SELECT rootpage FROM sqlite_master WHERE name='messages'").fetchone()[0]
            source.close()
            with path.open('rb') as f:
                f.seek((root - 1) * q.PAGE_SIZE)
                page = f.read(q.PAGE_SIZE)
            self.assertEqual(q._leaf_rowids(page, root), set(ids))

    def test_imports_only_changed_rowids_even_when_ids_decrease(self):
        with tempfile.TemporaryDirectory() as td:
            source = sqlite3.connect(Path(td) / 'nt_msg.db')
            cols = ['40001', '40050', '40013', '40020', '40033', '40021',
                    '40030', '40011', '40800', '40090']
            schema = ','.join('"' + c + '" ' + ('INTEGER PRIMARY KEY' if c == '40001' else '') for c in cols)
            for table in ('c2c_msg_table', 'group_msg_table'):
                source.execute(f'CREATE TABLE {table} ({schema})')
            for rid in (10, 5):
                source.execute('INSERT INTO c2c_msg_table VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (rid, 1700000000, 1, '', 0, '', 123, 1, b'hello', ''))
            source.commit()
            source.close()
            con = sqlite3.connect(':memory:')
            q.ensure_schema(con)
            with patch.object(q, 'pick_account', return_value=Path(td) / '123'), \
                 patch.object(q, '_media_index_cached', return_value={}), \
                 patch.object(q, '_parse_message', return_value={'kind': 'text', 'text': 'hello'}):
                first = q.import_all(con, rebuild=False, light=True, since_ts=1,
                                     decrypt_dir=td, changed_rowids={10})
                second = q.import_all(con, rebuild=False, light=True, since_ts=1,
                                      decrypt_dir=td, changed_rowids={5, 10})
                self.assertEqual((first['messages'], first['scanned']), (1, 1))
                self.assertEqual((second['messages'], second['scanned']), (1, 1))
                self.assertEqual({r[0] for r in con.execute('SELECT msg_id FROM qq_local_message')}, {5, 10})
            con.close()

    def test_incremental_insert_counts_only_new_messages(self):
        con = sqlite3.connect(':memory:')
        q.ensure_schema(con)
        row = ('c2c:test', 1, 1700000000, '', 0, '', 1, 1, 1, 'hello', '{}')
        self.assertEqual(q._qq_insert_msgs(con, [row], incremental=True), 1)
        self.assertEqual(q._qq_insert_msgs(con, [row], incremental=True), 0)
        con.close()

    def test_new_wal_commit_does_not_decrypt_main_database_again(self):
        with tempfile.TemporaryDirectory() as td:
            account = Path(td) / '123'
            dbdir = account / 'nt_qq' / 'nt_db'
            dbdir.mkdir(parents=True)
            salt = b's' * q.SALT_SIZE
            (dbdir / 'nt_msg.db').write_bytes(b'\0' * q.EXT_HEADER + salt + b'\0' * (3 * q.PAGE_SIZE - len(salt)))
            wal_salt = b'12345678'
            header = struct.pack('>IIII', 0x377f0682, 3007000, q.PAGE_SIZE, 0) + wal_salt + b'\0' * 8
            frame = lambda value: struct.pack('>II', 2, 2) + wal_salt + b'\0' * 8 + value * q.PAGE_SIZE
            wal = dbdir / 'nt_msg.db-wal'
            wal.write_bytes(header + frame(b'x'))
            con = sqlite3.connect(':memory:')
            def decrypt(_src, dst, _key):
                dst.write_bytes(b'\0' * (3 * q.PAGE_SIZE))
            with patch.dict(q._KEY_CACHE, {salt.hex(): '00' * 32}), \
                 patch.object(q, 'decrypt_db', side_effect=decrypt) as decrypt_main, \
                 patch.object(q, '_aes_cbc_decrypt', side_effect=lambda _k, _i, data: data):
                q.live_update(con, Path(td) / 'live', account)
                with wal.open('ab') as f:
                    f.write(frame(b'y'))
                src = dbdir / 'nt_msg.db'
                st = src.stat()
                os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
                read_bytes = Path.read_bytes
                def read_without_main(path):
                    if path == src:
                        raise AssertionError('checkpoint should not block new WAL frames')
                    return read_bytes(path)
                with patch.object(Path, 'read_bytes', autospec=True, side_effect=read_without_main):
                    q.live_update(con, Path(td) / 'live', account)
                self.assertEqual(decrypt_main.call_count, 1)
                self.assertEqual((Path(td) / 'live' / 'nt_msg.db').read_bytes()[q.PAGE_SIZE], ord('y'))
                new_salt = b'87654321'
                wal.write_bytes(header[:16] + new_salt + header[24:] +
                                struct.pack('>II', 3, 3) + new_salt + b'\0' * 8 + b'z' * q.PAGE_SIZE)
                q.live_update(con, Path(td) / 'live', account)
                self.assertEqual(decrypt_main.call_count, 1)
                result = (Path(td) / 'live' / 'nt_msg.db').read_bytes()
                self.assertEqual(result[q.PAGE_SIZE], 0)
                self.assertEqual(result[2 * q.PAGE_SIZE], ord('z'))
                with (dbdir / 'nt_msg.db').open('ab') as f:
                    f.write(b'\0' * q.PAGE_SIZE)
                q.live_update(con, Path(td) / 'live', account)
                self.assertEqual(decrypt_main.call_count, 1)
                wal.unlink()
                with (dbdir / 'nt_msg.db').open('r+b') as f:
                    f.seek(q.EXT_HEADER + q.PAGE_SIZE)
                    f.write(b'v' * q.PAGE_SIZE)
                q.live_update(con, Path(td) / 'live', account)
                result = (Path(td) / 'live' / 'nt_msg.db').read_bytes()
                self.assertEqual(decrypt_main.call_count, 1)
                self.assertEqual(result[q.PAGE_SIZE], ord('v'))
                self.assertEqual(result[2 * q.PAGE_SIZE], 0)
            con.close()

    def test_bootstrap_replays_committed_wal_frames(self):
        with tempfile.TemporaryDirectory() as td:
            account = Path(td) / '123'
            dbdir = account / 'nt_qq' / 'nt_db'
            dbdir.mkdir(parents=True)
            salt = b's' * q.SALT_SIZE
            src = dbdir / 'nt_msg.db'
            src.write_bytes(b'\0' * q.EXT_HEADER + salt + b'\0' * (2 * q.PAGE_SIZE - len(salt)))
            wal_salt = b'12345678'
            header = struct.pack('>IIII', 0x377f0682, 3007000, q.PAGE_SIZE, 0) + wal_salt + b'\0' * 8
            frame = struct.pack('>II', 2, 2) + wal_salt + b'\0' * 8 + b'x' * q.PAGE_SIZE
            uncommitted = struct.pack('>II', 2, 0) + wal_salt + b'\0' * 8 + b'y' * q.PAGE_SIZE
            stale = struct.pack('>II', 2, 2) + b'old-salt' + b'\0' * 8 + b'z' * q.PAGE_SIZE
            (dbdir / 'nt_msg.db-wal').write_bytes(header + frame + uncommitted + stale)
            con = sqlite3.connect(':memory:')
            def decrypt(_src, dst, _key):
                dst.write_bytes(b'\0' * (2 * q.PAGE_SIZE))
            with patch.dict(q._KEY_CACHE, {salt.hex(): '00' * 32}), \
                 patch.object(q, 'decrypt_db', side_effect=decrypt), \
                 patch.object(q, '_aes_cbc_decrypt', side_effect=lambda _k, _i, data: data):
                q.live_update(con, Path(td) / 'live', account)
            result = (Path(td) / 'live' / 'nt_msg.db').read_bytes()
            self.assertEqual(result[q.PAGE_SIZE], ord('x'))
            con.close()


if __name__ == '__main__':
    unittest.main()
