'use strict';

const IV_V2_FILE_CONTRACT_VERSION = 'interview-file-contract/1.0-draft';
const IV_V2_POLL_INTERVAL_MS = 1800;

const ivV2State = {
  currentStep: 1,
  selectedFile: null,
  uploadAttemptId: '',
  importId: '',
  projectId: '',
  workbookRevisionId: '',
  status: 'idle',
  loadingMessage: '',
  errorMessage: '',
  statusAction: '',
  statusCode: '',
  statusTraceId: '',
  requestBusy: false,
  saveBusy: false,
  confirmBusy: false,
  restoreBusy: false,
  buildBusy: false,
  reviewBusy: false,
  pendingPoll: 0,
  requestToken: 0,
  importData: null,
  mappingResponse: null,
  structureResponse: null,
  reviewIssuesResponse: null,
  draft: null,
  sheetCatalog: {},
  statusNote: '',
  draftDirty: false,
  idempotencyKey: '',
  idempotencyFingerprint: '',
  reviewFilter: 'open',
  selectedIssueId: '',
  issueDrafts: {},
  evidenceContextCache: {},
  contextBusyIssueId: '',
  contextToken: 0,
};

function ivV2$(id) {
  return document.getElementById(id);
}

function ivV2IsActive() {
  const interviewModeActive = typeof currentMode === 'undefined' || currentMode === 'interview';
  return window.ivState?.track === 'v2' && interviewModeActive;
}

function ivV2AllTrackButtons() {
  return Array.from(document.querySelectorAll('[data-iv-track]'));
}

function ivV2NextToken() {
  ivV2State.requestToken += 1;
  return ivV2State.requestToken;
}

function ivV2IsTokenCurrent(token) {
  return token === ivV2State.requestToken;
}

function ivV2ResetPoll() {
  if (ivV2State.pendingPoll) {
    clearTimeout(ivV2State.pendingPoll);
    ivV2State.pendingPoll = 0;
  }
}

function ivV2InvalidateAsync() {
  ivV2NextToken();
  ivV2ResetPoll();
  ivV2State.contextToken += 1;
  ivV2State.contextBusyIssueId = '';
}

function ivV2OperationBusy() {
  return Boolean(
    ivV2State.requestBusy
    || ivV2State.saveBusy
    || ivV2State.restoreBusy
    || ivV2State.confirmBusy
    || ivV2State.buildBusy
    || ivV2State.reviewBusy
  );
}

function ivV2PrecheckActive() {
  return ['uploading', 'QUARANTINED', 'PRECHECKING', 'loading'].includes(ivV2State.status);
}

function ivV2Esc(str) {
  return esc(str ?? '');
}

function ivV2FormatTime(value) {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
}

