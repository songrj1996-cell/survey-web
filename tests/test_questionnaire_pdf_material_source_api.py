from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import multiprocessing
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException, Request
import httpx
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
    TextStringObject,
)
from starlette.datastructures import UploadFile

from app.routers import questionnaire_pdf_materials as pdf_router_module
from app.routers.questionnaire_pdf_materials import (
    create_questionnaire_pdf_material_sources_router,
)
from app.schemas.questionnaire import MappingStatus, QuestionnaireSourceMode
from app.schemas.questionnaire_source_api import (
    PDF_MATERIAL_REVIEW_WARNING_CODE,
    QuestionnaireMaterialTrustLevel,
    QuestionnairePdfMaterialUploadSummary,
)
from app.schemas.research_assets import (
    AccessStatus,
    AssetContextType,
    AssetRole,
    BindingStatus,
    DocumentType,
    ExportPolicy,
    MediaType,
    ProcessingStatus,
    Provider,
    SensitivityStatus,
    SnapshotPolicy,
    SourceKind,
)
from app.services import (
    questionnaire_pdf_material_snapshot_api as pdf_api_module,
)
from app.services.questionnaire_pdf_material_snapshot_api import (
    MAX_QUESTIONNAIRE_PDF_BYTES,
    MAX_QUESTIONNAIRE_PDF_OBJECT_DEPTH,
    MAX_QUESTIONNAIRE_PDF_OBJECTS,
    MAX_QUESTIONNAIRE_PDF_PARSER_WALL_SECONDS,
    QuestionnairePdfMaterial,
    QuestionnairePdfMaterialConflictError,
    QuestionnairePdfMaterialInternalError,
    QuestionnairePdfMaterialInvalidError,
    QuestionnairePdfMaterialSnapshotApi,
)
from app.storage.research_assets import (
    FileResearchAssetStorage,
    ResearchAssetBundle,
    SnapshotConflictError,
    SnapshotPackage,
)


LOGIN = {"email": "pdf-material-user@example.com", "name": "PDF User"}
OWNER_REF = "email:pdf-material-user@example.com"
OTHER_OWNER_REF = "email:other-pdf-material-user@example.com"
FIXED_TIME = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)
PDF_PATH = "/api/questionnaire-sources/materials/pdf/snapshots"
SUMMARY_FIELDS = {
    "schema_version",
    "snapshot_id",
    "provider",
    "source_mode",
    "mapping_status",
    "processing_status",
    "trust_level",
    "file_count",
    "total_size_bytes",
    "document_count",
    "image_count",
    "page_count",
    "requires_human_review",
    "warning_codes",
}
_PARSER_PID_PATH_ENV = "SURVEY_WEB_TEST_PDF_PARSER_PID_PATH"


def _recording_pdf_validation_worker(
    connection,
    content: bytes,
    max_pages: int,
    max_objects: int,
    max_depth: int,
) -> None:
    pid_path = os.environ[_PARSER_PID_PATH_ENV]
    Path(pid_path).write_text(str(os.getpid()), encoding="ascii")
    pdf_api_module._pdf_validation_worker(
        connection,
        content,
        max_pages,
        max_objects,
        max_depth,
    )


def _pdf(
    page_count: int,
    configure=None,
) -> bytes:
    """Build a real PDF with pypdf rather than imitating a PDF signature."""
    writer = PdfWriter()
    pages = [
        writer.add_blank_page(width=612, height=792)
        for _ in range(page_count)
    ]
    if configure is not None:
        configure(writer, pages)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _encrypted_pdf() -> bytes:
    def configure(writer: PdfWriter, pages: list) -> None:
        writer.encrypt("private-password")

    return _pdf(1, configure)


def _attachment_pdf() -> bytes:
    def configure(writer: PdfWriter, pages: list) -> None:
        writer.add_attachment("private.txt", b"embedded private bytes")

    return _pdf(1, configure)


def _indirect_launch_action_pdf() -> bytes:
    def configure(writer: PdfWriter, pages: list) -> None:
        indirect_action_type = writer._add_object(NameObject("/Launch"))
        pages[0][NameObject("/A")] = DictionaryObject({
            NameObject("/S"): indirect_action_type,
            NameObject("/F"): TextStringObject("private.bin"),
        })

    return _pdf(1, configure)


def _file_attachment_without_embedded_file_pdf() -> bytes:
    def configure(writer: PdfWriter, pages: list) -> None:
        pages[0][NameObject("/Annots")] = ArrayObject([
            DictionaryObject({
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/FileAttachment"),
                NameObject("/Rect"): RectangleObject((0, 0, 10, 10)),
                NameObject("/FS"): DictionaryObject({
                    NameObject("/F"): TextStringObject("private.txt"),
                }),
            }),
        ])

    return _pdf(1, configure)


def _link_action_pdf(action_builder) -> bytes:
    def configure(writer: PdfWriter, pages: list) -> None:
        pages[0][NameObject("/Annots")] = ArrayObject([
            DictionaryObject({
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): RectangleObject((0, 0, 10, 10)),
                NameObject("/A"): action_builder(writer, pages),
            }),
        ])

    return _pdf(1, configure)


def _thread_link_action_pdf() -> bytes:
    return _link_action_pdf(lambda writer, pages: DictionaryObject({
        NameObject("/S"): NameObject("/Thread"),
        NameObject("/F"): TextStringObject("private.pdf"),
        NameObject("/D"): NumberObject(0),
    }))


def _named_print_link_action_pdf() -> bytes:
    return _link_action_pdf(lambda writer, pages: DictionaryObject({
        NameObject("/S"): NameObject("/Named"),
        NameObject("/N"): NameObject("/Print"),
    }))


def _internal_goto_link_action_pdf() -> bytes:
    return _link_action_pdf(lambda writer, pages: DictionaryObject({
        NameObject("/S"): NameObject("/GoTo"),
        NameObject("/D"): ArrayObject([
            pages[0].indirect_reference,
            NameObject("/Fit"),
        ]),
    }))


