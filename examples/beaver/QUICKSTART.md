# BEAVER quickstart: see the decision before rebuilding the experiment

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
call Gemini. The output ends with the exact next path for each kind of reader:

- **Want to audit a real run without credentials?** Run `make example-thelook`.
  It recomputes a held-out verdict from committed answer-level rows.
- **Want to reproduce BEAVER end to end?** Follow
  [`LATEST_RUNBOOK.md`](LATEST_RUNBOOK.md). It downloads the gated dataset,
  rebuilds SQLite, estimates every paid arm before spending, and preserves the
  outer holdout for one final reading.
- **Want to evaluate your own warehouse?** Run
  `contextlift --workspace /absolute/path/to/workspace next` and follow the one
  command it proposes.

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

