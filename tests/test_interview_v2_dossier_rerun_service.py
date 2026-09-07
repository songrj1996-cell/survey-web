import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from app.services import interview_v2_dossier_rerun_service as service
from app.services.interview_v2_import_service import InterviewV2ImportError
from tests.test_interview_v2_dossier_service import (
    EV_BACKGROUND, EV_BODY, IMPORT, PARTICIPANT, PROJECT, ready_payload,
)


BASE = "dossier_" + "a" * 32
NEXT = "dossier_" + "b" * 32
RERUN = "rerun_" + "c" * 32
REQUEST = {
    "from_stage": "participant_dossier",
    "participant_id": PARTICIPANT,
    "base_dossier_version_id": BASE,
    "preserve_manual_report_edits": True,
    "reuse_unchanged_artifacts": True,
    "force": False,
}


def base_revision():
    return {
        "project_id": PROJECT, "participant_id": PARTICIPANT,
        "dossier_version_id": BASE, "revision_payload_sha256": "d" * 64,
        "version_number": 1, "import_id": IMPORT,
        "source": ready_payload()[4], "attributes": {}, "dossier": {},
        "status": "approved", "review": {"decision": "approved"},
    }


def outputs():
    return (
        '{"participant_id":"' + PARTICIPANT + '","facts":[{"candidate_id":"f1",'
        '"attribute_key":"frequency","raw_value":"每天","fact_source":"explicit_self_report",'
        '"evidence_ids":["' + EV_BACKGROUND + '"]}],"analytical_labels":[]}',
        '{"participant_id":"' + PARTICIPANT + '","claims":[{"claim_type":"behavior",'
        '"statement":"会使用该功能","supporting_evidence_ids":["' + EV_BODY + '"],'
        '"conflicting_evidence_ids":[]}],"contradictions":[],"missing_context":[]}',
    )


