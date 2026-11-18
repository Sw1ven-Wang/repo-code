"""Host observations of repeated processes and inspections, independent of Memory.

An observation is not a proof that a command is pure. External state may change;
two explicitly justified retries remain available after the normal allowance.
"""
from __future__ import annotations
import contextlib
import json
from .util import digest

WARN_AFTER = 3
ALLOW_IDENTICAL = 5
EXPLICIT_RETRIES = 2
INSPECTION_TOOLS=frozenset({'read','search','glob','artifact.read','history.search','history.expand',
                           'memory.search','skill.search','memory.read','memory.explain','workspace.diff'})


def action_digest(arguments):
    return digest({k: v for k, v in arguments.items() if k != 'retry_reason'})


def observation(arguments, result):
    """Exclude transport IDs/timing; never compare incomplete/unknown outcomes."""
    if (result.get('running') or result.get('cancelled') or result.get('timeout')
            or not isinstance(result.get('exit_code'), int)
            or not result.get('relevant_state') or not result.get('environment_digest')
            or not all(isinstance(result.get(k), str) for k in ('stdout', 'stderr'))):
        return None
    data = {'action_digest': action_digest(arguments),
            'source_digest': result['relevant_state'],
            'environment_digest': result['environment_digest'],
            'exit_code': result['exit_code'], 'is_error': bool(result.get('is_error')),
            'stdout_digest': digest(result['stdout']), 'stderr_digest': digest(result['stderr']),
            'output_snapshot': result.get('output_snapshot')}
    return {**data, 'signature': digest(data)}


