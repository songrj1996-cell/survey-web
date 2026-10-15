// 奖励整理：Excel sheet 选择、字段映射、冲突审阅与严格 TXT 导出。

const rrState = {
  currentStep: 1,
  file: null,
  sessionId: null,
  sheets: [],
  availableDates: [],
  selectedDates: new Set(),
  selectedSheets: new Set(),
  mappings: {},
  analysis: null,
  result: null,
  noteMode: 'simple',
  busy: false,
};

const RR_FIELDS = [
  {key: 'date_col', label: '奖励发放日期', purpose: '筛选本次批次', aliases: ['奖励发放日期', '奖励日期', '日期', 'date']},
  {key: 'task_col', label: '任务', purpose: '按任务统计', aliases: ['任务', '任务名称', 'task']},
  {key: 'player_id_col', label: 'playerid', purpose: '保留原始群身份', aliases: ['playerid', 'player id', 'discordid', 'whatsappid', 'discord', 'whatsapp']},
  {key: 'account_id_col', label: 'MLBBaccountid', purpose: '账号去重与 TXT 拆分', aliases: ['mlbbaccountid', 'mlbb account id', 'ori', '游戏账号']},
  {key: 'status_col', label: 'Excel 原状态（选填）', purpose: '仅作前后对照；是否在群以最新名单为准', aliases: ['当前状态', '状态', 'status'], optional: true, emptyLabel: '不提供（以最新名单为准）'},
  {key: 'normal_reward_col', label: '普通奖励', purpose: '奖励加总项', aliases: ['普通奖励', '基础奖励', 'normal reward']},
  {key: 'excellent_reward_col', label: '优秀追加奖励', purpose: '奖励加总项', aliases: ['优秀额外奖励', '优秀追加奖励', '优秀奖励', 'excellent reward'], optional: true},
  {key: 'other_reward_col', label: '其他奖励', purpose: '奖励加总项', aliases: ['其他奖励', 'other reward'], optional: true},
];

function rrGoStep(step) {
  rrState.currentStep = step;
  [1, 2, 3, 4].forEach(number => {
    const panel = $(`rr-panel-${number}`);
    if (panel) panel.classList.toggle('panel--hidden', number !== step);
  });
  window.scrollTo({top: 0, behavior: 'smooth'});
}

function rrReset() {
  Object.assign(rrState, {
    currentStep: 1,
    file: null,
    sessionId: null,
    sheets: [],
    availableDates: [],
    selectedDates: new Set(),
    selectedSheets: new Set(),
    mappings: {},
    analysis: null,
    result: null,
    noteMode: 'simple',
    busy: false,
  });
  $('rr-file-input').value = '';
  $('rr-file-card').hidden = true;
  $('rr-file-card').innerHTML = '';
  $('rr-btn-upload').disabled = true;
  $('rr-date-tree').innerHTML = '';
  $('rr-sheet-list').innerHTML = '';
  $('rr-mapping-list').innerHTML = '';
  $('rr-analysis-summary').innerHTML = '';
  $('rr-invalid-list').innerHTML = '';
  $('rr-conflict-list').innerHTML = '';
  rrGoStep(1);
}

async function rrApi(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get('content-type') || '';
  const data = contentType.includes('application/json')
    ? await response.json()
    : {detail: await response.text()};
  if (!response.ok) throw new Error(data.detail || '操作失败，请稍后重试');
  return data;
}

function rrSetBusy(button, busy, busyText) {
  rrState.busy = busy;
  if (!button) return;
  if (!button.dataset.idleText) button.dataset.idleText = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? busyText : button.dataset.idleText;
}

function rrChooseFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.xlsx')) {
    showToast('奖励整理目前仅支持 .xlsx 文件', 'error');
    return;
  }
  rrState.file = file;
  $('rr-file-card').hidden = false;
  $('rr-file-card').innerHTML = `
    <div><strong>${esc(file.name)}</strong><span>${(file.size / 1024 / 1024).toFixed(2)} MB</span></div>
    <button class="btn btn--ghost btn--sm" id="rr-btn-change-file" type="button">更换文件</button>`;
  $('rr-btn-change-file').addEventListener('click', () => $('rr-file-input').click());
  $('rr-btn-upload').disabled = false;
}

function rrNormalizeHeader(value) {
  return String(value || '').toLowerCase().replace(/[\s_\-（）()：:]/g, '');
}

function rrAutoColumn(headers, field) {
  const normalized = headers.map(rrNormalizeHeader);
  const aliases = field.aliases.map(rrNormalizeHeader);
  let index = normalized.findIndex(header => aliases.includes(header));
  if (index >= 0) return index;
  index = normalized.findIndex(header => aliases.some(alias => alias.length >= 3 && header.includes(alias)));
  return index >= 0 ? index : (field.optional ? null : -1);
}

const RR_MONTHS = ['一月', '二月', '三月', '四月', '五月', '六月', '七月', '八月', '九月', '十月', '十一月', '十二月'];

function rrDateGroups() {
  const years = new Map();
  rrState.availableDates.forEach(item => {
    const [year, month, day] = item.date.split('-');
    if (!years.has(year)) years.set(year, new Map());
    const months = years.get(year);
    if (!months.has(month)) months.set(month, []);
    months.get(month).push({...item, day});
  });
  return years;
}

function rrDateCount(items) {
  return items.reduce((sum, item) => sum + Number(item.row_count || 0), 0);
}

function rrRenderDateTree() {
  const groups = rrDateGroups();
  if (!rrState.availableDates.length) {
    $('rr-date-tree').innerHTML = '<div class="rr-empty">Excel 中没有读取到可识别的奖励日期</div>';
    $('rr-date-selected-count').textContent = '未选择';
    rrUpdateAnalyzeButton();
    return;
  }
  const allCount = rrDateCount(rrState.availableDates);
  const yearsHtml = [...groups.entries()].sort(([a], [b]) => b.localeCompare(a)).map(([year, months]) => {
    const yearItems = [...months.values()].flat();
    const monthsHtml = [...months.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([month, items]) => `
      <details class="rr-date-branch rr-date-month" open>
        <summary>
          <label><input type="checkbox" data-rr-date-group="month" data-rr-year="${year}" data-rr-month="${month}" />
            <span>${RR_MONTHS[Number(month) - 1] || `${Number(month)}月`}</span><em>${rrDateCount(items)} 条</em>
          </label>
        </summary>
        <div class="rr-date-days">${items.sort((a, b) => a.date.localeCompare(b.date)).map(item => `
          <label class="rr-date-day"><input type="checkbox" data-rr-date="${item.date}" />
            <span>${Number(item.day)} 日</span><em>${item.row_count} 条</em>
          </label>`).join('')}</div>
      </details>`).join('');
    return `
      <details class="rr-date-branch rr-date-year" open>
        <summary>
          <label><input type="checkbox" data-rr-date-group="year" data-rr-year="${year}" />
            <span>${year} 年</span><em>${rrDateCount(yearItems)} 条</em>
          </label>
        </summary>
        <div class="rr-date-months">${monthsHtml}</div>
      </details>`;
  }).join('');
  $('rr-date-tree').innerHTML = `
    <label class="rr-date-all"><input type="checkbox" data-rr-date-group="all" />
      <span>全选日期</span><em>${allCount} 条</em>
    </label>${yearsHtml}`;

  $('rr-date-tree').querySelectorAll('summary input').forEach(input => {
    input.addEventListener('click', event => event.stopPropagation());
  });
  $('rr-date-tree').querySelectorAll('[data-rr-date]').forEach(input => {
    input.addEventListener('change', () => {
      if (input.checked) rrState.selectedDates.add(input.dataset.rrDate);
      else rrState.selectedDates.delete(input.dataset.rrDate);
      rrSyncDateSelection();
    });
  });
  $('rr-date-tree').querySelectorAll('[data-rr-date-group]').forEach(input => {
    input.addEventListener('change', () => {
      const dates = rrState.availableDates.filter(item => {
        if (input.dataset.rrDateGroup === 'all') return true;
        if (!item.date.startsWith(`${input.dataset.rrYear}-`)) return false;
        return input.dataset.rrDateGroup === 'year'
          || item.date.startsWith(`${input.dataset.rrYear}-${input.dataset.rrMonth}-`);
      });
      dates.forEach(item => {
        if (input.checked) rrState.selectedDates.add(item.date);
        else rrState.selectedDates.delete(item.date);
      });
      rrSyncDateSelection();
    });
  });
  rrSyncDateSelection();
}

