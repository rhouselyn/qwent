# -*- coding: utf-8 -*-
"""QQ 官方机器人网关（薄壳）。

只负责 WebSocket 收事件 + REST 被动回复 + 消息留存；指令、帮助、状态、
菜单、问答全部在 qqagent.py，共用内核在 botcore.py。
微信 ClawBot 通道已彻底拆到 wxagent.py，本模块不再包含任何微信通道逻辑。

配置仍在 backend/config.json 的 "qqbot" 段：
  {"enabled": false, "app_id": "", "app_secret": "", "group_reply": false,
   "citations": false, "allow_openids": [], "default_explorer": false,
   "default_thinking": false, "max_msg_chars": 700, "max_chunks": 4}
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from pathlib import Path

import websocket

import botcore as C
import qq_attach
import qqagent
import qq_messages

PROJECT = Path(__file__).resolve().parent.parent.parent

log = logging.getLogger("qqbot")
if not log.handlers:
    _h = logging.FileHandler(PROJECT / "backend" / "qqbot.log", encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

API_BASE = qqagent.API_BASE
TOKEN_URL = qqagent.TOKEN_URL
FALLBACK_GATEWAY = "wss://api.sgroup.qq.com/websocket"
INTENTS = 1 << 25                     # GROUP_AND_C2C（私聊 + 群@）

OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY, OP_RESUME = 0, 1, 2, 6
OP_RECONNECT, OP_INVALID, OP_HELLO, OP_HEARTBEAT_ACK = 7, 9, 10, 11

RECONNECT_DELAYS = [1, 2, 5, 10, 30, 60]
MAX_MSG_CHARS = 700
MAX_CHUNKS = 4

STATE_PATH = qqagent.STATE_PATH        # 兼容旧引用；实际状态由 qqagent 管理

DEFAULT_CONF = {
    "enabled": False,
    "app_id": "",
    "app_secret": "",
    "group_reply": False,
    "citations": False,
    "wechat_chat": None,              # 锁定某个本机会话为默认上下文；None = 全局
    "default_explorer": False,
    "default_thinking": False,
    "max_msg_chars": MAX_MSG_CHARS,
    "max_chunks": MAX_CHUNKS,
    "allow_openids": [],
}

STATUS: dict = {
    "running": False, "connected": False, "username": "", "error": "",
    "last_event": "", "users": 0, "started_at": "",
    "menu": "", "menu_error": "",
}

_runner: "QQBotRunner | None" = None
_apply_lock = threading.Lock()


def conf_of(main_conf: dict) -> dict:
    q = dict(DEFAULT_CONF)
    q.update(main_conf.get("qqbot") or {})
    return q


def status() -> dict:
    return dict(STATUS)


def apply_config(main_conf: dict) -> None:
    """按最新配置启动/停止/重连 bot（配置保存后与进程启动时调用）。"""
    global _runner
    with _apply_lock:
        q = conf_of(main_conf)
        want = bool(q.get("enabled") and q.get("app_id") and q.get("app_secret"))
        if _runner and not want:
            _runner.stop()
            _runner = None
        if want:
            if _runner and _runner.signature(q) and _runner.is_alive():
                _runner.refresh_conf(q)
            else:
                if _runner:
                    _runner.stop()
                _runner = QQBotRunner(q)
                _runner.start()


def shutdown() -> None:
    global _runner
    with _apply_lock:
        if _runner:
            _runner.stop()
            _runner = None


# ---------------------------------------------------------------- 兼容层
# 旧脚本/外部代码可能从 qqbot 直接取这些工具函数；统一转发到 botcore/qqagent，
# 保证升级后不会出现 ImportError。新代码请直接用 botcore / qqagent。
fmt_ts = C.fmt_ts
render_record = C.render_record
msg_text = C.msg_text
strip_citation_markers = C.strip_citation_markers
md_to_qq = C.md_to_plain
MENU_ITEMS = qqagent.MENU_ITEMS
set_menu = qqagent.set_menu
get_menu = qqagent.get_menu
_fetch_token = qqagent._fetch_token


def chunk_text(text: str, limit: int, max_chunks: int) -> list[str]:
    return C.chunk_text(text, limit, max_chunks,
                        "（内容过长已截断，可在助手网页查看全文）")


def _load_state() -> dict:
    return qqagent.STORE.load()


def _save_state(state: dict) -> None:
    qqagent.STORE.save(state)


def build_prompt(openid: str, question: str, history: list | None = None):
    import main as M
    return C.build_prompt(openid, question, store=qqagent.STORE,
                          conf=conf_of(M.load_conf()), channel="qq",
                          history=history)


def ask_qq(openid: str, question: str, history: list | None = None) -> str:
    """QQ 通道问答（兼容别名）；微信通道请走 wxagent.ask()。"""
    import main as M
    return qqagent.ask(openid, question, conf_of(M.load_conf()))


# ================================================================ 网关客户端
class QQBotRunner(threading.Thread):
    """一个机器人账户：WebSocket 收事件，REST 发回复。"""

    def __init__(self, q: dict):
        super().__init__(daemon=True, name="qqbot-gateway")
        self.q = q
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._token, self._token_exp = "", 0.0
        self.session_id, self.last_seq = None, None
        self._seq_lock = threading.Lock()
        self._msg_seq: dict[str, int] = {}
        self._work: "queue.Queue[tuple]" = queue.Queue()
        self._hb_interval = 30.0
        self._last_ack = time.time()
        self._ws: websocket.WebSocket | None = None

    # ---- 生命周期 ----
    def signature(self, q: dict) -> bool:
        return (self.q.get("app_id") == q.get("app_id")
                and self.q.get("app_secret") == q.get("app_secret"))

    def refresh_conf(self, q: dict) -> None:
        self.q = q

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:  # noqa: BLE001
            pass

    def is_alive(self) -> bool:  # noqa: A003
        return threading.Thread.is_alive(self)

    def run(self) -> None:
        STATUS.update({"running": True, "started_at": fmt_ts(time.time()),
                       "error": ""})
        worker = threading.Thread(target=self._worker_loop, daemon=True)
        worker.start()
        delay_idx = 0
        while not self._stop.is_set():
            try:
                self._session()
                delay_idx = 0
            except Exception as exc:  # noqa: BLE001
                log.warning("gateway session ended: %s", exc)
                STATUS["error"] = str(exc)[:200]
                STATUS["connected"] = False
            if self._stop.is_set():
                break
            delay = RECONNECT_DELAYS[min(delay_idx, len(RECONNECT_DELAYS) - 1)]
            delay_idx += 1
            self._stop.wait(delay)
        STATUS.update({"running": False, "connected": False})

    # ---- 连接 ----
    def _token_get(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._token_exp:
                return self._token
            r = qqagent._req("POST", TOKEN_URL, timeout=15, json={
                "appId": self.q["app_id"],
                "clientSecret": self.q["app_secret"]})
            r.raise_for_status()
            d = r.json()
            if not d.get("access_token"):
                raise RuntimeError(f"获取 access_token 失败: {d}")
            self._token = d["access_token"]
            try:
                ttl = int(d.get("expires_in") or 7200)
            except (TypeError, ValueError):
                ttl = 7200
            self._token_exp = time.time() + ttl - 300
            log.info("access token refreshed, ttl=%ds", ttl)
            return self._token

    def _gateway_url(self) -> str:
        try:
            r = qqagent._req("GET", f"{API_BASE}/gateway", timeout=15, headers={
                "Authorization": f"QQBot {self._token_get()}"})
            r.raise_for_status()
            url = r.json().get("url")
            if url:
                return url
        except Exception as exc:  # noqa: BLE001
            log.warning("get gateway failed (%s), use fallback", exc)
        return FALLBACK_GATEWAY

    def _session(self) -> None:
        ws = websocket.WebSocket()
        self._ws = ws
        ws.settimeout(5)
        url = self._gateway_url()
        log.info("connecting %s", url)
        ws.connect(url, timeout=15)
        hello = json.loads(ws.recv())
        if hello.get("op") != OP_HELLO:
            raise RuntimeError(f"expected HELLO, got op={hello.get('op')}")
        self._hb_interval = max(5.0, (hello.get("d") or {}).get(
            "heartbeat_interval", 30000) / 1000.0)
        token = self._token_get()
        if self.session_id and self.last_seq is not None:
            ws.send(json.dumps({"op": OP_RESUME, "d": {
                "token": f"QQBot {token}",
                "session_id": self.session_id, "seq": self.last_seq}}))
            log.info("resume sent session=%s seq=%s", self.session_id, self.last_seq)
        else:
            ws.send(json.dumps({"op": OP_IDENTIFY, "d": {
                "token": f"QQBot {token}", "intents": INTENTS, "shard": [0, 1]}}))
            log.info("identify sent intents=%d", INTENTS)

        hb_stop = threading.Event()
        hb = threading.Thread(target=self._heartbeat_loop,
                              args=(ws, hb_stop), daemon=True)
        hb.start()
        try:
            while not self._stop.is_set():
                try:
                    frame = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    if time.time() - self._last_ack > self._hb_interval * 3:
                        raise RuntimeError("heartbeat ACK timeout")
                    continue
                op = frame.get("op")
                if frame.get("s") is not None:
                    self.last_seq = frame["s"]
                if op == OP_DISPATCH:
                    self._on_dispatch(frame.get("t") or "", frame.get("d") or {})
                elif op == OP_HEARTBEAT_ACK:
                    self._last_ack = time.time()
                elif op == OP_HEARTBEAT:
                    self._send(ws, {"op": OP_HEARTBEAT, "d": self.last_seq})
                elif op == OP_RECONNECT:
                    raise RuntimeError("gateway requested reconnect")
                elif op == OP_INVALID:
                    if not frame.get("d"):
                        self.session_id, self.last_seq = None, None
                    raise RuntimeError("invalid session")
        finally:
            hb_stop.set()
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _heartbeat_loop(self, ws: websocket.WebSocket, stop_evt: threading.Event):
        while not stop_evt.wait(self._hb_interval):
            try:
                self._send(ws, {"op": OP_HEARTBEAT, "d": self.last_seq})
            except Exception:  # noqa: BLE001
                return

    def _send(self, ws: websocket.WebSocket, payload: dict) -> None:
        with self._lock:
            ws.send(json.dumps(payload))

    # ---- 事件 ----
    def _on_dispatch(self, t: str, d: dict) -> None:
        if t == "READY":
            self.session_id = d.get("session_id")
            user = d.get("user") or {}
            STATUS.update({"connected": True, "username": user.get("username", ""),
                           "error": ""})
            log.info("READY bot=%s(%s)", user.get("username"), user.get("id"))
            return
        if t == "RESUMED":
            STATUS["connected"] = True
            log.info("RESUMED")
            return
        if t in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            STATUS["last_event"] = fmt_ts(time.time())
            self._work.put((t, d))

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                t, d = self._work.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._handle_message(t, d)
            except Exception as exc:  # noqa: BLE001
                log.exception("handle message failed")
                try:
                    self._reply(t, d, f"[处理失败：{str(exc)[:150]}]")
                except Exception:  # noqa: BLE001
                    pass

    # ---- 消息留存 ----
    def _record(self, t: str, d: dict, content: str, direction: int,
                msg_id: str = "", media: list | None = None) -> None:
        try:
            import main as M
            con = M.db()
            try:
                qq_messages.record_from_event(con, t, d, content, direction, msg_id, media)
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("record qq message skipped: %s", exc)

    def _enrich(self, msg_id: str, content: str, media: list) -> None:
        try:
            import main as M
            con = M.db()
            try:
                qq_messages.enrich(con, msg_id, content, media)
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("enrich qq message skipped: %s", exc)

    # ---- 消息处理 ----
    def _handle_message(self, t: str, d: dict) -> None:
        conf_q = conf_of(self.q) if isinstance(self.q, dict) else dict(DEFAULT_CONF)
        is_group = t != "C2C_MESSAGE_CREATE"
        if is_group and not conf_q.get("group_reply"):
            return
        author = d.get("author") or {}
        openid = (author.get("user_openid") if not is_group
                  else author.get("member_openid")) or ""
        if not openid:
            return
        allow = conf_q.get("allow_openids") or []
        if allow and openid not in allow:
            log.info("openid %s… not in allow list, ignored", openid[:8])
            return
        content = re.sub(r"<@!?\d+>", "", str(d.get("content") or "")).strip()
        msg_id = d.get("id") or ""
        # 先落原文（哪怕后面的附件处理失败，也不会丢消息），再补附件解析结果。
        self._record(t, d, content, 0, msg_id)
        try:
            STATUS["users"] = len(qqagent.STORE.load())
        except Exception:
            pass

        extra, media = qq_attach.parse(msg_id, d.get("attachments") or [])
        if extra or media:
            combined = "\n".join(x for x in (content, extra) if x).strip()
            self._enrich(msg_id, combined, media)
        else:
            combined = content

        if not content and not any((m or {}).get("text") for m in media):
            self._send_text(t, d, "收到图片/语音/文件啦，已保存到本机，但没识别出文字。"
                                   "可以直接发文字问题，或再发一次带文字的说明～", conf_q)
            return

        reply = qqagent.command(openid, content, conf_q) if content else None
        if reply is None:
            if qqagent.paused(openid, conf_q):
                return
            reply = qqagent.ask(openid, combined, conf_q)
        self._send_text(t, d, reply, conf_q)

    def _send_text(self, t: str, d: dict, text: str, conf_q: dict) -> None:
        """完整回复只落库一条，发给 QQ 时按长度分片。"""
        if not text:
            return
        plain = C.md_to_plain(text)
        limit = int(conf_q.get("max_msg_chars") or MAX_MSG_CHARS)
        chunks = int(conf_q.get("max_chunks") or MAX_CHUNKS)
        for piece in C.chunk_text(plain, limit, chunks,
                                  "（内容过长已截断，可在助手网页查看全文）"):
            self._reply(t, d, piece)
        self._record(t, d, text, 1, d.get("id") or "")

    # ---- 发送 ----
    def _next_seq(self, msg_id: str) -> int:
        with self._seq_lock:
            n = self._msg_seq.get(msg_id, 0) + 1
            self._msg_seq[msg_id] = n
            if len(self._msg_seq) > 500:
                for k in list(self._msg_seq.keys())[:250]:
                    self._msg_seq.pop(k, None)
            return n

    def _reply(self, t: str, d: dict, text: str) -> None:
        if not text:
            return
        is_group = t != "C2C_MESSAGE_CREATE"
        target = d.get("group_openid") if is_group else (
            (d.get("author") or {}).get("user_openid") or "")
        path = (f"/v2/groups/{target}/messages" if is_group
                else f"/v2/users/{target}/messages")
        msg_id = d.get("id") or ""
        body = {"content": text, "msg_type": 0, "msg_seq": self._next_seq(msg_id)}
        if msg_id:
            body["msg_id"] = msg_id
        r = qqagent._req("POST", f"{API_BASE}{path}", timeout=20, json=body, headers={
            "Authorization": f"QQBot {self._token_get()}"})
        if r.status_code == 401:
            r = qqagent._req("POST", f"{API_BASE}{path}", timeout=20, json=body, headers={
                "Authorization": f"QQBot {self._token_get(force=True)}"})
        if r.status_code >= 400:
            log.error("send failed %s %s", r.status_code, r.text[:200])
            r.raise_for_status()
        log.info("replied %s len=%d", path, len(text))
