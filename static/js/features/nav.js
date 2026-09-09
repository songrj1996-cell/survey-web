// ============================================================
// Restart
// ============================================================

$('btn-restart').addEventListener('click', () => {
  if (typeof surveyEntryBusy === 'function' && surveyEntryBusy()) {
    showToast('当前上传或分析设置正在处理，请稍候', 'info');
    return;
  }
  if (reportInteractionBusy()) {
    showToast('当前报告操作尚未完成，请稍候再重新开始', 'info', 5000);
    return;
  }
  if (!confirm('确定要重新开始吗？当前会话数据将被清除。')) return;
  if (currentMode === 'comment') {
    cmReset();
    showToast('已重置，请重新上传文件', 'info');
    return;
  }
  if (currentMode === 'interview') {
    ivReset();
    showToast('已重置，请重新上传文件', 'info');
    return;
  }
  if (typeof saveContextDraft === 'function') saveContextDraft();
  state.sessionId = null;
  state.columns = null;
  state.planData = null;
  state.reportMd = null;
  state.qaLoading = false;
  state.reportVersionLoading = false;
  state.viewMode = 'session';
  state.historyId = null;
  state.sessionReport = {
    id: null,
    reportMd: null,
    title: '',
    reportNo: '',
    version: null,
    versions: [],
    activeVersion: null,
    selectedVersion: null,
    nextVersion: null,
    maxVersions: 5,
    canGenerateVersion: true,
    versionInstructions: {},
    qaHtml: '',
    qaMessages: [],
    feishuLinkHtml: '',
    running: false,
    stream: '',
    pendingVersionRequest: null,
    generatingVersion: null,
    lastVersionInstruction: '',
  };
  state.historyReport = {
    id: null,
    reportMd: null,
    title: '',
    reportNo: '',
    version: null,
    versions: [],
    activeVersion: null,
    selectedVersion: null,
    nextVersion: null,
    maxVersions: 5,
    canGenerateVersion: false,
    analystConvId: null,
    qaHtml: '',
    qaMessages: [],
    feishuLinkHtml: '',
    planData: null,
  };
  resetUploadZone();
  clearPlanInput();
  $('qa-input').disabled = false;
  $('btn-qa-send').disabled = false;
  // 回到统一上传入口
  state.mode = null;
  goStep(1);
  showToast('已重置，请重新上传文件', 'info');
});

// Questionnaire imports are managed by survey-entry.js.

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

let currentMode = 'survey'; // 'survey' | 'interview' | 'annotate' | 'comment'

function switchMode(mode) {
  if (currentMode === 'survey' && mode !== 'survey' && reportInteractionBusy()) {
    showToast('当前报告操作尚未完成，请稍候再切换功能', 'info', 5000);
    return;
  }
  currentMode = mode;
  const isSurvey = mode === 'survey';
  const isInterview = mode === 'interview';
  const isAnnotate = mode === 'annotate';
  const isComment = mode === 'comment';

  // 一级导航激活状态
  $('nav-survey').classList.toggle('nav-item--active', isSurvey);
  $('nav-survey').classList.toggle('nav-item--expanded', isSurvey);
  $('nav-interview').classList.toggle('nav-item--active', isInterview);
  $('nav-interview').classList.toggle('nav-item--expanded', isInterview);
  $('nav-annotate').classList.toggle('nav-item--active', isAnnotate);
  $('nav-annotate').classList.toggle('nav-item--expanded', isAnnotate);
  $('nav-comment').classList.toggle('nav-item--active', isComment);
  $('nav-settings').classList.remove('nav-item--active');

  // 历史记录是全局入口，不随当前功能模块切换
  $('btn-open-history').style.display = '';

  surveyPanels.forEach(p => p.classList.add('panel--hidden'));
  ivPanels.forEach(p => p && p.classList.add('panel--hidden'));
  annPanels.forEach(p => p.classList.add('panel--hidden'));
  cmPanels.forEach(p => p && p.classList.add('panel--hidden'));
  if (isSurvey) {
    goStep(state.currentStep);
  } else if (isInterview) {
    ivGoStep(ivState.currentStep);
  } else if (isAnnotate) {
    annGoStep(annState.currentStep);
  } else {
    cmGoStep(cmState.currentStep);
  }
}

// 一级导航点击
$('nav-header-survey').addEventListener('click', () => switchMode('survey'));
$('nav-header-interview').addEventListener('click', () => switchMode('interview'));
$('nav-header-annotate').addEventListener('click', () => switchMode('annotate'));
$('nav-header-comment').addEventListener('click', () => switchMode('comment'));

// 设置入口
$('nav-header-settings').addEventListener('click', () => {
  $('nav-settings').classList.add('nav-item--active');
  $('nav-survey').classList.remove('nav-item--active');
  $('nav-interview').classList.remove('nav-item--active');
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
