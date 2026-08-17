from __future__ import annotations

from copy import deepcopy
import math
import unittest

from app.services.questionnaire_snapshot_history import (
    QuestionnaireSnapshotHistoryError,
    has_matching_snapshot_provenance,
    snapshot_history_fields,
    snapshot_history_summary,
)


def _snapshot_source(*, provider: str = "google_forms") -> dict:
    if provider == "google_forms":
        source_type = "google"
        source_mode = "official_api"
    else:
        source_type = "bested"
        source_mode = "original_questionnaire_upload"
    return {
        "questionnaire_used": True,
        "source_type": source_type,
        "questionnaire_sha256": "a" * 64,
        "questionnaire_input_kind": "saved_snapshot",
        "questionnaire_snapshot_ref": {
            "schema_version": 1,
            "snapshot_id": "qsn_0123456789abcdef01234567",
            "package_sha256": "a" * 64,
            "definition_sha256": "b" * 64,
            "provider": provider,
            "source_mode": source_mode,
            "mapping_status": "exact",
            "question_count": 2,
            "asset_count": 1,
            "asset_reference_count": 2,
        },
        "questionnaire_response_bindings": [
            {
                "question_id": "question-1",
                "column_indexes": [0],
                "mapping_method": (
                    "provider_response_key"
                    if provider == "google_forms"
                    else "bested_code_and_header"
                ),
                "mapping_status": "exact",
                "confidence": 1.0,
                "warning_codes": [],
            },
            {
                "question_id": "question-2",
                "column_indexes": [1, 2],
                "mapping_method": (
                    "declared_column_index+normalized_header_fallback"
                    if provider == "google_forms"
                    else "bested_code_and_matrix_headers"
                ),
                "mapping_status": "normalized",
                "confidence": 0.85,
                "warning_codes": ["normalized_header_fallback"],
            },
        ],
        "owner_ref": "must-not-copy",
        "raw": {"must": "not-copy"},
    }


