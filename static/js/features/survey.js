// ============================================================
// STEP 1: Upload
// ============================================================

const uploadZone = $('upload-zone');
const fileInput = $('file-input');

uploadZone.addEventListener('click', () => fileInput.click());
uploadZone.addEventListener('dragover', e => { e.preventDefault(); uploadZone.classList.add('drag-over'); });
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('drag-over'));
uploadZone.addEventListener('drop', e => {
  e.preventDefault();
  uploadZone.classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) handleUpload(file);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) handleUpload(fileInput.files[0]);
});

async function handleUpload(file) {
  const MAX = 50 * 1024 * 1024;
  if (file.size > MAX) { showToast('文件超过 50MB 上限', 'error'); return; }

  uploadZone.innerHTML = `
    <div class="upload-zone__icon"><div class="spinner" style="width:40px;height:40px;border-width:3px"></div></div>
    <div class="upload-zone__text">
      <span class="upload-zone__primary">正在上传 ${esc(file.name)}…</span>
    </div>`;

  const fd = new FormData();
  fd.append('file', file);

  try {
    const resp = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '上传失败');

    state.sessionId = data.session_id;
    state.viewMode = 'session';
    state.historyId = null;
    clearPlanInput();
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
    renderPreview(data);
    goStep(2);
    showToast(`成功读取 ${data.total_rows} 行数据`, 'success');
    loadColumns();
  } catch (e) {
    showToast(`上传失败：${e.message}`, 'error');
    resetUploadZone();
  }
}

