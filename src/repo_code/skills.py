"""Portable SKILL.md import/export. Import always creates a review candidate."""
import json
from pathlib import Path
from .util import RepoError,atomic_write,digest

def import_skill(knowledge,path,scope='project'):
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>128000:raise RepoError('SKILL_FILE_DENIED')
    text=path.read_text(encoding='utf-8');meta={};body=text
    if text.startswith('---\n'):
        chunks=text.split('---',2)
        if len(chunks)!=3:raise RepoError('INVALID_SKILL_FRONTMATTER')
        for line in chunks[1].splitlines():
            if not line.strip():continue
            if ':' not in line:raise RepoError('UNSUPPORTED_SKILL_METADATA')
            key,value=line.split(':',1);key=key.strip();value=value.strip()
            if key not in {'name','description'}:raise RepoError('UNSUPPORTED_SKILL_METADATA',key)
            try:value=json.loads(value)
            except ValueError:pass
            if not isinstance(value,str):raise RepoError('INVALID_SKILL_METADATA')
            meta[key]=value
        body=chunks[2].strip()
    if not body:raise RepoError('EMPTY_SKILL')
    return knowledge.draft('skill',meta.get('name',path.parent.name or path.stem),body,scope,['skill-file:'+digest(text)],{'description':meta.get('description','')})

def export_skill(knowledge,item_id,path):
    item=knowledge.store.get('knowledge',item_id)
    if not item or item['kind']!='skill' or item['status']=='REVOKED':raise RepoError('SKILL_UNAVAILABLE')
    text='---\nname: '+json.dumps(item['title'],ensure_ascii=False)+'\ndescription: '+json.dumps(item['preconditions'].get('description',item['title']),ensure_ascii=False)+'\n---\n\n'+item['body']+'\n'
    atomic_write(Path(path),text.encode());return {'path':str(path),'id':item_id,'status':item['status']}
