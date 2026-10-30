/* 微信知识助手 - 前端逻辑 */
const $ = (s) => document.querySelector(s);
const state = {
  view: "chats", sessions: [], current: null,   // current: {username,name,is_group}
  msgs: {}, hasMore: {}, drawerOpen: false, qaHistory: [],
  globalMode: false, searchMode: false,
  quoted: [],            // 引用的消息 [{id, ts, who, text, chat}]
  pinBottom: true,       // 是否贴在最新消息处（图片加载后需要补滚）
};

/* ---------- AI 会话管理 ---------- */
const aiState = {
  tree: [],
  openScopes: {},
  currentSession: null,
  currentScope: null,
};
async function loadAiTree() {
  try { aiState.tree = await api("/api/ai/sessions"); } catch { aiState.tree = []; }
}
function renderAiTree() {
  const box = document.querySelector("#aiTreeInPanel");
  if (!box) return;
  box.innerHTML = "";
  if (!aiState.tree.length) {
    box.innerHTML = `<div class="qa-empty">还没有 AI 会话。</div>`;
    return;
  }
  for (const g of aiState.tree) {
    const wrap = document.createElement("div");
    wrap.className = "ai-group";
    // 默认展开：全局助手 + 当前会话所在的分组；其余收起，点分组头可切换
    const hasActive = g.sessions.some(s => s.id === aiState.currentSession);
    const open = aiState.openScopes[g.scope] ?? (g.scope === "assistant" || hasActive);
    const tag = g.scope === "assistant" ? '<span class="tag">全局</span>'
      : g.kind === "file" ? '<span class="tag file">文件</span>'
      : g.kind === "link" ? '<span class="tag link">链接</span>'
      : g.is_group ? '<span class="tag">群</span>' : "";
    wrap.innerHTML = `
      <div class="ai-group-head">
        <span class="caret">${open ? "▾" : "▸"}</span>
        <span class="gname">${esc(g.name)}</span>
        ${tag}
        <span class="cnt">${g.sessions.length} 个对话</span>
      </div>
      <div class="ai-sess-list" style="display:${open ? "block" : "none"}"></div>`;
    const list = wrap.querySelector(".ai-sess-list");
    for (const s of g.sessions) {
      const d = document.createElement("div");
      d.className = "ai-sess" + (aiState.currentSession === s.id ? " active" : "");
      d.innerHTML = `<span class="t">${esc(s.title || "新对话")}</span>
                     <span class="n">${s.count} 问</span>
                     <span class="del" title="删除该对话">✕</span>`;
      d.querySelector(".t").onclick = () => {
        aiState.currentSession = s.id;
        loadSessionTurns(s.id);
        renderAiTree();
      };
      d.querySelector(".n").onclick = () => d.querySelector(".t").click();
      d.querySelector(".del").onclick = async (e) => {
        e.stopPropagation();
        await api(`/api/ai/session/${s.id}/delete`, { method: "POST" });
        if (aiState.currentSession === s.id) { aiState.currentSession = null; state.qaHistory = []; renderQA(); }
        await loadAiTree(); renderAiTree();
        toast("已删除对话");
      };
      list.appendChild(d);
    }
    // 点分组头：展开 / 收起该分组下的会话
    const head = wrap.querySelector(".ai-group-head");
    head.onclick = (e) => {
      e.stopPropagation();
      aiState.openScopes[g.scope] = !open;
      renderAiTree();
    };
    box.appendChild(wrap);
  }
}
async function loadSessionTurns(sid) {
  if (!sid) { state.qaHistory = []; renderQA(); return; }
  try {
    const d = await api(`/api/ai/session/${sid}`);
    state.qaHistory = d.turns;
  } catch { state.qaHistory = []; }
  renderQA();
}
async function newAiSession() {
  const sc = (typeof aiScopeForContext === "function") ? aiScopeForContext() : null;
  const scope = sc ? sc.key : (state.current ? state.current.username : "assistant");
  const r = await api("/api/ai/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, name: sc ? sc.name : "" }),
  });
  aiState.currentSession = r.id;
  state.qaHistory = [];
  renderQA();
  await loadAiTree(); renderAiTree();
  toast("已新建会话");
}


const AVA_COLORS = ["#5b8ff9","#61ddaa","#65789b","#f6bd16","#7262fd","#78d3f8","#9661bc","#f6903d","#008685","#f08bb4"];
function hue(s) { let h = 0; for (const c of s || "") h = (h * 31 + c.charCodeAt(0)) % 997; return AVA_COLORS[h % AVA_COLORS.length]; }
function initial(name) { return (name || "?").trim().charAt(0).toUpperCase(); }
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const t = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (sameDay) return t;
  const sameYear = d.getFullYear() === now.getFullYear();
  return `${d.getMonth() + 1}月${d.getDate()}日` + (sameYear ? "" : ` ${d.getFullYear()}`) + ` ${t}`;
}
function maskSecrets(t) {
  return String(t || "")
    .replace(/sk-[A-Za-z0-9_-]{8,}/g, "sk-****")
    .replace(/(ghp|gho|github_pat)_[A-Za-z0-9_]{10,}/g, "$1_****")
    .replace(/(AKIA|ASIA)[A-Z0-9]{12,}/g, "$1****")
    .replace(/((?:api[_-]?key|token|password|passwd|secret)\s*[:=]\s*)[^\s,;]{6,}/gi, "$1****");
}

function fmtSize(n) { if (!n) return ""; const u = ["B","KB","MB","GB"]; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + u[i]; }

/* 剥掉模型泄漏的 DSML 调用标签（deepseek-reasoner/flash 等不支持原生 function calling） */
function stripDsml(text) {
  return String(text || "").replace(/<\/?[｜|]{0,2}\s*\/?\s*DSML[｜|]{0,2}[^>]*>/g, "").trim();
}

/* 剥掉模型自己在答案末尾写的「引用来源：[1]…[2]…」清单——正文里的 [n] 本身就是可点击链接 */
function stripRefFooter(text) {
  let t = String(text || "");
  for (let i = 0; i < 3; i++) {
    const m = t.match(/\n+[ \t]*(?:\*\*)?(?:引用来源|参考来源|参考资料|参考消息|出处|来源)(?:\*\*)?[ \t]*[:：]?[ \t]*\n(?:[ \t]*(?:[-*+][ \t]*)?\[\d{1,3}\][^\n]*\n?)*\s*$/);
    if (!m) break;
    if (!t.slice(0, m.index).trim()) return String(text || "").replace(/\s+$/, "");
    t = t.slice(0, m.index);
  }
  return t.replace(/\s+$/, "");
}

