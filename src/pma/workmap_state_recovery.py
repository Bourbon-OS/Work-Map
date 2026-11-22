"""Explicit candidate verification and fresh-revision activation; no old DB reads."""
from copy import deepcopy
import os
from uuid import uuid4
from pma.workmap_state_contract import validate, digest, now, StateConflict, StateDenied
from pma.workmap_state_store import StateStore
from pma.workmap_state_sources import capture, folder, local_file, materialize, portable_refs, resolver_for


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
    if any(os.path.lexists(local_file(candidate.path.with_name(candidate.path.name + suffix)))
           for suffix in ('', '-journal', '-wal', '-shm')):
        raise StateConflict('既存のDBとSQLite付随ファイルを保全してください。')
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


def restore_reviewed(document, candidate_path, target_path, *, expected_token, project_id, owner_instruction):
    """Restore a checked backup at its original, vacant location without replacing a DB."""
    from pma.workmap_state_contract import permission_authority_status
    from pma.workmap_state_read import trial_local_path
    from pma.workmap_state_trial_integrated import store_scope

    target = trial_local_path(target_path)
    candidate = StateStore(trial_local_path(candidate_path.resolve()))
    if not owner_instruction.strip():
        raise StateDenied('復旧を判断した実際のOwner指示を指定してください。')
    if target.parent.resolve() != target.parent or target.resolve() != target:
        raise StateDenied('元のMapの保存先を確認してください。')
    target_store = StateStore(target)
    if folder(target_store).resolve() != folder(target_store):
        raise StateDenied('元のMapの根拠フォルダの保存先を確認してください。')
    occupied = [target, *(target.with_name(target.name + suffix) for suffix in ('-journal', '-wal', '-shm'))]
    if any(os.path.lexists(local_file(path)) for path in occupied):
        raise StateDenied('現用DBとSQLite付随ファイルを保全してから、空いた元の保存先へ復旧してください。')

    original = document.get('state')
    if not isinstance(original, dict) or original.get('project_id') != project_id:
        raise StateDenied('確認したMapのproject_idがバックアップと一致しません。')
    validate(original)
    scope = list(store_scope(original, target))
    if not any(entry['constraints'] == scope and entry['effect'] == 'allow'
               and permission_authority_status(original, entry) == 'versioned'
               for entry in original['map']['permissions']):
        raise StateDenied('バックアップから元の保存先を確認できません。保守担当に相談してください。')
    reviewed = candidate.read(read_only=True)
    expected = deepcopy(original)
    expected['metadata']['store_role'] = 'recovery_candidate'
    if reviewed.token != expected_token or reviewed.snapshot != expected:
        raise StateConflict('確認した候補の版または内容がバックアップと一致しません。')

    def missing(_ref):
        raise OSError('候補の根拠が見つかりません。')

    if capture(reviewed.snapshot, resolver_for(candidate, missing)) != document['source_files']:
        raise StateConflict('確認した候補とバックアップで根拠が異なります。')
    target.parent.mkdir(parents=True, exist_ok=True)
    restored = restore_candidate(document, target)
    opened = restored.read(read_only=True)
    activated = activate_candidate(restored, expected_token=opened.token, expected_content=digest(expected),
                                   reviewed_current_scope=True)
    verified = restored.read(read_only=True)
    if verified.snapshot != activated:
        raise StateConflict('復旧後の版を確認できません。Mapを読み直してください。')
    return verified


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
