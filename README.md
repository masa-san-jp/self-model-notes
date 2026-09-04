# self-model-notes

本人を固定的な性格タイプへ分類せず、同意を得た観測から、根拠・反証・不確実性を残したままSelf Modelを更新するKnowledge Baseです。決定論的なbundle生成と、Claude Code / Codexなどの実行エージェント向けtask harnessを含みます。

## これは何か

このリポジトリは、次の記録を安全に積み上げて派生モデルと研究用signalを生成します。

```text
Source → Event → Claim → Pattern → Derived Self Model → Research Signal
```

- 選択した外部profile rootの`entities/`にあるMarkdown frontmatterが正本です。protocol repository内の`entities/`はREADME、template、synthetic fixtureだけを置きます。
- `data/`と`overviews/`はツールが生成するsnapshotです。直接編集しません。
- exportはSourceごとの同意を再確認し、raw voice本文や直接識別情報を出力しません。
- 診断、疾患推定、雇用判断、将来行動の確定予測には使いません。

要件の正本は[Issue #1](https://github.com/masa-san-jp/self-model-notes/issues/1)です。詳細な作業契約は[AGENTS.md](AGENTS.md)、データ設計は[docs/schema.md](docs/schema.md)を参照してください。

## 外部profile rootと実行境界

実データを扱うときは、`profile.yaml`とcanonical entityをprotocol repositoryの外に置きます。profile契約は`self-model-profile/v1`で、必須値は`contract_version`、`profile_id`、`subject_ids`、`storage_scope: external-local`です。profile rootは既存の通常ディレクトリで、repository、worktree、public projectionの外側でなければなりません。

`build_graph.py`、`build_self_model.py`、`bundle.py`、`audit.py`、`export_signals.py`、`new_entity.py`、`intake_conversation.py`の実データ実行には、毎回明示的な絶対pathを渡します。環境変数やrepository内`entities/`への暗黙fallbackはありません。生成物は選択したrootの`data/`と`overviews/`だけへatomicに書かれます。

```bash
python3 tools/agent_runtime.py tools/build_graph.py --profile-root /absolute/path/to/profile
python3 tools/agent_runtime.py tools/bundle.py --all --profile-root /absolute/path/to/profile
python3 tools/agent_runtime.py tools/export_signals.py \
  --subject subject/<id> --purpose artistic-research \
  --operation export-signals --profile-root /absolute/path/to/profile
```

移行はまずmetadata-onlyのplanを確認し、apply時だけ明示したapproval fileを渡します。plan/applyは`profile.yaml`やentity本文を出力せず、既存destinationを上書きせず、sourceを削除・移動しません。実n=1のapplyは人間承認Issue [#82](https://github.com/masa-san-jp/self-model-notes/issues/82)が完了するまで停止します。

```bash
python3 tools/agent_runtime.py tools/migrate_profile.py plan \
  --source /absolute/path/to/source-profile \
  --destination /absolute/path/to/new-profile --json
python3 tools/agent_runtime.py tools/migrate_profile.py apply \
  --source /absolute/path/to/source-profile \
  --destination /absolute/path/to/new-profile \
  --approval-file /absolute/path/to/approval.yaml --json
```

移行前のrepositoryにlegacy recordが残る間は、`profile_root.py validate-repository --json`が`BLOCKED_LEGACY_PROFILE`を返します。これはデータを消す指示ではなく、人間承認の移行ゲートです。

## 誰が使うか

- 実行エージェント：`tools/agent_runtime.py`を唯一のPython入口として、taskの選択・検証・完了を行います。
- 記録・調査担当：同意範囲を確認して`entities/`を更新し、生成物と検証結果を確認します。
- 下流の研究・制作システム：同意済みの`export_signals.py`出力だけを読み取ります。

親orchestrationのlive-private実行結果（その時点のデータ件数など）は、このリポジトリの実装完了条件ではありません。実行時に不足があれば、harnessは不足状態を隠さず停止します。

## 兄弟リポジトリとの関係

このリポジトリは、本人に関するSource・Event・Claim・Pattern・Derivedの正本です。兄弟リポジトリはそれぞれ別の知識領域または実行段階を担当し、このリポジトリの`entities/`を直接の正本として共有・編集しません。リポジトリ間で渡すのは、目的と同意を確認したprivacy-safeなnormalized signalまたはresearch handoffです。

| リポジトリ | 担当 | このリポジトリとの関係 |
| --- | --- | --- |
| [self-model-notes](https://github.com/masa-san-jp/self-model-notes) | 本人に関するEvidence-traceable Self Model | このリポジトリ自身。個人データとその根拠の正本を持つ |
| [art-history-notes](https://github.com/masa-san-jp/art-history-notes) | 美術史の時代・地域・作家・作品・技法・文脈 | 個人推定を行わない、独立したart-history signalの上流 |
| [marketing-trends-notes](https://github.com/masa-san-jp/marketing-trends-notes) | 市場変化・trend・practiceと新しい根拠 | 個人推定を行わない、独立したtrend/practice signalの上流 |
| [agentic-art-research](https://github.com/masa-san-jp/agentic-art-research) | 複数signalを束ね、根拠付きresearch brief/traceを作るruntime | このリポジトリの`research_signals`を、他の上流signalとともに読む下流 |
| [agentic-art-production](https://github.com/masa-san-jp/agentic-art-production) | 制作プロトコル、handoff、再開可能な実行・release gate | 検証済みのresearch handoffの先にある制作実行側。Self Modelの正本ではない |
| [agentic-art-orchestration](https://github.com/masa-san-jp/agentic-art-orchestration) | 複数リポジトリのpin・gate・目的E2Eを調整する親側 | このリポジトリを含む各システムを横断して組み合わせるが、個人データの正本ではない |

主な受け渡しは次の通りです。orchestrationはデータの所有者ではなく、各リポジトリの入力・検証・E2Eを調整します。

```text
self-model-notes ──────── research_signals ─┐
art-history-notes ─────── art-history signals ├─→ agentic-art-research
marketing-trends-notes ─ trend/practice ─────┘          │
                                                        └─ research handoff → agentic-art-production

agentic-art-orchestration ── 各リポジトリのpin・gate・目的E2Eを調整
```

詳細な責務とsignal境界は[docs/ecosystem-architecture.md](docs/ecosystem-architecture.md)を参照してください。下流へ渡るのは明示した目的のexportだけであり、`raw_voice`、直接識別情報、秘密、認証情報は渡しません。

## 最初に使う

### 前提

- Python 3.11以上
- Git
- PyYAML 6.x

通常の実行で仮想環境をactivateする必要はありません。すべて次の入口から実行してください。

```bash
python3 tools/agent_runtime.py <python-arguments>
```

入口は、PyYAMLをimportできるリポジトリ内`.venv/bin/python`を優先し、なければ現在のPythonへfallbackします。installやnetwork accessは行いません。どちらのPythonにもPyYAMLが無い場合は、安定したエラーで停止します。

### 初回だけの依存関係準備

現在のPythonでPyYAMLを使えるなら、この手順は不要です。使えない場合だけ、開発環境で一度実行します。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

以後のエージェント実行では、activateや毎回のinstallは不要です。

### まず確認するコマンド

```bash
python3 tools/agent_runtime.py tools/task_harness.py validate
python3 tools/agent_runtime.py tools/task_harness.py next --json
python3 tools/agent_runtime.py -m unittest discover -s tests -p "test_*.py"
```

`next --json`がtaskを返せば作業対象があります。候補が無い場合の終了コード`3`は異常ではなく、現在実行できるtaskが無いという正常な結果です。queue不正は`2`、許可外pathは`4`、宣言checkの失敗は`5`です。

## 記録を読む・生成する

```bash
python3 tools/agent_runtime.py tools/bundle.py --subject subject/<id> --profile-root /absolute/path/to/profile
python3 tools/agent_runtime.py tools/bundle.py --all --check --profile-root /absolute/path/to/profile
python3 tools/agent_runtime.py tools/export_signals.py \
  --subject subject/<id> \
  --purpose artistic-research \
  --operation export-signals \
  --profile-root /absolute/path/to/profile
```

新しいentityはtemplateから作成し、frontmatterを埋めます。外部profile rootの`entities/`が正本であり、生成後にsnapshotのstalenessを確認します。

```bash
python3 tools/agent_runtime.py tools/new_entity.py event <slug> --subject subject/<id> --profile-root /absolute/path/to/profile
python3 tools/agent_runtime.py tools/build_graph.py --profile-root /absolute/path/to/profile
python3 tools/agent_runtime.py tools/bundle.py --all --profile-root /absolute/path/to/profile
python3 tools/agent_runtime.py tools/bundle.py --all --check --profile-root /absolute/path/to/profile
```

exportは目的と操作の両方を明示し、Sourceの同意が1件でも不足していれば全体をdenyします。

## エージェントのtask実行

Phase 10の実装taskは、`execution/tasks.yaml`を直接解釈・編集せず、次の順序でharnessを使います。

```bash
python3 tools/agent_runtime.py tools/task_harness.py next --json
python3 tools/agent_runtime.py tools/task_harness.py claim SM-NNN --actor <actor> --remote origin --base <sha> --json
python3 tools/agent_runtime.py tools/task_harness.py context SM-NNN --json
python3 tools/agent_runtime.py tools/task_harness.py verify SM-NNN --json
python3 tools/agent_runtime.py tools/task_harness.py complete SM-NNN --pr <number> --commit <sha> --json
python3 tools/agent_runtime.py tools/task_harness.py release SM-NNN --actor <actor> --remote origin --json
```

1 task、1 agent、1 branch、1 PRが基本です。Issueに登録されていないtaskは実行しません。詳細なpath制限、claim lock、trusted-base検証、復旧手順は[docs/operations.md](docs/operations.md)を参照してください。

## ディレクトリ

```text
entities/          Subject / Source / Event / Claim / Pattern / Measurementの正本
config/            閉じた語彙、Drive Systems、Context、Confidence
docs/              schema、分析、取得、倫理、相互運用、実行計画
execution/         エージェントが順に消化する機械可読task
tools/             作成、検証、監査、bundle、export
tests/             単体・統合・禁止事項テストと匿名fixture
data/              決定論的な生成snapshot
overviews/         coverageなどの生成文書
```

## 現在の状態

自律task harnessと、仮想環境を自動選択するruntime entrypointは`main`にあります。登録済みtaskの実行可能性は、次のコマンドの結果を正本とします。

```bash
python3 tools/agent_runtime.py tools/task_harness.py validate
python3 tools/agent_runtime.py tools/task_harness.py next --json
```

GitHub ActionsはPRの独立したbackstopです。agent-only脅威モデルでは、trusted-baseのローカルharness検証が正規境界であり、GitHubのrequired merge gateは任意の運用強化です。

この判断は[Issue #60](https://github.com/masa-san-jp/self-model-notes/issues/60)に記録したSM-026の方針です。費用、公開範囲、branch protectionなどの設定変更は、このリポジトリの実行エージェントが独断で行いません。