function mdToHtml(text) {
  const src = (text || "").split("\r\n").join("\n");
  const codeBlocks = [];
  let t = src.replace(/```[a-zA-Z0-9]*\n([\s\S]*?)```/g, (_, code) => {
    codeBlocks.push('<pre class="md-pre"><code>' + esc(code.replace(/\n$/, "")) + "</code></pre>");
    return "\u0001B" + (codeBlocks.length - 1) + "\u0001";
  });
  const inline = (x) => {
    // 先把行内代码/Markdown 链接收进占位符，避免 URL 自动加链与 [n] 引用误伤已生成的 HTML
    const keep = [];
    const hold = (html) => "\u0001K" + (keep.push(html) - 1) + "\u0001";
    let s = esc(x)
      .replace(/`([^`]+)`/g, (_, c) => hold('<code class="md-code">' + c + "</code>"))
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
               (_, txt, url) => hold('<a href="' + url + '" target="_blank" rel="noopener">' + txt + "</a>"))
      .replace(/\[(\d{1,3})\]/g, '<span class="md-ref" data-ref="$1">[$1]</span>')
      .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    return s.replace(/\u0001K(\d+)\u0001/g, (_, i) => keep[+i]);
  };
  const out = [];
  let list = null, para = [];
  const flushPara = () => {
    if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; }
  };
  const flushList = () => {
    if (list) {
      out.push("<" + list.tag + ">" +
        list.items.map(i => "<li>" + inline(i) + "</li>").join("") +
        "</" + list.tag + ">");
      list = null;
    }
  };
  for (const raw of t.split("\n")) {
    const line = raw.trimEnd();
    const blk = line.trim().match(/^\u0001B(\d+)\u0001$/);
    if (blk) { flushPara(); flushList(); out.push(codeBlocks[+blk[1]]); continue; }
    if (!line.trim()) { flushPara(); flushList(); continue; }
    // Markdown 表格：| a | b |  +  |---|---|
    if (/^\s*\|.*\|\s*$/.test(line)) {
      flushPara(); flushList();
      const cells = line.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const last = out[out.length - 1];
      if (last && last.startsWith("<table")) {
        if (/^[-: ]+$/.test(cells.join(""))) { continue; }   // 分隔行跳过
        out[out.length - 1] = last.replace("</tbody></table>",
          "<tr>" + cells.map(c => "<td>" + inline(c) + "</td>").join("") + "</tr></tbody></table>");
      } else {
        out.push("<table class=\"md-table\"><thead><tr>" +
          cells.map(c => "<th>" + inline(c) + "</th>").join("") +
          "</tr></thead><tbody></tbody></table>");
      }
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flushPara(); flushList();
      const lv = Math.min(h[1].length + 2, 6);
      out.push("<h" + lv + ">" + inline(h[2]) + "</h" + lv + ">");
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      flushPara();
      if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; }
      list.items.push(line.replace(/^\s*[-*+]\s+/, "")); continue;
    }
    if (/^\s*\d+[.)]\s+/.test(line)) {
      flushPara();
      if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; }
      list.items.push(line.replace(/^\s*\d+[.)]\s+/, "")); continue;
    }
    if (/^\s*>\s?/.test(line)) {
      flushPara(); flushList();
      out.push("<blockquote>" + inline(line.replace(/^\s*>\s?/, "")) + "</blockquote>");
      continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { flushPara(); flushList(); out.push("<hr>"); continue; }
    para.push(line);
  }
  flushPara(); flushList();
  return out.join("\n");
}

function esc(s) { return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 2500); }
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || r.statusText); }
  return r.json();
}

const PANES = ["#sessionList", "#resultList", "#contactList", "#fileList",
               "#linkList", "#settingsPane", "#aiSessionList"];
function showPane(id) {
  PANES.forEach(p => { const el = $(p); if (el) el.style.display = "none"; });
  if (id) { const el = $(id); if (el) el.style.display = "block"; }
}

/* ---------------- 会话列表 ---------------- */
async function loadSessions() {
  state.sessions = await api("/api/sessions");
  renderSessions();
}
function renderSessions(filter = "") {
  const box = $("#sessionList");
  box.innerHTML = "";
  const mkItem = (html, cls, onclick) => {
    const d = document.createElement("div");
    d.className = "session-item " + cls; d.innerHTML = html; d.onclick = onclick;
    return d;
  };
  // 置顶 AI 助手
  box.appendChild(mkItem(
    `<div class="ava">✦</div>
     <div class="info"><div class="row1"><span class="name">AI 助手<b class="badge">置顶</b></span></div>
     <div class="row2">全局问答：所有聊天记录 + 随时追问</div></div>`,
    "ai", () => openAssistant()));
  const list = state.sessions.filter(s =>
    !filter || s.name.toLowerCase().includes(filter.toLowerCase()) || s.username.includes(filter));
  const pinned = list.filter(s => s.pinned);
  const rest = list.filter(s => !s.pinned);
  const add = (s) => {
    const item = mkItem(
      `${avatarImgHtml(s.username, s.name)}
       <div class="info"><div class="row1"><span class="name">${esc(s.name)}${
         s.pinned ? '<span class="pin-mark">📌</span>' : ""}</span>
       <span class="time">${s.last_ts ? fmtTime(s.last_ts).split(" ").pop() : ""}</span></div>
       <div class="row2">${esc(maskSecrets(s.summary) || `${s.cnt} 条消息`)}</div></div>
       <span class="pin-act" title="${s.pinned ? "取消置顶" : "置顶"}">${s.pinned ? "⊖" : "📌"}</span>`,
      state.current && state.current.username === s.username ? "active" : "",
      () => openChat(s.username, s.name, s.is_group));
    // 右键菜单：置顶、取消置顶、复制标识
    item.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();          // 避免事件继续冒泡到 document 把菜单关掉
      showSessionMenu(e.clientX, e.clientY, s);
    });
    item.dataset.username = s.username;
    const pinBtn = item.querySelector(".pin-act");
    pinBtn.onclick = async (e) => {
      e.stopPropagation();
      await api("/api/sessions/pin", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: s.username, pinned: s.pinned ? 0 : 1 }),
      });
      s.pinned = s.pinned ? 0 : 1;
      await loadSessions();
      toast(s.pinned ? "已置顶" : "已取消置顶");
    };
    box.appendChild(item);
  };
  pinned.forEach(add);
  rest.forEach(add);
}

/* ---------------- 右键菜单 ---------------- */
function showSessionMenu(x, y, s) {
  closeSessionMenu();
  const menu = document.createElement("div");
  menu.id = "ctxMenu";
  menu.className = "ctx-menu";
  const items = [
    s.pinned
      ? { label: "取消置顶", fn: () => setPin(s, 0) }
      : { label: "置顶会话", fn: () => setPin(s, 1) },
    { label: "打开会话", fn: () => openChat(s.username, s.name, s.is_group) },
    { label: "只看该会话的文件", fn: () => { showFiles(""); } },
    { label: "复制会话标识", fn: () => { navigator.clipboard && navigator.clipboard.writeText(s.username); toast("已复制 " + s.username); } },
  ];
  menu.innerHTML = items.map((it, i) =>
    `<div class="ctx-item" data-i="${i}">${esc(it.label)}</div>`).join("");
  menu.style.left = Math.min(x, window.innerWidth - 190) + "px";
  menu.style.top = Math.min(y, window.innerHeight - 160) + "px";
  document.body.appendChild(menu);
  menu.querySelectorAll(".ctx-item").forEach((el) => {
    el.onclick = () => { const it = items[+el.dataset.i]; closeSessionMenu(); it && it.fn(); };
  });
  setTimeout(() => document.addEventListener("mousedown", closeSessionMenu, { once: true }), 0);
}

function closeSessionMenu() {
  const m = document.getElementById("ctxMenu");
  if (m) m.remove();
}

async function setPin(s, pinned) {
  await api("/api/sessions/pin", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: s.username, pinned }),
  });
  s.pinned = pinned;
  await loadSessions();
  toast(pinned ? "已置顶" : "已取消置顶");
}

/* ---------------- 打开会话 ---------------- */
async function openChat(username, name, isGroup, sessionId) {
  state.current = { username, name, is_group: isGroup };
  state.searchMode = false;
  if (previewFile) exitPreview();
  // 离开链接预览时清掉上下文，避免后续提问被当成"针对本文"
  if (typeof previewLink !== "undefined") previewLink = null;
  $("#previewPane").style.display = "none";
  $("#msgList").style.display = "block";
  $(".composer #input").placeholder = "问 AI（Enter 发送，Shift+Enter 换行）";
  showPane("#sessionList");
  renderSessions($("#searchInput").value);
  showChatPane();
  $("#assistantPane").style.display = "none";
  $("#aiSessionsPanel").style.display = "none";
  $("#settingsFull").style.display = "none";
  document.querySelector(".chat-column").style.display = "flex";
  $("#drawer").style.display = "flex";
  $("#chatTitle").textContent = name;
  $("#chatMeta").textContent = isGroup ? "群聊" : "私聊";
  $("#msgList").onscroll = onMsgScroll;
  $("#msgs").innerHTML = "";
  state.msgs[username] = [];
  state.pinBottom = true;
  await loadMessages(username, { initial: true });
  startLiveStream(username);          // 新消息秒级推送（SSE）
  // 问答抽屉默认打开，默认范围＝本会话
  $("#scopeToggle").checked = true;
  toggleDrawer(true);
  updateScopeBar();
  updateDrawerScope();
  // 显示该会话自己的最新 AI 对话（不与其他会话/文件/链接混用）
  await enterAiScope(username, name);
}
function msgScroller() { return document.querySelector("#msgList"); }
function scrollMsgsToBottom() {
  const sc = msgScroller();
  if (sc) sc.scrollTop = sc.scrollHeight;
}

/* ---------------- 新消息实时推送（SSE，不轮询） ---------------- */
const liveState = { es: null, chat: null };

function stopLiveStream() {
  if (liveState.es) { try { liveState.es.close(); } catch {} }
  liveState.es = null; liveState.chat = null;
}

function startLiveStream(chat) {
  if (liveState.chat === chat && liveState.es) return;   // 已在同一个会话订阅
  stopLiveStream();
  liveState.chat = chat;
  const box0 = state.msgs[chat] || [];
  const afterId = box0.length ? box0[box0.length - 1].id : 0;
  const es = new EventSource(`/api/messages/stream?chat=${encodeURIComponent(chat)}&after_id=${afterId}`);
  liveState.es = es;
  es.onmessage = (ev) => {
    let d = null;
    try { d = JSON.parse(ev.data); } catch { return; }
    if (!d || !d.messages) return;
    if (!state.current || state.current.username !== chat) return;
    const have = new Set((state.msgs[chat] || []).map(m => m.id));
    const fresh = d.messages.filter(m => !have.has(m.id));
    if (!fresh.length) return;
    state.msgs[chat].push(...fresh);
    // 只有"正在看这个会话的聊天记录"时才插入 DOM（预览/助手视图不打断）
    const viewingChat = $("#msgList").style.display !== "none"
      && $("#previewPane").style.display === "none" && !isAssistant();
    if (viewingChat) {
      const msgsBox = $("#msgs");
      const frag = document.createDocumentFragment();
      fresh.forEach(m => frag.appendChild(renderMsg(m)));
      msgsBox.appendChild(frag);
      if (state.pinBottom) scrollMsgsToBottom();
    }
    // 会话列表里的最后一条预览 + 排序实时更新
    const last = fresh[fresh.length - 1];
    const s = (state.sessions || []).find(x => x.username === chat);
    if (s) {
      s.last_ts = last.ts;
      s.summary = last.parsed && last.parsed.text ? last.parsed.text.slice(0, 30) : s.summary;
      state.sessions.sort((a, b) => (b.pinned - a.pinned) ||
        ((b.last_ts || 0) - (a.last_ts || 0)));
      if (!state.searchMode) renderSessions($("#searchInput").value);
    }
    if (typeof onNewMessages === "function") onNewMessages(chat, fresh);
  };
  es.onerror = () => { /* EventSource 自带重连，这里不主动关闭 */ };
}

/* 进入会话/跳转时启动订阅 */
function startLivePolling(chat) { startLiveStream(chat); }

async function loadMessages(username, { initial = false, before = 0 } = {}) {
  const qs = `chat=${encodeURIComponent(username)}&limit=40` + (before ? `&before=${before}` : "");
  const data = await api("/api/messages?" + qs);
  const box = $("#msgs");
  const sc = msgScroller();
  const frag = document.createDocumentFragment();
  const oldHeight = sc ? sc.scrollHeight : 0;
  data.messages.forEach(m => frag.appendChild(renderMsg(m)));
  if (before) {
    box.prepend(frag);
    state.msgs[username] = [...data.messages, ...state.msgs[username]];
    // 加载更早消息时保持当前视口位置
    if (sc) sc.scrollTop = sc.scrollHeight - oldHeight;
  }
  else {
    box.appendChild(frag);
    state.msgs[username] = data.messages;
    if (initial) {
      scrollMsgsToBottom();              // 进入会话默认停在最新日期
      [80, 300, 800, 1500].forEach(ms => setTimeout(() => {
        if (state.pinBottom) scrollMsgsToBottom();
      }, ms));
    }
  }
  state.hasMore[username] = data.has_more;
  $("#loadMore").style.display = data.has_more ? "block" : "none";
}
function onMsgScroll() {
  const sc = msgScroller();
  if (sc) {
    state.pinBottom = sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 40;
  }
  const box = $("#msgs");
  if (box.parentElement.scrollTop < 60 && state.current && state.hasMore[state.current.username] && !loadMessages._busy) {
    loadMessages._busy = true;
    loadMessages(state.current.username, { before: state.msgs[state.current.username][0].id })
      .finally(() => loadMessages._busy = false);
  }
}
function showChatPane() {
  $("#chatEmpty").style.display = "none";
  $("#chatMain").style.display = "flex";
}

/* ---------------- 消息渲染 ---------------- */
/* 真实头像：微信 head_image.db 里有就显示，没有/加载失败回落字母块 */
function avatarImgHtml(username, name, extraStyle) {
  const letter = esc(initial(name || username || "?"));
  const bg = `background:${hue(username || name || "")};`;
  const style = extraStyle || bg;
  if (!username) return `<div class="ava" style="${style}">${letter}</div>`;
  return `<div class="ava" style="${style}">
      <img class="ava-img" src="/api/avatar?u=${encodeURIComponent(username)}" alt=""
           loading="lazy" onerror="this.style.display='none';this.parentNode.classList.add('ava-letter')">
      <span class="ava-fallback">${letter}</span></div>`;
}

function avatarFor(m) {
  const d = document.createElement("div");
  d.className = "ava";
  const username = m.is_sender ? (state.selfUsername || "") : (m.sender || "");
  const name = m.is_sender ? "我" : m.sender_name;
  d.innerHTML = (username
    ? `<img class="ava-img" src="/api/avatar?u=${encodeURIComponent(username)}" alt=""
            loading="lazy" onerror="this.style.display='none';this.parentNode.classList.add('ava-letter')">
       <span class="ava-fallback">${esc(initial(name))}</span>`
    : `<span class="ava-fallback">${esc(initial(name))}</span>`);
  if (!m.is_sender) d.style.background = hue(m.sender || m.sender_name);
  return d;
}
function renderMsg(m) {
  const p = m.parsed || {};
  if (p.system !== undefined) {
    const d = document.createElement("div");
    d.className = "sys"; d.textContent = p.system; d.dataset.id = m.id;
    return d;
  }
  const wrap = document.createElement("div");
  wrap.className = "msg " + (m.is_sender ? "right" : "left");
  wrap.dataset.id = m.id;
  wrap.appendChild(avatarFor(m));
  const qa = document.createElement("span");
  qa.className = "quote-act";
  qa.textContent = "引用";
  qa.onclick = () => addQuote(m);
  wrap.appendChild(qa);
  const col = document.createElement("div"); col.className = "col";
  if (state.current && state.current.is_group && !m.is_sender) {
    const who = document.createElement("div"); who.className = "who"; who.textContent = m.sender_name;
    col.appendChild(who);
  }
  const b = document.createElement("div"); b.className = "bubble";
  b.appendChild(renderBubbleContent(m, p));
  col.appendChild(b);
  wrap.appendChild(col);
  return wrap;
}
function renderBubbleContent(m, p) {
  const el = document.createElement("div");
  if (p.text !== undefined) { el.textContent = p.text; return el; }
  switch (p.kind) {
    case "image": {
      const img = document.createElement("img");
      img.className = "thumb"; img.alt = "[图片]";
      img.loading = "lazy";
      img.src = `/api/media/image?chat=${encodeURIComponent(m.chat)}&id=${m.id}`;
      img.onload = () => { if (state.pinBottom) scrollMsgsToBottom(); };
      img.onerror = () => { img.className = "fail"; img.removeAttribute("src"); img.textContent = "🖼 图片已过期或未下载"; };
      img.onclick = () => openImageViewer(m, img.src);
      el.appendChild(img);
      return el;
    }
    case "voice": {
      const sec = Math.round((p.voice_ms || 0) / 1000);
      const wrap = document.createElement("div");
      wrap.className = "voice-wrap";
      const play = document.createElement("div");
      play.className = "voice-play";
      play.innerHTML = `<span>▶</span><span>语音 ${sec || ""}"</span>`;
      play.onclick = () => {
        const au = new Audio(`/api/media/voice?chat=${encodeURIComponent(m.chat)}&id=${m.id}`);
        au.play().catch(() => toast("语音无法解码"));
      };
      wrap.appendChild(play);
      if (p.asr_text) {
        const t = document.createElement("div");
        t.className = "voice-text";
        t.textContent = p.asr_text;
        wrap.appendChild(t);
      }
      return wrap;
    }
    case "video": el.innerHTML = `🎬 <b>[视频]</b>`; return el;
    case "emoji": el.innerHTML = `😊 <span style="color:#8a8a90">[动画表情]</span>`; return el;
    case "card": el.innerHTML = `👤 <b>[名片]</b>`; return el;
    case "voip": el.innerHTML = `📞 <span style="color:#8a8a90">[通话]</span>`; return el;
    case "location": el.textContent = `📍 ${p.text || "[位置]"}`; return el;
    case "quote": {
      el.className = "quote";
      const ref = document.createElement("div"); ref.className = "ref";
      ref.textContent = `${p.refer?.who || "引用"}：${p.refer?.title || ""}`;
      const t = document.createElement("div"); t.textContent = p.title || "[消息]";
      el.appendChild(ref); el.appendChild(t);
      return el;
    }
    case "file": {
      el.className = "card";
      el.innerHTML = `📄 <span class="t">${esc(p.title || "[文件]")}</span>
        <div class="d">${p.size ? fmtSize(p.size) : ""} · 点击预览</div>`;
      el.style.cursor = "pointer";
      el.onclick = () => openFilePreview({
        chat: m.chat, id: m.src_local_id || m.id, ts: m.ts,
        title: p.title || "[文件]", chat_name: m.chat_name, size: p.size || 0,
      });
      return el;
    }
    case "link": {
      el.className = "card";
      el.innerHTML = `🔗 <span class="t">${esc(p.title || "[链接]")}</span>
        ${p.des ? `<div class="d">${esc(p.des)}</div>` : ""}
        ${p.url ? `<div class="u">${esc(p.url)}</div>` : ""}
        ${p.url ? '<div class="d" style="color:#07a150">点击阅读正文</div>' : ""}`;
      if (p.url) {
        el.style.cursor = "pointer";
        el.onclick = () => openLinkPreview(p.url, p.title || "网页");
      }
      return el;
    }
    default: el.textContent = p.text || "[不支持的消息]"; return el;
  }
}

