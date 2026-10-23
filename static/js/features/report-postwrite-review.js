/* Presentation of existing audit results only. No model requests or new verdicts. */
(function (host) {
  'use strict';
  const labels = {high: '高风险', low: '低风险', uncertain: '待确认'};
  const rank = {high: 0, uncertain: 1, low: 2};
  const inlineText = value => String(value || '').replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[*_\x60~]/g, '').trim();
  const normalized = value => inlineText(value).replace(/\s+/g, ' ').trim();
  function plain(value) {
    return String(value || '')
      .replace(/\breported_count:/g, '报告人数：').replace(/\breported_denominator:/g, '统计分母：')
      .replace(/\bscope_unresolved\b/g, '统计范围尚未明确')
      .replace(/\bsource_index\b/g, '原始回答对应表').replace(/\breview_refs\b/g, '引用依据')
      .replace(/\bB\d{4}\b/g, '对应段落').replace(/\bC\d{3}\b/g, '对应观点')
      .replace(/\bT\d{3}\b/g, '对应人数表述').replace(/\bq\d+\/r\d+\b/g, '对应回答')
      .replace(/\b(?:RuntimeError|TimeoutError|ReadError|RemoteProtocolError)\b/g, '模型通信异常');
  }
  function blockIndex(markdown) {
    const report = String(markdown || ''), blocks = [];
    let offset = 0;
    report.split('\n\n').forEach((text, index) => {
      if (text.trim()) blocks.push({id: 'B' + String(index + 1).padStart(4, '0'), text, start: offset, end: offset + text.length});
      offset += text.length + 2;
    });
    const headings = [];
    let fence = '', previous = null;
    offset = 0;
    for (const line of report.split('\n')) {
      const trimmed = line.trim(), fenceMatch = /^\s{0,3}(\x60{3,}|~{3,})/.exec(line);
      if (fenceMatch) {
        if (!fence) fence = fenceMatch[1];
        else if (fenceMatch[1][0] === fence[0] && fenceMatch[1].length >= fence.length) fence = '';
      } else if (!fence) {
        const atx = /^\s{0,3}(#{1,6})\s+(.+?)(?:\s+#+)?\s*$/.exec(line);
        if (atx) headings.push({level: atx[1].length, title: inlineText(atx[2]), start: offset});
        else if (/^(?:={3,}|-{3,})\s*$/.test(trimmed) && previous?.text.trim() && !/[|>#]/.test(previous.text)) {
          headings.push({level: trimmed[0] === '=' ? 1 : 2, title: inlineText(previous.text), start: previous.start});
        }
      }
      previous = {text: line, start: offset};
      offset += line.length + 1;
    }
    function pathAt(position) {
      const stack = [];
      for (const heading of headings) {
        if (heading.start > position) break;
        while (stack.length && stack[stack.length - 1].level >= heading.level) stack.pop();
        stack.push(heading);
      }
      const path = stack.filter(item => item.level > 1).map(item => item.title);
      return path.length ? path : stack.map(item => item.title);
    }
    return {report, blocks, pathAt};
  }
  function locations(index, original, blockIds = []) {
    if (!original) return [];
    const allowed = new Set(blockIds);
    const found = [];
    let position = index.report.indexOf(original);
    while (position >= 0) {
      const end = position + original.length;
      const overlaps = index.blocks.filter(block => block.start < end && block.end > position);
      if (!allowed.size || (overlaps.length && overlaps.every(block => allowed.has(block.id)))) {
        found.push({start: position, end, path: index.pathAt(position)});
      }
      position = index.report.indexOf(original, position + Math.max(1, original.length));
    }
    return found;
  }
  function degreeOnly(original, repair) {
    if (!original || !repair || normalized(original) === normalized(repair)) return false;
    // Only a bounded modifier replacement with an otherwise identical sentence
    // can be labeled low risk. Counts, negation and subjects cannot disappear.
    const modifier = /较长时间|较多时间|一些时间|一点时间|多一点时间|一定时间/g;
    const left = normalized(original), right = normalized(repair);
    return modifier.test(left) && (modifier.lastIndex = 0, modifier.test(right))
      && left.replace(modifier, '〈时间程度〉') === right.replace(modifier, '〈时间程度〉');
  }
  function meaningRisk(check) {
    if ((check.citation_issues || []).length) return 'high';
    if (check.verdict === 'uncertain' || check.complete === false) return 'uncertain';
    if (check.verdict === 'issue' && (check.sources || []).some(source => source.relation === 'unrelated')) return 'high';
    if (check.verdict === 'issue' && (check.sources || []).length
      && (check.sources || []).every(source => ['direct', 'inference'].includes(source.relation))
      && degreeOnly(check.original, check.repair)) return 'low';
    return 'uncertain';
  }
  function sourceLabel(source) {
    const unit = source.source || {};
    const player = String(unit.player || source.player || '');
    const label = /^P0*\d+$/.test(player)
      ? (Number.isInteger(unit.source_row) ? '原始数据第 ' + unit.source_row + ' 行玩家' : '相关玩家')
      : (/^R\d+$/.test(player) ? '玩家 ' + player : (player || '相关玩家'));
    const question = typeof unit.question === 'object'
      ? unit.question?.translation || unit.question?.original : unit.question;
    return {player: label, question: String(question || ''), original: String(unit.original || ''), meaning: plain(source.meaning)};
  }
  function build(ctx) {
    const audit = ctx?.reportAudit || ctx?.report_audit || {};
    const report = String(ctx?.reportMd ?? ctx?.report_md ?? '');
    const registry = audit.checkpoint?.registry || {};
    const index = blockIndex(report), claims = new Map((registry.claims || []).map(claim => [claim.id, claim]));
    const grouped = new Map();
    function add(claim, risk, reason, sources, repair, kind, fallbackKey) {
      const original = String(claim?.original || '');
      const spans = locations(index, original, claim?.block_ids || []);
      const key = spans.length === 1 ? spans[0].start + ':' + spans[0].end
        : JSON.stringify([original, claim?.block_ids || [], original ? '' : fallbackKey]);
      let issue = grouped.get(key);
      if (!issue) {
        issue = {key, original, locations: spans, risk, reasons: [], sources: [], repairs: [], kinds: []};
        grouped.set(key, issue);
      }
      if (rank[risk] < rank[issue.risk]) issue.risk = risk;
      if (reason && !issue.reasons.includes(plain(reason))) issue.reasons.push(plain(reason));
      for (const source of sources || []) {
        const item = sourceLabel(source);
        if (item.original && !issue.sources.some(existing => existing.player === item.player
          && existing.question === item.question && existing.original === item.original)) issue.sources.push(item);
      }
      if (repair && !issue.repairs.includes(repair)) issue.repairs.push(repair);
      if (!issue.kinds.includes(kind)) issue.kinds.push(kind);
    }
    for (const check of audit.checks || []) {
      // A passed interpretation is not re-judged. An independent citation issue
      // still needs to be shown even when the sentence's meaning was supported.
      if (!['issue', 'uncertain'].includes(check.verdict) && !(check.citation_issues || []).length) continue;
      const claim = claims.get(check.id) || {original: check.original};
      const reasons = [check.reason, ...(check.citation_issues || []).map(issue => issue.reason)].filter(Boolean);
      add(claim, meaningRisk(check), reasons.join('；') || '现有核验结果提示需要人工确认。',
        check.sources, check.repair, '原意或引用', check.id);
    }
    for (const count of audit.counts || []) {
      const reviewed = count.reviewed ?? count.complete;
      if (!reviewed) continue; // Unchecked work is progress, never a content error.
      const ambiguous = count.review_status === 'ambiguous';
      if (!(count.issues || []).length && !ambiguous) continue;
      const related = [...claims.values()].filter(claim => (claim.count_task_ids || []).includes(count.id));
      const countReason = [...(count.issues || [])];
      if (ambiguous) countReason.push('已核验但部分回答是否计入存在歧义：明确 ' + count.count_min
        + ' 人，可能 ' + count.count_min + '—' + count.count_max + '/' + count.denominator + ' 人。');
      const members = (count.members || []).filter(member => member.count !== false)
        .slice().sort((a, b) => Number(b.count === null) - Number(a.count === null));
      const sources = members.flatMap(member => (member.sources || []).map(source =>
        ({...source, player: source.source?.player || member.player})));
      for (const claim of related.length ? related : [{original: count.task?.original || ''}]) {
        add(claim, (count.issues || []).length ? 'high' : 'uncertain', countReason.join('；'), sources, '', '人数范围', count.id);
      }
    }
    const issues = [...grouped.values()].sort((a, b) => rank[a.risk] - rank[b.risk]
      || (a.locations[0]?.start ?? Infinity) - (b.locations[0]?.start ?? Infinity));
    for (const issue of issues) {
      issue.path = issue.locations.length === 1 && issue.locations[0].path.length
        ? issue.locations[0].path.join(' › ') : (issue.locations.length > 1 ? '原句出现多处，暂不自动定位' : '暂未定位到具体段落');
      issue.canLocate = issue.locations.length === 1;
      issue.title = issue.kinds.includes('人数范围') ? '人数或群体范围需要复核'
        : issue.risk === 'high' ? '原意或引用可能存在实质差异'
        : issue.risk === 'low' ? '措辞程度略有差异，可选调整' : '现有依据尚不能确认，需要人工判断';
    }
    const registrationComplete = audit.registration_status === 'complete' || registry.complete === true
      || (audit.registration_status == null && audit.status === 'complete');
    return {issues, audit, registrationComplete, complete: audit.status === 'complete',
      counts: Object.fromEntries(['high', 'low', 'uncertain'].map(risk => [risk, issues.filter(issue => issue.risk === risk).length]))};
  }
  function render(panel, model, actions = {}) {
    panel.replaceChildren();
    const doc = panel.ownerDocument || document;
    function node(tag, cls, text, parent = panel) {
      const item = doc.createElement(tag);
      if (cls) item.className = cls;
      if (text != null) item.textContent = text;
      parent.appendChild(item);
      return item;
    }
    const status = node('div', 'postwrite-review-status');
    status.setAttribute('aria-live', 'polite');
    node('h3', '', model.complete ? '本版核验已完成' : '报告已保留，核验尚未完成', status);
    if (!model.registrationComplete) {
      node('p', '', '核对清单尚未整理完成，原文与人数核验还没开始。', status);
    } else {
      const audit = model.audit;
      const progress = [];
      if (Number(audit.registered_claims) > 0) progress.push('原文已核对 ' + Number(audit.checked_claims || 0) + '/' + Number(audit.registered_claims) + ' 条');
      if ((audit.counts || []).length) progress.push('人数已检查 ' + Number(audit.counts_reviewed || 0) + '/' + audit.counts.length + ' 项');
      if (progress.length) node('p', '', progress.join(' · '), status);
      if (!model.complete) node('p', '', '尚未完成的内容不代表已通过；已完成结果会保留。', status);
    }
    if (!model.complete && model.audit.stop_reason) {
      node('p', '', model.audit.stop_reason === 'cancelled' ? '本次核验已停止，已完成结果已保留。'
        : '上次核验中断：' + plain(model.audit.stop_reason), status);
    }
    if ((model.audit.registration_errors || []).length) node('p', '', '仍有报告内容尚未整理到核对清单，不能视为已完成核验。', status);
    if (actions.canEdit) {
      const row = node('div', 'postwrite-review-actions');
      if (!model.complete) {
        const resume = node('button', 'btn btn--primary', '继续核验', row);
        resume.type = 'button'; resume.dataset.postwriteAction = 'resume'; resume.disabled = !!actions.busy;
        resume.onclick = actions.onResume;
        node('span', '', '只更新当前版本的核验结果，不新增报告版本。', row);
      }
      const regenerate = node('button', 'btn btn--ghost', '重新生成整份报告', row);
      regenerate.type = 'button'; regenerate.dataset.postwriteAction = 'regenerate';
      regenerate.disabled = !!actions.busy || !!actions.atVersionLimit;
      regenerate.onclick = actions.onRegenerate;
    }
    if (!model.issues.length) {
      const empty = node('div', 'postwrite-review-empty');
      node('strong', '', model.complete ? '未发现需要人工关注的问题' : '暂未发现需关注的问题，核验尚未完成', empty);
      node('p', '', model.complete ? '已完成既定三项核验，不逐条展示通过内容。' : '剩余内容还没有完成检查，不能据此判断全部通过。', empty);
    } else {
      const filters = node('div', 'postwrite-review-filters');
      filters.setAttribute('role', 'group'); filters.setAttribute('aria-label', '按风险筛选问题');
      const list = node('div', 'postwrite-review-list');
      const cards = [];
      for (const issue of model.issues) {
        const detail = node('details', 'postwrite-review-issue', null, list);
        detail.open = cards.length === 0;
        detail.dataset.risk = issue.risk;
        const summary = node('summary', '', null, detail);
        const top = node('span', 'postwrite-review-issue-head', null, summary);
        node('span', 'postwrite-risk postwrite-risk--' + issue.risk, labels[issue.risk], top);
        node('strong', '', issue.title, top);
        node('span', 'postwrite-review-path', issue.path, summary);
        node('span', 'postwrite-review-sentence', issue.original ? '“' + issue.original + '”' : '人数表述暂未对应到报告原句', summary);
        const body = node('div', 'postwrite-review-detail', null, detail);
        if (issue.sources.length) {
          node('h4', '', '相关原文', body);
          const more = issue.sources.length > 3 ? node('details', 'postwrite-review-more', null, body) : null;
          if (more) node('summary', '', '查看更多相关原文（' + (issue.sources.length - 3) + ' 条）', more);
          issue.sources.forEach((source, index) => {
            const parent = index >= 3 && more ? more : body;
            node('p', 'postwrite-review-source-label', [source.player, source.question].filter(Boolean).join(' · '), parent);
            node('blockquote', '', source.original, parent);
          });
        }
        node('h4', '', '为什么需要关注', body);
        for (const reason of issue.reasons) node('p', '', reason, body);
        if (issue.repairs.length) {
          node('h4', '', '修改参考', body);
          for (const repair of issue.repairs) node('p', '', repair, body);
        }
        if (issue.canLocate) {
          const locate = node('button', 'btn btn--ghost', '定位到报告原句', body);
          locate.type = 'button'; locate.disabled = !!actions.busy;
          locate.dataset.postwriteLocate = 'true';
          locate.onclick = () => actions.onLocate?.(issue);
        }
        cards.push({detail, risk: issue.risk});
      }
      for (const risk of ['all', 'high', 'low', 'uncertain']) {
        const button = node('button', 'btn btn--ghost', (risk === 'all' ? '全部问题' : labels[risk])
          + ' ' + (risk === 'all' ? model.issues.length : model.counts[risk]), filters);
        button.type = 'button'; button.dataset.riskFilter = risk; button.setAttribute('aria-pressed', String(risk === 'all'));
        button.onclick = () => {
          for (const child of filters.children) child.setAttribute('aria-pressed', String(child === button));
          for (const card of cards) card.detail.hidden = risk !== 'all' && risk !== card.risk;
        };
      }
    }
    const footer = node('p', 'postwrite-review-footnote', '提示供人工判断，不自动改写正文；正文和导出均不包含核验信息。');
    footer.dataset.reviewOnly = 'true';
    if (model.audit.usage) {
      const info = node('details', 'postwrite-review-run-info');
      node('summary', '', '运行信息', info);
      const usage = model.audit.usage;
      node('p', '', '本报告累计 ' + Number(usage.requests || 0) + ' 次调用 · 已记录 '
        + Number(usage.tokens || 0).toLocaleString() + ' tokens'
        + (usage.usage_complete ? '' : '（用量记录不完整，不是最终总用量）'), info);
    }
  }
  const highlightTimers = new WeakMap();
  function clearHighlights(container) {
    if (highlightTimers.has(container)) host.clearTimeout(highlightTimers.get(container));
    highlightTimers.delete(container);
    for (const mark of container.querySelectorAll('mark.postwrite-report-highlight')) {
      const parent = mark.parentNode;
      mark.replaceWith(...mark.childNodes);
      parent?.normalize();
    }
  }
  function locate(container, issue) {
    if (!container || !issue.canLocate || issue.locations.length !== 1) return false;
    clearHighlights(container);
    const wantedPath = issue.locations[0].path.map(normalized);
    let sentence = normalized(issue.original);
    if (/^\s*\|/.test(issue.original)) sentence = sentence.replace(/^\|\s*|\s*\|$/g, '').replace(/\s*\|\s*/g, ' ');
    if (!sentence) return false;
    const doc = container.ownerDocument, stack = [], positions = [];
    let text = '';
    const selector = 'h1,h2,h3,h4,h5,h6,p,li,blockquote,td,th';
    function append(char, node, offset) {
      if (/\s/.test(char)) {
        if (!text || text.endsWith(' ')) return;
        text += ' ';
      } else text += char;
      positions.push(node ? {node, offset} : null);
    }
    for (const element of container.querySelectorAll(selector)) {
      if (/^H[1-6]$/.test(element.tagName)) {
        const level = Number(element.tagName[1]);
        while (stack.length && stack[stack.length - 1].level >= level) stack.pop();
        stack.push({level, title: normalized(element.textContent)});
      }
      const local = stack.filter(item => item.level > 1);
      const path = (local.length ? local : stack).map(item => item.title);
      if (JSON.stringify(path) !== JSON.stringify(wantedPath) || element.querySelector(selector)) continue;
      append(' ', null, 0);
      const walker = doc.createTreeWalker(element, 4);
      let item;
      while ((item = walker.nextNode())) {
        if (item.parentElement?.closest('button,script,style')) continue;
        for (let i = 0; i < item.textContent.length; i++) append(item.textContent[i], item, i);
      }
    }
    const start = text.indexOf(sentence);
    if (start < 0 || text.indexOf(sentence, start + 1) >= 0) return false;
    const ranges = new Map();
    for (const point of positions.slice(start, start + sentence.length)) {
      if (!point) continue;
      if (!ranges.has(point.node)) ranges.set(point.node, {start: point.offset, end: point.offset + 1});
      else ranges.get(point.node).end = point.offset + 1;
    }
    if (!ranges.size) return false;
    let first = null;
    for (const [node, span] of ranges) {
      const range = doc.createRange(); range.setStart(node, span.start); range.setEnd(node, span.end);
      const mark = doc.createElement('mark'); mark.className = 'postwrite-report-highlight';
      mark.appendChild(range.extractContents()); range.insertNode(mark);
      if (!first) first = mark;
    }
    first.scrollIntoView({block: 'center', behavior: host.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
    highlightTimers.set(container, host.setTimeout(() => clearHighlights(container), 6000));
    return true;
  }

  const api = {build, render, locate, clearHighlights, blockIndex, locations, meaningRisk, plain, degreeOnly};
  host.PostwriteReview = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
