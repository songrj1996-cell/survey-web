window.RewardBatches = (() => {
  const root=$('reward-batches-panel'),api=window.workflowRequest,h=value=>esc(String(value??''));
  let data={tasks:[],other_rewards:[],imports:[],groups:[],batches:[]},dates=new Set(),selectedTasks=new Set(),selectedOthers=new Set(),selectedImports=new Set(),preview=null,batch=null,generationKey=null;
  const button=(action,label,primary=false,disabled=false,extra='')=>`<button type="button" class="btn ${primary?'btn--primary':'btn--ghost'}" data-rb="${action}" ${disabled?'disabled':''} ${extra}>${label}</button>`;
  async function open(){root.classList.remove('panel--hidden');try{data=await api('/api/reward-batches/choices');render();}catch(e){showToast(e.message,'error');}}
  function render(){
    root.innerHTML=`<div class="workflow-heading"><div><h1 class="panel__title">奖励整理</h1><p class="panel__desc">明确选择日期与来源，核对最新名单，再保存发奖 TXT 批次。</p></div>${button('refresh','刷新来源')}</div><div class="workflow-toolbar">${button('import','导入已有奖励 Excel',false,!PlatformPermissions.canUse('reward'))}${button('other','录入其他奖励',false,!PlatformPermissions.canUse('reward'))}${button('history','查看批次记录')}</div><div id="rb-other"></div><div id="rb-body"></div>`;
    if(batch){renderBatch();renderVersionDifferences();return;}if(preview){renderPreview();return;}renderChoices();renderHistory();
  }
  function renderHistory(){
    $('rb-body').insertAdjacentHTML('beforeend',`<section class="workflow-card" id="rb-history"><div class="workflow-heading"><h2>奖励批次</h2><span class="workflow-muted">已生成名单不代表已实际发放</span></div>${data.batches.slice().reverse().map(item=>`<div class="workflow-source-choice"><span>${h(item.dates.join('、'))} · ${item.total_diamonds} 钻<small>${h(item.owner_name)} · ${h(workflowTime(item.created_at))} · ${item.unique_accounts} 个账号</small></span>${button('batch','查看与下载',false,false,`data-id="${item.id}"`)}</div>`).join('')||'<p class="workflow-muted">尚未生成批次。</p>'}</section>`);
  }
  function availableDates(){
    return [...new Set([...data.tasks.map(t=>t.rules.payout_date),...data.other_rewards.map(r=>r.payout_date),...data.imports.flatMap(i=>i.analysis_request?.reward_dates||[])].filter(Boolean))].sort();
  }
  function dateBranchDates(input){
    const prefix=input.dataset.rbDateGroup==='all'?'':input.dataset.rbDatePrefix;
    return availableDates().filter(date=>date.startsWith(prefix));
  }
  function syncDates(){
    const tree=$('rb-date-tree');
    tree.querySelectorAll('[data-rb-date]').forEach(input=>{input.checked=dates.has(input.dataset.rbDate);});
    tree.querySelectorAll('[data-rb-date-group]').forEach(input=>{
      const items=dateBranchDates(input),count=items.filter(date=>dates.has(date)).length;
      input.checked=items.length>0&&count===items.length;
      input.indeterminate=count>0&&count<items.length;
    });
    $('rb-date-selected-count').textContent=dates.size?`已选 ${dates.size} 个日期`:'未选择';
    renderSources();
  }
  function renderChoices(){
    $('rb-body').innerHTML=`<section class="workflow-card"><div class="rr-config-module__head"><div><h2>1. 选择奖励发放日期</h2><p>可按年、月或具体日期多选，只纳入本次选中的日期。</p></div><span class="rr-selection-count" id="rb-date-selected-count">未选择</span></div><div class="rr-date-tree" id="rb-date-tree" aria-label="奖励发放日期"></div></section><div id="rb-sources"></div>`;
    const allDates=availableDates(),groups=new Map();
    allDates.forEach(date=>{
      const [year,month]=date.split('-');
      if(!groups.has(year))groups.set(year,new Map());
      const months=groups.get(year);
      if(!months.has(month))months.set(month,[]);
      months.get(month).push(date);
    });
    const tree=$('rb-date-tree');
    const branchLabel=(kind,prefix,label,count)=>`<label><input type="checkbox" data-rb-date-group="${kind}" data-rb-date-prefix="${h(prefix)}"><span>${h(label)}</span><em>${count} 个日期</em></label>`;
    tree.innerHTML=allDates.length?`<label class="rr-date-all"><input type="checkbox" data-rb-date-group="all"><span>全选日期</span><em>${allDates.length} 个日期</em></label>`+
      [...groups.entries()].sort(([a],[b])=>b.localeCompare(a)).map(([year,months])=>`<details class="rr-date-branch rr-date-year" open><summary>${branchLabel('year',year+'-',year+' 年',[...months.values()].flat().length)}</summary><div class="rr-date-months">${[...months.entries()].map(([month,items])=>`<details class="rr-date-branch rr-date-month" open><summary>${branchLabel('month',year+'-'+month+'-',Number(month)+' 月',items.length)}</summary><div class="rr-date-days">${items.map(date=>`<label class="rr-date-day"><input type="checkbox" data-rb-date="${h(date)}" aria-label="${h(date)}"><span>${Number(date.split('-')[2])} 日</span></label>`).join('')}</div></details>`).join('')}</div></details>`).join(''):
      '<div class="rr-empty">暂无可选日期。确认质量任务或录入其他奖励后即可选择。</div>';
    tree.querySelectorAll('summary input').forEach(input=>input.addEventListener('click',event=>event.stopPropagation()));
    tree.querySelectorAll('[data-rb-date]').forEach(input=>input.onchange=()=>{input.checked?dates.add(input.dataset.rbDate):dates.delete(input.dataset.rbDate);syncDates();});
    tree.querySelectorAll('[data-rb-date-group]').forEach(input=>input.onchange=()=>{dateBranchDates(input).forEach(date=>input.checked?dates.add(date):dates.delete(date));syncDates();});
    syncDates();
  }
  function renderSources(){
    $('rb-sources').innerHTML=`<section class="workflow-card"><h2>2. 选择奖励来源</h2><h3>质量标注任务</h3>${data.tasks.filter(t=>dates.has(t.rules.payout_date)).map(t=>`<label class="workflow-source-choice"><input type="checkbox" data-rb-task="${t.id}" ${selectedTasks.has(t.id)?'checked':''} ${!t.selectable?'disabled':''}><span><strong>${h(t.rules.name)}</strong><small>${t.rules.payout_date} · ${h(t.owner_name)} · 版本 ${t.version} · ${t.summary.base+t.summary.excellent} 钻${t.consumed?' · 已进入历史批次':!t.selectable?' · 当前不可结算':''}</small></span>${t.consumed?button('batch','查看原批次',false,false,`data-id="${t.consumed}"`):''}</label>`).join('')||'<p class="workflow-muted">所选日期没有任务。任务需由负责人确认完整结果后才可选。</p>'}
      <h3>其他奖励</h3>${data.other_rewards.filter(r=>dates.has(r.payout_date)).map(r=>`<label class="workflow-source-choice"><input type="checkbox" data-rb-other="${r.id}" ${selectedOthers.has(r.id)?'checked':''} ${!r.selectable?'disabled':''}><span>${h(r.player_id)} · ${r.amount} 钻<small>${h(r.reason)} · ${h(r.identity.group_name)} · ${r.payout_date}${r.consumed?' · 已进入批次':''}</small></span></label>`).join('')||'<p class="workflow-muted">所选日期没有其他奖励。</p>'}
      <h3>已有表格导入</h3>${data.imports.filter(i=>(i.analysis_request?.reward_dates||[]).some(date=>dates.has(date))).map(i=>`<label class="workflow-source-choice"><input type="checkbox" data-rb-import="${i.id}" ${selectedImports.has(i.id)?'checked':''}><span>${h(i.filename)}<small>${h(i.owner_name)} · 使用已确认字段映射，并重新核对飞书名单</small></span></label>`).join('')||'<p class="workflow-muted">先导入 Excel 并确认字段映射，再选择纳入本次整理。</p>'}</section>
      <div class="workflow-toolbar">${button('preview','读取最新名单并预览',true,!dates.size||!PlatformPermissions.canUse('reward'))}<span class="workflow-muted">预览不占用奖励来源，生成批次时才保存来源使用记录。</span></div>`;
    [['task',selectedTasks],['other',selectedOthers],['import',selectedImports]].forEach(([name,set])=>root.querySelectorAll(`[data-rb-${name}]`).forEach(input=>input.onchange=()=>{input.checked?set.add(input.dataset['rb'+name[0].toUpperCase()+name.slice(1)]):set.delete(input.dataset['rb'+name[0].toUpperCase()+name.slice(1)]);}));
  }
  function renderPreview(){
    const p=preview;
    $('rb-body').innerHTML=`<section class="workflow-card"><div class="workflow-heading"><div><h2>核对奖励预览</h2><p class="workflow-muted">名单读取：${h(workflowTime(p.roster_snapshot.read_at))} · 日期：${p.selection.dates.join('、')}</p></div>${button('choices','重新选择来源')}</div>
      <div class="workflow-summary"><span>可发放账号 <strong>${p.result.unique_accounts}</strong></span><span>当前汇总 <strong>${p.result.total_diamonds} 钻</strong></span><span>待处理身份 <strong>${p.identity_issues.length}</strong></span><span>待处理冲突 <strong>${p.unresolved_conflicts.length}</strong></span></div>
      ${p.generation_difference?`<div class="workflow-notice"><strong>生成前发现名单变化，请重新核对</strong><p>原预览 ${p.generation_difference.before_total} 钻 → 当前 ${p.generation_difference.after_total} 钻</p></div>`:''}
      ${p.roster_changes.length?`<details open><summary>账号或状态与原记录不同（${p.roster_changes.length}）</summary>${p.roster_changes.map(c=>`<p>${h(c.player_id)}：${h(c.before.ori||'无账号')} / ${h(c.before.status||'未知')} → ${h(c.after.ori)} / ${h(c.after.status)}</p>`).join('')}</details>`:''}
      ${p.identity_issues.map(issue=>`<div class="workflow-subcard"><strong>${h(issue.player_id)} · ${h(issue.task)}</strong><p class="workflow-error">${h(issue.message)}</p>${issue.candidates.length>1?`<select data-rb-identity="${h(issue.row_id)}"><option value="">请选择匹配身份</option>${issue.candidates.map(c=>`<option value="${c.key}">${h(c.group_name)} · ${h(c.ori)} · ${h(c.status)} · 第 ${c.row_number} 行</option>`).join('')}</select>`:'<p class="workflow-muted">请修正原始身份或由名单维护者更新飞书名单，再重新预览。</p>'}</div>`).join('')}
      ${p.conflicts.map(c=>`<fieldset class="workflow-conflict" data-conflict="${c.conflict_id}"><legend>${h(c.account)} · ${c.reasons.map(reason=>rrReasonLabel(reason)).join(' / ')}</legend><p class="workflow-muted">选择本次保留的奖励。同日同一任务只能保留一笔。</p>${c.rows.map(row=>`<label class="workflow-source-choice"><input type="checkbox" data-conflict-row="${h(row.row_id)}" ${(p.selections[c.conflict_id]||[]).includes(row.row_id)?'checked':''}><span>${h(row.task)} · ${row.total_reward} 钻<small>${row.date} · ${h(row.sheet)} · ${h(row.player_id)} · 基础 ${row.normal_reward} + 优秀 ${row.excellent_reward} + 其他 ${row.other_reward}</small></span></label>`).join('')}<label><input type="checkbox" data-conflict-reviewed ${Object.prototype.hasOwnProperty.call(p.selections,c.conflict_id)?'checked':''}> 我已核对该账号的保留范围</label></fieldset>`).join('')}
      ${p.identity_issues.length||p.conflicts.length?button('resolve','保存身份与冲突决定',true):''}
      <details ${p.excluded_rows.length?'open':''}><summary>本次不发放（${p.excluded_rows.length}）</summary>${p.excluded_rows.map(row=>`<p>${h(row.player_id)} · ${h(row.task)} · 原计算 ${row.total_reward} 钻 · ${h(row.exclusion_reason)}</p>`).join('')}</details>
      ${accountDetails(p.result)}<details><summary>TXT 预览 · zoneid / uid / 奖励</summary><p class="workflow-muted">${p.ready?'已核对的本次发放内容，生成时还会再次校验最新名单。':'仍有事项待核对，以下仅为当前已确定部分，不能作为完整发奖文件。'}</p><pre class="workflow-pre workflow-txt">${h(p.result.txt_preview||'暂无可导出的账号')}</pre></details>
      <div class="workflow-toolbar">${button('generate','核对最新名单并生成 TXT 批次',true,!p.ready)}<span class="workflow-muted">生成文件不代表已向游戏账号发奖。</span></div></section>`;
  }
  function accountDetails(result){
    return `<div class="workflow-table-wrap"><table class="workflow-table workflow-account-table"><thead><tr><th>ori</th><th>奖励来源</th><th>合计钻石</th></tr></thead><tbody>${result.accounts.map(account=>`<tr><td>${h(account.account)}</td><td>${(account.sources||[]).map(row=>`<p>${h(row.task)} · ${h(row.sheet)} · ${h(row.player_id)}<small>${h(row.date)} · 基础 ${row.normal_reward} + 优秀追加 ${row.excellent_reward} + 其他 ${row.other_reward} = ${row.total_reward} 钻</small></p>`).join('')}</td><td>${account.total_diamonds}</td></tr>`).join('')||'<tr><td colspan="3">当前没有可发放账号。</td></tr>'}</tbody></table></div>`;
  }
  function renderBatch(){
    $('rb-body').innerHTML=`<section class="workflow-card"><div class="workflow-heading"><div><h2>已保存发奖批次</h2><p class="workflow-muted">${h(workflowTime(batch.created_at))} · ${h(batch.owner_name)} · ${batch.dates.join('、')}</p></div>${button('choices','返回来源选择')}</div><div class="workflow-summary"><span>账号 <strong>${batch.unique_accounts}</strong></span><span>总额 <strong>${batch.total_diamonds} 钻</strong></span></div><p>此批次的来源、金额、名单状态和 TXT 已固定保存。再次下载不会创建新批次。</p><a class="btn btn--primary" href="/api/reward-batches/${batch.id}/download">下载 TXT</a> ${button('copy-txt','复制 TXT 内容')}<details open><summary>已保存 TXT 内容</summary><pre class="workflow-pre workflow-txt">${h(batch.txt_preview)}</pre></details>${accountDetails(batch)}<details><summary>来源版本与金额</summary>${Object.entries(batch.task_versions||{}).map(([id,entry])=>`<p>${h(entry.rules.name)} · 已发奖来源版本 ${entry.version}${data.tasks.find(t=>t.id===id)?` · 任务当前版本 ${data.tasks.find(t=>t.id===id).version}`:''}</p>`).join('')}<pre class="workflow-pre">${h(batch.note_full)}</pre></details></section>`;
  }
  function renderVersionDifferences(){
    const differences=batch?.current_source_differences||[];
    if(!differences.length)return;
    const labels={name:'任务名称',payout_date:'发奖日期',base_reward:'基础奖励',excellent_reward:'优秀追加',requires_count:'次数门槛',min_count:'最低次数',allow_repeat:'重复提交',announcement:'公告',ai_detect:'AI 作答识别'};
    const value=v=>v===null?'未填写':v===true?'开启':v===false?'关闭':String(v);
    $('rb-body').insertAdjacentHTML('beforeend',`<section class="workflow-card"><h2>来源后续修改</h2><p class="workflow-notice">以下任务在生成批次后有改动。历史 TXT 保持原样，这些来源不能再次全额发放；差额或冲正需要另行核对处理。</p>${differences.map(d=>`<div class="workflow-subcard"><strong>${h(d.task_name)} · V${d.old_version} → V${d.current_version}</strong><p>任务计算总额：${d.old_total} → ${d.current_total??'待补齐'} 钻 · 玩家：${d.old_player_count} → ${d.current_player_count}</p>${Object.entries(d.rule_changes).map(([key,change])=>`<p>${h(labels[key]||key)}：${h(value(change.before))} → ${h(value(change.after))}</p>`).join('')}<p class="workflow-muted">任务计算总额包含当时因群内状态等原因未发放的部分，不等于应补发金额。</p></div>`).join('')}</section>`);
  }
  function otherForm(){
    $('rb-other').innerHTML=`<dialog id="rb-other-dialog" class="workflow-dialog" aria-label="录入独立其他奖励"><h2>录入独立其他奖励</h2><p class="workflow-muted">用于抽奖、小活动等，不改变反馈质量或任务基础奖励资格。</p><div class="workflow-form-grid"><label class="workflow-field">奖励发放日期<input id="rb-other-date" type="date"></label><label class="workflow-field">所属群<select id="rb-other-group"><option value="">请选择</option>${data.groups.map(g=>`<option value="${g.id}">${h(g.name)}</option>`).join('')}</select></label><label class="workflow-field">playerid<input id="rb-other-player"></label><label class="workflow-field">奖励金额（正整数钻石）<input id="rb-other-amount" type="number" min="1" step="1"></label></div><label class="workflow-field">奖励原因<textarea id="rb-other-reason"></textarea></label><div id="rb-other-identity"></div>${button('save-other','保存其他奖励',true)} ${button('cancel-other','取消')}</dialog>`;
    $('rb-other-dialog').showModal();
  }
  async function acceptImport(identifier,explicitDates){
    dates=new Set(explicitDates);selectedImports.add(identifier);preview=null;batch=null;
    [1,2,3,4].forEach(i=>$(`rr-panel-${i}`).classList.add('panel--hidden'));await open();
    showToast('已保留导入映射和所选日期，请核对奖励来源后预览最新名单','success');
  }
  root.addEventListener('click',async e=>{
    const b=e.target.closest('[data-rb]');if(!b||b.disabled)return;b.disabled=true;
    try{
      const action=b.dataset.rb;
      if(action==='copy-txt'){await navigator.clipboard.writeText(batch.txt_preview);showToast('已复制本批次 TXT 内容','success');}
      if(action==='refresh')await open();
      if(action==='choices'){preview=null;batch=null;generationKey=null;await open();}
      if(action==='import'){root.classList.add('panel--hidden');rrGoStep(rrState.sessionId?2:1);}
      if(action==='other')otherForm();
      if(action==='cancel-other')$('rb-other').innerHTML='';
      if(action==='save-other'){
        await api('/api/reward-batches/other-rewards',{method:'POST',body:{payout_date:$('rb-other-date').value,source_id:$('rb-other-group').value,player_id:$('rb-other-player').value,amount:Number($('rb-other-amount').value),reason:$('rb-other-reason').value,candidate_key:$('rb-other-candidate')?.value||''}});
        showToast('其他奖励已保存','success');await open();
      }
      if(action==='preview'){
        const selection={dates:[...dates].sort(),task_versions:Object.fromEntries(data.tasks.filter(t=>t.selectable&&selectedTasks.has(t.id)&&dates.has(t.rules.payout_date)).map(t=>[t.id,t.version])),other_reward_ids:data.other_rewards.filter(r=>r.selectable&&selectedOthers.has(r.id)&&dates.has(r.payout_date)).map(r=>r.id),imported_session_ids:data.imports.filter(i=>selectedImports.has(i.id)&&(i.analysis_request?.reward_dates||[]).some(date=>dates.has(date))).map(i=>i.id)};
        preview=await api('/api/reward-batches/previews',{method:'POST',body:selection});generationKey=crypto.randomUUID();batch=null;render();
      }
      if(action==='resolve'){
        const selections={},identity_choices={...preview.identity_choices};root.querySelectorAll('[data-rb-identity]').forEach(el=>{if(el.value)identity_choices[el.dataset.rbIdentity]=el.value;});
        root.querySelectorAll('[data-conflict]').forEach(group=>{if(group.querySelector('[data-conflict-reviewed]').checked)selections[group.dataset.conflict]=[...group.querySelectorAll('[data-conflict-row]:checked')].map(el=>el.dataset.conflictRow);});
        preview=await api(`/api/reward-batches/previews/${preview.id}`,{method:'PUT',body:{expected_revision:preview.revision,selections,identity_choices}});generationKey=crypto.randomUUID();render();
      }
      if(action==='generate'){generationKey ||= crypto.randomUUID();batch=await api(`/api/reward-batches/previews/${preview.id}/generate`,{method:'POST',body:{expected_revision:preview.revision,idempotency_key:generationKey}});preview=null;render();showToast('批次已保存，可下载 TXT','success');}
      if(action==='history'){$('rb-body').innerHTML=`<section class="workflow-card"><h2>发奖批次记录</h2>${data.batches.slice().reverse().map(item=>`<div class="workflow-source-choice"><span>${item.dates.join('、')} · ${item.total_diamonds} 钻<small>${h(item.owner_name)} · ${h(workflowTime(item.created_at))} · ${item.unique_accounts} 个账号</small></span>${button('batch','查看与下载',false,false,`data-id="${item.id}"`)}</div>`).join('')||'<p class="workflow-muted">尚未生成批次。</p>'}${button('choices','返回来源选择')}</section>`;}
      if(action==='batch'){batch=await api(`/api/reward-batches/${b.dataset.id}`);preview=null;render();}
    }catch(err){
      if(err.detail?.preview){preview=err.detail.preview;generationKey=crypto.randomUUID();render();}
      if(err.detail?.candidates?.length&&$('rb-other-identity'))$('rb-other-identity').innerHTML=`<label class="workflow-field">选择匹配身份<select id="rb-other-candidate"><option value="">请选择</option>${err.detail.candidates.map(c=>`<option value="${c.key}">${h(c.group_name)} · ${h(c.ori)} · 第 ${c.row_number} 行</option>`).join('')}</select></label>`;
      showToast(err.message,'error',7000);
    }finally{if(b.isConnected)b.disabled=false;}
  });
  return {open,acceptImport};
})();
