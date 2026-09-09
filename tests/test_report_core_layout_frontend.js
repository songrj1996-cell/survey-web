// Run with: node --test tests/test_report_core_layout_frontend.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/features/report.js'), 'utf8');
const start = source.indexOf('function applyCoreHighlight() {');
const end = source.indexOf('\nfunction prepareReportMarkdownForPreview', start);
assert.ok(start >= 0 && end > start);
const highlightSource = source.slice(start, end);

// Small DOM model for this function's tree operations; actual browser QA is separate.
class Element {
  constructor(tagName, text = '') {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.className = '';
    this.style = {};
    this._text = text;
    this.classList = {add: name => { this.className += ` ${name}`; }};
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(text) {
    this.children.forEach(child => { child.parentNode = null; });
    this.children = [];
    this._text = text;
  }
  get nextElementSibling() {
    if (!this.parentNode) return null;
    return this.parentNode.children[this.parentNode.children.indexOf(this) + 1] || null;
  }
  appendChild(child) { return this.insertBefore(child, null); }
  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }
  insertBefore(child, next) {
    if (child.parentNode) {
      const siblings = child.parentNode.children;
      siblings.splice(siblings.indexOf(child), 1);
    }
    const index = next === null ? this.children.length : this.children.indexOf(next);
    assert.ok(index >= 0, 'reference node must belong to the parent');
    this.children.splice(index, 0, child);
    child.parentNode = this;
    return child;
  }
  matches(selector) {
    return selector.startsWith('.')
      ? this.className.split(/\s+/).includes(selector.slice(1))
      : this.tagName === selector.toUpperCase();
  }
  closest(selector) {
    for (let node = this; node; node = node.parentNode) {
      if (node.matches(selector)) return node;
    }
    return null;
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(item => item.trim());
    return this.children.flatMap(child => [
      ...(selectors.some(item => child.matches(item)) ? [child] : []),
      ...child.querySelectorAll(selector),
    ]);
  }
}

function fixture(title, existingNote = null) {
  const root = new Element('main');
  const background = new Element('h2', '调研背景');
  const heading = new Element('h2', title);
  const judgement = new Element('h3', '总体判断');
  const body = new Element('p', '设计1均值3.93，设计2均值3.70；有效回答57名。');
  const emphasis = new Element('strong', '判断依据与引用保持不变。');
  body.appendChild(emphasis);
  const detail = new Element('h2', 'Part 1 图标评价');
  const nodes = [background, heading, existingNote, judgement, body, detail].filter(Boolean);
  nodes.forEach(node => root.appendChild(node));
  const context = vm.createContext({
    $: id => id === 'report-content' ? root : null,
    document: {createElement: tag => new Element(tag)},
  });
  vm.runInContext(highlightSource, context);
  return {root, heading, body, emphasis, background, detail,
    apply: () => vm.runInContext('applyCoreHighlight()', context)};
}

const screenshotNote = '基于60份有效样本，Part 2 开放建议以57名有效回答玩家为分母';

test('screenshot heading becomes a plain heading plus a complete note inside the original highlight', () => {
  const f = fixture(`核心结论（${screenshotNote}）`);
  const originalBody = f.body.textContent;
  f.apply();
  const boxes = f.root.querySelectorAll('.core-summary-box');
  assert.equal(boxes.length, 1);
  assert.equal(f.heading.textContent, '核心结论');
  assert.equal(f.heading.nextElementSibling.tagName, 'P');
  assert.equal(f.heading.nextElementSibling.style.fontStyle, 'italic');
  assert.equal(f.root.querySelectorAll('blockquote').length, 0);
  assert.equal(f.heading.nextElementSibling.textContent, screenshotNote);
  assert.equal(f.heading.parentNode, boxes[0]);
  assert.equal(f.body.parentNode, boxes[0]);
  assert.equal(f.body.textContent, originalBody);
  assert.equal(f.emphasis.parentNode, f.body);
  assert.equal(f.background.parentNode, f.root);
  assert.equal(f.detail.parentNode, f.root);
  assert.deepEqual(f.root.querySelectorAll('h2').map(node => node.textContent),
    ['调研背景', '核心结论', 'Part 1 图标评价']);
});

test('a normal heading and italic sample note retain their nodes on repeated enhancement', () => {
  const note = new Element('p');
  const italic = new Element('em', '本次调研共收集60份有效回复。');
  note.appendChild(italic);
  const f = fixture('核心结论', note);
  f.apply();
  f.apply();
  assert.equal(f.heading.nextElementSibling, note);
  assert.equal(italic.parentNode, note);
  assert.equal(f.root.querySelectorAll('blockquote').length, 0);
  assert.equal(f.root.querySelectorAll('.core-summary-box').length, 1);
});

