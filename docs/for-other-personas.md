# 利用者・他エージェント向け入口

## ゼロから始める実行手順（Issue #136）

まず [README の初期化と同意](../README.md#初めて自分の自己モデルを作るissue-136) の順に
`profile_root.py init --profile-root <絶対path> --subject <slug>` と、本人が確認した範囲での
`profile_root.py consent` を実行します。init は新しい path 専用で、同意は作りません。
保存先は、この repo 以外も含むすべての Git checkout の外を選びます。
**agent は本人に目的・操作・有効期限を確認せず、`--confirm-owner-consent` を付けてはいけません。**
同意の Source はヒアリング用の `conversation` です。全文や識別情報は保存しません。
同じ profile へ制作 run と育成 session が同時に書き込まないでください。

通常は親の制作 run の入口で本人に1問を提示し、下記の順序を親 agent が実行します。
単独で確認する場合も、repo のルートから同じ CLI を使えます。`first-run` は run ごとに
変える opaque な ID、`my-self` は init 時の slug です。

```bash
python3 tools/agent_runtime.py tools/growth_tasks.py hearing open \
  --profile-root /absolute/path/to/profile --subject subject/my-self \
  --requester first-run --purpose artistic-research --json
```

init → 同意の直後は `outcome: offered` となり、空のモデルの gap から最初の質問が出ます。
packet の `intent` → `why` → `anchors`（あれば）→ `question` を本人に提示し、実際の回答だけを
1 block に構造化します。`task_id` と `queue_sha256` は packet の値をそのまま使います。
以下の block は CLI を試すための**合成回答**です。実 profile へコピーせず、本人の回答に置き換えます。
未取得の項目は `null`、確認して該当なしの場合だけ `[]`、判定不能の場合だけ `unknown` とします。
日時は出来事の日時、slug はその出来事の opaque な識別子です。

```bash
python3 tools/agent_runtime.py tools/growth_tasks.py hearing answer <task_id> \
  --profile-root /absolute/path/to/profile --requester first-run \
  --purpose artistic-research --subject subject/my-self \
  --expected-queue-sha256 <queue_sha256> --json <<'ANSWER'
[event: first-observation]
observed_at: "2026-10-04T09:00:00+00:00"
precision: minute
domain: creative-practice
social: alone
uncertainty: unknown
control: self-directed
fatigue: null
stress: unknown
trigger: 作業順序を急に変える必要が出た
observed_fact: 小さく試してから順序を決めた
raw_voice: "急に全部を変えるのは避けたい"
appraisal: null
emotion: null
body: null
cognition: null
action: 小さく試した
immediate_outcome: 作業を再開した
delayed_outcome: null
ANSWER
python3 tools/agent_runtime.py tools/export_signals.py \
  --profile-root /absolute/path/to/profile --subject subject/my-self \
  --purpose artistic-research --operation export-signals --requester first-run \
  --output /absolute/path/to/profile/data/self-signals.json
```

shell history に本人の回答を貼らず、実運用では agent が stdin に渡します。
`answer_format: slot-yaml` の場合は上の event block を使わず、packet の指定 slot と任意の
raw_voice を YAML で stdin に渡します。断る・無応答なら下記を使い、その後も export します。

```bash
python3 tools/agent_runtime.py tools/growth_tasks.py hearing skip <task_id> \
  --profile-root /absolute/path/to/profile --requester first-run --reason skipped --json
```

### 何回で signal が出るか

合成の空 profile で上記手順を再現した結果は、`init` → 明示同意 → `offered` → 1回答で
`answered` → export 成功（`signal_count: 0`）です。ヒアリングは Event を書き、
Claim や Pattern を自動生成しません。回数を増やすだけでは exported signal は出ません。

次に agent が [調査・記録手順](investigation-task.md) に従い、Event から1件の Claim を
外部 `entities/claims/` に派生する作業を行います。テンプレートの入口は次です。
作成直後の空テンプレートは未完成なので、検証・export の前に根拠と各フィールドを埋めます。

```bash
python3 tools/agent_runtime.py tools/new_entity.py claim first-hypothesis \
  --profile-root /absolute/path/to/profile --subject subject/my-self
```

上の合成回答では、次の値を持つ Claim を1件作ると export は `signal_count: 1` になりました。
これは説明用の合成仮説であり、本人についての推論ではありません。

| フィールド | 合成例／必要条件 |
|---|---|
| layer / motivation_direction / scope | `motivation` / `avoid` / `state` |
| statement | 急な全面変更を避けたい可能性がある |
| conditions | 作業順序が急に変わる場面 |
| supporting_evidence | hearing answer が返した `entity` の Event ID |
| counterevidence | 探索して未発見なら `[]`。未探索を該当なしにしない |
| alternative_explanations | 今回だけ時間が足りなかった／手順を比較するために試した |
| confidence / status | `low` / `hypothesis` |

探索内容・未確認事項は Claim の本文に残し、構造検証後に export を再実行します。
最短は **1回答 + 1件の agent による Claim 派生作業**です。export の確度は `uncertain`、
`avoids` が1件になります。これを supported な自己像として扱いません。
既存 gap queue は Event 収集・反証探索等を案内しますが、初回 Event からの Claim 派生を
自動予約するとは限りません。agent の上記作業を省くと何回回答しても signal が0のままです。

```bash
python3 tools/agent_runtime.py tools/build_graph.py --profile-root /absolute/path/to/profile
python3 tools/agent_runtime.py tools/build_graph.py --check --profile-root /absolute/path/to/profile
python3 tools/agent_runtime.py tools/growth_tasks.py generate --profile-root /absolute/path/to/profile
python3 tools/agent_runtime.py tools/growth_tasks.py next --profile-root /absolute/path/to/profile --json
python3 tools/agent_runtime.py tools/growth_tasks.py report --profile-root /absolute/path/to/profile
```

queue task の claim → 作業 → complete は [育成 session の運用](operations.md) に従います。
親の候補生成に十分な signal の量・種類は親側で判定します。空の export を案内する親 #273
との連携では、この init → 同意の入口を使います。signal 1件は候補生成全体の成功保証ではありません。
長期の milestone は [growth-milestones.yaml](../config/growth-milestones.yaml) の全6欄の
supported な根拠と反証、2 context、30日の幅です。固定の回答回数や session 回数での達成は保証しません。
単発 Event から Pattern や Trait は確定しません。

## 読む

```bash
cat overviews/coverage.md
python3 tools/agent_runtime.py tools/bundle.py --subject subject/<id> --profile-root /absolute/path/to/profile
python3 tools/agent_runtime.py tools/bundle.py --all --check --profile-root /absolute/path/to/profile
python3 tools/agent_runtime.py tools/export_signals.py --subject subject/<id> --purpose artistic-research --operation export-signals --profile-root /absolute/path/to/profile
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

## 制作runの入口（Issue #118）

全repoをつないで制作プランまで進める場合は、親の [READMEの最短ルート](https://github.com/masa-san-jp/agentic-art-orchestration#利用者向けの最短ルート)から始める。環境準備、offline体験、pin済みworkspace、`credential_free.py`経由のヒアリングとrun、出力・復旧の詳細は [agent runtime guide](https://github.com/masa-san-jp/agentic-art-orchestration/blob/main/docs/agent-runtime-guide.md)を参照する。

制作runを実行する利用agentは、`export_signals.py`を呼ぶ前に、次の順で本人へのヒアリングを行える。実装は[`docs/operations.md`](operations.md)、固定例は[`tests/contracts/growth-hearing-v1.fixture.json`](../tests/contracts/growth-hearing-v1.fixture.json)。

1. `hearing open`を実行する。`outcome: offered`なら2へ、それ以外（`unavailable`、非零終了、timeout）は4へ進む。
2. `offered`のpacketにある`intent`（全行、省略不可。言い換え可）→`why`→`anchors`（あれば、本人の過去の言葉として質問の前に示す）→`question`を、本人に1問だけ提示する。項目を増やさない。
3. 本人が答えたら1 blockに構造化して`hearing answer`。断られた・無応答なら`hearing skip`。どちらの結果でも4へ進む。
4. `export_signals.py --purpose <p> --profile-root <root> --requester <run-id>`を実行し、制作計画へ進む。

**不変条件**: ヒアリングの結果が何であれ（`answered`/`skipped`/`unavailable`）、また`hearing`系CLIが非零終了・timeoutしても、runは止めず4へ進む。ヒアリングはexportの前提条件ではない。回答は今回の計画には反映されず、次回以降の育成taskの解消として効く。

回答文・`anchors`・`intent`等のpacket内容は、利用agent側のevidence・state・Git・公開projectionへ保存しない。

## 禁止

- Self Modelから診断名を作る。
- 将来行動を確定予測する。
- confidenceを人間の価値スコアとして比較する。
- raw voice refsから許可なく原文を公開する。
- 同意確認を迂回して上流entityを直接読む。

追加・訂正はIssueに「対象Subject/Event」「根拠Source」「目的」「必要期限」を書く。entitiesや生成物を直接変更しない。
