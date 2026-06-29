// ============================================================
// Drawer 通用控制
// ============================================================

function openDrawer(id) { $(id).classList.add('drawer--open'); }
function closeDrawer(id) { $(id).classList.remove('drawer--open'); }

document.querySelectorAll('[data-drawer-close]').forEach(el => {
  el.addEventListener('click', e => {
    const drawer = e.target.closest('.drawer');
    if (drawer) drawer.classList.remove('drawer--open');
  });
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.drawer--open').forEach(d => d.classList.remove('drawer--open'));
});

// ============================================================
// 设置抽屉（左导航切换）
// ============================================================

const STAB_LOADERS = {
  texts: loadUiTextsSettings,
  prompts: loadPrompts,
  system: loadSystemSettings,
  perms: loadPermsTab,
  audit: loadAuditLogsTab,
};

function switchSettingsTab(name) {
  document.querySelectorAll('.settings-nav__item').forEach(el => {
    el.classList.toggle('settings-nav__item--active', el.dataset.stab === name);
  });
  ['texts', 'prompts', 'system', 'perms', 'audit'].forEach(k => {
    const el = $(`stab-content-${k}`);
    if (el) el.style.display = k === name ? '' : 'none';
  });
  if (STAB_LOADERS[name]) STAB_LOADERS[name]();
}

document.querySelectorAll('.settings-nav__item[data-stab]').forEach(el => {
  el.addEventListener('click', () => switchSettingsTab(el.dataset.stab));
});

// Settings nav collapse toggle
(function () {
  const nav = $('settings-nav');
  const toggleBtn = $('btn-settings-nav-toggle');
  if (!nav || !toggleBtn) return;
  const STORAGE_KEY = 'settings-nav-collapsed';
  if (localStorage.getItem(STORAGE_KEY) === '1') nav.classList.add('settings-nav--collapsed');
  toggleBtn.addEventListener('click', () => {
    const collapsed = nav.classList.toggle('settings-nav--collapsed');
    localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0');
  });
})();

// Main sidebar collapse toggle
(function () {
  const sidebar = document.querySelector('.sidebar');
  const toggleBtn = $('btn-sidebar-toggle');
  if (!sidebar || !toggleBtn) return;
  const STORAGE_KEY = 'sidebar-collapsed';
  if (localStorage.getItem(STORAGE_KEY) === '1') sidebar.classList.add('sidebar--collapsed');
  toggleBtn.addEventListener('click', () => {
    const collapsed = sidebar.classList.toggle('sidebar--collapsed');
    localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0');
  });
})();

function loadActiveSettingsTab() {
  const active = document.querySelector('.settings-nav__item--active');
  const name = active ? active.dataset.stab : 'texts';
  switchSettingsTab(name);
}

// ── 权限配置 ──────────────────────────────────────────────────

