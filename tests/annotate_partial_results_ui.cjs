// Executable UI behavior checks using the production scripts and a minimal DOM double.
// No browser dependencies, network, or real model calls are needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const source = name => fs.readFileSync(path.join(root, 'static/js/features', name), 'utf8');
const coreSource = fs.readFileSync(path.join(root, 'static/js/core/core.js'), 'utf8');
const sseGetSource = coreSource.slice(
  coreSource.indexOf('async function consumeSSEGet'),
  coreSource.indexOf('// ── SSE from POST', coreSource.indexOf('async function consumeSSEGet')),
);

function jsonResponse(status, body) {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300, status,
    headers: {get: name => name.toLowerCase() === 'content-type' ? 'application/json' : ''},
    text: async () => text, json: async () => body,
  };
}

function sseResponse(chunks) {
  let index = 0;
  return {
    ok: true, status: 200,
    headers: {get: name => name.toLowerCase() === 'content-type' ? 'text/event-stream; charset=utf-8' : ''},
    body: {getReader: () => ({
      read: async () => index < chunks.length ? {done: false, value: chunks[index++]} : {done: true},
      cancel: async () => {},
    })},
  };
}

const completion = (ids, total = 2, parts = ['第2列质量判断']) => ({
  partial: ids.length > 0, total, complete: total - ids.length, missing_ids: ids,
  gaps: Object.fromEntries(ids.map(id => [id, parts])),
});

const flushAsync = () => new Promise(resolve => setImmediate(resolve));

function sseEnvironment(fetch) {
  const context = {console, fetch, TextDecoder, Uint8Array, Set};
  vm.createContext(context);
  vm.runInContext(sseGetSource, context);
  return context;
}

function environment() {
  const nodes = new Map(), downloads = [], messages = [];
  class Element {
    constructor(id = '') { this.id = id; this.style = {}; this.dataset = {}; this.events = {}; this.hidden = false;
      this.value = 'all'; this.checked = false; this.disabled = false; this.children = new Map(); this.inputs = [];
      this.classList = {add() {}, remove() {}, toggle() {}}; }
    set innerHTML(value) {
      this.html = value;
      this.inputs = [...value.matchAll(/data-partial-id value="([^"]+)"/g)].map(match => {
        const input = new Element(); input.value = match[1]; return input;
      });
    }
    get innerHTML() { return this.html || ''; }
    addEventListener(type, action) { this.events[type] = action; }
    setAttribute() {}
    append() {}
    appendChild() {}
    remove() {}
    scrollTo() {}
    focus() {}
    click() { if (this.download) downloads.push({name: this.download, href: this.href}); }
    insertAdjacentElement(where, element) { nodes.set(element.id, element); }
    querySelector(selector) {
      if (!this.children.has(selector)) this.children.set(selector, new Element(selector));
      return this.children.get(selector);
    }
    querySelectorAll(selector) {
      if (selector === '[data-partial-id]') return this.inputs;
      if (selector === '[data-partial-id]:checked') return this.inputs.filter(input => input.checked);
      return [];
    }
  }
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, new Element(id));
    return nodes.get(id);
  };
  const context = {console, URLSearchParams, Blob, AbortController, Set, Map, TextDecoder, Uint8Array,
    URL: {createObjectURL: () => 'blob:result', revokeObjectURL() {}},
    setTimeout: () => 1, clearTimeout() {}, $, annPanels: [],
    esc: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;'),
    formatReportDuration: () => '', showToast: (message, type) => messages.push({message, type}),
    fetch: async () => ({ok: false, status: 400, json: async () => ({detail: '模拟下载失败'})}),
    document: {getElementById: $, querySelectorAll: () => [], querySelector: $,
      createElement: () => new Element(), body: new Element()},
  };
  context.window = context;
  context.location = {href: 'http://test/task'};
  vm.createContext(context);
  vm.runInContext(sseGetSource, context);
  vm.runInContext(source('annotate.js'), context);
  vm.runInContext(source('annotate_review.js'), context);
  const run = code => vm.runInContext(code, context);
  run(`Object.assign(annState, {
    sessionId: 'original-task', tasks: {quality: true, ai_detect: false}, openTextCols: [1],
    missingQualityIds: ['P1', 'P2'], missingOverallIds: ['P1', 'P2'],
    completion: {partial:true, total:2, complete:0, missing_ids:['P1','P2'], gaps:{P1:['质量判断'],P2:['质量判断']}},
    qualityResults: [], historySaved:true
  });`);
  return {context, run, $, messages, downloads};
}

