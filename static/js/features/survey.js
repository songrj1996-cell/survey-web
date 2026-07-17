// ============================================================
// STEP 1: Upload
// ============================================================

const uploadZone = $('upload-zone');
const fileInput = $('file-input');

function surveyUploadIsLocked() {
  return !!state.sessionId && state.currentStep > 1;
}

uploadZone.addEventListener('click', () => {
  if (!surveyUploadIsLocked()) fileInput.click();
});
uploadZone.addEventListener('dragover', e => {
  e.preventDefault();
  if (!surveyUploadIsLocked()) uploadZone.classList.add('drag-over');
});
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('drag-over'));
uploadZone.addEventListener('drop', e => {
  e.preventDefault();
  uploadZone.classList.remove('drag-over');
  if (surveyUploadIsLocked()) return;
  const file = e.dataTransfer.files[0];
  if (file) handleUpload(file);
});
fileInput.addEventListener('change', () => {
  if (surveyUploadIsLocked()) {
    fileInput.value = '';
    return;
  }
  if (fileInput.files[0]) handleUpload(fileInput.files[0]);
});

async function handleUpload(file) {
  const MAX = 50 * 1024 * 1024;
  if (file.size > MAX) { showToast('文件超过 50MB 上限', 'error'); return; }
  const uploadSignature = contextFileSignature(file);

  uploadZone.innerHTML = `
    <div class="upload-zone__icon"><div class="spinner" style="width:40px;height:40px;border-width:3px"></div></div>
    <div class="upload-zone__text">
      <span class="upload-zone__primary">正在上传 ${esc(file.name)}…</span>
    </div>`;

  const fd = new FormData();
  fd.append('file', file);

  try {
    const resp = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '上传失败');

    state.sessionId = data.session_id;
    state.viewMode = 'session';
    state.historyId = null;
    clearPlanInput();
    currentContextFileSignature = uploadSignature;
    const draft = loadContextDraft();
    const restoreContext = preserveContextDraftOnNextUpload ||
      (draft && draft.fileSignature && draft.fileSignature === uploadSignature);
    if (restoreContext) {
      writeContextForm(draft.fields || {});
      preserveContextDraftOnNextUpload = false;
    } else {
      clearContextDraft();
      clearContextForm();
    }
    state.sessionReport = {
      reportMd: null,
      title: '',
      reportNo: '',
      qaHtml: '',
      qaMessages: [],
      feishuLinkHtml: '',
      running: false,
      stream: '',
    };
    renderUploadedFileState(data.filename);
    renderPreview(data);
    goStep(2);
    showToast(`成功读取 ${data.total_rows} 行数据`, 'success');
    loadColumns();
  } catch (e) {
    showToast(`上传失败：${e.message}`, 'error');
    resetUploadZone();
  }
}

function renderUploadedFileState(filename) {
  state.uploadedFilename = String(filename || '').trim();
  fileInput.disabled = true;
  uploadZone.classList.remove('drag-over');
  uploadZone.classList.add('upload-zone--readonly');
  uploadZone.setAttribute('aria-disabled', 'true');
  uploadZone.innerHTML = `
    <div class="upload-zone__icon upload-zone__icon--complete">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
        <polyline points="14 2 14 8 20 8"/>
        <polyline points="9 15 11 17 15 13"/>
      </svg>
    </div>
    <div class="upload-zone__text">
      <span class="upload-zone__primary">已上传文件：${esc(state.uploadedFilename || '未记录文件名')}</span>
      <span class="upload-zone__secondary">当前流程已开始，回看时不可重新上传</span>
    </div>`;
}

function resetUploadZone() {
  state.uploadedFilename = '';
  fileInput.disabled = false;
  uploadZone.classList.remove('upload-zone--readonly', 'drag-over');
  uploadZone.removeAttribute('aria-disabled');
  uploadZone.innerHTML = `
    <div class="upload-zone__icon">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
        <polyline points="17 8 12 3 7 8"/>
        <line x1="12" y1="3" x2="12" y2="15"/>
      </svg>
    </div>
    <div class="upload-zone__text">
      <span class="upload-zone__primary">拖放文件到这里，或点击选择</span>
      <span class="upload-zone__secondary">支持 CSV / Excel（最大 50MB）</span>
    </div>`;
}

function renderPreview(data) {
  $('preview-meta').textContent =
    `${data.filename} · 已读取 ${data.total_rows} 行数据 · ${data.headers.length} 列`;
}

// ============================================================
// STEP 2: 题型确认
// ============================================================

async function loadColumns() {
  const list = $('col-list');
  $('col-confirm-count').textContent = '';
  list.innerHTML = `<div class="thinking-block"><div class="thinking-block__icon"><div class="spinner"></div></div>
    <div class="thinking-block__content"><div class="thinking-block__title">AI 正在识别题型与中文题名（较慢，请稍候）…</div>
    <div class="thinking-block__stream" id="col-stream-text"></div></div></div>`;
  $('btn-start-plan').disabled = true;

  try {
    await consumeSSE(`/api/columns/${state.sessionId}`, ev => {
      if (ev.type === 'chunk') {
        const el = $('col-stream-text');
        if (el) { el.textContent += ev.content; el.scrollTop = el.scrollHeight; }
      }
      if (ev.type === 'columns_ready') {
        state.columns = ev.columns;
        renderColumnRows(ev.columns);
      }
    });
    $('btn-start-plan').disabled = false;
  } catch (e) {
    list.innerHTML = `<div class="hist-empty">题型识别失败：${esc(e.message)}</div>`;
    showToast(e.message, 'error');
  }
}

function renderColumnRows(columns) {
  $('col-confirm-count').textContent = `共 ${columns.length} 道题`;
  $('col-list').innerHTML = columns.map((c, i) => columnRowHTML(c, i)).join('');
  columns.forEach((c, i) => updateExtra(i, c.role));
  refreshContextFormVisibility();
}

