# Agentic Art Research Knowledge System

GitHub Project: [Agentic Art Research](https://github.com/users/masa-san-jp/projects/4)

## リポジトリ責務

| Repository | 正本とする知識 | 境界から出すもの | 持たないもの |
|---|---|---|---|
| `self-model-notes` | 人のEvent・Claim・Pattern・動的Self Model | `research-signal-export/v1` | 芸術史、トレンド、作品生成 |
| `art-history-notes` | 芸術史の時間・空間・関係KB | 作品・運動・技法・文脈signal | 個人の内面推定 |
| `marketing-trends-notes` | 市場変化、実践、鮮度付き根拠 | trend / practice signal | 個人の内面推定 |
| `agentic-art-research` | 3系統のsignalを束ねる調査オーケストレーション | research brief / trace | 上流KBの正本データ |

```text
self-model-notes ─────────────┐
art-history-notes ────────────┼─ normalized signals → agentic-art-research
marketing-trends-notes ───────┘
```

## 統合原則

1. 各KBのcore schemaを統合先へ合わせない。
2. 翻訳は各KBのexport境界で1回だけ行う。
3. `agentic-art-research`は上流のentityを複製せず、repository、commit、entity IDで参照する。
4. signalは断定ではなく、certaintyとevidence refsを保持する。
5. 3入力が揃わない場合も、欠損をUnknownとして処理し、捏造しない。
6. Project #4は横断の進捗ビュー、各Repository Issueは実装要件と受入条件の正本とする。

## 依存順序

1. 各上流KBが独立して検証・bundle・exportできる。
2. `self-model-notes`の`research-signal-export/v1` envelopeをfixtureで固定する。
3. `agentic-art-research`側が3exportを受け取るcontract testを作る。
4. 実データではなく匿名fixtureでE2Eを通す。
5. 同意範囲を確認したデータだけを実運用へ接続する。

## SM-011 cross-repository evidence

- Consumer repository: [masa-san-jp/agentic-art-research](https://github.com/masa-san-jp/agentic-art-research)
- Upstream pin: `self-model-notes@7f1f371486fe983f0bcfefbbf92a5df1326dac7b`
- Local consumer fixture: [`tests/contracts/agentic-art-research-consumer-v1.fixture.json`](../tests/contracts/agentic-art-research-consumer-v1.fixture.json)
- The fixture contains only the `research-signal-export/v1` envelope; it does not copy core entities. Empty upstream inputs remain an empty `signals` list.
- The older `research_signals` fixture and consumer contract are superseded by Issue #28 and the parent orchestration decision. Consumer-side adoption remains review-gated until the orchestration adapter accepts this export envelope.

## 完了の定義

4リポジトリが同じschemaを持つことではない。各リポジトリが独立した正本を保ちながら、commit固定された機械可読signalを相互に検証できることを完了とする。
