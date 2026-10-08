"""Profile-local, one-value inference and owner confirmation (Issue #138).

Requests are transient stdout for a relay; checkpoints and drafts never leave
profile growth/. Confirmation publishes an existing-schema hypothesis, never
promotes confidence or support merely because the owner said yes.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
import yaml

try:
    from kb import Entity, discover_entities, load_yaml, serialize_markdown, validate_entities, vocabularies
    from profile_root import atomic_write_text, resolve_profile_root
    from intake_conversation import IntakeError, _scan_direct_identifiers
    from export_signals import _signals, _signal_groups
except ModuleNotFoundError:
    from tools.kb import Entity, discover_entities, load_yaml, serialize_markdown, validate_entities, vocabularies
    from tools.profile_root import atomic_write_text, resolve_profile_root
    from tools.intake_conversation import IntakeError, _scan_direct_identifiers
    from tools.export_signals import _signals, _signal_groups

STATE_CONTRACT = 'growth-elements/v1'
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
MAX_ATTEMPTS = 5
QUESTION_TYPES_PATH = Path(__file__).resolve().parents[1] / 'config' / 'element-question-types.yaml'
# Normalize before checking, including full-width spelling in inferred values.
EXISTENCE_QUESTION = re.compile(r'(?:ありました|あります|あった|ある|ありません|なかった|ない|ございます|ございました|有りました|有ります)(?:の)?(?:です|んです|でしょう)?か')
UNBOUNDED_WORDS = ('ほか', '他にも', '他に', '他の', '何か', 'なにか', 'いつか', 'どこか', 'どれか', '誰か', 'だれか', 'そのうち')
QUESTION_CHECKS = ('no_existence_question', 'no_unbounded_words', 'contains_scope_terms', 'asks_content')
# Explicit downstream priority breaks equal coverage ties, never alphabetical.
SECTIONS = {
    'tensions': ('両立しにくい二つの思いや行動', '本人が確認した両立しにくい思いが、次から制作のテーマの材料になります。'),
    'recurring_patterns': ('別の出来事にも現れる同じ言葉や行動の形', '同じ言葉や形が2件以上の出来事に出て、本人が確認したパターンだけが制作のテーマの材料になります。'),
    'seeks': ('得たいものや近づきたい状態', '本人が確認した得たいものが、次から制作のテーマの材料になります。'),
    'avoids': ('遠ざけたいことや離れたい状態', '本人が確認した遠ざけたいことが、次から制作のテーマの材料になります。'),
    'protects': ('失いたくないものや守りたい状態', '本人が確認した守りたいものが、次から制作のテーマの材料になります。'),
    'states': ('そのときだけの気持ちや反応', '本人が確認したそのときの反応が、次から制作の材料になります。'),
    'contexts': ('場所や相手などの場面によって変わる反応', '本人が確認した場面による違いが、次から制作の材料になります。'),
}
SECTION_FIELDS = {
    'tensions': {'claim-layer': 'tension'},
    'seeks': {'claim-layer': 'motivation', 'claim-direction': 'seek'},
    'avoids': {'claim-layer': 'motivation', 'claim-direction': 'avoid'},
    'protects': {'claim-layer': 'motivation', 'claim-direction': 'protect'},
    'states': {'claim-scope': 'state'},
    'contexts': {'claim-scope': 'context-bound'},
}
# Hiragana attached to another Japanese script is usually a particle or an
# inflection here. Only isolated runs can qualify; omit common function words.
HIRAGANA_FUNCTION_WORDS = frozenset(('これ', 'それ', 'あれ', 'ここ', 'そこ', 'どこ',
    'こと', 'もの', 'とき', 'ため', 'よう', 'ながら', 'ので', 'から', 'まで',
    'です', 'でした', 'ます', 'ました', 'した', 'して', 'いた', 'いる',
    'だった', 'たい', 'ない', 'どう', 'どうですか', 'その', 'この', 'あの',
    'していた', 'している', 'ていた', 'ている', 'でいた', 'でいる'))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n'


def scan_private(text):
    normalized = unicodedata.normalize('NFKC', text).casefold()
    _scan_direct_identifiers(normalized)
    # Unicode word boundaries can hide an address/URL joined to Japanese.
    if '@' in normalized or 'http://' in normalized or 'https://' in normalized:
        raise IntakeError('direct-identifier-detected')


def terms(text):
    """Conservative literal content-word candidates, at least two characters.

    No stemming or substring matching: Japanese particles/inflections adjoining
    kanji are not words. An isolated hiragana word can be delimited by quotes
    in a question to preserve the same boundary as the supplied raw utterance.
    """
    normalized = unicodedata.normalize('NFKC', text).casefold()
    result = set()
    for match in re.finditer(r'[一-龥々]+|[ぁ-ゖ]+|[ァ-ヶー]+|[a-z][a-z0-9]+', normalized):
        word = match.group()
        if len(word) < 2:
            continue
        if re.fullmatch(r'[ぁ-ゖ]+', word):
            neighbors = normalized[max(0, match.start() - 1):match.start()] + normalized[match.end():match.end() + 1]
            if word in HIRAGANA_FUNCTION_WORDS or re.search(r'[一-龥々ァ-ヶー]', neighbors):
                continue
        result.add(word)
    return result


def raw_text(event):
    return next((q['text'] for q in event.meta.get('raw_voice') or []
                 if isinstance(q, dict) and isinstance(q.get('text'), str) and q['text'].strip()), '')[:120]


def observed_order(event):
    value = (event.meta.get('time') or {}).get('observed_at')
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp(), event.id
    except (ValueError, TypeError):
        return float('-inf'), event.id


def select_section(entities, last_section=None):
    counts = {key: set() for key in SECTIONS}
    for signal in _signals(entities):
        for key, values in _signal_groups(signal, signal['statement']).items():
            if key in counts and values:
                counts[key].update(signal['evidence_refs'])
    return min(counts, key=lambda key: (key == last_section, len(counts[key]), list(SECTIONS).index(key)))


def question_shape_failures(value, scope_terms):
    """Literal safeguards for the one inferred rewording, also used for config."""
    normalized = unicodedata.normalize('NFKC', value).casefold()
    failures = []
    if EXISTENCE_QUESTION.search(normalized):
        failures.append({'check': 'no_existence_question', 'reason': 'Ask for content, not whether something exists.'})
    if any(word in normalized for word in UNBOUNDED_WORDS):
        failures.append({'check': 'no_unbounded_words', 'reason': 'Remove words that leave the scope unbounded.'})
    if not all(unicodedata.normalize('NFKC', term).casefold() in normalized for term in scope_terms):
        failures.append({'check': 'contains_scope_terms', 'reason': 'Keep every premise phrase of the selected question type.'})
    if not any(word in normalized for word in ('何', 'どんな', 'どの')):
        failures.append({'check': 'asks_content', 'reason': 'Ask for content using 何, どんな or どの.'})
    return failures


def load_question_types(path=QUESTION_TYPES_PATH):
    """Reject incomplete/unsafe proposals instead of inventing a fallback."""
    try:
        config = load_yaml(path)
        if (set(config) != {'contract_version', 'revision', 'owner_review', 'owner_reviewed_at', 'selection', 'sections'}
                or config['contract_version'] != 'element-question-types/v1'
                or type(config['revision']) is not int or config['revision'] < 1
                or config['owner_review'] not in ('pending', 'approved')
                or not isinstance(config['owner_reviewed_at'], str)
                or set(config['selection']) != {'standalone_below_event_count'}
                or type(config['selection']['standalone_below_event_count']) is not int
                or config['selection']['standalone_below_event_count'] < 1
                or set(config['sections']) != set(SECTIONS)):
            raise ValueError
        date.fromisoformat(config['owner_reviewed_at'])
        ids = set()
        for types in config['sections'].values():
            if not isinstance(types, list):
                raise ValueError
            if (not 3 <= sum(item.get('basis') == 'event' for item in types) <= 5
                    or not 1 <= sum(item.get('basis') == 'standalone' for item in types) <= 2):
                raise ValueError
            for item in types:
                if (set(item) - {'id', 'basis', 'question', 'scope_terms', 'requires_event_text'}
                        or not {'id', 'basis', 'question', 'scope_terms'} <= set(item)
                        or item['basis'] not in ('event', 'standalone')
                        or not isinstance(item['id'], str) or not SAFE_ID.fullmatch(item['id'])
                        or item['id'] in ids):
                    raise ValueError
                ids.add(item['id'])
                for field in ('scope_terms', 'requires_event_text'):
                    if field in item and (not isinstance(item[field], list) or not item[field]
                            or any(not isinstance(term, str) or not term.strip() for term in item[field])):
                        raise ValueError
                text = item['question']
                if (not isinstance(text, str) or not text.strip() or len(text) > 60
                        or '\n' in text or len(re.findall(r'[。.!?！？]', text)) != 1
                        or not text.endswith(('?', '？')) or question_shape_failures(text, item['scope_terms'])):
                    raise ValueError
                if item['basis'] == 'event':
                    if text.count('この出来事') != 1 or 'この出来事のとき' not in item['scope_terms']:
                        raise ValueError
                elif 'requires_event_text' in item or 'この出来事' in text:
                    raise ValueError
                scan_private(text)
        return config
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IntakeError, yaml.YAMLError):
        raise ValueError('QUESTION_TYPES_INVALID') from None


def select_question_type(config, section, event_text, last_type=None, prefer_standalone=False):
    normalized = unicodedata.normalize('NFKC', event_text).casefold()
    eligible = [item for item in config['sections'][section]
                if (item['basis'] == 'standalone' or event_text)
                and all(unicodedata.normalize('NFKC', phrase).casefold() in normalized
                       for phrase in item.get('requires_event_text', []))]
    if not eligible:
        raise ValueError('QUESTION_TYPES_INVALID')
    # Config order breaks ties. A heard type is remembered per section.
    preferred_basis = 'standalone' if prefer_standalone or not event_text else 'event'
    preferred = [item for item in eligible if item['basis'] == preferred_basis]
    item = next((item for item in preferred if item['id'] != last_type),
                next((item for item in eligible if item['id'] != last_type), eligible[0]))
    selected = {'contract_version': config['contract_version'], 'revision': config['revision'], **deepcopy(item)}
    if item['basis'] == 'event':
        # The premise's referent is a noun phrase; its time connector stays fixed.
        selected['scope_terms'] = [term.replace('この出来事', '') for term in item['scope_terms']]
    return selected


def event_noun_phrase(value, selected):
    """Extract a bounded nominal phrase from the single text answer.

    This is a conservative surface check, not a Japanese semantic parser.
    Preserve the template prefix and time connector, and reject word-only
    quotation insertions, particles and verbal endings in the noun slot.
    """
    normalized = unicodedata.normalize('NFKC', value).casefold()
    prefix = selected['question'].split('この出来事', 1)[0]
    if 'この出来事' in normalized or not normalized.startswith(prefix):
        return None
    phrase, separator, _ = normalized[len(prefix):].partition('のとき')
    if (not separator or not 2 <= len(phrase) <= 40 or not terms(phrase)
            or re.search(r'[、,。.!?！？\n]', phrase)
            or re.fullmatch(r'[「『“\"]+.*[」』”\"]+', phrase)
            or phrase.endswith(('は', 'が', 'を', 'に', 'へ', 'と', 'で', 'の', 'も', 'や',
                                'から', 'まで', 'より', 'って', 'ので', 'けど', 'ながら'))
            or not (re.search(r'[一-龥々ァ-ヶーa-z0-9]$', phrase)
                    or phrase.endswith(('こと', 'もの', 'とき', 'ところ', 'ひととき')))):
        return None
    return phrase


def repetition(events, anchor_id):
    """Count distinct Events sharing a literal word or structured action/trigger.

    Only witnesses including this answer Event qualify; duplicate quotes inside
    one Event cannot meet the condition. Two witnesses permit a hypothesis,
    never a supported Pattern or a certainty upgrade.
    """
    candidates = {}
    for event in events:
        if not raw_text(event):
            continue
        forms = {(1, word) for word in terms(raw_text(event))}
        for field in ('trigger', 'action'):
            value = event.meta.get(field)
            for text in value if isinstance(value, list) else [value]:
                if isinstance(text, str) and terms(text):
                    forms.add((0, unicodedata.normalize('NFKC', text).casefold().strip()))
        for priority, form in forms:
            candidates.setdefault((priority, form), set()).add(event.id)
    eligible = [(priority, form, sorted(ids)) for (priority, form), ids in candidates.items()
                if len(ids) >= 2 and anchor_id in ids]
    if not eligible:
        return None
    _, form, ids = min(eligible, key=lambda item: (item[0], -len(item[1]), item[1]))
    return form, ids


def _safe_file(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('UNSAFE_PROFILE_STATE')


class HearingElements:
    def __init__(self, profile_root, run_id, purpose, subject=None):
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
            raise ValueError('INVALID_RUN_ID')
        self.layout = resolve_profile_root(profile_root)
        subjects = self.layout.subject_ids
        self.subject = subject or (subjects[0] if len(subjects) == 1 else None)
        if self.subject not in subjects:
            raise ValueError('SUBJECT_REQUIRED')
        if purpose not in vocabularies()['allowed_purposes']:
            raise ValueError('INVALID_PURPOSE')
        self.run_id, self.purpose = run_id, purpose
        self.directory = self.layout.root / 'growth' / 'elements'
        self.path = self.directory / 'state.json'

    @contextmanager
    def locked(self):
        # All runs share a confirmation ledger and one lock, avoiding duplicate
        # confirmations. Reject every directory/file alias before reads/writes.
        for directory in (self.directory.parent, self.directory):
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                raise ValueError('UNSAFE_PROFILE_STATE')
            directory.mkdir(mode=0o700, exist_ok=True)
        _safe_file(self.path)
        fd = os.open(self.directory / 'state.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.path.read_text()) if self.path.exists() else {
                    'contract_version': STATE_CONTRACT, 'runs': {}, 'drafts': {}}
                if state.get('contract_version') != STATE_CONTRACT:
                    raise ValueError('INVALID_PROFILE_STATE')
                yield state
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def save(self, state):
        atomic_write_text(self.path, canonical(state))
        os.chmod(self.path, 0o600)

    def entities(self):
        try:
            entities = discover_entities(self.layout.entity_root)
        except (OSError, ValueError):
            # parse_markdown errors include the path and malformed YAML body.
            # Keep the public API safe too, not just the CLI error renderer.
            raise ValueError('ENTITIES_INVALID') from None
        # Existing root resolver doesn't inspect all nested entity aliases.
        for entity in entities:
            if entity.path.is_symlink() or entity.path.parent.is_symlink():
                raise ValueError('UNSAFE_ENTITY_PATH')
        if validate_entities(entities, root=self.layout.root):
            raise ValueError('ENTITIES_INVALID')
        return entities

    def consent(self, entities):
        try:
            from growth_tasks import _consent_is_valid_for_hearing, _select_hearing_source
        except ModuleNotFoundError:
            from tools.growth_tasks import _consent_is_valid_for_hearing, _select_hearing_source
        selected = [e for e in entities if e.meta.get('subject') == self.subject or e.id == self.subject]
        source = _select_hearing_source(selected, self.subject, self.purpose)
        sources = {e.id: e for e in selected if e.type == 'source'}
        # Same required operations as hearing, checked anew at every transition.
        if source is None or any(not _consent_is_valid_for_hearing(e.meta.get('consent'), self.purpose, date.today())
                                 for e in sources.values()):
            raise ValueError('CONSENT_INVALID')
        for event in (e for e in selected if e.type == 'event'):
            refs = event.meta.get('source_refs') or []
            for quote in event.meta.get('raw_voice') or []:
                if not isinstance(quote, dict) or quote.get('source_ref') not in refs or quote.get('source_ref') not in sources:
                    raise ValueError('RAW_VOICE_SOURCE_INVALID')
        return selected, source

    def run(self, state):
        run = state['runs'][self.run_id]
        if (run['subject'], run['purpose']) != (self.subject, self.purpose):
            raise ValueError('RUN_IDENTITY_MISMATCH')
        return run

    def request(self, run, step, instruction, inputs, fmt, checks=()):
        run.update(status='WAITING', step=step, pending={
            'contract_version': 'element-request/v1', 'run_id': self.run_id,
            'element_id': 'A1.' + step, 'attempt': 1, 'instruction': instruction,
            'inputs': inputs, 'answer_format': fmt, 'checks': list(checks), 'previous_failure': None})

    def text_request(self, run, step, instruction, inputs, max_chars=120):
        self.request(run, step, instruction, inputs, {'type': 'text', 'max_chars': max_chars},
                     ('non_empty', 'single_sentence', f'max_chars:{max_chars}', 'forbidden_tokens', 'privacy'))

    def choice_request(self, run, step, instruction, inputs, choices):
        self.request(run, step, instruction, inputs, {'type': 'choice', 'choices': choices}, ('one_of',))

    def report(self, run):
        # Do not echo accepted raw values, history or drafts to the parent.
        action = None
        if run['status'] == 'WAITING':
            action = {'kind': 'element', 'request': deepcopy(run['pending'])}
        elif run['status'] in ('HEARING', 'CONFIRMATION', 'SEED_REQUIRED'):
            action = {'kind': 'hearing', 'question': run['question'], 'why': run.get('why'),
                      'answer_format': 'yes-no' if run['status'] == 'CONFIRMATION' else 'event-block'}
        return {'run_id': self.run_id, 'status': run['status'], 'next_action': action,
                'blocked': run.get('blocked')}

    def next(self):
        with self.locked() as state:
            entities, _ = self.consent(self.entities())
            superseded = self.reject_dependents(state)
            if self.run_id in state['runs']:
                run = self.run(state)
                if run['status'] == 'CONFIRMATION':
                    self.start_hearing(state, run, entities)
                    self.save(state)
                elif superseded:
                    self.save(state)
                if (run['status'] == 'WAITING' and run['step'] == 'hearing-question'
                        and 'basis' not in run.get('question_type', {})):
                    # Upgrade old pending questions without accepting an unbounded
                    # legacy value or resetting the failure budget.
                    attempt = run['pending']['attempt']
                    anchor = next((e for e in entities if e.id == run.get('anchor')), None)
                    if anchor is None or self.fingerprint(anchor) != run.get('anchor_fingerprint'):
                        raise ValueError('EVENT_CHANGED')
                    self.start_question(state, run, entities)
                    run['pending']['attempt'] = attempt
                    self.save(state)
                return self.report(run)
            run = {'subject': self.subject, 'purpose': self.purpose, 'values': {}, 'pending': None}
            state['runs'][self.run_id] = run
            pending = [key for key, draft in state['drafts'].items()
                       if draft['subject'] == self.subject and draft['purpose'] == self.purpose
                       and draft['origin_run'] != self.run_id and draft['status'] == 'pending']
            # Snapshot only earlier runs' drafts. The current run cannot approve
            # its own inferred material. Ask one owner yes/no at a time.
            run['confirmations'] = sorted(pending)
            self.start_hearing(state, run, entities)
            self.save(state)
            return self.report(run)

    @staticmethod
    def reject_dependents(state):
        """A rejected Claim supersedes dependent drafts, including old ledgers."""
        changed = False
        while True:
            rejected = {key for key, draft in state['drafts'].items() if draft['status'] == 'rejected'}
            dependents = [draft for draft in state['drafts'].values()
                          if draft['status'] == 'pending' and rejected.intersection(draft['meta'].get('claim_refs', []))]
            if not dependents:
                return changed
            for draft in dependents:
                draft.update(status='rejected', rejection_reason='superseded')
            changed = True

    def start_hearing(self, state, run, entities):
        self.reject_dependents(state)
        while run['confirmations']:
            key = run['confirmations'][0]
            draft = state['drafts'][key]
            if draft['status'] == 'pending':
                run.update(status='CONFIRMATION', draft_id=key,
                           question=f"「{draft['statement']}」は、あなたの実感に合っていますか？",
                           why='はいと確認した内容だけが、制作へ渡す材料になります。')
                return
            run['confirmations'].pop(0)
        previous = state.get('last_heard', {}).get(self.subject + ':' + self.purpose)
        section = select_section(entities, previous)
        run['section'] = section
        self.start_question(state, run, entities)

    def start_question(self, state, run, entities):
        key = self.subject + ':' + self.purpose
        config = load_question_types()
        event_count = sum(e.type == 'event' for e in entities)
        previous_event = state.get('last_heard_event', {}).get(key)
        excluded = {previous_event} if previous_event else {
            # Canonical JSON sorts run IDs, so old ledger order is not a clock.
            # Conservatively avoid every previously heard legacy anchor until
            # a new accepted question establishes the explicit last index.
            r['anchor'] for r in state['runs'].values()
            if r['subject'] == self.subject and r['purpose'] == self.purpose
            and r.get('heard_section') and r.get('anchor')}
        events = sorted((e for e in entities if e.type == 'event' and raw_text(e) and terms(raw_text(e))),
                        key=observed_order, reverse=True)
        event = next((event for event in events if event.id not in excluded), None)
        run['anchor'] = event.id if event else None
        run['anchor_fingerprint'] = self.fingerprint(event) if event else None
        self.question_request(state, run, raw_text(event) if event else '', config,
                              event_count < config['selection']['standalone_below_event_count'])

    def question_request(self, state, run, event_text, config, prefer_standalone):
        key = self.subject + ':' + self.purpose
        last_type = state.get('last_question_type', {}).get(key, {}).get(run['section'])
        selected = select_question_type(config, run['section'], event_text, last_type, prefer_standalone)
        # Pin the selected revision/premises across retries and config updates.
        run['question_type'] = selected
        instruction = '選ばれたquestion_typeの問いだけを一文で言い換えてください。問いの中身とscope_termsの全前提語を残し、範囲を広げたり別の型を選んだりしないでください。何・どんな・どので中身を聞き、？で終えてください。有無の質問と範囲のない語は禁止です。'
        inputs = {'item_description': SECTIONS[run['section']][0], 'question_type': deepcopy(selected)}
        if selected['basis'] == 'event':
            inputs['recent_event'] = event_text
            instruction += 'この出来事を、本人の出来事の内容語を含む40字以内の短い名詞句に置き換えてください。助詞で終わる句、引用した単語だけの挿入は禁止です。'
        else:
            run['anchor'] = run['anchor_fingerprint'] = None
            instruction += '記録済みの出来事は使わず、この型の前提だけで問いを完結させてください。型の文をそのまま返しても構いません。'
        self.text_request(run, 'hearing-question', instruction, inputs, 60)
        run['pending']['checks'] += ['ends_with_question', 'no_production_context', *QUESTION_CHECKS]
        if selected['basis'] == 'event':
            run['pending']['checks'] += ['event_noun_phrase', 'contains_event_term']
        else:
            run['pending']['checks'].append('standalone_question')

    def failures(self, run, value):
        request = run['pending']
        fmt = request['answer_format']
        failures = []
        def fail(check, reason):
            failures.append({'check': check, 'reason': reason})
        if not isinstance(value, str) or not value.strip():
            return [{'check': 'non_empty', 'reason': 'Return one non-empty string.'}]
        if fmt['type'] == 'choice':
            if value not in fmt['choices']:
                fail('one_of', 'Choose exactly one listed value.')
            return failures
        if len(value) > fmt['max_chars']:
            fail('max_chars', 'Use at most the declared character limit.')
        if '\n' in value or len(re.findall(r'[。.!?！？]', value)) > 1:
            fail('single_sentence', 'Return one sentence only.')
        try:
            from growth_tasks import question_bank_forbidden_tokens
        except ModuleNotFoundError:
            from tools.growth_tasks import question_bank_forbidden_tokens
        normalized = unicodedata.normalize('NFKC', value).casefold()
        forbidden = (set(question_bank_forbidden_tokens()) | set(SECTIONS)
                     | {part for section in SECTIONS for part in section.split('_')}
                     | set(vocabularies()['claim_layers']) | set(vocabularies()['motivation_directions'])
                     | {'belief'})
        if any(unicodedata.normalize('NFKC', token).casefold() in normalized for token in forbidden):
            fail('forbidden_tokens', 'Remove internal schema vocabulary.')
        try:
            scan_private(value)
        except IntakeError:
            fail('privacy', 'Remove direct identifiers and URLs.')
        if any(word in normalized for word in ('テーマ', 'slug', '依頼文', 'プロンプト', 'theme', 'prompt')):
            fail('no_production_context', 'Do not refer to production instructions or a theme.')
        if run['step'] == 'hearing-question':
            # Old pending states must go through next() to bind a type first.
            if 'basis' not in run.get('question_type', {}):
                raise ValueError('QUESTION_TYPE_REQUIRED')
            failures.extend(question_shape_failures(value, run['question_type']['scope_terms']))
            if not value.endswith(('?', '？')):
                fail('ends_with_question', 'End the question with ? or ？.')
            if run['question_type']['basis'] == 'event':
                phrase = event_noun_phrase(value, run['question_type'])
                if phrase is None:
                    fail('event_noun_phrase', 'Replace the Event placeholder with a short nominal phrase, not a quoted word or a particle ending.')
                if not terms(phrase or '').intersection(terms(request['inputs']['recent_event'])):
                    fail('contains_event_term', 'Include an exact Event content word in the nominal phrase.')
                if run['anchor'].split('/', 1)[1] in normalized:
                    fail('no_production_context', 'Do not name a record slug.')
            elif 'この出来事' in normalized:
                fail('standalone_question', 'Keep this question independent of any recorded Event.')
        if run['step'] == 'alternative-2' and value == run['values']['alternative-1']:
            fail('distinct_alternative', 'Give a different explanation.')
        if run['step'] == 'pattern-statement' and unicodedata.normalize('NFKC', run['repetition']['form']).casefold() not in normalized:
            fail('contains_shared_form', 'Include the mechanically counted shared word or form.')
        # A derived sentence may use the person's words (e.g. a one-word
        # feeling), but must not merely return the raw quote as its value.
        def unquoted(text):
            return unicodedata.normalize('NFKC', text).casefold().strip(' \t\n。.!?！？「」『』"“”')
        if run['step'] != 'hearing-question' and run.get('event_text') and unquoted(run['event_text']) == unquoted(value):
            fail('derived_only', 'Write a derived statement rather than copying the full raw quote.')
        return failures

    def answer(self, answer):
        # Closed parent envelope. Rejected values are never interpolated into
        # errors / previous_failure / stdout / logs.
        if not isinstance(answer, dict) or set(answer) != {'contract_version', 'run_id', 'element_id', 'attempt', 'value'}:
            raise ValueError('INVALID_ANSWER_CONTRACT')
        value = answer['value']
        valid_value = isinstance(value, str) or (
            isinstance(value, dict) and set(value) == {'answer', 'reason'} and
            type(value['answer']) is bool and isinstance(value['reason'], str))
        if (answer['contract_version'] != 'element-answer/v1' or type(answer['attempt']) is not int
                or answer['attempt'] < 1 or not valid_value):
            raise ValueError('INVALID_ANSWER_CONTRACT')
        with self.locked() as state:
            entities, _ = self.consent(self.entities())
            run = self.run(state)
            request = run['pending']
            if run['status'] != 'WAITING' or any(answer[key] != request[key] for key in ('run_id', 'element_id', 'attempt')):
                raise ValueError('STALE_ANSWER')
            event_id = run['anchor'] if run['step'] == 'hearing-question' else run.get('event')
            fingerprint = run['anchor_fingerprint'] if run['step'] == 'hearing-question' else run['event_fingerprint']
            event = next((e for e in entities if e.id == event_id), None)
            standalone_question = (run['step'] == 'hearing-question'
                                   and run.get('question_type', {}).get('basis') == 'standalone')
            if not standalone_question and (event is None or self.fingerprint(event) != fingerprint):
                raise ValueError('EVENT_CHANGED')
            if run['step'] == 'pattern-statement':
                current = {e.id: self.fingerprint(e) for e in entities if e.id in run['pattern_fingerprints']}
                if current != run['pattern_fingerprints']:
                    raise ValueError('EVENT_CHANGED')
            failures = self.failures(run, answer['value'])
            if failures:
                request['previous_failure'] = failures
                if request['attempt'] >= MAX_ATTEMPTS:
                    run.update(status='BLOCKED', blocked={'element_id': request['element_id'], 'failures': failures})
                else:
                    request['attempt'] += 1
            else:
                run['values'][run['step']] = answer['value']
                self.advance(state, run, entities)
            self.save(state)
            return self.report(run)

    def advance(self, state, run, entities):
        step, values = run['step'], run['values']
        if step == 'hearing-question':
            run.update(status='HEARING', pending=None, question=values[step], why=SECTIONS[run['section']][1],
                       heard_section=run['section'])
            state.setdefault('last_heard', {})[self.subject + ':' + self.purpose] = run['section']
            state.setdefault('last_question_type', {}).setdefault(self.subject + ':' + self.purpose, {})[run['section']] = run['question_type']['id']
            if run.get('anchor'):
                state.setdefault('last_heard_event', {})[self.subject + ':' + self.purpose] = run['anchor']
        elif step in ('claim-statement', 'claim-layer', 'claim-direction', 'claim-scope'):
            if step == 'claim-statement':
                values['claim-evidence'] = run['event']
                values.update(SECTION_FIELDS.get(run['section'], {}))
            if 'claim-layer' not in values:
                self.choice_request(run, 'claim-layer', '主張の層を一つ選んでください。',
                                    {'statement': values['claim-statement']}, vocabularies()['claim_layers'])
            elif values['claim-layer'] == 'motivation' and 'claim-direction' not in values:
                self.choice_request(run, 'claim-direction', '主張が示す動機の方向を一つ選んでください。',
                                    {'statement': values['claim-statement']}, vocabularies()['motivation_directions'])
            elif 'claim-scope' not in values:
                self.choice_request(run, 'claim-scope', 'この一件の主張がその時だけか、場面の条件によるものかを選んでください。',
                                    {'statement': values['claim-statement'], 'event': run['event_text']}, ['state', 'context-bound'])
            elif values['claim-scope'] == 'context-bound' and 'claim-condition' not in values:
                self.text_request(run, 'claim-condition', 'この主張が当てはまる場面の条件を一文で書いてください。',
                                  {'statement': values['claim-statement'], 'event': run['event_text']})
            else:
                self.request_alternative(run)
        elif step == 'claim-condition':
            self.request_alternative(run)
        elif step == 'alternative-1':
            self.text_request(run, 'alternative-2', '先の説明とは異なる、もう一つの説明を一文で書いてください。',
                              {'event': run['event_text'], 'statement': values['claim-statement'], 'previous_explanation': values[step]})
            run['pending']['checks'].append('distinct_alternative')
        elif step == 'alternative-2':
            self.store_claim(state, run)
            events = [e for e in entities if e.type == 'event']
            repeated = repetition(events, run['event'])
            if repeated:
                form, refs = repeated
                run['repetition'] = {'form': form, 'evidence': refs}
                run['pattern_fingerprints'] = {e.id: self.fingerprint(e) for e in events if e.id in refs}
                witnesses = [run['event'], next(ref for ref in refs if ref != run['event'])]
                self.text_request(run, 'pattern-statement', '複数の出来事に現れた共通の形を、可能性として一文で書いてください。',
                                  {'shared_form': form, 'events': {e.id: raw_text(e) for e in events if e.id in witnesses}})
                run['pending']['checks'].append('contains_shared_form')
            else:
                run.update(status='COMPLETED', pending=None)
        elif step == 'pattern-statement':
            if repetition([e for e in entities if e.type == 'event'], run['event']) != (run['repetition']['form'], run['repetition']['evidence']):
                raise ValueError('REPETITION_CHANGED')
            self.store_pattern(state, run, entities)
            run.update(status='COMPLETED', pending=None)

    def request_alternative(self, run):
        values = run['values']
        self.text_request(run, 'alternative-1', '同じ出来事を別に説明できる可能性を一文で書いてください。',
                          {'event': run['event_text'], 'statement': values['claim-statement']})

    def draft_id(self, kind):
        digest = hashlib.sha256(canonical([self.run_id, self.subject, self.purpose]).encode()).hexdigest()[:24]
        return f'{kind}/hearing-{digest}'

    def draft(self, state, run, meta, statement):
        key = meta['id']
        refs = meta.get('supporting_evidence', meta.get('evidence', []))
        evidence = {e.id: self.fingerprint(e) for e in self.entities() if e.id in refs}
        state['drafts'][key] = {'status': 'pending', 'subject': self.subject, 'purpose': self.purpose,
                                'origin_run': self.run_id, 'statement': statement, 'meta': meta,
                                'evidence_fingerprints': evidence}
        # Keep drafts outside entities until owner confirmation, so no existing
        # export path (including legacy export) can see unconfirmed material.

    @staticmethod
    def fingerprint(entity):
        return hashlib.sha256(serialize_markdown(entity).encode()).hexdigest()

    def store_claim(self, state, run):
        v = run['values']
        meta = {'id': self.draft_id('claim'), 'type': 'claim', 'subject': self.subject,
                'created': date.today().isoformat(), 'updated': date.today().isoformat(),
                'statement': v['claim-statement'], 'layer': v['claim-layer'],
                'motivation_direction': v.get('claim-direction'), 'scope': v['claim-scope'],
                'conditions': [v['claim-condition']] if 'claim-condition' in v else [],
                'supporting_evidence': [v['claim-evidence']], 'counterevidence': None,
                'alternative_explanations': [v['alternative-1'], v['alternative-2']],
                'confidence': 'unknown', 'status': 'hypothesis', 'supersedes': None, 'superseded_by': None}
        self.draft(state, run, meta, meta['statement'])

    def store_pattern(self, state, run, entities):
        refs = run['repetition']['evidence']
        contexts = sorted({c for e in entities if e.id in refs for c in e.meta.get('context', {}).get('domains', [])})
        meta = {'id': self.draft_id('pattern'), 'type': 'pattern', 'subject': self.subject,
                'created': date.today().isoformat(), 'updated': date.today().isoformat(),
                'condition': run['values']['pattern-statement'], 'recurring_appraisal': None,
                'recurring_drive': None, 'recurring_action': None, 'reinforcement': None,
                'contexts_seen': contexts, 'evidence': refs, 'claim_refs': [self.draft_id('claim')], 'counterevidence': None,
                'confidence': 'unknown', 'status': 'hypothesis'}
        self.draft(state, run, meta, meta['condition'])
        self.reject_dependents(state)

    def respond(self, text):
        try:
            from growth_tasks import _write_hearing_event, _HearingAnswerRejected
        except ModuleNotFoundError:
            from tools.growth_tasks import _write_hearing_event, _HearingAnswerRejected
        with self.locked() as state:
            entities, source = self.consent(self.entities())
            run = self.run(state)
            if run['status'] not in ('HEARING', 'SEED_REQUIRED'):
                raise ValueError('NOT_WAITING_FOR_OWNER')
            try:
                scan_private(text)
            except IntakeError:
                raise ValueError('ANSWER_INVALID:direct-identifier-detected') from None
            try:
                event_id, path = _write_hearing_event(self.layout, text, subject_id=self.subject, source=source)
            except _HearingAnswerRejected as error:
                raise ValueError('ANSWER_INVALID:' + error.code) from None
            try:
                entities = self.entities()
                event = next(e for e in entities if e.id == event_id)
                if not raw_text(event):
                    raise ValueError('RAW_VOICE_REQUIRED')
                run.update(event=event_id, event_text=raw_text(event), event_fingerprint=self.fingerprint(event))
                if run['status'] == 'SEED_REQUIRED':
                    run['heard_section'] = run['section']
                    state.setdefault('last_heard', {})[self.subject + ':' + self.purpose] = run['section']
                self.text_request(run, 'claim-statement', 'この出来事から読み取れることを、可能性として一文で書いてください。',
                                  {'event': run['event_text'], 'item_description': SECTIONS[run['section']][0]})
                self.save(state)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return self.report(run)

    def confirm(self, answer):
        if answer not in ('yes', 'no'):
            raise ValueError('OWNER_YES_NO_REQUIRED')
        with self.locked() as state:
            entities, _ = self.consent(self.entities())
            run = self.run(state)
            if run['status'] != 'CONFIRMATION':
                raise ValueError('NOT_WAITING_FOR_CONFIRMATION')
            self.reject_dependents(state)
            draft = state['drafts'][run['draft_id']]
            if draft['status'] == 'rejected' and draft.get('rejection_reason') == 'superseded':
                self.start_hearing(state, run, entities)
                self.save(state)
                return self.report(run)
            if draft['status'] != 'pending':
                raise ValueError('CONFIRMATION_CHANGED')
            path = None
            if answer == 'yes':
                meta = draft['meta']
                if any(state['drafts'].get(ref, {}).get('status') != 'confirmed' for ref in meta.get('claim_refs', [])):
                    raise ValueError('CLAIM_CONFIRMATION_REQUIRED')
                current = {e.id: self.fingerprint(e) for e in entities if e.id in draft['evidence_fingerprints']}
                if current != draft['evidence_fingerprints']:
                    raise ValueError('EVENT_CHANGED')
                plural = {'claim': 'claims', 'pattern': 'patterns'}[meta['type']]
                path = self.layout.entity_root / plural / (meta['id'].split('/', 1)[1] + '.md')
                if path.parent.is_symlink() or path.exists() or path.is_symlink():
                    raise ValueError('ENTITY_ALREADY_EXISTS_OR_UNSAFE')
                entity = Entity(path=path, meta=deepcopy(meta), body='\n')
                if validate_entities([*self.entities(), entity], root=self.layout.root):
                    raise ValueError('DRAFT_INVALID')
                atomic_write_text(path, serialize_markdown(entity))
            try:
                draft['status'] = 'confirmed' if answer == 'yes' else 'rejected'
                self.reject_dependents(state)
                run['confirmations'].pop(0)
                self.start_hearing(state, run, self.consent(self.entities())[0])
                self.save(state)
            except BaseException:
                if path is not None:
                    path.unlink(missing_ok=True)
                raise
            return self.report(run)

    def skip(self):
        with self.locked() as state:
            self.consent(self.entities())
            run = self.run(state)
            if run['status'] not in ('HEARING', 'CONFIRMATION', 'SEED_REQUIRED'):
                raise ValueError('NOT_WAITING_FOR_OWNER')
            run.update(status='SKIPPED', pending=None)
            self.save(state)
            return self.report(run)
