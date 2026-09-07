"""用固定 173 候选规模和真实模型回放跨题观点阶段。

本脚本不导入 app.main、不读取或写入 data/。默认把 JSON 结果输出到 stdout；
只有显式传入 --output 时才写入指定路径。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import (
    LLM_CROSS_QUESTION_MAX_VIEWPOINTS,
    LLM_CROSS_QUESTION_STAGE_TIMEOUT_SECONDS,
)
from app.services.qualitative_viewpoints import build_report_viewpoint_stats


DEFAULT_FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "report_pipeline"
    / "cross_question_173_sanitized.json"
)


def _build_inputs(spec: dict) -> tuple[dict, dict, dict, list[str]]:
    clustered = {}
    open_text = {}
    columns = []
    headers = ["respondent_id"]
    common = [str(item) for item in spec["common_viewpoints"]]
    respondent_count = int(spec["respondents_per_question"])
    for question_index, theme_count in enumerate(spec["question_theme_counts"], 1):
        question = f"脱敏问题{question_index}"
        headers.append(question)
        columns.append({"index": question_index, "name": question, "role": "open_text"})
        themes = []
        for theme_index in range(int(theme_count)):
            if theme_index < len(common):
                name = common[theme_index]
            else:
                name = f"{spec['unique_theme_prefix']}-{question_index}-{theme_index + 1}"
            themes.append({
                "id": f"t{theme_index + 1:02d}",
                "name": name,
                "description": f"{name}的脱敏语义说明",
                "count": 1,
                "source_quotes": [f"{question}：{name}"],
            })
        clustered[question_index] = {
            "col_name": question,
            "part_index": 1,
            "total": respondent_count,
            "all_themes": themes,
        }
        open_text[question_index] = [
            {
                "respondent_key": f"p{respondent_index:04d}",
                "text": (
                    common[respondent_index % len(common)]
                    if respondent_index < respondent_count // 2
                    else f"{spec['unique_theme_prefix']}-{question_index}-{respondent_index}"
                ),
            }
            for respondent_index in range(respondent_count)
        ]
    plan = {
        "columns": columns,
        "parts": [{
            "name": "脱敏综合体验",
            "column_indexes": [column["index"] for column in columns],
        }],
    }
    return clustered, open_text, plan, headers


async def _run(spec: dict) -> dict:
    clustered, open_text, plan, headers = _build_inputs(spec)
    attempts = []
    diagnostics = {}
    result = []

    def record_attempt(event: dict) -> None:
        attempts.append(dict(event))

    async for kind, payload in build_report_viewpoint_stats(
        clustered,
        open_text,
        plan,
        headers,
        on_attempt_event=record_attempt,
    ):
        if kind == "diagnostics":
            diagnostics = payload
        elif kind == "result":
            result = payload

    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for event in attempts:
        if event.get("status") not in {"completed", "failed"}:
            continue
        event_usage = event.get("usage")
        if not isinstance(event_usage, dict):
            continue
        for key in usage:
            value = event_usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                usage[key] += value

    summary = {
        "fixture_source": spec.get("source"),
        "expected_candidate_count": int(spec["expected_candidate_count"]),
        "stage_budget_seconds": LLM_CROSS_QUESTION_STAGE_TIMEOUT_SECONDS,
        "max_viewpoints": LLM_CROSS_QUESTION_MAX_VIEWPOINTS,
        "upstream_attempt_count": sum(
            event.get("status") == "started" for event in attempts
        ),
        "fallback_attempt_count": sum(
            event.get("status") == "started" and bool(event.get("fallback"))
            for event in attempts
        ),
        "usage": usage,
        "diagnostics": diagnostics,
        "final_viewpoints": result,
    }
    failures = []
    if diagnostics.get("input_candidate_count") != int(spec["expected_candidate_count"]):
        failures.append("candidate_count_mismatch")
    if diagnostics.get("reduction_levels") != 0:
        failures.append("hierarchical_reduction_reappeared")
    if float(diagnostics.get("elapsed_seconds") or 0) > LLM_CROSS_QUESTION_STAGE_TIMEOUT_SECONDS:
        failures.append("stage_budget_exceeded")
    if diagnostics.get("status") not in {"completed", "recovered"}:
        failures.append(f"synthesis_status={diagnostics.get('status')}")
    if not 1 <= len(result) <= LLM_CROSS_QUESTION_MAX_VIEWPOINTS:
        failures.append("invalid_final_viewpoint_count")
    if any(len(item.get("source_scope_keys") or []) < 2 for item in result):
        failures.append("non_cross_question_viewpoint")
    if any(
        call.get("finish_reason") == "length" or call.get("error_type") == "timeout"
        for call in diagnostics.get("calls") or []
    ):
        failures.append("timeout_or_truncation")
    summary["gate"] = "GO" if not failures else "NO-GO"
    summary["failures"] = failures
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    spec = json.loads(args.fixture.read_text(encoding="utf-8"))
    summary = asyncio.run(_run(spec))
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if summary["gate"] == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
