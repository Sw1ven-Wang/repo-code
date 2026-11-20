import pytest
from repo_code.capabilities import CapabilityRouter,CapabilityContract
from repo_code.util import RepoError
from test_elp_experience import initial,learn,approved_fixture

@pytest.mark.asyncio
async def test_candidate_contract_and_no_match_router_are_idempotent(tmp_path):
 app,run,owner=await initial(tmp_path)
 try:
  source,lid,decision=await learn(app,owner,run)
  router=CapabilityRouter(owner);ref,contract=router.contract(decision['entity_id'])
  assert contract['kind']=='DECISION' and contract['status']=='CANDIDATE'
  a=router.route(source['id'],'RETENTION_RULE');b=router.route(source['id'],'RETENTION_RULE')
  assert not a['guidance'] and b['replayed'] and not b['guidance']
  _,receipt=router.receipt(source['id'],ref,historical=True)
  assert not receipt['applied'] and not receipt['verified'] and receipt['observed_operations']
  assert not receipt['offered']
 finally:app.close()

@pytest.mark.asyncio
async def test_approved_offer_is_not_application_and_not_reinjected(tmp_path):
 app,run,owner=await initial(tmp_path)
 try:
  source,_,decision=await learn(app,owner,run)
  current,facts=approved_fixture(app,owner,source,decision)
  router=CapabilityRouter(owner)
  a=router.route(current['id'],'RETENTION_RULE',facts=facts)
  assert len(a['guidance'])==1
  offer=router.mark_offered(a);assert offer['offered'] and offer['selected']
  b=router.route(current['id'],'RETENTION_RULE',facts=facts)
  assert not b['guidance']
  _,receipt=router.receipt(current['id'],a['contract_refs'][0],route_id=a['id'])
  assert receipt['offered'] and not receipt['applied'] and not receipt['verified']
  owner.repo.revoke(a['source_revisions'][0])
  with pytest.raises(RepoError):router.route(current['id'],'RETENTION_RULE',facts=facts)
 finally:app.close()

@pytest.mark.asyncio
async def test_long_issue_matches_summary_before_building_scoped_guidance(tmp_path,monkeypatch):
 app,run,owner=await initial(tmp_path)
 try:
  def procedure(value):value['procedure'].update(steps=['Inspect ZEPHYRQUARTZ_OMEGAPROBE output'])
  source,_,decision=await learn(app,owner,run,change=procedure)
  current,facts=approved_fixture(app,owner,source,decision)
  router=CapabilityRouter(owner)
  from repo_code.memory.retrieval import DynamicRetriever,EXPERIENCE_QUERY_CHAR_LIMIT
  original=DynamicRetriever.rank_experiences
  def rank(self,records,query,**options):
   assert len(query)<=EXPERIENCE_QUERY_CHAR_LIMIT
   assert all('ZEPHYRQUARTZ_OMEGAPROBE' not in row['text'] and 'sources' not in row for row in records)
   return original(self,records,query,**options)
  monkeypatch.setattr(DynamicRetriever,'rank_experiences',rank)
  goal='Fix retention\n'+'Detail: reproduce the issue.\n'*900+'\nCheck RETENTION_RULE'
  allowed=router.route(current['id'],goal,facts=facts)
  assert len(allowed['guidance'])==1
  hint=allowed['guidance'][0]
  contract=app.store.get('capability_contract',hint['contract_ref'])
  assert hint['steps']==['Inspect ZEPHYRQUARTZ_OMEGAPROBE output']
  assert hint['exclusions']==contract['exclusions'] and hint['expected_outputs']==contract['expected_outputs']
  assert contract['source_episode_refs'] and 'source_episode_refs' not in hint
  assert owner.repo.get('retrieval_traces',allowed['retrieval_trace'])['match_view']=='DESCRIPTION_AND_TRIGGERS'
  # Changing even the middle of the full goal must produce a new route.
  changed=goal[:10000]+'Corrected detail'+goal[10000:]
  assert router.route(current['id'],changed,facts=facts)['id']!=allowed['id']
  def unexpected(*args,**kwargs):pytest.fail('a rejected hit must not build a contract')
  monkeypatch.setattr(router,'contract',unexpected)
  miss=router.route(current['id'],'ZEPHYRQUARTZ_OMEGAPROBE zirconium',facts=facts)
  assert not miss['guidance'] and not miss['considered_contract_refs']
  denied=router.route(current['id'],goal,facts={k:False for k in facts})
  assert not denied['guidance'] and not denied['considered_contract_refs']
 finally:app.close()

