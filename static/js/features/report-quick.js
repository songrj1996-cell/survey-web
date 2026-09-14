// Early report modes, question sources and quick-result lifecycle.
'use strict';
let reportStyleRequestSerial = 0;
function selectedReportStyle() { return state.reportMode === 'quick' ? 'quick' : 'full'; }
function lockReportStyleSelection(locked) { state.reportStyleSelection.locked = !!locked; }
async function loadReportStyleOptions() {
  const sessionId=state.sessionId, serial=++reportStyleRequestSerial;
  if(!sessionId)return;
  if(state.reportStyleSelection.sessionId!==sessionId) state.reportStyleSelection={sessionId,value:'full',enabled:false,locked:false};
  try {
    const response=await fetch(`/api/report/${sessionId}/options`);
    const options=await response.json();
    if(!response.ok)throw new Error(options.detail||'无法读取报告方式');
    if(serial!==reportStyleRequestSerial||sessionId!==state.sessionId)return;
    state.reportStyleSelection.enabled=options.quick_enabled===true;
    state.reportStyleSelection.value=selectedReportStyle();
  } catch(error) {
    if(serial!==reportStyleRequestSerial||sessionId!==state.sessionId)return;
    state.reportStyleSelection.enabled=false;
  }
  if(typeof renderSurveyFocus==='function')renderSurveyFocus();
}
function revealQuickTarget(target) {
  for(let ancestor=target?.parentElement;ancestor;ancestor=ancestor.parentElement)if(ancestor.tagName==='DETAILS')ancestor.open=true;
}
function scrollQuickTarget(target) {
  if(!target)return;
  revealQuickTarget(target);
  target.scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});
  target.tabIndex=-1;target.focus({preventScroll:true});
}
function reportModeLabel(mode) {return {quick:'快速总结',insight:'观点洞察',statistics:'统计解读'}[mode]||'观点洞察';}
function quickEvidenceItems(question, finding = null) {
  const allowed = new Set((finding ? [finding] : question?.findings || []).flatMap(item => item.evidence_ids || []).map(String));
  return (question?.sources || []).filter(item => allowed.has(String(item.response_id))).map(item => ({
    response_id: String(item.response_id), text: String(item.text || ''),
    question: question.question || '', question_key: question.question_key || '',
    profile: item.profile || {}, ids: item.ids || {},
  }));
}
function quickReferenceText(text) {
  return String(text || '').replace(/\s+/g, ' ').trim().normalize('NFC')
    .replace(/^反复出现(?= · |：)/, '反复提及').replace(' · 风险待核实：', ' · 风险：');
}
const quickFrequencyOrder = ['反复提及','部分提及','零散提及','暂无法判断'];
function quickFindingFrequency(finding) { return finding.frequency === '反复出现' ? '反复提及' : finding.frequency; }
function quickFindingBody(finding) {
  const text = String(finding.text || '');
  if (quickFindingFrequency(finding) !== '零散提及') return text;
  return text.replace(/^(?:其他)?零散(?:建议|意见|反馈)(?:包括)?[：:]\s*/, '') || text;
}
function quickQuestionParts(question) {
  const text = String(question || '');
  const match = text.match(/^(.*?)\s*【((?:推定适用于|当前回答分布主要对应)[\s\S]*)】$/);
  return match ? {title:match[1].trim(),condition:match[2]} : {title:text,condition:''};
}
function renderQuickQuestionConditions(content) {
  content.querySelectorAll('h2').forEach(heading => {
    const full = heading.dataset.quickTitle || heading.textContent;
    const parts = quickQuestionParts(full);
    if (!parts.condition) return;
    const controls = [...heading.querySelectorAll('[data-quick-inline-reference]')];
    heading.dataset.quickFullTitle = full;
    heading.dataset.quickTitle = parts.title;
    heading.textContent = parts.title;
    controls.forEach(button => heading.append(button));
    const note = document.createElement('p'); note.className = 'quick-branch-note';
    note.dataset.quickBranchNote = 'true'; note.textContent = parts.condition;
    heading.after(note);
  });
}
function renderQuickInlineReferences(content, ctx) {
  content.querySelectorAll('[data-quick-inline-reference]').forEach(button => button.remove());
  content.querySelectorAll('[data-quick-branch-note]').forEach(note => note.remove());
  content.querySelectorAll('h2').forEach(heading => {
    if (heading.dataset.quickFullTitle) heading.textContent = heading.dataset.quickFullTitle;
    delete heading.dataset.quickFullTitle; delete heading.dataset.quickTitle;
    delete heading.dataset.quickQuestionKey;
  });
  if (!(ctx?.reportMode === 'quick' || ctx?.reportStyle === 'quick')) return;
  const questions = ctx.quickSummary?.questions || [];
  const headings = [...content.querySelectorAll('h2')];
  const sections = [
    ...(ctx.quickSummary?.objective_stats?.sections || []).map(question => ({question, objective:true})),
    ...questions.map(question => ({question, objective:false})),
  ].sort((a,b) => (Number.isInteger(a.question.source_order) ? a.question.source_order : Infinity)
    - (Number.isInteger(b.question.source_order) ? b.question.source_order : Infinity));
  // Match the complete section order and text. Never guess references from a
  // similar heading or attach a stale finding after the Markdown was edited.
  if (headings.length !== sections.length || sections.some(({question,objective},index) => {
    const label = !objective && (question.question_label || question.question_number);
    return quickReferenceText(headings[index].textContent) !== quickReferenceText(`${label ? label+' ' : ''}${question.question || '未命名题目'}`);
  })) return;
  const renderedVersion = activeVersionNumber(ctx), renderedId = activeReportId();
  const makeButton = (label, onClick) => {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'quick-inline-reference';
    button.dataset.quickInlineReference = 'true'; button.textContent = label;
    button.addEventListener('click', () => {
      if (activeReportCtx() === ctx && activeVersionNumber(ctx) === renderedVersion && activeReportId() === renderedId) onClick();
    });
    return button;
  };
  sections.forEach(({question,objective},index) => {
    const heading = headings[index];
    heading.dataset.quickQuestionKey = question.question_key;
    heading.dataset.quickTitle = heading.textContent;
    if (objective) return;
    const sectionButton = makeButton('查看本题原文', () => openReportSources(question.question_key, question.question, 'all'));
    sectionButton.setAttribute('aria-label', `${question.question}：查看本题原文`);
    heading.append(sectionButton);
    if (question.status !== 'complete') return;
    const blocks = [];
    for (let element = heading.nextElementSibling; element && element !== headings[index+1]; element = element.nextElementSibling) blocks.push(element);
    const lists = blocks.filter(element => element.tagName === 'UL' || element.tagName === 'OL');
    const items = lists.flatMap(list => [...list.children]);
    const findings = question.findings || [];
    if (!findings.length || items.length !== findings.length) return;
    const ordered = findings.map((finding,i) => ({finding,index:i})).sort((a,b) =>
      quickFrequencyOrder.indexOf(quickFindingFrequency(a.finding)) - quickFrequencyOrder.indexOf(quickFindingFrequency(b.finding)));
    const legacyMatch = findings.every((finding,i) => quickReferenceText(items[i].textContent) ===
      quickReferenceText(`${finding.frequency}${finding.risk ? ' · 风险' : ''}：${finding.text}`));
    const groupedMatch = ordered.every(({finding},i) => quickReferenceText(items[i].textContent) ===
      quickReferenceText(`${finding.risk ? '【风险】' : ''}${quickFindingBody(finding)}`));
    if (!legacyMatch && !groupedMatch) return;
    for (const frequency of quickFrequencyOrder) {
      const group = ordered.filter(({finding}) => quickFindingFrequency(finding) === frequency);
      if (!group.length) continue;
      const label = document.createElement('p'); label.className = 'quick-frequency-title';
      const strong = document.createElement('strong'); strong.textContent = `${frequency}：`; label.append(strong);
      const list = document.createElement('ol'); list.className = 'quick-frequency-list';
      group.forEach(({finding,index: findingIndex}) => {
        const item = document.createElement('li');
        if (finding.risk) {
          const risk = document.createElement('strong'); risk.className = 'quick-risk-label'; risk.textContent = '【风险】'; item.append(risk);
        }
        const text = document.createElement('span'); text.textContent = quickFindingBody(finding); item.append(text);
        if (quickEvidenceItems(question, finding).length) {
          const button = makeButton('查看依据', () => openReportSources(question.question_key, question.question, 'evidence', finding));
          button.setAttribute('aria-label', `${question.question}，观点 ${findingIndex+1}：查看依据`);
          item.append(button);
        }
        list.append(item);
      });
      lists[0].before(label, list);
    }
    blocks.filter(element => element.tagName === 'P' && quickFrequencyOrder.some(frequency =>
      quickReferenceText(element.textContent) === `${frequency}：`)).forEach(element => element.remove());
    lists.forEach(list => list.remove());
  });
}
function replaceQuickHeadingTag(heading, tag) {
  const replacement = document.createElement(tag);
  [...heading.attributes].forEach(attribute => replacement.setAttribute(attribute.name, attribute.value));
  // Move nodes to preserve source-button listeners and focus targets.
  while (heading.firstChild) replacement.append(heading.firstChild);
  heading.replaceWith(replacement);
  return replacement;
}
function resetQuickOutline(content) {
  content.querySelectorAll('[data-quick-outline-generated]').forEach(node => node.remove());
  content.querySelectorAll('[data-quick-outline-leaf]').forEach(heading => {
    heading.removeAttribute('data-quick-outline-leaf');
    replaceQuickHeadingTag(heading, 'h2');
  });
}
function renderQuickOutline(content, ctx) {
  const outline = ctx?.quickOutline;
  if (!(ctx?.reportMode === 'quick' || ctx?.reportStyle === 'quick') || outline?.schema_version !== 1) return;
  const headings = [...content.querySelectorAll('h2')];
  const keys = outline.question_keys, groups = outline.groups;
  if (!Array.isArray(keys) || !Array.isArray(groups) || !keys.length || !groups.length
      || new Set(keys).size !== keys.length || headings.length !== keys.length
      || keys.some((key, index) => typeof key !== 'string' || !/^\d+(?::\d+)*$/.test(key)
        || headings[index].dataset.quickQuestionKey !== key)
      || groups.some(group => !group || !Array.isArray(group.question_keys) || !group.question_keys.length
        || typeof group.title !== 'string' || typeof group.note !== 'string' || !/^quick-group-\d+$/.test(group.id))
      || new Set(groups.map(group => group.id)).size !== groups.length) return;
  const groupedKeys = groups.flatMap(group => group.question_keys);
  if (groupedKeys.length !== keys.length || groupedKeys.some((key, i) => key !== keys[i])) return;
  const byKey = new Map(keys.map((key, index) => [key, headings[index]]));
  groups.forEach(group => {
    group.question_keys.forEach((key, index) => {
      const heading = byKey.get(key);
      heading.id = `quick-question-${encodeURIComponent(key)}`;
      if (!group.title) return;
      if (index === 0) {
        const title = document.createElement('h2');
        title.id = group.id; title.textContent = group.title;
        title.dataset.quickOutlineGenerated = 'true'; title.dataset.quickOutlineGroup = group.id;
        title.dataset.quickTitle = group.title;
        if (group.note) title.title = group.note;
        heading.before(title);
        if (group.note) {
          const note = document.createElement('p'); note.className = 'quick-outline-note';
          note.dataset.quickOutlineGenerated = 'true'; note.textContent = group.note;
          heading.before(note);
        }
      }
      const leaf = replaceQuickHeadingTag(heading, 'h3');
      leaf.dataset.quickOutlineLeaf = group.id;
    });
  });
}
function renderQuickReportNavigation(md, ctx) {
  const content = $('report-content');
  const host = $('report-evidence-tools');
  if (!content || !host) return;
  resetQuickOutline(content);
  renderQuickInlineReferences(content, ctx);
  const quick = ctx?.reportMode === 'quick' || ctx?.reportStyle === 'quick';
  if (quick) renderQuickQuestionConditions(content);
  if (quick) renderQuickOutline(content, ctx);
  content.classList.toggle('quick-report', quick);
  host.replaceChildren();
  if (!ctx) return;
  if (quick && !ctx.quickSummary) hydrateQuickReportDetails(ctx);
  const navigation = document.createElement('nav');
  navigation.className = 'quick-report-nav';
  navigation.setAttribute('aria-label', '报告方式与原文');
  const intro = document.createElement('div');
  intro.className = 'quick-report-nav__intro';
  const badge = document.createElement('span');
  badge.className = 'quick-report-badge';
  badge.textContent = reportModeLabel(ctx.reportMode || (quick ? 'quick' : 'insight'));
  const copy = document.createElement('span');
  copy.textContent = quick ? '客观题统计 · 主观题逐题总结 · 原文按需查看' : '重点发现与决策含义 · 原文按需查看';
  intro.append(badge, copy);
  navigation.append(intro);
  const questions = ctx.quickSummary?.questions || [];
  const failedCount = questions.filter(question => question.status === 'failed').length;
  if (ctx.reportStatus === 'partial' || failedCount) {
    const partial = document.createElement('div');
    partial.className = 'quick-partial-notice';
    const text = document.createElement('p');
    text.textContent = failedCount
      ? `${questions.length - failedCount} 道主观题已完成，${failedCount} 道未完成。重试仅补全当前版本的失败题目，版本数量不变。`
      : '部分题目尚未完成。重试仅补全当前版本的失败题目，版本数量不变。';
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'btn btn--primary';
    retry.dataset.quickRetryFailed = 'true';
    retry.textContent = '仅重试失败题目';
    retry.disabled = reportInteractionBusy();
    retry.addEventListener('click', retryFailedQuickReport);
    partial.append(text, retry);
    navigation.append(partial);
  }
  if (quick) {
    const partial = navigation.querySelector('.quick-partial-notice');
    if (partial) host.append(partial);
    return;
  }
  const links = document.createElement('div');
  links.className = 'quick-report-nav__links';
  const source = document.createElement('button');
  source.type = 'button';
  source.textContent = '查看回答原文';
  source.addEventListener('click', () => openReportSources('', '全部题目', 'all'));
  links.append(source);
  navigation.append(links);
  host.append(navigation);
}
function reportExportUrl(url, version, scope = 'body') {
  const target = new URL(url, window.location.origin);
  if (version) target.searchParams.set('version', String(version));
  target.searchParams.set('scope', scope === 'evidence' ? 'evidence' : 'body');
  return target.pathname + target.search;
}

