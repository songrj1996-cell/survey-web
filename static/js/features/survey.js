// Upload and report-focus controls live in survey-entry.js.

function renderPreview(data) {
  const fileUploadCount = Math.max(0, Number(data.file_upload_answer_count) || 0);
  const fileUploadNotice = fileUploadCount > 0
    ? ` · 注意：${fileUploadCount} 个文件上传回答仅保留 Drive 元数据，文件内容未读取且不会进入本次分析`
    : '';
  $('preview-meta').textContent =
    `${data.filename} · 已读取 ${data.total_rows} 行数据 · ${data.headers.length} 列${fileUploadNotice}`;
}

// ============================================================
// STEP 2: 题型确认
// ============================================================

async function loadColumns() {
  const list = $('col-list');
  $('col-confirm-count').textContent = '';
  const loadingTitle = state.questionnaireUsed
    ? '正在从调研问卷读取题型、选项和矩阵结构…'
    : 'AI 正在识别题型与中文题名（较慢，请稍候）…';
  list.innerHTML = `<div class="thinking-block"><div class="thinking-block__icon"><div class="spinner"></div></div>
    <div class="thinking-block__content"><div class="thinking-block__title">${loadingTitle}</div>
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
  if (state.columnDraftSession !== state.sessionId) {
    state.columns = columns.map(prepareColumnDraft);
    state.columnDraftSession = state.sessionId;
    state.selectedQuestionKeys = state.columns.filter(isSelectableColumn).map(columnQuestionKey);
    state.columnFilter = 'all';
  }
  renderQuestionList();
  refreshContextFormVisibility();
}

// ── 可选业务上下文（本地草稿自动暂存 + 提交）────────────────────

const CONTEXT_DRAFT_KEY = 'survey_context_draft';
const CONTEXT_FIELD_IDS = {
  problem: 'ctx-problem',
  key_concerns: 'ctx-key-concerns',
  target_users: 'ctx-target-users',
  analysis_approach: 'ctx-analysis-approach',
};
let currentContextFileSignature = '';

function contextFileSignature(file) {
  if (!file) return '';
  return `${file.name || ''}|${file.size || 0}|${file.lastModified || 0}`;
}

function readContextForm() {
  const out = {};
  for (const [key, id] of Object.entries(CONTEXT_FIELD_IDS)) {
    const el = $(id);
    out[key] = el ? el.value.trim() : '';
  }
  return out;
}

function writeContextForm(data) {
  if (!data) return;
  for (const [key, id] of Object.entries(CONTEXT_FIELD_IDS)) {
    const el = $(id);
    if (el && data[key] != null) el.value = data[key];
  }
}

function loadContextDraft() {
  try {
    const raw = localStorage.getItem(CONTEXT_DRAFT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && parsed.fields) return parsed;
    return { fileSignature: '', fields: parsed || {} };
  } catch (e) {
    return null;
  }
}

function saveContextDraft() {
  try {
    localStorage.setItem(CONTEXT_DRAFT_KEY, JSON.stringify({
      fileSignature: currentContextFileSignature,
      fields: readContextForm(),
    }));
  } catch (e) { /* localStorage 不可用时静默忽略 */ }
}

function loadContextDraftForCurrentFile() {
  const draft = loadContextDraft();
  if (!draft || !draft.fileSignature || draft.fileSignature !== currentContextFileSignature) return null;
  return draft.fields || {};
}

function clearContextDraft() {
  try { localStorage.removeItem(CONTEXT_DRAFT_KEY); } catch (e) { /* 忽略 */ }
}

let contextDraftSaveTimer = null;
let preserveContextDraftOnNextUpload = false;
function scheduleContextDraftSave() {
  clearTimeout(contextDraftSaveTimer);
  contextDraftSaveTimer = setTimeout(saveContextDraft, 400);
}

function clearContextForm() {
  for (const id of Object.values(CONTEXT_FIELD_IDS)) {
    const el = $(id);
    if (el) el.value = '';
  }
}

function showBlockingFlowError(title, message) {
  const existing = $('blocking-flow-error-modal');
  if (existing) existing.remove();
  const modal = document.createElement('div');
  modal.id = 'blocking-flow-error-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:260;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.48);backdrop-filter:blur(4px);';
  modal.innerHTML = `
    <div style="width:min(520px,92vw);background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:22px;box-shadow:var(--shadow-lg);">
      <div style="font-size:18px;font-weight:700;margin-bottom:10px;color:var(--red);">${esc(title || '流程失败')}</div>
      <div style="font-size:14px;line-height:1.7;color:var(--text-2);white-space:pre-wrap;word-break:break-word;">${esc(message || '服务端处理失败，请重新开始后再试。')}</div>
      <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:22px;">
        <button class="btn btn--ghost" id="blocking-error-stay" type="button">留在当前页</button>
        <button class="btn btn--primary" id="blocking-error-restart" type="button">重新开始</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  const close = () => modal.remove();
  $('blocking-error-stay').onclick = close;
  $('blocking-error-restart').onclick = () => {
    saveContextDraft();
    close();
    const restart = $('btn-restart');
    if (restart) restart.click();
  };
  modal.addEventListener('click', e => {
    if (e.target === modal) close();
  });
}

(function initContextForm() {
  Object.values(CONTEXT_FIELD_IDS).forEach(id => {
    const el = $(id);
    if (el) el.addEventListener('input', scheduleContextDraftSave);
  });
})();

function refreshContextFormVisibility() {
  const wrap = $('context-form-wrap');
  if (!wrap) return;
  wrap.style.display = '';
  // Restore drafts only when initializing an upload. Column readiness must not
  // overwrite live input with a draft that is still waiting for its save timer.
}

let duplicateReportResolve = null;

function duplicateReportHistoryId(report) {
  return String(report?.history_id || report?.id || '').trim();
}

function closeDuplicateReportModal(result) {
  const modal = $('duplicate-report-modal');
  if (modal) modal.hidden = true;
  document.body.style.removeProperty('overflow');
  if (duplicateReportResolve) {
    const resolve = duplicateReportResolve;
    duplicateReportResolve = null;
    resolve(result);
  }
}

