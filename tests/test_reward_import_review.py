import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
from io import BytesIO
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from openpyxl import Workbook

from test_group_tasks import WorkflowCase, LOGIN, entry, snapshot
from app.core import config, security
from app.schemas.reward_roster import RewardAnalyzeRequest, RewardSheetMapping
from app.schemas.reward_batches import RewardSelection, BatchResolution, BatchGenerate
from app.services import reward_roster as imports, reward_batches as rewards, roster_sources, auth
from app.storage import group_workflow as store


class RewardImportReviewTests(WorkflowCase):
    def test_preview_does_not_rewrite_checked_import_dates(self):
        sid, request = self.upload()
        before = deepcopy(store.read('imports', sid))
        preview = imports.analyze_reward_session(sid, request, latest_roster=True, preview_only=True)
        self.assertEqual(len(preview['eligible_rows']), 2)
        self.assertEqual(store.read('imports', sid), before)
        self.assertEqual(imports.get_reward_session(sid)['analysis_request'], before['analysis_request'])

    def test_only_successfully_checked_imports_are_choices_and_sources(self):
        sid, _ = self.upload()
        self.assertEqual([item['id'] for item in rewards.choices(LOGIN)['imports']], [sid])
        saved = deepcopy(store.read('imports', sid))
        for analysis in (None, {**saved['analysis'], 'invalid_rows': [{'reason': '金额错误'}]}):
            store.mutate('imports', sid, lambda item: item.update(analysis=analysis))
            imports._SESSIONS.pop(sid, None)
            self.assertEqual(rewards.choices(LOGIN)['imports'], [])
            with self.assertRaises(HTTPException) as blocked:
                self.preview(sid)
            self.assertIn('未通过检查', str(blocked.exception.detail))

    def upload(self, with_status=False, old_status='群内人员', old_account=None):
        wb = Workbook(); ws = wb.active; ws.title = '任务完成记录'
        ws.append(['奖励发放日期', '任务', 'playerid', 'ori', '普通奖励'] + (['当前状态'] if with_status else []))
        for task in ('任务甲', '任务乙'):
            ws.append(['2026-10-15', task, entry()['player_id'], old_account or entry()['ori'], 50] + ([old_status] if with_status else []))
        output = BytesIO(); wb.save(output); wb.close()
        uploaded = imports.create_reward_session('奖励测试.xlsx', output.getvalue(), LOGIN)
        sid = uploaded['session_id']
        mapping = RewardSheetMapping(date_col=0, task_col=1, player_id_col=2, account_id_col=3, normal_reward_col=4,
                                     status_col=5 if with_status else None)
        request = RewardAnalyzeRequest(selected_sheets=['任务完成记录'], reward_dates=['2026-10-15'], mappings={'任务完成记录': mapping})
        imports.analyze_reward_session(sid, request, latest_roster=True)
        return sid, request

    def preview(self, sid, roster=None):
        roster = roster or snapshot()
        for source in roster['sources']:
            if not store.read('sources', source['id']):
                store.create('sources', source['id'], {**source, 'enabled': True})
        with patch.object(rewards, 'read_roster_snapshot', AsyncMock(return_value=roster)):
            return asyncio.run(rewards.create_preview(RewardSelection(dates=['2026-10-15'], imported_session_ids=[sid]), LOGIN))

    def generate(self, preview, roster=None):
        with patch.object(rewards, 'read_roster_snapshot', AsyncMock(return_value=roster or snapshot())):
            return asyncio.run(rewards.generate_batch(preview['id'], BatchGenerate(expected_revision=preview['revision'], idempotency_key='import-review-key-1234'), LOGIN))

    def test_no_status_column_uses_live_roster_without_false_changes(self):
        sid, request = self.upload()
        preview = self.preview(sid)
        self.assertTrue(preview['ready'])
        self.assertEqual(preview['roster_changes'], [])
        self.assertEqual(preview['result']['total_diamonds'], 100)
        self.assertEqual(self.generate(preview)['txt_preview'], '6389\t818043063\t301-100\r\n')
        with self.assertRaises(HTTPException):
            imports.analyze_reward_session(sid, request)  # Legacy standalone path must not bypass status.

    def test_retired_candidate_does_not_block_two_tasks(self):
        sid, _ = self.upload()
        retired = entry(source='group_b', eligible=False)
        retired['identity_error'] = '退群旧账号已清空'
        roster = snapshot([entry(), retired])
        preview = self.preview(sid, roster)
        self.assertTrue(preview['ready'])
        self.assertEqual(preview['identity_issues'], [])
        self.assertEqual({r['identity']['key'] for r in preview['selected_rows']}, {entry()['key']})
        resolved = rewards.resolve_preview(preview['id'], BatchResolution(expected_revision=1), LOGIN)
        self.assertTrue(rewards.get_preview(resolved['id'], LOGIN)['ready'])
        self.assertEqual(self.generate(resolved, roster)['total_diamonds'], 100)

    def test_all_retired_excluded_and_unknown_still_blocked(self):
        sid, _ = self.upload()
        preview = self.preview(sid, snapshot([entry(eligible=False), entry(source='group_b', eligible=False)]))
        self.assertEqual(len(preview['excluded_rows']), 2)
        self.assertEqual(preview['identity_issues'], [])
        self.assertEqual(preview['result']['total_diamonds'], 0)
        unknown = entry(source='group_b'); unknown.update(eligible=None, status='待核实', status_error='未知状态')
        blocked = self.preview(sid, snapshot([entry(), unknown]))
        self.assertFalse(blocked['ready'])
        self.assertEqual(len(blocked['identity_issues']), 2)

    def test_multiple_active_choices_persist_and_duplicate_reward_stays_blocked(self):
        sid, _ = self.upload()
        roster = snapshot([entry(), entry(source='group_b')])
        preview = self.preview(sid, roster)
        self.assertEqual(len(preview['identity_issues']), 2)
        choices = {row['row_id']: entry()['key'] for row in preview['identity_issues']}
        resolved = rewards.resolve_preview(preview['id'], BatchResolution(expected_revision=1, identity_choices=choices), LOGIN)
        self.assertTrue(resolved['ready'])
        self.assertEqual(rewards.get_preview(preview['id'], LOGIN)['identity_choices'], choices)
        self.generate(resolved, roster)
        # A fresh upload of the same file must not evade immutable source claims.
        duplicate, _ = self.upload()
        with self.assertRaises(HTTPException): self.preview(duplicate, roster)

    def test_differences_use_real_excel_values_and_location(self):
        sid, _ = self.upload(True, '已退群', '99999(1001)')
        changes = self.preview(sid)['roster_changes']
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[0]['before'], {'ori': '99999(1001)', 'status': '已退群'})
        self.assertEqual(changes[0]['fields'], ['account', 'status'])
        self.assertEqual((changes[0]['filename'], changes[0]['sheet'], changes[0]['row_number']), ('奖励测试.xlsx', '任务完成记录', 2))
        same, _ = self.upload(True, '群内成员')
        self.assertEqual(self.preview(same)['roster_changes'], [])

    def test_upload_time_is_wall_clock_and_legacy_time_is_not_invented(self):
        sid, _ = self.upload()
        created = store.read('imports', sid)['created_at']
        self.assertIsNotNone(datetime.fromisoformat(created).tzinfo)
        store.mutate('imports', sid, lambda r: r.update(created_at=123.4))
        self.assertEqual(rewards.choices(LOGIN)['imports'][0]['created_at'], 123.4)

    def test_delete_rejects_stale_preview_and_cannot_be_undone_by_cache(self):
        sid, _ = self.upload(); preview = self.preview(sid)
        cached = deepcopy(imports.get_reward_session(sid))
        original_blob = store.read_blob(sid)
        imports.delete_import(sid, LOGIN)
        self.assertEqual(rewards.choices(LOGIN)['imports'], [])
        with self.assertRaises(HTTPException): imports.get_reward_session(sid)
        with self.assertRaises(HTTPException): imports._persist_import(cached)
        with self.assertRaises(HTTPException) as caught: self.generate(preview)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(store.read_blob(sid), original_blob)

    def test_delete_after_generation_keeps_frozen_batch_and_claims(self):
        sid, _ = self.upload(); preview = self.preview(sid); batch = self.generate(preview)
        ledger = deepcopy(rewards._ledger())
        imports.delete_import(sid, LOGIN)
        self.assertEqual(rewards._ledger(), ledger)
        self.assertEqual(rewards.get_batch(batch['id'], LOGIN)['txt_preview'], batch['txt_preview'])

    def test_delete_http_owner_admin_and_other_permissions(self):
        from app.routers import reward_batches as routes
        sid, _ = self.upload()
        app = FastAPI(); app.include_router(routes.router)
        async def request(login, method='DELETE'):
            with patch.object(routes, '_current_login', AsyncMock(return_value=login)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated.test') as client:
                    return await client.request(method, '/api/reward-batches/imports/' + sid)
        admin = {'email': 'admin@example.test', 'name': '管理员'}
        other = {'email': 'other@example.test', 'name': '其他成员'}
        with ExitStack() as stack:
            for module in (config, security, auth): stack.enter_context(patch.object(module, 'FEISHU_LOGIN_REQUIRED', True))
            stack.enter_context(patch.object(security, 'FEISHU_ADMIN_EMAILS', {admin['email']}))
            stack.enter_context(patch.object(security, 'FEISHU_ALLOWED_EMAILS', set()))
            stack.enter_context(patch.object(auth, '_load_whitelist', return_value=[{'email': p['email'], 'enabled': True, 'perms_v': 4, 'perms': {'reward': 'use'}} for p in (LOGIN, other)]))
            self.assertEqual(asyncio.run(request(other)).status_code, 404)
            self.assertFalse(store.read('imports', sid).get('deleted_at'))
            self.assertEqual(rewards.choices(other)['imports'], [])
            self.assertTrue(rewards.choices(admin)['imports'][0]['can_delete'])
            self.assertTrue(rewards.choices(admin)['imports'][0]['selectable'])
            self.assertEqual(asyncio.run(request(admin)).status_code, 200)
            sid, _ = self.upload()
            with patch.object(auth, '_load_whitelist', return_value=[{'email': LOGIN['email'], 'enabled': True, 'perms_v': 4, 'perms': {'reward': 'read'}}]):
                self.assertEqual(asyncio.run(request(LOGIN)).status_code, 403)
                self.assertFalse(rewards.choices(LOGIN)['imports'][0]['can_delete'])
            self.assertEqual(asyncio.run(request(LOGIN)).status_code, 200)

    def test_in_group_synonyms_keep_custom_status_mapping_authoritative(self):
        source = {'eligible_statuses': ['群内人员'], 'ineligible_statuses': ['不在群内']}
        self.assertTrue(roster_sources.status_eligibility('群内成员', source))
        self.assertFalse(roster_sources.status_eligibility('不在群内', source))
        self.assertIsNone(roster_sources.status_eligibility('待核实', source))
        source['ineligible_statuses'].append('群内成员')
        self.assertFalse(roster_sources.status_eligibility('群内成员', source))
