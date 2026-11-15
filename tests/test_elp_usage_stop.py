"""The audit ledger must survive unsafe caller retry logic."""
import importlib.util
from pathlib import Path
import pytest
from repo_code.util import ProviderNotDispatched


def wrapper():
    path=Path(__file__).resolve().parents[1]/'integrations/swebench/audited_provider.py'
    spec=importlib.util.spec_from_file_location('elp_audit',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    instance=object.__new__(module.AuditedProvider)
    instance.records=[];instance.halt=None
    instance.total_tokens=None;instance.calls=60
    return instance


@pytest.mark.asyncio
async def test_unknown_dispatched_overload_cannot_be_erased_by_clearing_halt():
    p=wrapper()
    p.records=[{'status':'FAILED','error':'server_is_overloaded','reservation':123}]
    # Historical recovery wrappers clear halt before reissuing the same input.
    p.halt=None
    with pytest.raises(ProviderNotDispatched) as error:
        await p.complete('',[],[])
    assert error.value.code=='USAGE_UNSETTLED'
    assert len(p.records)==1
    assert p.budget_status()['known_used']==0
    assert p.budget_status()['charged_including_unknown_reserves']==123
    assert not p.budget_status()['cost_settled']


def test_successful_retry_does_not_settle_an_earlier_unknown_attempt():
    p=wrapper()
    p.records=[{'status':'FAILED','reservation':123},
               {'status':'COMPLETED','reservation':200,'settled_tokens':12}]
    status=p.budget_status()
    assert status['unknown_usage_calls']==1
    assert status['successful_responses']==1
    assert status['known_used']==12
    assert status['charged_including_unknown_reserves']==135
    assert status['actual_cost'] is None
    assert not status['cost_settled']


def test_proven_not_dispatched_cost_is_known_zero():
    p=wrapper()
    p.records=[{'status':'NOT_DISPATCHED','reservation':123,'settled_tokens':0}]
    assert p.budget_status()['cost_settled']
    assert p.budget_status()['dispatched_calls']==0


@pytest.mark.asyncio
async def test_known_usage_is_not_proof_a_failed_request_was_unprocessed():
    p=wrapper()
    p.records=[{'status':'FAILED','reservation':200,'settled_tokens':12}]
    with pytest.raises(ProviderNotDispatched) as error:
        await p.complete('',[],[])
    assert error.value.code=='RETRY_PROOF_REQUIRED'
    assert p.budget_status()['cost_settled'] and p.budget_status()['halted']
