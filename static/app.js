/* ============================================================
   问卷洞察 Survey Insight — app.js (v2)
   前端状态机 + SSE + 题型确认 + 主题 + 抽屉(设置/历史)
   ============================================================ */

'use strict';

// ── 配置 marked ──
marked.setOptions({ breaks: true, gfm: true });

// ── 全局状态 ──
const state = {
  sessionId: null,
  currentStep: 1,
  viewStep: 1,        // 当前查看的步骤（可回看已完成步骤，不影响 currentStep）
  columns: null,      // Step 2 题型数据
  planData: null,
  reportMd: null,
  qaLoading: false,
  viewMode: 'session', // 'session' | 'history'
  historyId: null,     // 当前查看/续聊的历史 id
  sessionReport: {
    reportMd: null,
    title: '',
    reportNo: '',
    qaHtml: '',
    qaMessages: [],
    feishuLinkHtml: '',
    running: false,
    stream: '',
  },
  historyReport: {
    id: null,
    reportMd: null,
    title: '',
    reportNo: '',
    analystConvId: null,
    qaHtml: '',
    qaMessages: [],
    feishuLinkHtml: '',
    planData: null,
  },
};
window.__surveyState = state;

// ── 题型选项（与后端 ROLE_LABEL_MAP 对齐）──
const ROLE_OPTIONS = [
  ['id',            '用户 ID'],
  ['mlbbid',        'MLBB ID'],
  ['profile_dim',   '画像维度'],
  ['single_choice', '单选题'],
  ['multi_choice',  '多选题'],
  ['scale',         '量表题'],
  ['matrix_scale',  '矩阵打分'],
  ['matrix_multi',  '矩阵多选'],
  ['open_text',     '开放题'],
  ['ignore',        '忽略此列'],
];
const MATRIX_ROLES = ['matrix_scale', 'matrix_multi'];
const CHOICE_ROLES = ['single_choice', 'profile_dim', 'multi_choice', 'matrix_multi'];

// ── DOM 引用 ──
const $  = id => document.getElementById(id);
const panels   = [1,2,3,4,5].map(n => $(`panel-${n}`));

// ── 工具 ──

function showToast(msg, type = 'info', duration = 4000) {
  const tc = $('toast-container');
  const el = document.createElement('div');
  el.className = `toast toast--${type}`;
  el.textContent = msg;
  tc.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

function renderMarkdown(md) {
  return marked.parse(md || '');
}

function esc(str) {
  return String(str ?? '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function shortName(name, max = 20) {
  if (!name) return '';
  return name.length <= max ? name : name.slice(0, max - 1) + '…';
}

function renderSideNav() {
  // 已无二级步骤，只维护一级 nav-item active 状态
}

function renderStepBars() {
  // 更新所有问卷分析 step-bar 按钮状态
  document.querySelectorAll('[data-survey-step]').forEach(btn => {
    const n = +btn.dataset.surveyStep;
    btn.classList.remove('step-bar__item--active', 'step-bar__item--done');
    if (n === state.viewStep) {
      btn.classList.add('step-bar__item--active');
    } else if (n <= state.currentStep) {
      // currentStep 本身在回看其他步骤时也显示为 --done（可点击）
      btn.classList.add('step-bar__item--done');
    }
    btn.disabled = n > state.currentStep;
  });
}

function goStep(n) {
  state.currentStep = n;
  state.viewStep = n;
  panels.forEach((p, i) => {
    const showing = i + 1 === n;
    p.classList.toggle('panel--hidden', !showing);
    p.classList.remove('panel--readonly');
  });
  renderStepBars();
  document.querySelector('.main').scrollTo({ top: 0, behavior: 'smooth' });
}

function setViewStep(n) {
  if (n > state.currentStep) return;
  state.viewStep = n;
  panels.forEach((p, i) => {
    const showing = i + 1 === n;
    p.classList.toggle('panel--hidden', !showing);
    // 回看已完成步骤时加只读遮罩
    if (showing && n < state.currentStep) {
      p.classList.add('panel--readonly');
    } else {
      p.classList.remove('panel--readonly');
    }
  });
  renderStepBars();
  document.querySelector('.main').scrollTo({ top: 0, behavior: 'smooth' });
}

// ── 主题切换 ──

function applyTheme(theme) {
  if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light');
  else                   document.documentElement.removeAttribute('data-theme');
  try { localStorage.setItem('survey-theme', theme); } catch {}
}

(function initTheme() {
  let saved = 'light';
  try { saved = localStorage.getItem('survey-theme') || 'light'; } catch {}
  applyTheme(saved);
})();

$('btn-theme').addEventListener('click', () => {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  applyTheme(isLight ? 'dark' : 'light');
});

// ── SSE consumer (GET / EventSource) ──
function consumeSSE(url, onEvent) {
  return new Promise((resolve, reject) => {
    const es = new EventSource(url);
    es.onmessage = e => {
      try {
        const data = JSON.parse(e.data);
        onEvent(data);
        if (data.type === 'error') { es.close(); reject(new Error(data.message || data.msg || '服务端处理失败')); }
        if ([
          'columns_ready', 'plan_ready', 'report_done', 'qa_done',
          'ai_detect_done', 'quality_done',
        ].includes(data.type)) {
          es.close(); resolve(data);
        }
      } catch {}
    };
    es.onerror = () => { es.close(); reject(new Error('连接中断，请刷新重试')); };
  });
}

// ── SSE from POST (fetch + ReadableStream) ──
async function consumeSSEPost(url, body, onEvent) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (!resp.ok) {
    const text = await resp.text();
    let detail = text;
    try { detail = JSON.parse(text).detail || text; } catch {}
    throw new Error(detail);
  }

  const ct = resp.headers.get('Content-Type') || '';
  if (!ct.includes('text/event-stream')) {
    const data = await resp.json();
    onEvent({ type: 'json', ...data });
    return data;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data:')) continue;
      const raw = line.slice(5).trim();
      if (!raw) continue;
      try {
        const data = JSON.parse(raw);
        onEvent(data);
        if (data.type === 'error') throw new Error(data.message);
        if (['plan_ready', 'report_done', 'qa_done'].includes(data.type)) return data;
      } catch (parseErr) {
        if (parseErr.message !== 'JSON') throw parseErr;
      }
    }
  }
}

// ============================================================
// STEP 1: Upload
// ============================================================

const uploadZone = $('upload-zone');
const fileInput  = $('file-input');

uploadZone.addEventListener('click', () => fileInput.click());
uploadZone.addEventListener('dragover', e => { e.preventDefault(); uploadZone.classList.add('drag-over'); });
uploadZone.addEventListener('dragleave', ()  => uploadZone.classList.remove('drag-over'));
uploadZone.addEventListener('drop', e => {
  e.preventDefault();
  uploadZone.classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) handleUpload(file);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) handleUpload(fileInput.files[0]);
});

async function handleUpload(file) {
  const MAX = 50 * 1024 * 1024;
  if (file.size > MAX) { showToast('文件超过 50MB 上限', 'error'); return; }

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
    state.viewMode  = 'session';
    state.historyId = null;
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
    renderPreview(data);
    goStep(2);
    showToast(`成功读取 ${data.total_rows} 行数据`, 'success');
    loadColumns();
  } catch (e) {
    showToast(`上传失败：${e.message}`, 'error');
    resetUploadZone();
  }
}

function resetUploadZone() {
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
}

