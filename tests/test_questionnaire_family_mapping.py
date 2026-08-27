from __future__ import annotations

from datetime import datetime, timezone
import tempfile
from pathlib import Path
import unittest

from app.schemas.questionnaire import (
    CanonicalOption,
    CanonicalQuestion,
    CanonicalQuestionType,
    CanonicalRow,
    CollectionState,
    MappingStatus,
    QuestionnaireSnapshot,
    QuestionnaireSourceMode,
)
from app.schemas.questionnaire_families import (
    FamilyQuestionRole,
    QuestionnaireFamilyStatus,
    family_summary,
)
from app.schemas.research_assets import Provider
from app.services.questionnaire_family_mapping import (
    FamilyVariantSnapshot,
    SemanticQuestionText,
    build_questionnaire_family,
    questionnaire_family_id,
    questionnaire_family_variant_id,
)
from app.storage.questionnaire_families import FileQuestionnaireFamilyStorage


OWNER = "owner-synthetic@example.test"
TITLE = "Multilingual study"
NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _question(
    question_id: str,
    provider_id: str | None,
    title: str,
    question_type: CanonicalQuestionType,
    *,
    options: tuple[str, ...] = (),
    rows: tuple[tuple[str, str], ...] = (),
) -> CanonicalQuestion:
    return CanonicalQuestion(
        question_id=question_id,
        provider_question_id=provider_id,
        canonical_type=question_type,
        title=title,
        required=False,
        options=[
            CanonicalOption(option_key=f"option-{index}", value=value)
            for index, value in enumerate(options)
        ],
        rows=[
            CanonicalRow(
                row_key=f"row-{index}",
                label=label,
                provider_question_id=row_provider_id,
            )
            for index, (row_provider_id, label) in enumerate(rows)
        ],
        mapping_status=MappingStatus.EXACT,
        mapping_confidence=1.0,
    )


def snapshot(
    form_id: str,
    language: str,
    *,
    include_discord: bool = False,
    omit_open: bool = False,
    choice_type: CanonicalQuestionType = CanonicalQuestionType.SINGLE_CHOICE,
    reorder: bool = False,
) -> QuestionnaireSnapshot:
    prefix = language
    questions = [
        _question(
            f"{prefix}-choice",
            f"{prefix}-provider-choice",
            "Preferred mode" if language == "en" else "Mode favorit",
            choice_type,
            options=(("Ranked", "Classic") if language == "en" else ("Peringkat", "Klasik")),
        ),
        _question(
            f"{prefix}-open",
            f"{prefix}-provider-open",
            "Why?" if language == "en" else "Mengapa?",
            CanonicalQuestionType.OPEN_TEXT,
        ),
        _question(
            f"{prefix}-matrix",
            None,
            "Rate features" if language == "en" else "Nilai fitur",
            CanonicalQuestionType.MATRIX_SINGLE,
            options=(("Good", "Bad") if language == "en" else ("Baik", "Buruk")),
            rows=(
                (f"{prefix}-row-a", "Speed" if language == "en" else "Kecepatan"),
                (f"{prefix}-row-b", "Stability" if language == "en" else "Stabilitas"),
            ),
        ),
    ]
    if omit_open:
        questions = [item for item in questions if item.canonical_type != CanonicalQuestionType.OPEN_TEXT]
    if reorder:
        questions = [questions[2], questions[0], questions[1]]
    if include_discord:
        questions.append(_question(
            f"{prefix}-discord",
            f"{prefix}-provider-discord",
            "Discord ID",
            CanonicalQuestionType.OPEN_TEXT,
        ))
    return QuestionnaireSnapshot(
        snapshot_id=f"snapshot-{language}",
        document_id=f"document-{language}",
        provider=Provider.GOOGLE_FORMS,
        provider_form_id=form_id,
        source_mode=QuestionnaireSourceMode.OFFICIAL_API,
        title=f"Form {language}",
        retrieved_at=NOW,
        content_hash=("a" if language == "en" else "b") * 64,
        collection_state=CollectionState.OPEN,
        item_count=0,
        question_count=len(questions),
        asset_count=0,
        mapping_status=MappingStatus.EXACT,
        canonical_questions=questions,
    )


