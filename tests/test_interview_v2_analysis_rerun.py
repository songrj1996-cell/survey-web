from copy import deepcopy
import unittest

from app.core import interview_v2_analysis as core
from tests import test_interview_v2_analysis_service as fixtures


PROJECT = fixtures.PROJECT
MODULE_A = fixtures.MODULE
MODULE_B = "module_" + "a" * 32
BASE = "analysis_" + "b" * 32
NEXT = "analysis_" + "c" * 32
LOGIN = {"email": "owner@example.com"}
PROMPTS = {"interview_v2_analysis_system": {"version": 1, "sha256": "d" * 64}}
MODELS = {"models": ["mock-model"], "max_tokens": 1000, "reasoning_effort": "low"}


def ready_fixture():
    ready = deepcopy(fixtures.ready_payload())
    _, evidence, boundary, coverage, _ = ready
    other_object, other_question = "evaluation_" + "a" * 32, "question_" + "a" * 32
    boundary["evaluation_objects"].append({
        "evaluation_object_id": other_object, "module_id": MODULE_B,
        "main_question_ids": [other_question], "decision_status": "confirmed",
    })
    for index, entry in enumerate(list(evidence["entries"])):
        evidence["entries"].append({
            **entry, "evidence_id": "ev_" + ("c" if index == 0 else "d") * 32,
            "module_id": MODULE_B, "main_question_id": other_question,
        })
    preview = coverage["coverage_preview"]
    preview["rows"].extend([
        {**row, "module_id": MODULE_B, "evaluation_object_id": other_object, "main_question_id": other_question}
        for row in list(preview["rows"])
    ])
    preview["summaries"].append({
        "module_id": MODULE_B, "evaluation_object_id": other_object,
        "main_question_id": other_question, "denominator_reliable": True,
    })
    return ready


def raw_output(module_input, title="原发现"):
    obj = module_input["evaluation_objects"][0]
    evidence = module_input["evidence_allowlist"]
    return {"module_id": module_input["module_id"], "findings": [{
        "title": title, "statement": title + "：支持与反例并存。",
        "evaluation_object_id": obj["evaluation_object_id"],
        "main_question_id": obj["main_question_ids"][0],
        "supporting_cases": [{"participant_id": fixtures.P1, "evidence_ids": [next(e["evidence_id"] for e in evidence if e["participant_id"] == fixtures.P1)]}],
        "counterexample_cases": [{"participant_id": fixtures.P2, "evidence_ids": [next(e["evidence_id"] for e in evidence if e["participant_id"] == fixtures.P2)]}],
        "observation_cases": [], "limitations": ["样本有限"], "confidence": 0.8,
    }]}


def analysis_fixture(ready=None, dossiers=None):
    ready = ready or ready_fixture()
    if dossiers is None:
        dossiers = [fixtures.current_dossier(pid, "approved")["revision"] for pid in (fixtures.P1, fixtures.P2)]
    versions = sorted([{
        "participant_id": item["participant_id"], "dossier_version_id": item["dossier_version_id"],
        "revision_payload_sha256": item["revision_payload_sha256"],
    } for item in dossiers], key=lambda item: item["participant_id"])
    analysis_input = core.build_analysis_input(
        project_id=PROJECT, source={**ready[4], "dossier_versions": versions},
        evidence_revision=ready[1], analysis_boundary=ready[2],
        coverage_revision=ready[3]["coverage_preview"], dossier_revisions=dossiers,
        unreviewed_participant_ids=[],
    )
    results = [core.validate_module_findings(raw_output(module), module_input=module, analysis_run_id=BASE) for module in analysis_input["modules"]]
    base = {
        "project_id": PROJECT, "import_id": fixtures.IMPORT, "analysis_run_id": BASE,
        "analysis_schema_version": core.ANALYSIS_SCHEMA_VERSION,
        "source": deepcopy(analysis_input["source"]), "input_fingerprint": analysis_input["input_fingerprint"],
        "status": "completed", "findings": [f for result in results for f in result["findings"]],
        "stat_facts": [s for result in results for s in result["stat_facts"]],
        "limitations": ["全局限制"], "model_usage": {"modules": [{"module_id": m["module_id"], "model": "base-model"} for m in analysis_input["modules"]]},
        "version_number": 1, "created_at": "2026-09-06T00:00:00Z",
    }
    base["revision_payload_sha256"] = core.payload_sha256(base)
    return ready, analysis_input, base


