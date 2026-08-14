from __future__ import annotations

import errno
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from queue import Empty
from unittest.mock import patch

from app.storage import interview_v2_store as store


PROJECT_ID = "project_" + "1" * 32
IMPORT_ID = "import_" + "2" * 32


def _revision(suffix: str) -> dict:
    revision = {
        "project_id": PROJECT_ID,
        "import_id": IMPORT_ID,
        "mapping_revision_id": "mapping_" + suffix * 32,
        "revision_number": 1,
        "mapping_sha256": suffix * 64,
        "change_kind": "manual_edit",
        "change_reason": f"worker-{suffix}",
        "created_at": "2026-08-13T00:00:00Z",
        "mapping": {"groups": []},
    }
    revision["revision_payload_sha256"] = (
        store.mapping_revision_payload_sha256(revision)
    )
    return revision


def _save_worker(
    root: str,
    suffix: str,
    ready: multiprocessing.synchronize.Event,
    start: multiprocessing.synchronize.Event,
    output: multiprocessing.queues.Queue,
) -> None:
    store.config.INTERVIEW_V2_DATA_DIR = Path(root)
    ready.set()
    if not start.wait(10):
        output.put(("error", "start timeout"))
        return
    try:
        revision, state = store.save_mapping_revision_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_mapping_revision=0,
            revision=_revision(suffix),
            updated_at="2026-08-13T00:00:01Z",
        )
    except FileExistsError:
        output.put(("conflict", suffix))
    except Exception as exc:
        output.put(("error", f"{type(exc).__name__}: {exc}"))
    else:
        output.put(
            (
                "success",
                revision["mapping_revision_id"],
                state["current_mapping_sha256"],
            )
        )


def _confirm_worker(
    root: str,
    timeout_seconds: float,
    output: multiprocessing.queues.Queue,
) -> None:
    store.config.INTERVIEW_V2_DATA_DIR = Path(root)
    store._MAPPING_LOCK_TIMEOUT_SECONDS = timeout_seconds
    try:
        store.confirm_mapping_revision_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_mapping_revision=1,
            mapping_sha256="a" * 64,
            confirmed_by="worker",
            confirmed_at="2026-08-13T00:00:02Z",
        )
    except TimeoutError:
        output.put(("timeout",))
    except Exception as exc:
        output.put(("error", f"{type(exc).__name__}: {exc}"))
    else:
        output.put(("confirmed",))


