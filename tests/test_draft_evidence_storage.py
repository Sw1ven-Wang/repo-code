"""Offline evidence contracts, not model efficacy or publication evidence."""
import copy,json
import pytest
from repo_code.episodes import EpisodeBuilder
from repo_code.memory.evidence_pipeline import EvidencePipeline,inline_assertions,project_test_link,complete_diff_view
from repo_code.memory.repository import MemoryRepository
from repo_code.memory.consolidation import Consolidator
from repo_code.memory.models import MemoryProfile
from repo_code.util import digest,RepoError
from test_curator_evidence_pipeline import prepared,provider,emit,procedure
from test_procedure_draft_p0 import draft
from test_review_workflow import setup


@pytest.mark.asyncio
async def test_draft_survives_missing_boundary_without_becoming_skill(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path,boundary=False)
    try:
        active=app.releases.active()
        p=provider(lambda payload,i:{'status':'COMPLETE','claims':[draft(payload)]})
        result=await owner.curate_episode(eid,p)
        assert result['status']=='INCOMPLETE' and result['stop_reason']=='MISSING_CAUSAL_ROLES'
        assert result['receipts'][0]['action']=='ADD' and p.calls==1
        saved=app.store.values('procedure_draft')
        assert len(saved)==1 and saved[0]['runtime_eligible'] is False
        assert not app.store.values('skill_candidate') and not app.store.values('session_grant')
        assert app.releases.active()==active
        assert await owner.curate_episode(eid,p)==result and p.calls==1
    finally:app.close()


@pytest.mark.asyncio
async def test_full_procedure_keeps_completeness_gate(tmp_path):
    app,run,old,eid,owner,ids=await prepared(tmp_path,boundary=False)
    try:
        result=await owner.curate_episode(eid,provider(lambda p,i:{'status':'COMPLETE','claims':[procedure(p)]}))
        assert result['status']=='INCOMPLETE'
        assert not app.store.values('procedure_draft') and not app.store.values('skill_candidate')
    finally:app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['quote','implementation','scope','revoked'])
async def test_incomplete_draft_still_checks_sources_and_scope(tmp_path,bad):
    app,run,old,eid,owner,ids=await prepared(tmp_path,boundary=False)
    try:
        def answer(p,i):
            claim=draft(p)
            if bad=='quote':claim['evidence_refs'][0]['quote']='invented source content'
            if bad=='implementation':claim['evidence_refs']=[r for r in claim['evidence_refs'] if r['relation']!='implements']
            if bad=='scope':claim['procedure_draft']['applicability']['scope']='global'
            if bad=='revoked':owner.repo.revoke(ids['change'])
            return {'status':'COMPLETE','claims':[claim]}
        try:result=await owner.curate_episode(eid,provider(answer))
        except RepoError:
            assert bad=='revoked'
        else:assert any(x['action']=='REJECT' for x in result['receipts'])
        assert not app.store.values('procedure_draft')
    finally:app.close()


@pytest.mark.parametrize('argv,stdin,expected',[
    (['python','-c','from demo import normalize; assert normalize(0)==1'],'',True),
    (['python','-'],'assert 0 == 0\n',True),
    (['python','-c','print("assert False")'],'',False),
    (['python','-c','# assert False\nprint(1)'],'',False),
    (['python','-O','-c','assert False'],'',False),
    (['echo','assert False'],'',False),
    (['python','-c','assert ('],'',False),
])
def test_inline_assertion_is_syntax_not_substring_or_execution_proof(argv,stdin,expected):
    assert inline_assertions({'argv':argv,'stdin':stdin}) is expected


@pytest.mark.asyncio
async def test_patch_batch_and_existing_tests_are_citable_and_pytest_is_linked(tmp_path):
    app,run=setup(tmp_path)
    try:
        emit(app,run,'read',{'path':'worker.py'},{'content':'1: def aggregate(rows): raise NotImplementedError()'})
        failed=emit(app,run,'process.run',{'argv':['python','-m','pytest','-q']},{'exit_code':1,'stdout':'1 failed\n'})
        ops=[{'kind':'replace','path':'worker.py','content':'def aggregate(rows):\n    return sum(rows)\n'},
             {'kind':'edit','path':'tests/test_worker.py','replacements':[{'old_text':'pass','new_text':'assert aggregate([]) == 0'}]}]
        change=emit(app,run,'patch',{'operations':ops},{'changed_paths':['worker.py','tests/test_worker.py']})
        good=emit(app,run,'process.run',{'argv':['python','-m','pytest','-q']},{'exit_code':0,'stdout':'2 passed\n'})
        inline=emit(app,run,'process.run',{'argv':['python','-c','from worker import aggregate; assert aggregate([])==0']},{'exit_code':0,'stdout':'boundary passed'})
        rejected=emit(app,run,'patch',{'operations':ops},{'is_error':True,'code':'READ_REQUIRED'})
        app.runtime.end(run['id'],'PARTIAL','offline batch fixture')
        old,=EpisodeBuilder(app.store).capture_run(run['id'],pipeline='legacy')
        eid=EpisodeBuilder(app.store).capture_causal(old)
        owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
        meta,records,ref=EvidencePipeline(owner).build(eid)
        assert 'ORIGINAL_FAILURE' in records[failed]['metadata']['role']
        assert 'POSITIVE_VERIFICATION' in records[good]['metadata']['role']
        assert 'BOUNDARY_COUNTEREXAMPLE' in records[inline]['metadata']['role']
        assert 'BOUNDARY_COUNTEREXAMPLE' in records[change]['metadata']['role']
        assert 'IMPLEMENTATION_CHANGE' not in records[rejected]['metadata']['role']
        assert 'return sum(rows)' in records[change]['fields']['new_hunk']
        assert 'aggregate' in records[change]['definitions']
        assert json.loads(records[change]['fields']['patch_operations'])==ops
        # Exact quotation of the recorded batch is accepted for an unverified Draft.
        app.store.put('operator_config','curator_evidence_pipeline',{'enabled':True,'max_expansions':0})
        def answer(p,i):
            d=draft_template(p)
            return {'status':'COMPLETE','claims':[d]}
        result=await owner.curate_episode(eid,provider(answer))
        assert result['receipts'][0]['action']=='ADD',result
        assert app.store.values('procedure_draft')[0]['runtime_eligible'] is False
    finally:app.close()


