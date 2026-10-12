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
    def test_action_semantic_repair_preserves_identity_and_existing_gates(self):
        from app.services.insight_report_v3.contracts import ACTION_REPAIR_TEXT_FIELDS
        original = {"id": "a04", "title": "改善反馈", "action_scope": "general", "target_group": None,
                    "problem": "有玩家提到压力", "objective": "理解体验", "design_directions": ["补充体验反馈"],
                    "validation_plan": "访谈核对", "finding_ids": ["1/f1"], "evidence_gap": "", "required_data": ""}
        text = {k: deepcopy(original[k]) for k in ACTION_REPAIR_TEXT_FIELDS}
        kwargs = dict(original=original, findings_by_id={"1/f1": {"response_ids": ["global"]}}, chapters=[], profiles=[], numeric_sources="")
        result, error = chapters.validate_action_repair(action_text=text, **kwargs)
        self.assertIsNone(error)
        self.assertEqual(result, original)
        boundary_text = {
            **text,
            "evidence_gap": "不清楚这一认知缺口在整体玩家中的普遍程度。",
            "required_data": "需要补充新手玩家认知调查。",
        }
        repaired, error = chapters.validate_action_repair(action_text=boundary_text, **kwargs)
        self.assertIsNone(error)
        self.assertEqual(repaired["evidence_gap"], boundary_text["evidence_gap"])
        for change in ({"finding_ids": ["other"]}, {"problem": "所有玩家都有压力"}, {"problem": "37%的玩家有压力"}):
            self.assertIsNotNone(chapters.validate_action_repair(action_text={**text, **change}, **kwargs)[1])

    def test_writer_evidence_and_semantic_repair_share_product_subject_exception(self):
        findings = {"1/f1": {"skewed_toward": []}}
        profiles = [{"profile_name": "性别", "values": ["女性", "男性"]}]
        kwargs = {
            "chapter_id": "c01", "assigned_finding_ids": {"1/f1"},
            "required_finding_ids": {"1/f1"}, "valid_short_ids": {"r0001"},
            "allowed_by_short": {"r0001": {"1/f1"}}, "findings_by_id": findings,
            "profiles": profiles, "numeric_sources": "",
        }
        base = {"chapter_id": "c01", "blocks": [{
            "id": "b1", "heading": "界面布局", "body": "新版界面的信息更集中。",
            "finding_ids": ["1/f1"], "evidence_ids": ["r0001"],
            "evidence_points": [{"evidence_id": "r0001", "summary": "按钮的描边更明显。"}],
        }]}
        self.assertIsNone(chapters._validate_writer_output(deepcopy(base), **kwargs))
        for body, allowed in (
            ("新版界面的信息更集中。", True),
            ("女性玩家的反馈更集中，按钮的描边更明显。", False),
            ("它的描边更明显。", False),
        ):
            with self.subTest(body=body):
                output = deepcopy(base)
                output["blocks"][0]["body"] = body
                self.assertEqual(chapters._validate_writer_output(output, **kwargs) is None, allowed)
                normalized, error = chapters.normalize_report_block_text(
                    heading="界面布局", body=body, findings=list(findings.values()),
                    profiles=profiles, numeric_sources="",
                )
                self.assertEqual(error is None, allowed)
                self.assertEqual(normalized is not None, allowed)
        base["blocks"][0]["evidence_points"][0]["summary"] = "女性玩家对按钮的偏好更集中。"
        self.assertIn("没有 skewed_toward", chapters._validate_writer_output(base, **kwargs))

    def test_writer_and_repair_reject_native_language_inferred_from_client_language(self):
        finding = {"profile_presence": [{"profile_name": "MLBB客户端语言", "values": ["英语"]}]}
        normalized, error = chapters.normalize_report_block_text(
            heading="英语母语玩家视角", body="该群体提到布局问题。",
            findings=[finding], profiles=[], numeric_sources="",
        )
        self.assertIsNone(normalized)
        self.assertIn("不得把客户端语言", error)
        normalized, error = chapters.normalize_report_block_text(
            heading="英语客户端玩家视角", body="该群体提到布局问题。",
            findings=[finding], profiles=[], numeric_sources="",
        )
        self.assertIsNone(error)
        self.assertIsNotNone(normalized)
        self.assertIsNone(chapters._profile_dimension_language_error(
            "当前未收集玩家母语，不能据此判断母语差异。", [finding],
        ))
        self.assertIsNotNone(chapters._profile_dimension_language_error(
            "不能据此推断母语，英语母语玩家反馈了布局问题。", [finding],
        ))
        finding["profile_presence"][0]["profile_name"] = "玩家母语"
        normalized, error = chapters.normalize_report_block_text(
            heading="英语母语玩家视角", body="该群体提到布局问题。",
            findings=[finding], profiles=[], numeric_sources="",
        )
        self.assertIsNone(error)

    @staticmethod
    def _group_finding(finding_id, response_id, value):
        return {
            "id": finding_id,
            "claim": f"{value}玩家的定性发现",
            "kind": "pain",
            "frequency_tier": "多道题均有提及",
            "response_ids": [response_id],
            "caveat": "",
            "profile_presence": [{
                "profile_name": "语言",
                "values": [value],
                "basis": "code_filtered_group_all_open_text",
            }],
            "skewed_toward": [],
            "support_basis": "code_filtered_group_qualitative_extraction",
            "question_key": f"group:{value}",
            "question": f"语言={value} 人群全部开放题",
            "audience": f"语言：{value}",
            "degraded": False,
        }

    def test_stage_three_repair_reuses_writer_frequency_and_numeric_gates(self):
        normalized, error = chapters.normalize_report_block_text(
            heading="失败压力",
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
        self.assertIsNone(normalized)
        self.assertIn("频次", error)

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
        self.assertEqual(chapters._frequency_claim_term("不清楚这一问题在整体玩家中的普遍程度"), "")
        self.assertEqual(chapters._frequency_claim_term("尚无法判断该问题是否普遍"), "")
        self.assertEqual(chapters._frequency_claim_term("无法确认问题规模；玩家普遍认为说明不足"), "普遍")
        self.assertEqual(chapters._frequency_claim_term("不清楚具体原因，但玩家普遍认为说明不足"), "普遍")

    def test_writer_gate_rejects_frequency_without_corrupting_sentence(self):
        source = "这部分玩家在等待好友时选择该模式。"
        normalized, error = chapters.normalize_report_block_text(
            heading="使用场景", body=source, findings=[], profiles=[], numeric_sources="",
        )
        self.assertIsNone(normalized)
        self.assertIn("频次", error)
        self.assertEqual(chapters._neutralize_frequency_claims(source), (source, []))
    def test_frequency_synonyms_stay_intact_for_bounded_repair(self):
        for source in (
            "这是所有玩家的绝对共识。", "两类玩家均提到压力。",
            "多数玩家往往选择该方案。", "该职责被广泛视为核心。",
            "玩家对职责的理解高度一致，形成了一致认知。",
        ):
            with self.subTest(source=source):
                self.assertEqual(chapters._neutralize_frequency_claims(source), (source, []))
                normalized, error = chapters.normalize_report_block_text(
                    heading="判断", body=source, findings=[], profiles=[], numeric_sources="",
                )
                self.assertIsNone(normalized)
                self.assertIn("频次", error)
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

    def test_universal_group_claim_is_rejected_not_rewritten(self):
        source = "无论客户端语言为英语还是印尼语的玩家，都认为装备信息清晰。"
        self.assertEqual(chapters._neutralize_frequency_claims(source), (source, []))
        normalized, error = chapters.normalize_report_block_text(
            heading="界面阅读", body=source, findings=[], profiles=[], numeric_sources="",
        )
        self.assertIsNone(normalized)
        self.assertIn("频次", error)
    def test_real_frequency_scan_false_positives_are_not_neutralized(self):
        for text in (
            "在装备预设列表、英雄对战设置以及职业玩家配装等多个浏览场景中，均存在误触。",
            "各展示方块形状不均，若玩家缺少相应展示物品，页面显得杂乱。",
            "客户端语言为印尼语的玩家反馈集中在本地化完整性和内容扩展需求上。",
            "保持并评估该可视化预览模式的一致性与可发现性，提升玩家操作直觉。",
            "无论哪种客户端，玩家都可以打开设置。",
            "无论哪种客户端，玩家都不认为操作方便。",
            "并非英语和印尼语的玩家都认为操作方便。",
        ):
            with self.subTest(text=text):
                self.assertEqual(chapters._neutralize_frequency_claims(text), (text, []))

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
            allow_group_chapters=True,
        )
        self.assertIsNone(error)

        error = chapters._validate_chapter_plan(
            valid,
            finding_ids=findings,
            non_degraded_ids=findings,
            stat_ids={"s001"},
            profiles=[{"profile_name": "性别", "values": ["女性", "男性"]}],
            allow_group_chapters=False,
        )
        self.assertIn("analysis_focus 缺失", error)

        invalid = {**valid, "chapters": [dict(valid["chapters"][0])]}
        invalid["chapters"][0]["finding_ids"] = ["1/f1", "1/f1"]
        error = chapters._validate_chapter_plan(
            invalid,
            finding_ids=findings,
            non_degraded_ids=findings,
            stat_ids={"s001"},
            profiles=[{"profile_name": "性别", "values": ["女性", "男性"]}],
            allow_group_chapters=True,
        )
        self.assertIn("重复", error)

    def test_cross_group_planner_keeps_global_guards_but_allows_local_missing_group(self):
        groups = {("语言", "英语"), ("语言", "印尼语")}
        finding_groups = {
            "group:en/g1": ("语言", "英语"),
            "group:id/g1": ("语言", "印尼语"),
        }
        plan = {
            "report_title": "语言体验洞察",
            "chapters": [{
                "id": "c01",
                "title": "关键阻力",
                "purpose": "分别呈现两组阻力",
                "finding_ids": ["1/f1", "group:en/g1", "group:id/g1"],
                "stat_ids": [],
                "group_focus": None,
            }],
            "suspected_skews": [],
        }
        self.assertIsNone(chapters._validate_chapter_plan(
            plan,
            finding_ids={"1/f1", "group:en/g1", "group:id/g1"},
            non_degraded_ids={"1/f1", "group:en/g1", "group:id/g1"},
            stat_ids=set(),
            profiles=[{"profile_name": "语言", "values": ["英语", "印尼语"]}],
            allow_group_chapters=False,
            finding_group_keys=finding_groups,
            cross_group_keys=groups,
        ))

        missing = deepcopy(plan)
        missing["chapters"][0]["finding_ids"].remove("group:id/g1")
        error = chapters._validate_chapter_plan(
            missing,
            finding_ids={"1/f1", "group:en/g1"},
            non_degraded_ids={"1/f1", "group:en/g1"},
            stat_ids=set(),
            profiles=[{"profile_name": "语言", "values": ["英语", "印尼语"]}],
            allow_group_chapters=False,
            finding_group_keys={"group:en/g1": ("语言", "英语")},
            cross_group_keys=groups,
        )
        self.assertIsNone(error)
        guards = dict(
            finding_ids={"1/f1", "group:en/g1", "group:id/g1"},
            non_degraded_ids={"1/f1", "group:en/g1", "group:id/g1"},
            stat_ids=set(),
            profiles=[{"profile_name": "语言", "values": ["英语", "印尼语"]}],
            allow_group_chapters=False,
            finding_group_keys=finding_groups,
            cross_group_keys=groups,
        )
        self.assertIn("必须且只能分配", chapters._validate_chapter_plan(missing, **guards))
        for change, expected in (
            ({"finding_ids": ["group:en/g1", "group:id/g1"]}, "缺少全体 finding"),
            ({"stat_ids": ["unknown"]}, "stat_ids 无效"),
            ({"finding_ids": ["1/f1", "1/f1"]}, "重复或未知"),
            ({"finding_ids": ["1/f1", "unknown"]}, "重复或未知"),
        ):
            with self.subTest(change=change):
                invalid = deepcopy(plan)
                invalid["chapters"][0].update(change)
                self.assertIn(expected, chapters._validate_chapter_plan(invalid, **guards))

    def test_cross_group_capacity_counts_deduplicated_sources_and_falls_back_per_chapter(self):
        base_one = {
            "id": "1/f1", "claim": "共同阻力", "kind": "pain",
            "response_ids": ["shared"], "question_key": "1", "question": "体验",
            "audience": "", "degraded": False, "profile_presence": [], "skewed_toward": [],
        }
        base_two = {
            **base_one,
            "id": "2/f1", "claim": "另一个主题", "response_ids": ["base-2"],
            "question_key": "2",
        }
        en_one = self._group_finding("group:en/g1", "shared", "英语")
        id_one = self._group_finding("group:id/g1", "id-small", "印尼语")
        en_two = self._group_finding("group:en/g2", "en-large", "英语")
        id_two = self._group_finding("group:id/g2", "id-large", "印尼语")
        findings = {
            item["id"]: item
            for item in (base_one, base_two, en_one, id_one, en_two, id_two)
        }
        registry = {
            "shared": {"column_index": 1, "text": "共享来源"},
            "id-small": {"column_index": 1, "text": "短来源"},
            "base-2": {"column_index": 2, "text": "普通来源"},
            "en-large": {"column_index": 2, "text": "x" * 91000},
            "id-large": {"column_index": 2, "text": "另一组来源"},
        }
        chapter_plan = {
            "report_title": "语言体验洞察",
            "chapters": [
                {
                    "id": "c01", "title": "阻力", "purpose": "解释阻力",
                    "finding_ids": ["1/f1", "group:en/g1", "group:id/g1"],
                    "stat_ids": [], "group_focus": None,
                },
                {
                    "id": "c02", "title": "机会", "purpose": "解释机会",
                    "finding_ids": ["2/f1", "group:en/g2", "group:id/g2"],
                    "stat_ids": [], "group_focus": None,
                },
            ],
            "suspected_skews": [],
        }
        groups = [
            {"profile_name": "语言", "profile_value": "英语"},
            {"profile_name": "语言", "profile_value": "印尼语"},
        ]
        updated, diagnostics = chapters.apply_cross_group_capacity_preflight(
            chapter_plan,
            requested_groups=groups,
            available_groups={("语言", "英语"), ("语言", "印尼语")},
            findings_by_id=findings,
            source_registry=registry,
            stats_catalog=[],
            qualitative_context=None,
            chapter_plan_status="complete",
        )

        first, second = updated["chapters"]
        self.assertEqual(first["cross_group_focus"], groups)
        self.assertEqual(first["cross_group_preflight"]["source_count"], 2)
        self.assertEqual(first["cross_group_preflight"]["status"], "enabled")
        self.assertNotIn("cross_group_focus", second)
        self.assertEqual(second["finding_ids"], ["2/f1"])
        self.assertEqual(
            second["cross_group_preflight"]["reason"],
            "chapter_input_chars_exceeded",
        )
        self.assertGreater(second["cross_group_preflight"]["input_chars"], 90000)
        self.assertEqual(diagnostics["enabled_chapter_count"], 1)
        self.assertEqual(diagnostics["fallback_chapter_count"], 1)
        actual = chapters.chapter_writer_input_diagnostics(
            chapter=second, findings_by_id=findings, source_registry=registry,
            stats_by_id={}, qualitative_context=None,
        )
        self.assertEqual(second["cross_group_preflight"]["ordinary_input_chars"], actual["input_chars"])

        # Move one group's finding to the other theme rather than lose it.
        # The otherwise valid plan must retain one grouped chapter.
        missing = deepcopy(chapter_plan)
        missing["chapters"][1]["finding_ids"].remove("group:id/g2")
        missing["chapters"][0]["finding_ids"].append("group:id/g2")
        updated, diagnostics = chapters.apply_cross_group_capacity_preflight(
            missing, requested_groups=groups,
            available_groups={("语言", "英语"), ("语言", "印尼语")},
            findings_by_id=findings, source_registry=registry, stats_catalog=[],
            qualitative_context=None, report_instruction="每章按语言分组",
        )
        self.assertEqual(diagnostics["enabled_chapter_count"], 1)
        self.assertEqual(updated["chapters"][0]["cross_group_focus"], groups)
        fallback = updated["chapters"][1]
        self.assertEqual(fallback["finding_ids"], ["2/f1"])
        self.assertEqual(fallback["cross_group_preflight"]["reason"], "chapter_group_coverage_incomplete")
        actual = chapters.chapter_writer_input_diagnostics(
            chapter=fallback, findings_by_id=findings, source_registry=registry,
            stats_by_id={}, qualitative_context=None, report_instruction="每章按语言分组",
        )
        self.assertEqual(fallback["cross_group_preflight"]["ordinary_input_chars"], actual["input_chars"])
        self.assertEqual(
            {item for chapter in updated["chapters"] for item in chapter["finding_ids"] if not item.startswith("group:")},
            {"1/f1", "2/f1"},
        )

    def test_capacity_gate_includes_system_prompt_at_exact_boundary(self):
        for query_size, system_size, expected_exceeded in (
            (89764, 882, True),  # Saved c06: the material alone fits, the request does not.
            (89118, 882, False),
            (89119, 882, True),
        ):
            with self.subTest(query=query_size, system=system_size):
                request = {
                    "query": "x" * query_size, "system_prompt": "系" * system_size,
                    "finding_ids": ["1/f1"], "sources": {"r001": {}},
                }
                with patch.object(chapters, "build_chapter_writer_request", return_value=request):
                    actual = chapters.chapter_writer_input_diagnostics(
                        chapter={}, findings_by_id={}, source_registry={},
                        stats_by_id={}, qualitative_context=None,
                    )
                self.assertEqual(actual["input_chars"], query_size + system_size)
                self.assertEqual(actual["query_chars"], query_size)
                self.assertEqual(actual["system_chars"], system_size)
                self.assertEqual(actual["serialized_chars"], actual["input_chars"])
                self.assertEqual(actual["input_target_exceeded"], expected_exceeded)

    def test_system_only_overflow_falls_back_locally_without_truncating_sources(self):
        groups = [
            {"profile_name": "语言", "profile_value": "英语"},
            {"profile_name": "语言", "profile_value": "印尼语"},
        ]
        findings = {}
        plan = {"chapters": []}
        for index in (1, 2):
            items = [
                {"id": f"{index}/f1", "response_ids": [f"base-{index}"]},
                self._group_finding(f"group:en/g{index}", f"en-{index}", "英语"),
                self._group_finding(f"group:id/g{index}", f"id-{index}", "印尼语"),
            ]
            findings.update({item["id"]: item for item in items})
            plan["chapters"].append({
                "id": f"c0{index}", "finding_ids": [item["id"] for item in items],
                "stat_ids": [],
            })
        registry = {"raw": {"text": "完整保留" * 30000}}
        before_registry, before_findings = deepcopy(registry), deepcopy(findings)

        def request(**kwargs):
            chapter = kwargs["chapter"]
            query_size = 89764 if chapter["id"] == "c01" else 89118
            if chapter.get("cross_group_preflight", {}).get("status") == "ordinary_fallback":
                query_size = 100
            return {
                "query": "x" * query_size, "system_prompt": "系" * 882,
                "finding_ids": chapter["finding_ids"], "sources": {"r001": {}},
            }

        with patch.object(chapters, "build_chapter_writer_request", side_effect=request):
            updated, diagnostic = chapters.apply_cross_group_capacity_preflight(
                plan, requested_groups=groups,
                available_groups={("语言", "英语"), ("语言", "印尼语")},
                findings_by_id=findings, source_registry=registry,
                stats_catalog=[], qualitative_context=None,
            )
        fallback, grouped = updated["chapters"]
        self.assertEqual(fallback["finding_ids"], ["1/f1"])
        self.assertEqual(fallback["cross_group_preflight"]["input_chars"], 90646)
        self.assertEqual(fallback["cross_group_preflight"]["ordinary_input_chars"], 982)
        self.assertEqual(fallback["cross_group_preflight"]["reason"], "chapter_input_chars_exceeded")
        self.assertEqual(grouped["cross_group_focus"], groups)
        self.assertEqual(grouped["cross_group_preflight"]["input_chars"], 90000)
        self.assertEqual(diagnostic["enabled_chapter_count"], 1)
        self.assertEqual(diagnostic["fallback_chapter_count"], 1)
        self.assertEqual(registry, before_registry)
        self.assertEqual(findings, before_findings)

    def test_cross_group_writer_requires_separate_labeled_blocks(self):
        findings = {
            "group:en/g1": self._group_finding("group:en/g1", "en", "英语"),
            "group:id/g1": self._group_finding("group:id/g1", "id", "印尼语"),
        }
        common = {
            "chapter_id": "c01",
            "assigned_finding_ids": set(findings),
            "required_finding_ids": set(),
            "valid_short_ids": {"r0001", "r0002"},
            "allowed_by_short": {
                "r0001": {"group:en/g1"},
                "r0002": {"group:id/g1"},
            },
            "findings_by_id": findings,
            "profiles": [{"profile_name": "语言", "values": ["英语", "印尼语"]}],
            "numeric_sources": "",
            "cross_group_keys": {("语言", "英语"), ("语言", "印尼语")},
        }
        valid = {
            "chapter_id": "c01",
            "blocks": [
                {
                    "id": "b1", "heading": "英语玩家视角", "body": "英语玩家提到协作阻力。",
                    "finding_ids": ["group:en/g1"], "evidence_ids": ["r0001"],
                    "evidence_points": [],
                },
                {
                    "id": "b2", "heading": "印尼语玩家视角", "body": "印尼语玩家提到学习阻力。",
                    "finding_ids": ["group:id/g1"], "evidence_ids": ["r0002"],
                    "evidence_points": [],
                },
            ],
        }
        self.assertIsNone(chapters._validate_writer_output(deepcopy(valid), **common))
        mixed = deepcopy(valid)
        mixed["blocks"] = [{
            **mixed["blocks"][0],
            "finding_ids": ["group:en/g1", "group:id/g1"],
        }]
        self.assertIn("混合了两组", chapters._validate_writer_output(mixed, **common))

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
        finding_rows = [{
            "id": "1/f1",
            "question": "体验反馈",
            "scope_key": "1",
        }]
        reconciled, diagnostics = chapters.ensure_required_group_chapters(
            chapter_plan,
            plan=focused_plan,
            profiles=[{"profile_name": "性别", "values": ["女性", "男性"]}],
            findings=finding_rows,
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
            findings=finding_rows,
        )
        self.assertEqual(len(unchanged["chapters"]), 1)
        self.assertFalse(missing["fallback_used"])
        self.assertEqual(
            missing["message"],
            "未提供分析主线，章节按问卷结构组织，未规划人群专章",
        )

        rogue = deepcopy(chapter_plan)
        rogue["chapters"][0]["title"] = "女性玩家体验阻力"
        rogue["chapters"][0]["group_focus"] = {
            "profile_name": "性别",
            "profile_value": "女性",
        }
        fallback, rejected = chapters.ensure_required_group_chapters(
            rogue,
            plan=_profile_plan(),
            profiles=[{"profile_name": "性别", "values": ["女性", "男性"]}],
            findings=finding_rows,
        )
        self.assertTrue(rejected["fallback_used"])
        self.assertEqual(len(rejected["rejected_unrequested_group_chapters"]), 1)
        self.assertTrue(all(item.get("group_focus") is None for item in fallback["chapters"]))
        self.assertEqual(
            [finding_id for item in fallback["chapters"] for finding_id in item["finding_ids"]],
            ["1/f1"],
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
            "problem": "失败后责任集中到单一职责会放大体验压力",
            "objective": "让责任与协作状态更容易被理解",
            "design_directions": ["提供协作状态反馈", "让异常责任归属便于复盘"],
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
        self.assertEqual(valid["actions"][0]["problem"], "失败后责任集中到单一职责会放大体验压力")
        self.assertEqual(valid["actions"][0]["design_directions"][0], "提供协作状态反馈")
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
                "response_ids": ["source1"],
                "support_basis": "code_filtered_group_qualitative_extraction",
                "profile_presence": [{"profile_name": "性别", "values": ["女性"]}],
            },
        }
        completed = [{
            "id": "c02",
            "title": "女性群体观察",
            "finding_ids": ["group:c02/g1"],
            "group_focus": {"profile_name": "性别", "profile_value": "女性"},
            "blocks": [{"id": "b1", "body": "女性玩家提到协作压力", "finding_ids": ["group:c02/g1"]}],
            "diagnostics": {"status": "complete"},
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

    def test_group_eligibility_uses_valid_blocks_not_allocations_or_other_groups(self):
        groups = [{"profile_name": "语言", "profile_value": value} for value in ("英语", "印尼语")]
        findings = {f"group:{i}/g1": self._group_finding(f"group:{i}/g1", f"src{i}", value)
                    for i, value in enumerate(("英语", "印尼语"))}
        # _group_finding uses the same language dimension as the real contract.
        chapter = {
            "id": "c1", "finding_ids": list(findings), "cross_group_focus": groups,
            "blocks": [
                {"id": "b1", "body": "英语玩家反馈", "finding_ids": ["group:0/g1"]},
                {"id": "b2", "body": "未形成结论", "finding_ids": ["group:1/g1"], "degraded": True},
            ],
            "diagnostics": {"status": "degraded", "attempts": [{"block_recovery": {
                "valid_block_ids": ["b1"], "unresolved_block_ids": ["b2"],
            }}]},
        }
        expected = {"group:0/g1": ("语言", "英语")}
        self.assertEqual(chapters.retained_group_findings(chapter, findings), expected)
        self.assertEqual(chapters._action_group_bindings([chapter], findings), ({("语言", "英语")}, expected))
        for mutation in ("all_failed", "whole_fallback", "unknown_status", "no_source", "unassigned", "mixed"):
            with self.subTest(mutation=mutation):
                modified, fs = deepcopy(chapter), deepcopy(findings)
                if mutation == "all_failed":
                    modified["blocks"][0]["degraded"] = True
                elif mutation == "whole_fallback":
                    modified["diagnostics"]["attempts"] = []
                elif mutation == "unknown_status":
                    modified["diagnostics"] = {}
                elif mutation == "no_source":
                    fs["group:0/g1"]["response_ids"] = []
                elif mutation == "unassigned":
                    modified["finding_ids"] = ["group:1/g1"]
                else:
                    modified["blocks"][0]["finding_ids"].append("group:1/g1")
                self.assertEqual(chapters.retained_group_findings(modified, fs), {})
        # Standalone group chapters use the same actual-block eligibility.
        standalone = deepcopy(chapter)
        standalone.pop("cross_group_focus")
        standalone["group_focus"] = groups[0]
        self.assertEqual(chapters.retained_group_findings(standalone, findings), expected)

    def test_confirmed_scale_bypasses_are_rejected_without_text_deletion(self):
        for text in ("相当数量的描述涉及压力。", "少量反馈提到功能异常。"):
            with self.subTest(text=text):
                normalized, error = chapters.normalize_report_block_text(
                    heading="体验观察", body=text, findings=[{"response_ids": ["src"]}],
                    profiles=[], numeric_sources="",
                )
                self.assertIsNone(normalized)
                self.assertIn("频次", error)
                self.assertEqual(chapters._neutralize_frequency_claims(text), (text, []))

    def test_chapter_and_core_prompts_prioritize_sources_over_summaries(self):
        for prompt in (chapters.DEFAULT_INSIGHT_V3_CHAPTER_WRITER_SYSTEM,
                       chapters.DEFAULT_INSIGHT_V3_CORE_WRITER_SYSTEM):
            self.assertIn("以原文为准", prompt)
            self.assertIn("肯否", prompt)
            self.assertIn("共现", prompt)
            self.assertNotIn("You can Buy item", prompt)

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
    async def test_actions_do_not_receive_or_accept_failed_group_findings(self):
        make = InsightV3ChapterContractTests._group_finding
        findings = {
            "group:en/g1": make("group:en/g1", "s1", "英语"),
            "group:id/g1": make("group:id/g1", "s2", "印尼语"),
            "group:en/g2": make("group:en/g2", "s3", "英语"),
        }
        group = {"profile_name": "语言", "profile_value": "英语"}
        chapter = {
            "id": "c01", "title": "体验", "finding_ids": list(findings),
            "cross_group_focus": [group, {"profile_name": "语言", "profile_value": "印尼语"}],
            "blocks": [
                {"id": "b1", "body": "英语玩家提到信息不清楚", "finding_ids": ["group:en/g1"]},
                {"id": "b2", "body": "未完成", "finding_ids": ["group:id/g1"], "degraded": True},
                {"id": "b3", "body": "未完成", "finding_ids": ["group:en/g2"], "degraded": True},
            ],
            "diagnostics": {"status": "degraded", "attempts": [{"block_recovery": {"valid_block_ids": ["b1"]}}]},
        }
        action = {
            "id": "a01", "title": "补充提示", "action_scope": "group", "target_group": group,
            "problem": "信息不清楚", "objective": "帮助理解", "design_directions": ["补充信息提示"],
            "validation_plan": "试用后访谈核对理解情况", "finding_ids": ["group:en/g1"],
            "evidence_gap": "", "required_data": "",
        }
        calls = []
        async def fake_call(_system, query, **kwargs):
            calls.append(query)
            self.assertIn('"finding_id":"group:en/g1"', query)
            self.assertNotIn('group:id/g1', query)
            self.assertNotIn('group:en/g2', query)
            self.assertIsNone(kwargs['validator']({"actions": [deepcopy(action)]}))
            for bad_id in ("group:id/g1", "group:en/g2"):
                bad = deepcopy(action)
                bad["finding_ids"].append(bad_id)
                self.assertIn("未形成合格人群段落", kwargs['validator']({"actions": [bad]}))
            bad_target = deepcopy(action)
            bad_target["target_group"] = {"profile_name": "语言", "profile_value": "印尼语"}
            self.assertIn("已局部降级", kwargs['validator']({"actions": [bad_target]}))
            return {"data": {"actions": [deepcopy(action)]}, "model": "mock"}
        with patch.object(chapters, "_direct_json_call", new=fake_call):
            output = await chapters.write_actions(
                chapters=[chapter], core_blocks=[], findings_by_id=findings,
                profiles=[], qualitative_context={},
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(output["actions"][0]["finding_ids"], ["group:en/g1"])

    async def test_planner_prompt_exposes_stat_limit_and_task_binding(self):
        result = {
            "report_title": "体验", "chapters": [{
                "id": "c01", "title": "体验问题", "purpose": "解释问题",
                "finding_ids": ["f"], "stat_ids": [], "group_focus": None,
            }], "suspected_skews": [], "research_tasks": [],
        }

        async def call(_system, query, **kwargs):
            self.assertIn("每章 stat_ids 最多 3 个", query)
            self.assertIn("research_tasks.stat_ids 必须已分配", query)
            self.assertIsNone(kwargs["validator"](result))
            return {"data": result}

        with patch.object(chapters, "_direct_json_call", side_effect=call):
            _, diagnostics = await chapters.plan_chapters(
                findings=[{"id": "f", "claim": "操作受阻"}], plan={},
                qualitative_context={}, stats_catalog=[], profiles=[],
            )
        self.assertEqual(diagnostics["logical_attempts"], 1)

    async def test_invalid_sample_interpretation_does_not_discard_valid_core(self):
        from unittest.mock import AsyncMock

        for sample in (True, {"body": "不合契约"}, "说明" * 251):
            with self.subTest(sample_type=type(sample).__name__):
                data = {"chapter_id": "core", "sample_overview": sample, "blocks": [{
                    "id": "b1", "heading": "选择负担", "body": "选卡时需要暂时转移注意力。",
                    "finding_ids": ["1/f1"], "evidence_ids": ["r0001"],
                }]}

                async def fake_call(_system, _query, **kwargs):
                    self.assertIsNone(kwargs["validator"](data))
                    return {"data": data, "model": "mock-writer"}

                with patch.object(chapters, "_direct_json_call", new=fake_call), patch.object(
                    chapters, "_translate_evidence", new=AsyncMock(return_value=deepcopy(data["blocks"])),
                ):
                    result = await chapters._write_one_section(
                        chapter={"id": "core", "title": "核心", "purpose": "解释", "finding_ids": ["1/f1"], "stat_ids": [], "sample_stats": []},
                        findings_by_id={"1/f1": {"id": "1/f1", "claim": "选卡转移注意力", "response_ids": ["global-one"]}},
                        source_registry={"global-one": {"text": "选卡时会移开视线。"}},
                        stats_by_id={}, profiles=[], qualitative_context={}, translation_namespace="sample-test",
                    )
                self.assertEqual(result["diagnostics"]["status"], "complete")
                self.assertEqual(result["diagnostics"]["sample_status"], "fallback")
                self.assertIsNone(result["sample_overview"])
                self.assertEqual(len(result["blocks"]), 1)

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


class LocalBlockRecoveryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def fixture():
        findings = {f"f{i}": {"id": f"f{i}", "claim": "信息难以阅读", "kind": "risk",
                              "response_ids": [f"global-{i}"]} for i in (1, 2, 3)}
        registry = {f"global-{i}": {"text": '文字标记"r0001"难以阅读。', "respondent_ref": f"player-{i}"}
                    for i in (1, 2, 3)}
        chapter = {"id": "c01", "title": "阅读体验", "purpose": "解释", "finding_ids": ["f1", "f2"], "stat_ids": []}
        def block(i, body="有玩家提到文字难以阅读。"):
            return {"id": f"b{i}", "heading": "阅读困难", "body": body,
                    "finding_ids": [f"f{i}"], "evidence_ids": [f"r{i:04d}"]}
        return findings, registry, chapter, block

    async def run_section(self, results, *, chapter=None, maximum=5, group_fixture=False):
        from unittest.mock import AsyncMock
        findings, registry, default_chapter, _ = self.fixture()
        if group_fixture:
            for index, value in ((2, "英语"), (3, "印尼语")):
                findings[f"f{index}"] = InsightV3ChapterContractTests._group_finding(
                    f"f{index}", f"global-{index}", value,
                )
        queries = []
        async def call(_system, query, **kwargs):
            queries.append(query)
            result = deepcopy(results[min(len(queries) - 1, len(results) - 1)])
            error = kwargs["validator"](result)
            return {"data": result if not error else None, "error": error or "", "model": "mock"}
        async def translate(**kwargs):
            return deepcopy(kwargs["blocks"])
        with patch.object(chapters, "_direct_json_call", new=call), patch.object(
            chapters, "_translate_evidence", new=translate,
        ), patch.object(chapters, "INSIGHT_V3_MAX_BLOCKS_PER_CHAPTER", maximum):
            result = await chapters._write_one_section(
                chapter=chapter or default_chapter, findings_by_id=findings,
                source_registry=registry, stats_by_id={}, profiles=[], qualitative_context={},
                translation_namespace="isolated-block-test",
            )
        return result, queries

    def test_existence_needs_source_and_does_not_exempt_scale_claim(self):
        for body, sourced, allowed in (
            ("有玩家提到选卡困难。", True, True),
            ("有受访者提到选卡困难。", True, True),
            ("有玩家提到选卡困难。", False, False),
            ("所有玩家都认可。", True, False),
            ("有玩家提到所有玩家都认可。", True, False),
            ("有玩家提到问题，部分玩家认为如此。", True, False),
            ("有玩家认为该现象普遍存在。", True, False),
        ):
            with self.subTest(body=body, sourced=sourced):
                result, error = chapters.normalize_report_block_text(
                    heading="发现", body=body, findings=[{"response_ids": ["r"]}] if sourced else [],
                    profiles=[], numeric_sources="",
                )
                self.assertEqual(error is None, allowed)
                if allowed:
                    self.assertEqual(result["body"], body)

    def test_inline_source_marker_gets_actionable_error_even_when_digits_are_known(self):
        for marker in ("[r0001]", "（r002）", "r001"):
            result, error = chapters.normalize_report_block_text(
                heading="发现", body="有玩家反馈难以阅读。" + marker,
                findings=[{"response_ids": ["global"]}], profiles=[], numeric_sources="0001 002 001",
            )
            self.assertIsNone(result)
            self.assertIn("引用只放 evidence_ids", error)

    def test_empty_stats_request_does_not_offer_a_fake_statistic_example(self):
        findings, registry, chapter, _ = self.fixture()
        request = chapters.build_chapter_writer_request(
            chapter=chapter, findings_by_id=findings, source_registry=registry,
            stats_by_id={}, qualitative_context={},
        )
        self.assertIn("本章 stat_facts 为空", request["query"])
        self.assertIn("题名、audience和caveat中的人数", request["query"])
        self.assertNotIn("[S:s001-r001-c002]", request["query"])

    def test_block_recovery_does_not_relax_cross_group_or_envelope_guards(self):
        findings = {"en": InsightV3ChapterContractTests._group_finding("en", "global-1", "英语"),
                    "id": InsightV3ChapterContractTests._group_finding("id", "global-2", "印尼语")}
        kwargs = dict(chapter_id="c01", assigned_finding_ids=set(findings), required_finding_ids=set(),
                      valid_short_ids={"r0001", "r0002"}, allowed_by_short={"r0001": {"en"}, "r0002": {"id"}},
                      findings_by_id=findings, profiles=[], numeric_sources="",
                      cross_group_keys={("语言", "英语"), ("语言", "印尼语")})
        data = {"chapter_id": "c01", "blocks": [{"id": "b1", "heading": "英语视角", "body": "有玩家提到阅读困难。",
                "finding_ids": ["en"], "evidence_ids": ["r0001"]}]}
        self.assertIsNone(chapters._validate_writer_output(deepcopy(data), **kwargs, check_coverage=False))
        self.assertIn("遗漏人群", chapters._validate_writer_output(deepcopy(data), **kwargs))
        mixed = deepcopy(data)
        mixed["blocks"][0]["finding_ids"].append("id")
        self.assertIn("混合", chapters._validate_writer_output(mixed, **kwargs, check_coverage=False))
        duplicate = deepcopy(data)
        duplicate["blocks"].append(deepcopy(duplicate["blocks"][0]))
        self.assertIn("重复", chapters._writer_envelope_error(duplicate, chapter_id="c01", maximum_blocks=5))

    async def test_only_failed_block_is_repaired_without_renumbering_sources(self):
        import json
        _, _, _, block = self.fixture()
        good, bad = block(1), block(2, "部分玩家认为文字难读。")
        fixed = block(2, "有受访者反馈字体难以辨认。")
        result, queries = await self.run_section([
            {"chapter_id": "c01", "blocks": [good, bad]},
            {"chapter_id": "c01", "blocks": [fixed]},
        ])
        self.assertEqual(len(queries), 2)
        self.assertEqual(result["blocks"][0]["body"], good["body"])
        self.assertEqual(result["blocks"][1]["body"], fixed["body"])
        self.assertEqual(result["diagnostics"]["status"], "complete")
        sources = json.loads(queries[1].split("<evidence_sources>\n")[1].split("\n</evidence_sources>")[0])
        self.assertEqual([item["response_id"] for item in sources], ["r0002"])
        self.assertEqual(sources[0]["text"], '文字标记"r0001"难以阅读。')
        self.assertNotIn("global-", queries[1])
        self.assertEqual(result["diagnostics"]["attempts"][-1]["block_recovery"]["valid_block_ids"], ["b1", "b2"])

    async def test_no_progress_keeps_good_block_and_marks_only_failed_block(self):
        _, _, _, block = self.fixture()
        good, bad = block(1), block(2, "所有玩家都认可。")
        result, queries = await self.run_section([
            {"chapter_id": "c01", "blocks": [good, bad]},
            {"chapter_id": "c01", "blocks": [bad]},
        ])
        self.assertEqual(len(queries), 2)
        self.assertEqual(result["blocks"][0]["body"], good["body"])
        self.assertTrue(result["blocks"][1]["degraded"])
        self.assertNotIn("所有玩家", result["blocks"][1]["body"])
        self.assertEqual(result["diagnostics"]["reason"], "partial_repair_no_progress")
        self.assertEqual(result["diagnostics"]["attempts"][-1]["block_recovery"]["uncovered_required_finding_ids"], ["f2"])

    async def test_repair_cannot_replace_frozen_block_or_change_chapter(self):
        _, _, _, block = self.fixture()
        for bad_repair in (
            {"chapter_id": "c01", "blocks": [block(1, "替换已冻结内容。"), block(2)]},
            {"chapter_id": "c99", "blocks": [block(2)]},
        ):
            result, queries = await self.run_section([
                {"chapter_id": "c01", "blocks": [block(1), block(2, "部分玩家抱怨。") ]}, bad_repair,
            ])
            self.assertEqual(len(queries), 2)
            self.assertEqual(result["blocks"][0]["body"], block(1)["body"])
            self.assertEqual(result["diagnostics"]["status"], "degraded")

    async def test_missing_required_material_uses_a_free_slot_not_rewrite(self):
        _, _, _, block = self.fixture()
        extra = block(2)
        extra["id"] = "coverage-gap"
        result, queries = await self.run_section([
            {"chapter_id": "c01", "blocks": [block(1)]},
            {"chapter_id": "c01", "blocks": [extra]},
        ])
        self.assertEqual(len(queries), 2)
        self.assertEqual(result["diagnostics"]["status"], "complete")
        self.assertEqual(result["blocks"][0]["body"], block(1)["body"])

    async def test_no_slot_means_visible_gap_without_extra_calls(self):
        _, _, _, block = self.fixture()
        result, queries = await self.run_section([{ "chapter_id": "c01", "blocks": [block(1)]}], maximum=1)
        self.assertEqual(len(queries), 1)
        self.assertEqual(len(result["blocks"]), 1)
        self.assertTrue(result["blocks"][0]["body"].startswith(block(1)["body"]))
        self.assertIn("本章分析缺口", result["blocks"][0]["body"])
        self.assertEqual(result["diagnostics"]["status"], "degraded")

    async def test_missing_groups_get_separate_repair_slots(self):
        _, _, chapter, block = self.fixture()
        chapter["finding_ids"] = ["f1", "f2", "f3"]
        chapter["cross_group_focus"] = [
            {"profile_name": "语言", "profile_value": value} for value in ("英语", "印尼语")
        ]
        group_blocks = [block(3), block(2)]  # Gap order follows the stable group sort.
        for item, value, suffix in zip(group_blocks, ("印尼语", "英语"), ("", "-next")):
            item.update(id="coverage-gap" + suffix, heading=value + "视角")
        result, queries = await self.run_section([
            {"chapter_id": "c01", "blocks": [block(1)]},
            {"chapter_id": "c01", "blocks": group_blocks},
        ], chapter=chapter, group_fixture=True)
        self.assertEqual(len(queries), 2)
        self.assertEqual(result["diagnostics"]["status"], "complete")
        self.assertEqual([b["finding_ids"] for b in result["blocks"]], [["f1"], ["f3"], ["f2"]])

    async def test_progress_never_exceeds_original_three_logical_calls(self):
        _, _, chapter, block = self.fixture()
        chapter["finding_ids"] = ["f1", "f2", "f3"]
        result, queries = await self.run_section([
            {"chapter_id": "c01", "blocks": [block(i, "部分玩家反馈。") for i in (1, 2, 3)]},
            {"chapter_id": "c01", "blocks": [block(1), block(2, "部分玩家反馈。"), block(3, "部分玩家反馈。")]},
            {"chapter_id": "c01", "blocks": [block(2), block(3, "部分玩家反馈。")]},
        ], chapter=chapter)
        self.assertEqual(len(queries), 3)
        self.assertEqual(result["diagnostics"]["status"], "degraded")
        self.assertEqual(len([b for b in result["blocks"] if not b.get("degraded")]), 2)

    async def test_malformed_bound_ids_are_local_failures_not_exceptions(self):
        _, _, _, block = self.fixture()
        bad = block(2)
        bad["finding_ids"] = [{"unknown": True}]
        result, _ = await self.run_section([
            {"chapter_id": "c01", "blocks": [block(1), bad]},
            {"chapter_id": "c01", "blocks": [block(2)]},
        ])
        self.assertEqual(result["diagnostics"]["status"], "complete")


class ResearchQualityContractTests(unittest.TestCase):
    def test_important_concrete_detail_is_mandatory_and_focus_is_transmitted(self):
        chapter = {"id": "c01", "title": "体验", "purpose": "解释矛盾", "finding_ids": ["f"], "stat_ids": [], "analysis_focus": {"core_question": "好玩为什么不优先选择"}}
        prepared = chapters.build_chapter_writer_request(
            chapter=chapter, findings_by_id={"f": {"id": "f", "claim": "具体操作异常", "kind": "fact", "preserve_reason": "specific_issue", "response_ids": ["global"]}},
            source_registry={"global": {"text": "点击后无效"}}, stats_by_id={}, qualitative_context={},
        )
        self.assertEqual(prepared["required_finding_ids"], {"f"})
        self.assertIn("好玩为什么不优先选择", prepared["query"])
        self.assertNotIn('"evidence_points"', prepared["query"])
        self.assertIn("<output_check>", prepared["query"])
        self.assertIn("不是可照抄的正文句式", prepared["query"])
        self.assertIn("[S:短统计ID]", prepared["query"])

    def test_objective_task_is_not_misclassified_as_group_chapter(self):
        plan = {"analysis_focus": {"expected_deliverables": ["说明不同经历人群的评价差异及选择原因"]}}
        resolved, gaps = chapters.required_group_chapters(
            plan=plan, profiles=[{"profile_name": "此前经历", "values": ["是", "否"]}],
            research_tasks=[{"deliverable_index": 0, "kind": "objective_analysis", "chapter_ids": ["c01"], "stat_ids": ["s001"], "gap": ""}],
        )
        self.assertEqual((resolved, gaps), ([], []))
        _, gaps = chapters.required_group_chapters(plan=plan, profiles=[])
        self.assertEqual(len(gaps), 1)

    def test_planner_cannot_claim_objective_task_without_assigned_stats(self):
        data = {"report_title": "研究", "chapters": [{"id": "c01", "title": "体验关系", "purpose": "比较", "finding_ids": ["f"], "stat_ids": [], "group_focus": None}], "suspected_skews": [], "research_tasks": [{"deliverable_index": 0, "kind": "objective_analysis", "chapter_ids": ["c01"], "stat_ids": ["s001"], "gap": ""}]}
        args = dict(finding_ids={"f"}, non_degraded_ids={"f"}, stat_ids={"s001"}, profiles=[], allow_group_chapters=False, expected_deliverables=["比较"])
        self.assertIn("没有分配", chapters._validate_chapter_plan(data, **args))
        data["chapters"][0]["stat_ids"] = ["s001"]
        self.assertIsNone(chapters._validate_chapter_plan(data, **args))


if __name__ == "__main__":
    unittest.main()
