"""Local CLI. Commands are explicit; errors return nonzero and never imply success."""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from .models import Contract, Budget, Candidate
from .providers import ScriptedProvider, OpenAIProvider, AnthropicProvider
from .service import RepoCode
from .util import RepoError, uid, atomic_write

def output(value):print(json.dumps(value,ensure_ascii=False,indent=2,default=str))
def load(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def auth_root(args):
    return args.auth_root or Path(os.getenv('REPO_CODE_AUTH_ROOT') or args.state_root.parent/'.repo-code-auth')

def provider(args):
    if args.provider=='scripted':
        return ScriptedProvider(load(args.script) if args.script else [])
    if not args.model and args.command!='models':raise RepoError('MODEL_REQUIRED','Pass --model using an ID available from your provider')
    if args.provider=='chatgpt-plan':
        from .chatgpt_provider import ChatGPTPlanProvider
        unsupported=[name for name in ('base_url','api_key_env','thinking','max_output_tokens','temperature') if getattr(args,name,None) is not None]
        if unsupported:raise RepoError('CHATGPT_UNSUPPORTED_OPTION','Unsupported with chatgpt-plan: '+', '.join('--'+x.replace('_','-') for x in unsupported))
        return ChatGPTPlanProvider(args.model or 'catalog-only',auth_root(args),reasoning_effort=args.reasoning_effort)
    cls=OpenAIProvider if args.provider=='openai' else AnthropicProvider
    if args.provider=='deepseek':cls=OpenAIProvider
    options={'model':args.model or 'catalog-only','base_url':args.base_url}
    if args.provider=='deepseek':options.update(base_url=args.base_url or 'https://api.deepseek.com',api_key_env=args.api_key_env or 'DEEPSEEK_API_KEY_REPO')
    if getattr(args,'reasoning_effort',None):
        if args.provider not in {'openai','deepseek'}:raise RepoError('REASONING_EFFORT_REQUIRES_OPENAI')
        options['reasoning_effort']=args.reasoning_effort
    if args.api_key_env:options['api_key_env']=args.api_key_env
    if args.provider in {'openai','deepseek'}:
        if getattr(args,'thinking',None):options['thinking']=args.thinking
        if getattr(args,'max_output_tokens',None):options['max_tokens']=args.max_output_tokens
        if getattr(args,'temperature',None) is not None:options['temperature']=args.temperature
    return cls(**options)

def parser():
    p=argparse.ArgumentParser(prog='repo-code',description='Standalone coding agent with evidence-bound delivery and controlled evolution')
    p.add_argument('--version',action='version',version='repo-code 1.5.1')
    p.add_argument('--state-root',type=Path,default=Path.home()/'.repo-code')
    p.add_argument('--sandbox',choices=['auto','native','docker','local'],default='auto')
    p.add_argument('--image',help='Preinstalled Docker image pinned as repo@sha256:...')
    p.add_argument('--provider',choices=['openai','deepseek','anthropic','chatgpt-plan','scripted'],default='scripted')
    p.add_argument('--auth-root',type=Path,help='Private ChatGPT OAuth store outside state and workspaces; or REPO_CODE_AUTH_ROOT')
    p.add_argument('--model');p.add_argument('--base-url');p.add_argument('--api-key-env');p.add_argument('--script',type=Path)
    p.add_argument('--reasoning-effort',choices=['none','minimal','low','medium','high','xhigh'])
    p.add_argument('--thinking',choices=['enabled','disabled'])
    p.add_argument('--max-output-tokens',type=int)
    p.add_argument('--temperature',type=float)
    p.add_argument('--memory-profile',type=Path,help='Explicit Memory v2 profile for new Runs; required unchanged on resume')
    p.add_argument('--mcp-config',type=Path,help='Operator-owned legacy stdio MCP profiles; native sandbox only')
    sub=p.add_subparsers(dest='command',required=True)
    auth=sub.add_parser('auth',help='ChatGPT plan authorization, independent of Codex login')
    auths=auth.add_subparsers(dest='auth_action',required=True)
    login=auths.add_parser('login');login.add_argument('--no-browser',action='store_true');login.add_argument('--timeout',type=float,default=180);login.add_argument('--port',type=int,default=0)
    auths.add_parser('status');auths.add_parser('logout')
    sub.add_parser('models',help='List models for the selected provider/account without inference')
    sub.add_parser('doctor');sub.add_parser('list');sub.add_parser('release-status')
    run=sub.add_parser('run');run.add_argument('--workspace',type=Path,required=True);run.add_argument('--goal',required=True);run.add_argument('--contract',type=Path)
    run.add_argument('--session-id');run.add_argument('--task-family')
    session=sub.add_parser('session',help='Host-owned advisory experience lifecycle; never publishes a Release')
    ss=session.add_subparsers(dest='session_action',required=True)
    sc=ss.add_parser('create');sc.add_argument('--scope',required=True);sc.add_argument('--ttl-seconds',type=float,default=86400)
    pc=ss.add_parser('create-project');pc.add_argument('--scope',required=True);pc.add_argument('--workspace',type=Path,required=True);pc.add_argument('--max-tasks',type=int,default=32)
    mc=ss.add_parser('create-collection');mc.add_argument('--scope',required=True);mc.add_argument('--name',required=True)
    ms=ss.add_parser('accumulation');ms.add_argument('session_id')
    sa=ss.add_parser('admit');sa.add_argument('session_id');sa.add_argument('--contract-ref',required=True);sa.add_argument('--candidate',required=True)
    sr=ss.add_parser('revoke');sr.add_argument('grant_id');sr.add_argument('--reason',required=True)
    for action in ['close','status']:
        sp=ss.add_parser(action);sp.add_argument('session_id')
    run.add_argument('--allow-write',action='store_true');run.add_argument('--allow-process',action='store_true');run.add_argument('--max-model-calls',type=int,default=30);run.add_argument('--wall-seconds',type=float,default=600);run.add_argument('--stream',action='store_true')
    for name in ['resume','report','recover','diff','processes']:
        q=sub.add_parser(name);q.add_argument('run_id')
    control=sub.add_parser('control');control.add_argument('run_id');control.add_argument('action',choices=['cancel','pause','steer']);control.add_argument('--text',default='');control.add_argument('--classification',choices=['execution_only','contract_change'])
    nxt=sub.add_parser('successor');nxt.add_argument('run_id');nxt.add_argument('--goal',required=True);nxt.add_argument('--contract',type=Path,required=True)
    appr=sub.add_parser('approve');appr.add_argument('operation_key');appr.add_argument('--deny',action='store_true')
    hc=sub.add_parser('confirm');hc.add_argument('run_id');hc.add_argument('check_id');hc.add_argument('--source-digest',required=True);hc.add_argument('--reviewer',required=True)
    op=sub.add_parser('operation');op.add_argument('operation_key');op.add_argument('--reconcile',action='store_true')
    exp=sub.add_parser('export');exp.add_argument('run_id');exp.add_argument('destination',type=Path)
    it=sub.add_parser('integrate');it.add_argument('run_id');it.add_argument('--confirm',action='store_true')
    co=sub.add_parser('contract');cos=co.add_subparsers(dest='contract_action',required=True)
    dr=cos.add_parser('draft');dr.add_argument('--goal',required=True);dr.add_argument('--output',type=Path,required=True);dr.add_argument('--interface',type=Path)
    fr=cos.add_parser('freeze');fr.add_argument('path',type=Path);fr.add_argument('--reviewer',required=True);fr.add_argument('--output',type=Path)
    va=cos.add_parser('validate');va.add_argument('path',type=Path)
    kn=sub.add_parser('knowledge');ks=kn.add_subparsers(dest='knowledge_action',required=True)
    kd=ks.add_parser('draft');kd.add_argument('--kind',choices=['memory','skill'],required=True);kd.add_argument('--title',required=True);kd.add_argument('--file',type=Path,required=True);kd.add_argument('--scope');kd.add_argument('--workspace',type=Path);kd.add_argument('--source',action='append',required=True)
    for name in ['approve','archive','revoke']:
        q=ks.add_parser(name);q.add_argument('id');q.add_argument('--reviewer',default='local-operator')
    ks.add_parser('list')
    for name in ['observe','problems','jobs'] :sub.add_parser(name)
    queue=sub.add_parser('enqueue');queue.add_argument('pattern');queue.add_argument('--dev',required=True)
    worker=sub.add_parser('worker-once');worker.add_argument('--max-candidates',type=int,default=3)
    skill=sub.add_parser('skill');ss=skill.add_subparsers(dest='skill_action',required=True)
    si=ss.add_parser('import');si.add_argument('path',type=Path);si.add_argument('--scope');si.add_argument('--workspace',type=Path)
    sx=ss.add_parser('export');sx.add_argument('id');sx.add_argument('path',type=Path)
    es=sub.add_parser('evolve');ess=es.add_subparsers(dest='evolve_action',required=True)
    dd=ess.add_parser('diagnose');dd.add_argument('pattern')
    se=ess.add_parser('search');se.add_argument('pattern');se.add_argument('--dev',required=True);se.add_argument('--max-candidates',type=int,default=3)
    sk=ess.add_parser('extract-skill');sk.add_argument('run_ids',nargs='+');sk.add_argument('--scope',default='project')
    ca=ess.add_parser('candidate');ca.add_argument('--changes',type=Path,required=True);ca.add_argument('--pattern',required=True);ca.add_argument('--hypothesis',required=True);ca.add_argument('--level',type=int,choices=[1,2],default=1);ca.add_argument('--source',action='append',default=[])
    for name in ['qualify','meta-qualify']:
        q=ess.add_parser(name);q.add_argument('candidate');q.add_argument('--dev',required=True);q.add_argument('--promotion',required=True);q.add_argument('--final',required=True)
    rt=ess.add_parser('retire');rt.add_argument('candidate');rt.add_argument('--reviewer',required=True)
    au=ess.add_parser('audit');au.add_argument('candidate')
    ap=ess.add_parser('approve');ap.add_argument('candidate');ap.add_argument('--reviewer',required=True)
    ac=ess.add_parser('activate');ac.add_argument('candidate');ac.add_argument('--expected-revision',type=int)
    rb=ess.add_parser('rollback');rb.add_argument('release');rb.add_argument('--expected',required=True);rb.add_argument('--reviewer',required=True)
    me=ess.add_parser('enable-meta');me.add_argument('--reviewer',required=True);me.add_argument('--acknowledge-research-risk',action='store_true')
    suite=sub.add_parser('suite-register');suite.add_argument('path',type=Path);suite.add_argument('--role',choices=['dev','promotion','final','meta_dev','meta_promotion','meta_final'],required=True);suite.add_argument('--name',required=True);suite.add_argument('--family')
    serv=sub.add_parser('serve');serv.add_argument('--port',type=int,default=0);serv.add_argument('--run-id',required=True)
    de=sub.add_parser('demo');de.add_argument('--output',type=Path,required=True)
    ma=sub.add_parser('maintenance');ma.add_argument('action',choices=['inspect','backup','restore','prune']);ma.add_argument('--destination',type=Path);ma.add_argument('--apply',action='store_true');ma.add_argument('--source',type=Path)
    return p

async def dispatch(args):
    if args.command=='auth':
        from .chatgpt_auth import ChatGPTAuth
        auth=ChatGPTAuth(auth_root(args))
        if args.auth_action=='login':result=await asyncio.to_thread(auth.login,timeout=args.timeout,port=args.port,open_browser=not args.no_browser,notify=lambda text:print(text,file=sys.stderr,flush=True))
        elif args.auth_action=='logout':result=await asyncio.to_thread(auth.logout)
        else:result=auth.status()
        output(result);return 0
    if args.command=='models':
        model=provider(args)
        if hasattr(model,'list_models'):result=await model.list_models()
        elif hasattr(model,'client'):result=(await model.client.models.list()).model_dump(mode='json')
        else:raise RepoError('MODEL_CATALOG_UNAVAILABLE')
        output(result);return 0
    from .acceptance import validate_contract
    from .bootstrap import draft_contract,review_and_freeze
    from .delivery import Delivery
    from .observation import Observer, EvolutionQueue
    from .governance import HoldoutVault
    from .evaluation import SuiteEvaluator
    from .evolution import EvolutionEngine
    if args.command=='contract' and args.contract_action in {'freeze','validate'}:
        if args.contract_action=='freeze':output(review_and_freeze(args.path,args.reviewer,args.output).model_dump(mode='json'))
        else:validate_contract(Contract.model_validate(load(args.path)));output({'valid':True})
        return 0
    if args.command=='maintenance' and args.action=='restore':
        from .maintenance import restore_backup
        if not args.source:raise RepoError('BACKUP_SOURCE_REQUIRED')
        output(restore_backup(args.source,args.state_root));return 0
    if args.command=='demo':
        from .demo import run_demo
        output(await run_demo(args.state_root,args.output,args.sandbox,args.image));return 0
    model=provider(args)
    if args.command=='contract':
        draft=await draft_contract(model,args.goal,load(args.interface) if args.interface else None)
        atomic_write(args.output,json.dumps(draft.model_dump(mode='json'),ensure_ascii=False,indent=2).encode());output({'draft':str(args.output),'status':'REQUIRES_OPERATOR_REVIEW'});return 0
    app=RepoCode(args.state_root,model,args.sandbox,args.image,on_delta=(lambda text:print(text,end='',file=sys.stderr,flush=True)) if getattr(args,'stream',False) else None,
                 memory_profile=load(args.memory_profile) if args.memory_profile else None)
    mcp_servers=[]
    try:
        cmd=args.command
        if cmd=='session':
            sessions=app.session_experiences();action=args.session_action
            if action=='create':output({'session_id':sessions.create(args.scope,ttl_seconds=args.ttl_seconds)})
            elif action=='create-project':output({'session_id':sessions.create_project(args.scope,args.workspace,max_tasks=args.max_tasks),'published':False})
            elif action=='create-collection':output({'session_id':sessions.create_collection(args.scope,args.name),'published':False})
            elif action=='accumulation':output(sessions.accumulation(args.session_id))
            elif action=='admit':output({'grant_id':sessions.admit(args.session_id,args.contract_ref,args.candidate),'published':False})
            elif action=='revoke':sessions.revoke(args.grant_id,args.reason);output({'revoked':args.grant_id})
            elif action=='close':sessions.close(args.session_id);output({'closed':args.session_id})
            else:output(app.store.get('experience_session',args.session_id))
            return 0
        if cmd=='doctor':
            import tempfile
            app.sandbox.prepare()
            with tempfile.TemporaryDirectory(dir=app.store.root) as td:
                script="import os,socket,json; denied=False; s=None\ntry:\n s=socket.socket();s.settimeout(0.5);s.connect(('1.1.1.1',443))\nexcept OSError:denied=True\nfinally:\n if s:s.close()\nprint(json.dumps({'uid':os.getuid(),'network_denied':denied,'network_probe':'outbound_tcp_connect'}))"
                probe=await app.sandbox.run('doctor',Path(td),['python','-c',script],timeout=10,readonly=True)
            try:observation=json.loads(probe['stdout'])
            except ValueError:observation={}
            valid=probe['exit_code']==0 and observation.get('uid',0)!=0 and observation.get('network_denied') is True
            output({'version':'1.5.1','sandbox':app.sandbox.environment(),'isolated':app.sandbox.isolated,'probe_passed':bool(valid),'observation':observation,'state_root':str(app.store.root),'models':{n:__import__('importlib.util',fromlist=['find_spec']).find_spec(n) is not None for n in ('openai','anthropic')},'live_model_tested':False})
            return 0 if valid else 2
        if cmd=='run':
            contract=Contract.model_validate(load(args.contract)) if args.contract else None
            r=app.new_task(args.goal,args.workspace,contract,Budget(model_calls=args.max_model_calls,wall_seconds=args.wall_seconds),args.allow_write,args.allow_process,session_id=args.session_id,task_family=args.task_family)
            if args.mcp_config:
                from .mcp import MCPClient
                for cfg in load(args.mcp_config)['servers']:
                    server=MCPClient(app.sandbox,Path(cfg['root']),cfg['command'],r['id']);await server.start()
                    app.broker.mcp[cfg['name']]=server;mcp_servers.append(server)
            result=await app.execute(r['id'],expose_control=True);output(result);return 0 if result['state']=='SUCCEEDED' else 2
        if cmd=='resume':app.runtime.resume(args.run_id);result=await app.execute(args.run_id,expose_control=True);output(result);return 0 if result['state']=='SUCCEEDED' else 2
        if cmd=='list':
            with contextlib.closing(app.store.connect()) as conn:output([dict(r) for r in conn.execute('SELECT id,task_id,state,revision FROM runs ORDER BY rowid DESC')]);return 0
        if cmd=='confirm':
            from .util import digest
            run=app.runtime.run(args.run_id);contract=app.runtime.contract(run)
            if not contract or not any(c.id==args.check_id and c.kind=='human' for c in contract.checks):raise RepoError('NOT_HUMAN_CHECK')
            if app.runtime.workspace(run).snapshot()!=args.source_digest:raise RepoError('STALE_SOURCE')
            app.store.put('human_acceptance',digest([args.run_id,args.check_id,args.source_digest]),{'approved':True,'reviewer':args.reviewer,'contract':contract.digest})
            output({'confirmed':args.check_id,'bound_source':args.source_digest});return 0
        if cmd=='report':output(app.agent.report(args.run_id));return 0
        if cmd=='recover':output(app.runtime.recover(args.run_id));return 0
        if cmd=='diff':
            r=app.runtime.run(args.run_id);output(app.runtime.workspace(r).diff(r['data']['manifest']['workspace_baseline_digest']));return 0
        if cmd=='processes':output([x for x in app.store.values('lease') if x['run_id']==args.run_id]);return 0
        if cmd=='control':
            from .ipc import BrokerClient
            eid=uid('control-');endpoint=app.store.get('executor_endpoint',args.run_id)
            client=None
            if endpoint and endpoint.get('state')=='ACTIVE':
                client=BrokerClient(tuple(endpoint['address']),endpoint['token'],args.run_id)
            if client:
                try:
                    result=await asyncio.to_thread(client.request,'CONTROL',{'event_id':eid,'kind':args.action.upper(),'text':args.text})
                except (OSError,RepoError):
                    # Persist a stop even if the owning Host died. Unknown resources stay blocked.
                    client=None;result=app.runtime.admit(args.run_id,eid,args.action.upper(),args.text)
            else:result=app.runtime.admit(args.run_id,eid,args.action.upper(),args.text)
            classification='pause' if args.action=='pause' else args.classification
            if classification and result['status'].startswith('PENDING'):
                if client:result=await asyncio.to_thread(client.request,'RESOLVE_CONTROL',{'event_id':eid,'classification':classification})
                else:result=app.runtime.finish_control(eid,classification)
            output(result);return 0
        if cmd=='successor':output(app.runtime.successor(args.run_id,args.goal,Contract.model_validate(load(args.contract))));return 0
        if cmd=='approve':app.broker.approve(args.operation_key,not args.deny);output(app.broker.get(args.operation_key));return 0
        if cmd=='operation':output(app.broker.reconcile(args.operation_key) if args.reconcile else app.broker.get(args.operation_key));return 0
        if cmd=='export':output(Delivery(app).export(args.run_id,args.destination));return 0
        if cmd=='integrate':output(Delivery(app).integrate(args.run_id,args.confirm));return 0
        if cmd=='knowledge':
            k=app.runtime.knowledge;a=args.knowledge_action
            if a=='draft':
                from .knowledge import project_scope
                output(k.draft(args.kind,args.title,args.file.read_text(),args.scope or project_scope(args.workspace or Path.cwd()),args.source))
            elif a=='approve':output(k.activate(args.id,args.reviewer))
            elif a=='archive':k.archive(args.id,args.reviewer);output({'archived':args.id})
            elif a=='revoke':k.revoke(args.id);output({'revoked':args.id,'historical_blobs_retained_for_audit':True})
            else:output(app.store.values('knowledge'))
            return 0
        if cmd=='skill':
            from .skills import import_skill,export_skill
            from .knowledge import project_scope
            result=import_skill(app.runtime.knowledge,args.path,args.scope or project_scope(args.workspace or Path.cwd())) if args.skill_action=='import' else export_skill(app.runtime.knowledge,args.id,args.path)
            output(result);return 0
        if cmd=='observe':output(Observer(app.store).consume());return 0
        if cmd=='enqueue':
            triage=Observer(app.store).triage(args.pattern)
            if triage['decision']!='SCHEDULE_TARGETED_EVOLUTION':raise RepoError('MORE_EVIDENCE_REQUIRED')
            job=EvolutionQueue(app.store).enqueue(args.pattern)
            with app.store.tx() as conn:conn.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps({'dev':args.dev}),job))
            output({'queued':job});return 0
        if cmd=='worker-once':
            queue=EvolutionQueue(app.store);job=queue.claim(lease_seconds=2100)
            if not job:output({'status':'IDLE_OR_DEFERRED'});return 0
            try:
                options=json.loads(job['data']);vault=HoldoutVault(app.store)
                ev=SuiteEvaluator(app,vault,lambda public:provider(args))
                result=await EvolutionEngine(app,model,ev).search(job['problem'],options['dev'],args.max_candidates)
                queue.finish(job['id'],'DONE' if result.get('selected') else 'COOLDOWN',owner_token=job['owner_token'])
                output({'job':job['id'],'result':result});return 0
            except BaseException:
                queue.finish(job['id'],'FAILED',owner_token=job['owner_token']);raise
        if cmd=='problems':output(app.store.values('pattern'));return 0
        if cmd=='jobs':
            with contextlib.closing(app.store.connect()) as conn:output([dict(x) for x in conn.execute('SELECT * FROM jobs')]);return 0
        if cmd=='release-status':output(app.releases.active());return 0
        vault=HoldoutVault(app.store)
        if cmd=='suite-register':output({'cohort_digest':vault.register(args.name,args.role,load(args.path),args.family)});return 0
        if cmd=='evolve':
            # Construct a fresh model client per trial; no oracle in this closure.
            ev=SuiteEvaluator(app,vault,lambda public:provider(args));eng=EvolutionEngine(app,model,ev);a=args.evolve_action
            if a=='diagnose':result=await eng.diagnose(args.pattern)
            elif a=='search':result=await eng.search(args.pattern,args.dev,args.max_candidates)
            elif a=='extract-skill':result=await eng.extract_skill(args.run_ids,args.scope)
            elif a=='candidate':result=app.releases.candidate(args.pattern,load(args.changes),args.hypothesis,args.source,level=args.level).model_dump()
            elif a=='retire':result=app.releases.retire(args.candidate,args.reviewer)
            elif a=='audit':
                candidate=Candidate.model_validate(app.store.get('candidate',args.candidate))
                result=app.releases.governor.audit(app.releases.components(candidate.base_release_digest),app.releases.components(candidate.candidate_release_digest))
            elif a=='qualify':result=await ev.qualify(args.candidate,args.dev,args.promotion,args.final)
            elif a=='meta-qualify':
                from .meta import MetaEvaluator
                result=await MetaEvaluator(app,vault,lambda public,purpose:provider(args)).qualify(args.candidate,args.dev,args.promotion,args.final)
            elif a=='approve':app.releases.approve(args.candidate,args.reviewer);result={'approved':args.candidate}
            elif a=='activate':result=app.releases.activate(args.candidate,args.expected_revision)
            elif a=='rollback':result=app.releases.rollback(args.release,args.expected,args.reviewer)
            else:
                if not args.acknowledge_research_risk:raise RepoError('META_RISK_ACK_REQUIRED')
                app.store.put('operator_config','meta',{'enabled':True,'reviewer':args.reviewer});result={'meta_enabled':True,'gates_still_required':True}
            output(result);return 2 if result.get('status')=='REJECT' else 0
        if cmd=='serve':
            from .ipc import BrokerServer
            server=BrokerServer(app.broker,port=args.port);server.start()
            output({'address':server.address,'worker_token':server.tokens.issue(args.run_id),'note':'Keep token private. Only Host-registered calls may be submitted.'})
            try:
                while True:await asyncio.sleep(1)
            finally:server.close()
        if cmd=='maintenance':
            from .maintenance import Maintenance
            m=Maintenance(app.store)
            if args.action=='inspect':result=m.inspect()
            elif args.action=='backup':
                if not args.destination:raise RepoError('BACKUP_DESTINATION_REQUIRED')
                result=m.backup(args.destination)
            else:result=m.prune(apply=args.apply,backup=args.destination)
            output(result);return 0
    finally:
        app.close()
        for server in mcp_servers:
            with contextlib.suppress(Exception):await server.close()
    return 0

def main():
    args=parser().parse_args()
    try:code=asyncio.run(dispatch(args))
    except KeyboardInterrupt:code=130
    except (RepoError,ValueError,OSError) as exc:
        output({'error':getattr(exc,'code',type(exc).__name__),'message':str(exc)});code=1
    raise SystemExit(code)

if __name__=='__main__':main()
