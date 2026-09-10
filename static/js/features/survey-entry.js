// Unified questionnaire import and report-focus workflow.
'use strict';

const surveyEntry = {
  method: 'local', files: { responses: null, questionnaire: null, statistics: null },
  busy: false, sourceBusy: false, columnsLoading: false, preferredFocus: 'insight', focus: 'insight',
  statsSource: 'python', importSignature: '', familyId: '', platform: 'auto',
};
const ENTRY_FILE_RULES = {
  responses: { label: '问卷回答数据', extension: /\.(csv|xlsx)$/i },
  questionnaire: { label: '调研问卷源文件', extension: /\.(xls|xlsx)$/i },
  statistics: { label: '统计结果表', extension: /\.xlsx$/i },
};
const ENTRY_FOCUS = {
  insight: {
    label: '观点洞察优先', open: '深入拆解观点、原因、情境与分歧',
    outline: [['核心观点与决策含义', '呈现主要观点、少数声音和证据边界'], ['原因、情境与分歧', '归纳使用场景与不同玩家的反馈'], ['关键人群差异', '统计数字辅助解释观点'], ['行动建议', '连接玩家证据与待验证问题']],
  },
  statistics: {
    label: '统计解读优先', open: '解释关键统计结果背后的原因',
    outline: [['总体指标表现', '解释满意度、偏好分布与关键结果'], ['核心人群差异', '比较画像和行为分组的统计差异'], ['开放题原因解释', '用代表性反馈解释重要数字'], ['完整统计附录', '保留正式统计结果和数据来源']],
  },
};

function surveyEntryBusy() { return surveyEntry.busy || surveyEntry.sourceBusy || surveyEntry.columnsLoading; }
function surveyConfirmationIsLocked() {
  return state.currentStep > 2 || (surveyEntryBusy() && !surveyEntry.columnsLoading);
}
function surveyUploadIsLocked() { return surveyEntryBusy() || (!!state.sessionId && state.currentStep > 1); }
function entryHasStatistics() { return surveyEntry.method === 'local' && !!surveyEntry.files.statistics; }
function entryStatus(message, error = false) {
  $('qe-upload-status').textContent = message;
  $('qe-upload-status').classList.toggle('is-error', error);
}
function entryError(data, fallback) {
  return typeof data?.detail === 'string' ? data.detail : (data?.detail?.message || fallback);
}
function entrySyncFocus() {
  surveyEntry.focus = entryHasStatistics() ? 'statistics' : surveyEntry.preferredFocus;
}
function entrySetBusy(busy) { surveyEntry.busy = busy; renderSurveyEntry(); }

function renderSurveyEntry() {
  const locked = surveyUploadIsLocked();
  // Include keyboard focus, not only pointer styling, when the source is read-only.
  document.getElementById('qsrc-google-family')?.toggleAttribute('inert', locked);
  const files = surveyEntry.files;
  document.querySelectorAll('[data-entry-method]').forEach(button => {
    const active = button.dataset.entryMethod === surveyEntry.method;
    button.classList.toggle('is-selected', active);
    button.setAttribute('aria-selected', String(active));
    button.disabled = locked;
  });
  $('qe-local-panel').hidden = surveyEntry.method !== 'local';
  $('qe-link-panel').hidden = surveyEntry.method !== 'link';
  $('qe-platform').disabled = locked || !!files.questionnaire;
  $('qe-platform').value = files.questionnaire ? 'bested' : surveyEntry.platform;
  Object.keys(ENTRY_FILE_RULES).forEach(key => {
    const selected = files[key];
    document.querySelector('[data-entry-name="' + key + '"]').textContent = selected?.name || '未选择文件';
    document.querySelector('[data-entry-drop="' + key + '"]').classList.toggle('is-selected', !!selected);
    const remove = document.querySelector('[data-entry-remove="' + key + '"]');
    remove.hidden = !selected;
    remove.disabled = locked;
    document.querySelector('[data-entry-pick="' + key + '"]').disabled = locked;
    $('qe-' + key).disabled = locked;
  });
  $('qe-platform-hint').textContent = files.questionnaire
    ? '已选择倍市得问卷源文件，调研平台固定为倍市得。未上传统计表时，回答数据需为倍市得 XLSX。'
    : '自动识别会参考配套问卷；仅有回答表时按通用表格读取，请在下一步核对题型。';
  $('qe-upload-rule').hidden = !files.statistics;
  $('qe-upload-rule').textContent = files.questionnaire
    ? '已上传专业统计表：后续将固定采用“统计解读优先”。移除统计表后，两种重心重新开放。'
    : '已选择专业统计表：请补充配套的倍市得问卷源文件后继续；后续报告重心固定为“统计解读优先”。';
  $('qe-upload').disabled = locked || !files.responses || (!!files.statistics && !files.questionnaire);
  $('qe-upload').textContent = surveyEntry.busy ? '正在读取数据…' : (state.sessionId ? '数据已上传' : '上传文件并继续');
  renderSurveyFocus();
}

