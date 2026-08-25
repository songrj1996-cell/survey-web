"""已保存问卷快照与本次回答文件的确定性绑定测试。"""

from __future__ import annotations

import hashlib
import io
import json
import unittest
import zipfile
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import openpyxl

from app.integrations.bested_questionnaire_client import (
    parse_bested_questionnaire_upload,
)
from app.schemas.questionnaire import (
    CanonicalQuestionType,
    MappingStatus,
    QuestionnaireSnapshot,
)
from app.schemas.research_assets import MediaType, ResearchAssetCollection
from app.services.questionnaire_mapping import map_bested_questionnaire_upload
from app.services.questionnaire_material_snapshot_api import _build_mapping
from app.services.questionnaire_snapshot_binding import (
    SnapshotSurveyBinding,
    bind_snapshot_to_survey_responses,
)
from app.storage.research_assets import (
    ResearchAssetBundle,
    SnapshotPackage,
    SnapshotPackageError,
)


FIXTURE = (
    Path(__file__).parent / "fixtures" / "research_assets" / "google_forms.json"
)
NOW = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)


def _workbook_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    workbook = openpyxl.Workbook()
    first = True
    for name, rows in sheets.items():
        worksheet = workbook.active if first else workbook.create_sheet()
        first = False
        worksheet.title = name
        for row in rows:
            worksheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _google_package() -> SnapshotPackage:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot = QuestionnaireSnapshot.model_validate(payload["snapshot"])
    snapshot = snapshot.model_copy(update={
        "canonical_questions": [
            question.model_copy(update={"mapping_status": MappingStatus.EXACT})
            if question.question_id == "q_upload"
            else question
            for question in snapshot.canonical_questions
        ],
    })
    collection = ResearchAssetCollection.model_validate(payload["collection"])
    media: dict[str, bytes] = {}
    assets = []
    media_index = 0
    for asset in collection.assets:
        if asset.media_type in {MediaType.IMAGE, MediaType.DOCUMENT}:
            content = f"binding-fixture-{media_index}".encode()
            media_index += 1
            digest = hashlib.sha256(content).hexdigest()
            media[digest] = content
            asset = asset.model_copy(update={
                "content_hash": digest,
                "size_bytes": len(content),
            })
        assets.append(asset)
    collection = collection.model_copy(update={"assets": assets})
    return SnapshotPackage(ResearchAssetBundle(snapshot, collection), media)


def _google_csv(*, omit: str | None = None) -> bytes:
    headers = [
        "role_id",
        "gf-q-choice",
        "gf-q-grid-usability",
        "gf-q-grid-art",
        "gf-q-open",
        "gf-q-upload",
    ]
    row = ["P01", "方案 A", "满意", "一般", "因为清晰", ""]
    if omit in headers:
        index = headers.index(omit)
        headers.pop(index)
        row.pop(index)
    return (",".join(headers) + "\n" + ",".join(row) + "\n").encode()


def _google_duplicate_title_package() -> SnapshotPackage:
    package = _google_package()
    questions = list(package.bundle.snapshot.canonical_questions)
    choice = next(q for q in questions if q.question_id == "q_concept_choice")
    questions = [
        question.model_copy(update={"title": choice.title})
        if question.question_id == "q_open"
        else question
        for question in questions
    ]
    snapshot = package.bundle.snapshot.model_copy(update={
        "canonical_questions": questions,
    })
    return SnapshotPackage(
        ResearchAssetBundle(snapshot, package.bundle.collection),
        package.media,
    )


def _bested_package() -> tuple[SnapshotPackage, bytes]:
    questionnaire = _workbook_bytes({
        "问卷内容": [
            ["题号", "题目"],
            ["Q1[多选题]", "常用模式"],
            ["选项", ""],
            ["1", "排位"],
            ["2", "经典"],
            ["Q2[矩阵单选题]", "功能评价"],
            ["选项", ""],
            ["1", "满意"],
            ["2", "一般"],
            ["矩阵行", ""],
            ["1", "易用性"],
            ["2", "稳定性"],
            ["Q3[填空题]", "补充建议"],
        ],
    })
    parsed = parse_bested_questionnaire_upload("questionnaire.xlsx", questionnaire)
    mapped = map_bested_questionnaire_upload(
        parsed,
        owner_ref="fixture-user",
        filename="questionnaire.xlsx",
        questionnaire_content=questionnaire,
        retrieved_at=NOW,
    )
    return SnapshotPackage(mapped.bundle, mapped.media), questionnaire


