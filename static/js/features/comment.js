// ============================================================
// 评论舆情分析模块
// ============================================================

const cmState = {
  sessionId: null,
  historyId: null,
  file: null,
  running: false,
  result: null,
  preprocess: null,
};

function cmGoStep(n) {
  cmState.currentStep = n;
  cmPanels.forEach((p, i) => p && p.classList.toggle('panel--hidden', i + 1 !== n));
  document.querySelectorAll('[data-comment-step]').forEach(btn => {
    const i = +btn.dataset.commentStep;
    btn.classList.remove('step-bar__item--active', 'step-bar__item--done');
    if (i < n) btn.classList.add('step-bar__item--done');
    else if (i === n) btn.classList.add('step-bar__item--active');
    btn.disabled = true; // 评论分析流程不支持步骤条回看
  });
  document.querySelector('.main')?.scrollTo({ top: 0, behavior: 'smooth' });
}
cmState.currentStep = 1;

// ── 上传区交互 ──
const cmUploadZone = $('cm-upload-zone');
const cmFileInput = $('cm-file-input');

function cmUpdateStartBtn() {
  const ready = !!cmState.file && $('cm-post-title').value.trim().length > 0;
  $('cm-btn-start').disabled = !ready || cmState.running;
}

function cmSetFile(file) {
  if (!file) return;
  if (!/\.(csv|xlsx|xls)$/i.test(file.name)) {
    showToast('仅支持 CSV / Excel 文件', 'error');
    return;
  }
  if (file.size > 50 * 1024 * 1024) {
    showToast('文件超过 50MB 上限', 'error');
    return;
  }
  cmState.file = file;
  $('cm-upload-primary').textContent = `已选择：${shortName(file.name, 32)}`;
  cmUploadZone.classList.add('upload-zone--filled');
  cmUpdateStartBtn();
}

if (cmUploadZone) {
  cmUploadZone.addEventListener('click', () => cmFileInput.click());
  cmUploadZone.addEventListener('dragover', e => { e.preventDefault(); cmUploadZone.classList.add('drag-over'); });
  cmUploadZone.addEventListener('dragleave', () => cmUploadZone.classList.remove('drag-over'));
  cmUploadZone.addEventListener('drop', e => {
    e.preventDefault();
    cmUploadZone.classList.remove('drag-over');
    cmSetFile(e.dataTransfer.files[0]);
  });
  cmFileInput.addEventListener('change', () => cmSetFile(cmFileInput.files[0]));
  $('cm-post-title').addEventListener('input', cmUpdateStartBtn);
}