async function ensureSessions() {
  if (!state.sessions || !state.sessions.length) {
    state.sessions = await api("/api/sessions");
  }
}

/* ---------------- 引用消息 ---------------- */
function addQuote(m) {
  const p = m.parsed || {};
  const text = p.text || p.title || (p.kind === "image" ? "[图片]" : p.kind === "voice" ? "[语音]" : "[消息]");
  if (state.quoted.some(q => q.id === m.id)) return;
  state.quoted.push({ id: m.id, ts: m.ts, who: m.sender_name, text: String(text).slice(0, 200),
                      chat: m.chat });
  renderQuoteBar();
}
function renderQuoteBar() {
  const bar = $("#quoteBar");
  if (!state.quoted.length) { bar.style.display = "none"; return; }
  bar.style.display = "flex";
  $("#quoteCount").textContent = `(${state.quoted.length} 条)`;
  $("#quoteList").innerHTML = state.quoted.map((q, i) =>
    `<span class="quote-chip" title="${esc(q.text)}" data-i="${i}">
       <span class="quote-chip-text">${i + 1}. ${esc(q.who)}: ${esc(q.text.slice(0, 40))}</span>
       <b class="quote-x" title="移除这条引用">✕</b></span>`
  ).join("");
  $("#quoteList").querySelectorAll(".quote-x").forEach(x => {
    x.onclick = (e) => {
      e.stopPropagation();
      e.preventDefault();
      const chip = x.closest(".quote-chip");
      const i = +chip.dataset.i;
      state.quoted.splice(i, 1);
      renderQuoteBar();
    };
  });
}
$("#quoteClear").onclick = () => { state.quoted = []; renderQuoteBar(); };

