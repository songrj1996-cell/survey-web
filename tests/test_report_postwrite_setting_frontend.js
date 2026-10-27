const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/features/settings.js'), 'utf8');
const start = source.indexOf('async function loadSystemSettings(');
const end = source.indexOf("\nwindow.addEventListener('beforeunload'", start);
assert(start >= 0 && end > start, 'Settings loader and event section must exist before running the fixture');
const section = source.slice(start, end);

function fixture({enabled = false, canUse = true, patchError = null, deferPatch = false} = {}) {
  const nodes = new Map(), handlers = {}, requests = [], toasts = [];
  const saved = {report_postwrite_enabled:enabled, report_quick_mode_enabled:false,
    group_management_preferences:{only_self:true, revision:1}};
  let release, styleRefreshes = 0;
  function panel(id) {
    const element = {
      _html:'', inputs:[],
      set innerHTML(value) {
        for (const input of this.inputs) if (nodes.get(input.id) === input) nodes.delete(input.id);
        this.inputs = [];
        this._html = value;
        for (const match of value.matchAll(/<input\b([^>]+)>/g)) {
          const inputId = /id="([^"]+)"/.exec(match[1])?.[1];
          if (!inputId) continue;
          const input = {id:inputId, checked:/\bchecked\b/.test(match[1]), disabled:false,
            closest(selector) {return selector.split(',').some(s => s.trim() === '#' + inputId) ? this : null;}};
          this.inputs.push(input);
          nodes.set(inputId, input);
        }
      },
      get innerHTML() {return this._html;},
      querySelectorAll() {return this.inputs;},
    };
    nodes.set(id, element);
    return element;
  }
  const body = panel('stab-content-functions'), platformBody = panel('stab-content-system');
  nodes.set('settings-content', {addEventListener(name, handler) {handlers[name] = handler;}});
  const fetch = async (url, options = {}) => {
    requests.push({url, ...options});
    if (options.method === 'PATCH') {
      if (deferPatch) await new Promise(resolve => {release = resolve;});
      if (patchError === 'network') throw new Error('合成网络中断');
      if (patchError) return {ok:false, json:async () => ({detail:'当前功能只读，不能保存'})};
      Object.assign(saved, JSON.parse(options.body));
    }
    return {ok:true, json:async () => ({...saved})};
  };
  const ctx = vm.createContext({$:id=>nodes.get(id), fetch, esc:String,
    PlatformPermissions:{canUse:()=>canUse}, showToast:(...args)=>toasts.push(args),
    state:{currentStep:2, sessionId:'synthetic', reportStyleSelection:{locked:false}},
    loadReportStyleOptions:async()=>{styleRefreshes++;},
  });
  vm.runInContext(section, ctx);
  return {nodes, body, platformBody, requests, toasts, saved,
    load:(section = 'functions')=>vm.runInContext('loadSystemSettings(' + JSON.stringify(section) + ')', ctx),
    change:id=>{const input=nodes.get(id);input.checked=!input.checked;return handlers.change({target:input});},
    release:()=>release(), styleRefreshes:()=>styleRefreshes};
}

test('switch renders the saved state and scope, with no request that enables it on load', async () => {
  const f=fixture(); await f.load();
  assert.equal(f.nodes.get('setting-report-postwrite').checked, false);
  assert.match(f.body.innerHTML, /先写作后核验/);
  assert.match(f.body.innerHTML, /保存后立即生效，无需重启/);
  assert.match(f.body.innerHTML, /不会打断正在运行的任务/);
  assert.match(f.body.innerHTML, /所有用户生效/);
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].method, undefined);
});

test('save sends only this setting, and reload keeps the selected state', async () => {
  const f=fixture();await f.load();await f.change('setting-report-postwrite');
  assert.deepEqual(JSON.parse(f.requests.at(-1).body), {report_postwrite_enabled:true});
  assert.equal(f.nodes.get('setting-report-postwrite').checked, true);
  assert.match(f.toasts.at(-1)[0], /先写作后核验已开启/);
  assert.equal(f.styleRefreshes(), 0);
  await f.load();assert.equal(f.nodes.get('setting-report-postwrite').checked, true);
  await f.change('setting-report-postwrite');
  assert.equal(f.saved.report_postwrite_enabled, false);
  assert.match(f.toasts.at(-1)[0], /已关闭/);
});

for (const patchError of ['403', 'network']) {
  test(`save failure ${patchError} restores the previous state with a readable message`, async () => {
    const f=fixture({patchError});await f.load();await f.change('setting-report-postwrite');
    const input=f.nodes.get('setting-report-postwrite');
    assert.equal(input.checked, false);assert.equal(input.disabled, false);
    assert.equal(f.saved.report_postwrite_enabled, false);
    assert.match(f.toasts.at(-1)[0], /保存失败/);
    assert.equal(f.toasts.at(-1)[1], 'error');
  });
}

test('read-only platform access displays the value but disables editing', async () => {
  const f=fixture({enabled:true, canUse:false});await f.load();
  assert.equal(f.nodes.get('setting-report-postwrite').checked, true);
  assert.equal(f.nodes.get('setting-report-postwrite').disabled, true);
});

test('saving disables the switch until the response completes', async () => {
  const f=fixture({deferPatch:true});await f.load();
  const change=f.change('setting-report-postwrite');
  assert.equal(f.nodes.get('setting-report-postwrite').disabled, true);
  f.release();await change;
  assert.equal(f.nodes.get('setting-report-postwrite').disabled, false);
});

test('the existing quick-report setting keeps its own request and refresh behavior', async () => {
  const f=fixture();await f.load();await f.change('setting-report-quick-mode');
  assert.deepEqual(JSON.parse(f.requests.at(-1).body), {report_quick_mode_enabled:true});
  assert.equal(f.styleRefreshes(), 1);
  assert.equal(f.saved.report_postwrite_enabled, false);
});

test('platform preferences stay separate from business switches without writing on either page load', async () => {
  const f=fixture({enabled:true});
  await f.load();
  assert.doesNotMatch(f.body.innerHTML, /setting-group-only-self/);
  const functionsMarkup=f.body.innerHTML;
  await f.load('system');
  assert.match(f.platformBody.innerHTML, /setting-group-only-self/);
  assert.doesNotMatch(f.platformBody.innerHTML,
    /setting-(?:survey-duplicate|comment-duplicate|google-forms-entry|report-quick-mode|report-postwrite)/);
  assert.equal(f.body.innerHTML, functionsMarkup);
  assert.equal(f.nodes.get('setting-report-postwrite').checked, true);
  assert.equal(f.saved.report_postwrite_enabled, true);
  assert.equal(f.requests.length, 2);
  assert(f.requests.every(request => request.url === '/api/app-settings' && request.method === undefined));
});
