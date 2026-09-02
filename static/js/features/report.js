// ============================================================
// STEP 4: Stats + Report
// ============================================================

function resetReportFailureUi() {
  state.sessionReport.error = '';
  state.sessionReport.reportLlmUsage = null;
  const box = $('report-error');
  if (box) box.hidden = true;
  $('ps-writing')?.classList.remove('progress-step--failed');
  const indicator = $('report-stream-container')?.querySelector('.stream-indicator');
  indicator?.classList.remove('stream-indicator--failed');
  const title = $('report-stream-status-title');
  const meta = $('report-stream-status-meta');
  const count = $('report-stream-count');
  const progress = $('report-generation-progress');
  const progressBar = $('report-generation-progress-bar');
  if (title) title.textContent = '正在准备报告内容';
  if (meta) meta.textContent = '每个章节完成并校验后会自动显示';
  if (count) count.textContent = '正在准备生成步骤';
  if (progress) progress.setAttribute('aria-valuenow', '0');
  if (progressBar) progressBar.style.width = '0%';
  document.querySelectorAll('[data-report-phase]').forEach(element => {
    element.className = 'report-phase report-phase--pending';
  });
  const remaining = $('report-progress-remaining');
  if (remaining) remaining.textContent = '正在计算后续处理步骤';
  renderReportLlmUsage({
    progressState: { phases: Object.fromEntries(REPORT_PHASE_ORDER.map(phase => [phase, 'pending'])) },
    running: state.sessionReport.running,
  });
}

function showReportFailureUi(message) {
  const rawMessage = String(message || '服务暂时不可用，请稍后重试');
  let cleanMessage = rawMessage;
  if (/Dify\s+503/i.test(rawMessage)) {
    cleanMessage = 'Dify 服务暂时不可用（503），自动重试后仍未恢复，请稍后重新生成。';
  } else if (/Dify\s+(?:502|504)/i.test(rawMessage)) {
    cleanMessage = 'Dify 网关暂时无法完成请求，自动重试后仍未恢复，请稍后重新生成。';
  } else if (cleanMessage.length > 300) {
    cleanMessage = `${cleanMessage.slice(0, 300)}…`;
  }
  state.sessionReport.error = cleanMessage;
  $('ps-writing')?.classList.remove('progress-step--active', 'progress-step--done');
  $('ps-writing')?.classList.add('progress-step--failed');

  const box = $('report-error');
  const messageEl = $('report-error-message');
  if (messageEl) messageEl.textContent = cleanMessage;
  if (box) box.hidden = false;

  const stream = $('report-stream-container');
  if (stream) stream.style.display = 'block';
  const indicator = stream?.querySelector('.stream-indicator');
  indicator?.classList.add('stream-indicator--failed');
  const title = $('report-stream-status-title');
  const meta = $('report-stream-status-meta');
  if (title) title.textContent = '报告生成已中断';
  if (meta) meta.textContent = '已完成的章节保留在下方，重新生成将从第一章开始';
}

function _formatReportWaitTime(ms) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

function formatReportDuration(value) {
  if (value === null || value === undefined || value === '') return '';
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) return '';
  const totalSeconds = Math.round(parsed);
  if (totalSeconds < 1) return '少于 1 秒';
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return [
    hours ? `${hours} 小时` : '',
    minutes ? `${minutes} 分` : '',
    seconds || (!hours && !minutes) ? `${seconds} 秒` : '',
  ].filter(Boolean).join(' ');
}

function _parseReportProgress(message) {
  const match = String(message || '').match(/分章生成\s+(\d+)\/(\d+)[：:]\s*(.+)/);
  if (!match) return null;
  return {
    current: Number(match[1]),
    total: Number(match[2]),
    task: match[3].replace(/[.…]+$/, '').trim(),
  };
}

const REPORT_PHASE_LABELS = {
  themes: '逐题主题分析',
  synthesis: '跨题观点归纳',
  writing: '报告撰写',
  finalize: '校验并保存',
};
const REPORT_PHASE_ORDER = Object.keys(REPORT_PHASE_LABELS);
const REPORT_LLM_LEGACY_TEXT = '该版本未记录模型/token用量';

const REPORT_STATUS_LABELS = {
  active: '处理中',
  retrying: '自动修正',
  recovered: '已恢复',
  completed: '已完成',
  degraded: '已降级',
  skipped: '已跳过',
};

function _normalizeReportUsageNumber(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function _normalizeReportModelList(value) {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map(item => String(item || '').trim()).filter(Boolean))];
}

function _normalizeReportActiveModels(value) {
  if (!value || typeof value !== 'object') return {};
  return Object.fromEntries(
    Object.entries(value)
      .map(([model, count]) => [String(model || '').trim(), _normalizeReportUsageNumber(count)])
      .filter(([model, count]) => model && count > 0),
  );
}

function normalizeReportLlmUsage(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const normalizeNode = node => {
    const source = node && typeof node === 'object' ? node : {};
    return {
      models_used: _normalizeReportModelList(source.models_used),
      fallback_models_used: _normalizeReportModelList(source.fallback_models_used),
      input_tokens: _normalizeReportUsageNumber(source.input_tokens),
      output_tokens: _normalizeReportUsageNumber(source.output_tokens),
      total_tokens: _normalizeReportUsageNumber(source.total_tokens),
      call_count: _normalizeReportUsageNumber(source.call_count),
      usage_reported_call_count: _normalizeReportUsageNumber(source.usage_reported_call_count),
      usage_missing_call_count: _normalizeReportUsageNumber(source.usage_missing_call_count),
      active_calls: _normalizeReportUsageNumber(source.active_calls),
      active_models: _normalizeReportActiveModels(source.active_models),
    };
  };
  const phases = {};
  let hasAnySignal = false;
  REPORT_PHASE_ORDER.forEach(phase => {
    phases[phase] = normalizeNode(raw.phases?.[phase]);
    const phaseUsage = phases[phase];
    if (
      phaseUsage.models_used.length
      || phaseUsage.fallback_models_used.length
      || phaseUsage.total_tokens
      || phaseUsage.call_count
      || phaseUsage.usage_missing_call_count
      || phaseUsage.active_calls
      || Object.keys(phaseUsage.active_models).length
    ) {
      hasAnySignal = true;
    }
  });
  const totals = normalizeNode(raw.totals);
  if (
    totals.models_used.length
    || totals.fallback_models_used.length
    || totals.total_tokens
    || totals.call_count
    || totals.usage_missing_call_count
    || totals.active_calls
    || Object.keys(totals.active_models).length
  ) {
    hasAnySignal = true;
  }
  return {
    phases,
    totals,
    hasAnySignal,
  };
}

function _reportUsageActiveModelsText(activeModels = {}) {
  return Object.entries(activeModels)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh-CN'))
    .map(([model, count]) => (count > 1 ? `${model} × ${count}` : model))
    .join('、');
}

function _reportUsageModelText(usage, { running = false } = {}) {
  if (!usage) return running ? '模型待调用' : '未记录模型';
  const activeModelsText = _reportUsageActiveModelsText(usage.active_models);
  if (usage.active_calls > 0 && activeModelsText) {
    return `当前：${activeModelsText}`;
  }
  const usedModels = usage.models_used.join('、');
  const fallbackModels = usage.fallback_models_used.join('、');
  if (usedModels && fallbackModels) return `已用：${usedModels}；备选：${fallbackModels}`;
  if (usedModels) return `已用：${usedModels}`;
  if (fallbackModels) return `已用：${fallbackModels}`;
  if (usage.call_count > 0) return `已发起 ${usage.call_count} 次调用`;
  return running ? '等待开始' : '未记录模型';
}

function _reportUsageTokensText(usage, { waitingText = 'Token 待统计' } = {}) {
  if (!usage) return waitingText;
  if (
    !usage.total_tokens
    && !usage.usage_missing_call_count
    && !usage.usage_reported_call_count
  ) {
    return usage.call_count > 0 ? waitingText : '等待开始';
  }
  const totalText = `${usage.usage_missing_call_count > 0 ? '已统计至少 ' : ''}${(usage.total_tokens || 0).toLocaleString('zh-CN')} token`;
  return usage.usage_missing_call_count > 0
    ? `${totalText} · ${usage.usage_missing_call_count}次调用 usage 未返回或不完整`
    : totalText;
}

function _reportUsagePhaseMeta(
  phase,
  progressState,
  usageState,
  { running = false, hasReport = false } = {},
) {
  const phaseUsage = usageState?.phases?.[phase] || null;
  const phaseStatus = progressState?.phases?.[phase] || 'pending';
  if (!running && usageState && usageState.hasAnySignal === false) {
    return '未记录模型/token';
  }
  if (!phaseUsage || (
    !phaseUsage.call_count
    && !phaseUsage.total_tokens
    && !phaseUsage.usage_missing_call_count
    && !phaseUsage.active_calls
    && !phaseUsage.models_used.length
    && !phaseUsage.fallback_models_used.length
  )) {
    if (!running && hasReport && usageState?.hasAnySignal) return '本环节未调用模型';
    if (!running && hasReport) return '未记录模型/token';
    if (phaseStatus === 'pending') return running ? '等待开始' : '未开始';
    if (running && phaseStatus === 'active') return '本地处理中，暂未调用模型';
    if (running && ['completed', 'degraded', 'skipped'].includes(phaseStatus)) {
      return '本环节未调用模型';
    }
    return running ? '等待写入模型 / Token' : '未记录模型/token';
  }
  return `${_reportUsageModelText(phaseUsage, { running })} · ${_reportUsageTokensText(phaseUsage)}`;
}

function _reportUsagePhaseDetailLines(usage, fallbackText) {
  if (!usage) return [fallbackText];
  const lines = [];
  const activeModelsText = _reportUsageActiveModelsText(usage.active_models);
  if (usage.active_calls > 0 && activeModelsText) {
    lines.push(`当前：${activeModelsText}`);
  }
  if (usage.models_used.length) {
    lines.push(`已用：${usage.models_used.join('、')}`);
  } else if (usage.call_count > 0 && !activeModelsText) {
    lines.push(`已发起 ${usage.call_count} 次调用`);
  }
  if (usage.fallback_models_used.length) {
    lines.push(`备选：${usage.fallback_models_used.join('、')}`);
  }
  if (
    usage.call_count > 0
    || usage.active_calls > 0
    || usage.total_tokens > 0
    || usage.usage_reported_call_count > 0
    || usage.usage_missing_call_count > 0
  ) {
    let tokenText = _reportUsageTokensText(usage, { waitingText: '待统计' });
    if (tokenText === '等待开始') tokenText = '待统计';
    lines.push(`Token 消耗：${tokenText}`);
  }
  return lines.length ? lines : [fallbackText];
}

