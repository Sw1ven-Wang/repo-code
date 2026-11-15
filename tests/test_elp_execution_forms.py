import pytest
from repo_code.commands import canonical_python, probe_assertion_argv


def test_exact_supported_forms_have_identical_ast_input():
    code='from package import compute\nprint(compute(7))'
    expected=['python', '-c', code]
    assert canonical_python(expected)==expected
    assert canonical_python(['python','-'],code)==expected
    assert canonical_python(['bash','-lc', "python - <<'PY'\n"+code+"\nPY"])==expected


@pytest.mark.parametrize('argv,stdin', [
    (['python','-O','-'],'assert False'),
    (['python','-'],'this is invalid syntax !'),
    (['bash','-lc',"python - <<'PY'\nassert False\nPY\ntrue"],''),
    (['bash','-lc',"python - <<PY\nassert False\nPY"],''),
    (['bash','-lc',"python - <<'PY'\nimport package\nprint(1)\nPY\ntrue\nPY"],''),
    (['python','-c','assert True'],'extra input'),
])
def test_ambiguous_or_optimized_forms_are_not_normalized(argv,stdin):
    assert canonical_python(argv,stdin) is None


def test_normalization_does_not_bypass_bound_assertion_rules():
    probe=canonical_python(['python','-'],'from package import compute\nprint(compute(7))')
    for code in ['if False:\n assert observed', 'try:\n assert observed\nexcept: pass', 'assert True']:
        assertion=canonical_python(['python','-'],code)
        assert probe_assertion_argv(probe,assertion) is None
    assertion=canonical_python(['python','-'],'assert observed == 7')
    assert 'raise AssertionError' in probe_assertion_argv(probe,assertion)[2]
