import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "static/js/features/interview-v2.js"
JS = JS_PATH.read_text(encoding="utf-8")


class DossierRerunFrontendTests(unittest.TestCase):
    def run_js(self, assertions):
        bootstrap = r"""
          const state = ivV2State;
          const participant = 'participant_' + '2'.repeat(32);
          const base = 'dossier_' + '3'.repeat(32);
          const requests = [], events = [];
          let response = {ok: true, status: 200, json: async () => ({rerun: {reused: false}})};
          state.projectId = 'project_' + '1'.repeat(32);
          state.importId = 'import_' + '4'.repeat(32);
          state.selectedParticipantId = participant;
          state.dossierResponse = {participant_id: participant, dossier_version_id: base, status: 'approved'};
          ivV2RenderConfirmed = () => { events.push('render'); };
          ivV2LoadParticipants = async () => { events.push('refresh'); return true; };
          ivV2SetStatusError = () => { events.push('error'); };
          fetch = async (url, options) => { requests.push({url, ...options}); if (response instanceof Error) throw response; return response; };
        """
        script = r"""
          const fs = require('fs'), vm = require('vm'), assert = require('assert');
          const context = { assert, console, URLSearchParams,
            document: { getElementById() {return null;}, querySelectorAll() {return [];} },
            window: { confirm() {return true;}, crypto: {randomUUID() {return 'stable-dossier-key';}} },
            showToast() {},
            esc(value) { return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); },
          };
          vm.createContext(context);
          const source = fs.readFileSync(process.argv[1], 'utf8').replace(/\nivV2Mount\(\);\s*$/, '');
          vm.runInContext(source, context);
        """
        script += "vm.runInContext(" + json.dumps(bootstrap + "\n(async () => {\n" + assertions + "\n})()") + ", context).catch(e => { console.error(e); process.exitCode = 1; });"
        result = subprocess.run(
            ["node", "-e", script, str(JS_PATH)], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_payload_endpoint_key_and_cache_version(self):
        self.assertIn("from_stage: 'participant_dossier'", JS)
        self.assertIn('/api/v1/interview-projects/${ivV2State.projectId}/reruns', JS)
        self.assertIn("'Idempotency-Key': idempotencyKey", JS)
        self.assertIn('/static/js/features/interview-v2.js?v=7', (ROOT / "static/index.html").read_text(encoding="utf-8"))
        self.run_js("""
          const payload = ivV2DossierRerunPayload();
          assert.deepEqual(payload, {from_stage: 'participant_dossier', participant_id: participant, base_dossier_version_id: base, preserve_manual_report_edits: true, reuse_unchanged_artifacts: true, force: false});
          assert.equal(ivV2DossierRerunKey(payload), ivV2DossierRerunKey(payload));
          await ivV2RerunCurrentDossier();
          assert.equal(requests.length, 1);
          assert(requests[0].url.endsWith('/reruns'));
          assert.equal(requests[0].headers['Idempotency-Key'], 'stable-dossier-key');
          assert(events.includes('refresh'));
        """)

    def test_busy_dirty_and_stale_states_block_shared_rerun(self):
        self.run_js("""
          assert(ivV2CanRerunCurrentDossier());
          for (const field of ['dossierBusy', 'reportBusy', 'reportExportBusy', 'dossierReviewDirty', 'reportDirty', 'boundaryDirty', 'draftDirty']) {
            state[field] = true;
            assert(!ivV2CanRerunCurrentDossier(), field);
            await ivV2RerunCurrentDossier();
            state[field] = false;
          }
          state.reportApprovalNote = 'unsaved'; assert(!ivV2CanRerunCurrentDossier()); state.reportApprovalNote = '';
          state.dossierResponse.status = 'stale'; assert(!ivV2CanRerunCurrentDossier());
          assert.equal(requests.length, 0);
        """)

    def test_dirty_note_disables_rerun_without_full_render_and_survives_render(self):
        self.run_js("""
          state.dossierResponse.status = 'generated';
          const rerun = {dataset: {ivV2Action: 'dossier-generate'}, disabled: false};
          const shell = {
            setAttribute() {},
            querySelectorAll() { return [rerun]; },
          };
          document.getElementById = id => id === 'iv-v2-confirmed-shell' ? shell : null;
          const note = '\\n<保留 & 可见>';
          ivV2HandleEditorInputOrChange({target: {
            dataset: {ivV2Action: 'dossier-review-note'}, value: note,
          }});
          assert.equal(state.dossierReviewNote, note);
          assert.equal(state.dossierReviewDirty, true);
          assert.equal(rerun.disabled, true);
          const elements = {
            'iv-v2-confirmed-shell': shell,
            'iv-v2-dossier-workbench': {hidden: true},
            'iv-v2-review-workspace': {hidden: false},
            'iv-v2-dossier-status': {innerHTML: ''},
            'iv-v2-dossier-participant-list': {innerHTML: ''},
            'iv-v2-dossier-main': {innerHTML: ''},
            'iv-v2-dossier-review-note': {value: ''},
            'iv-v2-dossier-evidence-content': {innerHTML: ''},
          };
          document.getElementById = id => elements[id] || null;
          state.currentStep = 4;
          ivV2RenderDossierWorkbench();
          assert.equal(elements['iv-v2-dossier-review-note'].value, note);
          state.dossierReviewNote = '';
          state.dossierReviewDirty = false;
          ivV2SyncConfirmedControls();
          assert.equal(rerun.disabled, false);
        """)

    def test_cancel_network_retry_error_and_reset(self):
        self.run_js("""
          window.confirm = () => false;
          await ivV2RerunCurrentDossier();
          assert.equal(requests.length, 0);
          window.confirm = () => true;
          response = new Error('network unavailable');
          await ivV2RerunCurrentDossier();
          assert(state.errorMessage.includes('network unavailable'));
          const key = requests[0].headers['Idempotency-Key'];
          response = {ok: true, status: 200, json: async () => ({rerun: {reused: true}})};
          await ivV2RerunCurrentDossier();
          assert.equal(requests[1].headers['Idempotency-Key'], key);
          assert.equal(state.dossierBusy, false);
          ivV2ResetStructureWorkspace = () => {};
          ivV2ClearFile = () => {}; ivV2ClearDirty = () => {}; ivV2SyncUploadButton = () => {};
          ivV2RenderEditor = () => {}; ivV2SetStep = () => {};
          ivV2Reset();
          assert.equal(Object.keys(state.dossierRerunKeys).length, 0);
          assert.equal(state.dossierRerunActiveParticipantId, '');
          assert.equal(state.dossierReviewNote, '');
          assert.equal(state.dossierReviewDirty, false);
        """)


if __name__ == "__main__":
    unittest.main()
