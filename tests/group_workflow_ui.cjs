// Run with node tests/group_workflow_ui.cjs. No network, browser or third-party dependency.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..');
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.checked=false;this.indeterminate=false;this.disabled=false;this.style={};this.listeners={};this.value='';this.classList={add(){},remove(){},contains(){return false;}};}
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
  let poll;const requests=[];
  const ui={window:{addEventListener(){},workflowConfirm:async()=>false,workflowRequest:async(url,options)=>{requests.push({url,options});if(url.endsWith('/announcement'))return{suggestions:{name:{value:'识别名称',evidence:'公告'},base_reward:{value:100,evidence:'100'}},warnings:[]};return task;}},document:{hidden:false,createElement:tag=>new Element(tag)},$,esc:x=>String(x??''),showToast(){},confirm:()=>false,setInterval:fn=>{poll=fn;},PlatformPermissions:permissions,console};
  vm.createContext(ui);vm.runInContext(fs.readFileSync(path.join(root,'static/js/features/group-tasks.js'),'utf8'),ui);
  const click=async(action,extra={})=>{const button={dataset:{gw:action,...extra},disabled:false,isConnected:true};await panel.listeners.click({target:{closest:()=>button}});};
  await click('view',{id:'task'});
  const name=fields.get('name');name.matches=selector=>selector==='input,textarea,select';
  panel.listeners.input({target:name});
  await click('extract');
  assert.equal(name.value,'手工名称','re-extraction must retain an edited value');
  assert.equal(fields.get('base_reward').value,'100','unchanged fields receive suggestions');
  assert.equal(hints.get('name').children[0].textContent,'采用识别值','manual adoption remains explicit');
  const before=panel.innerHTML;await poll();assert.equal(panel.innerHTML,before,'polling never replaces an unsaved form');
  assert.equal(await ui.window.groupWorkflowMayLeave(),false,'declining leave keeps the current draft');
  hints.get('name').children[0].onclick();assert.equal(name.value,'识别名称');
  console.log('Group workflow UI contracts passed: permissions, parent tri-state, empty defaults, structured errors, announcement draft protection, polling and leave protection.');
})().catch(err=>{console.error(err);process.exitCode=1;});
