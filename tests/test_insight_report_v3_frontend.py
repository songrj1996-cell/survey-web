import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT_JS = (ROOT / "static" / "js" / "features" / "report.js").read_text(
    encoding="utf-8"
)


class InsightV3FrontendContractTests(unittest.TestCase):
    def test_all_sources_button_is_honestly_named_without_changing_query(self):
        source = (ROOT / "static/js/features/report-quick.js").read_text(encoding="utf-8")
        self.assertIn("source.textContent = '查看全部回答'", source)
        self.assertIn("openReportSources('', '全部题目', 'all')", source)

    def test_audit_progress_is_retained_until_report_done(self):
        self.assertIn("let insightAudit = null;", REPORT_JS)
        self.assertIn("if (ev.phase === 'audit') insightAudit = { ...ev };", REPORT_JS)
        self.assertIn("['needs_review', 'incomplete'].includes(insightAudit?.audit_status)", REPORT_JS)

    def test_saved_report_warning_is_explicit_and_non_blocking(self):
        self.assertIn("语义审校未完整执行", REPORT_JS)
        self.assertIn("语义审校仍有待确认项", REPORT_JS)
        self.assertIn("报告已保存并在标题下方标注", REPORT_JS)
        self.assertIn("showToast(`${prefix}${countText}需要人工复核", REPORT_JS)


if __name__ == "__main__":
    unittest.main()