const quickReportHydrations = new Set();
async function hydrateQuickReportDetails(ctx) {
  const viewMode = state.viewMode, id = activeReportId(), version = activeVersionNumber(ctx);
  const key = `${viewMode}:${id}:${version}`;
  if (!id || !version || quickReportHydrations.has(key)) return;
  quickReportHydrations.add(key);
  try {
    const url = viewMode === 'history' ? `/api/history/${encodeURIComponent(id)}?version=${version}` : `/api/report/${encodeURIComponent(id)}?version=${version}`;
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok) throw new Error('报告元数据读取失败');
    if (state.viewMode !== viewMode || activeReportId() !== id || activeVersionNumber(ctx) !== version || activeReportCtx() !== ctx) return;
    syncReportVersionMeta(ctx, {...data, version, selected_version: version});
    renderQuickReportNavigation(ctx.reportMd, ctx);
    if (typeof buildTOC === 'function') buildTOC();
    updateReportActionAvailability();
  } catch (error) {
    quickReportHydrations.delete(key);
    showToast('暂未读取到逐题状态，请重新选择该报告版本。', 'info');
  }
}
let reportSourceContext=null;
let reportSourceSerial=0;
function openReportSources(questionKey,question,view = 'all', finding = null) {
  const ctx=activeReportCtx();
  const sourceQuestion = ctx.quickSummary?.questions?.find(item => item.question_key === questionKey);
  const evidenceItems = sourceQuestion ? quickEvidenceItems(sourceQuestion, finding) : (ctx.quickSummary?.questions || []).flatMap(item => quickEvidenceItems(item));
  reportSourceContext={id:activeReportId(),historyId:state.viewMode==='history'?state.historyId:null,version:activeVersionNumber(ctx)||1,reportContext:ctx,viewMode:state.viewMode,questionKey,question,offset:0,limit:50,query:'',view,evidenceItems,pageItems:[]};
  reportSourceContext.readingBody = document.querySelector?.('#panel-5 .report-document .report-body');
  reportSourceContext.readingPosition = reportSourceContext.readingBody?.scrollTop;
  const questionParts = quickQuestionParts(question);
  $('report-sources-title').textContent=finding ? `${questionParts.title} · 观点依据` : questionParts.title||'回答原文';
  $('report-sources-context').textContent=finding ? `V${reportSourceContext.version} · ${finding.text}（仅显示本观点引用的原文，附中文译文）` : `V${reportSourceContext.version||1} · ${reportModeLabel(ctx.reportMode)} · 原文只代表对应回答，不用于推算观点人数；中文译文在首次查看本页时生成`;
  document.querySelectorAll('[data-source-view]').forEach(button => {
    button.setAttribute('aria-selected', String(button.dataset.sourceView === view));
    button.hidden = !!finding && button.dataset.sourceView === 'all';
    if (button.dataset.sourceView === 'evidence') button.textContent = finding ? '本观点依据' : '精选证据';
  });
  $('report-sources-query').value='';
  $('report-sources-dialog').showModal();loadReportSources();
}
function reportSourceRequestCurrent(context, serial) {
  return context === reportSourceContext && serial === reportSourceSerial
    && context.reportContext === activeReportCtx() && context.viewMode === state.viewMode
    && context.id === activeReportId() && context.version === (activeVersionNumber(activeReportCtx()) || 1);
}
function sourcePlayerFields(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
  return Object.entries(value).filter(([key, item]) => key && (typeof item === 'string' || typeof item === 'number') && String(item).trim());
}
function renderSourcePlayerInfo(item) {
  const entries = [...sourcePlayerFields(item.ids), ...sourcePlayerFields(item.profile)];
  if (!entries.length) return null;
  const info = document.createElement('dl'); info.className = 'report-source-player';
  entries.forEach(([name, value]) => {
    const field = document.createElement('div'); field.className = 'report-source-player__field';
    const label = document.createElement('dt'); label.textContent = name;
    const text = document.createElement('dd'); text.textContent = String(value);
    field.append(label, text); info.append(field);
  });
  return info;
}
function renderReportSourceItems(items, context, serial = reportSourceSerial) {
  $('report-sources-list').replaceChildren();
  items.forEach(item => {
    const row = document.createElement('article'); row.className = 'report-source-item';
    const title = document.createElement('strong'); title.textContent = `${context.questionKey ? '' : quickQuestionParts(item.question || context.question).title+' · '}原文 ${item.response_id}`;
    const translation = document.createElement('div'); translation.className = 'report-source-translation';
    translation.dataset.status = item.translation_status || 'pending';
    const translationLabel = document.createElement('span'); translationLabel.className = 'report-source-label'; translationLabel.textContent = '中文译文';
    const translatedText = document.createElement('p');
    if (item.translation_status === 'complete') {
      translatedText.textContent = item.translation_zh;
    } else if (item.translation_status === 'failed') {
      const reason = ({call_timeout:'本次翻译超时，请重试',page_timeout:'本页翻译超时，请重试',
        input_budget_exceeded:'这条回复过长，暂时无法完成翻译',model_error:'翻译服务暂不可用，请重试',
        missing_translation:'未收到完整译文，请重试',invalid_structure:'未收到有效译文，请重试',
        invalid_translation:'未收到有效中文译文，请重试',translation_unavailable:'翻译服务暂不可用，请重试',
        cancelled:'翻译已中断，请重试'})[item.translation_error] || item.translation_error;
      translatedText.textContent = `翻译未完成${reason ? `：${reason}` : ''}`;
    } else {
      translatedText.textContent = '正在翻译…首次查看本页时需要稍等。';
    }
    translation.append(translationLabel, translatedText);
    if (item.translation_status === 'failed') {
      const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'btn btn--ghost report-source-retry';
      retry.textContent = '重试翻译';
      retry.addEventListener('click', () => {
        if (reportSourceRequestCurrent(context, serial) && item.translation_status === 'failed') {
          translateReportSourceItems(context, serial, [item.response_id]);
        }
      });
      translation.append(retry);
    }
    const original = document.createElement('div'); original.className = 'report-source-original';
    const originalLabel = document.createElement('span'); originalLabel.className = 'report-source-label'; originalLabel.textContent = '原文';
    const text = document.createElement('p'); text.textContent = item.text || '';
    original.append(originalLabel, text);
    row.append(title);
    const playerInfo = renderSourcePlayerInfo(item);
    if (playerInfo) row.append(playerInfo);
    row.append(translation, original); $('report-sources-list').append(row);
  });
}
function prepareReportSourcePage(items, context, serial) {
  // The server owns translation caching; only this visible page is retained here.
  context.pageItems = items.map(item => ({
    response_id: String(item.response_id), text: String(item.text || ''),
    question: item.question || context.question, question_key: item.question_key || context.questionKey,
    profile: item.profile || {}, ids: item.ids || {},
    translation_zh: '', translation_status: 'pending', translation_error: '',
  }));
  renderReportSourceItems(context.pageItems, context, serial);
}
async function translateReportSourceItems(context, serial, retryIds = null) {
  if (!reportSourceRequestCurrent(context, serial)) return;
  const pageItems = context.pageItems;
  const requestedIds = retryIds ? new Set(retryIds.map(String)) : null;
  const requested = pageItems.filter(item => requestedIds
    ? requestedIds.has(item.response_id) && item.translation_status === 'failed'
    : item.translation_status === 'pending');
  if (!requested.length) return;
  requested.forEach(item => { item.translation_status = 'pending'; item.translation_error = ''; });
  renderReportSourceItems(pageItems, context, serial);
  const body = {version: context.version, response_ids: requested.map(item => item.response_id)};
  if (context.historyId) body.history_id = context.historyId;
  try {
    const response = await fetch(`/api/report/${encodeURIComponent(context.id)}/sources/translate`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '翻译服务暂不可用');
    if (!reportSourceRequestCurrent(context, serial) || context.pageItems !== pageItems) return;
    const translated = new Map((Array.isArray(data.items) ? data.items : []).map(item => [String(item.response_id), item]));
    requested.forEach(item => {
      const result = translated.get(item.response_id);
      const sameSource = result && result.text === item.text && result.question_key === item.question_key;
      if (sameSource) {
        if (result.profile) item.profile = result.profile;
        if (result.ids) item.ids = result.ids;
      }
      if (sameSource && result.translation_status === 'complete' && typeof result.translation_zh === 'string' && result.translation_zh.trim()) {
        item.translation_zh = result.translation_zh;
        item.translation_status = 'complete';
      } else {
        item.translation_status = 'failed';
        item.translation_error = result && !sameSource ? '原文版本不匹配，请重新打开证据'
          : String(result?.translation_error || '未获取到中文译文，请重试').slice(0, 180);
      }
    });
  } catch (error) {
    if (!reportSourceRequestCurrent(context, serial) || context.pageItems !== pageItems) return;
    requested.forEach(item => {
      item.translation_status = 'failed'; item.translation_error = String(error.message || '翻译服务暂不可用').slice(0, 180);
    });
  }
  if (reportSourceRequestCurrent(context, serial) && context.pageItems === pageItems) renderReportSourceItems(pageItems, context, serial);
}
async function loadReportSources() {
  const context=reportSourceContext, serial=++reportSourceSerial;
  if(!context)return;
  context.pageItems=[];
  $('report-sources-list').textContent='正在读取原文…';
  $('report-sources-prev').disabled=true;$('report-sources-next').disabled=true;
  if (context.view === 'evidence') {
    const matching = context.evidenceItems.filter(item => !context.query || item.text.toLocaleLowerCase().includes(context.query.toLocaleLowerCase()));
    prepareReportSourcePage(matching.slice(context.offset, context.offset + context.limit), context, serial);
    if (!matching.length) $('report-sources-list').textContent = context.query ? '没有匹配的精选证据。' : '此题尚无已验证的精选证据，可切换查看全部原文。';
    $('report-sources-page').textContent = `${matching.length ? context.offset + 1 : 0}–${Math.min(context.offset + context.limit, matching.length)} / ${matching.length} 条`;
    $('report-sources-prev').disabled = context.offset === 0;
    $('report-sources-next').disabled = context.offset + context.limit >= matching.length;
    await translateReportSourceItems(context, serial);
    return;
  }
  const params=new URLSearchParams({version:String(context.version||1),question_key:context.questionKey,offset:String(context.offset),limit:String(context.limit),q:context.query});
  if(context.historyId)params.set('history_id',context.historyId);
  try {
    const response=await fetch(`/api/report/${encodeURIComponent(context.id)}/sources?${params}`);const data=await response.json();
    if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'原文读取失败');
    if(!reportSourceRequestCurrent(context,serial))return;
    prepareReportSourcePage(data.items || [], context, serial);
    if(!data.items?.length)$('report-sources-list').textContent='没有匹配的回答原文。';
    const total=Number(data.total)||0;
    $('report-sources-page').textContent=`${total?context.offset+1:0}–${Math.min(context.offset+context.limit,total)} / ${total} 条`;
    $('report-sources-prev').disabled=context.offset===0;
    $('report-sources-next').disabled=context.offset+context.limit>=total;
    await translateReportSourceItems(context,serial);
  } catch(error){if(reportSourceRequestCurrent(context,serial))$('report-sources-list').textContent=error.message;}
}
document.querySelectorAll('[data-source-view]').forEach(button => button.addEventListener('click', () => {
  if (!reportSourceContext) return;
  reportSourceContext.view = button.dataset.sourceView;
  reportSourceContext.offset = 0;
  document.querySelectorAll('[data-source-view]').forEach(item => item.setAttribute('aria-selected', String(item === button)));
  loadReportSources();
}));
$('report-sources-search').addEventListener('submit',event=>{event.preventDefault();if(!reportSourceContext)return;reportSourceContext.query=$('report-sources-query').value.trim();reportSourceContext.offset=0;loadReportSources();});
$('report-sources-prev').addEventListener('click',()=>{if(!reportSourceContext)return;reportSourceContext.offset=Math.max(0,reportSourceContext.offset-reportSourceContext.limit);loadReportSources();});
$('report-sources-next').addEventListener('click',()=>{if(!reportSourceContext)return;reportSourceContext.offset+=reportSourceContext.limit;loadReportSources();});
$('report-sources-close').addEventListener('click',()=>{$('report-sources-dialog').close();});
let reportSourceBackdropPressed = false;
function isReportSourceBackdrop(event) {
  const dialog = $('report-sources-dialog');
  if (event.target !== dialog) return false;
  const bounds = dialog.getBoundingClientRect();
  return event.clientX < bounds.left || event.clientX > bounds.right
    || event.clientY < bounds.top || event.clientY > bounds.bottom;
}
$('report-sources-dialog').addEventListener('pointerdown', event => {
  reportSourceBackdropPressed = event.button === 0 && isReportSourceBackdrop(event);
});
$('report-sources-dialog').addEventListener('pointercancel', () => { reportSourceBackdropPressed = false; });
$('report-sources-dialog').addEventListener('click', event => {
  const shouldClose = reportSourceBackdropPressed && isReportSourceBackdrop(event);
  reportSourceBackdropPressed = false;
  if (shouldClose) $('report-sources-dialog').close();
});
$('report-sources-dialog').addEventListener('close',()=>{
  reportSourceBackdropPressed = false;
  const context = reportSourceContext;
  if (context && reportSourceRequestCurrent(context, reportSourceSerial) && context.readingBody) {
    context.readingBody.scrollTop = context.readingPosition;
  }
  reportSourceContext=null;reportSourceSerial++;
});
async function retryFailedQuickReport() {
  if(reportInteractionBusy())return;
  const ctx = activeReportCtx();
  return startQuickReportRetry({reportId: activeReportId(), viewMode: state.viewMode,
    baseVersion: activeVersionNumber(), questions: ctx.quickSummary?.questions || []});
}

