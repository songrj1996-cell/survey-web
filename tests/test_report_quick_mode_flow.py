import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, FastAPI
from fastapi.testclient import TestClient
from app.routers import survey as survey_router
from app.services import survey_service, report_versions
from app.core.config import DEFAULT_QUICK_WRITER_REQUIREMENTS
from tests.test_survey_report_versions import _base_session, _isolated_report_runtime, _event_payloads, _writer


def _draft(ref='E1'):
    return json.dumps({'schema_version':1,'title':'快速报告','core':[{'text':'需验证的核心判断','evidence_ids':[ref]}],
        'findings':[{'title':'体验分歧','summary':'存在不同需求','reason':'场景不同','exceptions':'存在例外','implication':'分群验证','limitations':'尚未验证因果','evidence_ids':[ref]}],
        'risks':[], 'actions':[{'text':'补充验证','evidence_ids':[ref]}]},ensure_ascii=False)


class QuickReportFlowTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, *, count=1, replies=None, timeout=None):
        sess = _base_session()
        sess['pending_report_style']='quick'
        sess['plan']={'columns':[{'index':0,'role':'id','name':'玩家ID'},{'index':1,'role':'open_text','name':'体验'}], 'parts':[{'name':'体验','column_indexes':[1]}], 'cross_tabs':[]}
        sess['rows']=[['玩家ID','体验']]+[[f'p{i}',f'原文{i}'] for i in range(count)]
        sess['open_text']={1:[{'text':f'原文{i}','ids':{'uid':f'p{i}'},'profile':{}} for i in range(count)]}
        analyses=[]
        async def themes(*args, **kwargs):
            analyses.append(deepcopy(args[0]))
            yield ('diagnostics',{1:{'quality_status':'ok'}})
            yield ('result',{1:{'col_name':'体验','total':count,'count_unit':'players','themes':[{'id':'t1','name':'体验分歧','count':count,'percentage':100,'quote_evidence':[{'quote':'原文0','source':'uid=p0'}]}]}})
        async def synthesis(*args, **kwargs):
            yield ('result',[])
        writer = AsyncMock(side_effect=replies or [(_draft('E2'),'model')])
        if timeout:
            async def delayed(*args,**kwargs):
                await asyncio.sleep(10)
            writer.side_effect=delayed
        with _isolated_report_runtime(sess,writer) as runtime, ExitStack() as stack:
            stack.enter_context(patch.object(survey_service,'REPORT_QUICK_MODE_ENABLED',True))
            stack.enter_context(patch.object(survey_service,'_get_prompt_text',return_value=DEFAULT_QUICK_WRITER_REQUIREMENTS))
            stack.enter_context(patch.object(survey_service,'_batch_qualitative_analysis',new=themes))
            stack.enter_context(patch.object(survey_service,'build_report_viewpoint_stats',new=synthesis))
            if timeout: stack.enter_context(patch.object(survey_service,'LLM_QUICK_REPORT_STAGE_TIMEOUT_SECONDS',timeout))
            events=_event_payloads([event async for event in survey_service.report_stream('quick-flow',None)])
            saved=[deepcopy(call.args[1]) for call in runtime['save_session'].call_args_list]
        return sess,writer,events,analyses,saved

    async def test_standard_and_large_keep_full_analysis_and_use_one_writer(self):
        for count in (3,1201):
            with self.subTest(count=count):
                sess,writer,events,analyses,_=await self.run_case(count=count)
                self.assertFalse([e for e in events if e['type']=='error'],events)
                done=next(e for e in events if e['type']=='report_done')
                self.assertEqual(done['report_style'],'quick')
                self.assertEqual(writer.await_count,1)
                self.assertEqual(len(analyses[0][1]),count)
                snapshot=report_versions.resolve_report_version(sess)
                self.assertEqual(snapshot['quick_report_diagnostics']['logical_calls'],1)
                self.assertIn('来源标识与画像：uid=p0',snapshot['report_md'])
                self.assertIn('发现与证据附录',snapshot['qa_context_md'])

    async def test_invalid_output_repairs_once_and_saves_only_valid_result(self):
        sess,writer,events,_,_=await self.run_case(replies=[('broken','m'),(_draft('E2'),'m')])
        self.assertEqual(writer.await_count,2)
        self.assertEqual(sess['quick_report_diagnostics']['logical_calls'],2)
        self.assertTrue(any(e['type']=='report_done' for e in events))

    async def test_second_invalid_result_stops_without_success_snapshot(self):
        sess,writer,events,_,saved=await self.run_case(replies=[('broken','m'),(_draft('E999'),'m')])
        self.assertEqual(writer.await_count,2)
        self.assertFalse(any(e['type']=='report_done' for e in events))
        self.assertNotIn('report_versions',sess)
        self.assertEqual(saved[-1]['last_quick_report_failure']['logical_calls'],2)

    async def test_stage_timeout_cancels_writer_and_does_not_commit(self):
        sess,writer,events,_,saved=await self.run_case(timeout=.03)
        self.assertEqual(writer.await_count,1)
        self.assertNotIn('report_versions',sess)
        self.assertFalse(any(e['type']=='report_done' for e in events))
        self.assertEqual(saved[-1]['last_quick_report_failure']['stop_reason'],'timeout')

    async def test_flag_disabled_and_full_mode_compatibility(self):
        sess=_base_session()
        with patch.object(survey_service,'REPORT_QUICK_MODE_ENABLED',False):
            with self.assertRaises(HTTPException): survey_service._validate_report_style(sess,'quick')
            self.assertEqual(survey_service._validate_report_style(sess,'full'),'full')
        with _isolated_report_runtime(sess,_writer('完整报告')) as runtime:
            events=_event_payloads([event async for event in survey_service.report_stream('full-flow',None)])
        self.assertTrue(any(e['type']=='report_done' for e in events))
        self.assertEqual(sess['report_style'],'full')
        self.assertNotIn('quick_report_diagnostics',sess)


class QuickReportEndpointTests(unittest.TestCase):
    def test_options_are_owner_guarded_and_confirmation_passes_mode(self):
        app=FastAPI()
        app.include_router(survey_router.router)
        with TestClient(app) as client, patch.object(survey_router,'require_session_request_access',new=AsyncMock(return_value={'email':'owner@example.com'})) as access, patch.object(survey_router,'report_style_options',return_value={'quick_enabled':True,'report_style':'full'}), patch.object(survey_router,'validate_plan_confirm_ready'), patch.object(survey_router,'confirm_survey_plan',return_value={'approved':True}) as confirm, patch.object(survey_router,'audit_log',new=AsyncMock()):
            self.assertTrue(client.get('/api/report/s/options').json()['quick_enabled'])
            self.assertEqual(client.post('/api/plan/confirm',json={'session_id':'s','user_text':'ok','report_style':'quick'}).status_code,200)
            confirm.assert_called_once_with('s',{'email':'owner@example.com'},report_style='quick')
            self.assertEqual(access.await_count,2)
            self.assertEqual(client.post('/api/plan/confirm',json={'session_id':'s','user_text':'ok','report_style':'invalid'}).status_code,422)


if __name__=='__main__': unittest.main()
