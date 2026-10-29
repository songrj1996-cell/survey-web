// Production handlers and deferred responses, using synthetic state and no network.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=name=>fs.readFileSync(path.join(__dirname,'../static/js/features',name),'utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function environment(){
  const nodes=new Map(),events=new Map(),toasts=[],requests=[];
  class Element{
    constructor(){this.events={};this.dataset={};this.style={};this.children=new Map();this.disabled=false;this.checked=false;this.value='';this.html='';this.classes=new Set();
      this.classList={add:name=>this.classes.add(name),remove:name=>this.classes.delete(name),contains:name=>this.classes.has(name),toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)};}
    set innerHTML(value){this.html=value;for(const match of value.matchAll(/<input\b([^>]+)>/g)){
      const id=/id="([^"]+)"/.exec(match[1])?.[1];if(!id)continue;
      const input=new Element();input.id=id;input.checked=/\bchecked\b/.test(match[1]);input.dataset.revision=/data-revision="([^"]+)"/.exec(match[1])?.[1];nodes.set(id,input);
    }}
    get innerHTML(){return this.html;}
    addEventListener(type,handler){this.events[type]=handler;}
    querySelectorAll(){return [];}
    querySelector(selector){if(!this.children.has(selector))this.children.set(selector,new Element());return this.children.get(selector);}
    insertAdjacentHTML(where,value){this.innerHTML+=value;}
    setAttribute(){}
    closest(){return this;}
  }
  const $=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const context={console,$,esc:String,workflowTime:String,Event:class{constructor(type){this.type=type;}},
    document:{hidden:false,querySelectorAll:()=>[]},PlatformPermissions:{canUse:()=>true},
    localStorage:{setItem(){}},setInterval(){},showToast:(message,type)=>toasts.push({message,type}),
    addEventListener:(type,handler)=>{if(!events.has(type))events.set(type,[]);events.get(type).push(handler);},
    dispatchEvent:event=>{for(const handler of events.get(event.type)||[])handler(event);},
  };
  context.window=context;vm.createContext(context);
  return {context,$,nodes,events,toasts,requests};
}

async function settingsChecks(){
  const env=environment();let saved={only_self:false,revision:0},fail=false,allowLeave=true;
  env.context.groupWorkflowMayLeave=async()=>allowLeave;
  env.context.fetch=async(url,options={})=>{
    env.requests.push({url,...options});
    if(options.method==='PATCH'){
      if(fail)throw Error('合成网络中断');
      const body=JSON.parse(options.body);assert.equal(url,'/api/group-management/preferences');
      assert.equal(body.expected_revision,saved.revision);saved={only_self:body.only_self,revision:saved.revision+1};
      return {ok:true,json:async()=>({...saved})};
    }
    return {ok:true,json:async()=>({group_management_preferences:saved})};
  };
  const js=source('settings.js');
  const extract=name=>{const match=js.match(new RegExp('async function '+name+'\\([^)]*\\) \\{[\\s\\S]*?^\\}', 'm'));assert(match);return match[0];};
  vm.runInContext(extract('loadSystemSettings')+'\n'+extract('saveGroupManagementScope'),env.context);
  await env.context.loadSystemSettings();
  assert.match(env.$('stab-content-system').innerHTML,/群组管理 · 个人查看范围/);
  let input=env.$('setting-group-only-self');assert.equal(input.checked,false);
  input.checked=true;await env.context.saveGroupManagementScope(input);
  assert.equal(input.checked,true);assert.equal(input.disabled,false);assert.equal(saved.only_self,true);
  assert.equal(input.dataset.revision,'1');
  assert.deepEqual(JSON.parse(env.requests.at(-1).body),{only_self:true,expected_revision:0});
  assert.match(env.toasts.at(-1).message,/仅看自己/);
  await env.context.loadSystemSettings();input=env.$('setting-group-only-self');assert.equal(input.checked,true);
  allowLeave=false;const before=env.requests.length;input.checked=false;await env.context.saveGroupManagementScope(input);
  assert.equal(env.requests.length,before);assert.equal(input.checked,true,'cancel keeps the saved scope and unsaved draft');
  allowLeave=true;fail=true;input.checked=false;await env.context.saveGroupManagementScope(input);
  assert.equal(input.checked,true);assert.equal(input.disabled,false);assert.equal(saved.only_self,true);
  assert.match(env.toasts.at(-1).message,/失败.*重新打开设置核对/);
  env.context.fetch=async()=>({ok:true,json:async()=>({})});await env.context.loadSystemSettings();
  assert(!env.$('stab-content-system').innerHTML.includes('setting-group-only-self'),'ordinary members have no toggle');
}

