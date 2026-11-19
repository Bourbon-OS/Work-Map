"""The one-time bridge from v0.5 personal Maps to the current State Store."""
import base64
from copy import deepcopy
import hashlib
import json
import os
from uuid import uuid4

from pma.workmap_contract import validate_snapshot
from pma.workmap_notes import recorded_evidence
from pma.workmap_resume import close_history
from pma.workmap_sqlite_store import SQLiteStore
from pma.workmap_state_contract import (
    FORMAT, StateConflict, StateDenied, StateUnknown, digest, is_version_ref, now, validate, version_ref,
)
from pma.workmap_state_entry import resolve_trial_source
from pma.workmap_state_legacy_cutover import execute, prepare, rollback
from pma.workmap_state_read import trial_local_path
from pma.workmap_state_recovery import restore_candidate
from pma.workmap_state_sources import business_refs, local_file
from pma.workmap_state_store import StateStore
from pma.workmap_state_trial_integrated import INITIAL_OPERATIONS, capture_personal_instruction, store_scope
from pma.workmap_thread_view import meaning_thread


MEANING = {
    'rationale_added': 'rationale', 'correction': 'correction', 'close': 'close',
    'intent_confirmation': 'adoption', 'intent_correction': 'correction',
    'intent_refinement': 'refinement', 'reparent': 'reparent', 'reopen': 'rationale',
    'evidence_recorded': 'rationale',
}


def fields(item, names):
    return {name: deepcopy(item[name]) for name in names if name in item}


def references(entry):
    return list(dict.fromkeys(entry['source_refs'] + entry.get('evidence_refs', [])))


def checked_initial_owner(old, root, actor):
    key = old['aliases'].get('W0')
    if key not in old['map']['members'] or any(r.get('child') == key for r in old['map']['relations']):
        raise StateDenied('旧Mapの最上位Workを確認できません。')
    births = []
    for row in old['thread_entries']:
        if row['kind'] == 'work_added' and row['works'] == [key]:
            details = json.loads(row['consequence'])
            receipt = old['receipts'].get(row.get('transition_id'), {})
            if (details.get('registration_only') is True and details.get('parent_intent') is None
                    and receipt.get('result') == 'applied'):
                births.append((row, details.get('owner_sources', [])))
    if len(births) != 1 or births[0][0]['actor'] != actor or not births[0][1]:
        raise StateDenied('旧Mapを作成した編集者IDを指定してください。')
    for binding in births[0][1]:
        if binding['ref'] not in births[0][0]['source_refs']:
            raise StateUnknown('旧Mapの作成指示を照合できません。')
        raw = resolve_trial_source(root, binding['ref'])
        if hashlib.sha256(raw).hexdigest() != binding['sha256']:
            raise StateConflict('旧Mapの作成指示の内容が変わっています。')
    return key


