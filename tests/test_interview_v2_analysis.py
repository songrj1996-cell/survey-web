import unittest

from app.core.interview_v2_analysis import (
    InterviewV2AnalysisValidationError,
    build_analysis_input,
    validate_module_findings,
)


PROJECT = "project_" + "1" * 32
P1 = "participant_" + "2" * 32
P2 = "participant_" + "3" * 32
MODULE = "module_" + "4" * 32
OBJECT = "evaluation_" + "5" * 32
QUESTION = "question_" + "6" * 32
EV1 = "ev_" + "7" * 32
EV1B = "ev_" + "8" * 32
EV2 = "ev_" + "9" * 32
OBS = "ev_" + "a" * 32
SOURCE = {
    "structure_revision_id": "structure_" + "b" * 32,
    "evidence_revision_id": "evidence_" + "c" * 32,
    "boundary_revision_id": "boundary_" + "d" * 32,
    "coverage_revision_id": "coverage_" + "e" * 32,
    "dossier_versions": [
        {"participant_id": P1, "dossier_version_id": "dossier_" + "1" * 32},
        {"participant_id": P2, "dossier_version_id": "dossier_" + "2" * 32},
    ],
}


def analysis_input():
    evidence = {
        "expected_participants": [
            {"participant_id": P1, "group_id": "group_" + "1" * 32},
            {"participant_id": P2, "group_id": "group_" + "1" * 32},
        ],
        "entries": [
            {"evidence_id": EV1, "participant_id": P1, "module_id": MODULE,
             "main_question_id": QUESTION, "sheet_id": "sheet", "row": 5,
             "inclusion_status": "included", "identity_decision_status": "confirmed",
             "evidence_type": "participant_self_report", "normalized_content": "支持证据一"},
            {"evidence_id": EV1B, "participant_id": P1, "module_id": MODULE,
             "main_question_id": QUESTION, "sheet_id": "sheet", "row": 6,
             "inclusion_status": "included", "identity_decision_status": "confirmed",
             "evidence_type": "participant_self_report", "normalized_content": "同玩家另一记录员证据"},
            {"evidence_id": EV2, "participant_id": P2, "module_id": MODULE,
             "main_question_id": QUESTION, "sheet_id": "sheet", "row": 7,
             "inclusion_status": "included", "identity_decision_status": "confirmed",
             "evidence_type": "participant_self_report", "normalized_content": "反例证据"},
            {"evidence_id": OBS, "participant_id": P2, "module_id": MODULE,
             "main_question_id": QUESTION, "sheet_id": "sheet", "row": 8,
             "inclusion_status": "included", "identity_decision_status": "confirmed",
             "evidence_type": "researcher_observation", "normalized_content": "观察证据"},
        ],
    }
    boundary = {
        "evaluation_objects": [{"evaluation_object_id": OBJECT, "module_id": MODULE,
                                 "main_question_ids": [QUESTION], "decision_status": "confirmed"}],
        "source_scope_rules": [{"sheet_id": "sheet", "start_row": 1, "end_row": 20,
                                 "scope_type": "interview_body"}],
        "label_scope_rules": [
            {"label_key": "allowed", "scope_mode": "selected_modules",
             "module_ids": [MODULE], "decision_status": "confirmed"},
            {"label_key": "disabled", "scope_mode": "disabled",
             "decision_status": "confirmed"},
        ],
    }
    rows = [
        {"participant_id": participant_id, "module_id": MODULE,
         "evaluation_object_id": OBJECT, "main_question_id": QUESTION,
         "asked_status": "asked", "applicability": "applicable",
         "review_status": "confirmed"}
        for participant_id in (P1, P2)
    ]
    coverage = {
        "rows": rows,
        "summaries": [{"module_id": MODULE, "evaluation_object_id": OBJECT,
                       "main_question_id": QUESTION, "denominator_reliable": True}],
    }
    dossiers = [
        {"participant_id": P1, "dossier_version_id": "dossier_" + "1" * 32,
         "status": "approved", "attributes": {
             "facts": [{"attribute_key": "frequency"}],
             "analytical_labels": [{"label_key": "allowed"}, {"label_key": "disabled"}],
         },
         "dossier": {"claims": [{"module_id": MODULE, "supporting_evidence_ids": [EV1, EV1B]}]}},
        {"participant_id": P2, "dossier_version_id": "dossier_" + "2" * 32,
         "status": "generated", "attributes": {},
         "dossier": {"claims": [{"module_id": MODULE, "supporting_evidence_ids": [EV2, OBS]}]}},
    ]
    return build_analysis_input(
        project_id=PROJECT, source=SOURCE, evidence_revision=evidence,
        analysis_boundary=boundary, coverage_revision=coverage,
        dossier_revisions=dossiers, unreviewed_participant_ids=[P2],
    )


