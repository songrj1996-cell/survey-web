import unittest

from app.core.interview_v2_report import (
    REPORT_SCHEMA_VERSION,
    REPORT_SECTION_SPECS,
    InterviewV2ReportValidationError,
    build_report_input,
    validate_report_approval,
    validate_report_output,
    validate_report_section_output,
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


def _raw(
    claim_text="1/2 的玩家能理解入口。",
    *,
    stat_fact_id=STAT,
    evidence_roles=None,
):
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
        if key == "core_findings" and evidence_roles is not None:
            claims[0]["evidence_roles"] = evidence_roles
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
        self.assertEqual(1, claim["section_revision"])
        self.assertEqual("audit_passed", claim["audit_status"])
        self.assertIsNone(claim["superseded_by"])
        self.assertTrue(all(item["section_revision"] == 1 for item in result["sections"]))
        self.assertTrue(all(item["audit_status"] == "audit_passed" for item in result["sections"]))
        self.assertFalse(any(i["severity"] == "blocking" for i in result["audit_issues"]))

    def test_number_without_stat_fact_is_blocking(self):
        result = validate_report_output(
            _raw(stat_fact_id=None), report_input=self.report_input,
            report_version_id=REPORT,
        )
        self.assertEqual("audit_failed", result["audit_status"])
        self.assertIn("REPORT_STAT_FACT_MISMATCH", {i["code"] for i in result["audit_issues"]})

    def test_chinese_participant_count_without_matching_stat_is_blocking(self):
        result = validate_report_output(
            _raw("三名玩家能理解入口。", stat_fact_id=STAT),
            report_input=self.report_input,
            report_version_id=REPORT,
        )
        self.assertIn(
            "REPORT_STAT_FACT_MISMATCH",
            {item["code"] for item in result["audit_issues"]},
        )

    def test_chinese_fraction_and_vague_majority_must_match_proportion(self):
        analysis = _analysis()
        analysis["stat_facts"][0].update({
            "numerator": 1, "denominator": 10, "proportion": 0.1,
        })
        report_input = build_report_input(
            project_id="project_" + "0" * 32,
            project={},
            analysis_revision=analysis,
        )
        for text in ("一半玩家能理解入口。", "大多数玩家能理解入口。"):
            with self.subTest(text=text):
                result = validate_report_output(
                    _raw(text, stat_fact_id=STAT),
                    report_input=report_input,
                    report_version_id=REPORT,
                )
                self.assertIn(
                    "REPORT_STAT_FACT_MISMATCH",
                    {item["code"] for item in result["audit_issues"]},
                )

    def test_universal_zero_and_relative_quantities_require_matching_stat(self):
        cases = (
            ("所有玩家都支持入口。", 1.0, 0.5),
            ("全部玩家支持入口。", 1.0, 0.5),
            ("每位受访者都支持入口。", 1.0, 0.5),
            ("每个玩家都支持入口。", 1.0, 0.5),
            ("全员支持入口。", 1.0, 0.5),
            ("人人都支持入口。", 1.0, 0.5),
            ("无人反对入口。", 0.0, 0.5),
            ("没有玩家反对入口。", 0.0, 0.5),
            ("没有一名玩家反对入口。", 0.0, 0.1),
            ("无一玩家反对入口。", 0.0, 0.1),
            ("一个玩家也没有反对入口。", 0.0, 0.1),
            ("无一人反对入口。", 0.0, 0.1),
            ("并无玩家反对入口。", 0.0, 0.1),
            ("大部分玩家支持入口。", 0.6, 0.5),
            ("不到半数玩家支持入口。", 0.4, 0.5),
            ("至少半数玩家支持入口。", 0.5, 0.4),
            ("一半以上玩家支持入口。", 0.5, 0.4),
            ("一半以下玩家支持入口。", 0.5, 0.6),
            ("不超过半数玩家支持入口。", 0.5, 0.6),
            ("不少于半数玩家支持入口。", 0.5, 0.4),
            ("约半数玩家支持入口。", 0.5, 0.8),
        )
        for text, valid_proportion, invalid_proportion in cases:
            with self.subTest(text=text, state="missing"):
                missing = validate_report_output(
                    _raw(text, stat_fact_id=None),
                    report_input=self.report_input,
                    report_version_id=REPORT,
                )
                self.assertIn(
                    "REPORT_STAT_FACT_MISMATCH",
                    {item["code"] for item in missing["audit_issues"]},
                )
            for state, proportion, expected_mismatch in (
                ("valid", valid_proportion, False),
                ("invalid", invalid_proportion, True),
            ):
                with self.subTest(text=text, state=state):
                    analysis = _analysis()
                    analysis["stat_facts"][0].update({
                        "numerator": round(proportion * 10),
                        "denominator": 10,
                        "proportion": proportion,
                    })
                    report_input = build_report_input(
                        project_id="project_" + "0" * 32,
                        project={},
                        analysis_revision=analysis,
                    )
                    result = validate_report_output(
                        _raw(text, stat_fact_id=STAT),
                        report_input=report_input,
                        report_version_id=REPORT,
                    )
                    codes = {item["code"] for item in result["audit_issues"]}
                    self.assertEqual(
                        expected_mismatch,
                        "REPORT_STAT_FACT_MISMATCH" in codes,
                    )

    def test_participant_count_cannot_use_denominator_as_support_count(self):
        analysis = _analysis()
        analysis["stat_facts"][0].update({
            "numerator": 3, "denominator": 10, "proportion": 0.3,
        })
        report_input = build_report_input(
            project_id="project_" + "0" * 32,
            project={},
            analysis_revision=analysis,
        )
        for text in (
            "10名玩家支持入口。", "10名参与者支持入口。",
            "10个支持者认可入口。", "共10份反馈支持入口。", "10票支持入口。",
        ):
            with self.subTest(invalid_text=text):
                invalid = validate_report_output(
                    _raw(text, stat_fact_id=STAT),
                    report_input=report_input,
                    report_version_id=REPORT,
                )
                self.assertIn(
                    "REPORT_STAT_FACT_MISMATCH",
                    {item["code"] for item in invalid["audit_issues"]},
                )
        for text in (
            "3名玩家支持入口。", "3/10 的玩家支持入口。",
            "10名玩家中3名支持入口。",
            "样本包含10名玩家，其中3名支持入口。",
        ):
            with self.subTest(text=text):
                valid = validate_report_output(
                    _raw(text, stat_fact_id=STAT),
                    report_input=report_input,
                    report_version_id=REPORT,
                )
                self.assertNotIn(
                    "REPORT_STAT_FACT_MISMATCH",
                    {item["code"] for item in valid["audit_issues"]},
                )

    def test_labeled_chinese_counts_bind_numerator_and_denominator(self):
        analysis = _analysis()
        analysis["stat_facts"][0].update({
            "numerator": 3, "denominator": 10, "proportion": 0.3,
        })
        report_input = build_report_input(
            project_id="project_" + "0" * 32,
            project={},
            analysis_revision=analysis,
        )
        missing = validate_report_output(
            _raw("支持人数为三，样本人数为十。", stat_fact_id=None),
            report_input=report_input,
            report_version_id=REPORT,
        )
        self.assertIn(
            "REPORT_STAT_FACT_MISMATCH",
            {item["code"] for item in missing["audit_issues"]},
        )
        valid = validate_report_output(
            _raw("支持人数为三，样本人数为十。", stat_fact_id=STAT),
            report_input=report_input,
            report_version_id=REPORT,
        )
        self.assertNotIn(
            "REPORT_STAT_FACT_MISMATCH",
            {item["code"] for item in valid["audit_issues"]},
        )
        reversed_counts = validate_report_output(
            _raw("支持人数为十，样本人数为三。", stat_fact_id=STAT),
            report_input=report_input,
            report_version_id=REPORT,
        )
        self.assertIn(
            "REPORT_STAT_FACT_MISMATCH",
            {item["code"] for item in reversed_counts["audit_issues"]},
        )

    def test_percentage_comparators_are_checked_strictly(self):
        equal_analysis = _analysis()
        equal_analysis["stat_facts"][0].update({
            "numerator": 3, "denominator": 10, "proportion": 0.3,
        })
        equal_input = build_report_input(
            project_id="project_" + "0" * 32,
            project={},
            analysis_revision=equal_analysis,
        )
        for text in (
            "超过30%的玩家支持入口。", "不足30%的玩家支持入口。",
            "不到30%的玩家支持入口。", "超过三成玩家支持入口。",
            "不足三成玩家支持入口。", "不到三成玩家支持入口。",
        ):
            with self.subTest(text=text, state="equal"):
                result = validate_report_output(
                    _raw(text, stat_fact_id=STAT),
                    report_input=equal_input,
                    report_version_id=REPORT,
                )
                self.assertIn(
                    "REPORT_STAT_FACT_MISMATCH",
                    {item["code"] for item in result["audit_issues"]},
                )
        for text, numerator, proportion in (
            ("超过30%的玩家支持入口。", 31, 0.31),
            ("不足30%的玩家支持入口。", 29, 0.29),
            ("不到30%的玩家支持入口。", 29, 0.29),
            ("超过三成玩家支持入口。", 31, 0.31),
            ("不足三成玩家支持入口。", 29, 0.29),
            ("不到三成玩家支持入口。", 29, 0.29),
            ("至少30%的玩家支持入口。", 30, 0.3),
            ("不超过30%的玩家支持入口。", 30, 0.3),
        ):
            with self.subTest(text=text, state="valid"):
                analysis = _analysis()
                analysis["stat_facts"][0].update({
                    "numerator": numerator,
                    "denominator": 100,
                    "proportion": proportion,
                })
                report_input = build_report_input(
                    project_id="project_" + "0" * 32,
                    project={},
                    analysis_revision=analysis,
                )
                result = validate_report_output(
                    _raw(text, stat_fact_id=STAT),
                    report_input=report_input,
                    report_version_id=REPORT,
                )
                self.assertNotIn(
                    "REPORT_STAT_FACT_MISMATCH",
                    {item["code"] for item in result["audit_issues"]},
                )

    def test_player_display_id_is_not_treated_as_quantity(self):
        revision = self._approvable_revision(
            raw=_raw("P07 表示入口容易理解。", stat_fact_id=None)
        )
        self.assertEqual(
            "audit_passed", validate_report_approval(revision)["audit_status"]
        )

    def test_equivalent_ratio_and_percentage_notation_is_accepted(self):
        analysis = _analysis()
        analysis["stat_facts"][0].update({
            "numerator": 3, "denominator": 10, "proportion": 0.3,
        })
        report_input = build_report_input(
            project_id="project_" + "0" * 32,
            project={},
            analysis_revision=analysis,
        )
        for text in (
            "支持人数为3，样本人数为10。",
            "3／10 的玩家支持入口。",
            "3比10 的玩家支持入口。",
            "30.00%的玩家支持入口。",
        ):
            with self.subTest(text=text):
                result = validate_report_output(
                    _raw(text, stat_fact_id=STAT),
                    report_input=report_input,
                    report_version_id=REPORT,
                )
                self.assertNotIn(
                    "REPORT_STAT_FACT_MISMATCH",
                    {item["code"] for item in result["audit_issues"]},
                )

    def test_count_comparators_are_checked_strictly(self):
        for text, numerator, should_pass in (
            ("超过3名玩家支持入口。", 3, False),
            ("超过3名玩家支持入口。", 4, True),
            ("不足3名玩家支持入口。", 3, False),
            ("不足3名玩家支持入口。", 2, True),
        ):
            with self.subTest(text=text, numerator=numerator):
                analysis = _analysis()
                analysis["stat_facts"][0].update({
                    "numerator": numerator,
                    "denominator": 10,
                    "proportion": numerator / 10,
                })
                report_input = build_report_input(
                    project_id="project_" + "0" * 32,
                    project={},
                    analysis_revision=analysis,
                )
                result = validate_report_output(
                    _raw(text, stat_fact_id=STAT),
                    report_input=report_input,
                    report_version_id=REPORT,
                )
                mismatch = "REPORT_STAT_FACT_MISMATCH" in {
                    item["code"] for item in result["audit_issues"]
                }
                self.assertEqual(should_pass, not mismatch)

    def test_mixed_finding_defaults_to_support_evidence_only(self):
        analysis = _analysis()
        analysis["findings"][0]["observation_cases"] = [{
            "participant_id": "participant_" + "8" * 32,
            "evidence_ids": ["ev_" + "7" * 32],
        }]
        report_input = build_report_input(
            project_id="project_" + "0" * 32, project={}, analysis_revision=analysis
        )
        raw = _raw("玩家表示入口很清楚。", stat_fact_id=None)
        result = validate_report_output(raw, report_input=report_input, report_version_id=REPORT)
        claim = next(
            item for item in result["claims"]
            if item["section_key"] == "core_findings"
        )
        self.assertEqual(["support"], claim["evidence_roles"])
        self.assertEqual(["participant_self_report"], claim["evidence_type_allowlist"])
        self.assertEqual([PARTICIPANT], claim["participant_ids"])
        self.assertEqual([EVIDENCE], claim["evidence_ids"])
        self.assertNotIn(
            "REPORT_OBSERVATION_MISATTRIBUTED",
            {item["code"] for item in result["audit_issues"]},
        )

    def test_observation_role_derives_only_observation_evidence(self):
        analysis = _analysis()
        observation_participant = "participant_" + "8" * 32
        observation_evidence = "ev_" + "7" * 32
        analysis["findings"][0]["observation_cases"] = [{
            "participant_id": observation_participant,
            "evidence_ids": [observation_evidence],
        }]
        report_input = build_report_input(
            project_id="project_" + "0" * 32, project={}, analysis_revision=analysis
        )
        result = validate_report_output(
            _raw(
                "研究员在访谈中观察到入口容易理解。",
                stat_fact_id=None,
                evidence_roles=["observation"],
            ),
            report_input=report_input,
            report_version_id=REPORT,
        )
        claim = next(
            item for item in result["claims"]
            if item["section_key"] == "core_findings"
        )
        self.assertEqual(["observation"], claim["evidence_roles"])
        self.assertEqual(["researcher_observation"], claim["evidence_type_allowlist"])
        self.assertEqual([observation_participant], claim["participant_ids"])
        self.assertEqual([observation_evidence], claim["evidence_ids"])
        self.assertEqual("audit_passed", claim["audit_status"])

    def test_observation_role_requires_positive_non_self_report_attribution(self):
        analysis = _analysis()
        analysis["findings"][0]["observation_cases"] = [{
            "participant_id": PARTICIPANT, "evidence_ids": [EVIDENCE]
        }]
        report_input = build_report_input(
            project_id="project_" + "0" * 32, project={}, analysis_revision=analysis
        )
        for text in (
            "受访者自述入口很清楚。", "玩家坦言入口很清楚。",
            "用户回答入口很清楚。", "据玩家所说入口很清楚。",
            "研究员观察到，受访者自述入口很清楚。",
            "现场观察显示，玩家坦言入口很清楚。",
            "研究员记录到用户回答入口清晰。",
            "研究员观察到玩家觉得入口清晰。",
            "访谈员看到受访者回答入口清晰。",
            "研究员记录到据玩家所说入口清晰。",
        ):
            with self.subTest(text=text):
                result = validate_report_output(
                    _raw(
                        text,
                        stat_fact_id=None,
                        evidence_roles=["observation"],
                    ),
                    report_input=report_input,
                    report_version_id=REPORT,
                )
                self.assertIn(
                    "REPORT_OBSERVATION_MISATTRIBUTED",
                    {item["code"] for item in result["audit_issues"]},
                )

    def test_support_claim_can_describe_what_player_saw(self):
        result = validate_report_output(
            _raw("玩家看到入口后表示容易理解。", stat_fact_id=None),
            report_input=self.report_input,
            report_version_id=REPORT,
        )
        core_issues = {
            item["code"] for item in result["audit_issues"]
            if item.get("section_key") == "core_findings"
        }
        self.assertNotIn("REPORT_OBSERVATION_MISATTRIBUTED", core_issues)
        claim = next(
            item for item in result["claims"]
            if item["section_key"] == "core_findings"
        )
        self.assertEqual("audit_passed", claim["audit_status"])

    def test_observation_language_without_observation_role_is_blocking(self):
        result = validate_report_output(
            _raw("研究员在访谈中观察到入口容易理解。", stat_fact_id=None),
            report_input=self.report_input,
            report_version_id=REPORT,
        )
        self.assertIn(
            "REPORT_OBSERVATION_MISATTRIBUTED",
            {item["code"] for item in result["audit_issues"]},
        )

    def test_missing_selected_evidence_role_is_blocking(self):
        result = validate_report_output(
            _raw(
                "但也有玩家不认可入口。",
                stat_fact_id=None,
                evidence_roles=["counterexample"],
            ),
            report_input=self.report_input,
            report_version_id=REPORT,
        )
        self.assertIn(
            "REPORT_CLAIM_EVIDENCE_INVALID",
            {item["code"] for item in result["audit_issues"]},
        )

    def test_finding_cannot_use_empty_evidence_roles(self):
        with self.assertRaisesRegex(
            InterviewV2ReportValidationError, "evidence roles are invalid"
        ):
            validate_report_output(
                _raw(
                    "入口易于理解。",
                    stat_fact_id=None,
                    evidence_roles=[],
                ),
                report_input=self.report_input,
                report_version_id=REPORT,
            )

    def test_selected_role_must_derive_participant_and_evidence(self):
        analysis = _analysis()
        analysis["findings"][0]["supporting_cases"][0]["evidence_ids"] = []
        report_input = build_report_input(
            project_id="project_" + "0" * 32,
            project={},
            analysis_revision=analysis,
        )
        result = validate_report_output(
            _raw("入口易于理解。", stat_fact_id=None),
            report_input=report_input,
            report_version_id=REPORT,
        )
        self.assertIn(
            "REPORT_CLAIM_EVIDENCE_INVALID",
            {item["code"] for item in result["audit_issues"]},
        )

    def test_selected_roles_must_cover_every_referenced_finding(self):
        second_finding_id = "finding_" + "6" * 32
        analysis = _analysis()
        analysis["findings"].append({
            "finding_id": second_finding_id,
            "module_id": "module_" + "5" * 32,
            "title": "入口操作",
            "statement": "研究员观察到入口操作受阻。",
            "supporting_cases": [],
            "counterexample_cases": [],
            "observation_cases": [{
                "participant_id": "participant_" + "4" * 32,
                "evidence_ids": ["ev_" + "3" * 32],
            }],
            "stat_fact_id": None,
        })
        report_input = build_report_input(
            project_id="project_" + "0" * 32,
            project={},
            analysis_revision=analysis,
        )
        raw = _raw("入口易于理解。", stat_fact_id=None, evidence_roles=["support"])
        core_claim = next(
            section["claims"][0] for section in raw["sections"]
            if section["section_key"] == "core_findings"
        )
        core_claim["finding_ids"] = [FINDING, second_finding_id]
        validated = validate_report_output(
            raw,
            report_input=report_input,
            report_version_id=REPORT,
        )
        self.assertIn(
            "REPORT_CLAIM_EVIDENCE_INVALID",
            {item["code"] for item in validated["audit_issues"]},
        )
        validated["audit_status"] = "audited"
        validated["audit_issues"] = []
        for section in validated["sections"]:
            section["audit_status"] = "audit_passed"
        for claim in validated["claims"]:
            claim["qualification_status"] = "passed"
            claim["audit_status"] = "audit_passed"
        revision = {
            "report_version_id": REPORT,
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "status": "draft",
            "audit_status": "audited",
            "sections": validated["sections"],
            "claims": validated["claims"],
            "audit_issues": [],
            "frozen_findings": report_input["findings"],
            "frozen_stat_facts": report_input["stat_facts"],
        }
        with self.assertRaisesRegex(InterviewV2ReportValidationError, "blocked"):
            validate_report_approval(revision)

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

    def test_validates_claims_against_server_owned_edited_section(self):
        content = "1/2 的玩家能理解入口。"
        result = validate_report_section_output(
            {
                "section_key": "core_findings",
                "content": "模型不得替换这段正文。",
                "claims": [{
                    "claim_type": "finding", "text": content,
                    "start": 0, "end": len(content),
                    "finding_ids": [FINDING], "stat_fact_id": STAT,
                }],
            },
            content=content,
            report_input=self.report_input,
            report_version_id=REPORT,
            section_id="section_" + "1" * 32,
            section_key="core_findings",
            section_revision=2,
            locked=True,
        )
        self.assertEqual(content, result["section"]["content"])
        self.assertEqual(2, result["section"]["section_revision"])
        self.assertTrue(result["section"]["locked"])
        self.assertEqual("audit_passed", result["audit_status"])
        self.assertEqual(2, result["claims"][0]["section_revision"])

    def _approvable_revision(self, *, raw=None, report_input=None):
        effective_input = report_input or self.report_input
        validated = validate_report_output(
            raw or _raw(), report_input=effective_input, report_version_id=REPORT
        )
        return {
            "report_version_id": REPORT,
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "status": "draft",
            "audit_status": validated["audit_status"],
            "sections": validated["sections"],
            "claims": validated["claims"],
            "audit_issues": validated["audit_issues"],
            "frozen_findings": effective_input["findings"],
            "frozen_stat_facts": effective_input["stat_facts"],
        }

    def test_approval_rebuilds_all_sections_and_claims(self):
        result = validate_report_approval(self._approvable_revision())
        self.assertEqual("audit_passed", result["audit_status"])
        self.assertEqual(7, result["section_count"])
        self.assertEqual(7, result["claim_count"])

    def test_approval_rejects_pending_claims_with_distinct_reason(self):
        revision = self._approvable_revision()
        revision["claims"][0]["audit_status"] = "pending_reaudit"
        with self.assertRaisesRegex(InterviewV2ReportValidationError, "pending"):
            validate_report_approval(revision)

    def test_approval_does_not_trust_cached_pass_after_claim_tampering(self):
        revision = self._approvable_revision()
        claim = next(
            item for item in revision["claims"] if item["section_key"] == "core_findings"
        )
        claim["finding_ids"] = []
        claim["participant_ids"] = []
        claim["evidence_ids"] = []
        claim["stat_fact_id"] = None
        claim["qualification_status"] = "passed"
        claim["audit_status"] = "audit_passed"
        with self.assertRaisesRegex(InterviewV2ReportValidationError, "blocked"):
            validate_report_approval(revision)

    def test_approval_rejects_finding_with_empty_evidence_roles(self):
        revision = self._approvable_revision()
        claim = next(
            item for item in revision["claims"]
            if item["section_key"] == "core_findings"
        )
        claim["evidence_roles"] = []
        claim["evidence_type_allowlist"] = []
        claim["participant_ids"] = []
        claim["evidence_ids"] = []
        with self.assertRaisesRegex(InterviewV2ReportValidationError, "blocked"):
            validate_report_approval(revision)

    def test_approval_rebuild_rejects_observation_as_self_report(self):
        analysis = _analysis()
        analysis["findings"][0]["observation_cases"] = [{
            "participant_id": PARTICIPANT,
            "evidence_ids": [EVIDENCE],
        }]
        report_input = build_report_input(
            project_id="project_" + "0" * 32,
            project={},
            analysis_revision=analysis,
        )
        revision = self._approvable_revision(
            raw=_raw(
                "研究员观察到，受访者自述入口很清楚。",
                stat_fact_id=None,
                evidence_roles=["observation"],
            ),
            report_input=report_input,
        )
        revision["audit_status"] = "audited"
        revision["audit_issues"] = []
        for section in revision["sections"]:
            section["audit_status"] = "audit_passed"
        for claim in revision["claims"]:
            claim["qualification_status"] = "passed"
            claim["audit_status"] = "audit_passed"
        with self.assertRaisesRegex(InterviewV2ReportValidationError, "blocked"):
            validate_report_approval(revision)

    def test_approval_accepts_support_claim_describing_player_view(self):
        revision = self._approvable_revision(
            raw=_raw("玩家看到入口后表示容易理解。", stat_fact_id=None)
        )
        result = validate_report_approval(revision)
        self.assertEqual("audit_passed", result["audit_status"])

    def test_approval_rebuild_blocks_unbound_universal_quantity(self):
        validated = validate_report_output(
            _raw("所有玩家都能理解入口。", stat_fact_id=None),
            report_input=self.report_input,
            report_version_id=REPORT,
        )
        validated["audit_status"] = "audited"
        validated["audit_issues"] = []
        for section in validated["sections"]:
            section["audit_status"] = "audit_passed"
        for claim in validated["claims"]:
            claim["qualification_status"] = "passed"
            claim["audit_status"] = "audit_passed"
        revision = {
            "report_version_id": REPORT,
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "status": "draft",
            "audit_status": validated["audit_status"],
            "sections": validated["sections"],
            "claims": validated["claims"],
            "audit_issues": validated["audit_issues"],
            "frozen_findings": self.report_input["findings"],
            "frozen_stat_facts": self.report_input["stat_facts"],
        }
        with self.assertRaisesRegex(InterviewV2ReportValidationError, "blocked"):
            validate_report_approval(revision)

    def test_approval_rebuild_blocks_labeled_counts_and_strict_comparators(self):
        analysis = _analysis()
        analysis["stat_facts"][0].update({
            "numerator": 3, "denominator": 10, "proportion": 0.3,
        })
        report_input = build_report_input(
            project_id="project_" + "0" * 32,
            project={},
            analysis_revision=analysis,
        )
        for text in (
            "支持人数为十，样本人数为三。",
            "超过30%的玩家支持入口。",
            "不足三成玩家支持入口。",
        ):
            with self.subTest(text=text):
                revision = self._approvable_revision(
                    raw=_raw(text, stat_fact_id=STAT),
                    report_input=report_input,
                )
                revision["audit_status"] = "audited"
                revision["audit_issues"] = []
                for section in revision["sections"]:
                    section["audit_status"] = "audit_passed"
                for claim in revision["claims"]:
                    claim["qualification_status"] = "passed"
                    claim["audit_status"] = "audit_passed"
                with self.assertRaisesRegex(
                    InterviewV2ReportValidationError, "blocked"
                ):
                    validate_report_approval(revision)

    def test_approval_rejects_changed_section_body_even_when_cached_passed(self):
        revision = self._approvable_revision()
        revision["sections"][0]["content"] += "篡改"
        with self.assertRaisesRegex(InterviewV2ReportValidationError, "content hash"):
            validate_report_approval(revision)
