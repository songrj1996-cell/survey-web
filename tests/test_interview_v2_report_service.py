import json
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from app.core.interview_v2_report import (
    REPORT_SCHEMA_VERSION,
    REPORT_SECTION_SPECS,
    build_report_input,
    validate_report_output,
)
from app.services import (
    interview_v2_report_review_service as review_service,
    interview_v2_report_service as service,
)
from app.services.interview_v2_import_service import InterviewV2ImportError


PROJECT = "project_" + "1" * 32
ANALYSIS = "analysis_" + "2" * 32
FINDING = "finding_" + "3" * 32
STAT = "stat_" + "4" * 32
PARTICIPANT = "participant_" + "5" * 32
EVIDENCE = "ev_" + "6" * 32


def _analysis():
    revision = {
        "analysis_run_id": ANALYSIS, "revision_payload_sha256": "a" * 64,
        "input_fingerprint": "b" * 64, "source": {}, "status": "completed",
        "findings": [{
            "finding_id": FINDING, "module_id": "module_" + "7" * 32,
            "title": "发现", "statement": "入口可理解。",
            "supporting_cases": [{"participant_id": PARTICIPANT, "evidence_ids": [EVIDENCE]}],
            "counterexample_cases": [], "observation_cases": [], "stat_fact_id": STAT,
        }],
        "stat_facts": [{
            "stat_fact_id": STAT, "finding_id": FINDING,
            "numerator": 1, "denominator": None, "proportion": None,
        }],
        "limitations": [],
    }
    return {"state": {"current_analysis_run_id": ANALYSIS}, "revision": revision}


def _writer_output():
    sections = []
    claim_type_by_section = {
        "scope_and_sample": "scope", "core_findings": "finding",
        "module_findings": "finding", "participant_differences": "difference",
        "participant_logics": "logic", "recommendations": "suggestion",
        "evidence_and_limitations": "limitation",
    }
    for key, _title in REPORT_SECTION_SPECS:
        text = "入口可理解。" if key == "core_findings" else "本章暂无额外发现。"
        claims = [{
            "claim_type": claim_type_by_section[key], "text": text,
            "start": 0, "end": len(text),
            "finding_ids": [] if key in {"scope_and_sample", "evidence_and_limitations"} else [FINDING],
            "stat_fact_id": None,
        }]
        sections.append({"section_key": key, "content": text, "claims": claims})
    return json.dumps({"sections": sections}, ensure_ascii=False)


def _saved_report():
    report_id = "report_" + "8" * 32
    analysis = _analysis()["revision"]
    report_input = build_report_input(
        project_id=PROJECT,
        project={"research_focus": "入口"},
        analysis_revision=analysis,
    )
    validated = validate_report_output(
        json.loads(_writer_output()), report_input=report_input,
        report_version_id=report_id,
    )
    revision = {
        "project_id": PROJECT,
        "report_version_id": report_id,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "version_number": 1,
        "source": {
            "analysis_run_id": ANALYSIS,
            "analysis_revision_payload_sha256": analysis["revision_payload_sha256"],
            "analysis_source": analysis["source"],
        },
        "input_fingerprint": report_input["input_fingerprint"],
        "frozen_config": {"research_focus": "入口"},
        "status": "draft",
        "audit_status": validated["audit_status"],
        "sections": validated["sections"],
        "claims": validated["claims"],
        "audit_issues": validated["audit_issues"],
        "frozen_findings": report_input["findings"],
        "frozen_stat_facts": report_input["stat_facts"],
        "analysis_limitations": [],
        "model_usage": {},
    }
    section = next(
        item for item in revision["sections"]
        if item["section_key"] == "core_findings"
    )
    return {
        "project_id": PROJECT,
        "state": {"current_report_version_id": report_id, "current_version_number": 1},
        "revision": revision,
        "section": section,
    }


def _save_result(**kwargs):
    revision = deepcopy(kwargs["revision"])
    revision["project_id"] = PROJECT
    revision["version_number"] = 2
    return {
        "state": {
            "current_report_version_id": revision["report_version_id"],
            "current_version_number": 2,
        },
        "revision": revision,
    }


