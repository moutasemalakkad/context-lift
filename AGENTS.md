# Driving contextlift with an AI assistant

contextlift measures whether curated context makes a text-to-SQL agent more
accurate, and finds what to fix next. This file tells an AI assistant how to
run the loop for a user. Every step is one CLI command; there is no other
interface. The next command to run: `contextlift next`.
`contextlift --workspace DIR chat` is these same rules as a REPL: a model proposes
tool calls, one dispatcher checks each, the person types the confirmations.

## Operating rules

- Always pass `--workspace DIR` (or export `CONTEXTLIFT_WORKSPACE`) so every
  run is recorded: argv, input hashes, output copies, stdout, exit code. It
  is a global flag and goes BEFORE the command name (`contextlift --workspace
  DIR next`); after the command name every subparser exits 2.
  `DIR/index.json` then says which generate run produced which score run
  and which context version each arm used; do not keep that in file names.
- Always pass `--timeout 60` to commands that execute SQL on SQLite (gate,
  score, ablate, climb, triage, attribute, generate --retry). Predicted SQL
  is untrusted model output; without a bound one runaway query hangs the
  run. On BigQuery an unset `--timeout` is already 60 s (the command says
  so on stderr); pass it only to change the bound.
- Pass `--json` when a program (including you) reads the output, and never
  parse the prose reports. Ten commands take the flag: `score`, `gate`,
  `triage`, `ablate`, `climb`, `attribute`, `score-routing`, `routing-gate`,
  `next`, and `generate` (there only with `--estimate-only`). The other 21
  exit 2 on an unrecognized `--json`; read the files they write instead.
- Score each predictions file once with `score --save-report FILE.json`,
  then give ablate/climb the reports (`--without-report/--with-report`).
  Scoring executes SQL and is slow; the paired analysis is free. Under a
  workspace every analysis command saves its report into the run directory
  by itself; read `per_question[].status` from an ablate report instead of
  re-deriving fixed/broke from the id lists.
- The index picks what to compare: the baseline is the oldest scored arm
  with no context, the candidate the newest fully scored arm with context
  (`index.json` -> `pins`, with `explicit` saying whether a person set
  them). There is no pin step. `contextlift pin baseline|candidate ID` is an
  override for comparing two rules versions with each other. Never compare
  "vs the previous run" by position; the `rounds` chat tool (and
  `bundle`) give every rules version against the same baseline.
- On SQLite, run at most 2 scoring processes at a time. SQLite sorts spill
  temp files to disk; 10 parallel scorers once filled a disk and crashed
  the machine. On BigQuery, run one scoring command at a time and let it
  use threads: `--workers 4` is the default there (each round trip is
  seconds of network for a fraction of a second of server time). Do not
  start several BigQuery scorers side by side to go faster; raise
  `--workers` on the one command instead, and read the `time:` line of
  its estimate before starting it.
- Generate 3 repetitions per arm (`generate --reps 3`) and pool them in
  ablate by repeating the flag. Temperature 0 is not reproducible; one run
  is a sample, not a measurement.
- Before a paid `generate` or `draft-rules`, run it with `--estimate-only`
  and show the user the line it prints. Then run with `--budget USD` set to
  what they approved; both refuse above the estimate, and `generate` also
  stops at the ceiling mid-run. Both print a `spent:` line after the call,
  kept in the run's manifest.
- `--db` is a SQLite path or `bq://BILLING_PROJECT[/DATA_PROJECT.DATASET]`
  for BigQuery, everywhere. On BigQuery every SQL command is paid: run it
  with `--estimate-only` first (it dry-runs every statement and prints
  queries, GB and USD, then a second line with calls, seconds per call and
  minutes at the worker count), show both lines, then run with `--budget
  USD`. Answers already in the workspace's gold cache are not dry-run; the
  estimate says how many it reused. `mine` prints a ceiling first and the
  measured estimate once its shapes are ranked; its `--budget` applies
  there. Pass `--max-bytes` (default 1 GB per query) and never raise it
  without saying so; `log` prints its dry run as a ceiling (the job-history
  view overstates, 10x on one estate) and lets BigQuery cap the job on the
  real bytes; raise it only when the job fails on the cap.
  `schema` and `log` exist only to feed `generate --schema` and `mine
  --queries`.
- The first question on an estate is where the demand is: `log --demand
  --db bq://BILLING` writes one row per dataset (jobs, distinct shapes,
  distinct accounts, jobs per account per day), most accounts first, for
  every dataset or only `--datasets A,B`. The scope is a decision with a
  source, a file the customer gave or these counts, never a naming
  convention. A dataset over 50 jobs per account per day is a pipeline
  under personal credentials; pass those accounts to `--exclude-accounts`,
  which drops them in SQL before any hash. Same money rails as `log`.
