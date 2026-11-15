"""Versioned local NDJSON IPC with per-run audience credentials.
Only trusted callers register model tool UIDs; SUBMIT cannot mint a tool call.
"""
from __future__ import annotations
import asyncio
import base64
import contextlib
import json
import secrets
import socket
import socketserver
import threading
from .models import OperationRequest
from .util import canonical, RepoError, now, digest

class Tokens:
    def __init__(self,store):self.store=store
    def issue(self,run_id,audience="worker",ttl=3600):
        payload={"run_id":run_id,"audience":audience,"expires":now()+ttl,"nonce":secrets.token_hex(16)}
        return base64.urlsafe_b64encode(canonical(payload)).decode()+"."+self.store.sign(payload)
    def verify(self,token,run_id,audience):
        try:
            raw,sig=token.rsplit(".",1);p=json.loads(base64.urlsafe_b64decode(raw))
            valid=self.store.verify(p,sig) and p["expires"]>now() and p["run_id"]==run_id and p["audience"]==audience
        except (ValueError,KeyError,TypeError):valid=False
        if not valid:raise RepoError("UNAUTHORIZED")

class BrokerServer:
    def __init__(self,broker,host="127.0.0.1",port=0,drop_after_result=False):
        if host!="127.0.0.1":raise RepoError("LOOPBACK_ONLY")
        self.broker=broker;self.tokens=Tokens(broker.store);self.drop_after_result=drop_after_result
        outer=self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(30)
                while True:
                    line=self.rfile.readline(1_048_577)
                    if not line:return
                    if len(line)>1_048_576:return
                    try:
                        request=json.loads(line);method=request["method"];params=request["params"];run_id=params["run_id"]
                        audience="control" if method in {"CONTROL","GET_RUN","RESOLVE_CONTROL"} else "worker"
                        outer.tokens.verify(request["token"],run_id,audience)
                        if method=="SUBMIT":
                            if params["request"]["run_id"]!=run_id:raise RepoError("SCOPE_DENIED")
                            result=asyncio.run(outer.broker.submit(OperationRequest.model_validate(params["request"])))
                        elif method=="GET_OPERATION":result=outer.broker.get(params["operation_key"],run_id)
                        elif method=="GET_RUN":
                            r=outer.broker.runtime.run(run_id);result={"id":r["id"],"state":r["state"],"revision":r["revision"],"control_epoch":r["control_epoch"]}
                        elif method=="CONTROL":result=outer.broker.runtime.admit(run_id,params["event_id"],params["kind"],params.get("text",""))
                        elif method=="RESOLVE_CONTROL":
                            with contextlib.closing(outer.broker.store.connect()) as db:
                                ev=db.execute("SELECT run_id FROM control WHERE id=?",(params["event_id"],)).fetchone()
                            if not ev or ev[0]!=run_id:raise RepoError("SCOPE_DENIED")
                            result=outer.broker.runtime.finish_control(params["event_id"],params["classification"])
                        else:raise RepoError("UNKNOWN_METHOD")
                        if method=="SUBMIT" and params["request"]["run_id"]!=run_id:raise RepoError("SCOPE_DENIED")
                        if method=="SUBMIT" and outer.drop_after_result:
                            outer.drop_after_result=False;return
                        response={"id":request.get("id"),"result":result}
                    except Exception as exc:response={"id":locals().get("request",{}).get("id"),"error":{"code":getattr(exc,"code","INVALID_REQUEST")}}
                    self.wfile.write(canonical(response)+b"\n");self.wfile.flush()
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address=True;daemon_threads=True
        self.server=Server((host,port),Handler);self.thread=None
    @property
    def address(self):return self.server.server_address
    def start(self):
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();return self
    def close(self):
        self.server.shutdown();self.server.server_close()
        if self.thread:self.thread.join(timeout=3)
    def __enter__(self):return self.start()
    def __exit__(self,*_):self.close()

class BrokerClient:
    def __init__(self,address,token,run_id):self.address,self.token,self.run_id=address,token,run_id
    def request(self,method,params):
        msg={"id":secrets.token_hex(8),"method":method,"params":{"run_id":self.run_id,**params},"token":self.token}
        with socket.create_connection(self.address,timeout=30) as sock:
            sock.sendall(canonical(msg)+b"\n")
            with sock.makefile("rb") as f:line=f.readline(1_048_577)
        if not line:raise RepoError("RESPONSE_LOST","Query the original operation; do not invent a new key")
        out=json.loads(line)
        if "error" in out:raise RepoError(out["error"]["code"])
        return out["result"]
    def submit(self,request):return self.request("SUBMIT",{"request":request.model_dump()})
    def get(self,key):return self.request("GET_OPERATION",{"operation_key":key})
