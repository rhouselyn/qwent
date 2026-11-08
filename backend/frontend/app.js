/* 知识助手 - 前端逻辑（微信 / QQ 双界面共用） */
const $ = (s) => document.querySelector(s);
/* 平台层由 platform.js 提供：左上角 logo 切换、文案、系统消息、术语本地化 */
const isQQ = () => !!(window.Platform && window.Platform.isQQ());
const T = (k) => (window.Platform ? window.Platform.t(k) : k);
const Q = (s) => (window.Platform ? window.Platform.qtext(s) : String(s || ""));
const state = {
  view: "chats", sessions: [], current: null,   // current: {username,name,is_group}
  msgs: {}, hasMore: {}, drawerOpen: false, qaHistory: [],
  globalMode: false, searchMode: false,
  quoted: [],            // 引用的消息 [{id, ts, who, text, chat}]
  pinBottom: true,       // 是否贴在最新消息处（图片加载后需要补滚）
  qqConv: null,          // 当前打开的 QQ 会话
  qqConvName: "",        // 当前 QQ 会话显示名
  qqConvKind: "",        // local=NT 本地记录 / bot=机器人会话
  selfQqUin: 0,          // 自己的 QQ 号（取头像/昵称用）
  selfQqName: "",
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
  const platform = (window.Platform && Platform.get()) || "wechat";
  try { aiState.tree = await api(`/api/ai/sessions?platform=${platform}`); } catch { aiState.tree = []; }
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
    body: JSON.stringify({ scope, name: sc ? sc.name : "",
                           platform: (window.Platform && Platform.get()) || "wechat" }),
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
/* 中置时间分隔：同一会话内，与上一条间隔超过 5 分钟就插入居中时间标签。
   微信 / QQ 共用（QQ 之前把时间塞在气泡上方导致左右错位）。 */
const TIME_GAP_SEC = 300;
function fmtTimeLabel(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date();
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (d.toDateString() === now.toDateString()) return hm;
  const y = new Date(now.getTime() - 86400000);
  if (d.toDateString() === y.toDateString()) return `昨天 ${hm}`;
  const md = `${d.getMonth() + 1}月${d.getDate()}日`;
  return (d.getFullYear() === now.getFullYear() ? md : `${d.getFullYear()}年${md}`) + ` ${hm}`;
}
function makeTimeDivider(ts) {
  const d = document.createElement("div");
  d.className = "time-divider";
  d.dataset.ts = String(ts || 0);
  d.textContent = fmtTimeLabel(ts);
  return d;
}
/* 判断某条消息前是否要插入时间分隔（需要知道"上一条"的时间） */
function needsDivider(prevTs, ts) {
  if (!ts) return false;
  if (!prevTs) return true;
  return Math.abs(ts - prevTs) >= TIME_GAP_SEC;
}
/* 渲染一组消息：自动插入中置时间分隔 */
function renderMessagesWithTime(box, list, prevTs) {
  let last = prevTs || 0;
  for (const m of list) {
    if (needsDivider(last, m.ts)) box.appendChild(makeTimeDivider(m.ts));
    box.appendChild(renderMsg(m));
    last = m.ts || last;
  }
  return last;
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

const PANES = ["#sessionList", "#qqList", "#resultList", "#contactList", "#fileList",
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
     <div class="row2">${Q("全局问答：所有聊天记录 + 随时追问")}</div></div>`,
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
       <div class="info"><div class="row1"><span class="name">${esc(s.name)}</span>
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
    if (s.pinned) item.classList.add("pinned");   // 背景加深表示置顶（不再用针形图标）
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
  $(".composer #input").placeholder = "输入想了解的问题…";
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
    hint.textContent = Q(`还没有和「${name}」的聊天记录 · 可在右侧向 AI 提问`);
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
    const idx = ordered.findIndex(x => x.id === m.id);
    const prevTs = idx > 0 ? (ordered[idx - 1].ts || 0) : 0;
    if (needsDivider(prevTs, m.ts)) box.insertBefore(makeTimeDivider(m.ts), ref);
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
    const holder = document.createElement("div");
    renderMessagesWithTime(holder, data.messages, 0);
    frag.appendChild(holder);
    box.prepend(frag);
    state.msgs[username] = [...data.messages, ...state.msgs[username]];
    // 早段最后一条与之后第一条之间的分隔（避免拼接处时间断层）
    const after = box.querySelector(`.msg[data-id="${data.messages[0] && data.messages[0].id}"]`);
    if (after && state.msgs[username].length > data.messages.length) {
      const prevEl = after.previousElementSibling;
      if (prevEl && prevEl.classList.contains("msg")) {
        const prevTs = state.msgs[username][data.messages.length - 1].ts;
        if (needsDivider(prevTs, state.msgs[username][data.messages.length].ts)
            && !(prevEl.nextElementSibling && prevEl.nextElementSibling.classList.contains("time-divider"))) {
          box.insertBefore(makeTimeDivider(state.msgs[username][data.messages.length].ts), after);
        }
      }
    }
    // 加载更早消息时保持当前视口位置
    if (sc) sc.scrollTop = sc.scrollHeight - oldHeight;
  }
  else {
    if (initial) {
      renderMessagesWithTime(box, data.messages, 0);
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
/* 真实头像：微信 head_image.db 里有就显示，没有/加载失败回落字母块。
   后端对无头像账号返回 404，缓存这些账号避免列表每 30s 刷新时反复打后端
   （本机 800+ 会话时会产生成片的 404 请求）。 */
const noAvatar = new Set();
window.__markNoAvatar = (u) => { if (u) noAvatar.add(u); };
const AVATAR_ONERROR =
  "this.style.display='none';this.parentNode.classList.add('ava-letter');window.__markNoAvatar(this.dataset.u)";

function avatarImgHtml(username, name, extraStyle) {
  const letter = esc(initial(name || username || "?"));
  const bg = `background:${hue(username || name || "")};`;
  const style = extraStyle || bg;
  if (!username || noAvatar.has(username)) return `<div class="ava" style="${style}">${letter}</div>`;
  return `<div class="ava" style="${style}">
      <img class="ava-img" src="/api/avatar?u=${encodeURIComponent(username)}" alt=""
           loading="lazy" data-u="${attrEsc(username)}" onerror="${AVATAR_ONERROR}">
      <span class="ava-fallback">${letter}</span></div>`;
}

/* QQ 真实头像：好友用 QQ 号、群用群号请求 /api/qq/avatar，失败回退首字母 */
const noQqAvatar = new Set();
function qqAvatarHtml(kind, key, name, extra) {
  key = key == null ? "" : String(key);      // uin 是数字，hue() 只接受字符串
  const letter = esc(initial(name || "Q"));
  const bg = `background:${hue(key || String(name || ""))};`;
  if (!key || noQqAvatar.has(kind + ":" + key)) {
    return `<div class="ava qq-ava" style="${extra || bg}"><span class="ava-fallback">${letter}</span></div>`;
  }
  return `<div class="ava qq-ava" style="${extra || bg}">
      <img class="ava-img" src="/api/qq/avatar?kind=${kind}&key=${encodeURIComponent(key)}" alt=""
           loading="lazy" onerror="this.style.display='none';this.parentNode.classList.add('ava-letter');window.__qqNoAvatar('${kind}:${encodeURIComponent(key)}')">
      <span class="ava-fallback">${letter}</span></div>`;
}
window.__qqNoAvatar = (k) => { if (k) noQqAvatar.add(k); };

function avatarFor(m) {
  const d = document.createElement("div");
  d.className = "ava";
  const username = m.is_sender ? (state.selfUsername || "") : (m.sender || "");
  const name = m.is_sender ? "我" : m.sender_name;
  d.innerHTML = (username && !noAvatar.has(username)
    ? `<img class="ava-img" src="/api/avatar?u=${encodeURIComponent(username)}" alt=""
            loading="lazy" data-u="${attrEsc(username)}" onerror="${AVATAR_ONERROR}">
       <span class="ava-fallback">${esc(initial(name))}</span>`
    : `<span class="ava-fallback">${esc(initial(name))}</span>`);
  if (!m.is_sender) d.style.background = hue(m.sender || m.sender_name);
  return d;
}
function renderMsg(m) {
  const p = m.parsed || {};
  // 系统提示（改群名/邀请入群/撤回…）按平台显示真实微信或 QQ 的措辞
  if (p.system !== undefined || p.system_qq !== undefined) {
    const d = document.createElement("div");
    d.className = "sys";
    d.textContent = window.Platform ? window.Platform.sysText(m) : (p.system || "");
    d.dataset.id = m.id;
    return d;
  }
  const wrap = document.createElement("div");
  wrap.className = "msg " + (m.is_sender ? "right" : "left");
  wrap.dataset.id = m.id;
  wrap.appendChild(avatarFor(m));
  // 右键消息 = 加入底部引用栏（替代旧的悬浮"引用"按钮，减少遮挡）
  wrap.title = T("quoteHint");
  wrap.oncontextmenu = (e) => {
    e.preventDefault();
    addQuote(m);
    toast(T("quoteAdded"));
  };
  const col = document.createElement("div"); col.className = "col";
  if (state.current && state.current.is_group && !m.is_sender) {
    const who = document.createElement("div"); who.className = "who"; who.textContent = m.sender_name;
    col.appendChild(who);
  }
  if (m.recalled) {
    const tag = document.createElement("div"); tag.className = "recall-tag";
    tag.textContent = T("recalled");
    col.appendChild(tag);
  }
  const b = document.createElement("div"); b.className = "bubble";
  if (p.kind === "quote") {
    renderQuoteMessage(col, b, p);
  } else {
    b.appendChild(renderBubbleContent(m, p));
    col.appendChild(b);
  }
  wrap.appendChild(col);
  return wrap;
}

/* 引用他人消息：微信＝气泡内灰块「昵称：原文」；QQ＝气泡下方灰色引用条（左竖线）。
   点击引用区可展开完整原文；被引用内容来自后端 refermsg 解析（含媒体标签）。 */
function renderQuoteMessage(col, bubble, p) {
  bubble.classList.add("quote-bubble");
  const main = document.createElement("div");
  main.className = "quote-main";
  main.textContent = p.title_full || p.title || "";
  bubble.appendChild(main);

  const ref = document.createElement("div");
  ref.className = isQQ() ? "qq-reply" : "quote-ref";
  ref.title = "点击展开 / 收起引用原文";
  const who = document.createElement("span");
  who.className = "quote-ref-who";
  const name = p.refer && p.refer.who ? p.refer.who : "引用";
  who.textContent = isQQ() ? `${name}: ` : `${name}：`;
  const txt = document.createElement("span");
  txt.className = "quote-ref-text";
  txt.textContent = (p.refer && (p.refer.text || p.refer.title)) || "[消息]";
  ref.appendChild(who);
  ref.appendChild(txt);
  ref.onclick = (e) => { e.stopPropagation(); ref.classList.toggle("expanded"); };
  ref.setAttribute("data-nol10n", "");   // 引用原文是聊天内容，不能做术语替换

  if (isQQ()) {
    col.appendChild(bubble);
    col.appendChild(ref);
  } else {
    bubble.appendChild(ref);
    col.appendChild(bubble);
  }
}
function renderBubbleContent(m, p) {
  const el = document.createElement("div");
  if (p.text !== undefined) {
    // 纯文本里的分享链接 / 裸链接：和 QQ 一样可点（站内预览）
    const card = typeof parseTextCard === "function" ? parseTextCard(p.text) : null;
    if (card) {
      el.className = "card";
      el.innerHTML = `<span class="t">[链接] ${esc(card.title)}</span>`
        + (card.tag ? `<div class="d">${esc(card.tag)}</div>` : "")
        + (card.url ? `<div class="u">${esc(card.url)}</div>` : "")
        + `<div class="go">点击阅读正文 ↗</div>`;
      el.style.cursor = "pointer";
      el.onclick = () => openLinkPreview(card.url, card.title);
      return el;
    }
    if (/https?:\/\//.test(p.text) && typeof linkifyMsgText === "function") {
      el.innerHTML = linkifyMsgText(p.text);
      if (typeof bindMsgLinks === "function") bindMsgLinks(el);
      return el;
    }
    el.textContent = p.text;
    return el;
  }
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
      el.innerHTML = `<span class="t">[链接] ${esc(p.title || "")}</span>` +
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
  // QQ 本地会话用 state.qqConv 表示当前上下文（state.current 为空）
  const qqChat = isQQ() && state.qqConvKind === "local" && !!state.qqConv;
  if (isAssistant() || (!state.current && !qqChat)) { bar.style.display = "none"; return; }
  bar.style.display = "flex";
  $("#scopeLabel").textContent = "仅当前会话内提问";
}
function updateDrawerScope() {
  const scoped = $("#scopeToggle").checked;
  const pfLabel = () => (typeof assistantPlatformLabel === "function"
    ? assistantPlatformLabel(assistantPlatformValue()) : "全局");
  $("#drawerScope").textContent =
    previewFile ? (scoped ? "范围：此文件" : "范围：" + pfLabel())
    : previewLink ? (scoped ? "范围：本文" : "范围：" + pfLabel())
    : (isAssistant() ? "范围：" + pfLabel()
       : (scoped ? "范围：本会话" : "范围：" + pfLabel()));
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
          ? `「${scopeNameOf(aiState.currentScope)}」` : T("aiGlobal"))
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
      <div class="q">${esc(turn.question)}</div>
      <div class="qa-head">
        <span class="mode">${
          turn.mode === "agent" || turn.mode === "llm" || turn.mode === "stream" ? "AI 回答"
          : turn.mode === "fast" ? "快速回答"
          : turn.mode === "link" ? "本文问答"
          : turn.mode === "file" ? "文件问答" : "检索结果"}</span>
        <span class="qa-time">${turn.ts ? fmtTime(turn.ts) : ""}</span>
      </div>
      <div class="a md">${ansHtml}</div>`;
    // 正文里的 [n] 是可点击引用；文件类来源以 chips 附在答案后；下方保留可展开的引用来源合集
    const ansEl = d.querySelector(".a");
    if (typeof bindRefLinks === "function") bindRefLinks(ansEl, turn.refs);
    if (typeof attachSourceFiles === "function") attachSourceFiles(d, turn.refs);
    if (typeof insertRefsWrap === "function") insertRefsWrap(d, turn.refs);
    // 思考过程放在回答上方，回答下面只留引用
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
      if (ansEl && ansEl.parentNode) ansEl.parentNode.insertBefore(tw, ansEl);
      else d.appendChild(tw);
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
  if (scope.startsWith("moment:")) return T("momentsScope");
  if (scope.startsWith("file:")) return "文件问答";
  if (scope.startsWith("link:")) return "链接问答";
  const s = state.sessions.find(x => x.username === scope);
  return s ? s.name : scope;
}
function scopeToggleText(scope) {
  if (scope.startsWith("file:")) return "仅本文件";
  if (scope.startsWith("link:")) return "仅本文";
  if (scope.startsWith("moment:")) return T("momentOnly");
  return "仅本会话";
}
function assistantScopeLabel(g) {
  if (g.kind === "file") return "文件：" + g.name;
  if (g.kind === "link") return "链接：" + g.name;
  if (g.kind === "group") return "群聊：" + g.name;
  if (g.kind === "chat") return "会话：" + g.name;
  return g.name || "全局助手";
}
/* 全局助手的来源范围：默认只查当前平台，想跨平台要显式选「微信 + QQ」 */
const ASSIST_PLATFORM_KEY = "wk.assistant.platform.v1";
function assistantPlatformScope() {
  try { return localStorage.getItem(ASSIST_PLATFORM_KEY) || "auto"; } catch { return "auto"; }
}
function assistantPlatformValue() {
  const v = assistantPlatformScope();
  if (v === "wechat" || v === "qq" || v === "both") return v;
  return isQQ() ? "qq" : "wechat";   // auto：跟随当前界面
}
function assistantPlatformLabel(v) {
  return v === "wechat" ? "仅微信记录" : v === "qq" ? "仅 QQ 记录" : "微信 + QQ 记录";
}
function syncAssistantScopeSelect() {
  const sel = document.querySelector("#assistantScopeSelect");
  if (!sel) return;
  const scope = aiState.currentScope || "assistant";
  const plat = assistantPlatformValue();
  // 只有三项全局范围，默认跟随当前平台；具体群聊/文件/链接从右侧「AI 会话」
  // 树里选，选中的那个再动态补进下拉，避免把几百个群都塞进来。
  sel.innerHTML = `<option value="assistant:wechat">全部微信记录</option>` +
    `<option value="assistant:qq">全部 QQ 记录</option>` +
    `<option value="assistant:both">微信 + QQ 记录</option>`;
  if (scope === "assistant") {
    sel.value = "assistant:" + plat;
    return;
  }
  if ([...sel.options].some(o => o.value === scope)) {
    sel.value = scope;
  } else {
    const opt = document.createElement("option");
    opt.value = scope;
    const g = scopeGroup(scope);
    // 当前打开的 QQ 会话可能还没进 AI 会话树，用列表里的显示名，别露出 group:xxx
    const qqName = (scope === state.qqConv && state.qqConvName) ? "会话：" + state.qqConvName : "";
    opt.textContent = g ? assistantScopeLabel(g) : (qqName || scopeNameOf(scope));
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
  // 下拉里的 assistant:wechat / assistant:qq / assistant:both = 全局助手的来源范围
  if (typeof scope === "string" && scope.startsWith("assistant:")) {
    const pf = scope.split(":")[1];
    try { localStorage.setItem(ASSIST_PLATFORM_KEY, pf); } catch {}
    aiState.currentScope = "assistant";
    syncAssistantScopeSelect();
    updateAssistantScopeUI();
    updateDrawerScope();
    toast("提问范围：" + assistantPlatformLabel(pf));
    return;
  }
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
    return { key: `moment:${previewMoment}`, name: T("momentsScope"), kind: "moment" };
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
  if (isQQ() && state.qqConvKind === "local" && state.qqConv) {
    return { key: state.qqConv, name: state.qqConvName || state.qqConv, kind: "chat" };
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
  // QQ 本地会话：勾选=只问这个群/好友（chat=conv），取消=全局
  if (isQQ() && state.qqConvKind === "local" && state.qqConv) {
    return $("#scopeToggle").checked ? state.qqConv : null;
  }
  return $("#scopeToggle").checked && state.current ? state.current.username : null;
}

async function ask(question) {
  if (!question || !question.trim()) return;
  if (typeof askStream === "function") return askStream(question.trim());
  toast("流式模块未加载");
}

/* ---------------- 引用跳转 ---------------- */
async function jumpToRef(r) {
  // QQ 引用：直接用 QQ 本地会话打开并定位（不能走微信 /api/messages/context）
  if (isQQ() && r && r.chat && !r.kind) {
    return openQqLocalConversation(r.chat, r.chat_name || r.chat,
      String(r.chat).startsWith("group:"), r.id);
  }
  const wasAssistant = isAssistant() ||
    document.querySelector("#assistantPane").style.display !== "none";
  // 从文件/文章预览里跳走时，结束"仅此文件/本文"上下文，回到会话提问
  previewFile = null;
  if (typeof previewLink !== "undefined") previewLink = null;
  if (typeof previewMoment !== "undefined") previewMoment = null;
  $(".composer #input").placeholder = "输入想了解的问题…";
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
  if (!q.trim()) {
    state.searchMode = false;
    if (isQQ() && state.view === "chats") {
      showPane("#qqList");
      loadQqConversations();
      renderQqViewTabs();
    } else {
      showPane("#sessionList");
      renderSessions();
    }
    return;
  }
  if (isQQ() && state.view === "chats") {
    return globalSearchQq(q);
  }
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

/* QQ 模式「QQ 消息」 tab 的搜索：只搜机器人收到的消息 */
async function globalSearchQq(q) {
  state.searchMode = true;
  const box = $("#resultList");
  box.innerHTML = `<div class="qa-empty">搜索中…</div>`;
  showPane("#resultList");
  let res;
  try {
    const d = await api(`/api/qq/search?q=${encodeURIComponent(q)}&n=50`);
    res = d.items || [];
  } catch (e) {
    box.innerHTML = `<div class="qa-empty">搜索失败：${esc(e.message)}</div>`;
    return;
  }
  box.innerHTML = "";
  if (!res.length) { box.innerHTML = `<div class="qa-empty">无结果</div>`; return; }
  box.appendChild(Object.assign(document.createElement("div"), {
    className: "qa-empty", textContent: `QQ 消息命中 ${res.length} 条（回车切换回会话列表）` }));
  for (const r of res) {
    const d = document.createElement("div");
    d.className = "result-item";
    const tag = r.source === "local" ? "QQ记录" : "机器人";
    d.innerHTML = `<div class="meta">${esc(r.conv_name)} · ${fmtTime(r.ts)} · ${esc(r.who || (r.is_self ? "助手" : ""))} · ${tag}</div>
      <div class="snippet">${esc(maskSecrets(r.snippet))}</div>`;
    d.onclick = () => {
      if (r.source === "local") {
        openQqLocalConversation(r.conv, r.conv_name,
          String(r.conv || "").startsWith("group:"), r.id);
      } else {
        openQqConversation(r.conv, r.conv_name, r.scene, r.id);
      }
    };
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
  // 进 AI 助手等于离开 QQ 会话：清掉会话上下文，左侧高亮也一起复原
  state.qqConv = null;
  state.qqConvKind = null;
  qqLiveStop();
  syncQqConvActive();
  document.querySelectorAll("#sessionList .session-item.active")
    .forEach((el) => el.classList.remove("active"));
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
  if (isQQ()) return showQqContacts(q);
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
async function showQqContacts(q = "") {
  const box = $("#contactList");
  box.innerHTML = `<div class="qa-empty">加载中…</div>`;
  let data;
  try {
    data = await api("/api/qq/local/contacts?limit=2000&q=" + encodeURIComponent(q));
  } catch (e) {
    box.innerHTML = `<div class="qa-empty">QQ 联系人读取失败：${esc(e.message)}</div>`;
    showPane("#contactList");
    return;
  }
  const items = data.contacts || [];
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = `<div class="qa-empty">没有找到 QQ 联系人。可到「设置 → QQ 本地记录」导入。</div>`;
    showPane("#contactList");
    return;
  }
  const groups = items.filter(x => x.kind === "group");
  const friends = items.filter(x => x.kind !== "group");
  const addHeader = (title) => {
    const h = document.createElement("div");
    h.className = "contact-group";
    h.textContent = title;
    box.appendChild(h);
  };
  const addRow = (c) => {
    const d = document.createElement("div");
    d.className = "session-item";
    const name = c.name || c.nickname || String(c.uin || c.uid);
    d.innerHTML = `${qqAvatarHtml(c.kind === "group" ? "group" : "friend",
      c.kind === "group" ? (c.uin || "") : (c.uin || c.uid || ""), name)}
      <div class="info"><div class="row1"><span class="name">${esc(name)}</span>
      ${c.kind === "group" ? '<span class="tag">群</span>' : ""}</div>
      <div class="row2">${esc(String(c.uin || c.uid || ""))}${c.cnt ? " · " + c.cnt + " 条" : ""}</div></div>`;
    d.onclick = () => {
      if (c.conv) openQqLocalConversation(c.conv, name, c.kind === "group");
      else toast("这个联系人还没有 QQ 会话");
    };
    box.appendChild(d);
  };
  // 好友按拼音首字母分组（A→Z，# 收尾），与微信通讯录一致；群聊单独放最后
  let lastInitial = "";
  for (const c of friends) {
    const init = c.initial || "#";
    if (init !== lastInitial) {
      lastInitial = init;
      addHeader(init);
    }
    addRow(c);
  }
  if (groups.length) addHeader(`群聊（${groups.length}）`);
  for (const c of groups) addRow(c);
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
        <select id="fileState">${opts([["", "全部状态"], ["parsed", "已解析"],
          ["missing", "未下载/失败"], ["unprocessed", "未处理"]], fileView.state)}</select>
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
    d.innerHTML = `<div class="meta"><span class="meta-trunc">${esc(f.chat_name)} · ${fmtTime(f.ts)} · ${fmtSize(f.size)}</span>
        <span class="${stateCls}">${state}</span></div>
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

/* QQ 模式的文件 / 链接：来自 QQ NT 本地库，功能与微信文件/链接中心对齐 */
const qqFileView = { q: "", chat: "", kind: "", state: "", days: 0, sort: "new",
                     offset: 0, items: [], hasMore: true, loading: false, stats: null };
const qqLinkView = { q: "", chat: "", platform: "", state: "", days: 0, sort: "new",
                     offset: 0, items: [], hasMore: true, loading: false, stats: null };
let qqFileSessions = null;
const QQ_FILE_KIND_LABELS = { doc: "文档", sheet: "表格", slide: "演示", image: "图片",
                              pdf: "PDF", archive: "压缩包", audio: "音频",
                              video: "音视频", other: "其他", file: "文件" };

function renderQqFileShell() {
  const box = $("#fileList");
  const opts = (rows, cur) => rows.map(([v, label]) =>
    `<option value="${attrEsc(String(v))}" ${String(v) === String(cur) ? "selected" : ""}>${esc(label)}</option>`).join("");
  const chats = [["", "全部会话"], ...(qqFileSessions || []).slice(0, 150).map(s => [s.conv, s.name || s.conv])];
  const kindRows = Object.entries(QQ_FILE_KIND_LABELS).map(([k, label]) => {
    const st = qqFileView.stats && qqFileView.stats.kinds && qqFileView.stats.kinds[k];
    return [k, st ? `${label} ${st.parsed}/${st.total}` : label];
  });
  box.innerHTML = `
    <div class="files-toolbar">
      <div class="files-filters">
        <select id="qqFileChat">${opts(chats, qqFileView.chat)}</select>
        <select id="qqFileState">${opts([["", "全部状态"], ["parsed", "已解析"],
          ["unparsed", "待解析"], ["missing", "未下载"]], qqFileView.state)}</select>
        <select id="qqFileKind">${opts([["", "全部类型"], ...kindRows], qqFileView.kind)}</select>
        <select id="qqFileDays">${opts([[0, "不限时间"], [7, "最近 7 天"], [30, "最近 30 天"], [365, "最近一年"]], qqFileView.days)}</select>
        <select id="qqFileSort">${opts([["new", "最新在前"], ["old", "最早在前"], ["size", "文件最大"]], qqFileView.sort)}</select>
      </div>
      <div class="files-meta" id="qqFileMeta">加载中…</div>
    </div>
    <div id="qqFileItems"></div>
    <button id="qqFileMore" class="mini ghost files-more" style="display:none">加载更多</button>`;
  const onChange = () => {
    qqFileView.chat = $("#qqFileChat").value;
    qqFileView.state = $("#qqFileState").value;
    qqFileView.kind = $("#qqFileKind").value;
    qqFileView.days = +$("#qqFileDays").value || 0;
    qqFileView.sort = $("#qqFileSort").value;
    refreshQqFiles(true);
  };
  ["#qqFileChat", "#qqFileState", "#qqFileKind", "#qqFileDays", "#qqFileSort"].forEach(sel => $(sel).onchange = onChange);
  $("#qqFileMore").onclick = () => refreshQqFiles(false);
}

async function showQqFiles(opts = {}) {
  if (typeof opts === "string") opts = { q: opts };
  Object.assign(qqFileView, opts);
  if (!qqFileSessions) {
    try { qqFileSessions = (await api("/api/qq/local/sessions?limit=300")).sessions || []; }
    catch { qqFileSessions = []; }
  }
  renderQqFileShell();
  await refreshQqFiles(true);
  showPane("#fileList");
}

async function refreshQqFiles(reset = true) {
  if (qqFileView.loading) return;
  if (reset) { qqFileView.offset = 0; qqFileView.items = []; }
  qqFileView.loading = true;
  const meta = $("#qqFileMeta");
  if (meta) meta.textContent = "加载中…";
  const qs = new URLSearchParams({ limit: "100", offset: String(qqFileView.offset),
                                   sort: qqFileView.sort });
  if (qqFileView.chat) qs.set("conv", qqFileView.chat);
  if (qqFileView.kind) qs.set("kind", qqFileView.kind);
  if (qqFileView.state) qs.set("state", qqFileView.state);
  if (qqFileView.days) qs.set("days", String(qqFileView.days));
  if (qqFileView.q) qs.set("q", qqFileView.q);
  try {
    const d = await api("/api/qq/local/files?" + qs.toString());
    qqFileView.items.push(...(d.files || []));
    qqFileView.offset += (d.files || []).length;
    qqFileView.hasMore = !!d.hasMore;
    qqFileView.stats = d.stats || null;
    renderQqFileItems();
  } catch (e) {
    if (meta) meta.textContent = "加载失败：" + e.message;
  } finally {
    qqFileView.loading = false;
  }
}

function renderQqFileItems() {
  const box = $("#qqFileItems");
  if (!box) return;
  box.innerHTML = "";
  if (!qqFileView.items.length) {
    box.innerHTML = `<div class="qa-empty">没有符合条件的 QQ 文件。<br>
      换个会话 / 类型 / 时间试试；QQ 文件在导入后出现在这里。</div>`;
  }
  for (const f of qqFileView.items) {
    const d = document.createElement("div");
    d.className = "result-item";
    const badge = (f.kind || "file").toUpperCase();
    const state = f.parsed ? "已解析" : (f.exists ? "待解析" : "未下载");
    const stateCls = f.parsed ? "file-state ok" : "file-state warn";
    d.innerHTML = `<div class="meta"><span class="meta-trunc">${esc(f.conv_name || f.conv)} · ${fmtTime(f.ts)} · ${fmtSize(f.size)}</span>
        <span class="file-badge">${badge}</span>
        <span class="${stateCls}">${state}</span></div>
      <div class="snippet">${esc(f.name)}</div>`;
    d.onclick = () => openQqFilePreview(f);
    box.appendChild(d);
  }
  const meta = $("#qqFileMeta");
  if (meta) {
    let statsText = "";
    const st = qqFileView.stats;
    if (st) {
      if (qqFileView.kind && st.kinds && st.kinds[qqFileView.kind]) {
        const k = st.kinds[qqFileView.kind];
        statsText = `${QQ_FILE_KIND_LABELS[qqFileView.kind] || "当前类型"} 已解析 ${k.parsed}/${k.total} · `;
      }
      statsText += `全部 已解析 ${st.parsed}/${st.total}（本机 ${st.local || 0} · 未下载 ${st.missing || 0}）`;
    }
    meta.textContent = `${qqFileView.items.length} 个 QQ 文件${statsText ? " · " + statsText : ""}${qqFileView.hasMore ? " · 还有更多" : ""}`;
  }
  const more = $("#qqFileMore");
  if (more) more.style.display = qqFileView.hasMore ? "" : "none";
}

/* QQ 文件预览：与微信文件预览同一套版式；正文走懒解析，AI 提问按文件独立会话 */
async function openQqFilePreview(f) {
  previewFile = { chat: "qqfile", id: f.id, title: f.name, ts: f.ts,
                  size: f.size, chat_name: f.conv_name || f.conv, conv: f.conv };
  if (!state.current || state.current.username !== f.conv) {
    state.current = { username: f.conv, name: f.conv_name || f.conv,
                      is_group: String(f.conv || "").startsWith("group:") };
  }
  showChatPane();
  $("#assistantPane").style.display = "none";
  $("#aiSessionsPanel").style.display = "none";
  $("#settingsFull").style.display = "none";
  document.querySelector(".chat-column").style.display = "flex";
  $("#msgList").style.display = "none";
  $("#previewPane").style.display = "flex";
  $("#previewPane").style.flexDirection = "column";
  $("#backToChat").style.display = "inline";
  $("#chatTitle").textContent = f.name;
  $("#chatMeta").textContent = `${f.conv_name || f.conv} · ${fmtTime(f.ts)} · ${fmtSize(f.size)}`;
  $("#scopeToggle").checked = true;
  updateScopeBar();
  updateDrawerScope();
  $("#drawer").style.display = "flex";
  $("#drawerFab").style.display = "none";
  $("#pvBody").innerHTML = `<div class="qa-empty">加载中…</div>`;
  await enterAiScope(`file:qqfile:${f.id}`, f.name);
  updateDrawerScope();

  const raw = "/api/qq/local/media?path=" + encodeURIComponent(f.path || "");
  const head = `<div class="pv-head"><span class="pv-name">${esc(f.name)}</span>
      <span class="pv-actions">${f.exists ? `<a href="javascript:void(0)" id="qqRevealFile">打开所在位置 ↗</a>` : ""}
      ${f.exists ? `<a href="${raw}" target="_blank">用系统程序打开 ↗</a>` : ""}</span></div>`;
  if (!f.exists || !f.path) {
    $("#pvBody").innerHTML = head +
      `<div class="qa-empty">文件不在本机（可能已被 QQ 清理或未下载）。<br>
        仍可在右侧提问，AI 会结合该会话的聊天记录回答。</div>`;
    return;
  }
  const ext = (f.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
  if (ext === ".pdf") {
    $("#pvBody").innerHTML = head +
      `<iframe src="${raw}#navpanes=0&view=FitH&toolbar=1" class="pv-pdf"></iframe>`;
  } else if ([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"].includes(ext)) {
    $("#pvBody").innerHTML = head + `<img src="${raw}" class="pv-img">`;
  } else if ([".mp4", ".mov", ".avi", ".mkv", ".webm"].includes(ext)) {
    $("#pvBody").innerHTML = head +
      `<video src="${raw}" controls style="max-width:100%;display:block;margin:16px auto"></video>`;
  } else if ([".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".amr"].includes(ext)) {
    $("#pvBody").innerHTML = head +
      `<audio src="${raw}" controls style="display:block;margin:24px auto"></audio>`;
  } else {
    // 文本 / Office / 代码：懒解析正文（带缓存）
    try {
      const t = await api("/api/qq/local/file/parse", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: f.id }) });
      $("#pvBody").innerHTML = head + (t.text
        ? `<pre class="pv-text">${esc(t.text)}</pre>`
        : `<div class="qa-empty">${esc(t.error || "该格式暂不支持内容预览，仍可在右侧提问（AI 会读取文件正文）。")}</div>`);
    } catch (e) {
      $("#pvBody").innerHTML = head + `<div class="qa-empty">预览失败：${esc(e.message)}</div>`;
    }
  }
  if ($("#qqRevealFile")) {
    $("#qqRevealFile").onclick = async () => {
      try {
        await api("/api/qq/local/file/reveal", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: f.id }) });
        toast("已在资源管理器中定位文件");
      } catch (e) { toast("定位失败：" + e.message); }
    };
  }
}

/* ---------------- QQ 链接中心 ---------------- */
function renderQqLinkShell() {
  const box = $("#linkList");
  const opts = (rows, cur) => rows.map(([v, label]) =>
    `<option value="${attrEsc(String(v))}" ${String(v) === String(cur) ? "selected" : ""}>${esc(label)}</option>`).join("");
  const chats = [["", "全部会话"], ...(qqFileSessions || []).slice(0, 150).map(s => [s.conv, s.name || s.conv])];
  box.innerHTML = `
    <div class="files-toolbar">
      <div class="files-filters">
        <select id="qqLinkChat">${opts(chats, qqLinkView.chat)}</select>
        <select id="qqLinkPlatform">${opts([["", "全部平台"], ["xhs", "小红书"],
          ["bili", "B站"], ["article", "文章"], ["generic", "网页"]], qqLinkView.platform)}</select>
        <select id="qqLinkState">${opts([["", "全部状态"], ["parsed", "已解析"],
          ["pending", "待解析"], ["failed", "解析失败"]], qqLinkView.state)}</select>
        <select id="qqLinkDays">${opts([[0, "不限时间"], [7, "最近 7 天"],
          [30, "最近 30 天"], [365, "最近一年"]], qqLinkView.days)}</select>
        <select id="qqLinkSort">${opts([["new", "最新在前"], ["old", "最早在前"]], qqLinkView.sort)}</select>
      </div>
      <div class="files-meta" id="qqLinkMeta">加载中…</div>
    </div>
    <div id="qqLinkItems"></div>
    <button id="qqLinkMore" class="mini ghost files-more" style="display:none">加载更多</button>`;
  const onChange = () => {
    qqLinkView.chat = $("#qqLinkChat").value;
    qqLinkView.platform = $("#qqLinkPlatform").value;
    qqLinkView.state = $("#qqLinkState").value;
    qqLinkView.days = +$("#qqLinkDays").value || 0;
    qqLinkView.sort = $("#qqLinkSort").value;
    refreshQqLinks(true);
  };
  ["#qqLinkChat", "#qqLinkPlatform", "#qqLinkState", "#qqLinkDays", "#qqLinkSort"].forEach(sel => $(sel).onchange = onChange);
  $("#qqLinkMore").onclick = () => refreshQqLinks(false);
}

async function showQqLinks() {
  if (!qqFileSessions) {
    try { qqFileSessions = (await api("/api/qq/local/sessions?limit=300")).sessions || []; }
    catch { qqFileSessions = []; }
  }
  renderQqLinkShell();
  await refreshQqLinks(true);
  showPane("#linkList");
  // 自动解析：进页面时小批量补正文（与微信链接的自动解析对齐）
  let st = null;
  try { st = await api("/api/qq/local/links/parse/status"); } catch {}
  if (!st || !st.running) startQqLinkParse(5, true);
}

async function refreshQqLinks(reset = true) {
  if (qqLinkView.loading) return;
  if (reset) { qqLinkView.offset = 0; qqLinkView.items = []; }
  qqLinkView.loading = true;
  const meta = $("#qqLinkMeta");
  if (meta) meta.textContent = "加载中…";
  const qs = new URLSearchParams({ limit: "100", offset: String(qqLinkView.offset),
                                   sort: qqLinkView.sort });
  if (qqLinkView.chat) qs.set("conv", qqLinkView.chat);
  if (qqLinkView.platform) qs.set("platform", qqLinkView.platform);
  if (qqLinkView.state) qs.set("state", qqLinkView.state);
  if (qqLinkView.days) qs.set("days", String(qqLinkView.days));
  if (qqLinkView.q) qs.set("q", qqLinkView.q);
  try {
    const d = await api("/api/qq/local/links?" + qs.toString());
    qqLinkView.items.push(...(d.links || []));
    qqLinkView.offset += (d.links || []).length;
    qqLinkView.hasMore = !!d.hasMore;
    qqLinkView.stats = d.stats || null;
    renderQqLinkItems();
  } catch (e) {
    if (meta) meta.textContent = "加载失败：" + e.message;
  } finally {
    qqLinkView.loading = false;
  }
}

function renderQqLinkItems() {
  const box = $("#qqLinkItems");
  if (!box) return;
  box.innerHTML = "";
  if (!qqLinkView.items.length) {
    box.innerHTML = `<div class="qa-empty">没有符合条件的 QQ 链接。<br>
      换个会话/平台/状态或时间范围试试；QQ 聊天里的链接导入后会自动补解析。</div>`;
  }
  for (const l of qqLinkView.items) {
    const d = document.createElement("div");
    d.className = "result-item";
    const badge = l.parsed
      ? `<span class="link-badge ok">已解析</span>`
      : l.state === "failed"
        ? `<span class="link-badge fail" title="${attrEsc(l.error || "抓取失败")}">解析失败</span>`
        : `<span class="link-badge">解析中</span>`;
    const title = (l.title || l.url).split("\n")[0];
    d.innerHTML = `<div class="meta"><span class="meta-trunc">${esc(l.conv_name || l.conv)} · ${fmtTime(l.ts)}</span> ${badge}</div>
      <div class="snippet"><span class="file-badge">${esc(l.platform || "网页")}</span> ${esc(title)}</div>`;
    d.onclick = () => openLinkPreview(l.url, l.title || l.url);
    box.appendChild(d);
  }
  const meta = $("#qqLinkMeta");
  if (meta) {
    const parsedN = qqLinkView.items.filter(x => x.parsed).length;
    meta.textContent = `${qqLinkView.items.length} 条 QQ 链接 · ${parsedN} 条已解析`
      + (qqLinkView.hasMore ? " · 还有更多" : "");
  }
  const more = $("#qqLinkMore");
  if (more) more.style.display = qqLinkView.hasMore ? "" : "none";
}

function startQqLinkParse(limit, quiet) {
  api("/api/qq/local/links/parse", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ limit }) }).then(() => {
    if (!quiet) toast("开始解析 QQ 链接正文");
    if (window._qqLinkTimer) clearInterval(window._qqLinkTimer);
    let n = 0;
    window._qqLinkTimer = setInterval(async () => {
      n += 1;
      let st;
      try { st = await api("/api/qq/local/links/parse/status"); }
      catch { return; }
      const meta = $("#qqLinkMeta");
      if (meta && st.running) meta.textContent = `正在解析链接正文 ${st.progress}…`;
      if (!st.running || n > 120) {
        clearInterval(window._qqLinkTimer);
        window._qqLinkTimer = null;
        if (state.view === "links" && isQQ()) refreshQqLinks(true);
      }
    }, 3000);
  }).catch(e => { if (!quiet) toast("启动解析失败：" + e.message); });
}


async function refreshFiles(reset = true) {
  if (isQQ()) return refreshQqFiles();
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
  if (isQQ()) return showQqFiles(opts);
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
const linkView = { platform: "", state: "", days: 0, sort: "new", q: "", chat: "",
                   offset: 0, items: [], hasMore: true, loading: false, queue: null };
let linkQueryTimer = null;

function renderLinkShell() {
  const box = $("#linkList");
  const opts = (rows, cur) => rows.map(([v, label]) =>
    `<option value="${attrEsc(String(v))}" ${String(v) === String(cur) ? "selected" : ""}>${esc(label)}</option>`).join("");
  box.innerHTML = `
    <div class="files-toolbar">
      <div class="files-filters">
        <select id="linkChat">${opts([["", "全部会话"], ...fileSessions.slice(0, 150).map(x => [x.username, x.name || x.username])], linkView.chat)}</select>
        <select id="linkPlatform">${opts([["", "全部平台"], ["xhs", "小红书"], ["bili", "B站"],
          ["article", T("linkArticle")], ["generic", "网页"]], linkView.platform)}</select>
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
  ["#linkChat", "#linkPlatform", "#linkState", "#linkDays", "#linkSort"].forEach(sel => $(sel).onchange = onChange);
  $("#linkMore").onclick = () => refreshLinks(false);
}

function renderLinkItems() {
  const box = $("#linkItems");
  if (!box) return;
  box.innerHTML = "";
  if (!linkView.items.length) {
    box.innerHTML = `<div class="qa-empty">没有符合条件的链接。<br>
      ${Q("换个平台/状态或时间范围试试；聊天里的链接会自动登记并解析。")}</div>`;
  }
  for (const l of linkView.items) {
    const d = document.createElement("div");
    d.className = "result-item";
    const badge = l.parsed
      ? `<span class="link-badge ok">已解析</span>`
      : l.platform_key === "wxapp" ? `<span class="link-badge">仅卡片信息</span>`
      : l.state === 2 ? `<span class="link-badge fail">暂不支持</span>`
      : `<span class="link-badge">解析中</span>`;
    d.innerHTML = `<div class="meta"><span class="meta-trunc">${esc(l.chat_name)} · ${fmtTime(l.received)}</span> ${badge}</div>
      <div class="snippet"><span class="file-badge">${esc(Q(l.platform))}</span> ${esc(l.title)}${l.chars ? ` · ${l.chars} 字` : ""}</div>`;
    d.onclick = () => {
      if (l.url.startsWith("wxapp://")) {
        toast(Q("小程序内容需要在微信里打开，无法抓取正文"));
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
  if (isQQ()) return refreshQqLinks();
  if (linkView.loading) return;
  if (reset) { linkView.offset = 0; linkView.items = []; }
  linkView.loading = true;
  const meta = $("#linkMeta");
  if (meta) meta.textContent = "加载中…";
  const qs = new URLSearchParams({ n: "100", offset: String(linkView.offset), sort: linkView.sort });
  if (linkView.chat) qs.set("chat", linkView.chat);
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
  if (isQQ()) return showQqLinks();
  if (!fileSessions) {
    try { fileSessions = await api("/api/sessions"); } catch { fileSessions = []; }
  }
  renderLinkShell();
  await refreshLinks(true);
  showPane("#linkList");
}

/* ---------------- QQ 空间：独立页面，不显示微信朋友圈数据 ---------------- */
function showQzone() {
  const box = $("#snsList");
  box.innerHTML = `
    <div class="result-item" style="flex-direction:column;align-items:flex-start;gap:8px">
      <div class="meta"><span>QQ 空间动态</span></div>
      <div class="snippet" style="white-space:normal">
        本机 QQ 数据库只保存聊天记录、联系人与文件，<b>不保存 QQ 空间动态</b>；
        空间内容需要登录手机 QQ 或网页版查看。<br><br>
        为了不混淆两个平台的数据，这里不会显示微信朋友圈的内容。
        想看朋友圈请点左上角 logo 切回微信界面。
      </div>
    </div>
    <div class="qa-empty">QQ 空间暂无本地数据</div>`;
  showPane("#snsList");
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
    box.innerHTML = `<div class="qa-empty">${Q("还没有朋友圈数据。<br>服务启动时会自动从微信解密库同步，稍等片刻再试。")}</div>`;
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
      p.n_links ? `${p.n_links} 篇链接` : "",
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
  $("#chatTitle").textContent = `${name} 的${T("momentsScope")}`;
  $("#chatMeta").textContent = `${fmtTime(p.ts)}${p.location ? " · " + p.location : ""}`;
  // 每条朋友圈有自己独立的 AI 会话作用域
  await enterAiScope(`moment:${tid}`, `${name} 的${T("momentsScope")}`);
  $("#scopeToggle").checked = true;
  updateScopeBar();
  updateDrawerScope();
  $("#input").placeholder = T("momentAsk");
  const imgs = (p.media || []).filter(m => !m.video);
  const vids = (p.media || []).filter(m => m.video);
  let html = `<div class="pv-head"><span class="pv-name">${esc(name)}</span>
      <span class="pv-time">${fmtTime(p.ts)}${p.location ? " · 📍" + esc(p.location) : ""}</span></div>`;
  if (p.text) html += `<pre class="pv-text sns-text">${esc(p.text)}</pre>`;
  for (const l of (p.links || [])) {
    html += `<div class="sns-link" data-url="${esc(l.url)}">
      <span class="sns-link-ico"></span>
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
  $("#chatTitle").textContent = T("momentImage");
  $("#chatMeta").textContent = `${author || ""}${ts ? " · " + fmtTime(ts) : ""}`;
  $("#pvBody").innerHTML =
    `<div class="pv-head"><span class="pv-name" id="snsImgBack"
        style="cursor:pointer">${T("momentBack")}</span></div>
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
    if (el) el.textContent = `${st.posts} ${T("momentsStatus")} · ${st.comments} 评论 · 图片文字 ${st.ocr_done} 张` +
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

/* 设置按平台独立：微信模式只有「微信通道」，QQ 模式只有「QQ 机器人」。
   通用分组（模型/隐私/数据/索引/关于）两边都有，但各自单独渲染、互不可见。 */
function settingSections() {
  const list = [
    { id: "general", label: "通用", icon: "⚙", desc: "开机自启与程序行为" },
    { id: "model", label: "AI 模型", icon: "✦", desc: "接口与回答方式" },
    { id: "privacy", label: "隐私与脱敏", icon: "⊘", desc: "发往云端前屏蔽敏感信息" },
  ];
  if (isQQ()) {
    list.push({ id: "qqbot", label: "QQ 机器人", icon: "⇄", desc: "qqbot 凭据、菜单与消息" });
    list.push({ id: "qqlocal", label: "QQ 数据与同步", icon: "↻",
                desc: "导入记录与文件/链接解析" });
    list.push({ id: "about", label: "关于", icon: "i", desc: "状态与本地数据" });
    return list;
  }
  list.push({ id: "wxbot", label: "微信通道", icon: "⇄", desc: "ClawBot 扫码与 Agent 入口" });
  list.push(
    { id: "data", label: "数据与同步", icon: "↻", desc: "实时入库与定时任务" },
    { id: "index", label: "索引任务", icon: "⌁", desc: "搜索与媒体处理" },
    { id: "about", label: "关于", icon: "i", desc: "状态与本地数据" });
  return list;
}
let settingsSection = "model";

async function showSettings(section) {
  const sections = settingSections();
  if (!sections.some(x => x.id === settingsSection)) settingsSection = "model";
  settingsSection = section || settingsSection || "model";
  // 离开「微信通道」时停掉二维码状态轮询，避免对已卸载的 DOM 反复刷新
  if (settingsSection !== "wxbot" && window._ocTimer) {
    clearInterval(window._ocTimer);
    window._ocTimer = null;
  }
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
  nav.innerHTML = sections.map(x =>
    `<button class="sf-item ${x.id === settingsSection ? "active" : ""}" data-sec="${x.id}" aria-current="${x.id === settingsSection ? "page" : "false"}">
      <span class="sf-item-icon">${x.icon}</span><span class="sf-item-copy"><b>${esc(x.label)}</b><small>${esc(x.desc)}</small></span><span class="sf-arrow">›</span>
    </button>`
  ).join("");
  nav.querySelectorAll(".sf-item").forEach(el => el.onclick = () => showSettings(el.dataset.sec));
  const body = document.querySelector("#sfBody");
  body.removeAttribute("data-l10n");   // 每次重渲染都要允许再次本地化
  body.innerHTML = `<div class="qa-empty">加载中…</div>`;

  if (settingsSection === "general") await renderSettingsGeneral(body);
  if (settingsSection === "model") await renderSettingsModel(body);
  if (settingsSection === "privacy") await renderSettingsPrivacy(body);
  if (settingsSection === "wxbot") await renderSettingsWxBot(body);
  if (settingsSection === "qqbot") await renderSettingsQqBot(body);
  if (settingsSection === "qqlocal") await renderSettingsQqLocal(body);
  if (settingsSection === "data") await renderSettingsData(body);
  if (settingsSection === "index") await renderSettingsIndex(body);
  if (settingsSection === "about") renderSettingsAbout(body, status);
  // 兜底：把设置页里残留的微信术语换成 QQ 说法（不影响 input value / 消息正文）
  if (window.Platform) window.Platform.localize(body);
}

async function renderSettingsGeneral(body) {
  let st = { supported: true, enabled: false, target: "" };
  try { st = await api("/api/autostart"); } catch {}
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">GENERAL</span><h2>通用</h2>
      <p>程序级开关，微信与 QQ 两个界面共用。</p></div>
      <span class="settings-status-pill ${st.enabled ? "success" : "neutral"}">${st.enabled ? "开机自启已开启" : "开机自启已关闭"}</span></div>
    <div class="settings-grid"><section class="settings-card">
      <div class="settings-card-head"><span class="settings-card-icon">⏻</span><div><h3>启动</h3><p>登录 Windows 后自动在托盘运行助手，消息同步不中断。</p></div></div>
      <div class="settings-switch-card">
        <label class="switch-row"><input type="checkbox" id="autoStart" ${st.enabled ? "checked" : ""} ${st.supported ? "" : "disabled"}><span class="switch-track"></span><span class="sw-copy"><b>开机自启</b><span class="sw-hint">${st.supported ? (st.enabled ? "已登记到当前用户启动项" : "关闭时需手动打开助手") : "仅支持 Windows"}</span></span></label>
      </div>
      <div class="settings-note" id="autoStartNote">${st.target ? "启动命令：" + esc(st.target) : ""}</div>
    </section>
    <aside class="settings-side-card"><div class="settings-side-title">说明</div>
      <p>开机自启写入当前用户的启动项（注册表 HKCU\Software\Microsoft\Windows\CurrentVersion\Run），只影响你的账号。</p>
      <p style="margin-top:8px">助手启动后常驻系统托盘，关闭窗口不会退出，右键托盘图标可显示主界面或退出。</p>
    </aside></div>`;
  const sw = $("#autoStart");
  if (sw) sw.onchange = async () => {
    sw.disabled = true;
    try {
      const r = await api("/api/autostart", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: sw.checked }) });
      const note = $("#autoStartNote");
      if (note) note.textContent = r.target ? "启动命令：" + r.target : "";
      toast(sw.checked ? "已设为开机自启" : "已取消开机自启");
    } catch (e) {
      sw.checked = !sw.checked;
      toast("设置失败：" + e.message);
    } finally { sw.disabled = false; }
  };
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
        <div class="settings-switch-card"><label class="switch-row"><input type="checkbox" id="cfgThinking" ${c.thinking ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>启用深度思考</b><span class="sw-hint">适合复杂检索、归纳和多步推理</span></span></label>
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
        <label class="switch-row"><input type="checkbox" id="pvEnabled" ${p.enabled ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>启用脱敏</b><span class="sw-hint">关闭后上下文原样发送到模型服务商</span></span></label>
        <label class="switch-row"><input type="checkbox" id="pvPhone" ${p.phone ? "checked" : ""} ${p.enabled ? "" : "disabled"}><span class="switch-track"></span><span class="sw-copy"><b>手机号<i>[手机号]</i></b><span class="sw-hint">发送前替换</span></span></label>
        <label class="switch-row"><input type="checkbox" id="pvIdcard" ${p.idcard ? "checked" : ""} ${p.enabled ? "" : "disabled"}><span class="switch-track"></span><span class="sw-copy"><b>身份证号<i>[身份证]</i></b><span class="sw-hint">发送前替换</span></span></label>
        <label class="switch-row"><input type="checkbox" id="pvBank" ${p.bankcard ? "checked" : ""} ${p.enabled ? "" : "disabled"}><span class="switch-track"></span><span class="sw-copy"><b>银行卡号<i>[银行卡]</i></b><span class="sw-hint">发送前替换</span></span></label>
        <label class="switch-row"><input type="checkbox" id="pvEmail" ${p.email ? "checked" : ""} ${p.enabled ? "" : "disabled"}><span class="switch-track"></span><span class="sw-copy"><b>邮箱<i>[邮箱]</i></b><span class="sw-hint">发送前替换</span></span></label>
      </div>
      <div class="settings-actions"><button id="pvSave">保存设置</button><span id="pvMsg" class="msg" aria-live="polite"></span></div>
    </section>
    <aside class="settings-side-card"><div class="settings-side-title">说明</div><p>脱敏发生在每次模型请求前，修改后无需重启服务。</p><ul><li>只影响新发出的请求</li><li>检索、引用跳转使用本地原文</li><li>本地 AI 模式同样适用</li></ul></aside></div>`;
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

/* 机器人设置已拆到 settings_bot.js（微信通道 / QQ 机器人各自独立） */

async function refreshQqParseStats() {
  const fp = $("#qqFileParsedStat"), lp = $("#qqLinkParsedStat");
  try {
    const fs = await api("/api/qq/local/files?limit=1");
    if (fp) fp.textContent = `${(fs.stats || {}).parsed || 0} / ${(fs.stats || {}).total || 0}`;
  } catch { if (fp) fp.textContent = "—"; }
  try {
    const ls = await api("/api/qq/local/links?limit=1");
    if (lp) lp.textContent = `${(ls.stats || {}).parsed || 0} / ${(ls.stats || {}).total || 0}`;
  } catch { if (lp) lp.textContent = "—"; }
}

function pollQqParseStatus(kind) {
  const msg = $("#qqParseMsg");
  if (window._qqParseTimer) clearInterval(window._qqParseTimer);
  window._qqParseTimer = setInterval(async () => {
    let st;
    try { st = await api(`/api/qq/local/${kind}/parse/status`); }
    catch { return; }
    if (msg) msg.textContent = st.progress || "";
    if (!st.running) {
      clearInterval(window._qqParseTimer);
      window._qqParseTimer = null;
      refreshQqParseStats();
    }
  }, 2000);
}

function pollQqSyncStatus() {
  if (window._qqSyncTimer) clearInterval(window._qqSyncTimer);
  window._qqSyncTimer = setInterval(async () => {
    // 只在 QQ 数据与同步设置页可见时轮询
    if ($("#settingsFull").style.display === "none" ||
        !document.querySelector("#qqSyncState")) {
      clearInterval(window._qqSyncTimer); window._qqSyncTimer = null; return;
    }
    let d;
    try { d = await api("/api/qq/local/sync/status"); } catch { return; }
    const st = d.state || {};
    const note = $("#qqSyncState"), btn = $("#qqSyncNow");
    if (note) {
      note.textContent = st.running ? ("正在同步：" + (st.progress || ""))
        : (st.last ? "上次同步：" + st.last : "尚未同步")
        + (st.error ? " · " + st.error : "");
    }
    if (btn) { btn.disabled = !!st.running; btn.textContent = st.running ? "同步中…" : "立即同步一次"; }
    if (!st.running) { refreshQqParseStats(); }
  }, 3000);
}

async function renderSettingsQqLocal(body) {
  let data = { state: {}, stats: {} };
  let sync = { state: {}, config: { auto: true, interval_min: 10 } };
  try { data = await api("/api/qq/local/import/status"); } catch {}
  try { sync = await api("/api/qq/local/sync/status"); } catch {}
  const st = data.state || {}, stats = data.stats || {};
  const syncState = sync.state || {}, syncConf = sync.config || { auto: true, interval_min: 10 };
  const syncRunning = !!syncState.running;
  const running = !!st.running;
  const result = st.result ? JSON.stringify(st.result) : "";
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">QQ DATA &amp; SYNC</span><h2>QQ 数据与同步</h2><p>从本机 QQ NT 数据库只读导入聊天记录、联系人、文件与链接，并管理正文解析任务；不改动 QQ 数据。</p></div>
      <span class="settings-status-pill ${syncRunning ? "success" : (stats.messages ? "success" : "warning")}">${syncRunning ? "实时同步中" : (syncConf.realtime !== false ? "实时监听已开启" : (stats.messages ? "已导入" : "未导入"))}</span></div>
    <div class="settings-grid"><section class="settings-card">
      <div class="settings-card-head"><span class="settings-card-icon">↻</span><div><h3>自动同步</h3><p>实时监听 QQ 库变化即时增量入库；并自动解析新文件/链接正文。</p></div></div>
      <div class="settings-switch-card">
        <label class="switch-row"><input type="checkbox" id="qqSyncRt" ${syncConf.realtime !== false ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>启用实时同步</b><span class="sw-hint">每 5 秒检测 QQ 库变化，有新消息约 10~20 秒内可查（需 QQ 在线）</span></span></label>
        <label class="switch-row"><input type="checkbox" id="qqSyncAuto" ${syncConf.auto ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>启用兜底定时同步</b><span class="sw-hint">每 <input class="inline-number" id="qqSyncInterval" type="number" min="5" value="${syncConf.interval_min}"> 分钟全量窗口补一次，防止实时漏单</span></span></label>
      </div>
      <div class="settings-note" id="qqSyncState">${syncRunning ? "正在同步：" + esc(syncState.progress || "") : (syncState.last ? "上次同步：" + esc(syncState.last) : "尚未同步")}${syncState.error ? " · " + esc(syncState.error) : ""}</div>
      <div class="settings-actions">
        <button id="qqSyncNow" ${syncRunning ? "disabled" : ""}>${syncRunning ? "同步中…" : "立即同步一次"}</button>
        <span id="qqSyncMsg" class="msg"></span>
      </div>
    </section>
    <section class="settings-card">
      <div class="settings-card-head"><span class="settings-card-icon">↓</span><div><h3>本机 QQ 数据</h3><p>QQ 需要保持登录；导入期间可继续使用，完成后消息页/联系人/文件会自动刷新。</p></div></div>
      <div class="settings-form">
        <div class="settings-mini-stat"><span>消息</span><b>${stats.messages || 0}</b></div>
        <div class="settings-mini-stat"><span>会话</span><b>${stats.chats || 0}</b></div>
        <div class="settings-mini-stat"><span>联系人 / 群</span><b>${stats.contacts || 0}</b></div>
        <div class="settings-mini-stat"><span>文件</span><b>${stats.files || 0}</b></div>
        <div class="settings-mini-stat"><span>链接</span><b>${stats.links || 0}</b></div>
        <div class="settings-actions">
          <button id="qqLocalImport" ${running ? "disabled" : ""}>${running ? "正在导入…" : "重新导入 QQ 聊天记录"}</button>
          <span id="qqLocalMsg" class="msg">${esc(st.progress || "")}${result ? " " + esc(result) : ""}</span>
        </div>
      </div>
    </section>
    <section class="settings-card">
      <div class="settings-card-head"><span class="settings-card-icon">⌁</span><div><h3>文件与链接解析</h3><p>文件正文在预览时自动提取；也可批量解析。链接正文在打开链接页时自动补齐。</p></div></div>
      <div class="settings-form">
        <div class="settings-mini-stat"><span>文件已解析</span><b id="qqFileParsedStat">…</b></div>
        <div class="settings-mini-stat"><span>链接已解析</span><b id="qqLinkParsedStat">…</b></div>
        <div class="settings-actions">
          <button id="qqFileParseBtn">批量解析文件正文</button>
          <button id="qqLinkParseBtn" class="ghost">批量解析链接正文</button>
          <span id="qqParseMsg" class="msg"></span>
        </div>
      </div>
    </section>
    <aside class="settings-side-card"><div class="settings-side-title">说明</div>
      <p>导入只读取自己的本机数据库：密钥在 QQ 进程内存中定位，聊天库解密到临时目录，写入完成后删除。</p>
      <ul><li>QQ 未登录时无法取密钥</li><li>可重复导入，按消息 ID 覆盖</li><li>不会修改或上传 QQ 数据</li></ul>
    </aside></div>`;
  refreshQqParseStats();
  const btn = $("#qqLocalImport");
  if (btn) btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = "正在导入…";
    const msg = $("#qqLocalMsg");
    try {
      await api("/api/qq/local/import", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rebuild: true }) });
      if (msg) msg.textContent = "已开始导入，请保持 QQ 登录…";
      if (window._qqImportTimer) clearInterval(window._qqImportTimer);
      window._qqImportTimer = setInterval(async () => {
        let d;
        try { d = await api("/api/qq/local/import/status"); } catch { return; }
        const s = d.state || {};
        if (msg) msg.textContent = s.progress || "";
        if (!s.running) {
          clearInterval(window._qqImportTimer);
          window._qqImportTimer = null;
          if (s.error) toast("QQ 导入失败：" + s.error);
          else toast("QQ 本地记录已导入");
          showSettings("qqlocal");
        }
      }, 2000);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "重新导入 QQ 聊天记录";
      if (msg) msg.textContent = "启动失败：" + e.message;
    }
  };
  const syncSave = async () => {
    try {
      await api("/api/qq/local/sync/config", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auto: $("#qqSyncAuto").checked,
                               realtime: $("#qqSyncRt").checked,
                               interval_min: +$("#qqSyncInterval").value || 10 }) });
      toast("QQ 同步设置已保存");
    } catch (e) { toast("保存失败：" + e.message); }
  };
  if ($("#qqSyncAuto")) $("#qqSyncAuto").onchange = syncSave;
  if ($("#qqSyncRt")) $("#qqSyncRt").onchange = syncSave;
  if ($("#qqSyncInterval")) $("#qqSyncInterval").onchange = syncSave;
  const syncBtn = $("#qqSyncNow");
  if (syncBtn) syncBtn.onclick = async () => {
    syncBtn.disabled = true; syncBtn.textContent = "同步中…";
    try {
      await api("/api/qq/local/sync", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: "{}" });
      pollQqSyncStatus();
    } catch (e) { toast("启动同步失败：" + e.message); syncBtn.disabled = false; }
  };
  pollQqSyncStatus();
  const fpBtn = $("#qqFileParseBtn");
  if (fpBtn) fpBtn.onclick = async () => {
    fpBtn.disabled = true;
    try {
      await api("/api/qq/local/files/parse", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit: 100 }) });
      pollQqParseStatus("files");
    } catch (e) {
      const msg = $("#qqParseMsg");
      if (msg) msg.textContent = "启动失败：" + e.message;
      fpBtn.disabled = false;
    }
  };
  const lpBtn = $("#qqLinkParseBtn");
  if (lpBtn) lpBtn.onclick = async () => {
    lpBtn.disabled = true;
    try {
      await api("/api/qq/local/links/parse", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit: 30 }) });
      pollQqParseStatus("links");
    } catch (e) {
      const msg = $("#qqParseMsg");
      if (msg) msg.textContent = "启动失败：" + e.message;
      lpBtn.disabled = false;
    }
  };
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
        <div class="settings-switch-card">
          <label class="switch-row"><input type="checkbox" id="rtSync" ${rt.realtime ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>启用实时同步</b><span class="sw-hint">需要微信已登录且密钥有效</span></span></label>
          <label class="switch-row"><input type="checkbox" id="rtMedia" ${rt.media_on ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>新图片自动 OCR</b><span class="sw-hint">仅最近 7 天新图片，历史在索引任务补跑</span></span></label>
          <label class="switch-row"><input type="checkbox" id="rtAsr" ${rt.asr_on ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>新语音自动转写</b><span class="sw-hint">仅最近 7 天新语音/视频，历史在索引任务补跑</span></span></label>
          <div class="inline-setting"><label for="rtInterval">兜底轮询</label><div><input id="rtInterval" type="number" min="15" value="${rt.realtime_sec}"> <span>秒</span></div></div>
        </div>
        <div class="settings-note" id="rtState"></div>
      </section>
      <section class="settings-card"><div class="settings-card-head"><span class="settings-card-icon">◷</span><div><h3>定时同步</h3><p>定期扫描数据库，适合实时监听不可用时使用。</p></div></div>
        <div class="settings-switch-card">
          <label class="switch-row"><input type="checkbox" id="autoSync" ${a.auto ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>启用定时同步</b><span class="sw-hint">每 <input class="inline-number" id="autoInterval" type="number" min="5" value="${a.interval_min}"> 分钟执行一次</span></span></label>
        </div>
        <div class="settings-note">${a.last ? "上次运行：" + esc(a.last) : "尚未运行"}</div>
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
  if (isQQ()) return renderSettingsAboutQQ(body);
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">LOCAL KNOWLEDGE BASE</span><h2>关于</h2><p>查看当前知识库规模、运行模式与本地数据说明。</p></div><span class="settings-status-pill success">运行正常</span></div>
    <section class="settings-card"><div class="settings-card-head"><span class="settings-card-icon">i</span><div><h3>知识库概览</h3><p>微信 / QQ 两边的数据规模与本机占用。</p></div></div>
      <div class="settings-stats settings-stats-6" id="kbStats"><div class="settings-stat"><b>…</b><span>统计中</span></div></div>
      <div class="settings-info-list"><div><span>问答模式</span><b>${status.llm === "configured" ? "AI 生成 · " + esc(status.llm_model || "") : "检索模式（未配置 Key）"}</b></div><div><span>数据库密钥</span><b>fp=${esc(status.key_fp || "未读取")}</b></div><div><span>数据位置</span><b>data/knowledge.db 与 data/decrypted</b></div></div></section>
    <section class="settings-card" id="storageCard"><div class="settings-card-head"><span class="settings-card-icon">◍</span><div><h3>占用明细</h3><p>数据库按消息占比分摊到两边；媒体缓存按各自目录实测。</p></div></div><div id="storageBody" class="settings-form"><div class="qa-empty">统计中…</div></div></section>
    <div class="settings-privacy"><b>隐私说明</b><span>微信数据、API Key 与解密文件均只保存在本机。</span></div>`;
  loadStorageStats();
}

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + " GB";
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n + " B";
}
async function loadStorageStats() {
  const box = $("#storageBody");
  const statsBox = $("#kbStats");
  if (!box && !statsBox) return;
  let d;
  try { d = await api("/api/storage"); }
  catch (e) { if (box) box.innerHTML = `<div class="qa-empty">统计失败：${esc(e.message)}</div>`; return; }
  if ($("#kbStats")) {
    const wx = d.wechat || {}, qq = d.qq || {};
    const n = (x) => (Number(x) || 0).toLocaleString();
    const cell = (v, label) => `<div class="settings-stat"><b>${v}</b><span>${label}</span></div>`;
    $("#kbStats").innerHTML =
      cell(n(wx.rows?.messages), "微信消息") +
      cell(n(qq.rows?.messages), "QQ 消息") +
      cell(n(wx.rows?.contacts), "微信联系人") +
      cell(n(qq.rows?.contacts), "QQ 联系人/群") +
      cell(fmtBytes(wx.bytes), "微信占用") +
      cell(fmtBytes(qq.bytes), "QQ 占用");
  }
  if (!box) return;
  const wx = d.wechat || {}, qq = d.qq || {}, db = d.db || {};
  const row = (label, size, extra) => `
    <div class="settings-mini-stat"><span>${label}${extra ? " · " + extra : ""}</span><b>${fmtBytes(size)}</b></div>`;
  box.innerHTML = `
    <div class="settings-mini-stat"><span>数据库 knowledge.db</span><b>${fmtBytes(db.bytes)}</b></div>
    ${row("微信媒体缓存", wx.bytes, (wx.rows?.messages || 0).toLocaleString() + " 条消息")}
    ${row("QQ 实时副本 + 媒体", qq.bytes, (qq.rows?.messages || 0).toLocaleString() + " 条消息")}
    <div class="settings-mini-stat" style="border-top:1px solid #ececf0;margin-top:6px;padding-top:8px">
      <span><b>合计占用</b></span><b>${fmtBytes(d.total?.bytes || 0)}</b></div>
    <div class="settings-note" style="margin-top:6px">数据库按消息条数分摊：微信 ${fmtBytes(wx.db_bytes)} · QQ ${fmtBytes(qq.db_bytes)}；微信有效消息 ${(wx.rows?.messages_real || 0).toLocaleString()} 条（排除 ${((wx.rows?.messages || 0) - (wx.rows?.messages_real || 0)).toLocaleString()} 条公众号/系统噪音）。QQ 的实时解密副本在 QQ 媒体占用里，可随时删除（会自动重建）。</div>`;
}

async function renderSettingsAboutQQ(body) {
  let o, s;
  try {
    [o, s] = await Promise.all([api("/api/qq/local/overview"), api("/api/status").catch(() => null)]);
  } catch (e) { body.innerHTML = `<div class="qa-empty">加载失败：${esc(e.message)}</div>`; return; }
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">QQ LOCAL KNOWLEDGE BASE</span><h2>关于（QQ）</h2><p>查看 QQ 侧知识库规模、机器人运行状态与本地数据说明。</p></div><span class="settings-status-pill success">运行正常</span></div>
    <section class="settings-card"><div class="settings-card-head"><span class="settings-card-icon">i</span><div><h3>知识库概览</h3><p>微信 / QQ 两边的数据规模与本机占用。</p></div></div>
      <div class="settings-stats settings-stats-6" id="kbStats"><div class="settings-stat"><b>…</b><span>统计中</span></div></div>
      <div class="settings-info-list"><div><span>问答模式</span><b>${(s && s.llm === "configured") ? "AI 生成 · " + esc(s.llm_model || "") : "检索模式（未配置 Key）"}</b></div><div><span>QQ 数据库</span><b>${esc(o.last_ts ? "NT 数据库（只读解密）" : "尚未导入")}</b></div><div><span>数据位置</span><b>data/knowledge.db（QQ 本地表）与 data/decrypted</b></div></div></section>
    <section class="settings-card" id="storageCard"><div class="settings-card-head"><span class="settings-card-icon">◍</span><div><h3>占用明细</h3><p>数据库按消息占比分摊到两边；媒体缓存按各自目录实测。</p></div></div><div id="storageBody" class="settings-form"><div class="qa-empty">统计中…</div></div></section>
    <div class="settings-privacy"><b>隐私说明</b><span>QQ 数据仅保存在本机，不在微信界面出现；API Key 与解密文件只保存在本机。</span></div>`;
  loadStorageStats();
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
  if (url.startsWith("wxapp://")) { toast(Q("小程序内容需要在微信里打开，无法抓取正文")); return; }
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
      ${T("articleHint")}</div>`;
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
      `<div class="qa-empty">${Q("文件不在本地磁盘（可能已被微信清理或未下载）。")}<br>
       ${meta.msg_id ? `<a href="javascript:void(0)" id="jumpToFileMsg">跳转到这条文件消息 ↗</a>（查看聊天上下文）<br>` : ""}
       ${Q("仍可在右侧提问，AI 会结合聊天记录中的描述回答。")}</div>`;
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
  renderQqViewTabs();
  if (v !== "chats") qqLiveStop();
  if (v !== "settings") $("#settingsFull").style.display = "none";
  if (typeof previewLink !== "undefined" && v !== "links") previewLink = null;
  document.querySelectorAll(".rail-btn").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  if (v === "chats") {
    $("#assistantPane").style.display = "none";
    $("#aiSessionsPanel").style.display = "none";
    $("#settingsFull").style.display = "none";
    document.querySelector(".chat-column").style.display = "flex";
    $("#drawer").style.display = "flex";
    if (isQQ()) {
      showPane("#qqList");
      loadQqConversations();
    } else {
      showPane(state.searchMode && $("#resultList").children.length ? "#resultList" : "#sessionList");
    }
  }
  if (v === "contacts") showContacts();
  if (v === "files") {
    $("#searchInput").placeholder = Q("搜索文件名（选中会话可缩小范围）");
    showFiles();
    return;
  }
  if (v === "links") {
    $("#searchInput").placeholder = T("searchLinks");
    showLinks();
    return;
  }
  if (v === "moments") {
    if (isQQ()) {
      $("#searchInput").placeholder = "QQ 空间动态（暂无本地数据）";
      showQzone();
      return;
    }
    $("#searchInput").placeholder = T("searchMoments");
    showMoments();
    refreshSnsStatus();
    return;
  }
  $("#searchInput").placeholder = T("searchDefault");
  if (v === "settings") showSettings();
}
$("#searchInput").addEventListener("input", (e) => {
  if (state.view === "links") {
    qqLinkView.q = e.target.value;
    if (isQQ()) {
      clearTimeout(linkQueryTimer);
      linkQueryTimer = setTimeout(() => refreshQqLinks(true), 300);
      return;
    }
    linkView.q = e.target.value;
    clearTimeout(linkQueryTimer);
    linkQueryTimer = setTimeout(() => refreshLinks(true), 300);
    return;
  }
  if (state.view === "moments") {
    if (isQQ()) { showQzone(); return; }
    showMoments(e.target.value);
    return;
  }
  if (state.view === "contacts") {
    clearTimeout(window._contactTimer);
    if (isQQ()) {
      window._contactTimer = setTimeout(() => showQqContacts(e.target.value), 350);
      return;
    }
    window._contactTimer = setTimeout(() => showContacts(e.target.value), 350);
    return;
  }
  if (state.view === "files") {
    qqFileView.q = e.target.value;
    if (isQQ()) {
      clearTimeout(fileQueryTimer);
      fileQueryTimer = setTimeout(() => refreshQqFiles(true), 300);
      return;
    }
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
  if (e.key === "Enter" && state.view === "files") {
    clearTimeout(fileQueryTimer);
    if (isQQ()) refreshQqFiles(true); else refreshFiles(true);
  }
  if (e.key === "Enter" && state.view === "links") {
    clearTimeout(linkQueryTimer);
    if (isQQ()) refreshQqLinks(true); else refreshLinks(true);
  }
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
    // 按当前平台初始化列表：QQ 模式要显示 QQ 会话 + 视图切换 tab，
    // 微信模式显示会话列表。刷新页面后也必须保持一致。
    switchView(state.view || "chats");
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

/* ---------------------------------------------------------------- QQ 模式：qqbot 消息
   QQ 模式的机器人会话来自 qqbot，网关收到的私聊 / 群 @ 消息与回复落在本机，
   这里提供「消息」页的会话列表与只读视图；AI 问答仍照搬微信模式。 */
function renderQqViewTabs() {
  const bar = $("#qqViewTabs");
  if (bar) bar.style.display = "none";
}

function showQqSessionMenu(x, y, c) {
  closeSessionMenu();
  const menu = document.createElement("div");
  menu.id = "ctxMenu";
  menu.className = "ctx-menu";
  const items = [
    c.pinned
      ? { label: "取消置顶", fn: () => qqSetPin(c, 0) }
      : { label: "置顶会话", fn: () => qqSetPin(c, 1) },
    { label: "打开会话", fn: () => openQqLocalConversation(c.conv, c.name, !!c.is_group) },
    { label: "只看该会话的文件", fn: () => { showFiles({ chat: c.conv, q: "" }); } },
    { label: "复制会话标识", fn: () => { navigator.clipboard && navigator.clipboard.writeText(c.conv); toast("已复制 " + c.conv); } },
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

async function qqSetPin(c, pinned) {
  try {
    await api("/api/qq/local/session/pin", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conv: c.conv, pinned }) });
    c.pinned = pinned;
    toast(pinned ? "已置顶" : "已取消置顶");
    loadQqConversations();
  } catch (e) { toast("操作失败：" + e.message); }
}

async function loadQqConversations() {
  const box = $("#qqList");
  if (!box) return;
  box.innerHTML = `<div id="qqConvList"></div>`;
  const list = $("#qqConvList");
  const ai = document.createElement("div");
  ai.className = "session-item ai";
  ai.innerHTML = `<div class="ava">✦</div>
    <div class="info"><div class="row1"><span class="name">AI 助手<b class="badge">置顶</b></span></div>
    <div class="row2">${Q("全局问答：QQ 聊天记录 + 随时追问")}</div></div>`;
  ai.onclick = () => openAssistant();
  list.appendChild(ai);

  let locals = [];
  try {
    const d = await api("/api/qq/local/sessions?limit=300");
    locals = d.sessions || [];
  } catch (e) {
    const err = document.createElement("div");
    err.className = "qa-empty";
    err.textContent = "QQ 本地记录读取失败：" + e.message;
    list.appendChild(err);
  }
  if (locals.length) {
    const sorted = [...locals].sort((a, b) => (b.pinned - a.pinned) || (b.last_ts - a.last_ts));
    for (const c of sorted) {
      const item = document.createElement("div");
      item.className = "session-item qq-conv" + (state.qqConv === c.conv ? " active" : "");
      item.dataset.conv = c.conv;
      const last = (c.last_is_sender ? "我：" : "") + (c.last_text || "");
      if (c.pinned) item.classList.add("pinned");
      const akind = c.is_group ? "group" : "friend";
      const akey = c.is_group ? (c.uin || String(c.conv).replace("group:", "")) : (c.uin || String(c.conv).replace("c2c:", ""));
      item.innerHTML = `${qqAvatarHtml(akind, akey, c.name)}
        <div class="info"><div class="row1"><span class="name">${esc(c.name)}</span>
        <span class="time">${c.last_ts ? fmtTime(c.last_ts).split(" ").pop() : ""}</span></div>
        <div class="row2">${esc(last)}</div></div>
        <span class="pin-act" title="${c.pinned ? "取消置顶" : "置顶"}">${c.pinned ? "⊖" : "📌"}</span>`;
      item.onclick = () => openQqLocalConversation(c.conv, c.name, !!c.is_group);
      const pinBtn = item.querySelector(".pin-act");
      pinBtn.onclick = async (e) => {
        e.stopPropagation();
        try {
          await api("/api/qq/local/session/pin", { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ conv: c.conv, pinned: c.pinned ? 0 : 1 }) });
          toast(c.pinned ? "已取消置顶" : "已置顶");
          loadQqConversations();
        } catch (err) { toast("操作失败：" + err.message); }
      };
      item.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        e.stopPropagation();
        showQqSessionMenu(e.clientX, e.clientY, c);
      });
      list.appendChild(item);
    }
  }

  let convs = [];
  try {
    const d = await api("/api/qq/conversations");
    convs = d.conversations || [];
  } catch (e) {
    const err = document.createElement("div");
    err.className = "qa-empty";
    err.textContent = "QQ 机器人消息读取失败：" + e.message;
    list.appendChild(err);
  }
  if (convs.length) {
    for (const c of convs) {
      const item = document.createElement("div");
      item.className = "session-item qq-conv" + (state.qqConv === c.conv ? " active" : "");
      item.dataset.conv = c.conv;
      item.innerHTML = `${qqAvatarHtml(c.scene === "group" ? "group" : "friend", String(c.conv || "").replace(/^(group|c2c):/, ""), c.name)}
        <div class="info"><div class="row1"><span class="name">${esc(c.name)}</span>
        <span class="time">${c.last_ts ? fmtTime(c.last_ts).split(" ").pop() : ""}</span></div>
        <div class="row2">${esc(c.last || "")}</div></div>`;
      item.onclick = () => openQqConversation(c.conv, c.name, c.scene);
      item.addEventListener("contextmenu", async (e) => {
        e.preventDefault();
        const name = prompt("给这个会话起个名字", c.name);
        if (!name || !name.trim()) return;
        try {
          await api("/api/qq/conversations/rename", { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ conv: c.conv, name: name.trim() }) });
          toast("名称已更新");
          loadQqConversations();
        } catch (err) { toast("改名失败：" + err.message); }
      });
      list.appendChild(item);
    }
  }
  if (!locals.length && !convs.length) {
    const empty = document.createElement("div");
    empty.className = "qa-empty qq-empty";
    empty.textContent = "还没有 QQ 记录。到「设置 → QQ 本地记录」点「导入 QQ 聊天记录」。";
    list.appendChild(empty);
  }
}

function renderQqMessage(it) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + (it.is_self ? "right" : "left");
  wrap.dataset.id = "qq-" + it.id;
  wrap.dataset.ts = String(it.ts || 0);
  const ava = document.createElement("div");
  ava.className = "ava";
  ava.style.background = hue(it.who || "QQ");
  ava.innerHTML = `<span class="ava-fallback">${esc(initial(it.is_self ? "助手" : (it.who || "Q")))}</span>`;
  wrap.appendChild(ava);
  const col = document.createElement("div");
  col.className = "col";
  if (!it.is_self) {
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = it.who || "QQ 用户";
    col.appendChild(who);
  }
  // 时间不放在气泡上方（会导致左右缩进错位），统一用中置时间分隔
  const b = document.createElement("div");
  b.className = "bubble";
  b.textContent = it.text || "";
  b.setAttribute("data-nol10n", "");
  const media = Array.isArray(it.media) ? it.media : [];
  if (media.length) {
    const mbox = document.createElement("div");
    mbox.className = "qq-media";
    for (const m of media) {
      if (!m || !m.kind) continue;
      const src = m.name ? "/api/qq/media/" + encodeURIComponent(m.name) : "";
      if (m.kind === "image" && src) {
        const img = document.createElement("img");
        img.className = "qq-img";
        img.loading = "lazy";
        img.src = src;
        img.alt = m.text || m.filename || "图片";
        img.onclick = () => window.open(src, "_blank");
        mbox.appendChild(img);
      } else if (m.kind === "voice" && src) {
        const au = document.createElement("audio");
        au.className = "qq-audio";
        au.controls = true;
        au.preload = "none";
        au.src = src;
        mbox.appendChild(au);
      } else if (m.kind === "video" && src) {
        const vd = document.createElement("video");
        vd.className = "qq-video";
        vd.controls = true;
        vd.preload = "none";
        vd.src = src;
        mbox.appendChild(vd);
      } else if (m.kind === "file") {
        const a = document.createElement("a");
        a.className = "qq-file";
        a.textContent = "文件 · " + (m.filename || "未命名文件");
        if (src) {
          a.href = src;
          a.setAttribute("download", m.filename || "");
        }
        mbox.appendChild(a);
      } else if (m.kind === "card" && m.url) {
        // 分享链接 / 小程序卡片：点击打开对应链接
        const a = document.createElement("a");
        a.className = "qq-link-card";
        a.href = m.url;
        a.target = "_blank";
        a.rel = "noopener";
        a.innerHTML = `<b>${esc(m.tag || "分享")}</b><span>${esc(m.title || m.url)}</span>`
          + (m.desc ? `<small>${esc(m.desc)}</small>` : "");
        mbox.appendChild(a);
      }
    }
    if (mbox.children.length) b.appendChild(mbox);
  }
  col.appendChild(b);
  wrap.appendChild(col);
  wrap.title = "右键加入引用栏";
  wrap.oncontextmenu = (e) => {
    e.preventDefault();
    addQuote({ id: "qq-" + it.id, ts: it.ts, who: it.who,
               text: it.text, chat: state.qqConv,
               sender_name: it.who, parsed: { text: it.text } });
    toast(T("quoteAdded"));
  };
  return wrap;
}

async function openQqConversation(conv, name, scene, focusId = 0) {
  qqLiveStop();
  state.qqConv = conv;
  state.qqConvKind = "bot";
  state.qqConvName = name || conv;
  state.current = null;
  state.searchMode = false;
  stopLiveStream();
  if (typeof previewLink !== "undefined") previewLink = null;
  syncQqConvActive();
  showChatPane();
  $("#assistantPane").style.display = "none";
  $("#aiSessionsPanel").style.display = "none";
  document.querySelector(".chat-column").style.display = "flex";
  $("#drawer").style.display = "flex";
  $("#previewPane").style.display = "none";
  $("#msgList").style.display = "block";
  $("#chatTitle").textContent = name || conv;
  $("#chatMeta").textContent = "QQ " + (scene === "group" ? "群聊" : "私聊");
  $("#msgs").innerHTML = `<div class="sys">QQ 机器人收到的消息与回复（只读）</div>`;
  try {
    const d = await api("/api/qq/messages?conv=" + encodeURIComponent(conv) + "&limit=200");
    const frag = document.createDocumentFragment();
    for (const it of (d.messages || [])) frag.appendChild(renderQqMessage(it));
    $("#msgs").appendChild(frag);
  } catch (e) {
    const err = document.createElement("div");
    err.className = "sys";
    err.textContent = "消息读取失败：" + e.message;
    $("#msgs").appendChild(err);
  }
  scrollMsgsToBottom();
  if (focusId) {
    const el = $("#msgs").querySelector(`[data-id="qq-${focusId}"]`);
    if (el) {
      el.scrollIntoView({ block: "center" });
      el.classList.add("qq-focus");
      setTimeout(() => el.classList.remove("qq-focus"), 2200);
    }
  }
  $("#scopeToggle").checked = false;    // QQ 会话没有知识库范围，提问走全局
  updateDrawerScope();
}

/* ---------------------------------------------------------------- QQ 本地记录（NT 数据库）
   QQ 模式的消息列表来自本机 QQ NT 解密库，与微信知识库完全分开。 */
/* QQ 本地会话实时刷新：与微信同款 SSE（后端同步入库后 0.5 秒内推送） */
let _qqEs = null;
function _qqLastLocalId() {
  let mx = 0;
  document.querySelectorAll("#msgs [data-id^='qq-local-']").forEach(el => {
    const n = +String(el.dataset.id).replace("qq-local-", "");
    if (n > mx) mx = n;
  });
  return mx;
}
function qqLiveStart() {
  qqLiveStop();
  if (!(isQQ() && state.qqConvKind === "local" && state.qqConv)) return;
  const conv = state.qqConv;
  const after = _qqLastLocalId();
  const es = new EventSource(`/api/qq/local/stream?conv=${encodeURIComponent(conv)}&after_id=${after}`);
  _qqEs = es;
  es.onmessage = (ev) => {
    let d = null;
    try { d = JSON.parse(ev.data); } catch { return; }
    if (!d) return;
    if (d.messages && d.messages.length) {
      if (!(isQQ() && state.qqConvKind === "local" && state.qqConv === conv)) return;
      if ($("#msgList").style.display === "none") return;   // 预览/设置页打开时不插
      const box = $("#msgs");
      const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 100;
      const have = new Set([...box.querySelectorAll("[data-id^='qq-local-']")]
        .map(el => +String(el.dataset.id).replace("qq-local-", "")));
      const wraps = [...box.querySelectorAll(".msg[data-id^='qq-local-']")];
      let prevTs = wraps.length ? +(wraps[wraps.length - 1].dataset.ts || 0) : 0;
      for (const it of d.messages) {
        if (have.has(it.id)) continue;
        if (needsDivider(prevTs, it.ts)) box.appendChild(makeTimeDivider(it.ts));
        box.appendChild(renderQqLocalMessage(it));
        prevTs = it.ts || prevTs;
      }
      if (stick) scrollMsgsToBottom();
    }
    if (d.chats) {
      const list = $("#qqConvList");
      const keep = list ? list.scrollTop : 0;
      loadQqConversations().then(() => {
        const l2 = $("#qqConvList");
        if (l2) l2.scrollTop = keep;
      });
    }
  };
  es.onerror = () => { /* EventSource 自带重连 */ };
}
function qqLiveStop() {
  if (_qqEs) { try { _qqEs.close(); } catch {} _qqEs = null; }
}

function qqLocalMediaSrc(path) {
  return path ? "/api/qq/local/media?path=" + encodeURIComponent(path) : "";
}

function isGroupChat() {
  if (isQQ()) return String(state.qqConv || "").startsWith("group:");
  return !!(state.current && state.current.is_group);
}

/* 单行系统通知（加群/退群/撤回/改群名…）：和微信一样居中灰字，不套气泡 */
const QQ_SYS_NOTICE_RE = /^[^\n]{0,60}(加入了群聊|加入群聊|退出了群聊|退出群聊|撤回了一条消息|修改群名为[^\n]{0,40}|群名已修改[^\n]{0,40}|被移出群聊|移出了群聊|被管理员禁言|已被禁言|解除禁言|成为群成员|成为新群主|(开启|关闭)了全员禁言|邀请[^\n]{0,40}加入了?群聊)[。！!]?$/;

/* 纯文本里的卡片式分享（bilibili 等转发到 QQ 会变成「[标题-bilibili] https://...」） */
function parseTextCard(text) {
  const t = String(text || "").trim();
  const m = t.match(/^\[([^\]\n]{1,120})[-－]([^\]\n]{1,20})\]\s*(https?:\/\/\S+)$/);
  if (m) return { title: m[1].trim(), tag: m[2].trim(), url: m[3] };
  const m2 = t.match(/^\[([^\]\n]{1,120})\]\s*(https?:\/\/\S+)$/);
  if (m2) return { title: m2[1].trim(), tag: "", url: m2[2] };
  return null;
}

/* 正文里的裸链接：可点，走站内预览（和微信点链接一致） */
function linkifyMsgText(text) {
  return String(text || "").split(/(https?:\/\/[^\s<>"'）)】]+)/g).map((part, i) => {
    if (i % 2 === 0) return esc(part);
    const url = part.replace(/[.,;:!?]+$/, "");
    return '<a class="msg-link" href="#" data-url="' + attrEsc(url) + '">' + esc(url) + "</a>";
  }).join("");
}

function bindMsgLinks(root) {
  root.querySelectorAll("a.msg-link").forEach((a) => {
    a.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const url = a.dataset.url || "";
      if (url) openLinkPreview(url, url);
    };
  });
}

function renderQqLocalMessage(it) {
  const parsed = it.parsed || {};
  const kind = parsed.kind || "text";
  const rawText = String(parsed.text || it.text || "");
  const media = Array.isArray(parsed.media) ? parsed.media : [];
  // 单行撤回/加群等系统通知：居中灰条（原来的 /撤回了一条消息/ 判定太宽，
  // 会把「消息：…」这类正文也误判成提示）
  if (kind !== "system" && kind !== "call" && !media.length
      && QQ_SYS_NOTICE_RE.test(rawText.trim())) {
    const sys = document.createElement("div");
    sys.className = "sys qq-recall-tip";
    sys.textContent = rawText.trim();
    return sys;
  }
  if (kind === "system" || kind === "call") {
    const sys = document.createElement("div");
    sys.className = "sys";
    sys.textContent = rawText;
    sys.setAttribute("data-nol10n", "");
    return sys;
  }
  // 解析不出内容的占位（全库剩 31 条，原始记录已被 QQ 清掉）：不显示脏数据
  if (rawText.trim() === "[消息]" && !media.length) {
    const blank = document.createElement("div");
    blank.style.display = "none";
    return blank;
  }
  // 拍一拍：QQ 里就是一行居中灰字「张三 拍了拍 我」
  if (kind === "poke") {
    const sys = document.createElement("div");
    sys.className = "sys qq-recall-tip";
    const who = it.is_sender ? "我" : (it.sender_name || "有人");
    sys.textContent = rawText.startsWith("拍了拍") ? `${who} ${rawText}` : rawText;
    sys.setAttribute("data-nol10n", "");
    return sys;
  }
  const wrap = document.createElement("div");
  wrap.className = "msg " + (it.is_sender ? "right" : "left");
  wrap.dataset.id = "qq-local-" + it.id;
  wrap.dataset.ts = String(it.ts || 0);
  // 自己的消息也用真实昵称/头像（与微信一致），不再是「我」+ 空白头像
  const selfName = state.selfQqName || it.sender_name || "我";
  const selfKey = state.selfQqUin || String(it.sender_uin || "");
  const senderKey = it.is_sender ? String(selfKey || "") : String(it.sender_uin || it.sender_uid || "");
  const senderName = it.is_sender ? selfName : (it.sender_name || "Q");
  const avaWrap = document.createElement("div");
  avaWrap.innerHTML = qqAvatarHtml("friend", senderKey, senderName);
  wrap.appendChild(avaWrap.firstElementChild);
  const col = document.createElement("div");
  col.className = "col";
  if (!it.is_sender || isGroupChat()) {
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = senderName;
    col.appendChild(who);
  }
  const b = document.createElement("div");
  b.className = "bubble" + (it.recalled ? " recalled" : "");
  b.setAttribute("data-nol10n", "");

  // 先算哪些媒体真的渲染出来了：只有渲染成功才去掉正文里的 [图片]/[语音] 占位
  const rendered = { image: 0, voice: 0, video: 0, file: 0, card: 0 };
  for (const m of media) {
    if (!m || !m.kind) continue;
    if (m.kind === "card" && (m.url || m.title)) rendered.card++;
    else if (m.kind === "file") rendered.file++;
    else if (qqLocalMediaSrc(m.path || "")) rendered[m.kind] = (rendered[m.kind] || 0) + 1;
  }
  let text = rawText.replace(/\[com\.tencent\.miniapp[^\]]*\]/gi, "[小程序]");
  if (rendered.image) text = text.replace(/\[图片\]/g, "");
  if (rendered.video) text = text.replace(/\[视频\]/g, "");
  if (rendered.voice) text = text.replace(/\[语音\]/g, "");
  if (rendered.file) text = text.split("\n").filter((x) => !/^\s*\[文件\]/.test(x)).join("\n");
  if (rendered.card) text = text.split("\n").filter((x) => !/^\s*\[[^\]]{1,20}\]\s/.test(x)).join("\n");
  text = text.replace(/\n{3,}/g, "\n\n").trim();

  // 整条正文就是一条分享链接（bilibili 等）：直接渲染成可点卡片
  const textCard = (!rendered.card && text) ? parseTextCard(text) : null;
  if (textCard) {
    text = "";
    rendered.card++;
  }

  if (text) {
    b.innerHTML = linkifyMsgText(text);
    bindMsgLinks(b);
  }
  if (it.recalled) {
    const tag = document.createElement("span");
    tag.className = "recall-badge";
    tag.textContent = "已撤回" + (it.recall_who ? " · " + it.recall_who : "");
    b.appendChild(tag);
  }

  const mbox = document.createElement("div");
  mbox.className = "qq-media";
  for (const m of media) {
    if (!m || !m.kind) continue;
    const src = qqLocalMediaSrc(m.path || "");
    if (m.kind === "image") {
      if (src) {
        const img = document.createElement("img");
        img.className = "qq-img";
        img.loading = "lazy";
        // 用缩略图（<200px，几百 KB）做展示源，点开才加载原图；
        // 首屏几十张图时差别很大
        img.src = src.replace("/api/qq/local/media?", "/api/qq/local/media/thumb?") + "&w=240";
        img.alt = m.filename || "图片";
        img.onclick = () => window.open("/api/qq/local/media?path=" +
          encodeURIComponent(m.path || ""), "_blank");
        mbox.appendChild(img);
      }
      // 未缓存到本机的图片：直接不显示（QQ 只缓存看过的图，占位和拉取失败反而碍眼）
    } else if (m.kind === "voice") {
      if (src) {
        const au = document.createElement("audio");
        au.className = "qq-audio";
        au.controls = true;
        au.preload = "none";
        au.src = src;
        mbox.appendChild(au);
      }
    } else if (m.kind === "video") {
      if (src) {
        const vd = document.createElement("video");
        vd.className = "qq-video";
        vd.controls = true;
        vd.preload = "none";
        vd.src = src;
        mbox.appendChild(vd);
      }
    } else if (m.kind === "file") {
      const a = document.createElement("a");
      a.className = "qq-file";
      a.textContent = "文件 · " + (m.filename || "未命名文件");
      if (src) {
        a.href = src;
        a.setAttribute("download", m.filename || "");
      }
      mbox.appendChild(a);
    } else if (m.kind === "card" && (m.url || m.title)) {
      // 分享卡片：点击走站内预览（和微信点链接卡片一致）
      const a = document.createElement("a");
      a.className = "qq-link-card";
      if (m.url) a.href = m.url;
      const tag = /miniapp/i.test(m.tag || "") ? "小程序" : (m.tag || "");
      a.innerHTML = (tag ? "<b>" + esc(tag) + "</b>" : "")
        + "<span>" + esc(m.title || m.url || "分享") + "</span>"
        + (m.desc ? "<small>" + esc(m.desc) + "</small>" : "");
      a.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (m.url) openLinkPreview(m.url, m.title || m.url);
      };
      mbox.appendChild(a);
    }
  }
  if (textCard) {
    const a = document.createElement("a");
    a.className = "qq-link-card";
    a.href = textCard.url;
    a.innerHTML = "<b>" + esc(textCard.tag || "链接") + "</b><span>" + esc(textCard.title) + "</span>";
    a.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      openLinkPreview(textCard.url, textCard.title);
    };
    mbox.appendChild(a);
  }
  if (mbox.children.length) b.appendChild(mbox);
  // 只有媒体的消息（图片/视频/卡片）不套气泡底色，和 QQ/微信一致
  if (!text && !it.recalled && mbox.children.length
      && rendered.image + rendered.video + rendered.card > 0
      && !rendered.voice && !rendered.file) {
    b.classList.add("media-only");
  }
  col.appendChild(b);

  const refer = parsed.refer && parsed.refer.text;
  if (refer && refer !== rawText) {
    const q = document.createElement("div");
    q.className = "qq-reply";
    q.textContent = refer;
    q.setAttribute("data-nol10n", "");
    col.appendChild(q);
  }
  wrap.appendChild(col);
  wrap.title = "右键加入引用栏";
  wrap.oncontextmenu = (e) => {
    e.preventDefault();
    addQuote({ id: "qq-local-" + it.id, ts: it.ts, who: it.sender_name,
               text: parsed.text || it.text, chat: state.qqConv,
               sender_name: it.sender_name,
               parsed: { text: parsed.text || it.text } });
    toast(T("quoteAdded"));
  };
  return wrap;
}

/* 左侧 QQ 会话选中态：列表不重建时也要跟着当前会话走，
   否则点进去没高亮、切走后又一直留着高亮。 */
function syncQqConvActive() {
  const conv = (state.qqConvKind === "local" && state.qqConv) ? state.qqConv : "";
  document.querySelectorAll("#qqConvList .qq-conv").forEach((el) => {
    el.classList.toggle("active", !!conv && el.dataset.conv === conv);
  });
}

async function openQqLocalConversation(conv, name, isGroup, focusId = 0) {
  // 消息请求先发出去（最慢的一步）；自己的 QQ 信息只在第一次进来时补，
  // 不阻塞首屏——以前是串行 await self → messages → AI 树，点一下要等一拍。
  const msgsPromise = api("/api/qq/local/messages?conv=" +
    encodeURIComponent(conv) + "&limit=80" +
    (focusId ? "&around=" + focusId : ""));
  if (!state.selfQqUin) {
    api("/api/qq/local/self").then((me) => {
      state.selfQqUin = me.uin || 0;
      state.selfQqName = me.name || "";
    }).catch(() => {});
  }
  state.qqConv = conv;
  state.qqConvKind = "local";
  state.qqConvName = name || conv;
  state.current = null;
  state.searchMode = false;
  stopLiveStream();
  if (typeof previewLink !== "undefined") previewLink = null;
  syncQqConvActive();
  showChatPane();
  $("#assistantPane").style.display = "none";
  $("#aiSessionsPanel").style.display = "none";
  document.querySelector(".chat-column").style.display = "flex";
  $("#drawer").style.display = "flex";
  $("#previewPane").style.display = "none";
  $("#msgList").style.display = "block";
  $("#chatTitle").textContent = name || conv;
  $("#chatMeta").textContent = "QQ 本地记录 · " + (isGroup ? "群聊" : "私聊");
  $("#msgs").innerHTML = `<div class="sys">来自本机 QQ 数据库的聊天记录（只读）</div>`;
  let oldest = 0;
  try {
    const d = await msgsPromise;
    const items = d.messages || [];
    oldest = items.length ? (items[0].ts || 0) : 0;
    const frag = document.createDocumentFragment();
    let lastTs = 0;
    for (const it of items) {
      if (needsDivider(lastTs, it.ts)) frag.appendChild(makeTimeDivider(it.ts));
      frag.appendChild(renderQqLocalMessage(it));
      lastTs = it.ts || lastTs;
    }
    $("#msgs").appendChild(frag);
    if (items.length >= 80) {
      const more = document.createElement("div");
      more.className = "load-more";
      more.textContent = "加载更早的 QQ 消息…";
      more.onclick = async () => {
        more.textContent = "加载中…";
        try {
          const prev = await api("/api/qq/local/messages?conv=" +
            encodeURIComponent(conv) + "&limit=120&before=" + oldest);
          const rows = prev.messages || [];
          if (!rows.length) { more.textContent = "没有更早的消息了"; return; }
          const frag2 = document.createDocumentFragment();
          let ts2 = 0;
          for (const it of rows) {
            if (needsDivider(ts2, it.ts)) frag2.appendChild(makeTimeDivider(it.ts));
            frag2.appendChild(renderQqLocalMessage(it));
            ts2 = it.ts || ts2;
          }
          more.after(frag2);
          oldest = rows[0].ts || oldest;
          more.textContent = rows.length >= 120 ? "加载更早的 QQ 消息…" : "没有更早的消息了";
          if (rows.length < 120) more.onclick = null;
        } catch (e) { more.textContent = "加载失败：" + e.message; }
      };
      $("#msgs").prepend(more);
    }
  } catch (e) {
    const err = document.createElement("div");
    err.className = "sys";
    err.textContent = "消息读取失败：" + e.message;
    $("#msgs").appendChild(err);
  }
  scrollMsgsToBottom();
  // 实时刷新：5 秒轮询比最新 id 更新的消息（后端实时同步已入库，这里只补显示）
  qqLiveStart();
  if (focusId) {
    const el = $("#msgs").querySelector(`[data-id="qq-local-${focusId}"]`);
    if (el) {
      el.scrollIntoView({ block: "center" });
      el.classList.add("qq-focus");
      setTimeout(() => el.classList.remove("qq-focus"), 2200);
    }
  }
  // 与微信 openChat 对齐：默认「仅当前会话内提问」，并载入该会话自己的 AI 会话
  $("#scopeToggle").checked = true;
  toggleDrawer(true);
  updateScopeBar();
  updateDrawerScope();
  // AI 会话树/历史与消息无关，别挡着消息上屏；失败也不影响浏览
  enterAiScope(conv, name || conv).catch(() => {});
}

/* ---------------------------------------------------------------- 平台切换钩子 */
function onPlatformChange(p) {
  // 停掉只属于微信通道的二维码轮询
  if (window._ocTimer) { clearInterval(window._ocTimer); window._ocTimer = null; }
  qqLiveStop();
  // 微信 / QQ 是两套 Agent：切换平台时清空当前 AI 会话，重新加载对应平台的会话树
  if (typeof stopLiveStream === "function") stopLiveStream();
  aiState.currentSession = null;
  aiState.currentScope = null;
  aiState.tree = [];
  aiState.scopeContexts = {};
  state.qaHistory = [];
  renderQA();
  loadAiTree().then(() => {
    renderAiTree();
    if (typeof syncAssistantScopeSelect === "function") syncAssistantScopeSelect();
  });
  if (typeof renderQqViewTabs === "function") renderQqViewTabs();
  if (typeof resetSettingsSectionForPlatform === "function") resetSettingsSectionForPlatform();
  state.qqConv = null;
  if (state.view === "settings") { showSettings(); toast(p === "qq" ? "已切换到 QQ 界面" : "已切换到微信界面"); return; }
  // 已打开的聊天按新平台重画（系统提示措辞、引用样式）
  const key = state.current && state.current.username;
  if (key && (state.msgs[key] || []).length && $("#msgs")) {
    const frag = document.createDocumentFragment();
    (state.msgs[key] || []).forEach(m => frag.appendChild(renderMsg(m)));
    $("#msgs").innerHTML = "";
    $("#msgs").appendChild(frag);
    scrollMsgsToBottom();
  } else if (!isQQ() && $("#msgs")) {
    // 从 QQ 会话切回微信：state.current 为空，上面不会重画，
    // 不清理的话 QQ 消息会残留成微信皮肤下的「幽灵聊天」。
    $("#msgs").innerHTML = "";
    $("#chatMain").style.display = "none";
    $("#chatEmpty").style.display = "flex";
    state.qqConv = null;
  }
  switchView(state.view || "chats");
  toast(p === "qq" ? "已切换到 QQ 界面 · 设置与微信互相独立" : "已切换到微信界面");
}
window.onPlatformChange = onPlatformChange;
