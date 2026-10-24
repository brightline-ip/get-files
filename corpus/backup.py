"""Offline, encrypted backups with an authenticated end marker and verified restore."""
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import struct
import zipfile
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .processing import WorkerLock, working_directory, file_hash
from .storage import ClosingConnection
from .publication import (download_publication_key, manifest_digest, publication_key,
                          revision_identity)

MAGIC=b'SCBK1';BLOCK=1024*1024


@contextmanager
def backup_scratch(parent):
    parent=Path(parent).resolve();parent.mkdir(parents=True,exist_ok=True)
    scratch=parent/('b-'+secrets.token_hex(8));scratch.mkdir()
    try:yield scratch
    finally:
        if scratch.exists():
            if scratch.resolve().parent!=parent:raise ValueError('Unsafe backup cleanup path')
            shutil.rmtree(scratch)


def encrypt(source,target,key):
    prefix=secrets.token_bytes(8);cipher=AESGCM(key)
    with Path(source).open('rb') as src,Path(target).open('xb') as dst:
        dst.write(MAGIC+prefix);sequence=0
        while True:
            block=src.read(BLOCK);length=struct.pack('>I',len(block));nonce=prefix+struct.pack('>I',sequence)
            dst.write(length+cipher.encrypt(nonce,block,MAGIC+nonce+length));sequence+=1
            if not block:break


def decrypt(source,target,key):
    cipher=AESGCM(key)
    with Path(source).open('rb') as src,Path(target).open('xb') as dst:
        if src.read(5)!=MAGIC:raise ValueError('Unsupported backup format')
        prefix=src.read(8);sequence=0
        if len(prefix)!=8:raise ValueError('Truncated backup')
        while True:
            length=src.read(4)
            if len(length)!=4:raise ValueError('Missing authenticated end marker')
            count=struct.unpack('>I',length)[0]
            if count>BLOCK:raise ValueError('Invalid backup block size')
            nonce=prefix+struct.pack('>I',sequence);encrypted=src.read(count+16)
            if len(encrypted)!=count+16:raise ValueError('Truncated backup block')
            data=cipher.decrypt(nonce,encrypted,MAGIC+nonce+length);sequence+=1
            if not count:
                if src.read(1):raise ValueError('Unexpected backup trailing data')
                break
            dst.write(data)


def verify_data(root):
    root=Path(root).resolve();checks=[]
    with sqlite3.connect((root/'corpus.sqlite3').as_uri()+'?mode=ro',uri=True,factory=ClosingConnection) as db:
        db.execute('PRAGMA query_only=ON')
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('Database integrity check failed')
        for identity,original,manifest in db.execute('SELECT id,original_key,manifest_json FROM documents'):
            m=json.loads(manifest);checks.extend([(original,identity),(m['normalized_key'],m['normalized_sha256'])])
            checks.extend(((Path(original).parent/name).as_posix(),sha) for name,sha in m.get('artifact_hashes',{}).items())
        checks.extend(db.execute('SELECT storage_key,sha256 FROM file_artifacts').fetchall())
        counts={name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ['documents','jobs','chunks','file_artifacts']}
    for name,sha in checks:
        path=(root/name).resolve()
        if not path.is_relative_to(root) or file_hash(path)!=sha:raise ValueError('Artifact integrity check failed')
    return {'counts':counts,'artifact_hash_checks':len(checks),'integrity':'ok'}


