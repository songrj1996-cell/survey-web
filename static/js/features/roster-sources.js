window.RosterSources = (() => {
  const root=$('roster-sources-panel'), api=window.workflowRequest, h=value=>esc(String(value??''));
  let sources=[], editing=null, metadata=[], headers=[], dirty=false;
  window.rosterHasDraft=()=>dirty;
  window.rosterMayLeave=async()=>{if(!dirty)return true;if(!await window.workflowConfirm('名单来源有未保存的修改，放弃后才会离开。'))return false;dirty=false;editing=null;return true;};
  window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
  const button=(action,label,primary=false)=>`<button type="button" class="btn ${primary?'btn--primary':'btn--ghost'}" data-rs="${action}" ${action==='new'&&editing?'disabled':''}>${label}</button>`;
  async function open({home=false}={}){
    if(home){if(!await window.rosterMayLeave())return;editing=null;metadata=[];headers=[];}
    root.classList.remove('panel--hidden');try{sources=(await api('/api/roster-sources')).sources;render();}catch(e){showToast(e.message,'error');}
  }
  function render(){
    root.innerHTML=`<div class="workflow-heading"><div><h1 class="panel__title">名单来源</h1><p class="panel__desc">配置群、工作表和各自的列结构。每次奖励预览与生成批次前，都会重新读取全部启用来源。</p></div>${PlatformPermissions.canUse('roster')?button('new','新增来源',true):''}</div>
      <div id="rs-editor"></div><div class="workflow-source-grid">${sources.map(source=>`<article class="workflow-card"><div class="workflow-heading"><h2>${h(source.name)}</h2><span class="workflow-badge">${source.enabled?'已启用':'已停用'}</span></div><p>${h(source.sheet_title)} · 表头第 ${source.header_row} 行</p><p class="workflow-muted">playerid 第 ${source.player_col+1} 列 / ori 第 ${source.ori_col+1} 列 / 状态第 ${source.status_col+1} 列</p><p>最近读取：${source.last_read_at?h(workflowTime(source.last_read_at)):'尚未读取'} · ${h(source.last_read_status||'')}</p>${source.last_read_error?`<p class="workflow-error">${h(source.last_read_error)}</p>`:''}<p class="workflow-muted">${source.row_count??0} 个名单条目 · ${source.auth_mode==='user'?'操作者飞书身份':'应用身份'}</p><a href="${h(source.spreadsheet_url.startsWith('https://')?source.spreadsheet_url:'#')}" target="_blank" rel="noopener noreferrer">打开来源表格</a> ${PlatformPermissions.canUse('roster')?`<button class="btn btn--ghost" data-rs="edit" data-id="${source.id}" ${editing?'disabled':''}>编辑来源</button>`:''}</article>`).join('')||'<div class="workflow-empty">还没有名单来源。配置后，成员不需要为每个任务重复上传人员名单。</div>'}</div>`;
    root.insertAdjacentHTML('beforeend',`<section class="workflow-card"><div class="workflow-heading"><div><h2>名单预览</h2><p class="workflow-muted">按保存的列映射读取示例行；实际标注与发奖读取完整名单，不受飞书页面筛选影响。</p></div></div><div class="workflow-toolbar"><label>名单来源 <select id="rs-preview-source" aria-label="预览名单来源">${sources.map(source=>`<option value="${source.id}">${h(source.name)}</option>`).join('')}</select></label>${sources.length?button('preview','读取最新名单预览'):''}</div><div id="rs-preview-content" class="workflow-muted">${sources.length?'选择来源后读取预览。':'请先配置名单来源。'}</div></section>`);
    if(editing)renderEditor();
  }
  function options(value,optional=false){return `${optional?'<option value="">不映射</option>':'<option value="">请选择列</option>'}`+headers.map((title,i)=>`<option value="${i}" ${value===i?'selected':''}>第 ${i+1} 列 · ${h(title||'无标题')}</option>`).join('');}
  function renderEditor(){
    const s=editing;
    $('rs-editor').innerHTML=`<section class="workflow-card"><h2>${s.id?'编辑名单来源':'新增名单来源'}</h2><div class="workflow-form-grid">
      <label class="workflow-field">群名称<input id="rs-name" value="${h(s.name)}" required></label><label class="workflow-field">读取身份<select id="rs-auth"><option value="user" ${s.auth_mode!=='application'?'selected':''}>当前操作者的飞书账号</option><option value="application" ${s.auth_mode==='application'?'selected':''}>平台应用（需授予表格权限）</option></select></label>
      <label class="workflow-field workflow-wide">飞书电子表格或知识库链接<input id="rs-url" value="${h(s.spreadsheet_url)}" placeholder="https://…/wiki/… 或 https://…/sheets/…"></label></div>
      ${button('inspect','读取工作表列表')}<div class="workflow-form-grid"><label class="workflow-field">工作表<select id="rs-sheet"><option value="">请选择</option>${metadata.map(sheet=>`<option value="${h(sheet.sheet_id)}" ${sheet.sheet_id===s.sheet_id?'selected':''}>${h(sheet.title)}</option>`).join('')}</select></label><label class="workflow-field">表头行号<input id="rs-header" type="number" min="1" value="${s.header_row||1}"></label></div>${button('headers','读取表头和示例')}
      <div class="workflow-form-grid"><label class="workflow-field">playerid 列<select id="rs-player">${options(s.player_col)}</select></label><label class="workflow-field">ori 列<select id="rs-ori">${options(s.ori_col)}</select></label><label class="workflow-field">群内状态列<select id="rs-status">${options(s.status_col)}</select></label><label class="workflow-field">昵称列（可选）<select id="rs-nickname">${options(s.nickname_col,true)}</select></label></div>
      <div class="workflow-form-grid"><label class="workflow-field">在群状态（逗号分隔）<input id="rs-eligible" value="${h((s.eligible_statuses||['群内人员']).join('，'))}"></label><label class="workflow-field">不在群状态（逗号分隔）<input id="rs-ineligible" value="${h((s.ineligible_statuses||['已退群','退群','不在群内']).join('，'))}"></label></div>
      <details ${s.formula?.mode==='synced_sheet'?'open':''}><summary>任务记录复制公式</summary><label class="workflow-field">记录目标<select id="rs-formula-mode"><option value="same_workbook" ${s.formula?.mode!=='synced_sheet'?'selected':''}>记录与名单在同一工作簿（自动使用上方映射）</option><option value="synced_sheet" ${s.formula?.mode==='synced_sheet'?'selected':''}>另一工作簿中已有同步名单</option></select></label><p class="workflow-muted">跨工作簿请先在记录目标建立同步名单。平台会核对同步内容，未同步或无法读取时不生成公式。</p>
      <div class="workflow-form-grid"><label class="workflow-field">同步目标表格链接<input id="rs-target" value="${h(s.formula?.target_spreadsheet)}"></label><label class="workflow-field">同步名单 Sheet 名称<input id="rs-target-title" value="${h(s.formula?.sheet_title)}"></label>${[['player_col','player','playerid'],['ori_col','ori','ori'],['status_col','status','状态']].map(([key,id,label])=>`<label class="workflow-field">同步 ${label} 列号<input id="rs-f-${id}" type="number" min="1" value="${(s.formula?.[key]??0)+1}"></label>`).join('')}<label class="workflow-field">同步数据首行<input id="rs-first-row" type="number" min="2" value="${s.formula?.first_data_row||2}"></label></div><label class="workflow-field">同步方式说明<input id="rs-sync-note" value="${h(s.formula?.synchronization_note)}"></label></details>
      <label><input type="checkbox" id="rs-enabled" ${s.enabled!==false?'checked':''}> 启用此来源</label><div class="workflow-toolbar">${button('save','保存并验证来源',true)}${button('cancel','取消')}</div><div id="rs-sample" class="workflow-table-wrap"></div></section>`;
  }
  function collect(){
    const n=id=>$(id).value===''?null:Number($(id).value);
    return {...editing,name:$('rs-name').value,spreadsheet_url:$('rs-url').value,auth_mode:$('rs-auth').value,sheet_id:$('rs-sheet').value,header_row:n('rs-header'),
      player_col:n('rs-player'),ori_col:n('rs-ori'),status_col:n('rs-status'),nickname_col:n('rs-nickname'),
      eligible_statuses:$('rs-eligible').value.split(/[,，]/).map(t=>t.trim()).filter(Boolean),ineligible_statuses:$('rs-ineligible').value.split(/[,，]/).map(t=>t.trim()).filter(Boolean),enabled:$('rs-enabled').checked,
      formula:{mode:$('rs-formula-mode').value,target_spreadsheet:$('rs-target').value,sheet_title:$('rs-target-title').value,
        player_col:n('rs-f-player')-1,ori_col:n('rs-f-ori')-1,status_col:n('rs-f-status')-1,first_data_row:n('rs-first-row'),synchronization_note:$('rs-sync-note').value}};
  }
  root.addEventListener('click',async e=>{
    const b=e.target.closest('[data-rs]');if(!b||b.disabled)return;b.disabled=true;
    try{
      const action=b.dataset.rs;
      if(action==='preview'){
        const source=sources.find(item=>item.id===$('rs-preview-source').value);if(!source)return;
        const data=await api('/api/roster-sources/inspect',{method:'POST',body:{spreadsheet_url:source.spreadsheet_url,auth_mode:source.auth_mode,sheet_id:source.sheet_id,header_row:source.header_row}});
        $('rs-preview-content').innerHTML=`<p>${h(source.name)} · 读取时间：${h(workflowTime(new Date().toISOString()))} · 以下为示例行</p><div class="workflow-table-wrap"><table class="workflow-table"><thead><tr><th>playerid</th><th>昵称</th><th>ori</th><th>当前状态</th></tr></thead><tbody>${(data.sample||[]).map(row=>`<tr><td>${h(row[source.player_col])}</td><td>${h(source.nickname_col===null?'':row[source.nickname_col])}</td><td>${h(row[source.ori_col])}</td><td>${h(row[source.status_col])}</td></tr>`).join('')||'<tr><td colspan="4">当前没有名单行。</td></tr>'}</tbody></table></div>`;
      }
      if(action==='new'){dirty=false;editing={name:'',spreadsheet_url:'',auth_mode:'user',enabled:true};metadata=[];headers=[];render();}
      if(action==='edit'){editing=structuredClone(sources.find(s=>s.id===b.dataset.id));metadata=[{sheet_id:editing.sheet_id,title:editing.sheet_title}];headers=Array(Math.max(editing.player_col,editing.ori_col,editing.status_col,editing.nickname_col||0)+1).fill('');Object.entries(editing.expected_headers||{}).forEach(([i,title])=>headers[+i]=title);render();}
      if(action==='cancel'){if(!await window.rosterMayLeave())return;editing=null;dirty=false;render();}
      if(action==='inspect'||action==='headers'){
        editing=collect();const data=await api('/api/roster-sources/inspect',{method:'POST',body:{spreadsheet_url:editing.spreadsheet_url,auth_mode:editing.auth_mode,sheet_id:action==='headers'?editing.sheet_id:'',header_row:editing.header_row}});
        metadata=data.sheets;if(data.headers)headers=data.headers;renderEditor();
        if(data.sample)$('rs-sample').innerHTML=`<table class="workflow-table"><thead><tr>${headers.map(title=>`<th>${h(title)}</th>`).join('')}</tr></thead><tbody>${data.sample.map(row=>`<tr>${row.map(cell=>`<td>${h(cell)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
      }
      if(action==='save'){
        const s=collect(),keys=['name','spreadsheet_url','sheet_id','header_row','player_col','ori_col','status_col','nickname_col','eligible_statuses','ineligible_statuses','auth_mode','enabled','formula'];
        const body=Object.fromEntries(keys.map(key=>[key,s[key]]));body.expected_headers=Object.fromEntries([s.player_col,s.ori_col,s.status_col,s.nickname_col].filter(i=>i!==null).map(i=>[String(i),String(headers[i]??'')]));
        if(s.id)body.expected_revision=s.revision;
        await api('/api/roster-sources'+(s.id?'/'+s.id:''),{method:s.id?'PUT':'POST',body});editing=null;dirty=false;await open();showToast('名单来源已保存','success');
      }
    }catch(err){showToast(err.message,'error',6500);}finally{if(b.isConnected)b.disabled=false;}
  });
  root.addEventListener('input',event=>{if(editing&&event.target.closest('#rs-editor')){dirty=true;editing=collect();}});
  root.addEventListener('change',event=>{if(editing&&event.target.closest('#rs-editor')){dirty=true;editing=collect();}});
  return {open};
})();
