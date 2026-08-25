from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

try:
    import fcntl
except ImportError:  # Windows has no POSIX descriptor-path API.
    fcntl = None

from pydantic import ValidationError

from app.schemas.questionnaire_asset_review_state import (
    QuestionnaireAssetReviewCommand,
    QuestionnaireAssetReviewDecision,
    QuestionnaireAssetReviewEvent,
    QuestionnaireAssetReviewState,
)
from app.storage import questionnaire_asset_reviews as review_storage
from app.storage.questionnaire_asset_reviews import (
    FileQuestionnaireAssetReviewStorage,
    QuestionnaireAssetReviewConflictError,
    QuestionnaireAssetReviewInvalidError,
    QuestionnaireAssetReviewStorageError,
)


OWNER_REF = "email:asset-review@example.com"
OTHER_OWNER_REF = "email:other-asset-review@example.com"
SNAPSHOT_ID = "snapshot_asset_review_001"
OTHER_SNAPSHOT_ID = "snapshot_asset_review_002"
BASE_PACKAGE = b"persisted-snapshot-package"
BASE_PACKAGE_SHA256 = hashlib.sha256(BASE_PACKAGE).hexdigest()
BASE_PACKAGE_SIZE_BYTES = len(BASE_PACKAGE)
FIXED_TIME = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)