/* ---------------- 问答抽屉 ---------------- */
function toggleDrawer(force) {
  // 右侧 AI 结果列常显；保留函数以兼容旧调用
  const d = $("#drawer");
  if (!d) return;
  d.style.display = "flex";
  state.drawerOpen = true;
  updateScopeBar();
  updateDrawerScope();
}
function isAssistant() { return state.current && state.current.username === "assistant"; }
/* 输入框上方的提问范围勾选：勾选=仅在当前会话/文件内检索，取消=全局；跟随上下文动态显示 */
function updateScopeBar() {
  const bar = $("#scopeBar");
  if (!bar) return;
  if (isAssistant() || !state.current) { bar.style.display = "none"; return; }
  bar.style.display = "flex";
  $("#scopeLabel").textContent = previewFile
    ? `仅在文件「${previewFile.title || "当前文件"}」内提问`
    : previewLink
    ? `仅在「${$("#chatTitle").textContent || "本文"}」内提问`
    : `仅在「${state.current.name || state.current.username}」内提问`;
}
function updateDrawerScope() {
  const scoped = $("#scopeToggle").checked;
  $("#drawerScope").textContent =
    previewFile ? (scoped ? "范围：此文件" : "范围：全局")
    : previewLink ? (scoped ? "范围：本文" : "范围：全局")
    : (scoped ? "范围：本会话" : "范围：全局");
}
async function loadQA(chat) {
  // 助手页内部用 __assistant__ 标识，后端统一存为 assistant
  const key = chat === "assistant" ? "assistant" : chat;
  try {
    state.qaHistory = await api(`/api/qa?chat=${encodeURIComponent(key)}`);
  } catch { state.qaHistory = []; }
  renderQA();
}
function renderQA() {
  const box = isAssistant() ? $("#assistantConv") : $("#qaList");
  box.innerHTML = "";
  if (!state.qaHistory.length) {
    box.innerHTML = `<div class="qa-empty">还没有问答记录。<br>在下方输入框提问，AI 会在${isAssistant() ? "全部聊天记录" : "当前会话"}中检索作答。</div>`;
    return;
  }
    for (const turn of state.qaHistory) {
    const d = document.createElement("div");
    d.className = "qa-turn";
    const rawAns = String(turn.answer || "");
    const cleanAns = stripRefFooter(stripDsml(rawAns));
    const ansHtml = cleanAns ? mdToHtml(cleanAns)
      : (stripDsml(rawAns) !== rawAns.trim()
         ? "<i style='color:#9a9aa0'>（该轮回答是模型输出乱码，已自动清理）</i>" : "");
    d.innerHTML = `
      <div class="qa-head">
        <span class="mode">${
          turn.mode === "agent" || turn.mode === "llm" || turn.mode === "stream" ? "AI 回答"
          : turn.mode === "fast" ? "快速回答"
          : turn.mode === "link" ? "本文问答"
          : turn.mode === "file" ? "文件问答" : "检索结果"}</span>
        <span class="qa-time">${turn.ts ? fmtTime(turn.ts) : ""}</span>
      </div>
      <div class="q">${esc(turn.question)}</div>
      <div class="a md">${ansHtml}</div>`;
    // 正文里的 [n] 是可点击引用；文件类来源以 chips 附在答案后；下方保留可展开的引用来源合集
    const ansEl = d.querySelector(".a");
    if (typeof bindRefLinks === "function") bindRefLinks(ansEl, turn.refs);
    if (typeof attachSourceFiles === "function") attachSourceFiles(d, turn.refs);
    if (typeof insertRefsWrap === "function") insertRefsWrap(d, turn.refs);
    if (turn.trace && turn.trace.length) {
      const tw = document.createElement("div");
      tw.className = "trace-wrap";
      tw.innerHTML = `<div class="trace-toggle">思考过程 · ${turn.trace.length} 步 ▸</div>`;
      const tl = document.createElement("div");
      tl.className = "trace-list";
      tl.style.display = "none";
      tl.innerHTML = turn.trace.map((t, i) =>
        `<div class="trace-step">${i + 1}. <b>${esc(t.tool)}</b> ${esc(t.summary || "")}</div>`).join("");
      tw.querySelector(".trace-toggle").onclick = () => {
        const open = tl.style.display !== "none";
        tl.style.display = open ? "none" : "flex";
        tw.querySelector(".trace-toggle").textContent =
          `思考过程 · ${turn.trace.length} 步 ${open ? "▸" : "▾"}`;
      };
      tw.appendChild(tl);
      d.appendChild(tw);
    }
    box.appendChild(d);
  }
  box.scrollTop = box.scrollHeight;
}

/* ---------------- AI 助手的作用域（仅本会话） ---------------- */
function scopeNameOf(scope) {
  if (!scope || scope === "assistant") return "";
  const s = state.sessions.find(x => x.username === scope);
  return s ? s.name : scope;
}

