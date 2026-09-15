# What to do before you measure written context

[Documentation](README.md) / Research / Practices

This is a research record. For current setup and commands, start with
[Evaluate your data](EVALUATE.md). Costs and timings below belong to the cited runs.

## On this page

- [1. Name the datasets. Do not let the tool choose them.](#1-name-the-datasets-do-not-let-the-tool-choose-them)
- [2. Mine the data somebody has already written about.](#2-mine-the-data-somebody-has-already-written-about)
- [3. Check the set can resolve the effect before paying for the round.](#3-check-the-set-can-resolve-the-effect-before-paying-for-the-round)
- [4. Report three separate facts, and never dress one as another.](#4-report-three-separate-facts-and-never-dress-one-as-another)
- [5. Write rules with a subject.](#5-write-rules-with-a-subject)
- [6. Say which table a rule applies to.](#6-say-which-table-a-rule-applies-to)
- [7. A convention counted from query history is evidence, not a definition.](#7-a-convention-counted-from-query-history-is-evidence-not-a-definition)
- [8. Do not write a rule about a question whose hard part is a value.](#8-do-not-write-a-rule-about-a-question-whose-hard-part-is-a-value)
- [9. Have somebody confirm the answer keys.](#9-have-somebody-confirm-the-answer-keys)
- [10. Do not swap the model hoping it will fix the floor.](#10-do-not-swap-the-model-hoping-it-will-fix-the-floor)
- [11. The question drafter is probably your biggest loss. Count it.](#11-the-question-drafter-is-probably-your-biggest-loss-count-it)
- [12. Read the ledger, not the cost sheets](#12-read-the-ledger-not-the-cost-sheets)
- [13. An example handed to the wrong question is a distractor, measured](#13-an-example-handed-to-the-wrong-question-is-a-distractor-measured)
- [What is still unproven, stated plainly](#what-is-still-unproven-stated-plainly)
- [14. A semantic layer is not written business context. Measure its description share before trusting it.](#14-a-semantic-layer-is-not-written-business-context-measure-its-description-share-before-trusting-it)
- [15. Do not narrow reference text to the one line you think applies](#15-do-not-narrow-reference-text-to-the-one-line-you-think-applies)
- [16. A per-question snippet can never be cached. A shared block can.](#16-a-per-question-snippet-can-never-be-cached-a-shared-block-can)

Every practice here has a measurement behind it and a command that checks it.
A practice without its measurement is an opinion, and this file is not for
opinions.

Written 2026-09-12 from five paid rounds on one enterprise warehouse, four of
which produced nothing, plus a measured ladder on a public enterprise
benchmark (`docs/CASE_STUDY.md`).

Read the first four in order. They are the ones that cost real money to learn.

---

## 1. Name the datasets. Do not let the tool choose them.

**The mistake.** `mine` spreads the questions it takes round-robin across
tables, so the busiest table cannot fill the sample by itself. That default is
right for **looking around an estate** and wrong for **building a set that
proves something**. On one estate it produced three rounds in a row that could
not resolve anything, and the tool reported them as results about the context.

**Measured.** `scripts/estate_shape.py` on two gold sets:

| | questions | tables | questions per table | reserved half reachable |
|---|---|---|---|---|
| a public dataset | 104 | 9 | 11.56 | 100% |
| the estate, spread | 36 | 59 | 0.61 | 60% |
| the estate, `--no-spread` on two datasets | 43 | 13 | 4.78 | 100% |

"Reachable" means a reserved question shares a table with the working half, so
a rule written from the working half could possibly help it. Below 50% the
round measures the split, not the context.

**Do this.**

```
contextlift mine --datasets A,B --no-spread ...
uv run python scripts/estate_shape.py golds.csv     # free, no cloud call
```

Stop if questions per table is under 2.00 or reachable is under 50%. The
remedy is a different split or more questions, never better writing.

---

## 2. Mine the data somebody has already written about.

**The mistake.** The whole point is to measure **written** context. On one
estate the two datasets chosen by traffic had no documentation at all, so what
got measured was prose a model had mined out of query logs — while 300 KB of
human-written documentation for 27 other datasets sat unused in a sibling
repository.

**Measured.** `scripts/doc_coverage.py`:

```
  candidates: 758 rows, 72 read a documented dataset, 24 of those pass the gate
  golds     :  43 rows,  6 read a documented dataset
```

Six of 43. Five paid rounds measured the wrong context.

**Do this.** Before mining, answer one question: *where is the writing that is
supposed to help?* Then mine the datasets that writing covers.

```
uv run python scripts/doc_coverage.py candidates.csv golds.csv
```

A second thing that check finds, and it is worth taking to the customer: the
same estate documented 12 data products that had **no analytical query in 180
days**, while its five most-queried data products had no documentation. Both
halves of that mismatch are actionable whatever this framework measures.

---

## 3. Check the set can resolve the effect before paying for the round.

**The mistake.** Five paid rounds reported `NOT PROVEN` honestly and none of
them could have reported anything else.

**The arithmetic, and it needs no assumptions.** A round is scored on the
questions that **disagree** between the two arms. A question right in both arms
carries no information and neither does one wrong in both. With `d`
disagreements all falling one way the exact two-sided p is `2 * (1/2)^d`:

| disagreements, unanimous | p |
|---|---|
| 4 | 0.1250 |
| 5 | 0.0625 |
| **6** | **0.0312** |

**Six unanimous disagreements is the cheapest significant result that
exists.** The round that prompted this produced 1 fixed and 3 broke — four
disagreements, `p = 0.6250`.

**Measured, against the effects this framework has itself produced on an
enterprise warehouse:**

| rung, on a 596-question reserved half | lift | power at n=29 |
|---|---|---|
| iterated glossary | +1.9 | 0.0% |
| stronger model | +2.4 | 0.1% |
| oracle per-question hints | +5.2 | 1.0% |
| leak-checked worked-example bank | +6.9 | **2.5%** |
| the whole ladder | +16.4 | 30.9% |

Had the context been as good as the example bank that scored `p < 0.0001`, a
29-question reserved half had a 2.5% chance of saying so.

**Do this.** Nothing, it is automatic — `contextlift round` refuses now. To see
the numbers for a set you are planning:

```
uv run python scripts/resolvable_lift.py
contextlift round --target-lift 50 ...    # if you really expect 50 points
```

`--force` runs anyway and prints `FORCED` on the page.

---

## 4. Report three separate facts, and never dress one as another.

**Measured on the same round**, which is why they have to be separate:

```
  verdict  NOT PROVEN       an estimate about questions nobody has seen
  safety   REGRESSION       a COUNT: 1 fixed, 3 broke, of 29
  cost     +30.4% bytes     a COUNT: 169.0 GB -> 220.4 GB on 36 paired questions
```

The verdict needs statistics because it is a claim about the future. The
other two need none, because they count what happened. A small gold set can
give you a trustworthy safety and cost result on the day it cannot give you a
lift.

And read the cost line carefully, because ours disagreed with itself: more
questions got **cheaper** (17) than dearer (13), sign test `p = 0.5847`, while
the total went up 30% on a handful of queries. The biggest was one rule line
sending the agent to a different table:

```
  m711   control 0.00 GB -> treatment 30.17 GB
```

That one rule cost 30 GB and one correct answer.

---

## 5. Write rules with a subject.

**Measured.** Five of ten rule lines under test read like this:

```
Compute it as `sum(count_of_eligible_days) - sum(count_of_oos_days)`.
```

Compute **what**? The file that generated them says so itself: naming the
metric *"is a person's job"*. And 8 of 10 bare column names in that file were
on no table in the arm at all.

An instruction an agent cannot attach to anything is not context. Either a
person names it or it does not ship.

---

## 6. Say which table a rule applies to.

**Mechanism observed once, benefit unmeasured.** A fiscal-calendar rule, true
of the estate, moved the agent off a table that already carried
`fiscal_period` and onto a date dimension it then joined wrong. Scoped to
`on <that one table>` it could not have. Cost: 30 GB and one correct answer
(`m711`, above).

This one is a hypothesis with a mechanism, not a measured practice. It is
marked as such deliberately.

---

## 7. A convention counted from query history is evidence, not a definition.

A declared definition outranks a counted one. The counted ones carry the
failures in practices 5 and 6.

---

## 8. Do not write a rule about a question whose hard part is a value.

**Measured, and narrower than it first looked.** Of 43 golds, 23 pin an ISO
date in the answer key. But **20 of those 23 also state the date in the
question**, and that is fine — the agent reads it from the question like any
other filter. Only 3 are genuinely unanswerable, and all 3 are `9999-12-31`
sentinels meaning "no upper bound".

The distinction matters and it is easy to get wrong: a date **pinned in the
SQL** is not a problem, a date **absent from the question** is fatal. Counting
the first as the second overstates the ceiling by 7x.

```
uv run python scripts/regate_literals.py candidates.csv
```

---

## 9. Have somebody confirm the answer keys.

Public benchmarks with **paid annotators** still carry annotation errors —
52.8% of BIRD Mini-Dev's pairs by one published audit, a figure that includes
ambiguous questions and missing specifications as well as wrong SQL. A set
drafted by a model and reviewed by a model has an unknown error rate.

Budget about half a day of one analyst. Keep that ask separate from the ask
for more questions; merging the two produced a wrong conclusion here.

---

## 10. Do not swap the model hoping it will fix the floor.

**Measured twice.** This framework's own ladder puts the stronger model at
**+2.4 points** (5.7% to 8.1%, `p = 0.004`) — the smallest meaningful rung.
And on private enterprise warehouses, two frontier models from two vendors
inside a purpose-built agentic framework land 0.6 points apart:

```
  ReFoRCE + GPT-5.2            10.8%
  ReFoRCE + Claude-4.5-Sonnet  11.4%
```

Accuracy near 10% on a real enterprise warehouse is the published level, not a
broken setup. The model is not the bottleneck and the context rungs are worth
more.

---

---

## 11. The question drafter is probably your biggest loss. Count it.

**Measured 2026-09-12, on two independent pulls from the same estate.** Both
were reviewed by `gemini-2.5-pro`, which did not write the questions.

```
  deep pull (undocumented datasets): 177 candidates, kept 45 (25%)
        81  does_not_match      <- 46% of the pool
        47  duplicate
         3  dashboard_shaped
         1  dropdown_or_metadata

  scoped pull (documented datasets): 195 candidates, kept 49 (25%)
        81  duplicate
        59  does_not_match      <- 30% of the pool
         5  dashboard_shaped
         1  not_a_question
```

**140 of 372 candidates, 38%, were rejected because the drafted English did
not ask what the SQL answers.** Not one of those was a data problem. Every
one had SQL that ran on BigQuery, returned rows, was not truncated, was
inside budget, and passed all 16 gate checks. The money was already spent.
Only the sentence was wrong.

**What the drafter was doing wrong**, from a real row:

```
  gemini-2.5-flash wrote : "The material, plant, from date, to date, cost
                            estimate number, and total value per unit for
                            materials with a standard cost of 0"
  the SQL's own comment  : -- Pull the actual list of the 108 for follow-up
```

It listed the `SELECT` columns and called that a question. And the analyst's
own comment, which says what the query is *for*, was ignored — `strip_comments`
throws comments away everywhere else in the pipeline, and the drafter never
sees them.

**Why this is the practice that pays best.** Every other route to a bigger
gold set costs money or asks a customer for something: mine more shapes, raise
the byte ceiling, clear a security perimeter, borrow an analyst. Fixing the
drafter costs nothing — the SQL is already on disk — and it is worth more than
all of them.

**Do this.**

```
uv run python scripts/redraft_worksheet.py review.csv worksheet.csv
```

It pulls the `does_not_match` rows into a sheet for re-drafting, and puts the
analyst's SQL comments at the top of every row. 20 of the 59 scoped rows carry
one.

**And the four rules a question has to satisfy at once, which is why this is
hard rather than sloppy:**

1. **Identify the same answer.** Same rows, same grouping, same aggregation,
   same limit. If the SQL takes a top 10, the question must say top 10.
2. **State every parameter.** A date, a month, a specific id — anything a
   person chose arbitrarily. No document can ever supply it, so a question
   that omits it makes the gold unanswerable in both arms.
3. **Do NOT state business codes.** `status != 'CANCELLED'` is what the
   written context is supposed to teach. Spell it out in the question and you
   hand the answer to the arm that has no context, and measure nothing.
4. **Use business words, not column names.** "How much did we sell in each
   order state", never "total of `order_item_sale_price` grouped by
   `order_status`". The second measures vocabulary overlap.

Rules 2 and 3 pull against each other, and telling them apart is the whole
craft. The test: *could a person who knows this business, but has never seen
this table, supply the value?* If yes, leave it out. If it is arbitrary, state
it.

## 12. Read the ledger, not the cost sheets

**Measured 2026-09-12.** The per-command `spent:` lines for one day of work
added up to about **$74**. The project's own billing ledger, filtered to one
user so a shared project is not misattributed, said **$149.61 across 2,686
jobs**.

```
contextlift log ...            $0.0987     printed by the command
mine                       $35.1728     printed by the command
review + re-review          $0.4714     printed by the command
gate, two passes            $1.5111     printed by the command
round                       ~$112       NOT printed by anything
                           --------
per-command total            ~$74
the ledger                 $149.61
```

The gap is the round, and the reason is structural: its cost sheet prices the
chat calls and the answer keys, and is silent about the predicted queries it
then runs. **The agent writes those, they are the largest line, and nobody
bounds them.**

So when you report what an experiment cost, read the ledger. A time window is
not an attribution either — filter to the user, or a shared project will hand
you somebody else's bill.

**Closed 2026-09-13: the round now reads the ledger itself.** `round --cost`
looks up `total_bytes_billed` for the job ids the run already records and
prints the query bill beside the verdict, per arm, as cost per answer **and**
cost per correct answer. It is a metadata read — no gold SQL runs and no table
is scanned — so recovering what a round spent costs nothing. The figures also
go onto `--run-out` as `bytes_billed` and `query_cost_usd`, because the
ledger's own history is 180 days and a committed run should still know what it
cost after that.

Three rules the module keeps, each because the opposite is a number somebody
would quote:

- **Both figures print.** They move in opposite directions when accuracy
  falls, so quoting cost per answer alone can advertise a saving that is a
  loss.
- **A job missing from history is excluded and counted, never zero.** Treating
  it as free would make a round look cheaper the less of it could be read.
- **A round nobody could price says "not recovered. It is not zero"**, rather
  than printing `$0.0000`.

`tests/test_cost_reconciliation.py` asserts a printed figure agrees with the
ledger within 5%, measured against the ledger rather than against the claim —
one of its cases is exactly `$74` against `$149.61`, which must fail. Before
that test existed, the check was a person remembering to do it, and for
several weeks nobody did.

---

## 13. An example handed to the wrong question is a distractor, measured

**Measured 2026-09-12 by direct ablation**, which is why it overrides the
inference it replaces. `contextlift classes --spec examples/thelook/spec
--golds examples/thelook/golds.csv --run`: one class removed per arm, 74
working questions, 370 model calls.

```
  examples              -24.3 points [-34.8, -13.9]   66% without it vs 42% with
                                                       19 fixed, 1 broke
  rules                 +12.2 points [+3.0, +21.3]     30% without vs 42% with
                                                       2 fixed, 11 broke
  terms                  +1.4 points [-5.6, +8.4]      not shown either way
  authored descriptions  -0.0 points [-6.5, +6.5]      not shown either way
```

The full arm scored 42% against 39.2% on the committed round two days
earlier, so this reproduces rather than drifts.

**It does not say examples are bad.** On a 596-question benchmark a
leak-checked example bank measured **+6.9** (`docs/CASE_STUDY.md`). The
difference is hit rate:

```
  bank size             22 examples, 9,063 bytes
  matched a question    about 1 in 3
  carried anyway        the other 2 in 3
```

Two thirds of questions were handed 9 KB of SQL answering something else.
Irrelevant context is measurably worse than none
(https://arxiv.org/pdf/2408.07702).

**So the practice is about routing.** An example given to the question it
matches is the strongest lever measured anywhere in this project. The same
example given to every question is a drag. `routing.py` and
`--context-per-question` already exist; use them, or do not ship examples.

**What it cost to learn this: $0.08.** 725 BigQuery jobs over 0.0121 TiB,
read from the billing ledger rather than a cost sheet (practice 12). The
budget was capped at $20 and never approached, because the public dataset is
small. Four measured answers about what to write, for eight cents, on data
anybody can use. Compare that with five paid rounds on a customer estate that
could not resolve anything (practice 3).

**And it splits "business rules" into two things that measure differently.**
The word-to-column glossary is +1.4 and not shown either way. The
conventions that decide *how to answer* — which rows to exclude, what shape
the answer takes — are +12.2 and the largest effect here. Asking a customer
for "business rules" gets the first and misses the second.

**What this corrects.** `docs/ONBOARDING.md` ranked examples third and the
glossary last, citing +6.9 and +1.9 from a different dataset. The direct
ablation reversed both. The doc is corrected in place with the reason, and
this is the clearest case yet for practice 3: an effect inferred from another
experiment is not a measurement of yours.

---

## What is still unproven, stated plainly

- **Practices 6 and 7** are hypotheses with a mechanism, not measurements.
- **The direction of practice 4's cost line** is unresolved: 17 questions got
  cheaper, 13 dearer, `p = 0.5847`.
- **No customer-written context has been measured by this framework yet.** The
  ladder in `docs/CASE_STUDY.md` used a public benchmark's own human
  annotations. That is the open item, and practice 2 is how it gets closed.

## 14. A semantic layer is not written business context. Measure its description share before trusting it.

**What to do.** Before connecting a BI tool as a context source, run
`contextlift semantic --lookml <repo>` and read the share of metrics carrying a
written description. Near zero means the extraction gives you formulas and
join paths, and no meaning.

**The measurement.** Looker's own public project for thelook reports
`9 metrics, 0 with a written description (0%)`. Read into a context arm and
scored on 102 questions it produced **+2.0 points, NOT PROVEN (p = 0.6875),
safety MIXED** — 4 fixed, 2 broke. The hand-written rules for the same dataset
measured **+12.2 points**. Full output:
`examples/thelook/lookml-arm/round-2026-09-12.txt`.

**Why, structurally.** A LookML measure has a `type` and an expression, so it
can say `SUM(sale_price)`. It has no field for which rows count. That sentence
only fits in a `description`, and descriptions are what teams skip.

**Do not read this as "semantic layers do not help".** Cube measured +17 to
+23 points and dbt Labs +8, both from hand-written prose about business
conventions shipped alongside the layer. The lift in the literature comes from
the prose. This practice is about not mistaking the one for the other.

**And read the safety word, not just the lift.** MIXED, not SAFE. Two
questions that were right without this context were wrong with it. A +2.0 arm
bought with two working answers is a trade, and a connector that ships that
trade silently is worse than no connector.

## 15. Do not narrow reference text to the one line you think applies

**What to do.** When you have written business meaning for a database, hand
the agent the whole set for that database. Do not retrieve only the rule that
matches the question, even if you can match it perfectly.

**The measurement, 2026-09-13.** Two arms over the same 450 BIRD questions,
three repetitions each, gate-cleaned golds only. One arm received every
evidence sentence for its database. The other received only its own
question's sentence — an *oracle* retriever, meaning perfect selection, which
is the ceiling no real retriever can beat.

```
  blob        42.9%  [40.3%, 45.5%]     6,711 prompt tokens per call
  selection   41.3%  [38.7%, 44.0%]       822 prompt tokens per call
  lift         -1.6% [-5.6%, +1.1%]    fixed 25 / broke 35 of 450
                                       p = 0.2451   NOT PROVEN

  per database   toxicology             -7.7   p = 0.0290   REAL LOSS
                 thrombosis_prediction  +3.5   p = 0.3323   NOT PROVEN
                 card_games             -0.8   p = 0.6291   NOT PROVEN
```

Full output: `docs/evidence/blob-vs-selection.txt`. Rebuild with
`scripts/blob_arm.py` then `scripts/blob_vs_selection.py --pool`.

**The proposed mechanism, and it is not proven.** A lone sentence is *obeyed*.
The same sentence inside 145 lines is *consulted*. Counted by
`scripts/salience_counts.py` across 417 runs per arm on toxicology:

```
  puts `label` in the SELECT list when the gold does not
    selection   97 of 417 runs
    blob        74 of 417 runs

  conditioned on whether the question's own sentence mentions `label`
    mentions it      (n=61)   blob right and selection wrong on 20%
    does not mention (n=78)                                       9%
                              two-sided Fisher exact p = 0.0839
```

Both counts point the predicted way and **neither reaches significance**. Use
this as a reason to prefer the blob, which the arm comparison above supports on
its own, not as an established mechanism. Settling it needs matched sentence
pairs differing only in whether they name an unneeded column, assigned rather
than observed, and that experiment has not been run. Full output:
`docs/evidence/over-salience.txt`.

**The competing explanation was tested and refuted.** If breadth were the
blob's advantage, the broken questions would name values appearing in no other
sentence. Of the 19 broken questions, **0** do.

**A typo was the other candidate, and it also lost.** One sentence reads
`bond_type = ' = '`, with spaces inside the quotes, matching no row; the typo
is in 23 of the blob's 145 sentences, so the blob dilutes it while the
selection arm copies it verbatim. Repairing every such literal against the
real column values moved the pooled result **+0.6 points, p = 0.3075**, and
toxicology only +1.0 against a stated kill threshold of +2. The typo is not
the explanation. See `docs/evidence/repaired-literals.txt`; the repair itself
is worth running as free hygiene, not as a source of lift.

**This does not contradict practice 13, it bounds it.** Practice 13 measured a
worked example handed to the wrong question at **-24.3 points** and cites
irrelevant context being worse than none. The difference is what the
irrelevant text *is*. An example is a shape to copy, so a wrong one is
actively followed. A glossary line is reference, so a wrong one is inert. Keep
examples targeted. Do not target reference.

**In a catalog.** This is why an estate-tier or domain-tier rules file stays in
the spec repo and is delivered in the request, rather than being split across
the table entries it mentions. `spec-push` shows the shape: pushing the thelook
spec put an `overview` aspect on **3 of 7** entries, because its rules are
about revenue and output conventions, not about one table.

**What this does not establish.** 450 questions can only resolve a change
above about 5 points, so a small real benefit to selection could hide inside
`-1.6`. And BIRD's golds return raw codes such as `'+'`, which is what makes
decoding them a failure; an estate whose consumers want decoded values would
not fail this way.

## 16. A per-question snippet can never be cached. A shared block can.

**What to do.** Price a context arm on what it is *billed*, not on how many
characters it sends. A big shared context block and a small per-question
snippet sit on opposite sides of the prompt-cache minimum, and the comparison
reverses across that line.

**The measurement, 2026-09-13.** Implicit caching is on by default for Gemini
2.5 and needs **2,048 prompt tokens** on Flash
(https://ai.google.dev/gemini-api/docs/caching). The two arms above:

```
  blob arm       6,606 prompt tokens per call   above the floor
  selection arm    411 prompt tokens per call   permanently below it
```

**Two 25-call probes on byte-identical input disagree, and that is the
result.** `contextlift generate` reports its own usage; full output in
`docs/evidence/cache-probes.txt`:

```
probe 1  165,128 prompt tokens, 18,340 from cache   11%
probe 2  165,128 prompt tokens, 30,566 from cache   19%
```

Same 25 questions, same 22,340-character context, same model, same pool. The
prompt count is identical to the token, so the input really was the same.
**Do not quote a hit rate.** It is not a property of the prompt: it depends on
whether an earlier call has populated the cache by the time a later one fires,
and a four-thread pool races on the first several requests.

What holds across both probes is the eligibility, which is what the practice
turns on: the blob arm clears the 2,048 minimum and some of it is served from
cache; the selection arm is 411 tokens and cannot be cached at any hit rate.

**So the blob's cost penalty is not a property of sending more text.** It
depends on how many calls share the prefix and whether they are spaced to hit
it. The arm that sends more is the only one eligible for the discount.

**Two cost traps this exposed.** The estimator prices prompt and output tokens
and ignores **thinking** tokens: an experiment estimated at `$3.59` actually
cost `$4.80`, the gap being 538,514 thinking tokens. And cached tokens are
reported but still priced at the full rate, because the reference says only
that cost savings are passed on and gives no multiplier — a tool that guesses
a discount under-states a real invoice.

**In a catalog.** One shared context block per database is cheaper per call
than the same text split into per-table fragments, because only the shared
block clears the cache minimum. That is a cost argument for the estate-tier
rules file, on top of practice 15's accuracy argument for it.

**What this does not establish.** Both probes ran at one concurrency setting,
and two runs cannot bound a distribution. Whether serialising the calls or
warming the first one raises the share, and how high it can go, is not
measured. Nor is the size of the discount itself: the reference says savings
are passed on and gives no multiplier, so this project bills cached tokens at
the full rate and treats any saving as unmodelled upside.

[Documentation index](README.md) · Next: [Case studies](CASE_STUDY.md)
