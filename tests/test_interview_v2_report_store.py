import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.core import config
from app.storage import interview_v2_store as store


PROJECT = "project_" + "1" * 32
ANALYSIS = "analysis_" + "2" * 32
REPORT = "report_" + "3" * 32
REPORT_TWO = "report_" + "4" * 32
SECTION = "section_" + "5" * 32


class InterviewV2ReportStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="interview-v2-report-store-")
        self.patch = patch.object(config, "INTERVIEW_V2_DATA_DIR", self.temp.name)
        self.patch.start()
        analysis = {
            "analysis_run_id": ANALYSIS, "project_id": PROJECT,
            "status": "completed", "source": {}, "created_at": "2026-09-02T00:00:00Z",
        }
        analysis["revision_payload_sha256"] = store._analysis_digest(analysis)
        directory = Path(self.temp.name) / "projects" / PROJECT / "analysis_runs"
        store._atomic_write_json(directory / "versions" / f"{ANALYSIS}.json", analysis)
        store._atomic_write_json(directory / "state.json", {
            "project_id": PROJECT, "current_analysis_run_id": ANALYSIS,
            "current_version_number": 1, "history": [],
        })
        self.analysis_source_patch = patch.object(
            store, "_require_analysis_source_current_locked", return_value=None
        )
        self.analysis_source_check = self.analysis_source_patch.start()

    def tearDown(self):
        self.analysis_source_patch.stop()
        self.patch.stop()
        self.temp.cleanup()

    def _revision(
        self,
        *,
        report_version_id=REPORT,
        section_id=SECTION,
        section_revision=1,
        content="章节正文",
        locked=False,
    ):
        analysis = store.load_current_analysis_run(PROJECT)["revision"]
        return {
            "report_version_id": report_version_id,
            "report_schema_version": "interview-report/1.0",
            "source": {
                "analysis_run_id": ANALYSIS,
                "analysis_revision_payload_sha256": analysis["revision_payload_sha256"],
            },
            "status": "draft", "audit_status": "audited",
            "sections": [{
                "section_id": section_id,
                "report_version_id": report_version_id,
                "section_key": "scope_and_sample",
                "title": "研究范围与样本说明",
                "order": 1,
                "content": content,
                "content_sha256": store._canonical_payload_sha256(content),
                "section_revision": section_revision,
                "claim_ids": [],
                "locked": locked,
                "audit_status": "audit_passed",
            }],
            "claims": [], "audit_issues": [],
            "created_at": "2026-09-02T00:01:00Z",
        }

    def test_saves_immutable_report_and_loads_by_global_id(self):
        saved = store.save_report_version_cas(
            project_id=PROJECT, base_report_version_id=None, revision=self._revision()
        )
        loaded = store.load_report_version(REPORT)
        self.assertEqual(1, saved["revision"]["version_number"])
        self.assertEqual(PROJECT, loaded["project_id"])
        self.assertEqual(REPORT, loaded["revision"]["report_version_id"])

        locator = store._read_json(store._report_section_locator_path(SECTION))
        self.assertEqual(
            {"section_id", "project_id", "locator_payload_sha256"},
            set(locator),
        )
        located = store.load_current_report_for_section(SECTION)
        self.assertEqual(PROJECT, located["project_id"])
        self.assertEqual(REPORT, located["revision"]["report_version_id"])
        self.assertEqual(1, located["section"]["section_revision"])

    def test_rejects_when_analysis_head_changed(self):
        revision = self._revision()
        state_path = Path(self.temp.name) / "projects" / PROJECT / "analysis_runs" / "state.json"
        store._atomic_write_json(state_path, {
            "project_id": PROJECT,
            "current_analysis_run_id": "analysis_" + "9" * 32,
            "current_version_number": 2, "history": [],
        })
        with self.assertRaisesRegex(
            store.ReportInputConflictError, "report input changed"
        ):
            store.save_report_version_cas(
                project_id=PROJECT, base_report_version_id=None, revision=revision
            )

    def test_rejects_stale_report_head_with_explicit_conflict(self):
        store.save_report_version_cas(
            project_id=PROJECT, base_report_version_id=None, revision=self._revision()
        )
        with self.assertRaises(store.ReportHeadConflictError) as raised:
            store.save_report_version_cas(
                project_id=PROJECT,
                base_report_version_id="report_" + "9" * 32,
                revision=self._revision(report_version_id=REPORT_TWO),
            )
        self.assertEqual(REPORT, raised.exception.current_report_version_id)

    def test_rejects_stale_section_revision(self):
        store.save_report_version_cas(
            project_id=PROJECT, base_report_version_id=None, revision=self._revision()
        )
        with self.assertRaises(store.ReportSectionRevisionConflictError) as raised:
            store.save_report_version_cas(
                project_id=PROJECT,
                base_report_version_id=REPORT,
                revision=self._revision(
                    report_version_id=REPORT_TWO, section_revision=2
                ),
                section_id=SECTION,
                base_section_revision=2,
            )
        self.assertEqual(1, raised.exception.current_section_revision)

    def test_derived_revision_reuses_section_locator_and_keeps_old_version(self):
        store.save_report_version_cas(
            project_id=PROJECT, base_report_version_id=None, revision=self._revision()
        )
        locator_before = store._read_json(store._report_section_locator_path(SECTION))

        saved = store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=REPORT,
            revision=self._revision(
                report_version_id=REPORT_TWO,
                section_revision=2,
                content="人工编辑后的章节正文",
            ),
            section_id=SECTION,
            base_section_revision=1,
        )

        self.assertEqual(2, saved["revision"]["version_number"])
        self.assertEqual(
            locator_before,
            store._read_json(store._report_section_locator_path(SECTION)),
        )
        old = store.load_report_version(REPORT)
        self.assertEqual("章节正文", old["revision"]["sections"][0]["content"])
        current = store.load_current_report_for_section(SECTION)
        self.assertEqual(REPORT_TWO, current["revision"]["report_version_id"])
        self.assertEqual(2, current["section"]["section_revision"])

    def test_section_locator_digest_is_verified(self):
        store.save_report_version_cas(
            project_id=PROJECT, base_report_version_id=None, revision=self._revision()
        )
        locator_path = store._report_section_locator_path(SECTION)
        locator = store._read_json(locator_path)
        locator["locator_payload_sha256"] = "0" * 64
        store._atomic_write_json(locator_path, locator)
        with self.assertRaisesRegex(ValueError, "locator integrity check failed"):
            store.load_current_report_for_section(SECTION)

    def test_locked_section_cannot_be_overwritten_by_full_report_replacement(self):
        store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=None,
            revision=self._revision(locked=True),
        )
        replacements = []
        changed_content = self._revision(
            report_version_id=REPORT_TWO,
            locked=True,
            content="重生成正文",
        )
        replacements.append(changed_content)
        unlocked = self._revision(report_version_id=REPORT_TWO, locked=False)
        replacements.append(unlocked)
        moved = self._revision(
            report_version_id=REPORT_TWO,
            section_id="section_" + "6" * 32,
            locked=True,
        )
        replacements.append(moved)
        retitled = self._revision(report_version_id=REPORT_TWO, locked=True)
        retitled["sections"][0]["title"] = "被替换的标题"
        replacements.append(retitled)
        audit_changed = self._revision(report_version_id=REPORT_TWO, locked=True)
        audit_changed["sections"][0]["audit_status"] = "audit_failed"
        replacements.append(audit_changed)
        revision_changed = self._revision(
            report_version_id=REPORT_TWO,
            section_revision=2,
            locked=True,
        )
        replacements.append(revision_changed)
        for replacement in replacements:
            with self.subTest(section=replacement["sections"][0]):
                with self.assertRaises(store.ReportLockedSectionConflictError):
                    store.save_report_version_cas(
                        project_id=PROJECT,
                        base_report_version_id=REPORT,
                        revision=replacement,
                    )

        preserved = store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=REPORT,
            revision=self._revision(report_version_id=REPORT_TWO, locked=True),
        )
        self.assertEqual(REPORT_TWO, preserved["revision"]["report_version_id"])

    def test_locked_section_claim_and_audit_semantics_are_preserved(self):
        claim_id = "claim_" + "7" * 32
        base = self._revision(locked=True)
        base["sections"][0]["claim_ids"] = [claim_id]
        base["claims"] = [{
            "claim_id": claim_id,
            "report_version_id": REPORT,
            "section_id": SECTION,
            "section_key": "scope_and_sample",
            "section_revision": 1,
            "claim_type": "scope",
            "text": "章节正文",
            "source_span": {"start": 0, "end": 4},
            "finding_ids": [],
            "evidence_roles": [],
            "evidence_type_allowlist": [],
            "participant_ids": [],
            "evidence_ids": [],
            "stat_fact_id": None,
            "evidence_policy_version": "interview-report-claim-policy/1.0",
            "qualification_status": "passed",
            "content_sha256": store._canonical_payload_sha256({
                "text": "章节正文", "finding_ids": [],
            }),
            "audit_status": "audit_passed",
            "superseded_by": None,
        }]
        base["audit_issues"] = [{
            "audit_issue_id": "audit_" + "8" * 32,
            "code": "REPORT_TEST_WARNING",
            "severity": "warning",
            "message": "保留审计语义",
            "section_key": "scope_and_sample",
            "claim_id": claim_id,
            "source": "deterministic",
        }]
        store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=None,
            revision=base,
        )

        def replacement() -> dict:
            value = deepcopy(base)
            value["report_version_id"] = REPORT_TWO
            value["sections"][0]["report_version_id"] = REPORT_TWO
            value["claims"][0]["report_version_id"] = REPORT_TWO
            return value

        claim_changed = replacement()
        claim_changed["claims"][0]["participant_ids"] = [
            "participant_" + "9" * 32
        ]
        audit_changed = replacement()
        audit_changed["audit_issues"][0]["message"] = "审计语义被改写"
        for value in (claim_changed, audit_changed):
            with self.subTest(value=value):
                with self.assertRaises(store.ReportLockedSectionConflictError):
                    store.save_report_version_cas(
                        project_id=PROJECT,
                        base_report_version_id=REPORT,
                        revision=value,
                    )

    def test_targeted_cas_rejects_non_target_section_metadata_change(self):
        other_section_id = "section_" + "6" * 32
        base = self._revision()
        base["sections"].append({
            "section_id": other_section_id,
            "report_version_id": REPORT,
            "section_key": "core_findings",
            "title": "核心研究发现",
            "order": 2,
            "content": "另一章节",
            "content_sha256": store._canonical_payload_sha256("另一章节"),
            "section_revision": 1,
            "claim_ids": [],
            "locked": False,
            "audit_status": "audit_passed",
        })
        store.save_report_version_cas(
            project_id=PROJECT, base_report_version_id=None, revision=base
        )
        derived = deepcopy(base)
        derived["report_version_id"] = REPORT_TWO
        derived["sections"][0].update({
            "report_version_id": REPORT_TWO,
            "section_revision": 2,
            "content": "人工正文",
            "content_sha256": store._canonical_payload_sha256("人工正文"),
        })
        derived["sections"][1].update({
            "report_version_id": REPORT_TWO,
            "audit_status": "audit_failed",
        })
        with self.assertRaisesRegex(ValueError, "non-target report section changed"):
            store.save_report_version_cas(
                project_id=PROJECT,
                base_report_version_id=REPORT,
                revision=derived,
                section_id=SECTION,
                base_section_revision=1,
            )

    def test_legacy_5b_report_is_normalized_in_memory_and_locator_is_backfilled_explicitly(self):
        legacy = self._revision()
        legacy["project_id"] = PROJECT
        legacy["version_number"] = 1
        legacy["sections"][0].pop("section_revision")
        legacy["sections"][0].pop("audit_status")
        legacy["revision_payload_sha256"] = store._report_digest(legacy)
        version_path = (
            Path(self.temp.name) / "projects" / PROJECT / "reports"
            / "versions" / f"{REPORT}.json"
        )
        state_path = version_path.parents[1] / "state.json"
        store._atomic_write_json(version_path, legacy)
        store._atomic_write_json(state_path, {
            "project_id": PROJECT,
            "current_report_version_id": REPORT,
            "current_version_number": 1,
            "history": [{
                "report_version_id": REPORT,
                "version_number": 1,
                "revision_payload_sha256": legacy["revision_payload_sha256"],
            }],
        })
        store._write_or_reuse_id_locator_locked(
            path=store._report_locator_path(REPORT),
            id_field="report_version_id",
            entity_id=REPORT,
            project_id=PROJECT,
            label="report",
        )
        raw_before = version_path.read_bytes()

        located = store.load_current_report_for_section(SECTION)
        self.assertTrue(located["section_locator_missing"])
        self.assertEqual(1, located["section"]["section_revision"])
        self.assertEqual("audit_passed", located["section"]["audit_status"])
        self.assertFalse(store._report_section_locator_path(SECTION).exists())
        self.assertEqual(raw_before, version_path.read_bytes())

        store.ensure_current_report_section_locator(
            project_id=PROJECT,
            report_version_id=REPORT,
            section_id=SECTION,
        )
        derived = deepcopy(located["revision"])
        derived.pop("revision_payload_sha256", None)
        derived.pop("version_number", None)
        derived["report_version_id"] = REPORT_TWO
        derived["sections"][0].update({
            "report_version_id": REPORT_TWO,
            "section_revision": 2,
            "content": "兼容编辑后的正文",
            "content_sha256": store._canonical_payload_sha256("兼容编辑后的正文"),
        })
        saved = store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=REPORT,
            revision=derived,
            section_id=SECTION,
            base_section_revision=1,
        )
        self.assertEqual(2, saved["revision"]["sections"][0]["section_revision"])
        self.assertEqual(raw_before, version_path.read_bytes())

    def test_uncommitted_report_files_are_hidden_and_same_revision_retry_recovers(self):
        initial = store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=None,
            revision=self._revision(),
        )
        base_id = initial["revision"]["report_version_id"]
        original_write = store._atomic_write_json
        failure_kinds = ("version", "report_locator", "section_locator", "state")
        for index, kind in enumerate(failure_kinds, start=6):
            report_id = "report_" + format(index, "x") * 32
            section_id = "section_" + format(index, "x") * 32
            revision = self._revision(
                report_version_id=report_id, section_id=section_id
            )
            targets = {
                "version": store._report_dir(PROJECT) / "versions" / f"{report_id}.json",
                "report_locator": store._report_locator_path(report_id),
                "section_locator": store._report_section_locator_path(section_id),
                "state": store._report_dir(PROJECT) / "state.json",
            }
            failed = False

            def flaky_write(path, payload):
                nonlocal failed
                if not failed and Path(path) == targets[kind]:
                    failed = True
                    raise OSError(f"injected {kind} failure")
                return original_write(path, payload)

            with self.subTest(kind=kind):
                with patch.object(store, "_atomic_write_json", side_effect=flaky_write):
                    with self.assertRaisesRegex(OSError, f"injected {kind} failure"):
                        store.save_report_version_cas(
                            project_id=PROJECT,
                            base_report_version_id=base_id,
                            revision=revision,
                        )
                self.assertIsNone(store.load_report_version(report_id))
                self.assertEqual(
                    base_id,
                    store.load_current_report_version(PROJECT)["revision"][
                        "report_version_id"
                    ],
                )
                recovered = store.save_report_version_cas(
                    project_id=PROJECT,
                    base_report_version_id=base_id,
                    revision=revision,
                )
                self.assertEqual(report_id, recovered["revision"]["report_version_id"])
                replayed = store.save_report_version_cas(
                    project_id=PROJECT,
                    base_report_version_id=base_id,
                    revision=revision,
                )
                self.assertEqual(report_id, replayed["revision"]["report_version_id"])
                self.assertEqual(
                    1,
                    sum(
                        item.get("report_version_id") == report_id
                        for item in replayed["state"]["history"]
                    ),
                )
                base_id = report_id

    def test_source_staleness_is_rechecked_before_head_advance(self):
        store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=None,
            revision=self._revision(),
        )
        self.analysis_source_check.side_effect = ValueError("analysis input changed")
        with self.assertRaises(store.ReportInputConflictError):
            store.save_report_version_cas(
                project_id=PROJECT,
                base_report_version_id=REPORT,
                revision=self._revision(report_version_id=REPORT_TWO),
            )
        self.assertEqual(
            REPORT,
            store.load_current_report_version(PROJECT)["revision"]["report_version_id"],
        )

    def test_analysis_current_check_includes_frozen_upstream_sources(self):
        analysis = store.load_current_analysis_run(PROJECT)["revision"]
        self.assertTrue(store.is_analysis_run_current(
            PROJECT,
            analysis_run_id=ANALYSIS,
            revision_payload_sha256=analysis["revision_payload_sha256"],
        ))
        self.analysis_source_check.side_effect = ValueError("analysis input changed")
        self.assertFalse(store.is_analysis_run_current(
            PROJECT,
            analysis_run_id=ANALYSIS,
            revision_payload_sha256=analysis["revision_payload_sha256"],
        ))