// ── 可选业务上下文（本地草稿自动暂存 + 提交）────────────────────

const CONTEXT_DRAFT_KEY = 'survey_context_draft';
const CONTEXT_FIELD_IDS = {
  problem: 'ctx-problem',
  background: 'ctx-background',
  target_users: 'ctx-target-users',
  key_concerns: 'ctx-key-concerns',
  report_usage: 'ctx-report-usage',
};
let currentContextFileSignature = '';

function contextFileSignature(file) {
  if (!file) return '';
  return `${file.name || ''}|${file.size || 0}|${file.lastModified || 0}`;
}

function readContextForm() {
  const out = {};
  for (const [key, id] of Object.entries(CONTEXT_FIELD_IDS)) {
    const el = $(id);
    out[key] = el ? el.value.trim() : '';
  }
  return out;
}

function writeContextForm(data) {
  if (!data) return;
  for (const [key, id] of Object.entries(CONTEXT_FIELD_IDS)) {
    const el = $(id);
    if (el && data[key] != null) el.value = data[key];
  }
}

function loadContextDraft() {
  try {
    const raw = localStorage.getItem(CONTEXT_DRAFT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && parsed.fields) return parsed;
    return { fileSignature: '', fields: parsed || {} };
  } catch (e) {
    return null;
  }
}

function saveContextDraft() {
  try {
    localStorage.setItem(CONTEXT_DRAFT_KEY, JSON.stringify({
      fileSignature: currentContextFileSignature,
      fields: readContextForm(),
    }));
  } catch (e) { /* localStorage 不可用时静默忽略 */ }
}

function loadContextDraftForCurrentFile() {
  const draft = loadContextDraft();
  if (!draft || !draft.fileSignature || draft.fileSignature !== currentContextFileSignature) return null;
  return draft.fields || {};
}

function clearContextDraft() {
  try { localStorage.removeItem(CONTEXT_DRAFT_KEY); } catch (e) { /* 忽略 */ }
}

let contextDraftSaveTimer = null;
let preserveContextDraftOnNextUpload = false;
function scheduleContextDraftSave() {
  clearTimeout(contextDraftSaveTimer);
  contextDraftSaveTimer = setTimeout(saveContextDraft, 400);
}

function clearContextForm() {
  for (const id of Object.values(CONTEXT_FIELD_IDS)) {
    const el = $(id);
    if (el) el.value = '';
  }
}

function showBlockingFlowError(title, message) {
  const existing = $('blocking-flow-error-modal');
  if (existing) existing.remove();
  const modal = document.createElement('div');
  modal.id = 'blocking-flow-error-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:260;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.48);backdrop-filter:blur(4px);';
  modal.innerHTML = `
    <div style="width:min(520px,92vw);background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:22px;box-shadow:var(--shadow-lg);">
      <div style="font-size:18px;font-weight:700;margin-bottom:10px;color:var(--red);">${esc(title || '流程失败')}</div>
      <div style="font-size:14px;line-height:1.7;color:var(--text-2);white-space:pre-wrap;word-break:break-word;">${esc(message || '服务端处理失败，请重新开始后再试。')}</div>
      <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:22px;">
        <button class="btn btn--ghost" id="blocking-error-stay" type="button">留在当前页</button>
        <button class="btn btn--primary" id="blocking-error-restart" type="button">重新开始</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  const close = () => modal.remove();
  $('blocking-error-stay').onclick = close;
  $('blocking-error-restart').onclick = () => {
    saveContextDraft();
    close();
    const restart = $('btn-restart');
    if (restart) restart.click();
  };
  modal.addEventListener('click', e => {
    if (e.target === modal) close();
  });
}

(function initContextForm() {
  Object.values(CONTEXT_FIELD_IDS).forEach(id => {
    const el = $(id);
    if (el) el.addEventListener('input', scheduleContextDraftSave);
  });
})();

function refreshContextFormVisibility() {
  const wrap = $('context-form-wrap');
  if (!wrap) return;
  if (state.mode === 'crosstab') {
    wrap.style.display = 'none';
    return;
  }
  wrap.style.display = '';
  const draft = loadContextDraftForCurrentFile();
  if (draft) writeContextForm(draft);
}

function optionEditorHTML(i, c) {
  const options = [...(c.options || [])];
  (c.unmatched_values || [])
    .filter(item => item?.suggested_handling === 'standard_option')
    .forEach(item => {
      const value = String(item?.value || '').trim();
      if (value && !options.some(option => option.trim().toLocaleLowerCase() === value.toLocaleLowerCase())) {
        options.push(value);
      }
    });
  const aliases = c.value_aliases || {};
  const aliasGroups = Object.entries(aliases).filter(([canon, values]) =>
    Array.isArray(values) && values.some(v => String(v).trim() && String(v).trim() !== canon)
  ).length;
  const chips = options.map(opt => `<span class="option-summary-chip" title="${esc(opt)}">${esc(opt)}</span>`).join('');
  const more = '';
  const mergeBadge = aliasGroups ? `<span class="option-merge-badge">已合并 ${aliasGroups} 组</span>` : '';
  const rows = options.map(opt => `
    <div class="option-edit-row">
      <div class="option-edit-row__main">
        <textarea class="extra-input option-input" data-option="${i}" rows="2" placeholder="选项内容">${esc(opt)}</textarea>
        <div class="option-alias-hint">${(aliases[opt] || []).map(a =>
    `<span class="alias-chip"><span class="alias-chip__text" title="${esc(a)}">${esc(a)}</span><button class="alias-chip__remove" data-alias-col="${i}" data-alias-canon="${esc(opt)}" data-alias-val="${esc(a)}" title="移除此别名" type="button">×</button></span>`
  ).join('')}<button class="alias-add-btn" data-alias-add-col="${i}" data-alias-add-canon="${esc(opt)}" title="添加别名" type="button">+</button></div>
      </div>
      <button class="btn-icon option-remove" data-option-remove="${i}" title="删除选项" type="button">×</button>
    </div>
  `).join('');
  return `<div class="option-editor" data-option-editor="${i}">
    <details class="option-editor__details" ${c.low_confidence ? 'open' : ''}>
      <summary class="option-editor__summary">
        <span class="option-editor__summary-main">${chips || '<span class="option-summary-empty">暂无选项</span>'}${more}</span>
        <span class="option-editor__summary-actions">${mergeBadge}<span class="option-edit-link">编辑</span></span>
      </summary>
      <div class="option-editor__body">
        <div class="option-editor__head">
          <span>标准选项</span>
          <button class="btn btn--ghost btn--sm option-add" data-option-add="${i}" type="button">添加选项</button>
        </div>
        <div class="option-editor__rows">${rows}</div>
      </div>
    </details>
  </div>`;
}

function otherTextHTML(i, c) {
  const meta = c.other_text || {};
  if (!meta) return '';
  const examples = Array.isArray(meta.examples) ? meta.examples.slice(0, 5) : [];
  const count = Number(meta.count || examples.length || 0);
  const option = meta.option || 'Other / 其他';
  const exampleHTML = examples.length
    ? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;">${examples.map(v =>
        `<span class="option-summary-chip" title="${esc(v)}">${esc(v)}</span>`
      ).join('')}</div>`
    : '';
  return `<div class="option-editor" data-other-text="${i}">
    <div style="border:1px dashed var(--border);border-radius:8px;padding:10px 12px;background:rgba(255,255,255,.54);">
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text-2);">
        <input type="checkbox" data-other-text-enabled="${i}" ${meta.enabled !== false ? 'checked' : ''} />
        <span>其他填空补充：检测到 ${count} 条；统计时计入「${esc(option)}」，报告中作为本题补充反馈</span>
      </label>
      ${exampleHTML}
    </div>
  </div>`;
}

