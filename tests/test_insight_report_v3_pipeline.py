from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from app.services.insight_report_v3 import pipeline
from app.services.insight_report_v3.contracts import (
    FINDINGS_SCHEMA_VERSION,
    build_stage_source_contract,
)


def _plan():
    return {
        "columns": [
            {"index": 0, "name": "玩家 ID", "role": "id"},
            {"index": 1, "name": "体验反馈", "role": "open_text"},
        ],
        "parts": [{"name": "体验", "column_indexes": [1]}],
        "analysis_focus": {"report_organization": "围绕体验阻力组织"},
    }


def _open_text():
    return {
        1: [{
            "respondent_key": "player=A",
            "text": "失败时会有压力",
            "ids": {"玩家 ID": "A"},
            "profile": {},
            "segments": {},
        }],
    }


def _completed_findings():
    contract = build_stage_source_contract(
        open_text=_open_text(),
        plan=_plan(),
        headers=["玩家 ID", "体验反馈"],
        file_sha256="a" * 64,
    )
    source_id = contract["questions"][0]["all_source_ids"][0]
    artifact = {
        "schema_version": FINDINGS_SCHEMA_VERSION,
        "source_binding": deepcopy(contract["source_binding"]),
        "source_registry": deepcopy(contract["source_registry"]),
        "questions": [{
            "question_key": "1",
            "scope_key": "1",
            "question": "体验反馈",
            "audience": "",
            "column_indexes": [1],
            "status": "complete",
            "findings": [{
                "id": "1/f1",
                "claim": "失败归因带来压力",
                "kind": "pain",
                "frequency_tier": "有玩家",
                "support_count": 1,
                "response_ids": [source_id],
                "representative_respondent_count": 1,
                "support_basis": "unique_respondents_model_estimate",
                "caveat": "仅代表本题",
            }],
            "diagnostics": {"logical_attempts": 1, "upstream_attempts": 1},
        }],
        "diagnostics": {
            "stop_reason": "all_questions_finished",
            "status_counts": {"complete": 1, "degraded": 0, "skipped": 0},
        },
    }
    return artifact, contract


class InsightV3PipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_findings_reuse_requires_entire_versioned_source_binding(self):
        artifact, contract = _completed_findings()
        self.assertTrue(pipeline._can_reuse_findings(artifact, contract))

        changed = deepcopy(artifact)
        changed["source_binding"]["findings_prompt_version"] = "old-prompt"
        self.assertFalse(pipeline._can_reuse_findings(changed, contract))

        changed = deepcopy(artifact)
        changed["source_binding"]["frequency_rules_version"] = "old-rules"
        self.assertFalse(pipeline._can_reuse_findings(changed, contract))

    async def test_reused_findings_flow_emits_v3_phases_and_fixed_sections(self):
        artifact, _contract = _completed_findings()
        source_id = next(iter(artifact["source_registry"]))

        async def fake_plan_chapters(**_kwargs):
            return (
                {
                    "report_title": "玩家体验洞察",
                    "chapters": [{
                        "id": "c01",
                        "title": "压力与协作",
                        "purpose": "解释体验阻力",
                        "finding_ids": ["1/f1"],
                        "stat_ids": [],
                        "group_focus": None,
                    }],
                    "suspected_skews": [],
                },
                {
                    "status": "complete",
                    "logical_attempts": 1,
                    "attempts": [],
                    "models": ["mock-plan"],
                },
            )

        async def fake_write_chapters(**_kwargs):
            chapter = {
                "id": "c01",
                "title": "压力与协作",
                "purpose": "解释体验阻力",
                "finding_ids": ["1/f1"],
                "stat_ids": [],
                "group_focus": None,
                "blocks": [{
                    "id": "b1",
                    "heading": "失败归因形成压力",
                    "body": "队友协作不足会放大承担责任的压力。",
                    "finding_ids": ["1/f1"],
                    "evidence": [{
                        "response_id": source_id,
                        "translation_zh": "失败时经常只怪我。",
                        "translation_status": "source_zh",
                    }],
                }],
                "diagnostics": {
                    "status": "complete",
                    "logical_attempts": 1,
                    "attempts": [],
                    "models": ["mock-writer"],
                },
            }
            yield ("chapter_result", deepcopy(chapter))
            yield ("result", [chapter])

        async def fake_core(**_kwargs):
            return {
                "blocks": [{
                    "id": "core-1",
                    "heading": "责任压力是主要阻力",
                    "body": "失败归因会削弱继续学习的意愿。",
                    "finding_ids": ["1/f1"],
                    "evidence": [{
                        "response_id": source_id,
                        "translation_zh": "失败时经常只怪我。",
                        "translation_status": "source_zh",
                    }],
                }],
                "diagnostics": {
                    "status": "complete",
                    "logical_attempts": 1,
                    "attempts": [],
                    "models": ["mock-core"],
                },
            }

        stage_one = Mock(side_effect=AssertionError("compatible findings must be reused"))
        with (
            patch.object(pipeline, "generate_insight_findings", new=stage_one),
            patch.object(pipeline, "plan_chapters", new=fake_plan_chapters),
            patch.object(pipeline, "write_chapters", new=fake_write_chapters),
            patch.object(pipeline, "write_core_findings", new=fake_core),
        ):
            events = [
                event
                async for event in pipeline.generate_insight_report_v3(
                    open_text=_open_text(),
                    plan=_plan(),
                    headers=["玩家 ID", "体验反馈"],
                    file_sha256="a" * 64,
                    stats_md="<metadata>总样本: 1, 有效样本: 1</metadata>",
                    structured_tables=[],
                    qualitative_context={"problem": "理解体验阻力"},
                    existing_findings=artifact,
                    translation_namespace="test:v3",
                )
            ]

        stage_one.assert_not_called()
        progress = [payload for kind, payload in events if kind == "analysis_progress"]
        self.assertEqual(
            {(item["phase"], item["phase_index"], item["phase_total"]) for item in progress},
            {
                ("themes", 1, 4),
                ("writing", 2, 4),
                ("audit", 3, 4),
            },
        )
        self.assertFalse(any(item["phase"] == "finalize" for item in progress))
        self.assertTrue(any(item.get("reused") for item in progress))

        final_artifacts = [payload for kind, payload in events if kind == "artifacts"][-1]
        self.assertTrue(final_artifacts["viewpoint_diagnostics"]["findings_reused"])
        self.assertEqual(
            final_artifacts["viewpoint_diagnostics"]["chapters"][0]["status"],
            "complete",
        )
        report = next(payload for kind, payload in events if kind == "result")
        self.assertIn("## 一、调研背景", report)
        self.assertIn("## 二、样本概况", report)
        self.assertIn("## 三、核心发现", report)
        self.assertIn("## 四、压力与协作", report)
        self.assertIn("> 代表反馈：失败时经常只怪我。", report)


if __name__ == "__main__":
    unittest.main()
