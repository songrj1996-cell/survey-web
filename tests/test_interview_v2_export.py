import unittest
from copy import deepcopy
from io import BytesIO

from docx import Document
from pydantic import ValidationError

from app.core.interview_v2_export import (
    EXPORT_MEDIA_TYPE,
    InterviewV2ExportValidationError,
    build_export_package,
)
from app.core.interview_v2_report import (
    REPORT_SCHEMA_VERSION,
    REPORT_SECTION_SPECS,
    build_report_input,
    validate_report_output,
)
from app.schemas.interview_v2_export import (
    InterviewV2ExportArtifactResponse,
    InterviewV2ExportCreateRequest,
    InterviewV2ExportManifestResponse,
)
from app.services.report_render import markdown_to_docx


PROJECT = "project_" + "1" * 32
ANALYSIS = "analysis_" + "2" * 32
REPORT = "report_" + "3" * 32
FINDING_SELF_REPORT = "finding_" + "4" * 32
FINDING_OBSERVATION = "finding_" + "5" * 32
PARTICIPANT_A = "participant_" + "6" * 32
PARTICIPANT_B = "participant_" + "7" * 32
EVIDENCE_A = "ev_" + "8" * 32
EVIDENCE_B = "ev_" + "9" * 32


def _analysis_revision():
    return {
        "analysis_run_id": ANALYSIS,
        "revision_payload_sha256": "a" * 64,
        "input_fingerprint": "b" * 64,
        "source": {},
        "status": "completed",
        "findings": [
            {
                "finding_id": FINDING_SELF_REPORT,
                "module_id": "module_" + "a" * 32,
                "title": "入口反馈",
                "statement": "入口容易理解。",
                "supporting_cases": [
                    {
                        "participant_id": PARTICIPANT_A,
                        "evidence_ids": [EVIDENCE_A],
                    }
                ],
                "counterexample_cases": [],
                "observation_cases": [],
                "stat_fact_id": None,
            },
            {
                "finding_id": FINDING_OBSERVATION,
                "module_id": "module_" + "b" * 32,
                "title": "入口行为",
                "statement": "研究员观察到入口处出现短暂停顿。",
                "supporting_cases": [],
                "counterexample_cases": [],
                "observation_cases": [
                    {
                        "participant_id": PARTICIPANT_B,
                        "evidence_ids": [EVIDENCE_B],
                    }
                ],
                "stat_fact_id": None,
            },
        ],
        "stat_facts": [],
        "limitations": [],
    }


def _writer_output():
    claim_type_by_section = {
        "scope_and_sample": "scope",
        "core_findings": "finding",
        "module_findings": "finding",
        "participant_differences": "difference",
        "participant_logics": "logic",
        "recommendations": "suggestion",
        "evidence_and_limitations": "limitation",
    }
    sections = []
    for key, _title in REPORT_SECTION_SPECS:
        if key == "scope_and_sample":
            text = "本报告覆盖已确认的访谈样本。"
            finding_ids = []
            evidence_roles = None
        elif key == "evidence_and_limitations":
            text = "结论仅代表本轮访谈范围。"
            finding_ids = []
            evidence_roles = None
        elif key == "module_findings":
            text = "研究员观察到入口处出现短暂停顿。"
            finding_ids = [FINDING_OBSERVATION]
            evidence_roles = ["observation"]
        elif key == "recommendations":
            text = "建议继续验证入口提示是否清晰。"
            finding_ids = [FINDING_SELF_REPORT]
            evidence_roles = ["support"]
        else:
            text = "玩家认为入口容易理解。"
            finding_ids = [FINDING_SELF_REPORT]
            evidence_roles = ["support"]
        claim = {
            "claim_type": claim_type_by_section[key],
            "text": text,
            "start": 0,
            "end": len(text),
            "finding_ids": finding_ids,
            "stat_fact_id": None,
        }
        if evidence_roles is not None:
            claim["evidence_roles"] = evidence_roles
        sections.append({"section_key": key, "content": text, "claims": [claim]})
    return {"sections": sections}


