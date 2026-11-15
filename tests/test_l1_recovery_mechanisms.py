import pytest

from repo_code.commands import probe_assertion_argv
from repo_code.providers import ScriptedProvider
from test_candidate_review import action
from test_review_workflow import setup, tool


@pytest.mark.asyncio
async def test_anchor_diagnostics_and_same_version_retry(tmp_path):
    app, run = setup(tmp_path)
    try:
        path = 'sample.py'
        original = 'def one():\n    # implementation\n    return 0\n\ndef two():\n    # implementation\n    return 0\n'
        _, create = await tool(app, run, 'create', {'path': path, 'content': original}, 'create')
        version = create['result']['versions'][path]
        args = {'path': path, 'old_text': 'return 0', 'new_text': 'return 1', 'expected_version': version}
        _, first = await tool(app, run, 'edit', args, 'first')
        assert first['result']['code'] == 'AMBIGUOUS_OR_MISSING_MATCH'
        keys = ('path', 'file_version', 'anchor_digest', 'match_count', 'positions', 'positions_truncated')
        evidence = {k: first['result'][k] for k in keys}
        assert evidence['file_version'] == version and evidence['match_count'] == 2
        assert evidence['positions'] == [{'line': 3, 'column': 5}, {'line': 7, 'column': 5}]
        await tool(app, run, 'read', {'path': path}, 'read-again')
        _, retry = await tool(app, run, 'patch', {
            'operations': [{'kind': 'edit', **args, 'new_text': 'return 2'}],
        }, 'retry')
        assert retry['result']['code'] == 'EDIT_ANCHOR_RETRY_BLOCKED'
        assert retry['result']['executed'] is False and {k: retry['result'][k] for k in keys} == evidence
        _, expanded = await tool(app, run, 'edit', {
            **args, 'old_text': '    # implementation\n    return 0',
        }, 'expanded')
        assert expanded['result']['same_match_regions']
        assert expanded['result']['read_windows'][0] == {'path': path, 'start': 1, 'lines': 45}
        file = app.runtime.workspace(run).path(path)
        assert file.read_text() == original

        _, absent = await tool(app, run, 'edit', {**args, 'old_text': 'no such span'}, 'absent')
        assert absent['result']['match_count'] == 0 and not absent['result']['positions']
        _, specific = await tool(app, run, 'edit', {
            **args, 'old_text': 'def one():\n    # implementation\n    return 0',
            'new_text': 'def one():\n    # implementation\n    return 1',
        }, 'specific')
        _, next_version = await tool(app, run, 'edit', {
            **args, 'expected_version': specific['result']['versions'][path], 'new_text': 'return 2',
        }, 'different-version')
        assert next_version['result']['versions'][path] != version
        assert file.read_text() == original.replace('return 0', 'return 1', 1).replace('return 0', 'return 2')
    finally:
        app.close()


def test_probe_replay_requires_nontrivial_assertion():
    probe = ['python', '-c', 'from package import case\nprint(case(7), case(0))']
    for bad in ['print(observed)', 'assert True', 'assert observed == observed',
                'observed = None\nassert observed is None', 'assert (observed := 1)']:
        assert probe_assertion_argv(probe, ['python', '-c', bad]) is None
    assert probe_assertion_argv(['python', '-c', 'import pathlib; print(pathlib.Path.cwd())'],
                                ['python', '-c', 'assert observed']) is None
    cmd = probe_assertion_argv(probe, ['python', '-c', 'assert observed == (7, 0)'])
    assert cmd[0] == 'python' and '(case(7), case(0))' in cmd[2]
    assert 'raise AssertionError' in cmd[2]

    ordered = ['python', '-c', 'from package import case\nx = 0\nprint(x)\nx += 1\nprint(x, x + 1)']
    checked = probe_assertion_argv(ordered, ['python', '-c', 'assert observed == (0, (1, 2))'])
    import types
    from unittest.mock import patch
    package = types.ModuleType('package')
    package.case = lambda x: x
    with patch.dict('sys.modules', {'package': package}):
        exec(checked[2], {})
        wrong = probe_assertion_argv(ordered, ['python', '-c', 'assert observed[0] == 1'])
        with pytest.raises(AssertionError):
            exec(wrong[2], {})
    assert probe_assertion_argv(['python', '-c', 'import package\nif True: print(0)\nprint(1)'],
                                ['python', '-c', 'assert observed']) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('memory', [False, True])
