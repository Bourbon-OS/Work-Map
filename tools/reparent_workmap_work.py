"""Move an existing Work and its subtree through the canonical Core.

Supply meaning and sources, not internal IDs or a hand-built Transition.
Prepare-only saves the same ordinary request used by resume. It grants no
authority and does not assess unknown impact. Assessments are explicit local
operator inputs in the existing operation-review format.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from pma.workmap_contract import validate_snapshot, child_address_prefix, subtree_addresses
from pma.workmap_store_factory import open_store
from pma.workmap_store import StoreError
from tools.apply_workmap_transition import apply_result, bind_request, load_request


def reparent_work(store_path, *, backend, actor=None, work=None, parent=None,
                  to_root=False, reason=None, sources=(), resume=None,
                  prepare_only=False, assessments=None, evidence_repo=None):
    store_path = Path(store_path).resolve()
    snapshot = validate_snapshot(open_store(store_path, backend).read().snapshot)
    if snapshot['metadata'].get('store_role') != 'canonical_current':
        raise ValueError('selected Store is not canonical current')
    if resume is None:
        if not all(isinstance(v, str) and v.strip() for v in (actor, work, reason)) or not sources:
            raise ValueError('actor, Work, reason and source references are required')
        if to_root == (parent is not None):
            raise ValueError('choose Parent or explicit return to root')
        target = snapshot['aliases'].get(work, work)
        destination = None if to_root else snapshot['aliases'].get(parent, parent)
        if target not in snapshot['map']['members'] or (destination is not None and destination not in snapshot['map']['members']):
            raise ValueError('target and Parent must be current Works')
        parents = {r['child']: r['parent'] for r in snapshot['map']['relations'] if r['kind'] == 'parent'}
        prefix = 'W' if destination is None else child_address_prefix(snapshot['display_ids'][destination])
        number = 1
        while True:
            mapped = subtree_addresses(snapshot, target, prefix + str(number))
            if not any(v in snapshot['aliases'] for v in mapped.values()):
                break
            number += 1
        tid = 'transition:' + str(uuid4())
        refs = list(dict.fromkeys(sources))
        request = {'format_version': snapshot['format_version'], 'project_id': snapshot['project_id'],
            'transition_id': tid, 'actor_id': actor, 'base_revision': snapshot['revision'],
            'intent': reason, 'source_refs': refs, 'preconditions': [],
            'changes': [{'op': 'reparent', 'target': target, 'expected_parent': parents.get(target),
                         'new_parent': destination, 'new_display_id': mapped[target]}],
            'thread_entries': [{'id': 'rationale:' + tid, 'works': [target], 'kind': 'reparent',
                'summary': snapshot['works'][target]['name'] + ': Parent変更', 'why': reason,
                'occurred_at': None, 'recorded_at': datetime.now(timezone.utc).isoformat(),
                'actor': actor, 'source_refs': refs, 'transition_id': tid}]}
        directory = (store_path if backend == 'text' else store_path.parent) / 'transition-requests'
        directory.mkdir(exist_ok=True)
        request_path = directory / (tid.removeprefix('transition:') + '.json')
        saved = bind_request(request, store_path, backend, evidence_repo)
        with request_path.open('x', encoding='utf-8') as f:
            json.dump(saved, f, ensure_ascii=False, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
    else:
        if to_root or any(v is not None for v in (actor, work, parent, reason)) or sources:
            raise ValueError('resume cannot replace saved meaning or premises')
        request_path = Path(resume).resolve()
        request = load_request(store_path, request_path, backend, evidence_repo)
        if len(request.get('changes', [])) != 1 or request['changes'][0].get('op') != 'reparent':
            raise ValueError('resume requires one saved reparent request')
    report = {'request_path': str(request_path), 'transition_id': request['transition_id']}
    if prepare_only:
        report['result'] = {'result': 'prepared', 'base_revision': request['base_revision']}
        return report
    report['result'] = apply_result(store_path, request_path, backend=backend,
        context_assessments=assessments, evidence_repo=evidence_repo)
    if report['result'].get('result') == 'applied':
        # Re-read current data, never present the receipt's past result as current.
        try:
            current = validate_snapshot(open_store(store_path, backend).read().snapshot)
            changes = [h for h in current['display_id_history'] if h['transition_id'] == request['transition_id']]
            parents = {r['child']: r['parent'] for r in current['map']['relations'] if r['kind'] == 'parent'}
            change = request['changes'][0]
            same = bool(changes) and parents.get(change['target']) == change['new_parent']
            same = same and current['metadata'].get('store_role') == 'canonical_current'
            same = same and all(current['display_ids'].get(h['work_key']) == h['new_display_id']
                                and current['aliases'].get(h['old_display_id']) == h['work_key'] for h in changes)
            report['readback'] = {'status': 'confirmed' if same else 'changed_or_unavailable',
                'revision': current['revision'], 'addresses': changes}
        except (OSError, ValueError, RuntimeError, StoreError) as exc:
            report['readback'] = {'status': 'unknown', 'reason': str(exc)}
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--store', type=Path, required=True)
    p.add_argument('--backend', choices=('text', 'sqlite'), required=True)
    for field in ('actor', 'work', 'parent', 'reason'):
        p.add_argument('--' + field)
    p.add_argument('--to-root', action='store_true')
    p.add_argument('--source', action='append', default=[])
    p.add_argument('--resume', type=Path)
    p.add_argument('--prepare-only', action='store_true')
    p.add_argument('--evidence-repo', type=Path, help='absolute root for the selected Map documents')
    p.add_argument('--assessments', type=Path, help='explicit reviewed unknown impact; never a Permission grant')
    a = p.parse_args()
    if not a.store.is_absolute(): p.error('Store path must be absolute')
    try:
        result = reparent_work(a.store, backend=a.backend, actor=a.actor, work=a.work,
            parent=a.parent, to_root=a.to_root, reason=a.reason, sources=a.source,
            resume=a.resume, prepare_only=a.prepare_only,
            assessments=json.loads(a.assessments.read_text(encoding='utf-8')) if a.assessments else None,
            evidence_repo=a.evidence_repo)
    except (OSError, ValueError, RuntimeError, KeyError, StoreError) as exc:
        p.exit(2, str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['result'].get('result') == 'prepared' or result.get('readback', {}).get('status') == 'confirmed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
