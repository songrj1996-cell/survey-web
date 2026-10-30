import asyncio
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from fastapi import HTTPException
from openpyxl import Workbook

from app.core import config
from app.schemas.group_tasks import (TaskRules, FeedbackMapping, DuplicateDecision, QualityOverride, FeedbackCorrection)
from app.services import group_tasks as tasks
from app.storage import group_workflow as store

LOGIN = {"email": "owner@example.test", "name": "研究员甲"}


def entry(player="1234567890123456789", ori="818043063(6389)", source="group_a", eligible=True):
    return {"key": source+"_2", "source_id": source, "group_name": "测试群甲", "row_number": 2,
            "source_revision": 1, "player_id": player, "ori": ori, "status": "群内人员" if eligible else "已退群",
            "eligible": eligible, "nickname": "测试玩家", "identity_error": "", "status_error": ""}


def snapshot(entries=None):
    entries=entries or [entry()]
    sources=[{"id": source, "revision": 1, "name": "测试群"} for source in sorted({r["source_id"] for r in entries})]
    return {"id":"snapshot_test", "read_at":tasks.now(), "sources":sources, "entries":entries,
            "fingerprint":tasks.fingerprint({"sources": {s["id"]:s["revision"] for s in sources}, "entries":entries})}


class WorkflowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=config.DATA_DIR)
        self.patch = patch.object(config, "GROUP_WORKFLOW_DATA_DIR", Path(self.tmp.name)/"workflow")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def make_task(self, labels=("优秀反馈","优秀反馈","有效反馈","无效反馈"), requires_count=False, repeat=True, counts=None):
        created=tasks.create_task(TaskRules(name="合成体验任务", payout_date="2026-10-14", base_reward=100, excellent_reward=100,
                                          allow_repeat=repeat,requires_count=requires_count,min_count=3 if requires_count else None,ai_detect=False), LOGIN)
        rows=["playerid,回答,实际次数,ADV"]+[f"1234567890123456789,完整回答{i},{(counts or [3]*len(labels))[i]}," for i in range(len(labels))]
        created=tasks.upload_feedback(created["id"],created["version"],"feedback.csv","\n".join(rows).encode("utf-8-sig"),LOGIN)
        created=tasks.set_mapping(created["id"],created["version"],FeedbackMapping(id_col=0,open_text_cols=[1],count_col=2,adv_col=3),LOGIN)
        def seed(task):
            task["roster_snapshot"]=snapshot()
            for row,label in zip(task["feedback"],labels):
                if label:
                    row.update(quality={"overall":label,"overall_reason":"合成测试的完整判断依据",
                                        "q_labels":{"col_1":label}},quality_complete=True)
            tasks._refresh_checks(task)
        store.mutate("tasks",created["id"],seed)
        return store.read("tasks",created["id"])


