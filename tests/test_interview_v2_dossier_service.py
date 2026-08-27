import unittest
from unittest.mock import AsyncMock, patch

from app.services import interview_v2_dossier_service as service


PROJECT = "project_" + "1" * 32
IMPORT = "import_" + "2" * 32
PARTICIPANT = "participant_" + "3" * 32
GROUP = "group_" + "4" * 32
EV_BACKGROUND = "ev_" + "5" * 32
EV_BODY = "ev_" + "6" * 32


def ready_payload():
    evidence = {
        "expected_participants": [{"participant_id": PARTICIPANT, "group_id": GROUP}],
        "entries": [
            {"evidence_id": EV_BACKGROUND, "participant_id": PARTICIPANT, "group_id": GROUP,
             "sheet_id": "sheet", "row": 2, "inclusion_status": "included",
             "identity_decision_status": "confirmed", "evidence_type": "participant_self_report", "normalized_content": "每天"},
            {"evidence_id": EV_BODY, "participant_id": PARTICIPANT, "group_id": GROUP,
             "sheet_id": "sheet", "row": 8, "inclusion_status": "included",
             "identity_decision_status": "confirmed", "evidence_type": "participant_self_report", "normalized_content": "会使用该功能"},
        ],
    }
    boundary = {"source_scope_rules": [
        {"sheet_id": "sheet", "start_row": 1, "end_row": 4, "scope_type": "participant_background", "decision_status": "confirmed"},
        {"sheet_id": "sheet", "start_row": 5, "end_row": 10, "scope_type": "interview_body", "decision_status": "confirmed"},
    ]}
    source = {"evidence_revision_id": "evidence_" + "7" * 32,
              "boundary_revision_id": "boundary_" + "8" * 32,
              "coverage_revision_id": "coverage_" + "9" * 32}
    return ({"project_id": PROJECT, "import_id": IMPORT, "status": "READY_FOR_DOSSIERS"},
            evidence, boundary, {}, source)


class DossierServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_regenerate_runs_two_constrained_stages_and_saves(self):
        attribute_json = (
            '{"participant_id":"' + PARTICIPANT + '","facts":[{"candidate_id":"f1",'
            '"attribute_key":"frequency","raw_value":"每天","fact_source":"explicit_self_report",'
            '"evidence_ids":["' + EV_BACKGROUND + '"]}],"analytical_labels":[]}'
        )
        dossier_json = (
            '{"participant_id":"' + PARTICIPANT + '","claims":[{"claim_type":"behavior",'
            '"statement":"会使用该功能","supporting_evidence_ids":["' + EV_BODY + '"],'
            '"conflicting_evidence_ids":[]}],"contradictions":[],"missing_context":[]}'
        )

        def save(**kwargs):
            revision = {**kwargs["revision"], "version_number": 1, "project_id": PROJECT,
                        "participant_id": PARTICIPANT, "revision_payload_sha256": "a" * 64}
            return {"state": {"current_version_number": 1,
                               "current_dossier_version_id": revision["dossier_version_id"]},
                    "revision": revision}

        with (
            patch.object(service, "_ready_project", return_value=ready_payload()),
            patch.object(service, "collect_chat_completion", new=AsyncMock(
                side_effect=[(attribute_json, "attribute-model"), (dossier_json, "dossier-model")]
            )) as completion,
            patch.object(service.store, "save_participant_dossier_cas", side_effect=save) as persisted,
        ):
            result = await service.regenerate_dossier(
                PROJECT, PARTICIPANT, {"base_dossier_version_id": None}, {"email": "owner@example.com"}
            )

        self.assertEqual("generated", result["status"])
        self.assertEqual(2, completion.await_count)
        self.assertEqual("attribute-model", result["model_usage"]["attribute_model"])
        self.assertEqual(PARTICIPANT, persisted.call_args.kwargs["participant_id"])


if __name__ == "__main__":
    unittest.main()
