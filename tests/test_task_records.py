import asyncio
from unittest.mock import AsyncMock, patch

from test_group_tasks import WorkflowCase, LOGIN
from app.services import group_tasks as tasks
from app.services.task_records import aggregate_players, clipboard_records, lookup_formula, _text


class TaskRecordsTests(WorkflowCase):
    def test_fixed_twelve_columns_numbers_and_current_row_formula(self):
        task=self.make_task();task=tasks.confirm_task(task["id"],task["version"],LOGIN)
        reference={"sheet_title":"名单'甲","player_col":2,"ori_col":4,"status_col":3,"target_spreadsheet":"testbook","group_name":"测试群甲"}
        with patch("app.services.roster_sources.validate_formula_reference",AsyncMock(return_value=reference)):
            result=asyncio.run(clipboard_records(task["id"],task["version"],LOGIN))
        cells=result["text"].split("\t")
        self.assertEqual(len(cells),12);self.assertEqual(cells[2],'="1234567890123456789"')
        self.assertIn("ROW()",cells[3]);self.assertIn("'名单''甲'",cells[3]);self.assertNotIn("1343",cells[3])
        self.assertEqual(cells[7],"4");self.assertEqual(cells[-3:],["300","200","0"])
        self.assertEqual(cells[4], '')
        self.assertIn('<td>=&quot;1234567890123456789&quot;</td>', result['html'])
        self.assertNotIn('mso-number-format', result['html'])

    def test_identifier_and_adv_literals_preserve_text_without_executable_payload(self):
        from openpyxl.formula import Tokenizer
        for value in ('1234567890123456789', '001234567890', '+6281234567890',
                      'account"quoted', "'original-prefix", '=1+1',
                      '=HYPERLINK("https://example.invalid","x")'):
            with self.subTest(value=value):
                encoded = _text(value, force_text=True)
                tokens = Tokenizer(encoded).items
                self.assertEqual(len(tokens), 1)
                self.assertEqual((tokens[0].type, tokens[0].subtype), ('OPERAND', 'TEXT'))
                self.assertEqual(tokens[0].value[1:-1].replace('""', '"'), value)
        self.assertEqual(_text(''), '')
        self.assertEqual(_text('任务\t名称\n备注'), '任务 名称 备注')

    def test_task_names_that_look_like_formulas_remain_literal_text(self):
        from openpyxl.formula import Tokenizer
        for value in ('=1+1', '+123', '-123', '@SUM(A1:A2)', '  =1+1', '= "a" & "b"'):
            with self.subTest(value=value):
                tokens = Tokenizer(_text(value)).items
                self.assertEqual(len(tokens), 1)
                self.assertEqual((tokens[0].type, tokens[0].subtype), ('OPERAND', 'TEXT'))
                self.assertEqual(tokens[0].value[1:-1].replace('""', '"'), value)

    def test_unfinished_player_amount_is_not_zero(self):
        task=self.make_task(labels=("有效反馈",None));p=aggregate_players(task)[0]
        self.assertFalse(p["complete"]);self.assertIsNone(p["base"]);self.assertIsNone(p["total"])

    def test_data_column_follows_repeat_rule(self):
        t=self.make_task(labels=("有效反馈",),repeat=False)
        self.assertEqual(aggregate_players(t)[0]["data_value"],"")
        t=self.make_task(labels=("有效反馈",),requires_count=True,repeat=False,counts=[7])
        self.assertEqual(aggregate_players(t)[0]["data_value"],7)

    def test_mixed_groups_copy_scope_and_independent_formula_columns(self):
        from copy import deepcopy
        from test_group_tasks import entry, snapshot
        from app.storage import group_workflow as store
        task=self.make_task(labels=('优秀反馈','有效反馈'))
        second=entry('008123456789','1234567(2345)','group_b');second['group_name']='测试群乙'
        def seed(current):
            current['feedback'][1]['corrections']['player_id']=second['player_id']
            current['roster_snapshot']=snapshot([entry(),second]);tasks._refresh_checks(current)
        store.mutate('tasks',task['id'],seed)
        view=tasks.confirm_task(task['id'],task['version'],LOGIN)
        refs={'group_a':{'sheet_title':'Discord 名单','player_col':2,'ori_col':5,'status_col':3,'target_spreadsheet':'testbook','group_name':'测试群甲'},'group_b':{'sheet_title':'WhatsApp 名单','player_col':1,'ori_col':3,'status_col':2,'target_spreadsheet':'testbook','group_name':'测试群乙'}}
        async def reference(source,login):return refs[source]
        with patch('app.services.roster_sources.validate_formula_reference',reference):
            all_rows=asyncio.run(clipboard_records(task['id'],view['version'],LOGIN))
            one_group=asyncio.run(clipboard_records(task['id'],view['version'],LOGIN,'group_b'))
        self.assertEqual(all_rows['count'],2);self.assertEqual(one_group['count'],1)
        cells=one_group['text'].split('\t')
        self.assertEqual(cells[2],'="008123456789"')
        self.assertIn("'WhatsApp 名单'!$D:$D",cells[3])
        self.assertIn("'WhatsApp 名单'!$B:$B",cells[3])
        self.assertIn("'WhatsApp 名单'!$C:$C",cells[5])
