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

    // 雨课堂签到通道与自动签到
    var yktcfg = (s.ykt || {});
    var mode = (yktcfg.signMode || 'both');
    ['both', 'scan', 'app'].forEach(function (m) {
      var el = $('setSign' + m.charAt(0).toUpperCase() + m.slice(1));
      if (el) { el.checked = (m === mode); }
    });
    if ($('setYktAutoSign')) { $('setYktAutoSign').checked = !!yktcfg.autoSign; }

    // 学习通签到自动化
    var cxcfg = (s.cx || {});
    if ($('setCxAutoScan')) { $('setCxAutoScan').checked = cxcfg.autoScan !== false; }
    if ($('setCxAutoSign')) { $('setCxAutoSign').checked = !!cxcfg.autoSign; }

    // 自动签到调度与 Webhook 推送
    var acfg = (s.autoSign || {});
    if ($('setAutoPollSec')) { $('setAutoPollSec').value = String(acfg.pollIntervalSec || 180); }
    if ($('setAutoWebhook')) { $('setAutoWebhook').value = acfg.webhookUrl || ''; }

    // 学习通位置签到的默认坐标
    var loc = ((s.cx || {}).location || {});
    if ($('setCxAddress')) { $('setCxAddress').value = loc.address || ''; }
    // 回填固定 6 位小数：学习通/腾讯拾取器给的坐标都是 6 位，少位会让人
    // 以为精度被截掉了（之前直接赋 number，31.860000 会显示成 31.86）
    function fmt6(v) {
      return (v === 0 || v == null) ? '' : Number(v).toFixed(6);
    }
    if ($('setCxLat')) { $('setCxLat').value = fmt6(loc.lat); }
    if ($('setCxLng')) { $('setCxLng').value = fmt6(loc.lng); }
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
        if (window.cxApplySettings) { window.cxApplySettings(d.settings); }
        if (window.refreshAutoSignState) { window.refreshAutoSignState(); }
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

    if ($('setYktAutoSign')) {
      $('setYktAutoSign').addEventListener('change', function () {
        saveSettings({ ykt: { autoSign: this.checked } },
          this.checked ? '已开启雨课堂自动签到' : '已关闭雨课堂自动签到');
      });
    }
    if ($('setCxAutoScan')) {
      $('setCxAutoScan').addEventListener('change', function () {
        saveSettings({ cx: { autoScan: this.checked } },
          this.checked ? '已开启自动刷新签到' : '已关闭自动刷新签到');
      });
    }
    if ($('setCxAutoSign')) {
      $('setCxAutoSign').addEventListener('change', function () {
        var on = this.checked;
        saveSettings({ cx: { autoSign: on } },
          on ? '已开启学习通自动签到（IM 秒签 + 低频兜底）' : '已关闭学习通自动签到');
        if (on && window.HFUToneIM) {
          try { window.HFUToneIM.connect(false); } catch (e) { /* 忽略 */ }
        }
      });
    }

    var autoCfgTimer = null;
    if ($('setAutoPollSec')) {
      $('setAutoPollSec').addEventListener('change', function () {
        var sec = parseInt(this.value, 10);
        if (!isFinite(sec)) { sec = 180; }
        sec = Math.max(60, Math.min(600, sec));
        this.value = String(sec);
        saveSettings({ autoSign: { pollIntervalSec: sec } },
          '低频轮询间隔已设为 ' + sec + ' 秒');
      });
    }
    if ($('setAutoWebhook')) {
      var saveWebhook = function () {
        var u = ($('setAutoWebhook').value || '').trim();
        if (u && !/^https?:\/\//i.test(u)) {
          var m = $('webhookTestMsg');
          if (m) { m.textContent = '地址需以 http:// 或 https:// 开头'; }
          return;
        }
        saveSettings({ autoSign: { webhookUrl: u } },
          u ? 'Webhook 推送地址已保存' : '已清除 Webhook 推送地址');
      };
      $('setAutoWebhook').addEventListener('input', function () {
        if (autoCfgTimer) { clearTimeout(autoCfgTimer); }
        autoCfgTimer = setTimeout(saveWebhook, 600);
      });
      $('setAutoWebhook').addEventListener('change', saveWebhook);
    }
    if ($('btnTestWebhook')) {
      $('btnTestWebhook').addEventListener('click', function () {
        var btn = this;
        var msg = $('webhookTestMsg');
        var u = $('setAutoWebhook') ? $('setAutoWebhook').value.trim() : '';
        btn.disabled = true;
        if (msg) { msg.textContent = '发送中…'; }
        api('/api/autosign/test_webhook', { url: u }).then(function (d) {
          btn.disabled = false;
          if (msg) {
            msg.textContent = (d && d.ok)
              ? ('推送成功（' + (d.detail || 'OK') + '）')
              : ('推送失败：' + ((d && d.detail) || '未知错误'));
          }
        });
      });
    }

    // 学习通位置坐标：三个字段一起存，输入停 500ms 再写盘，
    // 免得每敲一个数字就打一次请求
    var locTimer = null;

    /* 坐标防呆。真实踩过的坑：纬度和经度被填成同一个值（31.847218 / 31.847218），
       签到提交后才从上距里看到「距教师指定签到地点 7820504.0 米」——那是 7800 公里。
       填错时宁可当场拒绝写盘，也别让它悄悄生效。 */
    function checkLatLng(lat, lng) {
      if (!isFinite(lat) || !isFinite(lng)) { return '经纬度都要填数字'; }
      if (lat < -90 || lat > 90) { return '纬度要在 -90 ~ 90 之间'; }
      if (lng < -180 || lng > 180) { return '经度要在 -180 ~ 180 之间'; }
      if (Math.abs(lat - lng) < 1e-9) { return '纬度和经度填成同一个值了，检查一下'; }
      // 国内常见范围：纬度 3~54、经度 73~136。两个都落在「像纬度」的区间 = 大概率填反
      if (lat > 3 && lat < 54 && lng > 3 && lng < 73) {
        return '经度看起来不像国内坐标（应在 73~136），是不是填反了';
      }
      return '';
    }

    function saveLoc() {
      var addr = $('setCxAddress') ? $('setCxAddress').value.trim() : '';
      // 统一到 6 位小数（1e-6 度约 0.1 米，再多一位没有意义）
      var lat = $('setCxLat') ? parseFloat(Number($('setCxLat').value).toFixed(6)) : NaN;
      var lng = $('setCxLng') ? parseFloat(Number($('setCxLng').value).toFixed(6)) : NaN;
      var why = checkLatLng(lat, lng);
      if (why) {
        var msg = $('settingsMsg');
        if (msg) { msg.textContent = why + '（没有保存）'; msg.className = 'err-line is-err'; }
        return;
      }
      var patch = { cx: { location: { address: addr } } };
      patch.cx.location.lat = lat;
      patch.cx.location.lng = lng;
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
    external: 'PPT 已按设置开在独立窗口里，侧栏不再内嵌。',
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
      setEmpty(text, phase === 'waiting' || phase === 'off'
        || phase === 'no-ppt' || phase === 'external');
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

  /* ------------------------------------------------ 自动签到守护状态面板 */
  var asPanel = $('autoSignPanel');
  var asLastId = 0;

  function renderAutoSign(d) {
    if (!asPanel || !d || d.ok === false) { return; }
    var dot = $('autoSignDot');
    var title = $('autoSignTitle');
    var meta = $('autoSignMeta');
    var box = $('autoSignEvents');
    var active = !!(d.cxAutoSign || d.yktAutoSign);

    if (dot) {
      dot.className = 'dot' + (active ? ' ok' : '');
    }
    if (title) {
      title.textContent = active ? '自动签到守护：运行中' : '自动签到守护：待机';
    }
    if (meta) {
      var st = d.stats || {};
      var parts = [
        '学习通：' + (d.cxAutoSign ? '开' : '关'),
        '雨课堂：' + (d.yktAutoSign ? '开' : '关'),
        '兜底 ' + (d.pollIntervalSec || 180) + 's',
        '已签 ' + (st.signed || 0)
      ];
      if (st.imTriggered) { parts.push('IM 秒签 ' + st.imTriggered); }
      if (st.needAction) { parts.push('待人工 ' + st.needAction); }
      if (d.webhookConfigured) { parts.push('推送已配'); }
      meta.textContent = parts.join(' · ');
    }

    var events = d.events || [];
    if (events.length && events[0].id > asLastId) {
      if (asLastId > 0) {
        var latest = events[0];
        if (latest.status === 'signed') {
          toast('[' + latest.trigger + '] 已签到：' + latest.name, 'ok');
        } else if (latest.status === 'need_action') {
          toast('需人工处理：' + latest.name + '（' + latest.detail + '）');
        }
      }
      asLastId = events[0].id;
    }

    if (!box) { return; }
    box.textContent = '';
    if (!events.length) {
      var empty = document.createElement('p');
      empty.className = 'autosign-empty';
      empty.textContent = active
        ? '守护线程运行中，等待 IM 通知或下一轮低频巡检。'
        : '在「设置」中开启学习通或雨课堂自动签到后，这里会实时记录秒签与巡检结果。';
      box.appendChild(empty);
      return;
    }
    var statusMap = {
      signed: ['已签', 'is-signed'],
      already: ['已签', 'is-signed'],
      need_action: ['需人工', 'is-need'],
      failed: ['失败', 'is-fail'],
      info: ['记录', '']
    };
    var platMap = { cx: '学习通', ykt: '雨课堂', system: '系统' };

    events.slice(0, 12).forEach(function (ev) {
      var row = document.createElement('div');
      row.className = 'autosign-row';
      var info = statusMap[ev.status] || ['记录', ''];

      var badge = document.createElement('span');
      badge.className = 'autosign-badge ' + info[1];
      badge.textContent = info[0];

      var body = document.createElement('div');
      body.className = 'autosign-row-body';

      var head = document.createElement('div');
      head.className = 'autosign-row-head';
      head.textContent = (ev.time || '') + ' · ' + (platMap[ev.platform] || ev.platform)
        + ' · ' + (ev.trigger || '轮询') + ' · ' + (ev.name || '');

      var det = document.createElement('div');
      det.className = 'autosign-row-detail';
      det.textContent = ev.detail || '';

      body.appendChild(head);
      if (ev.detail) { body.appendChild(det); }
      row.appendChild(badge);
      row.appendChild(body);
      box.appendChild(row);
    });
  }

  function refreshAutoSignState() {
    if (!asPanel) { return Promise.resolve(); }
    return api('/api/autosign/state').then(function (d) {
      renderAutoSign(d);
    });
  }

  if (asPanel) {
    var nowBtn = $('autoSignNowBtn');
    if (nowBtn) {
      nowBtn.addEventListener('click', function () {
        nowBtn.disabled = true;
        nowBtn.textContent = '检查中…';
        api('/api/autosign/trigger', { platform: 'all' }).then(function (d) {
          nowBtn.disabled = false;
          nowBtn.textContent = '立即检查';
          if (d && d.state) { renderAutoSign(d.state); }
          else { refreshAutoSignState(); }
        });
      });
    }
    var clearBtn = $('autoSignClearBtn');
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        api('/api/autosign/clear', {}).then(function (d) {
          if (d && d.state) { renderAutoSign(d.state); }
          else { refreshAutoSignState(); }
        });
      });
    }
    refreshAutoSignState();
    setInterval(refreshAutoSignState, 12000);
  }

  window.refreshAutoSignState = refreshAutoSignState;
  window.HFUTLivePpt = { openSettings: openSettings, poll: poll };
})();