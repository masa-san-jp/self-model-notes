# 要素ごとのヒアリングと派生（Issue #138 / SM-050）

Issue #138 の新しい入口は `growth_tasks.py element`。既存の `hearing/v1` API は
旧 pin の互換経路として残す。親 wrapper は新しい pin でこの入口へ切り替える必要がある。
親のコード・state は本変更では変更しない。実 run の本人評価が完了するまで、旧経路の撤去や
SM-050 の `done`、trusted-base acceptance は宣言しない。

## 境界と契約

`schemas/element-request.schema.json` と `schemas/element-answer.schema.json` は親
`agentic-art-orchestration` の同名ファイルからそのまま複製した。
参照 checkout HEAD: `a3e9e00fc6f7ab12285216bdf48040aa37d12f65`。
出所: masa-san-jp/agentic-art-orchestration の `schemas/element-request.schema.json` と `schemas/element-answer.schema.json`（#278、commit 5f7e186）。

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
全 element 操作で `--requester` を `--run-id` の別名として受け付ける。
親は同じ不透明な requester を同一 run の全操作へ渡す。返答と element envelope のキーは
別名によらず `run_id` のままで、本人確認には新しい requester/run ID を使う。
`--subject subject/<id>` は複数 Subject の profile では必須。
この例の path は操作対象の placeholder で、実 profile の自動探索は行わない。

```bash
python3 tools/agent_runtime.py tools/growth_tasks.py element next \
  --profile-root /absolute/path/to/profile --run-id run-one --purpose artistic-research
```

返り値は `run_id`、`status`、`next_action`、`blocked`。
`status: WAITING` なら `next_action: {kind: element, request: ...}`。
答え手はその request だけから一つの値を返す。

親 wrapper（#280）の中継契約は次のとおり。`next` は現在地を再取得する操作であり、
以下の owner 操作を推論の `answer` に置き換えない。本文の保存先は常に profile 内だけ。

| status | next_action | 親が中継する相手・次の操作 |
|---|---|---|
| `WAITING` | `kind: element`, `request: element-request/v1` | 答え手へ依頼だけを渡し、`element-answer/v1` を `answer` のstdinへ渡す |
| `HEARING` | `kind: hearing`, `question`, `why`, `answer_format: event-block` | 本人へ質問と効用を提示し、明示値を持つ annotated Event block を `respond` のstdinへ渡す |
| `CONFIRMATION` | `kind: hearing`, `question`, `why`, `answer_format: yes-no` | 本人へ一件ずつ確認し、はい／いいえを `confirm --owner-answer yes|no` へ渡す |
| `SEED_REQUIRED` | `kind: hearing`, `question`, `why`, `answer_format: event-block` | 本人の最近の出来事を聞き、Event block を `respond` のstdinへ渡す |
| `COMPLETED` | `null` | 推論終了。草案の本人確認は次の新しいrunへ回す |
| `SKIPPED` | `null` | 本人が今回は答えないとした状態。未確認草案は次のrunでも確認できる |
| `BLOCKED` | `null`, `blocked: {element_id, failures}` | 同じ要素が5回失敗。依頼本文を親のstateやlogへ保存せず停止する |

`respond` は `HEARING` / `SEED_REQUIRED`、`confirm` は `CONFIRMATION`、`skip` はこの三状態でのみ使う。
`skip` は現在runを `SKIPPED` にし、本人の拒否を推論で補完しない。
成功した操作（`BLOCKED` 以外）の終了コードは0、`BLOCKED` と固定コードの `ERROR` は2。
不正／古いanswerの `ERROR` では試行回数を消費せず、親は `next` で現在地を再取得する。

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

主張文の依頼は回答 Event のraw quoteと、質問対象の `item_description` を渡す。
欄から決まるフィールドは推論せず、プログラムで固定する。
`tensions` は layer=`tension`、`seeks` / `avoids` / `protects` は layer=`motivation` と
対応する方向 `seek` / `avoid` / `protect`、`states` は scope=`state`、`contexts` は scope=`context-bound`。
残る層・動機の方向・scopeだけを各一要素で選び、条件（context-bound の場合）、異なる代替説明2件も各一要素にする。
根拠が回答 Event 一件と決まっているため、その実在参照はプログラムで結線し、選択の依頼を出さない。
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
拒否されたClaimを `claim_refs` に持つPattern草案は、自動で `rejected`、
`rejection_reason: superseded` として確認列から外す。以前のledgerを再開した場合にも適用する。
`yes` は根拠 Event の fingerprint、実在参照、同意、既存 validator を再確認してから
既存型の `hypothesis` として `entities/claims/` または `entities/patterns/` へ保存する。
確認は文の本人評価であり、supported や確度の昇格ではない。
これにより既存 exporter 自体や既存レコードの export 意味を変えず、新経路の未確認文を全 export から隔離する。

