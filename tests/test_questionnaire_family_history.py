from copy import deepcopy
import unittest
from unittest.mock import patch

from app.services.questionnaire_family_history import (
    QuestionnaireFamilyHistoryError,
    family_history_fields,
    family_history_summary,
    has_matching_family_provenance,
)
from app.services.history_service import get_history_entry, get_history_list


FINGERPRINT = "a" * 64


def payload() -> dict:
    return {
        "source_type": "google",
        "questionnaire_used": True,
        "questionnaire_sha256": FINGERPRINT,
        "questionnaire_family_input_kind": "google_forms_family",
        "questionnaire_family_ref": {
            "schema_version": 1,
            "family_id": "fam_test",
            "mapping_fingerprint": FINGERPRINT,
            "languages": ["en", "id"],
            "variant_count": 2,
            "canonical_question_count": 30,
            "duplicate_response_count": 1,
            "unmatched_answer_count": 0,
            "file_upload_answer_count": 2,
        },
    }


class QuestionnaireFamilyHistoryTests(unittest.TestCase):
    def test_fields_round_trip_and_summary_omits_ids_and_hashes(self):
        source = payload()
        fields = family_history_fields(source)
        self.assertEqual(fields["questionnaire_family_ref"]["languages"], ["en", "id"])
        summary = family_history_summary(source)["questionnaire_family_summary"]
        self.assertEqual(summary["variant_count"], 2)
        self.assertEqual(summary["canonical_question_count"], 30)
        self.assertNotIn("family_id", summary)
        self.assertNotIn("mapping_fingerprint", summary)

    def test_raw_response_provenance_is_never_archived(self):
        source = payload()
        source["google_forms_response_provenance"] = [{"original_values": ["PII"]}]
        fields = family_history_fields(source)
        self.assertNotIn("google_forms_response_provenance", fields)

    def test_malformed_or_asymmetric_provenance_fails_closed(self):
        source = payload()
        malformed = deepcopy(source)
        malformed["questionnaire_sha256"] = "b" * 64
        with self.assertRaises(QuestionnaireFamilyHistoryError):
            family_history_fields(malformed)
        self.assertFalse(has_matching_family_provenance(source, malformed))
        self.assertFalse(has_matching_family_provenance(source, {}))
        self.assertTrue(has_matching_family_provenance({}, {}))

    def test_history_exposes_safe_summary_but_not_raw_response_provenance(self):
        source = payload()
        source.update({
            "id": "history-1",
            "filename": "google-family.json",
            "title": "Family report",
            "created_at": "2026-08-25T00:00:00",
            "owner_key": "email:owner@example.test",
            "report_md": "# Family report",
            "qa_messages": [],
            "rows_fed": False,
            "mode": "",
            "row_count": 10,
            "google_forms_response_provenance": [{"original_values": ["PII"]}],
        })
        login = {"email": "owner@example.test"}
        with patch(
            "app.services.history_service._load_history_with_report_numbers",
            return_value=[source],
        ):
            listed = get_history_list(login)
            detail = get_history_entry("history-1", login)
        self.assertEqual(listed[0]["questionnaire_family_summary"]["languages"], ["en", "id"])
        self.assertNotIn("family_id", listed[0]["questionnaire_family_summary"])
        self.assertNotIn("google_forms_response_provenance", listed[0])
        self.assertIsNotNone(detail)
        self.assertNotIn("google_forms_response_provenance", detail)
        self.assertEqual(
            detail["questionnaire_family_ref"]["mapping_fingerprint"],
            FINGERPRINT,
        )


if __name__ == "__main__":
    unittest.main()