class InterviewV2ReportServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_reaudit_prompt_text_and_digest_share_one_catalog_snapshot(self):
        catalog = {
            "interview_v2_report_claim_extract_system": {
                "current": "冻结提取提示词", "version": 3,
            },
            "interview_v2_report_audit_system": {
                "current": "冻结审校提示词", "version": 4,
            },
        }
        with patch.object(
            review_service, "_load_prompts", return_value=catalog
        ) as loaded:
            texts, snapshot = review_service._reaudit_prompt_snapshot()
        loaded.assert_called_once_with()
        self.assertEqual(
            "冻结提取提示词",
            texts["interview_v2_report_claim_extract_system"],
        )
        self.assertEqual(
            review_service.hashlib.sha256(
                "冻结提取提示词".encode("utf-8")
            ).hexdigest(),
            snapshot["interview_v2_report_claim_extract_system"]["sha256"],
        )
        self.assertEqual(
            4, snapshot["interview_v2_report_audit_system"]["version"]
        )

    async def test_audit_failure_persists_non_approvable_draft(self):
        current = _analysis()

        def save(**kwargs):
            revision = dict(kwargs["revision"])
            revision.update({"project_id": PROJECT, "version_number": 1})
            return {"state": {"current_report_version_id": revision["report_version_id"]}, "revision": revision}

        llm = AsyncMock(side_effect=[(_writer_output(), "writer-model"), ("not json", "audit-model")])
        with (
            patch.object(service, "get_current_analysis", return_value={"status": "completed"}),
            patch.object(service.store, "load_current_analysis_run", return_value=current),
            patch.object(service.store, "load_project", return_value={"project_id": PROJECT, "research_focus": "入口"}),
            patch.object(service, "_frozen_config", return_value={"research_focus": "入口"}),
            patch.object(service, "collect_chat_completion", llm),
            patch.object(service.store, "save_report_version_cas", side_effect=save) as save_mock,
            patch.object(service, "_get_interview_v2_report_system_prompt", return_value="writer"),
            patch.object(service, "_get_interview_v2_report_audit_system_prompt", return_value="audit"),
        ):
            result = await service.create_report(
                PROJECT, {"base_report_version_id": None, "freeze_current": True}, None
            )

        self.assertEqual("draft", result["status"])
        self.assertEqual("audit_failed", result["audit_status"])
        revision = save_mock.call_args.kwargs["revision"]
        self.assertIn("REPORT_AUDIT_INCOMPLETE", {i["code"] for i in revision["audit_issues"]})

    async def test_writer_receives_all_findings_with_research_focus(self):
        current = _analysis()

        def save(**kwargs):
            revision = dict(kwargs["revision"])
            revision.update({"project_id": PROJECT, "version_number": 1})
            return {"state": {"current_report_version_id": revision["report_version_id"]}, "revision": revision}

        llm = AsyncMock(side_effect=[(_writer_output(), "writer-model"), (json.dumps({"issues": []}), "audit-model")])
        with (
            patch.object(service, "get_current_analysis", return_value={"status": "completed"}),
            patch.object(service.store, "load_current_analysis_run", return_value=current),
            patch.object(service.store, "load_project", return_value={"project_id": PROJECT, "research_focus": "入口"}),
            patch.object(service, "_frozen_config", return_value={"research_focus": "入口"}),
            patch.object(service, "collect_chat_completion", llm),
            patch.object(service.store, "save_report_version_cas", side_effect=save),
            patch.object(service, "_get_interview_v2_report_system_prompt", return_value="writer"),
            patch.object(service, "_get_interview_v2_report_audit_system_prompt", return_value="audit"),
        ):
            await service.create_report(PROJECT, {"base_report_version_id": None}, None)

        writer_user = llm.await_args_list[0].args[0][1]["content"]
        self.assertIn("入口", writer_user)
        self.assertIn(FINDING, writer_user)

    async def test_whole_report_generation_stops_before_llm_when_section_is_locked(self):
        current = _analysis()
        locked_report = _saved_report()
        locked_report["revision"]["sections"][0]["locked"] = True
        llm = AsyncMock()
        with (
            patch.object(service, "get_current_analysis", return_value={"status": "completed"}),
            patch.object(service.store, "load_current_analysis_run", return_value=current),
            patch.object(service.store, "load_project", return_value={"project_id": PROJECT}),
            patch.object(
                service.store, "load_current_report_version", return_value=locked_report
            ),
            patch.object(service, "collect_chat_completion", llm),
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                await service.create_report(
                    PROJECT,
                    {
                        "base_report_version_id": locked_report["revision"][
                            "report_version_id"
                        ]
                    },
                    None,
                )
        self.assertEqual("REPORT_LOCKED_SECTIONS_PRESENT", caught.exception.code)
        llm.assert_not_awaited()

    async def test_manual_edit_creates_locked_pending_revision_and_supersedes_claims(self):
        saved = _saved_report()
        base_section = deepcopy(saved["section"])
        with (
            patch.object(review_service.store, "load_current_report_for_section", return_value=saved),
            patch.object(review_service.store, "load_project", return_value={"project_id": PROJECT}),
            patch.object(review_service, "_visible_to_owner", return_value=True),
            patch.object(review_service, "_is_report_current", return_value=True),
            patch.object(review_service.store, "save_report_version_cas", side_effect=_save_result) as save_mock,
        ):
            result = review_service.edit_report_section(
                saved["section"]["section_id"],
                {"base_section_revision": 1, "content": "入口仍可理解。", "locked": True, "edit_reason": "校正文案"},
                None,
            )

        self.assertEqual(2, result["section_revision"])
        self.assertEqual("pending_reaudit", result["audit_status"])
        self.assertTrue(result["locked"])
        self.assertTrue(str(result["reaudit_job_id"]).startswith("job_"))
        revision = save_mock.call_args.kwargs["revision"]
        edited = next(item for item in revision["sections"] if item["section_id"] == result["section_id"])
        self.assertEqual([], edited["claim_ids"])
        self.assertTrue(revision["superseded_claim_ids"])
        self.assertEqual("入口可理解。", base_section["content"])

    async def test_editing_approved_head_derives_new_draft_without_mutating_approval(self):
        saved = _saved_report()
        saved["revision"].update({
            "status": "approved",
            "approved_by": "email:reviewer@example.com",
            "approved_at": "2026-09-02T02:00:00Z",
        })
        approved_before = deepcopy(saved["revision"])
        with (
            patch.object(review_service.store, "load_current_report_for_section", return_value=saved),
            patch.object(review_service.store, "load_project", return_value={"project_id": PROJECT}),
            patch.object(review_service, "_visible_to_owner", return_value=True),
            patch.object(review_service, "_is_report_current", return_value=True),
            patch.object(review_service.store, "save_report_version_cas", side_effect=_save_result) as save_mock,
        ):
            result = review_service.edit_report_section(
                saved["section"]["section_id"],
                {
                    "base_section_revision": 1,
                    "content": "批准后派生的新正文。",
                    "locked": True,
                },
                None,
            )
        self.assertEqual("draft", result["status"])
        derived = save_mock.call_args.kwargs["revision"]
        self.assertNotIn("approved_by", derived)
        self.assertNotIn("approved_at", derived)
        self.assertEqual("approved", approved_before["status"])

    async def test_reaudit_reextracts_claims_and_passes_current_section(self):
        saved = _saved_report()
        with (
            patch.object(review_service.store, "load_current_report_for_section", return_value=saved),
            patch.object(review_service.store, "load_project", return_value={"project_id": PROJECT}),
            patch.object(review_service, "_visible_to_owner", return_value=True),
            patch.object(review_service, "_is_report_current", return_value=True),
            patch.object(review_service.store, "save_report_version_cas", side_effect=_save_result) as edit_save,
        ):
            edit_result = review_service.edit_report_section(
                saved["section"]["section_id"],
                {"base_section_revision": 1, "content": "入口仍可理解。", "locked": True},
                None,
            )
        pending_revision = deepcopy(edit_save.call_args.kwargs["revision"])
        pending_revision["project_id"] = PROJECT
        pending_revision["version_number"] = 2
        pending_section = next(
            item for item in pending_revision["sections"]
            if item["section_id"] == edit_result["section_id"]
        )
        pending_saved = {
            "project_id": PROJECT,
            "state": {"current_report_version_id": pending_revision["report_version_id"]},
            "revision": pending_revision,
            "section": pending_section,
        }
        extracted = json.dumps({"claims": [{
            "claim_type": "finding", "text": "入口仍可理解。", "start": 0,
            "end": len("入口仍可理解。"), "finding_ids": [FINDING], "stat_fact_id": None,
        }]}, ensure_ascii=False)
        llm = AsyncMock(side_effect=[(extracted, "extract-model"), (json.dumps({"issues": []}), "audit-model")])
        with (
            patch.object(review_service.store, "load_current_report_for_section", return_value=pending_saved),
            patch.object(review_service.store, "load_project", return_value={"project_id": PROJECT}),
            patch.object(review_service, "_visible_to_owner", return_value=True),
            patch.object(review_service, "_is_report_current", return_value=True),
            patch.object(review_service, "collect_chat_completion", llm),
            patch.object(
                review_service,
                "_reaudit_prompt_snapshot",
                return_value=(
                    {
                        "interview_v2_report_claim_extract_system": "extract",
                        "interview_v2_report_audit_system": "audit",
                    },
                    {},
                ),
            ),
            patch.object(review_service.store, "save_report_version_cas", side_effect=_save_result) as reaudit_save,
        ):
            result = await review_service.reaudit_report_section(
                pending_section["section_id"],
                {"base_section_revision": 2, "reaudit_job_id": edit_result["reaudit_job_id"]},
                None,
            )

        self.assertEqual(3, result["section_revision"])
        self.assertEqual("audit_passed", result["audit_status"])
        self.assertIsNone(result["reaudit_job_id"])
        revision = reaudit_save.call_args.kwargs["revision"]
        claim = next(item for item in revision["claims"] if item["section_id"] == pending_section["section_id"])
        self.assertEqual("audit_passed", claim["audit_status"])
        self.assertEqual([PARTICIPANT], claim["participant_ids"])

    async def test_transient_reaudit_failure_keeps_same_job_retryable(self):
        saved = _saved_report()
        section = saved["section"]
        section.update({
            "section_revision": 2,
            "content": "入口仍可理解。",
            "audit_status": "pending_reaudit",
            "locked": True,
            "reaudit_job_id": "job_" + "9" * 32,
            "edited_by": "owner@example.com",
            "edited_at": "2026-09-02T01:00:00Z",
            "edit_reason": "人工校正",
            "claim_ids": [],
        })
        section["content_sha256"] = review_service.payload_sha256(section["content"])
        saved["revision"]["claims"] = [
            item for item in saved["revision"]["claims"]
            if item.get("section_id") != section["section_id"]
        ]
        saved["revision"]["audit_status"] = "pending_reaudit"
        prompt_bundle = (
            {
                "interview_v2_report_claim_extract_system": "extract",
                "interview_v2_report_audit_system": "audit",
            },
            {},
        )
        with (
            patch.object(review_service.store, "load_current_report_for_section", return_value=saved),
            patch.object(review_service.store, "load_project", return_value={"project_id": PROJECT}),
            patch.object(review_service, "_visible_to_owner", return_value=True),
            patch.object(review_service, "_is_report_current", return_value=True),
            patch.object(review_service, "_reaudit_prompt_snapshot", return_value=prompt_bundle),
            patch.object(
                review_service, "collect_chat_completion",
                new=AsyncMock(side_effect=TimeoutError("gateway timeout")),
            ),
            patch.object(review_service.store, "save_report_version_cas", side_effect=_save_result) as failed_save,
        ):
            failed = await review_service.reaudit_report_section(
                section["section_id"],
                {
                    "base_section_revision": 2,
                    "reaudit_job_id": section["reaudit_job_id"],
                },
                None,
            )
        self.assertEqual("pending_reaudit", failed["audit_status"])
        self.assertEqual(section["reaudit_job_id"], failed["reaudit_job_id"])
        failed_revision = deepcopy(failed_save.call_args.kwargs["revision"])
        failed_revision["project_id"] = PROJECT
        failed_section = next(
            item for item in failed_revision["sections"]
            if item["section_id"] == section["section_id"]
        )
        retry_saved = {
            "project_id": PROJECT,
            "state": {"current_report_version_id": failed_revision["report_version_id"]},
            "revision": failed_revision,
            "section": failed_section,
        }
        extracted = json.dumps({"claims": [{
            "claim_type": "finding", "text": section["content"],
            "start": 0, "end": len(section["content"]),
            "finding_ids": [FINDING], "stat_fact_id": None,
        }]}, ensure_ascii=False)
        with (
            patch.object(review_service.store, "load_current_report_for_section", return_value=retry_saved),
            patch.object(review_service.store, "load_project", return_value={"project_id": PROJECT}),
            patch.object(review_service, "_visible_to_owner", return_value=True),
            patch.object(review_service, "_is_report_current", return_value=True),
            patch.object(review_service, "_reaudit_prompt_snapshot", return_value=prompt_bundle),
            patch.object(
                review_service, "collect_chat_completion",
                new=AsyncMock(side_effect=[
                    (extracted, "extract-model"),
                    (json.dumps({"issues": []}), "audit-model"),
                ]),
            ),
            patch.object(review_service.store, "save_report_version_cas", side_effect=_save_result) as retry_save,
        ):
            retried = await review_service.reaudit_report_section(
                section["section_id"],
                {
                    "base_section_revision": 3,
                    "reaudit_job_id": section["reaudit_job_id"],
                },
                None,
            )
        self.assertEqual("audit_passed", retried["audit_status"])
        self.assertIsNone(retried["reaudit_job_id"])
        retried_section = next(
            item for item in retry_save.call_args.kwargs["revision"]["sections"]
            if item["section_id"] == section["section_id"]
        )
        self.assertEqual("入口仍可理解。", retried_section["content"])
        self.assertEqual("人工校正", retried_section["edit_reason"])
        self.assertEqual(1, retried_section["reaudit_retry_count"])

    async def test_pending_report_cannot_be_approved(self):
        saved = _saved_report()
        saved["revision"]["audit_status"] = "pending_reaudit"
        saved["revision"]["sections"][0]["audit_status"] = "pending_reaudit"
        with (
            patch.object(review_service, "_load_accessible_report", return_value=saved),
            patch.object(review_service, "_is_report_current", return_value=True),
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                review_service.approve_report(
                    saved["revision"]["report_version_id"],
                    {"base_report_version_id": saved["revision"]["report_version_id"], "decision": "approved"},
                    None,
                )
        self.assertEqual("REPORT_CLAIMS_PENDING_AUDIT", caught.exception.code)

    async def test_stale_upstream_blocks_approval_before_save(self):
        saved = _saved_report()
        with (
            patch.object(review_service, "_load_accessible_report", return_value=saved),
            patch.object(review_service, "_is_report_current", return_value=False),
            patch.object(review_service.store, "save_report_version_cas") as save_mock,
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                review_service.approve_report(
                    saved["revision"]["report_version_id"],
                    {
                        "base_report_version_id": saved["revision"]["report_version_id"],
                        "decision": "approved",
                    },
                    None,
                )
        self.assertEqual("REPORT_INPUT_CHANGED", caught.exception.code)
        save_mock.assert_not_called()

    async def test_approval_revalidates_and_creates_immutable_approved_version(self):
        saved = _saved_report()
        with (
            patch.object(review_service, "_load_accessible_report", return_value=saved),
            patch.object(review_service, "_is_report_current", return_value=True),
            patch.object(review_service.store, "save_report_version_cas", side_effect=_save_result) as save_mock,
        ):
            result = review_service.approve_report(
                saved["revision"]["report_version_id"],
                {
                    "base_report_version_id": saved["revision"]["report_version_id"],
                    "decision": "approved", "note": "研究员确认",
                },
                None,
            )

        self.assertEqual("approved", result["status"])
        self.assertTrue(result["is_current_version"])
        approved = save_mock.call_args.kwargs["revision"]
        self.assertEqual("approved", approved["status"])
        self.assertEqual(saved["revision"]["report_version_id"], approved["approved_from_report_version_id"])
        self.assertEqual("draft", saved["revision"]["status"])

    async def test_legacy_5b_report_can_be_approved_after_in_memory_normalization(self):
        saved = _saved_report()
        legacy = deepcopy(saved["revision"])
        for section in legacy["sections"]:
            section.pop("section_revision", None)
            section.pop("audit_status", None)
        for claim in legacy["claims"]:
            claim.pop("section_revision", None)
            claim.pop("audit_status", None)
            claim.pop("superseded_by", None)
            claim.pop("evidence_roles", None)
            claim.pop("evidence_type_allowlist", None)
        saved["revision"] = review_service.store._normalize_legacy_report_revision(
            legacy
        )
        normalized_claim = next(
            item for item in saved["revision"]["claims"]
            if item["section_key"] == "core_findings"
        )
        self.assertEqual(["support"], normalized_claim["evidence_roles"])
        self.assertEqual(
            ["participant_self_report"],
            normalized_claim["evidence_type_allowlist"],
        )
        with (
            patch.object(review_service, "_load_accessible_report", return_value=saved),
            patch.object(review_service, "_is_report_current", return_value=True),
            patch.object(
                review_service.store, "save_report_version_cas", side_effect=_save_result
            ),
        ):
            result = review_service.approve_report(
                saved["revision"]["report_version_id"],
                {
                    "base_report_version_id": saved["revision"]["report_version_id"],
                    "decision": "approved",
                },
                None,
            )
        self.assertEqual("approved", result["status"])

    def test_legacy_5b_observation_and_mixed_roles_are_inferred_from_evidence(self):
        for evidence_roles, supporting_cases, text in (
            (
                ["observation"],
                [],
                "研究员观察到入口操作受阻。",
            ),
            (
                ["support", "observation"],
                [{
                    "participant_id": "participant_" + "9" * 32,
                    "evidence_ids": ["ev_" + "a" * 32],
                }],
                "研究员观察到入口操作受阻并记录到路径绕行。",
            ),
        ):
            with self.subTest(evidence_roles=evidence_roles):
                analysis = deepcopy(_analysis()["revision"])
                legacy_finding_id = "finding_" + "b" * 32
                analysis["findings"].append({
                    "finding_id": legacy_finding_id,
                    "module_id": "module_" + "c" * 32,
                    "title": "旧版观察发现",
                    "statement": text,
                    "supporting_cases": supporting_cases,
                    "counterexample_cases": [],
                    "observation_cases": [{
                        "participant_id": "participant_" + "d" * 32,
                        "evidence_ids": ["ev_" + "e" * 32],
                    }],
                    "stat_fact_id": None,
                })
                report_input = build_report_input(
                    project_id=PROJECT,
                    project={"research_focus": "入口"},
                    analysis_revision=analysis,
                )
                raw = json.loads(_writer_output())
                core = next(
                    item for item in raw["sections"]
                    if item["section_key"] == "core_findings"
                )
                core["content"] = text
                core["claims"] = [{
                    "claim_type": "finding",
                    "text": text,
                    "start": 0,
                    "end": len(text),
                    "finding_ids": [legacy_finding_id],
                    "evidence_roles": evidence_roles,
                    "stat_fact_id": None,
                }]
                report_id = "report_" + "8" * 32
                validated = validate_report_output(
                    raw,
                    report_input=report_input,
                    report_version_id=report_id,
                )
                legacy = {
                    "project_id": PROJECT,
                    "report_version_id": report_id,
                    "report_schema_version": REPORT_SCHEMA_VERSION,
                    "status": "draft",
                    "audit_status": validated["audit_status"],
                    "sections": validated["sections"],
                    "claims": validated["claims"],
                    "audit_issues": validated["audit_issues"],
                    "frozen_findings": report_input["findings"],
                    "frozen_stat_facts": report_input["stat_facts"],
                }
                for section in legacy["sections"]:
                    section.pop("section_revision", None)
                    section.pop("audit_status", None)
                for claim in legacy["claims"]:
                    claim.pop("section_revision", None)
                    claim.pop("audit_status", None)
                    claim.pop("superseded_by", None)
                    claim.pop("evidence_roles", None)
                    claim.pop("evidence_type_allowlist", None)

                normalized = review_service.store._normalize_legacy_report_revision(
                    legacy
                )
                normalized_core = next(
                    item for item in normalized["claims"]
                    if item["section_key"] == "core_findings"
                )
                self.assertEqual(evidence_roles, normalized_core["evidence_roles"])
                self.assertEqual(
                    "audit_passed",
                    review_service.validate_report_approval(normalized)["audit_status"],
                )

    def test_legacy_claim_that_fails_new_policy_is_marked_pending(self):
        saved = _saved_report()
        legacy = deepcopy(saved["revision"])
        counter_participant = "participant_" + "f" * 32
        counter_evidence = "ev_" + "0" * 32
        legacy["frozen_findings"][0]["counterexample_cases"] = [{
            "participant_id": counter_participant,
            "evidence_ids": [counter_evidence],
        }]
        for section in legacy["sections"]:
            section.pop("section_revision", None)
            section.pop("audit_status", None)
        for claim in legacy["claims"]:
            if claim.get("finding_ids"):
                claim["participant_ids"] = sorted(
                    [*claim.get("participant_ids", []), counter_participant]
                )
                claim["evidence_ids"] = sorted(
                    [*claim.get("evidence_ids", []), counter_evidence]
                )
            if claim.get("section_key") == "core_findings":
                claim["stat_fact_id"] = STAT
            claim.pop("section_revision", None)
            claim.pop("audit_status", None)
            claim.pop("superseded_by", None)
            claim.pop("evidence_roles", None)
            claim.pop("evidence_type_allowlist", None)

        normalized = review_service.store._normalize_legacy_report_revision(legacy)
        core_section = next(
            item for item in normalized["sections"]
            if item["section_key"] == "core_findings"
        )
        self.assertEqual("pending_reaudit", normalized["audit_status"])
        self.assertEqual("pending_reaudit", core_section["audit_status"])
        with self.assertRaisesRegex(
            review_service.InterviewV2ReportValidationError, "pending"
        ):
            review_service.validate_report_approval(normalized)
