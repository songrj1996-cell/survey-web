const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const survey = fs.readFileSync(path.join(__dirname, '../static/js/features/survey.js'), 'utf8');
const quick = fs.readFileSync(path.join(__dirname, '../static/js/features/report-quick.js'), 'utf8');
const openings = ['调研背景', '样本说明', '核心结论'];
const escape = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const planSource = survey.slice(survey.indexOf('let planOpeningRequestSerial'), survey.indexOf('// ── Plan confirm', survey.indexOf('function buildPlanHTML')));
const optionsSource = quick.slice(0, quick.indexOf('function revealQuickTarget'));
const sample = () => ({columns:[{index:0, role:'open_text', name:'体验反馈'}],
  parts:[{name:'原有业务分组', column_indexes:[0]}], cross_tabs:[], branch_rules:[], open_questions:[]});

function fixture() {
  const nodes = new Map();
  for (const id of ['plan-thinking', 'plan-card', 'plan-card-content', 'plan-input', 'btn-plan-ok', 'btn-plan-revise']) {
    nodes.set(id, {style:{}, innerHTML:'', disabled:false});
  }
  const ctx = vm.createContext({state:{sessionId:'session-a', reportMode:'insight', reportStyleSelection:{sessionId:'session-a'}},
    $:id=>nodes.get(id), MATRIX_ROLES:[], esc:escape, clearPlanInput(){}, loadReportStyleOptions:async()=>({postwrite_opening_sections:openings})});
  vm.runInContext(planSource, ctx);
  return {ctx, nodes, build:(plan, sections)=>ctx.buildPlanHTML(plan, ['原问卷题目'], sections)};
}

test('fixed opening reuses the existing Part cards before business groups without mutating the plan', () => {
  const f=fixture(), plan=sample(), before=JSON.stringify(plan);
  const original=f.build(plan, []), added=f.build(plan, openings);
  const cards=added.match(/<details class="plan-outline__part" open>[\s\S]*?<\/details>/g);
  assert.equal(cards.length,4);
  for (const [index,title] of openings.entries()) {
    assert.match(cards[index], /<summary class="plan-outline__part-summary">/);
    assert.ok(cards[index].includes(`<span class="plan-outline__part-num">Part ${index+1}</span>`));
    assert.ok(cards[index].includes(`<span class="plan-outline__part-title">${title}</span>`));
    assert.match(cards[index], /<div class="plan-outline__part-body">/);
    assert.match(cards[index], /<span class="plan-outline__part-meta">固定开篇<\/span>/);
    assert.doesNotMatch(cards[index], /0 道题/);
  }
  assert.doesNotMatch(added, /报告前三部分（固定顺序）|<ol>/);
  assert.match(cards[3], /Part 4/);
  assert.match(cards[3], /4\.1/);
  assert.match(cards[3], /原有业务分组/);
  // Cards have whitespace between them; remove each independently.
  const withoutOpening=cards.slice(0,3).reduce((html,card)=>html.replace(card,''),added);
  assert.equal(withoutOpening.replace(/\s+</g,'<').replace('Part 4','Part 1').replace('4.1','1.1'),
    original.replace(/\s+</g,'<'), 'original markup is retained except display numbers');
  assert.equal(JSON.stringify(plan), before);
});

