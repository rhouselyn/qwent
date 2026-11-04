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
  scopeContexts: {},       // scope -> {chat|file|link}，选中文件/链接作用域时按需解析
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
        selectAssistantScope(g.scope, s.id);
      };
      d.querySelector(".n").onclick = () => d.querySelector(".t").click();
      d.querySelector(".del").onclick = async (e) => {
        e.stopPropagation();
        await api(`/api/ai/session/${s.id}/delete`, { method: "POST" });
        if (aiState.currentSession === s.id) { aiState.currentSession = null; state.qaHistory = []; renderQA(); }
        await loadAiTree(); renderAiTree(); syncAssistantScopeSelect();
        toast("已删除对话");
      };
      list.appendChild(d);
    }
    // 点分组头：展开 / 收起；点组内对话才是切换提问范围
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
function initial(name) {
  // 用 Array.from 取完整字符：charAt(0) 会把 emoji 截成半个代理字符，头像显示成“�”
  const s = String(name || "?").trim();
  const ch = s ? Array.from(s)[0] : "?";
  return ch.toUpperCase();
}
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
    .replace(/\bsk-[A-Za-z0-9_-]{8,}/g, "sk-****")
    .replace(/\b(ghp|gho|github_pat)_[A-Za-z0-9_]{10,}/g, "$1_****")
    .replace(/\b(AKIA|ASIA)[A-Z0-9]{12,}/g, "$1****")
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
               (_, txt, url) => hold('<a href="' + safeHttpUrl(url) + '" target="_blank" rel="noopener noreferrer">' + txt + "</a>"))
      .replace(/\[(\d{1,3})\]/g, '<span class="md-ref" data-ref="$1">[$1]</span>')
      .replace(/(https?:\/\/[^\s<]+)/g, (_, url) => '<a href="' + safeHttpUrl(url) + '" target="_blank" rel="noopener noreferrer">' + url + '</a>');
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
function attrEsc(s) { return String(s || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function safeHttpUrl(url) {
  try {
    const u = new URL(String(url || "").replace(/&amp;/g, "&"));
    return (u.protocol === "http:" || u.protocol === "https:") ? attrEsc(u.href) : "#";
  } catch { return "#"; }
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 2500); }
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(e.detail || e.message || `${r.status} ${r.statusText}`);
  }
  return r.json();
}

const PANES = ["#sessionList", "#resultList", "#contactList", "#fileList",
               "#linkList", "#settingsPane", "#aiSessionList", "#snsList",
               "#settingsNav"];
function showPane(id) {
  PANES.forEach(p => { const el = $(p); if (el) el.style.display = "none"; });
  if (id) { const el = $(id); if (el) el.style.display = "block"; }
}

