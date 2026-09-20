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
const start=source.indexOf('function cloneColumn');
const model=source.slice(start,source.indexOf('// COLUMN_MODEL_END',start));
const ctx=vm.createContext({});vm.runInContext(model,ctx);
const q={role:'profile_dim',column_indexes:[4],options:['Known'],
 unmatched_values:[{value:'Epic',count:3,suggested_handling:'review'},
 {value:'Legend',count:90,suggested_handling:'review'}]};
const frozen=JSON.stringify(q);
for(const c of [q,ctx.prepareColumnDraft(q)]){
 const result=ctx.serializeColumnDraft(c);
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
console.log('PASS: raw defaults, explicit Other/raw, source preservation and legacy roles');
""")

    @unittest.skipUnless(os.getenv("SURVEY_PLAYWRIGHT_MODULE"), "Set Playwright path for browser acceptance")
    def test_profile_review_in_browser(self):
        self.run_node(r"""
const fs=require('node:fs'),assert=require('node:assert/strict');
const {chromium}=require(process.env.SURVEY_PLAYWRIGHT_MODULE);
const source=fs.readFileSync(process.argv[1]+'/static/js/features/survey.js','utf8');
const model=source.slice(source.indexOf('function cloneColumn'),source.indexOf('// COLUMN_MODEL_END'));
const editor=source.slice(source.indexOf('function editorOptionHTML'),source.indexOf('function openColumnEditor'));
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.SURVEY_BROWSER_PATH?{executablePath:process.env.SURVEY_BROWSER_PATH}:{})});
 try{
  const page=await browser.newPage();await page.route('**/*',r=>r.abort());
  await page.setContent('<div id="qe-editor-number"></div><div id="qe-editor-title"></div><div id="qe-editor-body"></div>');
  await page.addStyleTag({content:fs.readFileSync(process.argv[1]+'/static/style.css','utf8')});
  await page.addScriptTag({content:`
   const $=id=>document.getElementById(id), CHOICE_ROLES=['profile_dim','single_choice','multi_choice'], MATRIX_ROLES=[];
   const ROLE_OPTIONS=[['profile_dim','画像维度']];
   const esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
   ${model}
   const state={columns:[prepareColumnDraft({role:'profile_dim',column_indexes:[4],options:['Known'],
    unmatched_values:Array.from({length:100},(_,i)=>({value:'raw '+i,count:1,suggested_handling:'review'}))})]};
   let columnEditor={index:0,draft:cloneColumn(state.columns[0])};
   ${editor}
   renderColumnEditor();
  `});
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
