from copy import deepcopy
import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from app.core.interview_v2_analysis_boundary import (
    ANALYSIS_BOUNDARY_SCHEMA_VERSION,
    COVERAGE_SCHEMA_VERSION,
    InterviewV2AnalysisBoundaryError,
    build_analysis_boundary_proposal,
    build_coverage_preview,
    canonical_json_sha256,
    confirm_analysis_boundary,
    validate_analysis_boundary,
)
from app.schemas.interview_v2_analysis_boundary import (
    InterviewV2AnalysisBoundaryConfirmRequest,
    InterviewV2AnalysisBoundaryPayloadResponse,
    InterviewV2AnalysisBoundaryPutRequest,
    InterviewV2LabelScopeRuleRequest,
)
from tests.test_interview_v2_evidence import (
    build_fixture_checkpoint,
    two_participant_bundle,
)
from app.core.interview_v2_evidence import build_structure_and_evidence
from tests.test_interview_v2_structure import (
    IMPORT_ID,
    MAPPING_ID,
    PROJECT_ID,
    WORKBOOK_ID,
)


STRUCTURE_ID = "structure_" + "8" * 32
EVIDENCE_ID = "evidence_" + "9" * 32
FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "interview_v2"
    / "analysis_boundary"
    / "golden_boundary.json"
)


def _checkpoint():
    _snapshot, _mapping, _mapping_sha, result = build_fixture_checkpoint()
    return result["structure"], result["evidence"]


def _proposal(structure=None, evidence=None):
    if structure is None or evidence is None:
        structure, evidence = _checkpoint()
    return build_analysis_boundary_proposal(
        structure,
        evidence,
        project_id=PROJECT_ID,
        import_id=IMPORT_ID,
        structure_revision_id=STRUCTURE_ID,
        evidence_revision_id=EVIDENCE_ID,
    )


