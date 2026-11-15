import copy,json
from types import SimpleNamespace
import pytest
from repo_code.skill_validation import SkillValidator
from repo_code.evaluation import TrialRunner
from repo_code.capabilities import CapabilityRouter
from repo_code.memory.repository import MemoryRepository
from repo_code.memory.consolidation import Consolidator
from repo_code.memory.models import MemoryProfile
from repo_code.models import Assistant,ToolCall,Contract,Requirement,Check
from repo_code.providers import ScriptedProvider
from repo_code.acceptance import freeze_contract
from repo_code.util import RepoError
from test_elp_experience import initial,learn
from test_docker_sandbox import docker_app

async def setup_skill(app,run):
 owner=Consolidator(MemoryRepository(app.store),app.runtime,MemoryProfile(mode='lexical_only'))
 def change(v):v['procedure'].update(triggers=['task_family:editing-boundary'],exclusions=['run_cancelled'],steps=['Read source'],expected_output='Source inspected before repair')
 _,lid,d=await learn(app,owner,run,change=change)
 ref,_=CapabilityRouter(owner).contract(d['entity_id'],step_rules=[{'tool':'read','arguments_match':{'path':'main.py'}}],probe_ids=['positive','negative','boundary'])
 record=owner.repo.store.get('capability_contract',ref)
 entry={'experience_id':d['entity_id'],'revision':d['revision'],'revision_digest':d['revision_digest'],'scope':record['scope'],'task_family':'editing-boundary','status':'ACTIVE','phases':['PLAN','EDIT','DEBUG','VERIFY','RESUME'],'step_rules':record['step_rules'],'required_probe_ids':['positive','negative','boundary']}
 candidate=app.releases.candidate('skill-probe',{'skills_index':{'kind':'config','skills':[entry]}},'Read before repair',[ref])
 contract=freeze_contract(Contract(requirements=[Requirement(id='r',description='answer(x) returns x+1')],checks=[Check(id=name,kind='function',definition={'module':'main','function':'answer','cases':[{'id':'case','args':[x],'expected':x+1}]}) for name,x in [('positive',2),('negative',-2),('boundary',0)]],required_checks=['positive','negative','boundary'],coverage={'r':['positive','negative','boundary']}),'independent-test-curator')
 task={'id':'support','goal':'Implement answer(x) = x+1 in main.py.','files':{'main.py':'def answer(x): return x\n'},'contract':contract.model_dump(mode='json'),'budget':{'model_calls':8,'tool_calls':20,'wall_seconds':180},'capability_context':{'scope':record['scope'],'task_family':'editing-boundary'}}
 def factory(public):
  def handle(system,messages,tools,index):
   calls=[ToolCall(id='read',name='read',arguments={'path':'main.py'})] if index==0 else [ToolCall(id='edit',name='edit',arguments={'path':'main.py','old_text':public['files']['main.py'],'new_text':'def answer(x): return x+1\n'})] if index==1 else []
   return Assistant(text='Ready' if not calls else '',tool_calls=calls,usage={'input_tokens':100,'output_tokens':10})
  return ScriptedProvider(handler=handle)
 runner=TrialRunner(app,factory,isolated_states=True,trial_root=app.store.root.parent/'trials',legacy_window=False)
 validator=SkillValidator(app,runner)
 for name,kind in [('positive','POSITIVE'),('negative','NEGATIVE'),('boundary','BOUNDARY')]:
  validator.register({'probe_id':name,'kind':kind,'task_id':'support','check_id':name,'expected_behavior':'Declared function behavior','source_refs':[lid],'environment_profile':app.sandbox.environment_digest},task)
 return validator,ref,candidate,task

@pytest.mark.asyncio
async def test_static_checks_require_probes_and_isolation(tmp_path):
 app,run,owner=await initial(tmp_path)
 try:
  _,_,d=await learn(app,owner,run)
  ref,_=CapabilityRouter(owner).contract(d['entity_id'])
  candidate=app.releases.candidate('empty-probe',{'skills_index':{'kind':'config','skills':[]}},'not ready',[ref])
  v=SkillValidator(app,SimpleNamespace(isolated_states=False))
  report=v.s1(ref,candidate.id,{})
  assert not report['passed'] and 'SKILL_PROBES_REQUIRED' in report['reasons']
 finally:app.close()

