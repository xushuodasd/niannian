/* ============================================================
   统一外壳（三页唯一的壳）
   ------------------------------------------------------------
   夜话 / 心象 / 孕育 三页共用同一套「左竖栏 + 底部导航 + 右主区」结构。
   导航的文案与图标只在这里定义一次，避免三份拷贝各写各的而慢慢漂移；
   抽屉（窄屏唤出左栏）的开合也在这里统一绑定，页面只管调用 closeDrawer。

   用法（页面脚本最前面调用一次）：
     const refs = Shell.mount({
       active: "nighttalk",                     // 高亮哪一项：nighttalk / mind / genesis
       brand: "陪伴",                            // 左栏顶部的品牌名
       brandExtra: '<button class="linkbtn" ...>', // 可选：品牌名右侧的额外按钮
       mainTpl: "mainTpl",                      // 可选：<template> 的 id，内容即右主区
     });
     // refs = { rail, railBody, main, scrim, drawerBtn, openDrawer, closeDrawer }
   ============================================================ */
window.Shell = (function () {
  "use strict";

  // 三段导航：只在左栏底部出现，三页一致（href 即页面，key 即高亮标识）
  const NAV = [
    {
      key: "nighttalk", href: "/", title: "和她说说话", label: "夜话",
      icon: '<path d="M2.8 3.4h10.4a1.1 1.1 0 0 1 1.1 1.1v5a1.1 1.1 0 0 1-1.1 1.1H7.7l-3.1 2.4v-2.4H2.8a1.1 1.1 0 0 1-1.1-1.1v-5a1.1 1.1 0 0 1 1.1-1.1z"/>',
    },
    {
      key: "mind", href: "/debug.html", title: "她此刻的内心", label: "心象",
      icon: '<path d="M1.4 8h2.7l1.4-3.6 2.2 7.4 1.5-3.8h4.4"/>',
    },
    {
      key: "genesis", href: "/console.html", title: "孕育与管理生命体", label: "孕育",
      icon: '<path d="M8 14V7.4"/><path d="M8 7.4C8 5 6.4 3.4 4 3.4c0 2.4 1.6 4 4 4z"/><path d="M8.2 8.8c0-1.9 1.3-3.2 3.3-3.2 0 1.9-1.3 3.2-3.3 3.2z"/>',
    },
  ];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function navHTML(active) {
    return NAV.map((n) =>
      '<a href="' + n.href + '"' + (n.key === active ? ' class="on"' : "") +
      ' title="' + n.title + '">' +
      '<svg class="ni" viewBox="0 0 16 16" aria-hidden="true">' + n.icon + "</svg>" +
      n.label + "</a>"
    ).join("");
  }

  function mount(cfg) {
    cfg = cfg || {};
    const shell = document.createElement("div");
    shell.className = "shell";
    shell.innerHTML =
      '<aside class="rail" id="rail">' +
        '<div class="rail-head">' +
          '<div class="brand">' + esc(cfg.brand || "") + '<span class="bdot"></span></div>' +
          (cfg.brandExtra || "") +
        '</div>' +
        '<div class="rail-body" id="railBody"></div>' +
        '<div class="rail-foot"><nav class="pages">' + navHTML(cfg.active) + '</nav></div>' +
      '</aside>' +
      '<div class="scrim" id="scrim"></div>' +
      '<main class="main" id="main"></main>';

    // 左栏中部列表：可选用页面里的 <template> 填充
    const railBody = shell.querySelector("#railBody");
    if (cfg.railTpl) {
      const tpl = document.getElementById(cfg.railTpl);
      if (tpl) railBody.appendChild(tpl.content.cloneNode(true));
    }

    // 右主区：优先取页面里的 <template>（避免把大段 HTML 塞进 JS 字符串）
    const mainEl = shell.querySelector("#main");
    if (cfg.mainTpl) {
      const tpl = document.getElementById(cfg.mainTpl);
      if (tpl) mainEl.appendChild(tpl.content.cloneNode(true));
    }

    // 外壳放在 body 最前，页面原有的弹层 / 脚本不受影响
    document.body.insertBefore(shell, document.body.firstChild);

    const rail = shell.querySelector("#rail");
    const scrim = shell.querySelector("#scrim");
    const drawerBtn = mainEl.querySelector("#drawerBtn");

    function openDrawer() { rail.classList.add("open"); scrim.classList.add("show"); }
    function closeDrawer() { rail.classList.remove("open"); scrim.classList.remove("show"); }
    if (drawerBtn) drawerBtn.addEventListener("click", openDrawer);
    scrim.addEventListener("click", closeDrawer);

    return {
      shell: shell, rail: rail, railBody: railBody, main: mainEl,
      scrim: scrim, drawerBtn: drawerBtn,
      openDrawer: openDrawer, closeDrawer: closeDrawer,
    };
  }

  return { mount: mount, esc: esc, NAV: NAV };
})();