function _reportUsageSummaryTexts(usageState, { running = false, hasReport = false } = {}) {
  if (!usageState) {
    if (running) {
      return {
        total: '模型 / Token 将在调用后显示',
        note: '生成过程中会按环节持续刷新',
        legacy: false,
      };
    }
    if (hasReport) {
      return { total: REPORT_LLM_LEGACY_TEXT, note: '', legacy: true };
    }
    return { total: '', note: '', legacy: false };
  }
  if (!running && hasReport && usageState.hasAnySignal === false) {
    return { total: REPORT_LLM_LEGACY_TEXT, note: '', legacy: true };
  }
  const totals = usageState.totals;
  const total = _reportUsageTokensText(totals, { waitingText: 'Token 待统计' });
  const noteParts = [];
  const modelText = _reportUsageModelText(totals, { running });
  if (modelText && modelText !== '等待开始' && modelText !== '模型待调用') noteParts.push(modelText);
  if (totals.call_count > 0) noteParts.push(`共 ${totals.call_count} 次调用`);
  return {
    total,
    note: noteParts.join(' · '),
    legacy: false,
  };
}

function renderReportLlmUsage({ progressState = null, running = false } = {}) {
  const ctx = activeReportCtx();
  const usageState = normalizeReportLlmUsage(ctx?.reportLlmUsage);
  const hasReport = Boolean(ctx?.reportMd);
  const summary = _reportUsageSummaryTexts(usageState, { running, hasReport });

  document.querySelectorAll('[data-report-phase-meta]').forEach(element => {
    const phase = element.dataset.reportPhaseMeta;
    element.textContent = _reportUsagePhaseMeta(
      phase,
      progressState,
      usageState,
      { running, hasReport },
    );
    element.title = element.textContent;
  });

  const summaryBox = $('report-llm-summary');
  const summaryTotal = $('report-llm-total');
  const summaryNote = $('report-llm-summary-note');
  if (summaryBox && summaryTotal && summaryNote) {
    const visible = running || hasReport || Boolean(usageState);
    summaryBox.hidden = !visible;
    summaryTotal.textContent = summary.total || (running ? '模型 / Token 将在调用后显示' : REPORT_LLM_LEGACY_TEXT);
    summaryNote.textContent = summary.note;
  }

  const toolbar = $('report-toolbar-llm');
  const popover = $('report-llm-popover');
  const toolbarTotal = $('report-toolbar-llm-total');
  const toolbarNote = $('report-toolbar-llm-note');
  const toolbarPhases = $('report-toolbar-llm-phases');
  if (!toolbar || !popover || !toolbarTotal || !toolbarNote || !toolbarPhases) return;

  const toolbarVisible = hasReport || Boolean(usageState) || running;
  popover.hidden = !toolbarVisible;
  if (!toolbarVisible) setReportLlmPopoverOpen(false);
  toolbarTotal.textContent = summary.total || REPORT_LLM_LEGACY_TEXT;
  toolbarNote.textContent = summary.note;
  toolbarPhases.innerHTML = REPORT_PHASE_ORDER.map(phase => {
    const phaseUsage = usageState?.phases?.[phase] || null;
    const hasPhaseData = Boolean(
      phaseUsage
      && (
        phaseUsage.call_count
        || phaseUsage.total_tokens
        || phaseUsage.usage_missing_call_count
        || phaseUsage.active_calls
        || phaseUsage.models_used.length
        || phaseUsage.fallback_models_used.length
      )
    );
    const phaseStatus = progressState?.phases?.[phase] || '';
    const className = hasPhaseData
      ? (phaseUsage?.active_calls > 0 || phaseStatus === 'active'
        ? 'report-toolbar__llm-phase report-toolbar__llm-phase--active'
        : 'report-toolbar__llm-phase report-toolbar__llm-phase--done')
      : `report-toolbar__llm-phase${summary.legacy ? ' report-toolbar__llm-phase--legacy' : ''}`;
    const meta = _reportUsagePhaseMeta(
      phase,
      progressState,
      usageState,
      { running: running && state.viewMode === 'session', hasReport },
    );
    const detailLines = _reportUsagePhaseDetailLines(phaseUsage, meta);
    return `
      <div class="${className}">
        <strong>${esc(REPORT_PHASE_LABELS[phase])}</strong>
        <div class="report-toolbar__llm-phase-details" title="${esc(detailLines.join('；'))}">
          ${detailLines.map(line => `<span>${esc(line)}</span>`).join('')}
        </div>
      </div>`;
  }).join('');
}

function _createReportTaskProgress() {
  return {
    structured: false,
    phase: '',
    phases: {},
    items: new Map(),
    current: null,
    details: [],
  };
}

function _appendReportProgressDetail(progressState, message) {
  const text = String(message || '').trim();
  if (!text || progressState.details.at(-1) === text) return;
  progressState.details.push(text);
  if (progressState.details.length > 80) progressState.details.shift();
}

function _reportQuestionLabel(item) {
  const part = Number(item.part_index) > 0
    ? `Part ${item.part_index}${item.part_name ? ` · ${item.part_name}` : ''}`
    : (item.part_name || '未分章开放题');
  return `${part} · ${item.question_name || '开放题'}`;
}

function _applyAnalysisProgress(progressState, event) {
  progressState.structured = true;
  progressState.phase = event.phase || progressState.phase;
  progressState.phases[event.phase] = event.status || 'active';
  progressState.current = event;
  _appendReportProgressDetail(progressState, event.message);

  if (event.scope_key != null) {
    const key = String(event.scope_key);
    progressState.items.set(key, {
      ...(progressState.items.get(key) || {}),
      ...event,
    });
  }

  const phaseIndex = Number(event.phase_index) || 1;
  for (const [phase, index] of Object.keys(REPORT_PHASE_LABELS).map((phase, index) => [phase, index + 1])) {
    if (index < phaseIndex && !progressState.phases[phase]) {
      progressState.phases[phase] = 'completed';
    }
  }
}

function _reportProgressPercent(progressState) {
  const current = progressState.current || {};
  const phaseIndex = Math.max(1, Number(current.phase_index) || 1);
  const phaseTotal = Math.max(1, Number(current.phase_total) || 4);
  let withinPhase = ['completed', 'degraded', 'skipped'].includes(current.status) ? 1 : 0.08;
  if (current.phase === 'themes' && Number(current.item_total)) {
    const itemTotal = Number(current.item_total);
    const completedItems = [...progressState.items.values()].filter(item =>
      ['completed', 'degraded', 'skipped'].includes(item.status)
    ).length;
    withinPhase = Math.min(1, completedItems / itemTotal);
  } else if (Number(current.item_total)) {
    withinPhase = Math.min(
      1,
      Math.max(0, (Number(current.item_index) - 1) / Number(current.item_total)),
    );
  }
  return Math.min(100, Math.round(((phaseIndex - 1 + withinPhase) / phaseTotal) * 100));
}

function _renderReportPhaseTrack(progressState) {
  document.querySelectorAll('[data-report-phase]').forEach(element => {
    const phase = element.dataset.reportPhase;
    const status = progressState.phases[phase] || 'pending';
    element.className = `report-phase report-phase--${status}`;
  });
  renderReportLlmUsage({
    progressState: state.viewMode === 'session' ? progressState : null,
    running: state.viewMode === 'session' && state.sessionReport.running,
  });
}

function _reportRemainingText(progressState) {
  const current = progressState.current || {};
  if (current.phase === 'themes') {
    const total = Number(current.item_total) || 0;
    const completed = [...progressState.items.values()].filter(item =>
      ['completed', 'degraded', 'skipped'].includes(item.status)
    ).length;
    const remaining = Math.max(0, total - completed);
    return `后续还需：${remaining} 道题 → 跨题观点归纳 → 报告撰写 → 校验并保存`;
  }
  if (current.phase === 'synthesis') return '后续还需：报告撰写 → 校验并保存';
  if (current.phase === 'writing') return '后续还需：校验并保存';
  if (current.phase === 'finalize') return current.status === 'completed' ? '全部处理步骤已完成' : '这是最后一个处理阶段';
  return '正在计算后续处理步骤';
}

function _renderReportPreparationSteps(element, progressState) {
  if (!element) return;
  element.textContent = '';
  element.classList.add('report-stream-content--waiting');

  _renderReportPhaseTrack(progressState);
  const remaining = $('report-progress-remaining');
  if (remaining) remaining.textContent = _reportRemainingText(progressState);

  const current = progressState.current;
  if (current?.scope_key != null && !['completed', 'skipped'].includes(current.status)) {
    const currentCard = document.createElement('section');
    currentCard.className = `report-current-task report-current-task--${current.status || 'active'}`;
    const eyebrow = document.createElement('span');
    eyebrow.className = 'report-current-task__eyebrow';
    eyebrow.textContent = '当前处理';
    const title = document.createElement('strong');
    title.className = 'report-current-task__title';
    title.textContent = _reportQuestionLabel(current);
    const sample = document.createElement('span');
    sample.className = 'report-current-task__sample';
    const unit = current.count_unit === 'players' ? '名玩家' : '条回答';
    sample.textContent = `${Number(current.respondent_count) || 0} ${unit}`;
    const message = document.createElement('p');
    message.textContent = current.message || '正在处理';
    currentCard.append(eyebrow, title, sample, message);
    if (current.audience) {
      const audience = document.createElement('small');
      audience.textContent = current.audience;
      currentCard.append(audience);
    }
    if (Array.isArray(current.next_steps) && current.next_steps.length) {
      const next = document.createElement('small');
      next.textContent = `本题之后还需：${current.next_steps.join(' → ')}`;
      currentCard.append(next);
    }
    if (current.impact && current.impact !== 'none') {
      const impact = document.createElement('div');
      impact.className = 'report-quality-impact';
      impact.textContent = `对报告的影响：${current.impact}`;
      currentCard.append(impact);
    }
    element.append(currentCard);
  }

  const items = [...progressState.items.values()].sort((a, b) =>
    (Number(a.item_index) || 0) - (Number(b.item_index) || 0)
  );
  if (items.length) {
    const intro = document.createElement('div');
    intro.className = 'report-preparation__intro';
    intro.textContent = `逐题进度（${items.filter(item => ['completed', 'degraded', 'skipped'].includes(item.status)).length}/${Number(items[0]?.item_total) || items.length}）`;
    element.append(intro);
  }

  const list = document.createElement('ol');
  list.className = 'report-preparation';
  for (const step of items) {
    const item = document.createElement('li');
    item.className = `report-preparation__item report-preparation__item--${step.status}`;

    const marker = document.createElement('span');
    marker.className = 'report-preparation__marker';
    marker.setAttribute('aria-hidden', 'true');

    const copy = document.createElement('span');
    copy.className = 'report-preparation__copy';
    const label = document.createElement('strong');
    label.textContent = _reportQuestionLabel(step);
    const unit = step.count_unit === 'players' ? '名玩家' : '条回答';
    const summary = document.createElement('small');
    summary.textContent = `${Number(step.respondent_count) || 0} ${unit} · ${step.message || '等待处理'}`;
    copy.append(label, summary);
    if (step.impact && step.impact !== 'none') {
      const impact = document.createElement('small');
      impact.className = 'report-preparation__impact';
      impact.textContent = `影响：${step.impact}`;
      copy.append(impact);
    }

    const status = document.createElement('span');
    status.className = 'report-preparation__status';
    status.textContent = REPORT_STATUS_LABELS[step.status] || '等待中';

    item.append(marker, copy, status);
    list.append(item);
  }
  if (items.length) element.append(list);

  if (progressState.details.length) {
    const details = document.createElement('details');
    details.className = 'report-progress-details';
    const summary = document.createElement('summary');
    summary.textContent = `查看处理详情（${progressState.details.length}）`;
    const detailList = document.createElement('ol');
    for (const text of progressState.details) {
      const row = document.createElement('li');
      row.textContent = text;
      detailList.append(row);
    }
    details.append(summary, detailList);
    element.append(details);
  }
  element.scrollTop = element.scrollHeight;
}

