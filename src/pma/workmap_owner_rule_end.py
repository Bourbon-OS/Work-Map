"""Review one explicit Owner instruction to end an existing bounded rule.

This is a narrow authority input to the existing remove_action_rule operation,
not a grant, automatic expiry, text approval classifier, or authentication.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from pma.workmap_contract import inspect_work, rule_requires_disposition
from pma.workmap_core import TransitionConflict, TransitionDenied, _digest, _nonempty
from pma.workmap_owner_permission import _read, verify_permission_sources


@dataclass(frozen=True)
class ReviewedOwnerRuleEnd:
    request_digest: str
    repo: Path
    bindings: tuple


def _shape(request):
    changes, entries = request['changes'], request['thread_entries']
    if len(changes) != 1 or changes[0]['op'] != 'remove_action_rule' or len(entries) != 1:
        raise ValueError('one rule removal only')
    change, entry = changes[0], entries[0]
    old = change['expected']
    if (not isinstance(old, dict) or old['id'] != change['action_rule_id']
            or old['targets'] != [change['target']]
            or not _nonempty(old.get('clears_when')) or not _nonempty(old.get('applies_to'))
            or entry['kind'] != 'action_rule_changed' or entry['works'] != [change['target']]
            or not _nonempty(entry.get('why'))):
        raise ValueError('exact existing single-Work rule and completion basis required')
    refs = request['source_refs']
    if (not isinstance(refs, list) or not refs or any(not isinstance(r, str) for r in refs)
            or not set(old['refs']) <= set(refs) or not set(refs) <= set(entry['source_refs'])):
        raise ValueError('original rule sources must be retained')
    def owner_ref(ref):
        return ref.startswith('repo:docs/work-management/evidence/owner_') and ref.endswith('.txt')
    owners = [r for r in refs if owner_ref(r) and r not in old['refs']]
    evidence = entry.get('evidence_refs', [])
    if (not owners or not isinstance(evidence, list) or not evidence
            or any(not isinstance(r, str) for r in evidence) or not set(evidence) <= set(refs)
            or not any(r not in old['refs'] and not owner_ref(r) for r in evidence)):
        raise ValueError('actual Owner instruction and completion evidence required')
    return change, entry, list(dict.fromkeys(refs))


def bind_owner_rule_end_basis(request, repo):
    """Bind documents when preparing; do not rebind a saved request on resume."""
    bound = copy.deepcopy(request)
    try:
        _, entry, refs = _shape(bound)
        if entry.get('consequence'):
            raise ValueError('prepared rule-end basis already exists')
        entry['consequence'] = json.dumps({'owner_rule_end_sources': [
            {'ref': r, 'sha256': hashlib.sha256(_read(Path(repo).resolve(), r)).hexdigest()}
            for r in refs]}, ensure_ascii=False, sort_keys=True)
        return bound
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
        raise TransitionDenied('one Owner rule end and readable completion sources must be prepared') from exc


def review_owner_rule_end(request, repo):
    """Operator attests attribution, completion, applicability and exact scope."""
    try:
        _, entry, refs = _shape(request)
        basis = json.loads(entry['consequence'])
        if set(basis) != {'owner_rule_end_sources'} or not isinstance(basis['owner_rule_end_sources'], list):
            raise ValueError('prepared source bindings required')
        sources = basis['owner_rule_end_sources']
        if (len(sources) != len(refs) or any(set(v) != {'ref', 'sha256'} for v in sources)
                or [v['ref'] for v in sources] != refs):
            raise ValueError('source bindings differ')
        repo = Path(repo).resolve()
        for source in sources:
            if hashlib.sha256(_read(repo, source['ref'])).hexdigest() != source['sha256']:
                raise ValueError('prepared evidence changed')
        return ReviewedOwnerRuleEnd(_digest(request), repo,
            tuple((v['ref'], v['sha256']) for v in sources))
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
        raise TransitionDenied('exact Owner rule end and unchanged prepared sources must be reviewed') from exc


def verify_rule_end_sources(review):
    try:
        verify_permission_sources(review)
    except TransitionDenied as exc:
        raise TransitionDenied('rule-end source changed after review') from exc


def validate_owner_rule_end(snapshot, request, review, operation_review=None):
    if not isinstance(review, ReviewedOwnerRuleEnd) or review.request_digest != _digest(request):
        raise TransitionDenied('Owner rule-end review does not match this request')
    verify_rule_end_sources(review)
    change, _, _ = _shape(request)
    target, old = change['target'], change['expected']
    matches = [r for r in snapshot['map']['action_rules'] if r['id'] == change['action_rule_id']]
    if matches != [old]:
        raise TransitionConflict('reviewed Action Rule differs from its expected value')
    from pma.workmap_decision_context import require_operation_context
    require_operation_context(snapshot, request, operation_review)
    context = inspect_work(snapshot, target)
    chain = {w['work_key'] for w in context['parent_chain']}
    operations = {'remove_action_rule', 'append_thread'}
    if any(chain.intersection(p['targets']) and p['grantee'] == request['actor_id']
           and operations.intersection(p['operations']) and p['effect'] != 'allow'
           for p in snapshot['map']['permissions']):
        raise TransitionDenied('resolve the existing denial before ending the rule')
    if any(r['id'] != old['id'] and chain.intersection(r['targets'])
           and rule_requires_disposition(r, operations) for r in snapshot['map']['action_rules']):
        raise TransitionDenied('another applicable Action Rule still needs disposition')
    return json.dumps({'old_action_rule': old, 'new_action_rule': None,
                       'owner_rule_end_sources': [{'ref': r, 'sha256': h} for r, h in review.bindings]},
                      ensure_ascii=False, sort_keys=True)
