"""Trusted, narrow transfer of active Host observations between fresh states.

No Host keys, live operations, workspaces, conversation histories, model
proposals, official verdicts, or grading artifacts are transferred.
"""
import json
from pathlib import Path
from repo_code.util import RepoError,digest,canonical
from repo_code.governance import Lineage
from repo_code.memory.models import ClaimRecord


def export_observations(app,run_id):
    run=app.runtime.run(run_id);scope=run['data']['manifest']['scope']
    if run['data']['visibility']!='DEV' or not app.memory.profile.learning_enabled:
        raise RepoError('LEARNING_TRANSFER_SOURCE_DENIED')
    if app.runtime.unresolved(run_id) or not app.sandbox.resources_stopped(run_id):
        raise RepoError('LEARNING_TRANSFER_SOURCE_NOT_STOPPED')
    claims=[];events={};operations={};origins={};edges=set()
    with app.store.connect() as c:
        for row in app.memory.repo.values('memory_versions'):
            head=app.memory.repo.get('memory_control','claim:'+row['claim_id'])
            if head!={'version':row['version'],'lifecycle':'ACTIVE'}:continue
            if row['scope_id']!=scope or row['kind'] not in {'EPISODE','FACT'}:continue
            refs=row['support_refs']
            if not refs or any(r['origin_class']!='HOST_OBSERVATION' or r['source_kind'] not in {'verification','file_observation'} for r in refs):continue
            roots=[row['claim_id'],*row['derived_from']]
            app.memory.repo.ensure_dev(roots)
            claims.append(row)
            closure=set(roots)
            for root in roots:closure.update(Lineage(app.store).ancestors(root,c))
            for child,parent in c.execute('SELECT child,parent FROM lineage'):
                if child in closure:edges.add((child,parent))
            for ref in refs:
                origin=app.runtime.run(ref['run_id'])
                if origin['data']['visibility']!='DEV' or origin['data']['manifest']['scope']!=scope:
                    raise RepoError('LEARNING_TRANSFER_SCOPE_DENIED')
                # Only a terminal origin descriptor, needed to validate evidence.
                origins[origin['id']]={k:origin[k] for k in ['id','task_id','state','revision','control_epoch','writer_epoch','cancel_requested']}
                origins[origin['id']]['data']={'manifest':{'scope':scope},'visibility':'DEV',
                    'created':origin['data']['created'],'readonly_memory_origin':True}
                if ref['source_kind']=='verification':
                    event=c.execute('SELECT * FROM events WHERE event_id=? AND run_id=?',(ref['source_id'],origin['id'])).fetchone()
                    if event is None or event['type'] not in {'run.ended','memory.stage_observed'}:
                        raise RepoError('LEARNING_TRANSFER_EVENT_DENIED')
                    event=dict(event);event['data']=json.loads(event['data'])
                    if digest(event['data'])!=ref['content_digest']:raise RepoError('LEARNING_TRANSFER_EVIDENCE_MISMATCH')
                    events[event['event_id']]=event
                else:
                    op=c.execute('SELECT * FROM operations WHERE id=? AND run_id=?',(ref['source_id'],origin['id'])).fetchone()
                    op=dict(op) if op else None
                    if not op or op['state']!='SUCCEEDED' or json.loads(op['request'])['tool_name']!='read':
                        raise RepoError('LEARNING_TRANSFER_OPERATION_DENIED')
                    if digest(json.loads(op['result']))!=ref['content_digest']:raise RepoError('LEARNING_TRANSFER_EVIDENCE_MISMATCH')
                    operations[op['id']]=op
    return {'schema':1,'scope':scope,'source_run_id':run_id,'claims':claims,
        'origins':list(origins.values()),'events':list(events.values()),'read_operations':list(operations.values()),
        'lineage':sorted(edges),'source_revocation_epoch':app.memory.repo.epoch(),
        'exclusions':['host.key','workspace','messages','leases','model_proposals','gold','grading_logs']}


def import_observations(app,capsule,expected_digest,scope):
    if digest(capsule)!=expected_digest or capsule.get('schema')!=1 or capsule.get('scope')!=scope:
        raise RepoError('LEARNING_TRANSFER_DIGEST_OR_SCOPE')
    if app.store.values('learning_transfer'):raise RepoError('LEARNING_TRANSFER_ALREADY_IMPORTED')
    with app.store.connect() as c:
        if c.execute('SELECT COUNT(*) FROM runs').fetchone()[0]:raise RepoError('LEARNING_TRANSFER_REQUIRES_FRESH_STATE')
    origins={r['id']:r for r in capsule['origins']};events={e['event_id']:e for e in capsule['events']}
    operations={o['id']:o for o in capsule['read_operations']}
    for row in capsule['claims']:
        ClaimRecord.model_validate(row)
        if row['lifecycle']!='ACTIVE' or row['scope_id']!=scope or row['kind'] not in {'EPISODE','FACT'}:
            raise RepoError('LEARNING_TRANSFER_CLAIM_DENIED')
        for ref in row['support_refs']:
            if ref['scope_id']!=scope or ref['visibility_domain']!='DEV' or ref['origin_class']!='HOST_OBSERVATION':
                raise RepoError('LEARNING_TRANSFER_EVIDENCE_DENIED')
            origin=origins[ref['run_id']]
            if origin['state'] not in {'SUCCEEDED','FAILED','PARTIAL','BLOCKED','CANCELLED'}:
                raise RepoError('LEARNING_TRANSFER_ACTIVE_ORIGIN')
            if ref['source_kind']=='verification':value=events[ref['source_id']]['data']
            elif ref['source_kind']=='file_observation':
                op=operations[ref['source_id']]
                if op['state']!='SUCCEEDED' or json.loads(op['request'])['tool_name']!='read':raise RepoError('LEARNING_TRANSFER_OPERATION_DENIED')
                value=json.loads(op['result'])
            else:raise RepoError('LEARNING_TRANSFER_EVIDENCE_DENIED')
            if digest(value)!=ref['content_digest']:raise RepoError('LEARNING_TRANSFER_EVIDENCE_MISMATCH')
    with app.store.tx() as c:
        for row in capsule['origins']:
            c.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',tuple(row[k] for k in ['id','task_id','state','revision','control_epoch','writer_epoch','cancel_requested'])+(canonical(row['data']).decode(),))
        for event in capsule['events']:
            c.execute('INSERT INTO events(event_id,run_id,type,visibility,data,created) VALUES(?,?,?,?,?,?)',
                (event['event_id'],event['run_id'],event['type'],'DEV',canonical(event['data']).decode(),event['created']))
        for op in capsule['read_operations']:
            c.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)',tuple(op[k] for k in ['id','op_key','run_id','state','semantics','request','result','approved_epoch','created']))
        for row in capsule['claims']:
            key=row['claim_id']+':'+str(row['version'])
            app.memory.repo.put('memory_versions',key,row,c)
            app.memory.repo.put('memory_control','claim:'+row['claim_id'],{'version':row['version'],'lifecycle':'ACTIVE'},c)
            app.memory.repo.derive('memory-version:'+key,[row['claim_id'],*row['derived_from']],c)
        for child,parent in capsule['lineage']:app.memory.repo.derive(child,[parent],c)
        app.store.put('learning_transfer','import',{'digest':expected_digest,'source_run_id':capsule['source_run_id'],
            'claims':len(capsule['claims']),'scope':scope,'previous_messages_imported':0,'host_key_imported':False},c)
    return len(capsule['claims'])
