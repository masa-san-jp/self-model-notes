# Privacy and Consent

## 原則

- 目的別同意。分析、研究、作品化、二次利用を一括同意にしない。
- データ最小化。Gitにはlocatorと必要最小引用だけを置く。
- 疑似匿名を既定とする。
- 撤回可能性を持つ。
- export時にSourceの許可操作と目的を再検証する。
- Unknownと回答拒否を推測で埋めない。

## profile storage boundary

canonicalなreal profile recordはprotocol repository外のprofile rootに保存し、`profile.yaml`の`storage_scope: external-local`でその境界を示す。profile.yamlは保存場所の契約であり、Sourceのconsentを与えるものではない。`entities/`のREADMEやsynthetic fixtureを実データの代用にしない。

実データCLIは明示した`--profile-root`だけを読み書きし、repositoryや環境変数からprofileを推測しない。migrationのplanはmetadataだけを表示し、applyはsourceを削除・移動・上書きしない。実n=1のapplyは、Issue #82の人間承認、目的、保存先、retentionが確認されるまで実行しない。

## export判定

全根拠Sourceについて以下を満たす場合のみ許可する。

1. `consent.obtained == true`
2. `revoked_at == null`
3. 目的が`purposes`に含まれる
4. 操作が`allowed_operations`に含まれる
5. `expires_at`が未到来またはnull

1件でも不明ならdeny-by-default。

## 禁止用途

- 心理診断、精神疾患推定
- 採用、解雇、配置等の重大判断を単独で行うこと
- 本人に知らせない機微な内面収集
- 同意外の作品化・公開・第三者提供
- 再識別を目的とした結合

## Incident対応

機微情報をcommitした場合は通常の削除commitだけで済ませず、repository ownerへ即時報告し、履歴除去・credential rotation・利用停止を判断する。実行エージェントは独断で履歴改変しない。
