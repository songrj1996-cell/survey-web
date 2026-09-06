import json
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from app.services import interview_v2_report_rerun_service as service
from app.services.interview_v2_import_service import InterviewV2ImportError


PROJECT = "project_" + "1" * 32
BASE_REPORT = "report_" + "2" * 32
SECTION = "section_" + "3" * 32
LOCKED_SECTION = "section_" + "4" * 32
ANALYSIS = "analysis_" + "5" * 32
FINDING = "finding_" + "6" * 32
PARTICIPANT = "participant_" + "7" * 32
EVIDENCE = "ev_" + "8" * 32
LOGIN = {"email": "owner@example.com"}
KEY = "report-section-rerun-01"


def _saved_report(*, approved=False, target_locked=False):
    target_claim = "claim_" + "9" * 32
    locked_claim = "claim_" + "a" * 32
    revision = {
        "project_id": PROJECT,
        "report_version_id": BASE_REPORT,
        "revision_payload_sha256": "b" * 64,
        "report_schema_version": "interview-report/1.0",
        "version_number": 4,
        "source": {
            "analysis_run_id": ANALYSIS,
            "analysis_revision_payload_sha256": "c" * 64,
            "analysis_source": {},
        },
        "input_fingerprint": "d" * 64,
        "frozen_config": {"research_focus": "入口体验"},
        "frozen_findings": [{
            "finding_id": FINDING,
            "statement": "入口可理解。",
            "supporting_cases": [{
                "participant_id": PARTICIPANT,
                "evidence_ids": [EVIDENCE],
            }],
            "counterexample_cases": [],
            "observation_cases": [],
        }],
        "frozen_stat_facts": [],
        "analysis_limitations": [],
        "status": "approved" if approved else "draft",
        "audit_status": "audited",
        "sections": [
            {
                "section_id": SECTION,
                "report_version_id": BASE_REPORT,
                "section_key": "core_findings",
                "title": "核心发现",
                "order": 2,
                "section_revision": 2,
                "content": "旧正文。",
                "content_sha256": "e" * 64,
                "claim_ids": [target_claim],
                "locked": target_locked,
                "audit_status": "audit_passed",
            },
            {
                "section_id": LOCKED_SECTION,
                "report_version_id": BASE_REPORT,
                "section_key": "recommendations",
                "title": "轻量产品建议",
                "order": 6,
                "section_revision": 5,
                "content": "人工锁定建议。",
                "content_sha256": "f" * 64,
                "claim_ids": [locked_claim],
                "locked": True,
                "audit_status": "audit_passed",
                "edited_by": "email:researcher@example.com",
            },
        ],
        "claims": [
            {
                "claim_id": target_claim,
                "report_version_id": BASE_REPORT,
                "section_id": SECTION,
                "section_key": "core_findings",
                "text": "旧正文。",
                "content_sha256": "1" * 64,
                "superseded_by": None,
            },
            {
                "claim_id": locked_claim,
                "report_version_id": BASE_REPORT,
                "section_id": LOCKED_SECTION,
                "section_key": "recommendations",
                "text": "人工锁定建议。",
                "content_sha256": "2" * 64,
                "superseded_by": None,
            },
        ],
        "audit_issues": [],
        "model_usage": {"writer_model": "old-writer"},
        "created_at": "2026-09-03T00:00:00Z",
        "created_by": "email:owner@example.com",
    }
    if approved:
        revision.update({
            "approved_by": "email:owner@example.com",
            "approved_at": "2026-09-03T00:01:00Z",
            "approval_note": "已确认",
            "approved_from_report_version_id": "report_" + "0" * 32,
        })
    return {
        "project_id": PROJECT,
        "state": {"current_report_version_id": BASE_REPORT},
        "revision": revision,
    }


def _request():
    return {
        "from_stage": "report_section",
        "base_report_version_id": BASE_REPORT,
        "section_id": SECTION,
        "base_section_revision": 2,
        "instruction": "保持简洁",
        "preserve_manual_report_edits": True,
        "reuse_unchanged_artifacts": True,
        "force": False,
    }


def _writer_output():
    content = "入口可理解。"
    return json.dumps({
        "section_key": "core_findings",
        "content": content,
        "claims": [{
            "claim_type": "finding",
            "text": content,
            "start": 0,
            "end": len(content),
            "finding_ids": [FINDING],
            "evidence_roles": ["support"],
            "stat_fact_id": None,
        }],
    }, ensure_ascii=False)


def _prompt_bundle():
    return (
        {
            "interview_v2_report_section_rerun_system": "writer prompt",
            "interview_v2_report_audit_system": "audit prompt",
        },
        {
            "interview_v2_report_section_rerun_system": {
                "version": 1,
                "sha256": "3" * 64,
            },
            "interview_v2_report_audit_system": {
                "version": 1,
                "sha256": "4" * 64,
            },
        },
    )


def _claim_side_effect(**kwargs):
    return {
        **kwargs,
        "status": "pending",
        "_claim_acquired": True,
        "created_at": "2026-09-03T00:02:00Z",
    }


