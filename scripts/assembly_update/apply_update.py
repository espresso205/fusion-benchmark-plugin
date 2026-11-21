"""Apply a hash-bound update with a sibling backup; supports exact rollback."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import shutil
import uuid

def durable_replace(source,target):
    """Publish a flushed same-directory file; Windows flushes the rename itself."""
    if os.name=='nt':
        import ctypes
        move=ctypes.WinDLL('kernel32',use_last_error=True).MoveFileExW
        move.argtypes=(ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint32)
        move.restype=ctypes.c_int
        if not move(str(source),str(target),0x1|0x8):raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source,target)
        descriptor=os.open(str(Path(target).parent),os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)

def save_journal(path,journal):
    path=Path(path);temporary=path.parent/(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x',encoding='utf-8',newline='\n') as stream:
            json.dump(journal,stream,ensure_ascii=False,indent=2,allow_nan=False)
            stream.flush();os.fsync(stream.fileno())
        durable_replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def safe_path(root,name):
    relative=PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
        raise ValueError('unsafe update path: '+name)
    path=root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError('update path escapes target: '+name)
    return path

def replace_file(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.parent/(target.name+'.'+uuid.uuid4().hex+'.tmp')
    with temporary.open('xb') as stream:
        stream.write(Path(source).read_bytes());stream.flush();os.fsync(stream.fileno())
    durable_replace(temporary,target)

def install(package,target):
    package=Path(package).resolve();target=Path(target).resolve()
    if not target.is_dir():raise ValueError('existing plugin directory required')
    manifest=json.loads((package/'update-manifest.json').read_text(encoding='utf-8'))
    files=manifest['files']
    if not files:raise ValueError('empty update')
    for name,meta in files.items():
        source=safe_path(package/'payload',name);dest=safe_path(target,name)
        if not source.is_file() or sha(source)!=meta['sha256']:raise ValueError('update payload hash mismatch: '+name)
        actual=sha(dest) if dest.is_file() else None
        if actual!=meta['before_sha256']:raise ValueError('installed file differs from expected baseline: '+name)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    backup=target.parent/(target.name+'-before-assembly-v5-'+stamp);backup.mkdir()
    for name,meta in files.items():
        if meta['before_sha256'] is not None:
            path=safe_path(backup/'original',name);replace_file(safe_path(target,name),path)
            if sha(path)!=meta['before_sha256']:raise ValueError('backup verification failed: '+name)
    journal={'schema_version':2,'target':str(target),'files':files,'applied':[],'in_flight':None,
             'rollback_in_flight':None,'restored':[],'status':'installing'}
    journal_path=backup/'restore.json'
    def save():save_journal(journal_path,journal)
    save()
    try:
        for name in files:
            journal['in_flight']=name;save()
            replace_file(safe_path(package/'payload',name),safe_path(target,name))
            journal['applied'].append(name);journal['in_flight']=None;save()
        if any(sha(safe_path(target,n))!=m['sha256'] for n,m in files.items()):raise ValueError('installed verification failed')
        journal['status']='installed';save();return backup
    except Exception:
        rollback(target,backup);raise

def rollback(target,backup):
    target=Path(target).resolve();backup=Path(backup).resolve()
    journal=json.loads((backup/'restore.json').read_text(encoding='utf-8'))
    if target!=Path(journal['target']).resolve():raise ValueError('backup belongs to a different target')
    names=list(journal.get('applied',[]))
    if journal.get('in_flight') and journal['in_flight'] not in names:names.append(journal['in_flight'])
    for name in names:
        meta=journal['files'][name];dest=safe_path(target,name)
        actual=sha(dest) if dest.is_file() else None
        if actual not in (meta['before_sha256'],meta['sha256']):raise ValueError('updated file was modified; refusing rollback: '+name)
        if meta['before_sha256'] is not None and sha(safe_path(backup/'original',name))!=meta['before_sha256']:
            raise ValueError('backup hash mismatch: '+name)
    journal['status']='rolling_back';journal.setdefault('restored',[])
    save_journal(backup/'restore.json',journal)
    for name in reversed(names):
        meta=journal['files'][name];dest=safe_path(target,name)
        journal['rollback_in_flight']=name;save_journal(backup/'restore.json',journal)
        actual=sha(dest) if dest.is_file() else None
        if actual!=meta['before_sha256']:
            if meta['before_sha256'] is None:
                quarantine=safe_path(backup/'removed-new-files',name);quarantine.parent.mkdir(parents=True,exist_ok=True)
                durable_replace(dest,quarantine)
            else:replace_file(safe_path(backup/'original',name),dest)
        if (sha(dest) if dest.is_file() else None)!=meta['before_sha256']:raise ValueError('rollback verification failed: '+name)
        if name not in journal['restored']:journal['restored'].append(name)
        journal['rollback_in_flight']=None;save_journal(backup/'restore.json',journal)
    journal.update(status='restored',in_flight=None,rollback_in_flight=None)
    save_journal(backup/'restore.json',journal)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--rollback',type=Path);args=parser.parse_args()
    if args.rollback:rollback(args.target,args.rollback);print('Restored:',args.target)
    else:print('Installed. Backup:',install(Path(__file__).resolve().parent,args.target))

if __name__=='__main__':main()