@pytest.mark.asyncio
async def test_long_hit_pages_only_while_pinned_and_applicable(tmp_path):
 from repo_code.context import estimate_tokens
 from repo_code.capabilities import _guidance_view
 import json
 app,run,owner=await initial(tmp_path)
 try:
  source,_,decision=await learn(app,owner,run,change=lambda v:v.update(text='ERROR_42 '+('detail '*180)))
  current,facts=approved_fixture(app,owner,source,decision)
  owner.profile=owner.profile.model_copy(update={'evidence_tokens':128})
  router=CapabilityRouter(owner);rid=current['id']
  args=dict(run_id=rid,purpose='runtime',match_only=True,facts=facts)
  hits,_=owner.retrieve_experiences('ERROR_42',**args)
  assert len(hits)==1 and hits[0]['view']=='REFERENCE_ONLY' and estimate_tokens(hits[0])<=128
  hits,trace=owner.retrieve_experiences('ERROR_42',**args,token_budget=1)
  assert not hits and owner.repo.get('retrieval_traces',trace)['budget_skips'][0]['reason']=='REFERENCE_EXCEEDS_BUDGET'
  assert not owner.retrieve_experiences('ERROR_42',**args,token_budget=0)[0]
  assert not owner.retrieve_experiences('xyznotamatch',**args)[0]
  route=router.route(rid,'ERROR_42',facts=facts);g,=route['guidance'];ref=g['contract_ref']
  assert g['view']=='REFERENCE_ONLY' and 'steps' not in g and route['potential_tokens']<=128
  with pytest.raises(RepoError):router.read_offered(rid,ref,0,facts=facts)
  receipt=router.mark_offered(route);assert not receipt['applied'] and not receipt['verified']
  chunks=[];offset=0
  while offset is not None:
   page=router.read_offered(rid,ref,offset,facts=facts);chunks.append(page['content'])
   assert estimate_tokens(page)<=128 and page['offset']==offset
   offset=page['next_offset']
  assert len(chunks)>1 and json.loads(''.join(chunks))==_guidance_view(app.store.get('capability_contract',ref))
  with pytest.raises(RepoError):router.read_offered(rid,ref,0,facts={})
  owner.repo.revoke(route['source_revisions'][0])
  with pytest.raises(RepoError):router.read_offered(rid,ref,0,facts=facts)
 finally:app.close()

@pytest.mark.asyncio
async def test_bound_operation_sequence_is_observed_but_not_verified(tmp_path):
 from test_review_workflow import tool
 app,run,owner=await initial(tmp_path)
 try:
  source,_,decision=await learn(app,owner,run,change=lambda v:v['procedure'].update(steps=['Read source']))
  current,facts=approved_fixture(app,owner,source,decision,step_tools=['read'],step_rules=[{'tool':'read','arguments_match':{'path':'a.py'}}])
  router=CapabilityRouter(owner);route=router.route(current['id'],'RETENTION_RULE',facts=facts)
  router.mark_offered(route)
  for i in range(1):await tool(app,current,'read',{'path':'a.py'},'step-'+str(i))
  _,receipt=router.receipt(current['id'],route['contract_refs'][0],route_id=route['id'])
  assert receipt['applied'] and not receipt['verified'] and len(receipt['step_evidence'])==1
 finally:app.close()

@pytest.mark.asyncio
async def test_final_grader_proof_can_verify_only_declared_checks(tmp_path):
 from test_review_workflow import tool
 from repo_code.util import digest
 app,run,owner=await initial(tmp_path)
 try:
  source,_,d=await learn(app,owner,run,change=lambda v:v['procedure'].update(steps=['Read source']))
  current,facts=approved_fixture(app,owner,source,d,step_rules=[{'tool':'read','arguments_match':{'path':'a.py'}}],required_probe_ids=['behavior'])
  router=CapabilityRouter(owner);route=router.route(current['id'],'RETENTION_RULE',facts=facts);router.mark_offered(route)
  await tool(app,current,'read',{'path':'a.py'},'read-step')
  key='host-decision-fixture';intent={'run_id':current['id'],'source_digest':app.runtime.workspace(app.runtime.run(current['id'])).snapshot()}
  app.store.put('intent',key,intent)
  app.store.put('evidence','behavior-evidence',{'check_id':'behavior','status':'PASSED'})
  decision={'intent_digest':key,'verified':True,'evidence_ids':['behavior-evidence']}
  decision['signature']=app.store.sign(decision);app.store.put('decision',key,decision)
  _,r=router.receipt(current['id'],route['contract_refs'][0],route_id=route['id'])
  assert r['applied'] and r['verified'] and r['verification_refs']==[key]
  with app.store.tx() as c:app.runtime.update(c,app.runtime.run(current['id'],c),control=True)
  _,r=router.receipt(current['id'],route['contract_refs'][0],route_id=route['id'])
  assert not r['applied'] and not r['verified']
 finally:app.close()

