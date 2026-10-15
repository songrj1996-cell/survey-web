"""Fresh roster settlement, immutable batches and atomic source claims."""
from collections import defaultdict
from copy import deepcopy
import uuid

from fastapi import HTTPException

from app.schemas.reward_roster import RewardAnalyzeRequest
from app.services import group_tasks as tasks
from app.services.feature_access import can_read, owner_identity, require_permission, require_record
from app.services.reward_roster import _build_conflicts, _build_result, _parse_account, analyze_reward_session, get_reward_session, delete_import
from app.services.auth import _get_user_permission_levels
from app.core import config
from app.services.roster_sources import read_roster_snapshot
from app.services.task_records import aggregate_players
from app.storage import group_workflow as store


def _ledger():
    return store.read("ledger", "payouts") or {"batches": {}, "claims": {}, "idempotency": {}}


def choices(login):
    require_permission(login, "reward")
    ledger = _ledger()
    available = []
    for task in store.list_records("tasks"):
        if can_read(task, login):
            item = tasks.task_view(task, login, summary=True)
            item["consumed"] = ledger["claims"].get("task:" + task["id"])
            item["selectable"] = (task.get("confirmed_version") == task["version"] and not tasks.readiness_issues(task, settlement=True)
                                  and task["owner_key"] == owner_identity(login)["owner_key"] and not item["consumed"])
            available.append(item)
    others = []
    for record in store.list_records("other_rewards"):
        if can_read(record, login):
            item = deepcopy(record)
            item["consumed"] = ledger["claims"].get("other:" + item["id"])
            item["selectable"] = item["owner_key"] == owner_identity(login)["owner_key"] and not item["consumed"]
            others.append(item)
    may_write = not config.FEISHU_LOGIN_REQUIRED or _get_user_permission_levels(login).get("reward") == "use"
    imports = [{**{key: record.get(key) for key in ("id", "filename", "owner_name", "analysis_request", "created_at")},
                "can_delete": may_write, "selectable": may_write and record["owner_key"] == owner_identity(login)["owner_key"]}
               for record in store.list_records("imports") if not record.get("deleted_at") and can_read(record, login)]
    groups = [{"id": source["id"], "name": source["name"]} for source in store.list_records("sources") if source["enabled"]]
    return {"tasks": available, "other_rewards": others, "imports": imports, "groups": groups,
            "batches": [{key: batch.get(key) for key in ("id", "created_at", "owner_name", "dates", "total_diamonds", "unique_accounts")}
                        for batch in ledger["batches"].values() if can_read(batch, login)]}


async def create_other_reward(request, login):
    require_permission(login, "reward", write=True)
    if not request.reason.strip() or not request.player_id.strip():
        raise HTTPException(422, "请填写 playerid 和奖励原因")
    snapshot = await read_roster_snapshot(login)
    candidates = [row for row in snapshot["entries"] if row["source_id"] == request.source_id and row["player_id"] == request.player_id.strip()]
    chosen = [row for row in candidates if row["key"] == request.candidate_key] if request.candidate_key else candidates
    if len(chosen) != 1 or chosen[0]["identity_error"]:
        raise HTTPException(409, {"message": "其他奖励身份不唯一或账号错误，请修正后录入", "candidates": candidates})
    record = {**request.model_dump(mode="json"), "id": uuid.uuid4().hex, **owner_identity(login),
              "created_at": tasks.now(), "identity": chosen[0], "snapshot_id": snapshot["id"]}
    return store.create("other_rewards", record["id"], record)