def convert_records(old, root):
    view = meaning_thread(old, repo=root, include_sources=True)
    if view['status'] != 'confirmed':
        raise StateUnknown('Work Threadで確認できない判断理由があります。先に旧Mapの理由と根拠を確認してください。')
    raw = {item['id']: item for item in old['thread_entries']}
    results = [dict(fields(raw[row['id']], ('id', 'works', 'summary', 'actor', 'why', 'recorded_at', 'occurred_at')),
                    source_refs=references(raw[row['id']])) for row in recorded_evidence(old)]
    result_ids = {row['id'] for row in results}
    assignments = {}
    for displayed, keys in view['source_record_ids'].items():
        for key in keys:
            if key in assignments:
                raise StateUnknown('同じ理由が複数の記録に割り当てられています。')
            assignments[key] = displayed
    retirements = {}
    for work, key in old['retirements'].items():
        if key not in assignments:
            raise StateUnknown('終了したWorkの判断理由を確認できません。')
        retirements[work] = assignments[key]
    if len(set(retirements.values())) != len(retirements):
        raise StateUnknown('複数の終了判断を一件へまとめた記録は個別の確認が必要です。')
    for work, key in old['retirements'].items():
        if raw[key]['works'] != [work]:
            raise StateUnknown('終了したWorkと記録の対応を確認してください。')

    selected = {row['id'] for row in view['thread_entries']}
    meaning = []
    for item in view['thread_entries']:
        row_id = item['id']
        originals = [raw[key] for key in view['source_record_ids'][row_id]]
        if not originals or any(row['kind'] not in MEANING for row in originals):
            raise StateUnknown('選別された理由に、個別の変換確認が必要な記録があります。')
        first = originals[0]
        closed = [row for row in originals if row['kind'] == 'close']
        if len(closed) > 1:
            raise StateUnknown('複数の終了判断を一件へまとめた記録は個別の確認が必要です。')
        entry = dict(fields(item, ('id', 'works', 'summary', 'why', 'source_refs', 'revisit')),
                     kind=MEANING[first['kind']],
                     **fields(first, ('actor', 'occurred_at', 'recorded_at')))
        if len(originals) != 1 or row_id != first['id'] or any(
                item[f] != first[f] for f in ('summary', 'why')) or set(item['source_refs']) != set(references(first)):
            entry['original_reasons'] = [fields(row, ('id', 'kind', 'summary', 'why', 'actor',
                                          'recorded_at', 'occurred_at', 'source_refs', 'evidence_refs'))
                                          for row in originals]
        details = {}
        for row in originals:
            detail = fields(row, ('consequence', 'assumptions', 'change', 'revisit'))
            if detail:
                details[row['id']] = detail
        if closed:
            closing = closed[0]
            history = close_history(closing)
            if not history or 'identity' not in history:
                raise StateUnknown('終了したWorkの最終状態を確認できません。')
            entry.update(kind='close', identity=deepcopy(history['identity']), parent=history['parent_at_close'],
                         close_display_id=history['display_id'],
                         **fields(closing, ('outcome', 'successor', 'residuals')))
            if not history.get('reported_consequence') and closing['id'] in details:
                details[closing['id']].pop('consequence', None)
            elif history.get('reported_consequence'):
                details[closing['id']]['consequence'] = history['reported_consequence']
        if details:
            entry['historical'] = dict(details_by_id=details)
        for link in item['related_entries']:
            mapped = assignments.get(link, link)
            if mapped == row_id:
                continue
            if mapped in selected:
                field = 'related_entries'
            elif link in result_ids:
                field, mapped = 'related_results', link
            else:
                raise StateUnknown('判断理由からの参照先を変換できません。旧Mapで参照先を確認してください。')
            values = entry.setdefault(field, [])
            if mapped not in values:
                values.append(mapped)
        meaning.append(entry)
    return meaning, results, retirements


