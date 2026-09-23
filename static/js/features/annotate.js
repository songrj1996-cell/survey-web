// ============================================================
// 数据标注状态机
// ============================================================

const annState = {
  sessionId: null,
  completion: null,
  historySaved: null,
  partialRetryRunning: false,
  partialRetrySyncing: false,
  partialRetrySyncVersion: 0,
  partialRetrySyncedCompletion: null,
  currentStep: 1,
  headers: [],
  headersZh: [],
  idCol: 1,
  openTextCols: [],
  matrixColIdxs: new Set(),
  emptyColIdxs: new Set(),
  tasks: { ai_detect: false, quality: false },
  aiResults: [],
  highProbResults: [],
  reviewAiResults: [],
  aiReviewThreshold: 60,
  aiHighThreshold: 80,
  aiConfirmationComplete: false,
  confirmedAiIds: new Set(),
  aiReviewedIds: new Set(),
  aiReviewIndex: 0,
  aiQuestionIndex: 0,
  aiConfirmLayout: 'split',
  qualityResults: [],
  qualityCount: 0,
  qualityDurationSeconds: null,
  qualityReviewPlayerId: null,
  qualityQuestionIndex: 0,
  qualityReviewLayout: 'split',
  qualityReviewSaving: false,
  qualityReviewRunning: false,
  reviewFilename: '',
  missingAiIds: [],
  missingQualityIds: [],
  missingOverallIds: [],
  missingTranslationIds: [],
};

function annGoStep(n) {
  annState.currentStep = n;
  annPanels.forEach((p, i) => p.classList.toggle('panel--hidden', i + 1 !== n));
  // 更新数据标注步骤条状态
  document.querySelectorAll('[data-ann-step]').forEach(btn => {
    const i = +btn.dataset.annStep;
    btn.classList.remove('step-bar__item--active', 'step-bar__item--done');
    if (i < n) btn.classList.add('step-bar__item--done');
    else if (i === n) btn.classList.add('step-bar__item--active');
    btn.disabled = true; // 标注流程不支持回看
  });
  document.querySelector('.main').scrollTo({ top: 0, behavior: 'smooth' });
}

// ── ANN STEP 1: 上传 ────────────────────────────────────────

const annUploadZone = $('ann-upload-zone');
const annFileInput = $('ann-file-input');

annUploadZone.addEventListener('click', () => annFileInput.click());
annUploadZone.addEventListener('dragover', e => { e.preventDefault(); annUploadZone.classList.add('drag-over'); });
annUploadZone.addEventListener('dragleave', () => annUploadZone.classList.remove('drag-over'));
annUploadZone.addEventListener('drop', e => {
  e.preventDefault();
  annUploadZone.classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) annHandleUpload(file);
});
annFileInput.addEventListener('change', () => {
  if (annFileInput.files[0]) annHandleUpload(annFileInput.files[0]);
});

async function annHandleUpload(file) {
  const MAX = 50 * 1024 * 1024;
  if (file.size > MAX) { showToast('文件超过 50MB 上限', 'error'); return; }
  annUploadZone.innerHTML = `
    <div class="upload-zone__icon"><div class="spinner" style="width:40px;height:40px;border-width:3px"></div></div>
    <div class="upload-zone__text"><span class="upload-zone__primary">正在上传 ${esc(file.name)}…</span></div>`;

  const fd = new FormData();
  fd.append('file', file);
  try {
    const resp = await fetch('/api/annotate/upload', { method: 'POST', body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '上传失败');

    annState.sessionId = data.session_id;
    window.AnnotateReview?.reset();
    annState.reviewFilename = data.filename || file.name;
    annState.headers = data.headers;
    annState.headersZh = data.headers_zh || data.headers;
    annState.idCol = data.id_col;
    annState.openTextCols = data.open_text_cols;
    annState.matrixColIdxs = new Set(data.matrix_col_idxs || []);
    annState.emptyColIdxs = new Set(data.empty_col_idxs || []);

    $('ann-preview-meta').textContent =
      `${data.filename} · ${data.total_rows} 行数据 · ${data.headers.length} 列`;

    annRenderColConfig(
      data.headers,
      data.id_col,
      data.open_text_cols,
      data.headers_zh || data.headers,
      new Set(data.matrix_col_idxs || []),
      new Set(data.empty_col_idxs || []),
    );
    annGoStep(2);
    if (data.header_translation_warning) {
      showToast(data.header_translation_warning, 'error');
    } else {
      showToast(`成功读取 ${data.total_rows} 行数据`, 'success');
    }
  } catch (e) {
    showToast(`上传失败：${e.message}`, 'error');
    annResetUploadZone();
  }
}

function annResetUploadZone() {
  annUploadZone.innerHTML = `
    <div class="upload-zone__icon">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
        <polyline points="17 8 12 3 7 8"/>
        <line x1="12" y1="3" x2="12" y2="15"/>
      </svg>
    </div>
    <div class="upload-zone__text">
      <span class="upload-zone__primary">拖放文件到这里，或点击选择</span>
      <span class="upload-zone__secondary">支持 CSV / Excel（最大 50MB）</span>
    </div>`;
}

// ── ANN STEP 2: 列确认 + 任务 ──────────────────────────────

function annRenderColConfig(headers, idCol, openTextCols, headersZh, matrixIdxs, emptyColIdxs) {
  const zh = headersZh || headers;
  const otSet = new Set(openTextCols);
  const mxSet = matrixIdxs || new Set();
  const emptySet = emptyColIdxs || new Set();
  const container = $('ann-col-config');

  // ID 列选择（显示中文名，排除矩阵子列）
  const idOpts = headers.map((h, i) => {
    if (emptySet.has(i)) return '';
    return `<option value="${i}" ${i === idCol ? 'selected' : ''}>${i}: ${esc(zh[i] || h)}</option>`;
  }).join('');

  // 主观题列多选——每行一题，矩阵子列隐藏
  const otRows = headers.map((h, i) => {
    if (mxSet.has(i) || emptySet.has(i)) return '';
    const zhName = zh[i] || h;
    const hasDiff = zhName !== h;
    return `
    <label class="ann-col-check-item ann-col-check-item--full">
      <input type="checkbox" class="ann-ot-check" value="${i}" ${otSet.has(i) ? 'checked' : ''} />
      <span class="ann-col-idx">${i}</span>
      <span class="ann-col-name-wrap">
        <span class="ann-col-zh">${esc(zhName)}</span>
        ${hasDiff ? `<span class="ann-col-original">${esc(h)}</span>` : ''}
      </span>
    </label>`;
  }).join('');

  container.innerHTML = `
    <div class="ann-col-row">
      <label class="ann-label">玩家唯一 ID 列</label>
      <select class="type-select" id="ann-id-col-sel">${idOpts}</select>
    </div>
    <div class="ann-col-row" style="flex-direction:column;align-items:flex-start">
      <label class="ann-label">主观题列（可多选）</label>
      <div class="ann-col-check-list ann-col-check-list--full">${otRows}</div>
    </div>`;

  // 更新 annState
  $('ann-id-col-sel').addEventListener('change', e => {
    annState.idCol = +e.target.value;
  });
  container.querySelectorAll('.ann-ot-check').forEach(cb => {
    cb.addEventListener('change', () => {
      annState.openTextCols = [...container.querySelectorAll('.ann-ot-check:checked')].map(c => +c.value);
      annUpdateStartBtn();
    });
  });
  annState.idCol = idCol;
  annState.openTextCols = [...otSet];
  annUpdateStartBtn();
}

// 任务勾选
function annArrangeStep2Layout() {
  const panel = $('ann-panel-2');
  const colConfig = $('ann-col-config');
  const tasks = $('ann-tasks');
  const background = $('ann-background-block');
  const actions = panel ? panel.querySelector('.col-confirm-actions') : null;
  if (!panel || !colConfig || !tasks || !background || !actions) return;
  if (!tasks.querySelector('.ann-task-grid')) {
    const grid = document.createElement('div');
    grid.className = 'ann-task-grid';
    [...tasks.querySelectorAll(':scope > .ann-task-option')].forEach(option => grid.appendChild(option));
    tasks.appendChild(grid);
  }
  colConfig.insertAdjacentElement('afterend', tasks);
  tasks.insertAdjacentElement('afterend', background);
  background.insertAdjacentElement('afterend', actions);
  const bgInput = $('ann-background');
  if (bgInput) {
    bgInput.classList.add('ann-background-textarea');
    bgInput.rows = Math.max(bgInput.rows || 0, 5);
  }
  tasks.querySelectorAll('.ann-task-option').forEach(option => {
    const input = option.querySelector('input[type="checkbox"]');
    if (!input || option.querySelector('.ann-task-check')) return;
    const check = document.createElement('span');
    check.className = 'ann-task-check';
    check.setAttribute('aria-hidden', 'true');
    input.insertAdjacentElement('afterend', check);
  });
}

function annSyncTaskCards() {
  document.querySelectorAll('.ann-task-option').forEach(option => {
    const input = option.querySelector('input[type="checkbox"]');
    option.classList.toggle('ann-task-option--active', !!input?.checked);
  });
}

['task-ai-detect', 'task-quality'].forEach(id => {
  $(id).addEventListener('change', () => {
    annState.tasks.ai_detect = $('task-ai-detect').checked;
    annState.tasks.quality = $('task-quality').checked;
    $('ann-background-block').style.display = (annState.tasks.ai_detect || annState.tasks.quality) ? '' : 'none';
    annSyncTaskCards();
    annUpdateStartBtn();
  });
});

annArrangeStep2Layout();
annSyncTaskCards();

function annUpdateStartBtn() {
  const hasTask = annState.tasks.ai_detect || annState.tasks.quality;
  const hasCols = annState.openTextCols.length > 0;
  $('ann-btn-start').disabled = !(hasTask && hasCols);
}

$('ann-btn-start').addEventListener('click', annStartAnnotation);

async function annStartAnnotation() {
  $('ann-btn-start').disabled = true;
  annState.missingAiIds = [];
  annState.missingQualityIds = [];
  annState.missingTranslationIds = [];
  $('ann-btn-download').disabled = true;
  // 读取最新 id_col
  const idColSel = $('ann-id-col-sel');
  if (idColSel) annState.idCol = +idColSel.value;

  try {
    const resp = await fetch(`/api/annotate/${annState.sessionId}/confirm-columns`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id_col: annState.idCol,
        open_text_cols: annState.openTextCols,
        tasks: annState.tasks,
        background: ($('ann-background').value || '').trim(),
      }),
    });
    if (!resp.ok) {
      const d = await resp.json();
      throw new Error(d.detail || '保存失败');
    }
    annState.aiResults = [];
    annState.highProbResults = [];
    annState.reviewAiResults = [];
    annState.aiConfirmationComplete = false;
    annState.confirmedAiIds = new Set();
    annState.aiReviewedIds = new Set();
    annState.aiReviewIndex = 0;
    annState.aiQuestionIndex = 0;
    annState.aiConfirmLayout = 'split';
    annState.qualityResults = [];
    annState.qualityCount = 0;
    annState.qualityDurationSeconds = null;
    annState.missingQualityIds = [];
    annState.missingOverallIds = [];
    annState.missingTranslationIds = [];

    if (annState.tasks.ai_detect) {
      annGoStep(3);
      await annRunAiDetect();
    } else if (annState.tasks.quality) {
      annGoStep(5);
      await annRunQuality();
    }
  } catch (e) {
    showToast(`启动失败：${e.message}`, 'error');
    $('ann-btn-start').disabled = false;
  }
}

