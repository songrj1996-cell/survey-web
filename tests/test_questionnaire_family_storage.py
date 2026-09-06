from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from app.services.questionnaire_family_mapping import (
    FamilyVariantSnapshot,
    build_questionnaire_family,
)
from app.storage.questionnaire_families import (
    FileQuestionnaireFamilyStorage,
    QuestionnaireFamilyCatalogInvalidError,
    QuestionnaireFamilyStorageError,
)
from tests.test_questionnaire_family_mapping import snapshot


def _family(owner: str, title: str, updated_at: datetime):
    source = snapshot(f"FORM-{title}", "en")
    return build_questionnaire_family(
        owner_ref=owner,
        title=title,
        variants=[FamilyVariantSnapshot(language="en", snapshot=source)],
        semantic_questions={},
        now=updated_at,
    )


class QuestionnaireFamilyStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="family-storage-")
        self.storage = FileQuestionnaireFamilyStorage(self.temporary.name)
        self.now = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    def test_list_is_owner_scoped_sorted_and_cursor_paged(self):
        oldest = _family("owner-a", "Oldest", self.now)
        middle = _family("owner-a", "Middle", self.now + timedelta(minutes=1))
        newest = _family("owner-a", "Newest", self.now + timedelta(minutes=2))
        hidden = _family("owner-b", "Other owner", self.now + timedelta(minutes=3))
        for family in (oldest, middle, newest, hidden):
            self.storage.save_family(family)

        first = self.storage.list_families("owner-a", limit=2)
        self.assertEqual(
            [family.title for family in first.families],
            ["Newest", "Middle"],
        )
        self.assertIsNotNone(first.next_cursor)
        second = self.storage.list_families(
            "owner-a",
            cursor=first.next_cursor,
            limit=2,
        )
        self.assertEqual([family.title for family in second.families], ["Oldest"])
        self.assertIsNone(second.next_cursor)

    def test_list_rejects_invalid_cursor_and_limit(self):
        with self.assertRaises(QuestionnaireFamilyCatalogInvalidError):
            self.storage.list_families("owner-a", cursor="not-a-valid-cursor")
        with self.assertRaises(QuestionnaireFamilyCatalogInvalidError):
            self.storage.list_families("owner-a", limit=0)
        with self.assertRaises(QuestionnaireFamilyCatalogInvalidError):
            self.storage.list_families("owner-a", limit=51)

    def test_list_ignores_lock_and_temporary_files_but_rejects_corrupt_json(self):
        family = _family("owner-a", "Valid", self.now)
        self.storage.save_family(family)
        owner_directory = self.storage._path(
            family.owner_ref,
            family.family_id,
        ).parent
        (owner_directory / "ignored.lock").write_text("lock", encoding="utf-8")
        (owner_directory / "ignored.tmp").write_text("tmp", encoding="utf-8")
        self.assertEqual(len(self.storage.list_families("owner-a").families), 1)

        target = self.storage._path(family.owner_ref, family.family_id)
        target.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(QuestionnaireFamilyStorageError):
            self.storage.list_families("owner-a")


if __name__ == "__main__":
    unittest.main()
