from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from app.services.insight_report_v3 import regeneration


def _plan():
    return {
        "columns": [
            {"index": 0, "name": "语言", "role": "single_choice", "use_as_profile": True,
             "profile_scope": "analysis", "options": ["英语", "印尼语"]},
            {"index": 1, "name": "体验反馈", "role": "open_text"},
        ],
        "parts": [{"name": "体验", "column_indexes": [0, 1]}],
        "cross_tabs": [],
        "open_questions": [],
    }


def _call_result(data, *, model="planner-model", error=""):
    return {
        "data": data,
        "model": model,
        "raw_len": 10,
        "repaired": False,
        "error": error,
        "duration_seconds": 0.1,
    }


async def _run(instruction: str, plan=None):
    events = []
    async for event in regeneration.prepare_insight_regeneration(
        instruction=instruction,
        plan=deepcopy(plan or _plan()),
        qualitative_context={"problem": "理解语言相关体验"},
    ):
        events.append(event)
    return events, next(payload for kind, payload in events if kind == "result")


class InsightV3RegenerationTests(unittest.IsolatedAsyncioTestCase):
    def test_native_language_negations_are_local_and_conservative(self):
        negative = [
            "不涉及母语分析", "本报告不分析母语差异", "不得推断母语差异",
            "不要把客户端语言理解为玩家母语", "没有母语字段", "需要补充玩家母语",
            "Do not infer native language", "No analysis of mother tongue",
            "Do not treat client language as native language",
            "不将其解释为母语、国籍或地区",
            "保持客户端语言原始含义，不将其解释为母语、国籍或地区。",
            "不把客户端语言推断为玩家母语",
        ]
        positive = [
            "按母语比较", "不分析母语，但比较母语英语和印尼语玩家",
            "不要比较性别；请分析母语", "不得推断母语差异，按母语分组",
            "不能不分析母语差异", "不是不分析母语差异", "没有理由不分析母语",
            "不能没有母语分析", "不要忽略母语差异", "不是没有母语差异", "并非没有母语差异",
            "No native language field, but compare native speakers",
            "Do not ignore native language", "Do not not analyze native language",
            "不分析母语差异并比较母语英语与印尼语玩家",
            "不将其解释为母语，但仍要比较母语差异",
            "不将其解释为母语并按母语比较两组",
            "不将性别当作客户端设置并将客户端语言解释为母语",
            "不将性别当作客户端设置而将客户端语言解释为母语",
            "Do not treat gender as settings and interpret client language as native language",
        ]
        for text in negative:
            with self.subTest(negative=text):
                self.assertFalse(regeneration._requests_native_language(text))
        for text in positive:
            with self.subTest(positive=text):
                self.assertTrue(regeneration._requests_native_language(text))

    async def test_focus_checks_each_positive_field_and_list_item_independently(self):
        classification = {
            "categories": ["analysis_focus"], "supported": True, "capability": "supported",
            "reason": "按性别组织", "normalized_request": "分别写男女玩家",
        }
        focus = {
            "core_question": "不涉及母语分析", "report_organization": "按主题组织",
            "supporting_analyses": ["本报告不分析母语差异"],
            "evidence_role": "原文支撑", "expected_deliverables": ["体验报告"],
            "avoid_structures": ["母语分组", "不得推断母语差异"],
        }
        for field in (None, "report_organization", "expected_deliverables", "supporting_analyses"):
            with self.subTest(field=field):
                candidate = deepcopy(focus)
                if field:
                    candidate[field] = (["不涉及母语分析", "按母语分别写结论"]
                                        if isinstance(candidate[field], list) else "按母语分别写结论")
                caller = AsyncMock(side_effect=[
                    _call_result(classification), _call_result({"analysis_focus": candidate}),
                ])
                with patch.object(regeneration, "_direct_json_call", new=caller):
                    _events, result = await _run("分别写男女玩家")
                if field is None:
                    self.assertTrue(result["supported"])
                    self.assertEqual(result["effective_plan"]["analysis_focus"], candidate)
                else:
                    self.assertFalse(result["supported"])
                    self.assertIsNone(result["effective_plan"])
                    diag = result["diagnostics"]
                    self.assertEqual(diag["candidate_analysis_focus"], candidate)
                    self.assertIs(diag["candidate_applied"], False)
                    self.assertEqual(diag["rejection"]["code"], "dimension_mismatch")
                    self.assertTrue(diag["dimension_issues"][0]["field"].startswith(field))
                    # Successful revision transport is not relabelled as an LLM error.
                    self.assertEqual(diag["plan_revision_attempt"]["error"], "")

    async def test_failed_call_preserves_error_without_inferring_timeout_from_duration(self):
        for error in ("TimeoutError", "invalid JSON"):
            response = _call_result(None, error=error)
            response.update(duration_seconds=301.111, initial_duration_seconds=300.0,
                            repair_duration_seconds=1.111, raw_len=128, repaired=True)
            with patch.object(regeneration, "_direct_json_call", new=AsyncMock(return_value=response)):
                _events, result = await _run("按新的章节结构写")
            diag = result["diagnostics"]
            self.assertEqual(diag["classification_attempt"]["error"], error)
            self.assertEqual(diag["classification_attempt"]["raw_len"], 128)
            self.assertEqual(diag["rejection"]["code"], "classification_failed")
            self.assertIsNone(diag["original_classification"])
            self.assertIn("分类未成功", result["error_message"])
            self.assertEqual(diag["logical_calls"], 1)

    async def test_revision_failure_preserves_successful_classification(self):
        classification = {
            "categories": ["chapter_structure"], "supported": True, "capability": "supported",
            "reason": "变更结构", "normalized_request": "先写阻力",
        }
        caller = AsyncMock(side_effect=[_call_result(classification), _call_result(None, error="invalid JSON")])
        with patch.object(regeneration, "_direct_json_call", new=caller):
            _events, result = await _run("先写阻力")
        diag = result["diagnostics"]
        self.assertEqual(diag["classification"], classification)
        self.assertEqual(diag["original_classification"], classification)
        self.assertEqual(diag["rejection"]["stage"], "plan_revision")
        self.assertEqual(diag["plan_revision_attempt"]["error"], "invalid JSON")

    def test_request_to_avoid_native_inference_is_not_a_native_group_request(self):
        self.assertIsNone(regeneration._deterministic_unsupported(
            "按照实际字段写，不要把客户端语言理解为玩家母语", _plan(),
        ))

    def test_deterministic_guard_does_not_confuse_content_split_with_two_groups(self):
        self.assertIsNone(regeneration._deterministic_unsupported(
            "每个章节分别写玩家问题和建议"
        ))

    def test_actual_client_request_resolves_each_thematic_chapter(self):
        plan = _plan()
        plan["columns"][0].update({"name": "MLBB客户端语言", "options": ["英语", "印尼语", "英语和印尼语"]})
        for scope in ("每个主题章节", "各分析章节", "所有报告章节", "跨章节"):
            with self.subTest(scope=scope):
                instruction = f"{scope}按照现有的MLBB客户端语言字段分组，分别呈现英语客户端玩家与印尼语客户端玩家的反馈和结论。"
                requested, groups = regeneration._resolve_cross_chapter_groups(instruction, plan)
                self.assertTrue(requested)
                self.assertEqual(groups, [
                    {"profile_name": "MLBB客户端语言", "profile_value": "英语"},
                    {"profile_name": "MLBB客户端语言", "profile_value": "印尼语"},
                ])
                query = regeneration._focus_revision_query(
                    instruction=instruction,
                    classification={"categories": ["chapter_structure"], "normalized_request": instruction},
                    plan=plan, qualitative_context={},
                )
                self.assertIn('"requested_cross_chapter_groups":[{"profile_name":"MLBB客户端语言","profile_value":"英语"}', query)

    async def test_two_resolved_cross_chapter_groups_revise_focus_and_are_supported(self):
        classification = {
            "categories": ["chapter_structure", "analysis_focus"],
            "supported": True,
            "capability": "supported",
            "reason": "按实际语言画像组织两组材料",
            "normalized_request": "每个章节分别呈现英语与印尼语玩家",
        }
        focus = {
            "core_question": "两组玩家分别关注什么",
            "report_organization": "按主题组织",
            "supporting_analyses": ["结合开放题证据"],
            "evidence_role": "原话支撑定性归纳",
            "expected_deliverables": ["主题洞察"],
            "avoid_structures": ["不得输出观点级比例"],
        }
        caller = AsyncMock(side_effect=[
            _call_result(classification, model="classifier"),
            _call_result({"analysis_focus": focus}, model="reviser"),
        ])
        instruction = "我需要每个part都区分语言为英语的玩家和语言为印尼语的玩家，分别写结论。"
        with patch.object(regeneration, "_direct_json_call", new=caller):
            _events, result = await _run(instruction)

        self.assertTrue(result["supported"])
        self.assertEqual(result["diagnostics"]["classification"]["capability"], "supported")
        self.assertEqual(result["diagnostics"]["logical_calls"], 2)
        self.assertEqual(result["diagnostics"]["cross_chapter_groups"], [
            {"profile_name": "语言", "profile_value": "英语"},
            {"profile_name": "语言", "profile_value": "印尼语"},
        ])
        revised = result["effective_plan"]["analysis_focus"]
        self.assertIn("英语", revised["report_organization"])
        self.assertIn("印尼语", revised["report_organization"])
        self.assertTrue(any("每个主题章节" in item for item in revised["expected_deliverables"]))

    async def test_client_language_cannot_satisfy_native_language_request(self):
        for name in ("MLBB客户端语言", "语言", "界面语言"):
            with self.subTest(field=name):
                plan = _plan()
                plan["columns"][0]["name"] = name
                caller = AsyncMock(side_effect=AssertionError("missing dimension must reject before model"))
                with patch.object(regeneration, "_direct_json_call", new=caller):
                    _events, result = await _run(
                        "每个 part 都区分母语英语和母语印尼语玩家，分别写结论。", plan,
                    )
                self.assertFalse(result["supported"])
                self.assertIn("没有母语画像字段", result["error_message"])
                self.assertEqual(result["diagnostics"]["logical_calls"], 0)
                self.assertEqual(result["diagnostics"]["cross_chapter_groups"], [])

    def test_native_language_matches_native_field_when_client_options_are_identical(self):
        plan = _plan()
        plan["columns"][0]["name"] = "MLBB客户端语言"
        native = deepcopy(plan["columns"][0])
        native.update({"index": 2, "name": "玩家母语"})
        plan["columns"].append(native)
        requested, groups = regeneration._resolve_cross_chapter_groups(
            "每章分别写母语为英语的玩家与母语为印尼语的玩家", plan,
        )
        self.assertTrue(requested)
        self.assertEqual({item["profile_name"] for item in groups}, {"玩家母语"})

    async def test_matching_groups_do_not_override_invalid_or_data_scope_classification(self):
        cases = [
            _call_result(None, error="invalid JSON"),
            _call_result({
                "categories": ["chapter_structure", "data_scope"],
                "supported": False, "capability": "data_scope_change",
                "reason": "同时要求筛除样本", "normalized_request": "分组并只保留新玩家",
            }),
        ]
        for response in cases:
            with self.subTest(response=response["error"]):
                caller = AsyncMock(return_value=response)
                with patch.object(regeneration, "_direct_json_call", new=caller):
                    _events, result = await _run("每个part分别写英语与印尼语玩家，只保留新玩家")
                self.assertFalse(result["supported"])
                self.assertIsNone(result["effective_plan"])
                self.assertEqual(caller.await_count, 1)

    async def test_revised_focus_cannot_invent_native_language_dimension(self):
        classification = {
            "categories": ["analysis_focus"], "supported": True, "capability": "supported",
            "reason": "按客户端语言分组", "normalized_request": "客户端语言分组",
        }
        focus = {
            "core_question": "母语英语与印尼语玩家关注什么",
            "report_organization": "按主题组织", "supporting_analyses": [],
            "evidence_role": "原文支撑", "expected_deliverables": ["主题报告"],
            "avoid_structures": [],
        }
        caller = AsyncMock(side_effect=[_call_result(classification), _call_result({"analysis_focus": focus})])
        with patch.object(regeneration, "_direct_json_call", new=caller):
            _events, result = await _run("每章分别写英语与印尼语玩家")
        self.assertFalse(result["supported"])
        self.assertIn("画像含义错配", result["error_message"])
        self.assertIsNone(result["effective_plan"])

    async def test_cross_chapter_request_with_three_groups_is_rejected_before_model(self):
        plan = _plan()
        plan["columns"][0]["options"].append("泰语")
        caller = AsyncMock(side_effect=AssertionError("unresolved groups must not call model"))
        with patch.object(regeneration, "_direct_json_call", new=caller):
            _events, result = await _run(
                "每个 part 分别写英语、印尼语和泰语玩家的结论",
                plan,
            )

        self.assertFalse(result["supported"])
        self.assertEqual(
            result["diagnostics"]["classification"]["capability"],
            "cross_chapter_multi_group",
        )
        self.assertEqual(result["diagnostics"]["logical_calls"], 0)

    async def test_wording_only_request_keeps_plan_and_uses_one_classification_call(self):
        classification = {
            "categories": ["wording"],
            "supported": True,
            "capability": "supported",
            "reason": "只调整语言风格",
            "normalized_request": "改为更简洁的业务表达",
        }
        caller = AsyncMock(return_value=_call_result(classification))
        original = _plan()
        with patch.object(regeneration, "_direct_json_call", new=caller):
            _events, result = await _run("写得更简洁一些", original)

        self.assertTrue(result["supported"])
        self.assertEqual(result["effective_plan"], original)
        self.assertEqual(result["planning_instruction"], "")
        self.assertEqual(result["report_instruction"], "写得更简洁一些")
        self.assertEqual(result["diagnostics"]["status"], "wording_only")
        self.assertEqual(result["diagnostics"]["logical_calls"], 1)
        self.assertEqual(caller.await_count, 1)

    async def test_structure_request_revises_focus_before_chapter_planning(self):
        classification = {
            "categories": ["chapter_structure", "analysis_focus"],
            "supported": True,
            "capability": "supported",
            "reason": "改变章节顺序和交付内容",
            "normalized_request": "先写关键阻力，再写机会点",
        }
        focus = {
            "core_question": "哪些体验阻力最值得优先处理",
            "report_organization": "先按关键阻力组织章节，再归纳机会点",
            "supporting_analyses": ["结合开放题证据说明具体场景"],
            "evidence_role": "原话支撑判断，统计只说明样本结构",
            "expected_deliverables": ["关键阻力清单", "对应机会点"],
            "avoid_structures": ["不得按问卷题目机械逐题罗列"],
        }
        caller = AsyncMock(side_effect=[
            _call_result(classification, model="classifier"),
            _call_result({"analysis_focus": focus}, model="reviser"),
        ])
        original = _plan()
        with patch.object(regeneration, "_direct_json_call", new=caller):
            _events, result = await _run("先写关键阻力，再写机会点", original)

        self.assertTrue(result["supported"])
        self.assertEqual(result["effective_plan"]["analysis_focus"], focus)
        self.assertEqual(result["effective_plan"]["parts"], original["parts"])
        self.assertEqual(result["planning_instruction"], "先写关键阻力，再写机会点")
        self.assertEqual(result["diagnostics"]["status"], "plan_revised")
        self.assertEqual(result["diagnostics"]["logical_calls"], 2)
        self.assertEqual(result["diagnostics"]["models"], ["classifier", "reviser"])
        self.assertEqual(caller.await_count, 2)

    async def test_data_scope_request_is_explicitly_rejected(self):
        classification = {
            "categories": ["data_scope"],
            "supported": False,
            "capability": "data_scope_change",
            "reason": "要求排除部分样本",
            "normalized_request": "只分析最近一个月的玩家",
        }
        with patch.object(
            regeneration,
            "_direct_json_call",
            new=AsyncMock(return_value=_call_result(classification)),
        ):
            _events, result = await _run("只分析最近一个月的玩家")

        self.assertFalse(result["supported"])
        self.assertIn("数据与分析方案确认步骤", result["error_message"])
        self.assertEqual(result["diagnostics"]["status"], "rejected")

    async def test_failed_classification_does_not_fall_through_to_old_plan(self):
        with patch.object(
            regeneration,
            "_direct_json_call",
            new=AsyncMock(return_value=_call_result(None, error="invalid JSON")),
        ):
            _events, result = await _run("按我的新思路彻底改一下")

        self.assertFalse(result["supported"])
        self.assertIsNone(result["effective_plan"])
        self.assertEqual(
            result["diagnostics"]["classification"]["capability"],
            "ambiguous_or_other",
        )


if __name__ == "__main__":
    unittest.main()
