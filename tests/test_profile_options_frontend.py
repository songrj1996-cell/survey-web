"""Profile preservation on main's draft model and question editor."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class ProfileOptionsFrontendTests(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run([shutil.which("node"), "-e", script, str(ROOT)],
                                capture_output=True, text=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_profile_draft_and_direct_serialization_preserve_raw_by_default(self):
        self.run_node(r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1]+'/static/js/features/survey.js','utf8');
const core=fs.readFileSync(process.argv[1]+'/static/js/core/core.js','utf8');
const css=fs.readFileSync(process.argv[1]+'/static/survey-entry.css','utf8');
const html=fs.readFileSync(process.argv[1]+'/static/index.html','utf8');
assert(!core.includes("['profile_dim','画像维度']"));
assert(source.includes('data-question-profile'));
assert(source.includes('画像用途'));
assert(source.includes('画像 — 进入画像分析 + 引用标注'));
assert(source.includes('画像 — 仅引用标注'));
assert(!source.includes('用作分群维度'));
assert(css.includes('.qe-profile-toggle'));
assert(css.includes('grid-template-columns:20px 48px minmax(0,1fr) auto 115px minmax(190px,auto) auto'));
assert(html.includes('/static/survey-entry.css?v=7'));
assert(html.includes('/static/js/core/core.js?v=38'));
assert(html.includes('/static/js/features/survey.js?v=39'));
const constants=core.slice(core.indexOf('const PROFILE_ANALYSIS_ROLES'),core.indexOf('// PROFILE_ROLE_CONSTANTS_END'));
const start=source.indexOf('function cloneColumn');
const model=source.slice(start,source.indexOf('// COLUMN_MODEL_END',start));
const ctx=vm.createContext({});vm.runInContext(constants+model,ctx);
const q={role:'profile_dim',column_indexes:[4],options:['Known'],
 unmatched_values:[{value:'Epic',count:3,suggested_handling:'review'},
 {value:'Legend',count:90,suggested_handling:'review'}]};
const frozen=JSON.stringify(q);
for(const c of [q,ctx.prepareColumnDraft(q)]){
 const result=ctx.serializeColumnDraft(c);
 assert.equal(result.role,'single_choice');
 assert.equal(result.use_as_profile,true);
 assert.equal(result.profile_scope,'analysis');
 assert.equal(result.other_text.enabled,false);
 assert(!result.options.includes('Other / 其他'));
 assert.equal(result.unmatched_values.length,2);
}
assert.equal(JSON.stringify(q),frozen);
let c=ctx.prepareColumnDraft(q);
assert.equal(c.unmatched_handling,'keep_raw');
c.unmatched_handling='as_other';
let result=ctx.serializeColumnDraft(c);
assert.equal(result.other_text.enabled,true);
assert(result.options.includes('Other / 其他'));
result.unmatched_handling='keep_raw';
result=ctx.serializeColumnDraft(result);
assert.equal(result.other_text.enabled,false);
assert(!result.options.includes('Other / 其他'));
for(const extra of [{unmatched_handling:'as_other'}, {other_text:{enabled:true,option:'其他'}}]){
 const draft=ctx.prepareColumnDraft({...q,...extra});
 assert.equal(ctx.serializeColumnDraft(draft).other_text.enabled,true);
}
for(const role of ['single_choice','multi_choice']){
 const draft=ctx.prepareColumnDraft({...q,role});
 assert.equal(draft.unmatched_handling,'as_other');
 assert.equal(ctx.serializeColumnDraft(draft).other_text.enabled,true);
}
const open=ctx.serializeColumnDraft({role:'open_text',use_as_profile:true,profile_scope:'analysis',column_indexes:[7]});
assert.equal(open.use_as_profile,true);assert.equal(open.profile_scope,'label');
const multi=ctx.serializeColumnDraft({role:'multi_choice',use_as_profile:true,profile_scope:'analysis',column_indexes:[8],options:['A','B'],delimiter:','});
assert.equal(multi.use_as_profile,true);assert.equal(multi.profile_scope,'analysis');
const ignored=ctx.serializeColumnDraft({role:'ignore',use_as_profile:true,profile_scope:'label',column_indexes:[9]});
assert.equal(ignored.use_as_profile,false);assert(!('profile_scope' in ignored));
 console.log('PASS: profile flag, raw defaults, explicit Other/raw and legacy normalization');
""")

    def test_plan_html_executes_for_profile_modes_and_question_roles(self):
        self.run_node(r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1]+'/static/js/features/survey.js','utf8');
