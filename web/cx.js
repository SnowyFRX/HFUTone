/* ============================================================================
   HFUTone · 学习通签到一栏
   只做界面与调用：协议在 web/server.py 的 /api/cx/* 后面，实现在
   src/cxapi.py（接口）与 src/cxsign.py（编排）。
   ========================================================================== */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var S = {
    loggedIn: false,
    profile: {},
    signs: [],
    courses: [],
    loaded: false,
    busy: false,
    error: '',
    timer: null,
    pollTimer: null
  };

  /* ------------------------------------------------------------------ 工具 */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function api(path, body) {
    var opt = body === undefined ? {} : {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    };
    return fetch(path, opt).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; });
    }, function (e) {
      return { ok: false, error: String(e && e.message || e) };
    });
  }

  function toast(msg, kind) {
    var wrap = $('toasts');
    if (!wrap) { console.log('[cx]', msg); return; }
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(function () {
      el.classList.add('out');
      setTimeout(function () { el.remove(); }, 400);
    }, 2600);
  }

  function when(ts) {
    if (!ts) { return ''; }
    var d = new Date(Number(ts));
    if (isNaN(d.getTime())) { return ''; }
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getHours()) + ':' + p(d.getMinutes());
  }

  /* ------------------------------------------------------------ 状态渲染 */
  var TYPE_TEXT = {
    normal: '普通签到',
    qrcode: '二维码签到',
    pattern: '手势签到',
    location: '位置签到',
    code: '签到码'
  };

  function paintState() {
    var dot = $('cxDot');
    var status = $('cxStatus');
    var count = $('cxCount');

    if (dot) {
      // 和雨课堂那边同一套类名：dot ok / dot err / dot（灰）
      // 之前这里写的是 is-on / is-warn，那是 .ykt-row-state 的状态类，
      // 挂在 dot 上没有任何样式，所以登录了也一直是灰点。
      dot.className = 'dot' + (S.loggedIn && !S.error ? ' ok'
        : (S.error ? ' err' : ''));
    }
    if (status) {
      if (!S.loggedIn) {
        status.textContent = '未登录';
      } else if (S.busy) {
        status.textContent = '正在处理…';
      } else if (S.error) {
        status.textContent = S.error;
      } else {
        var who = S.profile.name || '已登录';
        status.textContent = who + (S.profile.school ? ' · ' + S.profile.school : '');
      }
    }
    if (count) {
      var live = liveSigns();
      var pending = live.length;
      count.textContent = pending
        ? (pending + ' 个待签')
        : (S.signs.length ? '全部已签或已截止' : '');
    }

    if ($('cxLoginBtn')) { $('cxLoginBtn').hidden = S.loggedIn; }
    if ($('cxLogout')) { $('cxLogout').hidden = !S.loggedIn; }
    if ($('cxScanBtn')) { $('cxScanBtn').disabled = !S.loggedIn || S.busy; }
    if ($('cxSignAll')) {
      var auto = liveSigns().filter(function (x) { return x.canAuto; }).length;
      $('cxSignAll').disabled = !S.loggedIn || S.busy || !auto;
      $('cxSignAll').textContent = auto ? '一键签到（' + auto + '）' : '一键签到';
    }
  }

  function liveSigns() {
    // 只留还能签的：已签的排除，过了截止时间（endTime）的排除。
    // 扫描是一次性的快照，老师在 5 秒窗口里发的签到，等界面渲染出来
    // 往往就过了截止时间——不在渲染时再判一次，界面就会出现
    // "已截止却还显示待签"的假象。
    var now = Date.now();
    return (S.signs || []).filter(function (x) {
      if (x.signed) { return false; }
      if (x.endTime && now > x.endTime) { return false; }
      return true;
    });
  }

  function paintList() {
    var box = $('cxSigns');
    if (!box) { return; }
    if (!S.loggedIn) {
      box.innerHTML = '<p class="ykt-empty">登录后这里会列出进行中的签到。</p>';
      return;
    }
    var rows = liveSigns();
    if (!rows.length) {
      box.innerHTML = '<p class="ykt-empty">'
        + (S.loaded ? '当前没有可签的签到。' : '点「刷新签到」看看有没有。')
        + '</p>';
      return;
    }

    box.innerHTML = rows.map(function (x) {
      var i = S.signs.indexOf(x);
      var label = TYPE_TEXT[x.signType] || x.signType;
      var stateText, cls;
      if (x.signed) {
        stateText = x.statusText || '已签到';
        cls = 'is-on';
      } else if (x.missing && x.missing.length) {
        stateText = '缺' + x.missing.join('/');
        cls = 'is-warn';
      } else {
        stateText = '未签到';
        cls = 'is-off';
      }

      var btn = x.signed
        ? '<button type="button" disabled>已签</button>'
        : '<button data-act="sign" data-i="' + i + '" type="button">签到</button>';

      return ''
        + '<div class="ykt-row" data-i="' + i + '">'
        +   '<div class="ykt-row-main">'
        +     '<div class="ykt-row-name">'
        +       esc(x.courseName || '')
        +       '<span class="cx-tag">' + esc(label) + '</span>'
        +     '</div>'
        +     '<div class="ykt-row-meta">'
        +       esc(x.name || x.desc || '')
        +       (x.place ? ' · ' + esc(x.place) : '')
        +       (x.startTime ? ' · ' + when(x.startTime) : '')
        +     '</div>'
        +   '</div>'
        +   '<span class="ykt-row-state ' + cls + '">' + esc(stateText) + '</span>'
        +   '<div class="ykt-row-btns">' + btn + '</div>'
        + '</div>';
    }).join('');
  }

  function apply(d) {
    if (!d) { return; }
    S.loggedIn = !!d.loggedIn;
    S.profile = d.profile || {};
    S.signs = d.signs || [];
    S.courses = d.courses || [];
    S.error = d.error || '';
    if (S.signs.length) { S.loaded = true; }
    paintState();
    paintList();
  }

  function refresh() {
    return api('/api/cx/state').then(apply);
  }

  /* --------------------------------------------------------------- 登录 */
  function openLogin() {
    if ($('cxLoginMsg')) { $('cxLoginMsg').textContent = ''; }
    if ($('cxLoginBackdrop')) { $('cxLoginBackdrop').classList.add('open'); }
    var el = $('cxPhone');
    if (el) { setTimeout(function () { el.focus(); }, 60); }
  }

  function closeLogin() {
    if ($('cxLoginBackdrop')) { $('cxLoginBackdrop').classList.remove('open'); }
  }

  function submitLogin() {
    var phone = ($('cxPhone') && $('cxPhone').value || '').trim();
    var pwd = ($('cxPassword') && $('cxPassword').value || '');
    var msg = $('cxLoginMsg');
    if (!phone || !pwd) {
      if (msg) { msg.textContent = '手机号和密码都要填'; }
      return;
    }
    S.busy = true;
    paintState();
    if (msg) { msg.textContent = '登录中…'; }
    api('/api/cx/login', { phone: phone, password: pwd }).then(function (d) {
      S.busy = false;
      if (d && d.ok) {
        if ($('cxPassword')) { $('cxPassword').value = ''; }
        closeLogin();
        toast('学习通登录成功', 'ok');
        refresh().then(function () { return doScan(); });
      } else {
        if (msg) { msg.textContent = (d && d.error) || '登录失败'; }
        paintState();
      }
    });
  }

  /* --------------------------------------------------------------- 扫描 */
  function doScan() {
    if (!S.loggedIn) { return Promise.resolve(); }
    S.busy = true;
    S.error = '';
    paintState();
    return api('/api/cx/scan', { force: true }).then(function (d) {
      S.busy = false;
      if (d && d.ok) {
        S.signs = d.signs || [];
        S.courses = d.courses || [];
        S.loaded = true;
        S.error = '';
      } else {
        S.error = (d && d.error) || '扫描失败';
      }
      paintState();
      paintList();
    });
  }

  /* --------------------------------------------------------- 签到与补输入 */
  function askInput(kind, lead, placeholder, onOk) {
    if (!$('cxInputBackdrop')) { return; }
    $('cxInputTitle').textContent = '补充' + kind;
    $('cxInputLead').textContent = lead || '';
    $('cxInputValue').value = '';
    $('cxInputValue').placeholder = placeholder || '';
    $('cxInputMsg').textContent = '';
    $('cxInputBackdrop').classList.add('open');
    $('cxInputBackdrop').dataset.kind = kind;

    var submit = function () {
      var v = ($('cxInputValue').value || '').trim();
      if (!v) { $('cxInputMsg').textContent = '还没填'; return; }
      $('cxInputBackdrop').classList.remove('open');
      onOk(v);
    };
    var btn = $('cxInputSubmit');
    if (btn) { btn.onclick = submit; }
    var inp = $('cxInputValue');
    if (inp) {
      inp.onkeydown = function (e) { if (e.key === 'Enter') { submit(); } };
      setTimeout(function () { inp.focus(); }, 60);
    }
  }

  function signOne(item, extra) {
    S.busy = true;
    paintState();
    var payload = { item: item };
    Object.keys(extra || {}).forEach(function (k) { payload[k] = extra[k]; });
    return api('/api/cx/sign', payload).then(function (d) {
      S.busy = false;
      if (d && d.ok) {
        toast((item.courseName || '') + ' ' + (item.name || '') + ' 签到成功', 'ok');
        return doScan();
      }
      // 缺东西：按 missing 弹对应输入框，填完再签一次
      var needs = (d && d.needs) || [];
      if (needs.indexOf('手势') >= 0) {
        paintState();
        askInput('手势', '按九宫格顺序填数字，左上角是 1，右下角是 9。', '例如 12369', function (v) {
          signOne(item, { pattern: v });
        });
        return;
      }
      if (needs.indexOf('签到码') >= 0) {
        paintState();
        var n = item.numberCount || 0;
        askInput('签到码', n ? ('老师给的是 ' + n + ' 位数字。') : '', '签到码', function (v) {
          signOne(item, { code: v });
        });
        return;
      }
      if (needs.indexOf('二维码') >= 0) {
        paintState();
        askInput('二维码', '把老师屏幕上那个二维码的链接粘进来（手机扫码后能看到的地址）。',
          'https://mobilelearn.chaoxing.com/widget/sign/e?id=...', function (v) {
            api('/api/cx/qr_parse', { text: v }).then(function (p) {
              if (p && p.ok && p.data) {
                signOne(item, { enc: p.data.enc });
              } else {
                toast((p && p.error) || '二维码解析失败', 'warn');
              }
            });
          });
        return;
      }
      if (needs.indexOf('验证码') >= 0) {
        paintState();
        askInput('验证码', '老师开了防作弊，需要图形验证码。填服务端给的验证串。',
          'validate', function (v) {
            signOne(item, { validate: v, enc: (d && d.enc2) || '' });
          });
        return;
      }
      if (needs.indexOf('位置') >= 0) {
        paintState();
        askInput('位置', '位置签到要地址和坐标。按「地址,纬度,经度」填，例如 翡翠湖校区,31.86,117.28。',
          '地址,纬度,经度', function (v) {
            var parts = v.split(',');
            if (parts.length < 3) { toast('格式不对', 'warn'); return; }
            signOne(item, {
              address: parts[0].trim(),
              lat: parseFloat(parts[1]),
              lng: parseFloat(parts[2])
            });
          });
        return;
      }
      paintState();
      toast((d && d.detail) || (d && d.error) || '签到失败', 'warn');
    });
  }

  function signAll() {
    var auto = liveSigns().filter(function (x) { return x.canAuto; });
    if (!auto.length) { return; }
    S.busy = true;
    paintState();
    api('/api/cx/sign_all', {}).then(function (d) {
      S.busy = false;
      if (!d || d.ok === false) {
        toast((d && d.error) || '一键签到失败', 'warn');
        paintState();
        return;
      }
      var ok = (d.done || []).length;
      var bad = (d.failed || []).length;
      var skip = (d.skipped || []).length;
      toast('签到完成：成功 ' + ok
        + (bad ? '，失败 ' + bad : '')
        + (skip ? '，跳过 ' + skip : ''),
        bad ? 'warn' : 'ok');
      doScan();
    });
  }

  /* --------------------------------------------------------------- 接线 */
  function wire() {
    var el;
    if ((el = $('cxLoginBtn'))) { el.addEventListener('click', openLogin); }
    if ((el = $('cxLoginCancel'))) { el.addEventListener('click', closeLogin); }
    if ((el = $('cxLoginSubmit'))) { el.addEventListener('click', submitLogin); }
    if ((el = $('cxPassword'))) {
      el.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { submitLogin(); }
      });
    }
    if ((el = $('cxLoginBackdrop'))) {
      el.addEventListener('click', function (e) {
        if (e.target === el) { closeLogin(); }
      });
    }

    if ((el = $('cxScanBtn'))) { el.addEventListener('click', doScan); }
    if ((el = $('cxSignAll'))) { el.addEventListener('click', signAll); }

    if ((el = $('cxLogout'))) {
      el.addEventListener('click', function () {
        api('/api/cx/logout', {}).then(function () {
          S.signs = [];
          S.loaded = false;
          toast('已退出学习通', 'ok');
          refresh();
        });
      });
    }

    if ((el = $('cxInputCancel'))) {
      el.addEventListener('click', function () {
        if ($('cxInputBackdrop')) { $('cxInputBackdrop').classList.remove('open'); }
      });
    }

    // 列表按钮用事件委托，行是动态渲染的
    if ((el = $('cxSigns'))) {
      el.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-act="sign"]');
        if (!btn) { return; }
        var i = Number(btn.dataset.i);
        var item = S.signs[i];
        if (item) { signOne(item); }
      });
    }

    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') { return; }
      closeLogin();
      if ($('cxInputBackdrop')) { $('cxInputBackdrop').classList.remove('open'); }
    });
  }

  function start() {
    wire();
    refresh().then(function () {
      // 登录了就先扫一次，省得用户还得手点
      if (S.loggedIn) { doScan(); }
    });
    // 和雨课堂一样，后台盯着——签到是有时间窗的
    S.pollTimer = setInterval(function () {
      if (document.hidden) { return; }
      refresh();
    }, 12000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  window.HFUTCx = {
    refresh: refresh,
    scan: doScan,
    state: function () { return S; }
  };
})();
