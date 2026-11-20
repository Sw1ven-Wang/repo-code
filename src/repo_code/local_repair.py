"""One Host-verified retrieval adjustment within a run. Never a capability grant."""
import json

from .util import RepoError, digest
from .memory.retrieval import EXPERIENCE_QUERY_CHAR_LIMIT


class LocalQueryRepair:
    kind = 'local_query_repair'
    prompt = ('Host diagnostic turn. The original task is unchanged. '
              'Propose up to 16 search terms copied verbatim from the current task, up to 256 characters '
              'in all. Return only {"query":"terms"} or {"query":""} to decline. No tool calls. '
              'The Host will test the query; it grants no permissions.')

    def __init__(self, runtime, run, goal, options):
        self.runtime, self.store = runtime, runtime.store
        self.run_id, self.goal, self.options = run['id'], goal, options
        self.fence = self._fence(run)
        self.context = digest(options)

    def _fence(self, run):
        m = run['data']['manifest']
        return digest([run['id'],run['control_epoch'],run['writer_epoch'],m['release_digest'],
                       m['task_spec_revision'],m['environment_digest'],self.runtime.current_goal(run)])

    def _transition(self, previous, status, **details):
        with self.store.tx() as c:
            run = self.runtime.run(self.run_id,c)
            if (run['state']!='RUNNING' or run['cancel_requested'] or self._fence(run)!=self.fence or
                    c.execute("SELECT 1 FROM control WHERE run_id=? AND status LIKE 'PENDING%'",(self.run_id,)).fetchone()):
                raise RepoError('STALE_CONTROL')
            old=self.store.get(self.kind,self.run_id,c) or {}
            if old.get('status')!=previous:
                raise RepoError('LOCAL_REPAIR_STATE_CHANGED')
            row={**old,'fence':self.fence,'context':self.context,'status':status,**details}
            self.store.put(self.kind,self.run_id,row,c)
            self.store.emit(c,self.run_id,'repair.query_stage',row,run['data']['visibility'])
            if status in {'LOCAL_QUERY_VERIFIED','QUERY_ACCEPTED_NO_MATCH'}:
                evidence={'kind':'LOCAL_QUERY_CHECK','run_id':self.run_id,**row,
                          'environment':run['data']['manifest']['environment_digest']}
                self.store.put('local_query_receipt',self.run_id,{'payload':evidence,
                               'signature':self.store.sign(evidence)},c,immutable=True)
            return row

    def prepare(self, router):
        """Return the ordinary route, or reserve one diagnostic main turn before dispatch."""
        run=self.runtime.run(self.run_id)
        row=self.store.get(self.kind,self.run_id) or {}
        if row.get('status') in {'DISPATCHING','DISPATCH_FAILED','VERIFYING'}:
            raise RepoError('LOCAL_REPAIR_RECONCILIATION_REQUIRED')
        same=row.get('fence')==self.fence and row.get('context')==self.context
        if same and row['status'] in {'LOCAL_QUERY_VERIFIED','QUERY_ACCEPTED_NO_MATCH'}:
            receipt=self.store.get('local_query_receipt',self.run_id)
            if not receipt or not self.store.verify(receipt['payload'],receipt['signature']) or any(
                    receipt['payload'].get(k)!=v for k,v in row.items()):
                raise RepoError('LOCAL_REPAIR_RECEIPT_INVALID')
            return router.route(self.run_id,self.goal,search_query=row['query'],**self.options),False
        original=None
        try:
            original=router.route(self.run_id,self.goal,**self.options)
        except RepoError as exc:
            if exc.code!='INVALID_MEMORY_QUERY':raise
            reason=exc.code
        else:
            if len(self.goal)<=EXPERIENCE_QUERY_CHAR_LIMIT or original['considered_contract_refs']:
                return original,False
            trace=router.owner.repo.get('retrieval_traces',original['retrieval_trace'])
            if not trace or not trace.get('eligible_count') or trace.get('selected') or trace.get('candidate_count'):
                return original,False
            reason='SHORT_QUERY_NO_MATCH'
        # A reserved or interrupted attempt is never redispatched, even after restart.
        # Reserve room for at least one subsequent ordinary model turn.
        remaining=self.runtime.budget_limits(run)['model_calls']
        if row or (remaining is not None and remaining-run['data']['budget_used']['model_calls']<2):
            return original,False
        self._transition(None,'RESERVED',reason=reason,goal_digest=digest(self.goal),
                         original_trace=original['retrieval_trace'] if original else None)
        self.prompt+=' Observed: '+('the experience retriever rejected the search text (INVALID_MEMORY_QUERY).'
                    if reason=='INVALID_MEMORY_QUERY' else 'the shortened query returned no matching experience.')
        return None,True

    def dispatch(self):
        self._transition('RESERVED','DISPATCHING')

    def close_dispatch(self,status):
        # A stale answer or transport error can close this exact attempt only;
        # it cannot verify a proposal or modify the new task/epoch's context.
        if status not in {'STALE_RESPONSE','NOT_DISPATCHED','DISPATCH_FAILED'}:raise ValueError(status)
        with self.store.tx() as c:
            row=self.store.get(self.kind,self.run_id,c)
            if row and row['status']=='DISPATCHING' and row['fence']==self.fence:
                row={**row,'status':status}
                self.store.put(self.kind,self.run_id,row,c)
                self.store.emit(c,self.run_id,'repair.query_stage',row,'DEV')

    def verify(self, router, reply):
        try:
            proposal=json.loads(reply.text) if len(reply.text)<=1024 else None
        except (ValueError,RecursionError):
            proposal=None
        query=proposal.get('query') if isinstance(proposal,dict) and set(proposal)=={'query'} else None
        if (reply.tool_calls or reply.stop_reason not in {'stop','end_turn'} or not isinstance(query,str) or
                not query.strip() or len(query)>256 or len(query.split())>16 or
                any(word not in self.goal for word in query.split())):
            self._transition('DISPATCHING','NO_PROPOSAL')
            return
        query=' '.join(query.split())
        current=self.runtime.run(self.run_id)
        self.runtime.assert_executable(current)
        if {p:v['blob'] for p,v in self.runtime.workspace(current).manifest()['files'].items()}!=self.options['file_versions']:
            self._transition('DISPATCHING','NOT_VERIFIED',error='WORKSPACE_CHANGED')
            return
        self._transition('DISPATCHING','VERIFYING',query=query)
        self.runtime.check_wall(self.run_id,'local_query_verify')
        try:
            route=router.route(self.run_id,self.goal,search_query=query,**self.options)
        except RepoError as exc:
            self._transition('VERIFYING','NOT_VERIFIED',error=exc.code)
            if exc.code!='INVALID_MEMORY_QUERY':raise
            return
        # A successful lookup with zero admissible hints is not a successful match.
        self._transition('VERIFYING','LOCAL_QUERY_VERIFIED' if route['guidance'] else 'QUERY_ACCEPTED_NO_MATCH',
                         verified_trace=route['retrieval_trace'],route_id=route['id'],
                         reuse_authorized=False)