## 決定論と失敗条件

直前に本人へ聞いた欄（同じsubject・purpose）は次の選択で最後に回す。
そのほかの質問対象は現在の export と同じ `_signal_groups` で得た各欄の distinct Event 根拠数が少ない順。
同数時は `tensions → recurring_patterns → seeks → avoids → protects → states → contexts`。
全欄を候補にし、traits は複数 Context・時点の根拠を要求するため一件のヒアリング対象にしない。
body/emotion slot の空欄やアルファベット順では選ばない。

Issue #140 では、問いの inputs は最近の出来事一件の raw quote（既存の120字上限）、
対象説明一つ、プログラムが選んだ `question_type` 一つ。
推論はこの型の中身を保った言い換え一文だけを返し、型の選択や前提の補完をしない。
60字以内、一文、末尾 `?`/`？`、出来事と問いの `terms()` 集合に2文字以上の内容語候補が
完全一致で一つ以上共通することを検査する。漢字1文字や部分文字列の一致では合格しない。
禁止語はquestion-bank の forbidden_tokensに、SECTIONSの欄名とその英語構成語、
claim_layers、motivation_directions、`belief` を加える。
さらに直接識別パターン、テーマ・slug・依頼文への言及を機械検査する。
識別パターンと禁止語は NFKC 正規化して検査し、全角や日本語に連結されたアドレス・URL も拒否する。
制作のテーマや依頼文は inputs に渡さず、生の言葉だけの単純コピーも派生文として受け付けない。
短い本人の語を分析文の中で使うことは許す。本人確認後も raw_voice field 自体は export しない。
問いと一緒に示す効用文は欄ごとの固定表で、本人確認前の export や即時反映を約束しない。

同じ語・形の判定は意味推論を使わず、2文字以上の連続した漢字・カタカナ・英数字語と、
独立したひらがな語の完全一致を distinct Event ごとに数える。
漢字に隣接する送り仮名・助詞や、共通の機能語だけは数えない。
ひらがなだけの語は問いで引用符などの境界を保つ必要がある。
内容語候補を含むtrigger/actionの文字列が2件以上で一致する場合は、raw quoteの語の一致より優先する。
回答 Event を含む二件以上があるときだけ、共通の形と二件の raw quote を材料に
Pattern 文一つを依頼する。文には数えた共通の形を要求する。
これは保守的な文字列一致で、同義語や言い換えは検出しない。件数は推論への入口条件であり昇格基準ではない。
Pattern も別に本人確認されるまで export しない。
Pattern は生成元 Claim を参照し、その Claim が本人確認されていなければ保存を拒否する。
Claim を「いいえ」とした場合、そのClaimに依存するPatternの確認は出さない。

同意はすべての遷移で再確認する。根拠 Event が変われば推論・確認は失敗し、
失敗した Event 作成や確認時の書き込みは rollback する。失敗値の本文をエラーに出さない。
CLI の ERROR は固定コードのみ。状態が進まなければ wrapper は `next` で再取得する。
不正な entity YAML の本文や絶対 path は、CLI・要素 API のエラーにも出さない。

## 答えを持っている問いの型（Issue #140、初期案）

正本は `config/element-question-types.yaml`。`contract_version: element-question-types/v1`、
`revision: 1`、`owner_review: pending` は**オーナー確認前の提案**であり、確認済みとは扱わない。
以下の22型をオーナーが確認し、承認・追加・変更は config の PR で記録する。
変更時は revision を増やし、承認後だけ owner_review を approved にする。
実 profile での意味・答えやすさの評価はオーケストレーターと本人の確認に残す。

各欄の基本型は、既に本人が記録した一件を「この出来事のとき」として範囲を固定する。
制作の有無、行き詰まり、緊張、拒否経験を新たに仮定しない。
「この出来事のとき、」に続く問いは次のとおり。