class InterviewV2MappingStoreProcessLockTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="interview-v2-mapping-store-"
        )
        self.root = Path(self.temp_dir.name) / "interview_v2"
        self.project_dir = self.root / "projects" / PROJECT_ID
        self.project_dir.mkdir(parents=True)
        self.config_patch = patch.object(
            store.config, "INTERVIEW_V2_DATA_DIR", self.root
        )
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _cleanup_processes(processes) -> None:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)

    def test_two_processes_same_base_only_one_revision_wins(self):
        context = multiprocessing.get_context("spawn")
        ready = [context.Event(), context.Event()]
        start = context.Event()
        output = context.Queue()
        processes = [
            context.Process(
                target=_save_worker,
                args=(str(self.root), suffix, ready_event, start, output),
            )
            for suffix, ready_event in zip(("a", "b"), ready)
        ]
        self.addCleanup(self._cleanup_processes, processes)
        for process in processes:
            process.start()
        for ready_event in ready:
            self.assertTrue(ready_event.wait(10), "worker did not become ready")
        start.set()

        results = []
        try:
            results = [output.get(timeout=10), output.get(timeout=10)]
        except Empty as exc:
            self.fail(f"worker result timed out: {exc}")
        for process in processes:
            process.join(timeout=10)
            self.assertFalse(process.is_alive(), "worker did not exit")
            self.assertEqual(process.exitcode, 0)

        self.assertCountEqual(
            [result[0] for result in results], ["success", "conflict"]
        )
        winner = next(result for result in results if result[0] == "success")
        winner_revision_id = winner[1]
        winner_sha256 = winner[2]

        state = store.load_mapping_state(PROJECT_ID)
        self.assertEqual(state["current_revision_number"], 1)
        self.assertEqual(state["current_mapping_revision_id"], winner_revision_id)
        self.assertEqual(state["current_mapping_sha256"], winner_sha256)
        self.assertEqual(len(state["revision_history"]), 1)
        self.assertEqual(
            state["revision_history"][0]["mapping_revision_id"],
            winner_revision_id,
        )

        revision_files = list(
            (self.project_dir / "mapping_revisions").glob("mapping_*.json")
        )
        self.assertEqual([path.stem for path in revision_files], [winner_revision_id])
        revision = json.loads(revision_files[0].read_text(encoding="utf-8"))
        self.assertEqual(revision["mapping_revision_id"], winner_revision_id)
        self.assertEqual(revision["mapping_sha256"], winner_sha256)
        self.assertEqual(
            list(self.project_dir.rglob("*.tmp")), [], "temporary files leaked"
        )

        lock_path = self.project_dir / ".mapping.lock"
        self.assertTrue(lock_path.is_file())
        self.assertEqual(lock_path.resolve().parent, self.project_dir.resolve())

    def test_missing_mapping_state_is_a_read_only_none(self):
        self.assertIsNone(store.load_mapping_state(PROJECT_ID))
        self.assertFalse((self.project_dir / ".mapping.lock").exists())

    def test_confirm_timeout_is_fail_closed_and_releases_cleanly(self):
        store.save_mapping_revision_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_mapping_revision=0,
            revision=_revision("a"),
            updated_at="2026-08-13T00:00:01Z",
        )
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        process = context.Process(
            target=_confirm_worker,
            args=(str(self.root), 0.15, output),
        )
        self.addCleanup(self._cleanup_processes, [process])

        with store._mapping_process_lock(PROJECT_ID):
            process.start()
            try:
                result = output.get(timeout=10)
            except Empty as exc:
                self.fail(f"confirm worker result timed out: {exc}")
        process.join(timeout=10)
        self.assertFalse(process.is_alive(), "confirm worker did not exit")
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(result, ("timeout",))

        state = store.load_mapping_state(PROJECT_ID)
        self.assertEqual(state["effective_status"], "GROUP_CONFIRMATION_REQUIRED")
        self.assertEqual(state["confirmation_events"], [])

        confirmed = store.confirm_mapping_revision_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_mapping_revision=1,
            mapping_sha256="a" * 64,
            confirmed_by="parent",
            confirmed_at="2026-08-13T00:00:03Z",
        )
        self.assertEqual(confirmed["effective_status"], "GROUP_MAPPING_CONFIRMED")
        self.assertEqual(len(confirmed["confirmation_events"]), 1)

    def test_unexpected_lock_error_writes_no_mapping_state(self):
        with (
            patch.object(
                store,
                "_try_acquire_file_lock",
                side_effect=OSError(errno.EIO, "simulated lock failure"),
            ),
            self.assertRaises(OSError),
        ):
            store.save_mapping_revision_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_mapping_revision=0,
                revision=_revision("a"),
                updated_at="2026-08-13T00:00:01Z",
            )

        self.assertFalse((self.project_dir / "mapping_state.json").exists())
        self.assertEqual(
            list((self.project_dir / "mapping_revisions").glob("mapping_*.json")),
            [],
        )

    def test_mapping_state_digest_blocks_history_tampering(self):
        store.save_mapping_revision_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_mapping_revision=0,
            revision=_revision("a"),
            updated_at="2026-08-13T00:00:01Z",
        )
        state_path = self.project_dir / "mapping_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["state_payload_sha256"],
            store.mapping_state_payload_sha256(state),
        )
        state["revision_history"][0]["change_kind"] = "undo"
        state_path.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )

        next_revision = _revision("b")
        next_revision["revision_number"] = 2
        next_revision["revision_payload_sha256"] = (
            store.mapping_revision_payload_sha256(next_revision)
        )
        with self.assertRaises(ValueError):
            store.save_mapping_revision_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                base_mapping_revision=1,
                revision=next_revision,
                updated_at="2026-08-13T00:00:02Z",
            )


if __name__ == "__main__":
    unittest.main()
