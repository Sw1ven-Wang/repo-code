"""Fault-inject the actual G4 probe; no external network is used here."""
import builtins
import contextlib
import errno
import io
import os
import socket
from types import SimpleNamespace

import pytest

from repo_code.regression import CandidateRegression
from repo_code.util import RepoError


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,create_errno,connect_errno,interfaces,uid,secret_readable,passes',[
    ('native',errno.EPERM,None,['lo'],1000,False,True),
    ('native',None,errno.ENETUNREACH,['lo'],1000,False,False),
    ('native',errno.EMFILE,None,['lo'],1000,False,False),
    ('docker',None,errno.ENETUNREACH,['lo'],1000,False,True),
    ('docker',None,None,['lo'],1000,False,False),
    ('docker',None,errno.ETIMEDOUT,['lo'],1000,False,False),
    ('docker',None,errno.ECONNREFUSED,['lo'],1000,False,False),
    ('docker',None,errno.ENETUNREACH,['lo','eth0'],1000,False,False),
    ('docker',None,errno.ENETUNREACH,['lo'],1000,True,False),
    ('docker',None,errno.ENETUNREACH,['lo'],0,False,False),
])
async def test_boundary_probe_rejects_breaches_and_inconclusive_network(
        tmp_path,monkeypatch,mode,create_errno,connect_errno,interfaces,uid,secret_readable,passes):
    closed=[]

    class ProbeSocket:
        def __init__(self):
            if create_errno is not None:
                raise OSError(create_errno,'injected socket creation failure')
        def settimeout(self,value):
            assert value>0
        def connect(self,address):
            if connect_errno is not None:
                raise OSError(connect_errno,'injected connection failure')
        def close(self):
            closed.append(True)

    async def run(run_id,folder,argv,**kwargs):
        assert kwargs=={'export':False,'readonly':True}
        # Execute the production-generated probe with controlled OS outcomes,
        # rather than feeding fabricated observation JSON to the validator.
        with monkeypatch.context() as patch:
            patch.setattr(socket,'socket',ProbeSocket)
            patch.setattr(os,'getuid',lambda:uid,raising=False)
            patch.setattr(os,'listdir',lambda path:interfaces)
            def read_secret(*args,**kw):
                if secret_readable:
                    return io.BytesIO(b'test-only signing key')
                raise PermissionError(errno.EACCES,'injected host secret denial')
            patch.setattr(builtins,'open',read_secret)
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(compile(argv[2],'<g4-isolation-probe>','exec'),{})
        return {'exit_code':0,'stdout':output.getvalue()}

    regression=CandidateRegression(None)
    regression.root=tmp_path
    regression.app=SimpleNamespace(store=SimpleNamespace(root=tmp_path/'state'),
        sandbox=SimpleNamespace(isolated=True,mode=mode,run=run))
    if passes:
        result=await regression.sandbox_boundaries()
        assert result['violations']==[]
        assert result['network_probe']==('socket_creation' if mode=='native' else 'outbound_tcp_connect')
    else:
        with pytest.raises(RepoError,match='G4_ISOLATION_BOUNDARY'):
            await regression.sandbox_boundaries()
    assert bool(closed)==(create_errno is None)
