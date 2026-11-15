"""Trusted early feedback using the same public-test policy as final export.

No gold text, hidden tests, or verifier-reset path lists are rendered to Solver.
This does not replace clean-baseline patch export or official grading.
"""
import re
from pathlib import PurePosixPath
from repo_code.util import RepoError
from regression_fixtures import RuleFixtureAddition
from export_policy import VerifierResetPolicy

PUBLIC_POLICY_HINT = ('Evaluation configuration and existing test expectations are protected. '
    'Add independent regression files instead of weakening existing tests. '
    'For existing SQLFluff rule YAML fixtures, preserve all original bytes and append only independent new cases. '
    'Unsupported changes are rejected before Host workspace effects; final export rechecks the complete candidate. '
    'Use workspace.diff to inspect your changes, then send a no-tool final with checks and remaining limitations. '
    'Only the external official grader determines benchmark success.')


class PublicTestWritePolicy:
    def __init__(self, verifier_policy=None):
        if verifier_policy is not None and not isinstance(verifier_policy, VerifierResetPolicy):
            raise RepoError('INVALID_VERIFIER_POLICY')
        self.restored = verifier_policy.restored_paths if verifier_policy else frozenset()

    def __call__(self, run, workspace, plans):
        store = workspace.store
        baseline = store.read_json(run['data']['manifest']['workspace_baseline_digest'])['files']
        for plan in plans:
            for name, after_ref in plan['after'].items():
                before = store.read_blob(baseline[name]['blob']) if name in baseline else None
                after = store.read_blob(after_ref) if after_ref else None
                if before == after: continue
                parts = PurePosixPath(name).parts
                if any(p in {'.git', '.repo-code', '.github'} for p in parts):
                    raise RepoError('PATCH_CONTROL_PATH_DENIED', 'Task control files are protected; keep the candidate focused on implementation.')
                if parts[-1] in {'pytest.ini', 'tox.ini', 'conftest.py', 'setup.cfg', 'pyproject.toml'}:
                    raise RepoError('PATCH_TEST_OR_JUDGE_CHANGE_DENIED', 'Evaluation configuration is protected. Add an independent regression test without modifying test configuration.')
                is_test = any(p in {'test', 'tests', 'testing'} for p in parts) or parts[-1].startswith('test_')
                if not is_test or before is None or name in self.restored: continue
                if re.fullmatch(r'test/fixtures/rules/std_rule_cases/[A-Za-z0-9_]+\.yml', name):
                    try: RuleFixtureAddition.prove(name, before, after or b'')
                    except RepoError as exc:
                        raise RepoError(exc.code, 'Existing rule YAML must retain all original bytes and expectations. Append independent new cases, or add a separate regression file; this change was not applied.') from None
                else:
                    raise RepoError('PATCH_TEST_OR_JUDGE_CHANGE_DENIED', 'This existing test is protected by local candidate policy. Preserve it and add a separate independent regression file; this change was not applied.')
