from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import interview_v2_structure_service as structure_service
from app.storage import interview_v2_store as store


PROJECT_ID = "project_" + "1" * 32
IMPORT_ID = "import_" + "2" * 32
WORKBOOK_ID = "workbook_" + "3" * 32
MAPPING_ID = "mapping_" + "4" * 32
STRUCTURE_ID_1 = "structure_" + "5" * 32
EVIDENCE_REVISION_ID_1 = "evidence_" + "6" * 32
STRUCTURE_ID_2 = "structure_" + "7" * 32
EVIDENCE_REVISION_ID_2 = "evidence_" + "8" * 32
EVIDENCE_ID = "ev_" + "9" * 32
EVIDENCE_ID_2 = "ev_" + "8" * 32
ISSUE_ID = "issue_" + "a" * 32
OVERRIDE_ID = "override_" + "b" * 32
REQUEST_1 = "c" * 64
REQUEST_2 = "d" * 64
GROUP_ID = "group_" + "a" * 32
PARTICIPANT_ID_1 = "participant_" + "b" * 32
PARTICIPANT_ID_2 = "participant_" + "c" * 32


def _snapshot() -> dict:
    snapshot = {
        "schema_version": "interview-workbook-physical-truth/1.0",
        "content_sha256": "e" * 64,
        "sheets": [
            {
                "sheet_id": "sheet_001",
                "name": "记录员 1",
                "cells": [
                    {
                        "address": "A2",
                        "row": 2,
                        "column": 1,
                        "raw_value": "主问题",
                    },
                    {
                        "address": "D2",
                        "row": 2,
                        "column": 4,
                        "raw_value": "玩家的私密原始回答",
                    },
                    {
                        "address": "E2",
                        "row": 2,
                        "column": 5,
                        "raw_value": "其他玩家不得泄露的回答",
                    },
                ],
            }
        ],
    }
    snapshot["snapshot_sha256"] = store.physical_snapshot_sha256(snapshot)
    return snapshot


