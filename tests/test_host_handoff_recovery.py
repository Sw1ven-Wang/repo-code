"""Regression coverage for the Host's bounded pre-handoff recovery."""
import json
import subprocess
import sys

import pytest

from repo_code.context import groups
from repo_code.providers import ScriptedProvider
from test_candidate_review import action
from test_review_workflow import setup


@pytest.mark.asyncio
@pytest.mark.parametrize('declared', [True, False])
async def test_zero_diff_write_gets_a_recovery_turn(tmp_path, declared):
    script = ['python', '-c',
              "from pathlib import Path; p=Path('a.py'); assert p.exists(); # replace source"
              r"\np.write_text('value=1')"]
    replies = []
    if not declared:
        replies.extend([action('read', 'read', path='a.py', lines=3),
                        action('ambiguous', 'edit', path='a.py', old_text='value=0', new_text='value=1')])
    replies.extend([action('write', 'process.run', argv=script, expect_workspace_change=declared),
                    {'text': 'The command exited zero but the patch is empty.'},
                    action('fix', 'create', path='fixed.py', content='value=1\n'),
                    action('check', 'process.run', argv=['pytest', '-k', 'focused']),
                    {'text': 'The candidate has a passing check.'}])
    prompts = []

    def respond(system, messages, tools, index):
        prompts.append(system)
        return replies[index]

    app, run = setup(tmp_path, provider=ScriptedProvider(handler=respond))
    original = app.sandbox.run

    async def execute_script(rid, folder, argv, *args, **kwargs):
        result = await original(rid, folder, argv, *args, **kwargs)
        if argv == script or '__REPO_CODE_CHECK_COMPLETED_' in str(argv):
            proc = subprocess.run([sys.executable, *argv[1:]], cwd=folder, capture_output=True, text=True, check=True)
            result.update(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)
            assert (folder / 'a.py').read_text() == 'value=0\n' * 120
        return result

    app.sandbox.run = execute_script
    try:
        result = await app.execute(run['id'])
        assert result['candidate_review']['status'] == 'NEW_CHECK_PASSED'
        assert result['state'] == 'PARTIAL' and result['delivery'] is None
        assert 'no net changes after a write attempt' in prompts[-3]
        assert ('WORKSPACE_WRITE_NO_EFFECT' if declared else 'AMBIGUOUS_OR_MISSING_MATCH') in prompts[-3]
        messages = app.store.messages(run['id'])
        groups(messages)
        receipt = next(json.loads(msg['content']) for msg in messages if msg.get('tool_call_id') == 'write')
        assert receipt['exit_code'] == 0
        assert receipt['is_error'] is declared
        assert app.provider.calls == len(replies)
    finally:
        app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('noise', ['read', 'suite'])
async def test_reverted_draft_gets_only_one_recovery_turn(tmp_path, noise):
    before, after = 'value=0\n' * 120, 'value=1\n' * 120
    replies = [action('read', 'read', path='a.py', lines=120),
               action('draft', 'edit', path='a.py', old_text=before, new_text=after),
               action('revert', 'edit', path='a.py', old_text=after, new_text=before),
               {'text': 'No implementation ready.'},
               (action('noise', 'read', path='a.py', start=5, lines=1) if noise == 'read'
                else action('noise', 'process.run', argv=['pytest'])),
               {'text': 'Still no implementation ready.'}]
    app, run = setup(tmp_path, provider=ScriptedProvider(replies))
    try:
        result = await app.execute(run['id'])
        assert result['candidate_review']['status'] == 'NO_SOURCE_CHANGE'
        assert result['state'] == 'PARTIAL' and result['delivery'] is None
        assert app.provider.calls == len(replies)
        assert app.runtime.workspace(run).path('a.py').read_text() == before
    finally:
        app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('apply_fix', [True, False])