async function check() {
  // Production GET SSE reader: chunked UTF-8, multiple events, real errors and early EOF.
  const payload = Buffer.from(
    'data: {"type":"progress","msg":"处理中"}\r\n\r\n'
    + 'data: {"type":"quality_done","count":1}\r\n\r\n',
  );
  const events = [];
  let sse = sseEnvironment(async () => sseResponse([
    new Uint8Array(payload.subarray(0, 38)),
    new Uint8Array(payload.subarray(38, 55)),
    new Uint8Array(payload.subarray(55)),
  ]));
  const terminal = await sse.consumeSSEGet('/stream', event => events.push(event), ['quality_done']);
  assert.equal(events[0].msg, '处理中');
  assert.equal(events.length, 2, 'multiple events in chunked stream are dispatched');
  assert.equal(terminal.type, 'quality_done');

  sse = sseEnvironment(async () => jsonResponse(400, {detail: '请选择当前仍有缺项的玩家'}));
  await assert.rejects(() => sse.consumeSSEGet('/stream', () => {}, ['quality_done']), /请选择当前仍有缺项的玩家/);
  sse = sseEnvironment(async () => sseResponse([new TextEncoder().encode('data: {"type":"error","message":"模型失败"}\n\n')]));
  await assert.rejects(() => sse.consumeSSEGet('/stream', () => {}, ['quality_done']), /模型失败/);
  sse = sseEnvironment(async () => sseResponse([new TextEncoder().encode('data: {"type":"progress"}\n\n')]));
  await assert.rejects(() => sse.consumeSSEGet('/stream', () => {}, ['quality_done']), /尚未收到完整结果/);
  sse = sseEnvironment(async () => sseResponse([new TextEncoder().encode('data: {"type":"quality_done"}\n\n')]));
  await assert.rejects(() => sse.consumeSSEGet('/stream', () => { throw new Error('渲染失败'); }, ['quality_done']), /渲染失败/);

  // Existing partial download behavior remains safe.
  let env = environment(), {context, run, $, messages, downloads} = env;
  run('annShowDone({refreshCompletion:false})');
  assert.equal($('ann-btn-download').disabled, false, 'all-failed results remain downloadable');
  assert.match($('ann-done-text').innerHTML, /部分完成/);
  await run('annDownloadResults()');
  assert.equal(context.location.href, 'http://test/task', 'HTTP error never navigates away');
  assert.match(messages.at(-1).message, /模拟下载失败/);
  assert.equal(downloads.length, 0);

  // AI-stage stale completion and stale missing arrays are replaced by the archived one-row truth.
  env = environment(); ({context, run, $, messages} = env);
  context.fetch = async url => {
    assert.match(String(url), /\/api\/history\/original-task$/);
    return jsonResponse(200, {id: 'original-task', annotate_completion: completion(['P1'])});
  };
  run('annShowDone()');
  assert.equal(run('annState.partialRetrySyncing'), true, 'retry is disabled while authoritative state loads');
  await flushAsync(); await flushAsync();
  assert.equal(run('annState.partialRetrySyncing'), false);
  assert.deepEqual([...run('annState.missingQualityIds')], ['P1']);
  assert.deepEqual([...run('annState.missingOverallIds')], []);
  assert.equal($('ann-partial-retry').inputs.length, 1);
  assert.equal($('ann-partial-retry').inputs[0].value, 'P1');

  // A second server check filters players completed since the panel was rendered.
  run(`Object.assign(annState, {
    completion: ${JSON.stringify(completion(['P1', 'P2']))},
    missingQualityIds:['P1','P2'], missingOverallIds:[], missingTranslationIds:[],
    partialRetrySyncedCompletion:null
  }); annShowDone({refreshCompletion:false});`);
  const urls = [];
  context.fetch = async () => jsonResponse(200, {id: 'original-task', annotate_completion: completion(['P1'])});
  context.consumeSSEGet = async (url, onEvent) => {
    urls.push(url);
    onEvent({type:'quality_done', count:2, complete_count:2, results:[],
      missing_ids:[], missing_overall_ids:[], missing_translation_ids:[], history_saved:true,
      completion:completion([])});
    return {type:'quality_done'};
  };
  let box = $('ann-partial-retry');
  box.querySelectorAll('[data-partial-id]').forEach(input => { input.checked = true; });
  await box.querySelector('[data-partial-run]').onclick();
  assert.equal(urls.length, 1);
  assert.match(urls[0], /retry_ids=P1/);
  assert.doesNotMatch(urls[0], /retry_ids=P2/);
  assert.ok(messages.some(item => /1 位已完成，本次只补 1 位/.test(item.message)));

  // If every selected player completed meanwhile, refresh the panel without calling the model.
  env = environment(); ({context, run, $, messages} = env);
  run('annShowDone({refreshCompletion:false})');
  context.fetch = async () => jsonResponse(200, {id: 'original-task', annotate_completion: completion([])});
  let modelCalls = 0;
  context.consumeSSEGet = async () => { modelCalls += 1; };
  box = $('ann-partial-retry'); box.querySelectorAll('[data-partial-id]')[0].checked = true;
  await box.querySelector('[data-partial-run]').onclick();
  assert.equal(modelCalls, 0);
  assert.equal($('ann-partial-retry').hidden, true);
  assert.ok(messages.some(item => /当前都已完成/.test(item.message)));

  // A delayed history response from an old task cannot overwrite a newly opened task.
  env = environment(); ({context, run} = env);
  let resolveHistory;
  context.fetch = () => new Promise(resolve => { resolveHistory = resolve; });
  const staleRefresh = run('annRefreshPartialRetry()');
  run(`annState.sessionId='new-task'; annState.partialRetrySyncVersion += 1;
    annState.partialRetrySyncing=false; annState.completion=${JSON.stringify(completion(['NEW']))};`);
  resolveHistory(jsonResponse(200, {id: 'original-task', annotate_completion: completion(['OLD'])}));
  await staleRefresh;
  assert.deepEqual([...run('annState.completion.missing_ids')], ['NEW']);

  // A failed GET handshake surfaces the backend detail, never a generic disconnect.
  env = environment(); ({context, run, messages} = env);
  context.fetch = async () => jsonResponse(400, {detail: '所选玩家当前都没有缺项，请刷新结果后重新选择'});
  await run("annRunQuality({retryIds:['P1'], preserveReview:true})");
  assert.ok(messages.some(item => /所选玩家当前都没有缺项/.test(item.message)));
  assert.ok(!messages.some(item => /连接中断/.test(item.message)));

  // If history is unavailable, selection remains usable and the backend is still the final filter.
  env = environment(); ({context, run, $, messages} = env);
  run('annShowDone({refreshCompletion:false})');
  context.fetch = async () => jsonResponse(404, {detail: '历史记录不存在'});
  const fallbackUrls = [];
  context.consumeSSEGet = async (url, onEvent) => {
    fallbackUrls.push(url);
    onEvent({type:'quality_done', count:1, complete_count:1, results:[], missing_ids:['P2'],
      missing_overall_ids:[], missing_translation_ids:[], history_saved:true, completion:completion(['P2'])});
    return {type:'quality_done'};
  };
  box = $('ann-partial-retry'); box.querySelectorAll('[data-partial-id]')[0].checked = true;
  await box.querySelector('[data-partial-run]').onclick();
  assert.equal(fallbackUrls.length, 1);
  assert.ok(messages.some(item => /未能校正最新缺项/.test(item.message)));

  console.log('PASS: authoritative retry sync, mixed selection filtering, HTTP detail, safe fallback, and GET SSE framing');
}

