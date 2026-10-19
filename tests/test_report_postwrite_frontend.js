const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/features/report.js'), 'utf8');
function section(from, to) { return source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from))); }
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.textContent = ''; this.hidden = false; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; }
  text() { return this.textContent + this.children.map(c => c.text()).join('\n'); }
}
function fixture(audit, canEdit = true) {
  const panel = new Element('section'), calls = [];
  const ctx = {id:'history-id', selectedVersion:2, reportAudit:audit, canEdit, versions:[{}, {}], maxVersions:5};
  const sandbox = vm.createContext({document:{createElement: tag => new Element(tag)}, $:()=>panel,
    activeReportCtx:()=>ctx, activeVersionNumber:()=>2, reportInteractionBusy:()=>false,
    state:{viewMode:'history'}, window:{}, runStats: args => calls.push(args)});
  vm.runInContext(section('function renderPostwriteAudit(', 'function renderReportWorkspace('), sandbox);
  vm.runInContext('renderPostwriteAudit()', sandbox);
  return {panel, calls, ctx, render:()=>vm.runInContext('renderPostwriteAudit()', sandbox)};
}
test('issue advisory preserves original and escapes model text through textContent', () => {
  const {panel} = fixture({status:'complete', checked_claims:1, registered_claims:1, checks:[{
    id:'C1', verdict:'issue', original:'合理解释', reason:'待人工看', sources:[{ref:'q1/r2', meaning:'释义',
      source:{player:'P2', question:{translation:'体验'}, original:'<img src=x onerror=alert(1)>'}}]}]});
  assert.match(panel.text(), /三项核验已完成/);
  assert.match(panel.text(), /<img src=x/);
  assert.equal(panel.children.some(c=>c.tagName === 'img'), false);
  assert.match(panel.text(), /合理解释/);
});
test('resume binds history and selected version and is not a full regeneration', () => {
  assert.match(source, /async function runStats\(options = \{\}\)/);
  assert.doesNotMatch(section('function renderPostwriteAudit(', 'function renderReportWorkspace('), /startReport\(/);
  const {panel, calls} = fixture({status:'partial', checks:[]});
  const button = panel.children.find(c=>c.dataset.postwriteAction === 'resume');
  button.onclick();
  assert.equal(calls[0].baseVersion, 2);
  assert.equal(calls[0].historyId, 'history-id');
  assert.equal(calls[0].retryFailed, true);
  assert.equal(calls[0].regenerate, false);
});
test('read-only history has no mutation buttons', () => {
  const {panel} = fixture({status:'partial', checks:[]}, false);
  assert.equal(panel.children.filter(c=>c.tagName === 'button').length, 0);
});
test('switch to legacy clears audit instead of showing newer findings', () => {
  const {panel, ctx, render} = fixture({status:'complete', checks:[]});
  ctx.reportAudit = null;
  render();
  assert.equal(panel.hidden, true);
  assert.equal(panel.children.length, 0);
});
test('metadata binding clears audit when version changes', () => {
  const target = {id:'x', selectedVersion:1, snapshotMetaKey:'x:1', reportAudit:{status:'complete'}};
  const sandbox = vm.createContext({state:{sessionId:'x'}, normalizeReportVersions:x=>x||[],
    toFiniteVersion:x=>Number(x)||null, normalizeReportLlmUsage:x=>x, target});
  vm.runInContext(section('function syncReportVersionMeta(', 'function activeVersionNumber('), sandbox);
  vm.runInContext('syncReportVersionMeta(target, {selected_version:2})', sandbox);
  assert.equal(target.reportAudit, null);
});
test('postwrite progress describes remaining audit instead of old analysis steps', () => {
  const sandbox = vm.createContext({});
  vm.runInContext(section('function _reportRemainingText(', 'function _renderReportPreparationSteps('), sandbox);
  const text = vm.runInContext('_reportRemainingText({current:{phase:"postwrite",stage:"meaning"}})', sandbox);
  assert.match(text, /核对原意/);
  assert.match(text, /保存/);
  assert.doesNotMatch(text, /跨题观点归纳/);
});

test('registered pending notes do not look like completed verification or missing registration', () => {
  const {panel} = fixture({status:'partial', registration_status:'complete', registered_claims:155, checked_claims:0,
    verification_notes:['样本信息待核验', '<script>比较强度未核验</script>'], registration_notes:['报告未写的统计无需登记'],
    usage:{requests:1,tokens:106354,usage_complete:true,model_seconds:343.533}});
  assert.match(panel.text(), /观点登记已完成/);
  assert.match(panel.text(), /核验尚未完成/);
  assert.match(panel.text(), /0\/155/);
  assert.match(panel.text(), /登记时待核验提示：样本信息待核验/);
  assert.match(panel.text(), /登记说明：报告未写的统计无需登记/);
  assert.doesNotMatch(panel.text(), /三项核验已完成|登记缺项/);
  assert.equal(panel.children.some(c=>c.tagName === 'script'), false);
  assert.match(panel.text(), /106354/);
});

test('registration errors remain distinct from deferred verification', () => {
  const {panel} = fixture({status:'partial', registration_status:'incomplete', registration_errors:['B2未登记'],
    verification_notes:['C1尚未核验']});
  assert.match(panel.text(), /观点登记尚未完成/);
  assert.match(panel.text(), /登记缺项：B2未登记/);
  assert.match(panel.text(), /登记时待核验提示：C1尚未核验/);
});

test('reviewed ambiguity shows range and original with no resume button', () => {
  const {panel} = fixture({status:'complete', has_ambiguity:true, counts_reviewed:1, counts_exact:0,
    counts_ambiguous:1, counts_unreviewed:0, counts:[{id:'T1', reviewed:true, complete:false,
      review_status:'ambiguous', count:null, count_min:33, count_max:41, denominator:78,
      members:[{player:'P2',count:null,sources:[{ref:'D2',meaning:'是否满足条件有歧义',source:{original:'原始歧义回答'}}]}]}]});
  assert.match(panel.text(), /核验已完成 · 含歧义待人工确认/);
  assert.match(panel.text(), /已核验但有歧义/);
  assert.match(panel.text(), /33—41\/78/);
  assert.match(panel.text(), /原始歧义回答/);
  assert.doesNotMatch(panel.text(), /null\/78|尚未核验 1 项/);
  assert.equal(panel.children.some(c=>c.dataset.postwriteAction === 'resume'), false);
});

test('real missing task remains unreviewed and resumable', () => {
  const {panel} = fixture({status:'partial', counts:[{id:'T1', reviewed:false, complete:false}]});
  assert.match(panel.text(), /尚未核验/);
  assert.equal(panel.children.some(c=>c.dataset.postwriteAction === 'resume'), true);
});
