"""Explicit MCP 2025-06-18 stdio client. Newer protocol eras are not silently assumed."""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import signal
import sys
from pathlib import Path
from .util import RepoError, uid, now, digest

def validate_schema(value,schema):
    # Deliberately supported subset; never silently ignore constraint keywords.
    if not isinstance(schema,dict):raise RepoError("MCP_SCHEMA_UNSUPPORTED")
    allowed={"type","properties","required","additionalProperties","items","enum","description","title","default","$schema","minimum","maximum","minLength","maxLength","minItems","maxItems","pattern"}
    if set(schema)-allowed:raise RepoError("MCP_SCHEMA_UNSUPPORTED",str(sorted(set(schema)-allowed)))
    typ=schema.get("type")
    if typ is not None and typ not in {"object","array","string","integer","number","boolean","null"}:raise RepoError("MCP_SCHEMA_UNSUPPORTED")
    if schema.get("additionalProperties",True) not in (True,False):raise RepoError("MCP_SCHEMA_UNSUPPORTED")
    types={"object":dict,"array":list,"string":str,"integer":int,"number":(int,float),"boolean":bool,"null":type(None)}
    if typ in types and (not isinstance(value,types[typ]) or typ in {"number","integer"} and isinstance(value,bool)):raise RepoError("MCP_ARGUMENT_TYPE")
    if "enum" in schema and value not in schema["enum"]:raise RepoError("MCP_ARGUMENT_ENUM")
    if isinstance(value,(float,int)) and not isinstance(value,bool):
        if "minimum" in schema and value<schema["minimum"]:raise RepoError("MCP_ARGUMENT_RANGE")
        if "maximum" in schema and value>schema["maximum"]:raise RepoError("MCP_ARGUMENT_RANGE")
    if isinstance(value,str):
        if len(value)<schema.get("minLength",0) or len(value)>schema.get("maxLength",1_048_576):raise RepoError("MCP_ARGUMENT_LENGTH")
        if "pattern" in schema:
            # Regex execution is not accepted in the privileged Host without a bounded engine.
            raise RepoError("MCP_SCHEMA_UNSUPPORTED","pattern requires a bounded validator")
    if isinstance(value,list):
        if len(value)<schema.get("minItems",0) or len(value)>schema.get("maxItems",10000):raise RepoError("MCP_ARGUMENT_LENGTH")
    if isinstance(value,dict):
        if not set(schema.get("required",[]))<=value.keys():raise RepoError("MCP_ARGUMENT_REQUIRED")
        if schema.get("additionalProperties") is False and not value.keys()<=schema.get("properties",{}).keys():raise RepoError("MCP_ARGUMENT_EXTRA")
        for k,v in value.items():
            if k in schema.get("properties",{}):validate_schema(v,schema["properties"][k])
    if isinstance(value,list):
        for v in value:validate_schema(v,schema.get("items",{}))

