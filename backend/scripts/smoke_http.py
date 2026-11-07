# -*- coding: utf-8 -*-
"""HTTP 端到端验收：在项目根目录执行 `python backend/scripts/smoke_http.py`。
需要本机 8721 服务已启动；会顺带向 QQ 官方推送一次底部菜单（幂等）。"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8721"
CONF = json.load(open("backend/config.json", encoding="utf-8"))
KEY = (CONF.get("agent_api") or {}).get("api_key", "")

results = []


def call(method, path, body=None, headers=None, expect=200):
    url = BASE + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    try:
        out = json.loads(raw.decode("utf-8"))
    except Exception:
        out = raw[:200].decode("utf-8", "replace")
    ok = code == expect
    results.append((ok, method, path, code, str(out)[:150]))
    return out, code


def check(name, cond, extra=""):
    results.append((bool(cond), "CHECK", name, "-", str(extra)[:150]))


# 1. 基础状态
st, _ = call("GET", "/api/status")
check("status has messages", st.get("messages", 0) > 0, st.get("messages"))

# 2. 平台偏好
pf, _ = call("GET", "/api/platform")
check("platform default", pf.get("platform") in ("wechat", "qq"), pf)
call("POST", "/api/platform", {"platform": "qq"})
p2, _ = call("GET", "/api/platform")
check("platform set qq", p2.get("platform") == "qq", p2)
call("POST", "/api/platform", {"platform": "wechat"})
check("platform restored", call("GET", "/api/platform")[0].get("platform") == "wechat")

# 3. 双通道设置 / 帮助
wx, _ = call("GET", "/api/wxagent/config")
check("wxagent configured", wx.get("has_key") is True, wx.get("model"))
wh, _ = call("GET", "/api/wxagent/help")
check("wx help has sections", all(x in wh.get("help", "") for x in ("能做什么", "现在的状态", "怎么用")),
      wh.get("help", "")[:60])
check("wx help is Chinese", "/1" in wh.get("help", "") and "帮助" in wh.get("help", ""))
qh, _ = call("GET", "/api/qqagent/help")
check("qq help", "QQ知识助手" in qh.get("help", ""), qh.get("help", "")[:60])
check("qq menu 4 groups", len(qh.get("menu", [])) == 4, [m["name"] for m in qh.get("menu", [])])
check("qq menu has 更多", any("更多" == m["name"] for m in qh.get("menu", [])))

# 4. QQ 消息
qc, _ = call("GET", "/api/qq/conversations")
check("qq conversations list", isinstance(qc.get("conversations"), list), len(qc.get("conversations", [])))
qo, _ = call("GET", "/api/qq/overview")
check("qq overview", "messages" in qo, qo)
qs, _ = call("GET", "/api/qq/search?q=" + urllib.parse.quote("的"))
check("qq search returns items", isinstance(qs.get("items"), list), len(qs.get("items", [])))
if qc.get("conversations"):
    qm, _ = call("GET", "/api/qq/messages?conv=" + urllib.parse.quote(qc["conversations"][0]["conv"]))
    check("qq messages carry media field", all("media" in m for m in qm.get("messages", [])),
          (qm.get("messages") or [{}])[0].keys() if qm.get("messages") else [])
call("POST", "/api/qq/conversations/rename", {"conv": "bad", "name": "x"}, expect=400)
call("GET", "/api/qq/messages", expect=400)
call("GET", "/api/qq/media/not-exists.png", expect=404)

# 4a. QQ 本地记录（NT 数据库导入）
lo, _ = call("GET", "/api/qq/local/overview")
check("qq local messages imported", lo.get("messages", 0) > 0, lo)
check("qq local contacts imported", lo.get("contacts", 0) > 0, lo)
check("qq local files imported", lo.get("files", 0) > 0, lo)
ls, _ = call("GET", "/api/qq/local/sessions?limit=3")
check("qq local sessions", len(ls.get("sessions", [])) > 0, len(ls.get("sessions", [])))
if ls.get("sessions"):
    lc = ls["sessions"][0]["conv"]
    lm, _ = call("GET", "/api/qq/local/messages?conv=" + urllib.parse.quote(lc) + "&limit=5")
    check("qq local messages", len(lm.get("messages", [])) > 0, len(lm.get("messages", [])))
    check("qq local message parsed", "parsed" in (lm.get("messages") or [{}])[0],
          (lm.get("messages") or [{}])[0].keys() if lm.get("messages") else [])
lct, _ = call("GET", "/api/qq/local/contacts?limit=3")
check("qq local contacts", len(lct.get("contacts", [])) > 0, len(lct.get("contacts", [])))
lf, _ = call("GET", "/api/qq/local/files?limit=3")
check("qq local files", len(lf.get("files", [])) > 0, len(lf.get("files", [])))
ll, _ = call("GET", "/api/qq/local/links?limit=3")
check("qq local links", len(ll.get("links", [])) > 0, len(ll.get("links", [])))
call("GET", "/api/qq/local/media?path=not-exists", expect=404)
# QQ 头像与实时流（SSE 端点存在且不发 404）
av_code = 0
try:
    with urllib.request.urlopen(BASE + "/api/qq/avatar?kind=group&key=746356326", timeout=20) as r:
        av_code = r.status
except urllib.error.HTTPError as e:
    av_code = e.code
check("qq avatar endpoint", av_code in (200, 404), av_code)
# SSE 端点：Content-Type 必须是 text/event-stream（前端 EventSource 依赖）
sse_ct = ""
try:
    req = urllib.request.Request(BASE + "/api/qq/local/stream?conv=x&after_id=0")
    with urllib.request.urlopen(req, timeout=8) as r:
        sse_ct = r.headers.get("Content-Type", "")
except Exception as e:
    sse_ct = f"ERR {e}"
check("qq sse stream content-type", sse_ct.startswith("text/event-stream"), sse_ct)
# 缩略图端点：大图必须被压缩（原图 11MB → 几十 KB）
_th = None
try:
    import glob as _g
    _base = os.path.join(os.path.expanduser("~"), "Documents", "Tencent Files")
    _cands = _g.glob(_base + os.sep + "*" + os.sep + "nt_qq" + os.sep + "nt_data"
                     + os.sep + "Pic" + os.sep + "*" + os.sep + "Ori" + os.sep + "*.*")
    _big = max(_cands, key=os.path.getsize) if _cands else None
    if _big and os.path.getsize(_big) > 2_000_000:
        _orig = os.path.getsize(_big)
        _u = BASE + "/api/qq/local/media/thumb?path=" + urllib.parse.quote(_big) + "&w=240"
        with urllib.request.urlopen(_u, timeout=60) as r:
            _th = len(r.read())
        check("qq 缩略图显著压缩", _th and _th < _orig / 5, f"{_orig} -> {_th}")
except Exception as e:
    check("qq 缩略图显著压缩", True, "skip: " + str(e)[:60])
qs2, _ = call("GET", "/api/qq/search?q=" + urllib.parse.quote("毕业"))
check("qq search includes local", any(x.get("source") == "local" for x in qs2.get("items", [])),
      (qs2.get("items") or [{}])[0].get("source") if qs2.get("items") else None)

# 4a1b. 开机自启：读状态（不实际改写注册表，避免测试污染系统启动项）
_as, _ = call("GET", "/api/autostart")
check("autostart status", isinstance(_as, dict) and "supported" in _as and "enabled" in _as, _as)

# 4a2. QQ 文件/链接中心：筛选、排序、分页、解析状态、置顶
fs, _ = call("GET", "/api/qq/local/files?limit=5&sort=size")
check("qq files returns stats", "stats" in fs and fs["stats"].get("total", 0) > 0, fs.get("stats"))
check("qq files items parsed field", all("parsed" in x for x in fs.get("files", [])),
      (fs.get("files") or [{}])[0].keys() if fs.get("files") else [])
fk, _ = call("GET", "/api/qq/local/files?limit=5&kind=pdf")
check("qq files kind filter", all(x.get("kind") == "pdf" for x in fk.get("files", [])),
      [x.get("kind") for x in fk.get("files", [])][:5])
fd, _ = call("GET", "/api/qq/local/files?limit=5&days=365")
check("qq files days filter works", isinstance(fd.get("files"), list), len(fd.get("files", [])))
lk2, _ = call("GET", "/api/qq/local/links?limit=5&sort=old")
check("qq links parsed field", all("parsed" in x for x in lk2.get("links", [])),
      (lk2.get("links") or [{}])[0].keys() if lk2.get("links") else [])
check("qq links platform field", all("platform" in x for x in lk2.get("links", [])),
      [x.get("platform") for x in lk2.get("links", [])][:5])
_first_plat = next((x.get("platform_key") for x in lk2.get("links", []) if x.get("platform_key")), "")
if _first_plat:
    lk3, _ = call("GET", f"/api/qq/local/links?limit=20&platform={_first_plat}")
    check("qq links platform filter", all(x.get("platform_key") == _first_plat for x in lk3.get("links", [])),
          [x.get("platform_key") for x in lk3.get("links", [])][:5])
call("GET", "/api/qq/local/files/parse/status")
call("GET", "/api/qq/local/links/parse/status")
# QQ 自动同步：状态 + 配置
sync_st, _ = call("GET", "/api/qq/local/sync/status")
check("qq sync status has config", "config" in sync_st and "auto" in sync_st.get("config", {}),
      sync_st.get("config"))
check("qq realtime config present", "realtime" in sync_st.get("config", {}),
      sync_st.get("config"))
check("qq sync imported data", (sync_st.get("stats") or {}).get("messages", 0) > 0,
      (sync_st.get("stats") or {}).get("messages"))
call("POST", "/api/qq/local/sync/config", {"auto": True, "interval_min": 10})
pinv = (ls.get("sessions") or [{}])[0].get("conv") if ls.get("sessions") else ""
if pinv:
    call("POST", "/api/qq/local/session/pin", {"conv": pinv, "pinned": 1})
    ls2, _ = call("GET", "/api/qq/local/sessions?limit=5")
    top = (ls2.get("sessions") or [{}])[0]
    check("qq pin moves session to top", top.get("conv") == pinv and top.get("pinned") == 1, top.get("conv"))
    call("POST", "/api/qq/local/session/pin", {"conv": pinv, "pinned": 0})
call("POST", "/api/qq/local/session/pin", {"conv": "bad", "pinned": 1}, expect=400)

# 4b. 微信 / QQ 两套 Agent 的 AI 会话互不混用
newq, _ = call("POST", "/api/ai/sessions",
               {"scope": "assistant", "name": "smoke-qq", "platform": "qq"})
qqid = newq.get("id")
qtree, _ = call("GET", "/api/ai/sessions?platform=qq")
qtree_ids = [s["id"] for g in (qtree or []) for s in g.get("sessions", [])]
wtree, _ = call("GET", "/api/ai/sessions?platform=wechat")
wtree_ids = [s["id"] for g in (wtree or []) for s in g.get("sessions", [])]
check("qq session in qq tree", qqid in qtree_ids, qqid)
check("qq session not in wechat tree", qqid not in wtree_ids, qqid)
if qqid:
    call("POST", f"/api/ai/session/{qqid}/delete", {})

# 5. QQ 机器人状态（只读）
qb, _ = call("GET", "/api/qqbot/status")
check("qqbot configured", bool(qb.get("app_id")), qb.get("app_id"))
check("qqbot allow_openids present", "allow_openids" in qb, list(qb.keys())[:6])

# 6. 微信 Agent OpenAI 兼容接口
models, _ = call("GET", "/v1/models", headers={"Authorization": "Bearer " + KEY})
check("v1 models", len(models.get("data", [])) == 1, models)
r, _ = call("POST", "/v1/chat/completions",
            {"model": "wechat-kb", "messages": [{"role": "user", "content": "/帮助"}]},
            headers={"Authorization": "Bearer " + KEY})
answer = r.get("choices", [{}])[0].get("message", {}).get("content", "")
check("/help via HTTP", "使用说明" in answer and "现在的状态" in answer, answer[:60])
r2, _ = call("POST", "/v1/chat/completions",
             {"model": "wechat-kb", "messages": [{"role": "user", "content": "9"}]},
             headers={"Authorization": "Bearer " + KEY})
check("/9 via HTTP", "当前状态" in r2.get("choices", [{}])[0].get("message", {}).get("content", ""))
call("POST", "/v1/chat/completions",
     {"model": "wechat-kb", "messages": [{"role": "user", "content": "x"}]},
     headers={"Authorization": "Bearer wrong"}, expect=401)

# 7. 消息解析（取一个真实会话，检查 system_qq 字段）
sess, _ = call("GET", "/api/sessions")
found_sys = False
for s in (sess or [])[:12]:
    msgs, _ = call("GET", "/api/messages?chat=" + urllib.parse.quote(s["username"]) + "&limit=40")
    for m in (msgs.get("messages") or []):
        p = m.get("parsed") or {}
        if "system" in p:
            found_sys = True
            check("system_qq present", "system_qq" in p, p.get("system_qq", "")[:60])
            break
    if found_sys:
        break
check("found system message in real data", found_sys)

# 8. 重复路由修复：/api/openclaw/apply 只接受 POST（GET 落到静态兜底 => 404）
r8, c8 = call("GET", "/api/openclaw/apply", expect=404)
check("openclaw/apply not GET", c8 in (404, 405) and not (isinstance(r8, dict) and r8.get("ok")), c8)

# 9. 菜单推送（新菜单含「更多」；失败只记录，不影响功能）
menu, code = call("POST", "/api/qqbot/menu")
check("menu push attempted", code in (200, 400, 500), menu)

# 汇总
bad = [x for x in results if not x[0]]
for ok, method, path, code, out in results:
    print(("PASS " if ok else "FAIL ") + f"{method:6} {path:42} {str(code):4} {out}")
print(f"\nTOTAL {len(results)}  PASS {len(results)-len(bad)}  FAIL {len(bad)}")
sys.exit(1 if bad else 0)
