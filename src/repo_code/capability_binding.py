"""Portable capability templates and run-local, evidence-bound instantiations.

Templates never contain repository paths or command literals. A binding grants
no tool permissions and cannot substitute for independent acceptance evidence.
"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .util import RepoError, digest


class BindingSlot(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['path', 'argv']
    purpose: str = Field(min_length=12, max_length=1200)


def validate_template(slots, rules, steps):
    if not slots or len(slots) > 8 or len(rules) != len(steps):
        raise RepoError('CAPABILITY_TEMPLATE_SCHEMA')
    parsed = {}
    for name, value in slots.items():
        if not name.isidentifier() or len(name) > 60:
            raise RepoError('CAPABILITY_SLOT_NAME')
        parsed[name] = BindingSlot.model_validate(value).model_dump()
    used = set()
    allowed = {'read': 'path', 'edit': 'path', 'workspace.diff': 'path', 'process.run': 'argv'}
    for rule in rules:
        field = allowed.get(rule.get('tool'))
        args = rule.get('arguments_match', {})
        if set(rule) != {'tool', 'arguments_match'} or field is None or set(args) != {field}:
            raise RepoError('CAPABILITY_TEMPLATE_ARGUMENT')
        ref = args[field]
        if not isinstance(ref, dict) or set(ref) != {'slot'} or ref['slot'] not in parsed:
            raise RepoError('CAPABILITY_LITERAL_BINDING_DENIED')
        if parsed[ref['slot']]['kind'] != field:
            raise RepoError('CAPABILITY_SLOT_TYPE')
        used.add(ref['slot'])
    if used != set(parsed):
        raise RepoError('CAPABILITY_UNUSED_SLOT')
    return parsed


def authorized(router, run, offer, contract_ref):
    if (not offer or not offer['offered'] or offer['run_id'] != run['id']
            or contract_ref not in offer['contract_refs']
            or offer['control_epoch'] != run['control_epoch']
            or offer['release_digest'] != run['data']['manifest']['release_digest']):
        raise RepoError('CAPABILITY_BINDING_OFFER_REQUIRED')
    route = router.store.get('capability_route', offer['route_id'])
    if not route:
        raise RepoError('CAPABILITY_ROUTE_MISMATCH')
    router.owner.repo.ensure_dev([contract_ref, *route['source_revisions']])
    router.owner.repo.check([offer['release_digest']])
    from .session_experience import SessionExperiences
    active = SessionExperiences(router.owner.runtime).entries(run['id'])
    if any(active.get(entity, {}).get('session_grant_id') != grant
           for entity, grant in route.get('session_grants', {}).items()):
        raise RepoError('SESSION_AUTHORIZATION_CHANGED')


def bind(router, run_id, contract_ref, route_id, values, evidence, rationale):
    store = router.store
    run = router.owner.runtime.run(run_id)
    router.owner.runtime.assert_executable(run)
    from .tool_models import SkillBind
    SkillBind.model_validate({'contract_ref':contract_ref,'route_id':route_id,
                             'values':values,'evidence':evidence,'rationale':rationale})
    contract = store.get('capability_contract', contract_ref)
    offer = store.get('capability_offer', route_id)
    authorized(router, run, offer, contract_ref)
    slots = (contract or {}).get('binding_slots')
    if not slots:
        raise RepoError('CAPABILITY_NOT_PORTABLE')
    validate_template(slots, contract['step_rules'], contract['steps'])
    if set(values) != set(slots) or set(evidence) != set(slots):
        raise RepoError('CAPABILITY_BINDING_SLOT_COVERAGE')
    ws = router.owner.runtime.workspace(run)
    source = ws.snapshot()
    observations = {}
    for name, slot in slots.items():
        value = values[name]
        if slot['kind'] == 'path':
            if not isinstance(value, str) or not value or value not in evidence[name]:
                raise RepoError('CAPABILITY_BINDING_PATH_EVIDENCE',
                    f'Slot {name!r} has kind=path: values[{name!r}] must be a current relative file path included in evidence[{name!r}], not a prose description. For kind=argv use a JSON array of argument strings, not a shell string. Read cited files before retrying.')
            ws.path(value)  # Workspace traversal/symlink policy remains authoritative.
        else:
            from .tool_models import Process
            from pydantic import ValidationError
            try:Process.model_validate({'argv': value}, strict=True)
            except ValidationError:
                raise RepoError('CAPABILITY_BINDING_ARGV_TYPE',
                    f'Slot {name!r} has kind=argv: supply a nonempty JSON array of argument strings, not a shell command string; cite the current test configuration or test source in evidence.') from None
        paths = evidence[name]
        if not isinstance(paths, list) or not 1 <= len(paths) <= 8:
            raise RepoError('CAPABILITY_BINDING_EVIDENCE_REQUIRED')
        for path in paths:
            obs = store.get('observation', digest([run_id, path]))
            current = ws.read(path)['version']
            if not obs or obs['version'] != current:
                raise RepoError('CAPABILITY_BINDING_READ_REQUIRED', path)
            observations[path] = current
    resolved = [{'tool': rule['tool'], 'arguments_match': {
        field: values[ref['slot']] for field, ref in rule['arguments_match'].items()
    }} for rule in contract['step_rules']]
    if ws.snapshot() != source:
        raise RepoError('CAPABILITY_BINDING_SOURCE_CHANGED')
    key = digest([run_id, route_id, contract_ref])
    payload = {'run_id': run_id, 'route_id': route_id, 'contract_ref': contract_ref,
               'values': values, 'evidence': evidence, 'rationale': rationale,
               'source_digest': source, 'observations': observations,
               'environment': router.owner.runtime.sandbox.environment_digest,
               'control_epoch': run['control_epoch'], 'writer_epoch':run['writer_epoch'], 'step_rules': resolved}
    with store.tx() as c:
        current=router.owner.runtime.run(run_id,c)
        router.owner.runtime.assert_executable(current)
        if any(current[k]!=run[k] for k in ('control_epoch','writer_epoch')):
            raise RepoError('CAPABILITY_BINDING_SOURCE_CHANGED')
        old = store.get('capability_binding', key, c)
        if old:
            if not store.verify(old['payload'],old['signature']):raise RepoError('CAPABILITY_BINDING_CORRUPT')
            if any(old['payload'][k] != payload[k] for k in ('values', 'evidence', 'rationale')):
                raise RepoError('CAPABILITY_BINDING_IMMUTABLE')
            return {'binding_ref': key, 'status': 'BOUND', 'grants_authority': False}
        payload['sequence'] = store.emit(c, run_id, 'capability.bound',
                                        {'contract_ref': contract_ref, 'binding_ref': key}, 'DEV')
        store.put('capability_binding', key, {'payload': payload, 'signature': store.sign(payload)}, c, immutable=True)
    return {'binding_ref': key, 'status': 'BOUND', 'grants_authority': False,
            'steps':contract['steps'],'bound_step_rules':resolved,
            'next': 'Execute the returned bound_step_rules in order after this binding. Earlier reads justified the binding; they do not count as post-binding execution. Normal permissions and independent checks still determine application and success.'}


def resolve(router, run, offer, contract_ref, contract):
    """Return frozen per-run rules, never replace the portable release contract."""
    authorized(router, run, offer, contract_ref)
    key = digest([run['id'], offer['route_id'], contract_ref])
    row = router.store.get('capability_binding', key)
    if not row or not router.store.verify(row['payload'], row['signature']):
        return None
    p = row['payload']
    if (p['run_id'] != run['id'] or p['contract_ref'] != contract_ref
            or p['control_epoch'] != run['control_epoch']
            or p['environment'] != router.owner.runtime.sandbox.environment_digest):
        return None
    return {**contract, 'step_rules': p['step_rules']}, key, p['sequence']
