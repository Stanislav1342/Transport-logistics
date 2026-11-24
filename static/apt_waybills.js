(function () {
  'use strict';

  const DEFAULT_PROTECTED_LABELS = new Set([
    'Окно аптеки',
    'Qr code',
    'Титульный лист',
    'Номер МЛ',
    'ФИО водителя',
  ]);

  const DEFAULT_DIGIT_LABELS = new Set([
    'Рукописная цифра',
    'Печатная цифра',
  ]);

  const DEFAULT_OPERATOR_LABELS = [
    'Печать',
    'Плохая печать',
    'Подпись',
    'Расшифровка',
    'Расшифровка печать',
    'Рукописная цифра',
    'Исправление',
    'Печать экспедиции',
  ];

  const QUICK_RELABEL_ORDER = [
    'Расшифровка',
    'Подпись',
    'Печать',
    'Печать экспедиции',
    'Рукописная цифра',
    'Печатная цифра',
    'Расшифровка печать',
    'Плохая печать',
    'Исправление',
  ];

  const STAGE_ORDER = { render: 1, detect: 2, analyze: 3, done: 4 };

  const state = {
    initialized: false,
    protectedLabels: new Set(DEFAULT_PROTECTED_LABELS),
    digitLabels: new Set(DEFAULT_DIGIT_LABELS),
    operatorAddLabels: [...DEFAULT_OPERATOR_LABELS],
    editableLabels: new Set(QUICK_RELABEL_ORDER),
    selectedFile: null,
    uploading: false,
    actionPending: false,
    pollTimer: null,
    activeSessionId: null,
    session: null,
    recentSessions: [],
    filterErrorsOnly: true,
    currentPageId: null,
    currentWindowIdx: 0,
    selectedElementId: null,
    hoveredElementId: null,
    highlightedElementIds: new Set(),
    expandedHintKey: null,
    drawMode: null, // { label: string }
    dragState: null, // { startX, startY, currX, currY, pointerId }
    currentWindowImg: null,
    currentWindowImgKey: null,
    operatorUrls: new Set(),
    quarantineUrls: new Set(),
    fullPageUrl: null,
    qStatusFilter: 'pending',
    qItems: [],
    qActiveItem: null,
    qLoading: false,
    confirmHintCommentKey: null,
  };

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return '0 Б';
    const units = ['Б', 'КБ', 'МБ', 'ГБ'];
    let val = bytes;
    let idx = 0;
    while (val >= 1024 && idx < units.length - 1) {
      val /= 1024;
      idx += 1;
    }
    return `${val >= 10 || idx === 0 ? val.toFixed(0) : val.toFixed(1)} ${units[idx]}`;
  }

  function authHeaders(extra) {
    if (typeof window.authHeaders === 'function') {
      return window.authHeaders(extra);
    }
    const headers = Object.assign({}, extra || {});
    if (window.authToken) {
      headers['X-Auth-Token'] = window.authToken;
    }
    return headers;
  }

  function handleUnauthorized() {
    if (typeof window.lockScreen === 'function') {
      window.lockScreen('Сессия завершена. Введите PIN-код снова.');
    }
  }

  async function apiJson(url, options = {}) {
    const opts = Object.assign({ cache: 'no-store' }, options);
    opts.headers = authHeaders(options.headers || {});
    const resp = await fetch(url, opts);
    if (resp.status === 401) {
      handleUnauthorized();
      throw new Error('Требуется повторная авторизация.');
    }
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
      const msg = (data && (data.detail || data.error)) || `Ошибка сервера (HTTP ${resp.status})`;
      throw new Error(msg);
    }
    return data;
  }

  async function fetchBlobUrl(url, poolSet) {
    const resp = await fetch(url, { headers: authHeaders(), cache: 'no-store' });
    if (resp.status === 401) {
      handleUnauthorized();
      throw new Error('Требуется авторизация.');
    }
    if (!resp.ok) {
      const data = await resp.json().catch(() => null);
      throw new Error((data && (data.detail || data.error)) || `Ошибка загрузки (${resp.status})`);
    }
    const blob = await resp.blob();
    const objUrl = URL.createObjectURL(blob);
    poolSet.add(objUrl);
    return objUrl;
  }

  function revokeUrlPool(poolSet) {
    for (const url of poolSet) {
      try {
        URL.revokeObjectURL(url);
      } catch (_) {}
    }
    poolSet.clear();
  }

  function isElementEditable(el) {
    return Boolean(el) && el.editable !== false && !state.protectedLabels.has(el.label);
  }

  function getSafeOperatorLabels() {
    const ordered = QUICK_RELABEL_ORDER.filter(
      lbl => !state.protectedLabels.has(lbl) && state.editableLabels.has(lbl)
    );
    for (const lbl of state.operatorAddLabels) {
      if (!state.protectedLabels.has(lbl) && !ordered.includes(lbl)) {
        ordered.push(lbl);
      }
    }
    return ordered;
  }

  function shortLabel(el) {
    if (!el) return '';
    if (el.label === 'Рукописная цифра') {
      return `рц (${el.ocr_value ?? '?'})`;
    }
    if (el.label === 'Печатная цифра') {
      return `пц (${el.ocr_value ?? '?'})`;
    }
    return el.label;
  }

  function hasWindowDiff(win) {
    if (!win) return false;
    const orig = win.original_elements || [];
    const curr = win.elements || [];
    if (orig.length !== curr.length) return true;
    const origMap = new Map(orig.map(e => [e.id, e]));
    for (const el of curr) {
      const o = origMap.get(el.id);
      if (!o) return true;
      if (o.label !== el.label) return true;
      if (state.digitLabels.has(el.label) && (o.ocr_value ?? null) !== (el.ocr_value ?? null)) {
        return true;
      }
    }
    return false;
  }

  function hasPageChanges(page) {
    if (!page) return false;
    return (page.windows || []).some(hasWindowDiff);
  }

  function pageHasErrors(page) {
    if (!page) return false;
    return (page.windows || []).some(w => (w.errors && w.errors.length > 0) || (w.warnings && w.warnings.length > 0));
  }

  function pageHasPendingErrors(page) {
    if (!page) return false;
    return (page.windows || []).some(w => w.review_status === 'pending' || w.review_status === 'fixed_with_errors');
  }

  function getFilteredPages() {
    if (!state.session) return [];
    const pages = state.session.pages || [];
    if (!state.filterErrorsOnly) return pages;
    const problemPages = pages.filter(p => pageHasErrors(p) || hasPageChanges(p));
    return problemPages.length > 0 ? problemPages : pages;
  }

  function getCurrentPage() {
    if (!state.session || !state.currentPageId) return null;
    return (state.session.pages || []).find(p => p.page_id === state.currentPageId) || null;
  }

  function getCurrentWindow() {
    const page = getCurrentPage();
    if (!page) return null;
    const wins = page.windows || [];
    return wins[state.currentWindowIdx] || wins[0] || null;
  }

  // ─── Здоровье ML-сервиса и недавние сессии ──────────────────────────
  async function checkMlHealth(silent = false) {
    const pill = $('apt-ml-health-badge');
    if (pill && !silent) {
      pill.className = 'apt-health-pill';
      pill.innerHTML = '<span class="apt-health-dot"></span> Проверка ML-сервиса...';
    }
    try {
      const data = await apiJson('/api/ml/health');
      if (Array.isArray(data.protected_labels)) {
        state.protectedLabels = new Set(data.protected_labels);
      }
      if (Array.isArray(data.digit_labels)) {
        state.digitLabels = new Set(data.digit_labels);
      }
      if (Array.isArray(data.editable_labels)) {
        state.editableLabels = new Set(data.editable_labels.filter(l => !state.protectedLabels.has(l)));
      }
      if (Array.isArray(data.operator_add_labels)) {
        state.operatorAddLabels = data.operator_add_labels.filter(l => !state.protectedLabels.has(l));
      }
      if (pill) {
        pill.className = 'apt-health-pill is-ok';
        const ver = data.version ? ` v${escapeHtml(data.version)}` : '';
        pill.innerHTML = `<span class="apt-health-dot"></span> ML-сервис готов${ver}`;
      }
      return true;
    } catch (err) {
      if (pill) {
        pill.className = 'apt-health-pill is-err';
        pill.innerHTML = `<span class="apt-health-dot"></span> ML недоступен`;
        pill.title = err.message || '';
      }
      return false;
    }
  }

  async function loadRecentSessions() {
    const bar = $('apt-sessions-bar');
    if (!bar) return;
    try {
      const data = await apiJson('/api/ml/sessions?limit=15');
      const items = Array.isArray(data) ? data : (Array.isArray(data?.sessions) ? data.sessions : []);
      state.recentSessions = items;
      if (state.recentSessions.length === 0) {
        bar.innerHTML = '<span class="apt-muted">Пока нет сохранённых проверок PDF. Загрузите первый файл выше.</span>';
        return;
      }
      bar.innerHTML = state.recentSessions.map(item => {
        const isActive = item.session_id === state.activeSessionId;
        const summary = item.summary || {};
        const errPages = summary.error_pages ?? 0;
        const statusIcon = item.status === 'ready' ? (errPages > 0 ? '⚠️' : '✅') : (item.status === 'error' ? '❌' : '⏳');
        const fname = escapeHtml(item.source_filename || item.session_id);
        const meta = item.status === 'ready'
          ? `${summary.total_waybills ?? 0} МЛ / ош: ${summary.pending_pages ?? errPages}`
          : `${item.progress ?? 0}%`;
        return `
          <button type="button" class="apt-session-chip ${isActive ? 'active' : ''}" data-session-id="${escapeHtml(item.session_id)}">
            <span>${statusIcon}</span>
            <span>${fname}</span>
            <span style="opacity:0.8; font-weight:500;">(${escapeHtml(meta)})</span>
          </button>
        `;
      }).join('');
      bar.querySelectorAll('.apt-session-chip').forEach(btn => {
        btn.addEventListener('click', () => {
          const sid = btn.getAttribute('data-session-id');
          if (sid) openExistingSession(sid);
        });
      });
    } catch (err) {
      bar.innerHTML = `<span style="color:var(--error); font-size:12.5px;">${escapeHtml(err.message)}</span>`;
    }
  }

  // ─── Загрузка PDF и прогресс ────────────────────────────────────────
  function updateDropzoneUI() {
    const emptyBox = $('apt-pdf-dropzone');
    const loadedBox = $('apt-pdf-loaded');
    const startBtn = $('apt-start-check-btn');
    if (!emptyBox || !loadedBox || !startBtn) return;

    if (!state.selectedFile) {
      emptyBox.hidden = false;
      loadedBox.hidden = true;
      startBtn.disabled = true;
      return;
    }

    emptyBox.hidden = true;
    loadedBox.hidden = false;
    $('apt-pdf-name').textContent = state.selectedFile.name;
    $('apt-pdf-size').textContent = formatBytes(state.selectedFile.size);
    startBtn.disabled = state.uploading;
  }

  function handlePdfFileSelect(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      alert('Пожалуйста, выберите файл в формате PDF.');
      return;
    }
    if (file.size > 100 * 1024 * 1024) {
      alert('Файл превышает максимальный размер 100 МБ.');
      return;
    }
    state.selectedFile = file;
    updateDropzoneUI();
  }

  function clearSelectedPdf() {
    state.selectedFile = null;
    const inp = $('apt-pdf-input');
    if (inp) inp.value = '';
    updateDropzoneUI();
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function renderProgress(statusData) {
    const card = $('apt-progress-card');
    if (!card) return;
    card.style.display = 'block';
    const stage = String(statusData.stage || 'render');
    const pct = Math.max(0, Math.min(100, Number(statusData.progress || 0)));
    const curRank = STAGE_ORDER[stage] || 1;

    ['render', 'detect', 'analyze'].forEach(st => {
      const el = $(`apt-step-${st}`);
      if (!el) return;
      const r = STAGE_ORDER[st];
      el.classList.remove('is-active', 'is-done');
      if (statusData.status === 'ready' || r < curRank) {
        el.classList.add('is-done');
      } else if (r === curRank) {
        el.classList.add('is-active');
      }
    });

    const fill = $('apt-progress-fill');
    if (fill) fill.style.width = `${pct}%`;

    const proc = statusData.processed_pages ?? 0;
    const tot = statusData.total_pages ?? 0;
    const stageLabels = {
      queued: 'Постановка в очередь...',
      render: 'Рендер страниц PDF...',
      detect: 'Детекция окон, печатей и подписей...',
      analyze: 'OCR и проверка правил окон...',
      done: 'Анализ завершён',
      error: 'Ошибка анализа',
    };
    const txt = $('apt-progress-text');
    if (txt) {
      const pagePart = tot > 0 ? ` (${proc} из ${tot} стр.)` : '';
      txt.textContent = `${stageLabels[stage] || stage}${pagePart} — ${pct}%`;
    }

    const errBox = $('apt-progress-error');
    if (errBox) {
      if (statusData.status === 'error') {
        errBox.style.display = 'block';
        errBox.textContent = statusData.error || 'Не удалось обработать PDF.';
      } else {
        errBox.style.display = 'none';
      }
    }
  }

  async function startPdfCheck() {
    if (!state.selectedFile || state.uploading) return;
    stopPolling();
    state.uploading = true;
    updateDropzoneUI();

    const errBox = $('apt-progress-error');
    if (errBox) errBox.style.display = 'none';

    const startBtn = $('apt-start-check-btn');
    if (startBtn) {
      startBtn.disabled = true;
      startBtn.textContent = '⏳ Загрузка PDF в ML-сервис...';
    }

    try {
      const maxPagesInp = $('apt-max-pages-input');
      const maxPages = Math.max(1, Math.min(250, Number(maxPagesInp?.value || 150) || 150));
      const fd = new FormData();
      fd.append('file', state.selectedFile, state.selectedFile.name);
      fd.append('max_pages', String(maxPages));

      const res = await apiJson(
        `/api/ml/sessions/upload?filename=${encodeURIComponent(state.selectedFile.name)}&max_pages=${maxPages}`,
        { method: 'POST', body: fd }
      );

      state.activeSessionId = res.session_id;
      renderProgress(res);
      startStatusPolling(res.session_id);
      loadRecentSessions();
    } catch (err) {
      renderProgress({ status: 'error', stage: 'error', progress: 0, error: err.message });
    } finally {
      state.uploading = false;
      updateDropzoneUI();
      if (startBtn) {
        startBtn.textContent = '🚀 Проверить маршрутные листы';
      }
    }
  }

  function startStatusPolling(sessionId) {
    stopPolling();
    const tick = async () => {
      try {
        const st = await apiJson(`/api/ml/sessions/${encodeURIComponent(sessionId)}/status`);
        renderProgress(st);
        if (st.status === 'ready') {
          stopPolling();
          const card = $('apt-progress-card');
          if (card) card.style.display = 'none';
          await loadFullSession(sessionId);
          loadRecentSessions();
        } else if (st.status === 'error') {
          stopPolling();
        }
      } catch (err) {
        stopPolling();
        renderProgress({ status: 'error', stage: 'error', progress: 0, error: err.message });
      }
    };
    state.pollTimer = setInterval(tick, 900);
    tick();
  }

  async function openExistingSession(sessionId) {
    stopPolling();
    state.activeSessionId = sessionId;
    try {
      const st = await apiJson(`/api/ml/sessions/${encodeURIComponent(sessionId)}/status`);
      if (st.status === 'ready') {
        const card = $('apt-progress-card');
        if (card) card.style.display = 'none';
        await loadFullSession(sessionId);
        loadRecentSessions();
      } else if (st.status === 'error') {
        renderProgress(st);
      } else {
        renderProgress(st);
        startStatusPolling(sessionId);
      }
    } catch (err) {
      alert('Ошибка загрузки сессии: ' + err.message);
    }
  }

  async function loadFullSession(sessionId) {
    const data = await apiJson(`/api/ml/sessions/${encodeURIComponent(sessionId)}`);
    state.session = data;
    state.activeSessionId = data.session_id;
    state.selectedElementId = null;
    state.hoveredElementId = null;
    state.highlightedElementIds.clear();
    state.expandedHintKey = null;
    state.drawMode = null;
    state.currentWindowImg = null;
    state.currentWindowImgKey = null;
    revokeUrlPool(state.operatorUrls);

    const pages = data.pages || [];
    const hasProblemPages = pages.some(pageHasErrors);
    state.filterErrorsOnly = hasProblemPages;

    const firstProblem = pages.find(pageHasPendingErrors) || pages.find(pageHasErrors) || pages[0] || null;
    state.currentPageId = firstProblem ? firstProblem.page_id : null;
    if (firstProblem) {
      const probWin = (firstProblem.windows || []).find(w => (w.errors && w.errors.length) || (w.warnings && w.warnings.length));
      state.currentWindowIdx = probWin ? probWin.window_idx : 0;
    } else {
      state.currentWindowIdx = 0;
    }

    const ws = $('apt-session-workspace');
    if (ws) ws.style.display = 'block';
    renderAllSessionUI();
  }

  // ─── Рендер рабочей области оператора ───────────────────────────────
  function renderAllSessionUI() {
    if (!state.session) return;
    renderSessionSummary();
    renderNavigationControls();
    renderWindowTabs();
    renderRightPane();
    loadAndDrawCurrentWindowCanvas();
    renderBoxInspector();
  }

  function renderSessionSummary() {
    const s = state.session.summary || {};
    const grid = $('apt-kpi-grid');
    if (!grid) return;

    const cards = [
      { label: 'Всего МЛ', value: s.total_waybills ?? 0, cls: 'is-primary' },
      { label: 'Страниц', value: s.total_pages ?? 0, cls: '' },
      { label: 'С ошибками (модель)', value: s.error_pages ?? 0, cls: 'is-danger' },
      { label: 'Ожидают проверки', value: s.pending_pages ?? 0, cls: (s.pending_pages ?? 0) > 0 ? 'is-danger' : 'is-success' },
      { label: 'Подтверждено ошибок', value: s.confirmed_error_pages ?? 0, cls: '' },
      { label: 'Исправлено в норму', value: s.fixed_clean_pages ?? 0, cls: 'is-success' },
      { label: 'В дообучении', value: s.quarantined_pages ?? 0, cls: 'is-primary' },
    ];

    grid.innerHTML = cards.map(c => `
      <div class="apt-kpi ${c.cls}">
        <div class="apt-kpi-label">${escapeHtml(c.label)}</div>
        <div class="apt-kpi-value">${escapeHtml(c.value)}</div>
      </div>
    `).join('');
  }

  function renderNavigationControls() {
    const pages = state.session.pages || [];
    const problemCount = pages.filter(p => pageHasErrors(p) || hasPageChanges(p)).length;

    const btnErr = $('apt-filter-errors-btn');
    const btnAll = $('apt-filter-all-btn');
    if (btnErr && btnAll) {
      btnErr.textContent = `⚠️ Только проблемные (${problemCount})`;
      btnAll.textContent = `📄 Все страницы (${pages.length})`;
      btnErr.className = state.filterErrorsOnly ? 'btn btn-primary btn-sm' : 'btn btn-outline btn-sm';
      btnAll.className = !state.filterErrorsOnly ? 'btn btn-primary btn-sm' : 'btn btn-outline btn-sm';
    }

    const filtered = getFilteredPages();
    if (filtered.length > 0 && !filtered.some(p => p.page_id === state.currentPageId)) {
      state.currentPageId = filtered[0].page_id;
      const pw = (filtered[0].windows || []).find(w => (w.errors && w.errors.length) || (w.warnings && w.warnings.length));
      state.currentWindowIdx = pw ? pw.window_idx : 0;
    }

    const curPage = getCurrentPage();
    const sel = $('apt-waybill-select');
    if (sel) {
      const waybills = state.session.waybills || [];
      sel.innerHTML = waybills.map(wb => {
        const ml = escapeHtml(wb.ml_number || 'Без МЛ');
        const drv = escapeHtml(wb.driver_name || 'Водитель неизвестен');
        const selected = curPage && curPage.waybill_idx === wb.waybill_idx ? 'selected' : '';
        return `<option value="${wb.waybill_idx}" ${selected}>МЛ ${ml} — ${drv} (ош: ${wb.error_pages}/${wb.total_pages})</option>`;
      }).join('');
    }

    const curIdx = filtered.findIndex(p => p.page_id === state.currentPageId);
    const prevBtn = $('apt-prev-page-btn');
    const nextBtn = $('apt-next-page-btn');
    const cnt = $('apt-page-counter');
    if (prevBtn) prevBtn.disabled = curIdx <= 0;
    if (nextBtn) nextBtn.disabled = curIdx < 0 || curIdx >= filtered.length - 1;
    if (cnt) {
      cnt.textContent = curPage
        ? `Стр. ${curIdx + 1} из ${filtered.length} (PDF стр. ${curPage.global_page_num})`
        : 'Нет страниц';
    }

    const meta = $('apt-page-meta-text');
    if (meta && curPage) {
      meta.innerHTML = `МЛ: <b>${escapeHtml(curPage.ml_number || '—')}</b> &middot; Водитель: <b>${escapeHtml(curPage.driver_name || '—')}</b> &middot; Стр. МЛ №${escapeHtml(curPage.page_num)}`;
    }

    const qPill = $('apt-quarantine-status-pill');
    if (qPill && curPage) {
      if (curPage.quarantine_item_id) {
        qPill.style.display = 'inline-flex';
        qPill.textContent = `✅ В карантине (${curPage.quarantine_item_id})`;
      } else {
        qPill.style.display = 'none';
      }
    }

    const qBtn = $('apt-btn-submit-quarantine');
    if (qBtn && curPage) {
      const changed = hasPageChanges(curPage);
      qBtn.disabled = !changed || state.actionPending;
      qBtn.textContent = curPage.quarantine_item_id
        ? '🔄 Обновить исправления в дообучении'
        : '🧠 Отправить исправления в дообучение';
    }
  }

  function renderWindowTabs() {
    const wrap = $('apt-window-tabs');
    if (!wrap) return;
    const page = getCurrentPage();
    if (!page) {
      wrap.innerHTML = '';
      return;
    }
    const wins = page.windows || [];
    if (wins.length === 0) {
      wrap.innerHTML = '<div class="apt-muted">На этой странице окна аптек не найдены. Вы можете открыть «🔍 Весь лист с рамками».</div>';
      return;
    }
    if (!wins.some(w => w.window_idx === state.currentWindowIdx)) {
      state.currentWindowIdx = wins[0].window_idx;
    }

    wrap.innerHTML = wins.map(w => {
      const errCnt = (w.errors || []).length + (w.warnings || []).length;
      const isActive = w.window_idx === state.currentWindowIdx;
      const statusCls = errCnt > 0 ? 'has-errors' : 'is-clean';
      const label = errCnt > 0 ? `Окно #${w.window_number} (ош: ${errCnt})` : `Окно #${w.window_number} (норма)`;
      return `<button type="button" class="apt-win-pill ${statusCls} ${isActive ? 'active' : ''}" data-win-idx="${w.window_idx}">${escapeHtml(label)}</button>`;
    }).join('');

    wrap.querySelectorAll('.apt-win-pill').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = Number(btn.getAttribute('data-win-idx'));
        selectWindow(idx);
      });
    });
  }

  function selectPage(pageId, preferProblemWindow = true) {
    state.currentPageId = pageId;
    state.selectedElementId = null;
    state.hoveredElementId = null;
    state.highlightedElementIds.clear();
    state.expandedHintKey = null;
    state.confirmHintCommentKey = null;
    cancelDrawMode();

    const page = getCurrentPage();
    if (page && (page.windows || []).length > 0) {
      if (preferProblemWindow) {
        const pw = page.windows.find(w => (w.errors && w.errors.length) || (w.warnings && w.warnings.length));
        state.currentWindowIdx = pw ? pw.window_idx : page.windows[0].window_idx;
      } else {
        state.currentWindowIdx = page.windows[0].window_idx;
      }
    } else {
      state.currentWindowIdx = 0;
    }
    renderAllSessionUI();
  }

  function selectWindow(winIdx) {
    state.currentWindowIdx = winIdx;
    state.selectedElementId = null;
    state.hoveredElementId = null;
    state.highlightedElementIds.clear();
    state.expandedHintKey = null;
    state.confirmHintCommentKey = null;
    cancelDrawMode();
    renderWindowTabs();
    renderRightPane();
    loadAndDrawCurrentWindowCanvas();
    renderBoxInspector();
  }

  function goToNextProblemWindow() {
    if (!state.session) return;
    const pages = state.session.pages || [];
    const curPageIdx = Math.max(0, pages.findIndex(p => p.page_id === state.currentPageId));

    // Сначала ищем на текущей странице после текущего окна
    const curPage = pages[curPageIdx];
    if (curPage) {
      const nextWin = (curPage.windows || []).find(
        w => w.window_idx > state.currentWindowIdx && (w.review_status === 'pending' || w.review_status === 'fixed_with_errors')
      );
      if (nextWin) {
        selectWindow(nextWin.window_idx);
        return;
      }
    }

    // Затем ищем по следующим страницам (с переходом через конец)
    for (let offset = 1; offset <= pages.length; offset++) {
      const p = pages[(curPageIdx + offset) % pages.length];
      const pw = (p.windows || []).find(w => w.review_status === 'pending' || w.review_status === 'fixed_with_errors');
      if (pw) {
        selectPage(p.page_id, false);
        selectWindow(pw.window_idx);
        return;
      }
    }

    alert('Все проблемные окна в этой сессии уже разобраны!');
  }

  // ─── HTML5 Canvas и координаты через crop_rect ──────────────────────
  function loadAndDrawCurrentWindowCanvas() {
    const page = getCurrentPage();
    const win = getCurrentWindow();
    const canvas = $('apt-window-canvas');
    const resetBtn = $('apt-btn-reset-window');
    if (!canvas) return;

    if (!page || !win) {
      const ctx = canvas.getContext('2d');
      canvas.width = 600;
      canvas.height = 240;
      ctx.fillStyle = '#f8fafc';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      if (resetBtn) resetBtn.disabled = true;
      return;
    }

    if (resetBtn) {
      const dirty = hasWindowDiff(win) || ((win.review_meta && win.review_meta.actions_count) > 0) || win.review_status === 'confirmed_error';
      resetBtn.disabled = !dirty || state.actionPending;
    }

    const key = `${state.activeSessionId}:${page.page_id}:${win.window_idx}`;
    if (state.currentWindowImgKey === key && state.currentWindowImg) {
      drawWindowCanvas();
      return;
    }

    const url = `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/pages/${encodeURIComponent(page.page_id)}/windows/${win.window_idx}/image`;
    fetchBlobUrl(url, state.operatorUrls)
      .then(blobUrl => {
        if (`${state.activeSessionId}:${getCurrentPage()?.page_id}:${getCurrentWindow()?.window_idx}` !== key) {
          return;
        }
        const img = new Image();
        img.onload = () => {
          state.currentWindowImgKey = key;
          state.currentWindowImg = img;
          drawWindowCanvas();
        };
        img.src = blobUrl;
      })
      .catch(err => {
        console.error('Ошибка загрузки изображения окна:', err);
      });
  }

  function getCanvasMetrics(win, canvas) {
    const [cx1, cy1, cx2, cy2] = win.crop_rect || [0, 0, canvas.width, canvas.height];
    const cropW = Math.max(1, cx2 - cx1);
    const cropH = Math.max(1, cy2 - cy1);
    return {
      cx1,
      cy1,
      cx2,
      cy2,
      scaleX: canvas.width / cropW,
      scaleY: canvas.height / cropH,
    };
  }

  function drawWindowCanvas() {
    const win = getCurrentWindow();
    const canvas = $('apt-window-canvas');
    const img = state.currentWindowImg;
    if (!win || !canvas || !img) return;

    if (canvas.width !== img.naturalWidth || canvas.height !== img.naturalHeight) {
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
    }

    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0);

    const m = getCanvasMetrics(win, canvas);

    // Линия разделения зоны «Получение / Возврат», если модель её определила
    if (win.return_split_y !== null && win.return_split_y !== undefined) {
      const splitY = (win.return_split_y - m.cy1) * m.scaleY;
      if (splitY > 4 && splitY < canvas.height - 4) {
        ctx.save();
        ctx.strokeStyle = '#6366f1';
        ctx.lineWidth = 2;
        ctx.setLineDash([7, 5]);
        ctx.beginPath();
        ctx.moveTo(0, splitY);
        ctx.lineTo(canvas.width, splitY);
        ctx.stroke();
        ctx.fillStyle = '#4f46e5';
        ctx.font = 'bold 12px sans-serif';
        ctx.fillText('Граница раздела (Получение / Возврат)', 10, Math.max(16, splitY - 6));
        ctx.restore();
      }
    }

    // Контур самого окна аптеки
    if (Array.isArray(win.bbox) && win.bbox.length === 4) {
      const [wx1, wy1, wx2, wy2] = win.bbox;
      ctx.save();
      const hasErr = Boolean((win.errors && win.errors.length) || (win.warnings && win.warnings.length));
      ctx.strokeStyle = hasErr ? 'rgba(185, 28, 28, 0.7)' : 'rgba(22, 163, 74, 0.7)';
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.strokeRect((wx1 - m.cx1) * m.scaleX, (wy1 - m.cy1) * m.scaleY, (wx2 - wx1) * m.scaleX, (wy2 - wy1) * m.scaleY);
      ctx.restore();
    }

    // Элементы внутри окна
    const hasErr = Boolean((win.errors && win.errors.length) || (win.warnings && win.warnings.length));
    for (const el of (win.elements || [])) {
      const [x1, y1, x2, y2] = el.bbox;
      const drawX = (x1 - m.cx1) * m.scaleX;
      const drawY = (y1 - m.cy1) * m.scaleY;
      const drawW = (x2 - x1) * m.scaleX;
      const drawH = (y2 - y1) * m.scaleY;

      const editable = isElementEditable(el);
      const isSelected = el.id === state.selectedElementId;
      const isHovered = el.id === state.hoveredElementId;
      const isHighlighted = state.highlightedElementIds.has(el.id);

      let stroke = '#16a34a';
      let fill = 'rgba(22, 163, 74, 0.07)';
      if (!editable) {
        stroke = 'rgba(100, 116, 139, 0.7)';
        fill = 'rgba(100, 116, 139, 0.04)';
      } else if (isHighlighted || (hasErr && el.label === 'Плохая печать')) {
        stroke = '#dc2626';
        fill = 'rgba(220, 38, 38, 0.16)';
      } else if (el.source === 'operator') {
        stroke = '#0284c7';
        fill = 'rgba(2, 132, 199, 0.14)';
      } else if (hasErr && state.digitLabels.has(el.label)) {
        stroke = '#dc2626';
        fill = 'rgba(220, 38, 38, 0.12)';
      }

      ctx.save();
      if (!editable) {
        ctx.setLineDash([4, 3]);
      }
      ctx.fillStyle = fill;
      ctx.fillRect(drawX, drawY, drawW, drawH);

      ctx.strokeStyle = isSelected ? '#1d4ed8' : stroke;
      ctx.lineWidth = (isSelected || isHovered || isHighlighted) ? 3.5 : 2;
      ctx.strokeRect(drawX, drawY, drawW, drawH);

      // Подпись над рамкой
      const tag = shortLabel(el);
      ctx.font = 'bold 12px -apple-system, BlinkMacSystemFont, sans-serif';
      const tw = ctx.measureText(tag).width;
      const labelY = drawY >= 20 ? drawY - 18 : drawY + 2;
      ctx.fillStyle = isSelected ? '#1d4ed8' : (!editable ? 'rgba(71, 85, 105, 0.88)' : stroke);
      ctx.fillRect(drawX, labelY, tw + 8, 17);
      ctx.fillStyle = '#ffffff';
      ctx.fillText(tag, drawX + 4, labelY + 13);
      ctx.restore();
    }

    // Резиновая рамка в режиме рисования
    if (state.drawMode && state.dragState) {
      const rx = Math.min(state.dragState.startX, state.dragState.currX);
      const ry = Math.min(state.dragState.startY, state.dragState.currY);
      const rw = Math.abs(state.dragState.currX - state.dragState.startX);
      const rh = Math.abs(state.dragState.currY - state.dragState.startY);
      ctx.save();
      ctx.fillStyle = 'rgba(2, 132, 199, 0.18)';
      ctx.fillRect(rx, ry, rw, rh);
      ctx.strokeStyle = '#0284c7';
      ctx.lineWidth = 2.5;
      ctx.setLineDash([6, 4]);
      ctx.strokeRect(rx, ry, rw, rh);
      ctx.restore();
    }
  }

  function clientToCanvasXY(canvas, evt) {
    const rect = canvas.getBoundingClientRect();
    const x = (evt.clientX - rect.left) * (canvas.width / Math.max(1, rect.width));
    const y = (evt.clientY - rect.top) * (canvas.height / Math.max(1, rect.height));
    return {
      x: Math.max(0, Math.min(canvas.width, x)),
      y: Math.max(0, Math.min(canvas.height, y)),
    };
  }

  function findEditableElementAt(win, canvas, cx, cy) {
    if (!win) return null;
    const m = getCanvasMetrics(win, canvas);
    let best = null;
    let bestArea = Infinity;
    for (const el of (win.elements || [])) {
      if (!isElementEditable(el)) continue; // Защищённые классы запрещено выбирать и редактировать
      const [x1, y1, x2, y2] = el.bbox;
      const dx = (x1 - m.cx1) * m.scaleX;
      const dy = (y1 - m.cy1) * m.scaleY;
      const dw = (x2 - x1) * m.scaleX;
      const dh = (y2 - y1) * m.scaleY;
      if (cx >= dx && cx <= dx + dw && cy >= dy && cy <= dy + dh) {
        const area = dw * dh;
        if (area < bestArea) {
          bestArea = area;
          best = el;
        }
      }
    }
    return best;
  }

  function startDrawMode(label) {
    if (!label || state.protectedLabels.has(label)) {
      alert('Этот системный класс защищён от ручного добавления.');
      return;
    }
    state.drawMode = { label };
    state.dragState = null;
    const stage = $('apt-canvas-stage');
    if (stage) stage.classList.add('is-drawing');
    const banner = $('apt-draw-banner');
    if (banner) {
      banner.style.display = 'flex';
      $('apt-draw-label-name').textContent = label;
      const digitWrap = $('apt-draw-digit-wrap');
      const digitInp = $('apt-draw-digit-val');
      if (digitWrap && digitInp) {
        digitWrap.style.display = state.digitLabels.has(label) ? 'inline-flex' : 'none';
        digitInp.value = '';
      }
    }
    drawWindowCanvas();
  }

  function cancelDrawMode() {
    state.drawMode = null;
    state.dragState = null;
    const stage = $('apt-canvas-stage');
    if (stage) stage.classList.remove('is-drawing');
    const banner = $('apt-draw-banner');
    if (banner) banner.style.display = 'none';
    drawWindowCanvas();
  }

  function setupCanvasInteractions() {
    const canvas = $('apt-window-canvas');
    if (!canvas) return;

    canvas.addEventListener('pointerdown', evt => {
      const win = getCurrentWindow();
      if (!win || state.actionPending) return;
      const pt = clientToCanvasXY(canvas, evt);

      if (state.drawMode) {
        state.dragState = {
          startX: pt.x,
          startY: pt.y,
          currX: pt.x,
          currY: pt.y,
          pointerId: evt.pointerId,
        };
        try {
          canvas.setPointerCapture(evt.pointerId);
        } catch (_) {}
        drawWindowCanvas();
        return;
      }

      const hit = findEditableElementAt(win, canvas, pt.x, pt.y);
      state.selectedElementId = hit ? hit.id : null;
      drawWindowCanvas();
      renderBoxInspector();
    });

    canvas.addEventListener('pointermove', evt => {
      const win = getCurrentWindow();
      if (!win) return;
      const pt = clientToCanvasXY(canvas, evt);

      if (state.drawMode && state.dragState) {
        state.dragState.currX = pt.x;
        state.dragState.currY = pt.y;
        drawWindowCanvas();
        return;
      }

      if (!state.drawMode) {
        const hit = findEditableElementAt(win, canvas, pt.x, pt.y);
        const newHover = hit ? hit.id : null;
        canvas.style.cursor = hit ? 'pointer' : 'default';
        if (newHover !== state.hoveredElementId) {
          state.hoveredElementId = newHover;
          drawWindowCanvas();
        }
      }
    });

    const finishDrag = async evt => {
      if (!state.drawMode || !state.dragState) return;
      const win = getCurrentWindow();
      const page = getCurrentPage();
      const drag = state.dragState;
      const targetLabel = state.drawMode.label;
      const optionalDigitRaw = ($('apt-draw-digit-val')?.value || '').trim();

      try {
        canvas.releasePointerCapture(evt.pointerId);
      } catch (_) {}

      const pt = clientToCanvasXY(canvas, evt);
      const rx = Math.min(drag.startX, pt.x);
      const ry = Math.min(drag.startY, pt.y);
      const rw = Math.abs(pt.x - drag.startX);
      const rh = Math.abs(pt.y - drag.startY);

      cancelDrawMode();
      if (!win || !page) return;

      const m = getCanvasMetrics(win, canvas);
      const x1 = Math.max(0, Math.round(m.cx1 + rx / m.scaleX));
      const y1 = Math.max(0, Math.round(m.cy1 + ry / m.scaleY));
      const x2 = Math.min(page.width || 10000, Math.round(m.cx1 + (rx + rw) / m.scaleX));
      const y2 = Math.min(page.height || 10000, Math.round(m.cy1 + (ry + rh) / m.scaleY));

      if (x2 - x1 < 12 || y2 - y1 < 12) {
        alert('Выделенная область слишком маленькая (минимум 12×12 пикселей). Протяните рамку чуть крупнее.');
        return;
      }

      const body = {
        action: 'add_box',
        new_label: targetLabel,
        bbox: [x1, y1, x2, y2],
      };
      if (state.digitLabels.has(targetLabel) && optionalDigitRaw) {
        const parsed = parseInt(optionalDigitRaw, 10);
        if (!Number.isInteger(parsed) || parsed < 1 || parsed > 99) {
          alert('Введите целое число от 1 до 99.');
          return;
        }
        body.new_value = parsed;
      }
      await sendWindowAction(body);
    };

    canvas.addEventListener('pointerup', finishDrag);
    canvas.addEventListener('pointercancel', () => {
      if (state.drawMode) cancelDrawMode();
    });

    document.addEventListener('keydown', evt => {
      if (evt.key === 'Escape' && state.drawMode) {
        evt.preventDefault();
        cancelDrawMode();
      }
    });
  }

  // ─── Выполнение действий и мини-панель выбранной рамки ──────────────
  async function sendWindowAction(payload) {
    const page = getCurrentPage();
    const win = getCurrentWindow();
    if (!state.activeSessionId || !page || !win || state.actionPending) return;

    state.actionPending = true;
    const url = `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/pages/${encodeURIComponent(page.page_id)}/windows/${win.window_idx}/action`;
    try {
      const resp = await apiJson(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      const winPos = (page.windows || []).findIndex(w => w.window_idx === win.window_idx);
      if (winPos >= 0 && resp.window) {
        page.windows[winPos] = resp.window;
      }
      if ('page_quarantine_item_id' in resp) {
        page.quarantine_item_id = resp.page_quarantine_item_id;
      }
      if (resp.waybill) {
        const wbPos = (state.session.waybills || []).findIndex(wb => wb.waybill_idx === resp.waybill.waybill_idx);
        if (wbPos >= 0) state.session.waybills[wbPos] = resp.waybill;
      }
      if (resp.session_summary) {
        state.session.summary = resp.session_summary;
      }

      if (state.selectedElementId && !(resp.window?.elements || []).some(e => e.id === state.selectedElementId)) {
        state.selectedElementId = null;
      }
      state.highlightedElementIds.clear();
      state.confirmHintCommentKey = null;

      renderAllSessionUI();
    } catch (err) {
      alert('Ошибка действия: ' + err.message);
    } finally {
      state.actionPending = false;
      renderNavigationControls();
      loadAndDrawCurrentWindowCanvas();
    }
  }

  function renderBoxInspector() {
    const box = $('apt-box-inspector');
    if (!box) return;
    const page = getCurrentPage();
    const win = getCurrentWindow();
    const el = win && state.selectedElementId
      ? (win.elements || []).find(e => e.id === state.selectedElementId && isElementEditable(e))
      : null;

    if (!el || !page) {
      box.style.display = 'none';
      box.innerHTML = '';
      return;
    }

    const isDigit = state.digitLabels.has(el.label);
    const safeLabels = getSafeOperatorLabels().filter(l => l !== el.label);
    const quickBtns = safeLabels.slice(0, 6).map(lbl => `
      <button type="button" class="btn btn-outline btn-sm apt-relabel-btn" data-label="${escapeHtml(lbl)}">
        Это ${escapeHtml(lbl)}
      </button>
    `).join('');

    box.style.display = 'block';
    box.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:8px;">
        <div>
          <span style="font-size:12px; color:var(--text-muted);">Выбрана рамка:</span>
          <b>${escapeHtml(el.label)}</b>
          ${isDigit ? `<span style="color:var(--primary); font-weight:700;">(значение: ${escapeHtml(el.ocr_value ?? '?')})</span>` : ''}
        </div>
        <div style="display:flex; gap:6px;">
          <button type="button" class="btn btn-danger btn-sm" id="apt-insp-del-btn">🗑️ Удалить (ложная рамка)</button>
          <button type="button" class="btn btn-outline btn-sm" id="apt-insp-close-btn">✕</button>
        </div>
      </div>
      ${isDigit ? `
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:8px; padding:8px; background:#fff; border-radius:8px; border:1px solid var(--border-light);">
          <img id="apt-insp-crop-img" alt="Кроп цифры" style="width:54px; height:54px; object-fit:contain; border-radius:6px; border:1px solid var(--border-light); background:#f8fafc;">
          <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
            <span style="font-size:12.5px; font-weight:600;">Верное число (1..99):</span>
            <input type="number" min="1" max="99" class="apt-digit-input" id="apt-insp-digit-val" value="${escapeHtml(el.ocr_value ?? '')}">
            <button type="button" class="btn btn-primary btn-sm" id="apt-insp-fix-btn">💾 Сохранить число</button>
          </div>
        </div>
      ` : ''}
      <div style="font-size:12px; color:var(--text-muted); margin-bottom:4px;">Быстрая смена класса:</div>
      <div class="apt-quick-relabels">${quickBtns}</div>
    `;

    $('apt-insp-close-btn')?.addEventListener('click', () => {
      state.selectedElementId = null;
      drawWindowCanvas();
      renderBoxInspector();
    });

    $('apt-insp-del-btn')?.addEventListener('click', () => {
      sendWindowAction({ action: 'delete_box', element_id: el.id });
    });

    if (isDigit) {
      const cropUrl = `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/pages/${encodeURIComponent(page.page_id)}/elements/${encodeURIComponent(el.id)}/crop`;
      fetchBlobUrl(cropUrl, state.operatorUrls)
        .then(u => {
          const img = $('apt-insp-crop-img');
          if (img) img.src = u;
        })
        .catch(() => {});

      $('apt-insp-fix-btn')?.addEventListener('click', () => {
        const val = parseInt($('apt-insp-digit-val')?.value || '', 10);
        if (!Number.isInteger(val) || val < 1 || val > 99) {
          alert('Укажите целое число от 1 до 99.');
          return;
        }
        sendWindowAction({ action: 'fix_digit', element_id: el.id, new_value: val });
      });
    }

    box.querySelectorAll('.apt-relabel-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const target = btn.getAttribute('data-label');
        if (target) sendWindowAction({ action: 'relabel_box', element_id: el.id, new_label: target });
      });
    });
  }

  // ─── Правая панель: error_hints и 2-кнопочный UX ────────────────────
  function renderRightPane() {
    const banner = $('apt-win-status-banner');
    const warnBox = $('apt-win-warnings');
    const list = $('apt-hints-list');
    if (!banner || !warnBox || !list) return;

    const page = getCurrentPage();
    const win = getCurrentWindow();
    if (!page || !win) {
      banner.style.display = 'none';
      warnBox.innerHTML = '';
      list.innerHTML = '';
      return;
    }

    banner.style.display = 'block';
    const hints = win.error_hints || [];
    const errs = win.errors || [];
    const warns = win.warnings || [];

    if (errs.length === 0 && warns.length === 0) {
      banner.className = 'apt-win-status-banner is-clean';
      banner.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
          <div>✅ <b>Окно в норме</b> — ошибок не обнаружено!</div>
          <button type="button" class="btn btn-success btn-sm" id="apt-banner-next-prob-btn">➡️ Следующее проблемное окно</button>
        </div>
        ${hasPageChanges(page) ? '<div style="margin-top:6px; font-size:12.5px; font-weight:500;">Модель на этой странице была исправлена — не забудьте нажать <b>«Отправить исправления в дообучение»</b>.</div>' : ''}
      `;
      $('apt-banner-next-prob-btn')?.addEventListener('click', goToNextProblemWindow);
    } else if (win.review_status === 'confirmed_error') {
      banner.className = 'apt-win-status-banner is-confirmed';
      const cmt = win.review_meta?.comment ? ` (комментарий: «${escapeHtml(win.review_meta.comment)}»)` : '';
      banner.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
          <div>⚠️ <b>Ошибки подтверждены как реальные${cmt}</b> и попадут в итоговый отчёт.</div>
          <button type="button" class="btn btn-primary btn-sm" id="apt-banner-next-prob-btn">➡️ Следующее окно</button>
        </div>
      `;
      $('apt-banner-next-prob-btn')?.addEventListener('click', goToNextProblemWindow);
    } else {
      banner.className = 'apt-win-status-banner is-pending';
      banner.innerHTML = `❗ Найдено замечаний: <b>${errs.length + warns.length}</b>. Подтвердите реальную ошибку или исправьте промах модели.`;
    }

    warnBox.innerHTML = warns.length > 0
      ? warns.map(w => `<div style="background:#fffbeb; border:1px solid #fde68a; border-radius:8px; padding:8px 12px; font-size:12.5px; color:#92400e; margin-bottom:10px;">⚠️ ${escapeHtml(w)}</div>`).join('')
      : '';

    if (hints.length === 0) {
      list.innerHTML = warns.length > 0
        ? `<div class="apt-hint-card" style="border-color:#fde68a; border-left-color:#f59e0b;">
             <div class="apt-hint-title" style="color:#92400e;">Предупреждения по окну</div>
             <button type="button" class="btn btn-outline btn-sm" id="apt-warn-confirm-btn">✅ Подтвердить для отчёта</button>
           </div>`
        : '<div class="apt-muted">Вы также можете кликнуть по любой зелёной/синей рамке на изображении слева, чтобы удалить её или поменять класс.</div>';
      $('apt-warn-confirm-btn')?.addEventListener('click', () => {
        sendWindowAction({ action: 'confirm_real_error', comment: '' });
      });
      return;
    }

    list.innerHTML = hints.map((hint, idx) => {
      const hintKey = `${win.window_idx}:${idx}:${hint.code}`;
      const isExpanded = state.expandedHintKey === hintKey;
      const isConfirming = state.confirmHintCommentKey === hintKey;
      return `
        <div class="apt-hint-card" data-hint-idx="${idx}" data-hint-key="${escapeHtml(hintKey)}">
          <div class="apt-hint-title">${idx + 1}. ${escapeHtml(hint.message)}</div>
          <div class="apt-hint-primary-actions">
            <button type="button" class="btn btn-outline btn-sm apt-btn-real-err" style="border-color:#fca5a5; color:#991b1b;">
              ✅ Реальная ошибка (в отчёт)
            </button>
            <button type="button" class="btn btn-primary btn-sm apt-btn-model-fix">
              🤖 Модель ошиблась — исправить
            </button>
          </div>
          ${isConfirming ? `
            <div class="apt-hint-drawer">
              <div style="font-size:12.5px; color:var(--text-muted); margin-bottom:6px;">При необходимости добавьте комментарий в отчёт:</div>
              <div style="display:flex; gap:8px; flex-wrap:wrap;">
                <input type="text" class="form-control apt-confirm-comment-inp" placeholder="Например: нет подписи грузополучателя" style="flex:1; min-width:180px;">
                <button type="button" class="btn btn-danger btn-sm apt-confirm-submit-btn">Подтвердить ошибку</button>
                <button type="button" class="btn btn-outline btn-sm apt-confirm-cancel-btn">Отмена</button>
              </div>
            </div>
          ` : ''}
          ${isExpanded ? `<div class="apt-hint-drawer" id="apt-hint-drawer-${idx}"></div>` : ''}
        </div>
      `;
    }).join('');

    list.querySelectorAll('.apt-hint-card').forEach(card => {
      const idx = Number(card.getAttribute('data-hint-idx'));
      const hintKey = card.getAttribute('data-hint-key');
      const hint = hints[idx];

      card.addEventListener('mouseenter', () => {
        state.highlightedElementIds = new Set(hint.related_element_ids || []);
        drawWindowCanvas();
      });
      card.addEventListener('mouseleave', () => {
        state.highlightedElementIds.clear();
        drawWindowCanvas();
      });

      card.querySelector('.apt-btn-real-err')?.addEventListener('click', () => {
        state.confirmHintCommentKey = state.confirmHintCommentKey === hintKey ? null : hintKey;
        state.expandedHintKey = null;
        renderRightPane();
      });

      card.querySelector('.apt-confirm-submit-btn')?.addEventListener('click', () => {
        const cmt = card.querySelector('.apt-confirm-comment-inp')?.value?.trim() || '';
        sendWindowAction({ action: 'confirm_real_error', comment: cmt });
      });
      card.querySelector('.apt-confirm-cancel-btn')?.addEventListener('click', () => {
        state.confirmHintCommentKey = null;
        renderRightPane();
      });

      card.querySelector('.apt-btn-model-fix')?.addEventListener('click', () => {
        state.expandedHintKey = state.expandedHintKey === hintKey ? null : hintKey;
        state.confirmHintCommentKey = null;
        state.highlightedElementIds = new Set(hint.related_element_ids || []);
        renderRightPane();
        drawWindowCanvas();
      });

      if (state.expandedHintKey === hintKey) {
        const drawerEl = $(`apt-hint-drawer-${idx}`);
        if (drawerEl) populateHintDrawer(drawerEl, page, win, hint);
      }
    });
  }

  function getHintRelatedElements(win, hint, filterFn) {
    const byId = new Map((win.elements || []).map(e => [e.id, e]));
    const related = (hint.related_element_ids || []).map(id => byId.get(id)).filter(Boolean);
    if (related.length > 0) return filterFn ? related.filter(filterFn) : related;
    return filterFn ? (win.elements || []).filter(filterFn) : [];
  }

  function populateHintDrawer(drawer, page, win, hint) {
    const code = hint.code || 'other';

    // 1. Ошибки по цифрам (несовпадение / не удалось распознать)
    if (code === 'digit_mismatch' || code === 'digit_unrecognized') {
      let digitEls = getHintRelatedElements(win, hint, el => state.digitLabels.has(el.label));
      if (digitEls.length === 0) {
        digitEls = (win.elements || []).filter(el => state.digitLabels.has(el.label));
      }
      // На экран несовпадения всегда полезно показать обе цифры (печатную и рукописную), даже если hint ссылается на одну
      if (code === 'digit_mismatch') {
        const allDigits = (win.elements || []).filter(el => state.digitLabels.has(el.label));
        if (allDigits.length > digitEls.length) digitEls = allDigits;
      }

      if (digitEls.length === 0) {
        const addLbl = hint.suggested_add_label || 'Рукописная цифра';
        drawer.innerHTML = `
          <p class="apt-muted" style="margin-bottom:8px;">Рамка цифры не найдена в окне — выделите её на фото.</p>
          <button type="button" class="btn btn-primary btn-sm" id="apt-drawer-draw-btn">✏️ Обвести на фото: ${escapeHtml(addLbl)}</button>
        `;
        drawer.querySelector('#apt-drawer-draw-btn')?.addEventListener('click', () => startDrawMode(addLbl));
        return;
      }

      drawer.innerHTML = `
        <div class="apt-crops-row">
          ${digitEls.map(el => `
            <div class="apt-crop-card" data-el-id="${escapeHtml(el.id)}">
              <div style="font-size:12px; font-weight:700;">${escapeHtml(el.label)}</div>
              <div style="font-size:11.5px; color:var(--text-muted);">Модель видит: <b>${escapeHtml(el.ocr_value ?? 'не распознано')}</b></div>
              <div class="apt-crop-img-wrap">
                <img alt="${escapeHtml(el.label)}" class="apt-crop-thumb">
              </div>
              <div style="font-size:11.5px; font-weight:600;">Какое число на самом деле? (1..99)</div>
              <div class="apt-digit-input-row">
                <input type="number" min="1" max="99" class="apt-digit-input apt-fix-val-inp" value="${escapeHtml(el.ocr_value ?? '')}">
                <button type="button" class="btn btn-primary btn-sm apt-fix-save-btn">OK</button>
              </div>
              <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:8px;">
                <button type="button" class="btn btn-danger btn-sm apt-del-digit-btn" style="padding:5px 8px; font-size:11.5px;">Это не цифра — удалить</button>
                ${el.label === 'Рукописная цифра' ? `<button type="button" class="btn btn-outline btn-sm apt-relab-dec-btn" style="padding:5px 8px; font-size:11.5px;">Это Расшифровка</button>` : ''}
              </div>
            </div>
          `).join('')}
        </div>
      `;

      drawer.querySelectorAll('.apt-crop-card').forEach(card => {
        const eid = card.getAttribute('data-el-id');
        const cropUrl = `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/pages/${encodeURIComponent(page.page_id)}/elements/${encodeURIComponent(eid)}/crop`;
        fetchBlobUrl(cropUrl, state.operatorUrls)
          .then(u => {
            const img = card.querySelector('.apt-crop-thumb');
            if (img) img.src = u;
          })
          .catch(() => {});

        card.querySelector('.apt-fix-save-btn')?.addEventListener('click', () => {
          const val = parseInt(card.querySelector('.apt-fix-val-inp')?.value || '', 10);
          if (!Number.isInteger(val) || val < 1 || val > 99) {
            alert('Укажите целое число от 1 до 99.');
            return;
          }
          sendWindowAction({ action: 'fix_digit', element_id: eid, new_value: val });
        });
        card.querySelector('.apt-del-digit-btn')?.addEventListener('click', () => {
          sendWindowAction({ action: 'delete_box', element_id: eid });
        });
        card.querySelector('.apt-relab-dec-btn')?.addEventListener('click', () => {
          sendWindowAction({ action: 'relabel_box', element_id: eid, new_label: 'Расшифровка' });
        });
      });
      return;
    }

    // 2. Две рукописные цифры
    if (code === 'two_hw_digits') {
      const hwEls = getHintRelatedElements(win, hint, el => el.label === 'Рукописная цифра');
      drawer.innerHTML = `
        <p class="apt-muted" style="margin-bottom:8px;">Найдено несколько рукописных цифр. Удалите лишнюю рамку, исправьте число или переведите в «Расшифровка».</p>
        <div class="apt-crops-row">
          ${hwEls.map((el, i) => `
            <div class="apt-crop-card" data-el-id="${escapeHtml(el.id)}">
              <div style="font-size:12px; font-weight:700;">Рукописная #${i + 1} (${escapeHtml(el.ocr_value ?? '?')})</div>
              <div class="apt-crop-img-wrap"><img alt="Кроп цифры" class="apt-crop-thumb"></div>
              <div class="apt-digit-input-row">
                <input type="number" min="1" max="99" class="apt-digit-input apt-fix-val-inp" value="${escapeHtml(el.ocr_value ?? '')}">
                <button type="button" class="btn btn-primary btn-sm apt-fix-save-btn">Исправить</button>
              </div>
              <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:8px;">
                <button type="button" class="btn btn-danger btn-sm apt-del-digit-btn" style="padding:5px 8px; font-size:11.5px;">Это не цифра — удалить</button>
                <button type="button" class="btn btn-outline btn-sm apt-relab-dec-btn" style="padding:5px 8px; font-size:11.5px;">Это Расшифровка</button>
              </div>
            </div>
          `).join('')}
        </div>
        <button type="button" class="btn btn-outline btn-sm" id="apt-twohw-add-stamp">✏️ Обвести вторую печать рядом (Печать)</button>
      `;

      drawer.querySelectorAll('.apt-crop-card').forEach(card => {
        const eid = card.getAttribute('data-el-id');
        const cropUrl = `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/pages/${encodeURIComponent(page.page_id)}/elements/${encodeURIComponent(eid)}/crop`;
        fetchBlobUrl(cropUrl, state.operatorUrls)
          .then(u => {
            const img = card.querySelector('.apt-crop-thumb');
            if (img) img.src = u;
          })
          .catch(() => {});

        card.querySelector('.apt-fix-save-btn')?.addEventListener('click', () => {
          const val = parseInt(card.querySelector('.apt-fix-val-inp')?.value || '', 10);
          if (!Number.isInteger(val) || val < 1 || val > 99) {
            alert('Укажите целое число от 1 до 99.');
            return;
          }
          sendWindowAction({ action: 'fix_digit', element_id: eid, new_value: val });
        });
        card.querySelector('.apt-del-digit-btn')?.addEventListener('click', () => {
          sendWindowAction({ action: 'delete_box', element_id: eid });
        });
        card.querySelector('.apt-relab-dec-btn')?.addEventListener('click', () => {
          sendWindowAction({ action: 'relabel_box', element_id: eid, new_label: 'Расшифровка' });
        });
      });

      drawer.querySelector('#apt-twohw-add-stamp')?.addEventListener('click', () => startDrawMode('Печать'));
      return;
    }

    // 3. Плохая печать / Плохая печать на возврате
    if (code === 'bad_stamp' || code === 'bad_return_stamp') {
      const badStamps = getHintRelatedElements(win, hint, el => el.label === 'Плохая печать');
      if (badStamps.length === 0) {
        drawer.innerHTML = `<button type="button" class="btn btn-primary btn-sm" id="apt-bad-add-good">✏️ Обвести хорошую печать на фото</button>`;
        drawer.querySelector('#apt-bad-add-good')?.addEventListener('click', () => startDrawMode('Печать'));
        return;
      }
      drawer.innerHTML = badStamps.map((el, i) => `
        <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; margin-bottom:8px; padding:8px; background:#f8fafc; border-radius:8px; border:1px solid var(--border-light);" data-el-id="${escapeHtml(el.id)}">
          <span style="font-size:12.5px; font-weight:600;">Плохая печать #${i + 1}</span>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button type="button" class="btn btn-success btn-sm apt-stamp-good-btn">✅ На самом деле печать хорошая</button>
            <button type="button" class="btn btn-danger btn-sm apt-stamp-del-btn">🗑️ Это не печать — удалить</button>
          </div>
        </div>
      `).join('');
      drawer.querySelectorAll('[data-el-id]').forEach(row => {
        const eid = row.getAttribute('data-el-id');
        row.querySelector('.apt-stamp-good-btn')?.addEventListener('click', () => {
          sendWindowAction({ action: 'relabel_box', element_id: eid, new_label: 'Печать' });
        });
        row.querySelector('.apt-stamp-del-btn')?.addEventListener('click', () => {
          sendWindowAction({ action: 'delete_box', element_id: eid });
        });
      });
      return;
    }

    // 4. Исправление без второй печати
    if (code === 'correction_no_second_stamp') {
      const corrs = getHintRelatedElements(win, hint, el => el.label === 'Исправление');
      const addLabel = hint.suggested_add_label || 'Печать';
      drawer.innerHTML = `
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
          <button type="button" class="btn btn-primary btn-sm" id="apt-corr-add-stamp">✏️ Обвести на фото вторую Печать</button>
          ${corrs.map((el, i) => `
            <button type="button" class="btn btn-danger btn-sm apt-corr-del-btn" data-el-id="${escapeHtml(el.id)}">
              🗑️ Ложное «Исправление»${corrs.length > 1 ? ` #${i + 1}` : ''} — удалить
            </button>
          `).join('')}
        </div>
      `;
      drawer.querySelector('#apt-corr-add-stamp')?.addEventListener('click', () => startDrawMode(addLabel));
      drawer.querySelectorAll('.apt-corr-del-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          sendWindowAction({ action: 'delete_box', element_id: btn.getAttribute('data-el-id') });
        });
      });
      return;
    }

    // 5. Пропущенные объекты (missing_*)
    if (code.startsWith('missing_') && hint.suggested_add_label && !state.protectedLabels.has(hint.suggested_add_label)) {
      const target = hint.suggested_add_label;
      drawer.innerHTML = `
        <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
          <button type="button" class="btn btn-primary btn-sm" id="apt-missing-draw-btn">
            ✏️ Обвести на фото где ${escapeHtml(target)}
          </button>
          <span class="apt-muted">Нажмите и обведите нужную область одним прямоугольником на снимке слева.</span>
        </div>
      `;
      drawer.querySelector('#apt-missing-draw-btn')?.addEventListener('click', () => startDrawMode(target));
      return;
    }

    // 6. Универсальный fallback (только разрешённые операторские классы)
    const safeOpts = getSafeOperatorLabels();
    drawer.innerHTML = `
      <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
        <select class="apt-select" id="apt-other-label-sel" style="max-width:220px; font-size:12.5px;">
          ${safeOpts.map(l => `<option value="${escapeHtml(l)}">${escapeHtml(l)}</option>`).join('')}
        </select>
        <button type="button" class="btn btn-primary btn-sm" id="apt-other-draw-btn">✏️ Обвести объект на фото</button>
      </div>
    `;
    drawer.querySelector('#apt-other-draw-btn')?.addEventListener('click', () => {
      const lbl = drawer.querySelector('#apt-other-label-sel')?.value || safeOpts[0];
      if (lbl) startDrawMode(lbl);
    });
  }

  // ─── Отправка в карантин и скачивание отчётов ──────────────────────
  async function submitPageToQuarantine() {
    const page = getCurrentPage();
    if (!state.activeSessionId || !page || state.actionPending) return;
    if (!hasPageChanges(page)) {
      alert('На этой странице ещё нет правок разметки модели.');
      return;
    }
    const comment = prompt('Комментарий к исправлению модели (необязательно):', '') ?? null;
    if (comment === null) return;

    state.actionPending = true;
    renderNavigationControls();
    try {
      const res = await apiJson(
        `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/pages/${encodeURIComponent(page.page_id)}/submit-quarantine`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ comment }),
        }
      );
      page.quarantine_item_id = res.item_id;
      if (res.session_summary) {
        state.session.summary = res.session_summary;
      }
      renderSessionSummary();
      renderNavigationControls();
      renderRightPane();
      refreshQuarantineBadge();
      alert(`Страница отправлена в очередь дообучения (${res.item_id}). Кропов цифр: ${res.digit_crops_count ?? 0}.`);
    } catch (err) {
      alert('Ошибка отправки в карантин: ' + err.message);
    } finally {
      state.actionPending = false;
      renderNavigationControls();
    }
  }

  async function downloadSessionBlob(endpointSuffix, defaultFilename, btnEl) {
    if (!state.activeSessionId) return;
    const origText = btnEl ? btnEl.textContent : '';
    if (btnEl) {
      btnEl.disabled = true;
      btnEl.textContent = '⏳ Подготовка файла...';
    }
    try {
      const resp = await fetch(
        `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/${endpointSuffix}`,
        { headers: authHeaders(), cache: 'no-store' }
      );
      if (resp.status === 401) {
        handleUnauthorized();
        return;
      }
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error((data && (data.detail || data.error)) || `Ошибка скачивания (${resp.status})`);
      }
      const blob = await resp.blob();
      const cd = resp.headers.get('Content-Disposition') || '';
      let fname = defaultFilename;
      const mUtf = cd.match(/filename\*=UTF-8''([^;]+)/i);
      const mAsc = cd.match(/filename="([^"]+)"/i);
      if (mUtf) {
        try { fname = decodeURIComponent(mUtf[1]); } catch (_) {}
      } else if (mAsc) {
        fname = mAsc[1];
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (err) {
      alert('Ошибка скачивания: ' + err.message);
    } finally {
      if (btnEl) {
        btnEl.disabled = false;
        btnEl.textContent = origText;
      }
    }
  }

  // ─── Нативные <dialog> окна и просмотр полного листа ────────────────
  function setupDialogPolyfill() {
    if (!('closedBy' in HTMLDialogElement.prototype)) {
      const outside = (e, dlg) => {
        const r = dlg.getBoundingClientRect();
        return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      };
      for (const dlg of document.querySelectorAll('dialog[closedby="any"]')) {
        let pressedOutside = false;
        dlg.addEventListener('pointerdown', e => {
          pressedOutside = outside(e, dlg);
        });
        dlg.addEventListener('pointerup', e => {
          if (pressedOutside && outside(e, dlg)) {
            dlg.close();
          }
          pressedOutside = false;
        });
      }
    }
    for (const dlg of document.querySelectorAll('dialog.apt-dialog')) {
      dlg.addEventListener('close', () => {
        dlg.returnValue = '';
      });
    }
  }

  async function openFullPageDialog(imageUrl, titleText) {
    const dlg = $('aptFullPageDialog');
    const titleEl = $('aptFullPageDialogTitle');
    const imgEl = $('aptFullPageImg');
    if (!dlg || !imgEl) return;

    if (titleEl) titleEl.textContent = titleText || 'Полный лист с разметкой';
    if (state.fullPageUrl) {
      try { URL.revokeObjectURL(state.fullPageUrl); } catch (_) {}
      state.fullPageUrl = null;
    }
    imgEl.removeAttribute('src');
    if (typeof dlg.showModal === 'function') {
      dlg.showModal();
    }

    try {
      const objUrl = await fetchBlobUrl(imageUrl, state.operatorUrls);
      state.fullPageUrl = objUrl;
      imgEl.src = objUrl;
    } catch (err) {
      alert('Не удалось загрузить изображение страницы: ' + err.message);
      dlg.close();
    }
  }

  // ─── Админ-вкладка «Очередь дообучения» ──────────────────────────────
  async function refreshQuarantineBadge() {
    if (!window.authToken || window.currentUser?.role !== 'admin') return;
    const badge = $('nav-quarantine-badge');
    const tabCount = $('apt-q-tab-pending-count');
    try {
      const data = await apiJson('/api/ml/admin/quarantine?status=pending');
      const items = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []);
      const count = items.length;
      if (badge) {
        if (count > 0) {
          badge.style.display = 'inline-flex';
          badge.textContent = String(count);
        } else {
          badge.style.display = 'none';
        }
      }
      if (tabCount) tabCount.textContent = `(${count})`;
    } catch (_) {
      // Если ML ещё не поднят или нет сети — не шумим в фоне
    }
  }

  async function loadQuarantineList(statusOverride) {
    if (statusOverride) state.qStatusFilter = statusOverride;
    const tbody = $('apt-q-tbody');
    if (!tbody) return;

    // Обновление ARIA и табов
    const tablist = $('apt-q-tablist');
    if (tablist) {
      const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
      tabs.forEach(tab => {
        const isSel = tab.getAttribute('data-status') === state.qStatusFilter;
        tab.setAttribute('aria-selected', String(isSel));
        tab.tabIndex = isSel ? 0 : -1;
        if (isSel) {
          $('apt-q-tabpanel')?.setAttribute('aria-labelledby', tab.id);
        }
      });
    }

    state.qLoading = true;
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding:24px; color:var(--text-muted);">Загрузка очереди дообучения...</td></tr>';

    try {
      const data = await apiJson(`/api/ml/admin/quarantine?status=${encodeURIComponent(state.qStatusFilter)}`);
      state.qItems = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []);
      if (state.qStatusFilter === 'pending') {
        const tabCount = $('apt-q-tab-pending-count');
        if (tabCount) tabCount.textContent = `(${state.qItems.length})`;
        const badge = $('nav-quarantine-badge');
        if (badge) {
          badge.style.display = state.qItems.length > 0 ? 'inline-flex' : 'none';
          badge.textContent = String(state.qItems.length);
        }
      }

      if (state.qItems.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding:26px; color:var(--text-muted);">В выбранном статусе записей пока нет.</td></tr>';
        return;
      }

      const statusLabels = {
        pending: '<span class="badge-status status-running">На проверке</span>',
        approved: '<span class="badge-status status-success">Одобрено</span>',
        rejected: '<span class="badge-status status-error">Отклонено</span>',
      };

      tbody.innerHTML = state.qItems.map(item => {
        const dc = item.diff_counts || {};
        const invOk = Boolean(item.invariant_passed);
        const submitter = escapeHtml(item.submitted_by_name || item.submitted_by || '—');
        const exportedTag = item.exported_at ? `<div style="font-size:11px; color:var(--success); margin-top:2px;">Выгружено: ${escapeHtml(item.exported_at)}</div>` : '';
        return `
          <tr data-q-id="${escapeHtml(item.item_id)}">
            <th>
              <div>МЛ ${escapeHtml(item.ml_number || '—')} · Стр. ${escapeHtml(item.page_num ?? 1)}</div>
              <div style="font-size:11px; color:var(--text-muted); font-weight:500;">${escapeHtml(item.source_filename || item.item_id)}</div>
            </th>
            <td>${statusLabels[item.status] || escapeHtml(item.status)}${exportedTag}</td>
            <td>
              <div><b>${submitter}</b></div>
              <div style="font-size:11.5px; color:var(--text-muted); font-family:ui-monospace, monospace;">${escapeHtml(item.submitted_by || '')}</div>
            </td>
            <td>${escapeHtml(String(item.submitted_at || '').replace('T', ' '))}</td>
            <td>
              <span class="apt-diff-pill apt-diff-add" title="Добавлено">+${Number(dc.added || 0)}</span>
              <span class="apt-diff-pill apt-diff-del" title="Удалено">-${Number(dc.deleted || 0)}</span>
              <span class="apt-diff-pill apt-diff-rel" title="Сменён класс">~${Number(dc.relabeled || 0)}</span>
              <span class="apt-diff-pill apt-diff-dig" title="Исправлено цифр">#${Number(dc.digit_fixed || 0)}</span>
            </td>
            <td>
              <span class="badge-status ${invOk ? 'status-success' : 'status-error'}">
                ${invOk ? '✅ Пройдена' : '⚠️ Есть ошибки'}
              </span>
            </td>
            <td>${Number(item.digit_crops_count || 0)}</td>
            <td>
              <button type="button" class="btn btn-primary btn-sm apt-q-open-btn" data-q-id="${escapeHtml(item.item_id)}">
                👁️ Проверить
              </button>
            </td>
          </tr>
        `;
      }).join('');

      tbody.querySelectorAll('.apt-q-open-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          openQuarantineDetailDialog(btn.getAttribute('data-q-id'));
        });
      });
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="8" style="text-align:center; padding:24px; color:var(--error);">Ошибка: ${escapeHtml(err.message)}</td></tr>`;
    } finally {
      state.qLoading = false;
    }
  }

  function drawQuarantineOverlayCanvas(canvas, img, cropRect, elements, origMap, isAfter) {
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0);

    const [cx1, cy1, cx2, cy2] = cropRect;
    const scaleX = canvas.width / Math.max(1, cx2 - cx1);
    const scaleY = canvas.height / Math.max(1, cy2 - cy1);

    for (const el of (elements || [])) {
      const [x1, y1, x2, y2] = el.bbox;
      const dx = (x1 - cx1) * scaleX;
      const dy = (y1 - cy1) * scaleY;
      const dw = (x2 - x1) * scaleX;
      const dh = (y2 - y1) * scaleY;

      let stroke = '#16a34a';
      if (!isElementEditable(el)) {
        stroke = '#64748b';
      } else if (isAfter) {
        const prev = origMap ? origMap.get(el.id) : null;
        const isModified = !prev || prev.label !== el.label || (state.digitLabels.has(el.label) && prev.ocr_value !== el.ocr_value);
        if (isModified || el.source === 'operator') {
          stroke = '#0284c7';
        }
      }

      ctx.save();
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 2.5;
      ctx.strokeRect(dx, dy, dw, dh);
      const tag = shortLabel(el);
      ctx.font = 'bold 11px sans-serif';
      const tw = ctx.measureText(tag).width;
      const ly = dy >= 17 ? dy - 16 : dy + 2;
      ctx.fillStyle = stroke;
      ctx.fillRect(dx, ly, tw + 6, 15);
      ctx.fillStyle = '#ffffff';
      ctx.fillText(tag, dx + 3, ly + 11);
      ctx.restore();
    }
  }

  async function openQuarantineDetailDialog(itemId) {
    const dlg = $('aptQuarantineDialog');
    const titleEl = $('aptQuarantineDialogTitle');
    const metaBox = $('aptQMetaBox');
    const cmpBox = $('aptQWindowsCompare');
    const cropsBox = $('aptQCropsGallery');
    const commentInp = $('aptQAdminComment');
    if (!dlg || !itemId) return;

    revokeUrlPool(state.quarantineUrls);
    if (titleEl) titleEl.textContent = `Карточка дообучения (${itemId})`;
    if (metaBox) metaBox.innerHTML = '<div class="apt-muted">Загрузка деталей...</div>';
    if (cmpBox) cmpBox.innerHTML = '';
    if (cropsBox) cropsBox.innerHTML = '';
    if (commentInp) commentInp.value = '';

    if (typeof dlg.showModal === 'function') dlg.showModal();

    try {
      const item = await apiJson(`/api/ml/admin/quarantine/${encodeURIComponent(itemId)}`);
      state.qActiveItem = item;

      if (titleEl) {
        titleEl.textContent = `Проверка правки · МЛ ${item.ml_number || '—'} · Стр. ${item.page_num ?? 1}`;
      }
      if (commentInp) commentInp.value = item.review_comment || '';

      if (metaBox) {
        const inv = item.invariant_passed ? '✅ Пройдена (исправленные окна без ошибок)' : '⚠️ Остались ошибки в исправленных окнах';
        metaBox.innerHTML = `
          <div style="display:flex; flex-wrap:wrap; gap:14px; font-size:13px;">
            <div><span class="apt-muted">Сотрудник:</span> <b>${escapeHtml(item.submitted_by_name || item.submitted_by)}</b> (${escapeHtml(item.submitted_by)})</div>
            <div><span class="apt-muted">Водитель:</span> <b>${escapeHtml(item.driver_name || '—')}</b></div>
            <div><span class="apt-muted">Дата:</span> <b>${escapeHtml(String(item.submitted_at || '').replace('T', ' '))}</b></div>
            <div><span class="apt-muted">Логика:</span> <b>${escapeHtml(inv)}</b></div>
          </div>
          ${item.comment ? `<div style="margin-top:6px; font-size:13px;"><span class="apt-muted">Комментарий оператора:</span> ${escapeHtml(item.comment)}</div>` : ''}
        `;
      }

      const diffByWin = new Map((item.window_diffs || []).map(wd => [wd.window_idx, wd.diff]));
      const windows = (item.windows || []).filter(w => diffByWin.has(w.window_idx));
      const winsToShow = windows.length > 0 ? windows : (item.windows || []);

      if (cmpBox) {
        cmpBox.innerHTML = winsToShow.map(w => {
          const d = diffByWin.get(w.window_idx) || { added: [], deleted: [], relabeled: [], digit_fixed: [] };
          const summaryParts = [];
          if (d.added?.length) summaryParts.push(`+добавлено: ${d.added.map(a => escapeHtml(a.label)).join(', ')}`);
          if (d.deleted?.length) summaryParts.push(`-удалено: ${d.deleted.map(a => escapeHtml(a.label)).join(', ')}`);
          if (d.relabeled?.length) summaryParts.push(`~класс: ${d.relabeled.map(r => `${escapeHtml(r.from_label)} → ${escapeHtml(r.to_label)}`).join(', ')}`);
          if (d.digit_fixed?.length) summaryParts.push(`#цифры: ${d.digit_fixed.map(f => `${escapeHtml(f.from_value ?? '?')} → ${escapeHtml(f.to_value)}`).join(', ')}`);

          return `
            <div style="border:1px solid var(--border-light); border-radius:10px; padding:12px; margin-bottom:14px;">
              <div style="font-size:14px; font-weight:700; margin-bottom:4px;">Окно #${escapeHtml(w.window_number)}</div>
              <div style="font-size:12.5px; color:var(--primary); margin-bottom:8px;">${summaryParts.join(' | ') || 'Без изменений элементов'}</div>
              <div class="apt-compare-grid">
                <div class="apt-compare-col">
                  <div style="font-size:12.5px; font-weight:700; margin-bottom:6px; color:#991b1b;">Было (модель) — ошибок: ${(w.original_errors || []).length}</div>
                  <canvas id="apt-q-before-${w.window_idx}"></canvas>
                </div>
                <div class="apt-compare-col">
                  <div style="font-size:12.5px; font-weight:700; margin-bottom:6px; color:#166534;">Стало (оператор) — ошибок: ${(w.errors || []).length}</div>
                  <canvas id="apt-q-after-${w.window_idx}"></canvas>
                </div>
              </div>
            </div>
          `;
        }).join('');

        for (const w of winsToShow) {
          const imgUrl = `/api/ml/admin/quarantine/${encodeURIComponent(item.item_id)}/windows/${w.window_idx}/image`;
          fetchBlobUrl(imgUrl, state.quarantineUrls)
            .then(objUrl => {
              const img = new Image();
              img.onload = () => {
                const origMap = new Map((w.original_elements || []).map(e => [e.id, e]));
                const bCanv = $(`apt-q-before-${w.window_idx}`);
                const aCanv = $(`apt-q-after-${w.window_idx}`);
                if (bCanv) drawQuarantineOverlayCanvas(bCanv, img, w.crop_rect, w.original_elements, null, false);
                if (aCanv) drawQuarantineOverlayCanvas(aCanv, img, w.crop_rect, w.edited_elements, origMap, true);
              };
              img.src = objUrl;
            })
            .catch(() => {});
        }
      }

      if (cropsBox) {
        const crops = item.digit_crops || [];
        if (crops.length === 0) {
          cropsBox.innerHTML = '<div class="apt-muted">По этой странице нет вырезанных кропов цифр.</div>';
        } else {
          cropsBox.innerHTML = crops.map((c, idx) => `
            <div class="apt-crop-card" style="flex:0 0 175px;">
              <div style="font-size:12px; font-weight:700;">${escapeHtml(c.label)}</div>
              <div style="font-size:11.5px; color:var(--text-muted);">Было: <b>${escapeHtml(c.old_value ?? '?')}</b> → Стало: <b style="color:var(--success);">${escapeHtml(c.value)}</b></div>
              <div class="apt-crop-img-wrap"><img id="apt-q-crop-img-${idx}" alt="Кроп цифры"></div>
            </div>
          `).join('');

          crops.forEach((c, idx) => {
            const cUrl = `/api/ml/admin/quarantine/${encodeURIComponent(item.item_id)}/crops/${encodeURIComponent(c.file)}`;
            fetchBlobUrl(cUrl, state.quarantineUrls)
              .then(u => {
                const im = $(`apt-q-crop-img-${idx}`);
                if (im) im.src = u;
              })
              .catch(() => {});
          });
        }
      }
    } catch (err) {
      if (metaBox) metaBox.innerHTML = `<div style="color:var(--error);">Ошибка загрузки: ${escapeHtml(err.message)}</div>`;
    }
  }

  async function submitQuarantineDecision(decision) {
    const item = state.qActiveItem;
    if (!item) return;
    const comment = ($('aptQAdminComment')?.value || '').trim();
    const approveBtn = $('aptQApproveBtn');
    const rejectBtn = $('aptQRejectBtn');
    if (approveBtn) approveBtn.disabled = true;
    if (rejectBtn) rejectBtn.disabled = true;
    try {
      await apiJson(`/api/ml/admin/quarantine/${encodeURIComponent(item.item_id)}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, comment }),
      });
      $('aptQuarantineDialog')?.close();
      await loadQuarantineList();
      refreshQuarantineBadge();
    } catch (err) {
      alert('Ошибка сохранения решения: ' + err.message);
    } finally {
      if (approveBtn) approveBtn.disabled = false;
      if (rejectBtn) rejectBtn.disabled = false;
    }
  }

  async function exportApprovedDataset() {
    const btn = $('apt-q-export-btn');
    const resBox = $('apt-q-export-result');
    const includeExported = Boolean($('apt-q-include-exported')?.checked);
    if (btn) {
      btn.disabled = true;
      btn.textContent = '⏳ Выгрузка датасета...';
    }
    try {
      const res = await apiJson('/api/ml/admin/export-approved', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ include_exported: includeExported }),
      });
      if (resBox) {
        resBox.style.display = 'block';
        if (res.exported_pages > 0) {
          resBox.innerHTML = `
            <div style="background:#f0fdf4; border:1px solid #86efac; border-radius:10px; padding:12px 16px; color:#166534; font-size:13.5px;">
              ✅ <b>Выгрузка завершена!</b> Страниц: <b>${Number(res.exported_pages)}</b>, кропов цифр: <b>${Number(res.exported_crops || 0)}</b>.<br>
              <span style="font-size:12.5px;">Папка: <code>${escapeHtml(res.export_dir || '')}</code></span>
            </div>
          `;
        } else {
          resBox.innerHTML = `
            <div style="background:#f8fafc; border:1px solid var(--border); border-radius:10px; padding:12px 16px; font-size:13.5px;">
              ℹ️ ${escapeHtml(res.message || 'Нет новых одобренных записей для выгрузки.')}
            </div>
          `;
        }
      }
      loadQuarantineList();
    } catch (err) {
      alert('Ошибка выгрузки: ' + err.message);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '📦 Выгрузить одобренные в датасет';
      }
    }
  }

  // ─── Инициализация событий DOM ──────────────────────────────────────
  function initOnce() {
    if (state.initialized) return;
    state.initialized = true;

    setupDialogPolyfill();
    setupCanvasInteractions();

    const pdfInp = $('apt-pdf-input');
    const dropEmpty = $('apt-pdf-dropzone');
    const dropLoaded = $('apt-pdf-loaded');

    pdfInp?.addEventListener('change', e => {
      if (e.target.files?.[0]) handlePdfFileSelect(e.target.files[0]);
    });

    [dropEmpty, dropLoaded].forEach(zone => {
      if (!zone) return;
      zone.addEventListener('dragover', e => {
        e.preventDefault();
        zone.classList.add('dragover');
      });
      zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
      zone.addEventListener('drop', e => {
        e.preventDefault();
        zone.classList.remove('dragover');
        if (e.dataTransfer?.files?.[0]) handlePdfFileSelect(e.dataTransfer.files[0]);
      });
    });

    $('apt-pdf-clear')?.addEventListener('click', clearSelectedPdf);
    $('apt-start-check-btn')?.addEventListener('click', startPdfCheck);

    $('apt-toggle-history-btn')?.addEventListener('click', () => {
      const card = $('apt-history-card');
      if (!card) return;
      const show = card.style.display === 'none';
      card.style.display = show ? 'block' : 'none';
      if (show) loadRecentSessions();
    });

    $('apt-filter-errors-btn')?.addEventListener('click', () => {
      state.filterErrorsOnly = true;
      renderAllSessionUI();
    });
    $('apt-filter-all-btn')?.addEventListener('click', () => {
      state.filterErrorsOnly = false;
      renderAllSessionUI();
    });

    $('apt-waybill-select')?.addEventListener('change', e => {
      const wbIdx = Number(e.target.value);
      const wb = (state.session?.waybills || []).find(w => w.waybill_idx === wbIdx);
      if (wb && wb.page_ids?.length) {
        const pagesById = new Map((state.session.pages || []).map(p => [p.page_id, p]));
        const firstProb = wb.page_ids.map(id => pagesById.get(id)).find(p => p && pageHasErrors(p));
        selectPage((firstProb ? firstProb.page_id : wb.page_ids[0]), true);
      }
    });

    $('apt-prev-page-btn')?.addEventListener('click', () => {
      const list = getFilteredPages();
      const idx = list.findIndex(p => p.page_id === state.currentPageId);
      if (idx > 0) selectPage(list[idx - 1].page_id, true);
    });

    $('apt-next-page-btn')?.addEventListener('click', () => {
      const list = getFilteredPages();
      const idx = list.findIndex(p => p.page_id === state.currentPageId);
      if (idx >= 0 && idx < list.length - 1) selectPage(list[idx + 1].page_id, true);
    });

    $('apt-next-problem-btn')?.addEventListener('click', goToNextProblemWindow);

    $('apt-btn-full-page')?.addEventListener('click', () => {
      const page = getCurrentPage();
      if (!state.activeSessionId || !page) return;
      const url = `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/pages/${encodeURIComponent(page.page_id)}/image?annotated=1`;
      openFullPageDialog(url, `МЛ ${page.ml_number || '—'} · Стр. ${page.page_num} (полный лист)`);
    });

    $('aptFullPageCloseBtn')?.addEventListener('click', () => $('aptFullPageDialog')?.close());
    $('aptFullPageCloseFooterBtn')?.addEventListener('click', () => $('aptFullPageDialog')?.close());

    $('apt-btn-submit-quarantine')?.addEventListener('click', submitPageToQuarantine);
    $('apt-btn-reset-window')?.addEventListener('click', () => sendWindowAction({ action: 'reset_window' }));
    $('apt-draw-cancel-btn')?.addEventListener('click', cancelDrawMode);

    $('apt-btn-download-report')?.addEventListener('click', e => {
      downloadSessionBlob('report', `report_${state.activeSessionId || 'ml'}.txt`, e.currentTarget);
    });
    $('apt-btn-download-pdf')?.addEventListener('click', e => {
      downloadSessionBlob('archive-pdf', `checked_${state.activeSessionId || 'ml'}.pdf`, e.currentTarget);
    });

    // Админ: табы и диалог карантина
    const tablist = $('apt-q-tablist');
    if (tablist) {
      const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
      tabs.forEach((tab, idx) => {
        tab.addEventListener('click', () => loadQuarantineList(tab.getAttribute('data-status')));
        tab.addEventListener('keydown', e => {
          let next = null;
          if (e.key === 'ArrowRight') next = tabs[(idx + 1) % tabs.length];
          else if (e.key === 'ArrowLeft') next = tabs[(idx - 1 + tabs.length) % tabs.length];
          else if (e.key === 'Home') next = tabs[0];
          else if (e.key === 'End') next = tabs[tabs.length - 1];
          if (next) {
            e.preventDefault();
            next.focus();
            loadQuarantineList(next.getAttribute('data-status'));
          }
        });
      });
    }

    $('apt-q-refresh-btn')?.addEventListener('click', () => loadQuarantineList());
    $('apt-q-export-btn')?.addEventListener('click', exportApprovedDataset);
    $('aptQCloseBtn')?.addEventListener('click', () => $('aptQuarantineDialog')?.close());
    $('aptQCancelBtn')?.addEventListener('click', () => $('aptQuarantineDialog')?.close());
    $('aptQApproveBtn')?.addEventListener('click', () => submitQuarantineDecision('approve'));
    $('aptQRejectBtn')?.addEventListener('click', () => submitQuarantineDecision('reject'));
    $('aptQOpenFullPageBtn')?.addEventListener('click', () => {
      const item = state.qActiveItem;
      if (!item) return;
      openFullPageDialog(
        `/api/ml/admin/quarantine/${encodeURIComponent(item.item_id)}/image`,
        `Карантин ${item.item_id} · МЛ ${item.ml_number || '—'} · Стр. ${item.page_num ?? 1}`
      );
    });
  }

  // ─── Публичные хуки интеграции с index.html ─────────────────────────
  window.AptWaybills = {
    async onOpenOperator() {
      initOnce();
      await checkMlHealth(false);
      await loadRecentSessions();
    },

    async onOpenAdminQuarantine() {
      initOnce();
      await loadQuarantineList();
    },

    refreshQuarantineBadge,

    isBusy() {
      if (state.uploading || state.actionPending || state.pollTimer !== null || state.drawMode !== null) {
        return true;
      }
      if ($('aptQuarantineDialog')?.open || $('aptFullPageDialog')?.open) {
        return true;
      }
      // Не прерываем страницу автообновлением, если у оператора открыта активная проверка с несохранёнными правками
      if (state.session && (state.session.pages || []).some(p => hasPageChanges(p) && !p.quarantine_item_id)) {
        return true;
      }
      return false;
    },

    onLogout() {
      stopPolling();
      cancelDrawMode();
      state.activeSessionId = null;
      state.session = null;
      state.qActiveItem = null;
      state.currentWindowImg = null;
      state.currentWindowImgKey = null;
      revokeUrlPool(state.operatorUrls);
      revokeUrlPool(state.quarantineUrls);
      if (state.fullPageUrl) {
        try { URL.revokeObjectURL(state.fullPageUrl); } catch (_) {}
        state.fullPageUrl = null;
      }
      $('aptFullPageDialog')?.close();
      $('aptQuarantineDialog')?.close();
      const ws = $('apt-session-workspace');
      if (ws) ws.style.display = 'none';
      const pc = $('apt-progress-card');
      if (pc) pc.style.display = 'none';
    },
  };
})();
