# The loop on a real enterprise warehouse (BEAVER)

**Start here:** `make example-beaver` is a dependency-free, 60-second tour of
the recorded decision. Read [QUICKSTART.md](QUICKSTART.md) for what it verifies,
what it cannot verify, and the paths from instant tour to full reproduction.

[BEAVER](https://arxiv.org/abs/2409.02038) is the benchmark built from
private enterprise data warehouses - 121 questions over 97 tables with names
like `FCLT_ORG_DLC_KEY`, 3-7 table joins per question. It is the environment
this framework exists for, and the environment where models collapse: the
same model that scores 46% on small Kaggle databases scores about 4% here.

Unlike `examples/bird`, this example is not offline: the database content is
a 324 MB download and the model arms cost real money. The committed pieces
are the MIT-licensed metadata (question/gold/schema JSON, from
[beaver-may-2025](https://github.com/peterbaile/beaver-may-2025)) and the
scripts; RUNBOOK.md is the command sequence with costs marked.

## Setup (one time, ~10 minutes, free)

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

Then follow [RUNBOOK.md](RUNBOOK.md) - gate, split, generate, score, ablate,
attribute - each step one command with its cost marked.

To reproduce the larger seed-77 experiment and its statistically significant
fresh-holdout result, follow [LATEST_RUNBOOK.md](LATEST_RUNBOOK.md). It downloads
the current gated BEAVER questions with a read-only `HF_TOKEN` and keeps every
framework stage separately recorded.

## What to expect

Measured results from running this loop (the full campaign is
[docs/CASE_STUDY.md](../../docs/CASE_STUDY.md)):

- `gate` flags about a fifth of the published golds as unable to run
  (MySQL dialect); scoring against them would corrupt every number.
- The schema-only baseline lands in single digits. That is BEAVER's own
  published finding, not a setup mistake.
- A curated glossary alone moves little. Glossary plus a leak-checked
  worked-example bank (`curate-examples`) is the lever that multiplies
  accuracy - and `leak` matters, because this dataset contains template
  twins that would otherwise put answers inside the context.
