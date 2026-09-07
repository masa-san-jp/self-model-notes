"""AAK-05 acceptance: actual local Git, process restart, revocation and isolation."""
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tools import creative_feedback as memory
from tools.kb import parse_markdown, serialize_markdown
from tools.export_signals import validate_signal_export
from tools.profile_root import resolve_profile_root

ROOT = Path(__file__).resolve().parents[1]


class CreativeFeedbackMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='creative-memory-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.profile = self.root / 'profile-a'
        shutil.copytree(ROOT / 'tests/fixtures/e2e/entities', self.profile / 'entities')
        (self.profile / 'profile.yaml').write_text('contract_version: self-model-profile/v1\nprofile_id: synthetic-a\nsubject_ids: [subject/fixture]\nstorage_scope: external-local\n')
        for path in (self.profile / 'entities/sources').glob('*.md'):
            entity = parse_markdown(path)
            entity.meta['consent']['allowed_operations'].append('derive')
            path.write_text(serialize_markdown(entity))
        self.store = self.root / 'memory-a.git'
        self.options = dict(creator='creator-a', subject='subject/fixture', collection='memory-a')
        self.initial = memory.init_store(self.store, self.profile, **self.options, instance='instance-a')['knowledge_commit']
        self.record = dict(contract_version=memory.CONTRACT, record_id='choice-one', revision=1,
                           origin_instance_id='instance-a', creator_id='creator-a', collection_id='memory-a',
                           subject='subject/fixture', source_ref='source/evidence',
                           source_sha256=memory.source_hash(parse_markdown(self.profile / 'entities/sources/evidence.md')),
                           created_at='2026-09-07T14:00:00+00:00', epistemic_status='observed', lifecycle='accepted',
                           choice='adopt', context='studio: paper shadow prototype', summary='Selected a translucent paper screen for this prototype.',
                           confidence='low', alternative_explanations=['material availability', 'temporary lighting constraint'],
                           counterevidence=['previous prototype used solid board'], producer_kind='human', run_id='run-one', supersedes=None)

    def commit(self, record=None, operation='op-one', parent=None):
        return memory.commit_record(self.store, self.profile, record or self.record, **self.options,
                                    operation_id=operation, expected_parent=parent or memory.head(self.store))

    def cli(self, script, *args):
        return subprocess.run([sys.executable, str(ROOT / 'tools/agent_runtime.py'), str(ROOT / 'tools' / script), *args], cwd=ROOT, text=True, capture_output=True)

    def memory_cli(self, command, *args):
        return self.cli('creative_feedback.py', command, '--store-root', str(self.store), '--profile-root', str(self.profile),
                        '--creator', 'creator-a', '--subject', 'subject/fixture', '--collection', 'memory-a', *args)

    def test_ac1_git_persistence_restart_and_native_signal_export(self):
        candidate = memory.prepare(self.store, self.profile, self.record, **self.options)
        self.assertEqual(self.initial, memory.head(self.store))  # prepare never writes canonical data
        receipt = self.commit()
        self.assertEqual('COMMITTED', receipt['status'])
        self.assertEqual(self.record, json.loads(memory.git(self.store, 'show', receipt['target_commit'] + ':' + receipt['payload_ref'])))
        self.assertEqual(receipt['target_commit'], memory.head(self.store))
        self.assertEqual(self.initial, receipt['target_parent'])
        (self.store / 'derived-index.json').unlink()
        process = self.memory_cli('index')
        self.assertEqual(0, process.returncode, process.stdout + process.stderr)
        process = self.memory_cli('export-signals')
        self.assertEqual(0, process.returncode, process.stdout + process.stderr)
        output = json.loads(process.stdout)
        self.assertEqual([], validate_signal_export(output))
        self.assertEqual(1, output['signal_count'])
        self.assertIn(receipt['target_commit'], output['signals'][0]['source_locator'])
        self.assertEqual([], output['signals'][0]['traits'])
        self.assertEqual([self.record['context']], output['signals'][0]['contexts'])
        self.assertNotEqual(receipt['target_commit'], output['source_commit'])
        native = self.cli('export_signals.py', '--profile-root', str(self.profile), '--knowledge-store-root', str(self.store),
                          '--creator', 'creator-a', '--collection', 'memory-a', '--subject', 'subject/fixture', '--purpose', 'artistic-research')
        self.assertEqual(0, native.returncode, native.stdout + native.stderr)
        self.assertEqual(output['signals'][0]['source_locator'], json.loads(native.stdout)['signals'][0]['source_locator'])

    def test_ac2_inference_simulation_and_silence_never_become_personal_signals(self):
        for number, status in enumerate(('inferred', 'proposed', 'simulated', 'unknown'), 1):
            record = dict(self.record, record_id=f'choice-{number}', epistemic_status=status, producer_kind='agent',
                          choice=None if status == 'unknown' else 'adopt', confidence='unknown' if status == 'unknown' else 'low')
            self.commit(record, operation=f'op-{number}')
        found = memory.retrieve(self.store, self.profile, **self.options)
        self.assertEqual({'inferred', 'proposed', 'simulated', 'unknown'}, {item['record']['epistemic_status'] for item in found['records']})
        self.assertEqual(0, memory.export_memory(self.store, self.profile, **self.options)['signal_count'])
        with self.assertRaisesRegex(memory.MemoryError, 'HUMAN_OBSERVATION'):
            self.commit(dict(self.record, producer_kind='agent'))
        with self.assertRaisesRegex(memory.MemoryError, 'SILENCE_IS_NOT_PREFERENCE'):
            self.commit(dict(self.record, epistemic_status='unknown'))
        with self.assertRaisesRegex(memory.MemoryError, 'ALTERNATIVES_REQUIRED'):
            self.commit(dict(self.record, alternative_explanations=[]))

    def test_ac3_source_correction_recomputes_and_keeps_revision_history(self):
        first = self.commit()
        source = self.profile / 'entities/sources/evidence.md'
        entity = parse_markdown(source)
        entity.meta['reliability_notes'] = 'corrected source context'
        source.write_text(serialize_markdown(entity))
        # Even a deliberately stale index cannot revive the old source.
        self.assertEqual(0, memory.export_memory(self.store, self.profile, **self.options)['signal_count'])
        self.assertEqual('SOURCE_CHANGED', memory.index(self.store, self.profile, **self.options)['records'][0]['status'])
        revised = dict(self.record, revision=2, supersedes=1, source_sha256=memory.source_hash(entity), summary='Selected the screen only for the corrected lighting context.', run_id='run-two')
        second = self.commit(revised, operation='op-two')
        self.assertNotEqual(first['target_commit'], second['target_commit'])
        self.assertEqual(self.record, json.loads(memory.git(self.store, 'show', second['target_commit'] + ':' + first['payload_ref'])))
        self.assertEqual(revised['summary'], memory.export_memory(self.store, self.profile, **self.options)['signals'][0]['statement'])
        self.assertEqual(0, memory.export_memory(self.store, self.profile, **self.options, snapshot=first['target_commit'])['signal_count'])
        entity.meta['consent']['revoked_at'] = '2026-09-07'
        source.write_text(serialize_markdown(entity))
        self.assertEqual(0, memory.export_memory(self.store, self.profile, **self.options)['signal_count'])
        process = self.memory_cli('invalidate')
        self.assertEqual(0, process.returncode, process.stdout)
        self.assertNotEqual('ACTIVE', json.loads(process.stdout)['records'][0]['status'])
        self.assertEqual(second['target_commit'], memory.head(self.store))

    def test_ac3_withdrawal_is_append_only_and_does_not_need_new_consent(self):
        first = self.commit()
        withdrawn = dict(self.record, revision=2, supersedes=1, lifecycle='revoked', run_id='withdrawal')
        receipt = self.commit(withdrawn, operation='withdraw')
        self.assertEqual('COMMITTED', receipt['status'])
        self.assertEqual(0, memory.export_memory(self.store, self.profile, **self.options)['signal_count'])
        self.assertEqual('REVOKED', memory.index(self.store, self.profile, **self.options)['records'][0]['status'])
        self.assertIn(first['target_commit'], memory.git(self.store, 'rev-list', memory.REF))

    def test_ac4_fork_cannot_relabel_inherited_a_as_active_b(self):
        self.commit()
        fork = self.root / 'fork.git'
        result = subprocess.run(['git', 'clone', '--bare', str(self.store), str(fork)], capture_output=True)
        self.assertEqual(0, result.returncode)
        profile_b = self.root / 'profile-b'
        shutil.copytree(self.profile, profile_b)
        document = (profile_b / 'profile.yaml').read_text().replace('synthetic-a', 'synthetic-b')
        (profile_b / 'profile.yaml').write_text(document)
        options_b = dict(creator='creator-b', subject='subject/fixture', collection='memory-b')
        with self.assertRaisesRegex(memory.MemoryError, 'CREATOR_SCOPE_MISMATCH'):
            memory.export_memory(fork, profile_b, **options_b)
        store_b = self.root / 'memory-b.git'
        memory.init_store(store_b, profile_b, **options_b, instance='instance-b')
        self.assertEqual(0, memory.export_memory(store_b, profile_b, **options_b)['signal_count'])
        record_b = dict(self.record, creator_id='creator-b', collection_id='memory-b', origin_instance_id='instance-b', summary='Selected a solid board for another prototype.')
        memory.commit_record(store_b, profile_b, record_b, **options_b, operation_id='b-one', expected_parent=memory.head(store_b))
        self.assertEqual(record_b['summary'], memory.export_memory(store_b, profile_b, **options_b)['signals'][0]['statement'])
        self.assertEqual(self.record['summary'], memory.export_memory(self.store, self.profile, **self.options)['signals'][0]['statement'])
        self.assertEqual('creator-a', json.loads(memory.git(fork, 'show', memory.REF + ':knowledge/store.json'))['creator_id'])

    def test_ac5_raw_credentials_wrong_source_and_profile_boundaries(self):
        for record in (dict(self.record, raw_voice='not allowed'), dict(self.record, summary='ghp_' + 'x' * 35)):
            with self.assertRaises(memory.MemoryError):
                self.commit(record)
        entities, _ = memory._sources(resolve_profile_root(self.profile))
        raw = next(value for entity in entities for value in memory.raw_quotes(entity) if value)
        with self.assertRaisesRegex(memory.MemoryError, 'RAW_VOICE_FORBIDDEN'):
            self.commit(dict(self.record, summary=raw))
        with self.assertRaisesRegex(memory.MemoryError, 'SOURCE_OR_CONSENT_INVALID'):
            self.commit(dict(self.record, source_sha256='0' * 64))
        with self.assertRaises(memory.MemoryError):
            self.commit(dict(self.record, record_id='../escape'))
        self.assertEqual(self.initial, memory.head(self.store))
        alias = self.root / 'alias'
        alias.symlink_to(self.store, target_is_directory=True)
        with self.assertRaisesRegex(memory.MemoryError, 'STORE_PATH_INVALID'):
            memory.export_memory(alias, self.profile, **self.options)
        self.assertEqual('external-local', resolve_profile_root(self.profile).profile['storage_scope'])
        self.assertEqual('', memory.git(self.store, 'remote'))

    def test_replay_conflicts_and_index_failure_resume_keep_commits(self):
        with patch.object(memory, 'index', side_effect=OSError('synthetic disk error')):
            first = self.commit()
        self.assertEqual('INDEX_PENDING', first['status'])
        self.assertEqual(first['target_commit'], memory.head(self.store))
        second = self.commit(parent=self.initial)
        self.assertEqual('NO_CHANGE', second['status'])
        self.assertEqual(first['target_commit'], second['target_commit'])
        self.assertIsNotNone(second['index_hash'])
        with self.assertRaisesRegex(memory.MemoryError, 'OPERATION_CONFLICT'):
            self.commit(dict(self.record, summary='Different text'))
        with self.assertRaisesRegex(memory.MemoryError, 'REVISION_CONFLICT'):
            self.commit(dict(self.record, summary='Different text'), operation='another-op')
        with self.assertRaisesRegex(memory.MemoryError, 'PARENT_CONFLICT'):
            self.commit(dict(self.record, record_id='another-choice'), operation='another-op', parent=self.initial)
        self.assertEqual(first['target_commit'], memory.head(self.store))

    def test_query_empty_and_wrong_purpose_do_not_fabricate_reuse(self):
        self.assertEqual('EMPTY_HISTORY', memory.retrieve(self.store, self.profile, **self.options)['status'])
        self.commit()
        self.assertEqual('FOUND', memory.retrieve(self.store, self.profile, **self.options, query='paper')['status'])
        self.assertEqual('NOT_APPLICABLE', memory.retrieve(self.store, self.profile, **self.options, query='bronze')['status'])
        self.assertEqual(0, memory.export_memory(self.store, self.profile, **self.options, purpose='employment-decision')['signal_count'])

    def test_subject_consent_withdrawal_and_expiry_override_cached_evidence(self):
        self.commit()
        consent_path = self.profile / 'entities/sources/consent.md'
        source = parse_markdown(consent_path)
        source.meta['consent']['revoked_at'] = '2026-09-07'
        consent_path.write_text(serialize_markdown(source))
        self.assertEqual(0, memory.export_memory(self.store, self.profile, **self.options)['signal_count'])
        self.assertEqual('CONSENT_DENIED', memory.index(self.store, self.profile, **self.options)['records'][0]['status'])
        source.meta['consent']['revoked_at'] = None
        source.meta['consent']['expires_at'] = '2020-01-01'
        consent_path.write_text(serialize_markdown(source))
        self.assertEqual(0, memory.export_memory(self.store, self.profile, **self.options)['signal_count'])

    def test_two_writers_same_parent_only_one_canonical_commit(self):
        def writer(number):
            try:
                return self.commit(dict(self.record, record_id=f'writer-{number}'), operation=f'op-{number}', parent=self.initial)['status']
            except memory.MemoryError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, [1, 2]))
        self.assertEqual(['COMMITTED', 'PARENT_CONFLICT'], sorted(results))
        self.assertEqual(2, len(memory.git(self.store, 'rev-list', memory.REF).splitlines()))
        self.assertEqual(1, len(memory.retrieve(self.store, self.profile, **self.options)['records']))

    def test_more_than_nine_revisions_and_pinned_knowledge_envelope(self):
        for revision in range(1, 12):
            record = dict(self.record, revision=revision, supersedes=revision - 1 if revision > 1 else None)
            self.commit(record, operation=f'op-{revision}')
        found = memory.retrieve(self.store, self.profile, **self.options)
        hit = found['records'][0]
        self.assertEqual(11, hit['revision'])
        artifact = hit['artifact']
        self.assertEqual('artifact-record/v1', artifact['contract_version'])
        blob = memory.git(self.store, 'show', found['knowledge_commit'] + ':' + artifact['payload_ref']) + '\n'
        self.assertEqual(memory.digest(blob.encode()), artifact['content_sha256'])
        self.assertEqual('creator-a', artifact['creator_id'])
        receipt = self.commit(dict(self.record, revision=11, supersedes=10), operation='op-11', parent=self.initial)
        self.assertEqual(found['knowledge_commit'], receipt['target_commit'])

    def test_schema_shape_and_malformed_knowledge_locator(self):
        schema = json.loads((ROOT / 'schemas/creative-feedback-v1.schema.json').read_text())
        self.assertEqual(set(self.record), set(schema['required']))
        self.assertFalse(schema['additionalProperties'])
        self.commit()
        output = memory.export_memory(self.store, self.profile, **self.options)
        output['signals'][0]['source_locator'] = 'self-model-knowledge://memory-a/not-a-commit/../raw'
        self.assertTrue(validate_signal_export(output))


if __name__ == '__main__':
    unittest.main()