class InterviewV2AnalysisCoreTests(unittest.TestCase):
    def test_analysis_labels_obey_confirmed_module_scope(self):
        module_input = analysis_input()["modules"][0]
        attributes = module_input["participant_dossiers"][0]["attributes"]
        self.assertEqual([{"attribute_key": "frequency"}], attributes["facts"])
        self.assertEqual(["allowed"], [item["label_key"] for item in attributes["analytical_labels"]])

    def test_multiple_recorders_for_one_participant_count_once(self):
        module_input = analysis_input()["modules"][0]
        result = validate_module_findings({
            "module_id": MODULE,
            "findings": [{
                "title": "发现", "statement": "一名玩家提供了两条支持记录。",
                "evaluation_object_id": OBJECT, "main_question_id": QUESTION,
                "supporting_cases": [{"participant_id": P1, "evidence_ids": [EV1, EV1B]}],
                "counterexample_cases": [{"participant_id": P2, "evidence_ids": [EV2]}],
                "observation_cases": [{"participant_id": P2, "evidence_ids": [OBS]}],
                "limitations": [], "confidence": 0.8,
            }],
        }, module_input=module_input, analysis_run_id="analysis_" + "f" * 32)
        stat = result["stat_facts"][0]
        self.assertEqual(1, stat["numerator"])
        self.assertEqual(2, stat["denominator"])
        self.assertEqual([P1], [item["participant_id"] for item in stat["numerator_cases"]])

    def test_cross_participant_evidence_is_rejected(self):
        module_input = analysis_input()["modules"][0]
        with self.assertRaises(InterviewV2AnalysisValidationError):
            validate_module_findings({"module_id": MODULE, "findings": [{
                "title": "错误", "statement": "错误引用",
                "supporting_cases": [{"participant_id": P1, "evidence_ids": [EV2]}],
            }]}, module_input=module_input, analysis_run_id="analysis_" + "f" * 32)

    def test_observation_cannot_be_counted_as_player_mention(self):
        module_input = analysis_input()["modules"][0]
        with self.assertRaises(InterviewV2AnalysisValidationError):
            validate_module_findings({"module_id": MODULE, "findings": [{
                "title": "错误", "statement": "观察冒充自述",
                "supporting_cases": [{"participant_id": P2, "evidence_ids": [OBS]}],
            }]}, module_input=module_input, analysis_run_id="analysis_" + "f" * 32)

    def test_unreliable_coverage_never_outputs_denominator_or_ratio(self):
        module_input = analysis_input()["modules"][0]
        module_input["coverage_summaries"][0]["denominator_reliable"] = False
        result = validate_module_findings({"module_id": MODULE, "findings": [{
            "title": "发现", "statement": "只输出绝对人数",
            "evaluation_object_id": OBJECT, "main_question_id": QUESTION,
            "supporting_cases": [{"participant_id": P1, "evidence_ids": [EV1]}],
        }]}, module_input=module_input, analysis_run_id="analysis_" + "f" * 32)
        self.assertIsNone(result["stat_facts"][0]["denominator"])
        self.assertIsNone(result["stat_facts"][0]["proportion"])


if __name__ == "__main__":
    unittest.main()
