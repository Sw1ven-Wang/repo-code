import json
import pytest
from controller import summarize,require_calibration
from upgrade_pack import upgrade
from repo_code.util import RepoError


def test_handoff_is_not_acceptance_and_unreached_is_not_zero():
    row=summarize('queuecraft',delivered_turns=3,handed_off_turns=2,stage_results=[{'turn':2,'passed':False}],
                  probe_results={},terminal_result={'passed':1,'total':5})
    assert row['accepted_stage_turns']==0 and row['terminal_acceptance'] is None
    assert all(x is None for x in row['recall_probes'].values())
    with pytest.raises(RepoError,match='reference'):require_calibration(row)


def test_calibration_requires_completed_sequential_reference():
    positive={'status':'PASSED','completed_turns':37,'reference_contract_passed':True,'usage_complete':True,
              'within_frozen_budget':True,'first_probe_reached':True,'sequential_no_future_turns':True}
    row=summarize('ledgerflow',delivered_turns=37,handed_off_turns=37,stage_results=[],probe_results={},calibration=positive)
    require_calibration(row)
    positive['usage_complete']=False
    row=summarize('ledgerflow',delivered_turns=37,handed_off_turns=37,stage_results=[],probe_results={},calibration=positive)
    with pytest.raises(RepoError):require_calibration(row)


def test_upgrade_creates_new_pack_and_corrects_probe_without_mutating_original(tmp_path):
    original=tmp_path/'original';control=original/'continuous_memory/controller_only';control.mkdir(parents=True)
    for task in ['ledgerflow','queuecraft']:
        rows=[{'task_id':task,'turn':i,'turn_id':str(i),'kind':'recall_probe' if i==30 else 'user_message',
               'user_message':'requirement '+str(i)} for i in range(1,38)]
        (control/(task+'.turns.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
    (control/'probe_rubric.json').write_text(json.dumps({'queuecraft':[{'turn':30,'checks':['lifecycle']}]}),encoding='utf-8')
    grader=original/'grader_only/continuous_memory';grader.mkdir(parents=True)
    (grader/'test_ledgerflow_contract.py').write_text('def test_dry_run_and_preview_do_not_write(tmp_path):\n    assert False\n\ndef test_totals_preserve_sign_and_pagination(tmp_path):\n    pass\n',encoding='utf-8')
    before=(control/'queuecraft.turns.jsonl').read_bytes()
    upgrade(original,tmp_path/'v2')
    assert (control/'queuecraft.turns.jsonl').read_bytes()==before
    root=tmp_path/'v2';q=[json.loads(x) for x in (root/'continuous_memory/controller_only/queuecraft.turns.jsonl').read_text(encoding='utf-8').splitlines()]
    assert q[28]['kind']=='recall_probe' and q[29]['kind']=='user_message' and len(q)==37
    rubric=json.loads((root/'continuous_memory/controller_only/probe_rubric.json').read_text())
    assert rubric['queuecraft'][0]['turn']==29
    assert json.loads((root/'protocol-v2.json').read_text())['calibration_status'].startswith('BLOCKED')


def test_missing_stage_is_unknown_and_duplicate_stage_cannot_inflate_count():
    row=summarize('ledgerflow',delivered_turns=3,handed_off_turns=2,stage_results=[{'turn':2,'passed':True}],probe_results={})
    assert row['accepted_stage_turns']==1 and row['stage_assessed_turns']==1 and row['unassessed_handoffs']==[1]
    with pytest.raises(RepoError,match='DUPLICATE'):
        summarize('ledgerflow',delivered_turns=2,handed_off_turns=2,stage_results=[{'turn':2,'passed':True}]*2,probe_results={})
