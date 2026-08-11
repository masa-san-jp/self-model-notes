# 利用者・他エージェント向け入口

## 読む

```bash
cat overviews/coverage.md
python3 tools/bundle.py --subject subject/<id>
python3 tools/export_signals.py --subject subject/<id> --purpose artistic-research --operation export-signals
```

exportはSourceごとの同意を再検証し、1件でも不足があれば全体をdenyする。raw voice本文は既定で含まれない。運用上の復旧や衝突回避は[`docs/operations.md`](operations.md)を参照する。

## 解釈規則

| 表示 | 意味 | 利用時の扱い |
|---|---|---|
| observed fact | Sourceで確認できる観測 | locatorを確認して引用 |
| hypothesis / low | 初期推論 | 断定しない |
| supported | 複数根拠を持つClaim | counterevidenceも併記 |
| Pattern | 条件付き反復 | 条件を落として人格ラベルにしない |
| Unknown | 未観測・拒否・判定不能 | 推測で補わない |
| tension | 両立する葛藤 | 一方へ丸めない |

## 禁止

- Self Modelから診断名を作る。
- 将来行動を確定予測する。
- confidenceを人間の価値スコアとして比較する。
- raw voice refsから許可なく原文を公開する。
- 同意確認を迂回して上流entityを直接読む。

追加・訂正はIssueに「対象Subject/Event」「根拠Source」「目的」「必要期限」を書く。entitiesや生成物を直接変更しない。
