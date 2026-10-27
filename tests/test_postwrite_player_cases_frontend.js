// Offline real-browser checks. NODE_PATH may point to the bundled test runtime.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static/js/features/report.js'), 'utf8');
const helpers = source.split('// POSTWRITE_PLAYER_CASES_START')[1].split('// POSTWRITE_PLAYER_CASES_END')[0];
const core = fs.readFileSync(path.join(root, 'static/js/core/core.js'), 'utf8');
const renderer = core.slice(core.indexOf('function renderMarkdown(md)'), core.indexOf('\nfunction esc(', core.indexOf('function renderMarkdown(md)')));
const review = fs.readFileSync(path.join(root, 'static/js/features/report-postwrite-review.js'), 'utf8');
const marked = fs.readFileSync(path.join(path.dirname(require.resolve('marked')), 'marked.umd.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static/style.css'), 'utf8');
const artifacts = path.join(root, '.test-artifacts/player-case-typography');
let browser;

test.before(async () => {
  browser = await chromium.launch({headless: true, channel: process.env.PLAYER_CASE_BROWSER_CHANNEL || undefined});
});
test.after(async () => { await browser?.close(); });

async function fixture(t, md, audit = {status: 'complete'}, width = 1100) {
  const page = await browser.newPage({viewport: {width, height: 900}});
  t.after(() => page.close());
  await page.route('**/*', route => route.abort());
  await page.setContent('<!doctype html><html data-theme="light"><meta charset="utf-8"><title>典型玩家案例 · 合成样本</title>'
    + '<style>' + css + '\nhtml,body{height:auto;min-height:100%;overflow:auto;}body{margin:0;background:var(--surface);}main{max-width:960px;margin:auto;padding:28px 20px;}</style>'
    + '<main><article id="report-content" class="markdown-body"></article></main>');
  await page.addScriptTag({content: marked});
  await page.addScriptTag({content: 'marked.setOptions({breaks:true,gfm:true});' + renderer + helpers + review});
  await page.evaluate(({md, audit}) => {
    window.caseContext = {reportMd: md, reportAudit: audit};
    const container = document.getElementById('report-content');
    container.innerHTML = renderMarkdown(md);
    window.beforeCaseText = container.textContent;
    applyPostwritePlayerCases(container, caseContext);
  }, {md, audit});
  return page;
}

const examples = [
  ['inline', '典型玩家案例｜R73：**合成案例**。\n\n普通分析。', 1],
  ['standalone', '**典型玩家案例**\n\nR73：第一位。\n\nR74：第二位。\n\n普通分析。', 1],
  ['heading-list', '#### 典型玩家案例\n\n1. R73：第一位。\n2. R74 玩家选择放弃。\n\n普通分析。', 1],
  ['setext-heading', '典型玩家案例\n---\n\nR73：合成案例。\n\n普通分析。', 1],
  ['soft-break', '典型玩家案例\nR73：同一段案例。', 1],
  ['two-cases', '典型玩家案例：R73：第一位。\n\n分析。\n\n典型玩家案例｜R74：第二位。', 2],
  ['mere-mention', '下面通过典型玩家案例说明差异。\n\nR73：原文。', 0],
  ['id-only', 'R73：没有标签的玩家段落。', 0],
  ['ambiguous', '典型玩家案例\n\n总体而言需要改进。\n\nR73：原文。', 0],
  ['mixed-list', '典型玩家案例\n\n- R73：案例。\n- 这是分析，不是玩家。', 0],
  ['heading-boundary', '典型玩家案例\n\n## 下个章节\n\nR73：原文。', 0],
  ['existing-code', '```text\n典型玩家案例｜R73：代码内容。\n```', 0],
  ['evidence-quote', '> 典型玩家案例｜R73：证据原文。', 0],
  ['table', '| 典型玩家案例 | 其他 |\n| --- | --- |\n| R73 | 表格 |', 0],
];
for (const [name, md, count] of examples) {
  test(name + ': exact text, boundaries and idempotence', async t => {
    const page = await fixture(t, md);
    assert.equal(await page.locator('.report-player-case').count(), count);
    assert.equal(await page.locator('#report-content').textContent(), await page.evaluate(() => beforeCaseText));
    await page.evaluate(() => applyPostwritePlayerCases(document.getElementById('report-content'), caseContext));
    assert.equal(await page.locator('.report-player-case').count(), count);
    if (count) assert.doesNotMatch((await page.locator('.report-player-case').allTextContents()).join(''), /普通分析|普通结论/);
  });
}

