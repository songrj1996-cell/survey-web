// Execute the production UI scripts with synthetic state and deferred API responses.
// Run: node tests/annotate_ai_decision_ui.cjs. No network or model calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const source = name => fs.readFileSync(path.join(root, 'static/js/features', name), 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));

function environment() {
  const nodes = new Map(), messages = [];
  class Element {
    constructor() {
      this.style = {}; this.dataset = {}; this.events = {}; this.disabled = false;
      this.textContent = ''; this.innerHTML = ''; this.children = new Map();
      const classes = new Set();
      this.classList = {
        add: name => classes.add(name), remove: name => classes.delete(name),
        contains: name => classes.has(name),
        toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
      };
    }
    setAttribute(name, value) { this[name] = value; }
    addEventListener(type, handler) { this.events[type] = handler; }
    querySelector(selector) {
      if (!this.children.has(selector)) this.children.set(selector, new Element());
      return this.children.get(selector);
    }
    querySelectorAll() { return []; }
    getBoundingClientRect() { return {right: 0}; }
    insertAdjacentElement() {}
  }
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const context = {
    console, $, esc: value => String(value ?? ''), annPanels: [],
    document: {hidden: false, querySelectorAll: () => [], querySelector: $, getElementById: $},
    setInterval() {}, addEventListener() {},
    PlatformPermissions: {canUse: () => true},
    showToast: (message, type) => messages.push({message, type}),
    workflowRequest: () => { throw new Error('Unexpected request'); },
    fetch: async url => {
      assert.equal(url, '/api/upload-guide');
      return {ok: true, json: async () => ({})};
    },
  };
  context.window = context;
  vm.createContext(context);
  return {context, $, messages};
}

function assertSelection(env, prefix, decision) {
  for (const choice of ['normal', 'ai']) {
    const button = env.$(`${prefix}-ai-mark-${choice}`);
    assert.equal(button.classList.contains('ann-ai-decision-btn--active'), choice === decision);
    assert.equal(button['aria-pressed'], String(choice === decision));
  }
}

function taskFixture() {
  return {
    id: 'synthetic-task', version: 1, status: 'awaiting_ai_confirmation', can_edit: true,
    rules: {name: '合成人工确认测试', ai_detect: true}, owner_name: '合成操作者',
    workflow: {max_step: 3}, summary: {complete_count: 0, feedback_count: 2},
    headers: ['ID', '问题'], mapping: {id_col: 0, open_text_cols: [1]},
    feedback: [90, 72].map((prob, i) => ({
      id: `response-${i}`, player_id: `player-${i}`, ai_complete: true,
      ai: {ai_prob: prob, polish_prob: 88, reason: '合成判断依据'},
      answers: [{column: 1, answer: '合成原始回答'}],
    })),
    annotation: {
      phase: 'ai_review', options: {ai_detect: true, quality: true},
      review_ids: ['response-0', 'response-1'], reviewed_ids: [], review_threshold: 60, high_threshold: 80,
    },
  };
}