class QuestionnaireSnapshotHistoryTests(unittest.TestCase):
    def assert_invalid(self, source: dict) -> None:
        with self.assertRaisesRegex(
            QuestionnaireSnapshotHistoryError,
            "^问卷快照历史来源无效$",
        ):
            snapshot_history_fields(source)
        self.assertEqual(
            snapshot_history_summary(source),
            {"questionnaire_snapshot_summary": None},
        )

    def test_history_fields_are_exact_whitelist_and_deep_copy_safe(self):
        source = _snapshot_source()
        fields = snapshot_history_fields(source)

        self.assertEqual(
            set(fields),
            {
                "questionnaire_input_kind",
                "questionnaire_snapshot_ref",
                "questionnaire_response_bindings",
            },
        )
        self.assertEqual(
            set(fields["questionnaire_snapshot_ref"]),
            {
                "schema_version",
                "snapshot_id",
                "package_sha256",
                "definition_sha256",
                "provider",
                "source_mode",
                "mapping_status",
                "question_count",
                "asset_count",
                "asset_reference_count",
            },
        )
        self.assertEqual(
            set(fields["questionnaire_response_bindings"][0]),
            {
                "question_id",
                "column_indexes",
                "mapping_method",
                "mapping_status",
                "confidence",
                "warning_codes",
            },
        )
        self.assertNotIn("owner_ref", fields)
        self.assertNotIn("raw", fields)

        fields["questionnaire_snapshot_ref"]["snapshot_id"] = "changed"
        fields["questionnaire_response_bindings"][0]["column_indexes"][0] = 99
        self.assertEqual(
            source["questionnaire_snapshot_ref"]["snapshot_id"],
            "qsn_0123456789abcdef01234567",
        )
        self.assertEqual(
            source["questionnaire_response_bindings"][0]["column_indexes"],
            [0],
        )

    def test_old_non_snapshot_records_and_safe_fallback_remain_compatible(self):
        self.assertEqual(snapshot_history_fields(None), {})
        self.assertEqual(snapshot_history_fields({"questionnaire_used": False}), {})

        fallback = _snapshot_source()
        primary = {
            "questionnaire_used": True,
            "source_type": "google",
            "questionnaire_sha256": "a" * 64,
        }
        selected = snapshot_history_fields(primary, fallback=fallback)
        fallback["questionnaire_snapshot_ref"]["provider"] = "mutated"
        self.assertEqual(
            selected["questionnaire_snapshot_ref"]["provider"],
            "google_forms",
        )
        self.assertEqual(snapshot_history_fields({}, fallback=_snapshot_source()), {})

        wrong_hash = dict(primary, questionnaire_sha256="c" * 64)
        self.assertEqual(
            snapshot_history_fields(wrong_hash, fallback=_snapshot_source()),
            {},
        )
        wrong_source = dict(primary, source_type="bested")
        self.assertEqual(
            snapshot_history_fields(wrong_source, fallback=_snapshot_source()),
            {},
        )

    def test_top_level_snapshot_mapping_statuses_are_preserved(self):
        for status in (
            "exact",
            "normalized",
            "partial",
            "needs_review",
            "unsupported",
            "source_missing",
        ):
            source = _snapshot_source()
            source["questionnaire_snapshot_ref"]["mapping_status"] = status
            with self.subTest(status=status):
                self.assertEqual(
                    snapshot_history_fields(source)["questionnaire_snapshot_ref"][
                        "mapping_status"
                    ],
                    status,
                )

    def test_explicit_bad_primary_never_uses_fallback(self):
        broken = _snapshot_source()
        broken["questionnaire_snapshot_ref"] = {}
        with self.assertRaises(QuestionnaireSnapshotHistoryError):
            snapshot_history_fields(broken, fallback=_snapshot_source())

    def test_extra_ref_or_binding_keys_fail_closed(self):
        extra_ref = _snapshot_source()
        extra_ref["questionnaire_snapshot_ref"]["owner_ref"] = "secret"
        self.assert_invalid(extra_ref)

        extra_binding = _snapshot_source()
        extra_binding["questionnaire_response_bindings"][0]["header"] = "raw"
        self.assert_invalid(extra_binding)

        missing_schema = _snapshot_source()
        missing_schema["questionnaire_snapshot_ref"].pop("schema_version")
        self.assert_invalid(missing_schema)

    def test_hash_schema_and_provider_relationships_are_strict(self):
        mutations = []
        bad_package = _snapshot_source()
        bad_package["questionnaire_snapshot_ref"]["package_sha256"] = "c" * 64
        mutations.append(bad_package)
        bad_definition = _snapshot_source()
        bad_definition["questionnaire_snapshot_ref"]["definition_sha256"] = "B" * 64
        mutations.append(bad_definition)
        bad_schema = _snapshot_source()
        bad_schema["questionnaire_snapshot_ref"]["schema_version"] = 2
        mutations.append(bad_schema)
        boolean_schema = _snapshot_source()
        boolean_schema["questionnaire_snapshot_ref"]["schema_version"] = True
        mutations.append(boolean_schema)
        float_schema = _snapshot_source()
        float_schema["questionnaire_snapshot_ref"]["schema_version"] = 1.0
        mutations.append(float_schema)
        bad_used = _snapshot_source()
        bad_used["questionnaire_used"] = 1
        mutations.append(bad_used)
        bad_source = _snapshot_source()
        bad_source["source_type"] = "bested"
        mutations.append(bad_source)
        bad_mode = _snapshot_source()
        bad_mode["questionnaire_snapshot_ref"]["source_mode"] = (
            "original_questionnaire_upload"
        )
        mutations.append(bad_mode)

        for source in mutations:
            with self.subTest(source=source):
                self.assert_invalid(source)

    def test_provider_and_binding_method_families_cannot_be_mixed(self):
        google_with_bested_method = _snapshot_source()
        google_with_bested_method["questionnaire_response_bindings"][0][
            "mapping_method"
        ] = "bested_code_and_header"
        self.assert_invalid(google_with_bested_method)

        bested_with_google_method = _snapshot_source(provider="bested")
        bested_with_google_method["questionnaire_response_bindings"][0][
            "mapping_method"
        ] = "provider_response_key"
        self.assert_invalid(bested_with_google_method)

    def test_counts_binding_limits_types_and_coverage_fail_closed(self):
        cases = []
        boolean_count = _snapshot_source()
        boolean_count["questionnaire_snapshot_ref"]["asset_count"] = True
        cases.append(boolean_count)
        float_count = _snapshot_source()
        float_count["questionnaire_snapshot_ref"]["question_count"] = 2.0
        cases.append(float_count)
        missing_binding = _snapshot_source()
        missing_binding["questionnaire_response_bindings"].pop()
        cases.append(missing_binding)
        empty_binding = _snapshot_source()
        empty_binding["questionnaire_response_bindings"] = []
        cases.append(empty_binding)
        too_many_questions = _snapshot_source()
        too_many_questions["questionnaire_snapshot_ref"]["question_count"] = 1_025
        cases.append(too_many_questions)
        too_many_assets = _snapshot_source()
        too_many_assets["questionnaire_snapshot_ref"]["asset_count"] = 4_097
        cases.append(too_many_assets)
        too_many_refs = _snapshot_source()
        too_many_refs["questionnaire_snapshot_ref"]["asset_reference_count"] = 16_385
        cases.append(too_many_refs)

        for source in cases:
            with self.subTest(source=source):
                self.assert_invalid(source)

    def test_duplicate_ids_indexes_and_non_finite_confidence_fail_closed(self):
        duplicate_id = _snapshot_source()
        duplicate_id["questionnaire_response_bindings"][1]["question_id"] = (
            "question-1"
        )
        self.assert_invalid(duplicate_id)

        duplicate_index = _snapshot_source()
        duplicate_index["questionnaire_response_bindings"][1]["column_indexes"] = (
            [0, 2]
        )
        self.assert_invalid(duplicate_index)

        duplicate_within = _snapshot_source()
        duplicate_within["questionnaire_response_bindings"][1]["column_indexes"] = (
            [1, 1]
        )
        self.assert_invalid(duplicate_within)

        for confidence in (math.nan, math.inf, -math.inf, True, "1.0"):
            source = _snapshot_source()
            source["questionnaire_response_bindings"][0]["confidence"] = confidence
            with self.subTest(confidence=confidence):
                self.assert_invalid(source)

    def test_summary_contains_only_approved_safe_counts(self):
        self.assertEqual(
            snapshot_history_summary(_snapshot_source()),
            {
                "questionnaire_snapshot_summary": {
                    "provider": "google_forms",
                    "question_count": 2,
                    "asset_count": 1,
                }
            },
        )
        self.assertEqual(
            snapshot_history_summary({}),
            {"questionnaire_snapshot_summary": None},
        )

    def test_matching_provenance_requires_symmetric_valid_exact_payload(self):
        left = _snapshot_source()
        right = deepcopy(left)
        self.assertTrue(has_matching_snapshot_provenance(left, right))
        self.assertTrue(has_matching_snapshot_provenance({}, {}))
        self.assertFalse(has_matching_snapshot_provenance(left, {}))

        changed_package = deepcopy(right)
        changed_package["questionnaire_sha256"] = "c" * 64
        changed_package["questionnaire_snapshot_ref"]["package_sha256"] = "c" * 64
        self.assertFalse(
            has_matching_snapshot_provenance(left, changed_package)
        )

        malformed = deepcopy(right)
        malformed["questionnaire_response_bindings"][0]["confidence"] = math.nan
        self.assertFalse(has_matching_snapshot_provenance(left, malformed))

    def test_bested_provider_contract_is_supported(self):
        fields = snapshot_history_fields(_snapshot_source(provider="bested"))
        self.assertEqual(
            fields["questionnaire_snapshot_ref"]["source_mode"],
            "original_questionnaire_upload",
        )


if __name__ == "__main__":
    unittest.main()