test('display numbering continues through business groups and nested questions without changing their data', () => {
  const f=fixture(), plan=sample();
  plan.columns=[{index:0,role:'single_choice',name:'是否体验'},
    {index:1,role:'open_text',name:'体验建议'}, {index:2,role:'scale',name:'满意度'}];
  plan.parts=[{name:'体验反馈',column_indexes:[0,1]}, {name:'整体评价',column_indexes:[2]}];
  plan.branch_rules=[{parent_index:0,parent_name:'是否体验',allowed_options:['是'],eligible_count:3,
    targets:[{indexes:[1],name:'体验建议',answered_count:2}]}];
  const before=JSON.stringify(plan), html=f.build(plan,openings);
  assert.deepEqual([...html.matchAll(/plan-outline__part-num">Part (\d+)</g)].map(match=>match[1]),
    ['1','2','3','4','5']);
  for (const number of ['4.1','4.1.1','5.1']) {
    assert.ok(html.includes(`<span class="plan-outline__number">${number}</span>`));
  }
  assert.match(html,/进入该分支 3 人/);
  assert.match(html,/体验建议/);
  assert.equal(JSON.stringify(plan),before);
});

test('missing or empty metadata preserves the original rendering, and inserted text is escaped', () => {
  const f=fixture(), plan=sample();
  assert.equal(f.build(plan), f.build(plan, []));
  assert.equal(f.build(plan, null), f.build(plan, []));
  assert.doesNotMatch(f.build(plan), /固定开篇/);
  assert.match(f.build(plan), /Part 1/);
  assert.match(f.build(plan), /1\.1/);
  const html=f.build(plan, ['<img src=x onerror=alert(1)>']);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img/);
});

test('already-planned session receives the preview using the existing options request, without replanning', async () => {
  const f=fixture();let requests=0;
  f.ctx.fetch=async url=>{requests++;assert.equal(url,'/api/report/session-a/options');return {ok:true,json:async()=>({quick_enabled:false,postwrite_opening_sections:openings})};};
  vm.runInContext(optionsSource, f.ctx);
  f.ctx.showPlanCard(sample(), ['原问卷题目']);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests,1);
  assert.match(f.nodes.get('plan-card-content').innerHTML, /固定开篇/);
  assert.match(f.nodes.get('plan-card-content').innerHTML, /Part 4/);
  assert.equal(f.nodes.get('btn-plan-ok').disabled,false);
});

test('late metadata from another session cannot repaint the current plan', async () => {
  const f=fixture();let finish;
  f.ctx.loadReportStyleOptions=()=>new Promise(resolve=>{finish=resolve;});
  f.ctx.showPlanCard(sample(), ['原问卷题目']);
  const before=f.nodes.get('plan-card-content').innerHTML;
  f.ctx.state.sessionId='session-b';finish({postwrite_opening_sections:openings});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(f.nodes.get('plan-card-content').innerHTML,before);
});

test('late metadata from a superseded revision cannot replace the newer plan', async () => {
  const f=fixture(), callbacks=[];
  f.ctx.loadReportStyleOptions=()=>new Promise(resolve=>callbacks.push(resolve));
  f.ctx.showPlanCard(sample(), ['原问卷题目']);
  const revised=sample();revised.parts[0].name='修订后的业务分组';
  f.ctx.showPlanCard(revised, ['原问卷题目']);
  callbacks[1]({postwrite_opening_sections:openings});await new Promise(resolve=>setImmediate(resolve));
  const before=f.nodes.get('plan-card-content').innerHTML;
  callbacks[0]({postwrite_opening_sections:[]});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(f.nodes.get('plan-card-content').innerHTML,before);
  assert.match(before,/修订后的业务分组/);
});

test('options failure keeps the old plan usable and returns no opening claim', async () => {
  const f=fixture();let refresh=0;
  f.ctx.fetch=async()=>({ok:false,json:async()=>({detail:'合成读取失败'})});
  f.ctx.renderSurveyFocus=()=>{refresh++;};
  vm.runInContext(optionsSource,f.ctx);
  const options=await f.ctx.loadReportStyleOptions();
  assert.equal(options,null);assert.equal(refresh,1);
  f.ctx.showPlanCard(sample(), ['原问卷题目']);await new Promise(resolve=>setImmediate(resolve));
  assert.doesNotMatch(f.nodes.get('plan-card-content').innerHTML,/固定开篇/);
  assert.match(f.nodes.get('plan-card-content').innerHTML,/原有业务分组/);
});

test('quick-report option state and focus refresh still work, with no additional fetch', async () => {
  const f=fixture();let requests=0,refresh=0;
  f.ctx.state.reportMode='quick';
  f.ctx.fetch=async()=>{requests++;return {ok:true,json:async()=>({quick_enabled:true,postwrite_opening_sections:[]})};};
  f.ctx.renderSurveyFocus=()=>{refresh++;};
  vm.runInContext(optionsSource,f.ctx);
  const options=await f.ctx.loadReportStyleOptions();
  assert.equal(f.ctx.state.reportStyleSelection.enabled,true);
  assert.equal(f.ctx.state.reportStyleSelection.value,'quick');
  assert.equal(options.postwrite_opening_sections.length,0);
  assert.equal(requests,1);assert.equal(refresh,1);
});
