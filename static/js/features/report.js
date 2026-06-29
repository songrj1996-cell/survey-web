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
      if (ev.type === 'progress') {
        // 主观题聚类阶段：后端正在处理，前端显示实时进度避免用户以为卡死
        _updateProgressStatus(ev.message);
        const el = $('report-stream-content');
        if (el && !fullReport) {
          // 正文还空时，把进度显示在流式区，有正文后进度退到状态栏
          el.textContent = ev.message;
        }
      }
      if (ev.type === 'chunk') {
        // 收到正文后清掉进度占位
        if (!fullReport) $('report-stream-content').textContent = '';
        fullReport += ev.content;
        state.sessionReport.stream = fullReport;
        if (state.viewMode === 'session') {
          const el = $('report-stream-content');
          // 实时用 marked 渲染，避免 ** 等 markdown 符号显示为字面量
          el.innerHTML = renderMarkdown(fullReport);
          el.scrollTop = el.scrollHeight;
        }
      }
      if (ev.type === 'report_done') {
        _updateProgressStatus('');
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
  updateQaBadge();
}

function updateQaBadge() {
  const badge = $('qa-badge');
  if (!badge) return;
  const count = ($('qa-messages')?.querySelectorAll('.qa-message--user') || []).length;
  badge.textContent = count;
  badge.style.display = count > 0 ? '' : 'none';
}

function switchReportTab(name) {
  document.querySelectorAll('[data-report-tab]').forEach(btn => {
    btn.classList.toggle('report-tab__btn--active', btn.dataset.reportTab === name);
  });
  const reportPane = $('report-pane-report');
  const qaPane = $('report-pane-qa');
  if (reportPane) reportPane.classList.toggle('report-tab-pane--active', name === 'report');
  if (qaPane) qaPane.classList.toggle('report-tab-pane--active', name === 'qa');
  if (name === 'qa') {
    updateQaBadge();
    setTimeout(() => $('qa-input')?.focus(), 50);
  }
}

function renderReportBreadcrumb() {
  const el = $('report-breadcrumb');
  if (!el) return;
  const isCrosstab = state.mode === 'crosstab';
  const steps = isCrosstab
    ? [{ n: 1, label: '上传数据' }, { n: 3, label: '方案确认' }, { n: 4, label: '生成报告' }, { n: 5, label: '报告 & 追问' }]
    : [{ n: 1, label: '上传数据' }, { n: 2, label: '数据确认' }, { n: 3, label: '方案确认' }, { n: 4, label: '生成报告' }, { n: 5, label: '报告 & 追问' }];
  let html = '';
  steps.forEach(({ n, label }, i) => {
    const isActive = n === 5;
    const isDone = n < 5;
    let cls = 'report-toolbar__step';
    if (isDone) cls += ' report-toolbar__step--done report-toolbar__step--clickable';
    if (isActive) cls += ' report-toolbar__step--active';
    const displayNum = isCrosstab ? (i + 1) : n;
    html += `<span class="${cls}" data-step="${n}">${displayNum}. ${label}</span>`;
    if (i < steps.length - 1) html += `<span class="report-toolbar__step-sep"> / </span>`;
  });
  el.innerHTML = html;
  el.querySelectorAll('.report-toolbar__step--clickable').forEach(span => {
    span.addEventListener('click', () => {
      const n = +span.dataset.step;
      if (n <= state.currentStep) setViewStep(n);
    });
  });
}

document.querySelectorAll('[data-report-tab]').forEach(btn => {
  btn.addEventListener('click', () => switchReportTab(btn.dataset.reportTab));
});

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
  const li = $('feishu-link-inline');
  if (li) {
    li.innerHTML = ctx.feishuLinkHtml || '';
    li.style.display = ctx.feishuLinkHtml ? '' : 'none';
  }
  applyQAAvailability();
  updateReportContextSwitch();
  applyCoreHighlight();
  buildTOC();
  switchReportTab('report');
  renderReportBreadcrumb();
  updateQaBadge();
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
state.feishu = {
  configured: false, logged_in: false, allowed: true, name: '', email: '',
  perms: ['survey', 'annotate', 'comment'], is_admin: false
};

