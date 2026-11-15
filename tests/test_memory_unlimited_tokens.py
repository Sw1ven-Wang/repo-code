"""No Token cap must retain full reservations, including unknown usage."""
import pytest
from repo_code.memory.models import MemoryProfile
from repo_code.memory.repository import MemoryRepository
from repo_code.storage import Store
from repo_code.util import RepoError


def test_explicit_no_token_cap_preserves_settlement_and_unknown_usage(tmp_path):
    profile=MemoryProfile(mode='lexical_only',total_token_budget=None,auxiliary_calls=None)
    assert MemoryProfile.model_validate_json(profile.model_dump_json()).total_token_budget is None
    repo=MemoryRepository(Store(tmp_path/'state'))
    a=repo.reserve('run','main',600000,4096,None,profile.total_token_budget)
    repo.settle(a,{'prompt_tokens':600000,'completion_tokens':2000},0.1)
    b=repo.reserve('run','consolidation',900000,4096,None,None)
    repo.settle(b,{},0.2)
    rows=repo.values('memory_budget')
    assert len(rows)==2
    assert repo.get('memory_budget',a)['charged_tokens']==602000
    assert repo.get('memory_budget',b)['state']=='USAGE_UNKNOWN'
    assert repo.get('memory_budget',b)['charged_tokens']==904096


def test_finite_default_still_enforces_token_budget(tmp_path):
    profile=MemoryProfile(mode='lexical_only')
    assert profile.total_token_budget==500000
    repo=MemoryRepository(Store(tmp_path/'state'))
    a=repo.reserve('run','main',490000,4096,None,profile.total_token_budget)
    with pytest.raises(RepoError,match='MEMORY_TOKEN_BUDGET'):
        repo.reserve('run','main',10000,4096,None,profile.total_token_budget)
    assert len(repo.values('memory_budget'))==1
