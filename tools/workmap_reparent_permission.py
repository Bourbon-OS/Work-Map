"""Prepare/apply one own reparent grant, or end that exact helper-created grant.

Uses ordinary set_permission/remove_permission and normal Core checks. Saved
packets bind paths and request bytes for recovery, not a second Store or an
authentication mechanism. Constraints remain the existing operator-reviewed
scope; this helper does not introduce a new permission enforcement model.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from pma.workmap_contract import validate_snapshot
from pma.workmap_core import _digest
from pma.workmap_store import StoreError
from pma.workmap_store_factory import open_store
from pma.workmap_owner_permission import review_owner_permission, verify_permission_sources
from tools.apply_workmap_transition import apply_result

KIND = 'workmap-reparent-permission-v1'


def _state(path, backend):
    s = validate_snapshot(open_store(path, backend).read().snapshot)
    if s['metadata'].get('store_role') != 'canonical_current':
        raise ValueError('selected Store is not canonical current')
    return s


def _grant_shape(request, binding):
    """Only the two fixed operations on one planned endpoint, never arbitrary grants."""
    target = binding['work'] if binding['endpoint'] == 'work' else binding['parent']
    change, = request['changes']
    grant = change['value']
    if (binding['endpoint'] not in ('work', 'parent') or not target
            or change != {'op': 'set_permission', 'target': target,
                          'permission_id': grant['id'], 'expected': None, 'value': grant}
            or not grant['id'].startswith('permission:reparent:')
            or grant['targets'] != [target] or grant['grantee'] != binding['actor']
            or grant['operations'] != ['reparent', 'remove_permission']
            or grant['effect'] != 'allow' or grant['grantor_ref'] != 'principal:owner'
            or grant['constraints'] != [
                'reparent_work:' + binding['work'], 'reparent_parent:' + binding['parent'],
                'remove_permission:this_grant_only']
            or request['actor_id'] != binding['actor']):
        raise ValueError('not a helper-created own reparent grant')
    return change


def _read_packet(path, store, backend, evidence_repo, snapshot):
    packet = json.loads(Path(path).read_text(encoding='utf-8'))
    b, q = packet['binding'], packet['request']
    if (packet['kind'] != KIND or packet['request_digest'] != _digest(q)
            or b['store'] != str(store) or b['backend'] != backend
            or b['evidence_repo'] != str(evidence_repo)
            or b['project_id'] != snapshot['project_id'] or q['project_id'] != snapshot['project_id']
            or q['actor_id'] != b['actor']):
        raise ValueError('saved request does not match Store, evidence root or request bytes')
    if packet['action'] == 'grant':
        _grant_shape(q, b)
    elif packet['action'] == 'end':
        original = packet['grant_request']
        old = _grant_shape(original, b)
        if (snapshot['receipts'].get(original['transition_id'], {}).get('result') != 'applied'
                or snapshot['receipts'].get(original['transition_id'], {}).get('request_digest') != _digest(original)
                or q['changes'] != [{'op': 'remove_permission', 'target': old['target'],
                    'permission_id': old['permission_id'], 'expected': old['value']}]
                or q['source_refs'] != original['source_refs']):
            raise ValueError('end must reference the exact committed helper grant')
    else:
        raise ValueError('unknown permission action')
    return packet


def _save(store, backend, packet):
    directory = (store if backend == 'text' else store.parent) / 'transition-requests'
    directory.mkdir(exist_ok=True)
    path = directory / (packet['request']['transition_id'].removeprefix('transition:') + '-permission.json')
    with path.open('x', encoding='utf-8') as f:
        json.dump(packet, f, ensure_ascii=False, indent=2)
        f.write('\n'); f.flush(); os.fsync(f.fileno())
    return path


def _request(s, actor, summary, change, refs):
    tid = 'transition:' + str(uuid4())
    return {'format_version': s['format_version'], 'project_id': s['project_id'],
        'transition_id': tid, 'actor_id': actor, 'base_revision': s['revision'],
        'intent': summary, 'source_refs': refs, 'preconditions': [], 'changes': [change],
        'thread_entries': [{'id': 'thread:' + tid, 'works': [change['target']],
            'kind': 'permission_changed', 'summary': summary, 'why': summary,
            'occurred_at': None, 'recorded_at': datetime.now(timezone.utc).isoformat(),
            'actor': actor, 'source_refs': refs, 'transition_id': tid}]}


def manage_permission(store_path, *, backend, evidence_repo, actor=None, work=None,
                      parent=None, endpoint=None, reason=None, owner_source=None,
                      owner_instruction=None, reviewed_owner_permission=False,
                      end_grant=None, resume=None, prepare_only=False, assessments=None):
    store, repo = Path(store_path), Path(evidence_repo)
    if not store.is_absolute() or not repo.is_absolute():
        raise ValueError('Store and evidence root must be absolute paths')
    store, repo = store.resolve(), repo.resolve()
    s = _state(store, backend)
    if resume is not None:
        if any(v is not None for v in (actor, work, parent, endpoint, reason, owner_source, owner_instruction, end_grant)):
            raise ValueError('resume cannot replace saved inputs')
        path = Path(resume).resolve()
        packet = _read_packet(path, store, backend, repo, s)
    elif end_grant is not None:
        if not actor or any(v is not None for v in (work, parent, endpoint, reason, owner_source, owner_instruction)):
            raise ValueError('end needs the original grant file and its own actor only')
        old = _read_packet(end_grant, store, backend, repo, s)
        if old['action'] != 'grant' or old['binding']['actor'] != actor:
            raise ValueError('only the same actor can end its helper grant')
        original = old['request']; c = original['changes'][0]
        receipt = s['receipts'].get(original['transition_id'], {})
        if receipt.get('result') != 'applied' or receipt.get('request_digest') != _digest(original):
            raise ValueError('original grant was not committed exactly')
        current = next((p for p in s['map']['permissions'] if p['id'] == c['permission_id']), None)
        if current is None:
            return {'result': {'result': 'already_absent'}, 'readback': {'status': 'confirmed',
                'revision': s['revision'], 'permission_id': c['permission_id']}}
        if current != c['value']:
            raise ValueError('grant changed; do not remove a different current permission')
        q = _request(s, actor, '今回の親変更用Permissionだけを終了',
            {'op': 'remove_permission', 'target': c['target'], 'permission_id': c['permission_id'], 'expected': current},
            original['source_refs'])
        packet = {'kind': KIND, 'action': 'end', 'binding': old['binding'],
            'request': q, 'request_digest': _digest(q), 'grant_request': original}
        path = _save(store, backend, packet)
    else:
        if (not all(isinstance(v, str) and v.strip() for v in (actor, work, parent, reason))
                or endpoint not in ('work', 'parent') or bool(owner_source) == bool(owner_instruction)
                or not reviewed_owner_permission):
            raise ValueError('grant needs own actor, Work/new Parent, endpoint, reason and reviewed actual Owner source or instruction')
        key, dest = s['aliases'].get(work, work), s['aliases'].get(parent, parent)
        if key not in s['map']['members'] or dest not in s['map']['members'] or key == dest:
            raise ValueError('Work and new Parent must be distinct current Works')
        b = {'store': str(store), 'backend': backend, 'evidence_repo': str(repo),
            'project_id': s['project_id'], 'work': key, 'parent': dest, 'endpoint': endpoint, 'actor': actor}
        if owner_instruction:
            raw = ('Operator-attributed Owner instruction for this reparent scope:\n\n' + owner_instruction + '\n').encode('utf-8')
            ref = 'repo:docs/work-management/evidence/owner_reparent_' + hashlib.sha256(raw).hexdigest() + '.txt'
            source = repo / ref[5:]; source.parent.mkdir(parents=True, exist_ok=True)
            try:
                with source.open('xb') as f: f.write(raw)
            except FileExistsError:
                if source.read_bytes() != raw: raise ValueError('Owner evidence contains different bytes')
        else:
            ref = owner_source
        target = key if endpoint == 'work' else dest
        grant = {'id': 'permission:reparent:' + str(uuid4()), 'targets': [target],
            'grantee': actor, 'grantor_ref': 'principal:owner', 'effect': 'allow',
            'operations': ['reparent', 'remove_permission'], 'constraints': [
                'reparent_work:' + key, 'reparent_parent:' + dest, 'remove_permission:this_grant_only'],
            'source_refs': [ref]}
        q = _request(s, actor, reason, {'op': 'set_permission', 'target': target,
            'permission_id': grant['id'], 'expected': None, 'value': grant}, [ref])
        # Same attribution/source boundary as the existing Owner grant route.
        review = review_owner_permission(q, repo)
        verify_permission_sources(review)
        packet = {'kind': KIND, 'action': 'grant', 'binding': b, 'request': q,
            'request_digest': _digest(q), 'owner_sources': list(review.bindings)}
        path = _save(store, backend, packet)
    q = packet['request']
    report = {'request_path': str(path), 'transition_id': q['transition_id'],
              'permission_id': q['changes'][0]['permission_id']}
    if prepare_only:
        report['result'] = {'result': 'prepared', 'base_revision': q['base_revision']}
        return report
    if packet['action'] == 'grant':
        if not reviewed_owner_permission:
            raise ValueError('explicit Owner attribution review required for grant/resume')
        if q['transition_id'] not in s['receipts']:
            review = review_owner_permission(q, repo)
            if list(map(list, review.bindings)) != list(map(list, packet['owner_sources'])):
                raise ValueError('Owner source changed since preparation')
    # apply_result consumes an ordinary Transition, not this recovery packet.
    # Keep the raw request next to it for the existing generic recovery tools.
    raw_path = path.with_name(path.stem + '-transition.json')
    raw = (json.dumps(q, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    try:
        with raw_path.open('xb') as f: f.write(raw); f.flush(); os.fsync(f.fileno())
    except FileExistsError:
        if raw_path.read_bytes() != raw: raise ValueError('saved Transition differs from recovery packet')
    report['transition_path'] = str(raw_path)
    report['result'] = apply_result(store, raw_path, backend=backend, evidence_repo=repo,
        reviewed_owner_permission=packet['action'] == 'grant', context_assessments=assessments)
    if report['result'].get('result') == 'applied':
        try:
            latest = _state(store, backend); c = q['changes'][0]
            current = next((p for p in latest['map']['permissions'] if p['id'] == c['permission_id']), None)
            same = current == c['value'] if packet['action'] == 'grant' else current is None
            report['readback'] = {'status': 'confirmed' if same else 'changed_or_unavailable', 'revision': latest['revision']}
        except (OSError, ValueError, RuntimeError, StoreError) as exc:
            report['readback'] = {'status': 'unknown', 'reason': str(exc)}
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--store', type=Path, required=True)
    p.add_argument('--backend', choices=('sqlite', 'text'), required=True)
    p.add_argument('--evidence-repo', type=Path, required=True)
    for name in ('actor', 'work', 'parent', 'reason', 'owner-source', 'owner-instruction'):
        p.add_argument('--' + name)
    p.add_argument('--endpoint', choices=('work', 'parent'))
    p.add_argument('--end-grant', type=Path)
    p.add_argument('--resume', type=Path)
    p.add_argument('--prepare-only', action='store_true')
    p.add_argument('--reviewed-owner-permission', action='store_true')
    p.add_argument('--assessments', type=Path)
    a = vars(p.parse_args())
    store = a.pop('store'); assessment_path = a.pop('assessments')
    try:
        r = manage_permission(store, **a, assessments=json.loads(assessment_path.read_text(encoding='utf-8')) if assessment_path else None)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, StoreError) as exc:
        p.exit(2, str(exc) + '\n')
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r['result'].get('result') == 'prepared' or r.get('readback', {}).get('status') == 'confirmed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
