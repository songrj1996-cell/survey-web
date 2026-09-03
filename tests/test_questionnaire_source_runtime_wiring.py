from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_ROUTE_FRAGMENTS = (
    "/snapshots",
    "/bested/",
    "/materials/",
    "/asset-review",
    "/workflow/",
)


def _run_import(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(extra_env)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; import app.main as main; "
                "print(json.dumps({"
                "'routes': sorted({getattr(r, 'path', '') for r in main.app.routes}),"
                "'runtime_bound': hasattr(main, '_questionnaire_source_runtime'),"
                "'runtime_modules': sorted(m for m in sys.modules "
                "if m.startswith('app.routers.questionnaire_source_runtime') "
                "or m.startswith('app.services.questionnaire_source_runtime'))"
                "}, ensure_ascii=False))"
            ),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


class QuestionnaireSourceRuntimeWiringTests(unittest.TestCase):
    def test_default_disabled_import_publishes_no_questionnaire_source_routes(self):
        with tempfile.TemporaryDirectory(prefix="google-main-off-") as temporary:
            result = _run_import({
                "DATA_DIR": temporary,
                "GOOGLE_FORMS_QUALITATIVE_ENABLED": "false",
                "GOOGLE_FORMS_SERVICE_ACCOUNT_ENABLED": "false",
                "GOOGLE_FORMS_SERVICE_ACCOUNT_FILE": "",
            })
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertFalse(payload["runtime_bound"])
        self.assertEqual(payload["runtime_modules"], [])
        self.assertFalse(any(
            path.startswith("/api/questionnaire-sources")
            for path in payload["routes"]
        ))

    def test_enabled_without_service_account_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="google-main-no-service-") as temporary:
            result = _run_import({
                "DATA_DIR": temporary,
                "GOOGLE_FORMS_QUALITATIVE_ENABLED": "true",
                "GOOGLE_FORMS_SERVICE_ACCOUNT_ENABLED": "false",
                "GOOGLE_FORMS_SERVICE_ACCOUNT_FILE": "",
            })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未启用服务账号连接", result.stderr)

    def test_enabled_without_credential_path_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="google-main-no-file-") as temporary:
            result = _run_import({
                "DATA_DIR": temporary,
                "GOOGLE_FORMS_QUALITATIVE_ENABLED": "true",
                "GOOGLE_FORMS_SERVICE_ACCOUNT_ENABLED": "true",
                "GOOGLE_FORMS_SERVICE_ACCOUNT_FILE": "",
            })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未配置凭据文件", result.stderr)

    def test_config_defaults_are_disabled_and_storage_is_under_data_dir(self):
        with tempfile.TemporaryDirectory(prefix="google-config-") as temporary:
            env = os.environ.copy()
            env.update({
                "DATA_DIR": temporary,
                "GOOGLE_FORMS_QUALITATIVE_ENABLED": "",
                "GOOGLE_FORMS_SERVICE_ACCOUNT_ENABLED": "",
                "GOOGLE_FORMS_SERVICE_ACCOUNT_FILE": "",
                "RESEARCH_ASSET_STORAGE_DIR": "",
                "PYTHONIOENCODING": "utf-8",
            })
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json; from app.core import config; "
                        "print(json.dumps({"
                        "'enabled': config.GOOGLE_FORMS_QUALITATIVE_ENABLED,"
                        "'service': config.GOOGLE_FORMS_SERVICE_ACCOUNT_ENABLED,"
                        "'credential': config.GOOGLE_FORMS_SERVICE_ACCOUNT_FILE is None,"
                        "'storage': str(config.RESEARCH_ASSET_STORAGE_DIR)"
                        "}))"
                    ),
                ],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip())
        self.assertFalse(payload["enabled"])
        self.assertFalse(payload["service"])
        self.assertTrue(payload["credential"])
        self.assertEqual(
            Path(payload["storage"]).resolve(),
            (Path(temporary) / "research_assets").resolve(),
        )

    def test_runtime_modules_have_no_out_of_scope_dependencies(self):
        source_paths = (
            PROJECT_ROOT / "app" / "services" / "questionnaire_source_runtime.py",
            PROJECT_ROOT / "app" / "routers" / "questionnaire_source_runtime.py",
        )
        text = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
        for forbidden in (
            "bested",
            "questionnaire_sources",
            "questionnaire_snapshot_analysis",
            "questionnaire_asset_review",
            "questionnaire_pdf_material",
            "questionnaire_material_snapshot",
        ):
            self.assertNotIn(forbidden, text)
        for fragment in FORBIDDEN_ROUTE_FRAGMENTS:
            self.assertNotIn(fragment, text)


if __name__ == "__main__":
    unittest.main()