function unmatchedValuesHTML(i, c) {
  const values = Array.isArray(c.unmatched_values)
    ? c.unmatched_values.filter(item => item?.suggested_handling !== 'standard_option')
    : [];
  if (!values.length) return '';
  const totalCount = values.reduce((sum, item) => sum + Number(item?.count || 0), 0);
  const previewRows = values.map(item => `<div class="unmatched-value-row">
    <span class="unmatched-value-row__text">${esc(item.value)}</span>
    <span class="unmatched-value-row__count">${Number(item.count || 0)} 条</span>
  </div>`).join('');
  return `<div class="option-editor" data-unmatched-values="${i}">
    <div style="border:1px dashed var(--border);border-radius:8px;padding:10px 12px;background:rgba(255,255,255,.54);">
      <div style="font-size:13px;font-weight:600;color:var(--text);margin-bottom:6px;">未匹配内容</div>
      <div class="unmatched-help">请先确认上方标准选项。与标准选项或别名匹配的内容会自动按选项统计，剩余内容再按下方方式统一处理。</div>
      <label class="unmatched-bulk-row">
        <span>批量处理</span>
        <select class="extra-input unmatched-handling-select" data-unmatched-handling="${i}">
          <option value="as_other" selected>剩余内容按 Other 填空处理</option>
          <option value="keep_raw">剩余内容保留原值统计</option>
        </select>
      </label>
      <details class="unmatched-preview" open>
        <summary>逐条预览：${values.length} 种未匹配内容，共 ${totalCount} 条</summary>
        <div class="unmatched-value-list">${previewRows}</div>
      </details>
    </div>
  </div>`;
}

function autosizeOptionTextareas(root = document) {
  root.querySelectorAll('textarea.option-input').forEach(el => {
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  });
}

function matrixEditorHTML(i, c) {
  const colIndexes = c.column_indexes || [];
  const rows = c.rows || [];
  const rowCount = Math.max(colIndexes.length, rows.length);
  const summaryRows = rows.length ? rows : colIndexes.map((_, idx) => `子项${idx + 1}`);
  const chips = summaryRows.slice(0, 5).map(row => `<span class="matrix-summary-chip">${esc(row)}</span>`).join('');
  const more = summaryRows.length > 5 ? `<span class="matrix-summary-more">+${summaryRows.length - 5}</span>` : '';
  const bodyRows = Array.from({ length: rowCount }).map((_, idx) => {
    const colNo = colIndexes[idx] != null ? `列 ${colIndexes[idx]}` : `子项 ${idx + 1}`;
    const value = rows[idx] || '';
    return `<div class="matrix-edit-row">
      <span class="matrix-edit-row__col">${esc(colNo)}</span>
      <input class="extra-input matrix-row-input" data-matrix-row="${i}" data-matrix-row-idx="${idx}" value="${esc(value)}" placeholder="子项名称" />
    </div>`;
  }).join('');

  return `<div class="matrix-editor" data-matrix-editor="${i}">
    <details class="matrix-editor__details" ${c.low_confidence ? 'open' : ''}>
      <summary class="matrix-editor__summary">
        <span class="matrix-editor__summary-main">${chips || '<span class="matrix-summary-empty">暂无子项</span>'}${more}</span>
        <span class="matrix-edit-link">编辑子项</span>
      </summary>
      <div class="matrix-editor__body">
        <div class="matrix-editor__head">矩阵子项</div>
        <div class="matrix-editor__rows">${bodyRows}</div>
      </div>
    </details>
  </div>`;
}

function collectOptionsForColumn(i) {
  const seen = new Set();
  const values = [];
  document.querySelectorAll(`.option-input[data-option="${i}"]`).forEach(input => {
    const v = input.value.trim();
    const key = v.toLocaleLowerCase();
    if (v && !seen.has(key)) {
      seen.add(key);
      values.push(v);
    }
  });
  return values;
}

