# Case study: a three-day campaign on four public benchmarks

This is the record of the framework's first extended use on data it was not
built against, run 2026-08-30 through 2026-09-01. Every number below was
printed by a framework command; the full logs and prediction files are kept
in a private archive because the datasets' terms and the raw artifacts do
not belong in a public repo. Model: gemini-2.5-flash on Vertex AI unless
marked pro; three repetitions per arm wherever a claim rests on a number.
Total model spend for everything below: about $100.

## One harness, four benchmarks, 36 databases

| dataset | databases | usable golds | gate flagged | schema-only baseline |
|---|---|---|---|---|
| KaggleDBQA (real Kaggle DBs) | 8 | 266 of 272 | 2% | 46.2% |
| Spider 2.0-lite local (community DBs) | 16 | 24 of 24 | 0 | 20.8% |
| BEAVER DW (enterprise warehouse) | 1 | 96 of 121 | 21% | 3.8% |
| BEAVER NW (OpenStack) | 5 | 72 of 88 | 18% | 2.8% |

Two findings sit in that table. First, the gradient: the same model loses a
factor of ten between friendly community schemas and a real enterprise
warehouse - independent confirmation of the BEAVER paper's thesis. Second,
`gate` found a different defect class in every dataset: MySQL-dialect golds
in BEAVER DW, stale filters returning zero rows in NW (the data drifted
under the answer key), nothing in Spider's hand-vetted release. The answer
key's failure mode is dataset-specific, which is why auditing it is a
pipeline stage and not a one-off. Independent research published the same
year found two-thirds of a major leaderboard's golds carry annotation
errors, so this is the field's problem, not one dataset's.

## Does curated context help? Both answers, measured

On KaggleDBQA, whose authors ship real column documentation, adding those
docs verbatim: +5.4 points (p = 6e-6), and on its best database +20.2
points with zero breakage. Where docs exist and the schema is small,
curated context works, significantly.

On the enterprise warehouse the same move nearly failed. Two BEAVER
releases were used: the 121-question 2025 release in the table above, and
the gated 2026 release (5,787 dw questions, of which 2,015 passed the gate;
a fresh split gave 1,419 curate / 596 holdout, so the biggest paired runs
below are 596 questions x 3 repetitions = up to 1,788 observations, 1,779
after timeout exclusions). The measured ladder, each rung a paired pooled
`ablate`:

```
 3.8%   schema-only baseline
 5.7%   + iterated glossary (four curation rounds; round one BROKE 3
        questions and attribute named the rules responsible)
 8.1%   + stronger model (pro), p = 0.004
13.3%   + oracle hints (each question's own human-annotated gold tables,
        join keys, and column mapping - perfect but undeployable context,
        measuring the ceiling of schema-shaped hints)
20.2%   + leak-checked worked-example bank, p < 0.0001
        (596-question holdout, fixed 282 / broke 19)
```

Three lessons a client conversation needs:

1. **Glossaries alone are a weak lever on 100-table schemas.** Six levers
   measured flat or near-flat, each a paired 1-3 rep ablate on the curate
   set: schema retrieval (+0.0), thinking budget (+0.0), execute-and-retry
   (+0.0), value dictionaries (+0.6, p=.37), consensus voting (+1.0, p=.11),
   and the v3->v4 leave-one-out rule edit (+0.0). The negative results are as load-bearing
   as the wins: they are what stops a team spending months on the wrong
   curation.
2. **Worked examples are the strong lever** - what production systems call
   verified queries. They teach SQL patterns, not just column locations,
   which is why they beat the "oracle" schema hints.
3. **The example bank is also the easiest way to cheat by accident.** The
   first bank contained 25 template twins - scored questions' answers in
   disguise - and `leak` caught them; `curate-examples` now bans twins
   until the check passes (47 banned on the final build). Benchmark suites
   are template-expanded, so structurally similar templates remain across
   any split: expect smaller transfer on genuinely novel questions than on
   a held-out slice of the same suite.

## The guards, observed working

