import asyncio
import unittest
from unittest.mock import patch

from test_group_tasks import WorkflowCase, LOGIN, snapshot, entry
from app.core import config
from app.schemas.group_tasks import RunTask
from app.services import group_task_runner as runner
from app.storage import group_workflow as store


class RunnerTests(unittest.IsolatedAsyncioTestCase, WorkflowCase):
    async def test_admission_reads_latest_roster_and_failure_never_queues(self):
        from fastapi import HTTPException
        from unittest.mock import AsyncMock
        from app.services import roster_sources
        task=self.make_task(labels=(None,))
        request=RunTask(expected_version=task['version'])
        with patch.object(roster_sources,'read_roster_snapshot',AsyncMock(side_effect=HTTPException(409,'测试来源不可读'))),patch.object(runner,'require_background_llm_api_key') as key:
            with self.assertRaises(HTTPException):await runner.prepare_run(task['id'],request,LOGIN)
            key.assert_not_called()
        saved=store.read('tasks',task['id'])
        self.assertIsNone(saved['run']);self.assertEqual(saved['roster_snapshot'],task['roster_snapshot'])
        fresh=snapshot([entry(ori='818043064(6389)')])
        with patch.object(roster_sources,'read_roster_snapshot',AsyncMock(return_value=fresh)) as read,patch.object(runner,'require_background_llm_api_key',return_value='test-key'):
            result=await runner.prepare_run(task['id'],request,LOGIN)
            repeated=await runner.prepare_run(task['id'],RunTask(expected_version=result['version']),LOGIN)
            self.assertEqual(result['run']['id'],repeated['run']['id']);self.assertEqual(read.await_count,1)
        self.assertEqual(result['version'],task['version']+1)
        self.assertEqual(result['players'][0]['ori'],'818043064(6389)')

    async def test_below_count_gate_never_reaches_model_adapter(self):
        task=self.make_task(labels=(None,None),requires_count=True,counts=[2,3])
        called=[]
        async def annotate(current,row,checkpoint):
            called.append(row['id'])
            checkpoint({'quality':{'overall':'有效反馈','overall_reason':'达到次数门槛'},'quality_complete':True})
        with patch.object(runner,'require_background_llm_api_key',return_value='test-key'),patch.object(runner,'start_llm_usage_task',return_value=None),patch.object(runner,'annotate_group_feedback',annotate):
            runner.start_run(task['id'],RunTask(expected_version=task['version']),LOGIN)
            await runner._run_slice(task['id'],asyncio.Semaphore(2))
        self.assertEqual(called,[task['feedback'][1]['id']])
        saved=store.read('tasks',task['id'])
        self.assertEqual(saved['run']['status'],'completed')
        self.assertEqual(saved['run']['target_ids'],called)

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
            checkpoint({'quality':{'overall':'有效反馈','overall_reason':'并行隔离验证'},'quality_complete':True})
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
            checkpoint({"quality":{"overall":"有效反馈","overall_reason":"合成验证"},"quality_complete":True})
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
            processed.append(row["id"]);checkpoint({"quality":{"overall":"有效反馈","overall_reason":"补齐"},"quality_complete":True})
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
            with self.assertRaises(RuntimeError):await annotate.annotate_group_feedback(task,row,checkpoints.append)
        self.assertTrue(checkpoints[0]['ai_complete'])
        self.assertFalse(checkpoints[1]['quality_complete'])
        self.assertNotEqual(row['id'],task['feedback'][1]['id'])
