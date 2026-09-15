---
id: event/identity-mismatch-selfreport-20260915
type: event
subject: subject/masa
time:
  observed_at: 2026-09-15
  precision: day
context:
  domains: [work, creative-practice]
state:
  fatigue: null
  stress: null
trigger: "聞き取りの質問「自分らしくないと感じた場面は?」"
observed_facts:
  - "本人が、得意でやりたいことと仕事で求められることが一致するとは限らないため色々な場面で自分らしくないと感じると述べた"
  - "本人が、当リポジトリ群を作る活動は楽しいと述べた"
  - "本人が、実際にはほとんどの時間を仕事に費やしており苦手なことも多いと述べた"
raw_voice:
  - text: "自分らしくないは色々な場面で感じるよ。本当に得意でやりたいことと、自分が求められることが一致するとは限らないしね。"
    source_ref: source/conversation-20260915
  - text: "今はこのリポジトリ群を作っているけど、こうゆう活動は楽しい。実際はほとんどの時間を仕事に費やしていて、苦手なことも大いにある。"
    source_ref: source/conversation-20260915
appraisal: null
emotion: null
body: null
cognition: null
action: null
immediate_outcome: null
delayed_outcome: null
source_refs: [source/conversation-20260915]
created: 2026-09-15
updated: 2026-09-15
---

# 「自分らしくない」感覚についての自己申告（2026-09-15）

具体的な1つの場面ではなく、本人が一般的な傾向として述べた内容。`observed_at`は聞き取り時点。

`context.domains`は`work`と`creative-practice`とした——本人が「仕事」と「このリポジトリ群を
作る活動」の両方を名指しで対比しており、直接の発言に基づく。`social`・`uncertainty`・`control`
は尋ねていないため省略している。

「求められることと得意なことの不一致」というテーマ自体は、単発の自己評価であり、まだEventの
根拠のみでClaim化できる状態ではない（代替説明・反証の検討が必要）。