async function loadPermsTab() {
  const body = $('stab-content-perms');
  body.innerHTML = `<div class="hist-empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const resp = await fetch('/api/admin/users');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '加载失败');
    renderPermsTable(data.users || []);
  } catch (e) {
    body.innerHTML = `<div class="hist-empty">加载权限配置失败：${esc(e.message)}</div>`;
  }
}

function renderPermsTable(users) {
  const body = $('stab-content-perms');
  const addRow = `
    <div class="perm-add-row" id="perm-add-row">
      <input type="text" id="perm-new-email" class="plan-input" placeholder="飞书邮箱 或 Open ID（ou_xxxxx）" style="flex:1;min-width:240px" />
      <div class="perm-checkboxes">
        <label><input type="checkbox" id="perm-new-survey" checked class="perm-toggle" /> 问卷分析</label>
        <label><input type="checkbox" id="perm-new-annotate" checked class="perm-toggle" /> 数据标注</label>
        <label><input type="checkbox" id="perm-new-comment" checked class="perm-toggle" /> 评论分析</label>
      </div>
      <button class="btn btn--primary btn--sm" id="perm-add-btn">添加成员</button>
    </div>`;

  const rows = users.map(u => {
    const isAdmin = u.is_admin;
    const hasSurvey = u.perms.includes('survey');
    const hasAnnotate = u.perms.includes('annotate');
    const hasComment = u.perms.includes('comment');
    const adminBadge = isAdmin ? `<span class="perm-badge">管理员</span>` : '';
    const surveyCell = isAdmin
      ? `<span style="color:var(--green)">✓</span>`
      : `<input type="checkbox" class="perm-toggle" ${hasSurvey ? 'checked' : ''} data-perm-email="${esc(u.email)}" data-perm-type="survey" />`;
    const annotateCell = isAdmin
      ? `<span style="color:var(--green)">✓</span>`
      : `<input type="checkbox" class="perm-toggle" ${hasAnnotate ? 'checked' : ''} data-perm-email="${esc(u.email)}" data-perm-type="annotate" />`;
    const commentCell = isAdmin
      ? `<span style="color:var(--green)">✓</span>`
      : `<input type="checkbox" class="perm-toggle" ${hasComment ? 'checked' : ''} data-perm-email="${esc(u.email)}" data-perm-type="comment" />`;
    const deleteBtn = isAdmin ? `<span style="color:var(--text-3);font-size:12px">—</span>`
      : `<button class="btn btn--ghost btn--sm" data-perm-delete="${esc(u.email)}">删除</button>`;
    const enabledToggle = isAdmin ? '' : `
      <input type="checkbox" class="perm-toggle" ${u.enabled ? 'checked' : ''} data-perm-email="${esc(u.email)}" data-perm-type="enabled" title="${u.enabled ? '已启用（点击禁用）' : '已禁用（点击启用）'}" />`;

    return `<tr>
      <td>${esc(u.email)} ${adminBadge}</td>
      <td style="text-align:center">${surveyCell}</td>
      <td style="text-align:center">${annotateCell}</td>
      <td style="text-align:center">${commentCell}</td>
      <td style="text-align:center">${enabledToggle}</td>
      <td style="text-align:center">${deleteBtn}</td>
    </tr>`;
  }).join('');

  body.innerHTML = addRow + `
    <table class="perm-table">
      <thead><tr>
        <th>飞书邮箱</th>
        <th style="text-align:center">问卷分析</th>
        <th style="text-align:center">数据标注</th>
        <th style="text-align:center">评论分析</th>
        <th style="text-align:center">启用</th>
        <th style="text-align:center">操作</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  // 添加成员
  $('perm-add-btn').addEventListener('click', async () => {
    const email = ($('perm-new-email').value || '').trim();
    if (!email) { showToast('请输入邮箱或 Open ID', 'error'); return; }
    const perms = [];
    if ($('perm-new-survey').checked) perms.push('survey');
    if ($('perm-new-annotate').checked) perms.push('annotate');
    if ($('perm-new-comment').checked) perms.push('comment');
    try {
      const r = await fetch('/api/admin/users', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, perms }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || '添加失败');
      showToast(`已添加 ${email}`, 'success');
      loadPermsTab();
    } catch (e) { showToast(e.message, 'error'); }
  });

  // 权限勾选 + 启用状态变更
  body.querySelectorAll('[data-perm-email][data-perm-type]').forEach(cb => {
    cb.addEventListener('change', async () => {
      const email = cb.dataset.permEmail;
      const type = cb.dataset.permType;
      const checked = cb.checked;
      try {
        let patch = {};
        if (type === 'enabled') {
          patch = { enabled: checked };
        } else {
          // 重新读取该行另一个 checkbox 的状态
          const row = cb.closest('tr');
          const surveyEl = row.querySelector('[data-perm-type="survey"]');
          const annotateEl = row.querySelector('[data-perm-type="annotate"]');
          const commentEl = row.querySelector('[data-perm-type="comment"]');
          const perms = [];
          if ((type === 'survey' ? checked : surveyEl?.checked)) perms.push('survey');
          if ((type === 'annotate' ? checked : annotateEl?.checked)) perms.push('annotate');
          if ((type === 'comment' ? checked : commentEl?.checked)) perms.push('comment');
          patch = { perms };
        }
        const r = await fetch(`/api/admin/users/${encodeURIComponent(email)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch)
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || '更新失败');
        showToast('已保存', 'success', 1500);
      } catch (e) { showToast(e.message, 'error'); cb.checked = !checked; }
    });
  });

  // 删除
  body.querySelectorAll('[data-perm-delete]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const email = btn.dataset.permDelete;
      if (!confirm(`确认删除 ${email}？`)) return;
      try {
        const r = await fetch(`/api/admin/users/${encodeURIComponent(email)}`, { method: 'DELETE' });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || '删除失败');
        showToast(`已删除 ${email}`, 'success');
        loadPermsTab();
      } catch (e) { showToast(e.message, 'error'); }
    });
  });
}

function auditFeatureLabel(features, key) {
  const item = (features || []).find(f => f.key === key);
  return item ? item.label : (key || '未知功能');
}

function formatAuditTime(ts) {
  return String(ts || '').replace('T', ' ');
}

async function loadAuditLogsTab() {
  const body = $('stab-content-audit');
  if (!body) return;
  body.innerHTML = `<div class="hist-empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  const params = new URLSearchParams();
  Object.entries(state.auditFilters || {}).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  params.set('limit', '300');
  try {
    const resp = await fetch(`/api/admin/audit-logs?${params.toString()}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '加载失败');
    renderAuditLogsTab(data);
  } catch (e) {
    body.innerHTML = `<div class="hist-empty">加载用户日志失败：${esc(e.message)}</div>`;
  }
}

function renderAuditLogsTab(data) {
  const body = $('stab-content-audit');
  const filters = state.auditFilters || { start: '', end: '', user: '', feature: '' };
  const users = (data.users || []).filter(u => u.email);
  const features = data.features || [];
  const logs = data.logs || [];
  const userOptions = users.map(u => `
    <option value="${esc(u.email)}" ${filters.user === u.email ? 'selected' : ''}>${esc(u.email)}</option>
  `).join('');
  const featureOptions = features.map(f => `
    <option value="${esc(f.key)}" ${filters.feature === f.key ? 'selected' : ''}>${esc(f.label)}</option>
  `).join('');
  const rows = logs.map((item, idx) => {
    const userText = item.user_email || item.user_name || item.open_id || '未识别用户';
    const featureText = item.feature_label || auditFeatureLabel(features, item.feature);
    const actionText = item.action || '';
    const detailText = item.detail || '';
    const statusText = item.status || 'success';
    return `
    <tr class="audit-row" data-audit-row="${idx}">
      <td class="audit-time" title="${esc(formatAuditTime(item.ts))}">${esc(formatAuditTime(item.ts))}</td>
      <td>
        <div class="audit-user" title="${esc(userText)}">${esc(userText)}</div>
        ${item.user_name ? `<div class="audit-sub">${esc(item.user_name)}</div>` : ''}
      </td>
      <td title="${esc(featureText)}"><span class="audit-feature">${esc(featureText)}</span></td>
      <td class="audit-action" title="${esc(actionText)}">${esc(actionText)}</td>
      <td class="audit-detail" title="${esc(detailText)}">${esc(detailText)}</td>
      <td><span class="audit-status audit-status--${esc(statusText)}">${esc(statusText)}</span></td>
    </tr>
    <tr class="audit-detail-row" data-audit-detail="${idx}" hidden>
      <td colspan="6">
        <div class="audit-detail-card">
          <div><strong>用户</strong><span>${esc(userText)}${item.user_name ? `（${esc(item.user_name)}）` : ''}</span></div>
          <div><strong>功能</strong><span>${esc(featureText)}</span></div>
          <div><strong>操作</strong><span>${esc(actionText || '无')}</span></div>
          <div><strong>详情</strong><span>${esc(detailText || '无')}</span></div>
        </div>
      </td>
    </tr>
  `;
  }).join('');

  body.innerHTML = `
    <div class="audit-panel">
      <div class="audit-filters">
        <label>开始时间<input type="datetime-local" id="audit-filter-start" value="${esc(filters.start)}" /></label>
        <label>结束时间<input type="datetime-local" id="audit-filter-end" value="${esc(filters.end)}" /></label>
        <label>用户
          <select id="audit-filter-user">
            <option value="">全部用户</option>
            ${userOptions}
          </select>
        </label>
        <label>功能
          <select id="audit-filter-feature">
            <option value="">全部功能</option>
            ${featureOptions}
          </select>
        </label>
        <button class="btn btn--primary btn--sm" id="audit-filter-apply">筛选</button>
        <button class="btn btn--ghost btn--sm" id="audit-filter-reset">重置</button>
      </div>
      <div class="audit-summary">当前显示 ${logs.length} 条，匹配总数 ${data.total ?? logs.length} 条</div>
      <div class="audit-table-wrap">
        <table class="perm-table audit-table">
          <thead><tr>
            <th>时间</th>
            <th>用户</th>
            <th>功能</th>
            <th>操作</th>
            <th>做了什么</th>
            <th>状态</th>
          </tr></thead>
          <tbody>${rows || `<tr><td colspan="6" class="audit-empty">暂无日志</td></tr>`}</tbody>
        </table>
      </div>
    </div>
  `;

  $('audit-filter-apply').addEventListener('click', () => {
    state.auditFilters = {
      start: $('audit-filter-start').value || '',
      end: $('audit-filter-end').value || '',
      user: $('audit-filter-user').value || '',
      feature: $('audit-filter-feature').value || '',
    };
    loadAuditLogsTab();
  });
  $('audit-filter-reset').addEventListener('click', () => {
    state.auditFilters = { start: '', end: '', user: '', feature: '' };
    loadAuditLogsTab();
  });
  body.querySelectorAll('[data-audit-row]').forEach(row => {
    row.addEventListener('click', () => {
      const detail = body.querySelector(`[data-audit-detail="${row.dataset.auditRow}"]`);
      if (!detail) return;
      const open = detail.hasAttribute('hidden');
      body.querySelectorAll('.audit-detail-row').forEach(r => {
        if (r !== detail) r.setAttribute('hidden', '');
      });
      body.querySelectorAll('.audit-row--open').forEach(r => r.classList.remove('audit-row--open'));
      if (open) {
        detail.removeAttribute('hidden');
        row.classList.add('audit-row--open');
      } else {
        detail.setAttribute('hidden', '');
      }
    });
  });
}

async function loadPrompts() {
  const body = $('stab-content-prompts');
  body.innerHTML = `<div class="hist-empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const resp = await fetch('/api/prompts');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '加载失败');
    body.innerHTML = Object.values(data).map(promptCardHTML).join('');
  } catch (e) {
    body.innerHTML = `<div class="hist-empty">加载提示词失败：${esc(e.message)}</div>`;
  }
}

async function loadUiTextsSettings() {
  const body = $('stab-content-texts');
  body.innerHTML = `<div class="hist-empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const resp = await fetch('/api/ui-texts');
    if (!resp.ok) throw new Error('加载失败');
    const texts = await resp.json();
    body.innerHTML = Object.entries(texts).map(([key, item]) => `
      <div class="uitext-card" data-uitext-key="${esc(key)}">
        <div class="uitext-card__label">${esc(item.label)}</div>
        <textarea class="prompt-textarea uitext-textarea" rows="2">${esc(item.current)}</textarea>
        <div class="uitext-card__actions">
          <button class="btn btn--primary btn--sm" data-uitext-save="${esc(key)}">保存</button>
        </div>
      </div>
    `).join('');
  } catch (e) {
    body.innerHTML = `<div class="hist-empty">加载失败：${esc(e.message)}</div>`;
  }
}

$('stab-content-texts').addEventListener('click', async e => {
  const btn = e.target.closest('[data-uitext-save]');
  if (!btn) return;
  const key = btn.dataset.uitextSave;
  const card = btn.closest('.uitext-card');
  const textarea = card.querySelector('.uitext-textarea');
  try {
    btn.textContent = '保存中…';
    btn.disabled = true;
    const resp = await fetch(`/api/ui-texts/${key}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: textarea.value }),
    });
    if (!resp.ok) { const d = await resp.json(); throw new Error(d.detail || '保存失败'); }
    showToast('文案已保存', 'success');
    const el = document.querySelector(`[data-uitext="${key}"]`);
    if (el) el.textContent = textarea.value;
  } catch (err) {
    showToast(`保存失败：${err.message}`, 'error');
  } finally {
    btn.textContent = '保存';
    btn.disabled = false;
  }
});

