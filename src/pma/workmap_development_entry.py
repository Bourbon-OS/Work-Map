"""Current development command adapters for converted Maps."""
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

from pma.workmap_normal_entry import open_normal_state
from pma.workmap_state_contract import (
    StateConflict, StateDenied, StateUnknown, now, permission_authority_status, read_work,
)
from pma.workmap_state_core import prepare, review_request
from pma.workmap_state_entry import prepare_trial_add, save_trial_change, trial_core
from pma.workmap_state_trial_integrated import (
    append_or_close, handle, operation_sources, source_binding, store_scope,
)
from pma.workmap_state_trial_permission_pin import prepare_pin, shared_scope
from pma.workmap_store_types import StoreError
from pma.workmap_pin_guide import GUIDE_REF


def key_for(state, work, *, current=True):
    key = state['aliases'].get(work, work)
    available = state['map']['members'] if current else state['display_ids']
    if key not in available:
        raise StateUnknown('指定したWorkを確認できません。')
    return key


def work_view(state, key, actor=None):
    view = read_work(state, key, actor=actor)
    for work in [view.get('identity'), *view.get('ancestors', [])]:
        if work:
            work['display_id'] = state['display_ids'][work['work_key']]
            work['intent_status'] = 'confirmed' if work['intent'] is not None else 'unknown'
    return view


def choice_collections(state, view, key):
    collections = dict(view.get('parent_collections', {}))
    if view['status'] == 'current':
        collections[key] = view['collections']
    for group in collections.values():
        permissions = group['permissions']
        permissions['source_status'] = {p['id']: permission_authority_status(state, p)
                                        for p in permissions['recorded']}
    return collections


def pin_draft(state, path, root, actor, destination):
    core = trial_core(path, root)
    key = key_for(state, destination)
    view = work_view(state, key, actor)
    active = [pin for pin in view['own_pins'] if pin['mode'] == 'active']
    source = active[0]['work_id'] if active else None
    operation = 'move_pin' if source else 'set_pin'
    guide = source_binding(core, GUIDE_REF)
    context = dict(destination=view, source=work_view(state, source) if source else None,
                   shared_pins=shared_scope(state, actor, key, operation))
    binding = dict(store=list(store_scope(state, path)), revision=state['revision'],
                   evidence_root=str(root), guide=guide, actor=actor, source=source, destination=key)
    draft = dict(kind='pin-move-state-inputs-v1', binding=binding,
                 inputs=dict(reason=None, sources=[guide['ref']]),
                 resume_suggestions={'status': 'not_provided_by_this_projection'})
    return context, draft


def entry_index(records):
    return [{f: e[f] for f in ('id', 'summary', 'kind') if f in e} for e in records]


def read_selected(records, selected):
    if any(sum(e['id'] == identifier for e in records) != 1 for identifier in selected):
        raise StateUnknown('指定のWorkと表示範囲に、その記録を一件に特定できません。')
    return [e for e in records if not selected or e['id'] in selected]


def read_new(path, work, *, expected_project, revision, tree, tree_reader,
             brief, thread, evidence, selected, pin_destination, actor, pin_inputs,
             evidence_repo, unsupported, state_reader=open_normal_state):
    state = None
    try:
        state = state_reader(path)
        if state is None:
            return None
        if expected_project is not None and expected_project != state['project_id']:
            raise StateUnknown('指定したプロジェクトと一致しません。')
        if revision is not None and revision != state['revision']:
            raise StateConflict('Mapの版が変わりました。読み直して判断してください。')
        if unsupported:
            raise StateDenied('新形式でこの読取り指定にはまだ対応していません。')
        result = dict(status='confirmed', revision=state['revision'], project_id=state['project_id'])
        if pin_destination is not None:
            context, draft = pin_draft(state, path, evidence_repo.resolve(), actor, pin_destination)
            result.update(view='pin_move_review', read_only=True, context=context)
            if pin_inputs:
                result['inputs_draft'] = draft
            return result
        if tree:
            result['tree'] = tree_reader(state)
            if not brief:
                return result
        key = key_for(state, work, current=False)
        meaning = [e for e in state['thread_entries'] if key in e['works']]
        results = [e for e in state['evidence'] if key in e['works']]
        if thread:
            result['thread'] = dict(thread_entries=read_selected(meaning, selected),
                                    status='confirmed', scope='stored_meaning')
        elif evidence:
            result.update(work=key, evidence=read_selected(results, selected))
        else:
            view = work_view(state, key)
            identity = view.get('identity')
            context = dict(project_id=state['project_id'], revision=state['revision'],
                           work=dict(work_key=key, display_id=state['display_ids'][key],
                                     status='closed_history' if view['status'] == 'closed' else view['status'],
                                     identity=identity, intent_status=(identity or {}).get('intent_status','unknown')),
                           ancestors=view.get('ancestors', []), dependencies=view.get('dependencies', []),
                           children=view.get('children', []), collections=choice_collections(state, view, key),
                           thread=dict(entries=entry_index(meaning)), evidence=dict(entries=entry_index(results)),
                           retrieve=dict(work=key, expected_revision=state['revision']),
                           boundary='集合の値は表示したWorkと祖先の範囲です。未確認は空集合と区別します。')
            if view['status'] == 'closed':
                close = next(e for e in meaning if e['id'] == state['retirements'][key])
                context['close'] = close
                context['parent_at_close'] = close['parent']
            if selected or not brief:
                context['records'] = read_selected(meaning + results, selected)
            result.update(view='work_brief' if brief else 'work_detail', context=context)
        return result
    except (OSError, ValueError, sqlite3.Error, StoreError) as exc:
        return dict(status='unknown', reason=str(exc), **({'revision': state['revision']} if state else {}))


