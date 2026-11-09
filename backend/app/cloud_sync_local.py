# -*- coding: utf-8 -*-
"""本地侧：10 分钟增量同步、手动触发、全量基线。

云端不启动本模块（由 cloud_apply 接管），两端共用 cloud_pack / cloud_transport。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
import traceback
from pathlib import Path

import cloud_cdc
import cloud_conf
import cloud_pack
import cloud_transport

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_triggers_ready = False
_status: dict = {"running": False, "last": "", "last_ok": "", "error": "",
                 "last_rows": 0, "last_bytes": 0, "rounds": 0}


def db_path() -> Path:
    return cloud_conf.data_dir() / "knowledge.db"


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path()), timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    return con


def _ensure_triggers(con: sqlite3.Connection) -> None:
    global _triggers_ready
    if not _triggers_ready:
        cloud_cdc.install_triggers(con)
        _triggers_ready = True


def _epoch(manifest: dict) -> str:
    raw = f"{manifest.get('created')}|{manifest.get('db_size')}".encode()
    return hashlib.sha256(raw).hexdigest()[:12]


def _check_remote_watermark(tr, epoch: str, seq: int) -> None:
    """防止换电脑 / 旧备份用错水位：本地必须严格接在云端最后一个包之后。

    latest.json 由本地每次上传时覆盖写入，记着 (type, epoch, seq)。若本地
    data/cloud_state.json 或 knowledge.db 里的水位不是当前这一代（新电脑最常见），
    本地会从 seq=1 重新发包、还返回 ok=true，但云端永远在等更大的序号，属于静默失败。
    这里提前拦下并给出可操作的提示。
    """
    try:
        latest = cloud_transport.read_json(tr, "latest.json")
    except Exception:                # noqa: BLE001 服务器还没有任何包：交给基线流程
        return
    if not isinstance(latest, dict) or not latest.get("epoch"):
        return
    repoch = str(latest.get("epoch") or "")
    try:
        rseq = int(latest.get("seq") or 0)
    except (TypeError, ValueError):
        rseq = 0
    if not epoch:
        raise ValueError(
            "本地没有基线水位（换电脑或数据目录重建？）：请先在设置页生成全量基线，"
            "并在服务器执行 cloud_cli.py restore，之后才能增量同步")
    if epoch != repoch:
        raise ValueError(
            f"本地 epoch {epoch[:12]} 与云端 {repoch[:12]} 不一致："
            "请重新生成全量基线并让服务器恢复，不要直接发增量包")
    want = 1 if latest.get("type") == "baseline" else rseq + 1
    if seq != want:
        raise ValueError(
            f"本地序号 {seq} 接不上云端 {rseq}（应为 {want}）："
            "请检查 data/cloud_state.json 是否随项目一起迁移，或重做全量基线")


def sync_now(force: bool = False) -> dict:
    """打一个增量包并上传。手动按钮与定时循环都走这里。"""
    global _status
    with _lock:
        conf = cloud_conf.load_conf()
        if not cloud_conf.enabled(conf) and not force:
            return {"ok": False, "reason": "云端分流未开启"}
        started = time.time()
        try:
            key = cloud_conf.encrypt_key(conf)
            tr = cloud_transport.get_transport(conf)
            con = _db()
            try:
                _ensure_triggers(con)
                last_lsn = int(cloud_cdc.get_state(con, "last_sent_lsn", "0"))
                changes, lsn_to = cloud_cdc.collect(con, last_lsn)
                epoch = cloud_cdc.get_state(con, "epoch", "")
                seq = int(cloud_cdc.get_state(con, "last_sent_seq", "0")) + 1
                _check_remote_watermark(tr, epoch, seq)
                if not changes:
                    _status.update(last=time.strftime("%H:%M:%S"),
                                   last_ok=time.strftime("%Y-%m-%d %H:%M:%S"),
                                   error="", last_rows=0, last_bytes=0,
                                   rounds=_status["rounds"] + 1)
                    cloud_conf.write_state({"last_sync": int(time.time()),
                                            "last_sent_seq": seq - 1,
                                            "epoch": epoch})
                    return {"ok": True, "rows": 0, "seq": seq - 1,
                            "reason": "无变更"}
                sig = cloud_cdc.schema_sig(con)
                blob, manifest = cloud_pack.pack_delta(
                    seq, changes, last_lsn + 1, lsn_to, sig, key)
                manifest["epoch"] = epoch
                tr.put_bytes(cloud_pack.delta_name(seq), blob)
                cloud_transport.write_json(
                    tr, cloud_pack.delta_name(seq, "json"), manifest)
                cloud_transport.write_json(tr, "latest.json", {
                    "type": "delta", "seq": seq, "epoch": epoch,
                    "schema_sig": sig, "created": int(time.time())})
                cloud_cdc.set_state(con, "last_sent_lsn", lsn_to)
                cloud_cdc.set_state(con, "last_sent_seq", seq)
                cloud_cdc.prune(con, lsn_to)
            finally:
                con.close()
            _status.update(last=time.strftime("%H:%M:%S"),
                           last_ok=time.strftime("%Y-%m-%d %H:%M:%S"),
                           error="", last_rows=len(changes),
                           last_bytes=len(blob), rounds=_status["rounds"] + 1)
            cloud_conf.write_state({"last_sync": int(time.time()),
                                    "last_sent_seq": seq, "epoch": epoch,
                                    "last_rows": len(changes)})
            return {"ok": True, "rows": len(changes), "seq": seq,
                    "lsn_to": lsn_to, "bytes": len(blob),
                    "elapsed": round(time.time() - started, 2)}
        except Exception as exc:      # noqa: BLE001
            _status.update(last=time.strftime("%H:%M:%S"), error=str(exc)[:300])
            cloud_conf.write_state({"last_error": str(exc)[:300],
                                    "last_error_at": int(time.time())})
            return {"ok": False, "error": str(exc)[:300],
                    "trace": traceback.format_exc()[-800:]}


def baseline(force: bool = False) -> dict:
    """一次性全量基线：一致性快照 → 加密分片 → 上传 → 重置序号与水位。"""
    global _status
    with _lock:
        conf = cloud_conf.load_conf()
        if not cloud_conf.enabled(conf) and not force:
            return {"ok": False, "reason": "云端分流未开启"}
        started = time.time()
        try:
            key = cloud_conf.encrypt_key(conf)
            cc = cloud_conf.cloud_conf(conf)
            tr = cloud_transport.get_transport(conf)
            import json as _json
            out = cloud_conf.data_dir() / "cloud_out"
            out.mkdir(parents=True, exist_ok=True)
            mpath = out / "baseline.json"
            manifest = None
            reused = False
            # 断点续传：已生成的分片逐个校验 sha256，齐全则跳过 4.25GB 的重新压缩
            if mpath.exists():
                try:
                    cand = _json.loads(mpath.read_text(encoding="utf-8"))
                    if all((out / x["name"]).is_file()
                           and cloud_pack.sha256_file(out / x["name"]) == x["sha256"]
                           for x in cand["parts"]):
                        manifest = cand
                        reused = True
                        print(f"[baseline] 复用已生成的 {len(cand['parts'])} 个分片")
                except Exception:
                    manifest = None
            if manifest is None:
                # 快照之前先开触发器：否则「快照完成 → 触发器安装」之间的写入
                # 既不在快照里、也没有 CDC 记录，会被永久漏掉。
                c0 = _db()
                try:
                    _ensure_triggers(c0)
                finally:
                    c0.close()
                manifest = cloud_pack.pack_baseline(
                    db_path(), out, key, int(cc.get("baseline_chunk_mb") or 64))
                # 快照完成时刻的 CDC 水位：这批变更已包含在快照里，可以安全 prune。
                c0 = _db()
                try:
                    manifest["lsn_to"] = int(c0.execute(
                        "SELECT COALESCE(MAX(lsn),0) FROM _cloud_cdc").fetchone()[0])
                finally:
                    c0.close()
            epoch = manifest.get("epoch") or _epoch(manifest)
            manifest["epoch"] = epoch
            mpath.write_text(_json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
            uploaded = skipped = 0
            for p in manifest["parts"]:
                # 仅「复用同一批本地分片」时才允许按大小跳过；新基线一律重传，
                # 避免新快照的分片恰好同样大小、内容却不同而被误跳过。
                try:
                    if reused and tr.exists(p["name"]) and tr.size(p["name"]) == p["size"]:
                        skipped += 1
                        continue
                except Exception:
                    pass
                tr.put_file(out / p["name"], p["name"])
                uploaded += p["size"]
            tr.put_file(out / "baseline.json", "baseline.json")
            cloud_transport.write_json(tr, "latest.json", {
                "type": "baseline", "seq": 0, "epoch": epoch,
                "created": int(time.time())})

            con = _db()
            try:
                _ensure_triggers(con)
                # 只清掉快照已包含的变更；快照之后写入的留给第一个增量包。
                # 旧格式 manifest 没有 lsn_to，退回 0＝不 prune，保证不丢数据。
                lsn_to = int(manifest.get("lsn_to") or 0)
                cloud_cdc.prune(con, lsn_to)
                cloud_cdc.set_state(con, "last_sent_lsn", lsn_to)
                cloud_cdc.set_state(con, "last_sent_seq", 0)
                cloud_cdc.set_state(con, "epoch", epoch)
            finally:
                con.close()
            cloud_conf.write_state({"baseline_at": int(time.time()),
                                    "baseline_bytes": uploaded, "epoch": epoch,
                                    "last_sent_seq": 0})
            _status.update(last_ok=time.strftime("%Y-%m-%d %H:%M:%S"), error="")
            return {"ok": True, "epoch": epoch,
                    "db_size": manifest["db_size"],
                    "parts": len(manifest["parts"]), "uploaded": uploaded,
                    "skipped": skipped,
                    "elapsed": round(time.time() - started, 1)}
        except Exception as exc:      # noqa: BLE001
            _status.update(error=str(exc)[:300])
            return {"ok": False, "error": str(exc)[:300],
                    "trace": traceback.format_exc()[-800:]}


def _loop():
    while not _stop.is_set():
        conf = cloud_conf.load_conf()
        cc = cloud_conf.cloud_conf(conf)
        minutes = max(1, int(cc.get("interval_min") or 10))
        # 启动时先等一个完整周期，避免开机瞬间和实时同步抢库
        if _stop.wait(minutes * 60):
            return
        if cloud_conf.enabled(conf) and cc.get("auto", True):
            sync_now()


def start() -> None:
    global _thread
    if cloud_conf.IS_CLOUD or _thread is not None:
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="cloud-sync")
    _thread.start()
    _status["running"] = True


def stop() -> None:
    _stop.set()
    _status["running"] = False


def status() -> dict:
    st = cloud_conf.read_state()
    return {"side": "local", **_status,
            "enabled": cloud_conf.enabled(),
            "takeover": cloud_conf.takeover(),
            "interval_min": cloud_conf.cloud_conf().get("interval_min", 10),
            "transport": cloud_conf.cloud_conf().get("transport", "file"),
            "last_sent_seq": st.get("last_sent_seq", 0),
            "epoch": st.get("epoch", ""),
            "baseline_at": st.get("baseline_at", 0),
            "last_sync": st.get("last_sync", 0),
            "last_error": st.get("last_error", "")}