class MCPClient:
    def __init__(self,sandbox,workspace,argv,run_id):
        self.sandbox,self.workspace,self.argv,self.run_id=sandbox,Path(workspace),argv,run_id
        self.process=None;self.root=None;self.lock=asyncio.Lock();self.seq=0;self.tools={};self.lease=uid("mcp-")
    async def start(self):
        if self.sandbox.mode!="native":raise RepoError("MCP_ISOLATED_STDIO_UNAVAILABLE","native Linux backend required for this adapter")
        self.lease=uid("mcp-");self.tools={}
        self.root,user=await asyncio.to_thread(self.sandbox._jail,self.workspace,True)
        argv=list(self.argv)
        if argv[0] in {"python","python3"}:argv[0]="/usr/bin/python3"
        cfg={"root":str(self.root),"uid":user,"argv":argv,"cpu_seconds":60,"memory_bytes":512*1024*1024,"max_output":4*1024*1024}
        self.sandbox.store.put("lease",self.lease,{"id":self.lease,"run_id":self.run_id,"state":"RESERVED","mode":"mcp","protocol":1})
        self.process=await asyncio.create_subprocess_exec(sys.executable,"-I","-S",str(Path(__file__).with_name("sandbox_entry.py")),json.dumps(cfg),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,start_new_session=True,limit=1_048_576,env={"PATH":"/usr/bin:/bin"})
        self.sandbox.store.put("lease",self.lease,{"id":self.lease,"run_id":self.run_id,"state":"OWNED","pid":self.process.pid,"mode":"mcp","protocol":1})
        self.sandbox._external[self.lease]={"process":self.process,"root":self.root}
        result=await self._request("initialize",{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"repo-code","version":"1.5.1"}})
        if result.get("protocolVersion") not in {"2025-06-18","2025-03-26"}:await self.close();raise RepoError("MCP_PROTOCOL_UNSUPPORTED")
        await self._send({"jsonrpc":"2.0","method":"notifications/initialized"})
        cursor=None
        for _ in range(20):
            page=await self._request("tools/list",{"cursor":cursor} if cursor else {})
            for t in page.get("tools",[]):
                if t["name"] in self.tools:raise RepoError("MCP_DUPLICATE_TOOL")
                self.tools[t["name"]]=t
            cursor=page.get("nextCursor")
            if not cursor:break
        else:raise RepoError("MCP_PAGINATION_LIMIT")
        return self
    async def _send(self,msg):
        if not self.process or not self.process.stdin:raise RepoError("MCP_CLOSED")
        self.process.stdin.write((json.dumps(msg)+"\n").encode());await self.process.stdin.drain()
    async def _request(self,method,params,timeout=15,operation_id=None):
        async with self.lock:
            self.seq+=1;rid=self.seq
            if not self.process or not self.process.stdin or self.process.returncode is not None:
                raise RepoError("MCP_CLOSED")  # Known pre-send failure.
            effectful = method == "tools/call"
            key = digest([self.run_id, self.lease, rid])
            record = {"id":key, "run_id":self.run_id, "lease_id":self.lease,
                      "operation_id":operation_id, "rpc_id":rid, "method":method,
                      "params_digest":digest(params), "state":"PREPARED"}
            def persist(state, **extra):
                record.update(state=state, **extra)
                self.sandbox.store.put("mcp_request", key, record)
            persist("PREPARED")
            try:
                async with asyncio.timeout(timeout):
                    # A failed drain/write may still have delivered a prefix or
                    # the entire call. Persist intent BEFORE handing bytes over.
                    persist("SEND_STARTED")
                    await self._send({"jsonrpc":"2.0","id":rid,"method":method,"params":params})
                    persist("SENT")
                    for _ in range(100):
                        line=await self.process.stdout.readline()
                        if not line:raise RepoError("MCP_DISCONNECTED")
                        msg=json.loads(line)
                        if not isinstance(msg,dict) or msg.get("jsonrpc") != "2.0":
                            raise RepoError("MCP_INVALID_RESPONSE")
                        if msg.get("id")!=rid:continue
                        if "error" in msg:raise RepoError("MCP_SERVER_ERROR")
                        if "result" not in msg or not isinstance(msg["result"],dict):
                            raise RepoError("MCP_INVALID_RESPONSE")
                        persist("COMPLETED", result_digest=digest(msg["result"]))
                        return msg["result"]
                    raise RepoError("MCP_NOTIFICATION_LIMIT")
            except asyncio.CancelledError:
                persist("UNCERTAIN" if effectful else "INTERRUPTED", error="CANCELLED")
                raise
            except Exception as exc:
                code=getattr(exc,"code",type(exc).__name__)
                persist("UNCERTAIN" if effectful else "FAILED", error=code)
                if effectful:
                    # Includes EOF, partial write, timeout, malformed response,
                    # and server errors without a proven side-effect outcome.
                    raise RepoError("MCP_EFFECT_UNKNOWN",
                        f"{code} after send attempt; query the original Operation, never replay blindly") from exc
                if isinstance(exc,RepoError):raise
                raise RepoError("MCP_TRANSPORT_ERROR",code) from exc
    async def call(self,tool,arguments,operation_id=None):
        spec=self.tools.get(tool)
        if not spec:raise RepoError("MCP_UNKNOWN_TOOL")
        validate_schema(arguments,spec["inputSchema"])
        return await self._request("tools/call",{"name":tool,"arguments":arguments},operation_id=operation_id)
    async def close(self):
        if self.process and self.process.returncode is None:
            try:os.killpg(self.process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            await self.process.wait()
        if self.root:shutil.rmtree(self.root,ignore_errors=True)
        self.sandbox.store.put("lease",self.lease,{"id":self.lease,"run_id":self.run_id,"state":"STOPPED","mode":"mcp","protocol":1,"result":{"running":False,"cancelled":True}})
        self.sandbox._external.pop(self.lease,None);self.process=None;self.root=None
