<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com> -->

# Field notes

[Documentation](README.md) / Research / Field notes

Operational observations from runs recorded in September 2026. These excerpts
were moved out of the quickstart, walkthrough, and context-writing guide so
the guides can describe one current workflow.

**Read these as historical evidence.** Commands, service behavior, timings,
prices, and scope heuristics describe the original runs. Some have since
changed. Use [Evaluate your data](EVALUATE.md) for new runs, estimate paid
commands, and choose scope from owner input or demand rather than names.
Customer observations without committed artifacts remain reported findings,
not reproducible benchmarks. Qualifications from the original records follow.

## Browse the records

- [Sampling and coverage](#sampling-and-coverage): why a query sample missed the workload.
- [Context-writing observations](#context-writing-observations): definitions, examples, and identity constraints.
- [Costs and progress](#costs-and-progress): measured bills and heartbeat output.
- [Catalog deployment](#catalog-deployment): write/read limits and retrieval findings.

## Sampling and coverage

*Source: the former warehouse section of Quickstart. The analytical filters
and survival rates describe this sample; they are not universal requirements.*

**Read the gate line before anything else costs money.** `round` prints four
free checks first, and the first one is the one that catches the mistake
nobody expects:

```
[ok] table coverage: 38 of 39 golds have every table they need in the arm (97%)
```

If that says 3%, stop. It means the questions came from the whole warehouse
and your context spec describes a corner of it, so the agent will be asked
about tables it was never given. It answers with no SQL at all, both arms
fail the same way, and the lift is 0.0 for a reason that has nothing to do
with your context. On one estate that cost 37 minutes and 78 API calls before
anybody noticed. The fix is to add the tables to the spec — the gate names
the missing ones.

**Run `estate_shape.py` before you pay for a round.** It makes no cloud call
and it answers a question no gate could ask before: can a rule written from
one half of your questions even reach the other half? A rule only carries
when the two questions read the same table.

```
  104 questions over 9 tables
  11.56 questions per table
  1 of 9 tables carry exactly ONE question
  hash the id: 74 working, 30 reserved
    [ok  ] 30 of 30 reserved questions share a table with the working half (100%)
```

That is a healthy set. This is not:

```
  36 questions over 59 tables
  0.61 questions per table  <- under 2.00: most tables cannot appear on both sides
  41 of 59 tables carry exactly ONE question
  15 of 26 datasets carry exactly ONE question
    [ok  ] 6 of 10 reserved questions share a table with the working half (60%)
```

At 0.61 questions per table, four of ten reserved questions were about tables
the working half never touched, so no context could reach them however well
written. Three rounds on that set reported a reserved lift of +0.0 and every
one of them was measuring the split, not the context. The remedy is to mine
more questions on FEWER tables, or to re-split; writing better rules cannot
change it.

**Scope step 1 to the datasets people actually query, and pick them with
`log --demand`.** `--limit 2000` over a whole warehouse ranks one dataset's
8,000 shapes against every other dataset and keeps about 2 usable shapes per
table, which is the shape above. `--demand` costs about $0.27 and prints one
row per dataset straight from `INFORMATION_SCHEMA`:

```
contextlift log --db bq://YOUR_PROJECT --days 180 --demand --out demand.csv
contextlift log --db bq://YOUR_PROJECT --days 180 --limit 1200 \
             --datasets warehouse_core,mart_orders \
             --min-tables 2 --require-analytical --out query_log.csv
```

Two traps in reading `demand.csv`. **A high `distinct_accounts` is not
evidence of human traffic.** On one estate the dataset with 22 accounts had
1,127 of its 1,200 query shapes written by a BI tool, with generated aliases
like `clmn0_` and `t0_qt_34hirhvcwd`: those accounts clicked a dashboard.
**And the busiest dataset may be a scratch area.** The top one by traffic
there was literally named `x`, holding
`caprod_a_discount_detail_26_aug_backup` and
`eno_validation_2026003_cleaned` — a gold question about a table with a date
in its name expires next month. Skip names containing `test`, `backup` or
`_stg`, and skip any dataset where one account runs 30+ jobs a day at 100%
of its jobs, because that is a schedule and not a person.

**Then size the scope against the golds you will END with, not the
candidates you mine.** `mine` spreads kept shapes round-robin across tables
so the busiest one cannot fill the sample, which is right for surveying and
wrong here: about 10% of candidates survive review and the gate, so 900
candidates is roughly 44 golds, and spreading 44 golds over 229 tables gives
0.19 each. The rule:

```
tables in scope  <=  (candidates mined  x  yield)  /  2
```

Above that, pass `--no-spread` or narrow `--datasets` further.

**Do not skip `--min-tables 2 --require-analytical` on step 1.** `log` keeps
the busiest query shapes, and a dashboard tile runs hundreds of times for one
viewer, so without those two flags the export is almost all dashboards. On one
enterprise warehouse, measured over its own 180 days:

```
1,205,904  successful SELECTs
  290,940  with a GROUP BY
   29,684  ALSO naming two or more tables          <- 2.5%
```

At 2.5%, a top-2,000 pull by traffic held 42 analytical shapes, and of the 75
answer keys it produced, **two** joined two tables and aggregated. No written
business rule can change the answer to `SELECT ANY_VALUE(latest_week_text)`,
so 97% of that set could not move however good the context was. Two rounds on
it proved nothing, and the cause was in step 1. With the flags, the same
2,000 rows held 1,494 analytical shapes.

`gate` next, always. It audits your answer key against 16 defect classes and
4 warnings before any score exists. A published benchmark's own golds failed
it at 52.8%. Four of the classes name what fixes them rather than what broke:

| class | what it means | who fixes it |
|---|---|---|
| `OVER_BUDGET` | the byte ceiling turned the query away before it ran | you: raise `--max-bytes` |
| `NO_ACCESS` | a 403; the platform refused the call, not the SQL | whoever owns the org policy |
| `TABLE_GONE` | the query names a table that no longer exists | nobody: drop the gold |
| `EXEC_FAIL` | the SQL itself is wrong | the query's author |

That split matters. On the same warehouse 179 candidates came back
`EXEC_FAIL` under the old single label, which reads as "your query log is
junk". 112 of them were the 1 GB default ceiling and one flag away.

`contextlift next` reads the workspace and prints the one command to run now,
with the reason.


## Context-writing observations

*Source: the original context-writing guide. For the current checklist and
template, see [Write business context](ONBOARDING.md).*


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

## Costs and progress

*Source: the operational sections of Walkthrough. The quoted costs belong
to those runs; review a fresh estimate for your data.*

## The tool prices its own settings instead of asserting them

A flag with a number in it is a judgement somebody made, and until it is
priced nobody can argue with it. Two of them print what a different choice
would buy, computed from figures the run already has, so neither costs an
extra query.

The byte ceiling. `--max-bytes` refuses a query whose dry run scans more than
it, and on a warehouse of any size the 1 GB default refuses a lot:

```
  what raising --max-bytes would buy (it was 1 GB):
    --max-bytes 10     GB   61 of 112 come back, 0.28 TiB to run once, ~$1.74
    --max-bytes 200    GB   93 of 112 come back, 2.08 TiB to run once, ~$12.99
    --max-bytes 2000   GB   111 of 112 come back, 20.55 TiB to run once, ~$128.45
    one shape wants 29,546 GB on its own, 17x the next largest (1,766 GB): a
    ceiling that includes it is paid for one gold
```

That last line is the point. Without it you read "111 of 112" and pay $128
for one extra question.

The question-literal cap. `--max-question-literals` drops a drafted question
carrying more than N quoted values, because six product codes and an order
number is one analyst's own lookup, not a question anyone else asks. The
default is 2, and the run says what 3 would cost:

```
  what a looser --max-question-literals would buy (it is 2):
    --max-question-literals 3   39 of 84 dropped questions come back
    --max-question-literals 6   72 of 84 dropped questions come back
    the largest carried 35 quoted values, which is the kind of question the
    cap is for: one analyst's own lookup, not a question anyone else asks
```

Neither ladder changes anything by itself. They exist so a setting is a
decision you made with the numbers in front of you.

## What you will see while a long stage runs

`mine` makes one model call and one warehouse query per shape. On a real
estate that is 500 shapes and about 90 minutes. Every 25 items it prints one
line, appended, never a repainted bar:

```
mine       2m14s  [####------------]    100/437   eta 7m31s  $1.21
                  clean 61  over-budget 14  no-access 8  gone 5  bad-sql 3
```

The top line is where it is, how long is left, and what it has cost. The
second line is the part worth watching: it is a live answer to "is my query
log any good". `clean 61` means 61 usable answer keys so far. The rest are
named for **what recovers them**, not for what broke:

| name | meaning | what to do |
|---|---|---|
| `over-budget` | the ceiling turned it away before it ran | raise `--max-bytes` |
| `no-access` | a 403; the platform refused the call | ask whoever owns the policy |
| `gone` | names a table that was dropped | nothing; the gold goes |
| `not-a-question` | `SELECT *`, or `LIMIT` with no `ORDER BY` | nothing; it has no single right answer |
| `no-rows` | it runs and returns nothing | any wrong SQL also scores right on it |
| `bad-sql` | the SQL itself is wrong | read `gate_detail` |

A bar is never full until the stage is done, and `eta` reads `?` until there
is a rate to measure.

### When one item hangs

If a single item stops responding, the counting line cannot help you: it is
printed once per item, so an item that never finishes produces no line at
all. A separate heartbeat covers that case, and it says the two things that
diagnose a hang:

```
mine       3m53s  [###########-----]    100/150   eta 1m56s  $0.74
                  clean 22  over-budget 14  no-access 16  gone 18
mine       6m15s  [##########?-----]    101/150   stuck 3m28s  3 model retries (429), 1m02s waiting
```

Same columns, so you can read down. Two differences, both deliberate. The
bar carries a **`?` at the item being waited on**, so the line is visibly
about one item rather than the whole run. And where an eta would be it says
**how long this one item has taken** — an eta there would be a lie, because
the rate it is computed from has stopped.

The text after it is what the stage knows about its own calls. `3 model
retries (429)` means the model answered "too many requests" three times and
the tool waited between tries; `0 retries` is also printed, because "this is
slow and nothing is being retried" is a different problem from "this is
being throttled" and you cannot tell them apart by watching.

This exists because of a run that went silent for **six minutes and fifteen
seconds** with a time-based throttle already in place and working exactly as
written. The throttle was checked once per item, so it was a clock that could
only be read while things were already moving.

### The `$1.21` is why that column exists

`mine` checks what it has billed between shapes and stops at `--budget`,
keeping everything it found. The reason it watches the real spend rather than
trusting the estimate is that a mine's cost is concentrated in a few queries
and the estimate is not reliable in either direction. Measured on one
enterprise warehouse, 45 minutes into a 500-shape run:

| queries | size | share of the bill |
|---|---|---|
| 11 | over 10 GB | **$1.39** |
| 21 | 1 to 10 GB | $0.52 |
| 14 | under 1 GB | $0.02 |
| 42 | free (dry run or table probe) | $0.00 |

Two things to read off that. **11 queries of 88 were 72% of the bill**, each
near the `--max-bytes 50GB` ceiling, the largest scanning 48 GB. And **42 of
the 88 jobs cost nothing at all**: a dry run is free, and so is the
`SELECT 1 FROM t LIMIT 1` probe that checks whether a table still exists. So
a job count says almost nothing about a bill.

The dry run estimated $8.89 for that run. It billed $1.94, about a fifth. The
dry run prices the scan BigQuery can predict before it runs, and partition
pruning and the result cache then cut the real bill. It can also be too low.
An estimate is a useful warning and a bad ceiling, which is why `--budget` is
now checked twice: once against the estimate before starting, and again
against what has actually been billed, between shapes.

Those four rows are recomputed from the run's own job history by
`tests/test_mine_cost_bands.py`, which fails if this table drifts. It has
caught this table being wrong twice, and both mistakes are worth knowing:

- The figures were first computed by dividing bytes by 10^12 and pricing the
  result per TiB, which overstates by 10%.
- Worse, they were first pulled **by time window alone** from a shared
  production project. 623 of the jobs in that window belonged to other people
  querying the same project, and $13.40 of the $15.34 was their spend. This
  table said $10.84 for a run that cost $1.94. Filtering by the account that
  ran the mine gives the 88 jobs above.

That second mistake is the argument for the `app=context-lift` job label on
every query this tool runs. With the label, the first query would have been
right. See [ATTRIBUTION.md](ATTRIBUTION.md).

That gap is normal and it is why the estimate cannot be the only guard: a
dry run prices the scan BigQuery can predict, and a real scan can come in
above it. So `--budget` is checked before the run against the estimate and
again between shapes against what was actually billed. When it stops you get:

```
STOPPED ON BUDGET after 137 of 500 shapes: $25.04 billed against --budget $25.00
  every count below is over the shapes it reached, not over the log.
  The shapes are ranked, so what it reached is the top of the ranking.
```

Read that first line before any other number in the summary. A stopped run
sampled the top of the ranking, so `clean 22` means 22 clean out of the
shapes it reached, and says nothing about the rest of your log.

The review file is written **as each candidate is found**, not at the end,
so you can open it in another terminal while the run continues, and a run you
kill keeps its work. The cheapest way to fit a big log under a budget is to
lower `--max-bytes` rather than to raise the ceiling: the summary's
over-budget ladder prices each step for you.


## Catalog deployment

*Source: the catalog guide. These field probes include behavior that evolved
over the course of the deployment.*

## Field notes from a live enterprise deployment

Everything above is about where knowledge goes. Everything below is about
making the calls work. Each item cost hours and appears in no documentation.

## One rule that outlives the API

**Verify a write by reading it back, and compare lengths.** A catalog that
accepts your bytes and stores fewer of them looks exactly like one that worked.
Every silent failure below is a variation of this, and it applies to any context
source, not only this one.

## How much to trust each item below

Two of these have been probed a second time against the live store on purpose.
The rest were observed once, in the course of doing something else, which is how
a fact comes to be remembered more confidently than it was earned. Treat them as
leads that cost someone a day, not as established behaviour, and check the one
you are about to depend on.

Probed twice: the indexing lag (scoped down as a result, see below) and
`resolve`-then-fetch reliability. The other seven are single observations.

## Knowledge Catalog, measured

- **`lookupContext` returns YAML whose `overview` field holds the entry's
  markdown body.** Parse it with a YAML loader, not string slicing: long column
  names get folded across lines, and naive parsing gives false negatives.

- **`lookupContext` does not return custom aspects.** Only the built-in
  `overview`. An adapter that writes its context as a custom aspect type
  publishes successfully and reads back nothing — a silent no-op, not an error.
  `entries.get?view=ALL` does return custom aspects. Confirmed live on both the
  global and the regional endpoints.

- **Entry names carry the project *number*, not the project id.** Building the
  name from the id returns not-found.

- **There is indexing lag after a push.** Immediately after registering entries,
  `lookupContext` returned 0 characters; the same call a few minutes later
  returned the whole document. An adapter that reads straight after a write
  looks broken. Retry, and never cache an empty or stub-length body.

  Scoped by a later probe: 60 sequential reads of four documents from a warm,
  unwritten catalog came back **byte-identical**, and `resolve` never returned a
  name that then failed to fetch, 0 of 60. So the degraded bodies are not
  general flakiness. They appear in two conditions and the retry rule earns its
  place in those: **immediately after a write**, and **under a burst of
  concurrent reads** — the original stubs were seen while enriching about 40
  candidates back to back. A test double built from steady-state behaviour will
  not exercise either.

- **Retracted: "do not set a quota project on the credentials for catalog
  calls."** The original observation was real: a search returned zero results
  and no error, and the quota project was the diagnosis. Re-tested by its
  author on the same estate and by a second probe on another project, the
  count is identical with and without the quota project (12 and 12; 419 and
  329 on the other project, both non-zero). Either the diagnosis was wrong or
  the behaviour changed server-side; nobody can tell from here. Kept as the
  cleanest example of the shape a wrong belief takes: born from a genuine
  symptom, generalised into a rule, never re-tested.

- **The overview aspect has a size ceiling and exceeding it truncates
  silently.** Column descriptions for wide tables were dropped at registration
  with no warning.

- **Search scoping needs an explicit predicate, and it behaves differently by
  search mode.** Scoping a search to one entry group by name prefix worked in
  semantic mode and returned zero results in keyword mode. Whether that is
  intended was asked of the product team and never answered. An adapter should
  verify its scoping returns a non-zero count on a known-good query rather than
  assume the predicate applied.

- **Entry ids** in the deployment tested were `<dataset>__<table>`, which makes
  mapping a routed `dataset.table` to an entry trivial. Do not assume it; make
  it configurable.

- **An entry group lists more than tables, and entry type does not tell them
  apart.** One group held 129 entries: 127 tables, the group's own self-entry,
  and an index entry. The self-entry has type `entrygroup` (one word, no
  hyphen; a filter written with a hyphen matched nothing and the count stayed
  129). The index entry has type `generic`, the same string every real table
  carries, so a type filter keeps it. What separates them is `parentEntry`:
  set on all 127 tables, empty on both others. An adapter that builds a table
  list from a group should keep entries whose `parentEntry` is set and not
  consult the type. A count that is two too high is the symptom.

## Probed on a second project

The live transport (`DataplexTransport` in `catalog.py`, behind `contextlift
catalog-pull`) was written against a second project: BigQuery's own entries in
the regional `@bigquery` entry group, 44 tables, no custom entry group. Reads are
metadata calls and are not billed. What that probe measured, each once unless
stated:

- **The quota-project trap did not reproduce**, here or, on re-test, on the
  estate it was written about (see the retraction above). `check_scope` still
  exists because the failure it catches, a search that answers nothing and no
  error, has other causes on this list: the entry-group predicate in keyword
  mode is one, re-confirmed on the original estate (0 results in keyword mode,
  6 for the same intent in semantic mode).
- **`name:{table} type=table` matched exactly one entry**, three times out of
  three; `name:{table}` alone matched 13 things including glossary terms. On
  the search path the store takes only a result whose entry name keys to
  the requested `dataset.table`, because the same table name lives in
  several datasets of one project, and a search can return an entry for a
  different table first. Search is no longer how a page is addressed at
  all; the name is built from the key (see "Writing into the catalog").
- **`lookupContext` on one entry: 15606 characters, byte-identical over 43
  reads** (3 sequential, then 40 concurrent). Steady state is stable, as the
  first probe found; the burst condition did not degrade it here.
- **`context_budget` is best effort and coarse.** Budgets of 2000 and 500 both
  returned 6287 characters of the 15606, whole sections dropped. No budget
  returned everything. The API rejects a budget that is not a positive integer.
- **A missing entry returns 0 characters and no error.** The store resolves by
  search first and treats an empty body as degraded; `catalog-pull` writes no
  file for it.
- **Two entries in one lookup came back as 23 entry blocks, 43112 characters.**
  The lookup expands to related entries. The transport asks for one entry per
  call so a document for one table does not carry another.

## What catalog-pull writes

`catalog-pull --out DIR` writes the served bundle: the text `lookupContext`
returns for the entry, which is YAML (`resources:` first) with the overview
inside it as a quoted scalar, folded on BigQuery's own entries and with
literal `\n` sequences on a custom group's. That is what an agent gets, so it
is the arm, and it is kept byte-exact. It is not a file a person can edit,
and `catalog-push --docs` cannot send it back.

`catalog-pull --pages DIR` writes, beside it, the overview aspect as the
catalog stores it, with the push markers removed: `<dataset>.<table>.md`,
plain text, no file for a table without an overview. That is the round trip:
pull `--pages`, edit, `review`, `catalog-push --docs DIR`. A page pushed
unchanged is reported as unchanged, because the stamp is a hash of the text.

Either way the pull reads both: the served bundle and the stored overview, one
`entries.get` per table. It prints the two lengths and the budget when the
reader got less than the entry holds, and names a `--budget` that would serve
the page. It does not raise the budget itself; the budget the caller passed is
what that caller's readers get. Why the comparison is worth an extra read is
in "A read can be truncated too" below.

## Writing into the catalog

`contextlift catalog-push` is the fix half's last stage. What it writes, where,
and how each is verified; graded by what the fact rests on (public docs,
the earlier project's live code, or this repo's live test).

- **Pages become the table's `overview` aspect** (public docs; the earlier
  project wrote it live). `update_entry` with the aspect under the key
  `dataplex-types.global.overview` and `update_mask paths=["aspects"]`;
  the API returns the same aspect keyed by the project number, so a reader
  matches the key by its `.global.overview` suffix. Role:
  `roles/dataplex.entryOwner` on the entry group. Each page is wrapped in a
  begin and end marker carrying a content hash, so a second push sees an
  unchanged page without reading the body, and a page the catalog holds
  that contextlift did not write is replaced with its previous bytes kept.
- **A page's entry name is built from its key, not searched.** The entry
  id inside the group is a template of `{dataset}`, `{table}` and
  `{project}` (`--entry-id`). BigQuery's own group `@bigquery`, the
  default, had one shape on every estate measured,
  `bigquery.googleapis.com/projects/{project}/datasets/{dataset}/tables/{table}`,
  and that is its default template; a custom group's ids are whatever
  whoever registered the entries chose (`{dataset}__{table}` on the
  estates measured), so it takes `--entry-group G --entry-id
  '{dataset}__{table}'`. With a template nothing is searched; 99 of 99
  custom entries were reachable by the built name. Why: an earlier
  substring resolve put 12 of 99 pages on a sibling table's entry, and
  the same table resolved right in one run and wrong minutes later with
  only the search ranking changed; search given the complete
  fully-qualified name of a table returned four hits with a different
  table first; and the API has no other exact address (LookupEntry takes
  no fully-qualified name). Built names are read until one exists: a name
  that is missing is counted and the run goes on with it as that table's
  own line, and only a run where NO built name existed exits 2, naming the
  first name it tried, the `--project` and `--location` that build it, and
  `catalog-list` for the group's ids. Doubting the template on the first
  missing name refused a whole pull because the first of the tables listed
  was not catalogued, and blamed an `--entry-id` the caller never passed.
  Without a template (a custom group with no `--entry-id`) the search
  path remains, and there a hit counts only when its key equals the
  page's `dataset.table`: on one estate the search for a `__dbt_tmp`
  table returned a stale entry of another dataset and table first, and an
  earlier version took it. On the auto group `name:{dataset}.{table}`
  returns nothing, so `--entry-pattern` keeps `{table}`. The same name in
  several entry groups refuses unless `--entry-group` says which.
  `--dry-run` lists where each page would go (`key -> entry  would write
  (N chars)`), writes the plan under `--manifest-out` when given, writes
  nothing to the catalog and is not recorded in the workspace; the
  command says so on its last line.
- **Pages go in three phases, and a killed push is resumed by running it
  again.** Phase 0 resolves every page (searches are free) and refuses
  with exit 2 when two pages resolve to one entry, listed as `key1, key2
  -> entry`: an earlier resolve put 12 of 99 pages onto a sibling table's
  entry and read-back verified every one, because read-back proves the
  bytes survived, not that they landed on the right table. Phase 1 writes
  every page. Phase 2 reads all written pages back together, one wait of
  2, 4, 6 seconds between passes and not per page: on one deployment a
  push that read each page to exhaustion before writing the next took
  about 25 minutes for 99 pages, printing nothing, because 22 pages were
  read four times each (a line fold inside the end marker, see below). The
  re-run over the same 99 pages with this code took 90 seconds, with 95
  pages unchanged and 4 written.
  Progress now goes to stderr as it happens, and SIGINT or SIGTERM writes
  the manifest with the counts so far and `"status": "interrupted"`
  before the process ends. Running the same command again continues:
  stamped pages come back unchanged, the rest are written; `--verify-only`
  reads back the pages that were written and not verified. One push per
  workspace at a time: a lock file (`catalog-push.lock` in the workspace
  root, else beside the manifest) refuses a second push while the first
  one's process is alive, and says so when it replaces a stale one.
- **Verification is a read through `lookupContext`**, the call every reader
  uses, not through `get_entry`: a body that is stored whole and served cut
  is the failure the field measured. The served text is unfolded first: a
  double-quoted YAML scalar is folded at a fixed width with `\` + newline
  + indentation (no character) and `\ ` (one space), and on one estate
  the fold landed inside the end marker of 22 pages out of 99, each then
  called truncated with a served text longer than the page; 18 verified
  once unfolded. The classification is stub (under the store's
  threshold), missing (no begin marker), truncated (bytes gone: no end
  marker in a served text shorter than the page, or the body between the
  markers under 95% of what was written), unverified (no end marker in a
  served text no shorter than the page: nothing known lost, the marker not
  matched), verified. A page not verified then reads `get_entry` once to
  name the cause: "stored N of M", "reader sees N of M; the aspect holds
  all of it", or "end marker not found in the served text (served N
  characters for M written)". A verified page records
  `served_minus_written`, the bundle's overhead, with no verdict on it.
- **Rule lines become glossary terms** (public docs), linked to the table
  they name by an entry link of type `definition` created in the table's
  entry group, with the table as source and the term's entry as target;
  `lookupContext` lists linked terms under related business terms. The
  glossary lives at `locations/global`; a term needs its parent set on
  create. Whether a linked term shows in the bundle is checked by the live
  test, not assumed.
- **Worked examples become the table's `queries` aspect** when the API
  accepts a user write to it (the product's own documentation scans write
  that aspect, and `lookupContext` returns it), else verified-query entries
  in a custom entry group found by semantic search. The push records which
  home the estate supports.
- **Column descriptions are not written here.** They belong to the BigQuery
  table schema; the catalog ingests them. Changing the warehouse is a
  different permission and a different tool.
- **Custom aspect types are not used for context**: `lookupContext` does not
  return them (see above), so anything an agent should see lives in a
  system aspect, a glossary term, or a searchable entry.

Measured on the first live push (the owner's project, BigQuery's own
entries, 2026-09-05), each once:

- An overview of 2,294 characters was stored whole and appeared in
  `lookupContext` on the first read after the write, inside a 9,270
  character bundle; a second table's page the same. The `overview` aspect
  template caps `content` at 100 KiB, which is the ceiling the earlier
  silent truncation hit.
- The `queries` aspect accepts a user write and `lookupContext` returns the
  rows: a pushed example showed in the served page as
  `description: How many seasons are there?`. So examples live on the
  table itself; the verified-query entry group is the fallback only.
- A glossary term with a `definition` link to the table was created and
  read back from the glossary, but did not appear in the table's
  `lookupContext` bundle within six reads. The push therefore verifies a
  term where it lives and records `in_lookup` as an observation, and a
  reader that needs definitions must read the glossary or the entry links.
- `list_tables` on the `@bigquery` group: 53 entries, 44 kept, 9 without a
  parent entry (the dataset entries and the group's own).

## A read can be truncated too, and that one is worse

A store may cap what it returns as well as what it stores. Knowledge Catalog's
`lookupContext` takes a `context_budget` and the default truncates: a deployment
measured here passes 60000, because its SQL conventions sit at the end of the
overview document and a smaller budget silently returns the prose and drops the
rules.

On one estate (project PROJECT, entry group GROUP, measured 2026-09-05) the
read is not a cut at all: it serves a page whole or not at all at the budget it
is given. Same entry, same request, only `context_budget` changed. The entry
stored 71,690 characters (`entries.get`, `view=ALL`); budgets of 20,000 and
60,000 both returned 572 characters, and 80,000, 100,000 and 200,000 all
returned 78,854. A second entry storing 92,397 characters returned 532 at
60,000 and 101,385 at 150,000. All ten calls were HTTP 200 with no warning
field, no truncation flag and no other signal, and the 572-character body is a
valid page: the opening of the overview, no schema section, no column names.
The served text runs longer than the stored overview because the bundle carries
schema and profile around the page, so a served body under the stored size is a
page the reader did not get. On that estate 9 of 127 entries sat above the
budget the pipeline asked for, and they were the four largest finance and
shipment views, so the pages with the most in them were the ones the agent
never saw. This is one estate on one day, not a rule of the product: the second
project above returned 6,287 characters of a 15,606-character entry at budgets
of both 2,000 and 500, a partial serve rather than a stub.

That is the more dangerous half of the truncation problem. A write that silently
drops bytes is at least a store you can inspect. A read that silently drops the
end of a document produces a context arm that looks populated and is missing the
payload the experiment was measuring — and the run reports a null result nobody
questions, because everything about it looks right.

The rule for any adapter: never trust a default limit on a read, and check that
what came back is as long as what you believe is there.

## Retrieval, and what had to land with it

A catalog of any size cannot go in a prompt. One deployment measured here held
99 tables and roughly 1.8 million characters of description. Real systems
retrieve: a router picks a table, and only that table's documentation reaches
the model.

`generate --context-per-question FILE.json` takes `{question_id: context_text}`,
which is what any retriever emits. Two pieces had to land in the same change,
because without them the feature defeats its own measurement:

- **Arm identity.** An arm is what the model saw, so it is named after that: a
  hash over the sorted (question, retrieved text) pairs. Two different
  retrievals are two different arms. Without it they collide under one id and
  `ablate` compares them as if nothing changed, reporting a null result that is
  an artifact of the naming. An arm that retrieved nothing keeps the id it
  already had, so no existing workspace's history splits.
- **Leak checking per question.** `leak --context-per-question FILE.json` checks
  each question against its own retrieved text. It also checks every question
  the retriever did *not* serve — a reserved one — against the union of
  everything retrieved, because nobody hands a reserved question context but its
  answer can sit inside a document fetched for some other question. That is the
  leak that destroys the number you report, and a per-question check alone would
  miss it.

Read a catalog into a file first, and retrieve live only when you have to. A
file is reproducible: someone without your credentials can re-run the arm and
get your number. A live arm cannot be audited by anyone who was not there. The
two also answer different questions — a file answers "did this context help",
live answers "did my retrieval pipeline help", where the retriever's own
failures are part of what is being measured. The second is the rarer question.

## Where the documents come from

The routing gain above needed one thing before any rule was written: each
routed table had a page at all. `contextlift draft-context --db DB --out DIR`
writes that page mechanically, one `<dataset>.<table>.md` per table from the
schema (columns, types, the column and table descriptions the warehouse holds),
into the directory shape `DirectoryContextSource` reads. Every file says in its
second line that it was drafted from the schema and not reviewed, and ends with
an empty Rules section for a person. No model call and no rows are read: on
BigQuery it is three INFORMATION_SCHEMA queries per dataset, so it can be
re-run on every schema change.

Two cautions print with it. The pages use the schema's own words, so a
question set written from the SQL will match them by vocabulary rather than by
meaning; measure routing on questions people asked. And a page with no
description is a column list, which the summary counts (`84 columns, 31 with a
description; 5 tables have none`) so nobody mistakes coverage for content.

### Measured against hand-made pages, and what it did not move

On the deployment above, one round swapped exactly 14 of 113 documents, the
hand-made pages for `draft-context`'s (each of the 14 gained a real table
description where the hand-made page had repeated the table's name), with the
same 123 questions and the same router. Scored with `score-routing` into one
workspace and compared with `ablate`:

```
hand-made pages     26 of 123   21.1%
draft-context       23 of 123   18.7%
  context fixed 0 / broke 3   McNemar p = 0.2500   not significant
```

The drafted pages matched the hand-made ones inside the noise and did not beat
them, against the prediction made before the run. The churn line under the
same verdict read `answers changed at 39 of 123 pairs; 3 changed the verdict,
36 moved without changing it` (three of the 39 are pseudonymisation losses, so
36 to 39): a third of the answers moved while the score moved by three, for
the second time on this question set. The reason is upstream of any page. Of the 88 questions whose gold is one of the 14 tables, the router
put 77 in a different data product and 0 on a sibling table in the same
dataset (74 and 1 with the hand-made pages). The right dataset was never in
contention, so no description on the right table could matter.

The rule that comes out of it: on a question set whose golds sit outside the
router's reachable set, document quality is not the binding constraint and
cannot be measured through it. `routing-gate` says so, free, before anything is
spent; on this key it read `100 questions on 19 golds not in the catalog`.

## Measuring a retriever before trusting it

`contextlift score-routing` grades table routing with no model calls and no
warehouse scans, so the retriever can be measured on its own before anything
expensive runs. From the deployment above, 20 questions:

```
table routing, 20 questions, 3 repetitions, means:

  without catalog context : 0.650    (12, 13, 14 of 20)
  with catalog context    : 0.850    (18, 17, 16 of 20)
  snapped 0 of 20 predictions to the catalog
```

Three repetitions, not one. A single run of this comparison read 0.600 against
0.900 — the most favourable repetition on both sides, and a 5-point wider gap
than the truth. The same rule that makes `generate --reps 3` the default applies
to routing: one run is a sample.

That is worth running first for a reason beyond cost. A retriever's accuracy is
the ceiling on everything downstream, and comparing retrieval against a blob is
only meaningful when the blob cannot already hold the answer. On a gold set
touching four tables, a 16 KB blob contains the right table every time — recall
1.0 by construction — so retrieval starts behind and cannot win. Measuring that
comparison would produce a loss that means nothing about the feature. The
question set has to reach tables a blob cannot hold, or the experiment is
decided before it runs.

[Documentation index](README.md) · [Current evaluation guide](EVALUATE.md)
