import unittest

from app.core.interview_v2_dossier import (
    InterviewV2DossierValidationError,
    build_participant_input,
    validate_attribute_output,
    validate_dossier_output,
)


P1 = "participant_" + "1" * 32
P2 = "participant_" + "2" * 32
G1 = "group_" + "a" * 32
EV1 = "ev_" + "1" * 32
EV2 = "ev_" + "2" * 32
EV3 = "ev_" + "3" * 32


def evidence_revision():
    return {
        "expected_participants": [
            {"participant_id": P1, "group_id": G1},
            {"participant_id": P2, "group_id": G1},
        ],
        "entries": [
            {"evidence_id": EV1, "participant_id": P1, "group_id": G1,
             "sheet_id": "sheet_1", "row": 2, "inclusion_status": "included",
             "identity_decision_status": "system_verified", "evidence_type": "participant_self_report",
             "normalized_content": "我每天都会玩"},
            {"evidence_id": EV2, "participant_id": P1, "group_id": G1,
             "sheet_id": "sheet_1", "row": 8, "inclusion_status": "included",
             "identity_decision_status": "confirmed", "evidence_type": "participant_self_report",
             "normalized_content": "我会使用这个功能"},
            {"evidence_id": EV3, "participant_id": P2, "group_id": G1,
             "sheet_id": "sheet_1", "row": 8, "inclusion_status": "included",
             "identity_decision_status": "confirmed", "evidence_type": "participant_self_report",
             "normalized_content": "其他玩家内容"},
        ],
    }


BOUNDARY = {"source_scope_rules": [
    {"sheet_id": "sheet_1", "start_row": 1, "end_row": 4,
     "scope_type": "participant_background", "decision_status": "confirmed", "display_order": 1},
    {"sheet_id": "sheet_1", "start_row": 5, "end_row": 20,
     "scope_type": "interview_body", "decision_status": "confirmed", "display_order": 2},
]}


class DossierCoreTests(unittest.TestCase):
    def test_input_is_scoped_and_never_contains_other_participant(self):
        result = build_participant_input(
            participant_id=P1, evidence_revision=evidence_revision(), analysis_boundary=BOUNDARY
        )
        self.assertEqual([EV1], [item["evidence_id"] for item in result["attribute_evidence"]])
        self.assertEqual([EV2], [item["evidence_id"] for item in result["dossier_evidence"]])
        self.assertNotIn(EV3, result["evidence_allowlist"])

    def test_attribute_facts_and_labels_remain_separate(self):
        participant_input = build_participant_input(
            participant_id=P1, evidence_revision=evidence_revision(), analysis_boundary=BOUNDARY
        )
        result = validate_attribute_output({
            "participant_id": P1,
            "facts": [{"candidate_id": "f1", "attribute_key": "play_frequency",
                       "attribute_label": "游戏频率", "raw_value": "每天",
                       "fact_source": "explicit_self_report", "evidence_ids": [EV1], "confidence": .9}],
            "analytical_labels": [{"label_key": "high_frequency", "label": "高频玩家",
                                   "source_fact_candidate_ids": ["f1"], "evidence_ids": [EV1], "confidence": .8}],
        }, participant_input=participant_input)
        self.assertEqual(1, len(result["facts"]))
        self.assertEqual(result["facts"][0]["attribute_fact_id"], result["analytical_labels"][0]["source_fact_ids"][0])

    def test_sensitive_attribute_requires_explicit_self_report(self):
        participant_input = build_participant_input(
            participant_id=P1, evidence_revision=evidence_revision(), analysis_boundary=BOUNDARY
        )
        with self.assertRaises(InterviewV2DossierValidationError):
            validate_attribute_output({"participant_id": P1, "facts": [{
                "attribute_key": "gender", "raw_value": "男", "fact_source": "researcher_recorded_fact",
                "evidence_ids": [EV1]
            }]}, participant_input=participant_input)

    def test_dossier_rejects_cross_participant_evidence(self):
        participant_input = build_participant_input(
            participant_id=P1, evidence_revision=evidence_revision(), analysis_boundary=BOUNDARY
        )
        with self.assertRaises(InterviewV2DossierValidationError):
            validate_dossier_output({"participant_id": P1, "claims": [{
                "claim_type": "behavior", "statement": "会使用功能",
                "supporting_evidence_ids": [EV3]
            }]}, participant_input=participant_input)


if __name__ == "__main__":
    unittest.main()
