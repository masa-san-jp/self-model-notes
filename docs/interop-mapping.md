# Interoperability and Research Signals v1

Self Modelのcore schemaを利用先に合わせない。境界で次のsignalへ1回だけ翻訳する。

```yaml
schema: urn:self-model-notes:research-signals:v1
subject: subject/example
as_of: 2026-08-11
purpose: artistic-research
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
- consumerは`schema` major versionが未知ならfail closedする。
- source repositoryとcommit SHAを固定し、再現可能にする。