test('only selected-version metadata enables cases, including interrupted verification', async t => {
  for (const audit of [null, {}, [], false, 'complete']) {
    const page = await fixture(t, '典型玩家案例｜R73：合成案例。', audit);
    assert.equal(await page.locator('.report-player-case').count(), 0);
  }
  for (const status of ['complete', 'partial', 'cancelled']) {
    const page = await fixture(t, '典型玩家案例｜R73：合成案例。', {status});
    assert.equal(await page.locator('.report-player-case').count(), 1);
  }
});

test('version switch and audit refresh cannot leave wrappers or duplicate labels', async t => {
  const page = await fixture(t, '**典型玩家案例**\n\nR73：合成案例。');
  await page.evaluate(() => applyPostwritePlayerCases(document.getElementById('report-content'), {reportAudit: null}));
  assert.equal(await page.locator('.report-player-case').count(), 0);
  assert.equal(await page.locator('#report-content').textContent(), await page.evaluate(() => beforeCaseText));
  await page.evaluate(() => {
    const el = document.getElementById('report-content');
    applyPostwritePlayerCases(el, {reportAudit: {status: 'partial'}});
    applyPostwritePlayerCases(el, {reportAudit: {status: 'complete'}});
  });
  assert.equal(await page.locator('.report-player-case').count(), 1);
  assert.equal(await page.locator('.report-player-case__label').count(), 1);
  assert.ok(source.includes('applyPostwritePlayerCases(reportContent, ctx);'));
});

test('case inside existing core container is styled without moving headings', async t => {
  const page = await fixture(t, '## 核心结论\n\n### 主题\n\n典型玩家案例｜R73：案例。');
  await page.evaluate(() => {
    const el = document.getElementById('report-content');
    el.innerHTML = '<div class="core-summary-box"><h2>核心结论</h2><h3>主题</h3><p>典型玩家案例｜R73：案例。</p></div>';
    applyPostwritePlayerCases(el, caseContext);
  });
  assert.equal(await page.locator('.core-summary-box > .report-player-case').count(), 1);
  assert.deepEqual(await page.locator('h2,h3').allTextContents(), ['核心结论', '主题']);
});

test('light and dark themes keep readable case text and existing links', async t => {
  const page = await fixture(t, '典型玩家案例｜R73：见[合成证据](https://example.invalid/source)。');
  for (const theme of ['light', 'dark']) {
    const colors = await page.evaluate(theme => {
      document.documentElement.dataset.theme = theme;
      const box = document.querySelector('.report-player-case');
      return {background: getComputedStyle(box).backgroundColor,
        body: getComputedStyle(box.querySelector('p')).color,
        label: getComputedStyle(box.querySelector('.report-player-case__label')).color};
    }, theme);
    function luminance(rgb) {
      const values = rgb.match(/\d+/g).slice(0, 3).map(Number).map(n => n / 255)
        .map(n => n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4);
      return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722;
    }
    for (const foreground of [colors.body, colors.label]) {
      const a = luminance(foreground), b = luminance(colors.background);
      assert.ok((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) >= 4.5, JSON.stringify({theme, colors}));
    }
    assert.equal(await page.locator('.report-player-case a').getAttribute('href'), 'https://example.invalid/source');
  }
});

