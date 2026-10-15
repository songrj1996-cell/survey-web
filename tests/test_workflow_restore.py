"""Bounded synthetic acceptance for the restored order; no external requests."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import httpx
from fastapi import FastAPI

from test_group_tasks import WorkflowCase, LOGIN, entry, snapshot
from app.schemas.group_tasks import TaskRules, FeedbackMapping, RunTask, ConfirmAI, DuplicateDecision
from app.schemas.reward_batches import RewardSelection, BatchGenerate
from app.services import group_tasks as tasks, group_task_runner as runner, roster_sources, reward_batches
from app.services.task_records import clipboard_records
from app.storage import group_workflow as store


class WorkflowRestoreTests(WorkflowCase):
    def create(self, ai=True, count=False, repeat=False):
        return tasks.create_task(TaskRules(name='完整流程验收',payout_date='2026-10-15',base_reward=50,
            excellent_reward=100,ai_detect=ai,requires_count=count,min_count=3 if count else None,allow_repeat=repeat),LOGIN)

    def upload(self, task, rows=None):
        rows = rows or ['p1,The button sometimes freezes,3','p2,I like the feature,3']
        text='Discord User ID,What do you think?,Matches,,\n'+'\n'.join(rows)
        task=tasks.upload_feedback(task['id'],task['version'],'完整长文件名-合成反馈.xlsx.csv',text.encode(),LOGIN)
        return tasks.set_mapping(task['id'],task['version'],FeedbackMapping(id_col=0,open_text_cols=[1],count_col=2 if task['rules']['requires_count'] else None),LOGIN)

    def test_draft_cannot_skip_rules_upload_run_roster_or_confirmation(self):
        task=tasks.create_task(TaskRules(),LOGIN)
        self.assertEqual(task['workflow']['max_step'],1)
        actions=[lambda:tasks.upload_feedback(task['id'],task['version'],'a.csv',b'id,answer\np1,hi',LOGIN),
                 lambda:runner.start_run(task['id'],RunTask(expected_version=task['version']),LOGIN),
                 lambda:asyncio.run(tasks.refresh_roster(task['id'],task['version'],LOGIN)),
                 lambda:tasks.confirm_task(task['id'],task['version'],LOGIN)]
        for action in actions:
            with self.assertRaises(HTTPException): action()
        self.assertEqual(store.read('tasks',task['id'])['version'],1)

    def test_columns_preserve_indexes_hide_empty_and_translate_persistently(self):
        task=self.upload(self.create(ai=False))
        self.assertEqual(task['empty_col_idxs'],[3,4])
        self.assertEqual(len(task['column_types']),5)
        self.assertEqual(task['column_types'][0],'用户ID')
        with patch('app.services.annotate_workflow._translate_headers',AsyncMock(return_value=(['Discord 用户 ID','你怎么看？','次数','',''],''))):
            translated=asyncio.run(tasks.translate_upload_headers(task['id'],task['version'],LOGIN))
        self.assertEqual(translated['headers'][1],'What do you think?')
        self.assertEqual(translated['headers_zh'][1],'你怎么看？')
        self.assertEqual(translated['mapping']['open_text_cols'],[1])
        self.assertEqual(translated['version'],task['version'])
        self.assertEqual(tasks.task_view(store.read('tasks',task['id']),LOGIN)['feedback'][0]['answers'][0]['question_zh'],'你怎么看？')

    def test_legacy_roster_errors_do_not_block_annotation_on_reload(self):
        task=self.upload(self.create(ai=False))
        def old(saved):
            saved['input_issues']=[{'kind':'identity','message':'名单中找不到 playerid'}]
            saved['feedback'][0]['issues']=[{'kind':'identity','message':'名单中找不到 playerid'}]
            saved['roster_error']='旧名单无法读取'
        store.mutate('tasks',task['id'],old)
        loaded=tasks.task_view(store.read('tasks',task['id']),LOGIN)
        self.assertFalse(loaded['annotation_issues'])
        self.assertEqual(loaded['workflow']['max_step'],4)
        self.assertTrue(loaded['confirmation_issues'])

    def test_count_gate_missing_is_not_zero_and_required_column_is_enforced(self):
        task=self.create(count=True)
        task=tasks.upload_feedback(task['id'],task['version'],'a.csv',b'id,answer,count\np1,ok,2\np2,ok,\np3,ok,3',LOGIN)
        with self.assertRaises(HTTPException):
            tasks.set_mapping(task['id'],task['version'],FeedbackMapping(id_col=0,open_text_cols=[1]),LOGIN)
        task=tasks.set_mapping(task['id'],task['version'],FeedbackMapping(id_col=0,open_text_cols=[1],count_col=2),LOGIN)
        self.assertEqual([r['gate']['status'] for r in task['feedback']],['fail','missing','pass'])
        self.assertEqual(task['workflow']['max_step'],2)
        self.assertIsNone(task['feedback'][1]['count'])

    def test_group_is_metadata_only_and_disabled_source_cannot_be_newly_selected(self):
        task=self.upload(self.create(ai=False))
        store.create('sources','label_group',{'id':'label_group','name':'任务归属记录群','enabled':True})
        updated=tasks.update_rules(task['id'],task['version'],TaskRules(**{**task['rules'],'group_source_id':'label_group','group_name':'伪造群名'}),LOGIN)
        self.assertEqual(updated['rules']['group_name'],'任务归属记录群')
        self.assertEqual(updated['feedback'],task['feedback'])
        self.assertEqual(updated['summary'],task['summary'])
        self.assertEqual(roster_sources.group_options(LOGIN),[{'id':'label_group','name':'任务归属记录群'}])
        store.mutate('sources','label_group',lambda item:item.update(enabled=False))
        kept=tasks.update_rules(task['id'],updated['version'],TaskRules(**updated['rules']),LOGIN)
        self.assertEqual(kept['rules']['group_name'],'任务归属记录群')
        with self.assertRaises(HTTPException):tasks.create_task(TaskRules(group_source_id='label_group'),LOGIN)

    def test_full_http_order_ai_review_quality_roster_records_and_txt(self):
        asyncio.run(self._full_http())

    def test_late_account_collision_keeps_upload_duplicate_choice_and_final_stage(self):
        task=self.upload(self.create(ai=False),['p1,first answer,3','p1,duplicate answer,3','p2,other answer,3'])
        group=task['duplicate_groups'][0]
        task=tasks.resolve_duplicate(task['id'],DuplicateDecision(expected_version=task['version'],group_id=group['id'],keep_response_id=task['feedback'][0]['id']),LOGIN)
        def annotated(saved):
            for row in saved['feedback']:
                if not row['excluded']:
                    row.update(quality_complete=True,quality={'overall':'有效反馈','overall_reason':'合成依据'})
        store.mutate('tasks',task['id'],annotated)
        first=entry(player='p1');second=entry(player='p2');second['key']='group_a_3'
        with patch.object(roster_sources,'read_roster_snapshot',AsyncMock(return_value=snapshot([first,second]))):
            task=asyncio.run(tasks.refresh_roster(task['id'],task['version'],LOGIN))['task']
        self.assertTrue(task['feedback'][1]['excluded'])
        self.assertEqual(task['workflow']['max_step'],5)
        self.assertFalse(task['annotation_issues'])
        self.assertTrue(any(i['kind']=='roster_duplicate' for i in task['confirmation_issues']))
        collision=next(g for g in task['duplicate_groups'] if g['roster_only'])
        task=tasks.resolve_duplicate(task['id'],DuplicateDecision(expected_version=task['version'],group_id=collision['id'],keep_response_id=task['feedback'][0]['id']),LOGIN)
        self.assertEqual([r['excluded'] for r in task['feedback']],[False,True,True])
        self.assertEqual(task['workflow']['max_step'],5)

    async def _full_http(self):
        from app.routers import group_tasks as routes
        task=self.upload(self.create(ai=True))
        app=FastAPI();app.include_router(routes.router)
        calls=[]
        async def model(saved,row,checkpoint):
            stage=saved['run']['stage'];calls.append((stage,row['player_id']))
            if stage=='ai':checkpoint({'ai':{'id':row['id'],'ai_prob':10,'reason':'合成测试','translations':{'col_1':'合成译文'}},'ai_complete':True})
            else:checkpoint({'quality':{'overall':'优秀反馈','overall_reason':'合成完整判断'},'quality_complete':True})
        read=AsyncMock(side_effect=HTTPException(409,'标注期间名单不可读'))
        with patch.object(routes,'_current_login',AsyncMock(return_value=LOGIN)),patch.object(roster_sources,'read_roster_snapshot',read),patch.object(runner,'require_background_llm_api_key',return_value='synthetic'),patch.object(runner,'start_llm_usage_task',return_value=None),patch.object(runner,'annotate_group_feedback',model):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                path='/api/group-tasks/'+task['id']
                async def post(suffix,body=None,status=200):
                    nonlocal task
                    response=await client.post(path+'/'+suffix,json={'expected_version':task['version'],**(body or {})})
                    self.assertEqual(response.status_code,status,response.text)
                    if status==200:task=response.json().get('task',response.json())
                    return response
                await post('run',{'stage':'quality'},409)
                await post('roster',status=409)
                await post('run',{'stage':'ai'})
                await runner._run_slice(task['id'],asyncio.Semaphore(2))
                task=(await client.get(path)).json()
                self.assertEqual(task['workflow']['max_step'],3)
                self.assertEqual(task['status'],'awaiting_ai_confirmation')
                self.assertEqual(calls,[('ai','p1'),('ai','p2')])
                self.assertEqual(task['summary']['complete_count'],0)
                await post('run',{'stage':'quality'},409)
                await post('confirm-ai',{'response_ids':[task['feedback'][1]['id']]})
                self.assertEqual(task['workflow']['max_step'],4)
                await post('run',{'stage':'quality'})
                await runner._run_slice(task['id'],asyncio.Semaphore(2))
                task=(await client.get(path)).json()
                self.assertEqual(calls[-1],('quality','p1'))
                self.assertEqual(len(calls),3)
                self.assertEqual(task['workflow']['max_step'],5)
                self.assertEqual(task['summary']['base']+task['summary']['excellent'],150)
                read.assert_not_awaited()
                await post('confirm',status=409)
                result=await post('roster')
                self.assertFalse(result.json()['ok'])
                self.assertEqual(task['workflow']['max_step'],5)
                first=entry(player='p1'); second=entry(player='p2',ori='818043064(6389)');second['key']='group_a_3'
                fresh=snapshot([first,second]);read.side_effect=None;read.return_value=fresh
                await post('roster');await post('confirm')
                self.assertEqual(task['workflow']['max_step'],6)
        reference={'target_spreadsheet':'synthetic','sheet_title':'名单','player_col':0,'ori_col':1,'status_col':2}
        with patch.object(roster_sources,'validate_formula_reference',AsyncMock(return_value=reference)):
            copied=await clipboard_records(task['id'],task['version'],LOGIN)
        self.assertEqual(copied['count'],2)
        self.assertTrue(all(len(line.split('\t'))==12 for line in copied['text'].splitlines()))
        store.create('sources','group_a',{'id':'group_a','revision':1,'enabled':True,'name':'测试群'})
        with patch.object(reward_batches,'read_roster_snapshot',AsyncMock(return_value=fresh)):
            preview=await reward_batches.create_preview(RewardSelection(dates=['2026-10-15'],task_versions={task['id']:task['version']}),LOGIN)
            self.assertTrue(preview['ready'])
            batch=await reward_batches.generate_batch(preview['id'],BatchGenerate(expected_revision=preview['revision'],idempotency_key='restored-flow-test-123'),LOGIN)
        self.assertEqual(batch['txt_preview'],'6389\t818043063\t301-150\r\n')
        self.assertEqual(batch['total_diamonds'],150)

