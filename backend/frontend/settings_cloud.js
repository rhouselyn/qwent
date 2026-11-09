/* 设置页 · 云端分流（微信 / QQ 两个界面共用的通用分组）
 *
 * 前端只是控制面：改这里的 UI 不会影响云端机器人的消息收发，因为云端
 * bundle 不包含 frontend/ 目录。开关与状态都走 /api/cloud/*。
 */
async function renderSettingsCloud(body) {
  let st = {}, cfg = {};
  try { st = await api("/api/cloud/status"); } catch (e) { st = { error: String(e) }; }
  try { cfg = (await api("/api/cloud/config")).config || {}; } catch (e) { /* 兜底空配置 */ }

  const on = !!cfg.enabled;
  const hasKey = !!cfg.encrypt_key;
  const val = (v) => attrEsc(String(v || ""));
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">CLOUD</span><h2>云端分流</h2>
      <p>把知识库副本同步到云服务器，并让 QQ / 微信机器人在服务器上常驻。电脑关机后手机端照常可用。</p></div>
      <span class="settings-status-pill ${on ? "success" : "neutral"}" id="cloudPill">${on ? "已开启" : "未开启"}</span></div>
    <div class="settings-grid"><section class="settings-card">
      <div class="settings-card-head"><span class="settings-card-icon">☁</span><div><h3>开关与接管</h3>
        <p>开启后本机机器人自动下线，避免同一账号双端互踢；电脑上的网页问答仍走本地实时库。</p></div></div>
      <div class="settings-switch-card">
        <label class="switch-row"><input type="checkbox" id="cloudEnabled" ${on ? "checked" : ""}><span class="switch-track"></span>
          <span class="sw-copy"><b>启用云端分流</b><span class="sw-hint">关闭后完全恢复本机运行，云端副本保留不动</span></span></label>
        <label class="switch-row"><input type="checkbox" id="cloudTakeover" ${cfg.takeover_bots !== false ? "checked" : ""}><span class="switch-track"></span>
          <span class="sw-copy"><b>机器人由服务器托管</b><span class="sw-hint">关掉则只同步数据，QQ / 微信机器人仍留在本机</span></span></label>
        <label class="switch-row"><input type="checkbox" id="cloudAuto" ${cfg.auto !== false ? "checked" : ""}><span class="switch-track"></span>
          <span class="sw-copy"><b>定时增量同步</b><span class="sw-hint">默认每 10 分钟一次，也可随时手动触发</span></span></label>
      </div>
    </section>
    <aside class="settings-side-card"><div class="settings-side-title">隐私提示</div>
      <p>开启后<strong>全量聊天记录（含与他人对话）会加密上传到你的服务器</strong>。README 里「数据不出本机」的承诺在此模式下不再适用。</p>
      <p style="margin-top:8px">传输与静态存储均使用 AES-GCM 加密，密钥只保存在本机与服务器；不上传媒体缓存、数据库密钥与模型权重。</p>
    </aside></div>

    <div class="settings-grid"><section class="settings-card">
      <div class="settings-card-head"><span class="settings-card-icon">⇄</span><div><h3>连接配置</h3>
        <p>默认走 SSH 直传，复用本机 SSH 密钥，无需对象存储、无需公网入站端口。</p></div></div>
      <div class="settings-form cloud-form">
        <div class="settings-field"><label for="cloudTransport">传输方式</label>
          <select id="cloudTransport">
            <option value="scp" ${cfg.transport === "scp" ? "selected" : ""}>SSH / SFTP（推荐）</option>
            <option value="file" ${cfg.transport === "file" ? "selected" : ""}>本地目录（联调）</option>
          </select></div>
        <div class="settings-field"><label for="cloudInterval">同步间隔（分钟）</label>
          <input id="cloudInterval" type="number" min="1" max="120" value="${val(cfg.interval_min || 10)}">
          <small>定时自动同步周期，也可随时手动触发。</small></div>
        <div class="settings-field"><label for="cloudEndpoint">服务器地址</label>
          <input id="cloudEndpoint" value="${val(cfg.endpoint)}" placeholder="152.136.96.212">
          <small>填 IP 或域名，两端都无需开放入站端口。</small></div>
        <div class="settings-field"><label for="cloudUser">SSH 用户</label>
          <input id="cloudUser" value="${val(cfg.scp_user || "root")}" placeholder="root"></div>
        <div class="settings-field cloud-span"><label for="cloudKey">私钥路径</label>
          <input id="cloudKey" value="${val(cfg.scp_key)}" placeholder="C:/Users/.../.ssh/tencent_key">
          <small>只保存路径，私钥内容不会上传或回显。</small></div>
        <div class="settings-field cloud-span"><label for="cloudDir">远程收件目录</label>
          <input id="cloudDir" value="${val(cfg.scp_dir || "/opt/qwent/inbox")}" placeholder="/opt/qwent/inbox">
          <small>与服务器 cloud.endpoint 指向同一目录。</small></div>
      </div>
      <div class="settings-key-note"><span class="settings-key-dot"></span>
        <span id="cloudKeyNote">${hasKey ? "加密密钥已自动配置" : "开启时将自动生成加密密钥"} · AES-GCM 加密传输与静态存储 · 两端共用同一把，无需手动填写。</span></div>
      <div class="settings-actions">
        <button id="cloudSave">保存配置</button>
        <button id="cloudTest" class="ghost">测试连接</button>
        <button id="cloudSync" class="ghost">立即增量同步</button>
        <button id="cloudBaseline" class="ghost">生成全量基线</button>
        <span id="cloudActionMsg" class="msg" aria-live="polite"></span>
      </div>
    </section>
    <aside class="settings-side-card"><div class="settings-side-title">运行状态</div>
      <div id="cloudStatusBox"></div>
      <button class="mini ghost" id="cloudRefresh" style="margin-top:12px">刷新状态</button>
    </aside></div>`;

  const msg = (t, ok) => {
    const el = $("#cloudActionMsg");
    if (!el) return;
    el.textContent = t;
    el.className = "msg" + (ok === true ? " ok" : ok === false ? " err" : "");
  };
  const renderStatus = (s) => {
    const box = $("#cloudStatusBox");
    if (!box) return;
    const rows = [
      ["运行侧", s.side === "cloud" ? "云服务器" : "本机"],
      ["机器人归属", s.owned_by === "cloud" ? "服务器" : "本机"],
      ["加密", hasKey ? "AES-GCM · 已配置" : "开启时自动生成"],
      ["最后同步", s.last_ok || "尚未同步"],
      ["已发送序号", s.last_sent_seq ?? s.applied_seq ?? 0],
      ["本轮变更", s.last_rows ?? 0],
      ["数据纪元", s.epoch || "—"],
      ["基线时间", s.baseline_at ? new Date(s.baseline_at * 1000).toLocaleString() : "—"],
      ["最近错误", s.error || s.last_error || "无"],
    ];
    box.innerHTML = rows.map(([k, v]) =>
      `<div class="settings-kv"><span>${esc(String(k))}</span><b>${esc(String(v))}</b></div>`).join("");
  };
  renderStatus(st);

  // 不提交 encrypt_key：密钥由后端首次开启时生成，空值或掩码都不能覆盖已有密钥
  const payload = () => ({
    enabled: $("#cloudEnabled").checked,
    takeover_bots: $("#cloudTakeover").checked,
    auto: $("#cloudAuto").checked,
    transport: $("#cloudTransport").value,
    endpoint: $("#cloudEndpoint").value.trim(),
    scp_user: $("#cloudUser").value.trim(),
    scp_key: $("#cloudKey").value.trim(),
    scp_dir: $("#cloudDir").value.trim(),
    interval_min: Math.max(1, parseInt($("#cloudInterval").value, 10) || 10),
  });

  const save = async () => {
    const p = payload();
    if (p.enabled && !cfg._confirmed && !on) {
      const ok = window.confirm(
        "开启后，全量聊天记录（包含与他人的对话）将加密上传到你的云服务器。\n" +
        "README 中「数据不出本机」的承诺在此模式下不再适用。确认开启？");
      if (!ok) { $("#cloudEnabled").checked = false; return false; }
      cfg._confirmed = true;
    }
    try {
      const r = await api("/api/cloud/config", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(p) });
      cfg = { ...cfg, ...(r.config || {}) };
      $("#cloudPill").textContent = p.enabled ? "已开启" : "未开启";
      $("#cloudPill").className = "settings-status-pill " + (p.enabled ? "success" : "neutral");
      const note = $("#cloudKeyNote");
      if (note && r.config && r.config.encrypt_key) {
        note.textContent = "加密密钥已自动配置 · AES-GCM 加密传输与静态存储 · 两端共用同一把，无需手动填写。";
      }
      msg(r.ok ? "配置已保存" : "保存失败", !!r.ok);
      return !!r.ok;
    } catch (e) { msg("保存失败：" + e, false); return false; }
  };

  $("#cloudEnabled").onchange = async () => {
    const el = $("#cloudEnabled");
    if (el.checked && !(await save())) el.checked = false;
    else if (!el.checked) await save();
  };
  $("#cloudSave").onclick = save;
  $("#cloudTest").onclick = async () => {
    msg("测试中…");
    if (!(await save())) return;
    try { const r = await api("/api/cloud/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      msg(r.ok ? "连接正常：" + (r.target || r.root || "") : "失败：" + (r.error || "未知错误"), !!r.ok); }
    catch (e) { msg("测试失败：" + e, false); }
  };
  $("#cloudSync").onclick = async () => {
    msg("正在打包并上传…");
    if (!(await save())) return;
    try { const r = await api("/api/cloud/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      msg(r.ok ? `同步完成：${r.rows ?? 0} 条变更` : "同步失败：" + (r.error || ""), !!r.ok); }
    catch (e) { msg("同步失败：" + e, false); }
  };
  $("#cloudBaseline").onclick = async () => {
    if (!window.confirm("全量基线约 2.5–3 GB，会占用磁盘并上传数分钟。确认开始？")) return;
    msg("正在生成全量基线，请勿关闭程序…");
    if (!(await save())) return;
    try { const r = await api("/api/cloud/baseline", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      msg(r.ok ? `基线完成：${r.parts} 个分片` : "基线失败：" + (r.error || ""), !!r.ok); }
    catch (e) { msg("基线失败：" + e, false); }
  };
  $("#cloudRefresh").onclick = async () => {
    try { renderStatus(await api("/api/cloud/status")); } catch (e) { msg(String(e), false); }
  };
}
