#!/usr/bin/env python3
"""Check source syntax, literal documentation links and package content hashes."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote

root=Path(__file__).resolve().parents[1]
errors=[];python_count=0;links=0
for folder in ('src','tests','scripts','examples'):
    for file in (root/folder).rglob('*.py'):
        ast.parse(file.read_text(encoding='utf-8'),filename=str(file));python_count+=1
for file in [root/'README.md',root/'SECURITY.md',*(root/'docs').glob('*.md')]:
    text=file.read_text(encoding='utf-8')
    if len(re.findall(r'^```',text,re.M))%2:errors.append(str(file)+' unpaired code fence')
    for target in re.findall(r'\]\(([^)]+)\)',text):
        if '://' in target or target.startswith('#'):continue
        links+=1
        if not (file.parent/unquote(target.split('#')[0])).exists():errors.append(str(file)+' broken link '+target)
manifest=root/'SHA256SUMS'
verified=0
if manifest.exists():
    for line in manifest.read_text().splitlines():
        expected,rel=line.split('  ',1)
        file=root/rel
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=expected:errors.append('hash mismatch: '+rel)
        verified+=1
print(json.dumps({'python_files':python_count,'documentation_links':links,'verified_hashes':verified,'errors':errors},indent=2))
raise SystemExit(bool(errors))
