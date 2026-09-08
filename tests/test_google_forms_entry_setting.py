from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.schemas.requests import AppSettingsPatch
from app.services.settings_service import update_app_settings
from app.storage import settings as settings_storage


class GoogleFormsEntrySettingTests(unittest.TestCase):
    def test_default_is_disabled_and_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory(prefix="google-entry-setting-") as temporary:
            settings_file = Path(temporary) / "app_settings.json"
            settings_file.write_text(
                json.dumps({"comment_duplicate_reminder_enabled": False}),
                encoding="utf-8",
            )
            with patch.object(
                settings_storage,
                "APP_SETTINGS_FILE",
                str(settings_file),
            ):
                result = settings_storage._load_app_settings()

            persisted = json.loads(settings_file.read_text(encoding="utf-8"))

        self.assertFalse(result["comment_duplicate_reminder_enabled"])
        self.assertFalse(result["google_forms_entry_enabled"])
        self.assertEqual(persisted, result)

    def test_update_persists_google_entry_and_reports_audit_detail(self):
        current = {
            "comment_duplicate_reminder_enabled": True,
            "google_forms_entry_enabled": False,
        }
        with (
            patch(
                "app.services.settings_service._load_app_settings",
                return_value=current,
            ),
            patch("app.services.settings_service._save_app_settings") as save,
        ):
            settings, detail = update_app_settings(
                AppSettingsPatch(google_forms_entry_enabled=True)
            )

        self.assertTrue(settings["google_forms_entry_enabled"])
        self.assertEqual(detail, "Google Link 入口：开启")
        save.assert_called_once_with(settings)


if __name__ == "__main__":
    unittest.main()
