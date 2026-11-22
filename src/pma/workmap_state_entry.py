"""Connect the existing isolated forms and commands to the history-free Core."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pma.workmap_state_contract import (
    StateConflict, StateDenied, StateUnknown, address_prefix, digest, is_version_ref, now, read_work, validate, version_ref,
)
from pma.workmap_state_core import StateCore, prepare, resume_current, review_request
from pma.workmap_state_read import read_trial_state
from pma.workmap_state_sources import local_file
from pma.workmap_state_store import StateStore
from pma.workmap_store_types import StoreError


def resolve_trial_source(root, ref):
    relative = ref[5:].split('#sha256=', 1)[0]
    parts = PurePosixPath(relative).parts
    if (not ref.startswith('repo:') or not parts or parts[0] != 'docs'
            or any(part in ('.', '..') or ':' in part or '\\' in part for part in parts)):
        raise StateDenied('このMapのdocs内の根拠資料を指定してください。')
    path = (root / relative).resolve()
    if not path.is_relative_to(root / 'docs') or path.suffix.lower() not in ('.txt', '.md', '.json'):
        raise StateDenied('根拠資料がこのMapの文書範囲外です。')
    return local_file(path).read_bytes()


def trial_core(path, evidence_root):
    return StateCore(StateStore(path), lambda ref: resolve_trial_source(evidence_root, ref))


@dataclass(frozen=True)
class PreparedStateChange:
    store_path: Path
    evidence_root: Path
    request: dict
    request_digest: str
    review: object = None
    field: str | None = None
    old_value: object = None
    new_value: object = None

    @property
    def bindings(self):
        return self.request['sources']


def _prepare_context(source, snapshot, target, actor, operation, refs):
    state = validate(snapshot)
    if not source.state_trial or state['metadata']['store_role'] != 'canonical_current':
        raise StateDenied('この試験候補は読み取り専用です。')
    if (source.project_id is not None and source.project_id != state['project_id']
            or source.map_id is not None and source.map_id != state['map_id']):
        raise StateDenied('保存先のMapが変わっています。')
    if target not in state['map']['members'] or not actor.strip() or not refs:
        raise StateDenied('対象Work・担当・根拠資料を指定してください。')
    core = trial_core(source.path, source.evidence_root)
    selected = []
    bindings = {}
    for ref in dict.fromkeys(refs):
        raw = core.resolver(ref)
        sha = hashlib.sha256(raw).hexdigest()
        bound = ref if is_version_ref(ref) else version_ref(ref, raw)
        if not bound.endswith('#sha256=' + sha):
            raise StateConflict('根拠資料の版が一致しません。')
        bindings[bound] = sha
        if ref != bound:
            bindings[ref] = sha  # Also recheck the exact file selected by the operator.
        selected.append(bound)
    for grant in state['map']['permissions']:
        if grant['grantee'] != actor or grant['effect'] != 'allow':
            continue
        operations = grant['operations']
        if not (target in grant['targets'] and operation in operations
                or operation == 'add_work' and 'add_work_in_map' in operations):
            continue
        # A bound grant is authority only after the Core checks its actual scope.
        for ref in grant['source_refs']:
            try:
                raw = core.resolver(ref)
            except (OSError, StateUnknown, StateDenied):
                continue
            value = hashlib.sha256(raw).hexdigest()
            if ref.endswith('#sha256=' + value):
                bindings[ref] = value
    return list(dict.fromkeys(selected)), [{'ref': ref, 'sha256': sha} for ref, sha in bindings.items()]


def prepare_trial_add(source, snapshot, *, parent, name, intent, origin, actor, refs):
    if not all(isinstance(value, str) and value.strip() for value in (name, intent, origin, actor)):
        raise StateDenied('名前・目的・生まれた理由・登録者を指定してください。')
    actor = actor.strip()
    selected, bindings = _prepare_context(source, snapshot, parent, actor, 'add_work', refs)
    prefix = address_prefix(snapshot['display_ids'][parent])
    number = 1
    while prefix + str(number) in snapshot['aliases']:
        number += 1
    work = dict(work_key='work:' + uuid4().hex, name=name.strip(), intent=intent.strip(),
                origin=dict(summary=origin.strip(), refs=selected))
    q = prepare(snapshot, actor, 'add_work', parent, value=dict(work=work, display_id=prefix + str(number)),
                sources=bindings)
    return PreparedStateChange(source.path, source.evidence_root, q, digest(q))


def prepare_trial_edit(source, snapshot, work_key, *, field, value, actor, reason, refs,
                       kind=None, same_work_basis='', owner_reviewed=False):
    if field not in ('name', 'intent', 'origin') or not value.strip() or not reason.strip():
        raise StateDenied('変更項目・変更後の内容・理由を入力してください。')
    actor = actor.strip()
    selected, bindings = _prepare_context(source, snapshot, work_key, actor, 'update_work', refs)
    old = snapshot['works'][work_key]
    new = deepcopy(old)
    if value.strip() == (old[field]['summary'] if field == 'origin' else old[field]):
        raise StateDenied('現在と同じ値です。')
    new[field] = dict(summary=value.strip(), refs=selected) if field == 'origin' else value.strip()
    meaning = []
    if field != 'name':
        why = reason.strip()
        if field == 'intent':
            owner_directory = PurePosixPath('docs/work-management/evidence')
            paths = [PurePosixPath(ref[5:].split('#sha256=', 1)[0]) for ref in selected]
            if (kind not in ('correction', 'refinement') or not owner_reviewed or not same_work_basis.strip()
                    or not all(p.parent == owner_directory and p.name.startswith('owner_') and p.suffix == '.txt'
                               for p in paths)):
                raise StateDenied('同じ目的を扱うOwner判断と理由を、所定の根拠文書で確認してください。')
            why += '\n同じWorkで扱う理由：' + same_work_basis.strip()
        meaning = [dict(id='meaning:' + uuid4().hex, works=[work_key], summary='Workの' + field + 'を訂正',
                        kind='refinement' if field == 'intent' and kind == 'refinement' else 'correction',
                        why=why, actor=actor, recorded_at=now(), source_refs=selected,
                        change={'before': old, 'after': new})]
    q = prepare(snapshot, actor, 'update_work', work_key, expected=old, value=new, sources=bindings, meaning=meaning)
    review = review_request(snapshot, q, same_purpose=bool(meaning))
    return PreparedStateChange(source.path, source.evidence_root, q, digest(q), review, field, old[field], new[field])


def save_trial_change(prepared):
    request = prepared.request
    try:
        if digest(request) != prepared.request_digest:
            raise StateDenied('確認後に要求が変わりました。')
        core = trial_core(prepared.store_path, prepared.evidence_root)
        result = core.apply(request, review=prepared.review)
    except ValueError as exc:
        return dict(status='rejected', message=str(exc))
    except (StoreError, OSError, RuntimeError):
        return dict(status='unknown', message='応答は未確認です。再送せず、現在のMapを読み直してください。')
    try:
        current = read_trial_state(prepared.store_path, project_id=request['project_id'], map_id=request['map_id']).snapshot
    except (ValueError, StoreError, OSError):
        return dict(status='unknown', message='現在のMapを読み戻せません。再送せず保存先を確認してください。')
    key = request['value']['work']['work_key'] if request['operation'] == 'add_work' else request['target']
    same_revision = current['revision'] == result['revision']
    return dict(status='confirmed' if same_revision else 'saved_but_changed', revision=current['revision'],
                work_key=key, message='保存して読み戻しました。' if same_revision else '保存後にMapの版が変わりました。')


def inspect_trial_change(prepared):
    view = resume_current(StateStore(prepared.store_path), prepared.request)
    return dict(status='observed', message='過去の成否は未確認です。現在のMapと理由を確認してください。', **view)


def read_trial_resume(store_path, work_id):
    state = read_trial_state(store_path).snapshot
    key = state['aliases'].get(work_id, work_id)
    return dict(status='confirmed', past_result='unknown', observed_revision=state['revision'],
                store_role=state['metadata']['store_role'], current=read_work(state, key))
