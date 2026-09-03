import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app.routers import annotate as annotate_router
from app.routers import comment_analysis as comment_router
from app.routers import export as export_router
from app.routers import survey as survey_router
from app.services import annotate_workflow, interview_service, session_access, survey_service
from app.storage import sessions as session_storage


LOGIN_A = {"email": "owner-a@example.com", "open_id": "open-a"}
LOGIN_B = {"email": "owner-b@example.com", "open_id": "open-b"}
OWNER_A = "email:owner-a@example.com"
OWNER_B = "email:owner-b@example.com"


def _denied() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=session_access.SESSION_ACCESS_NOT_FOUND_DETAIL,
    )


class SessionAccessPolicyTests(unittest.TestCase):
    def test_login_required_distinguishes_authentication_from_ownership(self):
        loader = Mock(return_value={"owner_key": OWNER_A})
        with patch.object(session_access.config, "FEISHU_LOGIN_REQUIRED", True):
            with self.assertRaises(HTTPException) as unauthenticated:
                session_access.require_session_access("sid", None, loader=loader)
            self.assertEqual(unauthenticated.exception.status_code, 401)
            loader.assert_not_called()

            self.assertEqual(
                session_access.require_session_access("sid", LOGIN_A, loader=loader)["owner_key"],
                OWNER_A,
            )

            with self.assertRaises(HTTPException) as cross_owner:
                session_access.require_session_access("sid", LOGIN_B, loader=loader)
            self.assertEqual(cross_owner.exception.status_code, 404)
            self.assertEqual(
                cross_owner.exception.detail,
                session_access.SESSION_ACCESS_NOT_FOUND_DETAIL,
            )

    def test_missing_cross_owner_and_ownerless_share_one_non_leaking_404(self):
        cases = {
            "missing": Mock(side_effect=HTTPException(status_code=404, detail="原始缺失提示")),
            "cross-owner": Mock(return_value={
                "owner_key": OWNER_B,
                "filename": "secret.xlsx",
                "report_md": "secret report",
                "status": "running",
            }),
            "ownerless": Mock(return_value={
                "filename": "legacy-secret.xlsx",
                "report_md": "legacy report",
                "status": "complete",
            }),
        }
        details = []
        with patch.object(session_access.config, "FEISHU_LOGIN_REQUIRED", True):
            for name, loader in cases.items():
                with self.subTest(name=name), self.assertRaises(HTTPException) as caught:
                    session_access.require_session_access("sid", LOGIN_A, loader=loader)
                self.assertEqual(caught.exception.status_code, 404)
                details.append(caught.exception.detail)
        self.assertEqual(details, [session_access.SESSION_ACCESS_NOT_FOUND_DETAIL] * 3)
        self.assertNotIn("secret", " ".join(details).lower())
        self.assertNotIn("running", " ".join(details).lower())

    def test_legacy_owner_fields_are_accepted_but_ownerless_is_not_claimed(self):
        legacy_owned = {"owner_email": "OWNER-A@example.com"}
        ownerless = {}
        with patch.object(session_access.config, "FEISHU_LOGIN_REQUIRED", True):
            self.assertIs(
                session_access.require_session_access(
                    "sid", LOGIN_A, loader=lambda _sid: legacy_owned,
                ),
                legacy_owned,
            )
            with self.assertRaises(HTTPException):
                session_access.require_session_access(
                    "sid", LOGIN_A, loader=lambda _sid: ownerless,
                )
        self.assertNotIn("owner_key", ownerless)

    def test_login_disabled_preserves_open_session_behavior_and_loader_errors(self):
        cross_owner = {"owner_key": OWNER_B}
        missing = HTTPException(status_code=404, detail="原有本地模式提示")
        with patch.object(session_access.config, "FEISHU_LOGIN_REQUIRED", False):
            self.assertIs(
                session_access.require_session_access(
                    "sid", None, loader=lambda _sid: cross_owner,
                ),
                cross_owner,
            )
            with self.assertRaises(HTTPException) as caught:
                session_access.require_session_access(
                    "sid",
                    None,
                    loader=Mock(side_effect=missing),
                )
        self.assertIs(caught.exception, missing)

    def test_annotate_denial_does_not_refresh_session_lifetime(self):
        sid = "annotate-owner-isolation"
        sess = {"owner_key": OWNER_A, "ts": 123.0, "filename": "secret.xlsx"}
        annotate_workflow.annotate_sessions[sid] = sess
        self.addCleanup(annotate_workflow.annotate_sessions.pop, sid, None)

        with patch.object(session_access.config, "FEISHU_LOGIN_REQUIRED", True):
            with self.assertRaises(HTTPException):
                session_access.require_session_access(
                    sid,
                    LOGIN_B,
                    loader=annotate_workflow.peek_annotate_session,
                )
        self.assertEqual(sess["ts"], 123.0)

    def test_distinct_sessions_and_per_session_locks_remain_independent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.object(session_storage, "_SESSION_DIR", Path(tmp_dir)):
                sid_a1 = session_storage.new_session()
                sid_a2 = session_storage.new_session()
                sid_b = session_storage.new_session()
                session_storage.save_session(sid_a1, {"owner_key": OWNER_A, "value": "a1"})
                session_storage.save_session(sid_a2, {"owner_key": OWNER_A, "value": "a2"})
                session_storage.save_session(sid_b, {"owner_key": OWNER_B, "value": "b"})

                with patch.object(session_access.config, "FEISHU_LOGIN_REQUIRED", True):
                    first = session_access.require_session_access(sid_a1, LOGIN_A)
                    second = session_access.require_session_access(sid_a2, LOGIN_A)
                    other = session_access.require_session_access(sid_b, LOGIN_B)
                    with self.assertRaises(HTTPException):
                        session_access.require_session_access(sid_b, LOGIN_A)

                first["value"] = "a1-updated"
                session_storage.save_session(sid_a1, first)
                self.assertEqual(session_storage.get_session(sid_a2)["value"], "a2")
                self.assertEqual(session_storage.get_session(sid_b)["value"], other["value"])
                self.assertEqual(second["value"], "a2")

        lock_a1 = survey_service._report_generation_lock(sid_a1)
        lock_a2 = survey_service._report_generation_lock(sid_a2)
        self.assertIsNot(lock_a1, lock_a2)

    def test_interview_authorizes_before_disclosing_task_type(self):
        with (
            patch.object(session_access.config, "FEISHU_LOGIN_REQUIRED", True),
            patch.object(
                interview_service,
                "get_session",
                return_value={"kind": "survey", "owner_key": OWNER_B},
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                interview_service.validate_interview_session("sid", LOGIN_A)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.detail, session_access.SESSION_ACCESS_NOT_FOUND_DETAIL)

    def test_ownerless_context_cannot_be_silently_claimed_when_login_is_required(self):
        ownerless = {"rows": [["q"], ["answer"]]}
        ctx = SimpleNamespace(model_dump=lambda **_kwargs: {"problem": "test"})
        with (
            patch.object(session_access.config, "FEISHU_LOGIN_REQUIRED", True),
            patch.object(survey_service, "get_session", return_value=ownerless),
            patch.object(survey_service, "_assign_session_owner") as assign_owner,
            patch.object(survey_service, "save_session") as save_session,
        ):
            with self.assertRaises(HTTPException):
                survey_service.save_qualitative_context("sid", ctx, LOGIN_A)
        assign_owner.assert_not_called()
        save_session.assert_not_called()
        self.assertNotIn("owner_key", ownerless)


class SessionRouteBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_and_qa_recheck_owner_before_writer_or_session_write(self):
        for stream_factory, llm_name in (
            (
                lambda: survey_service.report_stream("sid", object()),
                "_direct_writer_round",
            ),
            (
                lambda: survey_service.qa_stream("sid", "question", object()),
                "_answer_qa_direct",
            ),
        ):
            with (
                self.subTest(stream=llm_name),
                patch.object(session_access.config, "FEISHU_LOGIN_REQUIRED", True),
                patch.object(
                    survey_service,
                    "_current_login",
                    new=AsyncMock(return_value=LOGIN_B),
                ),
                patch.object(
                    survey_service,
                    "get_session",
                    return_value={"owner_key": OWNER_A, "report_md": "secret"},
                ),
                patch.object(survey_service, llm_name, new=AsyncMock()) as llm_call,
                patch.object(survey_service, "save_session") as save_session,
                self.assertRaises(HTTPException) as caught,
            ):
                _ = [event async for event in stream_factory()]
            self.assertEqual(caught.exception.status_code, 404)
            llm_call.assert_not_awaited()
            save_session.assert_not_called()

    async def test_owner_can_reach_a_protected_report_version_read(self):
        payload = {"versions": [], "active_version": None}
        with (
            patch.object(session_access.config, "FEISHU_LOGIN_REQUIRED", True),
            patch.object(session_access, "get_session", return_value={"owner_key": OWNER_A}),
            patch.object(
                survey_router,
                "_current_login",
                new=AsyncMock(return_value=LOGIN_A),
            ),
            patch.object(
                survey_router,
                "get_session_report_versions",
                return_value=payload,
            ) as read_versions,
        ):
            result = await survey_router.list_report_versions("sid", object())
        self.assertIs(result, payload)
        read_versions.assert_called_once_with("sid")

    async def test_request_guard_is_a_true_noop_when_login_is_disabled(self):
        login_resolver = AsyncMock(return_value=LOGIN_A)
        session_loader = Mock(return_value={"owner_key": OWNER_A})
        with patch.object(session_access.config, "FEISHU_LOGIN_REQUIRED", False):
            login = await session_access.require_session_request_access(
                object(),
                "sid",
                login_resolver=login_resolver,
                loader=session_loader,
            )
        self.assertIsNone(login)
        login_resolver.assert_not_awaited()
        session_loader.assert_not_called()

    async def test_request_guard_loads_and_authorizes_when_login_is_required(self):
        login_resolver = AsyncMock(return_value=LOGIN_A)
        session_loader = Mock(return_value={"owner_key": OWNER_A})
        with patch.object(session_access.config, "FEISHU_LOGIN_REQUIRED", True):
            login = await session_access.require_session_request_access(
                object(),
                "sid",
                login_resolver=login_resolver,
                loader=session_loader,
            )
        self.assertIs(login, LOGIN_A)
        login_resolver.assert_awaited_once()
        session_loader.assert_called_once_with("sid")

    async def _assert_blocked_before_downstream(
        self,
        module,
        invoke,
        downstream_names: list[str],
    ) -> None:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(module, "_current_login", new=AsyncMock(return_value=LOGIN_B))
            )
            direct_guard = stack.enter_context(
                patch.object(module, "require_session_access", side_effect=_denied())
            ) if hasattr(module, "require_session_access") else None
            request_guard = stack.enter_context(
                patch.object(
                    module,
                    "require_session_request_access",
                    new=AsyncMock(side_effect=_denied()),
                )
            ) if hasattr(module, "require_session_request_access") else None
            downstream = {
                name: stack.enter_context(patch.object(module, name))
                for name in downstream_names
            }
            with self.assertRaises(HTTPException) as caught:
                await invoke()

        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.detail, session_access.SESSION_ACCESS_NOT_FOUND_DETAIL)
        guard_calls = sum(
            guard.call_count
            for guard in (direct_guard, request_guard)
            if guard is not None
        )
        self.assertEqual(guard_calls, 1)
        for name, mocked in downstream.items():
            with self.subTest(downstream=name):
                mocked.assert_not_called()

    async def test_all_survey_session_routes_block_before_read_write_or_sse(self):
        request = object()
        cases = [
            (
                "columns",
                lambda: survey_router.get_columns("sid", request),
                ["validate_columns_ready", "columns_require_llm", "columns_stream"],
            ),
            (
                "confirm-columns",
                lambda: survey_router.confirm_columns(
                    "sid", SimpleNamespace(columns=[]), request,
                ),
                ["set_survey_columns", "get_analysis_preset_offer_for_session", "audit_log"],
            ),
            (
                "apply-preset",
                lambda: survey_router.apply_analysis_preset_route(
                    "sid", SimpleNamespace(preset_id="preset"), request,
                ),
                ["apply_analysis_preset_to_session", "audit_log"],
            ),
            (
                "survey-context",
                lambda: survey_router.submit_survey_context(
                    "sid", SimpleNamespace(), request,
                ),
                ["save_qualitative_context", "audit_log"],
            ),
            (
                "plan",
                lambda: survey_router.get_plan("sid", request),
                ["validate_plan_ready", "plan_stream"],
            ),
            (
                "plan-confirm",
                lambda: survey_router.confirm_plan(
                    SimpleNamespace(session_id="sid", user_text="确认"), request,
                ),
                [
                    "validate_plan_confirm_ready",
                    "is_survey_plan_approval",
                    "confirm_survey_plan",
                    "plan_revision_stream",
                    "audit_log",
                ],
            ),
            (
                "stats",
                lambda: survey_router.compute_stats("sid", request),
                ["compute_survey_stats"],
            ),
            (
                "prepare-rerun",
                lambda: survey_router.prepare_report_rerun(
                    "sid", SimpleNamespace(), request,
                ),
                ["prepare_duplicate_report_rerun", "audit_log"],
            ),
            (
                "report-generate",
                lambda: survey_router.generate_report("sid", request),
                ["validate_report_ready", "report_stream"],
            ),
            (
                "report-read-version",
                lambda: survey_router.generate_report("sid", request, version=1),
                ["get_session_report_version"],
            ),
            (
                "report-list-versions",
                lambda: survey_router.list_report_versions("sid", request),
                ["get_session_report_versions"],
            ),
            (
                "report-create-version-disabled",
                lambda: survey_router.generate_report_version(
                    "sid", SimpleNamespace(), request,
                ),
                [],
            ),
            (
                "report-delete-version",
                lambda: survey_router.delete_report_version_route("sid", 1, request),
                ["delete_session_report_version"],
            ),
            (
                "qa",
                lambda: survey_router.qa(
                    SimpleNamespace(session_id="sid", question="secret?", version=None),
                    request,
                ),
                ["validate_qa_ready", "qa_stream"],
            ),
        ]
        for name, invoke, downstream in cases:
            with self.subTest(route=name):
                await self._assert_blocked_before_downstream(
                    survey_router,
                    invoke,
                    downstream,
                )

    async def test_all_session_exports_block_before_render_or_external_call(self):
        request = object()
        cases = [
            (
                "word",
                lambda: export_router.export_word("sid", request),
                ["prepare_word_download", "audit_log"],
            ),
            (
                "markdown",
                lambda: export_router.export_markdown("sid", request),
                ["prepare_markdown_download", "audit_log"],
            ),
            (
                "pdf",
                lambda: export_router.export_pdf("sid", request),
                ["prepare_pdf_download", "audit_log"],
            ),
            (
                "feishu",
                lambda: export_router.export_feishu("sid", request),
                [
                    "require_feishu_configured",
                    "get_session_export_data",
                    "_export_to_feishu",
                    "audit_log",
                ],
            ),
        ]
        for name, invoke, downstream in cases:
            with self.subTest(route=name):
                await self._assert_blocked_before_downstream(
                    export_router,
                    invoke,
                    downstream,
                )

    async def test_comment_sse_routes_block_before_validation_or_pipeline(self):
        request = object()
        cases = [
            (
                lambda: comment_router.comment_analysis_preprocess("sid", request),
                ["validate_comment_session_for_preprocess", "comment_preprocess_stream"],
            ),
            (
                lambda: comment_router.comment_analysis_run("sid", request),
                ["validate_comment_session_for_run", "comment_run_stream"],
            ),
        ]
        for invoke, downstream in cases:
            await self._assert_blocked_before_downstream(
                comment_router,
                invoke,
                downstream,
            )

    async def test_annotate_routes_block_before_mutation_llm_or_download(self):
        request = object()
        cases = [
            (
                lambda: annotate_router.annotate_confirm_columns(
                    "sid", SimpleNamespace(), request,
                ),
                ["peek_annotate_session", "annotate_set_column_config", "audit_log"],
            ),
            (
                lambda: annotate_router.annotate_run_ai_detect("sid", request),
                ["peek_annotate_session", "validate_annotate_session_for_ai", "ai_detect_stream"],
            ),
            (
                lambda: annotate_router.annotate_confirm_ai(
                    "sid", SimpleNamespace(), request,
                ),
                ["peek_annotate_session", "annotate_set_confirmed_ai", "audit_log"],
            ),
            (
                lambda: annotate_router.annotate_run_quality("sid", request),
                ["peek_annotate_session", "validate_annotate_session_for_quality"],
            ),
            (
                lambda: annotate_router.annotate_download("sid", request),
                ["peek_annotate_session", "build_and_save_annotate_download", "audit_log"],
            ),
        ]
        for invoke, downstream in cases:
            await self._assert_blocked_before_downstream(
                annotate_router,
                invoke,
                downstream,
            )


if __name__ == "__main__":
    unittest.main()
