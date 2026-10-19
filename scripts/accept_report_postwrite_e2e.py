"""Launch the real business UI in isolation; observe and bound real LLM traffic.

No synthetic business result, prompt change, automatic UI action or model retry.
Without --serve this only prepares the approved output directory.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import inspect
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def input_estimate(messages):
    text = "\n".join(m["content"] for m in messages)
    return int(sum(1.5 if ord(c) > 127 else 0.4 for c in text)) + 2048


def stage_for(messages):
    try:
        payload = json.loads(messages[-1]["content"])
    except (ValueError, KeyError, TypeError):
        return "preparation"
    if not isinstance(payload, dict):
        return "preparation"
    if "blocks" in payload and "report" in payload:
        return "registry"
    if "targets" in payload and "report" in payload:
        return "meaning"
    if "tasks" in payload and "source_units" in payload:
        return "quantity"
    if "players" in payload and "research_task" in payload and "questions" in payload:
        return "writer"
    return "preparation"


class AcceptanceStop(ValueError):
    pass


class AcceptanceBudget:
    """A single persistent budget for preparation + writer + registration + audit."""
    def __init__(self, output, model, *, clock=time.monotonic, max_calls=18,
                 max_tokens=3000000, max_seconds=3600, call_seconds=480, allowed_payloads=None):
        self.output, self.model, self.clock = Path(output), model, clock
        self.max_calls, self.max_tokens = max_calls, max_tokens
        self.max_seconds, self.call_seconds = max_seconds, call_seconds
        self.started = None
        self.entries = []
        self.stop_reason = ""
        self.allowed_payloads = allowed_payloads
        if (self.output / "calls.json").exists():
            raise ValueError("已有调用账本，禁止重启后重置本轮付费预算")

    def summary(self):
        return {"actual_http_requests": sum(sum(e.get("status") == "started" for e in x["events"]) for x in self.entries),
                "logical_calls": len(self.entries),
                "tokens": sum(x.get("actual_tokens", 0) for x in self.entries),
                "charged_or_reserved_tokens": sum(x["reserved"] for x in self.entries),
                "usage_complete": all(x.get("usage_complete", False) for x in self.entries),
                "paid_window_seconds": 0 if self.started is None else round(self.clock() - self.started, 3),
                "model_seconds": round(sum(x.get("elapsed_seconds", 0) for x in self.entries), 3),
                "stop_reason": self.stop_reason, "model": self.model,
                "limits": {"calls": self.max_calls, "tokens": self.max_tokens, "seconds": self.max_seconds}}

    def save(self):
        write_json(self.output / "calls.json", self.entries)
        write_json(self.output / "usage.json", self.summary())

    def stop(self, reason):
        self.stop_reason = reason
        self.save()
        raise AcceptanceStop(reason)

    async def call(self, original, messages, **kwargs):
        if self.stop_reason:
            raise AcceptanceStop(self.stop_reason)
        if any(x["state"] != "complete" or not x.get("usage_complete") for x in self.entries):
            self.stop("前次调用未完整结束或用量缺失，停止后续请求")
        stage = stage_for(messages)
        if self.allowed_payloads is not None:
            payload = json.loads(messages[-1]["content"]) if stage == "quantity" else None
            if (stage != "quantity" or len(self.entries) >= len(self.allowed_payloads)
                    or payload != self.allowed_payloads[len(self.entries)]):
                self.stop("续跑只允许既定剩余人数批次，禁止重新写作、登记、原意核验或其他调用")
        cap = int(kwargs.get("max_tokens") or 16000)
        reserve = input_estimate(messages) + cap
        preparation = sum(x["stage"] == "preparation" for x in self.entries)
        report = len(self.entries) - preparation
        if stage == "preparation" and preparation >= 4:
            self.stop("准备阶段达到4次调用预算")
        if stage != "preparation" and report >= 14:
            self.stop("报告阶段达到14次调用预算")
        if len(self.entries) >= self.max_calls or sum(x["reserved"] for x in self.entries) + reserve > self.max_tokens:
            self.stop("全流程调用或tokens预算不足，保留已有结果")
        remaining = self.max_seconds if self.started is None else self.max_seconds - (self.clock() - self.started)
        if remaining <= 0:
            self.stop("本轮付费时间预算已结束")
        if self.started is None:
            self.started = self.clock()
        number = len(self.entries) + 1
        entry = {"number": number, "stage": stage, "state": "started", "reserved": reserve,
                 "input_estimate": reserve - cap, "output_cap": cap, "events": [],
                 "started_at": datetime.now(timezone.utc).isoformat()}
        self.entries.append(entry)
        write_json(self.output / "requests" / f"{number:02d}.json", {"messages": messages,
                   "stage": stage, "model": self.model, "output_cap": cap,
                   "reasoning_effort": kwargs.get("reasoning_effort")})
        self.save()
        callback = kwargs.get("on_attempt_event")
        async def event(item):
            safe = {k: item[k] for k in ("status", "call_id", "request_id", "attempt_kind", "attempt", "model",
                    "response_model", "protocol", "fallback", "usage", "usage_complete", "finish_reason",
                    "elapsed_seconds", "http_status", "max_output_tokens") if k in item}
            entry["events"].append(safe)
            if item.get("status") == "started" and sum(e.get("status") == "started" for e in entry["events"]) > 1:
                self.stop("禁止重试或协议、模型回退")
            if item.get("status") in {"completed", "failed"}:
                entry["usage_complete"] = bool(item.get("usage_complete"))
                usage = item.get("usage") or {}
                entry["usage"] = usage
                if entry["usage_complete"]:
                    entry["actual_tokens"] = usage.get("total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
                    entry["reserved"] = entry["actual_tokens"]
                if (item.get("status") == "failed" or not entry["usage_complete"] or
                        item.get("finish_reason") in {"length", "max_tokens", "max_output_tokens"} or
                        usage.get("output_tokens", 0) > cap):
                    self.stop_reason = "请求失败、用量不完整、输出截断或超过输出上限，停止后续调用"
            self.save()
            if callback:
                result = callback(item)
                if inspect.isawaitable(result):
                    await result
        kwargs.update(models=[self.model], max_http_attempts=1, on_attempt_event=event)
        start = self.clock()
        try:
            result = await asyncio.wait_for(original(messages, **kwargs), timeout=min(remaining, self.call_seconds))
            (self.output / "responses").mkdir(exist_ok=True)
            (self.output / "responses" / f"{number:02d}.txt").write_text(result[0], encoding="utf-8")
            if self.stop_reason or not entry.get("usage_complete"):
                self.stop(self.stop_reason or "未取得完整用量")
            entry["state"] = "complete"
            return result
        except BaseException as exc:
            entry["state"] = "failed"
            entry["error_type"] = type(exc).__name__
            self.stop_reason = self.stop_reason or "请求失败，保留结果并停止后续调用"
            raise
        finally:
            entry["elapsed_seconds"] = round(self.clock() - start, 3)
            self.save()


def prepare_resume(output, source):
    """Copy only runtime inputs/checkpoint; preserve the original run and ledger."""
    output, source = Path(output).resolve(), Path(source).resolve()
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("续跑目录必须与原验收目录隔离")
    if (output / "reuse.json").exists() or (output / "calls.json").exists():
        raise ValueError("续跑目录已有账本或复用记录，禁止重置")
    runs = list((source / "data" / "report_postwrite_runs").glob("*.json"))
    if len(runs) != 1:
        raise ValueError("必须明确唯一检查点")
    run = json.loads(runs[0].read_text(encoding="utf-8"))
    state = run["state"]
    if (not state.get("draft") or not (state.get("registry") or {}).get("complete")
            or any(c.get("state") != "complete" or not c.get("usage_complete") for c in state["calls"])):
        raise ValueError("检查点未完整登记或历史调用用量不完整")
    history = source / "data" / "history.json"
    ledger = source / "calls.json"
    paths = [runs[0], history, ledger]
    paths += list((source / "data" / "sessions").glob("*.json"))
    if (source / "data" / "prompts.json").exists():
        paths.append(source / "data" / "prompts.json")
    targets = [(p, output / ("prior-calls.json" if p == ledger else p.relative_to(source))) for p in paths]
    if any(dst.exists() for _, dst in targets):
        raise ValueError("续跑目标文件已存在，禁止覆盖")
    manifest = {}
    for src, dst in targets:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        if hashlib.sha256(dst.read_bytes()).hexdigest() != digest:
            raise ValueError("检查点复制校验失败")
        manifest[str(src)] = digest
    write_json(output / "reuse.json", {"source": str(source), "files": manifest,
               "run_file": runs[0].name, "prior_usage": state.get("usage"),
               "limits": {"new_calls": 4, "new_tokens": 400000, "new_seconds": 1200}})
    return state


def remaining_count_payloads(run):
    """Read-only preflight using the same task binding as the business pipeline."""
    from app.services.report_postwrite_audit import count_scope, count_method, bind_members
    from app.services.report_postwrite_inputs import fingerprint, validate_frozen
    packet = validate_frozen(run["inputs"]["frozen"])
    pending = []
    for task in run["state"]["registry"]["tasks"]:
        players, units = count_scope(task, packet)
        if count_method(task) == "semantic":
            pending.append((task, players, units))
    population = sorted({p for _, players, _ in pending for p in players})
    payloads = []
    for start in range(0, len(population), 20):
        group, todo = population[start:start + 20], []
        for task, players, units in pending:
            scoped = [p for p in group if p in players]
            key = "members:" + task["id"] + ":" + fingerprint(scoped)[:12]
            selected = [u for u in units if u["player"] in scoped]
            existing = run["state"]["steps"].get(key)
            valid = False
            if existing is not None:
                rebound = bind_members({"members": existing.get("members", [])}, task, scoped, selected)
                structural = [e for e in existing.get("errors", []) if not e.endswith(":计数有歧义")]
                valid = not rebound["errors"] and not structural
            if scoped and not valid:
                todo.append((task, scoped, selected))
        if todo:
            payloads.append({"tasks": [{"id": t["id"], "task": t, "players": ps,
                             "allowed_refs": [u["ref"] for u in us]} for t, ps, us in todo],
                             "source_units": list({u["ref"]: u for _, _, us in todo for u in us}.values())})
    return payloads


def configure_isolation(output, repo, env_file=None, *, resume_state=None):
    """Must run before any app import; original .env is read only, never copied."""
    output, repo = Path(output).resolve(), Path(repo).resolve()
    if output == repo or repo in output.parents or output in repo.parents:
        raise ValueError("验收输出必须在仓库之外")
    output.mkdir(parents=True, exist_ok=True)
    from dotenv import load_dotenv
    load_dotenv(Path(env_file) if env_file else repo / ".env")
    values = {"DATA_DIR": str(output / "data"), "TEMP": str(output / "tmp"), "TMP": str(output / "tmp"),
              "PYTHONPYCACHEPREFIX": str(output / "pycache"), "FEISHU_LOGIN_REQUIRED": "false",
              "FEISHU_WIKI_AUTO_UPDATE_ENABLED": "false", "GOOGLE_FORMS_QUALITATIVE_ENABLED": "false",
              "INTERVIEW_V2_DATA_DIR": str(output / "data" / "interview_v2"),
              "RESEARCH_ASSET_STORAGE_DIR": str(output / "data" / "research_assets"),
              "REPORT_POSTWRITE_ENABLED": "true", "LLM_POSTWRITE_MODEL": "claude-opus-5-5",
              "LLM_POSTWRITE_REASONING": "medium", "POSTWRITE_MAX_CALLS": "14",
              "POSTWRITE_MAX_TOKENS": "2800000", "POSTWRITE_MAX_SECONDS": "3300",
              "POSTWRITE_CALL_SECONDS": "480", "POSTWRITE_OUTPUT_TOKENS": "64000",
              "POSTWRITE_REGISTRY_OUTPUT_TOKENS": "64000", "POSTWRITE_CLAIMS_PER_BATCH": "30",
              "POSTWRITE_PLAYERS_PER_BATCH": "20", "LLM_COLUMN_MODEL": "claude-opus-5-5",
              "LLM_COLUMN_FALLBACK_MODELS": "", "LLM_COLUMN_REASONING": "medium",
              "LLM_PLANNER_MODEL": "claude-opus-5-5", "LLM_PLANNER_FALLBACK_MODELS": "",
              "LLM_PLANNER_REASONING": "medium", "LLM_REPORT_MAX_ATTEMPTS": "1"}
    if resume_state is not None:
        values.update(POSTWRITE_MAX_CALLS=str(len(resume_state["calls"]) + 4),
                      POSTWRITE_MAX_TOKENS=str(resume_state["usage"]["charged_or_reserved_tokens"] + 400000),
                      POSTWRITE_MAX_SECONDS=str(math.ceil(resume_state["elapsed_seconds"] + 1200)))
    os.environ.update(values)
    sys.pycache_prefix = values["PYTHONPYCACHEPREFIX"]
    for sub in ("data", "tmp", "pycache", "requests", "responses", "screenshots", "downloads"):
        (output / sub).mkdir(exist_ok=True)
    os.chdir(output)
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    (output / "acceptance.env").write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--env-file", type=Path, help="Read existing configuration without copying secrets")
    parser.add_argument("--port", type=int, default=8881)
    parser.add_argument("--reuse-from", type=Path, help="Copy a completed registration/checkpoint for quantity-only resume")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    resume_state = prepare_resume(args.output_dir, args.reuse_from) if args.reuse_from else None
    values = configure_isolation(args.output_dir, repo, args.env_file, resume_state=resume_state)
    from app.core import config
    root = args.output_dir.resolve()
    paths = {k: str(v) for k, v in vars(config).items() if k.endswith(("_DIR", "_FILE")) and
             k in {"DATA_DIR", "INTERVIEW_V2_DATA_DIR", "RESEARCH_ASSET_STORAGE_DIR", "HISTORY_FILE",
                   "PROMPTS_FILE", "USER_LLM_USAGE_FILE", "WEB_LOGINS_FILE", "GROUP_WORKFLOW_DATA_DIR"}}
    if any(not Path(p).resolve().is_relative_to(root) for p in paths.values()):
        raise ValueError("发现隔离目录之外的数据路径")
    limits = {"calls": 4, "tokens": 400000, "seconds": 1200} if resume_state else {"calls": 18, "tokens": 3000000, "seconds": 3600}
    allowed = None
    if resume_state:
        reuse = json.loads((root / "reuse.json").read_text(encoding="utf-8"))
        run = json.loads((root / "data" / "report_postwrite_runs" / reuse["run_file"]).read_text(encoding="utf-8"))
        allowed = remaining_count_payloads(run)
        if not allowed or len(allowed) > limits["calls"]:
            raise ValueError("剩余批次数不在已批准预算内")
        write_json(root / "pending-payloads.json", allowed)
    write_json(root / "isolation.json", {"paths": paths, "config": values, "real_application": True,
               "has_api_key": bool(config.LLM_API_KEY), "budget": limits})
    if not args.serve:
        print(json.dumps({"prepared": True, "model_calls": 0, "isolated": True}), flush=True)
        return
    from app.integrations import llm_client
    budget = AcceptanceBudget(root, config.LLM_POSTWRITE_MODEL, max_calls=limits["calls"],
                              max_tokens=limits["tokens"], max_seconds=limits["seconds"], allowed_payloads=allowed)
    original = llm_client.collect_chat_completion
    async def guarded(messages, **kwargs):
        return await budget.call(original, messages, **kwargs)
    llm_client.collect_chat_completion = guarded
    from app.main import app
    import uvicorn
    print(json.dumps({"ready": True, "port": args.port, "isolated": True, "model": budget.model}), flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        budget.save()


if __name__ == "__main__":
    main()