- `climb`'s ladder refused a +20-point round on 15 questions and a -4.2
  early result alike: both inside the printed noise floor. Small-n rounds
  that said "not provable" were vindicated when the same effect size
  reached p < 0.0001 at n = 1,779.
- `ablate --show` names every broken question even inside a winning round;
  one arm measured +0.0 lift hiding 8 fixed and 8 broken underneath.
- The `--timeout` rail (added mid-campaign after a valid, read-only,
  index-using query ran 20+ minutes) later caught ~170 runaway golds in a
  single 5,787-question audit.
- Keep concurrent `score` runs to two: SQLite spills sort files to disk,
  and enough parallel scorers can fill it.

## Reproduce the shape of it

`examples/beaver/` carries the committed metadata, build scripts, and a
RUNBOOK with per-step costs. The final framework-only run of the loop -
five commands, no helper scripts - reproduced the headline on the same
holdout: 6.4% to 19.0%, fixed 79 / broke 4, p < 0.0001.

## thelook, on BigQuery, 2026-09-03

Moved here from `docs/QUICKSTART.md` when that file was deleted, so the numbers
keep a home. Public data, `bigquery-public-data.thelook_ecommerce`, 18 curate
questions, one model, one rules file as the only difference between the arms.

| | rate | interval |
|---|---|---|
| without context | 24.1% | [14.6%, 36.9%] |
| with context | 74.1% | [61.1%, 83.9%] |
| lift | +50.0 points | [+21.5, +67.4] |

8 fixed, 0 broken, p = 0.0078. The whole twelve-command loop cost **$0.34**.

## thelook, the loop closing, 2026-09-10

Two rounds over the same 104 golds, the same split and the same seed. The
only thing that changed between them is seven lines of written business
convention. The full story, with every command and its output, is in
[WALKTHROUGH.md](WALKTHROUGH.md).

Round 1 refused to claim anything:

```
verdict: UNDERPOWERED
  30 held-out questions cannot show a 10-point lift even if nothing broke;
  about 60 would. Nothing here says the context does not help
  held out: 30 questions, +13.3 points, 5 fixed, 1 broke
```

The held-out set MOVED, +13.3 points, five fixed and one broken. Six changed
answers out of thirty cannot rule out luck: p = 0.219 against a bar of 0.05.
Most tools would print +13.3 and stop.

The round then counted the failures on the working questions and said "16 of
28 are COLUMN_COUNT: worked examples", followed by its own warning that this
reads a count and not a cause. Reading three failing pairs side by side found
one cause behind thirteen of them: every rate, share and percentage question
returned the answer together with the numerator and denominator it came from.
One convention, one line, not sixteen examples.

Seven convention lines later, round 2:

```
verdict: REAL GAIN
  the held-out questions moved +20.0 points (p = 0.0312), and they were read once
  held out: 30 questions, +20.0 points, 6 fixed, 0 broke
  fit on the working questions: 52 of 74 questions, +30.8 points, 16 fixed, 0 broke
  gap: +10.8 points [-8.3, +29.8], the working-set lift minus the held-out one.
  It includes zero, so there is no evidence the context fitted the questions
  it was written from
```

| | round 1 | round 2 |
|---|---|---|
| held out | +13.3, 5 fixed, 1 broke | **+20.0, 6 fixed, 0 broke** |
| p | 0.219 | **0.0312** |
| verdict | UNDERPOWERED | **REAL GAIN** |

Read the gap before reading the win. It is the working-set lift minus the
held-out one, and it answers the only question that matters about rules
written while staring at failures: did the author just describe the answer
key? If the seven lines only described the 74 working questions they would
have helped those and done nothing for the thirty nobody looked at, the
verdict would read OVERFIT, and the gap's interval would exclude zero. It
includes zero.

Two things this does not claim. The honest sentence is "written conventions
make the agent match the business", not "the agent learned the business":
several of the seven lines resolve ambiguity in questions the author wrote.
And the same author wrote the questions and the context, which the held-out
split narrows rather than removes. Only questions mined from a real query log
remove it.

## How stable is a run? Two identical rounds, 2026-09-09

