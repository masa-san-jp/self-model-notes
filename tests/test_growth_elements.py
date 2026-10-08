"""Synthetic profile coverage for Issue #138; no real owner records."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys
from copy import deepcopy
import tempfile
import unittest
from unittest import mock

from tests.test_growth_hearing import GrowthHearingTestCase
from tests.test_validation import make_entity
from tools.growth_elements import (HearingElements, SECTIONS, SECTION_FIELDS, load_question_types,
                                   question_shape_failures, select_question_type, repetition, select_section, terms)
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
        report = self.engine.next()
        return self.answer(report, self.reword(report, '予定'))

    def reword(self, report, term):
        return report['next_action']['request']['inputs']['question_type']['question'].replace(
            'この出来事', f'「{term}」のこの出来事')

    def derive(self):
        report = self.question()
        report = self.engine.respond(self.event_block('synthetic-reply', trigger='締切が変わった'))
        values = {
            'A1.claim-statement': '予定の変更に合わせたい思いと、自分の判断を守りたい思いがぶつかる可能性がある。',
            'A1.claim-layer': 'motivation',
            'A1.claim-direction': 'protect',
            'A1.claim-scope': 'state',
            'A1.alternative-1': '単に作業時間を確保したかった可能性がある。',
            'A1.alternative-2': '周囲への説明を容易にしたかった可能性がある。',
        }
        while report['status'] == 'WAITING':
            request = report['next_action']['request']
            key = request['element_id']
            if key == 'A1.pattern-statement':
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

    def test_question_uses_one_quote_one_description_and_program_selected_type(self):
        report = self.engine.next()
        request = report['next_action']['request']
        self.assertEqual({'recent_event', 'item_description', 'question_type'}, set(request['inputs']))
        self.assertEqual('tensions-thought', request['inputs']['question_type']['id'])
        self.assertEqual(1, request['inputs']['question_type']['revision'])
        self.assertEqual(['この出来事のとき'], request['inputs']['question_type']['scope_terms'])
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

    def test_answerable_shape_checks_retry_the_same_type_and_preserve_safe_errors(self):
        invalid = [
            ('予定のこの出来事のとき、何を感じた場面がありましたか？', 'no_existence_question'),
            ('予定のこの出来事のとき、何を感じた場面がありますか？', 'no_existence_question'),
            ('予定のこの出来事のとき、何を感じた場面がありませんか？', 'no_existence_question'),
            ('予定のこの出来事のとき、何を感じた場面があるのでしょうか？', 'no_existence_question'),
            ('予定のこの出来事のとき、ほかに気になったことは何ですか？', 'no_unbounded_words'),
            ('予定のこの出来事のとき、何か気になった場面はどんな場面でしたか？', 'no_unbounded_words'),
            ('予定のこの出来事のとき、いつかしたいことは何ですか？', 'no_unbounded_words'),
            ('予定を迷ったとき、まず考えたことは何ですか？', 'contains_scope_terms'),
            ('予定のこの出来事のとき、まず考えたことは？', 'asks_content'),
            ('', 'non_empty'),
        ]
        for index, (value, check) in enumerate(invalid):
            with self.subTest(check=check, index=index):
                engine = self.new_engine('shape-' + str(index))
                before = engine.next()
                history = json.loads(engine.path.read_text()).get('last_question_type')
                report = self.answer(before, value, engine)
                request = report['next_action']['request']
                self.assertEqual('WAITING', report['status'])
                self.assertEqual(2, request['attempt'])
                self.assertEqual(before['next_action']['request']['inputs'], request['inputs'])
                self.assertEqual(before['next_action']['request']['checks'], request['checks'])
                self.assertIn(check, [failure['check'] for failure in request['previous_failure']])
                state = json.loads(engine.path.read_text())
                self.assertEqual(history, state.get('last_question_type'))
                self.assertEqual({}, state['runs'][engine.run_id]['values'])
                self.assertEqual('HEARING', self.answer(report, self.reword(report, '予定'), engine)['status'])

    def test_type_rotation_is_per_section_and_only_records_heard_questions(self):
        selected = []
        with mock.patch('tools.growth_elements.select_section', return_value='tensions'):
            for index in range(3):
                engine = self.new_engine('rotate-' + str(index))
                report = engine.next()
                selected.append(report['next_action']['request']['inputs']['question_type']['id'])
                self.assertEqual(report, self.new_engine(engine.run_id).next())
                self.answer(report, self.reword(report, '予定'), engine)
                engine.skip()
        self.assertEqual(['tensions-thought', 'tensions-choice', 'tensions-thought'], selected)
        with mock.patch('tools.growth_elements.select_section', return_value='states'):
            request = self.new_engine('different-section').next()['next_action']['request']
        self.assertEqual('states-feeling', request['inputs']['question_type']['id'])

    def test_question_type_and_revision_are_pinned_on_retry_after_config_change(self):
        report = self.engine.next()
        newer = deepcopy(load_question_types())
        newer['revision'] = 2
        newer['sections']['tensions'][0]['scope_terms'] = ['前回の制作のとき']
        with mock.patch('tools.growth_elements.load_question_types', return_value=newer):
            report = self.answer(report, '予定については？')
            self.assertEqual(1, report['next_action']['request']['inputs']['question_type']['revision'])
            self.assertEqual(report, self.new_engine('run-one').next())
            self.assertEqual('HEARING', self.answer(report, self.reword(report, '予定'))['status'])

    def test_old_untyped_pending_question_requires_rebinding_without_resetting_attempt(self):
        report = self.engine.next()
        state = json.loads(self.engine.path.read_text())
        run = state['runs']['run-one']
        del run['question_type']
        del run['pending']['inputs']['question_type']
        run['pending']['attempt'] = 3
        self.engine.save(state)
        old_report = self.engine.report(run)
        with self.assertRaisesRegex(ValueError, 'QUESTION_TYPE_REQUIRED'):
            self.answer(old_report, '予定を迷ったとき、何を思いましたか？')
        upgraded = self.new_engine('run-one').next()
        self.assertEqual(3, upgraded['next_action']['request']['attempt'])
        self.assertEqual(report['next_action']['request']['inputs'], upgraded['next_action']['request']['inputs'])
        self.assertEqual('HEARING', self.answer(upgraded, self.reword(upgraded, '予定'))['status'])

    def test_invalid_type_config_creates_no_partial_run_or_fallback(self):
        with mock.patch('tools.growth_elements.load_question_types', side_effect=ValueError('QUESTION_TYPES_INVALID')):
            with self.assertRaisesRegex(ValueError, 'QUESTION_TYPES_INVALID'):
                self.engine.next()
        self.assertFalse(self.engine.path.exists())

    def test_childhood_type_needs_explicit_anchor_and_preserves_both_premises(self):
        seed = parse_markdown(self.seed)
        seed.meta['raw_voice'][0]['text'] = '小さい頃、窓辺で絵を描いた'
        self.seed.write_text(serialize_markdown(seed))
        with mock.patch('tools.growth_elements.select_section', return_value='contexts'):
            report = self.engine.next()
        self.assertEqual('contexts-childhood', report['next_action']['request']['inputs']['question_type']['id'])
        report = self.answer(report, '窓辺のこの出来事のとき、印象に残った場面はどんな場面でしたか？')
        self.assertIn('contains_scope_terms', [failure['check'] for failure in report['next_action']['request']['previous_failure']])
        self.assertEqual('HEARING', self.answer(report, self.reword(report, '窓辺'))['status'])

    def test_question_requires_exact_content_word_not_single_kanji_or_substring(self):
        seed = parse_markdown(self.seed)
        seed.meta['raw_voice'][0]['text'] = '近くの川辺で休みたいと思った'
        self.seed.write_text(serialize_markdown(seed))
        report = self.engine.next()
        for question in ('最近どうですか？', '川で何を感じた？', '川辺道で休みたい？'):
            report = self.answer(report, question)
            self.assertIn('contains_event_term', [failure['check'] for failure in
                          report['next_action']['request']['previous_failure']])
        self.assertEqual('HEARING', self.answer(report, self.reword(report, '川辺'))['status'])
        self.assertEqual({'川辺'}, terms('近くの川辺で休みたいと思った'))

    def test_element_text_rejects_section_layer_and_direction_vocabulary(self):
        report = self.engine.next()
        run = json.loads(self.engine.path.read_text())['runs']['run-one']
        tokens = [*SECTIONS, 'recurring', 'belief', 'tension', 'behavioral-principle', 'motivation', 'seek', 'protect', 'avoid']
        for token in tokens:
            with self.subTest(token=token):
                failures = self.engine.failures(run, f'予定の{token}については？')
                self.assertIn('forbidden_tokens', [failure['check'] for failure in failures])
        self.assertEqual(report, self.engine.next())

    def test_last_heard_section_is_deferred_even_when_it_has_least_coverage(self):
        self.question()
        self.engine.skip()
        engine2 = self.new_engine('after-skipped')
        request = engine2.next()['next_action']['request']
        self.assertEqual(SECTIONS['recurring_patterns'][0], request['inputs']['item_description'])
        entities = [make_entity('pattern', 'covered', evidence=['event/one', 'event/two'])]
        self.assertEqual('tensions', select_section(entities))
        self.assertEqual('seeks', select_section(entities, last_section='tensions'))

    def test_section_fixes_fields_and_owner_confirmation_exports_promised_group(self):
        for index, (section, fields) in enumerate(SECTION_FIELDS.items()):
            with self.subTest(section=section):
                engine = self.new_engine('fields-' + str(index))
                with mock.patch('tools.growth_elements.select_section', return_value=section):
                    report = engine.next()
                    while report['status'] == 'CONFIRMATION':
                        report = engine.confirm('yes')
                term = sorted(terms(report['next_action']['request']['inputs']['recent_event']))[0]
                report = self.answer(report, self.reword(report, term), engine)
                self.assertEqual(SECTIONS[section][1], report['next_action']['why'])
                report = engine.respond(self.event_block('fields-reply-' + str(index), trigger='別々の締切'))
                self.assertEqual(SECTIONS[section][0], report['next_action']['request']['inputs']['item_description'])
                seen = []
                values = {'A1.claim-statement': '自分の判断を保つために、計画の変更をためらう可能性がある。',
                          'A1.claim-layer': 'other', 'A1.claim-scope': 'state',
                          'A1.claim-condition': '他の人と一緒に作業しているとき。',
                          'A1.alternative-1': '単に手順を整えた可能性がある。',
                          'A1.alternative-2': '周囲の予定を優先した可能性がある。'}
                while report['status'] == 'WAITING':
                    request = report['next_action']['request']
                    key = request['element_id']
                    seen.append(key)
                    value = (request['inputs']['shared_form'] + 'が繰り返される可能性がある。'
                             if key == 'A1.pattern-statement' else values[key])
                    report = self.answer(report, value, engine)
                self.assertNotIn('A1.claim-evidence', seen)
                for field in fields:
                    self.assertNotIn('A1.' + field, seen)
                draft = json.loads(engine.path.read_text())['drafts'][engine.draft_id('claim')]
                for field, value in fields.items():
                    meta_key = {'claim-layer': 'layer', 'claim-direction': 'motivation_direction', 'claim-scope': 'scope'}[field]
                    self.assertEqual(value, draft['meta'][meta_key])
                confirmation = self.new_engine('fields-confirm-' + str(index))
                self.assertEqual('CONFIRMATION', confirmation.next()['status'])
                report = confirmation.confirm('yes')
                while report['status'] == 'CONFIRMATION':
                    report = confirmation.confirm('yes')
                signal = next(signal for signal in self.signals() if signal['entity_id'] == engine.draft_id('claim'))
                self.assertTrue(signal[section])

    def test_short_japanese_and_latin_words_use_existing_event_for_question(self):
        cases = [('うれしい', 'うれしい'), ('火花を見た', '火花'), ('AI', 'AI')]
        for index, (voice, term) in enumerate(cases):
            seed = parse_markdown(self.seed)
            seed.meta['raw_voice'][0]['text'] = voice
            self.seed.write_text(serialize_markdown(seed))
            engine = self.new_engine('short-word-' + str(index))
            report = engine.next()
            self.assertEqual('WAITING', report['status'])
            self.assertEqual(voice, report['next_action']['request']['inputs']['recent_event'])
            self.assertEqual('HEARING', self.answer(report, self.reword(report, term), engine)['status'])

    def test_short_raw_word_can_anchor_derived_claim_and_pattern_without_pure_copy(self):
        seed = parse_markdown(self.seed)
        seed.meta.update(trigger=None, action=[], raw_voice=[{
            'text': 'うれしい', 'source_ref': 'source/conversation-20260901'}])
        self.seed.write_text(serialize_markdown(seed))
        report = self.engine.next()
        report = self.answer(report, self.reword(report, 'うれしい'))
        block = self.event_block('short-reply').replace('自分で決めたい', 'うれしい')
        report = self.engine.respond(block)
        report = self.answer(report, '「うれしい。」')
        self.assertEqual('A1.claim-statement', report['next_action']['request']['element_id'])
        self.assertIn('derived_only', [failure['check'] for failure in report['next_action']['request']['previous_failure']])
        report = self.answer(report, 'うれしい気持ちを保ちたい可能性がある。')
        report = self.answer(report, 'state')
        report = self.answer(report, '作業が終わった安堵だった可能性がある。')
        report = self.answer(report, '周囲からの応答に安心した可能性がある。')
        request = report['next_action']['request']
        self.assertEqual('A1.pattern-statement', request['element_id'])
        self.assertEqual('うれしい', request['inputs']['shared_form'])
        report = self.answer(report, 'うれしい気持ちが別の場面にも現れる可能性がある。')
        self.assertEqual('COMPLETED', report['status'])
        self.assertEqual([], self.signals())
        engine2 = self.new_engine('short-confirmation')
        engine2.next()
        engine2.confirm('yes')
        engine2.confirm('yes')
        self.assertEqual(2, len(self.signals()))

    def test_each_invalid_question_retries_only_same_element(self):
        invalid = [('予定を迷った。', 'ends_with_question'), ('何を感じましたか？', 'contains_event_term'),
                   ('予定のclaimは？', 'forbidden_tokens'), ('予定の制作テーマは？', 'no_production_context'),
                   ('予定のslugは？', 'no_production_context'), ('予定の依頼文は？', 'no_production_context'),
                   ('予定は？\n迷う？', 'single_sentence'), ('予定' + '長' * 60 + '？', 'max_chars'),
                   ('予定の連絡先はa@example.com？', 'privacy'),
                   ('予定を迷った. Why?', 'single_sentence'),
                   ('予定のRECENTとは？', 'no_production_context'),
                   ('予定の参照はｈｔｔｐｓ：／／example.com？', 'privacy')]
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
        self.assertTrue(signals[0]['tensions'])
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

    def test_repetition_ignores_single_kanji_and_inflection_only_matches(self):
        for voices in [('火を見た', '火を見た'), ('窓を開いていた', '棚を閉じていた')]:
            events = [make_entity('event', str(index), trigger=None, action=[],
                                 raw_voice=[{'text': voice, 'source_ref': 'source/example'}])
                      for index, voice in enumerate(voices)]
            self.assertIsNone(repetition(events, events[0].id))
        events = [make_entity('event', str(index), trigger='していた', action=['見ていた'],
                             raw_voice=[{'text': voice, 'source_ref': 'source/example'}])
                  for index, voice in enumerate(('予定を変えた', '窓辺で休んだ'))]
        self.assertIsNone(repetition(events, events[0].id))

    def test_repetition_prioritizes_content_bearing_trigger_or_action_over_raw_word(self):
        events = [make_entity('event', str(index), trigger='変更', action=[],
                             raw_voice=[{'text': '制作計画調整を続けた', 'source_ref': 'source/example'}])
                  for index in range(2)]
        self.assertEqual(('変更', ['event/0', 'event/1']), repetition(events, events[0].id))
        for event in events:
            event.meta.update(trigger=None, action=['作業を再開した'])
        self.assertEqual(('作業を再開した', ['event/0', 'event/1']), repetition(events, events[0].id))

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

    def test_invalid_scope_retries_and_evidence_is_wired_without_inference(self):
        self.question()
        report = self.engine.respond(self.event_block('synthetic-reply'))
        report = self.answer(report, 'その時は計画を優先した可能性がある。')
        self.assertEqual('A1.claim-scope', report['next_action']['request']['element_id'])
        report = self.answer(report, 'trait')
        self.assertEqual('A1.claim-scope', report['next_action']['request']['element_id'])
        self.assertEqual(2, report['next_action']['request']['attempt'])
        run = json.loads(self.engine.path.read_text())['runs']['run-one']
        self.assertEqual(run['event'], run['values']['claim-evidence'])
        self.assertEqual('tension', run['values']['claim-layer'])

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

    def test_privacy_guard_scans_every_jsonl_line_for_nested_element_records(self):
        for version in ('growth-elements/v1', 'element-request/v1', 'element-answer/v1'):
            path = self.profile / 'later-record.jsonl'
            path.write_text('{}\nnot-json\n\n' + json.dumps({'relay': {'contract_version': version}}) + '\n')
            self.assertEqual('growth-log', _classify_privacy_path(self.profile, path.name))

    def test_cli_next_answer_and_error_do_not_echo_rejected_private_value(self):
        base = [sys.executable, 'tools/agent_runtime.py', 'tools/growth_tasks.py', 'element']
        args = ['--run-id', 'cli-run', '--purpose', 'artistic-research', '--profile-root', str(self.profile)]
        result = subprocess.run(base + ['next'] + args, cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        result = subprocess.run(base + ['answer'] + args, cwd=ROOT, text=True,
                                input=json.dumps(self.envelope(report, self.reword(report, '予定'))), capture_output=True)
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

    def test_cli_requester_alias_full_question_to_owner_confirmation_and_export(self):
        self.cli_flow_trace = []
        base = [sys.executable, 'tools/agent_runtime.py', 'tools/growth_tasks.py', 'element']
        def call(command, run='cli-full-one', value=None, owner_answer=None):
            args = ['--requester', run, '--purpose', 'artistic-research', '--profile-root', str(self.profile)]
            if owner_answer:
                args += ['--owner-answer', owner_answer]
            result = subprocess.run(base + [command] + args, cwd=ROOT, text=True,
                                    input=value, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
            report = json.loads(result.stdout)
            self.cli_flow_trace.append({'command': command, 'exit': result.returncode, 'status': report['status']})
            return report
        report = call('next')
        self.assertEqual('A1.hearing-question', report['next_action']['request']['element_id'])
        report = call('answer', value=json.dumps(self.envelope(report, self.reword(report, '予定'))))
        self.assertEqual('HEARING', report['status'])
        report = call('respond', value=self.event_block('cli-full-reply'))
        values = ['計画に合わせたい思いと、自分で決めたい思いがぶつかる可能性がある。', 'state',
                  '単に作業時間を確保したかった可能性がある。', '周囲への説明を容易にしたかった可能性がある。']
        steps = ['claim-statement', 'claim-scope', 'alternative-1', 'alternative-2']
        for step, value in zip(steps, values):
            self.assertEqual('A1.' + step, report['next_action']['request']['element_id'])
            report = call('answer', value=json.dumps(self.envelope(report, value)))
        self.assertEqual('COMPLETED', report['status'])
        self.assertEqual([], self.signals())
        report = call('next', run='cli-full-two')
        self.assertEqual('CONFIRMATION', report['status'])
        report = call('confirm', run='cli-full-two', owner_answer='yes')
        self.assertEqual('WAITING', report['status'])
        result = subprocess.run([sys.executable, 'tools/agent_runtime.py', 'tools/export_signals.py',
                                 '--profile-root', str(self.profile), '--subject', 'subject/fixture',
                                 '--purpose', 'artistic-research', '--requester', 'cli-full-two'],
                                cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        exported = json.loads(result.stdout)
        self.assertEqual(1, exported['signal_count'])
        self.assertTrue(exported['signals'][0]['tensions'])
        self.cli_flow_trace.append({'command': 'export_signals', 'exit': result.returncode, 'signal_count': exported['signal_count']})
        report = call('answer', run='cli-full-two', value=json.dumps(self.envelope(report, self.reword(report, '自分'))))
        self.assertEqual('HEARING', report['status'])
        self.assertEqual('SKIPPED', call('skip', run='cli-full-two')['status'])

    def test_cli_seed_required_accepts_requester_and_respond(self):
        self.seed.unlink()
        base = [sys.executable, 'tools/agent_runtime.py', 'tools/growth_tasks.py', 'element']
        args = ['--requester', 'cli-seed', '--purpose', 'artistic-research', '--profile-root', str(self.profile)]
        result = subprocess.run(base + ['next'] + args, cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('SEED_REQUIRED', json.loads(result.stdout)['status'])
        result = subprocess.run(base + ['respond'] + args, cwd=ROOT, text=True,
                                input=self.event_block('cli-seed-reply'), capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('A1.claim-statement', json.loads(result.stdout)['next_action']['request']['element_id'])

    def test_malformed_entity_errors_do_not_echo_private_yaml_or_path(self):
        private = 'synthetic-private-marker-987'
        self.seed.write_text('---\nraw_voice: ["' + private + '"\n---\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '^ENTITIES_INVALID$'):
            self.engine.next()
        result = subprocess.run([sys.executable, 'tools/agent_runtime.py', 'tools/growth_tasks.py',
                                 'element', 'next', '--run-id', 'malformed', '--purpose', 'artistic-research',
                                 '--profile-root', str(self.profile)], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(2, result.returncode)
        self.assertNotIn(private, result.stdout + result.stderr)
        self.assertNotIn(str(self.profile), result.stdout + result.stderr)

    def test_joined_fullwidth_identifier_is_rejected_before_event_creation(self):
        self.question()
        before = self.engine.path.read_bytes()
        for trigger in ('参照はｈｔｔｐｓ：／／example.com', '連絡先はａ＠example.com'):
            with self.assertRaisesRegex(ValueError, 'direct-identifier-detected'):
                self.engine.respond(self.event_block('bad-unicode', trigger=trigger))
        self.assertEqual(before, self.engine.path.read_bytes())
        self.assertEqual([self.seed], list((self.profile / 'entities' / 'events').glob('*.md')))

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
        self.assertNotEqual('CONFIRMATION', engine2.confirm('no')['status'])
        state = json.loads(engine2.path.read_text())
        pattern = next(draft for draft in state['drafts'].values() if draft['meta']['type'] == 'pattern')
        self.assertEqual('rejected', pattern['status'])
        self.assertEqual('superseded', pattern['rejection_reason'])
        self.assertNotEqual('CONFIRMATION', self.new_engine('run-three').next()['status'])
        self.assertEqual([], self.signals())

    def test_old_rejected_claim_automatically_supersedes_pending_pattern_on_resume(self):
        seed = parse_markdown(self.seed)
        seed.meta['action'] = ['計画を変更した']
        self.seed.write_text(serialize_markdown(seed))
        self.derive()
        state = json.loads(self.engine.path.read_text())
        state['drafts'][self.engine.draft_id('claim')]['status'] = 'rejected'
        self.engine.save(state)
        self.assertNotEqual('CONFIRMATION', self.new_engine('resume-old-ledger').next()['status'])
        state = json.loads(self.engine.path.read_text())
        self.assertEqual('superseded', state['drafts'][self.engine.draft_id('pattern')]['rejection_reason'])
        self.assertEqual([], self.signals())

    def test_pattern_finished_after_claim_rejection_is_already_superseded(self):
        seed = parse_markdown(self.seed)
        seed.meta['action'] = ['計画を変更した']
        self.seed.write_text(serialize_markdown(seed))
        self.question()
        report = self.engine.respond(self.event_block('late-pattern'))
        for value in ('計画に合わせたい思いと、自分で決めたい思いがぶつかる可能性がある。',
                      'state', '作業時間を確保したかった可能性がある。', '周囲の予定を優先した可能性がある。'):
            report = self.answer(report, value)
        self.assertEqual('A1.pattern-statement', report['next_action']['request']['element_id'])
        confirming = self.new_engine('reject-before-pattern')
        self.assertEqual('CONFIRMATION', confirming.next()['status'])
        confirming.confirm('no')
        form = report['next_action']['request']['inputs']['shared_form']
        self.answer(report, form + 'が繰り返される可能性がある。')
        draft = json.loads(self.engine.path.read_text())['drafts'][self.engine.draft_id('pattern')]
        self.assertEqual('rejected', draft['status'])
        self.assertEqual('superseded', draft['rejection_reason'])
        self.assertNotEqual('CONFIRMATION', self.new_engine('after-late-pattern').next()['status'])
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


class QuestionTypeConfigTests(unittest.TestCase):
    def test_all_sections_have_three_to_five_valid_content_questions(self):
        config = load_question_types()
        self.assertEqual('pending', config['owner_review'])
        for section in SECTIONS:
            types = config['sections'][section]
            self.assertTrue(3 <= len(types) <= 5)
            for item in types:
                self.assertEqual([], question_shape_failures(item['question'], item['scope_terms']))
                self.assertTrue(item['question'].endswith('？'))
                self.assertLessEqual(len(item['question']), 60)
            first = select_question_type(config, section, '予定を迷った')
            self.assertNotEqual(first['id'], select_question_type(config, section, '予定を迷った', first['id'])['id'])

    def test_config_rejects_invalid_versions_missing_sections_duplicates_and_bad_questions(self):
        original = load_question_types()
        mutations = [
            lambda c: c.update(revision=True),
            lambda c: c.update(contract_version='unknown'),
            lambda c: c.update(owner_review='unknown'),
            lambda c: c.update(extra='untrusted'),
            lambda c: c['sections'].pop('tensions'),
            lambda c: c['sections'].update(traits=c['sections']['states']),
            lambda c: c['sections']['tensions'].pop(),
            lambda c: c['sections']['tensions'][1].update(id=c['sections']['tensions'][0]['id']),
            lambda c: c['sections']['tensions'][0].update(question='何ですか？'),
            lambda c: c['sections']['tensions'][0].update(question='この出来事のとき、何をした場面がありましたか？'),
            lambda c: c['sections']['tensions'][0].update(question='この出来事のとき、ほかに気になったことは何ですか？'),
            lambda c: c['sections']['tensions'][0].update(scope_terms=[]),
            lambda c: c['sections']['tensions'][0].update(requires_event_text=[]),
            lambda c: c['sections']['tensions'][0].update(question='この出来事のとき、予定は？'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'types.yaml'
            for mutate in mutations:
                config = deepcopy(original)
                mutate(config)
                path.write_text(json.dumps(config, ensure_ascii=False))
                with self.subTest(mutation=mutate), self.assertRaisesRegex(ValueError, '^QUESTION_TYPES_INVALID$'):
                    load_question_types(path)
            path.write_text('question: [unterminated-private-value')
            with self.assertRaisesRegex(ValueError, '^QUESTION_TYPES_INVALID$'):
                load_question_types(path)

    def test_unbounded_variants_and_full_width_existence_questions_are_rejected(self):
        for word in ('ほか', '他にも', '他に', '他の', '何か', 'なにか', 'いつか', 'どこか', 'どれか', '誰か', 'だれか', 'そのうち'):
            with self.subTest(word=word):
                failures = question_shape_failures(f'この出来事のとき、{word}気になったことは何ですか？', ['この出来事のとき'])
                self.assertIn('no_unbounded_words', [failure['check'] for failure in failures])
        for ending in ('ありますか', 'ありましたか', 'ありませんか', 'あったか', 'あるのですか', 'ございますか', '有りますか'):
            with self.subTest(ending=ending):
                failures = question_shape_failures(f'この出来事のとき、何をした場面が{ending}？', ['この出来事のとき'])
                self.assertIn('no_existence_question', [failure['check'] for failure in failures])
        failures = question_shape_failures('この出来事のとき、ＡＩを使った場面がありますか？', ['この出来事のとき'])
        self.assertIn('no_existence_question', [failure['check'] for failure in failures])

    def test_childhood_scope_is_only_eligible_for_explicit_childhood_event(self):
        config = load_question_types()
        self.assertEqual('contexts-scene', select_question_type(config, 'contexts', '最近窓辺で休んだ')['id'])
        self.assertEqual('contexts-childhood', select_question_type(config, 'contexts', '小さい頃、窓辺で絵を描いた')['id'])
        self.assertEqual('contexts-scene', select_question_type(config, 'contexts', '小さい頃、窓辺で絵を描いた', 'contexts-childhood')['id'])
