/* Browser integration against isolated FastAPI settings routes. Uses the supplied browser runtime. */
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

(async () => {
  const project = path.resolve(__dirname, '..');
  const dataDir = process.env.DATA_DIR;
  assert(dataDir && path.resolve(dataDir).startsWith(project + path.sep), 'Browser test requires an explicit isolated DATA_DIR under this worktree');
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0,'127.0.0.1',resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const configFile = path.join(dataDir, 'ui-' + Date.now(), 'models.json');
  const server = spawn('python',['-c',`import uvicorn; from tests.test_llm_model_settings_routes import make_model_settings_ui_app; uvicorn.run(make_model_settings_ui_app(),host='127.0.0.1',port=${port},log_level='error')`],{cwd:project,env:{...process.env,LLM_MODEL_SETTINGS_FILE:configFile,PYTHONIOENCODING:'utf-8',PYTHONDONTWRITEBYTECODE:'1'},windowsHide:true,stdio:['ignore','pipe','pipe']});
  let output=''; server.stdout.on('data',b=>{output+=b.toString();});server.stderr.on('data',b=>{output+=b.toString();});
  let browser;
  const base=`http://127.0.0.1:${port}`;
  try {
    let ready=false;
    for(let i=0;i<80;i++){
      if(server.exitCode!==null)throw new Error('Isolated settings fixture exited: '+output);
      try{const response=await fetch(base+'/api/model-settings');if(response.ok){ready=true;break;}}catch{}
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    assert(ready,'Isolated fixture did not start: '+output);
    const browserPath=process.env.MODEL_TEST_BROWSER;
    browser=await chromium.launch({headless:true,...(browserPath?{executablePath:browserPath}:{})});
    const page=await browser.newPage({viewport:{width:1024,height:1000}});
    page.setDefaultTimeout(5000);
    const errors=[],external=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',route=>{if(route.request().url().startsWith(base+'/'))return route.continue();external.push(route.request().url());return route.abort();});
    const checks=[];
    await page.goto(base);
    await page.getByRole('tab',{name:'模型目录',exact:true}).waitFor();
    assert.match(await page.locator('#model-settings-root').innerText(),/实际业务尚未接入/);
    assert(!fs.existsSync(configFile),'Reading settings must not create configuration');
    await page.getByRole('tab',{name:'模型目录',exact:true}).click();
    await page.getByRole('button',{name:'＋ 新增模型',exact:true}).click();
    await page.getByRole('textbox',{name:'模型标识',exact:true}).fill('ui-model-test');
    await page.getByRole('textbox',{name:'显示名称',exact:true}).fill('界面合成模型');
    await page.getByRole('spinbutton',{name:'上下文容量',exact:true}).fill('1000000');
    await page.getByRole('spinbutton',{name:'模型最大输出',exact:true}).fill('64000');
    await page.getByRole('textbox',{name:'支持的推理选项',exact:true}).fill('default, medium, high');
    await page.getByRole('checkbox').check();
    await page.getByRole('textbox',{name:'核实依据',exact:true}).fill('合成测试能力声明，不代表真实服务');
    await page.getByRole('tab',{name:'业务配置',exact:true}).click();
    await page.getByRole('tab',{name:'模型目录',exact:true}).click();
    assert.equal(await page.getByRole('textbox',{name:'模型标识',exact:true}).inputValue(),'ui-model-test');
    await page.getByRole('button',{name:'保存到草稿',exact:true}).click();
    for(const id of ['gpt-5.6-sol','claude-sonnet-5']){
      await page.locator(`[data-ms-model="${id}"]`).click();
      await page.getByRole('spinbutton',{name:'上下文容量',exact:true}).fill('1000000');
      await page.getByRole('spinbutton',{name:'模型最大输出',exact:true}).fill('100000');
      await page.getByRole('textbox',{name:'支持的推理选项',exact:true}).fill('default, medium, high');
      await page.getByRole('checkbox').check();
      await page.getByRole('textbox',{name:'核实依据',exact:true}).fill('仅用于合成界面验证');
      await page.getByRole('button',{name:'保存到草稿',exact:true}).click();
    }
    await page.getByRole('tab',{name:'业务配置',exact:true}).click();
    await page.locator('#ms-business').selectOption({label:'定性与洞察报告'});
    await page.getByRole('button',{name:/初稿写作/}).click();
    await page.getByRole('combobox',{name:'主模型',exact:true}).selectOption('ui-model-test');
    await page.getByRole('combobox',{name:'主模型推理程度',exact:true}).selectOption('high');
    await page.getByRole('button',{name:'＋ 添加备用',exact:true}).click();
    await page.getByRole('combobox',{name:'备用模型 1',exact:true}).selectOption('claude-sonnet-5');
    await page.getByRole('combobox',{name:'备用模型 1推理程度',exact:true}).selectOption('medium');
    await page.getByRole('button',{name:'＋ 添加备用',exact:true}).click();
    await page.getByRole('combobox',{name:'备用模型 2',exact:true}).selectOption('gpt-5.6-sol');
    await page.getByRole('combobox',{name:'备用模型 2推理程度',exact:true}).selectOption('high');
    await page.getByRole('button',{name:'上移备用 2',exact:true}).click();
    assert.equal(await page.getByRole('combobox',{name:'备用模型 1',exact:true}).inputValue(),'gpt-5.6-sol');
    assert.equal(await page.getByRole('combobox',{name:'备用模型 1推理程度',exact:true}).inputValue(),'high');
    await page.locator('[data-ms-advanced] summary').click();
    await page.getByRole('spinbutton',{name:'真实请求上限',exact:true}).fill('3');
    await page.getByRole('spinbutton',{name:'真实请求上限',exact:true}).press('Tab');
    await page.getByRole('combobox',{name:'备用模型 1',exact:true}).selectOption('ui-model-test');
    await page.getByRole('button',{name:'预览发布',exact:true}).click();
    await page.getByRole('alert').filter({hasText:'主模型与备用模型不能重复'}).waitFor();
    assert.equal(await page.getByRole('button',{name:'发布配置版本',exact:true}).count(),0);
    await page.getByRole('combobox',{name:'备用模型 1',exact:true}).selectOption('gpt-5.6-sol');
    await page.getByRole('spinbutton',{name:'主模型最大输出',exact:true}).fill('80000');
    await page.getByRole('spinbutton',{name:'主模型最大输出',exact:true}).press('Tab');
    await page.getByRole('button',{name:'预览发布',exact:true}).click();
    await page.getByRole('alert').filter({hasText:'输出长度超过'}).waitFor();
    await page.getByRole('spinbutton',{name:'主模型最大输出',exact:true}).fill('64000');
    await page.getByRole('spinbutton',{name:'主模型最大输出',exact:true}).press('Tab');
    checks.push('备用排序保留独立参数，重复模型与输出超限不能发布');
    await page.getByRole('button',{name:'预览发布',exact:true}).click();
    await page.getByRole('button',{name:'发布配置版本',exact:true}).waitFor();
    assert.match(await page.locator('.model-settings-review').innerText(),/ui-model-test/);
    await page.locator('#ms-note').fill('第一版浏览器验收');
    await page.getByRole('button',{name:'发布配置版本',exact:true}).click();
    await page.getByText('最新保存 v1',{exact:true}).waitFor();
    assert(fs.existsSync(configFile));
    const first=JSON.parse(fs.readFileSync(configFile,'utf8'));
    assert.equal(first.current.configuration.tasks['insight.writer'].primary.reasoning,'high');
    assert.equal(first.current.actor_name,'界面验证管理员');
    checks.push('新增目录、跨页草稿、真实设置接口检查与发布 v1');
    await page.getByRole('combobox',{name:'主模型推理程度',exact:true}).selectOption('medium');
    // A second administrator changes the same field and another business stage.
    const latest=await (await fetch(base+'/api/model-settings')).json();
    latest.configuration.tasks['insight.writer'].primary.reasoning='default';
    latest.configuration.tasks['survey.columns'].timeout_seconds=120;
    const remote=await fetch(base+'/api/model-settings/publish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:latest.revision,configuration:latest.configuration,note:'并发测试'})});
    assert.equal(remote.status,200);
    await page.getByRole('button',{name:'预览发布',exact:true}).click();
    await page.getByRole('button',{name:'加载最新版本并核对',exact:true}).waitFor();
    await page.getByRole('button',{name:'加载最新版本并核对',exact:true}).click();
    await page.getByRole('button',{name:'保留我的修改',exact:true}).waitFor();
    await page.getByRole('button',{name:'保留我的修改',exact:true}).click();
    assert.equal(await page.getByRole('combobox',{name:'主模型推理程度',exact:true}).inputValue(),'medium');
    await page.getByRole('button',{name:'预览发布',exact:true}).click();
    await page.getByRole('button',{name:'发布配置版本',exact:true}).click();
    await page.getByText('最新保存 v3',{exact:true}).waitFor();
    const merged=JSON.parse(fs.readFileSync(configFile,'utf8')).current.configuration;
    assert.equal(merged.tasks['insight.writer'].primary.reasoning,'medium');
    assert.equal(merged.tasks['survey.columns'].timeout_seconds,120);
    checks.push('并发冲突保留草稿、逐项选择、保留其他管理员独立修改');
    await page.getByRole('tab',{name:'版本历史',exact:true}).click();
    await page.locator('[data-ms-restore="1"]').click();
    await page.getByText('已恢复 v1 为草稿，尚未发布。',{exact:true}).waitFor();
    assert.match(await page.locator('#model-settings-root').innerText(),/已恢复 v1 为草稿/);
    await page.getByRole('button',{name:'预览发布',exact:true}).click();
    await page.getByRole('button',{name:'发布配置版本',exact:true}).click();
    await page.getByText('最新保存 v4',{exact:true}).waitFor();
    assert.equal(JSON.parse(fs.readFileSync(configFile,'utf8')).current.restored_from,1);
    checks.push('旧版本恢复为草稿，保存为 v4 而非覆盖历史');
    for(const width of [1024,736,390,320]){
      await page.setViewportSize({width,height:1000});
      for(const label of ['业务配置','模型目录','版本历史']){
        await page.getByRole('tab',{name:label,exact:true}).click();
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${width}px ${label} overflow`);
      }
    }
    await page.evaluate(()=>document.documentElement.dataset.theme='dark');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    checks.push('1024/736/390/320px 三页无横向溢出，深色主题');
    if(process.env.MODEL_TEST_CAPTURE==='1'){
      await page.getByRole('tab',{name:'业务配置',exact:true}).click();
      await page.setViewportSize({width:1024,height:1000});
      await page.evaluate(()=>document.documentElement.dataset.theme='light');
      await page.screenshot({path:path.join(dataDir,'ui-desktop.png'),fullPage:true});
      await page.setViewportSize({width:390,height:1000});
      await page.screenshot({path:path.join(dataDir,'ui-narrow.png'),fullPage:true});
      await page.evaluate(()=>document.documentElement.dataset.theme='dark');
      await page.setViewportSize({width:1024,height:1000});
      await page.screenshot({path:path.join(dataDir,'ui-dark.png'),fullPage:true});
    }
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    console.log(JSON.stringify({passed:true,checks,pageErrors:errors,externalRequests:external},null,2));
  } finally {
    if(browser)await browser.close();
    server.kill();
    await new Promise(resolve=>server.exitCode!==null?resolve():server.once('exit',resolve));
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
