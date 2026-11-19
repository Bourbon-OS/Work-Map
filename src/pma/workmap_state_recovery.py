"""Explicit candidate verification and fresh-revision activation; no old DB reads."""
from copy import deepcopy
from uuid import uuid4
from pma.workmap_state_contract import validate, digest, now, StateConflict, StateDenied
from pma.workmap_state_store import StateStore
from pma.workmap_state_sources import capture, materialize, portable_refs


def backup(store,resolver):
    state=store.read(read_only=True).snapshot
    return dict(format='workmap-state-backup-1',state=state,state_sha256=digest(state),source_files=capture(state,resolver))


def restore_candidate(document,target):
    if set(document)!={'format','state','state_sha256','source_files'} or document['format']!='workmap-state-backup-1':raise StateDenied('unsupported backup')
    state=deepcopy(document['state']);validate(state)
    if digest(state)!=document['state_sha256']:raise StateConflict('backup changed')
    if set(document['source_files'])!=set(portable_refs(state)):raise StateDenied('backup business source coverage differs')
    if state['metadata']['store_role']!='canonical_current':raise StateDenied('backup is not a current source')
    state['metadata']['store_role']='recovery_candidate'
    candidate=StateStore(target)
    if candidate.path.exists():raise StateConflict('preserve existing restore target')
    materialize(candidate,document['source_files']);candidate.initialize(state)
    return candidate


def activate_candidate(candidate,*,expected_token,expected_content,reviewed_current_scope=False):
    """An isolated recovery candidate stays read-only to the Core until reviewed.

    A new revision does not prove post-backup authority/ownership was recovered.
    """
    old=candidate.read();state=old.snapshot
    if old.token!=expected_token or digest(state)!=expected_content:raise StateConflict('candidate changed')
    if state['metadata']['store_role']!='recovery_candidate' or not reviewed_current_scope:raise StateDenied('current scope/ownership review required')
    current=deepcopy(state);current['metadata']['store_role']='canonical_current'
    current['revision']=uuid4().hex;current['recorded_at']=now()
    candidate.commit_validated(old.token,current)
    return current


def prepare_cutover(source,target,*,expected_source_token,expected_candidate_token):
    old=source.read();candidate=target.read()
    if old.token!=expected_source_token or candidate.token!=expected_candidate_token:raise StateConflict('cutover input changed')
    a=deepcopy(old.snapshot);b=deepcopy(candidate.snapshot)
    if a['metadata']['store_role']!='canonical_current' or b['metadata']['store_role']!='recovery_candidate':raise StateDenied('cutover roles differ')
    b['metadata']['store_role']='canonical_current'
    if a!=b:raise StateConflict('candidate business state differs')
    return dict(source_token=old.token,target_token=candidate.token,source_digest=digest(a),candidate_digest=digest(candidate.snapshot))


def execute_cutover(source,target,plan,*,fault_at=None):
    """Freeze source first. Interrupted target activation leaves zero, never two writers."""
    if fault_at not in (None,'after_freeze'):raise ValueError('unknown fault')
    expected=prepare_cutover(source,target,expected_source_token=plan['source_token'],expected_candidate_token=plan['target_token'])
    if expected!=plan:raise StateConflict('cutover plan changed')
    old=source.read();other=target.read()
    if (old.token!=plan['source_token'] or other.token!=plan['target_token']
            or digest(old.snapshot)!=plan['source_digest'] or digest(other.snapshot)!=plan['candidate_digest']):
        raise StateConflict('cutover inputs changed')
    frozen=deepcopy(old.snapshot);frozen['metadata']['store_role']='cutover_source';frozen['revision']=uuid4().hex;frozen['recorded_at']=now()
    source.commit_validated(old.token,frozen)
    if fault_at=='after_freeze':raise RuntimeError('injected after source frozen')
    return activate_candidate(target,expected_token=other.token,expected_content=digest(other.snapshot),reviewed_current_scope=True)
