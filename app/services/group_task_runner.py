"""Durable fair task scheduler. Browser disconnects cannot cancel a logical run."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from time import perf_counter
import uuid

from fastapi import HTTPException

from app.core import config
from app.core.llm_context import bind_llm_api_key, bind_llm_attempt_observer
from app.core.llm_limits import AttemptGate, bind_attempt_gate
from app.core.runtime_logging import bind_runtime_operator, reset_runtime_operator, runtime_operator_from_login
from app.services import group_tasks as tasks
from app.services.annotate_workflow import annotate_group_feedback
from app.services.feature_access import require_permission, require_record
from app.services.llm_credentials import require_background_llm_api_key
from app.services.llm_usage import start_llm_usage_task
from app.storage import group_workflow as store


async def prepare_run(task_id, request, login):
    """Read a fresh roster at admission, including retries, before any model work."""
    from app.services.roster_sources import read_roster_snapshot
    task = tasks.load_task(task_id, login, write=True)
    tasks._ensure_version(task, request.expected_version, allow_busy=True)
    if (task.get("run") or {}).get("status") in tasks.ACTIVE_RUNS:
        return tasks.task_view(task, login)
    try:
        snapshot = await read_roster_snapshot(login)
    except HTTPException as exc:
        def fail(current):
            tasks._ensure_version(current, request.expected_version)
            current["roster_error"] = str(exc.detail)
            current["updated_at"] = tasks.now()
            tasks._refresh_checks(current)
        store.mutate("tasks", task_id, fail)
        raise HTTPException(409, "标注尚未启动：最新名单读取失败。" + str(exc.detail)) from None

    def admit(current):
        require_record(current, login, write=True)
        tasks._ensure_version(current, request.expected_version, allow_busy=True)
        if (current.get("run") or {}).get("status") in tasks.ACTIVE_RUNS:
            return current["version"]
        changed = snapshot["fingerprint"] != (current.get("roster_snapshot") or {}).get("fingerprint")
        current.update(roster_snapshot=snapshot, roster_error="", updated_at=tasks.now())
        if changed:
            current["version"] += 1
            current["confirmed_version"] = None
            tasks._event(current, login, "标注前刷新名单", {"snapshot_id": snapshot["id"]})
        tasks._refresh_checks(current)
        return current["version"]
    version = store.mutate("tasks", task_id, admit)
    return start_run(task_id, request.model_copy(update={"expected_version": version}), login)


def start_run(task_id, request, login):
    tasks.load_task(task_id, login, write=True)
    require_permission(login, "annotate", write=True)
    def apply(task):
        require_record(task, login, write=True)
        tasks._ensure_version(task, request.expected_version, allow_busy=True)
        if (task.get("run") or {}).get("status") in tasks.ACTIVE_RUNS:
            return tasks.task_view(task, login)
        tasks._refresh_checks(task)
        issues = tasks.readiness_issues(task)
        if issues:
            raise HTTPException(409, {"message": "请先处理必要输入", "issues": issues})
        targets = [row["id"] for row in task["feedback"] if not tasks.effective_result(task, row)["complete"]]
        if request.response_ids is not None:
            requested = set(request.response_ids)
            if not requested <= {row["id"] for row in task["feedback"]}:
                raise HTTPException(422, "重试反馈不属于此任务")
            targets = [identifier for identifier in targets if identifier in requested]
        if not targets:
            raise HTTPException(409, "没有待补齐反馈，已完成结果和人工结论保持不变")
        require_background_llm_api_key(login)
        if task.get("run"):
            task["runs"].append(deepcopy(task["run"]))
        task["run"] = {"id": uuid.uuid4().hex, "version": task["version"], "status": "queued",
                       "queued_at": tasks.now(), "schedule_at": tasks.now(), "started_at": None,
                       "target_ids": targets, "attempted_ids": [], "inflight_ids": [], "completed_ids": [],
                       "http_attempts": 0, "attempt_events": [], "error": "",
                       "queue_wait_seconds": 0.0, "execution_seconds": 0.0, "save_seconds": 0.0,
                       "completed_slices": 0, "timing_partial": False,
                       "owner_login": {key: (login or {}).get(key, "") for key in ("open_id", "email", "name")}}
        task["confirmed_version"] = None
        tasks._event(task, login, "启动后台标注", {"run_id": task["run"]["id"], "target_count": len(targets), "only_incomplete": True})
        return tasks.task_view(task, login)
    return store.mutate("tasks", task_id, apply)


def _fenced(task, run_id, version):
    run = task.get("run") or {}
    if run.get("id") != run_id or run.get("version") != version or task["version"] != version or run.get("status") not in tasks.ACTIVE_RUNS:
        raise RuntimeError("任务运行版本已失效，已拒绝迟到结果")
    return run


def recover_interrupted_runs(task_ids=None):
    for task in store.list_records("tasks"):
        if task_ids is not None and task["id"] not in task_ids:
            continue
        if (task.get("run") or {}).get("status") == "running":
            def recover(current):
                run = current.get("run") or {}
                if run.get("status") != "running":
                    return
                run.update(status="incomplete", ended_at=tasks.now(),
                           timing_partial=True,
                           error="服务中断，已保存的结果可继续使用；未确认完成的远端请求需人工重试，可能产生重复模型费用")
                for row in current["feedback"]:
                    if row["id"] in run.get("inflight_ids", []) and not tasks.effective_result(current, row)["complete"]:
                        row["model_error"] = run["error"]
                run["inflight_ids"] = []
            store.mutate("tasks", task["id"], recover)


def _attempt_reservation(task_id, run_id, version, mutate=store.mutate):
    def reserve(task):
        run = _fenced(task, run_id, version)
        started = datetime.fromisoformat(run["started_at"])
        if (datetime.now(timezone.utc) - started).total_seconds() >= config.GROUP_RUN_TIMEOUT_SECONDS:
            raise RuntimeError("本轮已达到总耗时限制，请核对未完成反馈后重试")
        if run["http_attempts"] >= config.GROUP_RUN_MAX_HTTP_ATTEMPTS:
            raise RuntimeError("本轮已达到模型请求预算，请核对未完成反馈后重试")
        run["http_attempts"] += 1
    mutate("tasks", task_id, reserve)


async def _run_slice(task_id, slots):
    current = store.read("tasks", task_id)
    login = ((current or {}).get("run") or {}).get("owner_login")
    token = bind_runtime_operator(runtime_operator_from_login(login))
    try:
        return await _run_slice_impl(task_id, slots)
    finally:
        reset_runtime_operator(token)


async def _run_slice_impl(task_id, slots):
    save_seconds = 0.0
    execution_started = perf_counter()
    claim_requested_at = datetime.now(timezone.utc)

    def measured_mutate(*args, **kwargs):
        nonlocal save_seconds
        started = perf_counter()
        try:
            return store.mutate(*args, **kwargs)
        finally:
            # Includes lock wait, read/modify and durable write; no network work.
            save_seconds += perf_counter() - started

    def claim(task):
        run = task.get("run") or {}
        if run.get("status") != "queued":
            return None
        remaining = [identifier for identifier in run["target_ids"] if identifier not in run["attempted_ids"]]
        picked = remaining[:config.GROUP_TASK_REQUEST_CONCURRENCY]
        run.update(status="running", inflight_ids=picked)
        waiting_since = datetime.fromisoformat(run.get("schedule_at", run["queued_at"]))
        run["queue_wait_seconds"] = run.get("queue_wait_seconds", 0.0) + max(
            0.0, (claim_requested_at - waiting_since).total_seconds())
        run["started_at"] = run.get("started_at") or tasks.now()
        return deepcopy(task)
    task = measured_mutate("tasks", task_id, claim)
    if task is None:
        return
    run = task["run"]
    login = run["owner_login"] or None
    if not any((login or {}).values()):
        login = None
    run_id, version = run["id"], run["version"]
    gate = AttemptGate(slots, config.GROUP_TASK_REQUEST_CONCURRENCY,
                       lambda: _attempt_reservation(task_id, run_id, version, measured_mutate))
    recorder = None
    failure = ""
    slice_failed = False
    try:
        require_permission(login, "annotate", write=True)
        key = require_background_llm_api_key(login)
        recorder = start_llm_usage_task(login, category="annotate", action="质量标注后台任务",
                                         reference_id=task_id, title=task["rules"]["name"])

        async def observe(event):
            if recorder:
                await recorder.on_attempt_event(event)
            # Never persist messages, API keys, raw exceptions or HTTP headers.
            safe = {key: event[key] for key in ("call_id", "status", "model", "attempt_kind", "started_at", "duration_ms", "usage", "fallback") if key in event}
            def save(current):
                active = _fenced(current, run_id, version)
                active["attempt_events"].append(safe)
            measured_mutate("tasks", task_id, save)

        async def one(row):
            nonlocal slice_failed
            def checkpoint(update):
                def save(current):
                    active = _fenced(current, run_id, version)
                    saved = tasks._row(current, row["id"])
                    for field in ("quality", "quality_complete", "ai", "ai_complete", "model_error"):
                        if field in update:
                            saved[field] = deepcopy(update[field])
                    if tasks.effective_result(current, saved)["complete"] and row["id"] not in active["completed_ids"]:
                        active["completed_ids"].append(row["id"])
                    current["updated_at"] = tasks.now()
                measured_mutate("tasks", task_id, save)
            try:
                remaining_time = config.GROUP_RUN_TIMEOUT_SECONDS - (datetime.now(timezone.utc) - datetime.fromisoformat(run["started_at"])).total_seconds()
                async with asyncio.timeout(max(0, remaining_time)):
                    await annotate_group_feedback(task, row, checkpoint)
            except asyncio.CancelledError:
                raise
            except Exception:
                slice_failed = True
                checkpoint({"model_error": "本份标注未完成（上游失败、超时或结果校验未通过），已保存成功部分；可重试此份或全部待补齐反馈"})
            finally:
                def finish_row(current):
                    active = _fenced(current, run_id, version)
                    if row["id"] not in active["attempted_ids"]:
                        active["attempted_ids"].append(row["id"])
                measured_mutate("tasks", task_id, finish_row)
        with bind_llm_api_key(key), bind_llm_attempt_observer(observe), bind_attempt_gate(gate):
            await asyncio.gather(*(one(row) for row in task["feedback"] if row["id"] in run["inflight_ids"]))
    except asyncio.CancelledError:
        failure = "服务已停止；成功结果已保存，未完成的远端调用状态不确定，重试可能产生重复模型费用"
        raise
    except HTTPException as exc:
        detail = exc.detail
        failure = detail.get("message", "权限或个人模型凭据不可用") if isinstance(detail, dict) else str(detail)
    except Exception:
        failure = "后台执行遇到异常，已保存成功结果，请检查平台运行日志并重试"
    finally:
        if recorder:
            recorder.finish("failed" if failure or slice_failed else "completed")
        def finish(current):
            active = _fenced(current, run_id, version)
            active["inflight_ids"] = []
            expired = (datetime.now(timezone.utc) - datetime.fromisoformat(active["started_at"])).total_seconds() >= config.GROUP_RUN_TIMEOUT_SECONDS
            exhausted = active["http_attempts"] >= config.GROUP_RUN_MAX_HTTP_ATTEMPTS
            remaining = set(active["target_ids"]) - set(active["attempted_ids"])
            if remaining and not failure and not expired and not exhausted:
                # Round robin: put this task behind already waiting tasks after a bounded slice.
                active.update(status="queued", schedule_at=tasks.now())
            else:
                complete = len(active["completed_ids"]) == len(active["target_ids"])
                active.update(status="completed" if complete else "incomplete", ended_at=tasks.now(),
                              error=failure or ("本轮达到执行预算，剩余反馈可明确重试" if expired or exhausted else ""))
            current["updated_at"] = tasks.now()
        measured_mutate("tasks", task_id, finish)
        elapsed = perf_counter() - execution_started

        def save_timing(current):
            active = current.get("run") or {}
            if active.get("id") != run_id or current["version"] != version:
                return
            # Execution is elapsed slice time (not summed per concurrent request).
            # Save time is a separate measured subset, not an additive phase.
            active["execution_seconds"] = active.get("execution_seconds", 0.0) + elapsed
            active["save_seconds"] = active.get("save_seconds", 0.0) + save_seconds
            active["completed_slices"] = active.get("completed_slices", 0) + 1
        # Exclude this diagnostics-only write from save_seconds to avoid recursion.
        store.mutate("tasks", task_id, save_timing)


async def _scheduler():
    active = {}
    slots = asyncio.Semaphore(config.GROUP_MODEL_CONCURRENCY)
    try:
        while True:
            # Other web workers can take over after this process releases its lease.
            with store.runner_lease() as acquired:
                if not acquired:
                    await asyncio.sleep(1)
                    continue
                try:
                    recover_interrupted_runs()
                    while True:
                        for identifier, running in list(active.items()):
                            if running.done():
                                try:
                                    running.result()
                                except Exception:
                                    recover_interrupted_runs({identifier})
                                del active[identifier]
                        queued = sorted((t for t in store.list_records("tasks") if (t.get("run") or {}).get("status") == "queued" and t["id"] not in active),
                                        key=lambda t: (t["run"].get("schedule_at", t["run"]["queued_at"]), t["run"]["id"]))
                        for task in queued[:max(0, config.GROUP_TASK_CONCURRENCY - len(active))]:
                            active[task["id"]] = asyncio.create_task(_run_slice(task["id"], slots))
                        await asyncio.sleep(0.25)
                finally:
                    for running in active.values():
                        running.cancel()
                    if active:
                        await asyncio.gather(*active.values(), return_exceptions=True)
                    active.clear()
    finally:
        for running in active.values():
            running.cancel()
        if active:
            await asyncio.gather(*active.values(), return_exceptions=True)


@asynccontextmanager
async def group_workflow_lifespan(app):
    scheduler = asyncio.create_task(_scheduler())
    try:
        yield
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