function rrSyncDateSelection() {
  $('rr-date-tree').querySelectorAll('[data-rr-date]').forEach(input => {
    input.checked = rrState.selectedDates.has(input.dataset.rrDate);
  });
  $('rr-date-tree').querySelectorAll('[data-rr-date-group]').forEach(input => {
    const dates = rrState.availableDates.filter(item => {
      if (input.dataset.rrDateGroup === 'all') return true;
      if (!item.date.startsWith(`${input.dataset.rrYear}-`)) return false;
      return input.dataset.rrDateGroup === 'year'
        || item.date.startsWith(`${input.dataset.rrYear}-${input.dataset.rrMonth}-`);
    });
    const checkedCount = dates.filter(item => rrState.selectedDates.has(item.date)).length;
    input.checked = Boolean(dates.length) && checkedCount === dates.length;
    input.indeterminate = checkedCount > 0 && checkedCount < dates.length;
  });
  $('rr-date-selected-count').textContent = rrState.selectedDates.size
    ? `已选 ${rrState.selectedDates.size} 个日期`
    : '未选择';
  rrUpdateAnalyzeButton();
}

function rrRenderSheets() {
  $('rr-sheet-list').innerHTML = rrState.sheets.map(sheet => `
    <label class="rr-sheet-option${sheet.usable ? '' : ' rr-sheet-option--disabled'}">
      <input type="checkbox" data-rr-sheet="${esc(sheet.name)}" ${sheet.usable ? '' : 'disabled'} />
      <span class="rr-sheet-option__body">
        <strong>${esc(sheet.name)}</strong>
        <small>${sheet.usable ? `${sheet.row_count} 条数据 · ${sheet.headers.length} 列` : '未识别到可用表头'}</small>
      </span>
    </label>`).join('');
  $('rr-sheet-list').querySelectorAll('[data-rr-sheet]').forEach(input => {
    input.addEventListener('change', () => {
      if (input.checked) rrState.selectedSheets.add(input.dataset.rrSheet);
      else rrState.selectedSheets.delete(input.dataset.rrSheet);
      rrSyncSheetSelection();
      rrRenderMappings();
    });
  });
  rrSyncSheetSelection();
  rrRenderMappings();
}

function rrSyncSheetSelection() {
  $('rr-sheet-list').querySelectorAll('[data-rr-sheet]').forEach(input => {
    input.checked = rrState.selectedSheets.has(input.dataset.rrSheet);
  });
  const selectedCount = rrState.selectedSheets.size;
  $('rr-sheet-selected-count').textContent = selectedCount ? `已选 ${selectedCount} 个` : '未选择';
}

function rrMappingSelect(sheet, field, selected) {
  const headerCounts = new Map();
  sheet.headers.forEach(header => headerCounts.set(header, (headerCounts.get(header) || 0) + 1));
  const options = sheet.headers.map((header, index) => `
    <option value="${index}" ${index === selected ? 'selected' : ''}>${esc(header)}${headerCounts.get(header) > 1 ? `（第 ${index + 1} 列）` : ''}</option>`).join('');
  return `
    <select class="plan-input rr-mapping-select" aria-label="${esc(field.label)}映射到${esc(sheet.name)}"
      data-rr-map-sheet="${esc(sheet.name)}" data-rr-map-field="${field.key}">
      <option value="" ${selected === -1 ? 'selected' : ''}>请选择</option>
      ${field.optional ? `<option value="none" ${selected === null ? 'selected' : ''}>${esc(field.emptyLabel || '此项为 0（无对应列）')}</option>` : ''}${options}
    </select>`;
}