function applyPermGating() {
  const perms = state.feishu.perms || [];
  const hasSurvey = perms.includes('survey');
  const hasAnnotate = perms.includes('annotate');
  const hasComment = perms.includes('comment');
  // 侧边栏：无权限则隐藏入口
  const navSurvey = $('nav-survey');
  const navAnnotate = $('nav-annotate');
  const navComment = $('nav-comment');
  if (navSurvey) navSurvey.style.display = hasSurvey ? '' : 'none';
  if (navAnnotate) navAnnotate.style.display = hasAnnotate ? '' : 'none';
  if (navComment) navComment.style.display = hasComment ? '' : 'none';
  // 如果当前模式无权限，切换到第一个有权限的模式
  const allowedModes = [
    ['survey', hasSurvey], ['annotate', hasAnnotate], ['comment', hasComment],
  ].filter(([, ok]) => ok).map(([m]) => m);
  if (!allowedModes.includes(currentMode) && allowedModes.length) {
    switchMode(allowedModes[0]);
  }
  // 非管理员隐藏整个「设置」入口
  const navSettings = $('nav-settings');
  if (navSettings) navSettings.style.display = state.feishu.is_admin ? '' : 'none';
  // 管理员才显示权限配置 tab
  const permNav = $('stab-perms-nav');
  if (permNav) permNav.style.display = state.feishu.is_admin ? '' : 'none';
  const systemNav = $('stab-system-nav');
  if (systemNav) systemNav.style.display = state.feishu.is_admin ? '' : 'none';
  const auditNav = $('stab-audit-nav');
  if (auditNav) auditNav.style.display = state.feishu.is_admin ? '' : 'none';
  const adminLabel = $('settings-nav-admin-label');
  if (adminLabel) adminLabel.style.display = state.feishu.is_admin ? '' : 'none';
  const adminSep = $('settings-nav-admin-sep');
  if (adminSep) adminSep.style.display = state.feishu.is_admin ? '' : 'none';
}

async function refreshFeishuStatus() {
  try {
    const r = await fetch('/api/feishu/me');
    state.feishu = await r.json();
  } catch { /* ignore */ }
  const label = $('feishu-login-label');
  if (label) {
    label.textContent = state.feishu.logged_in
      ? `飞书：${state.feishu.name || state.feishu.email || '已登录'}`
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
    } catch { }
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
        <div style="font-size:15px;font-weight:600;color:var(--text)">导出飞书文档</div>
        <div style="font-size:13px;color:var(--text-2);line-height:1.7">
          系统会把当前报告创建为飞书文档（docx），归属于
          <strong style="color:var(--text)">${esc(email)}</strong>，并通过机器人发送文档链接。
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
  btn.textContent = '导出中…';

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
    try { await navigator.clipboard.writeText(data.url); showToast('飞书文档已创建，链接已复制，机器人消息已发送', 'success'); }
    catch { showToast('飞书文档已创建，机器人消息已发送', 'success'); }
  } catch (e) {
    showToast(`导出飞书文档失败：${e.message}`, 'error', 10000);
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

function showFeishuLink(url) {
  const oldBox = $('feishu-link-box');
  if (oldBox) oldBox.remove();
  const inline = $('feishu-link-inline');
  if (inline) {
    inline.style.display = '';
    inline.innerHTML = `<a href="${esc(url)}" target="_blank" rel="noopener"
      style="display:flex;align-items:center;gap:6px;padding:6px 14px 8px;font-size:12px;color:var(--accent);text-decoration:none">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
        <polyline points="14 2 14 8 20 8"/>
      </svg>✓ 查看飞书文档 →</a>`;
    activeReportCtx().feishuLinkHtml = inline.innerHTML;
  }
}

// Export dropdown toggle
$('btn-export-dropdown').addEventListener('click', e => {
  e.stopPropagation();
  $('export-dropdown-menu').classList.toggle('open');
});
document.addEventListener('click', e => {
  const dropdown = $('export-dropdown');
  const menu = $('export-dropdown-menu');
  if (menu && dropdown && !dropdown.contains(e.target)) menu.classList.remove('open');
});
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

    const url = qaMode === 'history' ? '/api/history-qa' : '/api/qa';
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
  if (!isTyping) updateQaBadge();
  return bubble;
}

