const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const review = require('../static/js/features/report-postwrite-review.js');
const sentence = '玩家需要较长时间熟悉卡牌效果。';
const report = '# 报告\n\n## 第四章 玩法体验\n\n### 4.1 上手过程\n\n' + sentence
  + '\n\n## 第五章 建议\n\n### 5.1 再次提及\n\n' + sentence;
function fixture(check, count) {
  const ctx = {reportMd:report, reportAudit:{status:'complete', registration_status:'complete',
    checked_claims:1, registered_claims:1, checks:check ? [check] : [], counts:count ? [count] : [],
    checkpoint:{registry:{claims:[{id:'C001',original:sentence,block_ids:['B0004'],count_task_ids:['T001']}]}}}};
  return ctx;
}
function check(verdict = 'issue', extra = {}) {
  return {id:'C001', original:sentence, verdict, complete:true, reason:'需要确认',
    sources:[{relation:'direct', source:{player:'P00002',source_row:2,question:{translation:'体验评价'},original:'需要一点时间。'}}], ...extra};
}
test('passed source support, reasonable explanations and suggestions produce no problem entries', () => {
  for (const verdict of ['supported','inference','advice']) {
    const ctx = fixture(check(verdict));
    const before = JSON.stringify(ctx);
    assert.equal(review.build(ctx).issues.length, 0);
    assert.equal(JSON.stringify(ctx), before);
  }
});
test('a real citation issue remains visible even when meaning was supported', () => {
  const model = review.build(fixture(check('supported',{citation_issues:[{reason:'引用了其他题的回答'}]})));
  assert.equal(model.issues.length, 1);
  assert.equal(model.issues[0].risk, 'high');
});
test('known unrelated meaning is high risk; ambiguous and unclassified issues are not guessed', () => {
  assert.equal(review.build(fixture(check('issue',{sources:[{relation:'unrelated'}]}))).issues[0].risk,'high');
  assert.equal(review.build(fixture(check('uncertain'))).issues[0].risk,'uncertain');
  assert.equal(review.build(fixture(check('issue',{reason:'完全错误!!!'}))).issues[0].risk,'uncertain');
});
test('only a bounded degree change is low risk; negation, subject and count changes cannot be downgraded', () => {
  const repair = sentence.replace('较长时间','一些时间');
  assert.equal(review.build(fixture(check('issue',{repair}))).issues[0].risk,'low');
  for (const other of [repair.replace('需要','不需要'),repair.replace('玩家','所有玩家'),repair.replace('卡牌','Survival'),repair+'多数人如此。']) {
    assert.notEqual(review.build(fixture(check('issue',{repair:other}))).issues[0].risk,'low');
  }
  assert.equal(review.build(fixture(check('issue',{repair,citation_issues:[{reason:'错引'}]}))).issues[0].risk,'high');
});
test('position follows block binding rather than the first repeated sentence', () => {
  const ctx = fixture(check());
  let issue = review.build(ctx).issues[0];
  assert.equal(issue.path, '第四章 玩法体验 › 4.1 上手过程');
  ctx.reportAudit.checkpoint.registry.claims[0].block_ids = ['B0007'];
  issue = review.build(ctx).issues[0];
  assert.equal(issue.path, '第五章 建议 › 5.1 再次提及');
  assert.equal(issue.locations[0].start, report.lastIndexOf(sentence));
});
test('ambiguous legacy locations and absent paragraphs never invent a chapter', () => {
  const ctx = fixture(check());
  delete ctx.reportAudit.checkpoint;
  assert.equal(review.build(ctx).issues[0].canLocate, false);
  assert.match(review.build(ctx).issues[0].path,/多处/);
  ctx.reportAudit.checks[0].original = '不存在的原句';
  assert.match(review.build(ctx).issues[0].path,/暂未定位/);
});
test('headings inside fenced code do not change the report location', () => {
  const md = '# 报告\n\n## 实际章节\n\n~~~\n## 假标题\n~~~\n\n原句';
  const index = review.blockIndex(md);
  assert.deepEqual(review.locations(index,'原句')[0].path, ['实际章节']);
});
test('setext headings, table blocks, Unicode and blank-block numbering keep real positions', () => {
  const md = '报告\n===\n\n第四章\n---\n\n\n\n| 玩家 | 意见 |\n| --- | --- |\n| 玩家甲 | 有趣 |';
  const index = review.blockIndex(md);
  assert.deepEqual(index.blocks.map(block=>block.id),['B0001','B0002','B0004']);
  assert.deepEqual(review.locations(index,'玩家甲',['B0004'])[0].path,['第四章']);
});
test('a span crossing unselected blocks is not assigned to a nearby chapter', () => {
  const index = review.blockIndex('## 一\n\n原句\n\n后半句');
  assert.equal(review.locations(index,'原句\n\n后半句',['B0002']).length,0);
});
test('exact correct counts and unchecked work are not content issues', () => {
  for (const count of [{id:'T001',reviewed:true,issues:[],review_status:'reviewed'},
    {id:'T001',reviewed:false,issues:['incomplete'],complete:false}]) {
    assert.equal(review.build(fixture(null,count)).issues.length,0);
  }
});
test('count inflation links to its report sentence and is merged with a meaning issue', () => {
  const ctx = fixture(check('uncertain'),{id:'T001',reviewed:true,issues:['reported_count:报告为20，Python核算为2'],members:[]});
  const model = review.build(ctx);
  assert.equal(model.issues.length,1);
  assert.equal(model.issues[0].risk,'high');
  assert.equal(model.issues[0].path,'第四章 玩法体验 › 4.1 上手过程');
  assert.deepEqual(model.issues[0].kinds,['原意或引用','人数范围']);
});
test('reviewed count ambiguity remains a visible range and is not mistaken for unfinished work', () => {
  const model = review.build(fixture(null,{id:'T001',reviewed:true,complete:false,
    review_status:'ambiguous',count_min:2,count_max:3,denominator:24,issues:[],members:[]}));
  assert.equal(model.issues[0].risk,'uncertain');
  assert.match(model.issues[0].reasons[0], /2—3\/24/);
});
test('an unlinked count issue is retained without fabricating an original sentence', () => {
  const ctx = fixture(null,{id:'T999',reviewed:true,issues:['分母不同'],members:[]});
  assert.equal(review.build(ctx).issues[0].original,'');
  assert.equal(review.build(ctx).issues[0].canLocate,false);
});
test('internal codes in explanations become plain labels, with source text preserved exactly', () => {
  const ctx = fixture(check('issue',{reason:'C001引用B0004，T001 scope_unresolved，见q1/r2和source_index'}));
  const issue = review.build(ctx).issues[0];
  assert.doesNotMatch(issue.reasons[0],/C001|B0004|T001|scope_unresolved|q1\/r2|source_index/);
  assert.equal(issue.sources[0].player,'原始数据第 2 行玩家');
  assert.equal(issue.sources[0].original,'需要一点时间。');
});
test('registration notes do not become content issues or disappear from the original record', () => {
  const ctx = fixture(check('inference'));
  ctx.reportAudit.verification_notes=['C001登记后待检查'];
  ctx.reportAudit.registration_notes=['标题无需登记'];
  const original = JSON.stringify(ctx);
  assert.equal(review.build(ctx).issues.length,0);
  assert.equal(JSON.stringify(ctx),original);
});
test('old results with no registry still locate a unique actual original', () => {
  const ctx = {reportMd:'# 报告\n\n## 真实章节\n\n唯一原句',
    reportAudit:{status:'complete',checks:[{id:'old',original:'唯一原句',verdict:'issue',reason:'待确认'}]}};
  assert.equal(review.build(ctx).issues[0].path,'真实章节');
  assert.equal(review.build(ctx).issues[0].canLocate,true);
});
test('presentation code cannot call models or network services and cannot rewrite a verdict', () => {
  const text = fs.readFileSync(path.join(__dirname,'../static/js/features/report-postwrite-review.js'),'utf8');
  assert.doesNotMatch(text, /\b(?:fetch|XMLHttpRequest|WebSocket)\s*\(/);
  assert.doesNotMatch(text, /\.verdict\s*=(?!=)/);
});