| 欄 | 型1 | 型2 | 型3 |
|---|---|---|---|
| tensions | まず考えたことは何ですか？ | 自分の選び方はどんな選び方でしたか？ | 自分の気持ちはどんな気持ちでしたか？ |
| recurring_patterns | 一番印象に残った場面はどんな場面でしたか？ | 最初にしたことは何ですか？ | 自分の進め方はどんな進め方でしたか？ |
| seeks | 自分が望んでいたことは何ですか？ | 一番心が向いたものは何ですか？ | 次にしたかったことは何ですか？ |
| avoids | 一番気になったことは何ですか？ | 周りとの距離の取り方はどんな取り方でしたか？ | 自分のペースはどんなペースでしたか？ |
| protects | 自分が大事にしていたことは何ですか？ | 自分を支えていたものは何ですか？ | 自分らしいと思う部分はどんな部分でしたか？ |
| states | 自分の気持ちはどんな気持ちでしたか？ | 一番印象に残ったことは何ですか？ | 自分の反応はどんな反応でしたか？ |
| contexts | 周りの様子はどんな様子でしたか？ | 自分がいた場所はどんな場所でしたか？ | 自分が目を向けていたものは何ですか？ |

contexts の4番目の題材は「小さい頃のこの出来事のとき、今でも印象に残る場面はどんな場面でしたか？」。
本人の raw quote に「小さい頃」がある場合だけ候補に入り、config 上は最初に置く。
小さい頃の記憶があると推測したり、最近の記録を幼少期の記録へ読み替えたりしない。
覚えていない、答えたくない場合の既存 skip は残す。答えを持っているかの意味判断は文字列検査で
保証できないため、初期案の確認では「望み」「支え」なども含め、本人が答えやすいかを評価する。
未知・未観測を「ない」へ変換せず、回答から緊張やPatternがあると決めつけない。

選択と検査は以下の決定論で行う。

1. 欄は上記の根拠数・直前欄の回避・固定の同数優先順で決める。
2. 選んだ欄の型から `requires_event_text` の全語が raw quote にある型と無条件の型を候補にする。
   同じsubject・purpose・欄で直前に本人へ出した型を避け、config順で最初の型を選ぶ。
   3回目以降も直前だけを避ける。無条件の型を最低2個要求する。
3. 選んだ型のID・版・問い・`scope_terms` をprofile内runへ保存し、requestのinputsへ渡す。
   同じ問いの再試行・再開では変更しない。失敗は履歴を更新せず、HEARINGになった時だけ履歴に残す。
4. 既存の60字・一文・本人の内容語・privacy検査に加え、NFKC正規化して
   `no_existence_question`（ありましたか／ありますか／ありませんか等）、
   `no_unbounded_words`（ほか／他にも／何か／いつか／どこか等）、
   `contains_scope_terms`（選択型の全前提語）、`asks_content`（何／どんな／どの）を検査する。
   前提語はそのまま残す。自然さを理由に消す・別の時期に置き換えることも拒否する。
5. 落ちたら同じ型・同じ要素だけを再試行し、5回でBLOCKED。空の問いや別型へfallbackしない。
   不正なconfigも固定コード `QUESTION_TYPES_INVALID` で停止し、部分runを保存しない。

型の追加はentity型・closed vocabularyの追加ではなく質問configだけの変更。
親のelement-request/answer envelope、HEARING中継、本人確認、同意、export境界は既存のまま。
旧版の未受理のhearing-questionは `next` で型を結び直し、attemptを維持する。
旧依頼に直接answerした場合は `QUESTION_TYPE_REQUIRED` で進めず、nextで再取得する。
既に受理された問い・草案は過去の記録として保持する。

合成profileでのCLI例（推論役に渡した一文を検査したもので、モデルの実応答評価ではない）:

- tensions: 「予定」のこの出来事のとき、まず考えたことは何ですか？
- recurring_patterns: 「予定」のこの出来事のとき、一番印象に残った場面はどんな場面でしたか？
- contexts（幼少期の記録）: 小さい頃の「窓辺」のこの出来事のとき、今でも印象に残る場面はどんな場面でしたか？

## 検証と残る human gate

合成 temporary profile の test は質問生成、要素再試行、回答 Event、草案、次 run の
本人確認、signal export、Pattern の二件条件、同意撤回、改変、privacy guard、symlink 拒否を検証する。
実 profile はテストに使用しない。
Issue #138 の実 run でマサさんが「意味のある質問」と認める条件は、実 profile に触れないという
オーケストレーター指示により実装エージェントでは未実施。オーケストレーターの pin/wrapper 更新後の
本人確認で完了を判断する。
