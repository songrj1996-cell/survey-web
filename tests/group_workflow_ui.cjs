// Run with node tests/group_workflow_ui.cjs. No network, browser or third-party dependency.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..');
class Element {
  constructor(tag='div'){this.tag=tag;this.isConnected=true;this.children=[];this.dataset={};this.checked=false;this.indeterminate=false;this.disabled=false;this.style={};this.listeners={};this.value='';this.classList={add(){},remove(){},toggle(){},contains(){return false;}};}
  append(...items){this.children.push(...items.filter(item=>typeof item!=='string'));}
  replaceChildren(...items){this.children=items;}
  querySelectorAll(selector){const out=[];const walk=node=>{for(const item of node.children){if(selector==='[data-permission]'&&item.dataset.permission||selector==='[data-permission]:checked'&&item.dataset.permission&&item.checked)out.push(item);walk(item);}};walk(this);return out;}
  querySelector(selector){
    if(!['.panel__header','.panel__desc','.ann-done-block','.step-bar'].includes(selector))return null;
    this.queries=this.queries||new Map();
    if(!this.queries.has(selector))this.queries.set(selector,new Element());
    return this.queries.get(selector);
  }
  setAttribute(name,value){this[name]=value;}
  removeAttribute(name){delete this[name];}
  insertAdjacentHTML(position,html){this.innerHTML=(this.innerHTML||'')+html;}
  closest(){return this;}
  addEventListener(event,callback){this.listeners[event]=callback;}
}
const document={createElement:tag=>new Element(tag),querySelectorAll:()=>[],getElementById:()=>null};
const nav=new Element();document.querySelector=selector=>selector==='.sidebar__nav'?nav:null;
let response={ok:true,json:async()=>({})};
const context={window:{},document,FormData:class{},fetch:async()=>response,console};
vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(root,'static/js/core/feature-permissions.js'),'utf8'),context);
const permissions=context.window.PlatformPermissions;
permissions.apply({is_admin:true,feature_catalog:[{key:'groups',label:'群组管理',children:[{key:'annotate',label:'质量标注'},{key:'reward',label:'奖励整理'},{key:'roster',label:'名单来源'}]}],permission_levels:{annotate:'read',reward:'use'}});
assert.equal(permissions.allowed('annotate'),true);assert.equal(permissions.canUse('annotate'),false);assert.equal(permissions.canUse('reward'),true);assert.equal(permissions.allowed('roster'),false);
const editor=new Element();permissions.tree(editor,[]);
const fieldset=editor.children[0],parent=fieldset.children[0].children[0].children[0];
const leaves=fieldset.querySelectorAll('[data-permission]');
assert.equal(leaves.filter(x=>x.checked).length,0,'new members have no initial grants');
parent.checked=true;parent.onchange();assert.equal(leaves.filter(x=>x.checked).length,3);
leaves[2].checked=false;leaves[2].onchange();assert.equal(parent.indeterminate,true);assert.equal(parent.checked,false);
assert.deepEqual(Array.from(permissions.selected(editor)),['annotate','reward']);
permissions.tree(editor,[],true);assert.equal(editor.querySelectorAll('[data-permission]').every(x=>x.disabled),true);