function collectMatrixRowsForColumn(i, c) {
  const inputs = Array.from(document.querySelectorAll(`.matrix-row-input[data-matrix-row="${i}"]`));
  if (!inputs.length) return c.rows || [];

  return inputs.map((input, idx) => {
    const value = input.value.trim();
    return value || (c.rows || [])[idx] || `子项${idx + 1}`;
  });
}

function buildEditedOptionAliases(c, editedOptions) {
  const aliases = { ...(c.value_aliases || {}) };
  const original = c.options_original || c.options || [];
  const editedSet = new Set(editedOptions);
  const norm = s => String(s || '').trim().toLocaleLowerCase();

  // 只有长度相同时才做位置匹配推断重命名（纯重命名场景）
  // 长度变了说明有增删，保守处理：只按名字保留既有 alias，不推断重命名，避免位移误归并
  if (original.length === editedOptions.length) {
    original.forEach((oldValue, idx) => {
      const newValue = editedOptions[idx];
      if (!oldValue || !newValue || norm(oldValue) === norm(newValue)) return;
      aliases[newValue] = [...new Set([...(aliases[newValue] || []), oldValue, ...(aliases[oldValue] || [])])];
      delete aliases[oldValue];
    });
  }

  Object.keys(aliases).forEach(k => {
    if (!editedSet.has(k)) delete aliases[k];
  });
  return aliases;
}

function columnRowHTML(c, i) {
  const opts = ROLE_OPTIONS.map(([val, label]) =>
    `<option value="${val}" ${val === c.role ? 'selected' : ''}>${label}</option>`
  ).join('');
  const name = c.name_zh || c.name || `列${(c.column_indexes || [])[0] ?? i}`;
  const isMatrix = MATRIX_ROLES.includes(c.role) || (c.column_indexes || []).length > 1;
  const matrixTag = isMatrix ? `<span class="col-row__tag">矩阵 · ${(c.column_indexes || []).length} 列</span>` : '';
  const roleClass = c.role ? ` col-row--role-${c.role}` : '';
  const lowConfClass = '';  // 不改背景色，跟随题型颜色
  const lowConfBadge = c.low_confidence
    ? `<div class="col-row__low-conf-badge"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>AI 判断信心低，请人工确认</div>`
    : '';

  return `<div class="col-row${roleClass}${lowConfClass}" data-card="${i}">
    <span class="col-row__num">${i + 1}</span>
    <div class="col-row__main">
      <div class="col-row__name" title="${esc(name)}">${esc(name)}${matrixTag}</div>
      ${lowConfBadge}
      <div class="q-extra" data-extra="${i}"></div>
    </div>
    <select class="type-select col-row__select" data-card="${i}">${opts}</select>
  </div>`;
}

function updateExtra(i, role) {
  const box = document.querySelector(`[data-extra="${i}"]`);
  if (!box) return;
  const c = state.columns[i] || {};
  const bits = [];

  if (MATRIX_ROLES.includes(role)) {
    bits.push(matrixEditorHTML(i, c));
  }

  if (role === 'multi_choice') {
    const delim = c.delimiter || '，';
    bits.push(`<span class="q-extra-inline">分隔符
      <input class="extra-input extra-input--sm" data-delim="${i}" value="${esc(delim)}" placeholder="，" /></span>`);
  }

  if (CHOICE_ROLES.includes(role)) {
    bits.push(optionEditorHTML(i, c));
    if ((role === 'single_choice' || role === 'multi_choice') && c.unmatched_values?.length) {
      bits.push(unmatchedValuesHTML(i, c));
    } else if ((role === 'single_choice' || role === 'multi_choice') && c.other_text) {
      bits.push(otherTextHTML(i, c));
    }
  } else if (role === 'scale' || role === 'matrix_scale') {
    const mn = (c.scale_min ?? 1), mx = (c.scale_max ?? 5);
    bits.push(`<span class="q-extra-inline">量程
      <input class="extra-input extra-input--sm" type="number" data-smin="${i}" value="${mn}" />
      <span class="scale-sep">-</span>
      <input class="extra-input extra-input--sm" type="number" data-smax="${i}" value="${mx}" /></span>`);
  }

  box.innerHTML = bits.join('');
  box.style.display = bits.length ? 'flex' : 'none';
  autosizeOptionTextareas(box);
}

$('col-list').addEventListener('change', e => {
  const sel = e.target.closest('.type-select');
  if (sel) {
    const i = +sel.dataset.card;
    const newRole = sel.value;
    state.columns[i].role = newRole;
    updateExtra(i, newRole);
    // 同步更新颜色类
    const row = document.querySelector(`.col-row[data-card="${i}"]`);
    if (row) {
      row.className = row.className.replace(/\bcol-row--role-\S+/g, '').trim();
      if (newRole) row.classList.add(`col-row--role-${newRole}`);
    }
  }

});

$('col-list').addEventListener('input', e => {
  const textarea = e.target.closest('textarea.option-input');
  if (textarea) {
    autosizeOptionTextareas(textarea.closest('.option-edit-row') || document);
  }
});

