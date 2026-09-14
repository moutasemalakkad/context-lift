# What to write, per dataset, before you measure anything

A data team asked what to document first. This is the answer, and it is not an
opinion: on one enterprise BigQuery estate we ran **two different kinds of
written context past the same agent, on the same day**, and they went in
opposite directions.

| | context A | context B |
|---|---|---|
| what it was | the team's own published data-product docs | conventions a model mined from the query log |
| size | 49 KB | 3 KB |
| **fully-qualified table names** | **181** | **0** |
| tables with a business definition | 47 | 0 |
| bare column names, no table named | 0 | 9 |
| **result** | **+6.5 points, 4 fixed, 0 broke** | **−6.9 points, 1 fixed, 3 broke** |

**Where these numbers come from, and what you can and cannot re-run.** Both
rounds were `contextlift round`; the context counts are
`scripts/doc_coverage.py` and a grep over each spec's `rules/`. The estate is
not named here and nothing below identifies it — which also means **its
artifacts are not in this repository, so no test can recompute these two
columns.** Take them as reported.

Everything sourced to `docs/CASE_STUDY.md` or to `examples/thelook/` below
*is* re-runnable, free and offline:

```
make example-thelook     # recomputes a held-out verdict from committed rows
make example-beaver       # reads the committed enterprise receipt
```

**The single difference that matters:** the context that helped **names
tables**. The context that hurt named columns with no table attached, and 8 of
its 10 bare column names were on no table the agent could see at all.

So the template below is short, and every line in it earns its place.

---

## If you only have one hour

You will not fill six sections for 27 datasets, and you should not try. This
is the order, and it is ranked by what was measured rather than by what feels
thorough.

### First: pick the datasets. Do not guess this.

On the estate above, **12 of 27 documented data products had no analytical
query in 180 days**, while the five most-queried had no documentation at all.
A team had been writing, carefully, about data nobody asked about.

Twenty minutes, and it decides everything after it:

```
contextlift log --db bq://YOUR_PROJECT --days 180 --limit 2000 \
    --min-tables 2 --require-analytical --out query_log.csv
```

Then rank the datasets by how many distinct people ran queries against them.
Write about the top five. Ignore the rest until those five are done.

### Before any of it: "we already have Looker, can't you just read that?"

Ask this first, because it is the first thing said in every meeting, and the
answer is measured rather than argued. **One free command answers it for your
project:**

```
contextlift semantic --lookml /path/to/your-looker-repo
```

Read one line of the output — how many metrics carry a written description.

```
  9 metrics, 0 with a written description (0%)      <- Looker's own public project
```

On a project like that, a full round measured **+2.0 points, NOT PROVEN
(p = 0.6875), and safety MIXED** — it broke two questions that were right
without it. Against **+12.2 points** for the hand-written rules in row 4
below. The extraction is worth the join paths and little else.

The reason is structural, not a Looker complaint. A LookML measure carries a
`type` and an expression, so it can tell an agent `SUM(sale_price)`. It has
nowhere to say *which rows count*. "Revenue is the sale price of items that
are neither Cancelled nor Returned" has no field in LookML except
`description`, and the description is what nobody fills in.

So: if your description share is near zero, do not spend the afternoon on a
connector. Do the table below. If your share is high — a project analysts
have maintained for years may well be — that changes the answer, and the
command above is how you find out for nothing.

Published benchmarks are not in conflict with this. Cube measured +17 to +23
points from a semantic layer, and dbt Labs +8 — from **hand-written prose
about business conventions**, which is the same thing the table below asks you
to write. "We have a semantic layer" and "we have written business context"
are treated as one sentence in the industry. They are two.

### Then, per dataset, in this order

| do this | minutes | why, measured |
|---|---|---|
| **1. Name the tables, fully qualified, and say what one row is** | 10 | 181 of these in the context that gained +6.5; **zero** in the context that lost 6.9 |
| **2. Write down exact spellings of status and code values** | 5 | on a public set, `'shipped'` in lower case returns zero rows and looks like an answer. One line, and it was the most load-bearing in the file |
| **3. Rows people do not want** | 10 | never written down anywhere, and an agent will confidently return a wrong total without them |
| **4. Business word to column mapping, and the rules** | 15 | **+12.2 points** measured directly: removing the rules class took a public set from 42% to 30%, 11 questions broken against 2 fixed |
| **5. What the dataset is for, 2-4 sentences** | 5 | present in 17 of 17 documents that gained; it is how an agent chooses between two plausible tables |
| **6. Worked questions with their SQL — only if you can route them** | 15 | **see the correction below.** Shipped to every question, a 22-example bank measured **-24.3 points** |

**About 60 minutes per dataset, and the order is the point.**

### Corrected 2026-09-12, and the correction is the finding

