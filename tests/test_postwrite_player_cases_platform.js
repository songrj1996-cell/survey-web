// Full production page + HTTP/SSE/storage; only external model responses are synthetic.
// Run: node tests/test_postwrite_player_cases_platform.js (NODE_PATH: bundled playwright).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

const repo = path.resolve(__dirname, '..');
const runId = new Date().toISOString().replace(/[:.]/g, '-') + '-' + process.pid;
const output = path.join(repo, '.test-artifacts', 'player-cases-platform', runId);
fs.mkdirSync(output, {recursive: true});
for (const folder of ['vendor', 'tmp', 'browser', 'screenshots']) fs.mkdirSync(path.join(output, folder));
process.env.TEMP = path.join(output, 'tmp');
process.env.TMP = path.join(output, 'tmp');
const results = [], requests = [], responses = [], external = [], pageErrors = [], observations = [], buttonDiagnostics = [], busyChecks = [];
let server, browser, origin;
const save = (name, obj) => fs.writeFileSync(path.join(output, name), JSON.stringify(obj, null, 2));
const read = name => JSON.parse(fs.readFileSync(path.join(output, name), 'utf8'));
const hash = data => crypto.createHash('sha256').update(data).digest('hex');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const reportId = 'synthetic-player-cases';

async function step(name, work, continueOnFailure = false) {
  const started = Date.now();
  try {
    const evidence = await work();
    results.push({name, status: 'passed', elapsed_ms: Date.now() - started, evidence});
    console.log('通过：' + name);
  } catch (error) {
    results.push({name, status: 'failed', elapsed_ms: Date.now() - started, error: error.stack});
    console.log('失败：' + name + '：' + error.message);
    if (!continueOnFailure) throw error;
  } finally { save('assertions.json', results); }
}

async function pageFor(width = 1280) {
  const context = await browser.newContext({viewport: {width, height: 960}, locale: 'zh-CN', timezoneId: 'Asia/Shanghai'});
  await context.route('**/*', route => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.url() === vendorUrl) return route.fulfill({path: path.join(output, 'vendor', 'marked-12.0.0.min.js'), contentType: 'text/javascript'});
    if (url.origin !== origin || /\/export(?:\/|$)/.test(url.pathname)) {
      external.push({url: request.url(), method: request.method()});
      return route.abort('blockedbyclient');
    }
    requests.push({time: new Date().toISOString(), url: request.url(), method: request.method(), body: request.postData()});
    return route.continue();
  });
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('response', async response => {
    if (!response.url().includes('/api/')) return;
    const item = {url: response.url(), status: response.status()};
    responses.push(item);
    try { item.body = await response.text(); } catch (error) { item.read_error = error.message; }
  });
  page.setDefaultTimeout(15000);
  return {page, context};
}

async function openHistory(page, id) {
  await page.locator('#btn-open-history').click();
  await page.locator(`[data-hist-id="${id}"]`).click();
  await page.locator('#report-content').waitFor({state: 'visible'});
  await page.locator('#btn-report-postwrite-review').waitFor({state: 'visible'});
}
async function selectVersion(page, number, text) {
  if (await page.locator('#postwrite-review-drawer').isVisible()) await page.locator('#btn-postwrite-review-close').click();
  const loading = await holdRequest(page, /\/api\/history\/[^/?]+\?version=\d+$/);
  try {
    await page.locator('#report-version-trigger').click();
    await page.locator(`[data-report-version-option="${number}"]`).click();
    await loading.intercepted;
    await checkBusyLocators(page, '加载历史版本V' + number);
  } finally { await loading.release(); }
  await page.waitForFunction(text => document.querySelector('#report-content')?.textContent.includes(text), text);
  assert((await page.locator('[data-postwrite-locate]').count()) > 0);
  assert((await page.locator('[data-postwrite-locate]').evaluateAll(nodes => nodes.every(node => !node.disabled))),
    '当前版本加载后，应恢复可定位按钮');
}