function listTask(id){return {id,rules:{name:id},owner_name:'合成操作者',status:'ready',can_edit:true,
  summary:{player_count:1,feedback_count:1,complete_count:1}};}

async function taskScopeRaceChecks(){
  const env=environment();let resolveOld;
  env.context.workflowRequest=()=>new Promise(resolve=>{resolveOld=resolve;});
  vm.runInContext(source('group-tasks.js').replace('return {open,list};','return {open,list,tests:{annState}};'),env.context);
  const cached=env.context.GroupTasks.tests.annState;
  cached.aiResults=[{id:'private-ai'}];cached.qualityResults=[{id:'private-quality'}];cached.headers=['private-header'];
  const pending=env.context.GroupTasks.list();
  const root=env.$('group-tasks-panel');root.innerHTML='他人的旧任务详情';root.classList.add('panel--hidden');
  env.context.dispatchEvent(new env.context.Event('group-management-scope-changed'));
  assert.equal(root.innerHTML,'','scope change immediately clears the old detail');
  assert.equal(cached.aiResults.length,0);assert.equal(cached.qualityResults.length,0);assert.equal(cached.headers.length,0);
  resolveOld({tasks:[listTask('SHOULD-NOT-RETURN')]});await pending;
  assert.equal(root.innerHTML,'','an old response cannot repaint hidden records');
  assert.equal(env.toasts.length,0,'obsolete requests do not emit misleading errors');
}

async function rewardScopeRaceChecks(rejectOld=false){
  const env=environment();let resolveOld,reject;
  env.context.workflowRequest=()=>new Promise((resolve,fail)=>{resolveOld=resolve;reject=fail;});
  vm.runInContext(source('reward-batches.js').replace('return {open,acceptImport};',
    `return {open,acceptImport,tests:{seed(){selectedTasks.add('other-task');selectedImports.add('other-import');preview={id:'old-preview'};batch={id:'old-batch'};generationKey='old-key';},
    state(){return {tasks:[...selectedTasks],imports:[...selectedImports],preview,batch,generationKey};}}};`),env.context);
  const pending=env.context.RewardBatches.open();env.context.RewardBatches.tests.seed();
  const root=env.$('reward-batches-panel');root.innerHTML='他人的旧奖励记录';root.classList.add('panel--hidden');
  env.context.dispatchEvent(new env.context.Event('group-management-scope-changed'));
  assert.equal(root.innerHTML,'');
  const state=env.context.RewardBatches.tests.state();
  assert.equal(state.tasks.length,0);assert.equal(state.imports.length,0);assert.equal(state.preview,null);assert.equal(state.batch,null);assert.equal(state.generationKey,null);
  if(rejectOld)reject(Object.assign(Error('旧请求错误'),{detail:{preview:{id:'private-old-preview'}}}));
  else resolveOld({tasks:[],imports:[],other_rewards:[],batches:[],groups:[]});
  await pending;await flush();
  assert.equal(root.innerHTML,'','neither late success nor late failure can restore the old preview');
  assert.equal(env.toasts.length,0);
}