def _complete_side_effect(**kwargs):
    return {
        "rerun_id": "rerun_" + "5" * 32,
        "status": "completed",
        "base_report_version_id": BASE_REPORT,
        "report_version_id": kwargs["report_version_id"],
        "section_id": SECTION,
        "base_section_revision": 2,
        "request_fingerprint": kwargs["request_fingerprint"],
    }


class InterviewV2ReportRerunServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_rerun_replaces_only_target_and_preserves_locked_section(self):
        saved = _saved_report(approved=True)
        original = deepcopy(saved["revision"])

        def save_result(**kwargs):
            revision = deepcopy(kwargs["revision"])
            revision["version_number"] = 5
            revision["revision_payload_sha256"] = "7" * 64
            return {
                "state": {
                    "current_report_version_id": revision["report_version_id"]
                },
                "revision": revision,
            }

        llm = AsyncMock(side_effect=[
            (_writer_output(), "writer-model"),
            (json.dumps({"issues": []}), "audit-model"),
        ])
        with (
            patch.object(service, "_load_accessible_report", return_value=saved),
            patch.object(service, "_is_report_current", return_value=True),
            patch.object(service, "_load_prompt_bundle", return_value=_prompt_bundle()),
            patch.object(
                service.store,
                "claim_report_rerun_operation",
                side_effect=_claim_side_effect,
            ),
            patch.object(
                service.store, "load_current_report_version", return_value=saved
            ),
            patch.object(service, "collect_chat_completion", llm),
            patch.object(
                service.store,
                "save_report_version_cas",
                side_effect=save_result,
            ) as save_mock,
            patch.object(
                service.store,
                "complete_report_rerun_operation",
                side_effect=_complete_side_effect,
            ),
            patch.object(
                service.store,
                "release_report_rerun_operation",
                return_value=True,
            ) as release_mock,
        ):
            result = await service.create_report_section_rerun(
                PROJECT, _request(), LOGIN, KEY
            )

        self.assertEqual("draft", result["status"])
        self.assertFalse(result["rerun"]["reused"])
        revision = save_mock.call_args.kwargs["revision"]
        target = next(
            item for item in revision["sections"] if item["section_id"] == SECTION
        )
        locked = next(
            item
            for item in revision["sections"]
            if item["section_id"] == LOCKED_SECTION
        )
        self.assertEqual(3, target["section_revision"])
        self.assertEqual("入口可理解。", target["content"])
        self.assertFalse(target["locked"])
        self.assertEqual("人工锁定建议。", locked["content"])
        self.assertTrue(locked["locked"])
        self.assertEqual(5, locked["section_revision"])
        self.assertNotIn("approved_by", revision)
        self.assertNotIn("approved_at", revision)
        self.assertNotIn("保持简洁", json.dumps(revision, ensure_ascii=False))
        self.assertEqual("section_rerun", revision["revision_action"])
        self.assertEqual(2, save_mock.call_args.kwargs["base_section_revision"])
        self.assertEqual(original, saved["revision"])
        release_mock.assert_not_called()

    async def test_audit_failure_creates_blocking_draft_instead_of_passing(self):
        saved = _saved_report()

        def save_result(**kwargs):
            revision = deepcopy(kwargs["revision"])
            revision["version_number"] = 5
            return {
                "state": {
                    "current_report_version_id": revision["report_version_id"]
                },
                "revision": revision,
            }

        with (
            patch.object(service, "_load_accessible_report", return_value=saved),
            patch.object(service, "_is_report_current", return_value=True),
            patch.object(service, "_load_prompt_bundle", return_value=_prompt_bundle()),
            patch.object(
                service.store,
                "claim_report_rerun_operation",
                side_effect=_claim_side_effect,
            ),
            patch.object(
                service.store, "load_current_report_version", return_value=saved
            ),
            patch.object(
                service,
                "collect_chat_completion",
                new=AsyncMock(side_effect=[
                    (_writer_output(), "writer-model"),
                    RuntimeError("audit unavailable"),
                ]),
            ),
            patch.object(
                service.store,
                "save_report_version_cas",
                side_effect=save_result,
            ) as save_mock,
            patch.object(
                service.store,
                "complete_report_rerun_operation",
                side_effect=_complete_side_effect,
            ),
        ):
            result = await service.create_report_section_rerun(
                PROJECT, _request(), LOGIN, KEY
            )

        self.assertEqual("audit_failed", result["audit_status"])
        codes = {
            item["code"]
            for item in save_mock.call_args.kwargs["revision"]["audit_issues"]
        }
        self.assertIn("REPORT_AUDIT_INCOMPLETE", codes)

    async def test_invalid_writer_output_releases_claim_without_advancing_head(self):
        saved = _saved_report()
        with (
            patch.object(service, "_load_accessible_report", return_value=saved),
            patch.object(service, "_is_report_current", return_value=True),
            patch.object(service, "_load_prompt_bundle", return_value=_prompt_bundle()),
            patch.object(
                service.store,
                "claim_report_rerun_operation",
                side_effect=_claim_side_effect,
            ),
            patch.object(
                service.store, "load_current_report_version", return_value=saved
            ),
            patch.object(
                service,
                "collect_chat_completion",
                new=AsyncMock(return_value=("{}", "writer-model")),
            ),
            patch.object(service.store, "save_report_version_cas") as save_mock,
            patch.object(
                service.store,
                "release_report_rerun_operation",
                return_value=True,
            ) as release_mock,
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                await service.create_report_section_rerun(
                    PROJECT, _request(), LOGIN, KEY
                )

        self.assertEqual("REPORT_RERUN_MODEL_OUTPUT_INVALID", raised.exception.code)
        save_mock.assert_not_called()
        release_mock.assert_called_once()

    async def test_completed_idempotent_replay_skips_models(self):
        base = _saved_report()

        def load_report(report_version_id, _login):
            if report_version_id == BASE_REPORT:
                return base
            completed = deepcopy(base)
            completed["revision"]["report_version_id"] = report_version_id
            completed["revision"]["version_number"] = 5
            completed["state"]["current_report_version_id"] = report_version_id
            return completed

        def completed_claim(**kwargs):
            return {
                **kwargs,
                "status": "completed",
                "_claim_acquired": False,
            }

        llm = AsyncMock()
        with (
            patch.object(service, "_load_accessible_report", side_effect=load_report),
            patch.object(
                service, "_is_report_current", return_value=False
            ) as current_mock,
            patch.object(service, "_load_prompt_bundle", return_value=_prompt_bundle()),
            patch.object(
                service.store,
                "claim_report_rerun_operation",
                side_effect=completed_claim,
            ),
            patch.object(service, "collect_chat_completion", llm),
        ):
            result = await service.create_report_section_rerun(
                PROJECT, _request(), LOGIN, KEY
            )

        self.assertTrue(result["rerun"]["reused"])
        llm.assert_not_awaited()
        current_mock.assert_not_called()

    async def test_existing_pending_request_reports_in_progress(self):
        saved = _saved_report()

        def pending_claim(**kwargs):
            return {
                **kwargs,
                "status": "pending",
                "_claim_acquired": False,
                "created_at": "2026-09-03T00:02:00Z",
            }

        with (
            patch.object(service, "_load_accessible_report", return_value=saved),
            patch.object(service, "_is_report_current", return_value=True),
            patch.object(service, "_load_prompt_bundle", return_value=_prompt_bundle()),
            patch.object(
                service.store,
                "claim_report_rerun_operation",
                side_effect=pending_claim,
            ),
            patch.object(service, "collect_chat_completion", new=AsyncMock()) as llm,
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                await service.create_report_section_rerun(
                    PROJECT, _request(), LOGIN, KEY
                )

        self.assertEqual("RERUN_IN_PROGRESS", raised.exception.code)
        llm.assert_not_awaited()

    async def test_idempotency_conflict_is_mapped_before_model(self):
        saved = _saved_report()
        with (
            patch.object(service, "_load_accessible_report", return_value=saved),
            patch.object(service, "_load_prompt_bundle", return_value=_prompt_bundle()),
            patch.object(
                service.store,
                "claim_report_rerun_operation",
                side_effect=service.store.ReportRerunIdempotencyConflictError(),
            ),
            patch.object(service, "collect_chat_completion", new=AsyncMock()) as llm,
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                await service.create_report_section_rerun(
                    PROJECT, _request(), LOGIN, KEY
                )

        self.assertEqual("RERUN_IDEMPOTENCY_CONFLICT", raised.exception.code)
        llm.assert_not_awaited()

    async def test_locked_target_is_rejected_before_prompt_or_model(self):
        saved = _saved_report(target_locked=True)
        with (
            patch.object(service, "_load_accessible_report", return_value=saved),
            patch.object(service, "_is_report_current", return_value=True),
            patch.object(service, "_load_prompt_bundle") as prompt_mock,
            patch.object(service, "collect_chat_completion", new=AsyncMock()) as llm,
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                await service.create_report_section_rerun(
                    PROJECT, _request(), LOGIN, KEY
                )

        self.assertEqual("REPORT_SECTION_LOCKED", raised.exception.code)
        prompt_mock.assert_not_called()
        llm.assert_not_awaited()

    async def test_stale_new_request_releases_claim_before_model(self):
        saved = _saved_report()
        with (
            patch.object(service, "_load_accessible_report", return_value=saved),
            patch.object(service, "_is_report_current", return_value=False),
            patch.object(service, "_load_prompt_bundle", return_value=_prompt_bundle()),
            patch.object(
                service.store,
                "claim_report_rerun_operation",
                side_effect=_claim_side_effect,
            ),
            patch.object(
                service.store,
                "release_report_rerun_operation",
                return_value=True,
            ) as release_mock,
            patch.object(service, "collect_chat_completion", new=AsyncMock()) as llm,
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                await service.create_report_section_rerun(
                    PROJECT, _request(), LOGIN, KEY
                )

        self.assertEqual("REPORT_INPUT_CHANGED", raised.exception.code)
        release_mock.assert_called_once()
        llm.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
