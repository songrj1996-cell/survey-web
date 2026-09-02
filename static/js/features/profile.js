/* 个人中心与个人 LLM Key 配置。 */
'use strict';

const profileState = {
  loaded: false,
  loading: false,
  saving: false,
  forced: false,
  activeTab: 'key',
  data: {
    required: false,
    personal_key_supported: false,
    configured: false,
    storage_ready: false,
    name: '',
    email: '',
  },
  usage: {
    loading: false,
    loaded: false,
    period: '30d',
    category: '',
    status: '',
    records: [],
    summary: null,
    nextOffset: null,
    generatedAt: '',
    error: '',
  },
};

const PROFILE_USAGE_CATEGORY_LABELS = {
  survey: '问卷',
  comment: '评论',
  interview: '访谈',
  annotate: '标注',
  other: '其他',
};

const PROFILE_USAGE_STATUS_LABELS = {
  running: '进行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

function profileErrorMessage(data, fallback) {
  const detail = data && data.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail.message === 'string') return detail.message;
  return fallback;
}

function profileAccountLabel() {
  return profileState.data.email || profileState.data.name || '当前飞书账号';
}

function profileUsageNumber(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function profileUsageDate(value) {
  const parsed = new Date(value || '');
  if (Number.isNaN(parsed.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(parsed);
}

function profileUsageTokenText(record) {
  const total = profileUsageNumber(record?.total_tokens);
  const missing = profileUsageNumber(record?.usage_missing_call_count);
  if (missing && !total) return '用量未完整返回';
  return `${missing ? '≥ ' : ''}${total.toLocaleString('zh-CN')} token`;
}

function renderProfileUsage() {
  const usage = profileState.usage;
  const summary = usage.summary || {};
  const total = profileUsageNumber(summary.total_tokens);
  const missing = profileUsageNumber(summary.usage_missing_call_count);

  $('profile-usage-total').textContent = missing && !total
    ? '未完整返回'
    : `${missing ? '≥ ' : ''}${total.toLocaleString('zh-CN')}`;
  $('profile-usage-input').textContent = profileUsageNumber(summary.input_tokens).toLocaleString('zh-CN');
  $('profile-usage-output').textContent = profileUsageNumber(summary.output_tokens).toLocaleString('zh-CN');
  $('profile-usage-tasks').textContent = profileUsageNumber(summary.task_count).toLocaleString('zh-CN');

  const incomplete = $('profile-usage-incomplete');
  incomplete.hidden = !missing;
  incomplete.textContent = missing
    ? `其中 ${missing.toLocaleString('zh-CN')} 次模型调用没有返回完整 usage，以上 Token 为已记录到的最低值。`
    : '';

  const records = Array.isArray(usage.records) ? usage.records : [];
  $('profile-usage-list').innerHTML = records.map(record => {
    const category = PROFILE_USAGE_CATEGORY_LABELS[record.category] || '其他';
    const status = PROFILE_USAGE_STATUS_LABELS[record.status] || '未知';
    const title = record.title
      ? `${record.action || 'AI 任务'} · ${record.title}`
      : (record.action || 'AI 任务');
    const models = Array.isArray(record.models_used) && record.models_used.length
      ? record.models_used.join('、')
      : '未记录';
    const fallbacks = Array.isArray(record.fallback_models_used) && record.fallback_models_used.length
      ? record.fallback_models_used.join('、')
      : '未使用';
    const historyAction = record.status === 'completed' && record.history_id
      ? `<button class="btn btn--ghost" type="button" data-profile-history-id="${esc(record.history_id)}">查看报告</button>`
      : '';
    return `
      <details class="profile-usage-record">
        <summary>
          <span class="profile-usage-record__time">${esc(profileUsageDate(record.started_at))}</span>
          <span class="profile-usage-record__name">
            <strong title="${esc(title)}">${esc(title)}</strong>
            <span>${esc(category)}</span>
          </span>
          <span class="profile-usage-record__tokens">${esc(profileUsageTokenText(record))}</span>
          <span class="profile-usage-status profile-usage-status--${esc(record.status || 'failed')}">${esc(status)}</span>
        </summary>
        <div class="profile-usage-record__details">
          <div class="profile-usage-detail"><span>使用模型</span><strong>${esc(models)}</strong></div>
          <div class="profile-usage-detail"><span>备用模型</span><strong>${esc(fallbacks)}</strong></div>
          <div class="profile-usage-detail"><span>模型调用</span><strong>${profileUsageNumber(record.call_count).toLocaleString('zh-CN')} 次</strong></div>
          <div class="profile-usage-detail"><span>输入 Token</span><strong>${profileUsageNumber(record.input_tokens).toLocaleString('zh-CN')}</strong></div>
          <div class="profile-usage-detail"><span>输出 Token</span><strong>${profileUsageNumber(record.output_tokens).toLocaleString('zh-CN')}</strong></div>
          <div class="profile-usage-detail"><span>缺失 usage</span><strong>${profileUsageNumber(record.usage_missing_call_count).toLocaleString('zh-CN')} 次</strong></div>
          ${historyAction ? `<div class="profile-usage-record__actions">${historyAction}</div>` : ''}
        </div>
      </details>`;
  }).join('');

  const empty = $('profile-usage-empty');
  empty.hidden = usage.loading || records.length > 0;
  empty.textContent = usage.error || '当前范围还没有可显示的模型用量记录。';
  const loadMore = $('profile-usage-load-more');
  loadMore.hidden = usage.nextOffset === null || usage.nextOffset === undefined;
  loadMore.disabled = usage.loading;
  loadMore.textContent = usage.loading ? '正在加载…' : '加载更多';
  $('profile-usage-synced').textContent = usage.generatedAt
    ? `最近同步：${profileUsageDate(usage.generatedAt)}`
    : '';
}

async function loadProfileUsage({ reset = true } = {}) {
  const usage = profileState.usage;
  if (usage.loading) return;
  usage.loading = true;
  if (reset) {
    usage.records = [];
    usage.nextOffset = null;
  }
  usage.error = '';
  renderProfileUsage();
  const params = new URLSearchParams({
    period: usage.period,
    offset: String(reset ? 0 : (usage.nextOffset || 0)),
    limit: '20',
  });
  if (usage.category) params.set('category', usage.category);
  if (usage.status) params.set('status', usage.status);
  try {
    const response = await fetch(`/api/profile/llm-usage?${params}`, { cache: 'no-store' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(profileErrorMessage(data, '读取用量记录失败'));
    usage.records = reset
      ? (data.records || [])
      : [...usage.records, ...(data.records || [])];
    usage.summary = data.summary || {};
    usage.nextOffset = data.next_offset ?? null;
    usage.generatedAt = data.generated_at || new Date().toISOString();
    usage.loaded = true;
  } catch (error) {
    usage.error = error.message || '读取用量记录失败';
    showToast(error.message || '读取用量记录失败', 'error');
  } finally {
    usage.loading = false;
    renderProfileUsage();
  }
}

function setProfileTab(tab, { load = true } = {}) {
  const selected = tab === 'usage' ? 'usage' : 'key';
  profileState.activeTab = selected;
  document.querySelectorAll('[data-profile-tab]').forEach(button => {
    const active = button.dataset.profileTab === selected;
    button.classList.toggle('profile-tab--active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  $('profile-key-panel').hidden = selected !== 'key';
  $('profile-usage-panel').hidden = selected !== 'usage';
  document.querySelector('.profile-modal__dialog')?.classList.toggle(
    'profile-modal__dialog--usage',
    selected === 'usage',
  );
  if (selected === 'usage' && load) {
    loadProfileUsage().catch(() => {});
  }
}

function renderProfile() {
  const data = profileState.data;
  const configured = Boolean(data.configured);
  const supported = Boolean(data.personal_key_supported);
  const storageReady = Boolean(data.storage_ready);
  const account = profileAccountLabel();
  const displayName = data.name || account;

  $('profile-account-text').textContent = data.email && data.name
    ? `${data.name} · ${data.email}`
    : account;
  $('profile-avatar').textContent = String(displayName || '我').trim().slice(0, 1).toUpperCase() || '我';
  $('profile-key-status').textContent = configured ? '已配置' : '未配置';
  $('profile-key-status').classList.toggle('profile-key-status--ready', configured);
  $('profile-key-field-label').textContent = configured ? '替换 API Key' : '填写 API Key';
  $('profile-key-input').placeholder = configured
    ? '输入新的 Key；已有 Key 不会在页面显示'
    : '输入AI Vital后台中，你的个人API KEY。';
  $('btn-profile-key-delete').hidden = !configured;
  $('btn-profile-key-save').disabled = profileState.saving || !supported || !storageReady;
  $('profile-key-input').disabled = profileState.saving || !supported || !storageReady;

  const notice = $('profile-key-notice');
  notice.className = 'profile-key-notice';
  if (!supported) {
    notice.textContent = '当前是免登录开发模式，继续使用服务端开发 Key；启用飞书强制登录后可配置个人 Key。';
  } else if (!storageReady) {
    notice.classList.add('profile-key-notice--error');
    notice.textContent = '服务端尚未配置个人 Key 加密主密钥，请联系管理员完成部署配置。';
  } else if (!configured) {
    notice.classList.add('profile-key-notice--required');
    notice.textContent = '请直接在下方填写你的 Key。保存成功后即可开始使用平台。';
  } else {
    notice.classList.add('profile-key-notice--success');
    notice.textContent = '后续 AI 任务只会使用当前账号保存的个人 Key。';
  }

  const cannotClose = Boolean(data.required && supported && !configured);
  profileState.forced = cannotClose;
  $('btn-profile-close').hidden = cannotClose;
}

function openProfileModal(force = false) {
  const modal = $('profile-modal');
  profileState.forced = force || profileState.forced;
  modal.hidden = false;
  modal.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  setProfileTab('key', { load: false });
  renderProfile();
  requestAnimationFrame(() => {
    if (!profileState.data.configured && !$('profile-key-input').disabled) {
      $('profile-key-input').focus();
    } else {
      $('profile-modal-title').focus?.();
    }
  });
}

function closeProfileModal() {
  if (profileState.forced) return;
  const modal = $('profile-modal');
  modal.hidden = true;
  modal.setAttribute('aria-hidden', 'true');
  document.body.style.removeProperty('overflow');
}

async function refreshProfile({ open = false } = {}) {
  if (profileState.loading) return profileState.data;
  profileState.loading = true;
  try {
    const response = await fetch('/api/profile', { cache: 'no-store' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(profileErrorMessage(data, '读取个人中心失败'));
    profileState.data = { ...profileState.data, ...data };
    profileState.loaded = true;
    renderProfile();
    if (open || (data.required && data.personal_key_supported && !data.configured)) {
      openProfileModal(!data.configured);
    }
    return profileState.data;
  } catch (error) {
    if (open) showToast(error.message, 'error');
    throw error;
  } finally {
    profileState.loading = false;
  }
}

window.syncProfileFromFeishuStatus = feishu => {
  profileState.data.name = feishu?.name || profileState.data.name;
  profileState.data.email = feishu?.email || profileState.data.email;
  if (profileState.loaded) renderProfile();
};

$('btn-feishu-login')?.addEventListener('click', async () => {
  if (!state.feishu.configured) {
    showToast('服务端未配置飞书应用（FEISHU_APP_ID/SECRET/REDIRECT_URI）', 'error');
    return;
  }
  if (!state.feishu.logged_in) {
    window.location.href = `/api/feishu/login?next=${encodeURIComponent(location.pathname)}`;
    return;
  }
  await refreshProfile({ open: true }).catch(() => {});
});

$('profile-key-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  if (profileState.saving) return;
  const input = $('profile-key-input');
  const apiKey = input.value.trim();
  if (apiKey.length < 8) {
    showToast('请填写有效的 LLM API Key', 'error');
    input.focus();
    return;
  }

  profileState.saving = true;
  $('btn-profile-key-save').textContent = '正在验证…';
  renderProfile();
  try {
    const response = await fetch('/api/profile/llm-key', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: apiKey }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(profileErrorMessage(data, '验证或保存失败'));
    input.value = '';
    profileState.data.configured = true;
    profileState.data.updated_at = data.updated_at || '';
    profileState.forced = false;
    profileState.usage.loaded = false;
    showToast('个人 LLM Key 已验证并保存', 'success');
    renderProfile();
    closeProfileModal();
  } catch (error) {
    showToast(error.message, 'error', 8000);
    input.focus();
  } finally {
    profileState.saving = false;
    $('btn-profile-key-save').textContent = '验证并保存';
    renderProfile();
  }
});

$('btn-profile-key-toggle')?.addEventListener('click', () => {
  const input = $('profile-key-input');
  const revealing = input.type === 'password';
  input.type = revealing ? 'text' : 'password';
  $('btn-profile-key-toggle').textContent = revealing ? '隐藏' : '显示';
  $('btn-profile-key-toggle').setAttribute('aria-label', revealing ? '隐藏 API Key' : '显示 API Key');
  input.focus();
});

$('btn-profile-key-delete')?.addEventListener('click', async () => {
  if (!window.confirm('确定删除当前账号保存的个人 LLM Key 吗？删除后将无法开始新的 AI 任务。')) return;
  try {
    const response = await fetch('/api/profile/llm-key', { method: 'DELETE' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(profileErrorMessage(data, '删除失败'));
    profileState.data.configured = false;
    showToast('个人 LLM Key 已删除', 'info');
    renderProfile();
    openProfileModal(true);
  } catch (error) {
    showToast(error.message, 'error');
  }
});

$('btn-profile-logout')?.addEventListener('click', async () => {
  if (!window.confirm(`确定退出 ${profileAccountLabel()} 的飞书登录吗？`)) return;
  try {
    await fetch('/api/feishu/logout', { method: 'POST' });
  } finally {
    window.location.href = '/login';
  }
});

$('btn-profile-close')?.addEventListener('click', closeProfileModal);
document.querySelectorAll('[data-profile-close]').forEach(node => {
  node.addEventListener('click', closeProfileModal);
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !$('profile-modal').hidden) closeProfileModal();
});

document.querySelectorAll('[data-profile-tab]').forEach(button => {
  button.addEventListener('click', () => setProfileTab(button.dataset.profileTab));
});

document.querySelectorAll('[data-profile-period]').forEach(button => {
  button.addEventListener('click', () => {
    const period = button.dataset.profilePeriod || '30d';
    if (profileState.usage.period === period) return;
    profileState.usage.period = period;
    document.querySelectorAll('[data-profile-period]').forEach(item => {
      item.classList.toggle('is-active', item === button);
    });
    loadProfileUsage().catch(() => {});
  });
});

$('profile-usage-category')?.addEventListener('change', event => {
  profileState.usage.category = event.target.value || '';
  loadProfileUsage().catch(() => {});
});

$('profile-usage-status')?.addEventListener('change', event => {
  profileState.usage.status = event.target.value || '';
  loadProfileUsage().catch(() => {});
});

$('profile-usage-load-more')?.addEventListener('click', () => {
  loadProfileUsage({ reset: false }).catch(() => {});
});

$('profile-usage-list')?.addEventListener('click', async event => {
  const button = event.target.closest('[data-profile-history-id]');
  if (!button) return;
  event.preventDefault();
  const historyId = button.dataset.profileHistoryId || '';
  if (!historyId || typeof openHistoryEntry !== 'function') {
    showToast('暂时无法打开对应报告', 'error');
    return;
  }
  closeProfileModal();
  try {
    await openHistoryEntry(historyId);
  } catch (error) {
    showToast(error.message || '打开报告失败', 'error');
  }
});

refreshProfile().catch(() => {});
