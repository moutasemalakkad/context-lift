# The iteration loop, one command per step

Run from this directory after README.md's setup. `scratch/` holds everything
generated; nothing in it commits. Costs are for gemini-2.5-flash on Vertex;
set your own project id:

```bash
T="contextlift --workspace ."       # every run recorded under ./runs/, see README "Recording runs"
PROJECT=your-gcp-project-id
```

## 1. Audit the answer key (free, run before trusting anything)

```bash
$T gate --timeout 60 --db scratch/beaver_dw.sqlite \
  --golds scratch/questions.csv --clean-out scratch/questions_clean.csv
```

Flagged golds are findings about the benchmark, not errors in your setup.
Only the clean ones can grade anything. `gate` reads `id` and `gold_sql`
and passes every other column through, so gating `questions.csv` keeps the
question text next to each clean gold for the steps below.

## 2. Split, then leave the holdout alone (free)

```bash
$T split --golds scratch/questions_clean.csv \
  --out-curate scratch/curate_ids.txt --out-holdout scratch/holdout_ids.txt \
  --out-curate-csv scratch/curate.csv --out-holdout-csv scratch/holdout.csv
```

The two CSVs keep every column of the input, so give `split` the
gate-cleaned questions file (id, question, gold_sql) and the arms below
read `curate.csv` directly.

## 3. Baseline arm - BEFORE any curation (paid: ~$1 per repetition)

```bash
$T generate --questions scratch/curate.csv --schema scratch/schema.txt \
  --out scratch/base.csv --backend gemini --project $PROJECT --workers 8
$T score --timeout 60 --db scratch/beaver_dw.sqlite \
  --predictions scratch/base.csv --save-report scratch/base_report.json
```

## 4. What kind of wrong? (free)

```bash
$T triage --report scratch/base_report.json --show
```

Step 3 already executed every query; triage reads its saved report and
runs no SQL.

The biggest bucket is your curation target. Write context rules for it in
`scratch/rules.md`, one per line: `- "business phrase" -> TABLE.COLUMN`.
This is the human step; no command does it.

## 5. The strongest lever: a leak-checked example bank (free)

```bash
$T curate-examples --pool scratch/curate.csv \
  --targets scratch/eval.csv --out scratch/bank.json
# eval.csv = the questions you will score in step 6 (a question is never its
# own example; pool == targets produces an empty bank). Then check BOTH
# context pieces against whatever you will score:
$T leak --context scratch/rules.md --predictions scratch/eval.csv
```

Note how many template twins it banned - a high count means less transfer
to genuinely new questions.

## 6. The context arm (paid: ~$2-6 per repetition, context size dependent)

```bash
$T generate --questions scratch/curate.csv --schema scratch/schema.txt \
  --out scratch/arm.csv --backend gemini --project $PROJECT --workers 8 \
  --context scratch/rules.md --examples scratch/bank.json
$T score --timeout 60 --db scratch/beaver_dw.sqlite \
  --predictions scratch/arm.csv --save-report scratch/arm_report.json
```

The ONLY difference from step 3 is `--context`/`--examples`. That identity
is what makes step 7's paired test valid, and `generate` enforces it.

## 7. The verdict (instant, from the saved reports)

```bash
$T ablate --db scratch/beaver_dw.sqlite --show \
  --without-report scratch/base_report.json \
  --with-report scratch/arm_report.json
```

Read three things: the lift, `context broke (N)` with question ids, and the
p-value. A lift that broke questions is not a clean win.

## 8. When something broke: name the guilty rule (free)

```bash
$T attribute --timeout 60 --db scratch/beaver_dw.sqlite \
  --without-context scratch/base.csv --with-context scratch/arm.csv \
  --context scratch/rules.md
```

Edit that one rule, rerun steps 6-7. That is one iteration. Every few
rounds, run `climb` with each round's CSV to see the guarded progression -
its ladder only moves on gains above the printed noise floor.

## Rules the campaign learned the hard way

- Always `--timeout 60`: one runaway (but valid, read-only) query otherwise
  hangs a run indefinitely.
- Never more than 2 score commands at once: SQLite sorts spill temp files,
  and enough parallel scorers can fill the disk.
- One run is a sample: `generate --reps 3` and pool by repeating the report
  flags in ablate before believing any number.
- Score the holdout once, at the end. Every earlier look turns it into
  training data.
