
import asyncio
import json

import pytest

from repo_code.commands import command_evidence
from repo_code.context import groups
from repo_code.models import Assistant, Budget, ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.task_progress import TaskProgress
from repo_code.workspace import Workspace
from test_memory_working_evidence import app_fixture, cover, tool as memory_tool
from test_review_workflow import setup, tool


@pytest.mark.parametrize(("argv", "kind", "scope", "outcome"), [
    (["python", "-c", "print(1)"], "BEHAVIOR_PROBE", None, "OBSERVED"),
    (["python", "-c", "assert True"], "ASSERTION_CHECK", None, "PASSED"),
    (["pytest", "-k", "small"], "TEST_EXECUTION", "FOCUSED", "PASSED"),
    (["python", "-m", "pytest", "tests/test_grid.py"], "TEST_EXECUTION", "FOCUSED", "PASSED"),
    (["sh", "-c", "pytest || true"], "TEST_EXECUTION", "AS_INVOKED", "UNCERTAIN"),
    (["sh", "-c", "pytest | tail"], "TEST_EXECUTION", "AS_INVOKED", "UNCERTAIN"),
])
def test_local_evidence_distinguishes_scope_and_shell_status(argv,kind,scope,outcome):
    record = command_evidence(argv,{"exit_code":0,"stdout":"3 passed"})
    assert (record["evidence_kind"],record["scope"],record["outcome"]) == (kind,scope,outcome)


@pytest.mark.asyncio
async def test_projection_reuses_scan_and_preserves_raw_goal(tmp_path,monkeypatch):
    app,run = app_fixture(tmp_path)
    run_id = run["id"]
    try:
        content = "".join(f"line_{index:03d}=public_contract_value_{index:03d}\n" for index in range(160))
        await memory_tool(app,run,"create",{"path":"module.py","content":content},"write")
        await memory_tool(app,run,"read",{"path":"module.py","start":1,"lines":80},"read")
        await cover(app,run)
        raw = app.store.messages(run_id)
        calls = []
        original = Workspace.manifest

        def scan(workspace, **kwargs):
            calls.append(workspace.root)
            return original(workspace,**kwargs)

        monkeypatch.setattr(Workspace,"manifest",scan)
        system,messages,_ = await app.memory.prepare_context(run_id,[])
        assert len(calls) == 1
        assert system.count(run["data"]["goal"]) == 1
        assert all(message.get("content") != run["data"]["goal"] for message in messages)
        assert app.store.messages(run_id) == raw
        groups(messages)
        timing = app.store.events(run_id,trusted=True)[-1]
        assert timing["type"] == "host.context_prepared"
        counts = timing["data"]["counts"]
        assert counts["preparation_worktree_bytes"] == len(content)
        assert counts["fill_attempts"] > 0
        assert timing["data"]["seconds"] == pytest.approx(sum(timing["data"]["stages_seconds"].values()))
    finally:
        app.close()


@pytest.mark.asyncio
async def test_prepare_stops_before_dispatch_with_budget_reason(tmp_path,monkeypatch):
    app,run = setup(tmp_path,True)
    try:
        import repo_code.runtime as runtime_module
        run_id = run["id"]
        started = app.runtime.run(run_id)["data"]["clock_active_since"]
        limit = run["data"]["manifest"]["budget"]["wall_seconds"]
        monkeypatch.setattr(runtime_module,"now",lambda:started+limit+1)
        result = await app.execute(run_id)
        assert app.provider.calls == 0
        assert result["state"] == "PARTIAL"
        stop = result["budget_stop"]
        assert (stop["dimension"],stop["stage"],stop["dispatch_status"]) == ("wall_seconds","prepare","NOT_DISPATCHED")
        assert not app.memory.repo.values("memory_budget")
    finally:
        app.close()