const EMPTY_RERUN_INSTRUCTION = '未填写补充要求，本次为重新生成';
let partialRerunRunning = false;
let partialRerunContext = null;

function normalizeReportVersions(versions) {
  return (Array.isArray(versions) ? versions : [])
    .map(item => {
      if (item && typeof item === 'object') {
        const version = Number(item.version ?? item.id ?? item.value);
        if (!Number.isFinite(version)) return null;
        const kind = item.kind || '';
        const rawInstruction = String(item.instruction || '').trim();
        const isRerun = kind === 'regenerate' || kind === 'rerun' || item.base_version != null;
        return {
          version,
          label: item.label || `V${version}`,
          created_at: item.created_at || '',
          instruction: rawInstruction || (isRerun ? EMPTY_RERUN_INSTRUCTION : ''),
          kind,
          base_version: toFiniteVersion(item.base_version),
          title: item.title || '',
          plan_approved_at: item.plan_approved_at || '',
          report_completed_at: item.report_completed_at || '',
          report_duration_seconds: item.report_duration_seconds,
          rerun_details: item.rerun_details && typeof item.rerun_details === 'object'
            ? item.rerun_details
            : {},
        };
      }
      const version = Number(item);
      if (!Number.isFinite(version)) return null;
      return { version, label: `V${version}`, created_at: '' };
    })
    .filter(Boolean)
    .sort((a, b) => a.version - b.version);
}

function toFiniteVersion(value) {
  const version = Number(value);
  return Number.isFinite(version) ? version : null;
}

let reportVersionLoadSerial = 0;
let reportVersionLoadTarget = null;

function reportInteractionBusy() {
  return !!(
    state.sessionReport.running
    || partialRerunRunning
    || state.qaLoading
    || state.reportVersionLoading
    || state.historyLoading
  );
}

function activeReportInteractionBusy() {
  return !!(
    partialRerunRunning
    ||
    state.qaLoading
    || state.reportVersionLoading
    || state.historyLoading
    || (state.viewMode === 'session' && state.sessionReport.running)
  );
}

function updateReportActionAvailability() {
  const busy = activeReportInteractionBusy();
  const renameBtn = $('btn-report-rename');
  if (renameBtn) renameBtn.disabled = !activeReportId() || busy;
  const exportDropdown = $('btn-export-dropdown');
  if (exportDropdown) exportDropdown.disabled = busy;
  const partialRerunBtn = $('btn-report-partial-rerun');
  if (partialRerunBtn) partialRerunBtn.disabled = !activeReportId() || busy;
  const hasSession = !!(
    state.sessionId
    || state.sessionReport.reportMd
    || state.sessionReport.running
  );
  const sessionBtn = $('btn-report-session');
  const historyBtn = $('btn-report-history');
  const backBtn = $('btn-report-back-session');
  if (sessionBtn) sessionBtn.disabled = (state.qaLoading || state.reportVersionLoading || state.historyLoading) || !hasSession;
  if (historyBtn) historyBtn.disabled = state.qaLoading || state.reportVersionLoading || state.historyLoading;
  if (backBtn) backBtn.disabled = busy;
  if (busy) $('export-dropdown-menu')?.classList.remove('open');
}

function syncReportVersionMeta(target, meta = {}) {
  if (!target) return;
  const normalized = normalizeReportVersions(meta.versions ?? target.versions);
  target.versions = normalized;
  target.versionInstructions = target.versionInstructions || {};
  normalized.forEach(item => {
    if (item.instruction) target.versionInstructions[item.version] = item.instruction;
  });
  const latestVersion = normalized.at(-1);
  target.lastVersionInstruction = latestVersion?.kind === 'regenerate'
    ? latestVersion.instruction
    : '';
  target.maxVersions = Number(meta.max_versions ?? target.maxVersions ?? 5) || 5;
  target.canGenerateVersion = meta.can_generate_version ?? target.canGenerateVersion ?? (target === state.sessionReport);
  const nextVersion = toFiniteVersion(meta.next_version);
  const version = toFiniteVersion(meta.version);
  const activeVersion = toFiniteVersion(meta.active_version);
  const selectedVersion = toFiniteVersion(meta.selected_version);
  if (nextVersion != null) target.nextVersion = nextVersion;
  if (version != null) target.version = version;
  if (activeVersion != null) target.activeVersion = activeVersion;
  if (selectedVersion != null) target.selectedVersion = selectedVersion;
  if (!target.selectedVersion) target.selectedVersion = target.version || target.activeVersion || normalized.at(-1)?.version || null;
  const selectedSummary = normalized.find(item => item.version === target.selectedVersion);
  const hasDuration = Object.prototype.hasOwnProperty.call(meta, 'report_duration_seconds')
    || Object.prototype.hasOwnProperty.call(selectedSummary || {}, 'report_duration_seconds');
  if (hasDuration) {
    const rawDuration = meta.report_duration_seconds ?? selectedSummary?.report_duration_seconds;
    const parsedDuration = Number(rawDuration);
    target.reportDurationSeconds = (
      rawDuration !== null
      && rawDuration !== undefined
      && rawDuration !== ''
      && Number.isFinite(parsedDuration)
      && parsedDuration >= 0
    ) ? parsedDuration : null;
  }
  const completedAt = meta.report_completed_at ?? selectedSummary?.report_completed_at;
  if (completedAt !== undefined) target.reportCompletedAt = completedAt || '';
  const hasLlmUsage = Object.prototype.hasOwnProperty.call(meta, 'report_llm_usage')
    || Object.prototype.hasOwnProperty.call(selectedSummary || {}, 'report_llm_usage');
  if (hasLlmUsage) {
    target.reportLlmUsage = normalizeReportLlmUsage(meta.report_llm_usage ?? selectedSummary?.report_llm_usage);
  }
}

function activeVersionNumber(ctx = activeReportCtx()) {
  return Number(ctx?.selectedVersion || ctx?.version || ctx?.activeVersion || 0) || null;
}

function reportVersionRevisionText(item) {
  const details = item?.rerun_details || {};
  if (details.target_label) {
    const base = details.base_version ? `基于 V${details.base_version}` : '局部重做';
    const changed = Array.isArray(details.changed_sections)
      ? details.changed_sections.join('、')
      : '';
    return `${base} · 重做 ${details.target_label}${changed ? ` · 更新 ${changed}` : ''}`;
  }
  return item?.instruction || (item?.version === 1 ? '首次生成' : '未记录修订要求');
}

function withOptionalVersion(url, version) {
  const numericVersion = toFiniteVersion(version);
  return numericVersion != null ? `${url}?version=${encodeURIComponent(numericVersion)}` : url;
}

async function loadSessionReportVersion(version) {
  const requestId = ++reportVersionLoadSerial;
  const sessionId = state.sessionId;
  reportVersionLoadTarget = version;
  state.reportVersionLoading = true;
  updateReportVersionUi();
  applyQAAvailability();
  try {
    const resp = await fetch(`/api/report/${sessionId}?version=${encodeURIComponent(version)}`);
    const data = await resp.json().catch(() => ({}));
    if (requestId !== reportVersionLoadSerial || state.sessionId !== sessionId) return false;
    if (!resp.ok) throw new Error(data.detail || '加载报告版本失败');
    state.viewMode = 'session';
    state.historyId = null;
    state.sessionReport.reportMd = data.report_md || '';
    state.sessionReport.title = data.title || reportTitleFromMarkdown(data.report_md || '');
    state.sessionReport.comparisonValidation = data.comparison_validation || {};
    state.sessionReport.qaMessages = normalizeQAMessages(data.qa_messages || []);
    state.sessionReport.qaHtml = '';
    state.sessionReport.feishuLinkHtml = '';
    syncReportVersionMeta(state.sessionReport, {
      ...data,
      report_llm_usage: data.report_llm_usage,
      version: data.version ?? version,
      selected_version: data.version ?? version,
    });
    renderReportWorkspace(state.sessionReport.reportMd, { preserveQa: true });
    return true;
  } finally {
    if (requestId === reportVersionLoadSerial) {
      reportVersionLoadTarget = null;
      state.reportVersionLoading = false;
      updateReportVersionUi();
      applyQAAvailability();
    }
  }
}

async function loadHistoryReportVersion(version) {
  const requestId = ++reportVersionLoadSerial;
  const historyId = state.historyReport.id;
  reportVersionLoadTarget = version;
  state.reportVersionLoading = true;
  updateReportVersionUi();
  applyQAAvailability();
  try {
    const resp = await fetch(`/api/history/${historyId}?version=${encodeURIComponent(version)}`);
    const data = await resp.json().catch(() => ({}));
    if (
      requestId !== reportVersionLoadSerial
      || state.historyReport.id !== historyId
    ) return false;
    if (!resp.ok) throw new Error(data.detail || '加载历史版本失败');
    state.viewMode = 'history';
    state.historyId = historyId;
    state.historyReport.reportMd = data.report_md || '';
    state.historyReport.title = data.title || reportTitleFromMarkdown(data.report_md || '');
    state.historyReport.comparisonValidation = data.comparison_validation || {};
    state.historyReport.qaMessages = normalizeQAMessages(data.qa_messages || []);
    state.historyReport.qaHtml = '';
    state.historyReport.feishuLinkHtml = '';
    syncReportVersionMeta(state.historyReport, {
      ...data,
      report_llm_usage: data.report_llm_usage,
      version: data.version ?? version,
      selected_version: data.version ?? version,
    });
    renderReportWorkspace(state.historyReport.reportMd, { preserveQa: true });
    return true;
  } finally {
    if (requestId === reportVersionLoadSerial) {
      reportVersionLoadTarget = null;
      state.reportVersionLoading = false;
      updateReportVersionUi();
      applyQAAvailability();
    }
  }
}