The same 26 questions, the same two arms (`2404ed0d5166` against
`06049c1c64a7`), K=1, run twice about two hours apart. The second was launched
from a fresh clone as part of the end-to-end check.

| | curate lift | fixed | broke |
|---|---|---|---|
| first run | +10.0 points | 4 | 2 |
| second run | +20.0 points | 5 | 1 |

Comparing the two run files question by question:

| | |
|---|---|
| (question, arm) pairs compared | 52 |
| answers with identical SQL | **0** (0%) |
| answers whose correctness changed | **2** |

The model rewrote every single query and scored the same way on 50 of 52. The
whole 10-point difference came from one question, `tl021`, which happened to
flip in opposite directions in the two arms: right then wrong in the control,
wrong then right in the treatment. On 20 questions that one flip is worth
exactly 10 points.

Two things follow, and both changed the code.

The repeat check cannot read the answer text. `reps_identical` asks whether
every repetition came back the same, and against this agent that is never
true, so it could never say "K=1 is enough". `reps_correctness_stable` asks
whether the thing the score is made of came out the same way twice, which here
was 96%.

And K=1 is not enough on a set this small. One question of noise moved the
headline by 10 points, which is the size of the effect the round is trying to
see.

## The answer key, gated for the first time, 2026-09-09

The 26 thelook golds had never been through `gate`, the answer-key check, so
every number measured against them rested on an unaudited key. Running it:

    contextlift gate --db bq://BILLING/bigquery-public-data.thelook_ecommerce \
                  --golds examples/thelook/golds.csv

**26 of 26 clean, 0 flagged.** Every gold executes and none carries a blocking
defect. 26 queries, 0.2 minutes, under a cent.

Named `--db bq://BILLING` alone, the same run flagged **26 of 26** and produced
no clean set. Every gold reads `bigquery-public-data` while `--db` names the
billing account, and the check reads the two as a mismatch. The way out already
existed and the message did not mention it; it does now.

Three warnings survive on many golds, and one of them is the design working:

`MISSING_LITERAL` fires on nine golds with "the question may leave out 2 values
the gold filters on (Cancelled, Returned)". That is exactly right and exactly
intended. `make_golds.py` takes the business rules out of the questions,
because a question that defines its own terms cannot measure whether writing
the definition down helps. The rule is in the spec repo instead. On an ordinary
estate this warning means a question nobody can answer; here it means the
question is doing its job, and a reader has to know which case they are in.

`MISSING_ORDER` and `MISSING_PROJECTION` are ordinary: the golds order rows and
name columns the questions do not. Both are kept, and both are reasons a
correct answer can still score wrong.

## Measure the description share before counting on a BI model: 0 of 9, +2.0 points

The question every customer asks in the first meeting is "can't you just read
my BI tool?" The answer turns on one number that nobody checks first: how many
of the model's metrics carry a written description. On the public demo project
measured below that number is **0 of 9**, and a model with nothing written in
it has nothing written to give an agent.

This is a property of one project, not of the tool that hosts it. A BI model
is built so a dashboard returns the right figure, and a dashboard never needed
a sentence explaining which rows count. Nobody wrote those descriptions
because nothing until now read them.

Looker's own public project for thelook was read into a context arm and one
round was run against the committed 104-question thelook gold set. Source:
`github.com/looker-open-source/thelook`, commit `88d68c3`, dated 2022-02-09.

```
python scripts/lookml_arm.py --lookml <clone> --out examples/thelook/lookml-arm
contextlift round --golds examples/thelook/golds.csv \
               --spec examples/thelook/lookml-arm \
               --db bq://PROJECT/bigquery-public-data.thelook_ecommerce \
               --holdout-share 0.99 --target-lift 11.0 --k 1 \
               --extra-columns reject --budget 40 --yes
```