function promptDuplicateReport(report) {
  const modal = $('duplicate-report-modal');
  if (!modal || !duplicateReportHistoryId(report)) return Promise.resolve(null);
  const versionCount = Number(report?.version_count || 1) || 1;
  const activeVersion = Number(report?.active_version || versionCount) || versionCount;
  let createdText = '';
  if (report?.created_at) {
    const created = new Date(report.created_at);
    if (!Number.isNaN(created.getTime())) createdText = created.toLocaleString('zh-CN', { hour12: false });
  }
  $('duplicate-report-number').textContent = String(report?.report_no || '已有报告');
  $('duplicate-report-name').textContent = String(report?.title || '分析报告');
  $('duplicate-report-meta').textContent = [
    createdText,
    `${versionCount} 个版本`,
    `当前 V${activeVersion}`,
  ].filter(Boolean).join(' · ');
  const instruction = $('duplicate-report-instruction');
  if (instruction) instruction.value = '';
  $('duplicate-report-empty-warning').hidden = false;
  modal.hidden = false;
  document.body.style.overflow = 'hidden';
  window.setTimeout(() => instruction?.focus(), 50);
  return new Promise(resolve => {
    duplicateReportResolve = resolve;
  });
}

$('duplicate-report-instruction')?.addEventListener('input', e => {
  $('duplicate-report-empty-warning').hidden = !!e.target.value.trim();
});
$('btn-duplicate-report-view')?.addEventListener('click', () => {
  closeDuplicateReportModal({ action: 'view', instruction: '' });
});
$('btn-duplicate-report-rerun')?.addEventListener('click', () => {
  closeDuplicateReportModal({
    action: 'rerun',
    instruction: $('duplicate-report-instruction')?.value.trim() || '',
  });
});

// COLUMN_MODEL_START: pure draft operations, also exercised by Node contract tests.
function cloneColumn(value) { return JSON.parse(JSON.stringify(value)); }
function columnQuestionKey(c) {
  const indexes = c.column_indexes?.length ? c.column_indexes : [c.column_index ?? c.index];
  return indexes.filter(v => v != null).map(Number).filter(Number.isFinite).sort((a,b) => a-b).join(':');
}
function isSelectableColumn(c) {
  return !['ignore', 'id', 'mlbbid'].includes(c.role) && !c.empty_column && !c.is_empty && !!columnQuestionKey(c);
}
function isIdentityColumn(c) { return ['id', 'mlbbid'].includes(c.role); }
function isMainConfirmationColumn(c) { return isSelectableColumn(c) || isIdentityColumn(c); }
function isSubjectiveColumn(c) {
  return c.role === 'open_text' || (['single_choice','multi_choice'].includes(c.role) && !!c.other_text && c.other_text.enabled !== false);
}
function columnSourceLabel(c, i, columns = []) {
  if (isIdentityColumn(c)) return '身份';
  const declared = c.question_number ?? c.question_no;
  if (declared != null && String(declared).trim()) return /^q/i.test(String(declared)) ? String(declared) : `Q${declared}`;
  const match = String(c.name || c.name_zh || '').match(/^\s*(Q\s*\d+(?:[.-]\d+)?|第\s*\d+\s*题)(?=[\s:：.、）)]|$)/i);
  if (match) return match[1].replace(/\s/g,'');
  if (!isSelectableColumn(c)) return `字段 ${i + 1}`;
  const ordinal = columns.slice(0, i + 1).filter(isSelectableColumn).length;
  return `Q${ordinal || i + 1}`;
}
function prepareColumnDraft(column) {
  const c = cloneColumn(column);
  c.options = [...(c.options || [])];
  (c.unmatched_values || []).filter(item => item.suggested_handling === 'standard_option').forEach(item => {
    if (!c.options.some(value => String(value).trim().toLocaleLowerCase() === String(item.value).trim().toLocaleLowerCase())) c.options.push(String(item.value));
  });
  c.options_original = [...(c.options_original || c.options)];
  c.value_aliases = cloneColumn(c.value_aliases || {});
  if ((c.unmatched_values || []).some(v => v.suggested_handling !== 'standard_option')) {
    c.unmatched_handling = c.unmatched_handling || (c.other_text?.enabled === false ? 'keep_raw' : 'as_other');
    c.other_text = {...(c.other_text || {}), option:c.other_text?.option || 'Other / 其他', enabled:c.unmatched_handling !== 'keep_raw'};
  }
  return c;
}
function renameColumnOptions(c, entries) {
  const aliases = {};
  const options = [];
  entries.forEach(({value, previous, aliases: values = []}) => {
    value = String(value || '').trim();
    if (!value) return;
    const canonical = options.find(v => v.toLocaleLowerCase() === value.toLocaleLowerCase()) || value;
    if (!options.includes(canonical)) options.push(canonical);
    const inherited = previous && previous !== canonical ? [previous, ...(c.value_aliases?.[previous] || [])] : [];
    aliases[canonical] = [...new Set([...(aliases[canonical] || []), ...values, ...inherited])].filter(v => v && v !== canonical);
  });
  c.options = options;
  c.value_aliases = aliases;
  return c;
}
function serializeColumnDraft(column) {
  const c = cloneColumn(column);
  c.column_indexes = c.column_indexes || [c.column_index ?? c.index];
  const choices = ['single_choice','multi_choice','profile_dim','matrix_single','matrix_multi'];
  if (choices.includes(c.role)) {
    const residuals = (c.unmatched_values || []).filter(v => v.suggested_handling !== 'standard_option');
    if (residuals.length && c.unmatched_handling !== 'keep_raw') {
      c.other_text = {...(c.other_text || {}), enabled:true, option:c.other_text?.option || 'Other / 其他'};
    }
    if (c.other_text?.enabled === false) {
      const other = (c.other_text.option || 'Other / 其他').trim().toLocaleLowerCase();
      c.options = c.options.filter(v => v.trim().toLocaleLowerCase() !== other);
    } else if (c.other_text?.enabled && !c.options.includes(c.other_text.option || 'Other / 其他')) {
      c.options.push(c.other_text.option || 'Other / 其他');
    }
    if (c.unmatched_handling === 'keep_raw' && residuals.length) c.other_text = {...(c.other_text || {}), enabled:false};
  }
  return c;
}
function selectedColumnsForSave(columns, keys) {
  const selected = new Set(keys || []);
  return columns.filter(c => isSelectableColumn(c) && selected.has(columnQuestionKey(c))).map(columnQuestionKey);
}
// COLUMN_MODEL_END

