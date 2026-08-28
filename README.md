# self-model-notes

人を固定的な性格タイプとして分類せず、観測された出来事から、感情・内的動機・条件付き行動原理・葛藤を根拠付きで更新する Self Model Knowledge Base。

要件の正本は [Issue #1](https://github.com/masa-san-jp/self-model-notes/issues/1) です。READMEや設計文書と矛盾する場合はIssue #1を正とします。

## 利用者向けの入口

このリポジトリは、対象者を固定的な性格タイプへ分類する診断ツールではありません。観測事実、根拠、解釈、同意範囲を分けて保持し、明示された範囲だけを調査用の信号へ書き出すナレッジベースです。

| したいこと | 入口 |
| --- | --- |
| 現在の自己モデルを読む | [`entities/`](entities/)、[`overviews/`](overviews/)、生成済みの `data/` |
| 1件の観測や主張を追加する | [`docs/investigation-task.md`](docs/investigation-task.md)、[`docs/schema.md`](docs/schema.md) |
| 他のリポジトリへ渡せる信号を確認する | `normalized-research-signal/v1` と [`docs/ecosystem-architecture.md`](docs/ecosystem-architecture.md) |
| エージェントとして作業する | [`AGENTS.md`](AGENTS.md)、[`execution/tasks.yaml`](execution/tasks.yaml) |

`PRIVATE_RAW`、`RESTRICTED`、認証情報、直接識別情報、同意範囲外の原文は保存・exportしません。確証のない内容は `unknown` や仮説として残し、心理診断や重大な判断には使いません。

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

`data/self-models/subject/<slug>.json` と `.md` はGit管理するcurrent snapshotです。entityをcommitした後に `python3 tools/agent_runtime.py tools/bundle.py --all` で再生成し、`python3 tools/agent_runtime.py tools/bundle.py --all --check` でJSON/Markdownのstalenessを確認します。過去snapshotは別名fileではなくGit historyで比較します。

## 最小コマンド

```bash
python3 -m pip install -e .
python3 tools/agent_runtime.py tools/new_entity.py subject sample-subject
python3 tools/agent_runtime.py tools/build_graph.py --check
python3 tools/agent_runtime.py -m unittest discover -s tests -p "test_*.py"
python3 tools/agent_runtime.py tools/build_graph.py
python3 tools/agent_runtime.py tools/audit.py
```

## Agent harness

Phase 10の実装taskは、次のライフサイクルを正規経路として実行する。

```bash
python3 tools/agent_runtime.py tools/task_harness.py validate
python3 tools/agent_runtime.py tools/task_harness.py next --json
python3 tools/agent_runtime.py tools/task_harness.py claim SM-NNN --actor <actor> --remote origin --base <sha> --json
python3 tools/agent_runtime.py tools/task_harness.py context SM-NNN --json
python3 tools/agent_runtime.py tools/task_harness.py verify SM-NNN --json
python3 tools/agent_runtime.py tools/task_harness.py complete SM-NNN --pr <number> --commit <sha> --json
python3 tools/agent_runtime.py tools/task_harness.py release SM-NNN --actor <actor> --remote origin --json
```

`execution/tasks.yaml`が機械可読のtask SSOTで、Issueに登録されていないtaskは実行対象になりません。selectorは最低IDのready taskだけを返します。claimは`refs/heads/harness-lock/sm-nnn`と`agent/sm-nnn-<actor>`を取得し、verifyは許可pathとchecksを検査します。completeはPR番号・HEAD・evidenceを対象taskだけへ記録し、releaseはremote mainへのmerge確認後に該当lockだけを削除します。

すべてのリポジトリPythonコマンドは`tools/agent_runtime.py`を通すため、エージェントや人間が仮想環境をactivateする必要はありません。入口はPyYAMLをimportできるプロジェクト`.venv`を優先し、無ければ現在のPythonを使います。依存関係が利用できない場合は、installやnetwork accessを行わず、安定したエラーで停止します。

PRの`harness-policy`はbase SHAのtrusted-baseを実行系の正本としてcandidateを検査します。候補側のharness、queue contract、依存、stop condition、check、evidence、許可外pathの改変でtrusted policyを弱めることはできません。出力は本文、raw voice、credentials、環境値、絶対pathを含みません。利用者を実行エージェントに限定する脅威モデルでは、このtrusted-base CI検証を必須境界とし、GitHubのrequired merge gateは任意の運用強化とします。Issue #60にこの判断を記録します。
SM-026完了後は、依存済みのready taskがないためdispatchは終了します。

## 原則

- 原文・観測事実・解釈を混ぜない。
- Claimは根拠、反証、代替説明、確信度を持つ。
- Unknownを推測で埋めない。
- Trait / State / Contextを分離する。
- 正式尺度がなければ尺度得点を作らない。
- 同意範囲外のデータをexportしない。
- 心理診断、精神疾患推定、重大な人事判断には使わない。
