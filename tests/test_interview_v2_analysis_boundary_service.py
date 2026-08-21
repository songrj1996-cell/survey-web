import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.core.interview_v2_analysis_boundary import (
    InterviewV2AnalysisBoundaryError,
)
from app.schemas.interview_v2_analysis_boundary import (
    InterviewV2AnalysisBoundaryResponse,
    InterviewV2CoveragePreviewResponse,
)
from app.services import interview_v2_analysis_boundary_service as service
from app.services.interview_v2_import_service import InterviewV2ImportError
from tests.test_interview_v2_evidence import build_fixture_checkpoint


PROJECT_ID = "project_" + "1" * 32
IMPORT_ID = "import_" + "2" * 32
STRUCTURE_ID = "structure_" + "3" * 32
EVIDENCE_ID = "evidence_" + "4" * 32
BOUNDARY_ID = "boundary_" + "5" * 32
COVERAGE_ID = "coverage_" + "6" * 32
NEXT_BOUNDARY_ID = "boundary_" + "7" * 32
NEXT_COVERAGE_ID = "coverage_" + "8" * 32
STRUCTURE_SHA = "a" * 64
EVIDENCE_SHA = "b" * 64
BOUNDARY_SHA = "c" * 64
COVERAGE_SHA = "d" * 64
NEXT_BOUNDARY_SHA = "e" * 64
NEXT_COVERAGE_SHA = "f" * 64
LOGIN = {"email": "owner@example.com", "name": "Owner"}


def _public(status: str = "GROUP_MAPPING_CONFIRMED") -> dict:
    return {
        "project_id": PROJECT_ID,
        "import_id": IMPORT_ID,
        "status": status,
    }


def _structure_bundle() -> dict:
    return {
        "state": {
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "current_structure_revision_id": STRUCTURE_ID,
            "current_structure_payload_sha256": STRUCTURE_SHA,
            "current_evidence_revision_id": EVIDENCE_ID,
            "current_evidence_payload_sha256": EVIDENCE_SHA,
            "effective_status": "READY_FOR_DOSSIERS",
            "derived_status": "READY_FOR_DOSSIERS",
            "artifact_status": "CURRENT",
            "is_stale": False,
        },
        "structure_revision": {
            "structure_revision_id": STRUCTURE_ID,
            "structure": {"modules": [], "main_questions": [], "occurrences": []},
        },
        "evidence_revision": {
            "evidence_revision_id": EVIDENCE_ID,
            "evidence": {"entries": []},
        },
    }


def _boundary(*, status: str = "draft", internal: bool = False) -> dict:
    source = {
        "project_id": PROJECT_ID,
        "import_id": IMPORT_ID,
        "structure_revision_id": STRUCTURE_ID,
        "evidence_revision_id": EVIDENCE_ID,
        "rules_version": "interview-v2-analysis-boundary-rules/1.0",
    }
    if internal:
        source["owner_email"] = "owner@example.com"
    payload = {
        "analysis_boundary_schema_version": "interview-analysis-boundary/1.0",
        "source": source,
        "status": status,
        "evaluation_objects": [],
        "source_scope_rules": [],
        "label_scope_rules": [],
    }
    if internal:
        payload["private_storage_path"] = "private/boundary.json"
        payload["evaluation_objects"] = [
            {
                "evaluation_object_id": "evaluation_" + "9" * 32,
                "module_id": "module_" + "a" * 32,
                "object_type": "concept",
                "display_name": "功能概念",
                "display_order": 1,
                "main_question_ids": ["question_" + "b" * 32],
                "occurrence_ids": ["occ_" + "c" * 32],
                "supersedes_evaluation_object_ids": [],
                "decision_status": status,
                "decision_source": "manual_override",
                "created_by": "email:owner@example.com",
            }
        ]
    return payload


def _coverage(*, internal: bool = False) -> dict:
    source = {
        "project_id": PROJECT_ID,
        "import_id": IMPORT_ID,
        "structure_revision_id": STRUCTURE_ID,
        "evidence_revision_id": EVIDENCE_ID,
        "analysis_boundary_sha256": "1" * 64,
        "rules_version": "interview-v2-question-coverage-rules/1.0",
    }
    if internal:
        source["raw_evidence_path"] = "private/evidence.json"
    return {
        "coverage_schema_version": "interview-question-coverage/1.0",
        "source": source,
        "participant_count": 0,
        "row_count": 0,
        "rows": [],
        "summaries": [],
        **({"created_by": "email:owner@example.com"} if internal else {}),
    }


def _boundary_bundle(
    *,
    status: str = "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
    boundary_id: str = BOUNDARY_ID,
    coverage_id: str = COVERAGE_ID,
    boundary_sha: str = BOUNDARY_SHA,
    coverage_sha: str = COVERAGE_SHA,
    revision_number: int = 1,
    boundary_status: str = "draft",
) -> dict:
    return {
        "state": {
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "current_structure_revision_id": STRUCTURE_ID,
            "current_structure_payload_sha256": STRUCTURE_SHA,
            "current_evidence_revision_id": EVIDENCE_ID,
            "current_evidence_payload_sha256": EVIDENCE_SHA,
            "current_boundary_revision_id": boundary_id,
            "current_boundary_revision_number": revision_number,
            "current_boundary_payload_sha256": boundary_sha,
            "current_coverage_revision_id": coverage_id,
            "current_coverage_revision_number": revision_number,
            "current_coverage_payload_sha256": coverage_sha,
            "effective_status": status,
            "derived_status": status,
            "artifact_status": "CURRENT",
            "is_stale": False,
            "current_request_fingerprint": "0" * 64,
            "revision_history": [],
        },
        "boundary_revision": {
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "revision_number": revision_number,
            "boundary_revision_id": boundary_id,
            "analysis_boundary": _boundary(status=boundary_status),
            "revision_payload_sha256": boundary_sha,
        },
        "coverage_revision": {
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "revision_number": revision_number,
            "coverage_revision_id": coverage_id,
            "boundary_revision_id": boundary_id,
            "coverage_preview": _coverage(),
            "revision_payload_sha256": coverage_sha,
        },
    }


def _put_request(*, with_current_heads: bool = True) -> dict:
    return {
        "base_boundary_revision_id": BOUNDARY_ID if with_current_heads else None,
        "base_coverage_revision_id": COVERAGE_ID if with_current_heads else None,
        "base_structure_revision_id": STRUCTURE_ID,
        "base_evidence_revision_id": EVIDENCE_ID,
        "evaluation_objects": [],
        "source_scope_rules": [],
        "label_scope_rules": [],
        "change_reason": "确认分析对象范围",
    }


