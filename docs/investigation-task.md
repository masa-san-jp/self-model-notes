# 1件の調査・記録タスク

実行系エージェントは1回に1 Subjectの1 Eventまたは1 Claimだけを追加・改訂する。

## 0. 開始条件

- Subject IDがある。
- Sourceの同意範囲を読める。
- 原典locatorへ到達できる。
- Issueに目的と対象が書かれている。

満たさなければ推測せず停止する。

## 1. Sourceを確認

同意、目的、許可操作、撤回、有効期限を確認する。原文をGitへ全文複製しない。

## 2. Eventを作る

```bash
python3 tools/new_entity.py event <slug> --subject subject/<id>
```

1出来事に限定し、trigger、observed facts、raw voice、action、immediate outcome、source refsを分ける。

## 3. Claimを作る

観測と推論を分離し、supporting evidence、counterevidence、代替説明2件、confidenceを入れる。動機を断定せずhypothesisから始める。

## 4. Pattern候補

複数Event、複数時点を確認する。件数だけでsupportedにしない。単発ならClaimのまま終える。

## 5. 検証

```bash
python3 tools/build_graph.py --check
python3 -m unittest discover -s tests -p "test_*.py"
python3 tools/build_graph.py
python3 tools/audit.py --subject subject/<id>
```

運用時の復旧・並行作業・生成物の扱いは[`docs/operations.md`](operations.md)に従う。生成物を手編集して検証を通すことはしない。

## 6. 人間による確認点

- raw voiceの引用範囲が同意内か。
- 代替説明が実質的に異なるか。
- 本人固有の事実を一般schemaへ追加していないか。
- Unknownを無理に埋めていないか。
- auditが示す次の観測が過度に侵襲的でないか。

## 7. 完了

変更entity、検証結果、同意判定、未確認事項をPRへ記載する。新たなschema判断が必要なら実装せずIssueを作る。
