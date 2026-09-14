# What each kind of bad context costs

The committed result of breaking one property of good context at a time, on
450 BIRD questions across three databases.

```
python examples/bird/perturbation/reproduce.py
```

No warehouse, no model, no network, no cost. It prints the curve below from
`correctness-3reps-2026-09-14.csv`, which records only whether each arm
answered each question correctly on each repetition — 310 KB, against 5 MB
for the full scored reports, because a paired comparison needs the flag and
nothing else.

```
450 questions x 3 repetition(s) = 1350 answers per arm, 6 arms, offline
clean arm: 585 of 1350 answers correct (43.3%)

DEGRADATION CURVE, worst first. Per QUESTION -- the number to defend.
positive-control-drop-value-decoding    -17.0 pts  broke  92 fixed  14  p = 0.0000  REAL LOSS
strip-subject                            -1.8 pts  broke  33 fixed  21  p = 0.1337  NOT PROVEN
conflicting-definitions                  -1.3 pts  broke  26 fixed  20  p = 0.4614  NOT PROVEN
irrelevant-context                       -0.3 pts  broke  24 fixed  27  p = 0.7798  NOT PROVEN
unknown-column                           +0.2 pts  broke  23 fixed  26  p = 0.7754  NOT PROVEN
```

Every one of those figures matches the paid run
(`docs/evidence/perturbation-bird-3reps.txt`) exactly — same lifts, same
counts, same p-values. That is what makes this a reproduction rather than a
second opinion, and a test asserts it.

**Each question is counted once.** Three answers to one question are not
three independent facts, so the per-answer test has an inflated denominator
and its p-value reads low. The script prints both so the gap is visible:
`strip-subject` is p = 0.1337 per question against p = 0.0809 per answer.
Quote the first.

## Read the positive control first

`positive-control-drop-value-decoding` deletes every sentence carrying a
quoted literal — the value decoding a schema cannot supply, `label = '+'`
meaning carcinogenic. It costs 17.0 points and breaks 92 questions against 14
fixed, at p = 0.0000.

That row licenses the others. Without it, four NOT PROVEN results would be
indistinguishable from a harness too blunt to detect anything. **If it ever
stops showing a large loss, no other row here means anything.**

It is also the same claim as the headline lift, measured backwards: adding
this evidence is worth **+30.8 points** on 39 questions (p = 0.0075); removing
it costs **−17.0** on 450 questions x 3 reps (p = 0.0000).

## The row that changed the tool

`unknown-column` points a rule at a column no table carries. It was the
linter's **hardest refusal** — it blocked a paid round. Measured at three
repetitions it costs **+0.2** points, 23 questions broken against 26 fixed,
p = 0.7754. At one repetition it was −0.2 with 24 broken and 23 fixed. Null
both times, and the sign flip between the two runs is itself the point: this
is noise, not a small effect.

A rule naming a column that does not exist is mostly *ignored*, not obeyed.
The count that justified REFUSE — 8 of 10 bare column names on one estate were
on no table in the arm — says the defect is **common**, not that it is
**costly**. `src/context_lift/lint.py` now rates it WARN, with the number in
its basis line.

## What this does not establish

- **450 questions resolve about 4.0 points**
  (`power.smallest_resolvable(450)`, at 80% power). Four of the five rows are
  NOT PROVEN, which means those defects cost less than that **or** that this
  set cannot see them. Not the same thing. The tool says how many would be
  needed: over 5,000 questions for the +0.2% on `unknown-column`.
  The denominator is **questions, not the 1350 answers** — using the answer
  count would give 2.0 points and claim a resolution this design does not
  have.
- **The ordering of the middle three rows is not a result.** At one
  repetition `irrelevant-context` measured −2.9 and was the worst of the four
  realistic defects; at three it is the least, at −0.3. That earlier reading
  is withdrawn. The four are not distinguishable from each other or from zero
  here, and the first run's ranking was noise read as an order.
- **One model, three BIRD databases, injected defects on public data** rather
  than defects found in the wild. A defect costing nothing on a clean
  seven-table schema may cost a great deal on a 900-dataset estate where the
  wrong table is expensive.

## Rebuilding it

`scripts/perturb_bird.py` runs the arms (paid, ~$6.50 for 2,700 calls on
gemini-2.5-flash) and `extract.py` turns the scored reports into the committed
file. The perturbations are data, in `src/context_lift/perturb.py`, so the
suite can be re-run against a new model without editing a spec by hand.
