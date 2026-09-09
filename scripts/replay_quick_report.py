"""Isolated synthetic writer replay and local platform preview. Never use runtime data/.

python scripts/replay_quick_report.py --output <outside-repository-directory>
Add --real for a real model call; --env-file reads only LLM_* process configuration.
Add --serve --port <port> to inspect the result in the actual platform history UI.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


async def replay(fixture, output: Path, real: bool):
    from app.core.config import DEFAULT_QUICK_WRITER_REQUIREMENTS, LLM_QUICK_REPORT_STAGE_TIMEOUT_SECONDS
    from app.services.report_quick_mode import build_quick_query, parse_quick_draft, render_quick_report
    from app.services.survey_service import _direct_writer_round, _ReportLLMUsageTracker
    catalog = fixture['evidence_catalog']
    started = time.monotonic()
    tracker = _ReportLLMUsageTracker()
    diagnostics = {'fixture_provenance':fixture['provenance'], 'real_model':real,
                   'scope':'writer only; precomputed synthetic evidence, upstream analysis not replayed',
                   'stage_budget_seconds':LLM_QUICK_REPORT_STAGE_TIMEOUT_SECONDS,
                   'planned_logical_calls':1 if real else 0, 'max_logical_calls':2 if real else 0,
                   'logical_calls':0, 'status':'running'}
    draft = deepcopy(fixture['sample_draft'])
    messages = [{'role':'system','content':DEFAULT_QUICK_WRITER_REQUIREMENTS}]
    query = build_quick_query(catalog,context={'problem':'确定核心体验的改进优先级；保留正向体验并核实低频风险'},focus={},instruction='这是合成材料的质量验收，不能把构造频次描述为实际游戏数据。')
    diagnostics['input_char_count']=len(query)
    try:
        if real:
            async with asyncio.timeout(LLM_QUICK_REPORT_STAGE_TIMEOUT_SECONDS):
                for attempt in range(2):
                    diagnostics['logical_calls']+=1
                    answer,model=await _direct_writer_round(messages,query,on_attempt_event=tracker.callback('writing'))
                    diagnostics['model']=model
                    diagnostics['output_char_count']=len(answer)
                    (output/f'writer-attempt-{attempt+1}.txt').write_text(answer,encoding='utf-8')
                    try:
                        draft=parse_quick_draft(answer,catalog)
                        break
                    except ValueError as error:
                        if attempt: raise
                        query=f'上轮未通过校验：{error}。请按原契约重新返回完整JSON。'
        else:
            draft=parse_quick_draft(json.dumps(draft,ensure_ascii=False),catalog)
        markdown,metrics=render_quick_report(draft,catalog)
        diagnostics.update(metrics)
        referenced=set(metrics['body_referenced_ids'])
        diagnostics['critical_evidence_referenced']=all(ref in referenced for ref in fixture['critical_evidence_ids'])
        diagnostics['segment_evidence_referenced']=all(ref in referenced for ref in fixture['segment_evidence_ids'])
        diagnostics['body_char_count']=len(markdown.split('## 发现与证据附录')[0])
        diagnostics['report_char_count']=len(markdown)
        diagnostics.update(status='completed',stop_reason='contract_validated')
        (output/'quick-report.md').write_text(markdown,encoding='utf-8')
        (output/'draft.json').write_text(json.dumps(draft,ensure_ascii=False,indent=2),encoding='utf-8')
        return markdown,diagnostics
    except BaseException as error:
        diagnostics.update(status='failed',stop_reason='timeout' if isinstance(error,TimeoutError) else type(error).__name__)
        raise
    finally:
        tracker.finalize_open_attempts()
        diagnostics['report_llm_usage']=tracker.snapshot()
        diagnostics['elapsed_seconds']=round(time.monotonic()-started,3)
        (output/'replay-result.json').write_text(json.dumps(diagnostics,ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--real',action='store_true')
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--port',type=int,default=8767)
    args=parser.parse_args()
    output=args.output.resolve()
    if output==ROOT or output.is_relative_to(ROOT) or 'data' in output.parts:
        parser.error('Output must be an isolated directory outside the repository and any data/ directory')
    env_file=args.env_file.resolve() if args.env_file else None
    output.mkdir(parents=True,exist_ok=True)
    # sessions.py uses cwd-relative data/sessions; isolate cwd as well as configured storage.
    os.chdir(output)
    os.environ['DATA_DIR']=str(output/'data')
    os.environ['RESEARCH_ASSET_STORAGE_DIR']=str(output/'assets')
    os.environ['REPORT_QUICK_MODE_ENABLED']='true'
    os.environ['FEISHU_LOGIN_REQUIRED']='false'
    os.environ['GOOGLE_FORMS_QUALITATIVE_ENABLED']='false'
    if args.real and env_file:
        from dotenv import dotenv_values
        for key,value in dotenv_values(env_file).items():
            if key.startswith('LLM_') and value is not None:
                os.environ[key]=value
    sys.path.insert(0,str(ROOT))
    fixture=json.loads((ROOT/'tests/fixtures/report_pipeline/quick_mode_cases.json').read_text(encoding='utf-8'))
    try:
        markdown,diagnostics=asyncio.run(replay(fixture,output,args.real))
    except Exception as error:
        print(json.dumps({'status':'failed','error_type':type(error).__name__,'diagnostics':str(output/'replay-result.json')}))
        return 1
    print(json.dumps({key:diagnostics[key] for key in ('status','real_model','logical_calls','elapsed_seconds','body_char_count','report_char_count','catalog_count','critical_evidence_referenced','segment_evidence_referenced')},ensure_ascii=False),flush=True)
    if args.serve:
        from app.services.report_versions import append_report_version
        from app.storage.history import mutate_history
        entry={'id':str(uuid.uuid4()),'filename':'快速模式验收示例（合成材料）.xlsx','created_at':datetime.now().isoformat(timespec='seconds'),'mode':'','row_count':fixture['respondent_count'],'plan':{'columns':[],'parts':[]}}
        append_report_version(entry,{'report_md':markdown,'report_style':'quick','quick_report_diagnostics':diagnostics,'qa_context_md':markdown,'qa_messages':[]})
        mutate_history(lambda history: history.insert(0,entry))
        (output/'preview.json').write_text(json.dumps({'history_id':entry['id'],'url':f'http://127.0.0.1:{args.port}/'},ensure_ascii=False),encoding='utf-8')
        print(f"Preview ready: http://127.0.0.1:{args.port}/ (history: {entry['id']})",flush=True)
        import uvicorn
        from app.main import app
        uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
