import asyncio
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from test_group_tasks import WorkflowCase, LOGIN, entry
from app.core import config
from app.integrations import feishu_sheets as sheets
from app.schemas.roster_sources import RosterSourceInput
from app.services import roster_sources as sources
from app.storage import group_workflow as store


class FeishuSheetsTests(WorkflowCase):
    def test_urls_are_parsed_not_fetched_and_private_hosts_rejected(self):
        self.assertEqual(sheets.parse_reference("https://tenant.feishu.cn/wiki/abcdefghij123456?sheet=sheet_a"),("wiki","abcdefghij123456"))
        for value in ("http://127.0.0.1/sheets/abcdefghij123456","https://feishu.cn.evil.test/sheets/abcdefghij123456","https://user@tenant.feishu.cn/sheets/abcdefghij123456"):
            with self.assertRaises(sheets.SheetsError):sheets.parse_reference(value)

    def test_full_ranges_include_filtered_and_hidden_rows(self):
        calls=[]
        metadata=[{"sheet_id":"sheet_a","title":"名单","hidden":True,"grid_properties":{"row_count":5,"column_count":3}}]
        async def get(path,token,params=None):
            calls.append((path,params))
            if path.endswith("/sheets/query"):return {"sheets":metadata}
            return {"revision":7,"valueRange":{"values":[]}}
        with patch.object(sheets,"_get",get),patch.object(config,"FEISHU_SHEETS_RANGE_ROWS",2):
            result=asyncio.run(sheets.read_sheet("abcdefghij123456","sheet_a","fake-token",max_column=2))
        ranges=[path for path,_ in calls if "/values/" in path]
        self.assertEqual(len(ranges),4)  # Three data segments plus a final revision probe.
        self.assertTrue(any("A5%3AC5" in path for path in ranges))
        self.assertEqual(len(result["values"]),5)
        self.assertFalse(any("filter" in path for path,_ in calls))

    def test_mid_read_layout_change_fails_closed(self):
        a={"sheet_id":"sheet_a","title":"名单","grid_properties":{"row_count":3,"column_count":3}}
        b={**a,"title":"已改名"}
        with patch.object(sheets,"list_sheets",AsyncMock(side_effect=[[a],[b]])),patch.object(sheets,"read_range",AsyncMock(return_value=[[],[],[]])):
            with self.assertRaises(sheets.SheetsError):asyncio.run(sheets.read_sheet("abcdefghij123456","sheet_a","fake",max_column=2))

    def test_source_mapping_header_drift_and_string_identity(self):
        source={**RosterSourceInput(name="测试群",spreadsheet_url="abcdefghij123456",sheet_id="sheet_a",player_col=2,ori_col=0,status_col=1).model_dump(),"id":"group_a","revision":1,"expected_headers":{"0":"ori","1":"状态","2":"playerid"}}
        payload={"spreadsheet":"abcdefghij123456","sheet":{"title":"名单"},"values":[["ori","状态","playerid"],["818043063(6389)","群内人员","1234567890123456789"]]}
        with patch.object(sheets,"read_sheet",AsyncMock(return_value=payload)):
            result=asyncio.run(sources._read_source(source,{**LOGIN,"token":"fake"}))
        self.assertEqual(result["entries"][0]["player_id"],"1234567890123456789")
        payload["values"][0][1]="新列"
        with patch.object(sheets,"read_sheet",AsyncMock(return_value=payload)):
            with self.assertRaises(sheets.SheetsError):asyncio.run(sources._read_source(source,{**LOGIN,"token":"fake"}))

    def test_one_failed_source_prevents_partial_snapshot(self):
        for identifier in ("group_a","group_b"):
            store.create("sources",identifier,{"id":identifier,"name":identifier,"sheet_id":"sheet_a","revision":1,"enabled":True})
        result={"entries":[entry()],"spreadsheet":"book","sheet_title":"名单","read_at":"now"}
        with patch.object(sources,"_read_source",AsyncMock(side_effect=[result,sheets.SheetsError("权限不足")])):
            with self.assertRaises(HTTPException) as caught:asyncio.run(sources.read_roster_snapshot(LOGIN))
        self.assertIn("group_b",caught.exception.detail)
        self.assertEqual(store.list_records("snapshots"),[])

    def test_same_layout_but_changed_values_rejects_mixed_revision(self):
        metadata=[{'sheet_id':'sheet_a','title':'名单','grid_properties':{'row_count':3,'column_count':3}}]
        with patch.object(sheets,'list_sheets',AsyncMock(return_value=metadata)),patch.object(sheets,'read_range',AsyncMock(side_effect=[sheets.RangeValues([[],[]],1),sheets.RangeValues([[]],2)])),patch.object(config,'FEISHU_SHEETS_RANGE_ROWS',2):
            with self.assertRaises(sheets.SheetsError):asyncio.run(sheets.read_sheet('abcdefghij123456','sheet_a','fake',max_column=2))

    def test_numeric_long_id_is_flagged_and_text_keeps_leading_zero(self):
        source={**RosterSourceInput(name='测试群',spreadsheet_url='abcdefghij123456',sheet_id='sheet_a',player_col=0,ori_col=1,status_col=2).model_dump(),'id':'group_a','revision':1}
        payload={'spreadsheet':'abcdefghij123456','sheet':{'title':'名单'},'values':[['playerid','ori','状态'],[1234567890123456789,'818043063(6389)','群内人员'],['001234567890123456789','818043064(6389)','群内人员']]}
        with patch.object(sheets,'read_sheet',AsyncMock(return_value=payload)):
            result=asyncio.run(sources._read_source(source,{**LOGIN,'token':'fake'}))
        self.assertIn('精度',result['entries'][0]['identity_error'])
        self.assertEqual(result['entries'][1]['player_id'],'001234567890123456789')
        self.assertEqual(result['entries'][1]['identity_error'],'')

    def test_inaccessible_existing_source_can_be_disabled_but_not_reenabled(self):
        from app.schemas.roster_sources import RosterSourceUpdate
        payload=RosterSourceInput(name='测试群',spreadsheet_url='abcdefghij123456',sheet_id='sheet_a',player_col=0,ori_col=1,status_col=2).model_dump()
        previous={**payload,'id':'group_a','revision':1,'spreadsheet':'abcdefghij123456','sheet_title':'名单','last_read_at':'old','row_count':2}
        store.create('sources','group_a',previous)
        with patch.object(sources,'_read_source',AsyncMock(side_effect=sheets.SheetsError('无权限'))) as remote:
            result=asyncio.run(sources.save_source(RosterSourceUpdate(**{**payload,'enabled':False},expected_revision=1),LOGIN,'group_a'))
            remote.assert_not_awaited()
            self.assertEqual(result['last_read_status'],'已停用')
            with self.assertRaises(HTTPException):asyncio.run(sources.save_source(RosterSourceUpdate(**payload,expected_revision=2),LOGIN,'group_a'))