def _service_patches(*, current=None):
    return (
        patch.object(
            service,
            "get_interview_import_with_mapping_status",
            return_value=_public(),
        ),
        patch.object(
            service.store,
            "load_current_structure_bundle",
            return_value=_structure_bundle(),
        ),
        patch.object(
            service.store,
            "load_current_analysis_boundary_bundle",
            return_value=current,
        ),
    )


class InterviewV2AnalysisBoundaryServiceTests(unittest.TestCase):
    def test_real_core_service_store_roundtrip_and_old_draft_retry(self):
        _snapshot, _mapping, _mapping_sha, core_result = (
            build_fixture_checkpoint()
        )
        structure_bundle = _structure_bundle()
        structure_bundle["structure_revision"]["structure"] = deepcopy(
            core_result["structure"]
        )
        structure_bundle["evidence_revision"]["evidence"] = deepcopy(
            core_result["evidence"]
        )
        current_source = {
            "source": {
                "structure_revision_id": STRUCTURE_ID,
                "structure_payload_sha256": STRUCTURE_SHA,
                "evidence_revision_id": EVIDENCE_ID,
                "evidence_payload_sha256": EVIDENCE_SHA,
            },
            "status": "READY_FOR_DOSSIERS",
            "is_stale": False,
        }

        with tempfile.TemporaryDirectory(prefix="iv2-boundary-seam-") as temp:
            root = Path(temp) / "v2"
            project_dir = root / "projects" / PROJECT_ID
            project_dir.mkdir(parents=True)
            with (
                patch.object(
                    service.store.config, "INTERVIEW_V2_DATA_DIR", root
                ),
                patch.object(
                    service,
                    "_load_structure_input",
                    return_value=(_public(), structure_bundle),
                ),
                patch.object(
                    service.store,
                    "_current_analysis_boundary_source_locked",
                    return_value=deepcopy(current_source),
                ),
            ):
                proposed = service.get_analysis_boundary(IMPORT_ID, LOGIN)
                self.assertFalse(
                    (project_dir / "analysis_boundary_state.json").exists()
                )
                self.assertFalse(
                    (project_dir / "analysis_boundary_revisions").exists()
                )
                self.assertFalse(
                    (project_dir / "coverage_revisions").exists()
                )

                draft = service.save_analysis_boundary(
                    IMPORT_ID,
                    {
                        "base_boundary_revision_id": None,
                        "base_coverage_revision_id": None,
                        "base_structure_revision_id": STRUCTURE_ID,
                        "base_evidence_revision_id": EVIDENCE_ID,
                        "evaluation_objects": deepcopy(
                            proposed["analysis_boundary"]["evaluation_objects"]
                        ),
                        "source_scope_rules": deepcopy(
                            proposed["analysis_boundary"]["source_scope_rules"]
                        ),
                        "label_scope_rules": deepcopy(
                            proposed["analysis_boundary"]["label_scope_rules"]
                        ),
                        "change_reason": "真实 seam 首次保存",
                    },
                    LOGIN,
                )
                draft_confirm_request = {
                    "boundary_revision_id": draft["boundary_revision_id"],
                    "coverage_revision_id": draft["coverage_revision_id"],
                    "boundary_payload_sha256": draft[
                        "boundary_payload_sha256"
                    ],
                    "coverage_payload_sha256": draft[
                        "coverage_payload_sha256"
                    ],
                }
                ready = service.confirm_analysis_boundary(
                    IMPORT_ID, draft_confirm_request, LOGIN
                )
                retried = service.confirm_analysis_boundary(
                    IMPORT_ID, draft_confirm_request, LOGIN
                )

                durable = service.store.load_current_analysis_boundary_bundle(
                    PROJECT_ID, IMPORT_ID
                )

        self.assertEqual(proposed["status"], "ANALYSIS_BOUNDARY_REQUIRED")
        self.assertEqual(
            draft["status"], "ANALYSIS_BOUNDARY_REVIEW_REQUIRED"
        )
        self.assertEqual(draft["boundary_revision_number"], 1)
        self.assertEqual(draft["coverage_revision_number"], 1)
        self.assertEqual(ready["status"], "READY_FOR_DOSSIERS")
        self.assertEqual(ready["analysis_boundary"]["status"], "confirmed")
        self.assertEqual(ready["boundary_revision_number"], 2)
        self.assertEqual(ready["coverage_revision_number"], 2)
        self.assertNotEqual(
            ready["boundary_revision_id"], draft["boundary_revision_id"]
        )
        self.assertNotEqual(
            ready["coverage_revision_id"], draft["coverage_revision_id"]
        )
        self.assertEqual(
            retried["boundary_revision_id"], ready["boundary_revision_id"]
        )
        self.assertEqual(
            retried["coverage_revision_id"], ready["coverage_revision_id"]
        )
        self.assertEqual(retried["boundary_revision_number"], 2)
        self.assertEqual(retried["coverage_revision_number"], 2)
        self.assertEqual(
            durable["state"]["current_boundary_revision_number"], 2
        )
        self.assertEqual(len(durable["state"]["revision_history"]), 2)
        self.assertEqual(len(durable["state"]["confirmation_events"]), 1)
        InterviewV2AnalysisBoundaryResponse.model_validate(retried)

    def test_owner_check_happens_before_any_artifact_read(self):
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
                service.store, "load_current_structure_bundle"
            ) as load_structure,
            patch.object(
                service.store, "load_current_analysis_boundary_bundle"
            ) as load_boundary,
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.get_analysis_boundary(IMPORT_ID, LOGIN)

        self.assertEqual(caught.exception.status_code, 404)
        load_structure.assert_not_called()
        load_boundary.assert_not_called()

    def test_get_proposal_is_read_only_and_returns_no_durable_heads(self):
        proposal = {
            "analysis_boundary": _boundary(),
            "coverage_preview": _coverage(),
        }
        p1, p2, p3 = _service_patches(current=None)
        with (
            p1,
            p2,
            p3,
            patch.object(
                service,
                "build_analysis_boundary_proposal",
                return_value=proposal,
            ) as build,
            patch.object(
                service,
                "confirm_analysis_boundary_payload",
                return_value=_boundary(status="confirmed"),
            ),
            patch.object(
                service.store, "save_analysis_boundary_bundle_cas"
            ) as save,
            patch.object(
                service.store, "confirm_analysis_boundary_cas"
            ) as confirm,
        ):
            response = service.get_analysis_boundary(IMPORT_ID, LOGIN)

        build.assert_called_once()
        save.assert_not_called()
        confirm.assert_not_called()
        self.assertEqual(response["status"], "ANALYSIS_BOUNDARY_REQUIRED")
        self.assertIsNone(response["boundary_revision_id"])
        self.assertIsNone(response["coverage_revision_id"])
        InterviewV2AnalysisBoundaryResponse.model_validate(response)

    def test_stale_get_returns_new_proposal_with_old_cas_heads_then_put_replaces_it(self):
        stale = _boundary_bundle()
        stale["state"].update(
            {
                "is_stale": True,
                "artifact_status": "STALE",
                "effective_status": "ANALYSIS_BOUNDARY_REQUIRED",
                "derived_status": "ANALYSIS_BOUNDARY_REQUIRED",
                "current_structure_revision_id": "structure_" + "9" * 32,
                "current_evidence_revision_id": "evidence_" + "a" * 32,
            }
        )
        proposal = {
            "analysis_boundary": _boundary(),
            "coverage_preview": _coverage(),
        }

        def replace_stale_pair(**kwargs):
            state = deepcopy(stale["state"])
            state.update(
                {
                    "is_stale": False,
                    "artifact_status": "CURRENT",
                    "effective_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                    "derived_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                    "current_structure_revision_id": STRUCTURE_ID,
                    "current_structure_payload_sha256": STRUCTURE_SHA,
                    "current_evidence_revision_id": EVIDENCE_ID,
                    "current_evidence_payload_sha256": EVIDENCE_SHA,
                    "current_boundary_revision_id": kwargs[
                        "boundary_revision"
                    ]["boundary_revision_id"],
                    "current_boundary_revision_number": 2,
                    "current_boundary_payload_sha256": kwargs[
                        "boundary_revision"
                    ]["revision_payload_sha256"],
                    "current_coverage_revision_id": kwargs[
                        "coverage_revision"
                    ]["coverage_revision_id"],
                    "current_coverage_revision_number": 2,
                    "current_coverage_payload_sha256": kwargs[
                        "coverage_revision"
                    ]["revision_payload_sha256"],
                }
            )
            return kwargs["boundary_revision"], kwargs["coverage_revision"], state

        p1, p2, p3 = _service_patches(current=stale)
        with (
            p1,
            p2,
            p3,
            patch.object(
                service,
                "build_analysis_boundary_proposal",
                return_value=proposal,
            ),
            patch.object(
                service,
                "validate_analysis_boundary",
                return_value=_boundary(),
            ),
            patch.object(
                service, "build_coverage_preview", return_value=_coverage()
            ),
            patch.object(
                service,
                "confirm_analysis_boundary_payload",
                return_value=_boundary(status="confirmed"),
            ),
            patch.object(
                service.store,
                "analysis_boundary_revision_payload_sha256",
                return_value=NEXT_BOUNDARY_SHA,
            ),
            patch.object(
                service.store,
                "coverage_revision_payload_sha256",
                return_value=NEXT_COVERAGE_SHA,
            ),
            patch.object(
                service.store,
                "save_analysis_boundary_bundle_cas",
                side_effect=replace_stale_pair,
            ) as save,
        ):
            proposed = service.get_analysis_boundary(IMPORT_ID, LOGIN)
            saved = service.save_analysis_boundary(
                IMPORT_ID, _put_request(), LOGIN
            )

        self.assertEqual(proposed["status"], "ANALYSIS_BOUNDARY_REQUIRED")
        self.assertTrue(proposed["is_stale"])
        self.assertEqual(proposed["boundary_revision_id"], BOUNDARY_ID)
        self.assertEqual(proposed["coverage_revision_id"], COVERAGE_ID)
        self.assertIsNone(proposed["boundary_payload_sha256"])
        self.assertIsNone(proposed["coverage_payload_sha256"])
        self.assertEqual(
            proposed["analysis_boundary"]["source"]["structure_revision_id"],
            STRUCTURE_ID,
        )
        self.assertEqual(save.call_args.kwargs["base_boundary_revision_id"], BOUNDARY_ID)
        self.assertEqual(save.call_args.kwargs["base_coverage_revision_id"], COVERAGE_ID)
        self.assertEqual(saved["status"], "ANALYSIS_BOUNDARY_REVIEW_REQUIRED")

    def test_put_freezes_both_upstream_heads_and_uses_dual_head_cas(self):
        current = _boundary_bundle()

        def save_pair(**kwargs):
            state = deepcopy(current["state"])
            state.update(
                {
                    "current_boundary_revision_id": kwargs[
                        "boundary_revision"
                    ]["boundary_revision_id"],
                    "current_boundary_revision_number": 2,
                    "current_boundary_payload_sha256": kwargs[
                        "boundary_revision"
                    ]["revision_payload_sha256"],
                    "current_coverage_revision_id": kwargs[
                        "coverage_revision"
                    ]["coverage_revision_id"],
                    "current_coverage_revision_number": 2,
                    "current_coverage_payload_sha256": kwargs[
                        "coverage_revision"
                    ]["revision_payload_sha256"],
                    "effective_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                    "derived_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                }
            )
            return kwargs["boundary_revision"], kwargs["coverage_revision"], state

        p1, p2, p3 = _service_patches(current=current)
        with (
            p1,
            p2,
            p3,
            patch.object(
                service, "validate_analysis_boundary", return_value=_boundary()
            ),
            patch.object(
                service, "build_coverage_preview", return_value=_coverage()
            ),
            patch.object(
                service,
                "confirm_analysis_boundary_payload",
                return_value=_boundary(status="confirmed"),
            ),
            patch.object(
                service.store,
                "analysis_boundary_revision_payload_sha256",
                return_value=NEXT_BOUNDARY_SHA,
            ),
            patch.object(
                service.store,
                "coverage_revision_payload_sha256",
                return_value=NEXT_COVERAGE_SHA,
            ),
            patch.object(
                service.store,
                "save_analysis_boundary_bundle_cas",
                side_effect=save_pair,
            ) as save,
        ):
            response = service.save_analysis_boundary(
                IMPORT_ID, _put_request(), LOGIN
            )

        kwargs = save.call_args.kwargs
        self.assertEqual(kwargs["base_boundary_revision_id"], BOUNDARY_ID)
        self.assertEqual(kwargs["base_coverage_revision_id"], COVERAGE_ID)
        self.assertEqual(
            kwargs["boundary_revision"]["source"],
            {
                "structure_revision_id": STRUCTURE_ID,
                "structure_payload_sha256": STRUCTURE_SHA,
                "evidence_revision_id": EVIDENCE_ID,
                "evidence_payload_sha256": EVIDENCE_SHA,
            },
        )
        self.assertEqual(
            kwargs["coverage_revision"]["source"],
            kwargs["boundary_revision"]["source"],
        )
        self.assertEqual(kwargs["boundary_revision"]["revision_number"], 2)
        self.assertEqual(kwargs["coverage_revision"]["revision_number"], 2)
        self.assertEqual(response["boundary_revision_number"], 2)
        self.assertEqual(response["coverage_revision_number"], 2)
        InterviewV2AnalysisBoundaryResponse.model_validate(response)

    def test_confirm_publishes_n_plus_one_confirmed_pair_then_confirms_it(self):
        current = _boundary_bundle()
        latest_holder = {}

        def save_confirmed_pair(**kwargs):
            boundary_revision = kwargs["boundary_revision"]
            coverage_revision = kwargs["coverage_revision"]
            state = deepcopy(current["state"])
            state.update(
                {
                    "current_boundary_revision_id": boundary_revision[
                        "boundary_revision_id"
                    ],
                    "current_boundary_revision_number": 2,
                    "current_boundary_payload_sha256": boundary_revision[
                        "revision_payload_sha256"
                    ],
                    "current_coverage_revision_id": coverage_revision[
                        "coverage_revision_id"
                    ],
                    "current_coverage_revision_number": 2,
                    "current_coverage_payload_sha256": coverage_revision[
                        "revision_payload_sha256"
                    ],
                    "effective_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                    "derived_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                }
            )
            latest_holder["bundle"] = {
                "state": state,
                "boundary_revision": boundary_revision,
                "coverage_revision": coverage_revision,
            }
            return boundary_revision, coverage_revision, state

        def load_boundary(*_args):
            bundle = latest_holder.get("bundle")
            if bundle is not None and bundle["state"].get("confirmed_at"):
                return bundle
            return current if bundle is None else bundle

        def confirm_pair(**kwargs):
            bundle = latest_holder["bundle"]
            bundle["state"].update(
                {
                    "effective_status": "READY_FOR_DOSSIERS",
                    "derived_status": "READY_FOR_DOSSIERS",
                    "confirmed_at": kwargs["confirmed_at"],
                    "confirmed_by": kwargs["confirmed_by"],
                }
            )
            return bundle["state"]

        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=_public(),
            ),
            patch.object(
                service.store,
                "load_current_structure_bundle",
                return_value=_structure_bundle(),
            ),
            patch.object(
                service.store,
                "load_current_analysis_boundary_bundle",
                side_effect=load_boundary,
            ),
            patch.object(
                service,
                "_owner_from_login",
                return_value={"owner_key": "email:owner@example.com"},
            ),
            patch.object(
                service,
                "confirm_analysis_boundary_payload",
                return_value=_boundary(status="confirmed"),
            ),
            patch.object(
                service, "build_coverage_preview", return_value=_coverage()
            ),
            patch.object(
                service.store,
                "analysis_boundary_revision_payload_sha256",
                return_value=NEXT_BOUNDARY_SHA,
            ),
            patch.object(
                service.store,
                "coverage_revision_payload_sha256",
                return_value=NEXT_COVERAGE_SHA,
            ),
            patch.object(
                service.store,
                "save_analysis_boundary_bundle_cas",
                side_effect=save_confirmed_pair,
            ) as save,
            patch.object(
                service.store,
                "confirm_analysis_boundary_cas",
                side_effect=confirm_pair,
            ) as confirm,
        ):
            response = service.confirm_analysis_boundary(
                IMPORT_ID,
                {
                    "boundary_revision_id": BOUNDARY_ID,
                    "coverage_revision_id": COVERAGE_ID,
                    "boundary_payload_sha256": BOUNDARY_SHA,
                    "coverage_payload_sha256": COVERAGE_SHA,
                },
                LOGIN,
            )

        saved_boundary = save.call_args.kwargs["boundary_revision"]
        saved_coverage = save.call_args.kwargs["coverage_revision"]
        self.assertEqual(saved_boundary["revision_number"], 2)
        self.assertEqual(saved_coverage["revision_number"], 2)
        self.assertEqual(saved_boundary["analysis_boundary"]["status"], "confirmed")
        self.assertEqual(
            confirm.call_args.kwargs["boundary_revision_id"],
            saved_boundary["boundary_revision_id"],
        )
        self.assertEqual(
            confirm.call_args.kwargs["coverage_revision_id"],
            saved_coverage["coverage_revision_id"],
        )
        self.assertEqual(response["status"], "READY_FOR_DOSSIERS")
        self.assertEqual(response["boundary_revision_number"], 2)

    def test_confirm_retry_of_ready_head_is_read_only(self):
        ready = _boundary_bundle(
            status="READY_FOR_DOSSIERS", boundary_status="confirmed"
        )
        p1, p2, p3 = _service_patches(current=ready)
        with (
            p1,
            p2,
            p3,
            patch.object(service.store, "save_analysis_boundary_bundle_cas") as save,
            patch.object(service.store, "confirm_analysis_boundary_cas") as confirm,
        ):
            response = service.confirm_analysis_boundary(
                IMPORT_ID,
                {
                    "boundary_revision_id": BOUNDARY_ID,
                    "coverage_revision_id": COVERAGE_ID,
                    "boundary_payload_sha256": BOUNDARY_SHA,
                    "coverage_payload_sha256": COVERAGE_SHA,
                },
                LOGIN,
            )

        save.assert_not_called()
        confirm.assert_not_called()
        self.assertEqual(response["status"], "READY_FOR_DOSSIERS")

    def test_confirm_retry_with_draft_heads_reuses_ready_n_plus_one_pair(self):
        draft = _boundary_bundle()
        ready = _boundary_bundle(
            status="READY_FOR_DOSSIERS",
            boundary_id=NEXT_BOUNDARY_ID,
            coverage_id=NEXT_COVERAGE_ID,
            boundary_sha=NEXT_BOUNDARY_SHA,
            coverage_sha=NEXT_COVERAGE_SHA,
            revision_number=2,
            boundary_status="confirmed",
        )
        ready["state"]["current_request_fingerprint"] = "9" * 64
        ready["state"]["revision_history"] = [
            {
                "revision_number": 1,
                "boundary_revision_id": BOUNDARY_ID,
                "coverage_revision_id": COVERAGE_ID,
                "boundary_payload_sha256": BOUNDARY_SHA,
                "coverage_payload_sha256": COVERAGE_SHA,
            }
        ]
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=_public(),
            ),
            patch.object(
                service.store,
                "load_current_structure_bundle",
                return_value=_structure_bundle(),
            ),
            patch.object(
                service.store,
                "load_current_analysis_boundary_bundle",
                return_value=ready,
            ),
            patch.object(
                service.store,
                "load_analysis_boundary_revision",
                return_value=draft["boundary_revision"],
            ),
            patch.object(
                service.store,
                "load_coverage_revision",
                return_value=draft["coverage_revision"],
            ),
            patch.object(
                service,
                "confirm_analysis_boundary_payload",
                return_value=_boundary(status="confirmed"),
            ),
            patch.object(
                service, "build_coverage_preview", return_value=_coverage()
            ),
            patch.object(
                service,
                "_build_revision_pair",
                return_value=(
                    ready["boundary_revision"],
                    ready["coverage_revision"],
                    "9" * 64,
                    "2026-08-20T00:00:00Z",
                ),
            ),
            patch.object(service.store, "save_analysis_boundary_bundle_cas") as save,
            patch.object(
                service.store,
                "confirm_analysis_boundary_cas",
                return_value=ready["state"],
            ),
        ):
            response = service.confirm_analysis_boundary(
                IMPORT_ID,
                {
                    "boundary_revision_id": BOUNDARY_ID,
                    "coverage_revision_id": COVERAGE_ID,
                    "boundary_payload_sha256": BOUNDARY_SHA,
                    "coverage_payload_sha256": COVERAGE_SHA,
                },
                LOGIN,
            )

        save.assert_not_called()
        self.assertEqual(response["boundary_revision_id"], NEXT_BOUNDARY_ID)
        self.assertEqual(response["boundary_revision_number"], 2)
        self.assertEqual(response["status"], "READY_FOR_DOSSIERS")

    def test_confirm_recovers_save_before_confirm_crash_without_new_revision(self):
        draft = _boundary_bundle()
        recovering = _boundary_bundle(
            status="ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
            boundary_id=NEXT_BOUNDARY_ID,
            coverage_id=NEXT_COVERAGE_ID,
            boundary_sha=NEXT_BOUNDARY_SHA,
            coverage_sha=NEXT_COVERAGE_SHA,
            revision_number=2,
            boundary_status="confirmed",
        )
        recovering["state"]["current_request_fingerprint"] = "9" * 64
        recovering["state"]["revision_history"] = [
            {
                "revision_number": 1,
                "boundary_revision_id": BOUNDARY_ID,
                "coverage_revision_id": COVERAGE_ID,
                "boundary_payload_sha256": BOUNDARY_SHA,
                "coverage_payload_sha256": COVERAGE_SHA,
            }
        ]

        def finish_confirmation(**_kwargs):
            recovering["state"].update(
                {
                    "effective_status": "READY_FOR_DOSSIERS",
                    "derived_status": "READY_FOR_DOSSIERS",
                }
            )
            return recovering["state"]

        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=_public(),
            ),
            patch.object(
                service.store,
                "load_current_structure_bundle",
                return_value=_structure_bundle(),
            ),
            patch.object(
                service.store,
                "load_current_analysis_boundary_bundle",
                return_value=recovering,
            ),
            patch.object(
                service.store,
                "load_analysis_boundary_revision",
                return_value=draft["boundary_revision"],
            ),
            patch.object(
                service.store,
                "load_coverage_revision",
                return_value=draft["coverage_revision"],
            ),
            patch.object(
                service,
                "confirm_analysis_boundary_payload",
                return_value=_boundary(status="confirmed"),
            ),
            patch.object(
                service, "build_coverage_preview", return_value=_coverage()
            ),
            patch.object(
                service,
                "_build_revision_pair",
                return_value=(
                    recovering["boundary_revision"],
                    recovering["coverage_revision"],
                    "9" * 64,
                    "2026-08-20T00:00:00Z",
                ),
            ),
            patch.object(service.store, "save_analysis_boundary_bundle_cas") as save,
            patch.object(
                service.store,
                "confirm_analysis_boundary_cas",
                side_effect=finish_confirmation,
            ) as confirm,
        ):
            response = service.confirm_analysis_boundary(
                IMPORT_ID,
                {
                    "boundary_revision_id": BOUNDARY_ID,
                    "coverage_revision_id": COVERAGE_ID,
                    "boundary_payload_sha256": BOUNDARY_SHA,
                    "coverage_payload_sha256": COVERAGE_SHA,
                },
                LOGIN,
            )

        save.assert_not_called()
        confirm.assert_called_once()
        self.assertEqual(
            confirm.call_args.kwargs["boundary_revision_id"], NEXT_BOUNDARY_ID
        )
        self.assertEqual(
            confirm.call_args.kwargs["coverage_revision_id"], NEXT_COVERAGE_ID
        )
        self.assertEqual(response["status"], "READY_FOR_DOSSIERS")
        self.assertEqual(response["boundary_revision_number"], 2)

    def test_first_save_rejects_proposal_id_reuse_and_untraced_new_object(self):
        _snapshot, _mapping, _mapping_sha, core_result = (
            build_fixture_checkpoint()
        )
        structure_bundle = _structure_bundle()
        structure_bundle["structure_revision"]["structure"] = deepcopy(
            core_result["structure"]
        )
        structure_bundle["evidence_revision"]["evidence"] = deepcopy(
            core_result["evidence"]
        )
        with (
            patch.object(
                service,
                "_load_structure_input",
                return_value=(_public(), structure_bundle),
            ),
            patch.object(
                service.store,
                "load_current_analysis_boundary_bundle",
                return_value=None,
            ),
            patch.object(
                service.store, "save_analysis_boundary_bundle_cas"
            ) as save,
        ):
            proposed = service.get_analysis_boundary(IMPORT_ID, LOGIN)
            objects = proposed["analysis_boundary"]["evaluation_objects"]

            reused_id = deepcopy(proposed["analysis_boundary"])
            multi_occurrence = max(
                reused_id["evaluation_objects"],
                key=lambda item: len(item["occurrence_ids"]),
            )
            multi_occurrence["occurrence_ids"] = multi_occurrence[
                "occurrence_ids"
            ][:-1]
            with self.assertRaises(InterviewV2ImportError) as reused_error:
                service.save_analysis_boundary(
                    IMPORT_ID,
                    {
                        **_put_request(with_current_heads=False),
                        "evaluation_objects": reused_id[
                            "evaluation_objects"
                        ],
                        "source_scope_rules": reused_id[
                            "source_scope_rules"
                        ],
                        "label_scope_rules": reused_id[
                            "label_scope_rules"
                        ],
                    },
                    LOGIN,
                )

            untraced = deepcopy(proposed["analysis_boundary"])
            untraced["evaluation_objects"][0]["decision_status"] = (
                "superseded"
            )
            added = deepcopy(objects[0])
            added.update(
                {
                    "evaluation_object_id": "evaluation_" + "f" * 32,
                    "display_name": "未声明来源的新对象",
                    "display_order": max(
                        item["display_order"] for item in objects
                    )
                    + 1,
                    "supersedes_evaluation_object_ids": [],
                }
            )
            untraced["evaluation_objects"].append(added)
            with self.assertRaises(InterviewV2ImportError) as lineage_error:
                service.save_analysis_boundary(
                    IMPORT_ID,
                    {
                        **_put_request(with_current_heads=False),
                        "evaluation_objects": untraced[
                            "evaluation_objects"
                        ],
                        "source_scope_rules": untraced[
                            "source_scope_rules"
                        ],
                        "label_scope_rules": untraced[
                            "label_scope_rules"
                        ],
                    },
                    LOGIN,
                )

        self.assertEqual(reused_error.exception.status_code, 422)
        self.assertEqual(
            reused_error.exception.code, "EVALUATION_OBJECT_IDENTITY_REUSE"
        )
        self.assertEqual(lineage_error.exception.status_code, 422)
        self.assertEqual(
            lineage_error.exception.code, "EVALUATION_OBJECT_LINEAGE_INVALID"
        )
        save.assert_not_called()

    def test_stale_rebuild_uses_new_head_proposal_as_lineage_base(self):
        _snapshot, _mapping, _mapping_sha, core_result = (
            build_fixture_checkpoint()
        )
        structure_bundle = _structure_bundle()
        structure_bundle["structure_revision"]["structure"] = deepcopy(
            core_result["structure"]
        )
        structure_bundle["evidence_revision"]["evidence"] = deepcopy(
            core_result["evidence"]
        )
        stale = _boundary_bundle()
        stale["state"].update(
            {
                "is_stale": True,
                "artifact_status": "STALE",
                "effective_status": "ANALYSIS_BOUNDARY_REQUIRED",
                "derived_status": "ANALYSIS_BOUNDARY_REQUIRED",
                "current_structure_revision_id": "structure_" + "9" * 32,
                "current_evidence_revision_id": "evidence_" + "a" * 32,
            }
        )

        def save_rebuild(**kwargs):
            state = deepcopy(stale["state"])
            state.update(
                {
                    "is_stale": False,
                    "artifact_status": "CURRENT",
                    "effective_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                    "derived_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                    "current_structure_revision_id": STRUCTURE_ID,
                    "current_structure_payload_sha256": STRUCTURE_SHA,
                    "current_evidence_revision_id": EVIDENCE_ID,
                    "current_evidence_payload_sha256": EVIDENCE_SHA,
                    "current_boundary_revision_id": kwargs[
                        "boundary_revision"
                    ]["boundary_revision_id"],
                    "current_boundary_revision_number": 2,
                    "current_boundary_payload_sha256": kwargs[
                        "boundary_revision"
                    ]["revision_payload_sha256"],
                    "current_coverage_revision_id": kwargs[
                        "coverage_revision"
                    ]["coverage_revision_id"],
                    "current_coverage_revision_number": 2,
                    "current_coverage_payload_sha256": kwargs[
                        "coverage_revision"
                    ]["revision_payload_sha256"],
                }
            )
            return kwargs["boundary_revision"], kwargs["coverage_revision"], state

        with (
            patch.object(
                service,
                "_load_structure_input",
                return_value=(_public(), structure_bundle),
            ),
            patch.object(
                service.store,
                "load_current_analysis_boundary_bundle",
                return_value=stale,
            ),
            patch.object(
                service.store,
                "save_analysis_boundary_bundle_cas",
                side_effect=save_rebuild,
            ) as save,
        ):
            proposed = service.get_analysis_boundary(IMPORT_ID, LOGIN)
            edited = deepcopy(proposed["analysis_boundary"])
            edited["evaluation_objects"][0]["display_name"] += "（已复核）"
            rebuilt = service.save_analysis_boundary(
                IMPORT_ID,
                {
                    **_put_request(),
                    "evaluation_objects": edited["evaluation_objects"],
                    "source_scope_rules": edited["source_scope_rules"],
                    "label_scope_rules": edited["label_scope_rules"],
                },
                LOGIN,
            )

        self.assertEqual(save.call_args.kwargs["base_boundary_revision_id"], BOUNDARY_ID)
        self.assertEqual(save.call_args.kwargs["base_coverage_revision_id"], COVERAGE_ID)
        saved_boundary = save.call_args.kwargs["boundary_revision"]
        self.assertEqual(
            saved_boundary["analysis_boundary"]["source"][
                "structure_revision_id"
            ],
            STRUCTURE_ID,
        )
        self.assertEqual(rebuilt["status"], "ANALYSIS_BOUNDARY_REVIEW_REQUIRED")

    def test_put_response_loss_retry_reuses_same_head_but_changed_payload_conflicts(self):
        _snapshot, _mapping, _mapping_sha, core_result = (
            build_fixture_checkpoint()
        )
        structure_bundle = _structure_bundle()
        structure_bundle["structure_revision"]["structure"] = deepcopy(
            core_result["structure"]
        )
        structure_bundle["evidence_revision"]["evidence"] = deepcopy(
            core_result["evidence"]
        )
        holder: dict[str, dict] = {"bundle": None}

        def load_current(*_args):
            return holder["bundle"]

        def publish_once(**kwargs):
            state = {
                "project_id": PROJECT_ID,
                "import_id": IMPORT_ID,
                "current_structure_revision_id": STRUCTURE_ID,
                "current_structure_payload_sha256": STRUCTURE_SHA,
                "current_evidence_revision_id": EVIDENCE_ID,
                "current_evidence_payload_sha256": EVIDENCE_SHA,
                "current_boundary_revision_id": kwargs["boundary_revision"][
                    "boundary_revision_id"
                ],
                "current_boundary_revision_number": 1,
                "current_boundary_payload_sha256": kwargs["boundary_revision"][
                    "revision_payload_sha256"
                ],
                "current_coverage_revision_id": kwargs["coverage_revision"][
                    "coverage_revision_id"
                ],
                "current_coverage_revision_number": 1,
                "current_coverage_payload_sha256": kwargs["coverage_revision"][
                    "revision_payload_sha256"
                ],
                "current_request_fingerprint": kwargs["request_fingerprint"],
                "effective_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                "derived_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                "artifact_status": "CURRENT",
                "is_stale": False,
                "revision_history": [],
            }
            holder["bundle"] = {
                "state": state,
                "boundary_revision": kwargs["boundary_revision"],
                "coverage_revision": kwargs["coverage_revision"],
            }
            return kwargs["boundary_revision"], kwargs["coverage_revision"], state

        with (
            patch.object(
                service,
                "_load_structure_input",
                return_value=(_public(), structure_bundle),
            ),
            patch.object(
                service.store,
                "load_current_analysis_boundary_bundle",
                side_effect=load_current,
            ),
            patch.object(
                service.store,
                "save_analysis_boundary_bundle_cas",
                side_effect=publish_once,
            ) as save,
            patch.object(
                service,
                "_now",
                side_effect=[
                    "2026-08-20T00:00:00Z",
                    "2026-08-20T00:00:01Z",
                    "2026-08-20T00:00:02Z",
                ],
            ),
        ):
            proposed = service.get_analysis_boundary(IMPORT_ID, LOGIN)
            request = {
                **_put_request(with_current_heads=False),
                "evaluation_objects": deepcopy(
                    proposed["analysis_boundary"]["evaluation_objects"]
                ),
                "source_scope_rules": deepcopy(
                    proposed["analysis_boundary"]["source_scope_rules"]
                ),
                "label_scope_rules": deepcopy(
                    proposed["analysis_boundary"]["label_scope_rules"]
                ),
            }
            first = service.save_analysis_boundary(IMPORT_ID, request, LOGIN)
            retried = service.save_analysis_boundary(IMPORT_ID, request, LOGIN)
            changed = deepcopy(request)
            changed["evaluation_objects"][0]["display_name"] += "（不同请求）"
            with self.assertRaises(InterviewV2ImportError) as conflict:
                service.save_analysis_boundary(IMPORT_ID, changed, LOGIN)

        self.assertEqual(save.call_count, 1)
        self.assertEqual(
            retried["boundary_revision_id"], first["boundary_revision_id"]
        )
        self.assertEqual(
            retried["coverage_revision_id"], first["coverage_revision_id"]
        )
        self.assertEqual(retried["boundary_revision_number"], 1)
        self.assertEqual(retried["coverage_revision_number"], 1)
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(
            conflict.exception.code, "ANALYSIS_BOUNDARY_REVISION_CONFLICT"
        )

    def test_stale_replacement_response_loss_retry_reuses_new_head(self):
        _snapshot, _mapping, _mapping_sha, core_result = (
            build_fixture_checkpoint()
        )
        structure_bundle = _structure_bundle()
        structure_bundle["structure_revision"]["structure"] = deepcopy(
            core_result["structure"]
        )
        structure_bundle["evidence_revision"]["evidence"] = deepcopy(
            core_result["evidence"]
        )
        old_structure_id = "structure_" + "9" * 32
        old_evidence_id = "evidence_" + "a" * 32
        old_source = {
            "structure_revision_id": old_structure_id,
            "structure_payload_sha256": "1" * 64,
            "evidence_revision_id": old_evidence_id,
            "evidence_payload_sha256": "2" * 64,
        }
        stale = _boundary_bundle()
        stale["state"].update(
            {
                "is_stale": True,
                "artifact_status": "STALE",
                "effective_status": "ANALYSIS_BOUNDARY_REQUIRED",
                "derived_status": "ANALYSIS_BOUNDARY_REQUIRED",
                "current_structure_revision_id": old_structure_id,
                "current_structure_payload_sha256": "1" * 64,
                "current_evidence_revision_id": old_evidence_id,
                "current_evidence_payload_sha256": "2" * 64,
            }
        )
        stale["boundary_revision"]["source"] = deepcopy(old_source)
        stale["boundary_revision"]["analysis_boundary"]["source"].update(
            {
                "structure_revision_id": old_structure_id,
                "evidence_revision_id": old_evidence_id,
            }
        )
        holder = {"bundle": stale}

        def load_current(*_args):
            return holder["bundle"]

        def publish_replacement(**kwargs):
            old_history = {
                "revision_number": 1,
                "boundary_revision_id": BOUNDARY_ID,
                "coverage_revision_id": COVERAGE_ID,
                "source": deepcopy(old_source),
            }
            new_history = {
                "revision_number": 2,
                "boundary_revision_id": kwargs["boundary_revision"][
                    "boundary_revision_id"
                ],
                "coverage_revision_id": kwargs["coverage_revision"][
                    "coverage_revision_id"
                ],
                "source": deepcopy(kwargs["boundary_revision"]["source"]),
            }
            state = {
                "project_id": PROJECT_ID,
                "import_id": IMPORT_ID,
                "current_structure_revision_id": STRUCTURE_ID,
                "current_structure_payload_sha256": STRUCTURE_SHA,
                "current_evidence_revision_id": EVIDENCE_ID,
                "current_evidence_payload_sha256": EVIDENCE_SHA,
                "current_boundary_revision_id": kwargs["boundary_revision"][
                    "boundary_revision_id"
                ],
                "current_boundary_revision_number": 2,
                "current_boundary_payload_sha256": kwargs["boundary_revision"][
                    "revision_payload_sha256"
                ],
                "current_coverage_revision_id": kwargs["coverage_revision"][
                    "coverage_revision_id"
                ],
                "current_coverage_revision_number": 2,
                "current_coverage_payload_sha256": kwargs["coverage_revision"][
                    "revision_payload_sha256"
                ],
                "current_request_fingerprint": kwargs["request_fingerprint"],
                "effective_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                "derived_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                "artifact_status": "CURRENT",
                "is_stale": False,
                "revision_history": [old_history, new_history],
            }
            holder["bundle"] = {
                "state": state,
                "boundary_revision": kwargs["boundary_revision"],
                "coverage_revision": kwargs["coverage_revision"],
            }
            return kwargs["boundary_revision"], kwargs["coverage_revision"], state

        with (
            patch.object(
                service,
                "_load_structure_input",
                return_value=(_public(), structure_bundle),
            ),
            patch.object(
                service.store,
                "load_current_analysis_boundary_bundle",
                side_effect=load_current,
            ),
            patch.object(
                service.store,
                "load_analysis_boundary_revision",
                return_value=stale["boundary_revision"],
            ) as load_old_boundary,
            patch.object(
                service.store,
                "save_analysis_boundary_bundle_cas",
                side_effect=publish_replacement,
            ) as save,
            patch.object(
                service,
                "_now",
                side_effect=[
                    "2026-08-20T00:00:00Z",
                    "2026-08-20T00:00:01Z",
                ],
            ),
        ):
            proposed = service.get_analysis_boundary(IMPORT_ID, LOGIN)
            request = {
                **_put_request(),
                "evaluation_objects": deepcopy(
                    proposed["analysis_boundary"]["evaluation_objects"]
                ),
                "source_scope_rules": deepcopy(
                    proposed["analysis_boundary"]["source_scope_rules"]
                ),
                "label_scope_rules": deepcopy(
                    proposed["analysis_boundary"]["label_scope_rules"]
                ),
            }
            replacement = service.save_analysis_boundary(
                IMPORT_ID, request, LOGIN
            )
            retried = service.save_analysis_boundary(IMPORT_ID, request, LOGIN)

        self.assertEqual(save.call_count, 1)
        load_old_boundary.assert_called_once_with(PROJECT_ID, BOUNDARY_ID)
        self.assertEqual(
            retried["boundary_revision_id"],
            replacement["boundary_revision_id"],
        )
        self.assertEqual(
            retried["coverage_revision_id"],
            replacement["coverage_revision_id"],
        )
        self.assertEqual(retried["boundary_revision_number"], 2)
        self.assertEqual(retried["coverage_revision_number"], 2)
        self.assertEqual(
            holder["bundle"]["state"]["revision_history"][0]["source"],
            old_source,
        )
        self.assertEqual(
            holder["bundle"]["state"]["current_structure_revision_id"],
            STRUCTURE_ID,
        )
        self.assertEqual(
            holder["bundle"]["state"]["current_evidence_revision_id"],
            EVIDENCE_ID,
        )

    def test_confirm_rejects_advanced_upstream_before_core_or_writes(self):
        advanced_structure_id = "structure_" + "9" * 32
        advanced_evidence_id = "evidence_" + "a" * 32
        advanced_bundle = _structure_bundle()
        advanced_bundle["state"].update(
            {
                "current_structure_revision_id": advanced_structure_id,
                "current_structure_payload_sha256": "1" * 64,
                "current_evidence_revision_id": advanced_evidence_id,
                "current_evidence_payload_sha256": "2" * 64,
            }
        )
        draft = _boundary_bundle()
        draft["state"]["is_stale"] = True
        with (
            patch.object(
                service,
                "_load_structure_input",
                return_value=(_public(), advanced_bundle),
            ),
            patch.object(
                service.store,
                "load_current_analysis_boundary_bundle",
                return_value=draft,
            ),
            patch.object(
                service, "confirm_analysis_boundary_payload"
            ) as confirm_core,
            patch.object(
                service.store, "save_analysis_boundary_bundle_cas"
            ) as save,
            patch.object(
                service.store, "confirm_analysis_boundary_cas"
            ) as confirm_store,
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.confirm_analysis_boundary(
                    IMPORT_ID,
                    {
                        "boundary_revision_id": BOUNDARY_ID,
                        "coverage_revision_id": COVERAGE_ID,
                        "boundary_payload_sha256": BOUNDARY_SHA,
                        "coverage_payload_sha256": COVERAGE_SHA,
                    },
                    LOGIN,
                )

        error = caught.exception
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.code, "ANALYSIS_BOUNDARY_INPUT_CONFLICT")
        self.assertEqual(
            error.context,
            {
                "current_structure_revision_id": advanced_structure_id,
                "current_evidence_revision_id": advanced_evidence_id,
                "current_structure_status": "READY_FOR_DOSSIERS",
            },
        )
        confirm_core.assert_not_called()
        save.assert_not_called()
        confirm_store.assert_not_called()

    def test_core_validation_error_is_a_stable_422(self):
        core_error = InterviewV2AnalysisBoundaryError(
            "ANALYSIS_BOUNDARY_INVALID",
            "被测对象绑定无效。",
            {"evaluation_object_id": "evaluation_" + "9" * 32},
        )
        p1, p2, p3 = _service_patches(current=None)
        with (
            p1,
            p2,
            p3,
            patch.object(
                service,
                "build_analysis_boundary_proposal",
                return_value={
                    "analysis_boundary": _boundary(),
                    "coverage_preview": _coverage(),
                },
            ),
            patch.object(
                service, "validate_analysis_boundary", side_effect=core_error
            ),
            patch.object(service.store, "save_analysis_boundary_bundle_cas") as save,
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.save_analysis_boundary(
                    IMPORT_ID, _put_request(with_current_heads=False), LOGIN
                )

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.code, "ANALYSIS_BOUNDARY_INVALID")
        self.assertEqual(caught.exception.context, core_error.context)
        save.assert_not_called()

    def test_store_input_conflict_is_a_sanitized_409(self):
        conflict = service.store.AnalysisBoundaryInputConflictError(
            current_structure_revision_id="structure_" + "9" * 32,
            current_evidence_revision_id="evidence_" + "a" * 32,
            current_structure_status="STRUCTURE_REVIEW_REQUIRED",
        )
        p1, p2, p3 = _service_patches(current=None)
        with (
            p1,
            p2,
            p3,
            patch.object(
                service,
                "build_analysis_boundary_proposal",
                return_value={
                    "analysis_boundary": _boundary(),
                    "coverage_preview": _coverage(),
                },
            ),
            patch.object(
                service, "validate_analysis_boundary", return_value=_boundary()
            ),
            patch.object(
                service, "build_coverage_preview", return_value=_coverage()
            ),
            patch.object(
                service.store,
                "analysis_boundary_revision_payload_sha256",
                return_value=NEXT_BOUNDARY_SHA,
            ),
            patch.object(
                service.store,
                "coverage_revision_payload_sha256",
                return_value=NEXT_COVERAGE_SHA,
            ),
            patch.object(
                service.store,
                "save_analysis_boundary_bundle_cas",
                side_effect=conflict,
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.save_analysis_boundary(
                    IMPORT_ID, _put_request(with_current_heads=False), LOGIN
                )

        error = caught.exception
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.code, "ANALYSIS_BOUNDARY_INPUT_CONFLICT")
        self.assertEqual(
            error.context,
            {
                "current_structure_revision_id": "structure_" + "9" * 32,
                "current_evidence_revision_id": "evidence_" + "a" * 32,
                "current_structure_status": "STRUCTURE_REVIEW_REQUIRED",
            },
        )

    def test_unrelated_store_value_error_remains_a_500(self):
        p1, p2, p3 = _service_patches(current=None)
        with (
            p1,
            p2,
            p3,
            patch.object(
                service,
                "build_analysis_boundary_proposal",
                return_value={
                    "analysis_boundary": _boundary(),
                    "coverage_preview": _coverage(),
                },
            ),
            patch.object(
                service, "validate_analysis_boundary", return_value=_boundary()
            ),
            patch.object(
                service, "build_coverage_preview", return_value=_coverage()
            ),
            patch.object(
                service.store,
                "analysis_boundary_revision_payload_sha256",
                return_value=NEXT_BOUNDARY_SHA,
            ),
            patch.object(
                service.store,
                "coverage_revision_payload_sha256",
                return_value=NEXT_COVERAGE_SHA,
            ),
            patch.object(
                service.store,
                "save_analysis_boundary_bundle_cas",
                side_effect=ValueError("durable digest mismatch"),
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.save_analysis_boundary(
                    IMPORT_ID, _put_request(with_current_heads=False), LOGIN
                )

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(
            caught.exception.code, "ANALYSIS_BOUNDARY_PERSISTENCE_FAILED"
        )

    def test_public_response_allowlists_drop_audit_and_storage_fields(self):
        response = service._response_from_payloads(
            public=_public(),
            boundary=_boundary(internal=True),
            coverage=_coverage(internal=True),
        )

        serialized = repr(response)
        self.assertNotIn("owner@example.com", serialized)
        self.assertNotIn("private/boundary.json", serialized)
        self.assertNotIn("private/evidence.json", serialized)
        self.assertNotIn("created_by", serialized)
        InterviewV2AnalysisBoundaryResponse.model_validate(response)
        coverage_response = service.get_coverage_preview
        self.assertTrue(callable(coverage_response))
        InterviewV2CoveragePreviewResponse.model_validate(
            {
                key: response.get(key)
                for key in {
                    "import_id",
                    "project_id",
                    "status",
                    "structure_revision_id",
                    "evidence_revision_id",
                    "boundary_revision_id",
                    "boundary_payload_sha256",
                    "coverage_revision_id",
                    "coverage_payload_sha256",
                    "coverage_preview",
                    "is_stale",
                }
            }
        )


if __name__ == "__main__":
    unittest.main()
