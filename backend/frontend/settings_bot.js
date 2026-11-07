/* 设置页 · 机器人分组（按平台完全独立）
 * 微信模式：只有微信通道（ClawBot / OpenClaw / Agent 入口），不出现任何 QQ 字段。
 * QQ  模式：只有 QQ 机器人（qqbot 凭据 / 菜单 / 消息），不出现微信通道。
 * 通用分组仍在 app.js；两边的功能照搬，配置和界面各自独立。
 */

async function renderSettingsBot(body) {
  return isQQ() ? renderSettingsQqBot(body) : renderSettingsWxBot(body);
}

/* ---------------------------------------------------------------- 微信通道 */
async function renderSettingsWxBot(body) {
  if (window._ocTimer) { clearInterval(window._ocTimer); window._ocTimer = null; }
  let oc, ag, hp;
  try {
    [oc, ag, hp] = await Promise.all([
      api("/api/openclaw/status"),
      api("/api/wxagent/config"),
      api("/api/wxagent/help"),
    ]);
  } catch (e) {
    body.innerHTML = `<div class="qa-empty">加载失败: ${esc(e.message)}</div>`;
    return;
  }
  const helpPre = esc(hp.help || "");
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">WECHAT CHANNEL</span>
      <h2>微信通道</h2><p>扫码接入 ClawBot，在微信里直接向本机会话记录提问。QQ 机器人请在 QQ 模式的设置里配置。</p></div>
      <span class="settings-status-pill ${oc.enabled ? "success" : "neutral"}">${oc.enabled ? "已启用" : "未启用"}</span></div>
    <div class="settings-grid"><div class="settings-stack">

      <section class="settings-card" id="ocCard">
        <div class="settings-card-head"><span class="settings-card-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="9" cy="8" rx="6.2" ry="5" fill="#07c160"/><ellipse cx="15.2" cy="14" rx="6.2" ry="5" fill="#3ddc84"/><circle cx="6.8" cy="7.4" r="1" fill="#fff"/><circle cx="11.2" cy="7.4" r="1" fill="#fff"/><circle cx="13.2" cy="13.4" r="1" fill="#fff"/><circle cx="17.2" cy="13.4" r="1" fill="#fff"/></svg></span><div><h3>ClawBot 扫码接入</h3><p>点「扫码登录」→ 手机微信扫码确认 → 微信里直接发消息提问。</p></div></div>
        <label class="switch-row"><input type="checkbox" id="ocEnabled" ${oc.enabled ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>启用微信通道</b><span class="sw-hint">关闭后停止网关，微信里不再回复</span></span></label>
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
        <div class="settings-switch-card" style="margin-top:12px">
          <label class="switch-row"><input type="checkbox" id="wxExplorer" ${ag.default_explorer ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>默认探索模式</b><span class="sw-hint">多轮检索，回答更全但更慢</span></span></label>
          <label class="switch-row"><input type="checkbox" id="wxThinking" ${ag.default_thinking ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>默认深度思考</b><span class="sw-hint">使用推理模型</span></span></label>
        </div>
        <div class="settings-actions" style="margin-top:10px"><button id="wxSave">保存设置</button><span id="wxMsg" class="msg" aria-live="polite"></span></div>
      </section>


      <section class="settings-card">
        <div class="settings-card-head"><span class="settings-card-icon">?</span><div><h3>/帮助 里会显示什么</h3><p>在微信 ClawBot 里发 /帮助（或 1）看到的中文说明，含功能、现状与指引。</p></div></div>
        <details class="settings-help-details"><summary>展开预览</summary><pre data-nol10n>${helpPre}</pre></details>
      </section>

    </div>
    <aside class="settings-side-card"><div class="settings-side-title">接入步骤</div>
      <ol style="margin:0;padding-left:18px;font-size:12px;line-height:1.85">
        <li>在本页打开「启用微信通道」。</li>
        <li>点「扫码登录」，用手机微信扫码并在手机上确认。</li>
        <li>登录成功后助手自动重启网关，微信里会出现 ClawBot 联系人。</li>
        <li>直接发问题，或发 /帮助 看功能与当前状态。</li>
      </ol>
      <div class="settings-side-title" style="margin-top:12px">常用指令</div>
      <p style="font-size:12px;line-height:1.9">/1 帮助 · /2 快速 · /3 探索 · /4 思考 · /5 关思考<br>/6 新对话 · /7 暂停 · /8 继续 · /9 状态<br>斜杠可以省略，例如直接发「状态」。</p>
      <p style="margin-top:8px">登录凭据保存在本机，重启软件或电脑都不需要重新扫码。</p>
    </aside></div>`;

  $("#ocEnabled").onchange = async () => {
    const want = $("#ocEnabled").checked;
    $("#ocEnabled").disabled = true;
    $("#ocStatus").textContent = want ? "正在启用（重启网关约 40 秒）…" : "正在停止…";
    try {
      await api("/api/openclaw/toggle", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: want }) });
      toast(want ? "微信通道已启用" : "微信通道已关闭");
    } catch (e) { toast("操作失败：" + e.message); $("#ocEnabled").checked = !want; }
    finally { $("#ocEnabled").disabled = false; setTimeout(() => showSettings("wxbot"), 800); }
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
    $("#ocLogin").disabled = !s.enabled;
    $("#ocGateway").disabled = !s.enabled;
    $("#ocLoginStop").style.display = s.login_running ? "" : "none";
    const qrUrl = s.qr_url || "";
    const wrap = $("#ocQrWrap"), img = $("#ocQrImg");
    if (s.enabled && s.login_running && qrUrl) {
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
    catch (e) { if (document.querySelector("#ocStatus")) ocStatus.textContent = "状态获取失败: " + e.message; }
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

  const saveWx = async (regen) => {
    const msg = $("#wxMsg");
    $("#wxSave").disabled = true;
    msg.textContent = "保存中…"; msg.className = "msg";
    try {
      await api("/api/wxagent/config", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: $("#wxModel").value,
          default_explorer: $("#wxExplorer").checked,
          default_thinking: $("#wxThinking").checked,
          regenerate: !!regen,
        }) });
      msg.textContent = regen ? "密钥已重新生成并写入配置" : "已保存";
      msg.className = "msg ok";
      setTimeout(() => showSettings("wxbot"), 700);
    } catch (e) {
      msg.textContent = "保存失败: " + e.message; msg.className = "msg err";
      $("#wxSave").disabled = false;
    }
  };
  $("#wxSave").onclick = () => saveWx(false);
}

/* ---------------------------------------------------------------- QQ 机器人 */
async function renderSettingsQqBot(body) {
  let q, hp;
  try {
    [q, hp] = await Promise.all([
      api("/api/qqbot/status"), api("/api/qqagent/help")]);
  } catch (e) {
    body.innerHTML = `<div class="qa-empty">加载失败: ${esc(e.message)}</div>`;
    return;
  }
  const qqState = !q.enabled ? "未启用" : q.error ? "异常" : q.connected ? "已连接" : q.running ? "连接中…" : "已停止";
  const helpPre = esc(hp.help || "");
  body.innerHTML = `
    <div class="settings-page-head"><div><span class="settings-eyebrow">QQ BOT</span>
      <h2>QQ 机器人</h2><p>QQ 机器人通过官方 WebSocket 网关收消息、REST 被动回复，无需公网 IP；聊天记录/联系人请在「QQ 本地记录」导入。</p></div>
      <span class="settings-status-pill ${q.connected ? "success" : q.enabled ? "warning" : "neutral"}">${qqState}</span></div>
    <div class="settings-grid"><div class="settings-stack">

      <section class="settings-card" id="qqCard">
        <div class="settings-card-head"><span class="settings-card-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="12.5" rx="6" ry="7.6" fill="#12b7f5"/><ellipse cx="12" cy="14.6" rx="3.8" ry="5" fill="#fff"/><circle cx="9.6" cy="7" r="1.2" fill="#fff"/><circle cx="14.4" cy="7" r="1.2" fill="#fff"/><circle cx="9.6" cy="7" r=".5" fill="#0b2b44"/><circle cx="14.4" cy="7" r=".5" fill="#0b2b44"/><ellipse cx="12" cy="8.9" rx=".95" ry=".6" fill="#ff9f1a"/></svg></span><div><h3>连接与凭据</h3><p>在 q.qq.com 创建机器人后填入 AppID / AppSecret。</p></div></div>
        <label class="switch-row"><input type="checkbox" id="qqEnabled" ${q.enabled ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>启用 QQ 机器人</b><span class="sw-hint">关闭后断开网关，不再回复</span></span></label>
        <div class="settings-form" style="margin-top:14px">
          <div class="settings-field"><label for="qqAppId">AppID</label><input id="qqAppId" value="${attrEsc(q.app_id || "")}" placeholder="1020xxxxxx"></div>
          <div class="settings-field"><label for="qqSecret">AppSecret</label><input id="qqSecret" type="password" autocomplete="off" placeholder="${q.has_secret ? attrEsc("已配置 " + q.secret_masked + "（留空则不修改）") : "机器人密钥"}"><small>只保存在本机 config.json，不会上传。</small></div>
          <div class="settings-field"><label for="qqAllow">允许的用户 OpenID（可选）</label><input id="qqAllow" value="${attrEsc((q.allow_openids || []).join(", "))}" placeholder="留空 = 不限制；多个用逗号分隔"><small>白名单为空时只响应能私聊到机器人的用户。</small></div>
        </div>
        <div class="settings-switch-card">
          <label class="switch-row"><input type="checkbox" id="qqGroup" ${q.group_reply ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>响应群聊 @</b><span class="sw-hint">默认只响应私聊</span></span></label>
          <label class="switch-row"><input type="checkbox" id="qqCite" ${q.citations ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>附上引用来源</b><span class="sw-hint">回答末尾附本机记录出处</span></span></label>
          <label class="switch-row"><input type="checkbox" id="qqExplorer" ${q.default_explorer ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>默认探索模式</b><span class="sw-hint">多轮检索</span></span></label>
          <label class="switch-row"><input type="checkbox" id="qqThinking" ${q.default_thinking ? "checked" : ""}><span class="switch-track"></span><span class="sw-copy"><b>默认深度思考</b><span class="sw-hint">推理模型</span></span></label>
        </div>
        <div class="settings-actions"><button id="qqSave">保存并应用</button><button id="qqMenu" class="ghost">重新推送菜单</button><span id="qqMsg" class="msg" aria-live="polite"></span></div>
        <div class="settings-note">状态：${qqState}${q.error ? " · " + esc(q.error) : ""}${q.menu ? " · 菜单：" + (q.menu === "ok" ? "已自动配置" : q.menu === "pushing" ? "推送中…" : "推送失败" + (q.menu_error ? "（" + esc(q.menu_error) + "）" : "")) : ""}</div>
      </section>

      <section class="settings-card">
        <div class="settings-card-head"><span class="settings-card-icon">☰</span><div><h3>菜单与指令</h3><p>QQ 底部菜单与命令行功能一一对应，输入简单：/1~9 或中文短名都行。</p></div></div>
        <details class="settings-help-details"><summary>展开 /帮助 预览</summary><pre data-nol10n>${helpPre}</pre></details>
      </section>

    </div>
    <aside class="settings-side-card"><div class="settings-side-title">接入步骤</div>
      <ol style="margin:0;padding-left:18px;font-size:12px;line-height:1.85">
        <li>打开 <a href="https://q.qq.com" target="_blank" rel="noreferrer">q.qq.com</a>，创建或选择机器人。</li>
        <li>「开发设置」复制 AppID 与 AppSecret。</li>
        <li>「服务接入」选 WebSocket，不要选 Webhook。</li>
        <li>「事件订阅」勾选 C2C_MESSAGE_CREATE（私聊）；要群聊再勾群事件。</li>
        <li>「开发体验用户」添加你的 QQ 号。</li>
        <li>回到本页填入凭据、打开开关，点「保存并应用」。</li>
        <li>在 QQ 里给机器人发一条消息完成授权，之后即可提问。</li>
      </ol>
      <div class="settings-side-title" style="margin-top:12px">菜单</div>
      <p style="font-size:12px;line-height:1.85">帮助 / 提问方式 / 思考深度 / 更多，首次启用自动推送；失败可点「重新推送菜单」。菜单按钮与 /帮助 里的指令完全一致。</p>
      <p style="margin-top:8px">QQ 模式的设置与微信模式互相独立，切换平台不会改动另一边的配置。</p>
    </aside></div>`;

  $("#qqSave").onclick = async () => {
    const msg = $("#qqMsg");
    $("#qqSave").disabled = true; $("#qqSave").textContent = "保存中…";
    msg.textContent = ""; msg.className = "msg";
    try {
      const payload = {
        app_id: $("#qqAppId").value,
        enabled: $("#qqEnabled").checked,
        allow_openids: $("#qqAllow").value,
        group_reply: $("#qqGroup").checked,
        citations: $("#qqCite").checked,
        default_explorer: $("#qqExplorer").checked,
        default_thinking: $("#qqThinking").checked,
      };
      const sec = $("#qqSecret").value;
      if (sec) payload.app_secret = sec;
      const r = await api("/api/qqbot/config", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      if (r && r.error) { msg.textContent = "已保存，但连接异常: " + r.error; msg.className = "msg err"; }
      else { msg.textContent = "已保存并应用"; msg.className = "msg ok"; }
    } catch (e) { msg.textContent = "保存失败: " + e.message; msg.className = "msg err"; }
    finally { $("#qqSave").disabled = false; $("#qqSave").textContent = "保存并应用";
      setTimeout(() => showSettings("qqbot"), 1200); }
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

}

/* 平台切换后让设置页回到当前平台有效的分组 */
function resetSettingsSectionForPlatform() {
  const ok = settingSections().some(x => x.id === settingsSection);
  if (!ok) settingsSection = "model";
}
