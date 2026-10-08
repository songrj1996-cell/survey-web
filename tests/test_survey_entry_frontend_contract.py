"""DOM contracts and the context draft race; browser flows are verified separately."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1] / "static"
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
ENTRY = (ROOT / "js/features/survey-entry.js").read_text(encoding="utf-8")
SURVEY = (ROOT / "js/features/survey.js").read_text(encoding="utf-8")


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.nodes = {}
        self.feed(HTML)

    def handle_starttag(self, tag, attrs):
        node_id = dict(attrs).get("id")
        if node_id:
            self.ids.append(node_id)
            self.nodes[node_id] = (tag, dict(attrs))


class SurveyEntryFrontendContractTests(unittest.TestCase):
    def test_entry_dom_references_exist_once(self):
        ids = Elements().ids
        for node_id in set(re.findall(r"\$\('(qe-[a-z-]+)'\)", ENTRY + SURVEY)):
            self.assertEqual(ids.count(node_id), 1, node_id)

    def test_capability_copy_and_service_account_are_explicit(self):
        for text in ("需要为谷歌服务账号添加问卷编辑权限",
                     "nancysong@research-and-analysis-platform.iam.gserviceaccount.com",
                     "仅支持倍市得导出的 XLS / XLSX", "仅适配倍市得专业统计表 XLSX"):
            self.assertIn(text, HTML)

    def test_both_focus_choices_and_recovery_controls_remain_visible_in_dom(self):
        for value in ("quick", "insight", "statistics"):
            self.assertIn(f'data-entry-focus="{value}"', HTML)
        for node_id in ("qe-focus-lock", "qe-return-upload", "qe-plan-settings", "qe-retry-plan"):
            self.assertIn(node_id, Elements().ids)

    def test_settings_persist_before_planning_and_external_lock_is_source_scoped(self):
        self.assertIn("surveyEntry.method === 'local' && !!surveyEntry.files.statistics", ENTRY)
        self.assertLess(ENTRY.index("fetch('/api/analysis-settings/"), ENTRY.index("await startPlan()"))
        self.assertIn("body: JSON.stringify({ report_mode: surveyEntry.focus })", ENTRY)

    def test_readonly_google_panel_blocks_keyboard_and_pointer_interaction(self):
        self.assertIn("toggleAttribute('inert', locked)", ENTRY)
        self.assertIn("state.currentStep > 1", ENTRY)

    def test_confirmation_is_one_page_in_focus_columns_background_order(self):
        nodes = Elements().nodes
        self.assertLess(HTML.index('id="qe-focus-panel"'), HTML.index('id="col-list"'))
        self.assertLess(HTML.index('id="col-list"'), HTML.index('id="context-form-wrap"'))
        self.assertLess(HTML.index('id="context-form-wrap"'), HTML.index('id="btn-start-plan"'))
        for node_id in ("qe-data-confirm", "qe-focus-panel"):
            self.assertNotIn("hidden", nodes[node_id][1])
            self.assertNotIn(f"$('{node_id}').hidden", ENTRY)
        for old_control in ("qe-back-confirm", "qe-save-focus", "showSurveyFocus"):
            self.assertNotIn(old_control, HTML + ENTRY + SURVEY)
        self.assertIn("确认并生成分析方案", HTML)
        self.assertEqual(SURVEY.count("addEventListener('click', () => submitSurveyEntry())"), 1)

    def test_background_defaults_open_while_preview_and_settings_stay_collapsed(self):
        nodes = Elements().nodes
        self.assertEqual(nodes["context-form-details"][0], "details")
        self.assertIn("open", nodes["context-form-details"][1])
        for node_id in ("qe-focus-preview", "qe-plan-details"):
            self.assertEqual(nodes[node_id][0], "details")
            self.assertNotIn("open", nodes[node_id][1])
        for node_id in ("ctx-problem", "ctx-key-concerns", "ctx-target-users", "ctx-analysis-approach"):
            self.assertEqual(nodes[node_id][0], "textarea")
        self.assertIn("补充调研背景（选填）", HTML)
        self.assertIn('for="ctx-analysis-approach">期望的分析思路</label>', HTML)

    def test_compact_settings_do_not_replace_analysis_thinking(self):
        self.assertLess(HTML.index('id="qe-plan-settings"'), HTML.index('id="plan-card-content"'))
        self.assertIn("qe-plan-summary", Elements().ids)
        for field in ("core_question", "report_organization", "supporting_analyses", "evidence_role"):
            self.assertIn(field, SURVEY)
        self.assertIn("本次分析重点", SURVEY)

    def test_single_submit_freezes_input_and_keeps_existing_persistence(self):
        self.assertIn("toggleAttribute('inert', inputLocked)", ENTRY)
        submit = ENTRY.split("async function submitSurveyEntry()", 1)[1].split("function returnToSurveyFocus()", 1)[0]
        self.assertLess(submit.index("entrySetBusy(true)"), submit.index("await fetch("))
        self.assertIn("surveyEntryBusy() || !state.sessionId || state.currentStep > 2", submit)
        self.assertIn("finally { entrySetBusy(false);", submit)
        self.assertIn("readContextForm()", submit)
        back = ENTRY.split("function returnToSurveyFocus()", 1)[1].split("$('qe-retry-plan')", 1)[0]
        self.assertIn("goStep(2)", back)
        self.assertNotIn("renderColumns", back)
        self.assertNotIn("clearContext", back)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for draft verification")
    def test_column_draft_serialization_is_independent_of_visible_rows(self):
        result = subprocess.run([shutil.which("node"), str(ROOT.parent / "tests/test_survey_columns_frontend.js")], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_quick_accepts_objective_only_selection(self):
        submit = ENTRY.split('async function submitSurveyEntry()', 1)[1].split('function returnToSurveyFocus()', 1)[0]
        self.assertIn('selectedColumnsForSave(state.columns, state.selectedQuestionKeys).length', submit)
        self.assertNotIn('state.columns.some(c => isSubjectiveColumn', submit)

    def test_question_tabs_and_selection_are_separate_from_role(self):
        tabs = re.findall(r'data-question-filter="(\w+)"', HTML)
        self.assertEqual(tabs, ["all", "subjective", "pending"])
        self.assertIn("selected_question_keys: selectedColumnsForSave", SURVEY)
        self.assertIn("state.columnFilter = 'all'", SURVEY)
        self.assertIn("state.selectedQuestionKeys = state.columns.filter(isSelectableColumn)", SURVEY)
        self.assertIn('id="qe-column-editor"', HTML)

    def test_manual_plan_editor_is_removed_without_removing_revision_controls(self):
        css = (ROOT / "survey-entry.css").read_text(encoding="utf-8")
        for removed in ("qe-plan-edit", "qe-plan-part-editor", "manualPlanEditor", "ManualPlanEditor"):
            self.assertNotIn(removed, HTML + SURVEY + css)
        for node_id in ("plan-input", "btn-plan-ok", "btn-plan-revise", "qe-column-editor"):
            self.assertIn(node_id, Elements().ids)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for plan interaction checks")
    def test_confirmation_revision_and_column_unload_guard_remain_functional(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const elements = {};
const element = id => elements[id] ||= {
  value: '', style: {}, disabled: false,
  addEventListener(name, callback) { this[name] = callback; },
  querySelector() { return this; },
};
const initialPlan = {parts: [{name: 'Original'}], columns: [{index: 0}]};
const revisedPlan = {parts: [{name: 'Revised'}], columns: [{index: 0}]};
const requests = [], rendered = [], errors = [], pending = [];
let statsCalls = 0, unload, flushes = 0;
const sandbox = {
  $: element,
  state: {sessionId: 'synthetic-session', planData: initialPlan},
  selectedReportStyle: () => 'full', lockReportStyleSelection() {},
  syncPlanActionButtons() {}, _updateProgressStatus() {}, showToast() {},
  showBlockingFlowError: (...args) => errors.push(args),
  showPlanCard: (...args) => rendered.push(args),
  runStats: async () => { statsCalls++; },
  consumeSSEPost: async (url, body, onEvent) => {
    requests.push({url, body});
    if (body.user_text === 'ok') onEvent({type: 'json', approved: true});
    else onEvent({type: 'plan_ready', plan: revisedPlan, headers: ['Question']});
  },
  window: {addEventListener(name, callback) {
    assert.equal(name, 'beforeunload'); unload = callback;
  }},
  columnEditor: null,
  flushColumnEditor() { flushes++; },
};
vm.createContext(sandbox);
vm.runInContext(source.slice(source.indexOf("$('btn-plan-ok').addEventListener")), sandbox);
// Track promises from the real button/keyboard handlers, which intentionally return void.
vm.runInContext('const originalConfirmPlan = confirmPlan; confirmPlan = text => track(originalConfirmPlan(text));',
  Object.assign(sandbox, {track: promise => { pending.push(promise); return promise; }}));
(async () => {
  element('btn-plan-ok').click();
  await pending.shift();
  assert.equal(requests[0].url, '/api/plan/confirm');
  assert.equal(requests[0].body.plan, initialPlan);
  assert.equal(requests[0].body.report_style, 'full');
  assert.equal(statsCalls, 1);
  element('plan-input').value = 'Change chapter order';
  element('btn-plan-ok').click();
  assert.equal(requests.length, 1, 'unsubmitted feedback must block approval');
  element('btn-plan-revise').click();
  await pending.shift();
  assert.equal(requests[1].body.user_text, 'Change chapter order');
  assert.equal(Object.hasOwn(requests[1].body, 'plan'), false);
  assert.equal(sandbox.state.planData, revisedPlan);
  assert.equal(rendered[0][0], revisedPlan);
  assert.equal(statsCalls, 1, 'revision must wait for confirmation');
  element('plan-input').value = '';
  element('btn-plan-ok').click();
  await pending.shift();
  assert.equal(requests[2].body.plan, revisedPlan);
  assert.equal(statsCalls, 2);
  element('plan-input').value = 'Keyboard revision';
  let keyPrevented = false;
  element('plan-input').keydown({key: 'Enter', ctrlKey: true,
    preventDefault() { keyPrevented = true; }});
  await pending.shift();
  assert.equal(keyPrevented, true);
  assert.equal(requests[3].body.user_text, 'Keyboard revision');
  assert.deepEqual(errors, []);
  for (const draft of [null, {name: 'Original'}, {name: 'Changed'}]) {
    sandbox.columnEditor = draft && {draft, initial: JSON.stringify({name: 'Original'})};
    let prevented = false;
    const event = {preventDefault() { prevented = true; }};
    unload(event);
    assert.equal(prevented, draft?.name === 'Changed');
    if (prevented) assert.equal(event.returnValue, '');
  }
  assert.equal(flushes, 2);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(ROOT / "js/features/survey.js")],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the draft timing regression")
    def test_column_readiness_preserves_unsaved_input_and_intentionally_cleared_fields(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const contextCode = source.slice(source.indexOf('const CONTEXT_DRAFT_KEY'),
  source.indexOf('let duplicateReportResolve'));
const ids = ['ctx-problem', 'ctx-key-concerns', 'ctx-target-users', 'ctx-analysis-approach'];
const keys = ['problem', 'key_concerns', 'target_users', 'analysis_approach'];
const fields = Object.fromEntries(keys.map(key => [key, '旧草稿']));
for (const text of ['用户刚输入的新内容', '']) {
  let saved = JSON.stringify({ fileSignature: 'sample', fields });
  let pending;
  const elements = { 'context-form-wrap': { style: {} } };
  for (const id of ids) elements[id] = {
    value: '旧草稿',
    addEventListener(name, callback) { this[name] = callback; },
  };
  const sandbox = {
    $: id => elements[id],
    localStorage: { getItem: () => saved, setItem: (_, value) => { saved = value; } },
    setTimeout: callback => { pending = callback; return 1; },
    clearTimeout() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(contextCode, sandbox);
  vm.runInContext("currentContextFileSignature = 'sample';", sandbox);
  for (const id of ids) {
    elements[id].value = text;
    elements[id].input();
  }
  // The SSE callback runs before the 400ms draft save, including while editing.
  vm.runInContext('refreshContextFormVisibility();', sandbox);
  for (const id of ids) assert.equal(elements[id].value, text, id);
  pending();
  for (const key of keys) assert.equal(JSON.parse(saved).fields[key], text, key);
}
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(ROOT / "js/features/survey.js")],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