def draft_template(payload):
    change=next(s for s in payload['evidence'] if 'IMPLEMENTATION_CHANGE' in s['roles'] and 'new_hunk' in s['selectors'])
    diagnostic=next(s for s in payload['evidence'] if 'ROOT_CAUSE_DIAGNOSIS' in s['roles'] and 'read_content' in s['selectors'])
    ref=lambda s,k,relation:{'evidence_id':s['evidence_id'],'source_digest':s['source_digest'],
        'selector':k,'quote':s['selectors'][k].splitlines()[0],'relation':relation}
    return {'category':'PROCEDURE_DRAFT','text':'Normalize records before aggregation','subject':'aggregation order',
        'evidence_refs':[ref(diagnostic,'read_content','supports'),ref(change,'new_hunk','implements')],
        'procedure_draft':{'problem_pattern':'A local aggregation repair',
            'applicability':{'scope':payload['draft_context']['scope'],'task_family':payload['draft_context']['task_family'],
                'trigger_hypotheses':['Aggregation input validation is incomplete'],'exclusion_hypotheses':[],
                'unknown_boundaries':['Only local source evidence is available'],'status':'UNVERIFIED'},
            'steps':['Inspect the normalization and aggregation boundary'],
            'positive_probe_proposals':[],'negative_probe_proposals':[],'boundary_probe_proposals':[],
            'status':'LOCAL_PROCEDURE_DRAFT'}}


@pytest.mark.parametrize('case',['complete','truncated','wrong_baseline','missing_source','offset'])
def test_only_complete_source_bound_diff_is_implementation_view(case):
    baseline=digest('baseline');body={'diff':'+return 1','changed_paths':['worker.py'],
        'baseline':baseline,'truncated':False,'offset':0,'next_offset':None,
        'inspection_progress':{'source_digest':digest('current')}}
    if case=='truncated':body['truncated']=True
    if case=='wrong_baseline':body['baseline']=digest('other')
    if case=='missing_source':body['inspection_progress']={}
    if case=='offset':body['offset']=4
    assert bool(complete_diff_view(body,{'worker.py'},baseline)) is (case=='complete')


def test_unrelated_or_external_test_target_is_not_project_association():
    changed={'worker.py','tests/test_worker.py'}
    assert project_test_link({'argv':['python','-m','pytest','-q']},changed)
    assert project_test_link({'argv':['pytest','tests/test_worker.py::test_zero']},changed)
    assert not project_test_link({'argv':['pytest','elsewhere/test_other.py']},changed)
    assert not project_test_link({'argv':['pytest','../private/test_worker.py']},changed)


@pytest.mark.asyncio
async def test_failed_create_cannot_be_cited_as_implemented_even_with_other_valid_write(tmp_path):
    app,run=setup(tmp_path)
    try:
        emit(app,run,'read',{'path':'worker.py'},{'content':'1: def aggregate(rows): return -1'})
        emit(app,run,'edit',{'path':'worker.py','old_text':'return -1','new_text':'return sum(rows)'},{'changed_paths':['worker.py']})
        bad=emit(app,run,'create',{'path':'worker.py','content':'def aggregate(rows): return sum(rows)'},
            {'code':'FILE_EXISTS','is_error':True})
        app.runtime.end(run['id'],'PARTIAL','failed attempted mutation fixture')
        old,=EpisodeBuilder(app.store).capture_run(run['id'],pipeline='legacy')
        eid=EpisodeBuilder(app.store).capture_causal(old)
        app.store.put('operator_config','curator_evidence_pipeline',{'enabled':True,'max_expansions':0})
        owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
        def answer(p,i):
            claim=draft_template(p)
            source=next(s for s in p['evidence'] if s['evidence_id']==bad)
            claim['evidence_refs'].append({'evidence_id':bad,'source_digest':source['source_digest'],
                'selector':'created_content','quote':source['selectors']['created_content'],'relation':'implements'})
            return {'status':'COMPLETE','claims':[claim]}
        result=await owner.curate_episode(eid,provider(answer))
        assert result['receipts'][0]['reason']=='CLAIM_RELATION_UNSUPPORTED'
        assert not app.store.values('procedure_draft')
    finally:app.close()