function ivV2FormatSize(bytes) {
  if (!Number.isFinite(bytes)) return '--';
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function ivV2StatusTone(status) {
  if (['GROUP_MAPPING_CONFIRMED', 'READY_FOR_DOSSIERS', 'ACCEPTED'].includes(status)) return 'success';
  if (status === 'STRUCTURE_REVIEW_REQUIRED') return 'warning';
  if (status === 'REJECTED') return 'danger';
  if (status === 'PRECHECKING' || status === 'loading') return 'info';
  return 'warning';
}

function ivV2HasStructureCheckpoint(status = ivV2State.status) {
  return ['STRUCTURE_REVIEW_REQUIRED', 'READY_FOR_DOSSIERS'].includes(String(status || ''));
}

function ivV2HasConfirmedMapping(status = ivV2State.status) {
  return ['GROUP_MAPPING_CONFIRMED', 'STRUCTURE_REVIEW_REQUIRED', 'READY_FOR_DOSSIERS'].includes(String(status || ''));
}

function ivV2CurrentStructureRevisionId() {
  return String(
    ivV2State.structureResponse?.structure_revision_id
    || ivV2State.reviewIssuesResponse?.structure_revision_id
    || ''
  );
}

function ivV2CurrentEvidenceRevisionId() {
  return String(
    ivV2State.structureResponse?.evidence_revision_id
    || ivV2State.reviewIssuesResponse?.evidence_revision_id
    || ''
  );
}

function ivV2HeadPairFromPayload(payload) {
  return {
    structure_revision_id: String(payload?.structure_revision_id || ''),
    evidence_revision_id: String(payload?.evidence_revision_id || ''),
  };
}

function ivV2HeadsMatch(left, right) {
  return (
    String(left?.structure_revision_id || '') === String(right?.structure_revision_id || '')
    && String(left?.evidence_revision_id || '') === String(right?.evidence_revision_id || '')
  );
}

function ivV2CurrentReviewIssues() {
  return Array.isArray(ivV2State.reviewIssuesResponse?.issues)
    ? ivV2State.reviewIssuesResponse.issues
    : [];
}

function ivV2CurrentStructurePayload() {
  return ivV2State.structureResponse?.structure || null;
}

function ivV2ShowToastFromError(error, fallback) {
  showToast(String(error?.message || fallback || '操作失败'), 'error', 7000);
}

function ivV2RenderUploadStatus() {
  const node = ivV2$('iv-v2-upload-status');
  if (!node) return;
  if (!ivV2State.errorMessage) {
    node.hidden = true;
    node.innerHTML = '';
    return;
  }
  node.hidden = false;
  node.innerHTML = `
    <div class="iv-v2-status-banner iv-v2-status-banner--danger">
      <strong>${ivV2Esc(ivV2State.statusCode ? `${ivV2State.statusCode} · ${ivV2State.errorMessage}` : ivV2State.errorMessage)}</strong>
      ${ivV2State.statusAction ? `<p>${ivV2Esc(ivV2State.statusAction)}</p>` : ''}
    </div>
  `;
}

function ivV2NormalizeApiError(payload, status, fallback) {
  const fromEnvelope = payload?.error || {};
  const detail = payload?.detail;
  const notFound = status === 404;
  return {
    message: String(
      fromEnvelope.message
      || (notFound ? 'V2未启用，当前环境没有挂载访谈 V2 接口' : '')
      || detail
      || fallback
      || '操作失败'
    ),
    suggestedAction: String(fromEnvelope.suggested_action || ''),
    code: String(fromEnvelope.code || (notFound ? 'INTERVIEW_V2_DISABLED' : '')),
    traceId: String(fromEnvelope.trace_id || ''),
  };
}

function ivV2SetStatusError(payload, status, fallback) {
  const normalized = ivV2NormalizeApiError(payload, status, fallback);
  ivV2State.errorMessage = normalized.message;
  ivV2State.statusAction = normalized.suggestedAction;
  ivV2State.statusCode = normalized.code;
  ivV2State.statusTraceId = normalized.traceId;
  ivV2RenderUploadStatus();
}

function ivV2ClearStatusError() {
  ivV2State.errorMessage = '';
  ivV2State.statusAction = '';
  ivV2State.statusCode = '';
  ivV2State.statusTraceId = '';
  ivV2RenderUploadStatus();
}

function ivV2ResetStructureWorkspace() {
  ivV2State.structureResponse = null;
  ivV2State.reviewIssuesResponse = null;
  ivV2State.selectedIssueId = '';
  ivV2State.issueDrafts = {};
  ivV2InvalidateEvidenceContext();
}

function ivV2InvalidateEvidenceContext() {
  ivV2State.contextToken += 1;
  ivV2State.evidenceContextCache = {};
  ivV2State.contextBusyIssueId = '';
}

function ivV2SetReviewError(payload, status, fallback) {
  ivV2SetStatusError(payload, status, fallback);
  ivV2RenderEditor();
  ivV2RenderConfirmed();
}

function ivV2Step3Shell() {
  return ivV2$('iv-v2-confirmed-shell')?.closest('[data-iv-track-content="v2"]') || null;
}

function ivV2RenderStep3Header() {
  const shell = ivV2Step3Shell();
  document.querySelectorAll('[data-iv-v2-step="3"] .step-bar__label').forEach(node => {
    node.textContent = '结构与证据复核';
  });
  if (!shell) return;
  const eyebrow = shell.querySelector('.iv-eyebrow');
  const title = shell.querySelector('.panel__title');
  const desc = shell.querySelector('.panel__desc');
  const previewTitle = ivV2$('iv-v2-confirmed-preview')?.closest('section')?.querySelector('.iv-v2-side-card__title');
  const historyTitle = ivV2$('iv-v2-confirmed-history')?.closest('section')?.querySelector('.iv-v2-side-card__title');
  if (eyebrow) eyebrow.textContent = ivV2HasStructureCheckpoint() ? String(ivV2State.status || 'STRUCTURE_REVIEW_REQUIRED') : 'GROUP_MAPPING_CONFIRMED';
  if (title) {
    title.textContent = ivV2HasStructureCheckpoint()
      ? '结构与证据复核'
      : '分组映射已确认，正在准备结构复核';
  }
  if (desc) {
    desc.textContent = ivV2HasStructureCheckpoint()
      ? '逐项处理结构问题，确认结构树和证据归属；当前阶段只开放复核，不开放玩家档案。'
      : '确认映射后会自动尝试生成结构复核工作台。若失败，可在此重试或返回映射编辑器。';
  }
  if (previewTitle) previewTitle.textContent = '结构与证据复核工作台';
  if (historyTitle) historyTitle.textContent = '版本与映射历史';
}

function ivV2MarkDirty(note = '当前编辑尚未保存') {
  ivV2State.draftDirty = true;
  ivV2State.statusNote = note;
  ivV2RenderEditorStatus();
  ivV2SyncEditorControls();
}

function ivV2ClearDirty(note = '') {
  ivV2State.draftDirty = false;
  if (note) ivV2State.statusNote = note;
}

function ivV2FocusValue() {
  return ivV2$('iv-v2-research-focus')?.value.trim() || '';
}

function ivV2UploadFingerprint() {
  const file = ivV2State.selectedFile;
  if (!file) return '';
  return [
    file.name || '',
    file.size || 0,
    file.lastModified || 0,
    ivV2FocusValue(),
    IV_V2_FILE_CONTRACT_VERSION,
  ].join('\u241f');
}

function ivV2EnsureIdempotencyKey() {
  const fingerprint = ivV2UploadFingerprint();
  if (!fingerprint) return '';
  if (fingerprint !== ivV2State.idempotencyFingerprint) {
    ivV2State.idempotencyFingerprint = fingerprint;
    ivV2State.idempotencyKey = window.crypto?.randomUUID
      ? window.crypto.randomUUID()
      : `iv2-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
  return ivV2State.idempotencyKey;
}

function ivV2SetStep(step) {
  if (!Number.isFinite(step)) step = ivV2State.currentStep;
  ivV2State.currentStep = step;
  if (!ivV2IsActive()) return;
  [1, 2, 3].forEach(index => {
    ivV2$(`iv-panel-${index}`)?.classList.toggle('panel--hidden', index !== step);
  });
  document.querySelector('.main')?.scrollTo({ top: 0, behavior: 'smooth' });
  ivV2RenderStepBars();
}

function ivV2PreviewStep(step) {
  if (ivV2OperationBusy() || ivV2PrecheckActive()) return;
  if (step > ivV2State.currentStep) return;
  ivV2SetStep(step);
}

function ivV2RenderStepBars() {
  const locked = ivV2OperationBusy() || ivV2PrecheckActive();
  document.querySelectorAll('[data-iv-v2-step]').forEach(button => {
    const step = Number(button.dataset.ivV2Step);
    button.classList.remove('step-bar__item--active', 'step-bar__item--done');
    if (step === ivV2State.currentStep) button.classList.add('step-bar__item--active');
    else if (step < ivV2State.currentStep) button.classList.add('step-bar__item--done');
    button.disabled = locked || step > ivV2State.currentStep;
  });
}

function ivV2SyncTrackToggle() {
  const track = window.ivState?.track || 'v1';
  const v1Busy = Boolean(
    window.ivState?.uploading
    || window.ivState?.running
    || window.ivState?.restoreBusy
    || window.ivState?.eventSource
    || window.ivState?.reviewBatchRunning
    || window.ivState?.reviewConfirming?.size
  );
  const locked = ivV2OperationBusy() || (track === 'v2' ? ivV2PrecheckActive() : v1Busy);
  ivV2AllTrackButtons().forEach(button => {
    const active = button.dataset.ivTrack === track;
    button.classList.toggle('iv-track-switch__btn--active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
    button.disabled = locked;
  });
  document.querySelectorAll('[data-iv-track-content]').forEach(section => {
    section.hidden = section.dataset.ivTrackContent !== track;
  });
}

function ivV2SetTrack(track) {
  if (!window.ivState) return;
  const currentTrack = window.ivState.track || 'v1';
  if (track === currentTrack) return;
  if (ivV2OperationBusy()) {
    showToast('当前操作完成前不能切换访谈版本', 'info');
    return;
  }
  if (currentTrack === 'v1' && (
    window.ivState.uploading
    || window.ivState.running
    || window.ivState.restoreBusy
    || window.ivState.eventSource
    || window.ivState.reviewBatchRunning
    || window.ivState.reviewConfirming?.size
  )) {
    showToast('旧版访谈仍有进行中的上传、生成或审阅操作，请等待完成', 'info');
    return;
  }
  if (currentTrack === 'v2' && ivV2PrecheckActive()) {
    showToast('工作簿预检完成前不能切换访谈版本', 'info');
    return;
  }
  window.ivState.track = track;
  ivV2SyncTrackToggle();
  if (track === 'v2') ivV2SetStep(ivV2State.currentStep);
  else ivGoStep(window.ivState.currentStep || 1);
}

function ivV2SyncUploadButton() {
  const button = ivV2$('iv-v2-start-import');
  if (!button) return;
  const checked = Boolean(ivV2$('iv-v2-contract-check')?.checked);
  button.disabled = !ivV2State.selectedFile || !checked || ivV2OperationBusy() || ivV2PrecheckActive();
}

function ivV2SelectFile(file) {
  if (!file || !/\.xlsx$/i.test(file.name || '')) {
    showToast('仅支持 .xlsx 文件', 'error');
    return;
  }
  if (file.size > 50 * 1024 * 1024) {
    showToast('文件不能超过 50MB', 'error');
    return;
  }
  ivV2State.selectedFile = file;
  ivV2State.idempotencyKey = '';
  ivV2State.idempotencyFingerprint = '';
  ivV2ClearStatusError();
  ivV2$('iv-v2-selected-file-name').textContent = file.name;
  ivV2$('iv-v2-selected-file-size').textContent = `${ivV2FormatSize(file.size)} · 可重新选择`;
  ivV2$('iv-v2-upload-empty').hidden = true;
  ivV2$('iv-v2-selected-file').hidden = false;
  ivV2$('iv-v2-upload-zone').classList.add('upload-zone--filled');
  ivV2SyncUploadButton();
}

function ivV2ClearFile() {
  ivV2State.selectedFile = null;
  ivV2State.idempotencyKey = '';
  ivV2State.idempotencyFingerprint = '';
  ivV2$('iv-v2-file-input').value = '';
  ivV2$('iv-v2-upload-empty').hidden = false;
  ivV2$('iv-v2-selected-file').hidden = true;
  ivV2$('iv-v2-upload-zone').classList.remove('upload-zone--filled', 'drag-over');
  ivV2SyncUploadButton();
}

function ivV2UpsertSheetCatalog(sourceSheets) {
  if (!Array.isArray(sourceSheets)) return;
  sourceSheets.forEach(sheet => {
    if (!sheet?.sheet_id) return;
    const sheetId = String(sheet.sheet_id);
    const existing = ivV2State.sheetCatalog[sheetId] || {
      sheet_id: sheetId,
      index: Number(sheet.index || 0),
      name: String(sheet.name || sheetId),
      candidate_columns: [],
    };
    const incomingColumns = Array.isArray(sheet.candidate_columns) ? sheet.candidate_columns : [];
    ivV2State.sheetCatalog[sheetId] = {
      ...existing,
      index: Number(sheet.index ?? existing.index ?? 0),
      name: String(sheet.name || existing.name || sheetId),
      candidate_columns: incomingColumns.length ? incomingColumns : existing.candidate_columns,
    };
  });
}

function ivV2RefreshSheetCatalog(response) {
  ivV2UpsertSheetCatalog(ivV2State.importData?.summary?.sheets || []);
  const proposal = response?.proposals || {};
  ivV2UpsertSheetCatalog((proposal.groups || []).flatMap(group => group.sheets || []));
  ivV2UpsertSheetCatalog(proposal.unassigned_sheets || []);
  ivV2UpsertSheetCatalog((response?.mapping?.groups || []).flatMap(group => group.sheets || []));
}

function ivV2MetaBySheet() {
  return new Map(Object.entries(ivV2State.sheetCatalog));
}

function ivV2BuildDraft(response) {
  const proposal = response?.proposals || {};
  const source = Number(response?.revision_number || 0) > 0 && Array.isArray(response?.mapping?.groups)
    ? response.mapping
    : proposal;
  return {
    groups: (source.groups || []).map((group, index) => ({
      temp_id: `group_${index + 1}_${Math.random().toString(16).slice(2, 8)}`,
      server_group_id: String(group.group_id || ''),
      display_name: String(group.display_name || `分组 ${index + 1}`),
      sheets: (group.sheets || []).map(sheet => ({
        sheet_id: String(sheet.sheet_id || ''),
        role: String(sheet.role || 'guide_reference'),
        recorder_label: String(sheet.recorder_label || ''),
      })).filter(sheet => sheet.sheet_id),
      participants: (group.participants || group.participant_bindings || []).map((participant, pIndex) => ({
        temp_id: `participant_${index + 1}_${pIndex + 1}_${Math.random().toString(16).slice(2, 8)}`,
        server_participant_id: String(participant.participant_id || ''),
        participant_label: String(participant.participant_label || ''),
        columns: (participant.columns || []).map(column => ({
          sheet_id: String(column.sheet_id || ''),
          column: Number(column.column_index || column.column || 0),
        })).filter(column => column.sheet_id && Number.isInteger(column.column) && column.column > 0),
      })).filter(participant => participant.participant_label || participant.columns.length),
    })).filter(group => group.sheets.length),
    ignoredSheetIds: Array.isArray(source.ignored_sheet_ids)
      ? source.ignored_sheet_ids.map(String)
      : [],
  };
}

function ivV2AvailableColumns(group, participant = null) {
  const sheetMeta = ivV2MetaBySheet();
  const recordSheetIds = new Set((group?.sheets || []).filter(sheet => sheet.role === 'record').map(sheet => sheet.sheet_id));
  const participantSheetIds = new Set((participant?.columns || []).map(column => column.sheet_id));
  const used = new Set();
  (group?.participants || []).forEach(groupParticipant => {
    groupParticipant.columns.forEach(column => used.add(`${column.sheet_id}:${column.column}`));
  });
  return Array.from(recordSheetIds).flatMap(sheetId => {
    const meta = sheetMeta.get(sheetId);
    return (meta?.candidate_columns || []).map(column => ({
      sheet_id: sheetId,
      column: Number(column.column_index || column.column || 0),
      column_letter: String(column.column_letter || ''),
      raw_header: String(column.raw_header || ''),
      sheet_name: meta?.name || sheetId,
    })).filter(column => (
      !used.has(`${sheetId}:${column.column}`)
      && !participantSheetIds.has(sheetId)
    ));
  }).sort((left, right) => (
    left.sheet_name.localeCompare(right.sheet_name, 'zh-CN') || left.column - right.column
  ));
}

function ivV2PruneGroup(group) {
  const validRecordSheets = new Set(group.sheets.filter(sheet => sheet.role === 'record').map(sheet => sheet.sheet_id));
  group.participants.forEach(participant => {
    participant.columns = participant.columns.filter(column => validRecordSheets.has(column.sheet_id));
  });
  group.participants = group.participants.filter(participant => participant.columns.length > 0);
}

function ivV2NormalizeDraft() {
  if (!ivV2State.draft) return;
  ivV2State.draft.groups = ivV2State.draft.groups.filter(group => group.sheets.length > 0);
  ivV2State.draft.groups.forEach((group, index) => {
    if (!String(group.display_name || '').trim()) group.display_name = `分组 ${index + 1}`;
    ivV2PruneGroup(group);
  });
}

function ivV2CreateGroup(name = '') {
  if (!ivV2State.draft) return null;
  const group = {
    temp_id: `group_new_${Math.random().toString(16).slice(2, 8)}`,
    server_group_id: '',
    display_name: name || `新分组 ${ivV2State.draft.groups.length + 1}`,
    sheets: [],
    participants: [],
  };
  ivV2State.draft.groups.push(group);
  return group;
}

function ivV2MoveSheet(sheetId, targetGroupId) {
  const draft = ivV2State.draft;
  if (!draft) return;
  const currentGroup = draft.groups.find(group => group.sheets.some(sheet => sheet.sheet_id === sheetId));
  if (currentGroup?.temp_id === targetGroupId) return;

  let movingSheet = null;
  draft.groups.forEach(group => {
    const nextSheets = [];
    group.sheets.forEach(sheet => {
      if (sheet.sheet_id === sheetId) movingSheet = { ...sheet };
      else nextSheets.push(sheet);
    });
    group.sheets = nextSheets;
    ivV2PruneGroup(group);
  });
  draft.ignoredSheetIds = draft.ignoredSheetIds.filter(id => id !== sheetId);

  if (!movingSheet) {
    movingSheet = {
      sheet_id: sheetId,
      role: (ivV2MetaBySheet().get(sheetId)?.candidate_columns || []).length ? 'record' : 'guide_reference',
      recorder_label: '',
    };
  }

  if (targetGroupId === '__ignored__') {
    draft.ignoredSheetIds.push(sheetId);
    ivV2NormalizeDraft();
    ivV2MarkDirty();
    return;
  }

  let targetGroup = draft.groups.find(group => group.temp_id === targetGroupId);
  if (!targetGroup && targetGroupId === '__new__') targetGroup = ivV2CreateGroup();
  if (!targetGroup) targetGroup = ivV2CreateGroup();
  if (!targetGroup) return;
  targetGroup.sheets.push(movingSheet);
  ivV2NormalizeDraft();
  ivV2MarkDirty();
}

function ivV2AllAssignedSheetIds() {
  const assigned = new Set();
  (ivV2State.draft?.groups || []).forEach(group => {
    group.sheets.forEach(sheet => assigned.add(sheet.sheet_id));
  });
  return assigned;
}

function ivV2SheetIdentity(group) {
  const recordSheetIds = group.sheets.filter(sheet => sheet.role === 'record').map(sheet => sheet.sheet_id).sort();
  const identitySheetIds = (recordSheetIds.length ? recordSheetIds : group.sheets.map(sheet => sheet.sheet_id).sort());
  return {
    identityRole: recordSheetIds.length ? 'record' : 'reference',
    identityKey: `${recordSheetIds.length ? 'record' : 'reference'}::${identitySheetIds.join('|')}`,
  };
}

function ivV2BaseCatalog() {
  const groups = new Map();
  const sheetOwner = new Map();
  (ivV2State.mappingResponse?.mapping?.groups || []).forEach(group => {
    const groupId = String(group.group_id || '');
    if (!groupId) return;
    const sheets = (group.sheets || []).map(sheet => ({
      sheet_id: String(sheet.sheet_id || ''),
      role: String(sheet.role || ''),
    })).filter(sheet => sheet.sheet_id);
    const recordSheetIds = sheets.filter(sheet => sheet.role === 'record').map(sheet => sheet.sheet_id);
    const identitySheetIds = recordSheetIds.length ? recordSheetIds : sheets.map(sheet => sheet.sheet_id);
    const participantIds = new Set(
      (group.participants || []).map(participant => String(participant.participant_id || '')).filter(Boolean)
    );
    groups.set(groupId, {
      identityRole: recordSheetIds.length ? 'record' : 'reference',
      participantIds,
    });
    identitySheetIds.forEach(sheetId => sheetOwner.set(sheetId, groupId));
  });
  return { groups, sheetOwner };
}

function ivV2InheritancePlan() {
  const baseRevision = Number(ivV2State.mappingResponse?.revision_number || 0);
  if (baseRevision <= 0) {
    return { groupIds: new Map(), participantIds: new Map() };
  }

  const baseCatalog = ivV2BaseCatalog();
  const nextGroups = ivV2State.draft?.groups || [];
  const ancestryByGroup = new Map();
  const consumersByBaseGroup = new Map();
  nextGroups.forEach(group => {
    const recordSheets = group.sheets.filter(sheet => sheet.role === 'record');
    const identitySheets = recordSheets.length ? recordSheets : group.sheets;
    const ancestry = new Set();
    identitySheets.forEach(sheet => {
      let owner = baseCatalog.sheetOwner.get(sheet.sheet_id) || '';
      if (
        owner
        && !recordSheets.length
        && baseCatalog.groups.get(owner)?.identityRole !== 'reference'
      ) {
        owner = '';
      }
      if (!owner) return;
      ancestry.add(owner);
      if (!consumersByBaseGroup.has(owner)) consumersByBaseGroup.set(owner, new Set());
      consumersByBaseGroup.get(owner).add(group.temp_id);
    });
    ancestryByGroup.set(group.temp_id, ancestry);
  });

  const groupIds = new Map();
  const participantIds = new Map();
  nextGroups.forEach(group => {
    const baseGroupId = String(group.server_group_id || '');
    const baseGroup = baseCatalog.groups.get(baseGroupId);
    if (!baseGroup) return;
    const currentIdentity = ivV2SheetIdentity(group);
    const ancestry = ancestryByGroup.get(group.temp_id) || new Set();
    const consumers = consumersByBaseGroup.get(baseGroupId) || new Set();
    if (
      currentIdentity.identityRole !== baseGroup.identityRole
      || ancestry.size !== 1
      || !ancestry.has(baseGroupId)
      || consumers.size !== 1
      || !consumers.has(group.temp_id)
    ) return;
    groupIds.set(group.temp_id, baseGroupId);
    group.participants.forEach(participant => {
      const participantId = String(participant.server_participant_id || '');
      if (!baseGroup.participantIds.has(participantId)) return;
      if (participantId) participantIds.set(participant.temp_id, participantId);
    });
  });

  return { groupIds, participantIds };
}

function ivV2Payload() {
  ivV2NormalizeDraft();
  const baseRevision = Number(ivV2State.mappingResponse?.revision_number || 0);
  const inheritance = ivV2InheritancePlan();
  return {
    base_mapping_revision: baseRevision,
    groups: (ivV2State.draft?.groups || []).map(group => ({
      group_id: baseRevision > 0 ? (inheritance.groupIds.get(group.temp_id) || null) : null,
      display_name: String(group.display_name || '').trim(),
      sheets: group.sheets.map(sheet => ({
        sheet_id: sheet.sheet_id,
        role: sheet.role,
        recorder_label: sheet.role === 'record' ? String(sheet.recorder_label || '').trim() : '',
      })),
      participant_bindings: group.participants.map(participant => ({
        participant_id: baseRevision > 0 ? (inheritance.participantIds.get(participant.temp_id) || null) : null,
        participant_label: String(participant.participant_label || '').trim(),
        columns: participant.columns.map(column => ({
          sheet_id: column.sheet_id,
          column: Number(column.column),
        })),
      })),
    })),
    ignored_sheet_ids: Array.from(new Set(ivV2State.draft?.ignoredSheetIds || [])),
    change_kind: 'manual_edit',
    change_reason: '前端分组编辑保存',
  };
}

async function ivV2LoadImportBundle(importId, { keepStep = false, token = ivV2NextToken() } = {}) {
  ivV2State.requestBusy = true;
  ivV2State.status = 'loading';
  ivV2State.loadingMessage = '正在读取预检结果与分组建议';
  ivV2ResetStructureWorkspace();
  ivV2RenderEditor();
  try {
    const [importResp, proposalResp] = await Promise.all([
      fetch(`/api/v1/interview-imports/${importId}`),
      fetch(`/api/v1/interview-imports/${importId}/group-proposals`),
    ]);
    const importData = await importResp.json();
    const proposalData = await proposalResp.json();
    if (!ivV2IsTokenCurrent(token)) return;
    if (!importResp.ok) throw new Error(ivV2NormalizeApiError(importData, importResp.status, '读取导入状态失败').message);
    if (!proposalResp.ok) throw new Error(ivV2NormalizeApiError(proposalData, proposalResp.status, '读取分组建议失败').message);

    ivV2State.importId = importData.import_id;
    ivV2State.projectId = importData.project_id;
    ivV2State.workbookRevisionId = importData.workbook_revision_id;
    ivV2State.importData = importData;
    ivV2State.mappingResponse = proposalData;
    ivV2RefreshSheetCatalog(proposalData);
    ivV2State.draft = ivV2BuildDraft(proposalData);
    ivV2State.status = importData.status || proposalData.status || 'loaded';
    ivV2State.loadingMessage = '';
    ivV2ClearStatusError();
    const confirmed = ivV2HasConfirmedMapping(proposalData.status) || ivV2HasConfirmedMapping(importData.status);
    ivV2ClearDirty(confirmed ? '当前处于已确认版本' : '已加载最新映射版本');
    if (!keepStep) ivV2SetStep(confirmed ? 3 : 2);
    ivV2RenderEditor();
    ivV2RenderConfirmed();
    if (confirmed && (ivV2HasStructureCheckpoint(importData.status) || ivV2HasStructureCheckpoint(proposalData.status))) {
      await ivV2LoadStructureWorkspace({ token, silentConflictRefresh: true });
    }
  } catch (error) {
    if (!ivV2IsTokenCurrent(token)) return;
    ivV2State.status = 'error';
    ivV2State.loadingMessage = '';
    ivV2State.errorMessage = String(error?.message || '读取映射状态失败');
    throw error;
  } finally {
    if (ivV2IsTokenCurrent(token)) {
      ivV2State.requestBusy = false;
      ivV2RenderEditor();
      ivV2RenderConfirmed();
    }
  }
}

function ivV2SchedulePoll(token) {
  ivV2ResetPoll();
  ivV2State.pendingPoll = window.setTimeout(() => {
    if (!ivV2IsTokenCurrent(token)) return;
    ivV2PollAttempt(token).catch(error => {
      if (!ivV2IsTokenCurrent(token)) return;
      ivV2State.requestBusy = false;
      ivV2State.status = 'error';
      ivV2State.loadingMessage = '';
      ivV2State.errorMessage = String(error?.message || '预检轮询失败');
      ivV2RenderUploadStatus();
      ivV2SetStep(ivV2State.importId ? 2 : 1);
      ivV2SyncUploadButton();
      ivV2RenderEditor();
    });
  }, IV_V2_POLL_INTERVAL_MS);
}

async function ivV2PollAttempt(token = ivV2State.requestToken) {
  if (!ivV2State.uploadAttemptId) return;
  const response = await fetch(`/api/v1/interview-upload-attempts/${ivV2State.uploadAttemptId}`);
  const data = await response.json();
  if (!ivV2IsTokenCurrent(token)) return;
  if (!response.ok) {
    const normalized = ivV2NormalizeApiError(data, response.status, '读取上传状态失败');
    ivV2SetStatusError(data, response.status, '读取上传状态失败');
    throw new Error(normalized.message);
  }

  ivV2State.status = data.status || 'PRECHECKING';
  ivV2State.loadingMessage = data.status === 'PRECHECKING'
    ? '正在预检工作簿结构，请稍候'
    : '正在准备映射编辑器';

  if (data.status === 'ACCEPTED' && data.import_id) {
    ivV2State.importId = String(data.import_id || '');
    ivV2State.projectId = String(data.project_id || '');
    ivV2State.workbookRevisionId = String(data.workbook_revision_id || '');
    await ivV2LoadImportBundle(data.import_id, { token });
    if (ivV2IsTokenCurrent(token)) showToast('预检完成，请确认分组与玩家绑定', 'success');
    return;
  }

  if (data.status === 'REJECTED') {
    if (!ivV2IsTokenCurrent(token)) return;
    ivV2State.requestBusy = false;
    ivV2State.status = 'REJECTED';
    ivV2SetStatusError(data, response.status, '工作簿预检未通过');
    ivV2SetStep(1);
    ivV2RenderEditor();
    showToast(ivV2State.errorMessage, 'error', 7000);
    return;
  }

  ivV2RenderEditor();
  ivV2SchedulePoll(token);
}

async function ivV2StartImport() {
  if (!ivV2State.selectedFile || ivV2OperationBusy() || ivV2PrecheckActive()) return;
  if (!ivV2$('iv-v2-contract-check')?.checked) {
    showToast('请先确认文件要求', 'error');
    return;
  }
  if (ivV2FocusValue().length > 4000) {
    showToast('本次研究重点不能超过 4000 字', 'error');
    return;
  }

  ivV2InvalidateAsync();
  const token = ivV2State.requestToken;
  ivV2State.uploadAttemptId = '';
  ivV2State.importId = '';
  ivV2State.projectId = '';
  ivV2State.workbookRevisionId = '';
  ivV2State.importData = null;
  ivV2State.mappingResponse = null;
  ivV2State.draft = null;
  ivV2State.sheetCatalog = {};
  ivV2State.statusNote = '';
  ivV2ClearDirty();
  ivV2State.requestBusy = true;
  ivV2State.status = 'uploading';
  ivV2State.loadingMessage = `正在上传 ${ivV2State.selectedFile.name}`;
  ivV2ClearStatusError();
  ivV2SyncUploadButton();
  ivV2SetStep(2);
  ivV2RenderEditor();

  const body = new FormData();
  body.append('file', ivV2State.selectedFile);
  body.append('research_focus', ivV2FocusValue());
  body.append('file_contract_version', IV_V2_FILE_CONTRACT_VERSION);
  body.append('contract_acknowledged', 'true');

  try {
    const response = await fetch('/api/v1/interview-upload-attempts', {
      method: 'POST',
      headers: { 'Idempotency-Key': ivV2EnsureIdempotencyKey() },
      body,
    });
    const data = await response.json();
    if (!ivV2IsTokenCurrent(token)) return;

    if (!response.ok) {
      const errorInfo = ivV2NormalizeApiError(data, response.status, '上传失败');
      ivV2SetStatusError(data, response.status, '上传失败');
      const expectedVersion = data?.error?.context?.expected_version;
      if (expectedVersion && expectedVersion !== IV_V2_FILE_CONTRACT_VERSION) {
        ivV2State.errorMessage = `文件要求版本已更新，请刷新页面后重试（当前应为 ${expectedVersion}）`;
        ivV2RenderUploadStatus();
        throw new Error(ivV2State.errorMessage);
      }
      throw new Error(errorInfo.message);
    }

    ivV2State.uploadAttemptId = data.upload_attempt_id;
    ivV2State.status = data.status || 'QUARANTINED';
    ivV2State.loadingMessage = data.status === 'ACCEPTED'
      ? '预检完成，正在打开映射编辑器'
      : '已接收文件，正在执行预检';

    if (data.status === 'REJECTED') {
      ivV2State.requestBusy = false;
      ivV2SetStatusError(data, response.status, '工作簿预检未通过');
      ivV2SetStep(1);
      ivV2RenderEditor();
      showToast(ivV2State.errorMessage, 'error', 7000);
      return;
    }

    if (data.status === 'ACCEPTED' && data.import_id) {
      ivV2State.importId = String(data.import_id || '');
      ivV2State.projectId = String(data.project_id || '');
      ivV2State.workbookRevisionId = String(data.workbook_revision_id || '');
      await ivV2LoadImportBundle(data.import_id, { token });
      if (ivV2IsTokenCurrent(token)) showToast('预检完成，请确认分组与玩家绑定', 'success');
      return;
    }

    ivV2State.requestBusy = false;
    ivV2RenderEditor();
    ivV2SchedulePoll(token);
  } catch (error) {
    if (!ivV2IsTokenCurrent(token)) return;
    ivV2State.requestBusy = false;
    ivV2State.status = 'error';
    if (!ivV2State.errorMessage) ivV2State.errorMessage = String(error?.message || '上传失败');
    ivV2RenderUploadStatus();
    ivV2SetStep(ivV2State.importId ? 2 : 1);
    ivV2RenderEditor();
    ivV2ShowToastFromError(error, '上传失败');
  } finally {
    if (ivV2IsTokenCurrent(token)) ivV2SyncUploadButton();
  }
}

function ivV2SheetName(sheetId) {
  return ivV2MetaBySheet().get(sheetId)?.name || sheetId;
}

function ivV2ColumnLabel(column) {
  const meta = ivV2MetaBySheet().get(column.sheet_id);
  const detail = (meta?.candidate_columns || []).find(item => Number(item.column_index || item.column || 0) === Number(column.column));
  const columnLabel = detail?.column_letter || `C${column.column}`;
  const header = String(detail?.raw_header || '');
  return `${meta?.name || column.sheet_id} · ${columnLabel}${header ? ` · ${header}` : ''}`;
}

function ivV2EditorSummaryHtml() {
  const importData = ivV2State.importData;
  if (!importData) {
    return `
      <div class="iv-v2-status-card iv-v2-status-card--${ivV2StatusTone(ivV2State.status)}">
        <strong>${ivV2Esc(ivV2State.loadingMessage || '正在准备上传状态')}</strong>
        <p>${ivV2Esc(ivV2State.errorMessage || '文件预检完成后，这里会显示工作簿摘要与风险提示。')}</p>
      </div>
    `;
  }
  const summary = importData.summary || {};
  return `
    <div class="iv-v2-status-grid">
      <div class="iv-v2-status-card iv-v2-status-card--${ivV2StatusTone(importData.status)}">
        <strong>${ivV2Esc(importData.status || '--')}</strong>
        <p>导入 ID：${ivV2Esc(importData.import_id || '--')}</p>
      </div>
      <div class="iv-v2-status-card">
        <strong>${ivV2Esc(summary.sheet_count ?? '--')} 个 Sheet</strong>
        <p>非空单元格 ${ivV2Esc(summary.non_empty_cell_count ?? '--')} · 文本 ${ivV2Esc(summary.text_char_count ?? '--')} 字</p>
      </div>
      <div class="iv-v2-status-card">
        <strong>${ivV2Esc(ivV2FormatTime(importData.updated_at))}</strong>
        <p>最近更新 · 工作簿版本 ${ivV2Esc(importData.workbook_revision_id || '--')}</p>
      </div>
    </div>
  `;
}

function ivV2IssuesSource() {
  const response = ivV2State.mappingResponse;
  if (!response) return [];
  if (Array.isArray(response.issues) && response.issues.length) return response.issues;
  return Array.isArray(response.proposals?.issues) ? response.proposals.issues : [];
}

function ivV2IssuesHtml() {
  const issues = ivV2IssuesSource();
  if (!issues.length) return '<div class="iv-v2-empty">当前没有待处理提示。</div>';
  return issues.map(issue => `
    <div class="iv-v2-issue">
      <strong>${ivV2Esc(issue.code || '提示')}</strong>
      <p>${ivV2Esc(issue.message || '')}</p>
      ${issue.suggested_action ? `<span>${ivV2Esc(issue.suggested_action)}</span>` : ''}
    </div>
  `).join('');
}

function ivV2HistoryHtml() {
  const history = ivV2State.mappingResponse?.history || [];
  if (!history.length) return '<div class="iv-v2-empty">还没有保存过映射版本。</div>';
  const currentRevision = Number(ivV2State.mappingResponse?.revision_number || 0);
  return history.map(entry => `
    <div class="iv-v2-history-item${entry.revision_number === currentRevision ? ' iv-v2-history-item--current' : ''}">
      <div>
        <strong>第 ${ivV2Esc(entry.revision_number)} 版</strong>
        <p>${ivV2Esc(entry.change_kind || 'manual_edit')} · ${ivV2Esc(ivV2FormatTime(entry.created_at))}</p>
      </div>
      <div class="iv-v2-history-item__actions">
        ${entry.confirmed ? '<span class="iv-v2-badge iv-v2-badge--success">已确认</span>' : ''}
        ${entry.revision_number === currentRevision
          ? '<span class="iv-v2-badge">当前版本</span>'
          : `<button class="btn btn--ghost btn--sm" type="button" data-iv-v2-action="restore-history" data-history-revision="${ivV2Esc(entry.revision_number)}"${ivV2State.draftDirty || ivV2OperationBusy() ? ' disabled' : ''}>恢复此版</button>`}
      </div>
    </div>
  `).join('');
}

function ivV2GroupOptions(selected) {
  return (ivV2State.draft?.groups || []).map(group => `
    <option value="${ivV2Esc(group.temp_id)}"${group.temp_id === selected ? ' selected' : ''}>${ivV2Esc(group.display_name)}</option>
  `).join('');
}

function ivV2RenderGroupsHtml() {
  const draft = ivV2State.draft;
  if (!draft?.groups?.length) {
    return '<div class="iv-v2-empty">还没有分组。可先新建分组，再把 Sheet 分配进去。</div>';
  }

  return draft.groups.map((group, groupIndex) => {
    const availableColumns = ivV2AvailableColumns(group);
    return `
      <section class="iv-v2-group-card">
        <div class="iv-v2-group-card__head">
          <div>
            <span class="iv-v2-card-label">分组名称</span>
            <input class="iv-v2-group-title" type="text" maxlength="200" value="${ivV2Esc(group.display_name)}" data-iv-v2-action="group-name" data-group-id="${ivV2Esc(group.temp_id)}" />
          </div>
          <button class="btn btn--ghost btn--sm" type="button" data-iv-v2-action="remove-group" data-group-id="${ivV2Esc(group.temp_id)}">移除分组</button>
        </div>
        <div class="iv-v2-subsection">
          <div class="iv-v2-subsection__title">Sheet 归属</div>
          ${(group.sheets || []).map(sheet => `
            <div class="iv-v2-sheet-row">
              <div class="iv-v2-sheet-row__meta">
                <strong>${ivV2Esc(ivV2SheetName(sheet.sheet_id))}</strong>
                <span>${ivV2Esc(sheet.sheet_id)}</span>
              </div>
              <div class="iv-v2-sheet-row__controls">
                <label>
                  <span>去向</span>
                  <select data-iv-v2-action="move-sheet" data-sheet-id="${ivV2Esc(sheet.sheet_id)}">
                    ${ivV2GroupOptions(group.temp_id)}
                    <option value="__new__">新建分组</option>
                    <option value="__ignored__">忽略此 Sheet</option>
                  </select>
                </label>
                <label>
                  <span>角色</span>
                  <select data-iv-v2-action="sheet-role" data-group-id="${ivV2Esc(group.temp_id)}" data-sheet-id="${ivV2Esc(sheet.sheet_id)}">
                    <option value="record"${sheet.role === 'record' ? ' selected' : ''}>记录页</option>
                    <option value="guide_reference"${sheet.role === 'guide_reference' ? ' selected' : ''}>提纲参考</option>
                    <option value="attribute_reference"${sheet.role === 'attribute_reference' ? ' selected' : ''}>属性参考</option>
                  </select>
                </label>
                <label class="${sheet.role === 'record' ? '' : 'is-disabled'}">
                  <span>记录员</span>
                  <input type="text" maxlength="200" value="${ivV2Esc(sheet.recorder_label)}" ${sheet.role === 'record' ? '' : 'disabled'} data-iv-v2-action="sheet-recorder" data-group-id="${ivV2Esc(group.temp_id)}" data-sheet-id="${ivV2Esc(sheet.sheet_id)}" placeholder="如：记录员 A" />
                </label>
              </div>
            </div>
          `).join('')}
        </div>
        <div class="iv-v2-subsection">
          <div class="iv-v2-subsection__title">玩家绑定</div>
          ${(group.participants || []).length ? group.participants.map(participant => {
            const participantAvailable = ivV2AvailableColumns(group, participant);
            return `
              <div class="iv-v2-participant-card">
                <div class="iv-v2-participant-card__head">
                  <input type="text" maxlength="200" value="${ivV2Esc(participant.participant_label)}" data-iv-v2-action="participant-label" data-group-id="${ivV2Esc(group.temp_id)}" data-participant-id="${ivV2Esc(participant.temp_id)}" placeholder="玩家标识" />
                  <button class="btn btn--ghost btn--sm" type="button" data-iv-v2-action="remove-participant" data-group-id="${ivV2Esc(group.temp_id)}" data-participant-id="${ivV2Esc(participant.temp_id)}">移除玩家</button>
                </div>
                <div class="iv-v2-pill-list">
                  ${participant.columns.map(column => `
                    <button class="iv-v2-pill" type="button" data-iv-v2-action="remove-column" data-group-id="${ivV2Esc(group.temp_id)}" data-participant-id="${ivV2Esc(participant.temp_id)}" data-sheet-id="${ivV2Esc(column.sheet_id)}" data-column="${ivV2Esc(column.column)}">
                      ${ivV2Esc(ivV2ColumnLabel(column))} ×
                    </button>
                  `).join('')}
                </div>
                <div class="iv-v2-inline-actions">
                  <select data-iv-v2-action="participant-add-column-select" data-group-id="${ivV2Esc(group.temp_id)}" data-participant-id="${ivV2Esc(participant.temp_id)}">
                    <option value="">追加记录列...</option>
                    ${participantAvailable.map(column => `<option value="${ivV2Esc(`${column.sheet_id}:${column.column}`)}">${ivV2Esc(ivV2ColumnLabel(column))}</option>`).join('')}
                  </select>
                </div>
              </div>
            `;
          }).join('') : '<div class="iv-v2-empty">当前分组还没有玩家绑定。</div>'}
          <div class="iv-v2-subsection__foot">
            <button class="btn btn--ghost btn--sm" type="button" data-iv-v2-action="add-participant" data-group-id="${ivV2Esc(group.temp_id)}" ${availableColumns.length ? '' : 'disabled'}>按空闲列新增玩家</button>
            <span>${availableColumns.length ? `剩余 ${availableColumns.length} 个待绑定列` : '没有剩余记录列'}</span>
          </div>
        </div>
      </section>
    `;
  }).join('');
}

function ivV2RenderLooseSheetsHtml() {
  const sheetMeta = Array.from(ivV2MetaBySheet().values()).sort((left, right) => left.index - right.index || left.name.localeCompare(right.name, 'zh-CN'));
  const assigned = ivV2AllAssignedSheetIds();
  const ignored = new Set(ivV2State.draft?.ignoredSheetIds || []);
  const loose = sheetMeta.filter(sheet => !assigned.has(sheet.sheet_id) && !ignored.has(sheet.sheet_id));
  const ignoredSheets = sheetMeta.filter(sheet => ignored.has(sheet.sheet_id));

  const renderRow = (sheet, ignoredMode = false) => `
    <div class="iv-v2-loose-sheet">
      <div>
        <strong>${ivV2Esc(sheet.name)}</strong>
        <p>${ivV2Esc(sheet.sheet_id)} · 候选列 ${(sheet.candidate_columns || []).length}</p>
      </div>
      <div class="iv-v2-inline-actions">
        <select data-iv-v2-action="move-sheet" data-sheet-id="${ivV2Esc(sheet.sheet_id)}">
          <option value="">分配到...</option>
          ${ivV2GroupOptions('')}
          <option value="__new__">新建分组</option>
          ${ignoredMode ? '' : '<option value="__ignored__">忽略此 Sheet</option>'}
        </select>
        ${ignoredMode ? `<button class="btn btn--ghost btn--sm" type="button" data-iv-v2-action="restore-ignored-sheet" data-sheet-id="${ivV2Esc(sheet.sheet_id)}">恢复</button>` : ''}
      </div>
    </div>
  `;

  return `
    <div class="iv-v2-side-card">
      <div class="iv-v2-side-card__title">待处理 Sheet</div>
      ${loose.length ? loose.map(sheet => renderRow(sheet)).join('') : '<div class="iv-v2-empty">所有 Sheet 都已归组或忽略。</div>'}
    </div>
    <div class="iv-v2-side-card">
      <div class="iv-v2-side-card__title">已忽略 Sheet</div>
      ${ignoredSheets.length ? ignoredSheets.map(sheet => renderRow(sheet, true)).join('') : '<div class="iv-v2-empty">当前没有忽略项。</div>'}
    </div>
  `;
}

function ivV2HistoryStacks() {
  const empty = { valid: false, undo: [], redo: [], byRevision: new Map() };
  const current = Number(ivV2State.mappingResponse?.revision_number || 0);
  const history = [...(ivV2State.mappingResponse?.history || [])]
    .sort((left, right) => Number(left.revision_number) - Number(right.revision_number));
  if (current === 0 && !history.length) return { ...empty, valid: true };
  if (!Number.isInteger(current) || current < 1 || history.length !== current) return empty;

  const undo = [];
  const redo = [];
  const byRevision = new Map();
  let previousRevision = 0;

  for (const entry of history) {
    const revision = Number(entry.revision_number);
    const changeKind = String(entry.change_kind || 'manual_edit');
    if (!Number.isInteger(revision) || revision !== previousRevision + 1 || revision > current) return empty;
    byRevision.set(revision, entry);

    if (revision === 1) {
      if (changeKind === 'undo' || changeKind === 'redo') return empty;
    } else if (changeKind === 'undo') {
      const expected = undo[undo.length - 1];
      const target = Number(entry.restored_from_revision_number || 0);
      if (!expected || target !== expected) return empty;
      undo.pop();
      redo.push(revision - 1);
    } else if (changeKind === 'redo') {
      const expected = redo[redo.length - 1];
      const target = Number(entry.restored_from_revision_number || 0);
      if (!expected || target !== expected) return empty;
      redo.pop();
      undo.push(revision - 1);
    } else if (changeKind === 'manual_edit' || changeKind === 'restore') {
      undo.push(revision - 1);
      redo.length = 0;
    } else {
      return empty;
    }
    previousRevision = revision;
  }

  if (previousRevision !== current) return empty;
  return { valid: true, undo, redo, byRevision };
}

function ivV2UndoTarget() {
  const stacks = ivV2HistoryStacks();
  if (!stacks.valid || !stacks.undo.length) return null;
  return stacks.byRevision.get(stacks.undo[stacks.undo.length - 1]) || null;
}

function ivV2RedoTarget() {
  const stacks = ivV2HistoryStacks();
  if (!stacks.valid || !stacks.redo.length) return null;
  return stacks.byRevision.get(stacks.redo[stacks.redo.length - 1]) || null;
}

function ivV2StatusBannerText() {
  if (ivV2State.errorMessage) {
    const suffix = [
      ivV2State.statusCode ? `代码：${ivV2State.statusCode}` : '',
      ivV2State.statusAction ? `建议动作：${ivV2State.statusAction}` : '',
      ivV2State.statusTraceId ? `Trace：${ivV2State.statusTraceId}` : '',
    ].filter(Boolean).join(' · ');
    return suffix ? `${ivV2State.errorMessage} · ${suffix}` : ivV2State.errorMessage;
  }
  if (ivV2State.draftDirty) return '当前草稿有未保存改动，需先保存后才能确认。';
  return ivV2State.loadingMessage || ivV2State.statusNote || '请先检查分组、Sheet 角色和玩家绑定。';
}

function ivV2RenderEditorStatus() {
  const status = ivV2$('iv-v2-editor-status');
  if (!status) return;
  const tone = ivV2State.errorMessage
    ? 'danger'
    : (ivV2State.draftDirty ? 'warning' : (ivV2State.status === 'GROUP_MAPPING_CONFIRMED' ? 'success' : 'info'));
  status.innerHTML = `
    <div class="iv-v2-status-banner iv-v2-status-banner--${tone}">
      <strong>${ivV2Esc(ivV2StatusBannerText())}</strong>
      <p>${ivV2Esc(ivV2State.errorMessage
        ? '修正后可重新上传或刷新状态。'
        : '保存草稿后会生成新映射版本；确认后停在 GROUP_MAPPING_CONFIRMED。')}</p>
    </div>
  `;
}

function ivV2SyncEditorControls() {
  const operationBusy = ivV2OperationBusy();
  const editorShell = ivV2$('iv-v2-editor-shell');
  const saveButton = ivV2$('iv-v2-save-draft');
  const confirmButton = ivV2$('iv-v2-confirm-mapping');
  const undoButton = ivV2$('iv-v2-undo');
  const redoButton = ivV2$('iv-v2-redo');
  const refreshButton = ivV2$('iv-v2-refresh');
  const addGroupButton = ivV2$('iv-v2-add-group');
  const revisionNumber = Number(ivV2State.mappingResponse?.revision_number || 0);
  const saveAllowed = revisionNumber === 0 || ivV2State.draftDirty;

  editorShell?.setAttribute('aria-busy', operationBusy ? 'true' : 'false');
  if (saveButton) {
    saveButton.disabled = !ivV2State.mappingResponse || !saveAllowed || operationBusy;
    saveButton.textContent = ivV2State.saveBusy ? '保存中...' : '保存草稿';
  }
  if (confirmButton) {
    confirmButton.disabled = !ivV2State.mappingResponse || !ivV2State.mappingResponse.confirmation_ready || revisionNumber < 1 || ivV2State.draftDirty || operationBusy;
    confirmButton.textContent = ivV2State.confirmBusy ? '确认中...' : '确认分组并停在检查点';
  }
  if (undoButton) undoButton.disabled = ivV2State.draftDirty || !ivV2UndoTarget() || operationBusy;
  if (redoButton) redoButton.disabled = ivV2State.draftDirty || !ivV2RedoTarget() || operationBusy;
  if (refreshButton) refreshButton.disabled = operationBusy || !ivV2State.importId;
  if (addGroupButton) addGroupButton.disabled = operationBusy || !ivV2State.draft;
  if (operationBusy) {
    editorShell?.querySelectorAll('input, select, textarea, button').forEach(control => {
      control.disabled = true;
    });
  }
}

function ivV2RenderEditor() {
  const meta = ivV2$('iv-v2-import-meta');
  const issues = ivV2$('iv-v2-issues');
  const groups = ivV2$('iv-v2-group-list');
  const loose = ivV2$('iv-v2-unassigned-sheets');
  const history = ivV2$('iv-v2-history-list');
  const status = ivV2$('iv-v2-editor-status');
  if (!meta || !issues || !groups || !loose || !history || !status) return;
  meta.innerHTML = ivV2EditorSummaryHtml();
  issues.innerHTML = ivV2IssuesHtml();
  history.innerHTML = ivV2HistoryHtml();

  if (!ivV2State.mappingResponse || !ivV2State.draft) {
    groups.innerHTML = `
      <div class="iv-v2-loading">
        <div class="spinner"></div>
        <p>${ivV2Esc(ivV2State.loadingMessage || '正在准备映射编辑器')}</p>
      </div>
    `;
    loose.innerHTML = '';
  } else {
    groups.innerHTML = ivV2RenderGroupsHtml();
    loose.innerHTML = ivV2RenderLooseSheetsHtml();
  }

  ivV2RenderEditorStatus();
  ivV2SyncEditorControls();
  ivV2RenderStepBars();
  ivV2SyncTrackToggle();
}

function ivV2EnsureIssueDraft(issue) {
  if (!issue?.issue_id) return null;
  if (!ivV2State.issueDrafts[issue.issue_id]) {
    const allowed = Array.isArray(issue.allowed_resolutions) ? issue.allowed_resolutions : [];
    const suggested = issue.suggested_resolution || {};
    ivV2State.issueDrafts[issue.issue_id] = {
      resolution: String(suggested.resolution || allowed[0] || ''),
      target_id: String(suggested.target_id || ''),
      row_role: String(suggested.row_role || ''),
      evidence_type: String(suggested.evidence_type || ''),
      comment: '',
    };
  }
  return ivV2State.issueDrafts[issue.issue_id];
}

function ivV2IssueById(issueId) {
  return ivV2CurrentReviewIssues().find(issue => issue.issue_id === issueId) || null;
}

function ivV2CurrentIssue() {
  return ivV2IssueById(ivV2State.selectedIssueId);
}

function ivV2SelectIssue(issueId) {
  const issue = ivV2IssueById(issueId);
  ivV2State.selectedIssueId = issue?.issue_id || '';
  if (issue) {
    ivV2EnsureIssueDraft(issue);
    ivV2EnsureEvidenceContext(issue);
  }
  ivV2RenderConfirmed();
}

function ivV2IssueSeverityTone(issue) {
  const severity = String(issue?.severity || '');
  if (severity === 'blocking') return 'danger';
  if (severity === 'recommended') return 'warning';
  return 'info';
}

function ivV2IssueStatusText(issue) {
  const status = String(issue?.status || 'open');
  if (status === 'resolved') return '已解决';
  if (status === 'dismissed') return '已忽略';
  return '待处理';
}

function ivV2VisibleReviewIssues() {
  const issues = ivV2CurrentReviewIssues();
  const filter = ivV2State.reviewFilter || 'open';
  if (filter === 'all') return issues;
  if (filter === 'resolved') {
    return issues.filter(issue => !ivV2IssueIsOpen(issue));
  }
  if (filter === 'blocking') {
    return issues.filter(issue => issue.status !== 'resolved' && issue.status !== 'dismissed' && issue.severity === 'blocking');
  }
  if (filter === 'recommended') {
    return issues.filter(issue => issue.status !== 'resolved' && issue.status !== 'dismissed' && issue.severity === 'recommended');
  }
  return issues.filter(issue => issue.status !== 'resolved' && issue.status !== 'dismissed');
}

function ivV2SyncSelectedIssue({ preserveAll = false } = {}) {
  const visible = ivV2VisibleReviewIssues();
  const currentVisible = visible.find(issue => issue.issue_id === ivV2State.selectedIssueId) || null;
  if (currentVisible) return currentVisible;
  if (preserveAll && ivV2State.reviewFilter === 'all') {
    const currentAny = ivV2IssueById(ivV2State.selectedIssueId);
    if (currentAny) return currentAny;
  }
  const nextIssue = visible[0] || null;
  ivV2State.selectedIssueId = nextIssue?.issue_id || '';
  return nextIssue;
}

function ivV2ReviewFilterOptions() {
  const summary = ivV2State.structureResponse?.review_summary || {};
  const open = Number(summary.open_issue_count ?? ivV2CurrentReviewIssues().length);
  const blocking = Number(summary.blocking_issue_count ?? 0);
  const recommended = Number(summary.recommended_issue_count ?? 0);
  const all = ivV2CurrentReviewIssues().length;
  const resolved = Math.max(0, all - open);
  return [
    { value: 'open', label: `待处理 ${open}` },
    { value: 'blocking', label: `阻塞 ${blocking}` },
    { value: 'recommended', label: `建议 ${recommended}` },
    { value: 'resolved', label: `已处理 ${resolved}` },
    { value: 'all', label: `全部 ${all}` },
  ];
}

function ivV2Modules() {
  return Array.isArray(ivV2CurrentStructurePayload()?.modules)
    ? ivV2CurrentStructurePayload().modules
    : [];
}

function ivV2MainQuestions() {
  return Array.isArray(ivV2CurrentStructurePayload()?.main_questions)
    ? ivV2CurrentStructurePayload().main_questions
    : [];
}

function ivV2Occurrences() {
  return Array.isArray(ivV2CurrentStructurePayload()?.occurrences)
    ? ivV2CurrentStructurePayload().occurrences
    : [];
}

function ivV2FormatResolutionLabel(action) {
  return {
    assign_row_role: '指定行角色',
    assign_module: '改绑模块',
    assign_main_question: '改绑主问题',
    set_evidence_identity: '确认身份与类型',
    exclude_evidence: '排除此证据',
    accept_suggestion: '接受系统建议',
  }[action] || action || '未命名动作';
}

function ivV2FormatRowRoleLabel(role) {
  return {
    module_header: '模块标题',
    main_question: '主问题',
    follow_up: '追问',
    observation_row: '观察记录',
  }[role] || role || '未指定';
}

function ivV2FormatEvidenceTypeLabel(type) {
  return {
    participant_self_report: '玩家自述',
    researcher_observation: '研究员观察',
  }[type] || type || '未指定';
}

function ivV2IssueEvidenceTargets(issue) {
  const ids = new Set();
  const suggested = String(issue?.suggested_resolution?.target_id || '');
  if (suggested.startsWith('ev_')) ids.add(suggested);
  (issue?.affected_ids?.evidence_ids || []).forEach(id => {
    if (String(id || '').startsWith('ev_')) ids.add(String(id));
  });
  return Array.from(ids);
}

function ivV2IssueHasResolvableAction(issue) {
  return Array.isArray(issue?.allowed_resolutions) && issue.allowed_resolutions.length > 0;
}

function ivV2IssueIsOpen(issue) {
  return !['resolved', 'dismissed'].includes(String(issue?.status || 'open'));
}

function ivV2IssueContextTarget(issue) {
  const draft = ivV2EnsureIssueDraft(issue) || {};
  if (String(draft.target_id || '').startsWith('ev_')) return String(draft.target_id);
  return ivV2IssueEvidenceTargets(issue)[0] || '';
}

function ivV2EvidenceContextKey(issue, evidenceId) {
  return `${issue?.issue_id || ''}:${evidenceId || ''}`;
}

async function ivV2EnsureEvidenceContext(issue, { force = false, retryOnHeadMismatch = true } = {}) {
  const evidenceId = ivV2IssueContextTarget(issue);
  if (!issue?.issue_id || !evidenceId) return;
  const cacheKey = ivV2EvidenceContextKey(issue, evidenceId);
  if (!force && ivV2State.evidenceContextCache[cacheKey]) return;
  const token = ++ivV2State.contextToken;
  ivV2State.contextBusyIssueId = issue.issue_id;
  ivV2RenderConfirmed();
  try {
    const response = await fetch(`/api/v1/interview-evidence/${evidenceId}/context`);
    const data = await response.json();
    if (token !== ivV2State.contextToken) return;
    if (!response.ok) {
      if (response.status === 409) {
        await ivV2LoadStructureWorkspace({ token: ivV2State.requestToken, silentConflictRefresh: true });
        return;
      }
      ivV2SetReviewError(data, response.status, '读取证据上下文失败');
      return;
    }
    const payloadHeads = ivV2HeadPairFromPayload(data);
    const currentHeads = {
      structure_revision_id: ivV2CurrentStructureRevisionId(),
      evidence_revision_id: ivV2CurrentEvidenceRevisionId(),
    };
    if (!ivV2HeadsMatch(payloadHeads, currentHeads)) {
      ivV2InvalidateEvidenceContext();
      if (retryOnHeadMismatch) {
        const loaded = await ivV2LoadStructureWorkspace({
          token: ivV2State.requestToken,
          silentConflictRefresh: true,
          headRetry: 0,
        });
        if (!loaded || !ivV2IsTokenCurrent(ivV2State.requestToken)) {
          ivV2SetReviewError(
            { error: { code: 'STRUCTURE_REVISION_CONFLICT', message: '证据上下文版本已变化，请刷新后重试。' } },
            409,
            '证据上下文版本已变化，请刷新后重试。'
          );
          return;
        }
        const refreshedIssue = ivV2IssueById(issue.issue_id);
        if (!refreshedIssue) {
          ivV2SetReviewError(
            { error: { code: 'STRUCTURE_REVISION_CONFLICT', message: '证据上下文版本已变化，请刷新后重试。' } },
            409,
            '证据上下文版本已变化，请刷新后重试。'
          );
          return;
        }
        await ivV2EnsureEvidenceContext(refreshedIssue, {
          force: true,
          retryOnHeadMismatch: false,
        });
        return;
      }
      ivV2SetReviewError(
        { error: { code: 'STRUCTURE_REVISION_CONFLICT', message: '证据上下文版本已变化，请刷新后重试。' } },
        409,
        '证据上下文版本已变化，请刷新后重试。'
      );
      return;
    }
    ivV2State.evidenceContextCache[cacheKey] = {
      token,
      evidence_id: evidenceId,
      payload: data,
    };
    ivV2ClearStatusError();
  } catch (error) {
    if (token !== ivV2State.contextToken) return;
    ivV2State.errorMessage = String(error?.message || '读取证据上下文失败');
  } finally {
    if (token === ivV2State.contextToken) {
      ivV2State.contextBusyIssueId = '';
      ivV2RenderConfirmed();
    }
  }
}

async function ivV2LoadStructureWorkspace({ token = ivV2State.requestToken, silentConflictRefresh = false, headRetry = 1 } = {}) {
  if (!ivV2State.importId) return false;
  try {
    const previousEvidenceRevisionId = ivV2CurrentEvidenceRevisionId();
    const [structureResp, issuesResp] = await Promise.all([
      fetch(`/api/v1/interview-imports/${ivV2State.importId}/structure`),
      fetch(`/api/v1/interview-imports/${ivV2State.importId}/review-issues`),
    ]);
    const structureData = await structureResp.json();
    const issuesData = await issuesResp.json();
    if (!ivV2IsTokenCurrent(token)) return false;
    if (!structureResp.ok || !issuesResp.ok) {
      const payload = !structureResp.ok ? structureData : issuesData;
      const status = !structureResp.ok ? structureResp.status : issuesResp.status;
      const code = String(payload?.error?.code || '');
      if (status === 409 && ['STRUCTURE_INPUT_CONFLICT', 'STRUCTURE_INPUT_NOT_READY'].includes(code)) {
        ivV2ResetStructureWorkspace();
        await ivV2LoadImportBundle(ivV2State.importId, { keepStep: false, token });
        if (!ivV2IsTokenCurrent(token)) return false;
        ivV2State.currentStep = 2;
        ivV2SetStep(2);
        ivV2SetReviewError(payload, status, '分组映射已变化，请先确认最新映射');
        return false;
      }
      if (status === 409 && ['STRUCTURE_NOT_BUILT', 'STRUCTURE_INPUT_STALE'].includes(code)) {
        ivV2ResetStructureWorkspace();
        ivV2State.status = 'GROUP_MAPPING_CONFIRMED';
        ivV2SetReviewError(payload, status, code === 'STRUCTURE_NOT_BUILT' ? '当前尚未生成结构复核结果' : '结构复核已过期');
        return false;
      }
      if (status === 409 && code === 'STRUCTURE_REVISION_CONFLICT') {
        if (!silentConflictRefresh) {
          ivV2SetReviewError(payload, status, '结构版本已变化');
        }
        return false;
      }
      ivV2SetReviewError(payload, status, '读取结构复核状态失败');
      return false;
    }
    const structureHeads = ivV2HeadPairFromPayload(structureData);
    const issuesHeads = ivV2HeadPairFromPayload(issuesData);
    if (!ivV2HeadsMatch(structureHeads, issuesHeads)) {
      if (headRetry > 0) {
        return await ivV2LoadStructureWorkspace({ token, silentConflictRefresh, headRetry: headRetry - 1 });
      }
      ivV2SetReviewError(
        { error: { code: 'STRUCTURE_REVISION_CONFLICT', message: '结构与问题列表版本不一致，请刷新后重试。' } },
        409,
        '结构与问题列表版本不一致，请刷新后重试。'
      );
      return false;
    }
    ivV2State.structureResponse = structureData;
    ivV2State.reviewIssuesResponse = issuesData;
    if (
      String(issuesHeads.evidence_revision_id || '')
      && String(issuesHeads.evidence_revision_id) !== previousEvidenceRevisionId
    ) {
      ivV2InvalidateEvidenceContext();
    }
    ivV2State.status = structureData.status || issuesData.status || ivV2State.status;
    const selected = ivV2SyncSelectedIssue({ preserveAll: true });
    if (selected) ivV2EnsureIssueDraft(selected);
    return true;
  } catch (error) {
    if (!ivV2IsTokenCurrent(token)) return false;
    ivV2State.errorMessage = String(error?.message || '读取结构复核状态失败');
    return false;
  } finally {
    if (ivV2IsTokenCurrent(token)) ivV2RenderConfirmed();
  }
}

function ivV2StructureBuildPayload() {
  return {
    base_mapping_revision_id: String(ivV2State.mappingResponse?.mapping_revision_id || ivV2State.mappingResponse?.revision_id || ''),
    base_mapping_sha256: String(ivV2State.mappingResponse?.mapping_sha256 || ''),
  };
}

async function ivV2EnsureStructureWorkspace({ forceRebuild = false, trigger = 'manual' } = {}) {
  if (!ivV2State.importId || !ivV2State.mappingResponse || ivV2OperationBusy()) return false;
  const token = ivV2NextToken();
  ivV2State.buildBusy = true;
  ivV2State.statusNote = forceRebuild || !ivV2HasStructureCheckpoint()
    ? '正在构建结构与证据复核结果'
    : '正在刷新结构与证据复核结果';
  ivV2ClearStatusError();
  ivV2RenderConfirmed();
  try {
    if (!forceRebuild && ivV2HasStructureCheckpoint()) {
      return await ivV2LoadStructureWorkspace({ token });
    }
    const response = await fetch(`/api/v1/interview-imports/${ivV2State.importId}/structure:build`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ivV2StructureBuildPayload()),
    });
    const data = await response.json();
    if (!ivV2IsTokenCurrent(token)) return false;
    if (!response.ok) {
      const code = String(data?.error?.code || '');
      if (response.status === 409 && code === 'STRUCTURE_INPUT_CONFLICT') {
        await ivV2LoadImportBundle(ivV2State.importId, { keepStep: false, token });
        if (!ivV2IsTokenCurrent(token)) return false;
        ivV2State.currentStep = 2;
        ivV2SetStep(2);
        ivV2State.errorMessage = '分组映射已更新，结构复核未落盘。请先确认最新映射，再重新开始结构复核。';
        ivV2State.statusCode = code;
        ivV2RenderConfirmed();
        return false;
      }
      if (response.status === 409 && code === 'STRUCTURE_INPUT_NOT_READY') {
        await ivV2LoadImportBundle(ivV2State.importId, { keepStep: false, token });
        if (!ivV2IsTokenCurrent(token)) return false;
        ivV2State.currentStep = 2;
        ivV2SetStep(2);
        return false;
      }
      ivV2SetReviewError(data, response.status, '生成结构复核失败');
      return false;
    }
    ivV2State.structureResponse = data;
    ivV2State.status = data.status || 'STRUCTURE_REVIEW_REQUIRED';
    const loaded = await ivV2LoadStructureWorkspace({ token, silentConflictRefresh: trigger === 'auto' });
    if (loaded && ivV2IsTokenCurrent(token)) {
      showToast(ivV2State.status === 'READY_FOR_DOSSIERS' ? '结构复核已完成，当前仅开放检查结果' : '结构复核工作台已准备完成', 'success');
    }
    return loaded;
  } catch (error) {
    if (!ivV2IsTokenCurrent(token)) return false;
    ivV2State.errorMessage = String(error?.message || '生成结构复核失败');
    ivV2RenderConfirmed();
    return false;
  } finally {
    if (ivV2IsTokenCurrent(token)) {
      ivV2State.buildBusy = false;
      ivV2RenderConfirmed();
    }
  }
}

function ivV2ReviewSummaryHtml() {
  const response = ivV2State.mappingResponse;
  if (!response) {
    return '<div class="iv-v2-empty">确认完成后，这里会显示结构复核摘要。</div>';
  }
  const previewSource = response.final_participant_preview?.participants?.length
    ? response.final_participant_preview
    : (response.proposals?.final_participant_preview || {});
  const participants = previewSource.participants || [];
  const structure = ivV2State.structureResponse;
  const review = structure?.review_summary || {};
  const evidence = structure?.evidence_summary || {};
  const confirmedAt = response.history?.at(-1)?.confirmed_at || ivV2State.importData?.updated_at || '';
  return `
    ${ivV2State.errorMessage ? `
      <div class="iv-v2-status-banner iv-v2-status-banner--danger">
        <strong>${ivV2Esc(ivV2StatusBannerText())}</strong>
        <p>${ivV2Esc('可直接使用下方重试按钮继续处理；错误信息会在成功刷新、成功构建或成功加载证据上下文后清除。')}</p>
      </div>
    ` : ''}
    <div class="iv-v2-status-grid">
      <div class="iv-v2-status-card iv-v2-status-card--${ivV2StatusTone(ivV2State.status)}">
        <strong>${ivV2Esc(ivV2State.status || 'GROUP_MAPPING_CONFIRMED')}</strong>
        <p>映射版 ${ivV2Esc(response.revision_number || '--')} · SHA ${ivV2Esc((response.mapping_sha256 || '').slice(0, 12) || '--')}</p>
      </div>
      <div class="iv-v2-status-card">
        <strong>${ivV2Esc(participants.length)} 名玩家</strong>
        <p>结构版 ${ivV2Esc(ivV2CurrentStructureRevisionId().slice(0, 18) || '--')} · 证据版 ${ivV2Esc(ivV2CurrentEvidenceRevisionId().slice(0, 18) || '--')}</p>
      </div>
      <div class="iv-v2-status-card">
        <strong>${ivV2Esc(ivV2FormatTime(confirmedAt))}</strong>
        <p>阻塞 ${ivV2Esc(review.blocking_issue_count ?? 0)} · 待处理 ${ivV2Esc(review.open_issue_count ?? 0)} · 证据 ${ivV2Esc(evidence.evidence_count ?? 0)}</p>
      </div>
    </div>
    <div class="iv-v2-status-banner iv-v2-status-banner--info">
      <strong>当前阶段只开放结构与证据复核</strong>
      <p>玩家档案与后续 dossier 工作台尚未开放；当状态为 READY_FOR_DOSSIERS 时，仅表示结构头部已稳定。</p>
    </div>
  `;
}

function ivV2IssueListHtml() {
  const issues = ivV2VisibleReviewIssues();
  const filters = ivV2ReviewFilterOptions();
  const operationBusy = ivV2OperationBusy();
  return `
    <section class="iv-v2-side-card iv-v2-review-shell">
      <div class="iv-v2-review-head">
        <div>
          <div class="iv-v2-side-card__title">问题队列</div>
          <p class="iv-v2-review-head__desc">优先处理阻塞项；单项修复会基于结构版与证据版双头校验。</p>
        </div>
        <div class="iv-v2-toolbar__actions">
          <button class="btn btn--ghost btn--sm" type="button" data-iv-v2-action="refresh-structure-review"${operationBusy ? ' disabled' : ''}>刷新状态</button>
          <button class="btn btn--primary btn--sm" type="button" data-iv-v2-action="retry-structure-build"${operationBusy ? ' disabled' : ''}>${ivV2HasStructureCheckpoint() ? '重新生成结构' : '开始结构复核'}</button>
        </div>
      </div>
      <div class="iv-v2-filter-row">
        <label class="iv-v2-filter-field">
          <span>显示范围</span>
          <select data-iv-v2-action="review-filter"${operationBusy ? ' disabled' : ''}>
            ${filters.map(item => `<option value="${ivV2Esc(item.value)}"${item.value === ivV2State.reviewFilter ? ' selected' : ''}>${ivV2Esc(item.label)}</option>`).join('')}
          </select>
        </label>
      </div>
      <div class="iv-v2-review-list">
        ${issues.length ? issues.map(issue => `
          <button class="iv-v2-review-item${issue.issue_id === ivV2State.selectedIssueId ? ' iv-v2-review-item--active' : ''}" type="button" data-iv-v2-action="select-review-issue" data-issue-id="${ivV2Esc(issue.issue_id)}">
            <div class="iv-v2-review-item__head">
              <strong>${ivV2Esc(issue.code || 'REVIEW_ISSUE')}</strong>
              <span class="iv-v2-badge iv-v2-badge--${ivV2IssueSeverityTone(issue)}">${ivV2Esc(ivV2IssueStatusText(issue))}</span>
            </div>
            <p>${ivV2Esc(issue.message || '')}</p>
            <span>${ivV2Esc(issue.report_impact || issue.reason || '请打开详情处理')}</span>
          </button>
        `).join('') : '<div class="iv-v2-empty">当前筛选下没有待处理问题。</div>'}
      </div>
    </section>
  `;
}

function ivV2StructureTreeHtml() {
  const structure = ivV2CurrentStructurePayload();
  if (!structure) {
    return '<div class="iv-v2-empty">结构结果尚未生成。</div>';
  }
  const modules = ivV2Modules();
  const mainQuestions = ivV2MainQuestions();
  const occurrences = ivV2Occurrences();
  return `
    <section class="iv-v2-side-card iv-v2-structure-tree">
      <div class="iv-v2-side-card__title">只读结构树</div>
      ${modules.length ? modules.map(module => {
        const questions = mainQuestions.filter(item => item.module_id === module.module_id);
        const linkedOccurrences = occurrences.filter(item => item.canonical_module_id === module.module_id);
        return `
          <div class="iv-v2-tree-node">
            <div class="iv-v2-tree-node__head">
              <strong>${ivV2Esc(module.canonical_name || module.module_id)}</strong>
              <span>${ivV2Esc(linkedOccurrences.length)} 行</span>
            </div>
            <p>${ivV2Esc(module.decision_status || '')} · ${ivV2Esc(module.mapping_method || '')}</p>
            <div class="iv-v2-tree-children">
              ${questions.length ? questions.map(question => `
                <div class="iv-v2-tree-leaf">
                  <strong>${ivV2Esc(question.canonical_text || question.main_question_id)}</strong>
                  <span>${ivV2Esc(question.occurrence_ids?.length || 0)} 个 occurrence</span>
                </div>
              `).join('') : '<div class="iv-v2-empty">当前模块下没有主问题。</div>'}
            </div>
          </div>
        `;
      }).join('') : '<div class="iv-v2-empty">当前没有模块节点。</div>'}
    </section>
  `;
}

function ivV2ReviewTargetOptions(kind, selected) {
  if (kind === 'module') {
    return ivV2Modules().map(module => `<option value="${ivV2Esc(module.module_id)}"${module.module_id === selected ? ' selected' : ''}>${ivV2Esc(module.canonical_name || module.module_id)}</option>`).join('');
  }
  if (kind === 'question') {
    return ivV2MainQuestions().map(question => `<option value="${ivV2Esc(question.main_question_id)}"${question.main_question_id === selected ? ' selected' : ''}>${ivV2Esc(question.canonical_text || question.main_question_id)}</option>`).join('');
  }
  return '';
}

function ivV2ReviewEvidenceTargetOptions(issue, selected) {
  return ivV2IssueEvidenceTargets(issue).map(id => `<option value="${ivV2Esc(id)}"${id === selected ? ' selected' : ''}>${ivV2Esc(id)}</option>`).join('');
}

function ivV2ReviewResolutionFieldsHtml(issue) {
  const draft = ivV2EnsureIssueDraft(issue);
  const allowed = Array.isArray(issue.allowed_resolutions) ? issue.allowed_resolutions : [];
  if (!allowed.length) {
    return '<div class="iv-v2-empty">该问题当前没有允许的前端修复动作。需要修正源文件并重新上传，或先调整映射后重新构建结构复核。</div>';
  }
  const action = String(draft?.resolution || allowed[0] || '');
  const suggestion = issue.suggested_resolution || {};
  const canLinkQuestion = ['follow_up', 'observation_row'].includes(String(draft?.row_role || ''));
  let extra = '';
  if (action === 'assign_row_role') {
    extra = `
      <label class="iv-v2-inline-field">
        <span>行角色</span>
        <select data-iv-v2-action="review-draft-row-role" data-issue-id="${ivV2Esc(issue.issue_id)}">
          <option value="">请选择</option>
          ${['module_header', 'main_question', 'follow_up', 'observation_row'].map(role => `<option value="${role}"${role === draft.row_role ? ' selected' : ''}>${ivV2Esc(ivV2FormatRowRoleLabel(role))}</option>`).join('')}
        </select>
      </label>
      ${canLinkQuestion ? `
        <label class="iv-v2-inline-field">
          <span>关联主问题（可选）</span>
          <select data-iv-v2-action="review-draft-target" data-issue-id="${ivV2Esc(issue.issue_id)}">
            <option value="">不关联主问题</option>
            ${ivV2ReviewTargetOptions('question', draft.target_id)}
          </select>
        </label>
      ` : ''}
    `;
  } else if (action === 'assign_module') {
    extra = `
      <label class="iv-v2-inline-field">
        <span>目标模块</span>
        <select data-iv-v2-action="review-draft-target" data-issue-id="${ivV2Esc(issue.issue_id)}">
          <option value="">请选择模块</option>
          ${ivV2ReviewTargetOptions('module', draft.target_id)}
        </select>
      </label>
    `;
  } else if (action === 'assign_main_question') {
    extra = `
      <label class="iv-v2-inline-field">
        <span>目标主问题</span>
        <select data-iv-v2-action="review-draft-target" data-issue-id="${ivV2Esc(issue.issue_id)}">
          <option value="">请选择主问题</option>
          ${ivV2ReviewTargetOptions('question', draft.target_id)}
        </select>
      </label>
    `;
  } else if (action === 'set_evidence_identity') {
    extra = `
      <label class="iv-v2-inline-field">
        <span>证据目标</span>
        <select data-iv-v2-action="review-draft-target" data-issue-id="${ivV2Esc(issue.issue_id)}">
          <option value="">请选择证据</option>
          ${ivV2ReviewEvidenceTargetOptions(issue, draft.target_id)}
        </select>
      </label>
      <label class="iv-v2-inline-field">
        <span>证据类型</span>
        <select data-iv-v2-action="review-draft-evidence-type" data-issue-id="${ivV2Esc(issue.issue_id)}">
          <option value="">请选择</option>
          ${['participant_self_report', 'researcher_observation'].map(type => `<option value="${type}"${type === draft.evidence_type ? ' selected' : ''}>${ivV2Esc(ivV2FormatEvidenceTypeLabel(type))}</option>`).join('')}
        </select>
      </label>
    `;
  } else if (action === 'exclude_evidence') {
    extra = `
      <label class="iv-v2-inline-field">
        <span>证据目标</span>
        <select data-iv-v2-action="review-draft-target" data-issue-id="${ivV2Esc(issue.issue_id)}">
          <option value="">请选择证据</option>
          ${ivV2ReviewEvidenceTargetOptions(issue, draft.target_id)}
        </select>
      </label>
    `;
  } else if (action === 'accept_suggestion') {
    extra = `
      <div class="iv-v2-suggestion">
        <strong>系统建议</strong>
        <p>${ivV2Esc([
          suggestion.resolution ? `动作：${ivV2FormatResolutionLabel(suggestion.resolution)}` : '',
          suggestion.row_role ? `行角色：${ivV2FormatRowRoleLabel(suggestion.row_role)}` : '',
          suggestion.evidence_type ? `证据类型：${ivV2FormatEvidenceTypeLabel(suggestion.evidence_type)}` : '',
          suggestion.target_id ? `目标：${suggestion.target_id}` : '',
        ].filter(Boolean).join(' · ') || '当前没有结构化建议，仅记录接受意见。')}</p>
      </div>
    `;
  }
  return `
    <div class="iv-v2-resolution-grid">
      <label class="iv-v2-inline-field">
        <span>修复动作</span>
        <select data-iv-v2-action="review-draft-resolution" data-issue-id="${ivV2Esc(issue.issue_id)}">
          ${allowed.map(item => `<option value="${ivV2Esc(item)}"${item === action ? ' selected' : ''}>${ivV2Esc(ivV2FormatResolutionLabel(item))}</option>`).join('')}
        </select>
      </label>
      ${extra}
      <label class="iv-v2-inline-field iv-v2-inline-field--full">
        <span>处理备注</span>
        <textarea rows="4" maxlength="500" data-iv-v2-action="review-draft-comment" data-issue-id="${ivV2Esc(issue.issue_id)}" placeholder="说明为什么这样处理，最多 500 字">${ivV2Esc(draft.comment || '')}</textarea>
        <em>${ivV2Esc((draft.comment || '').length)}/500</em>
      </label>
    </div>
  `;
}

function ivV2IssueDetailHtml() {
  const issue = ivV2CurrentIssue();
  if (!issue) {
    return '<div class="iv-v2-empty">从左侧选择一个问题后，可查看证据上下文并提交单项修复。</div>';
  }
  const draft = ivV2EnsureIssueDraft(issue);
  const evidenceId = ivV2IssueContextTarget(issue);
  const cache = evidenceId ? ivV2State.evidenceContextCache[ivV2EvidenceContextKey(issue, evidenceId)] : null;
  const evidence = cache?.payload?.evidence || null;
  const context = cache?.payload?.source_context || null;
  const operationBusy = ivV2OperationBusy();
  const canSubmit = ivV2IssueHasResolvableAction(issue) && ivV2IssueIsOpen(issue);
  return `
    <section class="iv-v2-side-card iv-v2-issue-detail">
      <div class="iv-v2-issue-detail__head">
        <div>
          <div class="iv-v2-side-card__title">${ivV2Esc(issue.code || 'REVIEW_ISSUE')}</div>
          <p>${ivV2Esc(issue.message || '')}</p>
        </div>
        <span class="iv-v2-badge iv-v2-badge--${ivV2IssueSeverityTone(issue)}">${ivV2Esc(issue.severity || 'info')}</span>
      </div>
      <div class="iv-v2-issue-meta">
        <span>状态：${ivV2Esc(ivV2IssueStatusText(issue))}</span>
        <span>影响：${ivV2Esc(issue.report_impact || '待确认')}</span>
        <span>建议动作：${ivV2Esc(issue.suggested_action || 'review_structure_issue')}</span>
      </div>
      <div class="iv-v2-context-card">
        <div class="iv-v2-context-card__head">
          <strong>证据上下文</strong>
          <div class="iv-v2-toolbar__actions">
            ${evidenceId ? `<button class="btn btn--ghost btn--sm" type="button" data-iv-v2-action="load-evidence-context" data-issue-id="${ivV2Esc(issue.issue_id)}"${operationBusy ? ' disabled' : ''}>${ivV2State.contextBusyIssueId === issue.issue_id ? '加载中...' : '刷新上下文'}</button>` : ''}
          </div>
        </div>
        ${evidenceId ? (
          (evidence || context) ? `
            <div class="iv-v2-context-card__body">
              ${evidence ? `
                <p>Context #${ivV2Esc(cache.token)} · ${ivV2Esc(evidence.sheet_name || evidence.sheet_id || '')} · ${ivV2Esc(evidence.cell_address || '')}</p>
                <p>玩家 ${ivV2Esc(evidence.participant_label || '--')} · 记录员 ${ivV2Esc(evidence.recorder_label || '--')} · 证据类型 ${ivV2Esc(ivV2FormatEvidenceTypeLabel(evidence.evidence_type || ''))}</p>
                <p>Prompt：${ivV2Esc(evidence.prompt_text || '无')}</p>
                <p>Raw：${ivV2Esc(evidence.raw_content || '')}</p>
                <p>Display：${ivV2Esc(evidence.display_content || '')}</p>
                <p>Normalized：${ivV2Esc(evidence.normalized_content || '')}</p>
                <p>身份状态 ${ivV2Esc(evidence.identity_decision_status || '--')} · 公式缓存 ${ivV2Esc(evidence.formula_cache_status || '--')}</p>
              ` : ''}
              ${context ? `
                <p>行 ${ivV2Esc(context.row ?? '--')} / 列 ${ivV2Esc(context.column ?? '--')} · source_cell_id ${ivV2Esc(context.source_cell_id || '--')}</p>
              ` : ''}
              <div class="iv-v2-context-list">
                ${((context?.neighboring_occurrences) || []).length ? context.neighboring_occurrences.map(item => `
                  <div class="iv-v2-context-list__item">
                    <strong>${ivV2Esc(ivV2FormatRowRoleLabel(item.row_role || 'unknown'))}</strong>
                    <span>${ivV2Esc(item.sheet_name || item.sheet_id || '')} · 第 ${ivV2Esc(item.row)} 行</span>
                    <p>${ivV2Esc(item.raw_prompt_text || item.raw_module_text || item.raw_type_text || '无额外文本')}</p>
                  </div>
                `).join('') : '<div class="iv-v2-empty">附近没有可公开的结构邻近项。</div>'}
              </div>
            </div>
          ` : `<div class="iv-v2-empty">${ivV2State.contextBusyIssueId === issue.issue_id ? '正在按证据目标懒加载上下文...' : '当前尚未加载上下文。'}</div>`
        ) : '<div class="iv-v2-empty">该问题没有可公开的证据目标，无法展开上下文。</div>'}
      </div>
      <div class="iv-v2-resolution-card">
        <div class="iv-v2-context-card__head">
          <strong>单项修复</strong>
          <button class="btn btn--primary btn--sm" type="button" data-iv-v2-action="submit-review-issue" data-issue-id="${ivV2Esc(issue.issue_id)}"${operationBusy || !canSubmit ? ' disabled' : ''}>${ivV2State.reviewBusy ? '提交中...' : '提交此项修复'}</button>
        </div>
        ${ivV2ReviewResolutionFieldsHtml(issue)}
      </div>
    </section>
  `;
}

function ivV2SyncConfirmedControls() {
  const confirmedShell = ivV2$('iv-v2-confirmed-shell');
  if (!confirmedShell) return;
  const operationBusy = ivV2OperationBusy();
  confirmedShell.setAttribute('aria-busy', operationBusy ? 'true' : 'false');
  confirmedShell.querySelectorAll('input, select, textarea, button').forEach(control => {
    const action = control.dataset?.ivV2Action || '';
    if (action === 'submit-review-issue') {
      const issue = ivV2IssueById(control.dataset.issueId || '');
      control.disabled = operationBusy || !(ivV2IssueHasResolvableAction(issue) && ivV2IssueIsOpen(issue));
      return;
    }
    control.disabled = operationBusy;
  });
}

function ivV2SyncCommentCounter(textarea) {
  if (!textarea) return;
  const counter = textarea.parentElement?.querySelector('em');
  if (counter) counter.textContent = `${String(textarea.value || '').length}/500`;
}

function ivV2HeadsHtml() {
  const response = ivV2State.mappingResponse;
  return `
    <section class="iv-v2-side-card">
      <div class="iv-v2-side-card__title">版本头信息</div>
      <div class="iv-v2-head-list">
        <div class="iv-v2-history-item">
          <div>
            <strong>映射头</strong>
            <p>${ivV2Esc(response?.mapping_revision_id || '--')} · SHA ${ivV2Esc((response?.mapping_sha256 || '').slice(0, 12) || '--')}</p>
          </div>
          <span class="iv-v2-badge">第 ${ivV2Esc(response?.revision_number || '--')} 版</span>
        </div>
        <div class="iv-v2-history-item">
          <div>
            <strong>结构头</strong>
            <p>${ivV2Esc(ivV2CurrentStructureRevisionId() || '--')}</p>
          </div>
          <span class="iv-v2-badge">${ivV2Esc(ivV2State.status || '--')}</span>
        </div>
        <div class="iv-v2-history-item">
          <div>
            <strong>证据头</strong>
            <p>${ivV2Esc(ivV2CurrentEvidenceRevisionId() || '--')}</p>
          </div>
          <span class="iv-v2-badge">${ivV2Esc(ivV2State.statusCode || 'CAS')}</span>
        </div>
      </div>
      <div class="iv-v2-status-banner iv-v2-status-banner--warning">
        <strong>冲突恢复说明</strong>
        <p>单项修复会同时提交 structure/evidence 双版本头。若任一头变化，前端会刷新当前工作台，避免跨版本写入。</p>
      </div>
    </section>
    <section class="iv-v2-side-card">
      <div class="iv-v2-side-card__title">映射历史</div>
      <div>${ivV2HistoryHtml()}</div>
    </section>
  `;
}

function ivV2RenderConfirmed() {
  const meta = ivV2$('iv-v2-confirmed-meta');
  const preview = ivV2$('iv-v2-confirmed-preview');
  const history = ivV2$('iv-v2-confirmed-history');
  if (!meta || !preview || !history) return;
  ivV2RenderStep3Header();

  const response = ivV2State.mappingResponse;
  if (!response) {
    meta.innerHTML = '<div class="iv-v2-empty">确认完成后，这里会显示结构复核工作台。</div>';
    preview.innerHTML = '';
    history.innerHTML = '';
    return;
  }

  meta.innerHTML = ivV2ReviewSummaryHtml();
  preview.innerHTML = `
    ${ivV2IssueListHtml()}
    ${ivV2IssueDetailHtml()}
    ${ivV2StructureTreeHtml()}
  `;
  history.innerHTML = ivV2HeadsHtml();
  const operationBusy = ivV2OperationBusy();
  const backButton = ivV2$('iv-v2-back-to-editor');
  const startOverButton = ivV2$('iv-v2-start-over');
  if (backButton) {
    backButton.disabled = operationBusy;
    backButton.textContent = '返回分组映射';
  }
  if (startOverButton) {
    startOverButton.disabled = operationBusy;
    startOverButton.textContent = '重新上传工作簿';
  }
  ivV2SyncConfirmedControls();
  ivV2SyncTrackToggle();
}

function ivV2FindGroup(groupId) {
  return ivV2State.draft?.groups?.find(group => group.temp_id === groupId) || null;
}

function ivV2FindParticipant(groupId, participantId) {
  const group = ivV2FindGroup(groupId);
  return {
    group,
    participant: group?.participants?.find(item => item.temp_id === participantId) || null,
  };
}

async function ivV2SaveDraft() {
  const revisionNumber = Number(ivV2State.mappingResponse?.revision_number || 0);
  if (
    !ivV2State.mappingResponse
    || ivV2OperationBusy()
    || (revisionNumber > 0 && !ivV2State.draftDirty)
  ) return;
  const token = ivV2NextToken();
  ivV2State.saveBusy = true;
  ivV2ClearStatusError();
  ivV2State.statusNote = '正在保存分组草稿';
  ivV2RenderEditor();
  ivV2RenderConfirmed();
  try {
    const response = await fetch(`/api/v1/interview-imports/${ivV2State.importId}/group-mapping`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ivV2Payload()),
    });
    const data = await response.json();
    if (!ivV2IsTokenCurrent(token)) return;
    if (!response.ok) {
      if (response.status === 409) {
        ivV2SetStatusError(data, response.status, '映射版本已变化');
        ivV2State.errorMessage = '映射版本已变化；当前未保存草稿仍保留。请先记录改动，再点击“刷新状态”获取最新版本。';
        throw new Error(ivV2State.errorMessage);
      }
      ivV2SetStatusError(data, response.status, '保存草稿失败');
      throw new Error(ivV2State.errorMessage);
    }

    ivV2State.mappingResponse = data;
    ivV2State.status = data.status || 'GROUP_CONFIRMATION_REQUIRED';
    ivV2RefreshSheetCatalog(data);
    ivV2State.draft = ivV2BuildDraft(data);
    ivV2ClearStatusError();
    ivV2ClearDirty('草稿已保存');
    ivV2RenderEditor();
    ivV2RenderConfirmed();
    showToast('分组草稿已保存', 'success');
  } catch (error) {
    if (!ivV2IsTokenCurrent(token)) return;
    ivV2ShowToastFromError(error, '保存草稿失败');
  } finally {
    if (ivV2IsTokenCurrent(token)) {
      ivV2State.saveBusy = false;
      ivV2RenderEditor();
      ivV2RenderConfirmed();
    }
  }
}

async function ivV2Restore(entry, changeKind, changeReason) {
  if (!entry || ivV2OperationBusy()) return;
  if (ivV2State.draftDirty) {
    showToast('当前有未保存改动，请先保存草稿再恢复历史版本', 'info');
    return;
  }
  const token = ivV2NextToken();
  ivV2State.restoreBusy = true;
  ivV2ClearStatusError();
  ivV2State.statusNote = '正在恢复历史版本';
  ivV2RenderEditor();
  ivV2RenderConfirmed();
  try {
    const response = await fetch(`/api/v1/interview-imports/${ivV2State.importId}/group-mapping:restore`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        base_mapping_revision: Number(ivV2State.mappingResponse?.revision_number || 0),
        target_mapping_revision_id: entry.mapping_revision_id,
        target_mapping_sha256: entry.mapping_sha256,
        change_kind: changeKind,
        change_reason: changeReason,
      }),
    });
    const data = await response.json();
    if (!ivV2IsTokenCurrent(token)) return;
    if (!response.ok) {
      if (response.status === 409) {
        await ivV2LoadImportBundle(ivV2State.importId, { keepStep: true, token });
        if (!ivV2IsTokenCurrent(token)) return;
        throw new Error('历史版本已变化，已刷新到最新状态');
      }
      ivV2SetStatusError(data, response.status, '恢复历史版本失败');
      throw new Error(ivV2State.errorMessage);
    }

    ivV2State.mappingResponse = data;
    ivV2State.status = data.status || 'GROUP_CONFIRMATION_REQUIRED';
    ivV2RefreshSheetCatalog(data);
    ivV2State.draft = ivV2BuildDraft(data);
    ivV2ClearStatusError();
    ivV2ClearDirty('已恢复到所选版本');
    ivV2State.currentStep = 2;
    ivV2SetStep(2);
    ivV2RenderEditor();
    ivV2RenderConfirmed();
    showToast('映射版本已恢复', 'success');
  } catch (error) {
    if (!ivV2IsTokenCurrent(token)) return;
    ivV2ShowToastFromError(error, '恢复历史版本失败');
  } finally {
    if (ivV2IsTokenCurrent(token)) {
      ivV2State.restoreBusy = false;
      ivV2RenderEditor();
      ivV2RenderConfirmed();
    }
  }
}

async function ivV2ConfirmMapping() {
  if (
    !ivV2State.mappingResponse
    || !ivV2State.mappingResponse.confirmation_ready
    || Number(ivV2State.mappingResponse.revision_number || 0) < 1
    || ivV2OperationBusy()
    || ivV2State.draftDirty
  ) return;
  const token = ivV2NextToken();
  ivV2State.confirmBusy = true;
  ivV2ResetStructureWorkspace();
  ivV2ClearStatusError();
  ivV2State.statusNote = '正在确认分组版本';
  ivV2RenderEditor();
  ivV2RenderConfirmed();
  try {
    const response = await fetch(`/api/v1/interview-imports/${ivV2State.importId}/group-mapping:confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        base_mapping_revision: Number(ivV2State.mappingResponse?.revision_number || 0),
        mapping_sha256: String(ivV2State.mappingResponse?.mapping_sha256 || ''),
      }),
    });
    const data = await response.json();
    if (!ivV2IsTokenCurrent(token)) return;
    if (!response.ok) {
      if (response.status === 409) {
        await ivV2LoadImportBundle(ivV2State.importId, { keepStep: true, token });
        if (!ivV2IsTokenCurrent(token)) return;
        throw new Error('确认前发现版本冲突，已刷新到最新状态');
      }
      ivV2SetStatusError(data, response.status, '确认分组失败');
      throw new Error(ivV2State.errorMessage);
    }

    ivV2State.mappingResponse = data;
    ivV2RefreshSheetCatalog(data);
    ivV2State.draft = ivV2BuildDraft(data);
    ivV2State.status = data.status || 'GROUP_MAPPING_CONFIRMED';
    ivV2ClearStatusError();
    ivV2ClearDirty('已确认映射，正在准备结构复核');
    ivV2State.currentStep = 3;
    ivV2SetStep(3);
    ivV2RenderEditor();
    ivV2RenderConfirmed();
    showToast('分组确认完成，正在准备结构复核', 'success');
    ivV2State.confirmBusy = false;
    await ivV2EnsureStructureWorkspace({ trigger: 'auto' });
  } catch (error) {
    if (!ivV2IsTokenCurrent(token)) return;
    ivV2ShowToastFromError(error, '确认分组失败');
  } finally {
    if (ivV2IsTokenCurrent(token)) {
      ivV2State.confirmBusy = false;
      ivV2RenderEditor();
      ivV2RenderConfirmed();
    }
  }
}

