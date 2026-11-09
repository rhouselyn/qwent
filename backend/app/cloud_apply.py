# -*- coding: utf-8 -*-
"""云端侧：按序号应用增量包、重建 FTS/向量、恢复全量基线。

会话表（ai_qa / ai_session / qq_message / qq_conv）由云端机器人独立写入，
既不参与同步，也在基线恢复前后做导出/导回，绝不被本地快照覆盖。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback

import cloud_cdc
import cloud_conf
import cloud_pack
import cloud_transport

SESSION_TABLES = ["ai_qa", "ai_session", "qq_message", "qq_conv"]

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_status: dict = {"running": False, "last": "", "last_ok": "", "error": "",
                 "applied_seq": 0, "last_rows": 0, "epoch": "", "lag_sec": 0}


def db_path():
    return cloud_conf.data_dir() / "knowledge.db"


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path()), timeout=60, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    return con


def _ensure_state_tables(con: sqlite3.Connection) -> None:
    for stmt in cloud_cdc.DDL:
        con.execute(stmt)
    con.commit()


def _pk_vals(tbl: str, pk: str) -> list:
    cols = cloud_cdc.SYNC_TABLES[tbl]
    raw = pk.split("|", len(cols) - 1) if len(cols) > 1 else [pk]
    return [int(v) if v.lstrip("-").isdigit() else v for v in raw]


def _apply_change(con: sqlite3.Connection, ch: dict) -> None:
    tbl, pk, op, row = ch["tbl"], ch["pk"], ch["op"], ch.get("row")
    if tbl not in cloud_cdc.SYNC_TABLES:
        return
    cols = cloud_cdc.SYNC_TABLES[tbl]
    vals = _pk_vals(tbl, pk)
    where = " AND ".join(f'"{c}"=?' for c in cols)
    if op == "D" or not row:
        con.execute(f'DELETE FROM "{tbl}" WHERE {where}', vals)
        return
    keys = list(row.keys())
    non_pk = [k for k in keys if k not in cols]
    if non_pk:
        sets = ",".join(f'"{k}"=?' for k in non_pk)
        cur = con.execute(f'UPDATE "{tbl}" SET {sets} WHERE {where}',
                          [row[k] for k in non_pk] + vals)
        if cur.rowcount:
            return
    collist = ",".join(f'"{k}"' for k in keys)
    ph = ",".join("?" for _ in keys)
    con.execute(f'INSERT OR IGNORE INTO "{tbl}" ({collist}) VALUES ({ph})',
                [row[k] for k in keys])


def _rebuild_fts(con: sqlite3.Connection, ch: dict) -> None:
    """从逻辑行重建 FTS 影子行（虚表不参与 CDC）。写法与本地导入保持一致。"""
    tbl, pk, op, row = ch["tbl"], ch["pk"], ch["op"], ch.get("row")
    try:
        from textutil import search_text as clean_search
        if tbl == "message":
            mid = _pk_vals("message", pk)[0]
            con.execute("DELETE FROM message_fts WHERE rowid=?", (mid,))
            if op != "D" and row:
                st = clean_search(1, row.get("content") or "")
                if len(st.strip()) >= 2:
                    cu = row.get("chat_user") or ""
                    con.execute("INSERT INTO message_fts(rowid, content, chat_user, is_group) "
                                "VALUES(?,?,?,?)",
                                (mid, st[:20000], cu, int("@chatroom" in cu)))
        elif tbl == "media_text":
            mid = _pk_vals("media_text", pk)[0]
            con.execute("DELETE FROM media_fts WHERE message_id=?", (mid,))
            if op != "D" and row and (row.get("text") or "").strip():
                con.execute("INSERT INTO media_fts(text, message_id) VALUES(?,?)",
                            (row["text"][:4000], mid))
        elif tbl == "link_cache":
            url = _pk_vals("link_cache", pk)[0]
            con.execute("DELETE FROM link_fts WHERE url=?", (url,))
            if op != "D" and row and (row.get("text") or "").strip():
                con.execute("INSERT INTO link_fts(text, title, url) VALUES(?,?,?)",
                            (row["text"][:40000], row.get("title") or "", url))
        elif tbl == "sns_media_ocr":
            tid = _pk_vals("sns_media_ocr", pk)[0]
            con.execute("DELETE FROM sns_media_fts WHERE tid=?", (tid,))
            if op != "D" and row and (row.get("text") or "").strip():
                con.execute("INSERT INTO sns_media_fts(text, tid) VALUES(?,?)",
                            (row["text"][:4000], tid))
    except sqlite3.OperationalError as exc:
        # 基线来自旧 schema 时个别 FTS 表可能缺失：不阻断主数据重放
        print(f"[cloud] fts rebuild skipped ({tbl}): {exc}")


def _rebuild_vec(con: sqlite3.Connection, mids: list[int]) -> int:
    """对新增消息补 512 维向量，复用本地同一套 bge-small-zh 与写入逻辑。"""
    if not mids or not cloud_conf.IS_CLOUD:
        return 0            # 只有云端需要补向量；本地由原有 _vec_loop 负责
    try:
        import vector
        if not vector.available():
            return 0
        vcon = vector.connect(str(db_path()))
        try:
            n = 0
            for mid in mids[:1000]:
                r = con.execute(
                    "SELECT id, content, chat_user, type FROM message WHERE id=?",
                    (mid,)).fetchone()
                if not r:
                    continue
                txt = vector.message_text(con, mid, r["type"], r["content"])
                if len(txt.strip()) < 2:
                    continue
                emb = vector.embed([txt])[0].tobytes()
                for stmt, params in (
                    ("DELETE FROM msg_vec_p WHERE message_id=?", (mid,)),
                    ("INSERT INTO msg_vec_p(message_id, embedding, chat_user) VALUES(?,?,?)",
                     (mid, emb, r["chat_user"])),
                    ("DELETE FROM msg_vec WHERE message_id=?", (mid,)),
                    ("INSERT INTO msg_vec(message_id, embedding) VALUES(?,?)", (mid, emb)),
                ):
                    try:
                        vcon.execute(stmt, params)
                    except sqlite3.OperationalError:
                        pass
                n += 1
            vcon.commit()
            return n
        finally:
            vcon.close()
    except Exception as exc:      # noqa: BLE001  模型/扩展缺失时降级为纯 FTS
        print(f"[cloud] vector rebuild skipped: {exc}")
        return 0


def _state_int(con: sqlite3.Connection, key: str) -> int:
    try:
        return int(cloud_cdc.get_state(con, key, "0"))
    except ValueError:
        return 0


def apply_next() -> dict:
    """应用下一个序号的增量包；序号不连续 / epoch 不符 / 校验失败一律拒绝。"""
    global _status
    with _lock:
        conf = cloud_conf.load_conf()
        key = cloud_conf.encrypt_key(conf)
        tr = cloud_transport.get_transport(conf)
        con = _db()
        try:
            _ensure_state_tables(con)
            applied = _state_int(con, "applied_seq")
            epoch = cloud_cdc.get_state(con, "epoch", "")
            want = applied + 1
            mname = cloud_pack.delta_name(want, "json")
            if not tr.exists(mname):
                _status.update(last=time.strftime("%H:%M:%S"),
                               applied_seq=applied, epoch=epoch)
                return {"ok": True, "rows": 0, "seq": applied,
                        "reason": f"没有第 {want} 号增量包"}
            manifest = cloud_transport.read_json(tr, mname)
            if int(manifest.get("seq", -1)) != want:
                raise ValueError("增量包序号与 manifest 不符")
            if epoch and manifest.get("epoch") and manifest["epoch"] != epoch:
                raise ValueError("增量包 epoch 与当前基线不一致，需要重新做全量基线")
            expected_sig = cloud_cdc.schema_sig(con)
            if manifest.get("schema_sig") and manifest["schema_sig"] != expected_sig:
                raise ValueError("schema 指纹不一致：本地已改表，需要重新做全量基线")
            blob = tr.read_bytes(manifest["file"])
            payload = cloud_pack.unpack_delta(blob, key, manifest.get("sha256", ""))
            changes = payload["changes"]
            con.execute("BEGIN IMMEDIATE")
            try:
                msg_mids = []
                for ch in changes:
                    _apply_change(con, ch)
                    _rebuild_fts(con, ch)
                    if ch["tbl"] == "message" and ch["op"] != "D":
                        msg_mids.append(_pk_vals("message", ch["pk"])[0])
                cloud_cdc.set_state(con, "applied_seq", want)
                if manifest.get("epoch"):
                    cloud_cdc.set_state(con, "epoch", manifest["epoch"])
                cloud_cdc.set_state(con, "applied_lsn", manifest.get("lsn_to", 0))
                con.commit()
            except Exception:
                con.rollback()
                raise
            vec_n = _rebuild_vec(con, msg_mids)
            try:
                con.execute("DELETE FROM _cloud_cdc")  # 基线快照带来的本地 CDC 残留
                con.commit()
            except sqlite3.OperationalError:
                pass
            _status.update(last=time.strftime("%H:%M:%S"),
                           last_ok=time.strftime("%Y-%m-%d %H:%M:%S"),
                           error="", applied_seq=want, last_rows=len(changes),
                           epoch=manifest.get("epoch", epoch), lag_sec=0)
            cloud_conf.write_state({"applied_seq": want,
                                    "applied_at": int(time.time()),
                                    "epoch": manifest.get("epoch", epoch)})
            return {"ok": True, "seq": want, "rows": len(changes),
                    "vectors": vec_n, "epoch": manifest.get("epoch", "")}
        except Exception as exc:      # noqa: BLE001
            _status.update(last=time.strftime("%H:%M:%S"), error=str(exc)[:300])
            cloud_conf.write_state({"last_error": str(exc)[:300],
                                    "last_error_at": int(time.time())})
            return {"ok": False, "error": str(exc)[:300],
                    "trace": traceback.format_exc()[-800:]}
        finally:
            con.close()


def restore_baseline() -> dict:
    """从 inbox 里的加密分片恢复 knowledge.db，并保住云端已有会话。"""
    conf = cloud_conf.load_conf()
    key = cloud_conf.encrypt_key(conf)
    tr = cloud_transport.get_transport(conf)
    manifest = cloud_transport.read_json(tr, "baseline.json")
    cache = cloud_conf.data_dir() / "cloud_restore"
    (cache / "base").mkdir(parents=True, exist_ok=True)
    for p in manifest["parts"]:
        data = tr.read_bytes(p["name"])
        fp = cache / p["name"]
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(data)
    old = db_path()
    dump = {}
    if old.exists():
        c0 = sqlite3.connect(str(old))
        try:
            dump = cloud_pack.export_tables(c0, SESSION_TABLES)
        finally:
            c0.close()
    result = cloud_pack.restore_baseline(cache, old, key, manifest)
    con = sqlite3.connect(str(old))
    try:
        _ensure_state_tables(con)
        if dump:
            cloud_pack.import_tables(con, dump)
        con.execute("DELETE FROM _cloud_cdc")
        cloud_cdc.set_state(con, "epoch", manifest.get("epoch", ""))
        cloud_cdc.set_state(con, "applied_seq", 0)
        cloud_cdc.set_state(con, "applied_lsn", int(manifest.get("lsn_to") or 0))
    finally:
        con.close()
    epoch = manifest.get("epoch", "")
    _status.update(applied_seq=0, epoch=epoch, error="",
                   last_ok=time.strftime("%Y-%m-%d %H:%M:%S"))
    cloud_conf.write_state({"applied_seq": 0, "epoch": epoch,
                            "baseline_at": int(time.time())})
    return {"ok": True, **result, "epoch": epoch,
            "sessions_restored": sum(len(v.get("rows") or []) for v in dump.values())}


def _loop():
    while not _stop.is_set():
        try:
            apply_next()
        except Exception as exc:      # noqa: BLE001
            _status["error"] = str(exc)[:200]
        if _stop.wait(30):
            return


def start() -> None:
    global _thread
    if not cloud_conf.IS_CLOUD or _thread is not None:
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="cloud-apply")
    _thread.start()
    _status["running"] = True


def stop() -> None:
    _stop.set()
    _status["running"] = False


def status() -> dict:
    st = cloud_conf.read_state()
    applied_at = int(st.get("applied_at") or 0)
    return {"side": "cloud", **_status,
            "lag_sec": int(time.time() - applied_at) if applied_at else None,
            "enabled": cloud_conf.enabled(),
            "transport": cloud_conf.cloud_conf().get("transport", "file"),
            "last_error": st.get("last_error", "")}
