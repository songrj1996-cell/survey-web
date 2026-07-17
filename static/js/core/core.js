/* ============================================================
   问卷洞察 Survey Insight — core.js
   前端状态机 + SSE + 题型确认 + 主题 + 抽屉(设置/历史)
   ============================================================ */

'use strict';

// ── 配置 marked ──
marked.setOptions({ breaks: true, gfm: true });

// ── 全局状态 ──
const state = {
  sessionId: null,
  mode: null,         // null=定性(5步) | 'crosstab'=倍市得跑数表(4步)
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
  auditFilters: { start: '', end: '', user: '', feature: '' },
};
window.__surveyState = state;

// ── 题型选项（与后端 ROLE_LABEL_MAP 对齐）──
const ROLE_OPTIONS = [
  ['id', '用户 ID'],
  ['mlbbid', 'MLBB ID'],
  ['profile_dim', '画像维度'],
  ['single_choice', '单选题'],
  ['multi_choice', '多选题'],
  ['scale', '量表题'],
  ['matrix_scale', '矩阵打分'],
  ['matrix_multi', '矩阵多选'],
  ['open_text', '开放题'],
  ['ignore', '忽略此列'],
];
const MATRIX_ROLES = ['matrix_scale', 'matrix_multi'];
const CHOICE_ROLES = ['single_choice', 'profile_dim', 'multi_choice', 'matrix_multi'];

// ── DOM 引用 ──
const $ = id => document.getElementById(id);
const panels = [1, 2, 3, 4, 5].map(n => $(`panel-${n}`));

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
  if (!md) return '';
  // marked 对 Unicode 序号（①②…）、中文标点紧邻 ** 时不产生 <strong>。
  // 预处理：把所有 **非空内容** 先替换成 <strong>，再交给 marked（marked 会保留已有 HTML）。
  // 不处理 *** 三星（斜体加粗）以免误替换。
  const preprocessed = md.replace(/\*\*(?!\s)(.+?)(?<!\s)\*\*/gs,
    (_, inner) => `<strong>${inner}</strong>`
  );
  return marked.parse(preprocessed);
}

function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
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
  applyStepBarForMode();
}

// 跑数表(crosstab)模式:隐藏「数据确认」(step2),可见步骤重新编号 1..4
function applyStepBarForMode() {
  const crosstab = state.mode === 'crosstab';
  document.querySelectorAll('[data-survey-step="2"]').forEach(b => {
    b.style.display = crosstab ? 'none' : '';
  });
  const seq = crosstab ? [1, 3, 4, 5] : [1, 2, 3, 4, 5];
  document.querySelectorAll('.step-bar').forEach(bar => {
    seq.forEach((step, i) => {
      const btn = bar.querySelector(`[data-survey-step="${step}"]`);
      if (!btn) return;
      const num = btn.querySelector('.step-bar__num');
      if (num) num.textContent = crosstab ? (i + 1) : step;
    });
  });
}

function clearTransientSelection() {
  const selection = window.getSelection?.();
  if (selection?.rangeCount) selection.removeAllRanges();
}

function goStep(n) {
  clearTransientSelection();
  state.currentStep = n;
  state.viewStep = n;
  panels.forEach((p, i) => {
    const showing = i + 1 === n;
    p.classList.toggle('panel--hidden', !showing);
    p.classList.remove('panel--readonly');
  });
  renderStepBars();
  document.querySelector('.main').scrollTo({ top: 0, behavior: 'auto' });
}

function setViewStep(n) {
  if (n > state.currentStep) return;
  clearTransientSelection();
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
  document.querySelector('.main').scrollTo({ top: 0, behavior: 'auto' });
}

// ── 主题切换 ──

function applyTheme(theme) {
  if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light');
  else document.documentElement.removeAttribute('data-theme');
  try { localStorage.setItem('survey-theme', theme); } catch { }
}

(function initTheme() {
  let saved = 'light';
  try { saved = localStorage.getItem('survey-theme') || 'light'; } catch { }
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
        try {
          onEvent(data);
        } catch (callbackErr) {
          // onEvent（如 showPlanCard）渲染时抛的 JS 错不应被吞掉
          console.error('[SSE] onEvent error:', callbackErr);
          es.close();
          reject(callbackErr);
          return;
        }
        if (data.type === 'error') { es.close(); reject(new Error(data.message || data.msg || '服务端处理失败')); }
        if ([
          'columns_ready', 'plan_ready', 'report_done', 'qa_done',
          'ai_detect_done', 'quality_done', 'comment_preprocess_done',
          'comment_quotes_done', 'comment_quotes_error',
        ].includes(data.type)) {
          es.close(); resolve(data);
        }
      } catch (parseErr) {
        // JSON parse 失败：数据格式异常，记录但不中断流
        console.warn('[SSE] JSON parse error:', parseErr, e.data?.slice(0, 100));
      }
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
    try { detail = JSON.parse(text).detail || text; } catch { }
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

// ── 进度状态栏（长耗时阶段显示"最后更新时间 + 当前进度"）──
function _updateProgressStatus(msg) {
  const el = $('progress-status-text');
  if (!el) return;
  if (!msg) { el.textContent = ''; return; }
  const t = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  el.textContent = `${t}  ${msg}`;
}
