"""Cut over two explicit local SQLite Stores from a reviewed legacy conversion."""
from copy import deepcopy
import hashlib
from uuid import uuid4

from pma.workmap_contract import validate_snapshot
from pma.workmap_state_contract import digest, is_version_ref, now, validate
from pma.workmap_state_entry import resolve_trial_source
from pma.workmap_state_sources import capture, resolver_for
from pma.workmap_store import StoreConflict, snapshot_object_id


def change_role(snapshot, role, revision, at):
    value = deepcopy(snapshot)
    value.update(revision=revision, recorded_at=at)
    value['metadata']['store_role'] = role
    return value


def confirm_owner_source(target, ref):
    if not is_version_ref(ref):
        raise StoreConflict('reviewed Owner source version is required')
    content = resolve_trial_source(target.path.parents[1], ref)
    if not ref.endswith('#sha256=' + hashlib.sha256(content).hexdigest()):
        raise StoreConflict('reviewed Owner source changed')


def confirm_carried_sources(target, candidate):
    def no_fallback(_ref):
        raise OSError('candidate is missing a carried source')

    capture(candidate, resolver_for(target, no_fallback))


def prepare(source, target, *, source_token, candidate_content, owner_source):
    """Bind the exact reviewed conversion and its source, without copying their contents."""
    old, new = source.read(read_only=True), target.read(read_only=True)
    legacy, candidate = validate_snapshot(old.snapshot), validate(new.snapshot)
    if (source.path.resolve() == target.path.resolve() or legacy['format_version'] != '0.4.2'
            or legacy['metadata'].get('store_role') != 'canonical_current'
            or candidate['metadata']['store_role'] != 'recovery_candidate'
            or legacy['project_id'] != candidate['project_id']
            or candidate['metadata'].get('migration', {}).get('legacy_revision') != legacy['revision']
            or old.token != source_token or digest(candidate) != candidate_content or not owner_source):
        raise StoreConflict('reviewed old state and conversion candidate do not match')
    confirm_owner_source(target, owner_source)
    confirm_carried_sources(target, candidate)
    plan = dict(source_path=str(source.path.resolve()), target_path=str(target.path.resolve()),
                old=old.token, candidate=new.token, at=now(), owner_source=owner_source)
    for phase, snapshot, role in (
        ('frozen', legacy, 'cutover_source'), ('active', candidate, 'canonical_current'),
        ('revoked', candidate, 'recovery_candidate'), ('restored', legacy, 'canonical_current'),
    ):
        revision = uuid4().hex
        plan[phase] = dict(revision=revision, token=snapshot_object_id(change_role(snapshot, role, revision, plan['at'])))
    return plan


def verify_plan(source, target, plan, reviewed_digest):
    if (digest(plan) != reviewed_digest or plan['source_path'] != str(source.path.resolve())
            or plan['target_path'] != str(target.path.resolve())):
        raise StoreConflict('different plan or Store location')
    confirm_owner_source(target, plan['owner_source'])


def commit_role(store, current, plan, phase, role):
    target = plan[phase]
    shaped = change_role(current.snapshot, role, target['revision'], plan['at'])
    if snapshot_object_id(shaped) != target['token']:
        raise StoreConflict('current content differs from reviewed conversion')
    return store.commit_validated(current.token, shaped)


def execute(source, target, plan, *, reviewed_digest, fault_at=None):
    if fault_at not in (None, 'after_freeze', 'after_activate'):
        raise ValueError('unsupported interruption point')
    verify_plan(source, target, plan, reviewed_digest)
    old, candidate = source.read(read_only=True), target.read(read_only=True)
    if old.token == plan['frozen']['token'] and candidate.token == plan['active']['token']:
        confirm_carried_sources(target, candidate.snapshot)
        return 'verified'
    if old.token not in (plan['old'], plan['frozen']['token']) or candidate.token != plan['candidate']:
        raise StoreConflict('cutover has changed; re-read both sides')
    confirm_carried_sources(target, candidate.snapshot)
    if old.token == plan['old']:
        commit_role(source, old, plan, 'frozen', 'cutover_source')
    if source.read(read_only=True).token != plan['frozen']['token']:
        raise StoreConflict('old Store has not been confirmed stopped')
    if fault_at == 'after_freeze':
        raise InterruptedError('interrupted after stopping old Store')
    confirm_owner_source(target, plan['owner_source'])
    confirm_carried_sources(target, candidate.snapshot)
    commit_role(target, candidate, plan, 'active', 'canonical_current')
    if fault_at == 'after_activate':
        raise InterruptedError('interrupted after activating new Store')
    if (source.read(read_only=True).token, target.read(read_only=True).token) != (
            plan['frozen']['token'], plan['active']['token']):
        raise StoreConflict('cutover result has changed')
    return 'verified'


def rollback(source, target, plan, *, reviewed_digest, fault_at=None):
    if fault_at not in (None, 'after_revoke'):
        raise ValueError('unsupported rollback interruption point')
    verify_plan(source, target, plan, reviewed_digest)
    old, new = source.read(read_only=True), target.read(read_only=True)
    if (old.token, new.token) == (plan['restored']['token'], plan['revoked']['token']):
        return 'reverted'
    allowed = (plan['candidate'], plan['active']['token'], plan['revoked']['token'])
    if old.token not in (plan['old'], plan['frozen']['token']) or new.token not in allowed:
        raise StoreConflict('rollback would discard a newer or unrecognized update')

    # Invalidate even a never-activated candidate: a prepared activation may
    # already have been handed the original token by a different process.
    if new.token != plan['revoked']['token']:
        commit_role(target, new, plan, 'revoked', 'recovery_candidate')
    if fault_at == 'after_revoke':
        raise InterruptedError('interrupted after disabling new Store')
    if target.read(read_only=True).token != plan['revoked']['token']:
        raise StoreConflict('new Store has not been confirmed stopped')
    current = source.read(read_only=True)
    if current.token == plan['restored']['token']:
        return 'reverted'
    if current.token not in (plan['old'], plan['frozen']['token']):
        raise StoreConflict('old Store changed; leave its new state in place')
    commit_role(source, current, plan, 'restored', 'canonical_current')
    if (source.read(read_only=True).token, target.read(read_only=True).token) != (
            plan['restored']['token'], plan['revoked']['token']):
        raise StoreConflict('rollback result has changed')
    return 'reverted'
