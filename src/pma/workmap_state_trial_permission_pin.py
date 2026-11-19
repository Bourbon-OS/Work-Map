"""Shared-location and own-Pin checks for a converted Map."""
from uuid import uuid4

from pma.workmap_state_contract import (
    StateConflict, StateDenied, StateUnknown, chain, collection_status, digest, now,
)
from pma.workmap_state_core import prepare, review_request
from pma.workmap_state_entry import PreparedStateChange


PERMIT_OPTIONS = {
    'edit': ('update_work',),
    'record': ('append_meaning', 'record_result'),
    'close': ('close', 'remove_pin', 'append_meaning'),
    'pin': ('set_pin', 'move_pin', 'remove_pin'),
    'map_add': ('add_work_in_map',),
}
LEGACY_RESTRICTIONS = {
    'set_pin': ('add_pin', 'update_pin', 'append_thread'),
    'move_pin': ('add_pin', 'update_pin', 'append_thread'),
    'append_meaning': ('append_thread', 'append_scoped_thread'),
    'record_result': ('append_thread', 'append_scoped_thread'),
}


def legacy_denial(state, key, actor, operations):
    """An imported denial still applies; its allow counterpart is never inferred."""
    restrictions = set(operations)
    for op in operations:
        restrictions.update(LEGACY_RESTRICTIONS.get(op, ()))
    scope = set(chain(state, key))
    if any(p['grantee'] == actor and p['effect'] != 'allow' and scope.intersection(p['targets'])
           and restrictions.intersection(p['operations']) for p in state['map']['permissions']):
        raise StateDenied('対象または親に拒否・未確認の制約があります。')


def shared_scope(state, actor, target, op):
    """Use the old Pin selector's endpoint and same-location rules on current state."""
    endpoints = {target}
    if op == 'move_pin':
        endpoints.update(p['work_id'] for p in state['map']['pins']
                         if p['actor_id'] == actor and p['mode'] == 'active')
    where = {p['where'] for p in state['map']['pins'] if p.get('where') and p['work_id'] in endpoints}
    peers = [p for p in state['map']['pins'] if p.get('where') in where]
    if any(collection_status(state, 'pins', peer['work_id']) != 'confirmed' for peer in peers):
        raise StateUnknown('共有作業場所の現在のPinを確認できません。')
    return peers


def verify_shared_pin(state, request, review):
    """Existing location and assignment are immutable during an own-Pin move."""
    operation, actor, target = (request[k] for k in ('operation', 'actor_id', 'target'))
    proposed = request['value']
    moved = proposed.get('to') if operation == 'move_pin' and isinstance(proposed, dict) else proposed
    if not isinstance(moved, dict):
        raise StateDenied('対象Pinを確認できません。')
    found = [p for p in state['map']['pins'] if p['actor_id'] == actor and p['work_id'] == target]
    if len(found) > 1:
        raise StateConflict('切替先に本人のPinが複数あり、一件に確定できません。')
    held = found[0] if found else None
    if moved.get('where') != (held or {}).get('where'):
        raise StateDenied('Pin切替で共有作業場所を変更できません。')
    if shared_scope(state, actor, target, operation) and not review.actual_acceptance:
        raise StateUnknown('共有作業場所の記録を照合してから切り替えてください。')


def prepare_pin(args, root, db, state, key, *, source_bindings, require_owner_instruction=True):
    if not args.reason.strip() or (require_owner_instruction and not args.owner_instruction.strip()):
        raise StateDenied('引受の理由と確認した指示を指定してください。')
    own = [p for p in state['map']['pins'] if p['actor_id'] == args.actor]
    active = next((p for p in own if p['mode'] == 'active'), None)
    found = [p for p in own if p['work_id'] == key]
    if len(found) > 1:
        raise StateConflict('切替先に本人のPinが複数あり、一件に確定できません。')
    held = found[0] if found else None
    if held is not None and held['mode'] != 'held':
        raise StateDenied('このWorkは既に担当しています。')
    operation = 'move_pin' if active is not None else 'set_pin'
    required = [(key, operation)] if active is None else [(active['work_id'], 'set_pin'), (key, operation)]
    bindings = source_bindings(args, root, db, state, required)
    observed = now()
    new = (dict(held, mode='active', confirmed_at=observed) if held is not None else
           dict(id='pin:' + uuid4().hex, work_id=key, actor_id=args.actor, mode='active',
                pinned_at=observed, confirmed_at=observed, refs=[b['ref'] for b in bindings]))
    expected, value = held, new
    if active is not None:
        expected, value = {'from': active, 'to': held}, {'from': active, 'to': new}
    request = prepare(state, args.actor, operation, key, expected=expected, value=value, sources=bindings)
    shared_scope(state, args.actor, key, operation)
    review = review_request(state, request, actual_acceptance=True)
    return PreparedStateChange(db, root, request, digest(request), review)

