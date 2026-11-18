"""Explicit Host-only benchmark admissions. Never registered as model tools.

The ordinary Task cap stays 1000. A signed, bounded conversation allocation
can raise it for a frozen evaluator and records both stage and session caps.
"""
from __future__ import annotations
from pathlib import Path
from .util import RepoError, digest, execution_lock, Faults
from .workspace import Workspace, Mutator, file_lock, relative_path


def stage_payload(stage):
    return {k:v for k,v in stage.items() if k not in {'signature','status','source_after'}}


def active_stage(runtime,run,c=None):
    stage=runtime.store.get('benchmark_active_stage',run['id'],c)
    if (not stage or stage['body']['run_id']!=run['id']
            or not runtime.store.verify(stage_payload(stage),stage.get('signature',''))):
        raise RepoError('BENCHMARK_STAGE_SIGNATURE_INVALID')
    return stage


def allocation(runtime,run,c=None):
    value=run['data'].get('benchmark_allocation')
    if not value:return None
    body=value.get('body',{})
    if not runtime.store.verify(body,value.get('signature','')) or body.get('run_id')!=run['id'] or body.get('task_id')!=run['task_id']:
        raise RepoError('BENCHMARK_ALLOCATION_INVALID')
    context=body.get('context_tokens')
    if context is not None and (type(context) is not int or not 6000<=context<=262144):
        raise RepoError('BENCHMARK_ALLOCATION_INVALID')
    if runtime.store.get('revocation',digest(body),c):raise RepoError('BENCHMARK_ALLOCATION_REVOKED')
    return body


def admit_budget(runtime,run_id,*,experiment_digest,session_model_calls,session_tool_calls,
                 stage_model_calls,stage_tool_calls,wall_seconds,stage_seconds,context_tokens=None):
    values={'session_model_calls':session_model_calls,'session_tool_calls':session_tool_calls,
            'stage_model_calls':stage_model_calls,'stage_tool_calls':stage_tool_calls,
            'wall_seconds':wall_seconds,'stage_seconds':stage_seconds}
    maxima={'session_model_calls':10000,'session_tool_calls':100000,'stage_model_calls':1000,
            'stage_tool_calls':10000,'wall_seconds':86400,'stage_seconds':3600}
    if context_tokens is not None:
        if type(context_tokens) is not int or not 6000<=context_tokens<=262144:raise RepoError('INVALID_BENCHMARK_BUDGET')
        values['context_tokens']=context_tokens;maxima['context_tokens']=262144
    if not isinstance(experiment_digest,str) or not experiment_digest.startswith('sha256:'):raise RepoError('INVALID_EXPERIMENT_IDENTITY')
    if any(type(v) is not int or not 0<v<=maxima[k] for k,v in values.items()):raise RepoError('INVALID_BENCHMARK_BUDGET')
    if stage_model_calls>session_model_calls or stage_tool_calls>session_tool_calls or stage_seconds>wall_seconds:raise RepoError('INVALID_BENCHMARK_BUDGET')
    with execution_lock(runtime.store.root/'executor_locks'/(run_id+'.lock')):
        with runtime.store.tx() as c:
            run=runtime.run(run_id,c)
            if not run['data']['manifest'].get('conversation_mode') or runtime.contract(run):raise RepoError('BENCHMARK_CONVERSATION_REQUIRED')
            if run['state']!='RUNNING' or any(run['data']['budget_used'].values()) or runtime.unresolved(run_id,c):raise RepoError('BENCHMARK_BUDGET_MUST_PRECEDE_DISPATCH')
            body={'run_id':run_id,'task_id':run['task_id'],'experiment_digest':experiment_digest,**values}
            old=allocation(runtime,run,c)
            if old:
                if old!=body:raise RepoError('BENCHMARK_ALLOCATION_FROZEN')
                return old
            run['data']['benchmark_allocation']={'body':body,'signature':runtime.store.sign(body)}
            runtime.update(c,run,data=run['data'],control=True)
            runtime.store.emit(c,run_id,'benchmark.budget_admitted',{'allocation_digest':digest(body),**body},run['data']['visibility'])
    return body


def check_budget(runtime,run,kind,count=1,c=None):
    body=allocation(runtime,run,c)
    if not body:return None
    if kind not in {'model_calls','tool_calls','child_calls'} or (kind=='child_calls' and count):raise RepoError('BENCHMARK_DELEGATION_DISABLED')
    if kind=='child_calls':return body
    used=run['data']['budget_used']
    if used[kind]+count>body['session_'+kind]:raise runtime.budget_error(run,kind,'benchmark_session')
    stage=active_stage(runtime,run,c)
    if used[kind]-stage['budget_start'].get(kind,0)+count>body['stage_'+kind]:raise runtime.budget_error(run,kind,'benchmark_stage')
    if runtime.active_seconds(run)-stage['active_seconds_start']>=body['stage_seconds']:
        raise runtime.budget_error(run,'wall_seconds','benchmark_stage')
    if runtime.active_seconds(run)>=body['wall_seconds']:raise runtime.budget_error(run,'wall_seconds','benchmark_session')
    return body


