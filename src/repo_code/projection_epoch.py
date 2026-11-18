"""Versioned Host projections. Raw evidence and authorization never change here."""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
import json
import math
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .context import estimate_tokens, groups
from .live_compact import indexed_groups, protocol_supported, source_archive
from .util import RepoError, canonical, digest


class EpochProfile(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True,strict=True)
    version: int=Field(default=2,ge=2,le=2)
    preserve_reasoning: bool=False
    high_watermark: float=Field(default=0.75,gt=0,lt=1)
    low_watermark: float=Field(default=0.60,gt=0,lt=1)
    recent_groups: int=Field(default=3,ge=1,le=20)
    @model_validator(mode='after')
    def ordered(self):
        if self.low_watermark>=self.high_watermark:raise ValueError('low watermark must be below high')
        return self


def seal_turn(runtime,run,c):
    """Called only in the trusted WAITING_USER transition, never a model tool."""
    cfg=run['data']['manifest'].get('context_compaction') or {}
    if cfg.get('mode')!='live-experimental-v2':return
    rows=c.execute('SELECT id,data FROM messages WHERE run_id=? ORDER BY id',(run['id'],)).fetchall()
    if not rows:return
    active=(run['data'].get('active_turn') or {}).get('text',run['data']['goal'])
    start=next((i for i in range(len(rows)-1,-1,-1) if json.loads(rows[i]['data'])=={'role':'user','content':active}),None)
    if start is None:raise RepoError('EPOCH_ACTIVE_TURN_MISSING')
    value={'run_id':run['id'],'start_id':str(rows[start]['id']),'end_id':str(rows[-1]['id']),
        'source_digest':digest([json.loads(r['data']) for r in rows[start:]]),
        'closure':'HOST_HANDOFF_ONLY','external_acceptance':False}
    runtime.store.put('projection_closed_turn',digest(value),value,c,immutable=True)


@dataclass(frozen=True)
class EpochProposal:
    body: dict
    source: list
    cards: dict
    fence: dict
    expected_pointer: dict | None


