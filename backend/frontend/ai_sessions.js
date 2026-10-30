/* ---------- AI 会话（多会话）管理，独立文件便于维护 ---------- */
const aiState = {
  tree: [],            // [{scope,name,is_group,sessions:[{id,title,ts,count}]}]
  openScopes: {},      // scope -> bool
  currentSession: null, // 当前展示的 session id
  currentScope: null,   // 'assistant' 或 chat username
};

function scopeKeyForCurrent() {
  if (typeof aiScopeForContext === "function") return aiScopeForContext().key;
  if (!state.current) return "assistant";
  return state.current.username === "__assistant__" ? "assistant" : state.current.username;
}

async function loadAiTree() {
  try { aiState.tree = await api("/api/ai/sessions"); } catch { aiState.tree = []; }
}

function renderAiTree() {
  // 左栏（AI 视图）与右侧面板（AI 助手三栏）各渲染一份
  [document.querySelector("#aiSessionList"), document.querySelector("#aiTreeInPanel")]
    .forEach((box) => box && renderAiTreeInto(box));
}

function renderAiTreeInto(box) {
  box.innerHTML = "";
  if (!aiState.tree.length) {
    box.innerHTML = `<div class="qa-empty">还没有 AI 会话。<br>在任意会话里提问，或点「＋ 新会话」。</div>`;
    return;
  }
  for (const g of aiState.tree) {
    const wrap = document.createElement("div");
    wrap.className = "ai-group";
    const open = aiState.openScopes[g.scope] ?? (g.scope === "assistant");
    wrap.innerHTML = `
      <div class="ai-group-head">
        <span class="caret">${open ? "▾" : "▸"}</span>
        <span>${esc(g.name)}</span>
        ${g.scope === "assistant" ? '<span class="tag">全局</span>' : (g.is_group ? '<span class="tag">群</span>' : "")}
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
      d.querySelector(".t").onclick = () => openAiSession(g, s);
      d.querySelector(".n").onclick = () => openAiSession(g, s);
      d.querySelector(".del").onclick = async (e) => {
        e.stopPropagation();
        await api(`/api/ai/session/${s.id}/delete`, { method: "POST" });
        if (aiState.currentSession === s.id) aiState.currentSession = null;
        await loadAiTree(); renderAiTree();
        toast("已删除对话");
      };
      list.appendChild(d);
    }
    wrap.querySelector(".ai-group-head").onclick = () => {
      aiState.openScopes[g.scope] = !open;
      renderAiTree();
    };
    box.appendChild(wrap);
  }
}

async function openAiSession(group, sess) {
  aiState.currentSession = sess.id;
  aiState.currentScope = group.scope;
  try {
    const d = await api(`/api/ai/session/${sess.id}`);
    state.qaHistory = d.turns;
  } catch { state.qaHistory = []; }

  // 保持当前视图（AI 视图或聊天视图），只换中间对话内容
  if (isAssistant() || $("#aiSessionsPanel").style.display !== "none") {
    state.current = { username: "__assistant__", name: "AI 助手", is_group: false };
    $("#assistantTitle").textContent = "✦ " +
      (group.scope === "assistant" ? "全局助手" : group.name);
    $("#assistantMeta").textContent = `${state.qaHistory.length} 轮`;
    renderQA();
    renderAiTree();
    return;
  }
  $("#drawerScope").textContent = `范围：${group.scope === "assistant" ? "全局" : group.name}`;
  renderQA();
  renderAiTree();
}

/* 载入指定会话的问答记录（不传则用该范围最新） */
async function loadSessionTurns(sid) {
  if (!sid) { state.qaHistory = []; renderQA(); return; }
  try {
    const d = await api(`/api/ai/session/${sid}`);
    state.qaHistory = d.turns;
  } catch { state.qaHistory = []; }
  renderQA();
}

/* 在当前范围新建会话 */
async function newAiSession() {
  const sc = (typeof aiScopeForContext === "function") ? aiScopeForContext() : null;
  const scope = sc ? sc.key : scopeKeyForCurrent();
  const r = await api("/api/ai/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, name: sc ? sc.name : "" }),
  });
  aiState.currentSession = r.id;
  state.qaHistory = [];
  renderQA();
  await loadAiTree(); renderAiTree();
  toast("已新建会话，可以开始提问");
}

/* 绑定：新会话按钮 + 左栏 AI 视图 */
$("#newSessionBtn").onclick = () => newAiSession();
bindAssistantComposer();