function updateAssistantScopeUI() {
  const wrap = document.querySelector("#assistantScopeWrap");
  if (!wrap) return;
  const scope = aiState.currentScope;
  if (!scope || scope === "assistant") {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "inline";
  const nm = document.querySelector("#assistantScopeName");
  if (nm) nm.textContent = "（" + scopeNameOf(scope) + "）";
}

/* 当前上下文的 AI 会话作用域：群聊/私聊、每个文件、每个链接各自独立，互不混用 */
function hashStr(s) {
  let h = 5381;
  const t = String(s || "");
  for (let i = 0; i < t.length; i++) h = ((h << 5) + h + t.charCodeAt(i)) & 0x7fffffff;
  return h.toString(36);
}

function aiScopeForContext() {
  if (previewFile) {
    return { key: `file:${previewFile.chat}:${previewFile.id || 0}`,
             name: previewFile.title || "文件",
             kind: "file" };
  }
  if (typeof previewLink !== "undefined" && previewLink) {
    return { key: `link:${hashStr(previewLink.url)}`,
             name: previewLink.title || "链接",
             kind: "link" };
  }
  if (isAssistant() || !state.current) return { key: "assistant", name: "全局助手", kind: "assistant" };
  return { key: state.current.username, name: state.current.name || state.current.username,
           kind: "chat" };
}

/* 切到某个上下文时，载入"该作用域自己的"最新会话（绝不沿用别的上下文的 session） */
async function enterAiScope(scopeKey, name) {
  aiState.currentScope = scopeKey;
  await loadAiTree();
  const g = aiState.tree.find(x => x.scope === scopeKey);
  const target = g && g.sessions.length ? g.sessions[0].id : null;
  aiState.currentSession = target;
  await loadSessionTurns(target);
  renderAiTree();
}

/* 当前提问的检索范围：返回 chat username 或 null（全局）；文件/链接上下文由 payload 单独带 */
function currentAskScope() {
  if (isAssistant()) {
    const scope = aiState.currentScope;
    const wrap = document.querySelector("#assistantScopeWrap");
    const on = wrap && wrap.style.display !== "none";
    const checked = document.querySelector("#assistantScopeToggle");
    if (on && scope && scope !== "assistant" && (!checked || checked.checked)) return scope;
    return null;
  }
  return $("#scopeToggle").checked ? state.current.username : null;
}

async function ask(question) {
  if (!question || !question.trim()) return;
  if (typeof askStream === "function") return askStream(question.trim());
  toast("流式模块未加载");
}

/* ---------------- 引用跳转 ---------------- */
async function jumpToRef(r) {
  const wasAssistant = isAssistant() ||
    document.querySelector("#assistantPane").style.display !== "none";
  // 从文件/文章预览里跳走时，结束"仅此文件/本文"上下文，回到会话提问
  previewFile = null;
  if (typeof previewLink !== "undefined") previewLink = null;
  $(".composer #input").placeholder = "问 AI（Enter 发送，Shift+Enter 换行）";
  const data = await api(`/api/messages/context?id=${r.id}&around=15`);
  state.current = { username: data.chat, name: data.chat_name,
                    is_group: data.chat.includes("@chatroom") };
  // 左栏：回到会话列表并定位该会话（滚动到可见）
  await ensureSessions();
  showPane("#sessionList");
  renderSessions($("#searchInput").value);
  requestAnimationFrame(() => {
    const items = [...document.querySelectorAll("#sessionList .session-item")];
    const target = items.find(i => i.dataset.username === data.chat);
    if (target) {
      target.scrollIntoView({ block: "center" });
      target.classList.add("active");
    }
  });
  // 中间：显示原文上下文
  showChatPane();
  $("#assistantPane").style.display = "none";
  $("#aiSessionsPanel").style.display = "none";
  document.querySelector(".chat-column").style.display = "flex";
  $("#previewPane").style.display = "none";
  $("#msgList").style.display = "block";
  $("#chatTitle").textContent = data.chat_name;
  $("#chatMeta").textContent = state.current.is_group ? "群聊" : "私聊";
  const box = $("#msgs");
  box.innerHTML = "";
  const frag = document.createDocumentFragment();
  data.messages.forEach(m => frag.appendChild(renderMsg(m)));
  box.appendChild(frag);
  state.msgs[data.chat] = data.messages;
  state.hasMore[data.chat] = true;
  $("#loadMore").style.display = "block";
  scrollMsgsToBottom();
  startLiveStream(data.chat);         // 跳转后同样订阅实时消息
  setTimeout(() => {
    const target = box.querySelector(`[data-id="${r.id}"]`);
    if (target) {
      target.scrollIntoView({ block: "center", behavior: "smooth" });
      target.classList.add("focus");
      setTimeout(() => target.classList.remove("focus"), 2500);
    }
  }, 60);
  // 右侧：若来自全局助手，则保留 AI 对话，可继续追问
  if (wasAssistant) {
    $("#drawer").style.display = "flex";
    aiState.currentScope = aiState.currentScope || "assistant";
    $("#drawerScope").textContent = "范围：" +
      (aiState.currentScope === "assistant" ? "全局" : scopeNameOf(aiState.currentScope));
    renderQA();
  } else {
    $("#drawer").style.display = "flex";
    loadSessionTurns(aiState.currentSession);
  }
  // 跳转后提问范围默认聚焦该会话（可随时取消勾选改为全局）
  $("#scopeToggle").checked = true;
  updateScopeBar();
  updateDrawerScope();
}

/* ---------------- 助手会话 ---------------- */
async function openAssistant(sessionId) {
  state.current = { username: "assistant", name: "AI 助手", is_group: false };
  state.searchMode = false;
  showPane("#sessionList");
  renderSessions($("#searchInput").value);
  $("#chatEmpty").style.display = "none";
  $("#chatMain").style.display = "flex";
  // 三栏：中间对话 + 右侧会话列表
  document.querySelector(".chat-column").style.display = "none";
  $("#drawer").style.display = "none";
  $("#aiSessionsPanel").style.display = "flex";
  $("#assistantPane").style.display = "flex";
  updateScopeBar();
  aiState.currentScope = "assistant";
  await loadAiTree();
  const group = aiState.tree.find(g => g.scope === "assistant");
  const target = sessionId || aiState.currentSession ||
    (group && group.sessions[0] ? group.sessions[0].id : null);
  aiState.currentSession = target;
  let turns = [];
  if (target) {
    try { turns = (await api(`/api/ai/session/${target}`)).turns; } catch { turns = []; }
  }
  state.qaHistory = turns;
  renderQA();
  renderAiTree();
  $("#assistantTitle").textContent = "✦ 全局助手";
  $("#assistantMeta").textContent = `${turns.length} 轮 · 右侧可切换对话`;
  updateAssistantScopeUI();
  if (window.bindAssistantComposer) bindAssistantComposer();
  const inp = $("#assistantInput"); if (inp) inp.focus();
}

/* ---------------- 全局搜索 ---------------- */
async function globalSearch(q) {
  if (!q.trim()) { state.searchMode = false; showPane("#sessionList"); renderSessions(); return; }
  state.searchMode = true;
  const box = $("#resultList");
  box.innerHTML = `<div class="qa-empty">搜索中…</div>`;
  showPane("#resultList");
  const res = await api(`/api/search?q=${encodeURIComponent(q)}&n=40`);
  box.innerHTML = "";
  if (!res.length) { box.innerHTML = `<div class="qa-empty">无结果</div>`; return; }
  box.appendChild(Object.assign(document.createElement("div"), { className: "qa-empty", textContent: `命中 ${res.length} 条（回车切换回会话列表）` }));
  for (const r of res) {
    const d = document.createElement("div");
    d.className = "result-item";
    d.innerHTML = `<div class="meta">${esc(r.chat_name)} · ${fmtTime(r.ts)} · ${esc(r.sender_name)}</div>
      <div class="snippet">${esc(maskSecrets(r.snippet))}</div>`;
    d.onclick = () => jumpToRef(r);
    box.appendChild(d);
  }
}

/* ---------------- AI 会话视图 ---------------- */

async function openAssistantById(sessionId) {
  // 从右侧面板选历史对话：中间切换到该对话
  aiState.currentSession = sessionId;
  try {
    const d = await api(`/api/ai/session/${sessionId}`);
    state.qaHistory = d.turns;
  } catch { state.qaHistory = []; }
  renderQA();
  renderAiTree();
}

async function openAssistant(sessionId) {
  // AI 助手（聊天列表置顶项）：加载上一次全局对话
  if (typeof previewLink !== "undefined") previewLink = null;
  if (previewFile) exitPreview();
  state.current = { username: "assistant", name: "AI 助手", is_group: false };
  $("#chatEmpty").style.display = "none";
  $("#chatMain").style.display = "flex";
  document.querySelector(".chat-column").style.display = "none";
  $("#previewPane").style.display = "none";
  $("#drawer").style.display = "none";
  $("#aiSessionsPanel").style.display = "flex";
  $("#assistantPane").style.display = "flex";
  updateScopeBar();
  aiState.currentScope = "assistant";
  await loadAiTree();
  const group = aiState.tree.find(g => g.scope === "assistant");
  const target = sessionId || (group && group.sessions[0] ? group.sessions[0].id : null);
  aiState.currentSession = target;
  let turns = [];
  if (target) {
    try { turns = (await api(`/api/ai/session/${target}`)).turns; } catch { turns = []; }
  }
  state.qaHistory = turns;
  renderQA();
  renderAiTree();
  $("#assistantTitle").textContent = "✦ 全局助手";
  $("#assistantMeta").textContent = `${turns.length} 轮 · 右侧可切换对话`;
  if (window.bindAssistantComposer) bindAssistantComposer();
  const inp = $("#assistantInput"); if (inp) inp.focus();
}

/* ---------------- 通讯录 / 文件 / 设置 ---------------- *//* ---------------- 通讯录 / 文件 / 设置 ---------------- */
async function showContacts(q = "") {
  const box = $("#contactList");
  box.innerHTML = `<div class="qa-empty">加载中…</div>`;
  const list = await api(`/api/contacts?q=${encodeURIComponent(q)}`);
  box.innerHTML = "";
  let lastInitial = "";
  for (const c of list) {
    if (c.initial !== lastInitial) {
      lastInitial = c.initial;
      const h = document.createElement("div");
      h.className = "contact-group";
      h.textContent = c.initial;
      box.appendChild(h);
    }
    const d = document.createElement("div");
    d.className = "session-item";
    d.innerHTML = `${avatarImgHtml(c.username, c.name)}
      <div class="info"><div class="row1"><span class="name">${esc(c.name)}</span>
      ${c.is_group ? '<span class="tag">群</span>' : ""}</div>
      <div class="row2">${esc(c.username)}</div></div>`;
    d.onclick = () => {
      const s = state.sessions.find(x => x.username === c.username);
      if (s) openChat(s.username, s.name, s.is_group);
      else { toast("该联系人没有会话记录"); }
    };
    box.appendChild(d);
  }
  showPane("#contactList");
}
async function showFiles(q = "") {
  const box = $("#fileList");
  box.innerHTML = `<div class="qa-empty">加载中…</div>`;
  const list = await api(`/api/files?q=${encodeURIComponent(q)}&n=200`);
  box.innerHTML = "";
  if (!list.length) box.innerHTML = `<div class="qa-empty">没有找到文件消息</div>`;
  for (const f of list) {
    const d = document.createElement("div");
    d.className = "result-item";
    d.innerHTML = `<div class="meta">${esc(f.chat_name)} · ${fmtTime(f.ts)} · ${fmtSize(f.size)}</div>
      <div class="snippet">📄 ${esc(f.title)}</div>`;
    d.onclick = () => openFilePreview(f);
    box.appendChild(d);
  }
  showPane("#fileList");
}
async function showLinks(q = "") {
  const box = $("#linkList");
  box.innerHTML = `<div class="qa-empty">加载中…</div>`;
  let list = [];
  try { list = await api(`/api/links?n=200&q=${encodeURIComponent(q)}`); } catch { list = []; }
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = `<div class="qa-empty">还没有收到过链接。<br>
      聊天里出现的小红书/B站/公众号链接会自动解析并存下来。</div>`;
    showPane("#linkList");
    return;
  }
  const head = document.createElement("div");
  head.className = "qa-empty";
  const parsedN = list.filter(x => x.parsed).length;
  head.textContent = `${list.length} 条链接 · ${parsedN} 条已解析入库`;
  box.appendChild(head);
  for (const l of list) {
    const d = document.createElement("div");
    d.className = "result-item";
    const badge = l.parsed
      ? `<span class="link-badge ok">已解析</span>`
      : l.state === 2 ? `<span class="link-badge fail">暂不支持</span>`
      : `<span class="link-badge">解析中</span>`;
    d.innerHTML = `<div class="meta">${esc(l.chat_name)} · ${fmtTime(l.received)} ·
        <span class="link-plat">${esc(l.platform)}</span> ${badge}</div>
      <div class="snippet">🔗 ${esc(l.title)}${l.chars ? ` · ${l.chars} 字` : ""}</div>`;
    d.onclick = () => openLinkPreview(l.url, l.title);
    box.appendChild(d);
  }
  showPane("#linkList");
}

