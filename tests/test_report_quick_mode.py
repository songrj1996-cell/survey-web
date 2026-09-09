from copy import deepcopy
import json
from pathlib import Path
import unittest

from app.services.report_quick_mode import (
    build_evidence_catalog, build_quick_query, parse_quick_draft,
    render_quick_report, supports_quick_report,
)
from app.services import history_service, report_versions, report_partial_rerun, survey_service
from unittest.mock import patch
from app.services import report_history, export_history
from app.storage import history as history_storage
from app.core import security
import tempfile

FIXTURE = Path(__file__).parent / 'fixtures/report_pipeline/quick_mode_cases.json'


class QuickReportContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.catalog = self.fixture['evidence_catalog']
        self.draft = self.fixture['sample_draft']

    def test_full_inventory_and_unexpanded_findings_preserved(self):
        before = deepcopy(self.catalog)
        parsed = parse_quick_draft(json.dumps(self.draft, ensure_ascii=False), self.catalog)
        md, metrics = render_quick_report(parsed, self.catalog)
        for entry in self.catalog:
            self.assertIn(f"### [{entry['id']}]", md)
            for quote in entry['player_quotes']:
                self.assertIn(quote['quote'], md)
                self.assertIn(quote['source'], md)
        self.assertIn('[E9] 大厅音乐可单独调节（补充发现与材料）', md)
        self.assertEqual(metrics['appendix_count'], len(self.catalog))
        self.assertEqual(self.catalog, before)

    def test_invalid_contracts_do_not_silently_pass(self):
        for mutate in (
            lambda d: d.update(schema_version=True),
            lambda d: d.update(findings=[]),
            lambda d: d['core'][0].update(evidence_ids=['E999']),
            lambda d: d['risks'][0].update(evidence_ids=[]),
            lambda d: d['actions'][0].update(evidence_ids=['E1', 'E1']),
            lambda d: d['findings'][0].update(reason=''),
            lambda d: d['core'][0].update(text='<img src=x onerror=alert(1)>'),
        ):
            draft = deepcopy(self.draft)
            mutate(draft)
            with self.assertRaises(ValueError):
                parse_quick_draft(json.dumps(draft), self.catalog)
        with self.assertRaises(ValueError):
            parse_quick_draft('{"schema_version":1', self.catalog)

    def test_degraded_scope_retains_whole_pool_and_identity_without_counts(self):
        entries = [{'text': f'原文 {i}', 'ids': {'uid': str(i)}, 'profile': {'人群': '新玩家'}} for i in range(1201)]
        clustered = {'part_1_col_1': {'themes': [{'id': 't1', 'name': '已识别', 'quotes': ['原文 0']}], 'filter_desc': '只看新玩家'}}
        scopes = [('part_1_col_1', 1, 1, {'name': '体验'}, entries)]
        catalog = build_evidence_catalog(clustered, [], scopes, {}, {'part_1_col_1': {'quality_status': 'degraded'}})
        fallback = next(e for e in catalog if e['kind'] == 'raw_fallback')
        self.assertEqual(len(fallback['player_quotes']), 1201)
        self.assertEqual(fallback['player_quotes'][-1]['source']['ids']['uid'], '1200')
        self.assertIn('没有可用的精确频次', catalog[0]['statistics'])
        self.assertIn('只看新玩家', catalog[0]['scope'])

    def test_all_themes_not_only_displayed_themes_enter_catalog(self):
        theme = {'id':'t1','name':'常见', 'count':10, 'percentage':10}
        rare = {'id':'t2','name':'低频风险', 'count':1, 'percentage':1, 'quote_evidence':[{'quote':'风险原文', 'source':'uid=P1；人群=老玩家'}]}
        catalog = build_evidence_catalog({1:{'themes':[theme], 'all_themes':[theme,rare], 'total':100,'count_unit':'players'}}, [], [(1,1,1,{'name':'体验'},[])], {}, {})
        self.assertEqual([e['title'] for e in catalog], ['常见', '低频风险'])
        self.assertEqual(catalog[1]['player_quotes'][0]['source'], 'uid=P1；人群=老玩家')

    def test_source_text_cannot_inject_html_or_headings(self):
        self.catalog[-1]['title'] = '<script>alert(1)</script>\n## forged'
        self.catalog[-1]['player_quotes'][0]['quote'] = '<img src=x onerror=alert(1)>\n### [E1] spoof'
        md, _ = render_quick_report(self.draft, self.catalog)
        self.assertNotIn('<script>', md)
        self.assertNotIn('<img', md)
        self.assertNotIn('\n### [E1] spoof', md)

    def test_modes_and_context_contract(self):
        for source in ({'analysis_mode':'quantitative'}, *({'mode':m} for m in ('interview','comment','annotate','crosstab'))):
            self.assertFalse(supports_quick_report(source))
        self.assertTrue(supports_quick_report({}))
        query = build_quick_query(self.catalog, context={'problem':'保持正向体验'}, focus={'core_question':'关键分歧'}, instruction='核实风险')
        for text in ('保持正向体验', '关键分歧', '核实风险', 'E9', 'raw_fallback'):
            self.assertIn(text, query)


