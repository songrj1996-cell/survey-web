// Persistent quality tasks. Timers only fetch server state; they never advance progress.
window.GroupTasks = (() => {
  const root = $('group-tasks-panel'), api = window.workflowRequest;
  const statusNames = {awaiting_ai_confirmation:'待确认 AI 作答',draft:'待配置',input_issues:'输入待处理',ready:'待标注',queued:'排队中',running:'标注中',incomplete:'待补齐',awaiting_confirmation:'待确认',confirmed:'可结算'};
  let task = null, step = 1, dirty = false, listFilter = '', groupFilter = '', qualityFilter = '', showAllInputs = false, pollBusy = false;
  let selectedResponse = '', latestList = [], clipboardPreview = null, configuredGroups = [], aiChoices = null;
  const maxStep = () => task?.workflow?.max_step || 1;
  async function fetchGroups() { configuredGroups=(await api('/api/group-tasks/groups')).groups||[]; }
  const taskMeta = t => `${t.rules.group_name ? h(t.rules.group_name)+' · ' : ''}${h(t.owner_name)} · ${statusNames[t.status]}`;
  const changedFields = new Set();
  const h = value => esc(String(value ?? ''));
  const btn = (action, text, primary=false, disabled=false, extra='') => `<button type="button" class="btn ${primary?'btn--primary':'btn--ghost'}" data-gw="${action}" ${disabled?'disabled':''} ${extra}>${text}</button>`;
  const busy = () => task && ['queued','running'].includes(task.status);
  const editable = () => task?.can_edit && !busy();
  const disabled = () => editable() ? '' : 'disabled';
  const error = e => showToast(e.message, 'error', 6500);
  async function mayLeave() { if (!dirty) return true; if (!await window.workflowConfirm('当前任务有未保存的填写。继续编辑会保留原文；放弃后才会切换页面。')) return false; dirty=false; aiChoices=null; changedFields.clear(); return true; }
  window.groupWorkflowMayLeave = mayLeave;
  window.groupWorkflowHasDraft = () => dirty;
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  function progress(t) {
    const run = t.run;
    if (!run) return `${t.summary.complete_count} / ${t.summary.feedback_count} 份结果完整`;
    return `本轮${run.stage==='ai'?'AI 识别':'质量打标'} ${run.completed_ids.length} / ${run.target_ids.length} 份 · 已处理 ${run.attempted_ids.length} 份${t.queue_position ? ` · 排队第 ${t.queue_position} 位` : ''}`;
  }
  function timing(t) {
    if(!t.run)return '';
    const r=t.run, seconds=v=>Number(v||0).toFixed(2);
    return `<details class="workflow-muted"><summary>本轮执行记录</summary><p>累计排队 ${seconds(r.queue_wait_seconds)} 秒 · 执行 ${seconds(r.execution_seconds)} 秒 · 保存 ${seconds(r.save_seconds)} 秒 · 模型请求 ${r.http_attempts||0} 次</p><p>耗时在每轮后台处理结束后更新。执行时间包含模型等待与保存；保存单独列示，不能与执行时间相加。${r.timing_partial?'服务曾中断，未完成处理段的耗时无法完整恢复。':''}</p></details>`;
  }
  function progressBar(t) {
    const total=t.run?.target_ids?.length||t.summary.feedback_count, done=t.run?.completed_ids?.length??t.summary.complete_count;
    return `<progress class="workflow-progress" max="${Math.max(1,total)}" value="${done}" aria-label="${h(t.rules?.name||'任务')}完成进度"></progress>`;
  }
  async function open() { root.classList.remove('panel--hidden'); if (dirty) return; if (task) await load(task.id); else await list(); }
  async function list() {
    try {
      const data = await api('/api/group-tasks'); task = null;
      root.innerHTML = `<div class="workflow-heading"><div><h1 class="panel__title">质量标注</h1><p class="panel__desc">从任务规则到反馈质量与奖励，记录和进度随时可继续。</p></div>${btn('new','新建任务',true,!PlatformPermissions.canUse('annotate'))}</div>
        <div id="gw-list-summary" class="workflow-summary"></div><div class="workflow-toolbar"><input id="gw-list-filter" aria-label="搜索任务" placeholder="搜索任务或负责人" value="${h(listFilter)}">${btn('list','刷新列表')}<span class="workflow-muted">后台标注不受页面关闭影响</span></div><div id="gw-task-list"></div><div class="workflow-toolbar">${PlatformPermissions.allowed('reward')?btn('rewards','查看奖励批次 →'):''}</div>`;
      renderList(data.tasks);
      $('gw-list-filter').oninput = e => { listFilter=e.target.value; renderList(latestList); };
    } catch(e) { error(e); }
  }
  function renderList(items) {
    latestList=items;
    $('gw-list-summary').innerHTML=[['标注中',items.filter(t=>t.status==='running').length],['排队中',items.filter(t=>t.status==='queued').length],['待处理任务',items.filter(t=>t.status!=='confirmed').length],['可结算任务',items.filter(t=>t.status==='confirmed').length]].map(([label,value])=>`<span>${label}<strong>${value}</strong></span>`).join('');
    const filtered = items.filter(t => `${t.rules.name} ${t.owner_name}`.toLowerCase().includes(listFilter.toLowerCase())).sort((a,b)=>Number(['running','queued'].includes(b.status))-Number(['running','queued'].includes(a.status)));
    $('gw-task-list').innerHTML = filtered.length ? `<div class="workflow-table-wrap"><table class="workflow-table"><thead><tr><th>任务</th><th>负责人</th><th>发奖日期</th><th>状态与进度</th><th></th></tr></thead><tbody>${filtered.map(t=>`<tr><td><strong>${h(t.rules.name||'未命名任务')}</strong><small>${t.rules.group_name?h(t.rules.group_name)+' · ':''}${t.summary.player_count} 位玩家 · ${t.summary.feedback_count} 份反馈</small></td><td>${h(t.owner_name)}</td><td>${h(t.rules.payout_date||'待确认')}</td><td><span class="workflow-badge">${statusNames[t.status]}</span>${progressBar(t)}<small>${h(progress(t))}</small></td><td>${btn('view',['queued','running'].includes(t.status)?'查看进度':t.can_edit?'继续处理':'查看',false,false,`data-id="${t.id}"`)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="workflow-empty">尚无匹配任务。新建任务后，依次设置规则、上传核查、标注与计奖。</div>';
  }
  async function load(id) { await fetchGroups(); task=await api(`/api/group-tasks/${id}`); render(); }
  function render() {
    step=Math.min(step,maxStep()); if(step===3&&!task.rules.ai_detect)step=4;
    root.innerHTML = `<div class="workflow-heading"><div>${btn('back','← 返回任务列表')}<h1 class="panel__title">${h(task.rules.name||'新任务')}</h1><p id="gw-task-meta" class="panel__desc">${taskMeta(task)}</p></div><span id="gw-progress" class="workflow-badge">${h(progress(task))}</span></div>
      <div class="workflow-steps" role="tablist" aria-label="任务处理步骤">${['任务设置','上传与列确认','AI 识别与确认','质量打标与复核','名单与奖励核对','任务记录'].map((name,i)=>`<button type="button" role="tab" aria-selected="${step===i+1}" data-gw="step" data-step="${i+1}" ${i+1>maxStep()||i===2&&!task.rules.ai_detect?'disabled aria-disabled="true" title="请先完成前面的步骤；未开启 AI 识别时直接进入质量打标"':''} class="${step===i+1?'is-active':''}"><span>${i+1}</span>${name}</button>`).join('')}</div>
      ${!task.can_edit?'<p class="workflow-notice">当前为只读查看，标注与发奖结果由任务负责人维护。</p>':''}
      ${busy()?'<p class="workflow-notice">本任务正在后台执行，影响结果的输入暂时锁定。可以切换到其他任务继续工作。</p>':''}
      <div id="gw-progress-track">${progressBar(task)}</div><div id="gw-update-hint" hidden class="workflow-notice">后台结果已更新。${btn('reload','查看最新结果')}</div><div id="gw-content"></div>`;
    [null,renderRules,renderInputs,renderAI,renderJudgments,renderSettlement,renderRecords][step]();
  }
  function ruleField(key,label,type='text',hint='') {
    return `<label class="workflow-field">${label}<input ${disabled()} data-rule="${key}" type="${type}" ${type==='number'?'min="0" step="1"':''} value="${h(task.rules[key])}"><small data-suggestion="${key}" class="workflow-muted">${hint}</small></label>`;
  }
  function renderRules() {
    $('gw-content').innerHTML = `<section class="workflow-card"><h2>任务公告</h2><p class="workflow-muted">可选，识别后仍可手动修改。</p>
      <label class="workflow-field">任务公告（可选）<textarea ${disabled()} data-rule="announcement" rows="5" placeholder="粘贴公告，或直接填写下方规则">${h(task.rules.announcement)}</textarea></label>
      <div class="workflow-toolbar">${btn('extract','识别并预填',false,!editable())}<span class="workflow-muted">识别结果可编辑，已手工修改的字段会保留。</span></div><p id="gw-extraction-warning" class="workflow-muted"></p>
      </section><section class="workflow-card"><h2>确认任务规则</h2><p class="workflow-muted">奖励按每份保留的完整反馈计算；发奖日期请明确确认。</p><div class="workflow-form-grid">${ruleField('name','任务名称')}${ruleField('payout_date','奖励发放日期','date','不会使用公告截止时间')}${ruleField('base_reward','每份基础奖励（钻石）','number')}${ruleField('excellent_reward','每份优秀额外奖励（钻石）','number','额外追加，不包含基础奖励')}</div>
      <div class="workflow-form-grid"><label class="workflow-field">数据校验<select ${disabled()} data-rule="requires_count"><option value="false" ${!task.rules.requires_count?'selected':''}>不要求实际次数</option><option value="true" ${task.rules.requires_count?'selected':''}>要求实际完成次数</option></select><small data-suggestion="requires_count"></small></label>${ruleField('min_count','最低实际次数','number','不要求数据校验时不适用')}
      <label class="workflow-field">重复提交<select ${disabled()} data-rule="allow_repeat"><option value="false" ${!task.rules.allow_repeat?'selected':''}>不允许，重复组保留一份</option><option value="true" ${task.rules.allow_repeat?'selected':''}>允许，每份独立判断并计奖</option></select><small data-suggestion="allow_repeat"></small></label>
      <label class="workflow-field">AI 作答识别<select ${disabled()} data-rule="ai_detect"><option value="true" ${task.rules.ai_detect?'selected':''}>开启识别并人工确认</option><option value="false" ${!task.rules.ai_detect?'selected':''}>仅做质量判断</option></select></label><label class="workflow-field">任务归属群组<select data-rule="group_source_id" ${disabled()}><option value="">未填写</option>${configuredGroups.map(g=>`<option value="${h(g.id)}" ${task.rules.group_source_id===g.id?'selected':''}>${h(g.name)}</option>`).join('')}${task.rules.group_source_id&&!configuredGroups.some(g=>g.id===task.rules.group_source_id)?`<option value="${h(task.rules.group_source_id)}" selected>${h(task.rules.group_name)}（已停用）</option>`:''}</select><small class="workflow-muted">仅用于记录任务归属</small></label></div>
      <p id="gw-count-rule" class="workflow-notice"></p><div class="workflow-toolbar">${btn('save-rules','保存并进入上传核查 →',true,!editable())}<span class="workflow-muted">影响结果的修改会使已有确认失效，历史发奖批次保留原样。</span></div></section>`;
    const updateRuleHints=()=>{
      const requires=root.querySelector('[data-rule="requires_count"]').value==='true', repeat=root.querySelector('[data-rule="allow_repeat"]').value==='true';
      root.querySelector('[data-rule="min_count"]').closest('label').hidden=!requires;
      $('gw-count-rule').textContent=repeat?'每份反馈独立判断并计奖；任务记录按玩家汇总，数据列为保留的反馈份数（含无效反馈）。':requires?'每位玩家保留一份反馈；任务记录数据列为实际体验次数。':'每位玩家保留一份反馈；本任务不要求次数，任务记录数据列留空。';
    };
    root.querySelector('[data-rule="requires_count"]').onchange=updateRuleHints;root.querySelector('[data-rule="allow_repeat"]').onchange=updateRuleHints;updateRuleHints();
  }
  function columnOptions(value,optional=false) { return `${optional?'<option value="">不映射</option>':'<option value="">请选择列</option>'}`+task.headers.map((name,i)=>(task.empty_col_idxs||[]).includes(i)?'':`<option value="${i}" ${value===i?'selected':''}>${h(name)}（第 ${i+1} 列）</option>`).join(''); }
  function feedbackText(row) {
    return `<div class="workflow-answers">${(row.answers||[]).map(answer=>`<section><strong>${h(answer.question)}</strong>${answer.question_zh&&answer.question_zh!==answer.question?`<p class="workflow-muted">${h(answer.question_zh)}</p>`:''}<p>${h(answer.answer||'（空白）')}</p>${(row.quality?.translations||row.ai?.translations)?.['col_'+answer.column]?`<p class="workflow-muted">中文参考：${h((row.quality?.translations||row.ai?.translations)['col_'+answer.column])}</p>`:''}</section>`).join('')}</div>`;
  }
  function renderInputs() {
    const m=task.mapping||{...task.mapping_suggestion,id_col:task.mapping_suggestion?.id_col??task.detected_id_col,open_text_cols:task.detected_open_text_cols||[]};
    $('gw-content').innerHTML=`<section class="workflow-card"><h2>上传反馈</h2><div class="workflow-upload"><input id="gw-upload" aria-label="选择反馈文件" type="file" accept=".xlsx,.csv" ${disabled()}><span id="gw-file-name" class="workflow-file-name">${h(task.uploads.at(-1)?.filename||'未选择文件')}</span>${btn('upload','上传反馈',true,!editable())}</div>${task.uploads.length?`<p class="workflow-muted">${task.feedback.length} 份原始反馈</p>`:''}</section>
      ${task.headers.length?`<section class="workflow-card"><h2>列确认 & 标注任务选择</h2><p class="workflow-muted">核对玩家唯一 ID 和主观题列。完成标注后再匹配名单、核对发奖账号。</p><label class="workflow-field"><span>玩家唯一 ID 列 <span class="rr-required">必填</span></span><select id="gw-map-id" ${disabled()}>${columnOptions(m.id_col)}</select></label>
      ${task.rules.requires_count?`<div class="workflow-form-grid"><label class="workflow-field"><span>数据校验列 <span class="rr-required">必填</span></span><select id="gw-map-count" ${disabled()}>${columnOptions(m.count_col)}</select></label><label class="workflow-field">校验条件：实际次数 ≥<input id="gw-min-count" aria-label="最低实际次数" type="number" min="1" step="1" value="${task.rules.min_count??''}" ${disabled()}><small>未达标的反馈按规则判为无效；缺失次数需补填。</small></label></div>`:''}
      <h3>主观题列（可多选）</h3>${task.header_translation_warning?`<div class="workflow-notice">${h(task.header_translation_warning)} ${btn('translate-headers','重试列名翻译',false,!editable())}</div>`:''}
      <div class="workflow-question-list">${task.headers.map((name,i)=>(task.empty_col_idxs||[]).includes(i)?'':`<label class="workflow-question-choice"><input type="checkbox" data-answer-col="${i}" ${(m.open_text_cols||[]).includes(i)?'checked':''} ${disabled()}><span class="workflow-muted">${i+1}</span><span><strong>${h(name||'（无标题列）')}</strong>${task.headers_zh?.[i]&&task.headers_zh[i]!==name?`<small>${h(task.headers_zh[i])}</small>`:''}</span><span class="workflow-badge">${h(task.column_types?.[i]||'待确认')}</span></label>`).join('')}</div>
      <div class="workflow-notice">${task.rules.ai_detect?'AI 作答识别 → 人工确认 → 回答质量打标与复核':'回答质量打标与复核'}</div><div class="workflow-toolbar">${btn('mapping','确认列设置',true,!editable())}</div></section>`:''}
      ${task.feedback.length?renderInputIssues(false):''}
      <div class="workflow-toolbar">${btn('step',task.rules.ai_detect?'进入 AI 识别与确认 →':'进入质量打标 →',true,maxStep()<3,`data-step="${task.rules.ai_detect?3:4}"`)}</div>`;
    $('gw-upload').onchange=()=>{$('gw-file-name').textContent=$('gw-upload').files[0]?.name||task.uploads.at(-1)?.filename||'未选择文件';};
    bindInputIssues();
  }
  function renderInputIssues(settlement) {
    const issues=settlement?task.input_issues||[]:task.annotation_issues||[];
    const visible=row=>(row.issues||[]).filter(issue=>settlement||!['roster_identity','adv'].includes(issue.kind));
    const groups=(task.duplicate_groups||[]).filter(g=>settlement||!g.roster_only);
    return `<section class="workflow-card"><div class="workflow-heading"><h2>${settlement?'名单匹配与输入核对':'输入检查'}</h2><label><input type="checkbox" id="gw-show-all" ${showAllInputs?'checked':''}> 展开全部输入</label></div><p class="workflow-muted">${issues.length} 项待处理${settlement?' · 名单读取：'+(task.roster_snapshot?.read_at?h(workflowTime(task.roster_snapshot.read_at)):'尚未读取'):''}</p>
      ${issues.filter(issue=>!issue.response_id&&!['duplicate','roster_duplicate'].includes(issue.kind)).map(issue=>`<p class="workflow-error">${h(issue.message)}</p>`).join('')}
      ${groups.map(group=>`<details class="workflow-feedback" ${!group.keep_response_id?'open':''}><summary>重复身份组 · ${group.response_ids.length} 份 · ${group.keep_response_id?'已处理':'请选择保留一份'}</summary>${group.response_ids.map(id=>{const row=task.feedback.find(r=>r.id===id);return `<div class="workflow-subcard"><strong>第 ${row.source_row} 行 · ${h(row.player_id)}</strong>${feedbackText(row)}${btn('duplicate',group.keep_response_id===id?'已保留此份':'保留此份',false,!editable(),`data-group="${group.id}" data-id="${id}"`)}</div>`;}).join('')}</details>`).join('')}
      ${task.feedback.filter(row=>showAllInputs||visible(row).length).map(row=>`<details class="workflow-feedback" ${visible(row).length&&!row.excluded?'open':''}><summary>第 ${row.source_row} 行 · ${h(row.player_id||'唯一 ID 待填写')}${settlement?' · '+h(row.identity?.group_name||'待匹配'):''}</summary>${visible(row).map(issue=>`<p class="workflow-error">${h(issue.message)}</p>`).join('')}${feedbackText(row)}<div class="workflow-form-grid"><label class="workflow-field">${settlement?'核对 playerid':'修正玩家唯一 ID'}<input data-fix="player_id" value="${h(row.player_id)}" ${disabled()}></label>${task.rules.requires_count?`<label class="workflow-field">实际次数<input data-fix="count" type="number" min="0" step="1" value="${row.count??''}" ${disabled()}></label>`:''}${settlement?`<label class="workflow-field">ADV（可选）<input data-fix="adv" value="${h(row.adv)}" ${disabled()}></label>`:''}</div>
      ${settlement&&row.identity_candidates?.length>1?`<label class="workflow-field">选择名单身份<select data-fix="candidate_key" ${disabled()}><option value="">请选择</option>${row.identity_candidates.map(c=>`<option value="${c.key}" ${row.identity?.key===c.key?'selected':''}>${h(c.group_name)} · ${h(c.ori)} · ${h(c.status)} · 第 ${c.row_number} 行</option>`).join('')}</select></label>`:''}<label class="workflow-field">修正说明<input data-fix="note" placeholder="说明修正依据" ${disabled()}></label><div class="workflow-toolbar">${btn('correct','保存修正',false,!editable(),`data-id="${row.id}"`)}</div></details>`).join('')||'<p class="workflow-muted">没有需要逐行修正的输入。</p>'}</section>`;
  }
  function bindInputIssues() { if($('gw-show-all'))$('gw-show-all').onchange=async e=>{if(!await mayLeave()){e.target.checked=showAllInputs;return;}showAllInputs=e.target.checked;render();}; }
  function renderAI() {
    if(aiChoices===null)aiChoices=new Set(task.feedback.filter(row=>!row.excluded&&row.confirmed_ai).map(row=>row.id));
    const rows=task.feedback.filter(row=>!row.excluded),pending=rows.filter(row=>['pass','not_applicable'].includes(row.gate?.status)&&!row.ai_complete).length;
    if(!rows.some(row=>row.id===selectedResponse))selectedResponse=rows[0]?.id||'';
    $('gw-content').innerHTML=`<section class="workflow-card"><div class="workflow-heading"><div><h2>AI 作答识别与人工确认</h2><p class="workflow-muted">先核对原文和识别依据，再确认哪些反馈属于 AI 作答。确认后进入质量打标。</p></div>${btn('run',task.run?'补齐 AI 识别':'开始 AI 作答识别',true,!editable()||!pending)}</div><p>保留 ${rows.length} 份反馈 · AI 识别待补齐 ${pending} 份</p>${timing(task)}${task.run?.error?`<p class="workflow-error">${h(task.run.error)}</p>`:''}</section>
      <div class="workflow-review"><aside class="workflow-response-list">${rows.map(row=>`<button class="workflow-response" data-gw="response" data-id="${row.id}" aria-pressed="${row.id===selectedResponse}"><strong>${h(row.player_id)}</strong><small>第 ${row.source_row} 行 · ${row.gate?.status==='fail'?'次数未达标':row.ai_complete?'AI 识别完成':'待识别'}</small></button>`).join('')}</aside><div class="workflow-review-detail">${rows.filter(row=>row.id===selectedResponse).map(row=>`<section class="workflow-card"><h3>第 ${row.source_row} 行 · ${h(row.player_id)}</h3>${feedbackText(row)}<p>${h(row.gate?.reason||'')}</p>${row.ai?`<h3>AI 识别依据</h3><p>AI 内容概率 ${h(row.ai.ai_prob)} · 润色概率 ${h(row.ai.polish_prob)}</p><p>${h(row.ai.reason)}</p><blockquote>${h(row.ai.evidence)}</blockquote><p>反向依据：${h(row.ai.counter_evidence)}</p>`:''}${row.ai_complete?`<label><input type="checkbox" data-ai-choice="${row.id}" ${aiChoices.has(row.id)?'checked':''} ${disabled()}> 人工判定本份为 AI 作答</label>`:''}${row.model_error?`<p class="workflow-error">${h(row.model_error)}</p>`:''}</section>`).join('')}</div></div>
      <section class="workflow-card"><h2>确认 AI 作答选择</h2><p>当前选择 ${aiChoices.size} 份为 AI 作答。已识别的其余反馈将继续质量打标；可确认没有 AI 作答。</p>${pending?'<p class="workflow-error">请先补齐 AI 识别结果，再确认并进入下一步。</p>':''}<div class="workflow-toolbar">${btn('confirm-ai','确认选择并进入质量打标 →',true,!editable()||!!pending)}</div></section>`;
    root.querySelectorAll('[data-ai-choice]').forEach(el=>el.onchange=()=>{el.checked?aiChoices.add(el.dataset.aiChoice):aiChoices.delete(el.dataset.aiChoice);dirty=true;});
  }
  function renderSettlement() {
    $('gw-content').innerHTML=`<section class="workflow-card"><div class="workflow-heading"><div><h2>名单与奖励核对</h2><p class="workflow-muted">质量标注已完成。现在读取最新名单，匹配玩家账号、状态和实际所属群。</p></div>${btn('roster',task.roster_snapshot?'重新读取名单':'读取最新名单',true,!editable())}</div></section>${renderInputIssues(true)}<section class="workflow-card"><h2>确认任务结果</h2>${task.confirmation_issues.map(issue=>`<p class="workflow-muted">${h(issue.message)}</p>`).join('')||'<p>名单与质量结果完整，可以确认用于奖励整理。</p>'}<div class="workflow-toolbar">${btn('confirm','确认结果并查看任务记录 →',true,!editable()||!!task.confirmation_issues.length)}</div></section>`;
    bindInputIssues();
  }
  function groupOptions() { return '<option value="">全部群</option>'+[...new Map(task.feedback.filter(row=>row.identity).map(row=>[row.identity.source_id,row.identity.group_name])).entries()].map(([id,name])=>`<option value="${id}" ${id===groupFilter?'selected':''}>${h(name)}</option>`).join(''); }
  function renderJudgments() {
    if(!task.feedback.length){$('gw-content').innerHTML=`<div class="workflow-empty">先上传反馈表并完成核查。${btn('step','前往上传核查',true,false,'data-step="2"')}</div>`;return;}
    const rows=task.feedback.filter(row=>!row.excluded&&(!groupFilter||row.identity?.source_id===groupFilter)&&(!qualityFilter||(qualityFilter==='pending'?!row.outcome.complete:row.outcome.label===qualityFilter)));
    if(!rows.some(row=>row.id===selectedResponse))selectedResponse=rows[0]?.id||'';
    $('gw-content').innerHTML=`<section class="workflow-card workflow-review-overview"><div class="workflow-heading"><div><h2>质量打标与复核</h2><p class="workflow-muted">每份完整反馈独立判断。数据门槛未达标的反馈已按规则判为无效，不调用模型。</p></div>${btn('run',task.run?.stage==='quality'?'重试待补齐质量结果':'开始质量打标',true,!editable()||!task.summary.pending_count)}</div>${task.run?`<p>${h(progress(task))}</p>`:''}${task.run?.error?`<p class="workflow-error">${h(task.run.error)}</p>`:''}
      ${timing(task)}<div class="workflow-summary"><span>保留反馈 <strong>${task.summary.feedback_count}</strong></span><span>结果完整 <strong>${task.summary.complete_count}</strong></span><span>待补齐 <strong>${task.summary.pending_count}</strong></span><span>已完成部分奖励 <strong>${task.summary.base+task.summary.excellent} 钻</strong></span></div>
      <div class="workflow-toolbar">${task.roster_snapshot?`<label>群 <select id="gw-group" data-filter>${groupOptions()}</select></label>`:''}<div class="workflow-filter-tabs" aria-label="反馈质量筛选">${[['','全部'],['优秀反馈','优秀'],['有效反馈','普通有效'],['无效反馈','无效'],['pending','待补齐']].map(([value,label])=>btn('quality-filter',`${label} ${task.feedback.filter(row=>!row.excluded&&(!groupFilter||row.identity?.source_id===groupFilter)&&(!value||(value==='pending'?!row.outcome.complete:row.outcome.label===value))).length}`,false,false,`data-value="${value}" aria-pressed="${qualityFilter===value}"`)).join('')}</div></div></section>
      <div class="workflow-review"><aside class="workflow-response-list" aria-label="反馈列表">${rows.map(row=>`<button type="button" class="workflow-response" data-gw="response" data-id="${row.id}" aria-pressed="${selectedResponse===row.id}"><strong>${h(row.identity?.nickname||row.player_id)}</strong><span class="workflow-badge">${h(row.outcome.label||'待补齐')}</span><small>${row.identity?.group_name?h(row.identity.group_name)+' · ':''}第 ${row.source_row} 行${row.manual_quality?' · 人工已调整':''}</small><p>${h((row.answers||[]).map(answer=>answer.answer).join(' '))}</p></button>`).join('')||'<p class="workflow-muted">没有符合条件的反馈。</p>'}</aside><div class="workflow-review-detail">${rows.filter(row=>row.id===selectedResponse).map(row=>`<details class="workflow-feedback" open><summary><strong>${h(row.player_id)}</strong> · ${row.identity?.group_name?h(row.identity.group_name)+' · ':''}第 ${row.source_row} 行 <span class="workflow-badge">${h(row.outcome.label||'待补齐')}</span> ${row.outcome.complete?`${row.outcome.base+row.outcome.excellent} 钻`:'奖励待确定'}</summary><p class="workflow-muted">${h(row.identity?.ori||'')} · ${h(row.gate?.reason||'')}</p><h3>玩家反馈原文</h3>${feedbackText(row)}<h3>当前整体质量与依据</h3><p>${h(row.outcome.reason)}</p><div class="workflow-summary"><span>基础奖励<strong>${row.outcome.base??'待确定'}</strong></span><span>优秀追加<strong>${row.outcome.excellent??'待确定'}</strong></span><span>本份合计<strong>${row.outcome.complete?row.outcome.base+row.outcome.excellent:'待确定'}</strong></span></div><p>结论来源：${({rule:'任务规则',human_ai:'人工确认 AI 作答',human:'人工调整',ai:'AI 整体判断',pending:'待补齐'})[row.outcome.source]}</p>
        ${row.ai?`<p><strong>AI 作答识别：</strong>AI 内容概率 ${h(row.ai.ai_prob ?? '待补齐')} · 润色概率 ${h(row.ai.polish_prob ?? '待补齐')}</p><p>${h(row.ai.reason||'识别依据待补齐')}</p>${row.ai.evidence?`<blockquote>支持依据：${h(row.ai.evidence)}</blockquote>`:''}${row.ai.counter_evidence?`<blockquote>反向依据：${h(row.ai.counter_evidence)}</blockquote>`:''}`:''}
        ${row.quality?`<details><summary>原始整体判断和逐题依据</summary><p>${h(row.quality.overall)}：${h(row.quality.overall_reason)}</p>${(row.answers||[]).map(a=>`<section class="workflow-subcard"><strong>${h(a.question)}</strong><p>${h(row.quality.q_labels?.['col_'+a.column]||'待补齐')} · ${h(row.quality.q_reasons?.['col_'+a.column]||'')}</p><blockquote>${h(row.quality.q_evidence?.['col_'+a.column]||'')}</blockquote>${row.quality.translations?.['col_'+a.column]?`<p>中文参考：${h(row.quality.translations['col_'+a.column])}</p>`:''}</section>`).join('')}</details>`:''}
        ${row.quality_complete&&!row.confirmed_ai&&row.gate?.status!=='fail'?`<div class="workflow-form-grid"><label class="workflow-field">人工整体质量<select data-review="label" ${disabled()}>${['优秀反馈','有效反馈','无效反馈'].map(label=>`<option ${row.outcome.label===label?'selected':''}>${label}</option>`).join('')}</select></label><label class="workflow-field">调整说明<input data-review="note" placeholder="请说明调整依据" value="${h(row.manual_quality?.note||'')}" ${disabled()}></label></div>${btn('override','保存人工调整',false,!editable(),`data-id="${row.id}"`)} ${btn('restore','恢复 AI 原判断',false,!editable()||!row.manual_quality,`data-id="${row.id}"`)}`:''}
        ${!row.outcome.complete?btn('retry','重试此份未完成部分',false,!editable(),`data-id="${row.id}"`):''}
        ${(row.reviews||[]).length?`<details><summary>人工处理记录（${row.reviews.length}）</summary>${row.reviews.map(review=>`<p>${h(review.owner_name||'操作者')} · ${h(workflowTime(review.at))} · ${h(review.note||'确认 AI 作答')} ${review.kind==='quality'?`：${h(review.after?.label||'恢复原判断')}`:''}</p>`).join('')}</details>`:''}</details>`).join('')||'<div class="workflow-empty">选择一份反馈查看原文与依据。</div>'}</div></div>
      <section class="workflow-card"><h2>完成质量复核</h2><p class="workflow-muted">全部质量结果完整后，进入名单匹配与奖励核对。</p><div class="workflow-toolbar">${btn('step','进入名单与奖励核对 →',true,!task.workflow?.annotation_complete,'data-step="5"')}</div></section>`;
    if($('gw-group'))$('gw-group').onchange=async e=>{if(!await mayLeave()){e.target.value=groupFilter;return;}groupFilter=e.target.value;render();};
  }
  function renderRecords() {
    const players=task.players.filter(p=>!groupFilter||p.source_id===groupFilter);
    $('gw-content').innerHTML=`<section class="workflow-card"><div class="workflow-heading"><div><h2>任务记录</h2><p class="workflow-muted">一位玩家一行。复制范围：${groupFilter?h(players[0]?.group_name||'当前群'):'本任务全部群'} · ${players.length} 行 · 无表头 12 列。</p></div>${btn('copy','复制任务记录',true,task.confirmed_version!==task.version)}</div><div class="workflow-toolbar"><label>所属群 <select data-filter id="gw-group">${groupOptions()}</select></label><span class="workflow-muted">${task.rules.allow_repeat?'数据列为保留反馈份数，含无效反馈':task.rules.requires_count?'数据列为实际体验次数':'数据列不适用，复制时留空'}</span></div>
      <div class="workflow-table-wrap"><table class="workflow-table"><thead><tr><th>玩家 / 群</th><th>ori / ADV</th><th>有效</th><th>次数 / 份数</th><th>数据任务</th><th>基础</th><th>优秀追加</th><th>其他</th><th>合计</th></tr></thead><tbody>${players.map(p=>`<tr><td>${h(p.player_id)}<small>${h(p.group_name)}</small></td><td>${h(p.ori)}<small>${h(p.adv||'ADV 未填写')}</small></td><td>${p.form_valid?'是':'否'}</td><td>${p.data_value??'待补齐'}</td><td>${p.data_pass===null?'不适用':p.data_pass?'是':'否'}</td><td>${p.base??'待补齐'}</td><td>${p.excellent??'待补齐'}</td><td>0</td><td>${p.total??'待补齐'}</td></tr>`).join('')}</tbody></table></div>
      ${players.map(p=>`<details class="workflow-feedback"><summary>${h(p.player_id)} · ${Object.entries(p.composition).map(([label,count])=>`${count} ${h(label)}`).join(' / ')}</summary>${p.response_ids.map(id=>{const row=task.feedback.find(r=>r.id===id);return `<p>第 ${row.source_row} 行：${h(row.outcome.label||'待补齐')} · 基础 ${row.outcome.base??'待确定'} + 优秀追加 ${row.outcome.excellent??'待确定'} · ${h(row.gate?.reason||'')} ${btn('record-response','查看原文与依据',false,false,`data-id="${row.id}"`)}</p>`;}).join('')}${p.issues.map(issue=>`<p class="workflow-error">${h(issue)}</p>`).join('')}${p.adv_choices.length>1?`<label class="workflow-field">确认任务记录中的 ADV<input data-player-adv value="${h(p.adv)}" placeholder="按实际账号填写，也可明确留空" ${disabled()}></label><label class="workflow-field">处理说明<input data-player-note ${disabled()}></label>${btn('adv','保存 ADV 决定',false,!editable(),`data-player="${h(p.player_id)}"`)}`:''}</details>`).join('')}
      <details><summary>查看完整复制列顺序（12 列）</summary><ol class="workflow-copy-columns">${['发奖日期','任务名称','playerid','ori（查询公式）','ADV','当前状态（查询公式）','表单有效','体验次数 / 反馈份数','数据任务完成','基础奖励','优秀追加','其他奖励'].map(label=>`<li>${h(label)}</li>`).join('')}</ol></details><p class="workflow-muted">ori 与当前状态会复制为查找公式。生成 TXT 前仍会读取最新名单。</p><div class="workflow-toolbar">${PlatformPermissions.allowed('reward')?btn('rewards','进入奖励整理 →',true,task.confirmed_version!==task.version):''}</div></section>`;
    if($('gw-group'))$('gw-group').onchange=async e=>{if(!await mayLeave()){e.target.value=groupFilter;return;}groupFilter=e.target.value;render();};
  }
  async function mutate(path,body,method='POST') { const value=await api(`/api/group-tasks/${task.id}/${path}`,{method,body:{expected_version:task.version,...body}}); task=value.task||value; aiChoices=null; dirty=false;changedFields.clear();render();return value; }
  async function action(button) {
    const name=button.dataset.gw;
    if(name==='quality-filter'){if(!await mayLeave())return;qualityFilter=button.dataset.value;renderJudgments();return;}
    if(name==='record-response'){if(!await mayLeave())return;selectedResponse=button.dataset.id;qualityFilter='';step=4;render();return;}
    if(name==='response'){if(step!==3&&!await mayLeave())return;selectedResponse=button.dataset.id;render();return;}
    if(name==='rewards'){if(await mayLeave())switchMode('reward');return;}
    if(name==='new'){if(!await mayLeave())return;await fetchGroups();task=await api('/api/group-tasks',{method:'POST',body:{}});step=1;render();return;}
    if(name==='back'||name==='list'){if(await mayLeave())await list();return;}
    if(name==='view'){if(await mayLeave()){await fetchGroups();task=await api(`/api/group-tasks/${button.dataset.id}`);selectedResponse='';groupFilter='';qualityFilter='';step=maxStep();render();}return;}
    if(name==='step'){if(+button.dataset.step>maxStep())throw new Error('请先完成前面的步骤');if(await mayLeave()){step=+button.dataset.step;render();root.scrollIntoView({block:'start'});}return;}
    if(name==='reload'){if(await mayLeave())await load(task.id);return;}
    if(name==='save-rules') {
      const rules={...task.rules};root.querySelectorAll('[data-rule]').forEach(el=>{const key=el.dataset.rule;rules[key]=['requires_count','allow_repeat','ai_detect'].includes(key)?el.value==='true':['base_reward','excellent_reward','min_count'].includes(key)?el.value===''?null:Number(el.value):key==='payout_date'?el.value||null:el.value;});
      await mutate('rules',{rules},'PUT');step=Math.min(2,maxStep());render();showToast(maxStep()<2?'规则已保存，请补齐任务名称、日期、奖励和次数要求':'任务规则已保存',maxStep()<2?'error':'success');return;
    }
    if(name==='extract') {
      const result=await api('/api/group-tasks/announcement',{method:'POST',body:{announcement:root.querySelector('[data-rule="announcement"]').value}});
      Object.entries(result.suggestions).forEach(([key,item])=>{
        const field=root.querySelector(`[data-rule="${key}"]`),hint=root.querySelector(`[data-suggestion="${key}"]`); if(!field)return;
        if(changedFields.has(key)&&String(item.value)!==field.value){if(hint){hint.textContent=`新识别值：${item.value}。`;const adopt=document.createElement('button');adopt.type='button';adopt.className='btn btn--ghost btn--sm';adopt.textContent='采用识别值';adopt.onclick=()=>{field.value=String(item.value);dirty=true;hint.textContent='已采用识别值，请保存规则';root.querySelector('[data-rule="requires_count"]').onchange();};hint.append(adopt);}}
        else {field.value=String(item.value); if(hint)hint.textContent=`识别依据：${item.evidence}`;dirty=true;}
      });
      root.querySelector('[data-rule="requires_count"]').onchange();
      $('gw-extraction-warning').textContent=result.warnings.join(' ')+' 缺失或不确定字段请手工核对。';return;
    }
    if(name==='upload') {const file=$('gw-upload').files[0];if(!file)throw new Error('请先选择反馈文件');if(task.feedback.length&&!await window.workflowConfirm('重新上传将保留旧原文供追溯，并建立新的反馈版本。是否继续？','确认重新上传'))return;const form=new FormData();form.append('file',file);form.append('expected_version',String(task.version));task=await api(`/api/group-tasks/${task.id}/upload`,{method:'POST',body:form});dirty=false;render();return;}
    if(name==='mapping') {
      const n=id=>!$(id)||$(id).value===''?null:Number($(id).value);
      if(n('gw-map-id')===null)throw new Error('请选择玩家唯一 ID 列');
      const mapping={id_col:n('gw-map-id'),count_col:task.rules.requires_count?n('gw-map-count'):null,adv_col:task.mapping?.adv_col??null,open_text_cols:[...root.querySelectorAll('[data-answer-col]:checked')].map(e=>+e.dataset.answerCol)};
      if(!mapping.open_text_cols.length)throw new Error('请至少选择一列主观题');
      if(task.rules.requires_count){const minimum=n('gw-min-count');if(mapping.count_col===null||!Number.isInteger(minimum)||minimum<1)throw new Error('请选择数据校验列，并填写至少为 1 的最低实际次数');if(minimum!==task.rules.min_count)await mutate('rules',{rules:{...task.rules,min_count:minimum}},'PUT');}
      await mutate('mapping',{mapping},'PUT');return;
    }
    if(name==='translate-headers'){await mutate('translate-headers',{});return;}
    if(name==='roster') {await mutate('roster',{});return;}
    if(name==='correct'){const panel=button.closest('details'),body={};panel.querySelectorAll('[data-fix]').forEach(el=>{body[el.dataset.fix]=el.dataset.fix==='count'?(el.value===''?null:Number(el.value)):el.value;});await mutate(`feedback/${button.dataset.id}`,body,'PUT');return;}
    if(name==='duplicate'){await mutate('duplicates',{group_id:button.dataset.group,keep_response_id:button.dataset.id});return;}
    if(name==='run'||name==='retry'){const stage=step===3?'ai':'quality';await mutate('run',{stage,...(name==='retry'?{response_ids:[button.dataset.id]}:{})});render();return;}
    if(name==='override'||name==='restore'){const panel=button.closest('details');await mutate(`feedback/${button.dataset.id}/quality`,{label:panel.querySelector('[data-review="label"]').value,note:panel.querySelector('[data-review="note"]').value,restore:name==='restore'},'PUT');return;}
    if(name==='confirm-ai'||name==='confirm-current-ai'){const ids=aiChoices||new Set(task.feedback.filter(row=>!row.excluded&&row.confirmed_ai).map(row=>row.id));if(name==='confirm-current-ai'){root.querySelector('[data-ai-current]').checked?ids.add(button.dataset.id):ids.delete(button.dataset.id);}await mutate('confirm-ai',{response_ids:[...ids]});step=Math.min(4,maxStep());render();return;}
    if(name==='confirm'){await mutate('confirm',{});step=6;render();showToast('当前任务版本已确认，可到奖励整理选择','success');return;}
    if(name==='adv'){const panel=button.closest('details');await mutate('player-adv',{player_id:button.dataset.player,adv:panel.querySelector('[data-player-adv]').value,note:panel.querySelector('[data-player-note]').value},'PUT');return;}
    if(name==='close-copy'){$('gw-copy-dialog').close();return;}
    if(name==='copy') {
      clipboardPreview=await api(`/api/group-tasks/${task.id}/clipboard?version=${task.version}&source_id=${encodeURIComponent(groupFilter)}`);
      root.querySelector('#gw-copy-dialog')?.remove();
      root.insertAdjacentHTML('beforeend',`<dialog id="gw-copy-dialog" class="workflow-dialog" aria-labelledby="gw-copy-title"><h2 id="gw-copy-title">任务记录 · 复制预览</h2><p>${h(clipboardPreview.scope)} · ${clipboardPreview.count} 行 · 无表头 12 列</p><textarea readonly aria-label="待复制任务记录" rows="10">${h(clipboardPreview.text)}</textarea><p class="workflow-muted">Tab 分列，换行分行；保留 playerid 文本与 ori、状态查询公式。</p><div class="workflow-toolbar">${btn('close-copy','关闭')}${btn('copy-confirm','复制内容',true)}</div></dialog>`);
      $('gw-copy-dialog').showModal();return;
    }
    if(name==='copy-confirm') {
      const payload=clipboardPreview;if(!payload)return;
      if(!navigator.clipboard)throw new Error('浏览器不支持剪贴板写入，请使用安全连接或本机地址，并允许剪贴板访问');
      if(window.ClipboardItem&&navigator.clipboard.write)await navigator.clipboard.write([new ClipboardItem({'text/plain':new Blob([payload.text],{type:'text/plain'}),'text/html':new Blob([payload.html],{type:'text/html'})})]);
      else await navigator.clipboard.writeText(payload.text);
      showToast(`已复制${payload.scope}，${payload.count} 行任务记录`,'success');
    }
  }
  root.addEventListener('input', e=>{
    const el=e.target;
    if(!task||!el.matches('input,textarea,select')||el.matches('[data-filter],#gw-show-all'))return;
    dirty=true;if(el.dataset.rule)changedFields.add(el.dataset.rule);
  });
  root.addEventListener('click',async e=>{const button=e.target.closest('[data-gw]');if(!button||button.disabled)return;button.disabled=true;try{await action(button);}catch(err){error(err);}finally{if(button.isConnected)button.disabled=false;}});
  setInterval(async()=>{
    if(pollBusy||root.classList.contains('panel--hidden')||document.hidden)return;
    pollBusy=true;
    try {
      if(!task){const data=await api('/api/group-tasks');if(!task&&$('gw-task-list'))renderList(data.tasks);}
      else {const id=task.id,latest=await api(`/api/group-tasks/${id}`);if(task?.id!==id)return;
        $('gw-progress').textContent=progress(latest);$('gw-task-meta').textContent=`${latest.rules.group_name?latest.rules.group_name+' · ':''}${latest.owner_name} · ${statusNames[latest.status]}`;
        $('gw-progress-track').innerHTML=progressBar(latest);
        if(latest.updated_at!==task.updated_at||latest.status!==task.status)$('gw-update-hint').hidden=false;
      }
    }catch{}finally{pollBusy=false;}
  },3000);
  return {open,list};
})();
