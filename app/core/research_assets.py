"""调研素材契约的无状态哈希、去重和引用完整性工具。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from app.schemas.questionnaire import QuestionnaireSnapshot
from app.schemas.research_assets import (
    AssetContextType,
    AssetDerivative,
    AssetReference,
    AssetRole,
    ResearchAsset,
    ResearchAssetCollection,
    ResearchDocument,
    ResearchSource,
)


class ResearchContractError(ValueError):
    """领域对象之间的引用或唯一性不满足契约。"""


def _json_compatible(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=False)
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """生成与字典插入顺序无关的 UTF-8 JSON 文本。"""
    return json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def structured_sha256(value: Any) -> str:
    return content_sha256(canonical_json(value).encode("utf-8"))


def build_asset_dedupe_key(asset: ResearchAsset) -> str:
    """优先按内容去重；无内容哈希时退化为稳定 Provider 身份。"""
    if asset.content_hash:
        return f"content:{asset.content_hash}"
    if asset.provider_resource_id:
        identity = {
            "provider": asset.provider.value,
            "provider_resource_id": asset.provider_resource_id,
            "provider_version": asset.provider_version,
        }
        return f"provider:{structured_sha256(identity)}"
    fallback = {
        "document_id": asset.document_id,
        "media_type": asset.media_type.value,
        "mime_type": asset.mime_type,
        "filename": asset.filename,
        "size_bytes": asset.size_bytes,
        "source_locator": asset.source_locator,
    }
    return f"metadata:{structured_sha256(fallback)}"


def build_import_idempotency_key(
    source: ResearchSource,
    document: ResearchDocument | None = None,
) -> str:
    """为同一来源版本生成稳定导入键，不包含获取时间或处理状态。"""
    identity: dict[str, Any] = {
        "source_kind": source.source_kind.value,
        "provider": source.provider.value,
        "owner_ref": source.owner_ref,
        "document_type": document.document_type.value if document else None,
        "content_hash": document.content_hash if document else None,
    }
    if not document or not document.content_hash:
        identity.update({
            "source_identity": source.original_url or source.original_name,
            "provider_modified_at": (
                document.provider_modified_at.isoformat()
                if document and document.provider_modified_at
                else None
            ),
            "provider_revision": (
                document.source_locator.provider_revision
                if document and document.source_locator
                else None
            ),
        })
    return f"import:v1:{structured_sha256(identity)}"


def _require_unique(label: str, values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        joined = "、".join(sorted(duplicates))
        raise ResearchContractError(f"{label} 存在重复 ID：{joined}")
    return seen


def validate_research_asset_collection(
    collection: ResearchAssetCollection,
) -> None:
    """校验来源、文档、素材和派生物之间的本地引用。"""
    source_ids = _require_unique(
        "素材来源", (source.source_id for source in collection.sources)
    )
    document_ids = _require_unique(
        "素材文档", (document.document_id for document in collection.documents)
    )
    asset_ids = _require_unique(
        "素材", (asset.asset_id for asset in collection.assets)
    )
    _require_unique(
        "素材引用", (reference.reference_id for reference in collection.references)
    )
    _require_unique(
        "素材派生物",
        (derivative.derivative_id for derivative in collection.derivatives),
    )

    for document in collection.documents:
        if document.source_id not in source_ids:
            raise ResearchContractError(
                f"素材文档 {document.document_id} 指向不存在的来源 "
                f"{document.source_id}"
            )
    for asset in collection.assets:
        if asset.document_id not in document_ids:
            raise ResearchContractError(
                f"素材 {asset.asset_id} 指向不存在的文档 {asset.document_id}"
            )
    for reference in collection.references:
        if reference.asset_id not in asset_ids:
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 指向不存在的素材 "
                f"{reference.asset_id}"
            )
    for derivative in collection.derivatives:
        if derivative.asset_id not in asset_ids:
            raise ResearchContractError(
                f"素材派生物 {derivative.derivative_id} 指向不存在的素材 "
                f"{derivative.asset_id}"
            )


def validate_research_contract(
    snapshot: QuestionnaireSnapshot,
    assets: list[ResearchAsset],
    references: list[AssetReference],
    derivatives: list[AssetDerivative],
) -> None:
    """校验一期契约的跨对象引用，不改变传入对象。"""
    asset_ids = _require_unique("素材", (asset.asset_id for asset in assets))
    reference_ids = _require_unique(
        "素材引用", (reference.reference_id for reference in references)
    )
    _require_unique(
        "素材派生物", (derivative.derivative_id for derivative in derivatives)
    )
    _require_unique(
        "Provider 题目",
        (
            f"{question.provider.value}:{question.provider_question_id}"
            for question in snapshot.provider_questions
        ),
    )
    canonical_question_ids = _require_unique(
        "Canonical 题目",
        (question.question_id for question in snapshot.canonical_questions),
    )
    canonical_questions = {
        question.question_id: question
        for question in snapshot.canonical_questions
    }

    for reference in references:
        if reference.asset_id not in asset_ids:
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 指向不存在的素材 {reference.asset_id}"
            )
        if (
            reference.role == AssetRole.OPTION_STIMULUS
            and not reference.option_key
        ):
            raise ResearchContractError(
                f"选项素材引用 {reference.reference_id} 缺少 option_key"
            )
        is_survey_context = reference.context_type in {
            AssetContextType.SURVEY_QUESTION,
            AssetContextType.SURVEY_OPTION,
        }
        if is_survey_context and reference.context_id not in canonical_question_ids:
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 指向不存在的题目 "
                f"{reference.context_id}"
            )
        if reference.role == AssetRole.OPTION_STIMULUS and is_survey_context:
            question = canonical_questions.get(reference.context_id)
            option_keys = {
                option.option_key for option in question.options
            } if question else set()
            if reference.option_key not in option_keys:
                raise ResearchContractError(
                    f"选项素材引用 {reference.reference_id} 指向不存在的选项 "
                    f"{reference.option_key}"
                )

    for derivative in derivatives:
        if derivative.asset_id not in asset_ids:
            raise ResearchContractError(
                f"素材派生物 {derivative.derivative_id} 指向不存在的素材 "
                f"{derivative.asset_id}"
            )

    used_reference_ids: set[str] = set()
    for question in snapshot.canonical_questions:
        used_reference_ids.update(question.asset_reference_ids)
        for row in question.rows:
            used_reference_ids.update(row.asset_reference_ids)
        for option in question.options:
            used_reference_ids.update(option.asset_reference_ids)
    missing_references = used_reference_ids - reference_ids
    if missing_references:
        joined = "、".join(sorted(missing_references))
        raise ResearchContractError(f"题目指向不存在的素材引用：{joined}")
