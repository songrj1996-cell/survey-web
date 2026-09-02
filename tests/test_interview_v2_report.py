import unittest

from app.core.interview_v2_report import (
    REPORT_SECTION_SPECS,
    InterviewV2ReportValidationError,
    build_report_input,
    validate_report_output,
)


FINDING = "finding_" + "a" * 32
STAT = "stat_" + "b" * 32
PARTICIPANT = "participant_" + "c" * 32
EVIDENCE = "ev_" + "d" * 32
ANALYSIS = "analysis_" + "e" * 32
REPORT = "report_" + "f" * 32


def _analysis():
    return {
        "analysis_run_id": ANALYSIS,
        "revision_payload_sha256": "1" * 64,
        "input_fingerprint": "2" * 64,
        "source": {"evidence_revision_id": "evidence_" + "3" * 32},
        "status": "completed",
        "findings": [{
            "finding_id": FINDING,
            "module_id": "module_" + "4" * 32,
            "title": "入口理解",
            "statement": "多数玩家能理解入口。",
            "supporting_cases": [{"participant_id": PARTICIPANT, "evidence_ids": [EVIDENCE]}],
            "counterexample_cases": [],
            "observation_cases": [],
            "stat_fact_id": STAT,
        }],
        "stat_facts": [{
            "stat_fact_id": STAT,
            "finding_id": FINDING,
            "numerator": 1,
            "denominator": 2,
            "proportion": 0.5,
        }],
        "limitations": [],
    }


def _raw(claim_text="1/2 的玩家能理解入口。", *, stat_fact_id=STAT):
    sections = []
    claim_type_by_section = {
        "scope_and_sample": "scope", "core_findings": "finding",
        "module_findings": "finding", "participant_differences": "difference",
        "participant_logics": "logic", "recommendations": "suggestion",
        "evidence_and_limitations": "limitation",
    }
    for key, _title in REPORT_SECTION_SPECS:
        content = claim_text if key == "core_findings" else "本章暂无额外发现。"
        needs_finding = key not in {"scope_and_sample", "evidence_and_limitations"}
        claims = [{
            "claim_type": claim_type_by_section[key], "text": content,
            "start": 0, "end": len(content),
            "finding_ids": [FINDING] if needs_finding else [],
            "stat_fact_id": stat_fact_id if key == "core_findings" else None,
            "participant_ids": ["participant_" + "9" * 32],
            "evidence_ids": ["ev_" + "9" * 32],
        }]
        sections.append({"section_key": key, "content": content, "claims": claims})
    return {"sections": sections}


class InterviewV2ReportCoreTests(unittest.TestCase):
    def setUp(self):
        self.report_input = build_report_input(
            project_id="project_" + "0" * 32,
            project={"research_focus": "重点观察入口"},
            analysis_revision=_analysis(),
        )

    def test_derives_claim_links_and_accepts_stat_fact_numbers(self):
        result = validate_report_output(
            _raw(), report_input=self.report_input, report_version_id=REPORT
        )
        claim = next(item for item in result["claims"] if item["section_key"] == "core_findings")
        self.assertEqual([PARTICIPANT], claim["participant_ids"])
        self.assertEqual([EVIDENCE], claim["evidence_ids"])
        self.assertEqual("passed", claim["qualification_status"])
        self.assertFalse(any(i["severity"] == "blocking" for i in result["audit_issues"]))

    def test_number_without_stat_fact_is_blocking(self):
        result = validate_report_output(
            _raw(stat_fact_id=None), report_input=self.report_input,
            report_version_id=REPORT,
        )
        self.assertEqual("audit_failed", result["audit_status"])
        self.assertIn("REPORT_STAT_FACT_MISMATCH", {i["code"] for i in result["audit_issues"]})

    def test_observation_cannot_be_presented_as_player_self_report(self):
        analysis = _analysis()
        analysis["findings"][0]["observation_cases"] = [{
            "participant_id": PARTICIPANT, "evidence_ids": [EVIDENCE]
        }]
        report_input = build_report_input(
            project_id="project_" + "0" * 32, project={}, analysis_revision=analysis
        )
        raw = _raw("玩家表示入口很清楚。", stat_fact_id=None)
        result = validate_report_output(raw, report_input=report_input, report_version_id=REPORT)
        self.assertIn("REPORT_OBSERVATION_MISATTRIBUTED", {i["code"] for i in result["audit_issues"]})

    def test_rejects_missing_fixed_section(self):
        raw = _raw()
        raw["sections"].pop()
        with self.assertRaises(InterviewV2ReportValidationError):
            validate_report_output(raw, report_input=self.report_input, report_version_id=REPORT)

    def test_rejects_unregistered_section_prose(self):
        raw = _raw()
        raw["sections"][0]["content"] += "未登记事实。"
        with self.assertRaisesRegex(InterviewV2ReportValidationError, "unregistered"):
            validate_report_output(raw, report_input=self.report_input, report_version_id=REPORT)

    def test_derives_sample_size_stat_fact_from_frozen_dossiers(self):
        analysis = _analysis()
        analysis["source"]["dossier_versions"] = [
            {"participant_id": PARTICIPANT, "dossier_version_id": "dossier_" + "1" * 32}
        ]
        report_input = build_report_input(
            project_id="project_" + "0" * 32, project={}, analysis_revision=analysis
        )
        sample = next(
            item for item in report_input["stat_facts"]
            if item.get("metric_type") == "sample_size"
        )
        self.assertEqual(1, sample["numerator"])
        self.assertEqual([PARTICIPANT], sample["denominator_participant_ids"])
