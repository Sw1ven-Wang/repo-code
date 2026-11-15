"""All generated Python executes after OS isolation, including after promotion."""
import json
import tempfile
from pathlib import Path
from .util import RepoError, atomic_write

WORKER='''import json,sys,importlib.util,inspect,asyncio
p=json.loads(sys.stdin.read());spec=importlib.util.spec_from_file_location("evolved","/workspace/policy.py");m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
r=m.handle(p)
if inspect.isawaitable(r):r=asyncio.run(r)
print(json.dumps(r,ensure_ascii=False,allow_nan=False))
'''
class EvolvedWorker:
    def __init__(self,store,sandbox):self.store,self.sandbox=store,sandbox
    async def invoke(self,component,payload,run_id="evolved",timeout=10):
        if component.get("kind")=="config":return component
        if component.get("kind")!="python":raise RepoError("UNKNOWN_COMPONENT_KIND")
        if not self.sandbox.isolated:raise RepoError("ISOLATION_REQUIRED")
        with tempfile.TemporaryDirectory(prefix="worker-input-",dir=self.store.root) as tmp:
            atomic_write(Path(tmp)/"policy.py",component["source"].encode())
            r=await self.sandbox.run(run_id,Path(tmp),["python","-I","-c",WORKER],stdin=json.dumps(payload),timeout=timeout,export=False,readonly=True)
        if r["exit_code"]!=0 or r["timeout"]:raise RepoError("EVOLVED_WORKER_FAILED",r["stderr"][-1000:])
        if len(r["stdout"])>65536:raise RepoError("EVOLVED_OUTPUT_LIMIT")
        try:
            output=json.loads(r["stdout"])
            if not isinstance(output,dict):raise ValueError()
            return output
        except ValueError:raise RepoError("EVOLVED_PROTOCOL_ERROR") from None

    async def converse(self,component,payload,provider,max_model_calls=3,run_id='optimizer-worker'):
        """Fixed capability protocol for evolvable algorithms.

        An isolated handle can request a bounded model-only operation. Host owns
        the provider/credentials and rejects tools or arbitrary broker methods.
        State is explicit JSON; fresh sandbox invocations never share authority.
        """
        current=payload
        for i in range(max_model_calls+1):
            out=await self.invoke(component,current,run_id)
            request=out.get('model_request')
            if request is None:return out.get('result',out)
            if i==max_model_calls:raise RepoError('WORKER_MODEL_BUDGET')
            if not isinstance(request,dict) or set(request)-{'system','messages'}:raise RepoError('WORKER_CAPABILITY_DENIED')
            if len(json.dumps(request))>64000:raise RepoError('WORKER_REQUEST_LIMIT')
            messages=request.get('messages',[])
            if not isinstance(messages,list) or any(not isinstance(m,dict) or set(m)-{'role','content'} or m.get('role') not in {'user','assistant'} or not isinstance(m.get('content'),str) for m in messages):raise RepoError('WORKER_REQUEST_SCHEMA')
            result=await provider.complete(str(request.get('system',''))[:16000],messages,[])
            if result.tool_calls:raise RepoError('WORKER_CAPABILITY_DENIED')
            current={'operation':payload.get('operation'),'input':payload,'continuation':out.get('state'),
                     'model_result':{'text':result.text,'usage':result.usage}}
        raise RepoError('WORKER_MODEL_BUDGET')