$('col-list').addEventListener('click', e => {
  const addBtn = e.target.closest('[data-option-add]');
  if (addBtn) {
    const i = +addBtn.dataset.optionAdd;
    const rows = document.querySelector(`[data-option-editor="${i}"] .option-editor__rows`);
    if (rows) {
      rows.insertAdjacentHTML('beforeend', `
        <div class="option-edit-row">
          <div class="option-edit-row__main">
            <textarea class="extra-input option-input" data-option="${i}" rows="2" placeholder="选项内容"></textarea>
            <div class="option-alias-hint"><button class="alias-add-btn" data-alias-add-col="${i}" data-alias-add-canon="" title="添加别名" type="button">+</button></div>
          </div>
          <button class="btn-icon option-remove" data-option-remove="${i}" title="删除选项" type="button">×</button>
        </div>
      `);
      autosizeOptionTextareas(rows);
      rows.querySelector('.option-edit-row:last-child .option-input')?.focus();
    }
  }

  const removeBtn = e.target.closest('[data-option-remove]');
  if (removeBtn) {
    removeBtn.closest('.option-edit-row')?.remove();
  }

  const aliasRemoveBtn = e.target.closest('.alias-chip__remove');
  if (aliasRemoveBtn) {
    const colIdx = +aliasRemoveBtn.dataset.aliasCol;
    const canon = aliasRemoveBtn.dataset.aliasCanon;
    const val = aliasRemoveBtn.dataset.aliasVal;
    const col = state.columns[colIdx];
    if (col?.value_aliases?.[canon]) {
      col.value_aliases[canon] = col.value_aliases[canon].filter(a => a !== val);
      if (!col.value_aliases[canon].length) delete col.value_aliases[canon];
    }
    aliasRemoveBtn.closest('.alias-chip')?.remove();
  }

  const aliasAddBtn = e.target.closest('.alias-add-btn');
  if (aliasAddBtn) {
    const colIdx = +aliasAddBtn.dataset.aliasAddCol;
    // 新增选项行 canon 为空时，从同行 input 读取当前值
    let canon = aliasAddBtn.dataset.aliasAddCanon;
    if (!canon) {
      canon = aliasAddBtn.closest('.option-edit-row')?.querySelector('.option-input')?.value?.trim() || '';
    }
    if (!canon) return;
    const input = document.createElement('input');
    input.className = 'alias-add-input';
    input.placeholder = '输入别名，回车确认';
    aliasAddBtn.replaceWith(input);
    input.focus();

    function commitAlias() {
      const val = input.value.trim();
      const col = state.columns[colIdx];
      if (val && col) {
        if (!col.value_aliases) col.value_aliases = {};
        if (!col.value_aliases[canon]) col.value_aliases[canon] = [];
        if (!col.value_aliases[canon].includes(val)) {
          // 全局唯一化：从其他 canon 的 alias list 和 DOM 中移除相同值，避免同一原始值被两个标准选项覆盖映射
          if (col.value_aliases) {
            Object.keys(col.value_aliases).forEach(k => {
              if (k === canon) return;
              col.value_aliases[k] = col.value_aliases[k].filter(a => a !== val);
              if (!col.value_aliases[k].length) delete col.value_aliases[k];
            });
          }
          document.querySelectorAll(`.alias-chip__remove[data-alias-col="${colIdx}"]`).forEach(btn => {
            if (btn.dataset.aliasCanon !== canon && btn.dataset.aliasVal === val) {
              btn.closest('.alias-chip')?.remove();
            }
          });
          col.value_aliases[canon].push(val);
          const chip = document.createElement('span');
          chip.className = 'alias-chip';
          chip.innerHTML = `<span class="alias-chip__text" title="${esc(val)}">${esc(val)}</span><button class="alias-chip__remove" data-alias-col="${colIdx}" data-alias-canon="${esc(canon)}" data-alias-val="${esc(val)}" title="移除此别名" type="button">×</button>`;
          input.parentNode.insertBefore(chip, input);
        }
      }
      const newBtn = document.createElement('button');
      newBtn.className = 'alias-add-btn';
      newBtn.dataset.aliasAddCol = colIdx;
      newBtn.dataset.aliasAddCanon = canon;
      newBtn.title = '添加别名';
      newBtn.type = 'button';
      newBtn.textContent = '+';
      input.replaceWith(newBtn);
    }

    input.addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); commitAlias(); } });
    input.addEventListener('blur', commitAlias);
  }
});

function collectConfirmedColumns() {
  return state.columns.map((c, i) => {
    const role = (document.querySelector(`.type-select[data-card="${i}"]`) || {}).value || c.role;
    const out = {
      name_zh: c.name_zh || c.name || '',
      role,
      column_indexes: c.column_indexes || (c.index != null ? [c.index] : []),
    };

    if (role === 'multi_choice') {
      const el = document.querySelector(`[data-delim="${i}"]`);
      out.delimiter = el ? el.value : (c.delimiter || '，');
    }

    const otherEnabledEl = document.querySelector(`[data-other-text-enabled="${i}"]`);
    const otherTextEnabled = otherEnabledEl
      ? otherEnabledEl.checked
      : c.other_text?.enabled !== false;

    if (CHOICE_ROLES.includes(role)) {
      let options = collectOptionsForColumn(i);
      let optionsOriginal = [...(c.options_original || c.options || [])];
      const unmatchedValues = Array.isArray(c.unmatched_values) ? c.unmatched_values : [];
      const addOption = value => {
        const text = String(value || '').trim();
        if (!text) return;
        const exists = options.some(option => option.trim().toLocaleLowerCase() === text.toLocaleLowerCase());
        if (!exists) options.push(text);
        const originalExists = optionsOriginal.some(option => String(option || '').trim().toLocaleLowerCase() === text.toLocaleLowerCase());
        if (!originalExists) optionsOriginal.push(text);
      };
      const reviewResiduals = unmatchedValues.filter(item => item?.suggested_handling !== 'standard_option');
      if (reviewResiduals.length) {
        const handlingEl = document.querySelector(`[data-unmatched-handling="${i}"]`);
        const handling = handlingEl?.value || 'as_other';
        if (handling === 'as_other') {
          const otherOption = 'Other / 其他';
          addOption(otherOption);
          out.other_text = {
            enabled: true,
            option: otherOption,
            count: reviewResiduals.reduce((sum, item) => sum + Number(item.count || 0), 0),
            examples: reviewResiduals.slice(0, 5).map(item => item.value),
          };
        }
      }
      // 关闭 Other 填空时移除系统补入的 Other 选项，避免后续统计仍把它
      // 当作该题的标准选项；原始未识别值会按其真实值保留。
      if (c.other_text && !otherTextEnabled) {
        const otherOption = String(c.other_text.option || 'Other / 其他').trim().toLocaleLowerCase();
        options = options.filter(option => option.trim().toLocaleLowerCase() !== otherOption);
      }
      if (options.length) {
        out.options = options;
        out.options_original = optionsOriginal;
        const aliases = buildEditedOptionAliases(c, options);
        if (Object.keys(aliases).length) out.value_aliases = aliases;
      }
    }

    if (role === 'matrix_multi' && c.delimiter) out.delimiter = c.delimiter;

    if (role === 'scale' || role === 'matrix_scale') {
      const mnEl = document.querySelector(`[data-smin="${i}"]`);
      const mxEl = document.querySelector(`[data-smax="${i}"]`);
      out.scale_min = mnEl ? Number(mnEl.value) : (c.scale_min ?? 1);
      out.scale_max = mxEl ? Number(mxEl.value) : (c.scale_max ?? 5);
    }
    if (MATRIX_ROLES.includes(role)) {
      const rows = collectMatrixRowsForColumn(i, c);
      if (rows.length) out.rows = rows;
    }
    if (!out.value_aliases && c.value_aliases && CHOICE_ROLES.includes(role)) {
      out.value_aliases = c.value_aliases;
    }
    if (
      (role === 'single_choice' || role === 'multi_choice')
      && !out.other_text
      && c.other_text
      && !c.unmatched_values?.length
    ) {
      out.other_text = {
        ...c.other_text,
        enabled: otherTextEnabled,
      };
    }
    return out;
  });
}