class ProcessProgress:
    def __init__(self, store, runtime):
        self.store, self.runtime = store, runtime

    def history(self, req):
        with contextlib.closing(self.store.connect()) as c:
            rows = c.execute("SELECT request,result FROM operations WHERE run_id=? AND state IN ('SUCCEEDED','FAILED') ORDER BY created DESC,rowid DESC", (req.run_id,)).fetchall()
        action = action_digest(req.arguments)
        found = []
        for row in rows:
            request = json.loads(row['request'])
            if request['tool_name'] != 'process.run' or action_digest(request['arguments']) != action:
                continue
            result = json.loads(row['result'])
            progress = result.get('process_progress')
            # An intervening timeout, new output, or older schema breaks the streak.
            if not progress or (found and any(progress.get(k) != found[0][0].get(k)
                                              for k in ('signature', 'writer_epoch', 'control_epoch'))):
                break
            found.append((progress, request['arguments'], result))
        return found

    def rejection(self, req):
        if req.tool_name in INSPECTION_TOOLS:return self._inspection_rejection(req)
        if req.tool_name != 'process.run':
            return None
        history = self.history(req)
        if len(history) < ALLOW_IDENTICAL:
            return None
        last, _, result = history[0]
        run = self.runtime.run(req.run_id)
        if (last['source_digest'] != self.runtime.workspace(run).snapshot()
                or last['writer_epoch'] != run['writer_epoch']
                or last['control_epoch'] != run['control_epoch']
                or last['environment_digest'] != self.runtime.sandbox.environment_digest):
            return None
        reason = (req.arguments.get('retry_reason') or '').strip()
        reasons = {(args.get('retry_reason') or '').strip() for _, args, _ in history}
        if reason and reason not in reasons and len(history) < ALLOW_IDENTICAL + EXPLICIT_RETRIES:
            return None
        return {'is_error': True, 'code': 'PROCESS_NO_PROGRESS', 'executed': False,
                'identical_observations': len(history), 'signature': last['signature'],
                'artifact': result.get('result_artifact') or result.get('artifact'),
                'message': 'Same command produced identical output with unchanged source and environment. '
                           'Use artifact.read for the existing result, narrow the inspection, edit code, or choose a new hypothesis. '
                           'For external-state changes/flakiness, supply a new retry_reason; at most two extra identical executions are allowed.'}

    def record(self, req, result):
        if req.tool_name in INSPECTION_TOOLS:return self._record_inspection(req,result)
        if req.tool_name != 'process.run':
            return result
        data = observation(req.arguments, result)
        run = self.runtime.run(req.run_id)
        history = self.history(req)
        epochs = {'writer_epoch': run['writer_epoch'], 'control_epoch': run['control_epoch']}
        count = 1
        if data and history and all(history[0][0].get(k) == v for k, v in {**epochs, 'signature': data['signature']}.items()):
            count += len(history)
        if data:
            data.update(epochs, identical_observations=count, warning=count >= WARN_AFTER)
            result = {**result, 'process_progress': data}
        self.store.put('process_progress', req.run_id, data or {})
        return result

    def _inspection_state(self,run):
        return {'source_digest':self.runtime.workspace(run).snapshot(),'writer_epoch':run['writer_epoch'],
                'control_epoch':run['control_epoch'],'environment_digest':self.runtime.sandbox.environment_digest}

    def _inspection_history(self,req):
        with contextlib.closing(self.store.connect()) as c:
            rows=c.execute("SELECT request,result FROM operations WHERE run_id=? AND state IN ('SUCCEEDED','FAILED') ORDER BY created DESC,rowid DESC",(req.run_id,)).fetchall()
        found=[]
        for row in rows:
            request=json.loads(row['request'])
            if request['tool_name']!=req.tool_name or request['arguments']!=req.arguments:continue
            result=json.loads(row['result']);progress=result.get('inspection_progress')
            if not progress or (found and progress['signature']!=found[0][0]['signature']):break
            found.append((progress,result))
        return found

    def _inspection_rejection(self,req):
        history=self._inspection_history(req)
        if len(history)<ALLOW_IDENTICAL:return None
        last,result=history[0];state=self._inspection_state(self.runtime.run(req.run_id))
        if any(last.get(k)!=v for k,v in state.items()):return None
        return {'is_error':True,'code':'INSPECTION_NO_PROGRESS','executed':False,
                'identical_observations':len(history),'signature':last['signature'],
                'artifact':result.get('result_artifact') or result.get('artifact'),
                'message':'This inspection already returned identical evidence five times in the current source and turn. '
                          'Use the existing observation, inspect a genuinely different slice, implement the current request, or report what remains incomplete.'}

    def _record_inspection(self,req,result):
        if result.get('is_error'):
            self.store.put('inspection_progress',req.run_id,{})
            return result
        state=self._inspection_state(self.runtime.run(req.run_id))
        # Transport IDs and trace storage names are not new evidence. Actual
        # content, versions, pagination and errors remain part of the identity.
        evidence={k:v for k,v in result.items() if k not in {'result_artifact','inspection_progress','trace_id'}}
        data={**state,'tool':req.tool_name,'action_digest':action_digest(req.arguments),'evidence_digest':digest(evidence)}
        data['signature']=digest(data)
        history=self._inspection_history(req)
        count=1+(len(history) if history and history[0][0]['signature']==data['signature'] else 0)
        data.update(identical_observations=count,warning=count>=WARN_AFTER)
        self.store.put('inspection_progress',req.run_id,data)
        return {**result,'inspection_progress':data}


