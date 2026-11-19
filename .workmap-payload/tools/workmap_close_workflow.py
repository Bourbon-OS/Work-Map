"""One explicit Owner close instruction, using existing permission/close operations."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from pma.workmap_close import evidence_reader
from pma.workmap_contract import ContextUnknown, SnapshotInvalid, inspect_work, require_decision_context, validate_snapshot
from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, _digest
from pma.workmap_store import StoreError
from pma.workmap_store_factory import open_store
from tools.apply_workmap_transition import _apply_request
from tools.workmap_pin_workflow import _missing_operations, _projection, _save

KIND = 'owner-close-workflow-v1'
FIELDS = ('actor', 'work', 'outcome', 'reason', 'sources', 'evidence_refs', 'result',
          'revisit', 'successors', 'residuals', 'release_own_pin')


def _permission_request(state, bundle, *, remove=False):
    grant = bundle['grant']; target = bundle['target']
    tid = 'transition:owner-close-' + ('end:' if remove else 'grant:') + bundle['workflow_id']
    change = dict(op='remove_permission' if remove else 'set_permission', target=target,
                  permission_id=grant['id'], expected=grant if remove else None)
    if not remove:
        change['value'] = grant
    summary = 'End only this temporary close authority' if remove else 'Record explicit limited Owner close instruction'
    entry = dict(id='thread:' + tid, works=[target], kind='permission_changed', summary=summary,
                 why=bundle['meaning']['reason'], actor=bundle['meaning']['actor'],
                 occurred_at=None, recorded_at=datetime.now(timezone.utc).isoformat(),
                 source_refs=[bundle['owner_source']], transition_id=tid)
    return dict(format_version=state['format_version'], project_id=state['project_id'],
                transition_id=tid, actor_id=entry['actor'], base_revision=state['revision'],
                intent=summary, source_refs=entry['source_refs'], preconditions=[],
                changes=[change], thread_entries=[entry])


def _check_bundle(bundle, path, backend, repo):
    if (not isinstance(bundle, dict) or bundle.get('kind') != KIND
            or bundle.get('store_binding') != {'path': str(path), 'backend': backend}
            or bundle.get('evidence_repo') != str(repo)
            or bundle.get('checkpoint_digest') != _digest({k: v for k, v in bundle.items() if k != 'checkpoint_digest'})):
        raise ValueError('saved Owner close belongs to a different Store/root or was changed')
    meaning, grant = bundle['meaning'], bundle['grant']
    if set(meaning) != set(FIELDS) or meaning['work'] != bundle['target']:
        raise ValueError('saved Owner close scope differs')
    if (not isinstance(bundle['owner_source'], str)
            or not bundle['owner_source'].startswith('repo:docs/work-management/evidence/owner_')
            or not bundle['owner_source'].endswith('.txt') or bundle['owner_source'] not in meaning['sources']
            or set(bundle['source_bindings']) != set(meaning['sources'] + meaning['evidence_refs'])
            or bundle['stage'] not in ('grant', 'close', 'cleanup', 'aborted', 'done')):
        raise ValueError('saved Owner instruction/evidence binding differs')
    required = ['close'] + (['remove_pin'] if meaning['release_own_pin'] else [])
    if grant is not None and (grant['id'] != 'permission:close-workflow:' + bundle['workflow_id']
            or grant['targets'] != [bundle['target']] or grant['grantee'] != meaning['actor']
            or grant['grantor_ref'] != 'principal:owner' or grant['effect'] != 'allow'
            or not set(grant['operations']) <= set(required + ['remove_permission'])
            or 'remove_permission' not in grant['operations'] or not set(grant['operations']).intersection(required)
            or grant['source_refs'] != [bundle['owner_source']]):
        raise ValueError('saved temporary authority exceeds one reviewed close')
    for key, remove in (('grant_request', False), ('cleanup_request', True)):
        request = bundle.get(key)
        if request is not None:
            expected = _permission_request({'format_version': request['format_version'],
                'project_id': bundle['project_id'], 'revision': request['base_revision']}, bundle, remove=remove)
            # Creation time is persisted, rather than regenerated for recovery.
            expected['thread_entries'][0]['recorded_at'] = request['thread_entries'][0]['recorded_at']
            if request != expected:
                raise ValueError('saved permission request differs from the bounded close')


def owner_close_workflow(store_path, *, backend, reviewed_close=False, reviewed_owner_close=False,
                         owner_source=None, resume=None, evidence_repo=None, **meaning):
    from tools.close_workmap_work import _prepare_close_request, _save_close_request, _check_request, close_work
    if not (reviewed_close and reviewed_owner_close):
        raise TransitionDenied('review this close and explicitly attribute its Owner instruction before execution')
    started = perf_counter(); path = Path(store_path).resolve()
    repo = Path(evidence_repo or Path(__file__).resolve().parents[1]).resolve()
    resolve = evidence_reader(repo)
    state = lambda: validate_snapshot(open_store(path, backend).read().snapshot)
    if resume is not None:
        if owner_source is not None or any(meaning.get(k) for k in FIELDS):
            raise ValueError('resume uses the saved Owner close without replacement decisions')
        checkpoint = Path(resume).resolve()
        bundle = json.loads(checkpoint.read_text(encoding='utf-8'))
        try:
            _check_bundle(bundle, path, backend, repo)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('saved Owner close structure differs') from exc
    else:
        if not isinstance(owner_source, str) or not (owner_source.startswith('repo:docs/work-management/evidence/owner_') and owner_source.endswith('.txt')):
            raise ValueError('supply the existing explicit Owner close instruction reference')
        if not resolve(owner_source).strip():
            raise ValueError('explicit Owner close instruction is empty')
        meaning = {k: meaning[k] for k in FIELDS}
        meaning['sources'] = list(dict.fromkeys([*meaning['sources'], owner_source]))
        s = state()
        initial = _prepare_close_request(s, **meaning, evidence_repo=repo)
        target = initial['changes'][-1]['target']; meaning['work'] = target
        entry = initial['thread_entries'][0]
        meaning.update(sources=entry['source_refs'], evidence_refs=entry.get('evidence_refs', []),
                       successors=entry.get('successor', []), residuals=entry['residuals'])
        require_decision_context(inspect_work(s, target))
        needed = ['close'] + (['remove_pin'] if meaning['release_own_pin'] else [])
        covered = {op for p in s['map']['permissions'] if p['grantee'] == meaning['actor']
                   and target in p['targets'] and p['effect'] == 'allow' for op in p['operations']}
        # Already-authorized close keeps the ordinary checks; it needs no cleanup authority.
        missing = _missing_operations(s, target, meaning['actor'], needed) if set(needed) - covered else []
        wf = str(uuid4())
        grant = (dict(id='permission:close-workflow:' + wf, targets=[target], grantee=meaning['actor'],
            grantor_ref='principal:owner', effect='allow', operations=[*missing, 'remove_permission'],
            constraints=['Only this explicit one-Work close and requested own Pin release; no parent move or other Work authority',
                         'Workflow ' + wf], source_refs=[owner_source]) if missing else None)
        after = copy.deepcopy(_projection(s))
        if grant:
            after['map']['permissions'].append(grant)
        bundle = dict(kind=KIND, store_binding={'path': str(path), 'backend': backend}, evidence_repo=str(repo),
            project_id=s['project_id'], workflow_id=wf, owner_source=owner_source, meaning=meaning, target=target,
            source_bindings={x['ref']: x['sha256'] for x in initial['preconditions'][0]['expected']['evidence']},
            frontier_revision=s['revision'], frontier_digest=_digest(_projection(s)), grant_after_digest=_digest(after),
            grant=grant, grant_request=None, close_path=None, close_request_digest=None, cleanup_request=None, failure=None,
            stage='grant' if grant else 'close')
        directory = (path if backend == 'text' else path.parent) / 'transition-requests'
        directory.mkdir(exist_ok=True)
        checkpoint = directory / (wf + '.owner-close.json')
        _save(checkpoint, bundle)
    report = dict(request_path=str(checkpoint), workflow_id=bundle['workflow_id'], work_key=bundle['target'])

    def finish(result):
        try:
            remaining = [p['id'] for p in state()['map']['permissions']
                         if bundle['grant'] and p['id'] == bundle['grant']['id']]
        except (OSError, StoreError, SnapshotInvalid):
            remaining = 'unknown'
        return {**report, **result, 'temporary_permissions_remaining': remaining,
                'entrypoint_seconds': perf_counter() - started}

    def current_basis(s):
        if s['project_id'] != bundle['project_id'] or s['revision'] != bundle['frontier_revision'] or _digest(_projection(s)) != bundle['frontier_digest']:
            raise TransitionConflict('close premises changed; review current Map, do not rebase')
        if any(hashlib.sha256(resolve(ref)).hexdigest() != digest for ref, digest in bundle['source_bindings'].items()):
            raise TransitionConflict('close instruction/evidence changed; review again')

    def cleanup():
        s = state(); grant = bundle['grant']
        if s['project_id'] != bundle['project_id']:
            raise TransitionConflict('selected Store project changed; do not end another project authority')
        actual = next((p for p in s['map']['permissions'] if grant and p['id'] == grant['id']), None)
        if actual is not None:
            if actual != grant or bundle['target'] not in s['map']['members']:
                raise TransitionConflict('temporary close authority changed; preserve it for inspection')
            if bundle['cleanup_request'] is None:
                bundle['cleanup_request'] = _permission_request(s, bundle, remove=True)
                _save(checkpoint, bundle)
            _apply_request(path, bundle['cleanup_request'], backend=backend, evidence_repo=repo)
            if any(p['id'] == grant['id'] for p in state()['map']['permissions']):
                raise TransitionConflict('temporary close authority has not ended')
        bundle['stage'] = 'aborted'; _save(checkpoint, bundle)

    try:
        if bundle['stage'] == 'grant':
            s = state()
            q = bundle['grant_request']
            if q is None:
                current_basis(s)
                q = bundle['grant_request'] = _permission_request(s, bundle)
                _save(checkpoint, bundle)
            if q['transition_id'] not in s['receipts']:
                current_basis(s)
            receipt = _apply_request(path, q, backend=backend, reviewed_owner_permission=True, evidence_repo=repo)
            if receipt.get('result') != 'applied':
                raise TransitionError('temporary close authority was not applied')
            s = state()
            if _digest(_projection(s)) != bundle['grant_after_digest']:
                raise TransitionConflict('context changed after close authority; review again')
            bundle.update(stage='close', frontier_revision=s['revision'], frontier_digest=bundle['grant_after_digest'])
            _save(checkpoint, bundle)
        if bundle['stage'] in ('close', 'done'):
            if bundle['close_path'] is None:
                s = state(); current_basis(s)
                q = _prepare_close_request(s, **bundle['meaning'], evidence_repo=repo)
                bundle['close_path'] = str(_save_close_request(path, backend, q, repo))
                bundle['close_request_digest'] = _digest(q)
                _save(checkpoint, bundle)
            saved = json.loads(Path(bundle['close_path']).read_text(encoding='utf-8'))
            q = saved['request']; _check_request(q)
            entry = q['thread_entries'][0]; meaning = bundle['meaning']
            if (_digest(q) != bundle['close_request_digest']
                    or saved['store_binding'] != bundle['store_binding'] or saved.get('evidence_repo') != str(repo)
                    or q['actor_id'] != bundle['meaning']['actor'] or q['changes'][-1]['target'] != bundle['target']
                    or entry['outcome'] != meaning['outcome'] or entry['why'] != meaning['reason']
                    or entry['source_refs'] != meaning['sources'] or entry.get('evidence_refs', []) != meaning['evidence_refs']
                    or entry.get('successor', []) != meaning['successors'] or entry['residuals'] != meaning['residuals']
                    or entry.get('consequence') != meaning['result'] or entry.get('revisit') != meaning['revisit']
                    or (len(q['changes']) == 2) != bundle['meaning']['release_own_pin']):
                raise ValueError('saved close request differs from its Owner scope')
            closed = close_work(path, backend=backend, resume=bundle['close_path'], reviewed_close=True, evidence_repo=repo)
            report['close_request_path'] = bundle['close_path']
            if closed['result'].get('result') == 'applied':
                if closed.get('readback', {}).get('status') == 'confirmed':
                    if bundle['grant'] and any(p['id'] == bundle['grant']['id'] for p in state()['map']['permissions']):
                        raise TransitionConflict('closed Work still exposes temporary authority')
                    bundle['stage'] = 'done'; _save(checkpoint, bundle)
                return finish({k: closed[k] for k in ('result', 'readback') if k in closed})
            if closed['result'].get('result') == 'unknown':
                return finish({'result': closed['result'], 'recovery': 'Resume this same Owner close checkpoint; do not issue another close'})
            bundle.update(stage='cleanup', failure=closed['result']); _save(checkpoint, bundle)
        if bundle['stage'] == 'cleanup':
            cleanup()
        return finish({'result': bundle['failure']})
    except (ContextUnknown, TransitionError, ValueError, KeyError, TypeError) as exc:
        failure = dict(result='rejected', category=type(exc).__name__, reason=str(exc))
        bundle.update(stage='cleanup', failure=failure); _save(checkpoint, bundle)
        try:
            cleanup()
        except (OSError, StoreError, SnapshotInvalid, TransitionError, ValueError) as cleanup_error:
            report['cleanup_error'] = str(cleanup_error)
        return finish({'result': failure})
    except (OSError, StoreError, SnapshotInvalid) as exc:
        return finish({'result': {'result': 'unknown', 'reason': str(exc)},
                       'recovery': 'Resume this same Owner close checkpoint; do not create a new grant or close'})