function entryFileChanged(key, file) {
  if (surveyUploadIsLocked()) return;
  if (file && (!ENTRY_FILE_RULES[key].extension.test(file.name) || file.size > 50 * 1024 * 1024)) {
    entryStatus(ENTRY_FILE_RULES[key].label + '格式不支持或超过 50MB，请按文件卡片说明选择。', true);
    $('qe-' + key).value = '';
    return;
  }
  surveyEntry.files[key] = file;
  surveyEntry.importSignature = '';
  entrySyncFocus();
  entryStatus(key === 'statistics' && !file ? '已移除统计表，其他文件已保留；两种报告重心重新开放。' : '');
  renderSurveyEntry();
}

Object.keys(ENTRY_FILE_RULES).forEach(key => {
  const input = $('qe-' + key);
  document.querySelector('[data-entry-pick="' + key + '"]').addEventListener('click', () => input.click());
  document.querySelector('[data-entry-remove="' + key + '"]').addEventListener('click', () => {
    if (surveyUploadIsLocked()) return;
    input.value = '';
    entryFileChanged(key, null);
  });
  input.addEventListener('change', () => {
    if (input.files[0]) entryFileChanged(key, input.files[0]);
  });
  const card = document.querySelector('[data-entry-drop="' + key + '"]');
  card.addEventListener('dragover', event => {
    event.preventDefault();
    if (!surveyUploadIsLocked()) card.classList.add('is-dragging');
  });
  card.addEventListener('dragleave', () => card.classList.remove('is-dragging'));
  card.addEventListener('drop', event => {
    event.preventDefault();
    card.classList.remove('is-dragging');
    if (!surveyUploadIsLocked() && event.dataTransfer.files[0]) entryFileChanged(key, event.dataTransfer.files[0]);
  });
});
document.querySelectorAll('[data-entry-method]').forEach(button => {
  button.addEventListener('click', () => {
    if (surveyUploadIsLocked()) return;
    surveyEntry.method = button.dataset.entryMethod;
    entryStatus('');
    entrySyncFocus();
    renderSurveyEntry();
  });
});
$('qe-platform').addEventListener('change', () => {
  surveyEntry.platform = $('qe-platform').value;
  surveyEntry.importSignature = '';
});
$('qe-copy-account').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('qe-service-account').textContent);
    showToast('服务账号已复制', 'success');
  } catch (_) {
    showToast('无法自动复制，请选中邮箱手动复制', 'info');
  }
});