$('btn-start-plan').addEventListener('click', startPlan);

async function startPlan() {
  const btn = $('btn-start-plan');
  if (btn) btn.disabled = true;
  clearPlanInput();

  // 跑数表模式:列已在上传时确定性建好,跳过题型确认;其余模式先存用户确认的题型
  if (state.mode !== 'crosstab') {
    try {
      const columns = collectConfirmedColumns();
      const resp = await fetch(`/api/columns/${state.sessionId}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ columns }),
      });
      if (!resp.ok) {
        const d = await resp.json();
        throw new Error(d.detail || '保存题型失败');
      }
    } catch (e) {
      showToast(`保存题型失败：${e.message}`, 'error');
      if (btn) btn.disabled = false;
      return;
    }

    try {
      const ctx = readContextForm();
      state.contextForm = ctx;
      const ctxResp = await fetch(`/api/survey-context/${state.sessionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(ctx),
      });
      if (!ctxResp.ok) {
        if (ctxResp.status === 404) {
          saveContextDraft();
          preserveContextDraftOnNextUpload = true;
          showToast(
            '当前会话已过期，请点击上方步骤条的"上传数据"重新上传文件；已填写的补充分析目标不会丢失',
            'error', 6000,
          );
        } else {
          showToast('保存补充分析目标失败，请重试', 'error');
        }
        if (btn) btn.disabled = false;
        return;
      }
    } catch (e) {
      showToast(`保存补充分析目标失败：${e.message}`, 'error');
      if (btn) btn.disabled = false;
      return;
    }
  }

  // 进入 Step 3，开始 AI 规划
  goStep(3);
  $('plan-thinking').style.display = 'flex';
  $('plan-thinking').querySelector('.thinking-block__title').textContent =
    state.mode === 'crosstab' ? 'AI 正在阅读问卷、规划报告章节，请稍候…' : 'AI 正在规划分析方案，请稍候…';
  $('plan-card').style.display = 'none';
  $('plan-stream-text').textContent = '';

  try {
    await consumeSSE(`/api/plan/${state.sessionId}`, ev => {
      if (ev.type === 'chunk') {
        const el = $('plan-stream-text');
        el.textContent += ev.content;
        el.scrollTop = el.scrollHeight;
      }
      if (ev.type === 'plan_ready') {
        state.planData = ev.plan;
        showPlanCard(ev.plan, ev.headers);
      }
    });
  } catch (e) {
    $('plan-thinking').style.display = 'none';
    showBlockingFlowError('方案生成失败', e.message);
    btn.disabled = false;
  }
}

// ============================================================
// STEP 3: Plan card
// ============================================================

function showPlanCard(plan, headers) {
  $('plan-thinking').style.display = 'none';
  $('plan-card').style.display = 'block';
  $('plan-card-content').innerHTML = buildPlanHTML(plan, headers);
  clearPlanInput();
  // 确保按钮状态与输入框同步（修订后面板可能处于 disabled 残留）
  $('btn-plan-ok').disabled = false;
  $('btn-plan-revise').disabled = true;
}

