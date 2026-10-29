"""Publish multi-file results through the existing request ledger.

Every member is frozen in SQLite before publication. A single directory rename
publishes the set; reconciliation never reruns the producing algorithm or DLL.
"""
from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path

from .core import Bridge, BridgeError, json_text, sha


def _members(db, identity):
    return db.execute('SELECT rowid,name,digest,length(data) AS size FROM artifact_members WHERE request_id=? ORDER BY name',(identity,)).fetchall()


def _file_sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def bundle_status(db, row):
    receipt=json.loads(row['receipt'])
    output=Path(receipt['output'])
    members=_members(db,row['id'])
    actual={p.name for p in output.parent.iterdir()} if output.parent.is_dir() else set()
    matches=bool(members) and actual=={m['name'] for m in members} and all(
        (output.parent/m['name']).is_file() and _file_sha(output.parent/m['name'])==m['digest'] for m in members)
    return receipt|{'state':'OUTPUT_CHANGED' if row['state']=='APPLIED' and not matches else row['state'],
                    'output_exists':output.exists(),'output_matches':matches}


def apply_bundle(bridge,db,row):
    receipt=json.loads(row['receipt']);output=bridge.destination_path(receipt['output']);members=_members(db,row['id'])
    if output.parent.exists():
        if not bundle_status(db,row)['output_matches']:raise BridgeError('OUTPUT_CONFLICT: artifact members differ')
        db.execute("UPDATE commands SET state='APPLIED' WHERE id=?",(row['id'],))
        return receipt|{'state':'APPLIED','reconciled_existing_output':True}
    if row['state']=='APPLIED':raise BridgeError('OUTPUT_MISSING: use a new request')
    for source in receipt['sources']:
        if _file_sha(source['file'])!=source['sha256']:raise BridgeError('STALE_SOURCE')
    if not members or not any(m['name']==output.name for m in members):raise BridgeError('ARTIFACT_MEMBERS_MISSING')
    output.parent.parent.mkdir(parents=True,exist_ok=True)
    staging=output.parent.with_name('.'+output.parent.name+'.publishing')
    staging=bridge.destination_path(staging)
    staging.mkdir(exist_ok=True)
    expected={m['name']:m for m in members}
    for path in staging.iterdir():
        if path.name not in expected or path.is_symlink() or not path.is_file() or _file_sha(path)!=expected[path.name]['digest']:
            raise BridgeError('ARTIFACT_PUBLICATION_CONFLICT')
    for member in members:
        target=staging/member['name']
        if target.exists():continue
        with target.open('xb') as stream,db.blobopen('artifact_members','data',member['rowid'],readonly=True) as blob:
            while chunk:=blob.read(1024*1024):stream.write(chunk)
            stream.flush();os.fsync(stream.fileno())
    if {p.name for p in staging.iterdir()}!={m['name'] for m in members} or any(
        not (staging/m['name']).is_file() or _file_sha(staging/m['name'])!=m['digest'] for m in members):
        raise BridgeError('INCOMPLETE_ARTIFACT_PUBLICATION: inspect the owned staging directory')
    os.rename(staging,output.parent)
    if not bundle_status(db,row)['output_matches']:raise BridgeError('ARTIFACT_READBACK_FAILED')
    db.execute("UPDATE commands SET state='APPLIED' WHERE id=?",(row['id'],))
    return receipt|{'state':'APPLIED','reconciled_existing_output':False}


def begin_bundle(bridge:Bridge,payload:dict,request_id:str,sources:list[dict],primary_name:str):
    request_id=bridge.request_id(request_id)
    if Path(primary_name).name!=primary_name or any(c in primary_name for c in '/\\:<>"|?*'):
        raise BridgeError('ARTIFACT_FILENAME')
    fingerprint=sha(json_text(payload).encode('utf8'))
    stage=bridge.destination_path(bridge.root/'work/artifacts'/sha(request_id.encode())[:24])
    output=bridge.destination_path(bridge.root/'deliverables/artifacts'/request_id/primary_name)
    with bridge.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
        if old:
            if old['payload_hash']!=fingerprint:raise BridgeError('REQUEST_ID_CONFLICT')
            return stage,bridge._receipt(old),False
        for item in sources:
            path=Path(item['file']).resolve(strict=True)
            if _file_sha(path)!=item['sha256']:raise BridgeError('STALE_SOURCE')
            item['file']=str(path)
        if not sources:raise BridgeError('ARTIFACT_SOURCE_REQUIRED')
        if stage.exists():raise BridgeError('ARTIFACT_STAGE_CONFLICT')
        stage.mkdir(parents=True)
        db.execute('CREATE TABLE IF NOT EXISTS artifact_members (request_id TEXT NOT NULL,name TEXT NOT NULL,digest TEXT NOT NULL,data BLOB NOT NULL,PRIMARY KEY(request_id,name))')
        receipt={'id':request_id,'kind':'BUNDLE','artifact_type':payload['operation'],'stage':str(stage),
            'output':str(output),'sources':sources,'source_sha256':sources[0]['sha256'],'payload':payload}
        db.execute('INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)',(request_id,fingerprint,'RUNNING',sources[0]['file'],
            json_text(sources).encode('utf8'),b'','[]',json_text(receipt)))
    return stage,receipt,True


