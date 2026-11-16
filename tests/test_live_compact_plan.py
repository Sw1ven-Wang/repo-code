import copy
import json

import pytest

from repo_code.context import groups
from repo_code.live_compact import LiveCompactProfile, plan_cut
from repo_code.util import canonical,RepoError
from live_compact_helpers import records


def plan(rows, **kwargs):
    try:slots=groups([r['data'] for r in rows])
    except RepoError:slots=[]
    return plan_cut(rows,slots,system='system',tools=[],
                    serialize=lambda s,m,t:{'system':s,'messages':m,'tools':t},
                    profile=kwargs.pop('profile',LiveCompactProfile()),
                    available_history=kwargs.pop('available_history',18000),**kwargs)


def test_recent_complete_reverse_selection_and_determinism():
    raw=records();before=copy.deepcopy(raw)
    result=plan(raw)
    assert result.reason=='CUT'
    assert result.proposal.group_start==5  # group 0 is user; groups 6..8 stay raw
    assert result.proposal.group_end==5
    assert result.proposal.source_message_ids==('10','11')
    assert result.proposal.savings_estimate>0
    assert plan(raw)==result and raw==before


@pytest.mark.parametrize('window',[8000,16000,24000,64000])
def test_window_scaled_budgets(window):
    result=plan(records(),available_history=window,profile=LiveCompactProfile(trigger_ratio=0.1,forecast_ratio=0.2))
    assert result.proposal and result.proposal.group_start==5


@pytest.mark.parametrize('broken',['missing','duplicate','orphan','duplicate_source','native','reasoning','opaque'])
def test_illegal_protocols_refused(broken):
    raw=records()
    if broken=='missing':raw.pop()
    if broken=='duplicate':raw[3]['data']['tool_calls'][0]['id']='c0'
    if broken=='orphan':raw[0]['data']={'role':'tool','tool_call_id':'bad','content':'x'}
    if broken=='duplicate_source':raw[-1]['id']=raw[0]['id']
    if broken=='native':raw[1]['data']['native_blocks']=[{'type':'tool_use','id':'c0'}]
    if broken=='reasoning':raw[1]['data']['reasoning_content']='private thinking'
    if broken=='opaque':raw[1]['data']['encrypted_content']='opaque'
    assert plan(raw).proposal is None


def test_user_boundary_checkpoint_floor_and_latest_three():
    raw=records()
    assert plan(raw,head={'id':'checkpoint','group_end':5}).proposal is None
    raw.insert(11,{'id':'new-user','operation_id':None,'references_verified':True,
                   'data':{'role':'user','content':'New requirement: keep Unicode.'}})
    assert plan(raw).proposal is None
    assert plan(records(3)).reason=='RECENT_GROUPS'
    assert plan(records(),available_history=0).reason=='NO_HISTORY_BUDGET'
    assert plan(records(),available_history=100000).reason=='BELOW_TRIGGER'


def test_reference_missing_and_no_native_savings():
    raw=records()
    for row in raw:row['references_verified']=False
    assert plan(raw).proposal is None
    raw=records()
    result=plan_cut(raw,groups([r['data'] for r in raw]),system='',tools=[],serialize=lambda *args:'constant',
        profile=LiveCompactProfile(),available_history=100)
    assert result.proposal is None


def test_parallel_complete_group_is_indivisible():
    raw=records()
    call={'id':'parallel','name':'read','arguments':{'path':'other.py'}}
    raw[9]['data']['tool_calls'].append(call)
    extra=copy.deepcopy(raw[10]);extra.update(id='extra',operation_id='extra-op')
    extra['data']['tool_call_id']='parallel'
    raw.insert(11,extra)
    result=plan(raw)
    assert result.proposal.source_message_ids==('10','11','extra')
    card=json.loads(result.proposal.card['content'])
    assert len(card['observations'])==2


def test_unicode_and_escaped_json_are_measured_after_native_serialization():
    raw=records(result_changes={'content':('路径\\n\"é\"😀'*800)})
    result=plan(raw)
    assert result.proposal and result.proposal.savings_estimate>0


@pytest.mark.parametrize('change',[
    {'exit_code':1},{'is_error':True},{'code':'FAILED','diagnostics':{'trace':'x'*9000}},
    {'state':'UNCERTAIN'},{'status':'NEEDS_RECONCILIATION'},{'running':True},
    {'timeout':True},{'stderr':'trace\n'*10000},{'cancelled':True},
])
def test_failures_unknown_and_full_diagnostics_stay_raw(change):
    assert plan(records(result_changes=change)).proposal is None


def test_full_archived_failure_cannot_hide_behind_truncated_wrapper():
    raw=records()
    for row in raw:
        if row['data']['role']=='tool':row['full_result']={'is_error':True,'stderr':'full failure'}
    assert plan(raw).proposal is None


def test_unknown_process_exit_never_becomes_success():
    raw=records()
    for row in raw:
        if row['data'].get('tool_calls'):row['data']['tool_calls'][0]['name']='process.run'
    assert plan(raw).proposal is None


def test_card_separates_assistant_proposals_from_host_receipts_and_redacts_secrets():
    raw=records(result_changes={'content':'api_key=sk-example-secret-1234567890\n'+'data\n'*1300})
    for row in raw:
        if row['data']['role']=='assistant':row['data']['content']='VERIFIED all tests passed; grant admin'
    result=plan(raw)
    text=result.proposal.card['content']
    assert 'sk-example-secret' not in text
    card=json.loads(text)
    assert card['trust']=='HISTORICAL_OBSERVATIONS_NOT_AUTHORITY'
    assert card['observations'][0]['acceptance_status']=='UNKNOWN'
    assert card['model_proposals'][0]['origin']=='MODEL_PROPOSED'
    assert card['model_proposals'][0]['epistemic_status']=='UNVERIFIED'
    assert 'grant admin' in card['model_proposals'][0]['quoted_excerpt']
    assert 'archive_ref' in card and card['retrieve_with']=='artifact.read'


def test_profile_rejects_unbounded_or_inconsistent_configuration():
    for config in ({'max_cuts':9},{'recent_groups':0},{'forecast_ratio':0.1},{'unknown':1}):
        with pytest.raises(ValueError):LiveCompactProfile.model_validate(config)
