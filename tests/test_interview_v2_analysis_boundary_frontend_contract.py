import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "static" / "js" / "features" / "interview-v2.js"
CSS_PATH = ROOT / "static" / "style.css"
INDEX_PATH = ROOT / "static" / "index.html"


class InterviewV2AnalysisBoundaryFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = JS_PATH.read_text(encoding="utf-8")
        cls.css = CSS_PATH.read_text(encoding="utf-8")
        cls.index = INDEX_PATH.read_text(encoding="utf-8")

    def _between(self, start: str, end: str) -> str:
        start_index = self.js.index(start)
        end_index = self.js.index(end, start_index)
        return self.js[start_index:end_index]

    def test_reuses_existing_step3_shell_without_new_index_markup(self):
        self.assertIn('id="iv-v2-confirmed-shell"', self.index)
        self.assertIn('id="iv-v2-confirmed-preview"', self.index)
        self.assertIn("const IV_V2_BOUNDARY_TABS = ['review', 'evaluation_objects', 'analysis_scope', 'coverage'];", self.js)
        for label in ('1 结构问题', '2 方案结构', '3 分析边界', '4 覆盖预览'):
            with self.subTest(label=label):
                self.assertIn(label, self.js)

    def test_uses_only_the_four_analysis_boundary_apis(self):
        for path in (
            '/analysis-boundary`',
            '/analysis-boundary`, {',
            '/analysis-boundary:confirm`',
            '/coverage-preview`',
        ):
            with self.subTest(path=path):
                self.assertIn(path, self.js)
        self.assertEqual(self.js.count('/analysis-boundary:confirm`'), 1)
        self.assertEqual(self.js.count('/coverage-preview`'), 1)

    def test_proposal_is_editable_and_first_put_uses_four_base_heads(self):
        toolbar = self._between(
            'function ivV2BoundaryToolbarHtml()',
            'function ivV2OccurrenceLabel',
        )
        payload = self._between(
            'function ivV2BoundaryWritePayload()',
            'function ivV2BoundaryConfirmPayload()',
        )
        self.assertIn(
            'const canSave = (!persisted || ivV2State.boundaryDirty) && !ivV2State.boundaryConflict;',
            toolbar,
        )
        self.assertIn("persisted ? '保存新版本' : '采用建议并保存'", toolbar)
        self.assertIn('base_structure_revision_id: ivV2CurrentStructureRevisionId()', payload)
        self.assertIn('base_evidence_revision_id: ivV2CurrentEvidenceRevisionId()', payload)
        self.assertIn('base_boundary_revision_id: ivV2CurrentBoundaryRevisionId() || null', payload)
        self.assertIn('base_coverage_revision_id: ivV2CurrentCoverageRevisionId() || null', payload)

    def test_put_uses_final_public_boundary_field_names(self):
        payload = self._between(
            'function ivV2BoundaryWritePayload()',
            'function ivV2BoundaryConfirmPayload()',
        )
        for field in (
            'object_type:',
            'display_name:',
            'scope_type:',
            'label_name:',
            'scope_mode:',
            'module_ids:',
            'evaluation_object_ids:',
        ):
            with self.subTest(field=field):
                self.assertIn(field, payload)
        for stale_field in (
            'kind:',
            'canonical_name:',
            'usage:',
            'scope_ids:',
        ):
            with self.subTest(stale_field=stale_field):
                self.assertNotIn(stale_field, payload)

    def test_confirm_payload_is_the_exact_four_item_cas(self):
        payload = self._between(
            'function ivV2BoundaryConfirmPayload()',
            'function ivV2FormatResolutionLabel',
        )
        for field in (
            'boundary_revision_id:',
            'coverage_revision_id:',
            'boundary_payload_sha256:',
            'coverage_payload_sha256:',
        ):
            with self.subTest(field=field):
                self.assertEqual(payload.count(field), 1)
        for forbidden in (
            'revision_number',
            'structure_revision_id',
            'evidence_revision_id',
            'base_boundary_revision_id',
            'base_coverage_revision_id',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, payload)

    def test_confirm_response_atomically_replaces_n_plus_one_heads(self):
        current_coverage = self._between(
            'function ivV2CurrentCoverageRevisionId()',
            'function ivV2BoundaryConfirmationHeadsReady()',
        )
        confirm = self._between(
            'async function ivV2ConfirmAnalysisBoundary()',
            'async function ivV2LoadStructureWorkspace',
        )
        self.assertLess(
            current_coverage.index('ivV2State.boundaryResponse?.coverage_revision_id'),
            current_coverage.index('ivV2State.coverageResponse?.coverage_revision_id'),
        )
        self.assertIn('const merged = { ...ivV2State.boundaryResponse, ...data };', confirm)
        self.assertIn('const submittedHeads = ivV2BoundaryConfirmPayload();', confirm)
        self.assertIn('!ivV2PersistedBoundaryResponseReady(data)', confirm)
        self.assertIn('data.boundary_revision_id === submittedHeads.boundary_revision_id', confirm)
        self.assertIn('data.coverage_revision_id === submittedHeads.coverage_revision_id', confirm)
        self.assertIn('ivV2State.boundaryResponse = merged;', confirm)
        self.assertIn('ivV2State.coverageResponse = data.coverage_preview ? data : null;', confirm)

    def test_statuses_conflict_retention_and_monotonic_tokens_are_explicit(self):
        for status in (
            'ANALYSIS_BOUNDARY_REQUIRED',
            'ANALYSIS_BOUNDARY_REVIEW_REQUIRED',
            'READY_FOR_DOSSIERS',
        ):
            with self.subTest(status=status):
                self.assertIn(status, self.js)
        self.assertIn('ivV2State.boundaryToken += 1;', self.js)
        self.assertIn('ivV2State.coverageToken += 1;', self.js)
        self.assertNotIn('ivV2State.boundaryToken = 0;', self.js)
        self.assertNotIn('ivV2State.coverageToken = 0;', self.js)
        self.assertIn('本地草稿仍保留', self.js)
        self.assertIn('boundary-discard-and-refresh', self.js)
        self.assertIn('if (response.status === 409)', self.js)
        structure_load = self._between(
            'async function ivV2LoadStructureWorkspace',
            'function ivV2StructureBuildPayload',
        )
        self.assertIn('if (ivV2State.boundaryDirty && ivV2State.boundaryDraft)', structure_load)
        self.assertIn('本地分析边界草稿仍保留', structure_load)

        save = self._between(
            'async function ivV2SaveAnalysisBoundary()',
            'async function ivV2LoadCoveragePreview',
        )
        confirm = self._between(
            'async function ivV2ConfirmAnalysisBoundary()',
            'async function ivV2LoadStructureWorkspace',
        )
        controls = self._between(
            'function ivV2SyncConfirmedControls()',
            'function ivV2SyncCommentCounter',
        )
        self.assertIn('|| ivV2State.boundaryConflict', save)
        self.assertIn('|| ivV2State.boundaryConflict', confirm)
        self.assertIn('|| Boolean(ivV2State.boundaryConflict)', controls)
        self.assertIn('&& !ivV2State.boundaryConflict', controls)
        self.assertIn('function ivV2HasUnsavedWork()', self.js)
        self.assertIn('return Boolean(ivV2State.draftDirty || ivV2State.boundaryDirty);', self.js)
        for operation in (
            'async function ivV2RefreshImportBundleFromEditor()',
            'async function ivV2StartImport()',
            'async function ivV2ConfirmMapping()',
            'function ivV2StartOver()',
        ):
            with self.subTest(operation=operation):
                body = self._between(operation, '\n}')
                self.assertIn('ivV2ConfirmDiscardUnsaved(', body)
        load_bundle = self._between(
            'async function ivV2LoadImportBundle(',
            'async function ivV2RefreshImportBundleFromEditor()',
        )
        start_import = self._between(
            'async function ivV2StartImport()',
            'function ivV2SheetName',
        )
        poll_attempt = self._between(
            'async function ivV2PollAttempt(',
            'async function ivV2StartImport()',
        )
        self.assertIn('resetWorkspace = true', load_bundle)
        self.assertIn('if (resetWorkspace) ivV2ResetStructureWorkspace();', load_bundle)
        self.assertIn(
            'await ivV2LoadImportBundle(data.import_id, { token, resetWorkspace: false });',
            start_import,
        )
        self.assertIn(
            'await ivV2LoadImportBundle(data.import_id, { token, resetWorkspace: false });',
            poll_attempt,
        )

    def test_object_source_and_label_edit_contracts_are_present(self):
        self.assertIn("const IV_V2_SOURCE_SCOPE_TYPES = ['interview_body', 'participant_background', 'excluded'];", self.js)
        self.assertIn("const IV_V2_LABEL_SCOPE_MODES = ['disabled', 'all_analysis', 'selected_modules', 'selected_evaluation_objects'];", self.js)
        for action in (
            'boundary-object-name',
            'boundary-object-up',
            'boundary-object-down',
            'boundary-object-split',
            'boundary-object-merge',
            'boundary-source-scope-type',
            'boundary-source-split',
            'boundary-label-scope-mode',
            'boundary-label-target',
        ):
            with self.subTest(action=action):
                self.assertIn(action, self.js)
        self.assertIn("supersedes_evaluation_object_ids: selectedObjectIds", self.js)
        self.assertIn("showToast('请选择系统允许的安全分段位置'", self.js)
        boundary_draft = self._between(
            'function ivV2BuildBoundaryDraft',
            'function ivV2EvaluationObjectKey',
        )
        self.assertIn('allowed_split_rows: Array.isArray(item.allowed_split_rows)', boundary_draft)
        self.assertIn("? item.allowed_split_rows.map(Number).filter(Number.isFinite)\n          : []", boundary_draft)
        self.assertIn('compatible_structure_rows:', boundary_draft)
        split_source = self._between(
            'function ivV2SplitSourceScopeRule',
            'function ivV2ValidateBoundaryDraft',
        )
        self.assertIn("const allowed = new Set((item?.allowed_split_rows || []).map(Number));", split_source)

    def test_coverage_is_read_only_and_denominator_is_backend_authoritative(self):
        denominator = self._between(
            'function ivV2CoverageDenominatorText',
            'function ivV2CoverageCellNeedsAttention',
        )
        self.assertIn("if (!summary || summary.denominator_reliable !== true) return '口径待确认';", denominator)
        self.assertIn('summary.denominator_participant_count', denominator)
        self.assertIn('summary.covered_participant_count', denominator)
        self.assertNotIn('.filter(', denominator)
        self.assertNotIn('.reduce(', denominator)
        self.assertIn('只读覆盖预览', self.js)
        self.assertIn('缺少资料不等于未询问；三维状态未确认时不展示分母。', self.js)
        self.assertNotIn('data-iv-v2-action="boundary-coverage-edit', self.js)

    def test_server_and_user_strings_are_escaped_in_boundary_rendering(self):
        for escaped in (
            "ivV2Esc(item.display_name || '')",
            'ivV2Esc(item.label_name || item.label_key)',
            'ivV2Esc(item.sheet_name || item.sheet_id)',
            "ivV2Esc(ivV2State.boundaryConflict.message || '服务端版本已变化。')",
            'ivV2Esc(participant.participant_label || participant.display_name || participantId)',
            'ivV2Esc(evidenceIds.join(\'、\'))',
        ):
            with self.subTest(escaped=escaped):
                self.assertIn(escaped, self.js)

    def test_new_styles_are_strictly_iv_v2_namespaced(self):
        for selector in (
            '.iv-v2-boundary-tabs',
            '.iv-v2-boundary-toolbar',
            '.iv-v2-evaluation-card',
            '.iv-v2-source-scope-rule',
            '.iv-v2-label-scope-rule',
            '.iv-v2-coverage-table',
            '.iv-v2-coverage-cell',
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, self.css)

    def test_node_vm_exercises_boundary_state_machine_and_identity_edits(self):
        scenario = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