// These tests exercise the real delegated handlers with deferred in-memory responses.
async function verifyRuleEditSafety() {
  const clone=value=>JSON.parse(JSON.stringify(value));
  const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};
  async function fixture(saved={}) {
    const state={requests:[],toasts:[],confirmations:0,windowEvents:{},poll:null,gate:null,failure:null,
      task:{id:'synthetic',version:1,status:'draft',can_edit:true,owner_name:'合成操作者',workflow:{max_step:1},
        summary:{complete_count:0,feedback_count:0},annotation:{complete:false},
        rules:{name:'',announcement:'合成公告',payout_date:'2026-10-30',base_reward:null,excellent_reward:null,
          requires_count:false,min_count:null,allow_repeat:false,ai_detect:false,group_source_ids:[]},...saved},
      suggestions:{name:{value:'识别旧名',evidence:'合成公告'},base_reward:{value:100,evidence:'100'},
        requires_count:{value:true,evidence:'要求次数'},allow_repeat:{value:true,evidence:'允许重复'}}};
    const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
    const panel=$('group-tasks-panel'),fields=new Map(Object.keys(state.task.rules).filter(key=>key!=='group_source_ids').map(key=>{
      const field=new Element('input');field.dataset.rule=key;
      field.matches=selector=>selector==='input,textarea,select';return[key,field];
    })),hints=new Map(),buttons=new Map();
    const readonly=new Element('input');readonly.disabled=true;
    const button=name=>{if(!buttons.has(name)){const el=new Element('button');el.dataset.gw=name;buttons.set(name,el);}return buttons.get(name);};
    panel.querySelector=selector=>{
      const rule=selector.match(/data-rule="([^"]+)"/);if(rule)return fields.get(rule[1]);
      const hint=selector.match(/data-suggestion="([^"]+)"/);if(hint){if(!hints.has(hint[1]))hints.set(hint[1],new Element());return hints.get(hint[1]);}return null;
    };
    panel.querySelectorAll=selector=>selector==='[data-rule]'?[...fields.values()]:
      selector==='input,textarea,select,button'?[...fields.values(),...buttons.values(),readonly]:[];
    const ui={window:{addEventListener:(name,fn)=>state.windowEvents[name]=fn,
      workflowConfirm:async()=>{state.confirmations++;return false;},workflowRequest:async(url,options={})=>{
        state.requests.push({url,method:options.method||'GET',body:options.body?clone(options.body):null});
        if(url.endsWith('/groups'))return{groups:[]};
        if(url.endsWith('/announcement'))return{suggestions:clone(state.suggestions)};
        if(url.endsWith('/rules')||url.endsWith('/settings')){
          if(state.gate){state.gate.started.resolve();await state.gate.response.promise;}
          if(state.failure)throw state.failure;
          state.task={...state.task,version:state.task.version+1,rules:url.endsWith('/rules')?clone(options.body.rules):
            {...state.task.rules,...clone(options.body)}};
          delete state.task.rules.expected_version;return clone(state.task);
        }
        return clone(state.task);
      }},document:{hidden:false,createElement:tag=>new Element(tag)},$,esc:String,
      workflowTime:context.window.workflowTime,showToast:(...args)=>state.toasts.push(args),
      PlatformPermissions:permissions,setInterval:fn=>state.poll=fn,console};
    vm.createContext(ui);vm.runInContext(fs.readFileSync(path.join(root,'static/js/features/group-tasks.js'),'utf8'),ui);
    const click=async(name,extra={})=>{const el=button(name);Object.assign(el.dataset,extra);
      await panel.listeners.click({target:{closest:selector=>selector==='[data-gw]'?el:null}});};
    const syncFields=()=>{for(const[key,field]of fields)field.value=String(state.task.rules[key]??'');};
    const input=(key,value)=>{const field=fields.get(key);assert.equal(field.disabled,false,'editing requires an unlocked field');field.value=String(value);panel.listeners.input({target:field});};
    const hold=()=>{state.gate={started:deferred(),response:deferred()};return state.gate;};
    syncFields();await click('view',{id:'synthetic'});
    return{state,ui,fields,hints,buttons,readonly,$,click,input,hold,syncFields};
  }
  for(const action of ['save-rules','save-settings']) {
    const f=await fixture();if(action==='save-settings')await f.click('settings',{id:'synthetic'});
    f.input('name','手改新名');f.input('base_reward',0);f.input('excellent_reward',0);
    f.buttons.set('extract',new Element('button'));f.buttons.get('extract').dataset.gw='extract';
    const gate=f.hold(),saving=f.click(action);await gate.started.promise;
    assert([...f.fields.values()].every(field=>field.disabled),'every rule field is locked before awaiting save');
    assert.equal(f.buttons.get('extract').disabled,true);
    assert.match(f.$('gw-rule-save-status').textContent,/正在保存.*UTC\+8/);
    const count=f.state.requests.length;
    await f.click(action);await f.click('back');await f.click('extract');await f.ui.window.GroupTasks.list();await f.ui.window.GroupTasks.open({home:true});
    assert.equal(f.state.requests.length,count,'pending save prevents duplicate requests and switching');
    assert.equal(await f.ui.window.groupWorkflowMayLeave(),false);
    gate.response.resolve();await saving;f.state.gate=null;
    assert.equal(f.state.task.rules.name,'手改新名');assert.equal(f.state.task.rules.base_reward,0);
    assert([...f.fields.values()].every(field=>!field.disabled),'success unlocks controls');
    assert.equal(f.readonly.disabled,true,'pre-existing disabled state is preserved');
    assert.equal(f.ui.window.groupWorkflowHasDraft(),false);
    assert.match(f.$('gw-rule-save-status').textContent,/已保存.*UTC\+8/);
    for(const status of [409,422,500,null]) {
      f.input('name','失败时保留原文');f.state.failure=Object.assign(new Error(status?'合成保存失败':'合成网络超时'),status?{status}:{});
      const failures=f.state.toasts.length;await f.click(action);
      assert.equal(f.fields.get('name').value,'失败时保留原文');assert.equal(f.fields.get('name').disabled,false);
      assert.equal(f.ui.window.groupWorkflowHasDraft(),true);assert.equal(f.readonly.disabled,true);
      const notice=f.$('gw-rule-save-status').textContent;
      assert.match(notice,/未能确认保存成功.*填写内容已保留.*UTC\+8/);
      assert(notice.includes('/api/group-tasks/synthetic/'));if(status)assert(notice.includes('HTTP '+status));
      assert(f.state.toasts.slice(failures).every(([,type])=>type==='error'));
    }
    f.state.failure=null;await f.click(action);assert.equal(f.ui.window.groupWorkflowHasDraft(),false,'successful retry clears draft');
  }
  const initial=await fixture();await initial.click('extract');
  assert.equal(initial.fields.get('name').value,'识别旧名');assert.equal(initial.fields.get('requires_count').value,'true','untouched false defaults can be prefilled initially');
  initial.input('name','已保存手工名');initial.input('base_reward',0);initial.input('requires_count','false');initial.input('allow_repeat','false');
  await initial.click('save-rules'); // Partial configuration: excellent_reward remains null.
  const savedManualRules=clone(initial.state.task);
  for(const reopen of [false,true]) {
    const f=reopen?await fixture(savedManualRules):initial;
    f.syncFields();await f.click('extract');
    assert.equal(f.fields.get('name').value,'已保存手工名');assert.equal(f.fields.get('base_reward').value,'0');
    assert.equal(f.fields.get('requires_count').value,'false');assert.equal(f.fields.get('allow_repeat').value,'false');
    assert.match(f.$('gw-extraction-status').textContent,/原值已保留.*采用后请保存.*UTC\+8/);
    f.hints.get('name').children.at(-1).onclick();assert.equal(f.fields.get('name').value,'识别旧名');
    await f.click('save-rules');assert.equal(f.state.task.rules.name,'识别旧名','adopted suggestion persists only after explicit save');
  }
  const saved=await fixture({version:2,rules:{...initial.state.task.rules,excellent_reward:0}});
  saved.state.suggestions={excellent_reward:{value:20,evidence:'优秀20'}};await saved.click('extract');
  assert.equal(saved.fields.get('excellent_reward').value,'0','saved zero is protected');
  const blank=await fixture({version:2,rules:{...initial.state.task.rules,excellent_reward:null}});
  blank.state.suggestions={excellent_reward:{value:20,evidence:'优秀20'}};await blank.click('extract');
  assert.equal(blank.fields.get('excellent_reward').value,'20','an unfilled saved field is still prefilled');
  console.log('Rule edit safety passed: both save locks, duplicate/switch prevention, success/retry, HTTP 409/422/500 and timeout recovery, partial-save/reopen protection, explicit adoption, zero/false and first extraction.');
}

