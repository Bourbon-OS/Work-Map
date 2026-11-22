"""Strict current-revision updates. No receipt lookup, replay or audit writes."""
from copy import deepcopy
from dataclasses import dataclass, field
from uuid import uuid4
from pma.workmap_store_types import StoreConflict
from pma.workmap_state_contract import (
    FORMAT, COLLECTIONS, StateInvalid, StateDenied, StateUnknown, StateConflict,
    validate, validate_work, refs, nonempty, digest, chain, subtree, address_prefix,
    collection_status, now, read_work, unverified_source,
)


def prepare(state, actor, operation, target, *, expected=None, value=None, sources, meaning=None):
    validate(state)
    return dict(format_version=FORMAT,project_id=state['project_id'],map_id=state['map_id'],
        base_revision=state['revision'],actor_id=actor,operation=operation,target=target,
        expected=deepcopy(expected),value=deepcopy(value),sources=deepcopy(sources),meaning=deepcopy(meaning or []))


@dataclass(frozen=True)
class ReviewedStateRequest:
    """Ephemeral trusted-caller review, never part of a saved state."""
    request_digest: str
    state_digest: str
    unknown_scopes: frozenset = field(default_factory=frozenset)
    rule_decisions: tuple = ()
    same_purpose: bool = False
    actual_acceptance: bool = False
    actual_result: bool = False
    dependency_decisions: tuple = ()
    permission_decisions: tuple = ()
    owner_permission: bool = False
    close_outcome: str | None = None
    close_reason: str | None = None
    close_refs: tuple = ()


def review_request(state, request, *, assessments=None, rule_decisions=None,
                   same_purpose=False, actual_acceptance=False, actual_result=False, dependency_decisions=None,
                   permission_decisions=None,owner_permission=False,close_basis=None):
    """Use existing human/runtime attribution checks before supplying decisions.

    Flags are not authentication, Owner authority, or a model self-approval API.
    A positive unknown assessment must cite this request's bound source bytes.
    """
    validate(state);bound={b['ref'] for b in request['sources']};scopes=set()
    for scope,a in (assessments or {}).items():
        if (not isinstance(scope,tuple) or len(scope)!=2 or scope[0] not in state['display_ids'] or scope[1] not in COLLECTIONS
                or set(a) != {'mode','reason','refs'} or a['mode']!='current_scope'
                or not nonempty(a['reason']) or not refs(a['refs']) or not a['refs'] or not set(a['refs'])<=bound):
            raise StateUnknown('positive operation-bound assessment required')
        scopes.add(scope)
    decisions=rule_decisions or {}
    if not set(decisions)<= {r['id'] for r in state['map']['action_rules']} or any(v not in ('allows','blocks','unknown') for v in decisions.values()):
        raise StateInvalid('rule review does not match current rules')
    deps=dependency_decisions or {}
    if not set(deps)<={x['id'] for x in state['map']['relations'] if x['kind']=='dependency'} or any(v not in ('allows','blocks','unknown') for v in deps.values()):
        raise StateInvalid('dependency review does not match current relations')
    permissions=permission_decisions or {}
    if not set(permissions)<={x['id'] for x in state['map']['permissions']} or any(v not in ('allows','blocks','unknown') for v in permissions.values()):
        raise StateInvalid('permission review does not match current grants')
    if owner_permission and (request['operation']!='set_permission' or not isinstance(request['value'],dict)
            or request['value'].get('grantor_ref')!='principal:owner' or not set(request['value'].get('source_refs',[]))<=bound):
        raise StateDenied('actual Owner grant review is limited to its bound set_permission')
    closing=close_basis or {}
    if close_basis is not None and (request['operation']!='close' or set(closing)!={'outcome','intent','reason','refs'}
            or closing['outcome'] not in ('completed','stopped','replaced') or closing['intent']!=state['works'][request['target']]['intent']
            or not nonempty(closing['reason']) or not refs(closing['refs']) or not closing['refs'] or not set(closing['refs'])<=bound):
        raise StateDenied('close needs its current Intent, actual reason and bound basis')
    return ReviewedStateRequest(digest(request),digest(state),frozenset(scopes),tuple(sorted(decisions.items())),
                                same_purpose,actual_acceptance,actual_result,tuple(sorted(deps.items())),tuple(sorted(permissions.items())),owner_permission,
                                closing.get('outcome'),closing.get('reason'),tuple(closing.get('refs',[])))


