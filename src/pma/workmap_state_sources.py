"""Portable business evidence only, created on explicit handoff/backup/restore."""
import base64
import hashlib
import os
from pathlib import Path
from uuid import uuid4
from pma.workmap_state_contract import StateUnknown, StateConflict, StateInvalid, is_version_ref


def business_refs(value):
    """Named Work/meaning fields, not arbitrary request/check/audit payloads."""
    found=set()
    for work in value['works'].values():found.update(work['origin']['refs'])
    current=value.get('map',value)
    for name,field in (('pins','refs'),('permissions','source_refs'),('action_rules','refs')):
        for item in current[name]:found.update(item[field])
    for relation in current['relations']:
        if relation['kind'] == 'dependency':
            found.update(relation['refs'])
    for item in value['thread_entries']:
        found.update(item['source_refs'])
        if 'identity' in item:found.update(item['identity']['origin']['refs'])
        change=item.get('change',{})
        for version in ('before','after'):
            if isinstance(change.get(version),dict) and 'origin' in change[version]:found.update(change[version]['origin']['refs'])
    for item in value['evidence']:found.update(item['source_refs'])
    for item in value.get('movements',{}).values():found.update(item['root_identity']['origin']['refs'])
    migration = value.get('metadata', {}).get('migration')
    if migration is not None:
        for group in migration['work_sources'].values():
            for links in group.values():
                found.update(links)
        for note in migration['review_notes']:
            found.update(note['refs'])
        for entry in value['thread_entries']:
            for original in entry.get('original_reasons', []):
                found.update(original['source_refs'])
                found.update(original.get('evidence_refs', []))
            found.update(entry.get('historical', {}).get('additional_source_refs', []))
    return sorted(found)


def portable_refs(value):
    refs = business_refs(value)
    migration = value.get('metadata', {}).get('migration')
    if migration is None:
        return refs
    missing = set(migration['missing_versioned_sources'])
    return [ref for ref in refs if is_version_ref(ref) and ref not in missing]


def checked_files(files):
    if not isinstance(files,dict):raise StateInvalid('business source bundle invalid')
    decoded={}
    for ref,encoded in files.items():
        if not is_version_ref(ref) or not isinstance(encoded,str):raise StateUnknown('portable source needs an identified version')
        try:raw=base64.b64decode(encoded,validate=True)
        except ValueError as exc:raise StateInvalid('source bytes invalid') from exc
        if hashlib.sha256(raw).hexdigest()!=ref.rsplit('#sha256=',1)[1]:raise StateConflict('portable source content changed')
        decoded[ref]=raw
    return decoded


def capture(value,resolver):
    files={}
    for ref in portable_refs(value):
        try:raw=resolver(ref)
        except (OSError,ValueError,KeyError) as exc:raise StateUnknown('required business source unavailable') from exc
        if not isinstance(raw,bytes):raise StateUnknown('source bytes required')
        files[ref]=base64.b64encode(raw).decode('ascii')
    checked_files(files)
    return files


def folder(store):return store.path.with_name(store.path.name+'.evidence')
def file_name(ref):return hashlib.sha256(ref.encode('utf-8')).hexdigest()+'.source'


def local_file(path):
    """Use Win32 extended paths for deep local evidence files, not for display."""
    text = str(path)
    if os.name == 'nt' and len(text) >= 240 and len(path.drive) == 2 and path.drive.endswith(':'):
        return Path('\\\\?\\' + text)
    return path


def materialize(store,files):
    """Files first, then publish their references; no paths supplied by sender."""
    decoded=checked_files(files)
    root=folder(store)
    if decoded:local_file(root).mkdir(exist_ok=True)
    for ref,raw in decoded.items():
        target=local_file(root/file_name(ref))
        if target.exists():
            if target.read_bytes()!=raw:raise StateConflict('existing business source differs')
            continue
        temp=local_file(root/('w'+uuid4().hex[:12]+'.tmp'))
        with temp.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(temp,target)


def source_reader(store,external,ref):
    """Fix the source location for both checks of this request."""
    target=local_file(folder(store)/file_name(ref))
    if target.is_file():
        def read():
            raw=target.read_bytes()
            if not is_version_ref(ref) or hashlib.sha256(raw).hexdigest()!=ref.rsplit('#sha256=',1)[1]:raise StateConflict('carried business source changed')
            return raw
        return read
    return lambda:external(ref)


def resolver_for(store,external):
    def resolve(ref):
        return source_reader(store,external,ref)()
    return resolve