test('real audit locator still highlights the selected case when another chapter repeats its sentence', async t => {
  const original = 'R73：合成玩家需要一些时间熟悉规则。';
  const md = '# 报告\n\n## 第一章\n\n#### 典型玩家案例\n\n' + original
    + '\n\n## 第二章\n\n' + original;
  const page = await fixture(t, md);
  const result = await page.evaluate(original => {
    const el = document.getElementById('report-content');
    const ok = PostwriteReview.locate(el, {canLocate: true, original, locations: [{path: ['第一章', '典型玩家案例']}]});
    return {ok, inCase: !!el.querySelector('.report-player-case mark.postwrite-report-highlight'), headings: [...el.querySelectorAll('h1,h2,h4')].map(h => h.textContent)};
  }, original);
  assert.equal(result.ok, true);
  assert.equal(result.inCase, true);
  assert.deepEqual(result.headings, ['报告', '第一章', '典型玩家案例', '第二章']);
  await page.evaluate(() => PostwriteReview.clearHighlights(document.getElementById('report-content')));
  assert.equal(await page.locator('mark').count(), 0);
  assert.equal(await page.locator('#report-content').textContent(), await page.evaluate(() => beforeCaseText));
});

test('desktop and 390px visual evidence: wrapped readable cases and no page overflow', async t => {
  const md = '# 合成报告：玩家追求的停止原因\n\n## 不少追求者已经停下\n\n停下的原因包括单排受挫、版本变化和生活节奏变化。\n\n- **单排受挫**：部分玩家在持续尝试后降低投入。\n- **作战打击信心**：参见下面的案例。\n\n**典型玩家案例**\n\nR73：这是合成展示样本。玩家最初和同学一起练习，希望达到更高水平；后来朋友陆续离开，自己仍断续尝试。一次失败后，他开始减少投入，转而观察榜单和其他玩家的经历。这段经历说明的是个人选择，不用于推断所有玩家。\n\n## 另一个场景\n\n典型玩家案例｜R113：合成玩家调整了目标。长英文链接也应在窄屏自然换行：https://example.invalid/' + 'longpath'.repeat(12) + '\n\n以上案例之外，仍需结合其他玩家的反馈判断。';
  fs.mkdirSync(artifacts, {recursive: true});
  for (const width of [1100, 390]) {
    const page = await fixture(t, md, {status: 'complete'}, width);
    assert.equal(await page.locator('.report-player-case').count(), 2);
    const layout = await page.evaluate(() => ({width: innerWidth, scroll: document.documentElement.scrollWidth,
      boxes: [...document.querySelectorAll('.report-player-case')].map(el => ({width: el.clientWidth, scroll: el.scrollWidth, color: getComputedStyle(el).backgroundColor}))}));
    assert.ok(layout.scroll <= layout.width, JSON.stringify(layout));
    assert.ok(layout.boxes.every(box => box.scroll <= box.width + 1), JSON.stringify(layout));
    await page.screenshot({path: path.join(artifacts, width === 390 ? 'preview-mobile.png' : 'preview-desktop.png'), fullPage: true});
    if (width === 1100) fs.writeFileSync(path.join(artifacts, 'preview.html'), await page.content());
  }
});

test('bounded preview timing uses only local formatting and preserves the Markdown', async t => {
  const md = '# 合成性能样本\n\n' + Array.from({length: 40}, (_, i) =>
    '## 主题' + i + '\n\n普通分析。\n\n典型玩家案例｜R' + (i + 1) + '：' + '合成案例文字。'.repeat(30)).join('\n\n');
  const page = await fixture(t, md);
  const metrics = await page.evaluate(md => {
    const el = document.getElementById('report-content'), samples = [];
    for (let i = 0; i < 5; i++) {
      el.innerHTML = renderMarkdown(md);
      const start = performance.now();
      applyPostwritePlayerCases(el, caseContext);
      samples.push(performance.now() - start);
    }
    return {characters: md.length, cases: el.querySelectorAll('.report-player-case').length, samplesMs: samples,
      markdownUnchanged: caseContext.reportMd === md};
  }, md);
  assert.equal(metrics.cases, 40);
  assert.equal(metrics.markdownUnchanged, true);
  fs.mkdirSync(artifacts, {recursive: true});
  fs.writeFileSync(path.join(artifacts, 'preview-timing.json'), JSON.stringify(metrics, null, 2));
});

