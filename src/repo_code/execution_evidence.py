"""Host-authenticated, immutable binding of a replayed probe to its execution."""
from dataclasses import asdict, dataclass
from .util import digest, RepoError


@dataclass(frozen=True)
class ExecutionEvidenceInput:
    version: int
    operation_id: str
    run_id: str
    control_epoch: int
    source_digest: str
    output_digest: str | None
    environment_digest: str | None
    release_digest: str
    contract_digest: str | None
    request_digest: str
    executed_command_digest: str
    stdin_ref: str
    stdout_ref: str
    stderr_ref: str
    probe_id: str
    check_id: str
    exit_code: int | None
    complete: bool


def bind_execution(store, run, operation_id, arguments, argv, stdin, result, probe_id):
    """Called only by Broker after it has validated and replayed the probe."""
    check = {'run_id': run['id'], 'probe_id': probe_id,
             'kind': 'LOCAL_MODEL_PROPOSED_CHECK' if probe_id.startswith('local-check:') else 'BOUND_PROBE',
             'grants_acceptance_authority': False,
             'assertion_digest': digest([arguments['argv'], arguments.get('stdin', '')]),
             'release_digest': run['data']['manifest']['release_digest'],
             'contract_digest': run['data']['manifest']['contract_digest']}
    check_id = digest(check)
    store.put('process_check', check_id, check, immutable=True)
    value = ExecutionEvidenceInput(
        1, operation_id, run['id'], run['control_epoch'], result['relevant_state'],
        result.get('output_snapshot'), result.get('environment_digest'),
        check['release_digest'], check['contract_digest'], check['assertion_digest'],
        digest([argv, stdin]), store.blob(arguments.get('stdin', '')),
        store.blob(result.get('stdout', '')), store.blob(result.get('stderr', '')),
        probe_id, check_id, result.get('exit_code'),
        not any(result.get(k) for k in ('timeout', 'cancelled', 'running'))
        and result.get('executed') is not False
        and (result.get('exit_code') != 0 or result.get('assertions_completed') is True))
    data = asdict(value)
    return {'data': data, 'signature': store.sign(data)}


def valid_execution(store, result, argv, stdin='', *, run=None, operation_id=None, approved_epoch=None):
    if store is None:
        return False
    try:
        proof = result['execution_evidence']; data = proof['data']
        value = ExecutionEvidenceInput(**data)
        if not store.verify(data, proof['signature']):
            return False
        check = store.get('process_check', value.check_id)
        if not check or digest(check) != value.check_id:
            return False
        if (check['run_id'] != value.run_id or check['probe_id'] != value.probe_id
                or check['release_digest'] != value.release_digest
                or check['contract_digest'] != value.contract_digest
                or check['assertion_digest'] != value.request_digest):
            return False
        if (value.version != 1 or not value.complete or type(value.exit_code) is not int
                or value.request_digest != digest([argv, stdin])
                or value.probe_id != (result.get('checked_probe') or result.get('independent_check'))
                or value.source_digest != result.get('relevant_state')
                or value.output_digest != result.get('output_snapshot')
                or value.source_digest != value.output_digest
                or not value.environment_digest
                or value.environment_digest != result.get('environment_digest')
                or value.exit_code != result.get('exit_code')
                or (value.exit_code == 0 and result.get('assertions_completed') is not True)
                or any(result.get(k) for k in ('timeout', 'cancelled', 'running'))
                or result.get('executed') is False):
            return False
        if store.read_blob(value.stdin_ref).decode() != stdin:
            return False
        if store.read_blob(value.stdout_ref).decode() != result.get('stdout', ''):
            return False
        if store.read_blob(value.stderr_ref).decode() != result.get('stderr', ''):
            return False
        if operation_id is not None and value.operation_id != operation_id:
            return False
        if approved_epoch is not None and value.control_epoch != approved_epoch:
            return False
        if run is not None:
            manifest = run['data']['manifest']
            if (value.run_id != run['id'] or value.control_epoch != run['control_epoch']
                    or value.release_digest != manifest['release_digest']
                    or value.contract_digest != manifest['contract_digest']
                    or (manifest.get('environment_digest') and value.environment_digest != manifest['environment_digest'])):
                return False
        return True
    except (KeyError, TypeError, ValueError, UnicodeError, RepoError):
        return False
