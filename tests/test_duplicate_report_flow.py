from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.routers import survey as survey_router
from app.schemas.requests import QualitativeContextRequest, ReportVersionRequest
from app.services import report_history, report_versions, survey_service
from app.storage import history as history_storage
from app.storage import sessions as session_storage


LOGIN = {"email": "owner@example.com", "name": "Owner"}
OWNER = {
    "owner_key": "email:owner@example.com",
    "owner_email": "owner@example.com",
    "owner_open_id": "",
    "owner_name": "Owner",
}
FILE_BYTES = b"same raw survey bytes"
FILE_SHA256 = hashlib.sha256(FILE_BYTES).hexdigest()
CONTEXT = {
    "problem": "核心  问题",
    "key_concerns": "关键原因",
    "target_users": "活跃玩家",
    "analysis_approach": "按体验链路展开",
}
COLUMNS = [
    {
        "index": 0,
        "name": "反馈",
        "role": "open_text",
        "column_indexes": [0],
    }
]
PLAN = {
    "columns": [],
    "parts": [],
    "cross_tabs": [],
    "open_questions": [],
    "branch_rules": [],
}


def _snapshot(title: str, *, qa_messages: list[dict] | None = None) -> dict:
    return {
        "report_md": f"# {title}\n\n{title}正文",
        "title": title,
        "qa_context_md": f"<qa_context><report>{title}</report></qa_context>",
        "qa_messages": deepcopy(qa_messages or []),
        "qa_provider": "",
        "qa_model": "",
        "report_writer_provider": "direct_llm",
        "report_writer_model": "writer-model",
        "analyst_conv_id": "",
        "analyst_app": "standard",
    }


def _fingerprint_session(*, rows: list | None = None, include_plan: bool = False) -> dict:
    sess = {
        "filename": "responses.xlsx",
        "rows": rows or [["反馈"], ["old answer"]],
        "source_type": "google",
        "file_sha256": FILE_SHA256,
        "questionnaire_sha256": "",
        "questionnaire_used": False,
        "confirmed_columns": deepcopy(COLUMNS),
        "qualitative_context": deepcopy(CONTEXT),
        "branch_rules": [],
        "open_text": {},
        **OWNER,
    }
    if include_plan:
        sess["plan"] = deepcopy(PLAN)
        sess["stats_md"] = "有效样本(总计):总体=1"
    return sess


def _event_payloads(events: list[str]) -> list[dict]:
    payloads = []
    for event in events:
        if event.startswith("data: "):
            payloads.append(json.loads(event.removeprefix("data: ").strip()))
    return payloads


class TemporaryDuplicateRuntimeMixin:
    """Redirect every persistent write and guard the real history file."""

    def setUp(self):
        super().setUp()
        real_history = Path(history_storage.HISTORY_FILE)
        self._real_history_path = real_history
        self._real_history_before = (
            hashlib.sha256(real_history.read_bytes()).hexdigest()
            if real_history.exists()
            else None
        )
        self._temp = tempfile.TemporaryDirectory(prefix="duplicate-report-flow-")
        root = Path(self._temp.name)
        self.history_file = root / "history.json"
        self.session_dir = root / "sessions"
        self._history_patch = patch.object(
            history_storage,
            "HISTORY_FILE",
            str(self.history_file),
        )
        self._session_patch = patch.object(
            session_storage,
            "_SESSION_DIR",
            self.session_dir,
        )
        self._history_patch.start()
        self._session_patch.start()
        survey_service._REPORT_GENERATION_LOCKS.clear()
        survey_service._REPORT_RERUN_TARGET_LOCKS.clear()

    def tearDown(self):
        survey_service._REPORT_GENERATION_LOCKS.clear()
        survey_service._REPORT_RERUN_TARGET_LOCKS.clear()
        self._session_patch.stop()
        self._history_patch.stop()
        self._temp.cleanup()
        real_after = (
            hashlib.sha256(self._real_history_path.read_bytes()).hexdigest()
            if self._real_history_path.exists()
            else None
        )
        self.assertEqual(real_after, self._real_history_before)
        super().tearDown()

    def _new_session(self, sess: dict) -> str:
        session_id = session_storage.new_session()
        session_storage.save_session(session_id, sess)
        return session_id

    def _archive_v1(self, *, qa_messages: list[dict] | None = None) -> tuple[str, dict]:
        history_id = str(uuid.uuid4())
        sess = _fingerprint_session(include_plan=True)
        report_versions.append_report_version(
            sess,
            _snapshot("原报告", qa_messages=qa_messages),
            kind="initial",
            created_at="2026-08-01T10:00:00",
        )
        saved = report_history.save_to_history(history_id, sess)
        return history_id, saved


