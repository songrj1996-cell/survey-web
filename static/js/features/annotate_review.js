// Confidence: quality_done.results[].q_review_signals[col_N], schema_version=1.
// Only the server's review_recommended chooses the queue. Unknown is not a todo.
// Human confirmation/history contracts do not exist yet. The existing quality-review
// endpoint saves labels only; neither a label change nor model signals imply confirmation.
(() => {
  'use strict';
  const root = document.getElementById('ann-quality-review-enhanced');
  if (!root) return;
  const labels = ['无效反馈', '有效反馈', '优秀反馈'];
  const pageSize = 12;
  const owns = (obj, key) => Object.prototype.hasOwnProperty.call(obj || {}, key);
  const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const array = value => Array.isArray(value) ? value : [];
  const text = value => typeof value === 'string' ? value : '';
  const labelOf = value => annCanonicalQualityLabel(text(value));
  const find = id => root.querySelector(`#${id}`);
  const reasonTitles = {
    valid_invalid_boundary: '有效 / 无效边界', ambiguous_question: '题意存在歧义',
    ambiguous_answer: '回答存在歧义', missing_context: '缺少必要背景',
    initial_review_disagreement: '初判与模型复判有分歧',
  };
  const ui = { view: 'queue', status: 'recommended', focus: 'all', confidence: 'all', label: 'all',
    selected: '', page: 0, drafts: new Map(), saving: false, message: '', error: false,
    generation: 0, cache: null };
  function isBusy() { return ui.saving || annState.qualityReviewRunning; }
  function announce(message, error = false) { ui.message = message; ui.error = error; }
  function reset() {
    ui.generation += 1;
    Object.assign(ui, { view: 'queue', status: 'recommended', focus: 'all', confidence: 'all', label: 'all',
      selected: '', page: 0, saving: false, message: '', error: false, cache: null });
    ui.drafts.clear();
    annState.reviewFilename = '';
    annState.missingOverallIds = [];
    $('ann-btn-quality-preview').hidden = true;
  }
  function ingest() { ui.cache = null; }
  function signalFor(player, key) {
    const raw = object(object(player.q_review_signals)[key]);
    // review_focus describes the review recommendation, not whether confidence exists.
    // A completed high/medium-confidence result normally has review_focus="none".
    const compatible = raw.schema_version === 1;
    const confidence = compatible && raw.applicable === true && ['high', 'medium', 'low'].includes(raw.validity_confidence)
      ? ({ high: '高', medium: '中', low: '低' })[raw.validity_confidence] || '未知' : '未知';
    return { confidence, applicable: raw.applicable !== false,
      notApplicable: compatible && raw.applicable === false && raw.source === 'not_applicable',
      recommended: compatible && raw.applicable === true && raw.review_recommended === true,
      codes: compatible ? array(raw.reason_codes).filter(code => owns(reasonTitles, code)) : [],
      reason: compatible ? text(raw.reason) : '', source: compatible ? text(raw.source) : '',
      assessed: compatible ? labelOf(raw.assessed_label) : '' };
  }
  function entries() {
    if (ui.cache?.source === annState.qualityResults && ui.cache.cols === annState.openTextCols) return ui.cache.rows;
    const rows = [], seen = new Set();
    for (const player of array(annState.qualityResults)) {
      const playerId = String(player.id ?? '');
      if (!playerId || annState.confirmedAiIds.has(playerId)) continue;
      for (const col of array(annState.openTextCols)) {
        const key = `col_${col}`, id = JSON.stringify([playerId, col]);
        if (seen.has(id)) continue;
        seen.add(id);
        const hasOriginal = owns(player.originals, key);
        const original = hasOriginal ? String(player.originals[key] ?? '') : '';
        const final = labelOf(object(player.q_labels)[key]);
        const legacy = object(object(player.human_reviews)[key]);
        const baseline = object(object(player.quality_review_baseline)[key]);
        const signal = signalFor(player, key);
        // The server omits empty originals. N/A is also authoritative in older
        // results without signals; a supplied nonempty original always wins.
        const blank = hasOriginal ? !original.trim() : final === 'N/A'
          && (signal.notApplicable || !owns(player.q_review_signals, key));
        const adjusted = Object.keys(legacy).length > 0;
        const ai = signal.assessed || labelOf(baseline.label) || labelOf(legacy.from_label) || (!adjusted ? final : '');
        const reason = text(baseline.reason) || (!adjusted ? text(object(player.q_reasons)[key]) : '旧记录未单独保留 AI 原始理由');
        const technical = !blank && (signal.source === 'pending_quality' || !labels.includes(final)
          || !text(object(player.q_reasons)[key]).trim());
        const recommended = signal.recommended && !blank && !technical;
        rows.push({ id, playerId, col, key, player, original, hasOriginal, blank, final, ai, reason,
          signal, adjusted, legacy, technical, recommended });
      }
    }
    ui.cache = { source: annState.qualityResults, cols: annState.openTextCols, rows };
    return rows;
  }
  function filtered() {
    return entries().filter(row => {
      if (ui.status === 'technical') return row.technical;
      if (row.technical) return false;
      if (ui.status === 'recommended' && !row.recommended) return false;
      if (ui.status === 'adjusted' && !row.adjusted) return false;
      if (ui.focus !== 'all' && !row.signal.codes.includes(ui.focus)) return false;
      if (ui.confidence !== 'all' && (row.blank ? '不适用' : row.signal.confidence) !== ui.confidence) return false;
      return ui.label === 'all' || (row.blank ? 'N/A' : row.final) === ui.label;
    }).sort((a, b) => Number(b.recommended) - Number(a.recommended));
  }
  function gapIds() {
    return new Set([...array(annState.missingQualityIds).map(String), ...array(annState.missingOverallIds).map(String),
      ...array(annState.qualityResults).filter(player => player.overall_pending === true).map(player => String(player.id)),
      ...entries().filter(row => row.technical).map(row => row.playerId)]);
  }
  function incomplete() {
    return gapIds().size > 0 || array(annState.missingTranslationIds).length > 0 || array(annState.missingAiIds).length > 0;
  }
  function editable(row) {
    return row && !row.blank && !row.technical && row.hasOriginal && labels.includes(row.final)
      && !!annState.sessionId && !incomplete();
  }
  function draftFor(row) { return ui.drafts.get(row.id) || row.final; }
  function statusText(row) {
    return row.technical ? '技术待补齐' : row.blank ? '未作答 · N/A'
      : row.adjusted ? '已改标 · 确认未记录' : '确认未记录';
  }
  function badge(value, kind = '') {
    return `<span class="ar-badge${kind ? ` ar-badge--${kind}` : ''}">${esc(value)}</span>`;
  }
  function options(values) {
    return values.map(([value, title]) => `<option value="${esc(value)}">${esc(title)}</option>`).join('');
  }
  function renderShell() {
    root.innerHTML = `<header class="ar-heading"><div><h2>质量结果复核</h2><p class="ar-muted" id="ar-context"></p></div>
      <button class="btn btn--ghost" type="button" data-ar-view="export">结果预览</button></header>
      <nav class="ar-tabs" aria-label="质量复核视图">
        ${[['queue', '复核工作台'], ['stats', '统计与记录'], ['versions', '历史版本'], ['export', '结果与导出']].map(([key, title]) =>
          `<button type="button" id="ar-tab-${key}" data-ar-view="${key}" aria-controls="ar-${key}-view">${title}</button>`).join('')}</nav>
      <section id="ar-queue-view" aria-labelledby="ar-tab-queue">
        <div class="ar-summary" id="ar-summary"></div>
        <div class="ar-toolbar"><div class="ar-status-tabs" id="ar-status" role="group" aria-label="复核状态"></div>
          <div class="ar-filters">
            <label>复核原因<select class="type-select" id="ar-focus">${options([['all', '全部原因'], ...Object.entries(reasonTitles)])}</select></label>
            <label>有效性信心<select class="type-select" id="ar-confidence">${options([['all', '全部信心'], ['低', '低'], ['中', '中'], ['高', '高'], ['未知', '未知 / 未记录']])}</select></label>
            <label>当前标签<select class="type-select" id="ar-label">${options([['all', '全部标签'], ...labels.map(x => [x, x]), ['N/A', 'N/A']])}</select></label>
          </div></div>
        <div class="ar-workspace"><aside class="ar-queue-pane" aria-label="逐题复核队列"><p id="ar-list-caption" class="ar-muted"></p><div id="ar-list"></div><div id="ar-pagination" class="ar-pagination"></div></aside>
          <section id="ar-detail" class="ar-detail" aria-label="当前题目复核"></section></div>
        <p class="ar-footnote">信心只针对“有效还是无效”，不区分普通与优秀，也不代表正确概率。建议复核由系统提供；改标后仍保留建议，不据此推断已人工确认。</p>
      </section>
      ${['stats', 'versions', 'export'].map(key => `<section id="ar-${key}-view" aria-labelledby="ar-tab-${key}" hidden></section>`).join('')}
      <p id="ar-message" class="ar-message" role="status" aria-live="polite"></p>`;
  }
  function renderQueue() {
    const all = entries(), recommended = all.filter(row => row.recommended).length;
    const adjusted = all.filter(row => row.adjusted && !row.blank && !row.technical).length;
    find('ar-summary').innerHTML = [
      ['建议优先复核', `${recommended} 题`, '包含低信心或初判与复判有分歧的题'],
      ['当前已改标', `${adjusted} 题`, '改标记录不等于独立人工确认记录'],
      ['技术待补齐', `${gapIds().size} 行`, `另有 ${array(annState.missingTranslationIds).length} 行译文待补`],
    ].map(([title, count, note]) => `<div><span>${esc(title)}</span><strong>${esc(count)}</strong><small>${esc(note)}</small></div>`).join('');
    find('ar-status').innerHTML = [['recommended', `建议复核 ${recommended}`], ['adjusted', `已改标 ${adjusted}`],
      ['technical', `技术待补齐 ${gapIds().size} 行`], ['all', '全部题目']].map(([value, title]) =>
      `<button type="button" data-ar-status="${value}" aria-pressed="${ui.status === value}">${title}</button>`).join('');
    for (const [id, key] of [['ar-focus', 'focus'], ['ar-confidence', 'confidence'], ['ar-label', 'label']]) {
      find(id).disabled = ui.status === 'technical'; find(id).value = ui[key];
    }
    const rows = filtered();
    ui.page = Math.max(0, Math.min(ui.page, Math.ceil(rows.length / pageSize) - 1));
    if (!ui.selected && rows.length) ui.selected = rows[ui.page * pageSize].id;
    find('ar-list-caption').textContent = ui.status === 'technical' ? '技术缺项单独处理，不计入人工待办'
      : `匹配 ${rows.length} 题${ui.status === 'recommended' ? ' · 仅系统建议项' : ' · 系统建议项优先'}`;
    find('ar-list').innerHTML = rows.slice(ui.page * pageSize, (ui.page + 1) * pageSize).map(row => `
      <button class="ar-candidate" type="button" data-ar-item="${esc(row.id)}" aria-pressed="${row.id === ui.selected}">
        <span class="ar-row"><strong>${esc(row.playerId)} · 第 ${annState.openTextCols.indexOf(row.col) + 1} 题</strong><span class="ar-muted">${row.blank ? '不适用' : `${row.signal.confidence}信心`}</span></span>
        <span class="ar-candidate-title">${esc(annState.headersZh[row.col] || annState.headers[row.col] || `列 ${row.col}`)}</span>
        <span class="ar-row">${badge(row.ai ? `AI ${row.ai}` : row.blank ? 'N/A' : 'AI 原判未提供')}${badge(row.technical ? '技术待补齐' : row.adjusted ? '已改标' : row.recommended ? '建议复核' : '可人工核对', row.recommended ? 'warning' : '')}</span>
      </button>`).join('') || '<div class="ar-empty">当前筛选没有匹配题目。可切换“全部题目”查看其他结果。</div>';
    if (ui.status === 'technical') {
      const represented = new Set(all.filter(row => row.technical).map(row => row.playerId));
      const missing = [...gapIds()].filter(id => !represented.has(id));
      if (missing.length) find('ar-list').innerHTML += `<div class="ar-notice"><strong>${missing.length} 行整体判断或结果未完整</strong><p>${esc(missing.slice(0, 10).join('、'))}${missing.length > 10 ? '…' : ''}</p><p>未提供逐题明细，不推算失败题数。</p></div>`;
    }
    find('ar-pagination').innerHTML = rows.length > pageSize
      ? `<button type="button" class="btn btn--ghost" data-ar-page="-1"${ui.page === 0 ? ' disabled' : ''}>上一页</button><span>${ui.page + 1} / ${Math.ceil(rows.length / pageSize)}</span><button type="button" class="btn btn--ghost" data-ar-page="1"${(ui.page + 1) * pageSize >= rows.length ? ' disabled' : ''}>下一页</button>` : '';
    renderDetail();
  }
  function retryControl() {
    const canRetry = gapIds().size > 0 || array(annState.missingTranslationIds).length > 0;
    return `<button type="button" class="btn btn--ghost" data-ar-action="retry"${!annState.sessionId || !canRetry || isBusy() ? ' disabled' : ''}>${annState.qualityReviewRunning ? '正在补齐…' : '重试技术缺项'}</button>`;
  }
  function renderDetail() {
    const row = entries().find(item => item.id === ui.selected), box = find('ar-detail');
    if (ui.status === 'technical' && !row?.technical) {
      box.innerHTML = `<h3>补齐缺失结果</h3><p class="ar-muted">${gapIds().size} 行质量结果、${array(annState.missingTranslationIds).length} 行译文待补齐。已有结果会保留。</p>${retryControl()}`;
      return;
    }
    if (!row) { box.innerHTML = '<div class="ar-empty">从题目队列选择一条回答。中信心和未知信心不会自动进入建议复核队列。</div>'; return; }
    const draft = draftFor(row), canEdit = editable(row), changed = draft !== row.final;
    const question = annState.headers[row.col] || '', questionZh = annState.headersZh[row.col] || question;
    box.innerHTML = `<header class="ar-row ar-between"><h3>${esc(row.playerId)} · 第 ${annState.openTextCols.indexOf(row.col) + 1} 题</h3>${badge(statusText(row))}</header>
      <div class="ar-question"><span class="ar-muted">题目</span><h3>${esc(questionZh || `列 ${row.col}`)}</h3>${question && question !== questionZh ? `<p class="ar-muted">${esc(question)}</p>` : ''}</div>
      <div class="ar-reason${row.recommended ? ' ar-reason--priority' : ''}"><div class="ar-row ar-between"><strong>${row.recommended ? '为何建议复核' : '有效性判断信心'}</strong>${badge(row.blank || !row.signal.applicable ? '不适用' : `有效性信心：${row.signal.confidence}`)}</div>
        <p>${esc(row.blank ? '该题未作答，固定为 N/A，不纳入人工质量确认。' : row.technical ? '质量结果尚未完整，请先补齐；本题不计入人工复核建议。'
          : row.signal.reason || (row.signal.confidence === '未知' ? '未记录有效性信心与复核原因，可对照题干、原文和 AI 依据人工核对。' : '未提供额外复核说明，可对照题干、原文和 AI 依据人工核对。'))}</p>
        <span class="ar-muted">${esc(row.signal.codes.map(code => reasonTitles[code]).join(' · '))}</span></div>
      <div class="ar-answers"><div><h4>玩家原文</h4><p>${esc(row.hasOriginal ? row.original || '（未作答）' : '原文未提供，暂不开放改标')}</p></div><div><h4>中文翻译</h4><p>${esc(text(object(row.player.translations)[row.key]) || '未提供译文')}</p></div></div>
      <div class="ar-evidence"><div><h4>AI 原始标签与理由 ${badge(row.ai || (row.blank ? 'N/A' : '未提供'))}</h4><p>${esc(row.reason || '未提供原始判断依据')}</p></div><div><h4>原文证据</h4><p>${esc(text(object(row.player.q_evidence)[row.key]) || '未提供原文证据')}</p></div></div>
      <details class="ar-overall"><summary>玩家整体质量：${esc(row.player.overall_pending ? '待补齐' : labelOf(row.player.overall) || '未提供')}</summary><p>${esc(text(row.player.overall_reason))}</p>${row.player.overall_review_note ? `<p>${esc(text(row.player.overall_review_note))}</p>` : ''}</details>
      <div class="ar-edit"><div class="ar-row ar-between"><h4>人工最终标签</h4><span class="ar-muted">当前：${esc(row.blank ? 'N/A' : row.final || '待补齐')}</span></div>
        <div class="ar-labels" role="group" aria-label="选择当前题最终标签">${labels.map(label => `<button type="button" data-ar-label="${label}" aria-pressed="${draft === label}"${!canEdit || isBusy() ? ' disabled' : ''}>${label}</button>`).join('')}</div>
        <div class="ar-actions"><button class="btn btn--primary" type="button" data-ar-action="save"${!canEdit || !changed || isBusy() ? ' disabled' : ''}>${ui.saving ? '正在保存…' : '保存标签'}</button>
          <button class="btn btn--ghost" type="button" data-ar-action="restore"${!canEdit || !labels.includes(row.ai) || row.final === row.ai || isBusy() ? ' disabled' : ''}>恢复 AI 原标签</button>
          <button class="btn btn--ghost ar-next" type="button" data-ar-action="next">下一题 →</button></div>
        <div class="ar-actions"><button class="btn btn--ghost" type="button" disabled>确认原判（尚未支持）</button><button class="btn btn--ghost" type="button" disabled>撤销确认（尚未支持）</button></div>
        <p class="ar-muted">${row.blank ? 'N/A 不参与人工复核数量统计。' : row.technical ? '本题结果不完整，请先补齐。' : !row.hasOriginal ? '缺少原文，不能核对并保存结论。' : incomplete() ? '尚有技术缺项，请先补齐后再保存标签。' : '当前支持改标与恢复原标签；独立确认状态、复核备注及完整操作历史尚不能保存。'}</p>
        ${incomplete() ? retryControl() : ''}</div>
      <details class="ar-events"><summary>本题改标记录</summary>${row.adjusted ? `<p>${esc(labelOf(row.legacy.from_label))} → ${esc(labelOf(row.legacy.to_label))}</p>` : '<p>当前没有已保存的改标差异。</p>'}<p>当前记录只反映标签差异，恢复原标签后差异会消失；不代表完整操作历史。</p></details>`;
  }
  function renderStats() {
    const rows = entries().filter(row => !row.blank && !row.technical);
    find('ar-stats-view').innerHTML = `<div class="ar-summary"><div><span>AI 自动标签准确率</span><strong>待校准</strong><small>缺少独立人工参照，不以改标率代替</small></div>
      <div><span>独立人工确认数</span><strong>未记录</strong><small>尚不支持保存确认原判与撤销确认</small></div>
      <div><span>当前已改标</span><strong>${rows.filter(row => row.adjusted).length} 题</strong><small>当前标签差异数，不是累计操作次数</small></div></div>
      <p class="ar-footnote">未复核题不能当作“AI 正确”；复核子集的结果也不能直接代表全量准确率。</p>
      <div class="ar-stats-list"><p>系统建议优先复核：${rows.filter(row => row.recommended).length} 题</p><p>中信心，可在全部题目中筛选：${rows.filter(row => row.signal.confidence === '中').length} 题</p><p>信心未知 / 未记录：${rows.filter(row => row.signal.confidence === '未知').length} 题（不自动加入待办）</p></div>`;
  }
  function renderVersions() {
    find('ar-versions-view').innerHTML = `<div class="ar-row ar-between"><h3>历史版本</h3><button class="btn btn--primary" type="button" disabled>重新打标，生成新版本（尚未支持）</button></div>
      <p class="ar-notice">历史目前保存 Excel，尚不支持恢复完整复核状态。可从左侧历史记录下载已保存的文件。</p>
      <div class="ar-empty">版本列表、复核状态恢复和保留旧版本重新打标，待后续支持后开放。</div>`;
  }
  function renderExport() {
    const all = entries();
    find('ar-export-view').innerHTML = `<h3>当前结果预览</h3><p class="ar-footnote">展示当前已返回的 ${all.length} 题，不受复核筛选影响。${incomplete() ? '技术结果未完整，下载暂不可用。' : '仅缺信心或待人工核对，不影响下载。'}</p>
      <div class="ar-table-wrap"><table><thead><tr><th>玩家 / 题目</th><th>AI 原判</th><th>有效性信心</th><th>当前标签</th><th>人工状态</th></tr></thead><tbody>${all.slice(0, 100).map(row => `<tr><td>${esc(row.playerId)} / ${annState.openTextCols.indexOf(row.col) + 1}</td><td>${esc(row.ai || (row.blank ? 'N/A' : '未提供'))}</td><td>${esc(row.blank ? '不适用' : row.signal.confidence)}</td><td>${esc(row.blank ? 'N/A' : row.final || '待补齐')}</td><td>${esc(statusText(row))}</td></tr>`).join('')}</tbody></table></div>
      <p class="ar-footnote">${all.length > 100 ? '页面仅预览前 100 题；下载不按页面截断。' : ''}此表用于核对页面结果；Excel 沿用现有字段，尚不保证包含逐题信心或独立人工确认记录。</p>
      <button class="btn btn--primary" type="button" data-ar-action="download"${incomplete() || isBusy() ? ' disabled' : ''}>下载结果 Excel</button>`;
  }
  function render() {
    if (!annState.tasks.quality) return false;
    const block = $('ann-quality-preview-block');
    block.hidden = !array(annState.qualityResults).length && !gapIds().size && !array(annState.missingTranslationIds).length;
    if (block.hidden) return true;
    $('ann-quality-review-legacy').hidden = true;
    if (!find('ar-queue-view')) renderShell();
    find('ar-context').textContent = annState.reviewFilename || '当前标注结果';
    root.querySelectorAll('.ar-tabs button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.arView === ui.view)));
    for (const view of ['queue', 'stats', 'versions', 'export']) find(`ar-${view}-view`).hidden = ui.view !== view;
    if (ui.view === 'queue') renderQueue();
    if (ui.view === 'stats') renderStats();
    if (ui.view === 'versions') renderVersions();
    if (ui.view === 'export') renderExport();
    find('ar-message').textContent = ui.message;
    find('ar-message').classList.toggle('ar-message--error', ui.error);
    find('ar-message').setAttribute('role', ui.error ? 'alert' : 'status');
    $('ann-btn-download').disabled = incomplete() || isBusy();
    $('ann-btn-restart').disabled = isBusy();
    return true;
  }
  async function save(restore = false) {
    const row = entries().find(item => item.id === ui.selected);
    if (!editable(row) || isBusy()) return;
    const label = restore ? row.ai : draftFor(row);
    if (!labels.includes(label) || label === row.final) return;
    const session = annState.sessionId, generation = ui.generation;
    const current = () => session === annState.sessionId && generation === ui.generation;
    ui.saving = true; annState.qualityReviewSaving = true;
    announce('正在保存标签…'); render();
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(`/api/annotate/${encodeURIComponent(session)}/quality-review`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
        body: JSON.stringify({ player_id: row.playerId, column_index: row.col, label }),
      });
      const data = await response.json().catch(() => ({}));
      if (!current()) return;
      if (!response.ok) throw new Error(response.status === 409 ? '结果已变化，本次保存未完成，请重新核对。'
        : response.status === 404 ? '当前标注会话已不可用，未确认保存成功。'
          : response.status === 401 || response.status === 403 ? '登录或访问权限已变化，请重新登录后再试。'
            : text(data.detail) || '保存失败，未确认保存成功，请重试。');
      const result = data.result;
      if (!result || String(result.id) !== row.playerId || labelOf(object(result.q_labels)[row.key]) !== label) {
        throw new Error('保存响应不完整，尚不能确认已保存。请重试同一标签核对。');
      }
      annState.qualityResults = annState.qualityResults.map(item => String(item.id) === row.playerId ? result : item);
      ui.drafts.delete(row.id); ingest();
      $('ann-done-text').innerHTML = annBuildDoneSummary();
      announce('标签已保存。独立人工确认状态未记录，系统复核建议仍保留。');
    } catch (error) {
      if (current()) announce(error.name === 'AbortError' ? '请求超时，保存状态尚未确认。请重试同一标签核对，勿以页面旧值判断保存失败。' : error.message, true);
    } finally {
      clearTimeout(timer);
      if (current()) { ui.saving = false; annState.qualityReviewSaving = false; render(); }
    }
  }
  function download() {
    if (!annState.tasks.quality) return false;
    if (incomplete() || isBusy() || !annState.sessionId) {
      announce('结果尚未完整或正在保存，请完成后再下载。', true); render(); return true;
    }
    window.location.href = `/api/annotate/${encodeURIComponent(annState.sessionId)}/download`;
    return true;
  }
  root.addEventListener('change', event => {
    const key = ({ 'ar-focus': 'focus', 'ar-confidence': 'confidence', 'ar-label': 'label' })[event.target.id];
    if (!key) return;
    ui[key] = event.target.value; ui.selected = ''; ui.page = 0; render();
  });
  root.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || button.disabled) return;
    if (button.dataset.arView) { ui.view = button.dataset.arView; render(); return; }
    if (button.dataset.arStatus) { ui.status = button.dataset.arStatus; ui.selected = ''; ui.page = 0; render(); return; }
    if (button.dataset.arItem) { ui.selected = button.dataset.arItem; announce(''); render(); return; }
    if (button.dataset.arPage) { ui.page += Number(button.dataset.arPage); ui.selected = ''; renderQueue(); return; }
    if (button.dataset.arLabel) {
      const row = entries().find(item => item.id === ui.selected);
      if (editable(row) && !isBusy()) {
        const label = button.dataset.arLabel;
        ui.drafts.set(row.id, label); renderDetail();
        root.querySelector(`[data-ar-label="${label}"]`)?.focus({ preventScroll: true });
      }
      return;
    }
    switch (button.dataset.arAction) {
      case 'save': save(); break;
      case 'restore': save(true); break;
      case 'download': download(); break;
      case 'retry':
        if (!isBusy() && annState.sessionId && (gapIds().size || array(annState.missingTranslationIds).length)) annRunQuality({ preserveReview: true });
        break;
      case 'next': {
        const rows = filtered(), index = rows.findIndex(row => row.id === ui.selected);
        if (index + 1 < rows.length) {
          ui.selected = rows[index + 1].id; ui.page = Math.floor((index + 1) / pageSize); announce('');
        } else announce('已到当前筛选的最后一题。');
        render(); break;
      }
      default: break;
    }
  });
  window.AnnotateReview = { render, ingest, reset, download, busy: isBusy };
})();
