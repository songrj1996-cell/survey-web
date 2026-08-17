'use strict';

(function initQuestionnaireAssetReview() {
  const MODULE_KEY = 'questionnaireAssetReview';
  const STYLE_ID = 'qar-stylesheet';
  const STYLE_URL = '/static/research-asset-review.css?v=1';
  const DRAWER_ID = 'qar-drawer';
  const HIDE_STATUSES = new Set([401, 403]);
  const MAX_THUMBNAIL_BYTES = 16 * 1024 * 1024;
  const MAX_CACHED_THUMBNAILS = 24;
  const MAX_CACHED_THUMBNAIL_BYTES = 64 * 1024 * 1024;
  const MAX_CONCURRENT_THUMBNAILS = 2;
  const MAX_SNAPSHOT_ID_UTF8_BYTES = 4096;
  const TOKEN_PATTERN = /^[0-9a-f]{64}$/;
  const WARNING_PATTERN = /^[a-z0-9][a-z0-9_.-]{0,127}$/;
  const CONTEXT_TYPES = new Set([
    'survey_question',
    'survey_option',
    'survey_row',
    'interview_position',
    'research_document',
    'report',
  ]);
  const ROLES = new Set([
    'question_stimulus',
    'question_instruction',
    'option_stimulus',
    'participant_response',
    'interview_evidence',
    'researcher_material',
    'analysis_target',
    'report_attachment',
  ]);
  const BINDING_STATUSES = new Set([
    'proposed',
    'confirmed',
    'needs_review',
    'rejected',
  ]);
  const MEDIA_TYPES = new Set([
    'image',
    'video',
    'audio',
    'slide',
    'document',
    'external_link',
  ]);
  const PREVIEW_STATUSES = new Set([
    'available',
    'unavailable',
  ]);

  const state = {
    phase: 'idle',
    snapshotId: '',
    requestSerial: 0,
    abortController: null,
    projection: null,
    errorMessage: '',
    notice: '',
    noticePhase: '',
    restoreFocusTarget: null,
    openSequence: 0,
    thumbnailStates: Object.create(null),
    activeThumbnailRequests: new Set(),
    thumbnailViews: Object.create(null),
    cachedThumbnailBytes: 0,
    thumbnailLru: [],
  };

  let root = null;
  let panel = null;
  let titleNode = null;
  let subtitleNode = null;
  let statusNode = null;
  let noticeNode = null;
  let summaryNode = null;
  let listNode = null;
  let retryButton = null;
  let closeButton = null;
  let emptyNode = null;

  function ensureStylesheet() {
    if (document.getElementById(STYLE_ID)) return;
    const link = document.createElement('link');
    link.id = STYLE_ID;
    link.rel = 'stylesheet';
    link.href = STYLE_URL;
    document.head.appendChild(link);
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showToast(message, type) {
    if (typeof window.showToast === 'function' && message) {
      window.showToast(message, type || 'error');
    }
  }

  function isPlainObject(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value);
  }

  function exactInt(value, min, max) {
    return Number.isInteger(value) && value >= min && value <= max ? value : null;
  }

  function boundedString(value, min, max) {
    return typeof value === 'string' && value.length >= min && value.length <= max ? value : null;
  }

  function enumValue(value, allowed) {
    return typeof value === 'string' && allowed.has(value) ? value : null;
  }

  function normalizedBool(value) {
    return typeof value === 'boolean' ? value : null;
  }

  function normalizedConfidence(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1
      ? value
      : null;
  }

  function validateSnapshotId(value) {
    if (typeof value !== 'string') return '';
    const normalized = value.trim();
    if (!normalized) return '';
    if (new TextEncoder().encode(normalized).length > MAX_SNAPSHOT_ID_UTF8_BYTES) {
      return '';
    }
    return normalized;
  }

  function reviewEndpoint(snapshotId) {
    return sameOriginUrl(
      `/api/questionnaire-sources/snapshots/${encodeURIComponent(snapshotId)}/asset-review`,
    ).toString();
  }

  function thumbnailEndpoint(snapshotId, assetToken) {
    return sameOriginUrl(
      `/api/questionnaire-sources/snapshots/${encodeURIComponent(snapshotId)}/asset-review/thumbnails/${encodeURIComponent(assetToken)}.png`,
    ).toString();
  }

  function sameOriginUrl(path) {
    const url = new URL(path, window.location.origin);
    if (url.origin !== window.location.origin) {
      throw new Error('素材审阅地址必须保持同源');
    }
    return url;
  }

  function escapeSnapshotLabel(snapshotId) {
    return typeof snapshotId === 'string' && snapshotId.trim()
      ? snapshotId.trim()
      : '当前快照';
  }

  function bindingStatusLabel(value) {
    if (value === 'confirmed') return '已确认';
    if (value === 'proposed') return '待确认';
    if (value === 'needs_review') return '待复核';
    if (value === 'rejected') return '已排除';
    return '状态未知';
  }

  function contextTypeLabel(value) {
    if (value === 'survey_question') return '题目';
    if (value === 'survey_option') return '选项';
    if (value === 'survey_row') return '矩阵行';
    if (value === 'interview_position') return '访谈位置';
    if (value === 'research_document') return '研究文档';
    if (value === 'report') return '报告';
    return '上下文';
  }

  function roleLabel(value) {
    if (value === 'question_stimulus') return '题面素材';
    if (value === 'question_instruction') return '作答说明';
    if (value === 'option_stimulus') return '选项素材';
    if (value === 'participant_response') return '参与者反馈';
    if (value === 'interview_evidence') return '访谈证据';
    if (value === 'researcher_material') return '研究材料';
    if (value === 'analysis_target') return '分析对象';
    if (value === 'report_attachment') return '报告附件';
    return '素材角色';
  }

  function mediaTypeLabel(value) {
    if (value === 'image') return '图片';
    if (value === 'video') return '视频';
    if (value === 'audio') return '音频';
    if (value === 'slide') return '幻灯片';
    if (value === 'document') return '文档';
    if (value === 'external_link') return '外链';
    return '素材';
  }

  function warningCodeLabel(value) {
    if (value === 'binding_confidence_low') return '绑定置信度低';
    if (value === 'asset_reused_multiple_times') return '同素材多处复用';
    if (value === 'preview_unavailable') return '暂无预览';
    if (value === 'binding_target_missing') return '绑定目标待核对';
    return value;
  }

  function safeErrorDetail(value, fallback) {
    return typeof value === 'string' && value.trim() && value.trim().length <= 500
      ? value.trim()
      : fallback;
  }

  function responseErrorMessage(response, fallback) {
    return response.json()
      .then(parsed => (
        parsed ? safeErrorDetail(parsed.detail, fallback) : fallback
      ))
      .catch(() => fallback);
  }

  function focusableNodes() {
    if (!panel) return [];
    return Array.from(panel.querySelectorAll(
      'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ));
  }

  function activeSnapshotLabel() {
    return escapeSnapshotLabel(state.snapshotId);
  }

  function setNotice(message, phase) {
    state.notice = message || '';
    state.noticePhase = message ? (phase || 'info') : '';
  }

  function hasOpenDrawer() {
    return !!root && root.hidden === false;
  }

  function resetThumbnailState(entry) {
    if (!entry) return;
    if (entry.controller) {
      entry.controller.abort();
    }
    releaseThumbnailObject(entry);
    entry.controller = null;
    entry.phase = 'idle';
    entry.error = '';
  }

  function releaseThumbnailObject(entry) {
    if (!entry || !entry.objectUrl) return;
    URL.revokeObjectURL(entry.objectUrl);
    if (Number.isInteger(entry.objectSize) && entry.objectSize > 0) {
      state.cachedThumbnailBytes = Math.max(
        0,
        state.cachedThumbnailBytes - entry.objectSize,
      );
    }
    if (typeof entry.assetToken === 'string' && entry.assetToken) {
      state.thumbnailLru = state.thumbnailLru.filter(
        token => token !== entry.assetToken,
      );
    }
    entry.objectUrl = '';
    entry.objectSize = 0;
  }

  function touchThumbnailLru(assetToken) {
    state.thumbnailLru = state.thumbnailLru.filter(token => token !== assetToken);
    state.thumbnailLru.push(assetToken);
  }

  function evictThumbnailCache(exemptAssetToken) {
    while (
      state.thumbnailLru.length > MAX_CACHED_THUMBNAILS
      || state.cachedThumbnailBytes > MAX_CACHED_THUMBNAIL_BYTES
    ) {
      const victimAssetToken = state.thumbnailLru[0];
      if (!victimAssetToken) break;
      if (victimAssetToken === exemptAssetToken && state.thumbnailLru.length === 1) {
        break;
      }
      state.thumbnailLru.shift();
      if (victimAssetToken === exemptAssetToken) {
        state.thumbnailLru.push(victimAssetToken);
        continue;
      }
      const victimState = state.thumbnailStates[victimAssetToken];
      if (!victimState || !victimState.objectUrl) continue;
      releaseThumbnailObject(victimState);
      victimState.phase = 'idle';
      victimState.error = '';
      rerenderThumbnailViews(victimAssetToken);
    }
  }

  function resetAllThumbnails() {
    const values = Object.values(state.thumbnailStates);
    for (const entry of values) {
      resetThumbnailState(entry);
    }
    state.thumbnailStates = Object.create(null);
    state.thumbnailViews = Object.create(null);
    state.cachedThumbnailBytes = 0;
    state.thumbnailLru = [];
  }

  function resetProjectionState() {
    state.projection = null;
    state.snapshotId = '';
    state.errorMessage = '';
    state.notice = '';
    state.noticePhase = '';
    resetAllThumbnails();
    if (listNode) listNode.replaceChildren();
    if (emptyNode) emptyNode.hidden = true;
  }

  function clearInFlightProjection() {
    if (state.abortController) {
      state.abortController.abort();
      state.abortController = null;
    }
  }

  function close() {
    state.requestSerial += 1;
    state.openSequence += 1;
    clearInFlightProjection();
    resetProjectionState();
    state.phase = 'idle';
    if (root) {
      root.hidden = true;
      root.setAttribute('aria-hidden', 'true');
    }
    const target = state.restoreFocusTarget;
    state.restoreFocusTarget = null;
    if (target && typeof target.focus === 'function' && document.contains(target)) {
      target.focus();
    }
  }

  function setPhase(phase, message) {
    state.phase = phase;
    state.errorMessage = message || '';
  }

  function summaryText() {
    const projection = state.projection;
    if (!projection) return '安全摘要';
    return [
      `引用 ${projection.total_references}`,
      `待复核 ${projection.review_required_references}`,
      '缩略图需手动逐项加载',
    ].join(' · ');
  }

  function isExternalTrigger(trigger) {
    return !!(trigger && panel && !panel.contains(trigger));
  }

  function ensureDrawer() {
    if (root && panel && titleNode && subtitleNode && statusNode && noticeNode && summaryNode && listNode && retryButton && closeButton && emptyNode) {
      return;
    }

    const existing = document.getElementById(DRAWER_ID);
    if (existing) {
      root = existing;
      panel = existing.querySelector('.qar-panel');
      titleNode = existing.querySelector('.qar-title');
      subtitleNode = existing.querySelector('.qar-subtitle');
      statusNode = existing.querySelector('.qar-status');
      noticeNode = existing.querySelector('.qar-notice');
      summaryNode = existing.querySelector('.qar-summary');
      listNode = existing.querySelector('.qar-list');
      retryButton = existing.querySelector('[data-qar-action="retry"]');
      closeButton = existing.querySelector('[data-qar-action="close"]');
      emptyNode = existing.querySelector('.qar-empty');
      return;
    }

    root = el('section', 'qar-drawer');
    root.id = DRAWER_ID;
    root.hidden = true;
    root.setAttribute('aria-hidden', 'true');

    const overlay = el('div', 'qar-overlay');
    overlay.addEventListener('click', () => close());

    panel = el('div', 'qar-panel');
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-labelledby', 'qar-title');

    const header = el('div', 'qar-header');
    const headerText = el('div', 'qar-header__text');
    titleNode = el('h3', 'qar-title', '问卷素材审阅');
    titleNode.id = 'qar-title';
    subtitleNode = el('p', 'qar-subtitle', '仅预览，确认结果尚未保存');
    headerText.append(titleNode, subtitleNode);
    closeButton = el('button', 'qar-close', '关闭');
    closeButton.type = 'button';
    closeButton.dataset.qarAction = 'close';
    closeButton.setAttribute('aria-label', '关闭素材审阅');
    closeButton.addEventListener('click', () => close());
    header.append(headerText, closeButton);

    const banner = el('div', 'qar-banner');
    const note = el('div', 'qar-note', '仅预览，确认结果尚未保存');
    statusNode = el('div', 'qar-status');
    statusNode.setAttribute('aria-live', 'polite');
    banner.append(note, statusNode);

    noticeNode = el('div', 'qar-notice');
    noticeNode.setAttribute('aria-live', 'polite');

    const toolbar = el('div', 'qar-toolbar');
    summaryNode = el('div', 'qar-summary');
    retryButton = el('button', 'qar-btn qar-btn--ghost', '重新读取');
    retryButton.type = 'button';
    retryButton.dataset.qarAction = 'retry';
    retryButton.addEventListener('click', () => {
      if (!state.snapshotId) return;
      openForSnapshot(state.snapshotId, {
        preserveRestoreFocus: true,
        preserveNotice: false,
      });
    });
    toolbar.append(summaryNode, retryButton);

    const body = el('div', 'qar-body');
    emptyNode = el('div', 'qar-empty', '当前快照没有可审阅的素材引用');
    listNode = el('div', 'qar-list');
    body.append(emptyNode, listNode);

    panel.append(header, banner, noticeNode, toolbar, body);
    root.append(overlay, panel);
    document.body.appendChild(root);

    document.addEventListener('keydown', handleGlobalKeydown);
  }

  function handleGlobalKeydown(event) {
    if (!hasOpenDrawer() || !panel) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== 'Tab') return;
    const nodes = focusableNodes();
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement;
    const insidePanel = !!(active && panel.contains(active));
    if (!insidePanel) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
      return;
    }
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
      return;
    }
    if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function openDrawer(options) {
    ensureStylesheet();
    ensureDrawer();
    if (!options || !options.preserveRestoreFocus) {
      const trigger = options && options.trigger ? options.trigger : null;
      if (isExternalTrigger(trigger)) {
        state.restoreFocusTarget = trigger;
      } else if (!state.restoreFocusTarget && document.activeElement && isExternalTrigger(document.activeElement)) {
        state.restoreFocusTarget = document.activeElement;
      }
    }
    root.hidden = false;
    root.setAttribute('aria-hidden', 'false');
    state.openSequence += 1;
    const openSequence = state.openSequence;
    requestAnimationFrame(() => {
      if (
        closeButton
        && root
        && root.hidden === false
        && state.openSequence === openSequence
      ) {
        closeButton.focus();
      }
    });
  }

  function createMetaList(item) {
    return [
      contextTypeLabel(item.context_type),
      roleLabel(item.role),
      mediaTypeLabel(item.media_type),
      bindingStatusLabel(item.binding_status),
      `置信度 ${Math.round(item.binding_confidence * 100)}%`,
    ];
  }

  function getThumbnailState(assetToken) {
    if (!state.thumbnailStates[assetToken]) {
      state.thumbnailStates[assetToken] = {
        assetToken,
        phase: 'idle',
        error: '',
        objectUrl: '',
        objectSize: 0,
        controller: null,
      };
    }
    return state.thumbnailStates[assetToken];
  }

  function getThumbnailViews(assetToken) {
    if (!state.thumbnailViews[assetToken]) {
      state.thumbnailViews[assetToken] = [];
    }
    return state.thumbnailViews[assetToken];
  }

  function normalizeReviewItem(raw) {
    if (!isPlainObject(raw)) return null;
    const referenceToken = boundedString(raw.reference_token, 64, 64);
    const assetToken = boundedString(raw.asset_token, 64, 64);
    const contextType = enumValue(raw.context_type, CONTEXT_TYPES);
    const contextLabel = boundedString(raw.context_label, 1, 500);
    const role = enumValue(raw.role, ROLES);
    const bindingStatus = enumValue(raw.binding_status, BINDING_STATUSES);
    const bindingConfidence = normalizedConfidence(raw.binding_confidence);
    const reviewRequired = normalizedBool(raw.review_required);
    const mediaType = enumValue(raw.media_type, MEDIA_TYPES);
    const previewStatus = enumValue(raw.preview_status, PREVIEW_STATUSES);
    if (
      !referenceToken
      || !assetToken
      || !TOKEN_PATTERN.test(referenceToken)
      || !TOKEN_PATTERN.test(assetToken)
      || !contextType
      || !contextLabel
      || !role
      || !bindingStatus
      || bindingConfidence === null
      || reviewRequired === null
      || !mediaType
      || !previewStatus
      || !Array.isArray(raw.warning_codes)
      || raw.warning_codes.length > 64
    ) {
      return null;
    }
    if (reviewRequired !== (bindingStatus === 'proposed' || bindingStatus === 'needs_review')) {
      return null;
    }
    if (previewStatus === 'available' && mediaType !== 'image') {
      return null;
    }
    const warningCodes = [];
    for (const code of raw.warning_codes) {
      const normalizedCode = boundedString(code, 1, 128);
      if (!normalizedCode || !WARNING_PATTERN.test(normalizedCode)) return null;
      warningCodes.push(normalizedCode);
    }
    if (new Set(warningCodes).size !== warningCodes.length) return null;
    return {
      reference_token: referenceToken,
      asset_token: assetToken,
      context_type: contextType,
      context_label: contextLabel,
      role,
      binding_status: bindingStatus,
      binding_confidence: bindingConfidence,
      review_required: reviewRequired,
      media_type: mediaType,
      preview_status: previewStatus,
      warning_codes: warningCodes,
    };
  }

  function normalizeProjection(payload) {
    if (!isPlainObject(payload)) return null;
    const schemaVersion = exactInt(payload.schema_version, 1, 1);
    const totalReferences = exactInt(payload.total_references, 0, 2000);
    const reviewRequiredReferences = exactInt(payload.review_required_references, 0, 2000);
    if (schemaVersion !== 1 || totalReferences === null || reviewRequiredReferences === null || !Array.isArray(payload.items) || payload.items.length > 2000) {
      return null;
    }
    const items = payload.items.map(normalizeReviewItem);
    if (items.some(item => item === null)) return null;
    if (items.length !== totalReferences) return null;
    const reviewCount = items.filter(item => item.review_required).length;
    if (reviewCount !== reviewRequiredReferences) return null;
    return {
      schema_version: 1,
      total_references: totalReferences,
      review_required_references: reviewRequiredReferences,
      items,
    };
  }

  function renderStatus() {
    if (!statusNode || !noticeNode || !summaryNode || !retryButton || !emptyNode || !titleNode || !subtitleNode) return;
    titleNode.textContent = `问卷素材审阅 · ${activeSnapshotLabel()}`;
    subtitleNode.textContent = '仅预览，确认结果尚未保存';
    summaryNode.textContent = summaryText();
    statusNode.className = 'qar-status';
    noticeNode.className = 'qar-notice';
    noticeNode.textContent = '';
    emptyNode.hidden = true;

    if (state.notice) {
      noticeNode.textContent = state.notice;
      if (state.noticePhase) noticeNode.classList.add(`is-${state.noticePhase}`);
    }

    if (state.phase === 'loading') {
      statusNode.textContent = '正在读取素材安全摘要…';
      statusNode.classList.add('is-loading');
      retryButton.disabled = true;
      return;
    }
    if (state.phase === 'empty') {
      statusNode.textContent = '当前快照没有素材引用，可直接关闭';
      statusNode.classList.add('is-empty');
      retryButton.disabled = false;
      emptyNode.hidden = false;
      return;
    }
    if (state.phase === 'error') {
      statusNode.textContent = state.errorMessage || '素材审阅暂时不可用';
      statusNode.classList.add('is-error');
      retryButton.disabled = false;
      return;
    }
    statusNode.textContent = state.projection
      ? '只读预览已更新，缩略图需要逐项手动加载'
      : '打开后将按需读取当前快照的素材安全摘要';
    if (state.phase === 'ready') statusNode.classList.add('is-ready');
    retryButton.disabled = !state.snapshotId;
  }

  function renderThumbnailShell(item) {
    const thumbState = getThumbnailState(item.asset_token);
    const shell = el('div', 'qar-thumb');
    shell.tabIndex = -1;
    shell.setAttribute('aria-live', 'polite');
    shell.dataset.preview = item.preview_status;
    if (item.preview_status !== 'available') {
      shell.classList.add('is-unavailable');
      shell.appendChild(el('span', 'qar-thumb__fallback', '暂无预览'));
      return shell;
    }

    if (thumbState.phase === 'ready' && thumbState.objectUrl) {
      shell.classList.add('is-ready');
      const img = document.createElement('img');
      img.className = 'qar-thumb__image';
      img.alt = `${item.context_label} 素材预览`;
      img.src = thumbState.objectUrl;
      shell.appendChild(img);
      return shell;
    }

    const controls = el('div', 'qar-thumb__controls');
    const buttonLabel = thumbState.phase === 'error' ? '重新加载缩略图' : '加载缩略图';
    const button = el('button', 'qar-btn qar-btn--ghost qar-thumb__button', buttonLabel);
    button.type = 'button';
    button.disabled = thumbState.phase === 'loading';
    button.addEventListener('click', () => loadThumbnail(item));
    controls.appendChild(button);

    if (thumbState.phase === 'loading') {
      shell.classList.add('is-loading');
      controls.appendChild(el('span', 'qar-thumb__status', '正在加载 PNG 缩略图…'));
    } else if (thumbState.phase === 'error') {
      shell.classList.add('is-error');
      controls.appendChild(el('span', 'qar-thumb__status is-error', thumbState.error || '缩略图读取失败'));
    } else {
      controls.appendChild(el('span', 'qar-thumb__status', '手动加载后才会请求缩略图'));
    }
    shell.appendChild(controls);
    return shell;
  }

  function registerThumbnailView(assetToken, shell, item) {
    const views = getThumbnailViews(assetToken);
    views.push({ shell, item });
  }

  function rememberThumbnailFocus(views) {
    const active = document.activeElement;
    if (!active) return { shouldRestore: false };
    for (let index = 0; index < views.length; index += 1) {
      const shell = views[index] && views[index].shell;
      if (shell && shell.contains(active)) {
        return { shouldRestore: true, index };
      }
    }
    return { shouldRestore: false };
  }

  function restoreThumbnailFocus(assetToken, focusState) {
    if (!focusState || !focusState.shouldRestore) return;
    const views = getThumbnailViews(assetToken);
    const shell = (views[focusState.index] && views[focusState.index].shell)
      || (views[0] && views[0].shell);
    if (!shell) return;
    const button = shell.querySelector('.qar-thumb__button:not([disabled])');
    if (button && typeof button.focus === 'function') {
      button.focus();
      if (document.activeElement === button) return;
    }
    if (typeof shell.focus === 'function') shell.focus();
  }

  function rerenderThumbnailViews(assetToken) {
    const views = getThumbnailViews(assetToken).filter(
      view => view && view.shell && view.shell.isConnected,
    );
    state.thumbnailViews[assetToken] = views;
    if (!views.length || !state.projection) return;
    const focusState = rememberThumbnailFocus(views);
    const nextViews = [];
    for (const view of views) {
      const shell = view.shell;
      const row = shell.closest('.qar-item');
      if (!row) continue;
      const currentShell = row.querySelector('.qar-thumb');
      if (currentShell !== shell) continue;
      const nextShell = renderThumbnailShell(view.item);
      currentShell.replaceWith(nextShell);
      nextViews.push({ shell: nextShell, item: view.item });
    }
    state.thumbnailViews[assetToken] = nextViews;
    restoreThumbnailFocus(assetToken, focusState);
  }

  function renderItems() {
    if (!listNode || !emptyNode) return;
    listNode.replaceChildren();
    state.thumbnailViews = Object.create(null);
    const projection = state.projection;
    if (!projection || !projection.items.length) {
      emptyNode.hidden = false;
      return;
    }
    emptyNode.hidden = true;
    const fragment = document.createDocumentFragment();
    for (const item of projection.items) {
      const row = el('article', 'qar-item');
      if (item.review_required) row.classList.add('is-review-required');
      const thumb = renderThumbnailShell(item);
      registerThumbnailView(item.asset_token, thumb, item);
      const content = el('div', 'qar-item__content');
      const top = el('div', 'qar-item__top');
      const titleWrap = el('div', 'qar-item__title-wrap');
      titleWrap.append(
        el('div', 'qar-item__title', item.context_label),
        el('div', 'qar-item__meta', createMetaList(item).join(' · ')),
      );
      const badge = el(
        'span',
        item.review_required ? 'qar-badge is-review' : 'qar-badge',
        item.review_required ? '待人工复核' : '已确认',
      );
      top.append(titleWrap, badge);

      const warningWrap = el('div', 'qar-item__warnings');
      if (item.warning_codes.length) {
        for (const warningCode of item.warning_codes) {
          warningWrap.appendChild(el('span', 'qar-pill', warningCodeLabel(warningCode)));
        }
      } else {
        warningWrap.appendChild(el('span', 'qar-pill is-neutral', '未返回额外警告'));
      }

      content.append(top, warningWrap);
      row.append(thumb, content);
      fragment.appendChild(row);
    }
    listNode.appendChild(fragment);
  }

  function render() {
    ensureDrawer();
    renderStatus();
    renderItems();
  }

  async function loadProjection(snapshotId, signal) {
    const response = await fetch(reviewEndpoint(snapshotId), {
      method: 'GET',
      signal,
      cache: 'no-store',
      credentials: 'same-origin',
      redirect: 'error',
    });
    if (HIDE_STATUSES.has(response.status)) {
      const hiddenError = new Error('当前账号暂无查看素材预览的权限');
      hiddenError.code = 'hidden';
      throw hiddenError;
    }
    if (!response.ok) {
      const fallback = response.status === 429
        ? '当前素材预览入口繁忙，请稍后再试'
        : response.status === 504
          ? '素材预览处理超时，请稍后重试'
          : response.status === 404
            ? '问卷素材审阅内容不存在'
            : response.status === 422
              ? '素材预览被安全策略阻止'
              : response.status === 500
                ? '素材审阅暂时不可用'
              : '素材审阅暂时不可用';
      const error = new Error(await responseErrorMessage(response, fallback));
      error.code = String(response.status);
      throw error;
    }
    const normalized = normalizeProjection(await response.json());
    if (!normalized) {
      const error = new Error('素材审阅返回结果无法识别');
      error.code = 'malformed';
      throw error;
    }
    return normalized;
  }

  async function loadThumbnail(item) {
    if (!state.snapshotId || !state.projection) return;
    const serialAtStart = state.requestSerial;
    const thumbState = getThumbnailState(item.asset_token);
    if (thumbState.phase === 'loading') {
      rerenderThumbnailViews(item.asset_token);
      return;
    }
    if (thumbState.phase === 'ready' && thumbState.objectUrl) {
      touchThumbnailLru(item.asset_token);
      rerenderThumbnailViews(item.asset_token);
      return;
    }
    if (thumbState.phase !== 'ready') {
      resetThumbnailState(thumbState);
    }
    if (state.activeThumbnailRequests.size >= MAX_CONCURRENT_THUMBNAILS) {
      thumbState.phase = 'error';
      thumbState.error = `当前最多同时加载 ${MAX_CONCURRENT_THUMBNAILS} 个缩略图，请稍后重试`;
      rerenderThumbnailViews(item.asset_token);
      return;
    }
    thumbState.phase = 'loading';
    thumbState.error = '';
    rerenderThumbnailViews(item.asset_token);

    const controller = new AbortController();
    const requestKey = Symbol(item.asset_token);
    state.activeThumbnailRequests.add(requestKey);
    thumbState.controller = controller;
    try {
      const response = await fetch(thumbnailEndpoint(state.snapshotId, item.asset_token), {
        method: 'GET',
        signal: controller.signal,
        cache: 'no-store',
        credentials: 'same-origin',
        redirect: 'error',
      });
      if (HIDE_STATUSES.has(response.status)) {
        throw new Error('当前账号暂无缩略图预览权限');
      }
      if (!response.ok) {
        const fallback = response.status === 429
          ? '缩略图入口繁忙，请稍后再试'
          : response.status === 504
            ? '缩略图处理超时，请稍后重试'
            : response.status === 404
              ? '缩略图不存在'
              : response.status === 422
                ? '缩略图被安全策略阻止'
                : response.status === 500
                  ? '缩略图暂时不可用'
                : '缩略图暂时不可用';
        throw new Error(await responseErrorMessage(response, fallback));
      }
      const contentType = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
      if (contentType !== 'image/png') {
        throw new Error('缩略图返回类型不安全');
      }
      const contentLength = Number.parseInt(response.headers.get('content-length') || '', 10);
      if (!Number.isInteger(contentLength) || contentLength <= 0 || contentLength > MAX_THUMBNAIL_BYTES) {
        throw new Error('缩略图大小超出安全范围');
      }
      const blob = await response.blob();
      if (blob.size <= 0 || blob.size > MAX_THUMBNAIL_BYTES || blob.size !== contentLength) {
        throw new Error('缩略图内容大小校验失败');
      }
      if (state.requestSerial !== serialAtStart || controller.signal.aborted || !hasOpenDrawer()) {
        return;
      }
      const objectUrl = URL.createObjectURL(blob);
      if (state.requestSerial !== serialAtStart || controller.signal.aborted || !hasOpenDrawer()) {
        URL.revokeObjectURL(objectUrl);
        return;
      }
      thumbState.controller = null;
      thumbState.objectUrl = objectUrl;
      thumbState.objectSize = blob.size;
      thumbState.phase = 'ready';
      thumbState.error = '';
      touchThumbnailLru(item.asset_token);
      state.cachedThumbnailBytes += blob.size;
      evictThumbnailCache(item.asset_token);
      rerenderThumbnailViews(item.asset_token);
    } catch (error) {
      if (controller.signal.aborted) return;
      if (state.requestSerial !== serialAtStart || !hasOpenDrawer()) return;
      thumbState.controller = null;
      thumbState.phase = 'error';
      thumbState.error = error instanceof Error && error.message
        ? error.message
        : '缩略图读取失败';
      rerenderThumbnailViews(item.asset_token);
    } finally {
      state.activeThumbnailRequests.delete(requestKey);
      if (thumbState.controller === controller) {
        thumbState.controller = null;
      }
    }
  }

  async function openForSnapshot(snapshotId, options) {
    const normalized = validateSnapshotId(snapshotId);
    if (!normalized) return;
    openDrawer(options || {});
    if (!(options && options.preserveNotice)) setNotice('', '');

    const previousSnapshot = state.snapshotId;
    state.requestSerial += 1;
    const requestSerial = state.requestSerial;
    clearInFlightProjection();
    resetAllThumbnails();
    state.snapshotId = normalized;
    state.projection = null;
    setPhase('loading', '');
    if (previousSnapshot && previousSnapshot !== normalized) {
      setNotice('已切换到新的快照请求，旧结果会被忽略', 'stale');
    }
    render();

    const abortController = new AbortController();
    state.abortController = abortController;
    try {
      const payload = await loadProjection(normalized, abortController.signal);
      if (state.requestSerial !== requestSerial || abortController.signal.aborted || !hasOpenDrawer()) return;
      state.projection = payload;
      setPhase(payload.items.length ? 'ready' : 'empty', '');
      if (payload.review_required_references > 0) {
        setNotice('当前只展示安全摘要；如需确认绑定，请结合后续主流程人工核对', 'review');
      }
      render();
    } catch (error) {
      if (abortController.signal.aborted || state.requestSerial !== requestSerial) return;
      resetAllThumbnails();
      const message = error instanceof Error && error.message
        ? error.message
        : '素材审阅暂时不可用';
      if (error && typeof error === 'object' && error.code === 'malformed') {
        showToast(message, 'error');
        close();
        return;
      }
      setPhase('error', message);
      render();
    } finally {
      if (state.abortController === abortController) {
        state.abortController = null;
      }
    }
  }

  const api = Object.freeze({
    openForSnapshot(snapshotId, options) {
      return openForSnapshot(snapshotId, options || {});
    },
    close() {
      close();
    },
  });

  if (!Object.prototype.hasOwnProperty.call(window, MODULE_KEY)) {
    Object.defineProperty(window, MODULE_KEY, {
      value: api,
      configurable: false,
      enumerable: false,
      writable: false,
    });
  }
})();
