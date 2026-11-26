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

  const STAGE_ORDER = { queued: 0, init: 1, render: 1, detect: 2, analyze: 3, done: 4 };

  const state = {
    initialized: false,
    protectedLabels: new Set(DEFAULT_PROTECTED_LABELS),
    digitLabels: new Set(DEFAULT_DIGIT_LABELS),
    operatorAddLabels: [...DEFAULT_OPERATOR_LABELS],
    editableLabels: new Set(QUICK_RELABEL_ORDER),
    selectedFiles: [],
    uploading: false,
    actionPending: false,
    pollTimer: null,
    activeSessionId: null,
    session: null,
    sessionFinalized: false,
    recentSessions: [],
    filterErrorsOnly: true,
    currentPageId: null,
    currentWindowIdx: 0,
    windowHistory: [], // стек { pageId, windowIdx } для кнопки «← Прошлое окно»
    selectedElementId: null,
    hoveredElementId: null,
    highlightedElementIds: new Set(),
    expandedHintKey: null,
    drawMode: null, // { label: string }
    dragState: null, // { startX, startY, currX, currY, pointerId }
    operatorEditor: null,
    adminEditor: null,
    reviewAreaForced: false,
    hideOperatorBoxes: false,
    qHideBeforeBoxes: false,
    qHideAfterBoxes: false,
    manualMarkupPages: new Set(), // page_id страниц с ручной правкой класса/рамки
    currentWindowImg: null,
    currentWindowImgKey: null,
    operatorUrls: new Set(),
    quarantineUrls: new Set(),
    fullPageUrl: null,
    qStatusFilter: 'pending',
    qItems: [],
    qReview: { item: null, order: [], winIdx: null, img: null, imgKey: null },
    qLoading: false,
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
    if (Array.isArray(win.original_elements)) {
      const orig = win.original_elements;
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
    // review_service.py v1.1.0 не отдаёт original_elements в JSON сессии
    return (
      win.review_status === 'fixed_clean'
      || win.review_status === 'fixed_with_errors'
      || (win.elements || []).some(e => e.source === 'operator')
    );
  }

  function hasPageChanges(page) {
    if (!page) return false;
    return (page.windows || []).some(hasWindowDiff);
  }

  function pageHasActiveIssues(page) {
    if (!page) return false;
    return (page.windows || []).some(
      w => (w.errors && w.errors.length > 0) || (w.warnings && w.warnings.length > 0)
    );
  }

  function pageHasErrors(page) {
    return pageHasActiveIssues(page);
  }

  function pageHasPendingErrors(page) {
    if (!page) return false;
    return (page.windows || []).some(w => w.review_status === 'pending' || w.review_status === 'fixed_with_errors');
  }

  function getFilteredPages() {
    if (!state.session) return [];
    const pages = state.session.pages || [];
    if (!state.filterErrorsOnly) return pages;
    return pages.filter(pageHasActiveIssues);
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

  function formatSessionDate(isoStr) {
    if (!isoStr) return 'дата не указана';
    const dt = new Date(isoStr);
    if (Number.isNaN(dt.getTime())) return String(isoStr).slice(0, 16).replace('T', ' ');
    const dd = String(dt.getDate()).padStart(2, '0');
    const mm = String(dt.getMonth() + 1).padStart(2, '0');
    const yyyy = dt.getFullYear();
    const hh = String(dt.getHours()).padStart(2, '0');
    const mi = String(dt.getMinutes()).padStart(2, '0');
    return `${dd}.${mm}.${yyyy} ${hh}:${mi}`;
  }

  // ─── Здоровье ML-сервиса и 5 недавних сессий ────────────────────────
  async function checkMlHealth() {
    const errBanner = $('apt-ml-error-banner');
    if (errBanner) {
      errBanner.style.display = 'none';
      errBanner.textContent = '';
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
      return true;
    } catch (err) {
      if (errBanner) {
        errBanner.style.display = 'block';
        errBanner.textContent = `ML-сервис временно недоступен: ${err.message || 'проверьте запуск review_service.py'}`;
      }
      return false;
    }
  }

  async function deleteRecentSession(sessionId) {
    if (!sessionId) return;
    if (!confirm('Удалить эту сессию проверки из списка и с диска?')) return;
    try {
      await apiJson(`/api/ml/sessions/${encodeURIComponent(sessionId)}/delete`, { method: 'POST' });
      if (state.activeSessionId === sessionId) {
        state.activeSessionId = null;
        state.session = null;
        state.sessionFinalized = false;
        const ws = $('apt-session-workspace');
        if (ws) ws.style.display = 'none';
      }
      await loadRecentSessions();
    } catch (err) {
      alert('Ошибка удаления сессии: ' + err.message);
    }
  }

  async function loadRecentSessions() {
    const bar = $('apt-sessions-bar');
    if (!bar) return;
    const isAdmin = window.currentUser?.role === 'admin';
    try {
      const data = await apiJson('/api/ml/sessions?limit=5');
      const rawItems = Array.isArray(data) ? data : (Array.isArray(data?.sessions) ? data.sessions : []);
      state.recentSessions = rawItems.slice(0, 5);
      if (state.recentSessions.length === 0) {
        bar.innerHTML = '<span class="apt-muted">Пока нет сохранённых проверок PDF. Загрузите первый файл выше.</span>';
        return;
      }
      bar.innerHTML = state.recentSessions.map(item => {
        const isActive = item.session_id === state.activeSessionId;
        const summary = item.summary || {};
        const errPages = summary.error_pages ?? 0;
        const dateLabel = formatSessionDate(item.created_at || item.updated_at);
        const fname = escapeHtml(item.source_filename || item.session_id);
        const meta = item.status === 'ready'
          ? `${dateLabel} · ${summary.total_waybills ?? 0} МЛ · ош: ${summary.pending_pages ?? errPages}`
          : (item.status === 'error' ? `${dateLabel} · ошибка` : `${dateLabel} · ${item.progress ?? 0}%`);
        return `
          <div class="apt-session-item-wrap">
            <button type="button" class="apt-session-chip ${isActive ? 'active' : ''}" data-session-id="${escapeHtml(item.session_id)}">
              <span>${fname}</span>
              <span style="opacity:0.8; font-weight:500;">(${escapeHtml(meta)})</span>
            </button>
            ${isAdmin ? `<button type="button" class="apt-session-del-btn" data-del-session-id="${escapeHtml(item.session_id)}" title="Удалить сессию">&times;</button>` : ''}
          </div>
        `;
      }).join('');
      bar.querySelectorAll('.apt-session-chip').forEach(btn => {
        btn.addEventListener('click', () => {
          const sid = btn.getAttribute('data-session-id');
          if (sid) openExistingSession(sid);
        });
      });
      bar.querySelectorAll('.apt-session-del-btn').forEach(btn => {
        btn.addEventListener('click', e => {
          e.stopPropagation();
          const sid = btn.getAttribute('data-del-session-id');
          if (sid) deleteRecentSession(sid);
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
    const clearBtn = $('apt-pdf-clear');
    const statusEl = $('apt-pdf-status');
    const listEl = $('apt-pdf-list');
    const sizeEl = $('apt-pdf-size');
    if (!emptyBox || !loadedBox || !startBtn) return;

    if (!state.selectedFiles || state.selectedFiles.length === 0) {
      emptyBox.hidden = false;
      loadedBox.hidden = true;
      startBtn.disabled = true;
      if (clearBtn) clearBtn.disabled = false;
      if (listEl) listEl.innerHTML = '';
      return;
    }

    emptyBox.hidden = true;
    loadedBox.hidden = false;
    const totalBytes = state.selectedFiles.reduce((acc, f) => acc + (f.size || 0), 0);
    if (statusEl) {
      statusEl.textContent = state.selectedFiles.length === 1
        ? 'PDF-файл выбран'
        : `Выбрано PDF-файлов: ${state.selectedFiles.length}`;
    }
    if (sizeEl) {
      sizeEl.textContent = state.selectedFiles.length === 1
        ? formatBytes(totalBytes)
        : `Всего: ${formatBytes(totalBytes)}`;
    }
    if (listEl) {
      listEl.innerHTML = state.selectedFiles.map((f, idx) => `
        <li class="apt-file-row">
          <span class="apt-file-row-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
          <span class="apt-file-row-meta">
            <span>${escapeHtml(formatBytes(f.size))}</span>
            <button type="button" class="apt-file-remove-btn" data-file-idx="${idx}" aria-label="Убрать файл ${escapeHtml(f.name)}" title="Убрать файл" ${state.uploading ? 'disabled' : ''}>&times;</button>
          </span>
        </li>
      `).join('');
      listEl.querySelectorAll('.apt-file-remove-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          if (state.uploading) return;
          const idx = Number(btn.getAttribute('data-file-idx'));
          if (Number.isInteger(idx) && idx >= 0 && idx < state.selectedFiles.length) {
            state.selectedFiles.splice(idx, 1);
            if (state.selectedFiles.length === 0) {
              clearSelectedPdf();
            } else {
              updateDropzoneUI();
            }
          }
        });
      });
    }
    startBtn.disabled = state.uploading;
    if (clearBtn) clearBtn.disabled = state.uploading;
  }

  function handlePdfFilesSelect(fileList) {
    const incoming = Array.from(fileList || []);
    if (incoming.length === 0) return;

    const existingKeys = new Set(
      state.selectedFiles.map(f => `${f.name}:${f.size}:${f.lastModified}`)
    );
    for (const file of incoming) {
      if (!file.name.toLowerCase().endsWith('.pdf')) {
        alert(`Файл «${file.name}» не является PDF.`);
        continue;
      }
      if (file.size > 600 * 1024 * 1024) {
        alert(`Файл «${file.name}» превышает максимальный размер 600 МБ.`);
        continue;
      }
      if (state.selectedFiles.length >= 50) {
        alert('Можно добавить не более 50 PDF-файлов за одну проверку.');
        break;
      }
      const key = `${file.name}:${file.size}:${file.lastModified}`;
      if (!existingKeys.has(key)) {
        existingKeys.add(key);
        state.selectedFiles.push(file);
      }
    }
    const inp = $('apt-pdf-input');
    if (inp) inp.value = '';
    updateDropzoneUI();
  }

  function resetWorkspaceView() {
    stopPolling();
    state.operatorEditor?.deactivate();
    cancelDrawMode();
    state.activeSessionId = null;
    state.session = null;
    state.sessionFinalized = false;
    state.reviewAreaForced = false;
    state.currentPageId = null;
    state.currentWindowIdx = 0;
    state.currentWindowImg = null;
    state.currentWindowImgKey = null;
    state.selectedElementId = null;
    state.hoveredElementId = null;
    state.highlightedElementIds.clear();
    state.expandedHintKey = null;
    state.windowHistory = [];
    state.manualMarkupPages.clear();
    state.hideOperatorBoxes = false;
    updateEyeButton('apt-op-eye-btn', false);
    revokeUrlPool(state.operatorUrls);
    const ws = $('apt-session-workspace');
    if (ws) ws.style.display = 'none';
    const pc = $('apt-progress-card');
    if (pc) pc.style.display = 'none';
    const repCard = $('apt-report-preview-card');
    if (repCard) repCard.style.display = 'none';
    const repBody = $('apt-report-preview-body');
    if (repBody) repBody.textContent = '';
    const finBanner = $('apt-finalize-banner');
    if (finBanner) {
      finBanner.style.display = 'none';
      finBanner.innerHTML = '';
    }
  }

  function clearSelectedPdf() {
    if (state.uploading) return;
    state.selectedFiles = [];
    const inp = $('apt-pdf-input');
    if (inp) inp.value = '';
    updateDropzoneUI();
    resetWorkspaceView();
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
      init: 'Инициализация и загрузка моделей...',
      render: 'Рендер страниц PDF...',
      detect: 'Детекция окон, печатей и подписей...',
      analyze: 'OCR и проверка правил окон...',
      done: 'Анализ завершён',
      error: 'Ошибка анализа',
    };
    const txt = $('apt-progress-text');
    if (txt) {
      const filesTot = Number(statusData.files_total || 0);
      const filePrefix = filesTot > 1
        ? `Файл ${statusData.current_file_idx || 1} из ${filesTot}${statusData.current_filename ? ` (${statusData.current_filename})` : ''} · `
        : '';
      const pagePart = tot > 0 ? ` (${proc} из ${tot} стр.)` : '';
      txt.textContent = `${filePrefix}${stageLabels[stage] || stage}${pagePart} — ${pct}%`;
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
    if (!state.selectedFiles.length || state.uploading) return;
    stopPolling();
    state.uploading = true;
    updateDropzoneUI();

    const errBox = $('apt-progress-error');
    if (errBox) errBox.style.display = 'none';

    const startBtn = $('apt-start-check-btn');
    if (startBtn) {
      startBtn.disabled = true;
      startBtn.textContent = 'Загрузка PDF в ML-сервис...';
    }

    try {
      let res;
      if (state.selectedFiles.length === 1) {
        const file = state.selectedFiles[0];
        const fd = new FormData();
        fd.append('file', file, file.name);
        res = await apiJson(
          `/api/ml/sessions/upload?filename=${encodeURIComponent(file.name)}`,
          { method: 'POST', body: fd }
        );
      } else {
        const batchRes = await apiJson('/api/ml/sessions/batch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ files_total: state.selectedFiles.length }),
        });
        const sid = batchRes.session_id;
        for (let i = 0; i < state.selectedFiles.length; i++) {
          const f = state.selectedFiles[i];
          if (startBtn) {
            startBtn.textContent = `Загрузка файла ${i + 1} из ${state.selectedFiles.length}...`;
          }
          const fd = new FormData();
          fd.append('file', f, f.name);
          await apiJson(
            `/api/ml/sessions/${encodeURIComponent(sid)}/files?filename=${encodeURIComponent(f.name)}`,
            { method: 'POST', body: fd }
          );
        }
        if (startBtn) startBtn.textContent = 'Запуск проверки...';
        res = await apiJson(`/api/ml/sessions/${encodeURIComponent(sid)}/start`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        if (!res.session_id) res.session_id = sid;
      }

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
        startBtn.textContent = 'Проверить маршрутные листы';
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
    state.sessionFinalized = Boolean(data.finalized);
    state.reviewAreaForced = false;
    state.operatorEditor?.deactivate();
    state.windowHistory = [];
    state.selectedElementId = null;
    state.hoveredElementId = null;
    state.highlightedElementIds.clear();
    state.expandedHintKey = null;
    state.drawMode = null;
    state.currentWindowImg = null;
    state.currentWindowImgKey = null;
    revokeUrlPool(state.operatorUrls);
    const finBanner = $('apt-finalize-banner');
    if (finBanner) {
      finBanner.style.display = 'none';
      finBanner.textContent = '';
    }

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
    if (state.sessionFinalized) {
      loadReportPreview(state.activeSessionId);
    } else {
      const repCard = $('apt-report-preview-card');
      if (repCard) repCard.style.display = 'none';
    }
  }

  // ─── Рендер рабочей области оператора ───────────────────────────────
  function updateReviewAreaVisibility() {
    const area = $('apt-review-area');
    const finBanner = $('apt-finalize-banner');
    if (!area || !state.session) return;

    const done = countPendingWindows() === 0 && !state.reviewAreaForced;
    if (done) {
      state.operatorEditor?.deactivate();
      cancelDrawMode();
      area.style.display = 'none';
      if (finBanner) {
        finBanner.style.display = 'flex';
        finBanner.style.alignItems = 'center';
        finBanner.style.justifyContent = 'space-between';
        finBanner.style.flexWrap = 'wrap';
        finBanner.style.gap = '10px';
        let msgSpan = finBanner.querySelector('.apt-fin-msg');
        if (!msgSpan) {
          const existingText = finBanner.textContent.trim();
          finBanner.innerHTML = `<span class="apt-fin-msg"></span><button type="button" class="btn btn-outline btn-sm" id="apt-open-review-btn">Открыть окна для просмотра</button>`;
          msgSpan = finBanner.querySelector('.apt-fin-msg');
          if (state.sessionFinalized && existingText) {
            msgSpan.textContent = existingText;
          }
          $('apt-open-review-btn')?.addEventListener('click', () => {
            state.reviewAreaForced = true;
            updateReviewAreaVisibility();
          });
        }
        if (!state.sessionFinalized) {
          msgSpan.textContent = 'Все спорные окна разобраны — ручная проверка завершена. Нажмите «Завершить проверку и сформировать отчёт».';
        } else if (!msgSpan.textContent.trim()) {
          msgSpan.textContent = 'Проверка завершена. Отчёт и архив готовы к скачиванию.';
        }
      }
    } else {
      area.style.display = 'block';
    }
  }

  function renderAllSessionUI() {
    if (!state.session) return;
    renderSessionSummary();
    renderNavigationControls();
    renderWindowTabs();
    renderRightPane();
    loadAndDrawCurrentWindowCanvas();
    renderBoxInspector();
    updateReviewAreaVisibility();
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
      { label: 'Принято (ошибки нет)', value: s.dismissed_clean_pages ?? 0, cls: 'is-success' },
      { label: 'Исправлено в норму', value: s.fixed_clean_pages ?? 0, cls: 'is-success' },
      { label: 'В дообучении', value: s.quarantined_pages ?? 0, cls: 'is-primary' },
    ];

    grid.innerHTML = cards.map(c => `
      <div class="apt-kpi ${c.cls}">
        <div class="apt-kpi-label">${escapeHtml(c.label)}</div>
        <div class="apt-kpi-value">${escapeHtml(c.value)}</div>
      </div>
    `).join('');

    updateFinalizeAndExportButtons();
  }

  function countPendingWindows() {
    if (!state.session) return 0;
    let cnt = 0;
    for (const page of (state.session.pages || [])) {
      for (const w of (page.windows || [])) {
        if (w.review_status === 'pending' || w.review_status === 'fixed_with_errors') {
          cnt += 1;
        }
      }
    }
    return cnt;
  }

  function updateFinalizeAndExportButtons() {
    const pendingHint = $('apt-finalize-pending-hint');
    const finBtn = $('apt-btn-finalize');
    const repBtn = $('apt-btn-download-report');
    const zipBtn = $('apt-btn-download-zip');
    if (!finBtn || !repBtn || !zipBtn) return;

    const pendingCount = countPendingWindows();
    if (pendingCount > 0) {
      if (pendingHint) {
        pendingHint.style.display = 'inline';
        pendingHint.textContent = `Для завершения разберите все замечания (осталось окон: ${pendingCount})`;
      }
      finBtn.style.display = 'none';
      repBtn.style.display = 'none';
      zipBtn.style.display = 'none';
    } else if (!state.sessionFinalized) {
      if (pendingHint) pendingHint.style.display = 'none';
      finBtn.style.display = 'inline-flex';
      repBtn.style.display = 'none';
      zipBtn.style.display = 'none';
    } else {
      if (pendingHint) pendingHint.style.display = 'none';
      finBtn.style.display = 'none';
      repBtn.style.display = 'inline-flex';
      zipBtn.style.display = 'inline-flex';
    }
  }

  function renderNavigationControls() {
    const pages = state.session.pages || [];
    const problemCount = pages.filter(pageHasActiveIssues).length;

    const btnErr = $('apt-filter-errors-btn');
    const btnAll = $('apt-filter-all-btn');
    if (btnErr && btnAll) {
      btnErr.textContent = `Только проблемные (${problemCount})`;
      btnAll.textContent = `Все страницы (${pages.length})`;
      btnErr.className = state.filterErrorsOnly ? 'btn btn-primary btn-sm' : 'btn btn-outline btn-sm';
      btnAll.className = !state.filterErrorsOnly ? 'btn btn-primary btn-sm' : 'btn btn-outline btn-sm';
    }

    const filtered = getFilteredPages();
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

    if (state.filterErrorsOnly && filtered.length === 0) {
      if (prevBtn) prevBtn.disabled = true;
      if (nextBtn) nextBtn.disabled = true;
      if (cnt) cnt.textContent = 'Проблемных страниц: 0';
    } else if (curIdx === -1 && curPage && state.filterErrorsOnly && filtered.length > 0) {
      // Текущая страница уже исправлена, но ещё открыта на экране — кнопки ищут ближайшие проблемные
      const curGlobal = Number(curPage.global_page_num || 0);
      if (prevBtn) prevBtn.disabled = !filtered.some(p => Number(p.global_page_num || 0) < curGlobal);
      if (nextBtn) nextBtn.disabled = !filtered.some(p => Number(p.global_page_num || 0) > curGlobal);
      if (cnt) cnt.textContent = `Разобрана (PDF стр. ${curPage.global_page_num}) · проблемных: ${filtered.length}`;
    } else {
      if (prevBtn) prevBtn.disabled = curIdx <= 0;
      if (nextBtn) nextBtn.disabled = curIdx < 0 || curIdx >= filtered.length - 1;
      if (cnt) {
        cnt.textContent = curPage
          ? `Стр. ${curIdx + 1} из ${filtered.length} (PDF стр. ${curPage.global_page_num})`
          : 'Нет страниц';
      }
    }

    const meta = $('apt-page-meta-text');
    if (meta && curPage) {
      meta.innerHTML = `МЛ: <b>${escapeHtml(curPage.ml_number || '—')}</b> &middot; Водитель: <b>${escapeHtml(curPage.driver_name || '—')}</b> &middot; Стр. МЛ №${escapeHtml(curPage.page_num)}`;
    }

    const qPill = $('apt-quarantine-status-pill');
    if (qPill && curPage) {
      if (curPage.quarantine_item_id) {
        qPill.style.display = 'inline-flex';
        qPill.textContent = `В очереди дообучения (${curPage.quarantine_item_id})`;
      } else {
        qPill.style.display = 'none';
      }
    }
  }

  function pushWindowHistory() {
    if (state.currentPageId === null) return;
    const prev = state.windowHistory[state.windowHistory.length - 1];
    if (!prev || prev.pageId !== state.currentPageId || prev.windowIdx !== state.currentWindowIdx) {
      state.windowHistory.push({ pageId: state.currentPageId, windowIdx: state.currentWindowIdx });
      if (state.windowHistory.length > 100) state.windowHistory.shift();
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
    const prevBtnHtml = `<button type="button" class="btn btn-outline btn-sm" id="apt-prev-window-btn">← Прошлое окно</button>`;
    if (wins.length === 0) {
      wrap.innerHTML = `${prevBtnHtml}<div class="apt-muted">На этой странице окна аптек не найдены. Вы можете открыть «Весь лист с рамками».</div>`;
      $('apt-prev-window-btn')?.addEventListener('click', goToPreviousWindow);
      return;
    }
    if (!wins.some(w => w.window_idx === state.currentWindowIdx)) {
      state.currentWindowIdx = wins[0].window_idx;
    }

    const tabsHtml = wins.map(w => {
      const errCnt = (w.errors || []).length + (w.warnings || []).length;
      const isActive = w.window_idx === state.currentWindowIdx;
      let statusCls = 'is-clean';
      let label = `Окно #${w.window_number} (норма)`;
      if (w.review_status === 'confirmed_error') {
        statusCls = 'has-errors';
        label = `Окно #${w.window_number} (ошибка подтв.)`;
      } else if (w.review_status === 'dismissed_clean') {
        statusCls = 'is-clean';
        label = `Окно #${w.window_number} (ошибки нет)`;
      } else if (errCnt > 0) {
        statusCls = 'has-errors';
        label = `Окно #${w.window_number} (ош: ${errCnt})`;
      }
      return `<button type="button" class="apt-win-pill ${statusCls} ${isActive ? 'active' : ''}" data-win-idx="${w.window_idx}">${escapeHtml(label)}</button>`;
    }).join('');

    wrap.innerHTML = prevBtnHtml + tabsHtml;
    $('apt-prev-window-btn')?.addEventListener('click', goToPreviousWindow);

    wrap.querySelectorAll('.apt-win-pill').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = Number(btn.getAttribute('data-win-idx'));
        selectWindow(idx, true);
      });
    });
  }

  function confirmLeaveImageEdit() {
    if (!state.operatorEditor || !state.operatorEditor.isActive()) return true;
    if (state.operatorEditor.hasChanges() && !confirm('На снимке есть незавершённые правки. Выйти без сохранения?')) {
      return false;
    }
    state.operatorEditor.deactivate();
    return true;
  }

  function selectPage(pageId, preferProblemWindow = true, recordHistory = true) {
    if (!confirmLeaveImageEdit()) return;
    if (recordHistory && state.currentPageId !== null && pageId !== state.currentPageId) {
      pushWindowHistory();
    }
    state.currentPageId = pageId;
    state.selectedElementId = null;
    state.hoveredElementId = null;
    state.highlightedElementIds.clear();
    state.expandedHintKey = null;
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

  function selectWindow(winIdx, recordHistory = true) {
    if (!confirmLeaveImageEdit()) return;
    if (recordHistory && winIdx !== state.currentWindowIdx) {
      pushWindowHistory();
    }
    state.currentWindowIdx = winIdx;
    state.selectedElementId = null;
    state.hoveredElementId = null;
    state.highlightedElementIds.clear();
    state.expandedHintKey = null;
    cancelDrawMode();
    renderWindowTabs();
    renderRightPane();
    loadAndDrawCurrentWindowCanvas();
    renderBoxInspector();
  }

  function goToPreviousWindow() {
    if (!state.session) return;
    if (!confirmLeaveImageEdit()) return;
    if (state.windowHistory.length > 0) {
      const prev = state.windowHistory.pop();
      if (prev && prev.pageId) {
        if (prev.pageId !== state.currentPageId) {
          state.currentPageId = prev.pageId;
        }
        state.currentWindowIdx = prev.windowIdx ?? 0;
        state.selectedElementId = null;
        state.hoveredElementId = null;
        state.highlightedElementIds.clear();
        state.expandedHintKey = null;
        cancelDrawMode();
        renderAllSessionUI();
        return;
      }
    }

    // Если стек пуст — шаг назад по порядку окон и страниц
    const pages = getFilteredPages();
    const curPageIdx = pages.findIndex(p => p.page_id === state.currentPageId);
    const curPage = pages[curPageIdx];
    if (curPage) {
      const wins = curPage.windows || [];
      const curWinPos = wins.findIndex(w => w.window_idx === state.currentWindowIdx);
      if (curWinPos > 0) {
        selectWindow(wins[curWinPos - 1].window_idx, false);
        return;
      }
    }
    if (curPageIdx > 0) {
      const prevPage = pages[curPageIdx - 1];
      const prevWins = prevPage.windows || [];
      state.currentPageId = prevPage.page_id;
      state.currentWindowIdx = prevWins.length > 0 ? prevWins[prevWins.length - 1].window_idx : 0;
      state.selectedElementId = null;
      state.expandedHintKey = null;
      cancelDrawMode();
      renderAllSessionUI();
    }
  }

  function advanceAfterDecision() {
    if (!state.session) return;
    const pages = state.session.pages || [];
    const curPageIdx = Math.max(0, pages.findIndex(p => p.page_id === state.currentPageId));

    // Сначала ищем неразобранное окно на текущей странице после текущего окна
    const curPage = pages[curPageIdx];
    if (curPage) {
      const nextWin = (curPage.windows || []).find(
        w => w.window_idx > state.currentWindowIdx && (w.review_status === 'pending' || w.review_status === 'fixed_with_errors')
      );
      if (nextWin) {
        selectWindow(nextWin.window_idx, true);
        return;
      }
    }

    // Затем по следующим страницам
    for (let offset = 1; offset <= pages.length; offset++) {
      const p = pages[(curPageIdx + offset) % pages.length];
      const pw = (p.windows || []).find(w => w.review_status === 'pending' || w.review_status === 'fixed_with_errors');
      if (pw) {
        selectPage(p.page_id, false, true);
        selectWindow(pw.window_idx, false);
        return;
      }
    }

    updateReviewAreaVisibility();
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
      const dirty = (
        hasWindowDiff(win)
        || ((win.review_meta && win.review_meta.actions_count) > 0)
        || win.review_status === 'confirmed_error'
        || win.review_status === 'dismissed_clean'
      );
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

  function updateEyeButton(btnId, hidden) {
    const btn = $(btnId);
    if (!btn) return;
    btn.classList.toggle('is-hidden', Boolean(hidden));
    btn.setAttribute('aria-pressed', String(Boolean(hidden)));
    btn.title = hidden ? 'Показать рамки классов' : 'Скрыть рамки классов';
    btn.textContent = hidden ? '👁️‍🗨️ Скрыты' : '👁️';
  }

  function getWindowErrorElementIds(win) {
    const ids = new Set();
    for (const h of (win?.error_hints || [])) {
      if (h.kind === 'missing' || h.status === 'confirmed') continue;
      for (const id of (h.related_element_ids || [])) {
        ids.add(id);
      }
    }
    return ids;
  }

  function drawWindowCanvas() {
    if (state.operatorEditor && state.operatorEditor.isActive()) {
      state.operatorEditor.draw();
      return;
    }
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

    if (state.hideOperatorBoxes) {
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
      return;
    }

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
    const errorElIds = getWindowErrorElementIds(win);
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
      } else if (isHighlighted || errorElIds.has(el.id)) {
        stroke = '#dc2626';
        fill = 'rgba(220, 38, 38, 0.16)';
      } else if (el.source === 'operator') {
        stroke = '#0284c7';
        fill = 'rgba(2, 132, 199, 0.14)';
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

  function validateBoxGeometry(win, _label, bbox) {
    return rootAptBoxEditor().validateGeometry(bbox, win?.bbox);
  }

  function rootAptBoxEditor() {
    return window.AptBoxEditor;
  }

  function findSelectableElementAt(win, canvas, cx, cy) {
    if (!win) return null;
    const m = getCanvasMetrics(win, canvas);
    let best = null;
    let bestArea = Infinity;
    for (const el of (win.elements || [])) {
      if (el.source !== 'operator' || !isElementEditable(el)) continue;
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
      if (state.operatorEditor && state.operatorEditor.isActive()) return;
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

      const hit = findSelectableElementAt(win, canvas, pt.x, pt.y);
      state.selectedElementId = hit ? hit.id : null;
      drawWindowCanvas();
      renderBoxInspector();
    });

    canvas.addEventListener('pointermove', evt => {
      if (state.operatorEditor && state.operatorEditor.isActive()) return;
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
        const hit = findSelectableElementAt(win, canvas, pt.x, pt.y);
        const newHover = hit ? hit.id : null;
        canvas.style.cursor = hit ? 'pointer' : 'default';
        if (newHover !== state.hoveredElementId) {
          state.hoveredElementId = newHover;
          drawWindowCanvas();
        }
      }
    });

    const finishDrag = async evt => {
      if (state.operatorEditor && state.operatorEditor.isActive()) return;
      if (!state.drawMode || !state.dragState) return;
      const win = getCurrentWindow();
      const page = getCurrentPage();
      const drag = state.dragState;
      const targetLabel = state.drawMode.label;

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

      const geomErr = validateBoxGeometry(win, targetLabel, [x1, y1, x2, y2]);
      if (geomErr) {
        alert(geomErr);
        return;
      }

      const body = {
        action: 'add_box',
        new_label: targetLabel,
        bbox: [x1, y1, x2, y2],
      };
      await sendWindowAction(body, { autoAdvanceIfClean: true });
    };

    canvas.addEventListener('pointerup', finishDrag);
    canvas.addEventListener('pointercancel', () => {
      if (state.operatorEditor && state.operatorEditor.isActive()) return;
      if (state.drawMode) cancelDrawMode();
    });

    document.addEventListener('keydown', evt => {
      if (state.operatorEditor && state.operatorEditor.isActive()) return;
      if (evt.key === 'Escape' && state.drawMode) {
        evt.preventDefault();
        cancelDrawMode();
      }
    });
  }

  // ─── Выполнение действий и удаление операторской рамки ──────────────
  function applyWindowActionResponse(page, win, resp) {
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
    state.sessionFinalized = false;
  }

  async function sendWindowAction(payload, options = {}) {
    if (!confirmLeaveImageEdit()) return;
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

      applyWindowActionResponse(page, win, resp);
      renderAllSessionUI();

      const updatedWin = resp.window || getCurrentWindow();
      const noMoreIssues = updatedWin && !(updatedWin.errors && updatedWin.errors.length) && !(updatedWin.warnings && updatedWin.warnings.length);
      const isResolved = updatedWin && !['pending', 'fixed_with_errors'].includes(updatedWin.review_status);
      if (options.autoAdvanceAlways || (options.autoAdvanceIfResolved && isResolved) || (options.autoAdvanceIfClean && noMoreIssues)) {
        advanceAfterDecision();
      }
    } catch (err) {
      alert('Ошибка действия: ' + err.message);
    } finally {
      state.actionPending = false;
      renderNavigationControls();
      loadAndDrawCurrentWindowCanvas();
    }
  }

  async function sendBatchDigitEdits(edits) {
    if (!confirmLeaveImageEdit()) return;
    const page = getCurrentPage();
    const win = getCurrentWindow();
    if (!state.activeSessionId || !page || !win || state.actionPending || !edits.length) return;

    state.actionPending = true;
    const url = `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/pages/${encodeURIComponent(page.page_id)}/windows/${win.window_idx}/action`;
    const postAction = body => apiJson(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    try {
      let resp;
      try {
        resp = await postAction({ action: 'batch_edit', edits });
      } catch (err) {
        const msg = String(err?.message || '');
        if (!msg.includes('Неизвестное действие') && !msg.includes('batch_edit')) {
          throw err;
        }
        for (const edit of edits) {
          resp = await postAction(edit);
        }
      }
      if (resp) applyWindowActionResponse(page, win, resp);
      renderAllSessionUI();
      const updatedWin = (resp && resp.window) || getCurrentWindow();
      const noMoreIssues = updatedWin && !(updatedWin.errors && updatedWin.errors.length) && !(updatedWin.warnings && updatedWin.warnings.length);
      if (noMoreIssues) {
        advanceAfterDecision();
      }
    } catch (err) {
      alert('Ошибка сохранения цифр: ' + err.message);
    } finally {
      state.actionPending = false;
      renderNavigationControls();
      loadAndDrawCurrentWindowCanvas();
    }
  }

  async function refreshSessionKeepPosition() {
    if (!state.activeSessionId) return;
    const data = await apiJson(`/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}`);
    state.session = data;
    renderAllSessionUI();
  }

  async function commitOperatorImageEdits(edits) {
    const page = getCurrentPage();
    const win = getCurrentWindow();
    if (!state.activeSessionId || !page || !win) {
      throw new Error('Окно проверки недоступно.');
    }
    state.actionPending = true;
    const url = `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/pages/${encodeURIComponent(page.page_id)}/windows/${win.window_idx}/action`;
    const postAction = body => apiJson(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    try {
      let resp;
      try {
        resp = await postAction({ action: 'batch_edit', edits });
      } catch (err) {
        const msg = String(err?.message || '');
        if (!msg.includes('Неизвестное действие') && !msg.includes('batch_edit')) {
          throw err;
        }
        // Fallback для ML-сервиса без пакетного batch_edit
        try {
          for (const edit of edits) {
            resp = await postAction(edit);
          }
        } catch (fbErr) {
          await refreshSessionKeepPosition().catch(() => {});
          throw fbErr;
        }
      }
      if (page.page_id) state.manualMarkupPages.add(page.page_id);
      if (resp) applyWindowActionResponse(page, win, resp);
    } finally {
      state.actionPending = false;
    }
  }

  function afterOperatorImageCommit() {
    renderAllSessionUI();
    const win = getCurrentWindow();
    const noMoreIssues = win && !(win.errors && win.errors.length) && !(win.warnings && win.warnings.length);
    if (noMoreIssues) {
      advanceAfterDecision();
    }
  }

  function updateImageEditButtons() {
    const isEd = Boolean(state.operatorEditor && state.operatorEditor.isActive());
    const editBtn = $('apt-btn-image-edit-mode');
    const resetBtn = $('apt-btn-reset-window');
    const insp = $('apt-box-inspector');
    if (editBtn) editBtn.style.display = isEd ? 'none' : '';
    if (resetBtn) resetBtn.style.display = isEd ? 'none' : '';
    if (insp && isEd) insp.style.display = 'none';
    drawWindowCanvas();
  }

  function renderBoxInspector() {
    const box = $('apt-box-inspector');
    if (!box) return;
    if (state.operatorEditor && state.operatorEditor.isActive()) {
      box.style.display = 'none';
      box.innerHTML = '';
      return;
    }
    const page = getCurrentPage();
    const win = getCurrentWindow();
    const el = win && state.selectedElementId
      ? (win.elements || []).find(e => e.id === state.selectedElementId && e.source === 'operator' && isElementEditable(e))
      : null;

    if (!el || !page) {
      box.style.display = 'none';
      box.innerHTML = '';
      return;
    }

    box.style.display = 'block';
    box.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
        <div>
          <span style="font-size:12px; color:var(--text-muted);">Добавленная вами рамка:</span>
          <b>${escapeHtml(shortLabel(el))}</b>
        </div>
        <div style="display:flex; gap:6px;">
          <button type="button" class="btn btn-danger btn-sm" id="apt-insp-del-btn">Удалить рамку</button>
          <button type="button" class="btn btn-outline btn-sm" id="apt-insp-close-btn">✕</button>
        </div>
      </div>
    `;

    $('apt-insp-close-btn')?.addEventListener('click', () => {
      state.selectedElementId = null;
      drawWindowCanvas();
      renderBoxInspector();
    });

    $('apt-insp-del-btn')?.addEventListener('click', () => {
      sendWindowAction({ action: 'delete_box', element_id: el.id });
    });
  }

  // ─── Панель замечаний под окном и 3-кнопочный UX ──────────────────
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

    const hints = win.error_hints || [];
    const errs = win.errors || [];
    const warns = win.warnings || [];

    if (win.review_status === 'dismissed_clean') {
      banner.style.display = 'block';
      banner.className = 'apt-win-status-banner is-clean';
      const origList = (win.original_errors || []);
      const origHtml = origList.length > 0
        ? `<div style="margin-top:6px; font-size:12px; font-weight:500; color:#334155;">Исходные замечания модели: ${origList.map(e => escapeHtml(e)).join('; ')}</div>`
        : '';
      banner.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
          <div>
            <b>Отмечено: ошибки нет</b> — окно попадёт в отчёт и архив как валидное (без отправки в дообучение).
            ${origHtml}
          </div>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button type="button" class="btn btn-outline btn-sm" id="apt-banner-confirm-err-btn" style="border-color:#fca5a5; color:#991b1b;">Реальная ошибка</button>
            <button type="button" class="btn btn-outline btn-sm" id="apt-banner-reset-decision-btn">Вернуть замечания</button>
          </div>
        </div>
      `;
      $('apt-banner-confirm-err-btn')?.addEventListener('click', () => sendWindowAction({ action: 'confirm_real_error', comment: '' }, { autoAdvanceAlways: true }));
      $('apt-banner-reset-decision-btn')?.addEventListener('click', () => sendWindowAction({ action: 'reset_window' }));
    } else if (win.review_status === 'confirmed_error') {
      banner.style.display = 'block';
      banner.className = 'apt-win-status-banner is-confirmed';
      banner.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
          <div><b>Реальная ошибка подтверждена</b> и зафиксирована для итогового отчёта.</div>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button type="button" class="btn btn-outline btn-sm" id="apt-banner-mark-valid-btn">Ошибки нет</button>
            <button type="button" class="btn btn-outline btn-sm" id="apt-banner-reset-decision-btn">Изменить решение</button>
          </div>
        </div>
      `;
      $('apt-banner-mark-valid-btn')?.addEventListener('click', () => sendWindowAction({ action: 'mark_valid' }, { autoAdvanceAlways: true }));
      $('apt-banner-reset-decision-btn')?.addEventListener('click', () => sendWindowAction({ action: 'reset_window' }));
    } else if (errs.length === 0 && warns.length === 0) {
      banner.style.display = 'block';
      banner.className = 'apt-win-status-banner is-clean';
      banner.innerHTML = `<div><b>Окно в норме</b> — ошибок не обнаружено.</div>`;
    } else {
      // Убрали красный отвлекающий баннер «Найдено замечаний...»
      banner.style.display = 'none';
      banner.innerHTML = '';
    }

    warnBox.innerHTML = warns.length > 0
      ? warns.map(w => `<div style="background:#fffbeb; border:1px solid #fde68a; border-radius:8px; padding:8px 12px; font-size:12.5px; color:#92400e; margin-bottom:10px;">${escapeHtml(w)}</div>`).join('')
      : '';

    if (hints.length === 0) {
      list.innerHTML = warns.length > 0
        ? `<div class="apt-hint-card" style="border-color:#fde68a; border-left-color:#f59e0b;">
             <div class="apt-hint-title" style="color:#92400e;">Предупреждения по окну</div>
             <div class="apt-hint-primary-actions" style="grid-template-columns: repeat(2, minmax(160px, 1fr));">
               <button type="button" class="btn btn-outline btn-sm" id="apt-warn-confirm-btn" style="border-color:#fca5a5; color:#991b1b;">Реальная ошибка</button>
               <button type="button" class="btn btn-outline btn-sm" id="apt-warn-valid-btn" style="border-color:#86efac; color:#166534;">Ошибки нет</button>
             </div>
           </div>`
        : '';
      $('apt-warn-confirm-btn')?.addEventListener('click', () => {
        sendWindowAction({ action: 'confirm_real_error', comment: '' }, { autoAdvanceAlways: true });
      });
      $('apt-warn-valid-btn')?.addEventListener('click', () => {
        sendWindowAction({ action: 'mark_valid' }, { autoAdvanceAlways: true });
      });
      return;
    }

    list.innerHTML = hints.map((hint, idx) => {
      const hintKey = `${win.window_idx}:${idx}:${hint.code}`;
      const isExpanded = state.expandedHintKey === hintKey;
      const isConfirmed = hint.status === 'confirmed'
        || (Array.isArray(win.confirmed_errors) && win.confirmed_errors.some(ce => ce === hint.raw_error || ce.endsWith(hint.message)));
      return `
        <div class="apt-hint-card ${isConfirmed ? 'is-confirmed' : ''}" data-hint-idx="${idx}" data-hint-key="${escapeHtml(hintKey)}">
          <div class="apt-hint-title">${idx + 1}. ${escapeHtml(hint.message)}</div>
          ${isConfirmed ? `<div style="font-size:12px; font-weight:700; color:#166534; margin-bottom:8px;">✓ Подтверждена как реальная ошибка</div>` : ''}
          <div class="apt-hint-primary-actions" ${isConfirmed ? 'style="grid-template-columns: repeat(2, minmax(160px, 1fr));"' : ''}>
            ${isConfirmed ? '' : `
              <button type="button" class="btn btn-outline btn-sm apt-btn-real-err" style="border-color:#fca5a5; color:#991b1b;">
                Реальная ошибка
              </button>
            `}
            <button type="button" class="btn btn-outline btn-sm apt-btn-no-err" style="border-color:#86efac; color:#166534;">
              Ошибки нет
            </button>
            <button type="button" class="btn btn-primary btn-sm apt-btn-model-fix">
              Модель ошиблась — исправить
            </button>
          </div>
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
        sendWindowAction(
          {
            action: 'confirm_real_error',
            comment: '',
            error_message: hint.raw_error || hint.message,
            error_index: idx,
          },
          { autoAdvanceIfResolved: true }
        );
      });

      card.querySelector('.apt-btn-no-err')?.addEventListener('click', () => {
        sendWindowAction(
          {
            action: 'mark_valid',
            error_message: hint.raw_error || hint.message,
            error_index: idx,
          },
          { autoAdvanceIfResolved: true }
        );
      });

      card.querySelector('.apt-btn-model-fix')?.addEventListener('click', () => {
        const directLabel = getDirectDrawLabelForHint(win, hint);
        if (directLabel) {
          state.expandedHintKey = hintKey;
          state.highlightedElementIds = new Set(hint.related_element_ids || []);
          renderRightPane();
          startDrawMode(directLabel);
          return;
        }
        state.expandedHintKey = state.expandedHintKey === hintKey ? null : hintKey;
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

  function getDirectDrawLabelForHint(win, hint) {
    if (!hint) return null;
    const code = hint.code || 'other';
    if (code.startsWith('missing_') && hint.suggested_add_label && !state.protectedLabels.has(hint.suggested_add_label)) {
      return hint.suggested_add_label;
    }
    if (code === 'digit_mismatch' || code === 'digit_unrecognized') {
      const digitEls = getHintRelatedElements(win, hint, el => state.digitLabels.has(el.label));
      if (digitEls.length === 0) {
        return hint.suggested_add_label || 'Рукописная цифра';
      }
    }
    if (code === 'bad_stamp' || code === 'bad_return_stamp') {
      const badStamps = getHintRelatedElements(win, hint, el => el.label === 'Плохая печать');
      if (badStamps.length === 0) return 'Печать';
    }
    if (code === 'correction_no_second_stamp') {
      const corrs = getHintRelatedElements(win, hint, el => el.label === 'Исправление');
      if (corrs.length === 0) return hint.suggested_add_label || 'Печать';
    }
    return null;
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
                <input type="number" min="1" max="99" class="apt-digit-input apt-fix-val-inp" data-orig-val="${escapeHtml(el.ocr_value ?? '')}" value="${escapeHtml(el.ocr_value ?? '')}">
              </div>
              <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:8px;">
                <button type="button" class="btn btn-danger btn-sm apt-del-digit-btn" style="padding:5px 8px; font-size:11.5px;">Это не цифра — удалить</button>
                ${el.label === 'Рукописная цифра' ? `<button type="button" class="btn btn-outline btn-sm apt-relab-sign-btn" style="padding:5px 8px; font-size:11.5px;">Это Подпись</button>` : ''}
                ${el.label === 'Рукописная цифра' ? `<button type="button" class="btn btn-outline btn-sm apt-relab-dec-btn" style="padding:5px 8px; font-size:11.5px;">Это Расшифровка</button>` : ''}
              </div>
            </div>
          `).join('')}
        </div>
        <div style="display:flex; justify-content:flex-end; margin-top:10px;">
          <button type="button" class="btn btn-primary btn-sm apt-fix-apply-all-btn">✅ Применить значения (Enter)</button>
        </div>
      `;

      const applyAllDigitFixes = () => {
        const edits = [];
        for (const card of drawer.querySelectorAll('.apt-crop-card')) {
          const eid = card.getAttribute('data-el-id');
          const inp = card.querySelector('.apt-fix-val-inp');
          if (!inp) continue;
          const origRaw = String(inp.getAttribute('data-orig-val') || '').trim();
          const curRaw = String(inp.value || '').trim();
          if (curRaw !== origRaw && curRaw !== '') {
            const val = parseInt(curRaw, 10);
            if (!Number.isInteger(val) || val < 1 || val > 99) {
              alert('Укажите целое число от 1 до 99.');
              inp.focus();
              return;
            }
            edits.push({ action: 'fix_digit', element_id: eid, new_value: val });
          }
        }
        if (edits.length === 0) {
          alert('Измените хотя бы одно значение цифры перед подтверждением.');
          return;
        }
        sendBatchDigitEdits(edits);
      };

      drawer.querySelector('.apt-fix-apply-all-btn')?.addEventListener('click', applyAllDigitFixes);

      drawer.querySelectorAll('.apt-crop-card').forEach(card => {
        const eid = card.getAttribute('data-el-id');
        const cropUrl = `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/pages/${encodeURIComponent(page.page_id)}/elements/${encodeURIComponent(eid)}/crop`;
        fetchBlobUrl(cropUrl, state.operatorUrls)
          .then(u => {
            const img = card.querySelector('.apt-crop-thumb');
            if (img) img.src = u;
          })
          .catch(() => {});

        card.querySelector('.apt-fix-val-inp')?.addEventListener('keydown', e => {
          if (e.key === 'Enter') {
            e.preventDefault();
            applyAllDigitFixes();
          }
        });
        card.querySelector('.apt-del-digit-btn')?.addEventListener('click', () => {
          sendWindowAction({ action: 'delete_box', element_id: eid }, { autoAdvanceIfClean: true });
        });
        card.querySelector('.apt-relab-sign-btn')?.addEventListener('click', () => {
          sendWindowAction({ action: 'relabel_box', element_id: eid, new_label: 'Подпись' }, { autoAdvanceIfClean: true });
        });
        card.querySelector('.apt-relab-dec-btn')?.addEventListener('click', () => {
          sendWindowAction({ action: 'relabel_box', element_id: eid, new_label: 'Расшифровка' }, { autoAdvanceIfClean: true });
        });
      });
      return;
    }

    // 2. Две рукописные цифры
    if (code === 'two_hw_digits') {
      const hwEls = getHintRelatedElements(win, hint, el => el.label === 'Рукописная цифра');
      drawer.innerHTML = `
        <p class="apt-muted" style="margin-bottom:8px;">Найдено несколько рукописных цифр. Удалите лишнюю рамку, исправьте число или переведите в «Подпись» / «Расшифровка».</p>
        <div class="apt-crops-row">
          ${hwEls.map((el, i) => `
            <div class="apt-crop-card" data-el-id="${escapeHtml(el.id)}">
              <div style="font-size:12px; font-weight:700;">Рукописная #${i + 1} (${escapeHtml(el.ocr_value ?? '?')})</div>
              <div class="apt-crop-img-wrap"><img alt="Кроп цифры" class="apt-crop-thumb"></div>
              <div class="apt-digit-input-row">
                <input type="number" min="1" max="99" class="apt-digit-input apt-fix-val-inp" data-orig-val="${escapeHtml(el.ocr_value ?? '')}" value="${escapeHtml(el.ocr_value ?? '')}">
              </div>
              <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:8px;">
                <button type="button" class="btn btn-danger btn-sm apt-del-digit-btn" style="padding:5px 8px; font-size:11.5px;">Это не цифра — удалить</button>
                <button type="button" class="btn btn-outline btn-sm apt-relab-sign-btn" style="padding:5px 8px; font-size:11.5px;">Это Подпись</button>
                <button type="button" class="btn btn-outline btn-sm apt-relab-dec-btn" style="padding:5px 8px; font-size:11.5px;">Это Расшифровка</button>
              </div>
            </div>
          `).join('')}
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center; justify-content:space-between; margin-top:10px;">
          <button type="button" class="btn btn-outline btn-sm" id="apt-twohw-add-stamp">Обвести вторую печать рядом (Печать)</button>
          <button type="button" class="btn btn-primary btn-sm apt-fix-apply-all-btn">✅ Применить значения (Enter)</button>
        </div>
      `;

      const applyAllTwoHwFixes = () => {
        const edits = [];
        for (const card of drawer.querySelectorAll('.apt-crop-card')) {
          const eid = card.getAttribute('data-el-id');
          const inp = card.querySelector('.apt-fix-val-inp');
          if (!inp) continue;
          const origRaw = String(inp.getAttribute('data-orig-val') || '').trim();
          const curRaw = String(inp.value || '').trim();
          if (curRaw !== origRaw && curRaw !== '') {
            const val = parseInt(curRaw, 10);
            if (!Number.isInteger(val) || val < 1 || val > 99) {
              alert('Укажите целое число от 1 до 99.');
              inp.focus();
              return;
            }
            edits.push({ action: 'fix_digit', element_id: eid, new_value: val });
          }
        }
        if (edits.length === 0) {
          alert('Измените хотя бы одно значение цифры перед подтверждением.');
          return;
        }
        sendBatchDigitEdits(edits);
      };

      drawer.querySelector('.apt-fix-apply-all-btn')?.addEventListener('click', applyAllTwoHwFixes);

      drawer.querySelectorAll('.apt-crop-card').forEach(card => {
        const eid = card.getAttribute('data-el-id');
        const cropUrl = `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/pages/${encodeURIComponent(page.page_id)}/elements/${encodeURIComponent(eid)}/crop`;
        fetchBlobUrl(cropUrl, state.operatorUrls)
          .then(u => {
            const img = card.querySelector('.apt-crop-thumb');
            if (img) img.src = u;
          })
          .catch(() => {});

        card.querySelector('.apt-fix-val-inp')?.addEventListener('keydown', e => {
          if (e.key === 'Enter') {
            e.preventDefault();
            applyAllTwoHwFixes();
          }
        });
        card.querySelector('.apt-del-digit-btn')?.addEventListener('click', () => {
          sendWindowAction({ action: 'delete_box', element_id: eid }, { autoAdvanceIfClean: true });
        });
        card.querySelector('.apt-relab-sign-btn')?.addEventListener('click', () => {
          sendWindowAction({ action: 'relabel_box', element_id: eid, new_label: 'Подпись' }, { autoAdvanceIfClean: true });
        });
        card.querySelector('.apt-relab-dec-btn')?.addEventListener('click', () => {
          sendWindowAction({ action: 'relabel_box', element_id: eid, new_label: 'Расшифровка' }, { autoAdvanceIfClean: true });
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
            Обвести на фото: ${escapeHtml(target)}
          </button>
          <span class="apt-muted">Режим выделения включён — обведите область одним прямоугольником на снимке ниже.</span>
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

  // ─── Финализация проверки и скачивание отчёта / архива ZIP ─────────
  async function finalizeSession() {
    if (!state.activeSessionId || state.actionPending) return;
    const btn = $('apt-btn-finalize');
    const finBanner = $('apt-finalize-banner');
    state.actionPending = true;
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Формирование отчёта и сохранение...';
    }
    try {
      const manualIds = new Set(state.manualMarkupPages);
      for (const p of (state.session?.pages || [])) {
        if (!p?.page_id) continue;
        const hasManualConfirmed = (p.windows || []).some(
          w => w.review_status === 'confirmed_error' && (hasWindowDiff(w) || (w.elements || []).some(e => e.source === 'operator'))
        );
        if (hasManualConfirmed) manualIds.add(p.page_id);
      }

      const res = await apiJson(
        `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/finalize`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            manual_markup_page_ids: Array.from(manualIds),
          }),
        }
      );
      if (res.session_summary && state.session) {
        state.session.summary = res.session_summary;
      }
      await loadFullSession(state.activeSessionId);
      state.sessionFinalized = true;
      updateFinalizeAndExportButtons();
      refreshQuarantineBadge();
      loadReportPreview(state.activeSessionId);
      if (finBanner) {
        const subPages = Number(res.submitted_pages || res.auto_quarantined_pages || res.quarantined_pages || 0);
        const validWb = res.valid_waybills;
        const errWb = res.error_waybills;
        const wbPart = (validWb !== undefined && errWb !== undefined)
          ? ` Валидных МЛ: ${validWb}, на доработку: ${errWb}.`
          : '';
        finBanner.style.display = 'block';
        finBanner.textContent = subPages > 0
          ? `Проверка завершена.${wbPart} Отчёт (.txt) и архив МЛ (.zip) готовы к скачиванию; в очередь дообучения передано страниц: ${subPages}.`
          : `Проверка завершена.${wbPart} Итоговый отчёт (.txt) и архив МЛ (.zip) готовы к скачиванию.`;
      }
    } catch (err) {
      alert('Ошибка завершения проверки: ' + err.message);
    } finally {
      state.actionPending = false;
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Завершить проверку и сформировать отчёт';
      }
    }
  }

  async function loadReportPreview(sessionId) {
    const card = $('apt-report-preview-card');
    const body = $('apt-report-preview-body');
    if (!card || !body || !sessionId) return;
    try {
      const resp = await fetch(
        `/api/ml/sessions/${encodeURIComponent(sessionId)}/report`,
        { headers: authHeaders(), cache: 'no-store' }
      );
      if (!resp.ok) return;
      const text = await resp.text();
      body.textContent = text;
      card.style.display = 'block';
    } catch (_) {
      // не мешаем основному UI при сетевой ошибке
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
    const listView = $('apt-q-list-view');
    const reviewView = $('apt-q-review-view');
    if (reviewView) reviewView.hidden = true;
    if (listView) listView.hidden = false;
    state.adminEditor?.deactivate();

    const tbody = $('apt-q-tbody');
    const startBtn = $('apt-q-start-review-btn');
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
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:24px; color:var(--text-muted);">Загрузка очереди дообучения...</td></tr>';

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
      if (startBtn) {
        const showStart = state.qStatusFilter === 'pending' && state.qItems.length > 0;
        startBtn.hidden = !showStart;
        if (showStart) {
          startBtn.textContent = `▶ Начать проверку (${state.qItems.length})`;
        }
      }

      if (state.qItems.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:26px; color:var(--text-muted);">В выбранном статусе записей пока нет.</td></tr>';
        return;
      }

      const statusLabels = {
        pending: '<span class="badge-status status-running">На проверке</span>',
        approved: '<span class="badge-status status-success">Одобрено</span>',
        rejected: '<span class="badge-status status-error">Отклонено</span>',
      };

      tbody.innerHTML = state.qItems.map(item => {
        const invOk = Boolean(item.invariant_passed);
        const isManualMarkup = String(item.comment || '').includes('[РУЧНАЯ ПРАВКА]') || !invOk;
        const submitter = escapeHtml(item.submitted_by_name || item.submitted_by || '—');
        const exportedTag = item.exported_at ? `<div style="font-size:11px; color:var(--success); margin-top:2px;">Выгружено: ${escapeHtml(item.exported_at)}</div>` : '';
        const manualBadge = isManualMarkup
          ? `<div class="apt-manual-badge">✏️ Ручная правка класса/рамки</div>`
          : '';
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
            <td>${escapeHtml(formatSessionDate(item.submitted_at))}</td>
            <td>
              ${manualBadge}
              <div>
                <span class="badge-status ${invOk ? 'status-success' : 'status-error'}">
                  ${invOk ? '✅ Окно в норме' : '⚠️ Зафиксирована ошибка окна (ручная разметка)'}
                </span>
              </div>
            </td>
            <td>${Number(item.digit_crops_count || 0)}</td>
            <td>
              <button type="button" class="btn btn-primary btn-sm apt-q-open-btn" data-q-id="${escapeHtml(item.item_id)}">
                ${item.status === 'pending' ? '👁️ Проверить' : 'Открыть'}
              </button>
            </td>
          </tr>
        `;
      }).join('');

      tbody.querySelectorAll('.apt-q-open-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          const id = btn.getAttribute('data-q-id');
          const order = state.qItems.map(i => i.item_id);
          const doneBox = $('apt-q-review-done');
          if (doneBox) doneBox.style.display = 'none';
          openQuarantineReview(id, order);
        });
      });
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding:24px; color:var(--error);">Ошибка: ${escapeHtml(err.message)}</td></tr>`;
    } finally {
      state.qLoading = false;
    }
  }

  function drawQuarantineOverlayCanvas(canvas, img, cropRect, elements, origMap, isAfter, returnSplitY = null, hideBoxes = false) {
    if (!canvas || !img) return;
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0);

    if (hideBoxes) return;

    const [cx1, cy1, cx2, cy2] = cropRect;
    const scaleX = canvas.width / Math.max(1, cx2 - cx1);
    const scaleY = canvas.height / Math.max(1, cy2 - cy1);

    if (returnSplitY !== null && returnSplitY !== undefined) {
      const splitY = (returnSplitY - cy1) * scaleY;
      if (splitY > 4 && splitY < canvas.height - 4) {
        ctx.save();
        ctx.strokeStyle = '#6366f1';
        ctx.lineWidth = 2;
        ctx.setLineDash([7, 5]);
        ctx.beginPath();
        ctx.moveTo(0, splitY);
        ctx.lineTo(canvas.width, splitY);
        ctx.stroke();
        ctx.restore();
      }
    }

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
        if (isModified || el.source === 'operator' || el.source === 'admin') {
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

  function confirmLeaveAdminEdit() {
    if (!state.adminEditor || !state.adminEditor.isActive()) return true;
    if (state.adminEditor.hasChanges() && !confirm('На снимке есть незавершённые правки. Выйти без сохранения?')) {
      return false;
    }
    state.adminEditor.deactivate();
    return true;
  }

  function getQuarantineReviewWindows(item) {
    if (!item) return [];
    const diffByWin = new Map((item.window_diffs || []).map(wd => [wd.window_idx, wd.diff]));
    const filtered = (item.windows || []).filter(w => diffByWin.has(w.window_idx));
    return filtered.length > 0 ? filtered : (item.windows || []);
  }

  function getCurrentQuarantineWindow() {
    const item = state.qReview.item;
    if (!item) return null;
    const wins = getQuarantineReviewWindows(item);
    return wins.find(w => w.window_idx === state.qReview.winIdx) || wins[0] || null;
  }

  function closeQuarantineReview(force = false) {
    if (!force && !confirmLeaveAdminEdit()) return;
    if (force) state.adminEditor?.deactivate();
    const listView = $('apt-q-list-view');
    const reviewView = $('apt-q-review-view');
    if (reviewView) reviewView.hidden = true;
    if (listView) listView.hidden = false;
    state.qReview = { item: null, order: [], winIdx: null, img: null, imgKey: null };
    revokeUrlPool(state.quarantineUrls);
    loadQuarantineList();
  }

  async function openQuarantineReview(itemId, order = null) {
    const listView = $('apt-q-list-view');
    const reviewView = $('apt-q-review-view');
    if (!reviewView || !itemId) return;
    if (!confirmLeaveAdminEdit()) return;

    if (Array.isArray(order) && order.length > 0) {
      state.qReview.order = [...order];
    }
    state.qReview.item = null;
    state.qReview.winIdx = null;
    state.qReview.img = null;
    state.qReview.imgKey = null;
    revokeUrlPool(state.quarantineUrls);

    if (listView) listView.hidden = true;
    reviewView.hidden = false;
    const metaBox = $('apt-q-review-meta');
    if (metaBox) metaBox.innerHTML = '<span class="apt-muted">Загрузка карточки...</span>';

    try {
      const item = await apiJson(`/api/ml/admin/quarantine/${encodeURIComponent(itemId)}`);
      state.qReview.item = item;
      const wins = getQuarantineReviewWindows(item);
      state.qReview.winIdx = wins.length > 0 ? wins[0].window_idx : null;
      renderQuarantineReview();
      loadAndDrawQuarantineWindowImage();
    } catch (err) {
      if (metaBox) metaBox.innerHTML = `<span style="color:var(--error);">Ошибка загрузки: ${escapeHtml(err.message)}</span>`;
    }
  }

  function loadAndDrawQuarantineWindowImage() {
    const item = state.qReview.item;
    const w = getCurrentQuarantineWindow();
    if (!item || !w) return;

    const key = `${item.item_id}:${w.window_idx}`;
    if (state.qReview.imgKey === key && state.qReview.img) {
      drawQuarantineReviewCanvases();
      return;
    }

    const imgUrl = `/api/ml/admin/quarantine/${encodeURIComponent(item.item_id)}/windows/${w.window_idx}/image`;
    fetchBlobUrl(imgUrl, state.quarantineUrls)
      .then(objUrl => {
        if (!state.qReview.item || `${state.qReview.item.item_id}:${getCurrentQuarantineWindow()?.window_idx}` !== key) {
          return;
        }
        const img = new Image();
        img.onload = () => {
          state.qReview.imgKey = key;
          state.qReview.img = img;
          drawQuarantineReviewCanvases();
        };
        img.src = objUrl;
      })
      .catch(() => {});
  }

  function drawQuarantineReviewCanvases() {
    const w = getCurrentQuarantineWindow();
    const img = state.qReview.img;
    if (!w || !img) return;
    const bCanv = $('apt-q-before-canvas');
    const aCanv = $('apt-q-after-canvas');
    const origMap = new Map((w.original_elements || []).map(e => [e.id, e]));
    if (bCanv) {
      drawQuarantineOverlayCanvas(bCanv, img, w.crop_rect, w.original_elements, null, false, w.return_split_y ?? null, state.qHideBeforeBoxes);
    }
    if (aCanv) {
      if (state.adminEditor && state.adminEditor.isActive()) {
        state.adminEditor.draw();
      } else {
        drawQuarantineOverlayCanvas(aCanv, img, w.crop_rect, w.edited_elements, origMap, true, w.return_split_y ?? null, state.qHideAfterBoxes);
      }
    }
  }

  function renderQuarantineReview() {
    const item = state.qReview.item;
    if (!item) return;

    const metaBox = $('apt-q-review-meta');
    const tabsBox = $('apt-q-window-tabs');
    const errBox = $('apt-q-after-errors');
    const beforeTitle = $('apt-q-before-title');
    const afterTitle = $('apt-q-after-title');
    const cropsSection = $('apt-q-crops-section');
    const cropsBox = $('apt-q-crops-row');
    const editBtn = $('apt-q-edit-btn');
    const decisionBar = $('apt-q-decision-bar');

    const isManualMarkup = String(item.comment || '').includes('[РУЧНАЯ ПРАВКА]') || !item.invariant_passed;
    const pendingTotal = (state.qItems || []).filter(i => i.status === 'pending').length;

    if (metaBox) {
      metaBox.innerHTML = `
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <b>МЛ ${escapeHtml(item.ml_number || '—')} · Стр. ${escapeHtml(item.page_num ?? 1)}</b>
          <span>Водитель: <b>${escapeHtml(item.driver_name || '—')}</b></span>
          <span>Сотрудник: <b>${escapeHtml(item.submitted_by_name || item.submitted_by || '—')}</b> <span style="font-family:ui-monospace, monospace; font-size:11.5px; color:var(--text-muted);">(${escapeHtml(item.submitted_by || '')})</span></span>
          <span class="apt-muted">${escapeHtml(formatSessionDate(item.submitted_at))}</span>
          ${isManualMarkup ? `<span class="apt-manual-badge" style="margin-bottom:0;">✏️ Ручная правка класса/рамки</span>` : ''}
          ${item.admin_edited ? `<span class="apt-manual-badge" style="margin-bottom:0; background:#dbeafe; border-color:#93c5fd; color:#1e40af;">🛠 Исправлено администратором</span>` : ''}
          ${item.status === 'pending' ? `<span class="apt-muted">Осталось на проверке: <b>${pendingTotal}</b></span>` : ''}
        </div>
      `;
    }

    const wins = getQuarantineReviewWindows(item);
    const curWin = getCurrentQuarantineWindow();
    if (curWin) state.qReview.winIdx = curWin.window_idx;

    if (tabsBox) {
      tabsBox.innerHTML = wins.map(w => {
        const errCnt = (w.errors || []).length;
        const isActive = curWin && w.window_idx === curWin.window_idx;
        const cls = errCnt > 0 ? 'has-errors' : 'is-clean';
        const lbl = errCnt > 0 ? `Окно #${w.window_number} (ош: ${errCnt})` : `Окно #${w.window_number} (норма)`;
        return `<button type="button" class="apt-win-pill ${cls} ${isActive ? 'active' : ''}" data-q-win="${w.window_idx}">${escapeHtml(lbl)}</button>`;
      }).join('');
      tabsBox.querySelectorAll('[data-q-win]').forEach(btn => {
        btn.addEventListener('click', () => {
          if (!confirmLeaveAdminEdit()) return;
          state.qReview.winIdx = Number(btn.getAttribute('data-q-win'));
          renderQuarantineReview();
          loadAndDrawQuarantineWindowImage();
        });
      });
    }

    if (curWin) {
      const diffByWin = new Map((item.window_diffs || []).map(wd => [wd.window_idx, wd.diff]));
      const d = diffByWin.get(curWin.window_idx) || { added: [], deleted: [], relabeled: [], digit_fixed: [] };
      const beforeParts = [];
      const afterParts = [];
      if (d.relabeled?.length) {
        beforeParts.push(`класс: ${d.relabeled.map(r => escapeHtml(r.from_label)).join(', ')}`);
        afterParts.push(`класс: ${d.relabeled.map(r => `${escapeHtml(r.from_label)} → ${escapeHtml(r.to_label)}`).join(', ')}`);
      }
      if (d.digit_fixed?.length) {
        beforeParts.push(`цифра: ${d.digit_fixed.map(f => escapeHtml(f.from_value ?? '?')).join(', ')}`);
        afterParts.push(`цифра: ${d.digit_fixed.map(f => `${escapeHtml(f.from_value ?? '?')} → ${escapeHtml(f.to_value)}`).join(', ')}`);
      }
      if (d.added?.length) afterParts.push(`+добавлено: ${d.added.map(a => escapeHtml(a.label)).join(', ')}`);
      if (d.deleted?.length) afterParts.push(`-удалено: ${d.deleted.map(a => escapeHtml(a.label)).join(', ')}`);

      const errs = curWin.errors || [];
      if (errBox) {
        if (errs.length > 0) {
          errBox.innerHTML = `<div style="background:#fef2f2; border:1px solid #fecaca; border-radius:8px; padding:8px 12px; color:#991b1b; font-size:12.5px;">${errs.map(e => `<div>• ${escapeHtml(e)}</div>`).join('')}</div>`;
        } else {
          errBox.innerHTML = `<div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:8px; padding:8px 12px; color:#166534; font-size:12.5px; font-weight:600;">✅ Ошибок нет</div>`;
        }
      }

      if (beforeTitle) {
        beforeTitle.style.color = '#991b1b';
        beforeTitle.innerHTML = `Было (модель) — ошибок: ${(curWin.original_errors || []).length}`
          + (beforeParts.length ? `<span class="apt-q-diff-badge">${beforeParts.join(' · ')}</span>` : '');
      }
      if (afterTitle) {
        afterTitle.style.color = errs.length > 0 ? '#991b1b' : '#166534';
        afterTitle.innerHTML = `Стало — ошибок: ${errs.length}`
          + (afterParts.length ? `<span class="apt-q-diff-badge">${afterParts.join(' · ')}</span>` : '');
      }
    }

    if (cropsBox) {
      const crops = (item.digit_crops || []).filter(c => (curWin ? Number(c.window_idx) === Number(curWin.window_idx) : true));
      if (crops.length === 0) {
        if (cropsSection) cropsSection.style.display = 'none';
        cropsBox.innerHTML = '';
      } else {
        if (cropsSection) cropsSection.style.display = 'block';
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

    const isExported = Boolean(item.exported_at);
    const canEdit = !isExported;
    const isAdminEditing = Boolean(state.adminEditor && state.adminEditor.isActive());
    if (editBtn) {
      editBtn.style.display = canEdit && !isAdminEditing ? '' : 'none';
    }
    if (decisionBar) {
      if (isExported) {
        decisionBar.innerHTML = `<div><span class="badge-status status-success">Выгружено в датасет (${escapeHtml(item.exported_at)})</span></div>`;
      } else if (item.status === 'pending') {
        decisionBar.innerHTML = `
          <button type="button" class="btn btn-danger" id="apt-q-reject-btn">❌ Отклонить</button>
          <button type="button" class="btn btn-success" id="apt-q-approve-btn">✅ Одобрить</button>
        `;
        $('apt-q-reject-btn')?.addEventListener('click', () => decideQuarantineReview('reject'));
        $('apt-q-approve-btn')?.addEventListener('click', () => decideQuarantineReview('approve'));
      } else if (item.status === 'approved') {
        decisionBar.innerHTML = `
          <span class="badge-status status-success">Одобрено</span>
          <button type="button" class="btn btn-danger" id="apt-q-reject-btn">❌ Перевести в отклонённые</button>
        `;
        $('apt-q-reject-btn')?.addEventListener('click', () => decideQuarantineReview('reject'));
      } else {
        decisionBar.innerHTML = `
          <span class="badge-status status-error">Отклонено</span>
          <button type="button" class="btn btn-success" id="apt-q-approve-btn">✅ Одобрить</button>
        `;
        $('apt-q-approve-btn')?.addEventListener('click', () => decideQuarantineReview('approve'));
      }
    }

    updateEyeButton('apt-q-before-eye-btn', state.qHideBeforeBoxes);
    updateEyeButton('apt-q-after-eye-btn', state.qHideAfterBoxes);
    drawQuarantineReviewCanvases();
  }

  async function commitAdminQuarantineEdits(edits) {
    const item = state.qReview.item;
    const w = getCurrentQuarantineWindow();
    if (!item || !w) throw new Error('Окно записи недоступно.');
    const updatedItem = await apiJson(
      `/api/ml/admin/quarantine/${encodeURIComponent(item.item_id)}/windows/${w.window_idx}/edit`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ edits }),
      }
    );
    state.qReview.item = updatedItem;
  }

  function updateAdminEditButton() {
    const editBtn = $('apt-q-edit-btn');
    const item = state.qReview.item;
    const isEd = Boolean(state.adminEditor && state.adminEditor.isActive());
    if (editBtn) {
      editBtn.style.display = (item && !item.exported_at && !isEd) ? '' : 'none';
    }
    drawQuarantineReviewCanvases();
  }

  async function decideQuarantineReview(decision) {
    const item = state.qReview.item;
    if (!item) return;

    if (state.adminEditor && state.adminEditor.isActive()) {
      if (state.adminEditor.hasChanges()) {
        const ok = await state.adminEditor.commit();
        if (!ok) return;
      } else {
        state.adminEditor.deactivate();
      }
    }

    const approveBtn = $('apt-q-approve-btn');
    const rejectBtn = $('apt-q-reject-btn');
    if (approveBtn) approveBtn.disabled = true;
    if (rejectBtn) rejectBtn.disabled = true;
    try {
      await apiJson(`/api/ml/admin/quarantine/${encodeURIComponent(item.item_id)}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision }),
      });

      if (item.status !== 'pending') {
        refreshQuarantineBadge();
        await openQuarantineReview(item.item_id, state.qReview.order);
        return;
      }

      const order = state.qReview.order || [];
      const curPos = order.indexOf(item.item_id);
      const pendingData = await apiJson('/api/ml/admin/quarantine?status=pending');
      const pendingItems = Array.isArray(pendingData) ? pendingData : (Array.isArray(pendingData?.items) ? pendingData.items : []);
      const pendingSet = new Set(pendingItems.map(x => x.item_id));
      refreshQuarantineBadge();

      let nextId = null;
      if (curPos >= 0) {
        for (let i = curPos + 1; i < order.length; i++) {
          if (pendingSet.has(order[i])) {
            nextId = order[i];
            break;
          }
        }
      }
      if (!nextId && pendingItems.length > 0) {
        nextId = pendingItems[0].item_id;
      }

      if (nextId) {
        await openQuarantineReview(nextId, order);
      } else {
        closeQuarantineReview(true);
        const doneBox = $('apt-q-review-done');
        if (doneBox) {
          doneBox.style.display = 'block';
          doneBox.innerHTML = `<div style="background:#f0fdf4; border:1px solid #86efac; border-radius:10px; padding:10px 14px; color:#166534; font-size:13.5px; font-weight:600;">Все правки на проверке разобраны ✅</div>`;
        }
      }
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
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Выгрузка датасета...';
    }
    try {
      const res = await apiJson('/api/ml/admin/export-approved', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ include_exported: false }),
      });
      if (resBox) {
        resBox.style.display = 'block';
        if (res.exported_pages > 0) {
          resBox.innerHTML = `
            <div style="background:#f0fdf4; border:1px solid #86efac; border-radius:10px; padding:12px 16px; color:#166534; font-size:13.5px;">
              <b>Выгрузка завершена.</b> Страниц: <b>${Number(res.exported_pages)}</b>, кропов цифр: <b>${Number(res.exported_crops || 0)}</b>.<br>
              <span style="font-size:12.5px;">Папка: <code>${escapeHtml(res.export_dir || '')}</code></span>
            </div>
          `;
        } else {
          resBox.innerHTML = `
            <div style="background:#f8fafc; border:1px solid var(--border); border-radius:10px; padding:12px 16px; font-size:13.5px;">
              ${escapeHtml(res.message || 'Нет новых одобренных записей для выгрузки.')}
            </div>
          `;
        }
      }
      loadQuarantineList();
      refreshQuarantineBadge();
    } catch (err) {
      alert('Ошибка выгрузки: ' + err.message);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Выгрузить одобренные в датасет';
      }
    }
  }

  // ─── Инициализация событий DOM ──────────────────────────────────────
  function initOnce() {
    if (state.initialized) return;
    state.initialized = true;

    setupDialogPolyfill();
    setupCanvasInteractions();

    state.operatorEditor = window.AptBoxEditor ? window.AptBoxEditor.create({
      canvas: $('apt-window-canvas'),
      panel: $('apt-image-edit-panel'),
      getContext: () => {
        const page = getCurrentPage();
        const win = getCurrentWindow();
        if (!page || !win || !state.currentWindowImg) return null;
        return {
          image: state.currentWindowImg,
          cropRect: win.crop_rect || [0, 0, state.currentWindowImg.naturalWidth, state.currentWindowImg.naturalHeight],
          windowBbox: win.bbox || null,
          returnSplitY: win.return_split_y ?? null,
          pageWidth: page.width || 10000,
          pageHeight: page.height || 10000,
          elements: win.elements || [],
        };
      },
      getAddableLabels: getSafeOperatorLabels,
      digitLabels: state.digitLabels,
      isEditable: isElementEditable,
      escapeHtml,
      onCommit: commitOperatorImageEdits,
      onAfterCommit: afterOperatorImageCommit,
      onStateChange: updateImageEditButtons,
      isBoxesHidden: () => state.hideOperatorBoxes,
    }) : null;

    state.adminEditor = window.AptBoxEditor ? window.AptBoxEditor.create({
      canvas: $('apt-q-after-canvas'),
      panel: $('apt-q-edit-panel'),
      getContext: () => {
        const item = state.qReview.item;
        const w = getCurrentQuarantineWindow();
        if (!item || !w || !state.qReview.img) return null;
        return {
          image: state.qReview.img,
          cropRect: w.crop_rect || [0, 0, state.qReview.img.naturalWidth, state.qReview.img.naturalHeight],
          windowBbox: w.bbox || null,
          returnSplitY: w.return_split_y ?? null,
          pageWidth: item.width || 10000,
          pageHeight: item.height || 10000,
          elements: w.edited_elements || [],
        };
      },
      getAddableLabels: getSafeOperatorLabels,
      digitLabels: state.digitLabels,
      isEditable: isElementEditable,
      escapeHtml,
      onCommit: commitAdminQuarantineEdits,
      onAfterCommit: () => renderQuarantineReview(),
      onStateChange: updateAdminEditButton,
      isBoxesHidden: () => state.qHideAfterBoxes,
      commitLabel: '✅ Сохранить правки разметки',
    }) : null;

    const pdfInp = $('apt-pdf-input');
    const dropEmpty = $('apt-pdf-dropzone');
    const dropLoaded = $('apt-pdf-loaded');

    pdfInp?.addEventListener('change', e => {
      if (e.target.files?.length) handlePdfFilesSelect(e.target.files);
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
        if (e.dataTransfer?.files?.length) handlePdfFilesSelect(e.dataTransfer.files);
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
      const filtered = getFilteredPages();
      if (filtered.length > 0 && !filtered.some(p => p.page_id === state.currentPageId)) {
        selectPage(filtered[0].page_id, true);
        return;
      }
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
        const firstProb = wb.page_ids.map(id => pagesById.get(id)).find(p => p && pageHasActiveIssues(p));
        selectPage((firstProb ? firstProb.page_id : wb.page_ids[0]), true);
      }
    });

    $('apt-prev-page-btn')?.addEventListener('click', () => {
      const list = getFilteredPages();
      if (list.length === 0) return;
      const idx = list.findIndex(p => p.page_id === state.currentPageId);
      if (idx > 0) {
        selectPage(list[idx - 1].page_id, true);
      } else if (idx === -1) {
        const curGlobal = Number(getCurrentPage()?.global_page_num || 0);
        const prevCand = [...list].reverse().find(p => Number(p.global_page_num || 0) < curGlobal);
        if (prevCand) selectPage(prevCand.page_id, true);
      }
    });

    $('apt-next-page-btn')?.addEventListener('click', () => {
      const list = getFilteredPages();
      if (list.length === 0) return;
      const idx = list.findIndex(p => p.page_id === state.currentPageId);
      if (idx >= 0 && idx < list.length - 1) {
        selectPage(list[idx + 1].page_id, true);
      } else if (idx === -1) {
        const curGlobal = Number(getCurrentPage()?.global_page_num || 0);
        const nextCand = list.find(p => Number(p.global_page_num || 0) > curGlobal) || list[0];
        if (nextCand) selectPage(nextCand.page_id, true);
      }
    });

    $('apt-btn-full-page')?.addEventListener('click', () => {
      const page = getCurrentPage();
      if (!state.activeSessionId || !page) return;
      const url = `/api/ml/sessions/${encodeURIComponent(state.activeSessionId)}/pages/${encodeURIComponent(page.page_id)}/image?annotated=1`;
      openFullPageDialog(url, `МЛ ${page.ml_number || '—'} · Стр. ${page.page_num} (полный лист)`);
    });

    $('aptFullPageCloseBtn')?.addEventListener('click', () => $('aptFullPageDialog')?.close());
    $('aptFullPageCloseFooterBtn')?.addEventListener('click', () => $('aptFullPageDialog')?.close());

    $('apt-btn-reset-window')?.addEventListener('click', () => sendWindowAction({ action: 'reset_window' }));
    $('apt-btn-image-edit-mode')?.addEventListener('click', () => {
      cancelDrawMode();
      state.selectedElementId = null;
      state.hideOperatorBoxes = false;
      updateEyeButton('apt-op-eye-btn', false);
      renderBoxInspector();
      state.operatorEditor?.activate();
    });
    $('apt-op-eye-btn')?.addEventListener('click', () => {
      state.hideOperatorBoxes = !state.hideOperatorBoxes;
      updateEyeButton('apt-op-eye-btn', state.hideOperatorBoxes);
      drawWindowCanvas();
    });
    $('apt-q-before-eye-btn')?.addEventListener('click', () => {
      state.qHideBeforeBoxes = !state.qHideBeforeBoxes;
      updateEyeButton('apt-q-before-eye-btn', state.qHideBeforeBoxes);
      drawQuarantineReviewCanvases();
    });
    $('apt-q-after-eye-btn')?.addEventListener('click', () => {
      state.qHideAfterBoxes = !state.qHideAfterBoxes;
      updateEyeButton('apt-q-after-eye-btn', state.qHideAfterBoxes);
      drawQuarantineReviewCanvases();
    });
    $('apt-draw-cancel-btn')?.addEventListener('click', cancelDrawMode);

    $('apt-btn-finalize')?.addEventListener('click', finalizeSession);
    $('apt-btn-download-report')?.addEventListener('click', e => {
      downloadSessionBlob('report', `report_${state.activeSessionId || 'ml'}.txt`, e.currentTarget);
    });
    $('apt-btn-download-zip')?.addEventListener('click', e => {
      downloadSessionBlob('export-zip', `waybills_${state.activeSessionId || 'ml'}.zip`, e.currentTarget);
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
    $('apt-q-start-review-btn')?.addEventListener('click', () => {
      if (state.qItems.length === 0) return;
      const doneBox = $('apt-q-review-done');
      if (doneBox) doneBox.style.display = 'none';
      openQuarantineReview(state.qItems[0].item_id, state.qItems.map(i => i.item_id));
    });
    $('apt-q-back-btn')?.addEventListener('click', () => closeQuarantineReview(false));
    $('apt-q-edit-btn')?.addEventListener('click', () => {
      state.qHideAfterBoxes = false;
      updateEyeButton('apt-q-after-eye-btn', false);
      state.adminEditor?.activate();
    });
    $('apt-q-full-page-btn')?.addEventListener('click', () => {
      const item = state.qReview.item;
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
      await checkMlHealth();
      await loadRecentSessions();
    },

    async onOpenAdminQuarantine() {
      initOnce();
      await loadQuarantineList();
    },

    refreshQuarantineBadge,

    isBusy() {
      if (
        state.uploading
        || state.actionPending
        || state.pollTimer !== null
        || state.drawMode !== null
        || (state.operatorEditor && state.operatorEditor.isActive())
        || (state.adminEditor && state.adminEditor.isActive())
      ) {
        return true;
      }
      if ($('aptFullPageDialog')?.open || ($('apt-q-review-view') && !$('apt-q-review-view').hidden)) {
        return true;
      }
      // Не прерываем страницу автообновлением, если у оператора открыта активная проверка с несохранёнными правками
      if (state.session && (state.session.pages || []).some(p => hasPageChanges(p) && !p.quarantine_item_id)) {
        return true;
      }
      return false;
    },

    onLogout() {
      resetWorkspaceView();
      state.adminEditor?.deactivate();
      state.qReview = { item: null, order: [], winIdx: null, img: null, imgKey: null };
      const rv = $('apt-q-review-view');
      if (rv) rv.hidden = true;
      const lv = $('apt-q-list-view');
      if (lv) lv.hidden = false;
      revokeUrlPool(state.quarantineUrls);
      if (state.fullPageUrl) {
        try { URL.revokeObjectURL(state.fullPageUrl); } catch (_) {}
        state.fullPageUrl = null;
      }
      $('aptFullPageDialog')?.close();
    },
  };
})();