// ── ANN STEP 3: AI 检测 ────────────────────────────────────

async function annRunAiDetect(options = {}) {
  const bar = $('ann-ai-progress-bar');
  const msg = $('ann-ai-progress-msg');
  const warnLog = $('ann-ai-warn-log');
  let backBtn = $('ann-btn-ai-back');
  if (!backBtn) {
    backBtn = document.createElement('button');
    backBtn.id = 'ann-btn-ai-back';
    backBtn.className = 'btn btn--ghost';
    backBtn.textContent = '重试 AI 识别';
    warnLog.insertAdjacentElement('afterend', backBtn);
  }
  backBtn.style.display = 'none';
  backBtn.onclick = () => annRunAiDetect();
  const appendAiLog = (text, type = 'warn') => {
    const div = document.createElement('div');
    div.className = `ann-warn-item ann-warn-item--${type}`;
    div.textContent = text;
    warnLog.appendChild(div);
  };
  bar.style.width = '0%';
  msg.textContent = '正在连接…';
  warnLog.innerHTML = '';
  annState.missingTranslationIds = [];
  const diagnostics = [];

  try {
    await consumeSSEGet(annRetryUrl("run-ai-detect", options.retryIds), ev => {
      if (ev.type === 'started') {
        bar.style.width = '2%';
        msg.textContent = ev.msg || `已连接，准备分析 ${ev.rows || 0} 行，约 ${ev.total_batches || 0} 批`;
      }
      if (ev.type === 'batch_started') {
        const pct = ev.total > 0 ? Math.round((ev.done / ev.total) * 100) : 0;
        bar.style.width = `${Math.max(3, pct)}%`;
        msg.textContent = ev.msg || `正在分析第 ${ev.batch || 1}/${ev.total || 1} 批`;
      }
      if (ev.type === 'dify_waiting') {
        msg.textContent = ev.msg || '正在等待 AI 返回，请勿关闭页面';
      }
      if (ev.type === 'dify_done') {
        diagnostics.push(ev.msg || `第 ${ev.batch || '?'} 批 AI 已返回`);
        msg.textContent = ev.msg || msg.textContent;
      }
      if (ev.type === 'batch_done') {
        const pct = ev.total > 0 ? Math.round((ev.done / ev.total) * 100) : 0;
        bar.style.width = `${pct}%`;
        msg.textContent = ev.msg || `${ev.done}/${ev.total} 批已完成`;
      }
      if (ev.type === 'progress') {
        const pct = ev.total > 0 ? Math.round((ev.done / ev.total) * 100) : 0;
        bar.style.width = `${pct}%`;
        msg.textContent = ev.msg || `${ev.done}/${ev.total} 批已完成`;
      }
      if (ev.type === 'warn') {
        const warning = ev.msg || '处理警告';
        diagnostics.push(warning);
        appendAiLog(warning);
      }
      if (ev.type === 'ai_detect_done') {
        annState.completion = ev.completion || null;
        annState.historySaved = ev.history_saved ?? null;
        bar.style.width = '100%';
        const results = ev.results || [];
        const missingIds = ev.missing_ids || [];
        const missingTranslationIds = ev.missing_translation_ids || [];
        annState.aiResults = results;
        annState.highProbResults = ev.high_prob || [];
        annState.reviewAiResults = ev.review_results || annState.highProbResults;
        annState.aiReviewThreshold = ev.review_threshold ?? annState.aiReviewThreshold;
        annState.aiHighThreshold = ev.high_threshold ?? annState.aiHighThreshold;
        annState.aiConfirmationComplete = !!ev.confirmation_complete;
        annState.missingAiIds = missingIds;
        annState.missingTranslationIds = missingTranslationIds;
        if (missingIds.length > 0) {
          msg.textContent = `AI 识别未完成：${results.length} 条有效结果，仍有 ${missingIds.length} 行未回填`;
          appendAiLog(`仍有 ${missingIds.length} 行没有得到完整结果，已停止后续质量打标。请重试 AI 识别。`, 'error');
        } else if (missingTranslationIds.length > 0) {
          msg.textContent = `AI 识别完成，共 ${results.length} 条结果；${missingTranslationIds.length} 行中文翻译待补齐`;
          appendAiLog(
            `AI 判断结果均已保留，仍有 ${missingTranslationIds.length} 行译文待补齐。后续质量打标会继续尝试修复。`,
          );
        } else {
          msg.textContent = `AI 检测完成，共 ${results.length} 条结果`;
        }
      }
    }, ['ai_detect_done']);

    if (annState.missingAiIds.length > 0) {
      backBtn.style.display = '';
      annGoStep(6);
      annShowDone();
      return;
    }

    // 有高概率结果 → 跳到确认步
    if (annState.aiResults.length === 0) {
      msg.textContent = 'AI 识别没有得到可用结果，请返回任务选择后重试';
      const detail = diagnostics.length
        ? `最近诊断：${diagnostics.slice(-3).join(' ｜ ')}`
        : '没有收到批次诊断信息，可能是连接在服务端返回前中断。';
      appendAiLog(`所有批次都没有解析出可用结果。${detail}`, 'error');
      backBtn.style.display = '';
      return;
    }

    if (annState.reviewAiResults.length > 0 && !annState.aiConfirmationComplete) {
      annRenderAiConfirm(annState.reviewAiResults);
      annGoStep(4);
    } else {
      showToast(`未发现需要复核的 AI 作答（≥ ${annState.aiReviewThreshold}%），自动跳过确认步骤`, 'info');
      await annAfterAiConfirm();
    }
  } catch (e) {
    msg.textContent = `AI 识别失败：${e.message}`;
    appendAiLog(e.message, 'error');
    backBtn.style.display = '';
    showToast(`AI 检测失败：${e.message}`, 'error');
  }
}

// ── ANN STEP 4: AI 确认 ────────────────────────────────────