let uuidCounter = 0;
const toasts = [];
const documentStub = {
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
};
const windowStub = {
  ivState: { track: 'v2' },
  setTimeout: () => 1,
  confirmResult: true,
  confirmCallCount: 0,
  confirm() {
    this.confirmCallCount += 1;
    return this.confirmResult;
  },
  crypto: {
    randomUUID: () => {
      uuidCounter += 1;
      return `00000000-0000-4000-8000-${uuidCounter.toString(16).padStart(12, '0')}`;
    },
  },
};
const context = {
  console,
  assert,
  document: documentStub,
  window: windowStub,
  currentMode: 'interview',
  fetch: async () => { throw new Error('unexpected fetch'); },
  confirm: (...args) => windowStub.confirm(...args),
  showToast: (...args) => toasts.push(args),
  esc: value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]),
  ivGoStep: () => {},
  setTimeout,
  clearTimeout,
  FormData: class FormData {
    append() {}
  },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: 'interview-v2.js' });

vm.runInContext(`(async () => {
  const id = (prefix, char) => prefix + '_' + char.repeat(32);
  const moduleId = id('module', '1');
  const question1 = id('question', '2');
  const question2 = id('question', '3');
  const occurrence1 = id('occ', '4');
  const occurrence2 = id('occ', '5');
  const occurrence3 = id('occ', '6');
  const structure1 = id('structure', '7');
  const evidence1 = id('evidence', '8');
  const structure2 = id('structure', '9');
  const evidence2 = id('evidence', 'a');
  const makeObject = (objectId, name, occurrenceIds, questionIds = [question1]) => ({
    evaluation_object_id: objectId,
    module_id: moduleId,
    parent_evaluation_object_id: '',
    object_type: 'concept',
    display_name: name,
    display_order: 1,
    main_question_ids: questionIds,
    occurrence_ids: occurrenceIds,
    supersedes_evaluation_object_ids: [],
    decision_status: 'draft',
    decision_source: 'user_selection',
    _lineage_anchor: true,
  });
  const setStructure = (structureRevisionId = structure1, evidenceRevisionId = evidence1) => {
    ivV2State.structureResponse = {
      structure_revision_id: structureRevisionId,
      evidence_revision_id: evidenceRevisionId,
      review_summary: { blocking_issue_count: 0 },
      structure: {
        modules: [{ module_id: moduleId, canonical_name: '模块一' }],
        main_questions: [
          { main_question_id: question1, module_id: moduleId, canonical_text: '问题一' },
          { main_question_id: question2, module_id: moduleId, canonical_text: '问题二' },
        ],
        occurrences: [
          { occurrence_id: occurrence1, sheet_id: 'sheet-a', row: 3, canonical_module_id: moduleId, canonical_main_question_id: question1 },
          { occurrence_id: occurrence2, sheet_id: 'sheet-a', row: 5, canonical_module_id: moduleId, canonical_main_question_id: question1 },
          { occurrence_id: occurrence3, sheet_id: 'sheet-a', row: 7, canonical_module_id: moduleId, canonical_main_question_id: question2 },
        ],
      },
    };
    ivV2State.reviewIssuesResponse = {
      structure_revision_id: structureRevisionId,
      evidence_revision_id: evidenceRevisionId,
      issues: [],
    };
  };
  setStructure();
  ivV2State.importId = 'import-vm';
  ivV2State.boundaryResponse = {
    structure_revision_id: structure1,
    evidence_revision_id: evidence1,
    boundary_revision_id: null,
    coverage_revision_id: null,
    boundary_payload_sha256: null,
    coverage_payload_sha256: null,
  };
  ivV2State.coverageResponse = null;
  ivV2State.boundaryDraft = { evaluation_objects: [], source_scope_rules: [], label_scope_rules: [] };
  let payload = ivV2BoundaryWritePayload();
  assert.strictEqual(payload.base_structure_revision_id, structure1);
  assert.strictEqual(payload.base_evidence_revision_id, evidence1);
  assert.strictEqual(payload.base_boundary_revision_id, null);
  assert.strictEqual(payload.base_coverage_revision_id, null);
  assert.match(ivV2BoundaryToolbarHtml(), /data-iv-v2-action="boundary-save"[\\s\\S]*?data-iv-v2-locked="false"/);

  const staleBoundaryId = id('boundary', 'b');
  const staleCoverageId = id('coverage', 'c');
  ivV2State.boundaryConflict = { code: 'ANALYSIS_BOUNDARY_INPUT_STALE', message: 'refresh required' };
  fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      status: 'ANALYSIS_BOUNDARY_REQUIRED',
      is_stale: true,
      structure_revision_id: structure1,
      evidence_revision_id: evidence1,
      boundary_revision_id: staleBoundaryId,
      coverage_revision_id: staleCoverageId,
      boundary_payload_sha256: '1'.repeat(64),
      coverage_payload_sha256: '2'.repeat(64),
      analysis_boundary: { evaluation_objects: [], source_scope_rules: [], label_scope_rules: [] },
      coverage_preview: { rows: [], summaries: [] },
    }),
  });
  assert.strictEqual(await ivV2LoadAnalysisBoundary({ render: false }), true);
  assert.strictEqual(ivV2State.boundaryDirty, true);
  assert.strictEqual(ivV2State.boundaryConflict, null);
  assert.strictEqual(ivV2State.coverageResponse, null);
  payload = ivV2BoundaryWritePayload();
  assert.strictEqual(payload.base_boundary_revision_id, staleBoundaryId);
  assert.strictEqual(payload.base_coverage_revision_id, staleCoverageId);
  assert.match(ivV2BoundaryToolbarHtml(), /data-iv-v2-action="boundary-save"[\\s\\S]*?data-iv-v2-locked="false"/);

  const sourceRuleId = id('scope', 'b');
  ivV2State.boundaryDraft = {
    evaluation_objects: [],
    label_scope_rules: [],
    source_scope_rules: [{
      source_scope_rule_id: sourceRuleId,
      sheet_id: 'sheet-a',
      start_row: 3,
      end_row: 7,
      scope_type: 'interview_body',
      display_order: 1,
      allowed_split_rows: [5],
      compatible_structure_rows: [4],
    }],
  };
  ivV2State.boundarySplitRows = { [sourceRuleId]: 4 };
  ivV2SplitSourceScopeRule(sourceRuleId);
  assert.strictEqual(ivV2State.boundaryDraft.source_scope_rules.length, 1, 'compatibility rows must not authorize a split');
  ivV2State.boundarySplitRows[sourceRuleId] = 5;
  ivV2SplitSourceScopeRule(sourceRuleId);
  assert.deepStrictEqual(
    Array.from(ivV2State.boundaryDraft.source_scope_rules, item => [item.start_row, item.end_row]),
    [[3, 4], [5, 7]],
  );

  const splitOldId = id('evaluation', 'c');
  ivV2State.boundaryDraft = {
    evaluation_objects: [makeObject(splitOldId, '旧方案', [occurrence1, occurrence2])],
    source_scope_rules: [],
    label_scope_rules: [{
      label_scope_rule_id: id('label_scope', 'd'),
      label_key: 'experience',
      label_name: '体验',
      scope_mode: 'selected_evaluation_objects',
      module_ids: [],
      evaluation_object_ids: [splitOldId],
    }],
  };
  ivV2State.boundaryOccurrenceSelection = { [splitOldId]: [occurrence2] };
  ivV2State.boundaryMergeSelection = [];
  ivV2SplitEvaluationObject(splitOldId);
  let active = ivV2ActiveEvaluationObjects();
  assert.strictEqual(ivV2FindEvaluationObject(splitOldId).decision_status, 'superseded');
  assert.strictEqual(active.length, 2);
  assert(active.every(item => item.supersedes_evaluation_object_ids.includes(splitOldId)));
  assert.strictEqual(ivV2ValidateBoundaryDraft().ok, true);
  payload = ivV2BoundaryWritePayload();
  assert.strictEqual(payload.evaluation_objects.length, 3);
  assert.strictEqual(payload.evaluation_objects.find(item => item.evaluation_object_id === splitOldId).decision_status, 'superseded');

  const mergeLeftId = id('evaluation', 'e');
  const mergeRightId = id('evaluation', 'f');
  ivV2State.boundaryDraft = {
    evaluation_objects: [
      makeObject(mergeLeftId, '左方案', [occurrence1]),
      { ...makeObject(mergeRightId, '右方案', [occurrence3], [question2]), display_order: 2 },
    ],
    source_scope_rules: [],
    label_scope_rules: [],
  };
  ivV2State.boundaryMergeSelection = [mergeLeftId, mergeRightId];
  ivV2MergeEvaluationObjects();
  active = ivV2ActiveEvaluationObjects();
  assert.strictEqual(active.length, 1);
  assert.deepStrictEqual(Array.from(active[0].supersedes_evaluation_object_ids).sort(), [mergeLeftId, mergeRightId].sort());
  assert.strictEqual(ivV2FindEvaluationObject(mergeLeftId).decision_status, 'superseded');
  assert.strictEqual(ivV2FindEvaluationObject(mergeRightId).decision_status, 'superseded');
  assert.strictEqual(ivV2ValidateBoundaryDraft().ok, true);

  const parentId = id('evaluation', '1');
  const hierarchyOldId = id('evaluation', '2');
  ivV2State.boundaryDraft = {
    evaluation_objects: [
      makeObject(parentId, '父概念', [occurrence1]),
      { ...makeObject(hierarchyOldId, '待转方案', [occurrence3], [question2]), display_order: 2 },
    ],
    source_scope_rules: [],
    label_scope_rules: [{
      label_scope_rule_id: id('label_scope', '3'),
      label_key: 'hierarchy',
      label_name: '层级',
      scope_mode: 'selected_evaluation_objects',
      module_ids: [],
      evaluation_object_ids: [hierarchyOldId],
    }],
  };
  ivV2ChangeEvaluationObjectHierarchy(hierarchyOldId, parentId);
  let variant = ivV2ActiveEvaluationObjects().find(item => item.parent_evaluation_object_id === parentId);
  assert(variant);
  assert.strictEqual(variant.object_type, 'variant');
  assert.deepStrictEqual(Array.from(variant.supersedes_evaluation_object_ids), [hierarchyOldId]);
  assert.strictEqual(ivV2FindEvaluationObject(hierarchyOldId).decision_status, 'superseded');
  assert.strictEqual(ivV2ValidateBoundaryDraft().ok, true);
  ivV2State.boundaryDraft.evaluation_objects.forEach(item => { item._lineage_anchor = true; });
  ivV2ChangeEvaluationObjectHierarchy(variant.evaluation_object_id, '');
  const restoredConcept = ivV2ActiveEvaluationObjects().find(item => (
    item.evaluation_object_id !== parentId && item.object_type === 'concept'
  ));
  assert(restoredConcept);
  assert.deepStrictEqual(Array.from(restoredConcept.supersedes_evaluation_object_ids), [variant.evaluation_object_id]);
  assert.strictEqual(variant.decision_status, 'superseded');
  assert.strictEqual(ivV2ValidateBoundaryDraft().ok, true);

  ivV2State.boundaryDraft = {
    evaluation_objects: [makeObject(id('evaluation', '4'), '<img src=x onerror=alert(1)>', [occurrence1])],
    source_scope_rules: [],
    label_scope_rules: [],
  };
  ivV2State.boundaryConflict = { code: 'CONFLICT', message: '<script>alert(1)</script>' };
  const escapedHtml = ivV2EvaluationObjectsHtml() + ivV2BoundaryToolbarHtml();
  assert(!escapedHtml.includes('<img src=x'));
  assert(!escapedHtml.includes('<script>alert'));
  assert(escapedHtml.includes('&lt;img'));
  assert(escapedHtml.includes('&lt;script&gt;'));
  assert.strictEqual(ivV2CoverageDenominatorText({ summaries: [{
    evaluation_object_id: 'object-a',
    main_question_id: 'question-a',
    denominator_reliable: true,
    denominator_participant_count: 10,
    covered_participant_count: 3,
  }], rows: new Array(99).fill({}) }, 'object-a', 'question-a'), '3/10');
  assert.strictEqual(ivV2CoverageDenominatorText({ summaries: [{
    evaluation_object_id: 'object-a',
    main_question_id: 'question-a',
    denominator_reliable: false,
    denominator_participant_count: 10,
    covered_participant_count: 3,
  }] }, 'object-a', 'question-a'), '口径待确认');
  ivV2State.boundaryConflict = null;

  const boundaryN = id('boundary', '5');
  const coverageN = id('coverage', '6');
  const boundaryN1 = id('boundary', '7');
  const coverageN1 = id('coverage', '8');
  ivV2State.boundaryResponse = {
    structure_revision_id: structure1,
    evidence_revision_id: evidence1,
    boundary_revision_id: boundaryN,
    coverage_revision_id: coverageN,
    boundary_payload_sha256: 'a'.repeat(64),
    coverage_payload_sha256: 'b'.repeat(64),
    confirmation_ready: true,
  };
  ivV2State.coverageResponse = ivV2State.boundaryResponse;
  ivV2State.boundaryDirty = false;
  ivV2State.status = 'ANALYSIS_BOUNDARY_REVIEW_REQUIRED';
  fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      status: 'READY_FOR_DOSSIERS',
      structure_revision_id: structure1,
      evidence_revision_id: evidence1,
      boundary_revision_id: boundaryN1,
      coverage_revision_id: coverageN1,
      boundary_payload_sha256: 'c'.repeat(64),
      coverage_payload_sha256: 'd'.repeat(64),
      coverage_preview: { rows: [], summaries: [] },
    }),
  });
  assert.strictEqual(await ivV2ConfirmAnalysisBoundary(), true);
  assert.strictEqual(ivV2State.boundaryResponse.boundary_revision_id, boundaryN1);
  assert.strictEqual(ivV2State.boundaryResponse.coverage_revision_id, coverageN1);
  assert.strictEqual(ivV2State.coverageResponse.coverage_revision_id, coverageN1);
  assert.strictEqual(ivV2State.status, 'READY_FOR_DOSSIERS');

  const dirtyDraft = { marker: 'keep-me', evaluation_objects: [], source_scope_rules: [], label_scope_rules: [] };
  ivV2State.boundaryDraft = dirtyDraft;
  ivV2State.boundaryDirty = true;
  fetch = async () => ({
    ok: false,
    status: 409,
    json: async () => ({ error: { code: 'ANALYSIS_BOUNDARY_REVISION_CONFLICT', message: 'stale' } }),
  });
  assert.strictEqual(await ivV2LoadAnalysisBoundary({ render: false }), false);
  assert.strictEqual(ivV2State.boundaryDraft, dirtyDraft);
  assert.strictEqual(ivV2State.boundaryDirty, true);
  assert.strictEqual(ivV2State.boundaryConflict.code, 'ANALYSIS_BOUNDARY_REVISION_CONFLICT');

  setStructure(structure1, evidence1);
  ivV2State.boundaryDraft = dirtyDraft;
  ivV2State.boundaryDirty = true;
  let fetchCount = 0;
  fetch = async url => {
    fetchCount += 1;
    if (url.endsWith('/structure')) return {
      ok: true,
      status: 200,
      json: async () => ({
        status: 'ANALYSIS_BOUNDARY_REQUIRED',
        structure_revision_id: structure2,
        evidence_revision_id: evidence2,
        review_summary: { blocking_issue_count: 0 },
        structure: ivV2State.structureResponse.structure,
      }),
    };
    if (url.endsWith('/review-issues')) return {
      ok: true,
      status: 200,
      json: async () => ({
        status: 'ANALYSIS_BOUNDARY_REQUIRED',
        structure_revision_id: structure2,
        evidence_revision_id: evidence2,
        issues: [],
      }),
    };
    throw new Error('dirty structure refresh must not fetch analysis boundary');
  };
  assert.strictEqual(await ivV2LoadStructureWorkspace({ token: ivV2State.requestToken }), true);
  assert.strictEqual(fetchCount, 2);
  assert.strictEqual(ivV2State.boundaryDraft, dirtyDraft);
  assert.strictEqual(ivV2State.boundaryConflict.code, 'ANALYSIS_BOUNDARY_INPUT_STALE');
  assert.match(ivV2BoundaryToolbarHtml(), /data-iv-v2-action="boundary-save"[\\s\\S]*?data-iv-v2-locked="true"/);
  assert.match(ivV2BoundaryToolbarHtml(), /data-iv-v2-action="boundary-confirm"[\\s\\S]*?data-iv-v2-locked="true"/);
  const conflictFetchCount = fetchCount;
  assert.strictEqual(await ivV2SaveAnalysisBoundary(), false);
  assert.strictEqual(fetchCount, conflictFetchCount, 'conflicted save must not issue a request');
  ivV2State.boundaryDirty = false;
  assert.strictEqual(await ivV2ConfirmAnalysisBoundary(), false);
  assert.strictEqual(fetchCount, conflictFetchCount, 'conflicted confirm must not issue a request');

  const guardedDraft = { marker: 'guard-me', evaluation_objects: [], source_scope_rules: [], label_scope_rules: [] };
  const originalResetStructureWorkspace = ivV2ResetStructureWorkspace;
  let resetStructureCount = 0;
  ivV2ResetStructureWorkspace = (...args) => {
    resetStructureCount += 1;
    return originalResetStructureWorkspace(...args);
  };
  const originalReset = ivV2Reset;
  let startOverResetCount = 0;
  ivV2Reset = () => { startOverResetCount += 1; };
  let guardedFetchCount = 0;
  fetch = async () => {
    guardedFetchCount += 1;
    throw new Error('cancelled destructive action must not fetch');
  };
  ivV2State.importId = 'import-vm';
  ivV2State.mappingResponse = {
    revision_number: 1,
    mapping_sha256: 'e'.repeat(64),
    confirmation_ready: true,
    status: 'GROUP_CONFIRMATION_REQUIRED',
  };
  ivV2State.boundaryResponse = {
    structure_revision_id: structure2,
    evidence_revision_id: evidence2,
    boundary_revision_id: boundaryN1,
    coverage_revision_id: coverageN1,
    boundary_payload_sha256: 'c'.repeat(64),
    coverage_payload_sha256: 'd'.repeat(64),
  };
  ivV2State.boundaryDraft = guardedDraft;
  ivV2State.boundaryDirty = true;
  ivV2State.boundaryConflict = null;
  ivV2State.draftDirty = false;
  window.confirmResult = false;
  const promptCountBefore = window.confirmCallCount;
  assert.strictEqual(await ivV2RefreshImportBundleFromEditor(), false);
  assert.strictEqual(ivV2StartOver(), false);
  await ivV2ConfirmMapping();
  assert.strictEqual(window.confirmCallCount, promptCountBefore + 3);
  assert.strictEqual(guardedFetchCount, 0);
  assert.strictEqual(resetStructureCount, 0);
  assert.strictEqual(startOverResetCount, 0);
  assert.strictEqual(ivV2State.boundaryDraft, guardedDraft);
  assert.strictEqual(ivV2State.boundaryDirty, true);

  window.confirmResult = true;
  fetch = async url => {
    guardedFetchCount += 1;
    if (url.endsWith('/group-proposals')) return {
      ok: true,
      status: 200,
      json: async () => ({
        status: 'GROUP_CONFIRMATION_REQUIRED',
        revision_number: 1,
        mapping_sha256: 'f'.repeat(64),
        confirmation_ready: true,
        mapping: { groups: [], ignored_sheet_ids: [] },
      }),
    };
    if (url.endsWith('/import-vm')) return {
      ok: true,
      status: 200,
      json: async () => ({
        import_id: 'import-vm',
        project_id: 'project-vm',
        workbook_revision_id: 'workbook-vm',
        status: 'GROUP_CONFIRMATION_REQUIRED',
      }),
    };
    throw new Error('unexpected confirmed refresh URL: ' + url);
  };
  assert.strictEqual(await ivV2RefreshImportBundleFromEditor(), true);
  assert.strictEqual(guardedFetchCount, 2);
  assert.strictEqual(resetStructureCount, 1);
  assert.strictEqual(ivV2State.boundaryDraft, null);

  ivV2State.boundaryDraft = guardedDraft;
  ivV2State.boundaryDirty = true;
  fetch = async url => {
    guardedFetchCount += 1;
    assert(url.endsWith('/group-mapping:confirm'));
    return {
      ok: false,
      status: 500,
      json: async () => ({ error: { code: 'TEST_STOP', message: 'stop after guarded request' } }),
    };
  };
  await ivV2ConfirmMapping();
  assert.strictEqual(guardedFetchCount, 3);
  assert.strictEqual(resetStructureCount, 2);

  const originalDollar = ivV2$;
  ivV2$ = elementId => {
    if (elementId === 'iv-v2-contract-check') return { checked: true };
    if (elementId === 'iv-v2-research-focus') return { value: '' };
    return null;
  };
  const uploadDraft = { marker: 'upload-guard', evaluation_objects: [], source_scope_rules: [], label_scope_rules: [] };
  ivV2State.selectedFile = { name: 'new-interview.xlsx', size: 128, lastModified: 1234 };
  ivV2State.importId = 'import-old';
  ivV2State.projectId = 'project-old';
  ivV2State.workbookRevisionId = 'workbook-old';
  ivV2State.mappingResponse = {
    revision_number: 1,
    mapping_sha256: '1'.repeat(64),
    confirmation_ready: true,
    status: 'ANALYSIS_BOUNDARY_REVIEW_REQUIRED',
  };
  ivV2State.boundaryDraft = uploadDraft;
  ivV2State.boundaryDirty = true;
  ivV2State.status = 'ANALYSIS_BOUNDARY_REVIEW_REQUIRED';
  const uploadResetBaseline = resetStructureCount;
  const uploadPromptBaseline = window.confirmCallCount;
  let uploadFetchCount = 0;
  fetch = async () => {
    uploadFetchCount += 1;
    throw new Error('cancelled reupload must not fetch');
  };
  window.confirmResult = false;
  await ivV2StartImport();
  assert.strictEqual(window.confirmCallCount, uploadPromptBaseline + 1);
  assert.strictEqual(uploadFetchCount, 0);
  assert.strictEqual(resetStructureCount, uploadResetBaseline);
  assert.strictEqual(ivV2State.boundaryDraft, uploadDraft);
  assert.strictEqual(ivV2State.importId, 'import-old');

  window.confirmResult = true;
  let workspaceClearedBeforeUpload = false;
  fetch = async (url, options = {}) => {
    uploadFetchCount += 1;
    if (url === '/api/v1/interview-upload-attempts') {
      assert.strictEqual(options.method, 'POST');
      workspaceClearedBeforeUpload = ivV2State.boundaryDraft === null
        && ivV2State.structureResponse === null
        && ivV2State.reviewIssuesResponse === null;
      return {
        ok: true,
        status: 200,
        json: async () => ({
          upload_attempt_id: 'attempt-new',
          import_id: 'import-new',
          project_id: 'project-new',
          workbook_revision_id: 'workbook-new',
          status: 'ACCEPTED',
        }),
      };
    }
    if (url.endsWith('/import-new/group-proposals')) return {
      ok: true,
      status: 200,
      json: async () => ({
        status: 'GROUP_CONFIRMATION_REQUIRED',
        revision_number: 1,
        mapping_sha256: '2'.repeat(64),
        confirmation_ready: true,
        mapping: { groups: [], ignored_sheet_ids: [] },
      }),
    };
    if (url.endsWith('/import-new')) return {
      ok: true,
      status: 200,
      json: async () => ({
        import_id: 'import-new',
        project_id: 'project-new',
        workbook_revision_id: 'workbook-new',
        status: 'GROUP_CONFIRMATION_REQUIRED',
      }),
    };
    throw new Error('unexpected reupload URL: ' + url);
  };
  await ivV2StartImport();
  assert.strictEqual(window.confirmCallCount, uploadPromptBaseline + 2);
  assert.strictEqual(uploadFetchCount, 3);
  assert.strictEqual(resetStructureCount, uploadResetBaseline + 1);
  assert.strictEqual(workspaceClearedBeforeUpload, true);
  assert.strictEqual(ivV2State.boundaryDraft, null);
  assert.strictEqual(ivV2State.importId, 'import-new');
  assert.strictEqual(ivV2State.projectId, 'project-new');
  assert.strictEqual(ivV2State.workbookRevisionId, 'workbook-new');
  assert.strictEqual(ivV2State.mappingResponse.mapping_sha256, '2'.repeat(64));

  const polledDraft = { marker: 'polled-upload-guard', evaluation_objects: [], source_scope_rules: [], label_scope_rules: [] };
  ivV2State.selectedFile = { name: 'polled-interview.xlsx', size: 256, lastModified: 5678 };
  ivV2State.importId = 'import-before-poll';
  ivV2State.projectId = 'project-before-poll';
  ivV2State.workbookRevisionId = 'workbook-before-poll';
  ivV2State.mappingResponse = {
    revision_number: 1,
    mapping_sha256: '3'.repeat(64),
    confirmation_ready: true,
    status: 'ANALYSIS_BOUNDARY_REVIEW_REQUIRED',
  };
  ivV2State.boundaryDraft = polledDraft;
  ivV2State.boundaryDirty = true;
  ivV2State.status = 'ANALYSIS_BOUNDARY_REVIEW_REQUIRED';
  const polledResetBaseline = resetStructureCount;
  const polledPromptBaseline = window.confirmCallCount;
  let polledFetchCount = 0;
  let polledWorkspaceClearedBeforeUpload = false;
  fetch = async (url, options = {}) => {
    polledFetchCount += 1;
    if (url === '/api/v1/interview-upload-attempts') {
      assert.strictEqual(options.method, 'POST');
      polledWorkspaceClearedBeforeUpload = ivV2State.boundaryDraft === null
        && ivV2State.structureResponse === null
        && ivV2State.reviewIssuesResponse === null;
      return {
        ok: true,
        status: 200,
        json: async () => ({ upload_attempt_id: 'attempt-polled', status: 'PRECHECKING' }),
      };
    }
    if (url.endsWith('/interview-upload-attempts/attempt-polled')) return {
      ok: true,
      status: 200,
      json: async () => ({
        upload_attempt_id: 'attempt-polled',
        import_id: 'import-polled',
        project_id: 'project-polled',
        workbook_revision_id: 'workbook-polled',
        status: 'ACCEPTED',
      }),
    };
    if (url.endsWith('/import-polled/group-proposals')) return {
      ok: true,
      status: 200,
      json: async () => ({
        status: 'GROUP_CONFIRMATION_REQUIRED',
        revision_number: 1,
        mapping_sha256: '4'.repeat(64),
        confirmation_ready: true,
        mapping: { groups: [], ignored_sheet_ids: [] },
      }),
    };
    if (url.endsWith('/import-polled')) return {
      ok: true,
      status: 200,
      json: async () => ({
        import_id: 'import-polled',
        project_id: 'project-polled',
        workbook_revision_id: 'workbook-polled',
        status: 'GROUP_CONFIRMATION_REQUIRED',
      }),
    };
    throw new Error('unexpected polled reupload URL: ' + url);
  };
  await ivV2StartImport();
  assert.strictEqual(window.confirmCallCount, polledPromptBaseline + 1);
  assert.strictEqual(polledFetchCount, 1);
  assert.strictEqual(resetStructureCount, polledResetBaseline + 1);
  assert.strictEqual(polledWorkspaceClearedBeforeUpload, true);
  assert.strictEqual(ivV2State.status, 'PRECHECKING');
  await ivV2PollAttempt(ivV2State.requestToken);
  assert.strictEqual(window.confirmCallCount, polledPromptBaseline + 1);
  assert.strictEqual(polledFetchCount, 4);
  assert.strictEqual(resetStructureCount, polledResetBaseline + 1);
  assert.strictEqual(ivV2State.boundaryDraft, null);
  assert.strictEqual(ivV2State.importId, 'import-polled');
  assert.strictEqual(ivV2State.projectId, 'project-polled');
  assert.strictEqual(ivV2State.workbookRevisionId, 'workbook-polled');
  assert.strictEqual(ivV2State.mappingResponse.mapping_sha256, '4'.repeat(64));
  ivV2$ = originalDollar;

  ivV2State.boundaryDraft = guardedDraft;
  ivV2State.boundaryDirty = true;
  assert.strictEqual(ivV2StartOver(), true);
  assert.strictEqual(startOverResetCount, 1);
  ivV2Reset = originalReset;
  ivV2ResetStructureWorkspace = originalResetStructureWorkspace;
})()`, context).then(
  () => process.stdout.write(JSON.stringify({ ok: true, toast_count: toasts.length })),
  error => { console.error(error.stack || error); process.exitCode = 1; },
);
"""
        result = subprocess.run(
            ["node", "-", str(JS_PATH)],
            input=scenario,
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=ROOT,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"Node VM scenario failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn('"ok":true', result.stdout)


if __name__ == "__main__":
    unittest.main()