function updateReportVersionUi() {
  const bar = $('report-version-bar');
  const picker = $('report-version-picker');
  const trigger = $('report-version-trigger');
  const value = $('report-version-value');
  const menu = $('report-version-menu');
  const manageBtn = $('btn-report-version-manage');
  const ctx = activeReportCtx();
  const versions = normalizeReportVersions(ctx?.versions);
  const hasVersions = versions.length > 0;
  const generatingVersion = toFiniteVersion(state.sessionReport.generatingVersion);
  const linkedHistoryId = String(state.sessionReport.pendingVersionRequest?.linkedHistoryId || '');
  const isLinkedHistoryRunning = state.viewMode === 'history'
    && linkedHistoryId
    && linkedHistoryId === String(state.historyReport.id || state.historyId || '');
  const isVersionRunning = state.sessionReport.running
    && generatingVersion != null
    && (state.viewMode === 'session' || isLinkedHistoryRunning);
  if (bar) bar.hidden = !hasVersions;
  const displayedVersion = state.reportVersionLoading && reportVersionLoadTarget != null
    ? reportVersionLoadTarget
    : activeVersionNumber(ctx) || versions.at(-1)?.version;
  const pickerDisabled = !hasVersions
    || state.qaLoading
    || state.reportVersionLoading
    || state.historyLoading
    || (state.viewMode === 'session' && state.sessionReport.running);
  if (value) value.textContent = displayedVersion ? `V${displayedVersion}` : '';
  if (trigger) {
    trigger.disabled = pickerDisabled;
    trigger.title = isVersionRunning ? `正在生成 V${generatingVersion}` : '选择报告版本';
  }
  if (menu) {
    menu.innerHTML = versions.map(item => {
      const isSelected = item.version === displayedVersion;
      const isActive = item.version === ctx?.activeVersion;
      const revision = reportVersionRevisionText(item);
      return `
        <button class="report-version-picker__option${isSelected ? ' report-version-picker__option--selected' : ''}"
          type="button" role="option" aria-selected="${isSelected}" data-report-version-option="${item.version}">
          <span class="report-version-picker__option-version">V${item.version}${isActive ? ' · 当前生效' : ''}</span>
          <span class="report-version-picker__option-note">${esc(revision)}</span>
        </button>`;
    }).join('');
  }
  if (picker && (!hasVersions || pickerDisabled)) {
    setReportVersionMenuOpen(false);
  }
  if (manageBtn) {
    manageBtn.hidden = !(state.viewMode === 'history' && versions.length > 1);
    manageBtn.disabled = reportInteractionBusy();
  }
  updateReportActionAvailability();
}

async function runStats(options = {}) {
  if (state.sessionReport.running) return;
  const generationSessionId = state.sessionId;
  if (!generationSessionId) {
    showToast('当前分析任务已失效，请重新上传文件', 'error');
    return;
  }
  const generationRequestId = (Number(state.reportGenerationSerial) || 0) + 1;
  state.reportGenerationSerial = generationRequestId;
  const isCurrentGeneration = () => (
    state.reportGenerationSerial === generationRequestId
    && state.sessionId === generationSessionId
  );
  const isLinkedRerun = options.linkedRerun === true;
  const linkedHistoryId = String(
    options.historyId || state.sessionReport.pendingVersionRequest?.linkedHistoryId || '',
  ).trim();
  const generationBaseVersion = toFiniteVersion(
    options.baseVersion || state.sessionReport.pendingVersionRequest?.baseVersion,
  );
  const targetVersion = toFiniteVersion(
    options.targetVersion || state.sessionReport.pendingVersionRequest?.targetVersion,
  );
  const generationInstruction = String(
    options.instruction ?? state.sessionReport.pendingVersionRequest?.instruction ?? '',
  ).trim();
  state.sessionReport.generatingVersion = isLinkedRerun ? targetVersion : null;
  if (isLinkedRerun) {
    state.sessionReport.pendingVersionRequest = {
      linkedHistoryId,
      baseVersion: generationBaseVersion,
      targetVersion,
      instruction: generationInstruction,
    };
  }
  state.viewMode = 'session';
  state.historyId = null;
  state.sessionReport.running = true;
  state.sessionReport.stream = '';
  state.sessionReport.reportMd = null;
  state.sessionReport.title = '';
  state.sessionReport.reportDurationSeconds = null;
  state.sessionReport.reportCompletedAt = '';
  state.sessionReport.reportLlmUsage = null;
  goStep(4);
  resetReportFailureUi();
  $('ps-stats').classList.remove('progress-step--done', 'progress-step--failed');
  $('ps-stats').classList.add('progress-step--active');
  $('ps-writing').classList.remove('progress-step--active', 'progress-step--done', 'progress-step--failed');
  $('report-stream-container').style.display = 'none';
  $('report-stream-content').textContent = '';
  updateReportVersionUi();
  applyQAAvailability();

  let generationStatusTimer = null;
  let generationCompleted = false;

  try {
    const statsResp = await fetch(`/api/stats/${generationSessionId}`, { method: 'POST' });
    if (!isCurrentGeneration()) return;
    if (!statsResp.ok) {
      const d = await statsResp.json();
      throw new Error(d.detail || '统计计算失败');
    }
    $('ps-stats').classList.remove('progress-step--active');
    $('ps-stats').classList.add('progress-step--done');
    $('ps-writing').classList.add('progress-step--active');
    $('report-stream-container').style.display = 'block';
    if (isLinkedRerun && targetVersion) showToast(`开始生成 V${targetVersion}`, 'info', 3000);
    let fullReport = '';
    let currentStep = 0;
    let totalSteps = 0;
    let currentTask = '正在准备报告内容';
    let completedSteps = 0;
    const taskProgress = _createReportTaskProgress();
    let stepStartedAt = Date.now();
    let lastSignalAt = Date.now();

    const renderGenerationStatus = () => {
      if (!isCurrentGeneration()) return;
      const now = Date.now();
      const waitedMs = now - stepStartedAt;
      const connectionText = now - lastSignalAt < 45000 ? '连接正常' : '正在等待服务响应';
      const waitingText = waitedMs >= 60000
        ? '本步骤内容较多，AI 仍在生成和校验'
        : '本步骤完成并校验后会一次性显示';
      const title = $('report-stream-status-title');
      const meta = $('report-stream-status-meta');
      const count = $('report-stream-count');
      const progress = $('report-generation-progress');
      const progressBar = $('report-generation-progress-bar');
      const structuredCurrent = taskProgress.structured ? taskProgress.current : null;
      const titleText = structuredCurrent?.scope_key != null
        ? _reportQuestionLabel(structuredCurrent)
        : (structuredCurrent?.message || (
          currentStep && totalSteps
            ? `正在生成 ${currentStep}/${totalSteps}：${currentTask}`
            : currentTask
        ));
      const percent = taskProgress.structured
        ? _reportProgressPercent(taskProgress)
        : (totalSteps ? Math.round((completedSteps / totalSteps) * 100) : 0);

      if (title) title.textContent = titleText;
      if (meta) {
        const statusText = structuredCurrent?.message || waitingText;
        meta.textContent = `${statusText} · ${connectionText} · 已等待 ${_formatReportWaitTime(waitedMs)}`;
      }
      if (count) {
        if (structuredCurrent?.phase === 'themes' && Number(structuredCurrent.item_total)) {
          const done = [...taskProgress.items.values()].filter(item =>
            ['completed', 'degraded', 'skipped'].includes(item.status)
          ).length;
          count.textContent = `已处理 ${done}/${structuredCurrent.item_total} 道题`;
        } else if (structuredCurrent?.phase_index) {
          count.textContent = `第 ${structuredCurrent.phase_index}/${structuredCurrent.phase_total || 4} 阶段`;
        } else {
          count.textContent = totalSteps
            ? `已完成 ${completedSteps}/${totalSteps} 个生成步骤`
            : '正在准备生成步骤';
        }
      }
      if (progress) progress.setAttribute('aria-valuenow', String(percent));
      if (progressBar) progressBar.style.width = `${percent}%`;
    };

    renderGenerationStatus();
    generationStatusTimer = window.setInterval(renderGenerationStatus, 1000);

    const onReportEvent = ev => {
      if (!isCurrentGeneration()) return;
      lastSignalAt = Date.now();
      if (ev.type === 'analysis_progress') {
        _applyAnalysisProgress(taskProgress, ev);
        _renderReportPhaseTrack(taskProgress);
        const remaining = $('report-progress-remaining');
        if (remaining) remaining.textContent = _reportRemainingText(taskProgress);
        currentTask = ev.message || currentTask;
        stepStartedAt = Date.now();
        renderGenerationStatus();
        const el = $('report-stream-content');
        if (el && !fullReport) _renderReportPreparationSteps(el, taskProgress);
      }
      if (ev.type === 'report_llm_status') {
        state.sessionReport.reportLlmUsage = normalizeReportLlmUsage(ev.report_llm_usage);
        if (state.viewMode === 'session') {
          renderReportLlmUsage({ progressState: taskProgress, running: true });
        }
      }
      if (ev.type === 'progress') {
        // 服务端按完整章节输出；状态区持续说明当前步骤，避免等待期间像卡死。
        const parsed = _parseReportProgress(ev.message);
        if (parsed) {
          currentStep = parsed.current;
          totalSteps = parsed.total;
          completedSteps = Math.max(0, currentStep - 1);
          currentTask = parsed.task;
          if (taskProgress.structured && taskProgress.current?.phase === 'writing') {
            taskProgress.current = {
              ...taskProgress.current,
              message: `正在生成 ${parsed.current}/${parsed.total}：${parsed.task}`,
              item_index: parsed.current,
              item_total: parsed.total,
            };
          }
        } else {
          currentTask = ev.message || currentTask;
          if (taskProgress.structured && taskProgress.current?.phase === 'writing') {
            taskProgress.current = {
              ...taskProgress.current,
              message: ev.message || taskProgress.current.message,
            };
          }
        }
        stepStartedAt = Date.now();
        renderGenerationStatus();
        const el = $('report-stream-content');
        if (el && !fullReport) {
          _appendReportProgressDetail(taskProgress, ev.message);
          _renderReportPreparationSteps(el, taskProgress);
        }
      }
      if (ev.type === 'heartbeat') renderGenerationStatus();
      if (ev.type === 'chunk') {
        const chunk = ev.content || '';
        const isFirstChunk = !fullReport;
        fullReport += chunk;
        state.sessionReport.stream = fullReport;
        if (state.viewMode === 'session') {
          const el = $('report-stream-content');
          if (el) {
            // 流式阶段只追加新文本，避免每个分片都重新解析整份 Markdown。
            if (isFirstChunk) {
              el.textContent = '';
              el.classList.remove('report-stream-content--waiting');
            }
            el.append(document.createTextNode(chunk));
            el.scrollTop = el.scrollHeight;
          }
        }
      }
      if (ev.type === 'report_done') {
        const completedLlmUsage = ev.report_llm_usage ?? state.sessionReport.reportLlmUsage;
        generationCompleted = true;
        completedSteps = totalSteps || completedSteps;
        renderGenerationStatus();
        state.sessionReport.running = false;
        resetReportFailureUi();
        state.sessionReport.id = generationSessionId;
        state.sessionReport.reportMd = ev.report_md;
        state.sessionReport.title = reportTitleFromMarkdown(ev.report_md);
        state.sessionReport.comparisonValidation = ev.comparison_validation || {};
        state.sessionReport.reportNo = ev.report_no || state.sessionReport.reportNo || '';
        state.sessionReport.qaMessages = [];
        state.sessionReport.qaHtml = '';
        state.sessionReport.feishuLinkHtml = '';
        state.sessionReport.reportLlmUsage = normalizeReportLlmUsage(
          completedLlmUsage,
        );
        const doneVersion = toFiniteVersion(ev.version) || (isLinkedRerun ? targetVersion : null);
        syncReportVersionMeta(state.sessionReport, {
          ...ev,
          versions: ev.versions || ev.report_versions || state.sessionReport.versions,
          active_version: ev.active_version || ev.active_report_version || doneVersion,
          version: doneVersion ?? ev.version,
          selected_version: doneVersion ?? state.sessionReport.selectedVersion,
        });
        if (doneVersion != null && isLinkedRerun) {
          const note = generationInstruction || EMPTY_RERUN_INSTRUCTION;
          state.sessionReport.versionInstructions[doneVersion] = note;
          state.sessionReport.lastVersionInstruction = note;
        }
        if (state.viewMode === 'session') {
          state.historyId = null;
          showReport(ev.report_md, { notify: !isLinkedRerun });
        } else {
          updateReportContextSwitch();
        }
        if (isLinkedRerun) {
          const versionLabel = doneVersion ? `V${doneVersion}` : '新版本';
          showToast(`${versionLabel} 已生成，并已加入原历史报告`, 'success', 7000);
          if (linkedHistoryId && typeof refreshHistoryEntryAfterGeneration === 'function') {
            refreshHistoryEntryAfterGeneration(linkedHistoryId).catch(error => {
              console.warn('[report] Refresh linked history failed:', error);
            });
          }
        } else if (state.viewMode !== 'session') {
          showToast('当前报告已生成完成，可点击「当前分析」查看', 'success', 7000);
        }
      }
    };
    await consumeSSE(`/api/report/${generationSessionId}`, onReportEvent);
  } catch (e) {
    if (!isCurrentGeneration()) return;
    state.sessionReport.running = false;
    if (isLinkedRerun) {
      showReportFailureUi(e.message);
      showToast(`新版本生成失败：${e.message}`, 'error', 7000);
    } else {
      showReportFailureUi(e.message);
      showToast(`报告生成失败：${state.sessionReport.error}`, 'error', 7000);
    }
  } finally {
    if (generationStatusTimer) window.clearInterval(generationStatusTimer);
    if (!isCurrentGeneration()) return;
    if (isLinkedRerun && generationCompleted) state.sessionReport.pendingVersionRequest = null;
    state.sessionReport.generatingVersion = null;
    updateReportVersionUi();
    applyQAAvailability();
  }
  return generationCompleted;
}