function ivV2UpdateIssueDraft(issueId, patch) {
  const issue = ivV2IssueById(issueId);
  if (!issue) return;
  const current = ivV2EnsureIssueDraft(issue);
  ivV2State.issueDrafts[issueId] = { ...current, ...patch };
}

function ivV2NormalizeIssueDraft(issue) {
  const draft = { ...ivV2EnsureIssueDraft(issue) };
  if (draft.resolution !== 'assign_row_role') {
    draft.row_role = '';
  }
  if (!['assign_row_role', 'assign_module', 'assign_main_question', 'set_evidence_identity', 'exclude_evidence'].includes(draft.resolution)) {
    draft.target_id = '';
  }
  if (draft.resolution !== 'set_evidence_identity') {
    draft.evidence_type = '';
  }
  if (draft.resolution === 'assign_row_role' && !['follow_up', 'observation_row'].includes(draft.row_role)) {
    draft.target_id = '';
  }
  return draft;
}

function ivV2ValidateIssueDraft(issue) {
  const draft = ivV2NormalizeIssueDraft(issue);
  const allowed = Array.isArray(issue.allowed_resolutions) ? issue.allowed_resolutions : [];
  if (!allowed.includes(draft.resolution)) return { ok: false, message: '当前问题不允许该修复动作' };
  if (!String(draft.comment || '').trim()) return { ok: false, message: '请填写处理备注' };
  if (String(draft.comment || '').trim().length > 500) return { ok: false, message: '处理备注不能超过 500 字' };
  if (draft.resolution === 'assign_row_role' && !draft.row_role) return { ok: false, message: '请选择行角色' };
  if (draft.resolution === 'assign_module' && !String(draft.target_id || '').startsWith('module_')) return { ok: false, message: '请选择目标模块' };
  if (draft.resolution === 'assign_main_question' && !String(draft.target_id || '').startsWith('question_')) return { ok: false, message: '请选择目标主问题' };
  if (draft.resolution === 'set_evidence_identity') {
    if (!String(draft.target_id || '').startsWith('ev_')) return { ok: false, message: '请选择证据目标' };
    if (!draft.evidence_type) return { ok: false, message: '请选择证据类型' };
  }
  if (draft.resolution === 'exclude_evidence' && !String(draft.target_id || '').startsWith('ev_')) {
    return { ok: false, message: '请选择要排除的证据' };
  }
  return { ok: true, draft };
}

