/* 平台层：左上角「微」字点一下，整界面切成 QQ（或切回微信）。
 * 两套界面共用同一套功能与数据，差别只在品牌、文案、配色、导航与设置归属。
 * QQ 模式：无公众号、朋友圈→QQ空间、不做频道；机器人接入只有 qqbot。
 */
(function () {
  "use strict";
  const KEY = "wk.platform.v1";

  const BRAND = {
    wechat: {
      key: "wechat", name: "微信", logo: "微", title: "Qwent · 微信",
      theme: "#07c160", emptyDesc: "选择一个会话浏览记录，或在置顶的 ✦ AI 助手 里全局提问",
    },
    qq: {
      key: "qq", name: "QQ", logo: "QQ", title: "Qwent · QQ",
      theme: "#12b7f5", emptyDesc: "选择一个会话浏览消息，或在置顶的 ✦ AI 助手 里全局提问",
    },
  };

  const NAV = {
    wechat: {
      chats: ["💬", "聊天"], contacts: ["👥", "通讯录"], files: ["📁", "文件"],
      links: ["📰", "链接 / 公众号"], moments: ["🌄", "朋友圈"], settings: ["⚙️", "设置"],
    },
    qq: {
      chats: ["💬", "消息"], contacts: ["👥", "联系人"], files: ["📁", "文件"],
      links: ["🔗", "链接"], moments: ["⭐", "QQ空间"], settings: ["⚙️", "设置"],
    },
  };

  // 显式文案表：app.js 里需要按平台变化的关键位置调用 Platform.t(key)
  const TEXT = {
    appTitle: ["Qwent · 微信", "Qwent · QQ"],
    searchDefault: ["搜索会话 / 全局搜消息（回车）", "搜索消息 / 全局搜消息（回车）"],
    searchFiles: ["搜索文件名…", "搜索文件名…"],
    searchLinks: ["搜索链接标题 / 来源群", "搜索链接标题 / 来源"],
    searchMoments: ["搜索朋友圈文字（含图片 OCR）", "搜索 QQ 空间文字（含图片 OCR）"],
    searchContacts: ["搜索联系人 / 群", "搜索联系人 / 群"],
    momentsEmpty: ["还没有朋友圈数据", "还没有 QQ 空间数据"],
    momentsStatus: ["条朋友圈", "条 QQ 空间动态"],
    momentsScope: ["朋友圈", "QQ空间"],
    momentOnly: ["仅本朋友圈", "仅本条动态"],
    momentAsk: ["针对这条朋友圈提问…", "针对这条动态提问…"],
    momentBack: ["← 返回朋友圈", "← 返回 QQ 空间"],
    momentImage: ["朋友圈图片", "QQ空间图片"],
    linkArticle: ["公众号", "文章"],
    articleHint: ["公众号文章可能有防盗链，可在浏览器中打开原文。",
                  "网页文章可能有防盗链，可在浏览器中打开原文。"],
    linksHead: ["链接 / 公众号", "链接"],
    recalled: ["已撤回", "已撤回"],
    chatGroup: ["群聊", "群聊"],
    chatPrivate: ["私聊", "私聊"],
    emptyChat: ["还没有和", "还没有和"],
    aiGlobal: ["全部聊天记录", "全部消息记录"],
    statusReady: ["知识库就绪：", "知识库就绪："],
    botSection: ["机器人", "机器人"],
    wxBotLabel: ["微信通道", "微信通道"],
    qqBotLabel: ["QQ 机器人", "QQ 机器人"],
    qqMsgs: ["QQ 消息", "QQ 消息"],
    kbMsgs: ["知识库记录", "知识库记录"],
    qqMsgEmpty: ["还没有 QQ 消息。在 QQ 里给机器人发一条消息，会话会出现在这里。",
                 "还没有 QQ 消息。在 QQ 里给机器人发一条消息，会话会出现在这里。"],
    quoteHint: ["右键加入引用栏", "右键加入引用栏"],
    quoteAdded: ["已加入引用栏（点 ✕ 可移除）", "已加入引用栏（点 ✕ 可移除）"],
  };

  // 兜底术语映射：渲染后的 UI 字符串统一本地化（绝不用于聊天消息正文）
  const TERMS = [
    ["链接 / 公众号", "链接"], ["链接/公众号", "链接"],
    ["公众号文章", "网页文章"], ["公众号正文", "网页正文"], ["公众号", "网页"],
    ["朋友圈", "QQ空间"], ["微信知识助手", "Qwent"], ["QQ知识助手", "Qwent"],
    ["微信记录", "消息记录"], ["微信通道", "QQ通道"], ["微信", "QQ"],
    ["通讯录", "联系人"], ["聊天记录", "消息记录"], ["小程序", "应用"],
    ["视频号", "短视频"], ["拍了拍", "戳了戳"],
  ];

  function read() {
    try {
      const v = localStorage.getItem(KEY);
      if (v === "qq" || v === "wechat") return v;
    } catch (e) { /* 隐私模式下 localStorage 不可用 */ }
    return "wechat";
  }

  let cur = read();

  const API = {
    get() { return cur; },
    isQQ() { return cur === "qq"; },
    brand() { return BRAND[cur]; },
    nav() { return NAV[cur]; },
    t(key) {
      const pair = TEXT[key];
      if (!pair) return key;
      return pair[cur === "qq" ? 1 : 0];
    },
    qtext(s) {
      if (cur !== "qq" || !s) return String(s || "");
      let out = String(s);
      for (const [a, b] of TERMS) out = out.split(a).join(b);
      return out;
    },
    sysText(m) {
      const p = (m && m.parsed) || {};
      const wx = p.system || "";
      if (cur !== "qq") return wx;
      return p.system_qq || API.qtext(wx);
    },
    localize(root) {
      if (cur !== "qq" || !root) return;
      // 不给容器打「已处理」标记：设置页内容会被反复重建，打了标记会导致
      // 第二次进入时残留微信术语。术语替换本身幂等，重复遍历无副作用。
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      for (const n of nodes) {
        if (!n.nodeValue || !n.nodeValue.trim()) continue;
        const p = n.parentElement;
        if (!p) continue;
        const tag = p.tagName;
        if (tag === "SCRIPT" || tag === "STYLE" || tag === "TEXTAREA" ||
            p.closest("[data-nol10n]")) continue;
        const v = API.qtext(n.nodeValue);
        if (v !== n.nodeValue) n.nodeValue = v;
      }
      const els = root.querySelectorAll ? root.querySelectorAll("[placeholder],[title],[aria-label]") : [];
      els.forEach((el) => {
        ["placeholder", "title", "aria-label"].forEach((a) => {
          const v = el.getAttribute(a);
          if (v) el.setAttribute(a, API.qtext(v));
        });
      });
    },
    apply() {
      document.documentElement.dataset.platform = cur;
      const b = BRAND[cur], other = BRAND[cur === "qq" ? "wechat" : "qq"];
      const meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute("content", b.theme);
      document.title = b.title;
      // 内联 SVG 图标：避免 /favicon.ico 404，且标签页图标跟着平台换色
      const icon = document.querySelector("#appIcon");
      if (icon) {
        const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">`
          + `<rect width="64" height="64" rx="14" fill="${b.theme}"/>`
          + `<text x="32" y="43" text-anchor="middle" font-size="${cur === "qq" ? 25 : 34}" `
          + `font-family="Microsoft YaHei,sans-serif" font-weight="700" fill="white">${b.logo}</text></svg>`;
        icon.setAttribute("href", "data:image/svg+xml," + encodeURIComponent(svg));
      }

      const av = document.querySelector(".rail .avatar");
      if (av) {
        av.textContent = b.logo;
        av.setAttribute("aria-label", b.title);
        av.setAttribute("title", "点击切换到 " + other.name);
        av.dataset.platform = cur;
        av.classList.toggle("qq-logo", cur === "qq");
      }
      const big = document.querySelector(".logo-big");
      if (big) { big.textContent = b.logo; big.classList.toggle("qq-logo", cur === "qq"); }
      const h2 = document.querySelector(".chat-empty h2");
      if (h2) h2.textContent = b.title;
      const desc = document.querySelector(".chat-empty > p:not(.hint):not(.qwent-sub)");
      if (desc) desc.innerHTML = b.emptyDesc;

      const nav = NAV[cur];
      document.querySelectorAll(".rail-btn[data-view]").forEach((btn) => {
        const item = nav[btn.dataset.view];
        if (!item) return;
        const ico = btn.querySelector(".rail-ico");
        if (ico) ico.textContent = item[0];   // 只换图标，别把 span 结构抹掉
        else btn.textContent = item[0];
        btn.title = item[1];
        btn.setAttribute("aria-label", item[1]);
      });
      // 静态输入框占位
      const si = document.querySelector("#searchInput");
      if (si && !si.dataset.platformDirty) si.placeholder = API.t("searchDefault");
      const ai = document.querySelector("#assistantInput");
      if (ai) ai.placeholder = cur === "qq" ? "输入想了解的问题…" : "输入想了解的问题…";
    },
    set(p) {
      if (p !== "qq" && p !== "wechat") return;
      if (p === cur) { API.apply(); return; }
      cur = p;
      try { localStorage.setItem(KEY, p); } catch (e) { /* ignore */ }
      API.apply();
      fetch("/api/platform", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ platform: p }),
      }).catch(() => {});
      if (typeof window.onPlatformChange === "function") {
        try { window.onPlatformChange(p); } catch (e) { console.warn("platform hook", e); }
      }
    },
    toggle() { API.set(cur === "qq" ? "wechat" : "qq"); },
    // 首次进入且本地没存过时，以后端为准（跨设备一致）
    async syncFromServer() {
      try {
        const hasLocal = (() => { try { return !!localStorage.getItem(KEY); } catch (e) { return false; } })();
        if (hasLocal) return;
        const r = await fetch("/api/platform").then((x) => x.json());
        if (r && (r.platform === "qq" || r.platform === "wechat") && r.platform !== cur) {
          cur = r.platform;
          API.apply();
          if (typeof window.onPlatformChange === "function") {
            try { window.onPlatformChange(cur); } catch (e) { console.warn("platform hook", e); }
          }
        }
      } catch (e) { /* 后端不可达时保持默认微信 */ }
    },
  };

  window.Platform = API;
  API.apply();
  API.syncFromServer();

  function bindLogo() {
    const av = document.querySelector(".rail .avatar");
    if (av && !av.dataset.bound) {
      av.dataset.bound = "1";
      av.addEventListener("click", () => API.toggle());
      // role=button：键盘也要能切换
      av.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
          e.preventDefault();
          API.toggle();
        }
      });
    }
  }
  // script 在 body 末尾，DOMContentLoaded 可能已经触发过
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindLogo);
  } else {
    bindLogo();
  }
})();