async def test_reverted_draft_hands_off_focused_reproduction_once(tmp_path, apply_fix):
    before, wrong, fixed = ('value=0\n' * 120, 'value=-1\n' * 120, 'value=1\n' * 120)
    check = ['python', '-c', 'from a import value; assert value == 1']
    suite = ['pytest', '-q', 'tests']
    replies = [action('read', 'read', path='a.py', lines=120),
               action('draft', 'edit', path='a.py', old_text=before, new_text=wrong),
               action('draft-check', 'process.run', argv=check),
               action('revert', 'edit', path='a.py', old_text=wrong, new_text=before),
               action('suite', 'process.run', argv=suite),
               {'text': 'No implementation ready.'},
               action('reproduce', 'process.run', argv=check),
               {'text': 'I reproduced the discrepancy.'}]
    if apply_fix:
        replies.extend([action('fix', 'edit', path='a.py', old_text=before, new_text=fixed),
                        action('recheck', 'process.run', argv=check)])
    replies.append({'text': 'Handoff.'})
    prompts = []

    def respond(system, messages, tools, index):
        prompts.append(system)
        return replies[index]

    app, run = setup(tmp_path, provider=ScriptedProvider(handler=respond))
    original = app.sandbox.run

    async def run_check(rid, folder, argv, *args, **kwargs):
        result = await original(rid, folder, argv, *args, **kwargs)
        if argv == check or '__REPO_CODE_CHECK_COMPLETED_' in str(argv):
            proc = subprocess.run([sys.executable, '-B', *argv[1:]], cwd=folder, capture_output=True, text=True)
            result.update(exit_code=proc.returncode, stderr=proc.stderr, stdout=proc.stdout)
        elif argv == suite:
            result.update(exit_code=1, stdout='2 failed\n')
        return result

    app.sandbox.run = run_check
    try:
        result = await app.execute(run['id'])
        review = result['candidate_review']
        assert 'branch that input actually reaches before editing' in prompts[0]
        assert 'smallest concrete example' in prompts[6]
        assert '"attempted_check_failure"' in prompts[6] and '"baseline_check_failure"' in prompts[6]
        assert 'Baseline reproduction (tool data)' in prompts[8]
        assert app.provider.calls == len(replies)
        assert review['status'] == ('NEW_CHECK_PASSED' if apply_fix else 'NO_SOURCE_CHANGE')
        assert bool(review['repaired_checks']) is apply_fix
        assert app.runtime.workspace(run).path('a.py').read_text() == (fixed if apply_fix else before)
    finally:
        app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['fixed', 'long_fixed', 'unrelated', 'stale'])
