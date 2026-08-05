// ============================================================
// Drawer 通用控制
// ============================================================

function openDrawer(id) { $(id).classList.add('drawer--open'); }
function closeDrawer(id) {
  $(id).classList.remove('drawer--open');
  if (id === 'settings-drawer') closePromptEditor();
}

document.querySelectorAll('[data-drawer-close]').forEach(el => {
  el.addEventListener('click', e => {
    const drawer = e.target.closest('.drawer');
    if (drawer) {
      drawer.classList.remove('drawer--open');
      if (drawer.id === 'settings-drawer') closePromptEditor();
    }
  });
});
document.addEventListener('keydown', e => {
  if (e.key === 'Tab' && promptEditorState.key) {
    const layer = $('prompt-editor-layer');
    const focusable = layer ? Array.from(layer.querySelectorAll(
      'button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), a[href], summary, [contenteditable="true"], [tabindex]:not([tabindex="-1"])'
    )).filter(el => !el.hidden && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden') : [];
    if (!focusable.length) {
      e.preventDefault();
      return;
    }
    const activeIndex = focusable.indexOf(document.activeElement);
    const direction = e.shiftKey ? -1 : 1;
    const nextIndex = activeIndex < 0
      ? (e.shiftKey ? focusable.length - 1 : 0)
      : (activeIndex + direction + focusable.length) % focusable.length;
    e.preventDefault();
    focusable[nextIndex].focus();
    return;
  }
  if (e.key !== 'Escape') return;
  if (promptEditorState.key) {
    e.preventDefault();
    closePromptEditor({ rerender: true });
    return;
  }
  document.querySelectorAll('.drawer--open').forEach(d => d.classList.remove('drawer--open'));
});

// ============================================================
// 设置抽屉（左导航切换）
// ============================================================

const STAB_LOADERS = {
  texts: loadUiTextsSettings,
  prompts: () => loadPrompts(true),
  glossary: () => loadGlossary(true),
  system: loadSystemSettings,
  perms: loadPermsTab,
  audit: loadAuditLogsTab,
};

function switchSettingsTab(name) {
  document.querySelectorAll('.settings-nav__item').forEach(el => {
    el.classList.toggle('settings-nav__item--active', el.dataset.stab === name);
  });
  ['texts', 'prompts', 'glossary', 'system', 'perms', 'audit'].forEach(k => {
    const el = $(`stab-content-${k}`);
    if (el) el.style.display = k === name ? '' : 'none';
  });
  if (name !== 'prompts') closePromptEditor();
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

// ── 术语库 ──────────────────────────────────────────────────

const GLOSSARY_IMPORT_INPUT_ID = 'glossary-import-input';
const GLOSSARY_STATUS_OPTIONS = [
  { value: 'all', label: '全部状态' },
  { value: 'enabled', label: '仅看启用' },
  { value: 'disabled', label: '仅看停用' },
];
const GLOSSARY_IMPORT_ACTION_LABELS = {
  create: '新增',
  update: '更新',
  unchanged: '无变化',
  error: '异常',
};

const glossaryUiState = {
  loaded: false,
  loading: false,
  loadPromise: null,
  items: [],
  pendingToggleIds: new Set(),
  revision: '',
  languages: [],
  filters: {
    search: '',
    category: 'all',
    status: 'all',
  },
  editor: {
    open: false,
    mode: 'create',
    targetId: '',
    category: '',
    ch: '',
    note: '',
    enabled: true,
    languages: [{ code: 'en', value: '' }],
    busy: false,
  },
  importer: {
    file: null,
    preview: null,
    fileHash: '',
    baseRevision: '',
    busy: false,
    error: '',
  },
};

function resetGlossaryEditor(overrides = {}) {
  glossaryUiState.editor = {
    open: false,
    mode: 'create',
    targetId: '',
    category: '',
    ch: '',
    note: '',
    enabled: true,
    languages: [{ code: 'en', value: '' }],
    busy: false,
    ...overrides,
  };
}

function resetGlossaryImporter(overrides = {}) {
  glossaryUiState.importer = {
    file: null,
    preview: null,
    fileHash: '',
    baseRevision: '',
    busy: false,
    error: '',
    ...overrides,
  };
}

function normalizeGlossaryAliases(value) {
  if (Array.isArray(value)) {
    return value
      .map(item => String(item ?? '').trim())
      .filter(Boolean);
  }
  return String(value ?? '')
    .split('|')
    .map(item => item.trim())
    .filter(Boolean);
}

function normalizeGlossaryItem(item) {
  const source = item && typeof item === 'object' ? item : {};
  const languageSource = source.terms_by_lang && typeof source.terms_by_lang === 'object'
    ? source.terms_by_lang
    : {};
  const languages = {};

  Object.entries(languageSource).forEach(([code, value]) => {
    const trimmedCode = String(code || '').trim().toLowerCase();
    if (!trimmedCode || trimmedCode === 'ch') return;
    const aliases = normalizeGlossaryAliases(value);
    if (aliases.length) languages[trimmedCode] = aliases;
  });

  return {
    id: String(source.id ?? ''),
    category: String(source.category ?? '').trim(),
    ch: String(source.ch ?? '').trim(),
    note: String(source.note ?? '').trim(),
    enabled: source.enabled !== false,
    languages,
  };
}

function normalizeGlossaryResponse(data) {
  const source = data && typeof data === 'object' ? data : {};
  const items = Array.isArray(source.items)
    ? source.items.map(normalizeGlossaryItem).filter(item => item.ch)
    : [];
  const languages = Array.from(new Set([
    ...(Array.isArray(source.languages) ? source.languages : []),
    ...items.flatMap(item => Object.keys(item.languages)),
  ].map(code => String(code || '').trim().toLowerCase()).filter(Boolean))).sort();

  return {
    items,
    revision: String(source.revision ?? ''),
    languages,
  };
}

function glossaryCategoryOptions(items = glossaryUiState.items) {
  return Array.from(new Set(
    items.map(item => item.category).filter(Boolean)
  )).sort((a, b) => a.localeCompare(b, 'zh-CN'));
}

function glossaryVisibleItems() {
  const query = glossaryUiState.filters.search.trim().toLowerCase();
  return glossaryUiState.items.filter(item => {
    const matchesQuery = !query || [
      item.category,
      item.ch,
      item.note,
      ...Object.values(item.languages).flat(),
    ].some(value => String(value || '').toLowerCase().includes(query));
    const matchesCategory = glossaryUiState.filters.category === 'all' || item.category === glossaryUiState.filters.category;
    const matchesStatus = glossaryUiState.filters.status === 'all'
      || (glossaryUiState.filters.status === 'enabled' ? item.enabled : !item.enabled);
    return matchesQuery && matchesCategory && matchesStatus;
  });
}

function glossaryDisplayLanguages() {
  const order = glossaryUiState.languages.length ? glossaryUiState.languages : [];
  const extra = glossaryVisibleItems().flatMap(item => Object.keys(item.languages));
  return Array.from(new Set([...order, ...extra])).sort();
}

function glossarySummaryCounts(items = glossaryUiState.items) {
  const enabled = items.filter(item => item.enabled).length;
  return {
    total: items.length,
    enabled,
    disabled: items.length - enabled,
  };
}

function glossaryPendingToggle(id) {
  return glossaryUiState.pendingToggleIds.has(String(id || ''));
}

function applyGlossaryCatalog(data) {
  const normalized = normalizeGlossaryResponse(data);
  glossaryUiState.items = normalized.items;
  glossaryUiState.languages = normalized.languages;
  glossaryUiState.revision = normalized.revision;
  if (
    glossaryUiState.filters.category !== 'all'
    && !glossaryCategoryOptions(normalized.items).includes(glossaryUiState.filters.category)
  ) {
    glossaryUiState.filters.category = 'all';
  }
}

function applyGlossaryUpsert(item, revision) {
  const normalized = normalizeGlossaryItem(item);
  const index = glossaryUiState.items.findIndex(entry => entry.id === normalized.id);
  if (index >= 0) {
    glossaryUiState.items.splice(index, 1, normalized);
  } else {
    glossaryUiState.items.push(normalized);
  }
  glossaryUiState.items.sort((a, b) => (
    String(a.category || '').localeCompare(String(b.category || ''), 'zh-CN')
    || String(a.ch || '').localeCompare(String(b.ch || ''), 'zh-CN')
  ));
  glossaryUiState.languages = Array.from(new Set(
    glossaryUiState.items.flatMap(entry => Object.keys(entry.languages || {}))
  )).sort();
  glossaryUiState.revision = revision ?? glossaryUiState.revision;
  if (
    glossaryUiState.filters.category !== 'all'
    && !glossaryCategoryOptions().includes(glossaryUiState.filters.category)
  ) {
    glossaryUiState.filters.category = 'all';
  }
}

function glossaryLanguageRowsHtml(rows) {
  const safeRows = rows.length ? rows : [{ code: '', value: '' }];
  return safeRows.map((row, index) => `
    <div class="glossary-editor__lang-row">
      <input class="glossary-input glossary-input--code" type="text" data-glossary-lang-code="${index}" value="${esc(row.code || '')}" placeholder="en" maxlength="16" />
      <input class="glossary-input glossary-input--aliases" type="text" data-glossary-lang-value="${index}" value="${esc(row.value || '')}" placeholder="名称；多个别名用 | 分隔" />
      <button class="btn btn--ghost btn--sm" type="button" data-glossary-remove-lang="${index}">移除</button>
    </div>
  `).join('');
}

function glossaryTableHtml(items, languages) {
  const rows = items.map(item => `
    <tr>
      <td>${esc(item.category || '未分类')}</td>
      <td>
        <div class="glossary-term__main">${esc(item.ch)}</div>
        <div class="glossary-term__sub">${esc(item.note || '无备注')}</div>
      </td>
      ${languages.map(code => `<td title="${esc((item.languages[code] || []).join(' | '))}">${esc((item.languages[code] || []).join(' | ') || '—')}</td>`).join('')}
      <td>
        <button
          class="glossary-status-switch${item.enabled ? ' glossary-status-switch--on' : ''}${glossaryPendingToggle(item.id) ? ' glossary-status-switch--busy' : ''}"
          type="button"
          role="switch"
          aria-checked="${item.enabled ? 'true' : 'false'}"
          aria-label="${esc(`${item.ch} 当前${item.enabled ? '已启用' : '已停用'}，点击${item.enabled ? '停用' : '启用'}`)}"
          aria-busy="${glossaryPendingToggle(item.id) ? 'true' : 'false'}"
          data-glossary-toggle="${esc(item.id)}"
          ${glossaryPendingToggle(item.id) ? 'disabled' : ''}
        >
          <span class="glossary-status-switch__track" aria-hidden="true">
            <span class="glossary-status-switch__text glossary-status-switch__text--on">开</span>
            <span class="glossary-status-switch__text glossary-status-switch__text--off">关</span>
            <span class="glossary-status-switch__thumb"></span>
          </span>
          <span class="glossary-status-switch__label">${glossaryPendingToggle(item.id) ? '更新中…' : item.enabled ? '已启用' : '已停用'}</span>
        </button>
      </td>
      <td>
        <div class="glossary-row-actions">
          <button class="btn btn--ghost btn--sm" type="button" data-glossary-edit="${esc(item.id)}">编辑</button>
          <button class="btn btn--ghost btn--sm" type="button" data-glossary-delete="${esc(item.id)}">删除</button>
        </div>
      </td>
    </tr>
  `).join('');

  return `
    <div class="glossary-table-wrap">
      <table class="glossary-table">
        <thead>
          <tr>
            <th>分类</th>
            <th>标准中文 ch</th>
            ${languages.map(code => `<th>${esc(code)}</th>`).join('')}
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function glossaryPreviewRows(preview) {
  if (Array.isArray(preview?.rows)) return preview.rows;
  return [];
}

function glossaryPreviewRowsHtml(preview, languages) {
  const rows = glossaryPreviewRows(preview);
  if (!rows.length) {
    return `<div class="glossary-empty">预览没有返回可导入的术语。</div>`;
  }
  return `
    <div class="glossary-table-wrap">
      <table class="glossary-table glossary-table--preview">
        <thead>
          <tr>
            <th>结果</th>
            <th>分类</th>
            <th>ch</th>
            ${languages.map(code => `<th>${esc(code)}</th>`).join('')}
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(row => {
            const normalized = normalizeGlossaryItem(row.item || row);
            const action = String(row.action || row.status || '').trim();
            const status = GLOSSARY_IMPORT_ACTION_LABELS[action] || action || '预览';
            const detail = String(row.detail || row.message || '').trim();
            return `
              <tr>
                <td><span class="glossary-status glossary-status--preview">${esc(status)}</span></td>
                <td>${esc(normalized.category || '未分类')}</td>
                <td>${esc(normalized.ch)}</td>
                ${languages.map(code => `<td>${esc((normalized.languages[code] || []).join(' | ') || '—')}</td>`).join('')}
                <td>${esc(detail || '—')}</td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function glossaryCanCommit(preview) {
  if (!preview) return false;
  if (preview.can_commit === false) return false;
  const stats = preview.stats || {};
  const conflictCount = Number(stats.conflicts ?? stats.conflict ?? 0);
  const errorCount = Number(stats.errors ?? stats.error ?? 0);
  const previewErrors = Array.isArray(preview.errors)
    ? preview.errors.map(error => String(error || '').trim()).filter(Boolean)
    : [];
  return conflictCount === 0 && errorCount === 0 && previewErrors.length === 0;
}

function renderGlossaryTab() {
  const body = $('stab-content-glossary');
  if (!body) return;

  const visibleItems = glossaryVisibleItems();
  const categories = glossaryCategoryOptions();
  const languages = glossaryDisplayLanguages();
  const summary = glossarySummaryCounts();
  const preview = glossaryUiState.importer.preview;
  const previewStats = preview?.stats || {};
  const previewRows = glossaryPreviewRows(preview);
  const previewCanCommit = glossaryCanCommit(preview) && !!glossaryUiState.importer.fileHash;
  const previewErrors = Array.isArray(preview?.errors)
    ? preview.errors.map(error => String(error || '').trim()).filter(Boolean)
    : [];
  const importPanelVisible = !!(
    preview
    || glossaryUiState.importer.file
    || glossaryUiState.importer.busy
    || glossaryUiState.importer.error
  );
  const previewLanguages = Array.from(new Set([
    ...(preview?.languages || []),
    ...previewRows.flatMap(row => Object.keys(normalizeGlossaryItem(row.item || row).languages)),
  ])).sort();

  body.innerHTML = `
    <section class="glossary-settings">
      <header class="glossary-settings__header">
        <div>
          <h2 class="glossary-settings__title">术语库</h2>
          <p class="glossary-settings__desc">命中术语时统一使用标准中文名；玩家原话、来源引用和结构化标识保持原样；未命中时保留原文，不让模型猜译。</p>
        </div>
        <div class="glossary-settings__summary">
          <span>当前 ${summary.total} 条</span>
          <span>启用 ${summary.enabled} 条</span>
          <span>停用 ${summary.disabled} 条</span>
        </div>
      </header>

      <div class="glossary-toolbar">
        <div class="glossary-toolbar__filters">
          <label class="prompt-search glossary-toolbar__search" for="glossary-search-input">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="11" cy="11" r="7"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            <input id="glossary-search-input" type="search" value="${esc(glossaryUiState.filters.search)}" placeholder="搜索分类、中文名或任意语言别名" />
          </label>
          <label class="glossary-select">
            <select id="glossary-category-filter" aria-label="分类筛选">
              <option value="all">全部分类</option>
              ${categories.map(category => `<option value="${esc(category)}" ${glossaryUiState.filters.category === category ? 'selected' : ''}>${esc(category)}</option>`).join('')}
            </select>
          </label>
          <label class="glossary-select">
            <select id="glossary-status-filter" aria-label="状态筛选">
              ${GLOSSARY_STATUS_OPTIONS.map(option => `<option value="${option.value}" ${glossaryUiState.filters.status === option.value ? 'selected' : ''}>${option.label}</option>`).join('')}
            </select>
          </label>
        </div>
        <div class="glossary-toolbar__actions">
          <button class="btn btn--ghost btn--sm" type="button" data-glossary-template>下载模板</button>
          <button class="btn btn--ghost btn--sm" type="button" data-glossary-export>导出全部</button>
          <button class="btn btn--ghost btn--sm" type="button" data-glossary-import>导入 Excel</button>
          <button class="btn btn--primary btn--sm" type="button" data-glossary-create>新增术语</button>
        </div>
      </div>

      <input id="${GLOSSARY_IMPORT_INPUT_ID}" type="file" accept=".xlsx" hidden />

      <div class="glossary-meta">
        <span>语言列：${languages.length ? languages.map(code => esc(code)).join('、') : '尚未配置'}</span>
        <span>当前显示 ${visibleItems.length} 条</span>
        <span>${glossaryUiState.revision ? `版本 ${esc(glossaryUiState.revision)}` : '版本未返回'}</span>
      </div>

      ${visibleItems.length ? glossaryTableHtml(visibleItems, languages) : `<div class="glossary-empty">没有符合当前筛选条件的术语。</div>`}

      <section class="glossary-panel${glossaryUiState.editor.open ? '' : ' glossary-panel--hidden'}" id="glossary-editor-panel">
        <div class="glossary-panel__head">
          <div>
            <h3>${glossaryUiState.editor.mode === 'edit' ? '编辑术语' : '新增术语'}</h3>
            <p>支持动态语言列；同一语言多个别名用 <code>|</code> 分隔。</p>
          </div>
          <button class="btn btn--ghost btn--sm" type="button" data-glossary-cancel-editor>收起</button>
        </div>
        <div class="glossary-editor">
          <label class="glossary-field">
            <span>分类</span>
            <input class="glossary-input" id="glossary-editor-category" list="glossary-category-options" value="${esc(glossaryUiState.editor.category)}" placeholder="例如：英雄 / 皮肤 / 装备" />
          </label>
          <label class="glossary-field">
            <span>标准中文名 ch</span>
            <input class="glossary-input" id="glossary-editor-ch" value="${esc(glossaryUiState.editor.ch)}" placeholder="必填，例如：米娅" />
          </label>
          <label class="glossary-field glossary-field--wide">
            <span>备注</span>
            <textarea class="glossary-input glossary-input--textarea" id="glossary-editor-note" rows="2" placeholder="可选，例如：正式服英雄名">${esc(glossaryUiState.editor.note)}</textarea>
          </label>
          <label class="glossary-switch glossary-field--wide">
            <input type="checkbox" id="glossary-editor-enabled" ${glossaryUiState.editor.enabled ? 'checked' : ''} />
            <span>启用该术语</span>
          </label>
          <div class="glossary-field glossary-field--wide">
            <div class="glossary-field__title-row">
              <div>
                <span>其他语言</span>
                <small>语言代码可自由增加，支持 en / id / ru 等任意列。</small>
              </div>
              <button class="btn btn--ghost btn--sm" type="button" data-glossary-add-lang>增加语言</button>
            </div>
            <div class="glossary-editor__lang-list">
              ${glossaryLanguageRowsHtml(glossaryUiState.editor.languages)}
            </div>
          </div>
          <div class="glossary-panel__actions glossary-field--wide">
            <button class="btn btn--ghost btn--sm" type="button" data-glossary-cancel-editor>取消</button>
            <button class="btn btn--primary btn--sm" type="button" data-glossary-save ${glossaryUiState.editor.busy ? 'disabled' : ''}>${glossaryUiState.editor.busy ? '保存中…' : '保存术语'}</button>
          </div>
        </div>
        <datalist id="glossary-category-options">
          ${categories.map(category => `<option value="${esc(category)}"></option>`).join('')}
        </datalist>
      </section>

      <section class="glossary-panel${importPanelVisible ? '' : ' glossary-panel--hidden'}" id="glossary-import-panel">
        <div class="glossary-panel__head">
          <div>
            <h3>导入预览${glossaryUiState.importer.file ? `：${esc(glossaryUiState.importer.file.name)}` : ''}</h3>
            <p>先检查列结构、缺失中文名和重复别名；确认前不会写入术语库。</p>
          </div>
          <button class="btn btn--ghost btn--sm" type="button" data-glossary-cancel-import>取消</button>
        </div>
        ${glossaryUiState.importer.error ? `<div class="glossary-alert glossary-alert--warning">${esc(glossaryUiState.importer.error)}</div>` : ''}
        ${!preview && glossaryUiState.importer.busy ? `<div class="glossary-empty"><div class="spinner" style="margin:0 auto 10px"></div>正在校验文件并生成预览…</div>` : ''}
        ${preview ? `
          <div class="glossary-import__meta">
            <span>识别列：${(previewLanguages.length ? previewLanguages : ['ch']).map(code => esc(code)).join('、')}</span>
            <span>可新增 ${esc(previewStats.created ?? previewStats.create ?? 0)} 条</span>
            <span>可更新 ${esc(previewStats.updated ?? previewStats.update ?? 0)} 条</span>
            ${(previewStats.conflicts ?? previewStats.conflict ?? 0) ? `<span>冲突 ${esc(previewStats.conflicts ?? previewStats.conflict)} 条</span>` : ''}
            ${(previewStats.errors ?? 0) ? `<span>异常 ${esc(previewStats.errors)} 条</span>` : ''}
          </div>
          ${previewErrors.length ? `<div class="glossary-alert glossary-alert--warning">${previewErrors.map(error => `<div>${esc(error)}</div>`).join('')}</div>` : ''}
          ${glossaryPreviewRowsHtml(preview, previewLanguages)}
          <div class="glossary-import__warning">若文件被替换，或其他管理员先修改了术语库，确认时会要求重新预览。</div>
          <div class="glossary-panel__actions">
            <button class="btn btn--ghost btn--sm" type="button" data-glossary-repreview ${glossaryUiState.importer.busy ? 'disabled' : ''}>重新预览</button>
            <button class="btn btn--primary btn--sm" type="button" data-glossary-commit ${glossaryUiState.importer.busy || !previewCanCommit ? 'disabled' : ''}>${glossaryUiState.importer.busy ? '导入中…' : `确认导入 ${(previewStats.total ?? previewRows.length ?? 0)} 条`}</button>
          </div>
        ` : ''}
      </section>
    </section>
  `;
}

async function loadGlossary(force = false) {
  const body = $('stab-content-glossary');
  if (!body) return;
  if (glossaryUiState.loaded && !force) {
    renderGlossaryTab();
    return;
  }
  if (glossaryUiState.loading) {
    const pending = glossaryUiState.loadPromise;
    if (pending) await pending;
    if (!force && glossaryUiState.loaded) renderGlossaryTab();
    return;
  }
  glossaryUiState.loading = true;
  glossaryUiState.loadPromise = (async () => {
    body.innerHTML = `<div class="hist-empty"><div class="spinner" style="margin:0 auto"></div></div>`;
    try {
      const resp = await fetch('/api/glossary', { cache: 'no-store' });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || '加载术语库失败');
      applyGlossaryCatalog(data);
      glossaryUiState.loaded = true;
      renderGlossaryTab();
    } catch (error) {
      body.innerHTML = `
        <div class="hist-empty">
          <div style="display:flex;flex-direction:column;align-items:center;gap:12px;">
            <div>加载术语库失败：${esc(error.message)}</div>
            <button class="btn btn--ghost btn--sm" type="button" data-glossary-retry-load>重试</button>
          </div>
        </div>`;
    } finally {
      glossaryUiState.loading = false;
    }
  })();
  try {
    await glossaryUiState.loadPromise;
  } finally {
    glossaryUiState.loadPromise = null;
  }
}

function openGlossaryEditor(item = null) {
  if (item) {
    const languageRows = Object.entries(item.languages).map(([code, aliases]) => ({
      code,
      value: aliases.join(' | '),
    }));
    resetGlossaryEditor({
      open: true,
      mode: 'edit',
      targetId: item.id,
      category: item.category,
      ch: item.ch,
      note: item.note,
      enabled: item.enabled,
      languages: languageRows.length ? languageRows : [{ code: 'en', value: '' }],
    });
  } else {
    resetGlossaryEditor({
      open: true,
      category: glossaryUiState.filters.category !== 'all' ? glossaryUiState.filters.category : '',
    });
  }
  renderGlossaryTab();
}

function closeGlossaryEditor() {
  resetGlossaryEditor();
  renderGlossaryTab();
}

function closeGlossaryImportPanel() {
  resetGlossaryImporter();
  renderGlossaryTab();
}

function collectGlossaryEditorPayload() {
  const category = String(glossaryUiState.editor.category || '').trim();
  const ch = String(glossaryUiState.editor.ch || '').trim();
  if (!ch) {
    throw new Error('请填写标准中文名 ch');
  }

  const languages = {};
  glossaryUiState.editor.languages.forEach(row => {
    const code = String(row.code || '').trim().toLowerCase();
    const aliases = normalizeGlossaryAliases(row.value);
    if (!code && !aliases.length) return;
    if (!code) throw new Error('请为每一行填写语言代码');
    if (code === 'ch') throw new Error('ch 是标准中文列，请不要在其他语言里重复添加');
    if (!/^[a-z][a-z0-9_-]{0,15}$/i.test(code)) throw new Error(`语言代码格式不正确：${code}`);
    if (!aliases.length) throw new Error(`请填写 ${code} 的术语名称`);
    if (Object.prototype.hasOwnProperty.call(languages, code)) {
      throw new Error(`语言代码重复：${code}`);
    }
    languages[code] = aliases;
  });

  return {
    category,
    ch,
    note: String(glossaryUiState.editor.note || '').trim(),
    enabled: !!glossaryUiState.editor.enabled,
    terms_by_lang: languages,
  };
}

async function saveGlossaryEditor() {
  let payload;
  try {
    payload = collectGlossaryEditorPayload();
  } catch (error) {
    showToast(error.message, 'error');
    return;
  }

  glossaryUiState.editor.busy = true;
  renderGlossaryTab();
  try {
    const isEdit = glossaryUiState.editor.mode === 'edit';
    const revisionQuery = glossaryUiState.revision
      ? `?expected_revision=${encodeURIComponent(glossaryUiState.revision)}`
      : '';
    const url = isEdit
      ? `/api/glossary/${encodeURIComponent(glossaryUiState.editor.targetId)}${revisionQuery}`
      : `/api/glossary${revisionQuery}`;
    const resp = await fetch(url, {
      method: isEdit ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...payload,
        expected_revision: glossaryUiState.revision || undefined,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const error = new Error(data.detail || '保存术语失败');
      error.status = resp.status;
      throw error;
    }
    showToast(isEdit ? '术语已更新' : '术语已新增', 'success');
    resetGlossaryEditor();
    await loadGlossary(true);
  } catch (error) {
    glossaryUiState.editor.busy = false;
    if (error.status === 409) {
      showToast('术语库版本已变化，请刷新后重试', 'error');
      await loadGlossary(true);
      return;
    }
    renderGlossaryTab();
    showToast(error.message, 'error');
  }
}

async function updateGlossaryTerm(id, changes, successMessage) {
  const pendingId = String(id || '');
  if (glossaryPendingToggle(pendingId)) return;
  const isToggleOnly = Object.keys(changes || {}).length === 1 && Object.prototype.hasOwnProperty.call(changes, 'enabled');
  if (isToggleOnly) {
    glossaryUiState.pendingToggleIds.add(pendingId);
    renderGlossaryTab();
  }
  try {
    const revisionQuery = glossaryUiState.revision
      ? `?expected_revision=${encodeURIComponent(glossaryUiState.revision)}`
      : '';
    const resp = await fetch(`/api/glossary/${encodeURIComponent(id)}${revisionQuery}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...changes,
        expected_revision: glossaryUiState.revision || undefined,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const error = new Error(data.detail || '更新术语失败');
      error.status = resp.status;
      throw error;
    }
    if (data?.item) {
      applyGlossaryUpsert(data.item, data.revision);
      if (isToggleOnly) {
        glossaryUiState.pendingToggleIds.delete(pendingId);
        renderGlossaryTab();
      }
    } else {
      await loadGlossary(true);
    }
    showToast(successMessage, 'success');
  } catch (error) {
    if (isToggleOnly) {
      glossaryUiState.pendingToggleIds.delete(pendingId);
      renderGlossaryTab();
    }
    if (error.status === 409) {
      showToast('术语库版本已变化，请刷新后再试', 'error');
      await loadGlossary(true);
      return;
    }
    showToast(error.message, 'error');
  }
}

async function deleteGlossaryTerm(id) {
  try {
    const revisionQuery = glossaryUiState.revision
      ? `?expected_revision=${encodeURIComponent(glossaryUiState.revision)}`
      : '';
    const resp = await fetch(`/api/glossary/${encodeURIComponent(id)}${revisionQuery}`, {
      method: 'DELETE',
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const error = new Error(data.detail || '删除术语失败');
      error.status = resp.status;
      throw error;
    }
    showToast('术语已删除', 'success');
    await loadGlossary(true);
  } catch (error) {
    if (error.status === 409) {
      showToast('术语库版本已变化，请刷新后再试', 'error');
      await loadGlossary(true);
      return;
    }
    showToast(error.message, 'error');
  }
}

async function previewGlossaryImport(file) {
  if (!file) return;
  resetGlossaryImporter({
    file,
    busy: true,
    error: '',
  });
  renderGlossaryTab();
  try {
    const form = new FormData();
    form.append('file', file);
    const resp = await fetch('/api/glossary/import/preview', {
      method: 'POST',
      body: form,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || '导入预览失败');
    }
    glossaryUiState.importer = {
      file,
      preview: data.preview || data,
      fileHash: String(data.file_hash || ''),
      baseRevision: String(data.base_revision ?? glossaryUiState.revision ?? ''),
      busy: false,
      error: '',
    };
    renderGlossaryTab();
    showToast('已生成导入预览', 'success', 1800);
  } catch (error) {
    glossaryUiState.importer.busy = false;
    glossaryUiState.importer.error = error.message;
    glossaryUiState.importer.preview = null;
    renderGlossaryTab();
    showToast(error.message, 'error');
  }
}

async function commitGlossaryImport() {
  const { file, fileHash, baseRevision, preview } = glossaryUiState.importer;
  if (!file || !preview) {
    showToast('请先完成导入预览', 'error');
    return;
  }

  glossaryUiState.importer.busy = true;
  renderGlossaryTab();
  try {
    const form = new FormData();
    form.append('file', file);
    if (fileHash) form.append('file_hash', fileHash);
    form.append('base_revision', baseRevision || glossaryUiState.revision || '');
    const resp = await fetch('/api/glossary/import/commit', {
      method: 'POST',
      body: form,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const error = new Error(data.detail || '术语库导入失败');
      error.status = resp.status;
      throw error;
    }
    showToast('术语库已导入', 'success');
    resetGlossaryImporter();
    await loadGlossary(true);
  } catch (error) {
    glossaryUiState.importer.busy = false;
    if (error.status === 409) {
      glossaryUiState.importer.error = '文件或术语库版本已变化，请重新预览后再确认。';
      renderGlossaryTab();
      showToast(glossaryUiState.importer.error, 'error');
      return;
    }
    renderGlossaryTab();
    showToast(error.message, 'error');
  }
}

async function downloadGlossaryFile(url, fallbackName) {
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error('下载失败');
    const blob = await resp.blob();
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    const disposition = resp.headers.get('content-disposition') || '';
    const matchedName = disposition.match(/filename\*?=(?:UTF-8''|")?([^\";]+)/i);
    anchor.href = objectUrl;
    anchor.download = matchedName ? decodeURIComponent(matchedName[1].replace(/"/g, '')) : fallbackName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(objectUrl);
  } catch (error) {
    showToast(error.message, 'error');
  }
}

// ── 权限配置 ──────────────────────────────────────────────────

const PERMISSION_FEATURES = [
  { key: 'survey', label: '问卷分析', desc: '问卷上传、分析方案与报告生成' },
  { key: 'interview', label: '访谈报告', desc: '访谈记录上传与证据型报告生成' },
  { key: 'annotate', label: '数据标注', desc: '样本质量检测与数据标注' },
  { key: 'comment', label: '评论分析', desc: '评论主题、情感与舆情分析' },
];

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
  const newFeatureOptions = PERMISSION_FEATURES.map(feature => `
    <label class="perm-feature-option">
      <input type="checkbox" class="perm-toggle" data-perm-new="${esc(feature.key)}" checked />
      <span>
        <strong>${esc(feature.label)}</strong>
        <small>${esc(feature.desc)}</small>
      </span>
    </label>
  `).join('');

  const rows = users.map(u => {
    const isAdmin = u.is_admin;
    const userPerms = Array.isArray(u.perms) ? u.perms : [];
    const adminBadge = isAdmin ? `<span class="perm-badge">管理员</span>` : '';
    const featureOptions = PERMISSION_FEATURES.map(feature => {
      const checked = isAdmin || userPerms.includes(feature.key);
      return `
        <label class="perm-feature-option${isAdmin ? ' perm-feature-option--locked' : ''}">
          <input type="checkbox" class="perm-toggle" ${checked ? 'checked' : ''}
            ${isAdmin ? 'disabled' : ''}
            data-perm-email="${esc(u.email)}" data-perm-type="${esc(feature.key)}" />
          <span>
            <strong>${esc(feature.label)}</strong>
            <small>${esc(feature.desc)}</small>
          </span>
        </label>
      `;
    }).join('');
    const deleteBtn = isAdmin ? ''
      : `<button class="btn btn--ghost btn--sm" data-perm-delete="${esc(u.email)}">删除</button>`;
    const enabledToggle = isAdmin
      ? `<span class="perm-status perm-status--enabled">始终启用</span>`
      : `<label class="perm-enabled-toggle">
          <input type="checkbox" class="perm-toggle" ${u.enabled ? 'checked' : ''}
            data-perm-email="${esc(u.email)}" data-perm-type="enabled" />
          <span>${u.enabled ? '已启用' : '已停用'}</span>
        </label>`;

    return `
      <article class="perm-member-card" data-perm-user="${esc(u.email)}">
        <div class="perm-member-card__head">
          <div class="perm-member-card__identity">
            <strong>${esc(u.email)}</strong>
            ${adminBadge}
          </div>
          <div class="perm-member-card__actions">
            ${enabledToggle}
            ${deleteBtn}
          </div>
        </div>
        <div class="perm-feature-grid">${featureOptions}</div>
      </article>
    `;
  }).join('');

  body.innerHTML = `
    <div class="perm-panel">
      <div class="perm-panel__intro">
        <div>
          <h3>成员与功能权限</h3>
          <p>按功能模块为成员授权。后续新增模块时会继续在同一区域扩展。</p>
        </div>
      </div>
      <section class="perm-add-card" id="perm-add-row">
        <div class="perm-add-card__head">
          <label for="perm-new-email">添加成员</label>
          <input type="text" id="perm-new-email" class="plan-input"
            placeholder="飞书邮箱 或 Open ID（ou_xxxxx）" />
        </div>
        <div class="perm-feature-grid">${newFeatureOptions}</div>
        <div class="perm-add-card__actions">
          <button class="btn btn--primary btn--sm" id="perm-add-btn">添加成员</button>
        </div>
      </section>
      <div class="perm-member-list">
        <div class="perm-member-list__title">已有成员 <span>${esc(users.length)}</span></div>
        ${rows || '<div class="hist-empty">暂无成员</div>'}
      </div>
    </div>`;

  // 添加成员
  $('perm-add-btn').addEventListener('click', async () => {
    const email = ($('perm-new-email').value || '').trim();
    if (!email) { showToast('请输入邮箱或 Open ID', 'error'); return; }
    const perms = Array.from(body.querySelectorAll('[data-perm-new]:checked'))
      .map(el => el.dataset.permNew);
    if (!perms.length) {
      showToast('请至少选择一个功能权限', 'error');
      return;
    }
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
          if (checked) {
            const card = cb.closest('.perm-member-card');
            const hasPermission = card.querySelector('[data-perm-type]:checked:not([data-perm-type="enabled"])');
            if (!hasPermission) {
              showToast('启用成员至少需要一个功能权限', 'error');
              cb.checked = false;
              return;
            }
          }
          patch = { enabled: checked };
        } else {
          const card = cb.closest('.perm-member-card');
          const perms = Array.from(card.querySelectorAll('[data-perm-type]:checked'))
            .map(el => el.dataset.permType)
            .filter(key => key !== 'enabled');
          const enabledEl = card.querySelector('[data-perm-type="enabled"]');
          if (enabledEl?.checked && !perms.length) {
            showToast('启用成员至少需要一个功能权限；如需暂停访问，请关闭“已启用”', 'error');
            cb.checked = true;
            return;
          }
          patch = { perms };
        }
        const r = await fetch(`/api/admin/users/${encodeURIComponent(email)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch)
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || '更新失败');
        if (type === 'enabled') {
          const label = cb.closest('.perm-enabled-toggle')?.querySelector('span');
          if (label) label.textContent = checked ? '已启用' : '已停用';
        }
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

const PROMPT_KIND_LABELS = {
  system: 'System Prompt',
  instruction: '业务指令',
};
const PROMPT_DRAFT_STORAGE_KEY = 'settings-prompt-drafts-v2';
const PROMPT_GROUP_STORAGE_KEY = 'settings-prompt-groups-v1';
let promptStorageWarningShown = false;
const promptUiState = {
  loaded: false,
  loading: false,
  loadPromise: null,
  search: '',
  promptsByKey: {},
  drafts: loadPromptDraftState(),
  collapsedGroups: loadPromptGroupState(),
};
const promptEditorState = {
  key: null,
  scrollTop: 0,
};

function loadPromptDraftState() {
  try {
    const raw = localStorage.getItem(PROMPT_DRAFT_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function savePromptDraftState() {
  try {
    localStorage.setItem(PROMPT_DRAFT_STORAGE_KEY, JSON.stringify(promptUiState.drafts));
  } catch {
    notifyPromptStorageUnavailable();
  }
}

function loadPromptGroupState() {
  try {
    const raw = localStorage.getItem(PROMPT_GROUP_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function savePromptGroupState() {
  try {
    localStorage.setItem(PROMPT_GROUP_STORAGE_KEY, JSON.stringify(promptUiState.collapsedGroups));
  } catch {
    notifyPromptStorageUnavailable();
  }
}

function notifyPromptStorageUnavailable() {
  if (promptStorageWarningShown) return;
  promptStorageWarningShown = true;
  showToast('浏览器无法持久保存本页状态；当前页面内仍可继续编辑', 'info');
}

function toPromptOrderValue(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num : Number.POSITIVE_INFINITY;
}

function normalizePromptGroupName(value) {
  return String(value || '').trim() || '其他';
}

function normalizePromptKind(value, editable) {
  const key = String(value || '').trim();
  if (PROMPT_KIND_LABELS[key]) return PROMPT_KIND_LABELS[key];
  if (key) return key;
  return editable ? '业务 Prompt' : '只读';
}

function normalizePromptRecord(prompt) {
  return {
    ...prompt,
    key: String(prompt.key || ''),
    label: String(prompt.label || prompt.key || '未命名提示词'),
    description: String(prompt.description || ''),
    current: String(prompt.current || ''),
    revision: String(prompt.revision || ''),
    history: Array.isArray(prompt.history) ? prompt.history : [],
    editable: !!prompt.editable,
    group: normalizePromptGroupName(prompt.group),
    group_order: toPromptOrderValue(prompt.group_order),
    order: toPromptOrderValue(prompt.order),
    kindLabel: normalizePromptKind(prompt.kind, !!prompt.editable),
  };
}

function getPromptDraft(key) {
  return promptUiState.drafts[key] || {};
}

function getPromptViewModel(prompt) {
  const draft = getPromptDraft(prompt.key);
  return {
    ...prompt,
    draftContent: Object.prototype.hasOwnProperty.call(draft, 'content') ? draft.content : prompt.current,
    draftNote: Object.prototype.hasOwnProperty.call(draft, 'note') ? draft.note : '',
    draftBaseRevision: String(draft.baseRevision || prompt.revision || ''),
    draftConflict: !!draft.conflict,
  };
}

function hasPromptContentChange(prompt, content) {
  return content !== prompt.current;
}

function hasDirtyPromptDrafts() {
  return Object.values(promptUiState.promptsByKey).some(prompt => {
    const draft = getPromptDraft(prompt.key);
    const content = Object.prototype.hasOwnProperty.call(draft, 'content') ? draft.content : prompt.current;
    return hasPromptContentChange(prompt, content);
  });
}

function prunePromptDrafts() {
  let changed = false;
  Object.entries(promptUiState.drafts).forEach(([key, draft]) => {
    const prompt = promptUiState.promptsByKey[key];
    if (!prompt) {
      delete promptUiState.drafts[key];
      changed = true;
      return;
    }
    const content = Object.prototype.hasOwnProperty.call(draft, 'content') ? draft.content : prompt.current;
    const note = Object.prototype.hasOwnProperty.call(draft, 'note') ? String(draft.note) : '';
    if (!hasPromptContentChange(prompt, content) && !note) {
      delete promptUiState.drafts[key];
      changed = true;
    }
  });
  if (changed) savePromptDraftState();
}

function setPromptDraft(key, patch) {
  const prompt = promptUiState.promptsByKey[key];
  if (!prompt) return;
  const next = {
    baseRevision: prompt.revision,
    ...getPromptDraft(key),
    ...patch,
  };
  const content = Object.prototype.hasOwnProperty.call(next, 'content') ? next.content : prompt.current;
  const note = Object.prototype.hasOwnProperty.call(next, 'note') ? String(next.note) : '';
  if (!hasPromptContentChange(prompt, content) && !note) {
    delete promptUiState.drafts[key];
  } else {
    promptUiState.drafts[key] = next;
  }
  savePromptDraftState();
}

function clearPromptDraft(key) {
  if (!Object.prototype.hasOwnProperty.call(promptUiState.drafts, key)) return;
  delete promptUiState.drafts[key];
  savePromptDraftState();
}

function promptMatchesSearch(prompt, keyword) {
  if (!keyword) return true;
  const haystack = [
    prompt.label,
    prompt.description,
    prompt.group,
    prompt.kindLabel,
    prompt.key,
  ].join('\n').toLowerCase();
  return haystack.includes(keyword);
}

function getPromptGroups() {
  const keyword = promptUiState.search.trim().toLowerCase();
  const prompts = Object.values(promptUiState.promptsByKey)
    .filter(prompt => promptMatchesSearch(prompt, keyword))
    .sort((a, b) => (
      a.group_order - b.group_order
      || a.group.localeCompare(b.group, 'zh-CN')
      || a.order - b.order
      || a.label.localeCompare(b.label, 'zh-CN')
      || a.key.localeCompare(b.key, 'zh-CN')
    ));

  const groups = [];
  const groupMap = new Map();
  prompts.forEach(prompt => {
    const groupName = prompt.group || '其他';
    let group = groupMap.get(groupName);
    if (!group) {
      group = {
        name: groupName,
        order: prompt.group_order,
        prompts: [],
      };
      groups.push(group);
      groupMap.set(groupName, group);
    }
    group.prompts.push(prompt);
  });
  return groups.sort((a, b) => (
    a.order - b.order || a.name.localeCompare(b.name, 'zh-CN')
  ));
}

function renderPromptKindPills(prompts) {
  const counts = new Map();
  prompts.forEach(prompt => {
    counts.set(prompt.kindLabel, (counts.get(prompt.kindLabel) || 0) + 1);
  });
  return Array.from(counts.entries()).map(([label, count]) => (
    `<span class="prompt-kind-pill">${esc(label)}<em>${count}</em></span>`
  )).join('');
}

function promptConflictNoticeHTML(view) {
  if (!view.draftConflict) return '';
  return `
    <div class="prompt-card__conflict-notice">
      <div>
        <strong>服务端版本已更新，本地草稿尚未保存。</strong>
        <span>请先对照最新版；确认仍要保留当前草稿时，再基于最新版继续。</span>
      </div>
      <details>
        <summary>查看服务端最新版</summary>
        <pre>${esc(view.current)}</pre>
      </details>
      <button class="btn btn--ghost btn--sm" type="button" data-rebase-prompt-draft="${esc(view.key)}">基于最新版继续</button>
    </div>
  `;
}

function promptCardHTML(prompt) {
  const view = getPromptViewModel(prompt);
  const readonly = !view.editable;
  const dirty = hasPromptContentChange(view, view.draftContent);
  const saveDisabled = readonly || !dirty || view.draftConflict;
  const noteText = String(view.draftNote || '');
  const hasDraft = Object.prototype.hasOwnProperty.call(promptUiState.drafts, view.key);
  const history = view.history.length ? `
    <div class="prompt-history">
      <button class="prompt-history__toggle" type="button" data-hist-toggle="${esc(view.key)}" aria-expanded="false">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
        修改历史（${view.history.length}）
      </button>
      <div class="prompt-history__list" data-hist-list="${esc(view.key)}">
        ${view.history.map(item => `
          <div class="history-item">
            <div class="history-item__meta">
              <span class="history-item__ts">${esc(item.ts)}</span>
              <span class="history-item__note">${esc(item.note || '')}</span>
            </div>
            <div class="history-item__preview" title="${esc(item.content || '')}">${esc((item.content || '').slice(0, 120))}</div>
          </div>
        `).join('')}
      </div>
    </div>
  ` : '';

  return `
    <article class="prompt-card ${readonly ? 'prompt-card--readonly' : ''} ${dirty ? 'prompt-card--dirty' : ''} ${view.draftConflict ? 'prompt-card--conflict' : ''}" data-prompt-card="${esc(view.key)}">
      <div class="prompt-card__header">
        <div class="prompt-card__title-wrap">
          <div class="prompt-card__title-row">
            <h3 class="prompt-card__title">${esc(view.label)}</h3>
            ${dirty ? `<span class="prompt-card__state">${view.draftConflict ? '版本冲突' : '未保存'}</span>` : ''}
          </div>
          <div class="prompt-card__meta">
            <span class="prompt-card__kind">${esc(view.kindLabel)}</span>
            <span class="prompt-card__key">${esc(view.key)}</span>
          </div>
        </div>
        <button class="btn btn--ghost btn--sm prompt-card__expand" type="button" data-open-prompt-editor="${esc(view.key)}">放大编辑</button>
      </div>
      <p class="prompt-card__desc">${esc(view.description || '未提供说明')}</p>
      ${readonly ? '<div class="prompt-card__readonly-note">当前项为只读配置，可查看内容，但不能在此页直接保存。</div>' : ''}
      ${promptConflictNoticeHTML(view)}
      <textarea class="prompt-textarea" data-content="${esc(view.key)}" ${readonly ? 'readonly' : ''}>${esc(view.draftContent)}</textarea>
      ${readonly ? '' : `
        <div class="prompt-card__actions">
          <input class="prompt-note-input" data-note="${esc(view.key)}" placeholder="本次修改说明（可选）" value="${esc(noteText)}" />
          <button class="btn btn--ghost" type="button" data-discard-prompt-draft="${esc(view.key)}" ${hasDraft ? '' : 'disabled'}>放弃草稿</button>
          <button class="btn btn--primary" type="button" data-save="${esc(view.key)}" ${saveDisabled ? 'disabled' : ''}>保存</button>
        </div>
      `}
      ${history}
    </article>
  `;
}

function promptEditorHTML(prompt) {
  const view = getPromptViewModel(prompt);
  const readonly = !view.editable;
  const dirty = hasPromptContentChange(view, view.draftContent);
  const saveDisabled = readonly || !dirty || view.draftConflict;
  const noteText = String(view.draftNote || '');
  const hasDraft = Object.prototype.hasOwnProperty.call(promptUiState.drafts, view.key);

  return `
    <div class="prompt-editor-sheet ${readonly ? 'prompt-editor-sheet--readonly' : ''}" role="dialog" aria-modal="true" aria-labelledby="prompt-editor-title">
      <div class="prompt-editor-sheet__header">
        <div class="prompt-editor-sheet__title-wrap">
          <div class="prompt-editor-sheet__eyebrow">提示词编辑</div>
          <div class="prompt-card__title-row">
            <h3 class="prompt-editor-sheet__title" id="prompt-editor-title">${esc(view.label)}</h3>
            ${dirty ? `<span class="prompt-card__state">${view.draftConflict ? '版本冲突' : '未保存'}</span>` : ''}
          </div>
          <div class="prompt-card__meta">
            <span class="prompt-card__kind">${esc(view.kindLabel)}</span>
            <span class="prompt-card__key">${esc(view.key)}</span>
          </div>
        </div>
        <button class="drawer__close prompt-editor-sheet__close" type="button" data-close-prompt-editor aria-label="关闭">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>
      <p class="prompt-card__desc">${esc(view.description || '未提供说明')}</p>
      ${readonly ? '<div class="prompt-card__readonly-note">当前项为只读配置，可查看内容，但不能在此页直接保存。</div>' : ''}
      ${promptConflictNoticeHTML(view)}
      <div class="prompt-editor-sheet__body">
        <textarea class="prompt-textarea prompt-textarea--expanded" data-content="${esc(view.key)}" ${readonly ? 'readonly' : ''}>${esc(view.draftContent)}</textarea>
      </div>
      <div class="prompt-editor-sheet__footer">
        ${readonly ? `
          <div class="prompt-editor-sheet__footer-actions">
            <button class="btn btn--ghost" type="button" data-close-prompt-editor>关闭</button>
          </div>
        ` : `
          <input class="prompt-note-input prompt-note-input--expanded" data-note="${esc(view.key)}" placeholder="本次修改说明（可选）" value="${esc(noteText)}" />
          <div class="prompt-editor-sheet__footer-actions">
            <button class="btn btn--ghost" type="button" data-discard-prompt-draft="${esc(view.key)}" ${hasDraft ? '' : 'disabled'}>放弃草稿</button>
            <button class="btn btn--primary" type="button" data-save="${esc(view.key)}" ${saveDisabled ? 'disabled' : ''}>保存</button>
          </div>
        `}
      </div>
    </div>
  `;
}

function openPromptEditor(key) {
  if (!promptUiState.promptsByKey[key]) return;
  promptEditorState.scrollTop = $('settings-content')?.scrollTop || 0;
  promptEditorState.key = key;
  renderPromptEditor({ focusEditor: true });
}

function closePromptEditor({ rerender = false } = {}) {
  if (!promptEditorState.key) return;
  const closedKey = promptEditorState.key;
  const scrollTop = promptEditorState.scrollTop;
  promptEditorState.key = null;
  promptEditorState.scrollTop = 0;
  renderPromptEditor();
  if (rerender) {
    rerenderPromptsPreservingSearch();
  }
  requestAnimationFrame(() => {
    const settingsContent = $('settings-content');
    if (settingsContent) settingsContent.scrollTop = scrollTop;
    if (rerender) {
      $('stab-content-prompts')
        ?.querySelector(`[data-open-prompt-editor="${closedKey}"]`)
        ?.focus();
    }
  });
}

function setPromptEditorBackgroundInert(isOpen) {
  const drawer = $('settings-drawer');
  const header = drawer?.querySelector('.drawer__header');
  const body = $('settings-body');
  if (header) header.inert = isOpen;
  if (body) body.inert = isOpen;
}

function getPromptEditorRestoreSelector(active, layer) {
  if (!active || !layer.contains(active)) return '';
  if (active.matches('[data-content]')) return '[data-content]';
  if (active.matches('[data-note]')) return '[data-note]';
  if (active.matches('[data-save]')) return '[data-save]';
  if (active.matches('[data-discard-prompt-draft]')) return '[data-discard-prompt-draft]';
  if (active.matches('[data-rebase-prompt-draft]')) return '[data-rebase-prompt-draft]';
  if (active.matches('[data-close-prompt-editor]')) return '[data-close-prompt-editor]';
  return '';
}

function renderPromptEditor({ focusEditor = false } = {}) {
  const layer = $('prompt-editor-layer');
  if (!layer) return;
  const restoreSelector = getPromptEditorRestoreSelector(document.activeElement, layer);
  const prompt = promptEditorState.key ? promptUiState.promptsByKey[promptEditorState.key] : null;
  if (!prompt) {
    layer.hidden = true;
    layer.innerHTML = '';
    $('settings-content')?.classList.remove('settings-content--editor-open');
    setPromptEditorBackgroundInert(false);
    return;
  }
  layer.hidden = false;
  layer.innerHTML = promptEditorHTML(prompt);
  $('settings-content')?.classList.add('settings-content--editor-open');
  setPromptEditorBackgroundInert(true);
  if (focusEditor || restoreSelector) {
    requestAnimationFrame(() => {
      const preferred = restoreSelector ? layer.querySelector(restoreSelector) : null;
      const target = preferred && !preferred.disabled
        ? preferred
        : (layer.querySelector('[data-content]') || layer.querySelector('[data-close-prompt-editor]'));
      target?.focus();
    });
  }
}

function renderPrompts() {
  const body = $('stab-content-prompts');
  if (!body) return;
  const groups = getPromptGroups();
  const visibleCount = groups.reduce((sum, group) => sum + group.prompts.length, 0);
  const draftCount = Object.keys(promptUiState.drafts).length;

  body.innerHTML = `
    <section class="prompt-settings">
      <header class="prompt-settings__header">
        <div>
          <h2 class="prompt-settings__title">提示词管理</h2>
          <p class="prompt-settings__desc">这里管理直连 LLM 流程中稳定复用的业务 Prompt / System Prompt，不包含运行时动态拼装的 schema、协议约束或临时上下文。</p>
        </div>
        <div class="prompt-settings__summary">
          <span>当前显示 ${visibleCount} 项</span>
          <span>${draftCount ? `已保留 ${draftCount} 份草稿` : '暂无未保存草稿'}</span>
        </div>
      </header>
      <div class="prompt-toolbar">
        <label class="prompt-search" for="prompt-search-input">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="11" cy="11" r="7"></circle>
            <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
          </svg>
          <input id="prompt-search-input" type="search" placeholder="搜索提示词名称、分组、说明或 key" value="${esc(promptUiState.search)}" />
        </label>
        ${draftCount ? '<div class="prompt-toolbar__hint">切换标签或刷新页面后，草稿会继续保留。</div>' : ''}
      </div>
      <div class="prompt-group-list">
        ${groups.length ? groups.map(group => {
          const collapsed = !!promptUiState.collapsedGroups[group.name];
          return `
            <section class="prompt-group ${collapsed ? 'prompt-group--collapsed' : ''}" data-prompt-group="${esc(group.name)}">
              <button class="prompt-group__header" type="button" data-prompt-group-toggle="${esc(group.name)}" aria-expanded="${collapsed ? 'false' : 'true'}">
                <div class="prompt-group__header-main">
                  <svg class="prompt-group__chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="6 9 12 15 18 9"></polyline>
                  </svg>
                  <div>
                    <div class="prompt-group__title-row">
                      <h3 class="prompt-group__title">${esc(group.name)}</h3>
                      <span class="prompt-group__count">${group.prompts.length} 项</span>
                    </div>
                    <div class="prompt-group__kinds">${renderPromptKindPills(group.prompts)}</div>
                  </div>
                </div>
              </button>
              <div class="prompt-group__body">
                <div class="prompt-card-list">
                  ${group.prompts.map(promptCardHTML).join('')}
                </div>
              </div>
            </section>
          `;
        }).join('') : '<div class="hist-empty">没有匹配的提示词，请调整搜索条件。</div>'}
      </div>
    </section>
  `;
  renderPromptEditor();
}

function rerenderPromptsPreservingSearch() {
  const searchInput = $('stab-content-prompts')?.querySelector('#prompt-search-input');
  const hadFocus = document.activeElement === searchInput;
  const selectionStart = hadFocus ? searchInput.selectionStart : null;
  const selectionEnd = hadFocus ? searchInput.selectionEnd : null;
  renderPrompts();
  if (!hadFocus) return;
  const nextSearchInput = $('stab-content-prompts')?.querySelector('#prompt-search-input');
  if (!nextSearchInput) return;
  nextSearchInput.focus();
  if (selectionStart !== null && selectionEnd !== null) {
    nextSearchInput.setSelectionRange(selectionStart, selectionEnd);
  }
}

function updatePromptToolbarHint() {
  const toolbar = $('stab-content-prompts')?.querySelector('.prompt-toolbar');
  if (!toolbar) return;
  let hint = toolbar.querySelector('.prompt-toolbar__hint');
  const draftCount = Object.keys(promptUiState.drafts).length;
  if (!draftCount) {
    hint?.remove();
    return;
  }
  if (!hint) {
    hint = document.createElement('div');
    hint.className = 'prompt-toolbar__hint';
    toolbar.appendChild(hint);
  }
  hint.textContent = '切换标签或刷新页面后，草稿会继续保留。';
}

function updatePromptSummary() {
  const summary = $('stab-content-prompts')?.querySelector('.prompt-settings__summary');
  if (!summary) return;
  const spans = summary.querySelectorAll('span');
  if (spans[0]) {
    const groups = getPromptGroups();
    const visibleCount = groups.reduce((sum, group) => sum + group.prompts.length, 0);
    spans[0].textContent = `当前显示 ${visibleCount} 项`;
  }
  if (spans[1]) {
    const draftCount = Object.keys(promptUiState.drafts).length;
    spans[1].textContent = draftCount ? `已保留 ${draftCount} 份草稿` : '暂无未保存草稿';
  }
  updatePromptToolbarHint();
}

function syncPromptStateBadge(container, view, dirty) {
  if (!container) return;
  const titleRow = container.querySelector('.prompt-card__title-row');
  let stateBadge = container.querySelector('.prompt-card__state');
  if (dirty) {
    if (!stateBadge && titleRow) {
      stateBadge = document.createElement('span');
      stateBadge.className = 'prompt-card__state';
      titleRow.appendChild(stateBadge);
    }
    if (stateBadge) stateBadge.textContent = view.draftConflict ? '版本冲突' : '未保存';
  } else {
    stateBadge?.remove();
  }
}

function syncPromptEditorDirtyState(key, view, dirty) {
  if (promptEditorState.key !== key) return;
  const layer = $('prompt-editor-layer');
  if (!layer || layer.hidden) return;
  syncPromptStateBadge(layer, view, dirty);
  const saveBtn = layer.querySelector('[data-save]');
  if (saveBtn) saveBtn.disabled = !dirty || view.draftConflict;
  const discardBtn = layer.querySelector('[data-discard-prompt-draft]');
  if (discardBtn) {
    discardBtn.disabled = !Object.prototype.hasOwnProperty.call(promptUiState.drafts, key);
  }
}

function syncPromptCardDirtyState(key) {
  const prompt = promptUiState.promptsByKey[key];
  const card = $('stab-content-prompts')?.querySelector(`[data-prompt-card="${key}"]`);
  if (!prompt) return;
  const view = getPromptViewModel(prompt);
  const dirty = hasPromptContentChange(view, view.draftContent);
  if (card) {
    card.classList.toggle('prompt-card--dirty', dirty);
    syncPromptStateBadge(card, view, dirty);
    const saveBtn = card.querySelector('[data-save]');
    if (saveBtn) saveBtn.disabled = !dirty || view.draftConflict;
    const discardBtn = card.querySelector('[data-discard-prompt-draft]');
    if (discardBtn) {
      discardBtn.disabled = !Object.prototype.hasOwnProperty.call(promptUiState.drafts, key);
    }
  }
  syncPromptEditorDirtyState(key, view, dirty);
  updatePromptSummary();
}

async function loadPrompts(force = false) {
  const body = $('stab-content-prompts');
  if (!body) return;
  if (promptUiState.loaded && !force) {
    renderPrompts();
    return;
  }
  if (promptUiState.loading) {
    const pendingLoad = promptUiState.loadPromise;
    if (pendingLoad) await pendingLoad;
    if (force) return loadPrompts(true);
    if (promptUiState.loaded) renderPrompts();
    return;
  }
  promptUiState.loading = true;
  const loadPromise = (async () => {
    body.innerHTML = `<div class="hist-empty"><div class="spinner" style="margin:0 auto"></div></div>`;
    try {
      const resp = await fetch('/api/prompts');
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '加载失败');
      promptUiState.promptsByKey = Object.fromEntries(
        Object.values(data).map(item => {
          const prompt = normalizePromptRecord(item);
          return [prompt.key, prompt];
        })
      );
      promptUiState.loaded = true;
      prunePromptDrafts();
      renderPrompts();
    } catch (e) {
      body.innerHTML = `<div class="hist-empty">加载提示词失败：${esc(e.message)}</div>`;
    } finally {
      promptUiState.loading = false;
    }
  })();
  promptUiState.loadPromise = loadPromise;
  try {
    await loadPromise;
  } finally {
    if (promptUiState.loadPromise === loadPromise) {
      promptUiState.loadPromise = null;
    }
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

window.addEventListener('beforeunload', event => {
  if (!hasDirtyPromptDrafts()) return;
  event.preventDefault();
  event.returnValue = '';
});

$('settings-drawer').addEventListener('input', e => {
  const glossarySearch = e.target.closest('#glossary-search-input');
  if (glossarySearch) {
    glossaryUiState.filters.search = glossarySearch.value || '';
    if (e.isComposing) return;
    const { selectionStart, selectionEnd } = glossarySearch;
    renderGlossaryTab();
    requestAnimationFrame(() => {
      const nextInput = $('glossary-search-input');
      if (!nextInput) return;
      nextInput.focus();
      if (selectionStart !== null && selectionEnd !== null) {
        nextInput.setSelectionRange(selectionStart, selectionEnd);
      }
    });
    return;
  }

  const glossaryCategoryInput = e.target.closest('#glossary-editor-category');
  if (glossaryCategoryInput) {
    glossaryUiState.editor.category = glossaryCategoryInput.value || '';
    return;
  }

  const glossaryChInput = e.target.closest('#glossary-editor-ch');
  if (glossaryChInput) {
    glossaryUiState.editor.ch = glossaryChInput.value || '';
    return;
  }

  const glossaryNoteInput = e.target.closest('#glossary-editor-note');
  if (glossaryNoteInput) {
    glossaryUiState.editor.note = glossaryNoteInput.value || '';
    return;
  }

  const glossaryEnabledInput = e.target.closest('#glossary-editor-enabled');
  if (glossaryEnabledInput) {
    glossaryUiState.editor.enabled = glossaryEnabledInput.checked;
    return;
  }

  const glossaryLangCode = e.target.closest('[data-glossary-lang-code]');
  if (glossaryLangCode) {
    const index = Number(glossaryLangCode.dataset.glossaryLangCode);
    if (glossaryUiState.editor.languages[index]) {
      glossaryUiState.editor.languages[index].code = glossaryLangCode.value || '';
    }
    return;
  }

  const glossaryLangValue = e.target.closest('[data-glossary-lang-value]');
  if (glossaryLangValue) {
    const index = Number(glossaryLangValue.dataset.glossaryLangValue);
    if (glossaryUiState.editor.languages[index]) {
      glossaryUiState.editor.languages[index].value = glossaryLangValue.value || '';
    }
    return;
  }

  const searchInput = e.target.closest('#prompt-search-input');
  if (searchInput) {
    promptUiState.search = searchInput.value || '';
    rerenderPromptsPreservingSearch();
    return;
  }

  const textarea = e.target.closest('[data-content]');
  if (textarea) {
    const key = textarea.dataset.content;
    setPromptDraft(key, { content: textarea.value });
    syncPromptCardDirtyState(key);
    return;
  }

  const noteInput = e.target.closest('[data-note]');
  if (noteInput) {
    const key = noteInput.dataset.note;
    setPromptDraft(key, { note: noteInput.value });
    syncPromptCardDirtyState(key);
  }
});

$('settings-drawer').addEventListener('click', async e => {
  const glossaryRetryLoadBtn = e.target.closest('[data-glossary-retry-load]');
  if (glossaryRetryLoadBtn) {
    await loadGlossary(true);
    return;
  }

  const glossaryCreateBtn = e.target.closest('[data-glossary-create]');
  if (glossaryCreateBtn) {
    openGlossaryEditor();
    return;
  }

  const glossaryCancelEditorBtn = e.target.closest('[data-glossary-cancel-editor]');
  if (glossaryCancelEditorBtn) {
    closeGlossaryEditor();
    return;
  }

  const glossaryAddLangBtn = e.target.closest('[data-glossary-add-lang]');
  if (glossaryAddLangBtn) {
    glossaryUiState.editor.languages.push({ code: '', value: '' });
    renderGlossaryTab();
    return;
  }

  const glossaryRemoveLangBtn = e.target.closest('[data-glossary-remove-lang]');
  if (glossaryRemoveLangBtn) {
    const index = Number(glossaryRemoveLangBtn.dataset.glossaryRemoveLang);
    glossaryUiState.editor.languages.splice(index, 1);
    if (!glossaryUiState.editor.languages.length) {
      glossaryUiState.editor.languages.push({ code: '', value: '' });
    }
    renderGlossaryTab();
    return;
  }

  const glossarySaveBtn = e.target.closest('[data-glossary-save]');
  if (glossarySaveBtn) {
    await saveGlossaryEditor();
    return;
  }

  const glossaryEditBtn = e.target.closest('[data-glossary-edit]');
  if (glossaryEditBtn) {
    const item = glossaryUiState.items.find(entry => entry.id === glossaryEditBtn.dataset.glossaryEdit);
    if (item) openGlossaryEditor(item);
    return;
  }

  const glossaryDeleteBtn = e.target.closest('[data-glossary-delete]');
  if (glossaryDeleteBtn) {
    const item = glossaryUiState.items.find(entry => entry.id === glossaryDeleteBtn.dataset.glossaryDelete);
    if (!item) return;
    if (window.confirm(`确认删除术语“${item.ch}”？`)) {
      await deleteGlossaryTerm(item.id);
    }
    return;
  }

  const glossaryToggleBtn = e.target.closest('[data-glossary-toggle]');
  if (glossaryToggleBtn) {
    const item = glossaryUiState.items.find(entry => entry.id === glossaryToggleBtn.dataset.glossaryToggle);
    if (!item) return;
    await updateGlossaryTerm(item.id, { enabled: !item.enabled }, item.enabled ? '术语已停用' : '术语已启用');
    return;
  }

  const glossaryTemplateBtn = e.target.closest('[data-glossary-template]');
  if (glossaryTemplateBtn) {
    await downloadGlossaryFile('/api/glossary/template', 'glossary-template.xlsx');
    return;
  }

  const glossaryExportBtn = e.target.closest('[data-glossary-export]');
  if (glossaryExportBtn) {
    await downloadGlossaryFile('/api/glossary/export', 'glossary-export.xlsx');
    return;
  }

  const glossaryImportBtn = e.target.closest('[data-glossary-import]');
  if (glossaryImportBtn) {
    const input = $(GLOSSARY_IMPORT_INPUT_ID);
    if (!input) return;
    input.value = '';
    input.click();
    return;
  }

  const glossaryCancelImportBtn = e.target.closest('[data-glossary-cancel-import]');
  if (glossaryCancelImportBtn) {
    closeGlossaryImportPanel();
    return;
  }

  const glossaryRepreviewBtn = e.target.closest('[data-glossary-repreview]');
  if (glossaryRepreviewBtn) {
    if (glossaryUiState.importer.file) {
      await previewGlossaryImport(glossaryUiState.importer.file);
    }
    return;
  }

  const glossaryCommitBtn = e.target.closest('[data-glossary-commit]');
  if (glossaryCommitBtn) {
    await commitGlossaryImport();
    return;
  }

  const openBtn = e.target.closest('[data-open-prompt-editor]');
  if (openBtn) {
    openPromptEditor(openBtn.dataset.openPromptEditor);
    return;
  }

  const closeBtn = e.target.closest('[data-close-prompt-editor]');
  if (closeBtn) {
    closePromptEditor({ rerender: true });
    return;
  }

  const rebaseBtn = e.target.closest('[data-rebase-prompt-draft]');
  if (rebaseBtn) {
    const key = rebaseBtn.dataset.rebasePromptDraft;
    const prompt = promptUiState.promptsByKey[key];
    const draft = promptUiState.drafts[key];
    if (prompt && draft) {
      promptUiState.drafts[key] = {
        ...draft,
        baseRevision: prompt.revision,
        conflict: false,
      };
      savePromptDraftState();
      renderPrompts();
      showToast('草稿已基于服务端最新版重新校验，请确认内容后保存', 'info');
    }
    return;
  }

  const discardBtn = e.target.closest('[data-discard-prompt-draft]');
  if (discardBtn) {
    const key = discardBtn.dataset.discardPromptDraft;
    if (window.confirm('放弃这项提示词的本地草稿并加载服务端最新版本？')) {
      clearPromptDraft(key);
      await loadPrompts(true);
    }
    return;
  }

  const groupToggle = e.target.closest('[data-prompt-group-toggle]');
  if (groupToggle) {
    const group = groupToggle.dataset.promptGroupToggle;
    promptUiState.collapsedGroups[group] = !promptUiState.collapsedGroups[group];
    savePromptGroupState();
    renderPrompts();
    return;
  }

  const toggle = e.target.closest('[data-hist-toggle]');
  if (toggle) {
    const key = toggle.dataset.histToggle;
    const list = document.querySelector(`[data-hist-list="${key}"]`);
    const opened = list?.classList.toggle('open');
    toggle.setAttribute('aria-expanded', opened ? 'true' : 'false');
    return;
  }

  const saveBtn = e.target.closest('[data-save]');
  if (saveBtn) {
    const key = saveBtn.dataset.save;
    const prompt = promptUiState.promptsByKey[key];
    if (!prompt) return;
    const view = getPromptViewModel(prompt);
    const content = view.draftContent;
    const note = view.draftNote;
    saveBtn.disabled = true;
    saveBtn.textContent = '保存中…';
    try {
      const revisionQuery = view.draftBaseRevision
        ? `?expected_revision=${encodeURIComponent(view.draftBaseRevision)}`
        : '';
      const resp = await fetch(`/api/prompts/${key}${revisionQuery}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, note }),
      });
      const d = await resp.json();
      if (!resp.ok) {
        const error = new Error(d.detail || '保存失败');
        error.status = resp.status;
        throw error;
      }
      clearPromptDraft(key);
      showToast('提示词已保存，下次分析生效', 'success');
      await loadPrompts(true);
    } catch (err) {
      if (err.status === 409 && promptUiState.drafts[key]) {
        await loadPrompts(true);
        if (promptUiState.promptsByKey[key] && promptUiState.drafts[key]) {
          promptUiState.drafts[key] = {
            ...promptUiState.drafts[key],
            conflict: true,
          };
          savePromptDraftState();
          renderPrompts();
        }
      }
      showToast(`保存失败：${err.message}`, 'error');
      saveBtn.disabled = false;
      saveBtn.textContent = '保存';
    }
  }
});

$('settings-drawer').addEventListener('change', async e => {
  const glossaryCategoryFilter = e.target.closest('#glossary-category-filter');
  if (glossaryCategoryFilter) {
    glossaryUiState.filters.category = glossaryCategoryFilter.value || 'all';
    renderGlossaryTab();
    return;
  }

  const glossaryStatusFilter = e.target.closest('#glossary-status-filter');
  if (glossaryStatusFilter) {
    glossaryUiState.filters.status = glossaryStatusFilter.value || 'all';
    renderGlossaryTab();
    return;
  }

  const glossaryImportInput = e.target.closest(`#${GLOSSARY_IMPORT_INPUT_ID}`);
  if (glossaryImportInput?.files?.[0]) {
    await previewGlossaryImport(glossaryImportInput.files[0]);
  }
});
