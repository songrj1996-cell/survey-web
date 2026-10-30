import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from test_group_tasks import WorkflowCase, LOGIN, snapshot, entry
from app.core import config
from app.schemas.group_tasks import RunControlRequest, RunTask
from app.services import group_task_runner as runner
from app.storage import group_workflow as store


class RunnerTests(unittest.IsolatedAsyncioTestCase, WorkflowCase):
    def start_for_control(self, task):
        with patch.object(runner, 'require_background_llm_api_key', return_value='test-key'):
            started = runner.start_run(task['id'], RunTask(expected_version=task['version']), LOGIN)
        return RunControlRequest(expected_version=task['version'], run_id=started['run']['id'])

    async def test_queued_pause_is_durable_idempotent_and_resumes_same_budget_and_snapshot(self):
        task = self.make_task(labels=(None, None))
        control = self.start_for_control(task)
        paused = runner.pause_run(task['id'], control, LOGIN)
        self.assertEqual(paused['status'], 'paused')
        self.assertNotIn('pause_login', paused['run'])
        self.assertNotIn('owner_login', paused['run'])
        first = store.read('tasks', task['id'])
        runner.pause_run(task['id'], control, LOGIN)
        self.assertEqual(store.read('tasks', task['id']), first)
        runner.recover_interrupted_runs()
        self.assertEqual(store.read('tasks', task['id']), first)
        called = []
        async def unexpected(*args):
            called.append(args)
        with patch.object(runner, 'annotate_group_feedback', unexpected):
            await runner._run_slice(task['id'], asyncio.Semaphore(1))
        self.assertEqual(called, [])
        with patch.object(runner, 'require_background_llm_api_key', return_value='test-key'):
            resumed = await runner.resume_run(task['id'], control, LOGIN)
            repeated = await runner.resume_run(task['id'], control, LOGIN)
        self.assertEqual(resumed['status'], 'queued')
        self.assertEqual(repeated['run'], resumed['run'])
        self.assertEqual(resumed['run']['id'], control.run_id)
        self.assertEqual(resumed['run']['http_attempts'], first['run']['http_attempts'])
        self.assertEqual(resumed['run']['target_ids'], first['run']['target_ids'])
        self.assertEqual(resumed['run'].get('paused_seconds', 0), 0, 'pause before first start must not enlarge the execution budget')

    async def test_running_pause_saves_current_feedback_and_resume_does_not_repeat_success(self):
        task = self.make_task(labels=('优秀反馈', None, None))
        task['feedback'][0]['manual_quality'] = {'label': '有效反馈', 'note': '人工保留'}
        store.mutate('tasks', task['id'], lambda current: current.update(task))
        control = self.start_for_control(task)
        entered, release = asyncio.Event(), asyncio.Event()
        processed = []
        async def annotate(current, row, checkpoint):
            processed.append(row['id'])
            if len(processed) == 1:
                entered.set()
                await release.wait()
            checkpoint({'quality': {'overall': '有效反馈', 'overall_reason': '合成验证', 'q_labels': {'col_1': '有效反馈'}}, 'quality_complete': True})
        with patch.object(runner, 'require_background_llm_api_key', return_value='test-key'), patch.object(runner, 'start_llm_usage_task', return_value=None), patch.object(runner, 'annotate_group_feedback', annotate), patch.object(config, 'GROUP_TASK_REQUEST_CONCURRENCY', 1):
            executing = asyncio.create_task(runner._run_slice(task['id'], asyncio.Semaphore(1)))
            try:
                async with asyncio.timeout(5):
                    await entered.wait()
                self.assertEqual(runner.pause_run(task['id'], control, LOGIN)['status'], 'pausing')
                self.assertEqual(runner.pause_run(task['id'], control, LOGIN)['status'], 'pausing')
                with self.assertRaises(Exception) as early:
                    await runner.resume_run(task['id'], control, LOGIN)
                self.assertEqual(early.exception.status_code, 409)
            finally:
                release.set()
                await executing
            paused = store.read('tasks', task['id'])
            self.assertEqual(paused['run']['status'], 'paused')
            self.assertEqual(len(processed), 1)
            self.assertTrue(paused['feedback'][1]['quality_complete'])
            self.assertEqual(paused['feedback'][0]['manual_quality'], task['feedback'][0]['manual_quality'])
            saved_models = deepcopy(paused['run']['llm_model_snapshot'])
            await runner.resume_run(task['id'], control, LOGIN)
            await runner._run_slice(task['id'], asyncio.Semaphore(1))
        completed = store.read('tasks', task['id'])
        self.assertEqual(completed['run']['status'], 'completed')
        self.assertEqual(processed, [task['feedback'][1]['id'], task['feedback'][2]['id']])
        current_models = completed['run']['llm_model_snapshot']
        for key in ('version', 'revision', 'configuration_sha256', 'configuration'):
            self.assertEqual(current_models[key], saved_models[key])
        self.assertEqual(current_models.get('use_saved_defaults', False), saved_models.get('use_saved_defaults', False))
        self.assertEqual(completed['run']['id'], control.run_id)
        self.assertEqual(completed['feedback'][0]['manual_quality'], task['feedback'][0]['manual_quality'])

    async def test_pause_wait_is_excluded_but_attempt_limit_is_preserved(self):
        task = self.make_task(labels=(None,))
        control = self.start_for_control(task)
        runner.pause_run(task['id'], control, LOGIN)
        def long_pause(current):
            current['run'].update(started_at=(datetime.now(timezone.utc)-timedelta(hours=2, seconds=5)).isoformat(),
                                  paused_at=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(), http_attempts=2)
        store.mutate('tasks', task['id'], long_pause)
        with patch.object(runner, 'require_background_llm_api_key', return_value='test-key'):
            resumed = await runner.resume_run(task['id'], control, LOGIN)
        self.assertGreater(resumed['run']['paused_seconds'], 7199)
        self.assertLess(runner._run_elapsed(resumed['run']), 10)
        with patch.object(config, 'GROUP_RUN_MAX_HTTP_ATTEMPTS', 2):
            with self.assertRaisesRegex(RuntimeError, '请求预算'):
                runner._attempt_reservation(task['id'], control.run_id, task['version'])
        self.assertEqual(store.read('tasks', task['id'])['run']['http_attempts'], 2)

    async def test_restart_during_pause_keeps_pause_and_uncertain_request_warning(self):
        task = self.make_task(labels=('有效反馈', None))
        control = self.start_for_control(task)
        store.mutate('tasks', task['id'], lambda current: current['run'].update(status='running', inflight_ids=[task['feedback'][1]['id']]))
        runner.pause_run(task['id'], control, LOGIN)
        runner.recover_interrupted_runs()
        saved = store.read('tasks', task['id'])
        self.assertEqual(saved['run']['status'], 'paused')
        self.assertTrue(saved['run']['timing_partial'])
        self.assertIn('重复模型费用', saved['run']['error'])
        self.assertTrue(saved['feedback'][0]['quality_complete'])
        with self.assertRaises(RuntimeError):
            runner._fenced(saved, control.run_id, task['version'])

    async def test_ai_pause_blocks_automatic_quality_and_resume_continues_stage(self):
        task = self.make_task(labels=(None,))
        def enable_ai(current):
            current['rules']['ai_detect'] = True
            current['annotation_options'] = {'ai_detect': True, 'quality': True, 'background': ''}
        store.mutate('tasks', task['id'], enable_ai)
        task = store.read('tasks', task['id'])
        control = self.start_for_control(task)
        async def annotate(current, row, checkpoint):
            checkpoint({'ai': {'ai_prob': 0, 'reason': '合成验证'}, 'ai_complete': True})
            runner.pause_run(task['id'], control, LOGIN)
        with patch.object(runner, 'require_background_llm_api_key', return_value='test-key'), patch.object(runner, 'start_llm_usage_task', return_value=None), patch.object(runner, 'annotate_group_feedback', annotate):
            await runner._run_slice(task['id'], asyncio.Semaphore(1))
            paused = store.read('tasks', task['id'])
            self.assertEqual(paused['run']['status'], 'paused')
            self.assertEqual(paused['run']['stage'], 'ai')
            self.assertFalse(paused['ai_confirmation_complete'])
            await runner.resume_run(task['id'], control, LOGIN)
            await runner._run_slice(task['id'], asyncio.Semaphore(1))
        resumed = store.read('tasks', task['id'])
        self.assertTrue(resumed['ai_confirmation_complete'])
        self.assertEqual(resumed['run']['stage'], 'quality')
        self.assertEqual(resumed['run']['status'], 'queued')
        for key in ('version', 'configuration_sha256', 'configuration'):
            self.assertEqual(resumed['run']['llm_model_snapshot'][key], paused['run']['llm_model_snapshot'][key])

    async def test_resume_validates_saved_models_without_loading_new_published_settings(self):
        from app.services import llm_model_settings
        from unittest.mock import AsyncMock
        task = self.make_task(labels=(None,))
        with patch.object(runner, 'require_background_llm_api_key', return_value='test-key'):
            started = await runner.prepare_run(task['id'], RunTask(expected_version=task['version']), LOGIN)
            control = RunControlRequest(expected_version=task['version'], run_id=started['run']['id'])
            runner.pause_run(task['id'], control, LOGIN)
            saved = store.read('tasks', task['id'])['run']['llm_model_snapshot']
            with patch.object(llm_model_settings, 'build_model_snapshot', AsyncMock(side_effect=AssertionError('must not load a replacement model setting'))):
                resumed = await runner.resume_run(task['id'], control, LOGIN)
        self.assertEqual(resumed['run']['llm_model_snapshot']['configuration_sha256'], saved['configuration_sha256'])

    async def test_pause_after_ai_completion_still_blocks_pending_automatic_transition(self):
        task = self.make_task(labels=(None,))
        def enable_ai(current):
            current['rules']['ai_detect'] = True
            current['annotation_options'] = {'ai_detect': True, 'quality': True, 'background': ''}
        store.mutate('tasks', task['id'], enable_ai)
        task = store.read('tasks', task['id'])
        control = self.start_for_control(task)
        original_mutate = store.mutate
        async def annotate(current, row, checkpoint):
            checkpoint({'ai': {'ai_prob': 0, 'reason': '合成验证'}, 'ai_complete': True})
        def intercept(kind, identifier, change, **kwargs):
            result = original_mutate(kind, identifier, change, **kwargs)
            # Insert the request exactly after finish writes completed, before auto-confirm.
            saved = store.read('tasks', task['id'])
            if getattr(change, '__name__', '') == 'finish' and saved['run']['status'] == 'completed':
                runner.pause_run(task['id'], control, LOGIN)
            return result
        with patch.object(runner, 'require_background_llm_api_key', return_value='test-key'), patch.object(runner, 'start_llm_usage_task', return_value=None), patch.object(runner, 'annotate_group_feedback', annotate), patch.object(store, 'mutate', side_effect=intercept):
            await runner._run_slice(task['id'], asyncio.Semaphore(1))
        saved = store.read('tasks', task['id'])
        self.assertEqual(saved['run']['status'], 'paused')
        self.assertEqual(saved['run']['stage'], 'ai')
        self.assertFalse(saved['ai_confirmation_complete'])

    async def test_paused_task_does_not_consume_scheduler_slot_or_block_another_task(self):
        paused_task, other = self.make_task(labels=(None,)), self.make_task(labels=(None,))
        control = self.start_for_control(paused_task)
        runner.pause_run(paused_task['id'], control, LOGIN)
        self.start_for_control(other)
        processed = []
        async def annotate(current, row, checkpoint):
            processed.append(current['id'])
            checkpoint({'quality': {'overall': '有效反馈', 'overall_reason': '合成验证', 'q_labels': {'col_1': '有效反馈'}}, 'quality_complete': True})
        with patch.object(runner, 'require_background_llm_api_key', return_value='test-key'), patch.object(runner, 'start_llm_usage_task', return_value=None), patch.object(runner, 'annotate_group_feedback', annotate), patch.object(config, 'GROUP_TASK_CONCURRENCY', 1):
            scheduler = asyncio.create_task(runner._scheduler())
            try:
                async with asyncio.timeout(5):
                    while store.read('tasks', other['id'])['run']['status'] != 'completed':
                        await asyncio.sleep(.02)
            finally:
                scheduler.cancel()
                await asyncio.gather(scheduler, return_exceptions=True)
        self.assertEqual(processed, [other['id']])
        self.assertEqual(store.read('tasks', paused_task['id'])['run']['status'], 'paused')

    async def test_pause_logs_have_timezone_and_name_without_account_identifiers(self):
        import io
        from contextlib import redirect_stdout
        task = self.make_task(labels=(None,))
        control = self.start_for_control(task)
        output = io.StringIO()
        with redirect_stdout(output):
            runner.pause_run(task['id'], control, LOGIN)
            with patch.object(runner, 'require_background_llm_api_key', return_value='test-key'):
                await runner.resume_run(task['id'], control, LOGIN)
        text = output.getvalue()
        self.assertIn('任务已暂停', text)
        self.assertIn('任务已继续', text)
        self.assertIn(LOGIN['name'], text)
        self.assertNotIn(LOGIN['email'], text)
        self.assertRegex(text, r'\[\d{4}-\d{2}-\d{2} [^\]]+[+-]\d{2}:\d{2}\]')

    async def test_completed_quality_wins_over_last_feedback_pause_request(self):
        task = self.make_task(labels=(None,))
        control = self.start_for_control(task)
        async def annotate(current, row, checkpoint):
            runner.pause_run(task['id'], control, LOGIN)
            checkpoint({'quality': {'overall': '有效反馈', 'overall_reason': '合成验证', 'q_labels': {'col_1': '有效反馈'}}, 'quality_complete': True})
        with patch.object(runner, 'require_background_llm_api_key', return_value='test-key'), patch.object(runner, 'start_llm_usage_task', return_value=None), patch.object(runner, 'annotate_group_feedback', annotate):
            await runner._run_slice(task['id'], asyncio.Semaphore(1))
        completed = store.read('tasks', task['id'])
        self.assertEqual(completed['run']['status'], 'completed')
        self.assertFalse(completed['run']['pause_requested'])

    async def test_control_rejects_stale_version_run_readonly_and_edit_while_paused(self):
        from fastapi import HTTPException
        from app.services import group_tasks as tasks
        task = self.make_task(labels=(None,))
        control = self.start_for_control(task)
        for request in [RunControlRequest(expected_version=task['version']+1, run_id=control.run_id), RunControlRequest(expected_version=task['version'], run_id='different-run')]:
            with self.assertRaises(HTTPException) as rejected:
                runner.pause_run(task['id'], request, LOGIN)
            self.assertEqual(rejected.exception.status_code, 409)
        with patch.object(runner, 'require_permission', side_effect=HTTPException(403, '只读账号')):
            with self.assertRaises(HTTPException) as denied:
                runner.pause_run(task['id'], control, LOGIN)
            self.assertEqual(denied.exception.status_code, 403)
        runner.pause_run(task['id'], control, LOGIN)
        with self.assertRaises(HTTPException):
            tasks._ensure_version(store.read('tasks', task['id']), task['version'])
        with patch.object(runner, 'require_background_llm_api_key', return_value='test-key'):
            with self.assertRaisesRegex(HTTPException, '继续任务'):
                runner.start_run(task['id'], RunTask(expected_version=task['version']), LOGIN)
        with patch.object(runner, 'require_background_llm_api_key', side_effect=HTTPException(403, '凭据不可用')):
            with self.assertRaises(HTTPException):
                await runner.resume_run(task['id'], control, LOGIN)
        self.assertEqual(store.read('tasks', task['id'])['run']['status'], 'paused')

    async def test_control_endpoints_validate_target_and_show_persistent_state(self):
        import httpx
        from fastapi import FastAPI
        from unittest.mock import AsyncMock
        from app.routers import group_tasks as routes
        task = self.make_task(labels=(None,))
        control = self.start_for_control(task)
        app = FastAPI()
        app.include_router(routes.router)
        with patch.object(routes, '_current_login', AsyncMock(return_value=LOGIN)), patch.object(runner, 'require_background_llm_api_key', return_value='test-key'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated.test') as client:
                response = await client.post('/api/group-tasks/'+task['id']+'/pause', json={'expected_version': task['version']})
                self.assertEqual(response.status_code, 422)
                paused = await client.post('/api/group-tasks/'+task['id']+'/pause', json=control.model_dump())
                self.assertEqual(paused.status_code, 200)
                self.assertEqual(paused.json()['status'], 'paused')
                listing = await client.get('/api/group-tasks')
                self.assertEqual(listing.json()['tasks'][0]['status'], 'paused')
                resumed = await client.post('/api/group-tasks/'+task['id']+'/resume', json=control.model_dump())
                self.assertEqual(resumed.status_code, 200)
                self.assertEqual(resumed.json()['run']['id'], control.run_id)

    async def test_control_storage_failure_does_not_report_success_or_lose_results(self):
        from fastapi import HTTPException
        task = self.make_task(labels=('优秀反馈', None))
        control = self.start_for_control(task)
        before = store.read('tasks', task['id'])
        with patch.object(store, 'mutate', side_effect=PermissionError('synthetic write failure')):
            with self.assertRaises(HTTPException) as rejected:
                runner.pause_run(task['id'], control, LOGIN)
        self.assertEqual(rejected.exception.status_code, 503)
        self.assertEqual(store.read('tasks', task['id']), before)
        runner.pause_run(task['id'], control, LOGIN)
        with patch.object(runner.tasks, 'load_task', side_effect=PermissionError('synthetic read failure')):
            with self.assertRaises(HTTPException) as rejected:
                await runner.resume_run(task['id'], control, LOGIN)
        self.assertEqual(rejected.exception.status_code, 503)
        self.assertEqual(store.read('tasks', task['id'])['run']['status'], 'paused')

    async def test_admission_never_reads_roster_and_repeated_start_is_idempotent(self):
        from fastapi import HTTPException
        from unittest.mock import AsyncMock
        from app.services import roster_sources
        task=self.make_task(labels=(None,))
        request=RunTask(expected_version=task['version'])
        with patch.object(roster_sources,'read_roster_snapshot',AsyncMock(side_effect=HTTPException(409,'测试来源不可读'))) as read,patch.object(runner,'require_background_llm_api_key',return_value='test-key'):
            result=await runner.prepare_run(task['id'],request,LOGIN)
            repeated=await runner.prepare_run(task['id'],request,LOGIN)
            self.assertEqual(result['run']['id'],repeated['run']['id'])
            read.assert_not_awaited()
        self.assertEqual(result['version'],task['version'])
        self.assertEqual(result['roster_snapshot']['id'],task['roster_snapshot']['id'])

    async def test_below_count_gate_never_reaches_model_adapter(self):
        task=self.make_task(labels=(None,None),requires_count=True,counts=[2,3])
        called=[]
        async def annotate(current,row,checkpoint):
            called.append(row['id'])
            checkpoint({'quality':{'overall':'有效反馈','overall_reason':'达到次数门槛','q_labels':{'col_1':'有效反馈'}},'quality_complete':True})
        with patch.object(runner,'require_background_llm_api_key',return_value='test-key'),patch.object(runner,'start_llm_usage_task',return_value=None),patch.object(runner,'annotate_group_feedback',annotate):
            runner.start_run(task['id'],RunTask(expected_version=task['version']),LOGIN)
            await runner._run_slice(task['id'],asyncio.Semaphore(2))
        self.assertEqual(called,[task['feedback'][1]['id']])
        saved=store.read('tasks',task['id'])
        self.assertEqual(saved['run']['status'],'completed')
        self.assertEqual(saved['run']['target_ids'],called)

    async def test_background_annotation_logs_use_group_workflow_area_only_during_run(self):
        from app.services import annotate_workflow as annotate

        task = self.make_task(labels=(None,))
        areas = []

        async def annotate_one(current, row, checkpoint):
            annotate._annotate_ai_log(
                "direct model done",
                task="quality-1-initial",
                model="test-model",
                attempt=1,
            )
            checkpoint({
                "quality": {
                    "overall": "有效反馈",
                    "overall_reason": "合成验证",
                    "q_labels": {"col_1": "有效反馈"},
                },
                "quality_complete": True,
            })

        def capture(area, _level, _message, **_fields):
            areas.append(area)

        with (
            patch.object(runner, "require_background_llm_api_key", return_value="test-key"),
            patch.object(runner, "start_llm_usage_task", return_value=None),
            patch.object(runner, "annotate_group_feedback", annotate_one),
            patch.object(annotate, "runtime_log", side_effect=capture),
        ):
            runner.start_run(task["id"], RunTask(expected_version=task["version"]), LOGIN)
            await runner._run_slice(task["id"], asyncio.Semaphore(2))
            annotate._annotate_ai_log("direct model done", task="quality-1-initial")

        self.assertEqual(areas, ["群组质量标注", "数据标注"])

    async def test_two_tasks_run_third_queues_and_closing_client_does_not_cancel(self):
        import httpx
        from fastapi import FastAPI
        from unittest.mock import AsyncMock
        from app.routers import group_tasks as routes
        from app.schemas.group_tasks import TaskRules
        from app.services import group_tasks as service
        from app.services import roster_sources
        jobs=[self.make_task(labels=(None,)) for _ in range(3)]
        release=asyncio.Event(); both_running=asyncio.Event(); started=[]
        async def annotate(task,row,checkpoint):
            started.append(task['id'])
            if len(started)==2:both_running.set()
            await release.wait()
            checkpoint({'quality':{'overall':'有效反馈','overall_reason':'并行隔离验证','q_labels':{'col_1':'有效反馈'}},'quality_complete':True})
        app=FastAPI();app.include_router(routes.router)
        with patch.object(routes,'_current_login',AsyncMock(return_value=LOGIN)),patch.object(roster_sources,'read_roster_snapshot',AsyncMock(return_value=snapshot())),patch.object(runner,'require_background_llm_api_key',return_value='test-key'),patch.object(runner,'start_llm_usage_task',return_value=None),patch.object(runner,'annotate_group_feedback',annotate),patch.object(config,'GROUP_TASK_CONCURRENCY',2):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://isolated.test') as client:
                for task in jobs:
                    response=await client.post('/api/group-tasks/'+task['id']+'/run',json={'expected_version':task['version']})
                    self.assertEqual(response.status_code,200)
            # The HTTP client has disconnected before background execution starts.
            scheduler=asyncio.create_task(runner._scheduler())
            try:
                async with asyncio.timeout(5):await both_running.wait()
                self.assertEqual(len(started),2)
                self.assertEqual(store.read('tasks',jobs[2]['id'])['run']['status'],'queued')
                draft=service.create_task(TaskRules(name='并行运行期间创建 D'),LOGIN)
                self.assertIsNone(draft.get('run'))
                release.set()
                async with asyncio.timeout(5):
                    while any(store.read('tasks',t['id'])['run']['status']!='completed' for t in jobs):await asyncio.sleep(.02)
                self.assertEqual(set(started),{t['id'] for t in jobs})
            finally:
                release.set();scheduler.cancel();await asyncio.gather(scheduler,return_exceptions=True)

    async def test_duplicate_start_and_fair_background_progress(self):
        order=[]
        async def annotate(task,row,checkpoint):
            order.append(task["id"])
            await asyncio.sleep(.005)
            checkpoint({"quality":{"overall":"有效反馈","overall_reason":"合成验证","q_labels":{"col_1":"有效反馈"}},"quality_complete":True})
        tasks=[self.make_task(labels=(None,None)) for _ in range(3)]
        with patch.object(runner,"require_background_llm_api_key",return_value="test-key"),patch.object(runner,"start_llm_usage_task",return_value=None),patch.object(runner,"annotate_group_feedback",annotate),patch.object(config,"GROUP_TASK_CONCURRENCY",1),patch.object(config,"GROUP_TASK_REQUEST_CONCURRENCY",1):
            for task in tasks:
                request=RunTask(expected_version=task["version"])
                first=runner.start_run(task["id"],request,LOGIN)
                duplicate=runner.start_run(task["id"],request,LOGIN)
                self.assertEqual(first["run"]["id"],duplicate["run"]["id"])
            scheduler=asyncio.create_task(runner._scheduler())
            try:
                async with asyncio.timeout(5):
                    while any(store.read("tasks",t["id"])["run"]["status"]!="completed" for t in tasks):
                        await asyncio.sleep(.05)
            finally:
                scheduler.cancel();await asyncio.gather(scheduler,return_exceptions=True)
        self.assertEqual(order[:3],[t["id"] for t in tasks])
        for task in tasks:
            saved=store.read("tasks",task["id"])
            self.assertEqual(len(saved["run"]["completed_ids"]),2)
            self.assertEqual(saved["version"],task["version"])
            self.assertGreater(saved["run"]["queue_wait_seconds"],0)
            self.assertGreater(saved["run"]["execution_seconds"],0)
            self.assertGreater(saved["run"]["save_seconds"],0)
            self.assertLessEqual(saved["run"]["save_seconds"],saved["run"]["execution_seconds"])
            self.assertEqual(saved["run"]["completed_slices"],2)
            self.assertFalse(saved["run"]["timing_partial"])

    async def test_retry_keeps_success_and_manual_result(self):
        task=self.make_task(labels=("优秀反馈",None))
        task["feedback"][0]["manual_quality"]={"label":"有效反馈","note":"人工保留"}
        store.mutate("tasks",task["id"],lambda current:current.update(task))
        processed=[]
        async def annotate(t,row,checkpoint):
            processed.append(row["id"]);checkpoint({"quality":{"overall":"有效反馈","overall_reason":"补齐","q_labels":{"col_1":"有效反馈"}},"quality_complete":True})
        with patch.object(runner,"require_background_llm_api_key",return_value="test-key"),patch.object(runner,"start_llm_usage_task",return_value=None),patch.object(runner,"annotate_group_feedback",annotate):
            runner.start_run(task["id"],RunTask(expected_version=task["version"]),LOGIN)
            await runner._run_slice(task["id"],asyncio.Semaphore(2))
        saved=store.read("tasks",task["id"])
        self.assertEqual(processed,[task["feedback"][1]["id"]])
        self.assertEqual(saved["feedback"][0]["manual_quality"],task["feedback"][0]["manual_quality"])

    async def test_restart_marks_uncertain_inflight_retryable(self):
        task=self.make_task(labels=("有效反馈",None))
        with patch.object(runner,"require_background_llm_api_key",return_value="test-key"):
            runner.start_run(task["id"],RunTask(expected_version=task["version"]),LOGIN)
        def crash(current):current["run"].update(status="running",inflight_ids=[current["feedback"][1]["id"]])
        store.mutate("tasks",task["id"],crash);runner.recover_interrupted_runs()
        saved=store.read("tasks",task["id"])
        self.assertEqual(saved["run"]["status"],"incomplete")
        self.assertTrue(saved["run"]["timing_partial"])
        self.assertTrue(saved["feedback"][0]["quality_complete"])
        self.assertIn("重复模型费用",saved["feedback"][1]["model_error"])

    async def test_late_callback_fence_rejects_other_run(self):
        task=self.make_task(labels=(None,))
        with patch.object(runner,"require_background_llm_api_key",return_value="test-key"):
            runner.start_run(task["id"],RunTask(expected_version=task["version"]),LOGIN)
        saved=store.read("tasks",task["id"])
        with self.assertRaises(RuntimeError):runner._fenced(saved,"wrong-run",task["version"])

    async def test_http_gate_is_bounded_and_cancel_releases_slots(self):
        from app.core.llm_limits import AttemptGate, bind_attempt_gate, llm_attempt_slot
        global_slots=asyncio.Semaphore(2)
        active=0;peak=0;local=[0,0];local_peak=[0,0];attempts=[]
        async def work(owner):
            nonlocal active,peak
            async with llm_attempt_slot():
                active+=1;local[owner]+=1;peak=max(peak,active);local_peak[owner]=max(local_peak[owner],local[owner])
                try:await asyncio.sleep(.01)
                finally:active-=1;local[owner]-=1
        async def task(owner):
            with bind_attempt_gate(AttemptGate(global_slots,1,lambda:attempts.append(owner))):
                await asyncio.gather(*(work(owner) for _ in range(3)))
        await asyncio.gather(task(0),task(1))
        self.assertEqual((peak,local_peak,len(attempts)),(2,[1,1],6))
        entered=asyncio.Event()
        async def held():
            with bind_attempt_gate(AttemptGate(global_slots,1,lambda:None)):
                async with llm_attempt_slot():entered.set();await asyncio.Event().wait()
        running=asyncio.create_task(held());await entered.wait();running.cancel();await asyncio.gather(running,return_exceptions=True)
        self.assertEqual(global_slots._value,2)

    async def test_adapter_checkpoints_ai_before_quality_failure_and_keeps_response_identity(self):
        from unittest.mock import AsyncMock
        from app.services import annotate_workflow as annotate
        task=self.make_task(labels=(None,None));task['rules']['ai_detect']=True
        row=task['feedback'][0];checkpoints=[]
        ai={'id':row['id'],'ai_prob':12,'reason':'合成依据','translations':{}}
        async def quality(*args,**kwargs):
            self.assertEqual(args[2][0][-1],row['id'])
            kwargs['on_progress']([{'id':row['id'],'q_labels':{'col_1':'有效反馈'}}])
            raise RuntimeError('simulated upstream failure')
        with patch.object(annotate,'_run_ai_batch_checked',AsyncMock(return_value=(1,[ai],set(),set(),''))),patch.object(annotate,'_run_one_quality_batch_strict',quality):
            await annotate.annotate_group_feedback(task,row,checkpoints.append)
            self.assertEqual(len(checkpoints),1)  # AI run must not start quality.
            row.update(checkpoints[0]);task['ai_confirmation_complete']=True
            with self.assertRaises(RuntimeError):await annotate.annotate_group_feedback(task,row,checkpoints.append)
        self.assertTrue(checkpoints[0]['ai_complete'])
        self.assertFalse(checkpoints[1]['quality_complete'])
        self.assertNotEqual(row['id'],task['feedback'][1]['id'])
