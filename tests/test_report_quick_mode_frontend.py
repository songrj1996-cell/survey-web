"""Executable frontend request contracts; visual acceptance uses the isolated app browser."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'static/index.html').read_text(encoding='utf-8')
QUICK = (ROOT / 'static/js/features/report-quick.js').read_text(encoding='utf-8')
REPORT = (ROOT / 'static/js/features/report.js').read_text(encoding='utf-8')


class QuickReportFrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_outline_keeps_source_buttons_stable_anchors_and_nested_toc(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const quick=fs.readFileSync(process.argv[1],'utf8'),report=fs.readFileSync(process.argv[2],'utf8').replace(/\r\n/g,'\n');
const dataKey=s=>s.replace(/^data-/,'').replace(/-([a-z])/g,(_,c)=>c.toUpperCase());
class Node {
 constructor(tag,text=''){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.events={};this.attrs={};this.scrollTop=0;this.top=0;this.isConnected=true;this.classes=new Set();this.classList={add:x=>this.classes.add(x),toggle:(x,on)=>on?this.classes.add(x):this.classes.delete(x)};if(text)this.textContent=text;}
 get attributes(){return [...Object.entries(this.attrs),...Object.entries(this.dataset).map(([k,v])=>['data-'+k.replace(/[A-Z]/g,c=>'-'+c.toLowerCase()),v])].map(([name,value])=>({name,value}));}
 get firstChild(){return this.children[0];}get textContent(){return this._text||this.children.map(n=>n.textContent).join('');}
 set textContent(t){this.children=[];this._text='';if(t){const n=new Node('#text');n._text=t;this.append(n);}}
 set innerHTML(t){assert.equal(t,'');this.children=[];}set id(v){this.attrs.id=v;}get id(){return this.attrs.id;}
 setAttribute(k,v){if(k.startsWith('data-'))this.dataset[dataKey(k)]=v;else this.attrs[k]=v;}
 removeAttribute(k){if(k.startsWith('data-'))delete this.dataset[dataKey(k)];else delete this.attrs[k];}
 append(...nodes){nodes.forEach(n=>{n.remove();n.parent=this;this.children.push(n);});}appendChild(n){this.append(n);}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;}
 before(...nodes){const p=this.parent,i=p.children.indexOf(this);nodes.forEach(n=>{n.remove();n.parent=p;});p.children.splice(i,0,...nodes);}
 replaceWith(n){this.before(n);this.remove();}
 get nextElementSibling(){return this.parent?.children[this.parent.children.indexOf(this)+1];}
 querySelectorAll(selector){return this.children.flatMap(walk).filter(n=>selector.split(',').some(part=>{const s=part.trim(),m=s.match(/\[([^\]]+)\]/);return (!s.match(/^h[123]/)||n.tagName===s.slice(0,2).toUpperCase())&&(m?dataKey(m[1]) in n.dataset:!!s.match(/^h[123]/));}));}
 closest(selector){for(let n=this;n;n=n.parent){if(selector==='details'&&n.tagName==='DETAILS')return n;if(selector==='.quick-toc-group'&&n.className==='quick-toc-group')return n;}return null;}
 addEventListener(k,f){this.events[k]=f;}removeEventListener(k){delete this.events[k];}
 getBoundingClientRect(){return {top:this.top};}scrollTo(v){this.scrollTop=v.top;}scrollIntoView(){this.located=true;}
}
const walk=n=>[n,...n.children.flatMap(walk)],content=new Node('div'),toc=new Node('ul'),body=new Node('div'),tocPanel={style:{}};
body.append(content);const h0=new Node('h2','经历'),h1=new Node('h2','体验'),h2=new Node('h2','体验');content.append(h0,h1,h2);
let ctx={reportMode:'quick',selectedVersion:1,quickSummary:{objective_stats:{sections:[{question_key:'0',question:'经历',source_order:1}]},questions:[1,2].map(i=>({question_key:String(i),question:'体验',source_order:i+1,status:'failed'}))},quickOutline:{schema_version:1,question_keys:['0','1','2'],groups:[{id:'quick-group-1',title:'基础信息与共同问题',note:'',question_keys:['0']},{id:'quick-group-2',title:'尝试过',note:'推定',question_keys:['1']},{id:'quick-group-3',title:'当前主玩',note:'推定',question_keys:['2']}]}};
const calls=[],state={sessionId:'test',sessionReport:ctx};
const s={console,state,document:{createElement:t=>new Node(t),querySelector:()=>body,querySelectorAll:()=>content.querySelectorAll('[data-quick-question-key]')},$:id=>({'report-content':content,'report-toc-list':toc,'report-toc':tocPanel}[id]||new Node('div')),activeReportCtx:()=>ctx,activeReportId:()=> 'test',activeVersionNumber:()=>ctx.selectedVersion,openReportSources:(...args)=>calls.push(args),revealQuickTarget(){},requestAnimationFrame:f=>{f();return 1;},cancelAnimationFrame(){},normalizeReportVersions:x=>x||[],toFiniteVersion:v=>v==null?null:Number(v)};
vm.createContext(s);vm.runInContext(quick,s);s.openReportSources=(...args)=>calls.push(args);
vm.runInContext(report.slice(report.indexOf('let _reportTocScrollHandler'),report.indexOf('let _tocDebounce')),s);
s.renderQuickInlineReferences(content,ctx);s.renderQuickOutline(content,ctx);
content.querySelectorAll('h2, h3').forEach((n,i)=>n.top=200+i*100);s.buildTOC();
const leaves=()=>content.querySelectorAll('[data-quick-outline-leaf]');
assert.deepEqual(leaves().map(n=>n.tagName),['H3','H3','H3']);
assert.deepEqual(leaves().map(n=>n.id),['quick-question-0','quick-question-1','quick-question-2']);
for(const n of leaves().slice(1))n.children.find(c=>c.tagName==='BUTTON').events.click();
assert.deepEqual(calls.map(c=>c[0]),['1','2'],'duplicate titles must retain distinct sources');
const details=walk(toc).filter(n=>n.tagName==='DETAILS');assert.equal(details.length,3);
assert(details.every(n=>n.children[0].tagName==='SUMMARY'&&n.children[1].tagName==='UL'));
assert.deepEqual(details.map(d=>d.children[1].children[0].children[0].textContent),['经历','体验','体验']);
// Moving to a question automatically opens the containing group.
details[2].open=false;leaves()[2].top=60;body.events.scroll();assert.equal(details[2].open,true);
s.locateQuickRetryQuestion({reportId:'test',questions:[{question_key:'2'}],items:{}});assert(leaves()[2].located);
const button=leaves()[2].children.find(c=>c.tagName==='BUTTON');
s.resetQuickOutline(content);s.renderQuickInlineReferences(content,ctx);s.renderQuickOutline(content,ctx);
assert.equal(leaves().length,3,'hydration must not duplicate groups');
assert.equal(content.querySelectorAll('[data-quick-outline-group]').length,3);
ctx.selectedVersion=2;button.events.click();assert.equal(calls.length,2,'stale buttons stay inactive');
s.resetQuickOutline(content);ctx.quickOutline.groups[1].question_keys=['2'];s.renderQuickOutline(content,ctx);
assert.equal(leaves().length,0,'missing or duplicated membership must stay flat');
assert.equal(content.querySelectorAll('h2').length,3);
// Version metadata switches must not retain the previous outline.
vm.runInContext(report.slice(report.indexOf('function syncReportVersionMeta('),report.indexOf('function syncReportVersionMeta(')+report.slice(report.indexOf('function syncReportVersionMeta(')).indexOf('\n}\n')+3),s);
ctx.id='test';s.syncReportVersionMeta(ctx,{version:1,selected_version:1,quick_outline:{schema_version:1},report_mode:'quick'});
s.syncReportVersionMeta(ctx,{version:2,selected_version:2,report_mode:'quick'});assert.equal(ctx.quickOutline,null);
console.log('Outline source links, duplicate titles, rehydration, nested TOC, retry anchors and version clearing passed');
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT/'static/js/features/report-quick.js'), str(ROOT/'static/js/features/report.js')], capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_version_limit_is_checked_before_dialog_navigation_or_generation(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const report=fs.readFileSync(process.argv[1],'utf8'),quick=fs.readFileSync(process.argv[2],'utf8');
const nodes=new Map(),toasts=[],requests=[];
const state={sessionId:'session-a',viewMode:'session',sessionReport:{reportMode:'quick',reportMd:'KEEP',selectedVersion:2,maxVersions:5,versions:[1,2,3,4,5].map(version=>({version}))},historyReport:{}};
let ctx=state.sessionReport,server=[1,2,3,4,5],fail=false,hold=null;
const s={state,console,Map,Set,URL,URLSearchParams,window:{location:{origin:'http://localhost'}},
 $:id=>{if(!nodes.has(id))nodes.set(id,{events:{},addEventListener(k,f){this.events[k]=f;},showModal(){throw Error('blocked dialog opened');}});return nodes.get(id);},
 document:{querySelectorAll:()=>[]},activeReportCtx:()=>ctx,activeReportId:()=>state.viewMode==='history'?'history-a':state.sessionId,activeVersionNumber:()=>ctx.selectedVersion,
 reportInteractionBusy:()=>false,normalizeReportVersions:v=>v,isQuickRetryRunning:()=>false,
 showToast:(...v)=>toasts.push(v),updateReportActionAvailability(){},updateReportVersionUi(){},
 fetch:async url=>{requests.push(url);if(fail)throw Error('network unavailable');if(hold)await hold;return {ok:true,json:async()=>({versions:server.map(version=>({version})),max_versions:5})};},
 goStep(){throw Error('blocked navigation');},consumeSSEPost(){throw Error('blocked model request');}
};
vm.createContext(s);
vm.runInContext(report.slice(report.indexOf('let quickRegenerationChecking'),report.indexOf('function reportInteractionBusy')),s);
vm.runInContext(report.slice(report.indexOf('async function runStats('),report.indexOf("$('btn-report-retry')")),s);
vm.runInContext(quick,s);
(async()=>{
 const before=JSON.stringify(state);
 await nodes.get('btn-report-regenerate').events.click();
 assert.equal(await s.runStats({regenerate:true,baseVersion:2}),false);
 assert.equal(JSON.stringify(state),before);assert.equal(requests.length,0);assert(toasts.some(v=>v[0].includes('上限')));
 // Old selected V2 cannot evade the cap. Fresh metadata also catches another tab's fifth version.
 ctx.versions=[1,2,3,4].map(version=>({version}));
 assert.equal(await s.ensureQuickRegenerationAllowed(),false);assert.equal(requests.length,1);assert.equal(ctx.selectedVersion,2);
 // Count saved versions, not their highest label; deleted numbers may leave gaps.
 ctx.versions=[1,2,4,7].map(version=>({version}));server=[1,2,4,7];
 assert.equal(await s.ensureQuickRegenerationAllowed(),true);
 state.viewMode='history';ctx={...ctx,versions:[1,2,3,4,5].map(version=>({version}))};state.historyReport=ctx;
 const count=requests.length;assert.equal(await s.ensureQuickRegenerationAllowed(),false);assert.equal(requests.length,count);
 ctx.versions=[{version:1}];server=[1];fail=true;
 assert.equal(await s.ensureQuickRegenerationAllowed(),false);assert.equal(ctx.reportMd,'KEEP');fail=false;
 let release;hold=new Promise(r=>release=r);const pending=s.ensureQuickRegenerationAllowed();
 assert.equal(await s.ensureQuickRegenerationAllowed(),false);
 const old=ctx;ctx={...ctx,selectedVersion:3,versions:[{version:3}]};release();
 assert.equal(await pending,false);assert.deepEqual(ctx.versions,[{version:3}]);assert.equal(old.versions.length,1);
 console.log('Version-cap entry, immutable reading state, history, gaps, stale metadata, duplicates and query failure passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT/'static/js/features/report.js'), str(ROOT/'static/js/features/report-quick.js')], capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_inline_retry_preserves_reading_versions_and_cancellation_ownership(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
class Element {
 constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.events={};this._text='';this.isConnected=true;this.scrollTop=720;}
 append(...nodes){nodes.forEach(n=>{n.parent=this;this.children.push(n);});}
 replaceChildren(...nodes){this.children=[];this._text='';this.append(...nodes);}
 set textContent(v){this._text=String(v);this.children=[];}get textContent(){return this._text+this.children.map(n=>n.textContent).join(' ');}
 setAttribute(k,v){this[k]=v;}addEventListener(k,f){this.events[k]=f;}
 contains(n){return walk(this).includes(n);}remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
 querySelectorAll(s){return walk(this).filter(n=>s.includes('h2')?n.tagName==='H2':s==='[data-quick-retry-action]'?n.dataset.quickRetryAction:false);}
 getBoundingClientRect(){return {top:100};}focus(){}scrollIntoView(){this.located=true;}
}
const walk=n=>[n,...n.children.flatMap(walk)],elements=new Map(),body=new Element(),heading=new Element('h2');
heading.dataset.quickQuestionKey='18';body.append(heading);
const pending=[...Array.from({length:17},(_,i)=>({question_key:String(i+1),question:'完成题'+i,status:'complete'})),{question_key:'18',question:'失败题',status:'failed'}];
const current={reportMode:'quick',selectedVersion:2,version:2,reportMd:'ORIGINAL',quickSummary:{questions:pending},versions:[{version:1},{version:2}]};
const state={sessionId:'session-a',viewMode:'session',sessionReport:current,historyReport:{},reportMd:'ORIGINAL',reportVersionLoading:false};
let id='session-a',ctx=current,requests=[],cancels=[],loads=[],toasts=[];
const sandbox={state,console,Map,Set,URL,URLSearchParams,window:{location:{origin:'http://localhost'}},
  document:{activeElement:null,createElement:t=>new Element(t),querySelector:s=>s==='.report-body'?body:null,querySelectorAll:s=>s.includes('[data-quick-question-key]')?[heading]:s==='[data-quick-retry-marker]'?walk(body).filter(n=>n.dataset.quickRetryMarker):[]},
 $:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
 activeReportId:()=>id,activeReportCtx:()=>ctx,activeVersionNumber:()=>ctx.selectedVersion,
 reportInteractionBusy:()=>!!state.quickRetry?.running,normalizeReportVersions:x=>x,
 showToast:(...v)=>toasts.push(v),updateReportActionAvailability(){},updateReportVersionUi(){},applyQAAvailability(){},
 consumeSSEPost:(url,data,callback)=>new Promise((resolve,reject)=>requests.push({url,data,callback,resolve,reject})),
 fetch:async(url,options)=>{cancels.push({url,options});return {ok:true,json:async()=>({cancelled:true})};},
 loadSessionReportVersion:async(v)=>{loads.push(['session',v]);ctx.selectedVersion=v;state.reportMd='NEW';body.scrollTop=0;return true;},
 loadHistoryReportVersion:async(v)=>{loads.push(['history',v]);ctx.selectedVersion=v;return true;}
};
vm.createContext(sandbox);vm.runInContext(source,sandbox);
const tick=()=>new Promise(r=>setImmediate(r)),host=()=>elements.get('quick-retry-status');
const button=a=>walk(host()).find(n=>n.dataset.quickRetryAction===a);
const done=(version,status='complete')=>({type:'report_done',version,report_status:status,versions:[{version:2},{version}],report_md:'NEW',quick_summary:{questions:pending.map(q=>({...q,status:status==='complete'?'complete':q.status}))}});
(async()=>{
 const before=JSON.stringify(current.quickSummary);
 const running=sandbox.retryFailedQuickReport();
 assert.equal(requests.length,1);assert.equal(requests[0].data.base_version,2);
 assert(host().textContent.includes('已完成 0/1'));assert(!host().textContent.includes('17/18'));
 assert.equal(body.scrollTop,720);assert.equal(state.reportMd,'ORIGINAL');assert.equal(ctx.selectedVersion,2);
 await sandbox.retryFailedQuickReport();assert.equal(requests.length,1,'duplicate retry is blocked');
 requests[0].callback({type:'analysis_progress',phase:'quick_questions',question_key:'1',status:'reused'});
 assert(host().textContent.includes('已完成 0/1'));
 requests[0].callback({type:'analysis_progress',phase:'quick_questions',question_key:'18',status:'running'});
 button('locate').events.click();assert(heading.located);
 requests[0].callback({type:'analysis_progress',phase:'quick_questions',question_key:'18',status:'complete'});
 assert(host().textContent.includes('已完成 1/1'));
 requests[0].callback(done(3));requests[0].resolve();await running;
 assert.equal(loads.length,0);assert.equal(state.reportMd,'ORIGINAL');assert.equal(ctx.selectedVersion,2);
 assert.equal(JSON.stringify(current.quickSummary),before);assert.equal(state.sessionId,'session-a');
 assert(host().textContent.includes('V3 已就绪'));assert.equal(ctx.versions.at(-1).version,3);
 await sandbox.viewQuickRetryUpdate();assert.deepEqual(loads,[['session',3]]);assert.equal(state.quickRetry,null);assert(host().hidden);
 // A partial result is retried against its saved version, without forcing the reader to open it first.
 ctx.selectedVersion=2;const second=sandbox.retryFailedQuickReport();
 requests[1].callback(done(4,'partial'));requests[1].resolve();await second;
 button('retry').events.click();assert.equal(requests[2].data.base_version,4);assert.equal(ctx.selectedVersion,2);
 requests[2].reject(new Error('network unavailable'));await tick();
 assert(host().textContent.includes('network unavailable'));assert(!sandbox.isQuickRetryRunning());assert(button('retry'));
 // History cancellation must wait for the restored job's session ID.
 id='history-a';state.viewMode='history';state.historyId=id;ctx={...current,id,selectedVersion:2};state.historyReport=ctx;
 const historical=sandbox.retryFailedQuickReport();assert.equal(requests[3].data.history_id,'history-a');
 await sandbox.stopQuickReportRetry();assert.equal(cancels.length,0);
 requests[3].callback({type:'session_ready',session_id:'restored-job'});await tick();
 assert.equal(cancels[0].url,'/api/report/restored-job/cancel');assert.equal(state.sessionId,'session-a');
 requests[3].callback({type:'cancelled'});requests[3].resolve();await historical;
 assert(host().textContent.includes('本次重试已停止'));assert.equal(ctx.selectedVersion,2);
 // Switching reports while SSE is in flight does not transplant metadata or content.
 const stale=sandbox.retryFailedQuickReport();const own=ctx;
 id='history-b';ctx={reportMode:'quick',selectedVersion:9,versions:[{version:9}],quickSummary:{questions:[]}};state.historyReport=ctx;
 sandbox.renderQuickRetryStatus();assert(host().hidden);
 requests[4].callback(done(5));requests[4].resolve();await stale;
 assert.equal(ctx.selectedVersion,9);assert.equal(ctx.versions.length,1);assert(host().hidden);
 id='history-a';ctx=own;state.historyReport=own;sandbox.renderQuickRetryStatus();assert(!host().hidden);
 await sandbox.viewQuickRetryUpdate();assert.deepEqual(loads.at(-1),['history',5]);
 // An obsolete stream cannot modify a newer job, including its running state.
 ctx.selectedVersion=2;const obsolete=sandbox.retryFailedQuickReport();const fresh={running:true,reportId:'elsewhere'};state.quickRetry=fresh;
 requests[5].callback(done(6));requests[5].resolve();await obsolete;assert.equal(state.quickRetry,fresh);assert(fresh.running);
 // Objective questions have no quick-question key, but must retain their reading position too.
 delete heading.dataset.quickQuestionKey;heading.id='toc-h-0';body.scrollTop=720;let offset=800;
 heading.getBoundingClientRect=()=>({top:100+offset-body.scrollTop});
 const objectivePosition=sandbox.captureQuickReadingPosition();offset+=40;
 sandbox.restoreQuickReadingPosition(objectivePosition);assert.equal(body.scrollTop,760);
 // At five versions, completion refreshes the same version and keeps the reading position.
 state.quickRetry=null;id='session-a';state.viewMode='session';ctx=current;state.sessionReport=ctx;
 ctx.selectedVersion=2;ctx.versions=[1,2,3,4,5].map(version=>({version}));ctx.maxVersions=5;body.scrollTop=720;
 const complete=sandbox.retryFailedQuickReport();assert.equal(requests[6].data.base_version,2);
 requests[6].callback({...done(2),completion:true,versions:ctx.versions,max_versions:5,active_version:5,next_version:6});
 requests[6].resolve();await complete;
 assert.deepEqual(loads.at(-1),['session',2]);assert.equal(ctx.selectedVersion,2);assert.equal(ctx.versions.length,5);
 assert.equal(body.scrollTop,720);assert.equal(state.quickRetry,null);assert(toasts.some(t=>t[0].includes('版本数量不变')));
 // Corrupt frozen inputs fail closed and offer refresh instead of an endless retry loop.
 const blocked=sandbox.retryFailedQuickReport();requests[7].reject(new Error('原版本缺少完整画像资料，已停止补全'));await blocked;
 assert(!button('retry'));assert(button('refresh'));assert.equal(state.reportMd,'NEW');
 await button('refresh').events.click();assert.equal(state.quickRetry,null);
 console.log('Inline retry counts, reading retention, immutable selection, partial continuation, cancel ownership and stale events passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT/'static/js/features/report-quick.js')], capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_replaced_late_style_picker_with_three_early_modes(self):
        self.assertNotIn('id="report-style-picker"', HTML)
        self.assertEqual(re.findall(r'data-entry-focus="(\w+)"', HTML), ['quick','insight','statistics'])
        self.assertNotIn('id="report-export-scope"', HTML)
        self.assertEqual(re.findall(r'data-export-format-trigger="(\w+)"', HTML), ['pdf','word','md','feishu'])
        self.assertEqual(HTML.count('data-export-scope="evidence"'), 4)
        self.assertEqual(HTML.count('data-export-scope="body"'), 4)
        self.assertNotIn('evidence_catalog', QUICK)

    def test_all_new_dialog_elements_exist_before_feature_scripts(self):
        ids = []
        class Parser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                value = dict(attrs).get('id')
                if value: ids.append(value)
        Parser().feed(HTML)
        for node_id in set(re.findall(r"\$\('([^']+)'\)", QUICK)):
            self.assertEqual(ids.count(node_id), 1, node_id)
            self.assertLess(HTML.index('id="'+node_id+'"'), HTML.index('/static/js/features/report-quick.js'))
        self.assertEqual(len(ids), len(set(ids)), 'duplicate ids break dialog and source routing')

    def test_export_and_sources_use_selected_snapshot(self):
        self.assertIn('activeVersionNumber(ctx)', QUICK)
        self.assertIn("params.set('history_id',context.historyId)", QUICK)
        self.assertIn('text.textContent = item.text', QUICK)
        self.assertIn("target.searchParams.set('scope', scope === 'evidence' ? 'evidence' : 'body')", QUICK)
        self.assertNotIn("new Blob([state.reportMd", REPORT)
        self.assertIn('reportExportUrl(`/api/export/${type}${history}/', REPORT)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for executable frontend contracts')
    def test_generation_routes_skip_planning_stats_and_freeze_retry_base(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const code=source.slice(source.indexOf('async function runStats('),source.indexOf("$('btn-report-retry')"));
function element(){return {classList:{add(){},remove(){}},style:{},querySelector(){return element();},setAttribute(){},append(){},textContent:'',innerHTML:''};}
(async()=>{
for(const test of [
 {mode:'quick',options:{},stats:false,method:'get'},
 {mode:'quick',options:{},stats:false,method:'get',cancel:true},
 {mode:'insight',options:{},stats:true,method:'get'},
 {mode:'statistics',options:{},stats:true,method:'get'},
 {mode:'quick',options:{retryFailed:true,baseVersion:3},stats:false,method:'post',suffix:'retry-failed'},
 {mode:'quick',options:{regenerate:true,baseVersion:2,instruction:'保留风险'},stats:false,method:'post',suffix:'versions'},
 {mode:'quick',options:{regenerate:true,baseVersion:2},stats:false,method:'post',suffix:'versions',capRace:true},
 {mode:'quick',options:{regenerate:true,baseVersion:2,historyId:'history-a'},stats:false,method:'post',suffix:'versions',capRace:true},
 {mode:'quick',options:{retryFailed:true,historyId:'history-a',baseVersion:4},stats:false,method:'post',suffix:'retry-failed'},
]){
const calls=[],elements=new Map();
const state={sessionId:'session-a',reportMode:test.mode,sessionReport:{reportMode:test.mode,reportMd:'KEEP ORIGINAL',versions:[],versionInstructions:{}},viewMode:'session'};
if(test.capRace&&test.options.historyId){state.viewMode='history';state.historyId='history-a';state.historyReport={reportMode:'quick',reportMd:'KEEP HISTORY'};}
const sandbox={state,Number,Date,Map,Set,console,
 ensureQuickRegenerationAllowed:async()=>true,
 $:id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);},
 window:{setInterval(){return 1;},clearInterval(){}},document:{createTextNode:v=>v},
 activeReportCtx:()=>state.viewMode==='history'?state.historyReport:state.sessionReport,toFiniteVersion:value=>value?Number(value):null,
 fetch:async(url,options)=>{calls.push({url,method:options.method});return {ok:true};},
 consumeSSE:async(url,callback)=>{calls.push({url,method:'get'});if(test.cancel){callback({type:'cancelled',message:'stopped'});return;}callback({type:'report_done',report_md:'# Summary',version:1,report_mode:test.mode,report_status:'complete'});},
 consumeSSEPost:async(url,body,callback)=>{calls.push({url,body,method:'post'});if(test.capRace)throw Error('报告版本已达上限（5 个）');if(test.options.historyId)callback({type:'session_ready',session_id:'restored-session'});callback({type:'report_done',report_md:'# Summary',version:5,report_mode:test.mode,report_status:'partial'});},
 renderReportWorkspace:()=>{calls.push({restored:true});},
 _createReportTaskProgress:()=>({structured:false,items:new Map(),phases:{},details:[]}),
 normalizeReportLlmUsage:v=>v,reportTitleFromMarkdown:()=> 'Summary',
 syncReportVersionMeta:(target,meta)=>{target.savedMeta=meta;},
 EMPTY_RERUN_INSTRUCTION:'none',
};
for(const name of ['showToast','goStep','resetReportFailureUi','updateReportVersionUi','applyQAAvailability','showReport','showReportFailureUi','_formatReportWaitTime','updateReportContextSwitch'])sandbox[name]=()=>{};
vm.createContext(sandbox);vm.runInContext(code,sandbox);
assert.equal(await sandbox.runStats(test.options),!test.cancel&&!test.capRace,JSON.stringify(test));
if(test.capRace){assert.equal(state.sessionReport.reportMd,'KEEP ORIGINAL');assert(calls.some(c=>c.restored));if(test.options.historyId){assert.equal(state.viewMode,'history');assert.equal(state.historyReport.reportMd,'KEEP HISTORY');assert.equal(state.sessionId,'session-a');}continue;}
assert.equal(calls.some(c=>c.url.includes('/stats/')),test.stats);
const report=calls.find(c=>c.url.includes('/report/'));
assert.equal(report.method,test.method);
if(test.suffix){assert(report.url.endsWith('/'+test.suffix));assert.equal(report.body.base_version,test.options.baseVersion);}
if(test.options.historyId){assert.equal(report.body.history_id,'history-a');assert.equal(state.sessionId,'restored-session');}
assert.equal(state.sessionReport.running,false);
assert.equal(elements.get('report-generation-title').textContent, {quick:'生成快速总结',insight:'生成观点洞察',statistics:'生成统计解读'}[test.mode]);
if(test.mode==='quick'){assert(elements.get('report-generation-description').textContent.includes('粗略频次'));assert(!elements.get('report-generation-description').textContent.includes('统计结果'));}
}
console.log('Frontend generation requests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT/'static/js/features/report.js')], capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for executable frontend contracts')
    def test_quick_breadcrumb_and_comparison_badge_follow_selected_version(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8'),elements=new Map();
let ctx={reportMode:'quick',reportStyle:'quick'};
const sandbox={state:{mode:null,viewMode:'session',sessionReport:{}},activeReportCtx:()=>ctx,
 $:id=>{if(!elements.has(id))elements.set(id,{style:{},dataset:{},querySelectorAll:()=>[],replaceChildren(){},append(){}});return elements.get(id);},
 formatReportDuration:()=>'',esc:v=>v,setComparisonValidationModalOpen(){},normalizedComparisonValidationStatus:()=> 'legacy',comparisonAuditRow:()=>({})};
vm.createContext(sandbox);
vm.runInContext(source.slice(source.indexOf('function renderReportBreadcrumb()'),source.indexOf("document.querySelectorAll('[data-report-tab]')",source.indexOf('function renderReportBreadcrumb()'))),sandbox);
vm.runInContext(source.slice(source.indexOf('function renderComparisonValidation('),source.indexOf('function renderReportWorkspace(')),sandbox);
sandbox.renderReportBreadcrumb();
assert(!elements.get('report-breadcrumb').innerHTML.includes('方案确认'));
assert(elements.get('report-breadcrumb').innerHTML.includes('4. 报告 & 追问'));
sandbox.renderComparisonValidation({});
assert.equal(elements.get('btn-comparison-validation').hidden,true);
assert.equal(elements.get('comparison-validation-alert').hidden,true);
ctx={reportMode:'insight',reportStyle:'full'};
sandbox.renderReportBreadcrumb();sandbox.renderComparisonValidation({});
assert(elements.get('report-breadcrumb').innerHTML.includes('方案确认'));
assert.equal(elements.get('btn-comparison-validation').hidden,false);
console.log('Selected-version breadcrumb and audit availability passed');
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT/'static/js/features/report.js')], capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for executable frontend contracts')
    def test_source_controls_survive_body_render_and_resolve_selected_evidence(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.events={};this.classList={toggle(){}};this._text='';}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(){}
  addEventListener(name,callback){this.events[name]=callback;}
  querySelectorAll(){return [];}
  querySelector(selector){return this.children.find(c=>"."+c.className===selector)||null;}
  set textContent(value){this._text=value;}
  get textContent(){return this._text+this.children.map(c=>c.textContent||'').join(' ');}
}
const elements=new Map(),calls=[];
const sandbox={console,Set,URL,URLSearchParams,state:{viewMode:'session'},window:{location:{origin:'http://localhost'}},
 document:{createElement:tag=>new Element(tag),querySelectorAll:()=>[]},
 $:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},reportInteractionBusy:()=>false};
vm.createContext(sandbox);vm.runInContext(source,sandbox);
sandbox.openReportSources=(key,label,view)=>calls.push({key,label,view});
const first={reportMode:'quick',reportStatus:'partial',quickSummary:{questions:[
 {question_key:'3',question:'Q1 体验',status:'complete',findings:[{evidence_ids:['r1','unknown']}],sources:[{response_id:'r1',text:'<script>literal</script>'},{response_id:'r2',text:'not selected'}]},
 {question_key:'7',question:'Q2 失败',status:'failed',findings:[],sources:[]}
]}};
sandbox.renderQuickReportNavigation('# Body',first);
const host=elements.get('report-evidence-tools');
assert(host.textContent.includes('仅重试失败题目'));
assert(!host.textContent.includes('精选证据'));
assert(!host.textContent.includes('逐题原文'));
assert.equal(elements.get('report-content').children.length,0);
assert.equal(sandbox.quickEvidenceItems(first.quickSummary.questions[0])[0].text,'<script>literal</script>');
const second={reportMode:'quick',reportStatus:'complete',quickSummary:{questions:[{question_key:'9',question:'V1 旧题',status:'complete',findings:[],sources:[]}]}};
sandbox.renderQuickReportNavigation('# Old version',second);
assert(!host.textContent.includes('Q1 体验'));
assert(!host.textContent.includes('仅重试失败题目'));
assert(!host.textContent.includes('V1 旧题'));
assert.equal(host.children.length,0);
console.log('Source host persistence, partial status and selected evidence cases passed');
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT/'static/js/features/report-quick.js')], capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for executable frontend contracts')
    def test_explicit_export_scope_retains_login_and_frozen_version(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const report=fs.readFileSync(process.argv[1],'utf8'),quick=fs.readFileSync(process.argv[2],'utf8');
const calls=[],button={innerHTML:'Feishu',disabled:false},state={viewMode:'history',sessionReport:{},feishu:{configured:true,logged_in:false}};
const sandbox={state,window:{location:{origin:'http://localhost',href:''}},location:{pathname:'/'},URL,
 activeReportInteractionBusy:()=>false,activeReportId:()=> 'history-a',activeVersionNumber:()=>3,confirmComparisonValidationExport:()=>true,
 $:()=>button,showToast(){},showFeishuConfirmModal:async()=>true,showFeishuLink(){},
 navigator:{clipboard:{writeText:async()=>{}}},refreshFeishuStatus:async()=>{},
 fetch:async(url,options)=>{calls.push({url,method:options.method});return {ok:true,json:async()=>({url:'https://example.test/document'})};}};
vm.createContext(sandbox);
vm.runInContext(quick.slice(quick.indexOf('function reportExportUrl('),quick.indexOf('const quickReportHydrations')),sandbox);
vm.runInContext(report.slice(report.indexOf('function downloadReportFile('),report.indexOf('// ── 飞书登录状态')),sandbox);
vm.runInContext(report.slice(report.indexOf('async function exportFeishu('),report.indexOf('function showFeishuLink(')),sandbox);
(async()=>{
for(const format of ['pdf','word','md'])for(const scope of ['body','evidence']) {
 sandbox.downloadReportFile(format,scope);
 const type=format==='md'?'markdown':format;
 assert.equal(sandbox.window.location.href,`/api/export/${type}-history/history-a?version=3&scope=${scope}`);
}
await sandbox.exportFeishu('evidence');assert.equal(calls.length,0);assert(sandbox.window.location.href.startsWith('/api/feishu/login?'));
state.feishu.logged_in=true;
await sandbox.exportFeishu('evidence');assert.deepEqual(calls,[{url:'/api/export/feishu-history/history-a?version=3&scope=evidence',method:'POST'}]);
console.log('All formats preserve explicit scope, selected version and Feishu login');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT/'static/js/features/report.js'), str(ROOT/'static/js/features/report-quick.js')], capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for executable frontend contracts')
    def test_partial_metadata_is_retained_only_for_same_snapshot(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const state={sessionId:'s',sessionReport:{}};
const sandbox={state,normalizeReportVersions:v=>v||[],toFiniteVersion:v=>v?Number(v):null,normalizeReportLlmUsage:v=>v};
vm.createContext(sandbox);vm.runInContext(source.slice(source.indexOf('function syncReportVersionMeta('),source.indexOf('function activeVersionNumber(')),sandbox);
const target={id:'s',versions:[],versionInstructions:{}};
const summary={questions:[{question_key:'2',status:'failed'}]};
sandbox.syncReportVersionMeta(target,{version:2,selected_version:2,report_mode:'quick',report_status:'partial',quick_summary:summary});
sandbox.syncReportVersionMeta(target,{versions:[{version:2,report_style:'quick',report_mode:'quick',report_status:'partial'}],selected_version:2});
assert.equal(target.quickSummary,summary);assert.equal(target.reportStatus,'partial');
sandbox.syncReportVersionMeta(target,{version:1,selected_version:1,report_mode:'quick'});
assert.equal(target.quickSummary,null,'a different version must not borrow current sources');
target.id='another-history';sandbox.syncReportVersionMeta(target,{version:1,selected_version:1});assert.equal(target.quickSummary,null);
console.log('Snapshot metadata and source isolation passed');
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT/'static/js/features/report.js')], capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for executable frontend contracts')
    def test_export_hover_then_click_keeps_scope_menu_open(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8'),nodes=new Map();
function node(){return {events:{},dataset:{},attributes:{},classList:{toggle(){},add(){},remove(){}},addEventListener(name,fn){this.events[name]=fn;},setAttribute(name,value){this.attributes[name]=value;},getAttribute(name){return this.attributes[name];}};}
const trigger=node();trigger.dataset.exportFormatTrigger='pdf';trigger.closest=selector=>selector==='[data-export-format-trigger]'?trigger:null;
const submenu={hidden:true,querySelector(){return {focus(){}};}};
const group=node();group.dataset.exportFormat='pdf';group.querySelector=selector=>selector==='[data-export-format-trigger]'?trigger:submenu;
const sandbox={document:{querySelectorAll:()=>[group],addEventListener(){}},$:id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);},setComparisonValidationModalOpen(){},setReportLlmPopoverOpen(){},setReportVersionMenuOpen(){}};
vm.createContext(sandbox);vm.runInContext(source.slice(source.indexOf('function setReportExportFormat('),source.indexOf("$('btn-qa-send').addEventListener")),sandbox);
group.events.pointerenter({pointerType:'mouse'});
assert.equal(submenu.hidden,false);
nodes.get('export-dropdown-menu').events.click({target:trigger,stopPropagation(){}});
assert.equal(submenu.hidden,false,'click after hover must not toggle the submenu closed');
assert.equal(trigger.attributes['aria-expanded'],'true');
sandbox.closeReportExportMenu();assert.equal(submenu.hidden,true);
nodes.get('export-dropdown-menu').events.click({target:trigger,stopPropagation(){}});
assert.equal(submenu.hidden,false,'click without hover also opens submenu');
console.log('Hover then click, close, and click-only export sequences passed');
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT/'static/js/features/report.js')], capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for executable frontend contracts')
    def test_source_translation_page_scope_failure_retry_and_stale_responses(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.events={};this.classList={toggle(){}};this._text='';}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;this._text='';}
  setAttribute(name,value){this[name]=value;}
  addEventListener(name,callback){this.events[name]=callback;}
  querySelectorAll(){return [];}
  querySelector(selector){return this.children.find(c=>"."+c.className===selector)||null;}
  showModal(){this.open=true;}
  close(){this.open=false;this.events.close?.();}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent||'').join(' ');}
}
const elements=new Map(),requests=[],readingBody={scrollTop:444};
const tabs=['evidence','all'].map(view=>{const e=new Element('button');e.dataset.sourceView=view;return e;});
const rows=[{response_id:'r1',text:'English <script>literal</script>',question_key:'q1',question:'Q1 体验',ids:{'MLBB ID':'12345','玩家 ID':'u1'},profile:{'段位':'Mythic','次数':0}},
 {response_id:'r2',text:'Another reply',question_key:'q1',question:'Q1 体验'}];
let selectedId='session-a',version=2;
let selected={reportMode:'quick',quickSummary:{questions:[
 {question_key:'q1',question:'Q1 体验',findings:[{evidence_ids:['r1','r2']}],sources:[...rows,{response_id:'r3',text:'Unselected evidence'}]},
 {question_key:'q2',question:'Q2 原因',findings:[{evidence_ids:['r4']}],sources:[{response_id:'r4',text:'Different question'}]}
]}};
const sandbox={console,Set,Map,URL,URLSearchParams,state:{viewMode:'session'},window:{location:{origin:'http://localhost'}},
 document:{createElement:tag=>new Element(tag),querySelector:()=>readingBody,querySelectorAll:selector=>selector==='[data-source-view]'?tabs:[]},
 $:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
 activeReportCtx:()=>selected,activeReportId:()=>selectedId,activeVersionNumber:()=>version,
 fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,respond:(data,ok=true)=>resolve({ok,json:async()=>data})}))};
vm.createContext(sandbox);vm.runInContext(source,sandbox);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const list=()=>elements.get('report-sources-list');
const walk=node=>[node,...node.children.flatMap(walk)];
const complete=(row,translation)=>({...row,translation_status:'complete',translation_zh:translation});
(async()=>{
 sandbox.openReportSources('q1','Q1 体验','evidence');
 assert(list().textContent.includes(rows[0].text),'originals appear before translation finishes');
 assert(list().textContent.includes('正在翻译'),'first viewing shows a truthful waiting state');
 assert(!list().textContent.includes('Unselected evidence'),'translation does not broaden selected evidence');
 assert.equal(requests.length,1);
 assert(list().textContent.includes('12345'));assert(list().textContent.includes('Mythic'));assert(list().textContent.includes('次数'));assert(list().textContent.includes('0'));
 assert.equal(requests[0].url,'/api/report/session-a/sources/translate');
 assert.equal(requests[0].options.method,'POST');
 assert.deepEqual(JSON.parse(requests[0].options.body),{version:2,response_ids:['r1','r2']},'only frozen IDs and version are sent');
 requests[0].respond({items:[{...rows[1],translation_status:'failed',translation_error:'服务暂不可用'},complete(rows[0],'中文 <img> 仍为文本')]});await tick();
 assert(list().textContent.includes('中文 <img> 仍为文本'));
 assert(list().textContent.includes('服务暂不可用'));
 assert.equal(walk(list()).filter(e=>e.tagName==='IMG').length,0);
 assert(list().children[0].textContent.includes('中文 <img> 仍为文本'),'reordered response aligns by source ID');
 const retry=walk(list()).find(e=>e.tagName==='BUTTON'&&e.textContent==='重试翻译');
 retry.events.click();
 assert.deepEqual(JSON.parse(requests[1].options.body).response_ids,['r2'],'retry sends only the failed entry');
 assert(!list().children[1].textContent.includes('重试翻译'),'pending retry cannot be submitted twice');
 requests[1].respond({items:[{...complete(rows[1],'第二条译文'),ids:{'玩家 ID':'old-source-id'},profile:{'段位':'Epic'}}]});await tick();
 assert(list().children[1].textContent.includes('old-source-id'));assert(list().children[1].textContent.includes('Epic'));
 assert(list().children[0].textContent.includes('中文 <img> 仍为文本'),'success survives failed-only retry');
 assert(list().children[1].textContent.includes('第二条译文'));
 // All-source mode also renders originals before requesting only the visible page.
 tabs[1].events.click();assert(requests[2].url.includes('/sources?'));
 requests[2].respond({items:rows,total:51});await tick();
 assert.deepEqual(JSON.parse(requests[3].options.body).response_ids,['r1','r2']);
 assert(list().textContent.includes(rows[0].text));
 elements.get('report-sources-next').events.click();
 assert(requests[4].url.includes('offset=50'));
 const page2={response_id:'r51',text:'Last page',question_key:'q1',question:'Q1 体验'};
 requests[4].respond({items:[page2],total:51});await tick();
 requests[3].respond({items:rows.map(row=>complete(row,'STALE PAGE'))});await tick();
 assert(!list().textContent.includes('STALE PAGE'));
 requests[5].respond({items:[complete(page2,'最后一页译文')]});await tick();
 assert(list().textContent.includes('最后一页译文'));
 assert(!list().textContent.includes(rows[0].text),'prior page does not remain cached in the visible list');
 // Switching the source view invalidates its prior translation request.
 tabs[0].events.click();const staleView=requests[6];
 tabs[1].events.click();requests[7].respond({items:[],total:0});await tick();
 staleView.respond({items:rows.map(row=>complete(row,'STALE VIEW'))});await tick();
 assert.equal(list().textContent,'没有匹配的回答原文。');
 assert.equal(requests.length,8,'an empty page makes no translation request');
 // A different question cannot receive an earlier question's translations.
 sandbox.openReportSources('q1','Q1 体验','evidence');const staleQuestion=requests[8];
 sandbox.openReportSources('q2','Q2 原因','evidence');
 staleQuestion.respond({items:rows.map(row=>complete(row,'STALE QUESTION'))});await tick();
 assert(!list().textContent.includes('STALE QUESTION'));assert(list().textContent.includes('Different question'));
 const q2={response_id:'r4',text:'Different question',question_key:'q2',question:'Q2 原因'};
 requests[9].respond({items:[complete({...q2,text:'Changed original'},'WRONG SOURCE')]});await tick();
 assert(!list().textContent.includes('WRONG SOURCE'));assert(list().textContent.includes('原文版本不匹配'));
 assert(list().textContent.includes('Different question'),'original remains byte-for-byte unchanged');
 // History requests carry their selected version and ownership reference.
 sandbox.state.viewMode='history';sandbox.state.historyId='history-a';selectedId='history-a';version=4;
 sandbox.openReportSources('q1','Q1 体验','evidence');
 assert.deepEqual(JSON.parse(requests[10].options.body),{version:4,response_ids:['r1','r2'],history_id:'history-a'});
 version=5;
 requests[10].respond({items:rows.map(row=>complete(row,'STALE VERSION'))});await tick();
 assert(!list().textContent.includes('STALE VERSION'),'changing active version rejects a pending result even before another load');
 sandbox.openReportSources('q1','Q1 体验','evidence');
 requests[11].respond({detail:'本次翻译请求失败'},false);await tick();
 assert(list().textContent.includes('本次翻译请求失败'));
 walk(list()).find(e=>e.tagName==='BUTTON'&&e.textContent==='重试翻译').events.click();
 elements.get('report-sources-close').events.click();list().textContent='Closed drawer';
 requests[12].respond({items:[complete(rows[0],'AFTER CLOSE')]});await tick();
 assert.equal(list().textContent,'Closed drawer','closing the drawer invalidates pending work');
 const point={text:'仅第二条支持的观点',evidence_ids:['r2','unknown']};
 sandbox.openReportSources('q1','Q1 体验','evidence',point);
 assert.deepEqual(JSON.parse(requests[13].options.body).response_ids,['r2']);
 assert(!list().textContent.includes(rows[0].text),'point evidence must not include the whole question');
 assert(elements.get('report-sources-context').textContent.includes(point.text));
 assert.equal(tabs[1].hidden,true);
 requests[13].respond({items:[complete(rows[1],'观点对应中文')]});await tick();
 assert(list().textContent.includes('观点对应中文'));
 readingBody.scrollTop=999;elements.get('report-sources-close').events.click();assert.equal(readingBody.scrollTop,444,'closing evidence restores reading position');
 sandbox.openReportSources('q1','Q1 体验','all');
 assert.equal(tabs[1].hidden,false,'question source entry restores the all-source tab');
 console.log('On-demand bilingual sources, point scope, failed-only retry, frozen source matching and stale response cases passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT/'static/js/features/report-quick.js')], capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for executable frontend contracts')
    def test_availability_does_not_silently_change_user_mode(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const elements=new Map();let response={quick_enabled:true};
const state={sessionId:'a',reportMode:'quick',reportStyleSelection:{}};
const sandbox={state,URL,URLSearchParams,console,window:{location:{origin:'http://localhost'}},document:{querySelectorAll:()=>[]},
 $:id=>{if(!elements.has(id))elements.set(id,{addEventListener(){}});return elements.get(id);},
 fetch:async()=>({ok:true,json:async()=>response}),renderSurveyFocus(){}};
vm.createContext(sandbox);vm.runInContext(source,sandbox);
(async()=>{await sandbox.loadReportStyleOptions();assert.equal(state.reportStyleSelection.enabled,true);
response={quick_enabled:false};await sandbox.loadReportStyleOptions();assert.equal(state.reportStyleSelection.enabled,false);assert.equal(state.reportMode,'quick');assert.equal(sandbox.selectedReportStyle(),'quick');
assert.equal(sandbox.reportExportUrl('/api/export/word/s',3,'evidence'),'/api/export/word/s?version=3&scope=evidence');
console.log('Availability and version-scoped exports passed');})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT/'static/js/features/report-quick.js')], capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()


class InlineReferenceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_inline_references_require_matching_sections_and_findings(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
class Node {
 constructor(tag,text=''){this.tagName=tag.toUpperCase();this._text=text;this.children=[];this.dataset={};this.events={};}
 get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
 set textContent(v){this._text=v;this.children=[];}
 append(node){node.parent=this;this.children.push(node);}
 get nextElementSibling(){return this.parent?.children[this.parent.children.indexOf(this)+1];}
 before(...nodes){nodes.forEach(n=>n.parent=this.parent);this.parent.children.splice(this.parent.children.indexOf(this),0,...nodes);}
 after(node){node.parent=this.parent;this.parent.children.splice(this.parent.children.indexOf(this)+1,0,node);}
 querySelectorAll(){return this.children.filter(n=>n.dataset.quickInlineReference);}
 remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
 addEventListener(k,fn){this.events[k]=fn;}setAttribute(k,v){this[k]=v;}
}
const root=new Node('DIV'),rating=new Node('H2','评分'),heading=new Node('H2','原因'),ul=new Node('UL');
root.append(rating);root.append(heading);root.append(ul);
const values=['零散提及：其他零散建议：换色','反复提及：喜欢','部分提及 · 风险：难用','反复提及：简单'];
values.forEach(text=>ul.append(new Node('LI',text)));
const question={question:'原因',question_key:'2',source_order:2,status:'complete',
 findings:[{text:'其他零散建议：换色',frequency:'零散提及',risk:false,evidence_ids:['c']},{text:'喜欢',frequency:'反复出现',risk:false,evidence_ids:['a']},{text:'难用',frequency:'部分提及',risk:true,evidence_ids:['b']},{text:'简单',frequency:'反复提及',risk:false,evidence_ids:['d']}],
 sources:['a','b','c','d'].map(response_id=>({response_id,text:response_id}))};
let ctx={reportMode:'quick',quickSummary:{questions:[question],objective_stats:{sections:[{question:'评分',source_order:1}]}}};
const walk=node=>[node,...node.children.flatMap(walk)];
const content={querySelectorAll:selector=>walk(root).filter(n=>selector==='h2'?n.tagName==='H2':selector==='[data-quick-branch-note]'?n.dataset.quickBranchNote:n.dataset.quickInlineReference)};
const calls=[];const sandbox={document:{createElement:tag=>new Node(tag)},activeReportCtx:()=>ctx,activeVersionNumber:()=>ctx.version || 1,activeReportId:()=> 'test',openReportSources:(...args)=>calls.push(args)};
vm.createContext(sandbox);vm.runInContext(source.slice(source.indexOf('function quickEvidenceItems('),source.indexOf('function renderQuickReportNavigation(')),sandbox);
const items=()=>walk(root).filter(n=>n.tagName==='LI');
const button=item=>item.children.find(n=>n.tagName==='BUTTON');
sandbox.renderQuickInlineReferences(content,ctx);
assert.equal(heading.children.length,1);assert.equal(heading.dataset.quickTitle,'原因');assert.equal(rating.children.length,0);
assert.deepEqual(root.children.filter(n=>n.tagName==='P').map(n=>n.textContent),['反复提及：','部分提及：','零散提及：']);
assert.deepEqual(items().map(n=>n.textContent),['喜欢查看依据','简单查看依据','【风险】难用查看依据','换色查看依据']);
button(items()[2]).events.click();assert.equal(calls[0][3],question.findings[2]);
assert.deepEqual(JSON.parse(JSON.stringify(sandbox.quickEvidenceItems(question,calls[0][3]))).map(x=>x.response_id),['b']);
ctx.version=2;button(items()[0]).events.click();assert.equal(calls.length,1,'stale buttons cannot bind a newly selected version');ctx.version=1;
sandbox.renderQuickInlineReferences(content,ctx);assert.equal(items().length,4,'hydration must not duplicate groups');
assert.equal(root.children.filter(n=>n.tagName==='P').length,3);
const branch='原因【推定适用于「经历」选择「尝试」的玩家（进入该分支 13 人）；本题 12 条有效回答】';
heading._text=branch;heading.dataset.quickTitle=branch;question.question=branch;
sandbox.renderQuickQuestionConditions(content);
assert.equal(heading.dataset.quickTitle,'原因');assert.equal(heading.dataset.quickFullTitle,branch);
assert(root.children.some(n=>n.dataset.quickBranchNote&&n.textContent.includes('13 人')));
sandbox.renderQuickInlineReferences(content,ctx);sandbox.renderQuickQuestionConditions(content);
assert.equal(root.children.filter(n=>n.dataset.quickBranchNote).length,1,'hydration must not duplicate branch notes');
assert.equal(items().length,4);assert.equal(heading.dataset.quickTitle,'原因');
items()[0].children.find(n=>n.tagName==='SPAN').textContent='edited';sandbox.renderQuickInlineReferences(content,ctx);
assert(items().every(n=>!button(n)),'changed text must never inherit stale point references');
delete heading.dataset.quickFullTitle;heading._text='different title';sandbox.renderQuickInlineReferences(content,ctx);assert.equal(heading.children.length,0);
console.log('Section/finding alignment, mixed objective order, rehydration and fail-closed edited text passed');
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT/'static/js/features/report-quick.js')], capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