def _aliased_reset_form_action_pdf() -> bytes:
    def configure(writer: PdfWriter, pages: list) -> None:
        shared_action = writer._add_object(DictionaryObject({
            NameObject("/S"): NameObject("/ResetForm"),
        }))
        pages[0][NameObject("/Annots")] = ArrayObject([
            DictionaryObject({
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): RectangleObject((0, 0, 10, 10)),
                NameObject("/A"): shared_action,
                # 后插键在 LIFO 栈中先遍历，先以普通上下文访问别名。
                NameObject("/ZZZ"): shared_action,
            }),
        ])

    return _pdf(1, configure)


def _root_entry_pdf(key: str, value) -> bytes:
    def configure(writer: PdfWriter, pages: list) -> None:
        writer.root_object[NameObject(key)] = value

    return _pdf(1, configure)


def _page_entry_pdf(key: str, value) -> bytes:
    def configure(writer: PdfWriter, pages: list) -> None:
        pages[0][NameObject(key)] = value

    return _pdf(1, configure)


def _strictly_broken_pdf() -> bytes:
    valid = _pdf(1)
    broken, replacements = re.subn(
        rb"(?<=startxref\n)\d+",
        b"0",
        valid,
        count=1,
    )
    if replacements != 1:
        raise AssertionError("test PDF did not contain one startxref")
    return broken


def _deep_pdf(depth: int) -> bytes:
    value = TextStringObject("safe leaf")
    for _ in range(depth):
        value = ArrayObject([value])
    return _root_entry_pdf("/SafeNestedValue", value)


def _many_object_pdf(count: int) -> bytes:
    def configure(writer: PdfWriter, pages: list) -> None:
        writer.root_object[NameObject("/SafeObjects")] = ArrayObject([
            ArrayObject()
            for _ in range(count)
        ])

    return _pdf(1, configure)


def _xref_object_pdf(count: int) -> bytes:
    def configure(writer: PdfWriter, pages: list) -> None:
        for _ in range(count):
            writer._add_object(DictionaryObject())

    return _pdf(1, configure)


ONE_PAGE_PDF = _pdf(1)
TWO_HUNDRED_PAGE_PDF = _pdf(200)
ZERO_PAGE_PDF = _pdf(0)
TWO_HUNDRED_ONE_PAGE_PDF = _pdf(201)
STRICTLY_BROKEN_PDF = _strictly_broken_pdf()
ENCRYPTED_PDF = _encrypted_pdf()
ATTACHMENT_PDF = _attachment_pdf()
INDIRECT_LAUNCH_ACTION_PDF = _indirect_launch_action_pdf()
FILE_ATTACHMENT_WITHOUT_EMBEDDED_FILE_PDF = (
    _file_attachment_without_embedded_file_pdf()
)
THREAD_LINK_ACTION_PDF = _thread_link_action_pdf()
NAMED_PRINT_LINK_ACTION_PDF = _named_print_link_action_pdf()
INTERNAL_GOTO_LINK_ACTION_PDF = _internal_goto_link_action_pdf()
ALIASED_RESET_FORM_ACTION_PDF = _aliased_reset_form_action_pdf()
DANGEROUS_PDFS = (
    (
        "javascript",
        _root_entry_pdf(
            "/Names",
            DictionaryObject({
                NameObject("/JavaScript"): DictionaryObject(),
            }),
        ),
    ),
    (
        "open_action",
        _root_entry_pdf(
            "/OpenAction",
            DictionaryObject({NameObject("/S"): NameObject("/GoTo")}),
        ),
    ),
    (
        "additional_actions",
        _root_entry_pdf(
            "/AA",
            DictionaryObject({
                NameObject("/O"): DictionaryObject({
                    NameObject("/S"): NameObject("/GoTo"),
                }),
            }),
        ),
    ),
    (
        "launch",
        _page_entry_pdf(
            "/A",
            DictionaryObject({
                NameObject("/S"): NameObject("/Launch"),
                NameObject("/F"): TextStringObject("private.bin"),
            }),
        ),
    ),
    (
        "rich_media",
        _page_entry_pdf(
            "/Annots",
            ArrayObject([DictionaryObject({
                NameObject("/Subtype"): NameObject("/RichMedia"),
            })]),
        ),
    ),
    (
        "xfa",
        _root_entry_pdf(
            "/AcroForm",
            DictionaryObject({
                NameObject("/XFA"): TextStringObject("private XFA"),
            }),
        ),
    ),
    (
        "uri",
        _page_entry_pdf(
            "/A",
            DictionaryObject({
                NameObject("/S"): NameObject("/URI"),
                NameObject("/URI"): TextStringObject(
                    "https://private.example.invalid",
                ),
            }),
        ),
    ),
)


def _material(
    content: bytes = ONE_PAGE_PDF,
    *,
    filename: str = "private-questionnaire.pdf",
    mime_type: str = "application/pdf",
) -> QuestionnairePdfMaterial:
    return QuestionnairePdfMaterial(filename, mime_type, content)


def _safe_summary() -> QuestionnairePdfMaterialUploadSummary:
    return QuestionnairePdfMaterialUploadSummary(
        snapshot_id="qsn_pdf_material_test",
        total_size_bytes=len(ONE_PAGE_PDF),
        page_count=1,
        warning_codes=[PDF_MATERIAL_REVIEW_WARNING_CODE],
    )


class _SequenceClock:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            value = FIXED_TIME + timedelta(seconds=self.calls)
            self.calls += 1
        return value


class _CorruptStorage:
    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        raise RuntimeError("private /storage/pdf-material-corrupt")

    def save_snapshot_package(self, owner_ref: str, package) -> None:
        raise AssertionError("corrupt load must fail before save")


class _RacingStorage:
    def __init__(self) -> None:
        self.package: SnapshotPackage | None = None
        self.load_calls = 0

    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        self.load_calls += 1
        return self.package

    def save_snapshot_package(self, owner_ref: str, package) -> None:
        self.package = package
        raise SnapshotConflictError("simulated winning writer")


