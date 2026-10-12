"""One isolated Q7 experiment, optionally auditing frozen text only; never import app.main.

Explicit experiment caps include translation calls. Production budgets and models
are not changed. Inputs are immutable saved artifacts, not live sessions.
"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ExperimentBudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, logical_limit=18, http_limit=72):
        self.logical_limit = min(18, logical_limit)
        self.http_limit = min(72, http_limit)
        self.logical = 0
        self.http_reserved = 0
        self.http_actual = 0

    def start_logical(self):
        if self.logical >= self.logical_limit:
            raise ExperimentBudgetExceeded("experiment_logical_budget_exhausted")
        self.logical += 1
        return self.logical

    def reserve_http(self):
        amount = min(2, self.http_limit - self.http_reserved)
        if amount < 1:
            raise ExperimentBudgetExceeded("experiment_http_budget_exhausted")
        self.http_reserved += amount
        return amount


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_paths(args):
    runtime = Path(args.runtime_root).resolve()
    output = Path(args.output).resolve()
    inputs = [Path(args.findings).resolve(), Path(args.session).resolve()]
    if getattr(args, "audit_input", None):
        inputs.append(Path(args.audit_input).resolve())
    if getattr(args, "env_file", None) and not Path(args.env_file).is_file():
        raise ValueError("env-file is missing; no model calls were started")
    # No repository runtime, no overlap with frozen inputs, no overwrite.
    if runtime == ROOT or ROOT in runtime.parents or runtime == ROOT.parent:
        raise ValueError("runtime must be outside repository")
    for path in inputs:
        if runtime == path or runtime in path.parents or path.is_relative_to(ROOT):
            raise ValueError("runtime must not contain frozen inputs or use repository data")
        if not path.is_file():
            raise ValueError("frozen input is missing")
    if output.exists() or output.with_suffix(".md").exists():
        raise ValueError("experiment output already exists; do not replay automatically")
    if output == ROOT or ROOT in output.parents or any(output == path for path in inputs):
        raise ValueError("output must be an independent isolated artifact")
    return runtime, output, inputs


def load_frozen_audit(path, *, inputs, source):
    """Reject mismatched or unfinished snapshots before making any model call."""
    frozen = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(frozen, dict) or frozen.get("state") != "finished":
        raise ValueError("audit-input must be a finished saved experiment")
    recorded = {Path(p).resolve(): value for p, value in (frozen.get("input_sha256") or {}).items()}
    if any(recorded.get(p) != sha(p) for p in inputs[:2]):
        raise ValueError("audit-input findings/session hashes do not match")
    if frozen.get("findings") != source.get("findings"):
        raise ValueError("audit-input findings differ from the frozen extraction")
    written, core, actions = (frozen.get(key) for key in
                              ("chapters_before_audit", "core_before_audit", "actions_before_audit"))
    if (not isinstance(written, list) or not written
            or any(not isinstance(c, dict) or not isinstance(c.get("blocks"), list) for c in written)
            or not isinstance(core, dict) or not isinstance(core.get("blocks"), list)
            or not isinstance(actions, dict) or not isinstance(actions.get("actions"), list)):
        raise ValueError("audit-input is missing pre-audit chapter/core/action results")
    return frozen


async def run(args):
    runtime, output, inputs = validate_paths(args)
    os.environ["DATA_DIR"] = str(runtime)
    os.environ["FEISHU_LOGIN_REQUIRED"] = "false"
    if args.env_file:
        from dotenv import dotenv_values
        for key, value in dotenv_values(args.env_file).items():
            if key.startswith("LLM_") and value is not None:
                os.environ[key] = value
    sys.path.insert(0, str(ROOT))
    from app.services import report_engine, report_source_translation
    from app.services.insight_report_v3 import audit, chapters
    from app.services.insight_report_v3.research_context import linked_source_context, complete_stats_tables
    from app.services.insight_report_v3.assemble import render_written_section, render_actions_section, render_unresolved_audit_notice
    from scripts.replay_insight_report_v3 import _attempt_summary
    from app.core import config

    if Path(config.DATA_DIR).resolve() != runtime:
        raise ValueError("config imported before isolation was configured")
    if not config.LLM_API_KEY:
        raise ValueError("LLM_API_KEY is unavailable; no model calls were started")
    source = json.loads(inputs[0].read_text(encoding="utf-8"))
    session = json.loads(inputs[1].read_text(encoding="utf-8"))
    frozen = load_frozen_audit(inputs[2], inputs=inputs, source=source) if len(inputs) == 3 else None
    findings = {f["id"]: deepcopy(f) for f in source["findings"]}
    registry, context_diag = linked_source_context(source["source_registry"], rows=session["rows"], plan=session["plan"])
    catalog = [] if frozen else chapters.build_stats_catalog(complete_stats_tables(session["stats_md"], session.get("stats_blocks") or []))
    budget = Budget()
    active_call = ContextVar("source_audit_call", default=None)
    stage = "chapter"
    evidence = {"state": "started", "semantic_acceptance": "not_reviewed", "scope": "one-Q7-closed-loop-not-full-report", "input_sha256": {str(p): sha(p) for p in inputs},
                "versions": {"audit": config.INSIGHT_V3_AUDIT_PROMPT_VERSION, "repair": config.INSIGHT_V3_AUDIT_REPAIR_PROMPT_VERSION,
                             "findings": source["prompt_version"], "writer": config.INSIGHT_V3_CHAPTER_PROMPT_VERSION},
                "findings": deepcopy(source["findings"]), "source_context": context_diag,
                "limits": {"logical": 18, "http": 72}, "calls": [], "attempt_events": [], "completions": []}
    if frozen:
        evidence.update(scope="one-Q7-frozen-audit-only-not-full-report", generation_replayed=False,
                        parent_versions=deepcopy(frozen.get("versions") or {}))
        evidence["versions"]["writer"] = (frozen.get("versions") or {}).get("writer")
        evidence["models"] = {"audit": config.LLM_INSIGHT_V3_AUDIT_MODEL,
                              "audit_fallbacks": list(config.LLM_INSIGHT_V3_AUDIT_FALLBACK_MODELS),
                              "audit_reasoning": config.LLM_INSIGHT_V3_AUDIT_REASONING,
                              "repair": config.LLM_REPORT_MODEL,
                              "repair_fallbacks": list(config.LLM_REPORT_FALLBACK_MODELS)}
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def save():
        evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
        evidence["logical_calls"] = budget.logical
        evidence["http_attempts"] = budget.http_actual
        evidence["metrics"] = _attempt_summary(evidence["attempt_events"])
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    original_direct = report_engine._direct_json_call
    original_collect = report_engine.collect_chat_completion

    async def direct(system, query, **kwargs):
        number = budget.start_logical()
        token = active_call.set(number)
        call = {"number": number, "stage": stage, "system": system, "query": query, "state": "started"}
        evidence["calls"].append(call)
        save()
        try:
            result = await original_direct(system, query, **kwargs)
            call.update(state="finished", result=deepcopy(result))
            return result
        finally:
            active_call.reset(token)
            save()
            print(json.dumps({"logical": number, "stage": stage, "state": call["state"], "elapsed": evidence["elapsed_seconds"]}), flush=True)

    async def collect(messages, **kwargs):
        number = active_call.get()
        if number is None:
            number = budget.start_logical()  # translation call outside direct JSON helper
            evidence["calls"].append({"number": number, "stage": "translation", "messages": deepcopy(messages)})
        reserved = budget.reserve_http()
        observed = 0
        callback = kwargs.get("on_attempt_event")

        async def observe(event):
            nonlocal observed
            if event.get("status") == "started":
                observed += 1
                budget.http_actual += 1
            evidence["attempt_events"].append({**deepcopy(event), "experiment_call": number, "experiment_stage": stage})
            save()
            if callback:
                value = callback(event)
                if inspect.isawaitable(value):
                    await value
        try:
            answer, model = await original_collect(messages, **{**kwargs, "max_http_attempts": reserved, "on_attempt_event": observe})
            evidence["completions"].append({"number": number, "model": model, "answer": answer})
            return answer, model
        finally:
            budget.http_reserved -= reserved - observed
            save()

    save()
    spec = {"id": "q7", "title": "卡牌乱斗的吸引力、体验阻力与改进方向", "purpose": "解释玩家评价的具体依据，保留正反意见与关键条件，不将相似话题合并为相同态度。",
            "finding_ids": list(findings), "stat_ids": [], "group_focus": None, "analysis_focus": {}}
    try:
        with patch.object(chapters, "_direct_json_call", new=direct), patch.object(audit, "_direct_json_call", new=direct), patch.object(report_engine, "collect_chat_completion", new=collect), patch.object(report_source_translation, "collect_chat_completion", new=collect):
            if frozen:
                written = deepcopy(frozen["chapters_before_audit"])
                core = deepcopy(frozen["core_before_audit"])
                actions = deepcopy(frozen["actions_before_audit"])
                evidence["inherited_generation_diagnostics"] = {
                    "chapters": [deepcopy(c.get("diagnostics") or {}) for c in written],
                    "core": deepcopy(core.get("diagnostics") or {}),
                    "actions": deepcopy(actions.get("diagnostics") or {}),
                }
            else:
                written = []
                async for kind, value in chapters.write_chapters(chapters=[spec], findings_by_id=findings, source_registry=registry,
                        stats_catalog=[], profiles=[], qualitative_context=session.get("qualitative_context"), translation_namespace="source-audit-v2-q7", concurrency=1):
                    if kind == "result":
                        written = value
                evidence["chapters_before_audit"] = deepcopy(written)
                save()
                stage = "core"
                core = await chapters.write_core_findings(chapters=written, findings_by_id=findings, source_registry=registry, profiles=[],
                        qualitative_context=session.get("qualitative_context"), translation_namespace="source-audit-v2-q7", stats_catalog=[], sample_stats=catalog[:3])
                evidence["core_before_audit"] = deepcopy(core)
                save()
                stage = "actions"
                actions = await chapters.write_actions(chapters=written, core_blocks=core["blocks"], findings_by_id=findings, profiles=[], qualitative_context=session.get("qualitative_context"))
            evidence["chapters_before_audit"] = deepcopy(written)
            evidence["core_before_audit"] = deepcopy(core)
            evidence["actions_before_audit"] = deepcopy(actions)
            stage = "audit"
            preflight_chapters = deepcopy(written)
            if frozen and core.get("sample_overview"):
                preflight_chapters.append({"id": "sample", "title": "样本概况", "blocks": [{
                    **deepcopy(core["sample_overview"]), "id": "sample-overview", "finding_ids": [], "evidence": [],
                }]})
            units = audit.build_audit_units(core_blocks=core["blocks"], chapters=preflight_chapters, actions=actions["actions"], findings_by_id=findings, source_registry=registry)
            batches, oversize = audit._pack_units(units, source_registry=registry, query_builder=audit._audit_query)
            evidence["audit_preflight"] = {"units": len(units), "source_checks": sum(u["kind"] == "finding_binding" for u in units),
                                          "initial_batches": len(batches), "input_chars": [len(audit._audit_query(b, registry)) for b in batches], "oversize": len(oversize)}
            save()
            result = await audit.audit_report_sections(core_blocks=core["blocks"], chapters=written, actions=actions["actions"],
                    findings_by_id=findings, source_registry=registry, profiles=[], sample_overview=core.get("sample_overview"))
            evidence["audit"] = result
            evidence["execution_status"] = "complete" if (
                all(c["diagnostics"].get("status") == "complete" for c in written)
                and core["diagnostics"].get("status") == "complete"
                and actions["diagnostics"].get("status") == "complete"
                and result["diagnostics"].get("status") in {"passed", "repaired"}
            ) else "degraded_or_needs_review"
            if frozen:
                evidence["execution_status"] = "complete" if result["diagnostics"].get("status") in {"passed", "repaired"} else "degraded_or_needs_review"
                evidence["frozen_text_unchanged_before_audit"] = all(
                    evidence[key] == frozen[key] for key in ("chapters_before_audit", "core_before_audit", "actions_before_audit")
                )
            report = ["# 卡牌乱斗 Q7 冻结文本审校回放（非完整报告）" if frozen else "# 卡牌乱斗 Q7 来源核对闭环试验", render_unresolved_audit_notice(result["unresolved"])]
            section_offset = 1 if frozen and result.get("sample_overview") else 0
            if frozen and result.get("sample_overview"):
                report.append(render_written_section(section_title="样本概况", blocks=[result["sample_overview"]], findings_by_id=findings, section_index=1, evidence_ids={}))
            report.append(render_written_section(section_title="核心发现", blocks=result["core_blocks"], findings_by_id=findings, section_index=1 + section_offset, evidence_ids={}))
            for index, chapter in enumerate(result["chapters"], 2 + section_offset):
                report.append(render_written_section(section_title=chapter["title"], blocks=chapter["blocks"], findings_by_id=findings, section_index=index, evidence_ids={}))
            report.append(render_actions_section(actions=result["actions"], chapter_note=actions.get("chapter_note", ""), section_index=3 + section_offset))
            markdown = "\n\n".join(report)
            output.with_suffix(".md").write_text(markdown, encoding="utf-8")
            evidence.update(state="finished", body_characters=len(markdown), findings_unchanged=list(findings.values()) == source["findings"])
    except Exception as exc:
        evidence.update(state="failed", error_type=type(exc).__name__, error=str(exc)[:500])
    finally:
        evidence["inputs_unchanged"] = all(sha(p) == evidence["input_sha256"][str(p)] for p in inputs)
        save()
    print(json.dumps({"state": evidence["state"], "logical": budget.logical, "http": budget.http_actual,
                      "elapsed": evidence["elapsed_seconds"], "output": str(output)}), flush=True)
    return 0 if evidence["state"] == "finished" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("findings", "session", "runtime-root", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--env-file")
    parser.add_argument("--audit-input", help="Finished saved experiment: reuse pre-audit text; do not run extraction or writing")
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    if not args.real:
        parser.error("explicit --real required")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
