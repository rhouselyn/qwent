/* 流式问答：问题立即上屏、实时显示工具调用与引用、答案逐字输出 */
const streamState = { busy: false, controller: null };

/* 正文 [n] → 可点击引用：消息跳原文、公众号打开正文、文件直接打开源文件预览 */
function bindRefLinks(container, refs) {
  container.querySelectorAll(".md-ref").forEach(el => {
    el.style.cssText = "color:#3b82f6;cursor:pointer;text-decoration:underline;font-size:0.9em";
    el.onclick = () => {
      const n = +el.dataset.ref;
      const r = (refs || []).find(x => x.n === n);
      if (r) {
        if (r.kind === "moment" && r.tid) return openMomentByTid(r.tid);
        if (r.kind === "link" && r.url) return openLinkPreview(r.url, r.chat_name);
        if (r.kind === "file") return openFilePreview({
          chat: r.chat, id: r.local_id || 0, ts: r.ts || 0,
          title: r.title || r.snippet || "", chat_name: r.chat_name || "", size: r.size || 0 });
        return jumpToRef(r);
      }
      // 快速回答按消息 id 标注且无 refs 映射 → 直接按 id 跳原文
      if (n > 1000) jumpToRef({ id: n });
    };
  });
}

/* 文件类来源（PDF/图片/Word 等）：以源文件 chips 附在答案后面，点击打开/跳转源文件 */
function attachSourceFiles(turnEl, refs) {
  const files = (refs || []).filter(r => r && r.kind === "file");
  const old = turnEl.querySelector(".src-files");
  if (old) old.remove();
  if (!files.length) return;
  const wrap = document.createElement("div");
  wrap.className = "src-files";
  for (const r of files) {
    const chip = document.createElement("span");
    chip.className = "src-chip";
    const title = r.title || r.snippet || "文件";
    const isImg = /\.(png|jpe?g|gif|webp|bmp)$/i.test(title);
    chip.textContent = `${isImg ? "🖼" : "📄"} ${title}${r.size ? " · " + fmtSize(r.size) : ""}`;
    chip.title = "点击打开源文件";
    chip.onclick = () => openFilePreview({
      chat: r.chat, id: r.local_id || 0, ts: r.ts || 0,
      title, chat_name: r.chat_name || "", size: r.size || 0 });
    wrap.appendChild(chip);
  }
  const a = turnEl.querySelector(".a");
  if (a && a.parentNode) a.parentNode.insertBefore(wrap, a.nextSibling);
  else turnEl.appendChild(wrap);
}