<!-- evidence: examples/thelook/lookml-arm/round-2026-09-12.txt -->
```
verdict: NOT PROVEN
  102 held-out questions could have shown a 10-point lift and did not (p = 0.6875)
safety: MIXED
  4 fixed, 2 broke, of 102 on the held-out questions
  broke: tl018, ta002
  held out: 102 questions, +2.0 points
  arms 2404ed0d5166 against c6266a145763, K=1, 208 answers scored
```

Six questions moved: four up, two down. That is noise. The full output is
committed beside the arm, and `tests/test_case_study_numbers.py` fails if this
block stops matching it.

**Not an underpowered round, which is the whole point.** The tool reports that
a 10-point lift was resolvable on this holdout. The design could have seen a
large effect; the truth was +2.0. That is different from the earlier customer
rounds, which could not have reached a verdict at any effect size.

**Why every question is scored and none reserved.** A reserved half stops a
person writing rules while looking at the questions. Looker wrote this project
in February 2022. The gold set was built in 2026 from `queries.sql` by
`make_golds.py`. The context predates the questions by four years and nobody
in this loop authored it, so it cannot have been fitted to them.

**The mechanism, free to check before paying anything:**

```
$ contextlift semantic --lookml <clone>
  9 metrics, 0 with a written description (0%)
  38 dimensions, 0 of them described
  6 join paths
```

Zero of nine. The arm could offer `SUM(sale_price)`,
`COUNT(DISTINCT order_id)` and six join conditions. The hand-written thelook
rules say "Revenue is the sale price of items that are neither Cancelled nor
Returned" — *which rows count*. LookML has nowhere to put that except a
`description`, and nobody writes descriptions.

**Set against the published results, this is a distinction and not a
contradiction.** Cube's benchmark measures +17 to +23 points, paired McNemar
p < 0.002, on 100 questions — nearly our n, the same test. What they supplied
was 4 KB of *hand-authored markdown* about business conventions. dbt Labs
measure +8 points on an already-modeled project. A JAMIA Open study moved 8.3%
to 78.3% on 60 queries with a *hand-written* business-context document. In
every case the lift came from prose a person wrote, not from a layer's
machine-readable definitions.

Generated context is not the problem either: Shkapenyuk et al. (2025) topped
BIRD at 81.95% with automatically generated business context. But that system
generates by profiling the database and mining query logs — from data and
usage. It does not extract a BI tool's existing definitions.

**The caveat.** Looker's demo project is small and 0% described. A large
enterprise Looker project maintained by analysts for years may carry a high
description share, and this round says nothing about that case. The claim is
conditional on the description share — which is exactly what makes it usable,
because that share costs one free command to measure before anyone pays.

**What to do with it.** Run `contextlift semantic --lookml` on the project first
and read the description share. If it is near zero, the extraction is worth
join paths and little else, and the afternoon is better spent on
`docs/ONBOARDING.md`. And note the safety word: MIXED, not SAFE. This context
broke `tl018` and `ta002`, which were right without it. +2.0 points bought
with two working answers is a trade, not a win.

## Sending everything beats perfect selection, 2026-09-13

The question this was built to answer is whether written context helps. It
skips a prior one: **does it matter which context you send?** Every retrieval
system assumes yes. So the assumption got measured, with the retriever set to
cheat.

Two arms on 450 BIRD questions across three databases, three repetitions each.
The *blob* arm sends every evidence sentence for the database — 145 sentences,
22,340 characters, no selection at all. The *selection* arm sends only the one
sentence BIRD attached to that exact question. That is oracle retrieval: not a
real retriever, but the ceiling on every real retriever, because no ranker can
beat knowing the answer key.

Full output in `docs/evidence/blob-vs-selection.txt`. The pooled result:

```
paired ablation on 1350 observations (450 questions x 3 reps, execution accuracy)
  without context :  42.9%  [40.3%, 45.5%]
  with context    :  41.3%  [38.7%, 44.0%]
  lift            :  -1.6%  [-5.6%, +1.1%]

  each question counted once - the number to defend
    fixed 25 / broke 35 of 450 questions
    p = 0.2451   not significant at 0.05

  ruling          : NOT PROVEN
```