def require_inputs(*values):
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise StateDenied('操作に必要な入力が不足しています。')


def confirmed_report(path, state, saved, operation, *, actor, expected=None):
    status = saved['status']
    unconfirmed = dict(result=dict(result='applied'), readback=dict(status='changed_or_unavailable'))
    if status == 'saved_but_changed':
        return unconfirmed
    if status != 'confirmed':
        value = 'rejected' if status == 'rejected' else 'unknown'
        return dict(result=dict(result=value, reason=saved['message']))
    try:
        current = open_normal_state(path)
    except (OSError, ValueError, sqlite3.Error, StoreError):
        return unconfirmed
    key = saved['work_key']
    if current is None or current['revision'] != saved['revision'] or current['map_id'] != state['map_id']:
        return unconfirmed
    if operation == 'add':
        work, parent = expected
        verified = current['works'].get(key) == work and any(
            r['kind'] == 'parent' and r['child'] == key and r['parent'] == parent
            for r in current['map']['relations'])
    elif operation == 'pin':
        verified = [p['work_id'] for p in current['map']['pins']
                    if p['actor_id'] == actor and p['mode'] == 'active'] == [key]
    elif operation == 'result':
        verified = expected in current['evidence']
    else:
        verified = key in current['retirements'] and key not in current['map']['members']
    return dict(result=dict(result='applied'),
                readback=dict(status='confirmed' if verified else 'changed_or_unavailable', work_key=key,
                              display_id=current['display_ids'].get(key), revision=current['revision']))


def add(args, core, state):
    if args.root or args.display_id or args.owner_instruction or args.reviewed_owner_add or args.reason:
        raise StateDenied('試験範囲は現在の親への、既存許可に基づく通常登録です。')
    require_inputs(args.actor, args.parent, args.name, args.intent, args.origin)
    key = key_for(state, args.parent)
    operation_sources(args, core.store.path.parents[1], core.store.path, state, [(key, 'add_work')])
    source = SimpleNamespace(path=core.store.path, evidence_root=args.evidence_repo, project_id=state['project_id'],
                             map_id=state['map_id'], state_trial=True)
    prepared = prepare_trial_add(source, state, parent=key, name=args.name, intent=args.intent,
                                 origin=args.origin, actor=args.actor, refs=args.source)
    return save_trial_change(prepared), prepared.request['value']['work'], key


