"""Stage-aware reporting and calibration gates for a versioned Mini10 taskpack.

These are trusted-controller helpers, never injected into Solver history.
Unreached probes and tests for unseen requirements are not model failures.
"""
from repo_code.util import RepoError


def summarize(task, *, delivered_turns, handed_off_turns, stage_results, probe_results,
              terminal_result=None, calibration=None):
    if task not in {'ledgerflow','queuecraft'}:raise RepoError('UNKNOWN_CONTINUOUS_TASK')
    if not 0<=handed_off_turns<=delivered_turns<=37:raise RepoError('INVALID_TURN_COUNTS')
    if len({r['turn'] for r in stage_results})!=len(stage_results):raise RepoError('DUPLICATE_STAGE_RESULTS')
    expected=[11,20,26,36] if task=='ledgerflow' else [10,18,29,36]
    probes={str(turn):probe_results.get(str(turn),probe_results.get(turn)) if turn<=handed_off_turns else None for turn in expected}
    relevant=[r for r in stage_results if r['turn']<=handed_off_turns]
    assessed=[r for r in relevant if type(r.get('passed')) is bool]
    calibrated=bool(calibration and calibration.get('status')=='PASSED' and calibration.get('completed_turns')==37
                    and calibration.get('reference_contract_passed') and calibration.get('usage_complete')
                    and calibration.get('within_frozen_budget') and calibration.get('first_probe_reached')
                    and calibration.get('sequential_no_future_turns'))
    terminal_applicable=delivered_turns==37
    return {'task':task,'protocol_version':2,'delivered_turns':delivered_turns,'handed_off_turns':handed_off_turns,
            'accepted_stage_turns':sum(bool(r.get('passed')) for r in relevant),
            'stage_assessed_turns':len(assessed),'stage_failed_turns':sum(r['passed'] is False for r in assessed),
            'unassessed_handoffs':[i for i in range(1,handed_off_turns+1) if i not in {r['turn'] for r in assessed}],
            'stage_results':relevant,'recall_probes':probes,'unreached_probe_status':'N/A',
            'terminal_acceptance':terminal_result if terminal_applicable else None,
            'terminal_diagnostic_only':terminal_result if not terminal_applicable else None,
            'terminal_requirements_received':terminal_applicable,'memory_measurement_calibrated':calibrated,
            'calibration_status':'PASSED' if calibrated else 'BLOCKED_REFERENCE_OR_BUDGET_CALIBRATION',
            'note':'A handoff is not a passing stage. A bounded partial run is not a measured long-term recall failure.'}


def require_calibration(record):
    if not record.get('memory_measurement_calibrated'):
        raise RepoError('CONTINUOUS_MEMORY_NOT_CALIBRATED','Require a full-spec reference and a sequential, no-future-turn execution with measured complete Usage under the frozen budget before claiming long-term Memory scores.')
