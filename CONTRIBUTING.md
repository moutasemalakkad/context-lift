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

Both are structural protocols — you satisfy them by having the right method,
with no import and no base class. See [Authoring](docs/AUTHORING.md) for what a new one
must do and, more importantly, what it must not.

## Style

Match the file you are editing. Comments in this codebase tend to explain
*why*, often with the measurement or the incident that forced the decision,
because those are the things a reader cannot recover from the code.

Line length 88. No formatter is enforced.

## Documentation

- Keep the README focused on what the tool does, one runnable example, and
  where to go next. Add every reader-facing guide to the [docs index](docs/README.md).
- A guide has one job: purpose, prerequisites, steps, expected result, and a
  next-step link. Link to canonical instructions instead of copying them.
- Keep current instructions in guides and dated experiments in case studies
  or field notes. Preserve source files, caveats, and reproducibility links.
- Check command flags against CLI help. Use a workspace, estimate paid calls,
  bound SQL execution, and keep reserved answers out of curation.
- Add a contents list to long references. Preserve old heading anchors when
  moving sections, and update relative links from the new location.
- Keep images legible in light/dark themes and at narrow widths; provide alt
  text and text equivalents for important facts. Figures come from committed
  data. Run `make results-chart` after changing the chart generator, and
  `make readme-workflow` after changing the workflow generator.
- The demo is a recording with canned model answers. `make demo-gif` records
  it again; `make demo-still` extracts the linked still frame with ffmpeg.
- Validate changed links, commands, visuals, and the existing tests that pin
  quoted numbers. Do not relax evidence checks to make an editorial edit pass.

## Licence

Apache-2.0. By contributing you agree your contribution is licensed under it.
New files carry the SPDX header the existing files use.
