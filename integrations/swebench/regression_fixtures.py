"""Narrow proof for appending independent SQLFluff rule regression cases."""
import hashlib
import re
from dataclasses import dataclass
import yaml
from repo_code.util import RepoError


def _load(data):
    class Unique(yaml.SafeLoader):pass
    def mapping(loader,node,deep=False):
        result={}
        for key,value in node.value:
            name=loader.construct_object(key,deep=deep)
            if not isinstance(name,str):raise RepoError('FIXTURE_CASE_SCHEMA')
            if name in result:raise RepoError('FIXTURE_DUPLICATE_KEY')
            result[name]=loader.construct_object(value,deep=deep)
        return result
    Unique.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,mapping)
    try:
        for token in yaml.scan(data):
            if isinstance(token,(yaml.tokens.AnchorToken,yaml.tokens.AliasToken,yaml.tokens.TagToken)):
                raise RepoError('FIXTURE_DYNAMIC_YAML_DENIED')
        return yaml.load(data,Loader=Unique)
    except yaml.YAMLError:
        raise RepoError('FIXTURE_INVALID_YAML','Appended regression cases must be valid static YAML') from None


@dataclass(frozen=True)
class RuleFixtureAddition:
    path:str
    before_sha256:str
    after_sha256:str
    new_cases:tuple[str,...]

    @classmethod
    def prove(cls,path,before,after):
        if not re.fullmatch(r'test/fixtures/rules/std_rule_cases/[A-Za-z0-9_]+\.yml',path):
            raise RepoError('FIXTURE_PATH_DENIED')
        if not before or not after.startswith(before) or after==before:
            raise RepoError('FIXTURE_MUST_APPEND_ONLY')
        old=_load(before);new=_load(after)
        if not isinstance(old,dict) or not isinstance(new,dict) or any(k not in new or new[k]!=v for k,v in old.items()):
            raise RepoError('FIXTURE_EXISTING_CASE_CHANGED')
        added=sorted(new.keys()-old.keys())
        if not added:raise RepoError('FIXTURE_NEW_CASE_REQUIRED')
        for key in added:
            case=new[key]
            if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_]+',key) or not isinstance(case,dict):raise RepoError('FIXTURE_CASE_SCHEMA')
            if set(case)-{'pass_str','fail_str','fix_str','configs'} or not any(k in case for k in ['pass_str','fail_str']):raise RepoError('FIXTURE_CASE_SCHEMA')
            if any(not isinstance(v,str) for k,v in case.items() if k!='configs'):raise RepoError('FIXTURE_CASE_SCHEMA')
            config=case.get('configs',{})
            if config and (not isinstance(config,dict) or set(config)!={'core'} or not isinstance(config['core'],dict) or set(config['core'])!={'dialect'} or not isinstance(config['core']['dialect'],str)):
                raise RepoError('FIXTURE_CONFIG_DENIED')
        return cls(path,hashlib.sha256(before).hexdigest(),hashlib.sha256(after).hexdigest(),tuple(added))

    def verify(self,name,before,after):
        return name==self.path and before is not None and after is not None and RuleFixtureAddition.prove(name,before,after)==self


def prove_additions(source,workspace):
    result={}
    for path in (source/'test/fixtures/rules/std_rule_cases').glob('*.yml'):
        name=path.relative_to(source).as_posix();other=workspace/name
        if other.is_file() and path.read_bytes()!=other.read_bytes():
            result[name]=RuleFixtureAddition.prove(name,path.read_bytes(),other.read_bytes())
    return result
