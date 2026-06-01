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
  columns: null,      // Step 2 题型数据
  planData: null,
  reportMd: null,
  qaLoading: false,
  viewMode: 'session', // 'session' | 'history'
  historyId: null,     // 当前查看/续聊的历史 id
};

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

// ── DOM 引用 ──
const $  = id => document.getElementById(id);
const panels   = [1,2,3,4,5].map(n => $(`panel-${n}`));
const navSteps = [1,2,3,4,5].map(n => $(`nav-step-${n}`));

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

function goStep(n) {
  state.currentStep = n;
  panels.forEach((p, i) => p.classList.toggle('panel--hidden', i + 1 !== n));
  navSteps.forEach((s, i) => {
    s.classList.remove('step--active', 'step--done');
    if (i + 1 === n)        s.classList.add('step--active');
    else if (i + 1 < n)     s.classList.add('step--done');
  });
  document.querySelector('.main').scrollTo({ top: 0, behavior: 'smooth' });
}

// ── 主题切换 ──

function applyTheme(theme) {
  if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light');
  else                   document.documentElement.removeAttribute('data-theme');
  try { localStorage.setItem('survey-theme', theme); } catch {}
}

(function initTheme() {
  let saved = 'dark';
  try { saved = localStorage.getItem('survey-theme') || 'dark'; } catch {}
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
        if (data.type === 'error') { es.close(); reject(new Error(data.message)); }
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

function columnRowHTML(c, i) {
  const opts = ROLE_OPTIONS.map(([val, label]) =>
    `<option value="${val}" ${val === c.role ? 'selected' : ''}>${label}</option>`
  ).join('');
  const name = c.name_zh || c.name || `列${(c.column_indexes || [])[0] ?? i}`;
  const isMatrix = MATRIX_ROLES.includes(c.role) || (c.column_indexes || []).length > 1;
  const matrixTag = isMatrix ? `<span class="col-row__tag">矩阵 · ${(c.column_indexes || []).length} 列</span>` : '';

  return `<div class="col-row" data-card="${i}">
    <span class="col-row__num">${i + 1}</span>
    <div class="col-row__main">
      <div class="col-row__name" title="${esc(name)}">${esc(name)}${matrixTag}</div>
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

  // 矩阵题：只读展示子项行
  if (MATRIX_ROLES.includes(role) && (c.rows || []).length) {
    bits.push(`<span class="col-extra-readonly">子项：${esc(c.rows.join(' / '))}</span>`);
  }

  if (role === 'multi_choice') {
    const delim = c.delimiter || '，';
    bits.push(`<span class="q-extra-inline">分隔符
      <input class="extra-input extra-input--sm" data-delim="${i}" value="${esc(delim)}" placeholder="，" /></span>`);
    if ((c.options || []).length) {
      bits.push(`<span class="col-extra-readonly" title="${esc(c.options.join(' / '))}">选项：${esc(shortName(c.options.join(' / '), 60))}</span>`);
    }
  } else if (role === 'matrix_multi') {
    if ((c.options || []).length) {
      bits.push(`<span class="col-extra-readonly" title="${esc(c.options.join(' / '))}">列选项：${esc(shortName(c.options.join(' / '), 60))}</span>`);
    }
  } else if (role === 'scale' || role === 'matrix_scale') {
    const mn = (c.scale_min ?? 1), mx = (c.scale_max ?? 5);
    bits.push(`<span class="q-extra-inline">量程
      <input class="extra-input extra-input--sm" type="number" data-smin="${i}" value="${mn}" />
      <span class="scale-sep">—</span>
      <input class="extra-input extra-input--sm" type="number" data-smax="${i}" value="${mx}" /></span>`);
  }

  box.innerHTML = bits.join('');
  box.style.display = bits.length ? 'flex' : 'none';
}

// 事件委托：题型下拉变化
$('col-list').addEventListener('change', e => {
  const sel = e.target.closest('.type-select');
  if (sel) {
    const i = +sel.dataset.card;
    state.columns[i].role = sel.value;
    updateExtra(i, sel.value);
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
      if (c.options) out.options = c.options;
    }
    if (role === 'matrix_multi') {
      if (c.options) out.options = c.options;
      if (c.delimiter) out.delimiter = c.delimiter;
    }
    if (role === 'scale' || role === 'matrix_scale') {
      const mnEl = document.querySelector(`[data-smin="${i}"]`);
      const mxEl = document.querySelector(`[data-smax="${i}"]`);
      out.scale_min = mnEl ? Number(mnEl.value) : (c.scale_min ?? 1);
      out.scale_max = mxEl ? Number(mxEl.value) : (c.scale_max ?? 5);
    }
    if (MATRIX_ROLES.includes(role) && c.rows) out.rows = c.rows;
    // 同义归并（LLM 识别，UI 只读透传）
    if (c.value_aliases && ['single_choice', 'profile_dim', 'multi_choice', 'matrix_multi'].includes(role)) {
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

$('btn-plan-ok').addEventListener('click', () => confirmPlan('ok'));
$('btn-plan-revise').addEventListener('click', () => {
  const txt = $('plan-input').value.trim();
  if (!txt) { showToast('请先输入修改意见', 'info'); return; }
  confirmPlan(txt);
});
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
        $('btn-plan-ok').disabled = false;
        $('btn-plan-revise').disabled = false;
        return;
      }
    }

    if (approved) {
      await runStats();
    }
  } catch (e) {
    showToast(`操作失败：${e.message}`, 'error');
    $('btn-plan-ok').disabled = false;
    $('btn-plan-revise').disabled = false;
  }
}

// ============================================================
// STEP 4: Stats + Report
// ============================================================

async function runStats() {
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
        const el = $('report-stream-content');
        el.textContent = fullReport;
        el.scrollTop = el.scrollHeight;
      }
      if (ev.type === 'report_done') {
        state.viewMode = 'session';
        state.historyId = null;
        showReport(ev.report_md);
      }
    });
  } catch (e) {
    showToast(`报告生成失败：${e.message}`, 'error');
  }
}

function showReport(md) {
  state.reportMd = md;
  goStep(5);

  const titleMatch = md.match(/^#\s+(.+?)$/m);
  $('report-title-display').textContent = titleMatch ? titleMatch[1].trim() : '分析报告';

  $('report-content').innerHTML = renderMarkdown(md);
  $('qa-messages').innerHTML = '';
  const lb = $('feishu-link-box'); if (lb) lb.remove();  // 清掉上一份报告的飞书链接
  if (state.viewMode === 'session') showToast('报告生成完毕！', 'success');
}

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

// ── 飞书登录状态 ──
state.feishu = { configured: false, logged_in: false, name: '' };

async function refreshFeishuStatus() {
  try {
    const r = await fetch('/api/feishu/me');
    state.feishu = await r.json();
  } catch { /* ignore */ }
  const label = $('feishu-login-label');
  if (label) {
    label.textContent = state.feishu.logged_in
      ? `飞书：${state.feishu.name || '已登录'}`
      : '登录飞书';
  }
}

$('btn-feishu-login').addEventListener('click', () => {
  if (!state.feishu.configured) {
    showToast('服务端未配置飞书应用（FEISHU_APP_ID/SECRET/REDIRECT_URI）', 'error');
    return;
  }
  if (state.feishu.logged_in) {
    showToast(`已登录飞书：${state.feishu.name || ''}`, 'info');
    return;
  }
  window.location.href = `/api/feishu/login?next=${encodeURIComponent(location.pathname)}`;
});

// ── 飞书文档导出 ──
$('btn-export-feishu').addEventListener('click', exportFeishu);

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
  const btn = $('btn-export-feishu');
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = '生成中…';

  const url = state.viewMode === 'history' && state.historyId
    ? `/api/export/feishu-history/${state.historyId}`
    : `/api/export/feishu/${state.sessionId}`;
  try {
    const resp = await fetch(url, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) {
      if (resp.status === 401) {
        showToast('飞书登录已过期，请重新登录', 'error');
        await refreshFeishuStatus();
      }
      throw new Error(data.detail || '生成失败');
    }
    showFeishuLink(data.url);
    try { await navigator.clipboard.writeText(data.url); showToast('飞书文档已生成，链接已复制', 'success'); }
    catch { showToast('飞书文档已生成', 'success'); }
  } catch (e) {
    showToast(`生成飞书文档失败：${e.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

function showFeishuLink(url) {
  let box = $('feishu-link-box');
  if (!box) {
    box = document.createElement('div');
    box.id = 'feishu-link-box';
    box.className = 'feishu-link-box';
    const reportBody = document.querySelector('#panel-5 .report-body');
    reportBody.parentNode.insertBefore(box, reportBody);
  }
  box.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
    飞书文档：<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>`;
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

  appendQABubble('user', question);
  const typingBubble = appendQABubble('ai', null, true);

  try {
    let answer = '';

    const url  = state.viewMode === 'history' ? '/api/history-qa' : '/api/qa';
    const body = state.viewMode === 'history'
      ? { history_id: state.historyId, question }
      : { session_id: state.sessionId, question };

    await consumeSSEPost(url, body, ev => {
      if (ev.type === 'chunk') {
        answer += ev.content;
        typingBubble.innerHTML = renderMarkdown(answer);
        typingBubble.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
      if (ev.type === 'qa_done') {
        typingBubble.innerHTML = renderMarkdown(ev.answer || answer);
      }
    });
  } catch (e) {
    typingBubble.textContent = `❌ ${e.message}`;
    showToast(`追问失败：${e.message}`, 'error');
  } finally {
    state.qaLoading = false;
    $('btn-qa-send').disabled = false;
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
// 设置抽屉（提示词管理）
// ============================================================

$('btn-open-settings').addEventListener('click', () => {
  openDrawer('settings-drawer');
  loadPrompts();
});

async function loadPrompts() {
  const body = $('settings-body');
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

$('settings-body').addEventListener('click', async e => {
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

    body.innerHTML = `<div class="hist-list">` + list.map(h => `
      <div class="hist-card" data-hist-id="${esc(h.id)}">
        <div class="hist-card__title">${esc(h.title)}</div>
        <div class="hist-card__meta">
          <span class="hist-card__file">${esc(h.filename)}</span>
          ${h.has_qa ? `<span class="hist-card__qa-badge">可续聊</span>` : ''}
          <span class="hist-card__time">${esc(formatTime(h.created_at))}</span>
        </div>
      </div>`).join('') + `</div>`;
  } catch (e) {
    body.innerHTML = `<div class="hist-empty">加载历史失败：${esc(e.message)}</div>`;
  }
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
  const card = e.target.closest('[data-hist-id]');
  if (!card) return;
  const id = card.dataset.histId;
  try {
    const resp = await fetch(`/api/history/${id}`);
    const entry = await resp.json();
    if (!resp.ok) throw new Error(entry.detail || '加载失败');

    state.viewMode  = 'history';
    state.historyId = id;
    state.reportMd  = entry.report_md;
    state.planData  = entry.plan || null;

    closeDrawer('history-drawer');
    showReport(entry.report_md);
    $('qa-input').placeholder = entry.analyst_conv_id
      ? '可基于该历史报告继续追问（Enter 发送）'
      : '该历史记录无可续聊的对话，仅供查看';
    $('qa-input').disabled  = !entry.analyst_conv_id;
    $('btn-qa-send').disabled = !entry.analyst_conv_id;
    showToast('已载入历史报告', 'success');
  } catch (err) {
    showToast(`载入失败：${err.message}`, 'error');
  }
});

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
  resetUploadZone();
  fileInput.value = '';
  $('qa-input').disabled = false;
  $('btn-qa-send').disabled = false;
  goStep(1);
  showToast('已重置，请重新上传文件', 'info');
});

// ── Init ──
goStep(1);
refreshFeishuStatus();

// ============================================================
// 模式切换（问卷分析 ↔ 数据标注）
// ============================================================

const surveyPanels = panels;            // panel-1 ~ panel-5
const surveyNav    = $('steps-nav');
const annNav       = $('ann-steps-nav');
const annPanelIds  = [1, 2, 3, 4, 5, 6];
const annPanels    = annPanelIds.map(n => $(`ann-panel-${n}`));
const annNavSteps  = annPanelIds.map(n => $(`ann-nav-step-${n}`));

let currentMode = 'survey'; // 'survey' | 'annotate'

function switchMode(mode) {
  currentMode = mode;
  const isSurvey = mode === 'survey';
  $('btn-mode-survey').classList.toggle('mode-tab--active', isSurvey);
  $('btn-mode-annotate').classList.toggle('mode-tab--active', !isSurvey);
  surveyNav.style.display  = isSurvey  ? '' : 'none';
  annNav.style.display     = !isSurvey ? '' : 'none';
  surveyPanels.forEach(p => p.classList.add('panel--hidden'));
  annPanels.forEach(p => p.classList.add('panel--hidden'));
  if (isSurvey) {
    goStep(state.currentStep);
  } else {
    annGoStep(annState.currentStep);
  }
}

$('btn-mode-survey').addEventListener('click',   () => switchMode('survey'));
$('btn-mode-annotate').addEventListener('click', () => switchMode('annotate'));

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
  annNavSteps.forEach((s, i) => {
    s.classList.remove('step--active', 'step--done');
    if (i + 1 === n)       s.classList.add('step--active');
    else if (i + 1 < n)    s.classList.add('step--done');
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
['task-ai-detect', 'task-quality'].forEach(id => {
  $(id).addEventListener('change', () => {
    annState.tasks.ai_detect = $('task-ai-detect').checked;
    annState.tasks.quality   = $('task-quality').checked;
    $('ann-background-block').style.display = annState.tasks.ai_detect ? '' : 'none';
    annUpdateStartBtn();
  });
});

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
  bar.style.width = '0%';
  msg.textContent = '正在连接…';
  warnLog.innerHTML = '';

  try {
    await consumeSSE(`/api/annotate/${annState.sessionId}/run-ai-detect`, ev => {
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
      if (ev.type === 'ai_detect_done') {
        bar.style.width = '100%';
        msg.textContent = `AI 检测完成，共 ${ev.results.length} 条结果`;
        annState.aiResults       = ev.results || [];
        annState.highProbResults = ev.high_prob || [];
      }
    });

    // 有高概率结果 → 跳到确认步
    if (annState.highProbResults.length > 0) {
      annRenderAiConfirm(annState.highProbResults);
      annGoStep(4);
    } else {
      showToast('未发现高概率 AI 作答（≥ 80%），自动跳过确认步骤', 'info');
      await annAfterAiConfirm();
    }
  } catch (e) {
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
