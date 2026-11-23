/* ============================================================================
   HFUTone · 前端交互（直播课堂）
   与本地 Python 服务通信（同源，无需 CORS）。
   ========================================================================== */
(function () {
  'use strict';

  const UI_VERSION = '2026.11.16.7';
  const $ = (id) => document.getElementById(id);
  // 安全绑定：元素缺失时跳过而不是抛错，避免个别控件版本错位时拖垮全部按钮
  const on = (el, ev, fn, opts) => { if (el) el.addEventListener(ev, fn, opts); };
  const rowsEl = $('rows');
  const deckEl = $('deckStage');
  const deckPager = $('deckPager');
  const deckCounter = $('deckCounter');
  const prevBtn = $('btnPrev');
  const nextBtn = $('btnNext');
  const allCoursesEl = $('allCourses');
  const hiddenNote = $('hiddenNote');
  const hiddenText = $('hiddenText');
  const unhideBtn = $('btnUnhide');
  const hideBtn = $('btnHide');
  const workspaceEl = document.querySelector('.workspace');
  const dotEl = $('dot');
  const statusEl = $('statusText');
  const countEl = $('countText');
  const playBtn = $('btnPlay');
  const refreshBtn = $('btnRefresh');
  const noticeEl = $('notice');
  const noticeText = $('noticeText');
  const diagnoseBtn = $('btnDiagnose');

  const state = {
    courses: [],
    allCourses: [],
    upcoming: [],
    hidden: new Set(),
    selected: -1,
    activeCard: 0,
    busy: false,
    loaded: false,
    username: '',
  };

  let oceanEngine = null;
  let pageActive = !document.hidden;
  let offlineMode = false;
  let offlineTimer = 0;
  let offlineProbeRunning = false;

  function setPageActive(active) {
    pageActive = Boolean(active);
    if (oceanEngine) oceanEngine.setActive(pageActive);
    if (pageActive && offlineMode) scheduleOfflineProbe(1200);
  }

  /* ------------------------------------------------------------ 账号弹层
     放在所有初始化之前：即使后面某段脚本出错、或者用户点得比脚本加载还快，
     账号按钮也能打开登录框（index.html 里还有一份纯 DOM 兜底）。 */
  const backdrop = $('loginBackdrop');
  const errLine = $('loginErr');
  let loginOpenedAt = 0;
  let loginReturnFocus = null;

  function openLogin(prefill) {
    errLine.textContent = '';
    $('u').value = prefill?.username || '';
    $('p').value = '';
    loginOpenedAt = Date.now();
    loginReturnFocus = document.activeElement;
    backdrop.classList.add('open');
    setTimeout(() => (prefill?.username ? $('p') : $('u')).focus(), 120);
  }

  function closeLogin() {
    backdrop.classList.remove('open');
    const target = loginReturnFocus;
    loginReturnFocus = null;
    if (target && target.isConnected && typeof target.focus === 'function') {
      setTimeout(() => target.focus(), 0);
    }
  }

  window.HFUTLive = {
    version: UI_VERSION,
    openLogin: () => openLogin({ username: state.username }),
    setActive: (active) => setPageActive(active),
    reportError: (error) => window.HFUTLiveBoot?.report('manual', {
      message: error?.message || String(error), error,
    }),
  };
  const versionEl = $('uiVersion');
  if (versionEl) versionEl.textContent = `HFUTone UI · ${UI_VERSION}`;

  // 全局异常仍由 index.html 采集并写日志；这里只给用户一个克制的提示。
  if (window.HFUTLiveBoot) {
    const hadBootErrors = window.HFUTLiveBoot.errors.length > 0;
    window.HFUTLiveBoot.onError = () => toast('界面脚本异常，已写入诊断日志');
    if (hadBootErrors) toast('界面脚本异常，已写入诊断日志');
  }

  on($('btnCancel'), 'click', closeLogin);
  // 刚打开的瞬间忽略背景点击，防止某些 WebView 把同一次鼠标事件补发给新弹层
  on(backdrop, 'click', (e) => {
    if (e.target === backdrop && Date.now() - loginOpenedAt > 300) closeLogin();
  });
  document.addEventListener('keydown', (e) => {
    if (!backdrop.classList.contains('open')) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      closeLogin();
      return;
    }
    if (e.key !== 'Tab') return;
    const focusable = [...backdrop.querySelectorAll('button:not(:disabled), input:not(:disabled), [tabindex="0"]')];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    } else if (!backdrop.contains(document.activeElement)) {
      e.preventDefault();
      first.focus();
    }
  });

  on($('loginForm'), 'submit', async (e) => {
    e.preventDefault();
    const username = $('u').value.trim();
    const password = $('p').value;
    if (!username || !password) { errLine.textContent = '学号和密码都要填。'; return; }

    const btn = $('btnSave');
    btn.disabled = true;
    btn.textContent = '正在验证…';
    errLine.textContent = '';

    try {
      const res = await api('/api/login', { username, password });
      if (res.ok) {
        closeLogin();
        toast('登录成功');
        load(true);
      } else {
        errLine.textContent = res.error || '登录失败';
      }
    } catch (err) {
      errLine.textContent = '登录失败：' + err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = '保存并登录';
    }
  });

  // 服务端 statusName 的真实取值：进行中 / 未开始 / 已结束
  // （早期版本误写成「直播中」，导致正在上的课永远匹配不上）
  const ST_LIVE = '进行中';
  const ST_SOON = '未开始';
  const ST_DONE = '已结束';
  // 兼容历史上可能出现的其它写法
  const LIVE_ALIASES = new Set([ST_LIVE, '直播中', '直播', '进行中']);
  const isLiveCourse = (c) => !!c && LIVE_ALIASES.has(String(c.statusName || '').trim());

  const STATUS_CLASS = { [ST_LIVE]: 'live', [ST_SOON]: 'soon', [ST_DONE]: 'done' };

  // ------------------------------------------------------------ 本地状态推进
  // 「未开始 → 进行中 → 已结束」按 startTime/endTime 在前端准时翻转：
  // 上课时间一到卡片自动变「进行中」，不用等用户点刷新。
  // 服务端数据每次回来都会整体覆盖，因此不会和权威状态打架；
  // 这里只在两次刷新之间保证显示是准时的。
  const FALLBACK_CLASS_MS = 104 * 60 * 1000;   // 没有 endTime 时的默认课长（两节连堂）

  function parseTimeMs(value) {
    const d = startParts(value);
    if (!d) return 0;
    return new Date(d.year, d.month - 1, d.day,
      Number(d.hour), Number(d.minute)).getTime();
  }

  // 就地推进状态；返回 { live, done, firstLive }，live>0 时调用方提示用户
  function advanceStatuses(list) {
    const now = Date.now();
    const flips = { live: 0, done: 0, firstLive: null };
    for (const c of list || []) {
      const st = String(c.statusName || '').trim();
      const s = parseTimeMs(c.startTime);
      if (!s) continue;
      const e = parseTimeMs(c.endTime) || s + FALLBACK_CLASS_MS;
      if (st === ST_SOON && now >= s) {
        c.statusName = now < e ? ST_LIVE : ST_DONE;
        if (c.statusName === ST_LIVE) {
          flips.live += 1;
          flips.firstLive = flips.firstLive || c;
        } else {
          flips.done += 1;
        }
      } else if (isLiveCourse(c) && now >= e) {
        c.statusName = ST_DONE;
        flips.done += 1;
      }
    }
    return flips;
  }

  let statusTimer = 0;
  function scheduleStatusTick() {
    clearTimeout(statusTimer);
    const now = Date.now();
    let next = Infinity;
    for (const c of state.allCourses || []) {
      const st = String(c.statusName || '').trim();
      const s = parseTimeMs(c.startTime);
      if (!s) continue;
      const e = parseTimeMs(c.endTime) || s + FALLBACK_CLASS_MS;
      if (st === ST_SOON && s > now) next = Math.min(next, s);
      else if (isLiveCourse(c) && e > now) next = Math.min(next, e);
    }
    if (!isFinite(next)) return;
    statusTimer = setTimeout(() => {
      const flips = advanceStatuses(state.allCourses);
      if (flips.live || flips.done) {
        renderCourses(state.allCourses, { animate: false });
      }
    }, Math.min(next - now + 500, 2147483647));
  }
  const CARD_TONES = [
    { accent: '#9c6b56', wash: 'rgba(204, 120, 92, 0.14)' },
    { accent: '#58768a', wash: 'rgba(90, 127, 151, 0.14)' },
    { accent: '#687c63', wash: 'rgba(105, 137, 101, 0.13)' },
    { accent: '#7b6c8e', wash: 'rgba(133, 112, 158, 0.12)' },
    { accent: '#957642', wash: 'rgba(177, 143, 76, 0.13)' },
  ];

  /* ------------------------------------------------------------ 工具 */
  function startParts(value) {
    const m = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    if (!m) return null;
    return {
      year: Number(m[1]), month: Number(m[2]), day: Number(m[3]),
      hour: m[4], minute: m[5],
    };
  }

  function fmtTime(value) {
    const d = startParts(value);
    return d ? `${String(d.month).padStart(2, '0')}-${String(d.day).padStart(2, '0')} ${d.hour}:${d.minute}` : (value || '—');
  }

  function courseView(course) {
    const raw = (course.name || '(未命名)').trim();
    const parts = raw.split(/[-_]+/).map((s) => s.trim()).filter(Boolean);
    const title = parts.shift() || raw;
    const teacher = parts[0] || '授课教师待定';
    const rest = parts.slice(1);
    const room = course.classroomName || rest.find((s) => /楼|室|\d/.test(s)) || '教室待定';
    const schedule = rest.find((s) => s.includes('节')) || '';
    const week = (schedule.match(/第[^星]*?周/) || [])[0] ||
      (rest.find((s) => /第.+周/.test(s)) || '');
    const session = (schedule.match(/第[\d,、]+节/) || [])[0] || '';
    const d = startParts(course.startTime);
    let relative = '';
    let weekday = '';
    if (d) {
      const date = new Date(d.year, d.month - 1, d.day);
      const now = new Date();
      const diff = Math.round((Date.UTC(d.year, d.month - 1, d.day) -
        Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000);
      relative = diff === 0 ? '今天' : diff === 1 ? '明天' : diff === 2 ? '后天' : '';
      weekday = '星期' + '日一二三四五六'[date.getDay()];
    }
    return {
      raw, title, teacher, room, week, session,
      date: d ? `${d.month}月${d.day}日` : '日期待定',
      clock: d ? `${d.hour}:${d.minute}` : '—',
      dayNote: [relative, weekday].filter(Boolean).join(' · ') || '课程直播',
    };
  }

  function courseIdentity(course) {
    if (course.courseId && course.userId) {
      return `class:${course.courseId}:${course.userId}`;
    }
    const view = courseView(course);
    return `name:${view.title}:${view.teacher}`;
  }

  function hiddenStorageKey() {
    return `hfutlive.hiddenCourses.v1:${state.username || 'default'}`;
  }

  function readHiddenCourses() {
    try {
      const value = JSON.parse(localStorage.getItem(hiddenStorageKey()) || '[]');
      return new Set(Array.isArray(value) ? value.filter((v) => typeof v === 'string') : []);
    } catch (e) {
      return new Set();
    }
  }

  function saveHiddenCourses() {
    try {
      localStorage.setItem(hiddenStorageKey(), JSON.stringify([...state.hidden]));
    } catch (e) {
      toast('隐藏记录无法保存到本机');
    }
  }

  function updateHiddenTools() {
    const count = state.allCourses.filter((course) => state.hidden.has(courseIdentity(course))).length;
    hiddenNote.hidden = count === 0;
    hiddenText.textContent = `已隐藏 ${count} 门课程`;
    hideBtn.disabled = state.busy || state.selected < 0;
  }

  function toast(msg) {
    const wrap = $('toasts');
    if (!wrap) return;
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(() => {
      el.classList.add('out');
      setTimeout(() => el.remove(), 220);
    }, 2400);
  }

  function setDot(kind) {
    dotEl.className = 'dot' + (kind ? ' ' + kind : '');
  }

  function showNotice(text) {
    noticeText.textContent = text;
    noticeEl.hidden = false;
  }

  function hideNotice() {
    noticeEl.hidden = true;
  }

  function setStatus(text, kind) {
    statusEl.textContent = text;
    setDot(kind);
  }

  function countUp(el, to, suffix = '门') {
    const from = Number(el.dataset.v || 0);
    if (from === to) { el.textContent = to ? `${to} ${suffix}` : ''; return; }
    const start = performance.now();
    const dur = 420;
    const settle = () => {
      el.textContent = to ? `${to} ${suffix}` : '';
      el.dataset.v = String(to);
    };
    const step = (now) => {
      // 页面不可见时不再排新帧，直接落定最终数字
      if (!pageActive) { settle(); return; }
      const t = Math.min(1, (now - start) / dur);
      const e = 1 - Math.pow(1 - t, 3);
      el.textContent = `${Math.round(from + (to - from) * e)} ${suffix}`;
      if (t < 1) requestAnimationFrame(step);
      else settle();
    };
    requestAnimationFrame(step);
  }

  /* ------------------------------------------------------------ 渲染 */
  function skeleton(n) {
    rowsEl.innerHTML = '';
    const frag = document.createDocumentFragment();
    for (let i = 0; i < n; i++) {
      const r = document.createElement('div');
      r.className = 'skel-row';
      r.innerHTML = '<span class="skel w70"></span><span class="skel w40"></span>' +
                    '<span class="skel w50"></span><span class="skel w30"></span>';
      frag.appendChild(r);
    }
    rowsEl.appendChild(frag);
    deckEl.innerHTML = '<article class="course-card card-skeleton is-active" aria-label="正在加载课程">' +
      '<div class="card-inner"><div class="skel-line"></div><div class="skel-title"></div>' +
      '<div class="skel-title short"></div><div class="skel-block"></div></div></article>';
    deckPager.innerHTML = '';
    deckCounter.textContent = '—';
    prevBtn.disabled = true;
    nextBtn.disabled = true;
  }

  // 排序：正在直播 > 未开始（近的在前）> 已结束（新的在前）
  // 服务端按 startTime 升序返回，直接渲染会让上百条历史课压在正在上的课前面。
  function orderCourses(list) {
    const rank = { [ST_LIVE]: 0, [ST_SOON]: 1, [ST_DONE]: 2 };
    return [...list].sort((a, b) => {
      const ra = rank[a.statusName] ?? 3;
      const rb = rank[b.statusName] ?? 3;
      if (ra !== rb) return ra - rb;
      const ta = String(a.startTime || '');
      const tb = String(b.startTime || '');
      return ra === 2 ? tb.localeCompare(ta) : ta.localeCompare(tb);
    });
  }

  function renderRows(input, { animate = true } = {}) {
    const courses = orderCourses(input || []);
    rowsEl.innerHTML = '';
    if (!courses.length) {
      rowsEl.innerHTML = '<div class="empty"><strong>没有拿到课程</strong>' +
        '检查是否连上校园网或 VPN，然后点右上角刷新。</div>';
      return;
    }

    const frag = document.createDocumentFragment();
    courses.forEach((c, i) => {
      const row = document.createElement('div');
      row.className = 'row' + (animate ? ' enter' : '') +
        (isLiveCourse(c) ? ' live-now' : '');
      row.style.setProperty('--i', String(Math.min(i, 28)));
      row.dataset.index = String(i);
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', 'false');

      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = c.name || '(未命名)';

      const st = document.createElement('span');
      st.className = 'st ' + (STATUS_CLASS[c.statusName] || '');
      st.textContent = c.statusName || '未知';

      const time = document.createElement('span');
      time.className = 'time';
      time.textContent = fmtTime(c.startTime);

      const room = document.createElement('span');
      room.className = 'room';
      room.textContent = c.classroomName || '—';

      row.append(name, st, time, room);
      frag.appendChild(row);
    });
    rowsEl.appendChild(frag);
    rowsEl.scrollTop = 0;
  }

  function makeCard(course, index) {
    const view = courseView(course);
    const tone = CARD_TONES[index % CARD_TONES.length];
    const isLive = isLiveCourse(course);
    const card = document.createElement('article');
    card.className = 'course-card';
    card.dataset.cardIndex = String(index);
    card.setAttribute('role', 'button');
    card.setAttribute('aria-label',
      `${view.title}，${view.date} ${view.clock}，${view.room}，${isLive ? ST_LIVE : ST_SOON}`);
    card.tabIndex = -1;
    card.style.setProperty('--card-accent', tone.accent);
    card.style.setProperty('--card-wash', tone.wash);
    card.classList.toggle('is-live', isLive);
    card.innerHTML = '<div class="card-inner">' +
      `<div class="card-top"><span class="card-status${isLive ? ' live' : ''}"><i></i>${isLive ? ST_LIVE : ST_SOON}</span>` +
      '<span class="card-index"></span></div>' +
      '<div class="card-title-wrap"><p class="card-overline"></p>' +
      '<h3 class="card-title"></h3><p class="card-teacher"></p></div>' +
      '<div class="card-info">' +
      '<div class="card-meta"><span class="card-label">日期</span><strong class="card-value date"></strong><small class="card-sub day"></small></div>' +
      '<div class="card-meta time"><span class="card-label">开始时间</span><strong class="card-value clock"></strong><small class="card-sub session"></small></div>' +
      '<div class="card-meta"><span class="card-label">教室</span><strong class="card-value room"></strong><small class="card-sub week"></small></div>' +
      '</div></div>';

    card.querySelector('.card-index').textContent = `${String(index + 1).padStart(2, '0')} / ${String(state.upcoming.length).padStart(2, '0')}`;
    card.querySelector('.card-overline').textContent =
      isLive ? 'LIVE NOW' : (index === 0 ? 'NEXT CLASS' : 'UPCOMING CLASS');
    card.querySelector('.card-title').textContent = view.title;
    card.querySelector('.card-teacher').textContent = view.teacher;
    card.querySelector('.date').textContent = view.date;
    card.querySelector('.day').textContent = view.dayNote;
    card.querySelector('.clock').textContent = view.clock;
    card.querySelector('.session').textContent = view.session || '课程直播';
    card.querySelector('.room').textContent = view.room;
    card.querySelector('.week').textContent = view.week || '直播教室';
    return card;
  }

  function updateSelection() {
    rowsEl.querySelectorAll('.row').forEach((row, i) => {
      const on = i === state.selected;
      row.classList.toggle('sel', on);
      row.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    playBtn.disabled = state.busy || state.selected < 0;
    $('playHint').textContent = allCoursesEl.open
      ? '双击课程行开启直播 · Ctrl+C 复制 HLS 地址'
      : '左右切换课程 · 双击当前卡片或按 Enter 开播';
    updateHiddenTools();
  }

  function updateDeck() {
    const cards = deckEl.querySelectorAll('.course-card:not(.card-skeleton)');
    cards.forEach((card, i) => {
      const offset = i - state.activeCard;
      const distance = Math.abs(offset);
      card.style.setProperty('--x', `${offset * 47}%`);
      card.style.setProperty('--y', `${distance * 13}px`);
      card.style.setProperty('--scale', String(Math.max(0.8, 1 - distance * 0.08)));
      card.style.setProperty('--rotate', `${offset * -3.5}deg`);
      card.style.zIndex = String(30 - distance);
      card.style.opacity = distance === 0 ? '1' : distance === 1 ? '0.62' : distance === 2 ? '0.2' : '0';
      card.style.pointerEvents = distance > 2 ? 'none' : 'auto';
      card.classList.toggle('is-active', distance === 0);
      card.tabIndex = distance === 0 ? 0 : -1;
      card.setAttribute('aria-hidden', distance > 2 ? 'true' : 'false');
    });

    deckPager.querySelectorAll('.pager-dot').forEach((dot, i) => {
      const on = i === state.activeCard;
      dot.classList.toggle('active', on);
      if (on) dot.setAttribute('aria-current', 'true');
      else dot.removeAttribute('aria-current');
    });

    const total = state.upcoming.length;
    deckCounter.textContent = total ? `${String(state.activeCard + 1).padStart(2, '0')} / ${String(total).padStart(2, '0')}` : '—';
    prevBtn.disabled = !total || state.activeCard === 0;
    nextBtn.disabled = !total || state.activeCard === total - 1;
    updateSelection();
  }

  function renderDeck() {
    deckEl.innerHTML = '';
    deckPager.innerHTML = '';

    // 标题跟着内容走：有直播中的课就先说"正在直播"
    const liveCount = state.upcoming.filter(isLiveCourse).length;
    const eyebrow = $('deckEyebrow');
    const title = $('upcomingTitle');
    if (liveCount > 0) {
      if (eyebrow) eyebrow.textContent = 'LIVE NOW';
      if (title) title.textContent = liveCount > 1 ? `正在直播 · ${liveCount} 场` : '正在直播';
    } else {
      if (eyebrow) eyebrow.textContent = 'UP NEXT';
      if (title) title.textContent = '接下来';
    }
    if (!state.upcoming.length) {
      const hasCourses = state.courses.length > 0;
      deckEl.innerHTML = '<div class="empty-deck"><div class="empty-mark">✓</div>' +
        `<strong>${hasCourses ? '最近没有课程' : '还没有拿到课程'}</strong>` +
        `<p>${hasCourses ? '可展开下方「全部课程」查看历史记录。' : '检查校园网或 VPN 连接，然后点右上角刷新。'}</p></div>`;
      updateDeck();
      return;
    }

    const cards = document.createDocumentFragment();
    const dots = document.createDocumentFragment();
    state.upcoming.forEach((course, i) => {
      cards.appendChild(makeCard(course, i));
      const dot = document.createElement('button');
      dot.type = 'button';
      dot.className = 'pager-dot';
      dot.dataset.cardIndex = String(i);
      dot.setAttribute('aria-label', `查看第 ${i + 1} 场待开始课程`);
      dots.appendChild(dot);
    });
    deckEl.appendChild(cards);
    deckPager.appendChild(dots);
    updateDeck();
  }

  function renderCourses(courses, { animate = true } = {}) {
    const flipped = advanceStatuses(courses);
    const previousCard = state.upcoming[state.activeCard];
    const previousId = previousCard && previousCard.id;
    state.allCourses = courses;
    state.courses = courses.filter((course) => !state.hidden.has(courseIdentity(course)));
    // 卡片堆叠：正在直播的排最前（强调"现在能看"），其后才是待开始
    state.upcoming = [
      ...state.courses.filter(isLiveCourse),
      ...state.courses
        .filter((c) => c.statusName === '未开始')
        .sort((a, b) => String(a.startTime || '').localeCompare(String(b.startTime || ''))),
    ];

    const kept = previousId == null ? -1 : state.upcoming.findIndex((course) => course.id === previousId);
    state.activeCard = kept >= 0 ? kept : 0;
    state.selected = state.upcoming.length
      ? state.courses.indexOf(state.upcoming[state.activeCard])
      : -1;

    renderRows(state.courses, { animate });
    renderDeck();
    $('allCount').textContent = `${state.courses.length} 门`;
    countUp(countEl, state.upcoming.length, '场');
    updateHiddenTools();
    scheduleStatusTick();
    if (flipped.firstLive) {
      const t = courseView(flipped.firstLive).title;
      toast(flipped.live > 1 ? `${flipped.live} 门课已经开课` : `「${t}」已经开课`);
    }
  }

  function hideSelectedCourse() {
    if (state.busy || state.selected < 0) return;
    const course = state.courses[state.selected];
    if (!course) return;
    state.hidden.add(courseIdentity(course));
    saveHiddenCourses();
    renderCourses(state.allCourses, { animate: false });
    toast(`已隐藏「${courseView(course).title}」`);
  }

  function restoreHiddenCourses() {
    if (!state.hidden.size) return;
    state.hidden.clear();
    saveHiddenCourses();
    renderCourses(state.allCourses, { animate: false });
    toast('已恢复全部隐藏课程');
  }

  function select(index, { scroll = true } = {}) {
    if (!state.courses.length) return;
    const i = Math.max(0, Math.min(index, state.courses.length - 1));
    state.selected = i;
    const cardIndex = state.upcoming.indexOf(state.courses[i]);
    if (cardIndex >= 0) state.activeCard = cardIndex;
    updateDeck();
    if (scroll && allCoursesEl.open) {
      const row = rowsEl.querySelector(`[data-index="${i}"]`);
      if (row) row.scrollIntoView({ block: 'nearest' });
    }
  }

  function selectCard(index) {
    if (!state.upcoming.length) return;
    state.activeCard = Math.max(0, Math.min(index, state.upcoming.length - 1));
    state.selected = state.courses.indexOf(state.upcoming[state.activeCard]);
    updateDeck();
  }

  /* ------------------------------------------------------------ API */
  async function api(path, body, _retry = 1) {
    const opt = body === undefined
      ? { method: 'GET', cache: 'no-store' }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    let r;
    try {
      r = await fetch(path, opt);
    } catch (e) {
      // 启动瞬间服务可能还没 bind 完，重试一次再报错
      if (_retry > 0) {
        await new Promise((res) => setTimeout(res, 400));
        return api(path, body, _retry - 1);
      }
      throw e;
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }

  let pollTimer = 0;
  let pollCount = 0;
  let retryCount = 0;

  function scheduleOfflineProbe(delay = 20000) {
    clearTimeout(offlineTimer);
    if (!offlineMode || !pageActive) return;
    offlineTimer = setTimeout(probeOffline, delay);
  }

  async function probeOffline() {
    if (!offlineMode || !pageActive) return;
    if (state.busy || offlineProbeRunning) {
      scheduleOfflineProbe(8000);
      return;
    }
    offlineProbeRunning = true;
    try {
      const d = await api('/api/diagnose', {});
      if (d.net && d.net.api) {
        offlineMode = false;
        clearTimeout(offlineTimer);
        setStatus('网络已恢复，正在刷新课表…', 'busy');
        await load(true);
      } else {
        scheduleOfflineProbe();
      }
    } catch (_) {
      scheduleOfflineProbe();
    } finally {
      offlineProbeRunning = false;
    }
  }

  async function load(force) {
    if (state.busy) return;
    state.busy = true;
    refreshBtn.disabled = true;
    playBtn.disabled = true;
    hideBtn.disabled = true;
    setStatus(force ? '正在重新拉取课表…' : '正在连接…', 'busy');
    if (!state.loaded) skeleton(8);

    try {
      const data = await api(force ? '/api/refresh' : '/api/state', force ? {} : undefined);

      // 后端异步加载：还没就绪就轮询，别让用户盯着空列表
      if (!force && data && data.loaded === false) {
        state.busy = false;
        refreshBtn.disabled = false;
        if (pollCount++ < 30) {
          setStatus(data.status || '正在获取最新课表…', 'busy');
          clearTimeout(pollTimer);
          pollTimer = setTimeout(() => load(false), 1500);
        } else {
          setStatus('加载超时，点刷新重试', 'err');
          pollCount = 0;
        }
        return;
      }
      pollCount = 0;
      retryCount = 0;
      state.loaded = true;
      state.username = data.username || state.username || '';
      state.hidden = readHiddenCourses();
      renderCourses(data.courses || [], { animate: true });
      const n = (data.courses || []).length;
      const recovered = offlineMode && !data.offline;
      offlineMode = Boolean(data.offline);
      if (offlineMode) scheduleOfflineProbe();
      else clearTimeout(offlineTimer);
      if (data.offline) {
        setStatus(data.status || '离线数据', 'offline');
        showNotice(
          `当前连不上校内服务器，显示的是 ${data.cachedAt || '上次'} 缓存的课表。` +
          '连上校园网 WiFi 或 EasyConnect VPN 后点「重新检测」。'
        );
      } else {
        setStatus(data.status || `就绪 · ${n} 场直播`, n ? 'ok' : '');
        if (data.error) {
          showNotice(data.error);
        } else {
          hideNotice();
        }
      }
      if (recovered) toast('校园网已恢复，课表已更新');
      if (data.error && !data.offline) toast(data.error);
    } catch (e) {
      setStatus('连接失败：' + e.message, 'err');
      showNotice('连不上本地服务。正在自动重试，若一直失败请关掉窗口重新双击桌面图标。');
      // 最多自动重试 3 次（每次 1.2s），避免启动瞬间误报
      if (retryCount < 3) {
        retryCount += 1;
        clearTimeout(pollTimer);
        pollTimer = setTimeout(() => load(false), 1200);
      }
      state.courses = [];
      state.allCourses = [];
      state.upcoming = [];
      state.selected = -1;
      rowsEl.innerHTML = '<div class="empty"><strong>连不上本地服务</strong>' +
        '请确认启动器还在运行，然后重试。</div>';
      deckEl.innerHTML = '<div class="empty-deck"><strong>暂时连不上本地服务</strong>' +
        '<p>请确认启动器仍在运行，然后重新打开 HFUTone。</p></div>';
      deckPager.innerHTML = '';
      deckCounter.textContent = '—';
      prevBtn.disabled = true;
      nextBtn.disabled = true;
      $('allCount').textContent = '0 门';
      countUp(countEl, 0, '场');
      updateHiddenTools();
    } finally {
      state.busy = false;
      refreshBtn.disabled = false;
      updateSelection();
    }
  }

  async function play() {
    if (state.selected < 0 || state.busy) return;
    const course = state.courses[state.selected];
    if (!course) return;

    state.busy = true;
    playBtn.disabled = true;
    hideBtn.disabled = true;
    const old = playBtn.textContent;
    playBtn.textContent = '正在取流…';
    setStatus('正在拉取：' + (course.name || '').slice(0, 22), 'busy');

    try {
      const res = await api('/api/play', { id: course.id });
      if (res.ok) {
        setStatus(res.message || '直播已开启', 'ok');
        toast(res.message || '直播已开启');
      } else {
        setStatus(res.message || '取流失败', 'err');
        toast(res.message || '取流失败');
      }
    } catch (e) {
      setStatus('取流失败：' + e.message, 'err');
      toast('取流失败：' + e.message);
    } finally {
      state.busy = false;
      playBtn.textContent = old;
      updateSelection();
    }
  }

  async function copyStream(kind) {
    if (state.selected < 0) return;
    const course = state.courses[state.selected];
    try {
      const res = await api('/api/streams', { id: course.id });
      const lines = (res.streams || []).map((s) => s[kind]).filter(Boolean);
      if (!lines.length) { toast('这一场没有可用的流地址'); return; }
      await navigator.clipboard.writeText(lines.join('\n'));
      toast(`已复制 ${lines.length} 条 ${kind.toUpperCase()} 地址`);
    } catch (e) {
      toast('复制失败：' + e.message);
    }
  }

  /* ------------------------------------------------------------ 事件 */
  on(rowsEl, 'click', (e) => {
    const row = e.target.closest('.row');
    if (row) select(Number(row.dataset.index));
  });

  on(rowsEl, 'dblclick', (e) => {
    const row = e.target.closest('.row');
    if (row) { select(Number(row.dataset.index)); play(); }
  });

  let swipeStart = null;
  let suppressDeckClickUntil = 0;

  on(deckEl, 'click', (e) => {
    const card = e.target.closest('.course-card:not(.card-skeleton)');
    if (!card || Date.now() < suppressDeckClickUntil) return;
    selectCard(Number(card.dataset.cardIndex));
  });

  on(deckEl, 'dblclick', (e) => {
    const card = e.target.closest('.course-card:not(.card-skeleton)');
    if (!card || Date.now() < suppressDeckClickUntil) return;
    selectCard(Number(card.dataset.cardIndex));
    play();
  });

  on(deckEl, 'pointerdown', (e) => {
    if (e.button !== 0) return;
    swipeStart = { x: e.clientX, y: e.clientY };
  });
  on(deckEl, 'pointercancel', () => { swipeStart = null; });
  on(deckEl, 'pointerup', (e) => {
    if (!swipeStart) return;
    const dx = e.clientX - swipeStart.x;
    const dy = e.clientY - swipeStart.y;
    swipeStart = null;
    if (Math.abs(dx) < 46 || Math.abs(dx) < Math.abs(dy) * 1.25) return;
    suppressDeckClickUntil = Date.now() + 260;
    selectCard(state.activeCard + (dx < 0 ? 1 : -1));
  });

  on(deckPager, 'click', (e) => {
    const dot = e.target.closest('.pager-dot');
    if (dot) selectCard(Number(dot.dataset.cardIndex));
  });
  on(prevBtn, 'click', () => selectCard(state.activeCard - 1));
  on(nextBtn, 'click', () => selectCard(state.activeCard + 1));

  on(allCoursesEl, 'toggle', () => {
    if (!workspaceEl) return;
    workspaceEl.classList.toggle('all-open', allCoursesEl.open);
    if (!allCoursesEl.open && state.upcoming.length) {
      selectCard(state.activeCard);
    } else {
      updateSelection();
      if (allCoursesEl.open && state.selected >= 0) {
        requestAnimationFrame(() => select(state.selected));
      }
    }
  });

  // 卡片与表格共用一套课程选择；键盘操作跟随当前视图。
  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (backdrop.classList.contains('open')) return;
    if ((tag === 'BUTTON' || tag === 'SUMMARY') && (e.key === 'Enter' || e.key === ' ')) return;

    if (allCoursesEl.open) {
      if (e.key === 'ArrowDown') { e.preventDefault(); select(state.selected + 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); select(state.selected - 1); }
      else if (e.key === 'Home') { e.preventDefault(); select(0); }
      else if (e.key === 'End') { e.preventDefault(); select(state.courses.length - 1); }
    } else if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
      e.preventDefault(); selectCard(state.activeCard + 1);
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
      e.preventDefault(); selectCard(state.activeCard - 1);
    } else if (e.key === 'Home') { e.preventDefault(); selectCard(0); }
    else if (e.key === 'End') { e.preventDefault(); selectCard(state.upcoming.length - 1); }

    if (e.key === 'Enter' || (e.key === ' ' && e.target.closest?.('.course-card'))) {
      e.preventDefault(); play();
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'F5' || (e.ctrlKey && e.key.toLowerCase() === 'r')) {
      e.preventDefault(); load(true);
    } else if (e.ctrlKey && e.key.toLowerCase() === 'c' && state.selected >= 0 && !window.getSelection().toString()) {
      e.preventDefault(); copyStream('hls');
    }
  });

  on(diagnoseBtn, 'click', async () => {
    diagnoseBtn.disabled = true;
    const old = diagnoseBtn.textContent;
    diagnoseBtn.textContent = '检测中…';
    try {
      const d = await api('/api/diagnose', {});
      if (d.net && d.net.api) {
        hideNotice();
        toast('校内服务可达，正在刷新');
        load(true);
      } else {
        showNotice(d.hint || '校内服务不可达。');
        setStatus('校内服务不可达', 'offline');
      }
    } catch (e) {
      showNotice('检测失败：' + e.message);
    } finally {
      diagnoseBtn.disabled = false;
      diagnoseBtn.textContent = old;
    }
  });

  on($('btnClose'), 'click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const d = await api('/api/close_players', {});
      toast(d.message || '已关闭');
    } catch (err) {
      toast('关闭失败：' + err.message);
    } finally {
      btn.disabled = false;
    }
  });

  on(playBtn, 'click', play);
  on(hideBtn, 'click', hideSelectedCourse);
  on(unhideBtn, 'click', restoreHiddenCourses);
  on(refreshBtn, 'click', () => load(true));

  /* ------------------------------------------------------------ 启动 */
  document.addEventListener('visibilitychange', () => setPageActive(!document.hidden));
  window.addEventListener('blur', () => setPageActive(false));
  window.addEventListener('focus', () => setPageActive(!document.hidden));
  window.addEventListener('online', () => {
    if (!state.busy) {
      setStatus('网络连接已恢复，正在刷新…', 'busy');
      load(true);
    }
  });

  (function bootOcean() {
    const canvas = $('ocean');
    const Engine = window.AsciiSharks && window.AsciiSharks.OceanEngine;
    if (!Engine) return;
    try {
      // 与 jiuwanmu 控制台同一组：经典 / 双髻 / 灰鲭 / 长尾
      oceanEngine = new Engine(canvas, [
        { kind: 'classic', size: 0.86 },
        { kind: 'hammerhead', size: 0.70 },
        { kind: 'mako', size: 0.56 },
        { kind: 'thresher', size: 0.64 },
      ]);
      oceanEngine.setActive(pageActive);
    } catch (err) {
      canvas.style.display = 'none';
      window.HFUTLiveBoot?.report('ocean', { message: err.message, error: err });
    }
  })();

  load(false);
})();
