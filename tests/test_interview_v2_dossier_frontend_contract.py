from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "js" / "features" / "interview-v2.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


class DossierFrontendContractTests(unittest.TestCase):
    def test_fourth_stage_and_workbench_mount_points_exist(self):
        self.assertIn('data-iv-v2-step="4"', HTML)
        self.assertIn('id="iv-v2-dossier-workbench"', HTML)
        self.assertIn('id="iv-v2-dossier-participant-list"', HTML)
        self.assertIn('id="iv-v2-dossier-main"', HTML)
        self.assertIn('id="iv-v2-dossier-evidence-content"', HTML)

    def test_frontend_uses_batch_4a_endpoints(self):
        self.assertIn('/api/v1/interview-projects/${ivV2State.projectId}/participants', JS)
        self.assertIn('/dossiers/current?project_id=${encodeURIComponent(ivV2State.projectId)}', JS)
        self.assertIn('/dossiers:regenerate', JS)
        self.assertIn('/dossiers:review', JS)
        self.assertIn('/api/v1/interview-evidence/${evidenceId}/context', JS)

    def test_regenerate_and_review_send_version_heads(self):
        self.assertIn('base_dossier_version_id: ivV2State.dossierResponse?.dossier_version_id || null', JS)
        self.assertIn('base_dossier_version_id: ivV2State.dossierResponse.dossier_version_id', JS)
        self.assertIn("if (response.status === 409)", JS)

    def test_all_dossier_states_are_rendered(self):
        for status in ('not_generated', 'generated', 'approved', 'needs_changes', 'stale'):
            self.assertIn(f"{status}:", JS)
        self.assertIn('当前档案已过期', JS)
        self.assertIn('系统归纳，不代表玩家原话', JS)

    def test_evidence_ids_are_server_links_not_client_counts(self):
        self.assertIn('data-iv-v2-action="dossier-evidence"', JS)
        self.assertIn('人数和状态以服务端返回为准', JS)
        self.assertIn('ivV2State.importData?.dossier_summary', JS)

    def test_workbench_has_responsive_three_column_styles(self):
        self.assertIn('.iv-v2-dossier-layout', CSS)
        self.assertIn('grid-template-columns: minmax(210px, 0.8fr)', CSS)
        self.assertIn('@media (max-width: 760px)', CSS)


if __name__ == "__main__":
    unittest.main()