async function holdRequest(page, pattern) {
  let release, arrive, rejectArrival, finish, started = false, handlerError;
  const gate = new Promise(resolve => { release = resolve; });
  const completed = new Promise(resolve => { finish = resolve; });
  const intercepted = new Promise((resolve, reject) => { arrive = resolve; rejectArrival = reject; });
  const timeout = setTimeout(() => rejectArrival(new Error('未观察到预期的真实平台请求')), 15000);
  const handler = async route => {
    started = true;
    clearTimeout(timeout); arrive();
    await gate;
    try {
      const request = route.request();
      assert.equal(new URL(request.url()).origin, origin);
      requests.push({time: new Date().toISOString(), url: request.url(), method: request.method(), body: request.postData()});
      await route.continue();
    } catch (error) { handlerError = error; }
    finally { finish(); }
  };
  await page.route(pattern, handler);
  return {intercepted, release: async () => {
    clearTimeout(timeout); release();
    if (started) await completed;
    await page.unroute(pattern, handler);
    if (handlerError) throw handlerError;
  }};
}

async function checkBusyLocators(page, stage) {
  const nodes = page.locator('[data-postwrite-locate]');
  assert((await nodes.count()) > 0, stage + '前必须有真实核验面板按钮');
  const disabled = await nodes.evaluateAll(items => items.every(item => item.disabled));
  busyChecks.push({stage, disabled}); save('busy-state-checks.json', busyChecks);
  assert(disabled, stage + '期间定位按钮应禁用');
}

async function checkLinkContrast(page, theme) {
  const link = page.locator('#report-content .report-player-case a').first();
  assert.equal(await link.getAttribute('href'), '/#synthetic-reference');
  assert.equal(await link.innerText(), '合成说明');
  const readColors = () => link.evaluate(node => ({foreground: getComputedStyle(node).color,
    background: getComputedStyle(node.closest('.report-player-case')).backgroundColor,
    focusVisible: node.matches(':focus-visible')}));
  function luminance(value) {
    const channels = value.match(/[\d.]+/g).slice(0, 3).map(Number).map(n => n / 255)
      .map(n => n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4);
    return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
  }
  function contrast(colors) {
    const foreground = luminance(colors.foreground), background = luminance(colors.background);
    return (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05);
  }
  await page.mouse.move(0, 0);
  const normal = await readColors();
  await link.hover();
  const hover = await readColors();
  await link.focus();
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Tab');
  const focus = await readColors();
  assert(focus.focusVisible, '必须通过键盘实际聚焦案例链接');
  const evidence = Object.fromEntries(Object.entries({normal, hover, focus}).map(([state, colors]) => {
    const ratio = contrast(colors);
    assert(ratio >= 4.5, theme + '/' + state + ' 案例链接对比度不足：' + ratio);
    return [state, {...colors, ratio}];
  }));
  await page.keyboard.press('Tab');
  await page.mouse.move(0, 0);
  return {theme, ...evidence};
}
async function panel(page, status) {
  await page.locator('#btn-report-postwrite-review').click();
  await page.locator('#postwrite-review-drawer').waitFor({state: 'visible'});
  assert.match(await page.locator('#report-postwrite-audit').innerText(), status);
}
async function screenshot(page, name) {
  await page.screenshot({path: path.join(output, 'screenshots', name + '.png'), fullPage: true});
}
async function verifyCases(page, expected, excluded) {
  const cases = page.locator('#report-content .report-player-case');
  assert.equal(await cases.count(), 2);
  const text = await cases.allTextContents();
  assert(text.join('\n').includes(expected));
  if (excluded) assert(!text.join('\n').includes(excluded));
  assert(!text.join('\n').includes('这是分析正文'));
  assert(!text.join('\n').includes('此段为案例之外'));
  return {cases: text};
}
async function locate(page, sentence, inCore) {
  const card = page.locator('.postwrite-review-issue').filter({has: page.locator('.postwrite-review-sentence', {hasText: sentence})});
  assert.equal(await card.count(), 1);
  if ((await card.getAttribute('open')) === null) await card.locator('summary').click();
  const button = card.getByRole('button', {name: '定位到报告原句', exact: true});
  const diagnostic = {sentence, time: new Date().toISOString(), buttonEnabled: await button.isEnabled(),
    versionTriggerEnabled: await page.locator('#report-version-trigger').isEnabled(),
    resumeEnabled: await page.locator('[data-postwrite-action="resume"]').count()
      ? await page.locator('[data-postwrite-action="resume"]').isEnabled() : null,
    version: await page.locator('#report-version-value').innerText(), cardText: await card.innerText()};
  buttonDiagnostics.push(diagnostic); save('button-diagnostics.json', buttonDiagnostics);
  if (!diagnostic.buttonEnabled) await screenshot(page, 'disabled-location-' + buttonDiagnostics.length);
  assert(diagnostic.buttonEnabled, '版本已加载且版本菜单可用，但定位按钮仍禁用；未绕过按钮状态');
  await button.click();
  const marks = page.locator('#report-content mark.postwrite-report-highlight');
  await marks.first().waitFor({state: 'visible'});
  const evidence = await marks.evaluateAll(nodes => nodes.map(node => ({text: node.textContent,
    inCase: !!node.closest('.report-player-case'), inCore: !!node.closest('.core-summary-box'),
    visible: node.getBoundingClientRect().bottom > 0 && node.getBoundingClientRect().top < innerHeight})));
  assert(evidence.every(item => item.inCase && item.inCore === inCore));
  assert(evidence.some(item => item.visible));
  assert.equal(await page.locator('#postwrite-review-drawer').isVisible(), false);
  return evidence;
}
async function resume(page, id, expected) {
  const responsePromise = page.waitForResponse(response => response.request().method() === 'POST'
    && response.url().endsWith(`/api/report/${id}/retry-failed`));
  const loading = await holdRequest(page, origin + `/api/report/${id}/retry-failed`);
  try {
    await page.locator('[data-postwrite-action="resume"]').click();
    await loading.intercepted;
    await checkBusyLocators(page, '继续核验请求处理中');
  } finally { await loading.release(); }
  const response = await responsePromise;
  assert.equal(response.status(), 200);
  const payload = response.request().postDataJSON();
  assert.equal(payload.history_id, id);
  assert.equal(payload.base_version, 1);
  const raw = await response.text();
  const events = raw.split('\n').filter(line => line.startsWith('data: ')).map(line => JSON.parse(line.slice(6)));
  const done = events.find(event => event.type === 'report_done');
  assert(done, raw);
  assert.equal(done.report_audit.status, expected);
  assert.equal(done.version, 1);
  assert.equal(done.audit_updated, true);
  await page.waitForFunction(() => document.querySelector('#report-generation-panel')?.hidden !== false
    && document.querySelector('#btn-report-postwrite-review')?.getAttribute('hidden') === null);
  // Wait for actual restored report UI, rather than injecting state or invoking render helpers.
  await page.locator('#report-content .report-player-case').first().waitFor({state: 'visible'});
  await page.waitForFunction(() => !document.querySelector('#btn-report-postwrite-review')?.disabled);
  return {payload, events};
}

