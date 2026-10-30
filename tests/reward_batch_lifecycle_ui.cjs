// Synthetic handler checks and optional browser preview. No production server/data/model.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'static/js/features/reward-batches.js'),'utf8');
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
function fixture(){
  return {id:'synthetic-batch',status:'pending',revision:1,can_manage:true,created_at:'2026-10-30T06:30:00Z',owner_name:'合成研究员',
    dates:['2026-10-30'],unique_accounts:1,total_diamonds:500,accounts:[{account:'818043063(6389)',total_diamonds:500,
      sources:[{task:'合成体验任务',sheet:'测试群',player_id:'synthetic-player',date:'2026-10-30',normal_reward:300,excellent_reward:200,other_reward:0,total_reward:500}]}],
    task_versions:{'synthetic-task':{version:6,rules:{name:'合成体验任务'}}},txt_preview:'6389\t818043063\t301-500',note_full:'合成核对说明'};
}
function taskFixture(){
  return {id:'synthetic-task',version:6,owner_name:'合成研究员',can_edit:true,status:'confirmed',
    rules:{name:'合成体验任务',payout_date:'2026-10-30',ai_detect:false},
    summary:{complete_count:4,pending_count:0,feedback_count:4,player_count:1,base:300,excellent:200},
    annotation:{complete:true},reward_ready:true};
}
function environment(){
  class Element{
    constructor(){this.innerHTML='';this.textContent='';this.listeners={};this.isConnected=true;this.classList={add(){},remove(){},contains(){return false;}};}
    addEventListener(type,handler){this.listeners[type]=handler;}
    querySelectorAll(){return [];}
    insertAdjacentHTML(where,html){this.innerHTML+=html;}
  }
  const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  let saved=fixture(),allowed=true,answer=true,fail=false,waitForAction=null;
  const requests=[],confirmations=[],toasts=[],copies=[];
  const choices=()=>({tasks:[{...taskFixture(),selectable:saved.status==='voided',consumed:saved.status==='voided'?null:saved.id}],other_rewards:[],imports:[],groups:[],batches:[saved]});
  const api=async(url,options={})=>{
    requests.push({url,...options});
    if(options.method==='POST'){
      if(waitForAction)await waitForAction;
      if(fail){fail=false;throw Error('合成保存失败');}
      assert.equal(options.body.confirmed,true);assert.equal(options.body.expected_revision,saved.revision||1);
      saved={...saved,status:url.endsWith('/void')?'voided':'paid',revision:2,
        paid_by:'合成发奖人',paid_at:'2026-10-30T07:00:00Z',voided_by:'合成研究员',voided_at:'2026-10-30T07:00:00Z'};
      return structuredClone(saved);
    }
    if(url.endsWith('/choices'))return structuredClone(choices());
    return structuredClone(saved);
  };
  const context={window:{workflowRequest:api,addEventListener(){}},$,esc:escapeHtml,workflowTime:String,
    workflowConfirm:async(message,title)=>{confirmations.push({message,title});return answer;},
    PlatformPermissions:{canUse:()=>allowed},navigator:{clipboard:{writeText:async text=>copies.push(text)}},
    showToast:(...args)=>toasts.push(args)};
  vm.createContext(context);vm.runInContext(source,context);
  async function click(action){
    const button={dataset:{rb:action,id:saved.id},isConnected:true,disabled:false};
    await $('reward-batches-panel').listeners.click({target:{closest:()=>button}});
    assert.equal(button.disabled,false);
  }
  return {$,context,requests,toasts,copies,confirmations,click,get saved(){return saved;},
    answer:value=>{answer=value;},allow:value=>{allowed=value;},fail:()=>{fail=true;},hold:value=>{waitForAction=value;},
    setSaved:value=>{saved=value;}};
}
async function checks(){
  const env=environment(),{$,click}=env;
  await env.context.window.RewardBatches.open();await click('batch');
  assert.match($('rb-body').innerHTML,/data-rb="void"/);
  assert.match($('rb-body').innerHTML,/data-rb="confirm-paid"/);
  env.answer(false);await click('void');
  assert.equal(env.requests.filter(r=>r.method==='POST').length,0);
  assert.doesNotMatch(env.confirmations[0].message,/原因/);
  env.answer(true);env.fail();await click('void');
  assert.equal(env.saved.status,'pending');assert.match(env.toasts.at(-1)[0],/保存失败/);
  assert.match($('rb-body').innerHTML,/data-rb="void"/);
  await click('void');
  assert.match($('rb-body').innerHTML,/选择奖励发放日期/);
  assert.equal($('rb-date-selected-count').textContent,'已选 1 个日期');
  assert.match($('rb-sources').innerHTML,/data-rb-task="synthetic-task"\s+\s*>/,'released source is selectable and unchecked');
  assert.match($('rb-body').innerHTML,/rb-status--voided/);
  await click('batch');
  assert.doesNotMatch($('rb-body').innerHTML,/href="[^\"]+\/download"|data-rb="copy-txt"|data-rb="confirm-paid"/);
  await click('copy-txt');assert.equal(env.copies.length,0);
  env.setSaved(fixture());await click('batch');
  let release;env.hold(new Promise(resolve=>{release=resolve;}));
  const pending=click('confirm-paid');
  await new Promise(resolve=>setImmediate(resolve));
  const count=env.requests.filter(r=>r.method==='POST').length;
  await click('void');await click('confirm-paid');
  assert.equal(env.requests.filter(r=>r.method==='POST').length,count,'opposing/duplicate clicks are ignored in flight');
  release();await pending;env.hold(null);
  assert.equal(env.saved.status,'paid');
  assert.match($('rb-body').innerHTML,/rb-status--paid/);
  assert.doesNotMatch($('rb-body').innerHTML,/data-rb="void"|data-rb="confirm-paid"/);
  await click('history');assert.match($('rb-body').innerHTML,/已发放/,'history updates without reload');
  env.setSaved({...fixture(),status:undefined,revision:undefined});env.allow(false);await click('batch');
  assert.match($('rb-body').innerHTML,/待发放/);
  assert.doesNotMatch($('rb-body').innerHTML,/data-rb="void"|data-rb="confirm-paid"/);
  const before=env.requests.length;await click('void');assert.equal(env.requests.length,before);
  env.allow(true);env.setSaved({...fixture(),owner_name:'<img src=x>',paid_by:'<script>bad</script>',status:'paid'});await click('batch');
  assert.doesNotMatch($('rb-body').innerHTML,/<img|<script>/);
  console.log('Reward batch lifecycle UI passed: confirmation/cancel, no reason, failure/retry, dates retained, claims released, void TXT disabled, paid history, duplicate clicks, read-only, legacy and escaping.');
}
function serve(){
  const http=require('node:http');
  const html=`<!doctype html><html lang="zh-CN" data-theme="light"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>奖励批次生命周期 · 隔离合成验证</title><link rel="stylesheet" href="/style.css"><style>html,body{height:auto;overflow:auto}body{padding:24px}.qa-panel{padding:24px;background:var(--surface);border:1px solid var(--border);border-radius:14px;margin-top:20px}.qa-tools{display:flex;gap:8px;flex-wrap:wrap}@media(max-width:700px){body{padding:8px}.qa-panel{padding:12px}}</style><body><p>隔离合成验证：不连接游戏、飞书、模型或真实数据。</p><div class="qa-tools"><button class="btn btn--ghost" id="qa-theme">切换明暗主题</button><button class="btn btn--ghost" id="qa-reset">重置合成批次</button><a href="/?state=paid">已发放样例</a><a href="/?state=voided">已作废样例</a><a href="/?state=paid&theme=dark">暗色已发放样例</a></div><section class="qa-panel" id="reward-batches-panel"></section><section class="qa-panel" id="group-tasks-panel"></section><script>
    const batchSeed=${JSON.stringify(fixture())},taskSeed=${JSON.stringify(taskFixture())};let saved=structuredClone(batchSeed);
    window.$=id=>document.getElementById(id);window.esc=${escapeHtml.toString()};window.PlatformPermissions={canUse:()=>true};
    window.workflowTime=value=>new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+'（北京时间）';
    window.showToast=message=>{document.getElementById('qa-notice').textContent=message;};
    window.workflowConfirm=(message,title)=>new Promise(resolve=>{
      const dialog=document.createElement('dialog');dialog.className='workflow-dialog';dialog.innerHTML='<h2>'+esc(title)+'</h2><p style="white-space:pre-line">'+esc(message)+'</p><div class="workflow-toolbar"><button class="btn btn--ghost" id="qa-cancel">取消</button><button class="btn btn--primary" id="qa-confirm">确认</button></div>';document.body.append(dialog);
      const finish=value=>{dialog.close();dialog.remove();resolve(value);};dialog.querySelector('#qa-cancel').onclick=()=>finish(false);dialog.querySelector('#qa-confirm').onclick=()=>finish(true);dialog.oncancel=e=>{e.preventDefault();finish(false);};dialog.showModal();
    });
    window.workflowRequest=async(url,options={})=>{
      if(options.method==='POST'){
        if(saved.status!=='pending'||options.body.expected_revision!==saved.revision)throw Error('合成批次状态已变化');
        const action=url.endsWith('/void')?'voided':'paid';saved={...saved,status:action,revision:2,paid_at:new Date().toISOString(),paid_by:'合成发奖人',voided_at:new Date().toISOString(),voided_by:'合成研究员'};
        setTimeout(()=>GroupTasks.list(),0);return structuredClone(saved);
      }
      const task=structuredClone(taskSeed);
      if(saved.status==='paid')task.settlement={version:6,changed:false,paid_at:saved.paid_at,paid_by:saved.paid_by,batch_id:saved.id};
      if(url==='/api/group-tasks')return {tasks:[task]};
      if(url.endsWith('/choices'))return {tasks:[{...task,selectable:saved.status==='voided',consumed:saved.status==='voided'?null:saved.id}],other_rewards:[],imports:[],groups:[],batches:[structuredClone(saved)]};
      if(url==='/api/reward-batches/'+saved.id)return structuredClone(saved);
      throw Error('未配置的合成请求 '+url);
    };
    $('qa-theme').onclick=()=>document.documentElement.dataset.theme=document.documentElement.dataset.theme==='light'?'dark':'light';
    $('qa-reset').onclick=()=>{saved=structuredClone(batchSeed);RewardBatches.open({home:true});GroupTasks.list();};
    </script><p id="qa-notice" role="status"></p><script src="/reward-batches.js"></script><script src="/group-tasks.js"></script><script>RewardBatches.open();GroupTasks.list();</script></body></html>`;
  const server=http.createServer((req,res)=>{
    const routes={'/':[html,'text/html; charset=utf-8'],'/style.css':[fs.readFileSync(path.join(root,'static/style.css')),'text/css'],
      '/reward-batches.js':[source,'text/javascript; charset=utf-8'],'/group-tasks.js':[fs.readFileSync(path.join(root,'static/js/features/group-tasks.js')),'text/javascript; charset=utf-8']};
    const location=new URL(req.url,'http://127.0.0.1');
    if(location.pathname==='/'){
      const seed=fixture(),state=location.searchParams.get('state');
      if(['paid','voided'].includes(state))Object.assign(seed,{status:state,revision:2,paid_by:'合成发奖人',paid_at:'2026-10-30T07:00:00Z',voided_by:'合成研究员',voided_at:'2026-10-30T07:00:00Z'});
      let body=html.replace('let saved=structuredClone(batchSeed);','let saved='+JSON.stringify(seed)+';');
      if(location.searchParams.get('theme')==='dark')body=body.replace('data-theme="light"','data-theme="dark"');
      routes['/']=[body,'text/html; charset=utf-8'];
    }
    const value=routes[location.pathname];if(!value){res.writeHead(404);res.end();return;}res.writeHead(200,{'Content-Type':value[1]});res.end(value[0]);
  });
  server.listen(0,'127.0.0.1',()=>console.log('Synthetic reward lifecycle preview: http://127.0.0.1:'+server.address().port));
}
function browserShell(){
  // Real API validation shell: all helpers and feature handlers come from production files.
  return `<!doctype html><html lang="zh-CN" data-theme="light"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>奖励批次 · 真实路由隔离验证</title><link rel="stylesheet" href="/static/style.css"><style>html,body{height:auto;overflow:auto}body{padding:24px}.qa-panel{padding:24px;background:var(--surface);border:1px solid var(--border);border-radius:14px;margin-top:20px}.qa-tools{display:flex;gap:8px;flex-wrap:wrap}.workflow-confirm-message{white-space:pre-line}@media(max-width:700px){body{padding:8px}.qa-panel{padding:12px}}</style><body><p>真实路由隔离验证：仅合成任务，名单与登录替身；模型及外部网络禁用。</p><nav class="qa-tools"><button class="btn btn--ghost" id="qa-rewards">奖励整理</button><button class="btn btn--ghost" id="qa-quality">质量标注</button><button class="btn btn--ghost" id="qa-theme">切换明暗主题</button></nav><p id="qa-notice" role="status"></p><section class="qa-panel" id="reward-batches-panel"></section><section class="qa-panel panel--hidden" id="group-tasks-panel"></section><script>
    window.$=id=>document.getElementById(id);window.esc=${escapeHtml.toString()};
    window.showToast=(message,type)=>{$('qa-notice').textContent=message;$('qa-notice').dataset.type=type;};
    window.formatReportDuration=String;
    </script><script src="/static/js/core/feature-permissions.js"></script><script>PlatformPermissions.apply({permission_levels:{reward:'use',annotate:'use'},feature_catalog:[]});</script><script src="/static/js/features/reward-batches.js"></script><script src="/static/js/features/group-tasks.js"></script><script>
    $('qa-rewards').onclick=()=>{$('group-tasks-panel').classList.add('panel--hidden');RewardBatches.open({home:true});};
    $('qa-quality').onclick=()=>{$('reward-batches-panel').classList.add('panel--hidden');GroupTasks.open({home:true});};
    $('qa-theme').onclick=()=>document.documentElement.dataset.theme=document.documentElement.dataset.theme==='light'?'dark':'light';
    RewardBatches.open();
    </script></body></html>`;
}
if(process.argv.includes('--browser-shell'))process.stdout.write(browserShell());
else checks().then(()=>{if(process.argv.includes('--serve'))serve();}).catch(error=>{console.error(error);process.exitCode=1;});
