/* ============================================================================
   HFUTone · 学习通签到一栏
   只做界面与调用：协议在 web/server.py 的 /api/cx/* 后面，实现在
   src/cxapi.py（接口）与 src/cxsign.py（编排）。
   ========================================================================== */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var AUTO = { timer: 0, lastSign: 0, running: false };

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
        +       (x.fromChat ? ' · 来自班级群' : '')
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
  window.cxRefresh = function () {
    // im.js 收到群消息后用它把列表刷一遍
    if (typeof doScan === 'function') { doScan(); }
  };

  /* 自动刷新的两个开关存在 settings.json 里（cx.autoScan / cx.autoSign）。
     设置面板改完会调 window.cxApplySettings 把新值同步过来。 */
  function loadAutoCfg() {
    api('/api/settings').then(function (d) {
      if (d && d.settings) { applySettings(d.settings); }
    }).catch(function () { /* 拿不到就用默认：刷新开、签到关 */ });
  }

  function applySettings(cfg) {
    var cx = (cfg || {}).cx || {};
    S.cfg = { autoScan: cx.autoScan !== false, autoSign: !!cx.autoSign };
    // 安卓不做自动签到，即使配置里是 true 也按 false 走
    if (document.body.classList.contains('is-android')) { S.cfg.autoSign = false; }
    startAutoScan();
  }

  window.cxApplySettings = applySettings;

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
      if (d && d.ok) { autoSignIfEnabled(); }
    });
  }

  /* 自动签到：只在桌面版、且设置里勾了「自动签到」时执行。
     安卓不做（README 里有说明），所以这里先判一次平台。
     串行提交，避免几个签到同时打出去。 */
  function autoSignIfEnabled() {
    if (document.body.classList.contains('is-android')) { return; }
    if (!S.cfg || !S.cfg.autoSign) { return; }
    if (AUTO.running) { return; }
    var targets = liveSigns().filter(function (x) { return x.canAuto && !x.signed; });
    if (!targets.length) { return; }
    AUTO.running = true;
    toast('自动签到：' + targets.length + ' 条', 'ok');
    var i = 0;
    function next() {
      if (i >= targets.length) {
        AUTO.running = false;
        AUTO.lastSign = Date.now();
        return;
      }
      var it = targets[i++];
      signOne(it, {}).then(function () { next(); }, function () { next(); });
    }
    next();
  }

  /* 自动刷新。
     踩过的坑：原来用 setInterval(…, 10000)，但一次扫描要 15~17 秒
     （27 门课串行请求），于是扫描永远排不上间隙 —— 界面一直「正在处理」，
     按钮一直灰，用户点什么都要排队等扫描让路。
     改成「上一轮跑完再等」，并且间隔放到 60 秒；用户正在操作时直接跳过。 */
  var AUTO_GAP_MS = 60000;

  function startAutoScan() {
    if (AUTO.timer) { clearTimeout(AUTO.timer); AUTO.timer = 0; }
    function tick() {
      AUTO.timer = 0;
      if (!S.loggedIn || S.busy || AUTO.running) { schedule(); return; }
      if (document.hidden) { schedule(); return; }
      if (!S.cfg || S.cfg.autoScan === false) { schedule(); return; }
      doScan().then(schedule, schedule);
    }
    function schedule() {
      if (AUTO.timer) { return; }
      AUTO.timer = setTimeout(tick, AUTO_GAP_MS);
    }
    schedule();
  }

  /* 用户正在手动操作（签到/取验证码）时，把自动扫描按住，
     免得抢同一条 HTTP 通道让人干等。 */
  function pauseAuto() {
    if (AUTO.timer) { clearTimeout(AUTO.timer); AUTO.timer = 0; }
  }

  function resumeAuto() {
    if (!AUTO.timer && S.cfg && S.cfg.autoScan !== false) {
      AUTO.timer = setTimeout(function () { startAutoScan(); }, 3000);
    }
  }

  /* --------------------------------------------------------- 签到与补输入 */

  /* ---------------------------------------------------- 滑块验证码
     学习通的 showVCode=1 要求先过 captcha.chaoxing.com 的滑块，换到 validate
     才允许签到。取图/验图都走后端（图片要用 Referer，前端跨域拿不到）。

     x 的算法照 course_helper：把拖到的位置换算成设计稿坐标，再乘 280/264 减 8，
     得到缺口左边界（原图像素）。 */
  var CAP = { token: "", iv: "", ts: 0, referer: "", onOk: null, x: 0, dragging: false };

  function capReset() {
    var slice = $("cxCapSlice");
    if (slice) { slice.style.left = "0px"; }
    CAP.x = 0;
    var msg = $("cxCapMsg");
    if (msg) { msg.textContent = ""; msg.className = "err-line"; }
  }

  function capLoad() {
    capReset();
    var bg = $("cxCapBg");
    var slice = $("cxCapSlice");
    if (bg) { bg.removeAttribute("src"); }
    if (slice) { slice.removeAttribute("src"); }
    var msg = $("cxCapMsg");
    if (msg) { msg.textContent = "正在取验证码…"; }
    api("/api/cx/captcha/start?referer=" + encodeURIComponent(CAP.referer)).then(function (d) {
      if (!d || !d.ok) {
        if (msg) { msg.textContent = (d && d.error) || "取验证码失败"; }
        return;
      }
      CAP.token = d.token; CAP.iv = d.iv; CAP.ts = d.ts;
      CAP.originalWidth = d.width || 320;
      if (bg) { bg.src = d.bg; }
      if (slice) { slice.src = d.slice; }
      if (msg) { msg.textContent = ""; }
    }).catch(function (e) {
      if (msg) { msg.textContent = "取验证码失败：" + e; }
    });
  }

  function capShow() {
    var b = $("cxCapBackdrop");
    if (b) { b.classList.add("open"); }
  }

  function capHide() {
    var b = $("cxCapBackdrop");
    if (b) { b.classList.remove("open"); }
  }

  function capOpen(referer, onOk) {
    CAP.referer = referer || "https://mobilelearn.chaoxing.com/";
    CAP.onOk = onOk;
    pauseAuto();          // 取图/验图期间别让自动刷新抢通道
    capShow();
    capLoad();
  }

  function capDragStart(e) {
    CAP.dragging = true;
    CAP.startX = e.clientX != null ? e.clientX : (e.touches && e.touches[0].clientX) || 0;
    CAP.baseLeft = parseFloat(($("cxCapSlice") || {}).style.left) || 0;
  }

  function capDragMove(e) {
    if (!CAP.dragging) { return; }
    var stage = $("cxCapStage");
    var slice = $("cxCapSlice");
    if (!stage || !slice) { return; }
    var cx0 = e.clientX != null ? e.clientX : (e.touches && e.touches[0].clientX) || 0;
    var maxLeft = stage.clientWidth - slice.clientWidth;
    var left = Math.max(0, Math.min(maxLeft, CAP.baseLeft + (cx0 - CAP.startX)));
    slice.style.left = left + "px";
    CAP.x = left;
    if (e.cancelable) { e.preventDefault(); }
  }

  function capDragEnd() { CAP.dragging = false; }

  function capSubmit() {
    var stage = $("cxCapStage");
    var slice = $("cxCapSlice");
    var msg = $("cxCapMsg");
    if (!stage || !slice || !CAP.token) {
      if (msg) { msg.textContent = "验证码还没加载好"; }
      return;
    }
    var left = parseFloat(slice.style.left) || 0;
    // course_helper 的公式，一步不少：
    //   显示位置 -> 设计稿(320 宽) -> 缺口左边界(乘 280/264 再减 8) -> 原图坐标(乘 originalWidth/320)
    var design = left * (320 / stage.clientWidth);
    var cutout = design * (280 / 264) - 8;
    var ow = CAP.originalWidth || 320;
    var x = cutout * (ow / 320);
    if (x < 0) { x = 0; }
    if (msg) { msg.textContent = "正在验证…"; }
    api("/api/cx/captcha/verify", {
      x: Math.round(x), token: CAP.token, iv: CAP.iv, ts: CAP.ts, referer: CAP.referer
    }).then(function (d) {
      if (d && d.ok && d.validate) {
        capHide();
        var cb = CAP.onOk; CAP.onOk = null;
        if (cb) { cb(d.validate); }
      } else {
        if (msg) { msg.textContent = (d && d.error) || "验证没通过，换一张再试"; }
        capLoad();
      }
    }).catch(function (e) {
      if (msg) { msg.textContent = "验证失败：" + e; }
    });
  }

  function askInput(kind, lead, placeholder, onOk, onImage) {
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

      /* 二维码签到可以直接粘截图：剪贴板里有图就读成 dataURL 交给后端解码，
         省掉「先存文件→再扫码→再抄链接」那一圈。拖进来同样接。 */
      var takeImage = function (file) {
        if (!file || !onImage) { return false; }
        var fr = new FileReader();
        fr.onload = function () {
          $('cxInputBackdrop').classList.remove('open');
          onImage(String(fr.result || ''));
        };
        fr.readAsDataURL(file);
        return true;
      };
      inp.onpaste = function (e) {
        var items = (e.clipboardData && e.clipboardData.items) || [];
        for (var i = 0; i < items.length; i++) {
          if (items[i].type && items[i].type.indexOf('image') === 0) {
            e.preventDefault();
            takeImage(items[i].getAsFile());
            return;
          }
        }
      };
      inp.ondragover = function (e) { if (onImage) { e.preventDefault(); } };
      inp.ondrop = function (e) {
        if (!onImage) { return; }
        e.preventDefault();
        var files = (e.dataTransfer && e.dataTransfer.files) || [];
        if (files.length) { takeImage(files[0]); }
      };
    }
  }

  function signOne(item, extra) {
    pauseAuto();
    // 已知需要滑块验证码的项：先弹滑块，避免白提交一次换来「环境异常」
    if (item && item.needCaptcha && !(extra && extra.validate)) {
      capOpen(item.url || 'https://mobilelearn.chaoxing.com/', function (v) {
        signOne(item, { validate: v, enc: (extra && extra.enc) || '' });
      });
      return Promise.resolve({ ok: false, status: 'need' });
    }
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
        // 学习通没有「App 头免签」这条路，只能把二维码本身拿到手：
        //   1) 桌面版已自动从课件画面里抓过了（后端 qr_from_relay），到这里说明没抓到
        //   2) 截图/图片粘进来（走 /api/cx/qr_image，OpenCV 解码）
        //   3) 手机扫码后把链接粘进来（走 /api/cx/qr_parse）
        askInput('二维码',
          '把二维码图片粘贴/拖进这个框，或粘手机扫码后看到的链接。',
          'Ctrl+V 粘截图，或 https://mobilelearn.chaoxing.com/widget/sign/e?id=...',
          function (v) {
            if (/^https?:\/\//i.test(v)) {
              api('/api/cx/qr_parse', { text: v }).then(function (p) {
                if (p && p.ok && p.data) {
                  signOne(item, { enc: p.data.enc, code: p.data.code || '' });
                } else {
                  toast((p && p.error) || '二维码解析失败', 'warn');
                }
              });
              return;
            }
            toast('看起来不是链接，试试粘贴二维码图片', 'warn');
          },
          function (imageDataUrl) {
            api('/api/cx/qr_image', { image: imageDataUrl }).then(function (p) {
              if (p && p.ok && p.data) {
                toast('已从图片里读到签到码', 'ok');
                signOne(item, { enc: p.data.enc, code: p.data.code || '' });
              } else {
                toast((p && p.error) || '图里没找到学习通签到码', 'warn');
              }
            });
          });
        return;
      }
      if (needs.indexOf('验证码') >= 0) {
        paintState();
        // 坐标校准可能已经在失败重试里跑过了，结果随 d.calibrated 回来。
        // 先把它落盘，滑完验证码重交时才用得上（否则又差 800 多米）。
        if (d && d.calibrated && d.calibrated.lat && d.calibrated.lng) {
          item.lat = d.calibrated.lat;
          item.lng = d.calibrated.lng;
          // saveSettings 在 ppt.js 的闭包里，cx.js 拿不到（和 openBackdrop 一样的坑），
          // 所以直接打 /api/settings。
          api('/api/settings', {
            cx: { location: { lat: d.calibrated.lat, lng: d.calibrated.lng } }
          }).catch(function () { /* 落盘失败不影响这次签到 */ });
          toast('坐标已自动校准到 ' + d.calibrated.lat.toFixed(6) + ', '
                + d.calibrated.lng.toFixed(6), 'ok');
        }
        // 学习通的验证码是滑块，不是手填字符串：取图 → 拖动 → 换 validate → 重交
        capOpen(item.url || 'https://mobilelearn.chaoxing.com/', function (validate) {
          signOne(item, { validate: validate, enc: (d && d.enc2) || '',
                          lat: item.lat, lng: item.lng });
        });
        return;
      }
      if (needs.indexOf('位置') >= 0) {
        paintState();
        askInput('位置', '位置签到要地址和坐标。按「地址,纬度,经度」填，例如 屯溪路校区,31.86,117.28。',
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
      var why = (d && d.detail) || (d && d.error) || '签到失败';
      // 「距教师指定签到地点 7820504.0 米」这种一眼就是坐标填错了，别只甩原文
      var m = /距教师指定签到地点\s*([\d.]+)\s*米/.exec(why);
      if (m && parseFloat(m[1]) > 50000) {
        why += '。这个距离明显不对，先去设置里检查位置签到坐标（经纬度别填成同一个）';
      }
      toast(why, 'warn');
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
    if ((el = $('cxCapSubmit'))) { el.addEventListener('click', capSubmit); }
    if ((el = $('cxCapRetry'))) { el.addEventListener('click', capLoad); }
    if ((el = $('cxCapCancel'))) {
      el.addEventListener('click', function () {
        CAP.onOk = null;
        capHide();
        resumeAuto();
      });
    }
    if ((el = $('cxCapSlice'))) {
      el.addEventListener('pointerdown', capDragStart);
      el.addEventListener('pointermove', capDragMove);
      el.addEventListener('pointerup', capDragEnd);
      el.addEventListener('pointercancel', capDragEnd);
    }
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
    loadAutoCfg();
    startAutoScan();
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
