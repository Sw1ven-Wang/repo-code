import json
from repo_code.models import Budget,ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.context import groups
from repo_code.util import digest

def new_app(tmp_path,*,mode='live-experimental-v2',memory_profile=None,source_lines=490,live_profile=None):
    source=tmp_path/'source';source.mkdir(parents=True)
    (source/'file.py').write_text('counter = 0\n'+''.join(f'# public source line {n} with padding\n' for n in range(source_lines)),encoding='utf-8')
    app=RepoCode(tmp_path/'host',ScriptedProvider(),sandbox='local',context_compaction=mode,
        live_compact_profile=live_profile if live_profile is not None else {'trigger_ratio':0.1,'forecast_ratio':0.15} if mode=='live-experimental' else None,
        memory_profile=memory_profile)
    run=app.new_task('Task 1: preserve public API.',source,conversation=True,authorize_write=True,
        budget=Budget(model_calls=None,tool_calls=10000,wall_seconds=86400))
    return app,run

async def tool(app,rid,n,name='read',arguments=None):
    call=ToolCall(id='call-'+str(n),name=name,arguments=arguments or {'path':'file.py','lines':500})
    app.store.append_message(rid,{'role':'assistant','content':'Tentative hypothesis; not independent success.',
        'tool_calls':[call.model_dump(exclude_none=True)]})
    request=app.broker.register_call(rid,'turn-'+str(n),call);receipt=await app.broker.submit(request)
    app.broker.append_result(request,receipt)
    return receipt

async def closed_history(tmp_path,count=5,size=15000,**kwargs):
    app,run=new_app(tmp_path,**kwargs);rid=run['id']
    for n in range(1,count+1):
        if n>1:app.submit_turn(rid,'stage-'+str(n),'Task '+str(n)+': preserve current interface.')
        assert (await tool(app,rid,n))['status']=='SUCCEEDED'
        app.store.append_message(rid,{'role':'assistant','content':'MODEL_PROPOSED '+('unverified historical discussion; '*(size//34))})
        app.runtime.wait_for_user(rid)
    app.submit_turn(rid,'next-active','Current task: implement without hidden tests.')
    return app,run

def options(available=40000):
    return dict(system='',tools=[],serialize=lambda s,m,t:{'system':s,'messages':m,'tools':t},available_history=available)

def raw_hash(app,rid):
    with app.store.connect() as c:
        return digest([[tuple(r) for r in c.execute(sql,(rid,))] for sql in [
            'SELECT * FROM messages WHERE run_id=? ORDER BY id',
            'SELECT * FROM operations WHERE run_id=? ORDER BY id',
            'SELECT * FROM events WHERE run_id=? ORDER BY sequence']])