def runtime_feedback(store, runtime, run, provider=None, *, ledger=None):
    from .work_status import work_status
    if ledger is None:
        ledger = work_status(store,runtime,run)
    snapshot = ledger['source_digest']
    used = run['data']['budget_used']; limits = runtime.budget_limits(run)
    budget = {k: {'used': used.get(k, 0), 'limit': limits.get(k),
                  'remaining': max(0, limits[k] - used.get(k, 0)) if limits.get(k) is not None else None}
              for k in ('tool_calls', 'model_calls')}
    budget['wall_seconds_remaining'] = round(runtime.remaining_seconds(run['id']), 1)
    progress = store.get('process_progress', run['id'], default={})
    if progress and (progress['writer_epoch'] != run['writer_epoch']
                     or progress['control_epoch'] != run['control_epoch']
                     or progress['environment_digest'] != runtime.sandbox.environment_digest
                     or progress['source_digest'] != snapshot):
        progress = {}
    inspection=store.get('inspection_progress',run['id'],default={})
    if inspection and (inspection['writer_epoch']!=run['writer_epoch'] or inspection['control_epoch']!=run['control_epoch']
                       or inspection['environment_digest']!=runtime.sandbox.environment_digest
                       or inspection['source_digest']!=snapshot):inspection={}
    if provider is not None and callable(getattr(provider,'budget_status',None)):
        budget['cumulative_tokens']=provider.budget_status()
    elif run['data']['manifest'].get('memory_schema_version')==2:
        memory=getattr(runtime,'memory',None)
        if memory:
            rows=[x for x in memory.repo.values('memory_budget') if x['run_id']==run['id']]
            charged=sum(x['charged_tokens'] for x in rows);limit=memory.profile.total_token_budget
            budget['cumulative_tokens']={'limit':limit,'charged_including_unknown_reserves':charged,
                'remaining':max(0,limit-charged) if limit is not None else None,
                'unknown_usage_calls':sum(x['state']=='USAGE_UNKNOWN' for x in rows),
                'note':'Next request also needs admission reserve; remaining tokens are not a promise of another call.'}
    task=store.get('task_progress',run['id'],default={})
    current_task = (task.get('state',{}).get('control')==run['control_epoch'] and task.get('state',{}).get('source')==snapshot)
    stagnant = task.get('stagnant_batches',0) if current_task else 0
    investigation = {}
    if current_task and task.get('before_first_write'):
        investigation = {'operations_before_first_write':task['operations_in_state'],
                         'repeated_recent_reads':task['repeated_recent_reads'],
                         'recent_reads':task['recent_reads']}
        if task['investigation_warning']:
            investigation['guidance'] = ('Summarize your current hypothesis, supporting evidence, missing evidence, and next action in plan.update, '
                                         'or explain the blocker in a final response. Prefer a concrete implementation or a distinct check when supported.')
    return {'budget': budget, 'work_ledger':ledger,
            **({'investigation':investigation} if investigation else {}),
            'cross_tool_progress':{'stagnant_batches':stagnant,'warning':stagnant>=6,
                'guidance':'Reuse current evidence; implement, validate a distinct hypothesis, or hand off a final with limitations. Repeated unchanged recovery can stop this Run as PARTIAL.'},
            'process_progress': progress,'inspection_progress':inspection,
            'guidance': 'Repeated successful inspection is not task progress. After three identical observations, '
                        'form a new hypothesis or edit and test. After five, identical execution is withheld. '
                        'Keep tool budget for implementation and verification; report incomplete work honestly.'}


def coalesce_process_observations(messages):
    """Project older duplicates to references; preserve every protocol group/raw row.

    Only exact process outcomes with the same arguments, source and environment
    qualify. The newest result remains intact, even when elapsed time/IDs differ.
    """
    calls = {call['id']: call for m in messages for call in m.get('tool_calls', [])}
    latest = {}; replacements = {}
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        call = calls.get(message.get('tool_call_id'))
        if message.get('role') != 'tool' or not call or call['name'] not in {'process.run',*INSPECTION_TOOLS}:
            continue
        try:
            body = json.loads(message['content'])
        except (ValueError, TypeError):
            continue
        if not isinstance(body, dict):
            continue
        inspection=call['name'] in INSPECTION_TOOLS
        data = body.get('inspection_progress') if inspection else body.get('process_progress') or observation(call['arguments'], body)
        if not data:
            continue
        signature = data['signature']
        if signature not in latest:
            latest[signature] = message['tool_call_id']
            continue
        ref = body.get('result_artifact') or body.get('artifact')
        if not ref:
            continue
        content = json.dumps({('duplicate_inspection_observation' if inspection else 'duplicate_process_observation'): True, 'signature': signature,
                              'same_as_tool_call_id': latest[signature], 'artifact': ref,
                              'exit_code': body.get('exit_code'), 'is_error': bool(body.get('is_error')),
                              'retrieve_with': 'artifact.read'}, ensure_ascii=False)
        replacements[i] = {**message, 'content': content}
    return [replacements.get(i, message) for i, message in enumerate(messages)]


def latest_process_group(complete):
    """A withheld call must not demote the latest real process observation."""
    for index in range(len(complete) - 1, -1, -1):
        group = complete[index]
        ids = {c['id'] for m in group for c in m.get('tool_calls', []) if c['name'] == 'process.run'}
        for message in group:
            if message.get('tool_call_id') not in ids:
                continue
            try:
                body = json.loads(message['content'])
            except (ValueError, TypeError):
                continue
            if isinstance(body, dict) and (body.get('process_progress') or isinstance(body.get('stdout'), str)):
                return index
    return -1