function entryInitializeSession(data) {
  state.sessionId = data.session_id;
  state.mode = data.mode || null;
  state.surveySource = data.source_type || (entryHasStatistics() ? 'bested' : 'google');
  state.questionnaireUsed = !!data.questionnaire_used;
  const external = data.stats_source === 'external_crosstab' || data.mode === 'crosstab';
  $('qe-confirm-title').textContent = external ? '确认统计结构' : '确认题型';
  $('qe-confirm-description').textContent = external
    ? '题目与统计分组沿用已上传的专业统计表，请核对数据来源后继续。'
    : (state.questionnaireUsed ? '已从问卷源文件读取题型、选项和矩阵结构，请逐一核对。' : '请逐一核对识别出的题型与中文题名。题型直接影响后续统计口径。');
  state.viewMode = 'session';
  state.historyId = null;
  state.columns = null;
  state.planData = null;
  state.reportMd = null;
  state.reportVersionLoading = false;
  clearPlanInput();
  state.sessionReport = {
    id: null, reportMd: null, title: '', reportNo: '', version: null, versions: [],
    activeVersion: null, selectedVersion: null, nextVersion: null, maxVersions: 5,
    canGenerateVersion: true, versionInstructions: {}, qaHtml: '', qaMessages: [],
    feishuLinkHtml: '', running: false, stream: '', pendingVersionRequest: null,
    generatingVersion: null, lastVersionInstruction: '', comparisonValidation: {},
  };
  surveyEntry.statsSource = data.stats_source || 'python';
  state.uploadedFilename = data.filename;
  $('qe-focus-preview').open = false;
  $('context-form-details').open = true;
  $('qe-plan-details').open = false;
  $('qe-focus-status').textContent = '';
  $('qe-plan-settings').hidden = true;
  renderPreview(data);
  refreshContextFormVisibility();
  goStep(2);
  renderSurveyEntry();
}

async function uploadSurveyEntry() {
  if (surveyUploadIsLocked()) return;
  const { responses, questionnaire, statistics } = surveyEntry.files;
  if (!responses || (statistics && !questionnaire)) return;
  if (questionnaire && !statistics && !/\.xlsx$/i.test(responses.name)) {
    entryStatus('配套问卷解析需要倍市得 XLSX 回答数据，请替换回答文件或移除问卷源文件。', true);
    return;
  }
  const source = questionnaire ? 'bested' : (surveyEntry.platform === 'bested' ? 'bested' : 'google');
  const body = new FormData();
  if (statistics) {
    body.append('survey_file', questionnaire);
    body.append('data_file', responses);
    body.append('crosstab_file', statistics);
  } else {
    body.append('file', responses);
    body.append('source_type', source);
    if (questionnaire) body.append('questionnaire_file', questionnaire);
  }
  entrySetBusy(true);
  entryStatus('正在读取文件，请稍候…');
  try {
    const response = await fetch(statistics ? '/api/upload/crosstab' : '/api/upload', { method: 'POST', body });
    const data = await response.json();
    if (!response.ok) throw new Error(entryError(data, '上传失败'));
    if (!data.session_id || !Array.isArray(data.headers)) throw new Error('上传结果不完整，请重试');
    data.source_type = data.source_type || source;
    const signature = contextFileSignature(responses);
    const draft = loadContextDraft();
    const retain = preserveContextDraftOnNextUpload || draft?.fileSignature === signature;
    currentContextFileSignature = signature;
    if (retain && draft) writeContextForm(draft.fields || {});
    else { clearContextDraft(); clearContextForm(); }
    preserveContextDraftOnNextUpload = false;
    surveyEntry.familyId = '';
    entryInitializeSession(data);
    entryStatus('已上传：' + responses.name);
    if (statistics) {
      $('col-list').replaceChildren();
      const summary = document.createElement('div');
      summary.className = 'qe-notice';
      summary.textContent = '已读取专业统计表：' + data.crosstab_questions + ' 道题；统计分组：'
        + (data.crosstab_segments || []).join('、') + '。题目与统计结构以该表为准，无需重新识别题型。';
      $('col-list').appendChild(summary);
      $('col-confirm-count').textContent = '统计结构已确认';
      $('btn-start-plan').disabled = false;
    } else {
      await entryLoadColumns();
    }
  } catch (error) {
    entryStatus(error.message, true);
    showToast('上传失败：' + error.message, 'error');
  } finally { entrySetBusy(false); }
}
$('qe-upload').addEventListener('click', uploadSurveyEntry);