- An estate is many datasets and often several projects. `schema`,
  `draft-context`, `profile` and `log` take `--datasets A,B` (a bare name
  is the billing project's, `data-proj.hr` another project's) and the
  first three take `--tables-file catalog.txt`, the list `catalog-list`
  writes; a project-only `--db` with neither is refused. `log --db
  bq://BILLING` reads the jobs that RAN in that project, wherever their
  tables live: pull one log per billing project and repeat `mine
  --queries` for each. A 2-part `dataset.table` in SQL resolves in the
  billing project; when `gate` or `mine` prints `N golds failed with 'not
  found' in project BILLING`, the fix is 3-part names, not new SQL.
- Put the `bq://` spec in `bigquery.txt` at the workspace root. Two
  optional lines under it, `datasets: A, B` and `catalog: PROJECT LOCATION
  [ENTRY_GROUP]`, make `next` propose the schema over those datasets (or
  over `catalog.txt` when the file is in the workspace) and offer the
  catalog arm as one sequence beside the rules arm.
- Never edit the holdout, score it repeatedly, or curate from it. `split`
  prints the rule; hold to it. Every round is answered over `curate.csv`, so
  no report of a round holds a reserved row and a forgotten flag cannot leak
  one into triage, attribute or the verdict page. At the end, and only then,
  the two pinned versions answer `holdout.csv` once each, are scored, and are
  read with one plain `ablate --without-report .. --with-report .. --show`
  (no `--ids`). That reading is one look, both arms included; `contextlift next`
  proposes each of those commands at the verdict, `bundle` labels the reading,
  and the index counts the looks. `--ids curate_ids.txt` is the training
  reading of a round; `--ids holdout_ids.txt` is for reports imported from
  elsewhere that hold both halves.
- `draft-rules` and `draft-context` write drafts; a person reads them before
  `context-write` installs them. `review` refuses to produce golds while any
  row is undecided. Do not skip these by writing the files yourself.
- When nobody is there to review on day one, `review --draft --sheet
  review/review.csv` lets a model accept or reject each undecided row with a
  reason; it never writes SQL or question text. Pick a reviewer from a
  different model family than the agent (`--model`). The verdict then
  carries the caveat "N of M golds accepted by a model reviewer" in `ablate`,
  `climb`, `next` and `bundle` until a person audits the rows (set `source`
  to `reviewed` on each checked row). Repeat that caveat with the number.
- With `--backend command` the agent's cost is its own; the framework counts
  runs, failures, abstentions and timeouts. Check one question three times
  first: `generate` prints how many questions were identical across
  repetitions, and if all were, the reps are copies, not samples.
- Read the caveat lines `ablate` and `climb` print (one repetition, no split,
  not on the holdout, catalog changed between arms, copies). Repeat them to
  the user with the verdict; they are the verdict's conditions.
- `catalog-push` writes into a shared catalog: run it with `--dry-run`
  first and show the plan (a dry run is not recorded under runs/; the
  command says so); it never deletes. A failed line in its summary
  is a finding (a store that kept fewer bytes, a read cut short, a stub
  straight after the write): report it, and run `--verify-only` a few
  minutes later before calling it broken. It refuses when the holdout's
  answers are in what it would write; do not work around that.
- A killed `catalog-push` (Ctrl-C, SIGTERM) leaves a manifest with the
  counts so far; resume it by running the same command again (stamped
  pages come back unchanged). One push per workspace at a time: a second
  one is refused while the first is alive.
- The trap each stage prevents: the verb's own `--help`.

## The loop, in order

Every line is one command under `contextlift --workspace WS`. `contextlift next`
prints the next one with absolute paths; run it from anywhere, and run it
when unsure. `$DB` is a SQLite path or `bq://BILLING[/DATA_PROJECT.DATASET]`.

`next` starts the loop at `mine` and then walks answer key, gate, split,
schema, baseline arm, score baseline. The two `log` lines below come before
`mine` only on BigQuery, where the query history is the file `mine --queries`
reads; on SQLite you bring that CSV yourself and `mine` is the first command.

