/* ============================================================================
   mobile.js —— Android（WebView）端的行为适配

   桌面版是「左直播课表 + 右侧栏雨课堂/学习通」，Android 上没有对应的播放链路
   （ffmpeg + mpv 都是外部进程，要 Media3 重写），直播那一整块撤掉，界面主体
   直接给雨课堂/学习通。这里只做三件事，桌面 UA 一个字节都不变：

     1. 给 <body> 加 .is-android —— CSS 靠它隐藏直播卡、播放条、课表主区；
     2. 首屏把雨课堂/学习通那一栏撑满，并主动拉一次课程
        （桌面是「侧栏展开时才拉」，Android 上没有展开这个动作，不拉就永远
         停在「正在加载…」）；
     3. 「在线课堂」按钮在 Android 上不再做开合，改成「滚到面板 + 刷新」，
        未签到红点仍然挂在这个按钮上。

   桌面版内嵌的 Chromium 不会命中 Android UA，为什么不会命中已在 README 记过。
   ========================================================================== */
(function () {
  var ua = navigator.userAgent || '';
  if (!/Android/i.test(ua)) {
    return;
  }

  var body = document.body;
  if (body) {
    body.classList.add('is-android');
  // 手机上没有直播画面（见文件头说明），副标题别再把「直播课堂」挂在那儿
  var sub = document.getElementById('brandSub');
  if (sub) { sub.textContent = '雨课堂 · 学习通'; }
  // 设置里的坐标拾取器：桌面只给可复制的地址，安卓额外给一个能点的链接
  var urlRow = document.querySelector('.set-url-android');
  if (urlRow) { urlRow.hidden = false; }
  }

  function $(id) { return document.getElementById(id); }

  /* 侧栏在桌面版是「展开时拉课程」，Android 上没有展开这个动作，必须主动拉。

     为什么要两步：
       - 启动瞬间 ykt.js 自己的 pull() 还在路上，此时 window.HFUTYkt.refresh()
         会因为 state.loggedIn 仍是 false 而退化成「只 pull 不拉课」；
       - 直接打 /api/ykt/refresh 能把后端的 loaded 置真，但前端要等 15 秒轮询
         才会重绘。
     所以先直连后端拉一次，再等首轮 pull 回来后调前端 refresh() 重绘。 */
  function primeYkt() {
    fetch('/api/ykt/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    }).catch(function () { /* 拉不到由前端 15 秒轮询继续兜底 */ });

    setTimeout(function () {
      if (window.HFUTYkt && typeof window.HFUTYkt.refresh === 'function') {
        try { window.HFUTYkt.refresh(false); } catch (e) { /* 忽略 */ }
      }
    }, 900);
  }

  /* 「在线课堂」按钮：Android 上面板常驻，开合没有意义。
     捕获阶段拦掉 ykt.js 的 toggleSide，换成滚动到面板 + 刷新。 */
  function wireSideButton() {
    var btn = $('btnYkt');
    if (!btn) {
      return;
    }
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }, true);
    btn.addEventListener('click', function () {
      var side = $('viewYkt');
      if (side) {
        side.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
      primeYkt();
    }, false);
    btn.setAttribute('aria-expanded', 'true');
  }

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  ready(function () {
    // 进来就落到雨课堂面板顶部，而不是停在（已被撤掉的）直播课表位置
    var side = $('viewYkt');
    if (side) {
      side.classList.add('is-open');
      side.setAttribute('aria-hidden', 'false');
    }
    wireSideButton();
    wireAbout();

    // ykt.js 的 DOMContentLoaded 处理器比这里先生效（脚本更早加载），
    // 延一帧再拉，保证监听器都已经绑定好。
    setTimeout(primeYkt, 60);
  });

  /** 「说明」弹层：只在安卓显示，桌面没有这个按钮。 */
  function wireAbout() {
    var open = $('btnAbout');
    var box = $('aboutBackdrop');
    var close = $('aboutClose');
    if (!open || !box) { return; }
    open.addEventListener('click', function () { box.classList.add('open'); });
    if (close) { close.addEventListener('click', function () { box.classList.remove('open'); }); }
    box.addEventListener('click', function (e) {
      if (e.target === box) { box.classList.remove('open'); }
    });
  }
})();