function annRenderAiConfirm(reviewResults) {
  annState.reviewAiResults = [...(reviewResults || [])].sort(
    (a, b) => Number(b.ai_prob || 0) - Number(a.ai_prob || 0)
  );
  annState.confirmedAiIds = new Set(
    annState.reviewAiResults
      .filter(result => Number(result.ai_prob || 0) >= annState.aiHighThreshold)
      .map(result => String(result.id))
  );
  annState.aiReviewedIds = new Set();
  annState.aiReviewIndex = 0;
  annState.aiQuestionIndex = 0;
  annSetAiConfirmLayout('split');
  annRenderAiCandidateList();
  annRenderAiProfile();
  annUpdateAiReviewProgress();

  $('ann-confirm-desc').textContent =
    `以下 ${annState.reviewAiResults.length} 位玩家的内容生成概率 ≥ ${annState.aiReviewThreshold}%。达到 ${annState.aiHighThreshold}% 的已默认选择为 AI 作答。`;
}

function annCurrentAiReviewResult() {
  return annState.reviewAiResults[annState.aiReviewIndex] || null;
}

function annAiProbabilityText(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${Math.round(numeric)}%` : '—';
}

function annAiReviewDecision(result) {
  const rowId = String(result?.id || '');
  if (!annState.aiReviewedIds.has(rowId)) return 'pending';
  return annState.confirmedAiIds.has(rowId) ? 'ai' : 'normal';
}

function annAiReviewStatusText(result) {
  const decision = annAiReviewDecision(result);
  if (decision === 'ai') return '已确认 · AI 作答';
  if (decision === 'normal') return '已确认 · 正常反馈';
  return Number(result?.ai_prob || 0) >= annState.aiHighThreshold
    ? '待确认 · 系统预选 AI'
    : '待人工确认';
}

function annRenderAiCandidateList() {
  const list = $('ann-ai-candidate-list');
  list.innerHTML = annState.reviewAiResults.map((result, index) => {
    const decision = annAiReviewDecision(result);
    return `<button class="ann-ai-candidate-card${index === annState.aiReviewIndex ? ' ann-ai-candidate-card--active' : ''}"
      type="button" data-ann-ai-player-index="${index}">
      <span class="ann-ai-candidate-id">${esc(result.id)}</span>
      <strong class="ann-ai-candidate-prob">${annAiProbabilityText(result.ai_prob)}</strong>
      <span class="ann-ai-candidate-reason">${esc(result.reason || '暂无判断理由')}</span>
      <em class="ann-ai-candidate-status ann-ai-candidate-status--${decision}">${esc(annAiReviewStatusText(result))}</em>
    </button>`;
  }).join('');
}

function annRenderAiQuestion(result) {
  const columns = annState.openTextCols || [];
  const total = columns.length;
  if (!total) {
    $('ann-ai-question-index').textContent = '第 0 / 0 题';
    $('ann-ai-question-title').textContent = '没有可查看的开放题';
    $('ann-ai-question-dots').innerHTML = '';
    $('ann-ai-answer-original').textContent = '—';
    $('ann-ai-answer-translation').textContent = '—';
    $('ann-ai-prev-question').disabled = true;
    $('ann-ai-next-question').disabled = true;
    return;
  }

  annState.aiQuestionIndex = Math.max(0, Math.min(annState.aiQuestionIndex, total - 1));
  const columnIndex = columns[annState.aiQuestionIndex];
  const key = `col_${columnIndex}`;
  const original = (result.originals || {})[key] || '';
  const translation = (result.translations || {})[key] || '';
  $('ann-ai-question-index').textContent = `第 ${annState.aiQuestionIndex + 1} / ${total} 题`;
  $('ann-ai-question-title').textContent = annState.headers[columnIndex] || `列 ${columnIndex}`;
  $('ann-ai-answer-original').textContent = original || '（未作答）';
  $('ann-ai-answer-translation').textContent = translation || '（暂无中文翻译）';
  $('ann-ai-prev-question').disabled = annState.aiQuestionIndex === 0;
  $('ann-ai-next-question').disabled = annState.aiQuestionIndex === total - 1;
  $('ann-ai-question-dots').innerHTML = columns.map((_, index) =>
    `<button type="button" data-ann-ai-question-index="${index}"
      class="${index === annState.aiQuestionIndex ? 'ann-ai-question-dot--active' : ''}"
      aria-label="查看第 ${index + 1} 题">${index + 1}</button>`
  ).join('');
}

function annRenderAiProfile() {
  const total = annState.reviewAiResults.length;
  if (!total) return;
  annState.aiReviewIndex = Math.max(0, Math.min(annState.aiReviewIndex, total - 1));
  const result = annCurrentAiReviewResult();
  const rowId = String(result.id);
  const decision = annAiReviewDecision(result);
  const selectedAsAi = annState.confirmedAiIds.has(rowId);

  $('ann-ai-player-index').textContent = `玩家 ${annState.aiReviewIndex + 1} / ${total}`;
  $('ann-ai-player-id').textContent = rowId;
  $('ann-ai-probability').textContent = annAiProbabilityText(result.ai_prob);
  $('ann-ai-polish-probability').textContent = annAiProbabilityText(result.polish_prob);
  $('ann-ai-review-state').textContent = annAiReviewStatusText(result);
  $('ann-ai-review-state').className = `ann-ai-review-state ann-ai-review-state--${decision}`;
  $('ann-ai-suggestion').textContent = Number(result.ai_prob || 0) >= annState.aiHighThreshold
    ? 'AI 建议：高概率 AI 作答'
    : 'AI 建议：需结合原文判断';
  $('ann-ai-reason').textContent = result.reason || '暂无判断理由';
  $('ann-ai-evidence').textContent = result.evidence || '暂无明确支持证据';
  $('ann-ai-counter-evidence').textContent = result.counter_evidence || '暂无明确反向证据';
  $('ann-ai-prev-player').disabled = annState.aiReviewIndex === 0;
  $('ann-ai-next-player').disabled = annState.aiReviewIndex === total - 1;
  $('ann-ai-mark-normal').classList.toggle('ann-ai-decision-btn--active', !selectedAsAi);
  $('ann-ai-mark-normal').setAttribute('aria-pressed', String(!selectedAsAi));
  $('ann-ai-mark-ai').classList.toggle('ann-ai-decision-btn--active', selectedAsAi);
  $('ann-ai-mark-ai').setAttribute('aria-pressed', String(selectedAsAi));
  annRenderAiQuestion(result);
}

function annUpdateAiReviewProgress() {
  const total = annState.reviewAiResults.length;
  const reviewed = annState.aiReviewedIds.size;
  const defaultAi = annState.reviewAiResults.filter(
    result => Number(result.ai_prob || 0) >= annState.aiHighThreshold
  ).length;
  $('ann-ai-review-total').textContent = `${total} 位玩家`;
  $('ann-ai-default-total').textContent = `${defaultAi} 位 AI 作答`;
  $('ann-ai-reviewed-total').textContent = `${reviewed} / ${total}`;
  $('ann-ai-summary-progress').setAttribute('aria-valuemax', String(total));
  $('ann-ai-summary-progress').setAttribute('aria-valuenow', String(reviewed));
  $('ann-ai-summary-progress-bar').style.width = total ? `${reviewed / total * 100}%` : '0%';
  $('ann-ai-confirm-footer-status').textContent = reviewed === total
    ? `已完成 ${total} 位玩家的人工确认`
    : `请完成剩余 ${total - reviewed} 位玩家的人工确认`;
  $('ann-btn-confirm-ai').disabled = !total || reviewed !== total;
}

function annSetAiDecision(decision) {
  const result = annCurrentAiReviewResult();
  if (!result) return;
  const rowId = String(result.id);
  annState.aiReviewedIds.add(rowId);
  if (decision === 'ai') annState.confirmedAiIds.add(rowId);
  else annState.confirmedAiIds.delete(rowId);
  annRenderAiCandidateList();
  annRenderAiProfile();
  annUpdateAiReviewProgress();
}

function annGoToAiPlayer(index) {
  const total = annState.reviewAiResults.length;
  if (!total) return;
  annState.aiReviewIndex = Math.max(0, Math.min(index, total - 1));
  annState.aiQuestionIndex = 0;
  annRenderAiCandidateList();
  annRenderAiProfile();
}

function annSetAiConfirmLayout(layout) {
  annState.aiConfirmLayout = layout === 'focus' ? 'focus' : 'split';
  $('ann-ai-confirm-workspace').classList.toggle('ann-ai-confirm-workspace--focus', annState.aiConfirmLayout === 'focus');
  $('ann-ai-layout-split').classList.toggle('ann-ai-layout-btn--active', annState.aiConfirmLayout === 'split');
  $('ann-ai-layout-split').setAttribute('aria-pressed', String(annState.aiConfirmLayout === 'split'));
  $('ann-ai-layout-focus').classList.toggle('ann-ai-layout-btn--active', annState.aiConfirmLayout === 'focus');
  $('ann-ai-layout-focus').setAttribute('aria-pressed', String(annState.aiConfirmLayout === 'focus'));
}

$('ann-ai-candidate-list').addEventListener('click', event => {
  const card = event.target.closest('[data-ann-ai-player-index]');
  if (card) annGoToAiPlayer(Number(card.dataset.annAiPlayerIndex));
});

$('ann-ai-question-dots').addEventListener('click', event => {
  const button = event.target.closest('[data-ann-ai-question-index]');
  if (!button) return;
  annState.aiQuestionIndex = Number(button.dataset.annAiQuestionIndex);
  const result = annCurrentAiReviewResult();
  if (result) annRenderAiQuestion(result);
});

$('ann-ai-prev-player').addEventListener('click', () => annGoToAiPlayer(annState.aiReviewIndex - 1));
$('ann-ai-next-player').addEventListener('click', () => annGoToAiPlayer(annState.aiReviewIndex + 1));
$('ann-ai-prev-question').addEventListener('click', () => {
  annState.aiQuestionIndex -= 1;
  const result = annCurrentAiReviewResult();
  if (result) annRenderAiQuestion(result);
});
$('ann-ai-next-question').addEventListener('click', () => {
  annState.aiQuestionIndex += 1;
  const result = annCurrentAiReviewResult();
  if (result) annRenderAiQuestion(result);
});
$('ann-ai-mark-normal').addEventListener('click', () => annSetAiDecision('normal'));
$('ann-ai-mark-ai').addEventListener('click', () => annSetAiDecision('ai'));
$('ann-ai-layout-split').addEventListener('click', () => annSetAiConfirmLayout('split'));
$('ann-ai-layout-focus').addEventListener('click', () => annSetAiConfirmLayout('focus'));

$('ann-btn-confirm-ai').addEventListener('click', async () => {
  if (annState.aiReviewedIds.size !== annState.reviewAiResults.length) {
    showToast('请先完成所有待复核玩家的人工判定', 'error');
    return;
  }
  const checked = [...annState.confirmedAiIds];

  try {
    const resp = await fetch(`/api/annotate/${annState.sessionId}/confirm-ai`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirmed_ai_ids: checked }),
    });
    if (!resp.ok) {
      const d = await resp.json();
      throw new Error(d.detail || '保存失败');
    }
    annState.aiConfirmationComplete = true;
    showToast(`已确认 ${checked.length} 位 AI 作答受访者`, 'success');
    await annAfterAiConfirm();
  } catch (e) {
    showToast(`确认失败：${e.message}`, 'error');
  }
});

async function annAfterAiConfirm() {
  if (annState.tasks.quality) {
    annGoStep(5);
    await annRunQuality();
  } else {
    annGoStep(6);
    annShowDone();
  }
}

// ── ANN STEP 5: 质量打标 ───────────────────────────────────

async function annRunQuality(options = {}) {
  if (annState.partialRetrySyncing || annState.qualityReviewRunning || annState.qualityReviewSaving) return;
  const runSessionId = annState.sessionId;
  const preserveReview = options.preserveReview === true;
  let completed = false;
  annState.qualityReviewRunning = true;
  window.AnnotateReview?.render();
  const bar = $('ann-quality-progress-bar');
  const msg = $('ann-quality-progress-msg');
  const warnLog = $('ann-quality-warn-log');
  let retryActions = $('ann-quality-retry');
  if (!retryActions) {
    retryActions = document.createElement('div');
    retryActions.id = 'ann-quality-retry';
    retryActions.className = 'ann-quality-retry';
    const retryBtn = document.createElement('button');
    retryBtn.id = 'ann-btn-quality-retry';
    retryBtn.type = 'button';
    retryBtn.className = 'btn btn--primary';
    retryBtn.setAttribute('aria-describedby', 'ann-quality-retry-hint');
    retryBtn.onclick = () => annRunQuality();
    const retryHint = document.createElement('p');
    retryHint.id = 'ann-quality-retry-hint';
    retryHint.className = 'ann-quality-retry__hint';
    retryActions.append(retryBtn, retryHint);
    warnLog.insertAdjacentElement('afterend', retryActions);
  }
  const retryBtn = $('ann-btn-quality-retry');
  const retryHint = $('ann-quality-retry-hint');
  retryActions.style.display = 'none';
  bar.style.width = '0%';
  msg.textContent = '正在连接…';
  warnLog.innerHTML = '';

  try {
    await consumeSSEGet(annRetryUrl("run-quality", options.retryIds), ev => {
      if (annState.sessionId !== runSessionId) return;
      if (ev.type === 'progress') {
        const pct = ev.total > 0 ? Math.round((ev.done / ev.total) * 100) : 0;
        bar.style.width = `${pct}%`;
        msg.textContent = ev.msg || `${ev.done}/${ev.total} 批已完成`;
      }
      if (ev.type === 'warn') {
        const div = document.createElement('div');
        div.className = 'ann-warn-item';
        div.textContent = ev.msg;
        warnLog.appendChild(div);
      }
      if (ev.type === 'quality_done') {
        annState.completion = ev.completion || null;
        annState.historySaved = ev.history_saved ?? null;
        completed = true;
        bar.style.width = '100%';
        const missingQIds = ev.missing_ids || [];
        const missingOverallIds = ev.missing_overall_ids || [];
        const missingTranslationIds = ev.missing_translation_ids || [];
        annState.qualityCount = ev.complete_count ?? ev.count ?? 0;
        annState.qualityResults = ev.results || [];
        annState.qualityDurationSeconds = ev.quality_duration_seconds ?? null;
        if (!preserveReview) {
          annState.qualityReviewPlayerId = null;
          annState.qualityQuestionIndex = 0;
          annState.qualityReviewLayout = 'split';
        }
        annState.qualityReviewSaving = false;
        annState.missingQualityIds = missingQIds;
        annState.missingOverallIds = missingOverallIds;
        annState.missingTranslationIds = missingTranslationIds;
        window.AnnotateReview?.ingest(ev);
        $('ann-btn-quality-preview').hidden = false;
        if (missingQIds.length > 0 || missingOverallIds.length > 0) {
          msg.textContent = `已有 ${annState.qualityCount} 行质量判断完整；逐题判断待补 ${missingQIds.length} 行，整体判断待补 ${missingOverallIds.length} 行`;
        } else if (missingTranslationIds.length > 0) {
          msg.textContent = `质量判断完成，共 ${annState.qualityCount} 条结果；${missingTranslationIds.length} 行中文翻译仍待补齐`;
        } else {
          msg.textContent = `质量打标完成，共 ${annState.qualityCount} 条结果`;
        }
      }
    }, ['quality_done']);
    if (annState.sessionId !== runSessionId) return;
    if (!completed) throw new Error('连接已结束，尚未收到完整结果；已有结果会保留');
    if (annState.missingQualityIds.length > 0 || annState.missingOverallIds.length > 0 || annState.missingTranslationIds.length > 0) {
      const missingQualityCount = annState.missingQualityIds.length;
      const missingOverallCount = annState.missingOverallIds.length;
      const incompleteQualityCount = new Set([...annState.missingQualityIds, ...annState.missingOverallIds].map(String)).size;
      const missingTranslationCount = annState.missingTranslationIds.length;
      const retainedSummary = `已有 ${annState.qualityCount} 行质量判断完整，已成功的逐题和整体判断均会保留。`;
      const retryScope = `逐题判断待补 ${missingQualityCount} 行，整体判断待补 ${missingOverallCount} 行。仅处理未完成行，结合该玩家完整作答补齐缺失判断。`;
      if (incompleteQualityCount > 0 && missingTranslationCount > 0) {
        retryBtn.textContent = '重试失败行并补译';
        retryHint.textContent = `${retainedSummary}${retryScope}中文翻译待补 ${missingTranslationCount} 行。`;
      } else if (incompleteQualityCount > 0) {
        retryBtn.textContent = `补齐未完成的 ${incompleteQualityCount} 行`;
        retryHint.textContent = `${retainedSummary}${retryScope}`;
      } else {
        retryBtn.textContent = `补齐 ${missingTranslationCount} 行中文翻译`;
        retryHint.textContent = '质量打标已完成，本次仅补齐缺失译文，保留已有质量结果。';
      }
      retryActions.style.display = '';
      $('ann-btn-quality-preview').hidden = annState.qualityResults.length === 0;
      annGoStep(6);
      annShowDone();
      return;
    }
    if (!preserveReview) annGoStep(6);
    annShowDone();
  } catch (e) {
    if (annState.sessionId === runSessionId) {
      showToast(`质量打标失败：${e.message}`, 'error');
      const recovered = await annRefreshPartialRetry({ notifyFailure: false });
      if (recovered && annState.sessionId === runSessionId) {
        annGoStep(6);
        annShowDone({ quiet: true, refreshCompletion: false });
        showToast('已从历史记录恢复最新完成情况，可继续补齐缺失项', 'info');
      } else if (annHasIncompleteResults()) {
        annGoStep(6);
        annShowDone({ quiet: true, refreshCompletion: false });
        showToast('未能读取最新完成情况，页面暂按已有状态显示；重试时服务端会自动跳过已完成项', 'info');
      }
    }
  } finally {
    if (annState.sessionId === runSessionId) {
      annState.qualityReviewRunning = false;
      if (preserveReview) annShowDone({ quiet: true });
      window.AnnotateReview?.render();
    }
  }
}

// ── ANN STEP 6: 完成 ─────────────────────────────────────

function annBuildDoneSummary() {
  const totalCount = annState.tasks.ai_detect
    ? annState.aiResults.length
    : annState.qualityCount;
  const lines = [
    `<div class="ann-summary-title">${annHasIncompleteResults() || annState.completion?.partial ? '当前标注结果（部分完成）' : `完成共 ${totalCount} 条反馈的标注`}</div>`,
  ];
  if (annState.completion) {
    const c = annState.completion;
    lines.push(`<p>共 ${c.total} 位玩家，${c.complete} 位结果完整，${c.missing_ids.length} 位有待补齐项。以下质量结论仅基于已完成判断。</p>`);
  }
  if (annState.tasks.ai_detect) {
    lines.push(
      `<div class="ann-summary-line">AI 识别结果：${annState.reviewAiResults.length} 条进入人工复核，${annState.confirmedAiIds.size} 位确认高概率 AI</div>`
    );
  }
  if (annState.tasks.quality) {
    const counts = { '优秀反馈': 0, '有效反馈': 0, '无效反馈': 0, '待补': 0 };
    annState.qualityResults.forEach(result => {
      const overall = annQualityOverallLabel(result);
      if (Object.hasOwn(counts, overall)) counts[overall] += 1;
    });
    lines.push(
      `<div class="ann-summary-line">整体质量：优秀反馈 ${counts['优秀反馈']} 条，有效反馈 ${counts['有效反馈']} 条，无效反馈 ${counts['无效反馈']} 条${counts['待补'] ? `，待补 ${counts['待补']} 条` : ''}</div>`
    );
    const duration = formatReportDuration(annState.qualityDurationSeconds);
    if (duration) {
      lines.push(`<div class="ann-summary-line">质量打标总耗时：${esc(duration)}</div>`);
    }
  }
  return lines.join('');
}

function annHasIncompleteResults() {
  return annState.completion?.partial || [annState.missingAiIds, annState.missingQualityIds, annState.missingOverallIds, annState.missingTranslationIds]
    .some(ids => (ids || []).length > 0);
}

function annShowDone(options = {}) {
  const notify = !options.quiet;
  const summary = annBuildDoneSummary();
  const missingAi = annState.missingAiIds || [];
  const missingQ = annState.missingQualityIds || [];
  const missingOverall = annState.missingOverallIds || [];
  const missingTranslations = annState.missingTranslationIds || [];
  const missingParts = [];
  if (missingAi.length) missingParts.push(`AI 检测漏返 ${missingAi.length} 行`);
  if (missingQ.length) missingParts.push(`逐题判断待补 ${missingQ.length} 行`);
  if (missingOverall.length) missingParts.push(`整体判断待补 ${missingOverall.length} 行`);
  if (missingTranslations.length) missingParts.push(`中文翻译缺失 ${missingTranslations.length} 行`);
  if (missingParts.length) {
    $('ann-done-text').innerHTML =
      summary +
      `<div class="ann-summary-error">部分完成：${missingParts.join('；')}。已有结果可下载，缺失项标为待补齐。</div>`;
    $('ann-btn-download').disabled = false;
    if (notify) showToast('部分完成：可下载已有结果，也可选择失败项补齐', 'info');
  } else {
    $('ann-done-text').innerHTML = summary;
    $('ann-btn-download').disabled = false;
    if (notify) showToast('标注完成，请预览结果后下载', 'success');
  }
  if (annState.historySaved === false) {
    $('ann-done-text').innerHTML += '<p role="alert">历史保存未成功；页面结果仍保留，请尝试下载，勿关闭页面。</p>';
  }
  const canCompleteQuality = annState.tasks.quality && (missingQ.length || missingOverall.length || missingTranslations.length);
  $('ann-btn-quality-complete').hidden = !canCompleteQuality;
  $('ann-btn-quality-complete').style.display = canCompleteQuality ? '' : 'none';
  annRenderQualityPreview();
  annRenderPartialRetry();
  if (
    options.refreshCompletion !== false
    && annHasIncompleteResults()
    && annState.historySaved !== false
    && annState.completion
    && annState.partialRetrySyncedCompletion !== annState.completion
    && !annState.partialRetrySyncing
  ) {
    annState.partialRetrySyncedCompletion = annState.completion;
    void annRefreshPartialRetry();
  }
}

const ANN_EDITABLE_QUALITY_LABELS = ['无效反馈', '有效反馈', '优秀反馈'];

function annIsEffectivelyEmptyAnswer(value) {
  return !String(value || '').trim();
}

function annCanonicalQualityLabel(label, overall = false) {
  const normalized = String(label || '').trim();
  if (normalized === '普通反馈') return '有效反馈';
  if (overall && normalized === 'N/A') return '无效反馈';
  return normalized;
}

function annUsesHolisticQuality(result) {
  return Number(result?.quality_policy_version || 0) >= 5
    || ['model_holistic', 'empty_no_answers'].includes(result?.overall_source);
}

function annQualityOverallLabel(result) {
  const holistic = annUsesHolisticQuality(result);
  const label = annCanonicalQualityLabel(result?.overall, !holistic);
  if (result?.overall_pending || !ANN_EDITABLE_QUALITY_LABELS.includes(label)) return '待补';
  return label;
}

function annQualityOverallSourceText(result) {
  if (!annUsesHolisticQuality(result)) return '旧规则：整体按逐题标签折算，保留原有判断口径';
  if (result.overall_source === 'empty_no_answers') return '全部主观题未作答：没有可供整体判断的回答';
  return '整体独立判断，不随逐题标签自动折算';
}

function annQualityOverallReason(result) {
  if (annQualityOverallLabel(result) === '待补') return '整体判断尚未完整返回，请补齐后查看';
  return result?.overall_reason || '暂无整体判断依据';
}

function annQualityQuestionLabel(result, key) {
  if (annIsEffectivelyEmptyAnswer((result?.originals || {})[key])) return 'N/A';
  return annCanonicalQualityLabel((result?.q_labels || {})[key]) || '待补';
}

function annQualityLabelClass(label) {
  return {
    '无效反馈': 'ann-quality-badge--invalid',
    '有效反馈': 'ann-quality-badge--valid',
    '优秀反馈': 'ann-quality-badge--excellent',
    'N/A': 'ann-quality-badge--na',
    '待补': 'ann-quality-badge--na',
  }[label] || 'ann-quality-badge--na';
}

function annQualityHumanReviews(result) {
  return result?.human_reviews || {};
}

function annQualityBaseline(result, key, emptyAnswer = false) {
  const baseline = (result?.quality_review_baseline || {})[key] || {};
  return {
    label: emptyAnswer
      ? 'N/A'
      : annCanonicalQualityLabel(baseline.label || (result?.q_labels || {})[key]) || '待补',
    reason: baseline.reason || (result?.q_reasons || {})[key] || '',
  };
}

function annQualityMatchesQuestionFilters(result, questionFilter, adjustmentFilter) {
  const reviews = annQualityHumanReviews(result);
  const keys = annState.openTextCols.map(col => `col_${col}`);
  if (questionFilter !== 'all' && adjustmentFilter === 'adjusted') {
    return keys.some(key => annQualityQuestionLabel(result, key) === questionFilter && reviews[key]);
  }
  if (questionFilter !== 'all' && !keys.some(key => annQualityQuestionLabel(result, key) === questionFilter)) {
    return false;
  }
  const hasAdjustment = Object.keys(reviews).length > 0;
  if (adjustmentFilter === 'adjusted') return hasAdjustment;
  if (adjustmentFilter === 'untouched') return !hasAdjustment;
  return true;
}

function annFilteredQualityResults() {
  const overallFilter = $('ann-quality-overall-filter').value;
  const questionFilter = $('ann-quality-question-filter').value;
  const adjustmentFilter = $('ann-quality-adjustment-filter').value;
  return annState.qualityResults.filter(result => (
    (overallFilter === 'all' || annQualityOverallLabel(result) === overallFilter)
    && annQualityMatchesQuestionFilters(result, questionFilter, adjustmentFilter)
  ));
}

function annPreferredQualityQuestionIndex(result) {
  const questionFilter = $('ann-quality-question-filter').value;
  const adjustmentFilter = $('ann-quality-adjustment-filter').value;
  const reviews = annQualityHumanReviews(result);
  let index = -1;
  if (questionFilter !== 'all' && adjustmentFilter === 'adjusted') {
    index = annState.openTextCols.findIndex(col => {
      const key = `col_${col}`;
      return annQualityQuestionLabel(result, key) === questionFilter && reviews[key];
    });
  }
  if (index < 0 && questionFilter !== 'all') {
    index = annState.openTextCols.findIndex(
      col => annQualityQuestionLabel(result, `col_${col}`) === questionFilter
    );
  }
  if (index < 0 && adjustmentFilter === 'adjusted') {
    index = annState.openTextCols.findIndex(col => reviews[`col_${col}`]);
  }
  return index >= 0 ? index : 0;
}

function annRenderQualitySummary() {
  const counts = { '无效反馈': 0, '有效反馈': 0, '优秀反馈': 0, '待补': 0 };
  annState.qualityResults.forEach(result => {
    const overall = annQualityOverallLabel(result);
    if (Object.hasOwn(counts, overall)) counts[overall] += 1;
  });
  const items = [
    ['全部玩家', annState.qualityResults.length],
    ['无效反馈', counts['无效反馈']],
    ['有效反馈', counts['有效反馈']],
    ['优秀反馈', counts['优秀反馈']],
  ];
  if (counts['待补']) items.push(['整体待补', counts['待补']]);
  $('ann-quality-summary-grid').innerHTML = items.map(([label, count]) => `
    <div class="ann-quality-summary-item">
      <span>${label}</span><strong>${count} 位</strong>
    </div>`).join('');
}

function annCurrentQualityResult(results = annFilteredQualityResults()) {
  if (!results.length) return null;
  let current = results.find(result => String(result.id) === String(annState.qualityReviewPlayerId));
  if (!current) {
    current = results[0];
    annState.qualityReviewPlayerId = String(current.id);
    annState.qualityQuestionIndex = annPreferredQualityQuestionIndex(current);
  }
  return current;
}

function annRenderQualityPlayerList(results) {
  const list = $('ann-quality-player-list');
  if (!results.length) {
    list.innerHTML = '<div class="ann-quality-empty">当前筛选没有匹配玩家</div>';
    return;
  }
  list.innerHTML = results.map(result => {
    const adjustedCount = Object.keys(annQualityHumanReviews(result)).length;
    const active = String(result.id) === String(annState.qualityReviewPlayerId);
    const overall = annQualityOverallLabel(result);
    return `<button class="ann-ai-candidate-card ann-quality-player-card${active ? ' ann-ai-candidate-card--active' : ''}"
      type="button" data-ann-quality-player-id="${esc(result.id)}">
      <span class="ann-ai-candidate-id">${esc(result.id)}</span>
      <span class="ann-quality-badge ${annQualityLabelClass(overall)}">${esc(overall)}</span>
      <span class="ann-ai-candidate-reason">${esc(
        adjustedCount ? `已人工调整 ${adjustedCount} 道题` : annQualityOverallReason(result)
      )}</span>
      <em class="ann-quality-review-status${adjustedCount ? ' ann-quality-review-status--adjusted' : ''}">
        ${adjustedCount ? '已人工复核' : '尚未修改'}
      </em>
    </button>`;
  }).join('');
}

function annRenderQualityProfile(results) {
  const pane = $('ann-quality-profile-pane');
  const result = annCurrentQualityResult(results);
  if (!result) {
    pane.innerHTML = '<div class="ann-quality-empty">请调整筛选条件后继续查看</div>';
    return;
  }
  const playerIndex = results.indexOf(result);
  const totalQuestions = annState.openTextCols.length;
  annState.qualityQuestionIndex = Math.max(0, Math.min(annState.qualityQuestionIndex, totalQuestions - 1));
  const col = annState.openTextCols[annState.qualityQuestionIndex];
  const key = `col_${col}`;
  const title = annState.headersZh[col] || annState.headers[col] || `列 ${col}`;
  const original = (result.originals || {})[key] || '';
  const translated = (result.translations || {})[key] || '';
  const evidence = (result.q_evidence || {})[key] || '';
  const emptyAnswer = annIsEffectivelyEmptyAnswer(original);
  const label = annQualityQuestionLabel(result, key);
  const baseline = annQualityBaseline(result, key, emptyAnswer);
  const review = annQualityHumanReviews(result)[key];
  const adjustedCount = Object.keys(annQualityHumanReviews(result)).length;
  const incomplete = annHasIncompleteResults();
  const disabled = emptyAnswer || incomplete || annState.qualityReviewSaving;
  const overall = annQualityOverallLabel(result);
  const questionDots = annState.openTextCols.map((questionCol, index) => {
    const questionKey = `col_${questionCol}`;
    const questionLabel = annQualityQuestionLabel(result, questionKey);
    const adjusted = Boolean(annQualityHumanReviews(result)[questionKey]);
    return `<button type="button" data-ann-quality-question-index="${index}"
      class="ann-quality-question-dot ${annQualityLabelClass(questionLabel)}${index === annState.qualityQuestionIndex ? ' ann-quality-question-dot--active' : ''}${adjusted ? ' ann-quality-question-dot--adjusted' : ''}"
      aria-label="查看第 ${index + 1} 题，${esc(questionLabel)}${adjusted ? '，已人工修改' : ''}">${index + 1}</button>`;
  }).join('');
  const labelButtons = ANN_EDITABLE_QUALITY_LABELS.map(option => `
    <button type="button" class="ann-quality-label-btn ${annQualityLabelClass(option)}"
      data-ann-quality-label="${option}" aria-pressed="${label === option}"${disabled ? ' disabled' : ''}>${option}</button>`).join('');

  pane.innerHTML = `
    <div class="ann-quality-profile-header">
      <div class="ann-ai-player-identity">
        <span>玩家档案</span>
        <h2>${esc(result.id)}</h2>
        <em class="ann-quality-review-status${adjustedCount ? ' ann-quality-review-status--adjusted' : ''}">${adjustedCount ? '已人工复核' : '尚未修改'}</em>
      </div>
      <div class="ann-quality-overall-card">
        <span>当前整体质量</span>
        <strong class="ann-quality-badge ${annQualityLabelClass(overall)}">${esc(overall)}</strong>
        <p>${esc(annQualityOverallReason(result))}</p>
        <p>${esc(annQualityOverallSourceText(result))}</p>
        ${annUsesHolisticQuality(result) && adjustedCount ? `<p>${esc(result.overall_review_note || `已人工调整 ${adjustedCount} 道题；整体保留原判断，未重新评估`)}</p>` : ''}
      </div>
    </div>
    <div class="ann-ai-player-nav">
      <button type="button" data-ann-quality-player-nav="prev" aria-label="上一位玩家"${playerIndex === 0 ? ' disabled' : ''}>‹</button>
      <span>玩家 ${playerIndex + 1} / ${results.length}</span>
      <button type="button" data-ann-quality-player-nav="next" aria-label="下一位玩家"${playerIndex === results.length - 1 ? ' disabled' : ''}>›</button>
    </div>
    <div class="ann-ai-question-panel ann-quality-question-panel">
      <div class="ann-ai-question-heading">
        <div><span>第 ${annState.qualityQuestionIndex + 1} / ${totalQuestions} 题</span><strong>${esc(title)}</strong></div>
        <div class="ann-ai-question-nav">
          <button type="button" data-ann-quality-question-nav="prev"${annState.qualityQuestionIndex === 0 ? ' disabled' : ''}>上一题</button>
          <button type="button" data-ann-quality-question-nav="next"${annState.qualityQuestionIndex === totalQuestions - 1 ? ' disabled' : ''}>下一题</button>
        </div>
      </div>
      <div class="ann-ai-question-dots ann-quality-question-dots">${questionDots}</div>
      <div class="ann-quality-answer-grid">
        <div class="ann-ai-answer-block"><span>玩家原文</span><p>${esc(original || '（未作答）')}</p></div>
        <div class="ann-ai-answer-block ann-ai-answer-block--translation"><span>中文翻译</span><p>${esc(translated || '（无内容）')}</p></div>
      </div>
      <div class="ann-quality-evidence-grid">
        <div class="ann-quality-evidence-card">
          <span>AI 原始标签与判断依据</span>
          <strong class="ann-quality-badge ${annQualityLabelClass(baseline.label)}">${esc(baseline.label)}</strong>
          <p>${esc(baseline.reason || (baseline.label === '待补' ? '逐题判断尚未完整返回' : '未提供判断依据'))}</p>
        </div>
        <div class="ann-quality-evidence-card">
          <span>原文证据</span>
          <p>${esc(evidence || '无原文证据')}</p>
        </div>
      </div>
      <div class="ann-quality-edit-panel">
        <div class="ann-quality-edit-head">
          <div><strong>人工最终标注</strong>${emptyAnswer ? '<span>该题未作答，固定标为 N/A，不开放修改</span>' : ''}</div>
          <div class="ann-quality-label-actions" role="group" aria-label="调整这道题的最终标签">${labelButtons}</div>
        </div>
        <div class="ann-quality-save-state" aria-live="polite">
          ${annState.qualityReviewSaving
            ? '正在保存人工调整…'
            : incomplete
              ? '结果尚未完整，请先补齐逐题判断、整体判断和译文后再人工调整'
              : review
                ? `人工调整记录：${esc(annCanonicalQualityLabel(review.from_label))} → ${esc(annCanonicalQualityLabel(review.to_label))}；下载时以当前标签为准`
                : emptyAnswer
                  ? '当前题未作答，标签固定为 N/A'
                  : '尚未修改，当前采用 AI 原始标签'}
        </div>
      </div>
    </div>`;
}

function annSetQualityReviewLayout(layout) {
  annState.qualityReviewLayout = layout === 'focus' ? 'focus' : 'split';
  $('ann-quality-review-workspace').classList.toggle(
    'ann-ai-confirm-workspace--focus',
    annState.qualityReviewLayout === 'focus'
  );
  $('ann-quality-layout-split').classList.toggle('ann-ai-layout-btn--active', annState.qualityReviewLayout === 'split');
  $('ann-quality-layout-split').setAttribute('aria-pressed', String(annState.qualityReviewLayout === 'split'));
  $('ann-quality-layout-focus').classList.toggle('ann-ai-layout-btn--active', annState.qualityReviewLayout === 'focus');
  $('ann-quality-layout-focus').setAttribute('aria-pressed', String(annState.qualityReviewLayout === 'focus'));
}

function annRenderQualityPreview() {
  if (window.AnnotateReview?.render()) return;
  const block = $('ann-quality-preview-block');
  if (!annState.tasks.quality || annState.qualityResults.length === 0) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  const results = annFilteredQualityResults();
  annCurrentQualityResult(results);
  $('ann-quality-filter-count').textContent = `当前显示 ${results.length} / ${annState.qualityResults.length} 位玩家`;
  annRenderQualitySummary();
  annRenderQualityPlayerList(results);
  annRenderQualityProfile(results);
  annSetQualityReviewLayout(annState.qualityReviewLayout);
}

function annGoToQualityPlayer(playerId) {
  const result = annState.qualityResults.find(item => String(item.id) === String(playerId));
  if (!result) return;
  annState.qualityReviewPlayerId = String(result.id);
  annState.qualityQuestionIndex = annPreferredQualityQuestionIndex(result);
  annRenderQualityPreview();
}

async function annApplyQualityLabel(label) {
  if (annState.qualityReviewSaving || annHasIncompleteResults()) return;
  const results = annFilteredQualityResults();
  const result = annCurrentQualityResult(results);
  const col = annState.openTextCols[annState.qualityQuestionIndex];
  const key = `col_${col}`;
  if (!result || !ANN_EDITABLE_QUALITY_LABELS.includes(label)) return;
  if (annQualityQuestionLabel(result, key) === label) return;

  annState.qualityReviewSaving = true;
  annRenderQualityProfile(results);
  try {
    const resp = await fetch(`/api/annotate/${annState.sessionId}/quality-review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        player_id: String(result.id),
        column_index: col,
        label,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '保存人工调整失败');
    const index = annState.qualityResults.findIndex(item => String(item.id) === String(result.id));
    if (index >= 0 && data.result) annState.qualityResults[index] = data.result;
    annState.qualityReviewSaving = false;
    annShowDone({ quiet: true });
    const savedResult = data.result || result;
    const savedMessage = annUsesHolisticQuality(savedResult)
      ? '人工标签已保存，整体保留原判断，未重新评估'
      : '人工标签已保存，整体质量已按旧规则重新计算';
    showToast(data.changed ? savedMessage : '当前标签无需修改', 'success');
  } catch (e) {
    annState.qualityReviewSaving = false;
    annRenderQualityPreview();
    showToast(`保存失败：${e.message}`, 'error');
  }
}