async function refreshJobs() {
  const box = $("#jobsBox");
  if (!box) return;
  let j;
  try { j = await api("/api/jobs"); } catch { return; }
  const row = (key, label, state, canStart) => `
    <div class="job-row">
      <span class="label">${label}</span>
      <span class="job-state ${state.running ? "running" : ""}">
        ${state.running ? "进行中 " + (state.count || "") : (state.done !== undefined ? state.done + (state.total ? "/" + state.total : "") : "")}
      </span>
      <span>
        <button class="mini" data-job="${key}" ${state.running || !canStart ? "disabled" : ""}>开始</button>
        <button class="mini ghost" data-stop="${key}" ${state.running ? "" : "disabled"}>停止</button>
      </span>
    </div>`;
  box.innerHTML = row("vectors", "向量索引", j.vector, j.vector.model)
    + row("ocr", "图片 OCR", j.ocr, true)
    + row("asr", "语音转写", j.asr, true)
    + row("links", "公众号正文", j.links, true);
  box.querySelectorAll("[data-job]").forEach(b => b.onclick = async () => {
    await api(`/api/jobs/${b.dataset.job}/start`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast("任务已启动"); setTimeout(refreshJobs, 1500);
  });
  box.querySelectorAll("[data-stop]").forEach(b => b.onclick = async () => {
    await api(`/api/jobs/${b.dataset.stop}/stop`, { method: "POST" });
    toast("已停止"); setTimeout(refreshJobs, 1000);
  });
  if ((j.vector.running || j.ocr.running || j.asr.running)) {
    clearTimeout(refreshJobs._t);
    refreshJobs._t = setTimeout(refreshJobs, 3000);
  }
}

const SETTING_SECTIONS = [
  { id: "model", label: "🤖 AI 模型" },
  { id: "data", label: "🗃 数据与同步" },
  { id: "index", label: "🧮 索引任务" },
  { id: "about", label: "ℹ️ 关于" },
];
let settingsSection = "model";

async function showSettings(section) {
  settingsSection = section || settingsSection || "model";
  // 设置占满中间+右侧；左边的图标栏就是入口
  showPane(null);
  $("#chatEmpty").style.display = "none";
  $("#chatMain").style.display = "flex";
  document.querySelector(".chat-column").style.display = "none";
  $("#drawer").style.display = "none";
  $("#assistantPane").style.display = "none";
  $("#aiSessionsPanel").style.display = "none";
  $("#previewPane").style.display = "none";
  document.querySelector("#settingsFull").style.display = "flex";
  const status = await api("/api/status").catch(() => null);
  const nav = document.querySelector("#sfNav");
  nav.innerHTML = SETTING_SECTIONS.map(x =>
    `<div class="sf-item ${x.id === settingsSection ? "active" : ""}" data-sec="${x.id}">${esc(x.label)}</div>`
  ).join("");
  nav.querySelectorAll(".sf-item").forEach(el => el.onclick = () => showSettings(el.dataset.sec));
  const body = document.querySelector("#sfBody");
  body.innerHTML = `<div class="qa-empty">加载中…</div>`;

  if (settingsSection === "model") await renderSettingsModel(body);
  if (settingsSection === "data") await renderSettingsData(body);
  if (settingsSection === "index") await renderSettingsIndex(body);
  if (settingsSection === "about") renderSettingsAbout(body, status);
}

async function renderSettingsModel(body) {
  const c = await api("/api/config");
  body.innerHTML = `
    <h3>🤖 AI 模型</h3>
    <div class="card">
      <div class="form-row"><label>接口地址</label>
        <input id="cfgBase" value="${esc(c.base_url)}"></div>
      <div class="form-row"><label>模型名</label>
        <input id="cfgModel" value="${esc(c.model)}"></div>
      <div class="form-row"><label>API Key</label>
        <input id="cfgKey" placeholder="${c.has_key ? "已配置 " + esc(c.key_masked) + "（留空则不修改）" : "sk-..."}"></div>
      <div class="form-row"><label>深度思考</label>
        <label class="chk"><input type="checkbox" id="cfgThinking" ${c.thinking ? "checked" : ""} style="width:auto">
          启用推理模型</label>
        <input id="cfgThinkingModel" value="${esc(c.thinking_model || "")}" placeholder="deepseek-reasoner" style="flex:1"></div>
      <div class="btn-row">
        <button id="saveCfg">保存</button>
        <button id="testCfg" class="ghost">测试连接</button>
        <span id="cfgMsg" class="msg"></span></div>
      <div class="hint-sm">支持任意 OpenAI 兼容接口（DeepSeek / GLM / Kimi / 硅基流动）。Key 只存本机。</div>
    </div>`;
  $("#saveCfg").onclick = async () => {
    const msg = $("#cfgMsg");
    try {
      await api("/api/config", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: $("#cfgBase").value, model: $("#cfgModel").value,
                               api_key: $("#cfgKey").value,
                               thinking: $("#cfgThinking").checked,
                               thinking_model: $("#cfgThinkingModel").value }) });
      msg.textContent = "已保存"; msg.className = "msg ok";
      setTimeout(() => showSettings("model"), 600);
    } catch (e) { msg.textContent = "保存失败: " + e.message; msg.className = "msg err"; }
  };
  $("#testCfg").onclick = async () => {
    const msg = $("#cfgMsg");
    msg.textContent = "测试中…"; msg.className = "msg";
    try {
      const r = await api("/api/config/test", { method: "POST" });
      msg.textContent = `连接成功（${r.model}）：${r.reply}`; msg.className = "msg ok";
    } catch (e) { msg.textContent = e.message; msg.className = "msg err"; }
  };
}

async function renderSettingsData(body) {
  let s, a, rt;
  try {
    [s, a, rt] = await Promise.all([
      api("/api/status"), api("/api/sync/auto"), api("/api/sync/realtime")]);
  } catch (e) { body.innerHTML = `<div class="qa-empty">加载失败: ${esc(e.message)}</div>`; return; }
  body.innerHTML = `
    <h3>🗃 数据与同步</h3>
    <div class="card">
      <b>实时同步</b>
      <div class="form-row"><label>开关</label>
        <label class="chk"><input type="checkbox" id="rtSync" ${rt.realtime ? "checked" : ""} style="width:auto">
          启用（微信收到新消息即自动入库，撤回前即抓取）</label></div>
      <div class="form-row"><label>兜底轮询</label>
        <label class="chk" style="font-size:12px;color:#6b6b70">每
          <input id="rtInterval" type="number" min="15" value="${rt.realtime_sec}" style="width:64px"> 秒</label></div>
      <div class="job-state" id="rtState"></div>
      <div class="hint-sm">触发方式：微信数据库文件一有变化立即同步（毫秒级感知），
        长时间无变化时按间隔兜底。需要微信已登录且密钥有效。</div>
    </div>
    <div class="card">
      <b>定时同步（兜底）</b>
      <div class="form-row"><label>开关</label>
        <label class="chk"><input type="checkbox" id="autoSync" ${a.auto ? "checked" : ""} style="width:auto">
          每 <input id="autoInterval" type="number" min="5" value="${a.interval_min}" style="width:64px"> 分钟</label>
        <span class="job-state">${a.last ? "上次 " + a.last : "尚未运行"}</span></div>
    </div>
    <div class="card">
      <button id="syncBtn" ${s.sync.running ? "disabled" : ""}>${s.sync.running ? "同步中…" : "立即同步一次"}</button>
      <pre>${esc(s.sync.log || "")}</pre>
    </div>`;
  const saveSync = async () => {
    await api("/api/sync/realtime", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ realtime: $("#rtSync").checked,
                             realtime_sec: +$("#rtInterval").value || 60 }) });
    await api("/api/sync/auto", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto: $("#autoSync").checked,
                             interval_min: +$("#autoInterval").value || 30 }) });
    toast("同步设置已保存");
    setTimeout(() => showSettings("data"), 600);
  };
  $("#rtSync").onchange = saveSync;
  $("#rtInterval").onchange = saveSync;
  $("#autoSync").onchange = saveSync;
  $("#autoInterval").onchange = saveSync;
  $("#syncBtn").onclick = async () => {
    await api("/api/sync", { method: "POST" });
    toast("同步已启动"); setTimeout(() => showSettings("data"), 1500);
  };
  const rtState = $("#rtState");
  rtState.textContent = rt.last ? `最近：${rt.last} · 新增 ${rt.added} 条` +
    (rt.media_ocr || rt.media_asr ? ` · OCR ${rt.media_ocr} / 转写 ${rt.media_asr}` : "") +
    (rt.error ? " · " + rt.error : "") : "尚未运行";
  rtState.className = "job-state" + (rt.error ? " err" : "");
}