function optionEditorHTML(i, c) {
  const options = c.options || [];
  const aliases = c.value_aliases || {};
  const aliasGroups = Object.entries(aliases).filter(([canon, values]) =>
    Array.isArray(values) && values.some(v => String(v).trim() && String(v).trim() !== canon)
  ).length;
  const chips = options.slice(0, 6).map(opt => `<span class="option-summary-chip">${esc(opt)}</span>`).join('');
  const more = options.length > 6 ? `<span class="option-summary-more">+${options.length - 6}</span>` : '';
  const mergeBadge = aliasGroups ? `<span class="option-merge-badge">已合并 ${aliasGroups} 组</span>` : '';
  const rows = options.map(opt => `
    <div class="option-edit-row">
      <div class="option-edit-row__main">
        <input class="extra-input option-input" data-option="${i}" value="${esc(opt)}" placeholder="选项内容" />
        ${Array.isArray(aliases[opt]) && aliases[opt].length
          ? `<div class="option-alias-hint">${esc(aliases[opt].join(' / '))}</div>`
          : ''}
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

function buildEditedOptionAliases(c, editedOptions) {
  const aliases = { ...(c.value_aliases || {}) };
  const original = c.options_original || c.options || [];
  const editedSet = new Set(editedOptions);

  original.forEach((oldValue, idx) => {
    const newValue = editedOptions[idx];
    if (!oldValue || !newValue || oldValue === newValue) return;
    aliases[newValue] = [...new Set([...(aliases[newValue] || []), oldValue, ...((aliases[oldValue] || []))])];
    delete aliases[oldValue];
  });

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

  if (MATRIX_ROLES.includes(role) && (c.rows || []).length) {
    bits.push(`<span class="col-extra-readonly">子项：${esc(c.rows.join(' / '))}</span>`);
  }

  if (role === 'multi_choice') {
    const delim = c.delimiter || '，';
    bits.push(`<span class="q-extra-inline">分隔符
      <input class="extra-input extra-input--sm" data-delim="${i}" value="${esc(delim)}" placeholder="，" /></span>`);
  }

  if (CHOICE_ROLES.includes(role)) {
    bits.push(optionEditorHTML(i, c));
  } else if (role === 'scale' || role === 'matrix_scale') {
    const mn = (c.scale_min ?? 1), mx = (c.scale_max ?? 5);
    bits.push(`<span class="q-extra-inline">量程
      <input class="extra-input extra-input--sm" type="number" data-smin="${i}" value="${mn}" />
      <span class="scale-sep">-</span>
      <input class="extra-input extra-input--sm" type="number" data-smax="${i}" value="${mx}" /></span>`);
  }

  box.innerHTML = bits.join('');
  box.style.display = bits.length ? 'flex' : 'none';
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

$('col-list').addEventListener('click', e => {
  const addBtn = e.target.closest('[data-option-add]');
  if (addBtn) {
    const i = +addBtn.dataset.optionAdd;
    const rows = document.querySelector(`[data-option-editor="${i}"] .option-editor__rows`);
    if (rows) {
      rows.insertAdjacentHTML('beforeend', `
        <div class="option-edit-row">
          <div class="option-edit-row__main">
            <input class="extra-input option-input" data-option="${i}" value="" placeholder="选项内容" />
          </div>
          <button class="btn-icon option-remove" data-option-remove="${i}" title="删除选项" type="button">×</button>
        </div>
      `);
      rows.querySelector('.option-edit-row:last-child .option-input')?.focus();
    }
  }

  const removeBtn = e.target.closest('[data-option-remove]');
  if (removeBtn) {
    removeBtn.closest('.option-edit-row')?.remove();
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

    if (CHOICE_ROLES.includes(role)) {
      const options = collectOptionsForColumn(i);
      if (options.length) {
        out.options = options;
        out.options_original = c.options_original || c.options || [];
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
    if (MATRIX_ROLES.includes(role) && c.rows) out.rows = c.rows;
    if (!out.value_aliases && c.value_aliases && CHOICE_ROLES.includes(role)) {
      out.value_aliases = c.value_aliases;
    }
    return out;
  });
}

$('btn-start-plan').addEventListener('click', startPlan);

async function startPlan() {
  const btn = $('btn-start-plan');
  btn.disabled = true;

  // 先存储用户确认的题型
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
    btn.disabled = false;
    return;
  }

  // 进入 Step 3，开始 AI 规划
  goStep(3);
  $('plan-thinking').style.display = 'flex';
  $('plan-thinking').querySelector('.thinking-block__title').textContent = 'AI 正在规划分析方案，请稍候…';
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
    showToast(`方案生成失败：${e.message}`, 'error');
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
  syncPlanActionButtons();
}

function buildPlanHTML(plan, headers) {
  let html = '';

  const colMap = {};
  for (const c of plan.columns) colMap[c.index] = c;

  // 1. 报告章节（列分类已在 Step 2 由用户确认，此处不再展示）
  html += `<div class="plan-section">
    <div class="plan-section__title">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
      报告章节
    </div>
    <div class="plan-parts">`;

  for (let i = 0; i < plan.parts.length; i++) {
    const p = plan.parts[i];
    const colNames = p.column_indexes.map(idx => {
      const c = colMap[idx];
      return c ? (c.name || (headers && headers[idx]) || `列${idx}`) : `列${idx}`;
    }).join('、');
    html += `<div class="plan-part">
      <span class="plan-part__num">Part ${i+1}</span>
      <span class="plan-part__name">${esc(p.name)}</span>
      <span class="plan-part__cols">${esc(colNames)}</span>
    </div>`;
  }
  html += `</div></div>`;

  // 2. 交叉分析
  const cross = plan.cross_tabs || [];
  if (cross.length) {
    html += `<div class="plan-section">
      <div class="plan-section__title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        交叉分析
      </div>
      <div class="plan-cross">`;
    for (const ct of cross) {
      const pName = (colMap[ct.profile_index]?.name) || (headers && headers[ct.profile_index]) || `列${ct.profile_index}`;
      const qName = (colMap[ct.question_index]?.name) || (headers && headers[ct.question_index]) || `列${ct.question_index}`;
      html += `<div class="plan-cross-item">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        ${esc(pName)} × ${esc(qName)}
      </div>`;
    }
    html += `</div></div>`;
  }

  // 3. 待确认问题
  const openQs = plan.open_questions || [];
  if (openQs.length) {
    html += `<div class="plan-section">
      <div class="plan-section__title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        待确认问题
      </div>
      <div class="plan-questions">`;
    openQs.forEach((q, i) => {
      html += `<div class="plan-question">
        <span class="plan-question__num">Q${i+1}</span>
        <span>${esc(q)}</span>
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

$('btn-plan-ok').addEventListener('click', () => {
  if (($('plan-input').value || '').trim()) return;
  confirmPlan('ok');
});
$('btn-plan-revise').addEventListener('click', () => {
  const txt = $('plan-input').value.trim();
  if (!txt) { showToast('请先输入修改意见', 'info'); return; }
  confirmPlan(txt);
});
$('plan-input').addEventListener('input', syncPlanActionButtons);
$('plan-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const txt = $('plan-input').value.trim();
    if (txt) confirmPlan(txt);
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
        $('plan-input').value = '';
        showToast('方案已修订，请再次确认', 'success');
        syncPlanActionButtons();
        return;
      }
    }

    if (approved) {
      await runStats();
    }
  } catch (e) {
    showToast(`操作失败：${e.message}`, 'error');
    syncPlanActionButtons();
  }
}

// ============================================================
// STEP 4: Stats + Report
// ============================================================

async function runStats() {
  state.viewMode = 'session';
  state.historyId = null;
  state.sessionReport.running = true;
  state.sessionReport.stream = '';
  state.sessionReport.reportMd = null;
  state.sessionReport.title = '';
  goStep(4);
  $('ps-stats').classList.add('progress-step--active');
  $('ps-writing').classList.remove('progress-step--active', 'progress-step--done');
  $('report-stream-container').style.display = 'none';
  $('report-stream-content').textContent = '';

  try {
    const statsResp = await fetch(`/api/stats/${state.sessionId}`, { method: 'POST' });
    if (!statsResp.ok) {
      const d = await statsResp.json();
      throw new Error(d.detail || '统计计算失败');
    }
    $('ps-stats').classList.remove('progress-step--active');
    $('ps-stats').classList.add('progress-step--done');
    $('ps-writing').classList.add('progress-step--active');

    $('report-stream-container').style.display = 'block';
    let fullReport = '';

    await consumeSSE(`/api/report/${state.sessionId}`, ev => {
      if (ev.type === 'chunk') {
        fullReport += ev.content;
        state.sessionReport.stream = fullReport;
        if (state.viewMode === 'session') {
          const el = $('report-stream-content');
          el.textContent = fullReport;
          el.scrollTop = el.scrollHeight;
        }
      }
      if (ev.type === 'report_done') {
        state.sessionReport.running = false;
        state.sessionReport.reportMd = ev.report_md;
        state.sessionReport.title = reportTitleFromMarkdown(ev.report_md);
        if (state.viewMode === 'session') {
          state.historyId = null;
          showReport(ev.report_md);
        } else {
          showToast('当前报告已生成完成，可点击「当前分析」查看', 'success', 7000);
          updateReportContextSwitch();
        }
      }
    });
  } catch (e) {
    state.sessionReport.running = false;
    showToast(`报告生成失败：${e.message}`, 'error');
  }
}

function applyCoreHighlight() {
  const content = $('report-content');
  if (!content) return;

  const wrapElements = (items, extraClass = '') => {
    const cleanItems = items.filter(Boolean);
    if (!cleanItems.length) return;
    if (cleanItems[0].closest('.core-highlight-box')) return;
    const wrapper = document.createElement('div');
    wrapper.className = `core-highlight-box ${extraClass}`.trim();
    cleanItems[0].parentNode.insertBefore(wrapper, cleanItems[0]);
    cleanItems.forEach(item => wrapper.appendChild(item));
  };

  // 找「核心结论」h2
  let coreH2 = null;
  for (const h of content.querySelectorAll('h2')) {
    if (h.textContent.trim() === '核心结论') { coreH2 = h; break; }
  }
  if (coreH2) {
    const toWrap = [coreH2];
    let el = coreH2.nextElementSibling;
    while (el && el.tagName !== 'H1' && el.tagName !== 'H2') {
      toWrap.push(el);
      el = el.nextElementSibling;
    }
    wrapElements(toWrap, 'core-summary-box');
  }

  const isSummaryTitle = text => /^(本章总结|本节总结|章节总结|本部分总结)\s*[:：]?$/.test(text.trim());
  const summaryHeadings = Array.from(content.querySelectorAll('h3, h4')).filter(h => isSummaryTitle(h.textContent));
  summaryHeadings.forEach(heading => {
    const items = [heading];
    let el = heading.nextElementSibling;
    while (el) {
      if (/^H[1-6]$/.test(el.tagName)) break;
      items.push(el);
      el = el.nextElementSibling;
    }
    wrapElements(items, 'chapter-summary-box');
  });

  const inlineSummaries = Array.from(content.querySelectorAll('p')).filter(p => {
    if (p.closest('.core-highlight-box')) return false;
    return /^(本章总结|本节总结|章节总结|本部分总结)\s*[:：]/.test(p.textContent.trim());
  });
  inlineSummaries.forEach(p => {
    const items = [p];
    let el = p.nextElementSibling;
    while (el && !/^H[1-6]$/.test(el.tagName)) {
      if (!['P', 'UL', 'OL', 'BLOCKQUOTE'].includes(el.tagName)) break;
      items.push(el);
      el = el.nextElementSibling;
    }
    wrapElements(items, 'chapter-summary-box');
  });
}

