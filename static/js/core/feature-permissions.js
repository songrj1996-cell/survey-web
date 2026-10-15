// Server-supplied permission catalog is the single source for menu and checkbox labels.
window.PlatformPermissions = (() => {
  let catalog = [], levels = {}, settingsVisible = false;
  const allowed = key => Object.prototype.hasOwnProperty.call(levels, key);
  const canUse = key => levels[key] === 'use';
  function apply(identity) {
    catalog = identity.feature_catalog || [];
    levels = identity.permission_levels || Object.fromEntries((identity.perms || []).map(key => [key, 'use']));
    const navigation = document.querySelector('.sidebar__nav');
    settingsVisible = false;
    catalog.forEach(group => {
      // Settings stay inside their drawer; the main sidebar has one footer entry.
      if (group.key === 'settings') {
        group.children.forEach(feature => {
          const show = allowed(feature.key) && (!feature.admin_only || identity.is_admin);
          document.querySelectorAll(`.settings-nav__item[data-stab="${feature.tab}"]`).forEach(tab => {
            tab.style.display = show ? '' : 'none';
          });
          if (show && (identity.is_admin || canUse(feature.key))) settingsVisible = true;
        });
        return;
      }
      let section = navigation.querySelector(`[data-feature-group="${group.key}"]`);
      if (!section) {
        section = document.createElement('section');
        section.className = 'platform-nav-group';
        section.dataset.featureGroup = group.key;
        const title = document.createElement('div');
        title.className = 'platform-nav-title'; title.textContent = group.label;
        section.append(title); navigation.append(section);
      }
      let visible = 0;
      group.children.forEach(feature => {
        let item;
        item = document.getElementById(`nav-${feature.key}`);
        if (item) section.append(item);
        const show = allowed(feature.key) && (!feature.admin_only || identity.is_admin);
        if (item) item.style.display = show ? '' : 'none';
        if (show) visible++;
      });
      section.hidden = !visible;
    });
    const settingsEntry = document.getElementById('nav-settings');
    if (settingsEntry) settingsEntry.hidden = !settingsVisible;
    ['settings-nav-admin-label','settings-nav-admin-sep'].forEach(id => { const el = document.getElementById(id); if (el) el.style.display = 'none'; });
    return catalog.flatMap(group => group.children.filter(f => !f.tab && allowed(f.key)).map(f => f.key));
  }
  function tree(container, selected = [], disabled = false) {
    container.replaceChildren();
    catalog.forEach(group => {
      const children = group.children.filter(feature => !feature.admin_only);
      if (!children.length) return;
      const fieldset = document.createElement('fieldset'); fieldset.className = 'permission-tree-group';
      const legend = document.createElement('legend'), parentLabel = document.createElement('label');
      const parent = document.createElement('input'); parent.type = 'checkbox'; parent.disabled = disabled;
      parentLabel.append(parent, ' ' + group.label); legend.append(parentLabel); fieldset.append(legend);
      const countLabel = document.createElement('small'); countLabel.className = 'permission-count'; parentLabel.append(countLabel);
      children.forEach(feature => {
        const label = document.createElement('label'), input = document.createElement('input');
        input.type = 'checkbox'; input.value = feature.key; input.dataset.permission = feature.key;
        input.checked = selected.includes(feature.key); input.disabled = disabled;
        label.append(input, ' ' + feature.label); fieldset.append(label);
      });
      const inputs = [...fieldset.querySelectorAll('[data-permission]')];
      const update = () => { const count = inputs.filter(input => input.checked).length; parent.checked = count === inputs.length; parent.indeterminate = count > 0 && count < inputs.length; countLabel.textContent = `${count}/${inputs.length}`; };
      parent.onchange = () => { inputs.forEach(input => { input.checked = parent.checked; }); update(); };
      inputs.forEach(input => input.onchange = update); update(); container.append(fieldset);
    });
  }
  return {apply, allowed, canUse, tree, canOpenSettings: () => settingsVisible, catalog: () => catalog, selected: element => [...element.querySelectorAll('[data-permission]:checked')].map(input => input.value)};
})();

window.workflowTime = value => {
  const raw = String(value || '');
  const timestamp = Date.parse(raw && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? raw + 'Z' : raw);
  return Number.isFinite(timestamp) ? new Date(timestamp + 8 * 3600000).toISOString().slice(0, 19).replace('T', ' ') + ' UTC+8' : '时间未记录';
};

window.workflowConfirm = (message, acceptLabel = '放弃并继续') => new Promise(resolve => {
  const dialog = document.createElement('dialog');
  dialog.className = 'workflow-dialog workflow-confirm';
  dialog.setAttribute('aria-label', '确认当前操作');
  dialog.innerHTML = '<h2>请确认</h2><p class="workflow-confirm-message"></p><div class="workflow-toolbar"><button type="button" class="btn btn--ghost" data-answer="stay">继续编辑</button><button type="button" class="btn btn--primary" data-answer="continue"></button></div>';
  dialog.querySelector('.workflow-confirm-message').textContent = message;
  dialog.querySelector('[data-answer="continue"]').textContent = acceptLabel;
  dialog.querySelectorAll('[data-answer]').forEach(button => button.onclick = () => dialog.close(button.dataset.answer));
  dialog.addEventListener('close', () => { const accepted = dialog.returnValue === 'continue'; dialog.remove(); resolve(accepted); }, {once: true});
  document.body.append(dialog); dialog.showModal();
});

window.workflowRequest = async (url, {method = 'GET', body, ...options} = {}) => {
  const form = body instanceof FormData;
  const response = await fetch(url, {method, ...options, ...(body === undefined ? {} : {body: form ? body : JSON.stringify(body)}),
    headers: {...(form || body === undefined ? {} : {'Content-Type':'application/json'}), ...(options.headers || {})}});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail;
    const error = new Error(typeof detail === 'string' ? detail : detail?.message || (Array.isArray(detail) ? '填写内容不完整或格式无效，请检查必填项' : '操作未完成，请重试'));
    error.detail = detail; error.status = response.status; throw error;
  }
  return payload;
};
