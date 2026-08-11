# Evidence and Inference Policy

## 区分

| 区分 | 保存先 | 例 |
|---|---|---|
| Raw evidence | Source locator / Event.raw_voice | 本人の原文 |
| Observation | Event.observed_facts/action/outcome | 記録上確認できる事実 |
| Inference | Claim | 動機、意味づけ、感情の解釈 |
| Repeated rule | Pattern | 複数Eventを横断した条件付き規則 |
| Derived view | data/self-models | 現時点の集約 |

## Confidence

confidenceは真実度の数値ではなく、現時点の根拠状態を示す閉じた語彙とする。初期値は`unknown | low | medium | high`。自動昇格規則は置かない。

## 反証可能性

Claimは、何が観測されれば弱まるかを本文またはcounterevidenceで示す。高confidenceでも反証証拠を削除しない。

## 代替説明

最低2件。単なる言い換えではなく、同じEventを説明し得る異なる因果・目的を置く。

## 改訂

過去Claimは削除・上書きしない。新Claimを作り、`supersedes`で接続する。Derived viewは`as_of`ごとに再生成する。