// ── 开始分析 ──
function cmAppendProgress(message) {
  if (!message) return;
  $('cm-progress-msg').textContent = message;
  const log = $('cm-progress-log');
  const line = document.createElement('div');
  line.className = 'cm-progress__line';
  line.textContent = message;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function showCommentDuplicateModal(report) {
  return new Promise(resolve => {
    let existing = $('comment-duplicate-modal');
    if (existing) existing.remove();
    const modal = document.createElement('div');
    modal.id = 'comment-duplicate-modal';
    modal.style.cssText = 'position:fixed;inset:0;z-index:220;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.48);backdrop-filter:blur(4px);';
    modal.innerHTML = `
      <div style="width:min(520px,92vw);background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:22px;box-shadow:var(--shadow-lg);">
        <div style="font-size:18px;font-weight:700;color:var(--text);margin-bottom:8px">检测到历史报告</div>
        <div style="font-size:14px;color:var(--text-2);line-height:1.7;margin-bottom:16px">
          这个文件之前已经生成过评论分析报告：<br>
          <strong style="color:var(--text)">${esc(report.title || '评论分析报告')}</strong><br>
          ${esc(formatTime(report.created_at))} · 有效 ${esc(report.valid_count || 0)} 条 · 抽样 ${esc(report.sample_count || 0)} 条
        </div>
        <div style="display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap">
          <button class="btn btn--ghost" id="comment-dup-cancel">取消</button>
          <button class="btn btn--ghost" id="comment-dup-history">查看历史报告</button>
          <button class="btn btn--primary" id="comment-dup-rerun">仍然重新分析</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
    const cleanup = result => { modal.remove(); resolve(result); };
    $('comment-dup-cancel').onclick = () => cleanup('cancel');
    $('comment-dup-history').onclick = () => cleanup('history');
    $('comment-dup-rerun').onclick = () => cleanup('rerun');
  });
}

async function cmStart() {
  if (cmState.running || !cmState.file) return;
  const title = $('cm-post-title').value.trim();
  const content = $('cm-post-content').value.trim();
  if (!title) { showToast('请填写帖子标题', 'error'); return; }
  if (!content) { showToast('请填写帖子原文', 'error'); return; }

  cmState.running = true;
  cmUpdateStartBtn();
  const fd = new FormData();
  fd.append('file', cmState.file);
  fd.append('post_title', title);
  fd.append('post_content', content);

  try {
    const resp = await fetch('/api/comment-analysis/upload', { method: 'POST', body: fd });
    const raw = await resp.text();
    let data = {};
    try {
      data = raw ? JSON.parse(raw) : {};
    } catch {
      data = { detail: raw || '服务端没有返回可解析的错误信息' };
    }
    if (!resp.ok) throw new Error(data.detail || '上传失败');
    cmState.sessionId = data.session_id;
    cmState.historyId = null;
    if (data.duplicate_report) {
      const action = await showCommentDuplicateModal(data.duplicate_report);
      if (action === 'history') {
        await openHistoryEntry(data.duplicate_report.id);
        return;
      }
      if (action !== 'rerun') {
        cmGoStep(1);
        return;
      }
    }

    // 进入进度面板，先预处理大文件，再开始流式 AI 分析
    cmGoStep(2);
    $('cm-progress-log').innerHTML = '';
    cmAppendProgress('上传完成，正在准备预处理…');
    await cmPreprocess();
    await cmRun();
  } catch (e) {
    showToast(`分析失败：${e.message}`, 'error');
    cmGoStep(1);
  } finally {
    cmState.running = false;
    cmUpdateStartBtn();
  }
}

async function cmPreprocess() {
  await consumeSSE(`/api/comment-analysis/preprocess/${cmState.sessionId}`, ev => {
    if (ev.type === 'progress') {
      cmAppendProgress(ev.message);
    }
    if (ev.warning) {
      cmAppendProgress(ev.warning);
    }
    if (ev.type === 'comment_preprocess_done') {
      cmState.preprocess = ev.sample_meta || ev;
      const scanned = ev.scan_rows ?? ev.sample_meta?.scan_rows ?? 0;
      const valid = ev.valid_count ?? ev.sample_meta?.valid_count ?? 0;
      const sample = ev.sample_count ?? ev.sample_meta?.sample_count ?? 0;
      cmAppendProgress(`预处理完成：扫描 ${scanned} 行，有效评论 ${valid} 条，抽样 ${sample} 条。`);
      showToast(`有效评论 ${valid} 条，抽样 ${sample} 条`, 'success');
    }
  });
}

async function cmRun() {
  let mainReportDone = false;
  try {
    await consumeSSE(`/api/comment-analysis/run/${cmState.sessionId}`, ev => {
      if (ev.type === 'progress') {
        cmAppendProgress(ev.message);
      }
      if (ev.type === 'comment_done') {
        mainReportDone = true;
        cmState.result = ev;
        cmRenderResult(ev, { quotesPending: true });
        cmGoStep(3);
        showToast('舆情报告已生成，玩家评论原文精选继续后台生成中', 'success');
      }
      if (ev.type === 'comment_quotes_done') {
        cmState.result = { ...(cmState.result || {}), ...ev };
        cmRenderResult(cmState.result);
        cmAppendProgress('玩家评论原文精选已更新到报告。');
        if ((ev.selected_raw_comments || []).length) {
          showToast('玩家评论原文精选已生成', 'success');
        }
      }
      if (ev.type === 'comment_quotes_error') {
        cmAppendProgress(ev.message || '玩家评论原文精选生成失败，舆情报告已完成。');
        if (cmState.result) cmRenderResult(cmState.result);
        showToast(ev.message || '精选评论生成失败，舆情报告已完成', 'info');
      }
    });
  } catch (err) {
    if (mainReportDone) {
      cmAppendProgress(`玩家评论原文精选连接中断：${err.message}`);
      if (cmState.result) cmRenderResult(cmState.result);
      showToast('舆情报告已生成，精选评论连接中断', 'info');
      return;
    }
    throw err;
  }
}

// ── 结果渲染 ──
function cmRenderResult(res, opts = {}) {
  const title = String(res.title || '').trim();
  const postTitle = String(res.post_title || $('cm-post-title').value || '').trim();
  const displayTitle = title || (postTitle ? `${postTitle}·舆情简报` : '舆情简报');
  $('cm-result-title').textContent = shortName(displayTitle, 36);

  // AI 简报
  let reportMd = cmNormalizeReportMarkdown(res.report_md || '*（未生成简报）*');
  if (opts.quotesPending && !reportMd.includes('## 玩家评论原文精选')) {
    reportMd = `${reportMd}\n\n## 玩家评论原文精选\n\n> 玩家评论原文精选生成中…`;
  }
  $('cm-report-content').innerHTML = renderMarkdown(reportMd);
}

function cmNormalizeReportMarkdown(md) {
  let text = String(md || '').trim();
  const sampleIdx = text.indexOf('> 样本口径');
  if (sampleIdx > 0) {
    text = text.slice(sampleIdx).trim();
  }
  return text
    .replace(/^#\s+.*(?:\r?\n)+/g, '')
    .replace(/^##\s*(?:AI\s*)?舆情简报\s*(?:\r?\n)+/g, '')
    .trim();
}

// ── 导出 & 重置 ──
$('cm-btn-pdf')?.addEventListener('click', () => {
  if (cmState.historyId) window.location.href = `/api/export/pdf-history/${cmState.historyId}`;
  else if (cmState.sessionId) window.location.href = `/api/export/pdf/${cmState.sessionId}`;
  else showToast('还没有生成报告', 'error');
});

$('cm-btn-feishu')?.addEventListener('click', async () => {
  if (!cmState.sessionId) { showToast('还没有生成报告', 'error'); return; }
  const btn = $('cm-btn-feishu');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '导出中…';
  try {
    const url = cmState.historyId
      ? `/api/export/feishu-history/${cmState.historyId}`
      : `/api/export/feishu/${cmState.sessionId}`;
    const resp = await fetch(url, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '导出失败');
    showToast('飞书文档已创建', 'success');
    if (data.url) window.open(data.url, '_blank');
  } catch (e) {
    showToast(`导出飞书文档失败：${e.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

function cmReset() {
  cmState.sessionId = null;
  cmState.historyId = null;
  cmState.file = null;
  cmState.result = null;
  cmState.preprocess = null;
  cmFileInput.value = '';
  $('cm-post-title').value = '';
  $('cm-post-content').value = '';
  $('cm-upload-primary').textContent = '拖放文件到这里，或点击选择';
  cmUploadZone.classList.remove('upload-zone--filled');
  cmUpdateStartBtn();
  cmGoStep(1);
}

$('cm-btn-start')?.addEventListener('click', cmStart);
$('cm-btn-restart')?.addEventListener('click', cmReset);

