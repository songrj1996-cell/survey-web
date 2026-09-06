(() => {
  'use strict';

  const CAPABILITIES_ENDPOINT = '/api/questionnaire-sources/capabilities';
  const FAMILIES_ENDPOINT = '/api/questionnaire-sources/google-forms/families';
  const MAX_VARIANTS = 10;
  const state = {
    variants: [{ language: '', form_url: '' }],
    summary: null,
    busy: false,
    catalogBusy: false,
  };

  const el = (tag, className = '', text = '') => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };

  function structuredError(status, payload) {
    const detail = payload && typeof payload.detail === 'object'
      ? payload.detail
      : null;
    const message = detail?.message
      || (typeof payload?.detail === 'string' ? payload.detail : '')
      || `请求失败（HTTP ${status}）`;
    const error = new Error(message);
    error.code = typeof detail?.code === 'string' ? detail.code : '';
    return error;
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {
      credentials: 'same-origin',
      ...options,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    if (!response.ok) throw structuredError(response.status, payload);
    return payload;
  }

  function parseEditLink(value) {
    const raw = String(value || '').trim();
    let parsed;
    try {
      parsed = new URL(raw);
    } catch (_) {
      throw new Error('请输入完整的 Google Forms 编辑链接');
    }
    if (
      parsed.protocol !== 'https:'
      || parsed.hostname !== 'docs.google.com'
      || !/^\/forms\/d\/[A-Za-z0-9_-]+\/edit\/?$/.test(parsed.pathname)
    ) {
      throw new Error('仅支持 https://docs.google.com/forms/d/.../edit 编辑链接');
    }
    return parsed.pathname.split('/')[3];
  }

  function errorText(error) {
    const message = error instanceof Error ? error.message : '请求失败';
    return error?.code ? `${message}（${error.code}）` : message;
  }

  function setStatus(message, kind = '') {
    const host = document.getElementById('qsrc-status');
    if (!host) return;
    host.textContent = message;
    host.className = `qsrc-status${kind ? ` qsrc-status--${kind}` : ''}`;
  }

  function validate() {
    const title = String(document.getElementById('qsrc-title')?.value || '').trim();
    if (!title) throw new Error('请填写调研项目名称');
    if (state.variants.length < 1 || state.variants.length > MAX_VARIANTS) {
      throw new Error('请添加 1–10 个 Google Forms 版本');
    }
    const languages = new Set();
    const formIds = new Set();
    const variants = state.variants.map(item => {
      const language = String(item.language || '').trim().toLowerCase();
      const formUrl = String(item.form_url || '').trim();
      if (!/^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/.test(language)) {
        throw new Error('语言代码请使用 en、id、zh-cn 这类格式');
      }
      if (languages.has(language)) throw new Error('同一项目不能重复填写语言代码');
      languages.add(language);
      const formId = parseEditLink(formUrl);
      if (formIds.has(formId)) throw new Error('同一 Google Form 不能重复添加');
      formIds.add(formId);
      return { language, form_url: formUrl };
    });
    return { title, variants };
  }

  function renderVariants() {
    const host = document.getElementById('qsrc-variants');
    if (!host) return;
    host.replaceChildren();
    state.variants.forEach((item, index) => {
      const row = el('div', 'qsrc-variant');
      const languageLabel = el('label', 'qsrc-field');
      languageLabel.appendChild(el('span', 'qsrc-field__label', `语言 ${index + 1}`));
      const language = document.createElement('input');
      language.type = 'text';
      language.maxLength = 35;
      language.placeholder = index === 0 ? '例如 en' : '例如 id';
      language.value = item.language;
      language.dataset.qsrcField = 'language';
      language.dataset.qsrcIndex = String(index);
      languageLabel.appendChild(language);

      const linkLabel = el('label', 'qsrc-field qsrc-field--link');
      linkLabel.appendChild(el('span', 'qsrc-field__label', 'Google Forms 编辑链接'));
      const link = document.createElement('input');
      link.type = 'url';
      link.maxLength = 2048;
      link.placeholder = 'https://docs.google.com/forms/d/.../edit';
      link.value = item.form_url;
      link.dataset.qsrcField = 'form_url';
      link.dataset.qsrcIndex = String(index);
      linkLabel.appendChild(link);

      const remove = el('button', 'qsrc-remove', '移除');
      remove.type = 'button';
      remove.disabled = state.variants.length <= 1 || state.busy;
      remove.setAttribute('aria-label', `移除语言版本 ${index + 1}`);
      remove.addEventListener('click', () => {
        if (state.variants.length <= 1) return;
        state.variants.splice(index, 1);
        state.summary = null;
        renderVariants();
        renderSummary();
      });
      row.append(languageLabel, linkLabel, remove);
      host.appendChild(row);
    });
    host.querySelectorAll('input').forEach(input => {
      input.disabled = state.busy;
      input.addEventListener('input', event => {
        const index = Number(event.target.dataset.qsrcIndex);
        const field = event.target.dataset.qsrcField;
        if (state.variants[index] && (field === 'language' || field === 'form_url')) {
          state.variants[index][field] = event.target.value;
          state.summary = null;
          renderSummary();
        }
      });
    });
    const add = document.getElementById('qsrc-add-variant');
    if (add) {
      add.disabled = state.busy || state.variants.length >= MAX_VARIANTS;
      add.textContent = state.variants.length >= MAX_VARIANTS
        ? '已达到 10 个版本上限'
        : `添加语言版本（${state.variants.length}/10）`;
    }
  }

  function renderSummary() {
    const host = document.getElementById('qsrc-summary');
    if (!host) return;
    host.replaceChildren();
    const summary = state.summary;
    if (!summary) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    host.append(
      el('strong', '', summary.status === 'ready' ? '结构检查通过' : '结构需要复核'),
      el(
        'p',
        '',
        `${summary.variant_count} 个版本 · ${(summary.languages || []).join(' / ')}`
        + ` · ${summary.canonical_question_count} 道规范题`,
      ),
      el(
        'p',
        'qsrc-note',
        '开始分析后会读取最新回答；每个 Form 内按 responseId 防止 API 重复读取，不做跨 Form 的回答内容去重。',
      ),
    );
    if (Array.isArray(summary.diagnostics) && summary.diagnostics.length) {
      const list = el('ul', 'qsrc-diagnostics');
      summary.diagnostics.forEach(item => {
        list.appendChild(el(
          'li',
          '',
          `${item.language || '未记录语言'}：${item.message || item.code || '结构差异'}`,
        ));
      });
      host.appendChild(list);
    }
    const start = el('button', 'btn btn--primary', '读取最新回答并进入数据确认');
    start.type = 'button';
    start.disabled = summary.status !== 'ready' || state.busy;
    start.addEventListener('click', () => startAnalysis(summary.family_id));
    host.appendChild(start);
  }

  async function createFamily() {
    let payload;
    try {
      payload = validate();
    } catch (error) {
      setStatus(errorText(error), 'error');
      return;
    }
    state.busy = true;
    renderVariants();
    renderSummary();
    setStatus('正在只读读取各语言 Form，并建立统一题目映射…', 'loading');
    try {
      state.summary = await requestJson(FAMILIES_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      setStatus(
        state.summary.status === 'ready'
          ? '项目已保存，可以读取回答并开始分析。'
          : '项目已保存，但结构差异需要先处理。',
        state.summary.status === 'ready' ? 'success' : 'warning',
      );
      await loadCatalog();
    } catch (error) {
      setStatus(errorText(error), 'error');
    } finally {
      state.busy = false;
      renderVariants();
      renderSummary();
    }
  }

  async function startAnalysis(familyId) {
    if (!familyId || state.busy) return;
    state.busy = true;
    renderVariants();
    renderSummary();
    setStatus('正在分页读取回答，并创建统一分析会话…', 'loading');
    try {
      const session = await requestJson(
        `${FAMILIES_ENDPOINT}/${encodeURIComponent(familyId)}/analysis-sessions`,
        { method: 'POST' },
      );
      const ingress = window.surveySessionIngress;
      if (!ingress || typeof ingress.acceptGoogleFormsFamilySession !== 'function') {
        throw new Error('现有定性分析入口尚未就绪');
      }
      ingress.acceptGoogleFormsFamilySession(session);
      setStatus('统一分析会话已创建。', 'success');
    } catch (error) {
      setStatus(errorText(error), 'error');
    } finally {
      state.busy = false;
      renderVariants();
      renderSummary();
    }
  }

  async function refreshFamily(familyId) {
    if (!familyId || state.catalogBusy) return;
    state.catalogBusy = true;
    setStatus('正在刷新已保存项目的问卷结构…', 'loading');
    try {
      state.summary = await requestJson(
        `${FAMILIES_ENDPOINT}/${encodeURIComponent(familyId)}/refresh`,
        { method: 'POST' },
      );
      renderSummary();
      await loadCatalog();
      setStatus('项目结构已刷新。', 'success');
    } catch (error) {
      setStatus(errorText(error), 'error');
    } finally {
      state.catalogBusy = false;
    }
  }

  function renderCatalog(items) {
    const host = document.getElementById('qsrc-catalog-list');
    if (!host) return;
    host.replaceChildren();
    if (!items.length) {
      host.appendChild(el('p', 'qsrc-empty', '还没有已保存的 Google Forms 调研项目。'));
      return;
    }
    items.forEach(item => {
      const card = el('article', 'qsrc-project');
      const body = el('div', 'qsrc-project__body');
      body.append(
        el('strong', 'qsrc-project__title', item.title || '未命名项目'),
        el(
          'span',
          'qsrc-project__meta',
          `${(item.languages || []).join(' / ')} · ${item.variant_count} 个版本`
          + ` · ${item.canonical_question_count} 道规范题`,
        ),
      );
      const actions = el('div', 'qsrc-project__actions');
      const refresh = el('button', 'btn btn--ghost', '刷新结构');
      refresh.type = 'button';
      refresh.addEventListener('click', () => refreshFamily(item.family_id));
      const start = el('button', 'btn btn--primary', '继续分析');
      start.type = 'button';
      start.disabled = item.status !== 'ready';
      start.addEventListener('click', () => startAnalysis(item.family_id));
      actions.append(refresh, start);
      card.append(body, actions);
      host.appendChild(card);
    });
  }

  async function loadCatalog() {
    const host = document.getElementById('qsrc-catalog-list');
    if (host) host.replaceChildren(el('p', 'qsrc-empty', '正在读取已保存项目…'));
    try {
      const payload = await requestJson(`${FAMILIES_ENDPOINT}?limit=20`);
      renderCatalog(Array.isArray(payload?.items) ? payload.items : []);
    } catch (error) {
      if (host) {
        host.replaceChildren(el('p', 'qsrc-empty qsrc-empty--error', errorText(error)));
      }
    }
  }

  function buildPanel() {
    const mount = document.getElementById('survey-google-upload');
    const upload = document.getElementById('upload-zone');
    if (!mount || !upload) return null;

    const panel = el('section', 'qsrc-panel');
    panel.id = 'qsrc-google-family';
    panel.hidden = true;
    panel.setAttribute('aria-labelledby', 'qsrc-title-heading');

    const heading = el('div', 'qsrc-heading');
    const headingText = el('div');
    const h2 = el('h2', 'qsrc-heading__title', '直接连接多语言 Google Forms');
    h2.id = 'qsrc-title-heading';
    headingText.append(
      h2,
      el(
        'p',
        'qsrc-heading__desc',
        '支持 1–10 个语言版本，只读读取问卷和回答，统一进入现有定性分析。',
      ),
    );
    heading.appendChild(headingText);

    const titleLabel = el('label', 'qsrc-field');
    titleLabel.appendChild(el('span', 'qsrc-field__label', '调研项目名称'));
    const titleInput = document.createElement('input');
    titleInput.id = 'qsrc-title';
    titleInput.maxLength = 200;
    titleInput.placeholder = '例如：Chat Lobby 多语言调研';
    titleLabel.appendChild(titleInput);

    const variants = el('div', 'qsrc-variants');
    variants.id = 'qsrc-variants';

    const controls = el('div', 'qsrc-controls');
    const add = el('button', 'btn btn--ghost', '添加语言版本（1/10）');
    add.id = 'qsrc-add-variant';
    add.type = 'button';
    add.addEventListener('click', () => {
      if (state.variants.length >= MAX_VARIANTS) return;
      state.variants.push({ language: '', form_url: '' });
      state.summary = null;
      renderVariants();
      renderSummary();
    });
    const save = el('button', 'btn btn--primary', '检查结构并保存项目');
    save.id = 'qsrc-save-family';
    save.type = 'button';
    save.addEventListener('click', createFamily);
    controls.append(add, save);

    const status = el('div', 'qsrc-status');
    status.id = 'qsrc-status';
    status.setAttribute('aria-live', 'polite');

    const summary = el('div', 'qsrc-summary');
    summary.id = 'qsrc-summary';
    summary.hidden = true;

    const catalog = el('details', 'qsrc-catalog');
    catalog.open = true;
    catalog.append(
      el('summary', 'qsrc-catalog__title', '已保存调研项目'),
      Object.assign(el('div', 'qsrc-catalog__list'), { id: 'qsrc-catalog-list' }),
    );

    panel.append(heading, titleLabel, variants, controls, status, summary, catalog);
    mount.insertBefore(panel, upload);
    renderVariants();
    return panel;
  }

  async function initialize() {
    const panel = buildPanel();
    if (!panel) return;
    try {
      const capabilities = await requestJson(CAPABILITIES_ENDPOINT);
      if (
        capabilities?.google_forms_connection !== true
        || capabilities?.google_forms_unified_analysis !== true
      ) {
        return;
      }
      panel.hidden = false;
      await loadCatalog();
    } catch (_) {
      panel.hidden = true;
    }
  }

  initialize();
})();