$('btn-report-retry')?.addEventListener('click', () => {
  if (state.sessionReport.running) return;
  const pending = state.sessionReport.pendingVersionRequest;
  if (pending?.linkedHistoryId) {
    runStats({
      linkedRerun: true,
      historyId: pending.linkedHistoryId,
      baseVersion: pending.baseVersion,
      targetVersion: pending.targetVersion,
      instruction: pending.instruction,
    });
    return;
  }
  runStats();
});

function applyCoreHighlight() {
  const content = $('report-content');
  if (!content) return;

  const detailDividerText = '以下为详细信息，各位可以按需查看';
  const isDetailDivider = el => {
    if (!el || el.tagName !== 'P') return false;
    const text = el.textContent.trim();
    return text === detailDividerText
      || /^-{3,}\s*以下为详细信息，各位可以按需查看\s*-{3,}$/.test(text);
  };

  const detailDivider = Array.from(content.querySelectorAll('p')).find(isDetailDivider) || null;
  if (detailDivider) {
    detailDivider.textContent = detailDividerText;
    detailDivider.classList.add('report-detail-divider');
  }

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
      if (isDetailDivider(el)) break;
      if (el.tagName === 'H3') {
        el.textContent = el.textContent.replace(/\s*[:：]\s*关键发现\s*$/, '').trim();
      }
      toWrap.push(el);
      el = el.nextElementSibling;
    }
    wrapElements(toWrap, 'core-summary-box');
  }

  const isSummaryTitle = text => /^(本章总结|本节总结|章节总结|本部分总结)\s*[:：]?$/.test(text.trim());
  const summaryHeadings = Array.from(content.querySelectorAll('h3, h4')).filter(h => isSummaryTitle(h.textContent));
  summaryHeadings.forEach(heading => {
    const items = [heading];
    const summaryBody = heading.nextElementSibling;
    if (summaryBody?.tagName === 'P') items.push(summaryBody);
    wrapElements(items, 'chapter-summary-box');
  });

  const inlineSummaries = Array.from(content.querySelectorAll('p')).filter(p => {
    if (p.closest('.core-highlight-box')) return false;
    return /^(本章总结|本节总结|章节总结|本部分总结)\s*[:：]/.test(p.textContent.trim());
  });
  inlineSummaries.forEach(p => {
    const items = [p];
    const summaryList = p.nextElementSibling;
    if (summaryList && ['OL', 'UL'].includes(summaryList.tagName)) items.push(summaryList);
    wrapElements(items, 'chapter-summary-box');
  });
}

function prepareReportMarkdownForPreview(md) {
  return String(md || '').replace(/(\d)~(?=\d)/g, (_, digit) => `${digit}\\~`);
}

function removeLegacyStatsChartPayloads() {
  const content = $('report-content');
  if (!content) return;
  content.querySelectorAll('pre code.language-stats-chart').forEach(code => {
    code.closest('pre')?.remove();
  });
}

function enhanceReportTables() {
  const content = $('report-content');
  if (!content) return;

  const percentValue = text => {
    const match = String(text || '').match(/(-?\d+(?:\.\d+)?)\s*%/);
    if (!match) return null;
    return Math.min(100, Math.max(0, Number(match[1])));
  };
  const leadingNumber = text => {
    const match = String(text || '').trim().match(/^(\d+(?:\.\d+)?)/);
    return match ? Number(match[1]) : null;
  };
  const markLowSample = (cell, showBadge = false) => {
    if (!cell) return;
    cell.classList.add('report-cell--low-sample');
    cell.title = '样本不足，仅供参考';
    if (showBadge && !cell.querySelector('.report-low-sample-note')) {
      const note = document.createElement('span');
      note.className = 'report-low-sample-note';
      note.textContent = '样本不足，仅供参考';
      cell.appendChild(note);
    }
  };

  content.querySelectorAll('table').forEach(table => {
    const headers = Array.from(table.querySelectorAll('thead th'));
    if (!headers.length) return;
    const headerLabels = headers.map(th => th.textContent.trim());
    const actionHeaders = ['建议内容', '优先级', '产品动作', '验证方式', '依据', '不确定性/前提'];
    const isActionTable = headerLabels.length === actionHeaders.length
      && actionHeaders.every((label, index) => headerLabels[index] === label);
    if (isActionTable) {
      table.classList.add('report-action-table');
      if (!table.parentElement?.classList.contains('report-table-scroll')) {
        const wrapper = document.createElement('div');
        wrapper.className = 'report-table-scroll report-action-table-scroll';
        table.parentNode.insertBefore(wrapper, table);
        wrapper.appendChild(table);
      }
    }
    const percentIndexes = headers
      .map((th, index) => /占比|比例|百分比/.test(th.textContent) ? index : -1)
      .filter(index => index >= 0);
    const sampleIndex = headers.findIndex(th =>
      /样本量|有效样本|回答人数|该画像总计/.test(th.textContent)
    );
    const metricIndexes = headers
      .map((th, index) => /频数|人数|占比|比例|百分比|样本量|有效样本|回答人数|该画像总计|均值|中位数|标准差/.test(th.textContent)
        ? index : -1)
      .filter(index => index >= 0);
    const isStatsTable = metricIndexes.length > 0
      && /选项|取值|子项|画像/.test(headerLabels[0] || '');
    if (isStatsTable) {
      table.classList.add('report-stats-table');
      table.style.setProperty('--report-stats-min-width', `${Math.max(680, headers.length * 112)}px`);
      metricIndexes.forEach(index => headers[index]?.classList.add('report-stats-table__metric'));
      if (!table.parentElement?.classList.contains('report-table-scroll')) {
        const wrapper = document.createElement('div');
        wrapper.className = 'report-table-scroll report-stats-table-scroll';
        table.parentNode.insertBefore(wrapper, table);
        wrapper.appendChild(table);
      }
    }

    table.querySelectorAll('tbody tr').forEach(row => {
      const cells = Array.from(row.cells);
      if (isStatsTable) {
        metricIndexes.forEach(index => cells[index]?.classList.add('report-stats-table__metric'));
      }
      percentIndexes.forEach(index => {
        const cell = cells[index];
        const percent = percentValue(cell?.textContent);
        if (cell && percent !== null) {
          cell.classList.add('report-data-bar');
          cell.style.setProperty('--report-data-percent', `${percent}%`);
        }
      });

      cells.forEach(cell => {
        if (/\d+\s*\*/.test(cell.textContent || '')) markLowSample(cell, true);
      });

      if (sampleIndex >= 0) {
        const sampleCell = cells[sampleIndex];
        const sampleN = leadingNumber(sampleCell?.textContent);
        if (sampleN !== null && sampleN < 5) {
          cells.forEach(cell => markLowSample(cell));
          markLowSample(sampleCell, true);
        }
      }
    });
  });
}