test('repeated enhancement and reopening an abnormal report do not accumulate notes or boxes', () => {
  const title = `核心结论（${screenshotNote}）`;
  for (let reopen = 0; reopen < 2; reopen++) {
    const f = fixture(title);
    f.apply();
    f.apply();
    f.apply();
    assert.equal(f.root.querySelectorAll('.core-sample-note').length, 1);
    assert.equal(f.root.querySelectorAll('.core-sample-note')[0].textContent, screenshotNote);
    assert.equal(f.root.querySelectorAll('blockquote').length, 0);
    assert.equal(f.root.querySelectorAll('.core-summary-box').length, 1);
  }
});

for (const note of ['样本数：60', '有效样本量=60', 'N=60', '60份有效回复', screenshotNote]) {
  test(`recognizes explicit sample counts with ASCII parentheses: ${note}`, () => {
    const f = fixture(`核心结论 (${note})`);
    f.apply();
    assert.equal(f.heading.textContent, '核心结论');
    assert.equal(f.heading.nextElementSibling.textContent, note);
  });
}

for (const title of ['核心结论（设计2需优化）', '核心结论（小样本下的取舍）',
  '核心结论（样本选择偏差）', '核心结论（N=60)', '核心结论（基于60份有效样本）补充', '其他结论（N=60）']) {
  test(`does not reinterpret a business heading or malformed title: ${title}`, () => {
    const f = fixture(title);
    f.apply();
    assert.equal(f.heading.textContent, title);
    assert.equal(f.root.querySelectorAll('blockquote').length, 0);
    assert.equal(f.root.querySelectorAll('.core-summary-box').length, 0);
  });
}

for (const tag of ['p', 'blockquote']) {
  test(`does not duplicate an identical existing ${tag} sample note`, () => {
    const note = new Element('p', screenshotNote);
    const original = tag === 'blockquote' ? new Element('blockquote') : note;
    if (tag === 'blockquote') original.appendChild(note);
    const f = fixture(`核心结论（${screenshotNote}）`, original);
    f.apply();
    assert.equal(f.heading.nextElementSibling, note);
    assert.equal(note.style.fontStyle, 'italic');
    assert.equal(f.root.querySelectorAll('blockquote').length, 0);
    assert.equal(f.root.textContent.split(screenshotNote).length - 1, 1);
  });
}

test('notes are inserted as text and never interpreted as HTML', () => {
  const note = '样本数：60，口径含 <img src=x onerror=alert(1)> & 其他说明';
  const f = fixture(`核心结论（${note}）`);
  f.apply();
  assert.equal(f.heading.nextElementSibling.tagName, 'P');
  assert.equal(f.heading.nextElementSibling.style.fontStyle, 'italic');
  assert.equal(f.heading.nextElementSibling.textContent, note);
  assert.equal(f.root.querySelectorAll('img').length, 0);
});

test('a heading without a sample note does not invent one', () => {
  const f = fixture('核心结论');
  f.apply();
  assert.equal(f.root.querySelectorAll('blockquote').length, 0);
  assert.equal(f.root.querySelectorAll('.core-summary-box').length, 1);
});

test('an existing sample quote becomes an italic paragraph without losing links', () => {
  const quote = new Element('blockquote');
  const paragraph = new Element('p', '本次调研共收集60份有效回复。');
  const link = new Element('a', '样本口径来源');
  paragraph.appendChild(link);
  quote.appendChild(paragraph);
  const f = fixture('核心结论', quote);
  f.apply();
  f.apply();
  assert.equal(f.heading.nextElementSibling, paragraph);
  assert.equal(paragraph.style.fontStyle, 'italic');
  assert.equal(link.parentNode, paragraph);
  assert.equal(f.root.querySelectorAll('blockquote').length, 0);
});

test('unrelated quotes and multi-paragraph evidence blocks keep their original structure', () => {
  for (const sampleFirst of [false, true]) {
    const quote = new Element('blockquote');
    quote.appendChild(new Element('p', sampleFirst ? '样本数：60' : '玩家认为图标容易辨认。'));
    if (sampleFirst) quote.appendChild(new Element('p', '另一段独立证据。'));
    const f = fixture('核心结论', quote);
    f.apply();
    assert.equal(f.heading.nextElementSibling, quote);
    assert.equal(f.root.querySelectorAll('blockquote').length, 1);
    assert.equal(f.root.querySelectorAll('.core-sample-note').length, 0);
  }
});
