import asyncio
from copy import deepcopy
from unittest.mock import patch, AsyncMock

from fastapi import HTTPException

from test_group_tasks import WorkflowCase, LOGIN, snapshot, entry
from app.schemas.reward_batches import RewardSelection, BatchGenerate, OtherRewardCreate, BatchAction
from app.services import group_tasks as tasks, reward_batches as rewards
from app.storage import group_workflow as store


class RewardBatchTests(WorkflowCase):
    def test_fresh_roster_hidden_formatting_still_generates_correct_txt(self):
        task, _, selection = self.prepare()
        raw_id = '\u202a' + entry()['player_id'] + '\u202c'
        fresh = snapshot([entry(player=raw_id)])
        with patch.object(rewards, 'read_roster_snapshot', AsyncMock(return_value=fresh)):
            preview = asyncio.run(rewards.create_preview(selection, LOGIN))
        self.assertTrue(preview['ready'])
        self.assertFalse(preview['identity_issues'])
        batch = self.generate(preview, roster=fresh)
        self.assertEqual(batch['txt_preview'], '6389\t818043063\t301-500\r\n')
        self.assertEqual(batch['total_diamonds'], 500)

    def test_settlement_does_not_fuzzy_match_or_choose_hidden_id_collisions(self):
        row = {'row_id': 'synthetic', 'player_id': '83866270215', 'task': '合成任务', 'sheet': ''}
        _, _, issues, _ = rewards._reconcile([row], snapshot([entry(player='83866370215')]), {})
        self.assertEqual(len(issues), 1)
        row['player_id'] = entry()['player_id']
        _, _, issues, _ = rewards._reconcile([row], snapshot([entry(), entry(player='\u202a' + row['player_id'] + '\u202c', source='group_b')]), {})
        self.assertEqual(len(issues[0]['candidates']), 2)

    def test_unmatched_player_exclusion_persists_into_txt_and_stales_on_restore(self):
        from app.schemas.group_tasks import FeedbackCorrection
        task = self.make_task(labels=('优秀反馈', '有效反馈'))
        other = entry('008123456789', '1234567(2345)', 'group_b')
        def seed(current):
            current['feedback'][1]['cells'][0] = other['player_id']
            current['roster_snapshot'] = snapshot([other])
        store.mutate('tasks', task['id'], seed)
        task = tasks.correct_feedback(task['id'], task['feedback'][0]['id'], FeedbackCorrection(
            expected_version=task['version'], reward_excluded=True), LOGIN)
        store.create('sources', 'group_b', {'id': 'group_b', 'revision': 1, 'enabled': True})
        fresh = snapshot([other])
        with patch.object(rewards, 'read_roster_snapshot', AsyncMock(return_value=fresh)):
            preview = asyncio.run(rewards.create_preview(RewardSelection(dates=['2026-10-14'],
                task_versions={task['id']: task['version']}), LOGIN))
        self.assertTrue(preview['ready'])
        self.assertFalse(preview['identity_issues'])
        self.assertEqual(preview['result']['total_diamonds'], 100)
        self.assertEqual(preview['excluded_rows'][0]['player_id'], entry()['player_id'])
        batch = self.generate(preview, roster=fresh)
        self.assertEqual(batch['txt_preview'], '2345\t1234567\t301-100\r\n')
        self.assertEqual(len(batch['excluded_rows']), 1)
        tasks.correct_feedback(task['id'], task['feedback'][0]['id'], FeedbackCorrection(
            expected_version=task['version'], reward_excluded=False), LOGIN)
        with self.assertRaises(HTTPException):
            self.generate(preview, key='different-key-after-restore', roster=fresh)
        self.assertEqual(rewards.get_batch(batch['id'], LOGIN)['txt_preview'], batch['txt_preview'])

    def test_manual_exclusion_survives_new_membership_and_omits_final_txt(self):
        from app.schemas.group_tasks import FeedbackCorrection
        task = self.make_task(labels=('优秀反馈', '有效反馈'))
        other = entry('008123456789', '1234567(2345)', 'group_c')
        def seed(current):
            current['feedback'][1]['cells'][0] = other['player_id']
            current['roster_snapshot'] = snapshot([entry(eligible=False), entry(source='group_b', eligible=False), other])
            tasks._refresh_checks(current)
        store.mutate('tasks', task['id'], seed)
        task = tasks.correct_feedback(task['id'], task['feedback'][0]['id'], FeedbackCorrection(
            expected_version=task['version'], reward_excluded=True), LOGIN)
        for source in ('group_a', 'group_b', 'group_c'):
            store.create('sources', source, {'id': source, 'revision': 1, 'enabled': True})
        fresh = snapshot([entry(), entry(source='group_b'), other])
        with patch.object(rewards, 'read_roster_snapshot', AsyncMock(return_value=fresh)):
            preview = asyncio.run(rewards.create_preview(RewardSelection(dates=['2026-10-14'],
                task_versions={task['id']: task['version']}), LOGIN))
        self.assertTrue(preview['ready'])
        self.assertFalse(preview['identity_issues'])
        self.assertEqual(preview['result']['total_diamonds'], 100)
        self.assertEqual(preview['excluded_rows'][0]['player_id'], entry()['player_id'])
        self.assertIn('手动排除', preview['excluded_rows'][0]['exclusion_reason'])
        batch = self.generate(preview, roster=fresh)
        self.assertEqual(batch['txt_preview'], '2345\t1234567\t301-100\r\n')
        self.assertEqual(len(batch['excluded_rows']), 1)
        self.assertNotIn('818043063', batch['txt_preview'])

    def test_exclusion_is_scoped_to_one_task_and_restore_invalidates_preview(self):
        from app.schemas.group_tasks import FeedbackCorrection
        first = self.make_task(labels=('有效反馈',))
        second = self.make_task(labels=('有效反馈',))
        store.mutate('tasks', first['id'], lambda current: current.update(roster_snapshot=snapshot([entry(eligible=False)])))
        first = tasks.correct_feedback(first['id'], first['feedback'][0]['id'], FeedbackCorrection(
            expected_version=first['version'], reward_excluded=True), LOGIN)
        store.create('sources', 'group_a', {'id': 'group_a', 'revision': 1, 'enabled': True})
        with patch.object(rewards, 'read_roster_snapshot', AsyncMock(return_value=snapshot())):
            preview = asyncio.run(rewards.create_preview(RewardSelection(dates=['2026-10-14'],
                task_versions={first['id']: first['version'], second['id']: second['version']}), LOGIN))
        self.assertTrue(preview['ready'])
        self.assertEqual(preview['result']['total_diamonds'], 100)
        self.assertEqual(len(preview['excluded_rows']), 1)
        tasks.correct_feedback(first['id'], first['feedback'][0]['id'], FeedbackCorrection(
            expected_version=first['version'], reward_excluded=False), LOGIN)
        with self.assertRaises(HTTPException):
            self.generate(preview)
        self.assertFalse(rewards._ledger()['batches'])

    def test_multiple_completed_tasks_settle_together_without_task_roster_confirmation(self):
        first=self.make_task();second=self.make_task(labels=('有效反馈',))
        tasks.update_rules(second['id'],second['version'],__import__('app.schemas.group_tasks',fromlist=['TaskRules']).TaskRules(**{**second['rules'],'name':'第二项任务'}),LOGIN)
        for item in (first,second):
            store.mutate('tasks',item['id'],lambda t:t.update(roster_snapshot=None,confirmed_version=None))
        selected={item['id']:store.read('tasks',item['id'])['version'] for item in (first,second)}
        self.assertTrue(all(item['selectable'] for item in rewards.choices(LOGIN)['tasks']))
        store.create('sources','group_a',{'id':'group_a','revision':1,'enabled':True})
        with patch.object(rewards,'read_roster_snapshot',AsyncMock(return_value=snapshot())) as read:
            preview=asyncio.run(rewards.create_preview(RewardSelection(dates=['2026-10-14'],task_versions=selected),LOGIN))
            self.assertEqual(read.await_count,1)
        self.assertTrue(preview['ready']);self.assertEqual(preview['result']['total_diamonds'],600)
        self.assertEqual(preview['result']['unique_accounts'],1)
        self.assertEqual(self.generate(preview)['total_diamonds'],600)

    def test_deleted_task_blocks_old_preview_but_keeps_historical_batch_and_claims(self):
        task,preview,_=self.prepare()
        tasks.delete_task(task['id'],task['version'],LOGIN)
        self.assertEqual(rewards.choices(LOGIN)['tasks'],[])
        with self.assertRaises(HTTPException):self.generate(preview)
        other,preview,_=self.prepare();batch=self.generate(preview,'history-preservation')
        tasks.delete_task(other['id'],other['version'],LOGIN)
        self.assertEqual(rewards.get_batch(batch['id'],LOGIN)['total_diamonds'],500)
        self.assertEqual(rewards.download_batch(batch['id'],LOGIN)[0],batch['txt_preview'].encode())
        self.assertIn('task:'+other['id'],rewards._ledger()['claims'])

    def test_note_group_never_limits_final_roster_identity(self):
        task=self.make_task(labels=('有效反馈',))
        store.create('sources','notes',{'id':'notes','name':'归属备注','enabled':True})
        from app.schemas.group_tasks import TaskSettingsUpdate
        task=tasks.update_settings(task['id'],TaskSettingsUpdate(expected_version=task['version'],name=task['rules']['name'],payout_date='2026-10-14',base_reward=100,excellent_reward=100,group_source_id='notes'),LOGIN)
        with patch.object(rewards,'read_roster_snapshot',AsyncMock(return_value=snapshot())):
            preview=asyncio.run(rewards.create_preview(RewardSelection(dates=['2026-10-14'],task_versions={task['id']:task['version']}),LOGIN))
        self.assertTrue(preview['ready'])
        self.assertEqual(preview['selected_rows'][0]['identity']['source_id'],'group_a')

    def prepare(self):
        task=self.make_task();task=tasks.confirm_task(task["id"],task["version"],LOGIN)
        if not store.read("sources","group_a"):
            store.create("sources","group_a",{"id":"group_a","name":"实际名单群","revision":1,"enabled":True})
        selection=RewardSelection(dates=["2026-10-14"],task_versions={task["id"]:task["version"]})
        with patch.object(rewards,"read_roster_snapshot",AsyncMock(return_value=snapshot())):
            preview=asyncio.run(rewards.create_preview(selection,LOGIN))
        return task,preview,selection

    def generate(self,preview,key="idempotent-test-key-0001",roster=None):
        with patch.object(rewards,"read_roster_snapshot",AsyncMock(return_value=roster or snapshot())):
            return asyncio.run(rewards.generate_batch(preview["id"],BatchGenerate(expected_revision=preview["revision"],idempotency_key=key),LOGIN))

    def test_immutable_500_txt_idempotency_and_claims(self):
        task,preview,selection=self.prepare();batch=self.generate(preview)
        self.assertEqual(batch["txt_preview"],"6389\t818043063\t301-500\r\n")
        self.assertEqual(batch["total_diamonds"],500)
        with patch.object(rewards,"read_roster_snapshot",AsyncMock(side_effect=RuntimeError("network must not be called for replay"))):
            replay=asyncio.run(rewards.generate_batch(preview["id"],BatchGenerate(expected_revision=preview["revision"],idempotency_key="idempotent-test-key-0001"),LOGIN))
        self.assertEqual(replay["id"],batch["id"])
        with patch.object(rewards,"read_roster_snapshot",AsyncMock(return_value=snapshot())):
            with self.assertRaises(HTTPException):asyncio.run(rewards.create_preview(selection,LOGIN))
        updated=tasks.update_rules(task["id"],task["version"],__import__('app.schemas.group_tasks',fromlist=['TaskRules']).TaskRules(**{**task["rules"],"base_reward":200}),LOGIN)
        tasks.confirm_task(task["id"],updated["version"],LOGIN)
        current_batch=rewards.get_batch(batch["id"],LOGIN)
        self.assertEqual(current_batch["total_diamonds"],500)
        self.assertEqual(current_batch["current_source_differences"][0]["current_total"],800)
        self.assertNotIn("current_source_differences",rewards._ledger()["batches"][batch["id"]])
        self.assertEqual(rewards.download_batch(batch["id"],LOGIN)[0],batch["txt_preview"].encode())

    def test_latest_status_changes_require_review_no_batch_created(self):
        task,preview,_=self.prepare()
        with self.assertRaises(HTTPException) as caught:self.generate(preview,roster=snapshot([entry(eligible=False)]))
        updated=caught.exception.detail["preview"]
        self.assertEqual(updated["generation_difference"]["before_total"],500)
        self.assertEqual(updated["generation_difference"]["after_total"],0)
        self.assertEqual(len(updated["excluded_rows"]),1)
        self.assertFalse(rewards._ledger()["batches"])

    def test_failed_fresh_read_keeps_previous_preview(self):
        _,preview,_=self.prepare();before=store.read("previews",preview["id"])
        with patch.object(rewards,"read_roster_snapshot",AsyncMock(side_effect=HTTPException(403,"无权限"))):
            with self.assertRaises(HTTPException):asyncio.run(rewards.generate_batch(preview["id"],BatchGenerate(expected_revision=1,idempotency_key="fresh-read-failure"),LOGIN))
        self.assertEqual(store.read("previews",preview["id"]),before)
        self.assertFalse(rewards._ledger()["batches"])

    def test_task_changes_after_preview_cannot_settle_old_version(self):
        task,preview,_=self.prepare()
        store.mutate("tasks",task["id"],lambda t:t.update(version=t["version"]+1,confirmed_version=None))
        with self.assertRaises(HTTPException):self.generate(preview)

    def test_parallel_clicks_create_one_batch(self):
        _,preview,_=self.prepare()
        async def both():
            with patch.object(rewards,"read_roster_snapshot",AsyncMock(return_value=snapshot())):
                request=BatchGenerate(expected_revision=1,idempotency_key="parallel-same-action-key")
                return await asyncio.gather(rewards.generate_batch(preview["id"],request,LOGIN),rewards.generate_batch(preview["id"],request,LOGIN))
        results=asyncio.run(both())
        self.assertEqual(results[0]["id"],results[1]["id"])
        self.assertEqual(len(rewards._ledger()["batches"]),1)

    def test_unknown_status_is_issue_not_exited(self):
        _,preview,_=self.prepare()
        unknown=entry();unknown.update(status="待核实",eligible=None,status_error="未知状态")
        current=store.read("previews",preview["id"]);current["roster_snapshot"]=snapshot([unknown])
        result=rewards.preview_view(current)
        self.assertEqual(len(result["identity_issues"]),1)
        self.assertEqual(result["excluded_rows"],[])

    def test_other_reward_is_independent_and_positive(self):
        task,preview,_=self.prepare()
        with patch.object(rewards,"read_roster_snapshot",AsyncMock(return_value=snapshot())):
            record=asyncio.run(rewards.create_other_reward(OtherRewardCreate(payout_date="2026-10-14",source_id="group_a",player_id=entry()["player_id"],amount=25,reason="合成抽奖"),LOGIN))
            selection=RewardSelection(dates=["2026-10-14"],task_versions={task["id"]:task["version"]},other_reward_ids=[record["id"]])
            merged=asyncio.run(rewards.create_preview(selection,LOGIN))
        self.assertEqual(merged["result"]["total_diamonds"],525)
        self.assertEqual(store.read("tasks",task["id"])["version"],task["version"])

    def test_two_independent_other_rewards_with_same_reason_are_not_duplicate_feedback(self):
        self.prepare()
        with patch.object(rewards,'read_roster_snapshot',AsyncMock(return_value=snapshot())):
            records=[asyncio.run(rewards.create_other_reward(OtherRewardCreate(payout_date='2026-10-14',source_id='group_a',player_id=entry()['player_id'],amount=25,reason='活动奖励'),LOGIN)) for _ in range(2)]
            preview=asyncio.run(rewards.create_preview(RewardSelection(dates=['2026-10-14'],other_reward_ids=[r['id'] for r in records]),LOGIN))
        self.assertTrue(preview['ready'])
        self.assertEqual(preview['result']['total_diamonds'],50)
        self.assertEqual(self.generate(preview)['total_diamonds'],50)

    def test_import_and_task_use_explicit_mixed_dates_and_live_identity(self):
        from test_reward_roster import workbook_bytes, mapping
        from app.services import reward_roster
        from app.schemas.reward_roster import RewardAnalyzeRequest
        task,_,_=self.prepare()
        content=workbook_bytes({'旧表':[['2026-10-13','旧活动',entry()['player_id'],'10001(2001)','已退群',25,0,0],['2026-10-12','未选择日期',entry()['player_id'],'10001(2001)','群内人员',900,0,0]]})
        created=reward_roster.create_reward_session('legacy.xlsx',content,LOGIN)
        sid=created['session_id']
        reward_roster.analyze_reward_session(sid,RewardAnalyzeRequest(selected_sheets=['旧表'],reward_dates=['2026-10-13'],mappings={'旧表':mapping()}),latest_roster=True)
        reward_roster._SESSIONS.clear()  # persisted imports survive process-local cache loss
        with patch.object(rewards,'read_roster_snapshot',AsyncMock(return_value=snapshot())):
            preview=asyncio.run(rewards.create_preview(RewardSelection(dates=['2026-10-13','2026-10-14'],task_versions={task['id']:task['version']},imported_session_ids=[sid]),LOGIN))
        self.assertEqual(preview['result']['total_diamonds'],525)
        self.assertEqual({r['account'] for r in preview['eligible_rows']},{entry()['ori']})
        self.assertEqual(len(preview['eligible_rows']),2)
        self.assertEqual(self.generate(preview)['txt_preview'],'6389\t818043063\t301-525\r\n')


    def action(self, revision=1, confirmed=True):
        return BatchAction(expected_revision=revision, confirmed=confirmed)

    def test_void_releases_only_its_claims_and_allows_fresh_generation(self):
        task, preview, selection = self.prepare()
        original_task = store.read('tasks', task['id'])
        original_view = tasks.task_view(original_task, LOGIN)
        batch = self.generate(preview)
        self.assertEqual(tasks.task_view(store.read('tasks', task['id']), LOGIN), original_view)
        store.mutate('ledger', 'payouts', lambda ledger: ledger['claims'].update(unrelated='another-batch'))
        voided = rewards.void_batch(batch['id'], self.action(), LOGIN)
        self.assertEqual(voided['status'], 'voided')
        self.assertEqual(voided['voided_by'], LOGIN['name'])
        self.assertEqual(voided['txt_preview'], batch['txt_preview'])
        self.assertEqual(rewards._ledger()['claims'], {'unrelated': 'another-batch'})
        self.assertEqual(store.read('tasks', task['id']), original_task)
        self.assertEqual(tasks.task_view(store.read('tasks', task['id']), LOGIN), original_view)
        self.assertTrue(rewards.choices(LOGIN)['tasks'][0]['selectable'])
        with patch.object(rewards, 'read_roster_snapshot', AsyncMock(return_value=snapshot())):
            fresh = asyncio.run(rewards.create_preview(selection, LOGIN))
        replacement = self.generate(fresh, key='replacement-generation-key')
        self.assertNotEqual(replacement['id'], batch['id'])
        self.assertEqual(replacement['txt_preview'], batch['txt_preview'])
        before = rewards._ledger()
        rewards.void_batch(batch['id'], self.action(), LOGIN)
        self.assertEqual(rewards._ledger(), before, 'retry cannot release replacement claims')
        self.assertEqual(rewards._ledger()['claims']['task:'+task['id']], replacement['id'])

    def test_void_download_replay_and_paid_confirmation_are_blocked(self):
        _, preview, _ = self.prepare()
        batch = self.generate(preview)
        rewards.void_batch(batch['id'], self.action(), LOGIN)
        for operation in (lambda: rewards.download_batch(batch['id'], LOGIN),
                          lambda: self.generate(preview),
                          lambda: self.generate(preview, key='stale-preview-with-new-key'),
                          lambda: rewards.confirm_paid(batch['id'], self.action(), LOGIN)):
            with self.assertRaises(HTTPException) as error:
                operation()
            self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(rewards.get_batch(batch['id'], LOGIN)['txt_preview'], batch['txt_preview'])

    def test_paid_confirmation_is_display_only_and_preserves_download_claims_and_task_functions(self):
        task, preview, _ = self.prepare()
        original = store.read('tasks', task['id'])
        before_view = tasks.task_view(original, LOGIN)
        batch = self.generate(preview)
        claims = rewards._ledger()['claims']
        paid = rewards.confirm_paid(batch['id'], self.action(), LOGIN)
        self.assertEqual(paid['status'], 'paid')
        self.assertEqual(paid['revision'], 2)
        self.assertEqual(paid['paid_by'], LOGIN['name'])
        self.assertEqual(store.read('tasks', task['id']), original)
        after_view = tasks.task_view(original, LOGIN)
        settlement = after_view.pop('settlement')
        self.assertEqual(after_view, before_view, 'annotation status, controls, records and results remain unchanged')
        self.assertEqual(settlement['version'], task['version'])
        self.assertFalse(settlement['changed'])
        self.assertEqual(rewards._ledger()['claims'], claims)
        self.assertFalse(rewards.choices(LOGIN)['tasks'][0]['selectable'])
        self.assertEqual(rewards.download_batch(batch['id'], LOGIN)[0], batch['txt_preview'].encode())
        self.assertEqual(rewards.confirm_paid(batch['id'], self.action(), LOGIN), paid)
        self.assertEqual(len(rewards._ledger()['batches'][batch['id']]['events']), 1)
        with self.assertRaises(HTTPException):
            rewards.void_batch(batch['id'], self.action(2), LOGIN)
        store.mutate('tasks', task['id'], lambda t: t.update(version=t['version']+1))
        updated = tasks.task_view(store.read('tasks', task['id']), LOGIN)
        self.assertTrue(updated['settlement']['changed'])
        self.assertEqual(updated['settlement']['version'], task['version'])

    def test_paid_list_reads_ledger_once_and_unpaid_tasks_have_no_settlement(self):
        task, preview, _ = self.prepare()
        other = self.make_task()
        batch = self.generate(preview)
        rewards.confirm_paid(batch['id'], self.action(), LOGIN)
        actual = store.read
        with patch.object(store, 'read', wraps=actual) as reads:
            listed = tasks.list_tasks(LOGIN)
        self.assertEqual(len([call for call in reads.call_args_list if call.args == ('ledger', 'payouts')]), 1)
        by_id = {item['id']: item for item in listed}
        self.assertIn('settlement', by_id[task['id']])
        self.assertNotIn('settlement', by_id[other['id']])

    def test_legacy_batches_read_as_pending_without_migration_then_can_confirm(self):
        task, preview, _ = self.prepare()
        batch = self.generate(preview)
        def legacy(ledger):
            ledger['batches'][batch['id']].pop('status')
            ledger['batches'][batch['id']].pop('revision')
        store.mutate('ledger', 'payouts', legacy)
        before = rewards._ledger()
        self.assertEqual(rewards.get_batch(batch['id'], LOGIN)['status'], 'pending')
        self.assertEqual(rewards.choices(LOGIN)['batches'][0]['status'], 'pending')
        self.assertNotIn('settlement', tasks.task_view(store.read('tasks', task['id']), LOGIN))
        self.assertEqual(rewards._ledger(), before)
        self.assertEqual(rewards.confirm_paid(batch['id'], self.action(), LOGIN)['status'], 'paid')

    def test_batch_action_requires_confirmation_version_and_existing_record(self):
        from pydantic import ValidationError
        for confirmed in ('true', 1, None):
            with self.assertRaises(ValidationError):
                BatchAction(expected_revision=1, confirmed=confirmed)
        with self.assertRaises(ValidationError):
            BatchAction(expected_revision=1, confirmed=True, reason='not supported')
        _, preview, _ = self.prepare()
        batch = self.generate(preview)
        before = rewards._ledger()
        for action in (lambda: rewards.confirm_paid(batch['id'], self.action(confirmed=False), LOGIN),
                       lambda: rewards.void_batch(batch['id'], self.action(9), LOGIN),
                       lambda: rewards.void_batch('missing', self.action(), LOGIN)):
            with self.assertRaises(HTTPException):
                action()
            self.assertEqual(rewards._ledger(), before)

    def test_status_actions_require_reward_use_and_record_access(self):
        from app.services import feature_access
        from app.core import config
        _, preview, _ = self.prepare()
        batch = self.generate(preview)
        before = rewards._ledger()
        for method in (rewards.void_batch, rewards.confirm_paid):
            with patch.object(config, 'FEISHU_LOGIN_REQUIRED', True), patch.object(feature_access, '_get_user_permission_levels', return_value={'reward':'view'}):
                with self.assertRaises(HTTPException) as error:
                    method(batch['id'], self.action(), LOGIN)
                self.assertEqual(error.exception.status_code, 403)
            with self.assertRaises(HTTPException) as error:
                method(batch['id'], self.action(), {'email':'stranger@example.test','name':'合成其他成员'})
            self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(rewards._ledger(), before)

    def test_failed_status_backup_or_save_preserves_entire_ledger_and_task(self):
        _, preview, _ = self.prepare()
        batch = self.generate(preview)
        before = rewards._ledger()
        for failure in ('_verified_backup', '_write'):
            for method in (rewards.void_batch, rewards.confirm_paid):
                with patch.object(store, failure, side_effect=OSError('synthetic failure')):
                    with self.assertRaises(HTTPException) as error:
                        method(batch['id'], self.action(), LOGIN)
                    self.assertEqual(error.exception.status_code, 503)
                self.assertEqual(rewards._ledger(), before)

    def test_concurrent_void_and_paid_have_one_winner(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        _, preview, _ = self.prepare()
        batch = self.generate(preview)
        barrier = threading.Barrier(2)
        def call(method):
            barrier.wait(timeout=5)
            try:
                return method(batch['id'], self.action(), LOGIN)['status']
            except HTTPException as error:
                return error.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(call, method) for method in (rewards.void_batch, rewards.confirm_paid)]
            results = [future.result(timeout=10) for future in pending]
        self.assertIn(409, results)
        status = rewards.get_batch(batch['id'], LOGIN)['status']
        self.assertIn(status, results)
        self.assertEqual(bool(rewards._ledger()['claims']), status == 'paid')
        self.assertEqual(len(rewards._ledger()['batches'][batch['id']]['events']), 1)

    def test_status_endpoints_and_void_download_use_isolated_router(self):
        import httpx
        from fastapi import FastAPI
        from app.routers import reward_batches as router_module
        _, preview, _ = self.prepare()
        batch = self.generate(preview)
        app = FastAPI()
        app.include_router(router_module.router)  # Never import app.main or start the production application.
        async def scenario():
            with patch.object(router_module, '_current_login', AsyncMock(return_value=LOGIN)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic.test') as client:
                    response = await client.post('/api/reward-batches/'+batch['id']+'/void', json={'expected_revision':1,'confirmed':True})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['status'], 'voided')
                    response = await client.get('/api/reward-batches/'+batch['id']+'/download')
                    self.assertEqual(response.status_code, 409)
                    response = await client.post('/api/reward-batches/'+batch['id']+'/confirm-paid', json={'expected_revision':2,'confirmed':True})
                    self.assertEqual(response.status_code, 409)
                    response = await client.post('/api/reward-batches/'+batch['id']+'/void', json={'confirmed':True})
                    self.assertEqual(response.status_code, 422)
        asyncio.run(scenario())


    def test_unreadable_settlement_metadata_does_not_block_annotation_operations(self):
        task = self.make_task()
        original_read = store.read
        def read(kind, identifier):
            if kind == 'ledger':
                raise OSError('synthetic unavailable reward ledger')
            return original_read(kind, identifier)
        with patch.object(store, 'read', side_effect=read):
            view = tasks.task_view(task, LOGIN)
            self.assertTrue(view['settlement_unavailable'])
            self.assertNotIn('settlement', view)
            self.assertTrue(view['reward_ready'])
            self.assertTrue(tasks.list_tasks(LOGIN)[0]['settlement_unavailable'])
            from app.schemas.group_tasks import TaskRules
            updated = tasks.update_rules(task['id'],task['version'],TaskRules(**{**task['rules'],'base_reward':200}),LOGIN)
            self.assertEqual(updated['rules']['base_reward'],200)


    def test_void_mixed_task_other_reward_and_import_can_all_be_reselected(self):
        from test_reward_roster import workbook_bytes, mapping
        from app.services import reward_roster
        from app.schemas.reward_roster import RewardAnalyzeRequest
        task, _, _ = self.prepare()
        content = workbook_bytes({'合成表':[['2026-10-13','合成独立活动',entry()['player_id'],entry()['ori'],'群内人员',25,0,0]]})
        session = reward_roster.create_reward_session('synthetic-mixed.xlsx', content, LOGIN)['session_id']
        reward_roster.analyze_reward_session(session,RewardAnalyzeRequest(selected_sheets=['合成表'],reward_dates=['2026-10-13'],mappings={'合成表':mapping()}),latest_roster=True)
        with patch.object(rewards,'read_roster_snapshot',AsyncMock(return_value=snapshot())):
            other = asyncio.run(rewards.create_other_reward(OtherRewardCreate(payout_date='2026-10-14',source_id='group_a',player_id=entry()['player_id'],amount=25,reason='合成抽奖'),LOGIN))
            selection = RewardSelection(dates=['2026-10-13','2026-10-14'],task_versions={task['id']:task['version']},other_reward_ids=[other['id']],imported_session_ids=[session])
            preview = asyncio.run(rewards.create_preview(selection,LOGIN))
        batch = self.generate(preview)
        self.assertEqual(batch['total_diamonds'],550)
        rewards.void_batch(batch['id'],self.action(),LOGIN)
        self.assertEqual(rewards._ledger()['claims'],{})
        self.assertTrue(rewards.choices(LOGIN)['other_rewards'][0]['selectable'])
        with patch.object(rewards,'read_roster_snapshot',AsyncMock(return_value=snapshot())):
            fresh = asyncio.run(rewards.create_preview(selection,LOGIN))
        replacement = self.generate(fresh,key='mixed-replacement-generation')
        self.assertEqual(replacement['total_diamonds'],550)
        self.assertNotIn('settlement',tasks.task_view(store.read('tasks',task['id']),LOGIN))


    def test_confirm_paid_endpoint_updates_only_batch_and_settlement_display(self):
        import httpx
        from fastapi import FastAPI
        from app.routers import reward_batches as router_module
        task, preview, _ = self.prepare()
        original = store.read('tasks', task['id'])
        batch = self.generate(preview)
        app = FastAPI(); app.include_router(router_module.router)
        async def scenario():
            with patch.object(router_module, '_current_login', AsyncMock(return_value=LOGIN)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic.test') as client:
                    response = await client.post('/api/reward-batches/'+batch['id']+'/confirm-paid',json={'expected_revision':1,'confirmed':True})
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(response.json()['status'],'paid')
                    self.assertEqual((await client.get('/api/reward-batches/'+batch['id']+'/download')).status_code,200)
        asyncio.run(scenario())
        self.assertEqual(store.read('tasks',task['id']),original)
        self.assertIn('settlement',tasks.task_view(original,LOGIN))


    def test_existing_batch_missing_backup_path_is_503_but_unknown_batch_is_404(self):
        _,preview,_=self.prepare();batch=self.generate(preview)
        before=rewards._ledger()
        with patch.object(store,'_verified_backup',side_effect=FileNotFoundError('synthetic long path')):
            with self.assertRaises(HTTPException) as error:
                rewards.void_batch(batch['id'],self.action(),LOGIN)
            self.assertEqual(error.exception.status_code,503)
        self.assertEqual(rewards._ledger(),before)
        with self.assertRaises(HTTPException) as error:
            rewards.void_batch('unknown-batch',self.action(),LOGIN)
        self.assertEqual(error.exception.status_code,404)


def serve_browser_validation():
    """Loopback-only real routers + synthetic fixtures; never import app.main."""
    import hashlib
    import json
    import os
    from pathlib import Path
    import socket
    import subprocess
    import sys
    from contextlib import ExitStack
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from app.core import config
    from app.routers import reward_batches as reward_router, group_tasks as group_router

    root = Path(__file__).resolve().parents[1]
    data = Path(config.DATA_DIR).resolve()
    allowed = (root / '.verification' / 'reward-batch-lifecycle').resolve()
    if not data.is_relative_to(allowed) or config.FEISHU_LOGIN_REQUIRED or (root / '.env').exists():
        raise RuntimeError('Browser validation requires explicit isolated DATA_DIR, fake login, and no .env')
    if store.list_records('tasks'):
        raise RuntimeError('Use a new isolated DATA_DIR; existing validation records are preserved')

    # Prevent all outbound connections; the server accepts only local browser requests.
    actual_connect = socket.socket.connect
    def blocked_connect(connection, address):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return actual_connect(connection, address)  # Windows asyncio self-pipe.
        raise RuntimeError('External/model connections are disabled in browser validation')

    case = RewardBatchTests()
    with ExitStack() as stack:
        stack.enter_context(patch.object(socket.socket, 'connect', blocked_connect))
        stack.enter_context(patch.object(rewards, 'read_roster_snapshot', AsyncMock(return_value=snapshot())))
        stack.enter_context(patch.object(reward_router, '_current_login', AsyncMock(return_value=LOGIN)))
        stack.enter_context(patch.object(group_router, '_current_login', AsyncMock(return_value=LOGIN)))
        task, preview, _ = case.prepare()
        first = case.generate(preview)
        from app.schemas.group_tasks import TaskRules
        second = case.make_task(labels=('有效反馈',))
        second = tasks.update_rules(second['id'],second['version'],TaskRules(**{**second['rules'],'name':'第二项合成任务'}),LOGIN)
        initial_tasks = store.list_records('tasks')
        hashes = {item['id']: hashlib.sha256((Path(config.GROUP_WORKFLOW_DATA_DIR)/'tasks'/(item['id']+'.json')).read_bytes()).hexdigest() for item in initial_tasks}
        (data / 'initial-task-hashes.json').write_text(json.dumps(hashes, indent=2), encoding='utf-8')
        (data / 'browser-fixtures.json').write_text(json.dumps({'task_id':task['id'],'other_task_id':second['id'],'batch_id':first['id'],'date':'2026-10-14'},indent=2),encoding='utf-8')
        html = subprocess.check_output(['node',str(root/'tests'/'reward_batch_lifecycle_ui.cjs'),'--browser-shell'],text=True,encoding='utf-8')
        app = FastAPI()
        app.include_router(reward_router.router)
        app.include_router(group_router.router)
        app.mount('/static', StaticFiles(directory=root/'static'), name='static')
        @app.get('/', response_class=HTMLResponse)
        async def page():
            return html

        listener = socket.socket()
        listener.bind(('127.0.0.1',0))
        listener.listen(128)
        print('Real-router synthetic browser validation: http://127.0.0.1:'+str(listener.getsockname()[1]),flush=True)
        print('Isolated DATA_DIR: '+str(data),flush=True)
        try:
            uvicorn.Server(uvicorn.Config(app,log_level='warning',access_log=False)).run(sockets=[listener])
        finally:
            listener.close()


if __name__ == '__main__' and '--serve-browser' in __import__('sys').argv:
    serve_browser_validation()
