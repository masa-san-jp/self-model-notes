# Autonomous Agent Contract

このファイルは、GPT-5.6 Luna / Claude Sonnet相当の実行系エージェントが1タスクを自律完了するための作業契約です。

## 1. 正本の優先順位

1. [Issue #1](https://github.com/masa-san-jp/self-model-notes/issues/1) — 要件SSOT
2. `docs/schema.md` — データ設計SSOT
3. `execution/tasks.yaml` — 実行順序・依存関係SSOT
4. 各docs — 手順・説明
5. README — 現状案内

矛盾を見つけたら上位を正とし、勝手に要件を変更しない。上位文書でも解消できない場合はIssueに選択肢、影響、推奨案を書いて停止する。

## 2. 1回の実行単位

1. `execution/tasks.yaml` から `status: ready` かつ全依存が `done` の最小IDを1件選ぶ。
2. `allowed_paths` だけを変更する。共有ファイルが必要ならタスクに追記せずIssueで確認する。
3. `acceptance` をすべて満たす。
4. `checks` をすべて実行する。
5. 生成物を再生成し、差分を確認する。
6. タスクの `status` を `done` にし、`evidence` にcommit/PR/テスト結果を記録する。
7. 1タスク1PRを原則とする。

複数タスクを一度に実装しない。関連する改善を見つけても、現在タスクの完了に不要なら新Issue候補として報告する。

## 3. 必須の作業順序

```text
inspect → decide → edit → test → regenerate → diff → report
```

- inspect: Issue #1、対象タスク、対象ファイル、関連テストを読む。
- decide: 変更する型・不変条件・失敗条件を先に列挙する。
- edit: 最小差分で実装する。
- test: 正常系だけでなく禁止事項を落とすテストを書く。
- regenerate: `data/` と `overviews/` をツールで更新する。
- diff: 意図しない生成物・機微情報・識別情報がないか確認する。
- report: 変更、検証、未解決、次にreadyになるタスクを記載する。

## 4. 絶対に守る不変条件

- `entities/` のfrontmatterが正本。`data/`を直接編集しない。
- Source → Event → Claim → Pattern → Derivedの参照を逆引きできる。
- `raw_voice`と分析文を同じフィールドに置かない。
- Claimに `supporting_evidence`、`counterevidence`、2件以上の`alternative_explanations`、`confidence`を持たせる。
- 単発EventからPatternを確定しない。
- Formal measurementは正式なSourceとinstrument metadataがなければ作れない。
- Traitは複数Context・複数時点の根拠なしに付与しない。
- Unknown、拒否、未観測を0やfalseへ変換しない。
- 同意目的を超えたexportを拒否する。
- 直接識別情報、秘密、認証情報、原文全文をfixtureへ入れない。
- 診断名・疾患推定・雇用等の重大判断用スコアを実装しない。

## 5. 設計変更が必要な場合

次の場合は実装せず停止する。

- エンティティ型の追加・削除
- 既存ID規則の変更
- closed vocabularyへの値追加
- confidenceやpattern昇格の閾値固定
- 同意・privacyの弱化
- Issue #1のスコープ外機能
- sibling repositoryのcore schema変更

報告は「観測事実」「問題」「選択肢A/B」「推奨」「未決定時の影響」の順にする。

## 6. 検証

```bash
python3 tools/build_graph.py --check
python3 -m unittest discover -s tests -p "test_*.py"
python3 tools/build_graph.py
python3 tools/audit.py --dry-run
git diff --exit-code -- data/ overviews/coverage.md
```

タスク固有の`checks`があれば追加で実行する。失敗した検証を削除・skipして通したことにしない。

## 7. 同時実行

- 同じ`allowed_paths`を持つタスクは並列実行しない。
- `docs/schema.md`、`tools/kb.py`、`execution/tasks.yaml`は共有ロック対象。
- 他エージェントの未完成差分を修正・restore・stashしない。
- 自分の変更外で検証が失敗した場合、原因と該当ファイルを報告して停止する。

## 8. 完了報告テンプレート

```markdown
## 完了
- Task: SM-XXX
- 変更: ...
- 受入条件: x/x
- 検証: command → result
- 生成物差分: ...
- 機微情報確認: none / details
- 未解決: none / ...
- 次のready task: SM-XXX
```

