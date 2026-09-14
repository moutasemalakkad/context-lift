# The thelook spec repo

The written context whose value the worked example measures. A person authors
these files; the framework only reads them.

    rules/       the business rules, as one system instruction
    vocabulary/  canonical terms and the other words people use for them
    schema/      what the estate already had. It had nothing.
    authored/    what a curator wrote. Treatment arm only.
    examples/    worked examples, filled in by a round from curate golds

## Why schema/ is empty and still exists

Checked live on 2026-09-09: 0 of 75 columns across the seven thelook tables
carry a description in BigQuery. That is what makes this dataset a clean
control. Arm 0d carries what the estate already had, and here it carries
nothing, so the whole difference between the two arms is authored context.

The files exist and are empty on purpose. An absent file means nobody looked;
an empty one means somebody looked and there was nothing.

## Where the rules came from, and the part to be honest about

Every rule in `rules/00-definitions.md` was written in brackets on a query's
comment in `queries.sql`, and `make_golds.py` took it out of the question. A
definition inside a question is context the bare arm gets for free, so leaving
it there would make a lift impossible to show.

The circularity is real and worth stating rather than hiding: the same person
wrote the seed queries, their comments, and this spec. On a customer estate the
questions come from their analysts and the rules from their own documentation,
which is not the same thing. The Spider 2.0 Google Analytics companion exists
as the independence check, with questions and context nobody here wrote.

What this example does show honestly: whether writing these rules down moves a
fixed agent on questions that no longer contain them.