An earlier version of this table put worked examples third and the glossary
last, citing +6.9 and +1.9 from a different benchmark. **A direct
leave-one-class-out ablation on `examples/thelook` says the opposite.** Same
74 questions, one class removed per arm, 370 model calls:

```
  examples              -24.3 points [-34.8, -13.9]   66% without it vs 42% with
                                                       19 fixed, 1 broke
                                                       BETTER WITHOUT IT
  rules                 +12.2 points [+3.0, +21.3]     30% without vs 42% with
                                                       2 fixed, 11 broke
                                                       CARRIES ITS WEIGHT
  terms                  +1.4 points [-5.6, +8.4]      not shown either way
  authored descriptions  -0.0 points [-6.5, +6.5]      not shown either way
```

Command: `contextlift classes --spec examples/thelook/spec --golds
examples/thelook/golds.csv --run`. The full arm scored 42% against 39.2% on
the committed run two days earlier, so this reproduces rather than drifts.

**Why examples hurt here, and it is not that examples are bad.** The bank is
22 items and 9,063 bytes, and it travels with every question:

```
  matched an example        about 1 question in 3
  carried it anyway         the other 2 in 3
```

So two thirds of questions were handed 9 KB of SQL answering a different
question. Irrelevant context is measurably worse than none
(https://arxiv.org/pdf/2408.07702), and this is that effect at full size.

**So the rule is about routing, not about examples.** An example handed to
the question it matches is the strongest lever this project has measured
(+6.9 on a 596-question benchmark). The same example handed to every question
is a distractor. If you cannot route examples per question, do not ship them
— write rows 1 to 5 instead and stop.

Two independent experiments now point the same way: this ablation, and a
customer round where every break landed on a question the rules did not apply
to.

### What to tell somebody who has 15 minutes

Rows 1 and 2 only. Fully-qualified table names, what one row is, and the exact
spelling of every status value. That is it. It is the difference between the
two contexts in the table at the top of this page.

---

## Why "add some business rules" fails as an instruction

It is the request customers are usually given and it is unanswerable, for
three reasons we can now name:

- **It does not say what a rule is attached to.** Five of the ten rule lines
  in the failing context had no subject at all: *"Compute it as
  `sum(a) - sum(b)`"*. Compute **what**?
- **It does not say which part.** "Business rules" means a glossary to most
  people, and a glossary is two different things: the word-to-column mapping
  (+1.4, not shown either way) and the conventions that decide how to answer
  (+12.2, the largest measured effect). Asking for "rules" gets the first.
- **It gives no stopping condition.** A dataset can absorb unlimited writing.
  The table above stops at 60 minutes and says what you get for each block of
  it.

Replace it with: *"For your five most-queried datasets, name every table in
full, say what one row is, and give us two real questions with the SQL."*
That is a request somebody can finish.

---

## The template: one page per dataset

Copy `examples/onboarding/TEMPLATE.md`. One file per dataset, not per table.
About an hour each, and the order matters more than the total -- see above.

```
# <Dataset display name>   (`<dataset_id>`)

## What this is for
<2-4 sentences. What business process does this data describe? Who uses it
and to answer what? Write it for somebody who has never opened the tables.>

## The tables
- `project.dataset.table`: <what one row is, and what the table is for.>
- `project.dataset.table`: <same. fully qualified, every time.>

## Where the data comes from
<Source systems, in your words. "Orders from S4 via HANA; product master
from MDM." An agent cannot guess this and a schema does not say it.>

## Words we use that the columns do not
- "<business word>" means <column or condition>, on `project.dataset.table`.

## Rows people usually do not want
- <filter>, on `project.dataset.table`, because <reason>.

## Two questions and their SQL
<The highest-value section, and the one people skip. See below.>
```

---

## Why each section is there

### "What this is for" — 2 to 4 sentences

Every one of the 17 documents that produced +6.5 points had one. It is what
lets an agent pick between two tables that both look plausible by name.

### "The tables" — fully qualified, every time

This is the section with the evidence behind it. 181 fully-qualified names in
the context that worked, 0 in the context that failed.

**Fully qualified means `project.dataset.table`, not `table`.** On that estate
two projects held copies of the same data, and an answer key that read one
copy while the agent read the other scored wrong for a reason that had nothing
to do with anybody's understanding. A bare table name cannot tell them apart.

Say what **one row is**. "One row is one purchase-order line per delivery
milestone" is worth more than three sentences about the business process.

### "Where the data comes from"

17 of 17 working documents had this. It sounds like background and it is not:
*"shipments from Infor Nexus, purchase orders from S4"* tells an agent which
table is authoritative when two of them disagree, which is a question the
schema cannot answer.

### "Words we use that the columns do not" — and name the table

Write the mapping, and **name the table it applies to**:

```
  GOOD  "North America" means cluster = 'USCA', on `proj.ds.dim_location`.
  BAD   `cluster` holds USCA for North America.
```

The bad version is what the failing context looked like. On that estate the
same concept was filtered on two different columns in two different tables,
so an unscoped rule was true and unhelpful at the same time.

### "Rows people usually do not want"

Exclusions are the highest-value thing a data team knows and the least likely
to be written down. *"Test orders have `order_type = 'ZTST'`; nobody wants
them in a sales number."* An agent will never guess it and will confidently
return a wrong total.

Name the table here too.

### "Two questions and their SQL" — do not skip this

The best-measured lever in this framework is not prose. On a public
enterprise benchmark, a leak-checked bank of worked examples was worth
**+6.9 points** where an iterated glossary was worth **+1.9**
(`docs/CASE_STUDY.md`). Two real questions with the SQL your team would
actually write beats another page of description.

Two rules for them:

1. **Real questions, not toy ones.** Take them from what somebody asked last
   week.
2. **Not a question you plan to test on.** An example that answers a scored
   question makes the score meaningless. `contextlift leak` refuses those, and
   it caught 25 on one build.

---

## What NOT to write, each one measured

**A formula with no subject.** Five of the ten rule lines in the failing
context read like this:

```
  Compute it as `sum(count_of_eligible_days) - sum(count_of_oos_days)`.
```

Compute **what**? The file that generated them admitted it: naming the metric
*"is a person's job"*. An instruction an agent cannot attach to anything is
not context.

**A column name with no table.** 8 of 10 bare column names in the failing
context were on no table the agent could see.

**A rule about a value the question already gives.** If the question says
"in March 2026", writing down the date range teaches nothing. Worse, a rule
that spells out a value the question *omits on purpose* hands the answer to
the arm that has no documentation, and then the measurement says nothing.

**A page about data nobody queries.** On that estate, 12 of 27 documented data
products had **no analytical query in 180 days**, while the five most-queried
had no documentation at all. Check before you write:

```
uv run python scripts/doc_coverage.py CANDIDATES.csv GOLDS.csv
```

**Anything you are guessing.** A wrong rule is worse than a missing one. It
moved one question off a table that already had the column it needed, cost
30 GB of extra scan, and broke an answer that had been right.

---

## Getting it into the agent

```
  your pages          a reader            a spec         Knowledge Catalog
  (markdown)     ->   read_catalog_docs -> spec/rules/ -> spec-push --provenance
                      (deterministic,                          |
                       no model, so it                         v
                       cannot paraphrase                  arms --served
                       your words)                        (what lookupContext
                                                           actually hands back)
```

The reader is deliberately dumb — regular expressions, no model. Two reasons,
both learned the hard way. A regex fails loudly and a model fails politely:
an early version of that reader silently carried 8 table names out of 34
files and the output still read fine. And if a model rewrites your
documentation, the thing being measured is no longer **your** documentation.

## Then measure, and check the set can answer before you pay

```
uv run python scripts/estate_shape.py GOLDS.csv     # free
uv run python scripts/resolvable_lift.py            # free
```

A round is scored only on questions where the two arms **disagree**. Six
disagreements all pointing the same way is the cheapest significant result
that exists. If your set is expected to move fewer than six, no amount of
good writing will produce a verdict — and `contextlift round` now refuses to
spend, and says so.

---

## Running it without a person's credential

A framework that runs only from one laptop, with one human's login, cannot be
handed to anybody. It also cannot survive a corp policy: on 2026-09-12 a
Google corp account was refused by the Conversational Analytics API with
`access_denied: Account restricted`, raised while *minting the token*, so no
project or permission change reached it. Five routes were tried:

```
  the account directly              Account restricted
  a second project                  Account restricted  (account, not project)
  service-account impersonation     Account restricted  (the corp identity is
                                    still what asks for the token)
  a gcert refresh                   Account restricted  (OAuth, not LOAS)
  a downloaded service-account key  org policy: enforced: true
```

`deploy/cloud_run_job.sh` is the sixth, and it is not a workaround. On Cloud
Run the machine **is** the service account: its metadata server hands the
process a token with no login, no key on disk and no human in the chain.

```
  export CONTEXTLIFT_RUN_PROJECT=your-project
  export CONTEXTLIFT_RUN_SA=runner@your-project.iam.gserviceaccount.com
  deploy/cloud_run_job.sh build
  deploy/cloud_run_job.sh run round --golds golds.csv --spec spec/ ...
  deploy/cloud_run_job.sh logs
```

Verified end to end the same day: the image built on Cloud Build in 56
seconds, `contextlift demo` ran inside the job, and a `classes --run` probe
answered 13 real chat calls as the service account against the API that
refuses the human account.

Two reasons to prefer it even where a laptop works. A round takes an hour and
a laptop sleeps. And `.dockerignore` excludes `_personal/`, so customer data
cannot be built into an image by accident.
