import unittest

from app.services.insight_report_v3.profiles import (
    attach_profile_presence,
    evaluate_skew,
    filter_group_sources,
    profile_catalog,
    validate_profile_language,
)


def _plan():
    return {
        "columns": [
            {
                "index": 0,
                "name": "性别",
                "role": "single_choice",
                "use_as_profile": True,
                "profile_scope": "analysis",
            },
            {
                "index": 1,
                "name": "熟练英雄",
                "role": "open_text",
                "use_as_profile": True,
                "profile_scope": "label",
            },
            {"index": 2, "name": "体验", "role": "open_text"},
        ],
        "parts": [{"name": "体验", "column_indexes": [2]}],
    }


def _registry(total=20):
    result = {}
    for index in range(total):
        response_id = f"global-{index + 1}"
        result[response_id] = {
            "response_id": response_id,
            "column_index": 2,
            "source_index": index + 1,
            "respondent_ref": f"player-{index + 1}",
            "profile": {
                "性别": "女性" if index < total // 2 else "男性",
                "熟练英雄": "Layla",
            },
            "text": f"反馈 {index + 1}",
        }
    # 同一玩家的第二条开放题来源，用于验证按 respondent_ref 去重。
    result["global-1b"] = {
        **result["global-1"],
        "response_id": "global-1b",
        "source_index": 21,
        "text": "同一玩家补充反馈",
    }
    return result


class InsightV3ProfileTests(unittest.TestCase):
    def test_profile_precision_gate_does_not_confuse_hero_counts_with_people(self):
        catalog = [{"profile_name": "游玩MLBB时长", "values": ["超过3年", "1至3年"]}]
        self.assertIsNone(validate_profile_language(
            "超过3年的玩家通常只熟悉4个发育路英雄，因此玩家会优先选择舒适英雄。",
            findings=[{"skewed_toward": []}],
            catalog=catalog,
        ))

    def test_profile_precision_gate_still_rejects_direct_group_counts(self):
        catalog = [{"profile_name": "性别", "values": ["女性", "男性"]}]
        for text in ("7名女性玩家提到压力", "女性玩家中有7人提到压力", "有7名玩家提到压力"):
            with self.subTest(text=text):
                self.assertIn("精确人数", validate_profile_language(
                    text,
                    findings=[{"skewed_toward": []}],
                    catalog=catalog,
                ))

    def test_presence_uses_representative_ids_and_analysis_scope_only(self):
        artifact = {
            "source_registry": _registry(),
            "questions": [{
                "findings": [{
                    "id": "2/f1",
                    "response_ids": ["global-1", "global-1b", "global-12"],
                }],
            }],
        }
        hydrated = attach_profile_presence(artifact, plan=_plan())
        presence = hydrated["questions"][0]["findings"][0]["profile_presence"]
        self.assertEqual(presence, [{
            "profile_name": "性别",
            "values": ["女性", "男性"],
            "basis": "representative_response_ids_only",
        }])
        self.assertNotIn("熟练英雄", str(presence))

    def test_group_filter_returns_all_sources_for_matching_players(self):
        records = filter_group_sources(
            _registry(),
            plan=_plan(),
            profile_name="性别",
            profile_value="女性",
        )
        self.assertEqual(len(records), 11)
        self.assertEqual({item["respondent_ref"] for item in records}, {
            f"player-{index}" for index in range(1, 11)
        })

    def test_skew_requires_every_conservative_threshold(self):
        registry = _registry()
        passed = evaluate_skew(
            scope_response_ids=[f"global-{index}" for index in range(1, 21)],
            supporter_response_ids=[
                *[f"global-{index}" for index in range(1, 8)],
                "global-11",
            ],
            source_registry=registry,
            plan=_plan(),
            profile_name="性别",
            profile_value="女性",
        )
        self.assertEqual(passed["status"], "verified")
        self.assertEqual(passed["skewed_toward"], {
            "profile_name": "性别",
            "profile_value": "女性",
            "supporter_count": 7,
        })
        self.assertIn("supporter_share", passed["diagnostics"])
        self.assertNotIn("supporter_share", passed["skewed_toward"])

        failed = evaluate_skew(
            scope_response_ids=[f"global-{index}" for index in range(1, 21)],
            supporter_response_ids=["global-1", "global-2", "global-3", "global-11"],
            source_registry=registry,
            plan=_plan(),
            profile_name="性别",
            profile_value="女性",
        )
        self.assertEqual(failed["status"], "not_skewed")
        self.assertIsNone(failed["skewed_toward"])

    def test_writer_gate_rejects_unverified_difference_and_profile_precision(self):
        catalog = profile_catalog(_registry(), _plan())
        finding = {"skewed_toward": []}
        self.assertIn(
            "没有 skewed_toward",
            validate_profile_language(
                "该视角在女性玩家中更突出。",
                findings=[finding],
                catalog=catalog,
            ),
        )
        verified = {
            "skewed_toward": [{
                "profile_name": "性别",
                "profile_value": "女性",
                "supporter_count": 7,
            }],
        }
        self.assertIsNone(validate_profile_language(
            "该视角在女性玩家中更突出。",
            findings=[verified],
            catalog=catalog,
        ))
        self.assertIn("百分比", validate_profile_language(
            "女性玩家占 70%，反馈更集中。",
            findings=[verified],
            catalog=catalog,
        ))
        self.assertIn("精确人数", validate_profile_language(
            "该视角在7名女性玩家中更突出。",
            findings=[verified],
            catalog=catalog,
        ))
        self.assertIn("精确人数", validate_profile_language(
            "女性玩家中有7人明确提及压力。",
            findings=[verified],
            catalog=catalog,
        ))


if __name__ == "__main__":
    unittest.main()
