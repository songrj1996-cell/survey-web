// ============================================================
// 历史记录抽屉
// ============================================================

const historyState = {
  all: [],
  filters: {
    type: 'all',
    from: '',
    to: '',
  },
};

function openHistoryDrawer() {
  openDrawer('history-drawer');
  loadHistory();
}

$('btn-open-history').addEventListener('click', openHistoryDrawer);

async function loadHistory() {
  const body = $('history-body');
  body.innerHTML = `<div class="hist-empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const resp = await fetch('/api/history');
    const list = await resp.json();
    if (!resp.ok) throw new Error((list && list.detail) || '加载失败');
    historyState.all = Array.isArray(list) ? list : [];
    renderHistoryPanel();
  } catch (e) {
    body.innerHTML = `<div class="hist-empty">加载历史失败：${esc(e.message)}</div>`;
  }
}

function renderHistoryPanel() {
  const body = $('history-body');
  const list = historyState.all || [];
  if (!list.length) {
    body.innerHTML = `<div class="hist-empty">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg>
        暂无历史记录，生成报告后会自动保存最近 20 份
      </div>`;
    return;
  }

  const filtered = filterHistoryList(list);
  body.innerHTML = `
    ${renderHistoryFilters(filtered.length, list.length)}
    ${filtered.length
      ? `<div class="hist-list">` + filtered.map(renderHistoryCard).join('') + `</div>`
      : `<div class="hist-empty">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg>
          没有符合筛选条件的历史记录
        </div>`}`;
  bindHistoryFilters();
}

function renderHistoryFilters(count, total) {
  const f = historyState.filters;
  return `
    <div class="hist-filters">
      <div class="hist-filter hist-filter--type">
        <span>报告类型</span>
        <div class="hist-type-filter" role="group" aria-label="报告类型筛选">
          <button type="button" class="${f.type === 'all' ? 'active' : ''}" data-hist-filter-type="all">全部类型</button>
          <button type="button" class="${f.type === 'survey' ? 'active' : ''}" data-hist-filter-type="survey">问卷分析</button>
          <button type="button" class="${f.type === 'comment' ? 'active' : ''}" data-hist-filter-type="comment">评论分析</button>
          <button type="button" class="${f.type === 'annotate' ? 'active' : ''}" data-hist-filter-type="annotate">数据标注</button>
        </div>
      </div>
      <label class="hist-filter">
        <span>生成时间</span>
        <input type="date" data-hist-filter-from value="${esc(f.from)}">
      </label>
      <label class="hist-filter hist-filter--compact">
        <span>至</span>
        <input type="date" data-hist-filter-to value="${esc(f.to)}">
      </label>
      <button class="hist-filter__clear" type="button" data-hist-filter-clear>重置</button>
      <div class="hist-filter__count">显示 ${esc(count)} / ${esc(total)} 份</div>
    </div>`;
}

function bindHistoryFilters() {
  const types = document.querySelectorAll('[data-hist-filter-type]');
  const from = document.querySelector('[data-hist-filter-from]');
  const to = document.querySelector('[data-hist-filter-to]');
  const clear = document.querySelector('[data-hist-filter-clear]');
  types.forEach(btn => btn.addEventListener('click', e => {
    e.stopPropagation();
    historyState.filters.type = btn.dataset.histFilterType || 'all';
    renderHistoryPanel();
  }));
  if (from) from.addEventListener('change', () => {
    historyState.filters.from = from.value;
    renderHistoryPanel();
  });
  if (to) to.addEventListener('change', () => {
    historyState.filters.to = to.value;
    renderHistoryPanel();
  });
  if (clear) clear.addEventListener('click', e => {
    e.stopPropagation();
    historyState.filters = { type: 'all', from: '', to: '' };
    renderHistoryPanel();
  });
}

function filterHistoryList(list) {
  const { type, from, to } = historyState.filters;
  return list.filter(item => {
    const itemType = historyTypeKey(item.mode);
    if (type !== 'all' && itemType !== type) return false;
    const date = historyDateKey(item.created_at);
    if ((from || to) && !date) return false;
    if (from && date && date < from) return false;
    if (to && date && date > to) return false;
    return true;
  });
}

function historyDateKey(value) {
  if (!value) return '';
  const d = new Date(value);
  if (!Number.isNaN(d.getTime())) {
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }
  return String(value).slice(0, 10);
}

function historyTypeKey(mode) {
  if (mode === 'comment') return 'comment';
  if (mode === 'annotate') return 'annotate';
  return 'survey';
}

function renderHistoryCard(h) {
  const isActive = state.viewMode === 'history' && state.historyId === h.id;
  const reportNo = String(h.report_no || '').trim()
    || (h.id ? `R-${String(h.id).slice(0, 4).toUpperCase()}` : 'R-?');
  const source = historySourceMeta(h.mode);
  const qa = historyQaMeta(h);
  const quantity = historyQuantityText(h);
  const action = h.mode === 'annotate'
    ? `<button class="hist-card__edit hist-card__download" type="button" data-hist-download title="下载标注结果" aria-label="下载标注结果">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          <span>下载</span>
        </button>`
    : `<button class="hist-card__edit" type="button" data-hist-edit title="修改报告名称" aria-label="修改报告名称">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 20h9"/>
            <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>
          </svg>
          <span>改名</span>
        </button>`;
  return `
    <div class="hist-card hist-card--${esc(source.key)}${isActive ? ' hist-card--active' : ''}" data-hist-id="${esc(h.id)}" data-hist-mode="${esc(h.mode || '')}">
      <div class="hist-card__top">
        <span class="hist-card__no">${esc(reportNo)}</span>
        <span class="hist-card__source hist-card__source--${esc(source.key)}">${esc(source.label)}</span>
      </div>
      <div class="hist-card__title-row">
        <div class="hist-card__title" data-hist-title>${esc(h.title)}</div>
      </div>
      <div class="hist-card__meta-list">
        <div class="hist-card__meta" title="${esc(h.filename || '文件未记录')}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
          <span>${esc(h.filename || '文件未记录')}</span>
        </div>
        <div class="hist-card__meta">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-7"/></svg>
          <span>${esc(quantity)}</span>
        </div>
        <div class="hist-card__meta">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
          <span>${esc(formatTime(h.created_at))}</span>
        </div>
      </div>
      <div class="hist-card__foot">
        <span class="hist-card__qa hist-card__qa--${esc(qa.key)}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>
          ${esc(qa.label)}
        </span>
        ${action}
      </div>
    </div>`;
}

function historySourceMeta(mode) {
  if (mode === 'comment') return { key: 'comment', label: '评论分析' };
  if (mode === 'annotate') return { key: 'annotate', label: '数据标注' };
  if (mode === 'crosstab') return { key: 'survey', label: '问卷分析' };
  return { key: 'survey', label: '问卷分析' };
}

function historyQaMeta(h) {
  if (h.mode === 'comment') return { key: 'disabled', label: '无追问功能' };
  if (h.mode === 'annotate') return { key: 'disabled', label: '无追问功能' };
  if (Number(h.qa_count || 0) > 0) return { key: 'done', label: '已追问' };
  return { key: 'pending', label: '未追问' };
}

function historyQuantityText(h) {
  if (h.mode === 'comment') {
    return `有效 ${h.comment_valid_count || 0} 条`;
  }
  const rowCount = Number(h.row_count || h.total_rows || 0);
  if (h.mode === 'annotate') return rowCount > 0 ? `标注 ${rowCount} 行样本` : '数量未记录';
  return rowCount > 0 ? `有效样本 ${rowCount} 份` : '有效样本未记录';
}

function formatTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch { return iso; }
}

$('history-body').addEventListener('click', async e => {
  const downloadBtn = e.target.closest('[data-hist-download]');
  if (downloadBtn) {
    e.stopPropagation();
    const card = downloadBtn.closest('[data-hist-id]');
    if (card?.dataset.histId) {
      window.location.href = `/api/annotate-history/${card.dataset.histId}/download`;
    }
    return;
  }
  const editBtn = e.target.closest('[data-hist-edit]');
  if (editBtn) {
    e.stopPropagation();
    startHistoryTitleEdit(editBtn.closest('[data-hist-id]'));
    return;
  }
  if (e.target.closest('.hist-card-title-edit, .hist-card-title-action')) return;
  const card = e.target.closest('[data-hist-id]');
  if (!card) return;
  if (card.dataset.histMode === 'annotate') {
    showToast('数据标注记录没有预览，请点击下载按钮获取 Excel', 'info');
    return;
  }
  const id = card.dataset.histId;
  await openHistoryEntry(id);
});

async function openHistoryEntry(id) {
  try {
    const resp = await fetch(`/api/history/${id}`);
    const entry = await resp.json();
    if (!resp.ok) throw new Error(entry.detail || '加载失败');

    if (entry.mode === 'comment') {
      switchMode('comment');
      cmState.sessionId = id;
      cmState.historyId = id;
      cmState.result = entry.comment_result || {
        report_md: entry.report_md,
        title: entry.title,
        post_title: entry.comment_post_title || '',
        themes: [],
        other_themes: [],
        sentiment_overall: {},
        sample_meta: entry.comment_sample_meta || {},
      };
      cmState.result.title = cmState.result.title || entry.title;
      cmState.result.post_title = cmState.result.post_title || entry.comment_post_title || '';
      cmState.preprocess = entry.comment_sample_meta || null;
      closeDrawer('history-drawer');
      cmRenderResult(cmState.result);
      cmGoStep(3);
      showToast('已载入评论历史报告', 'success');
      return;
    }

    saveActiveReportUi();
    state.viewMode = 'history';
    state.historyId = id;
    state.historyReport.id = id;
    state.historyReport.reportNo = entry.report_no || '';
    state.historyReport.reportMd = entry.report_md;
    state.historyReport.title = entry.title || reportTitleFromMarkdown(entry.report_md);
    state.historyReport.createdAt = entry.created_at || '';
    state.historyReport.mode = entry.mode || 'survey';
    state.historyReport.analystConvId = entry.analyst_conv_id || null;
    state.historyReport.planData = entry.plan || null;
    state.historyReport.qaMessages = normalizeQAMessages(entry.qa_messages);
    state.historyReport.qaHtml = '';
    state.historyReport.feishuLinkHtml = '';

    // 切回问卷/报告模式：隐藏可能残留的评论分析/标注面板，避免与报告工作区叠加显示
    switchMode('survey');
    closeDrawer('history-drawer');
    renderReportWorkspace(entry.report_md, { preserveQa: true });
    showToast('已载入历史报告', 'success');
  } catch (err) {
    showToast(`载入失败：${err.message}`, 'error');
  }
}

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