def rebase_publication_journals(root, selected_root=None):
    """Replace machine-local journal paths using only authenticated database identities."""
    root=Path(root).resolve();selected_root=Path(selected_root or root).resolve();database=root/'corpus.sqlite3'
    with sqlite3.connect(database,factory=ClosingConnection) as db:
        db.row_factory=sqlite3.Row
        tables={row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        document_rows=(db.execute('SELECT * FROM publication_journal').fetchall()
                       if 'publication_journal' in tables else [])
        for row in document_rows:
            manifest=json.loads(row['manifest_json'])
            normalized=manifest.get('normalized_key')
            original=manifest.get('original_key')
            if (not isinstance(normalized,str) or not isinstance(original,str)
                    or Path(normalized).is_absolute() or Path(original).is_absolute()
                    or '..' in Path(normalized).parts or '..' in Path(original).parts
                    or Path(normalized).as_posix()!=normalized or Path(original).as_posix()!=original):
                raise ValueError('Publication journal contains unsafe manifest storage keys')
            relative=Path(normalized).parent
            target=(selected_root/relative).resolve()
            if (not target.is_relative_to(selected_root) or normalized!=relative.as_posix()+'/normalized.md'
                    or Path(original).parent!=relative
                    or manifest.get('document_id')!=row['document_id']
                    or revision_identity(manifest)!=row['revision_identity']
                    or manifest_digest(manifest)!=row['manifest_sha256']
                    or publication_key(row['document_id'],row['revision_identity'])!=row['publication_key']):
                raise ValueError('Publication journal binding is invalid')
            candidate=(selected_root/'staging'/('restored-'+row['publication_key'])).resolve()
            state=row['state']
            detail=row['detail']
            if state in {'RESERVED','STAGED','VALIDATED'}:
                state='RECONCILIATION_REQUIRED';detail='staging candidate is not included in backups'
            db.execute('UPDATE publication_journal SET candidate_dir=?,target_dir=?,state=?,detail=? WHERE publication_key=?',
                       (str(candidate),str(target),state,detail,row['publication_key']))
        download_rows=(db.execute('SELECT * FROM download_publication_journal').fetchall()
                       if 'download_publication_journal' in tables else [])
        artifacts=({row['job_id']:row for row in db.execute('SELECT * FROM file_artifacts').fetchall()}
                   if download_rows else {})
        for row in download_rows:
            retrieval=json.loads(row['retrieval_json'])
            expected_key=download_publication_key(row['job_id'],row['sha256'],row['extension'],retrieval)
            extension=row['extension']
            if (expected_key!=row['publication_key'] or not isinstance(extension,str)
                    or not extension.startswith('.') or '/' in extension or '\\' in extension):
                raise ValueError('Download publication journal binding is invalid')
            relative=Path('files')/row['sha256'][:2]/row['sha256']/('original'+extension)
            target=(selected_root/relative).resolve();candidate=target.with_name('pending-'+row['job_id'])
            if not target.is_relative_to(selected_root):
                raise ValueError('Download publication target is unsafe')
            state=row['state'];detail=row['detail'];artifact=artifacts.get(row['job_id'])
            if state=='PUBLISHED':
                if (not artifact or artifact['sha256']!=row['sha256']
                        or artifact['extension']!=extension
                        or artifact['bytes']!=row['bytes']
                        or json.loads(artifact['retrieval_json'])!=retrieval
                        or Path(artifact['storage_key']).as_posix()!=relative.as_posix()):
                    raise ValueError('Published download receipt binding is invalid')
            elif state in {'RESERVED','STAGED','VALIDATED'}:
                state='RECONCILIATION_REQUIRED';detail='staging candidate is not included in backups'
            db.execute('UPDATE download_publication_journal SET candidate_path=?,target_path=?,state=?,detail=? WHERE publication_key=?',
                       (str(candidate),str(target),state,detail,row['publication_key']))


def create_backup(data,target,key):
    data=Path(data).resolve();target=Path(target).resolve()
    if target.is_relative_to(data) or target.exists():raise ValueError('Choose a new backup file outside live data')
    target.parent.mkdir(parents=True,exist_ok=True)
    with WorkerLock(data),backup_scratch(data.parent/'backup-staging') as scratch:
        snapshot=scratch/'snapshot';snapshot.mkdir()
        with sqlite3.connect((data/'corpus.sqlite3').as_uri()+'?mode=ro',uri=True,factory=ClosingConnection) as source,sqlite3.connect(snapshot/'corpus.sqlite3',factory=ClosingConnection) as dest:source.backup(dest)
        files=[(snapshot/'corpus.sqlite3','corpus.sqlite3')]
        for tree in ['documents','files']:
            for directory,dirs,names in os.walk(data/tree,followlinks=False):
                for name in dirs+names:
                    path=Path(directory)/name
                    if path.is_symlink() or path.is_junction():raise ValueError('Backup refuses reparse points')
                files.extend((Path(directory)/name,(Path(directory)/name).relative_to(data).as_posix()) for name in names)
        manifest={name:file_hash(path) for path,name in files}
        archive=scratch/'archive.zip'
        with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_STORED) as package:
            for path,name in files:package.write(path,name)
            package.writestr('backup-manifest.json',json.dumps(manifest,sort_keys=True))
        pending=scratch/'encrypted.scbk';encrypt(archive,pending,key)
        # Verify the encrypted package can be restored before committing it.
        restored=scratch/'verified';restore_backup(pending,restored,key)
        result=verify_data(restored)
        # On Windows rename refuses to overwrite a file created concurrently.
        if target.exists():raise ValueError('Backup target already exists')
        os.rename(pending,target)
        return {**result,'backup':str(target),'bytes':target.stat().st_size,'sha256':file_hash(target)}


def restore_backup(backup,destination,key):
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Restore destination must not exist')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with backup_scratch(destination.parent) as scratch:
        archive=scratch/'archive.zip';decrypt(backup,archive,key)
        output=scratch/'data';output.mkdir()
        with zipfile.ZipFile(archive) as package:
            members=package.infolist()
            if len(members)>100000:raise ValueError('Backup has too many files')
            names=[m.filename for m in members]
            if len(names)!=len(set(names)):raise ValueError('Duplicate backup members')
            manifest=json.loads(package.read('backup-manifest.json'))
            if set(names)!=set(manifest)|{'backup-manifest.json'}:raise ValueError('Backup manifest does not cover every file')
            for member in members:
                name=member.filename
                if name=='backup-manifest.json':continue
                path=Path(name)
                if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or (name!='corpus.sqlite3' and path.parts[0] not in {'documents','files'}):raise ValueError('Unsafe backup member')
                target=output/path;target.parent.mkdir(parents=True,exist_ok=True)
                with package.open(member) as src,target.open('xb') as dst:
                    while block:=src.read(BLOCK):dst.write(block)
                if file_hash(target)!=manifest[name]:raise ValueError('Backup member hash mismatch')
        rebase_publication_journals(output,destination)
        result=verify_data(output)
        os.rename(output,destination)
        return result
