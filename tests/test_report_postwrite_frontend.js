const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const review = require('../static/js/features/report-postwrite-review.js');
const source = fs.readFileSync(path.join(__dirname, '../static/js/features/report.js'), 'utf8');
function section(from, to) { return source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from))); }
class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.dataset = {}; this.textContent = ''; this.hidden = false;
    this.attrs = {}; this.classes = new Set();
    this.classList = {toggle:(name, on)=>on ? this.classes.add(name) : this.classes.delete(name)};
  }
  setAttribute(name, value) { this.attrs[name] = value; }
  focus() { this.focused = true; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; }
  text() { return this.textContent + this.children.map(c => c.text()).join('\n'); }
}
function descendants(node) { return node.children.flatMap(child => [child, ...descendants(child)]); }
function capacityFixture() {
  const summary = new Element('div');
  const nodes = {'report-llm-summary':summary};
  const parent = {children:[summary]};
  summary.insertAdjacentElement = (position, node) => {
    assert.equal(position, 'afterend');
    nodes[node.id] = node;
    parent.children.splice(1, 0, node);
  };
  const sandbox = vm.createContext({$:id=>nodes[id], document:{createElement:tag=>{
    const node = new Element(tag); node.style = {}; return node;
  }}});
  vm.runInContext(section('function _renderWriterCapacity(', 'function _createReportTaskProgress('), sandbox);
  return {nodes, parent, render:notice=>sandbox._renderWriterCapacity(notice)};
}

test('capacity advice stays directly under token usage, with one or two plain text lines', () => {
  const f = capacityFixture();
  f.render({status:'checking', message:'容量检查：正在估算本次输入及输出预留…'});
  const box = f.nodes['report-writer-capacity'];
  assert.equal(f.parent.children[1], box);
  assert.equal(box.children.length, 1);
  assert.equal(box.attrs.role, 'status');
  assert.equal(box.attrs['aria-live'], 'polite');
  f.render({status:'sufficient', message:'容量预估：80%（含输出预留） · 容量充足',
    impact:'预计可一次完成写作；预估值仅供参考。'});
  assert.equal(f.parent.children[1], box);
  assert.equal(f.parent.children.length, 2);
  assert.equal(box.children.length, 2);
  assert.match(box.text(), /80%/);
  assert.match(box.className, /report-progress-remaining/);
  assert.ok(descendants(box).every(node=>node.tagName === 'p'));
});

test('over-limit and unknown advice have no actions and never invent zero percent', () => {
  const f = capacityFixture();
  for (const notice of [{status:'over_limit', message:'容量预估：108%（含输出预留） · 已达到或超出上限',
    impact:'可能发生截断或生成失败，报告仍继续生成。'},
    {status:'unknown', message:'容量检查：暂时无法估算容量占用', impact:'容量风险尚不明确，报告继续生成。'}]) {
    f.render(notice);
    const box = f.nodes['report-writer-capacity'];
    assert.match(box.text(), /继续生成/);
    assert.doesNotMatch(box.text(), /占用.*0%|确认|暂停/);
    assert.ok(descendants(box).every(node=>node.tagName !== 'button'));
    assert.equal(box.children.length, 2);
  }
});

test('capacity notice escapes source text and clears between generations', () => {
  const f = capacityFixture();
  f.render({status:'unknown', message:'<img src=x onerror=alert(1)>', impact:'<script>bad()</script>'});
  const box = f.nodes['report-writer-capacity'];
  assert.match(box.text(), /<img/);
  assert.equal(descendants(box).length, 2);
  f.render(null);
  assert.equal(box.hidden, true);
  assert.equal(box.children.length, 0);
  assert.match(section('async function runStats(', 'function prepareReportMarkdownForPreview('), /_renderWriterCapacity\(null\)/);
});

test('later writer and audit progress cannot replace the capacity notice', () => {
  const handler = section("if (ev.type === 'analysis_progress')", "if (ev.type === 'report_llm_status')");
  assert.match(handler, /ev\.phase === 'postwrite' && ev\.stage === 'writer_capacity'/);
  assert.match(handler, /_renderWriterCapacity\(ev\.writer_capacity\)/);
  const f = capacityFixture();
  f.render({status:'over_limit', message:'容量预估：108%', impact:'报告继续生成。'});
  const before = f.nodes['report-writer-capacity'].text();
  const sandbox = vm.createContext({REPORT_PHASE_LABELS:{themes:'主题', writing:'写作'}});
  vm.runInContext(section('function _createReportTaskProgress(', 'function _reportProgressPercent('), sandbox);
  for (const stage of ['writer', 'registry', 'meaning', 'quantity']) {
    sandbox.progress = sandbox._createReportTaskProgress();
    sandbox.event = {phase:'postwrite', stage, status:'running', message:'正在处理'};
    vm.runInContext('_applyAnalysisProgress(progress, event)', sandbox);
    assert.equal(f.nodes['report-writer-capacity'].text(), before);
  }
});