class _FixedStorage:
    def __init__(self, package: SnapshotPackage) -> None:
        self.package = package

    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        return self.package

    def save_snapshot_package(self, owner_ref: str, package) -> None:
        raise AssertionError("existing snapshot must be checked before save")


class _BlockingStorage:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.finish = threading.Event()
        self.worker_threads: list[int] = []
        self.package: SnapshotPackage | None = None

    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        self.worker_threads.append(threading.get_ident())
        self.started.set()
        if not self.finish.wait(timeout=2):
            raise AssertionError("test did not release PDF worker")
        return None

    def save_snapshot_package(self, owner_ref: str, package) -> None:
        self.package = package


class _TimeoutConnection:
    def __init__(self, *, polls: bool) -> None:
        self.polls = polls
        self.poll_timeouts: list[float] = []
        self.close_calls = 0

    def poll(self, timeout: float) -> bool:
        self.poll_timeouts.append(timeout)
        return self.polls

    def recv(self):
        raise AssertionError("timed-out parser connection must not recv")

    def close(self) -> None:
        self.close_calls += 1


class _TimeoutProcess:
    def __init__(self) -> None:
        self.pid: int | None = None
        self.alive = False
        self.start_calls = 0
        self.join_timeouts: list[float] = []
        self.terminate_calls = 0
        self.kill_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        self.pid = 424_242
        self.alive = True

    def join(self, timeout: float) -> None:
        self.join_timeouts.append(timeout)

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.alive = False

    def kill(self) -> None:
        self.kill_calls += 1
        self.alive = False


class _TimeoutMultiprocessingContext:
    def __init__(self) -> None:
        self.parent_connection = _TimeoutConnection(polls=False)
        self.child_connection = _TimeoutConnection(polls=False)
        self.process = _TimeoutProcess()
        self.process_target = None
        self.process_args = None
        self.process_daemon = None

    def Pipe(self, *, duplex: bool):
        if duplex:
            raise AssertionError("PDF parser pipe must be one-way")
        return self.parent_connection, self.child_connection

    def Process(self, *, target, args, daemon: bool):
        self.process_target = target
        self.process_args = args
        self.process_daemon = daemon
        return self.process


class _ChunkedReceive:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.calls = 0

    async def __call__(self) -> dict:
        index = self.calls
        self.calls += 1
        if index >= len(self.chunks):
            return {"type": "http.disconnect"}
        return {
            "type": "http.request",
            "body": self.chunks[index],
            "more_body": index < len(self.chunks) - 1,
        }


class _DisconnectingReceive:
    def __init__(self, first_chunk: bytes) -> None:
        self.first_chunk = first_chunk
        self.calls = 0

    async def __call__(self) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {
                "type": "http.request",
                "body": self.first_chunk,
                "more_body": True,
            }
        return {"type": "http.disconnect"}


class _StallingReceive:
    def __init__(self, first_chunk: bytes) -> None:
        self.first_chunk = first_chunk
        self.calls = 0
        self.never = asyncio.Event()

    async def __call__(self) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {
                "type": "http.request",
                "body": self.first_chunk,
                "more_body": True,
            }
        await self.never.wait()
        raise AssertionError("unreachable")


def _multipart_body(
    files: list[tuple[str, str, bytes, str]],
    *,
    fields: list[tuple[str, str]] | None = None,
) -> tuple[bytes, bytes]:
    boundary = b"questionnaire-pdf-material-test"
    parts: list[bytes] = []
    for name, filename, content, mime_type in files:
        parts.extend((
            b"--" + boundary + b"\r\n",
            b'Content-Disposition: form-data; name="'
            + name.encode("ascii")
            + b'"; filename="'
            + filename.encode("ascii")
            + b'"\r\n',
            b"Content-Type: " + mime_type.encode("ascii") + b"\r\n\r\n",
            content,
            b"\r\n",
        ))
    for name, value in fields or []:
        parts.extend((
            b"--" + boundary + b"\r\n",
            b'Content-Disposition: form-data; name="'
            + name.encode("ascii")
            + b'"\r\n\r\n',
            value.encode("utf-8"),
            b"\r\n",
        ))
    parts.append(b"--" + boundary + b"--\r\n")
    return b"".join(parts), b"multipart/form-data; boundary=" + boundary


