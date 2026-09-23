import unittest
from unittest.mock import patch

from app.services.insight_report_v3.findings import (
    build_findings_query,
    generate_insight_findings,
)


def _plan():
    return {
        "columns": [
            {"index": 0, "name": "Q1", "role": "open_text"},
            {"index": 1, "name": "Q2", "role": "open_text"},
            {"index": 2, "name": "年龄", "role": "profile_dim"},
        ],
        "parts": [{"name": "反馈", "column_indexes": [0, 1, 2]}],
        "analysis_focus": {"report_organization": "按问题机制组织"},
    }


def _open_text():
    return {
        0: [
            {"respondent_key": "A", "text": "回答 A1"},
            {"respondent_key": "A", "text": "回答 A2"},
            {"respondent_key": "B", "text": "回答 B"},
            {"respondent_key": "C", "text": "回答 C"},
        ],
        1: [
            {"respondent_key": "A", "text": "第二题 A"},
            {"respondent_key": "D", "text": "第二题 D"},
            {"respondent_key": "E", "text": "第二题 E"},
        ],
        2: [{"respondent_key": "A", "text": "18-24"}],
    }


async def _collect(**kwargs):
    events = []
    async for event in generate_insight_findings(**kwargs):
        events.append(event)
    return events


class InsightReportV3FindingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_failed_question_does_not_block_other_question(self):
        calls = {"Q1": 0, "Q2": 0}

        async def fake_call(_system, query, **_kwargs):
            key = "Q1" if "<question>\nQ1\n" in query else "Q2"
            calls[key] += 1
            if key == "Q1":
                return {
                    "data": {
                        "findings": [{
                            "id": "f1", "claim": "部分玩家有反馈", "kind": "fact",
                            "frequency_tier": "部分", "support_count": 3,
                            "response_ids": ["r0001", "r0002"], "caveat": "",
                        }]
                    },
                    "model": "mock", "raw_len": 10, "repaired": False,
                    "error": "", "duration_seconds": 0.01,
                }
            return {
                "data": None, "model": "mock", "raw_len": 0,
                "repaired": True, "error": "invalid output", "duration_seconds": 0.01,
            }

        with patch(
            "app.services.insight_report_v3.findings._direct_json_call",
            new=fake_call,
        ):
            events = await _collect(
                open_text=_open_text(), plan=_plan(), headers=["Q1", "Q2", "年龄"],
                file_sha256="c" * 64, concurrency=2, max_retries=2,
            )

        artifact = [payload for kind, payload in events if kind == "result"][0]
        self.assertEqual(artifact["diagnostics"]["status_counts"], {
            "complete": 1, "degraded": 1, "skipped": 0,
        })
        by_key = {item["question_key"]: item for item in artifact["questions"]}
        self.assertEqual(by_key["0"]["status"], "complete")
        self.assertEqual(by_key["1"]["status"], "degraded")
        self.assertEqual(calls, {"Q1": 1, "Q2": 3})
        self.assertEqual(len(by_key["1"]["degraded_source_ids"]), 3)
        self.assertEqual(artifact["diagnostics"]["logical_calls"], 4)

    async def test_input_too_large_degrades_without_model_call(self):
        with patch(
            "app.services.insight_report_v3.findings._direct_json_call",
        ) as direct:
            events = await _collect(
                open_text={0: [{"respondent_key": "A", "text": "x" * 100}]},
                plan={
                    "columns": [{"index": 0, "name": "Q1", "role": "open_text"}],
                    "parts": [{"name": "反馈", "column_indexes": [0]}],
                },
                headers=["Q1"], file_sha256="d" * 64,
                input_char_limit=20, max_retries=0,
            )
        artifact = [payload for kind, payload in events if kind == "result"][0]
        self.assertEqual(artifact["questions"][0]["failure"]["code"], "input_too_large")
        direct.assert_not_called()

    def test_query_exposes_only_short_ids_and_merges_same_player_texts(self):
        question = {
            "question": "Q1", "audience": "", "respondent_count": 1,
            "model_sources": [{
                "response_id": "r0001",
                "responses": ["第一条", "第二条"],
            }],
        }
        query = build_findings_query(question, {"problem": "理解流失原因"})
        self.assertIn("r0001", query)
        self.assertIn("第一条", query)
        self.assertIn("第二条", query)
        self.assertNotIn("ir3v1-", query)
        self.assertIn("大量 / 很多：本题不可用", query)


if __name__ == "__main__":
    unittest.main()
