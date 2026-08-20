import unittest
from copy import deepcopy
from unittest.mock import patch

from app.services import interview_v2_structure_service as service
from app.services.interview_v2_import_service import InterviewV2ImportError
from app.schemas.interview_v2_structure import (
    InterviewV2EvidenceContextResponse,
    InterviewV2StructureBuildResponse,
)


PROJECT_ID = "project_" + "1" * 32
IMPORT_ID = "import_" + "2" * 32
WORKBOOK_ID = "workbook_" + "3" * 32
MAPPING_ID = "mapping_" + "4" * 32
STRUCTURE_ID = "structure_" + "5" * 32
EVIDENCE_REVISION_ID = "evidence_" + "6" * 32
ISSUE_ID = "issue_" + "7" * 32
EVIDENCE_ID = "ev_" + "8" * 32
LOGIN = {"email": "owner@example.com", "name": "Owner"}


def _public(status: str = "GROUP_MAPPING_CONFIRMED") -> dict:
    return {
        "project_id": PROJECT_ID,
        "import_id": IMPORT_ID,
        "workbook_revision_id": WORKBOOK_ID,
        "status": status,
    }


def _input_bundle() -> dict:
    return {
        "interview_import": _public(),
        "workbook_revision": {"snapshot_sha256": "b" * 64},
        "physical_snapshot": {
            "snapshot_sha256": "b" * 64,
            "sheets": [],
        },
        "mapping_state": {
            "confirmed_mapping_revision_id": MAPPING_ID,
            "confirmed_mapping_sha256": "a" * 64,
        },
        "mapping_revision": {
            "mapping_revision_id": MAPPING_ID,
            "mapping_sha256": "a" * 64,
            "mapping": {"groups": []},
        },
    }


def _core_result(*, blocking: bool = False) -> dict:
    issue = {
        "issue_id": ISSUE_ID,
        "status": "open",
        "severity": "blocking",
    }
    return {
        "structure": {
            "structure_schema_version": "interview-structure/1.0",
            "source": {
                "project_id": PROJECT_ID,
                "import_id": IMPORT_ID,
                "workbook_revision_id": WORKBOOK_ID,
                "base_snapshot_sha256": "b" * 64,
                "mapping_revision_id": MAPPING_ID,
                "mapping_sha256": "a" * 64,
                "rules_version": "interview-v2-structure-rules/1.0",
            },
            "modules": [],
            "main_questions": [],
            "occurrences": [],
        },
        "evidence": {
            "source": {
                "project_id": PROJECT_ID,
                "import_id": IMPORT_ID,
                "workbook_revision_id": WORKBOOK_ID,
                "base_snapshot_sha256": "b" * 64,
                "mapping_revision_id": MAPPING_ID,
                "mapping_sha256": "a" * 64,
            },
            "entries": [],
        },
        "review_issues": [issue] if blocking else [],
        "status": (
            "STRUCTURE_REVIEW_REQUIRED"
            if blocking
            else "READY_FOR_DOSSIERS"
        ),
    }


def _current_bundle(*, request_fingerprint: str = "f" * 64) -> dict:
    result = _core_result(blocking=True)
    state = {
        "project_id": PROJECT_ID,
        "import_id": IMPORT_ID,
        "workbook_revision_id": WORKBOOK_ID,
        "current_mapping_revision_id": MAPPING_ID,
        "current_mapping_sha256": "a" * 64,
        "current_structure_revision_number": 1,
        "current_structure_revision_id": STRUCTURE_ID,
        "current_evidence_revision_number": 1,
        "current_evidence_revision_id": EVIDENCE_REVISION_ID,
        "current_request_fingerprint": request_fingerprint,
        "effective_status": "STRUCTURE_REVIEW_REQUIRED",
        "is_stale": False,
        "artifact_status": "CURRENT",
    }
    return {
        "state": state,
        "structure_revision": {
            "structure_revision_id": STRUCTURE_ID,
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "workbook_revision_id": WORKBOOK_ID,
            "snapshot_sha256": "b" * 64,
            "mapping_revision_id": MAPPING_ID,
            "mapping_sha256": "a" * 64,
            "structure": result["structure"],
        },
        "evidence_revision": {
            "evidence_revision_id": EVIDENCE_REVISION_ID,
            "structure_revision_id": STRUCTURE_ID,
            "evidence": result["evidence"],
        },
        "review_issues": result["review_issues"],
        "manual_overrides": [],
    }