def _token(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _descriptor_path(descriptor: int) -> Path:
    for directory in (Path("/dev/fd"), Path("/proc/self/fd")):
        try:
            return Path(os.readlink(directory / str(descriptor))).resolve()
        except OSError:
            continue
    if fcntl is not None and hasattr(fcntl, "F_GETPATH"):
        try:
            raw_path = fcntl.fcntl(
                descriptor,
                fcntl.F_GETPATH,
                b"\0" * 1024,
            )
        except OSError:
            pass
        else:
            return Path(os.fsdecode(raw_path.split(b"\0", 1)[0])).resolve()
    raise AssertionError(f"无法解析目录描述符：{descriptor}")


def _command(
    *,
    expected_revision: int = 0,
    idempotency_key: str | None = None,
    reference_token: str | None = None,
    asset_token: str | None = None,
    decision: QuestionnaireAssetReviewDecision = (
        QuestionnaireAssetReviewDecision.CONFIRMED
    ),
    reviewer_token: str | None = None,
    base_package_sha256: str = BASE_PACKAGE_SHA256,
    base_package_size_bytes: int = BASE_PACKAGE_SIZE_BYTES,
) -> QuestionnaireAssetReviewCommand:
    return QuestionnaireAssetReviewCommand(
        expected_revision=expected_revision,
        idempotency_key=idempotency_key or _token("idempotency-1"),
        reference_token=reference_token or _token("reference-1"),
        asset_token=asset_token or _token("asset-1"),
        decision=decision,
        reviewer_token=reviewer_token or _token("reviewer-1"),
        base_package_sha256=base_package_sha256,
        base_package_size_bytes=base_package_size_bytes,
    )


def _append_in_process(
    root: str,
    start_event,
    result_queue,
    suffix: str,
) -> None:
    storage = FileQuestionnaireAssetReviewStorage(
        root,
        clock=lambda: FIXED_TIME,
    )
    command = _command(
        idempotency_key=_token(f"process-idempotency-{suffix}"),
        reference_token=_token(f"process-reference-{suffix}"),
        asset_token=_token(f"process-asset-{suffix}"),
    )
    try:
        start_event.wait(timeout=5)
        state = storage.append(OWNER_REF, SNAPSHOT_ID, command)
    except QuestionnaireAssetReviewConflictError:
        result_queue.put("conflict")
    except Exception as error:  # pragma: no cover - surfaced in parent assertion
        result_queue.put(f"error:{type(error).__name__}:{error}")
    else:
        result_queue.put(f"ok:{state.revision}")


def _load_in_process(root: str, result_queue) -> None:
    storage = FileQuestionnaireAssetReviewStorage(root)
    try:
        storage.load_state(
            OWNER_REF,
            SNAPSHOT_ID,
            base_package_sha256=BASE_PACKAGE_SHA256,
            base_package_size_bytes=BASE_PACKAGE_SIZE_BYTES,
        )
    except QuestionnaireAssetReviewStorageError as error:
        result_queue.put(f"error:{type(error).__name__}")
    except Exception as error:  # pragma: no cover - surfaced in parent assertion
        result_queue.put(f"unexpected:{type(error).__name__}:{error}")
    else:
        result_queue.put("loaded")


class FileQuestionnaireAssetReviewStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="questionnaire-asset-review-storage-test-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "review-root"
        self.storage = FileQuestionnaireAssetReviewStorage(
            self.root,
            clock=lambda: FIXED_TIME,
        )

    def _load(
        self,
        *,
        owner_ref: str = OWNER_REF,
        snapshot_id: str = SNAPSHOT_ID,
        base_package_sha256: str = BASE_PACKAGE_SHA256,
        base_package_size_bytes: int = BASE_PACKAGE_SIZE_BYTES,
    ):
        return self.storage.load_state(
            owner_ref,
            snapshot_id,
            base_package_sha256=base_package_sha256,
            base_package_size_bytes=base_package_size_bytes,
        )

    def _state_path(
        self,
        *,
        owner_ref: str = OWNER_REF,
        snapshot_id: str = SNAPSHOT_ID,
    ) -> Path:
        return self.storage._state_path(owner_ref, snapshot_id)

    def _append_first(self):
        return self.storage.append(OWNER_REF, SNAPSHOT_ID, _command())

    def _assert_no_temporary_files(self) -> None:
        if not self.root.exists():
            return
        self.assertEqual(
            [path for path in self.root.rglob("*") if path.name.endswith(".tmp")],
            [],
        )

    def _tree_entries(self) -> set[Path]:
        if not self.root.exists():
            return set()
        return {path.relative_to(self.root) for path in self.root.rglob("*")}

    def test_missing_load_returns_anchored_revision_zero_without_any_write(self) -> None:
        self.assertFalse(self.root.exists())

        state = self._load()

        self.assertEqual(state.schema_version, 1)
        self.assertEqual(state.revision, 0)
        self.assertEqual(state.events, ())
        self.assertIsNone(state.head_event_sha256)
        self.assertEqual(state.base_package_sha256, BASE_PACKAGE_SHA256)
        self.assertEqual(
            state.base_package_size_bytes,
            BASE_PACKAGE_SIZE_BYTES,
        )
        self.assertEqual(
            state.owner_scope_key,
            "05c3ff6407f9fdd39ccd177132accf5009f2a66aa463c841222b268384bdd907",
        )
        self.assertEqual(
            state.snapshot_storage_key,
            hashlib.sha256(SNAPSHOT_ID.encode("utf-8")).hexdigest(),
        )
        self.assertFalse(self.root.exists())

    def test_first_append_persists_canonical_owner_scoped_state_and_loads(self) -> None:
        state = self._append_first()

        self.assertEqual(state.revision, 1)
        self.assertEqual(len(state.events), 1)
        event = state.events[0]
        self.assertEqual(event.revision, 1)
        self.assertEqual(event.decision, QuestionnaireAssetReviewDecision.CONFIRMED)
        self.assertEqual(event.recorded_at, FIXED_TIME)
        self.assertEqual(event.previous_event_sha256, BASE_PACKAGE_SHA256)
        self.assertEqual(event.event_sha256, state.head_event_sha256)

        target = self._state_path()
        self.assertTrue(target.is_file())
        self.assertEqual(
            target,
            self.root
            / ".asset-reviews-v1"
            / state.owner_scope_key
            / f"{state.snapshot_storage_key}.json",
        )
        persisted = target.read_bytes()
        self.assertEqual(json.loads(persisted), state.model_dump(mode="json"))
        self.assertEqual(
            persisted,
            json.dumps(
                json.loads(persisted),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        self.assertNotIn(OWNER_REF.encode("utf-8"), persisted)
        self.assertNotIn(SNAPSHOT_ID.encode("utf-8"), persisted)
        self.assertEqual(self._load(), state)

    def test_confirm_reject_and_reset_append_to_one_hash_chain(self) -> None:
        state = self._append_first()
        prior_head = state.head_event_sha256
        for revision, decision in enumerate(
            (
                QuestionnaireAssetReviewDecision.REJECTED,
                QuestionnaireAssetReviewDecision.RESET,
            ),
            start=2,
        ):
            old_events = state.events
            old_event_bytes = tuple(
                event.model_dump_json().encode("utf-8") for event in old_events
            )
            state = self.storage.append(
                OWNER_REF,
                SNAPSHOT_ID,
                _command(
                    expected_revision=revision - 1,
                    idempotency_key=_token(f"idempotency-{revision}"),
                    decision=decision,
                ),
            )
            self.assertEqual(state.revision, revision)
            self.assertEqual(state.events[-1].decision, decision)
            self.assertEqual(state.events[-1].previous_event_sha256, prior_head)
            self.assertEqual(len(state.events), len(old_events) + 1)
            self.assertEqual(state.events[:-1], old_events)
            self.assertEqual(
                tuple(
                    event.model_dump_json().encode("utf-8")
                    for event in state.events[:-1]
                ),
                old_event_bytes,
            )
            prior_head = state.head_event_sha256

        self.assertEqual(
            tuple(event.decision for event in state.events),
            (
                QuestionnaireAssetReviewDecision.CONFIRMED,
                QuestionnaireAssetReviewDecision.REJECTED,
                QuestionnaireAssetReviewDecision.RESET,
            ),
        )

    def test_replay_of_same_command_returns_current_state_without_writing(self) -> None:
        original = _command()
        first = self.storage.append(OWNER_REF, SNAPSHOT_ID, original)
        second = self.storage.append(
            OWNER_REF,
            SNAPSHOT_ID,
            _command(
                expected_revision=1,
                idempotency_key=_token("idempotency-2"),
                reference_token=_token("reference-2"),
                asset_token=_token("asset-2"),
            ),
        )
        self.assertEqual(first.revision, 1)
        self.assertEqual(second.revision, 2)
        target = self._state_path()
        before = target.read_bytes()
        before_mtime = target.stat().st_mtime_ns

        replayed = self.storage.append(OWNER_REF, SNAPSHOT_ID, original)

        self.assertEqual(replayed, second)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mtime_ns, before_mtime)

        replay_with_current_cas = original.model_copy(
            update={"expected_revision": second.revision}
        )
        replayed = self.storage.append(
            OWNER_REF,
            SNAPSHOT_ID,
            replay_with_current_cas,
        )
        self.assertEqual(replayed, second)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mtime_ns, before_mtime)

    def test_idempotent_replay_does_not_consult_a_failed_clock(self) -> None:
        original = _command()
        expected = self.storage.append(OWNER_REF, SNAPSHOT_ID, original)
        target = self._state_path()
        before = target.read_bytes()
        before_mtime = target.stat().st_mtime_ns

        def failed_clock() -> datetime:
            raise RuntimeError("injected clock failure")

        storage = FileQuestionnaireAssetReviewStorage(
            self.root,
            clock=failed_clock,
        )
        replayed = storage.append(OWNER_REF, SNAPSHOT_ID, original)

        self.assertEqual(replayed, expected)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mtime_ns, before_mtime)

    def test_failed_clock_for_new_event_preserves_existing_state(self) -> None:
        self._append_first()
        target = self._state_path()
        before = target.read_bytes()
        before_mtime = target.stat().st_mtime_ns

        def failed_clock() -> datetime:
            raise RuntimeError("injected clock failure")

        storage = FileQuestionnaireAssetReviewStorage(
            self.root,
            clock=failed_clock,
        )
        with self.assertRaises(QuestionnaireAssetReviewStorageError):
            storage.append(
                OWNER_REF,
                SNAPSHOT_ID,
                _command(
                    expected_revision=1,
                    idempotency_key=_token("failed-clock-new-event"),
                ),
            )

        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mtime_ns, before_mtime)

    def test_same_idempotency_key_with_different_command_conflicts(self) -> None:
        self._append_first()
        target = self._state_path()
        before = target.read_bytes()

        with self.assertRaisesRegex(
            QuestionnaireAssetReviewConflictError,
            "幂等",
        ):
            self.storage.append(
                OWNER_REF,
                SNAPSHOT_ID,
                _command(decision=QuestionnaireAssetReviewDecision.REJECTED),
            )

        self.assertEqual(target.read_bytes(), before)

    def test_base_identity_mismatch_precedes_idempotency_replay(self) -> None:
        self._append_first()
        target = self._state_path()
        before = target.read_bytes()
        other_sha = _token("different persisted package")

        with self.assertRaisesRegex(
            QuestionnaireAssetReviewConflictError,
            "基础快照",
        ):
            self.storage.append(
                OWNER_REF,
                SNAPSHOT_ID,
                _command(base_package_sha256=other_sha),
            )

        with self.assertRaisesRegex(
            QuestionnaireAssetReviewConflictError,
            "基础快照",
        ):
            self._load(base_package_size_bytes=BASE_PACKAGE_SIZE_BYTES + 1)
        self.assertEqual(target.read_bytes(), before)

    def test_stale_revision_conflicts_without_mutation(self) -> None:
        self._append_first()
        target = self._state_path()
        before = target.read_bytes()

        with self.assertRaisesRegex(
            QuestionnaireAssetReviewConflictError,
            "revision",
        ):
            self.storage.append(
                OWNER_REF,
                SNAPSHOT_ID,
                _command(
                    idempotency_key=_token("stale-idempotency"),
                    expected_revision=0,
                ),
            )

        self.assertEqual(target.read_bytes(), before)

    def test_thread_contenders_for_same_revision_have_one_winner(self) -> None:
        barrier = threading.Barrier(3)
        results: list[str] = []
        result_lock = threading.Lock()

        def contender(suffix: str) -> None:
            storage = FileQuestionnaireAssetReviewStorage(
                self.root,
                clock=lambda: FIXED_TIME,
            )
            command = _command(
                idempotency_key=_token(f"thread-idempotency-{suffix}"),
                reference_token=_token(f"thread-reference-{suffix}"),
                asset_token=_token(f"thread-asset-{suffix}"),
            )
            barrier.wait(timeout=5)
            try:
                state = storage.append(OWNER_REF, SNAPSHOT_ID, command)
            except QuestionnaireAssetReviewConflictError:
                result = "conflict"
            except Exception as error:  # pragma: no cover - assertion reports it
                result = f"error:{type(error).__name__}:{error}"
            else:
                result = f"ok:{state.revision}"
            with result_lock:
                results.append(result)

        threads = [
            threading.Thread(target=contender, args=(suffix,))
            for suffix in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(sorted(results), ["conflict", "ok:1"])
        self.assertEqual(self._load().revision, 1)

    def test_many_scopes_release_thread_lock_registry_entries(self) -> None:
        baseline = set(FileQuestionnaireAssetReviewStorage._scope_locks)

        for index in range(100):
            owner_ref = f"email:asset-review-{index}@example.com"
            snapshot_id = f"snapshot_asset_review_{index:03d}"
            self.storage.append(
                owner_ref,
                snapshot_id,
                _command(
                    idempotency_key=_token(f"scope-idempotency-{index}"),
                    reference_token=_token(f"scope-reference-{index}"),
                    asset_token=_token(f"scope-asset-{index}"),
                ),
            )

        self.assertEqual(
            set(FileQuestionnaireAssetReviewStorage._scope_locks),
            baseline,
        )

    def test_process_contenders_for_same_revision_have_one_winner(self) -> None:
        context = multiprocessing.get_context(
            "spawn" if os.name == "nt" else "fork"
        )
        start_event = context.Event()
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_append_in_process,
                args=(str(self.root), start_event, result_queue, suffix),
            )
            for suffix in ("a", "b")
        ]
        try:
            for process in processes:
                process.start()
            start_event.set()
            results = [result_queue.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            result_queue.close()
            result_queue.join_thread()

        self.assertEqual(sorted(results), ["conflict", "ok:1"])
        self.assertEqual(self._load().revision, 1)

    def test_racing_missing_preflight_cannot_persist_reverse_timestamps(self) -> None:
        first_time = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)
        stale_preflight_time = datetime(2026, 8, 17, 7, 0, tzinfo=timezone.utc)
        refreshed_time = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
        first_write_started = threading.Event()
        release_first_write = threading.Event()
        second_preflight_clock_read = threading.Event()
        results: dict[str, object] = {}
        errors: list[Exception] = []
        original_atomic_replace = (
            FileQuestionnaireAssetReviewStorage._atomic_replace_state
        )
        blocked_once = False

        def blocking_atomic_replace(
            cls,
            directory,
            target_name,
            content,
            previous_content,
        ):
            nonlocal blocked_once
            if not blocked_once:
                blocked_once = True
                first_write_started.set()
                if not release_first_write.wait(timeout=5):
                    raise TimeoutError("first write release timed out")
            return original_atomic_replace(
                directory,
                target_name,
                content,
                previous_content,
            )

        second_clock_calls = 0

        def second_clock() -> datetime:
            nonlocal second_clock_calls
            second_clock_calls += 1
            if second_clock_calls == 1:
                second_preflight_clock_read.set()
                return stale_preflight_time
            return refreshed_time

        first_storage = FileQuestionnaireAssetReviewStorage(
            self.root,
            clock=lambda: first_time,
        )
        second_storage = FileQuestionnaireAssetReviewStorage(
            self.root,
            clock=second_clock,
        )

        def append_first() -> None:
            try:
                results["first"] = first_storage.append(
                    OWNER_REF,
                    SNAPSHOT_ID,
                    _command(),
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        def append_second() -> None:
            try:
                results["second"] = second_storage.append(
                    OWNER_REF,
                    SNAPSHOT_ID,
                    _command(
                        expected_revision=1,
                        idempotency_key=_token("timestamp-race-idempotency"),
                    ),
                )
            except QuestionnaireAssetReviewConflictError as error:
                results["second"] = error
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        with patch.object(
            FileQuestionnaireAssetReviewStorage,
            "_atomic_replace_state",
            new=classmethod(blocking_atomic_replace),
        ):
            first_thread = threading.Thread(target=append_first)
            second_thread = threading.Thread(target=append_second)
            first_thread.start()
            try:
                self.assertTrue(first_write_started.wait(timeout=2))
                second_thread.start()
                self.assertTrue(second_preflight_clock_read.wait(timeout=2))
            finally:
                release_first_write.set()
                first_thread.join(timeout=5)
                if second_thread.is_alive():
                    second_thread.join(timeout=5)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(errors, [])
        second_result = results["second"]
        if isinstance(second_result, QuestionnaireAssetReviewConflictError):
            self.assertEqual(self._load().revision, 1)
            return
        state = self._load()
        self.assertEqual(state.revision, 2)
        self.assertGreaterEqual(
            state.events[1].recorded_at,
            state.events[0].recorded_at,
        )
        self.assertEqual(state.events[1].recorded_at, refreshed_time)

    def test_owner_and_snapshot_scopes_are_isolated(self) -> None:
        original = self._append_first()

        other_owner = self._load(owner_ref=OTHER_OWNER_REF)
        other_snapshot = self._load(snapshot_id=OTHER_SNAPSHOT_ID)

        self.assertEqual(other_owner.revision, 0)
        self.assertEqual(other_snapshot.revision, 0)
        self.assertNotEqual(original.owner_scope_key, other_owner.owner_scope_key)
        self.assertNotEqual(
            original.snapshot_storage_key,
            other_snapshot.snapshot_storage_key,
        )
        self.assertFalse(
            self._state_path(owner_ref=OTHER_OWNER_REF).exists()
        )
        self.assertFalse(
            self._state_path(snapshot_id=OTHER_SNAPSHOT_ID).exists()
        )

    def test_copying_state_across_owner_or_snapshot_is_rejected(self) -> None:
        self._append_first()
        source = self._state_path()
        for owner_ref, snapshot_id in (
            (OTHER_OWNER_REF, SNAPSHOT_ID),
            (OWNER_REF, OTHER_SNAPSHOT_ID),
        ):
            with self.subTest(owner_ref=owner_ref, snapshot_id=snapshot_id):
                target = self._state_path(
                    owner_ref=owner_ref,
                    snapshot_id=snapshot_id,
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                target.chmod(0o600)
                with self.assertRaises(QuestionnaireAssetReviewInvalidError):
                    self._load(owner_ref=owner_ref, snapshot_id=snapshot_id)
                target.unlink()

    def test_tampered_envelopes_and_hash_chain_fail_closed(self) -> None:
        self._append_first()
        self.storage.append(
            OWNER_REF,
            SNAPSHOT_ID,
            _command(
                expected_revision=1,
                idempotency_key=_token("idempotency-2"),
            ),
        )
        target = self._state_path()
        original = target.read_bytes()
        document = json.loads(original)

        mutations = {
            "unknown field": lambda value: value.update({"unknown": True}),
            "schema version": lambda value: value.update({"schema_version": 2}),
            "missing schema version": lambda value: value.pop("schema_version"),
            "missing top-level field": lambda value: value.pop(
                "base_package_size_bytes"
            ),
            "owner scope": lambda value: value.update({"owner_scope_key": "0" * 64}),
            "snapshot scope": lambda value: value.update(
                {"snapshot_storage_key": "0" * 64}
            ),
            "base sha": lambda value: value.update(
                {"base_package_sha256": "0" * 64}
            ),
            "base size": lambda value: value.update(
                {"base_package_size_bytes": BASE_PACKAGE_SIZE_BYTES + 1}
            ),
            "revision": lambda value: value.update({"revision": 99}),
            "head hash": lambda value: value.update({"head_event_sha256": "0" * 64}),
            "event revision": lambda value: value["events"][0].update({"revision": 2}),
            "missing event field": lambda value: value["events"][0].pop(
                "reviewer_token"
            ),
            "previous hash": lambda value: value["events"][0].update(
                {"previous_event_sha256": "0" * 64}
            ),
            "later previous hash": lambda value: value["events"][1].update(
                {"previous_event_sha256": "0" * 64}
            ),
            "command hash": lambda value: value["events"][0].update(
                {"command_sha256": "0" * 64}
            ),
            "event hash": lambda value: value["events"][0].update(
                {"event_sha256": "0" * 64}
            ),
            "idempotency token": lambda value: value["events"][0].update(
                {"idempotency_key": "0" * 64}
            ),
            "duplicate idempotency": lambda value: value["events"][1].update(
                {"idempotency_key": value["events"][0]["idempotency_key"]}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = json.loads(json.dumps(document))
                mutate(changed)
                target.write_text(
                    json.dumps(changed, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                tampered = target.read_bytes()
                with self.assertRaises(QuestionnaireAssetReviewInvalidError):
                    self._load()
                with self.assertRaises(QuestionnaireAssetReviewInvalidError):
                    self.storage.append(
                        OWNER_REF,
                        SNAPSHOT_ID,
                        _command(
                            expected_revision=2,
                            idempotency_key=_token("must-not-repair"),
                        ),
                    )
                self.assertEqual(target.read_bytes(), tampered)
                target.write_bytes(original)

    def test_duplicate_json_keys_are_rejected_without_repair(self) -> None:
        self._append_first()
        target = self._state_path()
        original = target.read_text(encoding="utf-8")
        duplicate = original.replace(
            '"schema_version":1',
            '"schema_version":1,"schema_version":1',
            1,
        )
        self.assertNotEqual(duplicate, original)
        target.write_text(duplicate, encoding="utf-8")

        with self.assertRaises(QuestionnaireAssetReviewInvalidError):
            self._load()

    def test_persisted_revision_zero_state_is_rejected_without_repair(self) -> None:
        empty_state = self._load()
        target = self._state_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        persisted = json.dumps(
            empty_state.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        target.write_bytes(persisted)
        target.chmod(0o600)

        with self.assertRaises(QuestionnaireAssetReviewInvalidError):
            self._load()
        with self.assertRaises(QuestionnaireAssetReviewInvalidError):
            self.storage.append(OWNER_REF, SNAPSHOT_ID, _command())
        self.assertEqual(target.read_bytes(), persisted)

    @unittest.skipIf(os.name == "nt", "Windows uses ACLs, not POSIX mode bits")
    def test_group_or_other_readable_state_is_rejected_without_repair(self) -> None:
        self._append_first()
        target = self._state_path()
        persisted = target.read_bytes()
        target.chmod(0o644)

        with self.assertRaises(QuestionnaireAssetReviewInvalidError):
            self._load()
        with self.assertRaises(QuestionnaireAssetReviewInvalidError):
            self.storage.append(
                OWNER_REF,
                SNAPSHOT_ID,
                _command(
                    expected_revision=1,
                    idempotency_key=_token("insecure-mode-idempotency"),
                ),
            )
        self.assertEqual(target.read_bytes(), persisted)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_unknown_and_duplicate_event_keys_are_rejected(self) -> None:
        self._append_first()
        target = self._state_path()
        original = target.read_text(encoding="utf-8")

        document = json.loads(original)
        document["events"][0]["unknown"] = True
        target.write_text(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        with self.assertRaises(QuestionnaireAssetReviewInvalidError):
            self._load()

        target.write_text(original, encoding="utf-8")
        event_asset_token = json.loads(original)["events"][0]["asset_token"]
        field = f'"asset_token":"{event_asset_token}"'
        duplicate = original.replace(field, f"{field},{field}", 1)
        self.assertNotEqual(duplicate, original)
        target.write_text(duplicate, encoding="utf-8")
        with self.assertRaises(QuestionnaireAssetReviewInvalidError):
            self._load()

    def test_symlink_fifo_hardlink_and_oversize_state_files_are_rejected(self) -> None:
        cases = (
            ("hardlink", "oversize")
            if os.name == "nt"
            else ("symlink", "fifo", "hardlink", "oversize")
        )
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(
                    prefix=f"asset-review-{case}-"
                ) as temporary:
                    root = Path(temporary) / "root"
                    storage = FileQuestionnaireAssetReviewStorage(
                        root,
                        clock=lambda: FIXED_TIME,
                    )
                    target = storage._state_path(OWNER_REF, SNAPSHOT_ID)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if case == "symlink":
                        outside = Path(temporary) / "outside.json"
                        outside.write_bytes(b"{}")
                        target.symlink_to(outside)
                    elif case == "fifo":
                        os.mkfifo(target)
                    elif case == "hardlink":
                        storage.append(OWNER_REF, SNAPSHOT_ID, _command())
                        os.link(target, Path(temporary) / "second-link.json")
                    else:
                        target.write_bytes(
                            b"x"
                            * (
                                review_storage.MAX_QUESTIONNAIRE_ASSET_REVIEW_STATE_BYTES
                                + 1
                            )
                        )

                    if case == "fifo" and hasattr(os, "fork"):
                        context = multiprocessing.get_context("fork")
                        result_queue = context.Queue()
                        process = context.Process(
                            target=_load_in_process,
                            args=(str(root), result_queue),
                        )
                        try:
                            process.start()
                            process.join(timeout=3)
                            if process.is_alive():
                                process.terminate()
                                process.join(timeout=3)
                                self.fail("FIFO 状态文件导致读取阻塞")
                            result = result_queue.get(timeout=3)
                            self.assertTrue(result.startswith("error:"), result)
                        finally:
                            if process.is_alive():
                                process.terminate()
                                process.join(timeout=3)
                            result_queue.close()
                            result_queue.join_thread()
                    else:
                        with self.assertRaises(QuestionnaireAssetReviewStorageError):
                            storage.load_state(
                                OWNER_REF,
                                SNAPSHOT_ID,
                                base_package_sha256=BASE_PACKAGE_SHA256,
                                base_package_size_bytes=BASE_PACKAGE_SIZE_BYTES,
                            )

    def test_symlink_fifo_and_hardlink_lock_files_are_rejected(self) -> None:
        cases = ("hardlink",) if os.name == "nt" else ("symlink", "fifo", "hardlink")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(
                    prefix=f"asset-review-lock-{case}-"
                ) as temporary:
                    root = Path(temporary) / "root"
                    storage = FileQuestionnaireAssetReviewStorage(
                        root,
                        clock=lambda: FIXED_TIME,
                    )
                    lock_path = storage._lock_path(OWNER_REF, SNAPSHOT_ID)
                    state_path = storage._state_path(OWNER_REF, SNAPSHOT_ID)
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                    outside = Path(temporary) / "outside-lock"
                    if case == "symlink":
                        outside.write_bytes(b"outside")
                        lock_path.symlink_to(outside)
                    elif case == "fifo":
                        os.mkfifo(lock_path)
                    else:
                        outside.write_bytes(b"outside")
                        os.link(outside, lock_path)

                    with self.assertRaises(QuestionnaireAssetReviewStorageError):
                        storage.append(OWNER_REF, SNAPSHOT_ID, _command())
                    self.assertFalse(state_path.exists())
                    if case != "fifo":
                        self.assertEqual(outside.read_bytes(), b"outside")

    def test_atomic_file_fsync_and_replace_failures_keep_old_state(self) -> None:
        self._append_first()
        target = self._state_path()
        before = target.read_bytes()
        command = _command(
            expected_revision=1,
            idempotency_key=_token("failure-idempotency"),
        )
        for method_name in ("_fsync_file", "_replace_file"):
            with self.subTest(method_name=method_name):
                before_entries = self._tree_entries()
                with patch.object(
                    FileQuestionnaireAssetReviewStorage,
                    method_name,
                    side_effect=OSError("injected failure"),
                ):
                    with self.assertRaises(QuestionnaireAssetReviewStorageError):
                        self.storage.append(OWNER_REF, SNAPSHOT_ID, command)
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(self._tree_entries(), before_entries)
                self._assert_no_temporary_files()

    @unittest.skipIf(os.name == "nt", "Windows cannot fsync directories")
    def test_first_append_fsyncs_each_new_directory_parent_in_order(self) -> None:
        fsynced_directories: list[Path] = []
        original_fsync = FileQuestionnaireAssetReviewStorage._fsync_directory

        def tracking_fsync(descriptor: int) -> None:
            fsynced_directories.append(_descriptor_path(descriptor))
            original_fsync(descriptor)

        with patch.object(
            FileQuestionnaireAssetReviewStorage,
            "_fsync_directory",
            new=staticmethod(tracking_fsync),
        ):
            state = self._append_first()

        owner_directory = self._state_path().parent.resolve()
        self.assertEqual(
            fsynced_directories,
            [
                self.root.parent.resolve(),
                self.root.resolve(),
                (self.root / ".asset-reviews-v1").resolve(),
                owner_directory,
            ],
        )

    @unittest.skipIf(os.name == "nt", "Windows cannot fsync directories")
    def test_new_directory_parent_fsync_failures_precede_lock_and_state(self) -> None:
        for failed_call in (1, 2, 3):
            with self.subTest(failed_call=failed_call):
                with tempfile.TemporaryDirectory(
                    prefix=f"asset-review-parent-fsync-{failed_call}-"
                ) as temporary:
                    root = Path(temporary) / "root"
                    storage = FileQuestionnaireAssetReviewStorage(
                        root,
                        clock=lambda: FIXED_TIME,
                    )
                    original_fsync = (
                        FileQuestionnaireAssetReviewStorage._fsync_directory
                    )
                    call_count = 0

                    def failing_fsync(descriptor: int) -> None:
                        nonlocal call_count
                        call_count += 1
                        if call_count == failed_call:
                            raise OSError("injected parent fsync failure")
                        original_fsync(descriptor)

                    with patch.object(
                        FileQuestionnaireAssetReviewStorage,
                        "_fsync_directory",
                        new=staticmethod(failing_fsync),
                    ):
                        with self.assertRaises(
                            QuestionnaireAssetReviewStorageError
                        ):
                            storage.append(OWNER_REF, SNAPSHOT_ID, _command())

                    state_path = storage._state_path(OWNER_REF, SNAPSHOT_ID)
                    lock_path = storage._lock_path(OWNER_REF, SNAPSHOT_ID)
                    self.assertFalse(state_path.exists())
                    self.assertFalse(lock_path.exists())
                    self.assertEqual(
                        [
                            path
                            for path in root.rglob("*")
                            if path.is_file() or path.is_symlink()
                        ],
                        [],
                    )

    def test_directory_fsync_failure_rolls_back_and_cleans_temporary_files(self) -> None:
        self._append_first()
        target = self._state_path()
        before = target.read_bytes()

        with patch.object(
            FileQuestionnaireAssetReviewStorage,
            "_fsync_directory",
            side_effect=OSError("injected directory fsync failure"),
        ):
            before_entries = self._tree_entries()
            with self.assertRaises(QuestionnaireAssetReviewStorageError):
                self.storage.append(
                    OWNER_REF,
                    SNAPSHOT_ID,
                    _command(
                        expected_revision=1,
                        idempotency_key=_token("directory-fsync-failure"),
                    ),
                )

        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self._tree_entries(), before_entries)
        self._assert_no_temporary_files()

    def test_committed_update_reports_cleanup_unlink_failure_without_temp_leak(
        self,
    ) -> None:
        self._append_first()
        command = _command(
            expected_revision=1,
            idempotency_key=_token("cleanup-unlink-failure"),
        )
        original_unlink = FileQuestionnaireAssetReviewStorage._unlink_file
        failed_once = False

        def failing_once(name: str, directory: int) -> None:
            nonlocal failed_once
            if name.endswith(".tmp") and not failed_once:
                failed_once = True
                raise OSError("injected cleanup unlink failure")
            original_unlink(name, directory)

        with patch.object(
            FileQuestionnaireAssetReviewStorage,
            "_unlink_file",
            new=staticmethod(failing_once),
        ):
            with self.assertRaises(QuestionnaireAssetReviewStorageError):
                self.storage.append(OWNER_REF, SNAPSHOT_ID, command)

        self.assertTrue(failed_once)
        self._assert_no_temporary_files()
        committed = self._load()
        self.assertEqual(committed.revision, 2)
        target = self._state_path()
        before = target.read_bytes()
        replayed = self.storage.append(OWNER_REF, SNAPSHOT_ID, command)
        self.assertEqual(replayed, committed)
        self.assertEqual(target.read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "Windows cannot fsync directories")
    def test_post_cleanup_directory_fsync_failure_cannot_return_success(self) -> None:
        self._append_first()
        command = _command(
            expected_revision=1,
            idempotency_key=_token("cleanup-directory-fsync-failure"),
        )
        owner_directory = self._state_path().parent.resolve()
        original_fsync = FileQuestionnaireAssetReviewStorage._fsync_directory
        owner_fsync_calls = 0

        def fail_second_owner_fsync(descriptor: int) -> None:
            nonlocal owner_fsync_calls
            if _descriptor_path(descriptor) == owner_directory:
                owner_fsync_calls += 1
                if owner_fsync_calls == 2:
                    raise OSError("injected post-cleanup fsync failure")
            original_fsync(descriptor)

        with patch.object(
            FileQuestionnaireAssetReviewStorage,
            "_fsync_directory",
            new=staticmethod(fail_second_owner_fsync),
        ):
            with self.assertRaises(QuestionnaireAssetReviewStorageError):
                self.storage.append(OWNER_REF, SNAPSHOT_ID, command)

        self.assertEqual(owner_fsync_calls, 2)
        self._assert_no_temporary_files()
        committed = self._load()
        self.assertEqual(committed.revision, 2)
        target = self._state_path()
        before = target.read_bytes()
        replayed = self.storage.append(OWNER_REF, SNAPSHOT_ID, command)
        self.assertEqual(replayed, committed)
        self.assertEqual(target.read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "Windows uses ACLs, not POSIX mode bits")
    def test_state_and_lock_files_are_owner_only(self) -> None:
        self._append_first()
        state_path = self._state_path()
        lock_path = self.storage._lock_path(OWNER_REF, SNAPSHOT_ID)
        self.assertTrue(state_path.is_file())
        self.assertTrue(lock_path.is_file())
        self.assertEqual(lock_path.parent, state_path.parent)
        self.assertIn(state_path.stem, lock_path.name)
        files = [path for path in self.root.rglob("*") if path.is_file()]
        self.assertGreaterEqual(len(files), 2)
        for path in files:
            with self.subTest(path=path.name):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_data_guard_keeps_every_created_entry_under_explicit_root(self) -> None:
        self._append_first()

        resolved_root = self.root.resolve(strict=True)
        self.assertEqual(self.storage.root, self.root.absolute())
        for path in self.root.rglob("*"):
            with self.subTest(path=path.name):
                self.assertTrue(path.resolve(strict=True).is_relative_to(resolved_root))

    def test_event_count_limit_rejects_append_without_mutation(self) -> None:
        with patch.object(
            review_storage,
            "MAX_QUESTIONNAIRE_ASSET_REVIEW_EVENTS",
            2,
        ):
            self._append_first()
            self.storage.append(
                OWNER_REF,
                SNAPSHOT_ID,
                _command(
                    expected_revision=1,
                    idempotency_key=_token("limit-idempotency-2"),
                ),
            )
            target = self._state_path()
            before = target.read_bytes()
            with self.assertRaises(QuestionnaireAssetReviewStorageError):
                self.storage.append(
                    OWNER_REF,
                    SNAPSHOT_ID,
                    _command(
                        expected_revision=2,
                        idempotency_key=_token("limit-idempotency-3"),
                    ),
                )
            self.assertEqual(target.read_bytes(), before)

    def test_serialized_size_limit_rejects_append_without_mutation(self) -> None:
        self._append_first()
        target = self._state_path()
        before = target.read_bytes()
        with patch.object(
            review_storage,
            "MAX_QUESTIONNAIRE_ASSET_REVIEW_STATE_BYTES",
            len(before) + 64,
        ):
            with self.assertRaises(QuestionnaireAssetReviewStorageError):
                self.storage.append(
                    OWNER_REF,
                    SNAPSHOT_ID,
                    _command(
                        expected_revision=1,
                        idempotency_key=_token("size-limit-idempotency"),
                    ),
                )
        self.assertEqual(target.read_bytes(), before)

    def test_early_validation_and_explicit_root_fail_without_writes(self) -> None:
        with self.assertRaises(TypeError):
            FileQuestionnaireAssetReviewStorage()  # type: ignore[call-arg]
        invalid_scopes = (
            ("", SNAPSHOT_ID),
            (" ", SNAPSHOT_ID),
            (f" {OWNER_REF}", SNAPSHOT_ID),
            (OWNER_REF, ""),
            (OWNER_REF, " "),
            (OWNER_REF, f"{SNAPSHOT_ID} "),
            ("o" * 100_000, SNAPSHOT_ID),
            (OWNER_REF, "s" * 100_000),
        )
        for owner_ref, snapshot_id in invalid_scopes:
            with self.subTest(owner_ref=owner_ref[:20], snapshot_id=snapshot_id[:20]):
                with self.assertRaises(QuestionnaireAssetReviewStorageError):
                    self.storage.load_state(
                        owner_ref,
                        snapshot_id,
                        base_package_sha256=BASE_PACKAGE_SHA256,
                        base_package_size_bytes=BASE_PACKAGE_SIZE_BYTES,
                    )
                with self.assertRaises(QuestionnaireAssetReviewStorageError):
                    self.storage.append(owner_ref, snapshot_id, _command())
        self.assertFalse(self.root.exists())

    def test_base_size_rejects_bool_zero_and_excessive_values_without_writes(self) -> None:
        command = _command()
        for invalid_size in (True, 0, 2**63):
            with self.subTest(invalid_size=invalid_size):
                with self.assertRaises(ValidationError):
                    QuestionnaireAssetReviewCommand(
                        **{
                            **command.model_dump(),
                            "base_package_size_bytes": invalid_size,
                        }
                    )
                with self.assertRaises(QuestionnaireAssetReviewStorageError):
                    self.storage.load_state(
                        OWNER_REF,
                        SNAPSHOT_ID,
                        base_package_sha256=BASE_PACKAGE_SHA256,
                        base_package_size_bytes=invalid_size,
                    )
        self.assertFalse(self.root.exists())

    def test_invalid_clock_output_fails_before_creating_storage(self) -> None:
        for label, clock_value in (
            ("naive", datetime(2026, 8, 17, 8, 0)),
            ("not datetime", "2026-08-17T08:00:00Z"),
        ):
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory(
                    prefix=f"asset-review-clock-{label}-"
                ) as temporary:
                    root = Path(temporary) / "root"
                    storage = FileQuestionnaireAssetReviewStorage(
                        root,
                        clock=lambda value=clock_value: value,
                    )
                    with self.assertRaises(QuestionnaireAssetReviewStorageError):
                        storage.append(OWNER_REF, SNAPSHOT_ID, _command())
                    self.assertFalse(root.exists())

    def test_symlinked_or_non_directory_scope_components_fail_closed(self) -> None:
        cases = (
            ("root-file",)
            if os.name == "nt"
            else ("root-file", "root-symlink", "namespace-symlink", "owner-symlink")
        )
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(
                    prefix=f"asset-review-scope-{case}-"
                ) as temporary:
                    base = Path(temporary)
                    root = base / "root"
                    outside = base / "outside"
                    outside.mkdir()
                    if case == "root-file":
                        root.write_bytes(b"not a directory")
                    elif case == "root-symlink":
                        root.symlink_to(outside, target_is_directory=True)
                    elif case == "namespace-symlink":
                        root.mkdir()
                        (root / ".asset-reviews-v1").symlink_to(
                            outside,
                            target_is_directory=True,
                        )
                    else:
                        storage = FileQuestionnaireAssetReviewStorage(root)
                        owner_directory = storage._state_path(
                            OWNER_REF,
                            SNAPSHOT_ID,
                        ).parent
                        owner_directory.parent.mkdir(parents=True)
                        owner_directory.symlink_to(outside, target_is_directory=True)

                    storage = FileQuestionnaireAssetReviewStorage(root)
                    with self.assertRaises(QuestionnaireAssetReviewStorageError):
                        storage.load_state(
                            OWNER_REF,
                            SNAPSHOT_ID,
                            base_package_sha256=BASE_PACKAGE_SHA256,
                            base_package_size_bytes=BASE_PACKAGE_SIZE_BYTES,
                        )
                    with self.assertRaises(QuestionnaireAssetReviewStorageError):
                        storage.append(OWNER_REF, SNAPSHOT_ID, _command())
                    self.assertEqual(list(outside.iterdir()), [])

    def test_command_is_strict_frozen_and_bounded(self) -> None:
        command = _command()
        with self.assertRaises(ValidationError):
            command.expected_revision = 2
        with self.assertRaises(ValidationError):
            QuestionnaireAssetReviewCommand(
                **{
                    **command.model_dump(),
                    "expected_revision": True,
                }
            )
        with self.assertRaises(ValidationError):
            QuestionnaireAssetReviewCommand(
                **{
                    **command.model_dump(),
                    "idempotency_key": "not-a-token",
                }
            )

    def test_append_revalidates_constructed_command_before_any_write(self) -> None:
        invalid = QuestionnaireAssetReviewCommand.model_construct(
            expected_revision=True,
            idempotency_key="not-a-token",
            reference_token=_token("constructed-reference"),
            asset_token=_token("constructed-asset"),
            decision=QuestionnaireAssetReviewDecision.CONFIRMED,
            reviewer_token=_token("constructed-reviewer"),
            base_package_sha256=BASE_PACKAGE_SHA256,
            base_package_size_bytes=0,
        )

        with self.assertRaises(QuestionnaireAssetReviewStorageError):
            self.storage.append(OWNER_REF, SNAPSHOT_ID, invalid)
        self.assertFalse(self.root.exists())

    def test_state_with_event_has_strict_json_roundtrip(self) -> None:
        event = QuestionnaireAssetReviewEvent(
            revision=1,
            idempotency_key=_token("roundtrip-idempotency"),
            reference_token=_token("roundtrip-reference"),
            asset_token=_token("roundtrip-asset"),
            decision=QuestionnaireAssetReviewDecision.CONFIRMED,
            reviewer_token=_token("roundtrip-reviewer"),
            recorded_at=FIXED_TIME,
            command_sha256=_token("roundtrip-command"),
            previous_event_sha256=BASE_PACKAGE_SHA256,
            event_sha256=_token("roundtrip-event"),
        )
        state = QuestionnaireAssetReviewState(
            schema_version=1,
            owner_scope_key=_token("roundtrip-owner-scope"),
            snapshot_storage_key=_token("roundtrip-snapshot"),
            base_package_sha256=BASE_PACKAGE_SHA256,
            base_package_size_bytes=BASE_PACKAGE_SIZE_BYTES,
            revision=1,
            head_event_sha256=event.event_sha256,
            events=(event,),
        )

        loaded = QuestionnaireAssetReviewState.model_validate_json(
            state.model_dump_json()
        )

        self.assertEqual(loaded, state)
        missing_schema = state.model_dump(mode="json")
        missing_schema.pop("schema_version")
        with self.assertRaises(ValidationError):
            QuestionnaireAssetReviewState.model_validate(missing_schema)

    def test_state_contract_rejects_duplicate_idempotency_keys(self) -> None:
        first = QuestionnaireAssetReviewEvent(
            revision=1,
            idempotency_key=_token("duplicate-idempotency"),
            reference_token=_token("duplicate-reference-1"),
            asset_token=_token("duplicate-asset-1"),
            decision=QuestionnaireAssetReviewDecision.CONFIRMED,
            reviewer_token=_token("duplicate-reviewer"),
            recorded_at=FIXED_TIME,
            command_sha256=_token("duplicate-command-1"),
            previous_event_sha256=BASE_PACKAGE_SHA256,
            event_sha256=_token("duplicate-event-1"),
        )
        second = QuestionnaireAssetReviewEvent(
            revision=2,
            idempotency_key=first.idempotency_key,
            reference_token=_token("duplicate-reference-2"),
            asset_token=_token("duplicate-asset-2"),
            decision=QuestionnaireAssetReviewDecision.REJECTED,
            reviewer_token=_token("duplicate-reviewer"),
            recorded_at=FIXED_TIME,
            command_sha256=_token("duplicate-command-2"),
            previous_event_sha256=first.event_sha256,
            event_sha256=_token("duplicate-event-2"),
        )

        with self.assertRaises(ValidationError):
            QuestionnaireAssetReviewState(
                schema_version=1,
                owner_scope_key=_token("duplicate-owner-scope"),
                snapshot_storage_key=_token("duplicate-snapshot"),
                base_package_sha256=BASE_PACKAGE_SHA256,
                base_package_size_bytes=BASE_PACKAGE_SIZE_BYTES,
                revision=2,
                head_event_sha256=second.event_sha256,
                events=(first, second),
            )


if __name__ == "__main__":
    unittest.main()