class AnalysisModuleRerunCoreTests(unittest.TestCase):
    def setUp(self):
        self.ready, self.inputs, self.base = analysis_fixture()

    def input(self, **overrides):
        return core.build_analysis_module_rerun_input(**{
            "base_revision": self.base, "analysis_input": self.inputs, "module_id": MODULE_A,
            "prompt_snapshot": PROMPTS, "model_configuration": MODELS, **overrides,
        })

    def replacement(self, module_id=MODULE_A):
        module = next(m for m in self.inputs["modules"] if m["module_id"] == module_id)
        result = core.validate_module_findings(raw_output(module, "新发现"), module_input=module, analysis_run_id=NEXT)
        merged = core.merge_analysis_module_result(base_revision=self.base, module_id=module_id, result=result, analysis_run_id=NEXT)
        return {**deepcopy(self.base), **merged, "analysis_run_id": NEXT}

    def test_frozen_scope_and_no_alias_to_inputs(self):
        frozen = self.input()
        self.assertEqual(MODULE_A, frozen["module_input"]["module_id"])
        self.assertNotIn("modules", frozen)
        frozen["module_input"]["evidence_allowlist"].clear()
        self.assertTrue(self.inputs["modules"][0]["evidence_allowlist"])

    def test_fingerprint_covers_scope_prompt_model_and_base(self):
        first = self.input()["input_fingerprint"]
        for changes in (
            {"module_id": MODULE_B}, {"prompt_snapshot": {"changed": "prompt"}},
            {"model_configuration": {**MODELS, "max_tokens": 2000}},
        ):
            self.assertNotEqual(first, self.input(**changes)["input_fingerprint"])

    def test_changed_or_tampered_upstream_is_rejected(self):
        for changes in ("source", "input_fingerprint"):
            invalid = deepcopy(self.inputs)
            invalid[changes] = {} if changes == "source" else "0" * 64
            with self.assertRaises(core.InterviewV2AnalysisValidationError):
                self.input(analysis_input=invalid)
        invalid = deepcopy(self.base)
        invalid["findings"][0]["statement"] = "tampered"
        with self.assertRaises(core.InterviewV2AnalysisValidationError):
            self.input(base_revision=invalid)

    def test_other_module_semantics_and_numbers_are_preserved(self):
        before = deepcopy(self.base)
        revision = self.replacement()
        core.validate_analysis_module_replacement(self.base, revision, MODULE_A)
        old = next(f for f in self.base["findings"] if f["module_id"] == MODULE_B)
        new = next(f for f in revision["findings"] if f["module_id"] == MODULE_B)
        self.assertEqual({k: v for k, v in old.items() if k != "stat_fact_id"}, {k: v for k, v in new.items() if k != "stat_fact_id"})
        self.assertNotEqual(old["stat_fact_id"], new["stat_fact_id"])
        self.assertEqual([1, 1], [s["numerator"] for s in revision["stat_facts"]])
        self.assertEqual([2, 2], [s["denominator"] for s in revision["stat_facts"]])
        self.assertEqual(before, self.base)

    def test_storage_guard_rejects_changes_to_reused_work(self):
        for field in ("statement", "supporting_cases"):
            revision = self.replacement()
            other = next(f for f in revision["findings"] if f["module_id"] == MODULE_B)
            other[field] = "rewritten" if field == "statement" else []
            with self.assertRaises(core.InterviewV2AnalysisValidationError):
                core.validate_analysis_module_replacement(self.base, revision, MODULE_A)
        revision = self.replacement()
        revision["limitations"] = []
        with self.assertRaises(core.InterviewV2AnalysisValidationError):
            core.validate_analysis_module_replacement(self.base, revision, MODULE_A)

    def test_empty_target_and_module_without_findings_remain_supported(self):
        empty = {"module_id": MODULE_A, "findings": [], "stat_facts": []}
        merged = core.merge_analysis_module_result(base_revision=self.base, module_id=MODULE_A, result=empty, analysis_run_id=NEXT)
        self.assertEqual([MODULE_B], [f["module_id"] for f in merged["findings"]])
        self.base.update(merged)
        self.base["analysis_run_id"] = NEXT
        self.base.pop("revision_payload_sha256")
        self.base["revision_payload_sha256"] = core.payload_sha256(self.base)
        self.assertEqual(MODULE_A, self.input()["module_input"]["module_id"])

    def test_duplicate_and_wrong_module_results_are_rejected(self):
        module = self.inputs["modules"][0]
        result = core.validate_module_findings(raw_output(module), module_input=module, analysis_run_id=NEXT)
        result["findings"] *= 2
        with self.assertRaises(core.InterviewV2AnalysisValidationError):
            core.merge_analysis_module_result(base_revision=self.base, module_id=MODULE_A, result=result, analysis_run_id=NEXT)
        with self.assertRaises(core.InterviewV2AnalysisValidationError):
            self.input(module_id="module_" + "0" * 32)
