import copy,json
import pytest
from repo_code.providers import ScriptedProvider
from repo_code.util import RepoError
from test_procedure_promotion import fixture,Runner

@pytest.mark.asyncio
async def test_compiler_receives_public_write_boundaries_and_alignment_not_private_oracle(tmp_path):
    app,draft,task,probes,plan,review=await fixture(tmp_path)
    try:
        task=copy.deepcopy(task);task['contract']['protected_paths']=['tests/public.py']
        task['contract']['checks'][0]['definition']['expected']='PRIVATE_ORACLE_SENTINEL'
        seen=[]
        def capture(system,messages,tools,count):
            payload=json.loads(messages[0]['content']);seen.append(payload)
            assert 'exactly one step_rule' in system and 'public_support_path_policy' in system
            assert 'Binding preparation is a prerequisite' in system
            assert 'after binding' in payload['binding_execution_contract']['application']
            assert 'no retroactive application credit' in payload['binding_execution_contract']['matching']
            assert 'verification argv' in payload['binding_execution_contract']['verification']
            assert payload['existing_host_obligations']['planning_policy']
            assert 'Test authoring may use existing tests, new files or inline assertions' in system
            assert payload['public_support_path_policy'][task['id']]['protected_paths']==['tests/public.py']
            assert set(payload['public_support_path_policy'][task['id']])=={'allowed_paths','forbidden_paths','protected_paths'}
            assert 'PRIVATE_ORACLE_SENTINEL' not in json.dumps(payload)
            raise RepoError('CAPTURE_ONLY')
        result=await app.procedure_promotion(Runner()).run(draft['id'],ScriptedProvider(handler=capture),ScriptedProvider(),
            support_tasks={task['id']:task},probes=probes)
        assert seen and result['status']=='BLOCKED' and result['reason']=='CAPTURE_ONLY'
        assert not app.store.values('session_grant')
    finally:app.close()