let _reportTocScrollHandler = null;

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
  const reportBody = document.querySelector('#panel-5 .report-document .report-body');
  if (_reportTocScrollHandler && reportBody) {
    reportBody.removeEventListener('scroll', _reportTocScrollHandler);
    _reportTocScrollHandler = null;
  }
  if (!filtered.length) { $('report-toc').style.display = 'none'; return; }
  $('report-toc').style.display = '';
  tocList.innerHTML = '';
  const links = [];
  filtered.forEach((h, idx) => {
    if (!h.id) h.id = `toc-h-${idx}`;
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.href = `#${h.id}`;
    a.textContent = h.textContent;
    a.title = h.textContent;
    a.setAttribute('aria-label', h.textContent);
    a.classList.add(`report-toc__link--${h.tagName.toLowerCase()}`);
    a.addEventListener('click', e => {
      e.preventDefault();
      if (reportBody) {
        const top = h.getBoundingClientRect().top - reportBody.getBoundingClientRect().top + reportBody.scrollTop - 24;
        reportBody.scrollTo({ top, behavior: 'smooth' });
      } else {
        h.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
    links.push(a);
    li.appendChild(a);
    tocList.appendChild(li);
  });

  if (reportBody) {
    const updateActiveToc = () => {
      const marker = reportBody.scrollTop + 72;
      let activeIndex = 0;
      filtered.forEach((heading, index) => {
        const top = heading.getBoundingClientRect().top
          - reportBody.getBoundingClientRect().top
          + reportBody.scrollTop;
        if (top <= marker) activeIndex = index;
      });
      links.forEach((link, index) => link.classList.toggle('toc-active', index === activeIndex));
    };
    let activeFrame = null;
    _reportTocScrollHandler = () => {
      if (activeFrame) cancelAnimationFrame(activeFrame);
      activeFrame = requestAnimationFrame(updateActiveToc);
    };
    reportBody.addEventListener('scroll', _reportTocScrollHandler, { passive: true });
    updateActiveToc();
  }
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
  const duration = formatReportDuration(state.sessionReport.reportDurationSeconds);
  if (duration && state.viewMode === 'session') {
    html += `<span class="report-toolbar__step-sep"> · </span><span class="report-toolbar__step">总耗时 ${esc(duration)}</span>`;
  }
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
  const isHistory = state.viewMode === 'history' && !!state.historyReport.reportMd;
  const historyMeta = $('report-history-meta');
  const historyInfo = $('report-history-info');
  const breadcrumb = $('report-breadcrumb');
  const backBtn = $('btn-report-back-session');

  bar.style.display = '';
  sessionBtn.classList.toggle('report-context-switch__btn--active', state.viewMode === 'session');
  sessionBtn.disabled = !hasSession;
  historyBtn.style.display = '';
  historyBtn.classList.toggle('report-context-switch__btn--active', isHistory);
  historyBtn.textContent = '历史报告';
  historyBtn.title = '打开历史记录';

  if (breadcrumb) breadcrumb.hidden = isHistory;
  if (historyMeta) historyMeta.hidden = !isHistory;
  if (backBtn) backBtn.hidden = !hasSession;
  if (historyInfo && isHistory) {
    const ctx = state.historyReport;
    const modeLabel = ctx.mode === 'comment' ? '评论分析' : ctx.mode === 'annotate' ? '数据标注' : '问卷分析';
    let createdText = '';
    if (ctx.createdAt) {
      const created = new Date(ctx.createdAt);
      if (!Number.isNaN(created.getTime())) {
        const pad = value => String(value).padStart(2, '0');
        createdText = `${created.getFullYear()}-${pad(created.getMonth() + 1)}-${pad(created.getDate())} ${pad(created.getHours())}:${pad(created.getMinutes())}`;
      }
    }
    const duration = formatReportDuration(ctx.reportDurationSeconds);
    const durationText = duration ? `总耗时 ${duration}` : '';
    historyInfo.textContent = [ctx.reportNo, createdText, modeLabel, durationText].filter(Boolean).join(' · ');
  }
  updateReportVersionUi();
  renderReportLlmUsage({ running: state.viewMode === 'session' && state.sessionReport.running });
}

function applyQAAvailability() {
  const input = $('qa-input');
  const btn = $('btn-qa-send');
  if (!input || !btn) return;
  if (state.reportVersionLoading || state.historyLoading) {
    input.placeholder = state.historyLoading ? '正在加载历史报告，请稍候' : '正在切换报告版本，请稍候';
    input.disabled = true;
    btn.disabled = true;
    return;
  }
  if (state.viewMode === 'session' && state.sessionReport.running) {
    input.placeholder = '报告生成中，暂时不能追问';
    input.disabled = true;
    btn.disabled = true;
    return;
  }
  if (state.viewMode === 'history') {
    const canChat = !!state.historyReport.reportMd;
    input.placeholder = canChat ? '可基于该历史报告发起或继续追问（Enter 发送）' : '该历史记录没有可追问的报告';
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

function comparisonAuditRow(label, value, className = '') {
  const row = document.createElement('div');
  row.className = `comparison-validation__row ${className}`.trim();
  const labelNode = document.createElement('strong');
  labelNode.textContent = label;
  const valueNode = document.createElement('span');
  valueNode.textContent = String(value || '—');
  row.append(labelNode, valueNode);
  return row;
}

function renderComparisonValidation(validation) {
  const panel = $('comparison-validation');
  const statusNode = $('comparison-validation-status');
  const countsNode = $('comparison-validation-counts');
  const body = $('comparison-validation-body');
  if (!panel || !statusNode || !countsNode || !body) return;

  const audit = validation && typeof validation === 'object' ? validation : {};
  const status = String(audit.status || 'legacy');
  const labels = {
    passed: '统计比较校验通过',
    repaired: '统计比较校验已自动修正',
    needs_review: '统计比较校验发现待确认风险',
    incomplete: '统计比较校验未完整执行',
    legacy: '此版本无统计比较校验记录',
  };
  panel.dataset.status = labels[status] ? status : 'incomplete';
  statusNode.textContent = (
    status === 'passed' && Number(audit.catalog_group_count || 0) === 0
      ? '未发现可确定校验的量表比较'
      : labels[status] || labels.incomplete
  );
  body.replaceChildren();

  if (status === 'legacy') {
    countsNode.textContent = '';
    body.append(comparisonAuditRow(
      '说明',
      '该版本生成于本校验功能上线之前，不能据此判断正文中的比较结论已经通过复核。',
    ));
    panel.open = false;
    return;
  }

  const detected = Number(audit.detected_count || 0);
  const applied = Number(audit.applied_count || 0);
  const unresolved = Number(audit.unresolved_count || 0);
  countsNode.textContent = `发现 ${detected} · 修改 ${applied} · 待确认 ${unresolved}`;
  if (audit.coverage) body.append(comparisonAuditRow('校验范围', audit.coverage));
  if (status === 'passed' && Number(audit.catalog_group_count || 0) === 0) {
    body.append(comparisonAuditRow(
      '覆盖说明',
      '本版本没有识别出至少两个可按同一口径比较的量表项目，因此没有执行具体比较句复核。',
    ));
  }
  if (audit.error) {
    body.append(comparisonAuditRow('执行状态', audit.error, 'comparison-validation__row--risk'));
  }
  if (audit.repair_error) {
    body.append(comparisonAuditRow('修补状态', audit.repair_error, 'comparison-validation__row--risk'));
  }
  if (audit.risk) {
    body.append(comparisonAuditRow('风险提示', audit.risk, 'comparison-validation__row--risk'));
  }

  const changes = Array.isArray(audit.changes) ? audit.changes : [];
  changes.forEach((change, index) => {
    const item = document.createElement('section');
    item.className = 'comparison-validation__item comparison-validation__item--changed';
    const title = document.createElement('h4');
    title.textContent = `自动修改 ${index + 1} · ${change.section || '报告正文'}`;
    item.append(
      title,
      comparisonAuditRow('修改前', change.original),
      comparisonAuditRow('修改后', change.replacement),
      comparisonAuditRow('事实依据', change.factual_basis),
      comparisonAuditRow('复核说明', change.risk),
    );
    body.append(item);
  });

  const unresolvedItems = Array.isArray(audit.unresolved) ? audit.unresolved : [];
  unresolvedItems.forEach((issue, index) => {
    const item = document.createElement('section');
    item.className = 'comparison-validation__item comparison-validation__item--risk';
    const title = document.createElement('h4');
    title.textContent = `待人工确认 ${index + 1} · ${issue.section || '报告正文'}`;
    item.append(
      title,
      comparisonAuditRow('原文', issue.original),
      comparisonAuditRow('不一致', (issue.reasons || []).join('；')),
      comparisonAuditRow('事实依据', issue.factual_basis),
      comparisonAuditRow('风险', issue.risk),
    );
    body.append(item);
  });

  const warnings = Array.isArray(audit.parser_warnings) ? audit.parser_warnings : [];
  if (warnings.length) {
    body.append(comparisonAuditRow('修补响应提示', warnings.join('；')));
  }
  if (!body.childElementCount) {
    body.append(comparisonAuditRow('结果', '未发现需要修改的统计比较表述。'));
  }
  panel.open = status !== 'passed';
}

function renderReportWorkspace(md, { preserveQa = true } = {}) {
  state.reportMd = md;
  if (state.viewMode === 'history') showReportPanelPreservingProgress();
  else goStep(5);

  const ctx = activeReportCtx();
  renderComparisonValidation(ctx.comparisonValidation);
  const title = ctx.title || reportTitleFromMarkdown(md);
  $('report-title-display').textContent = title;
  const renameBtn = $('btn-report-rename');
  if (renameBtn) {
    const reportId = activeReportId();
    renameBtn.dataset.reportId = reportId;
    renameBtn.disabled = !reportId;
  }

  const reportContent = $('report-content');
  try {
    reportContent.innerHTML = renderMarkdown(prepareReportMarkdownForPreview(md));
  } catch (error) {
    console.error('[report] Markdown render failed, falling back to plain text:', error);
    reportContent.textContent = md || '';
  }
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
  updateReportVersionUi();
  renderReportLlmUsage({ running: state.viewMode === 'session' && state.sessionReport.running });
  applyCoreHighlight();
  removeLegacyStatsChartPayloads();
  enhanceReportTables();
  buildTOC();
  switchReportTab('report');
  renderReportBreadcrumb();
  updateQaBadge();
}

function showReport(md, { notify = true } = {}) {
  const ctx = activeReportCtx();
  ctx.reportMd = md;
  ctx.title = reportTitleFromMarkdown(md);
  renderReportWorkspace(md, { preserveQa: true });
  if (notify && state.viewMode === 'session') showToast('报告生成完毕！', 'success');
}

function switchReportContext(mode) {
  if (state.qaLoading || state.reportVersionLoading || state.historyLoading) {
    showToast('当前操作完成后才能切换报告', 'info');
    return;
  }
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
    renderReportLlmUsage({ running: true });
  } else if (state.sessionReport.error) {
    goStep(4);
    $('report-stream-content').textContent = state.sessionReport.stream || '';
    showReportFailureUi(state.sessionReport.error);
  } else {
    setViewStep(Math.min(state.currentStep, 4));
  }
  updateReportContextSwitch();
}

$('btn-report-session')?.addEventListener('click', () => switchReportContext('session'));
$('btn-report-history')?.addEventListener('click', () => {
  if (state.qaLoading || state.reportVersionLoading || state.historyLoading) {
    showToast('当前操作完成后才能打开其他报告', 'info');
    return;
  }
  if (typeof openHistoryDrawer === 'function') {
    openHistoryDrawer();
    return;
  }
  openDrawer('history-drawer');
  loadHistory();
});
$('btn-report-back-session')?.addEventListener('click', () => switchReportContext('session'));
function setReportLlmPopoverOpen(open) {
  const popover = $('report-llm-popover');
  const trigger = $('btn-report-llm-usage');
  const panel = $('report-toolbar-llm');
  if (!popover || !trigger || !panel) return;
  const nextOpen = Boolean(open) && !popover.hidden;
  popover.classList.toggle('report-llm-popover--open', nextOpen);
  trigger.setAttribute('aria-expanded', String(nextOpen));
  panel.hidden = !nextOpen;
}

$('btn-report-llm-usage')?.addEventListener('click', e => {
  e.stopPropagation();
  const trigger = e.currentTarget;
  setReportVersionMenuOpen(false);
  $('export-dropdown-menu')?.classList.remove('open');
  setReportLlmPopoverOpen(trigger.getAttribute('aria-expanded') !== 'true');
});

function setReportVersionMenuOpen(open) {
  const picker = $('report-version-picker');
  const trigger = $('report-version-trigger');
  const menu = $('report-version-menu');
  if (!picker || !trigger || !menu) return;
  const nextOpen = Boolean(open) && !trigger.disabled;
  picker.classList.toggle('report-version-picker--open', nextOpen);
  trigger.setAttribute('aria-expanded', String(nextOpen));
  menu.hidden = !nextOpen;
}

$('report-version-trigger')?.addEventListener('click', e => {
  e.stopPropagation();
  const trigger = e.currentTarget;
  setReportLlmPopoverOpen(false);
  setReportVersionMenuOpen(trigger.getAttribute('aria-expanded') !== 'true');
});

$('report-version-menu')?.addEventListener('click', async e => {
  const option = e.target.closest('[data-report-version-option]');
  if (!option) return;
  e.stopPropagation();
  const version = Number(option.dataset.reportVersionOption);
  setReportVersionMenuOpen(false);
  if (!Number.isFinite(version) || version === activeVersionNumber(activeReportCtx())) return;
  try {
    if (state.viewMode === 'history') {
      await loadHistoryReportVersion(version);
    } else {
      await loadSessionReportVersion(version);
    }
  } catch (error) {
    showToast(error.message, 'error');
    updateReportVersionUi();
  }
});

document.addEventListener('click', e => {
  if (!e.target.closest('#report-version-picker')) setReportVersionMenuOpen(false);
  if (!e.target.closest('#report-llm-popover')) setReportLlmPopoverOpen(false);
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    setReportVersionMenuOpen(false);
    setReportLlmPopoverOpen(false);
  }
});

function closeReportVersionManageModal() {
  const modal = $('report-version-manage-modal');
  if (modal) modal.hidden = true;
  document.body.style.removeProperty('overflow');
}

function renderReportVersionManageList() {
  const list = $('report-version-manage-list');
  if (!list) return;
  const versions = normalizeReportVersions(state.historyReport.versions);
  list.innerHTML = versions.map(item => {
    const isActive = item.version === state.historyReport.activeVersion;
    const note = reportVersionRevisionText(item) || EMPTY_RERUN_INSTRUCTION;
    return `<div class="report-version-delete-row">
      <div class="report-version-delete-row__content">
        <div class="report-version-delete-row__title">V${item.version}${isActive ? ' · 当前生效' : ''}</div>
        <div class="report-version-delete-row__meta">${esc(item.created_at || '生成时间未记录')}</div>
        <div class="report-version-delete-row__instruction">${esc(note)}</div>
      </div>
      <div class="report-version-delete-row__actions">
        <button class="btn btn--ghost" type="button" data-history-delete-version="${item.version}"
          ${versions.length <= 1 || reportInteractionBusy() ? 'disabled' : ''}>删除 V${item.version}</button>
      </div>
    </div>`;
  }).join('');
}

function openReportVersionManageModal() {
  if (state.viewMode !== 'history' || normalizeReportVersions(state.historyReport.versions).length <= 1) return;
  renderReportVersionManageList();
  $('report-version-manage-modal').hidden = false;
  document.body.style.overflow = 'hidden';
}

$('btn-report-version-manage')?.addEventListener('click', openReportVersionManageModal);
$('btn-report-version-manage-close')?.addEventListener('click', closeReportVersionManageModal);
document.querySelectorAll('[data-version-manage-close]').forEach(node => {
  node.addEventListener('click', closeReportVersionManageModal);
});
$('report-version-manage-list')?.addEventListener('click', async e => {
  const deleteBtn = e.target.closest('[data-history-delete-version]');
  if (!deleteBtn || reportInteractionBusy()) return;
  const version = Number(deleteBtn.dataset.historyDeleteVersion);
  const historyId = String(state.historyReport.id || state.historyId || '');
  if (!Number.isFinite(version) || !historyId) return;
  if (!window.confirm(`确定删除 V${version} 吗？删除后不可恢复。`)) return;

  const requestId = ++reportVersionLoadSerial;
  const previousSelectedVersion = activeVersionNumber(state.historyReport);
  state.reportVersionLoading = true;
  updateReportVersionUi();
  applyQAAvailability();
  renderReportVersionManageList();
  try {
    const resp = await fetch(`/api/history/${encodeURIComponent(historyId)}/versions/${version}`, { method: 'DELETE' });
    const data = await resp.json().catch(() => ({}));
    if (requestId !== reportVersionLoadSerial || String(state.historyReport.id || '') !== historyId) return;
    if (!resp.ok) throw new Error(data.detail || '删除旧版本失败');
    syncReportVersionMeta(state.historyReport, {
      ...data,
      versions: data.versions || data.report_versions || [],
      active_version: data.active_version || data.active_report_version || data.version,
    });
    const versions = normalizeReportVersions(state.historyReport.versions);
    const selectedStillExists = versions.some(item => item.version === previousSelectedVersion);
    const nextVersion = selectedStillExists
      ? previousSelectedVersion
      : toFiniteVersion(data.active_version || data.active_report_version || data.version) || versions.at(-1)?.version;
    state.historyReport.selectedVersion = nextVersion || null;
    state.reportVersionLoading = false;
    if (!selectedStillExists && nextVersion) await loadHistoryReportVersion(nextVersion);
    if (typeof refreshHistoryEntryAfterGeneration === 'function') {
      await refreshHistoryEntryAfterGeneration(historyId);
    }
    showToast(`已删除 V${version}`, 'success');
    if (versions.length <= 1) closeReportVersionManageModal();
    else renderReportVersionManageList();
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    if (requestId === reportVersionLoadSerial) state.reportVersionLoading = false;
    updateReportVersionUi();
    applyQAAvailability();
    renderReportVersionManageList();
  }
});

function closePartialRerunModal() {
  if (partialRerunRunning) return;
  const modal = $('report-partial-rerun-modal');
  if (modal) modal.hidden = true;
  partialRerunContext = null;
  document.body.style.removeProperty('overflow');
}

function partialRerunTargets(type) {
  const capability = partialRerunContext?.capability || {};
  return type === 'part'
    ? (Array.isArray(capability.parts) ? capability.parts : [])
    : (Array.isArray(capability.questions) ? capability.questions : []);
}

function renderPartialRerunTargetOptions() {
  const type = $('report-partial-rerun-type')?.value || 'question';
  const targetSelect = $('report-partial-rerun-target');
  if (!targetSelect) return;
  const targets = partialRerunTargets(type);
  targetSelect.innerHTML = targets.map(item => {
    const value = type === 'part' ? item.part_index : item.scope_key;
    const label = type === 'part'
      ? item.part_title
      : `${item.question_name}（${item.part_title}，${item.response_count} 条）`;
    return `<option value="${esc(value)}">${esc(label)}</option>`;
  }).join('');
  targetSelect.disabled = partialRerunRunning || !targets.length;
  renderPartialRerunImpact();
}

function selectedPartialRerunTarget() {
  const type = $('report-partial-rerun-type')?.value || 'question';
  const key = String($('report-partial-rerun-target')?.value || '');
  const targets = partialRerunTargets(type);
  const item = targets.find(target => String(
    type === 'part' ? target.part_index : target.scope_key,
  ) === key);
  return { type, key, item };
}

function renderPartialRerunImpact() {
  const impact = $('report-partial-rerun-impact');
  if (!impact) return;
  const { type, item } = selectedPartialRerunTarget();
  if (!item) {
    impact.textContent = '当前类型没有可重做目标。';
    return;
  }
  const scopeCount = type === 'part' ? (item.scope_keys || []).length : 1;
  const partTitle = item.part_title;
  impact.textContent = [
    `实际分析调用：${scopeCount ? `${scopeCount} 个目标 scope` : '无开放题模型分析'}`,
    `报告写回：${partTitle}、核心结论、行动建议`,
    '保持不变：一级标题、其他 Part、Bug 模块和无关统计',
  ].join('；');
}

async function openPartialRerunModal() {
  if (reportInteractionBusy()) return;
  const historyId = String(activeReportId() || '');
  const baseVersion = activeVersionNumber();
  if (!historyId || !baseVersion) {
    showToast('当前报告没有可绑定的历史版本', 'error');
    return;
  }
  const button = $('btn-report-partial-rerun');
  if (button) button.disabled = true;
  try {
    const resp = await fetch(
      `/api/history/${encodeURIComponent(historyId)}?version=${encodeURIComponent(baseVersion)}`,
      { credentials: 'same-origin', cache: 'no-store' },
    );
    const detail = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(detail.detail || '读取局部重做范围失败');
    const capability = detail.partial_rerun || {};
    if (!capability.available) {
      showToast(capability.reason || '该版本暂不支持局部重做', 'info', 7000);
      return;
    }
    if (Number(detail.version_count || 0) >= Number(detail.max_versions || 5)) {
      showToast(`报告版本已达上限（${detail.max_versions || 5} 个），请先删除一个旧版本`, 'info', 7000);
      return;
    }
    partialRerunContext = { historyId, baseVersion, capability, detail };
    $('report-partial-rerun-base').textContent = `基础版本：V${baseVersion} · ${detail.title || '分析报告'}`;
    const typeSelect = $('report-partial-rerun-type');
    typeSelect.value = (capability.questions || []).length ? 'question' : 'part';
    typeSelect.disabled = false;
    $('report-partial-rerun-instruction').value = '';
    $('report-partial-rerun-progress').hidden = true;
    $('btn-report-partial-rerun-start').textContent = '开始局部重做';
    renderPartialRerunTargetOptions();
    $('report-partial-rerun-modal').hidden = false;
    document.body.style.overflow = 'hidden';
  } catch (error) {
    showToast(error.message, 'error', 7000);
  } finally {
    updateReportActionAvailability();
  }
}

async function startPartialRerun() {
  if (partialRerunRunning || !partialRerunContext) return;
  const { type, key, item } = selectedPartialRerunTarget();
  if (!item || !key) {
    showToast('请选择要重做的题目或 Part', 'info');
    return;
  }
  const { historyId, baseVersion, detail } = partialRerunContext;
  const instruction = String($('report-partial-rerun-instruction')?.value || '').trim();
  let doneEvent = null;
  partialRerunRunning = true;
  $('report-partial-rerun-progress').hidden = false;
  $('report-partial-rerun-progress-title').textContent = `基于 V${baseVersion} 局部重做`;
  $('report-partial-rerun-progress-message').textContent = '正在校验基础版本与数据指纹…';
  $('btn-report-partial-rerun-start').disabled = true;
  $('btn-report-partial-rerun-cancel').disabled = true;
  $('btn-report-partial-rerun-close').disabled = true;
  $('report-partial-rerun-type').disabled = true;
  $('report-partial-rerun-target').disabled = true;
  $('report-partial-rerun-instruction').disabled = true;
  updateReportActionAvailability();
  try {
    await consumeSSEPost(
      `/api/history/${encodeURIComponent(historyId)}/partial-rerun`,
      {
        base_version: baseVersion,
        target_type: type,
        target_key: key,
        instruction,
      },
      ev => {
        if (ev.type === 'partial_rerun_progress') {
          $('report-partial-rerun-progress-title').textContent = `步骤 ${ev.phase_index || 1}/${ev.phase_total || 5}`;
          $('report-partial-rerun-progress-message').textContent = ev.message || '正在局部重做';
        } else if (ev.type === 'partial_rerun_done') {
          doneEvent = ev;
          const details = ev.rerun_details || {};
          const seconds = Number(details.elapsed_seconds || 0);
          $('report-partial-rerun-progress-title').textContent = `V${ev.version} 已生成`;
          $('report-partial-rerun-progress-message').textContent = [
            `重做 ${details.target_label || '目标范围'}`,
            `更新 ${(details.changed_sections || []).join('、')}`,
            seconds ? `耗时 ${formatReportDuration(seconds)}` : '',
            '未执行整份报告重跑',
          ].filter(Boolean).join('；');
        }
      },
    );
    if (!doneEvent) throw new Error('局部重做连接结束，但没有收到新版本确认');
    state.viewMode = 'history';
    state.historyId = historyId;
    state.historyReport.id = historyId;
    state.historyReport.reportNo = detail.report_no || '';
    state.historyReport.createdAt = detail.created_at || '';
    state.historyReport.mode = detail.mode || 'survey';
    syncReportVersionMeta(state.historyReport, {
      ...doneEvent,
      versions: doneEvent.versions || [],
      version: doneEvent.version,
      selected_version: doneEvent.version,
    });
    await loadHistoryReportVersion(doneEvent.version);
    const details = doneEvent.rerun_details || {};
    showToast(
      `V${doneEvent.version} 已生成：只重做 ${details.target_label || '所选范围'}，未整份重跑`,
      'success',
      8000,
    );
    partialRerunRunning = false;
    closePartialRerunModal();
    if (typeof refreshHistoryEntryAfterGeneration === 'function') {
      refreshHistoryEntryAfterGeneration(historyId).catch(error => {
        console.warn('[partial-rerun] Refresh history failed:', error);
      });
    }
  } catch (error) {
    $('report-partial-rerun-progress-title').textContent = '局部重做失败';
    $('report-partial-rerun-progress-message').textContent = `${error.message}；基础版本未被覆盖。`;
    showToast(`局部重做失败：${error.message}`, 'error', 8000);
  } finally {
    partialRerunRunning = false;
    $('btn-report-partial-rerun-start').disabled = false;
    $('btn-report-partial-rerun-cancel').disabled = false;
    $('btn-report-partial-rerun-close').disabled = false;
    $('report-partial-rerun-type').disabled = false;
    $('report-partial-rerun-target').disabled = false;
    $('report-partial-rerun-instruction').disabled = false;
    updateReportActionAvailability();
  }
}

$('btn-report-partial-rerun')?.addEventListener('click', openPartialRerunModal);
$('report-partial-rerun-type')?.addEventListener('change', renderPartialRerunTargetOptions);
$('report-partial-rerun-target')?.addEventListener('change', renderPartialRerunImpact);
$('btn-report-partial-rerun-start')?.addEventListener('click', startPartialRerun);
$('btn-report-partial-rerun-cancel')?.addEventListener('click', closePartialRerunModal);
$('btn-report-partial-rerun-close')?.addEventListener('click', closePartialRerunModal);
document.querySelectorAll('[data-partial-rerun-close]').forEach(node => {
  node.addEventListener('click', closePartialRerunModal);
});

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
  const title = data.title || reportTitleFromMarkdown(data.report_md || '');
  if (state.sessionId === data.id) {
    const reportMd = replaceReportTitleInMarkdown(
      state.sessionReport.reportMd || '',
      title,
    );
    state.sessionReport.title = title;
    state.sessionReport.reportNo = data.report_no || state.sessionReport.reportNo || '';
    state.sessionReport.reportMd = reportMd;
  }
  if (state.historyReport.id === data.id) {
    const reportMd = replaceReportTitleInMarkdown(
      state.historyReport.reportMd || '',
      title,
    );
    state.historyReport.title = title;
    state.historyReport.reportNo = data.report_no || state.historyReport.reportNo || '';
    state.historyReport.reportMd = reportMd;
  }
  if ((state.viewMode === 'history' && state.historyId === data.id) || (state.viewMode === 'session' && state.sessionId === data.id)) {
    saveActiveReportUi();
    const reportMd = replaceReportTitleInMarkdown(
      activeReportCtx().reportMd || state.reportMd || '',
      title,
    );
    activeReportCtx().title = title;
    activeReportCtx().reportMd = reportMd;
    state.reportMd = reportMd;
    renderReportWorkspace(reportMd, { preserveQa: true });
  }
}

function startReportTitleEdit() {
  if (reportInteractionBusy()) {
    showToast('当前操作完成后才能修改报告名称', 'info');
    return;
  }
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
  if (state.reportVersionLoading || (state.viewMode === 'session' && state.sessionReport.running)) {
    showToast('当前报告版本准备完成后再导出', 'info');
    return;
  }
  const version = activeVersionNumber();
  if (state.viewMode === 'history' && state.historyId) {
    window.location.href = withOptionalVersion(`/api/export/word-history/${state.historyId}`, version);
  } else {
    window.location.href = withOptionalVersion(`/api/export/word/${state.sessionId}`, version);
  }
});

