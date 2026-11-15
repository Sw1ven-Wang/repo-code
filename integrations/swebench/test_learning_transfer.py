import copy
import pytest
from pathlib import Path
from repo_code.service import RepoCode
from repo_code.providers import ScriptedProvider
from repo_code.memory.models import MemoryProfile
from repo_code.util import RepoError,digest
from learning_transfer import export_observations,import_observations

SCOPE='eval:cross-task-fixture'


def service(path):
    return RepoCode(path,ScriptedProvider(),'local',memory_profile=MemoryProfile(
        mode='lexical_only',learning_enabled=True,auto_activate_observations=True,semantic_consolidation=False))


@pytest.mark.asyncio
async def test_only_host_observation_and_evidence_move_into_fresh_state(tmp_path):
    source=tmp_path/'input';source.mkdir();(source/'private-workspace.txt').write_text('not transferred')
    app=service(tmp_path/'one')
    run=app.runtime.create('Public training task',source,app.releases.active()['digest'],scope=SCOPE)
    app.runtime.end(run['id'],'PARTIAL','PUBLIC_FIXTURE_STOP')
    await app.memory.on_terminal(run['id'])
    capsule=export_observations(app,run['id'])
    assert len(capsule['claims'])==1 and not capsule['read_operations']
    other=service(tmp_path/'two')
    key_before=(other.store.root/'host.key').read_bytes()
    assert import_observations(other,capsule,digest(capsule),SCOPE)==1
    assert (other.store.root/'host.key').read_bytes()==key_before
    assert key_before!=(app.store.root/'host.key').read_bytes()
    assert not list(other.store.root.rglob('private-workspace.txt'))
    assert other.store.messages(run['id'])==[]
    new_source=tmp_path/'new-input';new_source.mkdir()
    new=other.runtime.create('Task outcome observation',new_source,other.releases.active()['digest'],scope=SCOPE)
    source_pin=other.store.read_json(new['data']['manifest']['knowledge_snapshot_digest'])
    assert len(source_pin['items'])==1
    ref=capsule['claims'][0]['support_refs'][0]
    evidence=other.memory.history.expand(new['id'],ref['source_id'])
    assert evidence['historical'] and 'PUBLIC_FIXTURE_STOP' in evidence['content']
    assert not evidence['grants_edit_authority']
    with pytest.raises(RepoError):import_observations(other,capsule,digest(capsule),SCOPE)
    app.close();other.close()


@pytest.mark.asyncio
async def test_transfer_refuses_tampering_hidden_and_wrong_scope(tmp_path):
    source=tmp_path/'input';source.mkdir()
    app=service(tmp_path/'one')
    run=app.runtime.create('Public task',source,app.releases.active()['digest'],scope=SCOPE)
    app.runtime.end(run['id'],'PARTIAL','FIXTURE');await app.memory.on_terminal(run['id'])
    capsule=export_observations(app,run['id'])
    other=service(tmp_path/'two')
    bad=copy.deepcopy(capsule);bad['claims'][0]['text']='tampered'
    with pytest.raises(RepoError):import_observations(other,bad,digest(capsule),SCOPE)
    with pytest.raises(RepoError):import_observations(other,capsule,digest(capsule),'another-scope')
    app.memory.repo.revoke(run['id'])
    with pytest.raises(RepoError):export_observations(app,run['id'])
    app.close();other.close()
