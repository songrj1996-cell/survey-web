// ============================================================
// Restart
// ============================================================

$('btn-restart').addEventListener('click', () => {
  if (!confirm('确定要重新开始吗？当前会话数据将被清除。')) return;
  if (typeof saveContextDraft === 'function') saveContextDraft();
  state.sessionId = null;
  state.columns = null;
  state.planData = null;
  state.reportMd = null;
  state.qaLoading = false;
  state.viewMode = 'session';
  state.historyId = null;
  state.sessionReport = {
    reportMd: null,
    title: '',
    reportNo: '',
    qaHtml: '',
    qaMessages: [],
    feishuLinkHtml: '',
    running: false,
    stream: '',
  };
  resetUploadZone();
  fileInput.value = '';
  resetCrosstabUploader();
  clearPlanInput();
  $('qa-input').disabled = false;
  $('btn-qa-send').disabled = false;
  // 回到分析类型选择层
  state.mode = null;
  $('analysis-type-picker').style.display = '';
  $('upload-area').style.display = 'none';
  const ctArea = $('crosstab-upload-area');
  if (ctArea) ctArea.style.display = 'none';
  goStep(1);
  showToast('已重置，请重新上传文件', 'info');
});

// ── 分析类型选择器 ──
$('btn-qual-enter').addEventListener('click', () => {
  state.mode = null;
  $('analysis-type-picker').style.display = 'none';
  $('upload-area').style.display = '';
  // 每次进入上传区都（重新）加载说明文案，确保显示
  fetch('/api/upload-guide')
    .then(r => r.json())
    .then(({ content }) => {
      const el = $('upload-guide');
      if (el && content) el.innerHTML = marked.parse(content);
    })
    .catch(() => { });
});

// ── 定量分析（跑数表模式）：三文件上传 ──
const CT_FILE_SLOTS = [
  { key: 'survey', inputId: 'ct-survey', label: '问卷文件' },
  { key: 'data', inputId: 'ct-data', label: '回答数据' },
  { key: 'crosstab', inputId: 'ct-crosstab', label: '跑数表' },
];

function getCrosstabFile(slot) {
  const input = $(slot.inputId);
  return input ? input.files[0] : null;
}

function isSupportedCrosstabFile(file) {
  return /\.(csv|xlsx|xls)$/i.test(file.name || '');
}

function updateCrosstabFileCard(slot) {
  const file = getCrosstabFile(slot);
  const card = document.querySelector(`[data-ct-slot="${slot.key}"]`);
  const nameEl = document.querySelector(`[data-ct-file-name="${slot.key}"]`);
  if (!card || !nameEl) return;
  card.classList.toggle('crosstab-file-card--selected', !!file);
  nameEl.textContent = file ? file.name : '未选择文件';
}

function updateCrosstabUploadState() {
  CT_FILE_SLOTS.forEach(updateCrosstabFileCard);
  const btn = $('btn-ct-upload');
  if (!btn) return;
  const ready = CT_FILE_SLOTS.every(slot => !!getCrosstabFile(slot));
  if (!btn.dataset.loading) btn.disabled = !ready;
}

function resetCrosstabUploader() {
  CT_FILE_SLOTS.forEach(slot => {
    const input = $(slot.inputId);
    if (input) input.value = '';
  });
  const uploader = document.querySelector('.crosstab-uploader');
  if (uploader) uploader.classList.remove('crosstab-uploader--loading');
  const btn = $('btn-ct-upload');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '上传并解析跑数表';
    delete btn.dataset.loading;
  }
  updateCrosstabUploadState();
}

function setCrosstabUploadLoading(loading) {
  const btn = $('btn-ct-upload');
  const uploader = document.querySelector('.crosstab-uploader');
  if (uploader) uploader.classList.toggle('crosstab-uploader--loading', loading);
  if (!btn) return;
  if (loading) {
    btn.dataset.loading = '1';
    btn.disabled = true;
    btn.textContent = '正在上传与解析...';
  } else {
    delete btn.dataset.loading;
    btn.textContent = '上传并解析跑数表';
    updateCrosstabUploadState();
  }
}

