import unittest
from copy import deepcopy

from app.core.interview_v2_report import (
    InterviewV2ReportValidationError,
    build_report_section_rerun_input,
    validate_report_section_rerun_output,
)


PROJECT = "project_" + "1" * 32
REPORT = "report_" + "2" * 32
SECTION = "section_" + "3" * 32
ANALYSIS = "analysis_" + "4" * 32
FINDING = "finding_" + "5" * 32
PARTICIPANT = "participant_" + "6" * 32
EVIDENCE = "ev_" + "7" * 32


def _revision():
    return {
        "project_id": PROJECT,
        "report_version_id": REPORT,
        "revision_payload_sha256": "a" * 64,
        "report_schema_version": "interview-report/1.0",
        "source": {
            "analysis_run_id": ANALYSIS,
            "analysis_revision_payload_sha256": "b" * 64,
            "analysis_source": {},
        },
        "input_fingerprint": "c" * 64,
        "frozen_config": {"research_focus": "入口理解"},
        "frozen_findings": [{
            "finding_id": FINDING,
            "statement": "入口可理解。",
            "supporting_cases": [{
                "participant_id": PARTICIPANT,
                "evidence_ids": [EVIDENCE],
            }],
            "counterexample_cases": [],
            "observation_cases": [],
        }],
        "frozen_stat_facts": [],
        "analysis_limitations": [],
        "sections": [{
            "section_id": SECTION,
            "section_key": "core_findings",
            "title": "核心发现",
            "order": 2,
            "section_revision": 3,
            "content": "旧正文。",
            "locked": False,
        }],
    }


def _prompt_snapshot():
    return {
        "interview_v2_report_section_rerun_system": {
            "version": 1,
            "sha256": "d" * 64,
        },
        "interview_v2_report_audit_system": {
            "version": 1,
            "sha256": "e" * 64,
        },
    }


class InterviewV2ReportRerunCoreTests(unittest.TestCase):
    def test_input_freezes_exact_base_section_prompt_and_instruction(self):
        frozen = build_report_section_rerun_input(
            report_revision=_revision(),
            section_id=SECTION,
            instruction="  保持结论简洁  ",
            prompt_snapshot=_prompt_snapshot(),
        )

        self.assertEqual(REPORT, frozen["base_report"]["report_version_id"])
        self.assertEqual(SECTION, frozen["target_section"]["section_id"])
        self.assertEqual(3, frozen["target_section"]["section_revision"])
        self.assertEqual("保持结论简洁", frozen["instruction"])
        self.assertEqual(_prompt_snapshot(), frozen["prompts"])
        self.assertRegex(frozen["input_fingerprint"], r"^[0-9a-f]{64}$")

        changed = build_report_section_rerun_input(
            report_revision=_revision(),
            section_id=SECTION,
            instruction="换一种表述",
            prompt_snapshot=_prompt_snapshot(),
        )
        self.assertNotEqual(
            frozen["input_fingerprint"], changed["input_fingerprint"]
        )

    def test_locked_or_wrong_section_is_rejected(self):
        locked = _revision()
        locked["sections"][0]["locked"] = True
        with self.assertRaisesRegex(
            InterviewV2ReportValidationError, "locked"
        ):
            build_report_section_rerun_input(
                report_revision=locked,
                section_id=SECTION,
                instruction="",
                prompt_snapshot=_prompt_snapshot(),
            )
        with self.assertRaisesRegex(
            InterviewV2ReportValidationError, "section"
        ):
            build_report_section_rerun_input(
                report_revision=_revision(),
                section_id="section_" + "9" * 32,
                instruction="",
                prompt_snapshot=_prompt_snapshot(),
            )

    def test_output_keeps_section_identity_and_increments_revision(self):
        frozen = build_report_section_rerun_input(
            report_revision=_revision(),
            section_id=SECTION,
            instruction="",
            prompt_snapshot=_prompt_snapshot(),
        )
        content = "入口可理解。"
        validated = validate_report_section_rerun_output(
            {
                "section_key": "core_findings",
                "content": content,
                "claims": [{
                    "claim_type": "finding",
                    "text": content,
                    "start": 0,
                    "end": len(content),
                    "finding_ids": [FINDING],
                    "evidence_roles": ["support"],
                    "stat_fact_id": None,
                }],
            },
            rerun_input=frozen,
            report_version_id="report_" + "8" * 32,
        )

        self.assertEqual(SECTION, validated["section"]["section_id"])
        self.assertEqual(4, validated["section"]["section_revision"])
        self.assertFalse(validated["section"]["locked"])
        self.assertEqual([EVIDENCE], validated["claims"][0]["evidence_ids"])

        wrong = deepcopy({
            "section_key": "recommendations",
            "content": content,
            "claims": [],
        })
        with self.assertRaisesRegex(
            InterviewV2ReportValidationError, "identity"
        ):
            validate_report_section_rerun_output(
                wrong,
                rerun_input=frozen,
                report_version_id="report_" + "8" * 32,
            )


if __name__ == "__main__":
    unittest.main()
