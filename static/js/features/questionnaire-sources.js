'use strict';

(function initQuestionnaireSourcePanel() {
  const STYLE_ID = 'qsrc-stylesheet';
  const PANEL_ID = 'qsrc-panel';
  const INPUT_PREFIX = 'qsrc-input-';
  const MAX_IMAGE_COUNT = 20;
  const CAPABILITIES_URL = '/api/questionnaire-sources/capabilities';
  const HTTP_HIDE_STATUSES = new Set([401, 403, 404]);
  const CAPABILITY_KEYS = [
    'snapshot_package_upload',
    'bested_original_questionnaire_upload',
    'screenshot_material_upload',
    'pdf_material_upload',
  ];

  const SOURCE_DEFS = [
    {
      key: 'snapshot_package_upload',
      type: 'snapshot',
      endpoint: '/api/questionnaire-sources/snapshots',
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
    capabilityRequestSerial: 0,
    capabilityAbortController: null,
  };

  let panel = null;
  let cardsHost = null;
  let refreshButton = null;

  function ensureStylesheet() {
    if (document.getElementById(STYLE_ID)) return;
    const link = document.createElement('link');
    link.id = STYLE_ID;
    link.rel = 'stylesheet';
    link.href = '/static/questionnaire-sources.css?v=1';
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

  function getCardState(type) {
    if (!panelState.cardStates[type]) {
      panelState.cardStates[type] = {
        files: [],
        phase: 'idle',
        message: '',
        summary: null,
        requestSerial: 0,
        abortController: null,
      };
    }
    return panelState.cardStates[type];
  }

  function mountPanel() {
    if (panel && cardsHost && refreshButton) return true;
    const uploadArea = document.getElementById('upload-area');
    const bestedUpload = document.getElementById('survey-bested-upload');
    if (!uploadArea || !bestedUpload) return false;

    const existing = document.getElementById(PANEL_ID);
    if (existing) {
      panel = existing;
      cardsHost = existing.querySelector('.qsrc-panel__cards');
      refreshButton = existing.querySelector('.qsrc-panel__refresh');
      return !!(cardsHost && refreshButton);
    }

    panel = el('section', 'qsrc-panel');
    panel.id = PANEL_ID;
    panel.hidden = true;
    panel.setAttribute('aria-labelledby', 'qsrc-panel-title');

    const head = el('div', 'qsrc-panel__head');
    const titleWrap = el('div', 'qsrc-panel__title-wrap');
    titleWrap.append(
      el('div', 'qsrc-panel__eyebrow', '本地问卷快照'),
      Object.assign(el('h2', 'qsrc-panel__title', '单独保存本地问卷来源'), { id: 'qsrc-panel-title' }),
      el('p', 'qsrc-panel__desc', '支持把本地问卷文件或材料保存为独立快照，便于后续单独复用。这里保存的快照不会自动用于当前报告。'),
    );

    refreshButton = el('button', 'qsrc-panel__refresh', '刷新能力');
    refreshButton.type = 'button';
    refreshButton.addEventListener('click', () => refresh());
    head.append(titleWrap, refreshButton);

    cardsHost = el('div', 'qsrc-panel__cards');
    const disclaimer = el('div', 'qsrc-card__disclaimer', '提示：本区域只负责独立保存本地问卷快照。保存成功后，需要在后续单独选择或导入，当前问卷分析流程不会自动改用这些快照。');
    const foot = el('div', 'qsrc-panel__foot', '支持能力由服务端显式声明；未开放的入口不会展示。');

    panel.append(head, cardsHost, disclaimer, foot);
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
    return normalized;
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
      message: def.type === 'snapshot' || def.type === 'bested'
        ? '已保存独立快照，不会自动用于当前报告'
        : '已保存独立快照，当前报告不会自动引用',
      extra: { label: '保存成功', detail },
    };
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
    resetButton.disabled = cardState.phase === 'loading' || !fileCount;

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

    const input = document.createElement('input');
    input.id = `${INPUT_PREFIX}${def.type}`;
    input.type = 'file';
    input.accept = def.accept;
    input.multiple = !!def.multiple;
    input.className = 'qsrc-sr-only';
    input.addEventListener('change', () => {
      const files = Array.from(input.files || []);
      setFiles(def, files);
    });

    const picker = el('div', 'qsrc-card__picker');
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

    const footer = el('div', 'qsrc-card__footer');
    const submitButton = el('button', 'qsrc-btn qsrc-btn--primary', def.submitLabel);
    submitButton.type = 'button';
    submitButton.dataset.qsrcAction = 'submit';
    submitButton.addEventListener('click', () => upload(def));
    const resetButton = el('button', 'qsrc-btn qsrc-btn--ghost', '重置');
    resetButton.type = 'button';
    resetButton.dataset.qsrcAction = 'reset';
    resetButton.addEventListener('click', () => reset(def.type));
    footer.append(submitButton, resetButton);

    const status = el('div', 'qsrc-card__status');
    status.setAttribute('aria-live', 'polite');

    card.append(top, input, picker, footer, status);
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

  function responseErrorMessage(response, fallback) {
    return response.json()
      .then(parsed => (
        parsed && typeof parsed.detail === 'string' && parsed.detail.trim()
          ? parsed.detail.trim()
          : fallback
      ))
      .catch(() => fallback);
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
        panelState.capabilities = null;
        setPanelVisibility(false);
        return;
      }
      panelState.capabilities = result.capabilities;
      renderCards();
      setPanelVisibility(supportedSources().length > 0);
    } catch (error) {
      if (abortController.signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
        return;
      }
      panelState.capabilities = null;
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
    if (!mountPanel()) return;
    refresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
  } else {
    bootstrap();
  }
})();