function buildTOC() {
  const tocList = $('report-toc-list');
  if (!tocList) return;
  const content = $('report-content');
  if (!content) return;
  // 选取 h1/h2/h3，跳过第一个 h1（报告大标题）
  const headings = Array.from(content.querySelectorAll('h1, h2, h3'));
  const filtered = headings.filter((h, idx) => {
    if (h.tagName === 'H1' && idx === 0) return false;
    if (h.closest('.core-summary-box') && h.tagName !== 'H2') return false;
    return true;
  });
  if (!filtered.length) { $('report-toc').style.display = 'none'; return; }
  $('report-toc').style.display = '';
  tocList.innerHTML = '';
  filtered.forEach((h, idx) => {
    if (!h.id) h.id = `toc-h-${idx}`;
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.href = `#${h.id}`;
    a.textContent = h.textContent;
    if (h.tagName === 'H2') {
      a.style.paddingLeft = '10px';
      a.style.fontSize = '12px';
    } else if (h.tagName === 'H3') {
      a.style.paddingLeft = '20px';
      a.style.fontSize = '11px';
      a.style.color = 'var(--text-3)';
    }
    a.addEventListener('click', e => {
      e.preventDefault();
      const reportBody = document.querySelector('#panel-5 .report-layout .report-body');
      if (reportBody) {
        const top = h.offsetTop - reportBody.offsetTop;
        reportBody.scrollTo({ top, behavior: 'smooth' });
      } else {
        h.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
    li.appendChild(a);
    tocList.appendChild(li);
  });
}

let _tocDebounce = null;
function buildTOCDebounced() {
  clearTimeout(_tocDebounce);
  _tocDebounce = setTimeout(buildTOC, 800);
}

function reportTitleFromMarkdown(md) {
  const titleMatch = (md || '').match(/^#\s+(.+?)$/m);
  return titleMatch ? titleMatch[1].trim() : '分析报告';
}

function replaceReportTitleInMarkdown(md, title) {
  const cleanTitle = String(title || '').trim() || '分析报告';
  if (/^#\s+.+?$/m.test(md || '')) {
    return (md || '').replace(/^#\s+.+?$/m, `# ${cleanTitle}`);
  }
  return `# ${cleanTitle}\n\n${String(md || '').trimStart()}`;
}

function activeReportCtx() {
  return state.viewMode === 'history' ? state.historyReport : state.sessionReport;
}

function activeReportId() {
  if (state.viewMode === 'history') {
    return state.historyId || state.historyReport.id || '';
  }
  return state.sessionId || state.sessionReport.id || '';
}

function saveActiveReportUi() {
  const ctx = activeReportCtx();
  const qa = $('qa-messages');
  const inline = $('feishu-link-inline');
  if (qa) ctx.qaHtml = qa.innerHTML;
  if (inline) ctx.feishuLinkHtml = inline.innerHTML;
}

function normalizeQAMessages(messages) {
  if (!Array.isArray(messages)) return [];
  return messages
    .filter(m => m && (m.role === 'user' || m.role === 'ai') && String(m.content || '').trim())
    .map(m => ({
      role: m.role,
      content: String(m.content || ''),
      ts: m.ts || '',
    }));
}

function renderQAMessages(messages) {
  const container = $('qa-messages');
  if (!container) return;
  container.innerHTML = '';
  normalizeQAMessages(messages).forEach(m => appendQABubble(m.role, m.content));
}

function updateReportContextSwitch() {
  const bar = $('report-context-switch');
  const sessionBtn = $('btn-report-session');
  const historyBtn = $('btn-report-history');
  if (!bar || !sessionBtn || !historyBtn) return;
  const hasSession = !!(state.sessionId || state.sessionReport.reportMd || state.sessionReport.running);
  const hasHistory = !!state.historyReport.reportMd;
  bar.style.display = hasSession || hasHistory ? '' : 'none';
  sessionBtn.classList.toggle('report-context-switch__btn--active', state.viewMode === 'session');
  sessionBtn.disabled = !hasSession;
  historyBtn.style.display = hasHistory ? '' : 'none';
  historyBtn.classList.toggle('report-context-switch__btn--active', state.viewMode === 'history');
  historyBtn.textContent = hasHistory ? `历史报告：${shortName(state.historyReport.title || '历史报告', 18)}` : '历史报告';
}

function applyQAAvailability() {
  const input = $('qa-input');
  const btn = $('btn-qa-send');
  if (!input || !btn) return;
  if (state.viewMode === 'history') {
    const canChat = !!state.historyReport.analystConvId;
    input.placeholder = canChat ? '可基于该历史报告继续追问（Enter 发送）' : '该历史记录无可续聊的对话，仅供查看';
    input.disabled = !canChat;
    btn.disabled = !canChat || state.qaLoading;
  } else {
    input.placeholder = '基于报告或原始数据继续提问…（Enter 发送，Shift+Enter 换行）';
    input.disabled = false;
    btn.disabled = state.qaLoading;
  }
}

function showReportPanelPreservingProgress() {
  state.viewStep = 5;
  panels.forEach((p, i) => {
    const showing = i + 1 === 5;
    p.classList.toggle('panel--hidden', !showing);
    p.classList.remove('panel--readonly');
  });
  renderStepBars();
  document.querySelector('.main').scrollTo({ top: 0, behavior: 'smooth' });
}

function renderReportWorkspace(md, { preserveQa = true } = {}) {
  state.reportMd = md;
  if (state.viewMode === 'history') showReportPanelPreservingProgress();
  else goStep(5);

  const ctx = activeReportCtx();
  const title = ctx.title || reportTitleFromMarkdown(md);
  $('report-title-display').textContent = title;
  const renameBtn = $('btn-report-rename');
  if (renameBtn) {
    const reportId = activeReportId();
    renameBtn.dataset.reportId = reportId;
    renameBtn.disabled = !reportId;
  }

  $('report-content').innerHTML = renderMarkdown(md);
  if (preserveQa && ctx.qaHtml) {
    $('qa-messages').innerHTML = ctx.qaHtml;
  } else if (preserveQa && normalizeQAMessages(ctx.qaMessages).length) {
    renderQAMessages(ctx.qaMessages);
  } else {
    $('qa-messages').innerHTML = '';
  }
  const lb = $('feishu-link-box'); if (lb) lb.remove();
  const li = $('feishu-link-inline'); if (li) li.innerHTML = ctx.feishuLinkHtml || '';
  applyQAAvailability();
  updateReportContextSwitch();
  applyCoreHighlight();
  buildTOC();
}

function showReport(md) {
  const ctx = activeReportCtx();
  ctx.reportMd = md;
  ctx.title = reportTitleFromMarkdown(md);
  renderReportWorkspace(md, { preserveQa: true });
  if (state.viewMode === 'session') showToast('报告生成完毕！', 'success');
}

function switchReportContext(mode) {
  if (mode === state.viewMode) return;
  saveActiveReportUi();
  if (mode === 'history') {
    if (!state.historyReport.reportMd) return;
    state.viewMode = 'history';
    state.historyId = state.historyReport.id;
    renderReportWorkspace(state.historyReport.reportMd, { preserveQa: true });
    return;
  }
  state.viewMode = 'session';
  state.historyId = null;
  if (state.sessionReport.reportMd) {
    renderReportWorkspace(state.sessionReport.reportMd, { preserveQa: true });
  } else if (state.sessionReport.running) {
    goStep(4);
    $('report-stream-container').style.display = 'block';
    $('report-stream-content').textContent = state.sessionReport.stream || '';
    $('ps-stats').classList.remove('progress-step--active');
    $('ps-stats').classList.add('progress-step--done');
    $('ps-writing').classList.add('progress-step--active');
  } else {
    setViewStep(Math.min(state.currentStep, 4));
  }
  updateReportContextSwitch();
}

$('btn-report-session')?.addEventListener('click', () => switchReportContext('session'));
$('btn-report-history')?.addEventListener('click', () => switchReportContext('history'));

async function updateReportTitle(historyId, title) {
  const cleanTitle = String(title || '').trim();
  if (!historyId) throw new Error('没有可改名的报告');
  if (!cleanTitle) throw new Error('报告名称不能为空');
  const payload = JSON.stringify({ id: historyId, title: cleanTitle });
  const attempts = [
    { url: '/api/history-title', options: { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: payload } },
    { url: `/api/history/${encodeURIComponent(historyId)}/title`, options: { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: cleanTitle }) } },
    { url: `/api/history/${encodeURIComponent(historyId)}/title`, options: { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: cleanTitle }) } },
  ];
  let lastError = null;
  for (const attempt of attempts) {
    const resp = await fetch(attempt.url, { ...attempt.options, credentials: 'same-origin', cache: 'no-store' });
    let data = {};
    try { data = await resp.json(); } catch { data = {}; }
    if (resp.ok) return data;
    lastError = data.detail || `${resp.status} ${resp.statusText || ''}`.trim();
    if (resp.status !== 404 && resp.status !== 405) break;
  }
  throw new Error(lastError || '改名失败');
}

function applyRenamedReport(data) {
  const title = data.title || reportTitleFromMarkdown(data.report_md);
  const reportMd = data.report_md || replaceReportTitleInMarkdown(activeReportCtx().reportMd || state.reportMd || '', title);
  if (state.sessionId === data.id) {
    state.sessionReport.title = title;
    state.sessionReport.reportNo = data.report_no || state.sessionReport.reportNo || '';
    state.sessionReport.reportMd = reportMd;
  }
  if (state.historyReport.id === data.id) {
    state.historyReport.title = title;
    state.historyReport.reportNo = data.report_no || state.historyReport.reportNo || '';
    state.historyReport.reportMd = reportMd;
  }
  if ((state.viewMode === 'history' && state.historyId === data.id) || (state.viewMode === 'session' && state.sessionId === data.id)) {
    saveActiveReportUi();
    activeReportCtx().title = title;
    activeReportCtx().reportMd = reportMd;
    state.reportMd = reportMd;
    renderReportWorkspace(reportMd, { preserveQa: true });
  }
}

function startReportTitleEdit() {
  const titleEl = $('report-title-display');
  const btn = $('btn-report-rename');
  const row = titleEl?.closest('.report-title-row');
  const historyId = btn?.dataset.reportId || activeReportId();
  if (!titleEl || !row || !historyId || row.querySelector('.report-title-edit')) return;

  const oldTitle = titleEl.textContent.trim();
  const input = document.createElement('input');
  input.className = 'report-title-edit';
  input.value = oldTitle;
  input.setAttribute('aria-label', '报告名称');

  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn-title-edit btn-title-edit--save';
  saveBtn.type = 'button';
  saveBtn.textContent = '保存';

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn-title-edit btn-title-edit--cancel';
  cancelBtn.type = 'button';
  cancelBtn.textContent = '取消';

  const finish = () => {
    input.remove();
    saveBtn.remove();
    cancelBtn.remove();
    titleEl.style.display = '';
    btn.style.display = '';
  };

  const save = async () => {
    const nextTitle = input.value.trim();
    if (!nextTitle) { showToast('报告名称不能为空', 'error'); return; }
    saveBtn.disabled = true;
    try {
      const data = await updateReportTitle(historyId, nextTitle);
      finish();
      applyRenamedReport(data);
      showToast('报告名称已更新', 'success');
    } catch (e) {
      saveBtn.disabled = false;
      showToast(`改名失败：${e.message}`, 'error');
    }
  };

  titleEl.style.display = 'none';
  btn.style.display = 'none';
  row.prepend(input);
  row.append(saveBtn, cancelBtn);
  input.focus();
  input.select();
  saveBtn.addEventListener('click', save);
  cancelBtn.addEventListener('click', finish);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); save(); }
    if (e.key === 'Escape') { e.preventDefault(); finish(); }
  });
}

$('btn-report-rename')?.addEventListener('click', startReportTitleEdit);

// ============================================================
// STEP 5: Export + QA
// ============================================================