async function memberDeleteBrowserChecks(){
  const dataDir=process.env.DATA_DIR,project=path.resolve(__dirname,'..');
  assert(dataDir&&path.resolve(dataDir).startsWith(project+path.sep),'Member browser checks require isolated DATA_DIR');
  const {chromium}=require('playwright'),{spawn}=require('node:child_process');
  const socket=require('node:net').createServer();await new Promise(resolve=>socket.listen(0,'127.0.0.1',resolve));
  const port=socket.address().port;await new Promise(resolve=>socket.close(resolve));
  const server=spawn('python',['-c',`import sys;sys.path.insert(0,'tests');import uvicorn;from test_platform_permissions import make_member_delete_ui_app;uvicorn.run(make_member_delete_ui_app(),host='127.0.0.1',port=${port},log_level='error')`],
    {cwd:project,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONIOENCODING:'utf-8'},windowsHide:true,stdio:['ignore','pipe','pipe']});
  let output='',browser;server.stdout.on('data',v=>output+=v);server.stderr.on('data',v=>output+=v);
  const base=`http://127.0.0.1:${port}`,checks=[],errors=[],external=[];
  try{
    let ready=false;
    for(let i=0;i<100;i++){
      if(server.exitCode!==null)throw Error('Member fixture exited: '+output);
      try{if((await fetch(base+'/api/admin/users')).ok){ready=true;break;}}catch{}
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    assert(ready,'Member fixture startup failed: '+output);
    browser=await chromium.launch({headless:true,executablePath:process.env.MEMBER_TEST_BROWSER});
    const page=await browser.newPage({viewport:{width:1024,height:1000}});page.setDefaultTimeout(5000);
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',route=>{if(route.request().url().startsWith(base+'/'))return route.continue();external.push(route.request().url());return route.abort();});
    await page.goto(base);
    await page.locator('[data-member="admin@example.test"]').click();
    assert.equal(await page.locator('#member-delete').count(),0,'protected administrator has no delete button');
    await page.getByRole('button',{name:'新增成员',exact:true}).click();
    assert.equal(await page.locator('#member-delete').count(),0,'unsaved member has no delete button');
    await page.getByRole('button',{name:'撤销修改',exact:true}).click();
    await page.locator('[data-member="member@example.test"]').click();
    await page.locator('#member-name').fill('未保存的名字');
    const original=fs.readFileSync(path.join(dataDir,'whitelist.json'),'utf8');
    await page.locator('#member-delete').click();
    const dialog=()=>page.getByRole('dialog',{name:'确认当前操作'});
    assert.match(await dialog().innerText(),/权限变更历史/);assert.match(await dialog().innerText(),/未保存的修改也会放弃/);
    await dialog().locator('[data-answer="stay"]').click();
    assert.equal(await page.locator('#member-name').inputValue(),'未保存的名字');
    assert.equal(fs.readFileSync(path.join(dataDir,'whitelist.json'),'utf8'),original);
    // The request fails at transport level: draft and member stay editable.
    await page.route('**/api/admin/users/*',route=>route.request().method()==='DELETE'?route.abort():route.continue());
    await page.locator('#member-delete').click();await dialog().locator('[data-answer="continue"]').click();
    await page.locator('#test-toast').filter({hasText:'刷新成员列表核对结果'}).waitFor();
    assert.equal(await page.locator('#member-name').inputValue(),'未保存的名字');
    assert.equal(await page.locator('#member-delete').isEnabled(),true);
    assert.equal(fs.readFileSync(path.join(dataDir,'whitelist.json'),'utf8'),original);
    await page.unroute('**/api/admin/users/*');
    // Another administrator saves a newer revision while this editor remains open.
    const changed=await fetch(base+'/api/admin/users/member@example.test',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'远端更新成员',expected_revision:1})});
    assert.equal(changed.status,200,await changed.text());
    await page.locator('#member-delete').click();await dialog().locator('[data-answer="continue"]').click();
    await page.locator('#test-toast').filter({hasText:'HTTP 409'}).waitFor();
    assert.equal(await page.locator('#member-name').inputValue(),'未保存的名字');
    assert(JSON.parse(fs.readFileSync(path.join(dataDir,'whitelist.json'),'utf8')).users.some(u=>u.name==='远端更新成员'));
    checks.push('受保护管理员与新增成员无删除入口；取消、网络失败、过期版本均保留成员与草稿');
    await page.locator('#member-cancel').click();
    await page.reload();
    await page.locator('[data-member="member@example.test"]').click();
    for(const width of [1024,390,320]){
      await page.setViewportSize({width,height:1000});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`members ${width}px overflow`);
    }
    await page.evaluate(()=>document.documentElement.dataset.theme='dark');
    await page.waitForFunction(()=>getComputedStyle(document.body).color==='rgb(226, 232, 240)');
    await page.screenshot({path:path.join(dataDir,'member-delete-narrow.png'),fullPage:true});
    const history=path.join(dataDir,'history.json');fs.writeFileSync(history,'{"synthetic":"saved-report"}');
    const taskFile=path.join(dataDir,'retained-task.json');fs.writeFileSync(taskFile,'{"synthetic":"saved-task"}');
    let release;const pending=new Promise(resolve=>{release=resolve;});let deletions=0;
    await page.route('**/api/admin/users/*',async route=>{if(route.request().method()==='DELETE'){deletions++;await pending;}await route.continue();});
    await page.locator('#member-delete').click();await dialog().locator('[data-answer="continue"]').click();
    await page.waitForFunction(()=>document.querySelector('#member-delete')?.textContent==='正在删除…');
    assert.equal(await page.locator('#member-delete').isDisabled(),true);
    await page.evaluate(()=>document.querySelector('#member-delete').click());
    assert.equal(deletions,1,'duplicate click must not send another DELETE');release();
    await page.locator('#test-toast').filter({hasText:'成员权限条目已删除'}).waitFor();
    assert.equal(await page.locator('[data-member="member@example.test"]').count(),0);
    assert.equal(await page.locator('[data-member="ou_synthetic"]').count(),1);
    assert.equal(fs.readFileSync(history,'utf8'),'{"synthetic":"saved-report"}');
    assert.equal(fs.readFileSync(taskFile,'utf8'),'{"synthetic":"saved-task"}');
    const audit=JSON.parse(fs.readFileSync(path.join(dataDir,'audit_logs.json'),'utf8'));
    const entries=Array.isArray(audit)?audit:audit.logs;
    const deleted=entries.find(e=>e.action==='删除成员');
    assert(deleted);assert.match(deleted.detail,/远端更新成员/);assert(!deleted.detail.includes('member@example.test'));
    await page.locator('[data-member="ou_synthetic"]').click();
    await page.locator('#member-delete').click();await dialog().locator('[data-answer="continue"]').click();
    await page.locator('[data-member="ou_synthetic"]').waitFor({state:'detached'});
    checks.push('1024/390/320px及深色无溢出；删除防重复，启用/停用成员均真正移除，报告任务保留，日志按姓名记录');
    assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    console.log(JSON.stringify({memberBrowser:true,checks,pageErrors:errors,externalRequests:external},null,2));
  }finally{
    if(browser)await browser.close();server.kill();
    await new Promise(resolve=>server.exitCode!==null?resolve():server.once('exit',resolve));
  }
}

(async()=>{
  await settingsChecks();await taskScopeRaceChecks();await rewardScopeRaceChecks();await rewardScopeRaceChecks(true);
  console.log('PASS: admin-only settings, saved scope restoration, cancellation/failure handling, cache/selection clearing, and stale task/reward responses.');
  if(process.env.MEMBER_TEST_BROWSER)await memberDeleteBrowserChecks();
})().catch(error=>{console.error(error);process.exitCode=1;});