(async()=>{
  await verifyRuleEditSafety();
  response={ok:false,status:409,json:async()=>({detail:{message:'名单发生变化',preview:{id:'review-again'}}})};
  await assert.rejects(context.window.workflowRequest('/synthetic'),err=>err.status===409&&err.detail.preview.id==='review-again');
  const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const fields=new Map(['name','announcement','payout_date','base_reward','excellent_reward','requires_count','min_count','allow_repeat','ai_detect'].map(key=>[key,Object.assign(new Element('input'),{dataset:{rule:key},matches:()=>true})]));
  fields.get('name').value='手工名称';fields.get('announcement').value='公告原文';
  const hints=new Map();const panel=$('group-tasks-panel');
  panel.querySelector=selector=>{const rule=selector.match(/data-rule="([^"]+)"/);if(rule)return fields.get(rule[1]);const suggestion=selector.match(/data-suggestion="([^"]+)"/);if(suggestion){if(!hints.has(suggestion[1]))hints.set(suggestion[1],new Element());return hints.get(suggestion[1]);}return null;};
  panel.querySelectorAll=()=>[];
  const task={id:'task',rules:{name:'已存名称'},summary:{complete_count:0,feedback_count:0},owner_name:'合成操作者',version:1,status:'draft',can_edit:true};
  let poll;const requests=[],toasts=[],clipboardWrites=[];let fetchedTask=task,requestHook,actionPanel;
  const ui={window:{addEventListener(){},workflowConfirm:async()=>false,workflowRequest:async(url,options)=>{requests.push({url,options});if(requestHook){const handled=await requestHook(url,options);if(handled!==undefined)return handled;}if(url.endsWith('/groups'))return {groups:[{id:'group_a',name:'群甲'},{id:'group_b',name:'群乙'}]};if(url.endsWith('/announcement'))return{suggestions:{name:{value:'识别名称',evidence:'公告'},base_reward:{value:100,evidence:'100'}},warnings:[]};return fetchedTask;}},document:{hidden:false,createElement:tag=>new Element(tag)},$,esc:x=>String(x??''),workflowTime:context.window.workflowTime,showToast:(...args)=>toasts.push(args),confirm:()=>false,setInterval:fn=>{poll=fn;},PlatformPermissions:permissions,console,Blob,navigator:{clipboard:{writeText:async text=>clipboardWrites.push(text)}}};
  ui.formatReportDuration=String;
  vm.createContext(ui);vm.runInContext(fs.readFileSync(path.join(root,'static/js/features/group-tasks.js'),'utf8'),ui);
  const click=async(action,extra={})=>{const button={dataset:{gw:action,...extra},disabled:false,isConnected:true,closest:()=>actionPanel};await panel.listeners.click({target:{closest:selector=>selector==='[data-gw]'?button:null}});};
  await click('view',{id:'task'});
  assert.doesNotMatch(panel.innerHTML,/data-step="[2-6]"/,'unfinished rules expose no later-stage action');
  const draftHtml=panel.innerHTML;
  await click('step',{step:'4'});
  assert.equal(panel.innerHTML,draftHtml,'a direct later-step action is rejected');
  assert.doesNotMatch(panel.innerHTML,/版本 1/,'internal concurrency revision is not shown');
  assert.match(nodes.get('gw-content').innerHTML,/任务归属群组/);
  const name=fields.get('name');name.matches=selector=>selector==='input,textarea,select';
  panel.listeners.input({target:name});
  await click('extract');
  assert.equal(name.value,'手工名称','re-extraction must retain an edited value');
  assert.equal(fields.get('base_reward').value,'100','unchanged fields receive suggestions');
  assert.equal(hints.get('name').children[0].textContent,'采用识别值','manual adoption remains explicit');
  const before=panel.innerHTML;await poll();assert.equal(panel.innerHTML,before,'polling never replaces an unsaved form');
  fetchedTask={...task,updated_at:'2026-10-16T08:00:00Z'};
  $('gw-update-hint').hidden=true;
  await poll();assert.equal($('gw-update-hint').hidden,true,'same-version background updates do not raise a false result banner');
  assert.equal(panel.innerHTML,before,'background timestamps do not replace unsaved inputs');
  assert.equal(await ui.window.groupWorkflowMayLeave(),false,'declining leave keeps the current draft');
  hints.get('name').children[0].onclick();assert.equal(name.value,'识别名称');
  ui.window.workflowConfirm=async()=>true;
  fetchedTask={...task,rules:{name:'旧任务',payout_date:'2026-10-22',base_reward:100,excellent_reward:200,group_source_id:'group_a',group_name:'群甲'}};
  await click('settings',{id:'task'});
  assert.match(nodes.get('gw-content').innerHTML,/data-task-group value="group_a"[^>]* checked/,'legacy single group is preselected');
  assert.match(nodes.get('gw-content').innerHTML,/任务归属群组（可多选）/);
  assert.match(nodes.get('gw-content').innerHTML,/<details class="gw-task-group-select">/,'groups use a collapsed dropdown');
  assert.match(nodes.get('gw-content').innerHTML,/<span data-group-summary-text>群甲<\/span>/,'selected group appears inside the dropdown field');
  const settingFields=['name','payout_date','base_reward','excellent_reward'].map(key=>fields.get(key));
  settingFields.forEach(field=>field.value=String(fetchedTask.rules[field.dataset.rule]));
  let checkedGroups=[{value:'group_a'},{value:'group_b'}];
  panel.querySelectorAll=selector=>selector==='[data-rule]'?settingFields:selector==='[data-task-group]:checked'?checkedGroups:[];
  await click('save-settings');
  assert.deepEqual(Array.from(requests.at(-1).options.body.group_source_ids),['group_a','group_b'],'settings submit every selected group');
  checkedGroups=[];await click('save-settings');
  assert.deepEqual(Array.from(requests.at(-1).options.body.group_source_ids),[],'settings can clear all groups');
  fetchedTask={...task,rules:{...fetchedTask.rules,group_source_ids:['group_b','retired'],group_names:['群乙','停用旧群'],group_source_id:'group_b',group_name:'群乙、停用旧群'}};
  await click('settings',{id:'task'});
  assert.match(nodes.get('gw-content').innerHTML,/data-task-group value="retired"[^>]* checked/,'inactive saved groups remain selected');
  assert.match(nodes.get('gw-content').innerHTML,/停用旧群（已停用）/);
  assert.match(nodes.get('gw-content').innerHTML,/<span data-group-summary-text>群乙、停用旧群<\/span>/,'all selected group names appear in the closed field');
  checkedGroups=[{value:'group_b'},{value:'retired'}];
  await click('view',{id:'task'});await click('save-rules');
  assert.deepEqual(Array.from(requests.at(-1).options.body.rules.group_source_ids),['group_b','retired'],'new-task rules also submit a group list');
  console.log('Group workflow UI contracts passed: permissions, parent tri-state, empty defaults, structured errors, announcement draft protection, polling and leave protection.');
  console.log('Task group multiselect passed: legacy selection, settings/rules submission, empty selection and inactive groups.');

  // Exercise the actual delegated handlers, not just the presence of a label.
  const quality={overall:'优秀反馈',overall_reason:'合成判断',q_labels:{col_1:'优秀反馈'},q_reasons:{col_1:'具体体验'}};
  const feedback=['group_a','group_b'].map((group,i)=>({id:'response'+i,source_row:i+2,player_id:'10000000000000000'+i,excluded:false,quality_complete:true,quality,
    answers:[{column:1,question:'操作体验',answer:'合成回答'+i}],issues:[],identity:{source_id:group,group_name:i?'群乙':'群甲',status:'群内人员'},outcome:{label:'优秀反馈',base:100,excellent:100}}));
  const players=feedback.map((row,i)=>({player_id:row.player_id,source_id:row.identity.source_id,group_name:row.identity.group_name,identity:row.identity,
    ori:'80000000'+i+'(6001)',adv:'',form_valid:true,data_value:'',data_pass:null,base:100,excellent:100,total:200,
    feedback_count:1,response_ids:[row.id],composition:{'优秀反馈':1},issues:[],adv_choices:[]}));
  fetchedTask={...task,version:7,confirmed_version:7,updated_at:'2026-10-29T01:00:00Z',rules:{...fetchedTask.rules,ai_detect:false,allow_repeat:false,requires_count:false},
    headers:['playerid','操作体验'],mapping:{id_col:0,open_text_cols:[1]},feedback,players,runs:[],input_issues:[],confirmation_issues:[],duplicate_groups:[],
    workflow:{max_step:6},summary:{complete_count:2,feedback_count:2,player_count:2,base:200,excellent:200},
    annotation:{phase:'results',complete:true,options:{ai_detect:false,quality:true},review_ids:[],reviewed_ids:[],high_threshold:80,review_threshold:60},
    roster_snapshot:{read_at:'2026-10-29T01:00:00Z'},status:'confirmed'};
  await click('records',{id:'task'});
  const records=()=>$('gw-result-body').innerHTML;
  assert.match(records(),/当前任务记录已确认，可以复制/);
  assert.doesNotMatch(records(),/gw-record-details|gw-record-issue|data-gw="record-response"/,'normal players appear only in the table');
  assert.equal((records().match(/<th scope="col">/g)||[]).length,12);
  assert.equal((records().match(/<td>/g)||[]).length,24,'one row per player, 12 columns per row');
  assert.match(records(),/全部群 · 2 位玩家/);
  assert.doesNotMatch(records(),/data-gw="copy" disabled/);
  assert.match($('ga-panel-6').querySelector('.panel__header').innerHTML,/data-tab="records" aria-selected="true"/);
  // Identity resolution has no note control or note payload; only current members compete.
  const stableTask=fetchedTask;
  const activeChoices=[{key:'a',group_name:'群甲',ori:'800000000(6001)',status:'群内成员'},
    {key:'b',group_name:'群乙',ori:'800000000(6001)',status:'群内成员'}];
  const ambiguous={...feedback[0],identity:null,identity_choices:activeChoices,identity_candidates:activeChoices,
    issues:[{kind:'roster_identity',message:'多个名单条目均为群内成员，请选择所属群'}]};
  fetchedTask={...stableTask,confirmed_version:null,feedback:[ambiguous,feedback[1]],
    confirmation_issues:[{response_id:ambiguous.id,message:ambiguous.issues[0].message}]};
  await click('records',{id:'task'});
  assert.match(records(),/data-fix="candidate_key"/);
  assert.match(records(),/<article class="workflow-feedback gw-record-issue"><h3 class="gw-record-issue-title">/,'identity issues are visible without opening a disclosure');
  assert.doesNotMatch(records(),/处理说明|data-fix="note"/);
  let candidateValue='';
  actionPanel={querySelector:selector=>{assert.equal(selector,'[data-fix="candidate_key"]');return{value:candidateValue};}};
  const beforeChoice=requests.length;await click('record-identity',{id:'response0'});
  assert.equal(requests.length,beforeChoice,'empty identity still requires a choice');
  candidateValue='b';
  requestHook=async(url,options)=>{if(url.endsWith('/feedback/response0')){
    assert.equal(options.method,'PUT');assert.equal(options.body.candidate_key,'b');
    assert(!('note' in options.body),'identity save never asks for or sends a note');
    fetchedTask={...stableTask,confirmed_version:null};return fetchedTask;
  }};
  await click('record-identity',{id:'response0'});
  assert(toasts.at(-1)[0].includes('身份选择已保存'));
  fetchedTask={...stableTask,feedback:[{...feedback[0],identity_choices:[activeChoices[0]],identity_candidates:activeChoices},feedback[1]]};
  requestHook=undefined;await click('records',{id:'task'});
  assert.doesNotMatch(records(),/data-fix="candidate_key"|保存身份选择/,'automatic match needs no manual save');
  const inactive={...ambiguous,identity_choices:[],reward_exclusion_available:true,
    issues:[{kind:'roster_identity',message:'匹配的名单条目均不在群内，可排除该玩家奖励'}]};
  const inactiveTask={...stableTask,confirmed_version:null,feedback:[inactive,feedback[1]],
    confirmation_issues:[{response_id:inactive.id,message:inactive.issues[0].message}]};
  fetchedTask=inactiveTask;await click('records',{id:'task'});
  assert.match(records(),/排除该玩家奖励/);
  const aggregateMessage='同一玩家的群或 ori 不唯一，请修正反馈身份';
  fetchedTask={...inactiveTask,confirmation_issues:[...inactiveTask.confirmation_issues,
    {kind:'player',player_id:feedback[0].player_id,message:aggregateMessage},
    {kind:'player',player_id:feedback[1].player_id,message:aggregateMessage},
    {kind:'roster',message:'另一个必须保留的问题'}]};
  await click('records',{id:'task'});
  assert.match(records(),new RegExp(aggregateMessage+'（2 项）'),'repeated notices are counted rather than stacked');
  assert.equal((records().match(/class="workflow-error gw-record-issue-notice"/g)||[]).length,2,'different notices are never dropped');
  assert.match(records(),/匹配的名单条目均不在群内，可排除该玩家奖励/,'the row-specific cause stays visible');
  const duplicateTask={...stableTask,confirmed_version:null,duplicate_groups:[{id:'duplicate-1',roster_only:true,response_ids:['response0','response1']}],
    confirmation_issues:[{kind:'duplicate',message:'重复账号待核对'}]};
  fetchedTask=duplicateTask;await click('records',{id:'task'});
  assert.equal((records().match(/data-gw="record-duplicate"/g)||[]).length,2,'every duplicate candidate keeps its own action');
  requestHook=async(url,options)=>{if(url.endsWith('/duplicates')){
    assert.equal(options.body.group_id,'duplicate-1');assert.equal(options.body.keep_response_id,'response1');
    fetchedTask={...stableTask,confirmed_version:null};return fetchedTask;
  }};
  await click('record-duplicate',{group:'duplicate-1',id:'response1'});
  assert.doesNotMatch(records(),/data-gw="record-duplicate"/);
  requestHook=undefined;
  fetchedTask=inactiveTask;await click('records',{id:'task'});
  assert.doesNotMatch(records(),/data-fix="candidate_key"|处理说明/);
  requestHook=async(url,options)=>{if(url.endsWith('/feedback/response0')){
    assert.equal(options.method,'PUT');assert.equal(options.body.reward_excluded,true);
    assert(!('note' in options.body));
    fetchedTask={...stableTask,confirmed_version:null,feedback:[{...feedback[0],reward_excluded:true,outcome:{...feedback[0].outcome,base:0,excellent:0}},feedback[1]],
      players:[{...players[0],reward_excluded:true,base:0,excellent:0,total:0},players[1]]};return fetchedTask;
  }};
  await click('record-exclude',{id:'response0'});
  assert.match(records(),/已排除该玩家在本任务中的全部奖励/);
  assert(records().indexOf('data-gw="record-restore-reward"')<records().indexOf('class="workflow-table-wrap gw-record-table"'),'restore action is above the table');
  assert.match(records(),/复制范围：全部群 · 1 位玩家/);
  assert.match(records(),/恢复该玩家奖励/);
  requestHook=async(url,options)=>{if(url.endsWith('/feedback/response0')){
    assert.equal(options.body.reward_excluded,false);fetchedTask=inactiveTask;return fetchedTask;
  }};
  await click('record-restore-reward',{id:'response0'});
  assert.match(records(),/排除该玩家奖励/);
  assert(toasts.at(-1)[0].includes('已恢复该玩家奖励'));
  const unmatched={...inactive,identity_candidates:[],identity_choices:[],
    issues:[{kind:'roster_identity',message:'名单中找不到 playerid，请核对名单后重新读取，或排除该玩家奖励'}]};
  const unmatchedTask={...inactiveTask,feedback:[unmatched,feedback[1]],
    confirmation_issues:[{response_id:unmatched.id,message:unmatched.issues[0].message}]};
  requestHook=undefined;fetchedTask=unmatchedTask;await click('records',{id:'task'});
  assert.match(records(),/名单中找不到 playerid/);
  assert.match(records(),/data-gw="record-exclude"/);
  assert.match(records(),/反馈和标注结果保留/);
  assert.doesNotMatch(records(),/data-fix="candidate_key"/);
  requestHook=async(url,options)=>{if(url.endsWith('/feedback/response0')){
    assert.equal(options.body.reward_excluded,true);
    assert.equal(options.body.expected_version,unmatchedTask.version);
    fetchedTask={...stableTask,confirmed_version:null,feedback:[{...feedback[0],reward_excluded:true},feedback[1]],
      players:[{...players[0],reward_excluded:true,base:0,excellent:0,total:0},players[1]]};return fetchedTask;
  }};
  await click('record-exclude',{id:'response0'});
  assert.match(records(),/恢复该玩家奖励/);
  assert.match(records(),/复制范围：全部群 · 1 位玩家/);
  requestHook=async(url,options)=>{if(url.endsWith('/feedback/response0')){
    assert.equal(options.body.reward_excluded,false);fetchedTask=unmatchedTask;return fetchedTask;
  }};
  await click('record-restore-reward',{id:'response0'});
  assert.match(records(),/名单中找不到 playerid/);
  assert.match(records(),/data-gw="record-exclude"/);
  requestHook=undefined;
  for(const failed of [
    {...unmatchedTask,can_edit:false},
    {...unmatchedTask,roster_error:'合成读取失败',feedback:[{...unmatched,reward_exclusion_available:false},feedback[1]]}
  ]){
    fetchedTask=failed;await click('records',{id:'task'});
    assert(failed.can_edit===false?/data-gw="record-exclude" disabled/.test(records()):!records().includes('data-gw="record-exclude"'));
  }
  fetchedTask={...inactiveTask,can_edit:false};requestHook=undefined;await click('records',{id:'task'});
  assert.match(records(),/data-gw="record-exclude" disabled/);
  fetchedTask={...stableTask,can_edit:false,feedback:[{...feedback[0],reward_excluded:true}],
    players:[{...players[0],reward_excluded:true,base:0,excellent:0,total:0}]};
  await click('records',{id:'task'});
  assert.match(records(),/当前任务记录已确认，本范围没有可复制玩家/);
  assert.match(records(),/data-gw="copy" disabled/);
  assert.match(records(),/data-gw="record-restore-reward" disabled/);
  assert.doesNotMatch(records(),/gw-record-details/);
  const advIssue='同一玩家有不同 ADV，请明确选择记录账号';
  fetchedTask={...stableTask,confirmed_version:null,players:[{...players[0],adv_choices:['A','B'],issues:[advIssue]},players[1]],
    confirmation_issues:[{kind:'player',player_id:players[0].player_id,message:advIssue}]};
  await click('records',{id:'task'});
  assert(records().indexOf('data-gw="record-adv"')<records().indexOf('class="workflow-table-wrap gw-record-table"'),'ADV decision is above the table');
  let advNote='';actionPanel={querySelector:selector=>({value:selector==='[data-player-adv]'?'B':advNote})};
  const beforeAdv=requests.length;await click('record-adv',{player:players[0].player_id});
  assert.equal(requests.length,beforeAdv,'ADV explanation remains required');
  advNote='合成核对说明';
  requestHook=async(url,options)=>{if(url.endsWith('/player-adv')){
    assert.equal(options.method,'PUT');assert.equal(options.body.adv,'B');assert.equal(options.body.note,advNote);
    assert.equal(options.body.player_id,players[0].player_id);fetchedTask={...stableTask,confirmed_version:null};return fetchedTask;
  }};
  await click('record-adv',{player:players[0].player_id});
  assert.match(toasts.at(-1)[0],/ADV 决定已保存/);
  assert.doesNotMatch(records(),/data-gw="record-adv"/,'resolved ADV no longer adds a player card');
  requestHook=undefined;
  fetchedTask=stableTask;await click('records',{id:'task'});
  console.log('Identity and exclusion handlers passed: no note, active-only choice, automatic match, exclude, restore and read-only protection.');
  await $('gw-group').onchange({target:{value:'group_b'}});
  assert.match(records(),/群乙 · 1 位玩家/);
  assert(records().includes(players[1].player_id)&&!records().includes(players[0].player_id));
  let clipboardFailure=false;
  const payload={text:Array.from({length:12},(_,i)=>String(i)).join('\t'),html:'<table><tr>'+Array.from({length:12},(_,i)=>'<td>'+i+'</td>').join('')+'</tr></table>',columns:12,count:1,scope:'群乙'};
  requestHook=async url=>{if(url.includes('/clipboard?')){if(clipboardFailure)throw Object.assign(new Error('任务已更新'),{status:409});return payload;}};
  await click('copy');
  assert.equal(clipboardWrites.at(-1),payload.text);
  assert.match(requests.at(-1).url,/clipboard\?version=7&source_id=group_b$/);
  assert(toasts.some(([message,type])=>type==='success'&&message.includes('已复制群乙，1 行')));
  assert(!panel.innerHTML.includes('gw-copy-dialog'),'copy has no preview dialog');
  ui.window.ClipboardItem=class {constructor(formats){this.formats=formats;}};
  ui.navigator.clipboard.write=async items=>{
    const formats=items[0].formats;
    const [plain,html]=await Promise.all([formats['text/plain'],formats['text/html']]);
    clipboardWrites.push({text:await plain.text(),html:await html.text()});
  };
  await click('copy');
  assert.deepEqual(clipboardWrites.at(-1),{text:payload.text,html:payload.html},'plain text and HTML preserve one server result');
  let release,startCopy;
  const copyStarted=new Promise(resolve=>{startCopy=resolve;});
  requestHook=url=>url.includes('/clipboard?')?new Promise(resolve=>{release=resolve;startCopy();}):undefined;
  const copying=click('copy');
  await copyStarted;
  assert.equal(await ui.window.groupWorkflowMayLeave(),false,'leaving is blocked while the clipboard result is being prepared');
  const beforeRequests=requests.length;await click('copy');
  assert.equal(requests.length,beforeRequests,'double click does not issue a second copy request');
  release(payload);await copying;
  requestHook=async url=>{if(url.includes('/clipboard?'))throw Object.assign(new Error('公式目标未同步'),{status:409});};
  const copiedCount=clipboardWrites.length;
  await click('copy');
  assert.equal(clipboardWrites.length,copiedCount);
  assert.match(records(),/复制失败：公式目标未同步（HTTP 409）/);
  fetchedTask={...fetchedTask,version:8,confirmed_version:null,updated_at:'2026-10-29T02:00:00Z'};
  await poll();
  assert.match(records(),/data-gw="copy" disabled/,'changed results must be confirmed again');
  assert.match(records(),/群乙 · 1 位玩家/,'background refresh preserves the selected group');
  requestHook=async(url,options)=>{
    if(url.endsWith('/confirm')){assert.equal(options.body.expected_version,8);fetchedTask={...fetchedTask,confirmed_version:8};return fetchedTask;}
    if(url.endsWith('/roster')){fetchedTask={...fetchedTask,confirmed_version:null,confirmation_issues:[{kind:'roster',message:'合成名单读取失败'}],roster_error:'合成名单读取失败'};return{ok:false,task:fetchedTask};}
  };
  await click('record-confirm');assert.match(records(),/当前任务记录已确认，可以复制/);
  await click('record-roster');
  assert.match(records(),/合成名单读取失败/);assert.match(records(),/data-gw="copy" disabled/);
  requestHook=undefined;fetchedTask={...fetchedTask,confirmation_issues:[],roster_error:'',can_edit:false,confirmed_version:8};
  await click('records',{id:'task'});
  assert.match(records(),/data-gw="record-roster" disabled/,'read-only viewers cannot refresh or save');
  assert.doesNotMatch(records(),/data-gw="copy" disabled/,'confirmed records remain copyable for authorized readers');
  await $('gw-group').onchange({target:{value:'group_b'}});
  await click('record-response',{id:'response1'});
  assert.match($('ga-quality-profile-pane').innerHTML,/100000000000000001/,'source link opens the selected player: '+JSON.stringify(toasts.slice(-2)));
  await click('result-tab',{tab:'records'});
  assert.match(records(),/群乙 · 1 位玩家/,'returning from quality review retains the group filter');
  fetchedTask={...fetchedTask,players:[players[0]],feedback:[feedback[0]],updated_at:'2026-10-29T03:00:00Z'};
  await poll();
  assert.match(records(),/原选群（当前无记录） · 0 位玩家/,'a vanished group never silently expands the copy scope');
  assert.match(records(),/data-gw="copy" disabled/);
  fetchedTask={...fetchedTask,annotation:{...fetchedTask.annotation,complete:false}};
  const requestsBefore=requests.length;await click('records',{id:'task'});
  assert(toasts.at(-1)[0].includes('完成质量标注后'));
  assert.equal(requests.length,requestsBefore+2,'opening an unfinished task only fetches groups and task');
  console.log('Task-record handlers passed: direct entry, 12 columns, scoped copy, rich clipboard, busy protection, stale versions, roster failures, read-only state and exact player navigation.');
})().catch(err=>{console.error(err);process.exitCode=1;});