/* 可展开的「引用来源」合集：每条都可点击（消息跳原文 / 朋友圈打开详情 / 公众号打开正文 / 文件打开源文件） */
function buildRefsWrap(refs) {
  if (!refs || !refs.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "refs-wrap";
  wrap.innerHTML = `<div class="refs-toggle">引用来源（${refs.length}） ▸</div>`;
  const list = document.createElement("div");
  list.className = "refs";
  list.style.display = "none";
  for (const r of refs) {
    const item = document.createElement("div");
    item.className = "ref-item";
    const isLink = r.kind === "link" && r.url;
    const isFile = r.kind === "file";
    const isMoment = r.kind === "moment" && r.tid;
    const label = isFile ? (r.title || r.snippet || "文件")
                : isLink ? (r.chat_name || "网页文章") : (r.chat_name || "");
    item.innerHTML = `<div class="ref-head">
        <span class="ref-chip">[${r.n}] ${esc(isMoment ? "朋友圈 · " + label : label)} ${r.ts ? fmtTime(r.ts) : ""}</span>
        <span class="ref-jump">${isMoment ? "查看朋友圈 ↗" : isLink ? "打开原文 ↗" : isFile ? "打开文件 ↗" : "跳转原文 ↗"}</span></div>`;
    item.querySelector(".ref-jump").onclick = () => {
      if (isMoment) openMomentByTid(r.tid);
      else if (isLink) openLinkPreview(r.url, r.chat_name);
      else if (isFile) openFilePreview({ chat: r.chat, id: r.local_id || 0, ts: r.ts || 0,
        title: r.title || r.snippet || "", chat_name: r.chat_name || "", size: r.size || 0 });
      else jumpToRef(r);
    };
    if (r.ctx && r.ctx.length) {
      const ctxBox = document.createElement("div");
      ctxBox.className = "ref-ctx";
      ctxBox.style.display = "none";
      ctxBox.innerHTML = r.ctx.map(x =>
        `<div class="ctx-line ${x.is_hit ? "hit" : ""}">
           <span class="ctx-who">${esc(x.who)}</span>
           <span class="ctx-time">${fmtTime(x.ts)}</span>
           <span class="ctx-text">${esc(x.text)}</span></div>`).join("");
      const tg = document.createElement("a");
      tg.className = "ref-ctx-toggle";
      tg.textContent = `上下文 ${r.ctx.length} 条 ▸`;
      tg.onclick = () => {
        const open = ctxBox.style.display !== "none";
        ctxBox.style.display = open ? "none" : "block";
        tg.textContent = `上下文 ${r.ctx.length} 条 ${open ? "▸" : "▾"}`;
      };
      item.appendChild(tg); item.appendChild(ctxBox);
    }
    list.appendChild(item);
  }
  wrap.querySelector(".refs-toggle").onclick = () => {
    const open = list.style.display !== "none";
    list.style.display = open ? "none" : "block";
    wrap.querySelector(".refs-toggle").textContent =
      `引用来源（${refs.length}） ${open ? "▸" : "▾"}`;
  };
  wrap.appendChild(list);
  return wrap;
}

/* 把引用合集插到答案（和文件 chips）之后 */
function insertRefsWrap(turnEl, refs) {
  const old = turnEl.querySelector(".refs-wrap");
  if (old) old.remove();
  const wrap = buildRefsWrap(refs);
  if (!wrap) return;
  const anchor = turnEl.querySelector(".src-files") || turnEl.querySelector(".a");
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(wrap, anchor.nextSibling);
  else turnEl.appendChild(wrap);
}

/* refs 到达：绑定 [n] 链接 + 文件源 chips + 可展开引用合集 */
function renderRefsInto(turn, refs) {
  attachSourceFiles(turn, refs);
  const a = turn.querySelector(".a");
  if (a) bindRefLinks(a, refs);
  insertRefsWrap(turn, refs);
}


function qaBox() { return isAssistant() ? $("#assistantConv") : $("#qaList"); }

function makeTurnShell(question) {
  const box = qaBox();
  const turn = document.createElement("div");
  turn.className = "qa-turn streaming";
    const askTime = new Date().toLocaleTimeString("zh-CN", {hour:"2-digit", minute:"2-digit"});
    turn.innerHTML = `
    <div class="qa-head">
      <span class="mode">思考中<span class="dots"><i></i><i></i><i></i></span></span>
      <span class="qa-time">${askTime}</span>
      <span class="qa-ops"><a class="op-stop" title="停止">■ 停止</a></span>
    </div>
    <div class="q">${esc(question)}</div>
    <div class="live" style="display:none"></div>
    <div class="a md"></div>`;
  box.appendChild(turn);
  box.scrollTop = box.scrollHeight;
  return turn;
}

function setLive(turn, text) {
  const live = turn.querySelector(".live");
  if (!text) { live.style.display = "none"; return; }
  live.style.display = "block";
  live.innerHTML = `<span class="live-spin"></span>${esc(text)}`;
  qaBox().scrollTop = qaBox().scrollHeight;
}

async function askStream(question) {
  if (streamState.busy) { toast("上一条还在回答中"); return; }
  // 文件预览中勾选范围 → 针对该文件提问；文章预览 → 针对本文提问；取消勾选 → 全局检索
  const scopeOn = $("#scopeToggle") ? $("#scopeToggle").checked : true;
  const fileCtx = (previewFile && scopeOn)
    ? { chat: previewFile.chat, local_id: previewFile.id, ts: previewFile.ts,
        name: previewFile.title }
    : null;
  const linkCtx = (!fileCtx && typeof previewLink !== "undefined" && previewLink && scopeOn)
    ? { url: previewLink.url, title: previewLink.title }
    : null;
  const chat = (fileCtx || linkCtx) ? null : currentAskScope();
  const sc = (typeof aiScopeForContext === "function") ? aiScopeForContext() : null;
  // 发问前就把会话建好并放进 tree：否则要等流结束才出现，从其它窗口跳回来是空的
  if (!aiState.currentSession && typeof api === "function") {
    try {
      const r = await api("/api/ai/sessions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scope: sc ? sc.key : (chat || "assistant"),
          name: question.slice(0, 24),
        }),
      });
      aiState.currentSession = r.id;
      await loadAiTree(); renderAiTree();
    } catch {}
  }
  const turn = makeTurnShell(question);
  const modeEl = turn.querySelector(".mode");
  const answerEl = turn.querySelector(".a");
  let answerText = "";
  let refs = [];
  let stopped = false;

  turn.querySelector(".op-stop").onclick = () => {
    stopped = true;
    if (streamState.controller) streamState.controller.abort();
  };

  streamState.busy = true;
  streamState.controller = new AbortController();
  $("#sendBtn") && ($("#sendBtn").disabled = true);
  $("#assistantSend") && ($("#assistantSend").disabled = true);

  try {
    const resp = await fetch("/api/ask/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question, chat, file: fileCtx, link: linkCtx,
        scope: sc ? sc.key : (chat || "assistant"),
        scope_name: sc ? sc.name : "",
        session_id: aiState.currentSession,
        thinking: $("#thinkToggle") ? $("#thinkToggle").checked : undefined,
        explorer: $("#exploreToggle") ? $("#exploreToggle").checked : false,
        quoted: (fileCtx || linkCtx) ? [] : state.quoted.map(q => q.id),
        history: state.qaHistory.slice(-3).map(t => ({ q: t.question, a: t.answer })),
      }),
      signal: streamState.controller.signal,
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        let ev;
        try { ev = JSON.parse(line.slice(5)); } catch { continue; }
        switch (ev.type) {
          case "status":
            setLive(turn, ev.text);
            modeEl.innerHTML = `${esc(ev.text)}<span class="dots"><i></i><i></i><i></i></span>`;
            break;
          case "tool":
            setLive(turn, `正在调用：${ev.tool}`);
            break;
          case "tool_result":
            setLive(turn, `${ev.tool} → ${ev.summary}（已收集 ${ev.refs || 0} 条引用）`);
            break;
          case "reset":
            answerText = "";
            answerEl.innerHTML = "";
            break;
          case "refs":
            refs = ev.refs || [];
            renderRefsInto(turn, refs);
            break;
          case "delta":
            if (!answerText) { modeEl.textContent = "回答"; setLive(turn, ""); }
            answerText += ev.text;
            answerEl.innerHTML = mdToHtml(answerText);
            if (typeof bindRefLinks === "function") bindRefLinks(answerEl, refs);
            qaBox().scrollTop = qaBox().scrollHeight;
            break;
          case "done":
            modeEl.textContent = ev.mode === "agent" ? "AI 回答"
              : ev.mode === "link" ? "本文问答"
              : ev.mode === "file" ? "文件问答"
              : ev.mode === "retrieval" ? "检索结果" : "快速回答";
            setLive(turn, "");
            if (typeof stripRefFooter === "function") {
              // 快速回答以后端完整答案为准：流式增量可能缺开头/短答案整段未发
              if (ev.answer) answerText = ev.answer;
              if (typeof stripDsml === "function") answerText = stripDsml(answerText);
              answerText = stripRefFooter(answerText);
              if (!answerText && ev.mode === "agent") {
                answerText = "（该轮回答是模型输出乱码，已自动清理）";
                modeEl.textContent = "回答异常";
              }
              answerEl.innerHTML = mdToHtml(answerText);
              bindRefLinks(answerEl, refs);
              attachSourceFiles(turn, refs);
            }
            if (ev.session_id) aiState.currentSession = ev.session_id;
            break;
          case "error":
            modeEl.textContent = "出错了";
            answerEl.innerHTML = mdToHtml("**请求失败**：" + (ev.message || ""));
            break;
        }
      }
    }
  } catch (e) {
    if (!stopped) {
      modeEl.textContent = "出错了";
      answerEl.innerHTML = mdToHtml("**请求失败**：" + e.message);
    } else {
      modeEl.textContent = "已停止";
    }
  } finally {
    streamState.busy = false;
    streamState.controller = null;
    $("#sendBtn") && ($("#sendBtn").disabled = false);
    $("#assistantSend") && ($("#assistantSend").disabled = false);
    if (!stopped) {
      turn.classList.remove("streaming");
      // 与服务端对齐，拿到记录 id 便于折叠/隐藏
      state.qaHistory.push({ question, answer: answerText, refs, mode: "stream" });
      if (state.quoted.length) { state.quoted = []; renderQuoteBar(); }
      setTimeout(async () => {
        if (aiState.currentSession) {
          try {
            const d = await api(`/api/ai/session/${aiState.currentSession}`);
            state.qaHistory = d.turns;
            await loadAiTree(); renderAiTree();
          } catch {}
        }
      }, 300);
    }
  }
}
