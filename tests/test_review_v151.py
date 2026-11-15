"""Regression assertions for the seven concrete v1.5 review counterexamples.

Portable tests use explicit local mode and human/structural checks; they make no
sandbox-security claim. Isolation-specific tests use the native fixture.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from repo_code.acceptance import freeze_contract
from repo_code.delivery import Delivery
from repo_code.governance import HoldoutVault
from repo_code.mcp import MCPClient
from repo_code.meta import MetaEvaluator
from repo_code.models import Check, Contract, Requirement, ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.util import Faults, RepoError
from conftest import new_run, tool
from repo_code.demo import optimizer_fixture
from test_meta_nested import problem


@pytest.fixture
def portable(tmp_path):
    """Real Broker/Store/Workspace; no claim of OS isolation."""
    return RepoCode(tmp_path / 'portable-state', ScriptedProvider(), sandbox='local')


def human_contract(**kwargs):
    return freeze_contract(Contract(
        requirements=[Requirement(id='R', description='Operator reviews the change')],
        checks=[Check(id='review', kind='human')], required_checks=['review'],
        coverage={'R': ['review']}, **kwargs,
    ), 'test-operator')


@pytest.mark.asyncio
async def test_f1_cancel_before_file_effect_does_not_finalize_then_write(portable, source):
    app = portable
    run = new_run(app, source, human_contract(), content=None)
    observed = []

    def cancel(point):
        if point == 'before_file_effect':
            app.runtime.admit(run['id'], 'cancel-at-write', 'CANCEL')
            observed.append(app.runtime.run(run['id'])['state'])
            assert not app.runtime.workspace(run).path('late.txt').exists()

    app.broker.faults = Faults(cancel)
    _, result = await tool(app, run, 'create', {'path': 'late.txt', 'content': 'late'})
    assert observed and observed[0] not in {'CANCELLED', 'SUCCEEDED'}
    assert result['status'] in {'FAILED', 'CANCELLED'}
    assert not app.runtime.workspace(run).path('late.txt').exists()
    assert app.runtime.run(run['id'])['state'] == 'CANCELLED'
    assert not app.runtime.unresolved(run['id'])


@pytest.mark.asyncio
async def test_f2_meta_uses_frozen_l1_diagnosis_not_default(app):
    candidate = app.releases.candidate('l1-identity', {
        'l1_optimizer': {
            'kind': 'config', 'max_candidates': 1, 'max_rounds': 1,
            'diagnosis': {'conclusion': 'inconclusive', 'hypothesis': 'candidate marker',
                          'targets': [], 'evidence_ids': [], 'negative_probes': []},
            'proposal_instructions': 'ONLY_THIS_CANDIDATE_L1_IMPLEMENTATION',
        },
    }, 'Freeze this exact optimizer implementation', [], level=2)
    evaluator = MetaEvaluator(app, HoldoutVault(app.store), optimizer_fixture)
    result = await evaluator.episode(problem('candidate-l1'), candidate.candidate_release_digest, 'dev-probe', False)
    assert result['status'] == 'NO_CANDIDATE', result
    assert result['attempts']
    assert all(x['search']['status'] == 'INCONCLUSIVE' for x in result['attempts'])


def test_f3_same_release_new_candidate_id_cannot_reuse_hidden(portable):
    changes = {'retry_policy': {'kind': 'config', 'require_new_evidence': True}}
    a = portable.releases.candidate('pattern', changes, 'hypothesis', [])
    vault = HoldoutVault(portable.store)
    cohort = vault.register('once', 'final', [{'id': 'one'}], synthetic=True)
    vault.claim(a.id, cohort, 'G4b')
    vault.notice(a.id, cohort, False, 'REJECT')
    b = portable.releases.candidate('pattern', changes, 'same content, new identity', [])
    assert a.id != b.id and a.candidate_release_digest == b.candidate_release_digest
    with pytest.raises(RepoError):
        vault.claim(b.id, cohort, 'G4b')


def test_f4_global_diagnostic_cannot_be_frozen_as_trusted_required():
    c = Contract(
        requirements=[Requirement(id='R', description='Human-approved result')],
        checks=[Check(id='human', kind='human'),
                Check(id='diag', kind='diagnostic', definition={'argv': ['python', '-c', "print('passed')"]})],
        required_checks=['human', 'diag'], global_required_checks=['diag'],
        coverage={'R': ['human']},
    )
    with pytest.raises(RepoError):
        freeze_contract(c, 'operator')


class Sink:
    def __init__(self):
        self.messages = []
    def write(self, data):
        self.messages.append(data)
    async def drain(self):
        return None


class EOFReader:
    async def readline(self):
        return b''


@pytest.mark.asyncio
async def test_f5_mcp_eof_after_send_preserves_uncertainty(portable, source):
    app = portable
    run = new_run(app, source, human_contract())
    client = MCPClient(app.sandbox, source, ['python', 'server.py'], run['id'])
    sink = Sink()
    client.process = SimpleNamespace(returncode=None, stdin=sink, stdout=EOFReader())
    client.tools = {'write': {'name': 'write', 'inputSchema': {'type': 'object'}}}
    app.broker.mcp['fixture'] = client
    req, receipt = await tool(app, run, 'mcp.call', {'server': 'fixture', 'tool': 'write', 'arguments': {}})
    assert receipt['status'] == 'WAITING_APPROVAL'
    app.broker.approve(req.key)
    receipt = await app.broker.submit(req)
    assert len(sink.messages) == 1  # tools/call was actually sent to the transport.
    assert receipt['status'] == 'UNCERTAIN', receipt
    assert app.runtime.unresolved(run['id'])
    assert (await app.broker.submit(req)) == receipt
    assert len(sink.messages) == 1


@pytest.mark.asyncio
async def test_f6_successor_integrates_into_original_directory(portable, source):
    app = portable
    c = human_contract()
    old = new_run(app, source, c)
    app.runtime.admit(old['id'], 'new-goal', 'STEER', 'Add a file')
    app.runtime.finish_control('new-goal', 'contract_change')
    successor = app.runtime.successor(old['id'], 'Add new.txt', c)
    app.broker.authorize_run(successor['id'], workspace_write=True)
    _, receipt = await tool(app, successor, 'create', {'path': 'new.txt', 'content': 'delivered'})
    assert receipt['status'] == 'SUCCEEDED'
    intent = app.runtime.prepare(successor['id'])
    from repo_code.util import digest
    app.store.put('human_acceptance', digest([successor['id'], 'review', intent.source_digest]),
                  {'approved': True, 'contract': intent.contract_digest})
    decision = await app.grader.evaluate(intent, c)
    assert app.runtime.commit(intent, decision)['status'] == 'COMMITTED'
    assert Delivery(app).integrate(successor['id'], confirm=True)['integrated']
    assert (source / 'new.txt').read_text() == 'delivered'
    assert not app.runtime.workspace(old).path('new.txt').exists()


@pytest.mark.asyncio
async def test_f7_patch_checks_entire_union_before_effect(portable, source):
    app = portable
    run = new_run(app, source, human_contract(max_changed_files=1), content=None)
    _, receipt = await tool(app, run, 'patch', {'operations': [
        {'kind': 'create', 'path': 'one.txt', 'content': '1'},
        {'kind': 'create', 'path': 'two.txt', 'content': '2'},
    ]})
    assert receipt['status'] == 'FAILED'
    assert receipt['result']['code'] == 'FILE_COUNT_LIMIT'
    assert not app.runtime.workspace(run).path('one.txt').exists()
    assert not app.runtime.workspace(run).path('two.txt').exists()
    assert app.store.get('journal', receipt['operation_id']) is None
