<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com> -->

# theLook: replay a recorded comparison

From the repository root:

```bash
make example-thelook
```

Requires Python 3.10+ only. The command recomputes the second-round verdict
from [committed answer-level rows](committed/run-2026-09-10.csv); it does not
execute warehouse SQL or call a model.

## What it shows

On 30 reserved questions, accuracy moved from 40% to 60%: +20.0 points,
6 fixed and 0 broken, p = 0.03125. Of 74 working questions, 52 remained after
stored-example exclusions; their lift was +30.8 points.

These are recorded results from one model sample per arm on a synthetic,
seven-table commerce dataset. The same person wrote the questions and context,
and the wider historical sequence read the holdout twice. The
[research record](../../docs/research/README.md#thelook-the-loop-closing-2026-09-10)
keeps the earlier inconclusive reading beside this result. This is not a
protocol for reusing a consumed holdout.

## Inspect the artifacts

- [Question key](golds.csv) and [reproduction code](reproduce.py).
- [Evaluated context spec](spec/README.md).
- [LookML extraction arm](lookml-arm/README.md), including its distinct result.

For a new experiment on BigQuery's public
`bigquery-public-data.thelook_ecommerce`, follow the [main guide](../../docs/README.md)
and scope the database explicitly. Use a newly reviewed question set and reserve
one final comparison. Access policies, data drift, and larger estates require
their own validation; this synthetic example does not establish their outcomes.

[Return to examples](../README.md).