$('btn-export-word').addEventListener('click', () => {
  if (state.viewMode === 'history' && state.historyId) {
    window.location.href = `/api/export/word-history/${state.historyId}`;
  } else {
    window.location.href = `/api/export/word/${state.sessionId}`;
  }
});

// ── 飞书登录状态 + 权限门控 ──
state.feishu = { configured: false, logged_in: false, allowed: true, name: '', email: '',
                 perms: ['survey','annotate'], is_admin: false };

function applyPermGating() {
  const perms = state.feishu.perms || [];
  const hasSurvey = perms.includes('survey');
  const hasAnnotate = perms.includes('annotate');
  // 侧边栏：无权限则隐藏入口
  const navSurvey = $('nav-survey');
  const navAnnotate = $('nav-annotate');
  if (navSurvey) navSurvey.style.display = hasSurvey ? '' : 'none';
  if (navAnnotate) navAnnotate.style.display = hasAnnotate ? '' : 'none';
  // 如果当前模式无权限，切换到有权限的模式
  if (currentMode === 'survey' && !hasSurvey && hasAnnotate) switchMode('annotate');
  if (currentMode === 'annotate' && !hasAnnotate && hasSurvey) switchMode('survey');
  // 非管理员隐藏整个「设置」入口
  const navSettings = $('nav-settings');
  if (navSettings) navSettings.style.display = state.feishu.is_admin ? '' : 'none';
  // 管理员才显示权限配置 tab
  const permNav = $('stab-perms-nav');
  if (permNav) permNav.style.display = state.feishu.is_admin ? '' : 'none';
}

async function refreshFeishuStatus() {
  try {
    const r = await fetch('/api/feishu/me');
    state.feishu = await r.json();
  } catch { /* ignore */ }
  const label = $('feishu-login-label');
  if (label) {
    label.textContent = state.feishu.logged_in
      ? `飞书：${state.feishu.email || state.feishu.name || '已登录'}`
      : '登录飞书';
  }
  applyPermGating();
}

$('btn-feishu-login').addEventListener('click', async () => {
  if (!state.feishu.configured) {
    showToast('服务端未配置飞书应用（FEISHU_APP_ID/SECRET/REDIRECT_URI）', 'error');
    return;
  }
  if (state.feishu.logged_in) {
    try {
      await fetch('/api/feishu/logout', { method: 'POST' });
    } catch {}
    showToast('已退出飞书登录', 'info');
    window.location.href = '/login';
    return;
  }
  window.location.href = `/api/feishu/login?next=${encodeURIComponent(location.pathname)}`;
});

// ── 飞书文档导出 ──
$('btn-export-pdf').addEventListener('click', () => {
  if (state.viewMode === 'history' && state.historyId) {
    window.location.href = `/api/export/pdf-history/${state.historyId}`;
  } else if (state.sessionId) {
    window.location.href = `/api/export/pdf/${state.sessionId}`;
  } else {
    showToast('还没有生成报告', 'error');
  }
});

$('btn-export-feishu').addEventListener('click', exportFeishu);