async function ivV2SubmitIssueResolution(issueId) {
  const issue = ivV2IssueById(issueId);
  if (!issue || ivV2OperationBusy()) return;
  const validation = ivV2ValidateIssueDraft(issue);
  if (!validation.ok) {
    showToast(validation.message, 'error');
    return;
  }
  if (validation.draft.resolution === 'exclude_evidence') {
    const confirmed = window.confirm('确认排除此证据吗？排除后它不会进入报告；如果这是该玩家最后一条证据，后端也可能拒绝此次排除。');
    if (!confirmed) return;
  }
  const token = ivV2NextToken();
  const previousEvidenceRevisionId = ivV2CurrentEvidenceRevisionId();
  ivV2State.reviewBusy = true;
  ivV2State.statusNote = '正在提交单项修复';
  ivV2ClearStatusError();
  ivV2RenderConfirmed();
  try {
    const draft = validation.draft;
    const response = await fetch(`/api/v1/interview-review-issues/${issueId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        base_structure_revision_id: ivV2CurrentStructureRevisionId(),
        base_evidence_revision_id: ivV2CurrentEvidenceRevisionId(),
        resolution: draft.resolution,
        target_id: draft.target_id || null,
        row_role: draft.row_role || null,
        evidence_type: draft.evidence_type || null,
        comment: String(draft.comment || '').trim(),
      }),
    });
    const data = await response.json();
    if (!ivV2IsTokenCurrent(token)) return;
    if (!response.ok) {
      const code = String(data?.error?.code || '');
      if (response.status === 409) {
        if (['STRUCTURE_INPUT_CONFLICT', 'STRUCTURE_INPUT_NOT_READY'].includes(code)) {
          ivV2ResetStructureWorkspace();
          await ivV2LoadImportBundle(ivV2State.importId, { keepStep: false, token });
          if (!ivV2IsTokenCurrent(token)) return;
          ivV2State.currentStep = 2;
          ivV2SetStep(2);
          ivV2SetReviewError(data, response.status, '分组映射已变化，请先确认最新映射');
          return;
        }
        await ivV2LoadStructureWorkspace({ token, silentConflictRefresh: true });
        if (!ivV2IsTokenCurrent(token)) return;
        ivV2SetReviewError(data, response.status, code === 'STRUCTURE_REVISION_CONFLICT' ? '结构版本已更新，已刷新最新结果' : '结构状态已变化');
        return;
      }
      ivV2SetReviewError(data, response.status, '提交单项修复失败');
      return;
    }
    delete ivV2State.issueDrafts[issueId];
    if (String(data?.evidence_revision_id || '') && String(data.evidence_revision_id) !== previousEvidenceRevisionId) {
      ivV2InvalidateEvidenceContext();
    }
    await ivV2LoadStructureWorkspace({ token, silentConflictRefresh: true });
    if (ivV2IsTokenCurrent(token)) showToast('单项修复已提交', 'success');
  } catch (error) {
    if (!ivV2IsTokenCurrent(token)) return;
    ivV2State.errorMessage = String(error?.message || '提交单项修复失败');
    ivV2RenderConfirmed();
  } finally {
    if (ivV2IsTokenCurrent(token)) {
      ivV2State.reviewBusy = false;
      ivV2RenderConfirmed();
    }
  }
}

function ivV2HandleEditorClick(event) {
  if (ivV2OperationBusy()) return;
  const button = event.target.closest('[data-iv-v2-action]');
  if (!button) return;
  const action = button.dataset.ivV2Action;

  if (action === 'remove-group') {
    ivV2State.draft.groups = ivV2State.draft.groups.filter(group => group.temp_id !== button.dataset.groupId);
    ivV2NormalizeDraft();
    ivV2MarkDirty();
    ivV2RenderEditor();
    return;
  }

  if (action === 'add-participant') {
    const group = ivV2FindGroup(button.dataset.groupId);
    const [first] = ivV2AvailableColumns(group || {});
    if (!group || !first) return;
    group.participants.push({
      temp_id: `participant_new_${Math.random().toString(16).slice(2, 8)}`,
      server_participant_id: '',
      participant_label: first.raw_header || `${first.sheet_name}-${first.column_letter || first.column}`,
      columns: [{ sheet_id: first.sheet_id, column: first.column }],
    });
    ivV2MarkDirty();
    ivV2RenderEditor();
    return;
  }

  if (action === 'remove-participant') {
    const group = ivV2FindGroup(button.dataset.groupId);
    if (!group) return;
    group.participants = group.participants.filter(item => item.temp_id !== button.dataset.participantId);
    ivV2MarkDirty();
    ivV2RenderEditor();
    return;
  }

  if (action === 'remove-column') {
    const { group, participant } = ivV2FindParticipant(button.dataset.groupId, button.dataset.participantId);
    if (!group || !participant) return;
    participant.columns = participant.columns.filter(column => !(
      column.sheet_id === button.dataset.sheetId && Number(column.column) === Number(button.dataset.column)
    ));
    group.participants = group.participants.filter(item => item.columns.length > 0);
    ivV2MarkDirty();
    ivV2RenderEditor();
    return;
  }

  if (action === 'restore-history') {
    const entry = (ivV2State.mappingResponse?.history || []).find(item => Number(item.revision_number) === Number(button.dataset.historyRevision));
    ivV2Restore(entry, 'restore', `恢复到第 ${button.dataset.historyRevision} 版`);
    return;
  }

  if (action === 'restore-ignored-sheet') {
    ivV2State.draft.ignoredSheetIds = ivV2State.draft.ignoredSheetIds.filter(id => id !== button.dataset.sheetId);
    ivV2MarkDirty();
    ivV2RenderEditor();
    return;
  }

  if (action === 'select-review-issue') {
    ivV2SelectIssue(button.dataset.issueId);
    return;
  }

  if (action === 'retry-structure-build') {
    ivV2EnsureStructureWorkspace({ forceRebuild: true });
    return;
  }

  if (action === 'refresh-structure-review') {
    ivV2EnsureStructureWorkspace({ forceRebuild: false });
    return;
  }

  if (action === 'load-evidence-context') {
    ivV2EnsureEvidenceContext(ivV2IssueById(button.dataset.issueId), { force: true });
    return;
  }

  if (action === 'submit-review-issue') {
    ivV2SubmitIssueResolution(button.dataset.issueId);
  }
}

function ivV2HandleEditorInputOrChange(event) {
  const target = event.target;
  const action = target.dataset.ivV2Action;
  if (!action) return;
  if (action.startsWith('review-')) {
    if (ivV2OperationBusy()) return;
    const issueId = target.dataset.issueId;
    if (action === 'review-filter') {
      ivV2State.reviewFilter = target.value || 'open';
      const selected = ivV2SyncSelectedIssue({ preserveAll: true });
      if (selected) ivV2EnsureIssueDraft(selected);
      ivV2RenderConfirmed();
      return;
    }
    if (!issueId) return;
    if (action === 'review-draft-resolution') {
      ivV2UpdateIssueDraft(issueId, { resolution: target.value || '', target_id: '', row_role: '', evidence_type: '' });
      ivV2RenderConfirmed();
      return;
    }
    if (action === 'review-draft-row-role') {
      ivV2UpdateIssueDraft(issueId, { row_role: target.value || '', target_id: '' });
      ivV2RenderConfirmed();
      return;
    }
    if (action === 'review-draft-target') {
      ivV2UpdateIssueDraft(issueId, { target_id: target.value || '' });
      ivV2EnsureEvidenceContext(ivV2IssueById(issueId));
      ivV2RenderConfirmed();
      return;
    }
    if (action === 'review-draft-evidence-type') {
      ivV2UpdateIssueDraft(issueId, { evidence_type: target.value || '' });
      ivV2RenderConfirmed();
      return;
    }
    if (action === 'review-draft-comment') {
      ivV2UpdateIssueDraft(issueId, { comment: target.value || '' });
      ivV2SyncCommentCounter(target);
      return;
    }
    return;
  }
  if (ivV2OperationBusy()) return;

  if (action === 'group-name') {
    const group = ivV2FindGroup(target.dataset.groupId);
    if (group) group.display_name = target.value;
    ivV2MarkDirty();
    return;
  }

  if (action === 'sheet-role') {
    const group = ivV2FindGroup(target.dataset.groupId);
    const sheet = group?.sheets.find(item => item.sheet_id === target.dataset.sheetId);
    if (!sheet) return;
    sheet.role = target.value;
    if (sheet.role !== 'record') sheet.recorder_label = '';
    ivV2PruneGroup(group);
    ivV2MarkDirty();
    ivV2RenderEditor();
    return;
  }

  if (action === 'sheet-recorder') {
    const group = ivV2FindGroup(target.dataset.groupId);
    const sheet = group?.sheets.find(item => item.sheet_id === target.dataset.sheetId);
    if (sheet) sheet.recorder_label = target.value;
    ivV2MarkDirty();
    return;
  }

  if (action === 'participant-label') {
    const { participant } = ivV2FindParticipant(target.dataset.groupId, target.dataset.participantId);
    if (participant) participant.participant_label = target.value;
    ivV2MarkDirty();
    return;
  }

  if (action === 'move-sheet') {
    if (!target.value) return;
    ivV2MoveSheet(target.dataset.sheetId, target.value);
    ivV2RenderEditor();
    return;
  }

  if (action === 'participant-add-column-select') {
    if (!target.value) return;
    const [sheetId, columnValue] = target.value.split(':');
    const { participant } = ivV2FindParticipant(target.dataset.groupId, target.dataset.participantId);
    if (!participant) return;
    if (participant.columns.some(column => column.sheet_id === sheetId)) {
      showToast('同一玩家在每个记录 Sheet 中只能绑定一列', 'info');
      target.value = '';
      return;
    }
    participant.columns.push({ sheet_id: sheetId, column: Number(columnValue) });
    target.value = '';
    ivV2MarkDirty();
    ivV2RenderEditor();
  }
}

function ivV2Reset() {
  ivV2InvalidateAsync();
  ivV2State.currentStep = 1;
  ivV2State.selectedFile = null;
  ivV2State.uploadAttemptId = '';
  ivV2State.importId = '';
  ivV2State.projectId = '';
  ivV2State.workbookRevisionId = '';
  ivV2State.status = 'idle';
  ivV2State.loadingMessage = '';
  ivV2State.requestBusy = false;
  ivV2State.saveBusy = false;
  ivV2State.confirmBusy = false;
  ivV2State.restoreBusy = false;
  ivV2State.buildBusy = false;
  ivV2State.reviewBusy = false;
  ivV2State.importData = null;
  ivV2State.mappingResponse = null;
  ivV2State.draft = null;
  ivV2State.sheetCatalog = {};
  ivV2State.statusNote = '';
  ivV2State.reviewFilter = 'open';
  ivV2ResetStructureWorkspace();
  ivV2ClearStatusError();
  ivV2ClearDirty();
  ivV2ClearFile();
  if (ivV2$('iv-v2-research-focus')) ivV2$('iv-v2-research-focus').value = '';
  if (ivV2$('iv-v2-contract-check')) ivV2$('iv-v2-contract-check').checked = false;
  ivV2SyncUploadButton();
  ivV2RenderEditor();
  ivV2RenderConfirmed();
  ivV2SetStep(1);
}

function ivV2Mount() {
  ivV2AllTrackButtons().forEach(button => {
    button.addEventListener('click', () => ivV2SetTrack(button.dataset.ivTrack || 'v1'));
  });

  document.querySelectorAll('[data-iv-v2-step]').forEach(button => {
    button.addEventListener('click', () => ivV2PreviewStep(Number(button.dataset.ivV2Step)));
  });

  ivV2$('iv-v2-upload-zone')?.addEventListener('click', () => ivV2$('iv-v2-file-input')?.click());
  ivV2$('iv-v2-upload-zone')?.addEventListener('keydown', event => {
    if (!['Enter', ' '].includes(event.key)) return;
    event.preventDefault();
    ivV2$('iv-v2-file-input')?.click();
  });
  ivV2$('iv-v2-file-input')?.addEventListener('change', () => ivV2SelectFile(ivV2$('iv-v2-file-input')?.files?.[0]));
  ivV2$('iv-v2-upload-zone')?.addEventListener('dragover', event => {
    event.preventDefault();
    ivV2$('iv-v2-upload-zone')?.classList.add('drag-over');
  });
  ivV2$('iv-v2-upload-zone')?.addEventListener('dragleave', () => {
    ivV2$('iv-v2-upload-zone')?.classList.remove('drag-over');
  });
  ivV2$('iv-v2-upload-zone')?.addEventListener('drop', event => {
    event.preventDefault();
    ivV2$('iv-v2-upload-zone')?.classList.remove('drag-over');
    ivV2SelectFile(event.dataTransfer?.files?.[0]);
  });
  ivV2$('iv-v2-remove-file')?.addEventListener('click', event => {
    event.stopPropagation();
    ivV2ClearFile();
  });

  ivV2$('iv-v2-contract-check')?.addEventListener('change', ivV2SyncUploadButton);
  ivV2$('iv-v2-research-focus')?.addEventListener('input', () => {
    ivV2State.idempotencyKey = '';
    ivV2State.idempotencyFingerprint = '';
  });
  ivV2$('iv-v2-start-import')?.addEventListener('click', ivV2StartImport);
  ivV2$('iv-v2-refresh')?.addEventListener('click', () => {
    if (!ivV2State.importId || ivV2OperationBusy()) return;
    if (ivV2State.draftDirty && !confirm('刷新会丢弃当前未保存改动，确定继续吗？')) return;
    ivV2LoadImportBundle(ivV2State.importId, { keepStep: true }).catch(error => {
      ivV2ShowToastFromError(error, '刷新失败');
    });
  });
  ivV2$('iv-v2-add-group')?.addEventListener('click', () => {
    if (ivV2OperationBusy()) return;
    ivV2CreateGroup();
    ivV2MarkDirty();
    ivV2RenderEditor();
  });
  ivV2$('iv-v2-save-draft')?.addEventListener('click', ivV2SaveDraft);
  ivV2$('iv-v2-confirm-mapping')?.addEventListener('click', ivV2ConfirmMapping);
  ivV2$('iv-v2-undo')?.addEventListener('click', () => {
    const entry = ivV2UndoTarget();
    ivV2Restore(entry, 'undo', entry ? `撤销到第 ${entry.revision_number} 版` : '撤销');
  });
  ivV2$('iv-v2-redo')?.addEventListener('click', () => {
    const entry = ivV2RedoTarget();
    ivV2Restore(entry, 'redo', entry ? `重做到第 ${entry.revision_number} 版` : '重做');
  });
  ivV2$('iv-v2-editor-shell')?.addEventListener('click', ivV2HandleEditorClick);
  ivV2$('iv-v2-editor-shell')?.addEventListener('change', ivV2HandleEditorInputOrChange);
  ivV2$('iv-v2-editor-shell')?.addEventListener('input', event => {
    if (event.target.tagName === 'SELECT') return;
    ivV2HandleEditorInputOrChange(event);
  });
  ivV2$('iv-v2-confirmed-shell')?.addEventListener('click', ivV2HandleEditorClick);
  ivV2$('iv-v2-confirmed-shell')?.addEventListener('change', ivV2HandleEditorInputOrChange);
  ivV2$('iv-v2-confirmed-shell')?.addEventListener('input', event => {
    if (event.target.tagName === 'SELECT') return;
    ivV2HandleEditorInputOrChange(event);
  });
  ivV2$('iv-v2-back-to-editor')?.addEventListener('click', () => {
    if (ivV2OperationBusy()) return;
    ivV2State.currentStep = 2;
    ivV2SetStep(2);
  });
  ivV2$('iv-v2-start-over')?.addEventListener('click', () => {
    if (ivV2OperationBusy()) return;
    ivV2Reset();
  });

  ivV2SyncTrackToggle();
  ivV2SyncUploadButton();
  ivV2RenderUploadStatus();
  ivV2RenderEditor();
  ivV2RenderConfirmed();
}

window.interviewV2Feature = {
  isTrackActive: ivV2IsActive,
  syncTrackToggle: ivV2SyncTrackToggle,
  goStep: () => ivV2SetStep(ivV2State.currentStep),
  previewStep: ivV2PreviewStep,
  renderStepBars: ivV2RenderStepBars,
  reset: ivV2Reset,
};

ivV2Mount();
