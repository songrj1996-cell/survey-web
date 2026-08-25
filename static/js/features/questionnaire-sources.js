'use strict';

(function initQuestionnaireSourcePanel() {
  const STYLE_ID = 'qsrc-stylesheet';
  const PANEL_ID = 'qsrc-panel';
  const INPUT_PREFIX = 'qsrc-input-';
  const MAX_IMAGE_COUNT = 20;
  const SNAPSHOT_CATALOG_LIMIT = 20;
  const CAPABILITIES_URL = '/api/questionnaire-sources/capabilities';
  const SNAPSHOTS_ENDPOINT = '/api/questionnaire-sources/snapshots';
  const GOOGLE_FORMS_SNAPSHOTS_ENDPOINT = '/api/questionnaire-sources/google-forms/snapshots';
  const SNAPSHOT_ANALYSIS_INTERFACE_KEY = 'questionnaireSnapshotAnalysisSelection';
  const ASSET_REVIEW_INTERFACE_KEY = 'questionnaireAssetReview';
  const ASSET_REVIEW_SCRIPT_ID = 'qar-script';
  const ASSET_REVIEW_SCRIPT_URL = '/static/js/features/research-asset-review.js?v=1';
  const HTTP_HIDE_STATUSES = new Set([401, 403, 404]);
  const CAPABILITY_KEYS = [
    'snapshot_catalog',
    'snapshot_package_upload',
    'bested_original_questionnaire_upload',
    'screenshot_material_upload',
    'pdf_material_upload',
    'asset_review_projection',
  ];
  const OPTIONAL_CAPABILITY_KEYS = [
    'snapshot_analysis_session',
    'asset_review_decisions',
    'google_forms_connection',
    'source_workflow',
  ];

  const SOURCE_DEFS = [
    {
      key: 'google_forms_connection',
      type: 'google_forms',
      endpoint: GOOGLE_FORMS_SNAPSHOTS_ENDPOINT,
      title: 'Google Forms 原问卷',
      badge: '编辑链接',
      description: '粘贴 Google Forms 编辑链接，由部署账号读取问卷结构并保存为独立快照。',
      note: '仅支持 https://docs.google.com/forms/d/.../edit；公开填写链接或 forms.gle 请改用 PDF、截图或快照包。',
      placeholder: '粘贴 Google Forms 编辑链接',
      submitLabel: '读取并保存问卷',
      loadingLabel: '正在读取 Google Forms…',
      emptyLabel: '未填写编辑链接',
      validateValue(value) {
        try {
          const trimmed = typeof value === 'string' ? value.trim() : '';
          parseGoogleFormsEditorLink(trimmed);
          return {
            error: '',
            payload: { form_url: trimmed },
          };
        } catch (error) {
          return {
            error: error instanceof Error && error.message
              ? error.message
              : '请填写可访问的 Google Forms 编辑链接',
            payload: null,
          };
        }
      },
    },
    {
      key: 'snapshot_package_upload',
      type: 'snapshot',
      endpoint: SNAPSHOTS_ENDPOINT,
      fieldName: 'file',
      accept: '.zip,application/zip',
      multiple: false,
      maxFiles: 1,
      title: '问卷快照包',
      badge: 'ZIP',
      description: '导入本地整理好的问卷快照包，适合已经完成结构化归档的问卷。',
      note: '仅接收单个 ZIP 文件。',
      chooseLabel: '选择 ZIP',
      submitLabel: '保存快照',
      loadingLabel: '正在保存快照…',
      emptyLabel: '未选择文件',
      validate(files) {
        if (files.length !== this.maxFiles) return '请只选择 1 个 ZIP 文件';
        const file = files[0];
        if (!file || !/\.zip$/i.test(file.name)) return '请上传 .zip 格式的问卷快照包';
        return '';
      },
    },
    {
      key: 'bested_original_questionnaire_upload',
      type: 'bested',
      endpoint: '/api/questionnaire-sources/bested/snapshots',
      fieldName: 'file',
      accept: '.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      multiple: false,
      maxFiles: 1,
      title: '倍市得原问卷',
      badge: 'XLSX',
      description: '保存倍市得导出的原问卷结构，便于后续单独复用题型、题干和素材引用。',
      note: '仅接收单个 .xlsx 文件。',
      chooseLabel: '选择问卷',
      submitLabel: '保存原问卷',
      loadingLabel: '正在导入倍市得问卷…',
      emptyLabel: '未选择文件',
      validate(files) {
        if (files.length !== this.maxFiles) return '请只选择 1 个倍市得原问卷文件';
        const file = files[0];
        if (!file || !/\.xlsx$/i.test(file.name)) return '请上传 .xlsx 格式的倍市得原问卷';
        return '';
      },
    },
    {
      key: 'screenshot_material_upload',
      type: 'screenshots',
      endpoint: '/api/questionnaire-sources/materials/snapshots',
      fieldName: 'files',
      accept: '.png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp',
      multiple: true,
      maxFiles: MAX_IMAGE_COUNT,
      title: '问卷截图材料',
      badge: '1-20 张',
      description: '上传 PNG、JPEG 或 WebP 截图，由系统生成待复核的本地问卷快照。',
      note: '截图保存后需要人工复核，不会自动替换当前报告来源。',
      chooseLabel: '选择截图',
      submitLabel: '保存截图快照',
      loadingLabel: '正在处理截图材料…',
      emptyLabel: '未选择图片',
      validate(files) {
        if (!files.length) return '请先选择问卷截图';
        if (files.length > this.maxFiles) return '截图数量最多 20 张';
        const invalid = files.find(file => !/\.(png|jpe?g|webp)$/i.test(file.name));
        if (invalid) return '仅支持 PNG、JPEG 或 WebP 问卷截图';
        return '';
      },
    },
    {
      key: 'pdf_material_upload',
      type: 'pdf',
      endpoint: '/api/questionnaire-sources/materials/pdf/snapshots',
      fieldName: 'file',
      accept: '.pdf,application/pdf',
      multiple: false,
      maxFiles: 1,
      title: '问卷 PDF 材料',
      badge: 'PDF',
      description: '上传单个问卷 PDF，生成独立保存的本地快照，适合完整题本或导出的成稿。',
      note: 'PDF 保存后同样需要人工复核。',
      chooseLabel: '选择 PDF',
      submitLabel: '保存 PDF 快照',
      loadingLabel: '正在处理 PDF 材料…',
      emptyLabel: '未选择文件',
      validate(files) {
        if (files.length !== this.maxFiles) return '请只选择 1 个 PDF 文件';
        const file = files[0];
        if (!file || !/\.pdf$/i.test(file.name)) return '请上传 .pdf 格式的问卷材料';
        return '';
      },
    },
  ];

  const panelState = {
    capabilities: null,
    loadingCapabilities: false,
    cardStates: {},
    catalog: {
      items: [],
      nextCursor: '',
      phase: 'idle',
      message: '',
      requestSerial: 0,
      abortController: null,
      hasLoaded: false,
    },
    capabilityRequestSerial: 0,
    capabilityAbortController: null,
    selectedSnapshotId: '',
  };

  let panel = null;
  let cardsHost = null;
  let catalogSection = null;
  let catalogList = null;
  let catalogStatus = null;
  let catalogLoadMoreButton = null;
  let refreshButton = null;
  let assetReviewModulePromise = null;

  function ensureStylesheet() {
    if (document.getElementById(STYLE_ID)) return;
    const link = document.createElement('link');
    link.id = STYLE_ID;
    link.rel = 'stylesheet';
    link.href = '/static/questionnaire-sources.css?v=2';
    document.head.appendChild(link);
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatBytes(bytes) {
    const value = Number(bytes || 0);
    if (!Number.isFinite(value) || value <= 0) return '0 B';
    if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(value >= 10 * 1024 * 1024 ? 0 : 1)} MB`;
    if (value >= 1024) return `${Math.round(value / 1024)} KB`;
    return `${value} B`;
  }

  function parseGoogleFormsEditorLink(value) {
    const raw = typeof value === 'string' ? value.trim() : '';
    if (!raw) throw new Error('请先粘贴 Google Forms 编辑链接');
    let parsed;
    try {
      parsed = new URL(raw);
    } catch {
      throw new Error('请输入完整的 Google Forms 编辑链接');
    }
    if (parsed.protocol !== 'https:') {
      throw new Error('仅支持 https:// 开头的 Google Forms 编辑链接');
    }
    if (parsed.hostname === 'forms.gle') {
      throw new Error('暂不支持 forms.gle 短链，请改贴 Google Forms 编辑链接，或上传 PDF、截图、快照包');
    }
    if (parsed.hostname !== 'docs.google.com') {
      throw new Error('请粘贴 docs.google.com 域名下的 Google Forms 编辑链接');
    }
    const path = parsed.pathname.replace(/\/+$/, '');
    const editMatch = path.match(/^\/forms\/d\/([A-Za-z0-9_-]+)\/edit$/);
    if (editMatch) return editMatch[1];
    if (/^\/forms\/d\/e\/[A-Za-z0-9_-]+\/viewform$/.test(path) || /\/viewform$/.test(path)) {
      throw new Error('公开填写链接不能可靠读取问卷结构，请改贴 Google Forms 编辑链接，或上传 PDF、截图、快照包');
    }
    throw new Error('当前只支持 Google Forms 编辑链接 /forms/d/.../edit');
  }

  function getCardState(type) {
    if (!panelState.cardStates[type]) {
      panelState.cardStates[type] = {
        files: [],
        sourceValue: '',
        phase: 'idle',
        message: '',
        summary: null,
        requestSerial: 0,
        abortController: null,
      };
    }
    return panelState.cardStates[type];
  }

  function getCatalogState() {
    return panelState.catalog;
  }

  function mountPanel() {
    if (panel && cardsHost && refreshButton && catalogSection && catalogList && catalogStatus && catalogLoadMoreButton) return true;
    const uploadArea = document.getElementById('upload-area');
    const bestedUpload = document.getElementById('survey-bested-upload');
    if (!uploadArea || !bestedUpload) return false;

    const existing = document.getElementById(PANEL_ID);
    if (existing) {
      panel = existing;
      cardsHost = existing.querySelector('.qsrc-panel__cards');
      catalogSection = existing.querySelector('.qsrc-catalog');
      catalogList = existing.querySelector('.qsrc-catalog__list');
      catalogStatus = existing.querySelector('.qsrc-catalog__status');
      catalogLoadMoreButton = existing.querySelector('.qsrc-catalog__load-more');
      refreshButton = existing.querySelector('.qsrc-panel__refresh');
      return !!(cardsHost && refreshButton && catalogSection && catalogList && catalogStatus && catalogLoadMoreButton);
    }

    panel = el('section', 'qsrc-panel');
    panel.id = PANEL_ID;
    panel.hidden = true;
    panel.setAttribute('aria-labelledby', 'qsrc-panel-title');

    const head = el('div', 'qsrc-panel__head');
    const titleWrap = el('div', 'qsrc-panel__title-wrap');
    titleWrap.append(
      el('div', 'qsrc-panel__eyebrow', '问卷结构快照'),
      Object.assign(el('h2', 'qsrc-panel__title', '单独保存问卷来源'), { id: 'qsrc-panel-title' }),
      el('p', 'qsrc-panel__desc', '支持把 Google Forms 编辑链接、本地问卷文件或材料保存为本地独立快照，便于后续单独复用。这里保存的快照不会自动用于当前报告。'),
    );

    refreshButton = el('button', 'qsrc-panel__refresh', '刷新能力');
    refreshButton.type = 'button';
    refreshButton.addEventListener('click', () => refresh());
    head.append(titleWrap, refreshButton);

    cardsHost = el('div', 'qsrc-panel__cards');
    catalogSection = el('section', 'qsrc-catalog');
    catalogSection.hidden = true;
    catalogSection.setAttribute('aria-labelledby', 'qsrc-catalog-title');

    const catalogHead = el('div', 'qsrc-catalog__head');
    const catalogTitleWrap = el('div', 'qsrc-catalog__title-wrap');
    catalogTitleWrap.append(
      Object.assign(el('h3', 'qsrc-catalog__title', '已保存快照'), { id: 'qsrc-catalog-title' }),
      el('p', 'qsrc-catalog__desc', '仅显示安全摘要，不展示原始文件路径、媒体内容或其他敏感字段。快照不会自动绑定到当前报告。'),
    );
    catalogLoadMoreButton = el('button', 'qsrc-btn qsrc-btn--ghost qsrc-catalog__load-more', '加载更多');
    catalogLoadMoreButton.type = 'button';
    catalogLoadMoreButton.hidden = true;
    catalogLoadMoreButton.addEventListener('click', () => refreshCatalog({ append: true }));
    catalogHead.append(catalogTitleWrap, catalogLoadMoreButton);

    catalogList = el('div', 'qsrc-catalog__list');
    catalogStatus = el('div', 'qsrc-catalog__status');
    catalogStatus.setAttribute('aria-live', 'polite');
    catalogSection.append(catalogHead, catalogList, catalogStatus);

    const disclaimer = el('div', 'qsrc-card__disclaimer', '提示：本区域只负责独立保存本地问卷快照。若 Google Forms 无法访问，请按提示共享编辑权限，或改传问卷 PDF、截图、快照包；保存成功后仍需在后续单独选择，当前问卷分析流程不会自动改用这些快照。');
    const foot = el('div', 'qsrc-panel__foot', '支持能力由服务端显式声明；未开放的入口不会展示。');

    panel.append(head, cardsHost, catalogSection, disclaimer, foot);
    bestedUpload.insertAdjacentElement('afterend', panel);
    return true;
  }

  function setPanelVisibility(visible) {
    if (!panel) return;
    panel.hidden = !visible;
  }

  function supportedSources() {
    if (!panelState.capabilities) return [];
    return SOURCE_DEFS.filter(def => panelState.capabilities[def.key] === true);
  }

  function shouldShowCatalog() {
    return !!(panelState.capabilities && panelState.capabilities.snapshot_catalog === true);
  }

  function snapshotAnalysisEnabled() {
    return !!(panelState.capabilities && panelState.capabilities.snapshot_analysis_session === true);
  }

  function selectedSnapshotId() {
    return typeof panelState.selectedSnapshotId === 'string' ? panelState.selectedSnapshotId : '';
  }

  function resetAnalysisSelection() {
    if (!selectedSnapshotId()) return;
    panelState.selectedSnapshotId = '';
    renderCatalog();
  }

  function setSelectedSnapshotId(snapshotId) {
    const normalized = typeof snapshotId === 'string' ? snapshotId.trim() : '';
    if (!normalized) {
      resetAnalysisSelection();
      return;
    }
    panelState.selectedSnapshotId = normalized;
    renderCatalog();
  }

  function canUseSnapshotForAnalysis(entry) {
    return snapshotAnalysisEnabled() && Number(entry?.question_count || 0) > 0;
  }

  function canReviewSnapshotAssets(entry) {
    return !!(
      panelState.capabilities
      && panelState.capabilities.asset_review_projection === true
      && Number(entry?.asset_reference_count || 0) > 0
    );
  }

  function canSubmitSnapshotAssetReviewDecisions() {
    return !!(
      panelState.capabilities
      && panelState.capabilities.asset_review_decisions === true
    );
  }

  function isSelectedForAnalysis(snapshotId) {
    return selectedSnapshotId() === snapshotId;
  }

  function catalogSelectionHintText() {
    if (!snapshotAnalysisEnabled()) {
      return '当前批次仍只负责独立保存快照，不会自动进入报告流程。';
    }
    if (!selectedSnapshotId()) {
      return '可为本次标准分析选 1 份结构快照；当前批次只接结构，图片不会自动进入报告。';
    }
    return `已选择快照 ${selectedSnapshotId()}：后续上传回答数据时会按这份结构创建标准分析 session，图片不会自动进入报告。`;
  }

  function exposeSnapshotAnalysisSelection() {
    const api = Object.freeze({
      getSelectedSnapshotId: () => selectedSnapshotId(),
      reset: () => resetAnalysisSelection(),
    });
    const descriptor = Object.getOwnPropertyDescriptor(
      window,
      SNAPSHOT_ANALYSIS_INTERFACE_KEY,
    );
    if (descriptor && descriptor.value === api) return;
    Object.defineProperty(window, SNAPSHOT_ANALYSIS_INTERFACE_KEY, {
      value: api,
      configurable: true,
      enumerable: false,
      writable: false,
    });
  }

  function shouldShowPanel() {
    return supportedSources().length > 0 || shouldShowCatalog();
  }

  function setRefreshLoading(loading) {
    if (!refreshButton) return;
    refreshButton.disabled = loading;
    refreshButton.textContent = loading ? '读取中…' : '刷新能力';
  }

  function normalizeCapabilities(payload) {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    if (payload.schema_version !== 1) return null;
    const normalized = { schema_version: 1 };
    for (const key of CAPABILITY_KEYS) {
      if (typeof payload[key] !== 'boolean') return null;
      normalized[key] = payload[key];
    }
    for (const key of OPTIONAL_CAPABILITY_KEYS) {
      normalized[key] = payload[key] === true;
    }
    return normalized;
  }

  function normalizeCatalogEntry(entry) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return null;
    const snapshotId = typeof entry.snapshot_id === 'string' ? entry.snapshot_id.trim() : '';
    if (!snapshotId) return null;
    return {
      snapshot_id: snapshotId,
      provider: typeof entry.provider === 'string' ? entry.provider : '',
      source_mode: typeof entry.source_mode === 'string' ? entry.source_mode : '',
      collection_state: typeof entry.collection_state === 'string' ? entry.collection_state : '',
      mapping_status: typeof entry.mapping_status === 'string' ? entry.mapping_status : '',
      processing_status: typeof entry.processing_status === 'string' ? entry.processing_status : '',
      question_count: Number.isFinite(entry.question_count) ? entry.question_count : null,
      item_count: Number.isFinite(entry.item_count) ? entry.item_count : null,
      asset_count: Number.isFinite(entry.asset_count) ? entry.asset_count : null,
      image_asset_count: Number.isFinite(entry.image_asset_count) ? entry.image_asset_count : null,
      asset_reference_count: Number.isFinite(entry.asset_reference_count) ? entry.asset_reference_count : null,
      file_count: Number.isFinite(entry.file_count) ? entry.file_count : null,
      image_count: Number.isFinite(entry.image_count) ? entry.image_count : null,
      document_count: Number.isFinite(entry.document_count) ? entry.document_count : null,
      page_count: Number.isFinite(entry.page_count) ? entry.page_count : null,
      total_size_bytes: Number.isFinite(entry.total_size_bytes) ? entry.total_size_bytes : null,
      trust_level: typeof entry.trust_level === 'string' ? entry.trust_level : '',
      requires_human_review: entry.requires_human_review === true,
    };
  }

  function normalizeCatalogPayload(payload) {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    if (payload.schema_version !== 1 || !Array.isArray(payload.items)) return null;
    const items = payload.items.map(normalizeCatalogEntry).filter(Boolean);
    const nextCursor = typeof payload.next_cursor === 'string' ? payload.next_cursor : '';
    return {
      items,
      next_cursor: nextCursor,
    };
  }

  async function fetchCapabilities(signal) {
    const response = await fetch(CAPABILITIES_URL, { method: 'GET', signal });
    if (HTTP_HIDE_STATUSES.has(response.status)) return { hidden: true };
    if (!response.ok) throw new Error(`capabilities_${response.status}`);
    let payload;
    try {
      payload = await response.json();
    } catch {
      return { hidden: true };
    }
    const capabilities = normalizeCapabilities(payload);
    if (!capabilities) return { hidden: true };
    return { hidden: false, capabilities };
  }

  function snapshotCatalogUrl(cursor) {
    const trimmedCursor = typeof cursor === 'string' ? cursor.trim() : '';
    const cursorQuery = trimmedCursor ? `&cursor=${encodeURIComponent(trimmedCursor)}` : '';
    return `${SNAPSHOTS_ENDPOINT}?limit=${SNAPSHOT_CATALOG_LIMIT}${cursorQuery}`;
  }

  async function fetchCatalog(cursor, signal) {
    const response = await fetch(snapshotCatalogUrl(cursor), {
      method: 'GET',
      signal,
    });
    if (HTTP_HIDE_STATUSES.has(response.status)) return { hidden: true };
    if (!response.ok) {
      const fallback = response.status === 429
        ? '快照目录读取过于频繁，请稍后重试'
        : '快照目录暂时不可用';
      const detail = await responseErrorMessage(response, fallback);
      return { hidden: false, error: detail };
    }
    let payload;
    try {
      payload = await response.json();
    } catch {
      return { hidden: false, error: '快照目录返回结果无法识别' };
    }
    const normalized = normalizeCatalogPayload(payload);
    if (!normalized) return { hidden: false, error: '快照目录返回结果缺少安全摘要字段' };
    return { hidden: false, payload: normalized };
  }

  function setFiles(def, files) {
    const cardState = getCardState(def.type);
    cardState.files = files.slice();
    cardState.summary = null;
    if (!files.length) {
      cardState.phase = 'idle';
      cardState.message = '';
      renderCard(def);
      return;
    }
    const validation = def.validate(files);
    if (validation) {
      cardState.phase = 'error';
      cardState.message = validation;
    } else {
      cardState.phase = 'ready';
      cardState.message = `已选择 ${files.length} 个文件，可单独保存为本地问卷快照`;
    }
    renderCard(def);
  }

  function setSourceValue(def, value) {
    const cardState = getCardState(def.type);
    cardState.sourceValue = typeof value === 'string' ? value : '';
    cardState.summary = null;
    if (!cardState.sourceValue.trim()) {
      cardState.phase = 'idle';
      cardState.message = '';
      renderCard(def);
      return;
    }
    const validation = def.validateValue(cardState.sourceValue);
    if (validation.error) {
      cardState.phase = 'error';
      cardState.message = validation.error;
    } else {
      cardState.phase = 'ready';
      cardState.message = '已识别编辑链接，可读取结构并保存为独立快照';
    }
    renderCard(def);
  }

  function reset(defType) {
    if (defType) {
      const cardState = getCardState(defType);
      cardState.requestSerial += 1;
      if (cardState.abortController) {
        cardState.abortController.abort();
        cardState.abortController = null;
      }
      panelState.cardStates[defType] = {
        files: [],
        sourceValue: '',
        phase: 'idle',
        message: '',
        summary: null,
        requestSerial: cardState.requestSerial,
        abortController: null,
      };
      const input = document.getElementById(`${INPUT_PREFIX}${defType}`);
      if (input) input.value = '';
      const def = SOURCE_DEFS.find(item => item.type === defType);
      if (def) renderCard(def);
      return;
    }

    for (const def of SOURCE_DEFS) {
      const cardState = getCardState(def.type);
      cardState.requestSerial += 1;
      if (cardState.abortController) {
        cardState.abortController.abort();
        cardState.abortController = null;
      }
      panelState.cardStates[def.type] = {
        files: [],
        sourceValue: '',
        phase: 'idle',
        message: '',
        summary: null,
        requestSerial: cardState.requestSerial,
        abortController: null,
      };
      const input = document.getElementById(`${INPUT_PREFIX}${def.type}`);
      if (input) input.value = '';
    }
    renderCards();
  }

  function resetCatalogState() {
    const catalogState = getCatalogState();
    catalogState.requestSerial += 1;
    if (catalogState.abortController) {
      catalogState.abortController.abort();
      catalogState.abortController = null;
    }
    panelState.catalog = {
      items: [],
      nextCursor: '',
      phase: 'idle',
      message: '',
      requestSerial: catalogState.requestSerial,
      abortController: null,
      hasLoaded: false,
    };
    panelState.selectedSnapshotId = '';
    closeAssetReview();
  }

  function hidePanel(reason) {
    closeAssetReview();
    reset();
    resetCatalogState();
    panelState.capabilities = null;
    if (panelState.capabilityAbortController) {
      panelState.capabilityAbortController.abort();
      panelState.capabilityAbortController = null;
    }
    panelState.capabilityRequestSerial += 1;
    panelState.loadingCapabilities = false;
    setRefreshLoading(false);
    setPanelVisibility(false);
    if (reason) showUploadToast(reason, 'error');
  }

  function setCardMessage(messageNode, message, extra, phase) {
    messageNode.textContent = '';
    messageNode.className = 'qsrc-card__status';
    if (!message && !extra) return;
    if (phase) messageNode.classList.add(`is-${phase}`);
    if (message) {
      const line = el('span', '', message);
      messageNode.appendChild(line);
    }
    if (extra) {
      const meta = el('span', 'qsrc-card__status-meta');
      const pill = el('span', 'qsrc-card__status-pill', extra.label);
      meta.appendChild(pill);
      if (extra.detail) {
        meta.appendChild(el('span', '', extra.detail));
      }
      messageNode.appendChild(meta);
    }
  }

  function summaryMeta(def, summary) {
    if (!summary || typeof summary !== 'object') return null;
    const snapshotId = typeof summary.snapshot_id === 'string' ? summary.snapshot_id : '';
    const needsReview = summary.requires_human_review === true
      || summary.mapping_status === 'needs_review'
      || summary.processing_status === 'needs_review';
    const questionCount = Number.isFinite(summary.question_count) ? `题目 ${summary.question_count}` : '';
    const fileCount = Number.isFinite(summary.file_count) ? `文件 ${summary.file_count}` : '';
    const pageCount = Number.isFinite(summary.page_count) ? `页数 ${summary.page_count}` : '';
    const detail = [snapshotId ? `ID ${snapshotId}` : '', questionCount, fileCount, pageCount].filter(Boolean).join(' · ');
    if (needsReview) {
      return {
        phase: 'review',
        message: '已保存独立快照，需人工复核后再决定是否使用',
        extra: { label: '待人工复核', detail },
      };
    }
    return {
      phase: 'success',
        message: def.type === 'snapshot' || def.type === 'bested' || def.type === 'google_forms'
          ? '已保存独立快照，不会自动用于当前报告'
          : '已保存独立快照，当前报告不会自动引用',
      extra: { label: '保存成功', detail },
    };
  }

  function sourceModeLabel(value) {
    if (value === 'material_upload') return '材料快照';
    if (value === 'local_upload') return '本地快照';
    return value || '快照';
  }

  function mappingStatusLabel(value) {
    if (value === 'exact') return '映射准确';
    if (value === 'partial') return '映射部分可用';
    if (value === 'normalized') return '映射已归一';
    if (value === 'needs_review') return '待人工复核';
    if (value === 'unsupported') return '待补充处理';
    return value || '状态未知';
  }

  function reviewLabel(entry) {
    if (entry.requires_human_review || entry.mapping_status === 'needs_review' || entry.processing_status === 'needs_review') {
      return '待人工复核';
    }
    if (entry.collection_state === 'ready') return '可复用';
    return '已保存';
  }

  function catalogMeta(entry) {
    return [
      entry.source_mode ? sourceModeLabel(entry.source_mode) : '',
      entry.provider || '',
      entry.mapping_status ? mappingStatusLabel(entry.mapping_status) : '',
    ].filter(Boolean);
  }

  function catalogMetrics(entry) {
    return [
      Number.isFinite(entry.question_count) ? `题目 ${entry.question_count}` : '',
      Number.isFinite(entry.item_count) ? `条目 ${entry.item_count}` : '',
      Number.isFinite(entry.asset_count) ? `素材 ${entry.asset_count}` : '',
      Number.isFinite(entry.image_count) ? `图片 ${entry.image_count}` : '',
      Number.isFinite(entry.page_count) ? `页数 ${entry.page_count}` : '',
      Number.isFinite(entry.total_size_bytes) ? formatBytes(entry.total_size_bytes) : '',
    ].filter(Boolean);
  }

  function renderCatalog() {
    if (!catalogSection || !catalogList || !catalogStatus || !catalogLoadMoreButton) return;
    const catalogState = getCatalogState();
    const visible = shouldShowCatalog();
    catalogSection.hidden = !visible;
    catalogList.replaceChildren();
    catalogStatus.textContent = '';
    catalogStatus.className = 'qsrc-catalog__status';
    catalogLoadMoreButton.hidden = true;
    catalogLoadMoreButton.disabled = false;
    catalogLoadMoreButton.textContent = '加载更多';
    if (!visible) return;

    if (!catalogState.items.length) {
      const empty = el('div', 'qsrc-catalog__empty', catalogState.phase === 'loading' ? '正在读取已保存快照…' : '当前还没有可显示的已保存快照');
      catalogList.appendChild(empty);
    } else {
      const availableIds = new Set(catalogState.items.map(entry => entry.snapshot_id));
      if (selectedSnapshotId() && !availableIds.has(selectedSnapshotId())) {
        panelState.selectedSnapshotId = '';
      }
      const items = catalogState.items.map(entry => {
        const row = el('article', 'qsrc-catalog__item');
        if (isSelectedForAnalysis(entry.snapshot_id)) row.classList.add('is-selected');
        const top = el('div', 'qsrc-catalog__item-top');
        const titleWrap = el('div', 'qsrc-catalog__item-title-wrap');
        titleWrap.append(
          el('div', 'qsrc-catalog__item-id', entry.snapshot_id),
          el('div', 'qsrc-catalog__item-meta', catalogMeta(entry).join(' · ')),
        );
        const badge = el('span', 'qsrc-catalog__item-badge', reviewLabel(entry));
        if (reviewLabel(entry) === '待人工复核') badge.classList.add('is-review');
        top.append(titleWrap, badge);
        const metrics = el('div', 'qsrc-catalog__item-metrics', catalogMetrics(entry).join(' · ') || '仅保留安全摘要');
        row.append(top, metrics);
        if (canReviewSnapshotAssets(entry)) {
          const writable = canSubmitSnapshotAssetReviewDecisions();
          const reviewActions = el('div', 'qsrc-catalog__item-actions');
          const reviewNote = el(
            'div',
            'qsrc-catalog__item-selection-note',
            writable
              ? '可查看素材安全摘要并逐项确认；缩略图仍需手动加载。'
              : '只读预览素材安全摘要；缩略图需逐项手动加载，当前账号不能提交确认。',
          );
          const reviewButton = el(
            'button',
            'qsrc-btn qsrc-btn--ghost qsrc-catalog__item-action',
            '查看素材',
          );
          reviewButton.type = 'button';
          reviewButton.addEventListener('click', async () => {
            reviewButton.disabled = true;
            try {
              const review = await loadAssetReviewModule();
              if (!review || typeof review.openForSnapshot !== 'function') {
                throw new Error('素材审阅模块暂时不可用');
              }
              if (!canReviewSnapshotAssets(entry)) {
                throw new Error('素材审阅模块暂时不可用');
              }
              review.openForSnapshot(entry.snapshot_id, {
                trigger: reviewButton,
                writable: canSubmitSnapshotAssetReviewDecisions(),
              });
            } catch (error) {
              const message = error instanceof Error && error.message
                ? error.message
                : '素材审阅模块暂时不可用';
              showUploadToast(message, 'error');
            } finally {
              reviewButton.disabled = false;
            }
          });
          reviewActions.append(reviewNote, reviewButton);
          row.appendChild(reviewActions);
        }
        if (canUseSnapshotForAnalysis(entry)) {
          const actionWrap = el('div', 'qsrc-catalog__item-actions');
          const actionText = el(
            'div',
            'qsrc-catalog__item-selection-note',
            isSelectedForAnalysis(entry.snapshot_id)
              ? '当前回答文件会按这份快照结构进入标准分析，图片不会自动进入报告。'
              : '仅把这份快照的结构用于本次标准分析，图片不会自动进入报告。',
          );
          const actionButton = el(
            'button',
            isSelectedForAnalysis(entry.snapshot_id)
              ? 'qsrc-btn qsrc-btn--ghost qsrc-catalog__item-action'
              : 'qsrc-btn qsrc-btn--primary qsrc-catalog__item-action',
            isSelectedForAnalysis(entry.snapshot_id) ? '取消使用' : '用于本次分析',
          );
          actionButton.type = 'button';
          actionButton.setAttribute(
            'aria-pressed',
            isSelectedForAnalysis(entry.snapshot_id) ? 'true' : 'false',
          );
          actionButton.addEventListener('click', () => {
            if (isSelectedForAnalysis(entry.snapshot_id)) {
              resetAnalysisSelection();
            } else {
              setSelectedSnapshotId(entry.snapshot_id);
            }
          });
          actionWrap.append(actionText, actionButton);
          row.appendChild(actionWrap);
        }
        return row;
      });
      catalogList.replaceChildren(...items);
    }

    if (catalogState.phase === 'loading' && catalogState.items.length) {
      catalogStatus.textContent = '正在读取更多快照…';
      catalogStatus.classList.add('is-loading');
    } else if (catalogState.phase === 'error' && catalogState.message) {
      catalogStatus.textContent = catalogState.message;
      catalogStatus.classList.add('is-error');
    } else if (catalogState.hasLoaded && !catalogState.nextCursor) {
      catalogStatus.textContent = catalogState.items.length ? catalogSelectionHintText() : '目录为空，可先保存一个本地快照';
    }

    if (catalogState.nextCursor) {
      catalogLoadMoreButton.hidden = false;
      catalogLoadMoreButton.disabled = catalogState.phase === 'loading';
      catalogLoadMoreButton.textContent = catalogState.phase === 'loading' ? '读取中…' : '加载更多';
    }
  }

  function pickerClassName(cardState) {
    const classes = ['qsrc-card__picker'];
    if (cardState.phase === 'ready') classes.push('is-ready');
    if (cardState.phase === 'loading') classes.push('is-loading');
    if (cardState.phase === 'success') classes.push('is-success');
    if (cardState.phase === 'review') classes.push('is-review');
    if (cardState.phase === 'error') classes.push('is-error');
    return classes.join(' ');
  }

  function renderCard(def) {
    if (!cardsHost) return;
    const card = cardsHost.querySelector(`[data-qsrc-card="${def.type}"]`);
    if (!card) return;
    const cardState = getCardState(def.type);
    if (def.type === 'google_forms') {
      const picker = card.querySelector('.qsrc-card__picker');
      const input = card.querySelector('.qsrc-card__input');
      const helper = card.querySelector('.qsrc-card__helper');
      const submitButton = card.querySelector('[data-qsrc-action="submit"]');
      const resetButton = card.querySelector('[data-qsrc-action="reset"]');
      const status = card.querySelector('.qsrc-card__status');

      if (input) input.value = cardState.sourceValue;
      if (input) input.disabled = cardState.phase === 'loading';
      picker.className = pickerClassName(cardState);
      submitButton.disabled = cardState.phase === 'loading' || !cardState.sourceValue.trim();
      submitButton.textContent = cardState.phase === 'loading' ? def.loadingLabel : def.submitLabel;
      resetButton.disabled = cardState.phase !== 'loading' && !cardState.sourceValue.trim();
      resetButton.textContent = cardState.phase === 'loading' ? '取消读取' : '清空';
      if (helper) {
        helper.textContent = cardState.sourceValue.trim()
          ? '仅保存问卷结构快照；保存成功后仍需在后续手动选择用于本次分析。'
          : def.note;
      }

      const summary = summaryMeta(def, cardState.summary);
      if (summary) {
        setCardMessage(status, summary.message, summary.extra, summary.phase);
        return;
      }
      setCardMessage(status, cardState.message, null, cardState.phase === 'error' ? 'error' : cardState.phase === 'loading' ? 'loading' : '');
      return;
    }
    const picker = card.querySelector('.qsrc-card__picker');
    const fileName = card.querySelector('.qsrc-card__file-name');
    const fileMeta = card.querySelector('.qsrc-card__file-meta');
    const chooseButton = card.querySelector('[data-qsrc-action="choose"]');
    const submitButton = card.querySelector('[data-qsrc-action="submit"]');
    const resetButton = card.querySelector('[data-qsrc-action="reset"]');
    const status = card.querySelector('.qsrc-card__status');

    const fileCount = cardState.files.length;
    const totalSize = cardState.files.reduce((sum, file) => sum + (file.size || 0), 0);
    const firstName = fileCount ? cardState.files[0].name : def.emptyLabel;
    const fileLabel = fileCount > 1 ? `${firstName} 等 ${fileCount} 个文件` : firstName;
    fileName.textContent = fileLabel;
    fileMeta.textContent = fileCount
      ? `${fileCount} 个文件 · ${formatBytes(totalSize)}`
      : def.note;

    picker.className = pickerClassName(cardState);
    chooseButton.disabled = cardState.phase === 'loading';
    submitButton.disabled = cardState.phase === 'loading' || cardState.phase === 'error' || !fileCount;
    submitButton.textContent = cardState.phase === 'loading' ? def.loadingLabel : def.submitLabel;
    resetButton.disabled = cardState.phase !== 'loading' && !fileCount;
    resetButton.textContent = cardState.phase === 'loading' ? '取消上传' : '重置';

    const summary = summaryMeta(def, cardState.summary);
    if (summary) {
      setCardMessage(status, summary.message, summary.extra, summary.phase);
      return;
    }
    setCardMessage(status, cardState.message, null, cardState.phase === 'error' ? 'error' : cardState.phase === 'loading' ? 'loading' : '');
  }

  function createCard(def) {
    const card = el('article', 'qsrc-card');
    card.dataset.qsrcCard = def.type;

    const top = el('div', 'qsrc-card__top');
    const titleWrap = el('div', 'qsrc-card__title-wrap');
    titleWrap.append(
      el('h3', 'qsrc-card__title', def.title),
      el('p', 'qsrc-card__desc', def.description),
      el('p', 'qsrc-card__note', def.note),
    );
    top.append(titleWrap, el('span', 'qsrc-card__badge', def.badge));

    const picker = el('div', 'qsrc-card__picker');
    let input;
    if (def.type === 'google_forms') {
      input = document.createElement('input');
      input.id = `${INPUT_PREFIX}${def.type}`;
      input.type = 'url';
      input.className = 'qsrc-card__input';
      input.placeholder = def.placeholder;
      input.autocomplete = 'off';
      input.inputMode = 'url';
      input.spellcheck = false;
      input.setAttribute('aria-label', 'Google Forms 编辑链接');
      input.addEventListener('input', () => {
        setSourceValue(def, input.value);
      });
      picker.append(
        input,
        el('p', 'qsrc-card__helper', def.note),
      );
    } else {
      input = document.createElement('input');
      input.id = `${INPUT_PREFIX}${def.type}`;
      input.type = 'file';
      input.accept = def.accept;
      input.multiple = !!def.multiple;
      input.className = 'qsrc-sr-only';
      input.setAttribute('aria-label', `${def.title}文件选择`);
      input.addEventListener('change', () => {
        const files = Array.from(input.files || []);
        setFiles(def, files);
      });

      const pickerMain = el('div', 'qsrc-card__picker-main');
      const fileSummary = el('div', 'qsrc-card__file-summary');
      fileSummary.append(
        el('span', 'qsrc-card__file-name', def.emptyLabel),
        el('span', 'qsrc-card__file-meta', def.note),
      );
      const pickerActions = el('div', 'qsrc-card__picker-actions');
      const chooseButton = el('button', 'qsrc-btn qsrc-btn--ghost', def.chooseLabel);
      chooseButton.type = 'button';
      chooseButton.dataset.qsrcAction = 'choose';
      chooseButton.addEventListener('click', () => input.click());
      pickerActions.appendChild(chooseButton);
      pickerMain.append(fileSummary, pickerActions);
      picker.appendChild(pickerMain);
    }

    const footer = el('div', 'qsrc-card__footer');
    const submitButton = el('button', 'qsrc-btn qsrc-btn--primary', def.submitLabel);
    submitButton.type = 'button';
    submitButton.dataset.qsrcAction = 'submit';
    submitButton.addEventListener('click', () => (def.type === 'google_forms' ? uploadGoogleForms(def) : upload(def)));
    const resetButton = el('button', 'qsrc-btn qsrc-btn--ghost', def.type === 'google_forms' ? '清空' : '重置');
    resetButton.type = 'button';
    resetButton.dataset.qsrcAction = 'reset';
    resetButton.addEventListener('click', () => reset(def.type));
    footer.append(submitButton, resetButton);

    const status = el('div', 'qsrc-card__status');
    status.setAttribute('aria-live', 'polite');

    if (def.type === 'google_forms') {
      card.append(top, picker, footer, status);
    } else {
      card.append(top, input, picker, footer, status);
    }
    return card;
  }

  function renderCards() {
    if (!cardsHost) return;
    const supported = supportedSources();
    cardsHost.replaceChildren(...supported.map(createCard));
    for (const def of supported) renderCard(def);
  }

  function showUploadToast(message, type) {
    if (typeof window.showToast === 'function') {
      window.showToast(message, type);
    }
  }

  function assetReviewApi() {
    const candidate = window[ASSET_REVIEW_INTERFACE_KEY];
    if (!candidate || typeof candidate !== 'object') return null;
    return candidate;
  }

  function closeAssetReview() {
    const review = assetReviewApi();
    if (review && typeof review.close === 'function') {
      review.close();
    }
  }

  function removeAssetReviewScriptNode() {
    const existing = document.getElementById(ASSET_REVIEW_SCRIPT_ID);
    if (existing && existing.parentNode) existing.parentNode.removeChild(existing);
  }

  function loadAssetReviewModule() {
    const ready = assetReviewApi();
    if (ready) return Promise.resolve(ready);
    if (assetReviewModulePromise) return assetReviewModulePromise;
    assetReviewModulePromise = new Promise((resolve, reject) => {
      removeAssetReviewScriptNode();
      const script = document.createElement('script');
      script.id = ASSET_REVIEW_SCRIPT_ID;
      script.src = ASSET_REVIEW_SCRIPT_URL;
      script.async = true;
      const cleanup = () => {
        script.removeEventListener('load', handleLoad);
        script.removeEventListener('error', handleError);
      };
      const fail = message => {
        cleanup();
        assetReviewModulePromise = null;
        removeAssetReviewScriptNode();
        reject(new Error(message));
      };
      const handleLoad = () => {
        const review = assetReviewApi();
        if (!review) {
          fail('素材审阅模块未正确注册');
          return;
        }
        cleanup();
        assetReviewModulePromise = Promise.resolve(review);
        resolve(review);
      };
      const handleError = () => {
        fail('素材审阅资源加载失败');
      };
      script.addEventListener('load', handleLoad, { once: true });
      script.addEventListener('error', handleError, { once: true });
      document.head.appendChild(script);
    });
    return assetReviewModulePromise;
  }

  function responseErrorInfo(response, fallback) {
    return response.json()
      .then(parsed => {
        const detail = parsed && parsed.detail;
        if (typeof detail === 'string' && detail.trim()) {
          return { code: '', message: detail.trim() };
        }
        if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
          const code = typeof detail.code === 'string' && /^google_forms_[a-z_]+$/.test(detail.code)
            ? detail.code
            : '';
          const message = typeof detail.message === 'string' && detail.message.trim()
            ? detail.message.trim()
            : fallback;
          return { code, message };
        }
        return { code: '', message: fallback };
      })
      .catch(() => ({ code: '', message: fallback }));
  }

  function responseErrorMessage(response, fallback) {
    return responseErrorInfo(response, fallback).then(info => info.message);
  }

  function googleFormsErrorMessage(status, errorInfo) {
    const code = errorInfo && typeof errorInfo.code === 'string' ? errorInfo.code : '';
    const detail = errorInfo && typeof errorInfo.message === 'string' ? errorInfo.message : '';
    if (code.startsWith('google_forms_') && detail) {
      return detail;
    }
    if (status === 401) {
      return '登录状态已失效，请重新登录后再读取 Google Forms。';
    }
    if (status === 403) {
      return '当前账号没有问卷来源功能权限，请联系平台管理员。';
    }
    if (status === 404) {
      return 'Google Forms 导入入口当前不可用，请刷新页面或联系平台管理员。';
    }
    if (status === 422) {
      return detail && detail.includes('无效')
        ? '当前只支持 Google Forms 编辑链接 /forms/d/.../edit。公开填写链接和 forms.gle 短链暂不支持；请改贴编辑链接，或上传 PDF、截图、快照包。'
        : 'Google Forms 导入请求无效。请确认链接可打开到编辑页，或改传 PDF、截图、快照包。';
    }
    if (status === 429) {
      return 'Google Forms 导入入口正忙，请稍后重试；若赶时间，可先上传 PDF、截图或快照包。';
    }
    if (status === 502 || status === 503 || status === 504) {
      return 'Google Forms 暂时不可用，请稍后重试；若需要继续处理，请先上传问卷 PDF、截图或快照包。';
    }
    return detail || 'Google Forms 导入暂时不可用；若无法稍后重试，请先上传问卷 PDF、截图或快照包。';
  }

  async function upload(def) {
    const cardState = getCardState(def.type);
    const validation = def.validate(cardState.files);
    if (validation) {
      cardState.phase = 'error';
      cardState.message = validation;
      renderCard(def);
      showUploadToast(validation, 'error');
      return;
    }

    cardState.phase = 'loading';
    cardState.message = '正在上传并独立保存，不会自动用于当前报告';
    cardState.summary = null;
    cardState.requestSerial += 1;
    const requestSerial = cardState.requestSerial;
    if (cardState.abortController) cardState.abortController.abort();
    const abortController = new AbortController();
    cardState.abortController = abortController;
    renderCard(def);

    const formData = new FormData();
    for (const file of cardState.files) {
      formData.append(def.fieldName, file);
    }

    try {
      const response = await fetch(def.endpoint, {
        method: 'POST',
        body: formData,
        signal: abortController.signal,
      });
      if (cardState.requestSerial !== requestSerial) return;
      if (HTTP_HIDE_STATUSES.has(response.status)) {
        hidePanel('当前账号暂无本地问卷快照权限');
        return;
      }
      if (!response.ok) {
        const fallback = response.status === 429
          ? '当前入口繁忙，请稍后重试'
          : response.status === 504
            ? '处理超时，请稍后重试'
            : '保存失败，请重试';
        const detail = await responseErrorMessage(response, fallback);
        throw new Error(detail);
      }

      let summary;
      try {
        summary = await response.json();
      } catch {
        throw new Error('保存成功，但返回结果无法识别');
      }
      if (cardState.requestSerial !== requestSerial) return;
      if (!summary || typeof summary.snapshot_id !== 'string' || summary.snapshot_id.trim() === '') {
        throw new Error('保存成功，但返回结果缺少快照编号');
      }

      const meta = summaryMeta(def, summary);
      cardState.summary = summary;
      cardState.phase = meta ? meta.phase : 'success';
      cardState.message = meta ? meta.message : '已保存独立快照';
      renderCard(def);
      refreshCatalog();
      showUploadToast(cardState.phase === 'review' ? '已保存，后续请人工复核' : '已保存独立快照', cardState.phase === 'review' ? 'info' : 'success');
    } catch (error) {
      if (abortController.signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
        return;
      }
      if (cardState.requestSerial !== requestSerial) return;
      const message = error instanceof Error && error.message ? error.message : '保存失败，请重试';
      cardState.phase = 'error';
      cardState.message = message;
      cardState.summary = null;
      renderCard(def);
      showUploadToast(message, 'error');
    } finally {
      if (cardState.abortController === abortController) {
        cardState.abortController = null;
      }
    }
  }

  async function uploadGoogleForms(def) {
    const cardState = getCardState(def.type);
    const validation = def.validateValue(cardState.sourceValue);
    if (validation.error || !validation.payload) {
      const message = validation.error || '请先粘贴 Google Forms 编辑链接';
      cardState.phase = 'error';
      cardState.message = message;
      cardState.summary = null;
      renderCard(def);
      showUploadToast(message, 'error');
      return;
    }

    cardState.phase = 'loading';
    cardState.message = '正在读取问卷结构并独立保存，不会自动用于当前报告';
    cardState.summary = null;
    cardState.requestSerial += 1;
    const requestSerial = cardState.requestSerial;
    if (cardState.abortController) cardState.abortController.abort();
    const abortController = new AbortController();
    cardState.abortController = abortController;
    renderCard(def);

    try {
      const response = await fetch(def.endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(validation.payload),
        signal: abortController.signal,
      });
      if (cardState.requestSerial !== requestSerial) return;
      if (!response.ok) {
        const fallback = response.status === 504
          ? 'Google Forms 问卷导入超时，请稍后重试'
          : 'Google Forms 导入失败，请重试';
        const errorInfo = await responseErrorInfo(response, fallback);
        throw new Error(googleFormsErrorMessage(response.status, errorInfo));
      }

      let summary;
      try {
        summary = await response.json();
      } catch {
        throw new Error('保存成功，但返回结果无法识别');
      }
      if (cardState.requestSerial !== requestSerial) return;
      if (!summary || typeof summary.snapshot_id !== 'string' || summary.snapshot_id.trim() === '') {
        throw new Error('保存成功，但返回结果缺少快照编号');
      }

      const meta = summaryMeta(def, summary);
      cardState.summary = summary;
      cardState.phase = meta ? meta.phase : 'success';
      cardState.message = meta ? meta.message : '已保存独立快照';
      renderCard(def);
      refreshCatalog();
      showUploadToast('已保存独立快照', 'success');
    } catch (error) {
      if (abortController.signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
        return;
      }
      if (cardState.requestSerial !== requestSerial) return;
      const message = error instanceof Error && error.message ? error.message : 'Google Forms 导入失败，请重试';
      cardState.phase = 'error';
      cardState.message = message;
      cardState.summary = null;
      renderCard(def);
      showUploadToast(message, 'error');
    } finally {
      if (cardState.abortController === abortController) {
        cardState.abortController = null;
      }
    }
  }

  async function refreshCatalog(options) {
    if (!catalogSection || !shouldShowCatalog()) return;
    const append = !!(options && options.append);
    const catalogState = getCatalogState();
    if (catalogState.abortController) {
      catalogState.abortController.abort();
      catalogState.abortController = null;
    }
    catalogState.requestSerial += 1;
    const requestSerial = catalogState.requestSerial;
    const abortController = new AbortController();
    catalogState.abortController = abortController;
    catalogState.phase = 'loading';
    catalogState.message = '';
    renderCatalog();
    try {
      const result = await fetchCatalog(append ? catalogState.nextCursor : '', abortController.signal);
      if (catalogState.requestSerial !== requestSerial) return;
      if (result.hidden) {
        hidePanel('当前账号暂无本地问卷快照权限');
        return;
      }
      if (result.error) {
        catalogState.phase = 'error';
        catalogState.message = result.error;
        renderCatalog();
        return;
      }
      if (!result.payload) {
        catalogState.phase = 'error';
        catalogState.message = '快照目录暂时不可用';
        renderCatalog();
        return;
      }
      catalogState.items = append
        ? catalogState.items.concat(result.payload.items)
        : result.payload.items.slice();
      catalogState.nextCursor = result.payload.next_cursor;
      catalogState.phase = 'idle';
      catalogState.message = '';
      catalogState.hasLoaded = true;
      renderCatalog();
    } catch (error) {
      if (abortController.signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
        return;
      }
      if (catalogState.requestSerial !== requestSerial) return;
      const message = error instanceof Error && error.message ? error.message : '快照目录暂时不可用';
      catalogState.phase = 'error';
      catalogState.message = message;
      renderCatalog();
    } finally {
      if (catalogState.abortController === abortController) {
        catalogState.abortController = null;
      }
    }
  }

  async function refresh() {
    if (!mountPanel()) return;
    if (panelState.capabilityAbortController) {
      panelState.capabilityAbortController.abort();
      panelState.capabilityAbortController = null;
    }
    panelState.capabilityRequestSerial += 1;
    const requestSerial = panelState.capabilityRequestSerial;
    const abortController = new AbortController();
    panelState.capabilityAbortController = abortController;
    panelState.loadingCapabilities = true;
    setRefreshLoading(true);
    try {
      const result = await fetchCapabilities(abortController.signal);
      if (panelState.capabilityRequestSerial !== requestSerial) return;
      if (result.hidden || !result.capabilities) {
        closeAssetReview();
        panelState.capabilities = null;
        resetCatalogState();
        setPanelVisibility(false);
        return;
      }
      panelState.capabilities = result.capabilities;
      if (
        panelState.capabilities.asset_review_projection !== true
        || panelState.capabilities.asset_review_decisions !== true
      ) {
        closeAssetReview();
      }
      renderCards();
      renderCatalog();
      setPanelVisibility(shouldShowPanel());
      if (shouldShowCatalog()) {
        refreshCatalog();
      } else {
        closeAssetReview();
        resetCatalogState();
        renderCatalog();
      }
    } catch (error) {
      if (abortController.signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
        return;
      }
      closeAssetReview();
      panelState.capabilities = null;
      resetCatalogState();
      setPanelVisibility(false);
    } finally {
      if (panelState.capabilityAbortController === abortController) {
        panelState.capabilityAbortController = null;
      }
      if (panelState.capabilityRequestSerial === requestSerial) {
        panelState.loadingCapabilities = false;
        setRefreshLoading(false);
      }
    }
  }

  function bootstrap() {
    ensureStylesheet();
    exposeSnapshotAnalysisSelection();
    if (!mountPanel()) return;
    refresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
  } else {
    bootstrap();
  }
})();