def semantics(
    variants: list[tuple[str, str, QuestionnaireSnapshot]],
) -> dict[tuple[str, str], SemanticQuestionText]:
    family_id = questionnaire_family_id(
        OWNER,
        TITLE,
        [snapshot.provider_form_id or "" for _, _, snapshot in variants],
    )
    result = {}
    for language, form_id, source in variants:
        variant_id = questionnaire_family_variant_id(family_id, language, form_id)
        for question in source.canonical_questions:
            title = {
                CanonicalQuestionType.SINGLE_CHOICE: "偏好模式",
                CanonicalQuestionType.MULTI_CHOICE: "偏好模式",
                CanonicalQuestionType.OPEN_TEXT: (
                    "Discord ID" if "discord" in question.question_id else "原因"
                ),
                CanonicalQuestionType.MATRIX_SINGLE: "功能评价",
            }[question.canonical_type]
            option_labels = (
                ("排位", "经典")
                if "choice" in question.question_id
                else (("好", "差") if question.options else ())
            )
            row_labels = ("速度", "稳定性") if question.rows else ()
            result[(variant_id, question.question_id)] = SemanticQuestionText(
                title=title,
                options=option_labels,
                rows=row_labels,
            )
    return result


class QuestionnaireFamilyMappingTests(unittest.TestCase):
    def test_30_29_forms_with_only_discord_gap_are_ready_without_review(self):
        en_questions = [
            _question(
                f"en-core-{index}",
                f"en-provider-core-{index}",
                f"English core question {index}",
                CanonicalQuestionType.OPEN_TEXT,
            )
            for index in range(1, 30)
        ]
        en_questions.append(_question(
            "en-discord",
            "en-provider-discord",
            "Discord ID",
            CanonicalQuestionType.OPEN_TEXT,
        ))
        id_questions = [
            _question(
                f"id-core-{index}",
                f"id-provider-core-{index}",
                f"Pertanyaan inti {index}",
                CanonicalQuestionType.OPEN_TEXT,
            )
            for index in range(1, 30)
        ]
        en = snapshot("FORM_EN", "en").model_copy(update={
            "canonical_questions": en_questions,
            "question_count": 30,
        })
        id_form = snapshot("FORM_ID", "id").model_copy(update={
            "canonical_questions": id_questions,
            "question_count": 29,
        })
        family_id = questionnaire_family_id(OWNER, TITLE, ["FORM_EN", "FORM_ID"])
        en_variant = questionnaire_family_variant_id(family_id, "en", "FORM_EN")
        id_variant = questionnaire_family_variant_id(family_id, "id", "FORM_ID")
        translated = {
            **{
                (en_variant, f"en-core-{index}"): SemanticQuestionText(
                    title=f"规范核心题 {index}"
                )
                for index in range(1, 30)
            },
            **{
                (id_variant, f"id-core-{index}"): SemanticQuestionText(
                    title=f"规范核心题 {index}"
                )
                for index in range(1, 30)
            },
            (en_variant, "en-discord"): SemanticQuestionText(title="Discord ID"),
        }

        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[
                FamilyVariantSnapshot(language="en", snapshot=en),
                FamilyVariantSnapshot(language="id", snapshot=id_form),
            ],
            semantic_questions=translated,
            now=NOW,
        )

        self.assertEqual([item.question_count for item in family.variants], [30, 29])
        self.assertEqual(family.status, QuestionnaireFamilyStatus.READY)
        self.assertEqual(
            {item.code for item in family.diagnostics},
            {"optional_metadata_missing"},
        )
        self.assertFalse(any(item.blocking for item in family.diagnostics))

    def test_structural_anchor_keeps_common_metadata_and_only_discord_is_missing(self):
        en_questions = [
            _question(
                "en-discord",
                "en-provider-discord",
                "Discord ID",
                CanonicalQuestionType.OPEN_TEXT,
            ),
            _question(
                "en-contact",
                "en-provider-contact",
                "Contact account",
                CanonicalQuestionType.OPEN_TEXT,
            ),
            _question(
                "en-choice",
                "en-provider-choice",
                "Preferred mode",
                CanonicalQuestionType.SINGLE_CHOICE,
                options=("Ranked", "Classic", "Other"),
            ),
        ]
        id_questions = [
            _question(
                "id-contact",
                "id-provider-contact",
                "Akun kontak",
                CanonicalQuestionType.OPEN_TEXT,
            ),
            _question(
                "id-choice",
                "id-provider-choice",
                "Mode favorit",
                CanonicalQuestionType.SINGLE_CHOICE,
                options=("Peringkat", "Klasik", "Lain"),
            ),
        ]
        en = snapshot("FORM_EN", "en").model_copy(update={
            "canonical_questions": en_questions,
            "question_count": len(en_questions),
        })
        id_form = snapshot("FORM_ID", "id").model_copy(update={
            "canonical_questions": id_questions,
            "question_count": len(id_questions),
        })
        family_id = questionnaire_family_id(OWNER, TITLE, ["FORM_EN", "FORM_ID"])
        en_variant = questionnaire_family_variant_id(family_id, "en", "FORM_EN")
        id_variant = questionnaire_family_variant_id(family_id, "id", "FORM_ID")
        translated = {
            (en_variant, "en-discord"): SemanticQuestionText(title="Discord ID"),
            (en_variant, "en-contact"): SemanticQuestionText(title="联系账号"),
            (en_variant, "en-choice"): SemanticQuestionText(
                title="偏好模式",
                options=("排位", "经典", "其他"),
            ),
            (id_variant, "id-contact"): SemanticQuestionText(title="电子邮箱"),
            (id_variant, "id-choice"): SemanticQuestionText(
                title="偏好模式",
                options=("排位", "经典", "其他"),
            ),
        }

        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[
                FamilyVariantSnapshot(language="en", snapshot=en),
                FamilyVariantSnapshot(language="id", snapshot=id_form),
            ],
            semantic_questions=translated,
            now=NOW,
        )

        self.assertEqual(family.status, QuestionnaireFamilyStatus.READY)
        metadata = [
            item
            for item in family.canonical_questions
            if item.role == FamilyQuestionRole.OPTIONAL_RESPONDENT_METADATA
        ]
        self.assertEqual(len(metadata), 2)
        missing = [
            item
            for item in family.diagnostics
            if item.code == "optional_metadata_missing"
        ]
        self.assertEqual(len(missing), 1)

    def test_near_identical_translations_map_deterministically_when_types_repeat(self):
        en = snapshot("FORM_EN", "en")
        id_form = snapshot("FORM_ID", "id")
        en_extra = _question(
            "en-open-extra",
            "en-provider-open-extra",
            "What could make you stop playing?",
            CanonicalQuestionType.OPEN_TEXT,
        )
        id_extra = _question(
            "id-open-extra",
            "id-provider-open-extra",
            "Apa yang membuat Anda berhenti bermain?",
            CanonicalQuestionType.OPEN_TEXT,
        )
        en = en.model_copy(update={
            "canonical_questions": [*en.canonical_questions, en_extra],
            "question_count": en.question_count + 1,
        })
        id_form = id_form.model_copy(update={
            "canonical_questions": [*id_form.canonical_questions, id_extra],
            "question_count": id_form.question_count + 1,
        })
        family_id = questionnaire_family_id(OWNER, TITLE, ["FORM_EN", "FORM_ID"])
        en_variant = questionnaire_family_variant_id(family_id, "en", "FORM_EN")
        id_variant = questionnaire_family_variant_id(family_id, "id", "FORM_ID")
        translated = semantics([
            ("en", "FORM_EN", en),
            ("id", "FORM_ID", id_form),
        ])
        translated[(en_variant, "en-open")] = SemanticQuestionText(title="购买意愿与原因")
        translated[(en_variant, "en-open-extra")] = SemanticQuestionText(title="流失原因与触发点")
        translated[(id_variant, "id-open")] = SemanticQuestionText(title="购买意愿和原因")
        translated[(id_variant, "id-open-extra")] = SemanticQuestionText(title="流失原因和触发因素")

        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[
                FamilyVariantSnapshot(language="en", snapshot=en),
                FamilyVariantSnapshot(language="id", snapshot=id_form),
            ],
            semantic_questions=translated,
            now=NOW,
        )

        self.assertEqual(family.status, QuestionnaireFamilyStatus.READY)
        open_questions = [
            item for item in family.canonical_questions
            if item.canonical_type == CanonicalQuestionType.OPEN_TEXT
        ]
        self.assertEqual(len(open_questions), 2)
        self.assertTrue(all(len(item.variant_mappings) == 2 for item in open_questions))

    def test_reordered_translations_map_and_discord_gap_is_nonblocking(self):
        en = snapshot("FORM_EN", "en", include_discord=True)
        id_form = snapshot("FORM_ID", "id", reorder=True)
        declared = [("en", "FORM_EN", en), ("id", "FORM_ID", id_form)]
        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[
                FamilyVariantSnapshot(language=language, snapshot=source)
                for language, _, source in declared
            ],
            semantic_questions=semantics(declared),
            now=NOW,
        )

        self.assertEqual(family.status, QuestionnaireFamilyStatus.READY)
        self.assertEqual(len(family.variants), 2)
        self.assertEqual(len(family.canonical_questions), 4)
        metadata = [
            item for item in family.canonical_questions
            if item.role == FamilyQuestionRole.OPTIONAL_RESPONDENT_METADATA
        ]
        self.assertEqual(len(metadata), 1)
        self.assertEqual(len(metadata[0].variant_mappings), 1)
        self.assertIn("optional_metadata_missing", {
            item.code for item in family.diagnostics
        })
        matrix = next(
            item for item in family.canonical_questions
            if item.canonical_type == CanonicalQuestionType.MATRIX_SINGLE
        )
        self.assertEqual(len(matrix.variant_mappings), 2)
        self.assertEqual(len(matrix.variant_mappings[1].row_mappings), 2)

    def test_missing_core_question_requires_review(self):
        en = snapshot("FORM_EN", "en", include_discord=True)
        id_form = snapshot("FORM_ID", "id", omit_open=True)
        declared = [("en", "FORM_EN", en), ("id", "FORM_ID", id_form)]
        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[
                FamilyVariantSnapshot(language=language, snapshot=source)
                for language, _, source in declared
            ],
            semantic_questions=semantics(declared),
            now=NOW,
        )
        self.assertEqual(family.status, QuestionnaireFamilyStatus.NEEDS_REVIEW)
        self.assertIn("core_question_missing", {
            item.code for item in family.diagnostics if item.blocking
        })

    def test_semantic_type_conflict_requires_review(self):
        en = snapshot("FORM_EN", "en")
        id_form = snapshot(
            "FORM_ID", "id", choice_type=CanonicalQuestionType.MULTI_CHOICE
        )
        declared = [("en", "FORM_EN", en), ("id", "FORM_ID", id_form)]
        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[
                FamilyVariantSnapshot(language=language, snapshot=source)
                for language, _, source in declared
            ],
            semantic_questions=semantics(declared),
            now=NOW,
        )
        self.assertEqual(family.status, QuestionnaireFamilyStatus.NEEDS_REVIEW)
        self.assertIn("core_question_type_conflict", {
            item.code for item in family.diagnostics if item.blocking
        })

    def test_ambiguous_questions_are_counted_once_instead_of_three_times(self):
        en_questions = [
            _question(
                f"en-open-{index}",
                f"en-provider-open-{index}",
                f"English question {index}",
                CanonicalQuestionType.OPEN_TEXT,
            )
            for index in range(1, 6)
        ]
        id_questions = [
            _question(
                f"id-open-{index}",
                f"id-provider-open-{index}",
                f"Pertanyaan Indonesia {index}",
                CanonicalQuestionType.OPEN_TEXT,
            )
            for index in range(1, 6)
        ]
        en = snapshot("FORM_EN", "en").model_copy(update={
            "canonical_questions": en_questions,
            "question_count": len(en_questions),
        })
        id_form = snapshot("FORM_ID", "id").model_copy(update={
            "canonical_questions": id_questions,
            "question_count": len(id_questions),
        })

        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[
                FamilyVariantSnapshot(language="en", snapshot=en),
                FamilyVariantSnapshot(language="id", snapshot=id_form),
            ],
            semantic_questions={},
            now=NOW,
        )

        blocking = [item for item in family.diagnostics if item.blocking]
        self.assertEqual(len(blocking), 5)
        self.assertEqual({item.code for item in blocking}, {"core_question_ambiguous"})
        self.assertTrue(all(item.question_title for item in blocking))
        self.assertEqual(family_summary(family).blocking_issue_count, 5)

    def test_exact_duplicate_semantics_use_relative_order_only_as_tiebreaker(self):
        en_questions = [
            _question(
                f"en-repeat-{index}",
                f"en-provider-repeat-{index}",
                f"English repeated question {index}",
                CanonicalQuestionType.OPEN_TEXT,
            )
            for index in range(3)
        ]
        id_questions = [
            _question(
                f"id-repeat-{index}",
                f"id-provider-repeat-{index}",
                f"Pertanyaan berulang {index}",
                CanonicalQuestionType.OPEN_TEXT,
            )
            for index in range(3)
        ]
        en = snapshot("FORM_EN", "en").model_copy(update={
            "canonical_questions": en_questions,
            "question_count": len(en_questions),
        })
        id_form = snapshot("FORM_ID", "id").model_copy(update={
            "canonical_questions": id_questions,
            "question_count": len(id_questions),
        })
        family_id = questionnaire_family_id(OWNER, TITLE, ["FORM_EN", "FORM_ID"])
        en_variant = questionnaire_family_variant_id(family_id, "en", "FORM_EN")
        id_variant = questionnaire_family_variant_id(family_id, "id", "FORM_ID")
        translated = {
            **{
                (en_variant, question.question_id): SemanticQuestionText(
                    title="相同题干"
                )
                for question in en_questions
            },
            **{
                (id_variant, question.question_id): SemanticQuestionText(
                    title="相同题干"
                )
                for question in id_questions
            },
        }

        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[
                FamilyVariantSnapshot(language="en", snapshot=en),
                FamilyVariantSnapshot(language="id", snapshot=id_form),
            ],
            semantic_questions=translated,
            now=NOW,
        )

        self.assertEqual(family.status, QuestionnaireFamilyStatus.READY)
        fallback = next(
            item
            for item in family.diagnostics
            if item.code == "duplicate_semantic_position_fallback"
        )
        self.assertEqual(fallback.affected_count, 2)
        self.assertTrue(
            all(len(item.variant_mappings) == 2 for item in family.canonical_questions)
        )

    def test_family_storage_round_trip_is_owner_scoped(self):
        en = snapshot("FORM_EN", "en")
        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[FamilyVariantSnapshot(language="en", snapshot=en)],
            semantic_questions={},
            now=NOW,
        )
        with tempfile.TemporaryDirectory(prefix="questionnaire-family-storage-") as temporary:
            storage = FileQuestionnaireFamilyStorage(Path(temporary))
            storage.save_family(family)
            loaded = storage.load_family(OWNER, family.family_id)
            hidden = storage.load_family("other-owner@example.test", family.family_id)
        self.assertEqual(loaded, family)
        self.assertIsNone(hidden)


if __name__ == "__main__":
    unittest.main()
