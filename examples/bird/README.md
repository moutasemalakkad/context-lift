# The whole loop, on public data, offline

## Start with the enterprise study

```bash
make example-bird-enterprise
```

That dependency-free receipt tour shows the newer 11-database, three-repetition
study: a targeted expert-evidence and catalog policy raised fresh-holdout
accuracy from **37.4% to 56.9%** (**+19.5 points**, paired 95% interval +12.1
to +26.9). It also prints the 25.4% coverage boundary, three stable
regressions, and the oracle-routing qualification. The full methodology and
source-pinned reproduction are in
[`ENTERPRISE_RUNBOOK.md`](ENTERPRISE_RUNBOOK.md).

The original small walkthrough remains useful when you want to execute five
framework stages locally rather than inspect a large-run receipt:

```bash
python examples/bird/run.py      # or: make example-bird
```

No API key, no network, no cloud. Runs in about a second and prints five of the
stages a field engineer runs when onboarding a customer: gate, score, triage,
climb, ablate. Mining and the human review come before them and need a query
log and a reviewer, so they are not in the offline run.

## Why this dataset

Three [BIRD](https://bird-bench.github.io/) dev databases, **unmodified** —
`superhero`, `student_club`, `toxicology`. 5.3 MB together, so they commit.

Picking small databases rather than truncating a big one is deliberate. An
earlier version cut `california_schools` down to two counties to fit; that broke
29 of its 89 gold queries, whose WHERE clause named a county that no longer
existed. Both arms then returned empty and scored as agreement, which flatters
every number.

BIRD is the right teaching set for context work because **every question ships
an `evidence` string written by a human** — a business rule the schema does not
contain:

> `Free meal rate = Free Meal Count (K-12) / Enrollment (K-12). SAT performance
> levels are categorized as: Below Average (total score < 1200)...`

That is a metric definition plus a categorisation rule. It is the public
equivalent of an enterprise convention like *"unrestricted stock excludes
blocked and quality-inspection stock"* — the kind of thing no amount of reading
the schema tells you, and the reason an uncurated catalog scores badly.

So the enrichment rounds are not invented: **each round adds BIRD's own
evidence** for one failure shape.

## What you will see

```
round                     rate   ladder   vs prev   vs base  note
round 0: no context     30.8%   30.8%     +0.0%     +0.0%
round 1: top bucket     66.7%   66.7%    +35.9%    +35.9%
round 2: all rules      61.5%   66.7%     -5.1%    +30.8%  REGRESSED: broke 3, fixed 1
```

```
paired ablation on 39 questions (execution accuracy)
  without context :  30.8%  [18.6%, 46.4%]
  with context    :  61.5%  [45.9%, 75.1%]
  lift            : +30.8%  [+11.8%, +49.8%]   (the two rates subtracted; 95% interval on the paired difference)

  context fixed   : 15 questions
  context broke   : 3 questions
  McNemar p       : 0.0075   significant at 0.05
  one repetition per arm: a single sample, not a measurement; run each arm again before quoting this
  answers changed at 35 of 39 pairs; 18 changed the verdict, 17 moved without changing it
```

(The real report ends with a plain-words key to every term; one run per
arm here, so there is no per-question line to prefer over this one.)

Four things in that table, and the third one is why the loop exists:

1. **The baseline is not zero.** Some questions are answerable from the schema
   alone. Curation is worth the difference, not the whole number.

2. **Round 1 curated only the largest triage bucket.** That is the method:
   measure, find the biggest failure shape, write the rules for it, re-measure.

3. **Round 2 added rules for everything and got WORSE.** 66.7% down to 61.5%,
   broke 3 and fixed 1. Targeted curation beat blanket curation on real data.
   That was not designed into the example; it is what the run produced.

4. **The ladder held at 66.7% through the regression.** It only rises on a gain
   larger than the noise floor, so a bad round cannot lower the reported number
   and a lucky one cannot inflate it.

## Where the predictions come from

**Real model output.** `gemini-2.5-flash` at temperature 0, generated once by
`run_full_dev.py` and checked in so this prints the same numbers on every
machine with no API key. The gold SQL and the business rules are BIRD's own.

The questions are filtered, and the filter matters: gold must return exactly
**one column**, the evidence must state a formula, and the gold SQL must
actually compute it. Without the one-column rule a model with the right answer
gets marked wrong for picking different columns than the gold author did — on a
first attempt that was 5 of 12 failures and it hid the signal entirely.

Across all 11 BIRD dev databases the same setup gives **+23.1 points, 44 fixed,
10 broken, p < 0.0001 on 147 questions** (`run_full_dev.py`).

## Files

| file | what it is |
|---|---|
| `db/*.sqlite` | three unmodified BIRD dev databases, 5.3 MB |
| `predictions_round*.csv` | the three arms, real model output |
| `evidence.csv` | BIRD's business rule for each question |
| `run_full_dev.py` | regenerates predictions; needs the BIRD download and a model |
| `run.py` | the walkthrough |

## Doing this on your own data

The same loop on your own data, from the top:

```bash
contextlift mine   --db warehouse.db --queries query_log.csv --out candidates.csv
# a person reviews candidates.csv and writes the questions
contextlift gate   --db warehouse.db --golds golds.csv
contextlift score  --db warehouse.db --predictions baseline.csv
contextlift triage --db warehouse.db --predictions baseline.csv --show
# curate the rules for the largest bucket, re-run your agent, then:
contextlift climb  --db warehouse.db \
  --round "baseline=baseline.csv" --round "round 1=round1.csv"
```

**Run the baseline before you curate anything.** It is the only number that
shows what the curation was worth, and it cannot be recovered afterwards.

**And hold questions out.** `climb` warns about this because scoring on the
same questions that guided your curation measures how well you fitted them, not
whether the context generalises. Split the gold set, curate against one half,
and score the other half once, at the end.
