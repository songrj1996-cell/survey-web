"""Synthetic full-platform server. No real model, credentials, or Feishu mode.

Invoked by tests/test_postwrite_player_cases_platform.js in a fresh process.
All application imports happen after working-directory, write and network guards.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    allowed = repo / ".test-artifacts" / "player-cases-platform"
    output = args.output.resolve()
    if not output.is_relative_to(allowed.resolve()) or output == allowed.resolve():
        raise ValueError("运行产物必须位于已批准的独立运行目录")
    if (output / "data").exists():
        raise ValueError("拒绝覆盖已有运行数据；请使用新运行编号")
    for name in ("data", "tmp", "pycache", "browser"):
        (output / name).mkdir(parents=True, exist_ok=True)
    os.chdir(output)
    sys.path.insert(0, str(repo))
    sys.pycache_prefix = str(output / "pycache")
    for key in list(os.environ):
        if key.startswith(("LLM_", "FEISHU_", "POSTWRITE_")) or any(word in key for word in ("API_KEY", "SECRET", "ACCESS_TOKEN")):
            os.environ.pop(key)
    os.environ.update({
        "DATA_DIR": str(output / "data"), "TEMP": str(output / "tmp"), "TMP": str(output / "tmp"),
        "PYTHONPYCACHEPREFIX": str(output / "pycache"), "PYTHON_DOTENV_DISABLED": "1",
        "FEISHU_LOGIN_REQUIRED": "false", "FEISHU_WIKI_AUTO_UPDATE_ENABLED": "false",
        "FEISHU_NAVIGATION_NOTIFICATIONS_ENABLED": "false", "GOOGLE_FORMS_QUALITATIVE_ENABLED": "false",
        "REPORT_POSTWRITE_ENABLED": "true", "LLM_API_KEY": "synthetic-never-sent",
        "LLM_POSTWRITE_MODEL": "synthetic-offline-only",
        "INTERVIEW_V2_DATA_DIR": str(output / "data" / "interview_v2"),
        "RESEARCH_ASSET_STORAGE_DIR": str(output / "data" / "research_assets"),
    })
    writes, blocked_network, rejected_writes, calls = set(), [], [], []

    def check_write(value):
        if isinstance(value, int) or value is None:
            return
        candidate = Path(os.fsdecode(value)).resolve()
        if not candidate.is_relative_to(output):
            rejected_writes.append(str(candidate))
            raise PermissionError("测试隔离阻止目录外写入：" + str(candidate))
        writes.add(str(candidate.relative_to(output)))

    def loopback(host):
        if host in ("localhost", ""):
            return True
        try:
            return ipaddress.ip_address(str(host)).is_loopback
        except ValueError:
            return False

    def guard(event, values):
        if event == "open":
            filename, mode, flags = values
            if (mode and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                check_write(filename)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime"}:
            check_write(values[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            check_write(values[0]); check_write(values[1])
        elif event == "socket.connect" and isinstance(values[1], tuple) and not loopback(values[1][0]):
            blocked_network.append({"host": str(values[1][0]), "expected_probe": str(values[1][0]) == "192.0.2.1"})
            raise PermissionError("隔离验证禁止外部连接")
        elif event == "socket.getaddrinfo" and not loopback(values[0]):
            blocked_network.append({"host": str(values[0]), "expected_probe": False})
            raise PermissionError("隔离验证禁止外部域名解析")

    sys.addaudithook(guard)
    # No outbound connection is made: the audit hook rejects this before connect.
    try:
        with socket.socket() as probe:
            probe.connect(("192.0.2.1", 9))
    except PermissionError:
        pass
    else:
        raise AssertionError("网络隔离探针未被阻止")
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    from app.core import config
    from app.services.report_postwrite_inputs import freeze_inputs, report_blocks
    from app.services.report_postwrite_pipeline import initial_draft_state, run_pipeline, audit_summary
    from app.services.report_versions import append_report_version
    from app.storage.history import _save_history

    configured = {k: str(v) for k, v in vars(config).items()
                  if k in {"DATA_DIR", "GROUP_WORKFLOW_DATA_DIR", "FEISHU_NAVIGATION_DATA_DIR",
                           "REPORT_SOURCE_TRANSLATIONS_DIR", "INTERVIEW_V2_DATA_DIR", "RESEARCH_ASSET_STORAGE_DIR"}
                  or k.endswith("_FILE") and str(v).startswith(str(output))}
    for value in configured.values():
        if not Path(value).resolve().is_relative_to(output):
            raise AssertionError("配置路径未隔离")
    fixture = json.loads((repo / "tests/fixtures/postwrite_player_cases_platform.json").read_text(encoding="utf-8"))
    prompts = {"writer": config.DEFAULT_POSTWRITE_WRITER, "registry": config.DEFAULT_POSTWRITE_REGISTRY,
               "audit": config.DEFAULT_POSTWRITE_AUDITOR}
    frozen = freeze_inputs(fixture["session"])
    phase = "seed"
    failure_remaining = 1

    def persist_evidence():
        write_json(output / "collector-calls.json", calls)
        write_json(output / "isolation.json", {"cwd": str(Path.cwd()), "configured_paths": configured,
                   "session_directory": str(output / "data/sessions"), "write_paths": sorted(writes),
                   "rejected_writes": rejected_writes, "network_blocks": blocked_network,
                   "real_model_requests": 0, "feishu_exports": 0, "dotenv_loaded": False,
                   "background_workers": "test-process no-op lifespan"})

    def registry(report):
        version = "v2" if "V2" in report else "v1"
        blocks = report_blocks(report)
        claims = []
        for i, original in enumerate(fixture["claims"][version], 1):
            block = next(b for b in blocks if original in b["text"])
            claims.append({"id": f"C{i:03d}", "original": original, "block_ids": [block["id"]],
                           "kind": "fact", "review_refs": ["q1/r2" if i == 1 else "q1/r3"]})
        return {"claims": claims, "tasks": [], "coverage": [
            {"block_id": b["id"], **({"claim_ids": [c["id"] for c in claims if b["id"] in c["block_ids"]]}
             if any(b["id"] in c["block_ids"] for c in claims) else {"no_claim": True})} for b in blocks]}

    async def collect(messages, **kwargs):
        nonlocal failure_remaining
        payload = json.loads(messages[-1]["content"])
        stage = "meaning" if "targets" in payload else "registry" if "blocks" in payload else "writer"
        item = {"phase": phase, "stage": stage, "targets": [t["id"] for t in payload.get("targets", [])],
                "time": datetime.now().astimezone().isoformat(), "synthetic": True}
        calls.append(item)
        if len(calls) > 16 or stage == "writer" or phase == "browser" and stage != "meaning":
            item["result"] = "rejected_unexpected_stage"; persist_evidence()
            raise AssertionError("不允许写作、重新登记或超过合成调用上限")
        if phase == "browser" and "失败恢复" in payload.get("report", "") and failure_remaining:
            failure_remaining -= 1
            item["result"] = "controlled_failure"; persist_evidence()
            raise RuntimeError("固定合成失败：仅测试保留和继续")
        if stage == "registry":
            answer = registry(payload["report"])
        else:
            targets = payload["targets"]
            if phase == "seed-partial":
                targets = targets[:1]
            answer = {"checks": [{"id": t["id"], **{k: v for k, v in fixture["response"].items() if k not in {"relation", "meaning"}},
                "sources": [{"ref": ref, "relation": fixture["response"]["relation"],
                             "meaning": fixture["response"]["meaning"]} for ref in t["review_refs"]]} for t in targets]}
        callback = kwargs.get("on_attempt_event")
        if callback:
            await callback({"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                            "usage_complete": True, "finish_reason": "stop"})
        item["result"] = "fixed_response"; persist_evidence()
        return json.dumps(answer, ensure_ascii=False), "synthetic-offline-only"

    async def seed():
        nonlocal phase
        entries = []
        for ident, versions in (("synthetic-player-cases", ("v1", "v2")), ("synthetic-player-cases-failure", ("v1",))):
            entry = {"id": ident, "mode": "standard", "created_at": datetime.now().isoformat(),
                     "report_no": "R-901" if len(versions) == 2 else "R-902", "row_count": 2,
                     "filename": "仅合成样本.xlsx", "report_mode": "insight", "report_style": "full"}
            for number, key in enumerate(versions, 1):
                draft = fixture[key]
                if ident.endswith("failure"):
                    draft = draft.replace("# 合成观点洞察 V1", "# 合成观点洞察 失败恢复")
                phase = "seed-complete" if key == "v2" else "seed-partial"
                state = await run_pipeline(frozen, prompts=prompts,
                    previous=initial_draft_state(frozen, prompts=prompts, draft=draft), collect=collect)
                expected = "complete" if key == "v2" else "partial"
                assert state["audit"]["status"] == expected, state
                assert state["registry"]["complete"] and len(state["audit"]["checks"]) == (2 if key == "v2" else 1)
                snapshot = {"report_md": draft, "title": draft.splitlines()[0].lstrip("# "),
                            "report_mode": "insight", "report_style": "full", "report_status": expected,
                            "input_snapshot": deepcopy(frozen), "report_audit": {**audit_summary(state), "checkpoint": state},
                            "qa_messages": []}
                append_report_version(entry, snapshot, kind="initial" if number == 1 else "regenerate",
                                      base_version=None if number == 1 else 1)
            entries.append(entry)
        _save_history(entries)
        write_json(output / "before-history.json", entries)
        phase = "browser"

    asyncio.run(seed())
    from app.services import survey_service, group_task_runner
    survey_service.collect_chat_completion = collect

    @asynccontextmanager
    async def idle_lifespan(app):
        yield

    group_task_runner.group_workflow_lifespan = idle_lifespan
    from app.services import feishu_navigation_auto_update
    feishu_navigation_auto_update.navigation_lifespan = idle_lifespan
    from app.main import app
    from app.storage import sessions
    assert sessions._SESSION_DIR.resolve().is_relative_to(output)
    # HTTP/SSE/business handlers stay untouched. Only record completed requests.
    @app.middleware("http")
    async def evidence(request, call_next):
        if "feishu" in request.url.path and request.method != "GET" or "/export" in request.url.path:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "本轮禁止真实飞书导出"}, status_code=403)
        response = await call_next(request)
        with (output / "http-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": datetime.now().astimezone().isoformat(), "method": request.method,
                "path": request.url.path, "query": request.url.query, "status": response.status_code}, ensure_ascii=False) + "\n")
        persist_evidence()
        return response

    persist_evidence()
    hashes = {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in [repo / "static/index.html", repo / "static/style.css", *repo.glob("static/js/**/*.js"), *repo.glob("app/**/*.py")]}
    write_json(output / "source-hashes.json", hashes)
    print("隔离检查通过：合成数据，真实模型与飞书请求为零。", flush=True)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
