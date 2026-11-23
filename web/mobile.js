/* ============================================================================
   mobile.js —— Android 端的降级提示

   直播这块在 Android 上还打不通：桌面版是「ffmpeg 抽帧 + mpv 独立窗口」，
   Android 既没有这两个可执行文件，也没有对应的播放链路（要 Media3/ExoPlayer
   重写）。与其让用户点「手动拉取」「开启直播」然后看着它失败，不如直接说清楚。

   只在 UA 带 Android 时生效 —— 桌面程序内嵌的 Chromium 不会命中，
   所以桌面行为一个字节都不变。
   ========================================================================== */
(function () {
  var ua = navigator.userAgent || '';
  if (!/Android/i.test(ua)) {
    return;
  }

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  ready(function () {
    var note = 'Android 版暂不支持直播画面（桌面版用 ffmpeg + mpv，这里要 Media3 重写）。'
      + '雨课堂/学习通的课程与签到不受影响。';

    // 主区空状态文案
    var emptyText = document.getElementById('liveEmptyText');
    if (emptyText) {
      emptyText.textContent = note;
    }

    // 两个会必然失败的按钮：禁用并说明原因
    ['liveStart', 'btnPlay'].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) {
        return;
      }
      el.disabled = true;
      el.title = note;
      if (el.tagName === 'BUTTON') {
        el.textContent = id === 'liveStart' ? 'Android 暂不支持' : '直播画面暂不可用';
      }
    });

    // 侧栏直播卡片里的「手动拉取」
    var grab = document.getElementById('liveGrab');
    if (grab) {
      grab.disabled = true;
      grab.title = note;
    }
  });
})();