class InterviewV2StructureServiceTests(unittest.TestCase):
    def test_build_owner_check_happens_before_loading_raw_input(self):
        denied = InterviewV2ImportError(
            status_code=404,
            code="INTERVIEW_IMPORT_NOT_FOUND",
            message="导入记录不存在。",
        )
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                side_effect=denied,
            ),
            patch.object(
                service.store, "load_confirmed_structure_input_bundle"
            ) as load_input,
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.build_structure(
                    IMPORT_ID,
                    {
                        "base_mapping_revision_id": MAPPING_ID,
                        "base_mapping_sha256": "a" * 64,
                    },
                    LOGIN,
                )

        self.assertEqual(caught.exception.status_code, 404)
        load_input.assert_not_called()

    def test_build_rejects_stale_client_mapping_head_before_core(self):
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=_public(),
            ),
            patch.object(
                service.store,
                "load_confirmed_structure_input_bundle",
                return_value=_input_bundle(),
            ),
            patch.object(service, "build_structure_and_evidence") as build_core,
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.build_structure(
                    IMPORT_ID,
                    {
                        "base_mapping_revision_id": "mapping_" + "9" * 32,
                        "base_mapping_sha256": "c" * 64,
                    },
                    LOGIN,
                )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.code, "STRUCTURE_INPUT_CONFLICT")
        build_core.assert_not_called()

    def test_build_is_idempotent_and_never_discards_manual_review(self):
        current = _current_bundle()
        current["manual_overrides"] = [
            {"manual_override_id": "override_" + "1" * 32}
        ]
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=_public(),
            ),
            patch.object(
                service.store,
                "load_confirmed_structure_input_bundle",
                return_value=_input_bundle(),
            ),
            patch.object(
                service.store,
                "load_structure_state",
                return_value=current["state"],
            ),
            patch.object(
                service.store,
                "load_current_structure_bundle",
                return_value=current,
            ),
            patch.object(service, "build_structure_and_evidence") as build_core,
            patch.object(service.store, "save_structure_bundle_cas") as save,
        ):
            response = service.build_structure(
                IMPORT_ID,
                {
                    "base_mapping_revision_id": MAPPING_ID,
                    "base_mapping_sha256": "a" * 64,
                },
                LOGIN,
            )

        self.assertEqual(response["structure_revision_id"], STRUCTURE_ID)
        build_core.assert_not_called()
        save.assert_not_called()

    def test_first_build_saves_both_revision_heads_and_reads_durable_result(self):
        durable = _current_bundle()

        def save_side_effect(**kwargs):
            durable["manual_overrides"] = deepcopy(kwargs["manual_overrides"])
            return (
                kwargs["structure_revision"],
                kwargs["evidence_revision"],
                durable["state"],
            )

        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=_public(),
            ),
            patch.object(
                service.store,
                "load_confirmed_structure_input_bundle",
                return_value=_input_bundle(),
            ),
            patch.object(service.store, "load_structure_state", return_value=None),
            patch.object(
                service,
                "build_structure_and_evidence",
                return_value=_core_result(blocking=True),
            ),
            patch.object(
                service.store,
                "save_structure_bundle_cas",
                side_effect=save_side_effect,
            ) as save,
            patch.object(
                service.store,
                "load_current_structure_bundle",
                return_value=durable,
            ) as load_durable,
        ):
            response = service.build_structure(
                IMPORT_ID,
                {
                    "base_mapping_revision_id": MAPPING_ID,
                    "base_mapping_sha256": "a" * 64,
                },
                LOGIN,
            )

        kwargs = save.call_args.kwargs
        self.assertIsNone(kwargs["base_structure_revision_id"])
        self.assertIsNone(kwargs["base_evidence_revision_id"])
        self.assertEqual(kwargs["effective_status"], "STRUCTURE_REVIEW_REQUIRED")
        self.assertEqual(
            kwargs["structure_revision"]["request_fingerprint"],
            kwargs["request_fingerprint"],
        )
        self.assertEqual(
            kwargs["evidence_revision"]["request_fingerprint"],
            kwargs["request_fingerprint"],
        )
        load_durable.assert_called_once_with(PROJECT_ID, IMPORT_ID)
        self.assertEqual(response["review_summary"]["blocking_issue_count"], 1)
        InterviewV2StructureBuildResponse.model_validate(response)

    def test_build_mapping_race_returns_sanitized_input_conflict(self):
        latest_mapping_id = "mapping_" + "9" * 32
        latest_mapping_sha = "c" * 64
        conflict = service.store.StructureInputConflictError(
            current_mapping_revision_id=latest_mapping_id,
            current_mapping_sha256=latest_mapping_sha,
            current_mapping_status="GROUP_CONFIRMATION_REQUIRED",
        )
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=_public(),
            ),
            patch.object(
                service.store,
                "load_confirmed_structure_input_bundle",
                return_value=_input_bundle(),
            ),
            patch.object(service.store, "load_structure_state", return_value=None),
            patch.object(
                service,
                "build_structure_and_evidence",
                return_value=_core_result(blocking=True),
            ),
            patch.object(
                service.store,
                "save_structure_bundle_cas",
                side_effect=conflict,
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.build_structure(
                    IMPORT_ID,
                    {
                        "base_mapping_revision_id": MAPPING_ID,
                        "base_mapping_sha256": "a" * 64,
                    },
                    LOGIN,
                )

        error = caught.exception
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.code, "STRUCTURE_INPUT_CONFLICT")
        self.assertEqual(error.suggested_action, "refresh_group_mapping")
        self.assertEqual(
            error.context,
            {
                "current_mapping_revision_id": latest_mapping_id,
                "current_mapping_sha256": latest_mapping_sha,
                "current_mapping_status": "GROUP_CONFIRMATION_REQUIRED",
            },
        )

    def test_build_non_conflict_value_error_remains_persistence_failure(self):
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=_public(),
            ),
            patch.object(
                service.store,
                "load_confirmed_structure_input_bundle",
                return_value=_input_bundle(),
            ),
            patch.object(service.store, "load_structure_state", return_value=None),
            patch.object(
                service,
                "build_structure_and_evidence",
                return_value=_core_result(blocking=True),
            ),
            patch.object(
                service.store,
                "save_structure_bundle_cas",
                side_effect=ValueError("unrelated durable validation failure"),
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.build_structure(
                    IMPORT_ID,
                    {
                        "base_mapping_revision_id": MAPPING_ID,
                        "base_mapping_sha256": "a" * 64,
                    },
                    LOGIN,
                )

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(
            caught.exception.code,
            "STRUCTURE_PERSISTENCE_FAILED",
        )

    def test_concurrent_same_build_fingerprint_reloads_successfully(self):
        durable = _current_bundle()

        def lose_same_request(**kwargs):
            durable["state"]["current_request_fingerprint"] = kwargs[
                "request_fingerprint"
            ]
            raise FileExistsError("same request won the CAS")

        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=_public(),
            ),
            patch.object(
                service.store,
                "load_confirmed_structure_input_bundle",
                return_value=_input_bundle(),
            ),
            patch.object(service.store, "load_structure_state", return_value=None),
            patch.object(
                service,
                "build_structure_and_evidence",
                return_value=_core_result(blocking=True),
            ),
            patch.object(
                service.store,
                "save_structure_bundle_cas",
                side_effect=lose_same_request,
            ),
            patch.object(
                service.store,
                "load_current_structure_bundle",
                return_value=durable,
            ),
        ):
            response = service.build_structure(
                IMPORT_ID,
                {
                    "base_mapping_revision_id": MAPPING_ID,
                    "base_mapping_sha256": "a" * 64,
                },
                LOGIN,
            )

        self.assertEqual(response["structure_revision_id"], STRUCTURE_ID)

    def test_batch_resolution_uses_dual_head_cas_and_is_atomic(self):
        current = _current_bundle()
        resolved = _core_result(blocking=False)
        override_id = "override_" + "9" * 32
        resolved["manual_overrides"] = [
            {
                "manual_override_id": override_id,
                "issue_id": ISSUE_ID,
            }
        ]
        durable = deepcopy(current)
        durable["state"].update(
            {
                "current_structure_revision_id": "structure_" + "a" * 32,
                "current_evidence_revision_id": "evidence_" + "b" * 32,
                "effective_status": "READY_FOR_DOSSIERS",
            }
        )
        durable["review_issues"] = [
            {
                "issue_id": ISSUE_ID,
                "status": "resolved",
                "severity": "blocking",
            }
        ]

        def save_side_effect(**kwargs):
            durable["manual_overrides"] = deepcopy(kwargs["manual_overrides"])
            return (
                kwargs["structure_revision"],
                kwargs["evidence_revision"],
                durable["state"],
            )

        request = {
            "base_structure_revision_id": STRUCTURE_ID,
            "base_evidence_revision_id": EVIDENCE_REVISION_ID,
            "resolutions": [
                {
                    "issue_id": ISSUE_ID,
                    "resolution": "accept_suggestion",
                    "target_id": None,
                    "row_role": None,
                    "evidence_type": None,
                    "comment": "接受建议",
                }
            ],
        }
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=_public(),
            ),
            patch.object(
                service.store,
                "load_current_structure_bundle",
                side_effect=[current, durable],
            ),
            patch.object(
                service, "apply_review_resolutions", return_value=resolved
            ) as apply,
            patch.object(
                service.store,
                "save_structure_bundle_cas",
                side_effect=save_side_effect,
            ) as save,
        ):
            response = service.resolve_review_issues_batch(
                IMPORT_ID, request, LOGIN
            )

        kwargs = save.call_args.kwargs
        self.assertEqual(kwargs["base_structure_revision_id"], STRUCTURE_ID)
        self.assertEqual(kwargs["base_evidence_revision_id"], EVIDENCE_REVISION_ID)
        self.assertEqual(
            apply.call_args.kwargs["operation_fingerprint"],
            kwargs["request_fingerprint"],
        )
        self.assertEqual(
            kwargs["manual_overrides"][0]["request_fingerprint"],
            kwargs["request_fingerprint"],
        )
        self.assertEqual(response["status"], "READY_FOR_DOSSIERS")
        self.assertEqual(response["manual_override_ids"], [override_id])

    def test_review_mapping_race_returns_input_conflict_envelope(self):
        current = _current_bundle()
        latest_mapping_id = "mapping_" + "9" * 32
        latest_mapping_sha = "c" * 64
        conflict = service.store.StructureInputConflictError(
            current_mapping_revision_id=latest_mapping_id,
            current_mapping_sha256=latest_mapping_sha,
            current_mapping_status="GROUP_MAPPING_CONFIRMED",
        )
        request = {
            "base_structure_revision_id": STRUCTURE_ID,
            "base_evidence_revision_id": EVIDENCE_REVISION_ID,
            "resolutions": [
                {
                    "issue_id": ISSUE_ID,
                    "resolution": "accept_suggestion",
                    "target_id": None,
                    "row_role": None,
                    "evidence_type": None,
                    "comment": "接受建议",
                }
            ],
        }
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=_public(),
            ),
            patch.object(
                service.store,
                "load_current_structure_bundle",
                return_value=current,
            ),
            patch.object(
                service,
                "apply_review_resolutions",
                return_value=_core_result(blocking=False),
            ),
            patch.object(
                service.store,
                "save_structure_bundle_cas",
                side_effect=conflict,
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.resolve_review_issues_batch(
                    IMPORT_ID,
                    request,
                    LOGIN,
                )

        error = caught.exception
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.code, "STRUCTURE_INPUT_CONFLICT")
        self.assertEqual(error.suggested_action, "refresh_group_mapping")
        self.assertEqual(
            error.context,
            {
                "current_mapping_revision_id": latest_mapping_id,
                "current_mapping_sha256": latest_mapping_sha,
                "current_mapping_status": "GROUP_MAPPING_CONFIRMED",
            },
        )

    def test_evidence_raw_context_load_occurs_only_after_owner_check(self):
        denied = InterviewV2ImportError(
            status_code=404,
            code="INTERVIEW_IMPORT_NOT_FOUND",
            message="导入记录不存在。",
        )
        with (
            patch.object(
                service.store,
                "locate_evidence",
                return_value={"project_id": PROJECT_ID, "import_id": IMPORT_ID},
            ),
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                side_effect=denied,
            ),
            patch.object(service.store, "load_evidence_with_context") as load_raw,
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.get_evidence_context(EVIDENCE_ID, {"email": "other@example.com"})

        self.assertEqual(caught.exception.status_code, 404)
        load_raw.assert_not_called()

    def test_evidence_conflict_reload_failure_keeps_v2_error_envelope(self):
        current = _current_bundle()
        current["evidence_revision"]["evidence"]["entries"] = [
            {"evidence_id": EVIDENCE_ID}
        ]
        with (
            patch.object(
                service.store,
                "locate_evidence",
                return_value={"project_id": PROJECT_ID, "import_id": IMPORT_ID},
            ),
            patch.object(service, "_load_current_bundle", return_value=(_public(), current)),
            patch.object(
                service.store,
                "load_evidence_with_context",
                side_effect=FileExistsError("concurrent revision"),
            ),
            patch.object(
                service.store,
                "load_structure_state",
                side_effect=ValueError("corrupt head"),
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.get_evidence_context(EVIDENCE_ID, LOGIN)

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.code, "STRUCTURE_PERSISTENCE_FAILED")

    def test_review_issue_content_load_occurs_only_after_owner_check(self):
        denied = InterviewV2ImportError(
            status_code=404,
            code="INTERVIEW_IMPORT_NOT_FOUND",
            message="导入记录不存在。",
        )
        with (
            patch.object(
                service.store,
                "locate_review_issue",
                return_value={"project_id": PROJECT_ID, "import_id": IMPORT_ID},
            ),
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                side_effect=denied,
            ),
            patch.object(
                service.store, "load_current_structure_bundle"
            ) as load_content,
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.resolve_review_issue(
                    ISSUE_ID,
                    {
                        "base_structure_revision_id": STRUCTURE_ID,
                        "base_evidence_revision_id": EVIDENCE_REVISION_ID,
                        "resolution": "accept_suggestion",
                        "comment": "接受建议",
                    },
                    {"email": "other@example.com"},
                )

        self.assertEqual(caught.exception.status_code, 404)
        load_content.assert_not_called()

    def test_public_evidence_context_drops_storage_and_other_participant_fields(self):
        response = service._public_evidence_context(
            {
                "evidence": {
                    "evidence_id": EVIDENCE_ID,
                    "participant_id": "participant_01",
                    "participant_label": "P01",
                    "group_id": "group_01",
                    "recorder_label": "记录员1",
                    "occurrence_id": "occ_" + "c" * 32,
                    "evidence_type": "participant_self_report",
                    "capture_context": "follow_up_answer",
                    "raw_content": "目标玩家原话",
                    "display_content": "目标玩家展示值",
                    "normalized_content": "目标玩家原话",
                    "fragment_text_field": "normalized_content",
                    "fragment_start": 0,
                    "fragment_end": len("目标玩家原话"),
                    "source_cell_id": "cell_" + "d" * 32,
                    "sheet_id": "sheet_001",
                    "sheet_name": "1组记录1",
                    "row": 2,
                    "column": 4,
                    "cell_address": "D2",
                    "source_value_sha256": "e" * 64,
                    "formula_cache_status": "not_applicable",
                    "inclusion_status": "included",
                    "identity_decision_status": "system_verified",
                    "decision_source": "deterministic_context",
                    "confidence": 1.0,
                    "confirmed_by": "email:owner@example.com",
                    "owner_email": "owner@example.com",
                    "other_participant_raw": "其他玩家原话",
                    "storage_path": "private/evidence.json",
                },
                "occurrence": {
                    "occurrence_id": "occ_" + "c" * 32,
                    "group_id": "group_01",
                    "sheet_id": "sheet_001",
                    "sheet_name": "1组记录1",
                    "recorder_label": "记录员1",
                    "row": 2,
                    "row_role": "follow_up",
                    "mapping_method": "inherit_previous_main_question",
                    "confidence": 1.0,
                    "decision_status": "system_verified",
                    "decision_source": "deterministic_rule",
                    "owner_email": "owner@example.com",
                },
                "physical_snapshot": {
                    "raw_value": "整份快照不得返回",
                    "other_participant_raw": "其他玩家原话",
                },
                "owner_email": "owner@example.com",
            },
            EVIDENCE_ID,
            {
                "current_structure_revision_id": STRUCTURE_ID,
                "current_evidence_revision_id": EVIDENCE_REVISION_ID,
            },
        )

        serialized = repr(response)
        validated = InterviewV2EvidenceContextResponse.model_validate(
            response
        ).model_dump(mode="json")
        self.assertIn("目标玩家原话", serialized)
        self.assertNotIn("owner@example.com", serialized)
        self.assertNotIn("其他玩家原话", serialized)
        self.assertNotIn("整份快照不得返回", serialized)
        self.assertNotIn("private/evidence.json", serialized)
        self.assertNotIn("confirmed_by", serialized)
        self.assertEqual(validated["source_context"]["cell_address"], "D2")
        self.assertEqual(
            validated["evidence"]["display_content"], "目标玩家展示值"
        )
        self.assertEqual(
            validated["evidence"]["fragment_text_field"],
            "normalized_content",
        )
        self.assertEqual(validated["evidence"]["fragment_start"], 0)
        self.assertEqual(
            validated["evidence"]["fragment_end"], len("目标玩家原话")
        )

    def test_structure_summary_counts_only_included_evidence(self):
        bundle = _current_bundle()
        bundle["evidence_revision"]["evidence"]["entries"] = [
            {
                "evidence_id": "ev_" + "1" * 32,
                "evidence_type": "participant_self_report",
                "inclusion_status": "included",
                "identity_decision_status": "system_verified",
            },
            {
                "evidence_id": "ev_" + "2" * 32,
                "evidence_type": "researcher_observation",
                "inclusion_status": "excluded_by_user",
                "identity_decision_status": "needs_review",
            },
        ]

        summary = service._structure_response(bundle)["evidence_summary"]

        self.assertEqual(summary["evidence_count"], 1)
        self.assertEqual(summary["self_report_count"], 1)
        self.assertEqual(summary["observation_count"], 0)
        self.assertEqual(summary["needs_review_count"], 0)

    def test_public_structure_and_issue_payloads_drop_internal_actors(self):
        structure = service._public_structure_payload(
            {
                "structure_schema_version": "interview-structure/1.0",
                "source": {
                    "project_id": PROJECT_ID,
                    "import_id": IMPORT_ID,
                    "workbook_revision_id": WORKBOOK_ID,
                    "base_snapshot_sha256": "b" * 64,
                    "mapping_revision_id": MAPPING_ID,
                    "mapping_sha256": "a" * 64,
                    "rules_version": "rules/1",
                    "owner_email": "owner@example.com",
                },
                "modules": [
                    {
                        "module_id": "module_" + "1" * 32,
                        "confirmed_by": "email:owner@example.com",
                        "created_by": "email:owner@example.com",
                    }
                ],
                "main_questions": [],
                "occurrences": [],
            }
        )
        issue = service._public_review_issue(
            {
                "issue_id": ISSUE_ID,
                "code": "ROW_ROLE_UNKNOWN",
                "severity": "blocking",
                "status": "resolved",
                "message": "待确认",
                "affected_ids": {},
                "source_context": {},
                "resolution": {
                    "action": "assign_row_role",
                    "resolved_by": "email:owner@example.com",
                    "resolved_at": "2026-08-14T00:00:00Z",
                },
                "owner_email": "owner@example.com",
                "raw_storage_path": "private/issues.json",
            }
        )
        serialized = repr({"structure": structure, "issue": issue})
        self.assertNotIn("owner@example.com", serialized)
        self.assertNotIn("confirmed_by", serialized)
        self.assertNotIn("resolved_by", serialized)
        self.assertNotIn("created_by", serialized)
        self.assertNotIn("private/issues.json", serialized)

    def test_public_evidence_preserves_target_raw_display_and_normalized_values(self):
        response = service._public_evidence_context(
            {
                "evidence": {
                    "evidence_id": EVIDENCE_ID,
                    "sheet_id": "sheet_001",
                    "row": 4,
                    "column": 4,
                    "cell_address": "D4",
                    "source_cell_id": "cell_" + "f" * 32,
                    "raw_content": "=ROUND(41.6,0)",
                    "display_content": "42",
                    "normalized_content": "42",
                    "other_participant_raw": "不得返回",
                },
                "physical_snapshot": {
                    "sheets": [
                        {
                            "sheet_id": "sheet_001",
                            "cells": [
                                {
                                    "row": 4,
                                    "column": 5,
                                    "raw_value": "其他玩家不得返回",
                                }
                            ],
                        }
                    ]
                },
            },
            EVIDENCE_ID,
            {
                "current_structure_revision_id": STRUCTURE_ID,
                "current_evidence_revision_id": EVIDENCE_REVISION_ID,
            },
        )

        self.assertEqual(response["evidence"]["raw_content"], "=ROUND(41.6,0)")
        self.assertEqual(response["evidence"]["display_content"], "42")
        self.assertEqual(response["evidence"]["normalized_content"], "42")
        self.assertNotIn("其他玩家不得返回", repr(response))


if __name__ == "__main__":
    unittest.main()