/* ---------------- 会话列表 ---------------- */
async function loadSessions() {
  const box = $("#sessionList");
  const scrollTop = box ? box.scrollTop : 0;
  state.sessions = await api("/api/sessions");
  renderSessions();
  if (box) box.scrollTop = scrollTop;
}
function renderSessions(filter = "") {
  const box = $("#sessionList");
  box.innerHTML = "";
  const mkItem = (html, cls, onclick) => {
    const d = document.createElement("div");
    d.className = "session-item " + cls; d.innerHTML = html; d.onclick = onclick;
    d.setAttribute("role", "button"); d.tabIndex = 0;
    d.addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && e.target === d) {
        e.preventDefault(); onclick();
      }
    });
    return d;
  };
  // 置顶 AI 助手
  box.appendChild(mkItem(
    `<div class="ava">✦</div>
     <div class="info"><div class="row1"><span class="name">AI 助手<b class="badge">置顶</b></span></div>
     <div class="row2">全局问答：所有聊天记录 + 随时追问</div></div>`,
    "ai", () => openAssistant()));
  const needle = String(filter || "").trim().toLowerCase();
  const list = state.sessions.filter(s => {
    const name = String(s.name || "");
    const username = String(s.username || "");
    return !needle || name.toLowerCase().includes(needle) || username.toLowerCase().includes(needle);
  });
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
    { label: "只看该会话的文件", fn: () => { showFiles({ chat: s.username, q: "" }); } },
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
async function openChat(username, name, isGroup, sessionId, options = {}) {
  state.current = { username, name, is_group: isGroup };
  state.searchMode = false;
  if (previewFile) exitPreview();
  // 离开链接预览时清掉上下文，避免后续提问被当成"针对本文"
  if (typeof previewLink !== "undefined") previewLink = null;
  $("#previewPane").style.display = "none";
  $("#msgList").style.display = "block";
  $(".composer #input").placeholder = "问 AI（Enter 发送，Shift+Enter 换行）";
  if (!options.keepListPane) {
    showPane("#sessionList");
    renderSessions($("#searchInput").value);
  }
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
  // 从通讯录点进来还没有聊天记录：显示空会话提示，提问范围默认全局
  if (!(state.msgs[username] || []).length) {
    const hint = document.createElement("div");
    hint.className = "sys empty-hint";
    hint.textContent = `还没有和「${name}」的聊天记录 · 可在右侧向 AI 提问`;
    $("#msgs").appendChild(hint);
    $("#scopeToggle").checked = false;
    updateDrawerScope();
  }
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

function msgOrder(a, b) {
  return ((a.ts || 0) - (b.ts || 0)) || ((a.id || 0) - (b.id || 0));
}

/* 合并新消息并保持时间顺序：补漏重扫会把旧消息以新 id 入库，
   只按 id 追加会把它们排到列表末尾（刷新后才恢复）。 */
function mergeMessages(chat, incoming) {
  const prev = state.msgs[chat] || [];
  const known = new Set(prev.map(m => m.id));
  const add = (incoming || []).filter(m => !known.has(m.id));
  if (!add.length) return [];
  state.msgs[chat] = prev.concat(add).sort(msgOrder);
  return add;
}

/* 把新消息按时间位置插入 DOM（只在正看这个会话时动 DOM）。 */
function insertMessages(chat, add) {
  if (!add.length) return;
  if (!state.current || state.current.username !== chat) return;
  const viewingChat = $("#msgList").style.display !== "none"
    && $("#previewPane").style.display === "none" && !isAssistant();
  if (!viewingChat) return;
  const box = $("#msgs");
  const eh = box.querySelector(".empty-hint");
  if (eh) eh.remove();              // 空会话提示有了真消息就撤掉
  const sc = msgScroller();
  // 没贴在底部时，记录"距底部距离"：往上方插入旧消息后视口不跳
  const keepBottom = (!state.pinBottom && sc) ? sc.scrollHeight - sc.scrollTop : null;
  const ordered = state.msgs[chat];
  for (const m of add.slice().sort(msgOrder)) {
    if (box.querySelector(`[data-id="${m.id}"]`)) continue;
    // 找排序中位于它后面、且已在 DOM 里的那条消息作为插入点
    let ref = null;
    for (let i = ordered.findIndex(x => x.id === m.id) + 1; i < ordered.length; i++) {
      ref = box.querySelector(`[data-id="${ordered[i].id}"]`);
      if (ref) break;
    }
    box.insertBefore(renderMsg(m), ref);
  }
  if (keepBottom !== null) sc.scrollTop = sc.scrollHeight - keepBottom;
}

function startLiveStream(chat) {
  if (liveState.chat === chat && liveState.es) return;   // 已在同一个会话订阅
  stopLiveStream();
  liveState.chat = chat;
  // 水位取最大 id，不能取数组最后一条：补漏重扫后数组按时间排，
  // 最后一条不一定是最新 id。
  const afterId = (state.msgs[chat] || [])
    .reduce((mx, m) => Math.max(mx, m.id || 0), 0);
  const es = new EventSource(`/api/messages/stream?chat=${encodeURIComponent(chat)}&after_id=${afterId}`);
  liveState.es = es;
  es.onmessage = (ev) => {
    let d = null;
    try { d = JSON.parse(ev.data); } catch { return; }
    if (!d || !d.messages) return;
    if (!state.current || state.current.username !== chat) return;
    const prevLast = (state.msgs[chat] || []).slice(-1)[0];
    const add = mergeMessages(chat, d.messages);
    if (!add.length) return;
    insertMessages(chat, add);
    const newest = add.reduce((a, b) => (msgOrder(b, a) > 0 ? b : a));
    if (state.pinBottom && (!prevLast || msgOrder(newest, prevLast) > 0)) {
      scrollMsgsToBottom();
    }
    // 会话列表里的最后一条预览 + 排序实时更新（用时间最新的一条，
    // 补漏重扫补进来的旧消息不能把会话顶下去）
    const s = (state.sessions || []).find(x => x.username === chat);
    if (s && (newest.ts || 0) >= (s.last_ts || 0)) {
      s.last_ts = newest.ts;
      s.summary = newest.parsed && newest.parsed.text ? newest.parsed.text.slice(0, 30) : s.summary;
      state.sessions.sort((a, b) => (b.pinned - a.pinned) ||
        ((b.last_ts || 0) - (a.last_ts || 0)));
      if (!state.searchMode) renderSessions($("#searchInput").value);
    }
    if (typeof onNewMessages === "function") onNewMessages(chat, add);
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
  const oldHeight = sc ? sc.scrollHeight : 0;
  if (before) {
    const frag = document.createDocumentFragment();
    data.messages.forEach(m => frag.appendChild(renderMsg(m)));
    box.prepend(frag);
    state.msgs[username] = [...data.messages, ...state.msgs[username]];
    // 加载更早消息时保持当前视口位置
    if (sc) sc.scrollTop = sc.scrollHeight - oldHeight;
  }
  else {
    if (initial) {
      const frag = document.createDocumentFragment();
      data.messages.forEach(m => frag.appendChild(renderMsg(m)));
      box.appendChild(frag);
      state.msgs[username] = data.messages;
      scrollMsgsToBottom();              // 进入会话默认停在最新日期
      [80, 300, 800, 1500].forEach(ms => setTimeout(() => {
        if (state.pinBottom) scrollMsgsToBottom();
      }, ms));
    } else {
      // 兜底刷新同样按时间合入：补漏重扫补进来的旧消息不能追加到末尾
      insertMessages(username, mergeMessages(username, data.messages));
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
  $("#settingsFull").style.display = "none";   // 从设置页切走时清残留
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
  // 右键消息 = 加入底部引用栏（替代旧的悬浮"引用"按钮，减少遮挡）
  wrap.title = "右键加入引用栏";
  wrap.oncontextmenu = (e) => {
    e.preventDefault();
    addQuote(m);
    toast("已加入引用栏（点 ✕ 可移除）");
  };
  const col = document.createElement("div"); col.className = "col";
  if (state.current && state.current.is_group && !m.is_sender) {
    const who = document.createElement("div"); who.className = "who"; who.textContent = m.sender_name;
    col.appendChild(who);
  }
  if (m.recalled) {
    const tag = document.createElement("div"); tag.className = "recall-tag";
    tag.textContent = "已撤回";
    col.appendChild(tag);
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
      if (p.ocr_text) {
        const t = document.createElement("div");
        t.className = "voice-text";
        t.textContent = p.ocr_text;
        el.appendChild(t);
      }
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
    case "video": {
      const wrap = document.createElement("div");
      wrap.className = "video-wrap";
      const box = document.createElement("div");
      box.className = "video-box";
      const img = document.createElement("img");
      img.className = "video-thumb";
      img.loading = "lazy";
      img.alt = "[视频]";
      img.src = `/api/media/video/thumb?chat=${encodeURIComponent(m.chat)}&id=${m.id}`;
      img.onerror = () => { box.className = "video-box video-miss"; box.textContent = "🎬 [视频]（未下载或已被清理）"; };
      const play = document.createElement("span");
      play.className = "video-play";
      play.textContent = "▶";
      box.appendChild(img);
      box.appendChild(play);
      if (p.duration_s) {
        const dur = document.createElement("span");
        dur.className = "video-dur";
        dur.textContent = `${p.duration_s}″`;
        box.appendChild(dur);
      }
      box.onclick = () => openVideoViewer(m, p);
      wrap.appendChild(box);
      if (p.asr_text) {
        const t = document.createElement("div");
        t.className = "voice-text";
        t.textContent = p.asr_text;
        wrap.appendChild(t);
      }
      el.appendChild(wrap);
      return el;
    }
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
      el.innerHTML = `📄 <span class="t">${esc(p.title || "[文件]")}</span>` +
        `<div class="d">${p.size ? fmtSize(p.size) : ""} · 点击预览</div>`;
      el.style.cursor = "pointer";
      el.onclick = () => openFilePreview({
        chat: m.chat, id: m.src_local_id || m.id, ts: m.ts,
        title: p.title || "[文件]", chat_name: m.chat_name, size: p.size || 0,
      });
      return el;
    }
    case "record": {
      const rec = p.record || { items: [], count: 0 };
      el.className = "card record-card";
      const head = document.createElement("div");
      head.className = "rec-head";
      head.innerHTML = `📋 <span class="t">${esc(p.title || "聊天记录")}</span>` +
        `<span class="rec-count">${rec.count || rec.items.length} 条</span>` +
        `<span class="rec-toggle">展开 ▸</span>`;
      const body = document.createElement("div");
      body.className = "rec-body";
      body.style.display = "none";
      (rec.items || []).forEach((it) => {
        const row = document.createElement("div");
        row.className = "rec-item" + (it.url ? " clickable" : "");
        const who = it.name ? `<span class="rec-who">${esc(it.name)}</span>` : "";
        const time = it.time ? `<span class="rec-time">${esc(it.time)}</span>` : "";
        let main = esc(it.text || "");
        if (it.title) main = (main ? main + " " : "") + `<b>${esc(it.title)}</b>`;
        if (it.url && !it.title) main = main || esc(it.url);
        const badge = it.url ? '<span class="rec-go">↗</span>'
          : (it.type === 3 ? '<span class="rec-tag">图片</span>'
             : it.type === 34 ? '<span class="rec-tag">语音</span>'
             : it.type === 43 ? '<span class="rec-tag">视频</span>' : "");
        row.innerHTML = `${who}${time}<div class="rec-text">${main}${badge}</div>`;
        if (it.url) row.onclick = (ev) => { ev.stopPropagation(); openLinkPreview(it.url, it.title || it.url); };
        body.appendChild(row);
      });
      if (!(rec.items || []).length) body.textContent = p.des || "（无内容）";
      head.onclick = () => {
        const open = body.style.display !== "none";
        body.style.display = open ? "none" : "block";
        head.querySelector(".rec-toggle").textContent = open ? "展开 ▸" : "收起 ▾";
      };
      el.appendChild(head);
      el.appendChild(body);
      return el;
    }
    case "link": {
      el.className = "card";
      el.innerHTML = `🔗 <span class="t">${esc(p.title || "[链接]")}</span>` +
        (p.des ? `<div class="d">${esc(p.des)}</div>` : "") +
        (p.url ? `<div class="u">${esc(p.url)}</div>` : "") +
        (p.url ? '<div class="go">点击阅读正文 ↗</div>' : "");
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
    const where = isAssistant()
      ? (aiState.currentScope && aiState.currentScope !== "assistant"
          ? `「${scopeNameOf(aiState.currentScope)}」` : "全部聊天记录")
      : "当前会话";
    box.innerHTML = `<div class="qa-empty">还没有问答记录。<br>在下方输入框提问，AI 会在${where}中检索作答。</div>`;
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

/* ---------------- AI 助手的作用域（全局 / 会话 / 文件 / 链接） ---------------- */
function scopeGroup(scope) {
  return aiState.tree.find(g => g.scope === scope) || null;
}
function scopeNameOf(scope) {
  if (!scope || scope === "assistant") return "";
  const g = scopeGroup(scope);
  if (g && g.name) return g.name;
  if (scope.startsWith("moment:")) return "朋友圈";
  if (scope.startsWith("file:")) return "文件问答";
  if (scope.startsWith("link:")) return "链接问答";
  const s = state.sessions.find(x => x.username === scope);
  return s ? s.name : scope;
}
function scopeToggleText(scope) {
  if (scope.startsWith("file:")) return "仅本文件";
  if (scope.startsWith("link:")) return "仅本文";
  if (scope.startsWith("moment:")) return "仅本朋友圈";
  return "仅本会话";
}
function assistantScopeLabel(g) {
  if (g.kind === "file") return "文件：" + g.name;
  if (g.kind === "link") return "链接：" + g.name;
  if (g.kind === "group") return "群聊：" + g.name;
  if (g.kind === "chat") return "会话：" + g.name;
  return g.name || "全局助手";
}
function syncAssistantScopeSelect() {
  const sel = document.querySelector("#assistantScopeSelect");
  if (!sel) return;
  const scope = aiState.currentScope || "assistant";
  sel.innerHTML = `<option value="assistant">全部聊天记录</option>` +
    aiState.tree.filter(g => g.scope !== "assistant")
      .map(g => `<option value="${attrEsc(g.scope)}">${esc(assistantScopeLabel(g))}</option>`)
      .join("");
  if ([...sel.options].some(o => o.value === scope)) {
    sel.value = scope;
  } else if (scope !== "assistant") {
    const opt = document.createElement("option");
    opt.value = scope;
    opt.textContent = scopeNameOf(scope);
    sel.appendChild(opt);
    sel.value = scope;
  }
}
function updateAssistantScopeUI() {
  const wrap = document.querySelector("#assistantScopeWrap");
  if (!wrap) return;
  const scope = aiState.currentScope || "assistant";
  const g = scopeGroup(scope);
  const title = document.querySelector("#assistantTitle");
  const toggleText = document.querySelector("#assistantScopeToggleText");
  if (title) title.textContent = "✦ " + (scope === "assistant" ? "全局助手"
    : (g && g.name ? g.name : scopeNameOf(scope) || "AI 助手"));
  if (scope === "assistant") {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "inline-flex";
  if (toggleText) toggleText.textContent = scopeToggleText(scope);
  const nm = document.querySelector("#assistantScopeName");
  if (nm) nm.textContent = "（" + scopeNameOf(scope) + "）";
}
/* 在 AI 助手右侧列表或范围下拉里选择会话/文件后，提问范围随之切换 */
async function selectAssistantScope(scope, sessionId) {
  aiState.currentScope = scope || "assistant";
  const toggle = document.querySelector("#assistantScopeToggle");
  if (toggle && aiState.currentScope !== "assistant") toggle.checked = true;
  syncAssistantScopeSelect();
  updateAssistantScopeUI();
  const g = scopeGroup(aiState.currentScope);
  const target = sessionId || (g && g.sessions.length ? g.sessions[0].id : null);
  aiState.currentSession = target;
  await loadSessionTurns(target);
  const meta = document.querySelector("#assistantMeta");
  if (meta) meta.textContent = `${state.qaHistory.length} 轮 · 右侧可切换对话`;
  renderAiTree();
}
/* 文件/链接作用域不能只传 scope 字符串，提问前要还原成后端可读的上下文 */
async function resolveScopeAskContext(scope) {
  if (!scope || scope === "assistant" || scope.startsWith("moment:")) return {};
  if (aiState.scopeContexts[scope]) return aiState.scopeContexts[scope];
  try {
    const d = await api("/api/ai/scope/context?scope=" + encodeURIComponent(scope));
    aiState.scopeContexts[scope] = d || {};
  } catch {
    aiState.scopeContexts[scope] = {};
  }
  return aiState.scopeContexts[scope];
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
  if (typeof previewMoment !== "undefined" && previewMoment) {
    return { key: `moment:${previewMoment}`, name: "朋友圈", kind: "moment" };
  }
  if (isAssistant()) {
    const scope = aiState.currentScope;
    if (scope && scope !== "assistant") {
      const g = scopeGroup(scope);
      return { key: scope, name: (g && g.name) || scopeNameOf(scope),
               kind: (g && g.kind) || "chat" };
    }
    return { key: "assistant", name: "全局助手", kind: "assistant" };
  }
  if (!state.current) return { key: "assistant", name: "全局助手", kind: "assistant" };
  return { key: state.current.username, name: state.current.name || state.current.username,
           kind: "chat" };
}

/* 切到某个上下文时，载入"该作用域自己的"最新会话（绝不沿用别的上下文的 session） */
async function enterAiScope(scopeKey, name) {
  aiState.currentScope = scopeKey;
  await loadAiTree();
  syncAssistantScopeSelect();
  updateAssistantScopeUI();
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
    const checked = document.querySelector("#assistantScopeToggle");
    if (!scope || scope === "assistant") return null;
    if (checked && !checked.checked) return null;
    return scope;
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
  if (typeof previewMoment !== "undefined") previewMoment = null;
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

/* ---------------- 全局搜索 ---------------- */
async function globalSearch(q) {
  if (!q.trim()) { state.searchMode = false; showPane("#sessionList"); renderSessions(); return; }
  state.searchMode = true;
  const box = $("#resultList");
  box.innerHTML = `<div class="qa-empty">搜索中…</div>`;
  showPane("#resultList");
  let res;
  try {
    res = await api(`/api/search?q=${encodeURIComponent(q)}&n=40`);
  } catch (e) {
    box.innerHTML = `<div class="qa-empty">搜索失败：${esc(e.message)}</div>`;
    return;
  }
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
  // 从右侧面板选历史对话：中间切换到该对话，作用域跟随该对话
  aiState.currentSession = sessionId;
  try {
    const d = await api(`/api/ai/session/${sessionId}`);
    state.qaHistory = d.turns;
    if (d.scope) aiState.currentScope = d.scope;
    const toggle = document.querySelector("#assistantScopeToggle");
    if (toggle && aiState.currentScope !== "assistant") toggle.checked = true;
    syncAssistantScopeSelect();
    updateAssistantScopeUI();
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
  syncAssistantScopeSelect();
  updateAssistantScopeUI();
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
  const mainExplore = $("#exploreToggle");
  if ($("#assistantExploreMode")) $("#assistantExploreMode").checked = !!mainExplore?.checked;
  if ($("#assistantFastMode")) $("#assistantFastMode").checked = !mainExplore?.checked;
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
      if (s) openChat(s.username, s.name, s.is_group, undefined, { keepListPane: true });
      else openChat(c.username, c.name, c.is_group, undefined, { keepListPane: true });   // 没有聊天记录也开一个空会话
    };
    box.appendChild(d);
  }
  showPane("#contactList");
}
const fileView = { chat: "", kind: "", days: 0, sort: "new", q: "", offset: 0, items: [], hasMore: true, loading: false, stats: null };
let fileSessions = null;
let fileQueryTimer = null;
const FILE_KIND_LABELS = { doc: "文档", sheet: "表格", slide: "演示", image: "图片",
                           archive: "压缩包", media: "音视频", other: "其他" };

function renderFileShell() {
  const box = $("#fileList");
  const opts = (rows, cur) => rows.map(([v, label]) =>
    `<option value="${attrEsc(String(v))}" ${String(v) === String(cur) ? "selected" : ""}>${esc(label)}</option>`).join("");
  const chats = [["", "全部会话"], ...fileSessions.slice(0, 150).map(s => [s.username, s.name || s.username])];
  const kindRows = Object.entries(FILE_KIND_LABELS).map(([k, label]) => {
    const st = fileView.stats && fileView.stats.kinds && fileView.stats.kinds[k];
    return [k, st ? `${label} ${st.parsed}/${st.total}` : label];
  });
  box.innerHTML = `
    <div class="files-toolbar">
      <div class="files-filters">
        <select id="fileChat">${opts(chats, fileView.chat)}</select>
        <select id="fileKind">${opts([["", "全部类型"], ...kindRows], fileView.kind)}</select>
        <select id="fileDays">${opts([[0, "不限时间"], [7, "最近 7 天"], [30, "最近 30 天"], [365, "最近一年"]], fileView.days)}</select>
        <select id="fileSort">${opts([["new", "最新在前"], ["old", "最早在前"], ["size", "文件最大"]], fileView.sort)}</select>
      </div>
      <div class="files-meta" id="fileMeta">加载中…</div>
    </div>
    <div id="fileItems"></div>
    <button id="fileMore" class="mini ghost files-more" style="display:none">加载更多</button>`;
  const onChange = () => {
    fileView.chat = $("#fileChat").value;
    fileView.kind = $("#fileKind").value;
    fileView.days = +$("#fileDays").value || 0;
    fileView.sort = $("#fileSort").value;
    refreshFiles(true);
  };
  ["#fileChat", "#fileKind", "#fileDays", "#fileSort"].forEach(sel => $(sel).onchange = onChange);
  $("#fileMore").onclick = () => refreshFiles(false);
}

function renderFileItems() {
  const box = $("#fileItems");
  if (!box) return;
  box.innerHTML = "";
  if (!fileView.items.length) {
    box.innerHTML = `<div class="qa-empty">没有找到文件消息。<br>换个会话或类型试试，文件消息同步后会自动出现在这里。</div>`;
  }
  for (const f of fileView.items) {
    const d = document.createElement("div");
    d.className = "result-item";
    const badge = (f.ext || "").replace(".", "").toUpperCase() || FILE_KIND_LABELS[f.kind] || "文件";
    const state = f.parsed ? "已解析" : (f.status === "missing" ? "未下载/解析失败" : "未处理");
    const stateCls = f.parsed ? "file-state ok" : "file-state warn";
    d.innerHTML = `<div class="meta"><span class="file-badge">${esc(badge)}</span> ${esc(f.chat_name)} · ${fmtTime(f.ts)} · ${fmtSize(f.size)}
        · <span class="${stateCls}">${state}</span></div>
      <div class="snippet">${esc(f.title)}</div>`;
    d.onclick = () => openFilePreview(f);
    box.appendChild(d);
  }
  const meta = $("#fileMeta");
  if (meta) {
    let statsText = "";
    const st = fileView.stats;
    if (st) {
      if (fileView.kind && st.kinds && st.kinds[fileView.kind]) {
        const k = st.kinds[fileView.kind];
        statsText = `${FILE_KIND_LABELS[fileView.kind] || "当前类型"} 已解析 ${k.parsed}/${k.total} · `;
      }
      statsText += `全部 已解析 ${st.parsed}/${st.total}（未下载/失败 ${st.missing || 0} · 未处理 ${st.unprocessed || 0}）`;
    }
    meta.textContent = `${fileView.items.length} 个文件${statsText ? " · " + statsText : ""}${fileView.hasMore ? " · 还有更多" : ""}`;
  }
  const more = $("#fileMore");
  if (more) more.style.display = fileView.hasMore ? "" : "none";
}

async function refreshFiles(reset = true) {
  if (fileView.loading) return;
  if (reset) { fileView.offset = 0; fileView.items = []; }
  fileView.loading = true;
  const meta = $("#fileMeta");
  if (meta) meta.textContent = "加载中…";
  const qs = new URLSearchParams({ n: "200", offset: String(fileView.offset), sort: fileView.sort });
  if (fileView.chat) qs.set("chat", fileView.chat);
  if (fileView.kind) qs.set("kind", fileView.kind);
  if (fileView.q) qs.set("q", fileView.q);
  if (fileView.days) qs.set("since", String(Math.floor(Date.now() / 1000) - fileView.days * 86400));
  try {
    const list = await api("/api/files?" + qs.toString());
    fileView.items.push(...list);
    fileView.offset += list.length;
    fileView.hasMore = list.length >= 200;
    renderFileItems();
  } catch (e) {
    if (meta) meta.textContent = "加载失败：" + e.message;
  } finally {
    fileView.loading = false;
  }
}

async function showFiles(opts = {}) {
  if (typeof opts === "string") opts = { q: opts };
  Object.assign(fileView, opts);
  if (!fileSessions) {
    try { fileSessions = await api("/api/sessions"); } catch { fileSessions = []; }
  }
  if (!fileView.stats) {
    try { fileView.stats = await api("/api/files/stats"); } catch { fileView.stats = null; }
  }
  renderFileShell();
  await refreshFiles(true);
  showPane("#fileList");
}
const linkView = { platform: "", state: "", days: 0, sort: "new", q: "",
                   offset: 0, items: [], hasMore: true, loading: false, queue: null };
let linkQueryTimer = null;

function renderLinkShell() {
  const box = $("#linkList");
  const opts = (rows, cur) => rows.map(([v, label]) =>
    `<option value="${attrEsc(String(v))}" ${String(v) === String(cur) ? "selected" : ""}>${esc(label)}</option>`).join("");
  box.innerHTML = `
    <div class="files-toolbar">
      <div class="files-filters">
        <select id="linkPlatform">${opts([["", "全部平台"], ["xhs", "小红书"], ["bili", "B站"],
          ["article", "公众号"], ["generic", "网页"], ["wxapp", "小程序"]], linkView.platform)}</select>
        <select id="linkState">${opts([["", "全部状态"], ["parsed", "已解析"],
          ["pending", "解析中"], ["failed", "暂不支持"]], linkView.state)}</select>
        <select id="linkDays">${opts([[0, "不限时间"], [7, "最近 7 天"],
          [30, "最近 30 天"], [365, "最近一年"]], linkView.days)}</select>
        <select id="linkSort">${opts([["new", "最新在前"], ["old", "最早在前"]], linkView.sort)}</select>
      </div>
      <div class="files-meta" id="linkMeta">加载中…</div>
    </div>
    <div id="linkItems"></div>
    <button id="linkMore" class="mini ghost files-more" style="display:none">加载更多</button>`;
  const onChange = () => {
    linkView.platform = $("#linkPlatform").value;
    linkView.state = $("#linkState").value;
    linkView.days = +$("#linkDays").value || 0;
    linkView.sort = $("#linkSort").value;
    refreshLinks(true);
  };
  ["#linkPlatform", "#linkState", "#linkDays", "#linkSort"].forEach(sel => $(sel).onchange = onChange);
  $("#linkMore").onclick = () => refreshLinks(false);
}

function renderLinkItems() {
  const box = $("#linkItems");
  if (!box) return;
  box.innerHTML = "";
  if (!linkView.items.length) {
    box.innerHTML = `<div class="qa-empty">没有符合条件的链接。<br>
      换个平台/状态或时间范围试试；聊天里的链接会自动登记并解析。</div>`;
  }
  for (const l of linkView.items) {
    const d = document.createElement("div");
    d.className = "result-item";
    const badge = l.parsed
      ? `<span class="link-badge ok">已解析</span>`
      : l.platform_key === "wxapp" ? `<span class="link-badge">仅卡片信息</span>`
      : l.state === 2 ? `<span class="link-badge fail">暂不支持</span>`
      : `<span class="link-badge">解析中</span>`;
    d.innerHTML = `<div class="meta">${esc(l.chat_name)} · ${fmtTime(l.received)} ·
        <span class="link-plat">${esc(l.platform)}</span> ${badge}</div>
      <div class="snippet">🔗 ${esc(l.title)}${l.chars ? ` · ${l.chars} 字` : ""}</div>`;
    d.onclick = () => {
      if (l.url.startsWith("wxapp://")) {
        toast("小程序内容需要在微信里打开，无法抓取正文");
        return;
      }
      openLinkPreview(l.url, l.title);
    };
    box.appendChild(d);
  }
  const meta = $("#linkMeta");
  if (meta) {
    const parsedN = linkView.items.filter(x => x.parsed).length;
    let extra = "";
    const q = linkView.queue;
    if (q) {
      extra = ` · 待解析 ${q.pending}`;
      if (q.current) extra += ` · 正在解析：${esc(q.current.slice(0, 40))}`;
      if (q.error) extra += ` · 最近失败：${esc(q.error.slice(0, 40))}`;
    }
    meta.textContent = `${linkView.items.length} 条链接 · ${parsedN} 条已解析${extra}`;
  }
  const more = $("#linkMore");
  if (more) more.style.display = linkView.hasMore ? "" : "none";
}

async function refreshLinks(reset = true) {
  if (linkView.loading) return;
  if (reset) { linkView.offset = 0; linkView.items = []; }
  linkView.loading = true;
  const meta = $("#linkMeta");
  if (meta) meta.textContent = "加载中…";
  const qs = new URLSearchParams({ n: "100", offset: String(linkView.offset), sort: linkView.sort });
  if (linkView.platform) qs.set("platform", linkView.platform);
  if (linkView.state) qs.set("state", linkView.state);
  if (linkView.days) qs.set("days", String(linkView.days));
  if (linkView.q) qs.set("q", linkView.q);
  try {
    const [list, queue] = await Promise.all([
      api("/api/links?" + qs.toString()),
      api("/api/links/queue").catch(() => null)]);
    linkView.items.push(...list);
    linkView.offset += list.length;
    linkView.hasMore = list.length >= 100;
    linkView.queue = queue;
    renderLinkItems();
  } catch (e) {
    if (meta) meta.textContent = "加载失败：" + e.message;
  } finally {
    linkView.loading = false;
  }
}

async function showLinks() {
  renderLinkShell();
  await refreshLinks(true);
  showPane("#linkList");
}

/* ---------------- 朋友圈（本地永久留存 + 实时增量同步） ---------------- */
let previewMoment = null;
let snsOffset = 0;

async function showMoments(q = "", reset = true) {
  const box = $("#snsList");
  if (reset) { snsOffset = 0; box.innerHTML = `<div class="qa-empty">加载中…</div>`; }
  let data;
  try {
    data = await api(`/api/sns/posts?offset=${snsOffset}&limit=50&q=${encodeURIComponent(q)}`);
  } catch (e) {
    box.innerHTML = `<div class="qa-empty">加载失败：${esc(e.message)}</div>`;
    showPane("#snsList");
    return;
  }
  if (reset) box.innerHTML = "";
  if (!data.items.length && !snsOffset) {
    box.innerHTML = `<div class="qa-empty">还没有朋友圈数据。<br>服务启动时会自动从微信解密库同步，稍等片刻再试。</div>`;
    showPane("#snsList");
    return;
  }
  if (reset) {
    const head = document.createElement("div");
    head.className = "qa-empty";
    head.id = "snsStatusBar";
    box.appendChild(head);
    refreshSnsStatus();
  }
  for (const p of data.items) {
    const d = document.createElement("div");
    d.className = "result-item sns-item";
    const media = [
      p.n_img ? `🖼 ${p.n_img}${p.n_local ? `<span class="sns-local">·${p.n_local}</span>` : ""}` : "",
      p.n_vid ? `🎬 ${p.n_vid}` : "",
      p.n_links ? `🔗 ${p.n_links}` : "",
      p.n_comments ? `💬 ${p.n_comments}` : ""].filter(Boolean).join(" ");
    d.innerHTML = `<div class="sns-item-main">
        <div class="meta">${esc(p.author_name || p.author)} · ${fmtTime(p.ts)} ${media}</div>
        <div class="snippet">${p.text ? esc(p.text.slice(0, 120)) : "<i>(纯图/视频)</i>"}</div>
      </div>` +
      (p.first_img != null
        ? `<img class="sns-thumb" loading="lazy" decoding="async"
             src="/api/sns/image?tid=${encodeURIComponent(p.tid)}&idx=${p.first_img}">`
        : "");
    d.onclick = () => openMomentDetail(p.tid);
    box.appendChild(d);
  }
  snsOffset += data.items.length;
  if (data.items.length && data.total > snsOffset) {
    const more = document.createElement("div");
    more.className = "qa-empty load-more-sns";
    more.textContent = "加载更多…";
    more.onclick = () => { more.textContent = "加载中…"; showMoments(q, false); };
    box.appendChild(more);
  }
  showPane("#snsList");
}

async function openMomentDetail(tid) {
  showChatPane();
  $("#assistantPane").style.display = "none";
  $("#aiSessionsPanel").style.display = "none";
  $("#settingsFull").style.display = "none";
  document.querySelector(".chat-column").style.display = "flex";
  $("#msgList").style.display = "none";
  $("#previewPane").style.display = "flex";
  $("#previewPane").style.flexDirection = "column";
  $("#backToChat").style.display = "inline";
  previewMoment = tid;
  $("#pvBody").innerHTML = `<div class="qa-empty">加载中…</div>`;
  $("#drawer").style.display = "flex";
  $("#drawerFab").style.display = "none";
  let p;
  try {
    p = await api(`/api/sns/post/${encodeURIComponent(tid)}`);
  } catch (e) {
    $("#pvBody").innerHTML = `<div class="qa-empty">加载失败：${esc(e.message)}</div>`;
    return;
  }
  if (previewMoment !== tid) return;      // 用户已切走
  const name = p.author_name || p.author;
  $("#chatTitle").textContent = `${name} 的朋友圈`;
  $("#chatMeta").textContent = `${fmtTime(p.ts)}${p.location ? " · " + p.location : ""}`;
  // 每条朋友圈有自己独立的 AI 会话作用域
  await enterAiScope(`moment:${tid}`, `${name} 的朋友圈`);
  $("#scopeToggle").checked = true;
  updateScopeBar();
  updateDrawerScope();
  $("#input").placeholder = "针对这条朋友圈提问…";
  const imgs = (p.media || []).filter(m => !m.video);
  const vids = (p.media || []).filter(m => m.video);
  let html = `<div class="pv-head"><span class="pv-name">${esc(name)}</span>
      <span class="pv-time">${fmtTime(p.ts)}${p.location ? " · 📍" + esc(p.location) : ""}</span></div>`;
  if (p.text) html += `<pre class="pv-text sns-text">${esc(p.text)}</pre>`;
  for (const l of (p.links || [])) {
    html += `<div class="sns-link" data-url="${esc(l.url)}">
      <span class="sns-link-ico">🔗</span>
      <span class="sns-link-body"><span class="sns-link-title">${esc(l.title || l.url)}</span>
      <span class="sns-link-url">${esc(l.url.slice(0, 80))}</span></span>
      <span class="sns-link-go">读正文 ↗</span></div>`;
  }
  if (imgs.length) {
    html += `<div class="sns-img-grid">` + imgs.map(m => {
      const tip = m.ocr ? esc(m.ocr.slice(0, 80)) : "点击放大";
      return `<div class="sns-img-wrap" title="${tip}">
          <div class="sns-img-ph loading">…</div>
          <img class="sns-img" loading="lazy" decoding="async" alt="图 ${m.i + 1}"
             data-idx="${m.i}"
             src="/api/sns/image?tid=${encodeURIComponent(tid)}&idx=${m.i}"
             onload="const ph=this.parentNode.querySelector('.sns-img-ph'); if(ph) ph.remove();"
             onerror="const w=this.parentNode; const ph=w && w.querySelector('.sns-img-ph'); if(ph){ ph.textContent='拉取失败'; ph.classList.remove('loading'); } if(this.parentNode) this.remove();">
        </div>`;
    }).join("") + `</div>`;
  }
  for (const v of vids) {
    if (v.playable) {
      html += `<video class="sns-video" controls preload="metadata"
          src="/api/sns/video?tid=${encodeURIComponent(tid)}&idx=${v.i}"
          onerror="this.outerHTML='<div class=sns-video-ph>🎬 视频（${v.dur || "?"} 秒）— CDN 拉取失败，无法回放</div>'"></video>`;
    } else {
      html += `<div class="sns-video-ph">🎬 视频（${v.dur ? v.dur + " 秒" : "时长未知"}）— 无可用的视频源</div>`;
    }
  }
  const ocrAll = (p.media || []).filter(m => m.ocr && m.ocr.trim());
  if (ocrAll.length) {
    html += `<div class="sns-ocr-box"><div class="sns-ocr-toggle">图片文字（OCR，${ocrAll.length} 张） ▸</div>
      <div class="sns-ocr-body" style="display:none">` +
      ocrAll.map(m => `<div class="sns-ocr-item"><b>图 ${m.i + 1}</b><pre>${esc(m.ocr)}</pre></div>`).join("") +
      `</div></div>`;
  }
  if ((p.comments || []).length) {
    html += `<div class="sns-comments"><div class="sns-c-head">评论 ${p.comments.filter(c => c.ctype !== 1).length} · 赞 ${p.n_likes || 0}</div>` +
      p.comments.map(c => c.ctype === 1
        ? `<div class="sns-like-line">👍 ${esc(c.nick || "好友")}</div>`
        : `<div class="sns-comment"><span class="who">${esc(c.nick || "好友")}</span>
             <span class="text">${esc(c.text || "")}</span>
             <span class="ts">${fmtTime(c.ts)}</span></div>`).join("") +
      `</div>`;
  }
  html += `<div class="sns-hint">本条已永久保存在本地知识库：作者删除或设为“三天可见”不影响这份留存。</div>`;
  $("#pvBody").innerHTML = html;
  const og = $("#pvBody .sns-ocr-toggle");
  if (og) og.onclick = () => {
    const b = $("#pvBody .sns-ocr-body");
    const open = b.style.display !== "none";
    b.style.display = open ? "none" : "block";
    og.textContent = `图片文字（OCR，${ocrAll.length} 张） ${open ? "▸" : "▾"}`;
  };
  $("#pvBody").querySelectorAll(".sns-img-wrap img").forEach(el => {
    el.onclick = () => {
      const idx = +el.dataset.idx;
      const m = (p.media || []).find(x => x.i === idx);
      openMomentImage(tid, idx, m ? (m.ocr || "") : "", name, p.ts);
    };
  });
  $("#pvBody").querySelectorAll(".sns-link").forEach(el => {
    el.onclick = () => openLinkPreview(el.dataset.url, el.querySelector(".sns-link-title").textContent);
  });
  $("#input").focus();
}

/* 朋友圈单图查看：与聊天消息的图片查看器同款（预览面板内展示，点击返回朋友圈） */
function openMomentImage(tid, idx, ocr, author, ts) {
  $("#chatTitle").textContent = "朋友圈图片";
  $("#chatMeta").textContent = `${author || ""}${ts ? " · " + fmtTime(ts) : ""}`;
  $("#pvBody").innerHTML =
    `<div class="pv-head"><span class="pv-name" id="snsImgBack"
        style="cursor:pointer">← 返回朋友圈</span></div>
     <img src="/api/sns/image?tid=${encodeURIComponent(tid)}&idx=${idx}"
          class="pv-img" style="max-height:62vh">
     ${ocr && ocr.trim()
       ? `<div class="pv-seg"><div class="pv-seg-title">图片文字（OCR）</div>
            <pre class="pv-text">${esc(ocr)}</pre></div>`
       : ""}`;
  $("#snsImgBack").onclick = () => openMomentDetail(tid);
  const img = $("#pvBody .pv-img");
  if (img) img.onclick = () => openMomentDetail(tid);
}

function openMomentByTid(tid) { openMomentDetail(String(tid)); }

async function refreshSnsStatus() {
  try {
    const st = await api("/api/sns/status");
    const el = $("#snsStatusBar");
    if (el) el.textContent = `${st.posts} 条朋友圈 · ${st.comments} 评论 · 图片文字 ${st.ocr_done} 张` +
      (st.loop_error ? ` · 同步异常: ${st.loop_error}` : "") +
      (st.sync_running ? " · 同步中…" : "");
  } catch {}
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
    + row("links", "公众号正文", j.links, true)
    + row("files", "文件正文", j.files, true)
    + row("snsocr", "朋友圈 OCR", j.snsocr, true);
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
  { id: "model", label: "AI 模型", icon: "✦", desc: "接口与回答方式" },
  { id: "privacy", label: "隐私与脱敏", icon: "⊘", desc: "发往云端前屏蔽敏感信息" },
  { id: "bot", label: "机器人", icon: "⇄", desc: "QQ 与微信 ClawBot" },
  { id: "data", label: "数据与同步", icon: "↻", desc: "实时入库与定时任务" },
  { id: "index", label: "索引任务", icon: "⌁", desc: "搜索与媒体处理" },
  { id: "about", label: "关于", icon: "i", desc: "状态与本地数据" },
];
let settingsSection = "model";

async function showSettings(section) {
  settingsSection = section || settingsSection || "model";
  // 离开「微信通道」时停掉二维码状态轮询，避免对已卸载的 DOM 反复刷新
  if (settingsSection !== "bot" && window._ocTimer) {
    clearInterval(window._ocTimer);
    window._ocTimer = null;
  }
  // 设置目录放中栏(与会话列表同列),右侧内容区占满
  showPane("#settingsNav");
  $("#chatEmpty").style.display = "none";
  $("#chatMain").style.display = "flex";
  document.querySelector(".chat-column").style.display = "none";
  $("#drawer").style.display = "none";
  $("#assistantPane").style.display = "none";
  $("#aiSessionsPanel").style.display = "none";
  $("#previewPane").style.display = "none";
  document.querySelector("#settingsFull").style.display = "flex";
  const status = await api("/api/status").catch(() => null);
  const nav = document.querySelector("#settingsNav");
  nav.innerHTML = SETTING_SECTIONS.map(x =>
    `<button class="sf-item ${x.id === settingsSection ? "active" : ""}" data-sec="${x.id}" aria-current="${x.id === settingsSection ? "page" : "false"}">
      <span class="sf-item-icon">${x.icon}</span><span class="sf-item-copy"><b>${esc(x.label)}</b><small>${esc(x.desc)}</small></span><span class="sf-arrow">›</span>
    </button>`
  ).join("");
  nav.querySelectorAll(".sf-item").forEach(el => el.onclick = () => showSettings(el.dataset.sec));
  const body = document.querySelector("#sfBody");
  body.innerHTML = `<div class="qa-empty">加载中…</div>`;

  if (settingsSection === "model") await renderSettingsModel(body);
  if (settingsSection === "privacy") await renderSettingsPrivacy(body);
  if (settingsSection === "bot") await renderSettingsBot(body);
  if (settingsSection === "data") await renderSettingsData(body);
  if (settingsSection === "index") await renderSettingsIndex(body);
  if (settingsSection === "about") renderSettingsAbout(body, status);
}

async function renderSettingsModel(body) {
  const c = await api("/api/config");
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">AI CONFIGURATION</span><h2>AI 模型</h2><p>配置问答引擎，获得更自然、更完整的知识库回答。</p></div>
      <span class="settings-status-pill ${c.has_key ? "success" : "warning"}">${c.has_key ? "已配置" : "待配置"}</span></div>
    <div class="settings-grid"><section class="settings-card settings-card-primary">
      <div class="settings-card-head"><span class="settings-card-icon">✦</span><div><h3>连接配置</h3><p>支持任意 OpenAI 兼容接口。</p></div></div>
      <div class="settings-form">
        <div class="settings-field"><label for="cfgBase">接口地址</label><input id="cfgBase" value="${attrEsc(c.base_url)}" placeholder="https://api.example.com/v1"><small>通常以 /v1 结尾，需包含完整协议。</small></div>
        <div class="settings-field"><label for="cfgModel">模型名</label><input id="cfgModel" value="${attrEsc(c.model)}" placeholder="deepseek-chat"></div>
        <div class="settings-field"><label for="cfgKey">API Key</label><input id="cfgKey" type="password" autocomplete="off" placeholder="${c.has_key ? attrEsc("已配置 " + c.key_masked + "（留空则不修改）") : "sk-..."}"><small>密钥只保存在本机，不会展示完整内容。</small></div>
        <div class="settings-switch-card"><label class="switch-row"><input type="checkbox" id="cfgThinking" ${c.thinking ? "checked" : ""}><span class="switch-track"></span><span><b>启用深度思考</b><small>适合复杂检索、归纳和多步推理。</small></span></label>
          <div class="settings-field nested-field"><label for="cfgThinkingModel">推理模型</label><input id="cfgThinkingModel" value="${attrEsc(c.thinking_model || "")}" placeholder="deepseek-reasoner"></div></div>
      </div>
      <div class="settings-actions"><button id="saveCfg">保存配置</button><button id="testCfg" class="ghost">测试连接</button><span id="cfgMsg" class="msg" aria-live="polite"></span></div>
    </section><aside class="settings-side-card"><div class="settings-side-title">配置提示</div><p>推荐使用稳定的 OpenAI 兼容服务，接口地址和模型名需匹配。</p><ul><li>Key 仅存本机</li><li>留空 Key 可保留原配置</li><li>保存后可立即测试连通性</li></ul></aside></div>`;
  $("#saveCfg").onclick = async () => {
    const msg = $("#cfgMsg");
    $("#saveCfg").disabled = true; $("#testCfg").disabled = true; $("#saveCfg").textContent = "保存中…";
    try {
      await api("/api/config", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: $("#cfgBase").value, model: $("#cfgModel").value,
                               api_key: $("#cfgKey").value,
                               thinking: $("#cfgThinking").checked,
                               thinking_model: $("#cfgThinkingModel").value }) });
      msg.textContent = "已保存"; msg.className = "msg ok";
      setTimeout(() => showSettings("model"), 600);
    } catch (e) { msg.textContent = "保存失败: " + e.message; msg.className = "msg err"; $("#saveCfg").disabled = false; $("#testCfg").disabled = false; $("#saveCfg").textContent = "保存配置"; }
  };
  $("#testCfg").onclick = async () => {
    const msg = $("#cfgMsg");
    $("#testCfg").disabled = true; $("#saveCfg").disabled = true; $("#testCfg").textContent = "测试中…";
    msg.textContent = "测试中…"; msg.className = "msg";
    try {
      const r = await api("/api/config/test", { method: "POST" });
      msg.textContent = `连接成功（${r.model}）：${r.reply}`; msg.className = "msg ok";
    } catch (e) { msg.textContent = e.message; msg.className = "msg err"; }
    finally { $("#testCfg").disabled = false; $("#saveCfg").disabled = false; $("#testCfg").textContent = "测试连接"; }
  };
}

async function renderSettingsPrivacy(body) {
  let p;
  try { p = await api("/api/privacy"); }
  catch (e) { body.innerHTML = `<div class="qa-empty">加载失败: ${esc(e.message)}</div>`; return; }
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">OUTBOUND PRIVACY</span><h2>隐私与脱敏</h2><p>发给云端模型前自动替换手机号、身份证、银行卡和邮箱，本机原文与引用不受影响。</p></div>
      <span class="settings-status-pill ${p.enabled ? "success" : "neutral"}">${p.enabled ? "脱敏已开启" : "脱敏已关闭"}</span></div>
    <div class="settings-grid"><section class="settings-card">
      <div class="settings-card-head"><span class="settings-card-icon">⊘</span><div><h3>出站规则</h3><p>仅在调用模型接口时生效，不改动本地入库数据。</p></div></div>
      <div class="settings-switch-card">
        <label class="switch-row"><input type="checkbox" id="pvEnabled" ${p.enabled ? "checked" : ""}><span class="switch-track"></span><span><b>启用脱敏</b><small>关闭后上下文原样发送到模型服务商。</small></span></label>
        <label class="switch-row"><input type="checkbox" id="pvPhone" ${p.phone ? "checked" : ""} ${p.enabled ? "" : "disabled"}><span class="switch-track"></span><span><b>手机号</b><small>替换为 [手机号]</small></span></label>
        <label class="switch-row"><input type="checkbox" id="pvIdcard" ${p.idcard ? "checked" : ""} ${p.enabled ? "" : "disabled"}><span class="switch-track"></span><span><b>身份证号</b><small>替换为 [身份证]</small></span></label>
        <label class="switch-row"><input type="checkbox" id="pvBank" ${p.bankcard ? "checked" : ""} ${p.enabled ? "" : "disabled"}><span class="switch-track"></span><span><b>银行卡号</b><small>替换为 [银行卡]</small></span></label>
        <label class="switch-row"><input type="checkbox" id="pvEmail" ${p.email ? "checked" : ""} ${p.enabled ? "" : "disabled"}><span class="switch-track"></span><span><b>邮箱</b><small>替换为 [邮箱]</small></span></label>
      </div>
      <div class="settings-actions"><button id="pvSave">保存设置</button><span id="pvMsg" class="msg" aria-live="polite"></span></div>
    </section><aside class="settings-side-card"><div class="settings-side-title">说明</div><p>脱敏发生在每次模型请求前，修改后无需重启服务。</p><ul><li>只影响新发出的请求</li><li>检索、引用跳转使用本地原文</li><li>本地 AI 模式同样适用</li></ul></aside></div>`;
  const rules = ["pvPhone", "pvIdcard", "pvBank", "pvEmail"];
  const syncDisabled = () => rules.forEach(id => $("#" + id).disabled = !$("#pvEnabled").checked);
  $("#pvEnabled").onchange = syncDisabled;
  $("#pvSave").onclick = async () => {
    const msg = $("#pvMsg");
    $("#pvSave").disabled = true; $("#pvSave").textContent = "保存中…";
    try {
      await api("/api/privacy", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: $("#pvEnabled").checked,
                               phone: $("#pvPhone").checked,
                               idcard: $("#pvIdcard").checked,
                               bankcard: $("#pvBank").checked,
                               email: $("#pvEmail").checked }) });
      msg.textContent = "已保存"; msg.className = "msg ok";
      setTimeout(() => showSettings("privacy"), 600);
    } catch (e) {
      msg.textContent = "保存失败: " + e.message; msg.className = "msg err";
      $("#pvSave").disabled = false; $("#pvSave").textContent = "保存设置";
    }
  };
}

async function renderSettingsBot(body) {
  if (window._ocTimer) { clearInterval(window._ocTimer); window._ocTimer = null; }
  let q, oc;
  try {
    [q, oc] = await Promise.all([api("/api/qqbot/status"), api("/api/openclaw/status")]);
  } catch (e) {
    body.innerHTML = `<div class="qa-empty">加载失败: ${esc(e.message)}</div>`;
    return;
  }
  const qqState = !q.enabled ? "未启用" : q.error ? "异常" : q.connected ? "已连接" : q.running ? "连接中…" : "已停止";
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">BOT CHANNELS</span><h2>机器人</h2><p>在微信或 QQ 里直接向本机知识库提问，两个通道可单独开关。</p></div></div>
    <div class="settings-grid"><div class="settings-stack">

      <section class="settings-card" id="ocCard">
        <div class="settings-card-head"><span class="settings-card-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="9" cy="8" rx="6.2" ry="5" fill="#07c160"/><ellipse cx="15.2" cy="14" rx="6.2" ry="5" fill="#3ddc84"/><circle cx="6.8" cy="7.4" r="1" fill="#fff"/><circle cx="11.2" cy="7.4" r="1" fill="#fff"/><circle cx="13.2" cy="13.4" r="1" fill="#fff"/><circle cx="17.2" cy="13.4" r="1" fill="#fff"/></svg></span><div><h3>微信通道</h3><p>扫码接入 ClawBot，微信里直接提问。</p></div></div>
        <label class="switch-row"><input type="checkbox" id="ocEnabled" ${oc.enabled ? "checked" : ""}><span class="switch-track"></span><span><b>启用微信通道</b><small>关闭后停止网关，微信里不再回复。</small></span></label>
        <div class="settings-note" id="ocStatus" style="margin-top:12px">加载中…</div>
        <div class="settings-actions" style="margin-top:12px;flex-wrap:wrap">
          <button id="ocLogin" ${oc.enabled ? "" : "disabled"}>扫码登录</button>
          <button id="ocLoginStop" class="ghost" style="display:none">停止登录</button>
          <button id="ocGateway" class="ghost" ${oc.enabled ? "" : "disabled"}>重启网关</button>
        </div>
        <div id="ocQrWrap" style="display:none">
          <div class="oc-qr-title">用手机微信扫描二维码</div>
          <img id="ocQrImg" class="oc-qr-img" alt="微信登录二维码">
        </div>
        <div class="settings-note" id="ocLogHint" style="display:none"></div>
      </section>

      <section class="settings-card" id="qqCard">
        <div class="settings-card-head"><span class="settings-card-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="12.5" rx="6" ry="7.6" fill="#12b7f5"/><ellipse cx="12" cy="14.6" rx="3.8" ry="5" fill="#fff"/><circle cx="9.6" cy="7" r="1.2" fill="#fff"/><circle cx="14.4" cy="7" r="1.2" fill="#fff"/><circle cx="9.6" cy="7" r=".5" fill="#0b2b44"/><circle cx="14.4" cy="7" r=".5" fill="#0b2b44"/><ellipse cx="12" cy="8.9" rx=".95" ry=".6" fill="#ff9f1a"/></svg></span><div><h3>QQ 机器人</h3><p>通过 QQ 官方机器人协议接入；回答在 QQ 里直接发回。</p></div></div>
        <label class="switch-row"><input type="checkbox" id="qqEnabled" ${q.enabled ? "checked" : ""}><span class="switch-track"></span><span><b>启用 QQ 机器人</b><small>仅本人私聊可用；群聊不响应。</small></span></label>
        <div class="settings-form" style="margin-top:14px">
          <div class="settings-field"><label for="qqAppId">AppID</label><input id="qqAppId" value="${attrEsc(q.app_id || "")}" placeholder="1906230635"></div>
          <div class="settings-field"><label for="qqSecret">AppSecret</label><input id="qqSecret" type="password" autocomplete="off" placeholder="${q.has_secret ? attrEsc("已配置 " + q.secret_masked + "（留空则不修改）") : "机器人密钥"}"><small>只保存在本机 config.json，不会上传。</small></div>
        </div>
        <div class="settings-actions"><button id="qqSave">保存并应用</button><button id="qqMenu" class="ghost">重新推送菜单</button><span id="qqMsg" class="msg" aria-live="polite"></span></div>
        <div class="settings-note">状态：${qqState}${q.error ? " · " + esc(q.error) : ""}${q.menu ? " · 菜单：" + (q.menu === "ok" ? "已自动配置" : q.menu === "pushing" ? "推送中…" : "推送失败" + (q.menu_error ? "（" + esc(q.menu_error) + "）" : "")) : ""}</div>
      </section>

    </div>
    <aside class="settings-side-card"><div class="settings-side-title">微信接入</div>
      <p>点「扫码登录」→ 手机微信扫码并确认 → 微信里会自动出现 ClawBot 联系人，直接发消息即可。</p>
      <p style="margin-top:8px">登录凭据保存在本机，重启软件或电脑都不需要重新扫码。</p>
      <div class="settings-side-title" style="margin-top:12px">QQ 接入</div>
      <ol style="margin:0;padding-left:18px;font-size:12px;line-height:1.85">
        <li>打开 <a href="https://q.qq.com" target="_blank" rel="noreferrer">q.qq.com</a> 登录，进入「机器人管理」创建或选择机器人。</li>
        <li>进入「开发设置」，复制 AppID 和 AppSecret（Secret 需点击显示或重置后复制）。</li>
        <li>「服务接入 / 消息推送设置」选择 WebSocket，不要选 Webhook。</li>
        <li>「事件订阅」勾选 C2C_MESSAGE_CREATE（私聊），群聊不用勾。</li>
        <li>「开发体验用户」添加你的 QQ 号或手机号，否则私聊会提示服务不可用。</li>
        <li>回到本页填入 AppID / AppSecret，打开开关，点「保存并应用」。</li>
        <li>在 QQ 里给机器人发一条消息完成授权，之后即可直接提问。</li>
      </ol>
      <p style="margin-top:8px"><b>QQ 菜单</b>：保存并应用时会自动把 帮助 / 提问方式 / 思考深度 / 新对话 推送成会话底部按钮（首次启用或更换凭据时）；失败可点「重新推送菜单」。不配也能直接在 QQ 里发指令。</p>
    </aside></div>`;

  /* ---------- QQ ---------- */
  $("#qqSave").onclick = async () => {
    const msg = $("#qqMsg");
    $("#qqSave").disabled = true; $("#qqSave").textContent = "保存中…";
    msg.textContent = ""; msg.className = "msg";
    try {
      const payload = { app_id: $("#qqAppId").value, enabled: $("#qqEnabled").checked };
      const sec = $("#qqSecret").value;
      if (sec) payload.app_secret = sec;
      const r = await api("/api/qqbot/config", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      if (r && r.error) { msg.textContent = "已保存，但连接异常: " + r.error; msg.className = "msg err"; }
      else { msg.textContent = "已保存并应用"; msg.className = "msg ok"; }
    } catch (e) { msg.textContent = "保存失败: " + e.message; msg.className = "msg err"; }
    finally { $("#qqSave").disabled = false; $("#qqSave").textContent = "保存并应用";
      setTimeout(() => showSettings("bot"), 1200); }
  };
  $("#qqMenu").onclick = async () => {
    const msg = $("#qqMsg"), b = $("#qqMenu");
    b.disabled = true; b.textContent = "配置中…"; msg.textContent = ""; msg.className = "msg";
    try {
      const r = await api("/api/qqbot/menu", { method: "POST" });
      if (r && r.ok) { msg.textContent = "菜单已推送到 QQ"; msg.className = "msg ok"; }
      else { msg.textContent = "菜单配置失败: " + ((r && (r.error || r.body)) || "未知错误"); msg.className = "msg err"; }
    } catch (e) { msg.textContent = "菜单配置失败: " + e.message; msg.className = "msg err"; }
    finally { b.disabled = false; b.textContent = "重新推送菜单"; }
  };

  /* ---------- 微信 ---------- */
  $("#ocEnabled").onchange = async () => {
    const want = $("#ocEnabled").checked;
    $("#ocEnabled").disabled = true;
    $("#ocStatus").textContent = want ? "正在启用（重启网关约 40 秒）…" : "正在停止…";
    try {
      await api("/api/openclaw/toggle", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: want }) });
      toast(want ? "微信通道已启用" : "微信通道已关闭");
    } catch (e) { toast("操作失败：" + e.message); $("#ocEnabled").checked = !want; }
    finally { $("#ocEnabled").disabled = false; setTimeout(() => showSettings("bot"), 800); }
  };

  const ocStatus = $("#ocStatus");
  const renderOc = (s) => {
    if (!document.querySelector("#ocStatus")) return;
    if (!s.found) {
      ocStatus.innerHTML = "未检测到 OpenClaw。请先按 README 安装微信渠道（需要 Node 24）。";
      return;
    }
    ocStatus.innerHTML = `网关：<b>${s.gateway ? "运行中" : "未运行"}</b> · 登录：${s.login_running ? "进行中" : "空闲"}`
      + (s.enabled ? "" : " · 通道已关闭");
    const on = s.enabled;
    $("#ocLogin").disabled = !on;
    $("#ocGateway").disabled = !on;
    $("#ocLoginStop").style.display = s.login_running ? "" : "none";
    const qrUrl = s.qr_url || "";
    const wrap = $("#ocQrWrap"), img = $("#ocQrImg");
    if (on && s.login_running && qrUrl) {
      wrap.style.display = "block";
      const token = (qrUrl.match(/qrcode=([^&]+)/) || [])[1] || qrUrl.slice(-16);
      const src = "/api/openclaw/qr.png?t=" + encodeURIComponent(token);
      if (img.getAttribute("src") !== src) img.src = src;
    } else {
      wrap.style.display = "none";
    }
    const hint = $("#ocLogHint");
    if (s.login_running) {
      hint.style.display = "";
      hint.textContent = qrUrl ? "二维码已显示，用手机微信扫码并在手机上确认。" : "正在生成二维码…";
    } else {
      const tail = (s.login_log || "").split("\n")
        .filter(l => l.trim() && !/[█▀▄]/.test(l)).slice(-1)[0] || "";
      hint.style.display = tail ? "" : "none";
      hint.textContent = tail;
    }
  };
  const refreshOc = async () => {
    try { renderOc(await api("/api/openclaw/status")); }
    catch (e) { ocStatus.textContent = "状态获取失败: " + e.message; }
  };
  $("#ocLogin").onclick = async () => {
    const b = $("#ocLogin");
    b.disabled = true; b.textContent = "启动中…";
    try { await api("/api/openclaw/login", { method: "POST" }); toast("已发起登录，二维码稍后显示"); refreshOc(); }
    catch (e) { toast(e.message); }
    finally { b.disabled = false; b.textContent = "扫码登录"; }
  };
  $("#ocLoginStop").onclick = async () => {
    try { await api("/api/openclaw/login/stop", { method: "POST" }); } catch (e) {}
    refreshOc();
  };
  $("#ocGateway").onclick = async () => {
    const b = $("#ocGateway");
    b.disabled = true; b.textContent = "重启中…";
    try {
      const r = await api("/api/openclaw/gateway", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: "{}" });
      toast(r.gateway ? "网关已运行" : "网关未响应，请看状态行");
      refreshOc();
    } catch (e) { toast(e.message); }
    b.disabled = false; b.textContent = "重启网关";
  };
  refreshOc();
  window._ocTimer = setInterval(refreshOc, 3000);
}

async function renderSettingsData(body) {
  let s, a, rt;
  try {
    [s, a, rt] = await Promise.all([
      api("/api/status"), api("/api/sync/auto"), api("/api/sync/realtime")]);
  } catch (e) { body.innerHTML = `<div class="qa-empty">加载失败: ${esc(e.message)}</div>`; return; }
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">DATA PIPELINE</span><h2>数据与同步</h2><p>控制消息入库节奏，确保知识库保持最新。</p></div><span class="settings-status-pill ${rt.realtime ? "success" : "neutral"}">${rt.realtime ? "实时同步中" : "实时同步已关闭"}</span></div>
    <div class="settings-grid"><div class="settings-stack">
      <section class="settings-card"><div class="settings-card-head"><span class="settings-card-icon">↻</span><div><h3>实时同步</h3><p>发现微信数据库变化后立即入库，轮询作为兜底。</p></div></div>
        <label class="switch-row"><input type="checkbox" id="rtSync" ${rt.realtime ? "checked" : ""}><span class="switch-track"></span><span><b>启用实时同步</b><small>需要微信已登录且密钥有效。</small></span></label>
        <label class="switch-row"><input type="checkbox" id="rtMedia" ${rt.media_on ? "checked" : ""}><span class="switch-track"></span><span><b>新图片自动 OCR</b><small>只处理最近 7 天的新图片，历史存量在索引任务里补跑。</small></span></label>
        <label class="switch-row"><input type="checkbox" id="rtAsr" ${rt.asr_on ? "checked" : ""}><span class="switch-track"></span><span><b>新语音自动转写</b><small>只处理最近 7 天的新语音/视频，历史存量在索引任务里补跑。</small></span></label>
        <div class="inline-setting"><label for="rtInterval">兜底轮询</label><div><input id="rtInterval" type="number" min="15" value="${rt.realtime_sec}"> <span>秒</span></div></div><div class="settings-note" id="rtState"></div>
      </section>
      <section class="settings-card"><div class="settings-card-head"><span class="settings-card-icon">◷</span><div><h3>定时同步</h3><p>定期扫描数据库，适合实时监听不可用时使用。</p></div></div>
        <label class="switch-row"><input type="checkbox" id="autoSync" ${a.auto ? "checked" : ""}><span class="switch-track"></span><span><b>启用定时同步</b><small>每 <input class="inline-number" id="autoInterval" type="number" min="5" value="${a.interval_min}"> 分钟执行一次。</small></span></label><div class="settings-note">${a.last ? "上次运行：" + esc(a.last) : "尚未运行"}</div>
      </section>
      <section class="settings-card settings-action-card"><div><h3>手动同步</h3><p>立即扫描一次，适合刚登录微信或需要立刻更新时使用。</p></div><button id="syncBtn" ${s.sync.running ? "disabled" : ""}>${s.sync.running ? "同步中…" : "立即同步一次"}</button><pre>${esc(s.sync.log || "")}</pre></section>
    </div><aside class="settings-side-card"><div class="settings-side-title">同步说明</div><p>实时同步优先处理新消息；历史图片、语音和链接解析可在“索引任务”中补跑。</p><div class="settings-mini-stat"><span>当前状态</span><b>${s.sync.running ? "正在同步" : "空闲"}</b></div></aside></div>`;
  const saveSync = async () => {
    const controls = [$("#rtSync"), $("#rtMedia"), $("#rtAsr"), $("#rtInterval"), $("#autoSync"), $("#autoInterval")];
    controls.forEach(x => x.disabled = true);
    try {
      await api("/api/sync/realtime", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ realtime: $("#rtSync").checked,
                               media_ocr: $("#rtMedia").checked,
                               media_asr: $("#rtAsr").checked,
                               realtime_sec: +$("#rtInterval").value || 60 }) });
      await api("/api/sync/auto", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auto: $("#autoSync").checked,
                               interval_min: +$("#autoInterval").value || 30 }) });
      toast("同步设置已保存");
      setTimeout(() => showSettings("data"), 600);
    } catch (e) { toast("同步设置保存失败：" + e.message); controls.forEach(x => x.disabled = false); }
  };
  $("#rtSync").onchange = saveSync;
  $("#rtMedia").onchange = saveSync;
  $("#rtAsr").onchange = saveSync;
  $("#rtInterval").onchange = saveSync;
  $("#autoSync").onchange = saveSync;
  $("#autoInterval").onchange = saveSync;
  $("#syncBtn").onclick = async () => {
    const b = $("#syncBtn"); b.disabled = true; b.textContent = "同步中…";
    try { await api("/api/sync", { method: "POST" }); toast("同步已启动"); setTimeout(() => showSettings("data"), 1500); }
    catch (e) { toast("同步失败：" + e.message); b.disabled = false; b.textContent = "立即同步一次"; }
  };
  const rtState = $("#rtState");
  const errs = [rt.error, rt.live_err, rt.fast_err].filter(Boolean);
  rtState.textContent = rt.last ? `最近：${rt.last} · 新增 ${rt.added} 条` +
    (rt.media_ocr || rt.media_asr ? ` · OCR ${rt.media_ocr} / 转写 ${rt.media_asr}` : "") +
    (rt.selfheal ? " · 已触发自愈重扫" : "") +
    (errs.length ? " · " + errs.join("；") : "") : "尚未运行";
  rtState.className = "job-state" + (errs.length ? " err" : "");
}