function showFeishuConfirmModal(email) {
  return new Promise(resolve => {
    let existing = $('feishu-export-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'feishu-export-modal';
    modal.style.cssText = 'position:fixed;inset:0;z-index:200;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.5);backdrop-filter:blur(4px);';
    modal.innerHTML = `
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
                  padding:24px 28px;width:min(400px,90vw);display:flex;flex-direction:column;gap:16px;
                  box-shadow:var(--shadow-lg)">
        <div style="font-size:15px;font-weight:600;color:var(--text)">上传 PDF 到飞书</div>
        <div style="font-size:13px;color:var(--text-2);line-height:1.7">
          系统会把当前报告导出为 PDF，并通过「让我看看你又在做什么调研」机器人推送给
          <strong style="color:var(--text)">${esc(email)}</strong>
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn btn--ghost" id="feishu-modal-cancel">取消</button>
          <button class="btn btn--primary" id="feishu-modal-confirm">确认生成</button>
        </div>
      </div>`;
    document.body.appendChild(modal);

    const cleanup = (result) => { modal.remove(); resolve(result); };
    $('feishu-modal-cancel').onclick = () => cleanup(false);
    $('feishu-modal-confirm').onclick = () => cleanup(true);
    modal.addEventListener('click', e => { if (e.target === modal) cleanup(false); });
  });
}

async function exportFeishu() {
  if (!state.feishu.configured) {
    showToast('服务端未配置飞书应用', 'error');
    return;
  }
  if (!state.feishu.logged_in) {
    showToast('请先登录飞书（左下角）', 'info');
    window.location.href = `/api/feishu/login?next=${encodeURIComponent(location.pathname)}`;
    return;
  }

  const email = state.feishu.email || state.feishu.name || '当前账号';
  const confirmed = await showFeishuConfirmModal(email);
  if (!confirmed) return;

  const btn = $('btn-export-feishu');
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = '上传中…';

  const url = state.viewMode === 'history' && state.historyId
    ? `/api/export/feishu-history/${state.historyId}`
    : `/api/export/feishu/${state.sessionId}`;
  try {
    const resp = await fetch(url, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) {
      if (resp.status === 401) { showToast('飞书登录已过期，请重新登录', 'error'); await refreshFeishuStatus(); }
      throw new Error(data.detail || '生成失败');
    }
    showFeishuLink(data.url);
    try { await navigator.clipboard.writeText(data.url); showToast('PDF 已上传到飞书，机器人消息已发送', 'success'); }
    catch { showToast('PDF 已上传到飞书', 'success'); }
  } catch (e) {
    showToast(`上传 PDF 到飞书失败：${e.message}`, 'error', 10000);
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

function showFeishuLink(url) {
  // 清除旧的大 box（如果存在）
  const oldBox = $('feishu-link-box');
  if (oldBox) oldBox.remove();
  // 在按钮下方 inline 显示链接
  const inline = $('feishu-link-inline');
  if (inline) {
    inline.innerHTML = `<a href="${esc(url)}" target="_blank" rel="noopener"
      style="color:var(--accent);text-decoration:none;display:flex;align-items:center;gap:4px">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
        <polyline points="14 2 14 8 20 8"/>
      </svg>查看飞书 PDF</a>`;
    activeReportCtx().feishuLinkHtml = inline.innerHTML;
  }
}
$('btn-export-md').addEventListener('click', () => {
  if (state.viewMode === 'history') {
    // 历史无专用 md 导出端点，直接用浏览器下载已渲染的 md
    const blob = new Blob([state.reportMd || ''], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${($('report-title-display').textContent || '调研报告')}.md`;
    a.click();
    URL.revokeObjectURL(url);
  } else {
    window.location.href = `/api/export/markdown/${state.sessionId}`;
  }
});

$('btn-qa-send').addEventListener('click', sendQA);
$('qa-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendQA(); }
});

async function sendQA() {
  if (state.qaLoading) return;
  const question = $('qa-input').value.trim();
  if (!question) return;

  state.qaLoading = true;
  $('btn-qa-send').disabled = true;
  $('qa-input').value = '';

  const qaMode = state.viewMode;
  const qaCtx = activeReportCtx();
  appendQABubble('user', question);
  const typingBubble = appendQABubble('ai', null, true);

  try {
    let answer = '';
    let finalAnswer = '';

    const url  = qaMode === 'history' ? '/api/history-qa' : '/api/qa';
    const body = qaMode === 'history'
      ? { history_id: state.historyId, question }
      : { session_id: state.sessionId, question };

    await consumeSSEPost(url, body, ev => {
      if (ev.type === 'chunk') {
        answer += ev.content;
        typingBubble.innerHTML = renderMarkdown(answer);
        typingBubble.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
      if (ev.type === 'qa_done') {
        finalAnswer = ev.answer || answer;
        typingBubble.innerHTML = renderMarkdown(finalAnswer);
      }
    });
    finalAnswer = finalAnswer || answer;
    if (finalAnswer) {
      qaCtx.qaMessages = normalizeQAMessages([
        ...(qaCtx.qaMessages || []),
        { role: 'user', content: question },
        { role: 'ai', content: finalAnswer },
      ]);
    }
  } catch (e) {
    if (String(e.message || '').includes('请先登录飞书')) {
      await refreshFeishuStatus();
      const loginUrl = (state.feishu && state.feishu.login_url) || `/api/feishu/login?next=${encodeURIComponent(location.pathname)}`;
      typingBubble.innerHTML = `❌ 飞书登录态已失效，请<a href="${esc(loginUrl)}" style="color:var(--accent)">重新登录</a>后再追问`;
      showToast('飞书登录态已失效，请重新登录', 'error', 8000);
    } else {
      typingBubble.textContent = `❌ ${e.message}`;
      showToast(`追问失败：${e.message}`, 'error');
    }
  } finally {
    state.qaLoading = false;
    saveActiveReportUi();
    applyQAAvailability();
    $('qa-input').focus();
  }
}

function appendQABubble(role, text, isTyping = false) {
  const container = $('qa-messages');
  const msgDiv = document.createElement('div');
  msgDiv.className = `qa-message qa-message--${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'qa-message__avatar';
  avatar.textContent = role === 'user' ? '我' : 'AI';

  const bubble = document.createElement('div');
  bubble.className = 'qa-message__bubble';

  if (isTyping) {
    bubble.innerHTML = '<div class="typing-indicator"><span></span><span></span><span></span></div>';
  } else if (role === 'user') {
    bubble.textContent = text;
  } else {
    bubble.innerHTML = renderMarkdown(text || '');
  }

  msgDiv.appendChild(avatar);
  msgDiv.appendChild(bubble);
  container.appendChild(msgDiv);
  container.scrollTop = container.scrollHeight;
  return bubble;
}

// ============================================================
// Drawer 通用控制
// ============================================================

function openDrawer(id)  { $(id).classList.add('drawer--open'); }
function closeDrawer(id) { $(id).classList.remove('drawer--open'); }

document.querySelectorAll('[data-drawer-close]').forEach(el => {
  el.addEventListener('click', e => {
    const drawer = e.target.closest('.drawer');
    if (drawer) drawer.classList.remove('drawer--open');
  });
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.drawer--open').forEach(d => d.classList.remove('drawer--open'));
});

// ============================================================
// 设置抽屉（左导航切换）
// ============================================================

const STAB_LOADERS = {
  texts:   loadUiTextsSettings,
  prompts: loadPrompts,
  perms:   loadPermsTab,
};

function switchSettingsTab(name) {
  document.querySelectorAll('.settings-nav__item').forEach(el => {
    el.classList.toggle('settings-nav__item--active', el.dataset.stab === name);
  });
  ['texts', 'prompts', 'perms'].forEach(k => {
    const el = $(`stab-content-${k}`);
    if (el) el.style.display = k === name ? '' : 'none';
  });
  if (STAB_LOADERS[name]) STAB_LOADERS[name]();
}

document.querySelectorAll('.settings-nav__item[data-stab]').forEach(el => {
  el.addEventListener('click', () => switchSettingsTab(el.dataset.stab));
});

function loadActiveSettingsTab() {
  const active = document.querySelector('.settings-nav__item--active');
  const name = active ? active.dataset.stab : 'texts';
  switchSettingsTab(name);
}

// ── 权限配置 ──────────────────────────────────────────────────

async function loadPermsTab() {
  const body = $('stab-content-perms');
  body.innerHTML = `<div class="hist-empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const resp = await fetch('/api/admin/users');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '加载失败');
    renderPermsTable(data.users || []);
  } catch (e) {
    body.innerHTML = `<div class="hist-empty">加载权限配置失败：${esc(e.message)}</div>`;
  }
}

function renderPermsTable(users) {
  const body = $('stab-content-perms');
  const addRow = `
    <div class="perm-add-row" id="perm-add-row">
      <input type="text" id="perm-new-email" class="plan-input" placeholder="飞书邮箱 或 Open ID（ou_xxxxx）" style="flex:1;min-width:240px" />
      <div class="perm-checkboxes">
        <label><input type="checkbox" id="perm-new-survey" checked class="perm-toggle" /> 问卷分析</label>
        <label><input type="checkbox" id="perm-new-annotate" checked class="perm-toggle" /> 数据标注</label>
      </div>
      <button class="btn btn--primary btn--sm" id="perm-add-btn">添加成员</button>
    </div>`;

  const rows = users.map(u => {
    const isAdmin = u.is_admin;
    const hasSurvey = u.perms.includes('survey');
    const hasAnnotate = u.perms.includes('annotate');
    const adminBadge = isAdmin ? `<span class="perm-badge">管理员</span>` : '';
    const surveyCell = isAdmin
      ? `<span style="color:var(--green)">✓</span>`
      : `<input type="checkbox" class="perm-toggle" ${hasSurvey ? 'checked' : ''} data-perm-email="${esc(u.email)}" data-perm-type="survey" />`;
    const annotateCell = isAdmin
      ? `<span style="color:var(--green)">✓</span>`
      : `<input type="checkbox" class="perm-toggle" ${hasAnnotate ? 'checked' : ''} data-perm-email="${esc(u.email)}" data-perm-type="annotate" />`;
    const deleteBtn = isAdmin ? `<span style="color:var(--text-3);font-size:12px">—</span>`
      : `<button class="btn btn--ghost btn--sm" data-perm-delete="${esc(u.email)}">删除</button>`;
    const enabledToggle = isAdmin ? '' : `
      <input type="checkbox" class="perm-toggle" ${u.enabled ? 'checked' : ''} data-perm-email="${esc(u.email)}" data-perm-type="enabled" title="${u.enabled ? '已启用（点击禁用）' : '已禁用（点击启用）'}" />`;

    return `<tr>
      <td>${esc(u.email)} ${adminBadge}</td>
      <td style="text-align:center">${surveyCell}</td>
      <td style="text-align:center">${annotateCell}</td>
      <td style="text-align:center">${enabledToggle}</td>
      <td style="text-align:center">${deleteBtn}</td>
    </tr>`;
  }).join('');

  body.innerHTML = addRow + `
    <table class="perm-table">
      <thead><tr>
        <th>飞书邮箱</th>
        <th style="text-align:center">问卷分析</th>
        <th style="text-align:center">数据标注</th>
        <th style="text-align:center">启用</th>
        <th style="text-align:center">操作</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  // 添加成员
  $('perm-add-btn').addEventListener('click', async () => {
    const email = ($('perm-new-email').value || '').trim();
    if (!email) { showToast('请输入邮箱或 Open ID', 'error'); return; }
    const perms = [];
    if ($('perm-new-survey').checked) perms.push('survey');
    if ($('perm-new-annotate').checked) perms.push('annotate');
    try {
      const r = await fetch('/api/admin/users', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ email, perms }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || '添加失败');
      showToast(`已添加 ${email}`, 'success');
      loadPermsTab();
    } catch (e) { showToast(e.message, 'error'); }
  });

  // 权限勾选 + 启用状态变更
  body.querySelectorAll('[data-perm-email][data-perm-type]').forEach(cb => {
    cb.addEventListener('change', async () => {
      const email = cb.dataset.permEmail;
      const type = cb.dataset.permType;
      const checked = cb.checked;
      try {
        let patch = {};
        if (type === 'enabled') {
          patch = { enabled: checked };
        } else {
          // 重新读取该行另一个 checkbox 的状态
          const row = cb.closest('tr');
          const surveyEl = row.querySelector('[data-perm-type="survey"]');
          const annotateEl = row.querySelector('[data-perm-type="annotate"]');
          const perms = [];
          if ((type === 'survey' ? checked : surveyEl?.checked)) perms.push('survey');
          if ((type === 'annotate' ? checked : annotateEl?.checked)) perms.push('annotate');
          patch = { perms };
        }
        const r = await fetch(`/api/admin/users/${encodeURIComponent(email)}`, {
          method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify(patch)
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || '更新失败');
        showToast('已保存', 'success', 1500);
      } catch (e) { showToast(e.message, 'error'); cb.checked = !checked; }
    });
  });

  // 删除
  body.querySelectorAll('[data-perm-delete]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const email = btn.dataset.permDelete;
      if (!confirm(`确认删除 ${email}？`)) return;
      try {
        const r = await fetch(`/api/admin/users/${encodeURIComponent(email)}`, { method: 'DELETE' });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || '删除失败');
        showToast(`已删除 ${email}`, 'success');
        loadPermsTab();
      } catch (e) { showToast(e.message, 'error'); }
    });
  });
}

async function loadPrompts() {
  const body = $('stab-content-prompts');
  body.innerHTML = `<div class="hist-empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const resp = await fetch('/api/prompts');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '加载失败');
    body.innerHTML = Object.values(data).map(promptCardHTML).join('');
  } catch (e) {
    body.innerHTML = `<div class="hist-empty">加载提示词失败：${esc(e.message)}</div>`;
  }
}

async function loadUiTextsSettings() {
  const body = $('stab-content-texts');
  body.innerHTML = `<div class="hist-empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const resp = await fetch('/api/ui-texts');
    if (!resp.ok) throw new Error('加载失败');
    const texts = await resp.json();
    body.innerHTML = Object.entries(texts).map(([key, item]) => `
      <div class="uitext-card" data-uitext-key="${esc(key)}">
        <div class="uitext-card__label">${esc(item.label)}</div>
        <textarea class="prompt-textarea uitext-textarea" rows="2">${esc(item.current)}</textarea>
        <div class="uitext-card__actions">
          <button class="btn btn--primary btn--sm" data-uitext-save="${esc(key)}">保存</button>
        </div>
      </div>
    `).join('');
  } catch (e) {
    body.innerHTML = `<div class="hist-empty">加载失败：${esc(e.message)}</div>`;
  }
}

$('stab-content-texts').addEventListener('click', async e => {
  const btn = e.target.closest('[data-uitext-save]');
  if (!btn) return;
  const key = btn.dataset.uitextSave;
  const card = btn.closest('.uitext-card');
  const textarea = card.querySelector('.uitext-textarea');
  try {
    btn.textContent = '保存中…';
    btn.disabled = true;
    const resp = await fetch(`/api/ui-texts/${key}`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: textarea.value}),
    });
    if (!resp.ok) { const d = await resp.json(); throw new Error(d.detail || '保存失败'); }
    showToast('文案已保存', 'success');
    const el = document.querySelector(`[data-uitext="${key}"]`);
    if (el) el.textContent = textarea.value;
  } catch (err) {
    showToast(`保存失败：${err.message}`, 'error');
  } finally {
    btn.textContent = '保存';
    btn.disabled = false;
  }
});

function promptCardHTML(p) {
  const readonly = !p.editable;
  const badge = readonly
    ? `<span class="prompt-card__badge">Dify 管理</span>` : '';
  const difyLink = (readonly && p.dify_url)
    ? `<a class="prompt-dify-link" href="${esc(p.dify_url)}" target="_blank" rel="noopener">
         <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
         前往 Dify 后台
       </a>` : '';

  const editActions = readonly ? '' : `
    <div class="prompt-card__actions">
      <input class="prompt-note-input" data-note="${esc(p.key)}" placeholder="修改说明（可选）" />
      <button class="btn btn--primary" data-save="${esc(p.key)}">保存</button>
    </div>`;

  const hist = (p.history || []).length ? `
    <div class="prompt-history">
      <button class="prompt-history__toggle" data-hist-toggle="${esc(p.key)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
        修改历史（${p.history.length}）
      </button>
      <div class="prompt-history__list" data-hist-list="${esc(p.key)}">
        ${p.history.map(h => `
          <div class="history-item">
            <div class="history-item__meta">
              <span class="history-item__ts">${esc(h.ts)}</span>
              <span class="history-item__note">${esc(h.note || '')}</span>
            </div>
            <div class="history-item__preview" title="${esc(h.content)}">${esc((h.content || '').slice(0, 120))}</div>
          </div>`).join('')}
      </div>
    </div>` : '';

  return `<div class="prompt-card ${readonly ? 'prompt-card--readonly' : ''}">
    <div class="prompt-card__header">
      <div class="prompt-card__title">${esc(p.label)}</div>
      ${badge}
    </div>
    <div class="prompt-card__desc">${esc(p.description || '')}</div>
    ${difyLink}
    <textarea class="prompt-textarea" data-content="${esc(p.key)}" ${readonly ? 'readonly' : ''}>${esc(p.current || '')}</textarea>
    ${editActions}
    ${hist}
  </div>`;
}

