import asyncio
from copy import deepcopy
from unittest.mock import patch, AsyncMock

from fastapi import HTTPException

from test_group_tasks import WorkflowCase, LOGIN, snapshot, entry
from app.schemas.reward_batches import RewardSelection, BatchGenerate, OtherRewardCreate
from app.services import group_tasks as tasks, reward_batches as rewards
from app.storage import group_workflow as store


class RewardBatchTests(WorkflowCase):
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
