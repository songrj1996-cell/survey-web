"""调研素材契约的无状态脱敏、哈希、去重和完整性工具。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime, time
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel

from app.schemas.questionnaire import (
    CanonicalQuestion,
    CanonicalQuestionType,
    MappingStatus,
    QuestionnaireSnapshot,
)
from app.schemas.research_assets import (
    AssetContextType,
    AssetDerivative,
    AssetReference,
    AssetRole,
    DerivativeType,
    ResearchAsset,
    ResearchAssetCollection,
    ResearchDocument,
    ResearchSource,
)


class ResearchContractError(ValueError):
    """领域对象之间的引用、归属或唯一性不满足契约。"""


_TRANSIENT_PROVIDER_FIELDS = frozenset({"contenturi"})
_SECRET_PROVIDER_FIELDS = frozenset({
    "accesskeyid",
    "auth",
    "accesstoken",
    "apikey",
    "authorization",
    "clientsecret",
    "clientassertion",
    "cookie",
    "credential",
    "idtoken",
    "jwt",
    "privatekey",
    "oauthtoken",
    "password",
    "refreshtoken",
    "secret",
    "secretaccesskey",
    "setcookie",
    "token",
})
_SECRET_FIELD_SUFFIXES = (
    "accesskey",
    "accesstoken",
    "apikey",
    "authorizationcode",
    "bearertoken",
    "clientsecret",
    "cookie",
    "credential",
    "idtoken",
    "oauthcode",
    "password",
    "refreshtoken",
    "securitytoken",
    "secret",
    "secretkey",
    "sessiontoken",
    "signature",
    "csrftoken",
    "token",
)
_SENSITIVE_OR_TRANSIENT_QUERY_FIELDS = frozenset({
    "accesstoken",
    "authorization",
    "code",
    "credential",
    "expires",
    "expiration",
    "key",
    "oauthtoken",
    "signature",
    "sig",
    "token",
    "xamzcredential",
    "xamzdate",
    "xamzexpires",
    "xamzsecuritytoken",
    "xamzsignature",
    "xgoogalgorithm",
    "xgoogcredential",
    "xgoogdate",
    "xgoogexpires",
    "xgoogsecuritytoken",
    "xgoogsignature",
})


def _json_compatible(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=False)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """生成与字典插入顺序无关的 UTF-8 JSON 文本。"""
    try:
        return json.dumps(
            _json_compatible(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ResearchContractError("契约值必须是可稳定序列化的 JSON 数据") from error


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def structured_sha256(value: Any) -> str:
    return content_sha256(canonical_json(value).encode("utf-8"))


def _normalized_field_key(value: str) -> str:
    """统一 snake/camel/kebab 等字段名后再做敏感键判断。"""
    return "".join(character for character in value.casefold() if character.isalnum())


def _is_secret_field(value: str) -> bool:
    normalized = _normalized_field_key(value)
    return (
        normalized in _SECRET_PROVIDER_FIELDS
        or normalized.endswith(_SECRET_FIELD_SUFFIXES)
    )


def _is_potential_secret_field(value: str, item: Any) -> bool:
    """只对字符串/字节凭证应用宽松后缀规则，避免误删布尔元数据。"""
    if _normalized_field_key(value) in _SECRET_PROVIDER_FIELDS:
        return True
    return isinstance(item, (str, bytes)) and _is_secret_field(value)


def _sanitize_url(value: str) -> str:
    """移除 URL 中不能持久化的授权、签名和短期有效参数。"""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return value

    safe_query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if _normalized_field_key(key) not in _SENSITIVE_OR_TRANSIENT_QUERY_FIELDS
        and not _is_secret_field(key)
    ]
    safe_query.sort()
    safe_netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((
        parts.scheme.lower(),
        safe_netloc,
        parts.path,
        urlencode(safe_query, doseq=True),
        "",
    ))


def _sanitize_provider_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized_key = _normalized_field_key(key)
            if normalized_key in _TRANSIENT_PROVIDER_FIELDS:
                continue
            if _is_potential_secret_field(key, item):
                continue
            sanitized[key] = _sanitize_provider_value(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_provider_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_url(value)
    return value


def sanitize_provider_payload(value: Any) -> Any:
    """返回可持久化的 Provider JSON 副本，不修改调用方数据。

    Google 图片的 ``contentUri``、OAuth/密钥字段、带签名或授权参数的
    URL 以及 URL fragment 都不会进入返回值。稳定的 Provider ID、题目结构、
    ``sourceUri`` 和普通查询参数会保留。
    """
    return _sanitize_provider_value(_json_compatible(value))


def provider_definition_sha256(value: Any) -> str:
    """对脱敏后的 Provider 定义计算稳定哈希。"""
    return structured_sha256(sanitize_provider_payload(value))


def _require_safe_provider_payload(label: str, value: Any) -> None:
    if _json_compatible(value) != sanitize_provider_payload(value):
        raise ResearchContractError(
            f"{label} 含临时下载地址、授权信息或敏感 URL 参数，必须先脱敏"
        )


def build_asset_dedupe_key(
    asset: ResearchAsset,
    *,
    owner_ref: str,
    collection_id: str | None = None,
) -> str:
    """生成带用户隔离的素材去重键。

    同一用户默认可跨集合复用内容；传入 ``collection_id`` 时则进一步限制在
    单个集合内。键中只写入范围哈希，不暴露用户标识。
    """
    if not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    if collection_id is not None and not collection_id.strip():
        raise ValueError("collection_id 不能为空字符串")

    scope_hash = structured_sha256({
        "owner_ref": owner_ref,
        "collection_id": collection_id,
    })
    if asset.content_hash:
        identity = f"content:{asset.content_hash}"
    elif asset.provider_resource_id:
        provider_identity = {
            "provider": asset.provider.value,
            "provider_resource_id": asset.provider_resource_id,
            "provider_version": asset.provider_version,
            "document_id": asset.document_id,
            "source_locator": sanitize_provider_payload(asset.source_locator),
        }
        identity = f"provider:{structured_sha256(provider_identity)}"
    else:
        fallback = {
            "document_id": asset.document_id,
            "media_type": asset.media_type.value,
            "mime_type": asset.mime_type,
            "filename": asset.filename,
            "size_bytes": asset.size_bytes,
            "source_locator": sanitize_provider_payload(asset.source_locator),
        }
        identity = f"metadata:{structured_sha256(fallback)}"
    return f"asset:v2:{scope_hash}:{identity}"


def build_import_idempotency_key(
    source: ResearchSource,
    document: ResearchDocument | None = None,
) -> str:
    """为同一用户的同一来源版本生成稳定导入键。"""
    identity: dict[str, Any] = {
        "source_kind": source.source_kind.value,
        "provider": source.provider.value,
        "owner_ref": source.owner_ref,
        "document_type": document.document_type.value if document else None,
        "content_hash": document.content_hash if document else None,
    }
    if not document or not document.content_hash:
        identity.update({
            "source_identity": _sanitize_url(
                source.original_url or source.original_name
            ),
            "source_locator": sanitize_provider_payload(
                document.source_locator if document else None
            ),
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


def _require_safe_message_locators(label: str, items: Iterable[Any]) -> None:
    for index, item in enumerate(items):
        locator = getattr(item, "source_locator", None)
        if locator is not None:
            _require_safe_provider_payload(
                f"{label}[{index}].source_locator",
                locator,
            )


def _validate_derivatives(
    derivatives: list[AssetDerivative],
    asset_ids: set[str],
) -> None:
    derivative_ids = _require_unique(
        "素材派生物", (item.derivative_id for item in derivatives)
    )
    derivative_by_id = {item.derivative_id: item for item in derivatives}

    for derivative in derivatives:
        if derivative.asset_id not in asset_ids:
            raise ResearchContractError(
                f"素材派生物 {derivative.derivative_id} 指向不存在的素材 "
                f"{derivative.asset_id}"
            )
        parent_id = derivative.revised_from_derivative_id
        if parent_id is None:
            continue
        if parent_id not in derivative_ids:
            raise ResearchContractError(
                f"素材派生物 {derivative.derivative_id} 指向不存在的父版本 "
                f"{parent_id}"
            )
        if derivative_by_id[parent_id].asset_id != derivative.asset_id:
            raise ResearchContractError(
                f"素材派生物 {derivative.derivative_id} 与父版本 {parent_id} "
                "不属于同一素材"
            )

    for derivative in derivatives:
        path: set[str] = set()
        current: AssetDerivative | None = derivative
        while current and current.revised_from_derivative_id:
            if current.derivative_id in path:
                raise ResearchContractError(
                    f"素材派生版本链存在循环：{current.derivative_id}"
                )
            path.add(current.derivative_id)
            current = derivative_by_id.get(current.revised_from_derivative_id)


def validate_research_asset_collection(
    collection: ResearchAssetCollection,
) -> None:
    """校验集合内的用户归属、来源、文档、素材和派生版本。"""
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

    sources = {source.source_id: source for source in collection.sources}
    documents = {
        document.document_id: document for document in collection.documents
    }
    assets = {asset.asset_id: asset for asset in collection.assets}

    for source in collection.sources:
        if source.owner_ref != collection.owner_ref:
            raise ResearchContractError(
                f"素材来源 {source.source_id} 的 owner_ref 与集合不一致"
            )
        if source.original_url is not None:
            _require_safe_provider_payload(
                f"素材来源 {source.source_id} 的 original_url",
                source.original_url,
            )
        _require_safe_message_locators(
            f"素材来源 {source.source_id} 的 warnings",
            source.warnings,
        )
        _require_safe_message_locators(
            f"素材来源 {source.source_id} 的 issues",
            source.issues,
        )

    for document in collection.documents:
        if document.source_id not in source_ids:
            raise ResearchContractError(
                f"素材文档 {document.document_id} 指向不存在的来源 "
                f"{document.source_id}"
            )
        locator = document.source_locator
        if locator is None:
            pass
        else:
            _require_safe_provider_payload(
                f"素材文档 {document.document_id} 的 source_locator",
                locator,
            )
            if locator.source_id is not None and locator.source_id != document.source_id:
                raise ResearchContractError(
                    f"素材文档 {document.document_id} 的 source_locator.source_id 不一致"
                )
            if locator.document_id is not None and locator.document_id != document.document_id:
                raise ResearchContractError(
                    f"素材文档 {document.document_id} 的 source_locator.document_id 不一致"
                )
            source = sources[document.source_id]
            if locator.provider is not None and locator.provider != source.provider:
                raise ResearchContractError(
                    f"素材文档 {document.document_id} 的来源 Provider 不一致"
                )
        _require_safe_message_locators(
            f"素材文档 {document.document_id} 的 warnings",
            document.warnings,
        )
        _require_safe_message_locators(
            f"素材文档 {document.document_id} 的 issues",
            document.issues,
        )

    for asset in collection.assets:
        if asset.document_id not in document_ids:
            raise ResearchContractError(
                f"素材 {asset.asset_id} 指向不存在的文档 {asset.document_id}"
            )
        document = documents[asset.document_id]
        source = sources[document.source_id]
        if asset.provider != source.provider:
            raise ResearchContractError(
                f"素材 {asset.asset_id} 与所属文档的来源 Provider 不一致"
            )
        if asset.provider_resource_id is not None:
            _require_safe_provider_payload(
                f"素材 {asset.asset_id} 的 provider_resource_id",
                asset.provider_resource_id,
            )
        if asset.provider_version is not None:
            _require_safe_provider_payload(
                f"素材 {asset.asset_id} 的 provider_version",
                asset.provider_version,
            )
        locator = asset.source_locator
        if locator is not None:
            if locator.document_id is not None and locator.document_id != asset.document_id:
                raise ResearchContractError(
                    f"素材 {asset.asset_id} 的 source_locator.document_id 不一致"
                )
            _require_safe_provider_payload(
                f"素材 {asset.asset_id} 的 source_locator",
                locator,
            )
            if locator.source_id is not None and locator.source_id != document.source_id:
                raise ResearchContractError(
                    f"素材 {asset.asset_id} 的 source_locator.source_id 不一致"
                )
            if locator.provider is not None and locator.provider != asset.provider:
                raise ResearchContractError(
                    f"素材 {asset.asset_id} 的来源 Provider 不一致"
                )
        _require_safe_message_locators(
            f"素材 {asset.asset_id} 的 warnings",
            asset.warnings,
        )
        _require_safe_message_locators(
            f"素材 {asset.asset_id} 的 issues",
            asset.issues,
        )

    for reference in collection.references:
        if reference.asset_id not in asset_ids:
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 指向不存在的素材 "
                f"{reference.asset_id}"
            )
        locator = reference.source_locator
        _require_safe_provider_payload(
            f"素材引用 {reference.reference_id} 的 source_locator",
            locator,
        )
        _require_safe_message_locators(
            f"素材引用 {reference.reference_id} 的 warnings",
            reference.warnings,
        )
        if locator.source_id is not None and locator.source_id not in source_ids:
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 指向不存在的来源 "
                f"{locator.source_id}"
            )
        if locator.document_id is not None and locator.document_id not in document_ids:
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 指向不存在的文档 "
                f"{locator.document_id}"
            )
        if reference.asset_id not in assets:
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 指向不存在的素材"
            )

    for derivative in collection.derivatives:
        _require_safe_provider_payload(
            f"素材派生物 {derivative.derivative_id} 的 payload",
            derivative.payload,
        )

    _validate_derivatives(collection.derivatives, asset_ids)


def _provider_ids_for_question(question: CanonicalQuestion) -> set[str]:
    provider_ids = {
        row.provider_question_id
        for row in question.rows
        if row.provider_question_id is not None
    }
    if question.provider_question_id is not None:
        provider_ids.add(question.provider_question_id)
    return provider_ids


def _google_question_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        if isinstance(value.get("questionId"), str):
            found.add(value["questionId"])
        for child in value.values():
            found.update(_google_question_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_google_question_ids(child))
    return found


def _validate_google_exact_root_definition(
    snapshot: QuestionnaireSnapshot,
) -> None:
    """闭合 Forms 根原文与拆分 Provider Item，禁止跨表单拼接。"""
    if not (
        snapshot.provider.value == "google_forms"
        and snapshot.source_mode.value == "official_api"
        and snapshot.mapping_status == MappingStatus.EXACT
    ):
        return

    if not snapshot.provider_form_id:
        raise ResearchContractError(
            "Google 官方精确快照缺少 provider_form_id"
        )
    raw_form_id = snapshot.provider_raw_definition.get("formId")
    if not isinstance(raw_form_id, str) or not raw_form_id:
        raise ResearchContractError(
            "Google 官方精确快照根原始定义缺少 formId"
        )
    if raw_form_id != snapshot.provider_form_id:
        raise ResearchContractError(
            "Google 官方精确快照根原始 formId 与 provider_form_id 不一致"
        )

    raw_items = snapshot.provider_raw_definition.get("items")
    if not isinstance(raw_items, list):
        raise ResearchContractError(
            "Google 官方精确快照根原始定义缺少 items"
        )
    if len(raw_items) != len(snapshot.provider_items):
        raise ResearchContractError(
            "Google 官方精确快照根原始 items 与 Provider Items 数量不一致"
        )

    for position, (raw_item, provider_item) in enumerate(
        zip(raw_items, snapshot.provider_items, strict=True)
    ):
        if not isinstance(raw_item, dict):
            raise ResearchContractError(
                f"Google 官方精确快照根原始 items[{position}] 不是对象"
            )
        if raw_item.get("itemId") != provider_item.provider_item_id:
            raise ResearchContractError(
                f"Google 官方精确快照根原始 items[{position}] 的 itemId "
                "与 Provider Item 不一致"
            )
        if _google_question_ids(raw_item) != set(
            provider_item.provider_question_ids
        ):
            raise ResearchContractError(
                f"Google 官方精确快照根原始 items[{position}] 的 questionId "
                "与 Provider Item 不一致"
            )
        if raw_item != provider_item.raw_definition:
            raise ResearchContractError(
                f"Google 官方精确快照根原始 items[{position}] "
                "与 Provider Item 原始定义不一致"
            )


def _validate_provider_and_canonical_structure(
    snapshot: QuestionnaireSnapshot,
) -> dict[str, CanonicalQuestion]:
    _require_unique(
        "Provider Item",
        (item.provider_item_id for item in snapshot.provider_items),
    )
    provider_question_ids = _require_unique(
        "Provider 题目",
        (
            question_id
            for item in snapshot.provider_items
            for question_id in item.provider_question_ids
        ),
    )
    provider_items = {
        item.provider_item_id: item for item in snapshot.provider_items
    }

    for expected_position, item in enumerate(snapshot.provider_items):
        if item.provider_position != expected_position:
            raise ResearchContractError(
                f"Provider Item {item.provider_item_id} 的 position="
                f"{item.provider_position} 与数组位置 {expected_position} 不一致"
            )
        if item.provider != snapshot.provider:
            raise ResearchContractError(
                f"Provider Item {item.provider_item_id} 与问卷 Provider 不一致"
            )
        if item.source_locator.provider not in {None, snapshot.provider}:
            raise ResearchContractError(
                f"Provider Item {item.provider_item_id} 的来源 Provider 不一致"
            )
        _require_safe_provider_payload(
            f"Provider Item {item.provider_item_id} 的 source_locator",
            item.source_locator,
        )
        if item.source_locator.provider_item_id not in {
            None,
            item.provider_item_id,
        }:
            raise ResearchContractError(
                f"Provider Item {item.provider_item_id} 的来源 itemId 不一致"
            )
        if (
            item.source_locator.provider_question_id is not None
            and item.source_locator.provider_question_id
            not in item.provider_question_ids
        ):
            raise ResearchContractError(
                f"Provider Item {item.provider_item_id} 的来源 questionId 不属于该 Item"
            )
        if (
            snapshot.provider_form_id
            and item.source_locator.provider_form_id
            and item.source_locator.provider_form_id != snapshot.provider_form_id
        ):
            raise ResearchContractError(
                f"Provider Item {item.provider_item_id} 的来源 formId 不一致"
            )
        _require_safe_provider_payload(
            f"Provider Item {item.provider_item_id} 原始定义",
            item.raw_definition,
        )
        if snapshot.provider.value == "google_forms":
            raw_item_id = item.raw_definition.get("itemId")
            if (
                snapshot.source_mode.value == "official_api"
                and snapshot.mapping_status == MappingStatus.EXACT
                and raw_item_id is None
            ):
                raise ResearchContractError(
                    f"Google 官方精确快照 Item {item.provider_item_id} 缺少原始 itemId"
                )
            if raw_item_id not in {None, item.provider_item_id}:
                raise ResearchContractError(
                    f"Provider Item {item.provider_item_id} 与 Google 原始 itemId 不一致"
                )

            raw_question_ids = _google_question_ids(item.raw_definition)
            if raw_question_ids and raw_question_ids != set(item.provider_question_ids):
                raise ResearchContractError(
                    f"Provider Item {item.provider_item_id} 与 Google 原始 questionId 不一致"
                )
            if (
                snapshot.source_mode.value == "official_api"
                and snapshot.mapping_status == MappingStatus.EXACT
                and raw_question_ids != set(item.provider_question_ids)
            ):
                raise ResearchContractError(
                    f"Google 官方精确快照 Item {item.provider_item_id} "
                    "缺少完整原始 questionId"
                )

    canonical_ids = _require_unique(
        "Canonical 题目",
        (question.question_id for question in snapshot.canonical_questions),
    )
    canonical_questions = {
        question.question_id: question
        for question in snapshot.canonical_questions
    }
    section_ids = {
        question.question_id
        for question in snapshot.canonical_questions
        if question.canonical_type == CanonicalQuestionType.SECTION
    }
    canonical_provider_owners: dict[str, str] = {}

    for question in snapshot.canonical_questions:
        _require_safe_message_locators(
            f"题目 {question.question_id} 的 warnings",
            question.warnings,
        )
        row_keys = _require_unique(
            f"题目 {question.question_id} 的行",
            (row.row_key for row in question.rows),
        )
        option_keys = _require_unique(
            f"题目 {question.question_id} 的选项",
            (option.option_key for option in question.options),
        )
        if question.provider_item_id is not None:
            if question.provider_item_id not in provider_items:
                raise ResearchContractError(
                    f"题目 {question.question_id} 指向不存在的 Provider Item "
                    f"{question.provider_item_id}"
                )
            item_question_ids = set(
                provider_items[question.provider_item_id].provider_question_ids
            )
        else:
            item_question_ids = provider_question_ids

        if (
            question.provider_item_id is not None
            and question.provider_question_id is None
            and question.rows
            and not any(row.provider_question_id for row in question.rows)
        ):
            raise ResearchContractError(
                f"题目 {question.question_id} 有行但没有可关联的 Provider 题目 ID"
            )

        for question_id in _provider_ids_for_question(question):
            if question_id not in provider_question_ids:
                raise ResearchContractError(
                    f"题目 {question.question_id} 指向不存在的 Provider 题目 "
                    f"{question_id}"
                )
            if question.provider_item_id and question_id not in item_question_ids:
                raise ResearchContractError(
                    f"题目 {question.question_id} 的 Provider 题目 {question_id} "
                    "不属于声明的 Provider Item"
                )
            previous_owner = canonical_provider_owners.setdefault(
                question_id,
                question.question_id,
            )
            if previous_owner != question.question_id:
                raise ResearchContractError(
                    f"Provider 题目 {question_id} 同时映射到 Canonical 题目 "
                    f"{previous_owner} 和 {question.question_id}"
                )

        for branch in question.branching:
            if branch.option_key is not None and branch.option_key not in option_keys:
                raise ResearchContractError(
                    f"题目 {question.question_id} 的跳转规则指向不存在的选项 "
                    f"{branch.option_key}"
                )
            if (
                branch.target_section_id is not None
                and branch.target_section_id not in section_ids
            ):
                raise ResearchContractError(
                    f"题目 {question.question_id} 的跳转规则指向不存在的分区 "
                    f"{branch.target_section_id}"
                )

        if not row_keys and any(
            binding.row_key is not None
            for mapping in snapshot.response_column_mappings
            if mapping.question_id == question.question_id
            for binding in mapping.bindings
        ):
            raise ResearchContractError(
                f"题目 {question.question_id} 没有矩阵行却声明了回答 row_key"
            )

    _require_safe_provider_payload("问卷 Provider 原始定义", snapshot.provider_raw_definition)
    _validate_google_exact_root_definition(snapshot)
    if not canonical_ids and snapshot.response_column_mappings:
        raise ResearchContractError("无 Canonical 题目时不能声明回答列映射")
    return canonical_questions


def _validate_response_mappings(
    snapshot: QuestionnaireSnapshot,
    canonical_questions: dict[str, CanonicalQuestion],
    *,
    source_id: str,
) -> None:
    _require_unique(
        "回答列映射",
        (mapping.question_id for mapping in snapshot.response_column_mappings),
    )
    used_response_locations: set[tuple[str, str | int]] = set()
    mappings_by_question: dict[str, Any] = {
        mapping.question_id: mapping
        for mapping in snapshot.response_column_mappings
    }

    if snapshot.mapping_status in {
        MappingStatus.EXACT,
        MappingStatus.NORMALIZED,
    }:
        for question in snapshot.canonical_questions:
            if question.canonical_type in {
                CanonicalQuestionType.SECTION,
                CanonicalQuestionType.STATIC_TEXT,
            }:
                continue
            expected_provider_ids = _provider_ids_for_question(question)
            if not expected_provider_ids:
                raise ResearchContractError(
                    f"精确映射题目 {question.question_id} 缺少 Provider 身份"
                )
            mapping = mappings_by_question.get(question.question_id)
            if mapping is None:
                raise ResearchContractError(
                    f"精确映射题目 {question.question_id} 缺少回答列映射"
                )
            if snapshot.mapping_status == MappingStatus.EXACT and (
                mapping.mapping_status != MappingStatus.EXACT
            ):
                raise ResearchContractError(
                    f"问卷标记为精确映射，但题目 {question.question_id} 未精确映射"
                )
            bound_provider_ids = {
                binding.provider_question_id for binding in mapping.bindings
            }
            if bound_provider_ids != expected_provider_ids:
                raise ResearchContractError(
                    f"精确映射题目 {question.question_id} 未覆盖全部 Provider 题目"
                )
            expected_rows = {
                (
                    row.provider_question_id or question.provider_question_id,
                    row.row_key,
                )
                for row in question.rows
            }
            bound_rows = {
                (binding.provider_question_id, binding.row_key)
                for binding in mapping.bindings
                if binding.row_key is not None
            }
            if expected_rows and bound_rows != expected_rows:
                raise ResearchContractError(
                    f"精确映射题目 {question.question_id} 未覆盖全部矩阵行"
                )

    for mapping in snapshot.response_column_mappings:
        _require_safe_message_locators(
            f"题目 {mapping.question_id} 的回答映射 warnings",
            mapping.warnings,
        )
        question = canonical_questions.get(mapping.question_id)
        if question is None:
            raise ResearchContractError(
                f"回答列映射指向不存在的题目 {mapping.question_id}"
            )
        if mapping.mapping_status == MappingStatus.EXACT:
            expected_provider_ids = _provider_ids_for_question(question)
            bound_provider_ids = {
                binding.provider_question_id for binding in mapping.bindings
            }
            if not expected_provider_ids or bound_provider_ids != expected_provider_ids:
                raise ResearchContractError(
                    f"精确回答列映射 {mapping.question_id} 未覆盖全部 Provider 题目"
                )
            expected_rows = {
                (
                    row.provider_question_id or question.provider_question_id,
                    row.row_key,
                )
                for row in question.rows
            }
            bound_rows = {
                (binding.provider_question_id, binding.row_key)
                for binding in mapping.bindings
                if binding.row_key is not None
            }
            if expected_rows and bound_rows != expected_rows:
                raise ResearchContractError(
                    f"精确回答列映射 {mapping.question_id} 未覆盖全部矩阵行"
                )
        allowed_provider_ids = _provider_ids_for_question(question)
        rows = {row.row_key: row for row in question.rows}
        rows_by_provider_id: dict[str, set[str]] = {}
        for row in question.rows:
            provider_id = row.provider_question_id or question.provider_question_id
            if provider_id is not None:
                rows_by_provider_id.setdefault(provider_id, set()).add(row.row_key)
        binding_keys: list[str] = []

        for binding in mapping.bindings:
            if binding.provider_question_id not in allowed_provider_ids:
                raise ResearchContractError(
                    f"题目 {mapping.question_id} 的回答绑定指向其他题目的 "
                    f"Provider ID {binding.provider_question_id}"
                )
            expected_rows = rows_by_provider_id.get(binding.provider_question_id)
            if expected_rows and binding.row_key not in expected_rows:
                raise ResearchContractError(
                    f"题目 {mapping.question_id} 的回答绑定未准确关联矩阵行 "
                    f"{'/'.join(sorted(expected_rows))}"
                )
            if binding.row_key is not None:
                row = rows.get(binding.row_key)
                if row is None:
                    raise ResearchContractError(
                        f"题目 {mapping.question_id} 的回答绑定指向不存在的行 "
                        f"{binding.row_key}"
                    )
                if (
                    row.provider_question_id is not None
                    and row.provider_question_id != binding.provider_question_id
                ):
                    raise ResearchContractError(
                        f"题目 {mapping.question_id} 的回答绑定行与 Provider ID 不一致"
                    )
            if (
                binding.source_locator is not None
                and binding.source_locator.provider not in {None, snapshot.provider}
            ):
                raise ResearchContractError(
                    f"题目 {mapping.question_id} 的回答绑定来源 Provider 不一致"
                )
            locator = binding.source_locator
            if locator is not None:
                _require_safe_provider_payload(
                    f"题目 {mapping.question_id} 的回答绑定 source_locator",
                    locator,
                )
                if (
                    snapshot.provider_form_id
                    and locator.provider_form_id
                    and locator.provider_form_id != snapshot.provider_form_id
                ):
                    raise ResearchContractError(
                        f"题目 {mapping.question_id} 的回答绑定 formId 不一致"
                    )
                if locator.document_id not in {None, snapshot.document_id}:
                    raise ResearchContractError(
                        f"题目 {mapping.question_id} 的回答绑定文档不一致"
                    )
                if locator.source_id not in {None, source_id}:
                    raise ResearchContractError(
                        f"题目 {mapping.question_id} 的回答绑定来源不一致"
                    )
                if locator.provider_question_id not in {
                    None,
                    binding.provider_question_id,
                }:
                    raise ResearchContractError(
                        f"题目 {mapping.question_id} 的回答绑定 questionId 不一致"
                    )
                if (
                    question.provider_item_id is not None
                    and locator.provider_item_id not in {
                        None,
                        question.provider_item_id,
                    }
                ):
                    raise ResearchContractError(
                        f"题目 {mapping.question_id} 的回答绑定 itemId 不一致"
                    )
            if binding.column_index is not None:
                location = ("column", binding.column_index)
            else:
                location = ("key", binding.response_key or "")
            if location in used_response_locations:
                raise ResearchContractError(
                    f"回答位置重复绑定：{location[1]}"
                )
            used_response_locations.add(location)
            binding_keys.append(canonical_json({
                "provider_question_id": binding.provider_question_id,
                "row_key": binding.row_key,
                "response_key": binding.response_key,
                "column_index": binding.column_index,
            }))
        _require_unique(
            f"题目 {mapping.question_id} 的回答绑定",
            binding_keys,
        )


def _reference_site(
    context_type: AssetContextType,
    question_id: str,
    option_key: str | None = None,
    row_key: str | None = None,
) -> tuple[str, str, str | None, str | None]:
    return (context_type.value, question_id, option_key, row_key)


def _validate_reference_shape(reference: AssetReference) -> None:
    if reference.context_type == AssetContextType.SURVEY_OPTION:
        if not reference.option_key or reference.row_key is not None:
            raise ResearchContractError(
                f"选项素材引用 {reference.reference_id} 必须且只能指定 option_key"
            )
    elif reference.context_type == AssetContextType.SURVEY_ROW:
        if not reference.row_key or reference.option_key is not None:
            raise ResearchContractError(
                f"行素材引用 {reference.reference_id} 必须且只能指定 row_key"
            )
    elif reference.option_key is not None or reference.row_key is not None:
        raise ResearchContractError(
            f"素材引用 {reference.reference_id} 的上下文不能指定 option_key/row_key"
        )

    if (
        reference.role == AssetRole.OPTION_STIMULUS
        and reference.context_type != AssetContextType.SURVEY_OPTION
    ):
        raise ResearchContractError(
            f"选项素材引用 {reference.reference_id} 必须使用 survey_option 上下文"
        )


def _validate_references(
    snapshot: QuestionnaireSnapshot,
    collection: ResearchAssetCollection,
    canonical_questions: dict[str, CanonicalQuestion],
) -> None:
    references = {
        reference.reference_id: reference
        for reference in collection.references
    }
    snapshot_document = next(
        document
        for document in collection.documents
        if document.document_id == snapshot.document_id
    )
    expected_sites: dict[
        str,
        set[tuple[str, str, str | None, str | None]],
    ] = {}

    def add_sites(
        label: str,
        reference_ids: list[str],
        site: tuple[str, str, str | None, str | None],
    ) -> None:
        _require_unique(label, reference_ids)
        for reference_id in reference_ids:
            expected_sites.setdefault(reference_id, set()).add(site)

    for question in snapshot.canonical_questions:
        add_sites(
            f"题目 {question.question_id} 的素材引用",
            question.asset_reference_ids,
            _reference_site(
                AssetContextType.SURVEY_QUESTION,
                question.question_id,
            ),
        )
        for option in question.options:
            add_sites(
                f"题目 {question.question_id} 选项 {option.option_key} 的素材引用",
                option.asset_reference_ids,
                _reference_site(
                    AssetContextType.SURVEY_OPTION,
                    question.question_id,
                    option_key=option.option_key,
                ),
            )
        for row in question.rows:
            add_sites(
                f"题目 {question.question_id} 行 {row.row_key} 的素材引用",
                row.asset_reference_ids,
                _reference_site(
                    AssetContextType.SURVEY_ROW,
                    question.question_id,
                    row_key=row.row_key,
                ),
            )

    missing_references = set(expected_sites) - set(references)
    if missing_references:
        joined = "、".join(sorted(missing_references))
        raise ResearchContractError(f"题目指向不存在的素材引用：{joined}")

    survey_contexts = {
        AssetContextType.SURVEY_QUESTION,
        AssetContextType.SURVEY_OPTION,
        AssetContextType.SURVEY_ROW,
    }
    for reference in collection.references:
        _validate_reference_shape(reference)
        if reference.context_type not in survey_contexts:
            if reference.reference_id in expected_sites:
                raise ResearchContractError(
                    f"素材引用 {reference.reference_id} 的声明位置与题目结构不一致"
                )
            continue
        question = canonical_questions.get(reference.context_id)
        if question is None:
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 指向不存在的题目 "
                f"{reference.context_id}"
            )
        declared_site = _reference_site(
            reference.context_type,
            reference.context_id,
            option_key=reference.option_key,
            row_key=reference.row_key,
        )
        sites = expected_sites.get(reference.reference_id, set())
        if sites != {declared_site}:
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 的声明位置与题目结构不一致"
            )

        if reference.source_locator.provider not in {None, snapshot.provider}:
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 的问卷 Provider 不一致"
            )
        if (
            snapshot.provider_form_id
            and reference.source_locator.provider_form_id
            and reference.source_locator.provider_form_id != snapshot.provider_form_id
        ):
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 的 formId 不一致"
            )
        if (
            reference.source_locator.document_id is not None
            and reference.source_locator.document_id != snapshot.document_id
        ):
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 与问卷快照文档不一致"
            )
        if (
            reference.source_locator.source_id is not None
            and reference.source_locator.source_id != snapshot_document.source_id
        ):
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 与问卷快照来源不一致"
            )
        allowed_provider_ids = _provider_ids_for_question(question)
        located_provider_id = reference.source_locator.provider_question_id
        if (
            located_provider_id is not None
            and located_provider_id not in allowed_provider_ids
        ):
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 的 Provider 题目指向其他题目"
            )
        if (
            question.provider_item_id is not None
            and reference.source_locator.provider_item_id is not None
            and reference.source_locator.provider_item_id
            != question.provider_item_id
        ):
            raise ResearchContractError(
                f"素材引用 {reference.reference_id} 的 Provider Item 不一致"
            )

        if reference.context_type == AssetContextType.SURVEY_OPTION:
            option = next(
                item for item in question.options
                if item.option_key == reference.option_key
            )
            if (
                option.provider_option_id
                and reference.source_locator.provider_option_id
                and option.provider_option_id
                != reference.source_locator.provider_option_id
            ):
                raise ResearchContractError(
                    f"选项素材引用 {reference.reference_id} 的 Provider 选项不一致"
                )
        elif reference.context_type == AssetContextType.SURVEY_ROW:
            row = next(
                item for item in question.rows
                if item.row_key == reference.row_key
            )
            if (
                row.provider_question_id
                and reference.source_locator.provider_question_id
                and row.provider_question_id
                != reference.source_locator.provider_question_id
            ):
                raise ResearchContractError(
                    f"行素材引用 {reference.reference_id} 的 Provider 题目不一致"
                )


def validate_research_contract(
    snapshot: QuestionnaireSnapshot,
    collection: ResearchAssetCollection,
) -> None:
    """校验第 1 批问卷快照与素材集合的完整聚合契约。"""
    validate_research_asset_collection(collection)

    documents = {
        document.document_id: document for document in collection.documents
    }
    sources = {
        source.source_id: source for source in collection.sources
    }
    document = documents.get(snapshot.document_id)
    if document is None:
        raise ResearchContractError(
            f"问卷快照 {snapshot.snapshot_id} 指向不存在的文档 "
            f"{snapshot.document_id}"
        )
    source = sources[document.source_id]
    if source.provider != snapshot.provider:
        raise ResearchContractError(
            f"问卷快照 {snapshot.snapshot_id} 与来源 Provider 不一致"
        )
    if snapshot.asset_count != len(collection.assets):
        raise ResearchContractError(
            f"问卷快照 asset_count={snapshot.asset_count} 与集合素材数 "
            f"{len(collection.assets)} 不一致"
        )
    if (
        snapshot.provider_form_id
        and document.source_locator
        and document.source_locator.provider_form_id
        and snapshot.provider_form_id
        != document.source_locator.provider_form_id
    ):
        raise ResearchContractError(
            f"问卷快照 {snapshot.snapshot_id} 与文档 formId 不一致"
        )

    _require_safe_message_locators("问卷快照 warnings", snapshot.warnings)
    canonical_questions = _validate_provider_and_canonical_structure(snapshot)
    _validate_response_mappings(
        snapshot,
        canonical_questions,
        source_id=document.source_id,
    )
    _validate_references(snapshot, collection, canonical_questions)

    for derivative in collection.derivatives:
        if derivative.derivative_type == DerivativeType.HUMAN_REVISION:
            parent = next(
                item for item in collection.derivatives
                if item.derivative_id == derivative.revised_from_derivative_id
            )
            try:
                parent_is_newer = parent.created_at > derivative.created_at
            except TypeError as error:
                raise ResearchContractError(
                    f"人工修订 {derivative.derivative_id} 与父版本的时区口径不一致"
                ) from error
            if parent_is_newer:
                raise ResearchContractError(
                    f"人工修订 {derivative.derivative_id} 早于父派生版本"
                )
