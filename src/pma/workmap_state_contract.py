"""History-free current/meaning contract (isolated; no legacy adapters)."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

FORMAT = 'workmap-state-1'
COLLECTIONS = ('pins', 'permissions', 'action_rules')
MEANING_KINDS = {'rationale', 'correction', 'refinement', 'dissent', 'adoption', 'close', 'reopen', 'reparent'}
OPERATIONS = ('append_meaning','update_work','add_work','set_pin','remove_pin','move_pin',
    'set_permission','remove_permission','set_action_rule','remove_action_rule','start_review','finish_review',
    'record_result','set_relation','remove_relation','reparent','close','reopen','release_scope','accept_scope')


class StateInvalid(ValueError): pass
class StateDenied(ValueError): pass
class StateUnknown(ValueError): pass
class StateConflict(ValueError): pass


def now(): return datetime.now(timezone.utc).isoformat()
def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
def nonempty(value): return isinstance(value, str) and bool(value.strip())
def refs(value): return isinstance(value, list) and all(nonempty(x) for x in value)
def version_ref(ref,content):return ref+'#sha256='+hashlib.sha256(content).hexdigest()
def is_version_ref(ref):return isinstance(ref,str) and re.fullmatch(r'.+#sha256=[0-9a-f]{64}',ref) is not None
def timestamp(value):
    try: return isinstance(value, str) and datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
    except ValueError: return False
def require(condition, message):
    if not condition: raise StateInvalid(message)


def collection_status(state, name, key):
    """Same current-only completeness meaning as the established contract."""
    c = state['metadata']['completeness']
    if c == 'full': return 'confirmed'
    scope = c.get(name)
    return 'confirmed' if scope == 'full' or isinstance(scope, dict) and key in scope['confirmed_work_keys'] else 'unknown'


def parents(state):
    return {r['child']: r['parent'] for r in state['map']['relations'] if r['kind'] == 'parent'}


def chain(state, key):
    p = parents(state); result = [key]
    while result[-1] in p:
        if p[result[-1]] in result: raise StateInvalid('Parent cycle')
        result.append(p[result[-1]])
    return result[::-1]


def subtree(state, key):
    result = [key]
    for k in result:
        for child, parent in parents(state).items():
            if parent == k:
                if child in result: raise StateInvalid('Parent cycle')
                result.append(child)
    return result


def address_prefix(address): return 'W' if address in ('W0', 'W00') else address + '.'


def validate_work(work, key):
    require(isinstance(work, dict) and set(work) == {'work_key', 'name', 'intent', 'origin'}, 'Work has four identity fields')
    require(work['work_key'] == key and nonempty(key) and nonempty(work['name']), 'Work identity invalid')
    require(work['intent'] is None or nonempty(work['intent']), 'Intent invalid')
    origin = work['origin']
    require(isinstance(origin, dict) and set(origin) == {'summary', 'refs'} and refs(origin['refs']), 'Origin invalid')
    require(origin['summary'] is None or nonempty(origin['summary']), 'Origin summary invalid')


def unverified_source(state, ref):
    migration = state['metadata'].get('migration')
    return not is_version_ref(ref) or migration is not None and ref in migration['missing_versioned_sources']


def permission_authority_status(state, row):
    return 'unknown' if any(unverified_source(state, ref) for ref in row['source_refs']) else 'versioned'


def validate_migration(migration, known):
    fields = {'legacy_revision', 'missing_versioned_sources', 'legacy_permissions', 'work_sources', 'review_notes'}
    require(isinstance(migration, dict) and set(migration) == fields
            and nonempty(migration['legacy_revision']), 'invalid trial migration scope')
    missing = migration['missing_versioned_sources']
    require(refs(missing) and len(set(missing)) == len(missing) and all(map(is_version_ref, missing)),
            'invalid unresolved versioned sources')
    require(refs(migration['legacy_permissions']), 'invalid legacy permission scope')
    sources = migration['work_sources']
    require(isinstance(sources, dict) and set(sources) <= known and all(
        isinstance(fields, dict) and set(fields) <= {'identity', 'intent', 'origin', 'parent'}
        and all(map(refs, fields.values())) for fields in sources.values()), 'invalid imported Work sources')
    notes = migration['review_notes']
    require(isinstance(notes, list) and all(isinstance(note, dict) and set(note) <= {
        'id', 'targets', 'text', 'basis', 'refs', 'observed_at'} and nonempty(note.get('id'))
        and refs(note.get('targets')) and set(note['targets']) <= known and nonempty(note.get('text'))
        and refs(note.get('refs')) and nonempty(note.get('basis')) for note in notes), 'invalid migration review notes')


def validate(state):
    """Reject technical-history fields rather than carrying empty legacy slots."""
    fields = {'format_version','project_id','map_id','revision','recorded_at','works','display_ids',
              'aliases','map','retirements','thread_entries','evidence','movements','metadata'}
    require(isinstance(state, dict) and set(state) == fields and state['format_version'] == FORMAT, 'unsupported state contract/fields')
    require(all(nonempty(state[k]) for k in ('project_id','map_id','revision')) and timestamp(state['recorded_at']), 'state identity/time invalid')
    for k in ('works','display_ids','aliases','map','retirements','movements','metadata'):
        require(isinstance(state[k], dict), k + ' invalid')
    meta = state['metadata']
    base = {'store_role','completeness','unconfirmed_intents'}
    require(set(meta) in (base, base | {'migration'}), 'metadata fields invalid')
    require(meta['store_role'] in ('canonical_current','recovery_candidate','cutover_source'), 'Store role invalid')
    require(refs(meta['unconfirmed_intents']), 'unconfirmed Intent scope invalid')
    m = state['map']
    require(set(m) == {'members','relations','pins','permissions','action_rules'}, 'Map fields invalid')
    for k in m: require(isinstance(m[k], list), k + ' missing/unreadable')
    members = set(m['members']); known = set(state['display_ids'])
    migration = meta.get('migration')
    if 'migration' in meta:
        validate_migration(migration, known)
    require(set(meta['unconfirmed_intents'])<=members,'unconfirmed Intent target invalid')
    require(len(members) == len(m['members']) and members == set(state['works']) and members <= known, 'current Work membership invalid')
    require(refs(list(state['display_ids'].values())) and len(set(state['display_ids'].values())) == len(known), 'display IDs invalid')
    require(all(state['aliases'].get(a) == k for k,a in state['display_ids'].items()) and set(state['aliases'].values()) <= known, 'alias invalid')
    require(all(re.fullmatch(r'W[0-9]+(?:\.[1-9][0-9]*)*', a) for a in state['aliases']), 'alias address invalid')
    for k,w in state['works'].items(): validate_work(w,k)
    complete = meta['completeness']
    require(complete == 'full' or isinstance(complete, dict) and set(complete) <= set(COLLECTIONS), 'completeness invalid')
    if isinstance(complete, dict):
        for scope in complete.values():
            require(scope in (None, 'unknown', 'full') if not isinstance(scope, dict) else
                    set(scope) == {'confirmed_work_keys'} and refs(scope['confirmed_work_keys'])
                    and len(set(scope['confirmed_work_keys'])) == len(scope['confirmed_work_keys'])
                    and set(scope['confirmed_work_keys']) <= members, 'completeness scope invalid')
    ids = set(); parent_children = set()
    for r in m['relations']:
        require(isinstance(r,dict) and nonempty(r.get('id')) and r['id'] not in ids, 'relation ID invalid')
        ids.add(r['id'])
        if r.get('kind') == 'parent':
            require(set(r) == {'id','kind','child','parent'} and r['child'] in members and r['parent'] in members
                    and r['child'] != r['parent'] and r['child'] not in parent_children, 'Parent invalid')
            parent_children.add(r['child'])
        else:
            require(set(r) == {'id','kind','consumer','provider','action','requires','refs'} and r['kind'] == 'dependency'
                    and r['consumer'] in members and r['provider'] in known and nonempty(r['action'])
                    and nonempty(r['requires']) and refs(r['refs']) and bool(r['refs']), 'Dependency invalid')
    for k in members:
        path=chain(state,k); address=state['display_ids'][k]
        prefix=address_prefix(state['display_ids'][path[-2]]) if len(path)>1 else 'W'
        require(re.fullmatch(re.escape(prefix)+r'[0-9]+' if len(path)==1 else re.escape(prefix)+r'[1-9][0-9]*',address), 'address/Parent mismatch')
    active=set(); pin_ids=set()
    for p in m['pins']:
        fields = {'id','work_id','actor_id','mode','pinned_at','confirmed_at','refs'}
        extra = {'where'} if migration is not None else set()
        require(isinstance(p,dict) and fields <= set(p) <= fields | extra
                and ('where' not in p or nonempty(p['where']))
                and nonempty(p['id']) and p['id'] not in pin_ids and p['work_id'] in members and nonempty(p['actor_id'])
                and p['mode'] in ('active','held') and timestamp(p['pinned_at']) and timestamp(p['confirmed_at'])
                and refs(p['refs']) and bool(p['refs']), 'Pin invalid')
        pin_ids.add(p['id'])
        if p['mode']=='active':
            require(p['actor_id'] not in active,'multiple active Pins');active.add(p['actor_id'])
    for name,source_field in (('permissions','source_refs'),('action_rules','refs')):
        seen=set()
        for v in m[name]:
            require(isinstance(v,dict) and nonempty(v.get('id')) and v['id'] not in seen and refs(v.get('targets'))
                    and bool(v['targets']) and set(v['targets']) <= known and refs(v.get(source_field)) and bool(v[source_field]), name+' identity/scope invalid')
            seen.add(v['id'])
            if name=='permissions':
                require(set(v) == {'id','targets','operations','grantee','effect','constraints','source_refs','grantor_ref'}
                        and refs(v['operations']) and bool(v['operations']) and nonempty(v['grantee'])
                        and v['effect'] in ('allow','deny','unknown') and refs(v['constraints']) and nonempty(v['grantor_ref'])
                        and (all(is_version_ref(ref) for ref in v['source_refs']) or migration is not None
                             and v['id'] in migration['legacy_permissions']), 'Permission/current source version invalid')
            else:
                extra = {'legacy_scope','clears_when','observed_at'} if migration is not None else set()
                require(set(v) <= {'id','targets','statement','refs','applies_to','time_role'} | extra
                        and nonempty(v.get('statement')) and refs(v.get('applies_to',[])), 'Action Rule invalid')
    entries={}
    require(isinstance(state['thread_entries'],list) and isinstance(state['evidence'],list),'meaning/results invalid')
    for e in state['thread_entries']:
        required={'id','works','kind','summary','why','source_refs'}
        optional={'actor','occurred_at','recorded_at','related_entries','identity','outcome','successor','residuals','revisit','change','parent'}
        if migration is not None:
            optional |= {'original_reasons','historical','related_results','close_display_id'}
        require(isinstance(e,dict) and required <= set(e) <= required|optional and nonempty(e['id']) and e['id'] not in entries
                and e['kind'] in MEANING_KINDS and refs(e['works']) and bool(e['works']) and set(e['works']) <= known
                and nonempty(e['summary']) and nonempty(e['why']) and refs(e['source_refs']) and bool(e['source_refs']), 'meaning record invalid/technical fields forbidden')
        for f in ('occurred_at','recorded_at'):
            if f in e: require(e[f] is None or timestamp(e[f]),'meaning time invalid')
        if 'actor' in e: require(nonempty(e['actor']),'meaning actor invalid')
        if 'identity' in e: validate_work(e['identity'],e['identity']['work_key'])
        if 'parent' in e: require(e['parent'] is None or e['parent'] in known,'meaning Parent unavailable')
        if 'change' in e:
            change=e['change']
            if e['kind'] in ('correction','refinement'):
                require(isinstance(change,dict) and set(change)=={'before','after'},'identity change invalid')
                for work in change.values():validate_work(work,work['work_key'])
            elif e['kind']=='reparent':
                require(isinstance(change,dict) and set(change)=={'old_parent','new_parent','old_addresses','new_addresses'},'reparent change invalid')
            else:require(False,'generic audit payload is not meaning')
        entries[e['id']]=e
    for e in entries.values():
        require(refs(e.get('related_entries',[])) and set(e.get('related_entries',[])) <= set(entries),'missing meaning/correction link')
    evidence_ids=set()
    for e in state['evidence']:
        fields={'id','works','summary','source_refs'}
        optional={'actor','why','recorded_at','occurred_at'} if migration is not None else set()
        require(isinstance(e,dict) and fields <= set(e) <= fields | optional and nonempty(e['id'])
                and e['id'] not in evidence_ids and refs(e['works']) and set(e['works']) <= known
                and nonempty(e['summary']) and refs(e['source_refs']) and bool(e['source_refs']), 'Evidence invalid')
        evidence_ids.add(e['id'])
    for e in entries.values():
        require(refs(e.get('related_results',[])) and set(e.get('related_results',[])) <= evidence_ids, 'missing result link')
        originals = e.get('original_reasons',[])
        require(isinstance(originals,list) and all(isinstance(x,dict) and nonempty(x.get('id'))
                and nonempty(x.get('summary')) and nonempty(x.get('why'))
                and refs(x.get('source_refs')) and refs(x.get('evidence_refs',[])) for x in originals), 'meaning original invalid')
    for k,eid in state['retirements'].items():
        e=entries.get(eid,{})
        require(k in known-members and e.get('kind')=='close' and e.get('identity',{}).get('work_key')==k
                and e.get('outcome') in ('completed','stopped','replaced'), 'closed identity missing')
    moved=set()
    for root,v in state['movements'].items():
        fields={'release_id','return_of','root','keys','source_map_id','destination_map_id','payload_sha256','status','root_identity','original_parent'}
        require(isinstance(v,dict) and set(v)==fields and v['root']==root and root in known
                and refs(v['keys']) and root in v['keys'] and set(v['keys']) <= known
                and all(nonempty(v[x]) for x in ('release_id','source_map_id','destination_map_id','payload_sha256'))
                and v['source_map_id'] != v['destination_map_id'] and v['status'] in ('released','accepted','returned','reintegrated')
                and (v['return_of'] is None or nonempty(v['return_of']) and v['return_of']!=v['release_id'])
                and (v['original_parent'] is None or nonempty(v['original_parent'])), 'movement disposition invalid')
        validate_work(v['root_identity'],root)
        if v['status'] in ('released','returned'):
            require(not set(v['keys']) & members,'released scope is still current');moved.update(v['keys'])
    require(known-members <= set(state['retirements'])|moved,'noncurrent identity has no disposition')
    return state


def new_state(project_id, map_id, root_work, actor, authority_ref):
    """Explicit initial Owner setup; does not discover/modify any existing Map."""
    k=root_work['work_key']
    s=dict(format_version=FORMAT,project_id=project_id,map_id=map_id,revision=uuid4().hex,recorded_at=now(),
        works={k:deepcopy(root_work)},display_ids={k:'W0'},aliases={'W0':k},
        map=dict(members=[k],relations=[],pins=[],permissions=[dict(id='permission:'+uuid4().hex,targets=[k],
            operations=list(OPERATIONS),grantee=actor,effect='allow',constraints=['explicit_initial_owner'],source_refs=[authority_ref],grantor_ref='principal:owner')],action_rules=[]),
        retirements={},thread_entries=[],evidence=[],movements={},
        metadata=dict(store_role='canonical_current',completeness='full',unconfirmed_intents=[]))
    return validate(s)


def read_work(state, key, *, actor=None):
    validate(state)
    if key in state['map']['members']:
        migration = state['metadata'].get('migration')
        imported = {} if migration is None else dict(
            permission_source_status={p['id']: permission_authority_status(state,p)
                for p in state['map']['permissions'] if key in p['targets']},
            migration_work_sources=deepcopy(migration['work_sources'].get(key, {})),
            migration_review_notes=deepcopy([n for n in migration['review_notes'] if key in n['targets']]))
        return dict(**imported,status='current',identity=deepcopy(state['works'][key]),
            ancestors=[deepcopy(state['works'][k]) for k in chain(state,key)[:-1]],
            collections={n:dict(status=collection_status(state,n,key),recorded=deepcopy(
                [x for x in state['map'][n] if x.get('work_id')==key or key in x.get('targets',[])])) for n in COLLECTIONS},
            meaning=deepcopy([e for e in state['thread_entries'] if key in e['works']]),
            evidence=deepcopy([e for e in state['evidence'] if key in e['works']]),
            parent_collections={p:{n:dict(status=collection_status(state,n,p),recorded=deepcopy([
                x for x in state['map'][n] if x.get('work_id')==p or p in x.get('targets',[])])) for n in COLLECTIONS}
                for p in chain(state,key)[:-1]},
            own_pins=deepcopy([p for p in state['map']['pins'] if p['actor_id']==actor]) if actor else None,
            children=[dict(work_key=c,display_id=state['display_ids'][c],name=state['works'][c]['name'])
                for c,p in parents(state).items() if p==key],
            dependencies=deepcopy([x for x in state['map']['relations'] if x['kind']=='dependency' and key in (x['consumer'],x['provider'])]))
    if key in state['retirements']:
        e=next(e for e in state['thread_entries'] if e['id']==state['retirements'][key])
        return dict(status='closed',identity=deepcopy(e['identity']),meaning=deepcopy([e for e in state['thread_entries'] if key in e['works']]),
                    evidence=deepcopy([x for x in state['evidence'] if key in x['works']]))
    if key in state['movements']:
        return dict(status='moved',marker=deepcopy(state['movements'][key]))
    raise StateUnknown('identity is unavailable; not proof of absence or closure')