function rrRenderMappings() {
  const selected = rrState.sheets.filter(sheet => rrState.selectedSheets.has(sheet.name));
  if (!selected.length) {
    $('rr-mapping-list').innerHTML = '<div class="rr-empty">选择 sheet 后显示字段映射</div>';
    rrUpdateAnalyzeButton();
    return;
  }
  selected.forEach(sheet => {
    const mapping = rrState.mappings[sheet.name] || {};
    RR_FIELDS.forEach(field => {
      if (mapping[field.key] === undefined) mapping[field.key] = rrAutoColumn(sheet.headers, field);
    });
    rrState.mappings[sheet.name] = mapping;
  });
  const minWidth = 400 + (selected.length * 240);
  const columns = `210px repeat(${selected.length}, minmax(240px, 1fr)) minmax(190px, .8fr)`;
  const headerCells = selected.map(sheet => `
    <div class="rr-mapping-cell rr-mapping-head rr-mapping-head--sheet">
      <strong>${esc(sheet.name)}</strong>
      <small>${sheet.row_count} 条 · 表头第 ${sheet.header_row} 行</small>
    </div>`).join('');
  const fieldRows = RR_FIELDS.map((field, index) => {
    const lastClass = index === RR_FIELDS.length - 1 ? ' rr-mapping-cell--last' : '';
    const sheetCells = selected.map(sheet => `
      <div class="rr-mapping-cell rr-mapping-value${lastClass}">
        ${rrMappingSelect(sheet, field, rrState.mappings[sheet.name][field.key])}
      </div>`).join('');
    return `
      <div class="rr-mapping-cell rr-mapping-field-name${lastClass}"><strong>${esc(field.label)}</strong></div>
      ${sheetCells}
      <div class="rr-mapping-cell rr-mapping-purpose${lastClass}">${esc(field.purpose)}</div>`;
  }).join('');
  $('rr-mapping-list').innerHTML = `
    <div class="rr-mapping-table-wrap">
      <div class="rr-mapping-table" style="grid-template-columns:${columns};min-width:${minWidth}px">
        <div class="rr-mapping-cell rr-mapping-head rr-mapping-head--tool">工具字段</div>
        ${headerCells}
        <div class="rr-mapping-cell rr-mapping-head rr-mapping-head--purpose">用途</div>
        ${fieldRows}
      </div>
    </div>`;
  $('rr-mapping-list').querySelectorAll('[data-rr-map-field]').forEach(select => {
    select.addEventListener('change', () => {
      const value = select.value === '' ? -1 : (select.value === 'none' ? null : Number(select.value));
      rrState.mappings[select.dataset.rrMapSheet][select.dataset.rrMapField] = value;
      rrUpdateAnalyzeButton();
    });
  });
  rrUpdateAnalyzeButton();
}

function rrMappingComplete(sheetName) {
  const mapping = rrState.mappings[sheetName] || {};
  const valid = RR_FIELDS.every(field => {
    const value = mapping[field.key];
    return field.optional && value === null ? true : Number.isInteger(value) && value >= 0;
  });
  const selectedColumns = RR_FIELDS.map(field => mapping[field.key]).filter(Number.isInteger);
  return valid && new Set(selectedColumns).size === selectedColumns.length;
}

function rrUpdateAnalyzeButton() {
  const selected = [...rrState.selectedSheets];
  $('rr-btn-analyze').disabled = rrState.busy
    || !selected.length
    || !rrState.selectedDates.size
    || !selected.every(rrMappingComplete);
}