def finish_bundle(bridge:Bridge,request_id:str,files:Path,metadata:dict)->dict:
    """Freeze a completed staging set, including metadata needed after parent loss."""
    with bridge.connect() as db:
        row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row);stage=bridge.destination_path(receipt['stage'])
    files=bridge.destination_path(files)
    if files.parent!=stage or files.name!='files':raise BridgeError('ARTIFACT_STAGE_IDENTITY')
    members=[]
    for path in sorted(files.iterdir()):
        if not path.is_file() or path.is_symlink() or path.resolve().parent!=files:raise BridgeError('ARTIFACT_MEMBER_TYPE')
        members.append({'name':path.name,'sha256':_file_sha(path)})
    record={'members':members,'metadata':metadata}
    temporary=stage/'complete.tmp'
    with temporary.open('x',encoding='utf8') as stream:
        stream.write(json_text(record));stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,stage/'complete.json')
    return reconcile_bundle(bridge,request_id)


def reconcile_bundle(bridge:Bridge,request_id:str)->dict:
    bridge.request_id(request_id)
    with bridge.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
        if row is None:raise BridgeError('Unknown request_id')
        receipt=bridge._receipt(row)
        if receipt.get('kind')!='BUNDLE':raise BridgeError('ARTIFACT_REQUEST_REQUIRED')
        if row['state'] not in ('RUNNING','FAILED'):return bundle_status(db,row)
        stage=bridge.destination_path(receipt['stage']);complete=stage/'complete.json'
        if not complete.is_file():raise BridgeError('ARTIFACT_INCOMPLETE: no producing operation was replayed')
        record=json.loads(complete.read_text(encoding='utf8'));members=record['members'];paths={}
        for source in receipt['sources']:
            if _file_sha(source['file'])!=source['sha256']:raise BridgeError('STALE_SOURCE')
        for member in members:
            name=member['name']
            if Path(name).name!=name or any(c in name for c in '/\\:<>"|?*') or name in paths:raise BridgeError('ARTIFACT_FILENAME')
            path=bridge.owned_path(stage/'files'/name)
            if _file_sha(path)!=member['sha256']:raise BridgeError('ARTIFACT_CANDIDATE_CHANGED')
            paths[name]=path
        primary=Path(receipt['output']).name
        if primary not in paths and os.name=='nt':
            aliases=[name for name in paths if name.casefold()==primary.casefold()]
            if len(aliases)==1:
                primary=aliases[0]
                receipt['output']=str(Path(receipt['output']).with_name(primary))
        if primary not in paths:raise BridgeError('ARTIFACT_PRIMARY_MISSING')
        if set(record['metadata']) & {'id','kind','output','sources','stage','payload','state'}:raise BridgeError('ARTIFACT_METADATA_IDENTITY')
        receipt.update(record['metadata'],candidate_sha256=_file_sha(paths[primary]),members=members,replayed=False)
        receipt.pop('failure',None);receipt.pop('state',None)
        for member in members:
            path=paths[member['name']]
            cursor=db.execute('INSERT INTO artifact_members VALUES (?,?,?,zeroblob(?))',(request_id,member['name'],member['sha256'],path.stat().st_size))
            digest=hashlib.sha256()
            with path.open('rb') as stream,db.blobopen('artifact_members','data',cursor.lastrowid) as blob:
                while chunk:=stream.read(1024*1024):digest.update(chunk);blob.write(chunk)
            if digest.hexdigest()!=member['sha256']:raise BridgeError('ARTIFACT_CANDIDATE_CHANGED')
        db.execute("UPDATE commands SET state='PREPARED',after_blob=?,receipt=? WHERE id=?",(json_text(members).encode('utf8'),json_text(receipt),request_id))
    return receipt|{'state':'PREPARED'}


def fail_bundle(bridge,request_id,exc):
    with bridge.connect() as db:
        row=db.execute('SELECT receipt FROM commands WHERE id=?',(request_id,)).fetchone()
        receipt=json.loads(row['receipt']);receipt['failure']=str(exc)
        db.execute("UPDATE commands SET state='FAILED',receipt=? WHERE id=? AND state='RUNNING'",(json_text(receipt),request_id))
