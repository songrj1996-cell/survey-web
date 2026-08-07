import json
from pathlib import Path
import tempfile
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

from openpyxl import Workbook

from app.core.interview_parsing import (
    interview_source_refs,
    parse_interview_workbook,
    serialize_interview_workbook,
)
from app.services import interview_service, report_history
from app.storage import history as history_storage


def _workbook_bytes() -> bytes:
    workbook = Workbook()
    first = workbook.active
    first.title = "记录者A"
    first["A1"] = "玩家"
    first["B1"] = "战斗功能"
    first["A2"] = "玩家甲"
    first["B2"] = "希望失败后能立刻知道原因"
    second = workbook.create_sheet("记录者B")
    second["A1"] = "玩家甲"
    second["B1"] = "看不懂失败来自操作还是数值"
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


class _Request:
    async def is_disconnected(self):
        return False


class InterviewParsingTests(unittest.TestCase):
    def test_reads_every_sheet_and_preserves_cell_references(self):
        parsed = parse_interview_workbook("访谈记录.xlsx", _workbook_bytes())

        self.assertEqual([sheet["name"] for sheet in parsed["sheets"]], ["记录者A", "记录者B"])
        self.assertEqual(parsed["total_cells"], 6)
        refs = interview_source_refs(parsed)
        self.assertIn("记录者A!B2", refs)
        self.assertIn("记录者B!B1", refs)
        source_text = serialize_interview_workbook(parsed)
        self.assertIn("[记录者A!B2] 希望失败后能立刻知道原因", source_text)

    def test_rejects_non_xlsx(self):
        with self.assertRaisesRegex(Exception, "仅支持 .xlsx"):
            parse_interview_workbook("访谈记录.csv", b"not xlsx")


class InterviewServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        interview_service._INTERVIEW_LOCKS.clear()

    async def test_output_limit_switches_to_visible_fallback_model(self):
        llm = AsyncMock(
            side_effect=[
                RuntimeError(
                    "LLM stopped before completing output model=primary "
                    "protocol=responses; reason=max_output_tokens"
                ),
                ("完整结果", "gpt-5.5"),
            ]
        )

        with (
            patch.object(interview_service, "INTERVIEW_FALLBACK_MODELS", ("gpt-5.5",)),
            patch.object(interview_service, "collect_chat_completion", new=llm),
        ):
            results = [
                item
                async for item in interview_service._collect_stage(
                    messages=[{"role": "user", "content": "生成"}],
                    model="primary",
                    reasoning="medium",
                    max_tokens=32000,
                    request=_Request(),
                    stage="extract",
                    percent=8,
                )
            ]

        streamed = "".join(
            payload for kind, payload in results if kind == "heartbeat"
        )
        self.assertIn('"model": "primary"', streamed)
        self.assertIn('"model": "gpt-5.5"', streamed)
        self.assertIn('"is_fallback": true', streamed)
        self.assertIn('"fallback_reason": "output_limit"', streamed)
        self.assertEqual(results[-1], ("result", ("完整结果", "gpt-5.5")))
        self.assertEqual(
            [call.kwargs["models"] for call in llm.await_args_list],
            [("primary",), ("gpt-5.5",)],
        )
        self.assertEqual(
            [call.kwargs["max_tokens"] for call in llm.await_args_list],
            [32000, 32000],
        )

    async def test_upload_builds_interview_session_without_changing_source_data(self):
        stored = {}

        def fake_save(session_id, sess):
            stored.update(sess)

        with (
            patch.object(interview_service, "new_session", return_value="12345678-1234-1234-1234-123456789abc"),
            patch.object(interview_service, "get_session", return_value={"ts": 1}),
            patch.object(interview_service, "save_session", side_effect=fake_save),
        ):
            result = await interview_service.handle_interview_upload(
                "访谈记录.xlsx",
                _workbook_bytes(),
                None,
                "重点关注失败反馈",
            )

        self.assertEqual(result["total_cells"], 6)
        self.assertEqual(len(result["sheets"]), 2)
        self.assertEqual(stored["kind"], "interview")
        self.assertEqual(stored["mode"], "interview")
        self.assertEqual(stored["interview_research_focus"], "重点关注失败反馈")
        self.assertIn("[记录者B!B1]", stored["interview_source_text"])

    async def test_report_pipeline_uses_stage_models_and_emits_markdown(self):
        parsed = parse_interview_workbook("访谈记录.xlsx", _workbook_bytes())
        sess = {
            "session_id": "12345678-1234-1234-1234-123456789abc",
            "kind": "interview",
            "mode": "interview",
            "filename": "访谈记录.xlsx",
            "interview_workbook": parsed,
            "interview_source_text": serialize_interview_workbook(parsed),
        }
        extraction = {
            "players": [{"player_id": "P01", "aliases": ["玩家甲"]}],
            "modules": [
                {
                    "title": "战斗反馈",
                    "evidence": [
                        {
                            "player_id": "P01",
                            "need": "快速理解失败原因",
                            "logic_reason": "需要决定继续练习还是调整数值",
                            "finding": "失败反馈不够可诊断",
                            "record_excerpt": "希望失败后能立刻知道原因",
                            "source_refs": ["记录者A!B2", "不存在!Z99"],
                        }
                    ],
                }
            ],
            "limitations": [],
        }
        report = (
            "## 战斗反馈\n\n"
            "### 模块判断\n\n玩家需要快速理解失败原因，因为这会影响后续调整。\n\n"
            "### 主要发现\n\n#### 发现1：反馈不够可诊断\n\n"
            "- P01：希望失败后能立刻知道原因。[来源：记录者A!B2]\n\n"
            "### 产品建议\n\n- 增加失败原因提示。"
        )
        responses = [
            (json.dumps(extraction, ensure_ascii=False), "gpt-5.6-terra"),
            (report, "gpt-5.6-sol"),
            ('{"ok":true,"issues":[]}', "gpt-5.6-terra"),
        ]

        with (
            patch.object(interview_service, "get_session", return_value=sess),
            patch.object(interview_service, "_visible_to_owner", return_value=True),
            patch.object(interview_service, "save_session") as save_session,
            patch.object(
                interview_service,
                "save_to_history",
                return_value={"report_no": "R-001"},
            ) as save_history,
            patch.object(
                interview_service,
                "collect_chat_completion",
                new=AsyncMock(side_effect=responses),
            ) as llm,
        ):
            chunks = [
                chunk
                async for chunk in interview_service.interview_report_stream(
                    "12345678-1234-1234-1234-123456789abc",
                    _Request(),
                )
            ]

        combined = "".join(chunks)
        self.assertIn('"type": "interview_done"', combined)
        self.assertIn('"type": "interview_module_done"', combined)
        self.assertIn('"percent": 100', combined)
        self.assertIn("### 模块判断", combined)
        self.assertIn("### 主要发现", combined)
        self.assertIn("### 产品建议", combined)
        self.assertNotIn("不存在!Z99", json.dumps(sess["interview_extraction"], ensure_ascii=False))
        self.assertEqual(sess["interview_status"], "completed")
        self.assertEqual(sess["interview_report_no"], "R-001")
        self.assertEqual(llm.await_count, 3)
        self.assertEqual(
            [call.kwargs["models"][0] for call in llm.await_args_list],
            [
                interview_service.INTERVIEW_EXTRACT_MODEL,
                interview_service.INTERVIEW_REPORT_MODEL,
                interview_service.INTERVIEW_AUDIT_MODEL,
            ],
        )
        self.assertEqual(
            llm.await_args_list[0].kwargs["max_tokens"],
            interview_service.INTERVIEW_EXTRACT_MAX_TOKENS,
        )
        self.assertGreaterEqual(save_session.call_count, 5)
        save_history.assert_called_once()

    async def test_report_pipeline_resumes_from_completed_module_checkpoint(self):
        parsed = parse_interview_workbook("访谈记录.xlsx", _workbook_bytes())
        first_module_md = (
            "## 战斗反馈\n\n### 模块判断\n\n玩家需要理解失败原因。\n\n"
            "### 主要发现\n\n#### 发现1：失败原因不清楚\n\n"
            "- P01：希望失败后能立刻知道原因。[来源：记录者A!B2]\n\n"
            "### 产品建议\n\n- 增加失败原因提示。"
        )
        second_module_md = (
            "## 学习成本\n\n### 模块判断\n\n玩家需要区分操作和数值问题。\n\n"
            "### 主要发现\n\n#### 发现1：诊断成本较高\n\n"
            "- P01：看不懂失败来自操作还是数值。[来源：记录者B!B1]\n\n"
            "### 产品建议\n\n- 提供分层诊断。"
        )
        extraction = {
            "players": [{"player_id": "P01", "aliases": ["玩家甲"]}],
            "modules": [
                {
                    "title": "战斗反馈",
                    "evidence": [{"player_id": "P01", "source_refs": ["记录者A!B2"]}],
                },
                {
                    "title": "学习成本",
                    "evidence": [{"player_id": "P01", "source_refs": ["记录者B!B1"]}],
                },
            ],
            "limitations": [],
        }
        sess = {
            "session_id": "22345678-1234-1234-1234-123456789abc",
            "kind": "interview",
            "mode": "interview",
            "filename": "访谈记录.xlsx",
            "interview_workbook": parsed,
            "interview_source_text": serialize_interview_workbook(parsed),
            "interview_extraction": extraction,
            "interview_module_reports": [
                {"title": "战斗反馈", "report_md": first_module_md},
            ],
        }

        with (
            patch.object(interview_service, "get_session", return_value=sess),
            patch.object(interview_service, "_visible_to_owner", return_value=True),
            patch.object(interview_service, "save_session"),
            patch.object(
                interview_service,
                "save_to_history",
                return_value={"report_no": "R-002"},
            ),
            patch.object(
                interview_service,
                "collect_chat_completion",
                new=AsyncMock(
                    side_effect=[
                        (second_module_md, "gpt-5.6-sol"),
                        ('{"ok":true,"issues":[]}', "gpt-5.6-terra"),
                    ]
                ),
            ) as llm,
        ):
            chunks = [
                chunk
                async for chunk in interview_service.interview_report_stream(
                    sess["session_id"],
                    _Request(),
                )
            ]

        self.assertEqual(llm.await_count, 2)
        self.assertEqual(llm.await_args_list[0].kwargs["models"][0], interview_service.INTERVIEW_REPORT_MODEL)
        self.assertIn("学习成本", "".join(chunks))
        self.assertEqual(len(sess["interview_module_reports"]), 2)

    async def test_failed_audit_repairs_module_and_runs_second_audit(self):
        parsed = parse_interview_workbook("访谈记录.xlsx", _workbook_bytes())
        original_md = (
            "## 战斗反馈\n\n### 模块判断\n\n玩家需要理解失败原因。\n\n"
            "### 主要发现\n\n#### 发现1：反馈不足\n\n"
            "- P01：希望失败后能立刻知道原因。[来源：记录者A!B2]\n\n"
            "### 产品建议\n\n- 增加提示。"
        )
        repaired_md = original_md.replace(
            "玩家需要理解失败原因。",
            "玩家需要理解失败原因，因为这会决定继续练习操作还是调整数值。",
        )
        extraction = {
            "players": [{"player_id": "P01"}],
            "modules": [
                {
                    "title": "战斗反馈",
                    "evidence": [
                        {
                            "player_id": "P01",
                            "logic_reason": "决定继续练习还是调整数值",
                            "source_refs": ["记录者A!B2"],
                        }
                    ],
                }
            ],
            "limitations": [],
        }
        sess = {
            "session_id": "32345678-1234-1234-1234-123456789abc",
            "kind": "interview",
            "mode": "interview",
            "filename": "访谈记录.xlsx",
            "interview_workbook": parsed,
            "interview_source_text": serialize_interview_workbook(parsed),
            "interview_extraction": extraction,
            "interview_module_reports": [
                {"title": "战斗反馈", "report_md": original_md},
            ],
        }
        responses = [
            (
                '{"ok":false,"issues":[{"module_title":"战斗反馈",'
                '"problem":"模块判断缺少行为逻辑","suggestion":"补充决策原因"}]}',
                "gpt-5.6-terra",
            ),
            (repaired_md, "gpt-5.6-sol"),
            ('{"ok":true,"issues":[]}', "gpt-5.6-terra"),
        ]

        with (
            patch.object(interview_service, "get_session", return_value=sess),
            patch.object(interview_service, "_visible_to_owner", return_value=True),
            patch.object(interview_service, "save_session"),
            patch.object(
                interview_service,
                "save_to_history",
                return_value={"report_no": "R-003"},
            ),
            patch.object(
                interview_service,
                "collect_chat_completion",
                new=AsyncMock(side_effect=responses),
            ) as llm,
        ):
            chunks = [
                chunk
                async for chunk in interview_service.interview_report_stream(
                    sess["session_id"],
                    _Request(),
                )
            ]

        self.assertIn('"stage": "repair"', "".join(chunks))
        self.assertIn("因为这会决定", sess["report_md"])
        self.assertEqual(llm.await_count, 3)
        self.assertEqual(
            [call.kwargs["models"][0] for call in llm.await_args_list],
            [
                interview_service.INTERVIEW_AUDIT_MODEL,
                interview_service.INTERVIEW_REPAIR_MODEL,
                interview_service.INTERVIEW_AUDIT_MODEL,
            ],
        )

    async def test_exhausted_quality_audit_completes_with_warning(self):
        parsed = parse_interview_workbook("访谈记录.xlsx", _workbook_bytes())
        module_md = (
            "## 战斗反馈\n\n### 模块判断\n\n"
            "玩家需要理解失败原因，因为这会决定继续练习还是调整数值。\n\n"
            "### 主要发现\n\n#### 发现1：反馈不足\n\n"
            "- P01：希望失败后能立刻知道原因。[来源：记录者A!B2]\n\n"
            "### 产品建议\n\n- 增加提示。"
        )
        extraction = {
            "players": [{"player_id": "P01"}],
            "modules": [
                {
                    "title": "战斗反馈",
                    "evidence": [
                        {
                            "player_id": "P01",
                            "source_refs": ["记录者A!B2"],
                        }
                    ],
                }
            ],
            "limitations": [],
        }
        sess = {
            "session_id": "42345678-1234-1234-1234-123456789abc",
            "kind": "interview",
            "mode": "interview",
            "filename": "访谈记录.xlsx",
            "interview_workbook": parsed,
            "interview_source_text": serialize_interview_workbook(parsed),
            "interview_extraction": extraction,
            "interview_module_reports": [
                {"title": "战斗反馈", "report_md": module_md},
            ],
        }
        audit_issue = (
            '{"ok":false,"issues":[{"module_title":"战斗反馈",'
            '"problem":"需求逻辑仍可更清楚","suggestion":"继续补充"}]}'
        )
        responses = [
            (audit_issue, "gpt-5.6-terra"),
            (module_md, "gpt-5.6-sol"),
            (audit_issue, "gpt-5.6-terra"),
            (module_md, "gpt-5.6-sol"),
            (audit_issue, "gpt-5.6-terra"),
        ]

        with (
            patch.object(interview_service, "get_session", return_value=sess),
            patch.object(interview_service, "_visible_to_owner", return_value=True),
            patch.object(interview_service, "save_session"),
            patch.object(
                interview_service,
                "save_to_history",
                return_value={"report_no": "R-004"},
            ) as save_history,
            patch.object(
                interview_service,
                "collect_chat_completion",
                new=AsyncMock(side_effect=responses),
            ),
        ):
            chunks = [
                chunk
                async for chunk in interview_service.interview_report_stream(
                    sess["session_id"],
                    _Request(),
                )
            ]

        combined = "".join(chunks)
        self.assertIn('"type": "interview_module_repaired"', combined)
        self.assertIn('"type": "interview_done"', combined)
        self.assertNotIn('"type": "error"', combined)
        self.assertEqual(sess["interview_status"], "completed")
        self.assertEqual(sess["interview_audit"]["status"], "warning")
        self.assertTrue(sess["interview_audit"]["repair_exhausted"])
        self.assertIn("质量提醒", sess["interview_progress_message"])
        save_history.assert_called_once()

    async def test_invalid_repaired_reference_remains_blocking_and_is_logged(self):
        parsed = parse_interview_workbook("访谈记录.xlsx", _workbook_bytes())
        module_md = (
            "## 战斗反馈\n\n### 模块判断\n\n玩家需要理解失败原因。\n\n"
            "### 主要发现\n\n#### 发现1：反馈不足\n\n"
            "- P01：希望失败后能立刻知道原因。[来源：记录者A!B2]\n\n"
            "### 产品建议\n\n- 增加提示。"
        )
        invalid_repair = module_md.replace("记录者A!B2", "不存在!Z99")
        extraction = {
            "players": [{"player_id": "P01"}],
            "modules": [
                {
                    "title": "战斗反馈",
                    "evidence": [{"player_id": "P01", "source_refs": ["记录者A!B2"]}],
                }
            ],
            "limitations": [],
        }
        sess = {
            "session_id": "52345678-1234-1234-1234-123456789abc",
            "kind": "interview",
            "mode": "interview",
            "filename": "访谈记录.xlsx",
            "interview_workbook": parsed,
            "interview_source_text": serialize_interview_workbook(parsed),
            "interview_extraction": extraction,
            "interview_module_reports": [{"title": "战斗反馈", "report_md": module_md}],
        }
        responses = [
            (
                '{"ok":false,"issues":[{"module_title":"战斗反馈",'
                '"problem":"需要修订","suggestion":"补充"}]}',
                "gpt-5.6-terra",
            ),
            (invalid_repair, "gpt-5.6-sol"),
        ]

        with (
            patch.object(interview_service, "get_session", return_value=sess),
            patch.object(interview_service, "_visible_to_owner", return_value=True),
            patch.object(interview_service, "save_session"),
            patch.object(interview_service, "save_to_history") as save_history,
            patch.object(
                interview_service,
                "collect_chat_completion",
                new=AsyncMock(side_effect=responses),
            ),
            patch.object(interview_service.logger, "exception") as log_exception,
        ):
            chunks = [
                chunk
                async for chunk in interview_service.interview_report_stream(
                    sess["session_id"],
                    _Request(),
                )
            ]

        combined = "".join(chunks)
        self.assertIn('"type": "error"', combined)
        self.assertNotIn('"type": "interview_done"', combined)
        self.assertEqual(sess["interview_status"], "failed")
        save_history.assert_not_called()
        log_exception.assert_called_once()

    async def test_manual_review_revises_module_and_reaudits_before_saving(self):
        parsed = parse_interview_workbook("访谈记录.xlsx", _workbook_bytes())
        original_module = (
            "## 战斗反馈\n\n### 模块判断\n\n玩家需要理解失败原因。\n\n"
            "### 主要发现\n\n#### 发现1：反馈不足\n\n"
            "- P01：希望失败后能立刻知道原因。[来源：记录者A!B2]\n\n"
            "### 产品建议\n\n- 增加提示。"
        )
        repaired_module = original_module.replace(
            "玩家需要理解失败原因。",
            "玩家需要理解失败原因，因为这会决定继续练习还是调整数值。",
        )
        extraction = {
            "players": [{"player_id": "P01"}],
            "modules": [{
                "title": "战斗反馈",
                "evidence": [{
                    "player_id": "P01",
                    "source_refs": ["记录者A!B2"],
                }],
            }],
            "limitations": [],
        }
        sess = {
            "session_id": "62345678-1234-1234-1234-123456789abc",
            "kind": "interview",
            "mode": "interview",
            "filename": "访谈记录.xlsx",
            "interview_status": "completed",
            "interview_workbook": parsed,
            "interview_source_text": serialize_interview_workbook(parsed),
            "interview_extraction": extraction,
            "interview_module_reports": [{
                "title": "战斗反馈",
                "report_md": original_module,
            }],
            "interview_audit": {
                "status": "warning",
                "issues": [{
                    "module_title": "战斗反馈",
                    "problem": "需求逻辑不够清楚",
                    "suggestion": "补充后续决策原因",
                }],
            },
            "report_md": f"# 访谈报告\n\n{original_module}",
        }
        responses = [
            (repaired_module, "gpt-5.6-sol"),
            ('{"ok":true,"issues":[],"summary":"通过"}', "gpt-5.6-terra"),
        ]

        with (
            patch.object(interview_service, "get_session", return_value=sess),
            patch.object(interview_service, "_visible_to_owner", return_value=True),
            patch.object(interview_service, "save_session"),
            patch.object(
                interview_service,
                "save_to_history",
                return_value={"report_no": "R-006"},
            ) as save_history,
            patch.object(
                interview_service,
                "collect_chat_completion",
                new=AsyncMock(side_effect=responses),
            ),
        ):
            chunks = [
                chunk
                async for chunk in interview_service.revise_interview_audit_issue_stream(
                    sess["session_id"],
                    0,
                    _Request(),
                    None,
                )
            ]

        combined = "".join(chunks)
        self.assertIn('"type": "interview_review_progress"', combined)
        self.assertIn('"type": "interview_review_done"', combined)
        self.assertNotIn('"type": "error"', combined)
        self.assertIn("因为这会决定继续练习", sess["report_md"])
        self.assertEqual(sess["interview_audit"]["issues"], [])
        self.assertEqual(sess["interview_audit"]["status"], "passed")
        save_history.assert_called_once()

    async def test_manual_review_invalid_repair_does_not_overwrite_report(self):
        parsed = parse_interview_workbook("访谈记录.xlsx", _workbook_bytes())
        original_module = (
            "## 战斗反馈\n\n### 模块判断\n\n玩家需要理解失败原因。\n\n"
            "### 主要发现\n\n#### 发现1：反馈不足\n\n"
            "- P01：希望失败后能立刻知道原因。[来源：记录者A!B2]\n\n"
            "### 产品建议\n\n- 增加提示。"
        )
        sess = {
            "session_id": "72345678-1234-1234-1234-123456789abc",
            "kind": "interview",
            "mode": "interview",
            "filename": "访谈记录.xlsx",
            "interview_status": "completed",
            "interview_workbook": parsed,
            "interview_source_text": serialize_interview_workbook(parsed),
            "interview_extraction": {
                "players": [{"player_id": "P01"}],
                "modules": [{
                    "title": "战斗反馈",
                    "evidence": [{
                        "player_id": "P01",
                        "source_refs": ["记录者A!B2"],
                    }],
                }],
            },
            "interview_module_reports": [{
                "title": "战斗反馈",
                "report_md": original_module,
            }],
            "interview_audit": {
                "status": "warning",
                "issues": [{
                    "module_title": "战斗反馈",
                    "problem": "需求逻辑不够清楚",
                    "suggestion": "补充原因",
                }],
            },
            "report_md": f"# 访谈报告\n\n{original_module}",
        }
        original_report = sess["report_md"]
        invalid_repair = original_module.replace("记录者A!B2", "不存在!Z99")

        with (
            patch.object(interview_service, "get_session", return_value=sess),
            patch.object(interview_service, "_visible_to_owner", return_value=True),
            patch.object(interview_service, "save_session"),
            patch.object(interview_service, "save_to_history") as save_history,
            patch.object(
                interview_service,
                "collect_chat_completion",
                new=AsyncMock(return_value=(invalid_repair, "gpt-5.6-sol")),
            ),
            patch.object(interview_service.logger, "exception"),
        ):
            chunks = [
                chunk
                async for chunk in interview_service.revise_interview_audit_issue_stream(
                    sess["session_id"],
                    0,
                    _Request(),
                    None,
                )
            ]

        self.assertIn('"type": "error"', "".join(chunks))
        self.assertEqual(sess["report_md"], original_report)
        save_history.assert_not_called()

    async def test_manual_review_keeps_untouched_issues_when_reaudit_returns_passed(self):
        parsed = parse_interview_workbook("访谈记录.xlsx", _workbook_bytes())
        original_module = (
            "## 战斗反馈\n\n### 模块判断\n\n玩家需要理解失败原因。\n\n"
            "### 主要发现\n\n#### 发现1：反馈不足\n\n"
            "- P01：希望失败后能立刻知道原因。[来源：记录者A!B2]\n\n"
            "### 产品建议\n\n- 增加提示。"
        )
        repaired_module = original_module.replace(
            "玩家需要理解失败原因。",
            "玩家需要理解失败原因，因为这会影响后续决策。",
        )
        untouched_issue = {
            "module_title": "组队沟通",
            "problem": "建议缺少直接证据",
            "suggestion": "补充证据或收窄表述",
        }
        sess = {
            "session_id": "82345678-1234-1234-1234-123456789abc",
            "kind": "interview",
            "mode": "interview",
            "filename": "访谈记录.xlsx",
            "interview_status": "completed",
            "interview_workbook": parsed,
            "interview_source_text": serialize_interview_workbook(parsed),
            "interview_extraction": {
                "players": [{"player_id": "P01"}],
                "modules": [{
                    "title": "战斗反馈",
                    "evidence": [{
                        "player_id": "P01",
                        "source_refs": ["记录者A!B2"],
                    }],
                }],
            },
            "interview_module_reports": [{
                "title": "战斗反馈",
                "report_md": original_module,
            }],
            "interview_audit": {
                "status": "warning",
                "issues": [
                    {
                        "module_title": "战斗反馈",
                        "problem": "需求逻辑不够清楚",
                        "suggestion": "补充原因",
                    },
                    untouched_issue,
                ],
            },
            "report_md": f"# 访谈报告\n\n{original_module}",
        }
        responses = [
            (repaired_module, "gpt-5.6-sol"),
            ('{"ok":true,"issues":[],"summary":"通过"}', "gpt-5.6-terra"),
        ]

        with (
            patch.object(interview_service, "get_session", return_value=sess),
            patch.object(interview_service, "_visible_to_owner", return_value=True),
            patch.object(interview_service, "save_session"),
            patch.object(interview_service, "save_to_history", return_value={"report_no": "R-008"}),
            patch.object(
                interview_service,
                "collect_chat_completion",
                new=AsyncMock(side_effect=responses),
            ),
        ):
            chunks = [
                chunk
                async for chunk in interview_service.revise_interview_audit_issue_stream(
                    sess["session_id"],
                    0,
                    _Request(),
                    None,
                )
            ]

        self.assertIn('"type": "interview_review_done"', "".join(chunks))
        self.assertEqual(sess["interview_audit"]["issues"], [untouched_issue])
        self.assertFalse(sess["interview_audit"]["ok"])
        self.assertEqual(sess["interview_audit"]["status"], "warning")
        self.assertEqual(
            sess["interview_progress_message"],
            "批量修订完成；仍有待确认提醒",
        )

    def test_manual_review_reconciliation_keeps_order_and_deduplicates_new_issues(self):
        selected = {
            "module_title": "战斗反馈",
            "problem": "需求逻辑不够清楚",
            "suggestion": "补充原因",
        }
        untouched = {
            "module_title": "组队沟通",
            "problem": "建议缺少直接证据",
            "suggestion": "补充证据",
        }
        new_issue = {
            "module_title": "藏品展示",
            "problem": "样本范围不明确",
            "suggestion": "补充范围",
        }

        reconciled = interview_service._reconcile_manual_audit_issues(
            [selected, untouched],
            {0},
            [dict(untouched), new_issue],
        )

        self.assertEqual(reconciled, [untouched, new_issue])

    async def test_batch_review_groups_same_module_and_audits_once(self):
        parsed = parse_interview_workbook("访谈记录.xlsx", _workbook_bytes())
        original_module = (
            "## 战斗反馈\n\n### 模块判断\n\n玩家需要理解失败原因。\n\n"
            "### 主要发现\n\n#### 发现1：反馈不足\n\n"
            "- P01：希望失败后能立刻知道原因。[来源：记录者A!B2]\n\n"
            "### 产品建议\n\n- 增加提示。"
        )
        repaired_module = original_module.replace(
            "玩家需要理解失败原因。",
            "玩家需要理解失败原因，因为这会影响后续决策。",
        )
        issues = [
            {
                "module_title": "战斗反馈",
                "problem": "需求逻辑不够清楚",
                "suggestion": "补充原因",
            },
            {
                "module_title": "战斗反馈",
                "problem": "建议过于宽泛",
                "suggestion": "收窄建议范围",
            },
        ]
        sess = {
            "session_id": "92345678-1234-1234-1234-123456789abc",
            "kind": "interview",
            "mode": "interview",
            "filename": "访谈记录.xlsx",
            "interview_status": "completed",
            "interview_workbook": parsed,
            "interview_source_text": serialize_interview_workbook(parsed),
            "interview_extraction": {
                "players": [{"player_id": "P01"}],
                "modules": [{
                    "title": "战斗反馈",
                    "evidence": [{
                        "player_id": "P01",
                        "source_refs": ["记录者A!B2"],
                    }],
                }],
            },
            "interview_module_reports": [{
                "title": "战斗反馈",
                "report_md": original_module,
            }],
            "interview_audit": {"status": "warning", "issues": issues},
            "report_md": f"# 访谈报告\n\n{original_module}",
        }
        selections = [
            {"issue_index": index, **issue}
            for index, issue in enumerate(issues)
        ]
        completion = AsyncMock(side_effect=[
            (repaired_module, "gpt-5.6-sol"),
            ('{"ok":true,"issues":[],"summary":"通过"}', "gpt-5.6-terra"),
        ])

        with (
            patch.object(interview_service, "get_session", return_value=sess),
            patch.object(interview_service, "_visible_to_owner", return_value=True),
            patch.object(interview_service, "save_session"),
            patch.object(interview_service, "save_to_history", return_value={"report_no": "R-009"}),
            patch.object(interview_service, "collect_chat_completion", new=completion),
        ):
            chunks = [
                chunk
                async for chunk in interview_service.revise_interview_audit_issues_stream(
                    sess["session_id"], selections, _Request(), None
                )
            ]

        self.assertIn('"type": "interview_review_done"', "".join(chunks))
        self.assertEqual(completion.await_count, 2)
        self.assertEqual(sess["interview_audit"]["issues"], [])
        self.assertEqual(sess["interview_audit"]["manual_review_round"], 1)

    async def test_batch_review_failure_keeps_entire_original_report(self):
        parsed = parse_interview_workbook("访谈记录.xlsx", _workbook_bytes())
        first_module = (
            "## 战斗反馈\n\n### 模块判断\n\n玩家需要理解失败原因。\n\n"
            "### 主要发现\n\n#### 发现1：反馈不足\n\n"
            "- P01：希望失败后能立刻知道原因。[来源：记录者A!B2]\n\n"
            "### 产品建议\n\n- 增加提示。"
        )
        second_module = first_module.replace("战斗反馈", "组队沟通")
        repaired_first = first_module.replace("增加提示。", "增加明确的失败原因提示。")
        invalid_second = second_module.replace("记录者A!B2", "不存在!Z99")
        issues = [
            {"module_title": "战斗反馈", "problem": "原因不足", "suggestion": "补充原因"},
            {"module_title": "组队沟通", "problem": "建议宽泛", "suggestion": "收窄建议"},
        ]
        original_reports = [
            {"title": "战斗反馈", "report_md": first_module},
            {"title": "组队沟通", "report_md": second_module},
        ]
        original_report_md = f"# 访谈报告\n\n{first_module}\n\n{second_module}"
        sess = {
            "session_id": "a2345678-1234-1234-1234-123456789abc",
            "kind": "interview",
            "mode": "interview",
            "filename": "访谈记录.xlsx",
            "interview_status": "completed",
            "interview_workbook": parsed,
            "interview_source_text": serialize_interview_workbook(parsed),
            "interview_extraction": {
                "players": [{"player_id": "P01"}],
                "modules": [
                    {"title": title, "evidence": [{
                        "player_id": "P01",
                        "source_refs": ["记录者A!B2"],
                    }]}
                    for title in ("战斗反馈", "组队沟通")
                ],
            },
            "interview_module_reports": original_reports,
            "interview_audit": {"status": "warning", "issues": issues},
            "report_md": original_report_md,
        }
        selections = [
            {"issue_index": index, **issue}
            for index, issue in enumerate(issues)
        ]

        with (
            patch.object(interview_service, "get_session", return_value=sess),
            patch.object(interview_service, "_visible_to_owner", return_value=True),
            patch.object(interview_service, "save_session") as save_session,
            patch.object(interview_service, "save_to_history") as save_history,
            patch.object(
                interview_service,
                "collect_chat_completion",
                new=AsyncMock(side_effect=[
                    (repaired_first, "gpt-5.6-sol"),
                    (invalid_second, "gpt-5.6-sol"),
                ]),
            ),
            patch.object(interview_service.logger, "exception"),
        ):
            chunks = [
                chunk
                async for chunk in interview_service.revise_interview_audit_issues_stream(
                    sess["session_id"], selections, _Request(), None
                )
            ]

        self.assertIn('"report_unchanged": true', "".join(chunks))
        self.assertEqual(sess["report_md"], original_report_md)
        self.assertEqual(sess["interview_module_reports"], original_reports)
        save_session.assert_not_called()
        save_history.assert_not_called()


