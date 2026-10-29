import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

from fastapi import HTTPException

from test_group_tasks import WorkflowCase, LOGIN
from app.core import config, security
from app.core.feature_permissions import permission_levels
from app.services import auth, admin_users, feature_access
from app.storage import whitelist


def make_member_delete_ui_app():
    """Only synthetic member routes, with no app.main startup or external login."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from app.routers import admin as routes
    from app.services import audit
    admin = {'email': 'admin@example.test', 'name': '界面验证管理员'}
    for module in (auth, security, admin_users):
        module.FEISHU_ADMIN_EMAILS = {admin['email']}
        module.FEISHU_ALLOWED_EMAILS = set()
    auth._current_login = AsyncMock(return_value=admin)
    audit._current_login = auth._current_login
    whitelist._save_whitelist([
        {'email': 'member@example.test', 'name': '待删除成员', 'perms': ['annotate'], 'enabled': True,
         'perms_v': 4, 'revision': 1, 'permission_history': [{'at': '2026-10-29T00:00:00Z'}]},
        {'email': 'ou_synthetic', 'name': '保留成员', 'perms': ['survey'], 'enabled': False, 'perms_v': 4, 'revision': 1},
    ])
    root = Path(__file__).resolve().parents[1]
    app = FastAPI(); app.include_router(routes.router)
    app.mount('/static', StaticFiles(directory=root / 'static'), name='static')

    @app.get('/', response_class=HTMLResponse)
    def page():
        return '''<!doctype html><html lang="zh-CN" data-theme="light"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body style="overflow:auto;height:auto"><nav class="sidebar__nav" hidden></nav><div id="settings-drawer" style="padding:16px;max-width:1024px;margin:auto"><div id="settings-content"><div id="stab-content-perms"></div></div></div><p id="test-toast" role="status"></p><script>
        window.$=id=>document.getElementById(id);window.state={};
        window.esc=value=>{const node=document.createElement('span');node.textContent=String(value??'');return node.innerHTML.replace(/"/g,'&quot;');};
        window.showToast=(message,type)=>{const node=$('test-toast');node.textContent=message;node.dataset.type=type;};
        window.refreshFeishuStatus=async()=>{};
        </script><script src="/static/js/core/feature-permissions.js"></script><script src="/static/js/features/settings.js"></script><script>
        workflowRequest('/api/admin/users').then(data=>{PlatformPermissions.apply({feature_catalog:data.feature_catalog,is_admin:true});return loadPermsTab();});
        </script></body></html>'''

    return app


class PlatformHttpPermissionsTests(unittest.IsolatedAsyncioTestCase, WorkflowCase):
    async def test_delete_member_endpoint_guards_and_audit(self):
        import httpx
        from contextlib import ExitStack
        from fastapi import FastAPI
        from app.routers import admin as routes
        admin = {'email': 'admin@example.test', 'name': '管理员甲'}
        member = {'email': 'member@example.test', 'name': '合成成员', 'perms': ['annotate'],
                  'enabled': True, 'perms_v': 4, 'revision': 2}
        path = Path(self.tmp.name) / 'whitelist.json'
        app = FastAPI(); app.include_router(routes.router)
        with ExitStack() as stack:
            stack.enter_context(patch.object(whitelist, 'WHITELIST_FILE', path))
            for module in (security, admin_users):
                stack.enter_context(patch.object(module, 'FEISHU_ADMIN_EMAILS', {admin['email']}))
                stack.enter_context(patch.object(module, 'FEISHU_ALLOWED_EMAILS', set()))
            stack.enter_context(patch.object(auth, 'FEISHU_LOGIN_REQUIRED', True))
            login = stack.enter_context(patch.object(auth, '_current_login', AsyncMock(return_value=admin)))
            audit = stack.enter_context(patch.object(routes, 'audit_log', AsyncMock()))
            log = stack.enter_context(patch.object(routes, 'runtime_log'))
            whitelist._save_whitelist([member])
            before = path.read_bytes()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated.test') as client:
                url = '/api/admin/users/member@example.test'
                for suffix, status in (('', 422), ('?expected_revision=0', 422),
                                       ('?expected_revision=1', 409)):
                    response = await client.delete(url + suffix)
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(path.read_bytes(), before)
                for denied in (None, member):
                    login.return_value = denied
                    self.assertEqual((await client.delete(url + '?expected_revision=2')).status_code, 403)
                    self.assertEqual(path.read_bytes(), before)
                login.return_value = admin
                self.assertEqual((await client.delete('/api/admin/users/admin@example.test?expected_revision=1')).status_code, 403)
                self.assertEqual((await client.delete('/api/admin/users/missing@example.test?expected_revision=1')).status_code, 404)
                with patch.object(admin_users, 'mutate_whitelist', side_effect=OSError('synthetic storage failure')):
                    failed = await client.delete(url + '?expected_revision=2')
                self.assertEqual(failed.status_code, 503)
                self.assertNotIn('synthetic', failed.text)
                self.assertEqual(path.read_bytes(), before)
                audit.assert_not_awaited()
                self.assertEqual(log.call_args.kwargs['http_status'], 503)
                self.assertIn('刷新', failed.json()['detail'])
                response = await client.delete(url + '?expected_revision=2')
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(whitelist._load_whitelist(), [])
                self.assertEqual(auth._get_user_perms(member), [])
                self.assertEqual(log.call_args.kwargs['http_status'], 200)
                audit.assert_awaited_once()
                detail = audit.await_args.args[3]
                self.assertIn('合成成员', detail)
                self.assertNotIn(member['email'], detail)
                self.assertEqual((await client.delete(url + '?expected_revision=2')).status_code, 404)

    async def test_admin_reads_other_members_history_but_cannot_change_it(self):
        import httpx
        from contextlib import ExitStack
        from fastapi import FastAPI
        from app.routers import history as routes
        from app.services import history_service
        from app.storage import history as history_store
        admin={'email':'admin@example.test','name':'管理员'}
        record={'id':'historical-example','owner_key':'email:'+LOGIN['email'],'owner_email':LOGIN['email'],
                'owner_name':LOGIN['name'],'mode':'interview','filename':'example.xlsx','title':'他人的历史访谈报告',
                'report_md':'# 隔离验证\n已保存报告内容。','created_at':'2026-10-14T00:00:00Z'}
        app=FastAPI();app.include_router(routes.router)
        with ExitStack() as stack:
            for module in (config,auth,security):stack.enter_context(patch.object(module,'FEISHU_LOGIN_REQUIRED',True))
            stack.enter_context(patch.object(security,'FEISHU_ADMIN_EMAILS',{'admin@example.test'}))
            for module in (auth,routes):stack.enter_context(patch.object(module,'_current_login',AsyncMock(return_value=admin)))
            stack.enter_context(patch.object(history_store,'_load_history',return_value=[record]))
            stack.enter_context(patch.object(history_service,'_load_history_with_report_numbers',return_value=[record]))
            stack.enter_context(patch.object(routes,'audit_log',AsyncMock()))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://isolated.test') as client:
                response=await client.get('/api/history/historical-example')
                self.assertEqual(response.status_code,200,response.text)
                self.assertFalse(response.json()['can_edit'])
                self.assertEqual(response.json()['report_md'],record['report_md'])
                response=await client.patch('/api/history/historical-example/title',json={'title':'不得写入'})
                self.assertEqual(response.status_code,404)

    async def test_direct_endpoints_obey_grants_and_object_owner(self):
        import httpx
        from fastapi import FastAPI
        from app.routers import group_tasks, roster_sources, reward_batches, settings_api, survey, history, export, annotate
        task=self.make_task()
        app=FastAPI()
        for module in (group_tasks,roster_sources,reward_batches,settings_api,survey,history,export,annotate):app.include_router(module.router)
        member={"email":LOGIN["email"],"name":LOGIN["name"],"perms":{"annotate":"read"},"enabled":True,"perms_v":4}
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(config,"FEISHU_LOGIN_REQUIRED",True))
            stack.enter_context(patch.object(auth,"FEISHU_LOGIN_REQUIRED",True))
            stack.enter_context(patch.object(security,"FEISHU_LOGIN_REQUIRED",True))
            stack.enter_context(patch.object(auth,"_load_whitelist",return_value=[member]))
            for module in (auth,group_tasks,roster_sources,reward_batches):stack.enter_context(patch.object(module,"_current_login",AsyncMock(return_value=LOGIN)))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://isolated.test") as client:
                self.assertEqual((await client.get('/api/group-tasks/'+task['id'])).status_code,200)
                self.assertEqual((await client.post('/api/group-tasks',json={})).status_code,403)
                self.assertEqual((await client.post('/api/group-tasks/'+task['id']+'/run',json={"expected_version":task['version']})).status_code,403)
                for path in ('/api/roster-sources','/api/reward-batches/choices','/api/prompts','/api/glossary'):
                    self.assertEqual((await client.get(path)).status_code,403,path)
                self.assertEqual((await client.post('/api/upload')).status_code,403)
                member['perms']=['annotate']
                for module in (auth,group_tasks):stack.enter_context(patch.object(module,"_current_login",AsyncMock(return_value={"email":"other@example.test","name":"其他成员"})))
                member['email']='other@example.test'
                self.assertEqual((await client.get('/api/group-tasks/'+task['id'])).status_code,404)
                self.assertEqual((await client.get('/api/group-tasks/'+task['id']+'/clipboard',params={"version":task['version']})).status_code,404)

    async def test_business_roster_read_does_not_require_maintenance_grant(self):
        from app.services.roster_sources import _business_access
        with patch.object(config,"FEISHU_LOGIN_REQUIRED",True),patch.object(feature_access,"_get_user_permission_levels",return_value={"reward":"use"}):
            _business_access(LOGIN)
            with self.assertRaises(HTTPException):feature_access.require_permission(LOGIN,'roster',write=True)


class PlatformPermissionsTests(WorkflowCase):
    def test_delete_removes_only_the_member_and_preserves_business_files(self):
        path = Path(self.tmp.name) / 'whitelist.json'
        retained = {'email': 'other@example.test', 'name': '保留成员', 'perms': ['survey'],
                    'enabled': True, 'perms_v': 4, 'revision': 3, 'permission_history': [{'at': 'synthetic'}]}
        records = [Path(self.tmp.name) / name for name in ('history.json', 'audit_logs.json', 'task.json')]
        for record in records:
            record.write_text('{"synthetic":"keep-exactly"}', encoding='utf-8')
        original = [record.read_bytes() for record in records]
        with patch.object(whitelist, 'WHITELIST_FILE', path):
            for enabled in (True, False):
                whitelist._save_whitelist([{'email': 'member@example.test', 'name': '待删除成员',
                    'perms': ['annotate'], 'enabled': enabled, 'revision': 1, 'perms_v': 4,
                    'permission_history': [{'at': 'synthetic'}]}, retained])
                detail = admin_users.delete_whitelist_user(' MEMBER@EXAMPLE.TEST ', expected_revision=1)
                self.assertEqual(whitelist._load_whitelist(), [retained])
                self.assertIn('待删除成员', detail)
                self.assertEqual([record.read_bytes() for record in records], original)

    def test_delete_revision_admin_and_invalid_identifier_never_write(self):
        path = Path(self.tmp.name) / 'whitelist.json'
        with patch.object(whitelist, 'WHITELIST_FILE', path):
            whitelist._save_whitelist([{'email': 'ou_synthetic', 'name': '合成成员', 'perms': ['annotate'],
                                        'enabled': True, 'revision': 2, 'perms_v': 4}])
            before = path.read_bytes()
            for revision in (None, 1, True, '2'):
                with self.assertRaises(HTTPException) as error:
                    admin_users.delete_whitelist_user('ou_synthetic', expected_revision=revision)
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual(path.read_bytes(), before)
            for key in ('FEISHU_ADMIN_EMAILS', 'FEISHU_ALLOWED_EMAILS'):
                with patch.object(admin_users, key, {'ou_synthetic'}):
                    with self.assertRaises(HTTPException) as error:
                        admin_users.delete_whitelist_user('ou_synthetic', expected_revision=2)
                    self.assertEqual(error.exception.status_code, 403)
                    self.assertEqual(path.read_bytes(), before)
            with self.assertRaises(HTTPException) as error:
                admin_users.delete_whitelist_user('姓名不是账号', expected_revision=2)
            self.assertEqual(error.exception.status_code, 422)
            self.assertEqual(path.read_bytes(), before)
            admin_users.delete_whitelist_user('ou_synthetic', expected_revision=2)
            self.assertEqual(whitelist._load_whitelist(), [])

    def test_conservative_old_permissions_and_readonly(self):
        self.assertEqual(permission_levels({"perms":{"survey":"read","annotate":True,"roster":False,"members":True}}),{"survey":"read","annotate":"use"})
        self.assertEqual(permission_levels({"perms":["survey","annotate"],"read_only":True}),{"survey":"read","annotate":"read"})
        self.assertNotIn("roster",permission_levels({"perms":["survey","reward"]}))

    def test_save_cancel_contract_and_disable_keeps_grants(self):
        path=Path(self.tmp.name)/"whitelist.json"
        with patch.object(whitelist,"WHITELIST_FILE",path):
            admin_users.add_whitelist_user("member@example.test",["annotate"],True,"测试成员",LOGIN)
            initial=path.read_text()
            whitelist._load_whitelist()
            self.assertEqual(path.read_text(),initial)
            admin_users.update_whitelist_user("member@example.test",None,False,expected_revision=1,actor=LOGIN)
            saved=whitelist._load_whitelist()[0]
            self.assertFalse(saved["enabled"]);self.assertEqual(saved["perms"],["annotate"])
            self.assertEqual(len(saved["permission_history"]),2)
            with self.assertRaises(HTTPException):admin_users.update_whitelist_user("member@example.test",[],True,expected_revision=1)
            with self.assertRaises(HTTPException):admin_users.delete_whitelist_user("member@example.test")

    def test_empty_grants_and_admin_escalation_rejected(self):
        with self.assertRaises(HTTPException):admin_users.add_whitelist_user("new@example.test",[],True,"新成员")
        with self.assertRaises(HTTPException):admin_users.add_whitelist_user("new@example.test",["members"],True,"新成员")
        with patch.object(admin_users,"FEISHU_ADMIN_EMAILS",{"admin@example.test"}):
            with self.assertRaises(HTTPException):admin_users.update_whitelist_user("admin@example.test",["survey"],False,expected_revision=1)

    def test_admin_can_read_but_cannot_mutate_another_task(self):
        task=self.make_task()
        admin={"email":"admin@example.test","name":"管理员"}
        with patch.object(config,"FEISHU_LOGIN_REQUIRED",True),patch.object(security,"FEISHU_ADMIN_EMAILS",{"admin@example.test"}):
            self.assertEqual(feature_access.require_record(task,admin)["id"],task["id"])
            with self.assertRaises(HTTPException):feature_access.require_record(task,admin,write=True)
            with self.assertRaises(HTTPException):feature_access.require_record(task,{"email":"other@example.test"})

    def test_readonly_preserved_when_editing_checkboxes(self):
        path=Path(self.tmp.name)/"whitelist.json"
        with patch.object(whitelist,"WHITELIST_FILE",path):
            whitelist._save_whitelist([{"email":"reader@example.test","name":"只读成员","perms":{"annotate":"read"},"enabled":True,"revision":1}])
            admin_users.update_whitelist_user("reader@example.test",["annotate","reward"],True,expected_revision=1)
            saved=whitelist._load_whitelist()[0]
            self.assertEqual(permission_levels(saved),{"annotate":"read","reward":"use"})

    def test_missing_permission_cannot_write_or_read_feature(self):
        with patch.object(config,"FEISHU_LOGIN_REQUIRED",True),patch.object(feature_access,"_get_user_permission_levels",return_value={"annotate":"read"}):
            feature_access.require_permission(LOGIN,"annotate")
            with self.assertRaises(HTTPException):feature_access.require_permission(LOGIN,"annotate",write=True)
            with self.assertRaises(HTTPException):feature_access.require_permission(LOGIN,"roster")

    def test_saved_report_version_is_read_only_but_generation_get_is_not(self):
        from starlette.requests import Request
        def request(query=b''):
            return Request({'type':'http','method':'GET','path':'/api/report/example','query_string':query,'headers':[]})
        self.assertFalse(feature_access.legacy_request_is_read(request()))
        self.assertTrue(feature_access.legacy_request_is_read(request(b'version=1')))

    def test_readonly_annotation_download_does_not_publish_new_history(self):
        from app.services import annotate_workflow
        owner={**LOGIN,'owner_key':'email:'+LOGIN['email']}
        session={'owner_key':owner['owner_key']}
        with patch.object(config,'FEISHU_LOGIN_REQUIRED',True),patch.object(auth,'_current_login',AsyncMock(return_value=LOGIN)),patch.object(auth,'_get_user_permission_levels',return_value={'annotate':'read'}),patch.object(annotate_workflow,'get_annotate_session',return_value=session),patch.object(annotate_workflow,'_build_annotate_excel_from_session',return_value=(b'xlsx','file.xlsx')),patch.object(annotate_workflow,'_publish_annotate_result',AsyncMock()) as publish:
            result=asyncio.run(annotate_workflow.build_and_save_annotate_download('example',object()))
        self.assertEqual(result,(b'xlsx','file.xlsx'));publish.assert_not_awaited()