def _collect_sources(selection, login):
    dates = selection["dates"]
    if len(dates) != len(set(dates)):
        raise HTTPException(422, "发奖日期不能重复")
    rows, versions, claims = [], {}, []
    for task_id, version in selection["task_versions"].items():
        task = tasks.load_task(task_id, login, write=True, feature="reward")
        if task["version"] != version or task.get("confirmed_version") != version or tasks.readiness_issues(task, settlement=True):
            raise HTTPException(409, "选定任务已更新或尚未确认，请重新选择当前完整版本")
        if task["rules"]["payout_date"] not in dates:
            raise HTTPException(422, "选定任务的发奖日期不在本次明确选择范围")
        versions[task_id] = {"version": version, "rules": task["rules"], "confirmed_at": task["confirmed_at"]}
        claims.append("task:" + task_id)
        for index, player in enumerate(aggregate_players(task), 1):
            rows.append({"row_id": f"task-{task_id}-{tasks.fingerprint(player['player_id'])[:16]}",
                         "source_kind": "task", "source_key": "task:" + task_id, "task_id": task_id,
                         "sheet": player["group_name"], "source_id": player["source_id"], "row_number": index,
                         "date": task["rules"]["payout_date"], "task": task["rules"]["name"], "player_id": player["player_id"],
                         "previous_identity": player["identity"], "response_ids": player["response_ids"],
                         "normal_reward": player["base"], "excellent_reward": player["excellent"], "other_reward": 0,
                         "total_reward": player["total"], "version": version})
    for identifier in dict.fromkeys(selection["other_reward_ids"]):
        record = require_record(store.read("other_rewards", identifier), login, write=True)
        if record["payout_date"] not in dates:
            raise HTTPException(422, "其他奖励的日期不在选择范围")
        claims.append("other:" + identifier)
        rows.append({"row_id": "other-" + identifier, "source_kind": "other", "source_key": "other:" + identifier,
                     "reward_event_key": "other:" + identifier,
                     "sheet": record["identity"]["group_name"], "source_id": record["source_id"], "row_number": 1,
                     "date": record["payout_date"], "task": "其他奖励：" + record["reason"], "player_id": record["player_id"],
                     "previous_identity": record["identity"], "normal_reward": 0, "excellent_reward": 0,
                     "other_reward": record["amount"], "total_reward": record["amount"], "other_record": record})
    for identifier in dict.fromkeys(selection["imported_session_ids"]):
        session = require_record(get_reward_session(identifier), login, write=True)
        if not session.get("analysis_request"):
            raise HTTPException(409, "请先完成导入表格的字段映射和日期选择")
        # Re-parse all selected dates regardless of stale spreadsheet status/ori.
        available_dates = {day for sheet in session["sheets"] for day in sheet.get("date_counts", {})}
        imported_dates = [day for day in dates if day in available_dates]
        if not imported_dates:
            raise HTTPException(422, "选定导入文件没有本次选择日期的记录")
        imported_request = RewardAnalyzeRequest(**{**session["analysis_request"], "reward_dates": imported_dates})
        analyze_reward_session(identifier, imported_request, latest_roster=True)
        analysis = session["analysis"]
        if analysis["invalid_rows"]:
            raise HTTPException(409, {"message": "导入文件仍有异常，请修正后重新上传", "issues": analysis["invalid_rows"]})
        content_hash = tasks.fingerprint(session["content"].hex())
        for imported in analysis["eligible_rows"]:
            key = "import:" + tasks.fingerprint([content_hash, imported["sheet"], imported["row_number"]])
            claims.append(key)
            rows.append({**deepcopy(imported), "row_id": key, "source_kind": "import", "source_key": key,
                         "source_id": "", "import_id": identifier, "previous_account": imported["account"],
                         "source_filename": session["filename"], "import_sheet": imported["sheet"]})
    if not rows:
        raise HTTPException(422, "请明确选择至少一项有记录的奖励来源")
    ledger = _ledger()
    already = [key for key in claims if key in ledger["claims"]]
    if already:
        raise HTTPException(409, "选定来源已纳入历史批次；后续版本不能重新全额生成，请查看历史批次及版本差异")
    return rows, versions, sorted(set(claims))


