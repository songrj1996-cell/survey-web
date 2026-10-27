/* Actual drawer, scripts and isolated settings APIs; no external requests or models. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

(async () => {
  const project = path.resolve(__dirname, '..');
  const artifact = path.join(project, '.test-artifacts', 'settings-navigation');
  assert(fs.statSync(artifact).isDirectory(), 'Use the approved acceptance directory');
  const data = fs.mkdtempSync(path.join(artifact, 'browser-'));
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const code = `import os,sys,socket
from pathlib import Path
import dotenv
dotenv.load_dotenv=lambda *a,**k:False
import httpx
async def no_async(*a,**k):raise AssertionError('External HTTP forbidden')
def no_sync(*a,**k):raise AssertionError('External HTTP forbidden')
httpx.AsyncHTTPTransport.handle_async_request=no_async
httpx.HTTPTransport.handle_request=no_sync
allowed=Path(os.environ['DATA_DIR']).parent.resolve()
def normal(p):return str(Path(os.fsdecode(p)).resolve()).removeprefix(chr(92)*2+'?'+chr(92)).lower()
def guard(event,args):
 if event=='socket.connect' and isinstance(args[1],tuple) and args[1][0] not in ('127.0.0.1','::1','localhost'):raise AssertionError('External socket forbidden')
 paths=[]
 if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)) and (args[2] or 0)&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):paths=[args[0]]
 elif event in ('os.remove','os.rmdir','os.mkdir','os.chmod'):paths=[args[0]]
 elif event in ('os.rename','os.replace'):paths=list(args[:2])
 for p in paths:
  if isinstance(p,(str,bytes,os.PathLike)) and normal(p)!=normal(allowed) and not normal(p).startswith(normal(allowed)+os.sep):raise PermissionError('Browser fixture write outside approved directory')
sys.addaudithook(guard)
import uvicorn
from tests.test_settings_navigation import make_settings_navigation_ui_app
uvicorn.run(make_settings_navigation_ui_app(),host='127.0.0.1',port=${port},log_level='error')`;
  const server = spawn('python', ['-B', '-c', code], {cwd: project, windowsHide: true,
    env: {...process.env, DATA_DIR: data, TMP: data, TEMP: data, LLM_MODEL_SETTINGS_FILE: path.join(data, 'models.json'),
      LLM_API_BASE: 'https://offline.invalid/v1', LLM_API_KEY: 'offline-fixture', PYTHONDONTWRITEBYTECODE: '1',
      PYTHON_DOTENV_DISABLED: '1', PYTHONIOENCODING: 'utf-8'}, stdio: ['ignore', 'pipe', 'pipe']});
  let serverOutput = '', browser;
  server.stdout.on('data', b => { serverOutput += b.toString(); });
  server.stderr.on('data', b => { serverOutput += b.toString(); });
  const report = {passed: false, checks: [], pageErrors: [], externalRequests: [], baselineFindings: [], actualProject: project};
  try {
    let ready = false;
    for (let i=0; i<100; i++) {
      if (server.exitCode !== null) throw Error('Isolated fixture exited: ' + serverOutput);
      try { if ((await fetch(base+'/fixture/identity')).ok) { ready=true; break; } } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert(ready, 'Fixture did not start: '+serverOutput);
    browser = await chromium.launch({headless: true, ...(process.env.MODEL_TEST_BROWSER ? {executablePath: process.env.MODEL_TEST_BROWSER} : {})});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.setDefaultTimeout(7000);
    page.on('pageerror', e => report.pageErrors.push(e.stack || e.message));
    await page.route('**/*', route => {
      if (route.request().url().startsWith(base+'/')) return route.continue();
      report.externalRequests.push(route.request().url()); return route.abort();
    });
    const nav = page.locator('#settings-nav');
    const select = async (key, readySelector) => {
      await nav.locator(`[data-stab="${key}"]`).click();
      await page.waitForFunction(key => document.querySelector(`[data-stab="${key}"]`)?.getAttribute('aria-current') === 'page', key);
      if (readySelector) await page.locator(readySelector).first().waitFor();
    };
    const visible = () => nav.locator('[data-stab]:visible').evaluateAll(items => items.map(x => x.dataset.stab));
    const settings = () => page.evaluate(async () => (await fetch('/api/app-settings')).json());
    await page.route('**/api/admin/audit-logs?*',async route=>{
      const response=await route.fetch(),data=await response.json();
      data.users=[{email:'long-name@offline.invalid',name:'离线验收使用的很长很长的成员显示名称'}];
      data.features=[{key:'long-feature',label:'离线验收使用的很长很长的功能名称'}];
      await route.fulfill({response,json:data});
    });
    await page.goto(base);
    await page.locator('#model-settings-root .model-settings').waitFor();
    assert.deepEqual(await visible(), ['models','prompts','glossary','functions','system','perms','audit']);
    assert.equal(await nav.locator('button[data-stab] svg').count(), 7);
    assert.equal(await page.locator('[data-stab="texts"],#stab-content-texts').count(), 0);
    report.checks.push('两大分组、七菜单、完整图标、默认有效页面、文案编辑入口已移除');

    await select('functions', '#setting-report-quick-mode');
    assert.equal(await page.locator('#stab-content-functions #setting-group-only-self').count(), 0);
    const initial = await settings();
    const fields = {'setting-survey-duplicate':'survey_duplicate_reminder_enabled','setting-comment-duplicate':'comment_duplicate_reminder_enabled',
      'setting-google-forms-entry':'google_forms_entry_enabled','setting-report-quick-mode':'report_quick_mode_enabled','setting-report-postwrite':'report_postwrite_enabled'};
    for (const [id,key] of Object.entries(fields)) {
      const changed = page.waitForResponse(r => r.url()===base+'/api/app-settings' && r.request().method()==='PATCH');
      await page.locator('#'+id).setChecked(!initial[key]);
      const response = await changed;
      assert.equal(response.status(), 200);
      assert.deepEqual(response.request().postDataJSON(), {[key]: !initial[key]});
      await page.waitForFunction(id => !document.getElementById(id).disabled, id);
    }
    const updated = await settings();
    for (const key of Object.values(fields)) assert.equal(updated[key], !initial[key]);
    assert.equal(updated.unrelated_fixture, 'preserved');
    await page.evaluate(()=>fetch('/fixture/fail-settings/true',{method:'POST'}));
    const failing = page.waitForResponse(r => r.url()===base+'/api/app-settings' && r.request().method()==='PATCH');
    await page.locator('#setting-report-quick-mode').setChecked(initial.report_quick_mode_enabled);
    assert.equal((await failing).status(), 503);
    await page.waitForFunction(()=>!document.getElementById('setting-report-quick-mode').disabled);
    assert.equal(await page.locator('#setting-report-quick-mode').isChecked(), updated.report_quick_mode_enabled);
    await page.evaluate(()=>fetch('/fixture/fail-settings/false',{method:'POST'}));
    report.checks.push('五个功能开关逐项保存、仅提交目标字段、其他字段保留、503 失败回退');

    await select('system', '#setting-group-only-self');
    assert.equal(await page.locator('#stab-content-system #setting-report-quick-mode').count(), 0);
    const scoped = page.waitForResponse(r=>r.url()===base+'/api/group-management/preferences' && r.request().method()==='PATCH');
    await page.locator('#setting-group-only-self').check();
    assert.equal((await scoped).status(), 200);
    assert.equal((await settings()).report_quick_mode_enabled, updated.report_quick_mode_enabled);
    await page.evaluate(()=>applyFixtureRole('admin_b'));
    assert.equal((await settings()).group_management_preferences.only_self, false);
    await page.evaluate(()=>applyFixtureRole('admin'));
    assert.equal((await settings()).group_management_preferences.only_self, true);
    report.checks.push('个人偏好与业务开关分离，两个管理员的查看范围互不影响');

    await select('functions', '#setting-report-quick-mode');
    await page.locator('#settings-drawer .drawer__close').click();
    await page.locator('#nav-settings').click();
    assert.equal(await nav.locator('[aria-current="page"]').getAttribute('data-stab'), 'functions');
    await page.reload();
    await page.locator('#setting-report-quick-mode').waitFor();
    await page.evaluate(()=>localStorage.setItem('settings-last-tab','texts'));
    await page.reload();
    await page.locator('#model-settings-root .model-settings').waitFor();
    assert.equal(await nav.locator('[aria-current="page"]').getAttribute('data-stab'), 'models');
    report.checks.push('关闭重开与刷新记住有效页面，旧 texts 记录自动回退');

    await page.getByRole('tab',{name:'模型目录',exact:true}).click();
    await page.locator('[data-ms="new-model"]').click();
    await page.getByRole('textbox',{name:'模型标识',exact:true}).fill('unsaved-offline-model');
    await select('functions', '#setting-report-quick-mode');
    await select('models', '#ms-model-form');
    assert.equal(await page.getByRole('textbox',{name:'模型标识',exact:true}).inputValue(), 'unsaved-offline-model');
    await page.locator('#settings-drawer .drawer__close').click();
    await page.locator('#nav-settings').click();
    assert.equal(await page.getByRole('textbox',{name:'模型标识',exact:true}).inputValue(), 'unsaved-offline-model');
    await page.locator('[data-ms="cancel-model"]').click();
    await select('prompts', '#stab-content-prompts textarea[data-content]');
    const area = page.locator('#stab-content-prompts textarea[data-content]').first();
    const key = await area.getAttribute('data-content'), original = await area.inputValue();
    await area.fill(original+'\n离线草稿保留检查');
    await select('functions', '#setting-report-quick-mode');
    await select('prompts', `textarea[data-content="${key}"]`);
    assert.equal(await page.locator(`#stab-content-prompts textarea[data-content="${key}"]`).inputValue(), original+'\n离线草稿保留检查');
    await page.locator(`#stab-content-prompts textarea[data-content="${key}"]`).fill(original);
    await select('perms', '#member-list');
    await page.locator('[data-member="member@offline.invalid"]').click();
    const oldName = await page.locator('#member-name').inputValue();
    await page.locator('#member-name').fill('尚未保存的成员草稿');
    await nav.locator('[data-stab="functions"]').click();
    await page.getByRole('button',{name:'继续编辑',exact:true}).click();
    assert.equal(await page.locator('#member-name').inputValue(), '尚未保存的成员草稿');
    assert.equal(await nav.locator('[aria-current="page"]').getAttribute('data-stab'), 'perms');
    assert.equal(await page.locator('#member-permission-tree [data-permission="platform"]').count(), 1);
    assert.equal(await page.locator('#member-permission-tree [data-permission="ui_texts"]').count(), 0);
    await page.locator('#member-name').fill(oldName);
    report.checks.push('模型草稿、提示词草稿和成员离开确认保留；授权不重复、不保留文案选项');

    await select('glossary', '[data-glossary-edit]');
    assert.equal(await page.locator('[data-glossary-edit]').count(), 300);
    await page.locator('[data-glossary-edit]').last().click();
    await page.locator('#glossary-editor-category').waitFor();
    assert.equal(await page.locator('#glossary-editor-category').inputValue(), '合成术语');
    assert.equal(await page.locator('#glossary-editor-category').evaluate(el=>el===document.activeElement), true);
    assert(await page.locator('#glossary-editor-panel .glossary-panel__head').evaluate(el=>{const r=el.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;}));
    report.checks.push('300 条术语的编辑面板仍位于列表上方并滚动、聚焦到位');

    await page.evaluate(async()=>{await applyFixtureRole('member');await loadActiveSettingsTab();});
    assert.deepEqual(await visible(), ['prompts','glossary','functions','audit']);
    await page.evaluate(async()=>{await applyFixtureRole('readonly');await loadActiveSettingsTab();});
    await select('functions', '#setting-report-quick-mode');
    assert.equal(await page.locator('#setting-report-quick-mode').isDisabled(), true);
    assert.equal(await page.evaluate(()=>switchSettingsTab('models')), false);
    const refused = await page.evaluate(async()=>(await fetch('/api/app-settings',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({report_quick_mode_enabled:false})})).status);
    assert.equal(refused,403);
    assert.equal(await page.locator('#nav-settings').isVisible(),true);
    for (const role of ['none','retired','anonymous']) {
      await page.evaluate(async role=>{await applyFixtureRole(role);await loadActiveSettingsTab();},role);
      assert.deepEqual(await visible(),[]);
      assert.equal(await page.locator('[data-settings-group]:visible').count(),0);
      assert.equal(await page.locator('[data-settings-panel]:visible').count(),0);
      assert.equal(await page.locator('#nav-settings').isVisible(),false);
    }
    report.checks.push('普通、只读、无权限、旧文案权限及未登录身份均按原权限显示与拒绝写入');

    await page.evaluate(async()=>{await applyFixtureRole('admin');await loadActiveSettingsTab();});
    await select('functions', '#setting-report-quick-mode');
    await page.locator('#btn-settings-nav-toggle').focus();
    await page.keyboard.press('Enter');
    await nav.locator('[data-stab="functions"]').focus();
    assert.equal(await page.locator('#btn-settings-nav-toggle').getAttribute('aria-expanded'),'false');
    assert.equal(await nav.locator('[data-stab="functions"]').evaluate(el=>getComputedStyle(el,'::after').content),'"功能设置"');
    await page.keyboard.press('Enter');
    await page.locator('#btn-settings-nav-toggle').click();
    for (const width of [1440,1280,1100,1024,820,768,390,320]) {
      await page.setViewportSize({width,height:1000});
      const readiness={functions:'#setting-report-quick-mode',system:'#setting-group-only-self',models:'.model-settings',
        glossary:'[data-glossary-edit]',perms:'#member-list',audit:'#audit-filter-apply',prompts:'#stab-content-prompts textarea[data-content]'};
      for (const key of ['functions','system','models','glossary','perms','audit','prompts']) {
        await select(key,readiness[key]);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${width}/${key} overflow`);
        const panel=await page.locator('#settings-content').evaluate(el=>({client:el.clientWidth,scroll:el.scrollWidth}));
        assert(panel.scroll<=panel.client+1,`${width}/${key} panel overflow`);
        if(key==='audit') {
          for(const id of ['audit-filter-start','audit-filter-end','audit-filter-user','audit-filter-feature','audit-filter-apply','audit-filter-reset']) {
            assert(await page.locator('#'+id).evaluate(el=>{const r=el.getBoundingClientRect(),p=document.getElementById('settings-content').getBoundingClientRect();return r.left>=p.left&&r.right<=p.right;}),`${width}/${id} clipped`);
          }
        }
        assert(await nav.locator(`[data-stab="${key}"] svg`).evaluate(el=>{const r=el.getBoundingClientRect();return r.width>=15&&r.height>=15;}));
      }
    }
    await select('functions', '#setting-report-quick-mode');
    await page.setViewportSize({width:1440,height:1000});
    await page.screenshot({path:path.join(artifact,'settings-desktop.png'),fullPage:true,animations:'disabled'});
    await page.setViewportSize({width:390,height:1000});
    await page.screenshot({path:path.join(artifact,'settings-narrow.png'),fullPage:true,animations:'disabled'});
    await page.evaluate(()=>document.documentElement.dataset.theme='dark');
    await page.setViewportSize({width:1440,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(artifact,'settings-dark.png'),fullPage:true,animations:'disabled'});
    report.checks.push('八种宽度的七页与内容区均无横向溢出，长筛选名称和六个控件完整可见，折叠键盘与深色通过');
    assert.deepEqual(report.pageErrors,[]);
    assert.deepEqual(report.externalRequests,[]);
    report.passed=true;
    report.fullyAccepted=true;
  } catch (error) {
    report.error=error.stack || String(error);
    const failedPage=browser?.contexts()[0]?.pages()[0];
    if(failedPage) await failedPage.screenshot({path:path.join(artifact,'settings-failure.png'),fullPage:true}).catch(()=>{});
    throw error;
  } finally {
    fs.writeFileSync(path.join(artifact,'browser-result.json'),JSON.stringify(report,null,2));
    if(browser) await browser.close();
    if(server.exitCode===null) { server.kill(); await new Promise(resolve=>server.once('exit',resolve)); }
    if(['External HTTP forbidden','External socket forbidden','write outside approved'].some(message=>serverOutput.includes(message))) throw Error('Offline fixture guard rejected an operation');
  }
  console.log(JSON.stringify(report,null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
