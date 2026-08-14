"""问卷 PDF 到中可信、待人工复核素材快照的异步业务门面。"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import PurePosixPath

from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
)

from app.core.research_assets import content_sha256, structured_sha256
from app.schemas.questionnaire import (
    CollectionState,
    MappingStatus,
    QuestionnaireSnapshot,
    QuestionnaireSourceMode,
)
from app.schemas.questionnaire_source_api import (
    PDF_MATERIAL_REVIEW_WARNING_CODE,
    QuestionnaireMaterialTrustLevel,
    QuestionnairePdfMaterialUploadSummary,
)
from app.schemas.questionnaire_sources import (
    QuestionnaireAcquisitionRoute,
    QuestionnaireSourceAttempt,
    QuestionnaireSourceResult,
    questionnaire_source_priority,
)
from app.schemas.research_assets import (
    AccessStatus,
    AssetContextType,
    AssetReference,
    AssetRole,
    BindingStatus,
    DocumentType,
    ExportPolicy,
    ImportWarning,
    MediaType,
    ProcessingStatus,
    Provider,
    ResearchAsset,
    ResearchAssetCollection,
    ResearchDocument,
    ResearchSource,
    SensitivityStatus,
    SnapshotPolicy,
    SourceKind,
    SourceLocator,
)
from app.services.questionnaire_snapshot_api import (
    _validate_package_without_archive,
)
from app.services.questionnaire_source_service import (
    load_questionnaire_source_snapshot,
    save_questionnaire_source_snapshot,
)
from app.storage.research_assets import (
    ResearchAssetBundle,
    ResearchSnapshotStorage,
    SnapshotConflictError,
    SnapshotPackage,
)


SUPPORTED_QUESTIONNAIRE_PDF_MIME_TYPES = frozenset({"application/pdf"})
MAX_QUESTIONNAIRE_PDF_BYTES = 50 * 1024 * 1024
MAX_QUESTIONNAIRE_PDF_PAGES = 200
MAX_QUESTIONNAIRE_PDF_OBJECTS = 50_000
MAX_QUESTIONNAIRE_PDF_OBJECT_DEPTH = 64
MAX_QUESTIONNAIRE_PDF_PARSER_MEMORY_BYTES = 512 * 1024 * 1024
MAX_QUESTIONNAIRE_PDF_PARSER_CPU_SECONDS = 20
MAX_QUESTIONNAIRE_PDF_PARSER_WALL_SECONDS = 30.0

_PDF_SIGNATURE = b"%PDF-"
_PDF_EOF_MARKER = b"%%EOF"
_DANGEROUS_KEYS = frozenset({
    "/AA",
    "/AF",
    "/Collection",
    "/EF",
    "/EmbeddedFiles",
    "/JavaScript",
    "/JS",
    "/Launch",
    "/OpenAction",
    "/RichMedia",
    "/RichMediaContent",
    "/RichMediaSettings",
    "/URI",
    "/XFA",
})
_DANGEROUS_ACTIONS = frozenset({
    "/GoToE",
    "/GoToR",
    "/ImportData",
    "/JavaScript",
    "/Launch",
    "/Movie",
    "/Rendition",
    "/Sound",
    "/SubmitForm",
    "/URI",
})
_DANGEROUS_OBJECT_TYPES = frozenset({
    "/3D",
    "/EmbeddedFile",
    "/FileAttachment",
    "/Filespec",
    "/JavaScript",
    "/Movie",
    "/RichMedia",
    "/Screen",
    "/Sound",
})


class QuestionnairePdfMaterialSnapshotApiError(RuntimeError):
    """可由 HTTP 层安全分类的 PDF 材料导入错误基类。"""


class QuestionnairePdfMaterialInvalidError(
    QuestionnairePdfMaterialSnapshotApiError
):
    """文件声明、大小或 PDF 结构不符合安全合同。"""


class QuestionnairePdfMaterialConflictError(
    QuestionnairePdfMaterialSnapshotApiError
):
    """同一不可变快照身份已经对应不同内容。"""


class QuestionnairePdfMaterialInternalError(
    QuestionnairePdfMaterialSnapshotApiError
):
    """不得向 HTTP 响应暴露细节的建模或持久化失败。"""


class _PdfValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QuestionnairePdfMaterial:
    """一份本地上传、仅作为中可信材料保存的问卷 PDF。"""

    filename: str
    mime_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class _ValidatedPdfMaterial:
    content: bytes
    content_hash: str
    page_count: int


@dataclass(frozen=True, slots=True)
class _PdfMaterialMapping:
    result: QuestionnaireSourceResult
    media: dict[str, bytes]

    @property
    def package(self) -> SnapshotPackage:
        return SnapshotPackage(
            ResearchAssetBundle(
                self.result.snapshot,
                self.result.collection,
            ),
            dict(self.media),
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_id(prefix: str, *parts: object) -> str:
    return f"{prefix}_{structured_sha256(list(parts))[:24]}"


def _require_owner(owner_ref: str) -> str:
    if not isinstance(owner_ref, str) or not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    return owner_ref.strip()


def _retrieved_at(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception as error:
        raise QuestionnairePdfMaterialInternalError() from error
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise QuestionnairePdfMaterialInternalError()
    try:
        if value.utcoffset() is None:
            raise QuestionnairePdfMaterialInternalError()
    except (OverflowError, ValueError) as error:
        raise QuestionnairePdfMaterialInternalError() from error
    return value


def _normalized_filename(filename: object) -> str:
    if not isinstance(filename, str) or not filename:
        raise _PdfValidationError()
    if filename != filename.strip():
        raise _PdfValidationError()
    try:
        encoded = filename.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _PdfValidationError() from error
    if len(encoded) > 255:
        raise _PdfValidationError()
    if filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise _PdfValidationError()
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in filename
    ):
        raise _PdfValidationError()
    if PurePosixPath(filename).suffix.casefold() != ".pdf":
        raise _PdfValidationError()
    return filename


def _normalized_mime_type(mime_type: object) -> str:
    if not isinstance(mime_type, str):
        raise _PdfValidationError()
    normalized = mime_type.strip().casefold()
    if normalized not in SUPPORTED_QUESTIONNAIRE_PDF_MIME_TYPES:
        raise _PdfValidationError()
    return normalized


def _object_name(value: object) -> str | None:
    if isinstance(value, NameObject):
        return value
    return None


def _resolved_object_name(
    value: object,
    *,
    max_depth: int = MAX_QUESTIONNAIRE_PDF_OBJECT_DEPTH,
) -> str | None:
    """有界解引用 NameObject，避免字典键语义在遍历时丢失。"""

    seen: set[tuple[int, int]] = set()
    for _ in range(max_depth + 1):
        if not isinstance(value, IndirectObject):
            return _object_name(value)
        identity = (value.idnum, value.generation)
        if identity in seen:
            raise _PdfValidationError()
        seen.add(identity)
        try:
            value = value.get_object()
        except Exception as error:
            raise _PdfValidationError() from error
    raise _PdfValidationError()


def _validate_pdf_object_graph(
    reader: PdfReader,
    *,
    max_objects: int = MAX_QUESTIONNAIRE_PDF_OBJECTS,
    max_depth: int = MAX_QUESTIONNAIRE_PDF_OBJECT_DEPTH,
) -> None:
    """有界遍历对象图；只检查元数据，绝不解压或读取 stream 数据。"""

    xref_ids = {
        (generation, object_id)
        for generation, entries in reader.xref.items()
        for object_id in entries
    }
    xref_ids.update((0, object_id) for object_id in reader.xref_objStm)
    if len(xref_ids) > max_objects:
        raise _PdfValidationError()

    stack: list[tuple[object, int, bool]] = [(reader.trailer, 0, False)]
    visited_indirect: set[tuple[int, int, bool]] = set()
    visited_direct: set[tuple[int, bool]] = set()
    examined = 0

    while stack:
        value, depth, expected_action = stack.pop()
        examined += 1
        if examined > max_objects:
            raise _PdfValidationError()
        if depth > max_depth:
            raise _PdfValidationError()

        if isinstance(value, IndirectObject):
            identity = (value.idnum, value.generation, expected_action)
            if identity in visited_indirect:
                continue
            visited_indirect.add(identity)
            try:
                resolved = value.get_object()
            except Exception as error:
                raise _PdfValidationError() from error
            stack.append((resolved, depth, expected_action))
            continue

        if expected_action and not isinstance(
            value,
            (DictionaryObject, ArrayObject),
        ):
            raise _PdfValidationError()

        if isinstance(value, (DictionaryObject, ArrayObject)):
            identity = (id(value), expected_action)
            if identity in visited_direct:
                continue
            visited_direct.add(identity)

        if isinstance(value, DictionaryObject):
            try:
                raw_type = value.raw_get("/Type")
            except KeyError:
                raw_type = None
            is_action = (
                expected_action
                or _resolved_object_name(
                    raw_type,
                    max_depth=max_depth,
                ) == "/Action"
            )
            if is_action:
                try:
                    raw_action = value.raw_get("/S")
                except KeyError as error:
                    raise _PdfValidationError() from error
                if _resolved_object_name(
                    raw_action,
                    max_depth=max_depth,
                ) != "/GoTo":
                    raise _PdfValidationError()
            for key, child in value.items():
                key_name = _object_name(key)
                if key_name in _DANGEROUS_KEYS:
                    raise _PdfValidationError()
                if key_name == "/S":
                    action_name = _resolved_object_name(
                        child,
                        max_depth=max_depth,
                    )
                    if action_name is None:
                        raise _PdfValidationError()
                    if action_name in _DANGEROUS_ACTIONS:
                        raise _PdfValidationError()
                if key_name in {"/Type", "/Subtype"}:
                    object_type = _resolved_object_name(
                        child,
                        max_depth=max_depth,
                    )
                    if object_type is None:
                        raise _PdfValidationError()
                    if object_type in _DANGEROUS_OBJECT_TYPES:
                        raise _PdfValidationError()
                child_is_action = (
                    key_name == "/A"
                    or (is_action and key_name == "/Next")
                )
                stack.append((child, depth + 1, child_is_action))
        elif isinstance(value, ArrayObject):
            stack.extend(
                (child, depth + 1, expected_action)
                for child in value
            )


def _preflight_page_count(
    reader: PdfReader,
    *,
    max_pages: int = MAX_QUESTIONNAIRE_PDF_PAGES,
) -> int:
    try:
        root = reader.root_object
        pages_ref = root.raw_get("/Pages")
        pages = pages_ref.get_object() if isinstance(
            pages_ref,
            IndirectObject,
        ) else pages_ref
        if not isinstance(pages, DictionaryObject):
            raise _PdfValidationError()
        declared = pages.raw_get("/Count")
        if isinstance(declared, IndirectObject):
            declared = declared.get_object()
        if (
            not isinstance(declared, int)
            or isinstance(declared, bool)
            or not 1 <= declared <= max_pages
        ):
            raise _PdfValidationError()
        actual = len(reader.pages)
    except _PdfValidationError:
        raise
    except Exception as error:
        raise _PdfValidationError() from error
    if actual != declared or not 1 <= actual <= max_pages:
        raise _PdfValidationError()
    return actual


def _validate_pdf_content_in_process(
    content: bytes,
    *,
    max_pages: int,
    max_objects: int,
    max_depth: int,
) -> int:
    if not content.startswith(_PDF_SIGNATURE):
        raise _PdfValidationError()
    if _PDF_EOF_MARKER not in content[-1024:]:
        raise _PdfValidationError()
    try:
        reader = PdfReader(
            BytesIO(content),
            strict=True,
            root_object_recovery_limit=10_000,
        )
        if reader.is_encrypted:
            raise _PdfValidationError()
        _validate_pdf_object_graph(
            reader,
            max_objects=max_objects,
            max_depth=max_depth,
        )
        page_count = _preflight_page_count(reader, max_pages=max_pages)
    except _PdfValidationError:
        raise
    except (Exception, RecursionError) as error:
        raise _PdfValidationError() from error
    return page_count


def _apply_parser_resource_limits() -> None:
    """在隔离解析进程内尽力施加 OS 级 CPU/内存上限。"""

    try:
        import resource

        cpu_soft = MAX_QUESTIONNAIRE_PDF_PARSER_CPU_SECONDS
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_soft, cpu_soft + 5))
        if sys.platform.startswith("linux"):
            resource.setrlimit(
                resource.RLIMIT_AS,
                (
                    MAX_QUESTIONNAIRE_PDF_PARSER_MEMORY_BYTES,
                    MAX_QUESTIONNAIRE_PDF_PARSER_MEMORY_BYTES,
                ),
            )
        elif hasattr(resource, "RLIMIT_DATA"):
            resource.setrlimit(
                resource.RLIMIT_DATA,
                (
                    MAX_QUESTIONNAIRE_PDF_PARSER_MEMORY_BYTES,
                    MAX_QUESTIONNAIRE_PDF_PARSER_MEMORY_BYTES,
                ),
            )
    except (ImportError, OSError, ValueError):
        # 墙钟超时始终生效；不支持相应 rlimit 的平台保守降级。
        return


def _pdf_validation_worker(
    connection: object,
    content: bytes,
    max_pages: int,
    max_objects: int,
    max_depth: int,
) -> None:
    try:
        logging.getLogger("pypdf").setLevel(logging.CRITICAL)
        _apply_parser_resource_limits()
        page_count = _validate_pdf_content_in_process(
            content,
            max_pages=max_pages,
            max_objects=max_objects,
            max_depth=max_depth,
        )
        connection.send(("ok", page_count))
    except BaseException:
        try:
            connection.send(("invalid", None))
        except BaseException:
            pass
    finally:
        try:
            connection.close()
        except BaseException:
            pass


def _stop_validation_process(process: multiprocessing.Process) -> None:
    process.join(timeout=0.2)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1.0)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(timeout=1.0)


def _validate_pdf_content(content: bytes) -> int:
    """在有界子进程中解析不可信 PDF，避免主服务承受资源炸弹。"""

    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_pdf_validation_worker,
        args=(
            child_connection,
            content,
            MAX_QUESTIONNAIRE_PDF_PAGES,
            MAX_QUESTIONNAIRE_PDF_OBJECTS,
            MAX_QUESTIONNAIRE_PDF_OBJECT_DEPTH,
        ),
        daemon=True,
    )
    try:
        process.start()
        child_connection.close()
        if not parent_connection.poll(
            MAX_QUESTIONNAIRE_PDF_PARSER_WALL_SECONDS
        ):
            raise _PdfValidationError()
        try:
            status, page_count = parent_connection.recv()
        except (EOFError, OSError, ValueError) as error:
            raise _PdfValidationError() from error
        if (
            status != "ok"
            or not isinstance(page_count, int)
            or isinstance(page_count, bool)
            or not 1 <= page_count <= MAX_QUESTIONNAIRE_PDF_PAGES
        ):
            raise _PdfValidationError()
        return page_count
    except _PdfValidationError:
        raise
    except (OSError, RuntimeError) as error:
        raise QuestionnairePdfMaterialInternalError() from error
    finally:
        try:
            child_connection.close()
        except OSError:
            pass
        try:
            parent_connection.close()
        except OSError:
            pass
        if process.pid is not None:
            _stop_validation_process(process)


def _validated_material(
    material: QuestionnairePdfMaterial,
) -> _ValidatedPdfMaterial:
    if not isinstance(material, QuestionnairePdfMaterial):
        raise _PdfValidationError()
    _normalized_filename(material.filename)
    _normalized_mime_type(material.mime_type)
    content = material.content
    if (
        not isinstance(content, bytes)
        or not content
        or len(content) > MAX_QUESTIONNAIRE_PDF_BYTES
    ):
        raise _PdfValidationError()
    page_count = _validate_pdf_content(content)
    return _ValidatedPdfMaterial(
        content=content,
        content_hash=content_sha256(content),
        page_count=page_count,
    )


def _review_warning() -> ImportWarning:
    return ImportWarning(
        code=PDF_MATERIAL_REVIEW_WARNING_CODE,
        message=(
            "PDF 仅提供中可信问卷材料，不能保证稳定题目 ID、隐藏分支、"
            "回答列映射或素材绑定完整，必须人工复核后才能作为问卷定义使用"
        ),
        blocking=True,
    )


def _material_identity(material: _ValidatedPdfMaterial) -> str:
    return structured_sha256({
        "schema_version": 1,
        "format": "questionnaire_pdf_material",
        "mime_type": "application/pdf",
        "size_bytes": len(material.content),
        "page_count": material.page_count,
        "sha256": material.content_hash,
    })


def _build_mapping(
    owner_ref: str,
    material: _ValidatedPdfMaterial,
    retrieved_at: datetime,
) -> _PdfMaterialMapping:
    identity_hash = _material_identity(material)
    source_id = _stable_id(
        "src",
        "questionnaire_pdf_material",
        owner_ref,
        identity_hash,
    )
    document_id = _stable_id("doc", source_id, identity_hash)
    asset_id = _stable_id("asset", document_id, material.content_hash)
    snapshot_id = _stable_id("qsn", owner_ref, document_id, identity_hash)
    collection_id = _stable_id("rac", owner_ref, snapshot_id)
    warning = _review_warning()
    document_locator = SourceLocator(
        source_id=source_id,
        document_id=document_id,
        provider=Provider.LOCAL_UPLOAD,
        local_file_id=document_id,
    )
    asset_locator = SourceLocator(
        source_id=source_id,
        document_id=document_id,
        provider=Provider.LOCAL_UPLOAD,
        local_file_id=asset_id,
    )
    source = ResearchSource(
        source_id=source_id,
        source_kind=SourceKind.LOCAL_UPLOAD,
        provider=Provider.LOCAL_UPLOAD,
        original_name="问卷 PDF 材料",
        owner_ref=owner_ref,
        created_at=retrieved_at,
        acquisition_status=ProcessingStatus.NEEDS_REVIEW,
        access_status=AccessStatus.ACCESSIBLE,
        warnings=[warning],
    )
    document = ResearchDocument(
        document_id=document_id,
        source_id=source_id,
        document_type=DocumentType.DOCUMENT,
        title="问卷 PDF 材料",
        filename="questionnaire-material.pdf",
        mime_type="application/pdf",
        size_bytes=len(material.content),
        content_hash=material.content_hash,
        retrieved_at=retrieved_at,
        snapshot_policy=SnapshotPolicy.FULL_COPY,
        parse_status=ProcessingStatus.NEEDS_REVIEW,
        source_locator=document_locator,
        warnings=[warning],
    )
    asset = ResearchAsset(
        asset_id=asset_id,
        document_id=document_id,
        media_type=MediaType.DOCUMENT,
        mime_type="application/pdf",
        filename="questionnaire-material.pdf",
        display_name="问卷 PDF 材料",
        size_bytes=len(material.content),
        content_hash=material.content_hash,
        provider=Provider.LOCAL_UPLOAD,
        access_status=AccessStatus.ACCESSIBLE,
        processing_status=ProcessingStatus.NEEDS_REVIEW,
        sensitivity_status=SensitivityStatus.UNKNOWN,
        export_policy=ExportPolicy.MANUAL_CONFIRMATION,
        source_locator=asset_locator,
        warnings=[warning],
    )
    reference = AssetReference(
        reference_id=_stable_id("aref", asset_id, document_id),
        asset_id=asset_id,
        context_type=AssetContextType.RESEARCH_DOCUMENT,
        context_id=document_id,
        role=AssetRole.RESEARCHER_MATERIAL,
        source_locator=asset_locator,
        binding_status=BindingStatus.NEEDS_REVIEW,
        binding_confidence=0.0,
        warnings=[warning],
    )
    snapshot = QuestionnaireSnapshot(
        snapshot_id=snapshot_id,
        document_id=document_id,
        provider=Provider.LOCAL_UPLOAD,
        source_mode=QuestionnaireSourceMode.MATERIAL_UPLOAD,
        title="问卷 PDF 材料",
        retrieved_at=retrieved_at,
        content_hash=identity_hash,
        collection_state=CollectionState.UNKNOWN,
        item_count=0,
        question_count=0,
        asset_count=1,
        mapping_status=MappingStatus.NEEDS_REVIEW,
        provider_raw_definition={
            "format": "questionnaire_pdf_material",
            "trust_level": QuestionnaireMaterialTrustLevel.MEDIUM.value,
            "mime_type": "application/pdf",
            "size_bytes": len(material.content),
            "page_count": material.page_count,
        },
        warnings=[warning],
    )
    collection = ResearchAssetCollection(
        collection_id=collection_id,
        owner_ref=owner_ref,
        sources=[source],
        documents=[document],
        assets=[asset],
        references=[reference],
    )
    attempt = QuestionnaireSourceAttempt(
        source_id=source_id,
        source_mode=QuestionnaireSourceMode.MATERIAL_UPLOAD,
        priority=questionnaire_source_priority(
            QuestionnaireSourceMode.MATERIAL_UPLOAD
        ),
        acquisition_route=(
            QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD
        ),
        status=ProcessingStatus.NEEDS_REVIEW,
        snapshot_id=snapshot_id,
        warnings=[warning],
    )
    result = QuestionnaireSourceResult(
        snapshot=snapshot,
        collection=collection,
        selected_source_ids=[source_id],
        attempts=[attempt],
        partial_success=True,
    )
    return _PdfMaterialMapping(
        result=result,
        media={material.content_hash: material.content},
    )


def _summary(package: SnapshotPackage) -> QuestionnairePdfMaterialUploadSummary:
    snapshot = package.bundle.snapshot
    collection = package.bundle.collection
    document_assets = [
        asset for asset in collection.assets
        if asset.media_type == MediaType.DOCUMENT
    ]
    raw_page_count = snapshot.provider_raw_definition.get("page_count")
    if (
        snapshot.provider != Provider.LOCAL_UPLOAD
        or snapshot.source_mode != QuestionnaireSourceMode.MATERIAL_UPLOAD
        or snapshot.mapping_status != MappingStatus.NEEDS_REVIEW
        or len(collection.documents) != 1
        or len(document_assets) != 1
        or not isinstance(raw_page_count, int)
        or isinstance(raw_page_count, bool)
    ):
        raise QuestionnairePdfMaterialInternalError()
    return QuestionnairePdfMaterialUploadSummary(
        snapshot_id=snapshot.snapshot_id,
        provider=Provider.LOCAL_UPLOAD,
        source_mode=QuestionnaireSourceMode.MATERIAL_UPLOAD,
        mapping_status=MappingStatus.NEEDS_REVIEW,
        processing_status=ProcessingStatus.NEEDS_REVIEW,
        trust_level=QuestionnaireMaterialTrustLevel.MEDIUM,
        file_count=1,
        total_size_bytes=document_assets[0].size_bytes or 0,
        document_count=1,
        image_count=0,
        page_count=raw_page_count,
        requires_human_review=True,
        warning_codes=[PDF_MATERIAL_REVIEW_WARNING_CODE],
    )


def _load_existing(
    owner_ref: str,
    snapshot_id: str,
    storage: ResearchSnapshotStorage,
) -> SnapshotPackage | None:
    try:
        return load_questionnaire_source_snapshot(
            owner_ref,
            snapshot_id,
            storage,
        )
    except Exception as error:
        raise QuestionnairePdfMaterialInternalError() from error


def _validated_existing_summary(
    existing: object,
    *,
    owner_ref: str,
    material: _ValidatedPdfMaterial,
    expected_snapshot_id: str,
) -> QuestionnairePdfMaterialUploadSummary:
    if not isinstance(existing, SnapshotPackage):
        raise QuestionnairePdfMaterialInternalError()
    try:
        _validate_package_without_archive(
            existing,
            owner_ref,
            expected_snapshot_id,
        )
    except Exception as error:
        raise QuestionnairePdfMaterialInternalError() from error
    remapped = _build_mapping(
        owner_ref,
        material,
        existing.bundle.snapshot.retrieved_at,
    )
    if remapped.package != existing:
        raise QuestionnairePdfMaterialConflictError()
    return _summary(existing)


def _import_pdf(
    owner_ref: str,
    material: QuestionnairePdfMaterial,
    clock: Callable[[], datetime],
    storage: ResearchSnapshotStorage,
) -> QuestionnairePdfMaterialUploadSummary:
    try:
        validated = _validated_material(material)
    except _PdfValidationError as error:
        raise QuestionnairePdfMaterialInvalidError() from error
    retrieved_at = _retrieved_at(clock)
    try:
        mapped = _build_mapping(owner_ref, validated, retrieved_at)
    except QuestionnairePdfMaterialSnapshotApiError:
        raise
    except Exception as error:
        raise QuestionnairePdfMaterialInternalError() from error
    snapshot_id = mapped.result.snapshot.snapshot_id
    existing = _load_existing(owner_ref, snapshot_id, storage)
    if existing is not None:
        return _validated_existing_summary(
            existing,
            owner_ref=owner_ref,
            material=validated,
            expected_snapshot_id=snapshot_id,
        )
    try:
        package = save_questionnaire_source_snapshot(
            mapped.result,
            mapped.media,
            storage,
        )
    except SnapshotConflictError:
        raced = _load_existing(owner_ref, snapshot_id, storage)
        if raced is None:
            raise QuestionnairePdfMaterialInternalError()
        return _validated_existing_summary(
            raced,
            owner_ref=owner_ref,
            material=validated,
            expected_snapshot_id=snapshot_id,
        )
    except QuestionnairePdfMaterialSnapshotApiError:
        raise
    except Exception as error:
        raise QuestionnairePdfMaterialInternalError() from error
    return _summary(package)


async def _await_uncancelled(
    task: asyncio.Task[QuestionnairePdfMaterialUploadSummary],
) -> None:
    """外层取消后等待线程内原子保存结束，再传播取消。"""

    current = asyncio.current_task()
    while not task.done():
        if current is not None and hasattr(current, "uncancel"):
            current.uncancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    try:
        if not task.cancelled():
            task.exception()
    except BaseException:
        pass


@dataclass(frozen=True, slots=True)
class QuestionnairePdfMaterialSnapshotApi:
    """严格预检并原子保存 owner-scoped 中可信问卷 PDF。"""

    storage: ResearchSnapshotStorage
    clock: Callable[[], datetime] = field(default=_utc_now, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.storage, ResearchSnapshotStorage):
            raise TypeError("storage 必须实现 ResearchSnapshotStorage")
        if not callable(self.clock):
            raise TypeError("clock 必须可调用")

    async def import_pdf(
        self,
        owner_ref: str,
        material: QuestionnairePdfMaterial,
    ) -> QuestionnairePdfMaterialUploadSummary:
        owner = _require_owner(owner_ref)
        if not isinstance(material, QuestionnairePdfMaterial):
            raise QuestionnairePdfMaterialInvalidError()
        persist_task = asyncio.create_task(asyncio.to_thread(
            _import_pdf,
            owner,
            material,
            self.clock,
            self.storage,
        ))
        try:
            return await asyncio.shield(persist_task)
        except asyncio.CancelledError:
            await _await_uncancelled(persist_task)
            raise
        except QuestionnairePdfMaterialSnapshotApiError:
            raise
        except Exception as error:
            raise QuestionnairePdfMaterialInternalError() from error
