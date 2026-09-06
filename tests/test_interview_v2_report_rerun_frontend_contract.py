import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS = (
    ROOT / "static" / "js" / "features" / "interview-v2.js"
).read_text(encoding="utf-8")


class InterviewV2ReportRerunFrontendContractTests(unittest.TestCase):
    def _between(self, start: str, end: str) -> str:
        start_index = JS.index(start)
        end_index = JS.index(end, start_index)
        return JS[start_index:end_index]

    def test_single_section_rerun_button_and_route_are_wired(self):
        body = self._between(
            "function ivV2ReportBodyHtml() {",
            "function ivV2ReportApprovalHtml() {",
        )
        handler = self._between(
            "async function ivV2RerunReportSection(sectionId) {",
            "async function ivV2ApproveReport() {",
        )

        self.assertIn('data-iv-v2-action="report-rerun-section"', body)
        self.assertIn("重生成本章节", body)
        self.assertIn(
            "/api/v1/interview-projects/${ivV2State.projectId}/reruns",
            handler,
        )
        self.assertIn("'Idempotency-Key': idempotencyKey", handler)
        self.assertIn("ivV2RerunReportSection(button.dataset.sectionId || '')", JS)

    def test_payload_is_exact_single_section_contract(self):
        payload = self._between(
            "function ivV2ReportSectionRerunPayload(section) {",
            "function ivV2ReportApprovePayload() {",
        )

        for snippet in (
            "from_stage: 'report_section'",
            "base_report_version_id: String(ivV2State.reportResponse?.report_version_id || '')",
            "section_id: String(section?.section_id || '')",
            "base_section_revision: Number(section?.section_revision || 1)",
            "instruction: ''",
            "preserve_manual_report_edits: true",
            "reuse_unchanged_artifacts: true",
            "force: false",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, payload)

    def test_button_is_disabled_for_dirty_locked_stale_or_busy_state(self):
        refresh = self._between(
            "function ivV2SyncConfirmedControls() {",
            "function ivV2SyncCommentCounter(textarea) {",
        )
        body = self._between(
            "function ivV2ReportBodyHtml() {",
            "function ivV2ReportApprovalHtml() {",
        )

        self.assertIn("action === 'report-rerun-section'", refresh)
        self.assertIn("operationBusy || !editable", refresh)
        self.assertIn("Boolean(section.locked)", refresh)
        self.assertIn("ivV2HasUnsavedReportWork()", refresh)
        self.assertIn("Boolean(draft?.conflict)", refresh)
        self.assertIn(
            "const rerunEnabled = editable && !section.locked && !ivV2HasUnsavedReportWork() && !draft?.conflict",
            body,
        )
        self.assertIn(
            "report?.is_current_version && ivV2ReportEditableStatus(report?.status)",
            JS,
        )
        self.assertIn("['stale', 'superseded']", JS)

    def test_idempotency_key_is_stable_for_one_report_section_revision(self):
        key_helper = self._between(
            "function ivV2ReportSectionRerunKey(section) {",
            "function ivV2ReportSectionRerunPayload(section) {",
        )

        self.assertIn("reportRerunKeys: {}", JS)
        self.assertIn("section?.section_id || ''", key_helper)
        self.assertIn("Number(section?.section_revision || 1)", key_helper)
        self.assertIn("window.crypto?.randomUUID", key_helper)
        self.assertIn("ivV2State.reportRerunKeys[fingerprint]", key_helper)

    def test_success_refreshes_current_report_and_conflict_refreshes_workspace(self):
        handler = self._between(
            "async function ivV2RerunReportSection(sectionId) {",
            "async function ivV2ApproveReport() {",
        )
        conflict = handler.index("if (response.status === 409)")
        refresh = handler.index("await ivV2LoadReportWorkspace", conflict)
        feedback = handler.index("ivV2SetStatusError", refresh)
        self.assertLess(refresh, feedback)
        self.assertIn("await ivV2LoadCurrentReport(data.report_version_id", handler)
        self.assertIn("ivV2ClearUnsavedReportWork()", handler)
        self.assertIn("data.rerun?.reused", handler)


if __name__ == "__main__":
    unittest.main()
