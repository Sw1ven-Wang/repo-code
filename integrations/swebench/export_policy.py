"""Trusted verifier evidence for public-test export; never sent to the model."""
import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from repo_code.util import RepoError
from repo_code.workspace import relative_path


@dataclass(frozen=True)
class VerifierResetPolicy:
    base_commit: str
    script_sha256: str
    restored_paths: frozenset[str]

    @classmethod
    def from_script(cls, script, base_commit):
        """Recognize standalone resets before the official test-patch heredoc.

        Consume only a trusted, unmodified Harness script. Unsupported shell
        constructs fail closed; checkout commands in the gold diff or after
        test execution must never grant an export exception.
        """
        if not re.fullmatch(r'[a-f0-9]{40}', base_commit):
            raise RepoError('INVALID_VERIFIER_BASE_COMMIT')
        restored = set()
        injection = False
        for line in script.splitlines():
            if re.fullmatch(r"git apply -v - <<'EOF_[A-Za-z0-9_]+'", line):
                injection = True
                break
            try:
                words = shlex.split(line)
            except ValueError:
                raise RepoError('UNSUPPORTED_VERIFIER_SCRIPT') from None
            if words[:3] != ['git', 'checkout', base_commit]:
                continue
            paths = words[3:]
            if paths[:1] == ['--']:
                paths = paths[1:]
            for name in paths:
                relative_path(name)
                if (PurePosixPath(name).as_posix() != name or
                        any(c in name for c in ';|&$`*?[]<>!') or name.startswith('-')):
                    raise RepoError('UNSUPPORTED_VERIFIER_RESET_PATH', name)
                restored.add(name)
        if not injection:
            raise RepoError('VERIFIER_TEST_INJECTION_NOT_FOUND')
        return cls(base_commit, hashlib.sha256(script.encode()).hexdigest(), frozenset(restored))

    def audit(self):
        return {'base_commit': self.base_commit, 'script_sha256': self.script_sha256,
                'restored_paths': sorted(self.restored_paths)}


def load_verified_policy(output, instance_id, base_commit):
    """Read gold-control artifacts only on the trusted export side."""
    import json
    output = Path(output)
    control = json.loads((output/'config/gold_official.json').read_text())
    report_path = Path(control['results'])
    report = json.loads(report_path.read_text())
    if control['exit_code'] or instance_id not in report['resolved_ids'] or report['error_ids']:
        raise RepoError('VERIFIER_GOLD_CONTROL_NOT_PASSED')
    candidates = list((report_path.parent/'per-instance').glob('*/'+instance_id+'/eval.sh'))
    if len(candidates) != 1:
        raise RepoError('VERIFIER_SCRIPT_NOT_UNIQUE')
    return VerifierResetPolicy.from_script(candidates[0].read_text(), base_commit)
