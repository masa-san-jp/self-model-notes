# self-model-notes

人を固定的な性格タイプとして分類せず、観測された出来事から、感情・内的動機・条件付き行動原理・葛藤を根拠付きで更新する Self Model Knowledge Base。

要件の正本は [Issue #1](https://github.com/masa-san-jp/self-model-notes/issues/1) です。READMEや設計文書と矛盾する場合はIssue #1を正とします。

## 体系

```text
Source / Raw Evidence
        ↓
Observation / Event
        ↓
Claim / Hypothesis
        ↓
Repeated Pattern
        ↓
Derived Self Model
        ↓
Research Signals
```

正本は `entities/` のMarkdown frontmatterです。`data/` と `overviews/coverage.md` は生成物であり、手で編集しません。

## 入口

- 実装者: [AGENTS.md](AGENTS.md) → [docs/schema.md](docs/schema.md) → [docs/execution-plan.md](docs/execution-plan.md)
- 調査・記録担当: [docs/investigation-task.md](docs/investigation-task.md)
- 利用者: [docs/for-other-personas.md](docs/for-other-personas.md)
- エコシステム: [docs/ecosystem-architecture.md](docs/ecosystem-architecture.md)
- 機械可読タスク: [execution/tasks.yaml](execution/tasks.yaml)

## ディレクトリ

```text
entities/          Subject / Source / Event / Claim / Pattern / Measurement の正本
config/            閉じた語彙・Drive Systems・Context・Confidence
docs/              スキーマ、分析、取得、倫理、相互運用、実行計画
execution/         エージェントが順に消化する機械可読タスク
tools/             作成、検証、監査、派生モデル、bundle、export
tests/             単体・統合・禁止事項テストと匿名fixture
data/              決定論的な生成物
overviews/         coverage等の人間向け生成物
```

`data/self-models/subject/<slug>.json` と `.md` はGit管理するcurrent snapshotです。entityをcommitした後に `python3 tools/bundle.py --all` で再生成し、`python3 tools/bundle.py --all --check` でJSON/Markdownのstalenessを確認します。過去snapshotは別名fileではなくGit historyで比較します。

## 最小コマンド

```bash
python3 -m pip install -e .
python3 tools/new_entity.py subject sample-subject
python3 tools/build_graph.py --check
python3 -m unittest discover -s tests -p "test_*.py"
python3 tools/build_graph.py
python3 tools/audit.py
```

## 原則

- 原文・観測事実・解釈を混ぜない。
- Claimは根拠、反証、代替説明、確信度を持つ。
- Unknownを推測で埋めない。
- Trait / State / Contextを分離する。
- 正式尺度がなければ尺度得点を作らない。
- 同意範囲外のデータをexportしない。
- 心理診断、精神疾患推定、重大な人事判断には使わない。
