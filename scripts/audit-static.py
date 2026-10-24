"""Read-only source/storage checks. Reports locations and counts, never secrets."""
import ast
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
excluded={'data','test-output','node_modules','__pycache__','.venv','.git','security','backups','backup-staging','intake'}
files=[];errors=[];candidates=[];syntax={'python':0,'javascript':0,'json':0}
patterns=[re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),re.compile(r'\b(?:ghp_|github_pat_|sk-proj-)[A-Za-z0-9_\-]{24,}'),re.compile(r'\bAKIA[A-Z0-9]{16}\b')]
for directory,dirs,names in os.walk(ROOT):
    dirs[:]=[d for d in dirs if d not in excluded]
    for name in names:
        path=Path(directory)/name;relative=path.relative_to(ROOT).as_posix()
        raw=path.read_bytes();text=raw.decode('utf-8-sig')
        files.append({'path':relative,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
        for number,line in enumerate(text.splitlines(),1):
            if any(pattern.search(line) for pattern in patterns):candidates.append({'path':relative,'line':number})
        try:
            if path.suffix=='.py':ast.parse(text,filename=relative);syntax['python']+=1
            elif path.suffix=='.json':json.loads(text);syntax['json']+=1
            elif path.suffix in {'.mjs','.js','.cjs'}:
                result=subprocess.run(['node','--check',str(path)],capture_output=True,timeout=20)
                if result.returncode:errors.append(relative+': JavaScript syntax error')
                syntax['javascript']+=1
        except (SyntaxError,ValueError):errors.append(relative+': parse error')

data=ROOT/'data'
storage={'artifact_errors':[]}
# mode=ro and query_only prohibit database/schema writes; do not construct Library.
with sqlite3.connect(data.joinpath('corpus.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
    db.execute('PRAGMA query_only=ON')
    storage['quick_check']=db.execute('PRAGMA quick_check').fetchone()[0]
    storage['foreign_key_violations']=len(db.execute('PRAGMA foreign_key_check').fetchall())
    storage['counts']={name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ['documents','jobs','chunks','memberships','file_artifacts']}
    storage['document_statuses']=dict(db.execute('SELECT status,count(*) FROM documents GROUP BY status').fetchall())
    storage['job_statuses']=dict(db.execute('SELECT status,count(*) FROM jobs GROUP BY status').fetchall())
    expected=[]
    for identity,key,manifest in db.execute('SELECT id,original_key,manifest_json FROM documents'):
        m=json.loads(manifest);expected.extend([(key,identity),(m['normalized_key'],m['normalized_sha256'])])
        for name,sha in m.get('artifact_hashes',{}).items():expected.append(((Path(key).parent/name).as_posix(),sha))
    expected.extend(db.execute('SELECT storage_key,sha256 FROM file_artifacts').fetchall())
    storage['hash_checks']=len(expected)
    for key,sha in expected:
        target=(data/key).resolve()
        if not target.is_relative_to(data.resolve()):storage['artifact_errors'].append('outside storage');continue
        try:
            with target.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
            if actual!=sha:storage['artifact_errors'].append(key+': hash mismatch')
        except OSError:storage['artifact_errors'].append(key+': unreadable')

report={'source_files':files,'syntax':syntax,'syntax_errors':errors,'secret_pattern_candidates':candidates,'secret_scan_scope':'High-confidence patterns in source/config/docs only; no token value comparison, encrypted credential inspection or Git history available.','storage':storage}
output=ROOT/'test-output'/'static-audit.json';output.parent.mkdir(exist_ok=True)
output.write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps({**report,'source_files':len(files)}))
if errors or storage['artifact_errors'] or storage['quick_check']!='ok' or storage['foreign_key_violations']:raise SystemExit(1)
