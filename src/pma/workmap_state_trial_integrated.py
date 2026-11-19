"""Confirmed authority, own-Pin changes, and Work records for current State Maps."""
from dataclasses import dataclass, replace
import hashlib
from uuid import uuid4

from pma.workmap_state_contract import (
    StateConflict, StateDenied, StateUnknown, chain, digest, is_version_ref, new_state, now, parents,
    unverified_source, version_ref,
)
from pma.workmap_state_core import prepare, review_request
from pma.workmap_state_entry import PreparedStateChange, save_trial_change, trial_core
from pma.workmap_state_read import read_trial_state, trial_local_path
from pma.workmap_state_sources import local_file
from pma.workmap_state_store import StateStore
from pma.workmap_state_trial_permission_pin import PERMIT_OPTIONS, legacy_denial, prepare_pin


COMMANDS = ('permit', 'pin', 'reason', 'result', 'close')
INITIAL_OPERATIONS = ['add_work', 'set_permission']
INITIAL_SOURCE = 'repo:docs/work-management/evidence/owner_personal_init_'


def store_scope(state, db):
    return ('map_project_id:' + state['project_id'], 'map_store_path:' + str(db.resolve()), 'map_id:' + state['map_id'])


def capture_personal_instruction(root, instruction, *, initial=False):
    instruction = instruction.strip()
    if not instruction:
        raise StateDenied('実際のOwner指示を入力してください。')
    raw = (instruction + '\n').encode('utf-8')
    folder = root / 'docs/work-management/evidence'
    trial_local_path(folder.resolve())
    if not folder.resolve().is_relative_to(root / 'docs') or (root / 'docs').resolve() != root / 'docs':
        raise StateDenied('根拠の保存先はこのMapのdocs内を指定してください。')
    path = folder / ('owner_personal_' + ('init_' if initial else '') + hashlib.sha256(raw).hexdigest() + '.txt')
    target = local_file(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open('xb') as stream:
            stream.write(raw)
    except FileExistsError:
        if path.resolve() != path or target.read_bytes() != raw:
            raise StateConflict('同じ名前の根拠資料の内容または保存先が違います。')
    return version_ref('repo:' + path.relative_to(root).as_posix(), raw)


def initialize_personal_map(args, root, db):
    trial_local_path(db.resolve())
    if db.exists():
        raise StateDenied('初期化済みの保存先です。showで現在のMapを確認してください。')
    if not db.parent.resolve().is_relative_to(root):
        raise StateDenied('DBの保存先はこのMapのdata内を指定してください。')
    actor, name, intent, origin = (getattr(args, field).strip() for field in ('actor', 'name', 'intent', 'origin'))
    if not all((actor, name, intent, origin)):
        raise StateDenied('名前・目的・きっかけ・登録者を指定してください。')
    source = capture_personal_instruction(root, args.owner_instruction, initial=True)
    key = 'work:' + uuid4().hex
    work = dict(work_key=key, name=name, intent=intent, origin=dict(summary=origin, refs=[source]))
    state = new_state('project:' + str(uuid4()), 'map:' + str(uuid4()), work, actor, source)
    grant = state['map']['permissions'][0]
    grant.update(operations=INITIAL_OPERATIONS, constraints=list(store_scope(state, db)))
    StateStore(db).initialize(state)
    saved = read_trial_state(db, project_id=state['project_id'], map_id=state['map_id']).snapshot
    return dict(status='confirmed', work_key=key, display_id='W0', revision=saved['revision'])


def personal_owner_grant(state):
    imported = set(state['metadata'].get('migration', {}).get('legacy_permissions', []))
    root = state['aliases'].get('W0')
    if not root or root not in state['map']['members'] or root in parents(state):
        return None
    grants = [p for p in state['map']['permissions'] if p['id'] not in imported and p['targets'] == [root]
              and p['effect'] == 'allow' and p['grantor_ref'] == 'principal:owner'
              and p['operations'] == INITIAL_OPERATIONS and len(p['source_refs']) == 1
              and p['source_refs'][0].startswith(INITIAL_SOURCE) and is_version_ref(p['source_refs'][0])]
    return grants[0] if len(grants) == 1 else None


@dataclass(frozen=True)
class ReviewedOwner:
    """In-memory handoff after actual Owner attribution, like the existing reviewed caller.

    This is not an authentication or self-approval interface.
    """
    store: tuple[str, ...]
    actor: str
    instruction: str
    source: str
    intents: tuple
    permits: tuple
    closes: tuple


def source_binding(core, ref):
    try:
        raw = core.resolver(ref)
    except (OSError, KeyError) as exc:
        raise StateUnknown('根拠資料を確認できません。') from exc
    sha = hashlib.sha256(raw).hexdigest()
    if is_version_ref(ref) and not ref.endswith('#sha256=' + sha):
        raise StateConflict('確認した根拠の版が変わっています。')
    return dict(ref=ref if is_version_ref(ref) else ref + '#sha256=' + sha, sha256=sha)


def confirmed_owner(state, root, db, *, actor, instruction, source, permits, closes):
    """Bind already-reviewed literal instruction and decided scope to this candidate."""
    if (not source.startswith('repo:docs/work-management/evidence/owner_') or not source.endswith('.txt')
            or not instruction.strip() or not actor.strip()):
        raise StateDenied('確認済みのOwner指示と対象を指定してください。')
    core = trial_core(db, root)
    literal = core.resolver(source)
    if instruction not in literal.decode('utf-8').splitlines():
        raise StateDenied('実際のOwner回答と確認内容が一致しません。')
    intents = tuple((key, state['works'][key]['intent']) for key in sorted(set(permits) | set(closes)))
    if any(not intent for _, intent in intents):
        raise StateUnknown('Ownerが判断したWorkの目的を確認できません。')
    return ReviewedOwner(store_scope(state, db), actor, instruction, version_ref(source, literal),
                         intents, tuple((k, tuple(sorted(v))) for k,v in permits.items()), tuple(closes.items()))


def require_owner(confirmation, core, state, key, args):
    if (not isinstance(confirmation, ReviewedOwner)
            or confirmation.store != store_scope(state, core.store.path)
            or confirmation.actor != args.actor or confirmation.instruction != args.owner_instruction.strip()
            or dict(confirmation.intents).get(key) != state['works'][key]['intent']):
        raise StateDenied('このMap・担当・Work・目的に対する実際のOwner確認を受け取ってください。')
    return source_binding(core, confirmation.source)


def permit(args, core, state, key, confirmation, *, seed=None):
    flags = [flag for flag in PERMIT_OPTIONS if getattr(args, flag)]
    if not flags or 'map_add' in flags and (len(flags) > 1 or len(chain(state, key)) != 1) or args.source:
        raise StateDenied('通常の許可対象と操作を指定してください。')
    operations = {op for flag in flags for op in PERMIT_OPTIONS[flag]}
    scope_constraints = store_scope(state, core.store.path)
    bindings = []
    if seed is not None:
        # The local setup grants subsequent explicit approvals only to the actor
        # who initialized this Map at this location. It is not user authentication.
        if seed['grantee'] != args.actor or seed['constraints'] != list(scope_constraints):
            raise StateDenied('この保存先でMapを初期設定した編集者を確認してください。')
        legacy_denial(state, key, args.actor, ('set_permission',))
        bindings.append(source_binding(core, seed['source_refs'][0]))
        if args.close:
            operations.add('remove_permission')
    else:
        binding = require_owner(confirmation, core, state, key, args)
        allowed = set(dict(confirmation.permits).get(key, ()))
        if args.close and 'remove_permission' in allowed:
            operations.add('remove_permission')
        if allowed != operations:
            raise StateDenied('Ownerが確認した対象・操作と一致しません。')
    legacy_denial(state, key, args.actor, operations)
    if seed is not None:
        ref = capture_personal_instruction(core.store.path.parents[1], args.owner_instruction)
        binding = source_binding(core, ref)
    scope = dict(targets=[key], grantee=args.actor, effect='allow', grantor_ref='principal:owner',
                 operations=sorted(operations), constraints=list(scope_constraints), source_refs=[binding['ref']])
    grant = dict(id='permission:' + digest(scope), **scope)
    if grant in state['map']['permissions']:
        return dict(status='confirmed', work_key=key, permission_id=grant['id'], revision=state['revision'])
    request = prepare(state, args.actor, 'set_permission', key, value=grant, sources=[*bindings, binding])
    result = handle(core, request, review_request(state, request, owner_permission=True))
    if result['status'] == 'confirmed':
        result['permission_id'] = grant['id']
    return result


def operation_sources(args, root, db, state, required):
    core = trial_core(db, root)
    found = {}
    for key, operation in required:
        legacy_denial(state, key, args.actor, (operation,))
        failed = None
        for grant in state['map']['permissions']:
            if (grant['grantee'] != args.actor or grant['targets'] != [key] or grant['effect'] != 'allow'
                    or operation not in grant['operations'] or grant['constraints'] != list(store_scope(state, db))
                    or any(unverified_source(state, ref) for ref in grant['source_refs'])):
                continue
            try:
                refs = [found.get(ref) or source_binding(core, ref) for ref in grant['source_refs']]
            except (StateConflict, StateUnknown) as exc:
                failed = exc
                continue
            found.update({b['ref']: b for b in refs})
            break
        else:
            if failed:
                raise failed
            raise StateUnknown('この操作に必要な許可と根拠を確認できません。')
    return list(found.values())


def handle(core, request, review):
    return save_trial_change(PreparedStateChange(core.store.path, core.store.path.parents[1],
                                                request, digest(request), review))


def append_or_close(args, core, state, key, confirmation, *, existing_close_authority=False, personal=False):
    if [p['work_id'] for p in state['map']['pins'] if p['actor_id'] == args.actor and p['mode'] == 'active'] != [key]:
        raise StateDenied('対象Workの本人Pinを引き受けてから記録してください。')
    operation = {'reason': 'append_meaning', 'result': 'record_result', 'close': 'close'}[args.command]
    required = [(key, operation)]
    expected = value = None
    review, meaning = {}, []
    proof = []
    if operation == 'close':
        if personal and (args.outcome == 'replaced' or args.outcome == 'completed' and not args.evidence):
            raise StateDenied('この入口では中止、または達成根拠を指定した完了を記録できます。')
        if not personal and not existing_close_authority:
            proof.append(require_owner(confirmation, core, state, key, args))
            if dict(confirmation.closes).get(key) != args.outcome:
                raise StateDenied('Ownerが確認した終了種別と一致しません。')
        value = {name: [r for r in state['map'][name] if r.get('work_id') == key or key in r.get('targets', [])]
                 for name in ('pins', 'permissions', 'action_rules')}
        if (value['pins'] and not args.release_own_pin) or args.successor or value['action_rules']:
            raise StateDenied('本人Pinの解除と、許可された終了条件を指定してください。')
        required.extend((key, op) for name, op in (('pins','remove_pin'), ('permissions','remove_permission')) if value[name])
        expected = state['works'][key]
        source_refs = args.evidence
    else:
        source_refs = args.source
    selected = [source_binding(core, ref) for ref in source_refs]
    refs = list(dict.fromkeys(r['ref'] for r in selected))
    bindings = operation_sources(args, core.store.path.parents[1], core.store.path, state, required)
    if personal and operation == 'close':
        ref = capture_personal_instruction(core.store.path.parents[1], args.owner_instruction)
        proof.append(source_binding(core, ref))
    sources = list({b['ref']: b for b in [*bindings, *selected, *proof]}.values())
    if operation == 'append_meaning':
        meaning = [dict(id='meaning:' + uuid4().hex, kind='rationale', works=[key],
                        summary='判断理由', why=args.text.strip(), source_refs=refs or [bindings[0]['ref']],
                        actor=args.actor, recorded_at=now())]
    elif operation == 'record_result':
        value = dict(id='result:' + uuid4().hex, works=[key], summary=args.text.strip(), source_refs=refs)
        review['actual_result'] = True
    else:
        decision_refs = list(dict.fromkeys([*(item['ref'] for item in proof), *refs]))
        meaning = [dict(id='meaning:' + uuid4().hex, kind='close', works=[key],
                        summary='Workを終了', why=args.reason.strip(), source_refs=decision_refs,
                        actor=args.actor, recorded_at=now(), identity=expected, parent=parents(state).get(key),
                        outcome=args.outcome, residuals=args.residual)]
        review['close_basis'] = dict(outcome=args.outcome, intent=expected['intent'],
                                     reason=args.reason.strip(), refs=decision_refs)
    request = prepare(state, args.actor, operation, key, expected=expected, value=value, sources=sources, meaning=meaning)
    return handle(core, request, review_request(state, request, **review))


def apply_command(args, root, db, state, confirmation, *, normal=False):
    seed = personal_owner_grant(state) if normal else None
    if seed is None and state['metadata'].get('migration') is None:
        raise StateDenied('この入口は初期設定した新形式のMapまたは変換済みのMapで使えます。')
    key = state['aliases'].get(args.work, args.work)
    if key not in state['map']['members']:
        raise StateDenied('現在のMapのWorkを指定してください。')
    core = trial_core(db, root)
    if args.command == 'pin':
        change = prepare_pin(args, root, db, state, key, source_bindings=operation_sources)
        if (seed is not None and state['metadata'].get('migration') is None
                and change.request['operation'] == 'set_pin' and change.request['expected'] is not None):
            # Reuse the held Pin's source as well as the current grant when resuming it.
            request = change.request
            bound = {b['ref']: b for b in change.bindings}
            for ref in request['expected']['refs']:
                if ref not in bound:
                    bound[ref] = source_binding(core, ref)
            request = dict(request, sources=list(bound.values()))
            change = replace(change, request=request, request_digest=digest(request),
                             review=review_request(state, request, actual_acceptance=True))
        return save_trial_change(change)
    if args.command == 'permit':
        return permit(args, core, state, key, confirmation, seed=seed)
    if args.command == 'reason' and not args.owner_instruction.strip():
        raise StateDenied('実際のOwner指示を入力してください。')
    return append_or_close(args, core, state, key, confirmation, personal=seed is not None)
