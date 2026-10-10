import unittest
from datetime import date
from io import BytesIO
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from openpyxl import Workbook

from app.routers import reward_roster as reward_router
from app.schemas.reward_roster import (
    RewardAnalyzeRequest,
    RewardConflictResolution,
    RewardResolveRequest,
    RewardSheetMapping,
)
from app.services import reward_roster


HEADERS = [
    "奖励日期",
    "任务",
    "playerid",
    "MLBBaccountid",
    "当前状态",
    "普通奖励",
    "优秀额外奖励",
    "其他奖励",
]


def workbook_bytes(sheets: dict[str, list[list]]) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        worksheet = workbook.create_sheet(name)
        worksheet.append(HEADERS)
        for row in rows:
            worksheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def mapping() -> RewardSheetMapping:
    return RewardSheetMapping(
        date_col=0,
        task_col=1,
        player_id_col=2,
        account_id_col=3,
        status_col=4,
        normal_reward_col=5,
        excellent_reward_col=6,
        other_reward_col=7,
    )


class RewardRosterServiceTests(unittest.TestCase):
    def setUp(self):
        reward_roster._SESSIONS.clear()
        self.target = date(2026, 9, 24)

    def create(self, sheets):
        return reward_roster.create_reward_session(
            "奖励统计.xlsx",
            workbook_bytes(sheets),
            {"email": "researcher@example.com"},
        )

    def analyze(self, session_id, selected, reward_dates=None):
        return reward_roster.analyze_reward_session(
            session_id,
            RewardAnalyzeRequest(
                selected_sheets=selected,
                reward_dates=reward_dates or ["2026-09-24"],
                mappings={name: mapping() for name in selected},
            ),
        )

    def test_upload_lists_every_sheet_but_analysis_uses_only_selected_sheets(self):
        created = self.create({
            "UE群_任务完成": [[self.target, "打野调研", "discord_a", "10001(2001)", "群内人员", 100, 50, 0]],
            "玩家群_任务完成": [[self.target, "金币调研", "wa_b", "10002(2002)", "群内人员", 200, 0, 0]],
            "说明": [["不参与", "", "", "", "", "", "", ""]],
        })

        self.assertEqual([item["name"] for item in created["sheets"]], ["UE群_任务完成", "玩家群_任务完成", "说明"])
        self.assertEqual(created["available_dates"], [{
            "date": "2026-09-24",
            "row_count": 2,
            "sheets": ["UE群_任务完成", "玩家群_任务完成"],
        }])
        result = self.analyze(created["session_id"], ["UE群_任务完成"])

        self.assertTrue(result["export_ready"])
        self.assertEqual(result["result"]["unique_accounts"], 1)
        self.assertEqual(result["result"]["total_diamonds"], 150)
        self.assertEqual(result["selected_sheets"], ["UE群_任务完成"])

    def test_cross_sheet_account_requires_review_and_can_merge_different_tasks(self):
        created = self.create({
            "UE群": [
                [self.target, "任务A", "discord_a", "10001(2001)", "群内人员", 100, 0, 0],
                [self.target, "任务B", "discord_a", "10001(2001)", "群内人员", 50, 0, 0],
            ],
            "玩家群": [
                [self.target, "任务C", "wa_a", "10001(2001)", "群内人员", 200, 0, 0],
                [self.target, "任务D", "wa_b", "10002(2002)", "群内人员", 300, 0, 0],
            ],
        })
        analyzed = self.analyze(created["session_id"], ["UE群", "玩家群"])

        self.assertFalse(analyzed["export_ready"])
        self.assertEqual(len(analyzed["conflicts"]), 1)
        conflict = analyzed["conflicts"][0]
        self.assertIn("cross_sheet_account", conflict["reasons"])
        with self.assertRaises(HTTPException) as caught:
            reward_roster.reward_download(created["session_id"])
        self.assertEqual(caught.exception.status_code, 409)

        resolved = reward_roster.resolve_reward_conflicts(
            created["session_id"],
            RewardResolveRequest(resolutions=[RewardConflictResolution(
                conflict_id=conflict["conflict_id"],
                selected_row_ids=[row["row_id"] for row in conflict["rows"]],
            )]),
        )
        self.assertEqual(resolved["result"]["unique_accounts"], 2)
        self.assertEqual(resolved["result"]["total_diamonds"], 650)
        merged = next(item for item in resolved["result"]["accounts"] if item["account"] == "10001(2001)")
        self.assertEqual(merged["total_diamonds"], 350)
        self.assertEqual(merged["source_count"], 3)

    def test_multiple_dates_do_not_treat_same_sheet_task_as_duplicate(self):
        second_date = date(2026, 9, 25)
        created = self.create({
            "任务完成": [
                [self.target, "任务A", "p1", "10001(2001)", "群内人员", 100, 0, 0],
                [second_date, "任务A", "p1", "10001(2001)", "群内人员", 200, 0, 0],
            ],
        })

        analyzed = self.analyze(
            created["session_id"],
            ["任务完成"],
            ["2026-09-24", "2026-09-25"],
        )

        self.assertTrue(analyzed["export_ready"])
        self.assertEqual(analyzed["reward_dates"], ["2026-09-24", "2026-09-25"])
        self.assertEqual(analyzed["result"]["accounts"][0]["total_diamonds"], 300)
        content, filename = reward_roster.reward_download(created["session_id"])
        self.assertEqual(content.decode("utf-8"), "2001\t10001\t301-300\r\n")
        self.assertEqual(filename, "2026-09-24等2个日期-奖励发放名单.txt")

    def test_task_statistics_merge_sheets_and_accounts_sort_by_reward_descending(self):
        created = self.create({
            "UE群": [[self.target, "共同任务", "p1", "10001(2001)", "群内人员", 100, 0, 0]],
            "玩家群": [
                [self.target, "共同任务", "p2", "10002(2002)", "群内人员", 300, 0, 0],
                [self.target, "其他任务", "p3", "10003(2003)", "群内人员", 200, 0, 0],
            ],
        })

        analyzed = self.analyze(created["session_id"], ["UE群", "玩家群"])

        self.assertTrue(analyzed["export_ready"])
        shared = next(item for item in analyzed["result"]["task_stats"] if item["task"] == "共同任务")
        self.assertEqual(shared["sheets"], ["UE群", "玩家群"])
        self.assertEqual(shared["account_count"], 2)
        self.assertEqual(shared["diamonds"], 400)
        self.assertEqual(
            [item["total_diamonds"] for item in analyzed["result"]["accounts"]],
            [300, 200, 100],
        )

    def test_same_task_across_sheets_cannot_keep_more_than_one_source(self):
        created = self.create({
            "UE群": [[self.target, "任务A", "discord_a", "10001(2001)", "群内人员", 100, 0, 0]],
            "玩家群": [[self.target, "任务A", "wa_a", "10001(2001)", "群内人员", 200, 0, 0]],
        })
        analyzed = self.analyze(created["session_id"], ["UE群", "玩家群"])
        conflict = analyzed["conflicts"][0]
        self.assertIn("cross_sheet_same_task", conflict["reasons"])

        with self.assertRaises(HTTPException) as caught:
            reward_roster.resolve_reward_conflicts(
                created["session_id"],
                RewardResolveRequest(resolutions=[RewardConflictResolution(
                    conflict_id=conflict["conflict_id"],
                    selected_row_ids=[row["row_id"] for row in conflict["rows"]],
                )]),
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("同一任务只能保留一条", caught.exception.detail)

    def test_invalid_reward_blocks_export_instead_of_guessing(self):
        created = self.create({
            "任务完成": [[self.target, "任务A", "p1", "10001(2001)", "群内人员", 10.5, 0, 0]],
        })
        analyzed = self.analyze(created["session_id"], ["任务完成"])

        self.assertFalse(analyzed["export_ready"])
        self.assertEqual(len(analyzed["invalid_rows"]), 1)
        self.assertIn("非负整数", analyzed["invalid_rows"][0]["issue"])

    def test_unrecognizable_date_on_eligible_row_blocks_export(self):
        created = self.create({
            "任务完成": [
                [self.target, "任务B", "p2", "10002(2002)", "群内人员", 50, 0, 0],
                ["待确认", "任务A", "p1", "10001(2001)", "群内人员", 100, 0, 0],
            ],
        })
        analyzed = self.analyze(created["session_id"], ["任务完成"])

        self.assertFalse(analyzed["export_ready"])
        self.assertEqual(analyzed["invalid_rows"][0]["issue"], "奖励日期无法识别")

    def test_txt_has_no_header_no_duplicate_account_and_exactly_three_columns(self):
        created = self.create({
            "任务完成": [
                [self.target, "任务A", "p1", "10001(2001)", "群内人员", 100, 50, 0],
                [self.target, "任务B", "p1", "10001(2001)", "群内人员", 20, 0, 0],
                [self.target, "任务C", "p2", "10002(1999)", "群内人员", 80, 0, 0],
                [self.target, "任务D", "p3", "10003(3000)", "已离群", 999, 0, 0],
            ],
        })
        analyzed = self.analyze(created["session_id"], ["任务完成"])
        content, filename = reward_roster.reward_download(created["session_id"])
        lines = content.decode("utf-8").splitlines()

        self.assertTrue(analyzed["export_ready"])
        self.assertEqual(filename, "2026-09-24-奖励发放名单.txt")
        self.assertEqual(lines, ["1999\t10002\t301-80", "2001\t10001\t301-170"])
        self.assertTrue(all(len(line.split("\t")) == 3 for line in lines))
        self.assertNotIn("zoneid", content.decode("utf-8").lower())


class RewardRosterRoutePermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_routes_require_reward_permission_before_loading_session(self):
        request = object()
        login = {"email": "researcher@example.com"}
        with (
            patch.object(reward_router, "_require_feature", new=AsyncMock(return_value=login)) as require,
            patch.object(reward_router, "require_session_request_access", new=AsyncMock()) as session_access,
        ):
            result = await reward_router._require_reward_session(request, "session-1")

        self.assertEqual(result, login)
        require.assert_awaited_once_with(request, "reward")
        session_access.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