@pytest.mark.asyncio
@pytest.mark.docker_backend
async def test_actual_docker_support_replay_executes_all_negative_and_boundary_probes(docker_app,tmp_path):
 app=docker_app;source=tmp_path/'source';source.mkdir();(source/'a.py').write_text('value=0\n')
 run=app.new_task('Learn a scoped read-before-repair procedure',source,authorize_write=True,authorize_process=True)
 validator,ref,candidate,task=await setup_skill(app,run)
 before=app.releases.active()
 assert validator.s1(ref,candidate.id,{'support':task})['passed']
 result=await validator.s2(ref,candidate.id,{'support':task})
 assert result['passed'],result
 assert result['evidence_class']=='INTEGRATION_ONLY'
 assert len({r['state_root'] for r in result['trials']})==2
 assert all(not r['skipped'] and len(r['executed'])==3 for r in result['probes'])
 assert app.releases.active()==before and app.store.get('candidate_state',candidate.id)['state']=='INTEGRATION_ONLY'
 with pytest.raises(RepoError,match='SKILL_S3_COVERAGE'):await validator.s3(ref,candidate.id,[])
 repeated=[{**copy.deepcopy(task),'skill_role':role,'id':str(i),'goal':'renamed '+str(i)} for i,role in enumerate(['CORE','CORE','CORE','TRANSFER','NEGATIVE'])]
 with pytest.raises(RepoError,match='SKILL_REPEATED_TASK'):await validator.s3(ref,candidate.id,repeated)
 tasks=[]
 for index,role in enumerate(['CORE','CORE','CORE','TRANSFER','NEGATIVE']):
  t=copy.deepcopy(task);t['id']='independent-'+str(index);t['goal']+=' Variant '+str(index);t['files']['main.py']='# Independently frozen fixture '+str(index)+'\ndef answer(x):\n    return x + '+str(index+2)+'\n'
  t['skill_role']=role
  if role!='TRANSFER':t['capability_context']['task_family']='unrelated-'+str(index)
  tasks.append(t)
 regression=await validator.s3(ref,candidate.id,tasks)
 assert regression['passed'],regression
 assert not regression['misfires'] and regression['transfer_passed']
 assert app.store.get('candidate_state',candidate.id)['state']=='INTEGRATION_ONLY'
 assert not app.store.get('skill_qualification',candidate.id)
 with pytest.raises(RepoError,match='SKILL_QUALIFICATION_REQUIRED'):app.releases.activate(candidate.id)


@pytest.mark.asyncio
async def test_malformed_contract_is_persisted_as_rejection(tmp_path):
 app,run,owner=await initial(tmp_path)
 try:
  v=SkillValidator(app,SimpleNamespace(isolated_states=True))
  report=v.s1('missing-contract','missing-candidate',{})
  assert report['reasons']==['SKILL_SCHEMA_INVALID']
  app.store.put('capability_contract','bad',{'forged':True})
  assert not v.s1('bad','missing-candidate',{})['passed']
  assert len(app.store.values('skill_s1'))==2
 finally:app.close()

@pytest.mark.asyncio
async def test_s1_binds_contract_and_freezes_registered_probe_inputs(tmp_path):
 app,run,owner=await initial(tmp_path)
 try:
  validator,ref,candidate,task=await setup_skill(app,run)
  assert validator.s1(ref,candidate.id,{'support':task})['passed']
  changed=copy.deepcopy(task);changed['files']['main.py']='tampered'
  rejected=validator.s1(ref,candidate.id,{'support':changed})
  assert rejected['reasons']==['SKILL_PROBE_SOURCE_CHANGED']
  forged=copy.deepcopy(app.store.get('capability_contract',ref));forged['steps']=['Unrelated guidance']
  from repo_code.util import digest
  forged_ref=digest(forged);app.store.put('capability_contract',forged_ref,forged)
  assert validator.s1(forged_ref,candidate.id,{'support':task})['reasons']==['SKILL_CONTRACT_SOURCE_BINDING']
  assert validator.s1(ref,candidate.id,{'support':task})['passed']
  assert len(app.store.values('skill_probe'))==3
  assert not app.store.get('skill_qualification',candidate.id)
 finally:app.close()
