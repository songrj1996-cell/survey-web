// ============================================================
// Drawer 通用控制
// ============================================================

function openDrawer(id) { $(id).classList.add('drawer--open'); }
async function closeDrawer(id) {
  if (id === 'settings-drawer' && window.membersHasDraft?.() && !await window.membersMayLeave()) return;
  $(id).classList.remove('drawer--open');
  if (id === 'settings-drawer') closePromptEditor();
}

document.querySelectorAll('[data-drawer-close]').forEach(el => {
  el.addEventListener('click', async e => {
    const drawer = e.target.closest('.drawer');
    if (drawer) {
      if (drawer.id === 'settings-drawer' && window.membersHasDraft?.() && !await window.membersMayLeave()) return;
      drawer.classList.remove('drawer--open');
      if (drawer.id === 'settings-drawer') closePromptEditor();
    }
  });
});
document.addEventListener('keydown', async e => {
  if (document.querySelector('.workflow-dialog[open]')) return;
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
  if (window.membersHasDraft?.() && !await window.membersMayLeave()) return;
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

async function switchSettingsTab(name) {
  if (window.membersHasDraft?.() && !await window.membersMayLeave()) return;
  const feature = PlatformPermissions.catalog().flatMap(group => group.children).find(item => item.tab === name);
  if (feature && !PlatformPermissions.allowed(feature.key)) return;
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
  const allowedTab = PlatformPermissions.catalog().flatMap(group => group.children).find(item => item.tab && PlatformPermissions.allowed(item.key))?.tab;
  const name = active && active.style.display !== 'none' ? active.dataset.stab : allowedTab;
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
          <div class="glossary-import__warning">若文件被替换，或其他管理员先修改了术语库，确认时会要求重新预览。</div>
          <div class="glossary-panel__actions">
            <button class="btn btn--ghost btn--sm" type="button" data-glossary-repreview ${glossaryUiState.importer.busy ? 'disabled' : ''}>重新预览</button>
            <button class="btn btn--primary btn--sm" type="button" data-glossary-commit ${glossaryUiState.importer.busy || !previewCanCommit ? 'disabled' : ''}>${glossaryUiState.importer.busy ? '导入中…' : `确认导入 ${(previewStats.total ?? previewRows.length ?? 0)} 条`}</button>
          </div>
          ${glossaryPreviewRowsHtml(preview, previewLanguages)}
        ` : ''}
      </section>

      ${visibleItems.length ? glossaryTableHtml(visibleItems, languages) : `<div class="glossary-empty">没有符合当前筛选条件的术语。</div>`}
    </section>
  `;
  if (window.PlatformPermissions && !PlatformPermissions.canUse('glossary')) {
    body.querySelectorAll('[data-glossary-import],[data-glossary-create],[data-glossary-edit],[data-glossary-delete],[data-glossary-toggle],[data-glossary-save],[data-glossary-commit]').forEach(el=>el.disabled=true);
  }
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
  document.querySelector('#glossary-editor-panel .glossary-panel__head')?.scrollIntoView({ behavior: 'instant', block: 'nearest' });
  $('glossary-editor-category')?.focus({ preventScroll: true });
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
  $('glossary-import-panel')?.scrollIntoView({ behavior: 'instant', block: 'start' });
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

const memberUi = {users: [], editing: null, dirty: false, query: '', status: 'all', tab: 'members'};
window.membersHasDraft = () => memberUi.dirty;
window.membersMayLeave = async () => {
  if (!memberUi.dirty) return true;
  if (!await window.workflowConfirm('成员权限修改尚未保存。放弃只影响这份草稿，已保存的权限保持不变。')) return false;
  memberUi.dirty = false; memberUi.editing = null; return true;
};
window.addEventListener('beforeunload', event => { if (memberUi.dirty) { event.preventDefault(); event.returnValue = ''; } });

async function loadPermsTab() {
  if (memberUi.dirty) return;
  const body = $('stab-content-perms');
  body.innerHTML = '<div class="hist-empty">正在加载成员…</div>';
  try {
    const data = await workflowRequest('/api/admin/users');
    memberUi.users = data.users || []; renderPermsTable();
  } catch (error) { body.innerHTML = `<div class="hist-empty">${esc(error.message)}</div>`; }
}
function renderPermsTable() {
  const body = $('stab-content-perms');
  body.innerHTML = `<div class="workflow-heading"><div><h3>成员与权限</h3><p class="workflow-muted">按平台菜单选择功能，修改后需保存。停用成员保留业务记录和已有配置。</p></div><button class="btn btn--primary" id="member-new">新增成员</button></div>
    <div class="member-tabs" role="tablist" aria-label="成员权限页签"><button type="button" role="tab" aria-controls="member-content" id="member-tab-list">成员管理</button><button type="button" role="tab" aria-controls="member-content" id="member-tab-history">变更记录</button></div><div id="member-content" role="tabpanel"></div>`;
  $('member-new').onclick = async () => { if(!await membersMayLeave())return; memberUi.editing={email:'',name:'',perms:[],enabled:true}; memberUi.tab='members'; renderMemberContent(); };
  $('member-tab-list').onclick = async () => { if(!await membersMayLeave())return;memberUi.tab='members';renderMemberContent(); };
  $('member-tab-history').onclick = async () => { if(!await membersMayLeave())return;memberUi.tab='history';renderMemberContent(); };
  renderMemberContent();
}
function renderMemberContent() {
  const target=$('member-content');
  const history = memberUi.tab === 'history';
  $('member-new').hidden = history;
  [['member-tab-list', !history], ['member-tab-history', history]].forEach(([id, active]) => {
    $(id).setAttribute('aria-selected', String(active));
    $(id).tabIndex = active ? 0 : -1;
    $(id).onkeydown = async event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 'member-tab-list' : event.key === 'End' ? 'member-tab-history' : id === 'member-tab-list' ? 'member-tab-history' : 'member-tab-list';
      await $(next).onclick();
      if ($(next).getAttribute('aria-selected') === 'true') $(next).focus();
    };
  });
  target.setAttribute('aria-labelledby', history ? 'member-tab-history' : 'member-tab-list');
  if(memberUi.tab==='history') {
    const events=memberUi.users.flatMap(user=>(user.permission_history||[]).map(event=>({...event,name:event.after?.name||event.before?.name||user.name}))).sort((a,b)=>(Date.parse(b.at)||0)-(Date.parse(a.at)||0));
    target.innerHTML=`<div class="member-change-list">${events.map(event=>`<article class="member-change-card"><dl class="member-change-meta"><div><dt>操作人</dt><dd>${esc(event.actor_name||'管理员')}</dd></div><div><dt>操作模块</dt><dd>成员与权限</dd></div><div><dt>变更成员</dt><dd>${esc(event.name||'未记录姓名')}</dd></div><div><dt>操作时间</dt><dd><time>${esc(formatMemberChangeTime(event.at))}</time></dd></div></dl><div class="member-change-content"><strong>操作内容</strong><ul>${memberChangeLines(event).map(line=>`<li>${esc(line)}</li>`).join('')}</ul></div></article>`).join('')||'<div class="workflow-empty">尚无成员权限变更记录。保存修改后可在这里核对；更早操作可在用户日志查看。</div>'}</div>`;
    return;
  }
  target.innerHTML=`<div class="member-layout"><aside class="member-list" aria-label="成员列表"><input class="plan-input" id="member-search" aria-label="搜索成员" placeholder="搜索姓名或账号" value="${esc(memberUi.query)}"><select class="plan-input" id="member-status" aria-label="成员状态筛选">${[['all','全部成员'],['on','已启用'],['off','已停用']].map(([value,text])=>`<option value="${value}" ${memberUi.status===value?'selected':''}>${text}</option>`).join('')}</select><div id="member-list"></div></aside><div class="member-editor" id="member-editor"></div></div>`;
  const renderList=()=>{$('member-list').innerHTML=memberUi.users.filter(user=>`${user.name} ${user.email}`.toLowerCase().includes(memberUi.query.toLowerCase())&&(memberUi.status==='all'||Boolean(user.enabled)===(memberUi.status==='on'))).map((user)=>`<button class="btn btn--ghost" aria-pressed="${memberUi.editing?.email===user.email}" data-member="${esc(user.email)}">${esc(user.name||'待补充姓名')}<small>${user.is_admin?'管理员（受保护）':user.enabled?'已启用':'已停用'}</small></button>`).join('')||'<p class="workflow-muted">没有匹配的成员。</p>';
    target.querySelectorAll('[data-member]').forEach(button=>button.onclick=async()=>{if(!await membersMayLeave())return;memberUi.editing=structuredClone(memberUi.users.find(user=>user.email===button.dataset.member));renderList();renderMemberEditor();});};
  $('member-search').oninput=e=>{memberUi.query=e.target.value;renderList();};$('member-status').onchange=e=>{memberUi.status=e.target.value;renderList();};renderList();renderMemberEditor();
}

function formatMemberChangeTime(value) {
  return window.workflowTime(value);
}

function memberChangeLines(event) {
  const before = event.before, after = event.after || {}, lines = [];
  const features = PlatformPermissions.catalog().flatMap(group => group.children);
  const name = key => features.find(item => item.key === key)?.label || key;
  const grants = value => {
    const raw = value?.perms || [];
    const pairs = Array.isArray(raw) ? raw.map(key => [key, 'use']) : Object.entries(raw).filter(([, level]) => level);
    return Object.fromEntries(pairs.map(([key, level]) => [key, features.find(item => item.key === key)?.view_only || ['read', 'readonly', 'read_only'].includes(value?.permission_levels?.[key] || level) ? 'read' : 'use']));
  };
  const previous = grants(before), current = grants(after);
  const describe = (key, source) => name(key) + (source[key] === 'read' ? '（仅查看）' : '');
  if (!before) {
    lines.push(`新增成员：${after.name || event.name || '未记录姓名'}`, `成员状态：${after.enabled ? '启用' : '停用'}`);
    lines.push('授予权限：' + (Object.keys(current).map(key => describe(key, current)).join('、') || '无'));
    return lines;
  }
  if (before.name !== after.name) lines.push(`显示姓名：${before.name || '未填写'} → ${after.name || '未填写'}`);
  if (before.enabled !== after.enabled) lines.push(`成员状态：${before.enabled ? '启用' : '停用'} → ${after.enabled ? '启用' : '停用'}`);
  const added = Object.keys(current).filter(key => !(key in previous)), removed = Object.keys(previous).filter(key => !(key in current));
  if (added.length) lines.push('新增权限：' + added.map(key => describe(key, current)).join('、'));
  if (removed.length) lines.push('移除权限：' + removed.map(key => describe(key, previous)).join('、'));
  Object.keys(current).filter(key => key in previous && current[key] !== previous[key]).forEach(key => lines.push(`${name(key)}：${previous[key] === 'read' ? '仅查看' : '可使用'} → ${current[key] === 'read' ? '仅查看' : '可使用'}`));
  return lines.length ? lines : ['保存成员配置，姓名、状态和功能权限未变化。'];
}
function renderMemberEditor() {
  const target=$('member-editor'),user=memberUi.editing;
  if(!user){target.innerHTML='<div class="workflow-empty">选择成员编辑，或新增成员。</div>';return;}
  const existing=memberUi.users.some(item=>item.email===user.email),locked=user.is_admin;
  target.innerHTML=`<section class="workflow-card"><h3>${existing?'编辑成员':'新增成员'}</h3><label class="workflow-field">显示姓名<input id="member-name" value="${esc(user.name||'')}" ${locked?'disabled':''}></label><label class="workflow-field">唯一账号（飞书邮箱或 Open ID）<input id="member-email" value="${esc(user.email||'')}" ${existing?'disabled':''} placeholder="邮箱或 ou_…"></label><label><input id="member-enabled" type="checkbox" ${user.enabled?'checked':''} ${locked?'disabled':''}> 启用成员</label>
    <div id="member-permission-tree"></div>${Object.values(user.permission_levels||{}).includes('read')?'<p class="workflow-notice">历史只读限制继续保留；勾选功能不会将它升级为可修改。</p>':''}<section class="member-effect"><h3>授权效果</h3><div id="member-effect"></div><details id="member-preview"><summary>预览成员菜单</summary><div id="member-preview-menus"></div><p class="workflow-muted">仅预览菜单与操作权限，不切换账号或读取该成员的业务数据。</p></details></section>${locked?'<p class="workflow-muted">管理员身份由平台配置保护，不能在此修改。</p>':'<div class="workflow-toolbar"><span id="member-save-state" class="workflow-muted"></span><button class="btn btn--primary" id="member-save" disabled>保存修改</button><button class="btn btn--ghost" id="member-cancel" disabled>撤销修改</button></div>'}</section>`;
  PlatformPermissions.tree($('member-permission-tree'),user.perms||[],!!locked);
  const updateEffect = () => {
    const draft={...user,name:$('member-name').value.trim(),enabled:$('member-enabled').checked,perms:PlatformPermissions.selected($('member-permission-tree'))};
    const original=memberUi.users.find(item=>item.email===user.email);
    const changed=!existing||draft.name!==original.name||draft.enabled!==original.enabled||JSON.stringify([...draft.perms].sort())!==JSON.stringify([...(original.perms||[])].sort());
    memberUi.dirty=!locked&&changed;
    const changes=memberChangeLines({before:existing?original:null,after:draft});
    $('member-effect').innerHTML=`<p>${draft.enabled?`已开放 ${draft.perms.length} 项功能。`:'已停用：保留配置和历史记录，不能使用平台功能。'}</p><p class="workflow-muted">${locked?'管理员可查看全员记录；其他人的业务记录保持只读。':'默认处理自己的业务记录；既有账号的查看范围和只读限制保持原设置。'}</p>${memberUi.dirty?`<details open><summary>待保存变更</summary><ul>${changes.map(line=>`<li>${esc(line)}</li>`).join('')}</ul></details>`:''}`;
    $('member-preview-menus').innerHTML=draft.enabled?PlatformPermissions.catalog().map(group=>{const allowed=group.children.filter(item=>(locked||!item.admin_only)&&draft.perms.includes(item.key));return allowed.length?`<section><strong>${esc(group.label)}</strong><ul>${allowed.map(item=>`<li>${esc(item.label)}${item.view_only||draft.permission_levels?.[item.key]==='read'?' · 仅查看':''}</li>`).join('')}</ul></section>`:'';}).join('')||'<p>尚未开放任何功能。</p>':'<p>账号已停用，不展示功能菜单。</p>';
    if(!locked){$('member-save').disabled=!changed;$('member-cancel').disabled=!changed;$('member-save-state').textContent=changed?'修改尚未保存':'当前配置已保存';}
  };
  target.oninput=updateEffect;target.onchange=updateEffect;updateEffect();
  if(locked)return;
  $('member-cancel').onclick=()=>{memberUi.dirty=false;memberUi.editing=existing?structuredClone(memberUi.users.find(item=>item.email===user.email)):null;renderMemberEditor();};
  $('member-save').onclick=async()=>{
    const body={name:$('member-name').value.trim(),email:$('member-email').value.trim(),enabled:$('member-enabled').checked,perms:PlatformPermissions.selected($('member-permission-tree'))};
    if(!body.name||!body.email){showToast('请填写显示姓名和唯一账号','error');return;}
    if(body.enabled&&!body.perms.length){showToast('启用成员至少需要一项功能权限','error');return;}
    const button=$('member-save');button.disabled=true;
    try{
      if(existing){delete body.email;body.expected_revision=user.revision;}
      await workflowRequest('/api/admin/users'+(existing?'/'+encodeURIComponent(user.email):''),{method:existing?'PATCH':'POST',body});
      memberUi.dirty=false;memberUi.editing=null;showToast('成员资料与权限已保存','success');await loadPermsTab();await refreshFeishuStatus();
    }catch(error){showToast(error.message,'error',6000);}finally{if(button.isConnected)button.disabled=false;}
  };
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
    <option value="${esc(u.email)}" ${filters.user === u.email ? 'selected' : ''}>${esc(u.name || "未记录姓名")}</option>
  `).join('');
  const featureOptions = features.map(f => `
    <option value="${esc(f.key)}" ${filters.feature === f.key ? 'selected' : ''}>${esc(f.label)}</option>
  `).join('');
  const rows = logs.map((item, idx) => {
    const userText = item.user_name || '未记录姓名';
    const featureText = item.feature_label || auditFeatureLabel(features, item.feature);
    const actionText = item.action || '';
    const detailText = item.detail || '';
    const statusText = item.status || 'success';
    return `
    <tr class="audit-row" data-audit-row="${idx}">
      <td class="audit-time" title="${esc(formatAuditTime(item.ts))}">${esc(formatAuditTime(item.ts))}</td>
      <td>
        <div class="audit-user" title="${esc(userText)}">${esc(userText)}</div>

      </td>
      <td title="${esc(featureText)}"><span class="audit-feature">${esc(featureText)}</span></td>
      <td class="audit-action" title="${esc(actionText)}">${esc(actionText)}</td>
      <td class="audit-detail" title="${esc(detailText)}">${esc(detailText)}</td>
      <td><span class="audit-status audit-status--${esc(statusText)}">${esc(statusText)}</span></td>
    </tr>
    <tr class="audit-detail-row" data-audit-detail="${idx}" hidden>
      <td colspan="6">
        <div class="audit-detail-card">
          <div><strong>用户</strong><span>${esc(userText)}</span></div>
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
    editable: !!prompt.editable && window.PlatformPermissions?.canUse('prompts') !== false,
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
    if (!PlatformPermissions.canUse('ui_texts')) {
      body.querySelectorAll('textarea').forEach(el=>el.readOnly=true);
      body.querySelectorAll('[data-uitext-save]').forEach(el=>el.disabled=true);
    }
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

window.addEventListener('storage', event => {
  if (event.key === 'group-management-scope-updated') {
    window.dispatchEvent(new Event('group-management-scope-changed'));
    if ($('setting-group-only-self')) loadSystemSettings();
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
        <div class="uitext-card__label" id="duplicate-reminder-settings-title">重复文件提醒</div>
        <div class="prompt-card__desc">各功能独立控制。开启后，发现匹配的历史报告时，会提示查看已有报告或重新分析；关闭后按正常流程继续。保存后对下一次操作生效。</div>
        <div role="group" aria-labelledby="duplicate-reminder-settings-title" style="display:flex;flex-wrap:wrap;column-gap:32px;">
          <label class="setting-toggle">
            <input type="checkbox" id="setting-survey-duplicate" ${data.survey_duplicate_reminder_enabled ? 'checked' : ''} />
            <span>问卷分析</span>
          </label>
          <label class="setting-toggle">
            <input type="checkbox" id="setting-comment-duplicate" ${data.comment_duplicate_reminder_enabled ? 'checked' : ''} />
            <span>评论分析</span>
          </label>
        </div>
      </div>
      <div class="uitext-card">
        <div class="uitext-card__label">问卷分析·Google Link 入口</div>
        <div class="prompt-card__desc">底层 Google Forms 连接可用时，开启后用户刷新页面即可看到入口；关闭后相关接口也会停止开放。</div>
        <label class="setting-toggle">
          <input type="checkbox" id="setting-google-forms-entry" ${data.google_forms_entry_enabled ? 'checked' : ''} />
          <span>开放 Google Link 入口</span>
        </label>
      </div>
      <div class="uitext-card">
        <div class="uitext-card__label">问卷分析·快速总结</div>
        <div class="prompt-card__desc">开启后，数据确认页可选择快速总结，按原题顺序整理主观题回复。保存后生效，无需重启服务；关闭不影响正在生成的报告及已有报告的查看、追问和导出。</div>
        <label class="setting-toggle">
          <input type="checkbox" id="setting-report-quick-mode" ${data.report_quick_mode_enabled ? 'checked' : ''} />
          <span>开放快速总结</span>
        </label>
      </div>
      <div class="uitext-card">
        <div class="uitext-card__label">问卷分析·先写作后核验</div>
        <div class="prompt-card__desc">开启后，新建的观点洞察报告先写作，再核验来源、原意和人数范围；核验结果在独立审阅面板查看。关闭后，新建报告使用原流程。保存后立即生效，无需重启；不会打断正在运行的任务，也不会重写已有报告。已有新流程报告需重新生成或继续核验时，请先开启。此设置对使用当前工作台的所有用户生效。</div>
        <label class="setting-toggle">
          <input type="checkbox" id="setting-report-postwrite" ${data.report_postwrite_enabled ? 'checked' : ''} />
          <span>先写作后核验</span>
        </label>
      </div>
      ${data.group_management_preferences ? `
      <div class="uitext-card">
        <div class="uitext-card__label">群组管理 · 个人查看范围</div>
        <div class="prompt-card__desc">仅对当前管理员账号生效。勾选后，质量标注、奖励导入、其他奖励和奖励批次仅显示自己创建的记录；取消后可查看并操作全部记录。共享名单配置仍可使用，已启动的后台标注继续执行。</div>
        <label class="setting-toggle">
          <input type="checkbox" id="setting-group-only-self" data-revision="${data.group_management_preferences.revision}" ${data.group_management_preferences.only_self ? 'checked' : ''} />
          <span>仅看自己</span>
        </label>
      </div>` : ''}
    `;
    if (!PlatformPermissions.canUse('platform')) body.querySelectorAll('input').forEach(el=>el.disabled=true);
  } catch (e) {
    body.innerHTML = `<div class="hist-empty">加载平台设置失败：${esc(e.message)}</div>`;
  }
}

async function saveGroupManagementScope(input) {
  const requested = input.checked;
  input.disabled = true;
  try {
    if (window.groupWorkflowMayLeave && !await window.groupWorkflowMayLeave()) {
      input.checked = !requested;
      return;
    }
    const resp = await fetch('/api/group-management/preferences', {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({only_self: requested, expected_revision: Number(input.dataset.revision)}),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `保存查看范围失败（HTTP ${resp.status}）`);
    input.checked = data.only_self;
    input.dataset.revision = String(data.revision);
    window.dispatchEvent(new Event('group-management-scope-changed'));
    // Notify other tabs without storing identity or business records in the browser.
    try { localStorage.setItem('group-management-scope-updated', String(Date.now())); } catch {}
    showToast(data.only_self ? '已保存：群组管理仅看自己' : '已保存：群组管理查看全部', 'success');
  } catch (e) {
    input.checked = !requested;
    // A lost response does not prove that the server rejected the save.
    window.dispatchEvent(new Event('group-management-scope-changed'));
    showToast(`保存群组管理查看范围失败：${e.message}；请重新打开设置核对已保存范围`, 'error');
  } finally { input.disabled = false; }
}

$('stab-content-system')?.addEventListener('change', async e => {
  if (e.target.id === 'setting-group-only-self') {
    await saveGroupManagementScope(e.target);
    return;
  }
  const input = e.target.closest('#setting-comment-duplicate, #setting-survey-duplicate, #setting-google-forms-entry, #setting-report-quick-mode, #setting-report-postwrite');
  if (!input) return;
  const settingKey = {
    'setting-comment-duplicate': 'comment_duplicate_reminder_enabled',
    'setting-survey-duplicate': 'survey_duplicate_reminder_enabled',
    'setting-google-forms-entry': 'google_forms_entry_enabled',
    'setting-report-quick-mode': 'report_quick_mode_enabled',
    'setting-report-postwrite': 'report_postwrite_enabled',
  }[input.id];
  input.disabled = true;
  try {
    const resp = await fetch('/api/app-settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [settingKey]: input.checked }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '保存失败');
    input.checked = !!data[settingKey];
    showToast(settingKey === 'report_postwrite_enabled'
      ? `先写作后核验已${input.checked ? '开启' : '关闭'}，对后续操作立即生效` : '平台设置已保存', 'success');
    if (settingKey === 'report_quick_mode_enabled'
        && state.currentStep === 2 && state.sessionId
        && !state.reportStyleSelection?.locked
        && typeof loadReportStyleOptions === 'function') {
      await loadReportStyleOptions();
    }
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