Read the arm names carefully: *without context* is the blob and *with context*
is the selection, so the negative sign means the blob won. It won by 1.6
points, which at 450 questions this set cannot resolve — it can only prove a
change above about 5 points.

**One database moved enough to call.** Toxicology:

```
  lift            :  -7.7%  [-15.7%, -1.6%]
    fixed 7 / broke 19 of 139 questions
    p = 0.0290   significant at 0.05
  ruling          : REAL LOSS
```

Nineteen questions that the blob answered correctly were broken by receiving
their own evidence sentence and nothing else. Not by receiving *less* useful
text — by receiving *only* the useful text.

**What this does not establish.** It does not say retrieval is useless. It says
that on three BIRD databases, with a 22 KB blob that fits comfortably in the
context window, perfect selection bought nothing and cost 7.7 points on one
database. A 200-table estate whose blob will not fit is a different problem and
this result says nothing about it. It also does not compare against a *real*
retriever, which would perform worse than the oracle and so cannot rescue the
result.

**What it changes.** Selection is not the first thing to build. `docs/PRACTICES.md`
practice 15 states it as a rule: do not narrow reference text on the
assumption that focus helps.

## Why the winning arm wins: over-salience, directional and unproven

Toxicology's 7.7-point loss needs a mechanism, or it is a coincidence with a
p-value. The proposed one: **a lone sentence is obeyed as an instruction, while
the same sentence among 145 is consulted as reference.**

Toxicology's evidence keeps saying `label = '+'`. When that sentence arrives
alone, the answers start selecting `label` — a column the question never asked
for, which changes the returned shape and fails execution comparison. When it
arrives as one line of 145, it competes for attention and gets treated as
background.

That is a story. Counted by `scripts/salience_counts.py`, output in
`docs/evidence/over-salience.txt`:

```
1. runs that put 'label' in the SELECT LIST, per arm
   blob           74 of 417 runs   (182 name it anywhere in the statement)
   selection      97 of 417 runs   (205 name it anywhere in the statement)

2. blob right and selection wrong, split on whether the question's OWN sentence names 'label'
   sentence names it    12 of 61  broke  (20%, n=61)
   sentence does not     7 of 78  broke  (9%, n=78)
   two-sided Fisher exact p = 0.0839
```

Both counts point the predicted way. **Neither is significant.** The break
rate roughly doubles when the question's own sentence names the column, at
p = 0.0839, which is not 0.05.

The clause matters and picking the wrong one hides the effect entirely.
`WHERE label = '+'` is usually *correct* — the question asked about
carcinogenic molecules. The mistake is `label` in the SELECT list. Counted
anywhere in the statement the arms are 205 against 182 and there is nothing to
see.

**The competing explanation was tested and lost.** If the blob's advantage were
breadth — the value you need being spread across several sentences — then the
broken questions would be ones whose values appear nowhere else. Of the 19
broken questions, **0** name a value that appears in no other sentence. That
explanation is refuted. Over-salience is not thereby proven; it is what
survives.

**This number has a correction attached.** While the session was running the
same effect was counted by eye as 37 select-list mentions against 9, and a
break rate of 29% against 5%. Neither survived being computed by a program.
The script's output is the record.

**What would settle it.** Matched sentence pairs that differ only in whether
they name a column the question does not need, assigned rather than observed.
That experiment has not been run.

## Self-Route: the escape hatch nobody takes

If the blob wins but costs 16× the prompt tokens, the obvious fix is to send
the small context and let the model ask for more. That is Self-Route
(arXiv 2407.16833, EMNLP 2024), which measures a 65% cost cut at comparable
accuracy on long-context document QA.

It does not transfer. Full output in `docs/evidence/self-route.txt`:

```
  strict wording    2 of 139 escalated   1.4%
  soft wording      not recoverable -- see below
```

Two questions of 139 took the hatch. **A firmer instruction did not raise the
rate**, which is the useful part: the model is not staying silent because the
wording was too gentle. At 1.4% there is no escalation to price.

The guess at why, and it is a guess: the model can write valid SQL from the
schema alone, so "not enough information" is not a state it recognises. It does
not need the context to produce an answer. It needs the context to produce the
*right* answer, and it cannot tell those apart from the inside.

