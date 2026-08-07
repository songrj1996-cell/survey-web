from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.core import config
from app.schemas.requests import QualitativeContextRequest
from app.services import analysis_preferences, survey_service
from app.storage import analysis_presets


def _focus(label: str = "主线") -> dict:
    return {
        "core_question": f"{label}核心问题",
        "report_organization": f"{label}组织方式",
        "supporting_analyses": [f"{label}支撑分析"],
        "evidence_role": f"{label}证据角色",
        "expected_deliverables": [f"{label}交付物"],
        "avoid_structures": [f"避免{label}旧结构"],
    }


def _session(
    owner_key: str = "email:owner@example.com",
    *,
    questionnaire_authoritative: bool = False,
) -> dict:
    session = {
        "owner_key": owner_key,
        "rows": [
            ["MLBBID", " Ｑ１　整体满意度 ", "功能评价 [ 速度 ]", "功能评价 [ 稳定性 ]", "备注"],
            ["1001", "满意", "4", "5", ""],
            ["1002", "一般", "3", "4", ""],
        ],
        "confirmed_columns": [
            {
                "name_zh": "玩家编号",
                "role": "id",
                "column_indexes": [0],
                "confidence": 0.99,
            },
            {
                "name_zh": "整体满意度",
                "role": "single_choice",
                "column_indexes": [1],
                "options": ["满意", "一般"],
                "options_original": ["满意", "一般"],
                "confidence": 0.93,
            },
            {
                "name_zh": "功能评价",
                "role": "matrix_scale",
                "column_indexes": [2, 3],
                "rows": [" 速度 ", "稳定性"],
                "scale_min": 1,
                "scale_max": 5,
                "confidence": 0.91,
            },
            {
                "name_zh": "备注",
                "role": "ignore",
                "column_indexes": [4],
            },
        ],
        "qualitative_context": {
            "problem": "了解满意度",
            "background": "新功能灰度中",
            "target_users": "活跃玩家",
            "key_concerns": "问题原因",
            "report_usage": "版本复盘",
            "analysis_approach": "按体验链路展开（不得持久化进 context）",
        },
        "plan": {
            "analysis_focus": _focus(),
            "parts": [{"name": "不得持久化", "column_indexes": [1]}],
        },
        "plan_revision_texts": ["历史修订一", "历史修订二"],
        "stats_md": "不得持久化的统计",
        "report_md": "不得持久化的报告",
    }
    if questionnaire_authoritative:
        session["column_provider"] = "questionnaire"
    return session


class AnalysisPresetStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "nested" / "analysis_presets.json"
        self.config_patch = patch.object(
            config,
            "ANALYSIS_PRESETS_FILE",
            str(self.path),
            create=True,
        )
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.temp_dir.cleanup()

    def test_missing_file_returns_empty_document_without_creating_it(self):
        loaded = analysis_presets.load_analysis_presets()

        self.assertEqual(
            loaded,
            {"schema_version": 1, "revision": 0, "presets": []},
        )
        self.assertFalse(self.path.exists())

    def test_corrupt_or_unsupported_document_is_not_overwritten(self):
        self.path.parent.mkdir(parents=True)
        invalid_documents = (
            "{broken json",
            json.dumps({"schema_version": 2, "revision": 3, "presets": []}),
        )
        for raw in invalid_documents:
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                before = self.path.read_bytes()
                called = False

                def mutator(_presets):
                    nonlocal called
                    called = True

                with self.assertRaises(analysis_presets.AnalysisPresetStorageError):
                    analysis_presets.load_analysis_presets()
                with self.assertRaises(analysis_presets.AnalysisPresetStorageError):
                    analysis_presets.mutate_analysis_presets(mutator)

                self.assertFalse(called)
                self.assertEqual(self.path.read_bytes(), before)


class AnalysisPresetFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_across_non_semantic_session_changes(self):
        original = _session()
        changed = deepcopy(original)
        changed["rows"] = [
            ["新增忽略列", "MLBBID", "Q1   整体满意度", "功能评价 [ 速度 ]", "功能评价 [ 稳定性 ]", "备注"],
            ["", "9999", "满意", "5", "4", ""],
        ]
        changed["confirmed_columns"] = [
            {"role": "ignore", "column_indexes": [0]},
            {"role": "mlbbid", "column_indexes": [1]},
            {
                "role": "SINGLE_CHOICE",
                "column_indexes": [2],
                "options": ["完全不同的推断选项"],
                "options_original": ["也不参与指纹"],
                "confidence": 0.01,
            },
            {
                "role": "matrix_scale",
                "column_indexes": [3, 4],
                "rows": ["速度", "  稳定性  "],
                "scale_min": "1",
                "scale_max": "5",
                "confidence": 0.02,
            },
            {"role": "ignore", "column_indexes": [5]},
        ]

        first = analysis_preferences.build_analysis_preset_fingerprint(original)
        second = analysis_preferences.build_analysis_preset_fingerprint(changed)

        self.assertEqual(first, second)
        self.assertRegex(first or "", r"^v1:[0-9a-f]{64}$")

    def test_semantic_questionnaire_changes_change_the_fingerprint(self):
        base = _session(questionnaire_authoritative=True)
        original = analysis_preferences.build_analysis_preset_fingerprint(base)
        mutations = {
            "question_order": lambda value: value["confirmed_columns"].__setitem__(
                slice(1, 3),
                [value["confirmed_columns"][2], value["confirmed_columns"][1]],
            ),
            "confirmed_role": lambda value: value["confirmed_columns"][1].__setitem__(
                "role", "open_text"
            ),
            "original_header": lambda value: value["rows"][0].__setitem__(
                1, "Q1 推荐意愿"
            ),
            "matrix_rows": lambda value: value["confirmed_columns"][2]["rows"].__setitem__(
                1, "易用性"
            ),
            "scale_range": lambda value: value["confirmed_columns"][2].__setitem__(
                "scale_max", 7
            ),
            "authoritative_options": lambda value: value["confirmed_columns"][1][
                "options_original"
            ].append("不满意"),
        }

        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = deepcopy(base)
                mutate(changed)
                self.assertNotEqual(
                    analysis_preferences.build_analysis_preset_fingerprint(changed),
                    original,
                )


class AnalysisPreferenceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "analysis_presets.json"
        self.config_patch = patch.object(
            config,
            "ANALYSIS_PRESETS_FILE",
            str(self.path),
            create=True,
        )
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.temp_dir.cleanup()

    def test_owner_isolation_and_open_id_owner_are_enforced(self):
        owner_session = _session()
        saved = analysis_preferences.save_analysis_preset(
            owner_session,
            {"email": "OWNER@EXAMPLE.COM", "open_id": "ignored-by-email"},
        )
        self.assertIsNotNone(saved)

        self.assertIsNotNone(
            analysis_preferences.get_analysis_preset_offer(
                owner_session,
                {"email": "owner@example.com"},
            )
        )
        other_session = _session("email:other@example.com")
        self.assertIsNone(
            analysis_preferences.get_analysis_preset_offer(
                other_session,
                {"email": "other@example.com"},
            )
        )
        with patch.object(
            analysis_preferences,
            "load_analysis_presets",
        ) as load_mock:
            self.assertIsNone(
                analysis_preferences.get_analysis_preset_offer(
                    owner_session,
                    {"email": "other@example.com"},
                )
            )
            load_mock.assert_not_called()

        open_id_session = _session("open_id:ou_stable")
        open_id_session["rows"][0][1] = "另一份问卷"
        open_id_saved = analysis_preferences.save_analysis_preset(
            open_id_session,
            {"open_id": "ou_stable"},
        )
        self.assertIsNotNone(open_id_saved)

    def test_anonymous_and_ineligible_sessions_do_not_read_or_write_storage(self):
        session = _session("")
        with (
            patch.object(analysis_preferences, "load_analysis_presets") as load_mock,
            patch.object(analysis_preferences, "mutate_analysis_presets") as mutate_mock,
        ):
            self.assertIsNone(
                analysis_preferences.get_analysis_preset_offer(session, {})
            )
            self.assertIsNone(
                analysis_preferences.apply_analysis_preset(session, {}, "preset-id")
            )
            self.assertIsNone(
                analysis_preferences.save_analysis_preset(session, {})
            )

            quantitative = _session()
            quantitative["analysis_mode"] = "quantitative"
            self.assertIsNone(
                analysis_preferences.get_analysis_preset_offer(
                    quantitative,
                    {"email": "owner@example.com"},
                )
            )
            self.assertIsNone(
                analysis_preferences.save_analysis_preset(
                    _session(),
                    {"email": "owner@example.com"},
                    eligible=False,
                )
            )
            load_mock.assert_not_called()
            mutate_mock.assert_not_called()

        self.assertFalse(self.path.exists())

    def test_apply_revalidates_fingerprint_and_merges_without_overwriting_current_text(self):
        source = _session()
        source["plan_revision_texts"] = ["历史修订", "重复修订"]
        saved = analysis_preferences.save_analysis_preset(
            source,
            {"email": "owner@example.com"},
        )
        self.assertIsNotNone(saved)

        current = _session()
        current["qualitative_context"] = {
            "problem": "本次任务的新问题",
            "background": "   ",
            "analysis_approach": "本次任务当前分析思路",
        }
        current["plan_revision_texts"] = ["重复修订", "本次新增修订"]
        offered = analysis_preferences.get_analysis_preset_offer(
            current,
            {"email": "owner@example.com"},
        )
        self.assertEqual(offered["id"], saved["id"])

        changed_questionnaire = deepcopy(current)
        changed_questionnaire["rows"][0][1] = "另一道问题"
        unchanged_context = deepcopy(changed_questionnaire["qualitative_context"])
        self.assertIsNone(
            analysis_preferences.apply_analysis_preset(
                changed_questionnaire,
                {"email": "owner@example.com"},
                offered["id"],
            )
        )
        self.assertEqual(changed_questionnaire["qualitative_context"], unchanged_context)
        self.assertNotIn("applied_analysis_preset_id", changed_questionnaire)

        applied = analysis_preferences.apply_analysis_preset(
            current,
            {"email": "owner@example.com"},
            offered["id"],
        )
        self.assertEqual(applied["id"], saved["id"])
        self.assertEqual(current["qualitative_context"]["problem"], "本次任务的新问题")
        self.assertEqual(current["qualitative_context"]["background"], "新功能灰度中")
        self.assertEqual(
            current["qualitative_context"]["analysis_approach"],
            "本次任务当前分析思路",
        )
        self.assertEqual(current["applied_analysis_preset_id"], saved["id"])
        self.assertEqual(current["preset_analysis_focus"], _focus())
        self.assertEqual(
            current["plan_revision_texts"],
            ["历史修订", "重复修订", "本次新增修订"],
        )

    def test_analysis_approach_is_reused_only_when_current_input_is_empty(self):
        source = _session()
        source["qualitative_context"]["analysis_approach"] = "历史任务的完整分析思路"
        saved = analysis_preferences.save_analysis_preset(
            source,
            {"email": "owner@example.com"},
        )
        self.assertEqual(
            saved["context"]["analysis_approach"],
            "历史任务的完整分析思路",
        )

        blank_current = _session()
        blank_current["qualitative_context"]["analysis_approach"] = "   "
        applied = analysis_preferences.apply_analysis_preset(
            blank_current,
            {"email": "owner@example.com"},
            saved["id"],
        )
        self.assertIsNotNone(applied)
        self.assertEqual(
            blank_current["qualitative_context"]["analysis_approach"],
            "历史任务的完整分析思路",
        )

        nonempty_current = _session()
        nonempty_current["qualitative_context"]["analysis_approach"] = "本次任务的新分析思路"
        applied = analysis_preferences.apply_analysis_preset(
            nonempty_current,
            {"email": "owner@example.com"},
            saved["id"],
        )
        self.assertIsNotNone(applied)
        self.assertEqual(
            nonempty_current["qualitative_context"]["analysis_approach"],
            "本次任务的新分析思路",
        )

    def test_background_only_preset_is_saved_and_reused_without_analysis_focus(self):
        source = _session()
        source["qualitative_context"] = {
            "problem": "理解新手流失",
            "background": "新手流程刚刚改版",
            "target_users": "",
            "key_concerns": "",
            "report_usage": "下一版优化",
            "analysis_approach": "",
        }
        source["plan"] = {"parts": [{"name": "体验反馈"}]}
        source["plan_revision_texts"] = []

        saved = analysis_preferences.save_analysis_preset(
            source,
            {"email": "owner@example.com"},
        )

        self.assertIsNotNone(saved)
        self.assertIsNone(saved["analysis_focus"])
        current = _session()
        current["qualitative_context"] = {}
        current["plan_revision_texts"] = []
        applied = analysis_preferences.apply_analysis_preset(
            current,
            {"email": "owner@example.com"},
            saved["id"],
        )
        self.assertIsNotNone(applied)
        self.assertEqual(current["qualitative_context"]["problem"], "理解新手流失")
        self.assertEqual(current["qualitative_context"]["background"], "新手流程刚刚改版")
        self.assertNotIn("preset_analysis_focus", current)

    def test_context_submit_preserves_applied_fields_not_present_in_form(self):
        session = _session()
        session["qualitative_context"] = {
            "problem": "旧问题",
            "background": "预设中的完整背景",
            "target_users": "旧用户",
            "key_concerns": "旧关注",
            "report_usage": "预设中的报告用途",
            "analysis_approach": "旧思路",
        }
        request = QualitativeContextRequest(
            problem="当前问题",
            key_concerns="当前关注",
            target_users="当前用户",
            analysis_approach="当前思路",
        )

        with (
            patch.object(survey_service, "get_session", return_value=session),
            patch.object(survey_service, "save_session") as save_session,
        ):
            survey_service.save_qualitative_context("session-id", request)

        self.assertEqual(session["qualitative_context"]["problem"], "当前问题")
        self.assertEqual(session["qualitative_context"]["analysis_approach"], "当前思路")
        self.assertEqual(session["qualitative_context"]["background"], "预设中的完整背景")
        self.assertEqual(session["qualitative_context"]["report_usage"], "预设中的报告用途")
        save_session.assert_called_once_with("session-id", session)

    def test_repeated_apply_does_not_relabel_preset_revisions_as_current(self):
        source = _session()
        source["plan_revision_texts"] = ["历史修订"]
        saved = analysis_preferences.save_analysis_preset(
            source,
            {"email": "owner@example.com"},
        )
        current = _session()
        current["plan_revision_texts"] = ["本次修订"]

        for _ in range(2):
            applied = analysis_preferences.apply_analysis_preset(
                current,
                {"email": "owner@example.com"},
                saved["id"],
            )
            self.assertIsNotNone(applied)

        self.assertEqual(current["current_plan_revision_texts"], ["本次修订"])
        self.assertEqual(current["plan_revision_texts"], ["历史修订", "本次修订"])

    def test_questionnaire_change_clears_old_preset_and_all_old_revisions(self):
        source = _session()
        source["plan_revision_texts"] = ["历史问卷修订"]
        saved = analysis_preferences.save_analysis_preset(
            source,
            {"email": "owner@example.com"},
        )
        current = _session()
        current["plan_revision_texts"] = ["当前问卷修订"]
        analysis_preferences.apply_analysis_preset(
            current,
            {"email": "owner@example.com"},
            saved["id"],
        )
        changed_columns = deepcopy(current["confirmed_columns"])
        changed_columns[1]["role"] = "open_text"

        with (
            patch.object(survey_service, "get_session", return_value=current),
            patch.object(survey_service, "save_session") as save_session,
        ):
            survey_service.set_survey_columns("session-id", changed_columns)

        self.assertEqual(current["plan_revision_texts"], [])
        self.assertNotIn("applied_analysis_preset_id", current)
        self.assertNotIn("preset_analysis_focus", current)
        self.assertEqual(
            current["analysis_preference_fingerprint"],
            analysis_preferences.build_analysis_preset_fingerprint(current),
        )
        save_session.assert_called_once_with("session-id", current)

    def test_questionnaire_change_clears_local_revisions_without_applied_preset(self):
        current = _session()
        current["plan_revision_texts"] = ["只属于旧问卷的修订"]
        current["analysis_preference_fingerprint"] = (
            analysis_preferences.build_analysis_preset_fingerprint(current)
        )
        current["confirmed_columns"][1]["role"] = "open_text"

        changed = survey_service._discard_stale_applied_analysis_preset(current)

        self.assertTrue(changed)
        self.assertEqual(current["plan_revision_texts"], [])

    def test_save_upserts_only_allowed_preference_fields(self):
        session = _session()
        first = analysis_preferences.save_analysis_preset(
            session,
            {"email": "owner@example.com"},
        )
        session["qualitative_context"]["problem"] = "更新后的业务问题"
        session["qualitative_context"]["analysis_approach"] = "更新后的分析思路"
        session["plan"]["analysis_focus"] = _focus("更新")
        session["plan_revision_texts"] = ["完整修订 A", "完整修订 B"]
        second = analysis_preferences.save_analysis_preset(
            session,
            {"email": "owner@example.com"},
        )

        document = analysis_presets.load_analysis_presets()
        self.assertEqual(document["revision"], 2)
        self.assertEqual(len(document["presets"]), 1)
        self.assertEqual(first["id"], second["id"])
        stored = document["presets"][0]
        self.assertEqual(
            set(stored),
            {
                "id",
                "owner_key",
                "fingerprint",
                "fingerprint_version",
                "context",
                "analysis_focus",
                "plan_revision_texts",
                "created_at",
                "updated_at",
            },
        )
        self.assertEqual(set(stored["context"]), set(analysis_preferences._BUSINESS_CONTEXT_FIELDS))
        self.assertEqual(stored["context"]["analysis_approach"], "更新后的分析思路")
        self.assertEqual(stored["context"]["problem"], "更新后的业务问题")
        self.assertEqual(stored["analysis_focus"], _focus("更新"))
        self.assertEqual(stored["plan_revision_texts"], ["完整修订 A", "完整修订 B"])
        self.assertNotIn("parts", stored)
        self.assertNotIn("rows", stored)
        self.assertNotIn("stats_md", stored)
        self.assertNotIn("report_md", stored)
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