async function renderSettingsIndex(body) {
  const j = await api("/api/jobs");
  const row = (key, label, state, canStart) => `
    <div class="job-row">
      <span class="job-icon">${key === "vectors" ? "⌁" : key === "ocr" ? "▧" : key === "asr" ? "◖" : "↗"}</span><span class="job-copy"><b>${label}</b><small>${key === "vectors" ? "提升语义检索效果" : "处理历史存量内容"}</small></span>
      <span class="job-state ${state.running ? "running" : ""}">
        ${(() => {
          const total = (state.total === undefined || state.total === null) ? "…" : state.total;
          const done = (state.done === undefined || state.done === null) ? "…" : state.done;
          return state.running ? `进行中 ${state.count || 0}/${total}` : `${done}/${total}`;
        })()}</span>
      <span><button class="mini" data-job="${key}" ${state.running || !canStart ? "disabled" : ""}>开始</button>
      <button class="mini ghost" data-stop="${key}" ${state.running ? "" : "disabled"}>停止</button></span>
    </div>`;
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">INDEX & MEDIA</span><h2>索引任务</h2><p>补跑历史内容，让搜索、OCR 和语音转写更完整。</p></div><span class="settings-status-pill info">可断点续跑</span></div>
    <div class="settings-grid"><div class="settings-stack"><section class="settings-card"><div class="settings-card-head"><span class="settings-card-icon">⌁</span><div><h3>历史处理任务</h3><p>建议一次只运行一个任务，避免占用过多资源。</p></div></div><div class="jobs" id="jobsBox">
      ${row("vectors", "向量索引（语义检索）", j.vector, j.vector.model)}
      ${row("ocr", "图片 OCR（历史图片）", j.ocr, true)}
      ${row("asr", "语音转写（历史语音）", j.asr, true)}
      ${row("links", "公众号正文（历史链接）", j.links, true)}
      ${row("files", "文件正文（历史文件）", j.files, true)}
      ${row("snsocr", "朋友圈 OCR", j.snsocr, true)}
    </div></section><section class="settings-card settings-advanced"><div class="settings-card-head"><span class="settings-card-icon">↺</span><div><h3>补漏重扫</h3><p>按时间窗重新扫描，幂等执行，不会重复导入。</p></div></div><div class="rescan-actions">
        <button class="mini" data-rescan="1">最近 24 小时</button>
        <button class="mini" data-rescan="7">最近 7 天</button>
        <button class="mini" data-rescan="30">最近 30 天</button>
      </div></section></div><aside class="settings-side-card"><div class="settings-side-title">自动处理</div><p>新消息的图片、语音和朋友圈链接会在实时同步时自动处理。</p><ul><li>向量索引用于语义检索</li><li>OCR 处理图片文字</li><li>ASR 转写历史语音</li></ul></aside></div>`;
  if (["asr", "files", "snsocr"].some(k => j[k].total === undefined || j[k].total === null)
      && settingsSection === "index") {
    if (window._jobRetry) clearTimeout(window._jobRetry);
    window._jobRetry = setTimeout(() => {
      window._jobRetry = null;
      if (settingsSection === "index") showSettings("index");
    }, 2500);
  }
  body.querySelectorAll("[data-job]").forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = "启动中…";
    try { await api(`/api/jobs/${b.dataset.job}/start`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); toast("任务已启动"); setTimeout(() => showSettings("index"), 1500); }
    catch (e) { toast("启动失败：" + e.message); b.disabled = false; b.textContent = "开始"; }
  });
  body.querySelectorAll("[data-stop]").forEach(b => b.onclick = async () => {
    try { await api(`/api/jobs/${b.dataset.job}/stop`, { method: "POST" }); setTimeout(() => showSettings("index"), 800); } catch (e) { toast("停止失败：" + e.message); }
  });
  body.querySelectorAll("[data-rescan]").forEach(b => b.onclick = async () => {
    b.disabled = true;
    try { await api("/api/jobs/rescan/start", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days: +b.dataset.rescan }) });
    toast(`已开始重扫最近 ${b.dataset.rescan} 天`);
    setTimeout(() => showSettings("index"), 1200); } catch (e) { toast("重扫失败：" + e.message); b.disabled = false; }
  });
}

function renderSettingsAbout(body, status) {
  status = status || {};
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">LOCAL KNOWLEDGE BASE</span><h2>关于</h2><p>查看当前知识库规模、运行模式与本地数据说明。</p></div><span class="settings-status-pill success">运行正常</span></div>
    <section class="settings-card"><div class="settings-card-head"><span class="settings-card-icon">i</span><div><h3>知识库概览</h3><p>所有数据均保存在当前设备。</p></div></div><div class="settings-stats"><div class="settings-stat"><b>${(status.messages || 0).toLocaleString()}</b><span>条消息</span></div><div class="settings-stat"><b>${(status.chats || 0).toLocaleString()}</b><span>个会话</span></div><div class="settings-stat"><b>${(status.contacts || 0).toLocaleString()}</b><span>个联系人</span></div></div><div class="settings-info-list"><div><span>问答模式</span><b>${status.llm === "configured" ? "AI 生成 · " + esc(status.llm_model || "") : "检索模式（未配置 Key）"}</b></div><div><span>数据库密钥</span><b>fp=${esc(status.key_fp || "未读取")}</b></div><div><span>数据位置</span><b>data/knowledge.db 与 data/decrypted</b></div></div></section><div class="settings-privacy"><b>隐私说明</b><span>微信数据、API Key 与解密文件均只保存在本机。</span></div>`;
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
  const ocr = m.parsed && m.parsed.ocr_text
    ? `<div class="pv-seg"><div class="pv-seg-title">图片文字（OCR）</div>` +
      `<pre class="pv-text">${esc(m.parsed.ocr_text)}</pre></div>` : "";
  $("#pvBody").innerHTML =
    '<div class="pv-head"><span class="pv-name">点击空白处或 ← 返回聊天</span></div>' +
    '<img src="' + src + '" class="pv-img" style="max-height:60vh;cursor:zoom-out" ' +
    'onclick="document.querySelector(&#39;#backToChat&#39;).click()">' + ocr;
}

