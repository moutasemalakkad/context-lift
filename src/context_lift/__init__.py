# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""contextlift — measure whether curated business context makes a data agent
more accurate, and find out what to fix next.

Most accuracy in an enterprise text-to-SQL system comes from someone writing
down what the business means, not from the model. These modules measure whether
that writing-down was worth it, and turn "it is 40% wrong" into "fix the grain
rules on these three tables".

Named for the ISO 5725 sense: contextlift is how close results sit to the
reference value, precision is the spread. Both are reported, always together.

Getting a question set:
- mine.mine_candidates      query history -> candidate golds, execute-gated
- gate.gate                 audit the answer key before trusting any score
- leakage.split_golds       stable curate/holdout split, BEFORE curating
- leakage.check_leakage     does the context contain the questions being scored?

Measuring:
- compare.compare           result-set match between two queries
- harness.evaluate          scoring over (id, question, gold_sql, pred_sql)
- judge.judge               LLM equivalence tiebreaker, one-sided (see §4)
- metrics.*                 Wilson interval, McNemar exact paired test
- routing.evaluate_routing  accuracy of the table-picking stage on its own

Improving:
- ablate.ablate             what was the context worth? (paired)
- ablate.climb              what did each round of enrichment buy? + ladder guard
- triage.triage             what KIND of wrong, so curation has a target

Plumbing:
- executor.Executor         protocol; SQLiteExecutor (core), BigQueryExecutor
- backends.*                echo_backend (tests), gemini_backend (extra)

The order these run in, and why: `contextlift next` proposes the next command
"""

__version__ = "0.1.0"