def _approved_report(writer_output=None):
    analysis = _analysis_revision()
    report_input = build_report_input(
        project_id=PROJECT,
        project={"research_focus": "入口体验"},
        analysis_revision=analysis,
    )
    validated = validate_report_output(
        writer_output or _writer_output(),
        report_input=report_input,
        report_version_id=REPORT,
    )
    for section in validated["sections"]:
        section["audit_status"] = "audit_passed"
    for claim in validated["claims"]:
        claim["audit_status"] = "audit_passed"
        claim["qualification_status"] = "passed"
    return {
        "project_id": PROJECT,
        "report_version_id": REPORT,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "version_number": 4,
        "revision_payload_sha256": "c" * 64,
        "source": {
            "analysis_run_id": ANALYSIS,
            "analysis_revision_payload_sha256": analysis["revision_payload_sha256"],
        },
        "input_fingerprint": report_input["input_fingerprint"],
        "frozen_config": {"research_focus": "入口体验"},
        "status": "approved",
        "audit_status": "audited",
        "sections": validated["sections"],
        "claims": validated["claims"],
        "audit_issues": validated["audit_issues"],
        "frozen_findings": report_input["findings"],
        "frozen_stat_facts": report_input["stat_facts"],
        "analysis_limitations": [],
        "model_usage": {},
        "approved_by": "owner:private-user",
        "approved_at": "2026-09-02T03:04:05Z",
    }


def _evidence_by_id():
    return {
        EVIDENCE_A: {
            "evidence_id": EVIDENCE_A,
            "participant_id": PARTICIPANT_A,
            "participant_label": "玩家甲",
            "evidence_type": "participant_self_report",
            "normalized_content": "入口说明很清楚。",
            "raw_content": "RAW-PRIVATE-A",
            "display_content": "DISPLAY-PRIVATE-A",
            "recorder_label": "RECORDER-PRIVATE-A",
            "sheet_name": "第一组访谈",
            "cell_address": "D12",
            "inclusion_status": "included",
            "identity_decision_status": "system_verified",
        },
        EVIDENCE_B: {
            "evidence_id": EVIDENCE_B,
            "participant_id": PARTICIPANT_B,
            "participant_label": "玩家乙",
            "evidence_type": "researcher_observation",
            "normalized_content": "进入页面前停顿片刻。\n随后找到入口。",
            "raw_content": "RAW-PRIVATE-B",
            "display_content": "DISPLAY-PRIVATE-B",
            "recorder_label": "RECORDER-PRIVATE-B",
            "sheet_name": "观察记录",
            "cell_address": "R9C5",
            "inclusion_status": "included",
            "identity_decision_status": "human_confirmed",
        },
    }