class InterviewV2StructureStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="iv2s-"
        )
        self.root = Path(self.temp_dir.name) / "v2"
        self.config_patch = patch.object(
            store.config, "INTERVIEW_V2_DATA_DIR", self.root
        )
        self.config_patch.start()
        snapshot = _snapshot()
        owner = {
            "owner_key": "email:owner@example.com",
            "owner_email": "owner@example.com",
            "owner_open_id": "",
            "owner_name": "Owner",
        }
        store.publish_accepted_bundle(
            project={
                "project_id": PROJECT_ID,
                "status": "GROUP_CONFIRMATION_REQUIRED",
                "current_workbook_revision_id": WORKBOOK_ID,
                "current_import_id": IMPORT_ID,
                **owner,
            },
            workbook_revision={
                "workbook_revision_id": WORKBOOK_ID,
                "project_id": PROJECT_ID,
                "content_sha256": snapshot["content_sha256"],
                "physical_snapshot_version": snapshot["schema_version"],
                "snapshot_sha256": snapshot["snapshot_sha256"],
                **owner,
            },
            interview_import={
                "import_id": IMPORT_ID,
                "project_id": PROJECT_ID,
                "workbook_revision_id": WORKBOOK_ID,
                "status": "GROUP_CONFIRMATION_REQUIRED",
                "physical_snapshot_version": snapshot["schema_version"],
                **owner,
            },
            source_content=b"xlsx",
            physical_snapshot=snapshot,
        )
        mapping = {
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "workbook_revision_id": WORKBOOK_ID,
            "base_snapshot_sha256": snapshot["snapshot_sha256"],
            "groups": [
                {
                    "group_id": GROUP_ID,
                    "decision_status": "confirmed",
                    "sheets": [
                        {
                            "sheet_id": "sheet_001",
                            "role": "record",
                            "recorder_label": "记录员 1",
                            "decision_status": "confirmed",
                        }
                    ],
                    "participants": [
                        {
                            "participant_id": PARTICIPANT_ID_1,
                            "participant_label": "P01",
                            "decision_status": "confirmed",
                            "columns": [
                                {
                                    "sheet_id": "sheet_001",
                                    "column_index": 4,
                                    "decision_status": "confirmed",
                                }
                            ],
                        },
                        {
                            "participant_id": PARTICIPANT_ID_2,
                            "participant_label": "P02",
                            "decision_status": "confirmed",
                            "columns": [
                                {
                                    "sheet_id": "sheet_001",
                                    "column_index": 5,
                                    "decision_status": "confirmed",
                                }
                            ],
                        },
                    ],
                }
            ],
            "ignored_sheet_ids": [],
        }
        mapping_sha = store._canonical_payload_sha256(mapping)
        mapping_revision = {
            "mapping_revision_id": MAPPING_ID,
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "workbook_revision_id": WORKBOOK_ID,
            "revision_number": 1,
            "mapping_sha256": mapping_sha,
            "mapping": mapping,
            "issues": [],
            "confirmation_ready": True,
            "final_participant_preview": {},
            "change_kind": "manual_edit",
            "change_reason": "confirmed test mapping",
            "created_at": "2026-08-14T00:00:00Z",
            "created_by": owner["owner_key"],
        }
        mapping_revision["revision_payload_sha256"] = (
            store.mapping_revision_payload_sha256(mapping_revision)
        )
        store.save_mapping_revision_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_mapping_revision=0,
            revision=mapping_revision,
            updated_at="2026-08-14T00:00:00Z",
        )
        store.confirm_mapping_revision_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_mapping_revision=1,
            mapping_sha256=mapping_sha,
            confirmed_by=owner["owner_key"],
            confirmed_at="2026-08-14T00:00:01Z",
        )
        self.snapshot_sha256 = snapshot["snapshot_sha256"]
        self.mapping_sha256 = mapping_sha

    def tearDown(self):
        self.config_patch.stop()
        self.temp_dir.cleanup()

    def _source(
        self,
        *,
        mapping_revision_id: str = MAPPING_ID,
        mapping_sha256: str | None = None,
    ) -> dict:
        return {
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "workbook_revision_id": WORKBOOK_ID,
            "base_snapshot_sha256": self.snapshot_sha256,
            "mapping_revision_id": mapping_revision_id,
            "mapping_sha256": mapping_sha256 or self.mapping_sha256,
        }

    def _artifacts(
        self,
        *,
        revision_number: int = 1,
        structure_id: str = STRUCTURE_ID_1,
        evidence_revision_id: str = EVIDENCE_REVISION_ID_1,
        request_fingerprint: str = REQUEST_1,
        created_at: str = "2026-08-14T00:00:02Z",
        raw_content: str = "玩家的私密原始回答",
        mapping_revision_id: str = MAPPING_ID,
        mapping_sha256: str | None = None,
    ) -> tuple[dict, dict]:
        artifact_mapping_sha256 = mapping_sha256 or self.mapping_sha256
        input_fingerprint = store.confirmed_structure_input_sha256(
            {
                "project_id": PROJECT_ID,
                "import_id": IMPORT_ID,
                "workbook_revision_id": WORKBOOK_ID,
                "snapshot_sha256": self.snapshot_sha256,
                "mapping_revision_id": mapping_revision_id,
                "mapping_sha256": artifact_mapping_sha256,
            }
        )
        structure = {
            "structure_revision_id": structure_id,
            "revision_number": revision_number,
            "request_fingerprint": request_fingerprint,
            "input_fingerprint": input_fingerprint,
            "created_at": created_at,
            "structure_schema_version": "interview-structure/1.0",
            "source": self._source(
                mapping_revision_id=mapping_revision_id,
                mapping_sha256=artifact_mapping_sha256,
            ),
            "modules": [],
            "main_questions": [],
            "occurrences": [],
        }
        structure["revision_payload_sha256"] = (
            store.structure_revision_payload_sha256(structure)
        )
        evidence = {
            "evidence_revision_id": evidence_revision_id,
            "structure_revision_id": structure_id,
            "revision_number": revision_number,
            "request_fingerprint": request_fingerprint,
            "input_fingerprint": input_fingerprint,
            "created_at": created_at,
            "evidence_schema_version": "interview-evidence/1.0",
            "source": self._source(
                mapping_revision_id=mapping_revision_id,
                mapping_sha256=artifact_mapping_sha256,
            ),
            "expected_participants": [
                {
                    "participant_id": PARTICIPANT_ID_1,
                    "group_id": GROUP_ID,
                },
                {
                    "participant_id": PARTICIPANT_ID_2,
                    "group_id": GROUP_ID,
                },
            ],
            "entries": [
                {
                    "evidence_id": EVIDENCE_ID,
                    "participant_id": PARTICIPANT_ID_1,
                    "group_id": GROUP_ID,
                    "sheet_id": "sheet_001",
                    "source_cell_id": "cell_" + "f" * 32,
                    "source_address": "D2",
                    "module_id": "module_" + "0" * 32,
                    "main_question_id": "question_" + "1" * 32,
                    "evidence_type": "participant_self_report",
                    "inclusion_status": "included",
                    "identity_decision_status": "system_verified",
                    "formula_cache_status": "not_applicable",
                    "raw_content": raw_content,
                    "display_content": raw_content,
                    "normalized_content": raw_content,
                },
                {
                    "evidence_id": EVIDENCE_ID_2,
                    "participant_id": PARTICIPANT_ID_2,
                    "group_id": GROUP_ID,
                    "sheet_id": "sheet_001",
                    "source_cell_id": "cell_" + "e" * 32,
                    "source_address": "E2",
                    "module_id": "module_" + "0" * 32,
                    "main_question_id": "question_" + "1" * 32,
                    "evidence_type": "participant_self_report",
                    "inclusion_status": "included",
                    "identity_decision_status": "system_verified",
                    "formula_cache_status": "not_applicable",
                    "raw_content": "其他玩家的原始回答",
                    "display_content": "其他玩家的原始回答",
                    "normalized_content": "其他玩家的原始回答",
                },
            ],
        }
        evidence["revision_payload_sha256"] = (
            store.evidence_revision_payload_sha256(evidence)
        )
        return structure, evidence

    @staticmethod
    def _issue(*, resolved_at: str | None = None) -> dict:
        return {
            "issue_id": ISSUE_ID,
            "code": "QUESTION_PARENT_MISSING",
            "severity": "blocking",
            "status": "resolved" if resolved_at else "open",
            "resolved_at": resolved_at,
            "raw_content": "待确认的原始内容",
        }

    def _publish_initial(self):
        structure, evidence = self._artifacts()
        return store.save_structure_bundle_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_structure_revision_id=None,
            base_evidence_revision_id=None,
            structure_revision=structure,
            evidence_revision=evidence,
            review_issues=[self._issue()],
            manual_overrides=[],
            request_fingerprint=REQUEST_1,
            effective_status="STRUCTURE_REVIEW_REQUIRED",
            updated_at="2026-08-14T00:00:02Z",
        )

    def _save_second_mapping_draft(self) -> tuple[str, str]:
        mapping_revision_id = "mapping_" + "0" * 32
        mapping = {
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "workbook_revision_id": WORKBOOK_ID,
            "base_snapshot_sha256": self.snapshot_sha256,
            "groups": [
                {
                    "group_id": GROUP_ID,
                    "decision_status": "confirmed",
                    "sheets": [
                        {
                            "sheet_id": "sheet_001",
                            "role": "record",
                            "recorder_label": "记录员 2",
                            "decision_status": "confirmed",
                        }
                    ],
                    "participants": [
                        {
                            "participant_id": PARTICIPANT_ID_1,
                            "participant_label": "P01",
                            "decision_status": "confirmed",
                            "columns": [
                                {
                                    "sheet_id": "sheet_001",
                                    "column_index": 4,
                                    "decision_status": "confirmed",
                                }
                            ],
                        },
                        {
                            "participant_id": PARTICIPANT_ID_2,
                            "participant_label": "P02",
                            "decision_status": "confirmed",
                            "columns": [
                                {
                                    "sheet_id": "sheet_001",
                                    "column_index": 5,
                                    "decision_status": "confirmed",
                                }
                            ],
                        },
                    ],
                }
            ],
            "ignored_sheet_ids": [],
        }
        mapping_sha256 = store._canonical_payload_sha256(mapping)
        revision = {
            "mapping_revision_id": mapping_revision_id,
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "workbook_revision_id": WORKBOOK_ID,
            "revision_number": 2,
            "mapping_sha256": mapping_sha256,
            "mapping": mapping,
            "issues": [],
            "confirmation_ready": True,
            "final_participant_preview": {},
            "change_kind": "manual_edit",
            "change_reason": "confirmed replacement mapping",
            "created_at": "2026-08-14T00:02:00Z",
            "created_by": "email:owner@example.com",
        }
        revision["revision_payload_sha256"] = (
            store.mapping_revision_payload_sha256(revision)
        )
        store.save_mapping_revision_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_mapping_revision=1,
            revision=revision,
            updated_at="2026-08-14T00:02:00Z",
        )
        return mapping_revision_id, mapping_sha256

    def _confirm_second_mapping(self) -> tuple[str, str]:
        mapping_revision_id, mapping_sha256 = (
            self._save_second_mapping_draft()
        )
        store.confirm_mapping_revision_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_mapping_revision=2,
            mapping_sha256=mapping_sha256,
            confirmed_by="email:owner@example.com",
            confirmed_at="2026-08-14T00:02:01Z",
        )
        return mapping_revision_id, mapping_sha256

    def test_confirmed_input_is_frozen_and_cross_checked(self):
        bundle = store.load_confirmed_structure_input_bundle(IMPORT_ID)
        self.assertEqual(bundle["confirmed_input"], {
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "workbook_revision_id": WORKBOOK_ID,
            "snapshot_sha256": self.snapshot_sha256,
            "mapping_revision_id": MAPPING_ID,
            "mapping_sha256": self.mapping_sha256,
        })
        self.assertEqual(
            bundle["input_fingerprint"],
            store.confirmed_structure_input_sha256(bundle["confirmed_input"]),
        )

        mapping_path = (
            self.root
            / "projects"
            / PROJECT_ID
            / "mapping_revisions"
            / f"{MAPPING_ID}.json"
        )
        tampered = json.loads(mapping_path.read_text(encoding="utf-8"))
        tampered["mapping"]["groups"] = [{"group_id": "tampered"}]
        mapping_path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(ValueError):
            store.load_confirmed_structure_input_bundle(IMPORT_ID)

    def test_draft_mapping_race_rejects_first_publish_without_partial_writes(self):
        structure, evidence = self._artifacts()
        draft_mapping_id, draft_mapping_sha = self._save_second_mapping_draft()

        with self.assertRaises(
            store.StructureInputConflictError
        ) as caught:
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[self._issue()],
                manual_overrides=[],
                request_fingerprint=REQUEST_1,
                effective_status="STRUCTURE_REVIEW_REQUIRED",
                updated_at="2026-08-14T00:02:01Z",
            )

        self.assertEqual(
            caught.exception.current_mapping_revision_id,
            draft_mapping_id,
        )
        self.assertEqual(
            caught.exception.current_mapping_sha256,
            draft_mapping_sha,
        )
        self.assertEqual(
            caught.exception.current_mapping_status,
            "GROUP_CONFIRMATION_REQUIRED",
        )
        unwritten_paths = (
            store._structure_state_path(PROJECT_ID),
            store._structure_revision_path(PROJECT_ID, STRUCTURE_ID_1),
            store._evidence_revision_path(
                PROJECT_ID, EVIDENCE_REVISION_ID_1
            ),
            store._review_issues_path(PROJECT_ID, EVIDENCE_REVISION_ID_1),
            store._evidence_locator_path(EVIDENCE_ID),
            store._review_issue_locator_path(ISSUE_ID),
        )
        self.assertTrue(all(not path.exists() for path in unwritten_paths))

    def test_publish_load_safe_locators_and_idempotent_retry(self):
        durable_structure, durable_evidence, state = self._publish_initial()
        self.assertEqual(state["artifact_status"], "CURRENT")
        self.assertFalse(state["is_stale"])
        self.assertEqual(
            state["current_structure_revision_id"], STRUCTURE_ID_1
        )
        self.assertEqual(
            state["current_evidence_revision_id"], EVIDENCE_REVISION_ID_1
        )

        locator = store.locate_evidence(EVIDENCE_ID)
        self.assertEqual(locator["project_id"], PROJECT_ID)
        self.assertEqual(locator["import_id"], IMPORT_ID)
        self.assertNotIn("raw_content", locator)
        issue_locator = store.locate_review_issue(ISSUE_ID)
        self.assertEqual(issue_locator["project_id"], PROJECT_ID)
        self.assertNotIn("raw_content", issue_locator)

        context = store.load_evidence_with_context(
            PROJECT_ID, IMPORT_ID, EVIDENCE_REVISION_ID_1, EVIDENCE_ID
        )
        self.assertEqual(
            context["evidence"]["raw_content"], "玩家的私密原始回答"
        )
        self.assertEqual(
            context["physical_snapshot"]["snapshot_sha256"],
            self.snapshot_sha256,
        )

        retried_structure, retried_evidence = self._artifacts(
            created_at="2026-08-14T00:09:00Z",
            raw_content="重试不应覆盖首次持久化内容",
        )
        retried = store.save_structure_bundle_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_structure_revision_id=None,
            base_evidence_revision_id=None,
            structure_revision=retried_structure,
            evidence_revision=retried_evidence,
            review_issues=[self._issue()],
            manual_overrides=[],
            request_fingerprint=REQUEST_1,
            effective_status="STRUCTURE_REVIEW_REQUIRED",
            updated_at="2026-08-14T00:09:00Z",
        )
        self.assertEqual(retried[0], durable_structure)
        self.assertEqual(retried[1], durable_evidence)
        self.assertEqual(
            retried[1]["entries"][0]["raw_content"],
            "玩家的私密原始回答",
        )

    def test_semantic_status_gate_blocks_ready_bypass_atomically(self):
        structure, evidence = self._artifacts()
        with self.assertRaisesRegex(ValueError, "status contradicts"):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[self._issue()],
                manual_overrides=[],
                request_fingerprint=REQUEST_1,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:00:02Z",
            )
        self.assertFalse(store._structure_state_path(PROJECT_ID).exists())
        self.assertFalse(
            store._structure_revision_path(PROJECT_ID, STRUCTURE_ID_1).exists()
        )
        self.assertFalse(
            store._evidence_revision_path(
                PROJECT_ID, EVIDENCE_REVISION_ID_1
            ).exists()
        )
        self.assertFalse(
            store._review_issues_path(
                PROJECT_ID, EVIDENCE_REVISION_ID_1
            ).exists()
        )

        structure, evidence = self._artifacts()
        evidence["entries"] = []
        evidence["revision_payload_sha256"] = (
            store.evidence_revision_payload_sha256(evidence)
        )
        with self.assertRaisesRegex(ValueError, "status contradicts"):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[],
                manual_overrides=[],
                request_fingerprint=REQUEST_1,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:00:02Z",
            )
        self.assertFalse(store._structure_state_path(PROJECT_ID).exists())
        self.assertFalse(
            store._evidence_revision_path(
                PROJECT_ID, EVIDENCE_REVISION_ID_1
            ).exists()
        )

        structure, evidence = self._artifacts()
        evidence["entries"][0]["inclusion_status"] = "excluded_by_user"
        evidence["revision_payload_sha256"] = (
            store.evidence_revision_payload_sha256(evidence)
        )
        with self.assertRaisesRegex(ValueError, "status contradicts"):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[],
                manual_overrides=[],
                request_fingerprint=REQUEST_1,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:00:02Z",
            )
        self.assertFalse(store._structure_state_path(PROJECT_ID).exists())
        self.assertFalse(
            store._evidence_revision_path(
                PROJECT_ID, EVIDENCE_REVISION_ID_1
            ).exists()
        )

        self._publish_initial()
        state_path = store._structure_state_path(PROJECT_ID)
        state_before = state_path.read_bytes()
        structure, evidence = self._artifacts(
            revision_number=2,
            structure_id=STRUCTURE_ID_2,
            evidence_revision_id=EVIDENCE_REVISION_ID_2,
            request_fingerprint=REQUEST_2,
            created_at="2026-08-14T00:01:00Z",
        )
        evidence["entries"][0]["identity_decision_status"] = "needs_review"
        evidence["revision_payload_sha256"] = (
            store.evidence_revision_payload_sha256(evidence)
        )
        with self.assertRaisesRegex(ValueError, "status contradicts"):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=STRUCTURE_ID_1,
                base_evidence_revision_id=EVIDENCE_REVISION_ID_1,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[],
                manual_overrides=[],
                request_fingerprint=REQUEST_2,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:01:00Z",
            )
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertFalse(
            store._structure_revision_path(PROJECT_ID, STRUCTURE_ID_2).exists()
        )
        self.assertFalse(
            store._evidence_revision_path(
                PROJECT_ID, EVIDENCE_REVISION_ID_2
            ).exists()
        )

    def test_semantic_status_gate_rejects_malformed_contract_fields(self):
        for case in (
            "issue_status",
            "inclusion_status",
            "evidence_type",
            "formula_cache_status",
        ):
            with self.subTest(case=case):
                structure, evidence = self._artifacts()
                issues = []
                if case == "issue_status":
                    issue = self._issue()
                    issue["status"] = "dismissed"
                    issues = [issue]
                elif case == "inclusion_status":
                    evidence["entries"][0].pop("inclusion_status")
                elif case == "formula_cache_status":
                    evidence["entries"][0].pop("formula_cache_status")
                else:
                    evidence["entries"][0]["evidence_type"] = "unknown"
                evidence["revision_payload_sha256"] = (
                    store.evidence_revision_payload_sha256(evidence)
                )
                with self.assertRaises(ValueError):
                    store.save_structure_bundle_cas(
                        project_id=PROJECT_ID,
                        import_id=IMPORT_ID,
                        base_structure_revision_id=None,
                        base_evidence_revision_id=None,
                        structure_revision=structure,
                        evidence_revision=evidence,
                        review_issues=issues,
                        manual_overrides=[],
                        request_fingerprint=REQUEST_1,
                        effective_status="READY_FOR_DOSSIERS",
                        updated_at="2026-08-14T00:00:02Z",
                    )
                self.assertFalse(store._structure_state_path(PROJECT_ID).exists())

    def test_participant_manifest_matches_mapping_and_is_immutable(self):
        structure, evidence = self._artifacts()
        evidence["expected_participants"] = evidence["expected_participants"][:1]
        evidence["entries"] = evidence["entries"][:1]
        evidence["revision_payload_sha256"] = (
            store.evidence_revision_payload_sha256(evidence)
        )
        with self.assertRaisesRegex(ValueError, "manifest does not match"):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[],
                manual_overrides=[],
                request_fingerprint=REQUEST_1,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:00:02Z",
            )
        self.assertFalse(store._structure_state_path(PROJECT_ID).exists())
        self.assertFalse(
            store._evidence_revision_path(
                PROJECT_ID, EVIDENCE_REVISION_ID_1
            ).exists()
        )

        structure, evidence = self._artifacts()
        evidence.pop("expected_participants")
        evidence["revision_payload_sha256"] = (
            store.evidence_revision_payload_sha256(evidence)
        )
        with self.assertRaisesRegex(ValueError, "manifest"):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[self._issue()],
                manual_overrides=[],
                request_fingerprint=REQUEST_1,
                effective_status="STRUCTURE_REVIEW_REQUIRED",
                updated_at="2026-08-14T00:00:02Z",
            )

        self._publish_initial()
        state_path = store._structure_state_path(PROJECT_ID)
        state_before = state_path.read_bytes()
        structure, evidence = self._artifacts(
            revision_number=2,
            structure_id=STRUCTURE_ID_2,
            evidence_revision_id=EVIDENCE_REVISION_ID_2,
            request_fingerprint=REQUEST_2,
            created_at="2026-08-14T00:01:00Z",
        )
        fake_group_id = "group_" + "d" * 32
        fake_participant_1 = "participant_" + "e" * 32
        fake_participant_2 = "participant_" + "f" * 32
        evidence["expected_participants"] = [
            {
                "participant_id": fake_participant_1,
                "group_id": fake_group_id,
            },
            {
                "participant_id": fake_participant_2,
                "group_id": fake_group_id,
            },
        ]
        for entry, participant_id in zip(
            evidence["entries"],
            (fake_participant_1, fake_participant_2),
            strict=True,
        ):
            entry["group_id"] = fake_group_id
            entry["participant_id"] = participant_id
        evidence["revision_payload_sha256"] = (
            store.evidence_revision_payload_sha256(evidence)
        )
        with self.assertRaisesRegex(ValueError, "manifest does not match"):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=STRUCTURE_ID_1,
                base_evidence_revision_id=EVIDENCE_REVISION_ID_1,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[],
                manual_overrides=[],
                request_fingerprint=REQUEST_2,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:01:00Z",
            )
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertFalse(
            store._evidence_revision_path(
                PROJECT_ID, EVIDENCE_REVISION_ID_2
            ).exists()
        )

    def test_formula_and_per_participant_readiness_cannot_be_forged(self):
        structure, evidence = self._artifacts()
        evidence["entries"][0]["inclusion_status"] = "excluded_by_user"
        evidence["revision_payload_sha256"] = (
            store.evidence_revision_payload_sha256(evidence)
        )
        with self.assertRaisesRegex(ValueError, "status contradicts"):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[],
                manual_overrides=[],
                request_fingerprint=REQUEST_1,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:00:02Z",
            )

        structure, evidence = self._artifacts()
        evidence["entries"][0]["normalized_content"] = " \x00\r\n "
        evidence["revision_payload_sha256"] = (
            store.evidence_revision_payload_sha256(evidence)
        )
        with self.assertRaisesRegex(ValueError, "normalized content is invalid"):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[],
                manual_overrides=[],
                request_fingerprint=REQUEST_1,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:00:02Z",
            )

        structure, evidence = self._artifacts()
        evidence["entries"][0]["formula_cache_status"] = "unavailable"
        evidence["entries"][0]["identity_decision_status"] = "system_verified"
        evidence["revision_payload_sha256"] = (
            store.evidence_revision_payload_sha256(evidence)
        )
        with self.assertRaisesRegex(ValueError, "status contradicts"):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[],
                manual_overrides=[],
                request_fingerprint=REQUEST_1,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:00:02Z",
            )

        structure, evidence = self._artifacts()
        evidence["entries"][0].update(
            {
                "raw_content": "=ROUND(41.6,0)",
                "display_content": "42",
                "normalized_content": "=ROUND(41.6,0)",
                "formula_cache_status": "available",
            }
        )
        evidence["revision_payload_sha256"] = (
            store.evidence_revision_payload_sha256(evidence)
        )
        with self.assertRaisesRegex(ValueError, "display is invalid"):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[],
                manual_overrides=[],
                request_fingerprint=REQUEST_1,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:00:02Z",
            )

        evidence["entries"][0]["normalized_content"] = "42"
        evidence["revision_payload_sha256"] = (
            store.evidence_revision_payload_sha256(evidence)
        )
        store.save_structure_bundle_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_structure_revision_id=None,
            base_evidence_revision_id=None,
            structure_revision=structure,
            evidence_revision=evidence,
            review_issues=[],
            manual_overrides=[],
            request_fingerprint=REQUEST_1,
            effective_status="READY_FOR_DOSSIERS",
            updated_at="2026-08-14T00:00:02Z",
        )
        state = store.load_structure_state(PROJECT_ID)
        self.assertEqual(state["effective_status"], "READY_FOR_DOSSIERS")

    def test_resigned_checkpoint_status_tampering_fails_semantic_check(self):
        self._publish_initial()
        issues_path = store._review_issues_path(
            PROJECT_ID, EVIDENCE_REVISION_ID_1
        )
        issues_bundle = json.loads(issues_path.read_text(encoding="utf-8"))
        issues_bundle["effective_status"] = "READY_FOR_DOSSIERS"
        issues_bundle["review_issues_payload_sha256"] = (
            store._review_issues_payload_sha256(issues_bundle)
        )
        issues_path.write_text(
            json.dumps(issues_bundle, ensure_ascii=False), encoding="utf-8"
        )

        state_path = store._structure_state_path(PROJECT_ID)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["effective_status"] = "READY_FOR_DOSSIERS"
        state["current_review_issues_payload_sha256"] = issues_bundle[
            "review_issues_payload_sha256"
        ]
        state["revision_history"][-1]["review_issues_payload_sha256"] = (
            issues_bundle["review_issues_payload_sha256"]
        )
        state["revision_history"][-1]["effective_status"] = (
            "READY_FOR_DOSSIERS"
        )
        state["state_payload_sha256"] = store.structure_state_payload_sha256(
            state
        )
        state_path.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "contradicts persisted evidence"):
            store.load_structure_state(PROJECT_ID)

    def test_review_cas_appends_override_without_rewriting_locators(self):
        self._publish_initial()
        structure, evidence = self._artifacts(
            revision_number=2,
            structure_id=STRUCTURE_ID_2,
            evidence_revision_id=EVIDENCE_REVISION_ID_2,
            request_fingerprint=REQUEST_2,
            created_at="2026-08-14T00:01:00Z",
        )
        override = {
            "manual_override_id": OVERRIDE_ID,
            "changes": [
                {
                    "entity_type": "question_occurrence",
                    "entity_id": "occ_" + "0" * 32,
                    "before": {"main_question_id": None},
                    "after": {
                        "main_question_id": "question_" + "1" * 32
                    },
                }
            ],
            "reason": "人工确认问题归属",
            "created_by": "email:owner@example.com",
            "created_at": "2026-08-14T00:01:00Z",
        }
        with patch.object(
            store,
            "_write_or_reuse_locator",
            wraps=store._write_or_reuse_locator,
        ) as locator_writer:
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=STRUCTURE_ID_1,
                base_evidence_revision_id=EVIDENCE_REVISION_ID_1,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[],
                manual_overrides=[override],
                request_fingerprint=REQUEST_2,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:01:00Z",
            )
        locator_writer.assert_not_called()

        bundle = store.load_current_structure_bundle(PROJECT_ID, IMPORT_ID)
        self.assertEqual(bundle["state"]["effective_status"], "READY_FOR_DOSSIERS")
        self.assertEqual(bundle["state"]["manual_override_revision"], 1)
        self.assertEqual(
            bundle["manual_overrides"][0]["manual_override_id"],
            OVERRIDE_ID,
        )
        self.assertEqual(bundle["review_issues"], [])

        with self.assertRaises(FileExistsError):
            store.load_evidence_with_context(
                PROJECT_ID,
                IMPORT_ID,
                EVIDENCE_REVISION_ID_1,
                EVIDENCE_ID,
            )

    def test_confirmed_mapping_change_allows_exact_base_rebuild(self):
        self._publish_initial()
        previous = store.load_current_structure_bundle(PROJECT_ID, IMPORT_ID)
        previous_override_ids = list(
            previous["state"].get("manual_override_ids") or []
        )
        mapping_revision_id, mapping_sha256 = self._confirm_second_mapping()
        stale = store.load_structure_state(PROJECT_ID)
        self.assertTrue(stale["is_stale"])
        state_path = store._structure_state_path(PROJECT_ID)
        state_before_conflict = state_path.read_bytes()

        stale_structure, stale_evidence = self._artifacts(
            revision_number=2,
            structure_id=STRUCTURE_ID_2,
            evidence_revision_id=EVIDENCE_REVISION_ID_2,
            request_fingerprint=REQUEST_2,
            created_at="2026-08-14T00:03:00Z",
        )
        with self.assertRaises(
            store.StructureInputConflictError
        ) as caught:
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=STRUCTURE_ID_1,
                base_evidence_revision_id=EVIDENCE_REVISION_ID_1,
                structure_revision=stale_structure,
                evidence_revision=stale_evidence,
                review_issues=[],
                manual_overrides=[],
                request_fingerprint=REQUEST_2,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:03:00Z",
            )
        self.assertEqual(
            caught.exception.current_mapping_revision_id,
            mapping_revision_id,
        )
        self.assertEqual(
            caught.exception.current_mapping_sha256,
            mapping_sha256,
        )
        self.assertEqual(
            caught.exception.current_mapping_status,
            "GROUP_MAPPING_CONFIRMED",
        )
        self.assertEqual(state_path.read_bytes(), state_before_conflict)
        self.assertFalse(
            store._structure_revision_path(PROJECT_ID, STRUCTURE_ID_2).exists()
        )
        self.assertFalse(
            store._evidence_revision_path(
                PROJECT_ID, EVIDENCE_REVISION_ID_2
            ).exists()
        )
        self.assertFalse(
            store._review_issues_path(
                PROJECT_ID, EVIDENCE_REVISION_ID_2
            ).exists()
        )

        structure, evidence = self._artifacts(
            revision_number=2,
            structure_id=STRUCTURE_ID_2,
            evidence_revision_id=EVIDENCE_REVISION_ID_2,
            request_fingerprint=REQUEST_2,
            created_at="2026-08-14T00:03:01Z",
            mapping_revision_id=mapping_revision_id,
            mapping_sha256=mapping_sha256,
        )
        with self.assertRaises(FileExistsError):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[],
                manual_overrides=[],
                request_fingerprint=REQUEST_2,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:03:01Z",
            )

        store.save_structure_bundle_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_structure_revision_id=STRUCTURE_ID_1,
            base_evidence_revision_id=EVIDENCE_REVISION_ID_1,
            structure_revision=structure,
            evidence_revision=evidence,
            review_issues=[],
            manual_overrides=[],
            request_fingerprint=REQUEST_2,
            effective_status="READY_FOR_DOSSIERS",
            updated_at="2026-08-14T00:03:01Z",
        )
        rebuilt = store.load_current_structure_bundle(PROJECT_ID, IMPORT_ID)
        self.assertFalse(rebuilt["state"]["is_stale"])
        self.assertEqual(rebuilt["state"]["artifact_status"], "CURRENT")
        self.assertEqual(
            rebuilt["state"]["current_mapping_revision_id"],
            mapping_revision_id,
        )
        self.assertEqual(rebuilt["state"]["current_mapping_sha256"], mapping_sha256)
        self.assertEqual(
            rebuilt["state"]["manual_override_ids"], previous_override_ids
        )
        self.assertEqual(len(rebuilt["state"]["revision_history"]), 2)
        self.assertEqual(
            rebuilt["state"]["revision_history"][0]["structure_revision_id"],
            STRUCTURE_ID_1,
        )

        concurrent_structure, concurrent_evidence = self._artifacts(
            revision_number=2,
            structure_id="structure_" + "e" * 32,
            evidence_revision_id="evidence_" + "f" * 32,
            request_fingerprint="1" * 64,
            created_at="2026-08-14T00:03:02Z",
            mapping_revision_id=mapping_revision_id,
            mapping_sha256=mapping_sha256,
        )
        with self.assertRaises(FileExistsError):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=STRUCTURE_ID_1,
                base_evidence_revision_id=EVIDENCE_REVISION_ID_1,
                structure_revision=concurrent_structure,
                evidence_revision=concurrent_evidence,
                review_issues=[],
                manual_overrides=[],
                request_fingerprint="1" * 64,
                effective_status="READY_FOR_DOSSIERS",
                updated_at="2026-08-14T00:03:02Z",
            )

    def test_conflict_stale_and_head_tampering_fail_closed(self):
        self._publish_initial()
        structure, evidence = self._artifacts(
            request_fingerprint=REQUEST_2
        )
        with self.assertRaises(FileExistsError):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[self._issue()],
                manual_overrides=[],
                request_fingerprint=REQUEST_2,
                effective_status="STRUCTURE_REVIEW_REQUIRED",
                updated_at="2026-08-14T00:00:03Z",
            )

        next_mapping = {
            "groups": [{"group_id": "group_02"}],
            "ignored_sheet_ids": [],
        }
        next_mapping_sha = store._canonical_payload_sha256(next_mapping)
        next_mapping_revision = {
            "mapping_revision_id": "mapping_" + "0" * 32,
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "workbook_revision_id": WORKBOOK_ID,
            "revision_number": 2,
            "mapping_sha256": next_mapping_sha,
            "mapping": next_mapping,
            "issues": [],
            "confirmation_ready": True,
            "change_kind": "manual_edit",
            "change_reason": "mapping changed",
            "created_at": "2026-08-14T00:02:00Z",
        }
        next_mapping_revision["revision_payload_sha256"] = (
            store.mapping_revision_payload_sha256(next_mapping_revision)
        )
        store.save_mapping_revision_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_mapping_revision=1,
            revision=next_mapping_revision,
            updated_at="2026-08-14T00:02:00Z",
        )
        stale = store.load_structure_state(PROJECT_ID)
        self.assertTrue(stale["is_stale"])
        self.assertEqual(stale["artifact_status"], "STALE")

        structure_path = (
            self.root
            / "projects"
            / PROJECT_ID
            / "structure_revisions"
            / f"{STRUCTURE_ID_1}.json"
        )
        tampered = json.loads(structure_path.read_text(encoding="utf-8"))
        tampered["modules"] = [{"module_id": "module_tampered"}]
        structure_path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(ValueError):
            store.load_structure_state(PROJECT_ID)

    def test_interrupted_publication_reuses_durable_payloads(self):
        structure, evidence = self._artifacts()
        real_atomic_write = store._atomic_write_json
        failed_once = False

        def interrupt_state(path, value):
            nonlocal failed_once
            if path.name == "structure_state.json" and not failed_once:
                failed_once = True
                raise OSError("simulated hard interruption")
            return real_atomic_write(path, value)

        with (
            patch.object(store, "_atomic_write_json", side_effect=interrupt_state),
            self.assertRaises(OSError),
        ):
            store.save_structure_bundle_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_structure_revision_id=None,
                base_evidence_revision_id=None,
                structure_revision=structure,
                evidence_revision=evidence,
                review_issues=[self._issue()],
                manual_overrides=[],
                request_fingerprint=REQUEST_1,
                effective_status="STRUCTURE_REVIEW_REQUIRED",
                updated_at="2026-08-14T00:00:02Z",
            )
        self.assertIsNone(store.load_structure_state(PROJECT_ID))

        retry_structure, retry_evidence = self._artifacts(
            created_at="2026-08-14T00:10:00Z",
            raw_content="重试生成的内容",
        )
        durable = store.save_structure_bundle_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_structure_revision_id=None,
            base_evidence_revision_id=None,
            structure_revision=retry_structure,
            evidence_revision=retry_evidence,
            review_issues=[self._issue(resolved_at="retry")],
            manual_overrides=[],
            request_fingerprint=REQUEST_1,
            effective_status="READY_FOR_DOSSIERS",
            updated_at="2026-08-14T00:10:00Z",
        )
        self.assertEqual(durable[0]["created_at"], "2026-08-14T00:00:02Z")
        self.assertEqual(
            durable[1]["entries"][0]["raw_content"],
            "玩家的私密原始回答",
        )
        current = store.load_current_structure_bundle(PROJECT_ID, IMPORT_ID)
        self.assertIsNone(current["review_issues"][0]["resolved_at"])
        self.assertEqual(
            current["state"]["effective_status"],
            "STRUCTURE_REVIEW_REQUIRED",
        )

    def test_status_head_does_not_reload_snapshot_or_locators(self):
        self._publish_initial()
        with (
            patch.object(
                store,
                "_load_accepted_bundle_for_project_locked",
                side_effect=AssertionError("snapshot hot path"),
            ),
            patch.object(
                store,
                "_evidence_locator_path",
                side_effect=AssertionError("locator hot path"),
            ),
        ):
            state = store.load_structure_state(PROJECT_ID)
        self.assertEqual(state["artifact_status"], "CURRENT")

    def test_status_poll_reuses_verified_head_and_isolates_data_roots(self):
        self._publish_initial()
        with patch.object(
            store,
            "_load_structure_head_locked",
            wraps=store._load_structure_head_locked,
        ) as full_validator:
            first = store.load_structure_state(PROJECT_ID)
            first["effective_status"] = "caller mutation"
            second = store.load_structure_state(PROJECT_ID)
            self.assertEqual(full_validator.call_count, 1)
            self.assertEqual(
                second["effective_status"], "STRUCTURE_REVIEW_REQUIRED"
            )

            with tempfile.TemporaryDirectory(prefix="iv2s-other-") as other:
                with patch.object(
                    store.config,
                    "INTERVIEW_V2_DATA_DIR",
                    Path(other) / "v2",
                ):
                    self.assertIsNone(store.load_structure_state(PROJECT_ID))

            third = store.load_structure_state(PROJECT_ID)
            self.assertEqual(full_validator.call_count, 1)
            self.assertEqual(third, second)

    def test_status_cache_invalidates_on_evidence_tamper_and_delete(self):
        self._publish_initial()
        evidence_path = store._evidence_revision_path(
            PROJECT_ID, EVIDENCE_REVISION_ID_1
        )
        original = evidence_path.read_bytes()
        with patch.object(
            store,
            "_load_structure_head_locked",
            wraps=store._load_structure_head_locked,
        ) as full_validator:
            store.load_structure_state(PROJECT_ID)
            store.load_structure_state(PROJECT_ID)
            self.assertEqual(full_validator.call_count, 1)

            tampered = json.loads(original.decode("utf-8"))
            tampered["entries"][0]["raw_content"] = "篡改后的更长证据内容"
            evidence_path.write_text(
                json.dumps(tampered, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                store.load_structure_state(PROJECT_ID)
            self.assertEqual(full_validator.call_count, 2)

            evidence_path.write_bytes(original)
            store.load_structure_state(PROJECT_ID)
            self.assertEqual(full_validator.call_count, 3)

            evidence_path.unlink()
            with self.assertRaises(ValueError):
                store.load_structure_state(PROJECT_ID)
            self.assertEqual(full_validator.call_count, 4)

    def test_locator_collision_and_tampering_fail_closed(self):
        locator_path = store._evidence_locator_path(EVIDENCE_ID)
        foreign = {
            "schema_version": "interview-safe-locator/1.0",
            "kind": "evidence",
            "entity_id": EVIDENCE_ID,
            "project_id": "project_" + "0" * 32,
            "import_id": "import_" + "0" * 32,
        }
        foreign["locator_payload_sha256"] = store._locator_payload_sha256(
            foreign
        )
        store._atomic_write_json(locator_path, foreign)
        with self.assertRaises(FileExistsError):
            self._publish_initial()
        self.assertIsNone(store.load_structure_state(PROJECT_ID))

        locator_path.unlink()
        self._publish_initial()
        locator = json.loads(locator_path.read_text(encoding="utf-8"))
        locator["project_id"] = "project_" + "0" * 32
        locator_path.write_text(json.dumps(locator), encoding="utf-8")
        with self.assertRaises(ValueError):
            store.locate_evidence(EVIDENCE_ID)

    def test_actual_service_core_and_store_round_trip(self):
        with patch("app.core.security.FEISHU_LOGIN_REQUIRED", True):
            response = structure_service.build_structure(
                IMPORT_ID,
                {
                    "base_mapping_revision_id": MAPPING_ID,
                    "base_mapping_sha256": self.mapping_sha256,
                },
                {"email": "owner@example.com", "name": "Owner"},
            )
            repeated = structure_service.build_structure(
                IMPORT_ID,
                {
                    "base_mapping_revision_id": MAPPING_ID,
                    "base_mapping_sha256": self.mapping_sha256,
                },
                {"email": "owner@example.com", "name": "Owner"},
            )

        self.assertEqual(
            response["structure_revision_id"],
            repeated["structure_revision_id"],
        )
        self.assertEqual(
            response["evidence_revision_id"],
            repeated["evidence_revision_id"],
        )
        current = store.load_current_structure_bundle(PROJECT_ID, IMPORT_ID)
        self.assertEqual(current["state"]["artifact_status"], "CURRENT")
        self.assertEqual(
            current["structure_revision"]["structure_revision_id"],
            response["structure_revision_id"],
        )


if __name__ == "__main__":
    unittest.main()