function resetUploadZone() {
  uploadZone.innerHTML = `
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

function renderPreview(data) {
  $('preview-meta').textContent =
    `${data.filename} · 已读取 ${data.total_rows} 行数据 · ${data.headers.length} 列`;
}

// ============================================================
// STEP 2: 题型确认
// ============================================================

async function loadColumns() {
  const list = $('col-list');
  $('col-confirm-count').textContent = '';
  list.innerHTML = `<div class="thinking-block"><div class="thinking-block__icon"><div class="spinner"></div></div>
    <div class="thinking-block__content"><div class="thinking-block__title">AI 正在识别题型与中文题名（较慢，请稍候）…</div>
    <div class="thinking-block__stream" id="col-stream-text"></div></div></div>`;
  $('btn-start-plan').disabled = true;

  try {
    await consumeSSE(`/api/columns/${state.sessionId}`, ev => {
      if (ev.type === 'chunk') {
        const el = $('col-stream-text');
        if (el) { el.textContent += ev.content; el.scrollTop = el.scrollHeight; }
      }
      if (ev.type === 'columns_ready') {
        state.columns = ev.columns;
        renderColumnRows(ev.columns);
      }
    });
    $('btn-start-plan').disabled = false;
  } catch (e) {
    list.innerHTML = `<div class="hist-empty">题型识别失败：${esc(e.message)}</div>`;
    showToast(e.message, 'error');
  }
}

function renderColumnRows(columns) {
  $('col-confirm-count').textContent = `共 ${columns.length} 道题`;
  $('col-list').innerHTML = columns.map((c, i) => columnRowHTML(c, i)).join('');
  columns.forEach((c, i) => updateExtra(i, c.role));
}

function optionEditorHTML(i, c) {
  const options = c.options || [];
  const aliases = c.value_aliases || {};
  const aliasGroups = Object.entries(aliases).filter(([canon, values]) =>
    Array.isArray(values) && values.some(v => String(v).trim() && String(v).trim() !== canon)
  ).length;
  const chips = options.slice(0, 6).map(opt => `<span class="option-summary-chip">${esc(opt)}</span>`).join('');
  const more = options.length > 6 ? `<span class="option-summary-more">+${options.length - 6}</span>` : '';
  const mergeBadge = aliasGroups ? `<span class="option-merge-badge">已合并 ${aliasGroups} 组</span>` : '';
  const rows = options.map(opt => `
    <div class="option-edit-row">
      <div class="option-edit-row__main">
        <input class="extra-input option-input" data-option="${i}" value="${esc(opt)}" placeholder="选项内容" />
        <div class="option-alias-hint">${(aliases[opt] || []).map(a =>
    `<span class="alias-chip"><span class="alias-chip__text" title="${esc(a)}">${esc(a)}</span><button class="alias-chip__remove" data-alias-col="${i}" data-alias-canon="${esc(opt)}" data-alias-val="${esc(a)}" title="移除此别名" type="button">×</button></span>`
  ).join('')}<button class="alias-add-btn" data-alias-add-col="${i}" data-alias-add-canon="${esc(opt)}" title="添加别名" type="button">+</button></div>
      </div>
      <button class="btn-icon option-remove" data-option-remove="${i}" title="删除选项" type="button">×</button>
    </div>
  `).join('');
  return `<div class="option-editor" data-option-editor="${i}">
    <details class="option-editor__details" ${c.low_confidence ? 'open' : ''}>
      <summary class="option-editor__summary">
        <span class="option-editor__summary-main">${chips || '<span class="option-summary-empty">暂无选项</span>'}${more}</span>
        <span class="option-editor__summary-actions">${mergeBadge}<span class="option-edit-link">编辑</span></span>
      </summary>
      <div class="option-editor__body">
        <div class="option-editor__head">
          <span>标准选项</span>
          <button class="btn btn--ghost btn--sm option-add" data-option-add="${i}" type="button">添加选项</button>
        </div>
        <div class="option-editor__rows">${rows}</div>
      </div>
    </details>
  </div>`;
}

function matrixEditorHTML(i, c) {
  const colIndexes = c.column_indexes || [];
  const rows = c.rows || [];
  const rowCount = Math.max(colIndexes.length, rows.length);
  const summaryRows = rows.length ? rows : colIndexes.map((_, idx) => `子项${idx + 1}`);
  const chips = summaryRows.slice(0, 5).map(row => `<span class="matrix-summary-chip">${esc(row)}</span>`).join('');
  const more = summaryRows.length > 5 ? `<span class="matrix-summary-more">+${summaryRows.length - 5}</span>` : '';
  const bodyRows = Array.from({ length: rowCount }).map((_, idx) => {
    const colNo = colIndexes[idx] != null ? `列 ${colIndexes[idx]}` : `子项 ${idx + 1}`;
    const value = rows[idx] || '';
    return `<div class="matrix-edit-row">
      <span class="matrix-edit-row__col">${esc(colNo)}</span>
      <input class="extra-input matrix-row-input" data-matrix-row="${i}" data-matrix-row-idx="${idx}" value="${esc(value)}" placeholder="子项名称" />
    </div>`;
  }).join('');

  return `<div class="matrix-editor" data-matrix-editor="${i}">
    <details class="matrix-editor__details" ${c.low_confidence ? 'open' : ''}>
      <summary class="matrix-editor__summary">
        <span class="matrix-editor__summary-main">${chips || '<span class="matrix-summary-empty">暂无子项</span>'}${more}</span>
        <span class="matrix-edit-link">编辑子项</span>
      </summary>
      <div class="matrix-editor__body">
        <div class="matrix-editor__head">矩阵子项</div>
        <div class="matrix-editor__rows">${bodyRows}</div>
      </div>
    </details>
  </div>`;
}

function collectOptionsForColumn(i) {
  const seen = new Set();
  const values = [];
  document.querySelectorAll(`.option-input[data-option="${i}"]`).forEach(input => {
    const v = input.value.trim();
    const key = v.toLocaleLowerCase();
    if (v && !seen.has(key)) {
      seen.add(key);
      values.push(v);
    }
  });
  return values;
}

function collectMatrixRowsForColumn(i, c) {
  const inputs = Array.from(document.querySelectorAll(`.matrix-row-input[data-matrix-row="${i}"]`));
  if (!inputs.length) return c.rows || [];

  return inputs.map((input, idx) => {
    const value = input.value.trim();
    return value || (c.rows || [])[idx] || `子项${idx + 1}`;
  });
}

function buildEditedOptionAliases(c, editedOptions) {
  const aliases = { ...(c.value_aliases || {}) };
  const original = c.options_original || c.options || [];
  const editedSet = new Set(editedOptions);
  const norm = s => String(s || '').trim().toLocaleLowerCase();

  // 只有长度相同时才做位置匹配推断重命名（纯重命名场景）
  // 长度变了说明有增删，保守处理：只按名字保留既有 alias，不推断重命名，避免位移误归并
  if (original.length === editedOptions.length) {
    original.forEach((oldValue, idx) => {
      const newValue = editedOptions[idx];
      if (!oldValue || !newValue || norm(oldValue) === norm(newValue)) return;
      aliases[newValue] = [...new Set([...(aliases[newValue] || []), oldValue, ...(aliases[oldValue] || [])])];
      delete aliases[oldValue];
    });
  }

  Object.keys(aliases).forEach(k => {
    if (!editedSet.has(k)) delete aliases[k];
  });
  return aliases;
}

function columnRowHTML(c, i) {
  const opts = ROLE_OPTIONS.map(([val, label]) =>
    `<option value="${val}" ${val === c.role ? 'selected' : ''}>${label}</option>`
  ).join('');
  const name = c.name_zh || c.name || `列${(c.column_indexes || [])[0] ?? i}`;
  const isMatrix = MATRIX_ROLES.includes(c.role) || (c.column_indexes || []).length > 1;
  const matrixTag = isMatrix ? `<span class="col-row__tag">矩阵 · ${(c.column_indexes || []).length} 列</span>` : '';
  const roleClass = c.role ? ` col-row--role-${c.role}` : '';
  const lowConfClass = '';  // 不改背景色，跟随题型颜色
  const lowConfBadge = c.low_confidence
    ? `<div class="col-row__low-conf-badge"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>AI 判断信心低，请人工确认</div>`
    : '';

  return `<div class="col-row${roleClass}${lowConfClass}" data-card="${i}">
    <span class="col-row__num">${i + 1}</span>
    <div class="col-row__main">
      <div class="col-row__name" title="${esc(name)}">${esc(name)}${matrixTag}</div>
      ${lowConfBadge}
      <div class="q-extra" data-extra="${i}"></div>
    </div>
    <select class="type-select col-row__select" data-card="${i}">${opts}</select>
  </div>`;
}

function updateExtra(i, role) {
  const box = document.querySelector(`[data-extra="${i}"]`);
  if (!box) return;
  const c = state.columns[i] || {};
  const bits = [];

  if (MATRIX_ROLES.includes(role)) {
    bits.push(matrixEditorHTML(i, c));
  }

  if (role === 'multi_choice') {
    const delim = c.delimiter || '，';
    bits.push(`<span class="q-extra-inline">分隔符
      <input class="extra-input extra-input--sm" data-delim="${i}" value="${esc(delim)}" placeholder="，" /></span>`);
  }

  if (CHOICE_ROLES.includes(role)) {
    bits.push(optionEditorHTML(i, c));
  } else if (role === 'scale' || role === 'matrix_scale') {
    const mn = (c.scale_min ?? 1), mx = (c.scale_max ?? 5);
    bits.push(`<span class="q-extra-inline">量程
      <input class="extra-input extra-input--sm" type="number" data-smin="${i}" value="${mn}" />
      <span class="scale-sep">-</span>
      <input class="extra-input extra-input--sm" type="number" data-smax="${i}" value="${mx}" /></span>`);
  }

  box.innerHTML = bits.join('');
  box.style.display = bits.length ? 'flex' : 'none';
}

$('col-list').addEventListener('change', e => {
  const sel = e.target.closest('.type-select');
  if (sel) {
    const i = +sel.dataset.card;
    const newRole = sel.value;
    state.columns[i].role = newRole;
    updateExtra(i, newRole);
    // 同步更新颜色类
    const row = document.querySelector(`.col-row[data-card="${i}"]`);
    if (row) {
      row.className = row.className.replace(/\bcol-row--role-\S+/g, '').trim();
      if (newRole) row.classList.add(`col-row--role-${newRole}`);
    }
  }
});

$('col-list').addEventListener('click', e => {
  const addBtn = e.target.closest('[data-option-add]');
  if (addBtn) {
    const i = +addBtn.dataset.optionAdd;
    const rows = document.querySelector(`[data-option-editor="${i}"] .option-editor__rows`);
    if (rows) {
      rows.insertAdjacentHTML('beforeend', `
        <div class="option-edit-row">
          <div class="option-edit-row__main">
            <input class="extra-input option-input" data-option="${i}" value="" placeholder="选项内容" />
            <div class="option-alias-hint"><button class="alias-add-btn" data-alias-add-col="${i}" data-alias-add-canon="" title="添加别名" type="button">+</button></div>
          </div>
          <button class="btn-icon option-remove" data-option-remove="${i}" title="删除选项" type="button">×</button>
        </div>
      `);
      rows.querySelector('.option-edit-row:last-child .option-input')?.focus();
    }
  }

  const removeBtn = e.target.closest('[data-option-remove]');
  if (removeBtn) {
    removeBtn.closest('.option-edit-row')?.remove();
  }

  const aliasRemoveBtn = e.target.closest('.alias-chip__remove');
  if (aliasRemoveBtn) {
    const colIdx = +aliasRemoveBtn.dataset.aliasCol;
    const canon = aliasRemoveBtn.dataset.aliasCanon;
    const val = aliasRemoveBtn.dataset.aliasVal;
    const col = state.columns[colIdx];
    if (col?.value_aliases?.[canon]) {
      col.value_aliases[canon] = col.value_aliases[canon].filter(a => a !== val);
      if (!col.value_aliases[canon].length) delete col.value_aliases[canon];
    }
    aliasRemoveBtn.closest('.alias-chip')?.remove();
  }

  const aliasAddBtn = e.target.closest('.alias-add-btn');
  if (aliasAddBtn) {
    const colIdx = +aliasAddBtn.dataset.aliasAddCol;
    // 新增选项行 canon 为空时，从同行 input 读取当前值
    let canon = aliasAddBtn.dataset.aliasAddCanon;
    if (!canon) {
      canon = aliasAddBtn.closest('.option-edit-row')?.querySelector('.option-input')?.value?.trim() || '';
    }
    if (!canon) return;
    const input = document.createElement('input');
    input.className = 'alias-add-input';
    input.placeholder = '输入别名，回车确认';
    aliasAddBtn.replaceWith(input);
    input.focus();

    function commitAlias() {
      const val = input.value.trim();
      const col = state.columns[colIdx];
      if (val && col) {
        if (!col.value_aliases) col.value_aliases = {};
        if (!col.value_aliases[canon]) col.value_aliases[canon] = [];
        if (!col.value_aliases[canon].includes(val)) {
          // 全局唯一化：从其他 canon 的 alias list 和 DOM 中移除相同值，避免同一原始值被两个标准选项覆盖映射
          if (col.value_aliases) {
            Object.keys(col.value_aliases).forEach(k => {
              if (k === canon) return;
              col.value_aliases[k] = col.value_aliases[k].filter(a => a !== val);
              if (!col.value_aliases[k].length) delete col.value_aliases[k];
            });
          }
          document.querySelectorAll(`.alias-chip__remove[data-alias-col="${colIdx}"]`).forEach(btn => {
            if (btn.dataset.aliasCanon !== canon && btn.dataset.aliasVal === val) {
              btn.closest('.alias-chip')?.remove();
            }
          });
          col.value_aliases[canon].push(val);
          const chip = document.createElement('span');
          chip.className = 'alias-chip';
          chip.innerHTML = `<span class="alias-chip__text" title="${esc(val)}">${esc(val)}</span><button class="alias-chip__remove" data-alias-col="${colIdx}" data-alias-canon="${esc(canon)}" data-alias-val="${esc(val)}" title="移除此别名" type="button">×</button>`;
          input.parentNode.insertBefore(chip, input);
        }
      }
      const newBtn = document.createElement('button');
      newBtn.className = 'alias-add-btn';
      newBtn.dataset.aliasAddCol = colIdx;
      newBtn.dataset.aliasAddCanon = canon;
      newBtn.title = '添加别名';
      newBtn.type = 'button';
      newBtn.textContent = '+';
      input.replaceWith(newBtn);
    }

    input.addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); commitAlias(); } });
    input.addEventListener('blur', commitAlias);
  }
});

function collectConfirmedColumns() {
  return state.columns.map((c, i) => {
    const role = (document.querySelector(`.type-select[data-card="${i}"]`) || {}).value || c.role;
    const out = {
      name_zh: c.name_zh || c.name || '',
      role,
      column_indexes: c.column_indexes || (c.index != null ? [c.index] : []),
    };

    if (role === 'multi_choice') {
      const el = document.querySelector(`[data-delim="${i}"]`);
      out.delimiter = el ? el.value : (c.delimiter || '，');
    }

    if (CHOICE_ROLES.includes(role)) {
      const options = collectOptionsForColumn(i);
      if (options.length) {
        out.options = options;
        out.options_original = c.options_original || c.options || [];
        const aliases = buildEditedOptionAliases(c, options);
        if (Object.keys(aliases).length) out.value_aliases = aliases;
      }
    }

    if (role === 'matrix_multi' && c.delimiter) out.delimiter = c.delimiter;

    if (role === 'scale' || role === 'matrix_scale') {
      const mnEl = document.querySelector(`[data-smin="${i}"]`);
      const mxEl = document.querySelector(`[data-smax="${i}"]`);
      out.scale_min = mnEl ? Number(mnEl.value) : (c.scale_min ?? 1);
      out.scale_max = mxEl ? Number(mxEl.value) : (c.scale_max ?? 5);
    }
    if (MATRIX_ROLES.includes(role)) {
      const rows = collectMatrixRowsForColumn(i, c);
      if (rows.length) out.rows = rows;
    }
    if (!out.value_aliases && c.value_aliases && CHOICE_ROLES.includes(role)) {
      out.value_aliases = c.value_aliases;
    }
    return out;
  });
}

$('btn-start-plan').addEventListener('click', startPlan);

async function startPlan() {
  const btn = $('btn-start-plan');
  if (btn) btn.disabled = true;
  clearPlanInput();

  // 跑数表模式:列已在上传时确定性建好,跳过题型确认;其余模式先存用户确认的题型
  if (state.mode !== 'crosstab') {
    try {
      const columns = collectConfirmedColumns();
      const resp = await fetch(`/api/columns/${state.sessionId}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ columns }),
      });
      if (!resp.ok) {
        const d = await resp.json();
        throw new Error(d.detail || '保存题型失败');
      }
    } catch (e) {
      showToast(`保存题型失败：${e.message}`, 'error');
      if (btn) btn.disabled = false;
      return;
    }
  }

  // 进入 Step 3，开始 AI 规划
  goStep(3);
  $('plan-thinking').style.display = 'flex';
  $('plan-thinking').querySelector('.thinking-block__title').textContent =
    state.mode === 'crosstab' ? 'AI 正在阅读问卷、规划报告章节，请稍候…' : 'AI 正在规划分析方案，请稍候…';
  $('plan-card').style.display = 'none';
  $('plan-stream-text').textContent = '';

  try {
    await consumeSSE(`/api/plan/${state.sessionId}`, ev => {
      if (ev.type === 'chunk') {
        const el = $('plan-stream-text');
        el.textContent += ev.content;
        el.scrollTop = el.scrollHeight;
      }
      if (ev.type === 'plan_ready') {
        state.planData = ev.plan;
        showPlanCard(ev.plan, ev.headers);
      }
    });
  } catch (e) {
    showToast(`方案生成失败：${e.message}`, 'error');
    btn.disabled = false;
  }
}

