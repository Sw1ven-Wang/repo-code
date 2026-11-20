"""Host-only routing of frozen public inputs. A match nominates, never grants."""
import json
from .util import digest, RepoError


def _unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("ambiguous policy")
        obj[key] = value
    return obj


class QualificationCatalog:
    """Construct from trusted configuration, before executing any of the tasks.

    A rule names a public policy file and the exact JSON fields that define a
    behavioral contract. Do not pass current worker files or model output.
    Role and eligibility come from the trusted experiment controller.
    """
    def __init__(self, rules, tasks, *, observations=None):
        # Observations are supplied only by the Host's isolated public probes.
        # A worker's self-report or a trajectory assertion is never an input.
        observations = observations or {}
        self._rows = {}
        self.revision = digest([rules, tasks, observations]) if observations else digest([rules, tasks])
        for row in tasks:
            key = row['id']
            if key in self._rows or row['role'] not in {'source','support','target'}:
                raise RepoError('QUALIFICATION_CATALOG_INVALID')
            matches = set()
            fingerprint = digest(row['files'])
            for rule in rules:
                probe = rule.get('observation')
                fields = rule.get('equals', {})
                if not (fields or probe) or not rule['mechanism']:
                    raise RepoError('QUALIFICATION_RULE_INVALID')
                if probe:
                    seen = observations.get(key, {}).get(probe)
                    if not seen or seen.get('files_digest') != fingerprint or seen.get('outcome') != 'MATCH':
                        continue
                if not fields:
                    matches.add(rule['mechanism'])
                    continue
                try:
                    policy = json.loads(row['files'][rule['path']], object_pairs_hook=_unique)
                except (KeyError, ValueError, TypeError):
                    continue
                if isinstance(policy, dict) and all(k in policy and digest(policy[k]) == digest(v)
                                                    for k,v in fields.items()):
                    matches.add(rule['mechanism'])
            self._rows[key] = (row['role'], fingerprint, tuple(sorted(matches)))

    def family(self, key, files):
        _, fingerprint, mechanisms = self._rows[key]
        if digest(files) != fingerprint:
            raise RepoError('QUALIFICATION_INPUT_CHANGED')
        if len(mechanisms) == 1:
            return 'mechanism:' + mechanisms[0]
        # Unknown or ambiguous is task-local, never a shared fallback family.
        return 'unqualified:' + digest([key, fingerprint])[7:]

    def nominate(self, draft, source_id, *, source_files, passed_sources, passed_supports):
        role, original, matches = self._rows[source_id]
        base = {'catalog': self.revision, 'draft':draft['id'], 'source':source_id,
                'reuse_authorized':False, 'support_ids':[]}
        family = self.family(source_id, source_files)
        if role != 'source' or source_id not in passed_sources:
            return {**base,'status':'SOURCE_NOT_VERIFIED'}
        if len(matches) != 1 or draft['task_family'] != family:
            return {**base,'status':'NO_UNAMBIGUOUS_MECHANISM'}
        eligible = sorted(key for key,(role,fp,m) in self._rows.items()
                          if role=='support' and key in passed_supports and fp!=original and m==matches
                          and not any(r=='target' and f==fp for r,f,_ in self._rows.values()))
        return {**base,'status':'NOMINATED' if eligible else 'NO_INDEPENDENT_SUPPORT',
                'support_ids':eligible}
