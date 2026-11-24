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
  var CORE = { conn: null, ready: false, tried: false, acts: [], sent: 0 };

  function log(s) {
    try { console.log('[IM] ' + s); } catch (e) { /* 忽略 */ }
  }

  function ensureSdk(cb) {
    if (window.websdk) { cb(true); return; }
    var el = document.createElement('script');
    el.src = SDK_URL;
    el.onload = function () { cb(!!window.websdk); };
    el.onerror = function () { cb(false); };
    document.head.appendChild(el);
  }

  function report(items) {
    if (!items || !items.length) { return; }
    fetch('/api/cx/im/events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: items })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) {
        CORE.sent += (d.added || 0);
        if (d.added) {
          log('上报 ' + d.added + ' 条（累计 ' + d.total + '）');
          // 有新活动就让列表刷一下
          if (window.cxRefresh) { try { window.cxRefresh(); } catch (e) { /* 忽略 */ } }
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
              report(acts);
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

  function connect() {
    if (CORE.tried) { return; }
    CORE.tried = true;
    ensureSdk(function (ok) {
      if (!ok) { log('SDK 加载失败，群聊签到不可用'); return; }
      var W = window.websdk;
      var NS = W.default || W;
      if (!NS || typeof NS.connection !== 'function') { log('SDK 形态不认识'); return; }

      fetch('/api/cx/im/session').then(function (r) { return r.json(); }).then(function (d) {
        if (!d || !d.ok) { log('没拿到 IM 会话：' + ((d && d.error) || '')); return; }

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
            log('已连接');
            pullRoaming(d.groups || []);
          },
          onClosed: function () { CORE.ready = false; },
          onOffline: function () { CORE.ready = false; },
          onError: function (e) { log('错误 ' + JSON.stringify(e).slice(0, 120)); },
          onTextMessage: function (m) {
            var a = pickActivity(m);
            if (!a) { return; }
            CORE.acts.push(a);
            log('收到活动：' + a.courseName + ' / ' + a.atypeName);
            report([a]);
          }
        });
        CORE.conn.open({
          apiUrl: W.config.apiURL,
          user: d.uid,
          accessToken: d.token,
          appKey: W.config.appkey
        });
        log('正在连接 uid=' + String(d.uid).slice(0, 3) + '***');
      }).catch(function (e) { log('取会话失败：' + e); });
    });
  }

  /* 打开学习通面板时再连，别在启动时就占一条长连接 */
  document.addEventListener('DOMContentLoaded', function () {
    var tab = document.querySelector('[data-tab="cx"], #tabCx, #btnCx');
    if (tab) { tab.addEventListener('click', function () { connect(); }); }
    // 兜底：登录成功后第一次刷新签到也连上
    var btn = document.getElementById('cxScanBtn');
    if (btn) { btn.addEventListener('click', function () { connect(); }); }
  });

  window.HFUToneIM = { connect: connect, state: CORE };
})();