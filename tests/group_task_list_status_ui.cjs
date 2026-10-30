// Synthetic list/handler checks. --serve previews the same production UI in memory.
// No app startup, real data, model calls, dependencies or generated files.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'static/js/features/group-tasks.js'),'utf8');
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
function fixture(patch={}){
  return {id:'synthetic',version:1,owner_name:'合成负责人',can_edit:true,status:'ready',
    rules:{name:'合成标注任务',payout_date:'2026-10-30',ai_detect:false},
    headers:['玩家 ID','体验反馈'],mapping:{id_col:0,open_text_cols:[1]},feedback:[],runs:[],
    workflow:{max_step:4},annotation_issues:[],
    summary:{complete_count:38,pending_count:4,feedback_count:42,player_count:42},
    annotation:{phase:'quality_progress',complete:false,options:{ai_detect:false,quality:true},
      total:42,ai_done:42,quality_total:42,quality_done:38,review_ids:[],reviewed_ids:[],high_threshold:80,review_threshold:60},...patch};
}
function run(stage='quality',status='incomplete',error=''){
  return {id:'run-'+stage,version:1,stage,status,error,completed_ids:Array.from({length:38},(_,i)=>String(i)),
    attempted_ids:Array.from({length:42},(_,i)=>String(i)),target_ids:Array.from({length:42},(_,i)=>String(i))};
}
const aiAnnotation={...fixture().annotation,phase:'ai_progress',options:{ai_detect:true,quality:true},ai_done:38};
const completeAnnotation={...fixture().annotation,phase:'results',complete:true,quality_done:42};
const paid={batch_id:'paid-batch',version:1,paid_at:'2026-10-30T07:00:00+00:00',paid_by:'合成发奖人',changed:false};
const cases=[
  ['规则待填写',fixture({status:'draft'}),'待配置','warning','完善任务'],
  ['输入待处理',fixture({status:'input_issues'}),'输入待处理','warning','处理输入问题'],
  ['等待启动 AI',fixture({annotation:aiAnnotation}),'待开始 AI 识别','warning','前往开始识别'],
  ['等待启动质量打标',fixture(),'待开始质量打标','warning','前往开始打标'],
  ['排队',fixture({status:'queued',run:run('quality','queued','旧批次失败')}),'排队中','neutral','查看进度'],
  ['AI 正在运行',fixture({status:'running',run:run('ai','running','部分失败'),annotation:aiAnnotation}),'AI 识别中','neutral','查看进度'],
  ['质量正在运行',fixture({status:'running',run:run('quality','running','部分失败')}),'质量打标中','neutral','查看进度'],
  ['等待当前反馈保存后暂停',fixture({status:'pausing',run:{...run('quality','running'),pause_requested:true}}),'暂停中','warning','查看进度'],
  ['已暂停等待继续',fixture({status:'paused',run:{...run('quality','paused'),pause_requested:true}}),'已暂停','warning','查看进度'],
  ['暂停中遇到重启',fixture({status:'paused',run:{...run('ai','paused'),pause_requested:true,timing_partial:true},annotation:aiAnnotation}),'已暂停','warning','查看进度'],
  ['等待人工确认',fixture({status:'awaiting_ai_confirmation',run:run('ai'),annotation:{...aiAnnotation,phase:'ai_review'}}),'待确认 AI 作答','warning','确认 AI 作答'],
  ['结果待确认',fixture({status:'awaiting_confirmation',annotation:completeAnnotation}),'待确认','warning','查看结果'],
  ['仅 AI 标注完成',fixture({status:'annotation_done',annotation:{...completeAnnotation,options:{ai_detect:true,quality:false}}}),'标注完成','neutral','查看结果'],
  ['已结算',fixture({status:'confirmed',annotation:completeAnnotation,settlement:paid}),'已结算','settled','查看结果'],
  ['已结算后又标注中',fixture({status:'running',run:run('quality','running'),settlement:{...paid,changed:true}}),'质量打标中','neutral','查看进度'],
  ['已结算后结果未补齐',fixture({status:'incomplete',run:run(),settlement:{...paid,changed:true}}),'质量打标未完成','error','查看并重试'],
  ['可结算',fixture({status:'confirmed',annotation:completeAnnotation}),'可结算','neutral','查看结果'],
  ['AI 失败但外层仍是 ready',fixture({run:run('ai'),annotation:aiAnnotation}),'AI 识别未完成','error','查看并重试'],
  ['质量打标未完成',fixture({status:'incomplete',run:run()}),'质量打标未完成','error','查看并重试'],
  ['自动衔接失败',fixture({status:'incomplete',run:run('ai','completed'),annotation_auto_error:'合成启动失败'}),'后续标注未启动','error','查看并处理'],
  ['旧任务缺少运行记录',fixture({status:'incomplete'}),'标注待补齐','error','查看并处理'],
  ['当前校验发现缓存缺失',fixture({status:'incomplete',run:run('quality','completed')}),'质量打标未完成','error','查看并重试'],
  ['有错误但无明确结束状态',fixture({status:'incomplete',run:{...run('quality','incomplete','本轮失败'),status:undefined}}),'质量打标未完成','error','查看并重试'],
];
function environment(){
  class Element{
    constructor(){this.innerHTML='';this.textContent='';this.value='';this.style={};this.listeners={};this.isConnected=true;this.queries=new Map();
      this.classList={add(){},remove(){},toggle(){},contains(){return false;}};}
    addEventListener(type,handler){this.listeners[type]=handler;}
    querySelectorAll(){return [];}
    querySelector(selector){if(!this.queries.has(selector))this.queries.set(selector,new Element());return this.queries.get(selector);}
    insertAdjacentHTML(where,html){this.innerHTML+=html;}
    getBoundingClientRect(){return {right:100};}
  }
  const nodes=new Map(),requests=[],toasts=[];
  const $=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  let items=[],selected,poll,controlHandler,confirmHandler=async()=>true;
  const context={console,$,workflowTime:String,esc:escapeHtml,document:{hidden:false},PlatformPermissions:{canUse:()=>true},
    showToast:(...args)=>toasts.push(args),setInterval:fn=>{poll=fn;},window:{addEventListener(){},workflowConfirm:(...args)=>confirmHandler(...args),
      workflowRequest:async(url,options)=>{requests.push({url,options});if(options?.method&&options.method!=='GET'){
          assert(controlHandler,'ordinary navigation must never submit a run');
          if(options.method!=='DELETE'){assert.match(url,/\/(pause|resume)$/);assert.equal(options.method,'POST');}
          return controlHandler(url,options);
        }
        if(url==='/api/group-tasks')return {tasks:items};
        if(url==='/api/group-tasks/groups')return {groups:[]};
        if(url===`/api/group-tasks/${selected?.id}`)return selected;
        throw new Error('Unexpected synthetic request '+url);}}};
  vm.createContext(context);
  vm.runInContext(source.replace('return {open,list};','return {open,list,tests:{listState,renderList}};'),context);
  return {context,$,requests,toasts,tests:context.window.GroupTasks.tests,poll:()=>poll(),setItems:value=>{items=value;},setSelected:value=>{selected=value;},setControl:value=>{controlHandler=value;},setConfirm:value=>{confirmHandler=value;}};
}
async function checks(){
  const env=environment(),{tests,$}=env;
  for(const [name,task,label,tone,button] of cases){
    const before=JSON.stringify(task),state=tests.listState(task);
    assert.equal(state.label,label,name);assert.equal(state.tone,tone,name);assert.equal(state.button,button,name);
    assert.equal(JSON.stringify(task),before,'display must not change saved results: '+name);
    tests.renderList([task]);
    const html=$('gw-task-list').innerHTML;
    assert(html.includes(`gw-list-badge--${tone}">${label}</span>`),name);
    assert.match(html,new RegExp(`data-gw="view"[^>]*>${button}</button>`),name);
    assert.doesNotMatch(html,/data-gw="records"/,'task records stay inside result tabs: '+name);
    assert(html.includes('任务设置'),name);
    if(['running','queued'].includes(task.status))assert.match(html,/data-gw="pause"[^>]*>暂停任务<\/button>/,name);
    else if(task.status==='pausing')assert.match(html,/data-gw="pause" disabled[^>]*>正在暂停…<\/button>/,name);
    else if(task.status==='paused')assert.match(html,/data-gw="resume"[^>]*>继续任务<\/button>/,name);
    else assert.doesNotMatch(html,/data-gw="(?:pause|resume)"/,name);
    const readOnly=tests.listState({...task,can_edit:false});
    assert.equal(readOnly.button,'查看详情',name);
    if(!['neutral','settled'].includes(tone)&&task.status!=='pausing'){
      assert.match(readOnly.hint,/请联系任务负责人处理/,name);
      assert.doesNotMatch(readOnly.hint,/重试|请开始|请完成/,name);
    }
    tests.renderList([{...task,can_edit:false}]);
    assert.doesNotMatch($('gw-task-list').innerHTML,/data-gw="(?:pause|resume)"/,'read-only controls hidden: '+name);
  }
  tests.renderList([fixture({status:'confirmed',annotation:completeAnnotation,settlement:paid}),fixture({status:'confirmed',annotation:completeAnnotation})]);
  assert.match($('gw-task-list').innerHTML,/gw-list-badge--settled/);
  assert.match($('gw-task-list').innerHTML,/data-gw="settings"/,'settlement cannot disable task settings');
  assert.match($('gw-list-summary').innerHTML,/可结算任务<strong>1/);
  assert.match($('gw-list-summary').innerHTML,/已结算任务<strong>1/);
  tests.renderList([fixture({status:'incomplete',run:run(),settlement:{...paid,changed:true}})]);
  assert.match($('gw-task-list').innerHTML,/gw-list-badge--error/);
  assert.match($('gw-task-list').innerHTML,/gw-settled-badge/);
  assert.match($('gw-task-list').innerHTML,/结算后有修改/);
  tests.renderList([fixture({settlement_unavailable:true})]);
  assert.match($('gw-task-list').innerHTML,/结算状态暂时无法读取/);
  assert.match($('gw-task-list').innerHTML,/任务设置/);
  const stopped=fixture({status:'incomplete',run:run()});
  const retrying={...stopped,status:'queued',run:run('quality','queued'),annotation_auto_error:'旧错误'};
  const recovered={...stopped,status:'confirmed',annotation:completeAnnotation,annotation_auto_error:'旧错误'};
  env.setItems([stopped]);await env.context.window.GroupTasks.list();
  assert.match($('gw-task-list').innerHTML,/gw-list-badge--error/);
  env.setItems([retrying]);await env.poll();
  assert.doesNotMatch($('gw-task-list').innerHTML,/gw-list-badge--error/);
  env.setItems([recovered]);await env.poll();
  assert.match($('gw-task-list').innerHTML,/可结算/);assert.doesNotMatch($('gw-task-list').innerHTML,/gw-list-badge--error/);
  assert.equal(tests.listState(fixture({status:'awaiting_ai_confirmation',annotation:{...aiAnnotation,phase:'ai_review'},run:run('ai','incomplete','历史错误')})).tone,'warning','AI review must clear the historical failure');
  for(const [reason,expected] of [['服务中断，已保存结果',/服务中断.*重复模型费用/],['本轮达到执行预算',/达到执行限制/],['个人模型凭据不可用',/检查个人模型设置/],['权限不可用',/联系管理员/],['权限或个人模型凭据不可用',/权限或模型凭据不可用/],['内部异常<secret>',/本轮已停止/]]){
    const hint=tests.listState({...stopped,run:run('quality','incomplete',reason)}).hint;
    assert.match(hint,expected);assert(!hint.includes('<secret>'),'raw errors must not enter the list');
  }
  const hostile=fixture({rules:{name:'<img src=x onerror=alert(1)>',payout_date:'2026-10-30'},owner_name:'<script>bad</script>'});
  tests.renderList([hostile]);assert.doesNotMatch($('gw-task-list').innerHTML,/<img|<script>/);
  env.setItems([stopped,recovered]);await env.context.window.GroupTasks.list();
  $('gw-list-filter').oninput({target:{value:'无匹配项'}});assert.match($('gw-task-list').innerHTML,/尚无匹配任务/);
  $('gw-list-filter').oninput({target:{value:''}});
  // Use the actual delegated list button handler. It must open the matching stage without POSTing.
  for(const selected of [stopped,fixture({run:run('ai'),annotation:aiAnnotation,workflow:{max_step:3},rules:{name:'AI 合成任务',ai_detect:true}})]){
    env.setSelected(selected);
    const button={dataset:{gw:'view',id:selected.id},isConnected:true,disabled:false};
    await $('group-tasks-panel').listeners.click({target:{closest:selector=>selector==='[data-gw]'?button:null}});
    const stage=selected.run.stage;
    assert.match($(`ga-${stage==='ai'?'ai':'quality'}-progress-msg`).textContent,/未完成/);
    assert($('gw-content').innerHTML.includes(stage==='ai'?'重试 AI 识别':'重试质量打标'));
  }
  assert.deepEqual(env.toasts,[]);
  await env.context.window.GroupTasks.list();
  const active=fixture({id:'control-task',status:'running',run:run('quality','running')});
  let controlled=active;
  env.setItems([active]);await env.context.window.GroupTasks.list();
  env.setControl((url,options)=>{
    assert.equal(options.body.expected_version,active.version);assert.equal(options.body.run_id,active.run.id);
    controlled={...controlled,status:url.endsWith('/pause')?'pausing':'queued',run:{...controlled.run,status:url.endsWith('/pause')?'running':'queued',pause_requested:url.endsWith('/pause')}};
    env.setItems([controlled]);return controlled;
  });
  async function controlClick(action){
    const button={dataset:{gw:action,id:active.id,runId:active.run.id,version:String(active.version)},isConnected:true,disabled:false};
    await $('group-tasks-panel').listeners.click({target:{closest:selector=>selector==='[data-gw]'?button:null}});
  }
  await controlClick('pause');
  assert.match($('gw-task-list').innerHTML,/正在暂停…/);assert.match(env.toasts.at(-1)[0],/保存后暂停/);
  controlled={...controlled,status:'paused',run:{...controlled.run,status:'paused'}};
  env.setItems([controlled]);await env.poll();
  assert.match($('gw-task-list').innerHTML,/继续任务/);
  await controlClick('resume');
  assert.match($('gw-task-list').innerHTML,/排队中/,'list resume still works');
  controlled={...controlled,status:'paused',run:{...controlled.run,status:'paused'}};
  env.setItems([controlled]);await env.context.window.GroupTasks.list();
  env.setSelected(controlled);
  const view={dataset:{gw:'view',id:active.id},isConnected:true,disabled:false};
  await $('group-tasks-panel').listeners.click({target:{closest:selector=>selector==='[data-gw]'?view:null}});
  assert.match($('ga-quality-progress-msg').textContent,/已暂停/);
  assert.match($('ga-panel-5').querySelector('.panel__desc').textContent,/当前结果已保存/);
  assert.doesNotMatch($('group-tasks-panel').innerHTML,/data-gw="resume"/,'resume moves out of the task heading');
  assert.match($('ga-panel-5').querySelector('.panel__header').innerHTML,/gw-stage-progress-copy[\s\S]*data-gw="resume"/,'resume follows stage text in its heading');
  assert.doesNotMatch($('gw-content').innerHTML,/data-gw="annotation-run"/,'paused detail must not expose a restart shortcut');
  await controlClick('resume');
  await env.context.window.GroupTasks.list();
  assert.match($('gw-task-list').innerHTML,/排队中/);assert.doesNotMatch($('gw-task-list').innerHTML,/继续任务/);
  env.setControl(()=>{throw new Error('合成保存失败');});
  await controlClick('pause');assert.match(env.toasts.at(-1)[0],/合成保存失败/);
  assert.match($('gw-task-list').innerHTML,/排队中/,'a failed save cannot show a successful pause');
  env.setItems([{...controlled,can_edit:false}]);await env.context.window.GroupTasks.list();
  const beforeDenied=env.requests.length;await controlClick('pause');assert.equal(env.requests.length,beforeDenied,'read-only action sends no request');
  await pausedDeleteChecks();
  console.log(`Quality list status UI passed: ${cases.length} states, settings resume hidden, list/stage resume handlers, read-only permissions, polling, navigation and paused deletion/cancel/failure.`);
}
async function pausedDeleteChecks(){
  const env=environment(),{$}=env;
  async function click(gw,id='delete-task'){
    const button={dataset:{gw,id},isConnected:true,disabled:false};
    await $('group-tasks-panel').listeners.click({target:{closest:selector=>selector==='[data-gw]'?button:null}});
  }
  for(const status of ['queued','running','pausing','paused']){
    for(const can_edit of [true,false]){
      const selected=fixture({id:'delete-task',status,can_edit,run:{...run('quality',status==='pausing'?'running':status),pause_requested:status==='pausing'}});
      env.setSelected(selected);env.setItems([selected]);await env.context.window.GroupTasks.list();await click('settings');
      const html=$('gw-content').innerHTML;
      assert.doesNotMatch($('group-tasks-panel').innerHTML+html,/data-gw="resume"/,'settings never expose a resume button');
      assert.match($('group-tasks-panel').innerHTML,/data-gw="back"/,'settings keep the return-to-list entry');
      if(status==='paused'){
        assert.match($('group-tasks-panel').innerHTML,/任务已暂停，可返回任务列表继续/);
        assert.doesNotMatch($('group-tasks-panel').innerHTML,/点击“继续任务”/,'settings hint must match the available controls');
      }
      assert.match(html,/data-gw="save-settings" disabled/,'paused/active settings stay locked');
      if(status==='paused'&&can_edit){assert.match(html,/data-gw="delete-task"\s+>/);assert.match(html,/任务已暂停，可以删除/);}
      else{
        assert.match(html,/data-gw="delete-task" disabled/);
        const before=env.requests.length;await click('delete-task');
        assert.equal(env.requests.length,before,'disabled state also checked by action handler');
      }
    }
  }
  let paused=fixture({id:'delete-task',status:'paused',run:run('quality','paused')});
  env.setSelected(paused);env.setItems([paused]);await env.context.window.GroupTasks.list();await click('settings');
  let deletes=0;
  env.setControl((url,options)=>{assert.equal(options.method,'DELETE');assert.equal(url,'/api/group-tasks/delete-task');assert.equal(options.body.expected_version,paused.version);deletes++;return {deleted:true};});
  env.setConfirm(async()=>false);await click('delete-task');assert.equal(deletes,0,'cancel does not delete');
  env.setConfirm(async()=>true);
  env.setControl(()=>{throw Error('合成备份校验失败');});await click('delete-task');
  assert.match(env.toasts.at(-1)[0],/备份校验失败/);assert.match($('gw-content').innerHTML,/任务已暂停，可以删除/,'failed deletion preserves settings');
  env.setControl((url,options)=>{assert.equal(options.method,'DELETE');assert.equal(options.body.expected_version,paused.version);deletes++;env.setItems([]);return {deleted:true};});
  await click('delete-task');assert.equal(deletes,1);assert.match($('gw-task-list').innerHTML,/尚无匹配任务/);assert.match(env.toasts.at(-1)[0],/任务已删除/);
  for(const [stage,ai_detect] of [['ai',true],['ai',false],['quality',false]]){
    for(const can_edit of [true,false]){
      const selected=fixture({id:'delete-task',can_edit,status:'paused',run:run(stage,'paused'),annotation:stage==='ai'?aiAnnotation:fixture().annotation,workflow:{max_step:stage==='ai'?3:4},rules:{name:'暂停布局样本',ai_detect}});
      env.setSelected(selected);env.setItems([selected]);await env.context.window.GroupTasks.list();await click('view');
      const heading=$(`ga-panel-${stage==='ai'?3:5}`).querySelector('.panel__header').innerHTML;
      assert.match(heading,/gw-stage-progress-copy/);
      assert.doesNotMatch($('group-tasks-panel').innerHTML,/data-gw="resume"/);
      if(can_edit)assert.equal((heading.match(/data-gw="resume"/g)||[]).length,1);
      else assert.doesNotMatch(heading,/data-gw="resume"/);
      assert.doesNotMatch($('gw-content').innerHTML,/data-gw="resume"/,'resume is outside the progress block');
      if(can_edit){
        let resumed=false;
        env.setControl((url,options)=>{
          assert.equal(url,`/api/group-tasks/${selected.id}/resume`);assert.equal(options.method,'POST');
          assert.equal(options.body.run_id,selected.run.id);assert.equal(options.body.expected_version,selected.version);
          resumed=true;return {...selected,status:'queued',run:{...selected.run,status:'queued'}};
        });
        const button={dataset:{gw:'resume',id:selected.id,runId:selected.run.id,version:String(selected.version)},isConnected:true,disabled:false};
        await $('group-tasks-panel').listeners.click({target:{closest:selector=>selector==='[data-gw]'?button:null}});
        assert(resumed,'stage resume handler still submits the matching run');
        assert.match(env.toasts.at(-1)[0],/任务已继续/);
        assert.doesNotMatch($('group-tasks-panel').innerHTML,/data-gw="resume"/,'resume leaves the paused state');
      }
    }
  }
}
function serve(){
  const http=require('node:http');
  const items=cases.map(([name,t],i)=>{
    const item=structuredClone({...t,id:'synthetic-'+i,rules:{...t.rules,name}});
    if(['ai_progress','ai_review'].includes(item.annotation.phase)){item.workflow.max_step=3;item.rules.ai_detect=true;}
    if(item.annotation.complete)item.summary={...item.summary,complete_count:42,pending_count:0};
    if(item.annotation.phase==='ai_review'||item.run?.status==='completed'){
      if(item.run){item.run.completed_ids=[...item.run.target_ids];item.run.attempted_ids=[...item.run.target_ids];}
    }
    if(item.status==='draft')item.summary={...item.summary,complete_count:0,pending_count:0,feedback_count:0,player_count:0};
    return item;
  });
  const html=`<!doctype html><html lang="zh-CN" data-theme="light"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>质量标注列表 · 合成状态验证</title><link rel="stylesheet" href="/style.css"><style>html,body{height:auto;overflow:auto}body{padding:24px}#group-tasks-panel{padding:24px;background:var(--surface);border-radius:14px}.qa-controls{display:flex;gap:8px;margin-bottom:16px}@media(max-width:600px){body{padding:8px}#group-tasks-panel{padding:12px}}</style><body><div class="qa-controls"><button class="btn btn--ghost" id="qa-theme">切换明暗主题</button><button class="btn btn--ghost" id="qa-readonly">切换只读查看</button><span>合成任务，不连接后台或模型</span></div><section id="group-tasks-panel"></section><script>
    const fixtures=${JSON.stringify(items).replace(/</g,'\\u003c')};let readOnly=false;
    window.$=id=>document.getElementById(id);window.esc=${escapeHtml.toString()};window.PlatformPermissions={canUse:()=>!readOnly};
    window.showToast=message=>console.log(message);window.workflowTime=String;window.formatReportDuration=String;window.workflowConfirm=async()=>true;
    window.workflowRequest=async(url,options)=>{
      if(options?.method&&options.method!=='GET'){
        if(options.method==='DELETE'){
          const index=fixtures.findIndex(t=>url==='/api/group-tasks/'+t.id),t=fixtures[index];
          if(!t||readOnly||['queued','running','pausing'].includes(t.status)||options.body.expected_version!==t.version)throw Error('合成删除被拒绝');
          fixtures.splice(index,1);return {id:t.id,deleted:true};
        }
        const match=url.match(/^\\/api\\/group-tasks\\/([^/]+)\\/(pause|resume)$/),t=match&&fixtures.find(t=>t.id===match[1]);
        if(!t||readOnly||options.body.run_id!==t.run?.id||options.body.expected_version!==t.version)throw Error('合成操作被拒绝');
        if(match[2]==='pause'){
          if(!['queued','running'].includes(t.status))throw Error('合成状态已变化');
          t.status=t.status==='running'?'pausing':'paused';t.run.pause_requested=true;
          if(t.status==='paused')t.run.status='paused';
          else setTimeout(()=>{t.status='paused';t.run.status='paused';t.updated_at=String(Date.now());},1000);
        }else{if(t.status!=='paused')throw Error('合成任务尚未暂停');t.status='queued';t.run.status='queued';t.run.pause_requested=false;}
        t.updated_at=String(Date.now());return {...t,can_edit:!readOnly};
      }
      if(url==='/api/group-tasks')return {tasks:fixtures.map(t=>({...t,can_edit:!readOnly}))};if(url.endsWith('/groups'))return {groups:[]};const t=fixtures.find(t=>url==='/api/group-tasks/'+t.id);if(t)return {...t,can_edit:!readOnly};throw Error('未配置的合成接口');
    };
    $('qa-theme').onclick=()=>document.documentElement.dataset.theme=document.documentElement.dataset.theme==='light'?'dark':'light';
    $('qa-readonly').onclick=()=>{readOnly=!readOnly;GroupTasks.list();};
    </script><script src="/group-tasks.js"></script><script>GroupTasks.list();</script></body></html>`;
  const server=http.createServer((req,res)=>{
    const routes={'/':[html,'text/html; charset=utf-8'],'/style.css':[fs.readFileSync(path.join(root,'static/style.css')),'text/css'],'/group-tasks.js':[source,'text/javascript; charset=utf-8']};
    const resource=routes[req.url];if(!resource){res.writeHead(404);res.end();return;}
    res.writeHead(200,{'Content-Type':resource[1]});res.end(resource[0]);
  });
  server.listen(0,'127.0.0.1',()=>console.log('Synthetic quality-list preview: http://127.0.0.1:'+server.address().port));
}
checks().then(()=>{if(process.argv.includes('--serve'))serve();}).catch(error=>{console.error(error);process.exitCode=1;});
