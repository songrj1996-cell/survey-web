const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/features/settings.js'), 'utf8');
const start = source.indexOf('async function loadSystemSettings()');
const section = source.slice(start, source.indexOf("\nwindow.addEventListener('beforeunload'", start));

function fixture({enabled = false, canUse = true, patchError = null, deferPatch = false} = {}) {
  const nodes = new Map(), handlers = {}, requests = [], toasts = [];
  const saved = {report_postwrite_enabled:enabled, report_quick_mode_enabled:false};
  let release, styleRefreshes = 0;
  const body = {
    _html:'',
    set innerHTML(value) {
      this._html = value;
      for (const match of value.matchAll(/<input\b([^>]+)>/g)) {
        const id = /id="([^"]+)"/.exec(match[1])?.[1];
        if (id) nodes.set(id, {id, checked:/\bchecked\b/.test(match[1]), disabled:false,
          closest(selector) {return selector.split(',').some(s => s.trim() === '#' + id) ? this : null;}});
      }
    },
    get innerHTML() {return this._html;},
    querySelectorAll() {return [...nodes.values()].filter(node => node !== body);},
    addEventListener(name, handler) {handlers[name] = handler;},
  };
  nodes.set('stab-content-system', body);
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
  return {nodes, body, requests, toasts, saved,
    load:()=>vm.runInContext('loadSystemSettings()', ctx),
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
