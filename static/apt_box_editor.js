/* ==========================================================================
   Общий интерактивный редактор рамок на Canvas (v2.3)
   Используется и оператором («Ошибка на изображении»), и админом в очереди.
   ========================================================================== */
(function (root) {
  'use strict';

  function validateGeometry(bbox, windowBbox) {
    if (!Array.isArray(bbox) || bbox.length !== 4) {
      return 'Некорректные координаты рамки.';
    }
    const [x1, y1, x2, y2] = bbox;
    if (![x1, y1, x2, y2].every(Number.isFinite)) {
      return 'Некорректные координаты рамки.';
    }
    if (x2 - x1 < 8 || y2 - y1 < 8) {
      return 'Выделенная область слишком маленькая (минимум 8×8 пикселей).';
    }
    if (Array.isArray(windowBbox) && windowBbox.length === 4) {
      const [wx1, wy1, wx2, wy2] = windowBbox;
      const cx = (x1 + x2) / 2;
      const cy = (y1 + y2) / 2;
      if (cx < wx1 - 60 || cx > wx2 + 60 || cy < wy1 - 60 || cy > wy2 + 60) {
        return 'Центр рамки должен находиться в пределах текущего окна аптеки.';
      }
    }
    return null;
  }

  function buildEdits(items, deletedIds) {
    const edits = [];
    if (deletedIds) {
      for (const id of deletedIds) {
        edits.push({ action: 'delete_box', element_id: id });
      }
    }
    for (const el of (items || [])) {
      if (el._state === 'modified') {
        const edit = {
          action: 'update_box',
          element_id: el.id,
          new_label: el.label,
          bbox: (el.bbox || []).map(v => Math.round(v)),
        };
        const val = Number(el.ocr_value);
        if (el._valueTouched && Number.isInteger(val) && val >= 1 && val <= 99) {
          edit.new_value = val;
        }
        edits.push(edit);
      } else if (el._state === 'added') {
        const edit = {
          action: 'add_box',
          new_label: el.label,
          bbox: (el.bbox || []).map(v => Math.round(v)),
        };
        const val = Number(el.ocr_value);
        if (el.ocr_value !== null && el.ocr_value !== undefined && Number.isInteger(val) && val >= 1 && val <= 99) {
          edit.new_value = val;
        }
        edits.push(edit);
      }
    }
    return edits;
  }

  function create(opts) {
    const {
      canvas,
      panel,
      getContext,
      getAddableLabels,
      digitLabels,
      isEditable,
      escapeHtml,
      onCommit,
      onAfterCommit,
      onStateChange,
      isBoxesHidden,
      commitLabel = '✅ Завершить правку снимка',
    } = opts;

    let active = false;
    let items = [];
    let deletedIds = new Set();
    let selectedId = null;
    let drag = null;
    let addLabel = null;
    let menu = null; // null | 'add' | 'relabel'
    let message = '';
    let busy = false;
    let newCounter = 0;

    function getCtxSafe() {
      return typeof getContext === 'function' ? getContext() : null;
    }

    function getMetrics(ctxObj) {
      const [cx1, cy1, cx2, cy2] = ctxObj.cropRect || [0, 0, canvas.width, canvas.height];
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

    function clientToCanvas(evt) {
      const rect = canvas.getBoundingClientRect();
      const x = (evt.clientX - rect.left) * (canvas.width / Math.max(1, rect.width));
      const y = (evt.clientY - rect.top) * (canvas.height / Math.max(1, rect.height));
      return {
        x: Math.max(0, Math.min(canvas.width, x)),
        y: Math.max(0, Math.min(canvas.height, y)),
      };
    }

    function countChanges() {
      let n = deletedIds.size;
      for (const el of items) {
        if (el._state === 'added' || el._state === 'modified') n += 1;
      }
      return n;
    }

    function hasChanges() {
      return countChanges() > 0;
    }

    function findSelected() {
      if (!selectedId) return null;
      return items.find(e => e.id === selectedId) || null;
    }

    function getHandlesForElement(el, m) {
      const [x1, y1, x2, y2] = el.bbox;
      const dx = (x1 - m.cx1) * m.scaleX;
      const dy = (y1 - m.cy1) * m.scaleY;
      const dw = (x2 - x1) * m.scaleX;
      const dh = (y2 - y1) * m.scaleY;
      return [
        { mode: 'nw', hx: dx, hy: dy, cursor: 'nwse-resize' },
        { mode: 'n', hx: dx + dw / 2, hy: dy, cursor: 'ns-resize' },
        { mode: 'ne', hx: dx + dw, hy: dy, cursor: 'nesw-resize' },
        { mode: 'e', hx: dx + dw, hy: dy + dh / 2, cursor: 'ew-resize' },
        { mode: 'se', hx: dx + dw, hy: dy + dh, cursor: 'nwse-resize' },
        { mode: 's', hx: dx + dw / 2, hy: dy + dh, cursor: 'ns-resize' },
        { mode: 'sw', hx: dx, hy: dy + dh, cursor: 'nesw-resize' },
        { mode: 'w', hx: dx, hy: dy + dh / 2, cursor: 'ew-resize' },
      ];
    }

    function findHandleAt(ctxObj, cx, cy) {
      const el = findSelected();
      if (!el || !isEditable(el)) return null;
      const m = getMetrics(ctxObj);
      const hs = Math.max(8, Math.round(canvas.width / 120));
      const tol = hs * 1.2;
      for (const h of getHandlesForElement(el, m)) {
        if (Math.abs(cx - h.hx) <= tol && Math.abs(cy - h.hy) <= tol) {
          return { el, mode: h.mode, cursor: h.cursor };
        }
      }
      return null;
    }

    function findElementAt(ctxObj, cx, cy) {
      const m = getMetrics(ctxObj);
      let best = null;
      let bestArea = Infinity;
      for (const el of items) {
        if (!isEditable(el)) continue;
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

    function draw() {
      if (!active || !canvas) return;
      const ctxObj = getCtxSafe();
      if (!ctxObj || !ctxObj.image) return;
      const img = ctxObj.image;

      if (canvas.width !== img.naturalWidth || canvas.height !== img.naturalHeight) {
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
      }

      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0);

      if (typeof isBoxesHidden === 'function' && isBoxesHidden() && !(drag && drag.mode === 'draw')) {
        return;
      }

      const m = getMetrics(ctxObj);

      if (ctxObj.returnSplitY !== null && ctxObj.returnSplitY !== undefined) {
        const splitY = (ctxObj.returnSplitY - m.cy1) * m.scaleY;
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

      if (Array.isArray(ctxObj.windowBbox) && ctxObj.windowBbox.length === 4) {
        const [wx1, wy1, wx2, wy2] = ctxObj.windowBbox;
        ctx.save();
        ctx.strokeStyle = 'rgba(29, 78, 216, 0.55)';
        ctx.lineWidth = 2;
        ctx.setLineDash([6, 4]);
        ctx.strokeRect(
          (wx1 - m.cx1) * m.scaleX,
          (wy1 - m.cy1) * m.scaleY,
          (wx2 - wx1) * m.scaleX,
          (wy2 - wy1) * m.scaleY
        );
        ctx.restore();
      }

      const hs = Math.max(8, Math.round(canvas.width / 120));
      const half = hs / 2;

      for (const el of items) {
        const [x1, y1, x2, y2] = el.bbox;
        const drawX = (x1 - m.cx1) * m.scaleX;
        const drawY = (y1 - m.cy1) * m.scaleY;
        const drawW = (x2 - x1) * m.scaleX;
        const drawH = (y2 - y1) * m.scaleY;

        const editable = isEditable(el);
        const isSel = el.id === selectedId;
        const isChanged = el._state === 'added' || el._state === 'modified' || el.source === 'operator' || el.source === 'admin';

        let stroke = '#16a34a';
        let fill = 'rgba(22, 163, 74, 0.08)';
        if (!editable) {
          stroke = 'rgba(100, 116, 139, 0.7)';
          fill = 'rgba(100, 116, 139, 0.04)';
        } else if (isChanged) {
          stroke = '#0284c7';
          fill = 'rgba(2, 132, 199, 0.14)';
        }

        ctx.save();
        if (!editable) ctx.setLineDash([4, 3]);
        ctx.fillStyle = fill;
        ctx.fillRect(drawX, drawY, drawW, drawH);

        ctx.strokeStyle = isSel ? '#1d4ed8' : stroke;
        ctx.lineWidth = isSel ? 3.5 : 2;
        ctx.strokeRect(drawX, drawY, drawW, drawH);

        const isDigit = digitLabels && digitLabels.has(el.label);
        const tag = isDigit && el.ocr_value !== null && el.ocr_value !== undefined
          ? `${el.label}: ${el.ocr_value}`
          : el.label;
        ctx.font = 'bold 12px -apple-system, BlinkMacSystemFont, sans-serif';
        const tw = ctx.measureText(tag).width;
        const labelY = drawY >= 20 ? drawY - 18 : drawY + 2;
        ctx.fillStyle = isSel ? '#1d4ed8' : (!editable ? 'rgba(71, 85, 105, 0.88)' : stroke);
        ctx.fillRect(drawX, labelY, tw + 8, 17);
        ctx.fillStyle = '#ffffff';
        ctx.fillText(tag, drawX + 4, labelY + 13);

        if (isSel && editable) {
          ctx.fillStyle = '#ffffff';
          ctx.strokeStyle = '#1d4ed8';
          ctx.lineWidth = 2;
          ctx.setLineDash([]);
          for (const h of getHandlesForElement(el, m)) {
            ctx.fillRect(h.hx - half, h.hy - half, hs, hs);
            ctx.strokeRect(h.hx - half, h.hy - half, hs, hs);
          }
        }
        ctx.restore();
      }

      if (drag && drag.mode === 'draw') {
        const rx = Math.min(drag.startX, drag.currX);
        const ry = Math.min(drag.startY, drag.currY);
        const rw = Math.abs(drag.currX - drag.startX);
        const rh = Math.abs(drag.currY - drag.startY);
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

    function renderPanel() {
      if (!panel) return;
      if (!active) {
        panel.hidden = true;
        panel.innerHTML = '';
        return;
      }
      panel.hidden = false;
      const sel = findSelected();
      const labels = (typeof getAddableLabels === 'function' ? getAddableLabels() : []) || [];
      const changesCount = countChanges();
      const isDigitSel = sel && digitLabels && digitLabels.has(sel.label);

      let subHtml = '';
      if (addLabel) {
        subHtml = `
          <div class="apt-edit-row" style="margin-top:8px; color:#1d4ed8; font-weight:600; font-size:12.5px;">
            Обведите на снимке рамку «${escapeHtml(addLabel)}» (Esc — отмена)
          </div>
        `;
      }

      const relabelOpts = sel ? labels.filter(l => l !== sel.label) : [];

      panel.innerHTML = `
        <div class="apt-edit-row">
          <div class="apt-edit-dropdown-wrap">
            <button type="button" class="btn btn-outline btn-sm" data-ed-act="toggle-add" ${busy ? 'disabled' : ''}>➕ Добавить рамку</button>
            ${menu === 'add' ? `
              <div class="apt-class-menu" role="menu">
                ${labels.map(l => `
                  <button type="button" class="apt-class-menu-item" role="menuitem" data-ed-act="pick-add" data-lbl="${escapeHtml(l)}">${escapeHtml(l)}</button>
                `).join('')}
              </div>
            ` : ''}
          </div>
          ${sel ? `
            <div class="apt-edit-dropdown-wrap">
              <button type="button" class="btn btn-outline btn-sm" data-ed-act="toggle-relabel" ${busy ? 'disabled' : ''}>🏷️ Изменить класс</button>
              ${menu === 'relabel' ? `
                <div class="apt-class-menu" role="menu">
                  ${relabelOpts.map(l => `
                    <button type="button" class="apt-class-menu-item" role="menuitem" data-ed-act="pick-relabel" data-lbl="${escapeHtml(l)}">${escapeHtml(l)}</button>
                  `).join('')}
                </div>
              ` : ''}
            </div>
            <button type="button" class="btn btn-danger btn-sm" data-ed-act="delete-sel" ${busy ? 'disabled' : ''}>🗑️ Удалить рамку</button>
            ${isDigitSel ? `
              <label style="display:inline-flex; align-items:center; gap:6px; font-size:12.5px; font-weight:600;">
                Число (1..99):
                <input type="number" min="1" max="99" data-ed-act="digit-input" value="${escapeHtml(sel.ocr_value ?? '')}" ${busy ? 'disabled' : ''} style="width:68px; padding:4px 6px; border:1px solid #93c5fd; border-radius:6px; font-weight:700;">
              </label>
            ` : ''}
          ` : ''}
          <div class="apt-edit-spacer"></div>
          <span style="font-size:12.5px; color:var(--text-muted); font-weight:600;">Правок: ${changesCount}</span>
          <button type="button" class="btn btn-outline btn-sm" data-ed-act="cancel" ${busy ? 'disabled' : ''}>Отменить</button>
          <button type="button" class="btn btn-primary btn-sm" data-ed-act="commit" ${busy ? 'disabled' : ''}>${busy ? 'Сохранение...' : escapeHtml(commitLabel)}</button>
        </div>
        ${subHtml}
        ${message ? `<div class="apt-edit-msg">${escapeHtml(message)}</div>` : ''}
        <div class="apt-edit-hint">Нажмите на рамку, чтобы изменить размер (тяните за уголки), класс или удалить</div>
      `;
    }

    function activate() {
      const ctxObj = getCtxSafe();
      if (!ctxObj || !ctxObj.image) return;
      active = true;
      items = (ctxObj.elements || []).map(el => ({
        ...el,
        bbox: Array.isArray(el.bbox) ? [...el.bbox] : [0, 0, 0, 0],
        _state: 'orig',
        _valueTouched: false,
      }));
      deletedIds = new Set();
      selectedId = null;
      drag = null;
      addLabel = null;
      menu = null;
      message = '';
      busy = false;
      renderPanel();
      draw();
      if (typeof onStateChange === 'function') onStateChange();
    }

    function deactivate() {
      if (!active && !items.length) return;
      active = false;
      items = [];
      deletedIds = new Set();
      selectedId = null;
      drag = null;
      addLabel = null;
      menu = null;
      message = '';
      busy = false;
      if (canvas) canvas.style.cursor = 'default';
      renderPanel();
      if (typeof onStateChange === 'function') onStateChange();
    }

    async function commit() {
      if (!active) return false;
      if (!hasChanges()) {
        deactivate();
        return true;
      }
      if (busy) return false;
      const edits = buildEdits(items, deletedIds);
      busy = true;
      message = '';
      renderPanel();
      try {
        await onCommit(edits);
        deactivate();
        if (typeof onAfterCommit === 'function') onAfterCommit();
        return true;
      } catch (err) {
        busy = false;
        message = err?.message || 'Не удалось сохранить правки.';
        renderPanel();
        return false;
      }
    }

    if (canvas) {
      canvas.addEventListener('pointerdown', evt => {
        if (!active || busy) return;
        const ctxObj = getCtxSafe();
        if (!ctxObj) return;
        const pt = clientToCanvas(evt);
        message = '';

        if (addLabel) {
          drag = {
            mode: 'draw',
            startX: pt.x,
            startY: pt.y,
            currX: pt.x,
            currY: pt.y,
            pointerId: evt.pointerId,
          };
          try { canvas.setPointerCapture(evt.pointerId); } catch (_) {}
          draw();
          return;
        }

        const h = findHandleAt(ctxObj, pt.x, pt.y);
        if (h) {
          drag = {
            mode: h.mode,
            startX: pt.x,
            startY: pt.y,
            origBbox: [...h.el.bbox],
            moved: false,
            pointerId: evt.pointerId,
          };
          try { canvas.setPointerCapture(evt.pointerId); } catch (_) {}
          return;
        }

        const hit = findElementAt(ctxObj, pt.x, pt.y);
        if (hit) {
          selectedId = hit.id;
          menu = null;
          drag = {
            mode: 'move',
            startX: pt.x,
            startY: pt.y,
            origBbox: [...hit.bbox],
            moved: false,
            pointerId: evt.pointerId,
          };
          try { canvas.setPointerCapture(evt.pointerId); } catch (_) {}
        } else {
          selectedId = null;
          menu = null;
        }
        renderPanel();
        draw();
      });

      canvas.addEventListener('pointermove', evt => {
        if (!active) return;
        const ctxObj = getCtxSafe();
        if (!ctxObj) return;
        const pt = clientToCanvas(evt);

        if (drag && drag.mode === 'draw') {
          drag.currX = pt.x;
          drag.currY = pt.y;
          draw();
          return;
        }

        if (drag) {
          const sel = findSelected();
          if (!sel) return;
          if (Math.hypot(pt.x - drag.startX, pt.y - drag.startY) > 3) {
            drag.moved = true;
          }
          if (!drag.moved) return;

          const m = getMetrics(ctxObj);
          const dxImg = Math.round((pt.x - drag.startX) / m.scaleX);
          const dyImg = Math.round((pt.y - drag.startY) / m.scaleY);
          const maxW = ctxObj.pageWidth || 10000;
          const maxH = ctxObj.pageHeight || 10000;
          let [nx1, ny1, nx2, ny2] = drag.origBbox;
          const minSize = 8;

          if (drag.mode === 'move') {
            const bw = nx2 - nx1;
            const bh = ny2 - ny1;
            nx1 = Math.max(0, Math.min(maxW - bw, nx1 + dxImg));
            ny1 = Math.max(0, Math.min(maxH - bh, ny1 + dyImg));
            nx2 = nx1 + bw;
            ny2 = ny1 + bh;
          } else {
            if (drag.mode.includes('w')) nx1 = Math.max(0, Math.min(nx2 - minSize, nx1 + dxImg));
            if (drag.mode.includes('e')) nx2 = Math.min(maxW, Math.max(nx1 + minSize, nx2 + dxImg));
            if (drag.mode.includes('n')) ny1 = Math.max(0, Math.min(ny2 - minSize, ny1 + dyImg));
            if (drag.mode.includes('s')) ny2 = Math.min(maxH, Math.max(ny1 + minSize, ny2 + dyImg));
          }
          sel.bbox = [nx1, ny1, nx2, ny2];
          draw();
          return;
        }

        if (addLabel) {
          canvas.style.cursor = 'crosshair';
          return;
        }
        const h = findHandleAt(ctxObj, pt.x, pt.y);
        if (h) {
          canvas.style.cursor = h.cursor;
          return;
        }
        const hit = findElementAt(ctxObj, pt.x, pt.y);
        canvas.style.cursor = hit ? 'move' : 'default';
      });

      const finishPointer = evt => {
        if (!active || !drag) return;
        const ctxObj = getCtxSafe();
        const curDrag = drag;
        drag = null;
        try { canvas.releasePointerCapture(evt.pointerId); } catch (_) {}
        if (!ctxObj) return;

        if (curDrag.mode === 'draw') {
          const targetLabel = addLabel;
          const pt = clientToCanvas(evt);
          const m = getMetrics(ctxObj);
          const rx = Math.min(curDrag.startX, pt.x);
          const ry = Math.min(curDrag.startY, pt.y);
          const rw = Math.abs(pt.x - curDrag.startX);
          const rh = Math.abs(pt.y - curDrag.startY);
          const maxW = ctxObj.pageWidth || 10000;
          const maxH = ctxObj.pageHeight || 10000;
          const bbox = [
            Math.max(0, Math.round(m.cx1 + rx / m.scaleX)),
            Math.max(0, Math.round(m.cy1 + ry / m.scaleY)),
            Math.min(maxW, Math.round(m.cx1 + (rx + rw) / m.scaleX)),
            Math.min(maxH, Math.round(m.cy1 + (ry + rh) / m.scaleY)),
          ];
          const err = validateGeometry(bbox, ctxObj.windowBbox);
          addLabel = null;
          menu = null;
          selectedId = null;
          if (err) {
            message = err;
          } else {
            newCounter += 1;
            items.push({
              id: `new_${newCounter}`,
              label: targetLabel,
              bbox,
              ocr_value: null,
              source: 'operator',
              editable: true,
              _state: 'added',
              _valueTouched: false,
            });
          }
          renderPanel();
          draw();
          return;
        }

        const sel = findSelected();
        if (sel && curDrag.moved) {
          const err = validateGeometry(sel.bbox, ctxObj.windowBbox);
          if (err) {
            sel.bbox = [...curDrag.origBbox];
            message = err;
          } else {
            if (sel._state !== 'added') sel._state = 'modified';
            selectedId = null;
            menu = null;
          }
          renderPanel();
          draw();
        }
      };

      canvas.addEventListener('pointerup', finishPointer);
      canvas.addEventListener('pointercancel', evt => {
        if (!active || !drag) return;
        const sel = findSelected();
        if (sel && drag.origBbox) sel.bbox = [...drag.origBbox];
        drag = null;
        try { canvas.releasePointerCapture(evt.pointerId); } catch (_) {}
        draw();
      });
    }

    if (typeof document !== 'undefined') {
      document.addEventListener('keydown', evt => {
        if (!active) return;
        if (evt.key === 'Escape') {
          if (addLabel || menu || selectedId) {
            evt.preventDefault();
            addLabel = null;
            menu = null;
            selectedId = null;
            renderPanel();
            draw();
          }
          return;
        }
        if (evt.key === 'Enter' && !busy && hasChanges()) {
          const tag = (evt.target && evt.target.tagName) || '';
          if (tag !== 'INPUT' && tag !== 'TEXTAREA' && tag !== 'SELECT' && tag !== 'BUTTON') {
            evt.preventDefault();
            commit();
          }
        }
      });
    }

    if (panel) {
      panel.addEventListener('click', evt => {
        if (!active || busy) return;
        const btn = evt.target.closest('[data-ed-act]');
        if (!btn) {
          if (menu && !evt.target.closest('.apt-edit-dropdown-wrap')) {
            menu = null;
            renderPanel();
          }
          return;
        }
        const act = btn.getAttribute('data-ed-act');
        message = '';

        if (act === 'toggle-add') {
          addLabel = null;
          menu = menu === 'add' ? null : 'add';
          renderPanel();
          draw();
        } else if (act === 'pick-add') {
          addLabel = btn.getAttribute('data-lbl');
          menu = null;
          selectedId = null;
          renderPanel();
          draw();
        } else if (act === 'toggle-relabel') {
          addLabel = null;
          menu = menu === 'relabel' ? null : 'relabel';
          renderPanel();
        } else if (act === 'pick-relabel') {
          const sel = findSelected();
          const newLbl = btn.getAttribute('data-lbl');
          if (sel && newLbl) {
            sel.label = newLbl;
            if (!digitLabels || !digitLabels.has(newLbl)) {
              sel.ocr_value = null;
              sel._valueTouched = false;
            }
            if (sel._state !== 'added') sel._state = 'modified';
            selectedId = null;
            menu = null;
          }
          renderPanel();
          draw();
        } else if (act === 'delete-sel') {
          const sel = findSelected();
          if (sel) {
            if (sel._state !== 'added') {
              deletedIds.add(sel.id);
            }
            items = items.filter(e => e.id !== sel.id);
            selectedId = null;
            menu = null;
          }
          renderPanel();
          draw();
        } else if (act === 'cancel') {
          if (hasChanges() && !root.confirm('Отменить все несохранённые правки на снимке?')) {
            return;
          }
          deactivate();
        } else if (act === 'commit') {
          commit();
        }
      });

      panel.addEventListener('keydown', evt => {
        if (!active || busy || evt.key !== 'Enter') return;
        const inp = evt.target.closest('[data-ed-act="digit-input"]');
        if (!inp) return;
        evt.preventDefault();
        const sel = findSelected();
        if (!sel) return;
        const raw = String(inp.value || '').trim();
        const parsed = parseInt(raw, 10);
        if (!Number.isInteger(parsed) || parsed < 1 || parsed > 99) {
          message = 'Укажите целое число от 1 до 99.';
          renderPanel();
          return;
        }
        sel.ocr_value = parsed;
        sel._valueTouched = true;
        if (sel._state !== 'added') sel._state = 'modified';
        selectedId = null;
        menu = null;
        message = '';
        renderPanel();
        draw();
      });

      panel.addEventListener('change', evt => {
        if (!active || busy) return;
        const inp = evt.target.closest('[data-ed-act="digit-input"]');
        if (!inp) return;
        const sel = findSelected();
        if (!sel) return;
        const raw = String(inp.value || '').trim();
        if (!raw) {
          sel.ocr_value = null;
          sel._valueTouched = false;
          return;
        }
        const parsed = parseInt(raw, 10);
        if (!Number.isInteger(parsed) || parsed < 1 || parsed > 99) {
          message = 'Укажите целое число от 1 до 99.';
          renderPanel();
          return;
        }
        sel.ocr_value = parsed;
        sel._valueTouched = true;
        if (sel._state !== 'added') sel._state = 'modified';
        message = '';
        draw();
      });
    }

    return {
      activate,
      deactivate,
      isActive: () => active,
      hasChanges,
      draw,
      commit,
    };
  }

  const api = { create, buildEdits, validateGeometry };
  root.AptBoxEditor = api;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
})(typeof window !== 'undefined' ? window : globalThis);