async function groupTaskChecks() {
  const env = environment();
  const marker = 'return {open,list};';
  assert(source('group-tasks.js').includes(marker));
  let serverTask = taskFixture(), request, requestCount = 0;
  env.context.workflowRequest = (url, options) => {
    requestCount++;
    return new Promise((resolve, reject) => { request = {url, options, resolve, reject}; });
  };
  vm.runInContext(source('group-tasks.js').replace(marker,
    `return {open,list,tests:{annState,annSetAiDecision,annGoToAiPlayer,
      show(value){task=value;step=3;render();}}};`), env.context);
  const ui = env.context.GroupTasks.tests;
  ui.show(clone(serverTask));
  for (const index of [0, 1]) {
    ui.annGoToAiPlayer(index);
    assertSelection(env, 'ga', null);
    assert.match(env.$('ga-ai-review-state').textContent, /待/);
  }
  assert.equal(env.$('ga-ai-reviewed-total').textContent, '0 / 2');
  assert.equal(env.$('ga-btn-confirm-ai').disabled, true);
  ui.annGoToAiPlayer(0);
  assert.equal(ui.annState.confirmedAiIds.has('response-0'), true, 'system recommendation stays separate');

  const pending = ui.annSetAiDecision('normal');
  assertSelection(env, 'ga', null);
  assert.equal(env.$('ga-ai-reviewed-total').textContent, '0 / 2');
  await ui.annSetAiDecision('ai');
  assert.equal(requestCount, 1, 'a second click cannot bypass an in-flight save');
  request.reject(new Error('人工判定保存失败（模拟 HTTP 409），请刷新后重试'));
  await pending;
  assertSelection(env, 'ga', null);
  assert.equal(env.$('ga-btn-confirm-ai').disabled, true);
  assert.match(env.messages.at(-1).message, /保存失败.*409/);
  assert.equal(env.messages.at(-1).type, 'error');

  async function save(decision) {
    const promise = ui.annSetAiDecision(decision);
    const rid = ui.annState.reviewAiResults[ui.annState.aiReviewIndex].id;
    assert.equal(request.url, `/api/group-tasks/synthetic-task/feedback/${rid}/ai-review`);
    assert.equal(request.options.method, 'PUT');
    const row = serverTask.feedback.find(item => item.id === rid);
    row.ai_reviewed = true; row.ai_decision = request.options.body.is_ai;
    serverTask.annotation.reviewed_ids = serverTask.feedback.filter(item => item.ai_reviewed).map(item => item.id);
    serverTask.version++;
    request.resolve(clone(serverTask));
    await promise;
    assertSelection(env, 'ga', decision);
  }
  await save('normal');
  assert.equal(env.$('ga-ai-review-state').textContent, '已确认 · 正常反馈');
  assert.equal(env.$('ga-ai-reviewed-total').textContent, '1 / 2');
  assert.equal(env.$('ga-btn-confirm-ai').disabled, true);
  ui.annGoToAiPlayer(1);
  assertSelection(env, 'ga', null);
  await save('ai');
  assert.equal(env.$('ga-ai-review-state').textContent, '已确认 · AI 作答');
  assert.equal(env.$('ga-ai-reviewed-total').textContent, '2 / 2');
  assert.equal(env.$('ga-btn-confirm-ai').disabled, false);
  await save('normal');
  await save('ai');
  assert.equal(env.$('ga-ai-reviewed-total').textContent, '2 / 2', 'changing a decision does not double-count progress');

  const rejectedChange = ui.annSetAiDecision('normal');
  assertSelection(env, 'ga', 'ai');
  request.reject(new Error('模拟保存失败'));
  await rejectedChange;
  assertSelection(env, 'ga', 'ai');
  ui.show(clone(serverTask));
  assertSelection(env, 'ga', 'ai');
  ui.annGoToAiPlayer(0);
  assertSelection(env, 'ga', 'normal');
  const before = requestCount;
  serverTask.can_edit = false;
  ui.show(clone(serverTask));
  await ui.annSetAiDecision('ai');
  assert.equal(requestCount, before, 'read-only viewing never saves a decision');
  assertSelection(env, 'ga', 'normal');
}

function legacyChecks() {
  const env = environment();
  vm.runInContext(source('annotate.js') +
    '\nglobalThis.testUI={annState,annRenderAiConfirm,annGoToAiPlayer};', env.context);
  const ui = env.context.testUI;
  ui.annRenderAiConfirm([{id: 'high', ai_prob: 90}, {id: 'medium', ai_prob: 72}]);
  for (const index of [0, 1]) {
    ui.annGoToAiPlayer(index);
    assertSelection(env, 'ann', null);
  }
  assert.equal(env.$('ann-btn-confirm-ai').disabled, true);
  ui.annGoToAiPlayer(0);
  env.$('ann-ai-mark-normal').events.click();
  assertSelection(env, 'ann', 'normal');
  assert.equal(env.$('ann-ai-reviewed-total').textContent, '1 / 2');
  assert.equal(env.$('ann-btn-confirm-ai').disabled, true);
  ui.annGoToAiPlayer(1);
  assertSelection(env, 'ann', null);
  env.$('ann-ai-mark-ai').events.click();
  assertSelection(env, 'ann', 'ai');
  assert.equal(env.$('ann-btn-confirm-ai').disabled, false);
  env.$('ann-ai-mark-normal').events.click();
  assertSelection(env, 'ann', 'normal');
  ui.annGoToAiPlayer(0);
  assertSelection(env, 'ann', 'normal');
  ui.annGoToAiPlayer(1);
  assertSelection(env, 'ann', 'normal');
}

(async () => {
  await groupTaskChecks();
  legacyChecks();
  console.log('PASS: group and legacy AI review remain unselected until human confirmation; pending/failed saves, changes, restoration, read-only access and next-step progress verified.');
})().catch(error => { console.error(error); process.exitCode = 1; });