// ============================================================
// STEP 3: Plan card
// ============================================================

function showPlanCard(plan, headers) {
  $('plan-thinking').style.display = 'none';
  $('plan-card').style.display = 'block';
  $('plan-card-content').innerHTML = buildPlanHTML(plan, headers);
  clearPlanInput();
  // 确保按钮状态与输入框同步（修订后面板可能处于 disabled 残留）
  $('btn-plan-ok').disabled = false;
  $('btn-plan-revise').disabled = true;
}

function buildPlanHTML(plan, headers) {
  let html = '';

  const colMap = {};
  for (const c of plan.columns) colMap[c.index] = c;

  // 1. 报告章节（列分类已在 Step 2 由用户确认，此处不再展示）
  html += `<div class="plan-section">
    <div class="plan-section__title">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
      报告章节
    </div>
    <div class="plan-parts">`;

  for (let i = 0; i < plan.parts.length; i++) {
    const p = plan.parts[i];
    // 跑数表模式:章节是语义化的(name + scope),不绑定列号
    const detail = p.column_indexes
      ? p.column_indexes.map(idx => {
        const c = colMap[idx];
        return c ? (c.name || (headers && headers[idx]) || `列${idx}`) : `列${idx}`;
      }).join('、')
      : (p.scope || '');
    html += `<div class="plan-part">
      <span class="plan-part__num">Part ${i + 1}</span>
      <span class="plan-part__name">${esc(p.name)}</span>
      <span class="plan-part__cols">${esc(detail)}</span>
    </div>`;
  }
  html += `</div></div>`;

  // 2. 交叉分析
  const cross = plan.cross_tabs || [];
  if (cross.length) {
    html += `<div class="plan-section">
      <div class="plan-section__title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        交叉分析
      </div>
      <div class="plan-cross">`;
    for (const ct of cross) {
      const pName = (colMap[ct.profile_index]?.name) || (headers && headers[ct.profile_index]) || `列${ct.profile_index}`;
      const qName = (colMap[ct.question_index]?.name) || (headers && headers[ct.question_index]) || `列${ct.question_index}`;
      html += `<div class="plan-cross-item">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        ${esc(pName)} × ${esc(qName)}
      </div>`;
    }
    html += `</div></div>`;
  }

  // 3. 待确认问题
  const openQs = plan.open_questions || [];
  if (openQs.length) {
    html += `<div class="plan-section">
      <div class="plan-section__title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        待确认问题
      </div>
      <div class="plan-questions">`;
    openQs.forEach((q, i) => {
      html += `<div class="plan-question">
        <span class="plan-question__num">Q${i + 1}</span>
        <span>${esc(q)}</span>
      </div>`;
    });
    html += `</div></div>`;
  }

  return html;
}