function assignCrosstabFile(slot, file) {
  if (!file) return;
  if (!isSupportedCrosstabFile(file)) {
    showToast(`${slot.label}仅支持 CSV / Excel 文件`, 'error');
    return;
  }
  const input = $(slot.inputId);
  if (!input) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  input.files = transfer.files;
  updateCrosstabUploadState();
}

CT_FILE_SLOTS.forEach(slot => {
  const input = $(slot.inputId);
  const card = document.querySelector(`[data-ct-slot="${slot.key}"]`);
  if (!input || !card) return;
  input.addEventListener('change', () => {
    const file = getCrosstabFile(slot);
    if (file && !isSupportedCrosstabFile(file)) {
      input.value = '';
      showToast(`${slot.label}仅支持 CSV / Excel 文件`, 'error');
    }
    updateCrosstabUploadState();
  });
  card.addEventListener('dragover', e => {
    e.preventDefault();
    card.classList.add('crosstab-file-card--drag');
  });
  card.addEventListener('dragleave', () => card.classList.remove('crosstab-file-card--drag'));
  card.addEventListener('drop', e => {
    e.preventDefault();
    card.classList.remove('crosstab-file-card--drag');
    assignCrosstabFile(slot, e.dataTransfer.files[0]);
  });
});

$('btn-ct-back').addEventListener('click', () => {
  state.mode = null;
  resetCrosstabUploader();
  $('crosstab-upload-area').style.display = 'none';
  $('analysis-type-picker').style.display = '';
});

$('btn-quant-enter').addEventListener('click', () => {
  state.mode = 'crosstab';
  $('analysis-type-picker').style.display = 'none';
  $('crosstab-upload-area').style.display = '';
  updateCrosstabUploadState();
});

