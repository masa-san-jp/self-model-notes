#!/usr/bin/env python3
"""Creator-scoped derived memory in an external, local bare Git store.

Raw entities stay in the existing external-local profile. Only closed, validated
feedback records enter this store; no remote operation is performed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from .kb import ROOT, discover_entities, validate_entities
    from .profile_root import resolve_profile_root, validate_external_directory, atomic_write_text
    from .export_signals import _consent_denials, _source_commit, build_signal_export, validate_signal_export
except ImportError:
    from kb import ROOT, discover_entities, validate_entities
    from profile_root import resolve_profile_root, validate_external_directory, atomic_write_text
    from export_signals import _consent_denials, _source_commit, build_signal_export, validate_signal_export

OWNER = 'self-model-notes'
CONTRACT = 'creative-feedback/v1'
STORE = 'self-model-knowledge/v1'
POLICY = 'creative-feedback-policy/v1'
REF = 'refs/heads/knowledge'
ID = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
SHA = re.compile(r'^[0-9a-f]{40}$')
SECRET = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9]{20,}|-----BEGIN .*PRIVATE KEY|Bearer\s+\S+)', re.I)
FIELDS = set('contract_version record_id revision origin_instance_id creator_id collection_id subject source_ref source_sha256 created_at epistemic_status lifecycle choice context summary confidence alternative_explanations counterevidence producer_kind run_id supersedes'.split())


class MemoryError(ValueError):
    """Stable errors intentionally exclude private input values and paths."""


def require(condition, code):
    if not condition:
        raise MemoryError(code)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str) + '\n').encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def git(root, *args, data=None, env=None, optional=False):
    result = subprocess.run(['git', '-C', str(root), *args], input=data, capture_output=True, env=env)
    if result.returncode and not optional:
        raise MemoryError('GIT_OPERATION_FAILED')
    return result.stdout.decode().strip() if result.returncode == 0 else None


def head(root):
    return git(root, 'rev-parse', '--verify', REF, optional=True)


def _path(root):
    raw = Path(root)
    require(raw.is_absolute() and all(not p.is_symlink() for p in [raw, *raw.parents]), 'STORE_PATH_INVALID')
    return validate_external_directory(raw)


def _tree(root, commit):
    entries = git(root, 'ls-tree', '-r', commit).splitlines()
    names = [entry.split('\t', 1)[1] for entry in entries]
    require(all(name == 'knowledge/store.json' or re.fullmatch(r'knowledge/(records|receipts)/[a-z0-9-]+/[0-9]+\.json', name) for name in names), 'STORE_PATH_NOT_ALLOWED')
    require(all(line.startswith('100644 blob ') for line in entries), 'STORE_ENTRY_INVALID')
    objects = [entry.split('\t', 1)[0].split()[2] for entry in entries]
    # Read one immutable snapshot in one process; reopening Git per revision makes
    # cumulative history unnecessarily expensive. Validate every batch header.
    result = subprocess.run(['git', '-C', str(root), 'cat-file', '--batch'],
                            input=('\n'.join(objects) + '\n').encode(), capture_output=True)
    require(result.returncode == 0, 'GIT_OPERATION_FAILED')
    data, offset, tree = result.stdout, 0, {}
    for name, oid in zip(names, objects):
        end = data.find(b'\n', offset)
        header = data[offset:end].decode().split()
        require(len(header) == 3 and header[:2] == [oid, 'blob'], 'GIT_OBJECT_INVALID')
        size = int(header[2])
        start = end + 1
        require(data[start + size:start + size + 1] == b'\n', 'GIT_OBJECT_INVALID')
        tree[name] = json.loads(data[start:start + size])
        offset = start + size + 1
    require(offset == len(data), 'GIT_OBJECT_INVALID')
    return tree


def _commit(root, parent, updates, message):
    # An isolated index and update-ref CAS make interruption/parallel writers safe.
    # A losing writer leaves only unreachable objects, never partial canonical files.
    with tempfile.TemporaryDirectory(prefix='self-memory-index-') as temp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp) / 'index'),
                   GIT_AUTHOR_NAME='Self Model memory', GIT_AUTHOR_EMAIL='memory@localhost',
                   GIT_COMMITTER_NAME='Self Model memory', GIT_COMMITTER_EMAIL='memory@localhost')
        git(root, 'read-tree', parent if parent else '--empty', env=env)
        for name, value in sorted(updates.items()):
            blob = git(root, 'hash-object', '-w', '--stdin', data=encoded(value), env=env)
            git(root, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{name}', env=env)
        tree = git(root, 'write-tree', env=env)
        commit = git(root, 'commit-tree', tree, *(['-p', parent] if parent else []), '-m', message, env=env)
        result = subprocess.run(['git', '-C', str(root), 'update-ref', REF, commit, parent or '0' * 40], capture_output=True)
        require(result.returncode == 0, 'PARENT_CONFLICT')
        require(git(root, 'rev-parse', f'{commit}^{{tree}}') == tree, 'COMMIT_VERIFICATION_FAILED')
        return commit


def init_store(root, profile_root, *, creator, subject, collection, instance):
    root = _path(root)
    layout = resolve_profile_root(profile_root)
    require(not root.is_relative_to(layout.root) and not layout.root.is_relative_to(root), 'RAW_STORE_OVERLAP')
    for value in (creator, collection, instance):
        require(isinstance(value, str) and ID.fullmatch(value), 'IDENTITY_INVALID')
    require(subject in layout.subject_ids, 'SUBJECT_MISMATCH')
    require(not root.exists(), 'STORE_ALREADY_EXISTS')
    root.mkdir(parents=True)
    git(root, 'init', '--bare', '--quiet')
    git(root, 'symbolic-ref', 'HEAD', REF)
    identity = dict(contract_version=STORE, owner_repository=OWNER, creator_id=creator,
                    subject=subject, profile_id=layout.profile['profile_id'], collection_id=collection,
                    origin_instance_id=instance, storage_scope='local-git-derived-only')
    commit = _commit(root, None, {'knowledge/store.json': identity}, 'Initialize derived knowledge identity')
    return {'status': 'COMMITTED', 'knowledge_commit': commit, 'contract_version': STORE}


def open_store(root, profile_root, *, creator, subject, collection, snapshot=None):
    root = _path(root)
    require(root.is_dir() and git(root, 'rev-parse', '--is-bare-repository') == 'true', 'BARE_STORE_REQUIRED')
    layout = resolve_profile_root(profile_root)
    require(not root.is_relative_to(layout.root) and not layout.root.is_relative_to(root), 'RAW_STORE_OVERLAP')
    commit = snapshot or head(root)
    require(isinstance(commit, str) and SHA.fullmatch(commit), 'SNAPSHOT_REQUIRED')
    # A snapshot must belong to the selected store's history, not an arbitrary object.
    ancestor = subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', commit, REF], capture_output=True)
    require(ancestor.returncode == 0, 'SNAPSHOT_NOT_IN_STORE')
    tree = _tree(root, commit)
    identity = tree.get('knowledge/store.json', {})
    require(identity.get('contract_version') == STORE and identity.get('owner_repository') == OWNER, 'STORE_CONTRACT_INVALID')
    require((creator, subject, collection, layout.profile['profile_id']) ==
            (identity.get('creator_id'), identity.get('subject'), identity.get('collection_id'), identity.get('profile_id')),
            'CREATOR_SCOPE_MISMATCH')
    require(subject in layout.subject_ids, 'SUBJECT_MISMATCH')
    require(identity.get('storage_scope') == 'local-git-derived-only', 'STORE_SCOPE_INVALID')
    return root, layout, commit, tree, identity


def _sources(layout):
    require(all(not p.is_symlink() for p in layout.entity_root.rglob('*')), 'PROFILE_SYMLINK')
    entities = discover_entities(layout.entity_root)
    require(not validate_entities(entities, root=layout.root), 'PROFILE_VALIDATION_FAILED')
    return entities, {entity.id: entity for entity in entities}


def source_hash(source):
    # Fingerprint the selected canonical Source metadata, never copy its raw body.
    return digest(encoded(source.meta))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def raw_quotes(entity):
    for value in entity.meta.get('raw_voice') or []:
        if isinstance(value, dict) and isinstance(value.get('text'), str):
            yield value['text']
        elif isinstance(value, str):
            yield value


def validate_record(record, identity, entities=None):
    require(isinstance(record, dict) and set(record) == FIELDS, 'RECORD_FIELDS_INVALID')
    require(record['contract_version'] == CONTRACT, 'RECORD_CONTRACT_INVALID')
    for field in ('record_id', 'origin_instance_id', 'creator_id', 'collection_id', 'run_id'):
        require(isinstance(record[field], str) and ID.fullmatch(record[field]), 'RECORD_ID_INVALID')
    require(type(record['revision']) is int and record['revision'] >= 1, 'REVISION_INVALID')
    for field in ('creator_id', 'collection_id', 'subject', 'origin_instance_id'):
        require(record[field] == identity[field], 'RECORD_SCOPE_MISMATCH')
    require(isinstance(record['source_ref'], str) and re.fullmatch(r'source/[a-z0-9]+(?:-[a-z0-9]+)*', record['source_ref']), 'SOURCE_REF_INVALID')
    require(isinstance(record['source_sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', record['source_sha256']), 'SOURCE_HASH_INVALID')
    try:
        stamp = datetime.fromisoformat(record['created_at'])
        require(stamp.tzinfo is not None, 'TIMESTAMP_INVALID')
    except (ValueError, TypeError):
        raise MemoryError('TIMESTAMP_INVALID') from None
    require(record['epistemic_status'] in ('observed', 'inferred', 'proposed', 'simulated', 'unknown'), 'EPISTEMIC_INVALID')
    require(record['lifecycle'] in ('accepted', 'candidate', 'rejected', 'revoked'), 'LIFECYCLE_INVALID')
    require(record['choice'] in ('adopt', 'reject', 'revise', 'evaluate', None), 'CHOICE_INVALID')
    require(record['producer_kind'] in ('human', 'agent', 'tool'), 'PRODUCER_INVALID')
    if record['epistemic_status'] == 'observed':
        require(record['producer_kind'] == 'human' and record['choice'] is not None, 'HUMAN_OBSERVATION_REQUIRED')
    if record['epistemic_status'] == 'unknown':
        require(record['choice'] is None and record['confidence'] == 'unknown', 'SILENCE_IS_NOT_PREFERENCE')
    require(record['confidence'] in ('unknown', 'low', 'medium', 'high'), 'CONFIDENCE_INVALID')
    for field in ('summary', 'context'):
        require(isinstance(record[field], str) and bool(record[field].strip()), 'DERIVED_TEXT_REQUIRED')
    for field in ('alternative_explanations', 'counterevidence'):
        require(isinstance(record[field], list) and all(isinstance(item, str) and item.strip() for item in record[field]), 'EVIDENCE_INVALID')
    require(len(set(record['alternative_explanations'])) >= 2, 'ALTERNATIVES_REQUIRED')
    require(record['supersedes'] == (record['revision'] - 1 if record['revision'] > 1 else None), 'SUPERSESSION_INVALID')
    text = '\n'.join(_strings(record))
    require(not SECRET.search(text), 'RESTRICTED_CONTENT')
    if entities:
        for entity in entities:
            for raw in raw_quotes(entity):
                require(not raw or raw not in text, 'RAW_VOICE_FORBIDDEN')


def _availability(record, by_id, subject, purpose, operation='export-signals', today=None):
    source = by_id.get(record['source_ref'])
    if source is None or source.type != 'source' or source.meta.get('subject') != subject:
        return 'SOURCE_UNAVAILABLE'
    if source_hash(source) != record['source_sha256']:
        return 'SOURCE_CHANGED'
    if _consent_denials(source, purpose, operation, today or date.today()):
        return 'CONSENT_DENIED'
    person = by_id.get(subject)
    if person is None or purpose not in person.meta.get('allowed_purposes', []) or purpose in person.meta.get('prohibited_purposes', []):
        return 'PURPOSE_DENIED'
    for ref in person.meta.get('consent_refs', []):
        consent_source = by_id.get(ref)
        if consent_source is None or _consent_denials(consent_source, purpose, operation, today or date.today()):
            return 'CONSENT_DENIED'
    return 'ACTIVE'


def prepare(root, profile_root, record, *, creator, subject, collection, purpose='artistic-research'):
    root, layout, commit, tree, identity = open_store(root, profile_root, creator=creator, subject=subject, collection=collection)
    entities, by_id = _sources(layout)
    validate_record(record, identity, entities)
    if record['lifecycle'] != 'revoked':
        require(_availability(record, by_id, subject, purpose, 'derive') == 'ACTIVE', 'SOURCE_OR_CONSENT_INVALID')
    else:
        # Withdrawal tombstones may be stored after consent is withdrawn, but may
        # not introduce new text or facts. commit_record verifies their predecessor.
        require(record['revision'] > 1, 'REVOCATION_REQUIRES_HISTORY')
    return {'contract_version': 'creative-feedback-candidate/v1', 'parent': commit,
            'record': record, 'content_sha256': digest(encoded(record)), 'policy_version': POLICY}


def _record_path(record):
    return f"knowledge/records/{record['record_id']}/{record['revision']}.json"


def _latest(tree, identity):
    records = {}
    entries = [(name, value) for name, value in tree.items() if name.startswith('knowledge/records/')]
    for name, record in sorted(entries, key=lambda item: (item[0].split('/')[2], int(item[0].split('/')[-1][:-5]))):
        if not name.startswith('knowledge/records/'):
            continue
        validate_record(record, identity)
        require(name == _record_path(record), 'RECORD_PATH_MISMATCH')
        prior = records.get(record['record_id'])
        require(record['revision'] == (prior['revision'] + 1 if prior else 1), 'REVISION_GAP')
        records[record['record_id']] = record
    return records


def index(root, profile_root, *, creator, subject, collection, purpose='artistic-research', snapshot=None):
    root, layout, commit, tree, identity = open_store(root, profile_root, creator=creator, subject=subject, collection=collection, snapshot=snapshot)
    entities, by_id = _sources(layout)
    records = []
    for record in _latest(tree, identity).values():
        validate_record(record, identity, entities)
        status = _availability(record, by_id, subject, purpose)
        if record['lifecycle'] != 'accepted':
            status = record['lifecycle'].upper()
        records.append({'record_id': record['record_id'], 'revision': record['revision'],
                        'status': status, 'epistemic_status': record['epistemic_status'],
                        'content_sha256': digest(encoded(record)), 'payload_ref': _record_path(record)})
    result = {'contract_version': 'creative-feedback-index/v1', 'knowledge_commit': commit,
              'creator_id': creator, 'collection_id': collection, 'purpose': purpose, 'records': records}
    # Cache is never trusted for export: every read checks current source/consent.
    cache = root / 'derived-index.json'
    require(not cache.is_symlink(), 'INDEX_PATH_INVALID')
    atomic_write_text(cache, encoded(result).decode())
    return result


def commit_record(root, profile_root, record, *, creator, subject, collection, expected_parent, operation_id, purpose='artistic-research'):
    require(isinstance(operation_id, str) and ID.fullmatch(operation_id), 'OPERATION_ID_INVALID')
    root, layout, current, tree, identity = open_store(root, profile_root, creator=creator, subject=subject, collection=collection)
    validate_record(record, identity)
    receipt_path = f'knowledge/receipts/{operation_id}/1.json'
    existing = tree.get(receipt_path)
    content_hash = digest(encoded(record))
    if existing:
        require(existing['content_sha256'] == content_hash and existing['run_id'] == record['run_id'], 'OPERATION_CONFLICT')
        return _receipt(root, profile_root, existing, tree, creator, subject, collection, purpose, 'NO_CHANGE')
    require(expected_parent == current, 'PARENT_CONFLICT')
    path = _record_path(record)
    if path in tree:
        require(tree[path] == record, 'REVISION_CONFLICT')
        previous_receipt = next(value for name, value in tree.items() if name.startswith('knowledge/receipts/') and value['content_sha256'] == content_hash)
        return _receipt(root, profile_root, previous_receipt, tree, creator, subject, collection, purpose, 'NO_CHANGE')
    candidate = prepare(root, profile_root, record, creator=creator, subject=subject, collection=collection, purpose=purpose)
    require(candidate['parent'] == current, 'PARENT_CONFLICT')
    previous = _latest(tree, identity).get(record['record_id'])
    require(record['revision'] == (previous['revision'] + 1 if previous else 1), 'REVISION_CONFLICT')
    if record['lifecycle'] == 'revoked':
        require(previous is not None and all(record[key] == previous[key] for key in FIELDS - {'revision', 'supersedes', 'lifecycle', 'run_id', 'created_at'}), 'REVOCATION_CONTENT_CHANGED')
    receipt = {'contract_version': 'creative-feedback-operation/v1', 'operation_id': operation_id,
               'run_id': record['run_id'], 'owner': OWNER, 'collection': collection,
               'target_parent': current, 'accepted_ids': [record['record_id']], 'rejected_ids': [],
               'schema_version': CONTRACT, 'policy_version': POLICY, 'content_sha256': content_hash,
               'payload_ref': path, 'code_commit': _source_commit(ROOT)}
    committed = _commit(root, current, {path: record, receipt_path: receipt}, 'Persist validated creative feedback')
    require(_tree(root, committed)[path] == record, 'COMMIT_VERIFICATION_FAILED')
    return _receipt(root, profile_root, receipt, None, creator, subject, collection, purpose, 'COMMITTED', committed)


def _receipt(root, profile_root, receipt, tree, creator, subject, collection, purpose, status, commit=None):
    # Locate the original introducing commit, even after later appends/restarts.
    if commit is None:
        commits = git(root, 'rev-list', '--reverse', REF, '--', f"knowledge/receipts/{receipt['operation_id']}/1.json").splitlines()
        require(bool(commits), 'RECEIPT_COMMIT_MISSING')
        commit = commits[0]
    result = {key: value for key, value in receipt.items() if key not in ('payload_ref', 'content_sha256', 'code_commit')}
    result.update(contract_version='knowledge-write-receipt/v1', target_commit=commit, status=status,
                  reason='ALREADY_APPLIED' if status == 'NO_CHANGE' else 'VALIDATED', index_commit=None, index_hash=None)
    try:
        indexed = index(root, profile_root, creator=creator, subject=subject, collection=collection, purpose=purpose)
        result.update(index_commit=indexed['knowledge_commit'], index_hash=digest(encoded(indexed)))
    except (OSError, ValueError):
        result.update(status='INDEX_PENDING', reason='knowledge commit retained; rerun index')
    return result


def retrieve(root, profile_root, *, creator, subject, collection, purpose='artistic-research', snapshot=None, query=''):
    root, layout, commit, tree, identity = open_store(root, profile_root, creator=creator, subject=subject, collection=collection, snapshot=snapshot)
    indexed = index(root, profile_root, creator=creator, subject=subject, collection=collection, purpose=purpose, snapshot=commit)
    hits = []
    for item in indexed['records']:
        if item['status'] != 'ACTIVE':
            continue
        record = tree[item['payload_ref']]
        if query and query.casefold() not in (record['summary'] + ' ' + record['context']).casefold():
            continue
        receipt = next((value for name, value in tree.items() if name.startswith('knowledge/receipts/') and value.get('payload_ref') == item['payload_ref']), None)
        require(receipt is not None and SHA.fullmatch(receipt.get('code_commit', '')), 'PRODUCER_RECEIPT_MISSING')
        hits.append(dict(item, record=record, artifact=artifact_record(record, commit, receipt['code_commit']), reason='active source/consent and query match'))
    return {'status': 'FOUND' if hits else ('EMPTY_HISTORY' if not indexed['records'] else 'NOT_APPLICABLE'),
            'knowledge_commit': commit, 'policy_version': POLICY, 'records': hits}


def artifact_record(record, knowledge_commit, code_commit):
    """Owner projection of the shared envelope; canonical content remains here."""
    code = code_commit
    return dict(contract_version='artifact-record/v1', record_id=record['record_id'], revision=record['revision'],
                origin_instance_id=record['origin_instance_id'], creator_id=record['creator_id'],
                owner_repository=OWNER, collection_id=record['collection_id'], kind='creative-feedback',
                payload_schema=CONTRACT, payload_ref=_record_path(record), content_sha256=digest(encoded(record)),
                sources=[dict(source_repository=OWNER, locator=record['source_ref'],
                              content_sha256=record['source_sha256'], code_commit=code,
                              knowledge_commit=None, storage='external-local-source-metadata')],
                derived_from=[], epistemic_status=record['epistemic_status'], lifecycle=record['lifecycle'],
                applicability=dict(context=record['context'], time=record['created_at']),
                rights=dict(policy='consent-gated-derived-only', redistribution=False), access_scope='private', consent_ref=record['source_ref'],
                created_at=record['created_at'], reviewed_at=None, valid_until=None,
                producer=dict(kind=record['producer_kind'], generator_version=POLICY, code_commit=code, run_id=record['run_id']),
                supersedes=[] if record['supersedes'] is None else [dict(record_id=record['record_id'], revision=record['supersedes'])],
                invalidates=[])


def export_memory(root, profile_root, *, creator, subject, collection, purpose='artistic-research', snapshot=None):
    found = retrieve(root, profile_root, creator=creator, subject=subject, collection=collection, purpose=purpose, snapshot=snapshot)
    signals = []
    for hit in found['records']:
        record = hit['record']
        if record['epistemic_status'] != 'observed':
            continue  # Agent hypotheses and absence remain searchable, never personal signals.
        signals.append({'entity_ref': 'creative-feedback/' + record['record_id'], 'kind': 'claim',
                        'layer': 'motivation', 'motivation_direction': 'unknown',
                        'statement': record['summary'], 'certainty': record['confidence'],
                        'scope': 'context-bound', 'conditions': [record['context']],
                        'evidence_refs': [record['source_ref']]})
    result = build_signal_export({'allowed': True, 'subject': subject, 'purpose': purpose,
                                  'source_commit': _source_commit(ROOT), 'signals': signals})
    # Existing boundary stays unchanged: knowledge provenance travels in locators
    # and explicit constraints, while source_commit remains the executable code pin.
    records = {item['record']['record_id']: item['record'] for item in found['records']}
    for signal in result['signals']:
        record = records[signal['entity_id'].split('/', 1)[1]]
        locator = f"self-model-knowledge://{collection}/{found['knowledge_commit']}/{_record_path(record)}"
        signal['source_locator'] = locator
        signal['evidence_locator'] = locator + '#source'
        signal['constraints'] += ['Context-bound choice; no trait or preference promotion.',
                                  'Alternative explanations: ' + '; '.join(record['alternative_explanations']),
                                  'Counterevidence: ' + '; '.join(record['counterevidence']),
                                  'Epistemic status: observed human choice; derived interpretation remains uncertain.']
    require(not validate_signal_export(result), 'SIGNAL_EXPORT_INVALID')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['init', 'prepare', 'validate', 'commit', 'index', 'retrieve', 'invalidate', 'export-signals'])
    parser.add_argument('--store-root', required=True, type=Path)
    parser.add_argument('--profile-root', required=True, type=Path)
    parser.add_argument('--creator', required=True)
    parser.add_argument('--subject', required=True)
    parser.add_argument('--collection', required=True)
    parser.add_argument('--instance')
    parser.add_argument('--record', type=Path)
    parser.add_argument('--expected-parent')
    parser.add_argument('--operation-id')
    parser.add_argument('--snapshot')
    parser.add_argument('--query', default='')
    parser.add_argument('--purpose', default='artistic-research')
    args = parser.parse_args(argv)
    options = dict(creator=args.creator, subject=args.subject, collection=args.collection)
    try:
        if args.command == 'init':
            result = init_store(args.store_root, args.profile_root, **options, instance=args.instance)
        elif args.command in ('prepare', 'validate', 'commit'):
            require(args.record is not None and args.record.is_file() and not args.record.is_symlink(), 'RECORD_FILE_REQUIRED')
            record = json.loads(args.record.read_text())
            if args.command == 'commit':
                result = commit_record(args.store_root, args.profile_root, record, **options, expected_parent=args.expected_parent, operation_id=args.operation_id, purpose=args.purpose)
            else:
                result = prepare(args.store_root, args.profile_root, record, **options, purpose=args.purpose)
        elif args.command == 'export-signals':
            result = export_memory(args.store_root, args.profile_root, **options, purpose=args.purpose, snapshot=args.snapshot)
        elif args.command == 'retrieve':
            result = retrieve(args.store_root, args.profile_root, **options, purpose=args.purpose, snapshot=args.snapshot, query=args.query)
        else:
            result = index(args.store_root, args.profile_root, **options, purpose=args.purpose, snapshot=args.snapshot)
        print(encoded(result).decode(), end='')
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        reason = str(exc) if isinstance(exc, MemoryError) else 'INVALID_INPUT_OR_STORE'
        print(json.dumps({'status': 'CONFLICT' if reason.endswith('_CONFLICT') else 'REJECTED', 'reason': reason}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