// ── Plan confirm ──

function syncPlanActionButtons() {
  const hasText = !!($('plan-input').value || '').trim();
  $('btn-plan-ok').disabled = hasText;
  $('btn-plan-revise').disabled = !hasText;
}

function clearPlanInput() {
  const input = $('plan-input');
  if (!input) return;
  input.value = '';
  input.setAttribute('autocomplete', 'off');
  syncPlanActionButtons();
}

$('btn-plan-ok').addEventListener('click', () => {
  if (($('plan-input').value || '').trim()) return;
  confirmPlan('ok');
});
$('btn-plan-revise').addEventListener('click', () => {
  const txt = $('plan-input').value.trim();
  if (!txt) { showToast('请先输入修改意见', 'info'); return; }
  clearPlanInput();
  confirmPlan(txt);
});
$('plan-input').addEventListener('input', syncPlanActionButtons);
$('plan-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const txt = $('plan-input').value.trim();
    if (txt) {
      clearPlanInput();
      confirmPlan(txt);
    }
  }
});

async function confirmPlan(text) {
  $('btn-plan-ok').disabled = true;
  $('btn-plan-revise').disabled = true;

  try {
    let approved = false;
    let newPlan = null;
    let newHeaders = null;

    if (text.toLowerCase() === 'ok') {
      approved = true;
    } else {
      $('plan-thinking').style.display = 'flex';
      $('plan-thinking').querySelector('.thinking-block__title').textContent = 'AI 正在修订方案…';
      $('plan-stream-text').textContent = '';
      $('plan-card').style.display = 'none';

      await consumeSSEPost('/api/plan/confirm', {
        session_id: state.sessionId,
        user_text: text,
      }, ev => {
        if (ev.type === 'progress') {
          // 解析/重试状态——让用户知道后端还在工作
          const el = $('plan-stream-text');
          el.textContent = ev.message;
          _updateProgressStatus(ev.message);
        }
        if (ev.type === 'chunk') {
          const el = $('plan-stream-text');
          el.textContent += ev.content;
          el.scrollTop = el.scrollHeight;
        }
        if (ev.type === 'plan_ready') {
          newPlan = ev.plan;
          newHeaders = ev.headers;
        }
        if (ev.type === 'json' && ev.approved) {
          approved = true;
        }
      });

      if (newPlan) {
        state.planData = newPlan;
        showPlanCard(newPlan, newHeaders);
        showToast('方案已修订，请再次确认', 'success');
        return;
      }
    }

    if (approved) {
      await runStats();
    }
  } catch (e) {
    showToast(`操作失败：${e.message}`, 'error');
    // 修订失败时恢复方案卡片（隐藏 thinking 区，避免用户看到空白）
    if (state.planData) {
      $('plan-thinking').style.display = 'none';
      $('plan-card').style.display = 'block';
    }
    syncPlanActionButtons();
  }
}
