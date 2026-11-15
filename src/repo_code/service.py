from pathlib import Path
from .storage import Store
from .sandbox import Sandbox
from .runtime import Runtime
from .broker import Broker
from .acceptance import Grader
from .governance import Releases
from .agent import CodingAgent
from .providers import model_configuration

class RepoCode:
    """Public composition root shared by CLI, tests and embedding applications."""
    def __init__(self,state_root,provider,sandbox="auto",image=None,on_delta=None,memory_profile=None,memory_encoder=None):
        if hasattr(provider,'assert_state_safe'):provider.assert_state_safe(state_root)
        self.provider=provider
        self.store=Store(Path(state_root));self.sandbox=Sandbox(self.store,sandbox,image)
        self.runtime=Runtime(self.store,self.sandbox);self.releases=Releases(self.store,self.sandbox);self.releases.bootstrap()
        self.broker=Broker(self.store,self.runtime,self.sandbox,provider);self.grader=Grader(self.store,self.sandbox)
        self.agent=CodingAgent(self.store,self.runtime,self.broker,self.grader,self.releases,provider,on_delta)
        self.memory=None
        if memory_profile is not None:
            from .memory.service import MemoryService
            self.memory=MemoryService(self.store,self.runtime,provider,memory_profile,memory_encoder)
            self.runtime.memory=self.broker.memory=self.agent.memory=self.memory
    def new_task(self,goal,workspace,contract=None,budget=None,authorize_write=False,authorize_process=False,visibility="DEV",release=None,knowledge_pin=None,conversation=False,*,session_id=None,task_family=None):
        if hasattr(self.provider,'assert_workspace_safe'):self.provider.assert_workspace_safe(workspace)
        scope=self.session_experiences().get(session_id)['scope'] if session_id else None
        run=self.runtime.create(goal,workspace,release or self.releases.active()["digest"],contract,budget,scope=scope,visibility=visibility,knowledge_pin=knowledge_pin,model_config=model_configuration(self.provider),conversation=conversation)
        if session_id:self.session_experiences().bind(session_id,run['id'],task_family)
        self.broker.authorize_run(run["id"],authorize_write,authorize_process,delegate=True)
        return self.runtime.run(run["id"])
    def submit_turn(self,run_id,turn_id,text):
        from .util import execution_lock
        with execution_lock(self.store.root/'executor_locks'/(run_id+'.lock')):
            return self.runtime.submit_turn(run_id,turn_id,text)
    def finish_conversation(self,run_id):
        from .util import execution_lock
        with execution_lock(self.store.root/'executor_locks'/(run_id+'.lock')):
            return self.runtime.finish_conversation(run_id)
    async def execute(self,run_id,expose_control=False):
        from .util import execution_lock,RepoError
        with execution_lock(self.store.root/"executor_locks"/(run_id+".lock")):
            manifest=self.runtime.run(run_id)["data"]
            if manifest["manifest"].get("memory_schema_version")==2 and (self.memory is None or self.memory.profile_digest!=manifest["manifest"]["memory_profile_digest"]):
                raise RepoError("RUN_MEMORY_CONFIGURATION_CHANGED")
            supplied=model_configuration(self.agent.provider)
            if manifest.get("model_config")!=supplied:raise RepoError("RUN_MODEL_CONFIGURATION_CHANGED","Resume with the original provider/model configuration")
            try:result=await self._execute_owned(run_id,expose_control)
            finally:
                from .models import TERMINAL
                if self.runtime.run(run_id)['state'] in TERMINAL:
                    try:
                        from .episodes import EpisodeBuilder
                        EpisodeBuilder(self.store).capture_run(run_id)
                    except Exception as exc:
                        with self.store.tx() as c:
                            self.store.emit(c,run_id,'l1.observation_deferred',
                                            {'code':getattr(exc,'code',type(exc).__name__)},
                                            manifest.get('visibility','TRUSTED_ONLY'))
                if self.memory and manifest["manifest"].get("memory_schema_version")==2 and self.runtime.run(run_id)['state'] in TERMINAL:
                    try:await self.memory.on_terminal(run_id)
                    except Exception as exc:
                        # Auxiliary maintenance cannot rewrite an existing delivery verdict.
                        with self.store.tx() as c:self.store.emit(c,run_id,"memory.maintenance_deferred",{"code":getattr(exc,"code",type(exc).__name__)})
            if self.memory and manifest['manifest'].get('memory_schema_version')==2:
                result['memory_cost']=self.agent.report(run_id)['memory_cost']
            try:self.session_experiences().feedback(run_id,result)
            except Exception as exc:
                with self.store.tx() as c:self.store.emit(c,run_id,'session.feedback_deferred',{'code':getattr(exc,'code',type(exc).__name__)})
            return result

    def close(self):
        if self.memory:self.memory.close()

    def procedure_promotion(self, runner, release_evaluator=None):
        """Host-owned persistent Draft qualification; never exposed as a worker tool."""
        from .procedure_promotion import ProcedurePromotion
        return ProcedurePromotion(self, runner, release_evaluator)

    def session_experiences(self):
        from .session_experience import SessionExperiences
        return SessionExperiences(self.runtime)

    def learning_pipeline(self,runner):
        """Host-only cross-task collection handoff to existing qualification."""
        from .learning_pipeline import LearningPipeline
        return LearningPipeline(self,runner)

    def prospective_certification(self,evaluator):
        from .prospective import ProspectiveCertification
        return ProspectiveCertification(self,evaluator)

    async def _execute_owned(self,run_id,expose_control=False):
        """Expose only an authenticated loopback control channel for a live CLI Run.

        The endpoint token is private Host state, never part of model context.
        Independent CLI processes can cancel through the actual process owner.
        """
        if not expose_control:
            return await self.agent.execute(run_id)
        import asyncio
        from .ipc import BrokerServer
        from .util import uid
        server=BrokerServer(self.broker).start()
        generation=uid("executor-")
        self.store.put("executor_endpoint",run_id,{"generation":generation,"address":list(server.address),
            "token":server.tokens.issue(run_id,"control",ttl=86400),"state":"ACTIVE"})
        try:
            return await self.agent.execute(run_id)
        finally:
            current=self.store.get("executor_endpoint",run_id)
            if current and current["generation"]==generation:
                self.store.put("executor_endpoint",run_id,{"generation":generation,"state":"CLOSED"})
            await asyncio.to_thread(server.close)
