// Administrator configuration A/B. Publishing stores versions; business routing is not connected yet.
window.ModelSettings = (() => {
  const copy = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
  const equal = (a, b) => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
  const html = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const effortLabel = value => ({default:'模型默认',none:'不推理',minimal:'最低',low:'低',medium:'中',high:'高',xhigh:'极高',max:'最高'}[value] || value);
  let root, saved, draft, tab = 'tasks', business = '', task = '', editor = null;
  let busy = false, loading = null, review = null, restoredFrom = null, note = '', message = '', error = '', conflict = false, merges = [];
  let advanced = false;
  const dirty = () => !!saved && !equal(draft, saved.configuration);
  const hasDraft = () => dirty() || !!editor;
  const changedTasks = () => saved ? saved.tasks.filter(s => !equal(draft.tasks[s.key], saved.configuration.tasks[s.key])) : [];
  const model = id => draft.models.find(m => m.id === id);
  const current = () => draft.tasks[task];
  const button = (label, attrs = '', primary = false, disabled = false) => `<button type="button" class="btn ${primary?'btn--primary':'btn--ghost'}" ${attrs} ${disabled?'disabled':''}>${label}</button>`;
  function timeLabel(value) {
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai',dateStyle:'short',timeStyle:'medium'}).format(date) + ' +08:00' : '时间未记录';
  }
  async function request(path, body) {
    let response;
    try { response = await fetch('/api/model-settings' + path, {method:body ? 'POST' : 'GET', headers:body?{'Content-Type':'application/json'}:{}, body:body?JSON.stringify(body):undefined}); }
    catch (_) { throw Object.assign(new Error('未能连接配置服务，请检查网络后重试；草稿已保留。'), {status:0}); }
    let data;
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) {
      const detail = data.detail;
      const text = typeof detail === 'string' ? detail : detail?.message || (response.status === 422 ? '字段格式不正确，请检查模型标识、推理选项和数字范围。' : '配置操作未完成，请稍后重试。');
      throw Object.assign(new Error(text + `（HTTP ${response.status}）`), {status:response.status, problems:detail?.errors || []});
    }
    return data;
  }
  function accept(response) {
    saved = response; draft = copy(response.configuration);
    if (!saved.tasks.some(s => s.key === task)) task = saved.tasks[0]?.key || '';
    business = saved.tasks.find(s => s.key === task)?.business || '';
    review = null; editor = null; restoredFrom = null; conflict = false; merges = [];
  }
  async function load() {
    root = document.getElementById('model-settings-root');
    if (!root) return;
    if (saved && hasDraft()) { render(); return; }
    if (loading) return loading;
    root.innerHTML = '<p class="model-settings-loading" role="status">正在读取模型配置…</p>';
    loading = (async () => {
      try { accept(await request('')); error = ''; message = ''; render(); }
      catch (e) { error = e.message; root.innerHTML = `<p class="model-settings-error" role="alert">${html(error)}</p>${button('重新加载','data-ms="reload"')}`; }
      finally { loading = null; }
    })();
    return loading;
  }
  function startEdit() { review = null; restoredFrom = null; error = ''; message = ''; }
  const modelOptions = selected => (selected && !model(selected) ? `<option value="${html(selected)}">${html(selected)}（目录缺失）</option>` : '') + (!selected ? '<option value="">尚未配置</option>' : '') + draft.models.map(m => `<option value="${html(m.id)}" ${m.id===selected?'selected':''}>${html(m.id)}</option>`).join('');
  function candidateFields(candidate, index) {
    const m = model(candidate.model), supported = m?.efforts || ['default'];
    const values = supported.includes(candidate.reasoning) ? supported : [candidate.reasoning,...supported];
    const name = index < 0 ? '主模型' : `备用模型 ${index+1}`;
    return `<div class="model-settings-fields"><label>${name}<select aria-label="${name}" data-ms-candidate="${index}" data-field="model">${modelOptions(candidate.model)}</select></label><label>推理程度<select aria-label="${name}推理程度" data-ms-candidate="${index}" data-field="reasoning">${values.map(v=>`<option value="${html(v)}" ${v===candidate.reasoning?'selected':''}>${html(effortLabel(v))}${supported.includes(v)?'':'（目录未声明）'}</option>`).join('')}</select></label></div>`;
  }
  function renderTasks() {
    const groups = [...new Set(saved.tasks.map(s=>s.business))], selected = saved.tasks.find(s=>s.key===task), cfg = current();
    return `<div class="model-settings-business"><label for="ms-business">业务</label><select id="ms-business">${groups.map(g=>`<option ${g===business?'selected':''}>${html(g)}</option>`).join('')}</select><span>${saved.tasks.filter(s=>s.business===business).length} 个环节</span></div><div class="model-settings-workspace"><nav class="model-settings-stages" aria-label="业务环节">${saved.tasks.filter(s=>s.business===business).map(s=>`<button type="button" data-ms-task="${s.key}" aria-pressed="${s.key===task}"><strong>${html(s.label)}${equal(draft.tasks[s.key],saved.configuration.tasks[s.key])?'':' · 已修改'}</strong><small>${html(draft.tasks[s.key].primary.model || '尚未配置')}</small></button>`).join('')}</nav><section class="model-settings-editor"><h3>${html(selected.label)}</h3><p class="model-settings-muted">${html(selected.business)} · 仅调整此环节，其他环节独立配置</p>${candidateFields(cfg.primary,-1)}<details data-ms-advanced ${advanced?'open':''}><summary>高级设置：输出长度与请求限制</summary><div class="model-settings-fields"><label>主模型最大输出（tokens）<input aria-label="主模型最大输出" type="number" min="1024" step="1" value="${cfg.primary.max_tokens}" data-ms-candidate="-1" data-field="max_tokens"></label><label>单次最长等待（秒）<input aria-label="单次最长等待" type="number" min="1" step="1" placeholder="沿用原有规则" value="${cfg.timeout_seconds??''}" data-ms-route="timeout_seconds"></label><label>真实请求上限（包含重试和备用）<input aria-label="真实请求上限" type="number" min="1" max="100" step="1" placeholder="沿用原有规则" value="${cfg.max_http_attempts??''}" data-ms-route="max_http_attempts"></label></div></details><div class="model-settings-section"><div class="model-settings-title"><h4>备用模型</h4>${button('＋ 添加备用','data-ms="add-fallback"',false,cfg.fallbacks.length>=16)}</div><p class="model-settings-muted">按顺序尝试，每个模型使用自己的参数。发布后用于新任务；正在运行的任务沿用原配置。</p>${cfg.fallbacks.length?cfg.fallbacks.map((c,i)=>`<div class="model-settings-fallback"><div class="model-settings-title"><span>备用 ${i+1}</span><div class="model-settings-actions">${button('上移',`data-ms-move="${i}" data-dir="-1" aria-label="上移备用 ${i+1}"`,false,i===0)}${button('下移',`data-ms-move="${i}" data-dir="1" aria-label="下移备用 ${i+1}"`,false,i===cfg.fallbacks.length-1)}${button('移除',`data-ms-remove="${i}" aria-label="移除备用 ${i+1}"`)}</div></div>${candidateFields(c,i)}<label class="model-settings-cap">最大输出（tokens）<input aria-label="备用 ${i+1}最大输出" type="number" min="1024" step="1" value="${c.max_tokens}" data-ms-candidate="${i}" data-field="max_tokens"></label></div>`).join(''):'<p class="model-settings-muted">未设置备用，保留失败后停止的配置。</p>'}</div><p class="model-settings-notice">未核实的模型能力不会被当作已验证。修改其调用设置前，请先到模型目录填写实际限制及核实依据。</p></section></div>`;
  }
  function modelForm() {
    const m = editor;
    return `<form id="ms-model-form" class="model-settings-editor"><h3>${m.original?'编辑模型':'新增模型'}</h3><div class="model-settings-fields"><label>模型标识<input name="id" aria-label="模型标识" value="${html(m.id)}" maxlength="100" ${m.original?'readonly':''} required></label><label>显示名称<input name="name" aria-label="显示名称" value="${html(m.name)}" maxlength="100" required></label><label>上下文容量（tokens）<input name="context_tokens" aria-label="上下文容量" type="number" min="1024" step="1" placeholder="未知，留空" value="${m.context_tokens??''}"></label><label>最大输出（tokens）<input name="max_output_tokens" aria-label="模型最大输出" type="number" min="1024" step="1" placeholder="未知，留空" value="${m.max_output_tokens??''}"></label></div><label>支持的推理选项（英文值，以逗号分隔）<input name="efforts" aria-label="支持的推理选项" value="${html(m.efforts.join(', '))}" required></label><p class="model-settings-muted">保留 default（模型默认），其余选项以实际服务支持为准，例如 medium、high。</p><label class="model-settings-checkbox"><input name="verified" type="checkbox" ${m.verified?'checked':''}>已按实际模型服务资料核实以上能力</label><label>核实依据<textarea name="verification_note" aria-label="核实依据" maxlength="1000" placeholder="填写资料来源、版本或核对说明；不要填写密钥">${html(m.verification_note)}</textarea></label><p class="model-settings-muted">这是管理员声明；本页面不会发起真实模型测试。</p><p class="model-settings-error" id="ms-model-error" role="alert"></p><div class="model-settings-actions"><button type="submit" class="btn btn--primary">保存到草稿</button>${button('取消','data-ms="cancel-model"')}</div></form>`;
  }
  function renderModels() {
    return `<div class="model-settings-title"><div><h3>模型目录</h3><p class="model-settings-muted">环境中已有的模型自动列出，未知容量保持未知。</p></div>${button('＋ 新增模型','data-ms="new-model"')}</div>${editor?modelForm():''}<div class="model-settings-catalog">${draft.models.map(m=>`<article><div><strong>${html(m.name)}</strong><p class="model-settings-muted">${html(m.id)}</p><p class="model-settings-muted">推理：${m.efforts.map(e=>html(effortLabel(e))).join(' / ')}<br>上下文 ${m.context_tokens?.toLocaleString()??'未知'} · 最大输出 ${m.max_output_tokens?.toLocaleString()??'未知'} tokens</p><span class="model-settings-${m.verified?'verified':'unknown'}">${m.verified?'管理员已核实':'尚未核实'}</span></div>${button('编辑',`data-ms-model="${html(m.id)}"`)}</article>`).join('')}</div>`;
  }
  function renderHistory() {
    return `<h3>配置版本历史</h3><p class="model-settings-muted">恢复先形成草稿；确认发布后形成新版本。历史记录不会被覆盖。</p>${saved.history.length?saved.history.map(h=>`<article class="model-settings-history"><div class="model-settings-title"><strong>v${h.version}${h.version===saved.version?' · 最新保存':''}</strong>${button('恢复为草稿',`data-ms-restore="${h.version}"`,false,h.version===saved.version)}</div><p>${html(h.actor_name)} · <time>${html(timeLabel(h.created_at))}</time></p><p>${html(h.note || '未填写修改说明')}${h.restored_from?' · 恢复自 v'+h.restored_from:''}</p></article>`).join(''):'<p class="model-settings-notice">尚未保存配置版本，当前展示环境默认值。读取页面不会创建配置文件。</p>'}`;
  }
  function brief(value) {
    const candidates = [value.primary,...value.fallbacks];
    return candidates.map((c,i)=>`${i?'备用 '+i:'主模型'}：${c.model || '尚未配置'} / ${effortLabel(c.reasoning)} / 输出 ${c.max_tokens.toLocaleString()}`).join(' → ') + `；等待 ${value.timeout_seconds??'原规则'}；请求上限 ${value.max_http_attempts??'原规则'}`;
  }
  function reviewPanel() {
    if (!review) return '';
    return `<section class="model-settings-review" aria-label="发布预览"><h3>发布配置版本 v${saved.version+1}</h3><p class="model-settings-notice">本阶段只保存版本，尚不改变任何业务模型。业务接入后，新任务使用新版本，进行中的任务保留原快照。</p>${changedTasks().map(s=>`<div class="model-settings-diff"><strong>${html(s.business)} / ${html(s.label)}</strong><div><p>原配置：${html(brief(saved.configuration.tasks[s.key]))}</p><p>新配置：${html(brief(draft.tasks[s.key]))}</p></div></div>`).join('')}${!equal(draft.models,saved.configuration.models)?`<div class="model-settings-diff"><strong>模型目录变更</strong>${draft.models.filter(m=>!equal(m,saved.configuration.models.find(old=>old.id===m.id))).map(m=>`<p>${html(m.id)}：推理 ${m.efforts.map(e=>html(effortLabel(e))).join(' / ')}；上下文 ${m.context_tokens??'未知'}；输出 ${m.max_output_tokens??'未知'}；${m.verified?'管理员已核实':'尚未核实'}</p>`).join('')}</div>`:''}<p class="model-settings-notice">后续启用这些配置时，换模型可能影响准确性、信息完整性与引用可靠性；推理程度和备用尝试可能增加等待及费用。此处保存配置不调用模型。</p><details><summary>查看待核实事项（${review.warnings.length} 项）</summary><ul>${review.warnings.map(w=>`<li>${html(w)}</li>`).join('')}</ul></details><label>修改说明（选填）<textarea id="ms-note" maxlength="1000">${html(note)}</textarea></label><div class="model-settings-actions">${button('发布配置版本','data-ms="publish"',true)}${button('继续编辑','data-ms="close-review"')}</div></section>`;
  }
  function render() {
    if (!root || !saved) return;
    const count=changedTasks().length, catalogChanged=!equal(draft.models,saved.configuration.models);
    root.innerHTML=`<section class="model-settings"><div class="model-settings-title"><div><h2>模型管理</h2><p class="model-settings-muted">按业务环节维护模型、推理程度和备用顺序</p></div><span>${saved.version?'最新保存 v'+saved.version:'环境默认值'}</span></div><p class="model-settings-notice">${html(saved.message)}</p><div class="model-settings-tabs" role="tablist" aria-label="模型管理页面">${[['tasks','业务配置'],['models','模型目录'],['history','版本历史']].map(([key,label])=>`<button type="button" id="ms-tab-${key}" role="tab" aria-controls="ms-panel" aria-selected="${tab===key}" data-ms-tab="${key}">${label}</button>`).join('')}</div><div id="ms-panel" role="tabpanel" aria-labelledby="ms-tab-${tab}">${tab==='tasks'?renderTasks():tab==='models'?renderModels():renderHistory()}</div><div class="model-settings-error" role="alert" ${error?'':'hidden'}>${html(error)}</div>${conflict?`<div class="model-settings-notice">草稿没有丢失。加载最新配置后，可逐项核对双方修改。${button('加载最新版本并核对','data-ms="reconcile"')}</div>`:''}${merges.map((m,i)=>`<div class="model-settings-conflict"><strong>${html(m.label)}：双方均已修改</strong><p>我的草稿：${html(m.kind==='task'?brief(m.mine):JSON.stringify(m.mine))}</p><p>最新配置：${html(m.kind==='task'?brief(m.theirs):JSON.stringify(m.theirs))}</p><div class="model-settings-actions">${button('保留我的修改',`data-ms-resolve="${i}" data-choice="mine"`)}${button('采用最新修改',`data-ms-resolve="${i}" data-choice="theirs"`)}</div></div>`).join('')}<div class="model-settings-footer"><div><strong>${dirty()?[count?count+' 个环节已修改':'',catalogChanged?'模型目录已修改':''].filter(Boolean).join(' · '):'没有未保存修改'}</strong><p class="model-settings-muted">${restoredFrom?'已恢复 v'+restoredFrom+' 为草稿，尚未发布。':'草稿保留在当前页面；刷新或离开页面前会提醒。'}</p></div><div class="model-settings-actions">${button('撤销全部修改','data-ms="discard"',false,!hasDraft())}${button('预览发布','data-ms="preview"',true,!dirty()||!!editor||merges.length>0||conflict)}</div></div><p class="model-settings-status" role="status" aria-live="polite">${html(busy?'正在处理配置，请稍候…':message)}</p>${reviewPanel()}</section>`;
    root.querySelectorAll('button,input,select,textarea').forEach(el=>{if(busy)el.disabled=true;});
    root.querySelector('[data-ms-advanced]')?.addEventListener('toggle',e=>{advanced=e.target.open;});
  }
  async function action(fn) {
    if (busy) return;
    busy=true;error='';render();
    try { await fn(); }
    catch (e) {error=[e.message,...(e.problems||[])].join('\n');if(e.status===409)conflict=true;}
    finally {busy=false;render();}
  }
  async function reconcile() {
    const latest=await request(''),old=saved.configuration,ours=copy(draft),merged=copy(latest.configuration);
    merges=[];
    for (const s of latest.tasks) {
      const a=old.tasks[s.key],b=ours.tasks[s.key],c=latest.configuration.tasks[s.key];
      if(b && !equal(a,b)){merged.tasks[s.key]=b;if(!equal(a,c)&&!equal(b,c))merges.push({kind:'task',key:s.key,label:s.label,mine:b,theirs:c});}
    }
    for (const m of ours.models) {
      const a=old.models.find(v=>v.id===m.id),c=latest.configuration.models.find(v=>v.id===m.id);
      if(!equal(a,m)){const index=merged.models.findIndex(v=>v.id===m.id);if(index<0)merged.models.push(m);else merged.models[index]=m;if(!equal(a,c)&&!equal(m,c))merges.push({kind:'model',key:m.id,label:m.id,mine:m,theirs:c});}
    }
    saved=latest;draft=merged;conflict=false;review=null;restoredFrom=null;
    message=merges.length?'请逐项选择如何处理双方修改。':'已保留你的修改，并载入其他管理员的新配置；请重新预览。';
  }
  async function handleClick(event) {
    const b=event.target.closest('button');if(!b || !root?.contains(b) || b.disabled || busy)return;
    if(b.dataset.msTab){tab=b.dataset.msTab;review=null;render();return;}
    if(b.dataset.msTask){task=b.dataset.msTask;render();return;}
    if(b.hasAttribute('data-ms-remove')){startEdit();current().fallbacks.splice(Number(b.dataset.msRemove),1);render();return;}
    if(b.hasAttribute('data-ms-move')){startEdit();const i=Number(b.dataset.msMove),j=i+Number(b.dataset.dir);[current().fallbacks[i],current().fallbacks[j]]=[current().fallbacks[j],current().fallbacks[i]];render();return;}
    if(b.dataset.msModel){editor={...copy(model(b.dataset.msModel)),original:b.dataset.msModel};render();return;}
    if(b.hasAttribute('data-ms-resolve')){const i=Number(b.dataset.msResolve),m=merges[i],chosen=copy(b.dataset.choice==='mine'?m.mine:m.theirs);if(m.kind==='task')draft.tasks[m.key]=chosen;else{draft.models=draft.models.filter(v=>v.id!==m.key);if(chosen)draft.models.push(chosen);}merges.splice(i,1);render();return;}
    if(b.hasAttribute('data-ms-restore')){
      if(hasDraft()){message='请先保存或撤销现有草稿，再恢复历史版本。';render();return;}
      return action(async()=>{const result=await request('/versions/'+Number(b.dataset.msRestore)+'/restore',{expected_revision:saved.revision});draft=result.configuration;restoredFrom=result.restored_from;review=null;message=result.message;});
    }
    switch(b.dataset.ms){
      case 'reload': return load();
      case 'add-fallback': {const m=draft.models.find(m=>![current().primary,...current().fallbacks].some(c=>c.model===m.id));if(!m){message='没有可添加的其他模型，请先维护模型目录。';render();return;}startEdit();current().fallbacks.push({model:m.id,reasoning:'default',max_tokens:Math.min(current().primary.max_tokens,m.max_output_tokens??current().primary.max_tokens)});render();break;}
      case 'new-model': if(editor){message='请先保存或取消正在编辑的模型。';render();return;}editor={id:'',name:'',efforts:['default'],context_tokens:null,max_output_tokens:null,verified:false,verification_note:''};render();break;
      case 'cancel-model':editor=null;render();break;
      case 'close-review':review=null;render();break;
      case 'discard':if(window.workflowConfirm && !await window.workflowConfirm('撤销全部未保存的模型配置修改？已保存版本不受影响。','撤销修改'))return;draft=copy(saved.configuration);editor=null;review=null;restoredFrom=null;merges=[];conflict=false;error='';message='已撤销全部草稿修改。';render();break;
      case 'preview':return action(async()=>{const result=await request('/check',{expected_revision:saved.revision,configuration:draft,restored_from:restoredFrom});if(!result.valid){error=result.errors.join('\n');review=null;}else{review=result;message='配置检查完成，没有调用模型。';}});
      case 'publish':return action(async()=>{const response=await request('/publish',{expected_revision:saved.revision,configuration:draft,note,restored_from:restoredFrom});accept(response);note='';message=response.message;});
      case 'reconcile':return action(reconcile);
    }
  }
  document.addEventListener('click',e=>{if(e.target.closest('#model-settings-root'))handleClick(e).catch(()=>{error='操作未完成，草稿已保留，请重试。';busy=false;render();});});
  document.addEventListener('change',e=>{
    if(!root?.contains(e.target)||busy||!saved)return;
    const el=e.target;
    if(el.id==='ms-business'){business=el.value;task=saved.tasks.find(s=>s.business===business).key;render();return;}
    if(el.hasAttribute('data-ms-candidate')){
      startEdit();const i=Number(el.dataset.msCandidate),field=el.dataset.field,c=i<0?current().primary:current().fallbacks[i];c[field]=field==='max_tokens'?Number(el.value):el.value;
      if(field==='model' && !model(c.model)?.efforts.includes(c.reasoning)){c.reasoning='default';message='新模型未声明原推理选项，已改为模型默认；输出长度仍保留，请检查能力限制。';}
      render();root.querySelector(`[data-ms-candidate="${i}"][data-field="${field}"]`)?.focus({preventScroll:true});return;
    }
    if(el.dataset.msRoute){startEdit();current()[el.dataset.msRoute]=el.value===''?null:Number(el.value);render();}
  });
  document.addEventListener('input',e=>{
    if(!root?.contains(e.target)||busy)return;
    if(e.target.id==='ms-note')note=e.target.value;
    if(editor && e.target.closest('#ms-model-form')){
      const el=e.target;
      editor[el.name]=el.name==='verified'?el.checked:el.name==='efforts'?el.value.split(/[,，]/).map(v=>v.trim()).filter(Boolean):['context_tokens','max_output_tokens'].includes(el.name)?(el.value===''?null:Number(el.value)):el.value;
    }
  });
  document.addEventListener('submit',e=>{
    if(e.target.getAttribute('id')!=='ms-model-form')return;e.preventDefault();if(busy)return;
    const m={...copy(editor)};delete m.original;
    const problems=[];
    if(!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$/.test(m.id))problems.push('模型标识须为 1 至 100 位字母、数字或 . _ : / -');
    if(!m.name.trim()||m.name.length>100)problems.push('请填写 1 至 100 字的显示名称');
    if(!editor.original && model(m.id))problems.push('该模型标识已存在');
    if(!m.efforts.includes('default')||new Set(m.efforts).size!==m.efforts.length||m.efforts.some(v=>!/^[a-z][a-z0-9_-]{0,31}$/.test(v)))problems.push('推理选项须为英文值，不重复且保留 default');
    for(const key of ['context_tokens','max_output_tokens'])if(m[key]!==null&&(!Number.isInteger(m[key])||m[key]<1024||m[key]>100000000))problems.push('容量须为 1,024 至 100,000,000 的整数，未知可留空');
    if(m.context_tokens!==null&&m.max_output_tokens!==null&&m.max_output_tokens>=m.context_tokens)problems.push('最大输出必须小于上下文容量');
    if(m.verified&&(!m.context_tokens||!m.max_output_tokens||!m.verification_note.trim()))problems.push('标记已核实前须填写容量、输出限制与核实依据');
    if(problems.length){root.querySelector('#ms-model-error').textContent=problems.join('；');return;}
    startEdit();const index=draft.models.findIndex(v=>v.id===editor.original);if(index<0)draft.models.push(m);else draft.models[index]=m;editor=null;message='模型目录已更新到草稿，尚未保存版本。';render();
  });
  document.addEventListener('keydown',e=>{
    if(!root?.contains(e.target)||!e.target.matches('[data-ms-tab]')||!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
    const tabs=[...root.querySelectorAll('[data-ms-tab]')],i=tabs.indexOf(e.target),j=e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
    e.preventDefault();tabs[j].click();root.querySelector('#'+tabs[j].id)?.focus();
  });
  window.addEventListener('beforeunload',e=>{if(hasDraft()){e.preventDefault();e.returnValue='';}});
  return {load,hasDraft};
})();
