import json
import pytest
from repo_code.qualification_catalog import QualificationCatalog
from repo_code.util import RepoError


def setup_catalog(extra=(), rules=None):
    def row(key,role,uses=1,code=None):
        return dict(id=key,role=role,files={'src.py':code or key,
            'contract.json':json.dumps({'algorithm':'capacity','uses':uses})})
    rows=[row('tickets','source'),row('stock','support'),row('bins','target'),
          row('endless','target',2), row('never-graded','support'),
          row('renamed-source','support',code='tickets'),row('leaked-target','support',code='bins'),
          *extra]
    cfg=[{'mechanism':'one-use','path':'contract.json','equals':{'algorithm':'capacity','uses':1}}]
    return QualificationCatalog(rules or cfg,rows),{r['id']:r['files'] for r in rows}


def test_nomination_by_contract_not_title_and_no_permission_from_a_match():
    c,f=setup_catalog()
    fam=c.family('tickets',f['tickets'])
    assert c.family('bins',f['bins'])==c.family('stock',f['stock'])==fam
    assert c.family('endless',f['endless'])!=fam
    d={'id':'draft','task_family':fam}
    decision=c.nominate(d,'tickets',source_files=f['tickets'],passed_sources={'tickets'},
        passed_supports={'tickets','stock','bins','leaked-target','renamed-source'})
    assert decision['status']=='NOMINATED' and decision['support_ids']==['stock']
    assert decision['reuse_authorized'] is False
    assert c.nominate(d,'tickets',source_files=f['tickets'],passed_sources=set(),passed_supports={'stock'})['status']=='SOURCE_NOT_VERIFIED'
    assert c.nominate(d,'tickets',source_files=f['tickets'],passed_sources={'tickets'},passed_supports={'bins'})['status']=='NO_INDEPENDENT_SUPPORT'


def test_a_changed_source_or_a_self_declared_family_cannot_substitute():
    c,f=setup_catalog()
    with pytest.raises(RepoError,match='INPUT_CHANGED'):
        c.family('tickets',{**f['tickets'],'contract.json':f['endless']['contract.json']})
    n=c.nominate({'id':'d','task_family':'unrelated'},'tickets',source_files=f['tickets'],
        passed_sources={'tickets'},passed_supports={'stock'})
    assert n['status']=='NO_UNAMBIGUOUS_MECHANISM'


@pytest.mark.parametrize('raw',['{"algorithm":"capacity","uses":true}', '{"algorithm":"capacity","uses":1,"uses":1}', '{}','null','not json'])
def test_untrusted_or_inexact_contract_has_no_shared_fallback(raw):
    c,f=setup_catalog([{'id':k,'role':'target','files':{'contract.json':raw}} for k in ('x','y')])
    assert c.family('x',f['x'])!=c.family('y',f['y'])
    assert c.family('x',f['x'])!=c.family('stock',f['stock'])


def test_ambiguous_mechanism_never_yields_nomination():
    rules=[{'path':'contract.json','mechanism':m,'equals':{'uses':1}} for m in ('exactly-once','at-most-once')]
    c,f=setup_catalog(rules=rules);family=c.family('tickets',f['tickets'])
    n=c.nominate(dict(id='d',task_family=family),'tickets',source_files=f['tickets'],passed_sources={'tickets'},passed_supports={'stock'})
    assert n['status']=='NO_UNAMBIGUOUS_MECHANISM' and not n['support_ids']


def test_same_contract_different_failure_or_stale_witness_does_not_route():
    from repo_code.util import digest
    ref=digest({'probe':'single item, excess capacity and capacity-zero boundary'})
    rule={'path':'p','equals':{'uses':1},'observation':ref,'mechanism':'repeat'}
    rows=[dict(id=k,role=r,files={'p':'{"uses":1}','implementation':k}) for k,r in
          [('source','source'),('support','support'),('wanted','target'),('other-bug','target'),
           ('changed','target'),('crashed','target'),('unseen','target')]]
    observed={r['id']:{ref:{'outcome':'MATCH','files_digest':digest(r['files'])}} for r in rows[:-1]}
    observed['other-bug'][ref]['outcome']='NO_MATCH'
    observed['changed'][ref]['files_digest']=digest({'older':1})
    observed['crashed'][ref]['outcome']='ERROR'
    c=QualificationCatalog([rule],rows,observations=observed)
    f={r['id']:c.family(r['id'],r['files']) for r in rows}
    assert f['source']==f['support']==f['wanted']
    assert all(f[k]!=f['source'] for k in ['other-bug','changed','crashed','unseen'])
