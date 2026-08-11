# Milestone 1 Acceptance Matrix

| Issue #1 requirement | Artifact | Verification | Status |
|---|---|---|---|
| READMEが目的・利用方法を説明 | `README.md` | doc review | scaffolded |
| 6 entity型を定義 | `docs/schema.md` | `test_schema.py` | planned |
| 3層とTensionを分離 | Claim.layer | validator negative test | planned |
| Event→Claim→Pattern→Derived追跡 | graph + derived model | E2E test | planned |
| Claimの根拠・反証・代替・confidence | Claim validator | negative tests | planned |
| 正式尺度なしの数値得点禁止 | Measurement validator | negative test | planned |
| Trait/State/Context混同検出 | audit | audit test | planned |
| raw voiceと分析分離 | Event/Claim schema | negative test | planned |
| Unknown保持 | vocabulary + serializer | round-trip test | planned |
| consent/利用範囲保持 | Source schema | export denial test | planned |
| build_graph --check | `tools/build_graph.py` | CLI test | planned |
| auditが調査課題を出す | `tools/audit.py` | fixture test | planned |
| Subject bundle | `tools/bundle.py` | snapshot test | planned |
| research_signals export | `tools/export_signals.py` | contract test | planned |
| sibling対応定義 | `docs/interop-mapping.md` | doc review | scaffolded |
| 匿名fixture E2E | `tests/fixtures/e2e/` | CI | planned |
| Masa固有項目なし | fixture/schema | forbidden-key test | planned |

`Status`は実装PRで`planned → implemented → verified`の順に更新する。verifiedには検証コマンドまたはレビュー証拠が必要。

