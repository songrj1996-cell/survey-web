// Run with node tests/group_workflow_ui.cjs. No network, browser or third-party dependency.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..');
class Element {
  constructor(tag='div'){this.tag=tag;this.isConnected=true;this.children=[];this.dataset={};this.checked=false;this.indeterminate=false;this.disabled=false;this.style={};this.listeners={};this.value='';this.classList={add(){},remove(){},toggle(){},contains(){return false;}};}
  append(...items){this.children.push(...items.filter(item=>typeof item!=='string'));}
  replaceChildren(...items){this.children=items;}
  querySelectorAll(selector){const out=[];const walk=node=>{for(const item of node.children){if(selector==='[data-permission]'&&item.dataset.permission||selector==='[data-permission]:checked'&&item.dataset.permission&&item.checked)out.push(item);walk(item);}};walk(this);return out;}
  querySelector(){return null;}
  closest(){return this;}
  addEventListener(event,callback){this.listeners[event]=callback;}
}
const document={createElement:tag=>new Element(tag),querySelectorAll:()=>[],getElementById:()=>null};
const nav=new Element();document.querySelector=()=>nav;
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

(async()=>{
  response={ok:false,status:409,json:async()=>({detail:{message:'名单发生变化',preview:{id:'review-again'}}})};
  await assert.rejects(context.window.workflowRequest('/synthetic'),err=>err.status===409&&err.detail.preview.id==='review-again');
  const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const fields=new Map(['name','announcement','payout_date','base_reward','excellent_reward','requires_count','min_count','allow_repeat','ai_detect'].map(key=>[key,Object.assign(new Element('input'),{dataset:{rule:key},matches:()=>true})]));
  fields.get('name').value='手工名称';fields.get('announcement').value='公告原文';
  const hints=new Map();const panel=$('group-tasks-panel');
  panel.querySelector=selector=>{const rule=selector.match(/data-rule="([^"]+)"/);if(rule)return fields.get(rule[1]);const suggestion=selector.match(/data-suggestion="([^"]+)"/);if(suggestion){if(!hints.has(suggestion[1]))hints.set(suggestion[1],new Element());return hints.get(suggestion[1]);}return null;};
  panel.querySelectorAll=()=>[];
  const task={id:'task',rules:{name:'已存名称'},summary:{complete_count:0,feedback_count:0},owner_name:'合成操作者',version:1,status:'draft',can_edit:true};
  let poll;const requests=[];let fetchedTask=task;
  const ui={window:{addEventListener(){},workflowConfirm:async()=>false,workflowRequest:async(url,options)=>{requests.push({url,options});if(url.endsWith('/groups'))return {groups:[{id:'group_a',name:'群甲'},{id:'group_b',name:'群乙'}]};if(url.endsWith('/announcement'))return{suggestions:{name:{value:'识别名称',evidence:'公告'},base_reward:{value:100,evidence:'100'}},warnings:[]};return fetchedTask;}},document:{hidden:false,createElement:tag=>new Element(tag)},$,esc:x=>String(x??''),showToast(){},confirm:()=>false,setInterval:fn=>{poll=fn;},PlatformPermissions:permissions,console};
  vm.createContext(ui);vm.runInContext(fs.readFileSync(path.join(root,'static/js/features/group-tasks.js'),'utf8'),ui);
  const click=async(action,extra={})=>{const button={dataset:{gw:action,...extra},disabled:false,isConnected:true};await panel.listeners.click({target:{closest:selector=>selector==='[data-gw]'?button:null}});};
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
})().catch(err=>{console.error(err);process.exitCode=1;});
