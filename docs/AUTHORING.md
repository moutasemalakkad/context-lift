# Adding a warehouse or an agent

[Documentation](README.md) / Contribute / Add a warehouse or agent

Two things are pluggable, and both are **structural protocols**: you satisfy
them by having a method with the right shape. No import, no base class, no
registration. A class in your own repository qualifies.

This page says what a new one must do and, more importantly, what it must not.

## A warehouse: `Executor`

```python
class Executor(Protocol):
    def execute(self, sql: str) -> ExecResult:
        ...
```

That is the whole interface. `ExecResult` carries the rows, the column names
and an error class when the statement failed.

Shipped today: SQLite, DuckDB, BigQuery. The first two need nothing; BigQuery
is in the `[bigquery]` extra.

### What a new executor must not do

**It must not let a write through.** The refusal list is in `executor.py` and
is deliberately blunt: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`,
`MERGE`, `GRANT`, `REVOKE`, `ATTACH`, `DETACH`, `VACUUM`, `PRAGMA`, and
`TRUNCATE`/`REPLACE` used as statements rather than as scalar functions. The
keywords are refused **anywhere** in the statement, not only at the start,
because `WITH t AS (DELETE ...)` writes. A `SELECT` with the literal word
`DROP` inside a string literal is refused too. That is a false refusal, it is
known, and it is the safe direction: this SQL was written by a language model.

**It must not invent a row.** If the engine returns nothing, return nothing. A
backend that substitutes an empty result for an error makes a failed answer
look like a correct empty answer, and the round will score it as such.

**It must not silently truncate.** If you cap rows, the cap has to reach the
result, because a prediction cut at the cap and a gold that was not is a
comparison between two different questions.

**It must not reach outside the engine.** `EXTERNAL_QUERY`, `AI.*` and the
remote `ML.*` functions are refused. They run somewhere else or send rows to a
model, which is both a cost and a data-exfiltration surface.

### What it should do

Report bytes billed if the engine knows them. `round --cost` reads the job
ledger for BigQuery; an engine that can tell you what a query cost makes the
cost line work without a second lookup.

## An agent: `Runner`

```python
class Runner(Protocol):
    name: str

    def cost_sheet(self, n_questions: int, n_arms: int, k: int) -> CostSheet: ...

    def answer(self, question_id: str, question: str, arm: Arm) -> Prediction: ...
```

`answer` receives the **whole** context for one arm — the `Arm` object — and
returns a `Prediction` carrying the SQL, the job id if there is one, and an
`error_class` when the agent returned no SQL at all.

Shipped today: the Conversational Analytics API, and a CSV runner that replays
predictions somebody else produced, which is how a round is reproduced offline
with no key and no network.

### What a new runner must not do

**It must not see the gold SQL.** Ever. The `answer` signature does not receive
it, and that is on purpose. A runner that found the answer key another way
would produce a lift that means nothing, and nothing downstream could tell.

**It must not treat a refusal as a wrong answer, or the reverse.** Set
`error_class` and leave `pred_sql` as `None` when the agent produced no SQL. A
refusal and a wrong answer are different outcomes; collapsing them hides which
one happened, and the failure-shape report depends on the difference.

**It must not price what it cannot price.** `cost_sheet` returns what the
**model calls** cost. It cannot include the SQL the agent writes, because that
query does not exist until the run does. Say so rather than guessing — the
estimate already prints that sentence, and a runner that quietly folded in a
guessed query cost would make the estimate look complete when it is not.

**It must not retry silently.** Expose a `retries` count. A round that needed
forty retries to finish is a fact about the run.

## Both

**Carry an identity.** Any Google client gets `client_info=identity.client_info()`.
A test in this repository fails on one that does not, because usage nobody can
attribute back is usage nobody can support.

**Fail loudly and locally.** An exception with the question id in it beats a
`None` that surfaces four stages later as a missing row.

## Testing yours

Neither protocol needs a live service to test. The suite drives whole rounds
with a plain function as the runner and an in-memory SQLite database as the
executor — see `tests/test_round.py`. If your backend can only be tested
against a live service, that is worth fixing before it is worth shipping.

[Documentation index](README.md) · Next: [Contributing](../CONTRIBUTING.md)
