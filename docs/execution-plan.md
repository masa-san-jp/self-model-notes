# 自律実行計画

機械可読な正本は [`execution/tasks.yaml`](../execution/tasks.yaml) です。本書は人間向けの説明です。

## 方針

低〜中位の実行系モデルが設計判断を抱え込まないよう、作業を「1つの成果物」「限定されたpath」「明示されたテスト」「停止条件」に分割する。設計の変更と実装を同じタスクにしない。

## Phase

| Phase | 目的 | Exit gate |
|---|---|---|
| 0 Foundation | 正本、責務境界、エージェント契約を固定 | Issue #1、schema、task queueが相互参照 |
| 1 Structural Validation | entityを壊さず保存できる | hard validationとnegative testsが通る |
| 2 Derivation | graph、Self Model、bundleを決定論的生成 | 同じ入力でbyte-identical output |
| 3 Epistemic Audit | 人間理解としての弱点を可視化 | auditが非ゼロ終了せず調査課題を出す |
| 4 Privacy & Export | 同意境界付きsignal export | 許可外exportがhard fail |
| 5 E2E & Operations | 匿名fixtureで全経路を通す | CI、pre-commit、runbookが通る |
| 6 Ecosystem Integration | 4 repository間contractを固定 | consumer contract testが通る |

## エージェント割当規則

- 1エージェント1タスク1PR。
- `depends_on`未完了は開始しない。
- `allowed_paths`が重なるタスクは直列化する。
- schema判断が必要なら実装タスクを止め、decision issueへ分離する。
- 実行中に発見した改善は現在PRへ混ぜない。

## Definition of Ready

- 依存タスクがdone。
- 受入条件が機械検証または明確なレビュー項目になっている。
- 変更可能pathが列挙されている。
- fixtureまたは入力例がある。
- 未決定のschema判断がない。

## Definition of Done

- acceptance全件達成。
- checks全件成功。
- 正常系と禁止事項のテストがある。
- 生成物が最新。
- 機微情報・直接識別情報・秘密情報がdiffにない。
- docsとCLIの例が実際に動く。
- task evidenceにPR/commit/check結果が記録される。

## 停止条件

- Issue #1とschemaの矛盾。
- 同意や倫理要件を弱めないと実装できない。
- 正式尺度の得点推定が必要。
- pattern昇格等の未検証閾値を固定する必要。
- sibling KBのcore schema変更が必要。
- テスト失敗の原因が他エージェントの変更で、自分のpath外。

## 完成判定

Issue #1の第1マイルストーン受入条件を`docs/acceptance-matrix.md`でテスト・成果物へ対応づけ、全行が自動または証拠付きreviewでgreenになった時点で基盤完成とする。

## Harness CLI contract

Phase 10以降の実行taskは、execution/tasks.yamlを直接解釈せず、次のread-only selectorを入口にする。

    python3 tools/task_harness.py validate
    python3 tools/task_harness.py next --json

validateが失敗したqueueは実行対象にしてはならない。nextは依存がdoneであるready taskのうち、IDの数値が最小の1件だけを返す。選択可能なtaskがない場合は、taskをnullにしたJSONを返し、終了コード3で終了する。検証エラーは終了コード2とし、ソート済みで機械的に比較可能なエラーだけをstderrへ出力する。

CLIはqueueを読み取るだけで、Git、ネットワーク、queueファイルへの書き込みを行わない。JSONは固定キーを持つcanonical形式とし、entity本文、raw voice、秘密、認証情報、絶対パスを出力しない。

SM-021以降のライフサイクル操作は、選択したtaskだけを対象に次のCLIを使う。

    python3 tools/task_harness.py claim SM-NNN --actor ACTOR --remote REMOTE --base SHA --json
    python3 tools/task_harness.py context SM-NNN --json
    python3 tools/task_harness.py release SM-NNN --actor ACTOR --remote REMOTE --json

claimは固定ref `refs/heads/harness-lock/sm-NNN` をnon-force pushで取得し、成功時だけ `agent/sm-NNN-actor` branchとqueueの `in-progress` claimを作る。失敗時はqueue bytes、開始branch、既存lockを保持する。releaseはremote main上のdone taskと証拠、actor、branch、lock payloadの一致を確認してから、該当lockだけを削除する。

変更pathのguardは、active claimのbaseからHEADまでのcommit差分に加え、作業中のstage済み・未stage・untracked差分を検査する。rename/copyはsourceとdestinationの両方を対象とし、allowed_pathsにないpathを1件でも検出したら終了コード4で停止する。

    python3 tools/task_harness.py verify-paths SM-NNN --base SHA --json
    python3 tools/task_harness.py verify-paths SM-NNN --base SHA --committed-only --json

active claimのtaskを完了するときは、宣言されたchecksを順序どおり検証した同じ実行で、次のcompleteを使う。completeはPR番号と現在HEADのcommitを要求し、成功した場合だけstatus、claim、evidenceを同一task block内で更新する。

    python3 tools/task_harness.py verify SM-NNN --json
    python3 tools/task_harness.py complete SM-NNN --pr NUMBER --commit SHA --json
