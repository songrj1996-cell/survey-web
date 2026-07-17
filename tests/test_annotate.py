import asyncio
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

import openpyxl
from fastapi import HTTPException

import annotate
from app.core.parsing import _parse_file
from app.services import auth
from app.services import annotate_workflow


class AnnotateRuleTests(unittest.TestCase):
    def test_workflow_queries_only_contain_task_data(self):
        rows = [["P1", "answer one", "answer two"]]
        headers = ["ID", "Q1", "Q2"]
        ai_query = annotate.build_ai_detect_query(rows, headers, [1, 2], 0)
        quality_query = annotate.build_quality_label_query(rows, headers, [1, 2], 0)
        translation_query = annotate.build_translation_repair_query([
            {"id": "P1", "key": "col_1", "text": "answer one"},
        ])

        self.assertIn("任务模式：AI 内容生成识别", ai_query)
        self.assertIn("任务模式：逐题反馈质量打标", quality_query)
        self.assertIn("| P1 | answer one | answer two |", ai_query)
        self.assertNotIn("translations", ai_query)
        self.assertNotIn("```json", quality_query)
        self.assertIsInstance(json.loads(translation_query), list)

    def test_ai_polish_probability_is_independent_from_content_generation(self):
        payload = [{
            "id": "P1",
            "ai_prob": 12,
            "polish_prob": 91,
            "reason": "观点包含具体个人体验，仅表达可能经过整理",
            "evidence": "",
            "counter_evidence": "我在昨晚的排位中连续用了三局",
            "translations": {"col_1": "我在昨晚的排位中连续用了三局"},
        }]

        results, error = annotate.parse_ai_detect_result(
            "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        )

        self.assertEqual(error, "")
        self.assertEqual(results[0]["ai_prob"], 12)
        self.assertEqual(results[0]["polish_prob"], 91)

    def test_all_na_has_no_assessable_overall_quality(self):
        overall, reason = annotate.calculate_overall_quality(
            {"col_1": "N/A", "col_2": "N/A"}, [1, 2]
        )

        self.assertEqual(overall, "N/A")
        self.assertIn("无可评估", reason)

    def test_no_answer_placeholders_are_normalized_to_na(self):
        for placeholder in ("nil", "N/A", "none", "No", "no.", "Tidak", "暂无"):
            with self.subTest(placeholder=placeholder):
                result = {
                    "id": "P1",
                    "q_labels": {"col_1": "无效反馈"},
                    "q_reasons": {"col_1": "模型把占位文本当作回答"},
                    "q_evidence": {"col_1": placeholder},
                    "translations": {},
                }
                valid, missing, errors = annotate_workflow._validated_quality_results(
                    [result], [["P1", placeholder]], 0, [1], False
                )
                self.assertEqual(missing, set())
                self.assertEqual(errors, [])
                self.assertEqual(valid[0]["q_labels"]["col_1"], "N/A")
                self.assertEqual(valid[0]["q_evidence"]["col_1"], "")
                self.assertEqual(
                    annotate_workflow._quality_invalid_cols(
                        valid[0], ["P1", placeholder], [1]
                    ),
                    set(),
                )

    def test_substantive_nonempty_answer_cannot_be_na(self):
        result = {
            "id": "P1",
            "q_labels": {"col_1": "N/A"},
            "q_reasons": {"col_1": "无回答"},
            "q_evidence": {"col_1": ""},
            "translations": {},
        }

        valid, missing, errors = annotate_workflow._validated_quality_results(
            [result], [["P1", "The early game is too weak."]], 0, [1], False
        )

        self.assertEqual(valid, [])
        self.assertEqual(missing, {"P1"})
        self.assertTrue(any("非空回答不能为 N/A" in error for error in errors))

    def test_na_is_excluded_from_overall_denominator(self):
        overall, reason = annotate.calculate_overall_quality(
            {"col_1": "优秀反馈", "col_2": "N/A", "col_3": "优秀反馈", "col_4": "普通反馈"},
            [1, 2, 3, 4],
        )

        self.assertEqual(overall, "优秀反馈")
        self.assertIn("非N/A题目3道", reason)

    def test_missing_and_duplicate_ids_are_blocked_before_annotation(self):
        sid = "test-id-validation"
        annotate_workflow.annotate_sessions[sid] = {
            "rows": [["ID", "反馈"], ["P1", "回答一"], ["P1", "回答二"]],
            "headers": ["ID", "反馈"],
        }
        self.addCleanup(annotate_workflow.annotate_sessions.pop, sid, None)

        with self.assertRaises(HTTPException) as ctx:
            annotate_workflow.annotate_set_column_config(
                sid, 0, [1], {"ai_detect": True, "quality": True}, ""
            )

        self.assertIn("必须唯一", ctx.exception.detail)

    def test_quality_evidence_is_replaced_with_exact_original_when_model_rewrites_it(self):
        rows = [["P1", "技能前摇太长，团战时经常来不及释放"]]
        results = [{
            "id": "P1",
            "q_labels": {"col_1": "优秀反馈"},
            "q_reasons": {"col_1": "给出了具体原因和场景"},
            "q_evidence": {"col_1": "不存在于原文的证据"},
            "translations": {"col_1": "技能前摇太长，团战时经常来不及释放"},
        }]

        valid, missing, errors = annotate_workflow._validated_quality_results(
            results, rows, 0, [1], True
        )

        self.assertEqual(missing, set())
        self.assertEqual(errors, [])
        self.assertEqual(valid[0]["q_evidence"]["col_1"], rows[0][1])

    def test_invalid_optional_counter_evidence_does_not_discard_ai_result(self):
        rows = [["P1", "I played three ranked matches last night."]]
        results = [{
            "id": "P1", "ai_prob": 20, "polish_prob": 10,
            "reason": "包含具体个人经历", "evidence": "",
            "counter_evidence": "模型改写过的反向证据",
            "translations": {"col_1": "我昨晚进行了三场排位赛。"},
        }]

        valid, missing, errors = annotate_workflow._validated_ai_results(
            results, rows, 0, [1]
        )

        self.assertEqual(missing, set())
        self.assertEqual(errors, [])
        self.assertEqual(valid[0]["counter_evidence"], "")

    def test_translation_repair_prompt_and_parser_use_cell_keys(self):
        query = annotate.build_translation_repair_query([
            {"id": "P1", "key": "col_2", "text": "The skill delay is too long."}
        ])
        self.assertIn('"key": "col_2"', query)

        repaired, error = annotate.parse_translation_repair_result(
            '```json\n[{"id":"P1","key":"col_2","translation":"技能延迟太长。"}]\n```'
        )
        self.assertEqual(error, "")
        self.assertEqual(repaired[0]["translation"], "技能延迟太长。")

    def test_effective_batch_size_limits_total_question_cells(self):
        self.assertEqual(
            annotate_workflow._effective_batch_size(15, [1, 2, 3, 4, 5, 6], 36),
            6,
        )
        self.assertEqual(
            annotate_workflow._effective_batch_size(10, [1], 48),
            10,
        )

    def test_annotate_open_text_filter_excludes_empty_and_upload_columns(self):
        headers = ["ID", "Feedback", "Please upload a screenshot", "", "Evidence link"]
        rows = [headers] + [
            [
                f"P{index}",
                f"The skill delay feels too long in ranked match {index}.",
                f"https://example.com/screenshot-{index}.png",
                "",
                f"https://example.com/evidence-{index}.jpg",
            ]
            for index in range(1, 10)
        ]

        filtered = annotate_workflow._filter_annotate_open_text_cols(
            rows, headers, [1, 2, 3, 4],
        )

        self.assertEqual(filtered, [1])
        self.assertEqual(
            annotate_workflow._empty_annotate_column_indexes(rows, headers), [3]
        )

    def test_review_risk_ai_result_requires_original_evidence(self):
        rows = [["P1", "This answer is generic but still source text."]]
        results = [{
            "id": "P1", "ai_prob": 80, "polish_prob": 10,
            "reason": "疑似生成内容", "evidence": "", "counter_evidence": "",
            "translations": {"col_1": "该回答较为泛化，但仍是原文。"},
        }]

        valid, missing, errors = annotate_workflow._validated_ai_results(
            results, rows, 0, [1]
        )

        self.assertEqual(valid, [])
        self.assertEqual(missing, {"P1"})
        self.assertTrue(any("缺少原文证据" in error for error in errors))

    def test_low_risk_rewritten_ai_evidence_is_cleared_without_losing_result(self):
        rows = [["P1", "I played three ranked matches last night."]]
        results = [{
            "id": "P1", "ai_prob": 20, "polish_prob": 10,
            "reason": "包含具体个人经历", "evidence": "模型改写后的证据",
            "counter_evidence": "I played three ranked matches last night.",
            "translations": {"col_1": "我昨晚打了三场排位赛。"},
        }]

        valid, missing, errors = annotate_workflow._validated_ai_results(
            results, rows, 0, [1]
        )

        self.assertEqual(missing, set())
        self.assertEqual(errors, [])
        self.assertEqual(valid[0]["evidence"], "")

    def test_review_risk_rewritten_ai_evidence_is_rejected(self):
        rows = [["P1", "This answer is generic but still source text."]]
        results = [{
            "id": "P1", "ai_prob": 80, "polish_prob": 10,
            "reason": "疑似生成内容", "evidence": "模型改写后的证据",
            "counter_evidence": "", "translations": {"col_1": "中文"},
        }]

        valid, missing, errors = annotate_workflow._validated_ai_results(
            results, rows, 0, [1]
        )

        self.assertEqual(valid, [])
        self.assertEqual(missing, {"P1"})
        self.assertTrue(any("不是连续原文" in error for error in errors))

    def test_markdown_escaped_evidence_maps_back_to_exact_multiline_original(self):
        original = "第一行\n包含 | 竖线\n第三行"
        exact = annotate_workflow._exact_original_evidence(
            "第一行 包含 \\| 竖线", {"col_1": original}
        )

        self.assertEqual(exact, "第一行\n包含 | 竖线")

    def test_excel_preserves_original_and_adds_translation_and_reasons(self):
        rows = [["ID", "Feedback"], ["P1", "The skill delay is too long."]]
        ai_results = [{
            "id": "P1", "ai_prob": 10, "polish_prob": 80,
            "reason": "包含明确观点", "evidence": "", "counter_evidence": "The skill delay is too long.",
            "translations": {"col_1": "技能延迟太长。"},
        }]
        quality_results = [{
            "id": "P1", "overall": "普通反馈", "overall_reason": "非N/A题目1道",
            "q_labels": {"col_1": "普通反馈"},
            "q_reasons": {"col_1": "观点明确但缺少具体案例"},
            "q_evidence": {"col_1": "The skill delay is too long."},
            "translations": {},
        }]

        content = annotate.generate_annotated_excel(
            rows, rows[0], ai_results, set(), quality_results, [1], 0,
            {"ai_detect": True, "quality": True},
        )
        sheet = openpyxl.load_workbook(io.BytesIO(content)).active
        headers = [cell.value for cell in sheet[1]]
        values = [cell.value for cell in sheet[2]]

        self.assertIn("[Feedback]质量标注", headers)
        self.assertIn("[Feedback]质量原因", headers)
        self.assertIn("[Feedback]中文翻译", headers)
        self.assertEqual(values[headers.index("Feedback")], "The skill delay is too long.")
        self.assertEqual(values[headers.index("[Feedback]中文翻译")], "技能延迟太长。")
        self.assertEqual(values[headers.index("AI作答标签")], "非高概率AI作答")

    def test_incomplete_detail_tracks_translations_separately(self):
        detail = annotate_workflow._annotate_incomplete_detail({
            "missing_translation_ids": ["P1", "P2"],
        })

        self.assertIn("中文翻译缺失 2 行", detail)
        self.assertNotIn("AI 检测漏返", detail)

    def test_incomplete_detail_blocks_empty_or_partial_task_results(self):
        session = {
            "rows": [["ID", "Q1"], ["P1", "a"], ["P2", "b"]],
            "id_col": 0,
            "tasks": {"ai_detect": True, "quality": True},
            "ai_status": "complete",
            "ai_confirmation_complete": True,
            "quality_status": "complete",
            "confirmed_ai_ids": [],
            "ai_results": [{"id": "P1"}],
            "quality_results": [],
        }

        detail = annotate_workflow._annotate_incomplete_detail(session)

        self.assertIn("AI 检测漏返 1 行", detail)
        self.assertIn("质量打标漏返 2 行", detail)
        with self.assertRaises(HTTPException):
            annotate_workflow._build_annotate_excel_from_session(session)

    def test_query_budget_trims_only_model_copy_and_splits_large_batches(self):
        source = "x" * 2000
        rows = [["P1", source], ["P2", source]]
        build = lambda batch: "\n".join(str(row[1]) for row in batch)

        batches = annotate_workflow._chunk_rows_by_query_budget(rows, 10, 2500, build)
        model_rows, query = annotate_workflow._fit_rows_to_query_budget(
            rows, [1], 500, build
        )

        self.assertEqual(len(batches), 2)
        self.assertLessEqual(len(query), 500)
        self.assertLess(len(model_rows[0][1]), len(source))
        self.assertEqual(rows[0][1], source)
        with self.assertRaisesRegex(ValueError, "固定内容超过字符预算"):
            annotate_workflow._fit_rows_to_query_budget(
                rows, [1], 10, lambda batch: "fixed prompt that cannot be trimmed"
            )

    def test_only_csv_and_xlsx_upload_formats_are_supported(self):
        self.assertEqual(_parse_file("responses.csv", b"ID,Q1\nP1,answer\n")[1][0], "P1")
        with self.assertRaisesRegex(ValueError, r"\.csv.*\.xlsx"):
            _parse_file("responses.xls", b"not-an-xls")
        with self.assertRaisesRegex(ValueError, "无法解析 .xlsx"):
            _parse_file("responses.xlsx", b"not-an-xlsx")

    def test_manual_quality_review_api_and_service_are_removed(self):
        from app.routers.annotate import router

        paths = {route.path for route in router.routes}
        self.assertFalse(any("quality-review" in path for path in paths))
        self.assertFalse(hasattr(annotate_workflow, "annotate_apply_quality_review"))
        self.assertTrue(all(
            any(
                getattr(
                    getattr(dependency, "dependency", None), "__name__", ""
                ) == "_require_annotate_access"
                for dependency in route.dependencies
            )
            for route in router.routes
        ))


class AnnotateReviewTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _decode_sse_event(raw: str) -> dict:
        return json.loads(raw.removeprefix("data: ").strip())

    async def test_ai_detect_stream_sends_heartbeat_while_batch_is_waiting(self):
        sid = "test-ai-heartbeat"
        annotate_workflow.annotate_sessions[sid] = {
            "rows": [["ID", "Q1"], ["P1", "answer"]],
            "headers": ["ID", "Q1"],
            "id_col": 0,
            "open_text_cols": [1],
            "background": "",
            "tasks": {"ai_detect": True, "quality": True},
        }
        self.addCleanup(annotate_workflow.annotate_sessions.pop, sid, None)

        async def delayed_batch(*args, **kwargs):
            await asyncio.sleep(0.02)
            return 1, [{"id": "P1", "ai_prob": 10}], set(), set(), ""

        with (
            patch.object(annotate_workflow, "_ANNOTATE_SSE_HEARTBEAT_SECONDS", 0.001),
            patch.object(annotate_workflow, "_run_ai_batch_checked", side_effect=delayed_batch),
            patch.object(
                annotate_workflow,
                "_repair_missing_translations",
                new=AsyncMock(return_value=(set(), "")),
            ),
            patch.object(annotate_workflow, "audit_log", new=AsyncMock()),
        ):
            events = [
                self._decode_sse_event(raw)
                async for raw in annotate_workflow.ai_detect_stream(sid, object())
            ]

        self.assertTrue(any(event.get("type") == "heartbeat" for event in events))
        self.assertEqual(events[-1]["type"], "ai_detect_done")

    async def test_ai_retry_processes_only_missing_rows_and_preserves_valid_results(self):
        sid = "test-ai-missing-only"
        existing = {
            "id": "P1", "ai_prob": 10, "polish_prob": 5,
            "reason": "已有可信结果", "evidence": "", "counter_evidence": "answer one",
            "translations": {"col_1": "回答一"},
        }
        repaired = {
            "id": "P2", "ai_prob": 15, "polish_prob": 5,
            "reason": "补回结果", "evidence": "", "counter_evidence": "answer two",
            "translations": {"col_1": "回答二"},
        }
        annotate_workflow.annotate_sessions[sid] = {
            "rows": [["ID", "Q1"], ["P1", "answer one"], ["P2", "answer two"]],
            "headers": ["ID", "Q1"], "id_col": 0, "open_text_cols": [1],
            "background": "", "tasks": {"ai_detect": True, "quality": False},
            "ai_status": "running", "ai_results": [existing],
            "missing_ai_ids": ["P2"],
        }
        self.addCleanup(annotate_workflow.annotate_sessions.pop, sid, None)

        with (
            patch.object(
                annotate_workflow, "_run_ai_batch_checked",
                new=AsyncMock(return_value=(1, [repaired], set(), set(), "")),
            ) as run_batch,
            patch.object(
                annotate_workflow, "_repair_missing_translations",
                new=AsyncMock(return_value=(set(), "")),
            ),
            patch.object(annotate_workflow, "_save_annotate_result_history", new=AsyncMock()),
            patch.object(annotate_workflow, "audit_log", new=AsyncMock()),
        ):
            events = [
                self._decode_sse_event(raw)
                async for raw in annotate_workflow.ai_detect_stream(sid, object())
            ]

        processed_rows = run_batch.await_args.args[2]
        self.assertEqual([row[0] for row in processed_rows], ["P2"])
        self.assertEqual([item["id"] for item in events[-1]["results"]], ["P1", "P2"])
        self.assertIs(annotate_workflow.annotate_sessions[sid]["ai_results"][0], existing)

    async def test_quality_stream_sends_heartbeat_while_batch_is_waiting(self):
        sid = "test-quality-heartbeat"
        annotate_workflow.annotate_sessions[sid] = {
            "rows": [["ID", "Q1"], ["P1", "answer"]],
            "headers": ["ID", "Q1"],
            "id_col": 0,
            "open_text_cols": [1],
            "tasks": {"quality": True},
        }
        self.addCleanup(annotate_workflow.annotate_sessions.pop, sid, None)

        async def delayed_batch(*args, **kwargs):
            await asyncio.sleep(0.02)
            return 1, [{"id": "P1", "translations": {"col_1": "回答"}}], set(), ""

        with (
            patch.object(annotate_workflow, "_ANNOTATE_SSE_HEARTBEAT_SECONDS", 0.001),
            patch.object(annotate_workflow, "_run_one_quality_batch_strict", side_effect=delayed_batch),
            patch.object(annotate_workflow, "_save_annotate_result_history", new=AsyncMock()),
            patch.object(annotate_workflow, "audit_log", new=AsyncMock()),
        ):
            events = [
                self._decode_sse_event(raw)
                async for raw in annotate_workflow.quality_stream(sid, object())
            ]

        self.assertTrue(any(event.get("type") == "heartbeat" for event in events))
        self.assertEqual(events[-1]["type"], "quality_done")

    async def test_quality_retry_processes_only_missing_rows_and_preserves_valid_results(self):
        sid = "test-quality-missing-only"
        existing = {
            "id": "P1", "q_labels": {"col_1": "普通反馈"},
            "q_reasons": {"col_1": "已有可信结果"},
            "q_evidence": {"col_1": "answer one"},
            "translations": {"col_1": "回答一"},
            "overall": "普通反馈", "overall_reason": "普通反馈1道",
        }
        repaired = {
            "id": "P2", "q_labels": {"col_1": "普通反馈"},
            "q_reasons": {"col_1": "补回结果"},
            "q_evidence": {"col_1": "answer two"},
            "translations": {"col_1": "回答二"},
            "overall": "普通反馈", "overall_reason": "普通反馈1道",
        }
        annotate_workflow.annotate_sessions[sid] = {
            "rows": [["ID", "Q1"], ["P1", "answer one"], ["P2", "answer two"]],
            "headers": ["ID", "Q1"], "id_col": 0, "open_text_cols": [1],
            "tasks": {"quality": True}, "quality_status": "running",
            "quality_results": [existing], "missing_quality_ids": ["P2"],
        }
        self.addCleanup(annotate_workflow.annotate_sessions.pop, sid, None)

        with (
            patch.object(
                annotate_workflow, "_run_one_quality_batch_strict",
                new=AsyncMock(return_value=(1, [repaired], set(), "")),
            ) as run_batch,
            patch.object(
                annotate_workflow, "_repair_missing_translations",
                new=AsyncMock(return_value=(set(), "")),
            ),
            patch.object(annotate_workflow, "_save_annotate_result_history", new=AsyncMock()),
            patch.object(annotate_workflow, "audit_log", new=AsyncMock()),
        ):
            events = [
                self._decode_sse_event(raw)
                async for raw in annotate_workflow.quality_stream(sid, object())
            ]

        processed_rows = run_batch.await_args.args[2]
        self.assertEqual([row[0] for row in processed_rows], ["P2"])
        self.assertEqual([item["id"] for item in events[-1]["results"]], ["P1", "P2"])
        self.assertIs(annotate_workflow.annotate_sessions[sid]["quality_results"][0], existing)

    async def test_stream_cancellation_cancels_orphaned_model_tasks(self):
        sid = "test-ai-cancel"
        annotate_workflow.annotate_sessions[sid] = {
            "rows": [["ID", "Q1"], ["P1", "answer"]],
            "headers": ["ID", "Q1"], "id_col": 0, "open_text_cols": [1],
            "background": "", "tasks": {"ai_detect": True}, "ai_status": "running",
        }
        self.addCleanup(annotate_workflow.annotate_sessions.pop, sid, None)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow_batch(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        generator = annotate_workflow.ai_detect_stream(sid, object())
        await generator.__anext__()
        with patch.object(annotate_workflow, "_run_ai_batch_checked", side_effect=slow_batch):
            consumer = asyncio.create_task(generator.__anext__())
            await asyncio.wait_for(started.wait(), timeout=1)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await generator.aclose()

        self.assertTrue(cancelled.is_set())
        self.assertEqual(annotate_workflow.annotate_sessions[sid]["ai_status"], "incomplete")

    async def test_backend_feature_permission_is_enforced(self):
        with (
            patch.object(auth, "_current_login", new=AsyncMock(return_value={"email": "u@test"})),
            patch.object(auth, "_get_user_perms", return_value=["report"]),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await auth._require_feature(object(), "annotate")
        self.assertEqual(ctx.exception.status_code, 403)

        with (
            patch.object(auth, "_current_login", new=AsyncMock(return_value={"email": "u@test"})),
            patch.object(auth, "_get_user_perms", return_value=["annotate"]),
        ):
            login = await auth._require_feature(object(), "annotate")
        self.assertEqual(login["email"], "u@test")

    async def test_quality_stream_does_not_report_translation_gap_as_label_gap(self):
        sid = "test-quality-translation-gap"
        annotate_workflow.annotate_sessions[sid] = {
            "rows": [["ID", "Q1"], ["P1", "answer"]],
            "headers": ["ID", "Q1"],
            "id_col": 0,
            "open_text_cols": [1],
            "tasks": {"quality": True},
            "ai_results": [],
        }
        self.addCleanup(annotate_workflow.annotate_sessions.pop, sid, None)
        quality_result = {
            "id": "P1",
            "q_labels": {"col_1": "普通反馈"},
            "q_reasons": {"col_1": "有观点但细节有限"},
            "q_evidence": {"col_1": "answer"},
            "translations": {},
            "originals": {"col_1": "answer"},
            "overall": "普通反馈",
            "overall_reason": "普通反馈1道",
        }

        with (
            patch.object(
                annotate_workflow,
                "_run_one_quality_batch_strict",
                new=AsyncMock(return_value=(1, [quality_result], set(), "")),
            ),
            patch.object(
                annotate_workflow,
                "_repair_missing_translations",
                new=AsyncMock(return_value=({"P1"}, "中文翻译缺失")),
            ),
            patch.object(annotate_workflow, "_save_annotate_result_history", new=AsyncMock()),
            patch.object(annotate_workflow, "audit_log", new=AsyncMock()),
        ):
            events = [
                self._decode_sse_event(raw)
                async for raw in annotate_workflow.quality_stream(sid, object())
            ]

        done = events[-1]
        self.assertEqual(done["type"], "quality_done")
        self.assertEqual(done["missing_ids"], [])
        self.assertEqual(done["missing_translation_ids"], ["P1"])
        self.assertNotIn("missing_quality_ids", annotate_workflow.annotate_sessions[sid])
        self.assertEqual(
            annotate_workflow.annotate_sessions[sid]["missing_translation_ids"], ["P1"]
        )

    async def test_translation_repair_only_calls_model_for_missing_non_chinese_cells(self):
        results = [{
            "id": "P1",
            "translations": {"col_1": ""},
        }]
        rows = [["P1", "中文原文", "The skill delay is too long.", "666"]]
        response = (
            '```json\n[{"id":"P1","key":"col_2",'
            '"translation":"技能延迟太长。"}]\n```'
        )

        with patch.object(
            annotate_workflow,
            "workflow_run",
            new=AsyncMock(return_value=response),
        ) as call:
            missing, error = await annotate_workflow._repair_missing_translations(
                "sid", results, rows, 0, [1, 2, 3], "key", "test",
            )

        self.assertEqual(missing, set())
        self.assertEqual(error, "")
        self.assertEqual(results[0]["translations"]["col_1"], "中文原文")
        self.assertEqual(results[0]["translations"]["col_2"], "技能延迟太长。")
        self.assertEqual(results[0]["translations"]["col_3"], "666")
        sent_inputs = call.await_args.kwargs["inputs"]
        self.assertEqual(sent_inputs["mode"], "translation_repair")
        sent_query = sent_inputs["query"]
        self.assertNotIn('"key": "col_1"', sent_query)
        self.assertIn('"key": "col_2"', sent_query)
        self.assertNotIn('"key": "col_3"', sent_query)

    async def test_translation_repair_retries_small_batch_then_uses_other_workflow(self):
        results = [{"id": "P1", "translations": {}}]
        rows = [["P1", "The skill delay is too long."]]
        fallback_response = json.dumps([{
            "id": "P1", "key": "col_1", "translation": "技能延迟太长。",
        }])

        with patch.object(
            annotate_workflow,
            "workflow_run",
            new=AsyncMock(side_effect=["[]", "[]", fallback_response]),
        ) as call:
            missing, error = await annotate_workflow._repair_missing_translations(
                "sid", results, rows, 0, [1], "primary-key", "test",
                fallback_api_key="fallback-key",
            )

        self.assertEqual(missing, set())
        self.assertEqual(error, "")
        self.assertEqual(results[0]["translations"]["col_1"], "技能延迟太长。")
        self.assertEqual(call.await_count, 3)
        self.assertEqual(call.await_args_list[0].kwargs["api_key"], "primary-key")
        self.assertEqual(call.await_args_list[1].kwargs["api_key"], "primary-key")
        self.assertEqual(call.await_args_list[2].kwargs["api_key"], "fallback-key")

    def test_translation_validation_accepts_terms_that_should_remain_unchanged(self):
        for value in ("Aulus", "MLBB", "N/A", "https://example.com/image.png"):
            with self.subTest(value=value):
                self.assertTrue(annotate_workflow._translation_is_usable(value, value))
        self.assertFalse(
            annotate_workflow._translation_is_usable(
                "The skill delay is too long.", "The skill delay is too long."
            )
        )

    async def test_ai_batch_keeps_valid_label_when_translation_is_missing(self):
        direct_result = [{
            "id": "P1",
            "ai_prob": 15,
            "polish_prob": 70,
            "reason": "包含具体个人体验",
            "evidence": "",
            "counter_evidence": "I played three ranked matches last night.",
            "translations": {},
        }]

        with (
            patch.object(
                annotate_workflow,
                "_run_ai_direct_batch",
                new=AsyncMock(return_value=(direct_result, "")),
            ),
            patch.object(
                annotate_workflow,
                "_repair_missing_translations",
                new=AsyncMock(return_value=({"P1"}, "中文翻译缺失")),
            ),
        ):
            _, results, missing, missing_translations, _ = (
                await annotate_workflow._run_ai_batch_checked(
                    "sid", 1,
                    [["P1", "I played three ranked matches last night."]],
                    ["ID", "Q1"], [1], 0, "",
                )
            )

        self.assertEqual([result["id"] for result in results], ["P1"])
        self.assertEqual(missing, set())
        self.assertEqual(missing_translations, {"P1"})

    async def test_ai_batch_uses_ai_detect_workflow_mode(self):
        response = json.dumps([{
            "id": "P1",
            "ai_prob": 15,
            "polish_prob": 70,
            "reason": "包含具体个人体验",
            "evidence": "",
            "counter_evidence": "I played three ranked matches last night.",
        }])

        with patch.object(
            annotate_workflow, "workflow_run", new=AsyncMock(return_value=response)
        ) as call:
            results, error = await annotate_workflow._run_ai_direct_batch(
                "sid", [["P1", "I played three ranked matches last night."]],
                ["ID", "Q1"], [1], 0, "", "key", "1",
            )

        self.assertEqual(error, "")
        self.assertEqual(results[0]["id"], "P1")
        sent_inputs = call.await_args.kwargs["inputs"]
        self.assertEqual(sent_inputs["mode"], "ai_detect")
        self.assertIn("I played three ranked matches last night.", sent_inputs["query"])

    async def test_header_translation_uses_translation_repair_workflow_mode(self):
        first_response = json.dumps([
            {"id": "__headers__", "key": "col_0", "translation": "玩家ID"},
        ])
        second_response = json.dumps([
            {"id": "__headers__", "key": "col_1", "translation": "反馈"},
        ])

        with patch.object(
            annotate_workflow,
            "workflow_run",
            new=AsyncMock(side_effect=[first_response, second_response]),
        ) as call:
            translated, warning = await annotate_workflow._translate_headers(
                ["ID", "Feedback", "中文列"]
            )

        self.assertEqual(translated, ["玩家ID", "反馈", "中文列"])
        self.assertEqual(warning, "")
        self.assertEqual(call.await_count, 2)
        sent_inputs = call.await_args_list[0].kwargs["inputs"]
        self.assertEqual(sent_inputs["mode"], "translation_repair")
        self.assertNotIn("中文列", sent_inputs["query"])
        retry_inputs = call.await_args_list[1].kwargs["inputs"]
        self.assertNotIn('"key": "col_0"', retry_inputs["query"])
        self.assertIn('"key": "col_1"', retry_inputs["query"])

    async def test_header_translation_warns_after_targeted_retry_is_exhausted(self):
        with patch.object(
            annotate_workflow, "workflow_run", new=AsyncMock(return_value="[]")
        ) as call:
            translated, warning = await annotate_workflow._translate_headers(["Feedback"])

        self.assertEqual(translated, ["Feedback"])
        self.assertIn("1 个列名翻译失败", warning)
        self.assertEqual(call.await_count, 2)

    async def test_quality_repair_preserves_valid_questions_and_retries_only_invalid_columns(self):
        first = [{
            "id": "P1",
            "q_labels": {"col_1": "优秀反馈", "col_2": "N/A"},
            "q_reasons": {"col_1": "包含具体场景", "col_2": "无回答"},
            "q_evidence": {"col_1": "first answer", "col_2": ""},
            "translations": {},
        }]
        repaired = [{
            "id": "P1",
            "q_labels": {"col_2": "普通反馈"},
            "q_reasons": {"col_2": "回答了问题但细节有限"},
            "q_evidence": {"col_2": "second answer"},
            "translations": {},
        }]
        responses = [first, repaired]
        calls = []

        async def fake_workflow_run(*, inputs, **kwargs):
            calls.append(inputs)
            payload = responses.pop(0)
            return json.dumps(payload)

        with patch.object(annotate_workflow, "workflow_run", new=fake_workflow_run):
            _, results, missing, error = await annotate_workflow._run_one_quality_batch_strict(
                "sid", 1, [["P1", "first answer", "second answer"]],
                ["ID", "Q1", "Q2"], [1, 2], 0, False,
            )

        self.assertEqual(missing, set())
        self.assertEqual(error, "")
        self.assertEqual(results[0]["q_labels"]["col_1"], "优秀反馈")
        self.assertEqual(results[0]["q_labels"]["col_2"], "普通反馈")
        self.assertTrue(all(call["mode"] == "quality_label" for call in calls))
        self.assertIn("col_2", calls[1]["query"])
        self.assertNotIn("col_1", calls[1]["query"])

if __name__ == "__main__":
    unittest.main()
