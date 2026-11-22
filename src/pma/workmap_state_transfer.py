"""One-round independent branch handoff; current dispositions, no audit log."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
from pma.workmap_state_contract import (
    FORMAT, COLLECTIONS, StateDenied, StateUnknown, StateConflict, StateInvalid,
    validate, subtree, collection_status, digest, address_prefix, now,
)
from pma.workmap_state_core import prepare
from pma.workmap_state_sources import capture, checked_files, business_refs

PACKET_FORMAT='workmap-state-scope-1'


def payload_for(state,root):
    validate(state);keys=subtree(state,root);keyset=set(keys);owned=set(keys)
    closings={k:next(e for e in state['thread_entries'] if e['id']==eid) for k,eid in state['retirements'].items()}
    while True:
        more={k for k,e in closings.items() if e.get('parent') in owned}-owned
        if not more:break
        owned.update(more)
    if root in state['movements'] and state['movements'][root]['status']!='accepted':
        raise StateDenied('repeated/nested movement is outside this isolated contract')
    if any(set(m['keys']) & keyset for k,m in state['movements'].items() if k!=root):
        raise StateDenied('nested movement needs an explicit contract')
    for rel in state['map']['relations']:
        if rel['kind']=='dependency' and ((rel['consumer'] in keyset)!=(rel['provider'] in keyset)):
            raise StateDenied('cross-boundary Dependency needs explicit disposition')
    meanings=[e for e in state['thread_entries'] if set(e['works']) & owned]
    ids={e['id'] for e in meanings}
    if (any(not set(e['works'])<=owned or not set(e.get('related_entries',[]))<=ids for e in meanings)
            or any(set(e.get('related_entries',[])) & ids for e in state['thread_entries'] if e not in meanings)):
        raise StateDenied('cross-boundary meaning must not be dropped')
    results=[e for e in state['evidence'] if set(e['works']) & owned]
    if any(not set(e['works'])<=owned for e in results):raise StateDenied('shared result boundary requires disposition')
    current=state['map']
    for n in ('permissions','action_rules'):
        if any(set(x['targets']) & owned and not set(x['targets'])<=owned for x in current[n]):
            raise StateDenied('shared authority/rule must not be copied or lost')
    return dict(members=keys,owned_keys=sorted(owned),works={k:deepcopy(state['works'][k]) for k in keys},
        display_ids={k:state['display_ids'][k] for k in owned},aliases={a:k for a,k in state['aliases'].items() if k in owned},
        retirements={k:eid for k,eid in state['retirements'].items() if k in owned},
        relations=deepcopy([x for x in current['relations'] if
            (x['kind']=='parent' and x['child'] in keyset and x['parent'] in keyset) or
            (x['kind']=='dependency' and x['consumer'] in keyset and x['provider'] in keyset)]),
        pins=deepcopy([x for x in current['pins'] if x['work_id'] in keyset]),
        permissions=deepcopy([x for x in current['permissions'] if set(x['targets'])<=owned]),
        action_rules=deepcopy([x for x in current['action_rules'] if set(x['targets'])<=owned]),
        thread_entries=deepcopy(meanings),evidence=deepcopy(results),
        completeness={n:[k for k in keys if collection_status(state,n,k)=='confirmed'] for n in COLLECTIONS},
        unconfirmed_intents=[k for k in keys if k in state['metadata']['unconfirmed_intents']])


def packet_for(state,root,destination_map_id,resolver,meaning=None):
    prospective=deepcopy(state);prospective['thread_entries'].extend(deepcopy(meaning or []));validate(prospective)
    payload=payload_for(prospective,root)
    payload['source_files']=capture(payload,resolver)
    previous=state['movements'].get(root)
    if previous is not None and destination_map_id!=previous['source_map_id']:
        raise StateDenied('return destination differs')
    if destination_map_id==state['map_id']:raise StateDenied('recipient must be another Map')
    return dict(format=PACKET_FORMAT,project_id=state['project_id'],source_map_id=state['map_id'],
        destination_map_id=destination_map_id,release_id='release:'+uuid4().hex,
        return_of=previous['release_id'] if previous else None,root=root,payload=payload,
        original_parent=next((x['parent'] for x in state['map']['relations'] if x.get('child')==root),None),
        payload_sha256=digest(payload),committed_revision=None)


def _packet(packet):
    fields={'format','project_id','source_map_id','destination_map_id','release_id','return_of','root','original_parent','payload','payload_sha256','committed_revision'}
    if not isinstance(packet,dict) or set(packet)!=fields or packet['format']!=PACKET_FORMAT or digest(packet['payload'])!=packet['payload_sha256']:
        raise StateInvalid('handoff packet changed/invalid')
    if packet['return_of']==packet['release_id']:raise StateInvalid('return generation refers to itself')
    p=packet['payload']
    fields={'members','owned_keys','works','display_ids','aliases','retirements','relations','pins','permissions','action_rules','thread_entries','evidence','completeness','unconfirmed_intents','source_files'}
    if not isinstance(p,dict) or set(p)!=fields or packet['root'] not in p['members']:raise StateInvalid('payload fields/root invalid')
    checked_files(p['source_files'])
    if set(p['source_files'])!=set(business_refs(p)):raise StateInvalid('handoff business source coverage differs')
    validate(dict(format_version=FORMAT,project_id=packet['project_id'],map_id=packet['source_map_id'],revision=packet['release_id'],recorded_at=now(),
        works=p['works'],display_ids=p['display_ids'],aliases=p['aliases'],
        map={n:p[n] for n in ('members','relations','pins','permissions','action_rules')},retirements=p['retirements'],thread_entries=p['thread_entries'],evidence=p['evidence'],movements={},
        metadata=dict(store_role='canonical_current',completeness={n:{'confirmed_work_keys':p['completeness'][n]} for n in COLLECTIONS},unconfirmed_intents=p['unconfirmed_intents'])))


def _marker(packet,status):
    return {**{k:deepcopy(packet[k]) for k in ('release_id','return_of','root','original_parent','source_map_id','destination_map_id','payload_sha256')},
        'keys':list(packet['payload']['owned_keys']),'status':status,
        'root_identity':deepcopy(packet['payload']['works'][packet['root']])}


def change_scope(core,s,q,review,bound):
    value=q['value']
    if not isinstance(value,dict) or set(value)!={'packet','packet_ref','display_id'}:raise StateInvalid('handoff request fields invalid')
    packet=value['packet'];_packet(packet);root=packet['root'];keys=set(packet['payload']['members']);owned=set(packet['payload']['owned_keys'])
    ref=value['packet_ref']
    if ref not in bound:raise StateDenied('durable handoff artifact must be bound')
    try: actual=json.loads(core.resolver(ref))
    except (ValueError,TypeError) as exc:raise StateUnknown('handoff artifact unavailable') from exc
    if actual!=packet:raise StateConflict('artifact differs from reviewed packet')
    if packet['project_id']!=s['project_id']:raise StateDenied('different project')
    if q['operation']=='release_scope':
        prospective=deepcopy(s);prospective['thread_entries'].extend(deepcopy(q['meaning']))
        if (root!=q['target'] or packet['source_map_id']!=s['map_id'] or packet['committed_revision'] is not None
                or packet['original_parent']!=next((x['parent'] for x in s['map']['relations'] if x.get('child')==root),None)
                or {k:v for k,v in packet['payload'].items() if k!='source_files'}!=payload_for(prospective,root) or q['expected']!=s['works'][root]):
            raise StateConflict('release scope changed')
        for k in keys:core._permission(s,k,'release_scope',q['actor_id'],review,bound)
        old=s['movements'].get(root)
        if packet['return_of']!=(old['release_id'] if old else None):raise StateConflict('return generation differs')
        if old is not None and packet['destination_map_id']!=old['source_map_id']:raise StateDenied('wrong return destination')
        s['movements'][root]=_marker(packet,'returned' if old else 'released')
        m=s['map'];m['members']=[k for k in m['members'] if k not in keys]
        for k in keys:del s['works'][k]
        m['relations']=[x for x in m['relations'] if not any(x.get(f) in keys for f in ('child','parent','consumer','provider'))]
        for n in ('pins','permissions','action_rules'):
            m[n]=[x for x in m[n] if not (x.get('work_id') in owned or set(x.get('targets',[])) & owned)]
        s['retirements']={k:eid for k,eid in s['retirements'].items() if k not in owned}
        s['thread_entries']=[x for x in s['thread_entries'] if not set(x['works']) & owned]
        s['evidence']=[x for x in s['evidence'] if not set(x['works']) & owned]
        core._remove_completeness(s,keys)
    else:
        if not review.actual_acceptance:raise StateDenied('trusted committed-export/recipient review required')
        if packet['committed_revision'] is None or packet['destination_map_id']!=s['map_id'] or q['expected'] is not None:
            raise StateDenied('not a committed export for this recipient')
        previous=s['movements'].get(root)
        if packet['return_of'] is None:
            if previous is not None or owned & set(s['display_ids']):raise StateConflict('scope already known/processed; not fresh')
        elif (previous is None or previous['status']!='released' or previous['release_id']!=packet['return_of']
                or previous['destination_map_id']!=packet['source_map_id']):
            raise StateConflict('return does not match the released generation')
        elif q['target'] != previous['original_parent']:
            # Accepting a handoff does not authorize a Parent change.
            raise StateDenied('return to the original Parent before a separately authorized reparent')
        if previous and (owned & set(s['display_ids']))-set(previous['keys']):raise StateConflict('return collides with another known Work identity')
        if keys & set(s['map']['members']):raise StateConflict('Work already current')
        p=packet['payload'];m=s['map'];old_members=list(m['members'])
        if p['pins'] and any(x['mode']=='active' for x in p['pins']):
            if any(collection_status(s,'pins',k)!='confirmed' and (k,'pins') not in review.unknown_scopes for k in old_members):
                raise StateUnknown('recipient active Pin exclusivity unknown')
        before={n:[k for k in old_members if collection_status(s,n,k)=='confirmed'] for n in COLLECTIONS}
        addresses={root:value['display_id']}
        pending=set(p['owned_keys'])-{root}
        local_parents={x['child']:x['parent'] for x in p['relations'] if x['kind']=='parent'}
        local_parents.update({k:next(e for e in p['thread_entries'] if e['id']==eid).get('parent') for k,eid in p['retirements'].items()})
        while pending:
            ready={k for k in pending if local_parents.get(k) in addresses}
            if not ready:raise StateUnknown('carried Parent/address needs explicit placement')
            for k in ready:
                parent=local_parents[k];old_address=p['display_ids'][k]
                suffixes={old_address[len(address_prefix(a)):] for a,w in p['aliases'].items() if w==parent
                    and old_address.startswith(address_prefix(a)) and old_address[len(address_prefix(a)):].isdigit()}
                if len(suffixes)!=1:raise StateUnknown('historical address needs explicit placement')
                addresses[k]=address_prefix(addresses[parent])+next(iter(suffixes))
            pending-=ready
        for k,mapped in addresses.items():
            if mapped in s['aliases'] and s['aliases'][mapped]!=k:raise StateConflict('recipient address already used')
            s['display_ids'][k]=mapped;s['aliases'][mapped]=k
        s['works'].update(deepcopy(p['works']));s['retirements'].update(p['retirements'])
        m['members'].extend(p['members'])
        for n in ('relations','pins','permissions','action_rules'):m[n].extend(deepcopy(p[n]))
        m['relations'].append(dict(id='relation:'+uuid4().hex,kind='parent',child=root,parent=q['target']))
        for name in ('thread_entries','evidence'):
            for entry in p[name]:
                old=next((e for e in s[name] if e['id']==entry['id']),None)
                if old is not None and old!=entry:raise StateConflict('same identity has different carried meaning/result')
                if old is None:s[name].append(deepcopy(entry))
        s['metadata']['completeness']={n:{'confirmed_work_keys':before[n]+p['completeness'][n]} for n in COLLECTIONS}
        s['metadata']['unconfirmed_intents'].extend(p['unconfirmed_intents'])
        s['movements'][root]=_marker(packet,'reintegrated' if previous else 'accepted')
        return p['source_files']


def _write(path,value,*,exclusive=False):
    raw=(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode('utf-8')
    if exclusive:
        with path.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    else:
        temp=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
        with temp.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)


def prepare_release_file(store,root,destination_map_id,actor,path,ref,sources,*,resolver,meaning=None):
    """Durably save the Work payload BEFORE the source gives up current ownership."""
    path=Path(path)
    if not path.is_absolute():raise ValueError('absolute artifact path required')
    state=store.read().snapshot;packet=packet_for(state,root,destination_map_id,resolver,meaning)
    _write(path,packet,exclusive=True)
    bound={b['ref']:b for b in sources}
    bound.update({r:dict(ref=r,sha256=hashlib.sha256(raw).hexdigest()) for r,raw in checked_files(packet['payload']['source_files']).items()})
    bound[ref]={'ref':ref,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    return prepare(state,actor,'release_scope',root,expected=state['works'][root],
        value=dict(packet=packet,packet_ref=ref,display_id=None),sources=list(bound.values()),meaning=meaning)


def finalize_release_file(store,path):
    """After uncertain response, inspect current business disposition; never replay."""
    path=Path(path);packet=json.loads(path.read_bytes());_packet(packet)
    s=store.read().snapshot;marker=s['movements'].get(packet['root'])
    expected=_marker(packet,'returned' if packet['return_of'] else 'released')
    if s['metadata']['store_role']!='canonical_current' or marker!=expected or set(marker['keys']) & set(s['map']['members']):
        raise StateConflict('source is not committed and released for this artifact')
    packet['committed_revision']=s['revision'];_write(path,packet)
    return packet
