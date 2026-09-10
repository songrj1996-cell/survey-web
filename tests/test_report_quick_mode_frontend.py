"""Browser interaction acceptance; supply optional local Node/Playwright paths."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest

from app.services.report_quick_mode import render_quick_report

ROOT = Path(__file__).resolve().parents[1]

BROWSER_CHECK = r'''
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.SURVEY_PLAYWRIGHT_MODULE || 'playwright');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({headless:true, ...(process.env.SURVEY_BROWSER_PATH ? {executablePath:process.env.SURVEY_BROWSER_PATH} : {})});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1050}, reducedMotion:'reduce'});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    let slowOptions = null;
    let quickEnabled = false;
    let failSettingSave = false;
    const settingWrites = [];
    await page.route('**/api/**', async route => {
      if (route.request().url().endsWith('/app-settings')) {
        if (route.request().method() === 'PATCH') {
          if (failSettingSave) return route.fulfill({status:503,json:{detail:'设置保存失败'}});
          const body = route.request().postDataJSON();
          settingWrites.push(body);
          quickEnabled = body.report_quick_mode_enabled;
        }
        return route.fulfill({json:{comment_duplicate_reminder_enabled:true,google_forms_entry_enabled:false,report_quick_mode_enabled:quickEnabled}});
      }
      if (route.request().url().endsWith('/options')) {
        if (route.request().url().includes('/late/')) { slowOptions = route; return; }
        return route.fulfill({json:{quick_enabled: quickEnabled && !route.request().url().includes('/quant/'), report_style:'full'}});
      }
      return route.fulfill({json:{logged_in:true,authenticated:true,login_required:false,enabled:false,items:[],entries:[],texts:{},users:[],settings:{}}});
    });
    if (process.env.SURVEY_MARKED_FILE) await page.route('**/marked.min.js', route => route.fulfill({path:process.env.SURVEY_MARKED_FILE,contentType:'application/javascript'}));
    await page.route('https://fonts.googleapis.com/**', route => route.abort());
    await page.goto(input.url, {waitUntil:'networkidle'});
    await page.evaluate(() => { state.sessionId='first'; state.currentStep=3; goStep(3); $('plan-card').style.display='block'; });
    await page.evaluate(() => loadReportStyleOptions());
    assert.equal(await page.locator('#report-style-picker').isVisible(),false);
    await page.evaluate(() => { openDrawer('settings-drawer'); switchSettingsTab('system'); });
    const toggle = page.locator('#setting-report-quick-mode');
    await toggle.waitFor({state:'visible'});
    assert.equal(await toggle.isChecked(),false);
    await toggle.check();
    await page.waitForFunction(() => !$('setting-report-quick-mode').disabled && !$('report-style-picker').hidden);
    assert.deepEqual(settingWrites,[{report_quick_mode_enabled:true}]);
    await page.evaluate(() => loadSystemSettings());
    assert.equal(await toggle.isChecked(),true,'saved toggle survives reopening settings');
    if (input.output) await page.screenshot({path:input.output+'/platform-quick-toggle.png'});
    failSettingSave = true;
    await toggle.uncheck();
    await page.waitForFunction(() => !$('setting-report-quick-mode').disabled && $('setting-report-quick-mode').checked);
    assert.equal(await page.locator('#report-style-picker').evaluate(el => el.hidden),false,'failed save must preserve available mode');
    failSettingSave = false;
    await toggle.uncheck();
    await page.waitForFunction(() => !$('setting-report-quick-mode').disabled && $('report-style-picker').hidden);
    await toggle.check();
    await page.waitForFunction(() => !$('setting-report-quick-mode').disabled && !$('report-style-picker').hidden);
    await page.evaluate(() => closeDrawer('settings-drawer'));
    await page.locator('#report-style-picker').waitFor({state:'visible'});
    await page.check('input[name="report-style"][value="quick"]');
    assert.equal(await page.evaluate(() => selectedReportStyle()),'quick');
    await page.evaluate(() => lockReportStyleSelection(true));
    assert.equal(await page.locator('input[name="report-style"][value="quick"]').isDisabled(),true);
    await page.evaluate(() => { state.sessionId='quant'; return loadReportStyleOptions(); });
    assert.equal(await page.locator('#report-style-picker').isVisible(),false);
    assert.equal(await page.evaluate(() => selectedReportStyle()),'full');
    await page.evaluate(() => { state.sessionId='first'; return loadReportStyleOptions(); });
    assert.equal(await page.evaluate(() => selectedReportStyle()),'full');
    if (input.output) await page.screenshot({path:input.output+'/plan-mode-light.png'});

    // Stale capability response must not enable quick mode on a different session.
    await page.evaluate(() => { state.sessionId='late'; loadReportStyleOptions(); });
    await page.waitForFunction(() => state.reportStyleSelection.sessionId === 'late');
    await page.evaluate(() => { state.sessionId='quant'; return loadReportStyleOptions(); });
    if (slowOptions) await slowOptions.fulfill({json:{quick_enabled:true,report_style:'quick'}});
    assert.equal(await page.evaluate(() => selectedReportStyle()),'full');

    await page.evaluate(md => {
      state.sessionId='demo'; state.currentStep=5; state.mode=null; state.viewMode='session';
      state.sessionReport={...state.sessionReport,id:'demo',reportMd:md,title:'核心体验与优先改进方向',running:false,reportStyle:'quick',version:1,selectedVersion:1,versions:[{version:1,report_style:'quick'}]};
      renderReportWorkspace(md);
    }, input.markdown);
    assert.equal(await page.locator('.quick-report-nav').count(),1);
    assert.equal(await page.locator('.quick-appendix').getAttribute('open'),null);
    assert.equal(await page.locator('#btn-report-partial-rerun').isDisabled(),true);
    assert.equal(await page.locator('.quick-evidence').count(),input.catalogCount);
    const source = page.locator('.quick-reference').filter({hasText:'[E5]'}).first();
    await source.click();
    assert.equal(await page.locator('.quick-appendix').evaluate(el => el.open),true);
    const target = page.locator('#quick-evidence-E5');
    assert.equal(await target.isVisible(),true);
    const targetRect = await target.boundingBox();
    assert(targetRect.y >= 0 && targetRect.y < 900, 'evidence jump must land in the visible report body');
    await page.locator('.quick-evidence').filter({has:target}).getByRole('button',{name:'返回引用处'}).click();
    assert.equal(await source.evaluate(el => document.activeElement===el),true);
    const original = await page.evaluate(() => state.sessionReport.reportMd);
    assert.equal(original,input.markdown,'navigation must never rewrite export/QA Markdown');
    await page.getByRole('button',{name:'03  发现与证据附录',exact:true}).click();
    assert.equal(await page.locator('.quick-appendix').evaluate(el => el.open),true);
    await page.locator('#report-toc-list a').filter({hasText:'关键发现'}).click();
    assert.equal(await page.locator('#quick-layer-1').isVisible(),true);
    await page.evaluate(() => { document.querySelector('.quick-appendix').open=false; document.querySelector('#panel-5 .report-document .report-body').scrollTop=0; document.documentElement.setAttribute('data-theme','light'); });
    if (input.output) await page.screenshot({path:input.output+'/quick-report-light.png'});
    await page.evaluate(() => document.documentElement.setAttribute('data-theme','dark'));
    if (input.output) await page.screenshot({path:input.output+'/quick-report-dark.png'});
    await page.setViewportSize({width:700,height:1000});
    if (input.output) await page.screenshot({path:input.output+'/quick-report-narrow.png'});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1),false);

    await page.evaluate(() => {
      syncReportVersionMeta(state.sessionReport,{versions:[{version:1,report_style:'quick'},{version:2,report_style:'full'}],version:2,selected_version:2,report_style:'full'});
      renderReportWorkspace('# 完整报告\n\n## 核心结论\n\n完整正文');
    });
    assert.equal(await page.locator('.quick-report-nav').count(),0);
    assert.equal(await page.locator('.quick-appendix').count(),0);
    assert.equal(await page.locator('#btn-report-partial-rerun').isDisabled(),false);
    assert.deepEqual(errors,[]);
    process.stdout.write(JSON.stringify({passed:true,checks:['admin toggle persistence','save failure rollback','live selector update','selection','feature flag','stale response','evidence jump','return','appendix','TOC','version isolation','unchanged export/QA source','light/dark/narrow'],pageErrors:errors}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
'''


class QuickReportBrowserTests(unittest.TestCase):
    def test_actual_platform_page_interactions(self):
        node = os.getenv('SURVEY_NODE_PATH') or shutil.which('node')
        if not node:
            self.skipTest('Node is unavailable; browser verification not run')
        if not os.getenv('SURVEY_PLAYWRIGHT_MODULE'):
            self.skipTest('Set SURVEY_PLAYWRIGHT_MODULE to an installed Playwright package')
        fixture=json.loads((ROOT/'tests/fixtures/report_pipeline/quick_mode_cases.json').read_text(encoding='utf-8'))
        markdown,_=render_quick_report(fixture['sample_draft'],fixture['evidence_catalog'])
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str(ROOT)))
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        output=os.getenv('REPORT_QUICK_ACCEPTANCE_DIR','')
        if output: Path(output).mkdir(parents=True,exist_ok=True)
        try:
            result=subprocess.run([node,'-e',BROWSER_CHECK],input=json.dumps({'url':f'http://127.0.0.1:{server.server_port}/static/index.html','markdown':markdown,'catalogCount':len(fixture['evidence_catalog']),'output':output}),text=True,encoding='utf-8',capture_output=True,timeout=90)
            self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
            print(result.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__=='__main__': unittest.main()
