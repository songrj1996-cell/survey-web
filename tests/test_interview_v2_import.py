import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.services import interview_v2_import_service as service
from app.storage import interview_v2_store as store


CONTRACT_VERSION = "interview-file-contract/1.0-draft"


def _snapshot(content_hash: str) -> dict:
    return {
        "schema_version": "interview-workbook-physical-truth/1.0",
        "parser_version": "test-parser/1",
        "file_size": 12,
        "content_sha256": content_hash,
        "snapshot_sha256": "b" * 64,
        "summary": {
            "sheet_count": 1,
            "non_empty_cell_count": 4,
            "total_text_chars": 18,
            "formula_count": 0,
        },
        "sheets": [
            {
                "sheet_id": "sheet_01",
                "index": 0,
                "name": "记录页",
                "state": "visible",
                "declared_range": "A1:E2",
                "content_range": "A1:E2",
                "dimensions": {"row_count": 2, "column_count": 5},
                "hidden_rows": [],
                "hidden_columns": [],
                "merged_ranges": [],
                "cells": [
                    {
                        "address": "E2",
                        "raw_value": "不得公开的玩家原文",
                        "normalized_text": "不得公开的玩家原文",
                    }
                ],
                "candidate_participant_region": {
                    "column_range": "D:E",
                    "candidate_count": 2,
                    "raw_value": "不得公开",
                },
            }
        ],
        "warnings": [],
        "confirmation_required": [
            {
                "code": "GROUP_MAPPING_CONFIRMATION_REQUIRED",
                "message": "请确认分组。",
                "context": {
                    "sheet_id": "sheet_01",
                    "raw_text": "不得公开",
                },
            }
        ],
    }


