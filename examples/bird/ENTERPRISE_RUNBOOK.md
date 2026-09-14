# BIRD enterprise study: a +19.5-point targeted context lift

This is the long-form, reproducible account of the experiment behind:

```bash
make example-bird-enterprise
```

The short command validates and renders a committed receipt. This page explains
how the receipt was earned, where the intervention worked, and which claim it
**does not** support.

## Result first

On a fresh, never-generated 65-question cohort, selected by a policy learned on
development questions:

| arm | correct answers | execution accuracy |
|---|---:|---:|
| schema only | 73 / 195 | 37.4% |
| expert evidence + relevant catalog pages | 111 / 195 | 56.9% |
| **paired lift** | **+38 answers** | **+19.5 points** |

The paired 95% interval was **+12.1 to +26.9 points**. The question-clustered
McNemar result was **p = 0.0044**, so contextlift ruled **REAL GAIN**. At stable
question level, context fixed 15 and broke 3: `bird_0521`, `bird_1036`, and
`bird_1188`. Eleven more questions were unstable across three repetitions.
This is a large gain, but it is not a clean win.

The policy covered **65 of 256 reserved questions (25.4%)**. It should be read
as “serve this context when an explicit formula or literal definition was
retrieved,” not “accuracy rose 19.5 points on every BIRD request.”

## What makes the benchmark enterprise-shaped

[BIRD](https://bird-bench.github.io/) contains human-crowdsourced questions,
expert-authored external knowledge, dirty values, and professional-domain
databases. The full benchmark reports 12,751 pairs over 95 databases and 37
domains. This study uses its public validation estate: 1,534 questions over 11
SQLite databases, including finance, card transactions, education, a codebase
community, sport, and clinical prediction.

The source is pinned, rather than downloaded from a moving `main` branch:

* questions: `target-benchmark/bird-queries-validation` at
  `d108575b93498f02410b579a89e38488eeb7180d`;
* databases and descriptions: `target-benchmark/bird-corpus-validation` at
  `5ec3b6bb52e85d4ff2f7ad97b373ef8c0d8a096f`.

BIRD's upstream repository labels the dataset CC BY-SA 4.0. The large source
files are downloaded for the run and are not redistributed here.

## The design, including the failed ideas

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

## The most important qualification: routing is oracle

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

## Reproduce from source

### 1. Install and download

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

### 2. Gate and extract schema, at most two SQLite processes at once

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

### 3. Freeze the two splits before model generation

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

### 4. Reproduce candidate selection on development questions

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

### 5. Freeze the learned policy and perform one final reading

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

## Cost and operational notes

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