class DossierRerunServiceTests(unittest.IsolatedAsyncioTestCase):
    def common(self):
        base = base_revision()
        operation = {
            "project_id": PROJECT, "participant_id": PARTICIPANT,
            "base_dossier_version_id": BASE,
            "base_revision_payload_sha256": base["revision_payload_sha256"],
            "dossier_version_id": NEXT, "rerun_id": RERUN,
            "request_fingerprint": "e" * 64, "source": ready_payload()[4],
            "status": "pending", "_claim_acquired": True,
        }
        return base, operation

    def test_missing_base_is_not_found(self):
        with (
            patch.object(service.store, "load_project", return_value={"owner_key": "local:anonymous"}),
            patch.object(service, "_visible_to_owner", return_value=True),
            patch.object(service.store, "load_participant_dossier", return_value=None),
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                service._load_accessible_base(PROJECT, PARTICIPANT, BASE, None)
        self.assertEqual(404, raised.exception.status_code)
        self.assertEqual("INTERVIEW_DOSSIER_NOT_FOUND", raised.exception.code)

    async def test_two_stage_target_only_commit_and_no_approval_inheritance(self):
        base, operation = self.common()
        attribute, dossier = outputs()

        def claim(**kwargs):
            operation["request_fingerprint"] = kwargs["request_fingerprint"]
            operation["frozen_participant_input"] = kwargs["frozen_participant_input"]
            return operation

        def save(**kwargs):
            revision = {
                **kwargs["revision"], "project_id": PROJECT,
                "participant_id": PARTICIPANT, "version_number": 2,
                "revision_payload_sha256": "f" * 64,
            }
            return {
                "state": {"current_dossier_version_id": NEXT, "current_version_number": 2},
                "revision": revision,
                "operation": {**operation, "status": "completed"},
            }

        with (
            patch.object(service, "_load_accessible_base", return_value={"revision": base}),
            patch.object(service, "_ready_project", return_value=ready_payload()),
            patch.object(service.store, "load_dossier_rerun_operation", return_value=None),
            patch.object(service.store, "load_current_participant_dossier", return_value={
                "state": {"current_dossier_version_id": BASE}, "revision": base,
            }),
            patch.object(service.store, "claim_participant_dossier_rerun", side_effect=claim),
            patch.object(service.store, "save_participant_dossier_rerun_cas", side_effect=save) as persisted,
            patch.object(service, "collect_chat_completion", new=AsyncMock(
                side_effect=[(attribute, "attribute-model"), (dossier, "dossier-model")]
            )) as completion,
        ):
            result = await service.create_participant_dossier_rerun(
                PROJECT, REQUEST, {"email": "owner@example.com"}, "same-key"
            )

        self.assertEqual(2, completion.await_count)
        self.assertEqual("generated", result["status"])
        self.assertEqual({}, persisted.call_args.kwargs["revision"]["review"])
        frozen = persisted.call_args.kwargs["revision"]["rerun"]["frozen_participant_input"]
        self.assertEqual(PARTICIPANT, frozen["participant_id"])
        sent = "\n".join(call.args[0][1]["content"] for call in completion.await_args_list)
        self.assertIn(EV_BODY, sent)
        self.assertNotIn("participant_" + "9" * 32, sent)

    async def test_completed_replay_uses_frozen_input_without_model_call(self):
        base, operation = self.common()
        models = service._model_configuration()
        _, prompts = service._prompt_bundle()
        participant_input = {
            "participant_id": PARTICIPANT, "attribute_evidence": [],
            "dossier_evidence": [], "evidence_allowlist": [],
            "self_report_evidence_allowlist": [],
        }
        operation.update({
            "status": "completed", "frozen_participant_input": participant_input,
            "prompt_snapshot": prompts, "model_configuration": models,
        })
        operation["request_fingerprint"] = service._fingerprint(
            request=REQUEST, base=base, source=operation["source"],
            participant_input=participant_input, prompt_snapshot=prompts,
            model_configuration=models,
        )
        result_saved = {
            "state": {"current_dossier_version_id": NEXT, "current_version_number": 2},
            "revision": {
                **base, "dossier_version_id": NEXT, "version_number": 2,
                "revision_payload_sha256": "f" * 64, "status": "generated",
                "review": {}, "source": operation["source"],
            },
        }
        with (
            patch.object(service, "_load_accessible_base", return_value={"revision": base}),
            patch.object(service.store, "load_dossier_rerun_operation", return_value=operation),
            patch.object(service.store, "load_participant_dossier", return_value=result_saved),
            patch.object(service.store, "load_current_participant_dossier", return_value={
                "state": {"current_dossier_version_id": "dossier_" + "9" * 32},
                "revision": {"revision_payload_sha256": "1" * 64},
            }),
            patch.object(service, "collect_chat_completion", new=AsyncMock()) as completion,
        ):
            result = await service.create_participant_dossier_rerun(
                PROJECT, REQUEST, None, "same-key"
            )
        self.assertTrue(result["rerun"]["reused"])
        self.assertFalse(result["is_current_version"])
        completion.assert_not_awaited()

    async def test_failure_and_cancellation_release_without_commit(self):
        for effect in (RuntimeError("model down"), asyncio.CancelledError()):
            base, operation = self.common()

            def claim(**kwargs):
                operation["request_fingerprint"] = kwargs["request_fingerprint"]
                return operation

            with (
                patch.object(service, "_load_accessible_base", return_value={"revision": base}),
                patch.object(service, "_ready_project", return_value=ready_payload()),
                patch.object(service.store, "load_dossier_rerun_operation", return_value=None),
                patch.object(service.store, "load_current_participant_dossier", return_value={
                    "state": {"current_dossier_version_id": BASE}, "revision": base,
                }),
                patch.object(service.store, "claim_participant_dossier_rerun", side_effect=claim),
                patch.object(service.store, "release_participant_dossier_rerun", return_value=True) as released,
                patch.object(service, "collect_chat_completion", new=AsyncMock(side_effect=effect)),
            ):
                with self.assertRaises((InterviewV2ImportError, asyncio.CancelledError)):
                    await service.create_participant_dossier_rerun(
                        PROJECT, REQUEST, None, "same-key"
                    )
            released.assert_called_once()

    async def test_commit_idempotency_conflict_stays_409(self):
        base, operation = self.common()
        attribute, dossier = outputs()

        def claim(**kwargs):
            operation["request_fingerprint"] = kwargs["request_fingerprint"]
            return operation

        with (
            patch.object(service, "_load_accessible_base", return_value={"revision": base}),
            patch.object(service, "_ready_project", return_value=ready_payload()),
            patch.object(service.store, "load_dossier_rerun_operation", return_value=None),
            patch.object(service.store, "load_current_participant_dossier", return_value={
                "state": {"current_dossier_version_id": BASE}, "revision": base,
            }),
            patch.object(service.store, "claim_participant_dossier_rerun", side_effect=claim),
            patch.object(
                service.store, "save_participant_dossier_rerun_cas",
                side_effect=service.store.DossierRerunIdempotencyConflictError(),
            ),
            patch.object(service.store, "release_participant_dossier_rerun", return_value=True),
            patch.object(service, "collect_chat_completion", new=AsyncMock(
                side_effect=[(attribute, "attribute-model"), (dossier, "dossier-model")]
            )),
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                await service.create_participant_dossier_rerun(
                    PROJECT, REQUEST, None, "same-key"
                )
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual("RERUN_IDEMPOTENCY_CONFLICT", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