**An honest gap.** Both arms were given the same `--out` path, so the second
run overwrote the first. The strict arm survives on disk. The soft arm's rate
was reported as about 4% while the session ran and **cannot now be checked**,
so it is not quoted as a result. Its token counts survive, which is the only
proof the run happened. Recovering the number costs $0.13 and four minutes; it
has not been spent, because 1.4% already closes the route.

## Why the cheap arm cannot be made cheaper, 2026-09-13

Gemini 2.5 caches prompt prefixes implicitly, with **no discount below 2,048
prompt tokens on Flash**. That floor decides which arm can ever be discounted,
and the two arms fall on opposite sides of it. From the `spent:` lines in
`docs/evidence/blob-vs-selection.txt`, divided by their call counts:

| arm | prompt tokens per call | above the 2,048 floor? |
|---|---|---|
| blob | 2,754,714 / 417 = **6,606** | yes |
| selection | 171,393 / 417 = **411** | no, and never will be |

The expensive arm is the only one eligible. The cheap arm is 411 tokens and a
per-question snippet cannot be cached even in principle: the prefix changes
every call, so there is no shared prefix to hit. Making the selected context
five times longer would still leave it under the floor.

So the cost gap between the arms is smaller than the token counts suggest, and
it narrows in the blob's favour the more questions you ask against one
database. `docs/PRACTICES.md` practice 16 records this.

**The hit rate is not a number this can report, and two probes show why.** Run
twice on byte-identical input — same 25 questions, same 22,340-character
context, prompt count identical to the token — the cache served 11%, then 19%.
Output in `docs/evidence/cache-probes.txt`:

```
probe 1  165,128 prompt tokens, 18,340 from cache   11%
probe 2  165,128 prompt tokens, 30,566 from cache   19%
```

A hit rate is not a property of the prompt. It depends on whether an earlier
call has populated the cache by the time a later one fires, and the runner's
four-thread pool races on the first several requests. **No hit rate is
claimed.** What survives both probes is the eligibility, which is the point of
the section: the blob arm clears the floor and some of it is served from cache,
the selection arm cannot be at any rate.

The second probe exists because the first was quoted in `docs/PRACTICES.md`
with no committed run behind it. Re-running cost $0.07 and turned one
unsourced figure into a measured instability, which is worth more than the
figure was.

## The typo that was not the explanation, 2026-09-13

Toxicology's evidence contains `bond_type = ' = '` — with spaces inside the
quotes. No row holds that value, so an agent that follows the rule faithfully
returns nothing. Across three databases, 40 quoted literals matched no real
column value while differing from one only by whitespace or case.

That is a competing explanation for toxicology's 7.7-point loss, and a better
one than over-salience if true: the blob carries the same typo in 23 of its 145
sentences and dilutes it, while the selection arm carries one copy and copies
it verbatim. So `scripts/repair_literals.py` repairs every literal against the
real column values, and the repaired context ran as its own arm.

**The arm stated its own kill condition before it ran:** if toxicology moves
less than +2 points, the typo was never the mechanism. It moved +1.0. Full
output in `docs/evidence/repaired-literals.txt`:

```
=================== POOLED, 3 databases ===================
paired ablation on 1350 observations (450 questions x 3 reps, execution accuracy)
  without context :  41.3%  [38.7%, 44.0%]
  with context    :  41.9%  [39.3%, 44.6%]
  lift            :  +0.6%  [-0.8%, +3.5%]
    fixed 15 / broke 9 of 450 questions
    p = 0.3075   not significant at 0.05
```

Per database: toxicology +1.0 (p = 0.5488), card_games +1.6 (p = 0.2891),
thrombosis_prediction −0.9 (p = 1.0000). Repairing 40 wrong literals bought
six tenths of a point, and toxicology — the database with 26 of the 40 —
moved least of the two that moved at all.

**The arm did its job by failing.** The typo is not why toxicology lost 7.7
points, so over-salience survives by elimination. An arm designed to kill a
hypothesis, which then does not, is worth more than one designed to confirm it.

