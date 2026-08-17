from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE_PREFIX = "QUESTIONNAIRE_SOURCE_RUNTIME_PROBE="
QUESTIONNAIRE_SOURCE_PREFIX = "/api/questionnaire-sources"

EXPECTED_LOCAL_ROUTES = {
    ("GET", "/api/questionnaire-sources/capabilities"),
    ("POST", "/api/questionnaire-sources/snapshots"),
    ("GET", "/api/questionnaire-sources/snapshots/{snapshot_id}"),
    (
        "GET",
        "/api/questionnaire-sources/snapshots/{snapshot_id}/download",
    ),
    ("POST", "/api/questionnaire-sources/bested/snapshots"),
    ("POST", "/api/questionnaire-sources/materials/snapshots"),
    ("POST", "/api/questionnaire-sources/materials/pdf/snapshots"),
}

MAIN_PROBE = textwrap.dedent(
    f"""
    import json
    from pathlib import Path
    import sys

    from app import main
    from app.core import config

    routes = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith({QUESTIONNAIRE_SOURCE_PREFIX!r}):
            continue
        for method in sorted(getattr(route, "methods", None) or ()):
            routes.append({{"method": method, "path": path}})

    storage_root = Path(config.RESEARCH_ASSET_STORAGE_DIR)
    payload = {{
        "data_dir": str(Path(config.DATA_DIR).resolve()),
        "data_dir_exists": Path(config.DATA_DIR).is_dir(),
        "preview_enabled": config.QUESTIONNAIRE_LOCAL_SOURCE_PREVIEW_ENABLED,
        "routes": routes,
        "runtime_bound": hasattr(main, "_questionnaire_source_runtime"),
        "runtime_router_loaded": (
            "app.routers.questionnaire_source_runtime" in sys.modules
        ),
        "runtime_service_loaded": (
            "app.services.questionnaire_source_runtime" in sys.modules
        ),
        "storage_root": str(storage_root.resolve()),
        "storage_root_exists": storage_root.exists(),
    }}
    print({PROBE_PREFIX!r} + json.dumps(payload, sort_keys=True))
    """
)

CONFIG_PROBE = textwrap.dedent(
    f"""
    import json
    from pathlib import Path

    from app.core import config

    storage_root = Path(config.RESEARCH_ASSET_STORAGE_DIR)
    payload = {{
        "data_dir": str(Path(config.DATA_DIR).resolve()),
        "data_dir_exists": Path(config.DATA_DIR).is_dir(),
        "preview_enabled": config.QUESTIONNAIRE_LOCAL_SOURCE_PREVIEW_ENABLED,
        "storage_root": str(storage_root.resolve()),
        "storage_root_exists": storage_root.exists(),
    }}
    print({PROBE_PREFIX!r} + json.dumps(payload, sort_keys=True))
    """
)


