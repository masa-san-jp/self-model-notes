---
id: event/anger-trigger-20260913
type: event
subject: subject/masa
time:
  observed_at: 2026-09-13
  precision: day
context:
  domains: [work]
  social: [small-group]
  uncertainty: unknown
  control: unknown
state:
  fatigue: null
  stress: null
trigger: "懇親会で同僚らと「何に対して怒りを感じるか」という話題になった"
observed_facts:
  - "本人が「友人を悪く言われた時」に怒りを感じると答えた"
  - "同僚の一人が中立的な相槌を返した"
raw_voice:
  - text: "友人を悪く言われた時"
    source_ref: source/conversation-20260915
appraisal: null
emotion: null
body: null
cognition: null
action:
  - "怒りを感じる対象を明言した"
immediate_outcome:
  - "相手が中立的な相槌を返した"
delayed_outcome: null
source_refs: [source/conversation-20260915]
created: 2026-09-15
updated: 2026-09-15
---

# 怒りのトリガーについての発言（懇親会、2026-09-13）

同僚との懇親会（8人程度）で「何に対して怒りを感じるか」という話題になり、本人は「友人を悪く
言われた時」と答えた。

`context.domains`を`work`としたのは、参加者が同僚であるという本人の発言に基づく直接的な事実で
あり、推測ではない。`social`は8人程度という規模から`small-group`とした——語彙上`small-group`と
`large-group`の境界は明示されていないため、この記録内での判断であることを明記する。

`uncertainty`と`control`は本人に評価を尋ねていないため`unknown`のまま残す。`appraisal`以降
（感情・身体感覚・認知）も未取得であり、無いことを確認したわけではない。

同僚の返答「へぇ、そうゆう感じなんですね」は本人のraw_voiceではないため、`immediate_outcome`に
中立的な相槌として要約し、逐語引用は残していない。