function buildPlanHTML(plan, headers) {
  let html = '';

  const colMap = {};
  for (const c of plan.columns) colMap[c.index] = c;
  const columnDisplayName = idx => {
    const c = colMap[idx];
    const candidates = [c?.name_zh, c?.name, headers && headers[idx]];
    return candidates
      .map(value => String(value || '').trim())
      .find(value => value && !/^(?:列|column|col)\s*\d+$/i.test(value)) || '';
  };
  const humanizePlanText = text => String(text || '')
    .replace(/列\s*(\d+)/g, (_, rawIdx) => {
      const name = columnDisplayName(Number(rawIdx));
      return name ? `「${name}」` : '相关题目';
    })
    .replace(/\b(?:column|col|index)\s*[:#]?\s*(\d+)/gi, (_, rawIdx) => {
      const name = columnDisplayName(Number(rawIdx));
      return name ? `「${name}」` : '相关题目';
    });

  const branchRules = Array.isArray(plan.branch_rules) ? plan.branch_rules : [];
  const cross = Array.isArray(plan.cross_tabs) ? plan.cross_tabs : [];
  const rulesByParent = new Map();
  const ruleByTargetIndex = new Map();
  branchRules.forEach(rule => {
    if (!rulesByParent.has(rule.parent_index)) rulesByParent.set(rule.parent_index, []);
    rulesByParent.get(rule.parent_index).push(rule);
    (rule.targets || []).forEach(target => {
      (target.indexes || []).forEach(idx => ruleByTargetIndex.set(idx, { rule, target }));
    });
  });

  const rolePresentation = role => ({
    profile_dim: ['画像题', '统计各选项人数与占比'],
    single_choice: ['单选题', '统计各选项人数与占比'],
    multi_choice: ['多选题', '统计各选项选择人数与占比'],
    scale: ['量表题', '分析评分分布与集中趋势'],
    open_text: ['开放题', '归纳主要主题、原因与体验反馈'],
    matrix_scale: ['矩阵量表', '按矩阵子项比较评分表现'],
    matrix_multi: ['矩阵多选', '按矩阵子项比较选择分布'],
  }[role] || ['分析题', '结合本题有效回答进行分析']);

  const logicalIndexesFor = (idx, partSet) => {
    const col = colMap[idx];
    if (!col || !['matrix_scale', 'matrix_multi'].includes(col.role) || !col.matrix_group) return [idx];
    return [...partSet].filter(otherIdx => {
      const other = colMap[otherIdx];
      return other?.role === col.role && other?.matrix_group === col.matrix_group;
    });
  };

  const logicalQuestionCount = indexes => {
    const keys = new Set();
    indexes.forEach(idx => {
      const col = colMap[idx];
      const key = col?.matrix_group && ['matrix_scale', 'matrix_multi'].includes(col.role)
        ? `${col.role}:${col.matrix_group}`
        : `column:${idx}`;
      keys.add(key);
    });
    return keys.size;
  };

  const renderQuestion = (idx, number, partSet, visited, nested = false) => {
    if (visited.has(idx) || !partSet.has(idx)) return '';
    const col = colMap[idx] || {};
    const logicalIndexes = logicalIndexesFor(idx, partSet);
    logicalIndexes.forEach(itemIdx => visited.add(itemIdx));
    const name = col.matrix_group || columnDisplayName(idx) || '未命名题目';
    const [roleLabel, method] = rolePresentation(col.role);
    const applicability = ruleByTargetIndex.get(idx);
    let itemHTML = `<div class="plan-outline__question${nested ? ' plan-outline__question--nested' : ''}">
      <div class="plan-outline__question-head">
        <span class="plan-outline__number">${esc(number)}</span>
        <span class="plan-outline__question-name">${esc(name)}</span>
        <span class="plan-outline__role">${esc(roleLabel)}</span>
      </div>
      <div class="plan-outline__method">${esc(method)}</div>`;

    if (applicability && !nested) {
      const { rule, target } = applicability;
      const options = (rule.allowed_options || []).map(option => `「${option}」`).join(' / ');
      const prefix = rule.confidence === 'medium' ? '疑似条件关系' : '适用范围';
      itemHTML += `<div class="plan-outline__applicability${rule.confidence === 'medium' ? ' is-medium' : ''}">
        <span>${esc(prefix)}</span>
        ${esc(`「${rule.parent_name || '前置题目'}」选择 ${options} · 进入 ${Number(rule.eligible_count || 0)} 人 · 本题 ${Number(target.answered_count || 0)} 条有效回答`)}
      </div>`;
    }

    const childRules = rulesByParent.get(idx) || [];
    if (childRules.length) {
      itemHTML += '<div class="plan-outline__branches">';
      let childCounter = 0;
      childRules.forEach(rule => {
        const options = (rule.allowed_options || []).map(option => `「${option}」`).join(' / ');
        const confidenceLabel = rule.confidence === 'medium' ? '疑似' : '已识别';
        const confidenceClass = rule.confidence === 'medium' ? ' is-medium' : '';
        itemHTML += `<div class="plan-outline__branch">
          <div class="plan-outline__branch-condition">
            <span class="plan-outline__confidence${confidenceClass}">${esc(confidenceLabel)}</span>
            <span>选择 ${esc(options)}</span>
            <span class="plan-outline__sample">进入该分支 ${Number(rule.eligible_count || 0)} 人</span>
          </div>
          <div class="plan-outline__branch-children">`;
        const externalTargets = [];
        (rule.targets || []).forEach(target => {
          const targetIdx = (target.indexes || []).find(targetIndex => partSet.has(targetIndex));
          if (targetIdx == null) {
            externalTargets.push(target.name || '后续题目');
            return;
          }
          childCounter += 1;
          itemHTML += renderQuestion(targetIdx, `${number}.${childCounter}`, partSet, visited, true);
        });
        if (externalTargets.length) {
          itemHTML += `<div class="plan-outline__external">其他章节继续分析：${esc(externalTargets.join('、'))}</div>`;
        }
        itemHTML += '</div></div>';
      });
      itemHTML += '</div>';
    }
    itemHTML += '</div>';
    return itemHTML;
  };

  // 1. 报告章节大纲
  html += `<div class="plan-section">
    <div class="plan-section__title">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
      报告分析大纲
    </div>
    <div class="plan-outline">`;

  for (let i = 0; i < plan.parts.length; i++) {
    const p = plan.parts[i];
    const indexes = Array.isArray(p.column_indexes) ? p.column_indexes : [];
    const partSet = new Set(indexes);
    const partBranchRules = branchRules.filter(rule =>
      partSet.has(rule.parent_index)
      || (rule.targets || []).some(target => (target.indexes || []).some(idx => partSet.has(idx)))
    );
    const summaryParts = [];
    if (indexes.length) summaryParts.push(`${logicalQuestionCount(indexes)} 道题`);
    if (partBranchRules.length) summaryParts.push(`${partBranchRules.length} 组条件关系`);
    if (!summaryParts.length && p.scope) summaryParts.push(p.scope);

    html += `<details class="plan-outline__part" open>
      <summary class="plan-outline__part-summary">
        <span class="plan-outline__part-num">Part ${i + 1}</span>
        <span class="plan-outline__part-title">${esc(p.name)}</span>
        <span class="plan-outline__part-meta">${esc(summaryParts.join(' · '))}</span>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
      </summary>
      <div class="plan-outline__part-body">`;

    if (!indexes.length) {
      html += `<div class="plan-outline__scope">${esc(p.scope || '按本章节主题进行综合分析')}</div>`;
    } else {
      const nestedIndexes = new Set();
      branchRules.forEach(rule => {
        if (!partSet.has(rule.parent_index)) return;
        (rule.targets || []).forEach(target => {
          (target.indexes || []).forEach(targetIdx => {
            if (partSet.has(targetIdx)) nestedIndexes.add(targetIdx);
          });
        });
      });
      const visited = new Set();
      let rootCounter = 0;
      indexes.forEach(idx => {
        if (nestedIndexes.has(idx) || visited.has(idx)) return;
        rootCounter += 1;
        html += renderQuestion(idx, `${i + 1}.${rootCounter}`, partSet, visited);
      });
      // 容错：若父题在其他 Part，仍展示未渲染的问题及其适用条件。
      indexes.forEach(idx => {
        if (visited.has(idx)) return;
        rootCounter += 1;
        html += renderQuestion(idx, `${i + 1}.${rootCounter}`, partSet, visited);
      });
    }

    const partCross = cross.filter(item => partSet.has(item.question_index));
    if (partCross.length) {
      html += `<div class="plan-outline__supplement">
        <div class="plan-outline__supplement-title">补充分析</div>
        <div class="plan-cross">`;
      partCross.forEach(item => {
        const profileName = columnDisplayName(item.profile_index) || '相关画像题目';
        const questionName = columnDisplayName(item.question_index) || '相关分析题目';
        html += `<div class="plan-cross-item">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          ${esc(profileName)} × ${esc(questionName)}
        </div>`;
      });
      html += '</div></div>';
    }
    html += '</div></details>';
  }
  html += `</div></div>`;

  // 2. 待确认的分析思路
  const openQs = plan.open_questions || [];
  if (openQs.length) {
    html += `<div class="plan-section">
      <div class="plan-section__title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        待确认的分析思路
      </div>
      <div class="plan-questions">`;
    openQs.forEach((q, i) => {
      html += `<div class="plan-question">
        <span class="plan-question__num">Q${i + 1}</span>
        <span>${esc(humanizePlanText(q))}</span>
      </div>`;
    });
    html += `</div></div>`;
  }

  return html;
}