/* ---------------- 视频预览（播放器 + 语音转写文字） ---------------- */
async function openVideoViewer(m, p) {
  showChatPane();
  $("#msgList").style.display = "none";
  $("#previewPane").style.display = "flex";
  $("#previewPane").style.flexDirection = "column";
  $("#backToChat").style.display = "inline";
  $("#chatTitle").textContent = "视频播放";
  $("#chatMeta").textContent = (m.chat_name || "") + " · " + fmtTime(m.ts) +
    (p.duration_s ? ` · ${p.duration_s}秒` : "");
  updateScopeBar();
  const qs = `chat=${encodeURIComponent(m.chat)}&id=${m.id}`;
  $("#pvBody").innerHTML =
    `<video class="pv-video" controls autoplay src="/api/media/video?${qs}"></video>` +
    `<div class="pv-seg"><div class="pv-seg-title">语音转文字</div>` +
    `<pre class="pv-text" id="pvAsr">${esc(p.asr_text || "")}</pre></div>`;
  if (!p.asr_text) {
    // 转写是异步后台任务，打开时再取一次；没有就说明无可转写内容或还没轮到
    try {
      const d = await api(`/api/media/asr?${qs}`);
      const el = $("#pvAsr");
      if (el) el.textContent = d.text || "（这段视频没有语音内容，或尚未转写完成）";
    } catch {
      const el = $("#pvAsr");
      if (el) el.textContent = "（转写暂不可用）";
    }
  }
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
  if (url.startsWith("wxapp://")) { toast("小程序内容需要在微信里打开，无法抓取正文"); return; }
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
    const showTitle = (d && d.title) || title || "网页正文";
    $("#chatTitle").textContent = showTitle;
    $("#chatMeta").textContent = `${d ? d.chars : 0} 字 · 网页`;
    $("#pvBody").innerHTML =
      `<div class="pv-head"><span class="pv-name">${esc(showTitle)}</span>
        <span class="pv-actions"><a href="${esc(url)}" target="_blank" rel="noopener">在浏览器打开 ↗</a></span>
       </div><pre class="pv-text">${esc((d && d.text) || "")}</pre>`;
  }
  updateScopeBar();
}

