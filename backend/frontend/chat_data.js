/* 平台数据层：微信 / QQ 共用的一套「列表查询、显示、置顶、实时更新」实现。
 *
 * 只把平台差异写进配置：端点、查询参数、字段映射、DOM id 前缀、文案、
 * 单条 HTML 模板、会话来源、统计文案。加载更多 / 分页 / loading / 错误 /
 * 空态 / 工具栏绑定 / 显示流程全部只有这一份，修一处两边同时生效。
 * 本文件在 app.js 之前加载，运行时才调用 app.js 的全局函数（api/esc/toast…）。
 */
(function () {
  "use strict";

  function optionsHtml(rows, cur) {
    return rows.map(([v, label]) =>
      `<option value="${attrEsc(String(v))}" ${String(v) === String(cur) ? "selected" : ""}>${esc(label)}</option>`).join("");
  }

  /* 列表视图工厂：四个中心（微信文件/链接、QQ 文件/链接）共用。
   * cfg = { view, pane, ids, endpoint, query, parse, shellHtml, itemHtml,
   *         selectors, emptyHtml, metaText, sessions, beforeShow, afterShow,
   *         onItem } */
  function createList(cfg) {
    const view = cfg.view;

    function renderShell() {
      const box = $(cfg.ids.box);
      if (!box) return;
      box.innerHTML = cfg.shellHtml(view);
      const onChange = () => {
        (cfg.selectors || []).forEach((s) => {
          const el = $(s.id);
          if (el) view[s.key] = s.numeric ? (+el.value || 0) : el.value;
        });
        refresh(true);
      };
      (cfg.selectors || []).forEach((s) => {
        const el = $(s.id);
        if (el) el.onchange = onChange;
      });
      const more = $(cfg.ids.more);
      if (more) more.onclick = () => refresh(false);
    }

    function renderItems() {
      const box = $(cfg.ids.items);
      if (!box) return;
      box.innerHTML = "";
      if (!view.items.length) {
        box.innerHTML = cfg.emptyHtml(view);
      }
      for (const item of view.items) {
        const d = document.createElement("div");
        d.className = "result-item";
        d.innerHTML = cfg.itemHtml(item);
        d.onclick = () => cfg.onItem(item);
        box.appendChild(d);
      }
      const meta = $(cfg.ids.meta);
      if (meta) meta.textContent = cfg.metaText(view);
      const more = $(cfg.ids.more);
      if (more) more.style.display = view.hasMore ? "" : "none";
    }

    async function refresh(reset = true) {
      if (view.loading) return;
      if (reset) { view.offset = 0; view.items = []; }
      view.loading = true;
      const meta = $(cfg.ids.meta);
      if (meta) meta.textContent = "加载中…";
      try {
        // 默认单请求；少数列表要同时取「队列状态」等附加数据，用 cfg.fetch 覆盖
        const d = cfg.fetch
          ? await cfg.fetch(view)
          : await api(cfg.endpoint + "?" + cfg.query(view).toString());
        const r = cfg.parse(d, view);
        view.items.push(...(r.items || []));
        view.offset += (r.items || []).length;
        view.hasMore = !!r.hasMore;
        if (r.stats !== undefined) view.stats = r.stats;
        if (r.extra !== undefined) view.extra = r.extra;
        renderItems();
      } catch (e) {
        if (meta) meta.textContent = "加载失败：" + e.message;
      } finally {
        view.loading = false;
      }
    }

    async function show(opts = {}) {
      if (typeof opts === "string") opts = { q: opts };
      Object.assign(view, opts);
      if (cfg.sessions) await cfg.sessions();
      if (cfg.beforeShow) await cfg.beforeShow();
      renderShell();
      await refresh(true);
      showPane(cfg.ids.box);
      if (cfg.afterShow) cfg.afterShow();
    }

    return { view, renderShell, renderItems, refresh, show };
  }

  /* 置顶：两个平台只差端点、请求体与刷新函数；POST / 状态回写 / 提示共用。 */
  async function setPin(cfg) {
    try {
      await api(cfg.endpoint, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg.body),
      });
      await cfg.onDone();
      toast(cfg.pinned ? "已置顶" : "已取消置顶");
    } catch (e) {
      toast("操作失败：" + e.message);
    }
  }

  /* SSE 实时流：两个平台共用连接生命周期与帧解析，差异只在 URL 与 onData。 */
  function startLive(cfg) {
    if (cfg.store.es) { try { cfg.store.es.close(); } catch (e) { /* 已断开 */ } }
    cfg.store.es = null;
    const es = new EventSource(cfg.url());
    cfg.store.es = es;
    es.onmessage = (ev) => {
      let d = null;
      try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (d) cfg.onData(d);
    };
    es.onerror = () => { /* EventSource 自带重连，这里不主动关闭 */ };
    return es;
  }

  window.ChatData = { optionsHtml, createList, setPin, startLive };
})();