async def _call_asgi(
    app: FastAPI,
    receive,
    content_type: bytes,
) -> tuple[int, bytes]:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": PDF_PATH,
            "raw_path": PDF_PATH.encode("ascii"),
            "query_string": b"",
            "headers": [(b"content-type", content_type)],
            "client": ("test", 1),
            "server": ("test", 80),
        },
        receive,
        send,
    )
    status = next(
        message["status"]
        for message in sent
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return status, body


class QuestionnairePdfMaterialSourceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="questionnaire-pdf-material-source-api-test-",
        )
        self.storage = FileResearchAssetStorage(self.temporary.name)
        self.clock = _SequenceClock()
        self.api = QuestionnairePdfMaterialSnapshotApi(self.storage, self.clock)
        self.router = create_questionnaire_pdf_material_sources_router(self.api)
        self.app = FastAPI()
        self.app.include_router(self.router)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def _request(self, **kwargs) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with patch(
                "app.routers.questionnaire_pdf_materials._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ) as require_feature:
                response = await client.post(PDF_PATH, **kwargs)
        require_feature.assert_awaited_once()
        self.assertEqual(require_feature.await_args.args[1], "survey")
        return response

    def _endpoint(self):
        return next(route.endpoint for route in self.router.routes)

    @staticmethod
    def _request_object(receive, content_type: bytes) -> Request:
        return Request(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": PDF_PATH,
                "raw_path": PDF_PATH.encode("ascii"),
                "query_string": b"",
                "headers": [(b"content-type", content_type)],
                "client": ("test", 1),
                "server": ("test", 80),
            },
            receive=receive,
        )

    async def test_service_persists_medium_trust_document_with_full_pdf_bytes(self):
        material = _material()
        summary = await self.api.import_pdf(OWNER_REF, material)
        package = self.storage.load_snapshot_package(
            OWNER_REF,
            summary.snapshot_id,
        )

        self.assertIsNotNone(package)
        assert package is not None
        snapshot = package.bundle.snapshot
        collection = package.bundle.collection
        self.assertEqual(summary.provider, Provider.LOCAL_UPLOAD)
        self.assertEqual(
            summary.source_mode,
            QuestionnaireSourceMode.MATERIAL_UPLOAD,
        )
        self.assertEqual(summary.mapping_status, MappingStatus.NEEDS_REVIEW)
        self.assertEqual(summary.processing_status, ProcessingStatus.NEEDS_REVIEW)
        self.assertEqual(
            summary.trust_level,
            QuestionnaireMaterialTrustLevel.MEDIUM,
        )
        self.assertEqual(summary.file_count, 1)
        self.assertEqual(summary.document_count, 1)
        self.assertEqual(summary.image_count, 0)
        self.assertEqual(summary.page_count, 1)
        self.assertEqual(summary.total_size_bytes, len(ONE_PAGE_PDF))
        self.assertTrue(summary.requires_human_review)
        self.assertEqual(
            summary.warning_codes,
            [PDF_MATERIAL_REVIEW_WARNING_CODE],
        )

        self.assertEqual(snapshot.provider, Provider.LOCAL_UPLOAD)
        self.assertEqual(snapshot.source_mode, QuestionnaireSourceMode.MATERIAL_UPLOAD)
        self.assertEqual(snapshot.mapping_status, MappingStatus.NEEDS_REVIEW)
        self.assertEqual(snapshot.item_count, 0)
        self.assertEqual(snapshot.question_count, 0)
        self.assertEqual(snapshot.asset_count, 1)
        self.assertEqual(snapshot.provider_items, [])
        self.assertEqual(snapshot.canonical_questions, [])
        self.assertEqual(snapshot.response_column_mappings, [])
        self.assertEqual(collection.owner_ref, OWNER_REF)
        self.assertEqual(len(collection.sources), 1)
        self.assertEqual(len(collection.documents), 1)
        self.assertEqual(len(collection.assets), 1)
        self.assertEqual(len(collection.references), 1)

        source = collection.sources[0]
        document = collection.documents[0]
        asset = collection.assets[0]
        reference = collection.references[0]
        self.assertEqual(source.source_kind, SourceKind.LOCAL_UPLOAD)
        self.assertEqual(source.provider, Provider.LOCAL_UPLOAD)
        self.assertEqual(source.owner_ref, OWNER_REF)
        self.assertEqual(source.acquisition_status, ProcessingStatus.NEEDS_REVIEW)
        self.assertEqual(source.access_status, AccessStatus.ACCESSIBLE)
        self.assertEqual(document.document_type, DocumentType.DOCUMENT)
        self.assertEqual(document.snapshot_policy, SnapshotPolicy.FULL_COPY)
        self.assertEqual(document.parse_status, ProcessingStatus.NEEDS_REVIEW)
        self.assertEqual(document.mime_type, "application/pdf")
        self.assertEqual(document.size_bytes, len(ONE_PAGE_PDF))
        self.assertEqual(document.content_hash, asset.content_hash)

        self.assertEqual(asset.media_type, MediaType.DOCUMENT)
        self.assertEqual(asset.mime_type, "application/pdf")
        self.assertEqual(asset.provider, Provider.LOCAL_UPLOAD)
        self.assertEqual(asset.access_status, AccessStatus.ACCESSIBLE)
        self.assertEqual(asset.processing_status, ProcessingStatus.NEEDS_REVIEW)
        self.assertEqual(asset.sensitivity_status, SensitivityStatus.UNKNOWN)
        self.assertEqual(asset.export_policy, ExportPolicy.MANUAL_CONFIRMATION)
        self.assertEqual(asset.size_bytes, len(ONE_PAGE_PDF))
        self.assertEqual(set(package.media), {asset.content_hash})
        self.assertEqual(package.media[asset.content_hash], ONE_PAGE_PDF)
        self.assertEqual(asset.source_locator.local_file_id, asset.asset_id)

        self.assertEqual(reference.asset_id, asset.asset_id)
        self.assertEqual(reference.context_type, AssetContextType.RESEARCH_DOCUMENT)
        self.assertEqual(reference.context_id, document.document_id)
        self.assertEqual(reference.role, AssetRole.RESEARCHER_MATERIAL)
        self.assertEqual(reference.binding_status, BindingStatus.NEEDS_REVIEW)
        self.assertEqual(reference.binding_confidence, 0.0)

        warned_objects = [source, document, asset, reference, snapshot]
        for value in warned_objects:
            self.assertEqual(
                [warning.code for warning in value.warnings],
                [PDF_MATERIAL_REVIEW_WARNING_CODE],
            )
            self.assertTrue(value.warnings[0].blocking)

    async def test_service_accepts_one_and_two_hundred_real_pdf_pages(self):
        for expected_pages, content in (
            (1, ONE_PAGE_PDF),
            (200, TWO_HUNDRED_PAGE_PDF),
        ):
            with self.subTest(page_count=expected_pages):
                summary = await self.api.import_pdf(
                    OWNER_REF,
                    _material(content, filename=f"pages-{expected_pages}.pdf"),
                )
                self.assertEqual(summary.page_count, expected_pages)
                package = self.storage.load_snapshot_package(
                    OWNER_REF,
                    summary.snapshot_id,
                )
                self.assertIsNotNone(package)
                assert package is not None
                asset = package.bundle.collection.assets[0]
                self.assertEqual(package.media[asset.content_hash], content)

    async def test_service_rejects_zero_and_two_hundred_one_pages(self):
        for expected_pages, content in (
            (0, ZERO_PAGE_PDF),
            (201, TWO_HUNDRED_ONE_PAGE_PDF),
        ):
            with self.subTest(page_count=expected_pages):
                with self.assertRaises(QuestionnairePdfMaterialInvalidError):
                    await self.api.import_pdf(OWNER_REF, _material(content))

    async def test_service_rejects_name_mime_extension_signature_empty_and_size(self):
        cases = (
            ("path", _material(filename="../private.pdf")),
            ("mime", _material(mime_type="application/octet-stream")),
            ("extension", _material(filename="private.txt")),
            ("empty", _material(b"")),
            ("signature", _material(b"not a PDF")),
        )
        for label, material in cases:
            with self.subTest(label=label):
                with self.assertRaises(QuestionnairePdfMaterialInvalidError):
                    await self.api.import_pdf(OWNER_REF, material)

        with (
            patch.object(
                pdf_api_module,
                "MAX_QUESTIONNAIRE_PDF_BYTES",
                len(ONE_PAGE_PDF) - 1,
            ),
            self.assertRaises(QuestionnairePdfMaterialInvalidError),
        ):
            await self.api.import_pdf(OWNER_REF, _material())

    async def test_service_uses_strict_pdf_parsing_and_rejects_encryption(self):
        for label, content in (
            ("strict-xref", STRICTLY_BROKEN_PDF),
            ("encrypted", ENCRYPTED_PDF),
        ):
            with self.subTest(label=label):
                with self.assertRaises(QuestionnairePdfMaterialInvalidError):
                    await self.api.import_pdf(OWNER_REF, _material(content))

    async def test_service_parses_pdf_in_a_different_process(self):
        parent_pid = os.getpid()
        pid_path = Path(self.temporary.name) / "parser-child.pid"
        with (
            patch.dict(
                os.environ,
                {_PARSER_PID_PATH_ENV: str(pid_path)},
            ),
            patch.object(
                pdf_api_module,
                "_pdf_validation_worker",
                new=_recording_pdf_validation_worker,
            ),
        ):
            summary = await self.api.import_pdf(OWNER_REF, _material())

        child_pid = int(pid_path.read_text(encoding="ascii"))
        self.assertEqual(summary.page_count, 1)
        self.assertNotEqual(child_pid, parent_pid)
        self.assertNotIn(
            child_pid,
            {
                child.pid
                for child in multiprocessing.active_children()
            },
        )

    async def test_parser_wall_timeout_is_invalid_and_leaves_no_process(self):
        context = _TimeoutMultiprocessingContext()
        wall_seconds = 0.125
        self.assertGreater(
            MAX_QUESTIONNAIRE_PDF_PARSER_WALL_SECONDS,
            wall_seconds,
        )
        with (
            patch.object(
                pdf_api_module.multiprocessing,
                "get_context",
                return_value=context,
            ) as get_context,
            patch.object(
                pdf_api_module,
                "MAX_QUESTIONNAIRE_PDF_PARSER_WALL_SECONDS",
                wall_seconds,
            ),
            patch.object(
                self.storage,
                "save_snapshot_package",
                wraps=self.storage.save_snapshot_package,
            ) as save,
            self.assertRaises(QuestionnairePdfMaterialInvalidError),
        ):
            await self.api.import_pdf(OWNER_REF, _material())

        get_context.assert_called_once_with("spawn")
        save.assert_not_called()
        self.assertEqual(context.parent_connection.poll_timeouts, [wall_seconds])
        self.assertEqual(context.process.start_calls, 1)
        self.assertTrue(context.process_daemon)
        self.assertEqual(context.process.terminate_calls, 1)
        self.assertEqual(context.process.kill_calls, 0)
        self.assertEqual(context.process.join_timeouts, [0.2, 1.0])
        self.assertFalse(context.process.is_alive())
        self.assertGreaterEqual(context.parent_connection.close_calls, 1)
        self.assertGreaterEqual(context.child_connection.close_calls, 1)

    async def test_service_rejects_attachments_and_active_pdf_content(self):
        cases = (("attachment", ATTACHMENT_PDF), *DANGEROUS_PDFS)
        for label, content in cases:
            with self.subTest(label=label):
                with self.assertRaises(QuestionnairePdfMaterialInvalidError):
                    await self.api.import_pdf(OWNER_REF, _material(content))

    async def test_service_rejects_indirect_dangerous_action_type(self):
        with self.assertRaises(QuestionnairePdfMaterialInvalidError):
            await self.api.import_pdf(
                OWNER_REF,
                _material(INDIRECT_LAUNCH_ACTION_PDF),
            )

    async def test_service_rejects_file_attachment_without_embedded_file(self):
        with self.assertRaises(QuestionnairePdfMaterialInvalidError):
            await self.api.import_pdf(
                OWNER_REF,
                _material(FILE_ATTACHMENT_WITHOUT_EMBEDDED_FILE_PDF),
            )

    async def test_service_rejects_unsafe_link_action_contexts(self):
        for label, content in (
            ("thread", THREAD_LINK_ACTION_PDF),
            ("named-print", NAMED_PRINT_LINK_ACTION_PDF),
        ):
            with self.subTest(label=label):
                with self.assertRaises(QuestionnairePdfMaterialInvalidError):
                    await self.api.import_pdf(OWNER_REF, _material(content))

    async def test_service_accepts_internal_goto_link_action(self):
        summary = await self.api.import_pdf(
            OWNER_REF,
            _material(INTERNAL_GOTO_LINK_ACTION_PDF),
        )

        self.assertEqual(summary.page_count, 1)
        self.assertEqual(
            summary.trust_level,
            QuestionnaireMaterialTrustLevel.MEDIUM,
        )

    async def test_service_rejects_action_alias_seen_in_plain_context_first(self):
        with self.assertRaises(QuestionnairePdfMaterialInvalidError):
            await self.api.import_pdf(
                OWNER_REF,
                _material(ALIASED_RESET_FORM_ACTION_PDF),
            )

    async def test_service_enforces_xref_and_reachable_object_boundaries(self):
        self.assertGreater(MAX_QUESTIONNAIRE_PDF_OBJECTS, 64)
        cases = (
            ("xref-at-limit", _xref_object_pdf(60), True),
            ("xref-over-limit", _xref_object_pdf(61), False),
            ("graph-at-limit", _many_object_pdf(40), True),
            ("graph-over-limit", _many_object_pdf(41), False),
        )
        with patch.object(
            pdf_api_module,
            "MAX_QUESTIONNAIRE_PDF_OBJECTS",
            64,
        ):
            for label, content, accepted in cases:
                with self.subTest(label=label):
                    if accepted:
                        summary = await self.api.import_pdf(
                            OWNER_REF,
                            _material(content, filename=f"{label}.pdf"),
                        )
                        self.assertEqual(summary.page_count, 1)
                    else:
                        with self.assertRaises(
                            QuestionnairePdfMaterialInvalidError,
                        ):
                            await self.api.import_pdf(
                                OWNER_REF,
                                _material(content, filename=f"{label}.pdf"),
                            )

    async def test_service_enforces_object_depth_boundary(self):
        self.assertGreater(MAX_QUESTIONNAIRE_PDF_OBJECT_DEPTH, 8)
        with patch.object(
            pdf_api_module,
            "MAX_QUESTIONNAIRE_PDF_OBJECT_DEPTH",
            8,
        ):
            accepted = await self.api.import_pdf(
                OWNER_REF,
                _material(_deep_pdf(6), filename="depth-at-limit.pdf"),
            )
            self.assertEqual(accepted.page_count, 1)
            with self.assertRaises(QuestionnairePdfMaterialInvalidError):
                await self.api.import_pdf(
                    OWNER_REF,
                    _material(_deep_pdf(7), filename="depth-over-limit.pdf"),
                )

    async def test_repeated_upload_is_idempotent_across_clock_changes(self):
        with patch.object(
            self.storage,
            "save_snapshot_package",
            wraps=self.storage.save_snapshot_package,
        ) as save:
            first = await self.api.import_pdf(OWNER_REF, _material())
            second = await self.api.import_pdf(OWNER_REF, _material())

        self.assertEqual(first, second)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(self.clock.calls, 2)

    async def test_owner_scope_changes_identity_and_blocks_cross_owner_load(self):
        first = await self.api.import_pdf(OWNER_REF, _material())
        second = await self.api.import_pdf(OTHER_OWNER_REF, _material())

        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertIsNone(self.storage.load_snapshot_package(
            OWNER_REF,
            second.snapshot_id,
        ))
        self.assertIsNone(self.storage.load_snapshot_package(
            OTHER_OWNER_REF,
            first.snapshot_id,
        ))

    async def test_concurrent_repeated_upload_is_idempotent(self):
        first, second = await asyncio.gather(
            self.api.import_pdf(OWNER_REF, _material()),
            self.api.import_pdf(OWNER_REF, _material()),
        )

        self.assertEqual(first, second)
        self.assertIsNotNone(self.storage.load_snapshot_package(
            OWNER_REF,
            first.snapshot_id,
        ))

    async def test_storage_race_recovers_only_the_identical_winning_package(self):
        storage = _RacingStorage()
        api = QuestionnairePdfMaterialSnapshotApi(storage, lambda: FIXED_TIME)

        summary = await api.import_pdf(OWNER_REF, _material())

        self.assertIsNotNone(storage.package)
        self.assertGreaterEqual(storage.load_calls, 2)
        assert storage.package is not None
        self.assertEqual(
            summary.snapshot_id,
            storage.package.bundle.snapshot.snapshot_id,
        )

    async def test_existing_same_id_with_different_content_is_conflict(self):
        summary = await self.api.import_pdf(OWNER_REF, _material())
        package = self.storage.load_snapshot_package(
            OWNER_REF,
            summary.snapshot_id,
        )
        self.assertIsNotNone(package)
        assert package is not None
        changed_snapshot = package.bundle.snapshot.model_copy(update={
            "title": "different immutable snapshot title",
        })
        conflicting = SnapshotPackage(
            ResearchAssetBundle(
                changed_snapshot,
                package.bundle.collection,
            ),
            dict(package.media),
        )
        api = QuestionnairePdfMaterialSnapshotApi(
            _FixedStorage(conflicting),
            lambda: FIXED_TIME + timedelta(days=1),
        )

        with self.assertRaises(QuestionnairePdfMaterialConflictError):
            await api.import_pdf(OWNER_REF, _material())

    async def test_corrupt_storage_becomes_redacted_internal_error(self):
        api = QuestionnairePdfMaterialSnapshotApi(
            _CorruptStorage(),
            lambda: FIXED_TIME,
        )

        with self.assertRaises(QuestionnairePdfMaterialInternalError) as caught:
            await api.import_pdf(OWNER_REF, _material())

        self.assertNotIn(
            "/storage/pdf-material-corrupt",
            str(caught.exception),
        )

    async def test_service_offloads_work_and_waits_for_thread_after_cancel(self):
        storage = _BlockingStorage()
        api = QuestionnairePdfMaterialSnapshotApi(storage, lambda: FIXED_TIME)
        event_loop_thread = threading.get_ident()
        task = asyncio.create_task(api.import_pdf(OWNER_REF, _material()))
        await asyncio.wait_for(
            asyncio.to_thread(storage.started.wait),
            timeout=1,
        )

        task.cancel()
        before = time.monotonic()
        await asyncio.sleep(0.03)
        self.assertLess(time.monotonic() - before, 0.15)
        self.assertFalse(task.done())
        storage.finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        self.assertIsNotNone(storage.package)
        self.assertEqual(len(storage.worker_threads), 1)
        self.assertNotEqual(storage.worker_threads[0], event_loop_thread)

    async def test_http_success_is_safe_and_closes_the_unique_temp_file(self):
        temporary_files = []
        original_close = UploadFile.close

        async def track_close(upload: UploadFile) -> None:
            temporary_files.append(upload.file)
            await original_close(upload)

        with patch.object(UploadFile, "close", new=track_close):
            response = await self._request(files=[
                (
                    "file",
                    ("private-questionnaire.pdf", ONE_PAGE_PDF, "application/pdf"),
                ),
            ])

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), SUMMARY_FIELDS)
        self.assertEqual(payload["provider"], "local_upload")
        self.assertEqual(payload["source_mode"], "material_upload")
        self.assertEqual(payload["mapping_status"], "needs_review")
        self.assertEqual(payload["processing_status"], "needs_review")
        self.assertEqual(payload["trust_level"], "medium")
        self.assertEqual(payload["file_count"], 1)
        self.assertEqual(payload["document_count"], 1)
        self.assertEqual(payload["image_count"], 0)
        self.assertEqual(payload["page_count"], 1)
        self.assertTrue(payload["requires_human_review"])
        self.assertEqual(len(temporary_files), 1)
        self.assertTrue(temporary_files[0].closed)

        package = self.storage.load_snapshot_package(
            OWNER_REF,
            payload["snapshot_id"],
        )
        self.assertIsNotNone(package)
        assert package is not None
        serialized = json.dumps(payload, ensure_ascii=False)
        forbidden = [
            OWNER_REF,
            "private-questionnaire",
            self.temporary.name,
            *(asset.content_hash for asset in package.bundle.collection.assets),
        ]
        for value in forbidden:
            self.assertNotIn(value.casefold(), serialized.casefold())

    async def test_http_authentication_runs_before_body_consumption(self):
        body, content_type = _multipart_body([
            ("file", "questionnaire.pdf", ONE_PAGE_PDF, "application/pdf"),
        ])
        receive = _ChunkedReceive([body[:64], body[64:]])

        async def deny(request, feature: str):
            self.assertEqual(feature, "survey")
            self.assertEqual(receive.calls, 0)
            raise HTTPException(status_code=403, detail="denied")

        with patch(
            "app.routers.questionnaire_pdf_materials._require_feature",
            new=deny,
        ):
            status, payload = await _call_asgi(
                self.app,
                receive,
                content_type,
            )

        self.assertEqual(status, 403)
        self.assertEqual(json.loads(payload), {"detail": "denied"})
        self.assertEqual(receive.calls, 0)

    async def test_http_requires_exactly_one_file_field(self):
        boundary = "empty-pdf-material-source-test"
        empty = await self._request(
            content=f"--{boundary}--\r\n".encode("ascii"),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        unknown = await self._request(files=[
            (
                "unexpected",
                ("questionnaire.pdf", ONE_PAGE_PDF, "application/pdf"),
            ),
        ])
        text = await self._request(
            files=[
                (
                    "file",
                    ("questionnaire.pdf", ONE_PAGE_PDF, "application/pdf"),
                ),
            ],
            data={"caption": "private caption"},
        )
        two = await self._request(files=[
            ("file", ("one.pdf", ONE_PAGE_PDF, "application/pdf")),
            ("file", ("two.pdf", ONE_PAGE_PDF, "application/pdf")),
        ])

        for label, response in (
            ("empty", empty),
            ("unknown", unknown),
            ("text", text),
            ("two", two),
        ):
            with self.subTest(label=label):
                self.assertEqual(response.status_code, 422)

    async def test_http_rejects_extension_mime_signature_and_empty_pdf(self):
        cases = (
            (
                "extension",
                [("file", ("questionnaire.txt", ONE_PAGE_PDF, "application/pdf"))],
                422,
            ),
            (
                "mime",
                [(
                    "file",
                    (
                        "questionnaire.pdf",
                        ONE_PAGE_PDF,
                        "application/octet-stream",
                    ),
                )],
                415,
            ),
            (
                "signature",
                [("file", ("questionnaire.pdf", b"not a PDF", "application/pdf"))],
                422,
            ),
            (
                "empty",
                [("file", ("questionnaire.pdf", b"", "application/pdf"))],
                422,
            ),
        )
        for label, files, expected_status in cases:
            with self.subTest(label=label):
                response = await self._request(files=files)
                self.assertEqual(response.status_code, expected_status)

    async def test_http_enforces_pdf_byte_limit(self):
        with patch.object(
            pdf_router_module,
            "MAX_QUESTIONNAIRE_PDF_BYTES",
            len(ONE_PAGE_PDF) - 1,
        ):
            response = await self._request(files=[
                (
                    "file",
                    ("questionnaire.pdf", ONE_PAGE_PDF, "application/pdf"),
                ),
            ])

        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response.json(),
            {"detail": "问卷 PDF 材料超过上传大小限制"},
        )

    async def test_slow_upload_times_out_and_releases_upload_gate(self):
        body, content_type = _multipart_body([
            ("file", "questionnaire.pdf", ONE_PAGE_PDF, "application/pdf"),
        ])
        header_end = body.index(b"\r\n\r\n") + 4
        receive = _StallingReceive(body[:header_end + 16])
        temporary_files = []
        original_spooled_file = tempfile.SpooledTemporaryFile

        def tracked_spooled_file(*args, **kwargs):
            file = original_spooled_file(*args, **kwargs)
            temporary_files.append(file)
            return file

        with (
            patch(
                "app.routers.questionnaire_pdf_materials._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                pdf_router_module,
                "_PDF_UPLOAD_TIMEOUT_SECONDS",
                0.03,
            ),
            patch(
                "starlette.formparsers.SpooledTemporaryFile",
                new=tracked_spooled_file,
            ),
        ):
            status, payload = await _call_asgi(
                self.app,
                receive,
                content_type,
            )

        self.assertEqual(status, 408)
        self.assertEqual(
            json.loads(payload),
            {"detail": "问卷 PDF 材料上传超时，请重试"},
        )
        self.assertEqual(len(temporary_files), 1)
        self.assertTrue(temporary_files[0].closed)
        retry = await self._request(files=[
            ("file", ("retry.pdf", ONE_PAGE_PDF, "application/pdf")),
        ])
        self.assertEqual(retry.status_code, 200)

    async def test_disconnected_upload_is_safe_and_releases_upload_gate(self):
        body, content_type = _multipart_body([
            ("file", "questionnaire.pdf", ONE_PAGE_PDF, "application/pdf"),
        ])
        header_end = body.index(b"\r\n\r\n") + 4
        receive = _DisconnectingReceive(body[:header_end + 16])
        temporary_files = []
        original_spooled_file = tempfile.SpooledTemporaryFile

        def tracked_spooled_file(*args, **kwargs):
            file = original_spooled_file(*args, **kwargs)
            temporary_files.append(file)
            return file

        with (
            patch(
                "app.routers.questionnaire_pdf_materials._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch(
                "starlette.formparsers.SpooledTemporaryFile",
                new=tracked_spooled_file,
            ),
        ):
            status, payload = await _call_asgi(
                self.app,
                receive,
                content_type,
            )

        self.assertEqual(status, 400)
        self.assertEqual(
            json.loads(payload),
            {"detail": "问卷 PDF 材料上传未完整发送"},
        )
        self.assertEqual(len(temporary_files), 1)
        self.assertTrue(temporary_files[0].closed)
        retry = await self._request(files=[
            ("file", ("retry.pdf", ONE_PAGE_PDF, "application/pdf")),
        ])
        self.assertEqual(retry.status_code, 200)

    async def test_busy_import_precedes_body_and_cancel_defers_gate_release(self):
        endpoint = self._endpoint()
        expected = _safe_summary()
        first_started = asyncio.Event()
        first_finished = asyncio.Event()
        finish_first = asyncio.Event()
        calls: list[str] = []

        async def fake_import(api, owner_ref, material):
            calls.append(material.filename)
            if len(calls) == 1:
                first_started.set()
                await finish_first.wait()
                first_finished.set()
            return expected

        first_body, content_type = _multipart_body([
            ("file", "first.pdf", ONE_PAGE_PDF, "application/pdf"),
        ])
        second_body, _ = _multipart_body([
            ("file", "second.pdf", ONE_PAGE_PDF, "application/pdf"),
        ])
        first_request = self._request_object(
            _ChunkedReceive([first_body]),
            content_type,
        )
        second_receive = _ChunkedReceive([second_body])
        second_request = self._request_object(second_receive, content_type)

        with (
            patch.object(
                QuestionnairePdfMaterialSnapshotApi,
                "import_pdf",
                new=fake_import,
            ),
            patch(
                "app.routers.questionnaire_pdf_materials._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            first_task = asyncio.create_task(endpoint(first_request))
            await asyncio.wait_for(first_started.wait(), timeout=1)
            first_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first_task

            with self.assertRaises(HTTPException) as busy:
                await endpoint(second_request)
            self.assertEqual(busy.exception.status_code, 429)
            self.assertEqual(second_receive.calls, 0)
            self.assertEqual(calls, ["first.pdf"])

            finish_first.set()
            await asyncio.wait_for(first_finished.wait(), timeout=1)
            await asyncio.sleep(0)
            retry = await endpoint(self._request_object(
                _ChunkedReceive([second_body]),
                content_type,
            ))

        self.assertEqual(calls, ["first.pdf", "second.pdf"])
        self.assertEqual(retry, expected)

    async def test_import_timeout_holds_gate_until_cancelled_task_finishes(self):
        endpoint = self._endpoint()
        expected = _safe_summary()
        cancelled = asyncio.Event()
        finish = asyncio.Event()
        finished = asyncio.Event()
        calls = 0

        async def fake_import(api, owner_ref, material):
            nonlocal calls
            calls += 1
            if calls == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await finish.wait()
                    finished.set()
            return expected

        first_body, content_type = _multipart_body([
            ("file", "first.pdf", ONE_PAGE_PDF, "application/pdf"),
        ])
        second_body, _ = _multipart_body([
            ("file", "second.pdf", ONE_PAGE_PDF, "application/pdf"),
        ])
        second_receive = _ChunkedReceive([second_body])
        with (
            patch.object(
                QuestionnairePdfMaterialSnapshotApi,
                "import_pdf",
                new=fake_import,
            ),
            patch(
                "app.routers.questionnaire_pdf_materials._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                pdf_router_module,
                "_PDF_IMPORT_TIMEOUT_SECONDS",
                0.03,
            ),
        ):
            with self.assertRaises(HTTPException) as timed_out:
                await endpoint(self._request_object(
                    _ChunkedReceive([first_body]),
                    content_type,
                ))
            self.assertEqual(timed_out.exception.status_code, 504)
            await asyncio.wait_for(cancelled.wait(), timeout=1)

            with self.assertRaises(HTTPException) as busy:
                await endpoint(self._request_object(
                    second_receive,
                    content_type,
                ))
            self.assertEqual(busy.exception.status_code, 429)
            self.assertEqual(second_receive.calls, 0)

            finish.set()
            await asyncio.wait_for(finished.wait(), timeout=1)
            await asyncio.sleep(0)
            retry = await endpoint(self._request_object(
                _ChunkedReceive([second_body]),
                content_type,
            ))

        self.assertEqual(calls, 2)
        self.assertEqual(retry, expected)

    async def test_http_errors_are_stable_and_redacted(self):
        cases = (
            (
                QuestionnairePdfMaterialInvalidError(
                    "private invalid filename",
                ),
                422,
                "问卷 PDF 材料无效或不受支持",
            ),
            (
                QuestionnairePdfMaterialConflictError(
                    "private conflicting hash",
                ),
                409,
                "同一 PDF 材料快照 ID 已存在不同内容",
            ),
            (
                QuestionnairePdfMaterialInternalError(
                    "private /storage/path",
                ),
                500,
                "问卷 PDF 材料导入暂时不可用",
            ),
            (
                RuntimeError("private token=pdf-material-secret"),
                500,
                "问卷 PDF 材料导入暂时不可用",
            ),
        )
        for error, expected_status, expected_detail in cases:
            with self.subTest(error=type(error).__name__):
                async def fail(*args, current_error=error, **kwargs):
                    raise current_error

                with patch.object(
                    QuestionnairePdfMaterialSnapshotApi,
                    "import_pdf",
                    new=fail,
                ):
                    response = await self._request(files=[
                        (
                            "file",
                            (
                                "questionnaire.pdf",
                                ONE_PAGE_PDF,
                                "application/pdf",
                            ),
                        ),
                    ])
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.json(), {"detail": expected_detail})
                self.assertNotIn(str(error), response.text)


if __name__ == "__main__":
    unittest.main()
