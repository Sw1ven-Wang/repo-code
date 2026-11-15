import json

from repo_code.live_compact import LiveCompactProfile
from repo_code.models import ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.util import canonical, digest


def records(count=8, size=5400, result_changes=None):
    rows=[{'id':'1','operation_id':None,'data':{'role':'user','content':'Keep the public API unchanged.'},'references_verified':True}]
    for i in range(count):
        body={'path':f'f{i}.py','version':digest(str(i)),'content':f'# file {i}\n'+'value = 42\n'*(size//11),'is_error':False}
        body.update(result_changes or {})
        rows.extend([
            {'id':str(2*i+2),'operation_id':None,'references_verified':True,
             'data':{'role':'assistant','content':'Read source; this is not verified delivery.',
                     'tool_calls':[{'id':f'c{i}','name':'read','arguments':{'path':f'f{i}.py'}}]}},
            {'id':str(2*i+3),'operation_id':f'op{i}','operation_state':'SUCCEEDED','references_verified':True,
             'data':{'role':'tool','tool_call_id':f'c{i}','content':canonical(body).decode(),'is_error':False}}
        ])
    return rows


async def history(tmp_path, *, memory_profile=None, count=8, provider=None, mode='live-experimental', profile=None, budget=None, read_lines=500):
    source=tmp_path/'source';source.mkdir()
    for i in range(count):
        (source/f'f{i}.py').write_text(f'# file {i}\n'+'value = 42\n'*490,encoding='utf-8')
    app=RepoCode(tmp_path/'host',provider or ScriptedProvider(),sandbox='local',memory_profile=memory_profile,
                 context_compaction=mode,live_compact_profile=profile)
    run=app.new_task('Keep the public API unchanged.',source,authorize_write=True,budget=budget)
    for i in range(count):
        call=ToolCall(id=f'c{i}',name='read',arguments={'path':f'f{i}.py','lines':read_lines})
        app.store.append_message(run['id'],{'role':'assistant','content':'','tool_calls':[call.model_dump(exclude_none=True)]})
        request=app.broker.register_call(run['id'],f't{i}',call)
        receipt=await app.broker.submit(request)
        assert receipt['status']=='SUCCEEDED'
        app.broker.append_result(request,receipt)
    return app,run


def source_hashes(app,run_id):
    import contextlib
    with contextlib.closing(app.store.connect()) as c:
        return digest([[tuple(r) for r in c.execute(sql,(run_id,))] for sql in (
            'SELECT id,operation_id,data FROM messages WHERE run_id=? ORDER BY id',
            'SELECT * FROM operations WHERE run_id=? ORDER BY id')])


def low_profile():
    return LiveCompactProfile(trigger_ratio=0.1,forecast_ratio=0.2).model_dump()
