r"""Run a complete Insight Report v3 replay in an isolated runtime.

Automatic mode uses the existing upload, column detection, confirmation,
planning, plan confirmation and statistics services before invoking the real
report stream. Manual mode reuses a session from the same isolated DATA_DIR.
The command never targets the repository's runtime data unless the caller
explicitly opts in.

Examples:
  python scripts/replay_insight_report_v3.py INPUT.xlsx `
      --runtime-root C:\temp\insight-v3-full `
      --env-file C:\path\to\survey-web\.env --real

  python scripts/replay_insight_report_v3.py `
      --runtime-root C:\temp\insight-v3-full `
      --session-id UUID --base-version 1 --real
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path
import re
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def _load_llm_env(path: Path | None) -> None:
    if path is None:
        return
    from dotenv import dotenv_values

    for key, value in dotenv_values(path).items():
        if key.startswith("LLM_") and value is not None:
            os.environ[key] = value


def _request():
    from starlette.requests import Request

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/cli/insight-v3/full",
        "raw_path": b"/cli/insight-v3/full",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 0),
    }, receive=receive)


def _sse_payload(chunk: str) -> dict | None:
    text = str(chunk or "").strip()
    if text.startswith("data:"):
        text = text[5:].strip()
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _terminal_usage(events: list[dict]) -> dict:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    completed = [
        event for event in events
        if event.get("status") in {"completed", "failed"}
    ]
    available = False
    complete = bool(completed)
    for event in completed:
        usage = event.get("usage")
        if not isinstance(usage, dict):
            complete = False
            continue
        values = [usage.get(field) for field in totals]
        if any(type(value) is not int or value < 0 for value in values):
            complete = False
            continue
        available = True
        for field, value in zip(totals, values):
            totals[field] += value
        complete = complete and event.get("usage_complete") is True
    return {
        "available": available,
        "complete": available and complete,
        **{key: value if available else None for key, value in totals.items()},
    }


def _attempt_summary(events: list[dict]) -> dict:
    started = [event for event in events if event.get("status") == "started"]
    terminal = [
        event for event in events
        if event.get("status") in {"completed", "failed"}
    ]
    models = list(dict.fromkeys(
        str(event.get("response_model") or event.get("model") or "").strip()
        for event in terminal
        if str(event.get("response_model") or event.get("model") or "").strip()
    ))
    failures: dict[str, int] = {}
    for event in terminal:
        if event.get("status") != "failed":
            continue
        reason = str(event.get("error_category") or event.get("error_type") or "unknown")
        failures[reason] = failures.get(reason, 0) + 1
    usage = _terminal_usage(events)
    if len(started) != len(terminal):
        usage["complete"] = False
    return {
        "http_attempts": len(started),
        "completed_attempts": sum(event.get("status") == "completed" for event in terminal),
        "failed_attempts": sum(event.get("status") == "failed" for event in terminal),
        "models": models,
        "failures": failures,
        "usage": usage,
    }


def _version_state(session: dict) -> dict:
    """Record version/plan integrity without copying reports or source answers."""
    payload = {key: session.get(key) for key in (
        "report_versions", "active_report_version", "next_report_version", "plan",
    )}
    return {
        "versions": [item.get("version") for item in session.get("report_versions") or []],
        "active_report_version": session.get("active_report_version"),
        "next_report_version": session.get("next_report_version"),
        "plan_and_versions_sha256": hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str,
        ).encode("utf-8")).hexdigest(),
    }


def _diagnostic_attempt(event: dict) -> dict:
    # Do not persist request headers, credentials, HTTP bodies or player text.
    fields = (
        "call_id", "attempt", "attempt_index", "status", "model", "response_model",
        "phase", "step", "duration_seconds", "elapsed_seconds", "finish_reason",
        "error_type", "error_category", "usage", "usage_complete", "fallback",
        "attempt_number", "attempt_kind", "requested_model", "protocol",
        "started_at", "completed_at", "http_status",
        "request_id", "previous_call_id", "max_output_tokens",
    )
    return {key: deepcopy(event[key]) for key in fields if key in event}


class ReplayFailure(RuntimeError):
    def __init__(self, evidence: dict):
        super().__init__(evidence["error"])
        self.evidence = evidence


async def _run(args) -> dict:
    state = {
        "session_id": args.session_id,
        "attempt_events": [], "progress": [], "latest_usage": None,
        "regeneration_diagnostics": None, "version_before": None,
        "preparation_call_ids": set(), "preparation_http_attempts": 0,
    }
    started = time.monotonic()
    try:
        return await _run_inner(args, state)
    except Exception as exc:
        after = None
        after_error = None
        if state["session_id"]:
            try:
                from app.storage.sessions import get_session
                after = _version_state(get_session(state["session_id"]))
            except Exception as snapshot_exc:
                after_error = type(snapshot_exc).__name__
        report_attempts = [event for event in state["attempt_events"]
                           if str(event.get("call_id") or "") not in state["preparation_call_ids"]]
        raise ReplayFailure({
            "status": "failed", "error_type": type(exc).__name__, "error": str(exc)[:500],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "session_id": state["session_id"], "base_version": args.base_version,
            "regeneration_diagnostics": state["regeneration_diagnostics"],
            "progress": state["progress"],
            "version_state": {
                "before": state["version_before"], "after": after,
                "unchanged": (state["version_before"] == after
                              if state["version_before"] is not None and after is not None else None),
                "after_read_error": after_error,
            },
            "metrics": {
                "preparation_http_attempts": state["preparation_http_attempts"],
                "report_attempts": _attempt_summary(report_attempts),
                "all_attempts": _attempt_summary(state["attempt_events"]),
                "stream_report_llm_usage": state["latest_usage"],
            },
            "attempt_events": [_diagnostic_attempt(event) for event in state["attempt_events"]],
        }) from exc


def _section_titles(report_md: str) -> list[str]:
    return [
        match.group(1).strip()
        for match in re.finditer(r"(?m)^##\s+(.+?)\s*$", str(report_md or ""))
    ]


def _report_quality_metrics(report_md: str) -> dict:
    split = re.search(r"(?m)^##\s+(?:附录|完整统计附录|统计附录)", report_md)
    body = report_md[:split.start()] if split else report_md
    appendix = report_md[split.start():] if split else ""
    core = re.search(r"(?ms)^##\s+三、核心发现\s*\n(.*?)(?=^##\s|\Z)", body)
    return {
        "body_characters": len(body.rstrip()), "appendix_characters": len(appendix),
        "core_characters": len(core[1].strip()) if core else 0,
        "evidence_point_rows": len(re.findall(r"(?m)^.*\*\*证据要点：\*\*.*$", body)),
        "unexpanded_stat_tokens": len(re.findall(r"\[S:", report_md)),
        "research_gap_rows": len(re.findall(r"研究任务缺口", body)),
    }


async def _run_inner(args, state: dict) -> dict:
    from app.core.config import LLM_API_KEY
    from app.core.llm_context import bind_llm_api_key, bind_llm_attempt_observer
    from app.services import report_versions
    from app.services.insight_report_v3.contracts import artifact_without_source_text
    from app.services.report_partial_rerun import partial_rerun_capability
    from app.services.survey_service import report_stream
    from app.storage.sessions import get_session
    from scripts.replay_insight_report_v3_findings import _prepare_session

    attempt_events = state["attempt_events"]

    async def observe(event: dict):
        attempt_events.append(deepcopy(event))
        if event.get("status") in {"completed", "failed"}:
            print(json.dumps({
                "event": "model_attempt", **{key: event.get(key) for key in (
                    "status", "model", "phase", "step", "duration_seconds", "elapsed_seconds", "error_category",
                )},
            }, ensure_ascii=False), flush=True)

    key_context = bind_llm_api_key(LLM_API_KEY) if LLM_API_KEY else nullcontext()
    with key_context, bind_llm_attempt_observer(observe):
        session_id = args.session_id
        if not session_id:
            context = (
                json.loads(args.context_json.read_text(encoding="utf-8"))
                if args.context_json else {}
            )
            session_id = await _prepare_session(args.xlsx, context)
        state["session_id"] = session_id
        preparation_attempt_count = sum(
            event.get("status") == "started" for event in attempt_events
        )
        preparation_call_ids = {
            str(event.get("call_id") or "")
            for event in attempt_events
            if event.get("status") == "started"
        }
        state["preparation_call_ids"] = preparation_call_ids
        state["preparation_http_attempts"] = preparation_attempt_count

        session = get_session(session_id)
        state["version_before"] = _version_state(session)
        required = ("rows", "plan", "open_text", "stats_md", "file_sha256")
        missing = [field for field in required if not session.get(field)]
        if missing:
            raise RuntimeError(
                f"session 尚未完成前四步，缺少：{', '.join(missing)}"
            )

        generation_kind = "regenerate" if args.base_version is not None else "initial"
        done = None
        progress = state["progress"]
        latest_usage = None
        async for chunk in report_stream(
            session_id,
            _request(),
            instruction=args.instruction,
            base_version=args.base_version,
            generation_kind=generation_kind,
        ):
            payload = _sse_payload(chunk)
            if not payload:
                continue
            event_type = payload.get("type")
            if event_type == "error":
                raise RuntimeError(payload.get("message") or "完整报告回放失败")
            if event_type == "analysis_progress":
                progress.append(deepcopy(payload))
                print(json.dumps({
                    "event": "analysis_progress",
                    "phase": payload.get("phase"),
                    "status": payload.get("status"),
                    "item_index": payload.get("item_index"),
                    "item_total": payload.get("item_total"),
                    "message": payload.get("message"),
                    "chapter_id": payload.get("chapter_id"),
                    "chapter_status": payload.get("chapter_status"),
                }, ensure_ascii=False), flush=True)
            elif event_type == "report_llm_status":
                latest_usage = deepcopy(payload.get("report_llm_usage"))
                state["latest_usage"] = latest_usage
            elif event_type == "report_regeneration_diagnostics":
                state["regeneration_diagnostics"] = deepcopy(payload.get("diagnostics"))
            elif event_type == "report_done":
                done = payload

        if done is None:
            raise RuntimeError("完整报告流没有产生 report_done")

    session = get_session(session_id)
    version = report_versions.resolve_report_version(session, done["version"])
    findings = session.get("insight_v3_findings")
    public_findings = (
        artifact_without_source_text(findings)
        if isinstance(findings, dict) else None
    )
    report_md = str(version.get("report_md") or done.get("report_md") or "")
    report_attempts = [
        event for event in attempt_events
        if str(event.get("call_id") or "") not in preparation_call_ids
    ]

    return {
        "status": "complete",
        "session_id": session_id,
        "source_filename": str(session.get("filename") or ""),
        "preparation": "manual_session" if args.session_id else "automatic_existing_services",
        "version": done["version"],
        "regeneration_diagnostics": state["regeneration_diagnostics"],
        "version_state": {"before": state["version_before"], "after": _version_state(session)},
        "attempt_events": [_diagnostic_attempt(event) for event in attempt_events],
        "report_engine": version.get("report_engine"),
        "analyst_app": version.get("analyst_app"),
        "report_status": version.get("report_status"),
        "has_analysis_artifacts": "analysis_artifacts" in version,
        "partial_rerun": partial_rerun_capability(session, version),
        "comparison_validation": deepcopy(version.get("comparison_validation") or {}),
        "viewpoint_diagnostics": deepcopy(version.get("viewpoint_diagnostics") or {}),
        "findings": public_findings,
        "progress": progress,
        "report": {
            "character_count": len(report_md),
            **_report_quality_metrics(report_md),
            "section_titles": _section_titles(report_md),
            "markdown": report_md,
        },
        "metrics": {
            "preparation_http_attempts": preparation_attempt_count,
            "report_attempts": _attempt_summary(report_attempts),
            "all_attempts": _attempt_summary(attempt_events),
            "snapshot_report_llm_usage": deepcopy(
                version.get("report_llm_usage") or latest_usage or {}
            ),
        },
    }


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx", nargs="?", type=Path)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--base-version", type=int)
    parser.add_argument("--instruction", default="")
    parser.add_argument("--context-json", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--question-concurrency", type=int, default=2)
    parser.add_argument("--chapter-concurrency", type=int, default=2)
    parser.add_argument("--run-label", default="")
    parser.add_argument("--allow-worktree-runtime", action="store_true")
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    if not args.real:
        parser.error("该入口会调用真实模型；必须显式传入 --real")
    if not args.session_id and not args.xlsx:
        parser.error("自动准备需要 XLSX；复用人工流程时传 --session-id")
    if args.session_id and args.xlsx:
        parser.error("XLSX 与 --session-id 只能选择一种输入")
    if args.base_version is not None and not args.session_id:
        parser.error("--base-version 仅能与 --session-id 一起使用")
    if args.xlsx:
        args.xlsx = args.xlsx.resolve()
        if not args.xlsx.is_file():
            parser.error("XLSX 不存在")
    if args.context_json:
        args.context_json = args.context_json.resolve()
        if not args.context_json.is_file():
            parser.error("context-json 不存在")
    if args.env_file:
        args.env_file = args.env_file.resolve()
        if not args.env_file.is_file():
            parser.error("env-file 不存在")
    args.runtime_root = args.runtime_root.resolve()
    if args.runtime_root == ROOT and not args.allow_worktree_runtime:
        parser.error("工作树运行目录必须显式传 --allow-worktree-runtime")
    if args.runtime_root != ROOT and args.runtime_root.is_relative_to(ROOT):
        parser.error("隔离 runtime 不得放在仓库内部")
    if args.question_concurrency < 1 or args.chapter_concurrency < 1:
        parser.error("并发数必须大于 0")
    args.run_label = re.sub(
        r"[^a-zA-Z0-9_-]+", "-", args.run_label
    ).strip("-")[:60]
    return args


def main() -> int:
    args = _parse_args()
    args.runtime_root.mkdir(parents=True, exist_ok=True)
    evidence_dir = args.runtime_root / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(args.runtime_root)
    os.environ["DATA_DIR"] = str(args.runtime_root / "data")
    os.environ["FEISHU_LOGIN_REQUIRED"] = "false"
    os.environ["INSIGHT_REPORT_V3_ENABLED"] = "true"
    os.environ["INSIGHT_V3_QUESTION_CONCURRENCY"] = str(args.question_concurrency)
    os.environ["INSIGHT_V3_CHAPTER_CONCURRENCY"] = str(args.chapter_concurrency)
    _load_llm_env(args.env_file)
    sys.path.insert(0, str(ROOT))

    started = time.monotonic()
    suffix = f"-{args.run_label}" if args.run_label else ""
    try:
        evidence = asyncio.run(_run(args))
    except Exception as exc:
        failure = exc.evidence if isinstance(exc, ReplayFailure) else {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        failure["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (evidence_dir / f"full-report-failure{suffix}.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(failure, ensure_ascii=False), flush=True)
        return 1

    evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report_md = evidence["report"].pop("markdown")
    report_path = evidence_dir / f"full-report{suffix}.md"
    report_path.write_text(report_md, encoding="utf-8")
    evidence["report"]["path"] = str(report_path)
    evidence_path = evidence_dir / f"full-report{suffix}.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "status": evidence["status"],
        "session_id": evidence["session_id"],
        "version": evidence["version"],
        "report_engine": evidence["report_engine"],
        "elapsed_seconds": evidence["elapsed_seconds"],
        "http_attempts": evidence["metrics"]["all_attempts"]["http_attempts"],
        "total_tokens": evidence["metrics"]["all_attempts"]["usage"]["total_tokens"],
        "report": str(report_path),
        "evidence": str(evidence_path),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
