// ============================================================
// 数据标注状态机
// ============================================================

const annState = {
  sessionId: null,
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
  qualityResults: [],
  qualityCount: 0,
  missingAiIds: [],
  missingQualityIds: [],
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
    $('ann-background-block').style.display = annState.tasks.ai_detect ? '' : 'none';
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
    annState.qualityResults = [];
    annState.qualityCount = 0;

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

async function annRunAiDetect() {
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
    await consumeSSE(`/api/annotate/${annState.sessionId}/run-ai-detect`, ev => {
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
    });

    if (annState.missingAiIds.length > 0) {
      backBtn.style.display = '';
      showToast('AI 识别结果不完整，已停止后续任务', 'error');
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
  const table = $('ann-confirm-table');
  const headers = annState.headers;
  const otCols = annState.openTextCols;

  // 构建表头
  let thCells = `<th class="ann-th-check"><input type="checkbox" id="ann-check-master" /></th>
    <th class="ann-th-id">玩家 ID</th><th class="ann-th-prob">内容生成概率</th><th class="ann-th-polish">润色概率</th><th class="ann-th-fixed">判断理由</th><th class="ann-th-fixed">AI 支持证据</th><th class="ann-th-fixed">反向证据</th>`;
  for (const ci of otCols) {
    const hdr = headers[ci] || `列${ci}`;
    thCells += `<th class="ann-th-fixed">${esc(hdr)}（原文）</th><th class="ann-th-fixed">${esc(hdr)}（中文译）</th>`;
  }

  let rows = '';
  reviewResults.forEach((r, i) => {
    const checked = r.ai_prob >= annState.aiHighThreshold ? 'checked' : '';
    let tdCols = '';
    for (const ci of otCols) {
      const key = `col_${ci}`;
      const original = (r.originals || {})[key] || '';
      const trans = (r.translations || {})[key] || '';
      tdCols += `<td class="ann-cell-text ann-cell-fixed">${esc(original)}</td>
                 <td class="ann-cell-text ann-cell-trans ann-cell-fixed">${esc(trans)}</td>`;
    }
    rows += `<tr data-row="${i}">
      <td><input type="checkbox" class="ann-ai-check" data-id="${esc(r.id)}" ${checked} /></td>
      <td class="ann-cell-id">${esc(r.id)}</td>
      <td class="ann-cell-prob">${r.ai_prob}%</td>
      <td class="ann-cell-polish">${r.polish_prob}%</td>
      <td class="ann-cell-reason ann-cell-fixed">${esc(r.reason || '')}</td>
      <td class="ann-cell-evidence ann-cell-fixed">${esc(r.evidence || '')}</td>
      <td class="ann-cell-evidence ann-cell-fixed">${esc(r.counter_evidence || '')}</td>
      ${tdCols}
    </tr>`;
  });

  table.innerHTML = `<thead><tr>${thCells}</tr></thead><tbody>${rows}</tbody>`;

  // 主控勾选
  $('ann-check-master').addEventListener('change', e => {
    table.querySelectorAll('.ann-ai-check').forEach(cb => { cb.checked = e.target.checked; });
  });

  $('ann-confirm-desc').textContent =
    `以下 ${reviewResults.length} 位玩家的内容生成概率 ≥ ${annState.aiReviewThreshold}%。达到 ${annState.aiHighThreshold}% 的已默认勾选；润色概率不参与违规判断。`;
}

$('ann-btn-check-all').addEventListener('click', () => {
  document.querySelectorAll('.ann-ai-check').forEach(cb => { cb.checked = true; });
});
$('ann-btn-uncheck-all').addEventListener('click', () => {
  document.querySelectorAll('.ann-ai-check').forEach(cb => { cb.checked = false; });
});

$('ann-btn-confirm-ai').addEventListener('click', async () => {
  const checked = [...document.querySelectorAll('.ann-ai-check:checked')].map(cb => cb.dataset.id);
  annState.confirmedAiIds = new Set(checked);

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
  } else if (annState.missingTranslationIds.length > 0) {
    annGoStep(3);
    const retryBtn = $('ann-btn-ai-back');
    if (retryBtn) retryBtn.style.display = '';
    showToast('AI 判断已完成，但中文翻译仍不完整，请重试补齐后再下载', 'error');
  } else {
    annGoStep(6);
    annShowDone();
  }
}

// ── ANN STEP 5: 质量打标 ───────────────────────────────────

async function annRunQuality() {
  const bar = $('ann-quality-progress-bar');
  const msg = $('ann-quality-progress-msg');
  const warnLog = $('ann-quality-warn-log');
  let retryBtn = $('ann-btn-quality-retry');
  if (!retryBtn) {
    retryBtn = document.createElement('button');
    retryBtn.id = 'ann-btn-quality-retry';
    retryBtn.className = 'btn btn--ghost';
    retryBtn.textContent = '重试质量打标';
    retryBtn.onclick = () => annRunQuality();
    warnLog.insertAdjacentElement('afterend', retryBtn);
  }
  retryBtn.style.display = 'none';
  bar.style.width = '0%';
  msg.textContent = '正在连接…';
  warnLog.innerHTML = '';

  try {
    await consumeSSE(`/api/annotate/${annState.sessionId}/run-quality`, ev => {
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
        bar.style.width = '100%';
        const missingQIds = ev.missing_ids || [];
        const missingTranslationIds = ev.missing_translation_ids || [];
        annState.qualityCount = ev.count;
        annState.qualityResults = ev.results || [];
        annState.missingQualityIds = missingQIds;
        annState.missingTranslationIds = missingTranslationIds;
        if (missingQIds.length > 0) {
          msg.textContent = `质量打标完成，共 ${ev.count} 条结果（${missingQIds.length} 行重试后仍未回填）`;
        } else if (missingTranslationIds.length > 0) {
          msg.textContent = `质量打标完成，共 ${ev.count} 条结果；${missingTranslationIds.length} 行中文翻译仍待补齐`;
        } else {
          msg.textContent = `质量打标完成，共 ${ev.count} 条结果`;
        }
      }
    });
    if (annState.missingQualityIds.length > 0 || annState.missingTranslationIds.length > 0) {
      retryBtn.style.display = '';
      showToast(
        annState.missingQualityIds.length > 0
          ? '质量打标结果不完整，已停留在当前步骤'
          : '中文翻译仍不完整，请重试补齐后再下载',
        'error',
      );
      return;
    }
    annGoStep(6);
    annShowDone();
  } catch (e) {
    showToast(`质量打标失败：${e.message}`, 'error');
  }
}

// ── ANN STEP 6: 完成 ─────────────────────────────────────

function annBuildDoneSummary() {
  const totalCount = annState.tasks.ai_detect
    ? annState.aiResults.length
    : annState.qualityCount;
  const lines = [
    `<div class="ann-summary-title">完成共 ${totalCount} 条反馈的标注</div>`,
  ];
  if (annState.tasks.ai_detect) {
    lines.push(
      `<div class="ann-summary-line">AI 识别结果：${annState.reviewAiResults.length} 条进入人工复核，${annState.confirmedAiIds.size} 位确认高概率 AI</div>`
    );
  }
  if (annState.tasks.quality) {
    const counts = { '优秀反馈': 0, '普通反馈': 0, '无效反馈': 0 };
    annState.qualityResults.forEach(result => {
      if (Object.hasOwn(counts, result.overall)) counts[result.overall] += 1;
    });
    lines.push(
      `<div class="ann-summary-line">质量打标结果：优秀反馈 ${counts['优秀反馈']} 条，普通反馈 ${counts['普通反馈']} 条，无效反馈 ${counts['无效反馈']} 条</div>`
    );
  }
  return lines.join('');
}

function annShowDone() {
  const summary = annBuildDoneSummary();
  const missingAi = annState.missingAiIds || [];
  const missingQ = annState.missingQualityIds || [];
  const missingTranslations = annState.missingTranslationIds || [];
  const missingParts = [];
  if (missingAi.length) missingParts.push(`AI 检测漏返 ${missingAi.length} 行`);
  if (missingQ.length) missingParts.push(`质量打标漏返 ${missingQ.length} 行`);
  if (missingTranslations.length) missingParts.push(`中文翻译缺失 ${missingTranslations.length} 行`);
  if (missingParts.length) {
    $('ann-done-text').innerHTML =
      summary +
      `<div class="ann-summary-error">结果不完整：${missingParts.join('；')}，下载已被阻断。请返回对应任务重试。</div>`;
    $('ann-btn-download').disabled = true;
    showToast('部分行未能回填，下载已被阻断', 'error');
  } else {
    $('ann-done-text').innerHTML = summary;
    $('ann-btn-download').disabled = false;
    annRenderQualityPreview();
    showToast('标注完成，请预览结果后下载', 'success');
  }
}

function annRenderQualityPreview() {
  const block = $('ann-quality-preview-block');
  const table = $('ann-quality-preview-table');
  if (!annState.tasks.quality || annState.qualityResults.length === 0) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  const filter = $('ann-quality-filter').value;
  const results = annState.qualityResults.filter(result => filter === 'all' || result.overall === filter);
  $('ann-quality-filter-count').textContent = `${results.length} 位玩家`;

  let headers = '<th class="ann-th-id">玩家 ID</th><th class="ann-col-overall">整体质量</th><th class="ann-col-reason">整体原因</th>';
  for (const col of annState.openTextCols) {
    const title = annState.headersZh[col] || annState.headers[col] || `列${col}`;
    headers += `<th class="ann-col-label">${esc(title)} · 标签</th><th class="ann-col-reason">判断依据</th><th class="ann-col-response">回答原文 / 中文</th>`;
  }
  const rows = results.map(result => {
    let cells = `<td class="ann-cell-id">${esc(result.id)}</td><td class="ann-quality-overall ann-col-overall">${esc(result.overall || '')}</td><td class="ann-col-reason">${esc(result.overall_reason || '')}</td>`;
    for (const col of annState.openTextCols) {
      const key = `col_${col}`;
      const label = (result.q_labels || {})[key] || 'N/A';
      const reason = (result.q_reasons || {})[key] || '';
      const evidence = (result.q_evidence || {})[key] || '';
      const original = (result.originals || {})[key] || '';
      const translated = (result.translations || {})[key] || '';
      cells += `<td class="ann-col-label"><span class="ann-quality-label-readonly">${esc(label)}</span></td>
        <td class="ann-col-reason"><div class="ann-reason-text">${esc(reason)}</div>${evidence ? `<div class="ann-review-evidence"><span>证据</span>${esc(evidence)}</div>` : ''}</td>
        <td class="ann-col-response"><div class="ann-response-original">${esc(original)}</div>${translated && translated !== original ? `<div class="ann-cell-trans"><span>中文</span>${esc(translated)}</div>` : ''}</td>`;
    }
    return `<tr data-id="${esc(result.id)}">${cells}</tr>`;
  }).join('');
  table.innerHTML = `<thead><tr>${headers}</tr></thead><tbody>${rows || '<tr><td>当前筛选没有结果</td></tr>'}</tbody>`;
}

$('ann-quality-filter').addEventListener('change', () => {
  annRenderQualityPreview();
});

$('ann-btn-download').addEventListener('click', () => {
  window.location.href = `/api/annotate/${annState.sessionId}/download`;
});

$('ann-btn-restart').addEventListener('click', () => {
  if (!confirm('确定要重新标注吗？当前标注数据将被清除。')) return;
  annState.sessionId = null;
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
  annState.qualityResults = [];
  annState.qualityCount = 0;
  annState.missingAiIds = [];
  annState.missingQualityIds = [];
  annState.missingTranslationIds = [];
  $('ann-btn-download').disabled = true;
  $('task-ai-detect').checked = false;
  $('task-quality').checked = false;
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
