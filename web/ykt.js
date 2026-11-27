/* ============================================================================
   HFUTone · 雨课堂一栏
   只做界面与调用：所有协议都在 web/server.py 的 /api/ykt/* 后面。
   ========================================================================== */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var state = { loggedIn: false, lessons: [], loaded: false, busy: false, error: '', user: {} };

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
    if (!wrap) { console.log('[ykt]', msg); return; }
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(function () {
      el.classList.add('out');
      setTimeout(function () { el.remove(); }, 400);
    }, 2600);
  }

  /* -------------------------------------------------------- 侧栏开合
     直播是底座，雨课堂是附加层：默认收起，需要时展开成右侧一栏。 */
  var sideOpen = false;
  var lastPending = 0;

  function setSide(open) {
    sideOpen = !!open;
    // 不再用 hidden 硬切：display:none 会把过渡一起掐掉。
    // 收起/展开交给 .is-open，CSS 那边做宽度/位移/透明度过渡。
    var el = $('viewYkt');
    el.classList.toggle('is-open', sideOpen);
    el.setAttribute('aria-hidden', sideOpen ? 'false' : 'true');
    $('btnYkt').setAttribute('aria-expanded', sideOpen ? 'true' : 'false');
    $('btnYkt').classList.toggle('is-on', sideOpen);
    if (sideOpen) {
      // 侧栏比窗口高的时候（雨课堂面板 + PPT 卡片）浏览器可能停在底部，
      // 顶上的「YUKETANG」被裁掉半行。打开时归零，等渲染完再确认一次。
      el.scrollTop = 0;
      refresh(false);
      requestAnimationFrame(function () { el.scrollTop = 0; });
    }
  }

  function toggleSide() {
    setSide(!sideOpen);
  }

  /* --------------------------------------------------------- 面板切换
     雨课堂 / 学习通 两个面板共用一块固定高度的区域，切的是 display 和
     选中态。高度写死在 CSS 里（--side-panel-h），所以下面的直播画面
     不会因为面板内容多少而改变大小。 */
  var PANELS = {
    ykt: { tab: 'tabYkt', panel: 'panelYkt' },
    cx: { tab: 'tabCx', panel: 'panelCx' }
  };

  function showPanel(which) {
    var want = PANELS[which] ? which : 'ykt';
    Object.keys(PANELS).forEach(function (k) {
      var on = k === want;
      var tab = $(PANELS[k].tab);
      var panel = $(PANELS[k].panel);
      if (tab) {
        tab.classList.toggle('is-on', on);
        tab.setAttribute('aria-selected', on ? 'true' : 'false');
        tab.setAttribute('tabindex', on ? '0' : '-1');
      }
      if (panel) { panel.hidden = !on; }
    });
    // 切过去的那块先滚到顶，别让人接着上一块的滚动位置
    var box = $('sidePanels');
    if (box) { box.scrollTop = 0; }
    // 学习通面板第一次露出来时让它自己刷一下状态
    if (want === 'cx' && window.HFUTCx && window.HFUTCx.refresh) {
      window.HFUTCx.refresh();
    }
  }

  function wireTabs() {
    Object.keys(PANELS).forEach(function (k) {
      var tab = $(PANELS[k].tab);
      if (!tab) { return; }
      tab.addEventListener('click', function () { showPanel(k); });
      // 左右方向键在两个 tab 之间走，键盘用户不用 Tab 乱跳
      tab.addEventListener('keydown', function (e) {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') { return; }
        e.preventDefault();
        var next = k === 'ykt' ? 'cx' : 'ykt';
        showPanel(next);
        var el = $(PANELS[next].tab);
        if (el) { el.focus(); }
      });
    });
  }

  /* ------------------------------------------------------------ 侧栏宽度
     PPT 卡片的画面宽度 = 侧栏宽度 - 内边距，所以给一条能拖的分隔条：
     画面想多大就把侧栏拉多宽，双击回到默认。 */
  var SIDE_DEFAULT = 640, SIDE_MIN = 340;

  function sideWidth() {
    var v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--side-w'));
    return v > 0 ? v : SIDE_DEFAULT;
  }

  function applySideWidth(px) {
    document.documentElement.style.setProperty('--side-w', Math.round(px) + 'px');
  }

  function wireSideGrip() {
    var grip = $('sideGrip');
    if (!grip) { return; }
    var drag = null;

    grip.addEventListener('pointerdown', function (e) {
      drag = { x: e.clientX, w: sideWidth() };
      document.body.classList.add('is-side-drag');
      try { grip.setPointerCapture(e.pointerId); } catch (err) { /* 拿不到就退化成普通拖动 */ }
      e.preventDefault();
    });

    grip.addEventListener('pointermove', function (e) {
      if (!drag) { return; }
      var max = Math.min(900, window.innerWidth * 0.62);   // 再宽直播区就没地方了
      var want = drag.w - (e.clientX - drag.x);
      applySideWidth(Math.max(SIDE_MIN, Math.min(max, want)));
    });

    function stop() {
      if (!drag) { return; }
      drag = null;
      document.body.classList.remove('is-side-drag');
      try {
        localStorage.setItem('hfutone.side-w', String(Math.round(sideWidth())));
      } catch (err) { /* 无痕模式存不了，不算错 */ }
    }
    grip.addEventListener('pointerup', stop);
    grip.addEventListener('pointercancel', stop);

    grip.addEventListener('dblclick', function () {
      applySideWidth(SIDE_DEFAULT);
      try { localStorage.removeItem('hfutone.side-w'); } catch (err) { /* 忽略 */ }
    });

    var saved = 0;
    try { saved = parseFloat(localStorage.getItem('hfutone.side-w') || '0'); } catch (err) { saved = 0; }
    if (saved >= SIDE_MIN) { applySideWidth(saved); }
  }

  /* ---------------------------------------------------------------- 渲染 */
  function statusOf(row) {
    if (row.checkedAt) {
      // '服务器' 是 on-lesson 的 signinStatus 来的，比本地标记更权威
      var when = row.checkedAt === '服务器' ? '· 服务器记录' : row.checkedAt;
      return { text: '已签到 ' + when, cls: 'is-on' };
    }
    if (row._state) return row._state;
    return { text: '未签到', cls: 'is-off' };
  }

  function render() {
    var box = $('yktLessons');
    var user = state.user || {};
    var who = (user.name || '').trim();
    var status, count;
    if (!state.loggedIn) {
      status = state.error || '未登录';
      count = '';
    } else if (state.error) {
      status = '加载失败：' + state.error;
      count = who;
    } else if (!state.loaded) {
      status = who ? who + ' · 正在加载…' : '正在加载…';
      count = user.school || '';
    } else {
      status = (who || '已登录') + ' · ' + (state.lessons.length ? state.lessons.length + ' 节在播' : '暂无在播');
      count = user.school || '';
    }
    $('yktStatus').textContent = status;
    $('yktDot').className = 'dot' + (state.loggedIn && !state.error ? ' ok' : (state.error ? ' err' : ''));
    $('yktCount').textContent = count;
    $('yktLogout').hidden = !state.loggedIn;

    // 身份标识：让「App 登录成功了」和「App 身份真的生效了」在界面上能分开看。
    // 之前两者混在一起，用户点了登录、看到绿色成功、退出弹层却什么也没变。
    var idEl = $('yktIdentity');
    if (idEl) {
      var isApp = (state.identity || 'web') === 'app';
      idEl.hidden = !state.loggedIn;
      idEl.textContent = isApp ? 'App 身份' : '浏览器身份';
      idEl.className = 'identity' + (isApp ? ' is-app' : '');
      idEl.title = isApp
        ? '当前用 App 体系凭据发请求，动态码考勤可免扫'
        : '当前用浏览器凭据发请求；动态码考勤需要 App 身份';
    }

    // 未签到红点：不打扰看直播，只在顶栏那个开关上点一下
    var pending = state.loggedIn
      ? state.lessons.filter(function (r) { return !r.checkedAt; }).length : 0;
    $('yktBadge').hidden = !pending;
    if (pending && !lastPending) {
      toast('雨课堂：' + pending + ' 节课还没签到', 'warn');
    }
    lastPending = pending;
    $('yktCheckAll').disabled = !state.loggedIn || !state.lessons.length || state.busy;

    if (!state.loggedIn) {
      box.innerHTML = '<p class="ykt-empty">'
        + (state.error ? esc(state.error) + '，点【App 登录】重新登一下。'
                       : '还没登录。点【App 登录】用手机号/验证码登一次即可（手机号+密码也行）。')
        + '</p>';
      return;
    }
    if (!state.lessons.length) {
      box.innerHTML = '<p class="ykt-empty">当前没有进行中的课堂。上课后再点【刷新课程】。</p>';
      return;
    }
    box.innerHTML = state.lessons.map(function (row) {
      var st = statusOf(row);
      return '<div class="ykt-row" data-id="' + esc(row.lessonId) + '">'
        + '<div class="ykt-row-main">'
        + '<div class="ykt-row-name">' + esc(row.name || row.courseName || row.lessonId) + '</div>'
        + '<div class="ykt-row-meta">' + esc(row.lessonId)
        + (row.classroomName ? ' · ' + esc(row.classroomName) : '')
        + (row.teacher ? ' · ' + esc(row.teacher) : '') + '</div>'
        + '</div>'
        + '<span class="ykt-row-state ' + st.cls + '">' + esc(st.text) + '</span>'
        + '<div class="ykt-row-btns">'
        + '<button data-act="check" type="button"' + (row.checkedAt ? ' disabled' : '') + '>签到</button>'
        + '<button data-act="pres" type="button">看课件</button>'
        + '<button data-act="q" type="button">题目</button>'
        + (row.checkedAt === '服务器'
            ? '<button data-act="mark" type="button" disabled title="服务器已记录签到，取消本地标记也不会改服务器">服务器已签</button>'
            : '<button data-act="mark" type="button">' + (row.checkedAt ? '取消标记' : '标记已签') + '</button>')
        + '</div></div>';
    }).join('');
  }

  function rowById(id) {
    for (var i = 0; i < state.lessons.length; i++) {
      if (state.lessons[i].lessonId === id) return state.lessons[i];
    }
    return null;
  }

  /* -------------------------------------------------------------- 状态同步 */
  function pull() {
    return api('/api/ykt/state').then(function (d) {
      if (d && typeof d.loggedIn === 'boolean') {
        state.loggedIn = d.loggedIn;
        state.lessons = d.lessons || [];
        state.loaded = d.loaded;
        state.busy = d.busy;
        state.error = d.error || '';
        state.user = d.user || {};
        state.identity = d.identity || 'web';
        render();
        if (state.loggedIn && !(state.user.name || '').trim()) {
          // 登录态在、但还不知道是谁（例如上次没拉到）——补拉一次
          api('/api/ykt/profile').then(function (u) {
            if (u && u.user && u.user.name) { state.user = u.user; render(); }
          });
        }
      }
      return d;
    });
  }

  function refresh(force) {
    if (!state.loggedIn) { return pull(); }
    $('yktStatus').textContent = '正在拉取课程…';
    return api(force ? '/api/ykt/refresh' : '/api/ykt/lessons', {}).then(function (d) {
      if (d && d.error) { state.error = d.error; }
      return pull();
    });
  }

  /* --------------------------------------------------------------- 弹层 */
  function openBackdrop(id) { $(id).classList.add('open'); }
  function closeBackdrop(id) { $(id).classList.remove('open'); }

  /* --------------------------------------------------------------- 随堂题 */
  var qTimer = null, qLesson = '';

  function leftText(q) {
    if (q.unlimited || !q.limitSec || !q.dt) return '不限时';
    var left = q.dt + q.limitSec + (q.extendSec || 0) - Math.floor(Date.now() / 1000);
    if (left <= 0) return '已超时';
    var m = Math.floor(left / 60), s = left % 60;
    return '剩 ' + (m ? m + ' 分 ' : '') + s + ' 秒';
  }

  function renderQuestions() {
    var row = rowById(qLesson);
    var items = (row && row._questions) || [];
    var slides = (PRES_CACHE[qLesson] || {}).slides || [];
    // 课件里预埋、还没发布的题：只摆出来给人看，不提前交（交了会被拒）
    var pending = slides.filter(function (s) {
      return s && s.problem && !s.problem.published
        && !items.some(function (q) { return q.problemId === s.problem.problemId; });
    });
    $('yktQCount').textContent = items.length
      ? items.length + ' 道' + (pending.length ? ' · 另有 ' + pending.length + ' 道未发布' : '')
      : '';
    if (!items.length && !pending.length) {
      $('yktQStage').innerHTML = '<p class="pres-empty">这节课还没有题目。老师发了题再点 ↻ 拉一次。</p>';
      return;
    }

    function pageHtml(slide) {
      if (!slide || !slide.url) { return ''; }
      return '<img class="q-page" loading="lazy" data-i="' + slide.index + '"'
        + ' alt="课件第 ' + (slide.index + 1) + ' 页"'
        + ' src="/api/ykt/img?u=' + encodeURIComponent(slide.url) + '">';
    }
    function stemHtml(p) {
      if (!p) { return ''; }
      var out = p.body ? '<p class="q-stem">' + esc(p.body) + '</p>' : '';
      if (p.options && p.options.length) {
        out += '<ul class="q-opts">' + p.options.map(function (o) {
          return '<li><b>' + esc(o.key || '') + '</b>' + esc(o.value || '') + '</li>';
        }).join('') + '</ul>';
      }
      return out;
    }

    var cards = items.map(function (q, i) {
      var slide = q.index >= 0 ? slides[q.index] : null;
      var p = slide && slide.problem ? slide.problem : null;
      var badge = q.closed ? '<span class="q-badge is-closed">已收题</span>'
        : (q.answered ? '<span class="q-badge is-done">已作答</span>'
          : '<span class="q-badge">待作答</span>');
      var dead = leftText(q) === '已超时';
      var ph = q.problemType === 2 ? '多选：A,B,C'
        : q.problemType === 3 ? '填空：多个空用逗号分隔'
          : q.problemType === 5 ? '主观题文字答案'
            : '单选/判断：A / B / C / D';
      return '<div class="q-card" data-pid="' + esc(q.problemId) + '">'
        + '<div class="q-head">'
        + '<span class="q-badge">' + esc(q.typeName) + '</span>'
        + badge
        + '<span title="' + esc(q.problemId) + '">…' + esc(q.problemId.slice(-6)) + '</span>'
        + '<span class="q-left" data-left="' + i + '">' + esc(leftText(q)) + '</span>'
        + '</div>'
        + pageHtml(slide)
        + stemHtml(p)
        + '<div class="q-body">'
        + '<input type="text" data-in="' + esc(q.problemId) + '" placeholder="' + esc(ph) + '">'
        + '<button type="button" data-act="send" data-pid="' + esc(q.problemId) + '"'
        + ' data-type="' + q.problemType + '" data-dt="' + q.dt + '">提交</button>'
        + '</div>'
        + '<p class="q-note" data-note="' + esc(q.problemId) + '">'
        + (dead ? '已超时：提交会先试时限内通道，被拒再走补交通道' : '') + '</p>'
        + '</div>';
    }).join('');

    var later = pending.map(function (s) {
      return '<div class="q-card is-pending">'
        + '<div class="q-head">'
        + '<span class="q-badge is-closed">未发布</span>'
        + '<span class="q-left">课件第 ' + (s.index + 1) + ' 页</span>'
        + '</div>'
        + pageHtml(s)
        + stemHtml(s.problem)
        + '</div>';
    }).join('');

    $('yktQStage').innerHTML = cards + later;
    tickQuestions();
  }

  function tickQuestions() {
    var row = rowById(qLesson);
    var items = (row && row._questions) || [];
    items.forEach(function (q, i) {
      var el = document.querySelector('[data-left="' + i + '"]');
      if (!el) return;
      var t = leftText(q);
      el.textContent = t;
      el.classList.toggle('is-urgent', t.indexOf('剩') === 0 && parseInt(t.replace(/\D/g, ''), 10) <= 60);
    });
  }

  function loadQuestions(lessonId) {
    qLesson = lessonId;
    var row = rowById(lessonId);
    $('yktQStage').innerHTML = '<p class="pres-empty">正在连网关拉题目（最多十几秒）…</p>';
    $('yktQCount').textContent = '';
    api('/api/ykt/questions?lesson=' + encodeURIComponent(lessonId)).then(function (d) {
      if (row) row._questions = (d && d.items) || [];
      if (!d || !d.ok) {
        $('yktQStage').innerHTML = '<p class="pres-empty">' + esc((d && d.error) || '拉取失败') + '</p>';
        return;
      }
      renderQuestions();
    });
  }

  function openQuestions(row) {
    openBackdrop('yktQBackdrop');
    $('yktQTitle').textContent = (row.courseName || row.name || '课堂') + ' · 随堂题';
    $('yktQTitle').title = (row.name || row.courseName || '课堂') + ' · 随堂题';
    if (qTimer) { clearInterval(qTimer); qTimer = null; }
    qTimer = setInterval(function () {
      if (!$('yktQBackdrop').classList.contains('open')) return;
      tickQuestions();
    }, 1000);

    // 题干在课件页里（老师插入习题时连同 body/options 一起下发，未发布也有）。
    // 先把课件拉下来，题目卡片才能带上题干和那一页的图。
    function render() {
      if (row._questions && row._questions.length) {
        qLesson = row.lessonId;
        PRES.slides = (PRES_CACHE[row.lessonId] || {}).slides || [];  // 点题图放大时用这一份
        renderQuestions();
      } else {
        PRES.slides = (PRES_CACHE[row.lessonId] || {}).slides || [];
        loadQuestions(row.lessonId);
      }
    }
    if (PRES_CACHE[row.lessonId]) { render(); return; }
    $('yktQStage').innerHTML = '<p class="pres-empty">正在拉课件页（题干在上面）…</p>';
    api('/api/ykt/pres?lesson=' + encodeURIComponent(row.lessonId)).then(function (d) {
      PRES_CACHE[row.lessonId] = { slides: (d && d.ok && d.slides) || [], title: (d && d.title) || '' };
      render();
    }, function () { PRES_CACHE[row.lessonId] = { slides: [], title: '' }; render(); });
  }

  /* --------------------------------------------------------------- 课件 */
  function openPres(row, force) {
    presRow = row;
    openBackdrop('yktPresBackdrop');
    $('yktPresTitle').textContent = (row.courseName || row.name || '课堂课件');
    $('yktPresTitle').title = row.name || row.courseName || '';

    // 课件页在一次课堂里是静态的（图片地址不会变），重复点「看课件」不该再拉一遍。
    // 缓存必须按 lessonId 存在模块级，不能挂在 row 上：pull() 每 15 秒用服务端
    // 返回的新对象整个替换 state.lessons，挂在 row 上的字段下一轮就没了。
    var cached = PRES_CACHE[row.lessonId];
    if (!force && cached && cached.slides && cached.slides.length) {
      renderPres(row, cached);
      return;
    }

    $('yktPresCount').textContent = '正在拉取…';
    $('yktPresStage').innerHTML = '<p class="pres-empty">正在拉取课件…</p>';
    api('/api/ykt/pres?lesson=' + encodeURIComponent(row.lessonId)).then(function (d) {
      if (!d || !d.ok) {
        $('yktPresStage').innerHTML = '<p class="pres-empty">' + esc((d && d.error) || '拉取失败') + '</p>';
        $('yktPresCount').textContent = '';
        return;
      }
      PRES_CACHE[row.lessonId] = { slides: d.slides || [], title: d.title || '' };
      renderPres(row, d);
    });
  }

  function renderPres(row, d) {
      $('yktPresTitle').textContent = d.title || row.courseName || row.name || '课堂课件';
      $('yktPresTitle').title = row.name || d.title || '';
      var slides = d.slides || [];
      // 课件里预埋的随堂题：老师还没翻到那页、也还没点发布，服务端已经把题干
      // 和选项一起返回了。这里只做个角标提示，不提前替用户答题（发了没解锁的题
      // 交上去会被拒）。
      var withQ = slides.filter(function (s) { return s && s.problem; }).length;
      $('yktPresCount').textContent = slides.length + ' 页'
        + (withQ ? ' · 含 ' + withQ + ' 道随堂题' : '');
      if (!slides.length) {
        $('yktPresStage').innerHTML = '<p class="pres-empty">这份课件没有可显示的页</p>';
        return;
      }
      $('yktPresStage').innerHTML = slides.map(function (s, i) {
        var src = s.url ? '/api/ykt/img?u=' + encodeURIComponent(s.url) : '';
        var q = s.problem
          ? '<span class="pres-q" title="' + esc((s.problem.body || '').slice(0, 60))
            + (s.problem.published ? '' : '（还没发布）') + '">题</span>'
          : '';
        if (!src) { return '<p class="pres-empty">第 ' + (i + 1) + ' 页没有图片' + q + '</p>'; }
        return '<div class="pres-cell"><img loading="lazy" data-i="' + i + '" alt="第 '
          + (i + 1) + ' 页" src="' + src + '">' + q + '</div>';
      }).join('');
      PRES.slides = slides;
  }

  /* ------------------------------------------------ 课件单页放大
     缩略图只有 240px 宽，1080p 的 PPT 缩到那个尺寸字全是糊的。
     点一页进单页大图，←/→ 翻页，Esc 或点空白退出。 */
  var PRES = { slides: [], at: 0 };
  var PRES_CACHE = {};        // lessonId -> {slides, title}，课件页是静态的
  var presRow = null;              // 当前课件弹层属于哪个课堂，↻ 要用

  function pzSrc(s) {
    return s && s.url ? '/api/ykt/img?u=' + encodeURIComponent(s.url) : '';
  }

  function pzShow(i) {
    var box = $('yktPresZoom');
    var img = $('yktPzImg');
    if (!box || !img || !PRES.slides.length) { return; }
    var n = PRES.slides.length;
    PRES.at = ((i % n) + n) % n;              // 循环翻页
    var src = pzSrc(PRES.slides[PRES.at]);
    if (!src) { return; }
    img.src = src;
    img.alt = '第 ' + (PRES.at + 1) + ' 页';
    var c = $('yktPzCounter');
    if (c) { c.textContent = (PRES.at + 1) + ' / ' + n; }
    box.hidden = false;
  }

  function pzHide() {
    var box = $('yktPresZoom');
    if (box) { box.hidden = true; }
  }

  function pzOpen() {
    var box = $('yktPresZoom');
    return !!box && !box.hidden;
  }

  /* --------------------------------------------------------------- 事件 */
  function bind() {
    // 鼠标点完按钮就主动失焦：Chromium 会给聚焦元素画一圈系统强调色焦点环，
    // 这台机器强调色是橙黄，点在哪个按钮上就在哪个按钮外面留一圈框。
    // 只对指针点击生效（e.detail > 0），键盘 Enter 触发时 detail 是 0，焦点照旧留着。
    document.addEventListener('click', function (e) {
      if (!e.detail) return;
      var btn = e.target.closest ? e.target.closest('button, summary') : null;
      if (btn && btn.blur) btn.blur();
    }, true);

    $('btnYkt').addEventListener('click', toggleSide);


    $('yktRefreshBtn').addEventListener('click', function () { refresh(true); });

    /* ------------------------------------------------ App 登录（方案 A）
       用 App 身份登录一次即可：这套凭据既走 App 接口（动态码考勤免扫），
       也走网页接口（实测 on-lesson 两种头都 200）。
       两种登录方式，短信那条是两步（发码 / 登录），每步各过一次人机验证：
         kind=1  手机号/邮箱 + 密码
         kind=3  手机号 + 短信验证码（POST /api/v3/user/code/send 发码） */
    var appTimer = null;
    var appKind = 1;
    var appCodeTimer = null;

    function appShowForm() {
      if ($('yktAppForm')) { $('yktAppForm').hidden = false; }
      if ($('yktAppStage')) { $('yktAppStage').hidden = true; }
      if ($('yktAppFrame')) { $('yktAppFrame').src = 'about:blank'; }
      appSyncMode();
    }

    function appStopPoll() {
      if (appTimer) { clearInterval(appTimer); appTimer = null; }
    }

    /* ------------------------------------------------ 发送验证码按钮的状态
       原来这个按钮点完就只是 disabled 一下再 enabled 回来，人根本看不出
       码到底发没发出去 —— 尤其验证是在 iframe 里做的，主表单那一侧一点动静
       都没有，只能靠猜。改成三态：发送中 / 已发送 + 倒计时 / 重新发送。 */
    function appCodeReset(label) {
      if (appCodeTimer) { clearInterval(appCodeTimer); appCodeTimer = null; }
      var b = $('yktAppSendCode');
      if (!b) { return; }
      b.disabled = false;
      b.classList.remove('is-sent');
      b.textContent = label || '发送验证码';
    }

    function appCodeCountdown(sec) {
      if (appCodeTimer) { clearInterval(appCodeTimer); appCodeTimer = null; }
      var b = $('yktAppSendCode');
      if (!b) { return; }
      var left = sec || 60;
      b.classList.add('is-sent');
      b.disabled = true;
      b.textContent = '已发送 ' + left + 's';
      appCodeTimer = setInterval(function () {
        left -= 1;
        if (left <= 0) { appCodeReset('重新发送'); return; }
        b.textContent = '已发送 ' + left + 's';
      }, 1000);
    }

    function appMsg(text, cls) {
      var m = $('yktAppMsg');
      if (!m) { return; }
      m.textContent = text || '';
      // err-line 是尺寸与位置的基础类，修饰类只能追加；直接覆盖会把
      // min-height / 字号一起丢掉，提示行变成一行没样式的裸文本
      m.className = 'err-line' + (cls ? ' ' + cls : '');
    }

    /* 统一的「回到表单」。任何终态（发码成功 / 失败 / 取消）都走这里，
       免得人被关在验证页里：上一次 404 被 .then 吞掉之后，界面就停在那儿
       一动不动，看起来像验证按钮根本没生效。 */
    function appBackToForm(text, cls) {
      appStopPoll();
      appShowForm();
      if (text) { appMsg(text, cls || ''); }
    }

    /* 验证页发来的进度通知（见 yktapplogin.captcha_html 里的 notify）。
       轮询是 900ms 一次，这一步让它看起来是"立刻返回"。 */
    window.addEventListener('message', function (ev) {
      var d = ev.data;
      if (!d || d.src !== 'hfutone-ykt-captcha') { return; }
      var fr = $('yktAppFrame');
      if (fr && fr.contentWindow && ev.source && ev.source !== fr.contentWindow) { return; }
      var msg = $('yktAppStageMsg');
      if (d.phase === 'submitting') {
        if (msg) { msg.className = 'err-line'; msg.textContent = '验证通过，正在提交…'; }
      } else if (d.phase === 'sent') {
        appBackToForm('验证码已发送，填进来再点「去验证并登录」');
        appCodeCountdown(60);
        if ($('yktAppCode')) { $('yktAppCode').focus(); }
      } else if (d.phase === 'ok') {
        // 真正的收尾交给轮询（它还要刷新课表、关弹层），这里只先把话说清楚
        if (msg) {
          msg.className = 'app-result is-ok';
          msg.textContent = '✅ App 身份可用' + (d.name ? '：' + d.name : '');
        }
      } else if (d.phase === 'fail') {
        appBackToForm('❌ ' + (d.error || '人机验证没走完'), 'is-err');
      } else if (d.phase === 'closed' || d.phase === 'cancelled') {
        appStopPoll();
        appShowForm();
      }
    });

    /* 切换「密码 / 验证码」时同步字段与分段控件的选中态 */
    function appSyncMode() {
      var sms = appKind === 3;
      if ($('yktAppPwdField')) { $('yktAppPwdField').hidden = sms; }
      if ($('yktAppCodeField')) { $('yktAppCodeField').hidden = !sms; }
      if ($('yktAppAccount')) {
        $('yktAppAccount').placeholder = sms ? '11 位手机号' : '13800138000 / 邮箱';
      }
      if ($('yktAppAccountLabel')) {
        $('yktAppAccountLabel').textContent = sms ? '手机号' : '手机号 / 邮箱';
      }
      var seg = $('yktAppMode');
      if (seg) {
        for (var i = 0; i < seg.children.length; i++) {
          var b = seg.children[i];
          var on = String(b.getAttribute('data-kind')) === String(appKind);
          if (on) { b.classList.add('on'); } else { b.classList.remove('on'); }
          b.setAttribute('aria-selected', on ? 'true' : 'false');
        }
      }
    }

    function appOpenStage(page) {
      if ($('yktAppForm')) { $('yktAppForm').hidden = true; }
      if ($('yktAppStage')) { $('yktAppStage').hidden = false; }
      if ($('yktAppStageMsg')) {
        $('yktAppStageMsg').className = 'err-line';
        $('yktAppStageMsg').textContent = '请在下面完成人机验证';
      }
      if ($('yktAppFrame')) { $('yktAppFrame').src = page || '/api/ykt/applogin/page'; }
      appPoll();
    }

    function appPoll() {
      appStopPoll();
      appTimer = setInterval(function () {
        var msg = $('yktAppStageMsg');
        api('/api/ykt/applogin/state').then(function (d) {
          if (!d) { return; }
          var st = d.state || '';
          if (st === 'waiting') {
            if (msg && msg.className !== 'app-result') {
              msg.className = 'err-line';
              msg.textContent = '请在下面完成人机验证';
            }
            return;
          }
          if (st === 'sending' || st === 'verifying') {
            if (msg) { msg.className = 'err-line'; msg.textContent = d.message || '正在处理…'; }
            return;
          }
          if (st === 'sent') {
            // 短信第一步完成：人机验证用掉了，回表单填验证码
            appBackToForm('验证码已发送，填进来再点「去验证并登录」');
            appCodeCountdown(60);
            if ($('yktAppCode')) { $('yktAppCode').focus(); }
            return;
          }
          if (st === 'ok') {
            appStopPoll();
            var who = (d.user && d.user.name) || '';
            if (msg) {
              msg.className = 'app-result is-ok';
              msg.textContent = '✅ App 身份可用'
                + (who ? '：' + who : '')
                + '\n动态二维码考勤不用再扫码了；雨课堂面板右上角会显示「App 身份」。';
            }
            toast('雨课堂 App 身份已生效' + (who ? '：' + who : ''), 'ok');
            setTimeout(function () {
              closeBackdrop('yktAppBackdrop');
              appShowForm();
              pull().then(function () { refresh(true); });
            }, 2200);
            return;
          }
          if (st === 'fail') {
            var ver = d.verify || {};
            var name = (d.user && d.user.name) || '';
            var text;
            if (name && ver && ver.note) {
              // 登录成功但身份不可用 —— 最容易被误读成「什么也没变」的情况
              text = '⚠️ 账号登录成功（' + name + '），但 App 身份不可用：'
                + (ver.note || '')
                + (ver.code ? '（code=' + ver.code + '）' : '')
                + '；雨课堂仍按浏览器身份工作，签到照旧。';
            } else {
              text = '❌ ' + (d.message || '登录失败');
            }
            toast(text, 'err');
            appBackToForm(text, 'is-err');
            return;
          }
          /* st === 'idle'：什么都没开始，保持现状 */
        }).catch(function () {
          // 端点异常绝不能静默：上一次就是这里 404 被吞，验证完页面一动不动
          if (msg && msg.className !== 'app-result') {
            msg.className = 'err-line';
            msg.textContent = '读不到登录状态，点「返回重填」重试';
          }
        });
      }, 900);
    }

    if ($('yktAppMode')) {
      $('yktAppMode').addEventListener('click', function (ev) {
        var t = ev.target;
        var k = t && t.getAttribute ? t.getAttribute('data-kind') : '';
        if (!k) { return; }
        appKind = parseInt(k, 10) === 3 ? 3 : 1;
        appMsg('');
        appCodeReset('发送验证码');
        appSyncMode();
      });
    }

    if ($('yktAppSendCode')) {
      $('yktAppSendCode').addEventListener('click', function () {
        var phone = ($('yktAppAccount') && $('yktAppAccount').value || '').trim();
        if (!/^1\d{10}$/.test(phone)) {
          appMsg('先填 11 位手机号');
          return;
        }
        var b = $('yktAppSendCode');
        b.disabled = true;
        b.classList.remove('is-sent');
        b.textContent = '发送中…';
        appMsg('正在打开人机验证…');
        api('/api/ykt/applogin/sendsms', { phone: phone }).then(function (d) {
          if (!d || !d.ok) {
            appCodeReset('重新发送');
            appMsg((d && d.error) || '发送失败');
            return;
          }
          appMsg('');
          appOpenStage(d.page);
        }).catch(function (e) {
          appCodeReset('重新发送');
          appMsg('打不开人机验证：' + (e && e.message ? e.message : e));
        });
      });
    }

    if ($('yktAppBtn')) {
      $('yktAppBtn').addEventListener('click', function () {
        appStopPoll();
        appShowForm();
        appMsg('');
        appCodeReset('发送验证码');
        openBackdrop('yktAppBackdrop');
        if ($('yktAppAccount')) { $('yktAppAccount').focus(); }
      });
    }

    if ($('yktAppCancel')) {
      $('yktAppCancel').addEventListener('click', function () {
        appStopPoll();
        api('/api/ykt/applogin/cancel', {});
        appCodeReset('发送验证码');
        appMsg('');
        closeBackdrop('yktAppBackdrop');
        appShowForm();
      });
    }

    if ($('yktAppBack')) {
      $('yktAppBack').addEventListener('click', function () {
        // 返回重填：会话作废，避免残留的 stage 把下一次验证带到错误的分支
        appStopPoll();
        api('/api/ykt/applogin/cancel', {});
        appCodeReset($('yktAppSendCode') && $('yktAppSendCode').classList.contains('is-sent')
          ? '重新发送' : '发送验证码');
        appShowForm();
      });
    }

    if ($('yktAppGo')) {
      $('yktAppGo').addEventListener('click', function () {
        var acc = ($('yktAppAccount') && $('yktAppAccount').value || '').trim();
        var secret = appKind === 3
          ? ($('yktAppCode') && $('yktAppCode').value || '').trim()
          : ($('yktAppPassword') && $('yktAppPassword').value || '');
        if (!acc || !secret) {
          appMsg(appKind === 3 ? '手机号和验证码都要填' : '账号和密码都要填');
          return;
        }
        $('yktAppGo').disabled = true;
        appMsg('正在准备人机验证…');
        api('/api/ykt/applogin/start', { account: acc, password: secret, kind: appKind }).then(function (d) {
          $('yktAppGo').disabled = false;
          if (!d || !d.ok) {
            appMsg((d && d.error) || '启动失败');
            return;
          }
          appMsg('');
          appOpenStage(d.page);
        }).catch(function (e) {
          $('yktAppGo').disabled = false;
          appMsg('启动失败：' + (e && e.message ? e.message : e));
        });
      });
    }
    $('yktLogout').addEventListener('click', function () {
      api('/api/ykt/logout', {}).then(function () { pull(); });
    });

    $('yktCheckAll').addEventListener('click', function () {
      var items = state.lessons.filter(function (r) { return !r.checkedAt; })
        .map(function (r) {
          return { lessonId: r.lessonId, classroomId: r.classroomId, name: r.name,
                   signinStatus: r.signinStatus || 0 };
        });
      if (!items.length) { toast('没有需要签到的课堂'); return; }
      state.busy = true;
      render();
      $('yktStatus').textContent = '正在签到 ' + items.length + ' 节…';
      api('/api/ykt/checkin_all', { items: items }).then(function (d) {
        state.busy = false;
        if (!d || !d.ok) {
          toast('签到失败：' + (d && d.error || '未知'));
          render();
          return;
        }
        var res = d.results || [];
        var ok = res.filter(function (r) { return r.status === 'checked' || r.status === 'already'; }).length;
        var joined = res.filter(function (r) { return r.status === 'joined'; }).length;
        var bad = res.filter(function (r) { return !r.ok; });
        res.forEach(function (r) {
          var row = rowById(r.lessonId);
          if (!row) return;
          if (r.status === 'joined') {
            // 进班成功但考勤里还没有签到记录：保持"未签到"，红点不灭
            row.checkedAt = '';
            row._state = { text: '已进班 · 待签到', cls: 'is-warn' };
          } else if (r.ok) {
            row.checkedAt = '刚刚';
            delete row._state;
          } else {
            var badText = r.status === 'need_scan' ? '需手机扫码'
              : (r.status === 'need_location' ? '需位置签到' : (r.detail || '失败'));
            row._state = { text: badText, cls: 'is-warn' };
          }
        });
        state.lessons = d.lessons || state.lessons;
        render();
        toast('签到完成：成功 ' + ok
              + (joined ? '，已进班待签到 ' + joined : '')
              + (bad.length ? '，失败 ' + bad.length : ''), bad.length ? 'warn' : 'ok');
      });
    });

    $('yktLessons').addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-act]');
      if (!btn) return;
      var row = rowById(btn.closest('.ykt-row').dataset.id);
      if (!row) return;
      var act = btn.dataset.act;
      if (act === 'check') {
        btn.disabled = true;
        btn.textContent = '签…';
        api('/api/ykt/checkin', {
          lessonId: row.lessonId, classroomId: row.classroomId, name: row.name,
          signinStatus: row.signinStatus || 0
        }).then(function (d) {
          btn.textContent = '签到';
          if (d && d.status === 'joined') {
            // 只是进了班，老师还没发签到——别显示成已签到
            row.checkedAt = '';
            row._state = { text: '已进班 · 待签到', cls: 'is-warn' };
            toast((row.name || '') + ' 已进班，老师还没发起签到', 'warn');
          } else if (d && d.ok) {
            row.checkedAt = '刚刚';
            delete row._state;
            toast((row.name || '') + ' 已签到', 'ok');
          } else {
            row._state = {
              text: (d && d.status === 'need_scan') ? '需手机扫码'
                : ((d && d.status === 'need_location') ? '需位置签到' : ((d && d.detail) || '失败')),
              cls: 'is-warn'
            };
            toast('签到失败：' + ((d && d.detail) || (d && d.error) || '未知'), 'warn');
          }
          render();
        });
      } else if (act === 'pres') {
        openPres(row);
      } else if (act === 'q') {
        openQuestions(row);
      } else if (act === 'mark') {
        api('/api/ykt/mark', { lessonId: row.lessonId, on: !row.checkedAt }).then(function () {
          row.checkedAt = row.checkedAt ? '' : '手动';
          render();
        });
      }
    });

    $('yktPresClose').addEventListener('click', function () {
      pzHide();
      closeBackdrop('yktPresBackdrop');
    });

    $('yktPresReload').addEventListener('click', function () {
      if (presRow) { openPres(presRow, true); }
    });

    // 点缩略图 → 单页大图
    $('yktPresStage').addEventListener('click', function (e) {
      var img = e.target.closest ? e.target.closest('img[data-i]') : null;
      if (!img) { return; }
      pzShow(Number(img.dataset.i) || 0);
    });
    $('yktPzClose').addEventListener('click', pzHide);
    $('yktPzPrev').addEventListener('click', function () { pzShow(PRES.at - 1); });
    $('yktPzNext').addEventListener('click', function () { pzShow(PRES.at + 1); });
    // 点图外空白处退出（点图本身不退出，方便放大细看）
    $('yktPresZoom').addEventListener('click', function (e) {
      if (e.target === this) { pzHide(); }
    });

    $('yktQClose').addEventListener('click', function () {
      closeBackdrop('yktQBackdrop');
      if (qTimer) { clearInterval(qTimer); qTimer = null; }
    });
    $('yktQReload').addEventListener('click', function () { loadQuestions(qLesson); });

    $('yktQStage').addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-act="send"]');
      if (!btn) return;
      var pid = btn.dataset.pid;
      var input = document.querySelector('[data-in="' + pid + '"]');
      var note = document.querySelector('[data-note="' + pid + '"]');
      var text = (input && input.value || '').trim();
      if (!text) {
        if (note) { note.textContent = '先把答案填进去'; note.className = 'q-note is-err'; }
        return;
      }
      btn.disabled = true;
      if (note) { note.textContent = '提交中…'; note.className = 'q-note'; }
      api('/api/ykt/answer', {
        lessonId: qLesson, problemId: pid,
        type: parseInt(btn.dataset.type, 10) || 1,
        text: text, dt: parseInt(btn.dataset.dt, 10) || 0
      }).then(function (d) {
        btn.disabled = false;
        if (!note) return;
        if (d && d.ok) {
          note.textContent = (d.note || '提交成功') + (d.retried ? '（已走补交通道）' : '');
          note.className = 'q-note is-ok';
          var row = rowById(qLesson);
          (row && row._questions || []).forEach(function (q) {
            if (q.problemId === pid) q.answered = true;
          });
          tickQuestions();
        } else {
          note.textContent = '失败：' + ((d && d.error) || '未知');
          note.className = 'q-note is-err';
        }
      });
    });

    document.addEventListener('keydown', function (e) {
      // 单页放大层开着时，方向键翻页、Esc 只关这一层（别把整个课件层也关了）
      if (pzOpen()) {
        if (e.key === 'ArrowLeft') { e.preventDefault(); pzShow(PRES.at - 1); return; }
        if (e.key === 'ArrowRight') { e.preventDefault(); pzShow(PRES.at + 1); return; }
        if (e.key === 'Escape') { e.preventDefault(); pzHide(); return; }
      }
      if (e.key !== 'Escape') return;
      if (sideOpen && !$('yktQBackdrop').classList.contains('open')
          && !$('yktPresBackdrop').classList.contains('open')) {
        setSide(false);
        return;
      }
      if ($('yktQBackdrop').classList.contains('open')) {
        closeBackdrop('yktQBackdrop');
        if (qTimer) { clearInterval(qTimer); qTimer = null; }
        return;
      }
      if ($('yktPresBackdrop').classList.contains('open')) {
        pzHide();
        closeBackdrop('yktPresBackdrop');
        return;
      }
    });

    // 点遮罩关闭（课件层除外，避免误触关掉正在看的页）
    ['yktAppBackdrop'].forEach(function (id) {
      var el = $(id);
      if (!el) { return; }
      el.addEventListener('click', function (e) {
        if (e.target === this) {
          if (id === 'yktAppBackdrop') { appStopPoll(); appShowForm(); }
          closeBackdrop(id);
        }
      });
    });
  }

  /* ---------------------------------------------------------------- 启动 */
  function boot() {
    bind();
    wireTabs();
    pull();
    // 不管你停在哪个视图，后台都盯着——签到是有时间窗的，错过就没了
    setInterval(pull, 15000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  wireSideGrip();

  window.HFUTYkt = { setSide: setSide, toggleSide: toggleSide, refresh: refresh,
                    setWidth: applySideWidth, showPanel: showPanel,
                    render: render, state: state };
})();