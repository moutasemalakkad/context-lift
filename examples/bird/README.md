# The whole loop, on public data, offline

[Documentation](../../docs/README.md) / [Examples](../README.md) / BIRD

## Start with the small example

```bash
make example-bird
```

Run from the repository root with Python 3.10+ and uv installed. Initial
setup may need network access; the example scores committed predictions
against local SQLite databases with no API key or cloud calls. It demonstrates
gate, score, triage, climb, and ablate.

This is the local example in [guide stage 1](../../docs/README.md#1-install-and-try-the-example).
It has one repetition per arm and no reserved holdout. Its result is a
reproducible illustration, not a fresh estimate of generalization.

## Explore the enterprise study

```bash
make example-bird-enterprise
```

This reads the recorded 11-database, three-repetition study. The tour reports
both question-level and answer-level statistics; do not mix their denominators.
It includes the 25.4% coverage boundary, stable regressions, and the
oracle-routing qualification. See the [enterprise runbook](README.md#enterprise-study)
for exact results and fresh-run requirements.

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

Against the baseline, the all-rules arm broke **3 questions**:
`student_club_011`, `student_club_012`, and `superhero_006`.
These are the `broke` entries in the paired report, alongside 15 fixed questions.


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

## Enterprise study

The recorded 11-database experiment has its own local tour:

```bash
make example-bird-enterprise
```

The following is benchmark-specific provenance and reproduction, including
paid generation. Follow the [main guide](../../docs/README.md) for a new
experiment on your own data.

<details>
<summary>Study results, qualifications, and full reproduction commands</summary>

### Result first

On a fresh, never-generated 65-question cohort, selected by a policy learned on
development questions:

| arm | correct answers | execution accuracy |
|---|---:|---:|
| schema only | 73 / 195 | 37.4% |
| expert evidence + relevant catalog pages | 111 / 195 | 56.9% |
| **paired lift** | **+38 answers** | **+19.5 points** |

The table counts model answers. For the independent-question comparison, quote
**+18.5 points [6.5, 30.4] across 65 questions**, with question-level
McNemar **p = 0.0044** and ruling **REAL GAIN**. The pooled-answer estimate
is +19.5 [12.1, 26.9]; three repetitions of a question are correlated, so
that narrower interval overstates independent evidence. The source is
[enterprise_result.json](enterprise_result.json). At stable
question level, context fixed 15 and broke 3: `bird_0521`, `bird_1036`, and
`bird_1188`. Eleven more questions were unstable across three repetitions.
This is a large gain, but it is not a clean win.

The policy covered **65 of 256 reserved questions (25.4%)**. It should be read
as “serve this context when an explicit formula or literal definition was
retrieved,” not “accuracy rose 19.5 points on every BIRD request.”

### What makes the benchmark enterprise-shaped

[BIRD](https://bird-bench.github.io/) contains human-crowdsourced questions,
expert-authored external knowledge, dirty values, and professional-domain
databases. The full benchmark reports 12,751 pairs over 95 databases and 37
domains. This study uses its public validation estate: 1,534 questions over 11
SQLite databases, including finance, card transactions, education, a codebase
community, sport, and clinical prediction.

The source is pinned, rather than downloaded from a moving `main` branch:

| Source | Pinned revision |
|---|---|
| Questions: `target-benchmark/bird-queries-validation` | `d108575b93498f02410b579a89e38488eeb7180d` |
| Databases and descriptions: `target-benchmark/bird-corpus-validation` | `5ec3b6bb52e85d4ff2f7ad97b373ef8c0d8a096f` |

BIRD's upstream repository labels the dataset CC BY-SA 4.0. The large source
files are downloaded for the run and are not redistributed here.

### The design, including the failed ideas

The autonomous search did not start with the winning headline:

1. **Gate the answer key.** Of 1,534 source questions, 1,380 survived and 154
   were flagged. Defects overlapped: 82 missing parameters, 44 degenerate
   answers, 20 truncations, 6 text aggregates, 5 unordered limits, and 1
   timeout. Gate findings were removed before any model score.
2. **Require retrievable expert evidence.** 1,233 clean questions had nonempty
   BIRD evidence. This availability filter was applied without model outcomes.
3. **Create development and final reserves.** The deterministic default split
   assigned 347 questions to intervention development and 886 to a still
   untouched pool. Before generating any of those 886, seed 77 reserved 256 as
   the final pool and left 630 unused for future work.
4. **Measure two broad interventions on development questions.** Three
   repetitions produced 1,032 paired observations after three questions with
   unpaired execution outcomes were excluded symmetrically:

   | development arm | accuracy | lift | stable fixed | stable broke |
   |---|---:|---:|---:|---:|
   | schema only | 50.3% | — | — | — |
   | expert evidence | 57.7% | +7.4 points | 37 | 13 |
   | evidence + relevant catalog pages | 59.1% | +8.8 points | 43 | 11 |

   The catalog arm won, but serving it indiscriminately still broke too many
   questions.
5. **Learn a simple, inspectable policy.** Evidence was assigned to one of four
   mutually exclusive lexical classes: `formula`, `mapping`, `literal`, or
   `other`. Formula and literal definitions had the strongest development
   benefit, so the policy was frozen as “serve evidence + catalog only for
   `formula` or `literal`; abstain otherwise.” `context_policy.py` contains the
   exact regular expressions—there is no hidden classifier or handpicked ID.
6. **Open the fresh set once.** The frozen policy selected 65 of the 256
   reserved questions. Baseline and treatment were generated three times,
   every predictions file was scored once, and one aggregate `ablate` read
   produced the result above. All 195 answer pairs were defended; none was
   excluded.

This is what contextlift contributed: it rejected bad golds, showed that blanket
context caused regressions, turned those failures into a deployment rule, and
made that rule face new questions.

### The most important qualification: routing is oracle

BIRD supplies both per-question expert evidence and annotated relevant tables.
The treatment used those table annotations to choose catalog pages. Therefore
this experiment isolates the **value of correct context once retrieved**. It
does not measure whether a production router can find that context.

An end-to-end claim needs another arm:

1. route each question without using BIRD's `table_id`;
2. run `score-routing` against the annotated tables;
3. pass the router's own `context_per_question.json` to `generate`;
4. compare that arm on a new reserve.

Until that is done, +19.5 points is a context-value upper bound under correct
table routing, not a production accuracy forecast.

### Reproduce from source

#### 1. Install and download

Python 3.10+, about 1.5 GB of disk, a Gemini API key, and the Gemini optional
extra are required. Keep credentials out of argv because workspace manifests
record argv.

```bash
python -m pip install -e '.[gemini]'
export GEMINI_API_KEY='set-this-in-a-secret-manager'
export ROOT="$PWD/.scratch/bird-enterprise"
python examples/bird/prepare_enterprise.py --out "$ROOT"
```

The preparation command writes `source.json`, 11 SQLite databases, source
descriptions, per-database `golds.csv`, and evidence mappings. It makes no
model call and executes no SQL.

#### 2. Gate and extract schema, at most two SQLite processes at once

Run the following body once per database. `gate` can exit 1 because flagged
golds are a finding, not a broken run. Find the recorded clean output named by
the command; do not guess it from position in `runs/` in automation.

```bash
DB=financial
WS="$ROOT/work/$DB"
contextlift --workspace "$WS" gate --timeout 60 \
  --db "$ROOT/databases/$DB/$DB.sqlite" --golds "$WS/golds.csv" --json
contextlift --workspace "$WS" schema \
  --db "$ROOT/databases/$DB/$DB.sqlite" --out "$WS/schema.txt"
```

Repeat for all database names in `prepare_enterprise.py::DATABASES`, never
running more than two gates side by side. Combine only the recorded
`golds_clean.csv` rows whose `evidence` is nonempty into `eligible.csv`.

#### 3. Freeze the two splits before model generation

```bash
contextlift --workspace "$WS" split --golds "$WS/eligible.csv" \
  --out-curate-csv "$WS/untouched.csv" \
  --out-holdout-csv "$WS/development.csv" \
  --out-curate "$WS/untouched_ids.txt" \
  --out-holdout "$WS/development_ids.txt"

contextlift --workspace "$WS" split --seed 77 --golds "$WS/untouched.csv" \
  --out-curate-csv "$WS/future.csv" \
  --out-holdout-csv "$WS/final_pool.csv" \
  --out-curate "$WS/future_ids.txt" \
  --out-holdout "$WS/final_pool_ids.txt"
```

The second seed is necessary: repeating the same deterministic split over its
curate half correctly assigns every row to curate again.

#### 4. Reproduce candidate selection on development questions

Materialize the broad catalog arm:

```bash
python examples/bird/context_policy.py \
  --source "$ROOT/source/questions.json" \
  --rows "$WS/development.csv" \
  --descriptions "$ROOT/source/descriptions/$DB" \
  --out-questions "$WS/development_eligible.csv" \
  --out-context "$WS/development_context.json" --all

contextlift --workspace "$WS" leak \
  --predictions "$WS/development_eligible.csv" \
  --context-per-question "$WS/development_context.json"
```

For each baseline or treatment, estimate before spending:

```bash
contextlift --workspace "$WS" generate \
  --questions "$WS/development_eligible.csv" --schema "$WS/schema.txt" \
  --out "$WS/arms/dev_base.csv" --backend gemini \
  --model gemini-flash-latest --workers 8 --reps 3 \
  --input-price 0.30 --output-price 2.50 --estimate-only --json

contextlift --workspace "$WS" generate \
  --questions "$WS/development_eligible.csv" --schema "$WS/schema.txt" \
  --context-per-question "$WS/development_context.json" \
  --out "$WS/arms/dev_docs.csv" --backend gemini \
  --model gemini-flash-latest --workers 8 --reps 3 \
  --input-price 0.30 --output-price 2.50 --estimate-only --json
```

Show the estimates to the budget owner, then repeat each command with an
approved `--budget USD --yes` and without `--estimate-only --json`.
`gemini-flash-latest` is a moving alias; a rerun measures the then-current
model and need not reproduce identical SQL. The explicit price overrides are
the rates recorded by this experiment, not a promise of current pricing.

Score every output exactly once:

```bash
contextlift --workspace "$WS" score --timeout 60 \
  --db "$ROOT/databases/$DB/$DB.sqlite" \
  --predictions "$WS/arms/dev_base_rep1.csv" \
  --save-report "$WS/reports/dev_base_rep1.json" --json
```

Repeat for repetitions 2 and 3 and for `dev_docs`, with no more than two
SQLite scorers at once. Merge shards with `merge_reports.py`, which retains
only question IDs defended in all six reports and writes the exclusions:

```bash
python examples/bird/merge_reports.py --root "$ROOT" \
  --report-dir reports --without-prefix dev_base --with-prefix dev_docs \
  --out "$ROOT/development_merged"
```

#### 5. Freeze the learned policy and perform one final reading

The policy below is the one selected above. Do not change its regexes after
looking at final outcomes.

```bash
python examples/bird/context_policy.py \
  --source "$ROOT/source/questions.json" --rows "$WS/final_pool.csv" \
  --descriptions "$ROOT/source/descriptions/$DB" \
  --out-questions "$WS/final_target.csv" \
  --out-context "$WS/final_target_context.json"

contextlift --workspace "$WS" leak --predictions "$WS/final_target.csv" \
  --context-per-question "$WS/final_target_context.json"
```

Estimate, approve, generate three repetitions, and score baseline and
treatment exactly as above, changing the names to `final_base` and
`final_docs`. Then merge all database shards:

```bash
python examples/bird/merge_reports.py --root "$ROOT" \
  --report-dir final_reports --without-prefix base --with-prefix docs \
  --out "$ROOT/final_merged"
```

Finally, read the reserve once—no `--ids`:

```bash
contextlift --workspace "$ROOT/final_verdict" ablate \
  --without-report "$ROOT/final_merged/without_rep1.json" \
  --without-report "$ROOT/final_merged/without_rep2.json" \
  --without-report "$ROOT/final_merged/without_rep3.json" \
  --with-report "$ROOT/final_merged/with_rep1.json" \
  --with-report "$ROOT/final_merged/with_rep2.json" \
  --with-report "$ROOT/final_merged/with_rep3.json" --show --json
```

Expected counts are 65 paired questions, 195 paired answers, 73 baseline
correct, and 111 treatment correct. If source, model, or gate behavior changed,
report the new result instead of forcing these IDs.

### Cost and operational notes

The framework-accounted model spend was approximately **$4.60** across 3,753
calls, including development arms, the final arms, a three-call variability
pilot, and two incomplete baseline attempts discarded after their first budget
ceilings proved too low. The final two arms cost about **$0.57**. Estimates
were shown before every paid stage; actual budgets were ceilings, not targets.

The final result was heterogeneous. Formula 1, student club, finance, and
toxicology improved strongly, while card games and European football declined
at answer level. The aggregate result is statistically strong, but those two
domains and the three stable broken IDs are the next investigation—not facts
to hide behind the average.

</details>

[Return to examples](../README.md) · [Evaluate your own data](../../docs/README.md#2-choose-a-workload)