def _reconcile(rows, snapshot, identity_choices):
    by_player = defaultdict(list)
    for entry in snapshot["entries"]:
        by_player[entry["player_id"]].append(entry)
    eligible, excluded, issues, changes = [], [], [], []
    for original in rows:
        row = deepcopy(original)
        candidates = [entry for entry in by_player[row["player_id"]] if not row.get("source_id") or entry["source_id"] == row["source_id"]]
        # Retired memberships cannot compete with an active identity. Unknown states
        # remain candidates so that missing configuration never silently pays a player.
        current_candidates = [entry for entry in candidates if entry.get("eligible") is not False]
        if candidates and not current_candidates:
            row.update(exclusion_reason="最新名单中的匹配身份均不在群内", matching_identities=candidates)
            excluded.append(row)
            continue
        candidates = current_candidates
        selected = identity_choices.get(row["row_id"])
        chosen = [entry for entry in candidates if entry["key"] == selected] if selected and len(candidates) > 1 else candidates
        if len(chosen) != 1 or chosen[0].get("identity_error") or chosen[0].get("eligible") is None:
            issues.append({"row_id": row["row_id"], "player_id": row["player_id"], "task": row["task"],
                           "message": "最新名单中无此身份" if not candidates else "身份有多个匹配，请选择" if len(chosen) != 1 else chosen[0].get("identity_error") or chosen[0].get("status_error"),
                           "candidates": candidates, "selected_key": selected or ""})
            continue
        identity = chosen[0]
        uid, zoneid, ori = _parse_account(identity["ori"])
        row.update(identity=identity, account=ori, uid=uid, zoneid=zoneid, sheet=identity["group_name"])
        before = row.get("previous_identity") or {"ori": row.get("previous_account", ""), "status": row.get("previous_status", "")}
        def comparable_status(value):
            if value in ("群内人员", "群内成员"):
                return "in_group"
            if value in ("已退群", "退群", "不在群内"):
                return "out_of_group"
            return value
        changed_fields = []
        if before.get("ori") != ori:
            changed_fields.append("account")
        if before.get("status") and comparable_status(before["status"]) != comparable_status(identity["status"]):
            changed_fields.append("status")
        if changed_fields:
            changes.append({"row_id": row["row_id"], "player_id": row["player_id"], "before": before, "after": identity,
                            "fields": changed_fields, "task": row["task"], "date": row["date"], "source_kind": row["source_kind"],
                            "filename": row.get("source_filename", ""), "sheet": row.get("import_sheet", original["sheet"]),
                            "row_number": row["row_number"]})
        if not identity["eligible"] or row["total_reward"] == 0:
            row["exclusion_reason"] = "当前不在群内" if not identity["eligible"] else "计算金额为 0"
            excluded.append(row)
        else:
            eligible.append(row)
    return eligible, excluded, issues, changes


def _resolved(preview):
    rows, excluded, issues, changes = _reconcile(preview["source_rows"], preview["roster_snapshot"], preview.get("identity_choices", {}))
    conflicts, conflict_ids = _build_conflicts(rows)
    selections = preview.get("selections", {})
    selected_rows = [row for row in rows if row["row_id"] not in conflict_ids]
    unresolved = []
    for conflict in conflicts:
        identifier = conflict["conflict_id"]
        if identifier not in selections:
            unresolved.append(identifier)
            continue
        valid_rows = {row["row_id"]: row for row in conflict["rows"]}
        chosen = selections[identifier]
        if len(chosen) != len(set(chosen)) or any(key not in valid_rows for key in chosen):
            raise HTTPException(422, "冲突处理包含无效来源，请重新核对")
        retained = [valid_rows[key] for key in chosen]
        keys = [(row["date"], row.get("reward_event_key") or row["task"]) for row in retained]
        if len(keys) != len(set(keys)):
            raise HTTPException(422, "同一账号的同日同一任务只能保留一笔；合法重复反馈已在任务内汇总")
        selected_rows.extend(retained)
    result = _build_result({"reward_dates": preview["selection"]["dates"], "selected_sheets": sorted({row["sheet"] for row in rows})}, selected_rows)
    return {"eligible_rows": rows, "excluded_rows": excluded, "identity_issues": issues, "roster_changes": changes,
            "conflicts": conflicts, "unresolved_conflicts": unresolved, "selected_rows": selected_rows,
            "result": result, "ready": not issues and not unresolved and bool(selected_rows)}


