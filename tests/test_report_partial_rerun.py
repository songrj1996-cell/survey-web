import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.services import (
    history_service,
    report_history,
    report_partial_rerun,
    report_versions,
    survey_service,
)
from app.storage import history as history_storage


def _login():
    return {
        "email": "partial-rerun@example.com",
        "open_id": "",
        "name": "Partial Rerun Tester",
    }


def _plan():
    return {
        "columns": [
            {"index": 0, "name": "玩家ID", "role": "id"},
            {"index": 1, "name": "总体评分", "role": "scale"},
            {"index": 2, "name": "概念排名原因", "role": "open_text"},
            {"index": 3, "name": "其他建议", "role": "open_text"},
        ],
        "parts": [
            {"name": "概念评价", "column_indexes": [1, 2]},
            {"name": "补充反馈", "column_indexes": [3]},
        ],
        "cross_tabs": [],
        "open_questions": [2, 3],
    }


def _stats_md():
    return """## Part 1 概念评价

### 总体评分

- 均值: 4.00

| 评分 | 人数 | 占比 |
|---|---:|---:|
| 4 | 2 | 100.0% |

## Part 2 补充反馈
"""


def _open_text():
    return {
        2: [
            {
                "respondent_key": "玩家ID=A",
                "ids": {"玩家ID": "A"},
                "profile": {"地区": "中国"},
                "segments": {},
                "text": "概念一更清楚。",
                "ignored_extra": "must not persist",
            },
            {
                "respondent_key": "玩家ID=B",
                "ids": {"玩家ID": "B"},
                "profile": {"地区": "印尼"},
                "segments": {},
                "text": "概念二更有新鲜感。",
            },
        ],
        3: [
            {
                "respondent_key": "玩家ID=A",
                "ids": {"玩家ID": "A"},
                "profile": {"地区": "中国"},
                "segments": {},
                "text": "希望教程更短。",
            }
        ],
    }


def _theme(scope_key, col_idx, part_index, part_name, name):
    return {
        "column_index": col_idx,
        "part_index": part_index,
        "part_name": part_name,
        "filter_desc": "",
        "col_name": "概念排名原因" if col_idx == 2 else "其他建议",
        "total": 2 if col_idx == 2 else 1,
        "count_unit": "players",
        "themes": [
            {
                "id": f"T{scope_key}",
                "name": name,
                "description": name,
                "count": 1,
                "percentage": 50.0 if col_idx == 2 else 100.0,
                "quotes": ["代表原文"],
                "respondent_keys": ["玩家ID=A"],
            }
        ],
        "all_themes": [
            {
                "id": f"T{scope_key}",
                "name": name,
                "description": name,
                "count": 1,
                "percentage": 50.0 if col_idx == 2 else 100.0,
                "quotes": ["代表原文"],
                "respondent_keys": ["玩家ID=A"],
            }
        ],
        "other_themes": [],
    }


def _base_report():
    return """# 概念研究报告

<!--CORE_START-->
## 核心结论

### 总体判断

旧核心判断。
<!--CORE_END-->

---------------- 以下为详细信息，各位可以按需查看 ----------------

## Part 1 概念评价

**本节总结：**

1. **旧总结**：旧内容。

## Part 2 补充反馈

**本节总结：**

1. **保持原样**：教程反馈。

## 行动建议

1. **旧建议**（优先级：中）
   - **核心判断：** 旧判断
"""


def _entry():
    plan = _plan()
    sess = {
        "mode": "",
        "plan": plan,
        "rows": [
            ["玩家ID", "总体评分", "概念排名原因", "其他建议"],
            ["A", "4", "概念一更清楚。", "希望教程更短。"],
            ["B", "4", "概念二更有新鲜感。", ""],
        ],
        "open_text": _open_text(),
        "stats_md": _stats_md(),
        "stats_source": "python",
        "qualitative_context": {"problem": "选择更好的概念"},
        "file_sha256": "file-hash",
        "questionnaire_sha256": "questionnaire-hash",
    }
    source = report_partial_rerun.build_partial_rerun_source(sess)
    themes = {
        "2": _theme("2", 2, 1, "概念评价", "旧概念主题"),
        "3": _theme("3", 3, 2, "补充反馈", "教程建议"),
    }
    artifacts = report_partial_rerun.build_analysis_artifacts(
        source,
        use_large_mode=False,
        clustered_themes=themes,
        report_viewpoints=[],
        viewpoint_stats_md="<subjective_viewpoint_stats>旧目录</subjective_viewpoint_stats>",
        cluster_diagnostics={"2": {"status": "completed"}, "3": {"status": "completed"}},
        cluster_metrics={"scope_count": 2, "elapsed_seconds": 12},
    )
    snapshot = {
        "version": 1,
        "kind": "initial",
        "base_version": None,
        "instruction": "",
        "created_at": "2026-08-31T10:00:00",
        "report_md": _base_report(),
        "title": "概念研究报告",
        "qa_context_md": "<qa_context>\n<report>\n旧报告\n</report>\n<rows>\n保留原始上下文\n</rows>\n</qa_context>",
        "qa_messages": [],
        "qa_provider": "",
        "qa_model": "",
        "report_writer_provider": "direct_llm",
        "report_writer_model": "writer-old",
        "analyst_conv_id": "",
        "analyst_app": "standard",
        "comparison_validation": {},
        "analysis_artifacts": artifacts,
    }
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "owner_key": "email:partial-rerun@example.com",
        "owner_email": "partial-rerun@example.com",
        "owner_open_id": "",
        "owner_name": "Partial Rerun Tester",
        "report_no": "R-010",
        "filename": "survey.xlsx",
        "title": "概念研究报告",
        "created_at": "2026-08-31T10:00:00",
        "mode": "",
        "plan": plan,
        "stats_md": _stats_md(),
        "qualitative_context": sess["qualitative_context"],
        "partial_rerun_source": source,
        "report_md": _base_report(),
        "report_versions": [snapshot],
        "active_report_version": 1,
        "next_report_version": 2,
    }


