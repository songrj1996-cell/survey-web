"""Run Insight Report v3 stage one in an isolated runtime.

Automatic preparation calls the existing upload, column detection, column
confirmation, planning, plan confirmation and statistics service functions.
It does not implement a second preparation path. A manually prepared session
can be reused with --session-id.

Examples:
  python scripts/replay_insight_report_v3_findings.py INPUT.xlsx \
      --runtime-root C:\\temp\\insight-v3-jungle --real

  python scripts/replay_insight_report_v3_findings.py \
      --runtime-root C:\\temp\\insight-v3-jungle --session-id UUID \
      --concurrency 4 --question-limit 6 --real
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
from copy import deepcopy
import json
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

    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/cli/insight-v3",
        "raw_path": b"/cli/insight-v3",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 0),
    })


def _sse_payload(chunk: str) -> dict | None:
    text = str(chunk or "").strip()
    if text.startswith("data:"):
        text = text[5:].strip()
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


async def _consume_service_stream(stream, label: str) -> None:
    async for chunk in stream:
        payload = _sse_payload(chunk)
        if payload and payload.get("type") == "error":
            raise RuntimeError(f"{label}失败：{payload.get('message') or '未知错误'}")


async def _prepare_session(xlsx_path: Path, context: dict) -> str:
    from app.schemas.requests import QualitativeContextRequest
    from app.services.survey_service import (
        columns_stream,
        compute_survey_stats,
        confirm_survey_plan,
        handle_survey_upload,
        plan_stream,
        save_qualitative_context,
        set_survey_analysis_settings,
        set_survey_columns,
    )
    from app.storage.sessions import get_session

    request = _request()
    uploaded = await handle_survey_upload(
        xlsx_path.name,
        xlsx_path.read_bytes(),
        None,
        source_type="google",
    )
    session_id = uploaded["session_id"]
    set_survey_analysis_settings(session_id, "insight", report_mode="insight")
    await _consume_service_stream(columns_stream(session_id, request), "题型识别")
    session = get_session(session_id)
    columns = session.get("columns_detected")
    if not isinstance(columns, list) or not columns:
        raise RuntimeError("题型识别没有产生可确认列")
    set_survey_columns(session_id, columns)
    if context:
        save_qualitative_context(
            session_id,
            QualitativeContextRequest(**context),
            None,
        )
    await _consume_service_stream(plan_stream(session_id, request), "方案规划")
    session = get_session(session_id)
    plan = session.get("plan")
    if not isinstance(plan, dict):
        raise RuntimeError("方案规划没有产生可确认 plan")
    confirm_survey_plan(session_id, None, report_style="full", plan=plan)
    await compute_survey_stats(session_id, request)
    return session_id


def _terminal_usage(events: list[dict]) -> dict:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    available = False
    complete = True
    for event in events:
        if event.get("status") not in {"completed", "failed"}:
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            complete = False
            continue
        available = True
        for field in totals:
            value = usage.get(field)
            if type(value) is int and value >= 0:
                totals[field] += value
        complete = complete and bool(event.get("usage_complete"))
    return {"available": available, "complete": available and complete, **totals}


async def _run(args) -> dict:
    from app.core.config import LLM_API_KEY
    from app.core.llm_context import bind_llm_api_key, bind_llm_attempt_observer
    from app.services.insight_report_v3.contracts import artifact_without_source_text
    from app.services.insight_report_v3.findings import generate_insight_findings
    from app.storage.sessions import get_session, save_session

    all_attempt_events: list[dict] = []

    async def _attempt_observer(event: dict):
        all_attempt_events.append(deepcopy(event))

    key_context = bind_llm_api_key(LLM_API_KEY) if LLM_API_KEY else nullcontext()
    with key_context:
        session_id = args.session_id
        if not session_id:
            context = (
                json.loads(args.context_json.read_text(encoding="utf-8"))
                if args.context_json else {}
            )
            # Existing preparation services do not accept a per-call observer,
            # so bind it only around those calls. Stage one passes the same
            # observer explicitly and must not double-record through ContextVar.
            with bind_llm_attempt_observer(_attempt_observer):
                session_id = await _prepare_session(args.xlsx, context)

        session = get_session(session_id)
        required = ("rows", "plan", "open_text", "file_sha256")
        missing = [field for field in required if not session.get(field)]
        if missing:
            raise RuntimeError(f"session 尚未完成前四步，缺少：{', '.join(missing)}")

        checkpoint = None
        artifact = None
        stage_attempt_start = len(all_attempt_events)
        async for event_type, payload in generate_insight_findings(
            open_text=session["open_text"],
            plan=session["plan"],
            headers=list(session["rows"][0]),
            file_sha256=session["file_sha256"],
            qualitative_context=session.get("qualitative_context") or {},
            concurrency=args.concurrency,
            hard_timeout_seconds=args.hard_timeout_seconds,
            question_limit=args.question_limit,
            on_attempt_event=_attempt_observer,
        ):
            if event_type == "stage_started":
                checkpoint = payload
                session["insight_v3_findings"] = checkpoint
                save_session(session_id, session)
            elif event_type == "question_result":
                if checkpoint is None:
                    raise RuntimeError("阶段一未初始化检查点")
                by_scope = {
                    item["scope_key"]: item
                    for item in checkpoint.get("questions") or []
                }
                by_scope[payload["scope_key"]] = payload
                checkpoint["questions"] = list(by_scope.values())
                checkpoint["diagnostics"]["completed_question_count"] = len(by_scope)
                session["insight_v3_findings"] = checkpoint
                save_session(session_id, session)
                print(json.dumps({
                    "event": "question_result",
                    "scope_key": payload["scope_key"],
                    "status": payload["status"],
                    "elapsed_seconds": (payload.get("diagnostics") or {}).get("elapsed_seconds", 0),
                }, ensure_ascii=False), flush=True)
            elif event_type == "warning":
                print(json.dumps({"event": "warning", **payload}, ensure_ascii=False), flush=True)
            elif event_type == "result":
                artifact = payload
                session["insight_v3_findings"] = artifact
                save_session(session_id, session)

        if artifact is None:
            raise RuntimeError("阶段一没有产生最终产物")
        stage_attempt_events = all_attempt_events[stage_attempt_start:]
        artifact["diagnostics"]["observed_usage"] = _terminal_usage(stage_attempt_events)
        session["insight_v3_findings"] = artifact
        save_session(session_id, session)

    public = artifact_without_source_text(artifact)
    public["replay"] = {
        "session_id": session_id,
        "preparation": "manual_session" if args.session_id else "automatic_existing_services",
        "source_filename": str(session.get("filename") or ""),
        "question_limit": args.question_limit,
        "concurrency": args.concurrency,
        "all_http_attempts": sum(1 for event in all_attempt_events if event.get("status") == "started"),
        "all_usage": _terminal_usage(all_attempt_events),
    }
    return public


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx", nargs="?", type=Path)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--context-json", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--question-limit", type=int)
    parser.add_argument("--run-label", default="")
    parser.add_argument("--hard-timeout-seconds", type=float)
    parser.add_argument("--allow-worktree-runtime", action="store_true")
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    if not args.real:
        parser.error("该入口会调用真实模型；必须显式传入 --real")
    if not args.session_id and not args.xlsx:
        parser.error("自动准备需要 XLSX；复用人工流程时传 --session-id")
    if args.xlsx:
        args.xlsx = args.xlsx.resolve()
        if not args.xlsx.is_file():
            parser.error("XLSX 不存在")
    if args.context_json:
        args.context_json = args.context_json.resolve()
    if args.env_file:
        args.env_file = args.env_file.resolve()
    args.runtime_root = args.runtime_root.resolve()
    if args.runtime_root == ROOT and not args.allow_worktree_runtime:
        parser.error("工作树运行目录必须显式传 --allow-worktree-runtime")
    if args.runtime_root != ROOT and args.runtime_root.is_relative_to(ROOT):
        parser.error("隔离 runtime 不得放在仓库内部")
    if args.concurrency < 1:
        parser.error("concurrency 必须大于 0")
    if args.question_limit is not None and args.question_limit < 1:
        parser.error("question-limit 必须大于 0")
    args.run_label = re.sub(r"[^a-zA-Z0-9_-]+", "-", args.run_label).strip("-")[:60]
    return args


def main() -> int:
    args = _parse_args()
    args.runtime_root.mkdir(parents=True, exist_ok=True)
    evidence_dir = args.runtime_root / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(args.runtime_root)
    os.environ["DATA_DIR"] = str(args.runtime_root / "data")
    os.environ["FEISHU_LOGIN_REQUIRED"] = "false"
    _load_llm_env(args.env_file)
    sys.path.insert(0, str(ROOT))

    started = time.monotonic()
    try:
        public = asyncio.run(_run(args))
    except Exception as exc:
        failure = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        suffix = f"-{args.run_label}" if args.run_label else ""
        (evidence_dir / f"stage1-failure{suffix}.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(json.dumps(failure, ensure_ascii=False), flush=True)
        return 1

    public["replay"]["elapsed_seconds"] = round(time.monotonic() - started, 3)
    suffix = f"-{args.run_label}" if args.run_label else ""
    output_path = evidence_dir / f"stage1-findings{suffix}.json"
    output_path.write_text(
        json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps({
        "status": public["diagnostics"]["status"],
        "session_id": public["replay"]["session_id"],
        "status_counts": public["diagnostics"]["status_counts"],
        "logical_calls": public["diagnostics"]["logical_calls"],
        "upstream_attempts": public["diagnostics"]["upstream_attempts"],
        "elapsed_seconds": public["replay"]["elapsed_seconds"],
        "evidence": str(output_path),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