def preview_view(preview):
    result = deepcopy(preview)
    result.update(_resolved(preview))
    result["roster_snapshot"].pop("entries", None)
    return result


async def create_preview(selection, login):
    require_permission(login, "reward", write=True)
    payload = selection.model_dump(mode="json")
    rows, versions, claims = _collect_sources(payload, login)
    snapshot = await read_roster_snapshot(login)
    preview = {"id": uuid.uuid4().hex, "revision": 1, "created_at": tasks.now(), **owner_identity(login),
               "selection": payload, "source_rows": rows, "task_versions": versions, "source_claims": claims,
               "roster_snapshot": snapshot, "identity_choices": {}, "selections": {}}
    store.create("previews", preview["id"], preview)
    return preview_view(preview)


def resolve_preview(identifier, request, login):
    require_permission(login, "reward", write=True)
    require_record(store.read("previews", identifier), login, write=True)
    def update(preview):
        require_record(preview, login, write=True)
        if preview["revision"] != request.expected_revision:
            raise HTTPException(409, "预览已更新，请重新核对")
        if not set(request.identity_choices) <= {row["row_id"] for row in preview["source_rows"]}:
            raise HTTPException(422, "身份选择包含未知来源")
        preview.update(identity_choices=request.identity_choices, selections=request.selections)
        preview["revision"] += 1
        return preview_view(preview)
    return store.mutate("previews", identifier, update)


def get_preview(identifier, login):
    require_permission(login, "reward")
    return preview_view(require_record(store.read("previews", identifier), login))


