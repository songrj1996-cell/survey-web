from __future__ import annotations

import unittest

from app.integrations.google_forms_responses_client import (
    GoogleFormResponse,
    GoogleFormResponsesCapture,
    GoogleResponseAnswer,
)
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
    def test_multilingual_merge_deduplicates_within_variant_only(self):
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
        self.assertEqual(language_column.role, "profile_dim")
        self.assertIn("排位", binding.rows[1])
        self.assertIn("排位", binding.rows[2])

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
