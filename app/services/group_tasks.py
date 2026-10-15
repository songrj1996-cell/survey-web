"""Task lifecycle, input checking and per-response rewards. No browser execution state."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from fastapi import HTTPException

from app.core import config
from app.core.parsing import parse_file_with_cell_metadata
from app.schemas.group_tasks import FeedbackMapping, TaskRules
from app.services.feature_access import can_read, owner_identity, require_permission, require_record
from app.storage import group_workflow as store

LABELS = ("优秀反馈", "有效反馈", "无效反馈")
ACTIVE_RUNS = {"queued", "running"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _event(task: dict, login: dict | None, action: str, details=None) -> None:
    who = owner_identity(login)
    task.setdefault("events", []).append({
        "id": uuid.uuid4().hex, "at": now(), "actor_key": who["owner_key"],
        "actor_name": who["owner_name"], "action": action,
        "version": task["version"], "details": deepcopy(details or {}),
    })
    from app.services.audit import _audit_log_from_login
    from app.core.runtime_logging import runtime_log
    try:
        _audit_log_from_login(None, login, "annotate", action, task["rules"].get("name", ""),
                              metadata={"task_id": task["id"], "version": task["version"]})
    except Exception:
        runtime_log("审计记录", "warning", "质量任务的全局审计记录写入失败；任务内仍保留处理记录")


def load_task(task_id: str, login: dict | None, *, write=False, feature="annotate") -> dict:
    require_permission(login, feature, write=write)
    try:
        value = store.read("tasks", task_id)
    except ValueError:
        raise HTTPException(404, "任务不存在或无权访问") from None
    return require_record(value, login, write=write)


def _ensure_version(task: dict, version: int, *, allow_busy=False) -> None:
    if task["version"] != version:
        raise HTTPException(409, "任务已更新，请刷新后核对新版本再操作")
    if not allow_busy and (task.get("run") or {}).get("status") in ACTIVE_RUNS:
        raise HTTPException(409, "本任务正在排队或标注，影响结果的输入已锁定；其他任务仍可操作")


def create_task(rules: TaskRules, login: dict | None) -> dict:
    require_permission(login, "annotate", write=True)
    identifier = uuid.uuid4().hex
    task = {
        "id": identifier, **owner_identity(login), "created_at": now(), "updated_at": now(),
        "version": 1, "rules": rules.model_dump(mode="json"), "confirmed_version": None,
        "feedback": [], "uploads": [], "mapping": None, "headers": [],
        "duplicate_decisions": {}, "player_adv": {}, "events": [], "runs": [], "run": None,
        "roster_snapshot": None, "roster_error": "", "ai_confirmation_complete": False,
    }
    _event(task, login, "创建质量标注任务")
    store.create("tasks", identifier, task)
    return task_view(task, login)


def _change(task_id: str, login: dict | None, version: int, action: str, mutate) -> dict:
    load_task(task_id, login, write=True)
    def apply(task):
        require_record(task, login, write=True)
        _ensure_version(task, version)
        details = mutate(task)
        task["version"] += 1
        task["confirmed_version"] = None
        task["updated_at"] = now()
        _refresh_checks(task)
        _event(task, login, action, details)
        return task_view(task, login)
    try:
        return store.mutate("tasks", task_id, apply)
    except FileNotFoundError:
        raise HTTPException(404, "任务不存在或无权访问") from None


def _invalidate_model(row: dict) -> None:
    if row.get("ai") or row.get("quality") or row.get("manual_quality"):
        row.setdefault("judgment_history", []).append({
            "at": now(), "ai": deepcopy(row.get("ai")), "quality": deepcopy(row.get("quality")),
            "manual_quality": deepcopy(row.get("manual_quality")),
        })
    for key in ("ai", "quality", "manual_quality", "model_error", "quality_complete", "ai_complete", "confirmed_ai"):
        row.pop(key, None)


def update_rules(task_id, version, rules: TaskRules, login):
    def mutate(task):
        previous = task["rules"]
        updated = rules.model_dump(mode="json")
        semantic_change = any(previous.get(key) != updated.get(key) for key in ("announcement", "ai_detect"))
        if semantic_change:
            for row in task["feedback"]:
                _invalidate_model(row)
            task["ai_confirmation_complete"] = False
        task["rules"] = updated
        if previous["allow_repeat"] != updated["allow_repeat"]:
            task["duplicate_decisions"] = {}
        return {"before": previous, "after": updated, "model_context_changed": semantic_change}
    return _change(task_id, login, version, "修改任务规则", mutate)


def _mapping_guess(headers: list[str]) -> dict:
    def find(words):
        return next((i for i, h in enumerate(headers) if re.sub(r"[ _-]", "", str(h)).casefold() in words), None)
    player = find({"playerid", "discordid", "whatsappid", "玩家id", "discord/whatsappid"})
    count = find({"实际对局次数", "实际体验次数", "对局次数", "matches", "matchcount", "actualmatches"})
    adv = find({"adv", "adv账号", "advaccount"})
    return {"id_col": player, "count_col": count, "adv_col": adv, "open_text_cols": []}


def upload_feedback(task_id, version, filename: str, content: bytes, login):
    load_task(task_id, login, write=True)
    if len(content) > config.REWARD_ROSTER_MAX_UPLOAD_BYTES:
        raise HTTPException(413, "上传反馈超过大小限制")
    try:
        rows, cell_issues = parse_file_with_cell_metadata(filename, content)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if len(rows) < 2:
        raise HTTPException(422, "文件为空或只有表头")
    if len(rows) - 1 > config.GROUP_FEEDBACK_MAX_ROWS:
        raise HTTPException(413, f"反馈数量超过配置上限 {config.GROUP_FEEDBACK_MAX_ROWS}")
    upload_id = uuid.uuid4().hex
    def mutate(task):
        digest = store.save_blob(upload_id, content)
        if task["feedback"]:
            task.setdefault("feedback_versions", []).append({
                "version": task["version"], "headers": task["headers"], "mapping": task["mapping"],
                "feedback": task["feedback"],
            })
        task["headers"] = [str(item) for item in rows[0]]
        task["mapping"] = None
        task["mapping_suggestion"] = _mapping_guess(task["headers"])
        task["feedback"] = [
            {"id": uuid.uuid4().hex, "source_row": index + 1, "upload_id": upload_id,
             "cells": [str(value) if value is not None else "" for value in row],
             "cell_issues": {str(c): cell_issues[f"{index}:{c}"] for c in range(len(row)) if f"{index}:{c}" in cell_issues},
             "corrections": {}, "reviews": []}
            for index, row in enumerate(rows[1:], 1) if any(str(value).strip() for value in row)
        ]
        if not task["feedback"]:
            raise HTTPException(422, "没有非空反馈行")
        task["uploads"].append({"id": upload_id, "filename": str(filename).replace("\\", "/").split("/")[-1],
                                "sha256": digest, "at": now()})
        task["duplicate_decisions"] = {}
        task["player_adv"] = {}
        task["ai_confirmation_complete"] = False
        return {"upload_id": upload_id, "rows": len(task["feedback"])}
    return _change(task_id, login, version, "上传反馈", mutate)


def set_mapping(task_id, version, mapping: FeedbackMapping, login):
    def mutate(task):
        values = mapping.model_dump()
        indexes = [values["id_col"], *values["open_text_cols"]]
        indexes.extend(values[key] for key in ("count_col", "adv_col") if values[key] is not None)
        if len(indexes) != len(set(indexes)) or any(i < 0 or i >= len(task["headers"]) for i in indexes):
            raise HTTPException(422, "身份、回答、次数和 ADV 列不能重叠，且必须在表头范围内")
        if task["mapping"] != values:
            for row in task["feedback"]:
                _invalidate_model(row)
                row["corrections"] = {}
            task["duplicate_decisions"] = {}
            task["ai_confirmation_complete"] = False
        task["mapping"] = values
        return values
    return _change(task_id, login, version, "确认上传列映射", mutate)


def _cell(row, column):
    return str(row["cells"][column]).strip() if column is not None and column < len(row["cells"]) else ""


def _row(task, response_id):
    row = next((item for item in task["feedback"] if item["id"] == response_id), None)
    if not row:
        raise HTTPException(404, "反馈记录不存在")
    return row


def _parse_count(value):
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]+(?:\.0+)?", text):
        return None
    number = int(text.split(".")[0])
    return number if number <= 2147483647 else None


def _refresh_checks(task: dict) -> None:
    mapping = task.get("mapping")
    task["input_issues"] = []
    task["duplicate_groups"] = []
    if not mapping:
        if task["feedback"]:
            task["input_issues"].append({"kind": "mapping", "message": "请确认身份列和完整反馈题目列"})
        return
    snapshot = task.get("roster_snapshot")
    entries = (snapshot or {}).get("entries", [])
    by_player = defaultdict(list)
    for entry in entries:
        by_player[entry["player_id"]].append(entry)
    if not snapshot or task.get("roster_error"):
        task["input_issues"].append({"kind": "roster", "message": task.get("roster_error") or "请读取已配置的飞书名单以核查身份"})
    for row in task["feedback"]:
        edits = row["corrections"]
        row["player_id"] = str(edits.get("player_id", _cell(row, mapping["id_col"]))).strip()
        row["adv"] = str(edits.get("adv", _cell(row, mapping.get("adv_col")))).strip()
        row["excluded"] = False
        row["issues"] = []
        if not row["player_id"]:
            row["issues"].append({"kind": "identity", "message": "playerid 为空，请补填原始身份"})
        elif str(mapping["id_col"]) in row.get("cell_issues", {}) and "player_id" not in edits:
            row["issues"].append({"kind": "identity", "message": row["cell_issues"][str(mapping["id_col"])]})
        if row["adv"] and str(mapping.get("adv_col")) in row.get("cell_issues", {}) and "adv" not in edits:
            row["issues"].append({"kind": "adv", "message": "ADV：" + row["cell_issues"][str(mapping["adv_col"])]})
        candidates = by_player.get(row["player_id"], [])
        chosen = [item for item in candidates if item["key"] == edits.get("candidate_key")]
        row["identity_candidates"] = deepcopy(candidates)
        row["identity"] = deepcopy(chosen[0] if len(chosen) == 1 else candidates[0] if len(candidates) == 1 else None)
        if snapshot and row["player_id"] and not row["identity"]:
            row["issues"].append({"kind": "identity", "message": "身份匹配多个名单条目，请明确选择" if candidates else "名单中找不到 playerid，请修正身份或维护名单后重新读取"})
        if row["identity"] and row["identity"].get("identity_error"):
            row["issues"].append({"kind": "identity", "message": row["identity"]["identity_error"]})
        raw_count = edits.get("count", _cell(row, mapping.get("count_col")))
        count = _parse_count(raw_count)
        row["count"] = count
        if not task["rules"]["requires_count"]:
            row["gate"] = {"status": "not_applicable", "reason": "本任务不要求数据校验"}
        elif count is None:
            row["gate"] = {"status": "missing", "reason": "实际次数为空或无法解析，请补填；不会按 0 处理"}
            row["issues"].append({"kind": "count", "message": row["gate"]["reason"]})
        elif not task["rules"].get("min_count"):
            row["gate"] = {"status": "missing", "reason": "最低次数尚未配置"}
        elif count < task["rules"]["min_count"]:
            row["gate"] = {"status": "fail", "reason": f"实际完成 {count} 场，要求至少 {task['rules']['min_count']} 场；按任务规则判为无效，未进行 AI 判断。"}
        else:
            row["gate"] = {"status": "pass", "reason": f"实际完成 {count} 场，达到至少 {task['rules']['min_count']} 场要求"}
    if not task["rules"]["allow_repeat"]:
        parent = {row["id"]: row["id"] for row in task["feedback"]}
        def find(item):
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item
        seen = {}
        for row in task["feedback"]:
            identity = row.get("identity") or {}
            for kind, value in (("playerid", row["player_id"]), ("ori", identity.get("ori", ""))):
                if not value:
                    continue
                key = (kind, value)
                if key in seen:
                    parent[find(row["id"])] = find(seen[key])
                seen[key] = row["id"]
        groups = defaultdict(list)
        for row in task["feedback"]:
            groups[find(row["id"])].append(row)
        for members in groups.values():
            if len(members) < 2:
                continue
            ids = sorted(item["id"] for item in members)
            key = fingerprint(ids)[:24]
            retained = task["duplicate_decisions"].get(key)
            resolved = retained in ids
            task["duplicate_groups"].append({"id": key, "response_ids": ids, "keep_response_id": retained if resolved else None})
            for row in members:
                row["excluded"] = resolved and row["id"] != retained
            if not resolved:
                task["input_issues"].append({"kind": "duplicate", "group_id": key, "message": "身份重复，请查看完整反馈并选择保留一份"})
    for row in task["feedback"]:
        if not row["excluded"]:
            task["input_issues"].extend({**issue, "response_id": row["id"], "source_row": row["source_row"]} for issue in row["issues"])


async def refresh_roster(task_id, version, login):
    from app.services.roster_sources import read_roster_snapshot
    task = load_task(task_id, login, write=True)
    _ensure_version(task, version)
    try:
        snapshot = await read_roster_snapshot(login)
    except Exception as exc:
        def fail(current):
            require_record(current, login, write=True)
            _ensure_version(current, version)
            current["roster_error"] = str(getattr(exc, "detail", exc))
            _refresh_checks(current)
            return task_view(current, login)
        result = store.mutate("tasks", task_id, fail)
        return {"ok": False, "task": result}
    def mutate(current):
        current["roster_snapshot"] = snapshot
        current["roster_error"] = ""
        return {"snapshot_id": snapshot["id"], "read_at": snapshot["read_at"]}
    return {"ok": True, "task": _change(task_id, login, version, "刷新名单并核查身份", mutate)}


def correct_feedback(task_id, response_id, request, login):
    def mutate(task):
        row = _row(task, response_id)
        updates = request.model_dump(exclude_unset=True, exclude={"expected_version", "note"})
        updates = {key: value for key, value in updates.items() if value is not None or key == "count"}
        previous = deepcopy(row["corrections"])
        row["corrections"].update(updates)
        task["duplicate_decisions"] = {}
        if "player_id" in updates and "candidate_key" not in updates and previous.get("player_id") != updates["player_id"]:
            row["corrections"].pop("candidate_key", None)
        row["reviews"].append({"kind": "input", "at": now(), **owner_identity(login),
                               "before": previous, "after": deepcopy(row["corrections"]), "note": request.note.strip()})
        return {"response_id": response_id, "changes": updates, "note": request.note.strip()}
    return _change(task_id, login, request.expected_version, "修正反馈输入", mutate)


def resolve_duplicate(task_id, request, login):
    def mutate(task):
        _refresh_checks(task)
        group = next((item for item in task["duplicate_groups"] if item["id"] == request.group_id), None)
        if not group or request.keep_response_id not in group["response_ids"]:
            raise HTTPException(409, "重复组已变化，请重新核对")
        task["duplicate_decisions"][request.group_id] = request.keep_response_id
        return {"group_id": request.group_id, "keep_response_id": request.keep_response_id}
    return _change(task_id, login, request.expected_version, "处理重复反馈", mutate)


def effective_result(task: dict, row: dict) -> dict:
    if row.get("excluded"):
        return {"complete": True, "source": "excluded", "label": None, "reason": "重复记录已排除，保留原文供追溯", "base": 0, "excellent": 0}
    gate = row.get("gate") or {}
    label, reason, source = None, "", "pending"
    if gate.get("status") == "fail":
        label, reason, source = "无效反馈", gate["reason"], "rule"
    elif row.get("confirmed_ai"):
        label, reason, source = "无效反馈", "操作者已确认 AI 作答，按无效反馈处理", "human_ai"
    elif not row.get("issues") and gate.get("status") in ("pass", "not_applicable"):
        if row.get("quality_complete") and (not task["rules"]["ai_detect"] or row.get("ai_complete")):
            judgment = row.get("manual_quality") or row.get("quality") or {}
            label = judgment.get("label") or judgment.get("overall")
            reason = judgment.get("note") or judgment.get("overall_reason") or ""
            source = "human" if row.get("manual_quality") else "ai"
    if label not in LABELS:
        return {"complete": False, "source": "pending", "label": None,
                "reason": row.get("model_error") or "输入或质量结果尚未完整，可处理异常或重试缺项", "base": None, "excellent": None}
    base = (task["rules"].get("base_reward") or 0) if label != "无效反馈" else 0
    extra = (task["rules"].get("excellent_reward") or 0) if label == "优秀反馈" else 0
    return {"complete": True, "source": source, "label": label, "reason": reason, "base": base, "excellent": extra}


def override_quality(task_id, response_id, request, login):
    def mutate(task):
        row = _row(task, response_id)
        if row.get("excluded") or row.get("confirmed_ai") or (row.get("gate") or {}).get("status") == "fail":
            raise HTTPException(409, "请先处理排除、AI 作答确认或数据门槛；不能用质量标签绕过规则")
        if not row.get("quality_complete"):
            raise HTTPException(409, "质量结果尚未完整，请先补齐")
        before = deepcopy(row.get("manual_quality"))
        if request.restore:
            row.pop("manual_quality", None)
        else:
            if request.label not in LABELS:
                raise HTTPException(422, "请选择整体质量")
            row["manual_quality"] = {"label": request.label, "note": request.note.strip(), "at": now(), **owner_identity(login)}
        row["reviews"].append({"kind": "quality", "at": now(), **owner_identity(login),
                               "before": before, "after": deepcopy(row.get("manual_quality")), "note": request.note.strip()})
        return {"response_id": response_id, "before": before, "after": row.get("manual_quality"), "note": request.note.strip()}
    return _change(task_id, login, request.expected_version, "人工调整整体质量", mutate)


def confirm_ai(task_id, request, login):
    def mutate(task):
        eligible = [row for row in task["feedback"] if not row.get("excluded") and row.get("gate", {}).get("status") in ("pass", "not_applicable")]
        ids = {row["id"] for row in eligible}
        selected = set(request.response_ids)
        if not selected <= ids or any(not row.get("ai_complete") for row in eligible):
            raise HTTPException(409, "AI 作答识别尚有缺项或选择已失效，请先补齐")
        for row in eligible:
            before = bool(row.get("confirmed_ai"))
            row["confirmed_ai"] = row["id"] in selected
            if before != row["confirmed_ai"]:
                row["reviews"].append({"kind": "ai_confirmation", "at": now(), **owner_identity(login),
                                       "before": before, "after": row["confirmed_ai"]})
        task["ai_confirmation_complete"] = True
        return {"confirmed_response_ids": sorted(selected)}
    return _change(task_id, login, request.expected_version, "人工确认 AI 作答", mutate)


def set_player_adv(task_id, request, login):
    def mutate(task):
        if request.player_id not in {row.get("player_id") for row in task["feedback"]}:
            raise HTTPException(404, "任务中不存在此玩家")
        task["player_adv"][request.player_id] = {"value": request.adv.strip(), "note": request.note.strip(),
                                                "at": now(), **owner_identity(login)}
        return {"player_id": request.player_id, "adv": request.adv.strip(), "note": request.note.strip()}
    return _change(task_id, login, request.expected_version, "确认玩家 ADV", mutate)


def readiness_issues(task: dict, *, settlement=False) -> list[dict]:
    rules = task["rules"]
    issues = deepcopy(task.get("input_issues") or [])
    if not rules["name"].strip() or not rules["payout_date"] or rules["base_reward"] is None or rules["excellent_reward"] is None:
        issues.append({"kind": "rules", "message": "请确认任务名称、发奖日期和两项奖励金额（允许 0）"})
    if rules["requires_count"] and not rules.get("min_count"):
        issues.append({"kind": "rules", "message": "请配置实际次数门槛"})
    if not task["feedback"] or not task["mapping"]:
        issues.append({"kind": "upload", "message": "请上传反馈并确认完整列映射"})
    if settlement:
        retained = [row for row in task["feedback"] if not row.get("excluded")]
        if any(not effective_result(task, row)["complete"] for row in retained):
            issues.append({"kind": "incomplete", "message": "仍有未完成的反馈，不能确认结算"})
        if rules["ai_detect"] and any(row.get("gate", {}).get("status") in ("pass", "not_applicable") for row in retained) and not task["ai_confirmation_complete"]:
            issues.append({"kind": "ai_review", "message": "请核对 AI 作答识别结果，可确认没有 AI 作答"})
        from app.services.task_records import aggregate_players
        for player in aggregate_players(task):
            for issue in player["issues"]:
                issues.append({"kind": "player", "player_id": player["player_id"], "message": issue})
    return issues


def confirm_task(task_id, version, login):
    load_task(task_id, login, write=True)
    require_permission(login, "annotate", write=True)
    def apply(task):
        require_record(task, login, write=True)
        _ensure_version(task, version)
        _refresh_checks(task)
        issues = readiness_issues(task, settlement=True)
        if issues:
            raise HTTPException(409, {"message": "任务尚不能确认", "issues": issues})
        task["confirmed_version"] = task["version"]
        task["confirmed_at"] = now()
        _event(task, login, "确认任务结果")
        return task_view(task, login)
    return store.mutate("tasks", task_id, apply)


def task_view(task: dict, login, *, summary=False) -> dict:
    from app.services.task_records import aggregate_players
    result = deepcopy(task)
    result["can_edit"] = task["owner_key"] == owner_identity(login)["owner_key"]
    from app.services.auth import _get_user_permission_levels
    result["can_edit"] = result["can_edit"] and _get_user_permission_levels(login).get("annotate") == "use"
    for saved_run in [result.get("run"), *result.get("runs", [])]:
        if saved_run:
            saved_run.pop("owner_login", None)
    retained = [row for row in task["feedback"] if not row.get("excluded")]
    outcomes = [effective_result(task, row) for row in retained]
    result["summary"] = {
        "feedback_count": len(retained), "player_count": len({row.get("player_id") for row in retained}),
        "complete_count": sum(item["complete"] for item in outcomes),
        "pending_count": sum(not item["complete"] for item in outcomes),
        "base": sum(item["base"] or 0 for item in outcomes),
        "excellent": sum(item["excellent"] or 0 for item in outcomes),
    }
    run = task.get("run") or {}
    if run.get("status") in ACTIVE_RUNS:
        status = run["status"]
    elif not task["feedback"]:
        status = "draft"
    elif readiness_issues(task):
        status = "input_issues"
    elif result["summary"]["pending_count"]:
        status = "incomplete" if task.get("runs") or run else "ready"
    elif task.get("confirmed_version") == task["version"]:
        status = "confirmed"
    else:
        status = "awaiting_confirmation"
    result["status"] = status
    result["confirmation_issues"] = readiness_issues(task, settlement=True)
    if result.get("roster_snapshot"):
        result["roster_snapshot"].pop("entries", None)
    if summary:
        for key in ("feedback", "feedback_versions", "events", "uploads", "headers", "duplicate_groups", "input_issues", "confirmation_issues", "roster_snapshot", "runs"):
            result.pop(key, None)
        return result
    result.pop("feedback_versions", None)
    for row in result["feedback"]:
        row["outcome"] = effective_result(task, row)
        row["answers"] = [{"column": index, "question": task["headers"][index], "answer": _cell(row, index)}
                          for index in (task.get("mapping") or {}).get("open_text_cols", [])]
    result["players"] = aggregate_players(task)
    return result


def list_tasks(login, *, feature="annotate") -> list[dict]:
    require_permission(login, feature)
    result = [task_view(task, login, summary=True) for task in store.list_records("tasks") if can_read(task, login)]
    result.sort(key=lambda item: item["created_at"], reverse=True)
    queue = sorted((item for item in store.list_records("tasks") if (item.get("run") or {}).get("status") == "queued"),
                   key=lambda item: (item["run"].get("schedule_at", item["run"]["queued_at"]), item["run"]["id"]))
    positions = {item["id"]: index + 1 for index, item in enumerate(queue)}
    for item in result:
        item["queue_position"] = positions.get(item["id"])
    return result