async function rrUpload() {
  if (!rrState.file || rrState.busy) return;
  const button = $('rr-btn-upload');
  rrSetBusy(button, true, '正在读取…');
  const form = new FormData();
  form.append('file', rrState.file);
  try {
    const data = await rrApi('/api/reward-roster/upload', {method: 'POST', body: form});
    rrState.sessionId = data.session_id;
    rrState.sheets = data.sheets || [];
    rrState.availableDates = data.available_dates || [];
    rrState.selectedDates = new Set();
    rrState.selectedSheets = new Set();
    rrState.mappings = {};
    $('rr-eligible-status').textContent = data.eligible_status || '群内人员';
    rrRenderDateTree();
    rrRenderSheets();
    rrGoStep(2);
  } catch (error) {
    showToast(error.message, 'error', 6000);
  } finally {
    rrSetBusy(button, false, '');
  }
}

async function rrAnalyze() {
  if (rrState.busy || !rrState.selectedDates.size) return;
  const selected = [...rrState.selectedSheets];
  if (!selected.length || !selected.every(rrMappingComplete)) {
    showToast('请先选择 sheet、补齐必填字段，并确保所选列没有重复映射', 'error');
    return;
  }
  const mappings = Object.fromEntries(selected.map(name => [name, rrState.mappings[name]]));
  const button = $('rr-btn-analyze');
  rrSetBusy(button, true, '正在检查…');
  try {
    const data = await rrApi(`/api/reward-roster/${encodeURIComponent(rrState.sessionId)}/analyze`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({selected_sheets: selected, reward_dates: [...rrState.selectedDates].sort(), mappings}),
    });
    rrState.analysis = data;
    if (data.invalid_rows.length) {
      rrRenderAnalysis(data); rrGoStep(3);
    } else {
      await window.RewardBatches.acceptImport(rrState.sessionId, [...rrState.selectedDates]);
    }
  } catch (error) {
    showToast(error.message, 'error', 7000);
  } finally {
    rrSetBusy(button, false, '');
    rrUpdateAnalyzeButton();
  }
}

function rrReasonLabel(reason) {
  return ({
    cross_sheet_account: '账号在多个 sheet 中出现',
    cross_sheet_same_task: '同一任务跨 sheet 重复',
    same_sheet_duplicate: '同一 sheet 内存在重复记录',
  })[reason] || reason;
}

function rrRenderAnalysis(data) {
  $('rr-analysis-summary').innerHTML = `
    <div class="rr-analysis-banner">
      <div><strong>${data.conflicts.length ? `发现 ${data.conflicts.length} 组需要人工确认的账号` : '未发现账号冲突'}</strong>
      <span>${data.eligible_row_count} 条奖励来源，未处理前合计 ${data.potential_total_diamonds} 钻</span></div>
      <span class="rr-status-pill">${data.invalid_rows.length ? `${data.invalid_rows.length} 条异常` : `${data.conflicts.length} 组待确认`}</span>
    </div>`;
  $('rr-invalid-list').innerHTML = data.invalid_rows.length ? `
    <section class="rr-error-surface">
      <div class="rr-section-title">Excel 中有 ${data.invalid_rows.length} 条无效数据</div>
      <p>为避免错发，异常数据不能在页面内猜测修复。请修改 Excel 后重新上传。</p>
      <div class="rr-invalid-rows">${data.invalid_rows.map(row => `<div><strong>${esc(row.sheet)} · 第 ${row.row_number} 行</strong><span>${esc(row.issue)}</span></div>`).join('')}</div>
    </section>` : '';
  $('rr-conflict-list').innerHTML = data.conflicts.map(rrConflictHtml).join('');
  $('rr-btn-resolve').disabled = Boolean(data.invalid_rows.length) || !data.conflicts.length;
  if (data.invalid_rows.length) $('rr-btn-resolve').textContent = '请先修正 Excel 并重新上传';
  else $('rr-btn-resolve').textContent = '确认全部处理结果';
}

