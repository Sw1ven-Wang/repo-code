"""Greenfield acceptance establishment; draft generation is NOT freezing."""
import json
from pathlib import Path
from .models import Contract
from .acceptance import freeze_contract, validate_contract
from .util import RepoError, atomic_write, canonical

async def draft_contract(provider,goal,interface=None):
    schema=Contract.model_json_schema()
    prompt=("Act as an acceptance author, separately from the implementation agent. "
        "Convert the user's requirements into observable external behavior and explicit interfaces. "
        "Return ONLY JSON matching Contract. status must be DRAFT and reviewer null. "
        "Use deterministic externally judged cli/function/asgi cases, never trust DUT test reports. "
        "Each case has a unique id and explicit expected behavior; coverage references all requirements. "
        "Global required checks must be declared. Do not claim tests passed. "
        "If requirements are ambiguous, leave them as reviewable interface questions, not an invented success criterion. "
        "function definition={module,function,cases:[{id,args,kwargs,expected}]}; "
        "cli definition={cases:[{id,argv,stdin,exit_code,stdout}]}; "
        "asgi definition={module,app,cases:[{id,method,path,body,expected_status,expected_body}]}. "
        "SOURCE is sufficient for source delivery; wheel delivery requires build plus artifact_function. "
        "Schema: "+json.dumps(schema,ensure_ascii=False))
    answer=await provider.complete(prompt,[{'role':'user','content':json.dumps({'goal':goal,'interface':interface or {}},ensure_ascii=False)}],[])
    try:contract=Contract.model_validate_json(answer.text)
    except ValueError:raise RepoError('INVALID_CONTRACT_DRAFT') from None
    return contract.model_copy(update={'status':'DRAFT','reviewer':None})

def review_and_freeze(path,reviewer,out=None):
    contract=Contract.model_validate_json(Path(path).read_bytes())
    result=freeze_contract(contract,reviewer)
    atomic_write(Path(out or path),json.dumps(result.model_dump(mode='json'),ensure_ascii=False,indent=2).encode())
    return result