class InterviewV2ExportCoreTests(unittest.TestCase):
    def test_builds_deterministic_approved_report_and_evidence_appendix(self):
        report = _approved_report()
        evidence = _evidence_by_id()

        first = build_export_package(
            report,
            evidence,
            is_current_version=True,
        )
        reordered = {
            "ev_" + "f" * 32: {
                "evidence_id": "ev_" + "f" * 32,
                "raw_content": "unreferenced",
            },
            EVIDENCE_B: evidence[EVIDENCE_B],
            EVIDENCE_A: evidence[EVIDENCE_A],
        }
        second = build_export_package(
            deepcopy(report),
            reordered,
            is_current_version=True,
        )

        self.assertEqual(first, second)
        self.assertEqual("访谈研究报告_V4_已批准.docx", first["filename"])
        self.assertEqual(7, first["manifest"]["section_count"])
        self.assertEqual(7, len(first["manifest"]["section_manifest"]))
        self.assertEqual(2, first["manifest"]["evidence_count"])
        self.assertEqual(5, first["manifest"]["appendix_entry_count"])
        self.assertRegex(first["manifest_sha256"], r"^[0-9a-f]{64}$")
        InterviewV2ExportManifestResponse.model_validate(first["manifest"])

    def test_document_uses_only_confirmed_display_fields(self):
        report = _approved_report()
        package = build_export_package(
            report,
            _evidence_by_id(),
            is_current_version=True,
        )
        markdown = package["markdown"]

        for visible in (
            "正式交付稿 · 已批准",
            "报告版本：V4",
            "批准时间：2026-09-02 03:04:05 UTC",
            "证据附录",
            "玩家甲",
            "玩家乙",
            "玩家自述",
            "研究员观察",
            "入口说明很清楚。",
            "第一组访谈",
            "D12",
            "观察记录",
            "R9C5",
        ):
            self.assertIn(visible, markdown)
        for private in (
            PROJECT,
            REPORT,
            PARTICIPANT_A,
            PARTICIPANT_B,
            EVIDENCE_A,
            EVIDENCE_B,
            "owner:private-user",
            "RAW-PRIVATE-A",
            "RAW-PRIVATE-B",
            "DISPLAY-PRIVATE-A",
            "DISPLAY-PRIVATE-B",
            "RECORDER-PRIVATE-A",
            "RECORDER-PRIVATE-B",
        ):
            self.assertNotIn(private, markdown)
        for section in report["sections"]:
            self.assertIn(section["content"], markdown)

    def test_non_current_or_non_approved_report_is_blocked(self):
        with self.assertRaises(InterviewV2ExportValidationError) as stale:
            build_export_package(
                _approved_report(),
                _evidence_by_id(),
                is_current_version=False,
            )
        self.assertEqual("EXPORT_REPORT_NOT_CURRENT", stale.exception.code)

        draft = _approved_report()
        draft["status"] = "draft"
        with self.assertRaises(InterviewV2ExportValidationError) as unapproved:
            build_export_package(
                draft,
                _evidence_by_id(),
                is_current_version=True,
            )
        self.assertEqual("EXPORT_REPORT_NOT_APPROVED", unapproved.exception.code)

        legacy_audit_status = _approved_report()
        legacy_audit_status["audit_status"] = "audit_passed"
        package = build_export_package(
            legacy_audit_status,
            _evidence_by_id(),
            is_current_version=True,
        )
        self.assertEqual("approved", package["manifest"]["approval_status"])

    def test_report_is_reaudited_instead_of_trusting_cached_status(self):
        tampered = _approved_report()
        tampered["sections"][0]["content"] += "篡改"

        with self.assertRaises(InterviewV2ExportValidationError) as caught:
            build_export_package(
                tampered,
                _evidence_by_id(),
                is_current_version=True,
            )

        self.assertEqual("EXPORT_REPORT_AUDIT_BLOCKED", caught.exception.code)
        self.assertIn("deterministic", str(caught.exception))

    def test_missing_or_mismatched_evidence_is_blocked(self):
        missing = _evidence_by_id()
        missing.pop(EVIDENCE_A)
        with self.assertRaises(InterviewV2ExportValidationError) as absent:
            build_export_package(
                _approved_report(), missing, is_current_version=True
            )
        self.assertEqual("EXPORT_EVIDENCE_MISSING", absent.exception.code)

        mismatched = _evidence_by_id()
        mismatched[EVIDENCE_A]["participant_id"] = PARTICIPANT_B
        with self.assertRaises(InterviewV2ExportValidationError) as ownership:
            build_export_package(
                _approved_report(), mismatched, is_current_version=True
            )
        self.assertEqual(
            "EXPORT_EVIDENCE_OWNERSHIP_INVALID", ownership.exception.code
        )

    def test_unreviewed_or_wrong_type_evidence_is_blocked(self):
        unreviewed = _evidence_by_id()
        unreviewed[EVIDENCE_A]["identity_decision_status"] = "needs_review"
        with self.assertRaises(InterviewV2ExportValidationError) as identity:
            build_export_package(
                _approved_report(), unreviewed, is_current_version=True
            )
        self.assertEqual("EXPORT_EVIDENCE_NOT_REPORTABLE", identity.exception.code)

        wrong_type = _evidence_by_id()
        wrong_type[EVIDENCE_A]["evidence_type"] = "researcher_observation"
        with self.assertRaises(InterviewV2ExportValidationError) as evidence_type:
            build_export_package(
                _approved_report(), wrong_type, is_current_version=True
            )
        self.assertEqual("EXPORT_EVIDENCE_TYPE_INVALID", evidence_type.exception.code)

    def test_missing_safe_display_fields_are_blocked_without_id_fallback(self):
        for field in (
            "participant_label",
            "normalized_content",
            "sheet_name",
            "cell_address",
        ):
            with self.subTest(field=field):
                evidence = _evidence_by_id()
                evidence[EVIDENCE_A][field] = ""
                with self.assertRaises(InterviewV2ExportValidationError) as caught:
                    build_export_package(
                        _approved_report(), evidence, is_current_version=True
                    )
                self.assertEqual(
                    "EXPORT_EVIDENCE_DISPLAY_INVALID", caught.exception.code
                )

    def test_internal_identifiers_in_approved_prose_block_export(self):
        private_ids = (
            PARTICIPANT_A,
            "case_" + "b" * 32,
            "audit_" + "a" * 32,
            "binding_" + "c" * 32,
            "issue_" + "d" * 32,
            "occ_" + "e" * 32,
            "question_" + "f" * 32,
            "override_" + "1" * 32,
            "fact_" + "2" * 32,
            "label_" + "3" * 32,
            "label_scope_" + "4" * 32,
            "evaluation_" + "4" * 32,
            "scope_" + "5" * 32,
            "review_" + "6" * 32,
            "trace_" + "7" * 32,
            "EXPORT_" + "A" * 32,
            "sheet_001",
            "fact_candidate_1",
        )
        for private_id in private_ids:
            with self.subTest(private_id=private_id):
                writer_output = _writer_output()
                text = f"范围包含内部编号 {private_id}。"
                writer_output["sections"][0]["content"] = text
                writer_output["sections"][0]["claims"][0].update(
                    {"text": text, "end": len(text)}
                )
                report = _approved_report(writer_output)

                with self.assertRaises(InterviewV2ExportValidationError) as caught:
                    build_export_package(
                        report, _evidence_by_id(), is_current_version=True
                    )

                self.assertEqual(
                    "EXPORT_PRIVACY_BOUNDARY_VIOLATED", caught.exception.code
                )

    def test_real_docx_contains_redacted_evidence_appendix(self):
        package = build_export_package(
            _approved_report(), _evidence_by_id(), is_current_version=True
        )
        content = markdown_to_docx(package["markdown"])
        document = Document(BytesIO(content))
        rendered_text = "\n".join(
            paragraph.text for paragraph in document.paragraphs
        )

        for visible in (
            "访谈研究报告",
            "正式交付稿 · 已批准",
            "证据附录",
            "玩家甲",
            "玩家自述",
            "入口说明很清楚。",
            "第一组访谈",
            "D12",
        ):
            self.assertIn(visible, rendered_text)
        for private in (
            EVIDENCE_A,
            PARTICIPANT_A,
            "owner:private-user",
            "RAW-PRIVATE-A",
            "DISPLAY-PRIVATE-A",
            "RECORDER-PRIVATE-A",
        ):
            self.assertNotIn(private, rendered_text)


