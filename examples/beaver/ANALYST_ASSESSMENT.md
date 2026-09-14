# Analyst assessment: BEAVER enterprise warehouse

## Recommendation

Use [BEAVER](https://beaverbench.github.io/) for the complex test. Unlike
theLook, BEAVER was derived from private enterprise warehouses and then
anonymised for public benchmarking. The repository's pinned DW release has 121
questions over 97 tables, with opaque names such as `FCLT_ORG_DLC_KEY` and
questions requiring three to seven table joins. Its downloadable data is 324
MB as CSV and expands to an approximately 6 GB indexed SQLite database.

That makes BEAVER a materially closer proxy for enterprise analysis than the
seven-table synthetic theLook dataset:

- a schema large enough for table selection to be a real problem;
- abbreviated enterprise identifiers rather than friendly teaching names;
- multi-table analytical queries and explicit join-key metadata;
- gold SQL from a different dialect and other answer-key defects that a real
  evaluation must detect rather than silently score as model failures.

The upstream benchmark continues to evolve, but this example deliberately pins
the `beaver-may-2025` metadata so its scripts and recorded measurements do not
change underneath a run.

## What contextlift found

The existing three-day campaign tested the framework on the pinned BEAVER DW
warehouse. The important analyst findings were:

1. `gate` rejected 25 of 121 published questions (21%), primarily because the
   published SQL could not run in the test database. That prevents defects in
   the answer key from being counted as agent errors.
2. Schema-only execution accuracy was **3.8%**. The same model reached 46.2% on
   the smaller KaggleDBQA databases, showing that BEAVER is meaningfully harder
   rather than merely larger.
3. Four rounds of glossary work moved accuracy only to **5.7%**. For a large
   opaque schema, prose definitions alone were not the main constraint.
4. A stronger model reached **8.1%**, and perfect but undeployable oracle schema
   hints reached **13.3%**.
5. A leak-checked bank of verified queries reached **20.2%** on the 596-question
   holdout in the later gated release: **282 fixed, 19 broken, p < 0.0001**.
   `curate-examples` banned 47 template twins before that measurement.

The framework was useful here even though the first context intervention was
weak. It identified that glossary curation was not worth further investment,
showed that worked examples were the high-value lever, caught contaminated
examples, named regressions, and prevented invalid gold SQL from corrupting
the score. This is a more realistic demonstration of the product than choosing
only a small dataset on which context wins easily.

## Fresh local validation (2026-09-12)

The public archive was downloaded and the pinned adapter was run locally rather
than relying only on the older campaign summary. All 97 CSV tables loaded and
191 join-column indexes were created in a 7.95 GB SQLite database.

The current `gate` is stricter than the version used in the original campaign.
It retained **74 of 121** golds and flagged **47**, with overlapping defect
classes: 24 execution failures, 12 results over the executor row cap, 9 text
aggregates, 3 missing question parameters and 2 timeouts. These are findings
about the benchmark, not framework errors. The clean key split into 53 working
questions and 21 reserved questions; at that holdout size, the tool warns that
only a lift of about 22 points or more can be proven.

The local run is recorded through gate, split and schema generation. Paid arm
generation was subsequently completed with the Google AI Studio Developer API.

### End-to-end result

The fresh run used `gemini-flash-latest`, three repetitions per arm, and one
final reading of the 21-question holdout. A seven-rule draft was reviewed and
installed; a verified-query bank was built from the 53 working questions, and
`curate-examples` banned **0 template twins**.

Rules alone did not help on the working set: the paired rate moved from 9.2%
to 8.5% (**-0.7 points**), fixing no questions and breaking one observation of
`dw003`. The framework correctly called that training result **UNDERPOWERED**
rather than an improvement.

On the reserved questions, the complete treatment (rules plus verified-query
examples) moved execution accuracy from **4.8% [1.6%, 13.1%]** to **15.9%
[8.9%, 26.8%]**. Lift was **+11.1 points [−3.0, +22.1]**, with **2 questions
fixed and 0 broken** when each question counted once. The per-question McNemar
test was not significant (`p = 0.5000`), so the ruling is **UNDERPOWERED**. At
this effect size the tool estimates about 58 questions are needed; the current
holdout has 21. The seven fixed answer observations were `dw058` three times,
`dw000` twice, `dw012` once and `dw080` once.

The framework-accounted model spend was approximately **$3.25** at the supplied
token rates, including one partial baseline attempt stopped by its $0.78
ceiling. That stopped run was discarded and all reported arms contain three
complete repetitions.

### Larger fresh-holdout experiment

To determine whether the earlier directional lift survives a better-powered
test, a deterministic seed-77 sample of 1,200 questions was taken from the
current public BEAVER DW release. The current gate retained **266** and flagged
**934**: 729 execution failures, 143 missing parameters, 62 truncated results,
57 text aggregates, 44 whole-row answers, 27 timeouts, 16 zero-row answers and
8 degenerate answers (defect classes overlap). Gating took about 39 minutes on
the local 7.95 GB SQLite database.

The clean key was split before curation into **195 working questions and 71
reserved questions**. On a second split inside the working set, a verified-query
bank moved 58 evaluation questions from **10.5% to 23.3%**, a **+12.8-point
lift [6.2, 24.8]**. It fixed 9 questions and broke none at question level
(`p = 0.0039`), a **REAL GAIN**. This training reading selected the
verified-query arm before the outer holdout was touched.

For the one final holdout reading, `curate-examples` rebuilt the bank from all
195 working questions and banned **3 template twins**. One question was removed
from both arms because its execution was unpaired, and another was excluded in
both arms, leaving **69 paired questions (207 answers)**. Schema-only accuracy
was **5.3% [3.0%, 9.3%]**; verified-query examples reached **15.0% [10.8%,
20.5%]**. The lift was **+9.7 points [2.0, 18.3]**. At question level the bank
fixed 8 and broke 1 (`dw_247`), with `p = 0.0391`: the ruling is **REAL GAIN**,
but it is not a clean win because context broke a question. At answer level it
fixed 25 and broke 5; `dw_2481` and `dw_3806` were unstable rather than stable
question-level regressions.

The conclusion is narrower and more useful than “more context helps”:
general rules did not help, while leak-checked verified queries produced a
statistically significant lift on a fresh holdout. The three banned template
twins and the one broken question remain conditions on that verdict.

## What is reproducible

The metadata, conversion scripts and complete command sequence are committed
here. Downloading and building the warehouse is free; generating new model
arms costs money.

For the current 1,200-question experiment, follow
[`LATEST_RUNBOOK.md`](LATEST_RUNBOOK.md). Its stdlib-only preparation command
downloads the gated query rows, reproduces the seed-77 sample, and leaves every
gate, split, generation, score and comparison as a separately recorded
contextlift command.

```bash
cd examples/beaver
python3 drive_url.py 1Ayy5ZyeP310atvow-rtvaYB40xgpuF11 > /tmp/url.txt
curl -L --retry 3 -o /tmp/dw_csvs.zip "$(cat /tmp/url.txt)"
unzip -q /tmp/dw_csvs.zip -d scratch/
python3 build_db.py scratch/redacted dev_tables.json scratch/beaver_dw.sqlite
python3 add_indexes.py dw_join_keys.json scratch/beaver_dw.sqlite
python3 make_csvs.py scratch/
```

After setup, follow `RUNBOOK.md`. In accordance with the contextlift workflow,
estimate each paid generation before approving a budget, use three repetitions
per arm, and reserve the holdout until the final comparison.

## Limits

- The downloadable warehouse is anonymised benchmark data, not a live estate.
  It does not reproduce permissions, concurrent workloads, schema drift or an
  organisation's current demand distribution.
- The committed 121-question adapter is an older, stable slice of the evolving
  upstream benchmark; results from newer BEAVER releases are not directly
  interchangeable.
- The larger 596-question result is documented from the campaign archive, but
  the raw model outputs are not committed because of dataset terms. Reproduce
  the pinned public slice locally before using the result in a decision.
- Template-expanded benchmark questions remain more similar to one another
  than genuinely novel analyst demand. Even after twin removal, transfer to a
  customer's unseen questions may be smaller.