$('stab-content-prompts').addEventListener('click', async e => {
  // 历史折叠
  const toggle = e.target.closest('[data-hist-toggle]');
  if (toggle) {
    const key = toggle.dataset.histToggle;
    document.querySelector(`[data-hist-list="${key}"]`)?.classList.toggle('open');
    return;
  }
  // 保存
  const saveBtn = e.target.closest('[data-save]');
  if (saveBtn) {
    const key = saveBtn.dataset.save;
    const content = document.querySelector(`[data-content="${key}"]`).value;
    const note    = (document.querySelector(`[data-note="${key}"]`) || {}).value || '';
    saveBtn.disabled = true;
    saveBtn.textContent = '保存中…';
    try {
      const resp = await fetch(`/api/prompts/${key}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, note }),
      });
      const d = await resp.json();
      if (!resp.ok) throw new Error(d.detail || '保存失败');
      showToast('提示词已保存，下次分析生效', 'success');
      loadPrompts();
    } catch (err) {
      showToast(`保存失败：${err.message}`, 'error');
      saveBtn.disabled = false;
      saveBtn.textContent = '保存';
    }
  }
});

// ============================================================
// 历史记录抽屉
// ============================================================

$('btn-open-history').addEventListener('click', () => {
  openDrawer('history-drawer');
  loadHistory();
});

async function loadHistory() {
  const body = $('history-body');
  body.innerHTML = `<div class="hist-empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const resp = await fetch('/api/history');
    const list = await resp.json();
    if (!resp.ok) throw new Error((list && list.detail) || '加载失败');

    if (!list.length) {
      body.innerHTML = `<div class="hist-empty">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg>
        暂无历史记录，生成报告后会自动保存最近 5 份
      </div>`;
      return;
    }

    body.innerHTML = `<div class="hist-list">` + list.map(renderHistoryCard).join('') + `</div>`;
  } catch (e) {
    body.innerHTML = `<div class="hist-empty">加载历史失败：${esc(e.message)}</div>`;
  }
}

function renderHistoryCard(h) {
  const isActive = state.viewMode === 'history' && state.historyId === h.id;
  const reportNo = String(h.report_no || '').trim()
    || (h.id ? `R-${String(h.id).slice(0, 4).toUpperCase()}` : 'R-?');
  return `
    <div class="hist-card${isActive ? ' hist-card--active' : ''}" data-hist-id="${esc(h.id)}">
      <div class="hist-card__top">
        <span class="hist-card__no">${esc(reportNo)}</span>
        ${h.qa_count > 0 ? `<span class="hist-card__qa-badge">已追问</span>` : ''}
        <span class="hist-card__time">${esc(formatTime(h.created_at))}</span>
      </div>
      <div class="hist-card__title-row">
        <div class="hist-card__title" data-hist-title>${esc(h.title)}</div>
        <button class="hist-card__edit" type="button" data-hist-edit title="修改报告名称" aria-label="修改报告名称">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 20h9"/>
            <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>
          </svg>
        </button>
      </div>
      <div class="hist-card__file">${esc(h.filename || '')}</div>
    </div>`;
}

function formatTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch { return iso; }
}

$('history-body').addEventListener('click', async e => {
  const editBtn = e.target.closest('[data-hist-edit]');
  if (editBtn) {
    e.stopPropagation();
    startHistoryTitleEdit(editBtn.closest('[data-hist-id]'));
    return;
  }
  if (e.target.closest('.hist-card-title-edit, .hist-card-title-action')) return;
  const card = e.target.closest('[data-hist-id]');
  if (!card) return;
  const id = card.dataset.histId;
  try {
    const resp = await fetch(`/api/history/${id}`);
    const entry = await resp.json();
    if (!resp.ok) throw new Error(entry.detail || '加载失败');

    saveActiveReportUi();
    state.viewMode  = 'history';
    state.historyId = id;
    state.historyReport.id = id;
    state.historyReport.reportNo = entry.report_no || '';
    state.historyReport.reportMd = entry.report_md;
    state.historyReport.title = entry.title || reportTitleFromMarkdown(entry.report_md);
    state.historyReport.analystConvId = entry.analyst_conv_id || null;
    state.historyReport.planData = entry.plan || null;
    state.historyReport.qaMessages = normalizeQAMessages(entry.qa_messages);
    state.historyReport.qaHtml = '';
    state.historyReport.feishuLinkHtml = '';

    closeDrawer('history-drawer');
    renderReportWorkspace(entry.report_md, { preserveQa: true });
    showToast('已载入历史报告', 'success');
  } catch (err) {
    showToast(`载入失败：${err.message}`, 'error');
  }
});

function startHistoryTitleEdit(card) {
  if (!card || card.querySelector('.hist-card-title-edit')) return;
  const titleEl = card.querySelector('[data-hist-title]');
  const editBtn = card.querySelector('[data-hist-edit]');
  const oldTitle = titleEl?.textContent.trim() || '';
  const historyId = card.dataset.histId;
  if (!titleEl || !historyId) return;

  const input = document.createElement('input');
  input.className = 'hist-card-title-edit';
  input.value = oldTitle;
  input.setAttribute('aria-label', '报告名称');

  const saveBtn = document.createElement('button');
  saveBtn.className = 'hist-card-title-action';
  saveBtn.type = 'button';
  saveBtn.textContent = '保存';

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'hist-card-title-action hist-card-title-action--ghost';
  cancelBtn.type = 'button';
  cancelBtn.textContent = '取消';

  const row = titleEl.closest('.hist-card__title-row');
  const finish = () => {
    input.remove();
    saveBtn.remove();
    cancelBtn.remove();
    titleEl.style.display = '';
    if (editBtn) editBtn.style.display = '';
  };
  const save = async () => {
    const nextTitle = input.value.trim();
    if (!nextTitle) { showToast('报告名称不能为空', 'error'); return; }
    saveBtn.disabled = true;
    try {
      const data = await updateReportTitle(historyId, nextTitle);
      titleEl.textContent = data.title;
      finish();
      applyRenamedReport(data);
      updateReportContextSwitch();
      showToast('报告名称已更新', 'success');
    } catch (err) {
      saveBtn.disabled = false;
      showToast(`改名失败：${err.message}`, 'error');
    }
  };

  titleEl.style.display = 'none';
  if (editBtn) editBtn.style.display = 'none';
  row.prepend(input);
  row.append(saveBtn, cancelBtn);
  input.focus();
  input.select();
  saveBtn.addEventListener('click', save);
  cancelBtn.addEventListener('click', finish);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); save(); }
    if (e.key === 'Escape') { e.preventDefault(); finish(); }
  });
}

// ============================================================
// Restart
// ============================================================

$('btn-restart').addEventListener('click', () => {
  if (!confirm('确定要重新开始吗？当前会话数据将被清除。')) return;
  state.sessionId = null;
  state.columns = null;
  state.planData = null;
  state.reportMd = null;
  state.qaLoading = false;
  state.viewMode = 'session';
  state.historyId = null;
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
  resetUploadZone();
  fileInput.value = '';
  $('qa-input').disabled = false;
  $('btn-qa-send').disabled = false;
  // 回到分析类型选择层
  $('analysis-type-picker').style.display = '';
  $('upload-area').style.display = 'none';
  goStep(1);
  showToast('已重置，请重新上传文件', 'info');
});

// ── 分析类型选择器 ──
$('btn-qual-enter').addEventListener('click', () => {
  $('analysis-type-picker').style.display = 'none';
  $('upload-area').style.display = '';
  // 每次进入上传区都（重新）加载说明文案，确保显示
  fetch('/api/upload-guide')
    .then(r => r.json())
    .then(({ content }) => {
      const el = $('upload-guide');
      if (el && content) el.innerHTML = marked.parse(content);
    })
    .catch(() => {});
});

// ── UI 文案初始化 ──
async function initUiTexts() {
  try {
    const resp = await fetch('/api/ui-texts');
    if (!resp.ok) return;
    const texts = await resp.json();
    Object.entries(texts).forEach(([key, item]) => {
      const el = document.querySelector(`[data-uitext="${key}"]`);
      if (el) el.textContent = item.current;
    });
  } catch {}
}

// ── Init ──
goStep(1);
refreshFeishuStatus();
initUiTexts();

// ============================================================
// 模式切换（问卷分析 ↔ 数据标注）
// ============================================================

const surveyPanels = panels;            // panel-1 ~ panel-5
const annPanelIds  = [1, 2, 3, 4, 5, 6];
const annPanels    = annPanelIds.map(n => $(`ann-panel-${n}`));

let currentMode = 'survey'; // 'survey' | 'annotate'

function switchMode(mode) {
  currentMode = mode;
  const isSurvey = mode === 'survey';

  // 一级导航激活状态
  $('nav-survey').classList.toggle('nav-item--active', isSurvey);
  $('nav-survey').classList.toggle('nav-item--expanded', isSurvey);
  $('nav-annotate').classList.toggle('nav-item--active', !isSurvey);
  $('nav-annotate').classList.toggle('nav-item--expanded', !isSurvey);
  $('nav-settings').classList.remove('nav-item--active');

  // 历史记录按钮仅在问卷分析时显示
  $('btn-open-history').style.display = isSurvey ? '' : 'none';

  surveyPanels.forEach(p => p.classList.add('panel--hidden'));
  annPanels.forEach(p => p.classList.add('panel--hidden'));
  if (isSurvey) {
    goStep(state.currentStep);
  } else {
    annGoStep(annState.currentStep);
  }
}

// 一级导航点击
$('nav-header-survey').addEventListener('click', () => switchMode('survey'));
$('nav-header-annotate').addEventListener('click', () => switchMode('annotate'));

// 设置入口
$('nav-header-settings').addEventListener('click', () => {
  $('nav-settings').classList.add('nav-item--active');
  $('nav-survey').classList.remove('nav-item--active');
  $('nav-annotate').classList.remove('nav-item--active');
  openDrawer('settings-drawer');
  loadActiveSettingsTab();
});

// 步骤条点击（问卷分析和数据标注）—— 已完成步骤可回看
document.addEventListener('click', e => {
  const btn = e.target.closest('[data-survey-step]');
  if (btn) {
    const n = +btn.dataset.surveyStep;
    if (currentMode === 'survey' && n <= state.currentStep) setViewStep(n);
    return;
  }
  // 数据标注步骤条（标注流程不支持回看，忽略点击）
});

