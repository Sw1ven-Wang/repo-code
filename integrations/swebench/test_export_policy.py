"""Regression tests for the former PyVista public-test export false rejection."""
import shutil
import subprocess
import pytest
from adapter import patch_from_workspace
from export_policy import VerifierResetPolicy
from test_adapter import baseline, git
from repo_code.util import RepoError


def setup_case(tmp_path):
    base, source, work = baseline(tmp_path)
    for root in (base, source, work):
        (root/'tests').mkdir()
        (root/'tests/test_public.py').write_text('assert True\n')
        (root/'tests/test_other.py').write_text('assert True\n')
    git(base,'add','.')
    git(base,'-c','user.name=fixture','-c','user.email=fixture@localhost','commit','-m','public tests')
    sha = git(base,'rev-parse','HEAD').decode().strip()
    script = (f"#!/bin/bash\ngit checkout {sha} tests/test_public.py\n"
              "git apply -v - <<'EOF_123'\n"
              "this private diff is not parsed as shell\nEOF_123\npytest tests\n")
    return base, source, work, sha, script


@pytest.mark.parametrize('delete', [False,True])
def test_public_test_delta_preserved_and_original_baseline_applies(tmp_path,delete):
    base,source,work,sha,script=setup_case(tmp_path)
    (work/'change.py').write_text('after\n')
    if delete:(work/'tests/test_public.py').unlink()
    else:(work/'tests/test_public.py').write_text('assert 1 == 1\n')
    policy=VerifierResetPolicy.from_script(script,sha)
    patch,changed=patch_from_workspace(base,source,work,tmp_path/'replay',policy)
    assert changed==['change.py','tests/test_public.py']
    # Every candidate byte is present; no test hunk is silently removed.
    subprocess.run(['git','apply','-'],cwd=base,input=patch,check=True)
    assert (base/'change.py').read_bytes()==(work/'change.py').read_bytes()
    if not delete:assert (base/'tests/test_public.py').read_bytes()==(work/'tests/test_public.py').read_bytes()
    else:assert not (base/'tests/test_public.py').exists()
    # The unchanged official reset defeats even deletion of the public test.
    git(base,'checkout',sha,'tests/test_public.py')
    assert (base/'tests/test_public.py').read_bytes()==(source/'tests/test_public.py').read_bytes()


@pytest.mark.parametrize('name', ['tests/test_other.py','conftest.py','pytest.ini','tox.ini','setup.cfg','pyproject.toml','.github/workflows/judge.yml'])
def test_unreset_tests_and_judge_controls_still_denied(tmp_path,name):
    base,source,work,sha,script=setup_case(tmp_path)
    target=work/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('changed\n')
    with pytest.raises(RepoError):
        patch_from_workspace(base,source,work,tmp_path/'replay',VerifierResetPolicy.from_script(script,sha))


def test_default_denies_existing_public_test_without_verifier_evidence(tmp_path):
    base,source,work,sha,script=setup_case(tmp_path)
    (work/'tests/test_public.py').write_text('changed\n')
    with pytest.raises(RepoError) as error:
        patch_from_workspace(base,source,work,tmp_path/'replay')
    assert error.value.code=='PATCH_TEST_OR_JUDGE_CHANGE_DENIED'


def test_checkout_inside_gold_diff_or_after_tests_does_not_grant_exception(tmp_path):
    base,source,work,sha,script=setup_case(tmp_path)
    script="git apply -v - <<'EOF_123'\n"+f'git checkout {sha} tests/test_other.py\nEOF_123\npytest\ngit checkout {sha} tests/test_other.py\n'
    assert not VerifierResetPolicy.from_script(script,sha).restored_paths


@pytest.mark.parametrize('name',['../tests/test_public.py','tests/*.py','tests/test_public.py;echo','--force'])
def test_unsafe_reset_paths_denied(tmp_path,name):
    base,source,work,sha,script=setup_case(tmp_path)
    with pytest.raises(RepoError):
        VerifierResetPolicy.from_script(script.replace('tests/test_public.py',name),sha)


def test_wrong_baseline_and_symlinks_denied(tmp_path):
    base,source,work,sha,script=setup_case(tmp_path)
    policy=VerifierResetPolicy.from_script(script.replace(sha,'a'*40),'a'*40)
    with pytest.raises(RepoError,match='VERIFIER_BASE_COMMIT_MISMATCH'):
        patch_from_workspace(base,source,work,tmp_path/'wrong',policy)
    (work/'link.py').symlink_to(work/'change.py')
    with pytest.raises(RepoError,match='PATCH_SYMLINK_DENIED'):
        patch_from_workspace(base,source,work,tmp_path/'symlink')
