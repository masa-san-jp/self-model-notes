# Interoperability and Research Signals v1

Self Modelのcore schemaを利用先に合わせない。境界で次のsignalへ1回だけ翻訳する。

```yaml
contract_version: research-signal-export/v1
source_repository: self-model
source_commit: <sha>
purpose: artistic-research
generated_at: 2026-08-14T00:00:00Z
signal_count: 0
signals: []
```

## Mapping

| Signal | Self Model source | Agentic Art use | 自動断定しないこと |
|---|---|---|---|
| seeks | motivation Claims | 探索テーマ候補 | 本人の最終目的 |
| protects | motivation / behavioral Claims | 制約・守る条件 | 倫理的正当性 |
| avoids | motivation Claims | 避ける表現・状況候補 | 恐怖症・診断 |
| reacts_against | Pattern condition/action | 対抗する潮流候補 | 美術史上の影響関係 |
| drawn_toward | repeated approach behavior | 媒体・主題候補 | 恒常的嗜好 |
| influenced_by | explicit evidence only | 参照候補 | 暗黙の影響 |
| tensions | tension Claims | 創作上の摩擦候補 | 解消すべき問題 |
| recurring_patterns | Patterns | 制作プロセス候補 | 将来行動の保証 |
| emotional_material | Emotion Claims/raw refs | 素材候補 | 公開許可 |

## Contract

- 全signal itemに`evidence_refs`とitem-level certaintyを持たせられること。
- raw voice本文は既定でexportせず、参照だけを出す。
- `purpose: artistic-research`の同意がない根拠は除外ではなくexport全体をfailさせる。
- JSON Schema `tests/contracts/research-signals-v1.schema.json`で出力を検証する。
- consumerは`contract_version`が未知ならfail closedする。
- `--subject`は任意で、省略時は全Subjectを同意チェックしたうえで出力する。
- `--output`はファイル出力、`--limit 0`は全signal出力とする。
- source repositoryとcommit SHAを固定し、再現可能にする。

## Consumer handoff (SM-011 / K1 export envelope)

- Consumer: [agentic-art-research](https://github.com/masa-san-jp/agentic-art-research)
- Export envelope implementation: Issue [#28](https://github.com/masa-san-jp/self-model-notes/issues/28)
- Cross-repository fixture: [`tests/contracts/agentic-art-research-consumer-v1.fixture.json`](../tests/contracts/agentic-art-research-consumer-v1.fixture.json)
- Result: the local fixture contains only the `research-signal-export/v1` envelope and no core entities. The previous `urn:self-model-notes:research-signals:v1` consumer fixture is historical and is not the K1 envelope. Consumer adoption remains review-gated until the orchestration adapter is aligned with the envelope decision.
