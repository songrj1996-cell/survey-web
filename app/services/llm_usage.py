"""个人 LLM 任务用量记录、聚合与查询。"""
from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.security import _visible_to_owner
from app.storage.llm_usage import load_llm_usage, mutate_llm_usage
from app.storage.sessions import get_session


_ALLOWED_PERIODS = {"7d", "30d", "all"}
_ALLOWED_CATEGORIES = {"survey", "comment", "interview", "annotate", "other"}
_ALLOWED_STATUSES = {"running", "completed", "failed", "cancelled"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _owner_id(login: dict | None) -> str:
    login = login or {}
    open_id = str(login.get("open_id") or "").strip()
    email = str(login.get("email") or "").strip().lower()
    identity = f"open_id:{open_id}" if open_id else (f"email:{email}" if email else "")
    if not identity:
        return ""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _clean_text(value: Any, limit: int = 180) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _token_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _append_unique(values: list[str], value: Any) -> None:
    cleaned = _clean_text(value, 100)
    if cleaned and cleaned not in values:
        values.append(cleaned)


def _session_title(login: dict | None, reference_id: str) -> str:
    if not reference_id:
        return ""
    try:
        session = get_session(reference_id)
    except Exception:
        return ""
    if not _visible_to_owner(session, login):
        return ""
    return _clean_text(
        session.get("comment_post_title")
        or session.get("title")
        or session.get("filename")
        or session.get("interview_research_focus")
    )


class LLMUsageRecorder:
    """聚合同一用户任务中的多轮、并发与重试 HTTP 尝试。"""

    def __init__(
        self,
        owner_id: str,
        *,
        category: str,
        action: str,
        title: str = "",
        reference_id: str = "",
        history_id: str = "",
    ) -> None:
        now = _iso_now()
        self.owner_id = owner_id
        self.record_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._known_calls: set[str] = set()
        self._closed_calls: set[str] = set()
        self._finished = False
        self._record = {
            "id": self.record_id,
            "category": category if category in _ALLOWED_CATEGORIES else "other",
            "action": _clean_text(action) or "AI 任务",
            "title": _clean_text(title),
            "reference_id": _clean_text(reference_id, 100),
            "history_id": _clean_text(history_id, 100),
            "started_at": now,
            "updated_at": now,
            "completed_at": "",
            "status": "running",
            "models_used": [],
            "fallback_models_used": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "call_count": 0,
            "usage_reported_call_count": 0,
            "usage_missing_call_count": 0,
        }
        self._insert()

    def _insert(self) -> None:
        snapshot = dict(self._record)

        def _mutate(owners: dict[str, list[dict]]) -> None:
            owners.setdefault(self.owner_id, []).append(snapshot)

        mutate_llm_usage(_mutate)

    def _persist(self) -> None:
        snapshot = dict(self._record)
        snapshot["models_used"] = list(self._record["models_used"])
        snapshot["fallback_models_used"] = list(
            self._record["fallback_models_used"]
        )

        def _mutate(owners: dict[str, list[dict]]) -> None:
            records = owners.setdefault(self.owner_id, [])
            for index, item in enumerate(records):
                if item.get("id") == self.record_id:
                    records[index] = snapshot
                    return
            records.append(snapshot)

        mutate_llm_usage(_mutate)

    async def on_attempt_event(self, event: dict[str, Any]) -> None:
        call_id = _clean_text(event.get("call_id"), 100) or uuid.uuid4().hex
        status = str(event.get("status") or "").strip().lower()
        with self._lock:
            if self._finished:
                return
            if call_id not in self._known_calls:
                self._known_calls.add(call_id)
                self._record["call_count"] += 1
            _append_unique(self._record["models_used"], event.get("model"))
            _append_unique(
                self._record["models_used"], event.get("response_model")
            )
            if event.get("fallback"):
                _append_unique(
                    self._record["fallback_models_used"],
                    event.get("response_model") or event.get("model"),
                )
            if status not in {"completed", "failed"}:
                self._record["updated_at"] = _iso_now()
                self._persist()
                return
            if call_id in self._closed_calls:
                return
            self._closed_calls.add(call_id)
            usage = event.get("usage")
            if isinstance(usage, dict):
                self._record["input_tokens"] += _token_value(
                    usage.get("input_tokens")
                )
                self._record["output_tokens"] += _token_value(
                    usage.get("output_tokens")
                )
                self._record["total_tokens"] += _token_value(
                    usage.get("total_tokens")
                )
                self._record["usage_reported_call_count"] += 1
            if not isinstance(usage, dict) or not event.get("usage_complete"):
                self._record["usage_missing_call_count"] += 1
            self._record["updated_at"] = _iso_now()
            self._persist()

    def finish(self, status: str) -> None:
        resolved = status if status in _ALLOWED_STATUSES else "failed"
        with self._lock:
            if self._finished:
                return
            self._finished = True
            now = _iso_now()
            self._record["status"] = resolved
            self._record["updated_at"] = now
            self._record["completed_at"] = now
            self._persist()


def start_llm_usage_task(
    login: dict | None,
    *,
    category: str,
    action: str,
    reference_id: str = "",
    title: str = "",
    history_id: str = "",
) -> LLMUsageRecorder | None:
    owner_id = _owner_id(login)
    if not owner_id:
        return None
    resolved_title = _clean_text(title) or _session_title(login, reference_id)
    return LLMUsageRecorder(
        owner_id,
        category=category,
        action=action,
        title=resolved_title,
        reference_id=reference_id,
        history_id=history_id,
    )


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _public_record(record: dict) -> dict:
    return {
        "id": _clean_text(record.get("id"), 100),
        "category": _clean_text(record.get("category"), 40) or "other",
        "action": _clean_text(record.get("action")) or "AI 任务",
        "title": _clean_text(record.get("title")),
        "reference_id": _clean_text(record.get("reference_id"), 100),
        "history_id": _clean_text(record.get("history_id"), 100),
        "started_at": str(record.get("started_at") or ""),
        "updated_at": str(record.get("updated_at") or ""),
        "completed_at": str(record.get("completed_at") or ""),
        "status": (
            record.get("status")
            if record.get("status") in _ALLOWED_STATUSES
            else "failed"
        ),
        "models_used": [
            _clean_text(model, 100) for model in record.get("models_used") or []
            if _clean_text(model, 100)
        ],
        "fallback_models_used": [
            _clean_text(model, 100)
            for model in record.get("fallback_models_used") or []
            if _clean_text(model, 100)
        ],
        "input_tokens": _token_value(record.get("input_tokens")),
        "output_tokens": _token_value(record.get("output_tokens")),
        "total_tokens": _token_value(record.get("total_tokens")),
        "call_count": _token_value(record.get("call_count")),
        "usage_reported_call_count": _token_value(
            record.get("usage_reported_call_count")
        ),
        "usage_missing_call_count": _token_value(
            record.get("usage_missing_call_count")
        ),
    }


def get_user_llm_usage(
    login: dict | None,
    *,
    period: str = "30d",
    category: str = "",
    status: str = "",
    offset: int = 0,
    limit: int = 20,
) -> dict:
    owner_id = _owner_id(login)
    if not owner_id:
        raise ValueError("请先登录飞书")
    selected_period = period if period in _ALLOWED_PERIODS else "30d"
    selected_category = category if category in _ALLOWED_CATEGORIES else ""
    selected_status = status if status in _ALLOWED_STATUSES else ""
    safe_offset = max(0, int(offset or 0))
    safe_limit = min(100, max(1, int(limit or 20)))

    records = [_public_record(item) for item in load_llm_usage().get(owner_id, [])]
    cutoff = None
    if selected_period != "all":
        days = 7 if selected_period == "7d" else 30
        cutoff = _utc_now() - timedelta(days=days)
    period_records = [
        item for item in records
        if cutoff is None
        or ((_parse_timestamp(item.get("started_at")) or datetime.min.replace(
            tzinfo=timezone.utc
        )) >= cutoff)
    ]
    period_records.sort(
        key=lambda item: _parse_timestamp(item.get("started_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    summary = {
        "task_count": len(period_records),
        "input_tokens": sum(item["input_tokens"] for item in period_records),
        "output_tokens": sum(item["output_tokens"] for item in period_records),
        "total_tokens": sum(item["total_tokens"] for item in period_records),
        "call_count": sum(item["call_count"] for item in period_records),
        "usage_missing_call_count": sum(
            item["usage_missing_call_count"] for item in period_records
        ),
    }
    filtered = [
        item for item in period_records
        if (not selected_category or item["category"] == selected_category)
        and (not selected_status or item["status"] == selected_status)
    ]
    page = filtered[safe_offset:safe_offset + safe_limit]
    next_offset = safe_offset + len(page)
    return {
        "period": selected_period,
        "category": selected_category,
        "status": selected_status,
        "summary": summary,
        "records": page,
        "total_records": len(filtered),
        "next_offset": next_offset if next_offset < len(filtered) else None,
        "generated_at": _iso_now(),
    }
