import pytest

from repo_code.context import groups
from repo_code.models import Budget
from repo_code.providers import ScriptedProvider
from test_review_workflow import setup


def action(call_id, name, **arguments):
    return {'tool_calls': [{'id': call_id, 'name': name, 'arguments': arguments}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('memory', [False, True])
async def test_handoff_requests_a_distinct_boundary_check(tmp_path, memory):
    replies = [action('write', 'create', path='changed.py', content='value = 1\n'),
               action('focused', 'process.run', argv=['pytest', '-k', 'requested']),
               {'text': 'Ready with a passing focused test.'},
               action('boundary', 'process.run', argv=['pytest', '-k', 'unchanged_neighbor']),
               {'text': 'The requested and neighboring cases passed.'}]
    prompts = []

    def respond(system, messages, tools, index):
        prompts.append(system)
        return replies[index]

    app, run = setup(tmp_path, memory, ScriptedProvider(handler=respond))
    try:
        result = await app.execute(run['id'])
        assert result['state'] == 'PARTIAL' and result['delivery'] is None
        assert result['reason'].startswith('CANDIDATE_READY_EXTERNAL_EVALUATION')
        assert result['candidate_review']['status'] == 'NEW_CHECK_PASSED'
        assert len(result['candidate_review']['new_passing_checks']) == 1
        assert 'alternate input form' in prompts[0] and 'public entry point' in prompts[0]
        assert 'provisional handoff' in prompts[3] and 'counterexample' in prompts[3]
        events = [e['type'] for e in app.store.events(run['id'], trusted=True)]
        assert events.count('candidate.review_requested') == events.count('candidate.review_finished') == 1
        groups(app.store.messages(run['id']))
    finally:
        app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case, expected', [('duplicate', 'NO_NEW_PASSING_CHECK'),
                                            ('stale', 'NO_NEW_PASSING_CHECK'), ('failure', 'CHECK_FAILED')])
async def test_duplicate_stale_and_failed_checks_do_not_close_review(tmp_path, case, expected):
    selector = 'requested' if case == 'duplicate' else 'boundary'
    replies = [action('write', 'create', path='changed.py', content='value = 1\n'),
               action('focused', 'process.run', argv=['pytest', '-k', 'requested']),
               {'text': 'Ready.'}, action('second', 'process.run', argv=['pytest', '-k', selector])]
    if case == 'stale':
        replies.append(action('write-again', 'create', path='other.py', content='value = 2\n'))
    replies.append({'text': 'Handoff.'})
    if case == 'failure':
        replies.append({'text': 'The focused failure remains unresolved.'})
    app, run = setup(tmp_path, provider=ScriptedProvider(replies))
    try:
        if case == 'failure':
            original = app.sandbox.run

            async def run_with_failure(rid, folder, argv, *args, **kwargs):
                result = await original(rid, folder, argv, *args, **kwargs)
                if argv[-1] == 'boundary':
                    result.update(stdout='1 failed\n', exit_code=1)
                return result

            app.sandbox.run = run_with_failure
        result = await app.execute(run['id'])
        assert result['state'] == 'PARTIAL' and result['reason'] == 'CANDIDATE_REVIEW_INCOMPLETE'
        assert result['candidate_review']['status'] == expected
        assert not result['candidate_review']['new_passing_checks']
        assert app.runtime.workspace(run).path('changed.py').is_file()
        assert not app.runtime.unresolved(run['id'])
        assert app.provider.calls == len(replies)
    finally:
        app.close()


@pytest.mark.asyncio
async def test_review_preserves_candidate_when_no_calls_remain(tmp_path):
    provider = ScriptedProvider([action('write', 'create', path='changed.py', content='value = 1\n'),
                                 {'text': 'Ready.'}])
    app, _ = setup(tmp_path, provider=provider)
    try:
        run = app.new_task('Change behavior', tmp_path/'source', authorize_write=True, authorize_process=True,
                           budget=Budget(model_calls=2, tool_calls=3))
        result = await app.execute(run['id'])
        assert result['reason'] == 'CANDIDATE_REVIEW_INCOMPLETE'
        assert result['candidate_review']['status'] == 'INSUFFICIENT_BUDGET'
        assert app.runtime.workspace(run).path('changed.py').is_file()
        assert provider.calls == 2
    finally:
        app.close()
