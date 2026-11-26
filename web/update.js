/* ============================================================================
   update.js —— 启动时静默查一次版本

   延迟 6 秒（等首屏和签到扫描先跑起来），失败一律不出声：检查更新永远不该
   挡住签到。

   命中新版只做两件事：
     · 顶栏「设置」挂红点（更新入口在设置 → 其他 → 关于）
     · 把「关于」那一行换成下载入口，并把状态记在 window.__updatePending

   设置打开时 ppt.js 会读 __updatePending，自动落到「其他」档。
   ========================================================================== */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  function mark(info) {
    window.__updatePending = info;
    var dot = $('updateDot');
    if (dot) { dot.hidden = false; }
    var line = $('updateLine');
    var btn = $('btnCheckUpdate');
    if (line) {
      line.className = 'update-line is-new';
      line.innerHTML = 'v' + info.latest + ' 可用 · <a href="' + info.release +
        '" target="_blank" rel="noopener">去下载</a>';
    }
    if (btn) { btn.hidden = true; }
  }

  function run() {
    fetch('/api/update/check').then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok && d.hasNew) { mark(d); }
      })
      .catch(function () { /* 网络不通就算了，用户手动点也一样 */ });
  }

  function boot() { setTimeout(run, 6000); }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();