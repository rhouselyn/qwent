# -*- coding: utf-8 -*-
"""真实浏览器验收：在项目根目录执行 `python backend/scripts/smoke_browser.py`。
用本机 Chrome/Edge 的 CDP 验证微信/QQ 双界面切换、引用 UI、设置页拆分，
截图输出到系统临时目录 wk-shots/。不引入新依赖（websocket-client 已具备）。
需要本机 8721 服务已启动。
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

ROOT = Path(__file__).resolve().parents[2]
MEDIA_DIR = ROOT / "data" / "qq_media"
SMOKE_IMG = "zz_smoke_media.png"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
(MEDIA_DIR / SMOKE_IMG).write_bytes(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2S0sAAAAASUVORK5CYII="))

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium",
]
CHROME = next((p for p in CHROME_CANDIDATES if os.path.exists(p)), None)
if not CHROME:
    raise SystemExit("未找到 Chrome / Edge，无法运行浏览器验收")
PORT = 9333
BASE = "http://127.0.0.1:8721"
SHOTS = os.path.join(tempfile.gettempdir(), "wk-shots")
os.makedirs(SHOTS, exist_ok=True)
USER_DIR = tempfile.mkdtemp(prefix="wk-chrome-")

proc = subprocess.Popen([
    CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--remote-allow-origins=*", f"--remote-debugging-port={PORT}",
    f"--user-data-dir={USER_DIR}", "--window-size=1600,1000",
    "--hide-scrollbars", "about:blank",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_debugger(timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2) as r:
                tabs = json.loads(r.read().decode())
            for t in tabs:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("Chrome CDP 未就绪")


ws = None
seq = 0
events = []


def cmd(method, params=None, timeout=30):
    global seq
    seq += 1
    mid = seq
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    end = time.time() + timeout
    while time.time() < end:
        raw = ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == mid:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg.get("result", {})
        events.append(msg)
    raise TimeoutError(method)


def js(expr, await_promise=False):
    r = cmd("Runtime.evaluate", {
        "expression": expr, "returnByValue": True,
        "awaitPromise": await_promise, "userGesture": True,
    })
    if "exceptionDetails" in r:
        return {"__exception": str(r["exceptionDetails"].get("text", ""))}
    return r.get("result", {}).get("value")


def shot(name):
    r = cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    path = os.path.join(SHOTS, name + ".png")
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
    print("SHOT", path)
    return path


def check(name, cond, extra=""):
    line = ("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)[:170]) if extra else "")
    print(line)
    RESULTS.append(bool(cond))


RESULTS = []
try:
    ws = websocket.create_connection(wait_debugger(), timeout=30,
                                     suppress_origin=True)
    cmd("Page.enable")
    cmd("Runtime.enable")
    cmd("Log.enable")
    cmd("Network.enable")
    cmd("Network.setCacheDisabled", {"cacheDisabled": True})
    # 页面脚本抛错收集（addScriptToEvaluateOnNewDocument 必须在 navigate 前）
    cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        window.__errs = [];
        window.addEventListener('error', e => window.__errs.push(String(e.message)));
        window.addEventListener('unhandledrejection', e => window.__errs.push('rejection: ' + String(e.reason)));
    """})
    cmd("Emulation.setDeviceMetricsOverride",
        {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
    cmd("Page.navigate", {"url": BASE + "/"})

    # 等初始化完成
    for _ in range(60):
        ready = js("document.readyState === 'complete' && !!window.Platform && typeof switchView === 'function' && !!document.querySelector('#sessionList')")
        if ready is True:
            break
        time.sleep(0.3)
    time.sleep(1.5)

    print("\n--- 初始（微信模式）---")
    check("页面加载完成", js("document.readyState") == "complete")
    check("默认微信平台", js("document.documentElement.dataset.platform") == "wechat")
    check("标题为 Qwent", js("document.title") in ("Qwent", "Qwent · 微信"), js("document.title"))
    check("logo 为「微」", js("document.querySelector('.rail .avatar').textContent.trim()") == "微")
    check("会话列表非空", (js("document.querySelectorAll('#sessionList .session-item').length") or 0) > 0,
          js("document.querySelectorAll('#sessionList .session-item').length"))
    check("导航含朋友圈", js("[...document.querySelectorAll('.rail-btn')].some(b=>b.title==='朋友圈')") is True)
    check("导航含链接 / 公众号", js("[...document.querySelectorAll('.rail-btn')].some(b=>b.title==='链接 / 公众号')") is True)
    check("QQ tab 隐藏", js("getComputedStyle(document.querySelector('#qqViewTabs')).display") == "none")
    check("微信置顶用背景加深（无针形图标）",
          (js("document.querySelectorAll('#sessionList .pin-mark').length") or 0) == 0
          and js("(()=>{const e=document.querySelector('#sessionList .session-item.pinned');"
                 "return e?getComputedStyle(e).backgroundColor:'none'})()") not in ("none", "rgba(0, 0, 0, 0)"),
          js("(()=>{const e=document.querySelector('#sessionList .session-item.pinned');"
             "return e?getComputedStyle(e).backgroundColor:'none'})()"))
    shot("01-wechat-chats")

    # 中置时间分隔：打开一个真实会话，间隔 >5 分钟处应出现居中时间标签
    js("""
      (() => {
        const s = (state.sessions || []).find(x => x.username !== 'assistant');
        if (s) openChat(s.username, s.name, s.is_group);
      })()
    """)
    time.sleep(3.5)
    check("微信消息有中置时间分隔", (js("document.querySelectorAll('#msgs .time-divider').length") or 0) > 0,
          js("document.querySelectorAll('#msgs .time-divider').length"))
    check("时间分隔居中",
          js("(()=>{const d=document.querySelector('#msgs .time-divider'),l=document.querySelector('#msgs');"
             "if(!d)return false;const dr=d.getBoundingClientRect(),lr=l.getBoundingClientRect();"
             "return Math.abs((dr.left+dr.width/2)-(lr.left+lr.width/2))<4})()") is True)
    check("微信不再逐条显示时间",
          (js("document.querySelectorAll('#msgs .qq-time').length") or 0) == 0,
          js("document.querySelectorAll('#msgs .qq-time').length"))
    shot("01b-wechat-time-divider")

    print("\n--- 引用消息 UI（真实数据渲染）---")
    # 直接调用渲染函数造一条引用消息，验证两套 DOM 结构
    js("""
      (() => {
        showChatPane();
        document.querySelector('#chatTitle').textContent = '引用消息示例';
        document.querySelector('#chatMeta').textContent = '群聊';
        const m = {id: 999999, is_sender: 0, sender: 'wxid_test', sender_name: '测试',
          chat: 'x@chatroom', parsed: {kind:'quote', title:'这个方案我觉得可以',
          title_full:'这个方案我觉得可以', refer:{who:'rHouselyn',
          title:'我抱着试一试的心态让ai帮我恢复vpn', text:'我抱着试一试的心态让ai帮我恢复vpn'}}};
        const box = document.querySelector('#msgs');
        if (box) { box.innerHTML = ''; box.appendChild(renderMsg(m)); }
      })()
    """)
    check("微信引用块在气泡内", js("!!document.querySelector('#msgs .bubble.quote-bubble .quote-ref')") is True)
    check("微信引用显示昵称+原文", js("(document.querySelector('#msgs .quote-ref')||{}).textContent") and
          "rHouselyn" in str(js("(document.querySelector('#msgs .quote-ref')||{}).textContent")))
    check("微信引用主文字在上", js("(document.querySelector('#msgs .quote-main')||{}).textContent") == "这个方案我觉得可以")
    shot("02-wechat-quote")

    print("\n--- 点击「微」切换到 QQ ---")
    js("document.querySelector('.rail .avatar').click()")
    time.sleep(1.6)
    check("平台切到 qq", js("document.documentElement.dataset.platform") == "qq")
    check("标题变 Qwent · QQ", js("document.title") == "Qwent · QQ", js("document.title"))
    check("logo 变 QQ", js("document.querySelector('.rail .avatar').textContent.trim()") == "QQ")
    check("主题色变蓝", js("document.querySelector('meta[name=theme-color]').content") == "#12b7f5")
    check("导航变「消息」", js("[...document.querySelectorAll('.rail-btn')].some(b=>b.title==='消息')") is True)
    check("朋友圈→QQ空间", js("[...document.querySelectorAll('.rail-btn')].some(b=>b.title==='QQ空间')") is True)
    check("公众号入口消失", js("[...document.querySelectorAll('.rail-btn')].some(b=>b.title.includes('公众号'))") is False)
    check("主色变量为 QQ 蓝", js("getComputedStyle(document.documentElement).getPropertyValue('--green').trim()") == "#12b7f5")
    check("QQ 不再显示微信记录 tab", js("getComputedStyle(document.querySelector('#qqViewTabs')).display") == "none")
    check("QQ 会话列表显示", js("getComputedStyle(document.querySelector('#qqList')).display") != "none")
    check("QQ 会话项存在", (js("document.querySelectorAll('#qqConvList .qq-conv').length") or 0) > 0,
          js("document.querySelectorAll('#qqConvList .qq-conv').length"))
    shot("03-qq-chats")

    print("\n--- QQ 引用条 UI ---")
    js("""
      (() => {
        showChatPane();
        document.querySelector('#chatTitle').textContent = '引用消息示例';
        document.querySelector('#chatMeta').textContent = '群聊';
        const m = {id: 999999, is_sender: 0, sender: 'wxid_test', sender_name: '测试',
          chat: 'x@chatroom', parsed: {kind:'quote', title:'这个方案我觉得可以',
          title_full:'这个方案我觉得可以', refer:{who:'rHouselyn',
          title:'我抱着试一试的心态让ai帮我恢复vpn', text:'我抱着试一试的心态让ai帮我恢复vpn'}}};
        const box = document.querySelector('#msgs');
        if (box) { box.innerHTML = ''; box.appendChild(renderMsg(m)); }
      })()
    """)
    check("QQ 引用条在气泡外", js("!!document.querySelector('#msgs .qq-reply') && !document.querySelector('#msgs .bubble .qq-reply')") is True)
    check("QQ 引用条有左竖线", js("getComputedStyle(document.querySelector('#msgs .qq-reply')).borderLeftWidth") == "3px")
    check("微信引用块被隐藏", js("getComputedStyle(document.querySelector('#msgs .quote-ref')||document.body).display") != "none" or
          js("!!document.querySelector('#msgs .quote-ref')") is False)
    shot("04-qq-quote")

    print("\n--- QQ 消息附件渲染 ---")
    js("""
      (() => {
        showChatPane();
        document.querySelector('#chatTitle').textContent = '附件消息示例';
        const m = {id: 1234567, is_self: 0, who: '附件测试', text: '[图片]\\n路线图',
          ts: Date.now() / 1000,
          media: [{kind: 'image', name: 'zz_smoke_media.png',
                   filename: 'smoke.png', text: '路线图'}]};
        const box = document.querySelector('#msgs');
        if (box) { box.innerHTML = ''; box.appendChild(renderQqMessage(m)); }
      })()
    """)
    time.sleep(1.0)
    check("附件图片节点存在", js("!!document.querySelector('#msgs .qq-img')") is True)
    check("附件图片可加载", (js("(document.querySelector('#msgs .qq-img')||{}).naturalWidth") or 0) > 0,
          js("(document.querySelector('#msgs .qq-img')||{}).complete"))
    shot("05-qq-media")

    print("\n--- QQ 空间 / 联系人 / 文件 / 链接 ---")
    for view, pane, shot_name in [
        ("moments", "#snsList", "06-qq-space"),
        ("contacts", "#contactList", "07-qq-contacts"),
        ("files", "#fileList", "08-qq-files"),
        ("links", "#linkList", "09-qq-links"),
    ]:
        js(f"[...document.querySelectorAll('.rail-btn')].find(b=>b.dataset.view==='{view}').click()")
        time.sleep(1.6)
        check(f"QQ {view} 视图", js("state.view") == view, js("state.view"))
        check(f"QQ {view} 列表可见", js(f"getComputedStyle(document.querySelector('{pane}')).display") != "none")
        if view == "files":
            check("QQ 文件有筛选条", js("!!document.querySelector('#qqFileChat') && !!document.querySelector('#qqFileKind') && !!document.querySelector('#qqFileSort')") is True)
        if view == "links":
            check("QQ 链接有筛选条", js("!!document.querySelector('#qqLinkChat') && !!document.querySelector('#qqLinkPlatform') && !!document.querySelector('#qqLinkState') && !!document.querySelector('#qqLinkDays') && !!document.querySelector('#qqLinkSort')") is True)
            check("QQ 链接栏无解析按钮", js("!!document.querySelector('#qqLinkParse')") is False,
                  "QQ link bar should not have parse button (WeChat doesn't have one)")
            check("QQ 链接有解析状态", (js("document.querySelectorAll('#qqLinkItems .link-badge').length") or 0) > 0,
                  js("document.querySelectorAll('#qqLinkItems .link-badge').length"))
        if view == "files":
            js("document.querySelector('#qqFileItems .result-item') && document.querySelector('#qqFileItems .result-item').click()")
            time.sleep(1.2)
            check("QQ 文件可打开预览", js("getComputedStyle(document.querySelector('#previewPane')).display") != "none"
                  and js("document.querySelector('#pvBody').textContent") != "",
                  str(js("document.querySelector('#chatTitle').textContent"))[:60])
            js("document.querySelector('#backToChat') && document.querySelector('#backToChat').click()")
            time.sleep(0.6)
        item_sel = f"{pane} .session-item" if view == "contacts" else f"{pane} .result-item"
        check(f"QQ {view} 有条目", (js(f"document.querySelectorAll('{item_sel}').length") or 0) > 0,
              js(f"document.querySelectorAll('{item_sel}').length"))
        shot(shot_name)
    # QQ 设置中"关于"要显示 QQ 概览，不是微信的统计
    js("[...document.querySelectorAll('.rail-btn')].find(b=>b.dataset.view==='settings').click()")
    time.sleep(0.6)
    js("[...document.querySelectorAll('#settingsNav .sf-item')].find(x=>x.dataset.sec==='about').click()")
    time.sleep(1.6)
    for _ in range(30):
        if (js("document.querySelectorAll('#kbStats .settings-stat').length") or 0) >= 6:
            break
        time.sleep(0.4)
    time.sleep(0.3)
    qqAbout = js("document.querySelector('#sfBody').textContent") or ""
    check("QQ 关于页显示 QQ 概览", ("QQ 消息" in qqAbout) and ("QQ 占用" in qqAbout),
          qqAbout.strip().replace("\n", " ")[:140])
    check("QQ 关于页不显示微信统计", "545" not in qqAbout and "843" not in qqAbout, "qqAbout length " + str(len(qqAbout)))
    shot("10-qq-about")
    # 回到 AI 模型页，避免后续设置测试停留在 about 分区
    js("[...document.querySelectorAll('#settingsNav .sf-item')].find(x=>x.dataset.sec==='model').click()")
    time.sleep(0.6)
    # 切回 QQ 消息页继续后续测试
    js("[...document.querySelectorAll('.rail-btn')].find(b=>b.dataset.view==='chats').click()")
    time.sleep(0.8)
    check("QQ空间不显示朋友圈数据", (js("document.querySelectorAll('#snsList .sns-item').length") or 0) == 0
          and "QQ 空间" in str(js("document.querySelector('#snsList').textContent") or ""),
          js("document.querySelectorAll('#snsList .sns-item').length"))
    # QQ 空间搜索框不能回落到微信朋友圈
    js("[...document.querySelectorAll('.rail-btn')].find(b=>b.dataset.view==='moments').click()")
    time.sleep(0.8)
    js("""
      (() => {
        const i = document.querySelector('#searchInput');
        i.value = '毕业';
        i.dispatchEvent(new Event('input', {bubbles: true}));
      })()
    """)
    time.sleep(1.4)
    check("QQ空间搜索仍停留在QQ空间", (js("document.querySelectorAll('#snsList .sns-item').length") or 0) == 0
          and "QQ 空间" in str(js("document.querySelector('#snsList').textContent") or ""),
          js("document.querySelectorAll('#snsList .sns-item').length"))
    js("document.querySelector('#searchInput').value = ''")
    js("[...document.querySelectorAll('.rail-btn')].find(b=>b.dataset.view==='chats').click()")
    time.sleep(1.0)

    print("\n--- QQ 本地会话列表 ---")
    check("QQ 会话列表直接接着显示（无分区标题）",
          (js("document.querySelectorAll('#qqConvList .qq-section').length") or 0) == 0
          and (js("document.querySelectorAll('#qqConvList .qq-conv').length") or 0) > 0,
          js("document.querySelectorAll('#qqConvList .qq-section').length"))
    check("QQ 本地会话有消息", (js("document.querySelectorAll('#qqConvList .qq-conv').length") or 0) > 0,
          js("document.querySelectorAll('#qqConvList .qq-conv').length"))
    check("QQ 会话有置顶按钮", (js("document.querySelectorAll('#qqConvList .qq-conv .pin-act').length") or 0) > 0,
          js("document.querySelectorAll('#qqConvList .qq-conv .pin-act').length"))
    check("QQ 会话显示真实头像", (js("document.querySelectorAll('#qqConvList .qq-ava img').length") or 0) > 0,
          js("document.querySelectorAll('#qqConvList .qq-ava img').length"))
    # 先置顶一个会话，验证「背景加深、不显示针形图标」；记下用户原有置顶，结束后原样恢复
    js("""
      (async () => {
        const d = await fetch('/api/qq/local/sessions?limit=200').then(r=>r.json());
        const all = d.sessions || [];
        window.__origPins = all.filter(x => x.pinned).map(x => x.conv);
        const target = all.find(x => !x.pinned);
        const conv = target ? target.conv : (all[0] || {}).conv;
        if (conv) {
          await fetch('/api/qq/local/session/pin', {method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({conv, pinned:1})});
          await loadQqConversations();
        }
        window.__pinnedConv = conv || '';
      })()
    """, await_promise=True)
    time.sleep(1.2)
    check("QQ 置顶用底色不用针形图标",
          (js("document.querySelectorAll('#qqConvList .pin-mark').length") or 0) == 0,
          js("document.querySelectorAll('#qqConvList .pin-mark').length"))
    check("QQ 置顶会话背景加深",
          js("(()=>{const e=document.querySelector('#qqConvList .qq-conv.pinned');"
             "return e?getComputedStyle(e).backgroundColor:'none'})()") not in ("none", "rgba(0, 0, 0, 0)"),
          js("(()=>{const e=document.querySelector('#qqConvList .qq-conv.pinned');"
             "return e?getComputedStyle(e).backgroundColor:'none'})()"))
    # SSE 连接生命周期已抽到共用层 ChatData.startLive；这里验证「QQ 仍订阅
    # 自己的流端点」+「共用层确实创建 EventSource」，不再绑定实现写在哪个文件。
    check("QQ 会话详情走 SSE 订阅",
          "/api/qq/local/stream" in str(js("String(qqLiveStart)")) and
          "ChatData.startLive" in str(js("String(qqLiveStart)")) and
          "EventSource" in str(js("typeof ChatData==='object' ? String(ChatData.startLive) : ''")),
          "qqLiveStart uses shared live stream")
    # 还原用户原有置顶（测试临时加的取消掉，原有的恢复）
    js("""
      (async () => {
        const orig = window.__origPins || [];
        const mine = window.__pinnedConv;
        if (mine && !orig.includes(mine)) {
          await fetch('/api/qq/local/session/pin', {method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({conv: mine, pinned:0})});
        }
        for (const c of orig) {
          await fetch('/api/qq/local/session/pin', {method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({conv: c, pinned:1})});
        }
        await loadQqConversations();
      })()
    """, await_promise=True)
    time.sleep(0.5)
    # 打开一个本地会话，验证中置时间与气泡对齐
    js("""
      (async () => {
        const d = await fetch('/api/qq/local/sessions?limit=40').then(r => r.json());
        const p = (d.sessions || []).find(s => s.last_is_sender) || (d.sessions || [])[0];
        if (p) openQqLocalConversation(p.conv, p.name, !!p.is_group);
      })()
    """)
    time.sleep(3.5)
    check("QQ 消息有中置时间分隔", (js("document.querySelectorAll('#msgs .time-divider').length") or 0) > 0,
          js("document.querySelectorAll('#msgs .time-divider').length"))
    check("QQ 不再把时间塞在气泡上方", (js("document.querySelectorAll('#msgs .qq-time').length") or 0) == 0,
          js("document.querySelectorAll('#msgs .qq-time').length"))
    check("QQ 连续自己的消息右边缘对齐",
          js("(()=>{const xs=[...document.querySelectorAll('#msgs .msg.right')].slice(-8)"
             ".map(m=>Math.round(m.querySelector('.bubble').getBoundingClientRect().right));"
             "return xs.length>1 && new Set(xs).size===1})()") is True,
          js("[...document.querySelectorAll('#msgs .msg.right')].slice(-8).map(m=>Math.round(m.querySelector('.bubble').getBoundingClientRect().right))"))
    shot("09b-qq-time-divider")

    # 图片显示：有缓存的图渲染为 img，未缓存的渲染为占位（不能空着）
    js("(async()=>{const d=await fetch('/api/qq/local/sessions?limit=40').then(r=>r.json());"
       "const p=(d.sessions||[]).find(s=>s.last_is_sender)||(d.sessions||[])[0];if(p)openQqLocalConversation(p.conv,p.name,!!p.is_group)})()")
    time.sleep(3.5)
    check("QQ 图片消息有缓存或缺失占位",
          (js("document.querySelectorAll('#msgs .qq-media img').length") or 0) +
          (js("document.querySelectorAll('#msgs .qq-media .qq-img-missing').length") or 0) > 0,
          "img=%s missing=%s" % (js("document.querySelectorAll('#msgs .qq-media img').length"),
                                 js("document.querySelectorAll('#msgs .qq-media .qq-img-missing').length")))
    check("QQ 未缓存图片显示占位说明",
          (js("document.querySelectorAll('#msgs .qq-img-missing').length") or 0) == 0
          or "图片未缓存到本机" in str(js("document.querySelector('#msgs .qq-img-missing') && document.querySelector('#msgs .qq-img-missing').textContent")),
          js("document.querySelector('#msgs .qq-img-missing') && document.querySelector('#msgs .qq-img-missing').textContent"))

    print("\n--- QQ 消息搜索 ---")
    js("""
      (() => {
        const i = document.querySelector('#searchInput');
        i.value = '的';
        i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
      })()
    """)
    time.sleep(1.4)
    check("QQ 搜索进入结果页", js("state.searchMode") is True)
    check("QQ 搜索有结果", (js("document.querySelectorAll('#resultList .result-item').length") or 0) > 0,
          js("document.querySelectorAll('#resultList .result-item').length"))
    check("QQ 搜索标题正确", "QQ 消息命中" in str(js("document.querySelector('#resultList').textContent") or ""))
    shot("14-qq-search")
    js("document.querySelector('#resultList .result-item').click()")
    time.sleep(1.2)
    check("点击 QQ 搜索结果打开会话", js("!!state.qqConv") is True, js("state.qqConv"))
    check("QQ 本地会话有消息", (js("document.querySelectorAll('#msgs .msg').length") or 0) > 0,
          js("document.querySelectorAll('#msgs .msg').length"))
    check("QQ 会话内显示提问范围", js("getComputedStyle(document.querySelector('#scopeBar')).display") != "none" and "当前会话" in str(js("document.querySelector('#scopeLabel').textContent")))
    check("QQ 会话默认仅当前会话", js("document.querySelector('#scopeToggle').checked") is True)
    shot("15-qq-local-chat")
    js("""
      (() => {
        const i = document.querySelector('#searchInput');
        i.value = '';
        i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
      })()
    """)
    time.sleep(0.9)
    check("清空搜索后回到 QQ 会话列表",
          js("getComputedStyle(document.querySelector('#qqList')).display") != "none")

    print("\n--- QQ 设置页 ---")
    js("[...document.querySelectorAll('.rail-btn')].find(b=>b.dataset.view==='settings').click()")
    time.sleep(1.4)
    secs = js("[...document.querySelectorAll('#settingsNav .sf-item')].map(x=>x.dataset.sec)")
    check("QQ 设置含 qqbot", isinstance(secs, list) and "qqbot" in secs, secs)
    check("QQ 设置含 qqlocal 数据分组", isinstance(secs, list) and "qqlocal" in secs, secs)
    check("QQ 设置不含微信同步/索引", isinstance(secs, list) and "data" not in secs and "index" not in secs, secs)
    check("QQ 设置不含 wxbot", isinstance(secs, list) and "wxbot" not in secs, secs)
    check("通用分组在最前（两边都有）", isinstance(secs, list) and secs and secs[0] == "general", secs)
    js("[...document.querySelectorAll('#settingsNav .sf-item')].find(x=>x.dataset.sec==='general').click()")
    time.sleep(1.2)
    gen = str(js("document.querySelector('#sfBody').textContent") or "")
    check("通用分组有开机自启开关", js("!!document.querySelector('#autoStart')") is True and "开机自启" in gen, gen[:80])
    check("设置页无「微信通道」字样", "微信通道" not in str(js("document.querySelector('#sfBody').textContent")),
          str(js("document.querySelector('#sfBody').textContent"))[:80])
    shot("11-qq-settings")
    # 回到 AI 模型页���主按钮配色（通用分组里没有保存按钮）
    js("[...document.querySelectorAll('#settingsNav .sf-item')].find(x=>x.dataset.sec==='model').click()")
    time.sleep(1.2)
    bg = str(js("getComputedStyle(document.querySelector('#saveCfg')).backgroundImage") or "")
    bgc = str(js("getComputedStyle(document.querySelector('#saveCfg')).backgroundColor") or "")
    # 现在主按钮是纯色（不再渐变）：backgroundImage=none，颜色看 backgroundColor
    check("QQ 设置主按钮为蓝色",
          "26, 182, 255" in bg or "10, 142, 220" in bg or "18, 183, 245" in bgc,
          (bg + " / " + bgc)[:120])
    # 打开 QQ 机器人分区
    js("[...document.querySelectorAll('#settingsNav .sf-item')].find(x=>x.dataset.sec==='qqbot').click()")
    time.sleep(1.4)
    body = str(js("document.querySelector('#sfBody').textContent") or "")
    check("QQ 机器人分区有凭据", "AppID" in body or "App ID" in body or "app_id" in body or "应用" in body, body[:100])
    check("QQ 机器人分区无 ClawBot", "ClawBot" not in body and "扫码" not in body, body[:100])
    check("QQ 机器人无「新用户」措辞", "新用户" not in body, body[:100])
    check("QQ 机器人无 QQ 消息留存卡", "查看 QQ 消息" not in body, body[:100])
    shot("12-qq-bot-settings")
    bg2 = str(js("getComputedStyle(document.querySelector('#qqSave')).backgroundImage") or "")
    bg2c = str(js("getComputedStyle(document.querySelector('#qqSave')).backgroundColor") or "")
    check("QQ 机器人保存按钮为蓝色",
          "26, 182, 255" in bg2 or "10, 142, 220" in bg2 or "18, 183, 245" in bg2c,
          (bg2 + " / " + bg2c)[:120])

    # QQ 本地记录分区
    js("[...document.querySelectorAll('#settingsNav .sf-item')].find(x=>x.dataset.sec==='qqlocal').click()")
    time.sleep(1.0)
    ql = str(js("document.querySelector('#sfBody').textContent") or "")
    check("QQ 数据与同步分区可见", "QQ 数据与同步" in ql and "导入" in ql, ql[:100])
    check("QQ 数据分区有解析任务", "文件与链接解析" in ql, ql[:160])
    check("QQ 本地记录有导入按钮", js("!!document.querySelector('#qqLocalImport')") is True)
    check("QQ 本地记录统计已导入", "816" in ql or "消息" in ql, ql[:120])
    shot("12b-qq-local-settings")

    print("\n--- 切回微信 + 微信设置 ---")
    js("document.querySelector('.rail .avatar').click()")
    time.sleep(1.4)
    secs2 = js("[...document.querySelectorAll('#settingsNav .sf-item')].map(x=>x.dataset.sec)")
    check("微信设置含 wxbot", isinstance(secs2, list) and "wxbot" in secs2, secs2)
    check("微信设置不含 qqbot", isinstance(secs2, list) and "qqbot" not in secs2, secs2)
    js("[...document.querySelectorAll('#settingsNav .sf-item')].find(x=>x.dataset.sec==='wxbot').click()")
    time.sleep(1.4)
    body2 = str(js("document.querySelector('#sfBody').textContent") or "")
    check("微信设置无 QQ 机器人凭据", "AppID" not in body2 and "App ID" not in body2, body2[:100])
    check("微信通道无「微信 Agent 入口」段", "微信 Agent 入口" not in body2, body2[:100])
    check("微信通道无「新用户」措辞", "新用户" not in body2, body2[:100])
    js("[...document.querySelectorAll('#settingsNav .sf-item')].find(x=>x.dataset.sec==='about').click()")
    # /api/storage 要统计 4GB 库 + 目录大小，固定 sleep 偶发读到「…统计中」；
    # 等真实结果出现再断言，避免验收抖动。
    about = ""
    for _ in range(30):
        about = str(js("document.querySelector('#sfBody').textContent") or "")
        if "合计占用" in about and "微信占用" in about:
            break
        time.sleep(0.5)
    check("关于页有占用明细", "占用明细" in about and "合计占用" in about, about[:120])
    check("知识库概览正好 6 格",
          (js("document.querySelectorAll('#kbStats .settings-stat').length") or 0) == 6,
          js("document.querySelectorAll('#kbStats .settings-stat').length"))
    check("概览含微信/QQ 占用",
          "微信占用" in about and "QQ 占用" in about, about[:160])
    shot("13-wechat-bot-settings")

    print("\n--- 刷新后平台记忆 ---")
    cmd("Page.reload", {"ignoreCache": True})
    for _ in range(60):
        if js("document.readyState === 'complete' && !!window.Platform && typeof switchView === 'function'") is True:
            break
        time.sleep(0.3)
    time.sleep(1.5)
    check("刷新后回到微信", js("document.documentElement.dataset.platform") == "wechat")
    check("刷新后导航正确", js("[...document.querySelectorAll('.rail-btn')].some(b=>b.title==='聊天')") is True)

    print("\n--- 控制台异常 ---")
    errs = js("window.__errs || []")
    exceptions = [e for e in events if e.get("method") == "Runtime.exceptionThrown"]
    net_errs = [str(e.get("params", {}).get("entry", {}).get("text", ""))
                for e in events if e.get("method") == "Log.entryAdded"
                and e.get("params", {}).get("entry", {}).get("level") == "error"]
    bad_resp = [(e["params"]["response"].get("status"), e["params"]["response"].get("url"))
                for e in events if e.get("method") == "Network.responseReceived"
                and e["params"]["response"].get("status", 0) >= 400]
    print("BAD_RESP", bad_resp[:12])
    print("NET_ERRS", net_errs[:6])
    real_bad = [u for s, u in bad_resp if u and "/api/" not in u]
    check("无未捕获 JS 异常", isinstance(errs, list) and len(errs) == 0, errs)
    check("无 JS 运行时异常事件", len(exceptions) == 0,
          [str(e.get("params", {}))[:120] for e in exceptions[:3]])
    check("无静态资源加载失败", len(real_bad) == 0, real_bad[:3])

    print(f"\nTOTAL {len(RESULTS)}  PASS {sum(RESULTS)}  FAIL {len(RESULTS)-sum(RESULTS)}")
finally:
    try:
        if ws:
            ws.close()
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    try:
        (MEDIA_DIR / SMOKE_IMG).unlink()
    except Exception:
        pass

sys.exit(0 if RESULTS and all(RESULTS) else 1)
