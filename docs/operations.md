# Operator runbook

この文書は、匿名化されたSelf Model KBを安全に更新・検証・復旧するための最小手順です。原則は「正本は`entities/`、生成物はコマンドで再生成、同意はexport時に再検証」です。

## 通常の実行

1. 作業開始前にbranch、対象Issue、許可パス、未コミット差分を確認する。

   ```bash
   git status --short --branch
   ```

2. 新しいentityはtemplateから作成し、frontmatterを編集する。

   ```bash
   python3 tools/new_entity.py event <slug> --subject subject/<id>
   ```

3. loader、validator、graph、全テスト、soft audit、audit生成物のstalenessを順に確認する。

   ```bash
   python3 tools/build_graph.py --check
   python3 -m unittest discover -s tests -p "test_*.py"
   python3 tools/audit.py --dry-run
   python3 tools/audit.py --check
   python3 tools/bundle.py --all --check
   ```

4. entityを追加・改訂した場合だけ、生成物を再生成して差分を確認する。

   ```bash
   python3 tools/build_graph.py
   python3 tools/build_self_model.py --subject subject/<id>
   python3 tools/bundle.py --subject subject/<id>
   python3 tools/bundle.py --all --check
   ```

   entityを先にcommitし、JSON/Markdownを再生成して差分と機微情報を確認し、artifactを別commitする。`source_commit`は対象Subjectのcanonical entityだけから算出され、artifact commit自身は参照しない。

5. exportは目的と操作を明示する。Sourceが1件でも同意不備なら全体がdenyされる。

   ```bash
   python3 tools/export_signals.py \
     --subject subject/<id> \
     --purpose artistic-research \
     --operation export-signals
   ```

## 失敗時の復旧

- `build_graph.py --check`がstaleを報告したら、`data/`や`overviews/`を手編集せず、正本entityを確認して`python3 tools/build_graph.py`を再実行する。
- `audit.py --check`がstaleを報告したら、`data/audit.json`を手編集せず、正本entityを確認して`python3 tools/audit.py`を再実行する。`--check`自体はファイルを書き換えない。
- Self Modelやbundleが古い場合も、生成JSON/Markdownを直接直さず、対象Subjectのbuildコマンドを再実行する。
- `tools/build_self_model.py --check` はJSONだけ、`tools/bundle.py --subject ... --check` はJSONとMarkdown、`tools/bundle.py --all --check` は全active SubjectをID順に検証する。stale/missing時はrepair commandを表示するが、本文やraw voiceは表示しない。
- canonical entityに未commit差分がある場合はsnapshot生成・checkを行わず、対象pathだけを報告してentity commitを要求する。artifactだけの未commit差分はcheck対象として許可する。
- exportがdenyされたら、deny JSONの`source`と`rule`だけを確認する。同意条件を迂回したり、raw voiceを手でコピーしたりしない。
- テスト失敗時は失敗ログと差分を保持し、skip、削除、生成物の手編集で緑にしない。
- 不要な削除、reset、履歴改変は行わない。復旧不能な場合はIssueへ停止理由と観測事実を記録する。

## 並行作業と衝突回避

- 1 task、1 agent、1 branch、1 PRとする。別agentのbranchや未完成差分を修正・restore・stashしない。
- `docs/schema.md`、`tools/kb.py`、`execution/tasks.yaml`は共有lock対象。変更が必要なら現在taskへ混ぜず、ownerへ確認する。
- 同じallowed pathのtaskは並列に開始しない。別branchを作る前に`git status`と`git log`でbaseを確認する。
- 自分の変更外で衝突やテスト失敗が起きたら、対象ファイルと再現コマンドを報告して停止する。
- commit前に変更path、diff、機微情報、生成物のstalenessを確認する。pushとmergeはCI成功後に行う。

## Privacy and consent

- fixtureには直接識別情報、秘密、原文全文、実在人物固有の値を入れない。
- exportは`obtained`、`revoked_at`、purpose、operation、expiryを全Sourceについて再確認する。
- raw voice本文は既定でexportしない。参照が必要でも、同意範囲と目的を先に確認する。
