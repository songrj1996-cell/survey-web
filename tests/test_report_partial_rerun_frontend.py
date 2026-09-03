from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PartialRerunFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.js = (ROOT / "static" / "js" / "features" / "report.js").read_text(
            encoding="utf-8"
        )

    def test_modal_exposes_question_part_instruction_and_page_lifetime_warning(self):
        for token in (
            'id="btn-report-partial-rerun"',
            'id="report-partial-rerun-type"',
            '<option value="question">重做此题</option>',
            '<option value="part">重做此 Part</option>',
            'id="report-partial-rerun-target"',
            'id="report-partial-rerun-instruction"',
            "请保持当前页面打开",
            "失败不会覆盖基础版本",
        ):
            self.assertIn(token, self.html)

    def test_frontend_posts_selected_scope_and_waits_for_done_event(self):
        self.assertIn("/partial-rerun`,", self.js)
        self.assertIn("base_version: baseVersion", self.js)
        self.assertIn("target_type: type", self.js)
        self.assertIn("target_key: key", self.js)
        self.assertIn("ev.type === 'partial_rerun_progress'", self.js)
        self.assertIn("ev.type === 'partial_rerun_done'", self.js)
        self.assertIn("if (!doneEvent) throw new Error", self.js)
        self.assertIn("await loadHistoryReportVersion(doneEvent.version)", self.js)

    def test_version_picker_displays_base_scope_and_changed_sections(self):
        self.assertIn("function reportVersionRevisionText(item)", self.js)
        self.assertIn("details.base_version", self.js)
        self.assertIn("details.target_label", self.js)
        self.assertIn("details.changed_sections", self.js)
        self.assertIn("未整份重跑", self.js)


if __name__ == "__main__":
    unittest.main()
