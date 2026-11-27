"""Publish multi-file results through the existing request ledger.

The ledger binds each member to immutable storage before publication. A single
directory rename publishes the set. Reconciliation consumes saved bytes only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .core import Bridge, BridgeError, json_text, sha
from .object_store import ContentAddressedStore, ObjectStoreError, copy_verified, file_sha256 as _file_sha


def _members(db, identity):
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name IN ('artifact_members','artifact_member_refs')")}
    members = {}
    if 'artifact_members' in tables:
        for row in db.execute("SELECT rowid,name,digest,length(data) AS size,'sqlite' AS storage "
                              "FROM artifact_members WHERE request_id=?", (identity,)):
            members[row['name']] = row
    if 'artifact_member_refs' in tables:
        for row in db.execute("SELECT name,digest,size,storage FROM artifact_member_refs WHERE request_id=?",
                              (identity,)):
            if row['storage'] != 'cas':
                raise BridgeError('ARTIFACT_STORAGE_UNSUPPORTED')
            old = members.get(row['name'])
            if old is not None and (old['digest'], old['size']) != (row['digest'], row['size']):
                raise BridgeError('ARTIFACT_MEMBER_CONFLICT')
            members[row['name']] = row
    return [members[name] for name in sorted(members)]


def _check_source(source):
    # An offline database's main-file hash does not capture a later WAL/lock.
    # Existing sources keep their original hash contract. Only new explicit
    # absence references add this condition, until immutable publication.
    if source.get('must_be_absent') is True:
        if set(source)!={'file','must_be_absent'}:raise BridgeError('ARTIFACT_SOURCE_GUARD')
        if os.path.lexists(source['file']):raise BridgeError('STALE_SOURCE: previously absent database companion appeared')
    elif _file_sha(source['file'])!=source['sha256']:raise BridgeError('STALE_SOURCE')


def _member_parts(name):
    if not isinstance(name,str):raise BridgeError('ARTIFACT_FILENAME')
    parts=name.split('/')
    if any(not part or part in {'.','..'} or any(c in part for c in '\\:<>"|?*')
           or any(ord(c)<32 for c in part) or part.endswith((' ','.')) for part in parts):
        raise BridgeError('ARTIFACT_FILENAME')
    return parts


def _member_directories(names):
    files=set();directories=set()
    for name in names:
        parts=_member_parts(name);key=name.casefold() if os.name=='nt' else name
        if key in files:raise BridgeError('ARTIFACT_FILENAME')
        files.add(key)
        directories.update('/'.join(parts[:i]) for i in range(1,len(parts)))
    keys={v.casefold() if os.name=='nt' else v for v in directories}
    if files & keys:raise BridgeError('ARTIFACT_FILENAME')
    return directories


def _disk_tree(root):
    """Regular owned files only; never follow a symlink or Windows junction."""
    root=Path(root);files={};directories=set()
    if not root.exists():return files,directories
    if not root.is_dir() or root.is_symlink() or root.is_junction():raise BridgeError('ARTIFACT_MEMBER_TYPE')
    resolved=root.resolve();pending=[root]
    while pending:
        folder=pending.pop()
        for path in folder.iterdir():
            name=path.relative_to(root).as_posix();_member_parts(name)
            if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(resolved):raise BridgeError('ARTIFACT_MEMBER_TYPE')
            if path.is_dir():directories.add(name);pending.append(path)
            elif path.is_file():files[name]=path
            else:raise BridgeError('ARTIFACT_MEMBER_TYPE')
    return files,directories


def _tree_matches(root,members):
    try:
        actual,directories=_disk_tree(root);expected=_member_directories(m['name'] for m in members)
        return bool(members) and set(actual)=={m['name'] for m in members} and directories==expected and all(
            _file_sha(actual[m['name']])==m['digest'] for m in members)
    except (BridgeError,OSError):return False


def bundle_status(db, row):
    receipt=json.loads(row['receipt'])
    output=Path(receipt['output'])
    members=_members(db,row['id'])
    matches=_tree_matches(output.parent,members)
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
        _check_source(source)
    if not members or not any(m['name']==output.name for m in members):raise BridgeError('ARTIFACT_MEMBERS_MISSING')
    output.parent.parent.mkdir(parents=True,exist_ok=True)
    staging=output.parent.with_name('.'+output.parent.name+'.publishing')
    staging=bridge.destination_path(staging)
    staging.mkdir(exist_ok=True)
    expected={m['name']:m for m in members}
    directories=_member_directories(m['name'] for m in members)
    actual,actual_directories=_disk_tree(staging)
    if not actual_directories<=directories:raise BridgeError('ARTIFACT_PUBLICATION_CONFLICT')
    for name,path in actual.items():
        if name not in expected or _file_sha(path)!=expected[name]['digest']:
            raise BridgeError('ARTIFACT_PUBLICATION_CONFLICT')
    store = ContentAddressedStore(bridge.root / 'evidence' / 'objects')
    for member in members:
        target=staging.joinpath(*_member_parts(member['name']))
        if target.exists():continue
        target.parent.mkdir(parents=True,exist_ok=True)
        try:
            if member['storage'] == 'cas':
                store.copy_to(member['digest'], target, expected_size=member['size'], scratch=staging.parent)
            else:
                with db.blobopen('artifact_members', 'data', member['rowid'], readonly=True) as blob:
                    copy_verified(blob, target, member['digest'], member['size'], staging.parent)
        except ObjectStoreError as exc:
            raise BridgeError(str(exc)) from exc
    if not _tree_matches(staging,members):
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
        old=db.execute('SELECT payload_hash,state,receipt FROM commands WHERE id=?',(request_id,)).fetchone()
        if old:
            if old['payload_hash']!=fingerprint:raise BridgeError('REQUEST_ID_CONFLICT')
            return stage,bridge._receipt(old),False
        for item in sources:
            path=Path(item['file']).resolve(strict=item.get('must_be_absent') is not True)
            item['file']=str(path)
            _check_source(item)
        if not sources:raise BridgeError('ARTIFACT_SOURCE_REQUIRED')
        if stage.exists():raise BridgeError('ARTIFACT_STAGE_CONFLICT')
        stage.mkdir(parents=True)
        receipt={'id':request_id,'kind':'BUNDLE','artifact_type':payload['operation'],'stage':str(stage),
            'output':str(output),'sources':sources,'source_sha256':sources[0]['sha256'],'payload':payload}
        db.execute('INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)',(request_id,fingerprint,'RUNNING',sources[0]['file'],
            json_text(sources).encode('utf8'),b'','[]',json_text(receipt)))
    return stage,receipt,True


def finish_bundle(bridge:Bridge,request_id:str,files:Path,metadata:dict)->dict:
    """Freeze a completed staging set, including metadata needed after parent loss."""
    with bridge.connect() as db:
        row=db.execute('SELECT state,receipt FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row);stage=bridge.destination_path(receipt['stage'])
    files=bridge.destination_path(files)
    if files.parent!=stage or files.name!='files':raise BridgeError('ARTIFACT_STAGE_IDENTITY')
    actual,directories=_disk_tree(files)
    if directories!=_member_directories(actual):raise BridgeError('ARTIFACT_MEMBER_TYPE')
    members=[{'name':name,'sha256':_file_sha(path)} for name,path in sorted(actual.items())]
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
        row=db.execute('SELECT id,state,receipt FROM commands WHERE id=?',(request_id,)).fetchone()
        if row is None:raise BridgeError('Unknown request_id')
        receipt=bridge._receipt(row)
        if receipt.get('kind')!='BUNDLE':raise BridgeError('ARTIFACT_REQUEST_REQUIRED')
        if row['state'] not in ('RUNNING','FAILED'):return bundle_status(db,row)
        stage=bridge.destination_path(receipt['stage']);complete=stage/'complete.json'
        if not complete.is_file():raise BridgeError('ARTIFACT_INCOMPLETE: no producing operation was replayed')
        record=json.loads(complete.read_text(encoding='utf8'));members=record['members']
        names={member['name'] for member in members}
        directories=_member_directories(member['name'] for member in members)
        for source in receipt['sources']:
            _check_source(source)
        actual,actual_directories=_disk_tree(stage/'files')
        if set(actual)!=names or actual_directories!=directories:
            raise BridgeError('ARTIFACT_CANDIDATE_CHANGED')
        primary=Path(receipt['output']).name
        if primary not in names and os.name=='nt':
            aliases=[name for name in names if name.casefold()==primary.casefold()]
            if len(aliases)==1:
                primary=aliases[0]
                receipt['output']=str(Path(receipt['output']).with_name(primary))
        if primary not in names:raise BridgeError('ARTIFACT_PRIMARY_MISSING')
        if set(record['metadata']) & {'id','kind','output','sources','stage','payload','state'}:raise BridgeError('ARTIFACT_METADATA_IDENTITY')
        digests={member['name']:member['sha256'] for member in members}
        receipt.update(record['metadata'],candidate_sha256=digests[primary],members=members,replayed=False)
        receipt.pop('failure',None);receipt.pop('state',None)
        db.execute('''CREATE TABLE IF NOT EXISTS artifact_member_refs (
            request_id TEXT NOT NULL, name TEXT NOT NULL, digest TEXT NOT NULL,
            size INTEGER NOT NULL, storage TEXT NOT NULL, PRIMARY KEY(request_id, name))''')
        store = ContentAddressedStore(bridge.root / 'evidence' / 'objects')
        for member in members:
            name = member['name']
            try:
                obj = store.put(actual[name], expected_digest=member['sha256'])
            except ObjectStoreError as exc:
                raise BridgeError(str(exc)) from exc
            db.execute('INSERT INTO artifact_member_refs(request_id,name,digest,size,storage) VALUES (?,?,?,?,?)',
                       (request_id, name, obj.digest, obj.size, 'cas'))
        db.execute("UPDATE commands SET state='PREPARED',after_blob=?,receipt=? WHERE id=?",(json_text(members).encode('utf8'),json_text(receipt),request_id))
    return receipt|{'state':'PREPARED'}


def fail_bundle(bridge,request_id,exc):
    with bridge.connect() as db:
        row=db.execute('SELECT receipt FROM commands WHERE id=?',(request_id,)).fetchone()
        receipt=json.loads(row['receipt']);receipt['failure']=str(exc)
        db.execute("UPDATE commands SET state='FAILED',receipt=? WHERE id=? AND state='RUNNING'",(json_text(receipt),request_id))