let columnEditor = null;
let columnEditorOpener = null;
function columnRowHTML(c, i) {
  const key = columnQuestionKey(c);
  const selected = isSelectableColumn(c) && (state.selectedQuestionKeys || []).includes(key);
  const name = c.name_zh || c.name || '未命名题目';
  const options = ROLE_OPTIONS.map(([value,label]) => `<option value="${value}" ${c.role===value?'selected':''}>${label}</option>`).join('');
  const preview = (MATRIX_ROLES.includes(c.role) ? c.rows : c.options) || [];
  const responseCount = c.response_count ?? c.valid_response_count;
  const roleClass = ROLE_OPTIONS.some(([value]) => value === c.role) ? ` col-row--role-${c.role}` : '';
  return `<article class="col-row qe-question-row${roleClass}" data-question-row="${i}">
    <label class="qe-question-select"><input type="checkbox" data-question-select="${i}" ${selected?'checked':''} ${isSelectableColumn(c)?'':'disabled'} aria-label="选择 ${esc(name)}" /></label>
    <span class="col-row__num qe-question-number">${esc(columnSourceLabel(c,i,state.columns))}</span>
    <div class="qe-question-main"><strong>${esc(name)}</strong><div class="qe-question-preview">${preview.slice(0,3).map(v=>`<span>${esc(v)}</span>`).join('')}${preview.length>3?`<span>+${preview.length-3}</span>`:''}</div>${c.low_confidence?'<small class="qe-pending">待确认题型</small>':''}${isIdentityColumn(c)?'<small>身份字段 · 保留用于对应回答，不计入分析题数</small>':''}${isSubjectiveColumn(c)&&c.role!=='open_text'?'<small>包含已启用的其他填空</small>':''}</div>
    <span class="qe-response-count">${Number.isFinite(Number(responseCount))&&responseCount!=null?`${Number(responseCount)} 份回复`:''}</span>
    <select class="type-select" data-question-type="${i}" aria-label="${esc(name)}的题型">${options}</select>
    <button class="btn btn--ghost btn--sm" type="button" data-question-edit="${i}">编辑</button>
  </article>`;
}
function visibleQuestionIndexes() {
  return (state.columns || []).map((c,i)=>({c,i})).filter(({c})=>isMainConfirmationColumn(c) && (state.columnFilter==='all' || (state.columnFilter==='subjective'?isSubjectiveColumn(c):c.low_confidence))).map(({i})=>i);
}
function renderQuestionList() {
  if (!state.columns) return;
  const indexes = visibleQuestionIndexes();
  $('col-list').innerHTML = indexes.map(i=>columnRowHTML(state.columns[i],i)).join('') || '<p class="qe-hint">当前列表没有题目。</p>';
  const system = state.columns.map((c,i)=>({c,i})).filter(({c})=>!isMainConfirmationColumn(c));
  $('qe-system-list').innerHTML = system.map(({c,i})=>columnRowHTML(c,i)).join('');
  $('qe-system-columns').hidden = !system.length;
  $('qe-system-summary').textContent = `忽略字段与空白列 · ${system.length} 项（默认收起）`;
  $('qe-question-toolbar').hidden = false;
  const valid = state.columns.filter(isSelectableColumn);
  const selected = selectedColumnsForSave(state.columns, state.selectedQuestionKeys);
  $('col-confirm-count').textContent = `已选 ${selected.length} / ${valid.length} 道题`;
  document.querySelectorAll('[data-question-filter]').forEach(button=>{
    const filter=button.dataset.questionFilter;
    const count = valid.filter(c=>filter==='all'||(filter==='subjective'?isSubjectiveColumn(c):c.low_confidence)).length;
    button.textContent = `${{all:'全部题目',subjective:'主观题',pending:'待确认'}[filter]} ${count}`;
    button.setAttribute('aria-selected',String(state.columnFilter===filter));
  });
  const selectableIndexes = indexes.filter(i=>isSelectableColumn(state.columns[i]));
  const n = selectableIndexes.filter(i=>selected.includes(columnQuestionKey(state.columns[i]))).length;
  $('qe-select-all').checked = !!selectableIndexes.length && n===selectableIndexes.length;
  $('qe-select-all').indeterminate = n>0 && n<selectableIndexes.length;
  $('qe-select-all').disabled = !selectableIndexes.length;
  const quick = state.reportMode==='quick';
  const subjective = valid.filter(c=>selected.includes(columnQuestionKey(c))&&isSubjectiveColumn(c)).length;
  $('qe-selection-hint').textContent = quick ? `已选题目中的 ${subjective} 道主观题（含其他填空）将逐题总结，已选客观题会呈现统计结果。` : '题型可直接修改；选项、矩阵子项与原值映射请点击编辑。';
}
$('qe-columns-panel').addEventListener('click',event=>{
  const filter=event.target.closest('[data-question-filter]');
  if (filter) {state.columnFilter=filter.dataset.questionFilter;renderQuestionList();}
  const edit=event.target.closest('[data-question-edit]');
  if(edit) openColumnEditor(Number(edit.dataset.questionEdit),edit);
});
$('qe-columns-panel').addEventListener('change',event=>{
  if(surveyConfirmationIsLocked()) return;
  const selection=event.target.closest('[data-question-select]');
  if(selection){const key=columnQuestionKey(state.columns[Number(selection.dataset.questionSelect)]); const keys=new Set(state.selectedQuestionKeys);selection.checked?keys.add(key):keys.delete(key);state.selectedQuestionKeys=[...keys];renderQuestionList();}
  const type=event.target.closest('[data-question-type]');
  if(type){state.columns[Number(type.dataset.questionType)].role=type.value;renderQuestionList();}
});
$('qe-select-all').addEventListener('change',event=>{
  const keys=new Set(state.selectedQuestionKeys);
  visibleQuestionIndexes().filter(i=>isSelectableColumn(state.columns[i])).forEach(i=>{const key=columnQuestionKey(state.columns[i]);event.target.checked?keys.add(key):keys.delete(key);});
  state.selectedQuestionKeys=[...keys];renderQuestionList();
});
function editorOptionHTML(value,index,c) {
  return `<div class="qe-option-edit" data-edit-option-row data-previous="${esc(value)}"><input type="checkbox" data-merge-option="${index}" aria-label="合并选项 ${esc(value)}"/><div><label>选项 <textarea data-edit-option rows="2">${esc(value)}</textarea></label><label class="qe-alias-label">对应原值 / 别名（每行一个）<textarea data-edit-alias rows="2">${esc((c.value_aliases[value]||[]).join('\n'))}</textarea></label></div><button class="btn btn--ghost" type="button" data-remove-option="${index}" aria-label="删除选项 ${esc(value)}">×</button></div>`;
}
function renderColumnEditor() {
  const c=columnEditor.draft;
  $('qe-editor-number').textContent=columnSourceLabel(c,columnEditor.index,state.columns);
  $('qe-editor-title').textContent=c.name_zh||c.name||'编辑题目';
  const type=ROLE_OPTIONS.map(([value,label])=>`<option value="${value}" ${value===c.role?'selected':''}>${label}</option>`).join('');
  let html=`<label class="qe-editor-field">题目名称<input data-edit-name value="${esc(c.name_zh||c.name||'')}" /></label><label class="qe-editor-field">题型<select data-edit-role>${type}</select></label><p class="qe-hint">对应原始列：${esc((c.column_indexes||[c.index]).join('、'))}。修改题型会保留其他题型的编辑设置。</p>`;
  if (CHOICE_ROLES.includes(c.role)) {
    html+=`<section><div class="qe-editor-section-title"><h3>选项与原值映射</h3><button class="btn btn--ghost" type="button" data-add-option>添加选项</button></div><p class="qe-hint">重命名会保留原值对应关系；勾选多个选项后可合并。</p><div id="qe-option-rows">${c.options.map((v,i)=>editorOptionHTML(v,i,c)).join('')}</div><div class="qe-merge-bar"><label>合并到 <select data-merge-target>${c.options.map((v,i)=>`<option value="${i}">${esc(v)}</option>`).join('')}</select></label><button class="btn btn--ghost" type="button" data-merge-options>合并勾选项</button></div></section>`;
  }
  if(['multi_choice','matrix_multi'].includes(c.role)) html+=`<label class="qe-editor-field">多选分隔符<input data-edit-delimiter value="${esc(c.delimiter==='\n'?'\\n':(c.delimiter||'，'))}" /><small>换行分隔请填写 \\n</small></label>`;
  if(['scale','matrix_scale'].includes(c.role)) html+=`<div class="qe-editor-range"><label>量表最小值<input type="number" data-edit-min value="${Number(c.scale_min??1)}" /></label><label>量表最大值<input type="number" data-edit-max value="${Number(c.scale_max??5)}" /></label></div>`;
  if(MATRIX_ROLES.includes(c.role)) html+=`<section><h3>矩阵子项</h3>${(c.column_indexes||[]).map((index,i)=>`<label class="qe-editor-field">原始列 ${index}<input data-edit-matrix="${i}" value="${esc(c.rows?.[i]||'')}" placeholder="子项名称" /></label>`).join('')}</section>`;
  if(['single_choice','multi_choice'].includes(c.role)) {
    const residuals=(c.unmatched_values||[]).filter(v=>v.suggested_handling!=='standard_option');
    html+=`<section><h3>其他填空</h3><label class="qe-other-enable"><input type="checkbox" data-edit-other ${c.other_text?.enabled!==false&&c.other_text?'checked':''}/>保留 Other / 其他填空，并纳入主观题分析</label>`;
    if(residuals.length) html+=`<label class="qe-editor-field">未匹配内容处理<select data-edit-unmatched><option value="as_other" ${c.unmatched_handling!=='keep_raw'?'selected':''}>剩余内容按 Other 填空处理</option><option value="keep_raw" ${c.unmatched_handling==='keep_raw'?'selected':''}>剩余内容保留原值统计</option></select></label><p class="qe-hint">如需映射到已有选项，请把原值加入该选项的别名。</p><details><summary>${residuals.length} 种未匹配内容</summary>${residuals.map(v=>`<p>${esc(v.value)} <small>${Number(v.count||0)} 条</small></p>`).join('')}</details>`;
    html+='</section>';
  }
  html+='<label class="qe-other-enable"><input type="checkbox" data-edit-reviewed checked />已人工确认题型与选项</label>';
  $('qe-editor-body').innerHTML=html;
}
function flushColumnEditor() {
  if(!columnEditor) return;
  const c=columnEditor.draft, root=$('qe-editor-body'), value=selector=>root.querySelector(selector)?.value;
  c.name_zh=value('[data-edit-name]')?.trim() || c.name_zh || c.name;
  if(root.querySelector('[data-edit-option-row]')) renameColumnOptions(c,[...root.querySelectorAll('[data-edit-option-row]')].map(row=>({value:row.querySelector('[data-edit-option]').value,previous:row.dataset.previous,aliases:row.querySelector('[data-edit-alias]').value.split('\n').map(v=>v.trim()).filter(Boolean)})));
  else if(CHOICE_ROLES.includes(c.role)) {c.options=[];c.value_aliases={};}
  if(value('[data-edit-delimiter]')!=null)c.delimiter=value('[data-edit-delimiter]')==='\\n'?'\n':value('[data-edit-delimiter]');
  if(value('[data-edit-min]')!=null){c.scale_min=Number(value('[data-edit-min]'));c.scale_max=Number(value('[data-edit-max]'));}
  if(root.querySelector('[data-edit-matrix]'))c.rows=[...root.querySelectorAll('[data-edit-matrix]')].map(el=>el.value.trim());
  if(root.querySelector('[data-edit-other]')) c.other_text={...(c.other_text||{}),option:c.other_text?.option||'Other / 其他',enabled:root.querySelector('[data-edit-other]').checked};
  if(value('[data-edit-unmatched]')!=null){c.unmatched_handling=value('[data-edit-unmatched]');c.other_text.enabled=c.unmatched_handling==='as_other';}
}
function openColumnEditor(index,opener) {
  if(surveyConfirmationIsLocked())return;
  columnEditor={index,draft:cloneColumn(state.columns[index]),initial:JSON.stringify(state.columns[index])};
  columnEditorOpener=opener;$('qe-editor-error').textContent='';renderColumnEditor();$('qe-column-editor').showModal();
}
function closeColumnEditor(discard=false) {
  if(!columnEditor)return;
  flushColumnEditor();
  if(!discard&&JSON.stringify(columnEditor.draft)!==columnEditor.initial&&!window.confirm('题目还有未保存修改。放弃这些修改并关闭？'))return;
  $('qe-column-editor').close();columnEditor=null;columnEditorOpener?.focus();
}
$('qe-editor-close').addEventListener('click',()=>closeColumnEditor());
$('qe-editor-discard').addEventListener('click',()=>closeColumnEditor(true));
$('qe-column-editor').addEventListener('cancel',event=>{event.preventDefault();closeColumnEditor();});
let columnEditorBackdropPressed = false;
function isColumnEditorBackdrop(event) {
  const dialog = $('qe-column-editor');
  if (event.target !== dialog) return false;
  const bounds = dialog.getBoundingClientRect();
  return event.clientX < bounds.left || event.clientX > bounds.right
    || event.clientY < bounds.top || event.clientY > bounds.bottom;
}
$('qe-column-editor').addEventListener('pointerdown', event => {
  columnEditorBackdropPressed = event.button === 0 && isColumnEditorBackdrop(event);
});
$('qe-column-editor').addEventListener('pointercancel', () => { columnEditorBackdropPressed = false; });
$('qe-column-editor').addEventListener('close', () => { columnEditorBackdropPressed = false; });
$('qe-column-editor').addEventListener('click', event => {
  const shouldClose = columnEditorBackdropPressed && isColumnEditorBackdrop(event);
  columnEditorBackdropPressed = false;
  if (shouldClose) closeColumnEditor();
});
$('qe-editor-save').addEventListener('click',()=>{
  flushColumnEditor();const c=columnEditor.draft;
  if(['scale','matrix_scale'].includes(c.role)&&(!Number.isFinite(c.scale_min)||!Number.isFinite(c.scale_max)||c.scale_min>=c.scale_max)){$('qe-editor-error').textContent='量表最小值必须小于最大值。';return;}
  if(MATRIX_ROLES.includes(c.role)&&c.rows.some(v=>!v)){$('qe-editor-error').textContent='请补全矩阵子项名称。';return;}
  const usedAliases=new Map();
  for(const [canonical,aliases] of Object.entries(c.value_aliases||{})){for(const alias of aliases){if(usedAliases.has(alias)&&usedAliases.get(alias)!==canonical){$('qe-editor-error').textContent='同一个原值不能映射到多个选项，请调整别名。';return;}usedAliases.set(alias,canonical);}}
  if(CHOICE_ROLES.includes(c.role)&&!c.options.length){$('qe-editor-error').textContent='请至少保留一个选项。';return;}
  if($('qe-editor-body').querySelector('[data-edit-reviewed]').checked)c.low_confidence=false;
  const index=columnEditor.index;state.columns[index]=cloneColumn(c);$('qe-column-editor').close();columnEditor=null;renderQuestionList();document.querySelector(`[data-question-edit="${index}"]`)?.focus();showToast('题目设置已保存','success');
});
$('qe-editor-body').addEventListener('change',event=>{
  if(event.target.matches('[data-edit-other]')) { const handling=$('qe-editor-body').querySelector('[data-edit-unmatched]'); if(handling)handling.value=event.target.checked?'as_other':'keep_raw'; }
  if(event.target.matches('[data-edit-unmatched]')) { const enabled=$('qe-editor-body').querySelector('[data-edit-other]'); if(enabled)enabled.checked=event.target.value==='as_other'; }
  if(event.target.matches('[data-edit-role]')){const role=event.target.value;flushColumnEditor();columnEditor.draft.role=role;renderColumnEditor();}
});
$('qe-editor-body').addEventListener('click',event=>{
  const add=event.target.closest('[data-add-option]');const remove=event.target.closest('[data-remove-option]');const merge=event.target.closest('[data-merge-options]');
  if(!add&&!remove&&!merge)return;
  const marked=[...$('qe-editor-body').querySelectorAll('[data-merge-option]:checked')].map(el=>Number(el.dataset.mergeOption));
  const target=Number($('qe-editor-body').querySelector('[data-merge-target]')?.value);
  flushColumnEditor();const c=columnEditor.draft;
  if(add)c.options.push('');
  if(remove){const old=c.options.splice(Number(remove.dataset.removeOption),1)[0];delete c.value_aliases[old];}
  if(merge){if(marked.length<2){showToast('请至少勾选两个选项','info');return;}const canonical=c.options[target];if(!canonical)return;c.value_aliases[canonical]=[...new Set([...(c.value_aliases[canonical]||[]),...marked.filter(i=>i!==target).flatMap(i=>[c.options[i],...(c.value_aliases[c.options[i]]||[])])])];c.options=c.options.filter((v,i)=>!marked.includes(i)||i===target);Object.keys(c.value_aliases).forEach(key=>{if(!c.options.includes(key))delete c.value_aliases[key];});}
  renderColumnEditor();
});
function collectConfirmedColumns() {
  return (state.columns || []).map(serializeColumnDraft);
}