class InterviewV2ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="interview-v2-import-")
        self.login = {"email": "owner@example.com", "name": "Owner"}
        self.patches = [
            patch.object(
                service.config,
                "INTERVIEW_V2_DATA_DIR",
                Path(self.temp_dir.name) / "interview_v2",
            ),
            patch.object(
                service.config,
                "INTERVIEW_V2_FILE_CONTRACT_VERSION",
                CONTRACT_VERSION,
            ),
            patch("app.core.security.FEISHU_LOGIN_REQUIRED", True),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def _create(self, *, key="idem-01", content=b"xlsx-content", focus="重点"):
        return service.create_upload_attempt(
            filename="records.xlsx",
            content=content,
            login=self.login,
            research_focus=focus,
            file_contract_version=CONTRACT_VERSION,
            contract_acknowledged=True,
            idempotency_key=key,
        )

    def test_acceptance_publishes_immutable_bundle_and_public_summary_is_redacted(self):
        created, should_schedule = self._create()
        self.assertTrue(should_schedule)
        self.assertEqual(created["status"], "QUARANTINED")
        attempt_id = created["upload_attempt_id"]
        digest = created["content_sha256"]

        with patch.object(
            service,
            "parse_interview_v2_workbook",
            return_value=_snapshot(digest),
        ) as parser:
            accepted = service.run_upload_precheck(attempt_id)

        parser.assert_called_once_with("records.xlsx", b"xlsx-content")
        self.assertEqual(accepted["status"], "ACCEPTED")
        self.assertFalse(store.quarantined_source_exists(attempt_id))
        self.assertEqual(
            accepted["precheck_summary"],
            {
                "file_size_bytes": 12,
                "sheet_count": 1,
                "non_empty_cell_count": 4,
                "text_char_count": 18,
                "formula_count": 0,
                "warnings": [
                    {
                        "code": "GROUP_MAPPING_CONFIRMATION_REQUIRED",
                        "message": "请确认分组。",
                        "level": "confirmation_required",
                        "retryable": False,
                        "suggested_action": "",
                        "context": {"sheet_id": "sheet_01"},
                    }
                ],
            },
        )
        public_text = repr(accepted)
        self.assertNotIn("玩家原文", public_text)
        self.assertNotIn("raw_value", public_text)
        self.assertNotIn("owner_key", public_text)

        imported = service.get_interview_import(accepted["import_id"], self.login)
        self.assertEqual(imported["status"], "GROUP_CONFIRMATION_REQUIRED")
        self.assertEqual(imported["summary"]["sheets"][0]["content_range"], "A1:E2")
        self.assertNotIn("cells", repr(imported))
        snapshot = store.load_physical_snapshot(
            accepted["project_id"], accepted["workbook_revision_id"]
        )
        self.assertEqual(
            snapshot["sheets"][0]["cells"][0]["raw_value"],
            "不得公开的玩家原文",
        )

        source_path = (
            Path(self.temp_dir.name)
            / "interview_v2"
            / "projects"
            / accepted["project_id"]
            / "workbook_revisions"
            / accepted["workbook_revision_id"]
            / "source.xlsx"
        )
        self.assertEqual(source_path.read_bytes(), b"xlsx-content")

    def test_rejected_upload_keeps_only_redacted_metadata_and_creates_no_project(self):
        created, _ = self._create(focus="敏感研究重点原文")
        error = service.InterviewV2WorkbookError(
            "WORKBOOK_CORRUPTED",
            "文件损坏。",
            context={"raw_text": "不得保留", "sheet_count": 0},
            suggested_action="repair_workbook",
        )
        with patch.object(
            service, "parse_interview_v2_workbook", side_effect=error
        ):
            rejected = service.run_upload_precheck(created["upload_attempt_id"])

        self.assertEqual(rejected["status"], "REJECTED")
        self.assertIsNone(rejected["filename"])
        self.assertEqual(rejected["error"]["code"], "WORKBOOK_CORRUPTED")
        self.assertEqual(rejected["error"]["context"], {"sheet_count": 0})
        self.assertFalse(store.quarantined_source_exists(created["upload_attempt_id"]))
        root = Path(self.temp_dir.name) / "interview_v2"
        self.assertFalse((root / "projects").exists())
        metadata = store.load_upload_attempt(created["upload_attempt_id"])
        self.assertNotIn("filename", metadata)
        self.assertNotIn("research_focus", metadata)
        self.assertNotIn("records.xlsx", repr(metadata))
        self.assertNotIn("敏感研究重点原文", repr(metadata))
        self.assertNotIn("不得保留", repr(metadata))

    def test_publish_then_finalize_failure_recovers_without_duplicate_project(self):
        created, _ = self._create()
        digest = created["content_sha256"]
        real_finalize = store.finalize_upload_attempt_accepted

        with (
            patch.object(
                service,
                "parse_interview_v2_workbook",
                return_value=_snapshot(digest),
            ),
            patch.object(
                store,
                "finalize_upload_attempt_accepted",
                side_effect=OSError("simulated finalize failure"),
            ),
        ):
            interrupted = service.run_upload_precheck(created["upload_attempt_id"])

        self.assertEqual(interrupted["status"], "PRECHECKING")
        self.assertIsNone(interrupted["project_id"])
        internal = store.load_upload_attempt(created["upload_attempt_id"])
        self.assertTrue(
            store.accepted_bundle_exists(
                internal["project_id"],
                internal["workbook_revision_id"],
                internal["import_id"],
            )
        )
        self.assertTrue(store.quarantined_source_exists(created["upload_attempt_id"]))

        with patch.object(
            store,
            "finalize_upload_attempt_accepted",
            wraps=real_finalize,
        ) as finalize:
            recovered = service.run_upload_precheck(created["upload_attempt_id"])

        self.assertEqual(recovered["status"], "ACCEPTED")
        self.assertEqual(recovered["project_id"], internal["project_id"])
        self.assertFalse(store.quarantined_source_exists(created["upload_attempt_id"]))
        finalize.assert_called_once()
        projects = list(
            (Path(self.temp_dir.name) / "interview_v2" / "projects").iterdir()
        )
        self.assertEqual(len(projects), 1)

    def test_active_precheck_lease_prevents_duplicate_publish(self):
        created, _ = self._create()
        digest = created["content_sha256"]
        parsing_started = threading.Event()
        release_parser = threading.Event()

        def slow_parse(_filename, _content):
            parsing_started.set()
            self.assertTrue(release_parser.wait(timeout=5))
            return _snapshot(digest)

        with (
            patch.object(
                service, "parse_interview_v2_workbook", side_effect=slow_parse
            ) as parser,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            first_future = executor.submit(
                service.run_upload_precheck, created["upload_attempt_id"]
            )
            self.assertTrue(parsing_started.wait(timeout=5))
            second = service.run_upload_precheck(created["upload_attempt_id"])
            internal = store.load_upload_attempt(created["upload_attempt_id"])
            self.assertEqual(second["status"], "PRECHECKING")
            self.assertIsNone(second["project_id"])
            self.assertTrue(internal["project_id"].startswith("project_"))
            release_parser.set()
            first = first_future.result(timeout=5)

        self.assertEqual(first["status"], "ACCEPTED")
        self.assertEqual(parser.call_count, 1)

    def test_same_key_reuses_attempt_and_different_content_conflicts(self):
        first, should_schedule = self._create()
        reused, reused_schedule = self._create()
        self.assertTrue(should_schedule)
        self.assertTrue(reused_schedule)
        self.assertEqual(first["upload_attempt_id"], reused["upload_attempt_id"])

        with self.assertRaises(service.InterviewV2ImportError) as raised:
            self._create(content=b"other-content")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_KEY_CONFLICT")

    def test_concurrent_same_key_claims_one_attempt(self):
        barrier = threading.Barrier(8)

        def create_once():
            barrier.wait(timeout=5)
            return self._create()

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: create_once(), range(8)))

        attempt_ids = {item[0]["upload_attempt_id"] for item in results}
        self.assertEqual(len(attempt_ids), 1)
        self.assertTrue(all(scheduled for _, scheduled in results))
        upload_dirs = list(
            (Path(self.temp_dir.name) / "interview_v2" / "upload_attempts").iterdir()
        )
        self.assertEqual(len(upload_dirs), 1)

    def test_hard_interruption_after_idempotency_claim_is_recoverable(self):
        real_write = store._atomic_write_bytes

        def interrupt_source(path, content):
            if path.name == "source.xlsx":
                raise KeyboardInterrupt("simulated process interruption")
            return real_write(path, content)

        with patch.object(store, "_atomic_write_bytes", side_effect=interrupt_source):
            with self.assertRaises(KeyboardInterrupt):
                self._create(key="interrupted-claim")

        claim = store.load_idempotency(
            "email:owner@example.com", "interrupted-claim"
        )
        self.assertIsNotNone(claim)
        self.assertIsNone(store.load_upload_attempt(claim["upload_attempt_id"]))

        recovered, should_schedule = self._create(key="interrupted-claim")
        self.assertTrue(should_schedule)
        self.assertEqual(recovered["upload_attempt_id"], claim["upload_attempt_id"])
        self.assertEqual(
            store.read_quarantined_source(recovered["upload_attempt_id"]),
            b"xlsx-content",
        )

    def test_stale_claim_cannot_reject_new_claim(self):
        created, _ = self._create(key="lease-steal")
        attempt_id = created["upload_attempt_id"]
        first, claimed = store.claim_upload_precheck(
            attempt_id,
            claim_token="claim_" + "1" * 32,
            lease_expires_at=time.time() + 60,
            project_id="project_" + "1" * 32,
            import_id="import_" + "1" * 32,
            workbook_revision_id="workbook_" + "1" * 32,
            updated_at="first",
        )
        self.assertTrue(claimed)
        first["precheck_lease_expires_at"] = 0
        store.save_upload_attempt(first)
        second, claimed = store.claim_upload_precheck(
            attempt_id,
            claim_token="claim_" + "2" * 32,
            lease_expires_at=time.time() + 60,
            project_id="project_" + "2" * 32,
            import_id="import_" + "2" * 32,
            workbook_revision_id="workbook_" + "2" * 32,
            updated_at="second",
        )
        self.assertTrue(claimed)
        self.assertEqual(second["precheck_claim_token"], "claim_" + "2" * 32)

        metadata, rejected = store.finalize_upload_attempt_rejected(
            attempt_id,
            claim_token="claim_" + "1" * 32,
            error={"code": "STALE"},
            updated_at="stale",
        )
        self.assertFalse(rejected)
        self.assertEqual(metadata["status"], "PRECHECKING")
        self.assertEqual(metadata["precheck_claim_token"], "claim_" + "2" * 32)

    def test_stale_staging_cleanup_keeps_fresh_and_unrecognized_directories(self):
        staging_root = Path(self.temp_dir.name) / "interview_v2" / ".staging"
        stale = staging_root / ("project_" + "1" * 32 + "." + "a" * 32)
        fresh = staging_root / ("project_" + "2" * 32 + "." + "b" * 32)
        unrecognized = staging_root / "do-not-delete"
        for path in (stale, fresh, unrecognized):
            path.mkdir(parents=True, exist_ok=True)
        os.utime(stale, (1, 1))

        removed = store.cleanup_stale_staging(
            older_than_epoch=time.time() - 10
        )

        self.assertEqual(removed, 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unrecognized.exists())

    def test_stale_staging_cleanup_preserves_project_with_active_claim(self):
        created, _ = self._create(key="active-stage")
        project_id = "project_" + "5" * 32
        _, claimed = store.claim_upload_precheck(
            created["upload_attempt_id"],
            claim_token="claim_" + "5" * 32,
            lease_expires_at=time.time() + 60,
            project_id=project_id,
            import_id="import_" + "5" * 32,
            workbook_revision_id="workbook_" + "5" * 32,
            updated_at="active",
        )
        self.assertTrue(claimed)

        staging_root = Path(self.temp_dir.name) / "interview_v2" / ".staging"
        active_stage = staging_root / (project_id + "." + "c" * 32)
        inactive_stage = staging_root / (
            "project_" + "6" * 32 + "." + "d" * 32
        )
        active_stage.mkdir(parents=True, exist_ok=True)
        inactive_stage.mkdir(parents=True, exist_ok=True)
        os.utime(active_stage, (1, 1))
        os.utime(inactive_stage, (1, 1))

        removed = store.cleanup_stale_staging(
            older_than_epoch=time.time() - 24 * 60 * 60
        )

        self.assertEqual(removed, 1)
        self.assertTrue(active_stage.exists())
        self.assertFalse(inactive_stage.exists())

    def test_orphaned_upload_cleanup_is_strict_and_conservative(self):
        root = Path(self.temp_dir.name) / "interview_v2" / "upload_attempts"
        stale = root / ("upload_" + "1" * 32)
        fresh = root / ("upload_" + "2" * 32)
        completed = root / ("upload_" + "3" * 32)
        unrecognized = root / "other-directory"
        for path in (stale, fresh, completed, unrecognized):
            path.mkdir(parents=True, exist_ok=True)
            (path / "source.xlsx").write_bytes(b"source")
        (completed / "metadata.json").write_text("{}", encoding="utf-8")
        os.utime(stale, (1, 1))

        removed = store.cleanup_orphaned_upload_attempts(
            older_than_epoch=time.time() - 10
        )

        self.assertEqual(removed, 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(completed.exists())
        self.assertTrue(unrecognized.exists())

    def test_idempotent_retry_reschedules_quarantined_and_stale_precheck(self):
        created, _ = self._create(key="retry-schedule")
        _, quarantined_schedule = self._create(key="retry-schedule")
        self.assertTrue(quarantined_schedule)

        metadata, claimed = store.claim_upload_precheck(
            created["upload_attempt_id"],
            claim_token="claim_" + "4" * 32,
            lease_expires_at=time.time() + 60,
            project_id="project_" + "4" * 32,
            import_id="import_" + "4" * 32,
            workbook_revision_id="workbook_" + "4" * 32,
            updated_at="active",
        )
        self.assertTrue(claimed)
        _, active_schedule = self._create(key="retry-schedule")
        self.assertFalse(active_schedule)

        metadata["precheck_lease_expires_at"] = 0
        store.save_upload_attempt(metadata)
        _, stale_schedule = self._create(key="retry-schedule")
        self.assertTrue(stale_schedule)

    def test_needs_precheck_checks_owner_and_recoverable_states(self):
        created, _ = self._create(key="needs-precheck")
        attempt_id = created["upload_attempt_id"]
        self.assertTrue(
            service.upload_attempt_needs_precheck(attempt_id, self.login)
        )

        metadata, claimed = store.claim_upload_precheck(
            attempt_id,
            claim_token="claim_" + "7" * 32,
            lease_expires_at=time.time() + 60,
            project_id="project_" + "7" * 32,
            import_id="import_" + "7" * 32,
            workbook_revision_id="workbook_" + "7" * 32,
            updated_at="active",
        )
        self.assertTrue(claimed)
        self.assertFalse(
            service.upload_attempt_needs_precheck(attempt_id, self.login)
        )

        metadata["precheck_lease_expires_at"] = 0
        store.save_upload_attempt(metadata)
        self.assertTrue(
            service.upload_attempt_needs_precheck(attempt_id, self.login)
        )

        metadata["status"] = "REJECTED"
        store.save_upload_attempt(metadata)
        self.assertFalse(
            service.upload_attempt_needs_precheck(attempt_id, self.login)
        )

        with self.assertRaises(service.InterviewV2ImportError) as raised:
            service.upload_attempt_needs_precheck(
                attempt_id, {"email": "other@example.com"}
            )
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.code, "UPLOAD_ATTEMPT_NOT_FOUND")

    def test_accepted_query_retries_quarantine_cleanup_without_downgrade(self):
        created, _ = self._create(key="accepted-cleanup")
        digest = created["content_sha256"]
        real_delete = store.delete_quarantined_source
        with (
            patch.object(
                service,
                "parse_interview_v2_workbook",
                return_value=_snapshot(digest),
            ),
            patch.object(
                store,
                "delete_quarantined_source",
                side_effect=OSError("simulated cleanup failure"),
            ),
        ):
            accepted = service.run_upload_precheck(created["upload_attempt_id"])

        self.assertEqual(accepted["status"], "ACCEPTED")
        self.assertEqual(
            store.load_upload_attempt(created["upload_attempt_id"])["status"],
            "ACCEPTED",
        )
        self.assertTrue(store.quarantined_source_exists(created["upload_attempt_id"]))
        with patch.object(
            store, "delete_quarantined_source", wraps=real_delete
        ) as cleanup:
            accepted = service.run_upload_precheck(created["upload_attempt_id"])
        self.assertEqual(accepted["status"], "ACCEPTED")
        cleanup.assert_called_once_with(created["upload_attempt_id"])
        self.assertFalse(store.quarantined_source_exists(created["upload_attempt_id"]))

    def test_rejected_cleanup_hard_interruption_is_recovered_by_retry(self):
        created, _ = self._create(key="reject-hard-interrupt")
        with (
            patch.object(
                service,
                "parse_interview_v2_workbook",
                side_effect=service.InterviewV2WorkbookError(
                    "WORKBOOK_CORRUPTED", "文件损坏。"
                ),
            ),
            patch.object(
                store,
                "delete_quarantined_source",
                side_effect=SystemExit("simulated hard interruption"),
            ),
        ):
            with self.assertRaises(SystemExit):
                service.run_upload_precheck(created["upload_attempt_id"])

        self.assertTrue(store.quarantined_source_exists(created["upload_attempt_id"]))
        persisted = store.load_upload_attempt(created["upload_attempt_id"])
        self.assertEqual(persisted["status"], "REJECTED")
        self.assertEqual(persisted["error"]["code"], "WORKBOOK_CORRUPTED")

        retried = service.run_upload_precheck(created["upload_attempt_id"])
        self.assertEqual(retried["status"], "REJECTED")
        self.assertFalse(store.quarantined_source_exists(created["upload_attempt_id"]))

    def test_rejected_cleanup_hard_interruption_is_recovered_by_post_retry(self):
        created, _ = self._create(key="reject-post-hard-interrupt")
        with (
            patch.object(
                service,
                "parse_interview_v2_workbook",
                side_effect=service.InterviewV2WorkbookError(
                    "WORKBOOK_CORRUPTED", "文件损坏。"
                ),
            ),
            patch.object(
                store,
                "delete_quarantined_source",
                side_effect=SystemExit("simulated hard interruption"),
            ),
        ):
            with self.assertRaises(SystemExit):
                service.run_upload_precheck(created["upload_attempt_id"])

        self.assertTrue(store.quarantined_source_exists(created["upload_attempt_id"]))
        retried, should_schedule = self._create(key="reject-post-hard-interrupt")
        self.assertEqual(retried["status"], "REJECTED")
        self.assertFalse(should_schedule)
        self.assertFalse(store.quarantined_source_exists(created["upload_attempt_id"]))

    def test_rejected_get_recovers_cleanup_after_ordinary_failure(self):
        created, _ = self._create(key="reject-get-cleanup")
        with (
            patch.object(
                service,
                "parse_interview_v2_workbook",
                side_effect=service.InterviewV2WorkbookError(
                    "WORKBOOK_CORRUPTED", "文件损坏。"
                ),
            ),
            patch.object(
                store,
                "delete_quarantined_source",
                side_effect=OSError("simulated cleanup failure"),
            ),
        ):
            rejected = service.run_upload_precheck(created["upload_attempt_id"])

        self.assertEqual(rejected["status"], "REJECTED")
        self.assertTrue(store.quarantined_source_exists(created["upload_attempt_id"]))
        public = service.get_upload_attempt(created["upload_attempt_id"], self.login)
        self.assertEqual(public["status"], "REJECTED")
        self.assertFalse(store.quarantined_source_exists(created["upload_attempt_id"]))
        self.assertNotIn("owner_key", public)
        self.assertNotIn("source.xlsx", repr(public))

    def test_owner_isolation_returns_not_found(self):
        created, _ = self._create()
        with self.assertRaises(service.InterviewV2ImportError) as raised:
            service.get_upload_attempt(
                created["upload_attempt_id"], {"email": "other@example.com"}
            )
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.code, "UPLOAD_ATTEMPT_NOT_FOUND")

    def test_research_focus_limit_is_explicit_and_does_not_truncate(self):
        accepted_focus = "重" * 4000
        created, _ = self._create(key="focus-ok", focus=accepted_focus)
        metadata = store.load_upload_attempt(created["upload_attempt_id"])
        self.assertEqual(metadata["research_focus"], accepted_focus)

        with self.assertRaises(service.InterviewV2ImportError) as raised:
            self._create(key="focus-too-long", focus="重" * 4001)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.code, "RESEARCH_FOCUS_TOO_LONG")

    def test_invalid_resource_ids_never_escape_storage_root(self):
        with self.assertRaises(service.InterviewV2ImportError) as raised:
            service.get_upload_attempt("../../data", self.login)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.code, "RESOURCE_ID_INVALID")


if __name__ == "__main__":
    unittest.main()