class EpochProjection:
    def __init__(self,store,runtime,faults=None):
        from .live_compact_state import LiveProjection
        self.live=LiveProjection(store,runtime,faults);self.store=store;self.runtime=runtime;self.faults=self.live.faults

    def cfg(self,run_id):
        run=self.runtime.run(run_id);cfg=run['data']['manifest'].get('context_compaction') or {}
        if cfg.get('mode')!='live-experimental-v2':raise RepoError('EPOCH_NEW_RUN_REQUIRED')
        profile=EpochProfile.model_validate(cfg['profile'])
        if digest(profile)!=cfg['profile_digest']:raise RepoError('RUN_CONTEXT_COMPACTION_CHANGED')
        return run,cfg,profile

    def pointer(self,run_id,c=None):return self.store.get('active_projection_epoch',run_id,c)

    def fence(self,run_id,c=None):
        return {**self.live.fence(run_id,c),'active_projection_epoch':self.pointer(run_id,c)}

    def _safe(self,run,c):
        self.runtime.assert_executable(run)
        if self.runtime.unresolved(run['id'],c):raise RepoError('EPOCH_HOST_NOT_SETTLED')
        if self.store.get('pending_turn',run['id'],c,{}).get('requests'):raise RepoError('EPOCH_HOST_NOT_SETTLED')
        if self.runtime.sandbox and not self.runtime.sandbox.resources_stopped(run['id']):raise RepoError('EPOCH_HOST_NOT_SETTLED')

    def _sources(self,run_id,records,c=None):
        """Resolve the same raw message/artifact revocations used by Memory/L1."""
        if c is None:
            with self.store.connect() as connection:
                return self._sources(run_id,records,connection)
        from .governance import Lineage
        for row in records:
            refs={run_id,'message:'+row['id'],digest(row['data'])}
            if row['data']['role']=='tool':
                try:body=json.loads(row['data']['content'])
                except (ValueError,TypeError):body={}
                if isinstance(body,dict):
                    refs.update(body[k] for k in ('artifact','result_artifact','source_artifact','output_snapshot') if isinstance(body.get(k),str))
            for ref in refs:
                if any(self.store.get('revocation',r,c) for r in {ref,*Lineage(self.store).ancestors(ref,c)}):raise RepoError('EPOCH_SOURCE_REVOKED')
        if c is not None and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_control'").fetchone():
            if c.execute("SELECT 1 FROM memory_control WHERE id='restore_fence'").fetchone():raise RepoError('REVOCATION_SYNC_REQUIRED')

    def _closed(self,run_id,records):
        positions={r['id']:i for i,r in enumerate(records)};closed={}
        for value in self.store.values('projection_closed_turn'):
            if value['run_id']!=run_id:continue
            start=positions.get(value['start_id']);end=positions.get(value['end_id'])
            if start is None or end is None or digest([r['data'] for r in records[start:end+1]])!=value['source_digest']:
                raise RepoError('EPOCH_TURN_SOURCE_CHANGED')
            for i in range(start,end+1):closed[records[i]['id']]=value['start_id']
        return closed

    def _eligible(self,run_id,indexed,profile):
        all_rows=[r for _,_,rs in indexed for r in rs];closed=self._closed(run_id,all_rows)
        recent={i for i,g,_ in indexed if g[0].get('tool_calls')}
        recent=set(sorted(recent)[-profile.recent_groups:])
        with self.store.connect() as c:
            verified={r['id']:r for r in self.live._verify_sources(run_id,all_rows,c)}
        result={}
        for i,g,rs in indexed:
            if i in recent or any(m['role']=='user' for m in g):continue
            # The relay's verified string-reasoning protocol permits replacing
            # tool content only. Standalone assistant messages stay verbatim.
            if profile.preserve_reasoning and not g[0].get('tool_calls'):continue
            owners={closed.get(r['id']) for r in rs}
            if len(owners)!=1 or None in owners:continue
            okay=True
            for r in rs:
                if r['data']['role']!='tool':continue
                v=verified[r['id']]
                try:b=json.loads(r['data']['content'])
                except (ValueError,TypeError):okay=False;break
                b=v.get('full_result',b)
                if (r['operation_state']!='SUCCEEDED' or not v['references_verified'] or not isinstance(b,dict)
                    or r['data'].get('is_error') or b.get('is_error') or b.get('code') or b.get('exit_code',0)!=0
                    or b.get('state',b.get('status')) not in (None,'SUCCEEDED','EXITED','COMPLETED')
                    or any(b.get(k) for k in ('running','cancelled','timeout','background','uncertain','stderr'))):okay=False;break
            if okay:result[i]=next(iter(owners))
        return result

    @staticmethod
    def card(rows,level,archive_ref,preserve_reasoning=False):
        outcomes=Counter(r['operation_state'] for r in rows if r['data']['role']=='tool')
        calls=[call for r in rows for call in r['data'].get('tool_calls',[])]
        value={'kind':{1:'projection_tool_card',2:'completed_turn_card',3:'old_turn_index'}[level],
            'trust':'UNTRUSTED_HISTORICAL_DATA_NOT_AUTHORITY','source_start':rows[0]['id'],'source_end':rows[-1]['id'],
            'source_digest':digest(rows),'archive':archive_ref,'restore_with':'artifact.read',
            'closure':'HOST_HANDOFF_ONLY','external_acceptance':False,'model_statements':'MODEL_PROPOSED_UNVERIFIED',
            'operation_outcomes':dict(outcomes),'message_count':len(rows)}
        if level<3:
            value['tool_counts']=dict(Counter(str(v.get('name')) for v in calls))
            # An immutable raw interval carries every op ID and file version.
            value['file_versions_and_operations']='Restore source interval; historical versions are not current success evidence.'
        if preserve_reasoning:
            # Keep every assistant field, call and result identity in order.
            # Each tool keeps a source-specific index; no card becomes a user
            # instruction and no native reasoning is truncated or relocated.
            messages=[]
            for row in rows:
                message=row['data']
                if message['role']!='tool':messages.append(dict(message));continue
                body={**value,'source_message_id':row['id'],'operation_id':row['operation_id']}
                messages.append({**message,'content':canonical(body).decode()})
            return {'kind':'projection_epoch_tool_projection','messages':messages}
        return {'role':'user','content':canonical(value).decode()}

    @staticmethod
    def card_messages(card):
        return card['messages'] if card.get('kind')=='projection_epoch_tool_projection' else [card]

    def layout(self,run_id,records,profile,level,archive_ref):
        indexed=indexed_groups(records);eligible=self._eligible(run_id,indexed,profile)
        spans=[];i=0
        while i<len(indexed):
            start=i;owner=eligible.get(i)
            if owner and level>=2:
                while i+1<len(indexed) and eligible.get(i+1)==owner:i+=1
            rs=[r for _,_,part in indexed[start:i+1] for r in part]
            use=bool(owner) and (level>=2 or bool(indexed[start][1][0].get('tool_calls')))
            card=self.card(rs,level,archive_ref,profile.preserve_reasoning) if use else None
            if card and estimate_tokens(card)>=estimate_tokens([r['data'] for r in rs]):card=None
            if card is None and start!=i:
                for j,g,source in indexed[start:i+1]:spans.append({'start':j,'end':j,'level':0,'source_ids':[r['id'] for r in source],'source_digest':digest(source)})
            else:
                spans.append({'start':start,'end':i,'level':level if card else 0,
                    'source_ids':[r['id'] for r in rs],'source_digest':digest(rs),**({'card_digest':digest(card)} if card else {})})
            i+=1
        cards={};view=[g for _,g,_ in indexed]
        for span in spans:
            if span['level']:
                rs=[r for _,_,source in indexed[span['start']:span['end']+1] for r in source]
                card=self.card(rs,span['level'],archive_ref,profile.preserve_reasoning);cards[digest(card)]=card
                view[span['start']]=self.card_messages(card)
                for j in range(span['start']+1,span['end']+1):view[j]=[]
        return view,spans,cards

    def prepare(self,run_id,*,system,tools,serialize,available_history,scale=1.0,prefix_messages=(),fits_limit=None):
        run,cfg,profile=self.cfg(run_id);fence=self.fence(run_id);records=self.live.snapshot(run_id)
        if not protocol_supported([r['data'] for r in records],profile.preserve_reasoning):raise RepoError('PROTOCOL_REBASE_UNVERIFIED')
        self._sources(run_id,records)
        with self.store.connect() as c:self._safe(run,c)
        old=self.pointer(run_id)
        archive_ref=digest(source_archive(records));raw=[r['data'] for r in records]
        fixed=math.ceil(estimate_tokens(serialize(system,list(prefix_messages),tools))*scale)
        measure=lambda v:max(0,math.ceil(estimate_tokens(serialize(system,[*prefix_messages,*[m for g in v for m in g]],tools))*scale)-fixed)
        raw_tokens=measure(groups(raw));view=None
        for level in (1,2,3):
            view,coverage,cards=self.layout(run_id,records,profile,level,archive_ref)
            cost=measure(view)
            if cost<=profile.low_watermark*available_history:break
        # The low watermark is a maintenance target, not an admission limit.
        # Required recent evidence may fit H without reaching the target.
        if view is None or cost>available_history:
            raise RepoError('UNCOMPRESSIBLE_REQUIRED_CONTEXT','UNCOMPRESSIBLE_REQUIRED_CONTEXT: active requests, users, recent groups or unresolved evidence exceed history budget; no records removed')
        if fits_limit and not fits_limit([*prefix_messages,*[m for g in view for m in g]]):raise RepoError('UNCOMPRESSIBLE_REQUIRED_CONTEXT')
        body={'version':2,'run_id':run_id,'parent_epoch_id':(old or {}).get('id'),
            'generation':(old or {}).get('generation',0)+1,'manifest_digest':digest(run['data']['manifest']),
            'profile_digest':cfg['profile_digest'],'source_end':records[-1]['id'] if records else None,
            'source_digest':digest(records),'archive_ref':archive_ref,'coverage':coverage,'semantic_refs':[],
            'view_digest':digest(view),'raw_history_tokens':raw_tokens,'provider_view_tokens':cost,
            'available_history_tokens':available_history,'level':level,'savings_estimate':raw_tokens-cost,
            'low_watermark_reached':cost<=profile.low_watermark*available_history,
            'commit_fence':fence}
        if self.fence(run_id)!=fence:raise RepoError('EPOCH_STALE')
        return EpochProposal(body,records,cards,fence,old)

    def commit(self,proposal):
        b=proposal.body;rid=b['run_id'];ident=digest(b)
        self.faults.hit('epoch_before_source_index')
        ref=self._durable_blob(source_archive(proposal.source))
        if ref!=b['archive_ref']:raise RepoError('EPOCH_SOURCE_CHANGED')
        for key,card in proposal.cards.items():
            if self._durable_blob(card)!=key:raise RepoError('EPOCH_CARD_CORRUPT')
        self.faults.hit('epoch_after_cards')
        with self.store.tx() as c:
            if self.fence(rid,c)!=proposal.fence:raise RepoError('EPOCH_STALE')
            record=self.store.get('projection_epoch',ident,c)
            if record and record['state']=='COMMITTED':return record
            self.store.put('projection_epoch',ident,{'id':ident,'body':b,'state':'PREPARED'},c)
        self.faults.hit('epoch_after_record')
        self.faults.hit('epoch_before_cas')
        with self.store.tx() as c:
            record=self.store.get('projection_epoch',ident,c)
            if record and record['state']=='COMMITTED' and self.pointer(rid,c)=={'id':ident,'generation':b['generation']}:return record
            if self.pointer(rid,c)!=proposal.expected_pointer or self.fence(rid,c)!=proposal.fence:raise RepoError('EPOCH_STALE')
            self._safe(self.runtime.run(rid,c),c)
            current=self.live.snapshot(rid,c)
            if current!=proposal.source:raise RepoError('EPOCH_SOURCE_CHANGED')
            self._sources(rid,current,c)
            compressed_ids={i for span in b['coverage'] if span['level'] for i in span['source_ids']}
            if not all(r['references_verified'] for r in self.live._verify_sources(rid,
                [r for r in current if r['id'] in compressed_ids],c)):
                raise RepoError('EPOCH_SOURCE_MISSING')
            self.store.read_blob(ref)
            for key in proposal.cards:self.store.read_blob(key)
            for key,kind in [(ref,'projection_epoch_source'),*[(k,'projection_epoch_card') for k in proposal.cards]]:
                self.store.put('run_artifact',digest([rid,key]),{'run_id':rid,'artifact':key,'kind':kind},c,immutable=True)
            # Artifact reads must inherit revocation of the archived originals,
            # even if a stale in-flight response still knows an old archive ID.
            parents={rid}
            for row in current:
                parents.update(('message:'+row['id'],digest(row['data'])))
                if row['data']['role']=='tool':
                    try:result=json.loads(row['data']['content'])
                    except (TypeError,ValueError):result={}
                    if isinstance(result,dict):
                        parents.update(result[k] for k in ('artifact','result_artifact','source_artifact','output_snapshot')
                            if isinstance(result.get(k),str))
            c.executemany('INSERT OR IGNORE INTO lineage(child,parent) VALUES(?,?)',[(ref,p) for p in parents if p!=ref])
            c.executemany('INSERT OR IGNORE INTO lineage(child,parent) VALUES(?,?)',[(key,ref) for key in proposal.cards if key!=ref])
            record={'id':ident,'body':b,'state':'COMMITTED','signature':self.store.sign(b)}
            self.store.put('projection_epoch',ident,record,c)
            self.store.put('active_projection_epoch',rid,{'id':ident,'generation':b['generation']},c)
            self.store.put('projection_epoch_event',ident,{'kind':'EPOCH_REBASE' if b['parent_epoch_id'] else 'EPOCH_CREATED',
                'epoch_id':ident,'run_id':rid,'savings_estimate':b['savings_estimate'],'estimate_only':True},c,immutable=True)
        self.faults.hit('epoch_after_activation')
        return record

    def _durable_blob(self,value):
        """Concurrent identical writers may race Windows atomic replacement.

        A loser can reuse only an existing, digest-verified identical blob.
        Permission errors without a complete matching winner still propagate.
        """
        raw=canonical(value)
        try:return self.store.blob(raw)
        except (PermissionError,FileExistsError):
            key=digest(raw)
            if self.store.read_blob(key)!=raw:raise RepoError('EPOCH_SOURCE_CHANGED')
            return key

    def recover(self,run_id):
        with self.store.tx() as c:
            for row in c.execute("SELECT id,data FROM objects WHERE kind='projection_epoch'").fetchall():
                v=json.loads(row['data'])
                if v['body']['run_id']==run_id and v['state']=='PREPARED':
                    self.store.put('projection_epoch',row['id'],{**v,'state':'ABORTED'},c)

    def replay(self,run_id,records,epoch_id=None):
        run,cfg,profile=self.cfg(run_id);pointer=self.pointer(run_id)
        ident=epoch_id or (pointer or {}).get('id')
        if not ident:
            if any(v['body']['run_id']==run_id and v['state']=='COMMITTED' for v in self.store.values('projection_epoch')):
                raise RepoError('EPOCH_CHECKPOINT_CORRUPT')
            return groups([r['data'] for r in records]),None
        value=self.store.get('projection_epoch',ident)
        if not value or value['state']!='COMMITTED':raise RepoError('EPOCH_CHECKPOINT_CORRUPT')
        b=value['body']
        if (digest(b)!=ident or not self.store.verify(b,value.get('signature','')) or b['run_id']!=run_id
            or b['manifest_digest']!=digest(run['data']['manifest']) or b['profile_digest']!=cfg['profile_digest']):raise RepoError('EPOCH_CHECKPOINT_CORRUPT')
        if epoch_id is None and pointer!={'id':ident,'generation':b['generation']}:raise RepoError('EPOCH_CHECKPOINT_CORRUPT')
        prefix=[r for r in records if int(r['id'])<=int(b['source_end'])]
        if digest(prefix)!=b['source_digest']:raise RepoError('EPOCH_SOURCE_CHANGED')
        self._sources(run_id,prefix)
        archive=json.loads(self.live._reference(run_id,b['archive_ref']))
        if archive!=source_archive(prefix):raise RepoError('EPOCH_SOURCE_CHANGED')
        indexed=indexed_groups(prefix);view=[g for _,g,_ in indexed];expected=0
        for span in b['coverage']:
            start,end=span['start'],span['end']
            if start!=expected or not start<=end<len(indexed):raise RepoError('EPOCH_COVERAGE_CORRUPT')
            rows=[r for _,_,source in indexed[start:end+1] for r in source];expected=end+1
            if span['source_ids']!=[r['id'] for r in rows] or span['source_digest']!=digest(rows):raise RepoError('EPOCH_SOURCE_CHANGED')
            if span['level']:
                with self.store.connect() as c:
                    if not all(r['references_verified'] for r in self.live._verify_sources(run_id,rows,c)):
                        raise RepoError('EPOCH_SOURCE_MISSING')
                card=self.card(rows,span['level'],b['archive_ref'],profile.preserve_reasoning)
                if digest(card)!=span['card_digest'] or json.loads(self.live._reference(run_id,span['card_digest']))!=card:raise RepoError('EPOCH_CARD_CORRUPT')
                view[start]=self.card_messages(card)
                for j in range(start+1,end+1):view[j]=[]
        if expected!=len(indexed) or digest(view)!=b['view_digest']:raise RepoError('EPOCH_COVERAGE_CORRUPT')
        tail=[r['data'] for r in records[len(prefix):]]
        return [*view,*groups(tail)],value

    def project(self,run_id,slots,*,system,tools,serialize,available_history,scale=1.0,prefix_messages=(),maintenance=True,fits_limit=None,**unused):
        run,cfg,profile=self.cfg(run_id);records=self.live.snapshot(run_id);view,value=self.replay(run_id,records)
        fixed=math.ceil(estimate_tokens(serialize(system,list(prefix_messages),tools))*scale)
        measure=lambda v:max(0,math.ceil(estimate_tokens(serialize(system,[*prefix_messages,*[m for g in v for m in g]],tools))*scale)-fixed)
        raw_tokens=measure(groups([r['data'] for r in records]));before=measure(view);created=False
        if maintenance and (before>=profile.high_watermark*available_history or fits_limit and not fits_limit([*prefix_messages,*[m for g in view for m in g]])):
            self.recover(run_id)
            proposal=self.prepare(run_id,system=system,tools=tools,serialize=serialize,available_history=available_history,
                scale=scale,prefix_messages=prefix_messages,fits_limit=fits_limit)
            value=self.commit(proposal);view,value=self.replay(run_id,records);created=True
        count=(value or {}).get('body',{}).get('generation',0);cost=measure(view)
        card_positions={s['start'] for s in (value or {}).get('body',{}).get('coverage',[]) if s['level']}
        card_cost=measure([g for i,g in enumerate(view) if i in card_positions])
        unresolved=[]
        for _,g,rows in indexed_groups(records):
            failed=False
            for r in rows:
                if r['data']['role']!='tool':continue
                try:body=json.loads(r['data']['content'])
                except (TypeError,ValueError):body={}
                failed=failed or r['operation_state']!='SUCCEEDED' or bool(r['data'].get('is_error'))
                if isinstance(body,dict):
                    failed=failed or bool(body.get('is_error') or body.get('code') or body.get('stderr')
                        or body.get('exit_code',0)!=0 or any(body.get(k) for k in ('running','timeout','uncertain','cancelled','background')))
            if failed:unresolved.append(g)
        level=(value or {}).get('body',{}).get('level')
        return view,{'version':2,'reason':'EPOCH_REBASED' if created else 'EPOCH_REPLAY' if value else 'BELOW_TRIGGER',
            'projection_epoch_id':value['id'] if value else None,'epoch_generation':count,'head':None,'cuts':[],
            'protected_group_positions':[i for i,g in enumerate(view) if g],
            'coverage':value['body']['coverage'] if value else [],'fence':self.fence(run_id),
            'raw_history_tokens':raw_tokens,'provider_view_tokens':cost,'locked_uncut_tokens':0,
            'locked_card_tokens':card_cost,'available_history_tokens':available_history,
            'low_watermark_reached':cost<=profile.low_watermark*available_history,
            'compaction_trigger_count':count,'compaction_skip_reasons':{},'protocol_skip_count':0,
            'current_unresolved_evidence_tokens':measure(unresolved),'source_restore_success_rate':None,
            'total_projection_savings':raw_tokens-cost,
            'local_cut_savings':max(0,before-cost) if created and level==1 else 0,
            'turn_rollup_savings':max(0,before-cost) if created and level==2 else 0,
            'epoch_rebase_savings':before-cost if created and count>1 else 0,'rebase_count':max(0,count-1),
            'prefix_cache_loss_estimate':None,'actual_cache_read_tokens':None,'estimate_only':True}

    def assert_current(self,run_id,token):
        current=self.fence(run_id)
        if {k:v for k,v in current.items() if k!='revision'}!={k:v for k,v in token.get('fence',{}).items() if k!='revision'}:raise RepoError('EPOCH_STALE')
        self.replay(run_id,self.live.snapshot(run_id))

    def restore(self,run_id,epoch_id):
        rows=self.live.snapshot(run_id);_,value=self.replay(run_id,rows,epoch_id)
        return json.loads(self.live._reference(run_id,value['body']['archive_ref']))['messages']
