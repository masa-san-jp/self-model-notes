"""Synthetic profile coverage for Issue #138; no real owner records."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys
from unittest import mock

from tests.test_growth_hearing import GrowthHearingTestCase
from tests.test_validation import make_entity
from tools.growth_elements import HearingElements, SECTIONS, repetition, select_section
from tools.kb import discover_entities, parse_markdown, serialize_markdown, validate_entities
from tools.export_signals import build_signal_export, export_signals
from tools.profile_root import _classify_privacy_path

ROOT = Path(__file__).resolve().parents[1]


class ElementHearingTests(GrowthHearingTestCase):
    def setUp(self):
        super().setUp()
        self.write_consented_source()
        self.seed = self.write(make_entity('event', 'recent', subject='subject/fixture',
                               source_refs=['source/conversation-20260901'],
                               trigger='迷っていた', action=['窓を開けた'],
                               raw_voice=[{'text': '予定を迷いながら、窓辺で休みたいと思った',
                                           'source_ref': 'source/conversation-20260901'}]))
        self.engine = self.new_engine('run-one')

    def new_engine(self, run):
        return HearingElements(self.profile, run, 'artistic-research')

    def envelope(self, report, value):
        request = report['next_action']['request']
        return {'contract_version': 'element-answer/v1',
                **{key: request[key] for key in ('run_id', 'element_id', 'attempt')}, 'value': value}

    def answer(self, report, value, engine=None):
        return (engine or self.engine).answer(self.envelope(report, value))

    def question(self):
        return self.answer(self.engine.next(), '予定を迷っていたとき、休みたい気持ちと両立しにくかったことは？')

    def derive(self):
        report = self.question()
        report = self.engine.respond(self.event_block('synthetic-reply', trigger='締切が変わった'))
        values = {
            'A1.claim-statement': '予定が変わると、自分の判断を守りたい可能性がある。',
            'A1.claim-layer': 'motivation',
            'A1.claim-direction': 'protect',
            'A1.claim-scope': 'state',
            'A1.alternative-1': '単に作業時間を確保したかった可能性がある。',
            'A1.alternative-2': '周囲への説明を容易にしたかった可能性がある。',
        }
        while report['status'] == 'WAITING':
            request = report['next_action']['request']
            key = request['element_id']
            if key == 'A1.claim-evidence':
                value = request['answer_format']['choices'][0]
            elif key == 'A1.pattern-statement':
                value = request['inputs']['shared_form'] + 'を選び直す場面が繰り返される可能性がある。'
            else:
                value = values[key]
            report = self.answer(report, value)
        self.assertEqual('COMPLETED', report['status'])
        return report

    def signals(self):
        result = export_signals(discover_entities(self.profile / 'entities'), 'subject/fixture',
                                'artistic-research', source_commit='a' * 40)
        self.assertTrue(result['allowed'])
        return build_signal_export(result)['signals']

    def test_question_uses_only_one_quote_and_one_description(self):
        report = self.engine.next()
        request = report['next_action']['request']
        self.assertEqual({'recent_event', 'item_description'}, set(request['inputs']))
        self.assertEqual('予定を迷いながら、窓辺で休みたいと思った', request['inputs']['recent_event'])
        self.assertEqual(SECTIONS['tensions'][0], request['inputs']['item_description'])
        self.assertEqual({'type': 'text', 'max_chars': 60}, request['answer_format'])
        schema = json.loads((ROOT / 'schemas/element-request.schema.json').read_text())
        self.assertEqual(set(schema['required']), set(request))
        self.assertEqual('element-request/v1', request['contract_version'])
        self.assertEqual(report, self.engine.next())
        self.assertNotIn(str(self.profile), json.dumps(report))

    def test_selection_prioritizes_export_evidence_not_alpha_or_body_slots(self):
        self.assertEqual('tensions', select_section([]))
        entities = [make_entity('claim', 'tension', layer='tension', supporting_evidence=['event/one'])]
        self.assertEqual('recurring_patterns', select_section(entities))
        entities.append(make_entity('pattern', 'repeat', evidence=['event/one', 'event/two']))
        self.assertEqual('seeks', select_section(entities))
        entities.append(make_entity('claim', 'seek', motivation_direction='seek', supporting_evidence=['event/one']))
        self.assertEqual('avoids', select_section(entities))
        self.assertNotIn('body', SECTIONS)
        self.assertNotIn('emotion-slot', SECTIONS)

    def test_question_benefit_is_deterministic_and_matches_section(self):
        report = self.question()
        self.assertEqual('HEARING', report['status'])
        self.assertEqual(SECTIONS['tensions'][1], report['next_action']['why'])
        self.assertEqual('event-block', report['next_action']['answer_format'])

    def test_each_invalid_question_retries_only_same_element(self):
        invalid = [('予定を迷った。', 'ends_with_question'), ('何を感じましたか？', 'contains_event_term'),
                   ('予定のclaimは？', 'forbidden_tokens'), ('予定の制作テーマは？', 'no_production_context'),
                   ('予定のslugは？', 'no_production_context'), ('予定の依頼文は？', 'no_production_context'),
                   ('予定は？\n迷う？', 'single_sentence'), ('予定' + '長' * 60 + '？', 'max_chars'),
                   ('予定の連絡先はa@example.com？', 'privacy')]
        for index, (value, check) in enumerate(invalid):
            with self.subTest(value=value):
                engine = self.new_engine('invalid-' + str(index))
                report = engine.next()
                updated = self.answer(report, value, engine)
                pending = updated['next_action']['request']
                self.assertEqual('A1.hearing-question', pending['element_id'])
                self.assertEqual(2, pending['attempt'])
                self.assertEqual(report['next_action']['request']['inputs'], pending['inputs'])
                self.assertTrue(pending['previous_failure'])
                self.assertIn(check, [failure['check'] for failure in pending['previous_failure']])
                self.assertNotIn(value, json.dumps(pending['previous_failure'], ensure_ascii=False))

    def test_five_failed_values_block_without_new_candidate_or_empty_question(self):
        report = self.engine.next()
        for _ in range(5):
            report = self.answer(report, '無関係な問い？')
        self.assertEqual('BLOCKED', report['status'])
        self.assertIsNone(report['next_action'])
        self.assertEqual('A1.hearing-question', report['blocked']['element_id'])
        self.assertEqual(report, self.engine.next())

    def test_malformed_and_stale_answers_do_not_consume_attempt(self):
        report = self.engine.next()
        bad = self.envelope(report, '予定を迷ったのはなぜ？')
        bad['attempt'] = True
        with self.assertRaises(ValueError):
            self.engine.answer(bad)
        bad = self.envelope(report, '予定を迷ったのはなぜ？')
        bad['extra'] = 'private'
        with self.assertRaises(ValueError):
            self.engine.answer(bad)
        bad = self.envelope(report, '予定を迷ったのはなぜ？')
        bad['run_id'] = 'different'
        with self.assertRaises(ValueError):
            self.engine.answer(bad)
        self.assertEqual(report, self.engine.next())

    def test_full_flow_holds_draft_then_confirms_in_later_run_then_exports(self):
        self.derive()
        self.assertEqual([], self.signals())
        self.assertEqual([], list((self.profile / 'entities' / 'claims').glob('*.md')))
        state = json.loads(self.engine.path.read_text())
        self.assertEqual(1, len(state['drafts']))
        draft = next(iter(state['drafts'].values()))
        self.assertEqual('pending', draft['status'])
        self.assertIsNone(draft['meta']['counterevidence'])
        self.assertEqual(2, len(draft['meta']['alternative_explanations']))
        with self.assertRaises(ValueError):
            self.engine.confirm('yes')
        engine2 = self.new_engine('run-two')
        report = engine2.next()
        self.assertEqual('CONFIRMATION', report['status'])
        self.assertEqual('yes-no', report['next_action']['answer_format'])
        engine2.confirm('yes')
        signals = self.signals()
        self.assertEqual(1, len(signals))
        self.assertTrue(signals[0]['protects'])
        self.assertTrue(signals[0]['states'])
        self.assertEqual('unknown', signals[0]['certainty']['level'])
        entity = parse_markdown(next((self.profile / 'entities' / 'claims').glob('*.md')))
        self.assertEqual('hypothesis', entity.meta['status'])
        self.assertEqual('unknown', entity.meta['confidence'])
        self.assertNotIn('自分で決めたい', json.dumps(signals, ensure_ascii=False))
        self.assertEqual([], validate_entities(discover_entities(self.profile / 'entities'), root=self.profile))

    def test_owner_no_keeps_draft_unexported(self):
        self.derive()
        engine2 = self.new_engine('run-two')
        engine2.next()
        engine2.confirm('no')
        self.assertEqual([], self.signals())
        state = json.loads(engine2.path.read_text())
        self.assertEqual('rejected', next(iter(state['drafts'].values()))['status'])
        self.assertNotEqual('CONFIRMATION', self.new_engine('run-three').next()['status'])

    def test_pattern_requires_distinct_events_and_literal_form(self):
        event = make_entity('event', 'one', trigger=None, action=[],
                            raw_voice=[{'text': '選択、選択、選択', 'source_ref': 'source/example'}])
        self.assertIsNone(repetition([event], event.id))
        other = make_entity('event', 'two', trigger=None, action=[],
                            raw_voice=[{'text': '選択を見直した', 'source_ref': 'source/example'}])
        self.assertEqual(('選択', ['event/one', 'event/two']), repetition([event, other], event.id))
        event.meta['raw_voice'] = [{'text': '窓辺で休んだ', 'source_ref': 'source/example'}]
        self.assertIsNone(repetition([event, other], event.id))

    def test_repeated_pattern_is_separate_inference_and_separate_owner_confirmation(self):
        seed = parse_markdown(self.seed)
        seed.meta['action'] = ['計画を変更した']
        self.seed.write_text(serialize_markdown(seed))
        self.derive()
        state = json.loads(self.engine.path.read_text())
        self.assertEqual(2, len(state['drafts']))
        self.assertEqual([], self.signals())
        engine2 = self.new_engine('run-two')
        self.assertEqual('CONFIRMATION', engine2.next()['status'])
        self.assertEqual('CONFIRMATION', engine2.confirm('yes')['status'])
        self.assertEqual(1, len(self.signals()))
        engine2.confirm('yes')
        signals = self.signals()
        self.assertEqual(2, len(signals))
        self.assertTrue(any(s['recurring_patterns'] for s in signals))
        pattern = next((self.profile / 'entities' / 'patterns').glob('*.md'))
        meta = parse_markdown(pattern).meta
        self.assertEqual(2, len(set(meta['evidence'])))
        self.assertEqual('hypothesis', meta['status'])

    def test_wrong_layer_and_nonexistent_reference_retry_individually(self):
        self.question()
        report = self.engine.respond(self.event_block('synthetic-reply'))
        report = self.answer(report, 'その時は計画を優先した可能性がある。')
        report = self.answer(report, 'new-layer')
        self.assertEqual('A1.claim-layer', report['next_action']['request']['element_id'])
        report = self.answer(report, 'other')
        report = self.answer(report, 'event/does-not-exist')
        self.assertEqual('A1.claim-evidence', report['next_action']['request']['element_id'])
        self.assertEqual(2, report['next_action']['request']['attempt'])

    def test_consent_revocation_blocks_resume_and_owner_publication(self):
        self.derive()
        engine2 = self.new_engine('run-two')
        engine2.next()
        source = self.profile / 'entities' / 'sources' / 'conversation-20260901.md'
        entity = parse_markdown(source)
        entity.meta['consent']['revoked_at'] = '2026-09-30'
        source.write_text(serialize_markdown(entity))
        before = engine2.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'CONSENT_INVALID'):
            engine2.confirm('yes')
        with self.assertRaisesRegex(ValueError, 'CONSENT_INVALID'):
            engine2.next()
        self.assertEqual(before, engine2.path.read_bytes())
        self.assertEqual([], list((self.profile / 'entities' / 'claims').glob('*.md')))

    def test_changed_event_blocks_inference_and_confirmation(self):
        report = self.engine.next()
        seed = parse_markdown(self.seed)
        seed.meta['trigger'] = '別の理由'
        self.seed.write_text(serialize_markdown(seed))
        with self.assertRaisesRegex(ValueError, 'EVENT_CHANGED'):
            self.answer(report, '予定を迷ったのはなぜ？')
        # A fresh run can use the new evidence; confirmation rechecks it too.
        self.engine = self.new_engine('run-fresh')
        self.derive()
        engine2 = self.new_engine('run-two')
        engine2.next()
        path = next(path for path in (self.profile / 'entities' / 'events').glob('hearing-*.md'))
        entity = parse_markdown(path)
        entity.meta['trigger'] = '変更済み'
        path.write_text(serialize_markdown(entity))
        with self.assertRaisesRegex(ValueError, 'EVENT_CHANGED'):
            engine2.confirm('yes')

    def test_no_recent_event_requests_seed_without_fabricated_inference(self):
        self.seed.unlink()
        report = self.engine.next()
        self.assertEqual('SEED_REQUIRED', report['status'])
        self.assertEqual('hearing', report['next_action']['kind'])
        report = self.engine.respond(self.event_block('first'))
        self.assertEqual('A1.claim-statement', report['next_action']['request']['element_id'])

    def test_direct_identifier_answer_creates_nothing(self):
        self.question()
        before = self.engine.path.read_bytes()
        with self.assertRaises(ValueError):
            self.engine.respond(self.event_block('bad', trigger='a@example.com から連絡'))
        self.assertEqual(before, self.engine.path.read_bytes())
        self.assertEqual([self.seed], list((self.profile / 'entities' / 'events').glob('*.md')))

    def test_save_failure_rolls_back_event(self):
        self.question()
        with mock.patch.object(self.engine, 'save', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.engine.respond(self.event_block('rollback'))
        self.assertEqual([self.seed], list((self.profile / 'entities' / 'events').glob('*.md')))

    def test_state_stays_profile_local_with_private_file_permissions(self):
        report = self.engine.next()
        self.assertEqual(self.profile / 'growth' / 'elements' / 'state.json', self.engine.path)
        self.assertEqual(0o600, self.engine.path.stat().st_mode & 0o777)
        self.assertNotIn('answers', report)
        self.assertNotIn('drafts', report)
        self.assertFalse((ROOT / 'growth' / 'elements').exists())

    def test_run_and_state_symlink_escape_rejected(self):
        for unsafe in ('../escape', '/tmp/escape', ''):
            with self.assertRaises(ValueError):
                self.new_engine(unsafe)
        outside = self.profile.parent / 'outside'
        outside.mkdir()
        (self.profile / 'growth').mkdir()
        (self.profile / 'growth' / 'elements').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'UNSAFE_PROFILE_STATE'):
            self.engine.next()
        self.assertEqual([], list(outside.iterdir()))

    def test_skip_preserves_pending_confirmation_for_another_run(self):
        self.derive()
        engine2 = self.new_engine('run-two')
        engine2.next()
        self.assertEqual('SKIPPED', engine2.skip()['status'])
        self.assertEqual('CONFIRMATION', self.new_engine('run-three').next()['status'])
        self.assertEqual([], self.signals())

    def test_privacy_guard_rejects_state_requests_answers_and_parent_copies(self):
        self.engine.next()
        records = [json.loads(self.engine.path.read_text()),
                   self.engine.next()['next_action']['request'],
                   self.envelope(self.engine.next(), '予定を迷ったのはなぜ？'), self.engine.next()]
        for index, record in enumerate(records):
            path = self.profile / f'leak-{index}.json'
            path.write_text(json.dumps(record, ensure_ascii=False))
            self.assertEqual('growth-log', _classify_privacy_path(self.profile, path.name))
        # Contract schemas are not owner requests and remain allowed.
        self.assertIsNone(_classify_privacy_path(ROOT, 'schemas/element-request.schema.json'))

    def test_cli_next_answer_and_error_do_not_echo_rejected_private_value(self):
        base = [sys.executable, 'tools/agent_runtime.py', 'tools/growth_tasks.py', 'element']
        args = ['--run-id', 'cli-run', '--purpose', 'artistic-research', '--profile-root', str(self.profile)]
        result = subprocess.run(base + ['next'] + args, cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        result = subprocess.run(base + ['answer'] + args, cwd=ROOT, text=True,
                                input=json.dumps(self.envelope(report, '予定を迷ったのはなぜ？')), capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('HEARING', json.loads(result.stdout)['status'])
        result = subprocess.run(base + ['answer'] + args, cwd=ROOT, text=True,
                                input='{"private": "unique-secret-987"', capture_output=True)
        self.assertEqual(2, result.returncode)
        self.assertNotIn('unique-secret-987', result.stderr + result.stdout)
        self.assertNotIn(str(self.profile), result.stderr + result.stdout)

    def test_cli_requires_explicit_profile_root(self):
        result = subprocess.run([sys.executable, 'tools/agent_runtime.py', 'tools/growth_tasks.py',
                                 'element', 'next', '--run-id', 'one', '--purpose', 'artistic-research'],
                                cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(2, result.returncode)
        self.assertIn('PROFILE_ROOT_REQUIRED', result.stderr)

    def test_schema_copies_have_recorded_parent_digests(self):
        for name, digest in {
            'element-request': 'dec0cc4987bad6ade858becb1e741ac74848df79406995b4cdbdfafe9f0ef1d7',
            'element-answer': '7b74d114e0d9ee8976dc8ed17e43623b1e5a490a08c6bb4f1a5118b78c6cc812',
        }.items():
            data = (ROOT / 'schemas' / (name + '.schema.json')).read_bytes()
            self.assertEqual(digest, hashlib.sha256(data).hexdigest())
            schema = json.loads(data)
            self.assertEqual(name + '/v1', schema['$id'])
            self.assertFalse(schema['additionalProperties'])

    def test_context_condition_and_alternatives_are_separate_values(self):
        self.question()
        report = self.engine.respond(self.event_block('context-reply'))
        report = self.answer(report, '周囲と一緒にいると、計画の変更をためらう可能性がある。')
        report = self.answer(report, 'other')
        report = self.answer(report, report['next_action']['request']['answer_format']['choices'][0])
        self.assertEqual('A1.claim-scope', report['next_action']['request']['element_id'])
        # A single Event must not permit trait inference.
        self.assertEqual(['state', 'context-bound'], report['next_action']['request']['answer_format']['choices'])
        report = self.answer(report, 'context-bound')
        self.assertEqual('A1.claim-condition', report['next_action']['request']['element_id'])
        report = self.answer(report, '他の人と一緒に作業しているとき。')
        report = self.answer(report, '単に作業順序を整えた可能性がある。')
        report = self.answer(report, '単に作業順序を整えた可能性がある。')
        self.assertEqual('A1.alternative-2', report['next_action']['request']['element_id'])
        self.assertEqual(2, report['next_action']['request']['attempt'])
        report = self.answer(report, '相手の予定を優先した可能性がある。')
        self.assertEqual('COMPLETED', report['status'])
        engine2 = self.new_engine('run-two')
        engine2.next()
        engine2.confirm('yes')
        self.assertEqual(['他の人と一緒に作業しているとき。'], self.signals()[0]['contexts'])

    def test_rejected_claim_cannot_back_an_exported_pattern(self):
        seed = parse_markdown(self.seed)
        seed.meta['action'] = ['計画を変更した']
        self.seed.write_text(serialize_markdown(seed))
        self.derive()
        engine2 = self.new_engine('run-two')
        engine2.next()
        self.assertEqual('CONFIRMATION', engine2.confirm('no')['status'])
        with self.assertRaisesRegex(ValueError, 'CLAIM_CONFIRMATION_REQUIRED'):
            engine2.confirm('yes')
        engine2.confirm('no')
        self.assertEqual([], self.signals())

    def test_confirmation_save_failure_rolls_back_canonical_entity(self):
        self.derive()
        engine2 = self.new_engine('run-two')
        engine2.next()
        before = engine2.path.read_bytes()
        with mock.patch.object(engine2, 'save', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                engine2.confirm('yes')
        self.assertEqual(before, engine2.path.read_bytes())
        self.assertEqual([], self.signals())
        engine2.confirm('yes')
        self.assertEqual(1, len(self.signals()))

    def test_unconsented_raw_quote_reference_is_not_used(self):
        seed = parse_markdown(self.seed)
        seed.meta['raw_voice'][0]['source_ref'] = 'source/not-consented'
        self.seed.write_text(serialize_markdown(seed))
        with self.assertRaisesRegex(ValueError, 'RAW_VOICE_SOURCE_INVALID'):
            self.engine.next()

    def test_pending_only_exports_no_raw_text_and_regenerates_synthetic_graph(self):
        self.derive()
        engine2 = self.new_engine('run-two')
        engine2.next()
        engine2.confirm('yes')
        commands = [
            ['tools/build_graph.py'],
            ['tools/build_graph.py', '--check'],
            ['tools/audit.py', '--dry-run'],
        ]
        for command in commands:
            result = subprocess.run([sys.executable, 'tools/agent_runtime.py', *command,
                                     '--profile-root', str(self.profile)], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.profile / 'data').is_dir())
        self.assertTrue((self.profile / 'overviews' / 'coverage.md').is_file())