class QuestionnaireSourceRuntimeWiringTests(unittest.TestCase):
    def _run_probe(
        self,
        script: str,
        *,
        preview_value: str | None,
        storage_value: str | None,
        data_dir_name: str,
    ) -> tuple[dict[str, object], Path, Path | None]:
        with tempfile.TemporaryDirectory(
            prefix="questionnaire-source-runtime-wiring-test-",
        ) as temporary:
            temporary_root = Path(temporary)
            data_dir = temporary_root / data_dir_name
            storage_root = (
                Path(storage_value.strip()) if storage_value is not None else None
            )
            env = os.environ.copy()
            env["DATA_DIR"] = str(data_dir)
            env["PYTHON_DOTENV_DISABLED"] = "1"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONPATH"] = os.pathsep.join(
                filter(
                    None,
                    (str(PROJECT_ROOT), env.get("PYTHONPATH", "")),
                )
            )

            if preview_value is None:
                env.pop("QUESTIONNAIRE_LOCAL_SOURCE_PREVIEW_ENABLED", None)
            else:
                env["QUESTIONNAIRE_LOCAL_SOURCE_PREVIEW_ENABLED"] = (
                    preview_value
                )
            if storage_value is None:
                env.pop("RESEARCH_ASSET_STORAGE_DIR", None)
            else:
                env["RESEARCH_ASSET_STORAGE_DIR"] = storage_value

            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=temporary_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode != 0:
                self.fail(
                    "isolated questionnaire-source runtime probe failed:\n"
                    f"{completed.stderr}"
                )

            payload_line = next(
                (
                    line
                    for line in reversed(completed.stdout.splitlines())
                    if line.startswith(PROBE_PREFIX)
                ),
                None,
            )
            if payload_line is None:
                self.fail(
                    "isolated questionnaire-source runtime probe did not "
                    f"return JSON; stdout was {completed.stdout!r}"
                )
            payload = json.loads(payload_line.removeprefix(PROBE_PREFIX))
            return payload, data_dir, storage_root

    @staticmethod
    def _route_pairs(payload: dict[str, object]) -> list[tuple[str, str]]:
        return [
            (route["method"], route["path"])
            for route in payload["routes"]
        ]

    def test_default_and_false_flags_do_not_register_runtime_routes(self):
        scenarios = (
            ("default", None),
            ("explicit-false", "false"),
        )
        for label, preview_value in scenarios:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory(
                    prefix="questionnaire-source-storage-path-test-",
                ) as storage_temporary:
                    storage_root = Path(storage_temporary) / "research-assets"
                    payload, data_dir, configured_storage = self._run_probe(
                        MAIN_PROBE,
                        preview_value=preview_value,
                        storage_value=str(storage_root),
                        data_dir_name=f"{label}-data",
                    )

                self.assertFalse(payload["preview_enabled"])
                self.assertEqual(self._route_pairs(payload), [])
                self.assertFalse(payload["runtime_bound"])
                self.assertFalse(payload["runtime_router_loaded"])
                self.assertFalse(payload["runtime_service_loaded"])
                self.assertEqual(Path(payload["data_dir"]), data_dir.resolve())
                self.assertTrue(payload["data_dir_exists"])
                self.assertEqual(
                    Path(payload["storage_root"]),
                    configured_storage.resolve(),
                )
                self.assertFalse(payload["storage_root_exists"])

    def test_true_flag_registers_each_local_runtime_route_once(self):
        with tempfile.TemporaryDirectory(
            prefix="questionnaire-source-storage-path-test-",
        ) as storage_temporary:
            storage_root = Path(storage_temporary) / "research-assets"
            payload, data_dir, configured_storage = self._run_probe(
                MAIN_PROBE,
                preview_value="true",
                storage_value=str(storage_root),
                data_dir_name="enabled-data",
            )

        routes = self._route_pairs(payload)
        self.assertTrue(payload["preview_enabled"])
        self.assertTrue(payload["runtime_bound"])
        self.assertTrue(payload["runtime_router_loaded"])
        self.assertTrue(payload["runtime_service_loaded"])
        self.assertEqual(len(routes), 7)
        self.assertEqual(len(routes), len(set(routes)))
        self.assertEqual(set(routes), EXPECTED_LOCAL_ROUTES)
        self.assertNotIn(
            ("POST", "/api/questionnaire-sources/google-forms/snapshots"),
            routes,
        )
        self.assertFalse(
            any("/workflows/" in path for _, path in routes),
        )
        self.assertEqual(Path(payload["data_dir"]), data_dir.resolve())
        self.assertTrue(payload["data_dir_exists"])
        self.assertEqual(
            Path(payload["storage_root"]),
            configured_storage.resolve(),
        )
        self.assertFalse(payload["storage_root_exists"])

    def test_config_storage_root_override_and_default_are_lazy(self):
        with tempfile.TemporaryDirectory(
            prefix="questionnaire-source-config-override-test-",
        ) as storage_temporary:
            override_root = Path(storage_temporary) / "custom-research-assets"
            override, override_data_dir, configured_override = self._run_probe(
                CONFIG_PROBE,
                preview_value=None,
                storage_value=f"  {override_root}  ",
                data_dir_name="override-data",
            )

        self.assertEqual(
            Path(override["storage_root"]),
            configured_override.resolve(),
        )
        self.assertEqual(
            Path(override["data_dir"]),
            override_data_dir.resolve(),
        )
        self.assertTrue(override["data_dir_exists"])
        self.assertFalse(override["storage_root_exists"])
        self.assertFalse(override["preview_enabled"])

        default, default_data_dir, configured_default = self._run_probe(
            CONFIG_PROBE,
            preview_value=None,
            storage_value=None,
            data_dir_name="default-data",
        )
        self.assertIsNone(configured_default)
        self.assertEqual(
            Path(default["storage_root"]),
            (default_data_dir / "research_assets").resolve(),
        )
        self.assertEqual(
            Path(default["data_dir"]),
            default_data_dir.resolve(),
        )
        self.assertTrue(default["data_dir_exists"])
        self.assertFalse(default["storage_root_exists"])
        self.assertFalse(default["preview_enabled"])


if __name__ == "__main__":
    unittest.main()
