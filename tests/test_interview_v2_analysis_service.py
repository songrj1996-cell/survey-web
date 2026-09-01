import unittest
from unittest.mock import AsyncMock, patch

from app.services import interview_v2_analysis_service as service
from app.services.interview_v2_import_service import InterviewV2ImportError


PROJECT = "project_" + "1" * 32
IMPORT = "import_" + "2" * 32
P1 = "participant_" + "3" * 32
P2 = "participant_" + "4" * 32
MODULE = "module_" + "5" * 32
OBJECT = "evaluation_" + "6" * 32
QUESTION = "question_" + "7" * 32
EV1 = "ev_" + "8" * 32
EV2 = "ev_" + "9" * 32
SOURCE = {
    "structure_revision_id": "structure_" + "a" * 32,
    "evidence_revision_id": "evidence_" + "b" * 32,
    "boundary_revision_id": "boundary_" + "c" * 32,
    "boundary_payload_sha256": "d" * 64,
    "coverage_revision_id": "coverage_" + "e" * 32,
    "coverage_payload_sha256": "f" * 64,
}


def ready_payload():
    evidence = {
        "expected_participants": [
            {"participant_id": P1, "group_id": "group_" + "1" * 32},
            {"participant_id": P2, "group_id": "group_" + "1" * 32},
        ],
        "entries": [
            {"evidence_id": EV1, "participant_id": P1, "module_id": MODULE,
             "main_question_id": QUESTION, "sheet_id": "sheet", "row": 5,
             "inclusion_status": "included", "identity_decision_status": "confirmed",
             "evidence_type": "participant_self_report", "normalized_content": "支持"},
            {"evidence_id": EV2, "participant_id": P2, "module_id": MODULE,
             "main_question_id": QUESTION, "sheet_id": "sheet", "row": 6,
             "inclusion_status": "included", "identity_decision_status": "confirmed",
             "evidence_type": "participant_self_report", "normalized_content": "反例"},
        ],
    }
    boundary = {
        "evaluation_objects": [{"evaluation_object_id": OBJECT, "module_id": MODULE,
                                 "main_question_ids": [QUESTION], "decision_status": "confirmed"}],
        "source_scope_rules": [{"sheet_id": "sheet", "start_row": 1, "end_row": 10,
                                 "scope_type": "interview_body"}],
    }
    coverage = {"coverage_preview": {
        "rows": [
            {"participant_id": pid, "module_id": MODULE, "evaluation_object_id": OBJECT,
             "main_question_id": QUESTION, "asked_status": "asked",
             "applicability": "applicable", "review_status": "confirmed"}
            for pid in (P1, P2)
        ],
        "summaries": [{"module_id": MODULE, "evaluation_object_id": OBJECT,
                       "main_question_id": QUESTION, "denominator_reliable": True}],
    }}
    return ({"project_id": PROJECT, "import_id": IMPORT, "status": "READY_FOR_DOSSIERS"},
            evidence, boundary, coverage, SOURCE)


def current_dossier(participant_id, status):
    evidence_id = EV1 if participant_id == P1 else EV2
    return {"state": {"current_dossier_version_id": "dossier_" + ("1" if participant_id == P1 else "2") * 32},
            "revision": {
                "participant_id": participant_id,
                "dossier_version_id": "dossier_" + ("1" if participant_id == P1 else "2") * 32,
                "revision_payload_sha256": ("3" if participant_id == P1 else "4") * 64,
                "source": SOURCE, "status": status, "attributes": {},
                "dossier": {"claims": [{"module_id": MODULE,
                                           "supporting_evidence_ids": [evidence_id]}]},
            }}


class InterviewV2AnalysisServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_current_analysis_is_marked_stale_when_dossier_head_moves(self):
        stored_source = {
            **SOURCE,
            "dossier_versions": [
                {"participant_id": P1, "dossier_version_id": "dossier_" + "1" * 32},
                {"participant_id": P2, "dossier_version_id": "dossier_" + "2" * 32},
            ],
        }
        current = {
            "state": {"current_version_number": 1},
            "revision": {"analysis_run_id": "analysis_" + "f" * 32,
                         "status": "completed", "source": stored_source},
        }

        def moved_dossier(_project_id, participant_id):
            dossier = current_dossier(participant_id, "approved")
            if participant_id == P2:
                dossier["state"]["current_dossier_version_id"] = "dossier_" + "a" * 32
            return dossier

        with (
            patch.object(service, "_ready_project", return_value=ready_payload()),
            patch.object(service.store, "load_current_analysis_run", return_value=current),
            patch.object(service.store, "load_current_participant_dossier", side_effect=moved_dossier),
        ):
            result = service.get_current_analysis(PROJECT, None)
        self.assertEqual("stale", result["status"])

    async def test_create_freezes_dossiers_validates_and_saves(self):
        model_json = (
            '{"module_id":"' + MODULE + '","findings":[{"title":"发现",'
            '"statement":"一名玩家支持，另一名玩家提供反例。",'
            '"evaluation_object_id":"' + OBJECT + '","main_question_id":"' + QUESTION + '",'
            '"supporting_cases":[{"participant_id":"' + P1 + '","evidence_ids":["' + EV1 + '"]}],'
            '"counterexample_cases":[{"participant_id":"' + P2 + '","evidence_ids":["' + EV2 + '"]}],'
            '"observation_cases":[],"limitations":[],"confidence":0.8}]}'
        )

        def load_dossier(_project_id, participant_id):
            return current_dossier(participant_id, "approved" if participant_id == P1 else "generated")

        def save(**kwargs):
            revision = {**kwargs["revision"], "version_number": 1,
                        "revision_payload_sha256": "5" * 64}
            return {"state": {"current_version_number": 1,
                               "current_analysis_run_id": revision["analysis_run_id"]},
                    "revision": revision}

        with (
            patch.object(service, "_ready_project", return_value=ready_payload()),
            patch.object(service.store, "load_current_participant_dossier", side_effect=load_dossier),
            patch.object(service, "collect_chat_completion", new=AsyncMock(
                return_value=(model_json, "analysis-model")
            )),
            patch.object(service.store, "save_analysis_run_cas", side_effect=save) as persisted,
        ):
            result = await service.create_analysis_run(
                PROJECT, {"base_analysis_run_id": None, "freeze_current": True},
                {"email": "owner@example.com"},
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(1, result["stat_facts"][0]["numerator"])
        self.assertEqual(2, result["stat_facts"][0]["denominator"])
        self.assertIn("尚未人工批准", result["limitations"][0])
        frozen = persisted.call_args.kwargs["revision"]["source"]["dossier_versions"]
        self.assertEqual([P1, P2], [item["participant_id"] for item in frozen])

    async def test_needs_changes_dossier_blocks_analysis_before_model_call(self):
        def load_dossier(_project_id, participant_id):
            return current_dossier(participant_id, "needs_changes" if participant_id == P2 else "approved")

        with (
            patch.object(service, "_ready_project", return_value=ready_payload()),
            patch.object(service.store, "load_current_participant_dossier", side_effect=load_dossier),
            patch.object(service, "collect_chat_completion", new=AsyncMock()) as completion,
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                await service.create_analysis_run(
                    PROJECT, {"base_analysis_run_id": None, "freeze_current": True}, None
                )
        self.assertEqual("ANALYSIS_INPUT_NOT_READY", caught.exception.code)
        completion.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