// ── Plan confirm ──

function syncPlanActionButtons() {
  const hasText = !!($('plan-input').value || '').trim();
  $('btn-plan-ok').disabled = hasText;
  $('btn-plan-revise').disabled = !hasText;
}

function clearPlanInput() {
  const input = $('plan-input');
  if (!input) return;
  input.value = '';
  input.setAttribute('autocomplete', 'off');
  syncPlanActionButtons();
}

$('btn-plan-ok').addEventListener('click', () => {
  if (($('plan-input').value || '').trim()) return;
  confirmPlan('ok');
});
$('btn-plan-revise').addEventListener('click', () => {
  const txt = $('plan-input').value.trim();
  if (!txt) { showToast('请先输入修改意见', 'info'); return; }
  clearPlanInput();
  confirmPlan(txt);
});
$('plan-input').addEventListener('input', syncPlanActionButtons);
$('plan-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const txt = $('plan-input').value.trim();
    if (txt) {
      clearPlanInput();
      confirmPlan(txt);
    }
  }
});

async function confirmPlan(text) {
  $('btn-plan-ok').disabled = true;
  $('btn-plan-revise').disabled = true;

  try {
    let approved = false;
    let newPlan = null;
    let newHeaders = null;

    if (text.toLowerCase() === 'ok') {
      approved = true;
    } else {
      $('plan-thinking').style.display = 'flex';
      $('plan-thinking').querySelector('.thinking-block__title').textContent = 'AI 正在修订方案…';
      $('plan-stream-text').textContent = '';
      $('plan-card').style.display = 'none';

      await consumeSSEPost('/api/plan/confirm', {
        session_id: state.sessionId,
        user_text: text,
      }, ev => {
        if (ev.type === 'progress') {
          // 解析/重试状态——让用户知道后端还在工作
          const el = $('plan-stream-text');
          el.textContent = ev.message;
          _updateProgressStatus(ev.message);
        }
        if (ev.type === 'chunk') {
          const el = $('plan-stream-text');
          el.textContent += ev.content;
          el.scrollTop = el.scrollHeight;
        }
        if (ev.type === 'plan_ready') {
          newPlan = ev.plan;
          newHeaders = ev.headers;
        }
        if (ev.type === 'json' && ev.approved) {
          approved = true;
        }
      });

      if (newPlan) {
        state.planData = newPlan;
        showPlanCard(newPlan, newHeaders);
        showToast('方案已修订，请再次确认', 'success');
        return;
      }
    }

    if (approved) {
      await runStats();
    }
  } catch (e) {
    showBlockingFlowError('方案修订失败', e.message);
    // 修订失败时恢复方案卡片（隐藏 thinking 区，避免用户看到空白）
    if (state.planData) {
      $('plan-thinking').style.display = 'none';
      $('plan-card').style.display = 'block';
    }
    syncPlanActionButtons();
  }
}
