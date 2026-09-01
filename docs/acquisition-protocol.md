# データ取得プロトコル

## 取得前

1. Subjectを疑似匿名IDで作る。
2. 利用目的を`allowed_purposes`から選ぶ。
3. Sourceごとに同意、許可操作、有効期限、撤回を記録する。
4. 原文の保管場所とGitへ置く最小引用を分ける。

## 取得

- 抽象的な自己評価より具体的な出来事を優先する。
- 「いつ、どこで、何が起き、何をし、直後にどうなったか」を先に取る。
- 本人の比喩・言い回しはraw_voiceとして保持する。
- 回答拒否、スキップ、中断はUnknownとして尊重する。
- 応答時間、書き直し等は観測事実としてのみ保存する。

### 未取得・確認済み空・評価不能の区別

Eventのslotは、取得状態を次のとおり保存する。意味を後から推測して相互変換しない。

- `null`: 質問していない、取得していない、または観測されていない。
- `[]`: 確認した結果、該当する値が存在しなかった。
- `unknown`: 評価を試みたが、回答拒否・情報不足などで判定できなかった。

未取得のslotへ`[]`や`unknown`を記録してはならない。確認済みで該当なしの場合だけ`[]`を使う。

## 注釈付き会話のfile intake

会話ログ・音声文字起こしを取り込むときは、自由文を推測で分析せず、エージェントまたは人が確認した注釈付きUTF-8テキストを入力する。`tools/intake_conversation.py`はネットワーク、LLM、外部コネクタを使わず、明示された`[event: slug]`ブロックだけをSource/Event draftへ変換する。

Source metadataはYAMLまたはJSONで、`source_slug`、`subject`、`source_kind`、`captured_at`、opaqueな`locator`、完全な`consent`（`obtained`、`obtained_at`、`purposes`、`allowed_operations`、`expires_at`、`revoked_at`、`notes`）を必須とする。`allowed_operations`にはintakeのため`store-reference`を含める。未同意、撤回済み、期限切れ、目的・操作の欠落時はdraftを作らない。

Transcriptは次のline-oriented形式を使う。`domain`や`observed_fact`などの複数値slotは行を繰り返せる。質問していないslotは`null`、確認して該当しないslotは`[]`、評価不能は`unknown`を明示し、フィールドを省略しない。

```text
[event: planning]
observed_at: "2026-08-30T09:05:00+09:00"
precision: minute
domain: creative-practice
social: alone
uncertainty: unknown
control: self-directed
fatigue: null
stress: unknown
trigger: deadline changed
observed_fact: task order changed
raw_voice: "自分で決め直したい"
appraisal: []
emotion: []
body: []
cognition: []
action: plan changed
immediate_outcome: resumed work
delayed_outcome: []
```

複数の出来事は別々の`[event: slug]`ブロックにする。1ブロック内の曖昧な重複項目、自由文、直接識別情報（名前、email、電話番号、住所、URL）、120文字を超える`raw_voice`引用はnamed remediation付きで拒否する。raw voiceは最小引用だけをEventへ置き、原文全文はGitへ保存せず、Sourceのopaque locatorへ分離する。

実行例は次のとおり。出力先はcanonicalな`entities/`の外に置き、生成されたdraftを本人が確認してから適切な`entities/sources/`と`entities/events/`へ採否する。既存entityの上書き、Claim/Pattern/Derivedの生成、commitは行わない。

```bash
python3 tools/agent_runtime.py tools/intake_conversation.py \
  /path/to/annotated-transcript.txt \
  --metadata /path/to/source-metadata.yaml \
  --output-dir /tmp/self-model-intake/conversation-20260901
```

生成されるのは`*.source.draft.md`と`*.event.draft.md`だけである。出力が既存`entities/`内、同意不備、直接識別情報、出来事混在、入力形式不備の場合は、検証を完了してから一切書き込まず停止する。

## 取得後

1. Source locatorと取得時刻を確定する。
2. Eventへ切り分ける。複数出来事を1 Eventに詰め込まない。
3. 直接識別情報を削除する。
4. source_refsが原典へ到達可能か確認する。
5. 分析は別タスクとして実行する。

## 研究仮説としてのみ保持するもの

- 幼少期を経由すると防衛が下がる。
- 同じ話題への回帰が核を示す。
- 入力停止や削除が内面強度を示す。

これらを取得ロジックやスコアへ固定しない。
