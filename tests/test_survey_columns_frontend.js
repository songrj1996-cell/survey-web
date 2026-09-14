// Exercise the production draft model without depending on rendered rows.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../static/js/features/survey.js'),'utf8');
const model = source.split('// COLUMN_MODEL_START:')[1].split('// COLUMN_MODEL_END')[0];
const context = vm.createContext({});
vm.runInContext(model.slice(model.indexOf('function cloneColumn')),context);
const plain = value => JSON.parse(JSON.stringify(value));
const sample = [
  {column_indexes:[0],role:'id',name:'Player ID'},
  {column_indexes:[2],role:'single_choice',name:'Q5. 活动奖励',options:['金币','道具'],value_aliases:{'金币':['gold']},other_text:{enabled:true,option:'其他'}},
  {column_indexes:[7,8],role:'matrix_multi',name:'矩阵',rows:['体验','奖励'],options:['喜欢','不喜欢'],delimiter:'\n'},
  {column_indexes:[11],role:'open_text',name:'其他建议',low_confidence:true},
  {column_indexes:[12],role:'ignore',empty_column:true,name:''},
];
const drafts = sample.map(context.prepareColumnDraft);
assert.equal(context.columnQuestionKey(drafts[2]),'7:8');
assert.equal(context.columnQuestionKey({column_indexes:[8,7]}),'7:8');
assert.equal(context.columnSourceLabel(drafts[1],1),'Q5');
assert.equal(context.columnSourceLabel(drafts[3],3,drafts),'Q3');
assert.equal(context.columnSourceLabel(drafts[2],2,drafts),'Q2');
assert.equal(context.columnSourceLabel(drafts[0],0,drafts),'身份');
assert.equal(context.isMainConfirmationColumn(drafts[0]),true);
assert.equal(context.isMainConfirmationColumn(drafts[4]),false);
assert.equal(context.isSelectableColumn({role:'mlbbid',column_indexes:[30]}),false);
assert.equal(context.columnSourceLabel({role:'mlbbid',name:'Q99',question_number:99},1,drafts),'身份');
assert.equal(context.isSelectableColumn(drafts[0]),false);
assert.equal(context.isSelectableColumn(drafts[4]),false);
assert.equal(context.isSubjectiveColumn(drafts[1]),true);
context.state = {columns:drafts,columnFilter:'all'};
vm.runInContext(source.slice(source.indexOf('function visibleQuestionIndexes()'),source.indexOf('function renderQuestionList()')),context);
assert.deepEqual(plain(context.visibleQuestionIndexes()),[0,1,2,3],'identity remains in the default list');
context.state.columnFilter='subjective';
assert.deepEqual(plain(context.visibleQuestionIndexes()),[1,3]);
let keys = drafts.filter(context.isSelectableColumn).map(context.columnQuestionKey);
assert.deepEqual(plain(keys),['2','7:8','11']);
keys = keys.filter(k=>k!=='2');
// Switching display filters or report modes cannot change the authoritative list.
for(const mode of ['quick','insight','statistics']) {
  const displayed=drafts.filter(c=>c.role==='open_text');
  assert.equal(displayed.length,1);
  const saved=drafts.map(context.serializeColumnDraft);
  assert.equal(saved.length,5,mode);
  assert.deepEqual(plain(saved[2].rows),['体验','奖励']);
  assert.equal(saved[2].delimiter,'\n');
  assert.deepEqual(plain(context.selectedColumnsForSave(saved,keys)),['7:8','11']);
}
// Drawer discard keeps all settings, while editing and changing type preserves them.
const edited=context.cloneColumn(drafts[1]);
context.renameColumnOptions(edited,[{value:'游戏金币',previous:'金币',aliases:['gold']},{value:'礼包',previous:'道具',aliases:[]}]);
assert.deepEqual(plain(edited.value_aliases['游戏金币']),['gold','金币']);
assert.deepEqual(plain(drafts[1].options),['金币','道具']);
edited.role='scale';edited.scale_min=0;edited.scale_max=10;edited.role='single_choice';
assert.deepEqual(plain(context.serializeColumnDraft(edited).options),['游戏金币','礼包','其他']);
assert.equal(edited.scale_min,0);
assert.deepEqual(plain(edited.options_original),['金币','道具']);
// Renames and removals must not infer aliases by a shifted positional index.
context.renameColumnOptions(edited,[{value:'礼包',previous:'礼包',aliases:['道具']}]);
assert.equal(edited.value_aliases['礼包'].includes('金币'),false);
const unmatched=context.prepareColumnDraft({column_indexes:[20],role:'multi_choice',options:['A'],unmatched_values:[{value:'free tail',count:2}]});
assert.equal(context.isSubjectiveColumn(unmatched),true);
unmatched.unmatched_handling='keep_raw';unmatched.other_text.enabled=false;
const retained=context.serializeColumnDraft(unmatched);
assert.equal(retained.other_text.enabled,false);
assert.deepEqual(plain(retained.unmatched_values),[{value:'free tail',count:2}]);
assert.equal(retained.options.includes('Other / 其他'),false);
assert.deepEqual(sample[1].options,['金币','道具'],'source input is never mutated');
console.log('Column draft cases passed: source order, all defaults, filters, unselected keys, matrix, type restoration, aliases, discard, Other/raw.');