const html = fs.readFileSync(path.join(repo, 'static/index.html'), 'utf8');
const vendorUrl = /<script src="([^"]*marked@12\.0\.0\/marked\.min\.js)"/.exec(html)?.[1];
const fontUrls = new Set([...html.matchAll(/(?:href|src)="(https:\/\/fonts\.googleapis\.com\/[^\"]+)"/g)]
  .map(match => match[1].replaceAll('&amp;', '&')));

async function main() {
  let fatal;
  try {
    await step('依赖与隔离启动', async () => {
      assert(vendorUrl, '页面未声明预期的marked版本，停止而不替换依赖');
      const response = await fetch(vendorUrl, {signal: AbortSignal.timeout(30000)});
      assert.equal(response.status, 200);
      const vendor = Buffer.from(await response.arrayBuffer());
      fs.writeFileSync(path.join(output, 'vendor', 'marked-12.0.0.min.js'), vendor);
      save('vendor.json', {url: vendorUrl, sha256: hash(vendor), bytes: vendor.length, version: '12.0.0'});
      const port = await new Promise((resolve, reject) => {
        const socket = net.createServer();
        socket.once('error', reject);
        socket.listen(0, '127.0.0.1', () => {const port = socket.address().port; socket.close(() => resolve(port));});
      });
      origin = `http://127.0.0.1:${port}`;
      const log = fs.openSync(path.join(output, 'server.log'), 'a');
      server = spawn('python', ['-X', `pycache_prefix=${path.join(output, 'pycache')}`,
        path.join(repo, 'scripts/verify_postwrite_player_cases_platform.py'), '--output', output, '--port', String(port)],
        {cwd: output, env: {...process.env, PYTHONIOENCODING: 'utf-8'}, windowsHide: true, stdio: ['ignore', log, log]});
      const deadline = Date.now() + 45000;
      let ready = false;
      while (Date.now() < deadline) {
        if (server.exitCode !== null) throw new Error('隔离平台未启动：见server.log，退出码 ' + server.exitCode);
        try { if ((await fetch(origin, {signal: AbortSignal.timeout(1000)})).ok) {ready = true; break;} } catch {}
        await sleep(250);
      }
      assert(ready, '隔离平台启动超时');
      browser = await chromium.launch({headless: true, channel: process.env.PLAYER_CASE_BROWSER_CHANNEL || undefined,
        downloadsPath: path.join(output, 'browser'), env: {...process.env}});
      const isolation = read('isolation.json');
      assert.equal(isolation.rejected_writes.length, 0);
      assert.equal(isolation.network_blocks.length, 1);
      return {origin, isolation, browserVersion: browser.version()};
    });

    const {page, context} = await pageFor();
    try {
      await step('完整平台 → 历史V2 → V1 → V2，案例和核验面板随版本切换', async () => {
        await page.goto(origin);
        await openHistory(page, reportId);
        await verifyCases(page, '只属于V2', 'R73');
        await panel(page, /本版核验已完成/);
        assert.equal(await page.locator('[data-postwrite-action="resume"]').count(), 0);
        await page.locator('#btn-postwrite-review-close').click();
        await selectVersion(page, 1, '合成观点洞察 V1');
        await verifyCases(page, 'R73', '只属于V2');
        await panel(page, /核验尚未完成/);
        assert.equal(await page.locator('.postwrite-review-issue').count(), 1);
        await screenshot(page, 'desktop-v1-panel');
        await page.locator('#btn-postwrite-review-close').click();
        await selectVersion(page, 2, '合成观点洞察 V2');
        await panel(page, /本版核验已完成/);
        assert.equal(await page.locator('.postwrite-review-issue').count(), 2);
        await page.locator('#btn-postwrite-review-close').click();
        await selectVersion(page, 1, '合成观点洞察 V1');
        assert.equal(await page.locator('#report-content .report-player-case a[href="/#synthetic-reference"]').count(), 1);
        assert.equal(await page.locator('#report-content .report-player-case strong').filter({hasText: '单排受挫'}).count(), 1);
        await screenshot(page, 'desktop-v1-cases');
      });
      await step('核验面板按钮 → 核心结论案例原句，跨章节重复句不误定位', async () => {
        await panel(page, /核验尚未完成/);
        const evidence = await locate(page, '单排受挫', true);
        assert.equal(await page.locator('#report-content .report-player-case a[href="/#synthetic-reference"]').count(), 1);
        assert.equal(await page.locator('#report-content .report-player-case strong').filter({hasText: '单排受挫'}).count(), 1);
        await screenshot(page, 'desktop-v1-core-location');
        return evidence;
      }, true);
      await step('继续核验 → 真实HTTP/SSE/保存 → 原版本页面刷新和新问题定位', async () => {
        if (!(await page.locator('#postwrite-review-drawer').isVisible())) await panel(page, /核验尚未完成/);
        const evidence = await resume(page, reportId, 'complete');
        await panel(page, /本版核验已完成/);
        assert.equal(await page.locator('.postwrite-review-issue').count(), 2);
        assert.equal(await page.locator('[data-postwrite-action="resume"]').count(), 0);
        evidence.location = await locate(page, 'R54：版本变化', false);
        await verifyCases(page, 'R73', '只属于V2');
        await screenshot(page, 'desktop-v1-after-resume');
        return evidence;
      }, true);
      await step('切换其他版本、重进历史和刷新浏览器后，原版本核验结果保持', async () => {
        await selectVersion(page, 2, '合成观点洞察 V2');
        await verifyCases(page, '只属于V2', 'R73');
        await selectVersion(page, 1, '合成观点洞察 V1');
        await panel(page, /本版核验已完成/);
        await page.locator('#btn-postwrite-review-close').click();
        await openHistory(page, reportId);
        await selectVersion(page, 1, '合成观点洞察 V1');
        await panel(page, /本版核验已完成/);
        await page.locator('#btn-postwrite-review-close').click();
        await page.reload();
        await openHistory(page, reportId);
        await selectVersion(page, 1, '合成观点洞察 V1');
        await panel(page, /本版核验已完成/);
        await screenshot(page, 'desktop-v1-reloaded-panel');
      });
      await step('刷新并重新选择V1后，面板按钮定位', async () => {
        return await locate(page, 'R54：版本变化', false);
      }, true);
    } catch { await screenshot(page, 'desktop-failure'); }
    finally { await context.close(); }

    const mobile = await pageFor(1280);
    try {
      await step('390px已载入的完整页面：版本菜单、案例换行、明暗主题和核验面板', async () => {
        const page = mobile.page;
        await page.goto(origin);
        await openHistory(page, reportId);
        await page.setViewportSize({width: 390, height: 960});
        if (!(await page.locator('#btn-open-history').isVisible())) {
          observations.push({severity: 'limitation', scope: '现有完整平台窄屏入口',
            problem: '390px下侧栏整体隐藏，历史入口与主题按钮不可见。只验证桌面载入后缩窄的报告操作，不声称窄屏首次进入历史已通过。',
            source: 'static/style.css @media (max-width: 640px) .sidebar {display:none}', production_fixed: false});
        }
        await selectVersion(page, 1, '合成观点洞察 V1');
        await verifyCases(page, 'R73', '只属于V2');
        const sizes = await page.locator('.report-player-case').evaluateAll(nodes => nodes.map(node => ({
          width: node.getBoundingClientRect().width, scrollWidth: node.scrollWidth, clientWidth: node.clientWidth,
          background: getComputedStyle(node).backgroundColor, color: getComputedStyle(node).color})));
        assert(sizes.every(item => item.scrollWidth <= item.clientWidth + 1 && item.width <= 390));
        const lightContrast = await checkLinkContrast(page, 'light');
        await screenshot(page, 'mobile-light');
        await page.setViewportSize({width: 1280, height: 960});
        await page.locator('#btn-theme').click();
        await page.setViewportSize({width: 390, height: 960});
        const dark = await page.locator('.report-player-case').first().evaluate(node => ({background: getComputedStyle(node).backgroundColor,
          color: getComputedStyle(node).color}));
        assert.notEqual(dark.background, sizes[0].background);
        const darkContrast = await checkLinkContrast(page, 'dark');
        await screenshot(page, 'mobile-dark');
        await panel(page, /本版核验已完成/);
        await screenshot(page, 'mobile-panel');
        return {sizes, dark, lightContrast, darkContrast};
      });
      await step('390px切换版本后，通过面板按钮定位', async () => {
        const evidence = await locate(mobile.page, '单排受挫', true);
        await screenshot(mobile.page, 'mobile-location');
        return evidence;
      });
    } catch { await screenshot(mobile.page, 'mobile-failure'); }
    finally { await mobile.context.close(); }

    const failure = await pageFor();
    try {
      await step('合成核验失败 → 正文保留 → 再次继续同版本 → 刷新和定位恢复', async () => {
        const page = failure.page, id = reportId + '-failure';
        await page.goto(origin);
        await openHistory(page, id);
        await panel(page, /核验尚未完成/);
        const first = await resume(page, id, 'partial');
        await panel(page, /核验尚未完成/);
        assert.equal(await page.locator('.postwrite-review-issue').count(), 1);
        await screenshot(page, 'controlled-failure-preserved');
        const second = await resume(page, id, 'complete');
        await panel(page, /本版核验已完成/);
        assert.equal(await page.locator('.postwrite-review-issue').count(), 2);
        await locate(page, 'R54：版本变化', false);
        await page.reload();
        await openHistory(page, id);
        await panel(page, /本版核验已完成/);
        await locate(page, '单排受挫', true);
        await verifyCases(page, 'R73', '只属于V2');
        await screenshot(page, 'controlled-failure-recovered');
        return {first, second};
      });
    } catch { await screenshot(failure.page, 'recovery-failure'); }
    finally { await failure.context.close(); }

    await step('存储、调用、外部访问和生产文件完整性复核', async () => {
      const before = read('before-history.json'), after = read('data/history.json');
      save('after-history.json', after);
      for (const original of before) {
        const current = after.find(item => item.id === original.id);
        assert.equal(current.report_versions.length, original.report_versions.length);
        for (const snapshot of original.report_versions) {
          const saved = current.report_versions.find(item => item.version === snapshot.version);
          assert.equal(saved.report_md, snapshot.report_md);
          assert.deepEqual(saved.input_snapshot, snapshot.input_snapshot);
          if (snapshot.version === 2) assert.deepEqual(saved, snapshot);
        }
      }
      const calls = read('collector-calls.json'), actual = calls.filter(item => item.phase === 'browser');
      assert.equal(actual.length, 3);
      assert(actual.every(item => item.stage === 'meaning' && JSON.stringify(item.targets) === '["C002"]'));
      assert.equal(actual.filter(item => item.result === 'controlled_failure').length, 1);
      assert(external.every(item => item.method === 'GET' && fontUrls.has(item.url)),
        '除了页面声明的字体样式请求，不应出现任何外部或导出尝试：' + JSON.stringify(external));
      assert.equal(pageErrors.length, 0);
      assert(busyChecks.length > 3 && busyChecks.every(item => item.disabled));
      const isolation = read('isolation.json');
      assert.equal(isolation.rejected_writes.length, 0);
      assert(isolation.network_blocks.every(item => item.expected_probe));
      for (const [file, digest] of Object.entries(read('source-hashes.json'))) assert.equal(hash(fs.readFileSync(path.join(repo, file))), digest, file);
      assert(!requests.some(item => /\/export(?:\/|$)|\/versions$/.test(new URL(item.url).pathname)));
      const errors = responses.filter(item => item.status >= 400);
      // The optional Google Forms router is deliberately disabled in this process.
      // Keep its automatic capability probe visible; never swallow report/history failures.
      const optionalCapabilityErrors = errors.filter(item => item.status === 404
        && new URL(item.url).pathname === '/api/questionnaire-sources/capabilities');
      const unexpectedErrors = errors.filter(item => !optionalCapabilityErrors.includes(item));
      assert.equal(unexpectedErrors.length, 0, JSON.stringify(unexpectedErrors));
      observations.push({severity: 'environment', problem: '隔离配置关闭Google Forms，页面自动能力检查返回404；未修改对应接口。',
        path: '/api/questionnaire-sources/capabilities', count: optionalCapabilityErrors.length});
      save('before-after.json', {unchanged_bodies_and_inputs: true, unchanged_version_counts: true,
        untouched_v2: true, browser_collector_calls: actual, production_sources_unchanged: true,
        external_font_requests_blocked: external.length, external_connections_allowed: 0,
        optional_capability_404: optionalCapabilityErrors.length, target_http_errors: unexpectedErrors});
      return {actual, isolation, pageErrors, httpErrors: unexpectedErrors, blockedFontRequests: external.length,
        optionalCapabilityErrors};
    });
  } catch (error) { fatal = error.stack; }
  finally {
    await browser?.close();
    if (server && server.exitCode === null) {
      server.kill();
      await Promise.race([new Promise(resolve => server.once('exit', resolve)), sleep(5000)]);
    }
    save('requests.json', requests); save('responses.json', responses);
    save('browser-errors.json', {external, pageErrors});
    save('result.json', {scope: '观点洞察 → 先写作再核验', runId, output, results,
      passed: !fatal && results.every(item => item.status === 'passed'), observations, fatal,
      real_model_calls: 0, real_feishu_exports: 0, production_sources_unchanged_during_run: true,
      limitations: ['固定合成核验响应，不验证真实模型语义质量和耗时', '未做真实飞书导出',
        '外部字体请求被阻止，截图使用系统回退字体', '390px历史入口不可见，布局验证使用桌面载入后缩窄'],
      stopped_server: !server || server.exitCode !== null || server.signalCode !== null});
    console.log('证据目录：' + output);
    if (fatal || results.some(item => item.status === 'failed')) process.exitCode = 1;
  }
}
main().catch(error => {save('fatal.json', {error: error.stack}); console.error(error); process.exitCode = 1;});
