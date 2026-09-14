# Reproduce the larger BEAVER experiment

This is the shortest auditable route to the fresh-holdout result in
`ANALYST_ASSESSMENT.md`. It deliberately does not hide the experiment in one
shell script: every framework stage remains one recorded `contextlift` command,
and paid stages stop for an estimate and an explicit budget.

## 1. One-time setup

Build `scratch/beaver_dw.sqlite` by following `README.md` first. Install the
Gemini extra, accept the terms for both `beaverbench/beaver-query` and
`beaverbench/beaver-table` on Hugging Face, and export read-only credentials:

```bash
uv pip install -e '.[gemini]'
export HF_TOKEN='your-read-only-hugging-face-token'
export GEMINI_API_KEY='your-google-ai-studio-key'
```

Never put either value on a command line passed to contextlift: argv is recorded.
Prepare the exact seed-77 sample used in the assessment:

```bash
cd /path/to/contextlift
python examples/beaver/prepare_latest.py \
  --sample 1200 --seed 77 --out examples/beaver/latest/questions_1200.csv
mkdir -p examples/beaver/latest-work/reports
T='python -m context_lift --workspace /absolute/path/to/contextlift/examples/beaver/latest-work'
DB='/absolute/path/to/contextlift/examples/beaver/scratch/beaver_dw.sqlite'
W='/absolute/path/to/contextlift/examples/beaver/latest-work'
```

Use absolute paths in `T`, `DB` and `W`. The recorded run took about 39 minutes
to gate on its machine; model prices and availability can change.

## 2. Gate, split and schema

```bash
$T gate --timeout 60 --db "$DB" --golds "$W/../latest/questions_1200.csv" --json
```

`gate` exits non-zero when it finds defects; that is a finding. Read the path
to `golds_clean.csv` that it prints and substitute its run id below:

```bash
$T split --golds "$W/runs/RUN_ID-gate/outputs/golds_clean.csv" \
  --out-curate-csv "$W/curate.csv" --out-holdout-csv "$W/holdout.csv" \
  --out-curate "$W/curate_ids.txt" --out-holdout "$W/holdout_ids.txt"
$T schema --db "$DB" --out "$W/schema.txt"
```

The seed-77 run retained 266 questions and split them 195/71. If your gate
counts differ, stop and report the difference rather than forcing the old ids.

## 3. Baseline on the working questions

Run the estimate first and show its `estimated_cost_usd` to the person paying:

```bash
$T generate --questions "$W/curate.csv" --schema "$W/schema.txt" \
  --out "$W/base.csv" --backend gemini --model gemini-flash-latest \
  --workers 8 --reps 3 --input-price 0.30 --output-price 2.50 \
  --estimate-only --json
```

After approval, replace `APPROVED_USD` with the approved ceiling:

```bash
$T generate --questions "$W/curate.csv" --schema "$W/schema.txt" \
  --out "$W/base.csv" --backend gemini --model gemini-flash-latest \
  --workers 8 --reps 3 --input-price 0.30 --output-price 2.50 \
  --budget APPROVED_USD --yes
for N in 1 2 3; do
  $T score --timeout 60 --row-cap 100000 --db "$DB" \
    --predictions "$W/base_rep${N}.csv" \
    --save-report "$W/reports/base_rep${N}.json" --json
done
```

## 4. Select the verified-query arm without reading the outer holdout

```bash
$T split --golds "$W/curate.csv" --seed 99 \
  --out-curate-csv "$W/iter_pool.csv" --out-holdout-csv "$W/iter_eval.csv" \
  --out-curate "$W/iter_pool_ids.txt" --out-holdout "$W/iter_eval_ids.txt"
$T curate-examples --pool "$W/iter_pool.csv" --targets "$W/iter_eval.csv" \
  --out "$W/iter_bank.json"
$T generate --questions "$W/iter_eval.csv" --schema "$W/schema.txt" \
  --examples "$W/iter_bank.json" --out "$W/iter_examples.csv" \
  --backend gemini --model gemini-flash-latest --workers 8 --reps 3 \
  --input-price 0.30 --output-price 2.50 --estimate-only --json
```

Approve that printed estimate, rerun the same `generate` command with
`--budget APPROVED_USD --yes` instead of `--estimate-only --json`, then score
its three files exactly as above. Compare the saved baseline and treatment
reports with `ablate --ids "$W/iter_eval_ids.txt" --show`. This training
reading produced +12.8 points and selected the candidate.

## 5. One final holdout reading

Build the final bank before generating either reserved arm:

```bash
$T curate-examples --pool "$W/curate.csv" --targets "$W/holdout.csv" \
  --out "$W/holdout_bank.json"
```

Repeat the estimate-then-budget `generate` sequence twice over
`$W/holdout.csv`: once to `holdout_base.csv` without `--examples`, and once to
`holdout_examples.csv` with `--examples "$W/holdout_bank.json"`. Use three
repetitions for both. Score each of the six files once with `--timeout 60
--row-cap 100000 --save-report`, then make one final comparison:

```bash
$T ablate --timeout 60 --db "$DB" --show \
  --without-report "$W/reports/holdout_base_rep1.json" \
  --without-report "$W/reports/holdout_base_rep2.json" \
  --without-report "$W/reports/holdout_base_rep3.json" \
  --with-report "$W/reports/holdout_examples_rep1.json" \
  --with-report "$W/reports/holdout_examples_rep2.json" \
  --with-report "$W/reports/holdout_examples_rep3.json"
```

Do not rerun or curate from that comparison. Report the interval, fixed and
broken ids, p-value, and every caveat it prints. The recorded run excluded one
question symmetrically after an unpaired execution and one in both arms; it
then measured +9.7 points `[+2.0, +18.3]`, fixed 8, broke 1, and ruled
**REAL GAIN** (`p = 0.0391`).