async function renderSettingsIndex(body) {
  const j = await api("/api/jobs");
  const row = (key, label, state, canStart) => `
    <div class="job-row">
      <span class="label">${label}</span>
      <span class="job-state ${state.running ? "running" : ""}">
        ${state.running ? "进行中 " + (state.count || "") : (state.done !== undefined ? state.done + (state.total ? "/" + state.total : "") : "")}</span>
      <span><button class="mini" data-job="${key}" ${state.running || !canStart ? "disabled" : ""}>开始</button>
      <button class="mini ghost" data-stop="${key}" ${state.running ? "" : "disabled"}>停止</button></span>
    </div>`;
  body.innerHTML = `
    <h3>🧮 索引任务</h3>
    <div class="card"><div class="jobs" id="jobsBox">
      ${row("vectors", "向量索引（语义检索）", j.vector, j.vector.model)}
      ${row("ocr", "图片 OCR（历史图片）", j.ocr, true)}
      ${row("asr", "语音转写（历史语音）", j.asr, true)}
      ${row("links", "公众号正文（历史链接）", j.links, true)}
    </div></div>
    <div class="card">
      <div class="hint-sm">新消息的图片/语音会在实时同步时自动处理，无需手动；
        这里的任务用于补跑历史存量，均可断点续跑。建议一次只跑一个。</div>
    </div>`;
  body.querySelectorAll("[data-job]").forEach(b => b.onclick = async () => {
    await api(`/api/jobs/${b.dataset.job}/start`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    toast("任务已启动"); setTimeout(() => showSettings("index"), 1500);
  });
  body.querySelectorAll("[data-stop]").forEach(b => b.onclick = async () => {
    await api(`/api/jobs/${b.dataset.job}/stop`, { method: "POST" });
    setTimeout(() => showSettings("index"), 800);
  });
}

function renderSettingsAbout(body, status) {
  body.innerHTML = `
    <h3>ℹ️ 关于</h3>
    <div class="card">
      <div><span class="k">知识库</span>${status.messages.toLocaleString()} 条消息 ·
        ${status.chats} 个会话 · ${status.contacts} 个联系人</div>
      <div style="margin-top:6px"><span class="k">问答模式</span>${status.llm === "configured"
        ? "AI 生成（" + esc(status.llm_model) + "）" : "检索模式（未配置 Key）"}</div>
      <div style="margin-top:6px"><span class="k">数据库密钥</span>fp=${status.key_fp}
        （24h 有效，微信重启后需重新捕获）</div>
      <div style="margin-top:6px"><span class="k">数据位置</span>data/knowledge.db 与
        data/decrypted（全部在本机）</div>
    </div>`;
}


function openImageViewer(m, src) {
  showChatPane();
  $("#msgList").style.display = "none";
  $("#previewPane").style.display = "flex";
  $("#previewPane").style.flexDirection = "column";
  $("#backToChat").style.display = "inline";
  $("#chatTitle").textContent = "图片查看";
  $("#chatMeta").textContent = m.chat_name + " · " + fmtTime(m.ts);
  updateScopeBar();
  $("#pvBody").innerHTML =
    '<div class="pv-head"><span class="pv-name">点击空白处或 ← 返回聊天</span></div>' +
    '<img src="' + src + '" class="pv-img" style="max-height:70vh;cursor:zoom-out" ' +
    'onclick="document.querySelector(&#39;#backToChat&#39;).click()">';
}

/* ---------------- 网页/公众号文章预览 ---------------- */
let previewLink = null;

function isPlatformLink(u) {
  const s = (u || "").toLowerCase();
  return s.includes("xiaohongshu.com") || s.includes("xhslink.com")
      || s.includes("bilibili.com") || s.includes("b23.tv");
}

function renderPlatformBody(d, url) {
  const parts = d.parts || {};
  const imgs = d.images || [];
  const badge = d.platform ? `<span class="pv-badge">${esc(d.platform)}</span>` : "";
  const owner = d.owner ? `<span class="pv-owner">${esc(d.owner)}</span>` : "";
  const wall = imgs.length
    ? `<div class="pv-gallery">${imgs.map((u, i) =>
        `<img class="pv-thumb" data-i="${i}" src="/api/link/img?u=${encodeURIComponent(u)}"
              loading="lazy" onclick="window.__pvImg(${i})">`).join("")}</div>`
    : "";
  const segs = [];
  const put = (label, txt, cls) => {
    if (!txt || !txt.trim()) return;
    segs.push(`<div class="pv-seg ${cls}"><div class="pv-seg-title">${label}</div>
      <pre class="pv-text">${esc(txt)}</pre></div>`);
  };
  put("正文", parts.desc, "desc");
  put("图片文字（OCR）", parts.ocr, "ocr");
  put("视频字幕", parts.subtitle, "sub");
  put("视频语音转写" + (d.truncated ? "（仅前 15 分钟）" : ""), parts.asr, "asr");
  return `<div class="pv-head">
      <span class="pv-name">${esc(d.title || "链接内容")}</span>
      ${badge}${owner}
      <span class="pv-actions"><a href="${esc(url)}" target="_blank" rel="noopener">在浏览器打开 ↗</a></span>
    </div>${wall}${segs.join("")}`;
}

function normalizeUrl(u) {
  return String(u || "").replace(/&amp;/g, "&").replace(/[​﻿]/g, "").trim();
}

async function openLinkPreview(url, title) {
  url = normalizeUrl(url);      // 消息里的链接带 &amp;，与缓存键必须一致
  showChatPane();
  $("#assistantPane").style.display = "none";
  $("#aiSessionsPanel").style.display = "none";
  document.querySelector(".chat-column").style.display = "flex";
  $("#msgList").style.display = "none";
  $("#previewPane").style.display = "flex";
  $("#previewPane").style.flexDirection = "column";
  $("#backToChat").style.display = "inline";
  $("#chatTitle").textContent = title;
  const platform = isPlatformLink(url);
  $("#chatMeta").textContent = platform ? "解析中（图文 OCR / 视频转写）…" : "网页正文（已自动解析）";
  $("#pvBody").innerHTML = `<div class="qa-empty">${platform ? "正在解析，请稍候…" : "正在抓取正文…"}</div>`;
  previewLink = { url, title };
  // 每个链接有自己的 AI 会话
  await enterAiScope(`link:${hashStr(url)}`, title);
  $("#scopeToggle").checked = true;
  updateScopeBar();
  updateDrawerScope();
  $("#drawer").style.display = "flex";

  // 已解析过的链接：读缓存直接渲染（和 PDF 一样秒开）；未解析的显示进度并轮询
  let d = null;
  try {
    d = await api(`/api/link/text?url=${encodeURIComponent(url)}`);
    if (d && d.pending) {
      while (d && d.pending && previewLink && previewLink.url === url) {
        const pct = Math.max(5, Math.min(95, d.progress || 5));
        $("#chatMeta").textContent = d.step || "解析中…";
        $("#pvBody").innerHTML = `<div class="qa-empty">
          <div>${esc(d.step || "正在解析…")}</div>
          <div class="pv-progress"><i style="width:${pct}%"></i></div>
          <div style="font-size:11px;color:#9a9aa0;margin-top:6px">
            图文会识别图片文字，视频会转写语音，内容较长时需要一会儿</div></div>`;
        await new Promise(r => setTimeout(r, 1200));
        const j = await api(`/api/link/job?url=${encodeURIComponent(url)}`);
        if (j.state === "running") { d = { pending: true, step: j.step, progress: j.progress }; continue; }
        if (j.state === "error") throw new Error(j.error || "解析失败");
        d = j;
      }
    }
  } catch (e) {
    $("#pvBody").innerHTML = `<div class="qa-empty">抓取失败：${esc(e.message)}<br>
      公众号文章可能有防盗链，可在浏览器中打开原文。</div>`;
    return;
  }
  if (previewLink && previewLink.url !== url) return;   // 用户已切换到别的预览
  if (d && d.pending) {
    $("#pvBody").innerHTML = `<div class="qa-empty">该链接仍在解析，稍后重新打开即可看到内容。</div>`;
    return;
  }
  if (d && d.kind && d.kind !== "article") {
    $("#chatTitle").textContent = d.title || title;
    $("#chatMeta").textContent = `${d.platform || ""} · ${d.chars} 字${
      (d.images || []).length ? " · " + (d.images || []).length + " 张图" : ""}`;
    $("#pvBody").innerHTML = renderPlatformBody(d, url);
    window.__pvImg = (i) => {
      const imgs = d.images || [];
      if (imgs[i]) $("#pvBody").querySelectorAll(".pv-thumb").forEach(t =>
        t.classList.toggle("big", +t.dataset.i === i));
    };
  } else {
    $("#chatTitle").textContent = (d && d.title) || title;
    $("#chatMeta").textContent = `${d ? d.chars : 0} 字 · ${url.slice(0, 60)}`;
    $("#pvBody").innerHTML =
      `<div class="pv-head"><span class="pv-name">${esc((d && d.title) || title)}</span>
        <span class="pv-actions"><a href="${esc(url)}" target="_blank" rel="noopener">在浏览器打开 ↗</a></span>
       </div><pre class="pv-text">${esc((d && d.text) || "")}</pre>`;
  }
  updateScopeBar();
}

/* ---------------- 文件预览（内嵌在中间显示区） ---------------- */
let previewFile = null;

function exitPreview() {
  $("#previewPane").style.display = "none";
  $("#msgList").style.display = "block";
  $("#backToChat").style.display = "none";
  $("#drawerFab").style.display = "none";
  $("#drawer").style.display = "flex";
  previewFile = null;
  if (typeof previewLink !== "undefined") previewLink = null;
  if (state.current) $("#chatTitle").textContent = state.current.name;
  // 回到会话上下文：右侧切回该会话自己的 AI 对话
  if (state.current && state.current.username !== "assistant") {
    enterAiScope(state.current.username, state.current.name);
  }
  // 回到会话上下文：提问范围恢复为该会话（默认勾选）
  $("#scopeToggle").checked = true;
  updateScopeBar();
  updateDrawerScope();
}

async function openFilePreview(f) {
  // f: {chat, id(local_id), ts, title, chat_name, size}
  const name = f.title, chat = f.chat, lid = f.id, ts = f.ts;
  const qs = `chat=${encodeURIComponent(chat)}&local_id=${lid}&ts=${ts}&name=${encodeURIComponent(name)}`;
  previewFile = f;
  if (!state.current || state.current.username !== chat) {
    state.current = { username: chat, name: f.chat_name, is_group: chat.includes("@chatroom") };
  }
  // 统一切到"聊天视图"：从 AI 助手/会话面板进入时，先把多余面板收起来（否则会出现 4 个区域）
  showChatPane();
  $("#assistantPane").style.display = "none";
  $("#aiSessionsPanel").style.display = "none";
  $("#settingsFull").style.display = "none";
  document.querySelector(".chat-column").style.display = "flex";
  $("#msgList").style.display = "none";
  $("#previewPane").style.display = "flex";
  $("#previewPane").style.flexDirection = "column";
  $("#backToChat").style.display = "inline";
  $("#chatTitle").textContent = name;
  $("#chatMeta").textContent = `${f.chat_name} · ${fmtTime(ts)} · ${fmtSize(f.size)}`;
  // 提问范围默认聚焦此文件，可随时取消勾选改为全局
  $("#scopeToggle").checked = true;
  updateScopeBar();
  updateDrawerScope();
  // 预览时保留右侧问答栏（可边看边问）
  $("#drawer").style.display = "flex";
  $("#drawerFab").style.display = "none";
  $("#pvBody").innerHTML = `<div class="qa-empty">加载中…</div>`;

  let meta = { found: false };
  try { meta = await api("/api/file/meta?" + qs); } catch {}
  const ext = (meta.ext || (name.match(/\.[^.]+$/) || [""])[0]).toLowerCase();
  const head = `<div class="pv-head"><span class="pv-name">${esc(name)}</span>
      <span class="pv-actions"><a href="/api/file/raw?${qs}" target="_blank">用系统程序打开 ↗</a></span></div>`;

  if (!meta.found) {
    $("#pvBody").innerHTML = head +
      `<div class="qa-empty">文件不在本地磁盘（可能已被微信清理或未下载）。<br>
       ${meta.msg_id ? `<a href="javascript:void(0)" id="jumpToFileMsg">跳转到这条文件消息 ↗</a>（查看聊天上下文）<br>` : ""}
       仍可在右侧提问，AI 会结合聊天记录中的描述回答。</div>`;
    if (meta.msg_id) {
      $("#jumpToFileMsg").onclick = () => jumpToRef({ id: meta.msg_id });
    }
  } else if (ext === ".pdf") {
    $("#pvBody").innerHTML = head +
      `<iframe src="/api/file/raw?${qs}#navpanes=0&view=FitH&toolbar=1" class="pv-pdf"></iframe>`;
  } else if ([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"].includes(ext)) {
    $("#pvBody").innerHTML = head + `<img src="/api/file/raw?${qs}" class="pv-img">`;
  } else if (ext === ".mp4" || ext === ".mov" || ext === ".avi") {
    $("#pvBody").innerHTML = head +
      `<video src="/api/file/raw?${qs}" controls style="max-width:100%;display:block;margin:16px auto"></video>`;
  } else if ([".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".amr", ".silk"].includes(ext)) {
    $("#pvBody").innerHTML = head +
      `<audio src="/api/file/raw?${qs}" controls style="display:block;margin:24px auto"></audio>`;
  } else {
    // 文本 / Office / 代码：抽取正文显示
    try {
      const t = await api("/api/file/text?" + qs + "&limit=40000");
      $("#pvBody").innerHTML = head + (t.text
        ? `<pre class="pv-text">${esc(t.text)}</pre>`
        : `<div class="qa-empty">该格式暂不支持内容预览，仍可在右侧提问（AI 会读取文件正文）。</div>`);
    } catch (e) {
      $("#pvBody").innerHTML = head +
        `<div class="qa-empty">预览失败：${esc(e.message)}</div>`;
    }
  }
  // 文件有自己的 AI 会话：切到该文件作用域，右侧只显示这个文件的问答
  await enterAiScope(`file:${chat}:${lid}`, name);
  // 右侧自动聚焦到该文件问答
  updateDrawerScope();
  $("#input").placeholder = `针对「${name}」提问…`;
  $("#input").focus();
}

/* ---------------- 视图切换与事件 ---------------- */
function switchView(v) {
  state.view = v;
  if (typeof previewLink !== "undefined" && v !== "links") previewLink = null;
  document.querySelectorAll(".rail-btn").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  if (v === "chats") {
    $("#assistantPane").style.display = "none";
    $("#aiSessionsPanel").style.display = "none";
    $("#settingsFull").style.display = "none";
    document.querySelector(".chat-column").style.display = "flex";
    $("#drawer").style.display = "flex";
    showPane(state.searchMode && $("#resultList").children.length ? "#resultList" : "#sessionList");
  }
  if (v === "contacts") showContacts();
  if (v === "files") showFiles();
  if (v === "links") {
    $("#searchInput").placeholder = "搜索链接标题 / 来源群";
    showLinks();
    return;
  }
  $("#searchInput").placeholder = "搜索会话 / 全局搜消息（回车）";
  if (v === "settings") showSettings();
}
$("#searchInput").addEventListener("input", (e) => {
  if (state.view === "links") { showLinks(e.target.value); return; }
  if (state.view !== "chats") return;
  if (state.searchMode) return;
  renderSessions(e.target.value);
});
$("#searchInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && state.view === "chats") globalSearch(e.target.value);
  if (e.key === "Escape") { e.target.value = ""; globalSearch(""); }
});
document.querySelectorAll(".rail-btn").forEach(b => b.onclick = () => switchView(b.dataset.view));
$("#backToChat").onclick = () => exitPreview();
$("#drawerFab").onclick = () => {
  const d = $("#drawer");
  const hidden = d.style.display === "none";
  d.style.display = hidden ? "flex" : "none";
};
$("#scopeToggle").onchange = updateDrawerScope;
$("#sendBtn").onclick = () => { const v = $("#input").value; $("#input").value = ""; ask(v); };
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); const v = $("#input").value; $("#input").value = ""; ask(v); }
});
$("#loadMore").onclick = () => {
  if (state.current && state.msgs[state.current.username]?.length)
    loadMessages(state.current.username, { before: state.msgs[state.current.username][0].id });
};