**Keep the tool anyway, at a lower rank.** The repair is free, runs offline,
and found two distinct defect classes: whitespace in toxicology
(`' = '` → `'='`) and wrong case in card_games (`'restricted'` → `'Restricted'`).
Thrombosis_prediction had 0 repairable literals and 45 left alone, mostly date
and numeric columns a text-value scan cannot see — so the check is also honest
about where it is blind. It is worth running as hygiene before a round. It is
not worth claiming as lift.

**What it does not establish.** The repair is deliberately timid: a literal is
replaced only when exactly one real column value matches after trimming and
folding case. It never guesses at a near-miss, because a plausible wrong value
returns the wrong rows silently while the typo returns none and is visible.
A bolder repair might find more and would need its own safety measurement.

## What each kind of bad context costs, 2026-09-13

Every result above measures whether *good* context helps. This measures what
*bad* context costs, by breaking one property at a time and re-running. It is
the first experiment here with known ground truth: when you inject the defect
yourself, you do not have to adjudicate whether it is really a defect.

**The suite ran twice, and the first run was about the suite.** On thelook's 74
working questions the positive control — deleting a rule known to be
load-bearing — measured **−4.1 points**, while 74 questions resolve a change
above about **14.0** (`power.smallest_resolvable(74)`). A suite that cannot
resolve its own control cannot resolve anything smaller, so the remaining six
arms were not run. Output in `docs/evidence/perturbation-positive-control.txt`.

It moved to BIRD: 450 questions, resolving about 4.7 points, on a backend with
published prices. 2,700 calls, **$6.54** actual against $5.28 estimated.

```
defect                                  lift    95% interval        ruling
positive-control-drop-value-decoding  -18.9%  [-23.3%, -14.5%]   REAL LOSS
irrelevant-context                     -2.9%  [-6.0%,  +0.2%]    NOT PROVEN
strip-subject                          -2.7%  [-6.1%,  +0.8%]    NOT PROVEN
conflicting-definitions                -1.1%  [-4.5%,  +2.3%]    NOT PROVEN
unknown-column                         -0.2%  [-3.2%,  +2.8%]    NOT PROVEN
```

**The positive control did its job.** Deleting every sentence carrying a quoted
literal — the value decoding a schema cannot supply, `label = '+'` meaning
carcinogenic — broke **102 questions and fixed 17**, at p = 0.0000. So the
harness registers damage it knows is there, and every other row above is a
real measurement rather than a measurement of the harness's own deafness.

**The finding that changes the tool.** `unknown column` was the linter's
hardest refusal. Injected, it costs **−0.2 points [−3.2, +2.8]** — the
tightest interval of the four and centred on zero. A rule naming a column that
does not exist is mostly *ignored*, not obeyed. The count that used to justify
REFUSE — 8 of 10 bare column names on one estate were on no table in the arm —
says the defect is **common**, not that it is **costly**, and those are
different claims. `src/context_lift/lint.py` now rates it WARN, and the basis
line carries the measurement.

**Then it was run again at three repetitions, and one row did not survive.**
The tool's own output on the first run said *"one repetition per arm: a single
sample, not a measurement; run each arm again before quoting this."* So it was:
450 questions x 3 reps = 1,350 observations per arm.
Evidence: [`docs/evidence/perturbation-bird-3reps.txt`](evidence/perturbation-bird-3reps.txt).

```
defect                     1 rep    3 reps   rank 1rep -> 3rep
positive-control           -18.9    -17.0      1 -> 1
irrelevant-context          -2.9     -0.3      2 -> 4   MOVED
strip-subject               -2.7     -1.8      3 -> 2
conflicting-definitions     -1.1     -1.3      4 -> 3
unknown-column              -0.2     +0.2      5 -> 5
```

The control held: −17.0 points [−21.5, −13.1], **92 questions broken against
14 fixed**, p = 0.0000. The two ends of the table agree. What did not survive
is the ordering of the middle.