$('ann-quality-player-list').addEventListener('click', event => {
  const card = event.target.closest('[data-ann-quality-player-id]');
  if (card) annGoToQualityPlayer(card.dataset.annQualityPlayerId);
});

$('ann-quality-profile-pane').addEventListener('click', event => {
  const results = annFilteredQualityResults();
  const current = annCurrentQualityResult(results);
  if (!current) return;
  const playerNav = event.target.closest('[data-ann-quality-player-nav]');
  if (playerNav) {
    const index = results.indexOf(current) + (playerNav.dataset.annQualityPlayerNav === 'prev' ? -1 : 1);
    if (results[index]) annGoToQualityPlayer(results[index].id);
    return;
  }
  const questionNav = event.target.closest('[data-ann-quality-question-nav]');
  if (questionNav) {
    annState.qualityQuestionIndex += questionNav.dataset.annQualityQuestionNav === 'prev' ? -1 : 1;
    annRenderQualityProfile(results);
    return;
  }
  const dot = event.target.closest('[data-ann-quality-question-index]');
  if (dot) {
    annState.qualityQuestionIndex = Number(dot.dataset.annQualityQuestionIndex);
    annRenderQualityProfile(results);
    return;
  }
  const labelButton = event.target.closest('[data-ann-quality-label]');
  if (labelButton) annApplyQualityLabel(labelButton.dataset.annQualityLabel);
});

