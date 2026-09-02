import json
import unittest
from unittest.mock import AsyncMock, patch

from app.core.interview_v2_report import REPORT_SECTION_SPECS
from app.services import interview_v2_report_service as service


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


class InterviewV2ReportServiceTests(unittest.IsolatedAsyncioTestCase):
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