function fixture(audit, canEdit = true) {
  const panel = new Element('section'), calls = [];
  const drawer = new Element('div'), trigger = new Element('button'), title = new Element('div'), close = new Element('button');
  drawer.hidden = true;
  const nodes = {'report-postwrite-audit':panel, 'postwrite-review-drawer':drawer,
    'btn-report-postwrite-review':trigger, 'postwrite-review-title':title, 'btn-postwrite-review-close':close};
  const ctx = {id:'history-id', selectedVersion:2, reportAudit:audit, canEdit, versions:[{}, {}], maxVersions:5};
  const doc = {createElement: tag => {const el = new Element(tag); el.ownerDocument = doc; return el;}, activeElement:null};
  panel.ownerDocument = doc;
  close.focus = ()=>{doc.activeElement=close;};
  trigger.focus = ()=>{doc.activeElement=trigger;};
  drawer.querySelectorAll = ()=>[close];
  close.getClientRects = ()=>[{}];
  const sandbox = vm.createContext({document:doc, $:id=>nodes[id],
    activeReportCtx:()=>ctx, activeVersionNumber:()=>ctx.selectedVersion, reportInteractionBusy:()=>false,
    state:{viewMode:'history'}, window:{PostwriteReview:review}, runStats: args => calls.push(args)});
  vm.runInContext(section('function setPostwriteReviewOpen(', 'function renderReportWorkspace('), sandbox);
  vm.runInContext('renderPostwriteAudit()', sandbox);
  return {panel, calls, ctx, drawer, trigger, title, doc, close,
    render:()=>vm.runInContext('renderPostwriteAudit()', sandbox),
    open:()=>vm.runInContext('setPostwriteReviewOpen(true)', sandbox),
    key:event=>sandbox.handlePostwriteReviewKeydown(event)};
}
test('review lives outside report layout, with a closed dialog and print exclusion', () => {
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  const layout = html.slice(html.indexOf('<div class="report-layout">'), html.indexOf('<aside class="qa-side"'));
  assert.doesNotMatch(layout, /report-postwrite-audit/);
  assert.match(html, /id="postwrite-review-drawer" hidden inert aria-hidden="true"/);
  assert.match(html, /role="dialog" aria-modal="true" aria-labelledby="postwrite-review-title"/);
  const css = fs.readFileSync(path.join(__dirname, '../static/style.css'), 'utf8');
  assert.match(css, /@media print\s*\{\s*#postwrite-review-drawer, #btn-report-postwrite-review/);
});
test('review stays closed until opened, escapes with focus return and closes on version change', () => {
  const f = fixture({status:'complete', checks:[]});
  assert.equal(f.drawer.hidden, true);
  assert.equal(f.trigger.hidden, false);
  f.open();
  assert.equal(f.drawer.hidden, false);
  assert.equal(f.drawer.inert, false);
  assert.equal(f.trigger.attrs['aria-expanded'], 'true');
  assert.equal(f.doc.activeElement, f.close);
  let prevented = false;
  f.key({key:'Escape', preventDefault:()=>{prevented=true;}, stopPropagation:()=>{}});
  assert.equal(prevented, true);
  assert.equal(f.drawer.hidden, true);
  assert.equal(f.doc.activeElement, f.trigger);
  f.open(); f.ctx.selectedVersion=1; f.render();
  assert.equal(f.drawer.hidden, true);
  assert.match(f.title.text(), /V1/);
  f.open(); f.ctx.reportAudit=null; f.render();
  assert.equal(f.drawer.hidden, true);
  assert.equal(f.trigger.hidden, true);
});
test('keyboard tab stays inside the independent review panel', () => {
  const f=fixture({status:'complete'}); f.open();
  let prevented=false;
  f.key({key:'Tab',preventDefault:()=>{prevented=true;},stopPropagation:()=>{}});
  assert.equal(prevented,true); assert.equal(f.doc.activeElement,f.close);
});
test('issue advisory preserves original and escapes model text through textContent', () => {
  const {panel} = fixture({status:'complete', checked_claims:1, registered_claims:1, checks:[{
    id:'C1', verdict:'issue', original:'合理解释', reason:'待人工看', sources:[{ref:'q1/r2', meaning:'释义',
      source:{player:'P2', question:{translation:'体验'}, original:'<img src=x onerror=alert(1)>'}}]}]});
  assert.match(panel.text(), /本版核验已完成/);
  assert.match(panel.text(), /<img src=x/);
  assert.equal(panel.children.some(c=>c.tagName === 'img'), false);
  assert.match(panel.text(), /合理解释/);
});
test('resume binds history and selected version and is not a full regeneration', () => {
  assert.match(source, /async function runStats\(options = \{\}\)/);
  assert.doesNotMatch(section('function renderPostwriteAudit(', 'function renderReportWorkspace('), /startReport\(/);
  const {panel, calls} = fixture({status:'partial', checks:[]});
  const button = descendants(panel).find(c=>c.dataset.postwriteAction === 'resume');
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
  assert.match(panel.text(), /原文已核对/);
  assert.match(panel.text(), /核验尚未完成/);
  assert.match(panel.text(), /0\/155/);
  assert.doesNotMatch(panel.text(), /样本信息待核验/);
  assert.doesNotMatch(panel.text(), /报告未写的统计无需登记/);
  assert.doesNotMatch(panel.text(), /三项核验已完成|登记缺项/);
  assert.equal(panel.children.some(c=>c.tagName === 'script'), false);
  assert.match(panel.text(), /106,354/);
});

test('registration errors remain distinct from deferred verification', () => {
  const {panel} = fixture({status:'partial', registration_status:'incomplete', registration_errors:['B2未登记'],
    verification_notes:['C1尚未核验']});
  assert.match(panel.text(), /核对清单尚未整理完成/);
  assert.match(panel.text(), /尚未整理到核对清单/);
  assert.doesNotMatch(panel.text(), /B2未登记|C1尚未核验|0\/0/);
});

test('reviewed ambiguity shows range and original with no resume button', () => {
  const {panel} = fixture({status:'complete', has_ambiguity:true, counts_reviewed:1, counts_exact:0,
    counts_ambiguous:1, counts_unreviewed:0, counts:[{id:'T1', reviewed:true, complete:false,
      review_status:'ambiguous', count:null, count_min:33, count_max:41, denominator:78,
      members:[{player:'P2',count:null,sources:[{ref:'D2',meaning:'是否满足条件有歧义',source:{original:'原始歧义回答'}}]}]}]});
  assert.match(panel.text(), /本版核验已完成/);
  assert.match(panel.text(), /已核验.*存在歧义/);
  assert.match(panel.text(), /33—41\/78/);
  assert.match(panel.text(), /原始歧义回答/);
  assert.doesNotMatch(panel.text(), /null\/78|尚未核验 1 项/);
  assert.equal(descendants(panel).some(c=>c.dataset.postwriteAction === 'resume'), false);
});

test('real missing task remains unreviewed and resumable', () => {
  const {panel} = fixture({status:'partial', counts:[{id:'T1', reviewed:false, complete:false}]});
  assert.match(panel.text(), /核验尚未完成|尚未完成/);
  assert.equal(descendants(panel).some(c=>c.dataset.postwriteAction === 'resume'), true);
});


test('only regeneration is blocked by the version limit', () => {
  const f = fixture({status:'partial', checks:[]}); f.ctx.versions = [{},{},{},{},{}]; f.render();
  const actions = descendants(f.panel);
  assert.equal(actions.find(node=>node.dataset.postwriteAction === 'resume').disabled, false);
  assert.equal(actions.find(node=>node.dataset.postwriteAction === 'regenerate').disabled, true);
});

test('restoring audit result keeps the same report and follow-up history', () => {
  const session = {running:false, reportMd:'body', qaMessages:[{content:'old'}], qaHtml:'old html'};
  const history = {id:'history', selectedVersion:4, reportMd:'body', qaMessages:[{content:'keep me'}], qaHtml:'saved html'};
  const state = {viewMode:'session', sessionReport:{running:true}, historyReport:history};
  const sandbox = vm.createContext({state, activeReportCtx:()=>state.historyReport,
    syncReportVersionMeta:(target,meta)=>{target.selectedVersion=meta.selected_version;},
    normalizeReportLlmUsage:value=>value, renderReportWorkspace:()=>{}});
  vm.runInContext(section('function restorePostwriteAuditReport(', 'async function runStats('), sandbox);
  sandbox.restorePostwriteAuditReport({viewMode:'history',sessionReport:session,historyReport:history},
    {version:4, report_md:'body',report_audit:{status:'complete'},report_status:'complete'});
  assert.equal(state.viewMode, 'history'); assert.equal(history.selectedVersion, 4);
  assert.equal(history.qaMessages[0].content, 'keep me'); assert.equal(history.qaHtml, 'saved html');
  assert.equal(state.sessionReport.running, false);
});

test('review uses requested broad right-side coverage and has local-only module', () => {
  const css = fs.readFileSync(path.join(__dirname, '../static/style.css'),'utf8');
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'),'utf8');
  assert.match(css, /width: calc\(72vw - 16px\)/);
  assert.match(css, /max-width: 900px/);
  assert.ok(html.indexOf('report-postwrite-review.js') < html.indexOf('features/report.js'));
});