// QA 收起/展开按钮
function updateQAPanelButtons() {
  const side = $('qa-side');
  const wideBtn = $('btn-qa-wide');
  const collapseBtn = $('btn-qa-collapse');
  if (!side) return;
  if (wideBtn) {
    wideBtn.title = side.classList.contains('qa-side--wide') ? '缩小追问面板' : '展开追问面板';
  }
  if (collapseBtn) {
    collapseBtn.title = side.classList.contains('qa-side--collapsed') ? '展开追问面板' : '收起追问面板';
  }
}

const btnQaWide = $('btn-qa-wide');
if (btnQaWide) {
  btnQaWide.addEventListener('click', () => {
    const side = $('qa-side');
    if (!side) return;
    side.classList.remove('qa-side--collapsed');
    side.classList.toggle('qa-side--wide');
    updateQAPanelButtons();
  });
}

const btnQaCollapse = $('btn-qa-collapse');
if (btnQaCollapse) {
  btnQaCollapse.addEventListener('click', () => {
    const side = $('qa-side');
    if (!side) return;
    side.classList.toggle('qa-side--collapsed');
    updateQAPanelButtons();
  });
}
updateQAPanelButtons();

// ============================================================
// 数据标注状态机
// ============================================================

const annState = {
  sessionId:       null,
  currentStep:     1,
  headers:         [],
  headersZh:       [],
  idCol:           1,
  openTextCols:    [],
  matrixColIdxs:   new Set(),
  tasks:           { ai_detect: false, quality: false },
  aiResults:       [],
  highProbResults: [],
  confirmedAiIds:  new Set(),
  qualityCount:    0,
};

function annGoStep(n) {
  annState.currentStep = n;
  annPanels.forEach((p, i) => p.classList.toggle('panel--hidden', i + 1 !== n));
  // 更新数据标注步骤条状态
  document.querySelectorAll('[data-ann-step]').forEach(btn => {
    const i = +btn.dataset.annStep;
    btn.classList.remove('step-bar__item--active', 'step-bar__item--done');
    if (i < n)      btn.classList.add('step-bar__item--done');
    else if (i === n) btn.classList.add('step-bar__item--active');
    btn.disabled = true; // 标注流程不支持回看
  });
  document.querySelector('.main').scrollTo({ top: 0, behavior: 'smooth' });
}

// ── ANN STEP 1: 上传 ────────────────────────────────────────

const annUploadZone = $('ann-upload-zone');
const annFileInput  = $('ann-file-input');

annUploadZone.addEventListener('click', () => annFileInput.click());
annUploadZone.addEventListener('dragover', e => { e.preventDefault(); annUploadZone.classList.add('drag-over'); });
annUploadZone.addEventListener('dragleave', () => annUploadZone.classList.remove('drag-over'));
annUploadZone.addEventListener('drop', e => {
  e.preventDefault();
  annUploadZone.classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) annHandleUpload(file);
});
annFileInput.addEventListener('change', () => {
  if (annFileInput.files[0]) annHandleUpload(annFileInput.files[0]);
});

