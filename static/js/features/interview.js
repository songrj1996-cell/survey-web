// 访谈报告：多 Sheet 证据归并、真实模块进度、历史与导出。
const ivPanels = [1, 2, 3].map(n => $(`iv-panel-${n}`));
const ivState = {
  currentStep: 1,
  selectedFile: null,
  sessionId: null,
  filename: '',
  sheets: [],
  reportMd: '',
  reportNo: '',
  fromHistory: false,
  running: false,
  eventSource: null,
  audit: {},
  reviewQueue: [],
  reviewActive: null,
  reviewJobs: new Map(),
  stageModels: {},
};

const IV_STAGE_ORDER = ['upload', 'extract', 'write', 'audit', 'completed'];

function ivRenderStepBars() {
  const canReviewAll = Boolean(ivState.reportMd) && !ivState.fromHistory;
  document.querySelectorAll('[data-iv-step]').forEach(button => {
    const step = Number(button.dataset.ivStep);
    button.classList.remove('step-bar__item--active', 'step-bar__item--done');
    if (step === ivState.currentStep) {
      button.classList.add('step-bar__item--active');
    } else if (canReviewAll) {
      button.classList.add('step-bar__item--done');
    }
    button.disabled = !canReviewAll && step !== ivState.currentStep;
  });
}

function ivGoStep(step) {
  ivState.currentStep = step;
  ivPanels.forEach((panel, index) => {
    if (panel) panel.classList.toggle('panel--hidden', index + 1 !== step);
  });
  const reviewingUpload = step === 1 && Boolean(ivState.reportMd);
  $('iv-panel-1')?.classList.toggle('iv-panel--review', reviewingUpload);
  if ($('iv-research-focus')) $('iv-research-focus').readOnly = reviewingUpload;
  ivRenderStepBars();
  document.querySelector('.main')?.scrollTo({ top: 0, behavior: 'smooth' });
}

function ivPreviewStep(step) {
  if (step === ivState.currentStep) return;
  if (ivState.fromHistory) {
    showToast('历史记录只保留最终报告，无法回看当时的上传与生成页面', 'info');
    return;
  }
  if (!ivState.reportMd) return;
  if (step === 1 && !ivState.selectedFile) {
    showToast('当前浏览器没有保留原始文件预览，可查看生成过程和最终报告', 'info');
    return;
  }
  ivGoStep(step);
}

