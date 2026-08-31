import hashlib
import json
import os
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.core.config import DEFAULT_UPLOAD_GUIDE
from app.routers import settings_api
from app.schemas.requests import PromptUpdateRequest
from app.services import interview_service, report_engine, settings_service
from app.storage import prompts as prompt_storage
from app.storage import ui_texts as ui_text_storage


EXPECTED_PROMPT_KEYS = {
    "questionnaire_translation_system",
    "column_detect_system",
    "survey_planner_system",
    "crosstab_planner_system",
    "planner_extra",
    "report_writer_system",
    "writer_requirements",
    "large_sample_writer_requirements",
    "report_qa_system",
    "theme_extract_system",
    "theme_merge_system",
    "response_classify_system",
    "comment_relevance_system",
    "comment_extract_system",
    "comment_merge_system",
    "comment_classify_system",
    "comment_report_system",
    "comment_quote_batch_system",
    "comment_quote_final_system",
    "annotate_ai_system",
    "annotate_quality_system",
    "annotate_translation_system",
    "interview_extract_system",
    "interview_report_system",
    "interview_repair_system",
    "interview_audit_system",
    "interview_v2_attribute_system",
    "interview_v2_dossier_system",
}


def _write_json(path: str, value: dict) -> None:
    with open(path, "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as source:
        return json.load(source)


def _sha256(path: str) -> str:
    with open(path, "rb") as source:
        return hashlib.sha256(source.read()).hexdigest()


class PromptCatalogTests(unittest.TestCase):
    def test_catalog_has_28_current_non_dify_entries_in_six_groups(self):
        catalog = prompt_storage.DEFAULT_PROMPTS

        self.assertEqual(set(catalog), EXPECTED_PROMPT_KEYS)
        self.assertEqual(len(catalog), 28)
        self.assertNotIn("upload_guide", catalog)
        self.assertEqual(
            {entry["group"] for entry in catalog.values()},
            {
                "问卷理解与规划",
                "报告生成与追问",
                "大样本开放题",
                "评论分析",
                "数据标注",
                "访谈报告",
            },
        )
        for key, entry in catalog.items():
            self.assertEqual(entry["key"], key)
            self.assertTrue(entry["editable"])
            self.assertIn(entry["kind"], {"system", "instruction"})
            self.assertTrue(entry["current"].strip())
            self.assertNotIn("dify_app", entry)
            self.assertNotIn("dify_url", entry)

    def test_legacy_prompt_file_migrates_metadata_versions_and_is_idempotent(self):
        legacy = {
            "column_detect_system": {
                "key": "column_detect_system",
                "label": "旧题型识别",
                "description": "旧说明",
                "editable": True,
                "current": "OLD_COLUMN",
                "history": [],
                "version": 1,
                "dify_app": "retired",
                "dify_url": "https://retired.invalid",
            },
            "survey_planner_system": {
                "key": "survey_planner_system",
                "current": "OLD_PLANNER",
                "history": [],
                "version": 1,
            },
            "writer_requirements": {
                "key": "writer_requirements",
                "current": "OLD_WRITER",
                "history": [],
                "version": 10,
            },
            "planner_extra": {
                "key": "planner_extra",
                "current": "CUSTOM_PLANNER_EXTRA",
                "history": [{"ts": "2026-01-01", "content": "old", "note": "custom"}],
                "version": 2,
            },
            "future_prompt": {"current": "keep me"},
            "dify_analyst_system": {"current": "retired but retained by generic loader"},
        }
        with tempfile.TemporaryDirectory(prefix="prompt-catalog-test-") as temp_dir:
            prompt_file = os.path.join(temp_dir, "prompts.json")
            _write_json(prompt_file, legacy)
            with patch.object(prompt_storage, "PROMPTS_FILE", prompt_file):
                migrated = prompt_storage._load_prompts()
                first_hash = _sha256(prompt_file)
                second = prompt_storage._load_prompts()
                second_hash = _sha256(prompt_file)

        self.assertEqual(first_hash, second_hash)
        self.assertEqual(migrated, second)
        self.assertTrue(EXPECTED_PROMPT_KEYS.issubset(migrated))
        self.assertEqual(
            migrated["column_detect_system"]["current"],
            prompt_storage.DEFAULT_PROMPTS["column_detect_system"]["current"],
        )
        self.assertEqual(migrated["column_detect_system"]["version"], 2)
        self.assertEqual(migrated["survey_planner_system"]["version"], 4)
        self.assertEqual(migrated["writer_requirements"]["version"], 14)
        self.assertEqual(migrated["annotate_quality_system"]["version"], 2)
        self.assertEqual(migrated["theme_extract_system"]["version"], 3)
        self.assertEqual(migrated["theme_merge_system"]["version"], 2)
        self.assertEqual(migrated["response_classify_system"]["version"], 2)
        self.assertEqual(
            migrated["writer_requirements"]["current"],
            prompt_storage.DEFAULT_PROMPTS["writer_requirements"]["current"],
        )
        self.assertEqual(migrated["planner_extra"]["current"], "CUSTOM_PLANNER_EXTRA")
        self.assertEqual(migrated["planner_extra"]["version"], 3)
        self.assertEqual(len(migrated["planner_extra"]["history"]), 1)
        self.assertNotIn("dify_app", migrated["column_detect_system"])
        self.assertNotIn("dify_url", migrated["column_detect_system"])
        self.assertEqual(migrated["future_prompt"]["current"], "keep me")
        self.assertIn("dify_analyst_system", migrated)

    def test_version_bumps_refresh_defaults_but_preserve_custom_content(self):
        for key, previous_version in (
            ("survey_planner_system", 3),
            ("writer_requirements", 11),
            ("annotate_quality_system", 1),
            ("theme_extract_system", 2),
            ("theme_merge_system", 1),
            ("response_classify_system", 1),
        ):
            with self.subTest(key=key, content="default"):
                stored = deepcopy(prompt_storage.DEFAULT_PROMPTS[key])
                stored.update({
                    "current": "OUTDATED_DEFAULT",
                    "history": [],
                    "version": previous_version,
                })

                prompt_storage._sync_entry(
                    stored,
                    prompt_storage.DEFAULT_PROMPTS[key],
                )

                self.assertEqual(
                    stored["current"],
                    prompt_storage.DEFAULT_PROMPTS[key]["current"],
                )
                self.assertEqual(
                    stored["version"],
                    prompt_storage.DEFAULT_PROMPTS[key]["version"],
                )

            with self.subTest(key=key, content="custom"):
                stored = deepcopy(prompt_storage.DEFAULT_PROMPTS[key])
                stored.update({
                    "current": f"CUSTOM_{key}",
                    "history": [{
                        "ts": "2026-01-01",
                        "content": "earlier content",
                        "note": "customized",
                    }],
                    "version": previous_version,
                })

                prompt_storage._sync_entry(
                    stored,
                    prompt_storage.DEFAULT_PROMPTS[key],
                )

                self.assertEqual(stored["current"], f"CUSTOM_{key}")
                self.assertEqual(
                    stored["version"],
                    prompt_storage.DEFAULT_PROMPTS[key]["version"],
                )

    def test_qualitative_prompts_use_semantic_boundaries_without_count_caps(self):
        extract_prompt = prompt_storage.DEFAULT_PROMPTS["theme_extract_system"]["current"]
        merge_prompt = prompt_storage.DEFAULT_PROMPTS["theme_merge_system"]["current"]
        classify_prompt = prompt_storage.DEFAULT_PROMPTS["response_classify_system"]["current"]

        self.assertNotIn("5–15", extract_prompt)
        self.assertNotIn("10–25", merge_prompt)
        self.assertNotIn("1–3 个不同主题", classify_prompt)
        self.assertIn("不设置候选主题数量目标", extract_prompt)
        self.assertIn("最终主题不设置最少或最多数量", merge_prompt)
        self.assertIn("source_candidate_ids", merge_prompt)
        self.assertIn("不设置上限", classify_prompt)

    def test_catalog_api_filters_unknown_and_legacy_entries(self):
        with tempfile.TemporaryDirectory(prefix="prompt-api-test-") as temp_dir:
            prompt_file = os.path.join(temp_dir, "prompts.json")
            persisted = deepcopy(prompt_storage.DEFAULT_PROMPTS)
            persisted["future_prompt"] = {"current": "hidden"}
            persisted["dify_planner_system"] = {"current": "hidden"}
            _write_json(prompt_file, persisted)
            with patch.object(prompt_storage, "PROMPTS_FILE", prompt_file):
                visible = settings_service.get_all_prompts()

        self.assertEqual(set(visible), EXPECTED_PROMPT_KEYS)
        report_prompt = visible["report_writer_system"]
        self.assertEqual(
            report_prompt["revision"],
            hashlib.sha256(report_prompt["current"].encode("utf-8")).hexdigest(),
        )

    def test_upload_guide_uses_new_default_without_history_and_custom_with_history(self):
        scenarios = [
            ({"current": "OLD_DEFAULT", "history": []}, DEFAULT_UPLOAD_GUIDE),
            (
                {
                    "current": "CUSTOM_GUIDE",
                    "history": [{"ts": "2026-01-01", "content": "old", "note": "custom"}],
                },
                "CUSTOM_GUIDE",
            ),
        ]
        for legacy_entry, expected in scenarios:
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory(prefix="upload-guide-test-") as temp_dir:
                    prompt_file = os.path.join(temp_dir, "prompts.json")
                    ui_file = os.path.join(temp_dir, "ui_texts.json")
                    _write_json(prompt_file, {"upload_guide": legacy_entry})
                    _write_json(ui_file, {})
                    with (
                        patch.object(ui_text_storage, "PROMPTS_FILE", prompt_file),
                        patch.object(ui_text_storage, "UI_TEXTS_FILE", ui_file),
                    ):
                        loaded = ui_text_storage._load_ui_texts()
                        first_hash = _sha256(ui_file)
                        ui_text_storage._load_ui_texts()
                        second_hash = _sha256(ui_file)

                self.assertEqual(loaded["upload_guide"]["current"], expected)
                self.assertEqual(first_hash, second_hash)

    def test_prompt_update_is_atomic_and_rejects_empty_or_noop_content(self):
        with tempfile.TemporaryDirectory(prefix="prompt-update-test-") as temp_dir:
            prompt_file = os.path.join(temp_dir, "prompts.json")
            with patch.object(prompt_storage, "PROMPTS_FILE", prompt_file):
                prompt_storage._load_prompts()
                old_value = prompt_storage._load_prompts()["report_writer_system"]["current"]
                old_revision = settings_service._prompt_revision(old_value)
                settings_service.update_prompt(
                    "report_writer_system",
                    old_value + "\n新增约束",
                    "单元测试",
                    expected_revision=old_revision,
                )
                changed = _read_json(prompt_file)
                changed_hash = _sha256(prompt_file)
                with self.assertRaises(HTTPException) as conflict:
                    settings_service.update_prompt(
                        "report_writer_system",
                        old_value + "\n过期草稿",
                        "不应覆盖",
                        expected_revision=old_revision,
                    )
                conflict_hash = _sha256(prompt_file)
                with self.assertRaises(HTTPException) as noop:
                    settings_service.update_prompt(
                        "report_writer_system",
                        old_value + "\n新增约束",
                        "不应写入",
                    )
                noop_hash = _sha256(prompt_file)
                with self.assertRaises(HTTPException) as empty:
                    settings_service.update_prompt("report_writer_system", "   ", "")

        self.assertEqual(noop.exception.status_code, 409)
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(empty.exception.status_code, 422)
        self.assertEqual(changed_hash, conflict_hash)
        self.assertEqual(changed_hash, noop_hash)
        self.assertEqual(changed["report_writer_system"]["history"][0]["content"], old_value)
        self.assertEqual(changed["report_writer_system"]["history"][0]["note"], "单元测试")

    def test_runtime_builders_read_configurable_prompt_getters(self):
        with patch.object(
            report_engine,
            "_get_large_sample_writer_requirements_base",
            return_value="CONFIGURABLE_BASE",
        ):
            large_requirements = report_engine._get_large_sample_writer_requirements(
                has_satisfaction=True,
                has_business_context=True,
            )
        self.assertTrue(large_requirements.startswith("CONFIGURABLE_BASE"))
        self.assertIn("满意度优先原则", large_requirements)
        self.assertIn("用户已提供", large_requirements)

        getter_names = (
            "_get_interview_extract_system_prompt",
            "_get_interview_report_system_prompt",
            "_get_interview_repair_system_prompt",
            "_get_interview_audit_system_prompt",
        )
        patches = [
            patch.object(interview_service, name, return_value=name)
            for name in getter_names
        ]
        for item in patches:
            item.start()
        try:
            messages = [
                interview_service._extract_messages("source", ""),
                interview_service._module_messages({}, [], [], ""),
                interview_service._module_repair_messages("report", {}, [], []),
                interview_service._audit_messages("report", {}),
            ]
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(
            [items[0]["content"] for items in messages],
            list(getter_names),
        )


class PromptApiAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_get_and_put_require_admin(self):
        request = object()
        require_admin = AsyncMock(return_value={"email": "admin@example.com"})
        with (
            patch.object(settings_api, "_require_admin", new=require_admin),
            patch.object(settings_api, "get_all_prompts", return_value={"ok": {}}),
        ):
            result = await settings_api.get_prompts(request)
        self.assertEqual(result, {"ok": {}})
        require_admin.assert_awaited_once_with(request)

        require_admin.reset_mock()
        with (
            patch.object(settings_api, "_require_admin", new=require_admin),
            patch.object(settings_api, "update_prompt") as update_prompt,
            patch.object(settings_api, "audit_log", new=AsyncMock()) as audit_log,
        ):
            result = await settings_api.update_prompt_endpoint(
                "report_writer_system",
                PromptUpdateRequest(content="new prompt", note="test"),
                request,
                expected_revision="revision-1",
            )
        self.assertTrue(result["ok"])
        require_admin.assert_awaited_once_with(request)
        update_prompt.assert_called_once_with(
            "report_writer_system",
            "new prompt",
            "test",
            expected_revision="revision-1",
        )
        audit_log.assert_awaited_once()

    async def test_ui_text_put_requires_admin(self):
        request = object()
        require_admin = AsyncMock(return_value={"email": "admin@example.com"})
        with (
            patch.object(settings_api, "_require_admin", new=require_admin),
            patch.object(settings_api, "update_ui_text") as update_ui_text,
            patch.object(settings_api, "audit_log", new=AsyncMock()),
        ):
            await settings_api.update_ui_text_endpoint(
                "upload_guide",
                settings_api.UiTextUpdateRequest(content="safe guide"),
                request,
            )
        require_admin.assert_awaited_once_with(request)
        update_ui_text.assert_called_once_with("upload_guide", "safe guide")

    async def test_prompt_get_stops_before_storage_when_admin_check_fails(self):
        denied = HTTPException(status_code=403, detail="需要管理员权限")
        with (
            patch.object(settings_api, "_require_admin", new=AsyncMock(side_effect=denied)),
            patch.object(settings_api, "get_all_prompts") as get_all_prompts,
        ):
            with self.assertRaises(HTTPException) as caught:
                await settings_api.get_prompts(object())

        self.assertEqual(caught.exception.status_code, 403)
        get_all_prompts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