for (const filterId of [
  'ann-quality-overall-filter',
  'ann-quality-question-filter',
  'ann-quality-adjustment-filter',
]) {
  $(filterId).addEventListener('change', () => {
    annState.qualityReviewPlayerId = null;
    annState.qualityQuestionIndex = 0;
    annRenderQualityPreview();
  });
}

$('ann-quality-layout-split').addEventListener('click', () => annSetQualityReviewLayout('split'));
$('ann-quality-layout-focus').addEventListener('click', () => annSetQualityReviewLayout('focus'));

$('ann-btn-quality-complete').addEventListener('click', () => {
  if (annState.qualityReviewRunning || annState.qualityReviewSaving || window.AnnotateReview?.busy()) return;
  annGoStep(5);
  annRunQuality();
});

$('ann-btn-download').addEventListener('click', () => {
  if (window.AnnotateReview?.download()) return;
  annDownloadResults();
});

$('ann-btn-quality-preview').addEventListener('click', () => {
  annGoStep(6);
  annShowDone();
});

$('ann-btn-restart').addEventListener('click', () => {
  if (annState.qualityReviewRunning || annState.qualityReviewSaving || window.AnnotateReview?.busy()) {
    showToast('请等待当前保存或补齐完成后再开始新标注', 'info');
    return;
  }
  if (!confirm('确定要重新标注吗？当前标注数据将被清除。')) return;
  window.AnnotateReview?.reset();
  annState.sessionId = null;
  annState.completion = null;
  annState.historySaved = null;
  annState.partialRetryRunning = false;
  annState.partialRetrySyncing = false;
  annState.partialRetrySyncVersion += 1;
  annState.partialRetrySyncedCompletion = null;
  annState.currentStep = 1;
  annState.headers = [];
  annState.idCol = 1;
  annState.openTextCols = [];
  annState.tasks = { ai_detect: false, quality: false };
  annState.aiResults = [];
  annState.highProbResults = [];
  annState.reviewAiResults = [];
  annState.aiConfirmationComplete = false;
  annState.confirmedAiIds = new Set();
  annState.aiReviewedIds = new Set();
  annState.aiReviewIndex = 0;
  annState.aiQuestionIndex = 0;
  annState.aiConfirmLayout = 'split';
  annState.qualityResults = [];
  annState.qualityCount = 0;
  annState.qualityDurationSeconds = null;
  annState.qualityReviewPlayerId = null;
  annState.qualityQuestionIndex = 0;
  annState.qualityReviewLayout = 'split';
  annState.qualityReviewSaving = false;
  annState.missingAiIds = [];
  annState.missingQualityIds = [];
  annState.missingOverallIds = [];
  annState.missingTranslationIds = [];
  $('ann-btn-download').disabled = true;
  $('ann-btn-quality-complete').hidden = true;
  $('ann-btn-quality-complete').style.display = 'none';
  $('task-ai-detect').checked = false;
  $('task-quality').checked = false;
  $('ann-quality-overall-filter').value = 'all';
  $('ann-quality-question-filter').value = 'all';
  $('ann-quality-adjustment-filter').value = 'all';
  $('ann-background').value = '';
  $('ann-background-block').style.display = 'none';
  annSyncTaskCards();
  annResetUploadZone();
  annFileInput.value = '';
  annGoStep(1);
  showToast('已重置，请重新上传文件', 'info');
});


