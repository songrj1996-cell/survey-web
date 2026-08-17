from __future__ import annotations

from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HISTORY_SCRIPT = PROJECT_ROOT / "static" / "js" / "features" / "history.js"


def _function_body(source: str, name: str) -> str:
    declaration = re.search(rf"\bfunction\s+{re.escape(name)}\s*\(", source)
    if declaration is None:
        raise AssertionError(f"missing function {name}()")
    body_start = source.find("{", declaration.end())
    if body_start < 0:
        raise AssertionError(f"missing body for function {name}()")
    depth = 0
    for index in range(body_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[body_start + 1:index]
    raise AssertionError(f"unbalanced body for function {name}()")


class QuestionnaireSnapshotHistoryFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = HISTORY_SCRIPT.read_text(encoding="utf-8")

    def test_history_card_uses_safe_snapshot_summary_only(self):
        body = _function_body(self.source, "historySnapshotSummaryMeta")
        self.assertIn("questionnaire_snapshot_summary", body)
        self.assertIn("summary.provider", body)
        self.assertIn("快照结构 ·", body)
        for forbidden in (
            "has_questionnaire_snapshot",
            "snapshot_id",
            "package_sha256",
            "definition_sha256",
            "owner_ref",
            "column_indexes",
            "questionnaire_response_bindings",
            "provider_label",
        ):
            self.assertNotIn(forbidden, body)

    def test_render_history_card_reuses_existing_pill_class(self):
        body = _function_body(self.source, "renderHistoryCard")
        self.assertIn("historySnapshotSummaryMeta(h)", body)
        self.assertIn("hist-card__qa hist-card__qa--done", body)


if __name__ == "__main__":
    unittest.main()