async function annHandleUpload(file) {
  const MAX = 50 * 1024 * 1024;
  if (file.size > MAX) { showToast('文件超过 50MB 上限', 'error'); return; }
  annUploadZone.innerHTML = `
    <div class="upload-zone__icon"><div class="spinner" style="width:40px;height:40px;border-width:3px"></div></div>
    <div class="upload-zone__text"><span class="upload-zone__primary">正在上传 ${esc(file.name)}…</span></div>`;

  const fd = new FormData();
  fd.append('file', file);
  try {
    const resp = await fetch('/api/annotate/upload', { method: 'POST', body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '上传失败');

    annState.sessionId      = data.session_id;
    annState.headers        = data.headers;
    annState.headersZh      = data.headers_zh || data.headers;
    annState.idCol          = data.id_col;
    annState.openTextCols   = data.open_text_cols;
    annState.matrixColIdxs  = new Set(data.matrix_col_idxs || []);

    $('ann-preview-meta').textContent =
      `${data.filename} · ${data.total_rows} 行数据 · ${data.headers.length} 列`;

    annRenderColConfig(data.headers, data.id_col, data.open_text_cols, data.headers_zh || data.headers, new Set(data.matrix_col_idxs || []));
    annGoStep(2);
    showToast(`成功读取 ${data.total_rows} 行数据`, 'success');
  } catch (e) {
    showToast(`上传失败：${e.message}`, 'error');
    annResetUploadZone();
  }
}

function annResetUploadZone() {
  annUploadZone.innerHTML = `
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

// ── ANN STEP 2: 列确认 + 任务 ──────────────────────────────

function annRenderColConfig(headers, idCol, openTextCols, headersZh, matrixIdxs) {
  const zh     = headersZh || headers;
  const otSet  = new Set(openTextCols);
  const mxSet  = matrixIdxs || new Set();
  const container = $('ann-col-config');

  // ID 列选择（显示中文名，排除矩阵子列）
  const idOpts = headers.map((h, i) =>
    `<option value="${i}" ${i === idCol ? 'selected' : ''}>${i}: ${esc(zh[i] || h)}</option>`
  ).join('');

  // 主观题列多选——每行一题，矩阵子列隐藏
  const otRows = headers.map((h, i) => {
    if (mxSet.has(i)) return '';   // 矩阵子列不显示
    const zhName = zh[i] || h;
    const hasDiff = zhName !== h;
    return `
    <label class="ann-col-check-item ann-col-check-item--full">
      <input type="checkbox" class="ann-ot-check" value="${i}" ${otSet.has(i) ? 'checked' : ''} />
      <span class="ann-col-idx">${i}</span>
      <span class="ann-col-name-wrap">
        <span class="ann-col-zh">${esc(zhName)}</span>
        ${hasDiff ? `<span class="ann-col-original">${esc(h)}</span>` : ''}
      </span>
    </label>`;
  }).join('');

  container.innerHTML = `
    <div class="ann-col-row">
      <label class="ann-label">玩家唯一 ID 列</label>
      <select class="type-select" id="ann-id-col-sel">${idOpts}</select>
    </div>
    <div class="ann-col-row" style="flex-direction:column;align-items:flex-start">
      <label class="ann-label">主观题列（可多选）</label>
      <div class="ann-col-check-list ann-col-check-list--full">${otRows}</div>
    </div>`;

  // 更新 annState
  $('ann-id-col-sel').addEventListener('change', e => {
    annState.idCol = +e.target.value;
  });
  container.querySelectorAll('.ann-ot-check').forEach(cb => {
    cb.addEventListener('change', () => {
      annState.openTextCols = [...container.querySelectorAll('.ann-ot-check:checked')].map(c => +c.value);
      annUpdateStartBtn();
    });
  });
  annState.idCol        = idCol;
  annState.openTextCols = [...otSet];
  annUpdateStartBtn();
}

// 任务勾选
function annArrangeStep2Layout() {
  const panel = $('ann-panel-2');
  const colConfig = $('ann-col-config');
  const tasks = $('ann-tasks');
  const background = $('ann-background-block');
  const actions = panel ? panel.querySelector('.col-confirm-actions') : null;
  if (!panel || !colConfig || !tasks || !background || !actions) return;
  if (!tasks.querySelector('.ann-task-grid')) {
    const grid = document.createElement('div');
    grid.className = 'ann-task-grid';
    [...tasks.querySelectorAll(':scope > .ann-task-option')].forEach(option => grid.appendChild(option));
    tasks.appendChild(grid);
  }
  colConfig.insertAdjacentElement('afterend', tasks);
  tasks.insertAdjacentElement('afterend', background);
  background.insertAdjacentElement('afterend', actions);
  const bgInput = $('ann-background');
  if (bgInput) {
    bgInput.classList.add('ann-background-textarea');
    bgInput.rows = Math.max(bgInput.rows || 0, 5);
  }
  tasks.querySelectorAll('.ann-task-option').forEach(option => {
    const input = option.querySelector('input[type="checkbox"]');
    if (!input || option.querySelector('.ann-task-check')) return;
    const check = document.createElement('span');
    check.className = 'ann-task-check';
    check.setAttribute('aria-hidden', 'true');
    input.insertAdjacentElement('afterend', check);
  });
}

function annSyncTaskCards() {
  document.querySelectorAll('.ann-task-option').forEach(option => {
    const input = option.querySelector('input[type="checkbox"]');
    option.classList.toggle('ann-task-option--active', !!input?.checked);
  });
}

['task-ai-detect', 'task-quality'].forEach(id => {
  $(id).addEventListener('change', () => {
    annState.tasks.ai_detect = $('task-ai-detect').checked;
    annState.tasks.quality   = $('task-quality').checked;
    $('ann-background-block').style.display = annState.tasks.ai_detect ? '' : 'none';
    annSyncTaskCards();
    annUpdateStartBtn();
  });
});

annArrangeStep2Layout();
annSyncTaskCards();

function annUpdateStartBtn() {
  const hasTask = annState.tasks.ai_detect || annState.tasks.quality;
  const hasCols = annState.openTextCols.length > 0;
  $('ann-btn-start').disabled = !(hasTask && hasCols);
}

$('ann-btn-start').addEventListener('click', annStartAnnotation);

async function annStartAnnotation() {
  $('ann-btn-start').disabled = true;
  // 读取最新 id_col
  const idColSel = $('ann-id-col-sel');
  if (idColSel) annState.idCol = +idColSel.value;

  try {
    const resp = await fetch(`/api/annotate/${annState.sessionId}/confirm-columns`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id_col:         annState.idCol,
        open_text_cols: annState.openTextCols,
        tasks:          annState.tasks,
        background:     ($('ann-background').value || '').trim(),
      }),
    });
    if (!resp.ok) {
      const d = await resp.json();
      throw new Error(d.detail || '保存失败');
    }
    annState.aiResults       = [];
    annState.highProbResults = [];
    annState.confirmedAiIds  = new Set();
    annState.qualityCount    = 0;

    if (annState.tasks.ai_detect) {
      annGoStep(3);
      await annRunAiDetect();
    } else if (annState.tasks.quality) {
      annGoStep(5);
      await annRunQuality();
    }
  } catch (e) {
    showToast(`启动失败：${e.message}`, 'error');
    $('ann-btn-start').disabled = false;
  }
}

// ── ANN STEP 3: AI 检测 ────────────────────────────────────

async function annRunAiDetect() {
  const bar     = $('ann-ai-progress-bar');
  const msg     = $('ann-ai-progress-msg');
  const warnLog = $('ann-ai-warn-log');
  let backBtn = $('ann-btn-ai-back');
  if (!backBtn) {
    backBtn = document.createElement('button');
    backBtn.id = 'ann-btn-ai-back';
    backBtn.className = 'btn btn--ghost';
    backBtn.textContent = '返回任务选择';
    warnLog.insertAdjacentElement('afterend', backBtn);
  }
  backBtn.style.display = 'none';
  backBtn.onclick = () => {
    $('ann-btn-start').disabled = false;
    annGoStep(2);
  };
  const appendAiLog = (text, type = 'warn') => {
    const div = document.createElement('div');
    div.className = `ann-warn-item ann-warn-item--${type}`;
    div.textContent = text;
    warnLog.appendChild(div);
  };
  bar.style.width = '0%';
  msg.textContent = '正在连接…';
  warnLog.innerHTML = '';
  const diagnostics = [];

  try {
    await consumeSSE(`/api/annotate/${annState.sessionId}/run-ai-detect`, ev => {
      if (ev.type === 'started') {
        bar.style.width = '2%';
        msg.textContent = ev.msg || `已连接，准备分析 ${ev.rows || 0} 行，约 ${ev.total_batches || 0} 批`;
      }
      if (ev.type === 'batch_started') {
        const pct = ev.total > 0 ? Math.round((ev.done / ev.total) * 100) : 0;
        bar.style.width = `${Math.max(3, pct)}%`;
        msg.textContent = ev.msg || `正在分析第 ${ev.batch || 1}/${ev.total || 1} 批`;
      }
      if (ev.type === 'dify_waiting') {
        msg.textContent = ev.msg || '正在等待 AI 返回，请勿关闭页面';
      }
      if (ev.type === 'dify_done') {
        diagnostics.push(ev.msg || `第 ${ev.batch || '?'} 批 AI 已返回`);
        msg.textContent = ev.msg || msg.textContent;
      }
      if (ev.type === 'batch_done') {
        const pct = ev.total > 0 ? Math.round((ev.done / ev.total) * 100) : 0;
        bar.style.width = `${pct}%`;
        msg.textContent = ev.msg || `${ev.done}/${ev.total} 批已完成`;
      }
      if (ev.type === 'progress') {
        const pct = ev.total > 0 ? Math.round((ev.done / ev.total) * 100) : 0;
        bar.style.width = `${pct}%`;
        msg.textContent = ev.msg || `${ev.done}/${ev.total} 批已完成`;
      }
      if (ev.type === 'warn') {
        const warning = ev.msg || '处理警告';
        diagnostics.push(warning);
        appendAiLog(warning);
      }
      if (ev.type === 'ai_detect_done') {
        bar.style.width = '100%';
        const results = ev.results || [];
        msg.textContent = `AI 检测完成，共 ${results.length} 条结果`;
        annState.aiResults       = results;
        annState.highProbResults = ev.high_prob || [];
      }
    });

    // 有高概率结果 → 跳到确认步
    if (annState.aiResults.length === 0) {
      msg.textContent = 'AI 识别没有得到可用结果，请返回任务选择后重试';
      const detail = diagnostics.length
        ? `最近诊断：${diagnostics.slice(-3).join(' ｜ ')}`
        : '没有收到批次诊断信息，可能是连接在服务端返回前中断。';
      appendAiLog(`所有批次都没有解析出可用结果。${detail}`, 'error');
      backBtn.style.display = '';
      return;
    }

    if (annState.highProbResults.length > 0) {
      annRenderAiConfirm(annState.highProbResults);
      annGoStep(4);
    } else {
      showToast('未发现高概率 AI 作答（≥ 80%），自动跳过确认步骤', 'info');
      await annAfterAiConfirm();
    }
  } catch (e) {
    msg.textContent = `AI 识别失败：${e.message}`;
    appendAiLog(e.message, 'error');
    backBtn.style.display = '';
    showToast(`AI 检测失败：${e.message}`, 'error');
  }
}

// ── ANN STEP 4: AI 确认 ────────────────────────────────────

function annRenderAiConfirm(highProbResults) {
  const table   = $('ann-confirm-table');
  const headers = annState.headers;
  const otCols  = annState.openTextCols;

  // 构建表头
  let thCells = `<th><input type="checkbox" id="ann-check-master" checked /></th>
    <th>玩家 ID</th><th>AI 概率</th><th>润色程度</th><th>判断理由</th><th>关键证据</th>`;
  for (const ci of otCols) {
    const hdr = headers[ci] || `列${ci}`;
    thCells += `<th>${esc(hdr)}（原文）</th><th>${esc(hdr)}（中文译）</th>`;
  }

  let rows = '';
  highProbResults.forEach((r, i) => {
    const checked = 'checked';
    let tdCols = '';
    for (const ci of otCols) {
      const key = `col_${ci}`;
      const trans = (r.translations || {})[key] || '';
      // 找原文：需要从原始行中取，但这里只有 translations，用原文 evidence 作示意
      // 实际上我们没有把原文存在 highProbResults 里，用 evidence 代替
      tdCols += `<td class="ann-cell-text">${esc(trans || '')}</td>
                 <td class="ann-cell-text ann-cell-trans">${esc(trans)}</td>`;
    }
    rows += `<tr data-row="${i}">
      <td><input type="checkbox" class="ann-ai-check" data-id="${esc(r.id)}" ${checked} /></td>
      <td class="ann-cell-id">${esc(r.id)}</td>
      <td class="ann-cell-prob">${r.ai_prob}%</td>
      <td class="ann-cell-polish">${esc(r.is_polished || '')}</td>
      <td class="ann-cell-reason">${esc(r.reason || '')}</td>
      <td class="ann-cell-evidence">${esc(r.evidence || '')}</td>
      ${tdCols}
    </tr>`;
  });

  table.innerHTML = `<thead><tr>${thCells}</tr></thead><tbody>${rows}</tbody>`;

  // 主控勾选
  $('ann-check-master').addEventListener('change', e => {
    table.querySelectorAll('.ann-ai-check').forEach(cb => { cb.checked = e.target.checked; });
  });

  $('ann-confirm-desc').textContent =
    `以下 ${highProbResults.length} 位受访者 AI 作答概率 ≥ 80%，请逐行确认是否标注为 AI 作答`;
}

$('ann-btn-check-all').addEventListener('click', () => {
  document.querySelectorAll('.ann-ai-check').forEach(cb => { cb.checked = true; });
});
$('ann-btn-uncheck-all').addEventListener('click', () => {
  document.querySelectorAll('.ann-ai-check').forEach(cb => { cb.checked = false; });
});

$('ann-btn-confirm-ai').addEventListener('click', async () => {
  const checked = [...document.querySelectorAll('.ann-ai-check:checked')].map(cb => cb.dataset.id);
  annState.confirmedAiIds = new Set(checked);

  try {
    const resp = await fetch(`/api/annotate/${annState.sessionId}/confirm-ai`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirmed_ai_ids: checked }),
    });
    if (!resp.ok) {
      const d = await resp.json();
      throw new Error(d.detail || '保存失败');
    }
    showToast(`已确认 ${checked.length} 位 AI 作答受访者`, 'success');
    await annAfterAiConfirm();
  } catch (e) {
    showToast(`确认失败：${e.message}`, 'error');
  }
});

async function annAfterAiConfirm() {
  if (annState.tasks.quality) {
    annGoStep(5);
    await annRunQuality();
  } else {
    annGoStep(6);
    annShowDone();
  }
}

// ── ANN STEP 5: 质量打标 ───────────────────────────────────

async function annRunQuality() {
  const bar     = $('ann-quality-progress-bar');
  const msg     = $('ann-quality-progress-msg');
  const warnLog = $('ann-quality-warn-log');
  bar.style.width = '0%';
  msg.textContent = '正在连接…';
  warnLog.innerHTML = '';

  try {
    await consumeSSE(`/api/annotate/${annState.sessionId}/run-quality`, ev => {
      if (ev.type === 'progress') {
        const pct = ev.total > 0 ? Math.round((ev.done / ev.total) * 100) : 0;
        bar.style.width = `${pct}%`;
        msg.textContent = ev.msg || `${ev.done}/${ev.total} 批已完成`;
      }
      if (ev.type === 'warn') {
        const div = document.createElement('div');
        div.className = 'ann-warn-item';
        div.textContent = ev.msg;
        warnLog.appendChild(div);
      }
      if (ev.type === 'quality_done') {
        bar.style.width = '100%';
        msg.textContent = `质量打标完成，共 ${ev.count} 条结果`;
        annState.qualityCount = ev.count;
      }
    });
    annGoStep(6);
    annShowDone();
  } catch (e) {
    showToast(`质量打标失败：${e.message}`, 'error');
  }
}

// ── ANN STEP 6: 完成 ─────────────────────────────────────

function annShowDone() {
  const parts = [];
  if (annState.tasks.ai_detect) {
    parts.push(`AI 作答识别：${annState.aiResults.length} 条，${annState.confirmedAiIds.size} 位确认为 AI 作答`);
  }
  if (annState.tasks.quality) {
    parts.push(`质量打标：${annState.qualityCount} 条`);
  }
  $('ann-done-text').innerHTML = parts.join('<br>');
  showToast('所有标注任务完成！', 'success');
}

$('ann-btn-download').addEventListener('click', () => {
  window.location.href = `/api/annotate/${annState.sessionId}/download`;
});

$('ann-btn-restart').addEventListener('click', () => {
  if (!confirm('确定要重新标注吗？当前标注数据将被清除。')) return;
  annState.sessionId       = null;
  annState.currentStep     = 1;
  annState.headers         = [];
  annState.idCol           = 1;
  annState.openTextCols    = [];
  annState.tasks           = { ai_detect: false, quality: false };
  annState.aiResults       = [];
  annState.highProbResults = [];
  annState.confirmedAiIds  = new Set();
  annState.qualityCount    = 0;
  $('task-ai-detect').checked = false;
  $('task-quality').checked   = false;
  $('ann-background').value   = '';
  $('ann-background-block').style.display = 'none';
  annSyncTaskCards();
  annResetUploadZone();
  annFileInput.value = '';
  annGoStep(1);
  showToast('已重置，请重新上传文件', 'info');
});


// ── 上传说明文案 ──
fetch('/api/upload-guide')
  .then(r => r.json())
  .then(({ content }) => {
    const el = document.getElementById('upload-guide');
    if (el && content) el.innerHTML = marked.parse(content);
  })
  .catch(() => {});