class InterviewV2ExportSchemaTests(unittest.TestCase):
    def test_create_request_accepts_only_fixed_docx_with_appendix(self):
        request = InterviewV2ExportCreateRequest.model_validate({})
        self.assertEqual("docx", request.format)
        self.assertIs(True, request.include_evidence_appendix)

        for invalid in (
            {"format": "pdf"},
            {"include_evidence_appendix": False},
            {"include_evidence_appendix": 1},
            {"unexpected": True},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    InterviewV2ExportCreateRequest.model_validate(invalid)

    def test_ready_artifact_response_is_strict_and_omits_owner(self):
        package = build_export_package(
            _approved_report(), _evidence_by_id(), is_current_version=True
        )
        response = {
            "export_artifact_id": "export_" + "d" * 32,
            "project_id": PROJECT,
            "report_version_id": REPORT,
            "report_version_number": 4,
            "status": "READY",
            "format": "docx",
            "export_profile": package["export_profile"],
            "manifest": package["manifest"],
            "manifest_sha256": package["manifest_sha256"],
            "report_revision_payload_sha256": package[
                "report_revision_payload_sha256"
            ],
            "content_sha256": "e" * 64,
            "byte_size": 1024,
            "file_name": package["filename"],
            "media_type": EXPORT_MEDIA_TYPE,
            "download_url": (
                "/api/v1/interview-export-artifacts/"
                f"{'export_' + 'd' * 32}/download"
            ),
            "created_at": "2026-09-02T03:05:06Z",
        }
        validated = InterviewV2ExportArtifactResponse.model_validate(response)
        self.assertEqual("READY", validated.status)
        self.assertFalse(hasattr(validated, "created_by"))

        for key, value in (
            ("status", "BUILDING"),
            ("file_name", "report.pdf"),
            ("byte_size", 0),
        ):
            with self.subTest(key=key):
                invalid = {**response, key: value}
                with self.assertRaises(ValidationError):
                    InterviewV2ExportArtifactResponse.model_validate(invalid)
        with self.assertRaises(ValidationError):
            InterviewV2ExportArtifactResponse.model_validate(
                {**response, "created_by": "owner:private-user"}
            )
        with self.assertRaises(ValidationError):
            InterviewV2ExportArtifactResponse.model_validate(
                {
                    **response,
                    "download_url": (
                        "/api/v1/interview-export-artifacts/"
                        f"{'export_' + 'f' * 32}/download"
                    ),
                }
            )


if __name__ == "__main__":
    unittest.main()
