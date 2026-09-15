# Contributing

Thanks for looking. This is a measurement tool, so the bar for a change is a
little different from most repositories: **a number it prints has to be one
somebody can recompute.**

## Run the tests first

Installation downloads dependencies. The test suite then runs offline,
without a cloud account, credentials, or an API key.

```
make install    # creates .venv with uv, installs the package and dev deps
make test       # the full suite
```

That is the whole setup. If `make test` needs a network connection on your
machine, that is a bug and a good first issue.

The cloud pieces are optional extras (`[catalog]`, `[bigquery]`) and their
tests skip loudly when the extra is absent — a skip that does not say what is
missing is itself a defect here.

## What a good change looks like

**A bug fix comes with the command that showed the bug.** Every real defect
found in this project was found by running something, not by reading. The
issue template asks for the command and its output for that reason.

**A number needs a committed source.** If your change makes the tool print a
figure, a test has to recompute that figure from a committed file. Three
numbers once reached this project's README with nothing behind them, and the
fix was not better proofreading — it was `docs/assets/results.json`, where
every row names the file it came from and the test that pins it.

**Tests are thin and deliberate, not per-commit.** Write one when a wrong
result would be silent — a parser, a metric, anything that shapes data — or to
pin a bug you just hit. For the rest, a visible end-to-end run is the
verification. `make test` stays fast and cloud-free.

**Say what a change does not establish.** Most docs here end with a short "what
this does not show" paragraph. That is not modesty, it is the product: a tool
that overstates its own results cannot be used to check anyone else's.

## Things that will be sent back

- A number with no run behind it.
- A guard that cannot be enforced. If the tool cannot check it, do not name it
  like a rule — write it down as a convention instead.
- A new dependency for something the standard library does.
- An abstraction with one implementation.
- LLM attribution in a commit or a pull request. No `Co-Authored-By` naming an
  assistant, no tool names, no emoji marking generated work.

## Adding a warehouse or an agent

Implement the structural protocol: a class with the right methods needs no
base class or registration. The definitions live in
[executor.py](src/context_lift/executor.py) and
[runners](src/context_lift/runners/__init__.py).

```python
class Executor(Protocol):
    def execute(self, sql: str) -> ExecResult: ...

class Runner(Protocol):
    name: str
    def cost_sheet(self, n_questions: int, n_arms: int, k: int) -> CostSheet: ...
    def answer(self, question_id: str, question: str, arm: Arm) -> Prediction: ...
```

An executor returns rows, columns, and an error when execution fails. Preserve
empty results, errors, timeouts, and row-cap exclusions as distinct outcomes.
Reuse the read-only guard, prevent engine/filesystem escapes, and never silently
truncate or turn an error into an empty answer. Report billed bytes when known.
SQLite ships in the standard library; DuckDB and BigQuery use optional extras.

A runner receives the whole `Arm`, never gold SQL. Return no SQL plus an
`error_class` for a refusal; do not invent a prediction. Expose retries, job
identity, and matched-example metadata when available. Estimate model costs
honestly and state unknown query costs separately. The prompt, CA, and CSV
runners show the available paths. A command-runner integration is described in
[the reference](docs/REFERENCE.md#agent-backends).

Google clients use `client_info=identity.client_info()`. Fail with the relevant
question ID. Test the contract offline with an in-memory executor and a fake
runner; [test_round.py](tests/test_round.py) provides examples.

## Project scope and roadmap

The project measures one intervention: whether business context improves a
text-to-SQL agent. Useful contributions include more warehouses and agents,
better checks for invalid comparisons, and reproducible sources for every
reported number. PostgreSQL and additional cloud warehouses need suitable
backend tests. Larger independent corpora and production feedback need real
source data and a serving integration to evaluate.

These proposals remain closed unless new evidence changes the decision:

- **A general benchmark or observability platform**, including framework
  adapters with no measured need, would broaden the project beyond its purpose.
- **A `knowledge.yaml` schema** duplicates the file conventions and their lint
  checks; maintain one representation.
- **An `architect` verb** duplicates `spec-push` and its dry-run plan/drift view.
- **An `ask` verb** needs demonstrated demand for another interaction surface.

Open an issue with a concrete use case before implementing a scope expansion.

## Style

Match the file you are editing. Comments in this codebase tend to explain
*why*, often with the measurement or the incident that forced the decision,
because those are the things a reader cannot recover from the code.

Line length 88. No formatter is enforced.

## Documentation

Maintain three reader-facing pages rather than adding another guide:

- [The guide](docs/README.md): the continuous eight-stage workflow. Every stage
  uses Purpose, Inputs, Commands, Expected output, Best practices, Ready to
  continue, and Next stage. Keep SQLite on the main path and alternatives in
  adjacent expandable sections.
- [The reference](docs/REFERENCE.md): technical behavior, flags, integrations,
  and data handling. Link from the relevant stage; do not duplicate its steps.
- [The research archive](docs/research/README.md): dated measurements,
  source artifacts, qualifications, and corrections. Preserve unsuccessful
  results and evidence when trimming narrative.

The root README introduces the tool and points to the guide. Each benchmark
has one README entry point; keep reproduction details with its artifacts.
Dataset templates and evaluated context files are resources, not extra guides.

Check flags against CLI help and use one workspace through an example. Estimate
paid calls, bound SQL, and keep reserved questions out of curation. When moving
content, update repository links and tests to its canonical location; remove
superseded pages rather than adding navigation stubs.

Keep visuals legible in light/dark themes and at narrow widths, with alt text
and text equivalents. Figures use committed sources. `make results-chart`
regenerates the research chart; `make readme-workflow` regenerates the overview.
`make demo-gif` records the toy demo and `make demo-still` extracts its poster.
Validate links, commands, and rendered pages, including expanded alternatives.
Keep numerical evidence checks intact when relocating a claim.

## Licence

Apache-2.0. By contributing you agree your contribution is licensed under it.
New files carry the SPDX header the existing files use.
