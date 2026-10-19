// Contract tests against the accepted 6b4740a copy and interactive validation.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
const file=path.join(root,'static/js/features/group-tasks.js');
const source=fs.readFileSync(file,'utf8');
const old=execFileSync('git',['show','6b4740a:static/index.html'],{cwd:root,encoding:'utf8'});
const nodes=new Map();
function element(){return {value:'',checked:false,disabled:false,textContent:'',innerHTML:'',dataset:{},classList:{add(){},remove(){},toggle(){},contains(){return false;}},addEventListener(){},querySelectorAll(){return []}};}
const $=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id)};
const context={window:{workflowRequest:async()=>({}),addEventListener(){}},$: $,esc:String,document:{hidden:false},
  PlatformPermissions:{canUse:()=>true},setInterval(){},showToast(){},console};
vm.createContext(context);
vm.runInContext(source.replace('return {open,list};','return {open,list,tests:{annTemplates,annSteps,annState,annUpdateStartBtn,syncAnnotation,annQualityQuestionLabel,annQualityBaseline,annRenderQualityPlayerList,annRenderQualityProfile,setTask(value){task=value;}}};'),context);
const t=context.window.GroupTasks.tests;
assert(!t.annTemplates['6'].includes('专注审阅'),'results have a single readable split layout');
assert(!source.includes('进入名单与奖励核对'),'roster checking belongs to reward batches');
assert(!source.includes("btn('step','任务设置'"),'annotation stages have no settings shortcut');
for(const text of ['选择标注任务（可多选）','识别观点和实质内容可能由 AI 生成的回答；AI 润色单独展示，不作为违规依据','调研背景说明（可选）','仅帮助判断 AI 作答，不会写入导出结果','正在分批分析受访者回答，请耐心等待','先看候选人风险概览，再进入玩家档案逐题确认','确认标注结果，继续下一步','下载标注结果 Excel']){
  assert(old.includes(text),'reference must contain '+text);
  assert(Object.values(t.annTemplates).some(html=>html.includes(text)),'new view must retain '+text);
}
for(const html of Object.values(t.annTemplates))assert(!/id="ann-|id="task-/.test(html),'task views must not share DOM ids with legacy views');
assert(!source.includes('data-rule="ai_detect"'),'task choice must not appear in task rules');
const task={can_edit:true,rules:{requires_count:false},status:'ready',headers:[],feedback:[],annotation:{options:{ai_detect:true,quality:true,background:''},review_ids:[],reviewed_ids:[],review_threshold:60,high_threshold:80}};
t.setTask(task);t.syncAnnotation();assert.equal(t.annState.tasks.ai_detect,false);assert.equal(t.annState.tasks.quality,false);
task.mapping={id_col:0,open_text_cols:[1]};t.syncAnnotation();assert.equal(t.annState.tasks.ai_detect,true);
task.mapping=null;task.annotation_options=task.annotation.options;t.syncAnnotation();assert.equal(t.annState.tasks.ai_detect,true,'saved choices survive a replacement upload');
task.mapping={id_col:0,open_text_cols:[1]};
t.annState.idCol=0;t.annState.openTextCols=[1];t.annState.tasks={ai_detect:false,quality:false};
t.annUpdateStartBtn();assert.equal($('ga-btn-start').disabled,true);assert.match($('gw-ann-validation').textContent,/至少选择一项/);
t.annState.tasks.quality=true;t.annUpdateStartBtn();assert.equal($('ga-btn-start').disabled,false);
t.annState.openTextCols=[0];t.annUpdateStartBtn();assert.match($('gw-ann-validation').textContent,/ID 列不能/);
t.annState.openTextCols=[1];task.rules.requires_count=true;$('gw-map-count').value='';$('gw-min-count').value='3';t.annUpdateStartBtn();assert.match($('gw-ann-validation').textContent,/数据校验列/);
$('gw-map-count').value='1';t.annUpdateStartBtn();assert.match($('gw-ann-validation').textContent,/不能.*重复/);
$('gw-map-count').value='2';$('gw-min-count').value='2.5';t.annUpdateStartBtn();assert.equal($('ga-btn-start').disabled,true);
$('gw-min-count').value='3';t.annUpdateStartBtn();assert.equal($('ga-btn-start').disabled,false);
task.can_edit=false;t.annUpdateStartBtn();assert.equal($('ga-btn-start').disabled,true);
task.can_edit=true;task.status='running';t.annUpdateStartBtn();assert.equal($('ga-btn-start').disabled,true);
assert.equal((t.annSteps(3).match(/disabled/g)||[]).length,6,'future stages cannot be clicked while running');
assert(t.annSteps(3).includes('step-bar__item--active'));
task.status='confirmed';task.annotation.complete=true;
t.annState.headers=['ID','题目一','题目二'];t.annState.headersZh=[];t.annState.openTextCols=[1,2];
const result={id:'p1',overall:'有效反馈',overall_reason:'完整回答判断',originals:{col_1:'实际回答',col_2:''},q_labels:{},q_reasons:{}};
assert.equal(t.annQualityQuestionLabel(result,'col_1'),'待补齐','a missing label is not invalid feedback');
assert.equal(t.annQualityBaseline(result,'col_1').label,'待补齐');
assert.equal(t.annQualityQuestionLabel(result,'col_2'),'N/A','a blank answer stays N/A');
t.annState.qualityResults=[result];t.annState.qualityReviewPlayerId='p1';t.annState.qualityQuestionIndex=0;
t.annRenderQualityPlayerList([result]);t.annRenderQualityProfile([result]);
assert(!$('ga-quality-player-list').innerHTML.includes('尚未修改'));
assert(!$('ga-quality-profile-pane').innerHTML.includes('尚未修改'));
assert.match($('ga-quality-profile-pane').innerHTML,/质量标签待补齐/);
assert.equal(($('ga-quality-profile-pane').innerHTML.match(/data-ann-quality-label="[^"]+" aria-pressed="false" disabled/g)||[]).length,3);
result.q_labels.col_1='有效反馈';t.annRenderQualityProfile([result]);
assert.match($('ga-quality-profile-pane').innerHTML,/当前采用 AI 标签；如有异议，可按需调整/);
result.human_reviews={col_1:{from_label:'优秀反馈',to_label:'有效反馈'}};
t.annRenderQualityPlayerList([result]);assert.match($('ga-quality-player-list').innerHTML,/已人工调整 1 道题/);
console.log('Annotation reference-copy and task-selection UI contracts passed.');