def write_scope(runtime,run,plans):
    if not allocation(runtime,run):return
    stage=active_stage(runtime,run)
    import fnmatch
    allowed=stage['body']['editable_paths']
    for plan in plans:
        for name in plan['after']:
            if (any(fnmatch.fnmatchcase(name,pattern) for pattern in stage['body'].get('protected_paths',[]))
                    or not any(fnmatch.fnmatchcase(name,pattern) for pattern in allowed)):
                raise RepoError('BENCHMARK_STAGE_SCOPE_DENIED',name)


def remaining_seconds(runtime,run):
    body=allocation(runtime,run)
    if not body:return None
    stage=active_stage(runtime,run)
    active=runtime.active_seconds(run)
    return max(0,min(body['wall_seconds']-active,body['stage_seconds']-(active-stage['active_seconds_start'])))


class TrustedStageLoader:
    """Versioned two-phase Host source import with explicit crash recovery.

    Inputs are already whitelisted public stage files. A manifest cannot grant
    tool authority; its signature is checked independently by the Broker.
    """
    def __init__(self,runtime,faults=None):
        self.runtime=runtime;self.store=runtime.store;self.faults=faults or Faults()

    def _ready(self,run,c,initial=False):
        expected='RUNNING' if initial else 'WAITING_USER'
        if run['state']!=expected or not run['data']['manifest'].get('conversation_mode') or self.runtime.contract(run):raise RepoError('BENCHMARK_STAGE_NOT_WAITING')
        if run['cancel_requested'] or c.execute("SELECT 1 FROM control WHERE run_id=? AND status LIKE 'PENDING%'",(run['id'],)).fetchone():raise RepoError('CONTROL_PENDING')
        if self.runtime.unresolved(run['id'],c) or self.store.get('pending_turn',run['id'],c,{}).get('requests'):raise RepoError('RECOVERY_REQUIRED')
        if self.runtime.sandbox and not self.runtime.sandbox.resources_stopped(run['id']):raise RepoError('RESOURCE_LIFETIME_UNKNOWN')
        if not allocation(self.runtime,run,c):raise RepoError('BENCHMARK_ALLOCATION_REQUIRED')

    def admit(self,run_id,stage_id,public_files,editable_paths,*,prompt_digest,expected_before,initial=False,protected_paths=()):
        if not isinstance(stage_id,str) or not stage_id or len(stage_id)>120:raise RepoError('INVALID_BENCHMARK_STAGE')
        if not public_files or len(public_files)>20000:raise RepoError('INVALID_STAGE_FILES')
        checked={relative_path(k):v for k,v in public_files.items()}
        if any(type(v) is not bytes or len(v)>8*1024*1024 for v in checked.values()) or sum(map(len,checked.values()))>128*1024*1024:raise RepoError('WORKSPACE_LIMIT')
        editable_paths=[relative_path(p) for p in editable_paths]
        if not editable_paths:raise RepoError('INVALID_STAGE_SCOPE')
        body={'run_id':run_id,'stage_id':stage_id,'prompt_digest':prompt_digest,'editable_paths':editable_paths,
              'protected_paths':[relative_path(p) for p in protected_paths],
              'public_files':{k:digest(v) for k,v in sorted(checked.items())},'expected_before':expected_before,'initial':initial}
        identity=digest(body)
        with execution_lock(self.store.root/'executor_locks'/(run_id+'.lock')):
            with file_lock(self.store.root/'locks'/(run_id+'.lock')):
                with self.store.tx() as c:
                    run=self.runtime.run(run_id,c)
                    done=self.store.get('benchmark_stage_admission',identity,c)
                    if done and done['status']=='COMMITTED':return done
                    self._ready(run,c,initial)
                    if self.store.get('benchmark_stage_pending',run_id,c):raise RepoError('BENCHMARK_STAGE_RECOVERY_REQUIRED')
                    # A stage ID cannot be rebound to different source/permissions.
                    if self.store.get('benchmark_stage_id',digest([run_id,stage_id]),c):raise RepoError('IDEMPOTENCY_CONFLICT')
                    ws=self.runtime.workspace(run)
                    if ws.snapshot()!=expected_before:raise RepoError('STALE_VERSION')
                    before={k:(digest(ws.path(k).read_bytes()) if ws.path(k).is_file() else None) for k in checked}
                    for raw in checked.values():self.store.blob(raw)
                    complete=self.store.read_json(expected_before)['files']
                    intended={**complete,**{k:{'blob':digest(v),'executable':False} for k,v in checked.items()}}
                    if len(intended)>20000 or sum(len(self.store.read_blob(v['blob'])) for v in intended.values())>128*1024*1024:raise RepoError('WORKSPACE_LIMIT')
                    journal={'id':identity,'body':body,'status':'PREPARED',
                        'before':before,'after':body['public_files'],'control_epoch':run['control_epoch'],
                        'expected_after':self.store.json_blob({'files':intended}),
                        'writer_epoch':run['writer_epoch'],'budget_start':dict(run['data']['budget_used']),
                        'active_seconds_start':self.runtime.active_seconds(run)}
                    journal['signature']=self.store.sign(stage_payload(journal))
                    self.store.put('benchmark_stage_admission',identity,journal,c)
                    self.store.put('benchmark_stage_pending',run_id,{'id':identity},c)
                self.faults.hit('stage_prepared')
                self._finish(journal)
                return self.store.get('benchmark_stage_admission',identity)

    def _finish(self,journal):
        if not self.store.verify(stage_payload(journal),journal.get('signature','')):raise RepoError('BENCHMARK_STAGE_SIGNATURE_INVALID')
        run_id=journal['body']['run_id']
        for index,(name,value) in enumerate(journal['after'].items()):
            with self.store.tx() as c:
                run=self.runtime.run(run_id,c);self._ready(run,c,journal['body']['initial'])
                if run['control_epoch']!=journal['control_epoch'] or run['writer_epoch']!=journal['writer_epoch']:raise RepoError('BENCHMARK_STAGE_STALE_FENCE')
                mutator=Mutator(self.runtime.workspace(run),run_id)
                current=digest(mutator.ws.path(name).read_bytes()) if mutator.ws.path(name).is_file() else None
                if current not in {journal['before'][name],value}:raise RepoError('RECOVERY_REQUIRED','External stage change preserved')
                mutator.set_state({name:value})
            self.faults.hit('stage_file_'+str(index))
        self.faults.hit('stage_before_commit')
        with self.store.tx() as c:
            run=self.runtime.run(run_id,c);self._ready(run,c,journal['body']['initial'])
            if run['control_epoch']!=journal['control_epoch'] or run['writer_epoch']!=journal['writer_epoch']:raise RepoError('BENCHMARK_STAGE_STALE_FENCE')
            ws=self.runtime.workspace(run)
            if any(digest(ws.path(k).read_bytes())!=v for k,v in journal['after'].items()):raise RepoError('RECOVERY_REQUIRED')
            if ws.snapshot()!=journal['expected_after']:raise RepoError('RECOVERY_REQUIRED','Unexpected change outside admitted stage preserved')
            journal={**journal,'status':'COMMITTED','source_after':ws.snapshot()}
            self.store.put('benchmark_stage_admission',journal['id'],journal,c)
            self.store.put('benchmark_stage_id',digest([run_id,journal['body']['stage_id']]),{'id':journal['id']},c)
            self.store.put('benchmark_active_stage',run_id,journal,c)
            c.execute("DELETE FROM objects WHERE kind='benchmark_stage_pending' AND id=?",(run_id,))
            # Invalidate all file observations and context/control preparations.
            for path in self.store.read_json(journal['source_after'])['files']:
                c.execute("DELETE FROM objects WHERE kind='observation' AND id=?",(digest([run_id,path]),))
            self.runtime.update(c,run,data=run['data'],writer=True,control=True)
            self.store.emit(c,run_id,'benchmark.stage_admitted',{'id':journal['id'],'stage_id':journal['body']['stage_id'],
                'source_before':journal['body']['expected_before'],'source_after':journal['source_after']},run['data']['visibility'])
        self.faults.hit('stage_after_commit')

    def recover(self,run_id):
        with execution_lock(self.store.root/'executor_locks'/(run_id+'.lock')):
            with file_lock(self.store.root/'locks'/(run_id+'.lock')):
                pending=self.store.get('benchmark_stage_pending',run_id)
                if not pending:return None
                journal=self.store.get('benchmark_stage_admission',pending['id'])
                if not journal:raise RepoError('BENCHMARK_STAGE_SIGNATURE_INVALID')
                self._finish(journal)
                return self.store.get('benchmark_stage_admission',pending['id'])
