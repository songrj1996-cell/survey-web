// Persistent quality tasks. Timers only fetch server state; they never advance progress.
window.GroupTasks = (() => {
  const root = $('group-tasks-panel');
  let scopeEpoch = 0;
  const request = window.workflowRequest;
  const api = async (...args) => {
    const epoch = scopeEpoch;
    try {
      const value = await request(...args);
      if (epoch !== scopeEpoch) throw Object.assign(new Error('查看范围已变更，已忽略旧请求结果'), {scopeChanged:true});
      return value;
    } catch(e) {
      if (epoch !== scopeEpoch) throw Object.assign(new Error('查看范围已变更，已忽略旧请求结果'), {scopeChanged:true});
      throw e;
    }
  };
  const statusNames = {paused:'已暂停',pausing:'暂停中',awaiting_ai_confirmation:'待确认 AI 作答',draft:'待配置',input_issues:'输入待处理',ready:'待标注',queued:'排队中',running:'标注中',incomplete:'待补齐',annotation_done:'标注完成',awaiting_confirmation:'待确认',confirmed:'可结算'};
  let task = null, step = 1, dirty = false, listFilter = '', groupFilter = '', qualityFilter = '', showAllInputs = false, pollBusy = false;
  let uploadView=false, settingsView=false, requestBusy=0, resultTab='quality', recordBusy=false, recordNotice='';
  let selectedResponse = '', latestList = [], clipboardPreview = null, configuredGroups = [], aiChoices = null;
  const maxStep = () => Math.min(4, task?.workflow?.max_step || 1);
  async function fetchGroups() { configuredGroups=(await api('/api/group-tasks/groups')).groups||[]; }
  const taskMeta = t => `${t.rules.group_name ? h(t.rules.group_name)+' · ' : ''}${h(t.owner_name)} · ${statusNames[t.status]}`;
  const changedFields = new Set();
  let ruleSave = null, ruleSaveNotice = null;
  const h = value => esc(String(value ?? ''));
  const btn = (action, text, primary=false, disabled=false, extra='') => `<button type="button" class="btn ${primary?'btn--primary':'btn--ghost'}" data-gw="${action}" ${disabled?'disabled':''} ${extra}>${text}</button>`;
  const busy = () => task && ['queued','running','pausing','paused'].includes(task.status);
  const editable = () => task?.can_edit && !busy() && !ruleSave;
  const deletable = () => task?.can_edit && !['queued','running','pausing'].includes(task.status) && !ruleSave;
  const disabled = () => editable() ? '' : 'disabled';
  const error = e => { if (!e.scopeChanged) showToast(e.message, 'error', 6500); };
  async function mayLeave() { if(ruleSave){showToast('正在保存任务信息，请等待保存结束','error');return false;} if(recordBusy){showToast('正在处理任务记录，请等待当前操作结束','error');return false;} if (!dirty) return true; if (!await window.workflowConfirm('当前任务有未保存的填写。继续编辑会保留原文；放弃后才会切换页面。')) return false; dirty=false; aiChoices=null; changedFields.clear(); return true; }
  window.groupWorkflowMayLeave = mayLeave;
  window.groupWorkflowHasDraft = () => dirty || Boolean(ruleSave);
  window.addEventListener('beforeunload', event => { if (dirty || ruleSave) { event.preventDefault(); event.returnValue = ''; } });
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
  function listState(t) {
    const a=t.annotation||{},run=t.run||{};
    const state=(label,tone,button,hint,needsAction=false)=>({label,tone,button:t.can_edit?button:'查看详情',hint:!t.can_edit&&needsAction?'当前任务需要处理；请联系任务负责人处理，可进入查看详情。':hint});
    if(t.status==='paused'||run.status==='paused')return state('已暂停','warning','查看进度',run.timing_partial?'服务曾中断，任务保持暂停；继续时未确认的远端请求可能产生重复模型费用。':'已保存当前结果，点击“继续任务”后处理剩余反馈。',true);
    if(t.status==='pausing'||run.pause_requested&&['queued','running'].includes(run.status))return state('暂停中','warning','查看进度','当前已开始的反馈保存后暂停；期间仍可能调用模型。');
    // A partial batch can fail while other batches are still running or queued.
    if(['queued','running'].includes(t.status)||['queued','running'].includes(run.status)){
      const queued=t.status==='queued'||run.status==='queued';
      return state(queued?'排队中':run.stage==='ai'?'AI 识别中':'质量打标中','neutral','查看进度',queued?'后台排队中，无需重复操作。':'后台正在处理，无需重复操作。');
    }
    if(t.status==='draft')return state('待配置','warning','完善任务','请补齐任务规则并上传反馈。',true);
    if(t.status==='input_issues')return state('输入待处理','warning','处理输入问题','请处理输入检查中的待办项。',true);
    // Current completeness/phase wins over errors left by an earlier run.
    if(a.phase==='ai_review'||t.status==='awaiting_ai_confirmation')return state('待确认 AI 作答','warning','确认 AI 作答','请完成人工复核，才能继续后续流程。',true);
    if(a.complete||['annotation_done','awaiting_confirmation','confirmed'].includes(t.status)){
      if(t.status==='confirmed')return state('可结算','neutral','查看结果','可进入任务记录核对并复制结果。');
      if(t.status==='annotation_done')return state('标注完成','neutral','查看结果','本任务所选标注已完成。');
      return state('待确认','warning','查看结果','请进入任务记录处理待办事项。',true);
    }
    const stage=a.phase==='ai_progress'?'ai':a.phase==='quality_progress'?'quality':null;
    if(t.annotation_auto_error&&stage==='quality')return state('后续标注未启动','error','查看并处理','质量打标未能自动启动；请进入查看失败提示，处理后启动质量打标。',true);
    const stopped=stage&&run.stage===stage&&(run.status==='incomplete'||Boolean(run.error)||run.status==='completed'&&t.status==='incomplete');
    if(stopped){
      const name=stage==='ai'?'AI 识别':'质量打标',reason=String(run.error||'');
      let hint='本轮已停止，仍有未完成反馈。';
      if(/服务.*(中断|停止)/.test(reason))hint='服务中断，已保存成功结果；重试可能产生重复模型费用。';
      else if(/预算|耗时限制/.test(reason))hint='本轮已达到执行限制，仍有未完成反馈。';
      else if(/权限/.test(reason)&&/凭据|密钥|API Key/i.test(reason))hint='执行权限或模型凭据不可用，请进入查看具体提示。';
      else if(/凭据|密钥|API Key/i.test(reason))hint='模型凭据不可用，请进入查看提示并检查个人模型设置。';
      else if(/权限/.test(reason))hint='执行权限不可用，请进入查看提示并联系管理员处理。';
      return state(`${name}未完成`,'error','查看并重试',`${hint} 请进入查看失败提示，处理后重试${stage==='ai'?' AI 识别':'质量打标'}。`,true);
    }
    if(t.status==='incomplete')return state('标注待补齐','error','查看并处理','当前未在运行；请进入确认缺失项并继续处理。',true);
    if(t.status==='ready'||stage){
      const ai=stage?stage==='ai':a.options?.ai_detect??t.rules.ai_detect;
      return state(ai?'待开始 AI 识别':'待开始质量打标','warning',ai?'前往开始识别':'前往开始打标',ai?'请确认标注设置并开始识别。':'请开始质量打标。',true);
    }
    return state(statusNames[t.status]||'状态待确认','warning','查看并处理','请进入查看任务当前状态。',true);
  }
  function runControls(t) {
    if(!t.can_edit||!t.run?.id)return '';
    const attrs=`data-id="${h(t.id)}" data-run-id="${h(t.run.id)}" data-version="${t.version}"`;
    if(t.status==='paused')return btn('resume','继续任务',false,false,attrs);
    if(t.status==='pausing'||t.run.pause_requested&&['running','queued'].includes(t.run.status))return btn('pause','正在暂停…',false,true,attrs);
    return ['running','queued'].includes(t.status)?btn('pause','暂停任务',false,false,attrs):'';
  }
  async function open({home=false}={}) {
    if(ruleSave)return;
    if(home){
      if(!await mayLeave())return;
      task=null;step=1;settingsView=false;uploadView=false;resultTab='quality';recordNotice='';selectedResponse='';listFilter='';groupFilter='';qualityFilter='';showAllInputs=false;aiChoices=null;clipboardPreview=null;changedFields.clear();
    }
    root.classList.remove('panel--hidden');if(dirty)return;if(task)await load(task.id);else await list();
  }
  async function list() {
    if(ruleSave)return;
    try {
      const data = await api('/api/group-tasks'); task = null; settingsView=false; resultTab='quality'; recordNotice=''; root.classList.remove('workflow-annotation');
      root.innerHTML = `<div class="workflow-heading"><div><h1 class="panel__title">质量标注</h1><p class="panel__desc">从任务规则到反馈质量与奖励，记录和进度随时可继续。</p></div>${btn('new','新建任务',true,!PlatformPermissions.canUse('annotate'))}</div>
        <div id="gw-list-summary" class="workflow-summary"></div><div class="workflow-toolbar"><input id="gw-list-filter" aria-label="搜索任务" placeholder="搜索任务或负责人" value="${h(listFilter)}">${btn('list','刷新列表')}<span class="workflow-muted">后台标注不受页面关闭影响</span></div><div id="gw-task-list"></div>`;
      renderList(data.tasks);
      $('gw-list-filter').oninput = e => { listFilter=e.target.value; renderList(latestList); };
    } catch(e) { error(e); }
  }
  function renderList(items) {
    latestList=items;
    $('gw-list-summary').innerHTML=[['标注中',items.filter(t=>['running','pausing'].includes(t.status)).length],['排队中',items.filter(t=>t.status==='queued').length],['待处理任务',items.filter(t=>!['confirmed','annotation_done'].includes(t.status)).length],['可结算任务',items.filter(t=>t.status==='confirmed').length]].map(([label,value])=>`<span>${label}<strong>${value}</strong></span>`).join('');
    const filtered = items.filter(t => `${t.rules.name} ${t.owner_name}`.toLowerCase().includes(listFilter.toLowerCase())).sort((a,b)=>Number(['running','queued','pausing'].includes(b.status))-Number(['running','queued','pausing'].includes(a.status)));
    $('gw-task-list').innerHTML = filtered.length ? `<div class="workflow-table-wrap"><table class="workflow-table"><thead><tr><th>任务</th><th>负责人</th><th>发奖日期</th><th>状态与进度</th><th></th></tr></thead><tbody>${filtered.map(t=>{
      const s=listState(t);
      return `<tr><td><strong>${h(t.rules.name||'未命名任务')}</strong><small>${t.rules.group_name?h(t.rules.group_name)+' · ':''}${t.summary.player_count} 位玩家 · ${t.summary.feedback_count} 份反馈</small></td><td>${h(t.owner_name)}</td><td>${h(t.rules.payout_date||'待确认')}</td><td class="gw-list-status"><span class="workflow-badge gw-list-badge--${s.tone}">${h(s.label)}</span>${progressBar(t)}<small>${h(progress(t))}</small><small class="gw-list-hint gw-list-hint--${s.tone}">${h(s.hint)}</small></td><td>${btn('view',s.button,false,false,`data-id="${h(t.id)}" data-result-ready="${Boolean(t.annotation?.complete)}"`)}${runControls(t)}${t.can_edit?btn('settings','任务设置',false,false,`data-id="${t.id}"`):''}</td></tr>`;
    }).join('')}</tbody></table></div>` : '<div class="workflow-empty">尚无匹配任务。新建任务后，依次设置规则、上传核查、标注与计奖。</div>';
  }
  async function load(id) { await fetchGroups(); task=await api(`/api/group-tasks/${id}`); render(); }
  function render() {
    step=Math.min(step,maxStep()); if(step===3&&!task.rules.ai_detect)step=4;
    const annotationView=!settingsView&&step>=2&&step<=4;
    const stageResume=annotationView&&step>=3&&task.status==='paused'&&['ai_progress','quality_progress'].includes(task.annotation?.phase);
    root.classList.toggle('workflow-annotation',annotationView);
    root.innerHTML = `<div class="workflow-heading gw-task-heading"><div class="gw-task-title"><h1 class="panel__title" title="${h(task.rules.name||'新任务')}">${h(task.rules.name||'新任务')}</h1><p id="gw-task-meta" class="panel__desc">${taskMeta(task)}</p></div><div class="gw-task-actions"><span id="gw-progress" class="workflow-badge">${h(progress(task))}</span>${stageResume?'':runControls(task)}${annotationView?'':btn('back','← 返回任务列表')}</div></div>
      <div id="gw-annotation-navigation" class="gw-annotation-navigation" hidden></div>
      ${!task.can_edit?'<p class="workflow-notice">当前为只读查看，标注与发奖结果由任务负责人维护。</p>':''}
      ${busy()?`<p class="workflow-notice">${task.status==='paused'?'任务已暂停，点击“继续任务”后恢复处理。':task.status==='pausing'?'暂停中，当前已开始的反馈保存后暂停；期间仍可能调用模型。':'正在排队或标注。'}暂时不能修改本任务的文件、题目选择和标注规则；可以查看进度或处理其他任务。</p>`:''}
      <div id="gw-progress-track" hidden></div><div id="gw-update-hint" hidden class="workflow-notice">任务信息已更新。${btn('reload','查看最新结果')}</div><p id="gw-rule-save-status" class="workflow-muted" role="status" aria-live="polite" hidden></p><div id="gw-content"></div>`;
    if(settingsView)renderSettings();else [null,renderRules,renderInputs,renderAI,renderJudgments][step]();
    if(ruleSaveNotice?.taskId===task.id)showRuleSaveNotice(ruleSaveNotice.message,ruleSaveNotice.failed);
  }
  function ruleField(key,label,type='text',hint='') {
    const required=['name','payout_date','base_reward','excellent_reward','min_count'].includes(key);
    const missing=task.rules[key]===null||task.rules[key]===undefined||String(task.rules[key]).trim()==='';
    return `<label class="workflow-field gw-rule-field ${missing?'gw-field--missing':'gw-field--filled'} ${key==='payout_date'?'gw-field--manual':''}">${label}${required?'<span class="gw-required">必填</span>':''}<input ${disabled()} data-rule="${key}" type="${type}" ${required?'required':''} ${type==='number'?'min="0" step="1"':''} value="${h(task.rules[key])}"><small data-suggestion="${key}" class="workflow-muted">${hint}</small></label>`;
  }
  function updateFieldState(field) {
    const label=field.closest('.gw-rule-field');if(!label)return;
    const missing=field.value.trim()==='';label.classList.toggle('gw-field--missing',missing);label.classList.toggle('gw-field--filled',!missing);
  }
  function groupSetting() {
    const selected=task.rules.group_source_ids??(task.rules.group_source_id?[task.rules.group_source_id]:[]);
    const names=task.rules.group_names?.length?task.rules.group_names:[task.rules.group_name];
    const options=[...configuredGroups,...selected.filter(id=>!configuredGroups.some(g=>g.id===id)).map(id=>({id,name:names[selected.indexOf(id)]||id,inactive:true}))];
    const selectedNames=selected.map(id=>options.find(g=>g.id===id)?.name||id).join('、');
    return `<fieldset class="workflow-field gw-task-groups"><legend>任务归属群组（可多选）</legend><details class="gw-task-group-select"><summary data-group-summary aria-label="任务归属群组（可多选）" ${editable()?'':'aria-disabled="true" tabindex="-1"'}><span data-group-summary-text>${h(selectedNames||'请选择群组')}</span><span class="gw-task-group-caret" aria-hidden="true"></span></summary><div class="gw-task-group-options">${options.map(g=>`<label><input type="checkbox" data-task-group value="${h(g.id)}" data-group-name="${h(g.name)}" ${selected.includes(g.id)?'checked':''} ${disabled()}><span>${h(g.name)}${g.inactive?'（已停用）':''}</span></label>`).join('')||'<span class="workflow-muted">暂无已配置的群组</span>'}</div></details><small class="workflow-muted">仅用于记录任务归属，可不选</small></fieldset>`;
  }
  function selectedGroupIds() { return [...root.querySelectorAll('[data-task-group]:checked')].map(input=>input.value); }
  function updateGroupSummary(input) {
    const control=input.closest('.gw-task-group-select');
    control.querySelector('[data-group-summary-text]').textContent=[...control.querySelectorAll('[data-task-group]:checked')].map(item=>item.dataset.groupName).join('、')||'请选择群组';
  }
  function renderSettings() {
    $('gw-content').innerHTML=`<section class="workflow-card"><h2>任务设置</h2><div class="workflow-form-grid">${ruleField('name','任务名称')}${ruleField('payout_date','奖励发放日期','date','请手动确认发奖日期')}${ruleField('base_reward','每份基础奖励（钻石）','number')}${ruleField('excellent_reward','每份优秀额外奖励（钻石）','number','额外追加，不包含基础奖励')}${groupSetting()}</div><p class="workflow-muted">标注依据和原始反馈已固定。反馈有变化时，请新建任务。</p><div class="workflow-toolbar">${btn('save-settings','保存设置',true,!editable())}</div></section><section class="workflow-card gw-delete-card"><h2>删除任务</h2><p>删除整条任务及标注记录，已生成的历史发奖批次保留。</p>${['queued','running','pausing'].includes(task.status)?'<p class="workflow-muted">正在排队、标注或暂停中，请等待任务已暂停或处理结束后再删除。</p>':task.status==='paused'?'<p class="workflow-muted">任务已暂停，可以删除；删除后将无法继续本任务。</p>':''}${btn('delete-task','删除任务',false,!deletable())}</section>`;
  }
  function renderRules() {
    $('gw-content').innerHTML = `<section class="workflow-card"><h2>任务公告</h2><p class="workflow-muted">可选，识别后仍可手动修改。</p>
      <label class="workflow-field gw-announcement-field">任务公告（可选）<textarea ${disabled()} data-rule="announcement" rows="5" placeholder="粘贴公告，或直接填写下方规则">${h(task.rules.announcement)}</textarea></label>
      <div class="workflow-toolbar gw-extraction-actions"><p id="gw-extraction-status" class="gw-extraction-status" role="status" aria-live="polite">尚未识别</p>${btn('extract','识别并预填',false,!editable())}<span class="workflow-muted">识别结果可编辑，已手工修改的字段会保留。</span></div>
      </section><section class="workflow-card"><h2>确认任务规则</h2><p class="workflow-muted">奖励按每份保留的完整反馈计算；发奖日期请明确确认。</p><div class="workflow-form-grid">${ruleField('name','任务名称')}${ruleField('payout_date','奖励发放日期','date','需手动填写，公告截止时间不会代入')}${ruleField('base_reward','每份基础奖励（钻石）','number')}${ruleField('excellent_reward','每份优秀额外奖励（钻石）','number','额外追加，不包含基础奖励')}</div>
      <div class="workflow-form-grid"><label class="workflow-field">数据校验<select ${disabled()} data-rule="requires_count"><option value="false" ${!task.rules.requires_count?'selected':''}>不要求实际次数</option><option value="true" ${task.rules.requires_count?'selected':''}>要求实际完成次数</option></select><small data-suggestion="requires_count"></small></label>${ruleField('min_count','最低实际次数','number','不要求数据校验时不适用')}
      <label class="workflow-field">重复提交<select ${disabled()} data-rule="allow_repeat"><option value="false" ${!task.rules.allow_repeat?'selected':''}>不允许，重复组保留一份</option><option value="true" ${task.rules.allow_repeat?'selected':''}>允许，每份独立判断并计奖</option></select><small data-suggestion="allow_repeat"></small></label>
      ${groupSetting()}</div>
      <p id="gw-count-rule" class="workflow-notice"></p><div class="workflow-toolbar">${btn('save-rules','保存并进入上传核查 →',true,!editable())}</div></section>`;
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
  function renderInputIssues(settlement) {
    const issues=settlement?task.input_issues||[]:task.annotation_issues||[];
    const visible=row=>(row.issues||[]).filter(issue=>settlement||!['roster_identity','adv'].includes(issue.kind));
    const groups=(task.duplicate_groups||[]).filter(g=>settlement||!g.roster_only);
    return `<section class="workflow-card"><div class="workflow-heading"><h2>${settlement?'名单匹配与输入核对':'输入检查'}</h2><label><input type="checkbox" id="gw-show-all" ${showAllInputs?'checked':''}> 展开全部输入</label></div><p class="workflow-muted">${issues.length} 项待处理${settlement?' · 名单读取：'+(task.roster_snapshot?.read_at?h(workflowTime(task.roster_snapshot.read_at)):'尚未读取'):''}</p>
      ${issues.filter(issue=>!issue.response_id&&!['duplicate','roster_duplicate'].includes(issue.kind)).map(issue=>`<p class="workflow-error">${h(issue.message)}</p>`).join('')}
      ${groups.map(group=>`<details class="workflow-feedback" ${!group.keep_response_id?'open':''}><summary>重复身份组 · ${group.response_ids.length} 份 · ${group.keep_response_id?'已处理':'请选择保留一份'}</summary>${group.response_ids.map(id=>{const row=task.feedback.find(r=>r.id===id);return `<div class="workflow-subcard"><strong>第 ${row.source_row} 行 · ${h(row.player_id)}</strong>${feedbackText(row)}${btn('duplicate',group.keep_response_id===id?'已保留此份':'保留此份',false,!editable(),`data-group="${group.id}" data-id="${id}"`)}</div>`;}).join('')}</details>`).join('')}
      ${task.feedback.filter(row=>showAllInputs||visible(row).length).map(row=>`<details class="workflow-feedback" ${visible(row).length&&!row.excluded?'open':''}><summary>第 ${row.source_row} 行 · ${h(row.player_id||'唯一 ID 待填写')}</summary>${visible(row).map(issue=>`<p class="workflow-error">${h(issue.message)}</p>`).join('')}${feedbackText(row)}<p class="workflow-muted">原始反馈不可修改。需要修正 ID 或次数时，请修正文件后新建任务。</p></details>`).join('')||'<p class="workflow-muted">没有需要处理的原始输入问题。</p>'}</section>`;
  }
  function bindInputIssues() { if($('gw-show-all'))$('gw-show-all').onchange=async e=>{if(!await mayLeave()){e.target.checked=showAllInputs;return;}showAllInputs=e.target.checked;render();}; }
  function renderSettlement() {
    $('gw-content').innerHTML=`<section class="workflow-card"><div class="workflow-heading"><div><h2>名单与奖励核对</h2><p class="workflow-muted">质量标注已完成。现在读取最新名单，匹配玩家账号、状态和实际所属群。</p></div>${btn('roster',task.roster_snapshot?'重新读取名单':'读取最新名单',true,!editable())}</div></section>${renderInputIssues(true)}<section class="workflow-card"><h2>确认任务结果</h2>${task.confirmation_issues.map(issue=>`<p class="workflow-muted">${h(issue.message)}</p>`).join('')||'<p>名单与质量结果完整，可以确认用于奖励整理。</p>'}<div class="workflow-toolbar">${btn('confirm','确认结果并查看任务记录 →',true,!editable()||!!task.confirmation_issues.length)}</div></section>`;
    bindInputIssues();
  }
  const recordColumns=['发奖日期','任务名称','playerid','ori','ADV','当前群内状态','表单有效','体验次数 / 反馈份数','数据任务完成','基础奖励','优秀追加','其他奖励'];
  const recordsAvailable = value => !!value?.annotation?.complete && value.annotation.options?.quality !== false;
  const recordsConfirmed = () => recordsAvailable(task) && task.confirmed_version===task.version && !(task.confirmation_issues||[]).length && !busy();
  function resultTabs() {
    return '<div class="gw-result-tabs" role="tablist" aria-label="结果查看方式">'+[['quality','质量复核'],['records','任务记录']].map(([value,label])=>`<button type="button" id="gw-tab-${value}" role="tab" data-gw="result-tab" data-tab="${value}" aria-selected="${resultTab===value}" aria-controls="gw-result-body" ${value==='records'&&!recordsAvailable(task)?'disabled title="完成质量标注后可查看任务记录"':''}>${label}</button>`).join('')+'</div>';
  }
  function recordIssues() {
    const confirmed=recordsConfirmed();
    const rows=confirmed?[]:task.feedback.filter(row=>!row.excluded&&!row.reward_excluded&&(row.issues||[]).some(issue=>['roster_identity','adv'].includes(issue.kind)));
    const duplicates=confirmed?[]:(task.duplicate_groups||[]).filter(group=>group.roster_only&&!group.keep_response_id);
    const players=(task.players||[]).filter(p=>p.reward_excluded||!confirmed&&(p.issues||[]).length&&(p.adv_choices?.length>1||!rows.some(row=>row.player_id===p.player_id)));
    const notices=new Map();
    if(!confirmed)(task.confirmation_issues||[]).filter(issue=>!issue.response_id).forEach(issue=>notices.set(issue.message,(notices.get(issue.message)||0)+1));
    if(!rows.length&&!duplicates.length&&!players.length&&!notices.size)return '';
    return `<div class="gw-record-issues">${[...notices].map(([message,count])=>`<p class="workflow-error gw-record-issue-notice">${h(message)}${count>1?`（${count} 项）`:''}</p>`).join('')}
      ${rows.map(row=>`<article class="workflow-feedback gw-record-issue"><h3 class="gw-record-issue-title">第 ${row.source_row} 行 · ${h(row.player_id)}</h3><div class="gw-record-issue-body">${row.issues.map(issue=>`<p class="workflow-error">${h(issue.message)}</p>`).join('')}${row.identity_choices?.length>1?`<label class="workflow-field">选择名单身份<select data-fix="candidate_key" ${disabled()}><option value="">请选择</option>${row.identity_choices.map(c=>`<option value="${h(c.key)}" ${row.identity?.key===c.key?'selected':''}>${h(c.group_name)} · ${h(c.ori)} · ${h(c.status)}</option>`).join('')}</select></label>`:`<p class="workflow-muted">${row.reward_exclusion_available?'可维护名单后重新读取，或排除该玩家在本任务中的全部奖励；反馈和标注结果保留。':'请维护名单后重新读取；原始反馈中的 ID 不在此修改。'}</p>`}</div><div class="gw-record-issue-actions">${row.identity_choices?.length>1?btn('record-identity','保存身份选择',false,!editable()||recordBusy,`data-id="${h(row.id)}"`):''}${row.reward_exclusion_available?btn('record-exclude','排除该玩家奖励',false,!editable()||recordBusy,`data-id="${h(row.id)}"`):''}</div></article>`).join('')}
      ${duplicates.map(group=>`<article class="workflow-feedback gw-record-issue"><h3 class="gw-record-issue-title">重复账号待核对 · ${group.response_ids.length} 份反馈</h3><div class="gw-record-issue-body">${group.response_ids.map(id=>{const row=task.feedback.find(item=>item.id===id);return `<div class="gw-record-duplicate-choice"><span>第 ${row.source_row} 行 · ${h(row.player_id)}</span>${btn('record-duplicate','保留此份',false,!editable()||recordBusy,`data-group="${h(group.id)}" data-id="${h(id)}"`)}</div>`;}).join('')}</div></article>`).join('')}
      ${players.map(p=>`<article class="workflow-feedback gw-record-issue"><h3 class="gw-record-issue-title">玩家 ${h(p.player_id)}</h3><div class="gw-record-issue-body">${p.reward_excluded?'<p class="workflow-muted">已排除该玩家在本任务中的全部奖励；反馈与质量标注结果保留，不纳入复制和发奖名单。</p>':(p.issues||[]).map(issue=>`<p class="workflow-error">${h(issue)}</p>`).join('')}${!p.reward_excluded&&p.adv_choices?.length>1?`<div class="gw-record-adv-fields"><label class="workflow-field">确认任务记录中的 ADV<input data-player-adv value="${h(p.adv)}" ${disabled()}></label><label class="workflow-field">处理说明<input data-player-note ${disabled()}></label></div>`:''}</div><div class="gw-record-issue-actions">${p.reward_excluded?btn('record-restore-reward','恢复该玩家奖励',false,!editable()||recordBusy,`data-id="${h(p.response_ids[0])}"`):p.adv_choices?.length>1?btn('record-adv','保存 ADV 决定',false,!editable()||recordBusy,`data-player="${h(p.player_id)}"`):''}</div></article>`).join('')}</div>`;
  }
  function renderRecords() {
    const all=task.players||[], groups=[...new Map(all.filter(p=>p.source_id).map(p=>[p.source_id,p.group_name])).entries()];
    if(groupFilter&&!groups.some(([id])=>id===groupFilter))groups.push([groupFilter,'原选群（当前无记录）']);
    const players=all.filter(p=>!groupFilter||p.source_id===groupFilter), copyPlayers=players.filter(p=>!p.reward_excluded), confirmed=recordsConfirmed();
    const scope=groupFilter?groups.find(([id])=>id===groupFilter)[1]:'全部群';
    const total=key=>players.every(p=>p[key]!==null&&p[key]!==undefined)?players.reduce((sum,p)=>sum+p[key],0).toLocaleString():'待补齐';
    const notice=recordBusy?recordNotice:confirmed?(copyPlayers.length?'当前任务记录已确认，可以复制':'当前任务记录已确认，本范围没有可复制玩家'):!task.roster_snapshot?'请先读取最新名单，核对玩家账号和群内状态':(task.confirmation_issues||[]).length?'仍有事项待核对，暂不能复制':'当前记录尚未确认或结果已有更新，请核对后确认';
    const status=task.roster_snapshot?`名单读取时间：${h(workflowTime(task.roster_snapshot.read_at))}`:'读取已配置的名单后，在此处理身份匹配和账号冲突。';
    const tableRows=players.map(p=>{const cells=[task.rules.payout_date,task.rules.name,p.player_id,p.ori||'待匹配',p.adv||'—',p.reward_excluded?'已排除奖励':p.identity?.status||'待匹配',p.form_valid?'是':'否',p.data_value??'待补齐',p.data_pass===null?'不适用':p.data_pass?'是':'否',p.base??'待补齐',p.excellent??'待补齐',0];return '<tr>'+cells.map(cell=>`<td>${h(cell)}</td>`).join('')+'</tr>';}).join('');
    $('gw-result-body').innerHTML=`<section class="gw-records"><div class="workflow-heading"><div><h2>任务记录</h2><p class="workflow-muted">一位玩家一行，按固定 12 列复制到现有飞书表格。</p></div>${btn('copy',recordBusy&&recordNotice.includes('复制')?'正在复制…':'复制任务记录',true,!confirmed||recordBusy||!copyPlayers.length)}</div>
      <div class="gw-record-status" role="status" aria-live="polite"><div><strong>${h(notice)}</strong><p>${status}</p><p>账号与群内状态复制为查询公式，粘贴后跟随人员名单更新。</p>${!recordBusy&&recordNotice?`<p class="gw-record-operation">${h(recordNotice)}</p>`:''}</div><div class="workflow-toolbar">${btn('record-roster',task.roster_snapshot?'重新读取名单':'读取最新名单',false,!editable()||recordBusy)}${!confirmed?btn('record-confirm','确认任务记录',true,!editable()||recordBusy||!!task.confirmation_issues?.length):''}</div></div>
      <div class="gw-record-filter"><label>所属群 <select data-filter id="gw-group" ${recordBusy?'disabled':''}><option value="">全部群</option>${groups.map(([id,name])=>`<option value="${h(id)}" ${id===groupFilter?'selected':''}>${h(name)}</option>`).join('')}</select></label><span>复制范围：${h(scope)} · ${copyPlayers.length} 位玩家 · 无表头 12 列${players.some(p=>p.reward_excluded)?' · 已排除 '+players.filter(p=>p.reward_excluded).length+' 位玩家奖励':''}</span></div>
      <div class="gw-record-summary">${[['玩家人数',players.length+' 位'],['基础奖励',total('base')+' 钻'],['优秀追加',total('excellent')+' 钻'],['合计',total('total')+' 钻']].map(([label,value])=>`<div><span>${label}</span><strong>${value}</strong></div>`).join('')}</div>
      ${recordIssues()}
      <div class="workflow-table-wrap gw-record-table" tabindex="0" role="region" aria-label="玩家任务记录，固定 12 列"><table class="workflow-table"><thead><tr>${recordColumns.map((label,i)=>`<th scope="col">${h(label)}${[3,5].includes(i)?'<small class="gw-formula-badge">公式</small>':''}</th>`).join('')}</tr></thead><tbody>${tableRows||'<tr><td colspan="12">当前范围没有玩家记录。</td></tr>'}</tbody></table></div>
      <div class="gw-record-footer"><p class="workflow-muted">复制后直接粘贴到飞书已有表头下；长 ID 保留为文本。${task.rules.allow_repeat?'份数包含保留的无效反馈。':!task.rules.requires_count?'本任务不要求体验次数，该列复制时留空。':''}</p><details><summary>查看 12 列说明</summary><ol class="workflow-copy-columns">${recordColumns.map(label=>`<li>${h(label)}</li>`).join('')}</ol><p class="workflow-muted">ori 与当前群内状态为公式；其他奖励默认 0。不同目标工作簿请按群分别复制。</p></details></div></section>`;
    $('gw-group').onchange=async e=>{if(!await mayLeave()){e.target.value=groupFilter;return;}groupFilter=e.target.value;recordNotice='';render();};
  }
  async function recordAction(name,button) {
    if(recordBusy)return;
    if(!['record-identity','record-adv','record-exclude','record-restore-reward'].includes(name)&&!await mayLeave())return;
    if(recordBusy)return;
    if(name==='copy'&&!recordsConfirmed())throw new Error('请先核对并确认当前版本的任务记录');
    if(name!=='copy'&&!editable())throw new Error('当前任务仅可查看，无法修改记录');
    const id=task.id, version=task.version, epoch=scopeEpoch, source=groupFilter;
    const messages={copy:'正在准备复制内容…','record-roster':'正在读取最新名单…','record-confirm':'正在确认任务记录…','record-identity':'正在保存身份选择…','record-duplicate':'正在保存重复账号决定…','record-adv':'正在保存 ADV 决定…','record-exclude':'正在排除该玩家奖励…','record-restore-reward':'正在恢复该玩家奖励…'};
    let operation,body={};
    if(name==='record-identity'){const panel=button.closest('.gw-record-issue');body={candidate_key:panel.querySelector('[data-fix="candidate_key"]').value};if(!body.candidate_key)throw new Error('请选择名单身份');operation='feedback/'+button.dataset.id;}
    if(name==='record-adv'){const panel=button.closest('.gw-record-issue');body={player_id:button.dataset.player,adv:panel.querySelector('[data-player-adv]').value,note:panel.querySelector('[data-player-note]').value.trim()};if(!body.note)throw new Error('请填写 ADV 处理说明');operation='player-adv';}
    if(['record-exclude','record-restore-reward'].includes(name)){body={reward_excluded:name==='record-exclude'};operation='feedback/'+button.dataset.id;}
    recordBusy=true;recordNotice=messages[name];render();
    try {
      if(name==='copy'){
        if(!navigator.clipboard)throw new Error('浏览器未开放剪贴板，请使用安全连接或本机地址并允许剪贴板访问');
        const payload=api('/api/group-tasks/'+id+'/clipboard?version='+version+'&source_id='+encodeURIComponent(source)).then(value=>{
          if(epoch!==scopeEpoch||task?.id!==id||task.version!==version||groupFilter!==source)throw new Error('记录或复制范围已更新，请重新核对后复制');
          if(value.columns!==12||!value.count)throw new Error('没有可复制的完整 12 列记录');
          return value;
        });
        // Promise-backed ClipboardItems keep the write inside the user's click activation.
        // Both formats share one server request; no extra confirmation dialog is needed.
        let copied;
        try {
          if(window.ClipboardItem&&navigator.clipboard.write){
            const item=new window.ClipboardItem({'text/plain':payload.then(v=>new Blob([v.text],{type:'text/plain'})),'text/html':payload.then(v=>new Blob([v.html],{type:'text/html'}))});
            await navigator.clipboard.write([item]);copied=await payload;
          }else{copied=await payload;await navigator.clipboard.writeText(copied.text);}
        }catch(err){await payload.catch(()=>{});throw err;}
        recordNotice='已复制'+copied.scope+'，'+copied.count+' 行任务记录 · '+workflowTime(new Date().toISOString());
        showToast(recordNotice,'success');
      }else{
        operation=operation||({'record-roster':'roster','record-confirm':'confirm','record-duplicate':'duplicates'}[name]);
        if(name==='record-duplicate')body={group_id:button.dataset.group,keep_response_id:button.dataset.id};
        const value=await api('/api/group-tasks/'+id+'/'+operation,{method:['record-identity','record-adv','record-exclude','record-restore-reward'].includes(name)?'PUT':'POST',body:{expected_version:version,...body}});
        if(epoch!==scopeEpoch||task?.id!==id)return;
        task=value.task||value;dirty=false;changedFields.clear();
        if(value.ok===false)throw new Error(task.roster_error||'名单未能读取，请检查名单来源后重试');
        recordNotice=({'record-roster':'最新名单已读取，请核对后确认任务记录','record-confirm':'当前任务记录已确认','record-identity':'身份选择已保存，请重新核对记录','record-duplicate':'重复账号决定已保存，请重新核对记录','record-adv':'ADV 决定已保存，请重新核对记录','record-exclude':'已排除该玩家奖励，反馈和标注结果已保留','record-restore-reward':'已恢复该玩家奖励，请重新核对名单'})[name]+' · '+workflowTime(new Date().toISOString());
        showToast(recordNotice,'success');
      }
    }catch(err){
      if(epoch!==scopeEpoch)return;
      recordNotice=(name==='copy'?'复制失败：':'任务记录操作失败：')+err.message+(err.status?'（HTTP '+err.status+'）':'')+'；请核对提示后重试。';
      if(err.status===409){try{const latest=await api('/api/group-tasks/'+id);if(task?.id===id)task=latest;}catch{}}
      error(new Error(recordNotice));
    }finally{
      recordBusy=false;
      if(epoch===scopeEpoch&&task?.id===id)render();
    }
  }

  // View markup and rendering functions from 6b4740a; API/state adapters follow.
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
  aiReviewedIds: new Set(),
  aiReviewIndex: 0,
  aiQuestionIndex: 0,
  qualityResults: [],
  qualityCount: 0,
  qualityDurationSeconds: null,
  qualityReviewPlayerId: null,
  qualityQuestionIndex: 0,
  qualityReviewSaving: false,
  missingAiIds: [],
  missingQualityIds: [],
  missingTranslationIds: [],
};
const ANN_EDITABLE_QUALITY_LABELS = ["无效反馈", "有效反馈", "优秀反馈"];
const annTemplates = {
  "1": "<div class=\"panel__header\">\n          <h1 class=\"panel__title\">数据标注 · 上传文件</h1>\n          <p class=\"panel__desc\">上传问卷原始数据，支持 CSV / Excel（最大 50MB）</p>\n        </div>\n        <div class=\"upload-zone\" id=\"ga-upload-zone\">\n          <input type=\"file\" id=\"ga-file-input\" accept=\".csv,.xlsx\" hidden />\n          <div class=\"upload-zone__icon\">\n            <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.5\">\n              <path d=\"M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4\" />\n              <polyline points=\"17 8 12 3 7 8\" />\n              <line x1=\"12\" y1=\"3\" x2=\"12\" y2=\"15\" />\n            </svg>\n          </div>\n          <div class=\"upload-zone__text\">\n            <span class=\"upload-zone__primary\">拖放文件到这里，或点击选择</span>\n            <span class=\"upload-zone__secondary\">支持 CSV / Excel（最大 50MB）</span>\n          </div>\n        </div>",
  "2": "<div class=\"panel__header\">\n          <h1 class=\"panel__title\">列确认 &amp; 标注任务选择</h1>\n          <p class=\"panel__desc\" id=\"ga-preview-meta\">已读取文件</p>\n        </div>\n\n        <!-- 任务选择 -->\n        <div class=\"ann-tasks\" id=\"ga-tasks\">\n          <div class=\"plan-section__title\" style=\"margin-bottom:12px\">\n            <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\">\n              <circle cx=\"12\" cy=\"12\" r=\"10\" />\n              <path d=\"M12 8v4l3 3\" />\n            </svg>\n            选择标注任务（可多选）\n          </div>\n          <label class=\"ann-task-option\">\n            <input type=\"checkbox\" id=\"ga-task-ai-detect\" />\n            <span class=\"ann-task-label\">\n              <strong>AI 作答识别</strong>\n              <span>识别观点和实质内容可能由 AI 生成的回答；AI 润色单独展示，不作为违规依据</span>\n            </span>\n          </label>\n          <label class=\"ann-task-option\">\n            <input type=\"checkbox\" id=\"ga-task-quality\" />\n            <span class=\"ann-task-label\">\n              <strong>回答质量打标</strong>\n              <span>为每道已回答的主观题打上 无效反馈 / 有效反馈 / 优秀反馈 标签；未回答题记为 N/A</span>\n            </span>\n          </label>\n        </div>\n\n        <!-- 调研背景（可选，用于 AI 识别） -->\n        <div class=\"ann-background-block\" id=\"ga-background-block\" style=\"display:none\">\n          <div class=\"ann-background-head\">\n            <label class=\"ann-label\">调研背景说明（可选）</label>\n            <span>仅帮助判断 AI 作答，不会写入导出结果</span>\n          </div>\n          <textarea class=\"plan-input ann-background-textarea\" id=\"ga-background\" rows=\"5\"\n            placeholder=\"例如：这是一份 MLBB 玩家对新英雄技能满意度的调研，主要了解玩家对新英雄操作手感、强度感知和对局体验的看法。可以补充样本来源、活动背景、预期回答风格等。\"></textarea>\n        </div>\n\n        <!-- 列确认 -->\n        <div class=\"col-confirm-header\">\n          <div class=\"plan-section__title\">\n            <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\">\n              <path d=\"M9 11l3 3L22 4\" />\n              <path d=\"M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11\" />\n            </svg>\n            列设置\n          </div>\n        </div>\n        <p class=\"panel__desc\">AI 已自动检测 ID 列和主观题列，请核对。主观题列将用于 AI 识别和质量打标。</p>\n\n        <div class=\"ann-col-config\" id=\"ga-col-config\">\n          <!-- 由 JS 渲染：ID 列选择 + 主观题列多选 -->\n        </div>\n\n        <div class=\"col-confirm-actions\">\n          <button class=\"btn btn--primary\" id=\"ga-btn-start\" disabled>\n            <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\">\n              <polygon points=\"5 3 19 12 5 21 5 3\" />\n            </svg>\n            开始标注\n          </button>\n        </div>",
  "3": "<div class=\"panel__header\">\n          <h1 class=\"panel__title\">AI 作答识别中…</h1>\n          <p class=\"panel__desc\">正在分批分析受访者回答，请耐心等待</p>\n        </div>\n        <div class=\"ann-progress-block\" id=\"ga-ai-progress\">\n          <div class=\"ann-progress-bar-wrap\">\n            <div class=\"ann-progress-bar\" id=\"ga-ai-progress-bar\" style=\"width:0%\"></div>\n          </div>\n          <div class=\"ann-progress-msg\" id=\"ga-ai-progress-msg\">准备中…</div>\n        </div>\n        <div class=\"ann-warn-log\" id=\"ga-ai-warn-log\"></div>",
  "4": "<div class=\"panel__header ann-ai-confirm-header\">\n          <div>\n            <h1 class=\"panel__title\">AI 作答识别结果确认</h1>\n            <p class=\"panel__desc\" id=\"ga-confirm-desc\">先看候选人风险概览，再进入玩家档案逐题确认</p>\n          </div>\n        </div>\n        <div class=\"ann-ai-confirm-summary\">\n          <div class=\"ann-ai-summary-stat\"><span>待复核</span><strong id=\"ga-ai-review-total\">0 位玩家</strong></div>\n          <div class=\"ann-ai-summary-stat\"><span>系统预选</span><strong id=\"ga-ai-default-total\">0 位 AI 作答</strong></div>\n          <div class=\"ann-ai-summary-stat\"><span>人工进度</span><strong id=\"ga-ai-reviewed-total\">0 / 0</strong></div>\n          <div class=\"ann-ai-summary-progress\" id=\"ga-ai-summary-progress\" role=\"progressbar\" aria-label=\"人工复核进度\"\n            aria-valuemin=\"0\" aria-valuemax=\"0\" aria-valuenow=\"0\"><span id=\"ga-ai-summary-progress-bar\"></span></div>\n        </div>\n        <div class=\"ann-ai-confirm-workspace\" id=\"ga-ai-confirm-workspace\">\n          <aside class=\"ann-ai-candidate-pane\" aria-label=\"候选人概览\">\n            <div class=\"ann-ai-pane-heading\">\n              <div><strong>候选人概览</strong><span>按内容生成概率从高到低排列</span></div>\n            </div>\n            <div class=\"ann-ai-candidate-list\" id=\"ga-ai-candidate-list\"></div>\n          </aside>\n          <section class=\"ann-ai-profile-pane\" aria-label=\"玩家档案\">\n            <div class=\"ann-ai-player-nav\">\n              <button type=\"button\" id=\"ga-ai-prev-player\" aria-label=\"上一位玩家\">\n                <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><polyline points=\"15 18 9 12 15 6\" /></svg>\n              </button>\n              <span id=\"ga-ai-player-index\">玩家 0 / 0</span>\n              <button type=\"button\" id=\"ga-ai-next-player\" aria-label=\"下一位玩家\">\n                <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><polyline points=\"9 18 15 12 9 6\" /></svg>\n              </button>\n            </div>\n            <div class=\"ann-ai-profile-header\">\n              <div class=\"ann-ai-player-identity\">\n                <span>玩家档案</span>\n                <h2 id=\"ga-ai-player-id\">—</h2>\n                <em id=\"ga-ai-review-state\">待人工确认</em>\n              </div>\n              <div class=\"ann-ai-probability-grid\">\n                <div><span>内容生成概率</span><strong id=\"ga-ai-probability\">0%</strong></div>\n                <div><span>润色概率</span><strong id=\"ga-ai-polish-probability\">0%</strong></div>\n              </div>\n            </div>\n            <div class=\"ann-ai-judgement\">\n              <div><strong id=\"ga-ai-suggestion\">AI 建议</strong><span>润色概率仅供参考，不参与违规判断</span></div>\n              <p id=\"ga-ai-reason\"></p>\n            </div>\n            <div class=\"ann-ai-evidence-grid\">\n              <div>\n                <strong>AI 支持证据</strong>\n                <p id=\"ga-ai-evidence\"></p>\n              </div>\n              <div>\n                <strong>反向证据</strong>\n                <p id=\"ga-ai-counter-evidence\"></p>\n              </div>\n            </div>\n            <div class=\"ann-ai-question-panel\">\n              <div class=\"ann-ai-question-heading\">\n                <div><span id=\"ga-ai-question-index\">第 0 / 0 题</span><strong id=\"ga-ai-question-title\">—</strong></div>\n                <div class=\"ann-ai-question-nav\">\n                  <button type=\"button\" id=\"ga-ai-prev-question\">上一题</button>\n                  <button type=\"button\" id=\"ga-ai-next-question\">下一题</button>\n                </div>\n              </div>\n              <div class=\"ann-ai-question-dots\" id=\"ga-ai-question-dots\" aria-label=\"题目列表\"></div>\n              <div class=\"ann-ai-answer-block\"><span>玩家原文</span><p id=\"ga-ai-answer-original\"></p></div>\n              <div class=\"ann-ai-answer-block ann-ai-answer-block--translation\"><span>中文翻译</span><p id=\"ga-ai-answer-translation\"></p></div>\n            </div>\n            <div class=\"ann-ai-decision\" role=\"group\" aria-label=\"对这位玩家的整体人工判定\">\n              <strong>对这位玩家的整体人工判定</strong>\n              <div>\n                <button class=\"ann-ai-decision-btn\" id=\"ga-ai-mark-normal\" type=\"button\">\n                  <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M20 21a8 8 0 0 0-16 0\"/><circle cx=\"12\" cy=\"7\" r=\"4\"/><polyline points=\"16 11 18 13 22 9\"/></svg>\n                  正常反馈\n                </button>\n                <button class=\"ann-ai-decision-btn\" id=\"ga-ai-mark-ai\" type=\"button\">\n                  <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><rect x=\"4\" y=\"7\" width=\"16\" height=\"12\" rx=\"2\"/><path d=\"M9 3h6M12 3v4M8 12h.01M16 12h.01M9 16h6\"/></svg>\n                  AI 作答\n                </button>\n              </div>\n            </div>\n          </section>\n        </div>\n        <div class=\"col-confirm-actions ann-ai-confirm-footer\">\n          <span id=\"ga-ai-confirm-footer-status\">请逐位完成人工确认</span>\n          <button class=\"btn btn--primary\" id=\"ga-btn-confirm-ai\">\n            <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.5\">\n              <polyline points=\"20 6 9 17 4 12\" />\n            </svg>\n            确认标注结果，继续下一步\n          </button>\n        </div>",
  "5": "<div class=\"panel__header\">\n          <h1 class=\"panel__title\">回答质量打标中…</h1>\n          <p class=\"panel__desc\">正在分批标注每道主观题的回答质量，请耐心等待</p>\n        </div>\n        <div class=\"ann-progress-block\" id=\"ga-quality-progress\">\n          <div class=\"ann-progress-bar-wrap\">\n            <div class=\"ann-progress-bar\" id=\"ga-quality-progress-bar\" style=\"width:0%\"></div>\n          </div>\n          <div class=\"ann-progress-msg\" id=\"ga-quality-progress-msg\">准备中…</div>\n        </div>\n        <div class=\"ann-warn-log\" id=\"ga-quality-warn-log\"></div>",
  "6": "<div class=\"panel__header\">\n          <h1 class=\"panel__title\">标注结果预览</h1>\n          <p class=\"panel__desc\">查看完整标注、判断依据与回答原文，确认后下载结果</p>\n        </div>\n        <div class=\"ann-done-block\">\n          <div class=\"ann-result-summary\">\n            <div class=\"ann-done-icon\">\n              <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\">\n                <path d=\"M22 11.08V12a10 10 0 1 1-5.93-9.14\" />\n                <polyline points=\"22 4 12 14.01 9 11.01\" />\n              </svg>\n            </div>\n            <div class=\"ann-done-text\" id=\"ga-done-text\">标注完成</div>\n          </div>\n          <div class=\"ann-review-block\" id=\"ga-quality-preview-block\" hidden>\n            <div class=\"ann-quality-review-header\">\n              <div class=\"ann-quality-filter-toolbar\">\n                <label>\n                  <span>整体质量</span>\n                  <select class=\"type-select\" id=\"ga-quality-overall-filter\">\n                    <option value=\"all\">全部整体质量</option>\n                    <option value=\"无效反馈\">无效反馈</option>\n                    <option value=\"有效反馈\">有效反馈</option>\n                    <option value=\"优秀反馈\">优秀反馈</option>\n                  </select>\n                </label>\n                <label>\n                  <span>逐题标签</span>\n                  <select class=\"type-select\" id=\"ga-quality-question-filter\">\n                    <option value=\"all\">全部逐题标签</option>\n                    <option value=\"无效反馈\">含无效反馈</option>\n                    <option value=\"有效反馈\">含有效反馈</option>\n                    <option value=\"优秀反馈\">含优秀反馈</option>\n                    <option value=\"N/A\">含 N/A</option>\n                  </select>\n                </label>\n                <label>\n                  <span>人工修改状态</span>\n                  <select class=\"type-select\" id=\"ga-quality-adjustment-filter\">\n                    <option value=\"all\">全部复核状态</option>\n                    <option value=\"adjusted\">仅看人工改过</option>\n                    <option value=\"untouched\">尚未修改</option>\n                  </select>\n                </label>\n                <span class=\"ann-quality-filter-count\" id=\"ga-quality-filter-count\"></span>\n              </div>\n              \n            </div>\n            <div class=\"ann-quality-summary-grid\" id=\"ga-quality-summary-grid\" aria-live=\"polite\"></div>\n            <div class=\"ann-ai-confirm-workspace ann-quality-review-workspace\" id=\"ga-quality-review-workspace\">\n              <aside class=\"ann-ai-candidate-pane\" aria-label=\"质量结果玩家列表\">\n                <div class=\"ann-ai-pane-heading\">\n                  <div><strong>玩家列表</strong><span>点击进入档案，筛选后只显示匹配玩家</span></div>\n                </div>\n                <div class=\"ann-ai-candidate-list\" id=\"ga-quality-player-list\"></div>\n              </aside>\n              <section class=\"ann-ai-profile-pane ann-quality-profile-pane\" id=\"ga-quality-profile-pane\" aria-label=\"玩家质量档案\"></section>\n            </div>\n          </div>\n          <div class=\"ann-result-actions\">\n            <div class=\"ann-result-actions__primary\">\n              <button class=\"btn btn--primary\" id=\"ga-btn-download\">\n                <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\">\n                  <path d=\"M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4\" />\n                  <polyline points=\"7 10 12 15 17 10\" />\n                  <line x1=\"12\" y1=\"15\" x2=\"12\" y2=\"3\" />\n                </svg>\n                下载标注结果 Excel\n              </button>\n            </div>\n          </div>\n        </div>"
};
function annRenderColConfig(headers, idCol, openTextCols, headersZh, matrixIdxs, emptyColIdxs) {
  const zh = headersZh || headers;
  const otSet = new Set(openTextCols);
  const mxSet = matrixIdxs || new Set();
  const emptySet = emptyColIdxs || new Set();
  const container = annElement('ann-col-config');

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
      <select class="type-select" id="ga-id-col-sel">${idOpts}</select>
    </div>
    <div class="ann-col-row" style="flex-direction:column;align-items:flex-start">
      <label class="ann-label">主观题列（可多选）</label>
      <div class="ann-col-check-list ann-col-check-list--full">${otRows}</div>
    </div>`;

  // 更新 annState
  annElement('ann-id-col-sel').addEventListener('change', e => {
    annState.idCol = +e.target.value;
    annUpdateStartBtn();
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
function annArrangeStep2Layout() {
  const panel = annElement('ann-panel-2');
  const colConfig = annElement('ann-col-config');
  const tasks = annElement('ann-tasks');
  const background = annElement('ann-background-block');
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
  const bgInput = annElement('ann-background');
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
  root.querySelectorAll('.ann-task-option').forEach(option => {
    const input = option.querySelector('input[type="checkbox"]');
    option.classList.toggle('ann-task-option--active', !!input?.checked);
  });
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
  const list = annElement('ann-ai-candidate-list');
  list.innerHTML = annState.reviewAiResults.map((result, index) => {
    const decision = annAiReviewDecision(result);
    return `<button class="ann-ai-candidate-card${index === annState.aiReviewIndex ? ' ann-ai-candidate-card--active' : ''}"
      type="button" data-ann-ai-player-index="${index}">
      <span class="ann-ai-candidate-id">${esc(result.player_id || result.id)}</span>
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
    annElement('ann-ai-question-index').textContent = '第 0 / 0 题';
    annElement('ann-ai-question-title').textContent = '没有可查看的开放题';
    annElement('ann-ai-question-dots').innerHTML = '';
    annElement('ann-ai-answer-original').textContent = '—';
    annElement('ann-ai-answer-translation').textContent = '—';
    annElement('ann-ai-prev-question').disabled = true;
    annElement('ann-ai-next-question').disabled = true;
    return;
  }

  annState.aiQuestionIndex = Math.max(0, Math.min(annState.aiQuestionIndex, total - 1));
  const columnIndex = columns[annState.aiQuestionIndex];
  const key = `col_${columnIndex}`;
  const original = (result.originals || {})[key] || '';
  const translation = (result.translations || {})[key] || '';
  annElement('ann-ai-question-index').textContent = `第 ${annState.aiQuestionIndex + 1} / ${total} 题`;
  annElement('ann-ai-question-title').textContent = annState.headers[columnIndex] || `列 ${columnIndex}`;
  annElement('ann-ai-answer-original').textContent = original || '（未作答）';
  annElement('ann-ai-answer-translation').textContent = translation || '（暂无中文翻译）';
  annElement('ann-ai-prev-question').disabled = annState.aiQuestionIndex === 0;
  annElement('ann-ai-next-question').disabled = annState.aiQuestionIndex === total - 1;
  annElement('ann-ai-question-dots').innerHTML = columns.map((_, index) =>
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

  annElement('ann-ai-player-index').textContent = `玩家 ${annState.aiReviewIndex + 1} / ${total}`;
  annElement('ann-ai-player-id').textContent = result.player_id || rowId;
  annElement('ann-ai-probability').textContent = annAiProbabilityText(result.ai_prob);
  annElement('ann-ai-polish-probability').textContent = annAiProbabilityText(result.polish_prob);
  annElement('ann-ai-review-state').textContent = annAiReviewStatusText(result);
  annElement('ann-ai-review-state').className = `ann-ai-review-state ann-ai-review-state--${decision}`;
  annElement('ann-ai-suggestion').textContent = Number(result.ai_prob || 0) >= annState.aiHighThreshold
    ? 'AI 建议：高概率 AI 作答'
    : 'AI 建议：需结合原文判断';
  annElement('ann-ai-reason').textContent = result.reason || '暂无判断理由';
  annElement('ann-ai-evidence').textContent = result.evidence || '暂无明确支持证据';
  annElement('ann-ai-counter-evidence').textContent = result.counter_evidence || '暂无明确反向证据';
  annElement('ann-ai-prev-player').disabled = annState.aiReviewIndex === 0;
  annElement('ann-ai-next-player').disabled = annState.aiReviewIndex === total - 1;
  annElement('ann-ai-mark-normal').classList.toggle('ann-ai-decision-btn--active', decision === 'normal');
  annElement('ann-ai-mark-normal').setAttribute('aria-pressed', String(decision === 'normal'));
  annElement('ann-ai-mark-ai').classList.toggle('ann-ai-decision-btn--active', decision === 'ai');
  annElement('ann-ai-mark-ai').setAttribute('aria-pressed', String(decision === 'ai'));
  annRenderAiQuestion(result);
}
function annUpdateAiReviewProgress() {
  const total = annState.reviewAiResults.length;
  const reviewed = annState.aiReviewedIds.size;
  const defaultAi = annState.reviewAiResults.filter(
    result => Number(result.ai_prob || 0) >= annState.aiHighThreshold
  ).length;
  annElement('ann-ai-review-total').textContent = `${total} 位玩家`;
  annElement('ann-ai-default-total').textContent = `${defaultAi} 位 AI 作答`;
  annElement('ann-ai-reviewed-total').textContent = `${reviewed} / ${total}`;
  annElement('ann-ai-summary-progress').setAttribute('aria-valuemax', String(total));
  annElement('ann-ai-summary-progress').setAttribute('aria-valuenow', String(reviewed));
  annElement('ann-ai-summary-progress-bar').style.width = total ? `${reviewed / total * 100}%` : '0%';
  annElement('ann-ai-confirm-footer-status').textContent = reviewed === total
    ? `已完成 ${total} 位玩家的人工确认`
    : `请完成剩余 ${total - reviewed} 位玩家的人工确认`;
  annElement('ann-btn-confirm-ai').disabled = !total || reviewed !== total;
}
function annGoToAiPlayer(index) {
  const total = annState.reviewAiResults.length;
  if (!total) return;
  annState.aiReviewIndex = Math.max(0, Math.min(index, total - 1));
  annState.aiQuestionIndex = 0;
  annRenderAiCandidateList();
  annRenderAiProfile();
}
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
    const counts = { '优秀反馈': 0, '有效反馈': 0, '无效反馈': 0 };
    annState.qualityResults.forEach(result => {
      const overall = annCanonicalQualityLabel(result.overall, true);
      if (Object.hasOwn(counts, overall)) counts[overall] += 1;
    });
    lines.push(
      `<div class="ann-summary-line">质量打标结果：优秀反馈 ${counts['优秀反馈']} 条，有效反馈 ${counts['有效反馈']} 条，无效反馈 ${counts['无效反馈']} 条</div>`
    );
    const duration = formatReportDuration(annState.qualityDurationSeconds);
    if (duration) {
      lines.push(`<div class="ann-summary-line">质量打标总耗时：${esc(duration)}</div>`);
    }
  }
  return lines.join('');
}
function annIsEffectivelyEmptyAnswer(value) {
  return !String(value || '').trim();
}
function annCanonicalQualityLabel(label, overall = false) {
  const normalized = String(label || '').trim();
  if (normalized === '普通反馈') return '有效反馈';
  if (overall && normalized === 'N/A') return '无效反馈';
  return normalized;
}
function annQualityQuestionLabel(result, key) {
  if (annIsEffectivelyEmptyAnswer((result?.originals || {})[key])) return 'N/A';
  const label = annCanonicalQualityLabel((result?.q_labels || {})[key]);
  return ANN_EDITABLE_QUALITY_LABELS.includes(label) ? label : '待补齐';
}
function annQualityLabelClass(label) {
  return {
    '无效反馈': 'ann-quality-badge--invalid',
    '有效反馈': 'ann-quality-badge--valid',
    '优秀反馈': 'ann-quality-badge--excellent',
    'N/A': 'ann-quality-badge--na',
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
      : annQualityQuestionLabel({originals: result?.originals, q_labels: {[key]: baseline.label || (result?.q_labels || {})[key]}}, key),
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
  const overallFilter = annElement('ann-quality-overall-filter').value;
  const questionFilter = annElement('ann-quality-question-filter').value;
  const adjustmentFilter = annElement('ann-quality-adjustment-filter').value;
  return annState.qualityResults.filter(result => (
    (overallFilter === 'all' || annCanonicalQualityLabel(result.overall, true) === overallFilter)
    && annQualityMatchesQuestionFilters(result, questionFilter, adjustmentFilter)
  ));
}
function annPreferredQualityQuestionIndex(result) {
  const questionFilter = annElement('ann-quality-question-filter').value;
  const adjustmentFilter = annElement('ann-quality-adjustment-filter').value;
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
  const counts = { '无效反馈': 0, '有效反馈': 0, '优秀反馈': 0 };
  annState.qualityResults.forEach(result => {
    const overall = annCanonicalQualityLabel(result.overall, true);
    if (Object.hasOwn(counts, overall)) counts[overall] += 1;
  });
  annElement('ann-quality-summary-grid').innerHTML = [
    ['全部玩家', annState.qualityResults.length],
    ['无效反馈', counts['无效反馈']],
    ['有效反馈', counts['有效反馈']],
    ['优秀反馈', counts['优秀反馈']],
  ].map(([label, count]) => `
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
  const list = annElement('ann-quality-player-list');
  if (!results.length) {
    list.innerHTML = '<div class="ann-quality-empty">当前筛选没有匹配玩家</div>';
    return;
  }
  list.innerHTML = results.map(result => {
    const adjustedCount = Object.keys(annQualityHumanReviews(result)).length;
    const active = String(result.id) === String(annState.qualityReviewPlayerId);
    const overall = annCanonicalQualityLabel(result.overall, true) || '无效反馈';
    return `<button class="ann-ai-candidate-card ann-quality-player-card${active ? ' ann-ai-candidate-card--active' : ''}"
      type="button" data-ann-quality-player-id="${esc(result.id)}">
      <span class="ann-ai-candidate-id">${esc(result.player_id || result.id)}</span>
      <span class="ann-quality-badge ${annQualityLabelClass(overall)}">${esc(overall)}</span>
      <span class="ann-ai-candidate-reason">${esc(
        adjustedCount ? `已人工调整 ${adjustedCount} 道题` : (result.overall_reason || '暂无整体判断依据')
      )}</span>
      ${adjustedCount ? `<em class="ann-quality-review-status ann-quality-review-status--adjusted">已人工调整 ${adjustedCount} 道题</em>` : ''}
    </button>`;
  }).join('');
}
function annRenderQualityProfile(results) {
  const pane = annElement('ann-quality-profile-pane');
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
  const incomplete = label === '待补齐' || !task.annotation.complete;
  const disabled = emptyAnswer || incomplete || annState.qualityReviewSaving || !editable();
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
        <h2>${esc(result.player_id || result.id)}</h2>
        ${adjustedCount ? `<em class="ann-quality-review-status ann-quality-review-status--adjusted">已人工调整 ${adjustedCount} 道题</em>` : ''}
      </div>
      <div class="ann-quality-overall-card">
        <span>当前整体质量</span>
        <strong class="ann-quality-badge ${annQualityLabelClass(annCanonicalQualityLabel(result.overall, true))}">${esc(annCanonicalQualityLabel(result.overall, true) || '无效反馈')}</strong>
        <p>${esc(result.overall_reason || '')}</p>
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
          <p>${esc(baseline.reason || '未提供判断依据')}</p>
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
              ? '质量标签待补齐，完成后可按需调整'
            : review
              ? `人工调整记录：${esc(annCanonicalQualityLabel(review.from_label))} → ${esc(annCanonicalQualityLabel(review.to_label))}；下载时以当前标签为准`
              : emptyAnswer
                ? '当前标签固定为 N/A；不参与整体质量计算'
                : '当前采用 AI 标签；如有异议，可按需调整'}
        </div>
      </div>
    </div>`;
}
function annRenderQualityPreview() {
  const block = annElement('ann-quality-preview-block');
  if (!annState.tasks.quality || annState.qualityResults.length === 0) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  const results = annFilteredQualityResults();
  annCurrentQualityResult(results);
  annElement('ann-quality-filter-count').textContent = `当前显示 ${results.length} / ${annState.qualityResults.length} 位玩家`;
  annRenderQualitySummary();
  annRenderQualityPlayerList(results);
  annRenderQualityProfile(results);
}
function annGoToQualityPlayer(playerId) {
  const result = annState.qualityResults.find(item => String(item.id) === String(playerId));
  if (!result) return;
  annState.qualityReviewPlayerId = String(result.id);
  annState.qualityQuestionIndex = annPreferredQualityQuestionIndex(result);
  annRenderQualityPreview();
}

function annElement(id) { return $(id.startsWith('ann-') ? id.replace(/^ann-/, 'ga-') : 'ga-'+id); }
function syncAnnotation() {
  const a=task.annotation;
  annState.headers=task.headers;annState.headersZh=task.headers_zh||task.headers;
  annState.openTextCols=task.mapping?.open_text_cols||[];annState.idCol=task.mapping?.id_col??task.detected_id_col??0;
  annState.tasks=task.mapping||task.annotation_options?a.options:{ai_detect:false,quality:false};annState.aiReviewThreshold=a.review_threshold;annState.aiHighThreshold=a.high_threshold;
  const adapt=(row,value)=>({...value,id:row.id,player_id:row.player_id,originals:Object.fromEntries((row.answers||[]).map(v=>['col_'+v.column,v.answer]))});
  annState.aiResults=task.feedback.filter(r=>!r.excluded&&r.ai_complete).map(r=>adapt(r,r.ai));
  annState.reviewAiResults=annState.aiResults.filter(r=>a.review_ids.includes(r.id)).sort((x,y)=>y.ai_prob-x.ai_prob);
  annState.aiReviewedIds=new Set(a.reviewed_ids);
  annState.confirmedAiIds=new Set(task.feedback.filter(r=>a.review_ids.includes(r.id)&&(r.ai_reviewed?r.ai_decision:task.ai_confirmation_complete?r.confirmed_ai:Number(r.ai?.ai_prob)>=a.high_threshold)).map(r=>r.id));
  annState.qualityResults=task.feedback.filter(r=>!r.excluded&&!r.confirmed_ai&&r.quality_complete).map(r=>adapt(r,{...r.quality,...(r.manual_quality?{overall:r.manual_quality.label,overall_reason:r.manual_quality.note}:{})}));
  annState.qualityCount=annState.qualityResults.length;
  annState.qualityDurationSeconds=[...(task.runs||[]),task.run].filter(r=>r?.stage==='quality').reduce((sum,r)=>sum+(r.execution_seconds||0),0);
}
function annSteps(n) {
  const names=['上传数据','列确认 & 任务','AI 作答识别','AI 确认','质量打标','结果预览'];
  return `<div class="step-bar" aria-label="标注进度">${names.map((label,i)=>`<button type="button" disabled class="step-bar__item ${i+1===n?'step-bar__item--active':i+1<n?'step-bar__item--done':''}"><span class="step-bar__num">${i+1}</span><span class="step-bar__label">${label}</span></button>`).join('')}</div>`;
}
function annPage(n) {
  const navigation=$('gw-annotation-navigation');
  navigation.hidden=false;
  navigation.innerHTML=annSteps(n)+btn('back','← 返回任务列表');
  $('gw-content').innerHTML=`<div id="ga-panel-${n}" class="group-annotation-page">${annTemplates[n]}</div>`;
  const bar=navigation.querySelector('.step-bar'),active=bar.querySelector('.step-bar__item--active');
  if(active)bar.scrollLeft=Math.max(0,active.getBoundingClientRect().right-bar.getBoundingClientRect().right);
}
function bindAnn(id,fn) { const el=annElement(id);if(el)el.onclick=async event=>{if(el.disabled)return;try{await fn(event);}catch(e){error(e);}}; }
function annUpdateStartBtn() {
  const issues=[];
  if(!(annState.tasks.ai_detect||annState.tasks.quality))issues.push('请至少选择一项标注任务');
  if(!annState.openTextCols.length)issues.push('请至少选择一列主观题');
  if(annState.openTextCols.includes(annState.idCol))issues.push('玩家唯一 ID 列不能同时作为主观题');
  const count=$('gw-map-count'),minimum=$('gw-min-count');
  if(task.rules.requires_count){
    if(!count||count.value==='')issues.push('请选择数据校验列');
    else if(+count.value===annState.idCol||annState.openTextCols.includes(+count.value))issues.push('数据校验列不能与 ID 或主观题列重复');
    if(!minimum||!Number.isInteger(+minimum.value)||+minimum.value<1)issues.push('请填写至少为 1 的最低实际次数');
  }
  const hint=$('gw-ann-validation');if(hint)hint.textContent=issues.join('；');
  annElement('ann-btn-start').disabled=!editable()||!!issues.length;
}
function renderUpload() {
  annPage(1);
  const input=annElement('ann-file-input'),zone=annElement('ann-upload-zone');
  input.disabled=!editable();input.onchange=()=>uploadAnnotationFile(input.files[0]);
  zone.onclick=()=>{if(editable())input.click();};
  zone.ondragover=e=>{e.preventDefault();};zone.ondrop=e=>{e.preventDefault();if(editable())uploadAnnotationFile(e.dataTransfer.files[0]);};
  if(task.headers.length)$('gw-content').insertAdjacentHTML('beforeend',`<div class="workflow-toolbar">${btn('columns','返回列确认',true)}</div>`);
}
async function uploadAnnotationFile(file) {
  if(!file)return;
  requestBusy++;
  try {
    if(task.feedback.length)throw new Error('该任务已有原始反馈，反馈变动请新建任务');
    const zone=annElement('ann-upload-zone');zone.classList.add('upload-zone--loading');
    zone.querySelector('.upload-zone__primary').textContent='正在读取：'+file.name;
    const form=new FormData();form.append('file',file);form.append('expected_version',String(task.version));
    task=await api(`/api/group-tasks/${task.id}/upload`,{method:'POST',body:form});dirty=false;uploadView=false;step=2;render();
  }catch(e){if(!e.scopeChanged){error(e);renderUpload();}}finally{requestBusy--;}
}
function renderInputs() {
  if(!task.headers.length||uploadView){renderUpload();return;}
  syncAnnotation();annPage(2);
  annElement('ann-preview-meta').textContent=`${task.uploads.at(-1)?.filename||''} · ${task.feedback.length} 行数据 · ${task.headers.length} 列`;
  const mapping=task.mapping||{...task.mapping_suggestion,id_col:task.mapping_suggestion?.id_col??task.detected_id_col??0,open_text_cols:task.detected_open_text_cols||[]};
  annRenderColConfig(task.headers,mapping.id_col,mapping.open_text_cols,task.headers_zh,new Set(task.matrix_col_idxs||[]),new Set(task.empty_col_idxs||[]));
  if(task.rules.requires_count){
    annElement('ann-col-config').firstElementChild.insertAdjacentHTML('afterend',`<div class="ann-col-row"><label class="ann-label" for="gw-map-count">数据校验列 <span class="rr-required">必填</span></label><select id="gw-map-count" class="type-select">${columnOptions(mapping.count_col)}</select><label class="ann-label" for="gw-min-count">校验条件：实际次数 ≥</label><input id="gw-min-count" type="number" min="1" step="1" value="${task.rules.min_count??''}" readonly aria-label="最低实际次数"></div>`);
    $('gw-map-count').onchange=annUpdateStartBtn;$('gw-min-count').oninput=annUpdateStartBtn;
  }
  root.querySelectorAll('.ann-col-check-item').forEach(el=>{const i=+el.querySelector('input').value;el.insertAdjacentHTML('beforeend',`<span class="workflow-badge">${h(task.column_types?.[i]||'待确认')}</span>`);});
  annArrangeStep2Layout();
  annElement('task-ai-detect').checked=annState.tasks.ai_detect;annElement('task-quality').checked=annState.tasks.quality;
  annElement('ann-background').value=task.annotation.options.background;
  const sync=()=>{annState.tasks={ai_detect:annElement('task-ai-detect').checked,quality:annElement('task-quality').checked};annElement('ann-background-block').style.display=annState.tasks.ai_detect?'':'none';annSyncTaskCards();annUpdateStartBtn();};
  annElement('task-ai-detect').onchange=sync;annElement('task-quality').onchange=sync;
  annElement('ann-btn-start').parentElement.insertAdjacentHTML('beforebegin','<p id="gw-ann-validation" class="workflow-error" role="status"></p>');
  if(task.header_translation_warning)annElement('ann-col-config').insertAdjacentHTML('beforebegin',`<p class="workflow-notice">${h(task.header_translation_warning)} ${btn('translate-headers','重试列名翻译',false,!editable())}</p>`);
  if((task.annotation_issues||[]).some(i=>!['upload','mapping'].includes(i.kind))){annElement('ann-tasks').insertAdjacentHTML('beforebegin',renderInputIssues(false));bindInputIssues();}
  root.querySelectorAll('#ga-panel-2 input,#ga-panel-2 select,#ga-panel-2 textarea').forEach(el=>el.disabled=!editable());
  sync();bindAnn('ann-btn-start',startAnnotation);

}
async function startAnnotation() {
  annUpdateStartBtn();if(annElement('ann-btn-start').disabled)return;
  const options={ai_detect:annElement('task-ai-detect').checked,quality:annElement('task-quality').checked,background:annElement('ann-background').value.trim()};
  const mapping={id_col:annState.idCol,open_text_cols:[...annState.openTextCols],count_col:task.rules.requires_count?+$('gw-map-count').value:null,adv_col:task.mapping?.adv_col??null};
  if(task.rules.requires_count&&+$('gw-min-count').value!==task.rules.min_count)throw new Error('数据校验条件已固定，请新建任务');
  task=await api(`/api/group-tasks/${task.id}/mapping`,{method:'PUT',body:{expected_version:task.version,mapping,annotation_options:options}});
  dirty=false;changedFields.clear();
  if(task.annotation_issues.length){render();return;}
  step=options.ai_detect?3:4;render();
  await startAnnotationStage();
}
async function startAnnotationStage() {
  const phase=task.annotation.phase;
  if(!['ai_progress','quality_progress'].includes(phase)){render();return;}
  const stage=phase==='ai_progress'?'ai':'quality';
  await mutate('run',{stage});
}
function renderAnnotationProgress(stage) {
  const ai=stage==='ai',a=task.annotation,run=task.run,active=run?.stage===stage&&['running','queued'].includes(run.status);
  const suspended=['paused','pausing'].includes(task.status);
  annPage(ai?3:5);
  const prefix=ai?'ai':'quality',done=ai?a.ai_done:a.quality_done,total=ai?a.total:a.quality_total;
  annElement(`ann-${prefix}-progress-bar`).style.width=`${total?done/total*100:100}%`;
  annElement(`ann-${prefix}-progress-msg`).textContent=suspended?`${statusNames[task.status]}：已完成 ${done} / ${total} 份反馈`:active?`${run.status==='queued'?'排队等待中':'正在分批分析'}：已完成 ${done} / ${total} 份反馈`: `${ai?'AI 识别':'质量打标'}未完成：${done} 条有效结果，仍有 ${total-done} 行未回填`;
  if(suspended){
    const panel=annElement(`ann-panel-${ai?3:5}`);
    const title=`${ai?'AI 作答识别':'回答质量打标'} · ${statusNames[task.status]}`;
    const description=task.status==='paused'?'当前结果已保存，点击“继续任务”后恢复处理。':'当前已开始的反馈保存后暂停；期间仍可能调用模型。';
    panel.querySelector('.panel__title').textContent=title;
    panel.querySelector('.panel__desc').textContent=description;
    if(task.status==='paused'){
      const heading=panel.querySelector('.panel__header');
      heading.classList.add('gw-stage-progress-heading');
      heading.innerHTML=`<div class="gw-stage-progress-copy"><h1 class="panel__title">${h(title)}</h1><p class="panel__desc">${h(description)}</p></div>${runControls(task)}`;
    }
  }
  const errors=task.feedback.filter(r=>!r.excluded&&r.model_error&&!(ai?r.ai_complete:r.quality_complete||r.confirmed_ai)).map(r=>`第 ${r.source_row} 行：${r.model_error}`);
  if(run?.stage===stage&&run.error)errors.push(run.error);
  if(task.annotation_auto_error)errors.push(task.annotation_auto_error);
  annElement(`ann-${prefix}-warn-log`).innerHTML=errors.map(message=>`<div class="ann-warn-item">${h(message)}</div>`).join('')+(!active&&!suspended&&errors.length?`<div class="ann-warn-item ann-warn-item--error">仍有 ${total-done} 行没有得到完整结果，已停止后续${ai?'质量打标':'结果预览'}。请重试${ai?' AI 识别':'质量打标'}。</div>`:'');
  $('gw-content').insertAdjacentHTML('beforeend',`<div class="workflow-toolbar">${!active&&!suspended?btn('annotation-run',run?.stage===stage?ai?'重试 AI 识别':'重试质量打标':ai?'开始 AI 作答识别':'开始质量打标',true,!editable()):''}</div>`);
}
function renderAI() {
  if(task.annotation.phase==='ai_progress'){renderAnnotationProgress('ai');return;}
  if(task.annotation.phase!=='ai_review'){step=4;renderJudgments();return;}
  syncAnnotation();annPage(4);
  annRenderAiCandidateList();annRenderAiProfile();annUpdateAiReviewProgress();
  annElement('ann-confirm-desc').textContent=`内容生成概率 ≥ ${annState.aiReviewThreshold}% 的玩家进入人工复核；≥ ${annState.aiHighThreshold}% 默认预选。请逐位确认，润色概率仅供参考。`;
  bindAIReview();
  if(!editable())root.querySelectorAll('#ga-ai-mark-normal,#ga-ai-mark-ai,#ga-btn-confirm-ai').forEach(el=>el.disabled=true);
}
async function annSetAiDecision(decision) {
  const current=annCurrentAiReviewResult();if(!current||!editable())return;
  if(annState.aiReviewSaving)return;annState.aiReviewSaving=true;
  try { await mutate(`feedback/${current.id}/ai-review`,{is_ai:decision==='ai'},'PUT'); } catch(e){error(e);} finally {annState.aiReviewSaving=false;}
}
async function finishAIReview() {
  if(annState.aiReviewedIds.size!==annState.reviewAiResults.length)throw new Error('请先完成所有待复核玩家的人工判定');
  await mutate('confirm-ai',{response_ids:[...annState.confirmedAiIds]});step=4;render();await startAnnotationStage();
}
function renderJudgments() {
  if(task.annotation.phase==='ai_progress'||task.annotation.phase==='ai_review'){step=3;renderAI();return;}
  if(task.annotation.phase==='quality_progress'){renderAnnotationProgress('quality');return;}
  if(!recordsAvailable(task))resultTab='quality';
  syncAnnotation();annPage(6);
  const panel=$('ga-panel-6');
  panel.querySelector('.panel__desc').textContent='查看质量复核，或按玩家汇总任务完成情况并复制到飞书';
  panel.querySelector('.panel__header').insertAdjacentHTML('afterend',resultTabs());
  const body=panel.querySelector('.ann-done-block');body.id='gw-result-body';body.setAttribute('role','tabpanel');body.setAttribute('aria-labelledby','gw-tab-'+resultTab);
  if(resultTab==='records'&&recordsAvailable(task)){renderRecords();return;}
  annElement('ann-done-text').innerHTML=annBuildDoneSummary();
  annRenderQualityPreview();bindQualityReview();
  bindAnn('ann-btn-download',()=>{window.location.href=`/api/group-tasks/${task.id}/annotation-download`;});
  annElement('ann-btn-download').disabled=!task.annotation.complete;
}
async function annApplyQualityLabel(label) {
  const result=annCurrentQualityResult(),column=annState.openTextCols[annState.qualityQuestionIndex];
  if(!result||!editable()||annState.qualityReviewSaving||!task.annotation.complete||annQualityQuestionLabel(result,`col_${column}`)==='待补齐')return;
  annState.qualityReviewSaving=true;annRenderQualityProfile(annFilteredQualityResults());
  try {
    task=await api(`/api/group-tasks/${task.id}/feedback/${result.id}/quality`,{method:'PUT',body:{expected_version:task.version,column_index:column,label,note:'逐题人工复核'}});
    annState.qualityReviewSaving=false;dirty=false;syncAnnotation();annElement('ann-done-text').innerHTML=annBuildDoneSummary();annRenderQualityPreview();
    showToast('人工标签已保存','success');
  }catch(e){annState.qualityReviewSaving=false;if(!e.scopeChanged){annRenderQualityPreview();error(e);}}
}
function bindAIReview(){
annElement('ann-ai-candidate-list').addEventListener('click', event => {
  const card = event.target.closest('[data-ann-ai-player-index]');
  if (card) annGoToAiPlayer(Number(card.dataset.annAiPlayerIndex));
});

annElement('ann-ai-question-dots').addEventListener('click', event => {
  const button = event.target.closest('[data-ann-ai-question-index]');
  if (!button) return;
  annState.aiQuestionIndex = Number(button.dataset.annAiQuestionIndex);
  const result = annCurrentAiReviewResult();
  if (result) annRenderAiQuestion(result);
});

annElement('ann-ai-prev-player').addEventListener('click', () => annGoToAiPlayer(annState.aiReviewIndex - 1));
annElement('ann-ai-next-player').addEventListener('click', () => annGoToAiPlayer(annState.aiReviewIndex + 1));
annElement('ann-ai-prev-question').addEventListener('click', () => {
  annState.aiQuestionIndex -= 1;
  const result = annCurrentAiReviewResult();
  if (result) annRenderAiQuestion(result);
});
annElement('ann-ai-next-question').addEventListener('click', () => {
  annState.aiQuestionIndex += 1;
  const result = annCurrentAiReviewResult();
  if (result) annRenderAiQuestion(result);
});
annElement('ann-ai-mark-normal').addEventListener('click', () => annSetAiDecision('normal'));
annElement('ann-ai-mark-ai').addEventListener('click', () => annSetAiDecision('ai'));

bindAnn('ann-btn-confirm-ai',finishAIReview);
}
function bindQualityReview(){
annElement('ann-quality-player-list').addEventListener('click', event => {
  const card = event.target.closest('[data-ann-quality-player-id]');
  if (card) annGoToQualityPlayer(card.dataset.annQualityPlayerId);
});

annElement('ann-quality-profile-pane').addEventListener('click', event => {
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
  annElement(filterId).addEventListener('change', () => {
    annState.qualityReviewPlayerId = null;
    annState.qualityQuestionIndex = 0;
    annRenderQualityPreview();
  });
}





}

  function showRuleSaveNotice(message, failed=false) {
    ruleSaveNotice={taskId:task.id,message,failed};
    const notice=$('gw-rule-save-status');
    if(notice){notice.textContent=message;notice.hidden=false;notice.classList.toggle('workflow-error',failed);}
  }
  async function saveRuleForm(path, body) {
    if(ruleSave)return;
    const pending={taskId:task.id,epoch:scopeEpoch},url=`/api/group-tasks/${task.id}/${path}`;
    const controls=[...root.querySelectorAll('input,textarea,select,button')].map(el=>[el,el.disabled]);
    const payload={expected_version:task.version,...body};
    ruleSave=pending;
    controls.forEach(([el])=>el.disabled=true);
    showRuleSaveNotice('正在保存任务信息，请等待保存结束 · '+workflowTime(new Date().toISOString()));
    try {
      const value=await api(url,{method:'PUT',body:payload});
      if(ruleSave!==pending||task?.id!==pending.taskId)return;
      task=value.task||value;aiChoices=null;dirty=false;changedFields.clear();ruleSave=null;
      if(path==='rules')step=Math.min(2,maxStep());
      const message=path==='settings'?'任务设置已保存，标注结果保持不变':maxStep()<2?'规则已保存，请补齐任务名称、日期、奖励和次数要求':'任务规则已保存';
      ruleSaveNotice={taskId:task.id,message:message+' · '+workflowTime(new Date().toISOString()),failed:false};
      render();showToast(ruleSaveNotice.message,'success');
    } catch(e) {
      if(!e.scopeChanged&&scopeEpoch===pending.epoch&&task?.id===pending.taskId){
        const recovery=e.status===409?'填写内容已保留，请先核对最新任务后再保存':'填写内容已保留，请核对后重试';
        const message='未能确认保存成功：'+e.message+(e.status?'（HTTP '+e.status+'）':'')+'；请求：'+url+'；'+recovery+' · '+workflowTime(new Date().toISOString());
        showRuleSaveNotice(message,true);showToast(message,'error',6500);
      }
    } finally {
      if(ruleSave===pending)ruleSave=null;
      controls.forEach(([el,wasDisabled])=>{if(el.isConnected)el.disabled=wasDisabled;});
    }
  }
  async function mutate(path,body,method='POST') { const value=await api(`/api/group-tasks/${task.id}/${path}`,{method,body:{expected_version:task.version,...body}}); task=value.task||value; aiChoices=null; dirty=false;changedFields.clear();render();return value; }
  async function action(button) {
    const name=button.dataset.gw;
    if(name==='pause'||name==='resume'){
      const id=button.dataset.id,current=task?.id===id?task:latestList.find(t=>t.id===id);
      if(!current?.can_edit)throw new Error('当前仅可查看任务，不能暂停或继续');
      const value=await api(`/api/group-tasks/${id}/${name}`,{method:'POST',body:{expected_version:Number(button.dataset.version),run_id:button.dataset.runId}});
      if(task?.id===id){task=value;render();}else if(!task)await list();
      showToast(value.status==='pausing'?'暂停申请已接受，当前反馈保存后暂停；期间仍可能调用模型。':value.status==='paused'?'任务已暂停，已有结果保留。':'任务已继续，正在排队处理剩余反馈。','success');return;
    }
    if(name==='settings'){
      if(!await mayLeave())return;await fetchGroups();task=await api(`/api/group-tasks/${button.dataset.id}`);settingsView=true;render();return;
    }
    if(name==='save-settings'){
      const body={};root.querySelectorAll('[data-rule]').forEach(el=>{body[el.dataset.rule]=['base_reward','excellent_reward'].includes(el.dataset.rule)?el.value===''?null:Number(el.value):el.value;});
      body.group_source_ids=selectedGroupIds();
      if(!body.name.trim()||!body.payout_date||body.base_reward===null||body.excellent_reward===null)throw new Error('请补齐任务名称、发奖日期和两项奖励金额（允许 0）');
      await saveRuleForm('settings',body);return;
    }
    if(name==='delete-task'){
      if(!deletable())throw new Error('当前任务不能删除，请确认权限，并等待任务已暂停或处理结束。');
      if(!await window.workflowConfirm(`确认删除“${task.rules.name||'未命名任务'}”及其全部标注记录？删除后将不能再用于新的发奖批次；已生成的历史批次保留。`,'删除任务'))return;
      await api(`/api/group-tasks/${task.id}`,{method:'DELETE',body:{expected_version:task.version}});dirty=false;await list();showToast('任务已删除','success');return;
    }
    if(name==='annotation-run'){await startAnnotationStage();return;}
    if(name==='result-tab'){
      if(button.dataset.tab==='records'&&!recordsAvailable(task))throw new Error('完成质量标注后可查看任务记录');
      if(await mayLeave()){resultTab=button.dataset.tab;step=4;render();root.querySelector('#gw-tab-'+resultTab)?.focus();}return;
    }
    if(['copy','record-roster','record-confirm','record-identity','record-duplicate','record-adv','record-exclude','record-restore-reward'].includes(name)){await recordAction(name,button);return;}
    if(name==='annotation-results'){resultTab='quality';step=4;render();return;}
    if(name==='upload-view'||name==='columns'){uploadView=name==='upload-view';step=2;render();return;}

    if(name==='quality-filter'){if(!await mayLeave())return;qualityFilter=button.dataset.value;renderJudgments();return;}
    if(name==='record-response'){
      if(!await mayLeave())return;
      const row=task.feedback.find(item=>item.id===button.dataset.id);
      if(!row)return;
      if(row.confirmed_ai||!row.quality_complete){
        const line=button.closest('.gw-record-response');
        if(!line.nextElementSibling?.classList.contains('gw-record-evidence'))line.insertAdjacentHTML('afterend','<div class="gw-record-evidence">'+feedbackText(row)+'<p>'+h(row.outcome?.reason||'未进入质量打标，请结合原文和任务规则核对')+'</p></div>');
        return;
      }
      selectedResponse=row.id;qualityFilter='';resultTab='quality';step=4;render();
      ['ann-quality-overall-filter','ann-quality-question-filter','ann-quality-adjustment-filter'].forEach(id=>annElement(id).value='all');
      annGoToQualityPlayer(selectedResponse);return;
    }
    if(name==='response'){if(step!==3&&!await mayLeave())return;selectedResponse=button.dataset.id;render();return;}
    if(name==='rewards'){if(await mayLeave())switchMode('reward');return;}
    if(name==='new'){if(!await mayLeave())return;settingsView=false;await fetchGroups();task=await api('/api/group-tasks',{method:'POST',body:{}});uploadView=false;step=1;render();return;}
    if(name==='back'||name==='list'){if(await mayLeave())await list();return;}
    if(name==='view'||name==='records'){if(await mayLeave()){settingsView=false;resultTab=name==='records'?'records':'quality';recordNotice='';await fetchGroups();task=await api(`/api/group-tasks/${button.dataset.id}`);selectedResponse='';groupFilter='';qualityFilter='';uploadView=false;if(resultTab==='records'&&!recordsAvailable(task)){resultTab='quality';throw new Error('完成质量标注后可查看任务记录');}step=task.annotation?.complete?4:maxStep();render();}return;}
    if(name==='step'){if(+button.dataset.step>maxStep())throw new Error('请先完成前面的步骤');if(await mayLeave()){step=+button.dataset.step;render();root.scrollIntoView({block:'start'});}return;}
    if(name==='reload'){if(await mayLeave())await load(task.id);return;}
    if(name==='save-rules') {
      const rules={...task.rules};root.querySelectorAll('[data-rule]').forEach(el=>{const key=el.dataset.rule;rules[key]=['requires_count','allow_repeat','ai_detect'].includes(key)?el.value==='true':['base_reward','excellent_reward','min_count'].includes(key)?el.value===''?null:Number(el.value):key==='payout_date'?el.value||null:el.value;});
      rules.group_source_ids=selectedGroupIds();
      await saveRuleForm('rules',{rules});return;
    }
    if(name==='extract') {
      const taskId=task.id,status=$('gw-extraction-status');status.textContent='识别中…';status.dataset.state='running';
      try {
      const result=await api('/api/group-tasks/announcement',{method:'POST',body:{announcement:root.querySelector('[data-rule="announcement"]').value}});
      if(task?.id!==taskId||!status.isConnected)return;
      if(ruleSave){status.dataset.state='success';status.textContent='正在保存，本次识别建议未应用；保存后可重新识别';return;}
      const hasSavedRules=task.version>1||task.rules_initialized||Boolean(task.rules.name?.trim());
      let conflicts=0;
      Object.entries(result.suggestions).forEach(([key,item])=>{
        const field=root.querySelector(`[data-rule="${key}"]`),hint=root.querySelector(`[data-suggestion="${key}"]`); if(!field)return;
        const saved=task.rules[key],protectSaved=hasSavedRules&&saved!==null&&saved!==undefined&&String(saved).trim()!=='';
        if((changedFields.has(key)||protectSaved)&&String(item.value)!==field.value){conflicts++;if(hint){hint.textContent=`原值已保留；新识别建议：${item.value}。`;const adopt=document.createElement('button');adopt.type='button';adopt.className='btn btn--ghost btn--sm';adopt.textContent='采用识别值';adopt.onclick=()=>{if(ruleSave)return;field.value=String(item.value);dirty=true;changedFields.add(key);updateFieldState(field);hint.textContent='已采用识别值，请保存规则';root.querySelector('[data-rule="requires_count"]').onchange();};hint.append(adopt);}}
        else {field.value=String(item.value); if(hint)hint.textContent=`识别依据：${item.evidence}`;dirty=true;}
      });
      root.querySelector('[data-rule="requires_count"]').onchange();
      root.querySelectorAll('[data-rule]').forEach(updateFieldState);
      const missing=[...root.querySelectorAll('input[required]')].filter(el=>!el.closest('label').hidden&&el.value.trim()==='');
      status.dataset.state='success';status.textContent=(conflicts?`识别完成，${conflicts} 项建议与现有规则不同，原值已保留；采用后请保存。`:'识别成功，请核对下方规则。')+(missing.length?'请补齐标出的必填项。':'')+' · '+workflowTime(new Date().toISOString());
      }catch(e){if(status.isConnected){status.dataset.state='error';status.textContent='识别失败：'+e.message+'；可重试或手动填写';}throw e;}return;
    }
    if(name==='upload') {const file=$('gw-upload').files[0];if(!file)throw new Error('请先选择反馈文件');if(task.feedback.length)throw new Error('该任务已有原始反馈，反馈变动请新建任务');const form=new FormData();form.append('file',file);form.append('expected_version',String(task.version));task=await api(`/api/group-tasks/${task.id}/upload`,{method:'POST',body:form});dirty=false;render();return;}
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
    if(name==='run'||name==='retry'){const stage=task.annotation.phase==='ai_progress'?'ai':'quality';await mutate('run',{stage,...(name==='retry'?{response_ids:[button.dataset.id]}:{})});render();return;}
    if(name==='override'||name==='restore'){const panel=button.closest('details');await mutate(`feedback/${button.dataset.id}/quality`,{label:panel.querySelector('[data-review="label"]').value,note:panel.querySelector('[data-review="note"]').value,restore:name==='restore'},'PUT');return;}
    if(name==='confirm-ai'||name==='confirm-current-ai'){const ids=aiChoices||new Set(task.feedback.filter(row=>!row.excluded&&row.confirmed_ai).map(row=>row.id));if(name==='confirm-current-ai'){root.querySelector('[data-ai-current]').checked?ids.add(button.dataset.id):ids.delete(button.dataset.id);}await mutate('confirm-ai',{response_ids:[...ids]});step=Math.min(4,maxStep());render();return;}
    if(name==='confirm'){await mutate('confirm',{});step=6;render();showToast('当前任务版本已确认，可到奖励整理选择','success');return;}
    if(name==='adv'){const panel=button.closest('details');await mutate('player-adv',{player_id:button.dataset.player,adv:panel.querySelector('[data-player-adv]').value,note:panel.querySelector('[data-player-note]').value},'PUT');return;}

  }
  root.addEventListener('input', e=>{
    const el=e.target;
    if(!task||!el.matches('input,textarea,select')||el.matches('[data-filter],#gw-show-all,[id^=ga-quality-]'))return;
    dirty=true;if(el.dataset.rule){changedFields.add(el.dataset.rule);updateFieldState(el);}
    if(el.matches('[data-task-group]'))updateGroupSummary(el);
  });
  window.addEventListener('click',event=>{
    root.querySelectorAll('.gw-task-group-select[open]').forEach(control=>{if(!control.contains(event.target))control.open=false;});
  });
  root.addEventListener('keydown',event=>{
    const tab=event.target.closest('[data-gw="result-tab"]');
    if(tab&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){
      event.preventDefault();
      const tabs=[...root.querySelectorAll('[data-gw="result-tab"]:not(:disabled)')];
      const index=tabs.indexOf(tab),next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowLeft'?-1:1)+tabs.length)%tabs.length;
      tabs[next]?.focus();tabs[next]?.click();return;
    }
    const control=event.target.closest('.gw-task-group-select');
    if(event.key==='Escape'&&control?.open){control.open=false;control.querySelector('summary').focus();event.preventDefault();}
  });
  root.addEventListener('click',async e=>{
    if(e.target.closest('[data-group-summary]')&&!editable()){e.preventDefault();return;}
    const date=e.target;
    if(date.matches?.('input[type="date"][data-rule="payout_date"]')){
      if(!date.disabled&&!date.readOnly&&typeof date.showPicker==='function'){
        try{date.showPicker();}catch{} // Keep native date entry available if the browser cannot open its picker.
      }
      return;
    }
    const button=e.target.closest('[data-gw]');if(!button||button.disabled||ruleSave)return;button.disabled=true;requestBusy++;try{await action(button);}catch(err){error(err);}finally{requestBusy--;if(button.isConnected)button.disabled=false;}
  });
  setInterval(async()=>{
    if(pollBusy||requestBusy||root.classList.contains('panel--hidden')||document.hidden)return;
    pollBusy=true;
    try {
      if(!task){const data=await api('/api/group-tasks');if(!task&&$('gw-task-list'))renderList(data.tasks);}
      else {const before=task,id=task.id,latest=await api(`/api/group-tasks/${id}`);if(task!==before||requestBusy)return;
        $('gw-progress').textContent=progress(latest);$('gw-task-meta').textContent=`${latest.rules.group_name?latest.rules.group_name+' · ':''}${latest.owner_name} · ${statusNames[latest.status]}`;
        $('gw-progress-track').innerHTML=progressBar(latest);
        if(latest.updated_at!==task.updated_at||latest.status!==task.status){
          if(!dirty){
            const wasProgress=['ai_progress','quality_progress'].includes(task.annotation?.phase)&&step>=3;
            task=latest;if(wasProgress&&!settingsView)step=['ai_progress','ai_review'].includes(task.annotation.phase)?3:4;render();
          }else if(latest.version!==task.version)$('gw-update-hint').hidden=false;
        }
      }
    }catch{}finally{pollBusy=false;}
  },3000);
  window.addEventListener('group-management-scope-changed', () => {
    scopeEpoch++; task=null; dirty=false; settingsView=false; resultTab='quality';recordNotice=''; aiChoices=null; clipboardPreview=null;ruleSave=null;ruleSaveNotice=null;
    changedFields.clear(); latestList=[]; step=1;
    Object.assign(annState, {headers:[], headersZh:[], openTextCols:[], aiResults:[], reviewAiResults:[],
      qualityResults:[], qualityCount:0, aiReviewedIds:new Set(), confirmedAiIds:new Set(),
      qualityReviewPlayerId:null, qualityReviewSaving:false, aiReviewSaving:false});
    root.innerHTML='';
    if (!root.classList.contains('panel--hidden')) list().catch(error);
  });
  return {open,list};
})();
