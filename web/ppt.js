/* ============================================================================
   HFUTone · 设置 与 直播画面
   —— PPT 流用 ffmpeg 抽成 JPEG、WebSocket 推过来、canvas 逐帧画。
   页面里为什么没有 <video>：这台机器的 QtWebEngine 是 Chromium 83，
   没有 H.264/AAC（canPlayType 空串、MSE 不认 avc1），m3u8/rtmp 播不了。
   ========================================================================== */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  function api(path, body) {
    var opt = { headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) { opt.method = 'POST'; opt.body = JSON.stringify(body); }
    return fetch(path, opt).then(function (r) { return r.json(); })
      .catch(function () { return { ok: false }; });
  }

  function toast(msg, kind) {
    var wrap = $('toasts');
    if (!wrap) { return; }
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? ' is-' + kind : '');
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(function () { el.classList.add('is-out'); }, 2600);
    setTimeout(function () { el.remove(); }, 3100);
  }

  var S = {
    cfg: null, st: null, ws: null, retry: 0,
    wasRunning: false, got: 0, t0: 0, fps: 0, drew: false, lag: 0, lagN: 0
  };

  /* ------------------------------------------------------------ 设置 */
  var backdrop = $('settingsBackdrop');

  function fillSettings(cfg) {
    S.cfg = cfg || {};
    var s = S.cfg;
    if ($('setAutoPlay')) { $('setAutoPlay').checked = s.autoPlay !== false; }
    var st = (s.streams || {});
    if ($('setTeacher')) { $('setTeacher').checked = st.teacher !== false; }
    if ($('setStudent')) { $('setStudent').checked = !!st.student; }
    if ($('setPpt')) { $('setPpt').checked = !!st.ppt; }

    // 雨课堂签到通道：三选一
    var mode = ((s.ykt || {}).signMode || 'both');
    ['both', 'scan', 'app'].forEach(function (m) {
      var el = $('setSign' + m.charAt(0).toUpperCase() + m.slice(1));
      if (el) { el.checked = (m === mode); }
    });

    // 学习通位置签到的默认坐标
    var loc = ((s.cx || {}).location || {});
    if ($('setCxAddress')) { $('setCxAddress').value = loc.address || ''; }
    if ($('setCxLat')) {
      $('setCxLat').value = (loc.lat === 0 || loc.lat == null) ? '' : loc.lat;
    }
    if ($('setCxLng')) {
      $('setCxLng').value = (loc.lng === 0 || loc.lng == null) ? '' : loc.lng;
    }
  }

  function loadSettings() {
    return api('/api/settings').then(function (d) {
      if (d && d.settings) { fillSettings(d.settings); }
    });
  }

  function saveSettings(patch, note) {
    return api('/api/settings', patch).then(function (d) {
      if (d && d.ok) {
        fillSettings(d.settings);
        $('settingsMsg').textContent = note || '';
        setTimeout(function () { $('settingsMsg').textContent = ''; }, 1600);
      } else {
        $('settingsMsg').textContent = '没能写入配置';
      }
    });
  }

  function openSettings() {
    loadSettings();
    backdrop.classList.add('open');
    setTimeout(function () { var f = $('setAutoPlay'); if (f) { f.focus(); } }, 30);
  }

  function closeSettings() {
    backdrop.classList.remove('open');
  }

  if (backdrop) {
    backdrop.addEventListener('click', function (e) {
      if (e.target === backdrop) { closeSettings(); }
    });
    $('settingsDone').addEventListener('click', closeSettings);

    $('setAutoPlay').addEventListener('change', function () {
      saveSettings({ autoPlay: this.checked },
        this.checked ? '已开启自动推流' : '已关闭自动推流');
    });
    $('setTeacher').addEventListener('change', function () {
      saveSettings({ streams: { teacher: this.checked } },
        this.checked ? '老师流：开' : '老师流：关');
    });
    $('setStudent').addEventListener('change', function () {
      saveSettings({ streams: { student: this.checked } },
        this.checked ? '学生流：开' : '学生流：关');
    });
    if ($('setPpt')) {
      $('setPpt').addEventListener('change', function () {
        var on = this.checked;
        saveSettings({ streams: { ppt: on } },
          on ? 'PPT 也开独立窗口：开（下一轮自动补开）' : 'PPT 独立窗口：关');
      });
    }

    // 雨课堂签到通道：三选一，点完立刻存
    ['both', 'scan', 'app'].forEach(function (m) {
      var el = $('setSign' + m.charAt(0).toUpperCase() + m.slice(1));
      if (!el) { return; }
      el.addEventListener('change', function () {
        if (!this.checked) { return; }
        var label = { both: '两条都试', scan: '只走扫码', app: '只走 App 头' }[m];
        saveSettings({ ykt: { signMode: m } }, '雨课堂签到：' + label);
      });
    });

    // 学习通位置坐标：三个字段一起存，输入停 500ms 再写盘，
    // 免得每敲一个数字就打一次请求
    var locTimer = null;
    function saveLoc() {
      var addr = $('setCxAddress') ? $('setCxAddress').value.trim() : '';
      var lat = $('setCxLat') ? parseFloat($('setCxLat').value) : NaN;
      var lng = $('setCxLng') ? parseFloat($('setCxLng').value) : NaN;
      var patch = { cx: { location: { address: addr } } };
      if (!isNaN(lat)) { patch.cx.location.lat = lat; }
      if (!isNaN(lng)) { patch.cx.location.lng = lng; }
      saveSettings(patch, '位置坐标已保存');
    }
    function queueSaveLoc() {
      if (locTimer) { clearTimeout(locTimer); }
      locTimer = setTimeout(saveLoc, 500);
    }
    ['setCxAddress', 'setCxLat', 'setCxLng'].forEach(function (id) {
      var el = $(id);
      if (!el) { return; }
      el.addEventListener('input', queueSaveLoc);
      el.addEventListener('change', queueSaveLoc);
    });
  }

  var btnSettings = $('btnSettings');
  if (btnSettings) { btnSettings.addEventListener('click', openSettings); }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && backdrop && backdrop.classList.contains('open')) {
      closeSettings();
      e.stopPropagation();
    }
  }, true);

  /* ------------------------------------------------------------ 画面 */
  var card = $('liveCard');
  var stage = $('liveStage');
  var canvas = $('liveCanvas');
  var ctx = canvas ? canvas.getContext('2d') : null;
  var meta = $('liveMeta');
  var stat = $('liveStat');
  var emptyText = $('liveEmptyText');
  var btnStart = $('liveStart');
  var btnStop = $('liveStop');
  var lagEl = $('liveLag');
  var zoomOn = false;
  var zoomCtx = ($('liveZoomCanvas') || {}).getContext
    ? $('liveZoomCanvas').getContext('2d') : null;

  function sideOpen() {
    var el = $('viewYkt');
    return !!el && el.classList.contains('is-open');
  }

  function setEmpty(text, showButton) {
    if (emptyText && text) { emptyText.textContent = text; }
    if (btnStart) { btnStart.hidden = !showButton; }
  }

  /* 画面按容器等比缩放。
     背景：canvas 的内部分辨率是源分辨率（1280×720 或老师的实际推流尺寸），
     CSS 只给 max-width/max-height 时，flex 布局里两条约束会互相抵消——
     实测表现就是"高度跟着变、宽度不跟"，画面要么左右留黑边要么被压扁。
     这里用同一个缩放系数算宽高，宽和高必然同步。容器尺寸变了就重算。 */
  function fit() {
    if (!canvas || !stage) { return; }
    var cw = canvas.width, chh = canvas.height;
    var bw = stage.clientWidth, bh = stage.clientHeight;
    if (!cw || !chh || !bw || !bh) { return; }
    var k = Math.min(bw / cw, bh / chh);
    var w = Math.max(1, Math.round(cw * k));
    var h = Math.max(1, Math.round(chh * k));
    if (canvas.style.width !== w + 'px') { canvas.style.width = w + 'px'; }
    if (canvas.style.height !== h + 'px') { canvas.style.height = h + 'px'; }
  }

  if (typeof ResizeObserver === 'function' && stage) {
    new ResizeObserver(fit).observe(stage);
  }
  window.addEventListener('resize', fit);

  // 点画面放大：卡片只有 310px 宽，1080p 的 PPT 小字要铺满窗口才看得清。
  // canvas 本身搬到放大层里（同一块画布，搬运不丢像素），关掉再搬回卡片。
  var zoomLayer = $('liveZoom');
  var zoomCanvas = $('liveZoomCanvas');

  function setZoom(on) {
    if (!canvas || !zoomLayer) { return; }
    zoomOn = !!on;
    if (zoomOn) {
      zoomLayer.hidden = false;
      zoomCanvas.width = canvas.width;
      zoomCanvas.height = canvas.height;
      zoomCtx.drawImage(canvas, 0, 0);
      zoomLayer.classList.add('is-on');
    } else {
      zoomLayer.hidden = true;
      zoomLayer.classList.remove('is-on');
    }
  }

  if (stage) {
    stage.addEventListener('click', function () {
      if (!stage.classList.contains('is-live')) { return; }
      setZoom(!zoomOn);
    });
  }
  if (zoomLayer) {
    zoomLayer.addEventListener('click', function () { setZoom(false); });
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && zoomOn) { setZoom(false); }
  }, true);

  function paintFrame(buf) {
    if (!buf || !buf.byteLength) { return; }

    // 服务端在每帧前面塞了 8 字节小端 double：这帧从 ffmpeg 管道出来的时刻。
    // 拿它减当前时间，就是"ffmpeg 出图 → 画到 canvas 上"的真实延迟。
    var lagMs = -1;
    var img = buf;
    if (buf.byteLength > 8) {
      try {
        var stamp = new DataView(buf, 0, 8).getFloat64(0, true);
        if (stamp > 1e9) {
          lagMs = (Date.now() / 1000 - stamp) * 1000;
          img = buf.slice(8);
        }
      } catch (err) { lagMs = -1; }
    }
    var blob = new Blob([img], { type: 'image/jpeg' });

    function put(bmp, w, h, close) {
      if (!w || !h) { return; }
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;                   // 内部分辨率 = 源分辨率，缩放到 344px 显示
        canvas.height = h;
      }
      try {
        ctx.drawImage(bmp, 0, 0, w, h);
        if (zoomOn && zoomCtx) {
          if (zoomCanvas.width !== w || zoomCanvas.height !== h) {
            zoomCanvas.width = w;
            zoomCanvas.height = h;
          }
          zoomCtx.drawImage(bmp, 0, 0, w, h);
        }
      } catch (err) { return; }
      if (close && bmp.close) { bmp.close(); }
      if (!S.drew) {
        S.drew = true;
        stage.classList.add('is-live');
        setEmpty('', false);
      }
      fit();          // 源分辨率可能中途变化（换摄像头/换推流尺寸），重算缩放
      S.got += 1;
      if (lagMs >= 0) {
        S.lag = S.lag > 0 ? S.lag * 0.85 + lagMs * 0.15 : lagMs;   // 平滑，免得数字乱跳
        S.lagN += 1;
        paintLag();
      }
      var now = performance.now();
      if (!S.t0) { S.t0 = now; }
      if (now - S.t0 >= 1000) {
        S.fps = S.got * 1000 / (now - S.t0);
        S.got = 0;
        S.t0 = now;
        paintStat();
      }
    }

    if (window.createImageBitmap) {
      window.createImageBitmap(blob).then(function (bmp) {
        put(bmp, bmp.width, bmp.height, true);
      }).catch(function () { viaImage(blob, put); });
    } else {
      viaImage(blob, put);
    }
  }

  function viaImage(blob, put) {
    var url = URL.createObjectURL(blob);
    var img = new Image();
    img.onload = function () {
      put(img, img.naturalWidth, img.naturalHeight, false);
      URL.revokeObjectURL(url);
    };
    img.onerror = function () { URL.revokeObjectURL(url); };
    img.src = url;
  }

  function paintLag() {
    if (lagEl) {
      lagEl.hidden = false;
      lagEl.textContent = '延迟 ' + Math.round(S.lag) + ' ms';
    }
    window.__pptLatency = { ms: Math.round(S.lag), n: S.lagN };
  }

  function paintStat() {
    if (!stat) { return; }
    if (!S.drew) { stat.textContent = '—'; return; }
    var dim = canvas.width + '×' + canvas.height;
    stat.textContent = S.fps.toFixed(0) + ' fps · ' + dim;
  }

  function teardown() {
    if (S.ws) {
      try { S.ws.close(); } catch (err) { /* 已经在关了 */ }
      S.ws = null;
    }
    if (S.retry) { clearTimeout(S.retry); S.retry = 0; }
    S.drew = false;
    S.got = 0;
    S.t0 = 0;
    S.fps = 0;
    if (stage) { stage.classList.remove('is-live'); }
    if (stat) { stat.textContent = '—'; }
    if (lagEl) { lagEl.hidden = true; lagEl.textContent = '—'; }
    S.lag = 0;
    S.lagN = 0;
    if (btnStop) { btnStop.hidden = true; }
  }

  function connect() {
    if (S.ws && (S.ws.readyState === 0 || S.ws.readyState === 1)) { return; }
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var ws;
    try {
      ws = new WebSocket(proto + '//' + location.host + '/api/live/ppt/ws');
    } catch (err) {
      return;
    }
    ws.binaryType = 'arraybuffer';
    S.ws = ws;
    S.drew = false;
    S.got = 0;
    S.t0 = 0;
    ws.onmessage = function (ev) { paintFrame(ev.data); };
    ws.onerror = function () { /* onclose 会兜住 */ };
    ws.onclose = function () {
      if (S.ws === ws) { S.ws = null; }
      if (S.retry) { clearTimeout(S.retry); }
      S.retry = setTimeout(function () {
        if (S.st && S.st.ppt && S.st.ppt.running) { connect(); }
      }, 1500);
    };
  }

  var PHASE_TEXT = {
    waiting: '还没有课程开播。开播后画面会自动接上来。',
    starting: '画面还没推上来，正在等…',
    'no-ppt': '这堂课没有 PPT 流。',
    off: '自动推流已关闭，可以在「设置」里打开。',
    stopped: '已停止拉取画面。',
    error: '画面接入失败，稍后会自动重试。'
  };

  function apply(d) {
    S.st = d;
    var ppt = d.ppt || {};
    var auto = d.auto || {};
    var running = !!ppt.running;

    if (btnStop) { btnStop.hidden = !running; }

    if (running) {
      if (meta) { meta.textContent = '直播中'; }
      if (!S.drew) { setEmpty('正在接画面…', false); }
      connect();
      // 第一次真的接上画面时，把侧栏打开——卡片在里面
      if (!S.wasRunning && !sideOpen() && window.HFUTYkt && window.HFUTYkt.setSide) {
        window.HFUTYkt.setSide(true);
      }
    } else {
      teardown();
      var phase = auto.phase || 'waiting';
      if (meta) { meta.textContent = phase === 'playing' ? '等待画面' : '未开始'; }
      // error 时有具体原因（比如三流模式下 PPT 没流），优先把它显示出来
      var text = (phase === 'error' && auto.message)
        ? auto.message : (PHASE_TEXT[phase] || '还没有直播画面。');
      setEmpty(text, phase === 'waiting' || phase === 'off' || phase === 'no-ppt');
    }
    S.wasRunning = running;
  }

  function poll() {
    api('/api/live/state').then(function (d) {
      if (d && d.ok !== false) { apply(d); }
    });
  }

  if (btnStart) {
    btnStart.addEventListener('click', function () {
      btnStart.disabled = true;
      btnStart.textContent = '拉取中…';
      api('/api/live/ppt/start', {}).then(function (d) {
        btnStart.disabled = false;
        btnStart.textContent = '手动拉取';
        if (d && d.ok) { poll(); }
        else { toast((d && d.error) || '没有可拉取的画面'); }
      });
    });
  }

  if (btnStop) {
    btnStop.addEventListener('click', function () {
      api('/api/live/ppt/stop', {}).then(function () { poll(); });
    });
  }

  if (card) {
    poll();
    setInterval(poll, 3000);
  }

  window.HFUTLivePpt = { openSettings: openSettings, poll: poll };
})();