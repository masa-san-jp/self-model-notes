# Milestone 1 Acceptance Matrix

| Issue #1 requirement | Artifact | Verification | Status |
|---|---|---|---|
| READMEが目的・利用方法を説明 | `README.md` | doc review | review-gated |
| 6 entity型を定義 | `docs/schema.md` | `test_schema.py` | verified |
| 3層とTensionを分離 | Claim.layer | validator negative test | verified |
| Event→Claim→Pattern→Derived追跡 | graph + derived model | E2E test | verified |
| Claimの根拠・反証・代替・confidence | Claim validator | negative tests | verified |
| 正式尺度なしの数値得点禁止 | Measurement validator | negative test | verified |
| Trait/State/Context混同検出 | audit | audit test | verified |
| raw voiceと分析分離 | Event/Claim schema | negative test | verified |
| Unknown保持 | vocabulary + serializer | round-trip test | verified |
| consent/利用範囲保持 | Source schema | export denial test | verified |
| build_graph --check | `tools/build_graph.py` | CLI test | verified |
| auditが調査課題を出す | `tools/audit.py` | fixture test | verified |
| Subject bundle | `tools/bundle.py` | snapshot test | verified |
| research_signals export | `tools/export_signals.py` | contract test | verified |
| sibling対応定義 | `docs/interop-mapping.md` | doc review | review-gated |
| 匿名fixture E2E | `tests/fixtures/e2e/` | CI | verified |
| Masa固有項目なし | fixture/schema | forbidden-key test | verified |

`Status`は実装PRで`planned → implemented → verified`の順に更新する。verifiedには検証コマンドまたはレビュー証拠が必要。
`review-gated`は自動検証の対象外で、人間による文書・consumerレビューを完了条件とする。
