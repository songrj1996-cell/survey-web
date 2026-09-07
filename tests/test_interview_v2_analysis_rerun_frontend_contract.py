import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "static/js/features/interview-v2.js"
JS = JS_PATH.read_text(encoding="utf-8")


class AnalysisModuleRerunFrontendTests(unittest.TestCase):
    def run_js(self, assertions):
        bootstrap = r"""
          const state = ivV2State;
          const moduleA = 'module_' + 'a'.repeat(32), moduleB = 'module_' + 'b'.repeat(32);
          const base = 'analysis_' + 'c'.repeat(32);
          const events = [], requests = [];
          let response = { ok: true, status: 200, json: async () => ({ rerun: { reused: false } }) };
          state.projectId = 'project_' + '1'.repeat(32);
          state.importId = 'import_' + '2'.repeat(32);
          state.analysisResponse = { analysis_run_id: base, status: 'completed', model_usage: { modules: [{module_id: moduleA}, {module_id: moduleB}] } };
          state.importData = { analysis_summary: { analysis_run_id: base, report_ready: true } };
          state.reportResponse = { report_version_id: 'report-old', sections: [{ locked: true, content: 'manual' }] };
          ivV2RenderConfirmed = () => { events.push('render'); };
          ivV2LoadReportWorkspace = async () => { events.push('refresh'); return true; };
          ivV2SetStatusError = () => { events.push('error'); };
          ivV2Modules = () => [{module_id: moduleA, canonical_name: '<img src=x onerror=1>'}];
          fetch = async (url, options) => { requests.push({url, ...options}); if (response instanceof Error) throw response; return response; };
        """
        script = """
          const fs = require('fs'), vm = require('vm'), assert = require('assert');
          const context = { assert, console, URLSearchParams,
            document: { getElementById() {return null;}, querySelectorAll() {return [];} },
            window: { confirm() {return true;}, crypto: {randomUUID() {return 'mock-key-' + Math.random();}} },
            showToast() {},
            esc(value) { return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); },
          };
          vm.createContext(context);
          const source = fs.readFileSync(process.argv[1], 'utf8').replace(/\\nivV2Mount\\(\\);\\s*$/, '');
          vm.runInContext(source, context);
        """
        script += "vm.runInContext(" + json.dumps(bootstrap + "\n(async () => {\n" + assertions + "\n})()") + ", context).catch(e => { console.error(e); process.exitCode = 1; });"
        result = subprocess.run(["node", "-e", script, str(JS_PATH)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_markup_dispatch_and_cache_version(self):
        self.assertIn('data-iv-v2-action="analysis-select-module"', JS)
        self.assertIn("ivV2RerunAnalysisModule(ivV2State.selectedAnalysisModuleId)", JS)
        self.assertIn('/static/js/features/interview-v2.js?v=6', (ROOT / "static/index.html").read_text(encoding="utf-8"))
        self.run_js("""
          const html = ivV2AnalysisModuleRerunHtml();
          assert(html.includes('重跑所选模块'));
          assert(!html.includes('<img src=x'));
          assert(html.includes('&lt;img'));
          assert(html.includes('role="status"'));
        """)

    def test_busy_dirty_stale_and_mismatched_versions_block_action(self):
        self.run_js("""
          assert(ivV2CanRerunAnalysisModule(moduleA));
          for (const field of ['reportBusy', 'dossierBusy', 'reportDirty', 'boundaryDirty', 'draftDirty', 'reportExportBusy']) {
            state[field] = true;
            assert(!ivV2CanRerunAnalysisModule(moduleA), field);
            await ivV2RerunAnalysisModule(moduleA);
            state[field] = false;
          }
          state.reportApprovalNote = 'unsaved'; assert(!ivV2CanRerunAnalysisModule(moduleA)); state.reportApprovalNote = '';
          state.analysisResponse.status = 'stale'; assert(!ivV2CanRerunAnalysisModule(moduleA)); state.analysisResponse.status = 'completed';
          state.importData.analysis_summary.analysis_run_id = 'moved'; assert(!ivV2CanRerunAnalysisModule(moduleA));
          assert.equal(requests.length, 0);
        """)

    def test_exact_payload_and_keys_are_scoped_to_project_base_and_module(self):
        self.run_js("""
          const payload = ivV2AnalysisModuleRerunPayload(moduleA);
          assert.deepEqual(payload, {from_stage: 'analysis_module', base_analysis_run_id: base, module_id: moduleA, preserve_manual_report_edits: true, reuse_unchanged_artifacts: true, force: false});
          const key = ivV2AnalysisModuleRerunKey(payload);
          assert.equal(key, ivV2AnalysisModuleRerunKey(payload));
          assert.notEqual(key, ivV2AnalysisModuleRerunKey(ivV2AnalysisModuleRerunPayload(moduleB)));
          state.projectId = 'another-project';
          assert.notEqual(key, ivV2AnalysisModuleRerunKey(payload));
        """)

    def test_success_reloads_head_and_does_not_rewrite_report(self):
        self.run_js("""
          const report = JSON.stringify(state.reportResponse);
          await ivV2RerunAnalysisModule(moduleA);
          assert.equal(requests.length, 1);
          assert(requests[0].url.endsWith('/reruns'));
          assert(requests[0].headers['Idempotency-Key']);
          assert.equal(JSON.parse(requests[0].body).module_id, moduleA);
          assert(events.includes('refresh'));
          assert.equal(JSON.stringify(state.reportResponse), report);
          assert.equal(state.reportBusy, false);
          assert.equal(state.analysisRerunActiveModuleId, '');
        """)

    def test_conflict_refreshes_then_displays_error_and_preserves_content(self):
        self.run_js("""
          const before = JSON.stringify(state.reportResponse);
          response = {ok: false, status: 409, json: async () => ({error: {code: 'ANALYSIS_INPUT_CHANGED'}})};
          await ivV2RerunAnalysisModule(moduleA);
          assert(events.indexOf('refresh') < events.indexOf('error'));
          assert.equal(JSON.stringify(state.reportResponse), before);
          assert.equal(state.reportBusy, false);
        """)

    def test_network_retry_reuses_key_and_cancelled_confirmation_sends_nothing(self):
        self.run_js("""
          window.confirm = () => false;
          await ivV2RerunAnalysisModule(moduleA);
          assert.equal(requests.length, 0);
          window.confirm = () => true;
          response = new Error('network unavailable');
          await ivV2RerunAnalysisModule(moduleA);
          assert(state.errorMessage.includes('network unavailable'));
          const key = requests[0].headers['Idempotency-Key'];
          response = {ok: true, status: 200, json: async () => ({rerun: {reused: true}})};
          await ivV2RerunAnalysisModule(moduleA);
          assert.equal(requests[1].headers['Idempotency-Key'], key);
          assert.equal(state.reportBusy, false);
        """)

    def test_reset_clears_module_state_and_ignores_late_response(self):
        self.run_js("""
          ivV2ResetStructureWorkspace = () => {};
          ivV2ClearFile = () => {};
          ivV2ClearDirty = () => {};
          ivV2SyncUploadButton = () => {};
          ivV2RenderEditor = () => {};
          ivV2SetStep = () => {};
          let finish;
          response = {ok: true, status: 200, json: () => new Promise(resolve => {finish = resolve;})};
          const running = ivV2RerunAnalysisModule(moduleA);
          await Promise.resolve();
          assert.equal(state.analysisRerunActiveModuleId, moduleA);
          ivV2Reset();
          finish({rerun: {reused: false}});
          await running;
          assert.equal(state.analysisRerunActiveModuleId, '');
          assert.equal(state.selectedAnalysisModuleId, '');
          assert.equal(Object.keys(state.analysisRerunKeys).length, 0);
          assert.equal(state.analysisResponse, null);
          assert(!events.includes('refresh'));
        """)
