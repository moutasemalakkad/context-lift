# Quick start

Three steps. The first takes one second and needs nothing.

## 1. See what a round looks like — no key, no cloud, 0.14 seconds

```
uvx contextlift demo          # or: make install && .venv/bin/contextlift demo
```

It builds a seven-row warehouse in a temporary folder, asks six questions
with and without one written business rule, and prints:

```
  without the rule 3 of 6 right
  with the rule    6 of 6 right
  lift +50.0 points [+10.0, +90.0], 3 fixed, 0 broke
```

The model answers are canned so this runs with nothing installed, and the
screen says so. The warehouse, the SQL, the paired test and the verdict are
the real code.

## 2. See a real result — no key, no cloud, one minute

```
git clone https://github.com/moutasemalakkad/context-lift && cd context-lift
make install
make example-thelook
```

This recomputes a real round on a public BigQuery dataset from its recorded
answers:

```
verdict: REAL GAIN
  the held-out questions moved +20.0 points (p = 0.0312), and they were read once
  held out: 30 questions, +20.0 points, 6 fixed, 0 broke
  no warehouse, no model, no network
```

30 questions nobody wrote the context from. Six fixed, none broken. The
context that did it is seven lines of plain English in
`examples/thelook/spec/rules/`.

Getting there took two rounds, and the first one refused to claim anything.
That story is [WALKTHROUGH.md](WALKTHROUGH.md), and it is the part worth
reading.

## 3. Point it at your own data

You have a warehouse, not an answer key. So the first two stages build one
out of the queries your analysts already wrote.

### Start with ten of your own queries, not two thousand

Do this first. It takes about three minutes and costs a few cents, and it is
the difference between understanding the tool and being buried by it:

```
# ten queries, only the ones YOU ran, so you recognise every row
contextlift log  --db bq://YOUR_PROJECT --days 30 --limit 10 \
              --only-accounts you@your-company.com \
              --min-tables 2 --require-analytical --max-bytes 10GB \
              --out my_queries.csv

contextlift mine --db bq://YOUR_PROJECT --queries my_queries.csv --limit 10 \
              --write-questions --budget 1 --out my_candidates.csv

contextlift review --candidates my_candidates.csv --out .
```

Then open `review.csv` and read it. Ten rows, all yours. You will know within
a minute whether the drafted question matches what you actually meant, and
whether the SQL beside it is really the answer. That is the judgement the
whole framework rests on, and it is much easier to learn on ten queries you
wrote yourself than on two thousand from strangers.

Three things you are checking in that file, and they are the same three you
will check later at scale:

1. **Is the question what you meant?** A drafted question that says "sales by
   region" for SQL that filters to one country is wrong, and you are the only
   person who can tell.
2. **Is the SQL the answer, or just a query?** `SELECT *` and a `LIMIT` with
   no `ORDER BY` have no single right answer, so they cannot grade anything.
   The gate rejects those, and you will see it do it.
3. **Would written context change the answer at all?** If a row is
   `SELECT MAX(week) FROM one_table`, no business rule can move it. Those
   rows are the ones that made two earlier customer rounds prove nothing.

`--only-accounts` takes more than one address, so the same recipe scopes a
gold set to one team when you move past your own queries:
`--only-accounts alice@corp.com,bob@corp.com`. Or select by job label with
`--labels team=sales`, which survives someone leaving.

When the ten rows make sense, scale up.

### Then the full pass

```
# 1. what your analysts actually ask, from 180 days of job history
contextlift log  --db bq://YOUR_PROJECT --days 180 --limit 2000 \
              --min-tables 2 --require-analytical --max-bytes 100GB \
              --out query_log.csv

# 2. each shape becomes a candidate question with its SQL as the answer
contextlift mine --db bq://YOUR_PROJECT --queries query_log.csv --limit 500 \
              --rank-by users --write-questions --out candidates.csv

# 3. a person (or --draft, a model) decides keep or reject, row by row
contextlift review --candidates candidates.csv --out .
contextlift review --sheet review.csv --out golds.csv

# 4. then the loop
contextlift gate  --db bq://YOUR_PROJECT --golds golds.csv
python scripts/estate_shape.py golds.csv       # free: can a rule even carry?
contextlift split --golds golds.csv
contextlift round --db bq://YOUR_PROJECT --golds golds.csv --spec spec/
contextlift next  --workspace .
contextlift history --out history.csv             # free: what broke, and when
```

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

### Write it down as a script, not as a chat history

Every stage above takes flags you will get wrong once and then want to keep.
Put them in one file with a case per stage, so the run is reproducible by
somebody who was not there:

```bash
#!/usr/bin/env bash
set -euo pipefail
DB=bq://YOUR_PROJECT
case "${1:-}" in
  log)   contextlift log --db "$DB" --days 180 --limit 2000 \
             --min-tables 2 --require-analytical --max-bytes 100GB \
             --out query_log.csv ;;            # cents
  mine)  contextlift mine --db "$DB" --queries query_log.csv --limit 500 \
             --rank-by users --write-questions --out candidates.csv ;;  # dollars, ~90 min
  gate)  contextlift gate --db "$DB" --golds golds.csv ;;                  # cents
  *)     echo "stages: log mine gate ..." ; exit 1 ;;
esac
```

Each stage says what it costs in a comment beside it. A pipeline that only
lives in a terminal history is a demo.

### What it costs

Every command that spends money prints a cost sheet and waits for you to type
`RUN`. Free: `demo`, `split`, `arms`, `classes`, `leak`, `next`. Cents:
`gate`, which runs each gold once. Dollars: `round`.

### What to expect on your first round

**UNDERPOWERED**, most likely. A customer estate usually yields 50 to 90
questions, and 24 held-out questions can only prove a lift above about 20
points. That is not a failure and not a bug: it is the tool telling you what
your evidence can carry, and printing how many questions would settle it.

If you want a number on the first day, the thing to grow is the question set.

**Read the safety line, not only the verdict word.** Two numbers print, and
only one of them is a guess:

```
verdict: UNDERPOWERED
  24 held-out questions cannot show a 10-point lift even if nothing broke;
  about 60 would. Nothing here says the context does not help
safety: SAFE
  5 fixed, 0 broke, of 24 on the held-out questions. Nothing that was right
  without the context became wrong with it
```

The lift is a claim about questions nobody has seen, so it needs a test and
an interval, and on a small set it will read UNDERPOWERED. "Broke 0 of 24"
is a count of what happened to those 24. There is nothing to estimate, so
there is no test, and the word is true at any size. `SAFE` means keep the
context; `REGRESSION` means revert it or explain each broken question.

So UNDERPOWERED and SAFE together is the normal early state and it is good
news: this context does not yet have enough evidence behind it, and keeping
it costs nothing.

**After the second round, `contextlift history`.** Free, no cloud call. One row
per question, one column per round, so you can see whether a question has
been broken once or three rounds running. A question broken repeatedly is a
rule to fix; one broken once and right since is history. At a thousand
questions this is the only readable view — a round might move thirty of
them, and thirty is reviewable.

### What leaves your machine

Nothing, unless you run `contextlift share`. That sends counts, lengths, pass or
fail, and pseudonyms from a map that stays local, and it refuses the whole
export if a real name survives into it. No question text, no SQL, no table
names.
