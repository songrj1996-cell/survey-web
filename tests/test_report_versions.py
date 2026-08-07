from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from app.services import report_versions
from app.storage import history as history_storage


def _snapshot(title: str, *, created_at: str) -> dict:
    return {
        "report_md": f"# {title}\n\n正文",
        "title": title,
        "qa_context_md": f"<report>{title}</report>",
        "qa_messages": [{"role": "user", "content": f"关于{title}"}],
        "qa_provider": "direct_llm",
        "qa_model": f"qa-{title}",
        "report_writer_provider": "direct_llm",
        "report_writer_model": f"writer-{title}",
        "analyst_conv_id": f"conv-{title}",
        "analyst_app": "standard",
        "created_at": created_at,
    }


class ReportVersionTests(unittest.TestCase):
    def test_legacy_report_projects_read_only_v1_and_resolves_active_or_explicit(self):
        source = {
            "report_md": "# 旧报告\n\n旧正文",
            "title": "旧报告",
            "created_at": "2026-08-01T10:00:00",
            "qa_context_md": "<report>旧报告</report>",
            "qa_messages": [{"role": "user", "content": "旧问题"}],
            "qa_provider": "direct_llm",
            "qa_model": "qa-old",
            "report_writer_provider": "direct_llm",
            "report_writer_model": "writer-old",
            "analyst_conv_id": "legacy-conv",
            "analyst_app": "standard",
        }
        before = deepcopy(source)

        versions = report_versions.normalize_report_versions(source)

        self.assertEqual(source, before)
        self.assertNotIn("report_versions", source)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["version"], 1)
        self.assertEqual(versions[0]["kind"], "initial")
        self.assertIsNone(versions[0]["base_version"])
        self.assertEqual(versions[0]["report_md"], source["report_md"])
        self.assertEqual(
            report_versions.resolve_report_version(source),
            versions[0],
        )
        self.assertEqual(
            report_versions.resolve_report_version(source, "V1"),
            versions[0],
        )

        summaries = report_versions.report_version_summaries(source)
        self.assertEqual(
            set(summaries[0]),
            {
                "version",
                "kind",
                "base_version",
                "instruction",
                "created_at",
                "title",
            },
        )
        self.assertNotIn("report_md", summaries[0])
        self.assertNotIn("qa_context_md", summaries[0])

    def test_append_legacy_v1_to_v2_and_mirrors_active_snapshot(self):
        source = {
            "report_md": "# V1 报告\n\n第一版",
            "title": "V1 报告",
            "created_at": "2026-08-01T10:00:00",
            "qa_context_md": "<report>V1</report>",
            "qa_messages": [],
            "report_writer_provider": "direct_llm",
            "report_writer_model": "writer-v1",
            "analyst_conv_id": "conv-v1",
            "analyst_app": "standard",
        }
        second_input = _snapshot("V2 报告", created_at="2026-08-02T10:00:00")

        second = report_versions.append_report_version(
            source,
            second_input,
            instruction="聚焦流失原因",
        )

        self.assertEqual(second["version"], 2)
        self.assertEqual(second["kind"], "regenerate")
        self.assertEqual(second["base_version"], 1)
        self.assertEqual(second["instruction"], "聚焦流失原因")
        self.assertEqual(source["active_report_version"], 2)
        self.assertEqual(source["next_report_version"], 3)
        self.assertEqual(
            [item["version"] for item in source["report_versions"]],
            [1, 2],
        )
        self.assertEqual(source["report_md"], second_input["report_md"])
        self.assertEqual(source["title"], second_input["title"])
        self.assertEqual(source["qa_context_md"], second_input["qa_context_md"])
        self.assertEqual(source["qa_messages"], second_input["qa_messages"])
        self.assertEqual(
            source["report_writer_model"],
            second_input["report_writer_model"],
        )
        self.assertEqual(source["analyst_conv_id"], "conv-V2 报告")
        self.assertEqual(
            report_versions.resolve_report_version(source, 1)["title"],
            "V1 报告",
        )
        self.assertEqual(
            report_versions.resolve_report_version(source)["title"],
            "V2 报告",
        )

    def test_sync_materializes_initial_version_and_honors_selected_active(self):
        source = {
            "report_md": "# 初始报告\n\n正文",
            "title": "初始报告",
            "created_at": "2026-08-01T10:00:00",
        }
        initial = report_versions.sync_active_report_version(source)
        self.assertEqual(initial["version"], 1)
        self.assertEqual(source["active_report_version"], 1)
        self.assertEqual(source["next_report_version"], 2)
        self.assertEqual(len(source["report_versions"]), 1)

        report_versions.append_report_version(
            source,
            _snapshot("第二版", created_at="2026-08-02T10:00:00"),
        )
        source["active_report_version"] = 1
        active = report_versions.sync_active_report_version(source)
        self.assertEqual(active["version"], 1)
        self.assertEqual(source["title"], "初始报告")
        self.assertEqual(source["next_report_version"], 3)

    def test_version_limit_and_invalid_append_leave_source_unchanged(self):
        source = {
            "report_md": "# 第一版\n\n正文",
            "title": "第一版",
            "created_at": "2026-08-01T10:00:00",
        }
        report_versions.sync_active_report_version(source)
        with patch.object(report_versions, "MAX_REPORT_VERSIONS", 2):
            report_versions.append_report_version(
                source,
                _snapshot("第二版", created_at="2026-08-02T10:00:00"),
            )
            before_limit = deepcopy(source)
            with self.assertRaisesRegex(ValueError, "已达上限"):
                report_versions.append_report_version(
                    source,
                    _snapshot("第三版", created_at="2026-08-03T10:00:00"),
                )
            self.assertEqual(source, before_limit)

        before_invalid = deepcopy(source)
        with self.assertRaisesRegex(ValueError, "基础报告版本 V99 不存在"):
            report_versions.append_report_version(
                source,
                _snapshot("错误版本", created_at="2026-08-03T10:00:00"),
                base_version=99,
            )
        self.assertEqual(source, before_invalid)

    def test_update_materializes_legacy_and_only_active_update_changes_top_level(self):
        source = {
            "report_md": "# 第一版\n\n正文",
            "title": "第一版",
            "created_at": "2026-08-01T10:00:00",
            "qa_messages": [],
        }
        updated_first = report_versions.update_report_version(
            source,
            1,
            qa_context_md="<report>第一版完整上下文</report>",
            qa_messages=[{"role": "user", "content": "新问题"}],
            qa_provider="direct_llm",
            qa_model="qa-v1",
        )
        self.assertEqual(updated_first["version"], 1)
        self.assertEqual(source["active_report_version"], 1)
        self.assertEqual(len(source["report_versions"]), 1)
        self.assertEqual(source["qa_messages"], updated_first["qa_messages"])
        self.assertEqual(source["qa_model"], "qa-v1")

        report_versions.append_report_version(
            source,
            _snapshot("第二版", created_at="2026-08-02T10:00:00"),
        )
        active_before = {
            field: deepcopy(source[field])
            for field in (
                "report_md",
                "title",
                "qa_context_md",
                "qa_messages",
                "report_writer_model",
            )
        }
        renamed_first = report_versions.update_report_version(
            source,
            1,
            title="第一版改名",
            report_md="# 第一版改名\n\n正文",
        )
        self.assertEqual(renamed_first["version"], 1)
        self.assertEqual(renamed_first["title"], "第一版改名")
        self.assertEqual(
            report_versions.resolve_report_version(source, 1)["title"],
            "第一版改名",
        )
        for field, value in active_before.items():
            self.assertEqual(source[field], value)

        before_invalid = deepcopy(source)
        with self.assertRaisesRegex(ValueError, "不可修改"):
            report_versions.update_report_version(
                source,
                1,
                active_report_version=9,
            )
        self.assertEqual(source, before_invalid)

    def test_delete_rules_choose_highest_remaining_active_and_never_reuse_number(self):
        source = {
            "report_md": "# 第一版\n\n正文",
            "title": "第一版",
            "created_at": "2026-08-01T10:00:00",
        }
        report_versions.sync_active_report_version(source)
        report_versions.append_report_version(
            source,
            _snapshot("第二版", created_at="2026-08-02T10:00:00"),
        )
        report_versions.append_report_version(
            source,
            _snapshot("第三版", created_at="2026-08-03T10:00:00"),
            base_version=1,
        )

        deleted = report_versions.delete_report_version(source, 3)
        self.assertEqual(deleted["version"], 3)
        self.assertEqual(source["active_report_version"], 2)
        self.assertEqual(source["title"], "第二版")
        self.assertEqual(source["next_report_version"], 4)

        fourth = report_versions.append_report_version(
            source,
            _snapshot("第四版", created_at="2026-08-04T10:00:00"),
        )
        self.assertEqual(fourth["version"], 4)
        self.assertEqual(
            [item["version"] for item in source["report_versions"]],
            [1, 2, 4],
        )

        report_versions.delete_report_version(source, 1)
        report_versions.delete_report_version(source, 2)
        before_last_delete = deepcopy(source)
        with self.assertRaisesRegex(ValueError, "不能删除最后一个"):
            report_versions.delete_report_version(source, 4)
        self.assertEqual(source, before_last_delete)


class HistoryStorageTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="history-version-test-")
        self.history_file = os.path.join(self.temp_dir.name, "history.json")
        self.path_patch = patch.object(
            history_storage,
            "HISTORY_FILE",
            self.history_file,
        )
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_atomic_save_failure_preserves_original_and_cleans_unique_temp(self):
        original = [{"id": "original", "report_no": "R-001"}]
        history_storage._save_history(original)
        self.assertEqual(history_storage._load_history(), original)
        self.assertEqual(os.listdir(self.temp_dir.name), ["history.json"])

        with patch.object(
            history_storage.json,
            "dump",
            side_effect=RuntimeError("serialization failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "serialization failed"):
                history_storage._save_history([{"id": "replacement"}])

        self.assertEqual(history_storage._load_history(), original)
        self.assertEqual(os.listdir(self.temp_dir.name), ["history.json"])

    def test_mutate_history_serializes_concurrent_read_modify_write(self):
        history_storage._save_history([])

        def add_item(index: int) -> int:
            def mutation(history: list) -> int:
                time.sleep(0.001)
                history.append({"id": index})
                return len(history)

            return history_storage.mutate_history(mutation)

        with ThreadPoolExecutor(max_workers=8) as executor:
            lengths = list(executor.map(add_item, range(32)))

        stored = history_storage._load_history()
        self.assertEqual(len(stored), 32)
        self.assertEqual({item["id"] for item in stored}, set(range(32)))
        self.assertEqual(sorted(lengths), list(range(1, 33)))
        self.assertEqual(os.listdir(self.temp_dir.name), ["history.json"])

    def test_mutation_exception_does_not_write_partial_changes(self):
        original = [{"id": "kept"}]
        history_storage._save_history(original)

        def failing_mutation(history: list) -> None:
            history.append({"id": "not-saved"})
            raise ValueError("stop")

        with self.assertRaisesRegex(ValueError, "stop"):
            history_storage.mutate_history(failing_mutation)
        self.assertEqual(history_storage._load_history(), original)

    def test_report_number_backfill_preserves_newer_qa_and_versions(self):
        original = [{
            "id": "report-1",
            "created_at": "2026-08-01T10:00:00",
            "qa_messages": [],
            "report_versions": [{"version": 1, "report_md": "# V1"}],
        }]
        history_storage._save_history(original)
        stale = history_storage._load_history()

        def add_newer_fields(history: list) -> None:
            history[0]["qa_messages"] = [
                {"role": "user", "content": "并发写入的问题"},
            ]
            history[0]["report_versions"].append({
                "version": 2,
                "report_md": "# V2",
            })

        history_storage.mutate_history(add_newer_fields)
        result = history_storage._ensure_history_report_numbers(stale)
        stored = history_storage._load_history()

        self.assertEqual(result, stored)
        self.assertEqual(stored[0]["report_no"], "R-001")
        self.assertEqual(len(stored[0]["report_versions"]), 2)
        self.assertEqual(
            stored[0]["qa_messages"][0]["content"],
            "并发写入的问题",
        )

    def test_clean_numbered_get_does_not_rewrite_history(self):
        original = [{"id": "report-1", "report_no": "R-001"}]
        history_storage._save_history(original)

        with patch.object(history_storage, "_save_history_unlocked") as save:
            loaded = history_storage._load_history_with_report_numbers()

        self.assertEqual(loaded, original)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