class GroupTaskTests(WorkflowCase):
    def roster_task(self, entries, labels=('优秀反馈', '有效反馈')):
        task = self.make_task(labels=labels)
        def seed(current):
            current['roster_snapshot'] = snapshot(entries)
            tasks._refresh_checks(current)
        store.mutate('tasks', task['id'], seed)
        return tasks.task_view(store.read('tasks', task['id']), LOGIN)

    def test_only_active_identity_is_automatic_regardless_of_order_or_old_choice(self):
        active, retired = entry(), entry(source='group_b', eligible=False)
        active['status'] = '群内成员'
        for entries in ([active, retired], [retired, active]):
            task = self.roster_task(entries)
            task = store.read('tasks', task['id'])
            for row in task['feedback']:
                row['corrections']['candidate_key'] = retired['key']
            tasks._refresh_checks(task)
            self.assertTrue(all(row['identity']['key'] == active['key'] for row in task['feedback']))
            self.assertTrue(all(row['identity_selection'] == 'automatic' for row in task['feedback']))
            self.assertFalse(tasks.readiness_issues(task, settlement=True))
            self.assertEqual(tasks.task_view(task, LOGIN)['players'][0]['total'], 300)

    def test_multiple_active_identities_require_choice_and_save_without_note(self):
        first, second, retired = entry(), entry(source='group_b'), entry(source='group_c', eligible=False)
        task = self.roster_task([first, second, retired], labels=('有效反馈',))
        row = task['feedback'][0]
        self.assertIsNone(row['identity'])
        self.assertEqual([c['key'] for c in row['identity_choices']], [first['key'], second['key']])
        with self.assertRaises(HTTPException):
            tasks.confirm_task(task['id'], task['version'], LOGIN)
        updated = tasks.correct_feedback(task['id'], row['id'], FeedbackCorrection(
            expected_version=task['version'], candidate_key=second['key']), LOGIN)
        self.assertEqual(updated['feedback'][0]['identity']['key'], second['key'])
        self.assertEqual(updated['feedback'][0]['reviews'][-1]['note'], '')
        self.assertFalse(updated['confirmation_issues'])
        tasks.confirm_task(updated['id'], updated['version'], LOGIN)

    def test_identity_http_endpoint_accepts_no_note_and_rejects_stale_version(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers import group_tasks as router
        task = self.roster_task([entry(), entry(source='group_b')], labels=('有效反馈',))
        app = FastAPI()
        app.include_router(router.router)
        url = f"/api/group-tasks/{task['id']}/feedback/{task['feedback'][0]['id']}"
        with patch.object(router, '_current_login', AsyncMock(return_value=LOGIN)), TestClient(app) as client:
            payload = {'expected_version': task['version'], 'candidate_key': entry()['key']}
            result = client.put(url, json=payload)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(client.put(url, json=payload).status_code, 409)

    def test_no_active_identity_offers_exclusion_not_arbitrary_group(self):
        for entries in ([entry(eligible=False)], [entry(eligible=False), entry(source='group_b', eligible=False)]):
            task = self.roster_task(entries)
            row = task['feedback'][0]
            self.assertIsNone(row['identity'])
            self.assertTrue(row['reward_exclusion_available'])
            self.assertTrue(task['confirmation_issues'])

    def test_exclusion_covers_all_feedback_persists_and_restores_without_losing_quality(self):
        task = self.roster_task([entry(eligible=False), entry(source='group_b', eligible=False)])
        before = deepcopy(task['feedback'])
        row = before[0]
        excluded = tasks.correct_feedback(task['id'], row['id'], FeedbackCorrection(
            expected_version=task['version'], reward_excluded=True), LOGIN)
        self.assertTrue(all(r['reward_excluded'] for r in excluded['feedback']))
        self.assertEqual(excluded['summary']['base'] + excluded['summary']['excellent'], 0)
        self.assertFalse(excluded['confirmation_issues'])
        for old, new in zip(before, excluded['feedback']):
            for key in ('cells', 'quality', 'quality_complete'):
                self.assertEqual(old[key], new[key])
            self.assertEqual(old['outcome']['label'], new['outcome']['label'])
            self.assertFalse(new['excluded'])
        confirmed = tasks.confirm_task(task['id'], excluded['version'], LOGIN)
        reread = tasks.task_view(store.read('tasks', task['id']), LOGIN)
        self.assertTrue(reread['players'][0]['reward_excluded'])
        self.assertEqual(reread['events'][-2]['action'], '排除玩家奖励')
        restored = tasks.correct_feedback(task['id'], row['id'], FeedbackCorrection(
            expected_version=confirmed['version'], reward_excluded=False), LOGIN)
        self.assertFalse(restored['players'][0]['reward_excluded'])
        self.assertEqual(restored['players'][0]['total'], 300)
        self.assertTrue(restored['confirmation_issues'])
        self.assertIsNone(restored['confirmed_version'])

    def test_unmatched_player_can_exclude_all_rewards_and_restore(self):
        task = self.roster_task([entry(player='different')])
        before = deepcopy(task['feedback'])
        self.assertTrue(all(row['reward_exclusion_available'] for row in before))
        self.assertIn('名单中找不到 playerid', before[0]['issues'][0]['message'])
        updated = tasks.correct_feedback(task['id'], before[0]['id'], FeedbackCorrection(
            expected_version=task['version'], reward_excluded=True), LOGIN)
        self.assertTrue(all(row['reward_excluded'] for row in updated['feedback']))
        self.assertFalse(updated['confirmation_issues'])
        self.assertEqual(updated['players'][0]['total'], 0)
        decision = store.read('tasks', task['id'])['reward_exclusions'][before[0]['player_id']]
        self.assertEqual(decision['reason'], '名单中找不到 playerid')
        self.assertEqual(decision['snapshot_id'], 'snapshot_test')
        self.assertIn('owner_name', decision)
        for old, new in zip(before, updated['feedback']):
            for key in ('cells', 'quality', 'quality_complete'):
                self.assertEqual(old[key], new[key])
            self.assertEqual(old['outcome']['label'], new['outcome']['label'])
        confirmed = tasks.confirm_task(task['id'], updated['version'], LOGIN)
        restored = tasks.correct_feedback(task['id'], before[0]['id'], FeedbackCorrection(
            expected_version=confirmed['version'], reward_excluded=False), LOGIN)
        self.assertTrue(restored['confirmation_issues'])
        self.assertIsNone(restored['confirmed_version'])
        self.assertEqual(restored['players'][0]['total'], 300)
        self.assertFalse(restored['players'][0]['reward_excluded'])

    def test_unmatched_exclusion_endpoint_rejects_stale_and_read_only_access(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers import group_tasks as router
        task = self.roster_task([entry(player='different')], labels=('有效反馈',))
        app = FastAPI()
        app.include_router(router.router)
        url = f"/api/group-tasks/{task['id']}/feedback/{task['feedback'][0]['id']}"
        payload = {'expected_version': task['version'], 'reward_excluded': True}
        with patch.object(router, '_current_login', AsyncMock(return_value=LOGIN)), TestClient(app) as client:
            self.assertEqual(client.put(url, json=payload).status_code, 200)
            self.assertEqual(client.put(url, json=payload).status_code, 409)
        from app.services import feature_access
        with patch.object(router, '_current_login', AsyncMock(return_value=LOGIN)), \
             patch.object(config, 'FEISHU_LOGIN_REQUIRED', True), \
             patch.object(feature_access, '_get_user_permission_levels', return_value={'annotate': 'read'}), TestClient(app) as client:
            payload['expected_version'] += 1
            self.assertEqual(client.put(url, json=payload).status_code, 403)

    def test_exclusion_cannot_bypass_active_unknown_empty_or_failed_roster(self):
        unknown = entry(eligible=False)
        unknown.update(eligible=None, status='待核实', status_error='未知状态')
        for entries in ([entry()], [unknown], [entry(), entry(source='group_b')]):
            task = self.roster_task(entries)
            with self.assertRaises(HTTPException):
                tasks.correct_feedback(task['id'], task['feedback'][0]['id'], FeedbackCorrection(
                    expected_version=task['version'], reward_excluded=True), LOGIN)
        task = self.roster_task([entry(eligible=False)])
        store.mutate('tasks', task['id'], lambda current: current.update(roster_error='合成读取失败'))
        with self.assertRaises(HTTPException):
            tasks.correct_feedback(task['id'], task['feedback'][0]['id'], FeedbackCorrection(
                expected_version=task['version'], reward_excluded=True), LOGIN)

        for snapshot_value, raw_id, cell_issues in ((None, entry()['player_id'], {}),
                (snapshot([entry(player='different')]), '', {}),
                (snapshot([entry(player='different')]), entry()['player_id'], {'0': '身份数值已丢失精度'})):
            task = self.make_task(labels=('有效反馈',))
            def seed(current):
                current['roster_snapshot'] = snapshot_value
                current['feedback'][0]['cells'][0] = raw_id
                current['feedback'][0]['cell_issues'] = cell_issues
            store.mutate('tasks', task['id'], seed)
            with self.assertRaises(HTTPException):
                tasks.correct_feedback(task['id'], task['feedback'][0]['id'], FeedbackCorrection(
                    expected_version=task['version'], reward_excluded=True), LOGIN)

    def test_refresh_logs_automatic_count_and_invalidates_old_member_choice(self):
        task = self.roster_task([entry(), entry(source='group_b')], labels=('有效反馈',))
        row = task['feedback'][0]
        task = tasks.correct_feedback(task['id'], row['id'], FeedbackCorrection(
            expected_version=task['version'], candidate_key=entry(source='group_b')['key']), LOGIN)
        with patch('app.services.roster_sources.read_roster_snapshot', AsyncMock(
                return_value=snapshot([entry(), entry(source='group_b', eligible=False)]))):
            task = asyncio.run(tasks.refresh_roster(task['id'], task['version'], LOGIN))['task']
        self.assertEqual(task['feedback'][0]['identity']['source_id'], 'group_a')
        self.assertEqual(task['events'][-1]['details']['自动匹配玩家数'], 1)

    def test_multiple_group_settings_preserve_annotation_identity_and_rewards(self):
        from app.schemas.group_tasks import TaskSettingsUpdate
        task = self.make_task(labels=('有效反馈',))
        task = tasks.override_quality(task['id'], task['feedback'][0]['id'],
            QualityOverride(expected_version=task['version'], label='优秀反馈', note='人工复核'), LOGIN)
        for identifier, name in [('note_a', '备注群甲'), ('note_b', '备注群乙')]:
            store.create('sources', identifier, {'id': identifier, 'name': name, 'enabled': True})
        request = dict(name=task['rules']['name'], payout_date=task['rules']['payout_date'],
                       base_reward=100, excellent_reward=100)
        updated = tasks.update_settings(task['id'], TaskSettingsUpdate(expected_version=task['version'],
            group_source_ids=['note_a', 'note_b', 'note_a'], **request), LOGIN)
        self.assertEqual(updated['rules']['group_source_ids'], ['note_a', 'note_b'])
        self.assertEqual(updated['rules']['group_names'], ['备注群甲', '备注群乙'])
        self.assertEqual(updated['rules']['group_name'], '备注群甲、备注群乙')
        self.assertEqual(updated['rules']['group_source_id'], 'note_a')
        self.assertEqual(updated['feedback'], task['feedback'])
        self.assertEqual(updated['players'], task['players'])
        self.assertEqual(tasks.load_task(task['id'], LOGIN)['rules'], updated['rules'])
        # Omitting the optional group field must preserve the full selection.
        unchanged = tasks.update_settings(task['id'], TaskSettingsUpdate(expected_version=updated['version'], **request), LOGIN)
        self.assertEqual(unchanged['rules']['group_source_ids'], ['note_a', 'note_b'])
        cleared = tasks.update_settings(task['id'], TaskSettingsUpdate(expected_version=unchanged['version'],
            group_source_ids=[], **request), LOGIN)
        self.assertEqual(cleared['rules']['group_source_ids'], [])
        self.assertEqual(cleared['rules']['group_names'], [])
        self.assertEqual(cleared['rules']['group_source_id'], '')
        self.assertEqual(cleared['rules']['group_name'], '')

    def test_group_multiselect_legacy_and_inactive_sources(self):
        from app.schemas.group_tasks import TaskSettingsUpdate
        task = self.make_task(labels=('有效反馈',))
        # Simulate an old record without changing it during reads.
        def legacy(record):
            record['rules'].pop('group_source_ids', None)
            record['rules'].pop('group_names', None)
            record['rules'].update(group_source_id='old_group', group_name='历史归属群')
        store.mutate('tasks', task['id'], legacy)
        before = store.read('tasks', task['id'])
        tasks.task_view(before, LOGIN)
        self.assertEqual(store.read('tasks', task['id']), before)
        store.create('sources', 'new_group', {'id': 'new_group', 'name': '新群', 'enabled': True})
        request = dict(name=task['rules']['name'], payout_date=task['rules']['payout_date'], base_reward=100, excellent_reward=100)
        updated = tasks.update_settings(task['id'], TaskSettingsUpdate(expected_version=before['version'],
            group_source_ids=['old_group', 'new_group'], **request), LOGIN)
        self.assertEqual(updated['rules']['group_names'], ['历史归属群', '新群'])
        store.mutate('sources', 'new_group', lambda record: record.update(enabled=False))
        kept = tasks.update_settings(task['id'], TaskSettingsUpdate(expected_version=updated['version'],
            group_source_ids=['old_group', 'new_group'], **request), LOGIN)
        self.assertEqual(kept['rules']['group_names'], ['历史归属群', '新群'])
        for identifiers in (['unknown_group'], ['new_group']):
            with self.assertRaises(HTTPException) as caught:
                tasks.create_task(TaskRules(group_source_ids=identifiers), LOGIN)
            self.assertEqual(caught.exception.status_code, 422)
        # Old clients can still clear or replace the single-group field.
        legacy_update = tasks.update_settings(task['id'], TaskSettingsUpdate(expected_version=kept['version'],
            group_source_id='', **request), LOGIN)
        self.assertEqual(legacy_update['rules']['group_source_ids'], [])

    def test_group_multiselect_creation_derives_names_and_accepts_long_display(self):
        names = ['群甲' * 70, '群乙' * 70]
        for identifier, name in zip(['group_1', 'group_2'], names):
            store.create('sources', identifier, {'id': identifier, 'name': name, 'enabled': True})
        task = tasks.create_task(TaskRules(group_source_ids=['group_1', 'group_2'],
                                         group_names=['伪造名称'], group_name='伪造合并名称'), LOGIN)
        self.assertEqual(task['rules']['group_names'], names)
        self.assertGreater(len(task['rules']['group_name']), 200)
        roundtrip = tasks.update_rules(task['id'], task['version'], TaskRules(**task['rules']), LOGIN)
        self.assertEqual(roundtrip['rules']['group_names'], names)
        from pydantic import ValidationError
        for identifiers in ([''], ['x' * 129]):
            with self.assertRaises(ValidationError):
                TaskRules(group_source_ids=identifiers)

    def test_settings_preserve_annotations_and_lock_analysis_rules(self):
        from app.schemas.group_tasks import TaskSettingsUpdate
        task=self.make_task(labels=('有效反馈',))
        task=tasks.override_quality(task['id'],task['feedback'][0]['id'],QualityOverride(expected_version=task['version'],label='优秀反馈',note='人工复核'),LOGIN)
        store.create('sources','note_only',{'id':'note_only','name':'仅备注群','enabled':True})
        updated=tasks.update_settings(task['id'],TaskSettingsUpdate(expected_version=task['version'],name='改名',payout_date='2026-11-01',base_reward=150,excellent_reward=80,group_source_id='note_only'),LOGIN)
        self.assertEqual(updated['feedback'][0]['quality'],task['feedback'][0]['quality'])
        self.assertEqual(updated['feedback'][0]['manual_quality'],task['feedback'][0]['manual_quality'])
        self.assertEqual(updated['players'][0]['total'],230)
        self.assertEqual(updated['players'][0]['source_id'],'group_a')
        for key,value in [('requires_count',True),('allow_repeat',False),('announcement','改变标注依据'),('ai_detect',True)]:
            with self.assertRaises(HTTPException):
                tasks.update_rules(task['id'],updated['version'],TaskRules(**{**updated['rules'],key:value}),LOGIN)

    def test_upload_is_immutable_even_before_model_runs(self):
        task=self.make_task(labels=(None,))
        with self.assertRaises(HTTPException):
            tasks.upload_feedback(task['id'],task['version'],'replacement.csv',b'playerid,answer\np1,new',LOGIN)
        self.assertEqual(store.read('tasks',task['id']),task)
        self.assertEqual(len(list((Path(self.tmp.name)/'workflow/uploads').glob('*.bin'))),1)

    def test_delete_backups_are_complete_and_access_is_revoked(self):
        task=self.make_task()
        tasks.delete_task(task['id'],task['version'],LOGIN)
        self.assertNotIn(task['id'],[item['id'] for item in tasks.list_tasks(LOGIN)])
        with self.assertRaises(HTTPException):tasks.load_task(task['id'],LOGIN)
        archive=Path(config.GROUP_WORKFLOW_DATA_DIR)/'deleted_tasks'/f"{task['id']}-v{task['version']}"
        import json
        self.assertEqual(json.loads((archive/'task.json').read_text(encoding='utf-8')),task)
        self.assertEqual((archive/f"{task['uploads'][0]['id']}.bin").read_bytes(),store.read_blob(task['uploads'][0]['id']))
        with self.assertRaises(HTTPException):tasks.update_rules(task['id'],task['version'],TaskRules(**task['rules']),LOGIN)

    def test_delete_refuses_other_owner_busy_stale_and_failed_backup(self):
        task=self.make_task()
        for version,login in [(1,LOGIN),(task['version'],{'email':'other@example.test'})]:
            with self.assertRaises(HTTPException):tasks.delete_task(task['id'],version,login)
        store.mutate('tasks',task['id'],lambda t:t.update(run={'status':'running'}))
        with self.assertRaises(HTTPException):tasks.delete_task(task['id'],task['version'],LOGIN)
        store.mutate('tasks',task['id'],lambda t:t.update(run=None))
        before=store.read('tasks',task['id'])
        with patch.object(store,'read_blob',return_value=b'corrupt'):
            with self.assertRaises(HTTPException):tasks.delete_task(task['id'],task['version'],LOGIN)
        self.assertEqual(store.read('tasks',task['id']),before)

    def test_settings_http_contract_rejects_extra_fields_and_deletes(self):
        import httpx
        from fastapi import FastAPI
        from app.routers import group_tasks as routes
        task=self.make_task()
        app=FastAPI();app.include_router(routes.router)
        payload={'expected_version':task['version'],'name':'API 设置','payout_date':'2026-10-20','base_reward':50,'excellent_reward':10,'group_source_id':''}
        async def check():
            with patch.object(routes,'_current_login',AsyncMock(return_value=LOGIN)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    response=await client.put(f"/api/group-tasks/{task['id']}/settings",json={**payload,'requires_count':True})
                    self.assertEqual(response.status_code,422)
                    response=await client.put(f"/api/group-tasks/{task['id']}/settings",json=payload)
                    self.assertEqual(response.status_code,200)
                    response=await client.request('DELETE',f"/api/group-tasks/{task['id']}",json={'expected_version':response.json()['version']})
                    self.assertEqual(response.status_code,200)
                    self.assertEqual((await client.get(f"/api/group-tasks/{task['id']}")).status_code,404)
        asyncio.run(check())

    def test_four_feedback_500_and_version_invalidation(self):
        task=self.make_task()
        view=tasks.confirm_task(task["id"],task["version"],LOGIN)
        player=view["players"][0]
        self.assertEqual((player["feedback_count"],player["base"],player["excellent"],player["total"]),(4,300,200,500))
        changed=tasks.update_rules(task["id"],view["version"],TaskRules(**{**task["rules"],"base_reward":150}),LOGIN)
        self.assertIsNone(changed["confirmed_version"])
        self.assertEqual(changed["players"][0]["total"],650)
        self.assertEqual(changed["feedback"][0]["quality"],task["feedback"][0]["quality"])

    def test_gate_before_ai_and_missing_not_zero(self):
        task=self.make_task(labels=(None,None,None),requires_count=True,counts=[2,"",3])
        results=[tasks.effective_result(task,row) for row in task["feedback"]]
        self.assertEqual((results[0]["label"],results[0]["base"],results[0]["source"]),("无效反馈",0,"rule"))
        self.assertIsNone(task["feedback"][1]["count"])
        self.assertIsNone(results[1]["base"])
        self.assertFalse(results[2]["complete"])
        with self.assertRaises(HTTPException):tasks.confirm_task(task["id"],task["version"],LOGIN)

    def test_duplicates_are_kept_for_audit_excluded_from_count(self):
        task=self.make_task(repeat=False)
        group=task["duplicate_groups"][0]
        self.assertTrue(tasks.readiness_issues(task))
        result=tasks.resolve_duplicate(task["id"],DuplicateDecision(expected_version=task["version"],group_id=group["id"],keep_response_id=task["feedback"][0]["id"]),LOGIN)
        self.assertEqual(len(result["feedback"]),4)
        self.assertEqual(result["summary"]["feedback_count"],1)
        self.assertEqual(result["players"][0]["total"],200)

    def test_manual_override_restore_and_audit(self):
        task=self.make_task(labels=("有效反馈",))
        rid=task["feedback"][0]["id"]
        result=tasks.override_quality(task["id"],rid,QualityOverride(expected_version=task["version"],label="优秀反馈",note="完整反馈包含可用建议"),LOGIN)
        self.assertEqual(result["players"][0]["total"],200)
        self.assertEqual(result["feedback"][0]["quality"]["overall"],"有效反馈")
        result=tasks.override_quality(task["id"],rid,QualityOverride(expected_version=result["version"],restore=True,note="复核后恢复原判断"),LOGIN)
        self.assertEqual(result["players"][0]["total"],100)
        self.assertEqual(len(result["feedback"][0]["reviews"]),2)

    def test_failed_roster_read_retains_snapshot_and_blocks_confirmation(self):
        task=self.make_task()
        with patch("app.services.roster_sources.read_roster_snapshot",AsyncMock(side_effect=HTTPException(403,"无名单权限"))):
            result=asyncio.run(tasks.refresh_roster(task["id"],task["version"],LOGIN))
        self.assertFalse(result["ok"])
        saved=store.read("tasks",task["id"])
        self.assertEqual(saved["roster_snapshot"],task["roster_snapshot"])
        self.assertFalse(tasks.readiness_issues(saved))
        self.assertTrue(tasks.readiness_issues(saved, settlement=True))

    def test_long_excel_number_rejected_but_original_text_kept(self):
        book=Workbook();sheet=book.active;sheet.append(["playerid","回答"]);sheet.append([1234567890123456789,"示例完整回答"])
        content=BytesIO();book.save(content)
        task=tasks.create_task(TaskRules(name="精度检查",payout_date="2026-10-15",base_reward=0,excellent_reward=0),LOGIN)
        task=tasks.upload_feedback(task["id"],task["version"],"precision.xlsx",content.getvalue(),LOGIN)
        self.assertIn("0",task["feedback"][0]["cell_issues"])
        self.assertEqual(store.read_blob(task["uploads"][0]["id"]),content.getvalue())

    def test_stale_version_and_busy_inputs_cannot_mutate(self):
        task=self.make_task()
        with self.assertRaises(HTTPException) as caught:tasks.update_rules(task["id"],1,TaskRules(**task["rules"]),LOGIN)
        self.assertEqual(caught.exception.status_code,409)
        store.mutate("tasks",task["id"],lambda t:t.update(run={"status":"queued"}))
        with self.assertRaises(HTTPException):tasks.update_rules(task["id"],task["version"],TaskRules(**task["rules"]),LOGIN)

    def test_ori_collision_detected_for_distinct_playerids(self):
        task=self.make_task(labels=("有效反馈","优秀反馈"),repeat=False)
        def change(t):
            t["feedback"][1]["corrections"]["player_id"]="other-player"
            second=entry("other-player");second["key"]="group_a_3"
            t["roster_snapshot"]=snapshot([entry(),second]);tasks._refresh_checks(t)
        store.mutate("tasks",task["id"],change)
        task=store.read("tasks",task["id"])
        self.assertEqual(len(task["duplicate_groups"]),1)

    def test_original_count_cannot_be_changed_in_existing_task(self):
        task=self.make_task(labels=('有效反馈',),requires_count=True)
        with self.assertRaises(HTTPException):
            tasks.correct_feedback(task['id'],task['feedback'][0]['id'],FeedbackCorrection(expected_version=task['version'],count=None,note='原始次数无法核实'),LOGIN)
        self.assertEqual(store.read('tasks',task['id']),task)

    def test_announcement_only_keeps_typed_evidenced_suggestions(self):
        from app.services import task_announcement as announcement
        import json
        original='活动甲，基础奖励100钻，优秀额外50钻。10月20日截止。'
        response={'name':{'value':'活动甲','evidence':'活动甲'},'base_reward':{'value':100,'evidence':'基础奖励100钻'},'excellent_reward':{'value':50,'evidence':'优秀额外50钻'},'requires_count':{'value':True,'evidence':'不存在的句子'},'min_count':{'value':True,'evidence':'100'},'payout_date':'2026-10-20'}
        with patch.object(announcement,'collect_chat_completion',AsyncMock(return_value=(json.dumps(response),None))),patch.object(announcement,'require_background_llm_api_key',return_value='test'),patch.object(announcement,'start_llm_usage_task',return_value=None):
            result=asyncio.run(announcement.extract_announcement(original,LOGIN))
        self.assertEqual(set(result['suggestions']),{'name','base_reward','excellent_reward'})
        self.assertIn('payout_date',result['needs_confirmation'])