test('case typography and peer subheadings stay readable on desktop and narrow light/dark pages', async t => {
  const md = '# 合成展示报告\n\n## 四、合成章节\n\n### 4.1 **合成小节标题**\n\n普通报告正文，其中有**黑体加粗文本**。\n\n典型玩家案例｜R26：这是合成玩家的经历，案例内容应小于普通正文。\n\nR56：第二段合成案例，保留玩家编号与完整文字。\n\n### 5.1 合成第二小节\n\n普通分析段落不进入案例框。\n\n## 八、优化建议\n\n### 第一优先：合成优化方向\n\n合成建议正文。\n\n### 定位层面的提醒\n\n同一层级的无编号标题采用相同格式。\n\n### 典型玩家案例 | R73：标题形式的合成案例。';
  const evidence = [];
  for (const width of [1100, 390]) {
    const page = await fixture(t, md, {status: 'complete'}, width);
    for (const theme of ['light', 'dark']) {
      await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      await page.locator('body').evaluate(async body => {
        await Promise.all(body.getAnimations().map(animation => animation.finished.catch(() => {})));
      });
      const metrics = await page.evaluate(() => {
        const style = element => {
          const value = getComputedStyle(element);
          return {size: value.fontSize, weight: value.fontWeight, color: value.color};
        };
        const el = document.getElementById('report-content');
        return {normal: style(el.querySelector('p')), strong: style(el.querySelector('p strong')),
          chapters: [...el.querySelectorAll('h2')].map(style),
          cases: [...el.querySelectorAll('.report-player-case')].map(style),
          labels: [...el.querySelectorAll('.report-player-case__label')].map(label => ({...style(label), visibleText: label.innerText.trim()})),
          subheadings: [...el.querySelectorAll('h3.report-postwrite-subheading')].map(heading => ({...style(heading), text: heading.textContent,
            strongColor: heading.querySelector('strong') ? getComputedStyle(heading.querySelector('strong')).color : null})),
          caseHeadingStyledAsSection: [...el.querySelectorAll('.report-player-case h3')].some(heading => heading.classList.contains('report-postwrite-subheading')),
          separators: [...el.querySelectorAll('.report-player-case__separator')].map(separator => getComputedStyle(separator).display),
          background: getComputedStyle(document.body).backgroundColor,
          noOverflow: [...el.querySelectorAll('.report-player-case')].every(box => box.scrollWidth <= box.clientWidth + 1)};
      });
      assert.equal(metrics.normal.size, '14px');
      assert.equal(metrics.cases.length, 2);
      assert(metrics.cases.every(item => item.size === '13px'));
      assert(metrics.labels.every(item => item.size === '15px' && item.weight === '700' && item.visibleText === '典型玩家案例'));
      assert.equal(metrics.separators.length, 2);
      assert(metrics.separators.every(display => display === 'none'));
      assert.equal(metrics.subheadings.length, 4);
      assert(metrics.subheadings.some(item => item.text.startsWith('第一优先')));
      assert(metrics.subheadings.some(item => item.text === '定位层面的提醒'));
      assert(metrics.chapters.every(item => item.size === '16px'));
      assert.equal(metrics.caseHeadingStyledAsSection, false);
      const purple = theme === 'light' ? 'rgb(109, 40, 217)' : 'rgb(167, 139, 250)';
      assert(metrics.subheadings.every(item => item.size === '15px' && item.color === purple && item.weight === '700'));
      assert.equal(metrics.subheadings[0].strongColor, purple);
      assert.notEqual(metrics.strong.color, purple);
      function luminance(rgb) {
        const [r, g, b] = rgb.match(/\d+/g).slice(0, 3).map(Number).map(n => n / 255)
          .map(n => n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4);
        return r * .2126 + g * .7152 + b * .0722;
      }
      const foreground = luminance(purple), background = luminance(metrics.background);
      const contrast = (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05);
      assert(contrast >= 4.5, '小节标题对比度不足：' + contrast);
      assert(metrics.noOverflow);
      assert.equal(await page.locator('#report-content').textContent(), await page.evaluate(() => beforeCaseText));
      evidence.push({width, theme, ...metrics, subheadingContrast: contrast});
      fs.mkdirSync(artifacts, {recursive: true});
      await page.screenshot({path: path.join(artifacts, `typography-${width}-${theme}.png`), fullPage: true});
    }
  }
  fs.writeFileSync(path.join(artifacts, 'typography-metrics.json'), JSON.stringify(evidence, null, 2));
});

