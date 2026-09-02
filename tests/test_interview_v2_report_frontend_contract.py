import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "js" / "features" / "interview-v2.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


class InterviewV2ReportFrontendContractTests(unittest.TestCase):
    def _between(self, start: str, end: str) -> str:
        start_index = JS.index(start)
        end_index = JS.index(end, start_index)
        return JS[start_index:end_index]

    def test_step_five_and_report_workbench_mounts_exist(self):
        self.assertIn('data-iv-v2-step="5"', HTML)
        self.assertIn('分析与报告审核', HTML)
        for marker in (
            'id="iv-v2-report-workbench"',
            'id="iv-v2-report-status"',
            'id="iv-v2-report-section-list"',
            'id="iv-v2-report-body"',
            'id="iv-v2-report-approval"',
            'id="iv-v2-report-drawer"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, HTML)
        self.assertIn('/static/style.css?v=45', HTML)
        self.assertNotIn('<main class="iv-v2-report-main"', HTML)

    def test_frontend_uses_report_and_analysis_routes_with_exact_cas_fields(self):
        for snippet in (
            '/api/v1/interview-projects/${ivV2State.projectId}/analysis-runs/current',
            '/api/v1/interview-projects/${ivV2State.projectId}/analysis-runs',
            '/api/v1/interview-projects/${ivV2State.projectId}/reports',
            '/api/v1/interview-reports/${reportVersionId}',
            '/api/v1/interview-reports/${ivV2CurrentReportVersionId()}/claims/${claimId}',
            '/api/v1/interview-report-sections/${sectionId}`',
            '/api/v1/interview-report-sections/${sectionId}:reaudit',
            '/api/v1/interview-reports/${reportVersionId}:approve',
            'base_analysis_run_id: ivV2AnalysisSummary().analysis_run_id || null',
            'base_report_version_id: ivV2ReportSummary().report_version_id || null',
            'base_section_revision: Number(draft?.base_section_revision || section?.section_revision || 1)',
            "locked: true",
            "reaudit_job_id: String(section?.reaudit_job_id || '')",
            "base_report_version_id: ivV2State.reportResponse?.report_version_id || ''",
            "decision: 'approved'",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, JS)

    def test_report_status_gates_and_unsaved_protection_are_explicit(self):
        for snippet in (
            'report_summary.approval_ready',
            'ivV2DossierSummary().analysis_ready === true',
            'ivV2AnalysisSummary().report_ready === true',
            "report?.is_current_version && ivV2ReportEditableStatus(report?.status)",
            "ivV2CurrentDraftReport()",
            "section.audit_status === 'pending_reaudit'",
            "report?.status === 'stale'",
            "report && !report.is_current_version",
            "window.addEventListener('beforeunload'",
            'ivV2State.reportDirty',
            '章节仍待重审，请查看阻塞提醒后重试',
            'section_id 会指向当前报告，旧版本只允许查看，不允许编辑或重审',
            '保存后会基于当前已批准版本创建新的草稿版本',
            'reportBusy && !force && !internalRefresh',
            'ivV2ConfirmLeaveReportWorkspace()',
            "String(data.status || '') !== 'draft'",
            'aria-pressed=',
            'readonly aria-readonly="true"',
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, JS)

    def test_report_evidence_is_source_locator_not_client_side_inference(self):
        for snippet in (
            '/api/v1/interview-evidence/${evidenceId}/context',
            'Sheet</dt><dd>',
            'Cell</dt><dd>',
            '记录员</dt><dd>',
            '记录</dt><dd>',
            '备注</dt><dd>',
            '当前证据版本未公开该来源',
            'payload.source_context || payload.context || {}',
            '所有人数、可批准状态和待处理数量都以服务端返回为准',
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, JS)

    def test_report_css_is_scoped_and_responsive(self):
        for snippet in (
            '.iv-v2-report-workbench',
            '.iv-v2-report-layout',
            'grid-template-columns: minmax(210px, 0.8fr) minmax(0, 1.45fr) minmax(260px, 0.95fr);',
            '.iv-v2-report-editor:focus',
            '.iv-v2-report-section--active',
            'grid-column: 1 / -1;',
            '@media (max-width: 760px)',
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, CSS)

    def test_payload_helpers_and_escaping_contracts_are_present(self):
        patch_payload = self._between(
            "function ivV2ReportSectionPatchPayload(section) {",
            "function ivV2ReportSectionReauditPayload(section) {",
        )
        reaudit_payload = self._between(
            "function ivV2ReportSectionReauditPayload(section) {",
            "function ivV2ReportApprovePayload() {",
        )
        approve_payload = self._between(
            "function ivV2ReportApprovePayload() {",
            "async function ivV2SaveReportSection(sectionId) {",
        )
        body_html = self._between(
            "function ivV2ReportBodyHtml() {",
            "function ivV2ReportApprovalHtml() {",
        )
        drawer_html = self._between(
            "function ivV2ReportDrawerHtml() {",
            "function ivV2ReportEvidenceCardHtml(evidenceId, payload) {",
        )
        self.assertIn("base_section_revision: Number(draft?.base_section_revision || section?.section_revision || 1)", patch_payload)
        self.assertIn("locked: true", patch_payload)
        self.assertIn("reaudit_job_id: String(section?.reaudit_job_id || '')", reaudit_payload)
        self.assertIn("base_report_version_id: ivV2State.reportResponse?.report_version_id || ''", approve_payload)
        self.assertIn("decision: 'approved'", approve_payload)
        self.assertIn("ivV2Esc(draft?.content || '')", body_html)
        self.assertIn("ivV2Esc(draft?.edit_reason || '')", body_html)
        self.assertIn("ivV2Esc(claim.text || '')", drawer_html)
        self.assertIn("当前证据版本未公开该来源", drawer_html)
        self.assertIn("ivV2State.reportDirty = Object.values(ivV2State.reportSectionDrafts || {}).some(", JS)

    def test_conflict_refresh_preserves_server_error_feedback(self):
        function_pairs = (
            ("async function ivV2CreateAnalysisRun() {", "async function ivV2CreateReportVersion() {"),
            ("async function ivV2CreateReportVersion() {", "function ivV2ResetReportSectionDraft(sectionId) {"),
            ("async function ivV2ReauditReportSection(sectionId) {", "async function ivV2ApproveReport() {"),
            ("async function ivV2ApproveReport() {", "function ivV2ReportClaimCacheKey(claimId) {"),
        )
        for start, end in function_pairs:
            with self.subTest(function=start):
                block = self._between(start, end)
                conflict = block.index("if (response.status === 409)")
                refresh = block.index("await ivV2LoadReportWorkspace", conflict)
                feedback = block.index("ivV2SetStatusError", refresh)
                self.assertLess(refresh, feedback)

    def test_vm_executes_report_runtime_contracts(self):
        script = textwrap.dedent(
            """
            const fs = require('node:fs');
            const os = require('node:os');
            const path = require('node:path');
            const vm = require('node:vm');

            const source = fs.readFileSync('static/js/features/interview-v2.js', 'utf8');
            const log = [];
            let confirmCalls = 0;
            const elements = new Map();
            function makeNode(id = '') {
              return {
                id,
                hidden: false,
                value: '',
                checked: false,
                disabled: false,
                innerHTML: '',
                textContent: '',
                dataset: {},
                files: [],
                classList: { add() {}, remove() {}, toggle() {} },
                addEventListener() {},
                querySelector() { return makeNode(); },
                querySelectorAll() { return []; },
                closest() { return null; },
                setAttribute() {},
                scrollTo() {},
              };
            }
            const document = {
              getElementById(id) {
                if (!elements.has(id)) elements.set(id, makeNode(id));
                return elements.get(id);
              },
              querySelector() { return makeNode(); },
              querySelectorAll() { return []; },
            };
            const fetchQueue = [];
            async function fetchStub(url, options = {}) {
              const method = String(options.method || 'GET').toUpperCase();
              log.push({ type: 'fetch', url, method, body: options.body || null });
              const next = fetchQueue.shift();
              if (!next) throw new Error('unexpected fetch:' + url);
              if (typeof next === 'function') return next(url, options);
              if (String(next.expectedUrl || '') !== String(url)) {
                throw new Error(`expected fetch ${next.expectedUrl}, received ${url}`);
              }
              if (String(next.expectedMethod || 'GET').toUpperCase() !== method) {
                throw new Error(`expected method ${next.expectedMethod || 'GET'}, received ${method} for ${url}`);
              }
              return {
                ok: next.ok !== false,
                status: next.status || 200,
                json: async () => next.json,
              };
            }
            const context = {
              console,
              document,
              currentMode: 'interview',
              fetch: fetchStub,
              showToast(message, tone) { log.push({ type: 'toast', message, tone }); },
              esc(value) {
                return String(value ?? '')
                  .replace(/&/g, '&amp;')
                  .replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;')
                  .replace(/"/g, '&quot;')
                  .replace(/'/g, '&#39;');
              },
              FormData: class { append() {} },
              URLSearchParams,
              window: {
                ivState: { track: 'v2', currentStep: 1 },
                addEventListener() {},
                setTimeout() { return 1; },
                clearTimeout() {},
                confirm() { confirmCalls += 1; return true; },
                crypto: { randomUUID() { return 'uuid-fixed'; } },
              },
              setTimeout() { return 1; },
              clearTimeout() {},
            };
            context.global = context;
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(source, context, { filename: 'interview-v2.js' });
            const api = vm.runInContext(`({
              ivV2State,
              ivV2CreateAnalysisRun,
              ivV2SaveReportSection,
              ivV2LoadCurrentReport,
              ivV2CreateReportVersion,
              ivV2ReauditReportSection,
              ivV2ApproveReport,
              ivV2LoadReportClaim,
              ivV2LoadReportEvidence,
              ivV2ReportBodyHtml,
              ivV2ReportClaimCacheKey,
              ivV2ReportEvidenceIdsFromClaim,
              ivV2ReportDrawerHtml,
              ivV2NextReportToken,
              ivV2PreviewStep,
            })`, context);

            function setBaseState() {
              api.ivV2State.projectId = 'project_' + '1'.repeat(32);
              api.ivV2State.importId = 'import_' + '2'.repeat(32);
              api.ivV2State.requestToken = 9;
              api.ivV2State.status = 'READY_FOR_DOSSIERS';
              api.ivV2State.currentStep = 5;
              api.ivV2State.importData = {
                project_id: api.ivV2State.projectId,
                import_id: api.ivV2State.importId,
                workbook_revision_id: 'workbook_' + '3'.repeat(32),
                status: 'READY_FOR_DOSSIERS',
                dossier_summary: { analysis_ready: true, blocking_participant_ids: [] },
                analysis_summary: { analysis_run_id: 'analysis_' + '4'.repeat(32), report_ready: true, status: 'completed', finding_count: 2 },
                report_summary: { report_version_id: 'report_' + '5'.repeat(32), approval_ready: true, status: 'draft', audit_status: 'audited', pending_reaudit_count: 0, blocking_issue_count: 0 },
              };
              api.ivV2State.mappingResponse = { status: 'READY_FOR_DOSSIERS' };
              api.ivV2State.reportResponse = {
                report_version_id: 'report_' + '5'.repeat(32),
                status: 'draft',
                audit_status: 'pending_reaudit',
                is_current_version: true,
                sections: [{
                  section_id: 'section_' + '6'.repeat(32),
                  section_key: 'core_findings',
                  title: 'title',
                  content: '<b>body</b>',
                  section_revision: 2,
                  audit_status: 'pending_reaudit',
                  reaudit_job_id: 'job_' + '7'.repeat(32),
                  claim_ids: ['claim_' + '8'.repeat(32)],
                }],
                claims: [{
                  claim_id: 'claim_' + '8'.repeat(32),
                  claim_type: 'finding',
                  text: '<img src=x onerror=1>',
                  audit_status: 'audit_passed',
                  evidence_roles: ['support'],
                  participant_ids: ['participant_' + '9'.repeat(32)],
                  evidence_ids: ['ev_' + 'a'.repeat(32)],
                }],
                audit_issues: [],
                approved_by: null,
                approved_at: null,
              };
              api.ivV2State.selectedReportSectionId = 'section_' + '6'.repeat(32);
              api.ivV2State.selectedReportClaimId = 'claim_' + '8'.repeat(32);
              api.ivV2State.reportSectionDrafts = {};
              api.ivV2State.reportApprovalNote = '';
              api.ivV2State.reportDirty = false;
              api.ivV2State.reportBusy = false;
              api.ivV2State.reportToken = 0;
              api.ivV2State.reportClaimCache = {};
              api.ivV2State.reportEvidenceCache = {};
              api.ivV2State.requestBusy = false;
              api.ivV2State.dossierBusy = false;
              api.ivV2State.structureResponse = null;
              api.ivV2State.reviewIssuesResponse = null;
              api.ivV2State.boundaryResponse = null;
              api.ivV2State.boundaryDraft = null;
              api.ivV2State.coverageResponse = null;
              api.ivV2State.boundaryDirty = false;
              api.ivV2State.boundaryConflict = null;
            }

            function resetScenario() {
              fetchQueue.length = 0;
              log.length = 0;
            }

            function enqueue(expectedUrl, json, { method = 'GET', status = 200, ok = status < 400 } = {}) {
              fetchQueue.push({ expectedUrl, expectedMethod: method, json, status, ok });
            }

            function assertQueueDrained(label) {
              if (fetchQueue.length) throw new Error(`${label}: ${fetchQueue.length} queued responses were not consumed`);
            }

            function structureResponse() {
              return {
                status: 'READY_FOR_DOSSIERS',
                structure_revision_id: 'structure_' + 's'.repeat(32),
                evidence_revision_id: 'evidence_' + 'v'.repeat(32),
                structure: { modules: [], main_questions: [], occurrences: [] },
                review_summary: { blocking_issue_count: 0 },
              };
            }

            function reviewIssuesResponse() {
              return {
                status: 'READY_FOR_DOSSIERS',
                structure_revision_id: 'structure_' + 's'.repeat(32),
                evidence_revision_id: 'evidence_' + 'v'.repeat(32),
                issues: [],
              };
            }

            function analysisBoundaryResponse() {
              return {
                status: 'READY_FOR_DOSSIERS',
                structure_revision_id: 'structure_' + 's'.repeat(32),
                evidence_revision_id: 'evidence_' + 'v'.repeat(32),
                boundary_revision_id: 'boundary_' + 'w'.repeat(32),
                coverage_revision_id: 'coverage_' + 'x'.repeat(32),
                boundary_payload_sha256: '1'.repeat(64),
                coverage_payload_sha256: '2'.repeat(64),
                analysis_boundary: { evaluation_objects: [], source_scope_rules: [], label_scope_rules: [] },
                coverage_preview: { source: {
                  structure_revision_id: 'structure_' + 's'.repeat(32),
                  evidence_revision_id: 'evidence_' + 'v'.repeat(32),
                  boundary_revision_id: 'boundary_' + 'w'.repeat(32),
                  coverage_revision_id: 'coverage_' + 'x'.repeat(32),
                } },
              };
            }

            function enqueueImportRefresh(importData) {
              const importId = api.ivV2State.importId;
              enqueue(`/api/v1/interview-imports/${importId}`, importData);
              enqueue(`/api/v1/interview-imports/${importId}/group-proposals`, { status: 'READY_FOR_DOSSIERS' });
              enqueue(`/api/v1/interview-imports/${importId}/structure`, structureResponse());
              enqueue(`/api/v1/interview-imports/${importId}/review-issues`, reviewIssuesResponse());
              enqueue(`/api/v1/interview-imports/${importId}/analysis-boundary`, analysisBoundaryResponse());
            }

            function reportSection({ id = 'section_' + '6'.repeat(32), revision = 2, content = '<b>body</b>', auditStatus = 'pending_reaudit' } = {}) {
              return {
                section_id: id,
                section_key: 'core_findings',
                title: 'title',
                content,
                section_revision: revision,
                audit_status: auditStatus,
                reaudit_job_id: auditStatus === 'pending_reaudit' ? 'job_' + '7'.repeat(32) : null,
                claim_ids: ['claim_' + '8'.repeat(32)],
              };
            }

            function controlIsDisabled(html, action) {
              const match = html.match(new RegExp(`<[^>]+data-iv-v2-action="${action}"[^>]*>`));
              return Boolean(match && match[0].includes('disabled'));
            }

            function controlIsReadOnly(html, action) {
              const match = html.match(new RegExp(`<[^>]+data-iv-v2-action="${action}"[^>]*>`));
              return Boolean(match && match[0].includes('readonly'));
            }

            async function run() {
              resetScenario();
              setBaseState();
              const projectId = api.ivV2State.projectId;
              const importId = api.ivV2State.importId;
              const baseReportId = api.ivV2State.reportResponse.report_version_id;
              const analysisB = { analysis_run_id: 'analysis_' + 'b'.repeat(32), status: 'completed', findings: [], stat_facts: [], limitations: [] };
              const analysisImport = {
                ...api.ivV2State.importData,
                analysis_summary: { analysis_run_id: analysisB.analysis_run_id, report_ready: true, status: 'completed', finding_count: 0 },
              };
              enqueue(`/api/v1/interview-projects/${projectId}/analysis-runs`, analysisB, { method: 'POST' });
              enqueueImportRefresh(analysisImport);
              enqueue(`/api/v1/interview-projects/${projectId}/analysis-runs/current`, analysisB);
              enqueue(`/api/v1/interview-reports/${baseReportId}`, api.ivV2State.reportResponse);
              await api.ivV2CreateAnalysisRun();
              assertQueueDrained('create analysis');

              const analysisFetch = log.find(item => item.url && item.url.includes('/analysis-runs') && item.method === 'POST');
              const nestedRefreshRan = log.some(item => item.url === `/api/v1/interview-projects/${projectId}/analysis-runs/current`)
                && log.some(item => item.url === `/api/v1/interview-reports/${baseReportId}`);

              resetScenario();
              setBaseState();
              api.ivV2State.reportResponse.status = 'approved';
              api.ivV2State.reportSectionDrafts['section_' + '6'.repeat(32)] = {
                section_id: 'section_' + '6'.repeat(32),
                base_section_revision: 2,
                content: 'edited approved body',
                originalContent: '<b>body</b>',
                edit_reason: 'approved-edit',
                dirty: true,
                conflict: false,
              };
              api.ivV2State.reportDirty = true;
              const reportCId = 'report_' + 'c'.repeat(32);
              const reportC = {
                ...api.ivV2State.reportResponse,
                report_version_id: reportCId,
                status: 'draft',
                sections: [{ ...reportSection({ revision: 3, content: 'edited approved body' }), reaudit_job_id: 'job_' + 'd'.repeat(32) }],
              };
              const importC = {
                ...api.ivV2State.importData,
                report_summary: { report_version_id: reportCId, approval_ready: false, status: 'draft', audit_status: 'pending_reaudit', pending_reaudit_count: 1, blocking_issue_count: 0 },
              };
              enqueue(`/api/v1/interview-report-sections/${api.ivV2State.reportResponse.sections[0].section_id}`, {
                report_version_id: reportCId,
                section_id: api.ivV2State.reportResponse.sections[0].section_id,
                section_revision: 3,
                audit_status: 'pending_reaudit',
                reaudit_job_id: 'job_' + 'd'.repeat(32),
              }, { method: 'PATCH' });
              enqueue(`/api/v1/interview-reports/${reportCId}`, reportC);
              enqueueImportRefresh(importC);
              await api.ivV2SaveReportSection('section_' + '6'.repeat(32));
              assertQueueDrained('edit approved report');
              const approvedPatch = log.find(item => item.method === 'PATCH');
              const approvedPatchBody = JSON.parse(approvedPatch.body);
              const approvedSaveCreatedDraft = api.ivV2State.reportResponse.status === 'draft'
                && api.ivV2State.reportResponse.report_version_id === reportCId
                && api.ivV2State.reportDirty === false;
              const approvedConfirmCalls = confirmCalls;

              resetScenario();
              setBaseState();
              api.ivV2State.reportResponse.claims = [{ ...api.ivV2State.reportResponse.claims[0], claim_id: 'claim_' + 'e'.repeat(32) }];
              api.ivV2State.reportResponse.sections = [{ ...api.ivV2State.reportResponse.sections[0], claim_ids: ['claim_' + 'e'.repeat(32)] }];
              api.ivV2State.reportSectionDrafts = {
                ['section_' + '6'.repeat(32)]: {
                  section_id: 'section_' + '6'.repeat(32),
                  section_key: 'core_findings',
                  base_section_revision: 2,
                  content: 'draft local',
                  originalContent: '<b>body</b>',
                  edit_reason: 'reason',
                  dirty: true,
                  conflict: false,
                },
              };
              api.ivV2State.reportDirty = true;
              const reportFId = 'report_' + 'f'.repeat(32);
              api.ivV2State.reportApprovalNote = 'note that belongs to the old report';
              enqueue(`/api/v1/interview-reports/${reportFId}`, { report_version_id: reportFId, status: 'draft', audit_status: 'pending_reaudit', is_current_version: true, sections: [{ section_id: 'section_' + 'f'.repeat(32), section_key: 'core_findings', title: 'new', content: 'server body', section_revision: 4, audit_status: 'pending_reaudit', reaudit_job_id: 'job_' + '7'.repeat(32), claim_ids: [] }], claims: [], audit_issues: [] });
              await api.ivV2LoadCurrentReport('report_' + 'f'.repeat(32), { token: api.ivV2State.reportToken || 0 });
              assertQueueDrained('migrate dirty draft');
              const conflictDraft = api.ivV2State.reportSectionDrafts['section_' + 'f'.repeat(32)];
              const conflictSelectionMigrated = api.ivV2State.selectedReportSectionId === 'section_' + 'f'.repeat(32);
              const reportVersionClearedApprovalNote = api.ivV2State.reportApprovalNote === '';

              resetScenario();
              setBaseState();
              api.ivV2State.reportSectionDrafts = {
                ['section_' + '6'.repeat(32)]: {
                  section_id: 'section_' + '6'.repeat(32),
                  base_section_revision: 2,
                  content: 'local pending',
                  originalContent: '<b>body</b>',
                  edit_reason: '',
                  dirty: true,
                  conflict: false,
                },
              };
              api.ivV2State.reportDirty = true;
              const logLenBeforeDirty = log.length;
              await api.ivV2CreateReportVersion();
              await api.ivV2CreateAnalysisRun();
              await api.ivV2ReauditReportSection('section_' + '6'.repeat(32));
              const dirtyBlocked = log.length === logLenBeforeDirty;
              assertQueueDrained('dirty mutation gates');

              resetScenario();
              setBaseState();
              api.ivV2State.reportApprovalNote = 'unsaved approval note';
              const logLenBeforeNote = log.length;
              await api.ivV2CreateReportVersion();
              await api.ivV2CreateAnalysisRun();
              const approvalNoteBlocked = log.length === logLenBeforeNote;
              assertQueueDrained('approval note mutation gates');

              resetScenario();
              setBaseState();
              api.ivV2State.participantResponse = { participants: [] };
              api.ivV2State.reportApprovalNote = 'discard before leaving';
              const confirmCallsBeforeLeave = confirmCalls;
              api.ivV2PreviewStep(4);
              const leaveReportClearedUnsaved = api.ivV2State.currentStep === 4
                && api.ivV2State.reportApprovalNote === ''
                && api.ivV2State.reportDirty === false
                && Object.keys(api.ivV2State.reportSectionDrafts).length === 0;
              const leaveReportConfirmCalls = confirmCalls - confirmCallsBeforeLeave;
              assertQueueDrained('leave report workbench');

              resetScenario();
              setBaseState();
              api.ivV2State.reportResponse.is_current_version = false;
              const oldHtml = api.ivV2ReportBodyHtml();
              const oldReadOnly = controlIsReadOnly(oldHtml, 'report-edit-content')
                && controlIsDisabled(oldHtml, 'report-save-section')
                && controlIsDisabled(oldHtml, 'report-reaudit-section');

              resetScenario();
              setBaseState();
              api.ivV2State.reportResponse.status = 'stale';
              const staleHtml = api.ivV2ReportBodyHtml();
              const staleReadOnly = controlIsReadOnly(staleHtml, 'report-edit-content')
                && controlIsDisabled(staleHtml, 'report-save-section')
                && controlIsDisabled(staleHtml, 'report-reaudit-section');

              resetScenario();
              setBaseState();
              api.ivV2State.reportApprovalNote = 'must clear when same report becomes stale';
              const sameReportId = api.ivV2State.reportResponse.report_version_id;
              enqueue(`/api/v1/interview-reports/${sameReportId}`, {
                ...api.ivV2State.reportResponse,
                status: 'stale',
              });
              await api.ivV2LoadCurrentReport(sameReportId, { token: api.ivV2State.reportToken });
              assertQueueDrained('same report becomes stale');
              const staleSameVersionClearedApprovalNote = api.ivV2State.reportApprovalNote === '';

              resetScenario();
              setBaseState();
              const reportKId = 'report_' + 'k'.repeat(32);
              const reportK = {
                ...api.ivV2State.reportResponse,
                report_version_id: reportKId,
                sections: [reportSection({ id: 'section_' + 'k'.repeat(32) })],
              };
              const importK = {
                ...api.ivV2State.importData,
                report_summary: { report_version_id: reportKId, approval_ready: false, status: 'draft', audit_status: 'pending_reaudit', pending_reaudit_count: 1, blocking_issue_count: 0 },
              };
              enqueue(`/api/v1/interview-projects/${api.ivV2State.projectId}/reports`, reportK, { method: 'POST' });
              enqueueImportRefresh(importK);
              enqueue(`/api/v1/interview-projects/${api.ivV2State.projectId}/analysis-runs/current`, {
                analysis_run_id: api.ivV2State.importData.analysis_summary.analysis_run_id,
                status: 'completed',
                findings: [],
                stat_facts: [],
                limitations: [],
              });
              enqueue(`/api/v1/interview-reports/${reportKId}`, reportK);
              await api.ivV2CreateReportVersion();
              assertQueueDrained('create report');
              const createReportFetch = log.find(item => item.method === 'POST' && item.url.endsWith('/reports'));
              const createReportBody = JSON.parse(createReportFetch.body);
              const createReportSucceeded = api.ivV2State.reportResponse.report_version_id === reportKId
                && log.some(item => item.type === 'toast' && item.message === '报告已生成');

              resetScenario();
              setBaseState();
              api.ivV2State.reportSectionDrafts['section_' + '6'.repeat(32)] = {
                section_id: 'section_' + '6'.repeat(32),
                section_key: 'core_findings',
                base_section_revision: 2,
                content: 'local after conflict',
                originalContent: '<b>body</b>',
                edit_reason: 'conflict test',
                dirty: true,
                conflict: false,
              };
              api.ivV2State.reportDirty = true;
              const reportJId = 'report_' + 'j'.repeat(32);
              const reportJSectionId = 'section_' + 'j'.repeat(32);
              const reportJ = {
                ...api.ivV2State.reportResponse,
                report_version_id: reportJId,
                sections: [reportSection({ id: reportJSectionId, revision: 5, content: 'new server body' })],
              };
              const importJ = {
                ...api.ivV2State.importData,
                report_summary: { report_version_id: reportJId, approval_ready: false, status: 'draft', audit_status: 'pending_reaudit', pending_reaudit_count: 1, blocking_issue_count: 0 },
              };
              enqueue(`/api/v1/interview-report-sections/${api.ivV2State.reportResponse.sections[0].section_id}`, {
                error: { code: 'REPORT_SECTION_REVISION_CONFLICT', message: 'section changed' },
              }, { method: 'PATCH', status: 409, ok: false });
              enqueueImportRefresh(importJ);
              enqueue(`/api/v1/interview-projects/${api.ivV2State.projectId}/analysis-runs/current`, {
                analysis_run_id: api.ivV2State.importData.analysis_summary.analysis_run_id,
                status: 'completed',
                findings: [],
                stat_facts: [],
                limitations: [],
              });
              enqueue(`/api/v1/interview-reports/${reportJId}`, reportJ);
              await api.ivV2SaveReportSection('section_' + '6'.repeat(32));
              assertQueueDrained('patch conflict refresh');
              const patch409Draft = api.ivV2State.reportSectionDrafts[reportJSectionId];
              const patch409Selection = api.ivV2State.selectedReportSectionId;
              const fetchCountAfter409 = log.filter(item => item.type === 'fetch').length;
              await api.ivV2SaveReportSection(reportJSectionId);
              const patch409BlocksRetry = log.filter(item => item.type === 'fetch').length === fetchCountAfter409;

              resetScenario();
              setBaseState();
              const reportGId = 'report_' + 'g'.repeat(32);
              const reportHId = 'report_' + 'h'.repeat(32);
              const reportG = { ...api.ivV2State.reportResponse, report_version_id: reportGId, sections: [reportSection({ revision: 3, content: 'new body' })] };
              const reportH = { ...api.ivV2State.reportResponse, report_version_id: reportHId, sections: [reportSection({ revision: 4, content: 'new body' })] };
              const importG = { ...api.ivV2State.importData, report_summary: { report_version_id: reportGId, approval_ready: false, status: 'draft', audit_status: 'pending_reaudit', pending_reaudit_count: 1, blocking_issue_count: 0 } };
              const importH = { ...api.ivV2State.importData, report_summary: { report_version_id: reportHId, approval_ready: false, status: 'draft', audit_status: 'pending_reaudit', pending_reaudit_count: 1, blocking_issue_count: 0 } };
              enqueue(`/api/v1/interview-report-sections/${api.ivV2State.reportResponse.sections[0].section_id}`, {
                report_version_id: reportGId,
                section_id: api.ivV2State.reportResponse.sections[0].section_id,
                section_revision: 3,
                audit_status: 'pending_reaudit',
                reaudit_job_id: 'job_' + '7'.repeat(32),
              }, { method: 'PATCH' });
              enqueue(`/api/v1/interview-reports/${reportGId}`, reportG);
              enqueueImportRefresh(importG);
              enqueue(`/api/v1/interview-report-sections/${api.ivV2State.reportResponse.sections[0].section_id}:reaudit`, {
                report_version_id: reportHId,
                section_id: api.ivV2State.reportResponse.sections[0].section_id,
                section_revision: 4,
                audit_status: 'pending_reaudit',
                reaudit_job_id: 'job_' + '7'.repeat(32),
              }, { method: 'POST' });
              enqueue(`/api/v1/interview-reports/${reportHId}`, reportH);
              enqueueImportRefresh(importH);
              api.ivV2State.reportSectionDrafts['section_' + '6'.repeat(32)] = {
                section_id: 'section_' + '6'.repeat(32),
                section_key: 'core_findings',
                base_section_revision: 2,
                content: 'new body',
                originalContent: '<b>body</b>',
                edit_reason: 'x',
                dirty: true,
                conflict: false,
              };
              api.ivV2State.reportDirty = true;
              await api.ivV2SaveReportSection('section_' + '6'.repeat(32));
              api.ivV2State.reportSectionDrafts['section_' + '6'.repeat(32)] = {
                section_id: 'section_' + '6'.repeat(32),
                section_key: 'core_findings',
                base_section_revision: 4,
                content: 'new body',
                originalContent: 'new body',
                edit_reason: '',
                dirty: false,
                conflict: false,
              };
              api.ivV2State.reportDirty = false;
              await api.ivV2ReauditReportSection('section_' + '6'.repeat(32));
              assertQueueDrained('save and pending reaudit');
              const pendingToast = log.filter(item => item.type === 'toast').some(item => item.message.includes('章节仍待重审'));

              resetScenario();
              setBaseState();
              api.ivV2State.reportClaimCache[api.ivV2ReportClaimCacheKey(api.ivV2State.selectedReportClaimId)] = {
                claim: api.ivV2State.reportResponse.claims[0],
                findings: [{
                  title: 'finding<script>',
                  statement: 'finding<body>',
                  supporting_cases: [{ participant_id: 'participant<' , evidence_ids: ['ev<' ] }],
                  counterexample_cases: [{ participant_id: 'counter&', evidence_ids: ['ev&'] }],
                  observation_cases: [{ participant_id: 'observer>', evidence_ids: ['ev>'] }],
                  limitations: ['limitation<script>'],
                  coverage_scope: { module_id: 'module<1>' },
                }],
                stat_fact: { stat_fact_id: 'stat_' + 'z'.repeat(32), numerator: 1, denominator: null, proportion: null, denominator_participant_ids: ['participant<' ] },
                audit_issues: [],
              };
              const boundEvidenceIds = api.ivV2ReportEvidenceIdsFromClaim(
                api.ivV2State.reportClaimCache[api.ivV2ReportClaimCacheKey(api.ivV2State.selectedReportClaimId)]
              );
              api.ivV2State.reportEvidenceCache['ev_' + 'a'.repeat(32)] = {
                evidence: { sheet_name: 'Sheet<script>', cell_address: 'A1', raw_content: '<script>alert(1)</script>', prompt_text: '<b>note</b>', recorder_label: 'rec<&>' },
                source_context: { source_cell_id: 'A1' },
              };
              const drawer = api.ivV2ReportDrawerHtml();

              resetScenario();
              setBaseState();
              const claimBId = 'claim_' + 'q'.repeat(32);
              const evidenceBId = 'ev_' + 'b'.repeat(32);
              const evidenceMissingId = 'ev_' + 'm'.repeat(32);
              api.ivV2State.reportResponse.claims.push({ claim_id: claimBId, claim_type: 'finding', text: 'claim b', evidence_ids: [evidenceBId] });
              api.ivV2State.reportResponse.sections[0].claim_ids.push(claimBId);
              api.ivV2State.selectedReportEvidenceId = 'ev_' + 'a'.repeat(32);
              enqueue(`/api/v1/interview-reports/${api.ivV2State.reportResponse.report_version_id}/claims/${claimBId}`, {
                claim: { claim_id: claimBId, claim_type: 'finding', text: 'claim b', evidence_ids: [evidenceBId], evidence_roles: ['support'] },
                findings: [],
                stat_fact: null,
                audit_issues: [],
              });
              await api.ivV2LoadReportClaim(claimBId);
              assertQueueDrained('switch claim');
              const claimSwitchClearedEvidence = api.ivV2State.selectedReportEvidenceId === '';
              enqueue(`/api/v1/interview-evidence/${evidenceBId}/context`, {
                evidence: { evidence_id: evidenceBId, sheet_name: 'Sheet B', cell_address: 'B2', raw_content: 'source b', recorder_label: 'recorder b' },
                source_context: { source_cell_id: 'B2' },
              });
              await api.ivV2LoadReportEvidence(evidenceBId);
              assertQueueDrained('load claim evidence');
              const claimEvidenceLoaded = api.ivV2State.selectedReportEvidenceId === evidenceBId
                && api.ivV2ReportDrawerHtml().includes('Sheet B');
              enqueue(`/api/v1/interview-evidence/${evidenceMissingId}/context`, {
                error: { code: 'EVIDENCE_NOT_FOUND', message: 'missing' },
              }, { status: 404, ok: false });
              await api.ivV2LoadReportEvidence(evidenceMissingId);
              assertQueueDrained('load missing evidence');
              const missingEvidenceRecorded = api.ivV2State.reportEvidenceCache[evidenceMissingId]?.missing === true;

              resetScenario();
              setBaseState();
              api.ivV2State.reportResponse.audit_status = 'audit_passed';
              api.ivV2State.reportResponse.sections = [reportSection({ auditStatus: 'audit_passed' })];
              api.ivV2State.reportApprovalNote = 'approved in vm';
              const reportIId = 'report_' + 'i'.repeat(32);
              const reportI = { ...api.ivV2State.reportResponse, report_version_id: reportIId, status: 'approved', approved_by: 'tester', approved_at: '2026-09-02T00:00:00Z' };
              const importI = { ...api.ivV2State.importData, report_summary: { report_version_id: reportIId, approval_ready: false, status: 'approved', audit_status: 'audit_passed', pending_reaudit_count: 0, blocking_issue_count: 0 } };
              enqueue(`/api/v1/interview-reports/${api.ivV2State.reportResponse.report_version_id}:approve`, { report_version_id: reportIId }, { method: 'POST' });
              enqueue(`/api/v1/interview-reports/${reportIId}`, reportI);
              enqueueImportRefresh(importI);
              await api.ivV2ApproveReport();
              assertQueueDrained('approve report');
              const approveFetch = log.find(item => item.method === 'POST' && item.url.endsWith(':approve'));
              const approveBody = JSON.parse(approveFetch.body);
              const approveSucceeded = api.ivV2State.reportResponse.status === 'approved'
                && api.ivV2State.reportApprovalNote === ''
                && log.some(item => item.type === 'toast' && item.message === '报告已批准');

              resetScenario();
              setBaseState();
              const staleToken = api.ivV2NextReportToken();
              enqueue(`/api/v1/interview-reports/${'report_' + 'y'.repeat(32)}`, { report_version_id: 'report_' + 'y'.repeat(32), status: 'draft', is_current_version: true, sections: [], claims: [], audit_issues: [] });
              api.ivV2State.reportToken = staleToken + 1;
              await api.ivV2LoadCurrentReport('report_' + 'y'.repeat(32), { token: staleToken });
              assertQueueDrained('ignore stale response');

              const result = {
                nestedRefreshRan,
                analysisBody: JSON.parse(analysisFetch.body),
                approvedConfirmCalls,
                approvedPatchBody,
                approvedSaveCreatedDraft,
                dirtyBlocked,
                conflictFlag: Boolean(conflictDraft && conflictDraft.conflict),
                conflictContent: conflictDraft?.content || '',
                conflictSelectionMigrated,
                reportVersionClearedApprovalNote,
                approvalNoteBlocked,
                leaveReportClearedUnsaved,
                leaveReportConfirmCalls,
                oldReadOnly,
                staleReadOnly,
                staleSameVersionClearedApprovalNote,
                createReportBody,
                createReportSucceeded,
                patch409Conflict: Boolean(patch409Draft?.dirty && patch409Draft?.conflict),
                patch409Content: patch409Draft?.content || '',
                patch409Selection,
                patch409BlocksRetry,
                pendingToast,
                escapedDrawer: drawer.includes('&lt;script&gt;') && !drawer.includes('<script>'),
                boundEvidenceIds,
                claimSwitchClearedEvidence,
                claimEvidenceLoaded,
                missingEvidenceRecorded,
                approveBody,
                approveSucceeded,
                tokenIgnored: api.ivV2State.reportResponse.report_version_id === 'report_' + '5'.repeat(32),
              };
              process.stdout.write(JSON.stringify(result));
            }

            run().catch(error => {
              console.error(error);
              process.exit(1);
            });
            """
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".cjs", delete=False) as handle:
            handle.write(script)
            script_path = Path(handle.name)
        try:
            completed = subprocess.run(
                ["node", str(script_path)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
        finally:
            script_path.unlink(missing_ok=True)
        self.assertEqual(0, completed.returncode, msg=completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["nestedRefreshRan"])
        self.assertEqual(True, result["analysisBody"]["freeze_current"])
        self.assertRegex(result["analysisBody"]["base_analysis_run_id"], r"^analysis_")
        self.assertEqual(1, result["approvedConfirmCalls"])
        self.assertEqual(2, result["approvedPatchBody"]["base_section_revision"])
        self.assertEqual("edited approved body", result["approvedPatchBody"]["content"])
        self.assertEqual("approved-edit", result["approvedPatchBody"]["edit_reason"])
        self.assertTrue(result["approvedPatchBody"]["locked"])
        self.assertTrue(result["approvedSaveCreatedDraft"])
        self.assertTrue(result["dirtyBlocked"])
        self.assertTrue(result["conflictFlag"])
        self.assertEqual("draft local", result["conflictContent"])
        self.assertTrue(result["conflictSelectionMigrated"])
        self.assertTrue(result["reportVersionClearedApprovalNote"])
        self.assertTrue(result["leaveReportClearedUnsaved"])
        self.assertEqual(1, result["leaveReportConfirmCalls"])
        self.assertTrue(result["approvalNoteBlocked"])
        self.assertTrue(result["oldReadOnly"])
        self.assertTrue(result["staleReadOnly"])
        self.assertTrue(result["staleSameVersionClearedApprovalNote"])
        self.assertEqual(True, result["createReportBody"]["freeze_current"])
        self.assertRegex(result["createReportBody"]["base_report_version_id"], r"^report_")
        self.assertTrue(result["createReportSucceeded"])
        self.assertTrue(result["patch409Conflict"])
        self.assertEqual("local after conflict", result["patch409Content"])
        self.assertEqual("section_" + "j" * 32, result["patch409Selection"])
        self.assertTrue(result["patch409BlocksRetry"])
        self.assertTrue(result["pendingToast"])
        self.assertTrue(result["escapedDrawer"])
        self.assertEqual(["ev_" + "a" * 32], result["boundEvidenceIds"])
        self.assertTrue(result["claimSwitchClearedEvidence"])
        self.assertTrue(result["claimEvidenceLoaded"])
        self.assertTrue(result["missingEvidenceRecorded"])
        self.assertRegex(result["approveBody"]["base_report_version_id"], r"^report_")
        self.assertEqual("approved", result["approveBody"]["decision"])
        self.assertEqual("approved in vm", result["approveBody"]["note"])
        self.assertTrue(result["approveSucceeded"])
        self.assertTrue(result["tokenIgnored"])


if __name__ == "__main__":
    unittest.main()
