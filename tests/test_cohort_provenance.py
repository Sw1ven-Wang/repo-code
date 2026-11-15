import pytest
from repo_code.governance import HoldoutVault
from repo_code.util import RepoError
from test_review_workflow import setup

@pytest.mark.parametrize('mode',['independent','same-group','renamed-source','legacy'])
def test_applicability_does_not_define_contamination(tmp_path,mode):
    app,_=setup(tmp_path)
    try:
        v=HoldoutVault(app.store)
        c=app.releases.candidate('test',{'retry_policy':{'kind':'config','max_identical_failures':2}},'provenance protocol fixture',[])
        a={'id':'a','goal':'fix one','files':{'a.py':'x=1'}}
        b={'id':'b','goal':'fix another','files':{'a.py':'x=2'}}
        def register(name,role,t,g):
            if mode=='legacy':return v.register(name,role,[t],family='same-mechanism')
            return v.register(name,role,[t],applicability_family='same-mechanism',contamination_groups=[g])
        dev=register('dev','dev',a,'source-a');v.mark_exposure(c.id,dev)
        if mode=='renamed-source':b['files']=a['files']
        pro=register('pro','promotion',b,'source-a' if mode=='same-group' else 'source-b')
        if mode=='independent':
            v.claim(c.id,pro,'G3')
            with pytest.raises(RepoError,match='HOLDOUT_ALREADY_USED'):v.claim(c.id,pro,'G3')
        else:
            with pytest.raises(RepoError,match='HOLDOUT_'):v.claim(c.id,pro,'G3')
    finally:app.close()

def test_incomplete_provenance_is_not_inferred_from_family(tmp_path):
    app,_=setup(tmp_path)
    try:
        with pytest.raises(RepoError,match='COHORT_PROVENANCE_REQUIRED'):
            HoldoutVault(app.store).register('x','dev',[{'id':'a'}],applicability_family='precision')
    finally:app.close()

def test_legacy_exposure_cannot_be_erased_by_v2_registration(tmp_path):
    app,_=setup(tmp_path)
    try:
        v=HoldoutVault(app.store)
        c=app.releases.candidate('legacy',{'retry_policy':{'kind':'config','max_identical_failures':2}},'legacy provenance fixture',[])
        dev=v.register('old','dev',[{'id':'a'}],family='precision');v.mark_exposure(c.id,dev)
        pro=v.register('new','promotion',[{'id':'b','files':{'b.py':'x=1'}}],applicability_family='precision',contamination_groups=['new-source'])
        with pytest.raises(RepoError,match='HOLDOUT_CONTAMINATED'):v.claim(c.id,pro,'G3')
    finally:app.close()
