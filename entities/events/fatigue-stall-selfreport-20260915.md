---
id: event/fatigue-stall-selfreport-20260915
type: event
subject: subject/masa
time:
  observed_at: 2026-09-15
  precision: day
context: {}
state:
  fatigue: null
  stress: null
trigger: "聞き取りの質問「最近、理由もなく手が止まった瞬間はある?」"
observed_facts:
  - "本人が、疲れが溜まると手が止まることがしょっちゅうあると述べた"
raw_voice:
  - text: "手が止まるのはしょっちゅうあるよ。疲れが溜まってる。"
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

# 手が止まる傾向についての自己申告（2026-09-15）

具体的な1回分の日時・状況を尋ねる前に、本人が一般的な傾向として述べた内容。`observed_at`は
聞き取り時点であり、実際に手が止まった時点ではない——いつ起きたかは特定していない。

`context`を空にしているのは、場面（domain/social）を尋ねていないため。`state.fatigue`も
`null`のままにしている。「疲れている」という自己申告そのものは`observed_facts`と`raw_voice`
に事実として残し、構造化された状態slotへは変換していない——単発の申告から状態値を確定させない
ため。

同じ傾向を示す別の具体的な出来事が今後追加されれば、Pattern候補として扱えるようになる。