test('hiding a label separator preserves body punctuation and restores ordinary versions exactly', async t => {
  const md = '## 合成章节\n\n### 4.1 合成小节\n\n典型玩家案例｜R73：正文里的 A|B、C｜D 均保留。';
  const page = await fixture(t, md);
  assert.equal(await page.locator('.report-player-case__separator').count(), 1);
  assert.match(await page.locator('.report-player-case').innerText(), /A\|B、C｜D/);
  await page.evaluate(() => {
    const el = document.getElementById('report-content');
    applyPostwritePlayerCases(el, caseContext);
    applyPostwritePlayerCases(el, {reportAudit: null});
  });
  assert.equal(await page.locator('.report-player-case__separator, .report-postwrite-subheading, .report-player-case__label').count(), 0);
  assert.equal(await page.locator('#report-content').textContent(), await page.evaluate(() => beforeCaseText));
  assert.match(await page.locator('#report-content').innerText(), /典型玩家案例｜R73/);
  assert.equal(await page.locator('h3').evaluate(node => getComputedStyle(node).fontWeight), '600');
  await page.evaluate(() => {
    const el = document.getElementById('report-content');
    applyPostwritePlayerCases(el, caseContext);
    applyPostwritePlayerCases(el, caseContext);
  });
  assert.equal(await page.locator('.report-player-case__separator').count(), 1);
  assert.equal(await page.locator('.report-player-case__label').count(), 1);
  assert.equal(await page.locator('.report-postwrite-subheading').count(), 1);
});

test('real review panel button locates a case containing the hidden separator and a repeated sentence', async t => {
  const original = '典型玩家案例｜R73：**合成玩家**需要时间熟悉规则。';
  const md = '# 合成报告\n\n## 定位章节\n\n### 4.1 合成小节\n\n' + original + '\n\n## 对照章节\n\n' + original;
  const page = await fixture(t, md);
  const model = await page.evaluate(({md, original}) => {
    const ctx = {reportMd: md, reportAudit: {status: 'complete', checks: [{id: 'C001', verdict: 'issue', complete: true,
      original, reason: '合成定位提示', sources: []}], checkpoint: {registry: {complete: true,
        claims: [{id: 'C001', original, block_ids: ['B0004']}]}}}};
    const model = PostwriteReview.build(ctx);
    const panel = document.createElement('section'); panel.id = 'synthetic-review'; document.body.appendChild(panel);
    PostwriteReview.render(panel, model, {canEdit: false, busy: false,
      onLocate: issue => { window.locatedThroughButton = PostwriteReview.locate(document.getElementById('report-content'), issue); }});
    return {issues: model.issues.length, canLocate: model.issues[0].canLocate};
  }, {md, original});
  assert.equal(model.issues, 1);
  assert(model.canLocate);
  await page.getByRole('button', {name: '定位到报告原句', exact: true}).click();
  assert(await page.evaluate(() => locatedThroughButton));
  const marks = await page.locator('mark.postwrite-report-highlight').evaluateAll(nodes => ({count: nodes.length,
    firstCaseOnly: nodes.every(node => node.closest('.report-player-case') === document.querySelector('.report-player-case'))}));
  assert(marks.count > 0 && marks.firstCaseOnly);
  assert.equal(await page.locator('.report-player-case__separator').first().evaluate(node => getComputedStyle(node).display), 'none');
  await page.evaluate(() => PostwriteReview.clearHighlights(document.getElementById('report-content')));
  assert.equal(await page.locator('#report-content').textContent(), await page.evaluate(() => beforeCaseText));
});