const start=source.indexOf('function buildPlanHTML');
const end=source.indexOf('// ── Plan confirm',start);
assert(start>=0&&end>start,'buildPlanHTML source block must exist');
const context=vm.createContext({
 MATRIX_ROLES:[],
 esc:value=>String(value??'')
   .replaceAll('&','&amp;')
   .replaceAll('<','&lt;')
   .replaceAll('>','&gt;')
   .replaceAll('"','&quot;')
});
vm.runInContext(source.slice(start,end),context);
const roles={
 open_text:'开放题',
 single_choice:'单选题',
 multi_choice:'多选题'
};
const modes={
 none:{use_as_profile:false,profile_scope:undefined,label:null},
 label:{use_as_profile:true,profile_scope:'label',label:'画像题 · 仅标注'},
 analysis:{use_as_profile:true,profile_scope:'analysis',label:'画像题 · 进入分析'}
};
for(const [role,roleLabel] of Object.entries(roles)){
 for(const [mode,profile] of Object.entries(modes)){
  const column={index:0,name_zh:`${role}-${mode}`,role,use_as_profile:profile.use_as_profile};
  if(profile.profile_scope)column.profile_scope=profile.profile_scope;
  const plan={
   columns:[column],
   parts:[{name:'测试章节',scope:'验证方案卡渲染',column_indexes:[0]}],
   branch_rules:[],cross_tabs:[],open_questions:[]
  };
  const rendered=context.buildPlanHTML(plan,['原始题目']);
  assert(rendered.includes(`${role}-${mode}`),`${role}/${mode} must render its title`);
  assert(rendered.includes(profile.label||roleLabel),`${role}/${mode} must render the expected role label`);
  const hasOpenProfileNotice=rendered.includes('该题型不支持进入画像分析，已按仅标注处理');
  assert.equal(hasOpenProfileNotice,role==='open_text'&&profile.use_as_profile,`${role}/${mode} notice mismatch`);
 }
}
console.log('PASS: buildPlanHTML executes for 3 profile modes x 3 question roles');
""")

    @unittest.skipUnless(os.getenv("SURVEY_PLAYWRIGHT_MODULE"), "Set Playwright path for browser acceptance")
    def test_profile_review_in_browser(self):
        self.run_node(r"""
const fs=require('node:fs'),assert=require('node:assert/strict');
const {chromium}=require(process.env.SURVEY_PLAYWRIGHT_MODULE);
const source=fs.readFileSync(process.argv[1]+'/static/js/features/survey.js','utf8');
const core=fs.readFileSync(process.argv[1]+'/static/js/core/core.js','utf8');
const constants=core.slice(core.indexOf('const PROFILE_ANALYSIS_ROLES'),core.indexOf('// PROFILE_ROLE_CONSTANTS_END'));
const model=source.slice(source.indexOf('function cloneColumn'),source.indexOf('// COLUMN_MODEL_END'));
const editor=source.slice(source.indexOf('function editorOptionHTML'),source.indexOf('function openColumnEditor'));
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.SURVEY_BROWSER_PATH?{executablePath:process.env.SURVEY_BROWSER_PATH}:{})});
 try{
  const page=await browser.newPage();await page.route('**/*',r=>r.abort());
  await page.setContent('<div id="qe-editor-number"></div><div id="qe-editor-title"></div><div id="qe-editor-body"></div>');
  await page.addStyleTag({content:fs.readFileSync(process.argv[1]+'/static/style.css','utf8')});
  await page.addScriptTag({content:`
   const $=id=>document.getElementById(id), CHOICE_ROLES=['single_choice','multi_choice'], MATRIX_ROLES=[];
   ${constants}
   const ROLE_OPTIONS=[['single_choice','单选题'],['multi_choice','多选题']];
   const esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
   ${model}
   const state={columns:[prepareColumnDraft({role:'profile_dim',column_indexes:[4],options:['Known'],
    unmatched_values:Array.from({length:100},(_,i)=>({value:'raw '+i,count:1,suggested_handling:'review'}))})]};
   let columnEditor={index:0,draft:cloneColumn(state.columns[0])};
   ${editor}
   renderColumnEditor();
  `});
  assert.equal(await page.locator('[data-edit-role] option[value="profile_dim"]').count(),0);
  assert.equal(await page.getByText('画像用途',{exact:true}).count(),1);
  assert.equal(await page.locator('[data-edit-profile]').inputValue(),'analysis');
  assert.equal(await page.locator('[data-edit-profile]').isDisabled(),false);
  await page.getByText('100 种未匹配内容',{exact:true}).click();
  for(const width of [1100,390]){
   await page.setViewportSize({width,height:900});
   assert.equal(await page.locator('#qe-editor-body details p:visible').count(),10);
   assert.equal(await page.locator('[data-edit-unmatched]').inputValue(),'keep_raw');
   assert.equal(await page.locator('[data-edit-other]').isChecked(),false);
   assert.equal(await page.locator('[data-edit-option]').count(),1);
   assert.equal(await page.evaluate(()=>serializeColumnDraft(columnEditor.draft).other_text.enabled),false);
  }
  await page.getByText('查看其余 90 种取值',{exact:true}).click();
  assert.equal(await page.locator('#qe-editor-body details p:visible').count(),100);
  await page.locator('[data-edit-unmatched]').selectOption('as_other');
  assert.equal(await page.evaluate(()=>{flushColumnEditor();return serializeColumnDraft(columnEditor.draft).other_text.enabled;}),true);
  await page.locator('[data-edit-unmatched]').selectOption('keep_raw');
  assert.equal(await page.evaluate(()=>{flushColumnEditor();return serializeColumnDraft(columnEditor.draft).other_text.enabled;}),false);
  console.log('Browser PASS: main editor at desktop/mobile widths, preview and explicit handling');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
""")


if __name__ == "__main__":
    unittest.main()