@pytest.mark.asyncio
async def test_model_timeout_preserves_completed_candidate_and_unknown_usage(tmp_path,monkeypatch):
    app,run = setup(tmp_path,True)
    try:
        calls = []

        async def complete(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                return Assistant(tool_calls=[ToolCall(id="write",name="create",arguments={"path":"fixed.py","content":"value=1\n"})],
                                 usage={"input_tokens":10,"output_tokens":2})
            await asyncio.sleep(10)

        original = app.runtime.check_wall

        def remaining(run_id,stage):
            if stage == "dispatch" and calls:
                return 0.001
            return original(run_id,stage)

        monkeypatch.setattr(app.provider,"complete",complete)
        monkeypatch.setattr(app.runtime,"check_wall",remaining)
        result = await app.execute(run["id"])
        assert result["state"] == "PARTIAL" and result["reason"] == "BUDGET_EXHAUSTED"
        assert result["budget_stop"]["dispatch_status"] == "DISPATCHED_OR_UNKNOWN"
        assert result["budget_stop"]["stage"] == "model_wait"
        assert app.runtime.workspace(run).path("fixed.py").read_text() == "value=1\n"
        assert not app.runtime.unresolved(run["id"])
        rows = {row["state"]:row for row in app.memory.repo.values("memory_budget")}
        assert rows["USAGE_UNKNOWN"]["charged_tokens"] > 0
        assert rows["SETTLED"]["charged_tokens"] == 12
    finally:
        app.close()


@pytest.mark.asyncio
async def test_tool_budget_tail_is_paired_without_execution(tmp_path):
    replies = [Assistant(tool_calls=[
        ToolCall(id=str(index),name="create",arguments={"path":f"{index}.py","content":f"n={index}\n"})
        for index in range(3)
    ])]
    app,run = setup(tmp_path,provider=ScriptedProvider(replies))
    try:
        source = tmp_path/"source"
        run = app.new_task("Only the first tool can be admitted",source,authorize_write=True,budget=Budget(model_calls=5,tool_calls=1))
        result = await app.execute(run["id"])
        assert result["budget_stop"]["dimension"] == "tool_calls"
        assert not app.runtime.unresolved(run["id"])
        assert result["state"] == "PARTIAL"
        history = app.store.messages(run["id"])
        assert [m["tool_call_id"] for m in history if m["role"]=="tool"] == ["0","1","2"]
        groups(history)
        workspace = app.runtime.workspace(run)
        assert workspace.path("0.py").is_file() and not workspace.path("2.py").exists()
    finally:
        app.close()


@pytest.mark.asyncio
async def test_current_probe_survives_noise_and_two_local_test_scopes(tmp_path):
    app,run = setup(tmp_path,True)
    try:
        await tool(app,run,"process.run",{"argv":["python","-c","print('coordinates')"]},"probe")
        for i in range(4):
            await tool(app,run,"process.run",{"argv":["ls","-"+"a"*(i+1)]},f"noise-{i}")
        await tool(app,run,"process.run",{"argv":["pytest","-k","small"]},"small")
        await tool(app,run,"process.run",{"argv":["pytest","tests/test_grid.py"]},"grid")
        system,_,_ = await app.memory.prepare_context(run["id"],[])
        line = next(line for line in system.splitlines() if line.startswith("Host runtime status (not task acceptance): "))
        ledger = json.loads(line.split(": ",1)[1])["work_ledger"]
        assert ledger["behavior_probes"][0]["evidence_kind"] == "BEHAVIOR_PROBE"
        assert ledger["verification"]["scope"] == "FOCUSED"
        assert len(ledger["other_current_checks"]) == 1
        await tool(app,run,"create",{"path":"fresh.py","content":"x=2\n"},"fresh")
        _,state = app.memory.reducer.reduce(run["id"])
        assert all(entry.get("validity") != "CURRENT" for entry in state["host_observations"] if entry.get("verification"))
    finally:
        app.close()


@pytest.mark.asyncio
async def test_investigation_cost_survives_an_interleaved_novel_read(tmp_path):
    app,run = setup(tmp_path)
    try:
        tracker = TaskProgress(app.store,app.runtime)
        for index in range(24):
            pair = await tool(app,run,"read",{"path":"a.py","start":1,"lines":5},f"read-{index}")
            tracker.batch(run["id"],f"read-{index}",[pair])
        pair = await tool(app,run,"read",{"path":"a.py","start":6,"lines":5},"next")
        observed = tracker.batch(run["id"],"next",[pair])
        assert observed["operations_in_state"] == 25
        assert observed["investigation_warning"] and observed["new_evidence"]
        assert not observed["stop"]
    finally:
        app.close()