function isQuickRetryRunning() { return state.quickRetry?.running === true; }
function quickRetryMatchesView(job) {
  return !!job && String(activeReportId()) === job.reportId
    && (activeReportCtx()?.reportMode === 'quick' || activeReportCtx()?.reportStyle === 'quick');
}
function captureQuickReadingPosition() {
  const body = document.querySelector('.report-body');
  if (!body?.getBoundingClientRect) return null;
  const top = body.getBoundingClientRect().top;
  const headings = [...body.querySelectorAll('h2, h3[data-quick-question-key]')];
  const heading = headings.filter(h => h.getBoundingClientRect().top <= top + 110).at(-1);
  return {body, scrollTop: body.scrollTop, key: heading?.dataset.quickQuestionKey, id: heading?.id,
    offset: heading ? heading.getBoundingClientRect().top - top : 0};
}
function restoreQuickReadingPosition(position) {
  if (!position?.body?.isConnected) return;
  const {body, key, id, offset, scrollTop} = position;
  const heading = (key || id) && [...body.querySelectorAll('h2, h3[data-quick-question-key]')]
    .find(h => key ? h.dataset.quickQuestionKey === key : h.id === id);
  body.scrollTop = heading
    ? body.scrollTop + heading.getBoundingClientRect().top - body.getBoundingClientRect().top - offset
    : scrollTop;
}
function locateQuickRetryQuestion(job) {
  if (!quickRetryMatchesView(job)) return;
  const pending = job.questions.find(q => !['complete', 'failed'].includes(job.items[q.question_key]?.status)) || job.questions[0];
  const heading = [...document.querySelectorAll('#report-content [data-quick-question-key]')]
    .find(h => h.dataset.quickQuestionKey === pending?.question_key);
  heading?.scrollIntoView({behavior:'smooth', block:'start'});
}
async function stopQuickReportRetry(job = state.quickRetry) {
  if (state.quickRetry !== job || !job?.running || job.stopping) return;
  job.stopRequested = true;
  if (!job.sessionReady) { renderQuickRetryStatus(); return; }
  job.stopping = true;
  renderQuickRetryStatus();
  try {
    const response = await fetch(`/api/report/${encodeURIComponent(job.sessionId)}/cancel`, {method:'POST'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '停止失败');
  } catch (error) {
    if (state.quickRetry !== job || !job.running) return;
    job.stopping = false; job.stopRequested = false;
    showToast(error.message, 'error'); renderQuickRetryStatus();
  }
}
async function viewQuickRetryUpdate(job = state.quickRetry) {
  if (state.quickRetry !== job || !job?.result || !quickRetryMatchesView(job) || state.reportVersionLoading) return;
  const position = captureQuickReadingPosition();
  const version = Number(job.result.version);
  try {
    let loaded;
    if (state.viewMode === 'history' || job.viewMode === 'history') {
      state.historyReport.id = job.reportId;
      state.historyId = job.reportId;
      loaded = await loadHistoryReportVersion(version);
    } else loaded = await loadSessionReportVersion(version);
    if (loaded && state.quickRetry === job && String(activeReportId()) === job.reportId
        && activeVersionNumber() === version) {
      state.quickRetry = null;
      renderQuickRetryStatus();
      restoreQuickReadingPosition(position);
    }
  } catch (error) { showToast(error.message, 'error'); }
}
function renderQuickRetryStatus() {
  const host = $('quick-retry-status');
  if (!host) return;
  const job = state.quickRetry;
  const visible = quickRetryMatchesView(job);
  const position = captureQuickReadingPosition();
  const focusedAction = host.contains(document.activeElement) ? document.activeElement?.dataset.quickRetryAction : null;
  host.hidden = !visible;
  host.replaceChildren();
  document.querySelectorAll('[data-quick-retry-marker]').forEach(node => node.remove());
  document.querySelectorAll('#report-evidence-tools .quick-partial-notice').forEach(node => { node.hidden = visible; });
  if (!visible) { restoreQuickReadingPosition(position); return; }
  const title = document.createElement('strong');
  title.setAttribute('role', 'status');
  const complete = Object.values(job.items).filter(q => q.status === 'complete').length;
  title.textContent = job.running
    ? (job.stopRequested ? '正在停止，保存已完成进度…' : `正在重试 ${job.questions.length} 道题 · 已完成 ${complete}/${job.questions.length}`)
    : job.result ? (job.status === 'complete' ? `V${job.result.version} 已补全` : `V${job.result.version} 已保存补全进度，仍有题目未完成`)
      : (job.status === 'cancelled' ? '本次重试已停止' : '本次重试未完成');
  const copy = document.createElement('p');
  copy.textContent = job.error || `继续阅读当前版本。${job.result ? `V${job.result.version} 已就绪，点击查看更新。` : '其他报告内容可继续阅读。'}`;
  const text = document.createElement('div'); text.className = 'quick-retry-status__copy'; text.append(title, copy);
  const actions = document.createElement('div'); actions.className = 'quick-retry-status__actions';
  const button = (label, action, callback) => {
    const node = document.createElement('button'); node.type = 'button'; node.className = 'btn btn--secondary btn--sm';
    node.textContent = label; node.dataset.quickRetryAction = action; node.addEventListener('click', callback); actions.append(node); return node;
  };
  if (job.running) {
    button('定位题目', 'locate', () => locateQuickRetryQuestion(job));
    button('停止重试', 'stop', () => stopQuickReportRetry(job)).disabled = !!job.stopRequested;
  } else {
    if (job.result) button('查看更新', 'view', () => viewQuickRetryUpdate(job));
    if (job.status !== 'complete' && !job.blocked) button('继续重试未完成题目', 'retry', () => startQuickReportRetry({
      reportId:job.reportId, viewMode:job.viewMode, baseVersion:job.result?.version || job.baseVersion,
      questions:job.result?.quick_summary?.questions || job.originalQuestions}));
    if (job.blocked) button('刷新报告', 'refresh', async () => {
      const position = captureQuickReadingPosition();
      const version = activeVersionNumber();
      try {
        const loaded = state.viewMode === 'history' ? await loadHistoryReportVersion(version) : await loadSessionReportVersion(version);
        if (loaded && state.quickRetry === job) { state.quickRetry = null; renderQuickRetryStatus(); }
        restoreQuickReadingPosition(position);
      } catch (error) { showToast(error.message, 'error'); }
    });
    button('收起', 'dismiss', () => { if (state.quickRetry === job) { state.quickRetry = null; renderQuickRetryStatus(); } });
  }
  const row = document.createElement('div'); row.className = 'quick-retry-status__row'; row.append(text, actions);
  const details = document.createElement('details'); details.open = !!job.detailsOpen;
  const summary = document.createElement('summary'); summary.textContent = '查看本次进度'; details.append(summary);
  details.addEventListener('toggle', () => { if (state.quickRetry === job) job.detailsOpen = details.open; });
  const list = document.createElement('ul');
  const labels = {queued:'等待处理', running:'正在处理', complete:'已完成', failed:'未完成'};
  job.questions.forEach(question => {
    const item = document.createElement('li');
    const status = job.items[question.question_key]?.status || 'queued';
    item.textContent = `${question.question}：${labels[status] || status}`; list.append(item);
    document.querySelectorAll('#report-content [data-quick-question-key]').forEach(heading => {
      if (heading.dataset.quickQuestionKey !== question.question_key || activeVersionNumber() !== job.baseVersion) return;
      const marker = document.createElement('span'); marker.className = 'quick-retry-marker';
      marker.dataset.quickRetryMarker = 'true'; marker.textContent = job.running ? labels[status] : job.result ? '重试结果已更新' : '重试未完成'; heading.append(marker);
    });
  });
  details.append(list); host.append(row, details);
  if (focusedAction) [...host.querySelectorAll('[data-quick-retry-action]')]
    .find(node => node.dataset.quickRetryAction === focusedAction)?.focus({preventScroll:true});
  restoreQuickReadingPosition(position);
}
async function startQuickReportRetry({reportId, viewMode, baseVersion, questions}) {
  if (reportInteractionBusy() || isQuickRetryRunning()) return false;
  const pending = questions.filter(q => q.status !== 'complete');
  if (!reportId || !baseVersion || !pending.length) { showToast('该版本没有可重试题目', 'info'); return false; }
  const job = {reportId:String(reportId), viewMode, baseVersion:Number(baseVersion), sessionId:String(reportId),
    sessionReady:viewMode !== 'history', running:true, status:'running', questions:pending,
    originalQuestions:questions, items:Object.fromEntries(pending.map(q => [q.question_key, {status:'queued'}])), result:null};
  state.quickRetry = job;
  renderQuickRetryStatus(); updateReportActionAvailability(); applyQAAvailability();
  try {
    await consumeSSEPost(`/api/report/${encodeURIComponent(job.reportId)}/retry-failed`, {
      base_version:job.baseVersion, ...(viewMode === 'history' ? {history_id:job.reportId} : {}),
    }, event => {
      if (state.quickRetry !== job) return;
      if (event.type === 'session_ready') {
        job.sessionId = event.session_id; job.sessionReady = true;
        if (job.stopRequested) stopQuickReportRetry(job);
      }
      if (event.type === 'analysis_progress' && event.phase === 'quick_questions' && job.items[event.question_key]
          && event.status !== 'reused') job.items[event.question_key] = {...job.items[event.question_key], ...event};
      if (event.type === 'cancelled') { job.running = false; job.status = 'cancelled'; }
      if (event.type === 'report_done') {
        job.running = false; job.result = event; job.status = event.report_status === 'complete' ? 'complete' : 'partial';
        (event.quick_summary?.questions || []).forEach(q => { if (job.items[q.question_key]) job.items[q.question_key] = {status:q.status}; });
        // Update only the version list, never the selected snapshot or sources.
        const ctx = activeReportCtx();
        if (quickRetryMatchesView(job)) {
          const list = event.versions || event.report_versions;
          if (list) ctx.versions = normalizeReportVersions(list);
          ctx.activeVersion = event.active_version || event.active_report_version || event.version;
          ctx.nextVersion = event.next_version || Number(event.version) + 1;
          ctx.maxVersions = Number(event.max_versions || ctx.maxVersions || 5);
          updateReportVersionUi();
        }
        showToast(job.status === 'complete' ? `V${event.version} 已补全，版本数量不变` : `V${event.version} 已保存补全进度，可继续重试未完成题目`, job.status === 'complete' ? 'success' : 'info', 7000);
      }
      renderQuickRetryStatus();
    });
    if (job.running) throw new Error('连接结束，尚未收到保存确认。当前报告仍可阅读。');
    if (job.result?.completion && quickRetryMatchesView(job) && activeVersionNumber() === job.baseVersion) {
      await viewQuickRetryUpdate(job);
    }
  } catch (error) {
    if (state.quickRetry !== job) return false;
    job.running = false; job.status = 'error'; job.error = error.message;
    job.blocked = /资料|画像|缓存|不匹配|已更新|不存在|版本已达上限|没有可补全|没有可重试|未保存|完整重新生成/.test(error.message);
  } finally {
    if (state.quickRetry === job) { renderQuickRetryStatus(); updateReportActionAvailability(); applyQAAvailability(); }
  }
  return job.status === 'complete';
}
$('btn-report-cancel').addEventListener('click',async()=>{
  if(!state.sessionReport.running)return;
  const button=$('btn-report-cancel');button.disabled=true;button.textContent='正在停止…';
  try {
    const response=await fetch(`/api/report/${state.sessionId}/cancel`,{method:'POST'});const data=await response.json();
    if(!response.ok)throw new Error(data.detail||'停止失败');
    showToast('已请求停止，正在保存已完成内容','info');
  } catch(error){showToast(error.message,'error');button.disabled=false;button.textContent='停止生成';}
});

$('btn-report-regenerate').addEventListener('click',async()=>{
  if(reportInteractionBusy())return;
  const ctx=activeReportCtx();
  if(ctx.reportMode==='quick' && !await ensureQuickRegenerationAllowed())return;
  if(state.viewMode==='history' && ctx.reportMode!=='quick'){showToast('请重新上传同一份数据，从重复报告入口生成新版本。','info');return;}
  $('report-regenerate-base').textContent=`沿用 V${activeVersionNumber()} 的题目、背景与报告方式（${reportModeLabel(ctx.reportMode)}），重新处理全部题目。已有版本保留。`;
  $('report-regenerate-instruction').value='';$('report-regenerate-dialog').showModal();
});
$('report-regenerate-close').addEventListener('click',()=>{$('report-regenerate-dialog').close();});
$('report-regenerate-start').addEventListener('click',()=>{
  const options={regenerate:true,baseVersion:activeVersionNumber(),instruction:$('report-regenerate-instruction').value.trim(),...(state.viewMode==='history'?{historyId:state.historyId}:{})};
  $('report-regenerate-dialog').close();runStats(options);
});
