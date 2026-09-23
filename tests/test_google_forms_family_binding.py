from __future__ import annotations

import unittest

from app.integrations.google_forms_responses_client import (
    GoogleFileUploadAnswer,
    GoogleFormResponse,
    GoogleFormResponsesCapture,
    GoogleResponseAnswer,
)
from app.schemas.questionnaire import CanonicalQuestionType
from app.schemas.questionnaire_families import QuestionnaireFamilyStatus
from app.services.google_forms_family_binding import (
    bind_google_forms_family_responses,
)
from app.services.questionnaire_family_mapping import (
    FamilyVariantSnapshot,
    build_questionnaire_family,
)
from tests.test_questionnaire_family_mapping import (
    NOW,
    OWNER,
    TITLE,
    _question,
    semantics,
    snapshot,
)


def _response(response_id: str, prefix: str, choice: str, open_text: str):
    return GoogleFormResponse(
        response_id=response_id,
        create_time="2026-08-25T00:00:00Z",
        last_submitted_time="2026-08-25T00:01:00Z",
        respondent_email=None,
        answers=(
            GoogleResponseAnswer(
                question_id=f"{prefix}-provider-choice",
                text_values=(choice,),
            ),
            GoogleResponseAnswer(
                question_id=f"{prefix}-provider-open",
                text_values=(open_text,),
            ),
            GoogleResponseAnswer(
                question_id=f"{prefix}-row-a",
                text_values=(("Good" if prefix == "en" else "Baik"),),
            ),
            GoogleResponseAnswer(
                question_id=f"{prefix}-row-b",
                text_values=(("Bad" if prefix == "en" else "Buruk"),),
            ),
        ),
    )