class InterviewV2AnalysisBoundaryTests(unittest.TestCase):
    def test_proposal_is_deterministic_non_mutating_and_matches_golden_contract(self):
        structure, evidence = _checkpoint()
        before_structure = deepcopy(structure)
        before_evidence = deepcopy(evidence)

        first = _proposal(structure, evidence)
        second = _proposal(structure, evidence)
        golden = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["expected"]

        self.assertEqual(first, second)
        self.assertEqual(structure, before_structure)
        self.assertEqual(evidence, before_evidence)
        boundary = first["analysis_boundary"]
        coverage = first["coverage_preview"]
        self.assertEqual(
            boundary["analysis_boundary_schema_version"],
            ANALYSIS_BOUNDARY_SCHEMA_VERSION,
        )
        self.assertEqual(coverage["coverage_schema_version"], COVERAGE_SCHEMA_VERSION)
        self.assertEqual(boundary["status"], golden["initial_boundary_status"])
        self.assertEqual(
            len(boundary["evaluation_objects"]),
            golden["evaluation_object_count"],
        )
        self.assertEqual(
            len(boundary["source_scope_rules"]),
            golden["source_scope_rule_count"],
        )
        self.assertTrue(
            all(
                item["scope_type"] == golden["initial_scope_type"]
                for item in boundary["source_scope_rules"]
            )
        )
        self.assertEqual(
            sum(
                len(item["allowed_split_rows"])
                for item in boundary["source_scope_rules"]
            ),
            golden["source_scope_allowed_split_count"],
        )
        self.assertEqual(
            len(boundary["label_scope_rules"]),
            golden["label_scope_rule_count"],
        )
        self.assertEqual(coverage["participant_count"], golden["participant_count"])
        self.assertEqual(coverage["row_count"], golden["coverage_row_count"])
        self.assertEqual(
            coverage["source"]["analysis_boundary_sha256"],
            canonical_json_sha256(boundary),
        )

    def test_each_proposal_object_has_stable_explicit_question_and_occurrence_bindings(self):
        structure, _evidence = _checkpoint()
        boundary = _proposal()["analysis_boundary"]
        question_by_id = {
            item["main_question_id"]: item
            for item in structure["main_questions"]
        }

        self.assertEqual(
            {
                question_id
                for item in boundary["evaluation_objects"]
                for question_id in item["main_question_ids"]
            },
            set(question_by_id),
        )
        for item in boundary["evaluation_objects"]:
            self.assertEqual(item["object_type"], "concept")
            self.assertIsNone(item["parent_evaluation_object_id"])
            self.assertEqual(len(item["main_question_ids"]), 1)
            question = question_by_id[item["main_question_ids"][0]]
            self.assertEqual(item["module_id"], question["module_id"])
            self.assertEqual(item["occurrence_ids"], sorted(question["occurrence_ids"]))

    def test_rename_and_order_changes_preserve_existing_object_identity(self):
        structure, evidence = _checkpoint()
        base = _proposal(structure, evidence)["analysis_boundary"]
        boundary = deepcopy(base)
        target = boundary["evaluation_objects"][0]
        object_id = target["evaluation_object_id"]
        target["display_name"] = "重命名后的方案"
        target["display_order"] = 99
        target["decision_status"] = "draft"
        target["decision_source"] = "user_selection"

        normalized = validate_analysis_boundary(
            boundary,
            structure,
            evidence,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            structure_revision_id=STRUCTURE_ID,
            evidence_revision_id=EVIDENCE_ID,
            base_boundary=base,
        )

        renamed = next(
            item
            for item in normalized["evaluation_objects"]
            if item["evaluation_object_id"] == object_id
        )
        self.assertEqual(renamed["display_name"], "重命名后的方案")
        self.assertEqual(renamed["display_order"], 99)

    def test_reusing_existing_id_for_changed_binding_is_rejected(self):
        structure, evidence = _checkpoint()
        base = _proposal(structure, evidence)["analysis_boundary"]
        boundary = deepcopy(base)
        target = max(
            boundary["evaluation_objects"],
            key=lambda item: len(item["occurrence_ids"]),
        )
        target["occurrence_ids"] = target["occurrence_ids"][:-1]

        with self.assertRaises(InterviewV2AnalysisBoundaryError) as context:
            validate_analysis_boundary(
                boundary,
                structure,
                evidence,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                structure_revision_id=STRUCTURE_ID,
                evidence_revision_id=EVIDENCE_ID,
                base_boundary=base,
            )
        self.assertEqual(context.exception.code, "EVALUATION_OBJECT_IDENTITY_REUSE")

    def test_split_creates_new_ids_partitions_occurrences_and_preserves_lineage(self):
        structure, evidence = _checkpoint()
        base = _proposal(structure, evidence)["analysis_boundary"]
        boundary = deepcopy(base)
        old = max(
            boundary["evaluation_objects"],
            key=lambda item: len(item["occurrence_ids"]),
        )
        old["decision_status"] = "superseded"
        concept_id = "evaluation_" + "a" * 32
        variant_id = "evaluation_" + "b" * 32
        split_at = len(old["occurrence_ids"]) // 2
        concept = {
            **old,
            "evaluation_object_id": concept_id,
            "display_name": "拆分后的方案组",
            "decision_status": "draft",
            "decision_source": "user_selection",
            "occurrence_ids": old["occurrence_ids"][:split_at],
            "supersedes_evaluation_object_ids": [old["evaluation_object_id"]],
        }
        variant = {
            **concept,
            "evaluation_object_id": variant_id,
            "parent_evaluation_object_id": concept_id,
            "object_type": "variant",
            "display_name": "方案组 Variant A",
            "display_order": 1,
            "occurrence_ids": old["occurrence_ids"][split_at:],
            "supersedes_evaluation_object_ids": [old["evaluation_object_id"]],
        }
        boundary["evaluation_objects"].extend([concept, variant])

        normalized = validate_analysis_boundary(
            boundary,
            structure,
            evidence,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            structure_revision_id=STRUCTURE_ID,
            evidence_revision_id=EVIDENCE_ID,
            base_boundary=base,
        )

        by_id = {
            item["evaluation_object_id"]: item
            for item in normalized["evaluation_objects"]
        }
        self.assertEqual(
            by_id[concept_id]["supersedes_evaluation_object_ids"],
            [old["evaluation_object_id"]],
        )
        self.assertEqual(by_id[variant_id]["parent_evaluation_object_id"], concept_id)
        self.assertEqual(
            set(by_id[concept_id]["occurrence_ids"])
            | set(by_id[variant_id]["occurrence_ids"]),
            set(old["occurrence_ids"]),
        )

    def test_merge_creates_new_id_and_supersedes_both_base_objects(self):
        structure, evidence = _checkpoint()
        base = _proposal(structure, evidence)["analysis_boundary"]
        boundary = deepcopy(base)
        by_module = {}
        for item in boundary["evaluation_objects"]:
            by_module.setdefault(item["module_id"], []).append(item)
        first, second = next(items for items in by_module.values() if len(items) >= 2)[:2]
        first["decision_status"] = "superseded"
        second["decision_status"] = "superseded"
        merged_id = "evaluation_" + "c" * 32
        boundary["evaluation_objects"].append(
            {
                **first,
                "evaluation_object_id": merged_id,
                "display_name": "合并后的方案",
                "main_question_ids": sorted(
                    set(first["main_question_ids"] + second["main_question_ids"])
                ),
                "occurrence_ids": sorted(
                    set(first["occurrence_ids"] + second["occurrence_ids"])
                ),
                "supersedes_evaluation_object_ids": sorted(
                    [first["evaluation_object_id"], second["evaluation_object_id"]]
                ),
                "decision_status": "draft",
                "decision_source": "user_selection",
            }
        )

        normalized = validate_analysis_boundary(
            boundary,
            structure,
            evidence,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            structure_revision_id=STRUCTURE_ID,
            evidence_revision_id=EVIDENCE_ID,
            base_boundary=base,
        )
        merged = next(
            item
            for item in normalized["evaluation_objects"]
            if item["evaluation_object_id"] == merged_id
        )
        self.assertEqual(
            merged["supersedes_evaluation_object_ids"],
            sorted([first["evaluation_object_id"], second["evaluation_object_id"]]),
        )

    def test_existing_object_cannot_be_deleted_even_when_new_object_references_it(self):
        structure, evidence = _checkpoint()
        base = _proposal(structure, evidence)["analysis_boundary"]
        boundary = deepcopy(base)
        removed = boundary["evaluation_objects"].pop(0)
        replacement = {
            **removed,
            "evaluation_object_id": "evaluation_" + "d" * 32,
            "supersedes_evaluation_object_ids": [removed["evaluation_object_id"]],
            "decision_status": "draft",
            "decision_source": "user_selection",
        }
        boundary["evaluation_objects"].append(replacement)

        with self.assertRaises(InterviewV2AnalysisBoundaryError) as context:
            validate_analysis_boundary(
                boundary,
                structure,
                evidence,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                structure_revision_id=STRUCTURE_ID,
                evidence_revision_id=EVIDENCE_ID,
                base_boundary=base,
            )
        self.assertEqual(context.exception.code, "EVALUATION_OBJECT_LINEAGE_INVALID")

    def test_source_ranges_must_be_ordered_and_non_overlapping(self):
        structure, evidence = _checkpoint()
        boundary = _proposal(structure, evidence)["analysis_boundary"]
        original = boundary["source_scope_rules"][0]
        boundary["source_scope_rules"].append(
            {
                **original,
                "source_scope_rule_id": "scope_" + "c" * 32,
            }
        )

        with self.assertRaises(InterviewV2AnalysisBoundaryError) as context:
            validate_analysis_boundary(
                boundary,
                structure,
                evidence,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                structure_revision_id=STRUCTURE_ID,
                evidence_revision_id=EVIDENCE_ID,
            )
        self.assertEqual(context.exception.code, "SOURCE_SCOPE_OVERLAP")

    def test_source_split_must_be_contiguous_and_use_derived_safe_boundary(self):
        structure, evidence = _checkpoint()
        boundary = _proposal(structure, evidence)["analysis_boundary"]
        original = next(
            rule
            for rule in boundary["source_scope_rules"]
            if rule["sheet_id"] == "sheet_001"
        )
        split_row = 8
        first = {
            **original,
            "source_scope_rule_id": "scope_" + "d" * 32,
            "end_row": split_row - 1,
            "scope_type": "participant_background",
            "allowed_split_rows": [999],
        }
        second = {
            **original,
            "source_scope_rule_id": "scope_" + "e" * 32,
            "start_row": split_row,
            "scope_type": "interview_body",
            "allowed_split_rows": [998],
        }
        boundary["source_scope_rules"] = [
            rule
            for rule in boundary["source_scope_rules"]
            if rule["source_scope_rule_id"] != original["source_scope_rule_id"]
        ] + [first, second]

        normalized = validate_analysis_boundary(
            boundary,
            structure,
            evidence,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            structure_revision_id=STRUCTURE_ID,
            evidence_revision_id=EVIDENCE_ID,
        )
        split_rules = [
            rule
            for rule in normalized["source_scope_rules"]
            if rule["sheet_id"] == "sheet_001"
        ]
        self.assertEqual(
            [(rule["start_row"], rule["end_row"]) for rule in split_rules],
            [(2, 7), (8, 13)],
        )
        self.assertNotIn(998, split_rules[1]["allowed_split_rows"])
        self.assertNotIn(999, split_rules[0]["allowed_split_rows"])

        unsafe = deepcopy(boundary)
        unsafe_rules = [
            rule for rule in unsafe["source_scope_rules"] if rule["sheet_id"] == "sheet_001"
        ]
        unsafe_rules[0]["end_row"] = 6
        unsafe_rules[1]["start_row"] = 7
        with self.assertRaises(InterviewV2AnalysisBoundaryError) as context:
            validate_analysis_boundary(
                unsafe,
                structure,
                evidence,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                structure_revision_id=STRUCTURE_ID,
                evidence_revision_id=EVIDENCE_ID,
            )
        self.assertEqual(context.exception.code, "SOURCE_SCOPE_SPLIT_UNSAFE")

        gap = deepcopy(boundary)
        gap_rules = [
            rule for rule in gap["source_scope_rules"] if rule["sheet_id"] == "sheet_001"
        ]
        gap_rules[0]["end_row"] = 6
        gap_rules[1]["start_row"] = 8
        with self.assertRaises(InterviewV2AnalysisBoundaryError) as context:
            validate_analysis_boundary(
                gap,
                structure,
                evidence,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                structure_revision_id=STRUCTURE_ID,
                evidence_revision_id=EVIDENCE_ID,
            )
        self.assertEqual(context.exception.code, "SOURCE_SCOPE_GAP")

    def test_background_and_excluded_ranges_do_not_enter_coverage(self):
        structure, evidence = _checkpoint()
        proposed = _proposal(structure, evidence)
        boundary = proposed["analysis_boundary"]
        for index, rule in enumerate(boundary["source_scope_rules"]):
            rule["scope_type"] = (
                "participant_background" if index % 2 == 0 else "excluded"
            )

        coverage = build_coverage_preview(structure, evidence, boundary)

        self.assertEqual(coverage["participant_count"], 2)
        self.assertEqual(coverage["row_count"], 0)
        self.assertEqual(coverage["rows"], [])
        self.assertEqual(coverage["summaries"], [])

    def test_partial_group_exclusion_removes_only_that_groups_coverage_rows(self):
        structure, evidence = _checkpoint()
        group_a = evidence["expected_participants"][0]["group_id"]
        group_b = "group_" + "a" * 32
        participant_a = evidence["expected_participants"][0]["participant_id"]
        participant_b = evidence["expected_participants"][1]["participant_id"]
        evidence["expected_participants"][1]["group_id"] = group_b
        for entry in evidence["entries"]:
            if entry["participant_id"] == participant_b:
                entry["group_id"] = group_b
        evidence["entries"] = [
            entry
            for entry in evidence["entries"]
            if not (
                entry["participant_id"] == participant_a
                and entry["sheet_id"] == "sheet_002"
            )
            and not (
                entry["participant_id"] == participant_b
                and entry["sheet_id"] != "sheet_002"
            )
        ]
        for occurrence in structure["occurrences"]:
            occurrence["group_id"] = (
                group_b if occurrence["sheet_id"] == "sheet_002" else group_a
            )
        proposal = _proposal(structure, evidence)
        boundary = proposal["analysis_boundary"]
        for rule in boundary["source_scope_rules"]:
            rule["scope_type"] = (
                "interview_body"
                if rule["sheet_id"] == "sheet_001"
                else "excluded"
            )

        coverage = build_coverage_preview(structure, evidence, boundary)

        self.assertEqual(coverage["participant_count"], 2)
        self.assertGreater(coverage["row_count"], 0)
        self.assertEqual(
            {row["participant_id"] for row in coverage["rows"]},
            {participant_a},
        )
        self.assertTrue(
            all(summary["participant_count"] == 1 for summary in coverage["summaries"])
        )

    def test_observation_only_never_proves_asked_or_applicable(self):
        structure, evidence = _checkpoint()
        participant_id = evidence["expected_participants"][0]["participant_id"]
        target_question = next(
            entry["main_question_id"]
            for entry in evidence["entries"]
            if entry["participant_id"] == participant_id
            and entry["evidence_type"] == "participant_self_report"
        )
        for entry in evidence["entries"]:
            if (
                entry["participant_id"] == participant_id
                and entry["main_question_id"] == target_question
            ):
                entry["evidence_type"] = "researcher_observation"
                entry["capture_context"] = "observation"
        proposal = _proposal(structure, evidence)
        row = next(
            row
            for row in proposal["coverage_preview"]["rows"]
            if row["participant_id"] == participant_id
            and row["main_question_id"] == target_question
        )

        self.assertEqual(row["derived_status"], "observation_only")
        self.assertEqual(row["asked_status"], "unknown")
        self.assertEqual(row["applicability"], "unknown")
        self.assertGreater(row["observation_count"], 0)

    def test_blank_is_no_record_not_not_asked_and_has_no_ratio(self):
        snapshot, mapping, mapping_sha = two_participant_bundle(answer_two="")
        result = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )
        proposal = _proposal(result["structure"], result["evidence"])
        missing_id = "participant_" + "7" * 32
        row = next(
            row
            for row in proposal["coverage_preview"]["rows"]
            if row["participant_id"] == missing_id
        )
        summary = proposal["coverage_preview"]["summaries"][0]

        self.assertEqual(row["derived_status"], "no_record")
        self.assertEqual(row["asked_status"], "unknown")
        self.assertEqual(row["applicability"], "unknown")
        self.assertFalse(summary["denominator_reliable"])
        self.assertIsNone(summary["denominator_participant_count"])
        self.assertIsNone(summary["proportion"])

    def test_same_player_across_recorders_counts_once_in_summary(self):
        proposal = _proposal()
        summary = next(
            item
            for item in proposal["coverage_preview"]["summaries"]
            if item["covered_participant_count"] == 2
        )
        rows = [
            row
            for row in proposal["coverage_preview"]["rows"]
            if row["evaluation_object_id"] == summary["evaluation_object_id"]
        ]

        self.assertEqual(summary["participant_count"], 2)
        self.assertEqual(summary["covered_participant_count"], 2)
        self.assertEqual(len({row["participant_id"] for row in rows}), 2)
        self.assertGreater(sum(row["self_report_count"] for row in rows), 2)

    def test_confirmation_covers_all_included_sources_and_enables_only_reliable_ratios(self):
        structure, evidence = _checkpoint()
        boundary = _proposal(structure, evidence)["analysis_boundary"]
        confirmed = confirm_analysis_boundary(
            boundary,
            structure,
            evidence,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            structure_revision_id=STRUCTURE_ID,
            evidence_revision_id=EVIDENCE_ID,
        )
        coverage = build_coverage_preview(structure, evidence, confirmed)

        self.assertEqual(confirmed["status"], "confirmed")
        reliable = [item for item in coverage["summaries"] if item["denominator_reliable"]]
        unreliable = [item for item in coverage["summaries"] if not item["denominator_reliable"]]
        self.assertTrue(reliable)
        self.assertTrue(unreliable)
        self.assertTrue(all(item["proportion"] == 1.0 for item in reliable))
        self.assertTrue(all(item["proportion"] is None for item in unreliable))

        gap = deepcopy(boundary)
        evidence_row = evidence["entries"][0]["row"]
        evidence_sheet = evidence["entries"][0]["sheet_id"]
        gap["source_scope_rules"] = [
            rule
            for rule in gap["source_scope_rules"]
            if not (
                rule["sheet_id"] == evidence_sheet
                and rule["start_row"] <= evidence_row <= rule["end_row"]
            )
        ]
        with self.assertRaises(InterviewV2AnalysisBoundaryError) as context:
            confirm_analysis_boundary(
                gap,
                structure,
                evidence,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                structure_revision_id=STRUCTURE_ID,
                evidence_revision_id=EVIDENCE_ID,
            )
        self.assertEqual(context.exception.code, "SOURCE_SCOPE_COVERAGE_INVALID")

    def test_active_objects_cannot_share_an_occurrence(self):
        structure, evidence = _checkpoint()
        boundary = _proposal(structure, evidence)["analysis_boundary"]
        same_module = {}
        for item in boundary["evaluation_objects"]:
            same_module.setdefault(item["module_id"], []).append(item)
        first, second = next(items for items in same_module.values() if len(items) >= 2)[:2]
        second["main_question_ids"] = sorted(
            set(second["main_question_ids"] + first["main_question_ids"])
        )
        second["occurrence_ids"] = sorted(
            set(second["occurrence_ids"] + [first["occurrence_ids"][0]])
        )

        with self.assertRaises(InterviewV2AnalysisBoundaryError) as context:
            validate_analysis_boundary(
                boundary,
                structure,
                evidence,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                structure_revision_id=STRUCTURE_ID,
                evidence_revision_id=EVIDENCE_ID,
            )
        self.assertEqual(
            context.exception.code, "EVALUATION_OBJECT_OCCURRENCE_CONFLICT"
        )

    def test_confirmation_requires_every_canonical_occurrence_exactly_once(self):
        structure, evidence = _checkpoint()
        boundary = _proposal(structure, evidence)["analysis_boundary"]
        target = max(
            boundary["evaluation_objects"],
            key=lambda item: len(item["occurrence_ids"]),
        )
        omitted_occurrence_id = target["occurrence_ids"].pop()

        validated = validate_analysis_boundary(
            boundary,
            structure,
            evidence,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            structure_revision_id=STRUCTURE_ID,
            evidence_revision_id=EVIDENCE_ID,
        )
        self.assertNotIn(
            omitted_occurrence_id,
            {
                occurrence_id
                for item in validated["evaluation_objects"]
                for occurrence_id in item["occurrence_ids"]
            },
        )
        with self.assertRaises(InterviewV2AnalysisBoundaryError) as context:
            confirm_analysis_boundary(
                boundary,
                structure,
                evidence,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                structure_revision_id=STRUCTURE_ID,
                evidence_revision_id=EVIDENCE_ID,
            )
        self.assertEqual(context.exception.code, "ANALYSIS_BOUNDARY_NOT_CONFIRMABLE")
        self.assertIn(omitted_occurrence_id, context.exception.context["occurrence_ids"])

    def test_label_scope_has_exactly_four_explicit_shapes(self):
        object_id = "evaluation_" + "a" * 32
        module_id = "module_" + "b" * 32
        base = {
            "label_scope_rule_id": "label_scope_" + "c" * 32,
            "label_key": "v6",
            "label_name": "V6 玩家",
        }
        valid = [
            {**base, "scope_mode": "disabled", "module_ids": [], "evaluation_object_ids": []},
            {**base, "scope_mode": "all_analysis", "module_ids": [], "evaluation_object_ids": []},
            {**base, "scope_mode": "selected_modules", "module_ids": [module_id], "evaluation_object_ids": []},
            {**base, "scope_mode": "selected_evaluation_objects", "module_ids": [], "evaluation_object_ids": [object_id]},
        ]
        self.assertEqual(
            [InterviewV2LabelScopeRuleRequest.model_validate(item).scope_mode for item in valid],
            [
                "disabled",
                "all_analysis",
                "selected_modules",
                "selected_evaluation_objects",
            ],
        )
        with self.assertRaises(ValidationError):
            InterviewV2LabelScopeRuleRequest.model_validate(
                {
                    **base,
                    "scope_mode": "all_analysis",
                    "module_ids": [module_id],
                }
            )

    def test_requests_are_strict_unicode_safe_and_responses_ignore_internal_fields(self):
        structure, evidence = _checkpoint()
        boundary = _proposal(structure, evidence)["analysis_boundary"]
        payload = {
            "base_boundary_revision_id": None,
            "base_coverage_revision_id": None,
            "base_structure_revision_id": STRUCTURE_ID,
            "base_evidence_revision_id": EVIDENCE_ID,
            "evaluation_objects": boundary["evaluation_objects"],
            "source_scope_rules": boundary["source_scope_rules"],
            "label_scope_rules": [],
        }
        request = InterviewV2AnalysisBoundaryPutRequest.model_validate(payload)
        self.assertIsNone(request.base_boundary_revision_id)
        self.assertIsNone(request.base_coverage_revision_id)
        with self.assertRaises(ValidationError):
            InterviewV2AnalysisBoundaryPutRequest.model_validate(
                {**payload, "unexpected": True}
            )
        with self.assertRaises(ValidationError):
            InterviewV2AnalysisBoundaryPutRequest.model_validate(
                {
                    **payload,
                    "base_boundary_revision_id": "boundary_" + "a" * 32,
                }
            )
        invalid_unicode = deepcopy(payload)
        invalid_unicode["change_reason"] = "broken\ud800"
        with self.assertRaises(ValidationError):
            InterviewV2AnalysisBoundaryPutRequest.model_validate(invalid_unicode)

        confirm_payload = {
            "boundary_revision_id": "boundary_" + "a" * 32,
            "coverage_revision_id": "coverage_" + "b" * 32,
            "boundary_payload_sha256": "c" * 64,
            "coverage_payload_sha256": "d" * 64,
        }
        confirmed = InterviewV2AnalysisBoundaryConfirmRequest.model_validate(
            confirm_payload
        )
        self.assertEqual(
            confirmed.coverage_revision_id, "coverage_" + "b" * 32
        )
        for field in confirm_payload:
            malformed = dict(confirm_payload)
            malformed[field] = "not-a-valid-resource"
            with self.assertRaises(ValidationError, msg=field):
                InterviewV2AnalysisBoundaryConfirmRequest.model_validate(malformed)
        with self.assertRaises(ValidationError):
            InterviewV2AnalysisBoundaryConfirmRequest.model_validate(
                {**confirm_payload, "extra": "forbidden"}
            )

        response = InterviewV2AnalysisBoundaryPayloadResponse.model_validate(
            {**boundary, "internal_audit": {"private": True}}
        )
        self.assertFalse(hasattr(response, "internal_audit"))


if __name__ == "__main__":
    unittest.main()
