import subprocess
import pytest
import yaml
from adapter import patch_from_workspace
from regression_fixtures import RuleFixtureAddition
from test_adapter import baseline,git
from repo_code.util import RepoError

NAME='test/fixtures/rules/std_rule_cases/L031.yml'
BEFORE=b'rule: L031\nold_case:\n  pass_str: SELECT a\n'
ADDITION=b'\nnew_case:\n  fail_str: SELECT a.x FROM x AS a\n  fix_str: SELECT x.x FROM x\n  configs:\n    core:\n      dialect: tsql\n'


def test_append_only_fixture_is_preserved_in_complete_patch(tmp_path):
    base,source,work=baseline(tmp_path)
    for root in (base,source,work):
        p=root/NAME;p.parent.mkdir(parents=True);p.write_bytes(BEFORE)
    git(base,'add','.');git(base,'-c','user.name=fixture','-c','user.email=fixture@localhost','commit','-m','fixture')
    (work/NAME).write_bytes(BEFORE+ADDITION);(work/'change.py').write_text('after\n')
    proof=RuleFixtureAddition.prove(NAME,BEFORE,BEFORE+ADDITION)
    patch,changed=patch_from_workspace(base,source,work,tmp_path/'replay',regression_proofs={NAME:proof})
    assert changed==['change.py',NAME]
    subprocess.run(['git','apply','-'],cwd=base,input=patch,check=True)
    assert (base/NAME).read_bytes()==BEFORE+ADDITION
    assert (base/'change.py').read_text()=='after\n'


@pytest.mark.parametrize('after',[BEFORE.replace(b'SELECT a',b'SELECT b')+ADDITION,
    BEFORE+b'old_case:\n  pass_str: changed\n',
    BEFORE+b'x: !!python/object:eval {}\n',
    BEFORE+b'x: &v {pass_str: SELECT a}\ny: *v\n',
    BEFORE+b'x:\n  pass_str: SELECT a\n  skip: true\n',
    BEFORE+b'x:\n  pass_str: SELECT a\n  configs:\n    core:\n      ignore: ALL\n'])
def test_existing_cases_dynamic_yaml_and_judge_config_cannot_be_changed(after):
    with pytest.raises((RepoError,yaml.YAMLError)):RuleFixtureAddition.prove(NAME,BEFORE,after)


@pytest.mark.parametrize('addition',[b'\nbroken: [',b'\n1: {pass_str: SELECT 1}\nnew_case: {pass_str: SELECT 2}\n',b'\n? [unhashable, key]\n: value\n'])
def test_invalid_yaml_is_deterministic_policy_rejection(addition):
    with pytest.raises(RepoError):RuleFixtureAddition.prove(NAME,BEFORE,BEFORE+addition)


def test_proof_is_path_and_byte_bound():
    proof=RuleFixtureAddition.prove(NAME,BEFORE,BEFORE+ADDITION)
    assert not proof.verify('test/conftest.py',BEFORE,BEFORE+ADDITION)
    with pytest.raises(RepoError):RuleFixtureAddition.prove('test/conftest.py',BEFORE,BEFORE+ADDITION)
