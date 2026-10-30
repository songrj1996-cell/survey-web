"""Persistence failure tests; all files live under the caller's isolated DATA_DIR."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.core import config
from app.core.runtime_logging import bind_runtime_operator, reset_runtime_operator
from app.storage import group_workflow as store


def denied(code=5):
    error = PermissionError(13, "synthetic replacement denial")
    error.winerror = code
    return error


class GroupWorkflowStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=config.DATA_DIR)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'record.json'
        self.old = b'{"confirmed":"previous result"}'
        self.target.write_bytes(self.old)
        self.new = {'confirmed': '新结果', 'manual_quality': {'label': '有效反馈', 'note': '人工结论保留'}}
        self.delays = patch.object(config, 'GROUP_FILE_REPLACE_RETRY_DELAYS_SECONDS', (.05, .1, .2))
        self.delays.start()
        self.addCleanup(self.delays.stop)
        platform = patch.object(store, '_IS_WINDOWS', True)
        platform.start()
        self.addCleanup(platform.stop)

    def test_normal_save_has_no_sleep_or_extra_log_and_retains_json(self):
        with patch.object(store.time, 'sleep') as wait, patch.object(store, 'runtime_log') as log:
            store._write(self.target, self.new)
        wait.assert_not_called()
        log.assert_not_called()
        self.assertEqual(json.loads(self.target.read_text('utf-8')), self.new)
        self.assertEqual(list(self.root.glob('.save-*.tmp')), [])

    def test_transient_windows_denials_retry_the_same_bytes_and_same_temporary_file(self):
        actual_replace = os.replace
        for code in (5, 32):
            with self.subTest(code=code):
                attempts = []
                def replace(source, target):
                    attempts.append((str(source), Path(source).read_bytes()))
                    if len(attempts) <= 2:
                        self.assertEqual(Path(target).read_bytes(), self.old)
                        raise denied(code)
                    return actual_replace(source, target)
                self.target.write_bytes(self.old)
                with patch.object(store.os, 'replace', side_effect=replace), patch.object(store.time, 'sleep') as wait, patch.object(store, 'runtime_log') as log:
                    store._write(self.target, self.new)
                self.assertEqual(len(attempts), 3)
                self.assertEqual(len(set(attempts)), 1)
                self.assertEqual([call.args[0] for call in wait.call_args_list], [.05, .1])
                self.assertEqual(json.loads(self.target.read_text('utf-8')), self.new)
                self.assertEqual([call.args[1] for call in log.call_args_list], ['warning', 'success'])

    def test_permanent_denial_has_four_attempts_350ms_wait_and_old_file_is_unchanged(self):
        error = denied()
        with patch.object(store.os, 'replace', side_effect=error) as replace, patch.object(store.time, 'sleep') as wait, patch.object(store, 'runtime_log') as log:
            with self.assertRaises(PermissionError) as result:
                store._write(self.target, self.new)
        self.assertIs(result.exception, error)
        self.assertEqual(replace.call_count, 4)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [.05, .1, .2])
        self.assertAlmostEqual(sum(call.args[0] for call in wait.call_args_list), .35)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(list(self.root.glob('.save-*.tmp')), [])
        self.assertEqual([call.args[1] for call in log.call_args_list], ['warning', 'error'])

    def test_other_errors_and_other_platforms_are_not_retried(self):
        for windows, error in [(False, denied()), (True, denied(19)), (True, OSError(28, 'synthetic disk full')), (True, FileNotFoundError(2, 'synthetic missing path'))]:
            with self.subTest(windows=windows, error=type(error).__name__):
                with patch.object(store, '_IS_WINDOWS', windows), patch.object(store.os, 'replace', side_effect=error) as replace, patch.object(store.time, 'sleep') as wait, patch.object(store, 'runtime_log'):
                    with self.assertRaises(OSError) as result:
                        store._write(self.target, self.new)
                self.assertIs(result.exception, error)
                self.assertEqual(replace.call_count, 1)
                wait.assert_not_called()
                self.assertEqual(self.target.read_bytes(), self.old)

    def test_disabling_retries_returns_original_error_without_sleep(self):
        with patch.object(config, 'GROUP_FILE_REPLACE_RETRY_DELAYS_SECONDS', ()), patch.object(store.os, 'replace', side_effect=denied()) as replace, patch.object(store.time, 'sleep') as wait, patch.object(store, 'runtime_log'):
            with self.assertRaises(PermissionError):
                store._write(self.target, self.new)
        self.assertEqual(replace.call_count, 1)
        wait.assert_not_called()

    def test_failed_first_save_does_not_leave_a_partial_formal_file(self):
        self.target.unlink()
        with patch.object(store.os, 'replace', side_effect=denied()), patch.object(store.time, 'sleep'), patch.object(store, 'runtime_log'):
            with self.assertRaises(PermissionError):
                store._write(self.target, self.new)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob('.save-*.tmp')), [])

    def test_invalid_json_never_replaces_existing_file(self):
        with patch.object(store.os, 'replace') as replace:
            with self.assertRaises(ValueError):
                store._write(self.target, {'invalid': float('nan')})
        replace.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(list(self.root.glob('.save-*.tmp')), [])

    def test_mutation_callback_is_not_reexecuted_on_save_retry(self):
        actual_replace = os.replace
        with patch.object(config, 'GROUP_WORKFLOW_DATA_DIR', self.root / 'workflow'):
            store.create('tasks', 'synthetic', {'count': 0, 'manual': 'keep'})
            changes = []
            attempts = []
            def change(document):
                changes.append('once')
                document['count'] += 1
                return document
            def replace(source, target):
                attempts.append(str(source))
                if len(attempts) < 3:
                    raise denied()
                return actual_replace(source, target)
            with patch.object(store.os, 'replace', side_effect=replace), patch.object(store.time, 'sleep'), patch.object(store, 'runtime_log'):
                result = store.mutate('tasks', 'synthetic', change)
            self.assertEqual(changes, ['once'])
            self.assertEqual(result, {'count': 1, 'manual': 'keep'})
            self.assertEqual(store.read('tasks', 'synthetic'), result)

    def test_all_group_record_kinds_use_the_same_safe_save(self):
        with patch.object(config, 'GROUP_WORKFLOW_DATA_DIR', self.root / 'workflow'):
            for kind in sorted(store._KINDS):
                with self.subTest(kind=kind):
                    store.create(kind, 'synthetic', {'kind': kind, 'version': 1})
                    result = store.mutate(kind, 'synthetic', lambda doc: doc.update(version=2))
                    self.assertIsNone(result)
                    self.assertEqual(store.read(kind, 'synthetic'), {'kind': kind, 'version': 2})

    def test_retry_keeps_writer_lock_and_does_not_overwrite_a_newer_update(self):
        waiting, release, second_changed = threading.Event(), threading.Event(), threading.Event()
        actual_replace = os.replace
        rejected, errors = [], []
        def replace(source, target):
            if threading.current_thread().name == 'first-writer' and not rejected:
                rejected.append(True)
                raise denied()
            return actual_replace(source, target)
        def wait(_seconds):
            waiting.set()
            if not release.wait(5):
                raise AssertionError('synthetic coordinator did not release writer')
        def write(second):
            try:
                def change(doc):
                    if second:
                        second_changed.set()
                    doc['count'] += 1
                store.mutate('tasks', 'synthetic', change)
            except Exception as error:
                errors.append(error)
        with patch.object(config, 'GROUP_WORKFLOW_DATA_DIR', self.root / 'workflow'):
            store.create('tasks', 'synthetic', {'count': 0})
            with patch.object(store.os, 'replace', side_effect=replace), patch.object(store.time, 'sleep', side_effect=wait), patch.object(store, 'runtime_log'):
                first = threading.Thread(target=write, args=(False,), name='first-writer')
                second = threading.Thread(target=write, args=(True,), name='second-writer')
                first.start()
                try:
                    self.assertTrue(waiting.wait(5))
                    second.start()
                    self.assertFalse(second_changed.wait(.05), 'second mutation must wait for the original save')
                finally:
                    release.set()
                    first.join(5)
                    if second.ident is not None:
                        second.join(5)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(store.read('tasks', 'synthetic'), {'count': 2})

    def test_cleanup_error_does_not_hide_original_save_error(self):
        error = denied()
        actual_unlink = os.unlink
        with patch.object(store.os, 'replace', side_effect=error), patch.object(store.os, 'unlink', side_effect=denied(32)), patch.object(store.time, 'sleep'), patch.object(store, 'runtime_log') as log:
            with self.assertRaises(PermissionError) as result:
                store._write(self.target, self.new)
        self.assertIs(result.exception, error)
        self.assertEqual(self.target.read_bytes(), self.old)
        leftovers = list(self.root.glob('.save-*.tmp'))
        self.assertEqual(len(leftovers), 1)
        self.assertIn('临时保存文件未能清理', log.call_args.args[2])
        for path in leftovers:
            actual_unlink(path)

    def test_retry_logs_include_timezone_and_name_without_payload_or_account(self):
        actual_replace = os.replace
        calls = []
        def replace(source, target):
            calls.append(source)
            if len(calls) == 1:
                raise denied()
            return actual_replace(source, target)
        output = io.StringIO()
        token = bind_runtime_operator('合成操作者')
        try:
            with redirect_stdout(output), patch.object(store.os, 'replace', side_effect=replace), patch.object(store.time, 'sleep'):
                store._write(self.target, self.new)
        finally:
            reset_runtime_operator(token)
        text = output.getvalue()
        self.assertIn('合成操作者', text)
        self.assertIn('重试保存成功', text)
        self.assertNotIn('人工结论保留', text)
        self.assertNotIn('@', text)
        self.assertRegex(text, r'\[\d{4}-\d{2}-\d{2} [^\]]+[+-]\d{2}:\d{2}\]')

    def test_configuration_defaults_overrides_and_hard_wait_limit(self):
        code = 'import json; from app.core import config; print(json.dumps(config.GROUP_FILE_REPLACE_RETRY_DELAYS_SECONDS))'
        for overrides, expected in [({}, [.05, .1, .2]), ({'GROUP_FILE_REPLACE_RETRY_COUNT': '99', 'GROUP_FILE_REPLACE_RETRY_BASE_MS': '999'}, [.05, .1, .2]), ({'GROUP_FILE_REPLACE_RETRY_COUNT': '0'}, []), ({'GROUP_FILE_REPLACE_RETRY_COUNT': '2', 'GROUP_FILE_REPLACE_RETRY_BASE_MS': '10'}, [.01, .02]), ({'GROUP_FILE_REPLACE_RETRY_COUNT': 'bad', 'GROUP_FILE_REPLACE_RETRY_BASE_MS': 'bad'}, [.05, .1, .2])]:
            env = {key: value for key, value in os.environ.items() if not key.startswith('GROUP_FILE_REPLACE_RETRY_')}
            env.update(overrides)
            env['DATA_DIR'] = str(self.root / 'config-data')
            values = json.loads(subprocess.check_output([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1], env=env, text=True, encoding='utf-8'))
            self.assertEqual(values, expected)
            self.assertLessEqual(sum(values), .350000001)

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows file-sharing semantics')
    def test_real_windows_reader_release_allows_retry_without_regenerating_results(self):
        opened, release = threading.Event(), threading.Event()
        actual_replace = os.replace
        rejections = []
        def read_while_saving():
            with self.target.open('rb'):
                opened.set()
                release.wait(5)
                time.sleep(.02)
        reader = threading.Thread(target=read_while_saving)
        reader.start()
        self.assertTrue(opened.wait(5))
        def replace(source, target):
            try:
                return actual_replace(source, target)
            except PermissionError as error:
                rejections.append(error.winerror)
                release.set()
                raise
        try:
            with patch.object(store.os, 'replace', side_effect=replace):
                store._write(self.target, self.new)
        finally:
            release.set()
            reader.join(5)
        self.assertFalse(reader.is_alive())
        self.assertTrue(rejections)
        self.assertTrue(all(code in (5, 32) for code in rejections))
        self.assertEqual(json.loads(self.target.read_text('utf-8')), self.new)

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows file-sharing semantics')
    def test_real_persistent_windows_reader_preserves_old_file_and_reports_failure(self):
        with self.target.open('rb'), patch.object(store, 'runtime_log'):
            with self.assertRaises(PermissionError):
                store._write(self.target, self.new)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(list(self.root.glob('.save-*.tmp')), [])


    def test_backed_mutation_keeps_exact_original_bytes_and_idempotent_retry(self):
        import hashlib
        with patch.object(config, 'GROUP_WORKFLOW_DATA_DIR', self.root/'workflow'):
            original = {'batches': {'old': {'status': 'pending'}}, 'claims': {'task:old': 'old'}}
            store.create('ledger', 'payouts', original)
            path = self.root/'workflow'/'ledger'/'payouts.json'
            raw = path.read_bytes()
            def change(document):
                document['batches']['old']['status'] = 'voided'
                document['claims'].clear()
                return document
            result = store.mutate_with_backup('ledger', 'payouts', change)
            backups = list((self.root/'workflow'/'backups'/'ledger'/'payouts').glob('*.json'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), raw)
            self.assertIn(hashlib.sha256(raw).hexdigest(), backups[0].name)
            self.assertEqual(json.loads(backups[0].read_text('utf-8')), original)
            self.assertEqual(store.read('ledger', 'payouts'), result)
            store.mutate_with_backup('ledger', 'payouts', change)
            self.assertEqual(len(list(backups[0].parent.glob('*.json'))), 1)

    def test_corrupt_backup_and_failed_replace_keep_original_record(self):
        with patch.object(config, 'GROUP_WORKFLOW_DATA_DIR', self.root/'workflow'):
            original = {'status': 'pending', 'claims': ['preserved']}
            store.create('ledger', 'payouts', original)
            path = self.root/'workflow'/'ledger'/'payouts.json'
            raw = path.read_bytes()
            actual_read = Path.read_bytes
            def read_bytes(candidate):
                return b'{"corrupt":true}' if 'backups' in candidate.parts else actual_read(candidate)
            with patch.object(Path, 'read_bytes', read_bytes):
                with self.assertRaises(OSError):
                    store.mutate_with_backup('ledger', 'payouts', lambda d: d.update(status='voided'))
            self.assertEqual(path.read_bytes(), raw)
            with patch.object(store, '_write', side_effect=OSError('synthetic full disk')):
                with self.assertRaises(OSError):
                    store.mutate_with_backup('ledger', 'payouts', lambda d: d.update(status='paid'))
            self.assertEqual(path.read_bytes(), raw)
            backups = list((self.root/'workflow'/'backups'/'ledger'/'payouts').glob('*.json'))
            self.assertTrue(all(backup.read_bytes() == raw for backup in backups))


    def test_long_windows_backup_path_is_shortened_and_verified_snapshot_is_reused(self):
        import hashlib
        suffix=Path('backups')/'ledger'/'payouts'/('f'*64+'.json')
        width=max(1,252-len(str(self.root/'x'/suffix))+1)
        directory=self.root/('x'*width)
        with patch.object(config,'GROUP_WORKFLOW_DATA_DIR',directory):
            original={'status':'pending','claims':['preserved']}
            store.create('ledger','payouts',original)
            path=directory/'ledger'/'payouts.json'
            raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
            backup=directory/'backups'/'ledger'/'payouts'/(digest+'.json')
            self.assertLessEqual(len(str(backup)),252)
            self.assertGreater(len(str(backup.parent/('f'*32+'-'+digest+'.json'))),260)
            with patch.object(store,'_write',side_effect=FileNotFoundError('synthetic save failure')):
                with self.assertRaises(FileNotFoundError):
                    store.mutate_with_backup('ledger','payouts',lambda d:d.update(status='paid'))
            self.assertEqual(path.read_bytes(),raw)
            self.assertEqual(backup.read_bytes(),raw)
            saved=store.mutate_with_backup('ledger','payouts',lambda d:(d.update(status='paid') or d))
            self.assertEqual(saved['status'],'paid')
            self.assertEqual(len(list(backup.parent.glob('*.json'))),1,'retry reuses the fully verified snapshot')

    def test_existing_corrupt_content_addressed_backup_blocks_mutation(self):
        import hashlib
        directory=self.root/'workflow'
        with patch.object(config,'GROUP_WORKFLOW_DATA_DIR',directory):
            store.create('ledger','payouts',{'status':'pending'})
            path=directory/'ledger'/'payouts.json';raw=path.read_bytes()
            backup=directory/'backups'/'ledger'/'payouts'/(hashlib.sha256(raw).hexdigest()+'.json')
            backup.parent.mkdir(parents=True);backup.write_text('{"corrupt":true}',encoding='utf-8')
            with self.assertRaises(OSError):
                store.mutate_with_backup('ledger','payouts',lambda d:d.update(status='paid'))
            self.assertEqual(path.read_bytes(),raw)