$('btn-start-plan').addEventListener('click', () => submitSurveyEntry());

async function startPlan() {
  const btn = $('btn-start-plan');
  if (btn) btn.disabled = true;
  clearPlanInput();
  let confirmData = null;

  // 跑数表模式:列已在上传时确定性建好,跳过题型确认;其余模式先存用户确认的题型
  if (state.mode !== 'crosstab') {
    try {
      const columns = collectConfirmedColumns();
      const resp = await fetch(`/api/columns/${state.sessionId}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ columns, selected_question_keys: selectedColumnsForSave(columns, state.selectedQuestionKeys) }),
      });
      confirmData = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(confirmData.detail || '保存题型失败');
      }
    } catch (e) {
      showToast(`保存题型失败：${e.message}`, 'error');
      if (btn) btn.disabled = false;
      return;
    }

    // Both report focuses use the same data-confirmation context form.
    {
      try {
        const ctx = readContextForm();
        state.contextForm = ctx;
        const ctxResp = await fetch(`/api/survey-context/${state.sessionId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(ctx),
        });
        const ctxData = await ctxResp.json().catch(() => ({}));
        if (!ctxResp.ok) {
          if (ctxResp.status === 404) {
            saveContextDraft();
            preserveContextDraftOnNextUpload = true;
            showToast(
              '当前会话已过期，请点击上方步骤条的"上传数据"重新上传文件；已填写的调研背景不会丢失',
              'error', 6000,
            );
          } else {
            showToast(ctxData.detail || '保存调研背景失败，请重试', 'error');
          }
          if (btn) btn.disabled = false;
          return;
        }
        if (ctxData.duplicate_report && state.mode !== 'quantitative') {
          const duplicate = ctxData.duplicate_report;
          const historyId = duplicateReportHistoryId(duplicate);
          const decision = await promptDuplicateReport(duplicate);
          if (!decision || !historyId) {
            if (btn) btn.disabled = false;
            return;
          }
          if (decision.action === 'view') {
            if (btn) btn.disabled = false;
            if (typeof openHistoryEntry !== 'function') throw new Error('历史报告入口尚未加载，请刷新页面后重试');
            await openHistoryEntry(historyId);
            return;
          }
          if (decision.action === 'rerun') {
            const prepareResp = await fetch(`/api/report/${state.sessionId}/prepare-rerun`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                history_id: historyId,
                instruction: decision.instruction,
                base_version: duplicate.active_version || null,
              }),
            });
            const prepared = await prepareResp.json().catch(() => ({}));
            if (!prepareResp.ok) throw new Error(prepared.detail || '准备重新生成失败');
            const baseVersion = Number(prepared.base_version || duplicate.active_version || 1) || 1;
            const targetVersion = Number(
              prepared.target_version || (Number(duplicate.version_count || 1) + 1),
            ) || 2;
            state.planData = prepared.plan || state.planData;
            state.reportMode = prepared.report_mode || (prepared.report_style === 'quick' ? 'quick' : state.reportMode);
            state.sessionReport.pendingVersionRequest = {
              linkedHistoryId: historyId,
              baseVersion,
              targetVersion,
              instruction: String(prepared.instruction ?? decision.instruction ?? '').trim(),
            };
            const generated = await runStats({
              linkedRerun: true,
              historyId,
              baseVersion,
              targetVersion,
              instruction: state.sessionReport.pendingVersionRequest.instruction,
            });
            if (!generated && btn) btn.disabled = false;
            return;
          }
        }
      } catch (e) {
        showToast(`无法继续：${e.message}`, 'error');
        if (btn) btn.disabled = false;
        return;
      }
    }
  }

  if (state.reportMode === 'quick') {
    await runStats({ quick: true });
    return;
  }

  // 进入 Step 3，开始 AI 规划
  goStep(3);
  $('qe-plan-recovery').hidden = true;
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
    $('plan-thinking').style.display = 'none';
    $('qe-plan-recovery').hidden = false;
    showBlockingFlowError('方案生成失败', e.message);
    btn.disabled = false;
  }
}