function ivTitleFromMarkdown(md) {
  const match = String(md || '').match(/^#\s+(.+?)$/m);
  return match ? match[1].trim() : '访谈报告';
}

function ivFormatSize(bytes) {
  if (!Number.isFinite(bytes)) return '';
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function ivResetStages() {
  document.querySelectorAll('#iv-stage-list .iv-stage').forEach((row, index) => {
    row.classList.remove('iv-stage--active', 'iv-stage--done', 'iv-stage--error');
    const mark = row.querySelector('.iv-stage__mark');
    if (mark) mark.textContent = String(index + 1);
  });
  $('iv-module-progress-text').textContent = '等待识别报告模块';
  ivState.stageModels = {};
  document.querySelectorAll('.iv-stage__model').forEach(label => {
    label.hidden = true;
    label.textContent = '';
    label.classList.remove('iv-stage__model--fallback');
  });
}

function ivModelStage(stage) {
  const canonical = ivCanonicalStage(stage);
  return ['extract', 'write', 'audit'].includes(canonical) ? canonical : '';
}

function ivSetStageModel(stage, model, { fallback = false, summary = false } = {}) {
  const canonical = ivModelStage(stage);
  const cleanModel = String(model || '').trim();
  if (!canonical || !cleanModel) return;
  const label = $(`iv-model-${canonical}`);
  if (!label) return;
  const used = ivState.stageModels[canonical] || [];
  if (!used.includes(cleanModel)) used.push(cleanModel);
  ivState.stageModels[canonical] = used;
  label.hidden = false;
  label.classList.toggle('iv-stage__model--fallback', fallback);
  label.textContent = summary
    ? `已使用：${used.join('、')}`
    : `${fallback ? '备用模型' : '当前模型'}：${cleanModel}`;
}

function ivApplyModelsUsed(modelsUsed) {
  const grouped = { extract: [], write: [], audit: [] };
  Object.entries(modelsUsed || {}).forEach(([key, model]) => {
    let stage = '';
    if (key === 'extract') stage = 'extract';
    else if (key.startsWith('audit_') || key.includes('_audit_repair_')
      || key.startsWith('manual_audit_')) stage = 'audit';
    else if (key.startsWith('module_') || key.startsWith('manual_repair_')) stage = 'write';
    const cleanModel = String(model || '').trim();
    if (stage && cleanModel && !grouped[stage].includes(cleanModel)) {
      grouped[stage].push(cleanModel);
    }
  });
  Object.entries(grouped).forEach(([stage, models]) => {
    models.forEach(model => ivSetStageModel(stage, model, { summary: true }));
  });
}

function ivSetLiveState(status) {
  const pulse = document.querySelector('#iv-panel-2 .iv-live-card__pulse');
  if (!pulse) return;
  pulse.classList.remove('iv-live-card__pulse--error', 'iv-live-card__pulse--done');
  if (status === 'error') {
    pulse.classList.add('iv-live-card__pulse--error');
    pulse.innerHTML = '<i></i>已停止';
    if (!$('iv-live-preview').textContent.trim()) {
      $('iv-live-placeholder').hidden = false;
      $('iv-live-placeholder').innerHTML =
        '<div class="iv-live-stop-icon">!</div><p>生成已停止，已完成的进度会保留，可从左侧继续。</p>';
    }
    return;
  }
  if (status === 'done') {
    pulse.classList.add('iv-live-card__pulse--done');
    pulse.innerHTML = '<i></i>已完成';
    return;
  }
  pulse.innerHTML = '<i></i>处理中';
  if (!$('iv-live-preview').textContent.trim()) {
    $('iv-live-placeholder').hidden = false;
    $('iv-live-placeholder').innerHTML =
      '<div class="spinner"></div><p>完成第一个模块后，将在这里显示报告内容</p>';
  }
}

function ivReset() {
  if (ivState.eventSource) ivState.eventSource.close();
  ivState.currentStep = 1;
  ivState.selectedFile = null;
  ivState.sessionId = null;
  ivState.filename = '';
  ivState.sheets = [];
  ivState.reportMd = '';
  ivState.reportNo = '';
  ivState.fromHistory = false;
  ivState.running = false;
  ivState.eventSource = null;
  ivState.audit = {};
  ivState.reviewQueue = [];
  ivState.reviewActive = null;
  ivState.reviewJobs.clear();
  $('iv-file-input').value = '';
  $('iv-research-focus').value = '';
  $('iv-selected-file').hidden = true;
  $('iv-upload-empty').hidden = false;
  $('iv-upload-zone').classList.remove('drag-over', 'upload-zone--filled');
  $('iv-start-report').disabled = true;
  $('iv-start-report').classList.remove('btn--loading');
  $('iv-progress-message').textContent = '正在上传并解析访谈记录…';
  $('iv-file-summary').textContent = '';
  $('iv-live-preview').innerHTML = '';
  $('iv-live-placeholder').hidden = false;
  $('iv-progress-actions').hidden = true;
  $('iv-report-content').innerHTML = '';
  $('iv-report-toc-list').innerHTML = '';
  $('iv-report-meta').textContent = '';
  $('iv-report-title').textContent = '访谈报告';
  $('iv-review-panel').hidden = true;
  $('iv-review-list').innerHTML = '';
  $('iv-quality-badge').textContent = '✓ 证据复审通过';
  $('iv-quality-badge').disabled = true;
  $('iv-quality-badge').classList.remove('iv-quality-badge--warning');
  $('iv-quality-badge').setAttribute('aria-expanded', 'false');
  ivSetLiveState('running');
  ivSetPercent(0);
  ivResetStages();
  ivGoStep(1);
}

function ivSelectFile(file) {
  if (!file || !/\.xlsx$/i.test(file.name || '')) {
    showToast('访谈报告仅支持 .xlsx 文件', 'error');
    return;
  }
  if (file.size > 50 * 1024 * 1024) {
    showToast('文件超过 50MB 上限', 'error');
    return;
  }
  ivState.selectedFile = file;
  $('iv-selected-file-name').textContent = `已选择文件：${file.name}`;
  $('iv-selected-file-size').textContent = `${ivFormatSize(file.size)} · 当前仅保留 1 个文件，点击此区域可重新选择`;
  $('iv-upload-empty').hidden = true;
  $('iv-selected-file').hidden = false;
  $('iv-upload-zone').classList.add('upload-zone--filled');
  $('iv-start-report').disabled = false;
}

function ivRemoveFile() {
  ivState.selectedFile = null;
  $('iv-file-input').value = '';
  $('iv-selected-file').hidden = true;
  $('iv-upload-empty').hidden = false;
  $('iv-upload-zone').classList.remove('drag-over', 'upload-zone--filled');
  $('iv-start-report').disabled = true;
}

function ivSetPercent(percent) {
  const safe = Math.max(0, Math.min(100, Number(percent) || 0));
  $('iv-progress-percent').textContent = `${Math.round(safe)}%`;
  $('iv-progress-bar-fill').style.width = `${safe}%`;
  $('iv-progress-bar').setAttribute('aria-valuenow', String(Math.round(safe)));
}

function ivCanonicalStage(stage) {
  if (stage === 'extract_done') return 'extract';
  if (stage === 'module_repair') return 'write';
  if (stage === 'repair') return 'audit';
  return IV_STAGE_ORDER.includes(stage) ? stage : 'upload';
}

function ivUpdateStage(stage, isError = false) {
  const canonical = ivCanonicalStage(stage);
  const activeIndex = IV_STAGE_ORDER.indexOf(canonical);
  document.querySelectorAll('#iv-stage-list .iv-stage').forEach((row, index) => {
    row.classList.remove('iv-stage--active', 'iv-stage--done', 'iv-stage--error');
    const mark = row.querySelector('.iv-stage__mark');
    if (index < activeIndex || canonical === 'completed') {
      row.classList.add('iv-stage--done');
      if (mark) mark.textContent = '✓';
    } else if (index === activeIndex) {
      row.classList.add(isError ? 'iv-stage--error' : 'iv-stage--active');
      if (mark) mark.textContent = isError ? '!' : String(index + 1);
    } else if (mark) {
      mark.textContent = String(index + 1);
    }
  });
}

function ivApplyProgress(data) {
  ivSetPercent(data.percent);
  ivUpdateStage(data.stage);
  if (data.message) $('iv-progress-message').textContent = data.message;
  if (data.total_modules) {
    const canonicalStage = ivCanonicalStage(data.stage);
    const completed = canonicalStage === 'write'
      ? Math.max(0, Number(data.module_index || 1) - 1)
      : Number(data.module_index || data.total_modules || 0);
    $('iv-module-progress-text').textContent =
      data.module_title
        ? `${Math.min(completed, data.total_modules)}/${data.total_modules} 已完成 · 当前：${data.module_title}`
        : `${Math.min(completed, data.total_modules)}/${data.total_modules} 个模块已完成`;
  }
}

function ivRenderPartial(markdown) {
  if (!markdown) return;
  $('iv-live-placeholder').hidden = true;
  $('iv-live-preview').innerHTML = renderMarkdown(markdown);
  $('iv-live-preview').scrollTop = $('iv-live-preview').scrollHeight;
}

function ivBuildToc() {
  const content = $('iv-report-content');
  const list = $('iv-report-toc-list');
  list.innerHTML = '';
  const headings = Array.from(content.querySelectorAll('h2'));
  $('iv-report-toc').style.display = headings.length ? '' : 'none';
  headings.forEach((heading, index) => {
    heading.dataset.ivModuleTitle = heading.textContent.trim();
    heading.id = `iv-module-${index + 1}`;
    const li = document.createElement('li');
    const link = document.createElement('a');
    link.className = 'report-toc__link--h2';
    link.href = `#${heading.id}`;
    link.textContent = heading.textContent;
    link.addEventListener('click', event => {
      event.preventDefault();
      heading.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    li.appendChild(link);
    list.appendChild(li);
  });
}

function ivAuditIssues() {
  const issues = ivState.audit?.issues;
  return Array.isArray(issues) ? issues : [];
}

function ivIssueConfirmed(issue) {
  return issue?.review_status === 'confirmed';
}

function ivAuditIssueBaseKey(issue) {
  return [
    issue?.module_title,
    issue?.problem,
    issue?.suggestion,
  ].map(value => String(value || '').trim()).join('\u241f');
}

function ivAuditIssueKey(issue, issueIndex, issues = ivAuditIssues()) {
  const baseKey = ivAuditIssueBaseKey(issue);
  let occurrence = 0;
  for (let index = 0; index < issueIndex; index += 1) {
    if (ivAuditIssueBaseKey(issues[index]) === baseKey) occurrence += 1;
  }
  return `${baseKey}\u241e${occurrence}`;
}

function ivReviewJobForIssue(issue, issueIndex, issues = ivAuditIssues()) {
  return ivState.reviewJobs.get(ivAuditIssueKey(issue, issueIndex, issues)) || null;
}

function ivReviewJobStateText(job) {
  if (!job) return '';
  if (job.state === 'queued') {
    return job.action === 'confirm' ? '等待确认' : '等待修订';
  }
  return job.action === 'confirm' ? '保存中' : '修订中';
}

function ivModuleHeading(moduleTitle) {
  const title = String(moduleTitle || '').trim();
  return Array.from($('iv-report-content').querySelectorAll('h2'))
    .find(heading => heading.dataset.ivModuleTitle === title);
}

function ivToggleReviewPanel(open) {
  const panel = $('iv-review-panel');
  const badge = $('iv-quality-badge');
  if (!ivAuditIssues().length) open = false;
  panel.hidden = !open;
  badge.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function ivScrollToAuditIssue(issueIndex) {
  const card = $('iv-report-content').querySelector(
    `[data-iv-audit-index="${issueIndex}"]`
  );
  if (!card) {
    showToast('该提醒没有匹配到报告模块，请查看顶部提醒详情', 'info');
    return;
  }
  ivToggleReviewPanel(false);
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  card.classList.remove('iv-module-review-card--focus');
  requestAnimationFrame(() => card.classList.add('iv-module-review-card--focus'));
}

function ivRenderAuditReview() {
  const content = $('iv-report-content');
  content.querySelectorAll('.iv-module-review-card, .iv-module-review-label')
    .forEach(element => element.remove());

  const issues = ivAuditIssues();
  const pending = issues
    .map((issue, index) => ({ issue, index }))
    .filter(({ issue }) => !ivIssueConfirmed(issue));
  const badge = $('iv-quality-badge');
  badge.disabled = !issues.length;
  badge.classList.toggle('iv-quality-badge--warning', pending.length > 0);
  if (!issues.length) {
    badge.textContent = '✓ 证据复审通过';
    $('iv-review-list').innerHTML = '';
    ivToggleReviewPanel(false);
    return;
  }
  badge.textContent = pending.length
    ? `⚠ 自动审校完成，${pending.length} 项待确认`
    : `✓ ${issues.length} 项审校提醒已确认`;
  $('iv-review-summary').textContent = pending.length
    ? `还有 ${pending.length} 项内容建议人工确认`
    : '全部提醒均已确认，原始审校记录仍会保留';

  $('iv-review-list').innerHTML = issues.map((issue, index) => {
    const confirmed = ivIssueConfirmed(issue);
    const job = ivReviewJobForIssue(issue, index, issues);
    const stateText = confirmed ? '已确认' : (ivReviewJobStateText(job) || '待确认');
    return `
      <div class="iv-review-list__item" data-iv-review-jump="${index}" role="button" tabindex="0">
        <strong>${esc(issue.module_title || '未关联模块')}</strong>
        <p>${esc(issue.problem || issue.suggestion || '建议人工确认当前表述')}</p>
        <span class="iv-review-state${confirmed ? ' iv-review-state--confirmed' : ''}">
          ${stateText}
        </span>
      </div>
    `;
  }).join('');

  const headingStates = new Map();
  issues.forEach((issue, index) => {
    const heading = ivModuleHeading(issue.module_title);
    if (!heading) return;
    const confirmed = ivIssueConfirmed(issue);
    const job = ivReviewJobForIssue(issue, index, issues);
    const stateText = confirmed ? '已确认' : (ivReviewJobStateText(job) || '待确认');
    const existingState = headingStates.get(heading);
    headingStates.set(heading, existingState === false ? false : confirmed);

    const card = document.createElement('aside');
    card.className = [
      'iv-module-review-card',
      confirmed ? 'iv-module-review-card--confirmed' : '',
      job ? 'iv-module-review-card--busy' : '',
    ].filter(Boolean).join(' ');
    card.dataset.ivAuditIndex = String(index);
    card.innerHTML = `
      <div class="iv-module-review-card__head">
        <strong>${confirmed ? '审校提醒已确认' : '自动审校 · 待确认'}</strong>
        <span class="iv-review-state${confirmed ? ' iv-review-state--confirmed' : ''}">
          ${stateText}
        </span>
      </div>
      <p><strong>问题：</strong>${esc(issue.problem || '建议人工确认当前表述。')}</p>
      ${issue.suggestion ? `<p><strong>建议：</strong>${esc(issue.suggestion)}</p>` : ''}
      ${confirmed ? '' : `
        <div class="iv-module-review-card__actions">
          <button class="btn btn--ghost btn--sm" type="button"
            data-iv-review-action="confirm" data-iv-audit-index="${index}"${job ? ' disabled' : ''}>
            确认当前内容
          </button>
          <button class="btn btn--primary btn--sm" type="button"
            data-iv-review-action="revise" data-iv-audit-index="${index}"${job ? ' disabled' : ''}>
            按建议重新修订
          </button>
        </div>
        <div class="iv-module-review-card__progress">${esc(job?.message || '正在准备修订…')}</div>
      `}
    `;
    heading.insertAdjacentElement('afterend', card);
  });

  headingStates.forEach((confirmed, heading) => {
    const label = document.createElement('span');
    label.className = `iv-module-review-label${confirmed ? ' iv-module-review-label--confirmed' : ''}`;
    label.textContent = confirmed ? '已确认' : '待确认';
    heading.appendChild(label);
  });
}

function ivRenderReport(data, { fromHistory = false } = {}) {
  ivState.reportMd = data.report_md || '';
  ivState.filename = data.filename || ivState.filename;
  ivState.sheets = data.sheets || ivState.sheets;
  ivState.reportNo = data.report_no || data.interview_report_no || ivState.reportNo;
  ivState.fromHistory = fromHistory;
  ivState.running = false;
  $('iv-start-report').classList.remove('btn--loading');
  $('iv-report-content').innerHTML = renderMarkdown(ivState.reportMd);
  const title = data.title || ivTitleFromMarkdown(ivState.reportMd);
  $('iv-report-title').textContent = title;
  const sheetCount = Number(data.interview_sheet_count || ivState.sheets.length || 0);
  const playerCount = Number(data.interview_player_count || data.player_count || 0);
  const moduleCount = Number(data.interview_module_count || data.module_count || 0);
  $('iv-report-meta').textContent = [
    ivState.reportNo,
    sheetCount ? `${sheetCount} 个 Sheet` : '',
    playerCount ? `${playerCount} 位玩家` : '',
    moduleCount ? `${moduleCount} 个模块` : '',
    fromHistory ? '历史报告' : '已自动保存',
  ].filter(Boolean).join(' · ');
  const audit = data.interview_audit || data.audit || {};
  ivState.audit = audit;
  ivApplyModelsUsed(data.models_used || data.interview_models_used);
  ivRenderPartial(ivState.reportMd);
  $('iv-live-preview').scrollTop = 0;
  ivSetLiveState('done');
  ivBuildToc();
  ivRenderAuditReview();
  ivGoStep(3);
}

function ivHandleStreamEvent(data) {
  if (data.type === 'progress') {
    ivApplyProgress(data);
    return;
  }
  if (data.type === 'heartbeat') {
    ivSetPercent(data.percent);
    ivUpdateStage(data.stage);
    if (data.model) {
      ivSetStageModel(data.stage, data.model, { fallback: Boolean(data.is_fallback) });
    }
    return;
  }
  if (data.type === 'interview_model_status') {
    ivSetPercent(data.percent);
    ivUpdateStage(data.stage);
    ivSetStageModel(data.stage, data.model, { fallback: Boolean(data.is_fallback) });
    if (data.is_fallback) {
      const reason = data.fallback_reason === 'output_limit'
        ? '主模型输出达到上限'
        : '主模型未能完成';
      showToast(`${reason}，已自动切换备用模型 ${data.model}`, 'info', 8000);
    }
    return;
  }
  if (data.type === 'interview_module_done') {
    ivApplyProgress(data);
    ivRenderPartial(data.partial_report_md);
    $('iv-module-progress-text').textContent =
      `${data.module_index}/${data.total_modules} 已完成 · 刚完成：${data.module_title}`;
    return;
  }
  if (data.type === 'interview_module_repaired') {
    ivApplyProgress(data);
    ivRenderPartial(data.partial_report_md);
    $('iv-module-progress-text').textContent =
      `${data.total_modules}/${data.total_modules} 已完成 · 已修订：${data.module_title}`;
    return;
  }
  if (data.type === 'interview_done') {
    if (ivState.eventSource) ivState.eventSource.close();
    ivState.eventSource = null;
    ivSetPercent(100);
    ivUpdateStage('completed');
    ivRenderReport(data);
    showToast('访谈报告已生成、复审并保存到历史记录', 'success');
    return;
  }
  if (data.type === 'error') {
    if (ivState.eventSource) ivState.eventSource.close();
    ivState.eventSource = null;
    ivState.running = false;
    ivSetPercent(data.percent);
    ivUpdateStage(data.stage, true);
    ivApplyModelsUsed(data.models_used);
    ivSetLiveState('error');
    $('iv-progress-message').textContent = data.message || '报告生成失败';
    $('iv-progress-actions').hidden = false;
    showToast(data.message || '访谈报告生成失败', 'error', 8000);
  }
}

function ivRunReport() {
  if (!ivState.sessionId || ivState.running) return;
  ivState.running = true;
  $('iv-progress-actions').hidden = true;
  ivSetLiveState('running');
  const source = new EventSource(`/api/interview/run/${ivState.sessionId}`);
  ivState.eventSource = source;
  source.onmessage = event => {
    try {
      ivHandleStreamEvent(JSON.parse(event.data));
    } catch (error) {
      console.warn('[interview] invalid SSE event', error);
    }
  };
  source.onerror = async () => {
    source.close();
    if (ivState.eventSource === source) ivState.eventSource = null;
    ivState.running = false;
    try {
      const response = await fetch(`/api/interview/status/${ivState.sessionId}`);
      const status = await response.json();
      if (response.ok && status.status === 'completed' && status.report_md) {
        ivRenderReport(status);
        showToast('连接恢复成功，报告已经生成完成', 'success');
        return;
      }
      if (response.ok) {
        ivSetPercent(status.percent);
        ivUpdateStage(status.stage, true);
        ivApplyModelsUsed(status.models_used);
        if (status.partial_report_md) ivRenderPartial(status.partial_report_md);
        $('iv-progress-message').textContent =
          status.message || '生成连接中断，可从已完成进度继续';
      }
    } catch { }
    ivSetLiveState('error');
    $('iv-progress-actions').hidden = false;
    showToast('生成连接中断，可以从已完成进度继续', 'error');
  };
}

async function ivUploadAndStart() {
  const file = ivState.selectedFile;
  if (!file) {
    showToast('请先选择访谈 Excel', 'error');
    return;
  }
  const button = $('iv-start-report');
  button.disabled = true;
  button.classList.add('btn--loading');
  ivResetStages();
  ivSetPercent(2);
  ivUpdateStage('upload');
  ivGoStep(2);
  $('iv-progress-message').textContent = `正在上传并解析 ${file.name}…`;
  const body = new FormData();
  body.append('file', file);
  body.append('research_focus', $('iv-research-focus').value.trim());
  try {
    const response = await fetch('/api/interview/upload', { method: 'POST', body });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '上传失败');
    ivState.sessionId = data.session_id;
    ivState.filename = data.filename;
    ivState.sheets = data.sheets || [];
    ivState.fromHistory = false;
    $('iv-file-summary').innerHTML = `
      <strong>${esc(data.filename)}</strong>
      <span>${esc(ivState.sheets.length)} 个 Sheet · ${esc(data.total_cells)} 个非空单元格</span>`;
    ivSetPercent(5);
    ivUpdateStage('extract');
    ivRunReport();
  } catch (error) {
    button.disabled = false;
    button.classList.remove('btn--loading');
    ivGoStep(1);
    showToast(`上传失败：${error.message}`, 'error', 8000);
  }
}

async function ivRestoreStatus() {
  if (!ivState.sessionId) return;
  try {
    const response = await fetch(`/api/interview/status/${ivState.sessionId}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '无法恢复进度');
    if (data.status === 'completed' && data.report_md) {
      ivRenderReport(data);
      return;
    }
    ivSetPercent(data.percent);
    ivUpdateStage(data.stage);
    ivApplyModelsUsed(data.models_used);
    $('iv-progress-message').textContent = data.message || '准备继续生成';
    if (data.partial_report_md) ivRenderPartial(data.partial_report_md);
    ivRunReport();
  } catch (error) {
    showToast(`恢复失败：${error.message}`, 'error');
  }
}

function ivDownloadMarkdown() {
  const blob = new Blob([ivState.reportMd || ''], { type: 'text/markdown;charset=utf-8' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `${ivTitleFromMarkdown(ivState.reportMd).replace(/[\\/:*?"<>|]/g, '_')}.md`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

async function ivExport(type) {
  if (!ivState.sessionId || !ivState.reportMd) return;
  $('iv-export-menu').classList.remove('open');
  if (type === 'markdown') {
    ivDownloadMarkdown();
    return;
  }
  const useHistory = ivState.fromHistory;
  if (type === 'word' || type === 'pdf') {
    const suffix = useHistory ? `${type}-history` : type;
    window.location.href = `/api/export/${suffix}/${ivState.sessionId}`;
    return;
  }
  if (type === 'feishu') {
    if (!state.feishu?.configured) {
      showToast('服务端未配置飞书应用', 'error');
      return;
    }
    if (!state.feishu?.logged_in) {
      showToast('请先登录飞书（左下角）', 'info');
      return;
    }
    if (!confirm(`将以 ${state.feishu.email || state.feishu.name || '当前账号'} 创建飞书文档，是否继续？`)) return;
    const url = useHistory
      ? `/api/export/feishu-history/${ivState.sessionId}`
      : `/api/export/feishu/${ivState.sessionId}`;
    try {
      const response = await fetch(url, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '导出失败');
      try { await navigator.clipboard.writeText(data.url); } catch { }
      window.open(data.url, '_blank', 'noopener');
      showToast('飞书文档已创建，链接已复制', 'success');
    } catch (error) {
      showToast(`导出飞书文档失败：${error.message}`, 'error', 8000);
    }
  }
}

async function ivRenameReport() {
  if (!ivState.sessionId || !ivState.reportMd) return;
  const oldTitle = ivTitleFromMarkdown(ivState.reportMd);
  const title = prompt('请输入新的报告名称', oldTitle);
  if (title === null || !title.trim() || title.trim() === oldTitle) return;
  try {
    const response = await fetch(`/api/history/${ivState.sessionId}/title`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title.trim() }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '改名失败');
    ivState.reportMd = data.report_md;
    $('iv-report-title').textContent = data.title;
    $('iv-report-content').innerHTML = renderMarkdown(data.report_md);
    ivBuildToc();
    ivRenderAuditReview();
    showToast('报告名称已更新', 'success');
  } catch (error) {
    showToast(`改名失败：${error.message}`, 'error');
  }
}

function ivFindReviewIssueIndex(job) {
  const issues = ivAuditIssues();
  return issues.findIndex((issue, index) => (
    !ivIssueConfirmed(issue)
    && ivAuditIssueKey(issue, index, issues) === job.key
  ));
}

function ivEnqueueReviewAction(issueIndex, action) {
  if (!ivState.sessionId) return;
  const issues = ivAuditIssues();
  const issue = issues[issueIndex];
  if (!issue || ivIssueConfirmed(issue)) return;
  const key = ivAuditIssueKey(issue, issueIndex, issues);
  if (ivState.reviewJobs.has(key)) {
    showToast('这条审校提醒已经在处理队列中', 'info');
    return;
  }
  const waiting = Boolean(ivState.reviewActive);
  const job = {
    key,
    action,
    issue: { ...issue },
    state: 'queued',
    message: action === 'confirm' ? '已加入确认队列…' : '已加入修订队列…',
  };
  ivState.reviewJobs.set(key, job);
  ivState.reviewQueue.push(job);
  ivRenderAuditReview();
  if (waiting) {
    showToast(
      action === 'confirm' ? '已加入确认队列，将自动处理' : '已加入修订队列，将自动处理',
      'info',
    );
  }
  void ivProcessReviewQueue();
}

async function ivExecuteConfirmAuditIssue(issueIndex) {
  const response = await fetch(
    `/api/interview/review/${ivState.sessionId}/issues/${issueIndex}/confirm`,
    { method: 'PATCH' },
  );
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '确认失败');
  ivState.audit = data.interview_audit || {};
  ivRenderAuditReview();
  showToast('已确认当前内容，原始审校记录已保留', 'success');
}

async function ivExecuteReviseAuditIssue(issueIndex, job) {
  let finalData = null;
  await consumeSSEPost(
    `/api/interview/review/${ivState.sessionId}/issues/${issueIndex}/revise`,
    {},
    data => {
      if (data.type === 'interview_review_progress') {
        job.message = data.message || '正在修订…';
        ivRenderAuditReview();
      } else if (data.type === 'interview_review_done') {
        finalData = data;
      }
    },
  );
  if (!finalData?.report_md) throw new Error('修订完成后未返回有效报告');
  ivRenderReport(finalData, { fromHistory: ivState.fromHistory });
  showToast(finalData.message || '已按建议修订并重新审校', 'success', 5000);
}

async function ivProcessReviewQueue() {
  if (ivState.reviewActive || !ivState.sessionId) return;
  while (!ivState.reviewActive && ivState.reviewQueue.length) {
    const job = ivState.reviewQueue.shift();
    if (!ivState.reviewJobs.has(job.key)) continue;
    const issueIndex = ivFindReviewIssueIndex(job);
    if (issueIndex < 0) {
      ivState.reviewJobs.delete(job.key);
      ivRenderAuditReview();
      showToast(
        `“${job.issue.module_title || '对应模块'}”中的这条提醒已在前一次复审中解决或发生变化，未重复处理`,
        'info',
        6000,
      );
      continue;
    }

    ivState.reviewActive = job;
    job.state = 'running';
    job.message = job.action === 'confirm'
      ? '正在保存确认状态…'
      : '正在按建议修订对应模块…';
    ivRenderAuditReview();
    try {
      if (job.action === 'confirm') {
        await ivExecuteConfirmAuditIssue(issueIndex);
      } else {
        await ivExecuteReviseAuditIssue(issueIndex, job);
      }
    } catch (error) {
      const message = String(error.message || error);
      if (message.includes('会话不存在或已过期')) {
        showToast('原始证据会话已过期，请重新上传访谈记录后生成报告', 'error', 8000);
      } else {
        showToast(
          `${job.action === 'confirm' ? '确认' : '修订'}失败：${message}`,
          'error',
          8000,
        );
      }
    } finally {
      ivState.reviewJobs.delete(job.key);
      ivState.reviewActive = null;
      ivRenderAuditReview();
    }
  }
}

function ivConfirmAuditIssue(issueIndex) {
  ivEnqueueReviewAction(issueIndex, 'confirm');
}

function ivReviseAuditIssue(issueIndex) {
  ivEnqueueReviewAction(issueIndex, 'revise');
}

function ivLoadHistoryEntry(entry) {
  switchMode('interview');
  ivState.sessionId = entry.id;
  ivState.filename = entry.filename || '';
  ivState.sheets = [];
  ivState.reportNo = entry.report_no || '';
  ivRenderReport(entry, { fromHistory: true });
  showToast('已载入访谈历史报告', 'success');
}

const ivUploadZone = $('iv-upload-zone');
const ivFileInput = $('iv-file-input');
ivUploadZone.addEventListener('click', () => ivFileInput.click());
ivFileInput.addEventListener('change', () => ivSelectFile(ivFileInput.files[0]));
ivUploadZone.addEventListener('dragover', event => {
  event.preventDefault();
  ivUploadZone.classList.add('drag-over');
});
ivUploadZone.addEventListener('dragleave', () => ivUploadZone.classList.remove('drag-over'));
ivUploadZone.addEventListener('drop', event => {
  event.preventDefault();
  ivUploadZone.classList.remove('drag-over');
  ivSelectFile(event.dataTransfer.files[0]);
});
$('iv-remove-file').addEventListener('click', event => {
  event.stopPropagation();
  ivRemoveFile();
});
$('iv-start-report').addEventListener('click', ivUploadAndStart);
$('iv-retry-report').addEventListener('click', ivRestoreStatus);
$('iv-progress-new').addEventListener('click', ivReset);
$('iv-new-report').addEventListener('click', () => {
  if (ivState.running && !confirm('报告仍在生成，确定要开始新报告吗？')) return;
  ivReset();
});
$('iv-copy-report').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(ivState.reportMd || '');
    showToast('Markdown 已复制', 'success');
  } catch {
    showToast('复制失败，请使用 Markdown 下载', 'error');
  }
});
$('iv-rename-report').addEventListener('click', ivRenameReport);
$('iv-quality-badge').addEventListener('click', () => {
  ivToggleReviewPanel($('iv-review-panel').hidden);
});
$('iv-review-close').addEventListener('click', () => ivToggleReviewPanel(false));
$('iv-review-list').addEventListener('click', event => {
  const item = event.target.closest('[data-iv-review-jump]');
  if (item) ivScrollToAuditIssue(Number(item.dataset.ivReviewJump));
});
$('iv-review-list').addEventListener('keydown', event => {
  if (!['Enter', ' '].includes(event.key)) return;
  const item = event.target.closest('[data-iv-review-jump]');
  if (!item) return;
  event.preventDefault();
  ivScrollToAuditIssue(Number(item.dataset.ivReviewJump));
});
$('iv-report-content').addEventListener('click', event => {
  const button = event.target.closest('[data-iv-review-action]');
  if (!button) return;
  const issueIndex = Number(button.dataset.ivAuditIndex);
  if (button.dataset.ivReviewAction === 'confirm') {
    ivConfirmAuditIssue(issueIndex);
  } else if (button.dataset.ivReviewAction === 'revise') {
    ivReviseAuditIssue(issueIndex);
  }
});
$('iv-export-toggle').addEventListener('click', event => {
  event.stopPropagation();
  $('iv-export-menu').classList.toggle('open');
});
document.querySelectorAll('[data-iv-export]').forEach(button => {
  button.addEventListener('click', () => ivExport(button.dataset.ivExport));
});
document.querySelectorAll('[data-iv-step]').forEach(button => {
  button.addEventListener('click', () => ivPreviewStep(Number(button.dataset.ivStep)));
});
document.addEventListener('click', event => {
  if (!event.target.closest('#iv-export-dropdown')) $('iv-export-menu')?.classList.remove('open');
});
