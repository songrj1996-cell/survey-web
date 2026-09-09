"""V2 entry fails closed until the server explicitly enables it."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
import httpx

from app.routers import interview


ROOT = Path(__file__).resolve().parents[1]


class InterviewCapabilitiesTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_both_flag_states_without_enabling_v2_routes(self):
        app = FastAPI()
        app.include_router(interview.router)
        for enabled in (False, True):
            with self.subTest(enabled=enabled), patch.object(
                interview, "INTERVIEW_V2_ENABLED", enabled
            ), patch.object(interview, "_require_feature", new=AsyncMock()) as auth:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.get("/api/interview/capabilities")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"interview_v2_enabled": enabled})
                    auth.assert_awaited_once()
                    self.assertEqual(auth.call_args.args[1], "interview")
                    self.assertEqual((await client.post("/api/v1/interview-imports")).status_code, 404)

    async def test_forbidden_user_cannot_read_capabilities(self):
        app = FastAPI()
        app.include_router(interview.router)
        with patch.object(interview, "_require_feature", new=AsyncMock(
            side_effect=HTTPException(status_code=403, detail="forbidden")
        )):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/api/interview/capabilities")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("interview_v2_enabled", response.json())


class InterviewEntryTests(unittest.TestCase):
    def test_all_v2_buttons_fail_closed_before_javascript(self):
        buttons = []

        class Parser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "button" and attrs.get("data-iv-track") == "v2":
                    buttons.append(attrs)

        Parser().feed((ROOT / "static/index.html").read_text(encoding="utf-8"))
        self.assertEqual(len(buttons), 3)
        for attrs in buttons:
            self.assertIn("hidden", attrs)
            self.assertIn("disabled", attrs)

    def run_js(self, scenario):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
function button(track) {
  return {dataset: {ivTrack: track}, hidden: track === 'v2', disabled: false,
    classList: {toggle() {}}, setAttribute() {}};
}
const buttons = [button('v1'), button('v2'), button('v1'), button('v2'), button('v1'), button('v2')];
const sections = [{dataset: {ivTrackContent: 'v1'}}, {dataset: {ivTrackContent: 'v2'}}];
let calls = [], v1Steps = [], v2Steps = [];
const context = {console, assert, AbortSignal,
  document: {querySelectorAll(selector) {
    if (selector === '[data-iv-track]') return buttons;
    if (selector === '[data-iv-track-content]') return sections;
    return [];
  }},
  window: {ivState: {track: 'v1', currentStep: 2}},
  showToast() {}, ivGoStep(step) {v1Steps.push(step);},
  fetch: async (url, options) => {calls.push({url, options}); return {ok: true, json: async () => ({interview_v2_enabled: true})};},
};
vm.createContext(context);
const source = fs.readFileSync(process.argv[1], 'utf8').replace(/\nivV2Mount\(\);\s*$/, '');
vm.runInContext(source, context);
context.recordStep = step => v2Steps.push(step);
vm.runInContext('ivV2SetStep = recordStep;', context);
const api = vm.runInContext('({ivV2SyncTrackToggle, ivV2SetTrack, ivV2LoadCapabilities, ivV2State})', context);
const v2 = buttons.filter(b => b.dataset.ivTrack === 'v2');
const v1 = buttons.filter(b => b.dataset.ivTrack === 'v1');
function closed() {
  api.ivV2SyncTrackToggle();
  assert(v2.every(b => b.hidden && b.disabled));
  assert(v1.every(b => !b.hidden && !b.disabled));
  api.ivV2SetTrack('v2');
  assert.strictEqual(context.window.ivState.track, 'v1');
  assert.strictEqual(v2Steps.length, 0);
  assert.strictEqual(sections[1].hidden, true);
}
"""
        script += "\n(async () => {\n" + scenario + "\n})().catch(e => {console.error(e); process.exitCode = 1;});"
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "static/js/features/interview-v2.js")],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_loading_and_disabled_keep_v1_usable(self):
        self.run_js("""
closed();
let finish;
context.fetch = () => new Promise(resolve => {finish = resolve;});
const pending = api.ivV2LoadCapabilities();
closed();
finish({ok: true, json: async () => ({interview_v2_enabled: false})});
await pending;
closed();
""")

    def test_enabled_switches_both_ways_and_preserves_busy_lock(self):
        self.run_js("""
await api.ivV2LoadCapabilities();
assert(v2.every(b => !b.hidden && !b.disabled));
assert.strictEqual(calls[0].url, '/api/interview/capabilities');
assert.strictEqual(calls[0].options.cache, 'no-store');
assert(calls[0].options.signal);
context.window.ivState.uploading = true;
api.ivV2SyncTrackToggle();
assert(v2.every(b => b.disabled));
api.ivV2SetTrack('v2');
assert.strictEqual(context.window.ivState.track, 'v1');
context.window.ivState.uploading = false;
api.ivV2SyncTrackToggle();
api.ivV2SetTrack('v2');
assert.strictEqual(context.window.ivState.track, 'v2');
assert.strictEqual(sections[1].hidden, false);
assert.strictEqual(v2Steps.length, 1);
api.ivV2SetTrack('v1');
assert.strictEqual(context.window.ivState.track, 'v1');
assert.deepStrictEqual(v1Steps, [2]);
""")

    def test_failed_or_ambiguous_responses_never_enable_entry(self):
        self.run_js("""
for (const fetch of [
  async () => {throw new Error('network/timeout');},
  async () => ({ok: false, json: async () => ({interview_v2_enabled: true})}),
  async () => ({ok: true, json: async () => {throw new Error('invalid json');}}),
  ...[{}, null, {interview_v2_enabled: 'true'}, {interview_v2_enabled: 1}].map(
    body => async () => ({ok: true, json: async () => body})),
]) {
  context.fetch = fetch;
  await api.ivV2LoadCapabilities();
  closed();
}
""")


if __name__ == "__main__":
    unittest.main()
