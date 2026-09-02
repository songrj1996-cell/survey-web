import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.core.llm_context import current_llm_api_key
from app.routers import profile as profile_router
from app.services import llm_credentials
from app.storage import llm_credentials as credential_storage
from app.storage import llm_usage as usage_storage


class UserLlmCredentialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.credentials_file = os.path.join(
            self.temp_dir.name,
            "user_llm_credentials.json",
        )
        self.encryption_key = Fernet.generate_key().decode("ascii")
        self.usage_file = os.path.join(
            self.temp_dir.name,
            "user_llm_usage.json",
        )
        self.patches = [
            patch.object(
                credential_storage,
                "USER_LLM_CREDENTIALS_FILE",
                self.credentials_file,
            ),
            patch.object(
                llm_credentials,
                "USER_LLM_KEY_ENCRYPTION_KEY",
                self.encryption_key,
            ),
            patch.object(llm_credentials, "FEISHU_LOGIN_REQUIRED", True),
            patch.object(
                usage_storage,
                "USER_LLM_USAGE_FILE",
                self.usage_file,
            ),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    async def test_encrypted_keys_are_isolated_by_user_and_never_stored_plaintext(self):
        first = {"open_id": "ou_first", "email": "first@example.com"}
        second = {"open_id": "ou_second", "email": "second@example.com"}
        with patch.object(
            llm_credentials,
            "collect_chat_completion",
            new=AsyncMock(return_value=("OK", "model-a")),
        ) as validate:
            await llm_credentials.validate_and_save_user_llm_api_key(
                first,
                "first-user-secret",
            )
            await llm_credentials.validate_and_save_user_llm_api_key(
                second,
                "second-user-secret",
            )

        self.assertEqual(validate.await_count, 2)
        self.assertEqual(
            llm_credentials.get_user_llm_api_key(first),
            "first-user-secret",
        )
        self.assertEqual(
            llm_credentials.get_user_llm_api_key(second),
            "second-user-secret",
        )
        self.assertTrue(llm_credentials.get_user_llm_key_status(first)["configured"])

        with open(self.credentials_file, "r", encoding="utf-8") as file:
            raw = file.read()
        self.assertNotIn("first-user-secret", raw)
        self.assertNotIn("second-user-secret", raw)
        self.assertNotIn("first@example.com", raw)
        self.assertNotIn("ou_first", raw)
        parsed = json.loads(raw)
        self.assertEqual(len(parsed), 2)
        with open(self.usage_file, "r", encoding="utf-8") as file:
            usage_raw = file.read()
        self.assertNotIn("first-user-secret", usage_raw)
        self.assertNotIn("second-user-secret", usage_raw)
        self.assertNotIn("first@example.com", usage_raw)

    async def test_validation_failure_does_not_save_key(self):
        login = {"open_id": "ou_invalid"}
        with patch.object(
            llm_credentials,
            "collect_chat_completion",
            new=AsyncMock(
                side_effect=RuntimeError(
                    "auth rejected invalid-user-secret"
                )
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await llm_credentials.validate_and_save_user_llm_api_key(
                    login,
                    "invalid-user-secret",
                )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertNotIn("invalid-user-secret", str(caught.exception.detail))
        self.assertFalse(os.path.exists(self.credentials_file))

    async def test_delete_removes_only_current_user(self):
        first = {"open_id": "ou_first"}
        second = {"open_id": "ou_second"}
        with patch.object(
            llm_credentials,
            "collect_chat_completion",
            new=AsyncMock(return_value=("OK", "model-a")),
        ):
            await llm_credentials.validate_and_save_user_llm_api_key(first, "first-secret")
            await llm_credentials.validate_and_save_user_llm_api_key(second, "second-secret")

        self.assertTrue(llm_credentials.delete_user_llm_api_key(first))
        with self.assertRaises(HTTPException) as caught:
            llm_credentials.get_user_llm_api_key(first)
        self.assertEqual(caught.exception.status_code, 428)
        self.assertEqual(llm_credentials.get_user_llm_api_key(second), "second-secret")

    async def test_stream_scope_keeps_key_bound_until_generator_finishes(self):
        async def source():
            await __import__("asyncio").sleep(0)
            yield current_llm_api_key()

        chunks = []
        async for chunk in llm_credentials.stream_with_llm_api_key(
            source(),
            "scoped-secret",
        ):
            chunks.append(chunk)

        self.assertEqual(chunks, ["scoped-secret"])
        self.assertEqual(current_llm_api_key(), "")

    async def test_request_without_saved_key_is_rejected_before_llm_work(self):
        login = {"open_id": "ou_missing"}
        with patch.object(
            llm_credentials,
            "_current_login",
            new=AsyncMock(return_value=login),
        ):
            with self.assertRaises(HTTPException) as caught:
                await llm_credentials.require_request_llm_api_key(object())
        self.assertEqual(caught.exception.status_code, 428)
        self.assertEqual(
            caught.exception.detail["code"],
            "USER_LLM_KEY_REQUIRED",
        )

    async def test_profile_status_never_returns_api_key_or_ciphertext(self):
        login = {
            "open_id": "ou_profile",
            "name": "测试用户",
            "email": "profile@example.com",
        }
        status = {
            "required": True,
            "personal_key_supported": True,
            "configured": True,
            "storage_ready": True,
            "updated_at": "2026-08-31T00:00:00+00:00",
        }
        with (
            patch.object(
                profile_router,
                "_current_login",
                new=AsyncMock(return_value=login),
            ),
            patch.object(
                profile_router,
                "get_user_llm_key_status",
                return_value=status,
            ),
        ):
            result = await profile_router.get_profile(object())

        self.assertEqual(result["email"], "profile@example.com")
        self.assertTrue(result["configured"])
        self.assertNotIn("api_key", result)
        self.assertNotIn("ciphertext", result)

    def test_missing_encryption_key_is_reported_without_writing(self):
        with patch.object(llm_credentials, "USER_LLM_KEY_ENCRYPTION_KEY", ""):
            status = llm_credentials.get_user_llm_key_status(
                {"open_id": "ou_first"}
            )
        self.assertFalse(status["storage_ready"])
        self.assertFalse(status["configured"])
        self.assertFalse(os.path.exists(self.credentials_file))


if __name__ == "__main__":
    unittest.main()