class StateCore:
    def __init__(self, store, resolver, *, before_commit=None):
        from pma.workmap_state_sources import resolver_for
        self.store=store;self.external_resolver=resolver;self.resolver=resolver_for(store,resolver);self.before_commit=before_commit

    def _sources(self, request, readers=None):
        from pma.workmap_state_sources import source_reader
        if readers is None:readers={}
        bindings=request['sources']
        if not isinstance(bindings,list) or not bindings: raise StateDenied('bound sources required')
        found={}
        for b in bindings:
            if (not isinstance(b,dict) or set(b) != {'ref','sha256'} or not nonempty(b['ref'])
                    or b['ref'] in found or not isinstance(b['sha256'],str) or len(b['sha256'])!=64):
                raise StateDenied('source binding invalid')
            if b['ref'] not in readers:readers[b['ref']]=source_reader(self.store,self.external_resolver,b['ref'])
            try: actual=readers[b['ref']]()
            except (OSError,ValueError,KeyError) as exc: raise StateUnknown('source unavailable') from exc
            import hashlib
            if not isinstance(actual,bytes) or hashlib.sha256(actual).hexdigest()!=b['sha256']:
                raise StateConflict('source content changed')
            if '#sha256=' in b['ref'] and b['ref'].rsplit('#sha256=',1)[1]!=b['sha256']:
                raise StateConflict('current authority/evidence names another source version')
            found[b['ref']]=b['sha256']
        return found

    def _context(self,s,key,op,r,bound):
        if key not in s['map']['members']:
            if (op!='reopen' and not (op=='set_permission' and r.owner_permission)) or key not in s['retirements']:raise StateDenied('target is not current')
            e=next(e for e in s['thread_entries'] if e['id']==s['retirements'][key])
            if 'parent' not in e:raise StateUnknown('Parent at close unknown; do not infer a root')
            parent=e.get('parent')
            if parent is not None and parent not in s['works']:raise StateUnknown('closed Parent is not current')
            path=chain(s,parent)+[key] if parent is not None else [key]
        else:path=chain(s,key)
        if any((s['works'][k]['intent'] if k in s['works'] else e['identity']['intent']) is None
               or k in s['metadata']['unconfirmed_intents'] for k in path):
            raise StateUnknown('Work/Parent Intent unconfirmed')
        for k in path:
            for n in COLLECTIONS:
                if collection_status(s,n,k)!='confirmed' and (k,n) not in r.unknown_scopes:
                    raise StateUnknown('current '+n+' scope unconfirmed')
        decisions=dict(r.rule_decisions)
        if op in ('set_pin','move_pin','release_scope','reparent','close','remove_relation'):
            dependency_decisions=dict(r.dependency_decisions)
            for rel in s['map']['relations']:
                if rel['kind']=='dependency' and (rel['consumer'] in path or rel['provider'] in path):
                    if not set(rel['refs'])<=set(bound):raise StateUnknown('Dependency source not bound')
                    decision=dependency_decisions.get(rel['id'])
                    if decision=='blocks':raise StateDenied('Dependency blocks operation')
                    if decision!='allows':raise StateUnknown('Dependency impact unconfirmed')
        recording={'add_work','move_pin','set_pin','set_permission','remove_permission','append_meaning'}
        for rule in s['map']['action_rules']:
            if not set(rule['targets']) & set(path): continue
            if rule.get('legacy_scope') is not None:
                raise StateUnknown('Action Rule source or applicability unconfirmed')
            if not set(rule['refs']) <= set(bound): raise StateUnknown('Action Rule source not bound')
            if rule.get('applies_to') and op not in rule['applies_to']: continue
            if rule.get('time_role')=='review_trigger' and op in recording: continue
            if decisions.get(rule['id'])=='blocks': raise StateDenied('Action Rule blocks operation')
            if decisions.get(rule['id'])!='allows': raise StateUnknown('Action Rule effect unconfirmed')

    def _permission(self,s,key,op,actor,r,bound):
        self._context(s,key,op,r,bound)
        applicable=[p for p in s['map']['permissions'] if key in p['targets'] and p['grantee']==actor
                    and op in p['operations']]
        # Map-wide add is explicit and path/Map-bound; never ordinary inheritance.
        wide_grants = set()
        scope_constraints = set()
        if op=='add_work':
            project_scope = 'map_project_id:' + s['project_id']
            store_scope = 'map_store_path:' + str(self.store.path.resolve())
            scope_constraints = {project_scope, store_scope}
            for p in s['map']['permissions']:
                if p['grantee']==actor and 'add_work_in_map' in p['operations']:
                    map_constraints = [
                        c for c in p['constraints']
                        if c.startswith(('map_project_id:', 'map_store_path:'))
                    ]
                    if len(map_constraints) == 2 and set(map_constraints) == scope_constraints:
                        wide_grants.add(p['id'])
                        if p not in applicable:
                            applicable.append(p)
        if any(p['effect']!='allow' for p in applicable):raise StateDenied('operation not permitted')
        if op=='set_permission' and r.owner_permission:return
        if not applicable:raise StateDenied('operation not permitted')
        decisions=dict(r.permission_decisions);allowed=[]
        for p in applicable:
            if any(unverified_source(s, ref) for ref in p['source_refs']):continue
            if not set(p['source_refs']) <= set(bound):continue
            constraints = set(p['constraints'])
            remaining = constraints - scope_constraints if p['id'] in wide_grants else constraints
            if any(c.startswith('map_id:') for c in constraints):
                exact_store = {f'map_project_id:{s["project_id"]}', f'map_id:{s["map_id"]}',
                               'map_store_path:' + str(self.store.path.resolve())}
                if constraints != exact_store:
                    continue
                remaining = set()
            known = not remaining or p['constraints'] == ['explicit_initial_owner']
            if decisions.get(p['id'])=='blocks':continue
            if not known and decisions.get(p['id'])!='allows':continue
            allowed.append(p)
        if not allowed:raise StateUnknown('authority source/constraints not confirmed for this request')

    def apply(self,request,*,review=None,fault_at=None):
        request=deepcopy(request)
        fields={'format_version','project_id','map_id','base_revision','actor_id','operation','target','expected','value','sources','meaning'}
        if not isinstance(request,dict) or set(request)!=fields: raise StateInvalid('request fields invalid')
        committed=self.store.read();s=deepcopy(committed.snapshot);validate(s)
        if s['metadata']['store_role']!='canonical_current': raise StateDenied('candidate/retired Store is not current')
        if (request['format_version']!=FORMAT or request['project_id']!=s['project_id'] or request['map_id']!=s['map_id']
                or request['base_revision']!=s['revision']): raise StateConflict('reread current Map; old request not applied')
        if not nonempty(request['actor_id']): raise StateDenied('actor missing')
        readers={};bound=self._sources(request,readers)
        if review is None: review=review_request(s,request)
        if not isinstance(review,ReviewedStateRequest) or review.request_digest!=digest(request) or review.state_digest!=digest(s):
            raise StateConflict('review belongs to another request/current state')
        op=request['operation'];target=request['target'];actor=request['actor_id']
        self._permission(s,target,op,actor,review,bound)
        if op=='set_permission' and isinstance(request['value'],dict) and any(
                unverified_source(s, ref) for ref in request['value'].get('source_refs', [])):
            raise StateUnknown('new grants require a verified source version')
        if op in ('move_pin','set_pin') and s['metadata'].get('migration') is not None:
            from pma.workmap_state_trial_permission_pin import verify_shared_pin
            verify_shared_pin(s, request, review)
        carried=self._change(s,request,review,bound)
        meaning=request['meaning']
        if not isinstance(meaning,list): raise StateInvalid('meaning must be explicit records or empty')
        for e in meaning:
            if target not in e.get('works',[]) or not set(e.get('source_refs',[])) <= set(bound):
                raise StateDenied('meaning exceeds reviewed source/scope')
        s['thread_entries'].extend(deepcopy(meaning))
        s['revision']=uuid4().hex;s['recorded_at']=now();validate(s)
        prepared=self.store.prepare_state(committed.token,s)
        if carried:
            from pma.workmap_state_sources import materialize
            materialize(self.store,carried)
        if self.before_commit is not None:self.before_commit()
        self._sources(request,readers)  # same source locations, after encoding/carrying
        try: prepared.commit(fault_at=fault_at)
        except StoreConflict as exc: raise StateConflict('current Map changed; reread and judge again') from exc
        return dict(result='applied',revision=s['revision'])  # returned, not saved as a receipt

    def _require_meaning(self,q,kind):
        items=[e for e in q['meaning'] if e.get('kind') in kind and q['target'] in e.get('works',[])]
        if len(items)!=1: raise StateDenied('existing meaningful-change contract requires its reason')
        return items[0]

    def _change(self,s,q,r,bound):
        op=q['operation'];k=q['target'];a=q['actor_id'];v=q['value'];expected=q['expected'];m=s['map']
        if op=='append_meaning':
            if expected is not None or v is not None or not q['meaning']: raise StateInvalid('explicit meaning required')
        elif op=='update_work':
            if s['works'][k]!=expected: raise StateConflict('Work expected value changed')
            validate_work(v,k)
            if v['intent']!=expected['intent'] or v['origin']!=expected['origin']:
                if not r.same_purpose: raise StateDenied('actual same-purpose correction/refinement review required')
                e=self._require_meaning(q,{'correction','refinement'})
                if e.get('change') != {'before':expected,'after':v}: raise StateDenied('meaning omits actual identity change')
                if v['origin']!=expected['origin'] and not set(v['origin']['refs'])<=set(bound):raise StateDenied('new Origin sources not bound')
            s['works'][k]=deepcopy(v)
        elif op=='add_work':
            if expected is not None or not isinstance(v,dict) or set(v)!={'work','display_id'}: raise StateInvalid('new identity/address required')
            key=v['work']['work_key'];validate_work(v['work'],key)
            if key in s['display_ids'] or v['display_id'] in s['aliases']: raise StateConflict('identity/address already used')
            if v['work']['intent'] is None: raise StateUnknown('new Work Intent required')
            if not set(v['work']['origin']['refs'])<=set(bound):raise StateDenied('new Work sources not bound')
            s['works'][key]=deepcopy(v['work']);m['members'].append(key)
            s['display_ids'][key]=v['display_id'];s['aliases'][v['display_id']]=key
            m['relations'].append(dict(id='relation:'+uuid4().hex,kind='parent',child=key,parent=k))
            if isinstance(s['metadata']['completeness'],dict):
                for n in COLLECTIONS:
                    scope=s['metadata']['completeness'].get(n)
                    if scope!='full':
                        if not isinstance(scope,dict):scope=s['metadata']['completeness'][n]={'confirmed_work_keys':[]}
                        scope['confirmed_work_keys'].append(key)
        elif op in ('set_pin','remove_pin','set_permission','remove_permission','set_action_rule','remove_action_rule'):
            collection='pins' if op.endswith('pin') else ('permissions' if op.endswith('permission') else 'action_rules')
            setting = op.startswith('set_')
            item = v if setting else expected
            item_id = item['id']
            old=next((x for x in m[collection] if x['id']==item_id),None)
            if old!=expected: raise StateConflict('current record changed')
            if collection=='pins':
                if any(p['actor_id'] != a or p['work_id'] != k for p in (old, item) if p is not None):
                    raise StateDenied('current and proposed Pins must belong to the actor and target')
                retain = (setting and old is not None and old['mode']=='held' and item['mode']=='active'
                          and r.actual_acceptance and s['metadata'].get('migration') is not None
                          and not any(p['actor_id']==a and p['mode']=='active' for p in m['pins'])
                          and {k:v for k,v in old.items() if k not in ('mode','confirmed_at')}
                              == {k:v for k,v in item.items() if k not in ('mode','confirmed_at')})
                if not retain and not set(item['refs']) <= set(bound):
                    raise StateDenied('own target Pin with bound acceptance only')
                if setting and item['mode']=='active':self._exclusive_known(s,r)
            else:
                if (any(x['targets'] != [k] for x in (old, item) if x is not None)
                        or not set(item.get('source_refs',item.get('refs',[]))) <= set(bound)):
                    raise StateDenied('exact target/source required')
            if old is not None:m[collection].remove(old)
            if setting:m[collection].append(deepcopy(item))
        elif op=='move_pin':
            if not isinstance(v,dict) or set(v)!={'from','to'}: raise StateInvalid('Pin move invalid')
            old=v['from'];new=v['to'];previous=next((p for p in m['pins'] if p['id']==new['id']),None)
            if expected!={'from':old,'to':previous} or old not in m['pins'] or old['mode']!='active': raise StateConflict('Pin move premise changed')
            if old['actor_id']!=a or new['actor_id']!=a or new['work_id']!=k or new['mode']!='active': raise StateDenied('own Pin move only')
            self._permission(s,old['work_id'],'set_pin',a,r,bound)
            for member in m['members']:
                if collection_status(s,'pins',member)!='confirmed' and (member,'pins') not in r.unknown_scopes:
                    raise StateUnknown('active Pin exclusivity unknown')
            if previous is not None and (previous['actor_id']!=a or previous['work_id']!=k or previous['mode']!='held'
                    or any(previous[f]!=new[f] for f in ('id','pinned_at','refs'))):raise StateDenied('held identity changed')
            if previous is None and not set(new['refs']) <= set(bound):
                raise StateDenied('new destination Pin acceptance source not bound')
            next(p for p in m['pins'] if p['id']==old['id'])['mode']='held'
            if previous is not None:m['pins'].remove(previous)
            m['pins'].append(deepcopy(new))
        elif op in ('start_review','finish_review'):
            self._review_change(s,q,r,bound)
        elif op=='record_result':
            if expected is not None or not isinstance(v,dict) or v.get('works')!=[k] or not set(v.get('source_refs',[]))<=set(bound):
                raise StateDenied('result target/source invalid')
            s['evidence'].append(deepcopy(v))
        elif op=='close':
            if expected!=s['works'][k]: raise StateConflict('close identity changed')
            e=self._require_meaning(q,{'close'})
            if e.get('outcome')!=r.close_outcome or e.get('why')!=r.close_reason or not set(r.close_refs)<=set(e['source_refs']):
                raise StateDenied('reviewed close basis does not match outcome/meaning')
            parent=next((x['parent'] for x in m['relations'] if x.get('child')==k),None)
            if e.get('identity')!=expected or e.get('parent')!=parent: raise StateDenied('final identity/Parent missing')
            if not isinstance(v,dict) or set(v)!={'pins','permissions','action_rules'}:raise StateDenied('explicit close disposition required')
            for name in v:
                current=[x for x in m[name] if x.get('work_id')==k or k in x.get('targets',[])]
                if current!=v[name]:raise StateConflict('close disposition changed')
                for item in current:
                    if name=='pins' and item['actor_id']!=a:raise StateDenied('other participant still involved')
                    if name!='pins' and item['targets']!=[k]:raise StateDenied('shared authority/rule needs disposition')
                    self._permission(s,k,'remove_'+{'pins':'pin','permissions':'permission','action_rules':'action_rule'}[name],a,r,bound)
            if any(x.get('parent')==k or x.get('provider')==k or x.get('consumer')==k for x in m['relations']):
                raise StateDenied('current children/dependencies need disposition')
            for name in v:
                for item in v[name]:m[name].remove(item)
            m['members'].remove(k);del s['works'][k]
            m['relations']=[x for x in m['relations'] if x.get('child')!=k]
            s['retirements'][k]=e['id'];self._remove_completeness(s,{k})
        elif op=='reopen':
            if k not in s['retirements']:raise StateDenied('not a closed Work')
            e=next(e for e in s['thread_entries'] if e['id']==s['retirements'][k])
            if expected!=e['identity'] or v!={'parent':e.get('parent')}:raise StateConflict('reopen identity/Parent differs')
            self._require_meaning(q,{'reopen'})
            s['works'][k]=deepcopy(e['identity']);m['members'].append(k);del s['retirements'][k]
            if v['parent'] is not None:m['relations'].append(dict(id='relation:'+uuid4().hex,kind='parent',child=k,parent=v['parent']))
        elif op in ('set_relation','remove_relation'):
            item=v if op=='set_relation' else expected
            if not isinstance(item,dict):raise StateInvalid('relation required')
            old=next((x for x in m['relations'] if x['id']==item['id']),None)
            if old!=expected:raise StateConflict('relation changed')
            for relation in (old, item):
                if relation is None:
                    continue
                if relation['kind'] != 'dependency':
                    raise StateDenied('use explicit reparent contract')
                if relation['consumer'] != k:
                    raise StateDenied('current and proposed dependency must belong to the target')
            if old is not None:m['relations'].remove(old)
            if op=='set_relation':m['relations'].append(deepcopy(v))
        elif op=='reparent':
            old_parent=next((x['parent'] for x in m['relations'] if x.get('child')==k),None)
            if expected!=old_parent or not isinstance(v,dict) or set(v)!={'parent','display_id'}:raise StateConflict('Parent changed')
            self._permission(s,v['parent'],'reparent',a,r,bound)
            keys=subtree(s,k)
            if v['parent'] in keys:raise StateDenied('Parent cycle')
            for child in keys:self._permission(s,child,'reparent',a,r,bound)
            old_addresses={w:s['display_ids'][w] for w in keys};new_addresses={k:v['display_id']}
            old_prefix=address_prefix(s['display_ids'][k]);new_prefix=address_prefix(v['display_id'])
            for w in keys[1:]:new_addresses[w]=new_prefix+s['display_ids'][w][len(old_prefix):]
            if any(address in s['aliases'] and s['aliases'][address]!=w for w,address in new_addresses.items()):raise StateConflict('address reused')
            e=self._require_meaning(q,{'reparent'})
            if e.get('change')!={'old_parent':old_parent,'new_parent':v['parent'],'old_addresses':old_addresses,'new_addresses':new_addresses}:
                raise StateDenied('reparent meaning incomplete')
            m['relations']=[x for x in m['relations'] if x.get('child')!=k]
            m['relations'].append(dict(id='relation:'+uuid4().hex,kind='parent',child=k,parent=v['parent']))
            s['display_ids'].update(new_addresses)
            s['aliases'].update({address:w for w,address in new_addresses.items()})
        elif op in ('release_scope','accept_scope'):
            from pma.workmap_state_transfer import change_scope
            return change_scope(self,s,q,r,bound)
        else: raise StateDenied('unsupported Work Map operation')

    @staticmethod
    def _remove_completeness(s,keys):
        c=s['metadata']['completeness']
        if isinstance(c,dict):
            for scope in c.values():
                if isinstance(scope,dict):scope['confirmed_work_keys']=[k for k in scope['confirmed_work_keys'] if k not in keys]
        s['metadata']['unconfirmed_intents']=[k for k in s['metadata']['unconfirmed_intents'] if k not in keys]

    def _review_change(self,s,q,r,bound):
        v=q['value'];k=q['target'];a=q['actor_id'];m=s['map']
        if not isinstance(v,dict) or set(v)!={'pin','permission','scope','result'}: raise StateInvalid('review scope invalid')
        p=v['pin'];g=v['permission'];scope=v['scope']
        if (p['work_id']!=k or g['targets']!=[k] or g['grantee']!=p['actor_id'] or g['operations']!=['review']
                or g['effect']!='allow' or 'scope_sha256:'+digest(scope) not in g['constraints']
                or 'coordinator:'+a not in g['constraints']
                or not set(p['refs']+g['source_refs'])<=set(bound)):
            raise StateDenied('review actor/scope/grant differs')
        if not isinstance(scope, dict) or set(scope) != {'file', 'sha256'} or not nonempty(scope['file']):
            raise StateInvalid('reviewed file and version required')
        if scope['file'] not in bound:
            raise StateUnknown('reviewed file is not bound to verified source bytes')
        if bound[scope['file']] != scope['sha256']:
            raise StateConflict('reviewed file version differs')
        if not any(x['actor_id']==a and x['work_id']==k and x['mode']=='active' for x in m['pins']):raise StateDenied('coordinator active Pin required')
        if q['operation']=='start_review':
            if not r.actual_acceptance or q['expected'] is not None or v['result'] is not None:raise StateDenied('actual scoped acceptance required')
            self._exclusive_known(s,r)
            if any(x['actor_id']==p['actor_id'] for x in m['pins']) or any(x['grantee']==p['actor_id'] for x in m['permissions']):raise StateConflict('reviewer already involved')
            m['pins'].append(deepcopy(p));m['permissions'].append(deepcopy(g))
        else:
            if not r.actual_result or q['expected']!={'pin':p,'permission':g} or p not in m['pins'] or g not in m['permissions']:
                raise StateConflict('current review ended/changed or result not reviewed')
            result=v['result']
            if not isinstance(result,dict) or result.get('works')!=[k] or not set(result.get('source_refs',[]))<=set(bound):raise StateDenied('bound actual result required')
            m['pins'].remove(p);m['permissions'].remove(g);s['evidence'].append(deepcopy(result))

    @staticmethod
    def _exclusive_known(s,r):
        for k in s['map']['members']:
            if collection_status(s,'pins',k)!='confirmed' and (k,'pins') not in r.unknown_scopes:
                raise StateUnknown('active Pin exclusivity unknown')


def resume_current(store, request):
    """Read now; never manufacture a past success/absence from present values."""
    s=store.read().snapshot;validate(s)
    if request['project_id']!=s['project_id'] or request['map_id']!=s['map_id']:raise StateConflict('different Map')
    return dict(past_result='unknown',observed_revision=s['revision'],
        request_base_is_current=request['base_revision']==s['revision'],
        current=read_work(s,request['target'],actor=request['actor_id']))