async function acceptGoogleFormsFamilySession(data) {
  if (!data?.questionnaire_family_id || !Array.isArray(data.languages) || !data.session_id) throw new Error('Google Forms 统一会话返回结果无效');
  if (state.sessionId && state.currentStep > 1) throw new Error('当前分析已经开始，不能替换回答来源');
  surveyEntry.method = 'link';
  surveyEntry.familyId = data.questionnaire_family_id;
  currentContextFileSignature = '';
  clearContextDraft();
  clearContextForm();
  entryInitializeSession(data);
  entrySyncFocus();
  entryStatus('已连接多语言 Google Forms 回答');
  const count = Number(data.file_upload_answer_count) || 0;
  if (count) showToast(count + ' 个文件上传回答仅保留 Drive 元数据，文件内容未进入分析', 'info', 8000);
  await entryLoadColumns();
}
async function entryLoadColumns() {
  surveyEntry.columnsLoading = true;
  renderSurveyFocus();
  try {
    await loadColumns();
  } finally {
    surveyEntry.columnsLoading = false;
    renderSurveyFocus();
  }
}
Object.defineProperty(window, 'surveySessionIngress', {
  value: Object.freeze({
    acceptGoogleFormsFamilySession,
    isLocked: surveyUploadIsLocked,
    setSourceBusy(value) { surveyEntry.sourceBusy = value; renderSurveyEntry(); },
  }), configurable: true, enumerable: false, writable: false,
});

function renderSurveyFocus() {
  entrySyncFocus();
  const locked = entryHasStatistics();
  const busy = surveyEntryBusy();
  const readonly = state.currentStep > 2;
  const inputLocked = surveyConfirmationIsLocked();
  $('qe-data-confirm').toggleAttribute('inert', inputLocked);
  $('qe-columns-status').hidden = !surveyEntry.columnsLoading;
  $('qe-focus-lock').hidden = !locked;
  $('qe-insight-disabled').hidden = !locked;
  document.querySelectorAll('[data-entry-focus]').forEach(button => {
    const key = button.dataset.entryFocus;
    const active = key === surveyEntry.focus;
    button.disabled = inputLocked || (locked && key === 'insight');
    button.classList.toggle('is-selected', active);
    button.setAttribute('aria-checked', String(active));
    document.querySelector('[data-entry-focus-badge="' + key + '"]').textContent =
      locked ? (active ? '已自动选择' : '暂不可选') : (active ? '当前选择' : '可选择');
  });
  $('qe-focus-stats').textContent = locked ? '统计来源：已上传的专业统计表' : '统计来源：根据回答数据自动计算；两种报告重心均可选择。';
  $('qe-focus-outline').innerHTML = ENTRY_FOCUS[surveyEntry.focus].outline
    .map(([title, text]) => '<div class="qe-outline-row"><strong>' + esc(title) + '</strong><p>' + esc(text) + '</p></div>').join('');
  $('qe-return-upload').disabled = busy || readonly;
  $('btn-start-plan').disabled = busy || readonly || !state.sessionId || (state.mode !== 'crosstab' && !state.columns);
  $('qe-confirm-action').textContent = surveyEntry.columnsLoading
    ? (state.questionnaireUsed ? '题型读取中，完成后可继续' : '题型识别中，完成后可继续')
    : (busy ? '正在处理…' : (state.sessionId && state.mode !== 'crosstab' && !state.columns
      ? '请先完成题型识别' : '确认并生成分析方案'));
}
document.querySelectorAll('[data-entry-focus]').forEach(button => {
  button.addEventListener('click', () => {
    if (surveyConfirmationIsLocked() || entryHasStatistics()) return;
    surveyEntry.preferredFocus = button.dataset.entryFocus;
    renderSurveyFocus();
  });
});
$('qe-return-upload').addEventListener('click', () => {
  if (surveyEntryBusy() || state.currentStep > 2) return;
  saveContextDraft();
  preserveContextDraftOnNextUpload = true;
  // A new upload creates a new session; never mutate or delete the old session.
  state.sessionId = null;
  state.mode = null;
  state.columns = null;
  state.planData = null;
  surveyEntry.importSignature = '';
  goStep(1);
  renderSurveyEntry();
  entryStatus('请移除统计结果表后继续；回答数据、问卷源文件和调研背景会保留。');
  document.querySelector('[data-entry-remove="statistics"]').focus();
});

