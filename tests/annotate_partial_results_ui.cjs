// Executable UI behavior checks using the production scripts and a minimal DOM double.
// No browser dependencies, network, or real model calls are needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const source = name => fs.readFileSync(path.join(root, 'static/js/features', name), 'utf8');

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
  const context = {console, URLSearchParams, Blob, AbortController, Set, Map,
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
  const env = environment(), {context, run, $, messages, downloads} = env;
  run('annShowDone()');
  assert.equal($('ann-btn-download').disabled, false, 'all-failed results remain downloadable');
  assert.match($('ann-done-text').innerHTML, /部分完成/);
  assert.match($('ann-partial-retry').innerHTML, /P1/);
  await run('annDownloadResults()');
  assert.equal(context.location.href, 'http://test/task', 'HTTP error never navigates away');
  assert.match(messages.at(-1).message, /模拟下载失败/);
  assert.equal(downloads.length, 0);
  assert.equal(run('annState.sessionId'), 'original-task');
  context.fetch = async () => ({ok: true, headers: {get: () => "attachment; filename*=UTF-8''result.xlsx"}, blob: async () => new Blob(['synthetic'])});
  await run('annDownloadResults()');
  assert.equal(downloads.length, 1);
  assert.equal(downloads[0].name, 'result.xlsx');
  assert.equal(context.location.href, 'http://test/task');
  run('annState.partialRetryRunning = true');
  await run('annDownloadResults()');
  assert.equal(downloads.length, 1, 'no overlapping download during retry');
  run('annState.partialRetryRunning = false');

  const urls = [];
  context.consumeSSE = async (url, onEvent) => {
    urls.push(url);
    onEvent({type:'quality_done', count:1, complete_count:1,
      results:[{id:'P1', q_labels:{col_1:'有效反馈'}, q_reasons:{col_1:'回答了问题'}, originals:{col_1:'没有'}, overall:'有效反馈', overall_reason:'完整回答'}],
      missing_ids:['P2'], missing_overall_ids:['P2'], missing_translation_ids:[], history_saved:true,
      completion:{partial:true,total:2,complete:1,missing_ids:['P2'],gaps:{P2:['质量判断']}}});
  };
  let box = $('ann-partial-retry');
  await box.querySelector('[data-partial-run]').onclick();
  assert.equal(urls.length, 0, 'no selection does not call the model');
  box.querySelectorAll('[data-partial-id]')[0].checked = true;
  await box.querySelector('[data-partial-run]').onclick();
  assert.match(urls[0], /retry_ids=P1/);
  assert.doesNotMatch(urls[0], /P2/);
  assert.equal(run('annState.currentStep'), 6);
  assert.equal(run('annState.partialRetryRunning'), false);
  assert.equal(run('annState.sessionId'), 'original-task');
  assert.equal($('ann-btn-download').disabled, false);
  assert.equal($('ann-partial-retry').inputs.length, 1);
  assert.equal($('ann-partial-retry').inputs[0].value, 'P2');
  run('annState.historySaved = false; annShowDone()');
  assert.match($('ann-done-text').innerHTML, /历史保存未成功/);
  assert.equal($('ann-btn-download').disabled, false);
  console.log('PASS: partial/all-failed export, safe download, selection, busy guard, retry refresh, history warning (24 assertions)');
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
      async function consumeSSE(url, callback){
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
