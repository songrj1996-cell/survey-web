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
  pendingPoll: 0,
  requestToken: 0,
  importData: null,
  mappingResponse: null,
  draft: null,
  sheetCatalog: {},
  statusNote: '',
  draftDirty: false,
  idempotencyKey: '',
  idempotencyFingerprint: '',
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
}

function ivV2OperationBusy() {
  return Boolean(
    ivV2State.requestBusy
    || ivV2State.saveBusy
    || ivV2State.restoreBusy
    || ivV2State.confirmBusy
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
  if (status === 'GROUP_MAPPING_CONFIRMED' || status === 'ACCEPTED') return 'success';
  if (status === 'REJECTED') return 'danger';
  if (status === 'PRECHECKING' || status === 'loading') return 'info';
  return 'warning';
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
    const confirmed = proposalData.status === 'GROUP_MAPPING_CONFIRMED';
    ivV2ClearDirty(confirmed ? '当前处于已确认版本' : '已加载最新映射版本');
    if (!keepStep) ivV2SetStep(confirmed ? 3 : 2);
    ivV2RenderEditor();
    ivV2RenderConfirmed();
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

function ivV2RenderConfirmed() {
  const meta = ivV2$('iv-v2-confirmed-meta');
  const preview = ivV2$('iv-v2-confirmed-preview');
  const history = ivV2$('iv-v2-confirmed-history');
  if (!meta || !preview || !history) return;

  const response = ivV2State.mappingResponse;
  if (!response) {
    meta.innerHTML = '<div class="iv-v2-empty">确认完成后，这里会显示最终摘要。</div>';
    preview.innerHTML = '';
    history.innerHTML = '';
    return;
  }

  const previewSource = response.final_participant_preview?.participants?.length
    ? response.final_participant_preview
    : (response.proposals?.final_participant_preview || {});
  const participants = previewSource.participants || [];

  meta.innerHTML = `
    <div class="iv-v2-status-grid">
      <div class="iv-v2-status-card iv-v2-status-card--success">
        <strong>${ivV2Esc(response.status || 'GROUP_MAPPING_CONFIRMED')}</strong>
        <p>版本 ${ivV2Esc(response.revision_number || '--')} · SHA ${ivV2Esc((response.mapping_sha256 || '').slice(0, 12) || '--')}</p>
      </div>
      <div class="iv-v2-status-card">
        <strong>${ivV2Esc(participants.length)} 名玩家</strong>
        <p>确认后不会触发旧版报告生成流</p>
      </div>
      <div class="iv-v2-status-card">
        <strong>${ivV2Esc(ivV2FormatTime(response.history?.at(-1)?.confirmed_at || ivV2State.importData?.updated_at || ''))}</strong>
        <p>可返回编辑器继续修改并再次保存</p>
      </div>
    </div>
  `;

  preview.innerHTML = participants.length ? participants.map(item => `
    <div class="iv-v2-preview-row">
      <strong>${ivV2Esc(item.participant_label || '未命名玩家')}</strong>
      <span>${ivV2Esc(item.group_display_name || '未分组')}</span>
      <p>${(item.sources || []).map(source => ivV2Esc(`${ivV2SheetName(source.sheet_id)} · ${source.column_letter || source.column_index || ''}`)).join(' / ') || '无来源列'}</p>
    </div>
  `).join('') : '<div class="iv-v2-empty">当前没有最终玩家预览。</div>';
  history.innerHTML = ivV2HistoryHtml();
  const operationBusy = ivV2OperationBusy();
  const backButton = ivV2$('iv-v2-back-to-editor');
  const startOverButton = ivV2$('iv-v2-start-over');
  if (backButton) backButton.disabled = operationBusy;
  if (startOverButton) startOverButton.disabled = operationBusy;
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
    ivV2ClearDirty('已停在 GROUP_MAPPING_CONFIRMED 检查点');
    ivV2State.currentStep = 3;
    ivV2SetStep(3);
    ivV2RenderEditor();
    ivV2RenderConfirmed();
    showToast('分组确认完成，已停在检查点', 'success');
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
  }
}

function ivV2HandleEditorInputOrChange(event) {
  if (ivV2OperationBusy()) return;
  const target = event.target;
  const action = target.dataset.ivV2Action;
  if (!action) return;

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
  ivV2State.importData = null;
  ivV2State.mappingResponse = null;
  ivV2State.draft = null;
  ivV2State.sheetCatalog = {};
  ivV2State.statusNote = '';
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