function renderSurveyPlanSettings() {
  const selected = ENTRY_FOCUS[surveyEntry.focus];
  const pairs = [
    ['数据导入', surveyEntry.method === 'link' ? 'Google Form Link' : '上传本地文件'],
    ['调研平台', surveyEntry.method === 'link' ? 'Google Forms' : (state.surveySource === 'bested' ? '倍市得' : (surveyEntry.platform === 'google' ? 'Google Forms' : '通用表格（请核对题型）'))],
    ['统计来源', surveyEntry.statsSource === 'external_crosstab' ? '使用已上传的统计结果' : '系统自动统计'],
    ['报告重心', selected.label + (entryHasStatistics() ? '（已固定）' : '')],
    ['开放题作用', selected.open],
  ];
  $('qe-plan-settings-content').innerHTML = pairs.map(([key,value]) => '<dt>' + esc(key) + '</dt><dd>' + esc(value) + '</dd>').join('');
  $('qe-plan-summary').textContent = pairs[3][1] + ' · ' + pairs[2][1];
  $('qe-plan-settings').hidden = false;
}
async function submitSurveyEntry() {
  if (surveyEntryBusy() || !state.sessionId || state.currentStep > 2) return;
  if (state.mode !== 'crosstab' && !state.columns) {
    showToast('请先完成题型识别', 'info');
    return;
  }
  entrySyncFocus();
  entrySetBusy(true);
  renderSurveyFocus();
  $('qe-focus-status').textContent = '正在保存分析方式…';
  try {
    const response = await fetch('/api/analysis-settings/' + state.sessionId, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ report_focus: surveyEntry.focus }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(entryError(data, '保存报告重心失败'));
    state.mode = data.mode || null;
    surveyEntry.statsSource = data.stats_source;
    if (state.mode === 'crosstab') {
      const ctxResponse = await fetch('/api/survey-context/' + state.sessionId, {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(readContextForm()),
      });
      if (!ctxResponse.ok) throw new Error('保存调研背景失败，请重试');
    }
    renderSurveyPlanSettings();
    await startPlan();
    $('qe-focus-status').textContent = '';
  } catch (error) {
    $('qe-focus-status').textContent = error.message;
    showToast(error.message, 'error');
  } finally { entrySetBusy(false); renderSurveyFocus(); }
}

function returnToSurveyFocus() {
  if (surveyEntryBusy() || state.currentStep !== 3 || state.viewMode !== 'session') return;
  goStep(2);
  $('qe-focus-status').textContent = '';
  renderSurveyFocus();
  document.querySelector('[data-entry-focus="' + surveyEntry.focus + '"]').focus();
  document.querySelector('.main').scrollTo({ top: 0, behavior: 'auto' });
}
$('qe-retry-plan').addEventListener('click', returnToSurveyFocus);

function resetUploadZone() {
  surveyEntry.method = 'local';
  surveyEntry.files = { responses: null, questionnaire: null, statistics: null };
  surveyEntry.preferredFocus = 'insight';
  surveyEntry.focus = 'insight';
  surveyEntry.statsSource = 'python';
  surveyEntry.platform = 'auto';
  surveyEntry.familyId = '';
  surveyEntry.importSignature = '';
  state.uploadedFilename = '';
  state.questionnaireUsed = false;
  Object.keys(ENTRY_FILE_RULES).forEach(key => { $('qe-' + key).value = ''; });
  $('qe-focus-preview').open = false;
  $('context-form-details').open = true;
  $('qe-plan-details').open = false;
  $('qe-focus-status').textContent = '';
  $('qe-plan-settings').hidden = true;
  entryStatus('');
  renderSurveyEntry();
}
renderSurveyEntry();
