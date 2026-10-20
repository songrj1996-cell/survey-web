const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/features/report.js'), 'utf8');
const code = source.slice(source.indexOf('function comparisonAuditRow('), source.indexOf('function setPostwriteReviewOpen('));

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.style = {}; this.hidden = false; this.textContent = ''; }
  append(...items) { this.children.push(...items); }
  replaceChildren() { this.children = []; }
  get childElementCount() { return this.children.length; }
  text() { return this.textContent + this.children.map(item => item.text()).join('\n'); }
}

function fixture(audit, postwrite = true) {
  const names = ['comparison-validation', 'btn-comparison-validation', 'comparison-validation-trigger-label',
    'comparison-validation-alert', 'comparison-validation-alert-title', 'comparison-validation-alert-text',
    'comparison-validation-status', 'comparison-validation-counts', 'comparison-validation-body'];
  const nodes = Object.fromEntries(names.map(name => [name, new Element('div')]));
  const ctx = {reportMode: 'insight', comparisonValidation: audit, reportAudit: postwrite ? {status: 'partial'} : null};
  const sandbox = vm.createContext({document: {createElement: tag => new Element(tag)}, $: name => nodes[name],
    activeReportCtx: () => ctx, window: {confirm: () => true}});
  vm.runInContext(code, sandbox);
  const render = () => vm.runInContext('renderComparisonValidation(activeReportCtx().comparisonValidation)', sandbox);
  render();
  return {nodes, ctx, render, label: () => nodes['comparison-validation-trigger-label'].text(),
    body: () => nodes['comparison-validation-body'].text()};
}

test('postwrite without a record says not executed, never predates the feature', () => {
  const f = fixture({});
  assert.equal(f.label(), '未执行');
  assert.match(f.body(), /尚未执行/);
  assert.doesNotMatch(f.body(), /上线之前|校验通过|未发现需要修改/);
});

test('a non-postwrite record with unknown history does not claim an earlier creation date', () => {
  const f = fixture({}, false);
  assert.equal(f.label(), '未校验');
  assert.match(f.body(), /没有保存.*不能据此判断是否执行/);
  assert.doesNotMatch(f.body(), /上线之前/);
});

test('no comparable scales is neutral and does not certify counts or percentages', () => {
  const f = fixture({status: 'not_applicable', catalog_group_count: 0});
  assert.equal(f.label(), '无适用项');
  assert.equal(f.nodes['comparison-validation-status'].text(), '无适用的量表比较');
  assert.match(f.body(), /不代表其他人数和比例已核验/);
  assert.equal(f.nodes['comparison-validation-counts'].text(), '');
  assert.equal(f.nodes['comparison-validation-alert'].hidden, true);
  assert.doesNotMatch(f.body(), /校验通过|未发现需要修改/);
});

test('missing frozen scale evidence is not executed and its reason is visible', () => {
  const f = fixture({status: 'not_run', error: '此版本未保存量表比较依据，尚未执行比较校验。'});
  assert.equal(f.label(), '未执行');
  assert.match(f.body(), /未保存量表比较依据/);
  assert.equal(f.nodes['comparison-validation-counts'].text(), '');
});

test('failure is distinct from no applicable items and is not displayed as zero errors', () => {
  const f = fixture({status: 'failed', error: '本版统计比较校验执行失败，报告正文保持原样。'});
  assert.equal(f.label(), '执行失败');
  assert.equal(f.nodes['comparison-validation-alert'].hidden, false);
  assert.match(f.body(), /正文保持原样/);
  assert.equal(f.nodes['comparison-validation-counts'].text(), '');
  assert.doesNotMatch(f.body(), /未发现需要修改/);
});

test('genuine contradictions display original wording and Python facts without applying changes', () => {
  const original = '概念1的满意度均值最高。';
  const f = fixture({status: 'needs_review', detected_count: 1, applied_count: 0, unresolved_count: 1,
    unresolved: [{original, reasons: ['概念1并非整体满意度最高项'], factual_basis: '概念3 3.92 > 概念1 3.84',
      risk: '仅核对，未自动改写正文。'}]});
  assert.equal(f.label(), '待确认 1');
  assert.match(f.body(), /概念1的满意度均值最高/);
  assert.match(f.body(), /概念3 3.92/);
  assert.match(f.nodes['comparison-validation-counts'].text(), /修改 0/);
});

test('switching selected version clears stale review content, including unsafe markup', () => {
  const f = fixture({status: 'needs_review', unresolved: [{original: '<img src=x onerror=alert(1)>', reasons: []}]});
  assert.match(f.body(), /<img/);
  assert.equal(f.nodes['comparison-validation-body'].children.some(item => item.tagName === 'img'), false);
  f.ctx.comparisonValidation = {status: 'not_applicable'};
  f.render();
  assert.equal(f.label(), '无适用项');
  assert.doesNotMatch(f.body(), /<img/);
});

test('existing successful and repaired records retain their labels', () => {
  assert.equal(fixture({status: 'passed', catalog_group_count: 1}).label(), '校验通过');
  assert.equal(fixture({status: 'repaired', applied_count: 1}).label(), '已修正 1');
});
