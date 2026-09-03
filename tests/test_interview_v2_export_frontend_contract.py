import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static" / "js" / "features" / "interview-v2.js").read_text(
    encoding="utf-8"
)


class InterviewV2ExportFrontendContractTests(unittest.TestCase):
    def _between(self, start: str, end: str) -> str:
        start_index = JS.index(start)
        end_index = JS.index(end, start_index)
        return JS[start_index:end_index]

    def test_export_uses_fixed_artifact_then_download_contract(self):
        block = self._between(
            "async function ivV2ExportApprovedReport() {",
            "function ivV2ReportClaimCacheKey(claimId) {",
        )
        for snippet in (
            "!ivV2ReportExportReady(report)",
            "/api/v1/interview-reports/${reportVersionId}/exports",
            "format: 'docx'",
            "include_evidence_appendix: true",
            "cache: 'no-store'",
            "artifact.export_artifact_id",
            "/api/v1/interview-export-artifacts/${artifactId}/download",
            "await downloadResponse.blob()",
            "ivV2DownloadExportBlob(downloadResponse, blob, artifact.file_name || fallbackName)",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, block)
        self.assertLess(block.index("/exports`"), block.index("/download`"))

    def test_export_ui_and_version_invalidation_are_explicit(self):
        approval = self._between(
            "function ivV2ReportApprovalHtml() {",
            "function ivV2ReportDrawerHtml() {",
        )
        export_gate = self._between(
            "function ivV2ReportExportReady(report",
            "function ivV2InvalidateReportWorkspace() {",
        )
        load = self._between(
            "async function ivV2LoadCurrentReport(reportVersionId",
            "async function ivV2CreateAnalysisRun() {",
        )
        self.assertIn('data-iv-v2-action="report-export-word"', approval)
        self.assertIn("!ivV2ReportExportReady(report)", approval)
        self.assertIn("report?.is_current_version", export_gate)
        self.assertIn("=== 'approved'", export_gate)
        self.assertIn("['audited', 'audit_passed'].includes(auditStatus)", export_gate)
        self.assertIn("!ivV2State.reportDirty", export_gate)
        self.assertIn("导出 Word（含证据附录）", approval)
        self.assertIn("ivV2State.reportExportArtifact = null", load)
        self.assertNotIn("raw_content", approval)
        self.assertNotIn("recorder_label", approval)

    def test_vm_executes_gates_download_busy_and_error_contracts(self):
        script = textwrap.dedent(
            r"""
            const fs = require('node:fs');
            const vm = require('node:vm');

            const source = fs.readFileSync('static/js/features/interview-v2.js', 'utf8');
            const events = [];
            const fetchQueue = [];
            const elements = new Map();

            function makeNode(id = '') {
              return {
                id,
                hidden: false,
                value: '',
                checked: false,
                disabled: false,
                readOnly: false,
                innerHTML: '',
                textContent: '',
                dataset: {},
                files: [],
                style: {},
                classList: {
                  add() {}, remove() {}, toggle() {}, contains() { return false; },
                },
                addEventListener() {},
                querySelector() { return makeNode(); },
                querySelectorAll() { return []; },
                closest() { return null; },
                setAttribute() {},
                removeAttribute() {},
                scrollTo() {},
                click() { events.push({ type: 'click', download: this.download || '' }); },
                remove() {},
              };
            }

            const document = {
              body: { appendChild(node) { events.push({ type: 'append', download: node.download || '' }); } },
              getElementById(id) {
                if (!elements.has(id)) elements.set(id, makeNode(id));
                return elements.get(id);
              },
              querySelector() { return makeNode(); },
              querySelectorAll() { return []; },
              createElement(tag) { return makeNode(tag); },
            };

            function response(spec) {
              return {
                ok: spec.ok !== false,
                status: spec.status || 200,
                headers: { get(name) {
                  return String(name).toLowerCase() === 'content-disposition'
                    ? (spec.disposition || '')
                    : '';
                } },
                json: async () => spec.json || {},
                blob: async () => new Blob([spec.bytes || 'docx-bytes'], {
                  type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                }),
              };
            }

            async function fetchStub(url, options = {}) {
              events.push({
                type: 'fetch',
                url: String(url),
                method: String(options.method || 'GET').toUpperCase(),
                body: options.body || null,
                cache: String(options.cache || ''),
              });
              const spec = fetchQueue.shift();
              if (!spec) throw new Error('unexpected fetch: ' + url);
              if (String(spec.url) !== String(url)) {
                throw new Error(`expected ${spec.url}, received ${url}`);
              }
              return response(spec);
            }

            const urlApi = {
              createObjectURL() { events.push({ type: 'object-url' }); return 'blob:test'; },
              revokeObjectURL(url) { events.push({ type: 'revoke', url }); },
            };
            const context = {
              console,
              document,
              currentMode: 'interview',
              fetch: fetchStub,
              Blob,
              URL: urlApi,
              URLSearchParams,
              FormData: class { append() {} },
              showToast(message, tone) { events.push({ type: 'toast', message, tone }); },
              esc(value) {
                return String(value ?? '')
                  .replace(/&/g, '&amp;')
                  .replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;')
                  .replace(/"/g, '&quot;')
                  .replace(/'/g, '&#39;');
              },
              window: {
                ivState: { track: 'v2', currentStep: 1 },
                innerWidth: 1200,
                addEventListener() {},
                setTimeout() { return 1; },
                clearTimeout() {},
                confirm() { return true; },
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
              ivV2ExportApprovedReport,
              ivV2ReportApprovalHtml,
              ivV2LoadCurrentReport,
            })`, context);

            const reportId = 'report_' + '1'.repeat(32);
            const exportId = 'export_' + '2'.repeat(32);
            function setApproved() {
              api.ivV2State.currentStep = 5;
              api.ivV2State.status = 'READY_FOR_DOSSIERS';
              api.ivV2State.projectId = 'project_' + '3'.repeat(32);
              api.ivV2State.importId = 'import_' + '4'.repeat(32);
              api.ivV2State.reportToken = 7;
              api.ivV2State.reportBusy = false;
              api.ivV2State.reportExportBusy = false;
              api.ivV2State.reportExportArtifact = null;
              api.ivV2State.reportApprovalNote = '';
              api.ivV2State.reportDirty = false;
              api.ivV2State.importData = {
                report_summary: {
                  report_version_id: reportId,
                  status: 'approved',
                  audit_status: 'audit_passed',
                  approval_ready: false,
                  pending_reaudit_count: 0,
                  blocking_issue_count: 0,
                },
                analysis_summary: {},
                dossier_summary: {},
              };
              api.ivV2State.reportResponse = {
                report_version_id: reportId,
                report_version_number: 4,
                status: 'approved',
                audit_status: 'audit_passed',
                is_current_version: true,
                approved_at: '2026-09-02T08:00:00Z',
                approved_by: 'owner@example.com',
                sections: [],
                claims: [],
                audit_issues: [],
              };
            }

            function buttonDisabled(html) {
              const match = html.match(/<button[^>]+data-iv-v2-action="report-export-word"[^>]*>/);
              return Boolean(match && match[0].includes('disabled'));
            }

            async function run() {
              setApproved();
              const approvedEnabled = !buttonDisabled(api.ivV2ReportApprovalHtml());
              api.ivV2State.reportResponse.audit_status = 'audit_failed';
              const auditFailedDisabled = buttonDisabled(api.ivV2ReportApprovalHtml());
              await api.ivV2ExportApprovedReport();
              const auditFailedNoFetch = events.length === 0;
              api.ivV2State.reportResponse.status = 'draft';
              const draftDisabled = buttonDisabled(api.ivV2ReportApprovalHtml());
              api.ivV2State.reportResponse.status = 'stale';
              const staleDisabled = buttonDisabled(api.ivV2ReportApprovalHtml());
              api.ivV2State.reportResponse.status = 'approved';
              api.ivV2State.reportResponse.is_current_version = false;
              const oldDisabled = buttonDisabled(api.ivV2ReportApprovalHtml());
              api.ivV2State.reportResponse.is_current_version = true;
              api.ivV2State.reportDirty = true;
              const dirtyDisabled = buttonDisabled(api.ivV2ReportApprovalHtml());

              setApproved();
              const artifact = {
                export_artifact_id: exportId,
                report_version_id: reportId,
                report_version_number: 4,
                status: 'READY',
                format: 'docx',
                file_name: 'fallback.docx',
                byte_size: 10,
                created_at: '2026-09-02T08:01:00Z',
              };
              fetchQueue.push({
                url: `/api/v1/interview-reports/${reportId}/exports`,
                json: artifact,
              });
              fetchQueue.push({
                url: `/api/v1/interview-export-artifacts/${exportId}/download`,
                bytes: 'fixed-docx',
                disposition: "attachment; filename*=UTF-8''%E8%AE%BF%E8%B0%88-v4.docx",
              });
              const first = api.ivV2ExportApprovedReport();
              const second = api.ivV2ExportApprovedReport();
              await Promise.all([first, second]);
              const successEvents = events.slice();
              const post = successEvents.find(item => item.type === 'fetch' && item.method === 'POST');
              const download = successEvents.find(item => item.type === 'fetch' && item.method === 'GET');
              const exactBody = JSON.parse(post.body);
              const fetchCount = successEvents.filter(item => item.type === 'fetch').length;
              const createCache = post.cache;
              const downloadCache = download.cache;
              const downloadedName = successEvents.find(item => item.type === 'click')?.download || '';
              const successArtifactId = api.ivV2State.reportExportArtifact?.export_artifact_id || '';
              const busyCleared = api.ivV2State.reportExportBusy === false;

              events.length = 0;
              setApproved();
              context.refreshCount = 0;
              vm.runInContext(
                'ivV2LoadReportWorkspace = async () => { globalThis.refreshCount += 1; return true; };',
                context,
              );
              fetchQueue.push({
                url: `/api/v1/interview-reports/${reportId}/exports`,
                ok: false,
                status: 409,
                json: {
                  error: {
                    code: 'REPORT_EXPORT_BLOCKED',
                    message: '报告版本已变化',
                    suggested_action: 'refresh_report',
                  },
                },
              });
              await api.ivV2ExportApprovedReport();
              const conflictCode = api.ivV2State.statusCode;
              const conflictMessage = api.ivV2State.errorMessage;
              const conflictRefreshed = context.refreshCount === 1;

              events.length = 0;
              setApproved();
              api.ivV2State.reportExportArtifact = artifact;
              const nextReportId = 'report_' + '5'.repeat(32);
              fetchQueue.push({
                url: `/api/v1/interview-reports/${nextReportId}`,
                json: {
                  ...api.ivV2State.reportResponse,
                  report_version_id: nextReportId,
                  report_version_number: 5,
                  status: 'draft',
                },
              });
              await api.ivV2LoadCurrentReport(nextReportId, {
                token: api.ivV2State.reportToken,
              });
              const versionChangeClearedArtifact = api.ivV2State.reportExportArtifact === null;

              process.stdout.write(JSON.stringify({
                approvedEnabled,
                auditFailedDisabled,
                auditFailedNoFetch,
                draftDisabled,
                staleDisabled,
                oldDisabled,
                dirtyDisabled,
                exactBody,
                fetchCount,
                createCache,
                downloadCache,
                downloadedName,
                successArtifactId,
                busyCleared,
                conflictCode,
                conflictMessage,
                conflictRefreshed,
                versionChangeClearedArtifact,
                queueDrained: fetchQueue.length === 0,
              }));
            }

            run().catch(error => {
              console.error(error);
              process.exit(1);
            });
            """
        )
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".cjs", delete=False
        ) as handle:
            handle.write(script)
            script_path = Path(handle.name)
        try:
            completed = subprocess.run(
                ["node", str(script_path)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        finally:
            script_path.unlink(missing_ok=True)

        self.assertEqual(0, completed.returncode, msg=completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["approvedEnabled"])
        self.assertTrue(result["auditFailedDisabled"])
        self.assertTrue(result["auditFailedNoFetch"])
        self.assertTrue(result["draftDisabled"])
        self.assertTrue(result["staleDisabled"])
        self.assertTrue(result["oldDisabled"])
        self.assertTrue(result["dirtyDisabled"])
        self.assertEqual(
            {"format": "docx", "include_evidence_appendix": True},
            result["exactBody"],
        )
        self.assertEqual(2, result["fetchCount"])
        self.assertEqual("no-store", result["createCache"])
        self.assertEqual("no-store", result["downloadCache"])
        self.assertEqual("访谈-v4.docx", result["downloadedName"])
        self.assertEqual(ARTIFACT_ID := "export_" + "2" * 32, result["successArtifactId"])
        self.assertTrue(result["busyCleared"])
        self.assertEqual("REPORT_EXPORT_BLOCKED", result["conflictCode"])
        self.assertEqual("报告版本已变化", result["conflictMessage"])
        self.assertTrue(result["conflictRefreshed"])
        self.assertTrue(result["versionChangeClearedArtifact"])
        self.assertTrue(result["queueDrained"])


if __name__ == "__main__":
    unittest.main()
