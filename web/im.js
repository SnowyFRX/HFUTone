/* im.js —— 学习通班级群的签到通知（环信 Web SDK）
 *
 * 为什么放在前端：群消息只能由客户端 SDK 收。环信的 REST 不提供群历史消息
 * （/chatgroups/{id}/messages 返回 404，/chatmessages/{id} 返回 401），能拿到
 * 消息的只有 SDK 的 WebSocket 长连接。QtWebEngine 里正好能跑 Web SDK。
 *
 * 数据流：
 *   后端 /api/cx/im/session  → uid + accessToken + 群 id
 *   SDK conn.open            → 连上后推送离线消息 + 实时消息
 *   每条消息的 ext.attachment.att_chat_course → 签到活动
 *   POST /api/cx/im/events   → 后端缓存，扫描时并进签到列表
 *
 * 失败不打扰用户：连不上就静默（课程活动扫描照常），只在 console 留一行。
 */
(function () {
  var SDK_URL = '/im/Easemob.js';
  var CORE = { conn: null, ready: false, tried: false, acts: [], sent: 0, retryTimer: 0, retryDelay: 5000 };

  function log(s) {
    try { console.log('[IM] ' + s); } catch (e) { /* 忽略 */ }
    // 安卓上看不到 console，所以把状态写到面板上一行，出问题截图就能定位
    try {
      var el = document.getElementById('cxImState');
      if (el) {
        el.textContent = '群聊通知：' + s;
        el.className = 'cx-im-state' + (/失败|没拿到|出错/.test(s) ? ' is-bad' : '');
      }
    } catch (e) { /* 忽略 */ }
  }

  function ensureSdk(cb) {
    if (window.websdk) { cb(true); return; }
    var el = document.createElement('script');
    el.src = SDK_URL;
    el.onload = function () { cb(!!window.websdk); };
    el.onerror = function () { cb(false); };
    document.head.appendChild(el);
  }

  function report(items, realtime) {
    if (!items || !items.length) { return; }
    var isRt = (realtime !== false);
    fetch('/api/cx/im/events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: items, realtime: isRt })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) {
        CORE.sent += (d.added || 0);
        if (d.queued) {
          log((isRt ? '实时收到' : '漫游补录') + '，已触发后端秒签（' + d.queued + ' 条）');
        } else if (d.added) {
          log('上报 ' + d.added + ' 条（累计 ' + d.total + '）');
        }
        if (d.added || d.queued) {
          // 有新活动或触发秒签就让列表与状态面板刷新
          if (window.cxRefresh) { try { window.cxRefresh(); } catch (e) { /* 忽略 */ } }
          if (window.refreshAutoSignState) {
            setTimeout(function () {
              try { window.refreshAutoSignState(); } catch (e) { /* 忽略 */ }
            }, 1200);
          }
        }
      }
    }).catch(function () { /* 后端没起来就算了 */ });
  }

  function pickActivity(m) {
    try {
      var att = m && m.ext && m.ext.attachment && m.ext.attachment.att_chat_course;
      if (!att || !att.aid) { return null; }
      var label = (att.atypeName || '') + (att.title || '');
      if (label.indexOf('签到') < 0) { return null; }   // 作业/讨论不要
      var ci = att.courseInfo || {};
      return {
        activeId: String(att.aid),
        courseId: String(ci.courseid || ''),
        classId: String(ci.classid || ''),
        courseName: String(ci.coursename || ''),
        title: String(att.title || att.atypeName || '签到'),
        atypeName: String(att.atypeName || ''),
        url: String(att.url || '')
      };
    } catch (e) { return null; }
  }

  /* 拉每个群的漫游消息。
     环信 REST 没有历史消息（/chatgroups/{id}/messages 404、
     /chatmessages/{id} 401），真正的接口是 SDK 内部的
     POST /users/{uid}/messageroaming，队列名 = 群 id + @conference.easemob.com，
     返回的消息是 protobuf，由 SDK 解码。所以只能在浏览器里做。 */
  function pullRoaming(groups) {
    var conn = CORE.conn;
    if (!conn || !groups.length) { return; }
    var queue = groups.slice(0, 20);
    var idx = 0;
    function next() {
      if (idx >= queue.length) { log('漫游拉取完成'); return; }
      var g = queue[idx++];
      try {
        conn.fetchHistoryMessages({
          queue: String(g.groupId) + '@conference.easemob.com',
          isGroup: true,
          count: 20,
          success: function (list) {
            var acts = [];
            (list || []).forEach(function (m) {
              var a = pickActivity(m);
              if (a) { acts.push(a); }
            });
            if (acts.length) {
              log(g.groupId + ' 拉到 ' + acts.length + ' 条活动');
              report(acts, false);
            }
            next();
          },
          fail: function (e) {
            log('群 ' + g.groupId + ' 漫游失败');
            next();
          }
        });
      } catch (e) { next(); }
    }
    next();
  }

  function scheduleReconnect(why) {
    if (CORE.retryTimer) { return; }
    var wait = CORE.retryDelay;
    CORE.retryDelay = Math.min(30000, CORE.retryDelay * 2);
    log((why || '连接断开') + '，' + Math.round(wait / 1000) + ' 秒后自动重连');
    CORE.retryTimer = setTimeout(function () {
      CORE.retryTimer = 0;
      CORE.tried = false;
      connect(true);
    }, wait);
  }

  function connect(force) {
    if (CORE.ready) { return; }
    if (CORE.tried && !force) { return; }
    CORE.tried = true;
    ensureSdk(function (ok) {
      if (!ok) { CORE.tried = false; log('SDK 加载失败（/im/Easemob.js 没取到）'); return; }
      var W = window.websdk;
      var NS = W.default || W;
      if (!NS || typeof NS.connection !== 'function') { CORE.tried = false; log('SDK 形态不认识'); return; }

      log('SDK 已加载，正在取 IM 会话…');
      fetch('/api/cx/im/session').then(function (r) { return r.json(); }).then(function (d) {
        if (!d || !d.ok) {
          CORE.tried = false;
          log('没拿到 IM 会话：' + ((d && d.error) || '未知'));
          return;
        }
        log('拿到会话，正在连接（' + (d.groups || []).length + ' 个群）');

        W.config = {
          xmppURL: 'https://im-api-vip6-v2.easecdn.com/ws',
          apiURL: 'https://a1-vip6.easecdn.com',
          appkey: d.appKey || 'cx-dev#cxstudy',
          Host: 'easemob.com',
          https: true,
          isHttpDNS: false,
          isMultiLoginSessions: true,
          isAutoLogin: false,
          isWindowSDK: false,
          isSandBox: false,
          isDebug: false,
          autoReconnectNumMax: 10,
          autoReconnectInterval: 3,
          queue: '',
          heartBeatWait: 5000,
          delivery: false
        };
        CORE.conn = new NS.connection({
          appKey: W.config.appkey,
          isHttpDNS: false,
          isMultiLoginSessions: true,
          host: W.config.Host,
          https: true,
          url: W.config.xmppURL,
          apiUrl: W.config.apiURL,
          isAutoLogin: false,
          queue: '',
          heartBeatWait: 5000,
          autoReconnectNumMax: 10,
          autoReconnectInterval: 3,
          delivery: false,
          isDebug: false
        });
        CORE.conn.listen({
          onOpened: function () {
            CORE.ready = true;
            CORE.retryDelay = 5000;
            if (CORE.retryTimer) { clearTimeout(CORE.retryTimer); CORE.retryTimer = 0; }
            log('已连接（实时监听中），开始拉群消息');
            pullRoaming(d.groups || []);
          },
          onClosed: function () {
            var wasReady = CORE.ready;
            CORE.ready = false;
            if (wasReady) { scheduleReconnect('连接已关闭'); }
          },
          onOffline: function () {
            CORE.ready = false;
            scheduleReconnect('网络离线');
          },
          onError: function (e) {
            CORE.error = JSON.stringify(e || {}).slice(0, 120);
            log('连接出错：' + CORE.error);
          },
          onTextMessage: function (m) {
            var a = pickActivity(m);
            if (!a) { return; }
            CORE.acts.push(a);
            log('收到活动：' + a.courseName + ' / ' + a.atypeName);
            report([a], true);
          }
        });
        CORE.conn.open({
          apiUrl: W.config.apiURL,
          user: d.uid,
          accessToken: d.token,
          appKey: W.config.appkey
        });
        log('正在连接 uid=' + String(d.uid).slice(0, 3) + '***');
      }).catch(function (e) { CORE.tried = false; log('取会话请求失败：' + e); });
    });
  }

  /* 方案三：启动后如果学习通已登录，自动建立环信长连接用于实时秒签，
     不再等用户手动点开学习通 tab。 */
  function autoConnectIfLoggedIn() {
    fetch('/api/cx/state').then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.loggedIn) { connect(false); }
    }).catch(function () { /* 忽略 */ });
  }

  document.addEventListener('DOMContentLoaded', function () {
    var tab = document.querySelector('[data-tab="cx"], #tabCx, #btnCx');
    if (tab) { tab.addEventListener('click', function () { connect(false); }); }
    // 兜底：登录成功后第一次刷新签到也连上
    var btn = document.getElementById('cxScanBtn');
    if (btn) { btn.addEventListener('click', function () { connect(false); }); }
    ['tabCx', 'btnCx'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.addEventListener('click', function () { connect(false); }); }
    });
    setTimeout(autoConnectIfLoggedIn, 1500);
  });

  window.HFUToneIM = { connect: connect, state: CORE };
})();