function rrConflictHtml(conflict) {
  const byTask = new Map();
  conflict.rows.forEach(row => {
    const key = `${row.date}\u0000${row.task}`;
    if (!byTask.has(key)) byTask.set(key, {date: row.date, task: row.task, rows: []});
    byTask.get(key).rows.push(row);
  });
  const groups = [...byTask.values()].map(({date, task, rows}, taskIndex) => {
    const choices = rows.map(row => {
      const inputType = rows.length > 1 ? 'radio' : 'checkbox';
      const checked = rows.length === 1 ? 'checked' : '';
      return `
        <label class="rr-source-choice">
          <input type="${inputType}" name="rr-${conflict.conflict_id}-${taskIndex}" value="${esc(row.row_id)}" data-rr-row-choice ${checked} />
          <span><strong>${esc(row.sheet)} · 第 ${row.row_number} 行</strong><small>原始 playerid：${esc(row.player_id || '空')}</small></span>
          <b>${row.total_reward} 钻</b>
        </label>`;
    }).join('');
    const exclude = rows.length > 1 ? `
      <label class="rr-source-choice rr-source-choice--exclude">
        <input type="radio" name="rr-${conflict.conflict_id}-${taskIndex}" value="" data-rr-row-choice />
        <span><strong>该任务不发放</strong><small>排除以上全部重复记录</small></span>
      </label>` : '';
    return `<div class="rr-task-choice"><div class="rr-task-choice__title"><span><strong>${esc(task)}</strong><small>${esc(date)}</small></span>${rows.length > 1 ? '<em>只能保留一条</em>' : '<em>可保留或取消</em>'}</div>${choices}${exclude}</div>`;
  }).join('');
  return `
    <section class="rr-surface rr-conflict-card" data-rr-conflict="${esc(conflict.conflict_id)}">
      <div class="rr-section-head"><div><div class="rr-section-title">${esc(conflict.account)}</div><div class="rr-section-hint">${esc((conflict.player_ids || []).join(' / ') || '无原始 playerid')}</div></div><span class="rr-status-pill rr-status-pill--warn">待确认</span></div>
      <div class="rr-reason-list">${conflict.reasons.map(reason => `<span>${esc(rrReasonLabel(reason))}</span>`).join('')}</div>
      <div class="rr-task-choice-list">${groups}</div>
      <label class="rr-review-confirm"><input type="checkbox" data-rr-reviewed /><span>我已核对该账号，以上勾选就是最终保留的奖励来源</span></label>
    </section>`;
}

async function rrResolve() {
  if (rrState.busy || !rrState.analysis) return;
  const cards = [...$('rr-conflict-list').querySelectorAll('[data-rr-conflict]')];
  const unreviewed = cards.filter(card => !card.querySelector('[data-rr-reviewed]').checked);
  if (unreviewed.length) {
    showToast(`还有 ${unreviewed.length} 个账号未勾选“已核对”`, 'error');
    unreviewed[0].scrollIntoView({behavior: 'smooth', block: 'center'});
    return;
  }
  const resolutions = cards.map(card => ({
    conflict_id: card.dataset.rrConflict,
    selected_row_ids: [...card.querySelectorAll('[data-rr-row-choice]:checked')]
      .map(input => input.value).filter(Boolean),
  }));
  const button = $('rr-btn-resolve');
  rrSetBusy(button, true, '正在生成名单…');
  try {
    const data = await rrApi(`/api/reward-roster/${encodeURIComponent(rrState.sessionId)}/resolve`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({resolutions}),
    });
    rrRenderResult(data.result);
    rrGoStep(4);
  } catch (error) {
    showToast(error.message, 'error', 7000);
  } finally {
    rrSetBusy(button, false, '');
  }
}