def _bested_response(matrix_header: str = "功能评价__稳定性") -> bytes:
    return _workbook_bytes({
        "data": [
            [
                "role_id",
                "常用模式__排位",
                "常用模式__经典",
                "功能评价__易用性",
                matrix_header,
                "补充建议",
            ],
            ["P01", "排位", "", "满意", "一般", "继续优化"],
        ],
        "code": [
            ["编码", "题目"],
            ["1", "Q1.常用模式"],
            ["2", "Q2.功能评价"],
            ["3", "Q3.补充建议"],
        ],
    })


def _rewrite_with_compression_bomb(workbook_content: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(workbook_content), "r") as source:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                target.writestr(info, source.read(info))
            target.writestr("xl/media/compression-bomb.bin", b"0" * (2 * 1024 * 1024))
    return output.getvalue()


def _rewrite_with_unsafe_member(workbook_content: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(workbook_content), "r") as source:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                target.writestr(info, source.read(info))
            target.writestr("../outside.bin", b"unsafe")
    return output.getvalue()


class QuestionnaireSnapshotBindingTests(unittest.TestCase):
    def test_google_stable_response_keys_bind_to_existing_session_contract(self):
        binding = bind_snapshot_to_survey_responses(
            _google_package(),
            owner_ref="fixture-user",
            response_filename="responses.csv",
            response_content=_google_csv(),
        )

        self.assertIsInstance(binding, SnapshotSurveyBinding)
        self.assertEqual(binding.source_type, "google")
        self.assertEqual(binding.matched_questions, 4)
        self.assertEqual(binding.rows[0], (
            "你更喜欢哪个方案？",
            "功能评价 [易用性]",
            "功能评价 [美术表现]",
            "请补充理由",
            "上传参考文件",
            "role_id",
        ))
        self.assertEqual(binding.columns_detected[1].role, "matrix_single")
        self.assertEqual(binding.columns_detected[3].role, "ignore")
        self.assertEqual(binding.columns_detected[-1].role, "id")
        self.assertTrue(all(
            item.mapping_method == "provider_response_key"
            for item in binding.response_bindings
        ))

    def test_google_declared_headers_are_controlled_fallback(self):
        package = _google_package()
        response = _workbook_bytes({
            "回答": [
                [
                    "你更喜欢哪个方案？",
                    "功能评价 [易用性]",
                    "功能评价 [美术表现]",
                    "请补充理由",
                    "上传参考文件",
                ],
                ["方案 B", "一般", "满意", "更直观", ""],
            ]
        })

        binding = bind_snapshot_to_survey_responses(
            package,
            owner_ref="fixture-user",
            response_filename="responses.xlsx",
            response_content=response,
        )

        self.assertEqual(binding.rows[1][:4], ("方案 B", "一般", "满意", "更直观"))
        self.assertTrue(all(
            "header" in item.mapping_method or "column_index" in item.mapping_method
            for item in binding.response_bindings
        ))

    def test_google_missing_matrix_row_column_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "美术表现|未找到"):
            bind_snapshot_to_survey_responses(
                _google_package(),
                owner_ref="fixture-user",
                response_filename="responses.csv",
                response_content=_google_csv(omit="gf-q-grid-art"),
            )

    def test_google_duplicate_titles_bind_with_stable_response_keys(self):
        binding = bind_snapshot_to_survey_responses(
            _google_duplicate_title_package(),
            owner_ref="fixture-user",
            response_filename="responses.csv",
            response_content=_google_csv(),
        )

        self.assertEqual(binding.matched_questions, 4)
        self.assertEqual(binding.rows[0][0], "你更喜欢哪个方案？")
        self.assertEqual(binding.rows[0][3], "你更喜欢哪个方案？ 2")
        self.assertTrue(all(
            item.mapping_method == "provider_response_key"
            for item in binding.response_bindings
        ))

    def test_google_duplicate_titles_bind_declared_export_suffixes(self):
        response = _workbook_bytes({
            "回答": [[
                "你更喜欢哪个方案？",
                "功能评价 [易用性]",
                "功能评价 [美术表现]",
                "你更喜欢哪个方案？ 2",
                "上传参考文件",
            ], ["方案 B", "一般", "满意", "更直观", ""]],
        })

        binding = bind_snapshot_to_survey_responses(
            _google_duplicate_title_package(),
            owner_ref="fixture-user",
            response_filename="responses.xlsx",
            response_content=response,
        )

        self.assertEqual(binding.rows[1][:4], ("方案 B", "一般", "满意", "更直观"))
        self.assertEqual(binding.rows[0][0], "你更喜欢哪个方案？")
        self.assertEqual(binding.rows[0][3], "你更喜欢哪个方案？ 2")
        self.assertTrue(all(
            "header" in item.mapping_method
            for item in binding.response_bindings
        ))

    def test_google_duplicate_title_wrong_export_suffix_fails_closed(self):
        response = _workbook_bytes({
            "回答": [[
                "你更喜欢哪个方案？",
                "功能评价 [易用性]",
                "功能评价 [美术表现]",
                "你更喜欢哪个方案？ 3",
                "上传参考文件",
            ], ["方案 B", "一般", "满意", "更直观", ""]],
        })

        with self.assertRaisesRegex(ValueError, "未找到题目"):
            bind_snapshot_to_survey_responses(
                _google_duplicate_title_package(),
                owner_ref="fixture-user",
                response_filename="responses.xlsx",
                response_content=response,
            )

    def test_google_normalized_title_collision_still_fails_closed(self):
        package = _google_package()
        questions = list(package.bundle.snapshot.canonical_questions)
        questions = [
            question.model_copy(update={"title": "Duplicate title"})
            if question.question_id == "q_concept_choice"
            else question.model_copy(update={"title": "duplicate TITLE"})
            if question.question_id == "q_open"
            else question
            for question in questions
        ]
        snapshot = package.bundle.snapshot.model_copy(update={
            "canonical_questions": questions,
        })
        collision = SnapshotPackage(
            ResearchAssetBundle(snapshot, package.bundle.collection),
            package.media,
        )

        with self.assertRaisesRegex(ValueError, "重复题干"):
            bind_snapshot_to_survey_responses(
                collision,
                owner_ref="fixture-user",
                response_filename="responses.csv",
                response_content=_google_csv(),
            )

    def test_one_answer_column_cannot_bind_two_questions(self):
        package = _google_package()
        mappings = list(package.bundle.snapshot.response_column_mappings)
        mappings = [
            mapping.model_copy(update={
                "bindings": [mapping.bindings[0].model_copy(update={
                    "column_header": "你更喜欢哪个方案？",
                })],
            })
            if mapping.question_id == "q_open"
            else mapping
            for mapping in mappings
        ]
        snapshot = package.bundle.snapshot.model_copy(update={
            "response_column_mappings": mappings,
        })
        ambiguous = SnapshotPackage(
            ResearchAssetBundle(snapshot, package.bundle.collection),
            package.media,
        )

        response = _workbook_bytes({
            "回答": [[
                "你更喜欢哪个方案？",
                "功能评价 [易用性]",
                "功能评价 [美术表现]",
                "上传参考文件",
            ], ["方案 A", "满意", "一般", ""]],
        })
        with self.assertRaisesRegex(ValueError, "同时绑定多个题目"):
            bind_snapshot_to_survey_responses(
                ambiguous,
                owner_ref="fixture-user",
                response_filename="responses.xlsx",
                response_content=response,
            )

    def test_partial_question_structure_fails_while_package_may_be_partial(self):
        package = _google_package()
        questions = list(package.bundle.snapshot.canonical_questions)
        questions = [
            question.model_copy(update={"mapping_status": MappingStatus.PARTIAL})
            if question.question_id == "q_open"
            else question
            for question in questions
        ]
        snapshot = package.bundle.snapshot.model_copy(update={
            "mapping_status": MappingStatus.PARTIAL,
            "canonical_questions": questions,
        })
        partial_question = SnapshotPackage(
            ResearchAssetBundle(snapshot, package.bundle.collection),
            package.media,
        )
        with self.assertRaisesRegex(ValueError, "结构映射尚未完成复核"):
            bind_snapshot_to_survey_responses(
                partial_question,
                owner_ref="fixture-user",
                response_filename="responses.csv",
                response_content=_google_csv(),
            )

        package_level_only = SnapshotPackage(
            ResearchAssetBundle(
                package.bundle.snapshot.model_copy(update={
                    "mapping_status": MappingStatus.PARTIAL,
                }),
                package.bundle.collection,
            ),
            package.media,
        )
        bound = bind_snapshot_to_survey_responses(
            package_level_only,
            owner_ref="fixture-user",
            response_filename="responses.csv",
            response_content=_google_csv(),
        )
        self.assertEqual(bound.provenance.mapping_status, "partial")

    def test_bested_uses_q_number_code_and_matrix_headers(self):
        package, _ = _bested_package()
        binding = bind_snapshot_to_survey_responses(
            package,
            owner_ref="fixture-user",
            response_filename="responses.xlsx",
            response_content=_bested_response(),
        )

        self.assertEqual(binding.source_type, "bested")
        self.assertEqual(binding.matched_questions, 3)
        self.assertEqual(binding.rows[1][0], "排位")
        self.assertEqual(binding.columns_detected[1].rows, ("易用性", "稳定性"))
        self.assertTrue(all(
            item.mapping_method.startswith("bested_code")
            for item in binding.response_bindings
        ))

    def test_bested_missing_matrix_row_fails_closed(self):
        package, _ = _bested_package()
        with self.assertRaisesRegex(ValueError, "无法完整匹配"):
            bind_snapshot_to_survey_responses(
                package,
                owner_ref="fixture-user",
                response_filename="responses.xlsx",
                response_content=_bested_response("功能评价__速度"),
            )

    def test_bested_snapshot_rejects_duplicate_normalized_response_headers(self):
        package, _ = _bested_package()
        response = _workbook_bytes({
            "data": [
                [
                    "常用模式__排位",
                    "常用模式__经典",
                    "功能评价__易用性",
                    "功能评价__稳定性",
                    "补充建议",
                    "  补充建议  ",
                ],
                ["排位", "", "满意", "一般", "第一列", "第二列"],
            ],
            "code": [
                ["1", "Q1.常用模式"],
                ["2", "Q2.功能评价"],
                ["3", "Q3.补充建议"],
            ],
        })

        with self.assertRaisesRegex(ValueError, "多个规范化同名回答列"):
            bind_snapshot_to_survey_responses(
                package,
                owner_ref="fixture-user",
                response_filename="responses.xlsx",
                response_content=response,
            )

    def test_bested_snapshot_requires_unique_exact_code_question_cover(self):
        package, _ = _bested_package()
        subset = _workbook_bytes({
            "data": [["补充建议"], ["只回答开放题"]],
            "code": [["3", "Q3.补充建议"]],
        })
        with self.assertRaisesRegex(ValueError, "缺少 Q1、Q2"):
            bind_snapshot_to_survey_responses(
                package,
                owner_ref="fixture-user",
                response_filename="responses.xlsx",
                response_content=subset,
            )

        duplicate_qids = _workbook_bytes({
            "data": [["常用模式", "补充建议"], ["排位", "建议"]],
            "code": [
                ["1", "Q1.常用模式"],
                ["3", "Q3.补充建议"],
                ["3", "Q3.补充建议"],
            ],
        })
        with self.assertRaisesRegex(ValueError, "Q 号不能重复"):
            bind_snapshot_to_survey_responses(
                package,
                owner_ref="fixture-user",
                response_filename="responses.xlsx",
                response_content=duplicate_qids,
            )

    def test_bested_binding_defensively_requires_unique_question_coverage(self):
        package, _ = _bested_package()
        duplicate_binding = {
            "question_id": package.bundle.snapshot.canonical_questions[0].question_id,
            "column_indexes": [0],
            "mapping_method": "bested_code_and_header",
            "mapping_status": "normalized",
            "confidence": 0.95,
            "warning_codes": [],
        }
        with patch(
            "app.services.questionnaire_snapshot_binding."
            "match_bested_response_workbook",
            return_value={
                "matched_questions": 3,
                "bindings": [duplicate_binding, duplicate_binding, duplicate_binding],
            },
        ):
            with self.assertRaisesRegex(ValueError, "完整且唯一覆盖"):
                bind_snapshot_to_survey_responses(
                    package,
                    owner_ref="fixture-user",
                    response_filename="responses.xlsx",
                    response_content=_bested_response(),
                )

    def test_material_only_snapshot_has_no_analyzable_structure(self):
        material = _build_mapping("fixture-user", (), NOW).package

        with self.assertRaisesRegex(ValueError, "没有可绑定的问卷结构"):
            bind_snapshot_to_survey_responses(
                material,
                owner_ref="fixture-user",
                response_filename="responses.csv",
                response_content=b"a\nb\n",
            )

    def test_owner_and_media_closure_are_revalidated(self):
        package = _google_package()
        with self.assertRaisesRegex(SnapshotPackageError, "owner_ref"):
            bind_snapshot_to_survey_responses(
                package,
                owner_ref="other-user",
                response_filename="responses.csv",
                response_content=_google_csv(),
            )
        with self.assertRaisesRegex(SnapshotPackageError, "缺少图片素材媒体"):
            bind_snapshot_to_survey_responses(
                SnapshotPackage(package.bundle, {}),
                owner_ref="fixture-user",
                response_filename="responses.csv",
                response_content=_google_csv(),
            )

    def test_package_hash_includes_media_and_safe_provenance_excludes_raw_data(self):
        package = _google_package()
        first = bind_snapshot_to_survey_responses(
            package,
            owner_ref="fixture-user",
            response_filename="responses.csv",
            response_content=_google_csv(),
        )
        old_hash = next(iter(package.media))
        replacement_content = b"different-image-content"
        replacement_hash = hashlib.sha256(replacement_content).hexdigest()
        assets = [
            asset.model_copy(update={
                "content_hash": replacement_hash,
                "size_bytes": len(replacement_content),
            })
            if asset.content_hash == old_hash
            else asset
            for asset in package.bundle.collection.assets
        ]
        collection = package.bundle.collection.model_copy(update={"assets": assets})
        media = dict(package.media)
        media.pop(old_hash)
        media[replacement_hash] = replacement_content
        changed = SnapshotPackage(
            ResearchAssetBundle(package.bundle.snapshot, collection),
            media,
        )
        second = bind_snapshot_to_survey_responses(
            changed,
            owner_ref="fixture-user",
            response_filename="responses.csv",
            response_content=_google_csv(),
        )

        self.assertNotEqual(first.package_sha256, second.package_sha256)
        safe_payload = json.dumps({
            "snapshot": first.session_snapshot_ref(),
            "bindings": first.session_response_bindings(),
        })
        for forbidden in ("fixture-user", "provider_raw_definition", "media", "path"):
            self.assertNotIn(forbidden, safe_payload)
        with self.assertRaises(FrozenInstanceError):
            first.provider = "mutated"  # type: ignore[misc]

    def test_xlsx_compression_bomb_is_rejected_before_openpyxl(self):
        dangerous = _rewrite_with_compression_bomb(_workbook_bytes({
            "回答": [["gf-q-choice"], ["方案 A"]],
        }))
        with self.assertRaisesRegex(ValueError, "压缩比|安全上限"):
            bind_snapshot_to_survey_responses(
                _google_package(),
                owner_ref="fixture-user",
                response_filename="responses.xlsx",
                response_content=dangerous,
            )

    def test_xlsx_unsafe_member_is_rejected_before_openpyxl(self):
        dangerous = _rewrite_with_unsafe_member(_workbook_bytes({
            "回答": [["gf-q-choice"], ["方案 A"]],
        }))
        with self.assertRaisesRegex(ValueError, "不安全路径"):
            bind_snapshot_to_survey_responses(
                _google_package(),
                owner_ref="fixture-user",
                response_filename="responses.xlsx",
                response_content=dangerous,
            )

    def test_csv_width_and_row_limits_fail_before_unbounded_materialization(self):
        too_wide = (",".join(f"h{index}" for index in range(1025)) + "\n").encode()
        with self.assertRaisesRegex(ValueError, "列数超过安全上限"):
            bind_snapshot_to_survey_responses(
                _google_package(),
                owner_ref="fixture-user",
                response_filename="responses.csv",
                response_content=too_wide,
            )

        too_many_rows = ("gf-q-choice\n" + "方案 A\n" * 100_000).encode()
        with self.assertRaisesRegex(ValueError, "行数超过安全上限"):
            bind_snapshot_to_survey_responses(
                _google_package(),
                owner_ref="fixture-user",
                response_filename="responses.csv",
                response_content=too_many_rows,
            )


if __name__ == "__main__":
    unittest.main()
