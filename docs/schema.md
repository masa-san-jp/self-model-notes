# Self Model Schema v0.1

要件SSOTはIssue #1。本書は実装上のデータ設計SSOTです。1 entity = 1 Markdown、frontmatterを正本とします。

## 共通規則

```yaml
---
id: event/example
type: event
subject: subject/example
created: 2026-08-11
updated: 2026-08-11
---
```

- IDは`<singular-type>/<kebab-case-slug>`。pathは`entities/<plural-type>/<slug>.md`。
- slugはASCII小文字・数字・単一のハイフンだけで構成し、`[a-z0-9]+(?:-[a-z0-9]+)*`に一致させる。IDの型名は単数形、ディレクトリ名は`config/vocabularies.yaml`の`plural_paths`にある複数形を使う。
- たとえば`subject/example`の正しいpathは`entities/subjects/example.md`である。IDのslug、ファイル名、frontmatterの`type`は一致しなければならない。frontmatterにpathを重複保存しない。
- 日付はISO 8601。精度が不明なら推測で日を補わず`precision`を併記する。
- 全型共通の必須fieldは`id`、`type`、`created`、`updated`。`status`はSubject、Claim、Patternで使い、Source、Event、Measurementでは使わない。
- `null`は不明、`[]`は確認したが該当なし。両者を混同しない。
- `unknown`は汎用の欠損値ではない。`config/contexts.yaml`の`uncertainty`/`control`、`config/confidence.yaml`のconfidence、Eventの定義済み状態slot（`fatigue`、`stress`）のように、スキーマで明示された場所だけで使う。それ以外の未観測の値は`null`で保持する。
- 参照はentity IDで保持する。
- 本文は説明用。機械処理する事実はfrontmatterへ置く。

### 参照とpathの判定表

| type | ID prefix | 正本path | 参照できるentity |
|---|---|---|---|
| subject | `subject/` | `entities/subjects/<slug>.md` | `source`（consent） |
| source | `source/` | `entities/sources/<slug>.md` | `subject` |
| event | `event/` | `entities/events/<slug>.md` | `subject`, `source` |
| claim | `claim/` | `entities/claims/<slug>.md` | `subject`, `event`, `claim` |
| pattern | `pattern/` | `entities/patterns/<slug>.md` | `subject`, `event`, `claim` |
| measurement | `measurement/` | `entities/measurements/<slug>.md` | `subject`, `source`, `claim` |

参照先は存在するentity IDでなければならず、参照元と参照先の`subject`は一致させる。例外はSubjectが同意Sourceを参照する場合だけで、同意Source自身は同じSubjectを参照する。許可されていない型への参照、存在しないID、subject不一致、循環するsupersessionはhard errorとする。

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

### Invalid example（必ずreject）

`id`のslugに大文字が含まれ、正しいpath規則にも一致しない。

```yaml
id: subject/Example
type: subject
pseudonym: S-001
direct_identifiers_stored: false
consent_refs: [source/consent-example]
allowed_purposes: [self-reflection]
prohibited_purposes: [clinical-diagnosis]
status: active
created: 2026-08-11
updated: 2026-08-11
```

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

### Invalid example（必ずreject）

`subject`がSourceから参照できない`event` IDになっている。

```yaml
id: source/interview-001
type: source
subject: event/example-001
source_kind: interview
captured_at: 2026-08-11T10:00:00+09:00
locator: "gdrive://opaque-locator"
raw_content_stored: false
consent:
  obtained: true
  obtained_at: 2026-08-11
  purposes: [self-reflection]
  allowed_operations: [analyze]
  expires_at: null
  revoked_at: null
  notes: null
reliability_notes: null
created: 2026-08-11
updated: 2026-08-11
```

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
  uncertainty: unknown
  control: unknown
state:
  fatigue: null
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

### Invalid example（必ずreject）

`state.fatigue`に未定義の文字列を入れている。未観測なら`null`を使う。

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
  fatigue: maybe
  stress: null
trigger: "期限が外部から変更された"
observed_facts: ["本人が作業順序を組み替えた"]
raw_voice: []
appraisal: []
emotion: []
body: []
cognition: []
action: ["作業計画を再設計した"]
immediate_outcome: ["作業を再開した"]
delayed_outcome: []
source_refs: [source/interview-001]
created: 2026-08-11
updated: 2026-08-11
```

## Claim

```yaml
id: claim/example-control
type: claim
subject: subject/example
layer: motivation
motivation_direction: seek
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
- `motivation_direction`: `seek | protect | avoid | mixed | unknown` for `layer: motivation`; `null` for every other layer.
- status: `hypothesis | supported | revised | rejected`
- 代替説明は最低2件を保持する。
- Claimを上書きして履歴を消さず、改訂時は`supersedes`/`superseded_by`で接続する。

`seek`は得る・近づく・増やす方向、`protect`は関係・状態・自己像・安全を守る方向、`avoid`は苦痛・脅威・損失・状況から離れる方向を表す。`mixed`は複数方向が不可分な場合だけ、`unknown`は根拠から方向を判定できない場合だけ使う。Claim本文やDrive IDのキーワードから自動分類しない。

### Invalid example（必ずreject）

根拠が空で、代替説明も1件しかないためClaimとして保存できない。

