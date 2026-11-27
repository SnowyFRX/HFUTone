/* im.js —— 学习通班级群通知状态同步（长连接已下沉至后端 src/cxim.py）
 *
 * 架构演进：
 *   之前环信 Web SDK 跑在 WebView 里，Android 切后台/锁屏或 KeepAliveService
 *   后台重建时没有 WebView 或 JS 定时器被节流，导致 IM 秒签失效并退化为 180s 轮询。
 *   现在纯 Python 后端（src/cxim.py）直接维护环信 SockJS/WebSocket 长连接与漫游
 *   补录，不依赖页面存活；前端只负责轮询 /api/cx/im/status 展示状态，并在后端
 *   收到新消息时同步刷新界面列表。
 */
(function () {
  'use strict';

  var CORE = {
    backend: true,
    ready: false,
    state: 'idle',
    received: -1,
    groups: 0,
    error: ''
  };

  function paint(text, isBad) {
    try {
      var el = document.getElementById('cxImState');
      if (el) {
        el.textContent = '群聊通知：' + text;
        el.className = 'cx-im-state' + (isBad ? ' is-bad' : '');
      }
    } catch (e) { /* 忽略 */ }
  }

  function pollStatus() {
    return fetch('/api/cx/im/status')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) { return; }
        CORE.ready = !!d.connected;
        CORE.state = d.state || 'idle';
        CORE.groups = d.groups || 0;
        CORE.error = d.error || '';

        var text = d.detail || (d.connected ? '后端实时在线' : '未连接');
        if (d.received) { text += ' · 已收 ' + d.received + ' 条'; }
        var isBad = (d.state === 'backoff' && !!d.error);
        paint(text, isBad);

        // 后端收到新群聊签到消息时，立刻刷新前端签到列表与守护状态条
        if (CORE.received >= 0 && (d.received || 0) > CORE.received) {
          if (window.HFUTCx && window.HFUTCx.refresh) {
            try { window.HFUTCx.refresh(); } catch (e) { /* 忽略 */ }
          }
          if (window.refreshAutoSignState) {
            try { window.refreshAutoSignState(); } catch (e) { /* 忽略 */ }
          }
        }
        CORE.received = d.received || 0;
      })
      .catch(function () { /* 后端未就绪时静默 */ });
  }

  function connect(force) {
    if (force) {
      return fetch('/api/cx/im/reconnect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      }).then(function () { return pollStatus(); }).catch(function () {});
    }
    return pollStatus();
  }

  document.addEventListener('DOMContentLoaded', function () {
    pollStatus();
    setInterval(function () {
      if (!document.hidden) { pollStatus(); }
    }, 10000);

    // 切后台时心跳会被系统掐掉，回来时后端的退避计时器可能正卡在 60 秒。
    // 只在确实断开时才踢重连，别把好的连接也掐了。
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { return; }
      fetch('/api/cx/im/status').then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.connected) { connect(true); }
        })
        .catch(function () { connect(true); });
    });
  });

  window.HFUToneIM = { connect: connect, poll: pollStatus, state: CORE };
})();
