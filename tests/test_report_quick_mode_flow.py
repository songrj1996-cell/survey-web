"""Integrated quick routing, persistence and availability using synthetic sessions."""
import asyncio
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.routers import survey as survey_router
from app.services import survey_service, report_versions, report_quick_pipeline
from app.storage.prompts import DEFAULT_PROMPTS
from tests.test_report_quick_pipeline import answer
from tests.test_survey_report_versions import _event_payloads


def _draft(ref='1/r1'):
    return json.dumps({'schema_version': 2, 'stage': 'question', 'findings': [
        {'text': '等待影响体验', 'frequency': '部分提及', 'risk': False,
         'evidence_ids': [ref], 'risk_ids': []}], 'empty_reason': ''}, ensure_ascii=False)


def _quick_session(count=3, *, two_questions=False):
    columns = [{'column_indexes': [0], 'name': '玩家 ID', 'role': 'id'},
               {'column_indexes': [1], 'name': 'Q5 体验', 'role': 'open_text'}]
    rows = [['玩家 ID', '体验']] + [[f'p{i}', f'合成原文{i}'] for i in range(count)]
    if two_questions:
        columns.append({'column_indexes': [2], 'name': 'Q8 奖励', 'role': 'open_text'})
        rows[0].append('奖励')
        for row in rows[1:]:
            row.append('奖励不够清楚')
    return {'filename': 'synthetic.xlsx', 'owner_key': 'email:owner@example.com',
            'report_mode': 'quick', 'pending_report_style': 'quick',
            'mode': 'standard', 'analysis_mode': 'qualitative',
            'confirmed_columns': columns, 'rows': rows, 'qualitative_context': {},
            'selected_question_keys': ['1', '2'] if two_questions else ['1']}


@contextmanager
def _quick_runtime(sess, collect, *, enabled=True):
    with ExitStack() as stack:
        stack.enter_context(patch.object(survey_service, 'get_session', return_value=sess))
        stack.enter_context(patch.object(survey_service, '_current_login', new=AsyncMock(return_value={'email': 'owner@example.com'})))
        stack.enter_context(patch.object(survey_service, '_assign_session_owner'))
        def save(_id, value):
            saved = deepcopy(value)
            sess.clear()
            sess.update(saved)
        save_mock = stack.enter_context(patch.object(survey_service, 'save_session', side_effect=save))
        history = stack.enter_context(patch.object(survey_service, 'save_to_history'))
        stack.enter_context(patch.object(survey_service, 'is_quick_report_enabled', return_value=enabled))
        stack.enter_context(patch.object(survey_service, '_get_prompt_text', side_effect=lambda key: DEFAULT_PROMPTS[key]['current']))
        stack.enter_context(patch.object(survey_service, 'collect_chat_completion', new=collect))
        forbidden = [stack.enter_context(patch.object(survey_service, name, side_effect=AssertionError('quick must skip ' + name)))
                     for name in ('_batch_qualitative_analysis', 'build_report_viewpoint_stats', '_direct_writer_round', 'compute_survey_stats')]
        yield {'save': save_mock, 'history': history, 'forbidden': forbidden}


class QuickReportFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_completion_and_qa_share_history_owner_lock(self):
        from tests.test_report_version_history_export import quick_completion_fixture
        archive, _, _ = quick_completion_fixture()
        archive.update(quick_history_rerun=True, rerun_target_history_id=archive['id'])
        owner_lock = survey_service._report_rerun_target_lock(archive['id'])
        await owner_lock.acquire()
        collector = AsyncMock(side_effect=AssertionError('locked request must not call model'))
        try:
            with _quick_runtime(archive, collector), patch.object(survey_service, '_load_history', return_value=[archive]), patch.object(survey_service, '_answer_qa_direct', new=collector), patch('traceback.print_exc'):
                current = _event_payloads([e async for e in survey_service.qa_stream('restored-completion', '合成追问', None, version=1)])
                historical = _event_payloads([e async for e in survey_service.history_qa_stream(archive['id'], '合成追问', [archive], None, version=1)])
                retry = _event_payloads([e async for e in survey_service.report_stream('restored-completion', None, retry_failed=True, base_version=1)])
            for events in (current, historical, retry):
                self.assertTrue(any(e['type'] == 'error' and '正在' in e['message'] for e in events), events)
            collector.assert_not_called()
            self.assertTrue(owner_lock.locked(), 'rejected requests must not release another operation lock')
        finally:
            owner_lock.release()

    async def test_history_commit_recovers_after_session_copy_failure(self):
        import os
        import tempfile
        from app.services import report_history, export_download
        from app.services.report_modes import prepare_report_markdown
        from app.storage import history as history_storage
        sid = 'completion-copy-failure'
        sess = _quick_session(two_questions=True)
        async def first(messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            if payload['question_key'] == '2':
                raise RuntimeError('synthetic failure')
            return answer(payload), 'fake'
        with _quick_runtime(sess, first):
            _ = [e async for e in survey_service.report_stream(sid, None)]
        before = report_versions.resolve_report_version(sess, 1)
        report_versions.update_report_version(sess, 1, qa_messages=[{'role': 'user', 'content': '保留原追问'}])
        archive = {**deepcopy(sess), 'id': sid}
        calls, failed_saves = [], []
        async def finish(messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            calls.append(payload['question_key'])
            return answer(payload), 'fake'
        with tempfile.TemporaryDirectory(prefix='completion-copy-') as folder, patch.object(history_storage, 'HISTORY_FILE', os.path.join(folder, 'history.json')):
            history_storage._save_history([archive])
            with _quick_runtime(sess, finish) as runtime:
                normal_save = runtime['save'].side_effect
                def fail_completed_copy(key, value):
                    if report_versions.resolve_report_version(value, 1).get('quick_completion_revision'):
                        failed_saves.append(key)
                        raise OSError('synthetic session copy failure')
                    normal_save(key, value)
                runtime['save'].side_effect = fail_completed_copy
                events = _event_payloads([e async for e in survey_service.report_stream(sid, None, retry_failed=True, base_version=1)])
                self.assertFalse([e for e in events if e['type'] == 'error'], events)
                self.assertEqual(calls, ['2'])
                self.assertEqual(failed_saves, [sid])
                self.assertTrue(next(e for e in events if e['type'] == 'report_done')['completion'])
                self.assertEqual(report_versions.resolve_report_version(sess, 1)['report_status'], 'partial')
                recovered = survey_service.get_session_report_version(sid, 1)
                self.assertEqual(recovered['report_status'], 'complete')
                source = survey_service._report_source_snapshot(sid, version=1, login={'email': 'owner@example.com'})
                self.assertEqual(source['report_md'], recovered['report_md'])
                self.assertEqual(source['input_snapshot'], before['input_snapshot'])
                self.assertEqual(source['qa_messages'], [{'role': 'user', 'content': '保留原追问'}])
                with patch.object(export_download, 'get_session', return_value=sess):
                    exported, _ = export_download.get_session_export_data(sid, 1, scope='evidence')
                self.assertEqual(exported, prepare_report_markdown(source, 'evidence'))
                self.assertEqual(report_versions.resolve_report_version(sess, 1)['report_status'], 'partial')
                runtime['save'].side_effect = normal_save
                runtime['history'].side_effect = report_history.save_to_history
                seen = []
                async def qa(source, question):
                    seen.append(deepcopy(source))
                    return '合成追问回答', 'fake', source['qa_context_md']
                with patch.object(survey_service, '_answer_qa_direct', side_effect=qa), patch.object(survey_service, 'audit_log', new=AsyncMock()):
                    qa_events = _event_payloads([e async for e in survey_service.qa_stream(sid, '新追问', None, version=1)])
                self.assertTrue(any(e['type'] == 'qa_done' for e in qa_events))
                self.assertEqual(seen[0]['report_md'], recovered['report_md'])
            saved = report_versions.resolve_report_version(history_storage._load_history()[0], 1)
            self.assertEqual(saved['report_status'], 'complete')
            self.assertEqual(len(saved['qa_messages']), 3)
            self.assertEqual(saved['quick_completion_revision'], 1)

    async def test_five_versions_allow_only_failed_completion_with_all_profiles(self):
        sess = _quick_session(two_questions=True)
        for index, name in enumerate(('段位', '场次', '偏好'), start=3):
            sess['confirmed_columns'].append({
                'column_indexes': [index],
                'name': name,
                'role': 'single_choice',
                'use_as_profile': True,
            })
        for i, row in enumerate(sess['rows']):
            row.extend(['Gold' if i % 2 else 'Silver', str(i), '合作'])
        async def first(messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            if payload['question_key'] == '2':
                raise RuntimeError('synthetic failure')
            return answer(payload), 'fake'
        with _quick_runtime(sess, first):
            _ = [e async for e in survey_service.report_stream('five-versions', None)]
        base = report_versions.resolve_report_version(sess, 1)
        for _ in range(4):
            report_versions.append_report_version(sess, deepcopy(base), kind='regenerate', base_version=1)
        others = deepcopy(sess['report_versions'][1:])
        calls = []
        async def finish(messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            calls.append(payload)
            return answer(payload), 'fake'
        with _quick_runtime(sess, finish):
            events = _event_payloads([e async for e in survey_service.report_stream('five-versions', None, retry_failed=True, base_version=1)])
            self.assertFalse([e for e in events if e['type'] == 'error'], events)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]['question_key'], '2')
            expected = base['input_snapshot']['source_questions'][1]['sources']
            self.assertEqual([(r['text'], r['profile']) for r in calls[0]['sources']], [(r['text'], r['profile']) for r in expected])
            self.assertTrue(all(len(r['profile']) == 3 for r in calls[0]['sources']))
            self.assertEqual(len(sess['report_versions']), 5)
            self.assertEqual(sess['report_versions'][1:], others)
            self.assertEqual(report_versions.resolve_report_version(sess, 1)['report_status'], 'complete')
            calls.clear()
            blocked = _event_payloads([e async for e in survey_service.report_stream('five-versions', None, generation_kind='regenerate', base_version=1)])
            self.assertTrue(any('上限' in e.get('message', '') for e in blocked))
            self.assertEqual(calls, [])

    async def test_missing_success_checkpoint_stops_before_any_model_request(self):
        sess = _quick_session(two_questions=True)
        async def first(messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            if payload['question_key'] == '2':
                raise RuntimeError('synthetic failure')
            return answer(payload), 'fake'
        with _quick_runtime(sess, first):
            _ = [e async for e in survey_service.report_stream('missing-success', None)]
        sess['report_versions'][0]['quick_checkpoint']['questions'] = []
        before = deepcopy(sess['report_versions'])
        collector = AsyncMock(side_effect=AssertionError('must not call model'))
        with _quick_runtime(sess, collector):
            events = _event_payloads([e async for e in survey_service.report_stream('missing-success', None, retry_failed=True, base_version=1)])
        collector.assert_not_called()
        self.assertTrue(any('成功题目缓存' in e.get('message', '') for e in events))
        self.assertEqual(sess['report_versions'], before)

    async def test_mixed_report_has_objective_stats_in_order_without_extra_model_calls(self):
        sess = _quick_session()
        sess['confirmed_columns'].insert(1, {'column_indexes': [2], 'name': '评分', 'role': 'scale', 'scale_min': 1, 'scale_max': 5})
        sess['rows'][0].append('评分')
        for row, value in zip(sess['rows'][1:], [1, 3, 5]):
            row.append(value)
        sess['selected_question_keys'] = ['1', '2']
        collector = AsyncMock(return_value=(_draft(), 'fake'))
        with _quick_runtime(sess, collector):
            events = _event_payloads([e async for e in survey_service.report_stream('mixed-stats', None)])
            original = report_versions.resolve_report_version(sess, 1)
            self.assertFalse([e for e in events if e['type'] == 'error'], events)
            self.assertIn('均值: **3.00**', original['report_md'])
            self.assertLess(original['report_md'].index('## 评分'), original['report_md'].index('## Q5 体验'))
            self.assertEqual(collector.await_count, 1)
            self.assertTrue(any(e.get('phase') == 'quick_statistics' and e.get('status') == 'complete' for e in events))
            # A history rerun may have no raw workbook, and must use frozen stats.
            sess.pop('rows')
            events = _event_payloads([e async for e in survey_service.report_stream('mixed-stats', None, generation_kind='regenerate', base_version=1)])
        self.assertFalse([e for e in events if e['type'] == 'error'], events)
        new = report_versions.resolve_report_version(sess, 2)
        self.assertEqual(original['input_snapshot']['objective_stats'], new['input_snapshot']['objective_stats'])
        self.assertEqual(report_versions.resolve_report_version(sess, 1), original)
        self.assertIn('均值: **3.00**', new['report_md'])

    async def test_only_objective_questions_finish_without_model_calls(self):
        sess = _quick_session()
        sess['confirmed_columns'][1].update(role='scale', name='满意评分', scale_min=1, scale_max=5)
        for row, value in zip(sess['rows'][1:], [2, 3, 4]):
            row[1] = value
        collector = AsyncMock(side_effect=AssertionError('objective statistics must not call a model'))
        with _quick_runtime(sess, collector), patch.object(survey_service, 'LLM_QUICK_REPORT_MODEL', ''), patch.object(survey_service, 'LLM_QUICK_REPORT_FALLBACK_MODELS', ()):
            self.assertFalse(survey_service.validate_report_ready('objective-only'))
            events = _event_payloads([e async for e in survey_service.report_stream('objective-only', None)])
        self.assertFalse([e for e in events if e['type'] == 'error'], events)
        self.assertEqual(sess['report_status'], 'complete')
        self.assertIn('均值: **3.00**', sess['report_md'])
        self.assertEqual(sess['quick_summary']['questions'], [])
        collector.assert_not_awaited()

    async def test_cancel_after_statistics_before_subjective_success_can_retry(self):
        sess = _quick_session()
        sess['confirmed_columns'].append({'column_indexes': [2], 'name': '评分', 'role': 'scale'})
        sess['selected_question_keys'].append('2')
        for row in sess['rows']:
            row.append('3')
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        async def consume(**kwargs):
            return _event_payloads([e async for e in survey_service.report_stream('cancel-statistics', None, **kwargs)])
        with _quick_runtime(sess, AsyncMock(side_effect=blocked)):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 3)
            survey_service.cancel_report_run('cancel-statistics')
            events = await asyncio.wait_for(task, 3)
        self.assertTrue(any(e['type'] == 'report_done' for e in events), events)
        first = report_versions.resolve_report_version(sess, 1)
        self.assertEqual(first['report_status'], 'partial')
        self.assertIn('均值: **3.00**', first['report_md'])
        collector = AsyncMock(return_value=(_draft(), 'fake'))
        with _quick_runtime(sess, collector):
            events = await consume(base_version=1, retry_failed=True)
        self.assertFalse([e for e in events if e['type'] == 'error'], events)
        self.assertEqual(sess['report_status'], 'complete')
        self.assertEqual(collector.await_count, 1)
        self.assertEqual(report_versions.resolve_report_version(sess, 1)['input_snapshot'], first['input_snapshot'])
        self.assertEqual(len(report_versions.normalize_report_versions(sess)), 1)

    async def test_small_and_large_skip_old_analysis_writer_and_keep_full_input(self):
        for count in (3, 1201):
            with self.subTest(count=count):
                sess = _quick_session(count)
                sources = []
                async def collect(messages, **kwargs):
                    payload = json.loads(messages[1]['content'])
                    sources.extend(payload.get('sources', []))
                    return answer(payload), 'fake'
                with _quick_runtime(sess, AsyncMock(side_effect=collect)) as runtime:
                    events = _event_payloads([e async for e in survey_service.report_stream('quick-flow', None)])
                    for forbidden in runtime['forbidden']:
                        forbidden.assert_not_called()
                    runtime['history'].assert_called_once()
                self.assertFalse([e for e in events if e['type'] == 'error'], events)
                self.assertTrue(any(e['type'] == 'report_done' for e in events))
                snapshot = report_versions.resolve_report_version(sess)
                self.assertEqual(snapshot['report_mode'], 'quick')
                self.assertEqual(snapshot['report_status'], 'complete')
                self.assertEqual(len({s['response_id'] for s in sources}), count)
                self.assertNotIn('发现与证据附录', snapshot['report_md'])
                self.assertNotIn('stats_md', sess)
                self.assertNotIn('plan', sess)

    async def test_failed_question_retry_completes_same_version_and_keeps_success(self):
        sess = _quick_session(two_questions=True)
        sess['branch_rules'] = [{'parent_index': 0, 'parent_name': '合成分类', 'allowed_options': ['合成人群'],
                                 'confidence': 'high', 'source': 'inferred_from_responses',
                                 'targets': [{'indexes': [1]}, {'indexes': [2]}]}]
        fail = True
        keys = []
        async def collect(messages, **kwargs):
            payload = json.loads(messages[1]['content'])
            keys.append(payload['question_key'])
            if fail and payload['question_key'] == '2':
                raise RuntimeError('synthetic upstream failure')
            return answer(payload), 'fake'
        with _quick_runtime(sess, AsyncMock(side_effect=collect)):
            first_events = _event_payloads([e async for e in survey_service.report_stream('quick-retry', None)])
            self.assertFalse([e for e in first_events if e['type'] == 'error'], first_events)
            first = report_versions.resolve_report_version(sess, 1)
            self.assertEqual(first['report_status'], 'partial')
            first_outline = next(e for e in first_events if e['type'] == 'report_done')['quick_outline']
            self.assertEqual(first_outline['question_keys'], ['1', '2'])
            self.assertNotIn('quick_outline', first, 'outline must not be written into report snapshots')
            fail = False
            keys.clear()
            events = _event_payloads([e async for e in survey_service.report_stream('quick-retry', None, base_version=1, retry_failed=True)])
        self.assertFalse([e for e in events if e['type'] == 'error'], events)
        self.assertEqual(keys, ['2'])
        self.assertEqual(len(report_versions.normalize_report_versions(sess)), 1)
        second = report_versions.resolve_report_version(sess, 1)
        self.assertEqual(second['created_at'], first['created_at'])
        self.assertEqual(second['quick_completion_revision'], 1)
        self.assertEqual(second['report_status'], 'complete')
        self.assertEqual(second['quick_summary']['questions'][0], first['quick_summary']['questions'][0])
        self.assertEqual(next(e for e in events if e['type'] == 'report_done')['quick_outline'], first_outline)
        with patch.object(survey_service, 'get_session', return_value=sess), patch.object(survey_service, '_load_history', return_value=[]):
            selected = survey_service.get_session_report_version('quick-retry', 1)
        self.assertEqual(selected['quick_outline'], first_outline)
        self.assertEqual(selected['report_md'], second['report_md'])

    async def test_twice_invalid_output_is_partial_and_does_not_claim_success(self):
        sess = _quick_session()
        collect = AsyncMock(return_value=('broken', 'fake'))
        with _quick_runtime(sess, collect):
            events = _event_payloads([e async for e in survey_service.report_stream('quick-invalid', None)])
        self.assertEqual(collect.await_count, 2)
        done = next(e for e in events if e['type'] == 'report_done')
        self.assertEqual(done['report_status'], 'partial')
        self.assertEqual(sess['quick_summary']['questions'][0]['error'], 'invalid_structure')
        self.assertEqual(sess['quick_checkpoint']['questions'], [])

    async def test_admin_closed_rejects_before_model_call(self):
        sess = _quick_session()
        collect = AsyncMock()
        with _quick_runtime(sess, collect, enabled=False):
            events = _event_payloads([e async for e in survey_service.report_stream('quick-disabled', None)])
        collect.assert_not_called()
        self.assertTrue(any(e['type'] == 'error' for e in events))
        self.assertNotIn('report_versions', sess)

    async def test_inflight_flag_change_does_not_change_selected_mode(self):
        sess = _quick_session()
        calls = 0
        async def collect(messages, **kwargs):
            nonlocal calls
            calls += 1
            return answer(json.loads(messages[1]['content'])), 'fake'
        with _quick_runtime(sess, AsyncMock(side_effect=collect)), patch.object(survey_service, 'is_quick_report_enabled', side_effect=[True, False]):
            events = _event_payloads([e async for e in survey_service.report_stream('quick-inflight', None)])
        self.assertEqual(calls, 1)
        self.assertTrue(any(e['type'] == 'report_done' and e['report_mode'] == 'quick' for e in events))

    async def test_cancel_stops_actual_model_call_without_new_version(self):
        sess = _quick_session()
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def collect(messages, **kwargs):
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()
        async def consume():
            return _event_payloads([e async for e in survey_service.report_stream('quick-cancel', None)])
        with _quick_runtime(sess, AsyncMock(side_effect=collect)):
            task = asyncio.create_task(consume())
            await entered.wait()
            survey_service._REPORT_CANCEL_EVENTS['quick-cancel'].set()
            events = await task
        self.assertTrue(stopped.is_set())
        self.assertTrue(any(e['type'] == 'cancelled' for e in events))
        self.assertNotIn('report_versions', sess)


class QuickReportEndpointTests(unittest.TestCase):
    def test_mode_selection_is_owner_guarded_before_plan(self):
        app = FastAPI()
        app.include_router(survey_router.router)
        with TestClient(app) as client, patch.object(survey_router, 'require_session_request_access', new=AsyncMock()) as access, patch.object(survey_router, 'set_survey_analysis_settings', return_value={'report_mode': 'quick'}) as update:
            response = client.post('/api/analysis-settings/s', json={'report_mode': 'quick'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['report_mode'], 'quick')
            update.assert_called_once_with('s', None, report_mode='quick')
            access.assert_awaited_once()
            self.assertEqual(client.post('/api/analysis-settings/s', json={'report_mode': 'invalid'}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
