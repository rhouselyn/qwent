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
  $("#chatMain").style.display = "none";
  $("#assistantPane").style.display = "none";
  $("#aiSessionsPanel").style.display = "none";
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
  const [s, a, rt] = await Promise.all([
    api("/api/status"), api("/api/sync/auto"), api("/api/sync/realtime")]);
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
        （密钥账号级稳定，服务会定期 HMAC 实测；失效时自动扫运行中微信内存重取，免退出微信）</div>
      <div style="margin-top:6px"><span class="k">数据位置</span>data/knowledge.db 与
        data/decrypted（全部在本机）</div>
    </div>`;
}