// ── 上传说明文案 ──

fetch('/api/upload-guide')
  .then(r => r.json())
  .then(({ content }) => {
    const el = document.getElementById('upload-guide');
    if (el && content) el.innerHTML = marked.parse(content);
  })
  .catch(() => { });


// A failed download must never navigate away from the in-memory task.
async function annDownloadResults(historyId = null) {
  if (!historyId && (!annState.sessionId || annState.partialRetryRunning || annState.partialRetrySyncing || annState.qualityReviewRunning || annState.qualityReviewSaving)) return;
  const sid = annState.sessionId;
  try {
    const response = await fetch(historyId ? `/api/annotate-history/${encodeURIComponent(historyId)}/download` : `/api/annotate/${encodeURIComponent(sid)}/download`);
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(typeof error.detail === 'string' ? error.detail : `下载失败（${response.status}），已有结果仍保留`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const filename = match ? decodeURIComponent(match[1]) : '标注结果.xlsx';
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = filename;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) { showToast(error.message, 'error'); }
}

function annCompletionMissingIds(completion = annState.completion) {
  if (!completion || typeof completion !== 'object') return [];
  const listed = Array.isArray(completion.missing_ids) ? completion.missing_ids : Object.keys(completion.gaps || {});
  return [...new Set(listed.map(id => String(id)).filter(Boolean))];
}

function annApplyServerCompletion(completion) {
  if (!completion || typeof completion !== 'object') return false;
  const gaps = completion.gaps && typeof completion.gaps === 'object' ? completion.gaps : {};
  const missingIds = annCompletionMissingIds(completion);
  const matching = predicate => missingIds.filter(id => {
    const parts = Array.isArray(gaps[id]) ? gaps[id].map(String) : [];
    return parts.some(predicate);
  });
  const hasDetailedGaps = missingIds.some(id => Array.isArray(gaps[id]) && gaps[id].length > 0);

  annState.completion = completion;
  annState.missingAiIds = matching(part => part === 'AI判断');
  annState.missingTranslationIds = matching(part => part.endsWith('中文翻译'));
  const qualityStageAvailable = !annState.tasks.ai_detect || completion.ai_confirmation_complete === true;
  annState.missingOverallIds = qualityStageAvailable
    ? matching(part => part === '整体质量判断') : [];
  annState.missingQualityIds = qualityStageAvailable
    ? matching(part => part.endsWith('质量判断') && part !== '整体质量判断') : [];
  if (!hasDetailedGaps && missingIds.length) {
    if (annState.tasks.quality && qualityStageAvailable) annState.missingQualityIds = missingIds;
    else if (annState.tasks.ai_detect) annState.missingAiIds = missingIds;
  }
  return true;
}

async function annFetchServerCompletion(sessionId = annState.sessionId) {
  const response = await fetch(`/api/history/${encodeURIComponent(sessionId)}`, {
    cache: 'no-store', headers: { Accept: 'application/json' },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `读取完成情况失败（${response.status}）`);
  if (!data.annotate_completion || typeof data.annotate_completion !== 'object') {
    throw new Error('历史记录尚未保存最新完成情况');
  }
  return data.annotate_completion;
}

async function annRefreshPartialRetry(options = {}) {
  const sessionId = annState.sessionId;
  if (!sessionId || annState.partialRetrySyncing) return null;
  const syncVersion = ++annState.partialRetrySyncVersion;
  annState.partialRetrySyncing = true;
  annRenderPartialRetry();
  window.AnnotateReview?.render();
  try {
    const completion = await annFetchServerCompletion(sessionId);
    if (annState.sessionId !== sessionId || annState.partialRetrySyncVersion !== syncVersion) return null;
    annState.partialRetrySyncedCompletion = completion;
    annApplyServerCompletion(completion);
    annShowDone({ quiet: true, refreshCompletion: false });
    return completion;
  } catch (error) {
    if (options.notifyFailure && annState.sessionId === sessionId) {
      showToast(`未能校正最新缺项：${error.message}。将按页面当前选择提交，服务端仍会过滤已完成项。`, 'info');
    }
    return null;
  } finally {
    if (annState.sessionId === sessionId && annState.partialRetrySyncVersion === syncVersion) {
      annState.partialRetrySyncing = false;
      annRenderPartialRetry();
      window.AnnotateReview?.render();
    }
  }
}

function annRetryUrl(stage, ids) {
  const query = new URLSearchParams();
  if (ids) ids.forEach(id => query.append('retry_ids', String(id)));
  return `/api/annotate/${encodeURIComponent(annState.sessionId)}/${stage}${query.size ? '?' + query.toString() : ''}`;
}

function annRenderPartialRetry() {
  let box = $('ann-partial-retry');
  if (!box) {
    box = document.createElement('section'); box.id = 'ann-partial-retry';
    box.style.cssText = 'text-align:left;padding:16px 0';
    $('ann-done-text').insertAdjacentElement('afterend', box);
  }
  const aiPending = annState.missingAiIds.length > 0;
  const gaps = annState.completion?.gaps || {};
  const completionIds = annCompletionMissingIds();
  const ids = aiPending ? [...new Set(annState.missingAiIds.map(String))]
    : completionIds.length || annState.completion
      ? completionIds
      : [...new Set([
        ...annState.missingQualityIds, ...annState.missingOverallIds, ...annState.missingTranslationIds,
      ].map(String))];
  box.hidden = !ids.length;
  if (!ids.length) { box.innerHTML = ''; return; }
  const busy = annState.partialRetryRunning || annState.partialRetrySyncing || annState.qualityReviewRunning || annState.qualityReviewSaving;
  box.innerHTML = `<h3>选择失败项重跑</h3><p>按玩家选择，仅补其缺失项，已完成判断保留。重跑结果更新同一条历史记录。${aiPending ? '当前先补 AI 识别，完成确认后继续质量打标。' : ''}</p>
    <button type="button" class="btn btn--ghost" data-partial-all ${busy ? 'disabled' : ''}>全选 / 取消全选</button>
    <div style="max-height:240px;overflow:auto">${ids.map(id => `<label style="display:block;padding:8px 0"><input type="checkbox" data-partial-id value="${esc(String(id))}" ${busy ? 'disabled' : ''}> ${esc(String(id))}：${esc((gaps[id] || [aiPending ? 'AI 判断待补齐' : '结果待补齐']).join('、'))}</label>`).join('')}</div>
    <button type="button" class="btn btn--primary" data-partial-run ${busy ? 'disabled' : ''}>${busy ? '正在补齐…' : '重跑所选失败项'}</button>`;
  box.querySelector('[data-partial-all]').onclick = () => {
    const inputs = [...box.querySelectorAll('[data-partial-id]')];
    const checked = !inputs.every(input => input.checked);
    inputs.forEach(input => { input.checked = checked; });
  };
  box.querySelector('[data-partial-run]').onclick = async () => {
    const selected = [...box.querySelectorAll('[data-partial-id]:checked')].map(input => input.value);
    if (!selected.length) { showToast('请先选择要补齐的玩家', 'info'); return; }
    if (annState.partialRetryRunning || annState.partialRetrySyncing || annState.qualityReviewRunning || annState.qualityReviewSaving) return;
    const sessionId = annState.sessionId;
    const serverCompletion = await annRefreshPartialRetry({ notifyFailure: true });
    if (annState.sessionId !== sessionId) return;
    let retryIds = selected;
    if (serverCompletion) {
      const available = new Set(annCompletionMissingIds(serverCompletion));
      retryIds = selected.filter(id => available.has(String(id)));
      const skipped = selected.length - retryIds.length;
      if (!retryIds.length) {
        showToast('所选玩家当前都已完成，列表已更新', 'info');
        return;
      }
      if (skipped) showToast(`其中 ${skipped} 位已完成，本次只补 ${retryIds.length} 位`, 'info');
    }
    annState.partialRetryRunning = true;
    annRenderPartialRetry();
    try {
      const retryAiPending = annState.missingAiIds.length > 0;
      if (retryAiPending || !annState.tasks.quality) {
        annGoStep(3);
        await annRunAiDetect({retryIds});
      } else {
        annGoStep(5);
        await annRunQuality({retryIds, preserveReview: true});
        annGoStep(6); annShowDone({quiet: true});
      }
    } finally {
      annState.partialRetryRunning = false;
      annRenderPartialRetry();
      window.AnnotateReview?.render();
    }
  };
}
