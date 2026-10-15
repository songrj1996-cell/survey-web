const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const page = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const style = fs.readFileSync(path.join(root, 'static/style.css'), 'utf8');
const source = fs.readFileSync(path.join(root, 'static/js/features/reward-roster.js'), 'utf8');

assert.match(page, /id="nav-reward"[\s\S]*?>奖励整理</, 'reward roster is a first-level platform entry');
assert.match(page, /这里列出 Excel 内全部 sheet/, 'sheet selection describes the workbook-wide scope');
assert.match(page, /1\. 选择奖励日期/, 'date selection has a prominent first-use heading');
assert.match(page, /2\. 选择需要进入统计的 sheet/, 'sheet selection has a prominent first-use heading');
assert.match(page, /3\. 字段映射/, 'field mapping has a prominent first-use heading');
assert.doesNotMatch(page, /type="date"/, 'native calendar input is not used');
assert.match(page, />按任务统计</, 'result statistics are task-based');
assert.match(page, /id="rr-btn-download"/, 'result view contains the TXT download action');
assert.match(style, /\.rr-surface\s*\{[^}]*background:\s*var\(--surface\)/s,
  'major reward panels use the platform surface instead of the page background');
assert.match(style, /\.rr-task-choice\s*\{[^}]*background:\s*var\(--surface-2\)/s,
  'nested conflict groups use the platform secondary surface');
assert.doesNotMatch(style, /\.rr-(?:surface|conflict-card)[^{]*\{[^}]*background:\s*var\(--bg\)/s,
  'business cards never blend into the lowest page background');
assert.doesNotMatch(source, /\/api\/(?:feishu|llm)|run-ai|completion/i,
  'reward roster does not call Feishu or model endpoints');
assert.match(source, /reward_dates:\s*\[\.\.\.rrState\.selectedDates\]\.sort\(\)/,
  'analysis submits the selected Excel dates');
assert.match(source, /来自：\$\{esc\(item\.sheets\.join\('、'\)\)\}/,
  'merged task statistics identify source sheets');
assert.match(style, /\.rr-account-list--scroll\s*\{[^}]*height:\s*840px[^}]*overflow-y:\s*auto/s,
  'long account details keep a fixed scrollable viewport');
assert.match(style, /\.rr-date-tree,\s*\.rr-sheet-list\s*\{[^}]*background:\s*var\(--surface-2\)/s,
  'sheet choices share one compact secondary surface');
assert.match(style, /\.rr-mapping-table-wrap\s*\{[^}]*overflow-x:\s*auto/s,
  'wide mapping matrices scroll inside their own surface');
assert.match(source, /grid-template-columns:\$\{columns\}/,
  'mapping matrix creates one column per selected sheet');

class Element {
  constructor() {
    this.value = '';
    this.disabled = false;
    this.hidden = false;
    this.dataset = {};
    this.classList = {add() {}, remove() {}, toggle() {}};
  }
  addEventListener() {}
  querySelectorAll() { return []; }
  querySelector() { return new Element(); }
  scrollIntoView() {}
  select() {}
  set innerHTML(value) { this.html = value; }
  get innerHTML() { return this.html || ''; }
}

const nodes = new Map();
const $ = id => {
  if (!nodes.has(id)) nodes.set(id, new Element());
  return nodes.get(id);
};
const context = {
  console,
  $,
  esc: value => String(value ?? ''),
  showToast() {},
  fetch: async () => { throw new Error('network not expected'); },
  FormData: class { append() {} },
  navigator: {clipboard: {writeText: async () => {}}},
  document: {querySelectorAll: () => [], execCommand: () => true},
  window: {scrollTo() {}, location: {href: ''}},
  setTimeout,
  clearTimeout,
  Set,
  Map,
};
vm.createContext(context);
vm.runInContext(source, context);

vm.runInContext(`rrState.mappings = {A: {
  date_col:0, task_col:1, player_id_col:2, account_id_col:3,
  status_col:4, normal_reward_col:5, excellent_reward_col:6, other_reward_col:7
}}`, context);
assert.equal(vm.runInContext(`rrMappingComplete('A')`, context), true,
  'eight distinct mapped columns enable analysis');
vm.runInContext(`rrState.mappings.A.excellent_reward_col = null; rrState.mappings.A.other_reward_col = null`, context);
assert.equal(vm.runInContext(`rrMappingComplete('A')`, context), true,
  'optional reward categories may explicitly be treated as zero');
vm.runInContext(`rrState.mappings.A.excellent_reward_col = 7; rrState.mappings.A.other_reward_col = 7`, context);
assert.equal(vm.runInContext(`rrMappingComplete('A')`, context), false,
  'duplicate mapped columns keep analysis disabled');
assert.equal(vm.runInContext(`rrReasonLabel('cross_sheet_account')`, context), '账号在多个 sheet 中出现');

vm.runInContext(`
  rrState.sheets = [
    {name:'UE群', usable:true, row_count:20, header_row:1, headers:['奖励日期','任务','playerid','MLBBaccountid','当前状态','普通奖励','优秀额外奖励','其他奖励']},
    {name:'玩家群', usable:true, row_count:30, header_row:1, headers:['发奖日期','项目','Whatsappid','ori','成员状态','基础奖励','优秀奖励','临时活动补发']}
  ];
  rrState.selectedSheets = new Set(['UE群', '玩家群']);
  rrState.mappings = {};
  rrRenderMappings();
`, context);
const mappingHtml = nodes.get('rr-mapping-list').innerHTML;
assert.match(mappingHtml, /rr-mapping-table/, 'selected sheets render in one mapping matrix');
assert.match(mappingHtml, /工具字段[\s\S]*UE群[\s\S]*玩家群[\s\S]*用途/, 'matrix header is field, selected sheets, then purpose');
assert.match(mappingHtml, /奖励发放日期[\s\S]*筛选本次批次/, 'field rows explain how each mapping is used');
assert.doesNotMatch(mappingHtml, /rr-mapping-card/, 'mapping no longer renders one card per sheet');

vm.runInContext(`rrState.mappings.A = {date_col:0,task_col:1,player_id_col:2,account_id_col:3,normal_reward_col:4,status_col:null,excellent_reward_col:null,other_reward_col:null}`, context);
assert.equal(vm.runInContext(`rrMappingComplete('A')`, context), true, 'live roster import permits no Excel status column');
assert.equal(vm.runInContext(`rrAutoColumn(['日期','任务','playerid','ori','普通奖励'], RR_FIELDS.find(f=>f.key==='status_col'))`, context), null);
assert.match(mappingHtml, /不提供（以最新名单为准）/);
console.log('reward roster UI checks passed, including optional legacy status');
