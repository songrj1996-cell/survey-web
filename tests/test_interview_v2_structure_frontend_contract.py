import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "static" / "js" / "features" / "interview-v2.js"
CSS_PATH = ROOT / "static" / "style.css"


class InterviewV2StructureFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = JS_PATH.read_text(encoding="utf-8")
        cls.css = CSS_PATH.read_text(encoding="utf-8")

    def test_step3_uses_structure_review_endpoints_only(self):
        self.assertIn("/api/v1/interview-imports/${ivV2State.importId}/structure:build", self.js)
        self.assertIn("/api/v1/interview-imports/${ivV2State.importId}/structure", self.js)
        self.assertIn("/api/v1/interview-imports/${ivV2State.importId}/review-issues", self.js)
        self.assertIn("/api/v1/interview-evidence/${evidenceId}/context", self.js)
        self.assertIn("/api/v1/interview-review-issues/${issueId}", self.js)
        self.assertNotIn("review-issues:resolve-batch", self.js)
        self.assertNotIn("/api/interview/run", self.js)
        self.assertNotIn("/api/v1/interview-imports/${ivV2State.importId}/review-issues:resolve-batch", self.js)

    def test_single_issue_patch_carries_dual_revision_heads(self):
        self.assertIn("base_structure_revision_id: ivV2CurrentStructureRevisionId()", self.js)
        self.assertIn("base_evidence_revision_id: ivV2CurrentEvidenceRevisionId()", self.js)
        self.assertIn("method: 'PATCH'", self.js)
        self.assertIn("ivV2ValidateIssueDraft", self.js)
        self.assertIn("allowed_resolutions", self.js)
        self.assertIn("ivV2IssueHasResolvableAction(issue) && ivV2IssueIsOpen(issue)", self.js)
        self.assertIn("需要修正源文件并重新上传，或先调整映射后重新构建结构复核", self.js)
        self.assertIn("window.confirm('确认排除此证据吗？排除后它不会进入报告；如果这是该玩家最后一条证据，后端也可能拒绝此次排除。')", self.js)
        self.assertIn("delete ivV2State.issueDrafts[issueId];", self.js)
        self.assertIn("ivV2InvalidateEvidenceContext();", self.js)
        self.assertIn("String(data.evidence_revision_id) !== previousEvidenceRevisionId", self.js)

    def test_structure_review_handles_checkpoint_and_conflict_states(self):
        for needle in (
            "STRUCTURE_REVIEW_REQUIRED",
            "READY_FOR_DOSSIERS",
            "STRUCTURE_INPUT_CONFLICT",
            "STRUCTURE_INPUT_NOT_READY",
            "STRUCTURE_NOT_BUILT",
            "STRUCTURE_INPUT_STALE",
            "STRUCTURE_REVISION_CONFLICT",
            "玩家档案与后续 dossier 工作台尚未开放",
            "Context #${ivV2Esc(cache.token)}",
            "ivV2StatusBannerText()",
            "可直接使用下方重试按钮继续处理；错误信息会在成功刷新、成功构建或成功加载证据上下文后清除。",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.js)
        self.assertIn("await ivV2LoadImportBundle(ivV2State.importId, { keepStep: false, token });", self.js)
        self.assertIn("ivV2State.currentStep = 2;", self.js)
        self.assertIn("ivV2SetStep(2);", self.js)
        self.assertIn("if (['STRUCTURE_INPUT_CONFLICT', 'STRUCTURE_INPUT_NOT_READY'].includes(code)) {", self.js)
        self.assertIn("ivV2ResetStructureWorkspace();", self.js)
        self.assertIn("ivV2SetReviewError(data, response.status, '分组映射已变化，请先确认最新映射');", self.js)
        self.assertIn("ivV2SetReviewError(payload, status, '分组映射已变化，请先确认最新映射');", self.js)
        self.assertIn("code === 'STRUCTURE_REVISION_CONFLICT' ? '结构版本已更新，已刷新最新结果' : '结构状态已变化'", self.js)
        self.assertIn("if (!ivV2HeadsMatch(structureHeads, issuesHeads)) {", self.js)
        self.assertIn("headRetry > 0", self.js)
        self.assertIn("headRetry: headRetry - 1", self.js)
        self.assertIn("结构与问题列表版本不一致，请刷新后重试。", self.js)
        self.assertIn("const previousEvidenceRevisionId = ivV2CurrentEvidenceRevisionId();", self.js)
        self.assertIn("String(issuesHeads.evidence_revision_id) !== previousEvidenceRevisionId", self.js)

    def test_confirmed_shell_binds_change_and_input_for_review_form(self):
        self.assertIn("ivV2$('iv-v2-confirmed-shell')?.addEventListener('change', ivV2HandleEditorInputOrChange);", self.js)
        self.assertIn("ivV2$('iv-v2-confirmed-shell')?.addEventListener('input', event => {", self.js)
        self.assertIn("if (event.target.tagName === 'SELECT') return;", self.js)

    def test_context_token_is_monotonic_across_reset_and_async_invalidation(self):
        self.assertIn("ivV2State.contextToken += 1;", self.js)
        self.assertIn("ivV2State.contextBusyIssueId = '';", self.js)
        self.assertNotIn("ivV2State.contextToken = 0;", self.js)
        self.assertIn("function ivV2InvalidateEvidenceContext() {", self.js)

    def test_selected_issue_follows_visible_filter_and_keeps_all_view(self):
        self.assertIn("function ivV2SyncSelectedIssue({ preserveAll = false } = {}) {", self.js)
        self.assertIn("if (preserveAll && ivV2State.reviewFilter === 'all') {", self.js)
        self.assertIn("const nextIssue = visible[0] || null;", self.js)

    def test_evidence_context_renders_public_whitelist_fields(self):
        for needle in (
            "const evidence = cache?.payload?.evidence || null;",
            "玩家 ${ivV2Esc(evidence.participant_label || '--')}",
            "记录员 ${ivV2Esc(evidence.recorder_label || '--')}",
            "Prompt：${ivV2Esc(evidence.prompt_text || '无')}",
            "Raw：${ivV2Esc(evidence.raw_content || '')}",
            "Display：${ivV2Esc(evidence.display_content || '')}",
            "Normalized：${ivV2Esc(evidence.normalized_content || '')}",
            "身份状态 ${ivV2Esc(evidence.identity_decision_status || '--')} · 公式缓存 ${ivV2Esc(evidence.formula_cache_status || '--')}",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.js)
        self.assertIn("ivV2ClearStatusError();", self.js)
        self.assertIn("const payloadHeads = ivV2HeadPairFromPayload(data);", self.js)
        self.assertIn("if (!ivV2HeadsMatch(payloadHeads, currentHeads)) {", self.js)
        self.assertIn("证据上下文版本已变化，请刷新后重试。", self.js)
        self.assertIn("await ivV2LoadStructureWorkspace({", self.js)
        self.assertIn("token: ivV2State.requestToken,", self.js)
        self.assertIn("silentConflictRefresh: true,", self.js)
        self.assertIn("headRetry: 0,", self.js)
        self.assertIn("await ivV2EnsureEvidenceContext(refreshedIssue, {", self.js)
        self.assertIn("retryOnHeadMismatch: false", self.js)

    def test_review_filter_includes_resolved_bucket(self):
        self.assertIn("if (filter === 'resolved') {", self.js)
        self.assertIn("{ value: 'resolved', label: `已处理 ${resolved}` }", self.js)

    def test_all_v2_step3_labels_are_renamed_globally(self):
        self.assertIn("document.querySelectorAll('[data-iv-v2-step=\"3\"] .step-bar__label').forEach(node => {", self.js)
        self.assertIn("node.textContent = '结构与证据复核';", self.js)
        self.assertIn("const previewTitle = ivV2$('iv-v2-confirmed-preview')?.closest('section')?.querySelector('.iv-v2-side-card__title');", self.js)
        self.assertIn("const historyTitle = ivV2$('iv-v2-confirmed-history')?.closest('section')?.querySelector('.iv-v2-side-card__title');", self.js)
        self.assertIn("if (previewTitle) previewTitle.textContent = '结构与证据复核工作台';", self.js)
        self.assertIn("if (historyTitle) historyTitle.textContent = '版本与映射历史';", self.js)

    def test_build_success_toast_requires_workspace_load_success(self):
        self.assertIn("const loaded = await ivV2LoadStructureWorkspace({ token, silentConflictRefresh: trigger === 'auto' });", self.js)
        self.assertIn("if (loaded && ivV2IsTokenCurrent(token)) {", self.js)
        self.assertIn("return loaded;", self.js)

    def test_confirmed_shell_controls_are_truly_disabled_while_busy(self):
        self.assertIn("function ivV2SyncConfirmedControls() {", self.js)
        self.assertIn("confirmedShell.querySelectorAll('input, select, textarea, button').forEach(control => {", self.js)
        self.assertIn("control.disabled = operationBusy;", self.js)
        self.assertIn("control.disabled = operationBusy || !(ivV2IssueHasResolvableAction(issue) && ivV2IssueIsOpen(issue));", self.js)
        self.assertNotIn("if (action === 'select-review-issue') return;", self.js)

    def test_comment_input_updates_counter_without_full_rerender(self):
        self.assertIn("function ivV2SyncCommentCounter(textarea) {", self.js)
        self.assertIn("counter.textContent = `${String(textarea.value || '').length}/500`;", self.js)
        self.assertIn("ivV2SyncCommentCounter(target);", self.js)
        self.assertNotIn("if (action === 'review-draft-comment') {\n      ivV2UpdateIssueDraft(issueId, { comment: target.value || '' });\n      ivV2RenderConfirmed();", self.js)

    def test_style_is_namespaced_for_structure_review_shell(self):
        for needle in (
            ".iv-v2-review-shell",
            ".iv-v2-review-item",
            ".iv-v2-issue-detail",
            ".iv-v2-context-card",
            ".iv-v2-resolution-grid",
            ".iv-v2-structure-tree",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.css)


if __name__ == "__main__":
    unittest.main()
