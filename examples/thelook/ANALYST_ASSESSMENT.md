# Analyst assessment: theLook e-commerce

## Recommendation for a quick test

Use BigQuery's public `bigquery-public-data.thelook_ecommerce` dataset for a
quick enterprise-shaped trial of contextlift. Seven related operational tables cover
customers, products, inventory, orders, order items, distribution centres and
web events. The questions exercise the kinds of joins, status conventions,
time windows and business metrics that analysts encounter in a commerce
warehouse.

For a genuinely complex trial, use the [BEAVER enterprise warehouse
assessment](../beaver/ANALYST_ASSESSMENT.md) instead. Its pinned release has 97
tables, opaque enterprise identifiers and questions with three-to-seven-table
joins. theLook remains valuable as the inexpensive smoke test before making
that larger commitment.

It is a useful **proxy**, not enterprise data. The data is synthetic, the
estate has only seven tables, and it does not reproduce access controls,
multiple business units, years of schema drift or a real organisation's query
history. Use BEAVER when those properties matter and its larger download and
setup requirements are acceptable.

## What was tested

The committed trial compares one fixed text-to-SQL agent under two conditions:

1. **Control:** the schema context already present in BigQuery. A live check on
   2026-09-09 found no descriptions on any of the 75 columns.
2. **Treatment:** the same agent receives authored table context, vocabulary,
   output conventions, worked examples and eight business definitions such as
   revenue, margin, completed orders, return rate and purchase conversion.

The answer key contains 104 analyst questions. It was split before the context
was evaluated: 74 working questions and 30 held-out questions. Stored-example
matches were excluded rather than counted as text-to-SQL successes, leaving 52
paired working questions. The reserved set was read once by one control and
treatment pair, with all 30 questions scored.

## Result

On the held-out questions, execution accuracy moved from **40% (12/30)** to
**60% (18/30)**: **+20.0 percentage points**, with **6 fixed and 0 broken**.
The exact paired McNemar test gives **p = 0.03125**, so the framework's ruling
is **REAL GAIN**. On the 52 eligible working questions, the measured lift was
+30.8 points (16 fixed and 0 broken).

That makes contextlift useful in this trial in two distinct ways:

- it establishes that written business meaning improved execution accuracy on
  questions reserved from curation, rather than merely improving prompt style;
- its gates expose the conditions behind the number: no pasted SQL, no
  template twins crossing into the holdout, no question leakage, one holdout
  reading, and no stored-example lookup scored as generation.

## Important caveats

- This is one agent sample per arm, not the required three repetitions for a
  production decision. The significant paired result is promising but should
  be rerun with three repetitions before generalising it.
- The same person authored the seed questions and the business definitions.
  That circularity is documented; a customer trial should source questions
  from analyst demand and context from independently maintained documentation.
- A positive result here says that contextlift can detect the value of relevant
  semantic context on this workload. It does not predict the lift on another
  warehouse or prove that automatically generated schema prose will help.

## Reproduce the analysis

From the repository root:

```bash
python examples/thelook/reproduce.py
```

The command uses the committed arm outcomes only. It needs no warehouse,
model, credentials, network access or spend. It recomputes the paired result;
it cannot independently re-execute and validate the SQL decisions recorded on
the original run date.
