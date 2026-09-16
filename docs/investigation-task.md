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
python3 tools/agent_runtime.py tools/new_entity.py event <slug> --subject subject/<id>
```

1出来事に限定し、trigger、observed facts、raw voice、action、immediate outcome、source refsを分ける。
質問していない、取得していないslotは`null`で保存する。確認して該当なしの場合だけ`[]`、評価を試みて判定不能の場合だけ`unknown`とし、3状態を相互変換しない。

## 3. Claimを作る

観測と推論を分離し、supporting evidence、counterevidence、代替説明2件、confidenceを入れる。動機を断定せずhypothesisから始める。

## 4. Pattern候補

複数Event、複数時点を確認する。件数だけでsupportedにしない。単発ならClaimのまま終える。

## 5. 検証

```bash
python3 tools/agent_runtime.py tools/build_graph.py --check
python3 tools/agent_runtime.py -m unittest discover -s tests -p "test_*.py"
python3 tools/agent_runtime.py tools/build_graph.py
python3 tools/agent_runtime.py tools/audit.py --subject subject/<id>
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

## 8. 次に何を調べるか（gap駆動の育成queue、Issue #108）

対象・task内容を毎回考えるのではなく、`tools/growth_tasks.py`が生成するlocal queueから選ぶ。

```bash
python3 tools/agent_runtime.py tools/growth_tasks.py generate --profile-root <profile-root>
python3 tools/agent_runtime.py tools/growth_tasks.py next --profile-root <profile-root> --json
python3 tools/agent_runtime.py tools/growth_tasks.py claim <task-id> --actor <name> --profile-root <profile-root> --expected-queue-sha256 <sha>
python3 tools/agent_runtime.py tools/growth_tasks.py complete <task-id> --profile-root <profile-root> --expected-queue-sha256 <sha>
```

優先順位は miss（consuming runが得られなかった signal） → audit 指摘 → coverage の未観測 → milestone 未達の順。`acquire-event` だけが本人への1問（`question`フィールド、schema語彙なし）を必要とし、`derive-claim`・`search-counterevidence`・`refresh-claim`は既存entityだけで完了できる。

queueとmissの記録先は外部profile root（`growth/queue.yaml`、`data/misses.jsonl`）だけであり、公開repoのIssueには置かない。CASはqueueファイルのsha256で行い、gitを要求しない。育成sessionは制作runと独立していつでも起動できるが、同じprofile rootへ同時に書き込まない（`generate`はin-progress taskがあれば`QUEUE_BUSY`で拒否する）。
