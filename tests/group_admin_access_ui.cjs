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

(async()=>{
  await settingsChecks();await taskScopeRaceChecks();await rewardScopeRaceChecks();await rewardScopeRaceChecks(true);
  console.log('PASS: admin-only settings, saved scope restoration, cancellation/failure handling, cache/selection clearing, and stale task/reward responses.');
})().catch(error=>{console.error(error);process.exitCode=1;});