def carry_sources(snapshot, old_root, new_root, setup_ref):
    bound, files, unavailable = {}, {}, []
    for ref in business_refs(snapshot):
        root = new_root if ref == setup_ref else old_root
        try:
            raw = resolve_trial_source(root, ref)
        except (OSError, StateDenied):
            unavailable.append(ref)
            continue
        actual = version_ref(ref.split('#sha256=', 1)[0], raw)
        if is_version_ref(ref) and actual != ref:
            unavailable.append(ref)
            continue
        bound[ref] = actual
        files[actual] = base64.b64encode(raw).decode('ascii')
    if setup_ref not in files:
        raise StateConflict('新形式用の設定指示を照合できません。')

    # Bind the bytes found now without treating an old unversioned grant as authority.
    ref_fields = {'source_refs', 'refs', 'evidence_refs', 'additional_source_refs'}
    def bind(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ref_fields:
                    value[key] = list(dict.fromkeys(bound.get(ref, ref) for ref in item))
                else:
                    bind(item)
        elif isinstance(value, list):
            for item in value:
                bind(item)
    bind(snapshot)
    migration = snapshot['metadata']['migration']
    for scopes in migration['work_sources'].values():
        for key, refs in scopes.items():
            scopes[key] = list(dict.fromkeys(bound.get(ref, ref) for ref in refs))
    migration['missing_versioned_sources'] = [ref for ref in unavailable if is_version_ref(ref)]
    return files, unavailable


def locations(old_root, target_root):
    old_root, target_root = trial_local_path(old_root), trial_local_path(target_root)
    old_root, target_root = trial_local_path(old_root.resolve()), trial_local_path(target_root.resolve())
    if target_root.is_relative_to(old_root) or old_root.is_relative_to(target_root):
        raise StateDenied('移行先には旧Mapと分離したフォルダを指定してください。')
    old_db, new_db = (root / 'data/workmap.sqlite3' for root in (old_root, target_root))
    if old_db.resolve() != old_db or new_db.resolve() != new_db:
        raise StateDenied('MapのDBへ直接つながる保存先を指定してください。')
    return old_root, target_root, SQLiteStore(old_db), StateStore(new_db), new_db.parent / 'workmap-migration.json'


def prepare_personal_migration(old_root, target_root, actor, owner_instruction):
    old_root, target_root, source, target, plan_path = locations(old_root, target_root)
    if target_root.exists() and (not target_root.is_dir() or any(target_root.iterdir())):
        raise StateDenied('移行先には新しいフォルダを指定してください。')
    initial = source.read(read_only=True)
    old = validate_snapshot(initial.snapshot)
    metadata = old['metadata']
    if (old['format_version'] != '0.4.2' or metadata.get('basis') != 'new_empty_personal_store'
            or metadata.get('store_role') != 'canonical_current'
            or metadata.keys() - {'store_role', 'completeness', 'basis', 'work_sources'}):
        raise StateDenied('v0.5.0の個人向け試用版で作った現用Mapを指定してください。')
    key = checked_initial_owner(old, old_root, actor)
    meaning, results, retirements = convert_records(old, old_root)
    current = fields(old['map'], ('members', 'relations', 'pins', 'permissions', 'action_rules'))
    for rule in current['action_rules']:
        if isinstance(rule.get('applies_to'), str):
            rule['legacy_scope'] = rule.pop('applies_to')
    legacy_permissions = [grant['id'] for grant in current['permissions']]
    for grant in current['permissions']:
        grant.setdefault('constraints', [])
        if grant['effect'] == 'allow':
            grant['constraints'].append('legacy_reapproval_required')
        elif 'add_work_in_map' in grant['operations']:
            raise StateUnknown('Map全体への既存の禁止・未確認条件は、移行先での適用範囲を先に確認してください。')
    migration = dict(legacy_revision=old['revision'], missing_versioned_sources=[],
                     legacy_permissions=legacy_permissions, work_sources=deepcopy(metadata.get('work_sources', {})),
                     review_notes=deepcopy(old['map']['notes']))
    state = dict(format_version=FORMAT, project_id=old['project_id'], map_id='map:' + uuid4().hex,
                 revision=uuid4().hex, recorded_at=now(), works={wid: old['works'][wid] for wid in current['members']},
                 display_ids=old['display_ids'], aliases=old['aliases'], map=current, retirements=retirements,
                 thread_entries=meaning, evidence=results, movements={},
                 metadata=dict(store_role='canonical_current', completeness=metadata.get('completeness') or {},
                               unconfirmed_intents=[], migration=migration))
    setup = capture_personal_instruction(target_root, owner_instruction, initial=True)
    current['permissions'].append(dict(id='permission:' + uuid4().hex, targets=[key], operations=INITIAL_OPERATIONS,
                                      grantee=actor, effect='allow', grantor_ref='principal:owner',
                                      constraints=list(store_scope(state, target.path)), source_refs=[setup]))
    files, unresolved = carry_sources(state, old_root, target_root, setup)
    validate(state)
    if source.read(read_only=True).token != initial.token:
        raise StateConflict('候補の作成中に元のMapが変わりました。現在を読み直してください。')
    target.path.parent.mkdir(parents=True, exist_ok=True)
    bundle = dict(format='workmap-state-backup-1', state=state, state_sha256=digest(state), source_files=files)
    restore_candidate(bundle, target.path)
    review = prepare(source, target, source_token=initial.token,
                     candidate_content=digest(target.read(read_only=True).snapshot), owner_source=setup)
    review['source_bindings'] = sorted(files.keys() - {setup})
    with local_file(plan_path).open('x', encoding='utf-8') as stream:
        json.dump(review, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return dict(status='confirmed', candidate=str(target.path), review_code=digest(review),
                current_works=len(state['works']), closed_works=len(retirements),
                reasons=len(meaning), results=len(results), unverified_sources=unresolved,
                legacy_permission_count=len(legacy_permissions))


def apply_personal_migration(old_root, target_root, reviewed_digest, *, revert=False):
    old_root, target_root, source, target, plan_path = locations(old_root, target_root)
    plan = json.loads(local_file(plan_path).read_bytes())
    if digest(plan) != reviewed_digest:
        raise StateConflict('確認した候補の review_code が一致しません。')
    new = target.read(read_only=True)
    if not revert and new.token != plan['active']['token']:
        for ref in plan['source_bindings']:
            try:
                raw = resolve_trial_source(old_root, ref)
            except (OSError, StateDenied) as exc:
                raise StateConflict('候補作成後に旧Mapの根拠が確認できなくなりました。') from exc
            if version_ref(ref.split('#sha256=', 1)[0], raw) != ref:
                raise StateConflict('候補作成後に旧Mapの根拠が変わりました。')
    action = rollback if revert else execute
    outcome = action(source, target, plan, reviewed_digest=reviewed_digest)
    return dict(status='confirmed', outcome=outcome, map_dir=str(target_root), revision=target.read(read_only=True).snapshot['revision'])
