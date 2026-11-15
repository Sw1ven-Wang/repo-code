"""Harbor external agent using the original repo-code loop and trusted Broker."""
import asyncio
import json
import os
import shlex
import time
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.models.agent.context import AgentContext
from repo_code.acceptance import Grader
from repo_code.agent import CodingAgent
from repo_code.broker import Broker
from repo_code.governance import Releases
from repo_code.models import Budget
from repo_code.providers import OpenAIProvider
from repo_code.runtime import Runtime
from repo_code.service import RepoCode
from repo_code.storage import Store
from repo_code.util import RepoError,ProviderNotDispatched,atomic_write,digest

from .bridge import HarborSandbox,host_path


class BudgetedDeepSeek(OpenAIProvider):
    def __init__(self,token_limit,log_path,**kwargs):
        super().__init__(**kwargs)
        self.token_limit,self.log_path=token_limit,Path(log_path)
        self.records=[];self.used_tokens=0

    async def complete(self,system,messages,tools,on_delta=None):
        # Reserve conservatively before calling, then settle with provider usage.
        upper_input=len(json.dumps([system,messages,tools],ensure_ascii=False).encode())+4096
        if self.used_tokens+upper_input+self.max_tokens>self.token_limit:
            raise ProviderNotDispatched('TOTAL_TOKEN_BUDGET_EXHAUSTED','Remaining tokens cannot cover the conservative next-request reservation')
        record={'call':len(self.records)+1,'started':time.time(),'status':'STARTED'}
        self.records.append(record);self._save()
        settled=False
        try:
            reply=await super().complete(system,messages,tools,on_delta)
            if not reply.usage or reply.usage.get('total_tokens') is None:
                raise RepoError('PROVIDER_USAGE_UNAVAILABLE')
            self.used_tokens+=reply.usage['total_tokens']
            settled=True
            record.update(status='COMPLETED',usage=reply.usage)
            if self.used_tokens>self.token_limit:raise RepoError('TOTAL_TOKEN_BUDGET_EXCEEDED')
            return reply
        except BaseException as exc:
            if not settled and getattr(self,'last_usage',{}).get('total_tokens') is not None:
                self.used_tokens+=self.last_usage['total_tokens'];record['usage']=self.last_usage
            record.update(status='FAILED',error_code=getattr(exc,'code',type(exc).__name__))
            raise
        finally:
            record['finished']=time.time();self._save()

    def _save(self):
        self.log_path.parent.mkdir(parents=True,exist_ok=True)
        atomic_write(self.log_path,json.dumps({'used_tokens':self.used_tokens,'limit':self.token_limit,
            'calls':self.records},ensure_ascii=False,indent=2).encode())


class HarborRepoCode(RepoCode):
    def __init__(self,state_root,provider,environment,root,image_identity,process_cwd=None):
        self.provider=provider;self.store=Store(host_path(state_root))
        self.sandbox=HarborSandbox(self.store,environment,root,image_identity,process_cwd)
        self.runtime=Runtime(self.store,self.sandbox)
        self.releases=Releases(self.store,self.sandbox);self.releases.bootstrap()
        self.broker=Broker(self.store,self.runtime,self.sandbox,provider)
        self.grader=Grader(self.store,self.sandbox)
        self.agent=CodingAgent(self.store,self.runtime,self.broker,self.grader,self.releases,provider)


