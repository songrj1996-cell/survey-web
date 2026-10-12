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
    async def test_partial_block_failure_is_visible_and_recovery_diagnostics_survive(self):
        from unittest.mock import AsyncMock
        artifact, _ = _completed_findings()
        recovery = {"valid_block_ids": ["b1"], "unresolved_block_ids": ["b2"], "stop_reason": "partial_repair_no_progress"}
        chapter = {"id": "c01", "title": "体验", "purpose": "解释", "finding_ids": ["1/f1"], "stat_ids": [], "group_focus": None}
        written = {**chapter, "blocks": [
            {"id": "b1", "heading": "保留的判断", "body": "失败后的责任归属影响体验。", "finding_ids": ["1/f1"], "evidence": []},
            {"id": "b2", "heading": "本段分析尚未完成", "body": "本段未通过证据与表述校验。", "finding_ids": [], "evidence": [], "degraded": True},
        ], "diagnostics": {"status": "degraded", "logical_attempts": 2, "attempts": [{"attempt": 2, "block_recovery": recovery}], "models": [], "reason": "partial_repair_no_progress"}}
        async def write(**kwargs):
            yield "chapter_result", deepcopy(written)
            yield "result", [deepcopy(written)]
        async def audit(**kwargs):
            return {"core_blocks": kwargs["core_blocks"], "chapters": kwargs["chapters"], "unresolved": [], "diagnostics": {"status": "passed"}}
        with patch.object(pipeline, "plan_chapters", new=AsyncMock(return_value=({"report_title": "体验", "chapters": [chapter], "suspected_skews": []}, {"status": "complete"}))), patch.object(
            pipeline, "write_chapters", new=write,
        ), patch.object(pipeline, "write_core_findings", new=AsyncMock(return_value={"blocks": [], "diagnostics": {"status": "complete"}})), patch.object(
            pipeline, "write_actions", new=AsyncMock(return_value={"actions": [], "diagnostics": {"status": "complete"}}),
        ), patch.object(pipeline, "audit_report_sections", new=audit):
            events = [event async for event in pipeline.generate_insight_report_v3(
                open_text=_open_text(), plan=_plan(), headers=["玩家 ID", "体验反馈"], file_sha256="a" * 64,
                stats_md="<metadata>总样本: 1, 有效样本: 1</metadata>", structured_tables=[],
                qualitative_context={}, existing_findings=artifact, translation_namespace="isolated-partial",
            )]
        final = [value for kind, value in events if kind == "artifacts"][-1]
        diagnostic = final["viewpoint_diagnostics"]["chapters"][0]
        self.assertEqual(diagnostic["status"], "degraded")
        self.assertEqual(diagnostic["attempts"][-1]["block_recovery"], recovery)
        report = next(value for kind, value in events if kind == "result")
        self.assertIn("保留的判断", report)
        self.assertIn("本段分析尚未完成", report)

    def test_question_progress_matches_legacy_player_contract(self):
        fields = pipeline._question_progress_fields(
            {
                "scope_key": "part_1_col_1",
                "status": "complete",
            },
            {
                "scope_key": "part_1_col_1",
                "part_index": 1,
                "question": "失败体验",
                "audience": "仅不满意玩家",
                "respondent_count": 15,
            },
            {
                "parts": [{"name": "核心体验", "column_indexes": [1]}],
            },
        )

        self.assertEqual(fields, {
            "status": "completed",
            "step": "completed",
            "question_status": "complete",
            "scope_key": "part_1_col_1",
            "part_index": 1,
            "part_name": "核心体验",
            "question_name": "失败体验",
            "audience": "仅不满意玩家",
            "respondent_count": 15,
            "count_unit": "players",
        })

    def test_question_progress_uses_unpartitioned_default_and_terminal_status(self):
        fields = pipeline._question_progress_fields(
            {"scope_key": "9", "status": "skipped"},
            {
                "part_index": 0,
                "question": "补充反馈",
                "audience": "",
                "respondent_count": 0,
            },
            _plan(),
        )

        self.assertEqual(fields["status"], "skipped")
        self.assertEqual(fields["part_name"], "未分章开放题")
        self.assertEqual(fields["count_unit"], "players")

    def test_findings_reuse_requires_entire_versioned_source_binding(self):
        artifact, contract = _completed_findings()
        self.assertTrue(pipeline._can_reuse_findings(artifact, contract))

        changed = deepcopy(artifact)
        changed["source_binding"]["findings_prompt_version"] = "old-prompt"
        self.assertFalse(pipeline._can_reuse_findings(changed, contract))

        changed = deepcopy(artifact)
        changed["source_binding"]["frequency_rules_version"] = "old-rules"
        self.assertFalse(pipeline._can_reuse_findings(changed, contract))

    def test_findings_reuse_allows_focus_only_plan_change_when_inputs_match(self):
        artifact, baseline = _completed_findings()
        revised_plan = deepcopy(_plan())
        revised_plan["analysis_focus"] = {
            "core_question": "哪些体验阻力最重要",
            "report_organization": "先阻力后机会",
            "supporting_analyses": ["开放题场景"],
            "evidence_role": "原话支撑判断",
            "expected_deliverables": ["阻力清单"],
            "avoid_structures": ["逐题罗列"],
        }
        revised = build_stage_source_contract(
            open_text=_open_text(),
            plan=revised_plan,
            headers=["玩家 ID", "体验反馈"],
            file_sha256="a" * 64,
        )

        self.assertNotEqual(
            baseline["source_binding"]["plan_fingerprint"],
            revised["source_binding"]["plan_fingerprint"],
        )
        self.assertEqual(
            baseline["source_binding"]["findings_input_fingerprint"],
            revised["source_binding"]["findings_input_fingerprint"],
        )
        self.assertFalse(pipeline._can_reuse_findings(artifact, revised))
        self.assertTrue(pipeline._can_reuse_findings(
            artifact,
            revised,
            baseline_source_contract=baseline,
        ))

        changed_text = _open_text()
        changed_text[1][0]["text"] = "完全不同的反馈"
        changed = build_stage_source_contract(
            open_text=changed_text,
            plan=revised_plan,
            headers=["玩家 ID", "体验反馈"],
            file_sha256="a" * 64,
        )
        self.assertFalse(pipeline._can_reuse_findings(
            artifact,
            changed,
            baseline_source_contract=baseline,
        ))

    async def test_reused_findings_flow_emits_v3_phases_and_fixed_sections(self):
        artifact, _contract = _completed_findings()
        source_id = next(iter(artifact["source_registry"]))

        async def fake_plan_chapters(**_kwargs):
            self.assertEqual(_kwargs["report_instruction"], "先写阻力，再写机会")
            return (
                {
                    "report_title": "玩家体验洞察",
                    "chapters": [{
                        "id": "c01",
                        "title": "压力与协作",
                        "purpose": "解释体验阻力",
                        "finding_ids": ["1/f1"],
                        "stat_ids": [],
                        "group_focus": {
                            "profile_name": "性别",
                            "profile_value": "女性",
                        },
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
            self.assertEqual(_kwargs["report_instruction"], "用更直接的业务语言")
            self.assertTrue(all(
                chapter.get("group_focus") is None
                for chapter in _kwargs["chapters"]
            ))
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
                        "evidence_point": "失败后被队友归责会放大压力",
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
            self.assertEqual(_kwargs["report_instruction"], "用更直接的业务语言")
            return {
                "blocks": [{
                    "id": "core-1",
                    "heading": "责任压力是主要阻力",
                    "body": "失败归因会削弱继续学习的意愿。",
                    "finding_ids": ["1/f1"],
                    "evidence": [{
                        "response_id": source_id,
                        "evidence_point": "失败后被队友归责会削弱学习意愿",
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

        async def fake_actions(**_kwargs):
            self.assertEqual(_kwargs["report_instruction"], "用更直接的业务语言")
            return {
                "actions": [{
                    "id": "a01",
                    "title": "降低失败后的单人归责",
                    "action_scope": "general",
                    "target_group": None,
                    "problem": "失败责任集中会放大压力",
                    "objective": "让协作与责任来源更容易被理解",
                    "design_directions": ["补充协作状态反馈"],
                    "validation_plan": "结合试用反馈核对责任理解是否改善",
                    "finding_ids": ["1/f1"],
                    "evidence_gap": "",
                    "required_data": "",
                }],
                "chapter_note": "",
                "diagnostics": {
                    "status": "complete",
                    "logical_attempts": 1,
                    "attempts": [],
                    "models": ["mock-actions"],
                    "action_count": 1,
                    "general_action_count": 1,
                    "group_action_count": 0,
                },
            }

        async def fake_audit(**kwargs):
            self.assertEqual(kwargs["actions"][0]["id"], "a01")
            repaired_actions = deepcopy(kwargs["actions"])
            repaired_actions[0]["title"] = "已核对来源的协作建议"
            return {
                "core_blocks": deepcopy(kwargs["core_blocks"]),
                "chapters": deepcopy(kwargs["chapters"]),
                "actions": repaired_actions,
                "unresolved": [],
                "diagnostics": {
                    "schema_version": "insight-audit-v1",
                    "status": "passed",
                    "stop_reason": "all_claims_supported",
                    "logical_call_count": 1,
                    "upstream_attempt_count": 1,
                    "unresolved_count": 0,
                    "models": ["mock-audit"],
                    "elapsed_seconds": 0.1,
                    "warned": False,
                },
            }

        stage_one = Mock(side_effect=AssertionError("compatible findings must be reused"))
        group_extractor = Mock(
            side_effect=AssertionError("missing focus must not run group extraction")
        )
        with (
            patch.object(pipeline, "generate_insight_findings", new=stage_one),
            patch.object(pipeline, "plan_chapters", new=fake_plan_chapters),
            patch.object(pipeline, "extract_group_findings", new=group_extractor),
            patch.object(pipeline, "write_chapters", new=fake_write_chapters),
            patch.object(pipeline, "write_core_findings", new=fake_core),
            patch.object(pipeline, "write_actions", new=fake_actions),
            patch.object(pipeline, "audit_report_sections", new=fake_audit),
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
                    report_instruction="用更直接的业务语言",
                    planning_instruction="先写阻力，再写机会",
                    regeneration_diagnostics={
                        "status": "plan_revised",
                        "logical_calls": 2,
                        "models": ["mock-regeneration"],
                    },
                    translation_namespace="test:v3",
                )
            ]

        stage_one.assert_not_called()
        group_extractor.assert_not_called()
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
            final_artifacts["viewpoint_diagnostics"]["regeneration"]["status"],
            "plan_revised",
        )
        self.assertIn("mock-regeneration", final_artifacts["writer_models_used"])
        self.assertTrue(all(
            chapter.get("group_focus") is None
            for chapter in final_artifacts["insight_v3_chapter_plan"]["chapters"]
        ))
        self.assertEqual(final_artifacts["viewpoint_diagnostics"]["group_chapters"], {})
        self.assertTrue(
            final_artifacts["viewpoint_diagnostics"]["analysis_focus"]["fallback_used"]
        )
        self.assertEqual(
            len(final_artifacts["viewpoint_diagnostics"]["analysis_focus"]
                ["rejected_unrequested_group_chapters"]),
            1,
        )
        self.assertEqual(
            final_artifacts["viewpoint_diagnostics"]["chapters"][0]["status"],
            "complete",
        )
        self.assertEqual(final_artifacts["viewpoint_diagnostics"]["actions"]["action_count"], 1)
        self.assertEqual(
            final_artifacts["viewpoint_diagnostics"]["actions"]["group_action_count"],
            0,
        )
        self.assertEqual(final_artifacts["viewpoint_diagnostics"]["evidence_index"]["entry_count"], 1)
        self.assertEqual(final_artifacts["viewpoint_diagnostics"]["audit"]["status"], "passed")
        self.assertIn("frequency_review", final_artifacts["viewpoint_diagnostics"])
        self.assertEqual(
            final_artifacts["viewpoint_diagnostics"]["analysis_focus"]["message"],
            "未提供分析主线，章节按问卷结构组织，未规划人群专章",
        )
        report = next(payload for kind, payload in events if kind == "result")
        self.assertIn("## 一、调研背景", report)
        self.assertIn("## 二、样本概况", report)
        self.assertIn("## 三、核心发现", report)
        self.assertIn("## 四、压力与协作", report)
        self.assertIn("## 五、行动建议", report)
        self.assertIn("已核对来源的协作建议", report)
        self.assertNotIn("建议一：降低失败后的单人归责", report)
        self.assertNotIn("**证据要点：**", report)
        self.assertIn("## 附录 A：完整证据索引", report)
        self.assertEqual(report.count("失败时经常只怪我。"), 1)
        self.assertNotIn("代表反馈", report)

    async def test_cross_chapter_groups_extract_once_per_group_and_write_once_per_theme(self):
        await self._check_cross_chapter_groups()

    async def test_missing_group_falls_back_only_affected_chapter(self):
        await self._check_cross_chapter_groups(missing_group=True)

    async def test_group_diagnostics_follow_writer_fallback_not_extraction_success(self):
        await self._check_cross_chapter_groups(writer_fallback=True)

    async def test_group_diagnostics_reflect_global_plan_failure(self):
        await self._check_cross_chapter_groups(plan_degraded=True)

    async def test_group_diagnostics_preserve_partial_group_output(self):
        await self._check_cross_chapter_groups(partial_group=True)

    async def test_ordinary_block_failure_does_not_erase_two_valid_groups(self):
        await self._check_cross_chapter_groups(ordinary_failure=True)

    async def _check_cross_chapter_groups(
        self, *, missing_group=False, writer_fallback=False, plan_degraded=False,
        partial_group=False, ordinary_failure=False,
    ):
        artifact, _contract = _completed_findings()
        source_id = next(iter(artifact["source_registry"]))
        second = deepcopy(artifact["questions"][0]["findings"][0])
        second.update({
            "id": "1/f2",
            "claim": "学习反馈存在理解门槛",
            "kind": "opportunity",
        })
        artifact["questions"][0]["findings"].append(second)
        groups = [
            {"profile_name": "语言", "profile_value": "英语"},
            {"profile_name": "语言", "profile_value": "印尼语"},
        ]
        extraction_calls = []
        writing_calls = []

        def group_finding(group, suffix):
            value = group["profile_value"]
            slug = "en" if value == "英语" else "id"
            return {
                "id": f"group:{slug}/g{suffix}",
                "claim": f"{value}玩家的主题 {suffix} 发现",
                "kind": "pain",
                "frequency_tier": "多道题均有提及",
                "support_count": 1,
                "response_ids": [source_id],
                "representative_respondent_count": 1,
                "support_basis": "code_filtered_group_qualitative_extraction",
                "caveat": "",
                "profile_presence": [{
                    "profile_name": "语言",
                    "values": [value],
                    "basis": "code_filtered_group_all_open_text",
                }],
                "skewed_toward": [],
                "question_key": f"group:{slug}",
                "question": f"语言={value} 人群全部开放题",
                "audience": f"语言：{value}",
                "degraded": False,
            }

        async def fake_extract_group_findings(**kwargs):
            group = deepcopy(kwargs["chapter"]["group_focus"])
            extraction_calls.append(group)
            return (
                [group_finding(group, 1), group_finding(group, 2)],
                {
                    "status": "complete",
                    "logical_attempts": 1,
                    "attempts": [],
                    "models": ["mock-group"],
                },
            )

        async def fake_plan_chapters(**kwargs):
            self.assertEqual(kwargs["cross_chapter_groups"], groups)
            self.assertEqual(len(kwargs["findings"]), 6)
            return (
                {
                    "report_title": "双语玩家体验洞察",
                    "chapters": [
                        {
                            "id": "c01",
                            "title": "关键阻力",
                            "purpose": "分别呈现两组玩家的阻力",
                            "finding_ids": ["1/f1", "group:en/g1", "group:id/g1"]
                            + (["group:id/g2"] if missing_group else []),
                            "stat_ids": [],
                            "group_focus": None,
                        },
                        {
                            "id": "c02",
                            "title": "机会方向",
                            "purpose": "分别呈现两组玩家的机会",
                            "finding_ids": ["1/f2", "group:en/g2"]
                            + ([] if missing_group else ["group:id/g2"]),
                            "stat_ids": [],
                            "group_focus": None,
                        },
                    ],
                    "suspected_skews": [],
                },
                {
                    "status": "complete",
                    "logical_attempts": 1,
                    "attempts": [],
                    "models": ["mock-plan"],
                    **({"status": "degraded"} if plan_degraded else {}),
                },
            )

        async def fake_write_chapters(**kwargs):
            self.assertEqual(len(kwargs["chapters"]), 2)
            results = []
            for chapter in kwargs["chapters"]:
                writing_calls.append(chapter["id"])
                local_fallback = plan_degraded or (missing_group and chapter["id"] == "c02")
                if local_fallback:
                    self.assertNotIn("cross_group_focus", chapter)
                    self.assertEqual(chapter["finding_ids"], ["1/f1" if chapter["id"] == "c01" else "1/f2"])
                if local_fallback or (writer_fallback and chapter["id"] == "c02"):
                    written = deepcopy(chapter)
                    written.pop("cross_group_focus", None)
                    written["finding_ids"] = ["1/f1" if chapter["id"] == "c01" else "1/f2"]
                    written["blocks"] = [{
                        "id": chapter["id"] + "-ordinary", "heading": "共同阻力",
                        "body": "玩家提到该主题。", "finding_ids": written["finding_ids"], "evidence": [],
                    }]
                    written["diagnostics"] = {"status": "complete" if local_fallback else "degraded"}
                    if not local_fallback:
                        written["cross_group_preflight"].update({
                            "status": "ordinary_fallback", "reason": "writer_retry_exhausted",
                        })
                    results.append(written)
                    yield ("chapter_result", deepcopy(written))
                    continue
                self.assertEqual(chapter["cross_group_focus"], groups)
                written = {
                    **deepcopy(chapter),
                    "blocks": [{
                        "id": f"{chapter['id']}-en",
                        "heading": "英语玩家视角",
                        "body": "英语玩家提到该主题。",
                        "finding_ids": [next(
                            item for item in chapter["finding_ids"]
                            if item.startswith("group:en/")
                        )],
                        "evidence": [],
                    }, {
                        "id": f"{chapter['id']}-id",
                        "heading": "印尼语玩家视角",
                        "body": "印尼语玩家提到该主题。",
                        "finding_ids": [next(
                            item for item in chapter["finding_ids"]
                            if item.startswith("group:id/")
                        )],
                        "evidence": [],
                    }],
                    "diagnostics": {
                        "status": "complete",
                        "logical_attempts": 1,
                        "attempts": [],
                        "models": ["mock-writer"],
                    },
                }
                if chapter["id"] == "c02" and (partial_group or ordinary_failure):
                    if partial_group:
                        written["blocks"][1].update({"degraded": True, "heading": "本段分析尚未完成", "body": "暂不形成结论"})
                    else:
                        written["blocks"].append({"id": "c02-gap", "body": "暂不形成结论", "finding_ids": ["1/f2"], "degraded": True})
                    written["diagnostics"]["status"] = "degraded"
                    written["diagnostics"]["attempts"] = [{"block_recovery": {
                        "valid_block_ids": [b["id"] for b in written["blocks"] if not b.get("degraded")],
                        "unresolved_block_ids": [b["id"] for b in written["blocks"] if b.get("degraded")],
                    }}]
                results.append(written)
                yield ("chapter_result", deepcopy(written))
            yield ("result", results)

        async def fake_core(**_kwargs):
            return {
                "blocks": [{
                    "id": "core-1",
                    "heading": "共同主题",
                    "body": "两组材料均围绕具体体验展开。",
                    "finding_ids": ["1/f1"],
                    "evidence": [],
                }],
                "diagnostics": {
                    "status": "complete",
                    "logical_attempts": 1,
                    "attempts": [],
                    "models": ["mock-core"],
                },
            }

        async def fake_actions(**_kwargs):
            return {
                "actions": [],
                "chapter_note": "",
                "diagnostics": {
                    "status": "complete",
                    "logical_attempts": 1,
                    "attempts": [],
                    "models": ["mock-actions"],
                    "action_count": 0,
                    "general_action_count": 0,
                    "group_action_count": 0,
                },
            }

        async def fake_audit(**kwargs):
            return {
                "core_blocks": deepcopy(kwargs["core_blocks"]),
                "chapters": deepcopy(kwargs["chapters"]),
                "unresolved": [],
                "diagnostics": {
                    "status": "passed",
                    "stop_reason": "all_claims_supported",
                    "logical_call_count": 1,
                    "upstream_attempt_count": 1,
                    "unresolved_count": 0,
                    "models": ["mock-audit"],
                    "elapsed_seconds": 0.1,
                    "warned": False,
                },
            }

        with (
            patch.object(pipeline, "generate_insight_findings", new=Mock(
                side_effect=AssertionError("compatible findings must be reused")
            )),
            patch.object(pipeline, "extract_group_findings", new=fake_extract_group_findings),
            patch.object(pipeline, "plan_chapters", new=fake_plan_chapters),
            patch.object(pipeline, "write_chapters", new=fake_write_chapters),
            patch.object(pipeline, "write_core_findings", new=fake_core),
            patch.object(pipeline, "write_actions", new=fake_actions),
            patch.object(pipeline, "audit_report_sections", new=fake_audit),
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
                    qualitative_context={"problem": "比较双语玩家体验"},
                    existing_findings=artifact,
                    regeneration_diagnostics={
                        "status": "plan_revised",
                        "cross_chapter_requested": True,
                        "cross_chapter_groups": deepcopy(groups),
                        "logical_calls": 2,
                        "models": ["mock-regeneration"],
                    },
                    translation_namespace="test:v3-cross-groups",
                )
            ]

        self.assertCountEqual(extraction_calls, groups)
        self.assertEqual(writing_calls, ["c01", "c02"])
        final_artifacts = [payload for kind, payload in events if kind == "artifacts"][-1]
        cross = final_artifacts["viewpoint_diagnostics"]["cross_chapter_groups"]
        preflight_count = 0 if plan_degraded else 1 if missing_group else 2
        written_count = 0 if plan_degraded else 1 if missing_group or writer_fallback or partial_group else 2
        partial_count = int(partial_group)
        self.assertEqual(cross["enabled_chapter_count"], preflight_count)
        self.assertEqual(cross["fallback_chapter_count"], 2 - preflight_count)
        self.assertEqual(cross["written_group_chapter_count"], written_count)
        self.assertEqual(cross["written_partial_group_chapter_count"], partial_count)
        self.assertEqual(cross["written_fallback_chapter_count"], 2 - written_count - partial_count)
        focus = final_artifacts["viewpoint_diagnostics"]["analysis_focus"]
        self.assertEqual(focus["group_extraction_status"], "complete")
        self.assertEqual(focus["chapter_plan_status"], "degraded" if plan_degraded else "complete")
        self.assertEqual(focus["fallback_used"], written_count != 2)
        self.assertIn(f"最终保留双人群视角 {written_count}/2 章", focus["message"])
        self.assertEqual(len(focus["unresolved"]), 2 - written_count)
        if missing_group:
            self.assertEqual(focus["unresolved"][0]["reason"], "chapter_group_coverage_incomplete")
        if writer_fallback:
            self.assertEqual(focus["unresolved"][0]["reason"], "writer_retry_exhausted")
        if partial_group:
            row = cross["written_chapters"][1]
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["retained_groups"], [groups[0]])
            self.assertEqual(row["missing_groups"], [groups[1]])
            self.assertEqual(row["failed_block_ids"], ["c02-id"])
            self.assertEqual(cross["writing_status"], "partial")
            self.assertIn("1 章仅保留部分人群视角", focus["message"])
        self.assertEqual(len(final_artifacts["viewpoint_diagnostics"]["chapters"]), 2)


if __name__ == "__main__":
    unittest.main()
