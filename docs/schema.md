# Self Model Schema v0.1

要件SSOTはIssue #1。本書は実装上のデータ設計SSOTです。1 entity = 1 Markdown、frontmatterを正本とします。

## 共通規則

```yaml
---
id: event/example
type: event
subject: subject/example
status: draft             # draft | active | revised | rejected | archived
created: 2026-08-11
updated: 2026-08-11
---
```

- IDは`<singular-type>/<kebab-case-slug>`。pathは`entities/<plural-type>/<slug>.md`。
- 日付はISO 8601。精度が不明なら推測で日を補わず`precision`を併記する。
- `null`は不明、`[]`は確認したが該当なし。両者を混同しない。
- 参照はentity IDで保持する。
- 本文は説明用。機械処理する事実はfrontmatterへ置く。

## Subject

```yaml
id: subject/example
type: subject
pseudonym: S-001
direct_identifiers_stored: false
consent_refs: [source/consent-example]
allowed_purposes: [self-reflection, research, artistic-research]
prohibited_purposes: [clinical-diagnosis, employment-decision]
status: active
created: 2026-08-11
updated: 2026-08-11
```

直接識別情報は原則保存しない。pseudonymと分析内容を再識別情報へ不要に結合しない。

## Source

```yaml
id: source/interview-001
type: source
subject: subject/example
source_kind: interview
captured_at: 2026-08-11T10:00:00+09:00
locator: "gdrive://opaque-locator"
raw_content_stored: false
consent:
  obtained: true
  obtained_at: 2026-08-11
  purposes: [self-reflection, artistic-research]
  allowed_operations: [analyze, derive, bundle, export-signals]
  expires_at: null
  revoked_at: null
  notes: null
reliability_notes: null
created: 2026-08-11
updated: 2026-08-11
```

`source_kind`は`config/vocabularies.yaml`の閉じた語彙。原文全文のGit複製は必須にしない。

## Event

```yaml
id: event/example-001
type: event
subject: subject/example
time:
  observed_at: 2026-08-11T10:05:00+09:00
  precision: minute
context:
  domains: [work]
  social: [alone]
state:
  fatigue: unknown
  stress: unknown
trigger: "期限が外部から変更された"
observed_facts:
  - "本人が作業順序を組み替えた"
raw_voice:
  - text: "自分で決め直したい"
    source_ref: source/interview-001
appraisal: []
emotion: []
body: []
cognition: []
action:
  - "作業計画を再設計した"
immediate_outcome:
  - "作業を再開した"
delayed_outcome: []
source_refs: [source/interview-001]
created: 2026-08-11
updated: 2026-08-11
```

`observed_facts`と`raw_voice`は観測。appraisal以降に推論を混ぜる場合はClaimを作る。actionとoutcomeは分離する。

## Claim

```yaml
id: claim/example-control
type: claim
subject: subject/example
layer: motivation
scope: state                 # state | context-bound | trait-candidate | trait
statement: "外部制約時に制御感の回復を求める可能性がある"
conditions: ["自律を制限されたと知覚したとき"]
supporting_evidence: [event/example-001]
counterevidence: []
alternative_explanations:
  - "単に締切順へ最適化した"
  - "第三者への説明責任を優先した"
confidence: low
status: hypothesis
supersedes: null
superseded_by: null
created: 2026-08-11
updated: 2026-08-11
```

- layer: `emotion | motivation | behavioral-principle | tension | other`
- status: `hypothesis | supported | revised | rejected`
- 代替説明は最低2件を保持する。
- Claimを上書きして履歴を消さず、改訂時は`supersedes`/`superseded_by`で接続する。

## Pattern

```yaml
id: pattern/example-control-recovery
type: pattern
subject: subject/example
condition: "外部から意思決定を制限されたと知覚する"
recurring_appraisal: ["選択可能性が失われた"]
recurring_drive: [D5]
recurring_action: ["決定可能な構造へ再設計する"]
reinforcement: ["制御感の回復"]
contexts_seen: [work, project]
evidence: [event/example-001, event/example-002]
claim_refs: [claim/example-control]
counterevidence: []
confidence: low
status: hypothesis
created: 2026-08-11
updated: 2026-08-11
```

Patternは複数Eventを必要とする。ただし件数だけで`supported`へ昇格しない。文脈多様性、時点、反証、代替説明を監査対象とする。

## Measurement

```yaml
id: measurement/example-001
type: measurement
subject: subject/example
instrument:
  name: null
  version: null
  official: true
  scoring_reference: null
administered_at: null
source_ref: source/formal-measurement-001
scores: {}
interpretation_claim_refs: []
created: 2026-08-11
updated: 2026-08-11
```

`instrument.official: true`、正式尺度Source、scoring referenceが揃わないMeasurementに数値scoreを許可しない。会話から正式尺度得点を推定しない。

## Tension

TensionはClaimの`layer: tension`として保持する。

```yaml
statement: "深い関係を求める一方、拒絶に晒されることを避ける"
between:
  - "深い関係を求める"
  - "評価・拒絶への曝露を避ける"
```

両立しないように見える情報を一方へ丸めない。

## Derived Self Model

Derivedは手書きentityにしない。Subject、Claim、Patternから決定論的に生成する。

```yaml
self_model:
  subject: subject/example
  as_of: 2026-08-11
  source_commit: <sha>
  emotions: []
  motivations: []
  behavioral_principles: []
  tensions: []
  dominant_triggers: []
  dominant_rewards: []
  avoidance_targets: []
  protective_factors: []
  context_dependencies: []
  unknowns: []
  evidence_coverage: {}
```

`as_of`単位でスナップショットを比較できること。モデルの変化と観測増加による理解の変化を区別するため、根拠entity IDを各項目に残す。

## 参照制約

| From | Allowed target |
|---|---|
| Subject | Source(consent) |
| Source | Subject |
| Event | Subject, Source |
| Claim | Subject, Event, Claim |
| Pattern | Subject, Event, Claim |
| Measurement | Subject, Source, Claim |

循環参照、存在しないID、subject不一致はhard error。

## Trait / State / Context

- `state`: 現在の疲労、ストレス、気分等。単一Eventでも記録可。
- `context-bound`: 特定状況で再現する傾向。
- `trait-candidate`: 複数時点・複数Contextの候補。断定不可。
- `trait`: 事前に合意した基準と十分な反証検討を通過した場合のみ。初期実装は自動昇格させない。

## Validation Levels

- hard error: 構造破損、参照不整合、根拠なしClaim、尺度捏造、同意欠落、生成物不整合。
- soft audit: 根拠偏り、反証不足、単一Context、古いClaim、Unknown過多、Drive観測偏り。

