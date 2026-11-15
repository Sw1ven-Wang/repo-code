"""Malformed model patches must never create uncertain file effects."""
import contextlib
import pytest
from pydantic import ValidationError
from repo_code.models import ToolCall
from repo_code.providers import ScriptedProvider
from repo_code.service import RepoCode
from repo_code.memory.models import MemoryProfile
from repo_code.util import RepoError


@pytest.mark.parametrize('bad',[
    {'kind':'replace','path':'worker.py','old_text':'old','new_text':'new'},
    {'kind':'create','path':'new.py'},
    {'kind':'edit','path':'worker.py','replacements':None},
    {'kind':'edit','path':'worker.py','replacements':[{}]},
    {'kind':'move','source':'worker.py'},
    {'kind':'surprise','path':'worker.py'},
])
def test_invalid_patch_rejected_before_registration(tmp_path,bad):
    src=tmp_path/'source';src.mkdir();(src/'worker.py').write_text('old')
    app=RepoCode(tmp_path/'state',ScriptedProvider(),'local')
    try:
        run=app.new_task('test',src,authorize_write=True)
        with pytest.raises(ValidationError):app.broker.register_call(run['id'],'turn',ToolCall(id='bad',name='patch',arguments={'operations':[{'kind':'create','path':'safe.py','content':'never written'},bad]}))
        with contextlib.closing(app.store.connect()) as c:assert c.execute('SELECT count(*) FROM operations').fetchone()[0]==0
        assert app.runtime.run(run['id'])['state']=='RUNNING'
        assert app.runtime.workspace(run).read('worker.py')['content']=='old'
        assert not (app.runtime.workspace(run).root/'safe.py').exists()
    finally:app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('v2',[False,True])
async def test_patch_observed_version_and_stale_protection(tmp_path,v2):
    src=tmp_path/'source';src.mkdir();(src/'worker.py').write_text('old')
    app=RepoCode(tmp_path/'state',ScriptedProvider(),'local',memory_profile=MemoryProfile(mode='lexical_only') if v2 else None)
    try:
        run=app.new_task('test',src,authorize_write=True)
        call=lambda ident,name,args:app.broker.register_call(run['id'],'turn',ToolCall(id=ident,name=name,arguments=args))
        with pytest.raises(RepoError,match='Read this file'):call('unread','patch',{'operations':[{'kind':'replace','path':'worker.py','content':'new'}]})
        read=await app.broker.submit(call('read','read',{'path':'worker.py'}))
        req=call('good','patch',{'operations':[{'kind':'replace','path':'worker.py','content':'new'}]})
        assert req.arguments['operations'][0]['expected_version']==read['result']['version']
        result=await app.broker.submit(req)
        assert result['status']=='SUCCEEDED'
        stale={'operations':[{'kind':'replace','path':'worker.py','content':'bad','expected_version':read['result']['version']}]}
        if v2:
            with pytest.raises(RepoError,match='Read this file'):call('stale','patch',stale)
        else:
            rejected=await app.broker.submit(call('stale','patch',stale))
            assert rejected['status']=='FAILED' and rejected['result']['code']=='STALE_VERSION'
        assert app.runtime.workspace(run).read('worker.py')['content']=='new'
        assert app.runtime.run(run['id'])['state']=='RUNNING'
    finally:app.close()
