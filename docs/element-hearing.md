# 要素ごとのヒアリングと派生（Issue #138 / SM-050）

Issue #138 の新しい入口は `growth_tasks.py element`。既存の `hearing/v1` API は
旧 pin の互換経路として残す。親 wrapper は新しい pin でこの入口へ切り替える必要がある。
親のコード・state は本変更では変更しない。実 run の本人評価が完了するまで、旧経路の撤去や
SM-050 の `done`、trusted-base acceptance は宣言しない。

## 境界と契約

`schemas/element-request.schema.json` と `schemas/element-answer.schema.json` は親
`agentic-art-orchestration` の同名ファイルからそのまま複製した。
参照 checkout HEAD: `a3e9e00fc6f7ab12285216bdf48040aa37d12f65`。
読み取り専用の参照: `/Users/masa/aa-work/lanes/L278/schemas/`。

- request SHA-256: `dec0cc4987bad6ade858becb1e741ac74848df79406995b4cdbdfafe9f0ef1d7`
- answer SHA-256: `7b74d114e0d9ee8976dc8ed17e43623b1e5a490a08c6bb4f1a5118b78c6cc812`

機構は親の `docs/20261007-element-harness-design.md` 2〜3章、段階 A1 に従う。
推論の返答は親と同じ `element-answer/v1` の一つの値。プログラムが検査し、失敗時には
同じ依頼の `attempt` だけを増やす。5回失敗すると `BLOCKED`。
`previous_failure` は検査名と固定の短い理由だけで、不正な返答や絶対 path を含めない。
不正な envelope / stale attempt は受理せず、試行回数を消費しない。

全依頼、受理した値、確認待ち草案、run 状態は選択した external-local profile の
`growth/elements/state.json` にのみ保存する。state は atomic 更新、mode 0600、全 run 共通の
lock を使う。symlink の growth ディレクトリ・state・entity path を拒否する。
`next_action` は一時的な relay 用 stdout であり、本人の言葉を含む。
親はこれを画面または答え手へ中継し、依頼・答え・質問・stdout/stderr を親 state、repo、log に保存しない。
親で保存してよいのは run ID、status など本文を含まない情報だけ。
リポジトリ privacy guard は element request/answer/state の混入も拒否する。

## CLI と進行

すべて repo の runtime を通し、明示的な `--profile-root`、run ID、同意目的を指定する。
`--subject subject/<id>` は複数 Subject の profile では必須。
この例の path は操作対象の placeholder で、実 profile の自動探索は行わない。

```bash
python3 tools/agent_runtime.py tools/growth_tasks.py element next \
  --profile-root /absolute/path/to/profile --run-id run-one --purpose artistic-research
```

返り値は `run_id`、`status`、`next_action`、`blocked`。
`status: WAITING` なら `next_action: {kind: element, request: ...}`。
答え手はその request だけから一つの値を返す。

```bash
python3 tools/agent_runtime.py tools/growth_tasks.py element answer \
  --profile-root /absolute/path/to/profile --run-id run-one --purpose artistic-research < answer.json
```

`answer.json` は中継の形を示す名前。本人の言葉を含むファイルを repo や親 state に作らない。
通常は stdin へ直接渡す。答えは `contract_version`, `run_id`, `element_id`, `attempt`, `value`
だけを持ち、identity と attempt が依頼に一致する必要がある。
一度受理済みの古い answer は stale として拒否する。応答を失った wrapper は `next` で現在地を再取得する。

`status: HEARING` なら `next_action.kind: hearing` に質問と「何に効くか」の一文が出る。
本人の回答は既存 intake の annotated Event block として stdin へ渡す。
block の date/context/action 等は既存契約に従う明示値で、本人の回答から推測して埋めない。

```bash
python3 tools/agent_runtime.py tools/growth_tasks.py element respond \
  --profile-root /absolute/path/to/profile --run-id run-one --purpose artistic-research
```

