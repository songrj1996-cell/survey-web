// ============================================================
// STEP 4: Stats + Report
// ============================================================

function resetReportFailureUi() {
  state.sessionReport.error = '';
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

function _parseReportProgress(message) {
  const match = String(message || '').match(/分章生成\s+(\d+)\/(\d+)[：:]\s*(.+)/);
  if (!match) return null;
  return {
    current: Number(match[1]),
    total: Number(match[2]),
    task: match[3].replace(/[.…]+$/, '').trim(),
  };
}

const EMPTY_RERUN_INSTRUCTION = '未填写补充要求，本次为重新生成';

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
    || state.qaLoading
    || state.reportVersionLoading
    || state.historyLoading
  );
}

function activeReportInteractionBusy() {
  return !!(
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
}

function activeVersionNumber(ctx = activeReportCtx()) {
  return Number(ctx?.selectedVersion || ctx?.version || ctx?.activeVersion || 0) || null;
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
    state.sessionReport.qaMessages = normalizeQAMessages(data.qa_messages || []);
    state.sessionReport.qaHtml = '';
    state.sessionReport.feishuLinkHtml = '';
    syncReportVersionMeta(state.sessionReport, {
      ...data,
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
    state.historyReport.qaMessages = normalizeQAMessages(data.qa_messages || []);
    state.historyReport.qaHtml = '';
    state.historyReport.feishuLinkHtml = '';
    syncReportVersionMeta(state.historyReport, {
      ...data,
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
      const revision = item.instruction || (item.version === 1 ? '首次生成' : '未记录修订要求');
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
      const titleText = currentStep && totalSteps
        ? `正在生成 ${currentStep}/${totalSteps}：${currentTask}`
        : currentTask;
      const percent = totalSteps ? Math.round((completedSteps / totalSteps) * 100) : 0;

      if (title) title.textContent = titleText;
      if (meta) {
        meta.textContent = `${waitingText} · ${connectionText} · 已等待 ${_formatReportWaitTime(waitedMs)}`;
      }
      if (count) {
        count.textContent = totalSteps
          ? `已完成 ${completedSteps}/${totalSteps} 个生成步骤`
          : '正在准备生成步骤';
      }
      if (progress) progress.setAttribute('aria-valuenow', String(percent));
      if (progressBar) progressBar.style.width = `${percent}%`;
    };

    renderGenerationStatus();
    generationStatusTimer = window.setInterval(renderGenerationStatus, 1000);

    const onReportEvent = ev => {
      if (!isCurrentGeneration()) return;
      lastSignalAt = Date.now();
      if (ev.type === 'progress') {
        // 服务端按完整章节输出；状态区持续说明当前步骤，避免等待期间像卡死。
        const parsed = _parseReportProgress(ev.message);
        if (parsed) {
          currentStep = parsed.current;
          totalSteps = parsed.total;
          completedSteps = Math.max(0, currentStep - 1);
          currentTask = parsed.task;
        } else {
          currentTask = ev.message || currentTask;
        }
        stepStartedAt = Date.now();
        renderGenerationStatus();
        const el = $('report-stream-content');
        if (el && !fullReport) {
          el.textContent = '当前章节或模块完成并校验后，将在这里显示。';
          el.classList.add('report-stream-content--waiting');
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
        generationCompleted = true;
        completedSteps = totalSteps || completedSteps;
        renderGenerationStatus();
        state.sessionReport.running = false;
        resetReportFailureUi();
        state.sessionReport.id = generationSessionId;
        state.sessionReport.reportMd = ev.report_md;
        state.sessionReport.title = reportTitleFromMarkdown(ev.report_md);
        state.sessionReport.reportNo = ev.report_no || state.sessionReport.reportNo || '';
        state.sessionReport.qaMessages = [];
        state.sessionReport.qaHtml = '';
        state.sessionReport.feishuLinkHtml = '';
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
    historyInfo.textContent = [ctx.reportNo, createdText, modeLabel].filter(Boolean).join(' · ');
  }
  updateReportVersionUi();
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
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') setReportVersionMenuOpen(false);
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
    const note = item.instruction || (item.version === 1 ? '首次生成' : EMPTY_RERUN_INSTRUCTION);
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
      ? `飞书：${state.feishu.name || state.feishu.email || '已登录'}`
      : '登录飞书';
  }
  applyPermGating();
}

function showFeishuLogoutConfirmModal(account) {
  return new Promise(resolve => {
    const existing = $('feishu-logout-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'feishu-logout-modal';
    modal.style.cssText = 'position:fixed;inset:0;z-index:200;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.5);backdrop-filter:blur(4px);';
    modal.innerHTML = `
      <div role="dialog" aria-modal="true" aria-labelledby="feishu-logout-title"
           style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
                  padding:24px 28px;width:min(400px,90vw);display:flex;flex-direction:column;gap:16px;
                  box-shadow:var(--shadow-lg)">
        <div id="feishu-logout-title" style="font-size:15px;font-weight:600;color:var(--text)">退出飞书登录？</div>
        <div style="font-size:13px;color:var(--text-2);line-height:1.7">
          当前账号为 <strong style="color:var(--text)">${esc(account)}</strong>。退出后，如需继续使用飞书相关功能，需要重新登录授权。
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn btn--ghost" id="feishu-logout-cancel" type="button">取消</button>
          <button class="btn btn--primary" id="feishu-logout-confirm" type="button">确认退出</button>
        </div>
      </div>`;
    document.body.appendChild(modal);

    const onKeydown = event => {
      if (event.key === 'Escape') cleanup(false);
    };
    const cleanup = result => {
      document.removeEventListener('keydown', onKeydown);
      modal.remove();
      resolve(result);
    };
    $('feishu-logout-cancel').onclick = () => cleanup(false);
    $('feishu-logout-confirm').onclick = () => cleanup(true);
    modal.addEventListener('click', event => {
      if (event.target === modal) cleanup(false);
    });
    document.addEventListener('keydown', onKeydown);
    $('feishu-logout-cancel').focus();
  });
}

$('btn-feishu-login').addEventListener('click', async () => {
  if (!state.feishu.configured) {
    showToast('服务端未配置飞书应用（FEISHU_APP_ID/SECRET/REDIRECT_URI）', 'error');
    return;
  }
  if (state.feishu.logged_in) {
    const account = state.feishu.email || state.feishu.name || '当前账号';
    const confirmed = await showFeishuLogoutConfirmModal(account);
    if (!confirmed) return;
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
