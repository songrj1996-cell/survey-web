"""Synthetic group-only administrator access and account preference regression checks."""
import asyncio
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
import httpx
from pydantic import ValidationError

from test_group_tasks import WorkflowCase, LOGIN, snapshot
from app.core import config, security
from app.schemas.group_tasks import TaskSettingsUpdate
from app.schemas.requests import GroupManagementPreferencesPatch
from app.schemas.reward_batches import RewardSelection, BatchGenerate
from app.services import auth, feature_access, group_access, group_tasks as tasks
from app.services import reward_batches as rewards, settings_service
from app.storage import group_workflow as store

ADMIN = {"email": "admin@example.test", "name": "管理员甲"}
ADMIN_B = {"email": "admin-b@example.test", "name": "管理员乙"}
OTHER = {"email": "other@example.test", "name": "其他成员"}


class GroupAdminAccessTests(WorkflowCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(security, "FEISHU_ADMIN_EMAILS", {ADMIN['email'], ADMIN_B['email']}))
        self.enterContext(patch.object(security, "FEISHU_ALLOWED_EMAILS", set()))

    def only_self(self, enabled=True, login=None):
        login = login or ADMIN
        saved = group_access.admin_preferences(login) or {'revision': 0}
        return group_access.save_admin_preferences(login, GroupManagementPreferencesPatch(
            only_self=enabled, expected_revision=saved['revision']))

    def test_default_and_persisted_preferences_are_account_specific(self):
        self.assertEqual(group_access.admin_preferences(ADMIN), {'only_self': False, 'revision': 0})
        self.assertIsNone(group_access.admin_preferences(LOGIN))
        self.only_self()
        self.assertTrue(group_access.admin_preferences(ADMIN)['only_self'])
        self.assertFalse(group_access.admin_preferences(ADMIN_B)['only_self'])
        self.assertEqual(len(store.list_records('preferences')), 1)
        self.only_self(False)
        self.assertFalse(group_access.admin_preferences(ADMIN)['only_self'])

    def test_non_admin_and_stale_updates_cannot_change_preferences(self):
        with self.assertRaises(HTTPException) as error:
            self.only_self(login=LOGIN)
        self.assertEqual(error.exception.status_code, 403)
        self.only_self()
        with self.assertRaises(HTTPException) as error:
            group_access.save_admin_preferences(ADMIN, GroupManagementPreferencesPatch(only_self=False, expected_revision=0))
        self.assertEqual(error.exception.status_code, 409)
        self.assertTrue(group_access.admin_preferences(ADMIN)['only_self'])
        with self.assertRaises(ValidationError):
            GroupManagementPreferencesPatch(only_self='false', expected_revision=1)

    def test_unreadable_preferences_never_fall_back_to_all_records(self):
        task = self.make_task()
        with patch.object(store, 'read', side_effect=OSError('synthetic read failure')):
            with self.assertRaises(HTTPException) as error:
                group_access.record_scope(ADMIN)
            self.assertEqual(error.exception.status_code, 503)
        with patch.object(store, 'read', return_value={'only_self': 'true', 'revision': 1}):
            with self.assertRaises(HTTPException) as error:
                group_access.require_record(task, ADMIN)
            self.assertEqual(error.exception.status_code, 503)

    def test_admin_edits_other_task_without_changing_owner_and_records_operator(self):
        task = self.make_task()
        self.assertTrue(tasks.task_view(task, ADMIN)['can_edit'])
        changed = tasks.update_settings(task['id'], TaskSettingsUpdate(
            expected_version=task['version'], name='管理员修正名称', payout_date='2026-10-14',
            base_reward=100, excellent_reward=100), ADMIN)
        self.assertEqual(changed['owner_key'], task['owner_key'])
        self.assertEqual(changed['owner_name'], task['owner_name'])
        self.assertEqual(changed['rules']['name'], '管理员修正名称')
        self.assertEqual(changed['events'][-1]['actor_name'], ADMIN['name'])
        self.assertIsNotNone(datetime.fromisoformat(changed['events'][-1]['at']).utcoffset())
        with self.assertRaises(HTTPException) as error:
            tasks.update_settings(task['id'], TaskSettingsUpdate(
                expected_version=task['version'], name='旧版本不得覆盖', payout_date='2026-10-14',
                base_reward=100, excellent_reward=100), ADMIN)
        self.assertEqual(error.exception.status_code, 409)

    def test_regular_member_and_readonly_grants_still_block_writes(self):
        task = self.make_task()
        with self.assertRaises(HTTPException):
            tasks.load_task(task['id'], OTHER, write=True)
        with patch.object(config, 'FEISHU_LOGIN_REQUIRED', True), patch.object(
            feature_access, '_get_user_permission_levels', return_value={'annotate': 'read'}), patch.object(
            auth, '_get_user_permission_levels', return_value={'annotate': 'read'}):
            self.assertFalse(tasks.task_view(task, ADMIN)['can_edit'])
            with self.assertRaises(HTTPException) as error:
                tasks.load_task(task['id'], ADMIN, write=True)
            self.assertEqual(error.exception.status_code, 403)
        # The generic report/history gate deliberately retains its old contract.
        with self.assertRaises(HTTPException):
            feature_access.require_record(task, ADMIN, write=True)

    def test_only_self_filters_list_and_blocks_direct_task_links(self):
        task = self.make_task()
        own = deepcopy(task); own['id'] = 'admin-owned'; own.update(feature_access.owner_identity(ADMIN))
        store.create('tasks', own['id'], own)
        self.assertEqual(len(tasks.list_tasks(ADMIN)), 2)
        self.only_self()
        self.assertEqual([item['id'] for item in tasks.list_tasks(ADMIN)], ['admin-owned'])
        for write in (False, True):
            with self.assertRaises(HTTPException) as error:
                tasks.load_task(task['id'], ADMIN, write=write)
            self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(tasks.load_task(own['id'], ADMIN)['id'], own['id'])
        self.only_self(False)
        self.assertEqual(tasks.load_task(task['id'], ADMIN, write=True)['id'], task['id'])

    def test_lists_read_preference_once_per_list_not_per_record(self):
        self.make_task()
        self.make_task()
        with patch.object(group_access, 'admin_preferences', wraps=group_access.admin_preferences) as reads:
            tasks.list_tasks(ADMIN)
            self.assertEqual(reads.call_count, 1)
        with patch.object(group_access, 'admin_preferences', wraps=group_access.admin_preferences) as reads:
            rewards.choices(ADMIN)
            self.assertEqual(reads.call_count, 1)

    def test_reward_choices_cover_tasks_imports_other_rewards_and_batches(self):
        task = self.make_task()
        owner = feature_access.owner_identity(LOGIN)
        store.create('other_rewards', 'other', {**owner, 'id': 'other', 'payout_date': '2026-10-14'})
        store.create('imports', 'import', {**owner, 'id': 'import', 'filename': 'synthetic.xlsx'})
        store.create('ledger', 'payouts', {'claims': {}, 'idempotency': {}, 'batches': {
            'batch': {**owner, 'id': 'batch', 'task_versions': {}, 'dates': ['2026-10-14']}}})
        with patch.object(rewards, 'import_is_ready', return_value=True):
            choices = rewards.choices(ADMIN)
            self.assertTrue(choices['tasks'][0]['selectable'])
            self.assertTrue(choices['imports'][0]['selectable'])
            self.assertTrue(choices['other_rewards'][0]['selectable'])
            self.assertEqual(rewards.get_batch('batch', ADMIN)['id'], 'batch')
            self.only_self()
            choices = rewards.choices(ADMIN)
            for key in ('tasks', 'imports', 'other_rewards', 'batches'):
                self.assertEqual(choices[key], [])
            with self.assertRaises(HTTPException):
                rewards.get_batch('batch', ADMIN)

    def test_admin_settles_another_task_and_duplicate_claims_still_block(self):
        task = self.make_task()
        store.create('sources', 'group_a', {'id': 'group_a', 'name': '合成群', 'revision': 1, 'enabled': True})
        selection = RewardSelection(dates=['2026-10-14'], task_versions={task['id']: task['version']})
        with patch.object(rewards, 'read_roster_snapshot', AsyncMock(return_value=snapshot())):
            preview = asyncio.run(rewards.create_preview(selection, ADMIN))
            batch = asyncio.run(rewards.generate_batch(preview['id'], BatchGenerate(
                expected_revision=preview['revision'], idempotency_key='admin-settlement-check-001'), ADMIN))
        self.assertEqual(batch['owner_key'], feature_access.owner_identity(ADMIN)['owner_key'])
        self.assertEqual(store.read('tasks', task['id'])['owner_key'], task['owner_key'])
        self.assertFalse(rewards.choices(ADMIN)['tasks'][0]['selectable'])
        with self.assertRaises(HTTPException):
            asyncio.run(rewards.create_preview(selection, ADMIN))

    def test_only_self_blocks_new_reward_selection_and_import_deletion(self):
        task = self.make_task()
        owner = feature_access.owner_identity(LOGIN)
        store.create('imports', 'synthetic-import', {**owner, 'id': 'synthetic-import'})
        self.only_self()
        with self.assertRaises(HTTPException):
            asyncio.run(rewards.create_preview(RewardSelection(
                dates=['2026-10-14'], task_versions={task['id']: task['version']}), ADMIN))
        with self.assertRaises(HTTPException):
            rewards.delete_import('synthetic-import', ADMIN)
        self.assertFalse(store.read('imports', 'synthetic-import').get('deleted_at', False))

    def test_view_scope_change_preserves_admitted_background_access(self):
        task = self.make_task()
        self.only_self()
        with self.assertRaises(HTTPException):
            tasks.load_task(task['id'], ADMIN, write=True)
        with group_access.continue_authorized_run():
            self.assertEqual(tasks.load_task(task['id'], ADMIN, write=True)['id'], task['id'])
            with self.assertRaises(HTTPException):
                tasks.load_task(task['id'], OTHER, write=True)
        with self.assertRaises(HTTPException):
            tasks.load_task(task['id'], ADMIN, write=True)

    def test_settings_display_exposes_current_admin_preference_only(self):
        with patch.object(settings_service, 'get_app_settings', return_value={}):
            self.assertNotIn('group_management_preferences', settings_service.get_app_settings_for_display(LOGIN))
            self.assertFalse(settings_service.get_app_settings_for_display(ADMIN)['group_management_preferences']['only_self'])
            self.only_self()
            self.assertTrue(settings_service.get_app_settings_for_display(ADMIN)['group_management_preferences']['only_self'])
            self.assertFalse(settings_service.get_app_settings_for_display(ADMIN_B)['group_management_preferences']['only_self'])

    def test_settings_api_persists_scoped_preference_and_audits_only_success(self):
        from app.routers import settings_api as routes
        app = FastAPI(); app.include_router(routes.router)
        async def exercise():
            with patch.object(routes, '_require_settings_access', AsyncMock(return_value=ADMIN)), patch.object(
                routes, 'audit_log', AsyncMock()) as audit:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated.test') as client:
                    response = await client.patch('/api/group-management/preferences', json={'only_self': True, 'expected_revision': 0})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertTrue(response.json()['only_self'])
                    audit.assert_awaited_once()
                    self.assertIn('仅看自己', audit.call_args.args[-1])
                    response = await client.patch('/api/group-management/preferences', json={'only_self': False, 'expected_revision': 0})
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(audit.await_count, 1)
                    response = await client.patch('/api/group-management/preferences', json={'only_self': 'false', 'expected_revision': 1})
                    self.assertEqual(response.status_code, 422)
        asyncio.run(exercise())

    def test_real_task_and_reward_endpoints_follow_scope_and_admin_role(self):
        from app.routers import group_tasks as task_routes, reward_batches as reward_routes, settings_api
        task = self.make_task()
        app = FastAPI()
        for module in (task_routes, reward_routes, settings_api):
            app.include_router(module.router)
        async def exercise():
            with patch.object(task_routes, '_current_login', AsyncMock(return_value=ADMIN)), patch.object(
                reward_routes, '_current_login', AsyncMock(return_value=ADMIN)), patch.object(
                settings_api, '_require_settings_access', AsyncMock(return_value=ADMIN)), patch.object(
                settings_api, 'audit_log', AsyncMock()):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated.test') as client:
                    response = await client.get('/api/group-tasks/' + task['id'])
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()['can_edit'])
                    response = await client.put('/api/group-tasks/' + task['id'] + '/settings', json={
                        'expected_version': task['version'], 'name': 'HTTP 管理员修改', 'payout_date': '2026-10-14',
                        'base_reward': 100, 'excellent_reward': 100})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()['owner_key'], task['owner_key'])
                    response = await client.get('/api/reward-batches/choices')
                    self.assertTrue(response.json()['tasks'][0]['selectable'])
                    response = await client.patch('/api/group-management/preferences', json={'only_self': True, 'expected_revision': 0})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual((await client.get('/api/group-tasks')).json()['tasks'], [])
                    self.assertEqual((await client.get('/api/group-tasks/' + task['id'])).status_code, 404)
                    self.assertEqual((await client.get('/api/reward-batches/choices')).json()['tasks'], [])
            with patch.object(settings_api, '_require_settings_access', AsyncMock(return_value=OTHER)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated.test') as client:
                    response = await client.patch('/api/group-management/preferences', json={'only_self': True, 'expected_revision': 0})
                    self.assertEqual(response.status_code, 403)
        asyncio.run(exercise())

    def test_actual_background_continuation_is_not_stopped_by_only_self(self):
        from app.schemas.group_tasks import RunTask
        from app.services import group_task_runner as runner
        from test_group_annotation_parity import create_sample, ai_result
        task = create_sample(ai=True, quality=True)
        async def annotate(current, row, checkpoint):
            checkpoint({'ai': ai_result(row, probability=5), 'ai_complete': True})
        async def exercise():
            with patch.object(runner, 'require_background_llm_api_key', return_value='synthetic-only'), patch.object(
                runner, 'start_llm_usage_task', return_value=None), patch.object(runner, 'annotate_group_feedback', annotate):
                runner.start_run(task['id'], RunTask(expected_version=task['version'], stage='ai'), ADMIN)
                self.only_self()
                for _ in range(4):
                    await runner._run_slice(task['id'], asyncio.Semaphore(2))
                    if store.read('tasks', task['id'])['run']['stage'] == 'quality':
                        break
            restored = store.read('tasks', task['id'])
            self.assertEqual(restored['run']['stage'], 'quality')
            self.assertEqual(restored['run']['status'], 'queued')
            self.assertEqual(restored['owner_key'], task['owner_key'])
            self.assertNotIn('annotation_auto_error', restored)
        asyncio.run(exercise())