```
log       --demand --db bq://BILLING --days 30 --out demand.csv    # where the demand is: one row per dataset, most accounts first (BigQuery only)
log       --db bq://BILLING --days 90 --datasets A,B --out log.csv  # the jobs that ran in BILLING and touched A or B (BigQuery only; ceiling printed first)
mine      --db $DB --queries log.csv --datasets A,B --out candidates.csv --write-questions
                                                                   # candidate golds, round-robin across tables, scoped; --queries repeats for several logs
review    --candidates candidates.csv --out review/               # the sheet + REVIEW.md: the human rewrites the questions
review    --draft --sheet review/review.csv --estimate-only      # no reviewer yet: a model accepts or rejects rows; priced first, the verdict carries a caveat
review    --sheet review/review.csv --out golds.csv               # refuses while any row is undecided
gate      --db $DB --golds golds.csv                              # audit the answer key FIRST; the clean key is recorded, no --clean-out needed
routing-gate --golds runs/<gate>/outputs/golds_clean.csv --catalog catalog.txt     # can the key be routed to at all (free)
split     --golds runs/<gate>/outputs/golds_clean.csv --out-curate-csv curate.csv --out-holdout-csv holdout.csv \
          --out-curate curate_ids.txt --out-holdout holdout_ids.txt
schema    --db $DB --datasets A,B --out schema.txt                # what the prompt backend is shown; or --tables-file catalog.txt
generate  --questions curate.csv --schema schema.txt --out arms/base.csv --reps 3     # baseline, BEFORE any context
          (your own agent instead: --backend command --command 'RUN' --runner-version X, no --schema)
score     --db $DB --predictions arms/base_rep1.csv --save-report reports/base_rep1.json   (x3)
                                                                   # the gold cache and checkpoint default under the workspace
triage    --report reports/base_rep1.json --show                  # what KIND of wrong; the largest bucket first
draft-rules --report reports/base_rep1.json --predictions arms/base_rep1.csv --ids curate_ids.txt --out context/rules_draft.md
                                                                   # the framework's first draft, each rule citing its questions
<a person reads the draft>                                        # the one step no command replaces
context-write --source-file context/rules_draft.md --to context/rules.md      # installed with history (v1, v2, ...)
                                                                   # round 2 onward: add --add, or this round replaces the last one
leak      --predictions holdout.csv --context context/rules.md    # the answers must not be inside
generate  --questions curate.csv --schema schema.txt --context context/rules.md --out arms/rules.csv --reps 3
score     ... --save-report reports/rules_rep1.json   (x3)
ablate    --without-report reports/base_rep{1,2,3}.json --with-report reports/rules_rep{1,2,3}.json --ids curate_ids.txt --show
                                                                   # the training reading
attribute --without-context arms/base_rep1.csv --with-context arms/rules_rep1.csv \
          --without-report ... --with-report ... --context context/rules.md   # which rule did it
climb     --round "r0=..." --round "r1=..." --show                # every round against the baseline, guarded

# the end, once: the two pinned versions over the reserved questions
generate  --questions holdout.csv --schema schema.txt --out arms/holdout_base.csv --reps 3
generate  --questions holdout.csv --schema schema.txt --context context/rules.md --out arms/holdout_rules.csv --reps 3
score     ... --save-report reports/holdout_base_rep1.json   (x3, and x3 for the rules arm)
ablate    --without-report reports/holdout_base_rep{1,2,3}.json --with-report reports/holdout_rules_rep{1,2,3}.json --show
                                                                   # the number; no --ids, and the reserved questions are read once
```

Optional arms, same measurement:

```
catalog-list  --project P --location us --out catalog.txt               # the tables the catalog holds (free)
catalog-pull  --project P --location us --tables-file catalog.txt --out served/ --pages docs/
                                                                         # served/: the bundle a reader gets (the arm); docs/: the overview, editable (free)
              # a custom entry group: every catalog-pull and catalog-push carries --entry-group G --entry-id '{dataset}__{table}'
              # (entry names are built from the key, never searched; the @bigquery default builds BigQuery's own shape)
draft-context --db $DB --tables-file catalog.txt --out docs/            # a page for every undocumented table (free)
catalog-push  --project P --location us --docs docs/ --rules context/rules.md --examples bank.json --dry-run   # the plan
catalog-push  ... (without --dry-run)                                    # writes, then reads every write back (free)
catalog-pull  --project P --location us --tables a.b,c.d --out docs_served/   # what a reader sees after the push
catalog-route --project P --location us --questions golds.csv --docs docs/ --catalog catalog.txt
                                                                          # per-question retrieval; writes context_per_question.json + routing.csv;
                                                                          # a routed table outside the list is dropped before the top-N cut and named
score-routing --predictions routing.csv --catalog catalog.txt            # the router graded on its own
generate      ... --context-per-question context_per_question.json       # the retrieval arm
generate      ... --retry --db $DB                                        # drafts checked (dry run) before they run; one second chance
curate-examples --pool curate.csv --targets holdout.csv --out bank.json   # the worked-example bank, leak-checked
                                                                          # --pool is where examples may come from, --targets what will be SCORED with
                                                                          # them in the prompt; the two must be disjoint. A question is never its own
                                                                          # example, so pool == targets builds an empty bank and says so. To build one
                                                                          # for curate-set iteration, split curate again and pool from one half.
attribute     --context context/rules.md ...                              # which rule broke or fixed which question
```

## Reading results for the user

- `ablate`: report the lift with its interval AND `context broke (N)` with
  the question ids. A positive lift with broken questions is not a clean
  win; say both. Repeat the sentence after the ruling as printed: for NOT
  PROVEN, the lift this many questions can prove and how many questions the
  lift seen would need; for NO CHANGE, check routing then triage; for a
  REAL LOSS, the broken ids first.
- `climb`: the ladder score only moves on a gain above the printed noise
  floor. If the raw rate rose but the ladder did not, say the gain is
  inside the noise - do not report it as an improvement.
- `gate` exits non-zero when golds are flagged; that is a finding, not an
  error. Report the defect classes.
- `attribute` suspects are leads, not verdicts. The confirmation is a
  leave-one-out arm: regenerate with the one suspect rule removed.
- `curate-examples` prints how many template twins it banned. Repeat that
  number to the user; a high count means the pool is close to the scored
  questions and transfer to novel questions will be smaller.
