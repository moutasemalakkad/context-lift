# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`contextlift` measures whether curated business context makes a text-to-SQL agent
more accurate. It scores execution accuracy with confidence intervals, audits
the gold set, and tracks each round of context enrichment with a guard against
climbing on noise.

## Commands

```bash
make help            # list all targets
make install         # uv venv + editable install with [dev] (pytest only)
make test            # fast suite, about 20s, no cloud, no network
make example         # score + gate reports on the demo SQLite DB
make example-routing # routing report, without and with catalog snapping
make example-bird    # the whole loop on public BIRD data, offline
```

Run one test file or one test:

```bash
uv run --no-project pytest tests/test_compare.py -q
uv run --no-project pytest tests/test_compare.py::test_name -q
```

Gotchas on this machine:

- The default environment routes pip/uv through a private corp index that
  401s for this project. The Makefile forces `--index-url
  https://pypi.org/simple/`; do the same for any manual `uv pip install`.
- A manual `uv pip install` also needs `--python .venv/bin/python`. uv obeys a
  `VIRTUAL_ENV` inherited from another checkout over `./.venv`, and the install
  then lands in that other venv. The Makefile is safe because it exports
  `VIRTUAL_ENV := $(CURDIR)/.venv`; a bare command line is not.
- The venv breaks if the repo directory is renamed or moved (script shebangs
  carry absolute paths). Symptom: `uv run` cannot find pytest even though it
  is installed. Fix: `rm -rf .venv && make install`.

## Architecture

src layout; the package is `src/context_lift/`. Entry points: the `contextlift`
console script and `python -m context_lift`, both `cli.py:main`.

**Core is pure stdlib on purpose.** `dependencies = []` in pyproject. SQLite,
the comparator, metrics, and the gate run from a fresh clone with no
third-party packages. Anything cloud or LLM lives behind an optional extra
(`[bigquery]`, `[gemini]`, `[catalog]`, `[lookml]`) and is imported lazily
inside the function that needs it. Keep new dependencies behind extras too.

**The loop is the product.** Each stage is one module and one CLI command:

| stage | module | question it answers |
|---|---|---|
| `log` | backends_db.py | which query shapes people run; `--demand`: which datasets get the jobs, shapes and accounts |
| `semantic` | semantic.py | what rules did the BI model already write down? |
| `mine` | mine.py | where does a gold set come from? (candidates, not golds) |
| `rewrite-questions` | cli.py | the key was mined before a check existed: redraft its questions from the same SQL, no warehouse, no re-mining |
| `review` | review.py | the human step between mine and gate, as a stage (refuses while undecided); `--draft` lets a model accept or reject rows, and the verdict says so until a person re-reviews |
| `gate` | gate.py | is the answer key itself sound? |
| `routing-gate` | routing.py | is the routing answer key worth spending on? (one gold taking most questions, golds with one question, golds outside the catalog) |
| `split` | leakage.py | stable curate/holdout split (hash per id, not shuffle) |
| `schema` | backends_db.py | what schema text does the model see? (CREATE TABLE per table) |
| `profile` | profile.py | which values does each category column really hold, so the model stops guessing? |
| `generate` | generate.py | produce an arm's predictions (arms differ only by context) |
| `import` | cli.py | predictions made elsewhere, recorded as an arm in the workspace |
| `score` | harness.py | accuracy with a Wilson interval |
| `triage` | triage.py | what KIND of wrong were the failures? |
| `draft-rules` | rules.py | the first rules draft for one failure bucket, each rule citing its questions |
| `draft-context` | draft.py | the first document per table, from the schema, marked as a draft |
| `curate-examples` | examples.py | leak-checked worked-example bank (the strongest lever) |
| `context-write` | cli.py | install a reviewed draft as the working rules file, with version history |
| `leak` | leakage.py | does the context contain the answers? |
| `ablate` | ablate.py | what did the context buy overall? (paired, McNemar exact) |
| `climb` | ablate.py | what did each round buy against the same baseline, guarded against noise? |
| `attribute` | attribute.py | which context rule broke/fixed which question? |
| `score-routing` | routing.py | did the right table get picked? (no DB needed) |
| `catalog-list` | catalog.py | the tables a catalog entry group holds, by the parentEntry rule |
| `catalog-pull` | catalog.py | each table's document out of a live Knowledge Catalog, into files |
| `catalog-route` | catalog.py | which document does each question retrieve? (writes the per-question context generate reads) |
| `catalog-push` | push.py | reviewed pages, rules and examples into the catalog, every write read back |
| `next` | loop.py | where does the loop stand, and what is the one command to run next? |
| `index` | workspace.py | rebuild index.json from the run folders on disk |
| `pin` | workspace.py | override which arm counts as the baseline and which as the candidate |
| `bundle` | bundle.py | the pinned comparison as one static verdict.html, no script |
| `chat` | chat.py | the same loop as a REPL over the workspace, a model behind it when one is configured |

`contextlift next` gives the run order and why it matters (baseline before
curation; holdout scored once at the end).

**Scoring data flow** (`harness.evaluate`, the one call a library user makes):
Executor runs gold + predicted SQL → `compare.py` compares result sets under
SQL semantics (column-order independent, multiset unless the gold has ORDER
BY, float-tolerant, NULL-safe) → remaining disagreements optionally go to the
`judge.py` LLM tiebreaker → `metrics.py` wraps both rates in Wilson intervals.

Design constraints to preserve:

- `Executor` is a Protocol (`execute(sql) -> ExecResult`), not a base class.
  The harness, gate, and judge accept any object with that method.
- A judge backend is just `Callable[[str], str]` (`backends.py`). Tests use
  `echo_backend`; no network in the fast path, ever.
- The judge is one-sided: it only reviews comparator MISMATCHes, so it can
  only raise a score, never lower it. Documented in `docs/REFERENCE.md`.
- The comparator ignores column order by default (Spider/BIRD comparability).
  `column_order_matters=True` exists for business reporting. Do not "fix"
  this asymmetry; a past bug came from sorting cells within each row —
  compare whole-column arrangements, not per-row multisets.
- Predicted SQL is untrusted model output; executors enforce a read-only
  rail. Keep that in any new backend.
- `ablate`/`climb` pool repeated runs (temperature 0 is not reproducible) and
  use McNemar exact for paired comparison; `climb` warns without `--holdout`
  and reports a Blum-Hardt ladder score.

**Numbers in docs are computed, not typed.** Example output blocks in
README.md is exactly what `make example` /
`make example-routing` print, and `tests/test_readme_numbers.py`
recomputes the README's headline lift from the committed BIRD predictions. The
notebooks under `notebook/` are executed by `tests/test_notebooks.py`, so they
must stay runnable offline. If a change alters printed output, update the
README blocks and re-run the notebook tests.

`_personal/` is gitignored private notes; never commit it or quote it in
committed files.

## Tests

Every module has a mirror `tests/test_<module>.py`. Test names are sentences;
bodies are arrange→act→assert with literal data. The fast suite must stay
cloud-free: fake backends, tmp SQLite DBs, no credentials.