class PartialRerunContractTests(unittest.TestCase):
    def test_source_is_minimal_and_fingerprinted(self):
        entry = _entry()
        source = entry["partial_rerun_source"]
        self.assertNotIn("rows", source)
        self.assertNotIn("ignored_extra", source["open_text"]["2"][0])
        self.assertTrue(source["plan_fingerprint"])
        self.assertTrue(source["source_fingerprint"])

    def test_capability_maps_questions_to_stable_parts(self):
        entry = _entry()
        snapshot = report_versions.resolve_report_version(entry, 1)
        capability = report_partial_rerun.partial_rerun_capability(entry, snapshot)
        self.assertTrue(capability["available"])
        self.assertEqual(capability["questions"][0]["scope_key"], "2")
        self.assertEqual(capability["questions"][0]["part_title"], "Part 1 概念评价")
        self.assertEqual(capability["parts"][0]["scope_keys"], ["2"])

    def test_legacy_or_drifted_version_is_not_eligible(self):
        entry = _entry()
        snapshot = report_versions.resolve_report_version(entry, 1)
        snapshot.pop("analysis_artifacts")
        self.assertFalse(
            report_partial_rerun.partial_rerun_capability(entry, snapshot)["available"]
        )
        snapshot = report_versions.resolve_report_version(entry, 1)
        entry["plan"]["parts"][0]["name"] = "变化后的标题"
        result = report_partial_rerun.partial_rerun_capability(entry, snapshot)
        self.assertFalse(result["available"])
        self.assertIn("指纹不一致", result["reason"])

        entry = _entry()
        snapshot = report_versions.resolve_report_version(entry, 1)
        entry["partial_rerun_source"]["open_text"]["2"][0]["text"] = "被篡改"
        result = report_partial_rerun.partial_rerun_capability(entry, snapshot)
        self.assertFalse(result["available"])
        self.assertIn("指纹不一致", result["reason"])

    def test_part_validation_and_replacement_are_strict(self):
        good = "## Part 1 概念评价\n\n**本节总结：** 新内容"
        replaced = report_partial_rerun.replace_h2_section(
            _base_report(), "Part 1 概念评价", good
        )
        self.assertIn("新内容", replaced)
        self.assertIn("**保持原样**：教程反馈。", replaced)
        with self.assertRaisesRegex(ValueError, "不得夹带"):
            report_partial_rerun.validate_single_part(
                good + "\n\n## Part 2 补充反馈\n污染", "Part 1 概念评价"
            )
        with self.assertRaisesRegex(ValueError, "必须且只能"):
            report_partial_rerun.validate_single_part(
                "## Part 1 错误标题\n内容", "Part 1 概念评价"
            )

    def test_history_detail_exposes_targets_but_not_persisted_raw_source(self):
        entry = _entry()
        with patch.object(
            history_service,
            "_load_history_with_report_numbers",
            return_value=[deepcopy(entry)],
        ):
            detail = history_service.get_history_entry(entry["id"], _login(), 1)
        self.assertNotIn("partial_rerun_source", detail)
        self.assertNotIn("analysis_artifacts", detail)
        self.assertTrue(detail["partial_rerun"]["available"])
        self.assertEqual(detail["partial_rerun"]["questions"][0]["scope_key"], "2")


class PartialRerunAtomicHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="partial-rerun-history-")
        self.history_path = Path(self.temp_dir.name) / "history.json"
        self.history_patch = patch.object(history_storage, "HISTORY_FILE", str(self.history_path))
        self.history_patch.start()
        history_storage._save_history([_entry()])

    def tearDown(self):
        self.history_patch.stop()
        self.temp_dir.cleanup()

    def test_fingerprint_failure_preserves_original_bytes(self):
        before = self.history_path.read_bytes()
        with self.assertRaises(HTTPException):
            report_history.append_partial_rerun_to_history(
                _entry()["id"],
                {"report_md": "# 不应保存"},
                base_version=1,
                expected_plan_fingerprint="wrong",
                expected_source_fingerprint="wrong",
                instruction="test",
                login=_login(),
            )
        self.assertEqual(self.history_path.read_bytes(), before)

    def test_success_appends_without_mutating_base_version(self):
        entry = _entry()
        source = entry["partial_rerun_source"]
        snapshot = deepcopy(report_versions.resolve_report_version(entry, 1))
        snapshot["report_md"] = snapshot["report_md"].replace("旧内容", "新内容")
        snapshot["rerun_details"] = {"target_label": "概念排名原因"}
        stored, committed = report_history.append_partial_rerun_to_history(
            entry["id"],
            snapshot,
            base_version=1,
            expected_plan_fingerprint=source["plan_fingerprint"],
            expected_source_fingerprint=source["source_fingerprint"],
            instruction="局部更新",
            login=_login(),
        )
        self.assertEqual(committed["version"], 2)
        self.assertEqual(report_versions.resolve_report_version(stored, 1)["report_md"], _base_report())
        self.assertIn("新内容", report_versions.resolve_report_version(stored, 2)["report_md"])


class PartialRerunStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_only_calls_target_scope_and_commits_expected_sections(self):
        entry = _entry()
        captured = {}

        async def fake_batch(*args, **kwargs):
            scopes = kwargs.get("_scopes_override") or []
            captured["scope_keys"] = [str(scope[0]) for scope in scopes]
            yield ("analysis_metrics", {"scope_count": 1, "elapsed_seconds": 3.5})
            yield ("diagnostics", {"2": {"status": "completed"}})
            yield ("result", {"2": _theme("2", 2, 1, "概念评价", "新决策标准")})

        async def fake_viewpoints(*_args, **_kwargs):
            yield ("result", [])

        async def fake_completion(messages, **_kwargs):
            last = next(
                message["content"]
                for message in reversed(messages)
                if message.get("role") == "user"
            )
            if "核心结论覆盖与表达复核" in last:
                return "PASS", "writer-test"
            if "行动建议" in last and "只重新输出行动建议" in last:
                return (
                    "## 行动建议\n\n"
                    "1. **验证新概念**（优先级：高）\n"
                    "   - **核心判断：** 新判断\n"
                    "   - **产品动作：** 做测试\n"
                    "   - **验证方式：** A/B\n"
                    "   - **依据：** 新主题\n"
                    "   - **不确定性/前提：** 样本有限",
                    "writer-test",
                )
            if "<!--CORE_START-->" in last and "核心结论覆盖与表达复核" not in last:
                return (
                    "<!--CORE_START-->\n## 核心结论\n\n### 总体判断\n\n新核心判断。\n<!--CORE_END-->",
                    "writer-test",
                )
            return (
                "## Part 1 概念评价\n\n"
                "**本节总结：**\n\n"
                "1. **新总结**：玩家的决策标准已更新。\n\n"
                "**观点：清晰度标准**\n\n"
                "- **提及情况：** 1名玩家提及。",
                "writer-test",
            )

        def fake_commit(_history_id, snapshot, **kwargs):
            captured["snapshot"] = deepcopy(snapshot)
            source = deepcopy(entry)
            committed = report_versions.append_report_version(
                source,
                snapshot,
                kind="regenerate",
                base_version=kwargs["base_version"],
                instruction=kwargs["instruction"],
            )
            return source, committed

        request = object()
        with (
            patch.object(survey_service, "_current_login", AsyncMock(return_value=_login())),
            patch.object(survey_service, "_load_history", return_value=[deepcopy(entry)]),
            patch.object(survey_service, "_batch_qualitative_analysis", fake_batch),
            patch.object(survey_service, "build_report_viewpoint_stats", fake_viewpoints),
            patch.object(survey_service, "collect_chat_completion", fake_completion),
            patch.object(survey_service, "append_partial_rerun_to_history", fake_commit),
            patch.object(survey_service, "audit_log", AsyncMock()),
        ):
            events = []
            async for raw in survey_service.partial_report_rerun_stream(
                entry["id"],
                request,
                base_version=1,
                target_type="question",
                target_key="2",
                instruction="强调决策标准",
            ):
                payload = str(raw).split("data: ", 1)[-1].strip()
                events.append(json.loads(payload))

        self.assertEqual(captured["scope_keys"], ["2"])
        snapshot = captured["snapshot"]
        self.assertIn("新总结", snapshot["report_md"])
        self.assertIn("新核心判断", snapshot["report_md"])
        self.assertIn("验证新概念", snapshot["report_md"])
        self.assertIn("**保持原样**：教程反馈。", snapshot["report_md"])
        stats_block = survey_service.render_qualitative_stats_by_part(
            _stats_md(), _plan()
        )["Part 1 概念评价"]
        self.assertEqual(snapshot["report_md"].count(stats_block), 1)
        details = snapshot["rerun_details"]
        self.assertFalse(details["full_report_rerun"])
        self.assertEqual(details["scope_keys"], ["2"])
        self.assertEqual(
            details["changed_sections"],
            ["Part 1 概念评价", "核心结论", "行动建议"],
        )
        self.assertFalse(details["token_usage"]["available"])
        self.assertEqual(events[-1]["type"], "partial_rerun_done")
