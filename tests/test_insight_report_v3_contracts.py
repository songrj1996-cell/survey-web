import unittest

from app.services.insight_report_v3.contracts import (
    FORBIDDEN_MODEL_OUTPUT_FIELDS,
    MODEL_OUTPUT_FIELD_WHITELISTS,
    build_stage_source_contract,
    hydrate_findings,
    validate_findings_payload,
)
from scripts.replay_insight_report_v3_findings import _columns_for_auto_confirmation


def _plan():
    return {
        "columns": [
            {"index": 0, "name": "玩家 ID", "role": "id"},
            {"index": 1, "name": "主要痛点", "role": "open_text"},
            {"index": 2, "name": "满意度", "role": "scale"},
        ],
        "parts": [
            {"name": "体验", "column_indexes": [1, 2]},
        ],
    }


def _open_text():
    return {
        1: [
            {"respondent_key": "player=A", "text": "队友不协同"},
            {"respondent_key": "player=A", "text": "失败时只怪打野"},
            {"respondent_key": "player=B", "text": "压力很大"},
            {"respondent_key": "player=C", "text": "希望有更多引导"},
        ],
        # Even if a caller accidentally includes non-open material, it cannot
        # become an Insight v3 question.
        2: [{"respondent_key": "player=A", "text": "其他补充"}],
    }


class InsightReportV3ContractTests(unittest.TestCase):
    def test_auto_confirmation_promotes_only_inferred_profile_branch_parent(self):
        columns = [
            {"index": 0, "role": "profile_dim", "options": ["A", "B"]},
            {"index": 1, "role": "profile_dim", "options": ["X", "Y"]},
            {"index": 2, "role": "open_text"},
        ]
        confirmed = _columns_for_auto_confirmation(
            columns,
            [{"parent_index": 1, "allowed_options": ["X"]}],
        )
        self.assertEqual(confirmed[0]["role"], "profile_dim")
        self.assertEqual(confirmed[1]["role"], "single_choice")
        self.assertEqual(columns[1]["role"], "profile_dim")

    def test_model_output_field_whitelists_exclude_raw_text_fields(self):
        for fields in MODEL_OUTPUT_FIELD_WHITELISTS.values():
            self.assertTrue(fields.isdisjoint(FORBIDDEN_MODEL_OUTPUT_FIELDS))

    def test_forbidden_quote_field_is_rejected(self):
        payload = {
            "findings": [{
                "id": "f1", "claim": "有压力", "kind": "pain",
                "frequency_tier": "有玩家", "support_count": 1,
                "response_ids": ["r0001"], "caveat": "", "quote": "原文",
            }]
        }
        error = validate_findings_payload(
            payload, valid_short_ids={"r0001"}, respondent_total=1,
        )
        self.assertIn("禁止", error)

    def test_source_contract_groups_same_player_before_model(self):
        contract = build_stage_source_contract(
            open_text=_open_text(),
            plan=_plan(),
            headers=["玩家 ID", "主要痛点", "满意度"],
            file_sha256="a" * 64,
        )
        self.assertEqual(len(contract["questions"]), 1)
        question = contract["questions"][0]
        self.assertEqual(question["respondent_count"], 3)
        self.assertEqual(
            [item["response_id"] for item in question["model_sources"]],
            ["r0001", "r0002", "r0003"],
        )
        self.assertEqual(len(question["model_sources"][0]["responses"]), 2)
        self.assertEqual(len(question["short_id_map"]["r0001"]), 2)
        self.assertTrue(
            all(source_id.startswith("ir3v1-") for source_id in question["all_source_ids"])
        )
        self.assertNotIn("r0001", contract["source_registry"])

    def test_short_ids_are_validated_then_hydrated_to_global_ids(self):
        contract = build_stage_source_contract(
            open_text=_open_text(), plan=_plan(), headers=[], file_sha256="b" * 64,
        )
        question = contract["questions"][0]
        payload = {
            "findings": [{
                "id": "f1",
                "claim": "部分玩家感到压力",
                "kind": "pain",
                "frequency_tier": "少数",
                "support_count": 3,
                "response_ids": ["r0001", "r0002"],
                "caveat": "仅代表本题",
            }]
        }
        self.assertIsNone(validate_findings_payload(
            payload,
            valid_short_ids=set(question["short_id_map"]),
            respondent_total=question["respondent_count"],
        ))
        hydrated = hydrate_findings(
            payload,
            scope_key=question["scope_key"],
            short_id_map=question["short_id_map"],
            source_registry=contract["source_registry"],
        )
        self.assertEqual(hydrated[0]["representative_respondent_count"], 2)
        self.assertEqual(len(hydrated[0]["response_ids"]), 3)
        self.assertTrue(all(item.startswith("ir3v1-") for item in hydrated[0]["response_ids"]))

    def test_unknown_short_id_is_rejected_without_copying_global_id(self):
        payload = {
            "findings": [{
                "id": "f1", "claim": "有压力", "kind": "pain",
                "frequency_tier": "有玩家", "support_count": 1,
                "response_ids": ["ir3v1-long-id"], "caveat": "",
            }]
        }
        error = validate_findings_payload(
            payload, valid_short_ids={"r0001"}, respondent_total=1,
        )
        self.assertIn("不存在的短 ID", error)


if __name__ == "__main__":
    unittest.main()
