/* Quick report controls share the existing report workspace and source Markdown. */
'use strict';

let reportStyleRequestSerial = 0;

function selectedReportStyle() {
  const selection = state.reportStyleSelection;
  return selection.sessionId === state.sessionId && selection.enabled && selection.value === 'quick'
    ? 'quick' : 'full';
}

function lockReportStyleSelection(locked) {
  state.reportStyleSelection.locked = !!locked;
  const picker = $('report-style-picker');
  if (picker) picker.disabled = !!locked;
}

async function loadReportStyleOptions() {
  const sessionId = state.sessionId;
  const serial = ++reportStyleRequestSerial;
  const picker = $('report-style-picker');
  if (!picker) return;
  if (state.reportStyleSelection.sessionId !== sessionId) {
    state.reportStyleSelection = { sessionId, value: 'full', enabled: false, locked: false };
    picker.querySelector('[value="full"]').checked = true;
  }
  lockReportStyleSelection(false);
  picker.hidden = !state.reportStyleSelection.enabled;
  if (!sessionId) return;
  try {
    const response = await fetch(`/api/report/${encodeURIComponent(sessionId)}/options`);
    if (!response.ok) throw new Error('报告模式暂不可用');
    const options = await response.json();
    if (serial !== reportStyleRequestSerial || sessionId !== state.sessionId || state.reportStyleSelection.locked) return;
    const selection = state.reportStyleSelection;
    const firstLoad = !selection.enabled;
    selection.enabled = options.quick_enabled === true;
    if (!selection.enabled) selection.value = 'full';
    else if (firstLoad) selection.value = options.report_style === 'quick' ? 'quick' : 'full';
    picker.hidden = !selection.enabled;
    picker.querySelector(`[value="${selection.value}"]`).checked = true;
  } catch (error) {
    if (serial !== reportStyleRequestSerial || sessionId !== state.sessionId || state.reportStyleSelection.locked) return;
    state.reportStyleSelection.enabled = false;
    state.reportStyleSelection.value = 'full';
    picker.hidden = true;
  }
}

$('report-style-picker')?.addEventListener('change', event => {
  if (state.reportStyleSelection.locked || !state.reportStyleSelection.enabled) return;
  state.reportStyleSelection.value = event.target.value === 'quick' ? 'quick' : 'full';
});

function revealQuickTarget(target) {
  for (let ancestor = target?.parentElement; ancestor; ancestor = ancestor.parentElement) {
    if (ancestor.tagName === 'DETAILS') ancestor.open = true;
  }
}

function scrollQuickTarget(target) {
  if (!target) return;
  revealQuickTarget(target);
  const body = document.querySelector('#panel-5 .report-document .report-body');
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const behavior = reduced ? 'instant' : 'smooth';
  if (body) {
    body.scrollTo({ top: target.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop - 24, behavior });
  } else target.scrollIntoView({ behavior, block: 'start' });
  target.tabIndex = -1;
  target.focus({ preventScroll: true });
}

function renderQuickReportNavigation(md, ctx) {
  const content = $('report-content');
  const quick = ctx?.reportStyle === 'quick';
  content.classList.toggle('quick-report', quick);
  if (!quick) return;
  const headings = Array.from(content.querySelectorAll('h2'));
  const layers = ['核心判断', '关键发现', '发现与证据附录'].map(label => headings.find(h => h.textContent.trim() === label));
  if (layers.some(h => !h)) return;
  layers.forEach((h, i) => { h.id = `quick-layer-${i}`; });
  const appendixHeading = layers[2];
  const appendix = document.createElement('details');
  appendix.className = 'quick-appendix';
  const summary = document.createElement('summary');
  appendixHeading.before(appendix);
  summary.append(appendixHeading);
  const hint = document.createElement('span');
  hint.textContent = '全部发现与原文 · 按需展开';
  summary.append(hint);
  appendix.append(summary);
  while (appendix.nextSibling) appendix.append(appendix.nextSibling);

  const targets = new Map();
  appendix.querySelectorAll('h3').forEach(heading => {
    const match = heading.textContent.match(/^\[(E\d+)\]\s/);
    if (!match) return;
    heading.id = `quick-evidence-${match[1]}`;
    const details = document.createElement('details');
    details.className = 'quick-evidence';
    heading.before(details);
    const row = document.createElement('summary');
    row.append(heading);
    details.append(row);
    while (details.nextSibling && !(details.nextSibling.nodeType === 1 && details.nextSibling.tagName === 'H3')) {
      details.append(details.nextSibling);
    }
    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'quick-return';
    back.textContent = '返回引用处';
    back.hidden = true;
    row.after(back);
    targets.set(match[1], { heading, details, back });
  });

  targets.forEach(target => target.back.addEventListener('click', () => {
    if (target.reference?.isConnected) scrollQuickTarget(target.reference);
    else scrollQuickTarget(layers[1]);
  }));
  // Only generated body references and the inventory are interactive. Player quotes stay literal.
  const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (/\[E\d+\]/.test(node.textContent) && !node.parentElement.closest('h1,h2,h3,h4,blockquote,code,a,button,.quick-evidence')) textNodes.push(node);
  }
  textNodes.forEach(node => {
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    for (const match of node.textContent.matchAll(/\[(E\d+)\]/g)) {
      const target = targets.get(match[1]);
      if (!target) continue;
      fragment.append(document.createTextNode(node.textContent.slice(cursor, match.index)));
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'quick-reference';
      button.textContent = match[0];
      button.setAttribute('aria-label', `查看证据 ${match[1]}`);
      button.addEventListener('click', () => {
        target.reference = button;
        target.details.open = true;
        target.back.hidden = false;
        scrollQuickTarget(target.heading);
      });
      fragment.append(button);
      cursor = match.index + match[0].length;
    }
    fragment.append(document.createTextNode(node.textContent.slice(cursor)));
    node.replaceWith(fragment);
  });

  const navigation = document.createElement('nav');
  navigation.className = 'quick-report-nav';
  navigation.setAttribute('aria-label', '快速报告阅读层次');
  const intro = document.createElement('div');
  intro.className = 'quick-report-nav__intro';
  const badge = document.createElement('span');
  badge.className = 'quick-report-badge';
  badge.textContent = '快速模式';
  const copy = document.createElement('span');
  copy.textContent = '先看判断，再看发现，证据按需展开';
  intro.append(badge, copy);
  navigation.append(intro);
  const links = document.createElement('div');
  links.className = 'quick-report-nav__links';
  layers.forEach((heading, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = `${String(index + 1).padStart(2, '0')}  ${heading.textContent}`;
    button.addEventListener('click', () => {
      if (index === 2) appendix.open = true;
      scrollQuickTarget(heading);
    });
    links.append(button);
  });
  navigation.append(links);
  content.prepend(navigation);
}
