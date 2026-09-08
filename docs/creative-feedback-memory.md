# 本人の制作選択と派生知識

AAK-05 / SM-036の追加契約です。要件の正本はIssue #93が固定する
AAK-SPEC/PLAN `b0e7c7f8d0a1f756fa708deef4fb380a62e45e0d`。
Source→Event→Claim→Patternの既存型・閾値は維持し、制作時の文脈付き選択を
`creative-feedback/v1`として別の派生collectionに保存します。

## 保存境界

`self-model-profile/v1`は引き続きexternal-localです。profile.yaml、entity、rawを
Gitストアへコピーしません。`self-model-knowledge/v1`は本人所有の外部bare Git
repositoryです。protocol、raw profile、互いのworktreeと重なる保存先を拒否します。
新規storeのidentityにはcreator、subject、profile_id、collection、origin_instanceを
明示します。既存storeのidentityを変更するCLIはありません。forkしたAのstoreを
Bとして開くとCREATOR_SCOPE_MISMATCHです。Bの本人履歴は別storeとして初期化します。
remoteは不要で、すべての操作はローカルだけです。

正本は`knowledge/store.json`、`knowledge/records/<id>/<revision>.json`、
`knowledge/receipts/<operation-id>/1.json`のみです。記録schemaは
`schemas/creative-feedback-v1.schema.json`、実行validatorは
`tools/creative_feedback.py:validate_record`です。`derived-index.json`はGit外の
再生成cacheであり、export時の同意・source再検証を代替しません。

## 実行

初回setupで決定した外部pathと本人identityを、利用エージェントのローカル設定から
明示的に渡します。Masa・cwd・別profileへのfallbackはありません。

```bash
python3 tools/agent_runtime.py tools/creative_feedback.py init \
  --store-root /absolute/path/to/derived-memory.git \
  --profile-root /absolute/path/to/raw-profile \
  --creator creator-a --subject subject/example --collection memory-a --instance instance-a
```

以後の全commandも同じ`--store-root --profile-root --creator --subject --collection`
を必須とします。

| command | 追加入力 | 意味 |
|---|---|---|
| prepare / validate | --record <external-json> | schema、本人、source metadata hash、derive同意を検証。正本変更なし |
| commit | --record、--expected-parent <SHA>、--operation-id <opaque-id> | isolated indexでtree作成、update-ref CAS、receipt。remote副作用なし |
| index | 任意 --snapshot <SHA> | 最新revisionと現在のsource/同意から索引を再生成 |
| retrieve | 任意 --snapshot、--query | 選択collectionのみ検索。理由、状態、revision、hash、snapshotを返す |
| invalidate | なし | 現在のsource訂正・同意取消に基づき索引を再計算。正本削除なし |
| export-signals | 任意 --snapshot | observed本人選択だけを既存research-signal-export/v1へ投影 |

既存の正規export入口からも利用できます。

```bash
python3 tools/agent_runtime.py tools/export_signals.py \
  --profile-root /absolute/path/to/raw-profile \
  --knowledge-store-root /absolute/path/to/derived-memory.git \
  --creator creator-a --collection memory-a --subject subject/example \
  --purpose artistic-research --operation export-signals
```

`--knowledge-store-root`省略時の既存profile exportは互換のままです。
source_commit/各signal.commitは実行codeのSHAです。知識SHAはsource_locator内の
`self-model-knowledge://<collection>/<knowledge-commit>/knowledge/records/...`に
固定します。取り込んだ知識が判断を変えたかどうかは下流のreuse-traceで記録します。
検索に載っただけでは再利用実証になりません。

## 記録と失効

observedはhumanの明示選択（adopt/reject/revise/evaluate）を必要とします。
inferred/proposed/simulated/unknownは保存・検索しても本人signalにしません。
無反応はunknown・choice=null・confidence=unknownです。選択から恒常traitを
生成せず、context、counterevidence、2つ以上のalternative_explanations、既存の
confidence語彙を保持します。raw_voice等の未知field、既知raw引用、credential
pattern、未知major、traversal、symlink、scope不一致を拒否します。
自由記述の意味まで自動でprivacyを証明するものではなく、入力は派生要約だけとし、
原文はprofile側に残します。Source metadataのhashは由来の変更検出用で、原文ではありません。

訂正は同じIDの連続revisionとsupersedesで追加します。既存revisionの異内容は
REVISION_CONFLICT、同operation異内容はOPERATION_CONFLICT。同一内容の再実行は
NO_CHANGE / ALREADY_APPLIEDです。Source訂正・同意取消・期限・目的変更は毎回
profileから確認するため、古いsnapshotやcacheで失効を回避できません。
withdrawalは旧内容を変更しないrevoked revisionとして保持できます。

receiptのtarget_commitは自身を含む実Git commitから復元します（commit自身のSHAを
commit内へ循環保存しない）。保存後のindex失敗はINDEX_PENDINGであり、commitを
保持してindexだけ再実行できます。CAS失敗はPARENT_CONFLICTで正本refを変更しません。
Git transaction前の中断は正本不変、ref更新後の中断は永続receiptから再開します。

## 受入

`tests.test_creative_feedback_memory`が実bare Git・別process・A/B合成profileで
AC1〜5、raw拒否、失効、fork、replay、CAS、index復旧を観測します。
実Masa資料・実n=1移設#82・public projection・remote writeは実行しません。
AAK-02の実エージェント6runは別の統合受入です。

公開するreceiptは親のclosed `knowledge-write-receipt/v1`と一致します。
保存時のhash/payload/codeメタデータは別の`creative-feedback-operation/v1`として
owner Git内に保持し、共通receiptへ追加fieldを混ぜません。retrieveの`artifact`は
closed `artifact-record/v1`です。knowledge snapshotは検索結果の外側に保持し、
producer.code_commitは記録を保存した時点のoperationから復元します。
