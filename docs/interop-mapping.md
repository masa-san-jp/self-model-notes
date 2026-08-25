# Interoperability and Research Signals v1

Self Modelのcore schemaを利用先に合わせない。境界で次のsignalへ1回だけ翻訳する。

```yaml
schema: urn:self-model-notes:research-signals:v1
subject: subject/example
as_of: 2026-08-11
purpose: artistic-research
source_repository: masa-san-jp/self-model-notes
source_commit: <sha>
research_signals:
  seeks: []
  protects: []
  avoids: []
  reacts_against: []
  drawn_toward: []
  influenced_by: []
  tensions: []
  recurring_patterns: []
  emotional_material: []
  raw_voice_refs: []
  certainty: unknown
  evidence_refs: []
```

## Mapping

| Signal | Self Model source | Agentic Art use | 自動断定しないこと |
|---|---|---|---|
| seeks | motivation Claim + `motivation_direction: seek` | 探索テーマ候補 | 本人の最終目的 |
| protects | motivation Claim + `motivation_direction: protect` | 制約・守る条件 | 倫理的正当性 |
| avoids | motivation Claim + `motivation_direction: avoid` | 避ける表現・状況候補 | 恐怖症・診断 |
| reacts_against | Pattern condition/action | 対抗する潮流候補 | 美術史上の影響関係 |
| drawn_toward | repeated approach behavior | 媒体・主題候補 | 恒常的嗜好 |
| influenced_by | explicit evidence only | 参照候補 | 暗黙の影響 |
| tensions | tension Claims | 創作上の摩擦候補 | 解消すべき問題 |
| recurring_patterns | Patterns | 制作プロセス候補 | 将来行動の保証 |
| emotional_material | Emotion Claims/raw refs | 素材候補 | 公開許可 |

## Orchestration boundary DTO

`tools/export_signals.py` の `signals` は、current Claim / Patternを1件ずつ次の26 fieldへ翻訳する。record間の集約、本文キーワード、Drive ID、LLMによる分類は行わない。

```text
signal_id, repository, commit, entity_id, source_locator, evidence_locator,
evidence_kind, statement, certainty, unknowns, constraints, validity, freshness,
generated_at, adapter_version, consent_scope, export_permitted, seeks, protects,
avoids, tensions, recurring_patterns, raw_voice_locator, traits, states, contexts
```

`signal_id`は`self:<entity-type>:<slug>`、`repository`は`self-model`、`source_locator`と`evidence_locator`は`self-model://`のopaque locatorとする。`commit`は40桁のexport時HEAD、`certainty`は`high/medium→inferred`、`low→uncertain`、`unknown→unknown`、`freshness.status`は根拠がないため`unknown`とする。`motivation_direction`は`seek/protect/avoid`だけを対応listへ入れ、`mixed/unknown`は3listを空にしてUnknownsへ残す。`raw_voice_locator`は`#raw-voice-not-exported`を付け、本文を含めない。

## Contract

- 全signal itemに`evidence_refs`とitem-level certaintyを持たせられること。
- raw voice本文は既定でexportせず、`self-model://<entity-id>#raw-voice-not-exported`だけを出す。
- `purpose: artistic-research`の同意がない根拠は除外ではなくexport全体をfailさせる。
- JSON Schema `tests/contracts/research-signals-v1.schema.json`で出力を検証する。
- consumerは`schema` major versionが未知ならfail closedする。
- source repositoryとcommit SHAを固定し、再現可能にする。

## Consumer handoff (SM-011)

- Consumer: [agentic-art-research](https://github.com/masa-san-jp/agentic-art-research)
- Upstream pin for this fixture: `self-model-notes@7f1f371486fe983f0bcfefbbf92a5df1326dac7b`
- Cross-repository fixture: [`tests/contracts/agentic-art-research-consumer-v1.fixture.json`](../tests/contracts/agentic-art-research-consumer-v1.fixture.json)
- The fixture is normalized-research-signal/v1-shaped, contains no core entities, and represents missing upstream inputs as `certainty: unknown`. The pinned consumer adapter must accept it without schema or semantic inference.