async function loadSystemSettings() {
  const body = $('stab-content-system');
  body.innerHTML = `<div class="hist-empty"><div class="spinner" style="margin:0 auto"></div></div>`;
  try {
    const resp = await fetch('/api/app-settings');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '加载失败');
    body.innerHTML = `
      <div class="uitext-card">
        <div class="uitext-card__label">评论分析·重复文件提醒</div>
        <div class="prompt-card__desc">开启后，用户上传已生成过历史报告的同一文件时，会先提示可查看历史报告或继续重新分析。</div>
        <label class="setting-toggle">
          <input type="checkbox" id="setting-comment-duplicate" ${data.comment_duplicate_reminder_enabled ? 'checked' : ''} />
          <span>开启重复文件提醒</span>
        </label>
      </div>
    `;
  } catch (e) {
    body.innerHTML = `<div class="hist-empty">加载平台设置失败：${esc(e.message)}</div>`;
  }
}

$('stab-content-system')?.addEventListener('change', async e => {
  const input = e.target.closest('#setting-comment-duplicate');
  if (!input) return;
  input.disabled = true;
  try {
    const resp = await fetch('/api/app-settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ comment_duplicate_reminder_enabled: input.checked }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '保存失败');
    input.checked = !!data.comment_duplicate_reminder_enabled;
    showToast('平台设置已保存', 'success');
  } catch (err) {
    input.checked = !input.checked;
    showToast(`保存失败：${err.message}`, 'error');
  } finally {
    input.disabled = false;
  }
});

function promptCardHTML(p) {
  const readonly = !p.editable;
  const badge = readonly
    ? `<span class="prompt-card__badge">Dify 管理</span>` : '';
  const difyLink = (readonly && p.dify_url)
    ? `<a class="prompt-dify-link" href="${esc(p.dify_url)}" target="_blank" rel="noopener">
         <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
         前往 Dify 后台
       </a>` : '';

  const editActions = readonly ? '' : `
    <div class="prompt-card__actions">
      <input class="prompt-note-input" data-note="${esc(p.key)}" placeholder="修改说明（可选）" />
      <button class="btn btn--primary" data-save="${esc(p.key)}">保存</button>
    </div>`;

  const hist = (p.history || []).length ? `
    <div class="prompt-history">
      <button class="prompt-history__toggle" data-hist-toggle="${esc(p.key)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
        修改历史（${p.history.length}）
      </button>
      <div class="prompt-history__list" data-hist-list="${esc(p.key)}">
        ${p.history.map(h => `
          <div class="history-item">
            <div class="history-item__meta">
              <span class="history-item__ts">${esc(h.ts)}</span>
              <span class="history-item__note">${esc(h.note || '')}</span>
            </div>
            <div class="history-item__preview" title="${esc(h.content)}">${esc((h.content || '').slice(0, 120))}</div>
          </div>`).join('')}
      </div>
    </div>` : '';

  return `<div class="prompt-card ${readonly ? 'prompt-card--readonly' : ''}">
    <div class="prompt-card__header">
      <div class="prompt-card__title">${esc(p.label)}</div>
      ${badge}
    </div>
    <div class="prompt-card__desc">${esc(p.description || '')}</div>
    ${difyLink}
    <textarea class="prompt-textarea" data-content="${esc(p.key)}" ${readonly ? 'readonly' : ''}>${esc(p.current || '')}</textarea>
    ${editActions}
    ${hist}
  </div>`;
}

$('stab-content-prompts').addEventListener('click', async e => {
  // 历史折叠
  const toggle = e.target.closest('[data-hist-toggle]');
  if (toggle) {
    const key = toggle.dataset.histToggle;
    document.querySelector(`[data-hist-list="${key}"]`)?.classList.toggle('open');
    return;
  }
  // 保存
  const saveBtn = e.target.closest('[data-save]');
  if (saveBtn) {
    const key = saveBtn.dataset.save;
    const content = document.querySelector(`[data-content="${key}"]`).value;
    const note = (document.querySelector(`[data-note="${key}"]`) || {}).value || '';
    saveBtn.disabled = true;
    saveBtn.textContent = '保存中…';
    try {
      const resp = await fetch(`/api/prompts/${key}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, note }),
      });
      const d = await resp.json();
      if (!resp.ok) throw new Error(d.detail || '保存失败');
      showToast('提示词已保存，下次分析生效', 'success');
      loadPrompts();
    } catch (err) {
      showToast(`保存失败：${err.message}`, 'error');
      saveBtn.disabled = false;
      saveBtn.textContent = '保存';
    }
  }
});

