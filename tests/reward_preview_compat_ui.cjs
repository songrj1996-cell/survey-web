// Run with node tests/reward_preview_compat_ui.cjs. No network or runtime data.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const code=fs.readFileSync(path.join(__dirname,'../static/js/features/reward-batches.js'),'utf8');
class Element {
  constructor(){this.innerHTML='';this.listeners={};this.isConnected=true;this.classList={add(){},remove(){}};}
  addEventListener(type,callback){this.listeners[type]=callback;}
  querySelectorAll(){return [];}
  insertAdjacentHTML(position,html){this.innerHTML+=html;}
}
async function check(nativeUUID){
  const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const root=$('reward-batches-panel'),requests=[],toasts=[];
  const crypto={getRandomValues:array=>webcrypto.getRandomValues(array)};
  if(nativeUUID)crypto.randomUUID=()=>webcrypto.randomUUID();
  let nextError=null,revision=1,previewNumber=0;
  const preview=()=>({id:`preview-${previewNumber}`,revision,selection:{dates:['2026-10-09']},roster_snapshot:{read_at:'synthetic'},
    result:{unique_accounts:1,total_diamonds:100,accounts:[],txt_preview:'1\t2\t301-100'},identity_issues:[],unresolved_conflicts:[],
    roster_changes:[],conflicts:[],identity_choices:{},selections:{},excluded_rows:[],ready:true});
  const api=async(url,options={})=>{
    requests.push({url,...options});
    if(nextError){const error=nextError;nextError=null;throw error;}
    if(url.endsWith('/choices'))return {tasks:[],other_rewards:[],imports:[],groups:[],batches:[]};
    if(url.endsWith('/generate'))return {id:'saved-batch',created_at:'synthetic',owner_name:'合成操作者',dates:['2026-10-09'],
      unique_accounts:1,total_diamonds:100,accounts:[],task_versions:{},note_full:'',txt_preview:'1\t2\t301-100'};
    if(options.method==='POST'){previewNumber++;revision=1;}else revision++;
    return preview();
  };
  const context={window:{crypto,workflowRequest:api},$,esc:String,PlatformPermissions:{canUse:()=>true},workflowTime:String,
    showToast:(message,type)=>toasts.push({message,type}),console};
  vm.createContext(context);vm.runInContext(code,context);
  const click=async action=>{const button={dataset:{rb:action},isConnected:true};await root.listeners.click({target:{closest:()=>button}});assert.equal(button.disabled,false);};
  const generated=()=>requests.filter(r=>r.url.endsWith('/generate'));
  const key=()=>generated().at(-1).body.idempotency_key;
  const assertKey=()=>assert.match(key(),/^[A-Za-z0-9_-]{16,128}$/);
  const saved=()=>assert.match($('rb-body').innerHTML,/已保存发奖批次/);
  const failure=()=>{nextError=new Error('合成网络中断');};
  await click('preview');
  assert.match($('rb-body').innerHTML,/核对奖励预览/);
  assert.equal(toasts.length,0,'preview renders without a browser compatibility error');
  failure();await click('generate');assertKey();const retryKey=key();
  await click('generate');assert.equal(key(),retryKey,'retry retains its idempotency key');saved();
  assert.match($('rb-body').innerHTML,/\/api\/reward-batches\/saved-batch\/download/);
  await click('choices');await click('preview');
  failure();await click('generate');const beforeRefresh=key();
  await click('refresh-preview');failure();await click('generate');assertKey();assert.notEqual(key(),beforeRefresh);
  const beforeResolve=key();await click('resolve');failure();await click('generate');assertKey();assert.notEqual(key(),beforeResolve);
  const beforeChange=key();revision++;
  nextError=Object.assign(new Error('合成名单变化'),{detail:{preview:{...preview(),generation_difference:{before_total:100,after_total:100}}}});
  await click('generate');assert.match($('rb-body').innerHTML,/生成前发现名单变化/);
  assert.equal(toasts.at(-1).message,'合成名单变化','409 recovery does not throw a second compatibility error');
  await click('generate');assertKey();assert.notEqual(key(),beforeChange);saved();
  assert.equal(generated().at(-1).body.expected_revision,revision);
  assert.equal(toasts.some(t=>/randomUUID|not a function/.test(t.message)),false);
  console.log(`${nativeUUID?'Native UUID':'HTTP fallback'}: preview, refresh, resolve, 409 recovery, retry key and download link passed.`);
}
(async()=>{await check(true);await check(false);})().catch(error=>{console.error(error);process.exitCode=1;});