**A withdrawal, and it was mine.** At one repetition `irrelevant-context` —
five extra sentences naming columns no question uses — was the *worst* of the
four realistic defects at −2.9, and this file said it was closer to the −24.3
distractor result than Practice 15 predicts, calling it a lead worth
following. At three repetitions it is the **least** harmful of the four, at
−0.3 with an interval reaching +3.8. The lead is withdrawn.

It was one sample of a quantity whose interval already spanned six points.
At 450 questions the four realistic defects are not distinguishable from each
other or from zero, so the ordering the first run produced was noise being
read as a ranking. That is the whole reason the tool prints an interval beside
every lift, and I quoted the point estimate anyway.

**What this does not establish.** One model, three BIRD databases, injected
defects on public data rather than defects found in the wild. Four of the five
rows are NOT PROVEN, which means the defects cost less than about 5 points
**or** that 450 questions cannot see them — and the tool says which: resolving
the +0.2% on `unknown-column` would need **more than 5,000 questions**. A
defect costing nothing on a clean schema may cost a great deal on a
900-dataset estate where the wrong table is expensive.

## The 22 worked examples were costing 27 points, 2026-09-14

Run it: `make classes-thelook PROJECT=<project>`. Evidence:
[`docs/evidence/classes-thelook-cost.txt`](evidence/classes-thelook-cost.txt).

Every result above measures whether authored context helps *in total*. This
one asks a narrower question: **which class of it is doing the work?** One arm
carries all four classes. Four more each remove exactly one class and change
nothing else — same 74 questions, same schema, same model.

```
what each class is worth, on 74 working question(s):
  examples                  -27.0 points [-37.8, -16.2]  66% without it against 39% with, 21 fixed 1 broke, 22 item(s)  the arm is better WITHOUT it
  terms                      -2.7 points [-9.2, +3.8]  42% without it against 39% with, 4 fixed 2 broke, 5 item(s)  not shown either way
  authored descriptions      -1.4 points [-5.9, +3.2]  41% without it against 39% with, 2 fixed 1 broke, 10 item(s)  not shown either way
  rules                      +5.4 points [-5.1, +15.9]  34% without it against 39% with, 6 fixed 10 broke, 50 item(s)  not shown either way
```

A negative number means the arm scored **better without that class**. The
worked examples were not helping. They were costing 27 points: 66% against
39%, 21 questions fixed by their removal against 1 broken, and an interval
that does not reach zero. On 74 questions the floor is about 12 points
(`power.smallest_resolvable(74)`), so this is the only one of the four rows
that clears its own floor. The other three are reported as *not shown either
way*, which is not the same as costing nothing.

**Then the cost side, and the two axes disagree.**

```
  cost: $0.0361 of query bill across 370 answer(s)
    0 full arm     $0.0107  $0.0001/answer  $0.0004/correct answer
    minus examples $0.0113  $0.0002/answer  $0.0002/correct answer
```

Removing the examples **raised** the query bill, $0.0107 to $0.0113, and at
the same time **halved** the cost per correct answer, $0.0004 to $0.0002. The
arm scanned more data and got far more right, so per useful answer it came out
twice as cheap. Cost per answer alone calls that arm more expensive. Cost per
correct alone hides that it scanned more. This is the run that made the tool
print both.

**Why this is the most useful number in the file.** The whole framework has
been answering "did the context help". A NOT PROVEN on a whole spec leaves
nowhere to go. A 27-point loss attributed to one of four classes is an
instruction: the next round drops the examples. And the cheapest test is
narrower still — remove a single example and re-run, which is what the tool
prints underneath.

**What this does not establish.** It is not a claim about worked examples.
It is a claim about *these 22* on *this seven-table schema* with *this model*,
and the established distractor result runs the other way — a worked example
handed to the wrong question costs 24.3 points, which is a reason a *bank* of
22 could hurt while a well-matched example helps. One model, one public
dataset, one repetition per arm. The dollar figures are warehouse query bill
only; model calls are billed separately on the run's own `spent:` line, and
neither line is the whole bill.
