"""Run approved checks with both cwd and all runtime data isolated before importing app."""
from pathlib import Path
import os
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
workspace = ROOT / ".verification" / "group-reward" / ("run-" + uuid.uuid4().hex[:12])
workspace.mkdir(parents=True, exist_ok=False)
os.chdir(workspace)
os.environ.update(DATA_DIR=str(workspace / "data"), FEISHU_LOGIN_REQUIRED="false", LLM_API_KEY="isolated-test-no-real-key",
                  FEISHU_APP_ID="", FEISHU_APP_SECRET="", FEISHU_REDIRECT_URI="", FEISHU_WIKI_AUTO_UPDATE_ENABLED="false",
                  FEISHU_NAVIGATION_NOTIFICATIONS_ENABLED="false", GROUP_RUN_TIMEOUT_SECONDS="60")
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
names = sys.argv[1:] or ["test_group_tasks", "test_task_records", "test_feishu_sheets", "test_reward_batches", "test_group_task_runner", "test_platform_permissions"]
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in names)
print(f"Isolated validation directory: {workspace}")
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
