# BEAVER: the enterprise-warehouse example

The shortest way to understand what contextlift bought us is one command from a
fresh checkout:

```bash
make example-beaver
```

It needs only Python 3.10+: **no database, model key, download, network, or
package install**. In a few seconds it turns the recorded experiment summary
into the decision an analyst had to make:

```text
BEAVER / verified-query context

1  TRUST THE TEST
   gate kept 266/1,200 candidate golds and flagged 934
   ...
2  SPEND CURATION WHERE IT WORKS
   broad rules: -0.7 points (not selected)
   verified examples: +12.8 points on the working set (selected)
3  CASH THE CLAIM ON UNSEEN QUESTIONS
   5.3%  ->  15.0%   (+9.7 points, 95% CI +2.0 to +18.3)
   REAL GAIN (McNemar p=0.0391): 8 fixed, 1 broke
```

This is deliberately a **receipt reader**, not a benchmark runner. It checks
the arithmetic and guardrails in the committed summary, then explains what
each stage changed in the decision. It does not pretend to re-execute SQL or
call Gemini. See the reproduction section below to run a new experiment.

## What the one command demonstrates

### 1. A score is only as trustworthy as its answer key

Of 1,200 sampled benchmark questions, `gate` retained 266 and flagged 934.
Those flags included SQL execution failures, missing parameters, truncated
results, unsafe text aggregates, whole-row answers, timeouts, empty answers,
and degenerate answers. Without that stage, benchmark defects would be
reported as agent failures.

### 2. “Add more context” is not a strategy

Broad rules moved the working score **-0.7 points**. A leak-checked bank of
verified queries moved an inner working-set evaluation **+12.8 points** and
fixed nine questions without breaking one. Contextlift therefore rejected one
curation investment and selected the other *before* opening the outer
holdout.

### 3. Lift and regressions belong in the same sentence

On the fresh holdout, verified examples raised execution accuracy from **5.3%
to 15.0%**: **+9.7 points**, with a paired 95% interval of **+2.0 to +18.3**.
The question-level test fixed eight and broke one (`dw_247`), with McNemar
`p = 0.0391`. The ruling is **REAL GAIN**, but not a clean win. The quickstart
prints both facts rather than turning a positive average into a victory lap.

### 4. Leakage is measured, not promised

Before the final arm, `curate-examples` banned three template twins. That is a
condition on transfer to truly novel analyst questions, so it stays beside
the result.

## Trust boundary

`result.json` is a compact, non-sensitive summary copied from the recorded
workspace. `quickstart.py` verifies its count and percentage relationships and
refuses malformed or internally inconsistent data. It cannot prove the source
SQL was scored correctly; that requires the full runbook and gated warehouse.
This split is intentional: the tour is instant, while the reproducible claim
remains auditable rather than magically “recreated” from summary numbers.


## Full reproduction

The local tour above needs only Python. Running new model arms requires the
warehouse download, dataset permission, and a paid model backend. The
[recorded assessment](ANALYST_ASSESSMENT.md) retains both historical trials;
use this sequence for the larger seed-77 study.

<details>
<summary>Download, build, and reproduce the seed-77 experiment</summary>

