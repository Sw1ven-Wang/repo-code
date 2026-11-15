"""Offline forensic replay of the retained CSV overflow. Never calls an API.

Replays the stored rejected semantic response in a new shadow Host; historical
states and source archives remain unchanged. All shadow mutations are labelled.
"""
import argparse
import asyncio
import copy
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace


def main():
    root=Path(__file__).resolve().parents[1]
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',type=Path,default=root)
    p.add_argument('--input-host',type=Path,default=root/'reports/memory-v2-2026-11-01/e2-window16k/cross-module-csv/D/host')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--restrict-recorded-sources',action='store_true',help='Explicit counterfactual fixture: discard recorded claims outside the new prefix; not an exact API replay')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    sys.path.insert(0,str(args.source_root.resolve()/'src'))
    from repo_code.service import RepoCode
    from repo_code.providers import OpenAIProvider
    from repo_code.models import Assistant
    from repo_code.memory.models import MemoryProfile
    from repo_code.tool_models import definitions
    from repo_code.context import estimate_tokens,groups
    from repo_code.util import digest,canonical
    from repo_code.memory.encoders import LocalEncoder
    shadow=args.output/'shadow';shutil.copytree(args.input_host,shadow)
    with sqlite3.connect(shadow/'runtime.db') as c:
        c.row_factory=sqlite3.Row;row=c.execute('SELECT * FROM runs').fetchone();data=json.loads(row['data']);run_id=row['id']
        profile_data=json.loads((shadow/'artifacts'/data['manifest']['memory_profile_digest'][7:]).read_bytes())
        profile=MemoryProfile.model_validate(profile_data)
        data['workspace']=str((shadow/'workspaces'/run_id).resolve())
        c.execute("UPDATE runs SET state='RUNNING',cancel_requested=0,data=? WHERE id=?",(json.dumps(data),run_id))
        controls=[json.loads(x[0]) for x in c.execute('SELECT data FROM memory_control')]
        rejected=next(x for x in controls if 'visible_proposal_ref' in x)
        response=(shadow/'artifacts'/rejected['visible_proposal_ref'][7:]).read_text(encoding='utf-8')
    native=OpenAIProvider('deepseek-flash',base_url='https://api.deepseek.com',client=SimpleNamespace(),thinking='disabled',max_tokens=4096,temperature=0)
    replayed=[]
    class AuditProvider:
        inner=native;model=native.model;base_url=native.base_url;thinking='disabled';max_tokens=4096;temperature=0;reasoning_effort=None
        def build_request(self,*a):return native.build_request(*a)
        async def complete(self,*a,**kw):
            text=response;removed=0
            if args.restrict_recorded_sources:
                payload=json.loads(a[1][0]['content']);last=max(int(x['ref']['source_id'].split(':')[1]) for x in payload['new_closed_groups'])
                allowed={x['ref']['source_id'] for x in app.memory.history.records(run_id) if x['seq']<=last}
                value=json.loads(response);changes=[c for c in value['changes'] if set(c['source_ids'])<=allowed]
                removed=len(value['changes'])-len(changes);text=json.dumps({**value,'changes':changes})
            replayed.append({'request_digest':digest(self.build_request(*a[:3])[0]),'stored_response_ref':rejected['visible_proposal_ref'],
                'scoped_fixture':args.restrict_recorded_sources,'discarded_outside_new_prefix':removed,'response_digest':digest(text)})
            return Assistant(text=text,usage={})
    encoder=LocalEncoder(profile.encoder)
    app=RepoCode(shadow,AuditProvider(),'local',memory_profile=profile,memory_encoder=encoder)
    app.runtime.remaining_seconds=lambda run_id:600
    # Current codec defaults may add a field: rebind only the shadow profile.
    with app.store.tx() as c:
        run=app.runtime.run(run_id,c);run['data']['manifest']['memory_profile_digest']=app.memory.profile_digest
        c.execute('UPDATE runs SET data=? WHERE id=?',(canonical(run['data']).decode(),run_id))
    captured=[];serialize=app.memory.serialized
    def observed(system,messages,tools):
        request=serialize(system,messages,tools);context={}
        if messages:
            try:context=json.loads(messages[0].get('content',''))
            except (TypeError,ValueError):pass
        def cost(value):return estimate_tokens(value) if value else 0
        captured.append({'input_total':estimate_tokens(request),'system_and_contract':cost(system),'tools':cost(request.get('tools',tools)),
            'working_state':cost(context.get('working_state')),'summary':cost(context.get('summary')),
            'retrieved_memory':cost(context.get('retrieved_evidence')),'complete_tail':cost(messages[1:] if context else messages),
            'request_digest':digest(request),'required_window':16000,'reserved_output':4096,'safety_margin':500})
        return request
    app.memory.serialized=observed
    async def replay():
        try:
            system,messages,manifest=await app.memory.prepare_context(run_id,definitions(),policy={'max_tokens':16000,'reserve_tokens':4096})
            return {'status':'PACKED','manifest':manifest,'tool_groups_complete':bool(groups(messages)),
                    'final_input':manifest['estimated_input_tokens'],'raw_final_input_estimate':estimate_tokens(serialize(system,messages,definitions()))}
        except Exception as exc:return {'status':getattr(exc,'code',type(exc).__name__),'message':str(exc)}
        finally:encoder.close();app.close()
    result=asyncio.run(replay())
    result.update(paid_calls=0,replayed_semantic_responses=len(replayed),source_root=str(args.source_root.resolve()),
        replay_mode='SCOPED_COUNTERFACTUAL_FIXTURE' if args.restrict_recorded_sources else 'EXACT_RECORDED_RESPONSE',
        input_host=str(args.input_host.resolve()),shadow_only_mutations=['workspace relocation','RUNNING forensic state','wall-clock fixed to 600 seconds','current profile defaults rebound'],
        original_rejection=rejected['code'],budgets=captured,semantic_replays=replayed)
    (args.output/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in {'budgets','semantic_replays'}},indent=2))


if __name__=='__main__':main()