async def test_review_binds_probe_and_rechecks_after_write(tmp_path, memory):
    prompts, executed = [], []
    observed = {}
    script = ['python', '-c', 'from package import calculate\nprint(calculate(7))\nprint(42)\nprint(84)']

    def response(system, messages, tools, index):
        prompts.append(system)
        if index == 0:
            return action('start', 'create', path='changed.py', content='value = 0\n')
        if index == 1:
            return action('observe', 'process.run', argv=script)
        if index == 2:
            observed['ref'] = app.store.get('probe_fixture', run['id'])['ref']
            return action('first-fix', 'edit', path='changed.py', old_text='value = 0', new_text='value = 1')
        if index in (3, 6, 8):
            return {'text': 'Candidate ready.'}
        if index in (4, 7):
            return action(f'check-{index}', 'process.run', check_probe=observed['ref'],
                          argv=['python', '-c', 'assert observed == (1, 42, 84)'])
        if index == 5:
            return action('further-write', 'create', path='adjacent.py', content='value = 2\n')
        raise AssertionError(f'unexpected model request {index}')

    app, run = setup(tmp_path, memory, ScriptedProvider(handler=response))
    base_process = app.sandbox.run

    async def process(run_id, folder, argv, stdin, timeout, *args, **kwargs):
        result = await base_process(run_id, folder, argv, stdin, timeout, *args, **kwargs)
        executed.append(argv)
        if argv == script:
            result['stdout'] = '0\n42\n84\n'
        elif argv[0] == 'python':
            assert '= calculate(7)' in argv[2]
            # Execute the actual Host-generated script against this candidate.
            import types
            from unittest.mock import patch
            package = types.ModuleType('package')
            package.calculate = lambda number: int((folder / 'changed.py').read_text().split('=')[1])
            with patch.dict('sys.modules', {'package': package}):
                try:
                    import io
                    from contextlib import redirect_stdout
                    output = io.StringIO()
                    with redirect_stdout(output):
                        exec(argv[2], {})
                    result['stdout'] = output.getvalue()
                except AssertionError:
                    result.update(exit_code=1, stderr='AssertionError\n')
        return result

    original_dispatch = app.broker._execute

    async def dispatch(req, operation):
        result = await original_dispatch(req, operation)
        if result.get('observed_probe'):
            app.store.put('probe_fixture', run['id'], {'ref': result['observed_probe']})
        return result

    app.broker._execute = dispatch
    app.sandbox.run = process
    try:
        result = await app.execute(run['id'])
        review = result['candidate_review']
        assert review['status'] == 'NEW_CHECK_PASSED'
        assert review['required_probes'] == [observed['ref']] and not review['pending_probes']
        assert observed['ref'] in prompts[4] and observed['ref'] in prompts[7]
        assert '"check_probe": "' + observed['ref'] in prompts[4]
        assert '0\\n42\\n84\\n' in prompts[4] and '[1, 1, 1]' in prompts[4]
        assert sum(argv[0] == 'python' for argv in executed) == 3
        assert result['delivery'] is None
    finally:
        app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('probe_count', [1, 2])
async def test_review_ends_when_model_adds_only_a_summary(tmp_path, probe_count):
    prompts = []

    def respond(system, messages, tools, index):
        prompts.append(system)
        if index == 0:
            return action('write', 'create', path='changed.py', content='value = 1\n')
        if index <= probe_count:
            return action(f'probe-{index}', 'process.run',
                          argv=['python', '-c', f'from project import value\nprint(value({index}))'])
        if index == probe_count + 1:
            return {'text': 'Ready; local output was printed.'}
        if index == probe_count + 2:
            return {'text': 'The same outcome. No additional check.'}
        raise AssertionError('An unchanged review must not dispatch again')

    app, run = setup(tmp_path, provider=ScriptedProvider(handler=respond))
    try:
        result = await app.execute(run['id'])
        review = result['candidate_review']
        assert result['state'] == 'PARTIAL' and result['delivery'] is None
        assert result['reason'] == 'CANDIDATE_REVIEW_INCOMPLETE'
        assert review['status'] == 'NO_NEW_EVIDENCE'
        assert len(review['pending_probes']) == probe_count
        assert app.provider.calls == probe_count + 3
        assert f'Pending probe count: {probe_count}.' in prompts[-1]
        assert review['pending_probes'][0]['operation'] in prompts[-1]
        if probe_count == 2:
            assert review['pending_probes'][1]['operation'] not in prompts[-1].split('Focused probe (tool data): ')[1]
        events = [event['type'] for event in app.store.events(run['id'], trusted=True)]
        assert events.count('candidate.review_requested') == events.count('candidate.review_finished') == 1
        assert app.runtime.workspace(run).path('changed.py').is_file()
    finally:
        app.close()