/* ---------------- 自动刷新（实时同步推送新消息后自动更新列表和聊天窗） ---------------- */
let _autoRefreshTimer = null;
function startAutoRefresh() {
  if (_autoRefreshTimer) return;
  _autoRefreshTimer = setInterval(async () => {
    // 刷新会话列表（不打断当前聊天）
    if (state.view === "chats" && !state.searchMode) {
      const old = state.sessions;
      await loadSessions();
      // 如果正在看某个聊天，静默加载新消息
      if (state.current && state.current.username) {
        await loadMessages(state.current.username, { initial: false });
      }
    }
  }, 30000);
}

/* ---------------- 启动 ---------------- */
(async function init() {
  try {
    const s = await api("/api/status");
    $("#statusHint").textContent =
      `知识库就绪：${s.messages.toLocaleString()} 条消息 · ` +
      (s.llm === "configured" ? `模型 ${s.llm_model} 已就绪` : "未配置 LLM Key（问答将展示检索原文，填入 backend/config.json 后启用 AI 回答）");
    try { const me = await api("/api/me"); state.selfUsername = me.username; } catch {}
    await loadSessions();
    startAutoRefresh();
  } catch (e) {
    $("#statusHint").textContent = "后端连接失败: " + e.message;
  }
})();


/* 助手页独立输入框 */
function bindAssistantComposer() {
  const go = () => {
    const el = document.querySelector("#assistantInput");
    const v = el.value;
    if (!v.trim()) return;
    el.value = "";
    $("#thinkToggle") && ($("#thinkToggle").checked = document.querySelector("#assistantThink").checked);
    ask(v);
  };
  document.querySelector("#assistantSend").onclick = go;
  document.querySelector("#assistantInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); }
  });
  document.querySelector("#assistantNew").onclick = () => newAiSession();
  document.querySelector("#assistantInput").focus();
}