function serveFixture() {
  const http = require('node:http');
  const page = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8')
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '')
    .replace('</body>', `<style>.sidebar{display:none}.main{margin-left:0;width:100%}#ann-partial-retry{padding:20px;border:1px solid #ccc;border-radius:12px;margin:20px 0}#ann-partial-retry button{margin:8px;padding:8px}</style>
      <script>
      const $=id=>document.getElementById(id);
      const annPanels=Array.from({length:6},(_,i)=>$('ann-panel-'+(i+1)));
      const esc=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
      const formatReportDuration=()=>'';
      const showToast=(message)=>{const p=document.createElement('p');p.setAttribute('role','alert');p.textContent=message;$('toast-container').appendChild(p)};
      </script>
      <script src="/static/js/features/annotate.js"></script>
      <script src="/static/js/features/annotate_review.js"></script>
      <script>
      document.querySelectorAll('.panel').forEach(p=>p.classList.add('panel--hidden'));
      Object.assign(annState,{sessionId:'synthetic',tasks:{quality:true,ai_detect:false},headers:['玩家','是否遇到问题'],openTextCols:[1],qualityCount:0,
        missingQualityIds:['P1','P2'],missingOverallIds:['P1','P2'],historySaved:true,
        completion:{partial:true,total:2,complete:0,missing_ids:['P1','P2'],gaps:{P1:['质量判断'],P2:['质量判断']}},
        qualityResults:['P1','P2'].map(id=>({id,originals:{col_1:'没有问题'},q_labels:{},q_reasons:{},overall_pending:true}))});
      async function consumeSSEGet(url, callback){
        const selected=new URL(url,location.href).searchParams.getAll('retry_ids');
        const results=annState.qualityResults.map(r=>selected.includes(r.id)?{...r,q_labels:{col_1:'有效反馈'},q_reasons:{col_1:'明确回答没有问题'},overall:'有效反馈',overall_pending:false,overall_reason:'回答满足题意'}:r);
        const missing=results.filter(r=>r.overall_pending).map(r=>r.id);
        callback({type:'quality_done',results,count:2,complete_count:2-missing.length,missing_ids:missing,missing_overall_ids:missing,missing_translation_ids:[],history_saved:true,
          completion:{partial:missing.length>0,total:2,complete:2-missing.length,missing_ids:missing,gaps:Object.fromEntries(missing.map(id=>[id,['质量判断']]))}});
      }
      annGoStep(6);annShowDone({quiet:true});
      </script></body>`);
  http.createServer((request, response) => {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    if (pathname === '/') { response.setHeader('Content-Type','text/html; charset=utf-8'); response.end(page); return; }
    if (pathname.startsWith('/api/')) { response.writeHead(400, {'Content-Type':'application/json'}); response.end(JSON.stringify({detail:'模拟下载失败：页面和已有结果必须保留'})); return; }
    const file = path.resolve(root, '.' + pathname);
    if (!file.startsWith(path.join(root, 'static') + path.sep) || !fs.existsSync(file)) { response.writeHead(404); response.end(); return; }
    const type = file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'application/octet-stream';
    response.setHeader('Content-Type',type); fs.createReadStream(file).pipe(response);
  }).listen(8013, '127.0.0.1', () => console.log('Synthetic UI fixture http://127.0.0.1:8013/ (no model calls, no data writes)'));
}

if (require.main === module) {
  if (process.argv.includes('--serve')) serveFixture();
  else check().catch(error => { console.error(error); process.exitCode = 1; });
}