$('btn-ct-upload').addEventListener('click', async () => {
  const sf = $('ct-survey').files[0];
  const df = $('ct-data').files[0];
  const cf = $('ct-crosstab').files[0];
  if (!sf || !df || !cf) { showToast('请把问卷、回答数据、跑数表三个文件都选上', 'error'); return; }
  const MAX = 50 * 1024 * 1024;
  for (const f of [sf, df, cf]) {
    if (f.size > MAX) { showToast(`文件 ${f.name} 超过 50MB 上限`, 'error'); return; }
  }
  setCrosstabUploadLoading(true);
  const fd = new FormData();
  fd.append('survey_file', sf);
  fd.append('data_file', df);
  fd.append('crosstab_file', cf);
  try {
    const resp = await fetch('/api/upload/crosstab', { method: 'POST', body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '上传失败');
    state.sessionId = data.session_id;
    state.mode = 'crosstab';
    state.viewMode = 'session';
    state.historyId = null;
    clearPlanInput();
    state.sessionReport = {
      reportMd: null, title: '', reportNo: '', qaHtml: '',
      qaMessages: [], feishuLinkHtml: '', running: false, stream: '',
    };
    renderPreview(data);
    const segInfo = (data.crosstab_segments || []).join('、');
    showToast(`跑数表解析成功：${data.crosstab_questions} 道题、分段[${segInfo}]；回答 ${data.total_rows} 行`, 'success');
    // 跑数表模式：跳过题型确认，直接进方案确认
    startPlan();
  } catch (e) {
    showToast(`上传失败：${e.message}`, 'error');
    setCrosstabUploadLoading(false);
  }
});

// ── UI 文案初始化 ──
async function initUiTexts() {
  try {
    const resp = await fetch('/api/ui-texts');
    if (!resp.ok) return;
    const texts = await resp.json();
    Object.entries(texts).forEach(([key, item]) => {
      const el = document.querySelector(`[data-uitext="${key}"]`);
      if (el) el.textContent = item.current;
    });
  } catch { }
}

// ── Init ──
goStep(1);
refreshFeishuStatus();
initUiTexts();

// ============================================================
// 模式切换（问卷分析 ↔ 数据标注）
// ============================================================

const surveyPanels = panels;            // panel-1 ~ panel-5
const annPanelIds = [1, 2, 3, 4, 5, 6];
const annPanels = annPanelIds.map(n => $(`ann-panel-${n}`));

const cmPanels = [1, 2, 3].map(n => $(`cm-panel-${n}`));

let currentMode = 'survey'; // 'survey' | 'annotate' | 'comment'

function switchMode(mode) {
  currentMode = mode;
  const isSurvey = mode === 'survey';
  const isAnnotate = mode === 'annotate';
  const isComment = mode === 'comment';

  // 一级导航激活状态
  $('nav-survey').classList.toggle('nav-item--active', isSurvey);
  $('nav-survey').classList.toggle('nav-item--expanded', isSurvey);
  $('nav-annotate').classList.toggle('nav-item--active', isAnnotate);
  $('nav-annotate').classList.toggle('nav-item--expanded', isAnnotate);
  $('nav-comment').classList.toggle('nav-item--active', isComment);
  $('nav-settings').classList.remove('nav-item--active');

  // 历史记录是全局入口，不随当前功能模块切换
  $('btn-open-history').style.display = '';

  surveyPanels.forEach(p => p.classList.add('panel--hidden'));
  annPanels.forEach(p => p.classList.add('panel--hidden'));
  cmPanels.forEach(p => p && p.classList.add('panel--hidden'));
  if (isSurvey) {
    goStep(state.currentStep);
  } else if (isAnnotate) {
    annGoStep(annState.currentStep);
  } else {
    cmGoStep(cmState.currentStep);
  }
}

// 一级导航点击
$('nav-header-survey').addEventListener('click', () => switchMode('survey'));
$('nav-header-annotate').addEventListener('click', () => switchMode('annotate'));
$('nav-header-comment').addEventListener('click', () => switchMode('comment'));

// 设置入口
$('nav-header-settings').addEventListener('click', () => {
  $('nav-settings').classList.add('nav-item--active');
  $('nav-survey').classList.remove('nav-item--active');
  $('nav-annotate').classList.remove('nav-item--active');
  openDrawer('settings-drawer');
  loadActiveSettingsTab();
});

// 步骤条点击（问卷分析和数据标注）—— 已完成步骤可回看
document.addEventListener('click', e => {
  const btn = e.target.closest('[data-survey-step]');
  if (btn) {
    const n = +btn.dataset.surveyStep;
    if (currentMode === 'survey' && n <= state.currentStep) setViewStep(n);
    return;
  }
  // 数据标注步骤条（标注流程不支持回看，忽略点击）
});

// QA 收起/展开按钮
function updateQAPanelButtons() {
  const side = $('qa-side');
  const wideBtn = $('btn-qa-wide');
  const collapseBtn = $('btn-qa-collapse');
  if (!side) return;
  if (wideBtn) {
    wideBtn.title = side.classList.contains('qa-side--wide') ? '缩小追问面板' : '展开追问面板';
  }
  if (collapseBtn) {
    collapseBtn.title = side.classList.contains('qa-side--collapsed') ? '展开追问面板' : '收起追问面板';
  }
}

const btnQaWide = $('btn-qa-wide');
if (btnQaWide) {
  btnQaWide.addEventListener('click', () => {
    const side = $('qa-side');
    if (!side) return;
    side.classList.remove('qa-side--collapsed');
    side.classList.toggle('qa-side--wide');
    updateQAPanelButtons();
  });
}

const btnQaCollapse = $('btn-qa-collapse');
if (btnQaCollapse) {
  btnQaCollapse.addEventListener('click', () => {
    const side = $('qa-side');
    if (!side) return;
    side.classList.remove('qa-side--wide');
    side.classList.toggle('qa-side--collapsed');
    updateQAPanelButtons();
  });
}
updateQAPanelButtons();