def move_pin(args, core, state, packet):
    if not args.reviewed_pin_move or not packet or args.owner_instruction or args.owner_source or args.reviewed_owner_pin:
        raise StateDenied('現在の地図を読んだPin切替下書きと確認が必要です。許可を新設しません。')
    if set(packet) != {'kind', 'binding', 'inputs', 'resume_suggestions'}:
        raise StateDenied('読取りで得た下書き全体を使ってください。')
    binding = packet['binding']
    require_inputs(binding['actor'], binding['destination'])
    _, current = pin_draft(state, core.store.path, args.evidence_repo, binding['actor'], binding['destination'])
    inputs = packet['inputs']
    if packet != {**current, 'inputs': inputs}:
        raise StateConflict('Map・担当範囲・根拠の版が変わりました。読み直してください。')
    if not isinstance(inputs, dict) or set(inputs) != {'reason', 'sources'}:
        raise StateDenied('この下書きの入力は理由と根拠です。')
    require_inputs(inputs['reason'])
    sources = inputs['sources']
    if not isinstance(sources, list) or not sources:
        raise StateDenied('照合した根拠を指定してください。')
    require_inputs(*sources)
    selected = [source_binding(core, ref) for ref in sources]
    args.actor, args.reason = binding['actor'], inputs['reason']

    def bindings(operation_args, root, db, current_state, required):
        approved = operation_sources(operation_args, root, db, current_state, required)
        return list({b['ref']: b for b in [*approved, *selected]}.values())

    prepared = prepare_pin(args, args.evidence_repo, core.store.path, state, binding['destination'],
                           source_bindings=bindings, require_owner_instruction=False)
    return save_trial_change(prepared)


def record(args, core, state, key):
    require_inputs(args.actor, args.summary)
    if args.related_entry:
        raise StateDenied('関連結果の追加指定は今回の試験範囲外です。')
    selected = [source_binding(core, ref) for ref in args.source]
    if not selected:
        raise StateDenied('確認した結果の根拠を指定してください。')
    bindings = operation_sources(args, args.evidence_repo, core.store.path, state, [(key, 'record_result')])
    result = dict(id='result:' + uuid4().hex, works=[key], summary=args.summary.strip(),
                  source_refs=list(dict.fromkeys(b['ref'] for b in selected)), actor=args.actor, recorded_at=now())
    for name in ('why', 'occurred_at'):
        value = getattr(args, name)
        if value:
            result[name] = value
    sources = list({b['ref']: b for b in [*bindings, *selected]}.values())
    request = prepare(state, args.actor, 'record_result', key, value=result, sources=sources)
    return handle(core, request, review_request(state, request, actual_result=True)), result


def close(args, core, state, key, confirmation):
    require_inputs(args.actor, args.outcome, args.reason)
    if (not args.reviewed_close or args.result or args.revisit or args.successor
            or (args.outcome == 'completed' and not args.evidence) or not (args.source or args.evidence)
            or any(not value.strip() for value in args.residual)):
        raise StateDenied('終了判断と根拠が必要です。試験は後継指定のない終了が対象です。')
    explicit_owner = bool(args.reviewed_owner_close or args.owner_source or confirmation is not None)
    if explicit_owner:
        if not (args.reviewed_owner_close and args.owner_source and confirmation is not None):
            raise StateDenied('Owner指示から操作するには照合済みの判断を受け取ってください。')
        if source_binding(core, args.owner_source)['ref'] != confirmation.source:
            raise StateDenied('終了を許可したOwner指示の版と一致しません。')
        args.owner_instruction = confirmation.instruction
    args.command = 'close'
    args.evidence = list(dict.fromkeys(args.evidence + args.source))
    return append_or_close(args, core, state, key, confirmation, existing_close_authority=not explicit_owner)


def write_new(args, operation, *, packet=None, confirmation=None, state_reader=open_normal_state):
    if args.backend != 'sqlite':
        return None
    try:
        state = state_reader(args.store)
        if state is None:
            return None
        if state['metadata'].get('migration') is None:
            raise StateDenied('この入口の対象は変換済みのMapです。')
        if args.resume:
            raise StateDenied('現在のMap・理由・結果を読み直してください。')
        args.evidence_repo = (args.evidence_repo or args.store.parents[1]).resolve()
        if args.evidence_repo != args.store.resolve().parents[1]:
            raise StateDenied('書込みの根拠はこのMapの文書フォルダに限ります。')
        core = trial_core(args.store, args.evidence_repo)
        expected = None
        if operation == 'add':
            saved, work, parent = add(args, core, state)
            expected = (work, parent)
        elif operation == 'pin':
            saved = move_pin(args, core, state, packet)
        else:
            key = key_for(state, args.work)
            if operation == 'result':
                saved, expected = record(args, core, state, key)
            else:
                saved = close(args, core, state, key, confirmation)
        return confirmed_report(args.store, state, saved, operation, actor=args.actor, expected=expected)
    except StateDenied as exc:
        return dict(result=dict(result='rejected', reason=str(exc)))
    except (OSError, ValueError, sqlite3.Error, StoreError, KeyError, TypeError) as exc:
        return dict(result=dict(result='unknown', reason=str(exc)))