async def test_failed_check_requires_same_check_on_final_source(tmp_path, case):
    check = ['python', '-c', "from pathlib import Path; assert Path('fixed.py').read_text().strip() == 'value=1'"]
    if case=='long_fixed':check[-1]="print(('log-line '*1900+'MIDDLE_LOCAL_OBSERVATION')*2+'log-line '*700); "+check[-1]
    replies = [action('draft', 'create', path='fixed.py', content='value=0\n'),
               action('failed-check', 'process.run', argv=check)]
    if case == 'stale':
        replies.append(action('unrelated-write', 'create', path='adjacent.py', content='marker=True\n'))
    replies.append({'text': 'The candidate is incomplete.'})
    first_review = len(replies)
    if case in {'fixed','long_fixed'}:
        replies.extend([action('repair', 'edit', path='fixed.py', old_text='value=0', new_text='value=1'),
                        action('recheck', 'process.run', argv=check)])
    elif case == 'unrelated':
        replies.append(action('wrong-check', 'process.run', argv=['pytest', '-k', 'unrelated']))
    else:
        replies.append(action('wrong-check', 'process.run', argv=['pytest', '-k', 'also_unrelated']))
    replies.append({'text': 'Handoff.'})
    prompts = []

    def respond(system, messages, tools, index):
        prompts.append(system)
        return replies[index]

    app, run = setup(tmp_path, provider=ScriptedProvider(handler=respond))
    original = app.sandbox.run

    async def run_check(rid, folder, argv, *args, **kwargs):
        result = await original(rid, folder, argv, *args, **kwargs)
        if argv == check or '__REPO_CODE_CHECK_COMPLETED_' in str(argv):
            proc = subprocess.run([sys.executable, *argv[1:]], cwd=folder, capture_output=True, text=True)
            result.update(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)
        return result

    app.sandbox.run = run_check
    try:
        result = await app.execute(run['id'])
        state = result['candidate_review']
        expected = {'fixed': 'NEW_CHECK_PASSED','long_fixed':'NEW_CHECK_PASSED', 'unrelated': 'CHECK_FAILED', 'stale': 'RECHECK_PENDING'}
        assert state['status'] == expected[case]
        assert 'Focused failed check' in prompts[first_review]
        assert json.dumps(check) in prompts[first_review]
        assert bool(state['repaired_checks']) == (case in {'fixed','long_fixed'})
        assert bool(state['local_repair_pairs']) == (case in {'fixed','long_fixed'})
        if case in {'fixed','long_fixed'}:
            pair,=state['local_repair_pairs']
            assert pair['failed']!=pair['passed'] and pair['before']!=pair['after']
            from repo_code.verified_stages import VerifiedStages
            stages=VerifiedStages(app.runtime)
            row,=[p for x in app.store.values(stages.KIND) if (p:=stages.payload(x)).get('local_repair_pairs')]
            assert pair['failed'] in row['refs'] and pair['passed'] in row['refs']
            if case=='long_fixed':
                from repo_code.memory.evidence_pipeline import EvidencePipeline
                from repo_code.memory.consolidation import Consolidator
                from repo_code.memory.repository import MemoryRepository
                from repo_code.memory.models import MemoryProfile
                owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
                pipe=EvidencePipeline(owner);eid,=stages.materialize(row['id'])
                meta,records,_=pipe.build(eid);chain,=meta.verification_summary['local_check_chains']
                assert chain['check_digest']==pair['check'] and chain['authority']=='LOCAL_SAME_CHECK_ONLY'
                sid=chain['passed'];assert len(records[sid]['fields']['stdout_excerpt'])>24000
                selected,report=pipe.select(records,1024,[chain])
                assert {chain[k] for k in ('failed','change','passed')}<=set(report['selected_ids'])
                from repo_code.util import canonical,RepoError
                assert len(canonical(selected))<=1024
                req={'evidence_id':sid,'source_digest':records[sid]['metadata']['content_digest'],
                     'selector':'stdout_excerpt','offset':30000,'max_bytes':64}
                _,page=pipe.expand(eid,records,req,'test')
                assert page['content']==records[sid]['fields']['stdout_excerpt'][30000:30064]
                # Curator sees a small window, can obtain the middle, and cannot cite an unseen neighbor.
                from test_curator_evidence_pipeline import provider
                app.store.put('operator_config','curator_evidence_pipeline',{'enabled':True,'input_evidence_bytes':1024})
                marker='MIDDLE_LOCAL_OBSERVATION'
                middle=records[sid]['fields']['stdout_excerpt'].rindex(marker)
                def answer(v,i):
                    assert len(canonical(v['metadata']['evidence_catalogue']))<=1024
                    assert v['metadata']['verification_summary']['local_check_chains']==[chain]
                    visible=[s['selectors'].get('stdout_excerpt','') for s in v['evidence'] if s['evidence_id']==sid]
                    if not i:
                        assert not any(marker in text for text in visible)
                        return {'status':'NEEDS_EVIDENCE','claims':[],'evidence_requests':[dict(req,offset=middle,max_bytes=len(marker))]}
                    assert marker in visible
                    def observation(quote):
                        return {'category':'OBSERVATION','text':quote,'evidence_refs':[{
                            'evidence_id':sid,'selector':'stdout_excerpt','source_digest':req['source_digest'],'quote':quote,'relation':'supports'}]}
                    return {'status':'INCOMPLETE','claims':[observation(marker),observation(marker+'log-line')]}
                p=provider(answer);report=await owner.curate_episode(eid,p)
                assert report['status']=='INCOMPLETE' and p.calls==2
                assert [x['action'] for x in report['receipts']]==['ADD','REJECT']
                assert report['receipts'][1]['reason']=='CURATOR_QUOTE_UNSUPPORTED'
                lesson=app.store.get('lesson',report['receipts'][0]['id'])
                assert lesson['evidence_refs'][0]['match_start']==middle
                # A DB row alone cannot override the original event / stage signature.
                with app.store.tx() as c:
                    saved=c.execute('SELECT result FROM operations WHERE id=?',(pair['passed'],)).fetchone()[0]
                    c.execute('UPDATE operations SET result=? WHERE id=?',('{}',pair['passed']))
                with pytest.raises(RepoError,match='L1_STAGE_SOURCE_CHANGED'):pipe.build(eid)
                with app.store.tx() as c:c.execute('UPDATE operations SET result=? WHERE id=?',(saved,pair['passed']))
                owner.repo.revoke(chain['change'])
                with pytest.raises(RepoError):pipe.expand(eid,records,req,'test')
        assert app.provider.calls == len(replies)
        assert result['state'] == 'PARTIAL' and result['delivery'] is None
    finally:
        app.close()


@pytest.mark.asyncio
async def test_unrelated_pass_does_not_repeat_pending_probe_request(tmp_path):
    replies = [action('draft', 'create', path='fixed.py', content='value=1\n'),
               action('observe', 'process.run', argv=['python', '-c', 'from pkg import f\nprint(f())']),
               {'text': 'Candidate ready.'},
               action('unrelated', 'process.run', argv=['pytest', '-k', 'unrelated']),
               {'text': 'The same probe has still not been checked.'}]
    app, run = setup(tmp_path, provider=ScriptedProvider(replies))
    try:
        result = await app.execute(run['id'])
        assert result['candidate_review']['status'] == 'NO_NEW_EVIDENCE'
        assert len(result['candidate_review']['pending_probes']) == 1
        assert app.provider.calls == len(replies)
    finally:
        app.close()