class DuplicateUploadAndMatchTests(
    TemporaryDuplicateRuntimeMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def test_upload_persists_raw_and_paired_questionnaire_sha256(self):
        with patch.object(
            survey_service,
            "_parse_file",
            return_value=[["反馈"], ["回答"]],
        ):
            google = await survey_service.handle_survey_upload(
                "responses.csv",
                FILE_BYTES,
                LOGIN,
            )
        google_session = session_storage.get_session(google["session_id"])
        self.assertEqual(google_session["file_sha256"], FILE_SHA256)
        self.assertEqual(google_session["questionnaire_sha256"], "")
        self.assertFalse(google_session["questionnaire_used"])

        questionnaire_bytes = b"questionnaire workbook bytes"
        imported = {
            "rows": [["反馈"], ["回答"]],
            "questions": deepcopy(COLUMNS),
            "questionnaire_text": "Q1 反馈",
            "matched_questions": 1,
        }
        with patch.object(
            survey_service,
            "parse_bested_qualitative_upload",
            return_value=imported,
        ):
            bested = await survey_service.handle_survey_upload(
                "result.xlsx",
                FILE_BYTES,
                LOGIN,
                source_type="bested",
                questionnaire_filename="questionnaire.xlsx",
                questionnaire_content=questionnaire_bytes,
            )
        bested_session = session_storage.get_session(bested["session_id"])
        self.assertEqual(bested_session["file_sha256"], FILE_SHA256)
        self.assertEqual(
            bested_session["questionnaire_sha256"],
            hashlib.sha256(questionnaire_bytes).hexdigest(),
        )
        self.assertTrue(bested_session["questionnaire_used"])

    async def test_context_match_uses_owner_file_and_normalized_background(self):
        history_id, _ = self._archive_v1()
        fresh = _fingerprint_session()
        fresh.pop("qualitative_context")
        fresh["confirmed_columns"] = [
            {
                "role": "single_choice",
                "column_indexes": [0],
                "name_zh": "AI 本次生成的另一种题目简称",
                "index": 0,
            }
        ]
        session_id = self._new_session(fresh)
        submitted = QualitativeContextRequest(
            problem="  核心\n问题 ",
            key_concerns="关键、原因！",
            target_users="活跃玩家",
            analysis_approach="按体验链路展开",
        )

        duplicate = survey_service.save_qualitative_context(
            session_id,
            submitted,
            LOGIN,
        )

        self.assertEqual(duplicate["id"], history_id)
        self.assertEqual(duplicate["history_id"], history_id)
        self.assertEqual(duplicate["version_count"], 1)
        self.assertEqual(duplicate["active_version"], 1)

        changed = session_storage.get_session(session_id)
        changed["file_sha256"] = hashlib.sha256(b"different").hexdigest()
        session_storage.save_session(session_id, changed)
        self.assertIsNone(
            report_history.find_exact_survey_duplicate_report(changed, LOGIN)
        )

    async def test_context_similarity_threshold_is_inclusive_at_80_percent(self):
        history_id = str(uuid.uuid4())
        archived = _fingerprint_session(include_plan=True)
        archived["qualitative_context"] = {
            "problem": "abcdefghij",
            "key_concerns": "",
            "target_users": "",
            "analysis_approach": "",
        }
        report_versions.append_report_version(
            archived,
            _snapshot("相似度阈值原报告"),
            kind="initial",
        )
        report_history.save_to_history(history_id, archived)

        at_threshold = _fingerprint_session()
        at_threshold["qualitative_context"] = {
            **archived["qualitative_context"],
            "problem": "abcdefghXY",
        }
        at_threshold["confirmed_columns"][0].update({
            "role": "single_choice",
            "name_zh": "不同的 AI 题目简称",
        })
        self.assertEqual(
            report_history._duplicate_context_similarity(
                archived["qualitative_context"],
                at_threshold["qualitative_context"],
            ),
            0.8,
        )
        self.assertEqual(
            report_history.find_exact_survey_duplicate_report(
                at_threshold,
                LOGIN,
            )["id"],
            history_id,
        )

        below_threshold = deepcopy(at_threshold)
        below_threshold["qualitative_context"]["problem"] = "abcdefgXYZ"
        self.assertLess(
            report_history._duplicate_context_similarity(
                archived["qualitative_context"],
                below_threshold["qualitative_context"],
            ),
            0.8,
        )
        self.assertIsNone(
            report_history.find_exact_survey_duplicate_report(
                below_threshold,
                LOGIN,
            )
        )

    async def test_legacy_history_without_fingerprint_fields_never_matches(self):
        _, saved = self._archive_v1()
        legacy = deepcopy(saved)
        legacy.pop("file_sha256", None)
        history_storage._save_history([legacy])
        fresh = _fingerprint_session()
        self.assertIsNone(
            report_history.find_exact_survey_duplicate_report(fresh, LOGIN)
        )

    async def test_each_exact_match_dimension_rejects_independently(self):
        self._archive_v1()
        mutations = {
            "owner": lambda sess: sess.update({
                "owner_key": "email:other@example.com",
                "owner_email": "other@example.com",
            }),
            "source_type": lambda sess: sess.update({"source_type": "bested"}),
            "file_sha256": lambda sess: sess.update(
                {"file_sha256": hashlib.sha256(b"different").hexdigest()}
            ),
            "context_similarity": lambda sess: sess.update({
                "qualitative_context": {
                    "problem": "unrelated business problem",
                    "key_concerns": "different research concern",
                    "target_users": "another audience",
                    "analysis_approach": "unrelated analysis structure",
                }
            }),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = _fingerprint_session()
                mutate(changed)
                login = (
                    {"email": "other@example.com"}
                    if name == "owner"
                    else LOGIN
                )
                self.assertIsNone(
                    report_history.find_exact_survey_duplicate_report(changed, login)
                )

        questionnaire_hash = hashlib.sha256(b"original questionnaire").hexdigest()
        bested = _fingerprint_session(include_plan=True)
        bested.update({
            "source_type": "bested",
            "questionnaire_used": True,
            "questionnaire_sha256": questionnaire_hash,
        })
        report_versions.append_report_version(
            bested,
            _snapshot("倍市得原报告"),
            kind="initial",
        )
        bested_id = str(uuid.uuid4())
        report_history.save_to_history(bested_id, bested)
        changed_questionnaire = deepcopy(bested)
        changed_questionnaire.pop("report_versions")
        for field in (
            "report_md",
            "title",
            "qa_context_md",
            "qa_messages",
            "active_report_version",
            "next_report_version",
        ):
            changed_questionnaire.pop(field, None)
        changed_questionnaire["questionnaire_sha256"] = hashlib.sha256(
            b"different questionnaire"
        ).hexdigest()
        self.assertIsNone(
            report_history.find_exact_survey_duplicate_report(
                changed_questionnaire,
                LOGIN,
            )
        )


class DuplicatePrepareAndGenerationTests(
    TemporaryDuplicateRuntimeMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def test_prepare_revalidates_and_copies_plan_with_default_note(self):
        history_id, _ = self._archive_v1()
        session_id = self._new_session(_fingerprint_session(rows=[["反馈"], ["new answer"]]))

        prepared = survey_service.prepare_duplicate_report_rerun(
            session_id,
            LOGIN,
            history_id=history_id,
            instruction="   ",
        )

        self.assertTrue(prepared["skip_plan"])
        self.assertEqual(prepared["history_id"], history_id)
        self.assertEqual(prepared["base_version"], 1)
        self.assertEqual(prepared["target_version"], 2)
        self.assertEqual(
            prepared["instruction"],
            report_history.DEFAULT_RERUN_VERSION_INSTRUCTION,
        )
        stored = session_storage.get_session(session_id)
        self.assertEqual(stored["plan"], PLAN)
        self.assertEqual(stored["rerun_target_history_id"], history_id)
        self.assertEqual(stored["rerun_supplement"], "")

        stored["file_sha256"] = hashlib.sha256(b"changed after prepare").hexdigest()
        session_storage.save_session(session_id, stored)
        with self.assertRaises(HTTPException) as caught:
            survey_service.prepare_duplicate_report_rerun(
                session_id,
                LOGIN,
                history_id=history_id,
            )
        self.assertEqual(caught.exception.status_code, 409)

    async def test_prepare_rejects_forged_id_cross_owner_and_active_target(self):
        history_id, _ = self._archive_v1()
        valid_session_id = self._new_session(_fingerprint_session())
        with self.assertRaises(HTTPException) as forged:
            survey_service.prepare_duplicate_report_rerun(
                valid_session_id,
                LOGIN,
                history_id=str(uuid.uuid4()),
            )
        self.assertEqual(forged.exception.status_code, 409)

        other = _fingerprint_session()
        other.update({
            "owner_key": "email:other@example.com",
            "owner_email": "other@example.com",
        })
        other_session_id = self._new_session(other)
        with self.assertRaises(HTTPException) as cross_owner:
            survey_service.prepare_duplicate_report_rerun(
                other_session_id,
                {"email": "other@example.com"},
                history_id=history_id,
            )
        self.assertEqual(cross_owner.exception.status_code, 409)

        target_lock = survey_service._report_rerun_target_lock(history_id)
        await target_lock.acquire()
        try:
            with self.assertRaises(HTTPException) as active:
                survey_service.prepare_duplicate_report_rerun(
                    valid_session_id,
                    LOGIN,
                    history_id=history_id,
                )
            self.assertEqual(active.exception.status_code, 409)
        finally:
            target_lock.release()

    async def test_get_report_rerun_uses_new_session_and_atomically_appends_same_card(self):
        history_id, _ = self._archive_v1(
            qa_messages=[{"role": "user", "content": "生成前问题"}],
        )
        fresh = _fingerprint_session(rows=[["反馈"], ["new answer"]])
        session_id = self._new_session(fresh)
        survey_service.prepare_duplicate_report_rerun(
            session_id,
            LOGIN,
            history_id=history_id,
            instruction="",
        )
        ready = session_storage.get_session(session_id)
        ready["stats_md"] = "有效样本(总计):总体=1"
        ready["stats_blocks"] = []
        ready["open_text"] = {}
        session_storage.save_session(session_id, ready)

        writer_results = [
            ("# 新数据重跑报告", "writer-model"),
            ("NONE", "writer-model"),
            (
                "<!--CORE_START-->\n## 核心结论\n新数据结论。\n<!--CORE_END-->",
                "writer-model",
            ),
            ("## 行动建议\n\n**继续验证**", "writer-model"),
        ]
        writer_calls = 0

        async def writer_side_effect(_messages, _query):
            nonlocal writer_calls
            if writer_calls == 0:
                def add_history_qa(history: list) -> None:
                    entry = next(item for item in history if item["id"] == history_id)
                    report_versions.update_report_version(
                        entry,
                        1,
                        qa_messages=[
                            {"role": "user", "content": "生成期间新增问题"},
                            {"role": "ai", "content": "生成期间新增回答"},
                        ],
                    )

                history_storage.mutate_history(add_history_qa)
            result = writer_results[writer_calls]
            writer_calls += 1
            return result

        qa_sources: list[dict] = []

        def build_qa_context(source: dict, report: str = "") -> str:
            qa_sources.append(deepcopy(source))
            return f"<qa_context><rows>{source['rows'][1][0]}</rows><report>{report}</report></qa_context>"

        with (
            patch.object(
                survey_service,
                "_current_login",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                survey_service,
                "_get_report_writer_system_prompt",
                return_value="writer system",
            ),
            patch.object(
                survey_service,
                "_direct_writer_round",
                new=AsyncMock(side_effect=writer_side_effect),
            ),
            patch.object(
                survey_service,
                "normalize_glossary_terms",
                side_effect=lambda text: text,
            ),
            patch.object(
                survey_service,
                "inject_qualitative_stats",
                side_effect=lambda report, _stats, _plan: report,
            ),
            patch.object(
                survey_service,
                "_inject_disclaimer",
                side_effect=lambda report, mode="": report,
            ),
            patch.object(
                survey_service,
                "_inject_research_background",
                side_effect=lambda report, _context: report,
            ),
            patch.object(
                survey_service,
                "_build_qa_context",
                side_effect=build_qa_context,
            ),
            patch.object(
                survey_service.survey_stats,
                "find_numbers_not_in_stats",
                return_value=[],
            ),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch("traceback.print_exc"),
        ):
            events = [
                event
                async for event in survey_service.report_stream(
                    session_id,
                    object(),
                    generation_kind="initial",
                )
            ]

        payloads = _event_payloads(events)
        done = next(item for item in payloads if item.get("type") == "report_done")
        self.assertEqual(done["history_id"], history_id)
        self.assertEqual(done["version"], 2)
        self.assertFalse(done["can_generate_version"])
        self.assertEqual(qa_sources[-1]["rows"][1][0], "new answer")

        stored_history = history_storage._load_history()
        self.assertEqual(len(stored_history), 1)
        self.assertEqual(stored_history[0]["id"], history_id)
        self.assertEqual(
            [item["version"] for item in stored_history[0]["report_versions"]],
            [1, 2],
        )
        first = report_versions.resolve_report_version(stored_history[0], 1)
        second = report_versions.resolve_report_version(stored_history[0], 2)
        self.assertEqual(first["qa_messages"][-1]["content"], "生成期间新增回答")
        self.assertEqual(second["base_version"], 1)
        self.assertEqual(
            second["instruction"],
            report_history.DEFAULT_RERUN_VERSION_INSTRUCTION,
        )
        self.assertIn("new answer", second["qa_context_md"])

        rerun_session = session_storage.get_session(session_id)
        self.assertEqual(rerun_session["active_report_version"], 2)
        self.assertEqual(len(rerun_session["report_versions"]), 2)

    async def test_history_qa_remains_available_while_target_rerun_lock_is_held(self):
        history_id, _ = self._archive_v1()
        session_id = self._new_session(_fingerprint_session())
        survey_service.prepare_duplicate_report_rerun(
            session_id,
            LOGIN,
            history_id=history_id,
        )
        ready = session_storage.get_session(session_id)
        ready["stats_md"] = "有效样本(总计):总体=1"
        session_storage.save_session(session_id, ready)
        target_lock = survey_service._report_rerun_target_lock(history_id)
        await target_lock.acquire()
        try:
            with (
                patch.object(
                    survey_service,
                    "_answer_qa_direct",
                    new=AsyncMock(
                        return_value=("仍可回答", "qa-model", "<qa_context />")
                    ),
                ),
                patch.object(survey_service, "audit_log", new=AsyncMock()),
                patch("traceback.print_exc"),
            ):
                events = [
                    event
                    async for event in survey_service.history_qa_stream(
                        history_id,
                        "生成期间还能追问吗？",
                        history_storage._load_history(),
                        object(),
                        version=1,
                    )
                ]
            self.assertTrue(
                any(
                    payload.get("type") == "qa_done"
                    for payload in _event_payloads(events)
                )
            )
            with self.assertRaises(HTTPException) as caught:
                survey_service.delete_owned_history_report_version(
                    history_id,
                    1,
                    LOGIN,
            )
            self.assertEqual(caught.exception.status_code, 409)

            with (
                patch.object(
                    survey_service,
                    "_current_login",
                    new=AsyncMock(return_value=LOGIN),
                ),
                patch.object(
                    survey_service,
                    "_get_report_writer_system_prompt",
                    return_value="writer system",
                ),
                patch("traceback.print_exc"),
            ):
                blocked_events = [
                    event
                    async for event in survey_service.report_stream(
                        session_id,
                        object(),
                        generation_kind="initial",
                    )
                ]
            blocked = next(
                payload
                for payload in _event_payloads(blocked_events)
                if payload.get("type") == "error"
            )
            self.assertIn("正在重新生成", blocked["message"])
        finally:
            target_lock.release()


class DuplicateHistoryDeleteAndRouteTests(
    TemporaryDuplicateRuntimeMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def test_history_delete_is_owner_only_atomic_and_keeps_one_version(self):
        history_id, saved = self._archive_v1()
        report_versions.append_report_version(
            saved,
            _snapshot("第二版"),
            kind="regenerate",
            base_version=1,
            instruction="补充要求",
            created_at="2026-08-02T10:00:00",
        )
        history_storage._save_history([saved])

        with self.assertRaises(HTTPException) as denied:
            survey_service.delete_owned_history_report_version(
                history_id,
                1,
                {"email": "other@example.com"},
            )
        self.assertEqual(denied.exception.status_code, 404)

        result = survey_service.delete_owned_history_report_version(
            history_id,
            1,
            LOGIN,
        )
        self.assertEqual(result["id"], history_id)
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["version_count"], 1)
        self.assertFalse(result["can_generate_version"])
        self.assertEqual(len(history_storage._load_history()), 1)

        with self.assertRaises(HTTPException) as last_version:
            survey_service.delete_owned_history_report_version(
                history_id,
                2,
                LOGIN,
            )
        self.assertEqual(last_version.exception.status_code, 400)
        self.assertEqual(len(history_storage._load_history()[0]["report_versions"]), 1)

    async def test_stale_v1_session_save_merges_qa_without_erasing_history_v2(self):
        history_id, stale_session = self._archive_v1()
        current_entry = deepcopy(stale_session)
        report_versions.append_report_version(
            current_entry,
            _snapshot("第二版"),
            kind="regenerate",
            base_version=1,
            instruction="使用新数据重跑",
            created_at="2026-08-02T10:00:00",
        )
        history_storage._save_history([current_entry])

        report_versions.update_report_version(
            stale_session,
            1,
            qa_messages=[
                {"role": "user", "content": "旧会话追问"},
                {"role": "ai", "content": "旧会话回答"},
            ],
        )
        report_history.save_to_history(history_id, stale_session)

        stored = history_storage._load_history()[0]
        self.assertEqual(
            [item["version"] for item in stored["report_versions"]],
            [1, 2],
        )
        self.assertEqual(stored["active_report_version"], 2)
        self.assertEqual(
            report_versions.resolve_report_version(stored, 1)["qa_messages"][-1][
                "content"
            ],
            "旧会话回答",
        )
        self.assertEqual(
            report_versions.resolve_report_version(stored, 2)["report_md"],
            "# 第二版\n\n第二版正文",
        )

    async def test_explicit_session_delete_replaces_history_version_list(self):
        history_id, current = self._archive_v1()
        report_versions.append_report_version(
            current,
            _snapshot("第二版"),
            kind="regenerate",
            base_version=1,
            instruction="使用新数据重跑",
            created_at="2026-08-02T10:00:00",
        )
        history_storage._save_history([current])
        session_storage.save_session(history_id, deepcopy(current))

        result = survey_service.delete_session_report_version(
            history_id,
            1,
            LOGIN,
        )

        self.assertEqual(result["deleted_version"], 1)
        self.assertEqual(
            [
                item["version"]
                for item in history_storage._load_history()[0]["report_versions"]
            ],
            [2],
        )
        self.assertEqual(
            [
                item["version"]
                for item in session_storage.get_session(history_id)["report_versions"]
            ],
            [2],
        )

    async def test_stale_session_cannot_resurrect_explicitly_deleted_version(self):
        history_id, stale_session = self._archive_v1()
        report_versions.append_report_version(
            stale_session,
            _snapshot("第二版"),
            kind="regenerate",
            base_version=1,
            instruction="使用新数据重跑",
            created_at="2026-08-02T10:00:00",
        )
        history_storage._save_history([deepcopy(stale_session)])

        survey_service.delete_owned_history_report_version(
            history_id,
            1,
            LOGIN,
        )
        report_history.save_to_history(history_id, stale_session)

        stored = history_storage._load_history()[0]
        self.assertEqual(
            [item["version"] for item in stored["report_versions"]],
            [2],
        )
        self.assertEqual(stored["active_report_version"], 2)

    async def test_report_page_post_version_route_is_disabled(self):
        with self.assertRaises(HTTPException) as caught:
            await survey_router.generate_report_version(
                "session-id",
                ReportVersionRequest(instruction="旧入口"),
                object(),
            )
        self.assertEqual(caught.exception.status_code, 405)


if __name__ == "__main__":
    unittest.main()