@pytest.mark.asyncio
async def test_agent_loop_offers_once_and_automatically_records_application(tmp_path):
 from test_review_workflow import tool
 from repo_code.providers import ScriptedProvider
 from repo_code.models import Assistant,ToolCall
 app,run,owner=await initial(tmp_path)
 seen=[]
 try:
  def change(v):v['procedure'].update(triggers=['source_changed'],exclusions=['run_cancelled'],steps=['Read source'])
  source,_,d=await learn(app,owner,run,change=change)
  current,_=approved_fixture(app,owner,source,d,goal='Review the RETENTION_RULE behavior',
    phases=['PLAN','EDIT','DEBUG','VERIFY','RESUME'],step_rules=[{'tool':'read','arguments_match':{'path':'a.py'}}])
  app.broker.authorize_run(current['id'],True,True,delegate=False)
  await tool(app,current,'create',{'path':'changed.py','content':'x=1'},'pre-change')
  def reply(system,messages,tools,i):
   seen.append(system)
   return Assistant(text='Read source' if i==0 else 'Candidate ready',tool_calls=[ToolCall(id='actual-read',name='read',arguments={'path':'a.py'})] if i==0 else [],usage={'input_tokens':20,'output_tokens':5})
  p=ScriptedProvider(handler=reply);app.provider=app.agent.provider=app.broker.provider=p
  from repo_code.providers import model_configuration
  from repo_code.util import digest
  with app.store.tx() as c:
   configured=app.runtime.run(current['id'],c);configured['data']['model_config']=model_configuration(p)
   configured['data']['manifest']['model_config_digest']=digest(model_configuration(p))
   app.runtime.update(c,configured,data=configured['data'])
  app.store.put('operator_config','capability_router',{'enabled':True})
  result=await app.execute(current['id'])
  assert sum('Scoped decision guidance;' in s for s in seen)==1
  assert result['application_receipts']
  receipts=[app.store.get('application_receipt',r) for r in result['application_receipts']]
  assert any(r['applied'] and not r['verified'] for r in receipts)
 finally:app.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['wrong-path','no-offer','fake-signature','stale-source'])
async def test_application_and_verification_negative_controls(tmp_path,fault):
 from test_review_workflow import tool
 app,run,owner=await initial(tmp_path)
 try:
  source,_,d=await learn(app,owner,run,change=lambda v:v['procedure'].update(steps=['Read source']))
  current,facts=approved_fixture(app,owner,source,d,step_rules=[{'tool':'read','arguments_match':{'path':'a.py'}}],required_probe_ids=['behavior'])
  router=CapabilityRouter(owner);route=router.route(current['id'],'RETENTION_RULE',facts=facts)
  if fault!='no-offer':router.mark_offered(route)
  await tool(app,current,'read',{'path':'missing.py' if fault=='wrong-path' else 'a.py'},'read-proof')
  key='negative-proof';app.store.put('intent',key,{'run_id':current['id'],'source_digest':'stale' if fault=='stale-source' else app.runtime.workspace(app.runtime.run(current['id'])).snapshot()})
  app.store.put('evidence','negative-evidence',{'check_id':'behavior','status':'PASSED'})
  proof={'intent_digest':key,'verified':True,'evidence_ids':['negative-evidence']}
  proof['signature']='forged' if fault=='fake-signature' else app.store.sign(proof)
  app.store.put('decision',key,proof)
  _,result=router.receipt(current['id'],route['contract_refs'][0],route_id=route['id'])
  assert not result['verified']
  if fault in {'wrong-path','no-offer'}:assert not result['applied']
 finally:app.close()

@pytest.mark.asyncio
async def test_one_operation_cannot_prove_two_steps_even_with_replayed_events(tmp_path):
 from test_review_workflow import tool
 app,run,owner=await initial(tmp_path)
 try:
  source,_,d=await learn(app,owner,run,change=lambda v:v['procedure'].update(steps=['Read source','Read source again']))
  current,facts=approved_fixture(app,owner,source,d,step_rules=[{'tool':'read','arguments_match':{'path':'a.py'}}]*2)
  router=CapabilityRouter(owner);route=router.route(current['id'],'RETENTION_RULE',facts=facts);router.mark_offered(route)
  await tool(app,current,'read',{'path':'a.py'},'only-first')
  event=next(e for e in reversed(app.store.events(current['id'],trusted=True)) if e['type']=='tool.operation_finished')
  with app.store.tx() as c:app.store.emit(c,current['id'],'tool.operation_finished',event['data'],'DEV')
  _,r=router.receipt(current['id'],route['contract_refs'][0],route_id=route['id'])
  assert not r['applied']
  await tool(app,current,'read',{'path':'a.py'},'real-second')
  _,r=router.receipt(current['id'],route['contract_refs'][0],route_id=route['id'])
  assert r['applied'] and len({e['operation_id'] for e in r['step_evidence']})==2
 finally:app.close()