// ── 飞书登录状态 + 权限门控 ──
state.feishu = {
  configured: false, logged_in: false, allowed: true, name: '', email: '',
  perms: ['survey', 'interview', 'annotate', 'comment'], is_admin: false
};

function applyPermGating() {
  const perms = state.feishu.perms || [];
  const hasSurvey = perms.includes('survey');
  const hasInterview = perms.includes('interview');
  const hasAnnotate = perms.includes('annotate');
  const hasComment = perms.includes('comment');
  // 侧边栏：无权限则隐藏入口
  const navSurvey = $('nav-survey');
  const navInterview = $('nav-interview');
  const navAnnotate = $('nav-annotate');
  const navComment = $('nav-comment');
  if (navSurvey) navSurvey.style.display = hasSurvey ? '' : 'none';
  if (navInterview) navInterview.style.display = hasInterview ? '' : 'none';
  if (navAnnotate) navAnnotate.style.display = hasAnnotate ? '' : 'none';
  if (navComment) navComment.style.display = hasComment ? '' : 'none';
  // 如果当前模式无权限，切换到第一个有权限的模式
  const allowedModes = [
    ['survey', hasSurvey], ['interview', hasInterview],
    ['annotate', hasAnnotate], ['comment', hasComment],
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
      ? `个人中心 · ${state.feishu.name || state.feishu.email || '已登录'}`
      : '登录飞书';
  }
  applyPermGating();
  if (typeof window.syncProfileFromFeishuStatus === 'function') {
    window.syncProfileFromFeishuStatus(state.feishu);
  }
}