From the repository, install and activate the environment as in
[guide stage 1](../../docs/README.md#1-install-and-try-the-example), then run
these database-build commands from `examples/beaver/`. The database is about
6 GB; downloaded CSVs are about 324 MB.

```bash
# 1. CSVs from the BEAVER Google Drive folder (324 MB)
python3 drive_url.py 1Ayy5ZyeP310atvow-rtvaYB40xgpuF11 > /tmp/url.txt
curl -L --retry 3 -o /tmp/dw_csvs.zip "$(cat /tmp/url.txt)"
unzip -q /tmp/dw_csvs.zip -d scratch/

# 2. Load into SQLite (~6 GB) and index the join keys
python3 build_db.py scratch/redacted dev_tables.json scratch/beaver_dw.sqlite
python3 add_indexes.py dw_join_keys.json scratch/beaver_dw.sqlite

# 3. Reshape the metadata into the CSVs the commands read
python3 make_csvs.py scratch/
```

Return to the repository root before installing extras and generating arms.

### 1. One-time setup

Build the database using the download/build commands just above. Install the
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
DB='/absolute/path/to/contextlift/examples/beaver/scratch/beaver_dw.sqlite'
W='/absolute/path/to/beaver-evaluation'
export CONTEXTLIFT_WORKSPACE="$W"
mkdir -p "$W/reports"
```

Use absolute paths in `DB` and `W`. Keep the workspace outside the repository. The recorded run took about 39 minutes
to gate on its machine; model prices and availability can change.

### 2. Gate, split and schema

```bash
contextlift gate --timeout 60 --db "$DB" --golds examples/beaver/latest/questions_1200.csv --json
```

`gate` exits non-zero when it finds defects; that is a finding. Read the path
to `golds_clean.csv` that it prints and substitute its run id below:

```bash
contextlift split --golds "$W/runs/RUN_ID-gate/outputs/golds_clean.csv" \
  --out-curate-csv "$W/curate.csv" --out-holdout-csv "$W/holdout.csv" \
  --out-curate "$W/curate_ids.txt" --out-holdout "$W/holdout_ids.txt"
contextlift schema --db "$DB" --out "$W/schema.txt"
```

The seed-77 run retained 266 questions and split them 195/71. If your gate
counts differ, stop and report the difference rather than forcing the old ids.

### 3. Baseline on the working questions

Run the estimate first and show its `estimated_cost_usd` to the person paying:

```bash
contextlift generate --questions "$W/curate.csv" --schema "$W/schema.txt" \
  --out "$W/base.csv" --backend gemini --model gemini-flash-latest \
  --workers 8 --reps 3 --input-price 0.30 --output-price 2.50 \
  --estimate-only --json
```

After approval, replace `APPROVED_USD` with the approved ceiling:

```bash
contextlift generate --questions "$W/curate.csv" --schema "$W/schema.txt" \
  --out "$W/base.csv" --backend gemini --model gemini-flash-latest \
  --workers 8 --reps 3 --input-price 0.30 --output-price 2.50 \
  --budget APPROVED_USD --yes
for N in 1 2 3; do
  contextlift score --timeout 60 --row-cap 100000 --db "$DB" \
    --predictions "$W/base_rep${N}.csv" \
    --save-report "$W/reports/base_rep${N}.json" --json
done
```

### 4. Select the verified-query arm without reading the outer holdout

```bash
contextlift split --golds "$W/curate.csv" --seed 99 \
  --out-curate-csv "$W/iter_pool.csv" --out-holdout-csv "$W/iter_eval.csv" \
  --out-curate "$W/iter_pool_ids.txt" --out-holdout "$W/iter_eval_ids.txt"
contextlift curate-examples --pool "$W/iter_pool.csv" --targets "$W/iter_eval.csv" \
  --out "$W/iter_bank.json"
contextlift generate --questions "$W/iter_eval.csv" --schema "$W/schema.txt" \
  --examples "$W/iter_bank.json" --out "$W/iter_examples.csv" \
  --backend gemini --model gemini-flash-latest --workers 8 --reps 3 \
  --input-price 0.30 --output-price 2.50 --estimate-only --json
```

Approve that printed estimate, rerun the same `generate` command with
`--budget APPROVED_USD --yes` instead of `--estimate-only --json`, then score
its three files exactly as above. Compare the saved baseline and treatment
reports with `ablate --ids "$W/iter_eval_ids.txt" --show`. This training
reading produced +12.8 points and selected the candidate.

### 5. One final holdout reading

Build the final bank before generating either reserved arm:

```bash
contextlift curate-examples --pool "$W/curate.csv" --targets "$W/holdout.csv" \
  --out "$W/holdout_bank.json"
```

Repeat the estimate-then-budget `generate` sequence twice over
`$W/holdout.csv`: once to `holdout_base.csv` without `--examples`, and once to
`holdout_examples.csv` with `--examples "$W/holdout_bank.json"`. Use three
repetitions for both. Score each of the six files once with `--timeout 60
--row-cap 100000 --save-report`, then make one final comparison:

```bash
contextlift ablate --timeout 60 --db "$DB" --show \
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


</details>

[Return to examples](../README.md) · [Evaluate your own data](../../docs/README.md)
