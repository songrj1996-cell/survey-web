from copy import deepcopy
import unittest
from unittest.mock import patch

from app.services.insight_report_v3 import chapters


def _profile_plan():
    return {
        "columns": [
            {
                "index": 0,
                "name": "性别",
                "role": "single_choice",
                "use_as_profile": True,
                "profile_scope": "analysis",
            },
            {"index": 1, "name": "体验反馈", "role": "open_text"},
        ],
        "parts": [{"name": "体验", "column_indexes": [1]}],
    }


def _registry(total=20):
    result = {}
    for index in range(total):
        response_id = f"global-{index + 1}"
        result[response_id] = {
            "response_id": response_id,
            "column_index": 1,
            "source_index": index + 1,
            "respondent_ref": f"player-{index + 1}",
            "profile": {"性别": "女性" if index < total // 2 else "男性"},
            "text": f"反馈 {index + 1}",
        }
    return result


class InsightV3ChapterContractTests(unittest.TestCase):
    def test_stage_three_repair_reuses_writer_frequency_and_numeric_gates(self):
        normalized, error = chapters.normalize_report_block_text(
            heading="多数玩家认为失败压力明显",
            body="7名玩家都提到责任归属。",
            findings=[{"skewed_toward": []}],
            profiles=[],
            numeric_sources="",
        )
        self.assertIsNone(normalized)
        self.assertTrue("精确人数" in error or "数字" in error)

        normalized, error = chapters.normalize_report_block_text(
            heading="多数玩家认为失败压力明显",
            body="反馈提到责任归属与压力感受。",
            findings=[{"skewed_toward": []}],
            profiles=[],
            numeric_sources="",
        )
        self.assertIsNone(error)
        self.assertNotIn("多数玩家", normalized["heading"])
        self.assertTrue(any("多数" in item for item in normalized["frequency_terms"]))

    def test_writer_fallback_keeps_one_bound_evidence_id_per_finding(self):
        blocks = chapters._fallback_blocks(
            ["1/f1"],
            {
                "1/f1": {
                    "claim": "发育路玩家普遍反复提到失败归因带来压力",
                    "caveat": "仅代表本题",
                    "response_ids": ["global-1", "global-2"],
                },
            },
            {"global-1": "r0001", "global-2": "r0002"},
        )
        self.assertEqual(blocks[0]["evidence_ids"], ["r0001"])
        self.assertNotIn("普遍", blocks[0]["body"])
        self.assertNotIn("反复提到", blocks[0]["body"])

    def test_writer_fallback_is_bounded_keeps_risks_and_strips_frequency_prefix(self):
        findings = {
            f"1/f{index}": {
                "claim": (
                    "部分玩家担心失败归因带来压力"
                    if index == 6 else f"普通体验发现 {index}"
                ),
                "kind": "risk" if index == 6 else "fact",
                "frequency_tier": "部分" if index == 6 else "少数",
                "response_ids": [f"global-{index}"],
            }
            for index in range(1, 7)
        }
        blocks = chapters._fallback_blocks(
            list(findings),
            findings,
            {f"global-{index}": f"r{index:04d}" for index in range(1, 7)},
            maximum_blocks=3,
        )
        self.assertEqual(len(blocks), 3)
        self.assertIn("1/f6", {item for block in blocks for item in block["finding_ids"]})
        risk_block = next(block for block in blocks if "1/f6" in block["finding_ids"])
        self.assertFalse(risk_block["heading"].startswith("部分玩家"))
        self.assertFalse(risk_block["body"].startswith("部分玩家"))

    def test_writer_fallback_merges_instead_of_dropping_required_findings(self):
        findings = {
            f"1/f{index}": {
                "claim": f"风险发现 {index}",
                "kind": "risk",
                "response_ids": [f"global-{index}"],
            }
            for index in range(1, 8)
        }
        blocks = chapters._fallback_blocks(
            list(findings),
            findings,
            {f"global-{index}": f"r{index:04d}" for index in range(1, 8)},
            maximum_blocks=3,
        )
        self.assertEqual(len(blocks), 3)
        self.assertEqual(
            {item for block in blocks for item in block["finding_ids"]},
            set(findings),
        )

    def test_evidence_is_rebound_to_a_source_that_supports_the_block(self):
        blocks, rebound_count = chapters._canonicalize_block_evidence(
            [{
                "id": "b1",
                "heading": "压力来源",
                "body": "失败归因会放大压力。",
                "finding_ids": ["1/f1"],
                "evidence_ids": ["r0002"],
                "evidence_points": [{"evidence_id": "r0002", "summary": "错误来源的概括"}],
            }],
            allowed_by_short={"r0001": {"1/f1"}, "r0002": {"1/f2"}},
        )
        self.assertEqual(blocks[0]["evidence_ids"], ["r0001"])
        self.assertEqual(blocks[0]["evidence_points"], [])
        self.assertEqual(rebound_count, 1)

    def test_frequency_gate_distinguishes_claims_from_game_terms(self):
        self.assertEqual(chapters._frequency_claim_term("大量伤害会改变对线节奏"), "")
        self.assertEqual(chapters._frequency_claim_term("部分阶段需要谨慎处理"), "")
        self.assertEqual(chapters._frequency_claim_term("部分玩家担心对线压力"), "部分玩家")
        self.assertEqual(chapters._frequency_claim_term("玩家普遍将死亡视为高代价"), "普遍")
        self.assertEqual(chapters._frequency_claim_term("该问题在回答中被反复提到"), "反复提到")
        self.assertEqual(chapters._frequency_claim_term("常见情形是被迫补位"), "常见情形")
        self.assertEqual(chapters._frequency_claim_term("玩家对职责的理解高度一致"), "高度一致")
        self.assertEqual(chapters._frequency_claim_term("资源争夺是绝对共识"), "绝对共识")
        self.assertEqual(chapters._frequency_claim_term("该职责被广泛视为核心"), "被广泛视为")
        self.assertEqual(chapters._frequency_claim_term("两类玩家均提到压力"), "均提到")

    def test_writer_gate_neutralizes_model_frequency_language_in_place(self):
        data = {
            "chapter_id": "c01",
            "blocks": [{
                "id": "b1",
                "heading": "部分玩家的压力",
                "body": "玩家普遍反复提到被迫补位，这种情况往往带来压力。",
                "finding_ids": ["1/f1"],
                "evidence_ids": ["r0001"],
                "evidence_points": [{"evidence_id": "r0001", "summary": "玩家普遍提到被迫补位"}],
            }],
        }
        rewrites = []
        error = chapters._validate_writer_output(
            data,
            chapter_id="c01",
            assigned_finding_ids={"1/f1"},
            required_finding_ids=set(),
            valid_short_ids={"r0001"},
            allowed_by_short={"r0001": {"1/f1"}},
            findings_by_id={"1/f1": {"skewed_toward": []}},
            profiles=[],
            numeric_sources="",
            frequency_rewrites=rewrites,
        )
        self.assertIsNone(error)
        self.assertEqual(data["blocks"][0]["heading"], "玩家的压力")
        self.assertNotIn("普遍", data["blocks"][0]["body"])
        self.assertNotIn("反复提到", data["blocks"][0]["body"])
        self.assertNotIn("往往", data["blocks"][0]["body"])
        self.assertNotIn("普遍", data["blocks"][0]["evidence_points"][0]["summary"])
        self.assertEqual(rewrites[0]["block_id"], "b1")

    def test_writer_gate_neutralizes_frequency_synonyms_from_real_replay(self):
        heading, heading_terms = chapters._neutralize_frequency_claims(
            "资源争夺是打野职责认知的绝对共识"
        )
        body, body_terms = chapters._neutralize_frequency_claims(
            "玩家对职责的理解高度一致，形成了一致认知；该职责被广泛视为核心，"
            "两类玩家均提到压力，并在全部分析章节中反复出现且未见明显分歧。"
        )
        self.assertNotIn("共识", heading)
        self.assertNotIn("高度一致", body)
        self.assertNotIn("一致认知", body)
        self.assertNotIn("广泛", body)
        self.assertNotIn("均提到", body)
        self.assertNotIn("全部分析章节", body)
        self.assertNotIn("反复出现", body)
        self.assertNotIn("未见明显分歧", body)
        self.assertIn("绝对共识", heading_terms)
        self.assertIn("高度一致", body_terms)
        self.assertIn("形成了一致认知", body_terms)
        self.assertIn("被广泛视为", body_terms)
        self.assertIn("均提到", body_terms)
        self.assertIn("全部分析章节", body_terms)
        self.assertIn("反复出现", body_terms)
        self.assertIn("未见明显分歧", body_terms)

        phrasing, phrasing_terms = chapters._neutralize_frequency_claims(
            "也有玩家是在补位时尝试；部分老玩家不需要引导；部分描述提到需要适应；不少人转向其他位置。"
        )
        self.assertIn("也有提及者是在补位时尝试", phrasing)
        self.assertIn("老玩家不需要引导", phrasing)
        self.assertIn("描述提到需要适应", phrasing)
        self.assertIn("提及者转向其他位置", phrasing)
        self.assertNotIn("部分", phrasing)
        self.assertIn("有玩家", phrasing_terms)
        self.assertIn("部分老玩家", phrasing_terms)
        self.assertIn("部分描述", phrasing_terms)
        self.assertIn("不少人", phrasing_terms)

        all_players, all_player_terms = chapters._neutralize_frequency_claims(
            "该入口不应强制呈现给所有玩家。"
        )
        self.assertEqual(all_players, "该入口不应强制呈现给相关玩家。")
        self.assertIn("所有玩家", all_player_terms)

    def test_writer_finding_exposes_only_local_representative_ids(self):
        payload = chapters._writer_finding(
            {
                "id": "1/f1",
                "claim": "部分玩家担心失败归因",
                "response_ids": ["global-1", "global-2"],
            },
            {"global-1": "r0001", "global-2": "r0002"},
        )
        self.assertEqual(payload["claim"], "担心失败归因")
        self.assertEqual(payload["representative_evidence_ids"], ["r0001", "r0002"])
        self.assertNotIn("global-1", str(payload))

    def test_planner_requires_exact_once_coverage(self):
        findings = {"1/f1", "1/f2"}
        valid = {
            "report_title": "体验洞察",
            "chapters": [
                {
                    "id": "c01",
                    "title": "压力来源",
                    "purpose": "解释压力",
                    "finding_ids": ["1/f1"],
                    "stat_ids": ["s001"],
                    "group_focus": None,
                },
                {
                    "id": "c02",
                    "title": "学习动机",
                    "purpose": "解释动机",
                    "finding_ids": ["1/f2"],
                    "stat_ids": [],
                    "group_focus": {"profile_name": "性别", "profile_value": "女性"},
                },
            ],
            "suspected_skews": [{
                "finding_id": "1/f1",
                "profile_name": "性别",
                "profile_value": "女性",
                "reason": "值得核对",
            }],
        }
        error = chapters._validate_chapter_plan(
            valid,
            finding_ids=findings,
            non_degraded_ids=findings,
            stat_ids={"s001"},
            profiles=[{"profile_name": "性别", "values": ["女性", "男性"]}],
        )
        self.assertIsNone(error)

        invalid = {**valid, "chapters": [dict(valid["chapters"][0])]}
        invalid["chapters"][0]["finding_ids"] = ["1/f1", "1/f1"]
        error = chapters._validate_chapter_plan(
            invalid,
            finding_ids=findings,
            non_degraded_ids=findings,
            stat_ids={"s001"},
            profiles=[{"profile_name": "性别", "values": ["女性", "男性"]}],
        )
        self.assertIn("重复", error)

    def test_expected_deliverable_guarantees_group_chapter_but_missing_focus_does_not(self):
        chapter_plan = {
            "report_title": "体验洞察",
            "chapters": [{
                "id": "c01",
                "title": "体验阻力",
                "purpose": "解释主要问题",
                "finding_ids": ["1/f1"],
                "stat_ids": [],
                "group_focus": None,
            }],
            "suspected_skews": [],
        }
        focused_plan = _profile_plan()
        focused_plan["analysis_focus"] = {
            "expected_deliverables": ["形成女性玩家群体观察"],
        }
        reconciled, diagnostics = chapters.ensure_required_group_chapters(
            chapter_plan,
            plan=focused_plan,
            profiles=[{"profile_name": "性别", "values": ["女性", "男性"]}],
        )
        group_chapters = [
            item for item in reconciled["chapters"]
            if item.get("group_focus") == {"profile_name": "性别", "profile_value": "女性"}
        ]
        self.assertEqual(len(group_chapters), 1)
        self.assertEqual(diagnostics["status"], "present")
        self.assertEqual(diagnostics["inserted"][0]["mode"], "appended")

        unchanged, missing = chapters.ensure_required_group_chapters(
            chapter_plan,
            plan=_profile_plan(),
            profiles=[{"profile_name": "性别", "values": ["女性", "男性"]}],
        )
        self.assertEqual(len(unchanged["chapters"]), 1)
        self.assertEqual(
            missing["message"],
            "未提供分析主线，章节按问卷结构组织，未规划人群专章",
        )

    def test_action_contract_requires_bound_findings_and_keeps_internal_shape(self):
        findings = {
            "1/f1": {
                "id": "1/f1",
                "claim": "失败归因带来压力",
                "kind": "pain",
                "degraded": False,
                "skewed_toward": [],
            },
        }
        completed = [{
            "id": "c01",
            "title": "压力来源",
            "finding_ids": ["1/f1"],
            "group_focus": None,
        }]
        valid = {"actions": [{
            "id": "a01",
            "title": "降低失败后的单人归责压力",
            "action_scope": "general",
            "target_group": None,
            "problem": "不少老玩家认为失败后责任集中到单一职责会放大体验压力",
            "objective": "让责任与协作状态更容易被理解",
            "design_directions": ["玩家多数会从协作状态反馈中受益", "让异常责任归属便于复盘"],
            "validation_plan": "结合试用反馈核对玩家是否更容易理解责任来源",
            "finding_ids": ["1/f1"],
            "evidence_gap": "",
            "required_data": "",
        }]}
        self.assertIsNone(chapters._validate_action_output(
            valid,
            findings_by_id=findings,
            chapters=completed,
            profiles=[],
            numeric_sources="",
        ))
        self.assertEqual(valid["actions"][0]["problem"], "老玩家认为失败后责任集中到单一职责会放大体验压力")
        self.assertEqual(valid["actions"][0]["design_directions"][0], "玩家会从协作状态反馈中受益")
        invalid = {"actions": [{**valid["actions"][0], "finding_ids": ["unknown"]}]}
        self.assertIn("未知", chapters._validate_action_output(
            invalid,
            findings_by_id=findings,
            chapters=completed,
            profiles=[],
            numeric_sources="",
        ))

    def test_action_contract_enforces_caps_and_group_difference_gate(self):
        findings = {
            "group:c02/g1": {
                "id": "group:c02/g1",
                "claim": "女性玩家提到协作压力",
                "kind": "pain",
                "degraded": False,
                "skewed_toward": [],
            },
        }
        completed = [{
            "id": "c02",
            "title": "女性群体观察",
            "finding_ids": ["group:c02/g1"],
            "group_focus": {"profile_name": "性别", "profile_value": "女性"},
        }]

        def action(index: int) -> dict:
            return {
                "id": f"a{index:02d}",
                "title": "为女性玩家提供更清楚的协作提示",
                "action_scope": "group",
                "target_group": {"profile_name": "性别", "profile_value": "女性"},
                "problem": "女性玩家更突出地受到协作压力影响",
                "objective": "降低协作信息不清带来的体验压力",
                "design_directions": ["补充协作状态提示"],
                "validation_plan": "通过该人群的后续访谈核对理解是否改善",
                "finding_ids": ["group:c02/g1"],
                "evidence_gap": "",
                "required_data": "",
            }

        difference_error = chapters._validate_action_output(
            {"actions": [action(1)]},
            findings_by_id=findings,
            chapters=completed,
            profiles=[{"profile_name": "性别", "values": ["女性", "男性"]}],
            numeric_sources="",
        )
        self.assertIn("没有 skewed_toward", difference_error)

        too_many = []
        for index in range(1, chapters.INSIGHT_V3_MAX_GROUP_ACTIONS + 2):
            item = action(index)
            item["problem"] = "协作状态难以理解"
            too_many.append(item)
        cap_error = chapters._validate_action_output(
            {"actions": deepcopy(too_many)},
            findings_by_id=findings,
            chapters=completed,
            profiles=[{"profile_name": "性别", "values": ["女性", "男性"]}],
            numeric_sources="",
        )
        self.assertIn("人群专项建议不得超过", cap_error)

    def test_writer_gate_blocks_unverified_difference_and_profile_counts(self):
        profiles = [{"profile_name": "性别", "values": ["女性", "男性"]}]
        base = {
            "chapter_id": "c01",
            "blocks": [{
                "id": "b1",
                "heading": "压力来源",
                "body": "该视角在女性玩家中更突出。",
                "finding_ids": ["1/f1"],
                "evidence_ids": ["r0001"],
                "evidence_points": [],
            }],
        }
        kwargs = {
            "chapter_id": "c01",
            "assigned_finding_ids": {"1/f1"},
            "required_finding_ids": {"1/f1"},
            "valid_short_ids": {"r0001"},
            "allowed_by_short": {"r0001": {"1/f1"}},
            "profiles": profiles,
            "numeric_sources": "",
        }
        error = chapters._validate_writer_output(
            base,
            findings_by_id={"1/f1": {"skewed_toward": []}},
            **kwargs,
        )
        self.assertIn("没有 skewed_toward", error)

        verified = {
            "1/f1": {
                "skewed_toward": [{
                    "profile_name": "性别",
                    "profile_value": "女性",
                    "supporter_count": 7,
                }],
            },
        }
        precise = {
            **base,
            "blocks": [{
                **base["blocks"][0],
                "body": "该视角在7名女性玩家中更突出。",
            }],
        }
        error = chapters._validate_writer_output(
            precise,
            findings_by_id=verified,
            **kwargs,
        )
        self.assertIn("精确人数", error)

    def test_writer_may_omit_ordinary_finding_but_not_required_risk(self):
        output = {
            "chapter_id": "c01",
            "blocks": [{
                "id": "b1",
                "heading": "学习门槛",
                "body": "技能组合带来额外学习成本。",
                "finding_ids": ["1/f1"],
                "evidence_ids": ["r0001"],
                "evidence_points": [],
            }],
        }
        findings = {
            "1/f1": {"kind": "pain", "skewed_toward": []},
            "1/f2": {"kind": "fact", "skewed_toward": []},
            "1/f3": {"kind": "risk", "skewed_toward": []},
        }
        common = {
            "chapter_id": "c01",
            "assigned_finding_ids": set(findings),
            "valid_short_ids": {"r0001", "r0002", "r0003"},
            "allowed_by_short": {
                "r0001": {"1/f1"},
                "r0002": {"1/f2"},
                "r0003": {"1/f3"},
            },
            "findings_by_id": findings,
            "profiles": [],
            "numeric_sources": "",
            "maximum_blocks": 5,
        }
        self.assertIsNone(chapters._validate_writer_output(
            output,
            required_finding_ids=set(),
            **common,
        ))
        error = chapters._validate_writer_output(
            output,
            required_finding_ids={"1/f3"},
            **common,
        )
        self.assertIn("1/f3", error)


class InsightV3ProfileCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_chapter_translation_uses_the_run_namespace_without_chapter_suffix(self):
        captured = {}

        async def fake_call(_system, _query, **kwargs):
            data = {
                "chapter_id": "c01",
                "blocks": [{
                    "id": "b1",
                    "heading": "压力来源",
                    "body": "失败归因会放大压力。",
                    "finding_ids": ["1/f1"],
                    "evidence_ids": ["r0001"],
                    "evidence_points": [{
                        "evidence_id": "r0001",
                        "summary": "失败后被队友归责会放大压力",
                    }],
                }],
            }
            self.assertIsNone(kwargs["validator"](data))
            return {"data": data, "model": "mock-writer"}

        async def fake_translate(items, *, namespace, collect=None):
            captured["namespace"] = namespace
            return [{
                **item,
                "translation_zh": "翻译后的代表反馈",
                "translation_status": "complete",
            } for item in items]

        with (
            patch.object(chapters, "_direct_json_call", new=fake_call),
            patch.object(chapters, "translate_report_source_items", new=fake_translate),
        ):
            result = await chapters._write_one_section(
                chapter={
                    "id": "c01",
                    "title": "压力",
                    "purpose": "解释压力来源",
                    "finding_ids": ["1/f1"],
                    "stat_ids": [],
                    "group_focus": None,
                },
                findings_by_id={
                    "1/f1": {
                        "id": "1/f1",
                        "claim": "失败归因会放大压力",
                        "kind": "pain",
                        "question_key": "1",
                        "question": "体验反馈",
                        "response_ids": ["global-1"],
                        "skewed_toward": [],
                    },
                },
                source_registry={
                    "global-1": {
                        "column_index": 1,
                        "text": "I feel pressured after a loss.",
                    },
                },
                stats_by_id={},
                profiles=[],
                qualitative_context=None,
                translation_namespace="owner:session:run",
            )

        self.assertEqual(captured["namespace"], "owner:session:run")
        self.assertEqual(result["blocks"][0]["evidence"][0]["translation_status"], "complete")
        self.assertEqual(
            result["blocks"][0]["evidence"][0]["evidence_point"],
            "失败后被队友归责会放大压力",
        )

    async def test_group_extraction_uses_one_short_id_per_player_then_hydrates(self):
        registry = _registry(2)
        registry["global-1b"] = {
            **registry["global-1"],
            "response_id": "global-1b",
            "column_index": 2,
            "source_index": 3,
            "text": "同一玩家的另一题反馈",
        }
        captured = {}

        async def fake_call(_system, query, **kwargs):
            captured["query"] = query
            data = {
                "findings": [{
                    "id": "g1",
                    "claim": "该人群提到协作压力",
                    "kind": "pain",
                    "response_ids": ["r0001"],
                    "caveat": "仅为定性归纳",
                }],
            }
            self.assertIsNone(kwargs["validator"](data))
            return {"data": data, "model": "mock-group"}

        with patch.object(chapters, "_direct_json_call", new=fake_call):
            findings, diagnostics = await chapters.extract_group_findings(
                chapter={
                    "id": "c01",
                    "group_focus": {"profile_name": "性别", "profile_value": "女性"},
                },
                source_registry=registry,
                plan=_profile_plan(),
            )

        self.assertEqual(diagnostics["respondent_count"], 1)
        self.assertEqual(findings[0]["response_ids"], ["global-1", "global-1b"])
        self.assertNotIn("global-1", captured["query"])
        self.assertNotIn("respondent_ref", captured["query"])
        self.assertNotIn('"profile"', captured["query"])
        self.assertEqual(captured["query"].count('"response_id":"r0001"'), 1)

    async def test_skew_verifier_never_receives_profile_data(self):
        registry = _registry()
        short_map = {
            f"r{index:04d}": [f"global-{index}"]
            for index in range(1, 21)
        }
        source_contract = {
            "source_registry": registry,
            "questions": [{
                "scope_key": "1",
                "question": "体验反馈",
                "model_sources": [
                    {"response_id": short_id, "responses": [f"反馈 {index}"]}
                    for index, short_id in enumerate(short_map, 1)
                ],
                "short_id_map": short_map,
            }],
        }
        finding = {
            "id": "1/f1",
            "claim": "失败归因带来压力",
            "scope_key": "1",
            "skewed_toward": [],
        }
        captured = {}

        async def fake_call(_system, query, **kwargs):
            captured["query"] = query
            data = {
                "matches": [{
                    "finding_id": "1/f1",
                    "response_ids": [
                        "r0001", "r0002", "r0003", "r0004",
                        "r0005", "r0006", "r0007", "r0011",
                    ],
                }],
            }
            self.assertIsNone(kwargs["validator"](data))
            return {"data": data, "model": "mock-skew"}

        with patch.object(chapters, "_direct_json_call", new=fake_call):
            hydrated, diagnostics = await chapters.verify_suspected_skews(
                findings=[finding],
                suspected_skews=[{
                    "finding_id": "1/f1",
                    "profile_name": "性别",
                    "profile_value": "女性",
                    "reason": "待核对",
                }],
                source_contract=source_contract,
                plan=_profile_plan(),
            )

        self.assertNotIn("性别", captured["query"])
        self.assertNotIn("女性", captured["query"])
        self.assertNotIn("男性", captured["query"])
        self.assertNotIn("global-", captured["query"])
        self.assertEqual(
            hydrated[0]["skewed_toward"],
            [{"profile_name": "性别", "profile_value": "女性", "supporter_count": 7}],
        )
        self.assertEqual(diagnostics["verified_count"], 1)

    async def test_skew_preflight_rejects_small_scope_without_model_call(self):
        registry = _registry(10)
        short_map = {
            f"r{index:04d}": [f"global-{index}"]
            for index in range(1, 11)
        }
        source_contract = {
            "source_registry": registry,
            "questions": [{
                "scope_key": "1",
                "question": "体验反馈",
                "model_sources": [],
                "short_id_map": short_map,
            }],
        }
        call = patch.object(
            chapters,
            "_direct_json_call",
            side_effect=AssertionError("small scope must be prefiltered"),
        )
        with call as mocked:
            _findings, diagnostics = await chapters.verify_suspected_skews(
                findings=[{
                    "id": "1/f1",
                    "claim": "失败归因带来压力",
                    "scope_key": "1",
                    "skewed_toward": [],
                }],
                suspected_skews=[{
                    "finding_id": "1/f1",
                    "profile_name": "性别",
                    "profile_value": "女性",
                    "reason": "待核对",
                }],
                source_contract=source_contract,
                plan=_profile_plan(),
            )
        mocked.assert_not_called()
        self.assertEqual(diagnostics["prefiltered_count"], 1)
        self.assertEqual(diagnostics["verification_call_count"], 0)
        self.assertEqual(diagnostics["items"][0]["reason"], "scope_known_too_small")


if __name__ == "__main__":
    unittest.main()