/* ---------------- 文件预览（内嵌在中间显示区） ---------------- */
let previewFile = null;

function exitPreview() {
  $("#pvBody").innerHTML = "";      // 停掉预览里的视频/音频播放
  $("#previewPane").style.display = "none";
  $("#msgList").style.display = "block";
  $("#backToChat").style.display = "none";
  $("#drawerFab").style.display = "none";
  $("#drawer").style.display = "flex";
  previewFile = null;
  if (typeof previewLink !== "undefined") previewLink = null;
  if (typeof previewMoment !== "undefined") previewMoment = null;
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
      <span class="pv-actions">${meta.found ? `<a href="javascript:void(0)" id="revealFile">打开所在位置 ↗</a>` : ""}<a href="/api/file/raw?${qs}" target="_blank">用系统程序打开 ↗</a></span></div>`;

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
  if (meta.found && $("#revealFile")) {
    $("#revealFile").onclick = async () => {
      try {
        await api("/api/file/reveal", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat, id: lid, ts, title: name }) });
        toast("已在资源管理器中定位文件");
      } catch (e) { toast("定位失败：" + e.message); }
    };
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
  if (v !== "settings") $("#settingsFull").style.display = "none";
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
  if (v === "files") {
    $("#searchInput").placeholder = "搜索文件名（选中会话可缩小范围）";
    showFiles();
    return;
  }
  if (v === "links") {
    $("#searchInput").placeholder = "搜索链接标题 / 来源群";
    showLinks();
    return;
  }
  if (v === "moments") {
    $("#searchInput").placeholder = "搜索朋友圈文字（含图片 OCR）";
    showMoments();
    refreshSnsStatus();
    return;
  }
  $("#searchInput").placeholder = "搜索会话 / 全局搜消息（回车）";
  if (v === "settings") showSettings();
}
$("#searchInput").addEventListener("input", (e) => {
  if (state.view === "links") {
    linkView.q = e.target.value;
    clearTimeout(linkQueryTimer);
    linkQueryTimer = setTimeout(() => refreshLinks(true), 300);
    return;
  }
  if (state.view === "moments") { showMoments(e.target.value); return; }
  if (state.view === "files") {
    fileView.q = e.target.value;
    clearTimeout(fileQueryTimer);
    fileQueryTimer = setTimeout(() => refreshFiles(true), 300);
    return;
  }
  if (state.view !== "chats") return;
  if (state.searchMode) return;
  renderSessions(e.target.value);
});
$("#searchInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && state.view === "chats") globalSearch(e.target.value);
  if (e.key === "Enter" && state.view === "files") { clearTimeout(fileQueryTimer); refreshFiles(true); }
  if (e.key === "Enter" && state.view === "links") { clearTimeout(linkQueryTimer); refreshLinks(true); }
  if (e.key === "Escape") {
    e.target.value = "";
    if (state.view === "files") { fileView.q = ""; refreshFiles(true); }
    else if (state.view === "links") { linkView.q = ""; refreshLinks(true); }
    else globalSearch("");
  }
});
document.querySelectorAll(".rail-btn").forEach(b => b.onclick = () => switchView(b.dataset.view));
$("#backToChat").onclick = () => exitPreview();
$("#drawerFab").onclick = () => {
  const d = $("#drawer");
  const hidden = d.style.display === "none";
  d.style.display = hidden ? "flex" : "none";
};
$("#scopeToggle").onchange = updateDrawerScope;
$("#fastMode").onchange = () => { $("#exploreToggle").checked = false; };
$("#exploreMode").onchange = () => { $("#exploreToggle").checked = true; };
function submitQuestion(input) {
  const v = input && input.value.trim();
  if (!v) return;
  if (typeof streamState !== "undefined" && streamState.busy) { toast("上一条还在回答中，请稍候"); return; }
  input.value = "";
  ask(v);
}
$("#sendBtn").onclick = () => submitQuestion($("#input"));
// ＋新会话：在当前上下文（群/私聊/文件/链接/全局助手）新建一个 AI 会话
// 主脚本统一绑定，避免按需视图切换时出现死按钮。
$("#newSessionBtn").onclick = () => newAiSession();
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitQuestion($("#input")); }
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
      try {
        await loadSessions();
        // SSE 负责实时消息；这里仅做一次去重后的兜底同步。
        if (state.current && state.current.username && !isAssistant()) {
          await loadMessages(state.current.username, { initial: false });
        }
      } catch (e) {
        console.warn("自动刷新失败", e);
      }
    }
  }, 30000);
}

/* ---------------- 深链：/?chat=<wxid>&msg=<id> ---------------- */
async function applyDeepLink() {
  const qp = new URLSearchParams(location.search);
  const qmsg = Number(qp.get("msg") || 0);
  const qchat = qp.get("chat") || "";
  if (!qmsg && !qchat) return;
  try {
    if (qmsg) {
      await jumpToRef({ id: qmsg });
    } else {
      await openChat(qchat, qp.get("name") || qchat, qchat.includes("@chatroom"));
    }
    history.replaceState(null, "", location.pathname);
  } catch (e) {
    console.warn("深链打开失败", e);
  }
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
    await applyDeepLink();
  } catch (e) {
    $("#statusHint").textContent = "后端连接失败: " + e.message;
  }
})();


/* 助手页独立输入框 */
function bindAssistantComposer() {
  const go = () => {
    const el = document.querySelector("#assistantInput");
    $("#thinkToggle") && ($("#thinkToggle").checked = document.querySelector("#assistantThink").checked);
    $("#exploreToggle") && ($("#exploreToggle").checked = document.querySelector("#assistantExploreMode").checked);
    submitQuestion(el);
  };
  document.querySelector("#assistantSend").onclick = go;
  const input = document.querySelector("#assistantInput");
  if (!input.dataset.bound) {
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); }
    });
    input.dataset.bound = "1";
  }
  $("#assistantFastMode").onchange = () => { $("#exploreToggle").checked = false; };
  $("#assistantExploreMode").onchange = () => { $("#exploreToggle").checked = true; };
  document.querySelector("#assistantNew").onclick = () => newAiSession();
  const scopeSel = document.querySelector("#assistantScopeSelect");
  if (scopeSel && !scopeSel.dataset.bound) {
    scopeSel.addEventListener("change", () => selectAssistantScope(scopeSel.value));
    scopeSel.dataset.bound = "1";
  }
  const scopeToggle = document.querySelector("#assistantScopeToggle");
  if (scopeToggle && !scopeToggle.dataset.bound) {
    scopeToggle.addEventListener("change", updateAssistantScopeUI);
    scopeToggle.dataset.bound = "1";
  }
  input.focus();
}