class RepoCodeHarborAgent(BaseAgent):
    def __init__(self,*args,max_calls=30,max_tools=150,wall_seconds=3000,
                 token_limit=1200000,output_tokens=8192,thinking='enabled',
                 provider_base_url='https://api.deepseek.com',benchmark='swe-pro-v2',task_dir=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.max_calls,self.max_tools=int(max_calls),int(max_tools)
        self.wall_seconds,self.token_limit=float(wall_seconds),int(token_limit)
        self.output_tokens,self.thinking=int(output_tokens),thinking
        self.provider_base_url=provider_base_url
        if benchmark not in {'swe-pro-v2','e2e-swe','terminal-bench-2'}:raise ValueError('Unsupported benchmark')
        self.benchmark=benchmark;self.task_dir=Path(task_dir) if task_dir is not None else None

    @staticmethod
    def name():return 'repo-code'
    def version(self):return '1.5.1-harbor-1'

    async def setup(self,environment):
        # Credentials never go into extra_env, uploads or environment.exec.
        if not os.environ.get('DEEPSEEK_API_KEY_REPO'):raise RepoError('API_KEY_MISSING','DEEPSEEK_API_KEY_REPO')
        self.logs_dir.mkdir(parents=True,exist_ok=True)

    async def run(self,instruction,environment,context:AgentContext):
        from openai import AsyncOpenAI
        model=(self.model_name or 'deepseek-flash').split('/')[-1]
        if model!='deepseek-flash':raise RepoError('UNFROZEN_MODEL_CONFIGURATION')
        script='''import json,os,subprocess,sys
from pathlib import Path
kind=sys.argv[1];cwd=os.getcwd()
hidden=any(Path(p).exists() for p in ['/tests/test.sh','/solution/gold_patch.diff'])
credentials=[k for k in os.environ if k in ['DEEPSEEK_API_KEY_REPO','OPENAI_API_KEY','ANTHROPIC_API_KEY']]
if hidden or credentials:
    print(json.dumps({'root':None,'hidden_present':hidden,'credential_names_present':credentials}));sys.exit(0)
if kind=='swe-pro-v2':
    root=next((str(p) for p in [Path('/app'),Path('/testbed')] if (p/'.git').exists()),None)
else:
    root='/app' if kind=='e2e-swe' else '/repo-code-workspace'
    Path(root).mkdir(parents=True,exist_ok=True)
    if not (Path(root)/'.git').exists():
        subprocess.run(['git','init'],cwd=root,check=True,capture_output=True)
        subprocess.run(['git','-c','user.name=repo-code-benchmark','-c','user.email=benchmark@localhost',
            'commit','--allow-empty','-m','Empty benchmark workspace baseline'],cwd=root,check=True,capture_output=True)
print(json.dumps({'root':root,'cwd':cwd,'hidden_present':hidden,'credential_names_present':credentials}))
'''
        probe=await environment.exec('python3 -I -c '+shlex.quote(script)+' '+shlex.quote(self.benchmark),timeout_sec=30)
        info=json.loads(probe.stdout or '{}')
        if probe.return_code or not info.get('root'):raise RepoError('HARBOR_REPOSITORY_NOT_FOUND')
        if info['hidden_present'] or info['credential_names_present']:raise RepoError('HARBOR_INPUT_ISOLATION_FAILED')
        root=info['root']
        before=await environment.exec('git rev-parse HEAD',cwd=root,timeout_sec=30)
        if before.return_code:raise RepoError('HARBOR_BASE_COMMIT_UNAVAILABLE')
        base=before.stdout.strip()
        if not len(base)==40 or any(c not in '0123456789abcdef' for c in base):raise RepoError('INVALID_BASE_COMMIT')
        image=getattr(environment,'_image_identity',None) or environment.task_env_config.docker_image
        state=host_path(self.logs_dir.parent/'repo-code-host-state')
        if state.exists():raise RepoError('HARBOR_FRESH_STATE_REQUIRED')
        client=AsyncOpenAI(api_key=os.environ['DEEPSEEK_API_KEY_REPO'],base_url=self.provider_base_url,
            timeout=180,max_retries=0)
        provider=BudgetedDeepSeek(self.token_limit,self.logs_dir/'usage.json',model=model,
            base_url=self.provider_base_url,client=client,thinking=self.thinking,
            reasoning_effort='high' if self.thinking=='enabled' else None,max_tokens=self.output_tokens)
        process_cwd=info.get('cwd',root) if self.benchmark=='terminal-bench-2' else root
        app=HarborRepoCode(state,provider,environment,root,image,process_cwd)
        run=None;result=None;failure=None;capture=None
        try:
            source=state/'public-source'
            files,export_meta=await app.sandbox.export(source);app.sandbox.synced=files
            self.logs_dir.joinpath('manifest.json').write_text(json.dumps({
                'base_commit':base,'image':image,'source_snapshot':digest({'files':files}),
                'benchmark':self.benchmark,'official_task_id':self.task_dir.name if self.task_dir else self.logs_dir.parent.name,
                'public_instruction_digest':digest(instruction),'input_probe':info,
                'omitted_symlinks':export_meta['omitted_symlinks'],
                'provider':{'model':model,'base_url':self.provider_base_url,'thinking':self.thinking,
                    'reasoning_effort':'high' if self.thinking=='enabled' else None,'temperature':'omitted',
                    'max_tokens':self.output_tokens,'sdk_max_retries':0},
                'budget':{'model_calls':self.max_calls,'tool_calls':self.max_tools,'wall_seconds':self.wall_seconds,'total_tokens':self.token_limit},
                'official_score':None,'internal_contract':None},indent=2),encoding='utf-8')
            goal=instruction+'\n\nFile tools use paths relative to '+root+'; do not pass absolute paths to read/edit/create/search. process.run commands start in '+process_cwd+'. Use bounded commands and the installed dependencies. Delegation, MCP and asynchronous process.start are unavailable. Hidden verifier files are unavailable. Finish when the implementation is ready; only the official verifier determines benchmark success.'
            run=app.new_task(goal,source,budget=Budget(model_calls=self.max_calls,tool_calls=self.max_tools,
                wall_seconds=self.wall_seconds,child_calls=0),authorize_write=True,authorize_process=True)
            app.broker.authorize_run(run['id'],True,True,False)
            result=await app.execute(run['id'])
            if result['state'] in {'NEEDS_RECONCILIATION','CONTROL_PENDING','CANCELLED','WAITING_APPROVAL','BLOCKED'}:
                raise RepoError('HARBOR_AGENT_'+result['state'],result.get('reason') or '')
            await app.sandbox.sync(Path(result['workspace']))
        except BaseException as exc:
            failure=exc
            raise
        finally:
            # Capture before Harbor uploads/runs its official verifier. It will
            # be replayed by official patch_replay in a new, pristine image.
            try:
                if not isinstance(failure,asyncio.CancelledError):
                    capture=await environment.exec('git add -N -- . && git --no-pager diff --binary '+shlex.quote(base),cwd=root,timeout_sec=60)
                    if capture.return_code:
                        self.logs_dir.joinpath('patch-capture-error.txt').write_text(capture.stderr or '',encoding='utf-8')
                    else:self.logs_dir.joinpath('model.patch').write_text(capture.stdout or '',encoding='utf-8',newline='')
            except Exception as exc:
                self.logs_dir.joinpath('patch-capture-error.txt').write_text(type(exc).__name__,encoding='utf-8')
            if run:
                self.logs_dir.joinpath('repo-code-result.json').write_text(json.dumps(result or app.agent.report(run['id']),ensure_ascii=False,indent=2),encoding='utf-8')
                self.logs_dir.joinpath('trajectory.json').write_text(json.dumps(app.store.messages(run['id']),ensure_ascii=False,indent=2),encoding='utf-8')
            usages=[r.get('usage',{}) for r in provider.records]
            context.n_input_tokens=sum(u.get('prompt_tokens',0) for u in usages)
            context.n_output_tokens=sum(u.get('completion_tokens',0) for u in usages)
            context.n_cache_tokens=sum(u.get('prompt_cache_hit_tokens',0) for u in usages)
            context.metadata={'repo_code_run_id':run['id'] if run else None,'official_score':'PENDING_OFFICIAL_VERIFIER',
                'internal_state':result['state'] if result else None,'base_commit':base,'image':image}
            await client.close()
        if capture is None or capture.return_code:raise RepoError('HARBOR_PATCH_CAPTURE_FAILED')