class InterviewHistoryTests(unittest.TestCase):
    def test_history_entry_preserves_interview_metadata(self):
        session = {
            "mode": "interview",
            "filename": "访谈记录.xlsx",
            "report_md": "# 访谈洞察报告\n\n## 战斗反馈",
            "interview_workbook": {
                "sheets": [{"name": "记录者A"}, {"name": "记录者B"}],
            },
            "interview_player_count": 3,
            "interview_module_count": 2,
            "interview_research_focus": "重点关注失败反馈",
            "interview_models_used": {
                "extract": "gpt-5.6-terra",
                "report": ["gpt-5.6-sol"],
            },
            "interview_audit": {"ok": True, "issues": []},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = Path(tmpdir) / "history.json"
            with patch.object(history_storage, "HISTORY_FILE", str(history_path)):
                entry = report_history.save_to_history("history-interview-1", session)
            saved = json.loads(history_path.read_text(encoding="utf-8"))

        self.assertEqual(entry["mode"], "interview")
        self.assertEqual(entry["title"], "访谈洞察报告")
        self.assertEqual(entry["interview_sheet_count"], 2)
        self.assertEqual(entry["interview_player_count"], 3)
        self.assertEqual(entry["interview_module_count"], 2)
        self.assertEqual(entry["interview_research_focus"], "重点关注失败反馈")
        self.assertEqual(entry["interview_models_used"]["report"], ["gpt-5.6-sol"])
        self.assertTrue(entry["interview_audit"]["ok"])
        self.assertEqual(saved[0]["id"], "history-interview-1")

    def test_confirm_review_issue_persists_history_and_live_session(self):
        entry = {
            "id": "history-interview-2",
            "mode": "interview",
            "report_no": "R-007",
            "interview_audit": {
                "status": "warning",
                "issues": [{
                    "module_title": "战斗反馈",
                    "problem": "需求逻辑不够清楚",
                    "suggestion": "补充原因",
                }],
            },
        }
        session = {"mode": "interview", "interview_audit": entry["interview_audit"]}
        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = Path(tmpdir) / "history.json"
            with patch.object(history_storage, "HISTORY_FILE", str(history_path)):
                history_storage._save_history([entry])
                with (
                    patch.object(
                        report_history,
                        "_find_history_for_login",
                        side_effect=lambda entries, _hist_id, _login: entries[0],
                    ),
                    patch.object(report_history, "get_session", return_value=session),
                    patch.object(report_history, "_visible_to_owner", return_value=True),
                    patch.object(report_history, "save_session") as save_session,
                ):
                    result = report_history.confirm_interview_audit_issue(
                        "history-interview-2",
                        0,
                        {"email": "reviewer@example.com"},
                    )
                saved_history = history_storage._load_history()

        issue = result["interview_audit"]["issues"][0]
        self.assertEqual(issue["review_status"], "confirmed")
        self.assertEqual(issue["reviewed_by"], "reviewer@example.com")
        self.assertTrue(issue["reviewed_at"])
        self.assertEqual(
            session["interview_audit"]["issues"][0]["review_status"],
            "confirmed",
        )
        self.assertEqual(saved_history[0]["id"], "history-interview-2")
        save_session.assert_called_once_with("history-interview-2", session)


if __name__ == "__main__":
    unittest.main()
