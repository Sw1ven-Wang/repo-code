"""Actual isolated processes, including early successful exit and cancellation."""
import asyncio
import pytest
from conftest import tool
from repo_code.commands import command_evidence
from repo_code.work_status import work_status


def form(kind,code):
    if kind=='inline':return {'argv':['python','-c',code]}
    if kind=='stdin':return {'argv':['python','-'],'stdin':code}
    return {'argv':['bash','-lc',"python - <<'PY'\n"+code+"\nPY"]}


def new(app,source):
    (source/'package.py').write_text('def calculate(n):\n return 7\n')
    return app.new_task('Check actual behavior',source,authorize_write=True,authorize_process=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['inline','stdin','heredoc'])
async def test_host_registered_independent_checks_require_executed_assertions(app,source,kind):
    run=new(app,source)
    for index,(code,expected) in enumerate([
        ('from package import calculate\nassert calculate(7) == 7','PASSED'),
        ('from package import calculate\nassert calculate(7) == 0','FAILED'),
        ('from package import calculate\nif False:\n assert calculate(7) == 0','OBSERVED')]):
        args=form(kind,code)
        _,receipt=await tool(app,run,'process.run',args,'case-'+str(index))
        body=receipt['result']
        assert body.get('independent_check')
        evidence=command_evidence(args['argv'],body,args.get('stdin',''),strict=True,
                                  store=app.store,run=app.runtime.run(run['id']))
        assert evidence['outcome']==expected


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['inline','stdin','heredoc'])
async def test_bound_real_assertion_success_failure_and_replay(app,source,kind):
    run=new(app,source)
    _,obs=await tool(app,run,'process.run',form(kind,'from package import calculate\nprint(calculate(7))'),'observe')
    ref=obs['result']['observed_probe']
    _,bad=await tool(app,run,'process.run',{**form(kind,'assert observed == 0'),'check_probe':ref},'bad')
    assert bad['result']['exit_code']!=0
    args={**form(kind,'assert observed == 7'),'check_probe':ref}
    req,good=await tool(app,run,'process.run',args,'good')
    assert good['result']['assertions_completed']
    ledger=work_status(app.store,app.runtime,app.runtime.run(run['id']))
    assert ledger['verification']['outcome']=='PASSED' and ledger['verification']['validity']=='CURRENT'
    async def forbidden(*a,**kw):raise AssertionError('idempotent replay reexecuted process')
    app.sandbox.run=forbidden
    cached=await app.broker.submit(req)
    assert cached['result']['execution_evidence']==good['result']['execution_evidence']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['inline','stdin','heredoc'])
@pytest.mark.parametrize('failure',['exit-zero','timeout'])
async def test_zero_exit_or_timeout_before_assertion_is_not_verified(app,source,kind,failure):
    run=new(app,source)
    _,obs=await tool(app,run,'process.run',form(kind,'from package import calculate\nprint(calculate(7))'),'observe')
    ref=obs['result']['observed_probe']
    await tool(app,run,'read',{'path':'package.py'},'read')
    code='def calculate(n):\n import os; os._exit(0)\n' if failure=='exit-zero' else 'def calculate(n):\n import time; time.sleep(30); return 7\n'
    await tool(app,run,'edit',{'path':'package.py','old_text':'def calculate(n):\n return 7\n','new_text':code},'edit')
    args={**form(kind,'assert observed == 7'),'check_probe':ref,'timeout':0.3 if failure=='timeout' else 10}
    _,checked=await tool(app,run,'process.run',args,'check')
    body=checked['result']
    result=command_evidence(args['argv'],body,args.get('stdin',''),strict=True,store=app.store)
    assert not result or result['outcome']!='PASSED'
    if failure=='exit-zero':assert body['code']=='PROBE_CHECK_INCOMPLETE'
    else:assert body['timeout']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['stdin','heredoc'])
async def test_cancel_during_bound_check_revokes_its_evidence(app,source,kind):
    run=new(app,source)
    _,obs=await tool(app,run,'process.run',form(kind,'from package import calculate\nprint(calculate(7))'),'observe')
    ref=obs['result']['observed_probe']
    await tool(app,run,'read',{'path':'package.py'},'read')
    await tool(app,run,'edit',{'path':'package.py','old_text':'return 7','new_text':'import time; time.sleep(30); return 7'},'edit')
    args={**form(kind,'assert observed == 7'),'check_probe':ref,'timeout':20}
    pending=asyncio.create_task(tool(app,run,'process.run',args,'cancelled-check'))
    try:
        for _ in range(500):
            if any(v['state']=='OWNED' for v in app.store.values('lease')):break
            await asyncio.sleep(.01)
        else:raise AssertionError('process never became owned')
        app.runtime.admit(run['id'],'cancel','CANCEL')
        _,receipt=await pending
        result=command_evidence(args['argv'],receipt['result'],args.get('stdin',''),strict=True,
                                store=app.store,run=app.runtime.run(run['id']))
        assert not result or result['outcome']!='PASSED'
        assert app.sandbox.resources_stopped(run['id'])
    finally:
        if not pending.done():
            app.sandbox.stop_run(run['id'])
            await pending