// ── 飞书文档导出 ──
$('btn-export-pdf').addEventListener('click', () => {
  if (state.reportVersionLoading || (state.viewMode === 'session' && state.sessionReport.running)) {
    showToast('当前报告版本准备完成后再导出', 'info');
    return;
  }
  const version = activeVersionNumber();
  if (state.viewMode === 'history' && state.historyId) {
    window.location.href = withOptionalVersion(`/api/export/pdf-history/${state.historyId}`, version);
  } else if (state.sessionId) {
    window.location.href = withOptionalVersion(`/api/export/pdf/${state.sessionId}`, version);
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
  if (state.reportVersionLoading || (state.viewMode === 'session' && state.sessionReport.running)) {
    showToast('当前报告版本准备完成后再导出', 'info');
    return;
  }
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

  const exportMode = state.viewMode;
  const exportReportId = activeReportId();
  const exportVersion = activeVersionNumber();
  const isSameExportTarget = () => (
    state.viewMode === exportMode
    && activeReportId() === exportReportId
    && activeVersionNumber() === exportVersion
  );

  const btn = $('btn-export-feishu');
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = '导出中…';

  const url = exportMode === 'history'
    ? withOptionalVersion(`/api/export/feishu-history/${exportReportId}`, exportVersion)
    : withOptionalVersion(`/api/export/feishu/${exportReportId}`, exportVersion);
  try {
    const resp = await fetch(url, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) {
      if (resp.status === 401) { showToast('飞书登录已过期，请重新登录', 'error'); await refreshFeishuStatus(); }
      throw new Error(data.detail || '生成失败');
    }
    if (isSameExportTarget()) showFeishuLink(data.url);
    const versionText = exportVersion ? `V${exportVersion}` : '当前报告';
    try { await navigator.clipboard.writeText(data.url); showToast(`${versionText} 飞书文档已创建，链接已复制，机器人消息已发送`, 'success'); }
    catch { showToast(`${versionText} 飞书文档已创建，机器人消息已发送`, 'success'); }
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
  setReportLlmPopoverOpen(false);
  setReportVersionMenuOpen(false);
  $('export-dropdown-menu').classList.toggle('open');
});
document.addEventListener('click', e => {
  const dropdown = $('export-dropdown');
  const menu = $('export-dropdown-menu');
  if (menu && dropdown && !dropdown.contains(e.target)) menu.classList.remove('open');
});
$('btn-export-md').addEventListener('click', () => {
  if (state.reportVersionLoading || (state.viewMode === 'session' && state.sessionReport.running)) {
    showToast('当前报告版本准备完成后再导出', 'info');
    return;
  }
  const blob = new Blob([state.reportMd || ''], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${($('report-title-display').textContent || '调研报告')}.md`;
  a.click();
  URL.revokeObjectURL(url);
});

$('btn-qa-send').addEventListener('click', sendQA);
$('qa-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendQA(); }
});

async function sendQA() {
  if (
    state.qaLoading
    || state.reportVersionLoading
    || (state.viewMode === 'session' && state.sessionReport.running)
  ) return;
  const question = $('qa-input').value.trim();
  if (!question) return;

  state.qaLoading = true;
  $('btn-qa-send').disabled = true;
  $('qa-input').value = '';
  updateReportVersionUi();

  const qaMode = state.viewMode;
  const qaCtx = activeReportCtx();
  const qaReportId = activeReportId();
  const qaVersion = activeVersionNumber(qaCtx);
  const isSameQaTarget = () => (
    state.viewMode === qaMode
    && activeReportId() === qaReportId
    && activeVersionNumber(activeReportCtx()) === qaVersion
  );
  appendQABubble('user', question);
  let typingBubble = null;

  const ensureTypingBubble = () => {
    if (!typingBubble) typingBubble = appendQABubble('ai', null, true);
    return typingBubble;
  };
  ensureTypingBubble();

  try {
    let answer = '';
    let finalAnswer = '';

    const url = qaMode === 'history' ? '/api/history-qa' : '/api/qa';
    const body = qaMode === 'history'
      ? { history_id: qaReportId, question, version: qaVersion }
      : { session_id: qaReportId, question, version: qaVersion };

    await consumeSSEPost(url, body, ev => {
      if (ev.type === 'qa_scope') {
        if (!isSameQaTarget()) return;
        const scopeBubble = appendQABubble('ai', `本次追问的信息范围：${ev.message}`);
        const typingMessage = typingBubble?.parentElement;
        if (typingMessage && scopeBubble?.parentElement === $('qa-messages')) {
          $('qa-messages').insertBefore(scopeBubble.parentElement, typingMessage);
        }
      }
      if (ev.type === 'chunk') {
        answer += ev.content;
        if (isSameQaTarget()) {
          ensureTypingBubble().innerHTML = renderMarkdown(answer);
          typingBubble.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
      }
      if (ev.type === 'qa_done') {
        finalAnswer = ev.answer || answer;
        if (isSameQaTarget()) {
          ensureTypingBubble().innerHTML = renderMarkdown(finalAnswer);
        }
      }
    });
    finalAnswer = finalAnswer || answer;
    if (finalAnswer && isSameQaTarget()) {
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
      ensureTypingBubble().innerHTML = `❌ 飞书登录态已失效，请<a href="${esc(loginUrl)}" style="color:var(--accent)">重新登录</a>后再追问`;
      showToast('飞书登录态已失效，请重新登录', 'error', 8000);
    } else {
      ensureTypingBubble().textContent = `❌ ${e.message}`;
      showToast(`追问失败：${e.message}`, 'error');
    }
  } finally {
    state.qaLoading = false;
    if (isSameQaTarget()) saveActiveReportUi();
    applyQAAvailability();
    updateReportVersionUi();
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