class QuickReportVersionTests(unittest.TestCase):
    def test_history_persists_mode_and_selected_export_uses_same_version(self):
        with tempfile.TemporaryDirectory(prefix='quick-version-test-') as temp, patch.object(history_storage,'HISTORY_FILE',str(Path(temp)/'history.json')), patch.object(security,'FEISHU_LOGIN_REQUIRED',False):
            source={'id':'quick-history-test','filename':'synthetic.xlsx','mode':'','rows':[['反馈'],['合成原文']],'plan':{'parts':[],'columns':[]}}
            report_versions.append_report_version(source,{'report_md':'# 完整旧版\nFULL_ONLY','qa_context_md':'FULL_QA'})
            report_versions.append_report_version(source,{'report_md':'# 快速新版\nQUICK_ONLY\n## 发现与证据附录\n完整证据','report_style':'quick','quick_report_diagnostics':{'status':'completed'},'qa_context_md':'QUICK_QA'})
            report_history.save_to_history('quick-history-test',source)
            full=export_history.get_history_export_entry('quick-history-test',None,1)
            quick=export_history.get_history_export_entry('quick-history-test',None,2)
            self.assertEqual(full['report_style'],'full')
            self.assertEqual(quick['report_style'],'quick')
            self.assertEqual(full['qa_context_md'],'FULL_QA')
            self.assertEqual(quick['qa_context_md'],'QUICK_QA')
            self.assertNotIn('QUICK_ONLY',full['report_md'])
            self.assertIn('完整证据',quick['report_md'])
            self.assertNotIn('quick_report_diagnostics',full)
            self.assertEqual(quick['quick_report_diagnostics']['status'],'completed')

    def test_mixed_versions_do_not_inherit_style_or_diagnostics(self):
        source = {'report_md':'# 旧版\n原文'}
        report_versions.append_report_version(source, {'report_md':'# 快速\n<!--QUICK_REPORT_V1-->','report_style':'quick','quick_report_diagnostics':{'status':'completed'}})
        self.assertEqual(report_versions.resolve_report_version(source,1)['report_style'],'full')
        report_versions.append_report_version(source, {'report_md':'# 完整'})
        self.assertEqual(source['report_style'],'full')
        self.assertNotIn('quick_report_diagnostics', source)
        with patch.object(survey_service,'get_session',return_value=source):
            self.assertEqual(survey_service.get_session_report_version('s',2)['report_style'],'quick')
            self.assertEqual(survey_service.get_session_report_version('s',1)['report_style'],'full')
        self.assertEqual(report_versions.resolve_report_version(source,2)['quick_report_diagnostics']['status'],'completed')

    def test_quick_partial_rerun_is_rejected_before_artifact_reuse(self):
        capability = report_partial_rerun.partial_rerun_capability({}, {'report_style':'quick'})
        self.assertFalse(capability['available'])
        self.assertIn('快速报告',capability['reason'])


if __name__ == '__main__':
    unittest.main()