function rrRenderResult(result) {
  rrState.result = result;
  rrState.noteMode = 'simple';
  $('rr-result-summary').innerHTML = `
    <div class="rr-summary-card"><span>唯一账号</span><strong>${result.unique_accounts}</strong></div>
    <div class="rr-summary-card"><span>奖励总额</span><strong>${result.total_diamonds} 钻</strong></div>
    <div class="rr-summary-card"><span>任务统计</span><strong>${result.task_stats.length} 项</strong></div>`;
  $('rr-task-stats').innerHTML = result.task_stats.length ? result.task_stats.map(item => `
    <div class="rr-stat-row"><span><strong>${esc(item.task)}</strong><small>来自：${esc(item.sheets.join('、'))}</small></span><b>${item.account_count} 人 · ${item.diamonds} 钻</b></div>`).join('') : '<div class="rr-empty">没有可发放奖励</div>';
  $('rr-account-list').innerHTML = result.accounts.length ? result.accounts.map(item => `
    <details class="rr-account-row"><summary><span><strong>${esc(item.account)}</strong><small>${item.source_count} 条奖励来源</small></span><b>${item.total_diamonds} 钻</b></summary>
      <div class="rr-account-sources">${item.sources.map(source => `<div><span>${esc(source.sheet)} · ${esc(source.task)} · 第 ${source.row_number} 行</span><span>${esc(source.player_id || '无 playerid')} · ${source.total_reward} 钻</span></div>`).join('')}</div>
    </details>`).join('') : '<div class="rr-empty">没有可发放奖励</div>';
  $('rr-account-list').classList.toggle('rr-account-list--scroll', result.accounts.length > 15);
  $('rr-note-output').value = result.note_simple;
  $('rr-txt-preview').textContent = result.txt_preview || '没有可导出的账号';
  document.querySelectorAll('[data-rr-note]').forEach(button => {
    button.classList.toggle('rr-note-tab--active', button.dataset.rrNote === 'simple');
  });
}

async function rrCopyNote() {
  const text = $('rr-note-output').value;
  try {
    await navigator.clipboard.writeText(text);
    showToast('备注已复制', 'success');
  } catch {
    $('rr-note-output').select();
    document.execCommand('copy');
    showToast('备注已复制', 'success');
  }
}

const rrUploadZone = $('rr-upload-zone');
rrUploadZone.addEventListener('click', () => $('rr-file-input').click());
rrUploadZone.addEventListener('dragover', event => { event.preventDefault(); rrUploadZone.classList.add('upload-zone--drag'); });
rrUploadZone.addEventListener('dragleave', () => rrUploadZone.classList.remove('upload-zone--drag'));
rrUploadZone.addEventListener('drop', event => {
  event.preventDefault(); rrUploadZone.classList.remove('upload-zone--drag'); rrChooseFile(event.dataTransfer.files[0]);
});
$('rr-file-input').addEventListener('change', event => rrChooseFile(event.target.files[0]));
$('rr-btn-upload').addEventListener('click', rrUpload);
$('rr-btn-analyze').addEventListener('click', rrAnalyze);
$('rr-btn-resolve').addEventListener('click', rrResolve);
$('rr-btn-back-upload').addEventListener('click', rrReset);
$('rr-btn-back-mapping').addEventListener('click', () => rrGoStep(2));
$('rr-btn-copy-note').addEventListener('click', rrCopyNote);
$('rr-btn-download').addEventListener('click', () => {
  if (!rrState.result || !rrState.sessionId) return;
  window.RewardBatches.acceptImport(rrState.sessionId, [...rrState.selectedDates]);
});
document.querySelectorAll('[data-rr-note]').forEach(button => {
  button.addEventListener('click', () => {
    rrState.noteMode = button.dataset.rrNote;
    document.querySelectorAll('[data-rr-note]').forEach(item => item.classList.toggle('rr-note-tab--active', item === button));
    $('rr-note-output').value = rrState.noteMode === 'full' ? rrState.result.note_full : rrState.result.note_simple;
  });
});