```yaml
id: claim/example-control
type: claim
subject: subject/example
layer: motivation
motivation_direction: null
scope: state
statement: "外部制約時に制御感の回復を求める可能性がある"
conditions: ["自律を制限されたと知覚したとき"]
supporting_evidence: []
counterevidence: []
alternative_explanations: ["単に締切順へ最適化した"]
confidence: unknown
status: hypothesis
supersedes: null
superseded_by: null
created: 2026-08-11
updated: 2026-08-11
```

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

### Invalid example（必ずreject）

`evidence`が単一Eventだけで、Patternの最低条件を満たさない。必要な件数や昇格閾値を推測で固定してはならないが、単一Eventは明確に不十分である。

```yaml
id: pattern/example-control-recovery
type: pattern
subject: subject/example
condition: "外部から意思決定を制限されたと知覚する"
recurring_appraisal: ["選択可能性が失われた"]
recurring_drive: [D5]
recurring_action: ["決定可能な構造へ再設計する"]
reinforcement: ["制御感の回復"]
contexts_seen: [work]
evidence: [event/example-001]
claim_refs: [claim/example-control]
counterevidence: []
confidence: low
status: hypothesis
created: 2026-08-11
updated: 2026-08-11
```

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

### Invalid example（必ずreject）

非公式InstrumentとインタビューSourceから数値scoreを作っている。

```yaml
id: measurement/example-001
type: measurement
subject: subject/example
instrument:
  name: conversational-estimate
  version: "1"
  official: false
  scoring_reference: null
administered_at: 2026-08-11T10:00:00+09:00
source_ref: source/interview-001
scores:
  anxiety: 0.8
interpretation_claim_refs: []
created: 2026-08-11
updated: 2026-08-11
```

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
  schema_version: 2
  subject: subject/example
  as_of: 2026-08-11
  source_commit: <sha>
  derived_from: []
  observations: []
  measurements: []
  emotions: []
  motivations: []
  behavioral_principles: []
  tensions: []
  patterns: []
  dominant_triggers: []
  dominant_rewards: []
  avoidance_targets: []
  protective_factors: []
  context_dependencies: []
  claim_history: []
  unknowns: []
  evidence_coverage: {}
```

`as_of`単位でスナップショットを比較できること。モデルの変化と観測増加による理解の変化を区別するため、根拠entity IDを各項目に残す。

Derived Self Model v2のcurrent sectionには`rejected`または`superseded_by`が設定されたClaimを入れず、履歴は`claim_history`へ残す。`dominant_triggers`はcurrent Patternの`condition`、`dominant_rewards`は`reinforcement`からだけ生成する。`avoidance_targets`と`protective_factors`は、それぞれ`motivation_direction: avoid`と`protect`のClaimだけを対応付け、`seek`・`mixed`・`unknown`は推測で分類しない。Context依存はcontext-bound Claimの`conditions`またはPatternの`contexts_seen`から生成し、recordに元fieldを残す。

未定義の追加sectionは空配列とし、`unknowns`に`field-unobserved`を記録する。判定不能な方向は`motivation-direction-unresolved`、confidence不明は`confidence-unknown`としてClaim/Patternの根拠とともに保持する。Observationは`trigger`、`observed_facts`、`appraisal`、`emotion`、`body`、`cognition`、`actions`、`immediate_outcomes`、`delayed_outcomes`、`context`、`state`、`source_refs`、`raw_voice_refs`を保持するが、raw voice本文は含めない。

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

判定を再現可能にするため、次のように扱う。

| 概念 | 保存場所 | 最小条件 | 禁止事項 |
|---|---|---|---|
| State | Eventの`state` | そのEvent時点の観測。未収集は`null`、評価を試みて判定不能なら定義済みslotの`unknown` | StateをTraitへ自動昇格しない |
| Context | Eventの`context`、Claimの`scope: context-bound` | 場面・関係・不確実性・制御等の条件を明示 | Contextを省略して一般化しない |
| Trait candidate | Claimの`scope: trait-candidate` | 複数時点・複数Contextの根拠があり、なお候補として表現 | `trait`と断定しない |
| Trait | Claimの`scope: trait` | 事前合意した基準、十分な反証検討、人手レビュー | 初期実装で自動付与しない |

`scope: state`は「現在の状態に依存するClaim」、`scope: context-bound`は「特定Contextでのみ検討するClaim」であり、どちらもTraitを意味しない。`scope: trait-candidate`と`scope: trait`の差は根拠の広がりだけでなく、明示的なレビュー状態にもある。

### Unknownの判定例

```yaml
state:
  fatigue: null                 # 未観測。推測で埋めない
  stress: unknown               # 評価を試みたが判定不能
context:
  uncertainty: unknown          # 語彙で定義された「評価不能」
  control: externally-directed
claim:
  scope: context-bound           # Traitではない
  confidence: unknown            # 根拠状態を評価できない
```

`null`、`[]`、`unknown`は同じ意味へ正規化しない。`null`は未知・未観測、`[]`は確認済みで該当なし、`unknown`は該当語彙が定義する評価不能を表す。
生成されるcoverageでもこの3状態を分離して集計し、未定義キーは`unobserved`として別に扱う。

## Validation Levels

- hard error: 構造破損、参照不整合、根拠なしClaim、尺度捏造、同意欠落、生成物不整合。
- soft audit: 根拠偏り、反証不足、単一Context、古いClaim、Unknown過多、Drive観測偏り。