async def generate_batch(identifier, request, login):
    require_permission(login, "reward", write=True)
    owner = owner_identity(login)
    idempotency = tasks.fingerprint([owner["owner_key"], request.idempotency_key])
    previous = _ledger()["idempotency"].get(idempotency)
    if previous:
        batch = require_record(_ledger()["batches"][previous], login, write=True)
        if batch["preview_id"] != identifier:
            raise HTTPException(409, "此生成标识已用于另一份预览")
        return deepcopy(batch)
    preview = require_record(store.read("previews", identifier), login, write=True)
    if preview["revision"] != request.expected_revision:
        raise HTTPException(409, "预览版本已变化，请重新核对")
    # Never replace a valid preview on failure. A new successful read is always required.
    snapshot = await read_roster_snapshot(login)
    if snapshot["fingerprint"] != preview["roster_snapshot"]["fingerprint"]:
        before = _resolved(preview)
        def refresh(current):
            if current["revision"] != request.expected_revision:
                raise HTTPException(409, "预览已被更新，请刷新")
            current["roster_snapshot"] = snapshot
            current["selections"] = {}
            current["identity_choices"] = {}
            current["revision"] += 1
            after = _resolved(current)
            current["generation_difference"] = {"before_total": before["result"]["total_diamonds"], "after_total": after["result"]["total_diamonds"],
                                                "before_accounts": before["result"]["accounts"], "after_accounts": after["result"]["accounts"]}
            return preview_view(current)
        updated = store.mutate("previews", identifier, refresh)
        raise HTTPException(409, {"message": "名单在预览后发生变化，已展示新旧差异；请核对并重新处理必要冲突后生成", "preview": updated})
    def commit(ledger):
        repeated = ledger["idempotency"].get(idempotency)
        if repeated:
            existing = ledger["batches"][repeated]
            if existing["preview_id"] != identifier:
                raise HTTPException(409, "此生成标识已用于另一份预览")
            return existing
        current = require_record(store.read("previews", identifier), login, write=True)
        if current["revision"] != request.expected_revision:
            raise HTTPException(409, "预览已更新，请重新核对")
        # Configuration and all selected task versions are rechecked while holding the commit lock.
        revisions = {s["id"]: s["revision"] for s in store.list_records("sources") if s["enabled"]}
        if revisions != {s["id"]: s["revision"] for s in snapshot["sources"]}:
            raise HTTPException(409, "名单来源配置刚刚变化，请重新预览")
        for import_id in current["selection"]["imported_session_ids"]:
            imported = store.read("imports", import_id)
            if not imported or imported.get("deleted_at"):
                raise HTTPException(409, "此预览使用的表格导入已删除，请重新选择来源")
        for task_id, version in current["selection"]["task_versions"].items():
            task = tasks.load_task(task_id, login, write=True, feature="reward")
            if task["version"] != version or task.get("confirmed_version") != version or tasks.readiness_issues(task, settlement=True):
                raise HTTPException(409, "任务确认状态已变化，请重新预览当前版本")
        resolved = _resolved({**current, "roster_snapshot": snapshot})
        if not resolved["ready"]:
            raise HTTPException(409, "仍有身份、群内状态或奖励冲突待处理，或本次没有可发放奖励")
        claim_keys = list(current["source_claims"])
        for row in resolved["selected_rows"]:
            if row["source_kind"] != "other":
                claim_keys.append("business:" + tasks.fingerprint([row["date"], row["task"], row["account"]]))
                claim_keys.append("player_business:" + tasks.fingerprint([row["date"], row["task"], row["player_id"]]))
        if any(key in ledger["claims"] for key in claim_keys):
            raise HTTPException(409, "来源或同日同任务账号奖励已进入历史批次，不能再次计入；请查看批次及版本差异")
        batch_id = uuid.uuid4().hex
        batch = {"id": batch_id, "preview_id": identifier, "preview_revision": current["revision"], "created_at": tasks.now(), **owner,
                 "dates": current["selection"]["dates"], "selection": current["selection"], "task_versions": current["task_versions"],
                 "source_rows": current["source_rows"], "roster_snapshot": snapshot, "selections": current["selections"],
                 "identity_choices": current["identity_choices"], "selected_rows": resolved["selected_rows"],
                 "excluded_rows": resolved["excluded_rows"], "claims": sorted(set(claim_keys)), **resolved["result"]}
        ledger["batches"][batch_id] = batch
        ledger["idempotency"][idempotency] = batch_id
        for key in claim_keys:
            ledger["claims"][key] = batch_id
        return batch
    return store.mutate("ledger", "payouts", commit, default={"batches": {}, "claims": {}, "idempotency": {}})


def get_batch(identifier, login):
    require_permission(login, "reward")
    batch = deepcopy(require_record(_ledger()["batches"].get(identifier), login))
    differences = []
    for task_id, frozen in batch["task_versions"].items():
        current = store.read("tasks", task_id)
        if not current or not can_read(current, login) or current["version"] == frozen["version"]:
            continue
        players = aggregate_players(current)
        old_rows = [row for row in batch["source_rows"] if row.get("task_id") == task_id]
        old_total = sum(row["total_reward"] for row in old_rows)
        complete = all(player["complete"] for player in players) and bool(players)
        new_total = sum(player["total"] for player in players) if complete else None
        differences.append({"task_name": frozen["rules"]["name"], "old_version": frozen["version"],
                            "current_version": current["version"], "old_total": old_total, "current_total": new_total,
                            "old_player_count": len(old_rows), "current_player_count": len(players),
                            "rule_changes": {key: {"before": value, "after": current["rules"].get(key)}
                                             for key, value in frozen["rules"].items() if value != current["rules"].get(key)}})
    # Derived comparison is returned for inspection; never written into the immutable batch.
    batch["current_source_differences"] = differences
    return batch


def download_batch(identifier, login):
    batch = get_batch(identifier, login)
    return batch["txt_preview"].encode("utf-8"), f"奖励发放-{batch['dates'][0]}-{identifier[:8]}.txt"
