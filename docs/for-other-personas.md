# 利用者・他エージェント向け入口

## 読む

```bash
cat overviews/coverage.md
python3 tools/agent_runtime.py tools/bundle.py --subject subject/<id>
python3 tools/agent_runtime.py tools/bundle.py --all --check
python3 tools/agent_runtime.py tools/export_signals.py --subject subject/<id> --purpose artistic-research --operation export-signals
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