class GoogleFormsFamilyBindingTests(unittest.TestCase):
    def test_provider_declared_other_exposes_only_unmatched_response_candidates(self):
        en = snapshot("FORM_EN", "en", include_other=True)
        declared = [("en", "FORM_EN", en)]
        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[FamilyVariantSnapshot(language="en", snapshot=en)],
            semantic_questions=semantics(declared),
            now=NOW,
        )
        capture = GoogleFormResponsesCapture(
            form_id="FORM_EN",
            responses=(
                _response("standard", "en", "Ranked", "Standard response"),
                _response("custom-1", "en", "Custom mode", "First custom"),
                _response("custom-2", "en", "Custom mode", "Second custom"),
            ),
            page_count=1,
        )

        binding = bind_google_forms_family_responses(family, [capture])

        choice = next(
            item
            for item in binding.columns_detected
            if item.role == "single_choice"
        )
        session_choice = choice.to_session_value()
        self.assertEqual(session_choice["options"], ["排位", "经典", "Other / 其他"])
        self.assertEqual(session_choice["other_text"], {
            "enabled": True,
            "option": "Other / 其他",
            "provider_declared": True,
            "count": 2,
            "examples": ["Custom mode"],
            "values": ["Custom mode"],
        })
        self.assertEqual(binding.rows[1][0], "排位")
        self.assertEqual(binding.rows[2][0], "Custom mode")

    def test_multilingual_merge_only_guards_same_response_id_within_form(self):
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
        captures = [
            GoogleFormResponsesCapture(
                form_id="FORM_EN",
                responses=(
                    _response("same-id", "en", "Ranked", "Original English"),
                    _response("same-id", "en", "Ranked", "Duplicate"),
                ),
                page_count=1,
            ),
            GoogleFormResponsesCapture(
                form_id="FORM_ID",
                responses=(
                    _response("same-id", "id", "Peringkat", "Jawaban asli"),
                ),
                page_count=1,
            ),
        ]

        binding = bind_google_forms_family_responses(family, captures)

        self.assertEqual(binding.duplicate_response_count, 1)
        self.assertEqual(len(binding.rows), 3)
        self.assertEqual(binding.unmatched_answer_count, 0)
        self.assertEqual(binding.blocking_issue_count, 0)
        self.assertEqual(
            [item.language for item in binding.response_provenance],
            ["en", "id"],
        )
        self.assertEqual(
            binding.response_provenance[1].answers[1].original_values,
            ["Jawaban asli"],
        )
        language_column = next(
            item for item in binding.columns_detected
            if item.source_question_id == "system:source_language"
        )
        self.assertEqual(language_column.role, "single_choice")
        self.assertTrue(language_column.use_as_profile)
        self.assertEqual(language_column.profile_scope, "analysis")
        self.assertIn("排位", binding.rows[1])
        self.assertIn("排位", binding.rows[2])

    def test_same_answers_with_distinct_response_ids_are_all_preserved(self):
        en = snapshot("FORM_EN", "en")
        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[FamilyVariantSnapshot(language="en", snapshot=en)],
            semantic_questions={},
            now=NOW,
        )
        capture = GoogleFormResponsesCapture(
            form_id="FORM_EN",
            responses=(
                _response("response-1", "en", "Ranked", "Same answer"),
                _response("response-2", "en", "Ranked", "Same answer"),
            ),
            page_count=1,
        )

        binding = bind_google_forms_family_responses(family, [capture])

        self.assertEqual(binding.duplicate_response_count, 0)
        self.assertEqual(len(binding.rows), 3)
        self.assertEqual(
            [item.response_id for item in binding.response_provenance],
            ["response-1", "response-2"],
        )
        self.assertEqual(binding.rows[1][1], "Same answer")
        self.assertEqual(binding.rows[2][1], "Same answer")

    def test_partial_multiselect_matrix_and_file_metadata_are_preserved(self):
        questions = [
            _question(
                "en-multi",
                "en-provider-multi",
                "Choices",
                CanonicalQuestionType.MULTI_CHOICE,
                options=("Alpha", "Beta"),
            ),
            _question(
                "en-matrix-multi",
                None,
                "Matrix",
                CanonicalQuestionType.MATRIX_MULTI,
                options=("On", "Off"),
                rows=(
                    ("en-row-a", "Speed"),
                    ("en-row-b", "Stability"),
                ),
            ),
            _question(
                "en-file",
                "en-provider-file",
                "Upload",
                CanonicalQuestionType.FILE_UPLOAD,
            ),
            _question(
                "en-open",
                "en-provider-open",
                "Why",
                CanonicalQuestionType.OPEN_TEXT,
            ),
        ]
        source = snapshot("FORM_EN", "en").model_copy(update={
            "canonical_questions": questions,
            "question_count": len(questions),
        })
        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[FamilyVariantSnapshot(language="en", snapshot=source)],
            semantic_questions={},
            now=NOW,
        )
        capture = GoogleFormResponsesCapture(
            form_id="FORM_EN",
            responses=(
                GoogleFormResponse(
                    response_id="complete",
                    create_time="2026-08-25T00:00:00Z",
                    last_submitted_time="2026-08-25T00:01:00Z",
                    respondent_email=None,
                    answers=(
                        GoogleResponseAnswer(
                            question_id="en-provider-multi",
                            text_values=("Alpha", "Beta"),
                        ),
                        GoogleResponseAnswer(
                            question_id="en-row-a",
                            text_values=("On", "Off"),
                        ),
                        GoogleResponseAnswer(
                            question_id="en-row-b",
                            text_values=("On",),
                        ),
                        GoogleResponseAnswer(
                            question_id="en-provider-file",
                            file_uploads=(
                                GoogleFileUploadAnswer(
                                    file_id="file-1",
                                    file_name="one.png",
                                    mime_type="image/png",
                                ),
                                GoogleFileUploadAnswer(
                                    file_id="file-2",
                                    file_name="two.pdf",
                                    mime_type="application/pdf",
                                ),
                            ),
                        ),
                        GoogleResponseAnswer(
                            question_id="en-provider-open",
                            text_values=("Complete response",),
                        ),
                    ),
                ),
                GoogleFormResponse(
                    response_id="partial",
                    create_time="2026-08-25T00:02:00Z",
                    last_submitted_time="2026-08-25T00:03:00Z",
                    respondent_email=None,
                    answers=(GoogleResponseAnswer(
                        question_id="en-provider-open",
                        text_values=("Partial response",),
                    ),),
                ),
            ),
            page_count=1,
        )

        binding = bind_google_forms_family_responses(family, [capture])

        self.assertEqual(binding.rows[0][:5], (
            "Choices",
            "Matrix [Speed]",
            "Matrix [Stability]",
            "Upload",
            "Why",
        ))
        self.assertEqual(binding.rows[1][:5], (
            "Alpha\nBeta",
            "On\nOff",
            "On",
            "",
            "Complete response",
        ))
        self.assertEqual(binding.rows[2][:5], (
            "",
            "",
            "",
            "",
            "Partial response",
        ))
        self.assertEqual(binding.file_upload_answer_count, 2)
        file_evidence = next(
            item
            for item in binding.response_provenance[0].answers
            if item.original_question_id == "en-provider-file"
        )
        self.assertEqual(file_evidence.original_values, [])
        self.assertEqual(file_evidence.file_ids, ["file-1", "file-2"])
        self.assertNotIn("one.png", repr(binding))
        self.assertNotIn("two.pdf", repr(binding))

    def test_unmatched_provider_answer_is_counted_and_blocks_session(self):
        en = snapshot("FORM_EN", "en")
        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[FamilyVariantSnapshot(language="en", snapshot=en)],
            semantic_questions={},
            now=NOW,
        )
        capture = GoogleFormResponsesCapture(
            form_id="FORM_EN",
            responses=(GoogleFormResponse(
                response_id="response-1",
                create_time="2026-08-25T00:00:00Z",
                last_submitted_time="2026-08-25T00:01:00Z",
                respondent_email=None,
                answers=(GoogleResponseAnswer(
                    question_id="removed-question-id",
                    text_values=("orphan answer",),
                ),),
            ),),
            page_count=1,
        )
        binding = bind_google_forms_family_responses(family, [capture])
        self.assertEqual(binding.unmatched_answer_count, 1)
        self.assertEqual(binding.blocking_issue_count, 1)


if __name__ == "__main__":
    unittest.main()