// ============================================================
// STEP 3: Plan card
// ============================================================

function showPlanCard(plan, headers) {
  loadReportStyleOptions();
  $('plan-thinking').style.display = 'none';
  $('plan-card').style.display = 'block';
  $('plan-card-content').innerHTML = buildPlanHTML(plan, headers);
  $('plan-input').disabled = false;
  clearPlanInput();
  // 确保按钮状态与输入框同步（修订后面板可能处于 disabled 残留）
  $('btn-plan-ok').disabled = false;
  $('btn-plan-revise').disabled = true;
}

function buildPlanHTML(plan, headers) {
  let html = '';

  const colMap = {};
  for (const c of plan.columns) colMap[c.index] = c;
  const columnDisplayName = idx => {
    const c = colMap[idx];
    const candidates = [c?.name_zh, c?.name, headers && headers[idx]];
    return candidates
      .map(value => String(value || '').trim())
      .find(value => value && !/^(?:列|column|col)\s*\d+$/i.test(value)) || '';
  };
  const humanizePlanText = text => String(text || '')
    .replace(/列\s*(\d+)/g, (_, rawIdx) => {
      const name = columnDisplayName(Number(rawIdx));
      return name ? `「${name}」` : '相关题目';
    })
    .replace(/\b(?:column|col|index)\s*[:#]?\s*(\d+)/gi, (_, rawIdx) => {
      const name = columnDisplayName(Number(rawIdx));
      return name ? `「${name}」` : '相关题目';
    });

  const branchRules = Array.isArray(plan.branch_rules) ? plan.branch_rules : [];
  const cross = Array.isArray(plan.cross_tabs) ? plan.cross_tabs : [];
  const rulesByParent = new Map();
  const ruleByTargetIndex = new Map();
  branchRules.forEach(rule => {
    if (!rulesByParent.has(rule.parent_index)) rulesByParent.set(rule.parent_index, []);
    rulesByParent.get(rule.parent_index).push(rule);
    (rule.targets || []).forEach(target => {
      (target.indexes || []).forEach(idx => ruleByTargetIndex.set(idx, { rule, target }));
    });
  });

  const rolePresentation = role => ({
    profile_dim: ['画像题', '统计各选项人数与占比'],
    single_choice: ['单选题', '统计各选项人数与占比'],
    multi_choice: ['多选题', '统计各选项选择人数与占比'],
    scale: ['量表题', '分析评分分布与集中趋势'],
    open_text: ['开放题', '归纳主要主题、原因与体验反馈'],
    matrix_scale: ['矩阵量表', '按矩阵子项比较评分表现'],
    matrix_single: ['矩阵单选', '按矩阵子项比较单选分布'],
    matrix_multi: ['矩阵多选', '按矩阵子项比较选择分布'],
  }[role] || ['分析题', '结合本题有效回答进行分析']);

  const logicalIndexesFor = (idx, partSet) => {
    const col = colMap[idx];
    if (!col || !MATRIX_ROLES.includes(col.role) || !col.matrix_group) return [idx];
    return [...partSet].filter(otherIdx => {
      const other = colMap[otherIdx];
      return other?.role === col.role && other?.matrix_group === col.matrix_group;
    });
  };

  const logicalQuestionCount = indexes => {
    const keys = new Set();
    indexes.forEach(idx => {
      const col = colMap[idx];
      const key = col?.matrix_group && MATRIX_ROLES.includes(col.role)
        ? `${col.role}:${col.matrix_group}`
        : `column:${idx}`;
      keys.add(key);
    });
    return keys.size;
  };

  const renderQuestion = (idx, number, partSet, visited, nested = false, partFilter = null) => {
    if (visited.has(idx) || !partSet.has(idx)) return '';
    const col = colMap[idx] || {};
    const logicalIndexes = logicalIndexesFor(idx, partSet);
    logicalIndexes.forEach(itemIdx => visited.add(itemIdx));
    const name = col.matrix_group || columnDisplayName(idx) || '未命名题目';
    const [roleLabel, method] = rolePresentation(col.role);
    const applicability = ruleByTargetIndex.get(idx);
    const applicabilityCoveredByPart = Boolean(
      applicability
      && partFilter
      && Number(applicability.rule.parent_index) === Number(partFilter.column_index)
    );
    let itemHTML = `<div class="plan-outline__question${nested ? ' plan-outline__question--nested' : ''}">
      <div class="plan-outline__question-head">
        <span class="plan-outline__number">${esc(number)}</span>
        <span class="plan-outline__question-name">${esc(name)}</span>
        <span class="plan-outline__role">${esc(roleLabel)}</span>
      </div>
      <div class="plan-outline__method">${esc(method)}</div>`;

    if (applicability && !nested && !applicabilityCoveredByPart) {
      const { rule, target } = applicability;
      const options = (rule.allowed_options || []).map(option => `「${option}」`).join(' / ');
      const prefix = rule.confidence === 'medium' ? '疑似条件关系' : '适用范围';
      itemHTML += `<div class="plan-outline__applicability${rule.confidence === 'medium' ? ' is-medium' : ''}">
        <span>${esc(prefix)}</span>
        ${esc(`「${rule.parent_name || '前置题目'}」选择 ${options} · 进入 ${Number(rule.eligible_count || 0)} 人 · 本题 ${Number(target.answered_count || 0)} 条有效回答`)}
      </div>`;
    }

    const childRules = rulesByParent.get(idx) || [];
    if (childRules.length) {
      itemHTML += '<div class="plan-outline__branches">';
      let childCounter = 0;
      childRules.forEach(rule => {
        const options = (rule.allowed_options || []).map(option => `「${option}」`).join(' / ');
        const confidenceLabel = rule.confidence === 'medium' ? '疑似' : '已识别';
        const confidenceClass = rule.confidence === 'medium' ? ' is-medium' : '';
        itemHTML += `<div class="plan-outline__branch">
          <div class="plan-outline__branch-condition">
            <span class="plan-outline__confidence${confidenceClass}">${esc(confidenceLabel)}</span>
            <span>选择 ${esc(options)}</span>
            <span class="plan-outline__sample">进入该分支 ${Number(rule.eligible_count || 0)} 人</span>
          </div>
          <div class="plan-outline__branch-children">`;
        const externalTargets = [];
        (rule.targets || []).forEach(target => {
          const targetIdx = (target.indexes || []).find(targetIndex => partSet.has(targetIndex));
          if (targetIdx == null) {
            externalTargets.push(target.name || '后续题目');
            return;
          }
          childCounter += 1;
          itemHTML += renderQuestion(targetIdx, `${number}.${childCounter}`, partSet, visited, true, partFilter);
        });
        if (externalTargets.length) {
          itemHTML += `<div class="plan-outline__external">其他章节继续分析：${esc(externalTargets.join('、'))}</div>`;
        }
        itemHTML += '</div></div>';
      });
      itemHTML += '</div>';
    }
    itemHTML += '</div>';
    return itemHTML;
  };

  const analysisFocus = plan.analysis_focus && typeof plan.analysis_focus === 'object'
    ? plan.analysis_focus
    : null;
  if (analysisFocus) {
    const focusText = value => (Array.isArray(value) ? value : [value])
      .map(item => String(item || '').trim())
      .filter(Boolean)
      .join('；');
    const focusItems = [
      ['核心问题', focusText(analysisFocus.core_question)],
      ['报告主线', focusText(analysisFocus.report_organization)],
      ['辅助分析', focusText(analysisFocus.supporting_analyses)],
      ['证据作用', focusText(analysisFocus.evidence_role)],
      ['预期交付', focusText(analysisFocus.expected_deliverables)],
      ['避免结构', focusText(analysisFocus.avoid_structures)],
    ].filter(([, value]) => value);

    if (focusItems.length) {
      html += `<section class="plan-focus-card" aria-label="本次分析重点">
        <div class="plan-focus-card__title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M12 2v3M22 12h-3M12 22v-3M2 12h3"/></svg>
          本次分析重点
        </div>
        <div class="plan-focus-card__items">
          ${focusItems.map(([label, value]) => `<div class="plan-focus-card__item">
            <span class="plan-focus-card__label">${esc(label)}</span>
            <span class="plan-focus-card__value">${esc(value)}</span>
          </div>`).join('')}
        </div>
      </section>`;
    }
  }

  // 1. 报告章节大纲
  html += `<div class="plan-section">
    <div class="plan-section__title">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
      报告分析大纲
    </div>
    <div class="plan-outline">`;

  for (let i = 0; i < plan.parts.length; i++) {
    const p = plan.parts[i];
    const indexes = Array.isArray(p.column_indexes) ? p.column_indexes : [];
    const partFilter = p.filter && typeof p.filter === 'object' ? p.filter : null;
    const partFilterOptions = Array.isArray(partFilter?.allowed_options) ? partFilter.allowed_options : [];
    const partFilterName = partFilter ? (columnDisplayName(partFilter.column_index) || '筛选题目') : '';
    const partSet = new Set(indexes);
    const partBranchRules = branchRules.filter(rule => {
      if (partFilter && Number(rule.parent_index) === Number(partFilter.column_index)) return false;
      return partSet.has(rule.parent_index)
        || (rule.targets || []).some(target => (target.indexes || []).some(idx => partSet.has(idx)));
    });
    const summaryParts = [];
    if (indexes.length) summaryParts.push(`${logicalQuestionCount(indexes)} 道题`);
    if (partBranchRules.length) summaryParts.push(`${partBranchRules.length} 组条件关系`);
    if (partFilter && partFilterOptions.length) summaryParts.push('1 组人群筛选');
    if (!summaryParts.length && p.scope) summaryParts.push(p.scope);

    html += `<details class="plan-outline__part" open>
      <summary class="plan-outline__part-summary">
        <span class="plan-outline__part-num">Part ${i + 1}</span>
        <span class="plan-outline__part-title">${esc(p.name)}</span>
        <span class="plan-outline__part-meta">${esc(summaryParts.join(' · '))}</span>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
      </summary>
      <div class="plan-outline__part-body">`;

    if (partFilter && partFilterOptions.length) {
      const options = partFilterOptions.map(option => `「${option}」`).join(' / ');
      html += `<div class="plan-outline__applicability">
        <span>本章适用人群</span>
        ${esc(`「${partFilterName}」选择 ${options}；本章统计、满意度与开放反馈仅使用该组玩家`)}
      </div>`;
    }

    if (!indexes.length) {
      html += `<div class="plan-outline__scope">${esc(p.scope || '按本章节主题进行综合分析')}</div>`;
    } else {
      const nestedIndexes = new Set();
      branchRules.forEach(rule => {
        if (!partSet.has(rule.parent_index)) return;
        (rule.targets || []).forEach(target => {
          (target.indexes || []).forEach(targetIdx => {
            if (partSet.has(targetIdx)) nestedIndexes.add(targetIdx);
          });
        });
      });
      const visited = new Set();
      let rootCounter = 0;
      indexes.forEach(idx => {
        if (nestedIndexes.has(idx) || visited.has(idx)) return;
        rootCounter += 1;
        html += renderQuestion(idx, `${i + 1}.${rootCounter}`, partSet, visited, false, partFilter);
      });
      // 容错：若父题在其他 Part，仍展示未渲染的问题及其适用条件。
      indexes.forEach(idx => {
        if (visited.has(idx)) return;
        rootCounter += 1;
        html += renderQuestion(idx, `${i + 1}.${rootCounter}`, partSet, visited, false, partFilter);
      });
    }

    const partCross = cross.filter(item => partSet.has(item.question_index));
    if (partCross.length) {
      html += `<div class="plan-outline__supplement">
        <div class="plan-outline__supplement-title">补充分析</div>
        <div class="plan-cross">`;
      partCross.forEach(item => {
        const profileName = columnDisplayName(item.profile_index) || '相关画像题目';
        const questionName = columnDisplayName(item.question_index) || '相关分析题目';
        html += `<div class="plan-cross-item">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          ${esc(profileName)} × ${esc(questionName)}
        </div>`;
      });
      html += '</div></div>';
    }
    html += '</div></details>';
  }
  html += `</div></div>`;

  // 2. 待确认的分析思路
  const openQs = plan.open_questions || [];
  if (openQs.length) {
    html += `<div class="plan-section">
      <div class="plan-section__title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        待确认的分析思路
      </div>
      <div class="plan-questions">`;
    openQs.forEach((q, i) => {
      html += `<div class="plan-question">
        <span class="plan-question__num">Q${i + 1}</span>
        <span>${esc(humanizePlanText(q))}</span>
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
  confirmPlan(txt);
});
$('plan-input').addEventListener('input', syncPlanActionButtons);
$('plan-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    const txt = $('plan-input').value.trim();
    if (txt) {
      confirmPlan(txt);
    }
  }
});

async function confirmPlan(text) {
  const reportStyle = selectedReportStyle();
  lockReportStyleSelection(true);
  $('plan-input').disabled = true;
  $('btn-plan-ok').disabled = true;
  $('btn-plan-revise').disabled = true;

  try {
    let approved = false;
    let approvalWarning = '';
    let newPlan = null;
    let newHeaders = null;

    if (text.toLowerCase() === 'ok') {
      $('plan-thinking').style.display = 'flex';
      $('plan-thinking').querySelector('.thinking-block__title').textContent = '正在确认方案…';
      $('plan-stream-text').textContent = '';
      $('plan-card').style.display = 'none';

      await consumeSSEPost('/api/plan/confirm', {
        session_id: state.sessionId,
        user_text: text,
        report_style: reportStyle,
        plan: state.planData,
      }, ev => {
        if (ev.type === 'progress') {
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
          approvalWarning = String(ev.warning || '').trim();
        }
      });

      if (newPlan) {
        state.planData = newPlan;
        showPlanCard(newPlan, newHeaders);
        showToast('方案已修订，请再次确认', 'success');
        return;
      }
      if (!approved) throw new Error('未收到确认结果，请重试');
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
      if (!approved) throw new Error('未收到修订后的方案，请重试');
    }

    if (approved) {
      if (approvalWarning) showToast(approvalWarning, 'info', 8000);
      $('plan-thinking').style.display = 'none';
      $('plan-card').style.display = 'block';
      await runStats();
    }
  } catch (e) {
    showBlockingFlowError('方案修订失败', e.message);
    lockReportStyleSelection(false);
    // 修订失败时恢复方案卡片（隐藏 thinking 区，避免用户看到空白）
    if (state.planData) {
      $('plan-thinking').style.display = 'none';
      $('plan-card').style.display = 'block';
    }
    $('plan-input').disabled = false;
    syncPlanActionButtons();
  }
}

// Manual outline changes are local until the same confirmed plan is submitted.
let manualPlanEditor = null;
function flushManualPlanEditor() {
  if (!manualPlanEditor) return;
  document.querySelectorAll('[data-plan-part]').forEach(row => {
    const part = manualPlanEditor.draft.parts[Number(row.dataset.planPart)];
    part.name = row.querySelector('[data-plan-name]').value.trim();
    part.scope = row.querySelector('[data-plan-scope]').value.trim();
    part.column_indexes = [...row.querySelectorAll('[data-plan-column]:checked')].map(input => Number(input.value));
  });
}
function renderManualPlanEditor() {
  const plan = manualPlanEditor.draft;
  $('qe-plan-editor-body').innerHTML = plan.parts.map((part,i) => `<section class="qe-plan-part-editor" data-plan-part="${i}"><label class="qe-editor-field">章节 ${i+1}<input data-plan-name value="${esc(part.name||'')}" /></label><label class="qe-editor-field">分析重点<textarea data-plan-scope rows="2">${esc(part.scope||'')}</textarea></label><details><summary>本章题目 · ${(part.column_indexes||[]).length} 项</summary>${(plan.columns||[]).filter(c=>!['ignore','id','mlbbid'].includes(c.role)).map(c=>`<label class="qe-other-enable"><input type="checkbox" data-plan-column value="${Number(c.index)}" ${(part.column_indexes||[]).includes(c.index)?'checked':''} />${esc(c.name_zh||c.name||('原始列 '+c.index))}</label>`).join('')}</details><footer><button class="btn btn--ghost" type="button" data-plan-move="${i}" data-direction="-1" ${i===0?'disabled':''}>上移</button><button class="btn btn--ghost" type="button" data-plan-move="${i}" data-direction="1" ${i===plan.parts.length-1?'disabled':''}>下移</button><button class="btn btn--ghost" type="button" data-plan-remove="${i}" ${plan.parts.length===1?'disabled':''}>删除章节</button></footer></section>`).join('')+'<button class="btn btn--ghost" type="button" data-plan-add>添加章节</button>';
}
$('qe-plan-edit').addEventListener('click',()=>{
  if (!state.planData || state.currentStep!==3 || $('plan-input').disabled) return;
  manualPlanEditor={draft:cloneColumn(state.planData),initial:JSON.stringify(state.planData)};
  renderManualPlanEditor();$('qe-plan-editor').showModal();
});
function closeManualPlanEditor(discard=false) {
  if(!manualPlanEditor)return;
  flushManualPlanEditor();
  if(!discard&&JSON.stringify(manualPlanEditor.draft)!==manualPlanEditor.initial&&!window.confirm('分析结构还有未保存修改。放弃修改并关闭？'))return;
  manualPlanEditor=null;$('qe-plan-editor').close();$('qe-plan-edit').focus();
}
$('qe-plan-editor-close').addEventListener('click',()=>closeManualPlanEditor());
$('qe-plan-editor-discard').addEventListener('click',()=>closeManualPlanEditor(true));
$('qe-plan-editor').addEventListener('cancel',event=>{event.preventDefault();closeManualPlanEditor();});
$('qe-plan-editor-body').addEventListener('click',event=>{
  const move=event.target.closest('[data-plan-move]'),remove=event.target.closest('[data-plan-remove]'),add=event.target.closest('[data-plan-add]');
  if(!move&&!remove&&!add)return;
  flushManualPlanEditor();const parts=manualPlanEditor.draft.parts;
  if(move){const i=Number(move.dataset.planMove),j=i+Number(move.dataset.direction);[parts[i],parts[j]]=[parts[j],parts[i]];}
  if(remove&&parts.length>1)parts.splice(Number(remove.dataset.planRemove),1);
  if(add)parts.push({name:'新增章节',scope:'',column_indexes:[]});
  renderManualPlanEditor();
});
$('qe-plan-editor-save').addEventListener('click',()=>{
  flushManualPlanEditor();
  if(manualPlanEditor.draft.parts.some(part=>!part.name)){showToast('请填写每个章节的名称','info');return;}
  state.planData=cloneColumn(manualPlanEditor.draft);manualPlanEditor=null;$('qe-plan-editor').close();showPlanCard(state.planData,[]);showToast('分析结构已保存；确认方案后开始生成','success');
});
window.addEventListener('beforeunload',event=>{
  if(columnEditor){flushColumnEditor();if(JSON.stringify(columnEditor.draft)!==columnEditor.initial){event.preventDefault();event.returnValue='';}}
  if(manualPlanEditor){flushManualPlanEditor();if(JSON.stringify(manualPlanEditor.draft)!==manualPlanEditor.initial){event.preventDefault();event.returnValue='';}}
});