質問の材料になる最近の raw quote がない場合は `SEED_REQUIRED`。
この場合は推論による質問を捏造せず、最近の出来事一件を記録する案内を出す。
同じ `respond` で Event を保存し、主張の推論から始める。
`element skip` は質問・確認を断る経路。export は別操作で継続でき、草案は消さない。

主張文 → 層の列挙 → 回答 Event の参照を各一要素で作る。
動機の場合は方向、scope、条件（context-bound の場合）、異なる代替説明2件も各一要素にする。
一件の回答から trait を作らない。confidence は `unknown`、反証未探索は `null` を保持する。
ID、日付、参照結線、meta の組み立てはプログラムが行う。

`COMPLETED` はこの run の推論が終わり、草案が本人確認待ちになった状態。
まだ canonical entities や export には出ない。
別の run ID の `next` は冒頭で過去 run の草案を一つずつ `CONFIRMATION` として聞く。
本人の「はい／いいえ」を relay する専用操作は次のとおり。答え手が代わりに確認してはいけない。

```bash
python3 tools/agent_runtime.py tools/growth_tasks.py element confirm \
  --profile-root /absolute/path/to/profile --run-id run-two --purpose artistic-research \
  --owner-answer yes
```

`no` は草案を rejected として profile 内に保持する。
`yes` は根拠 Event の fingerprint、実在参照、同意、既存 validator を再確認してから
既存型の `hypothesis` として `entities/claims/` または `entities/patterns/` へ保存する。
確認は文の本人評価であり、supported や確度の昇格ではない。
これにより既存 exporter 自体や既存レコードの export 意味を変えず、新経路の未確認文を全 export から隔離する。

## 決定論と失敗条件

質問対象は現在の export と同じ `_signal_groups` で得た各欄の distinct Event 根拠数が少ない順。
同数時は `tensions → recurring_patterns → seeks → avoids → protects → states → contexts`。
全欄を候補にし、traits は複数 Context・時点の根拠を要求するため一件のヒアリング対象にしない。
body/emotion slot の空欄やアルファベット順では選ばない。

問いの inputs は最近の出来事一件の raw quote（既存の120字上限）と対象説明一つだけ。
60字以内、一文、末尾 `?`/`？`、出来事中の語を含むこと、question-bank の forbidden_tokens、
直接識別パターン、テーマ・slug・依頼文への言及を機械検査する。
制作のテーマや依頼文は inputs に渡さず、生成済み原文の単純コピーも派生文として受け付けない。
問いと一緒に示す効用文は欄ごとの固定表で、本人確認前の export や即時反映を約束しない。

同じ語・形の判定は意味推論を使わず、raw quote の連続した漢字2文字以上・カタカナ2文字以上・
英数字語3文字以上、または trigger/action の同一文字列を distinct Event ごとに数える。
回答 Event を含む二件以上があるときだけ、共通の形と二件の raw quote を材料に
Pattern 文一つを依頼する。文には数えた共通の形を要求する。
これは保守的な文字列一致で、同義語や言い換えは検出しない。件数は推論への入口条件であり昇格基準ではない。
Pattern も別に本人確認されるまで export しない。
Pattern は生成元 Claim を参照し、その Claim が本人確認されていなければ保存を拒否する。
Claim を「いいえ」とした場合、続く Pattern も「いいえ」または skip にできる。

同意はすべての遷移で再確認する。根拠 Event が変われば推論・確認は失敗し、
失敗した Event 作成や確認時の書き込みは rollback する。失敗値の本文をエラーに出さない。
CLI の ERROR は固定コードのみ。状態が進まなければ wrapper は `next` で再取得する。

## 検証と残る human gate

合成 temporary profile の test は質問生成、要素再試行、回答 Event、草案、次 run の
本人確認、signal export、Pattern の二件条件、同意撤回、改変、privacy guard、symlink 拒否を検証する。
実 profile はテストに使用しない。
Issue #138 の実 run でマサさんが「意味のある質問」と認める条件は、実 profile に触れないという
オーケストレーター指示により実装エージェントでは未実施。オーケストレーターの pin/wrapper 更新後の
本人確認で完了を判断する。
