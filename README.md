<div align="center">

<h1>contextlift</h1>

**Find out whether your business context makes an AI SQL agent more accurate.**

[![test](https://github.com/moutasemalakkad/context-lift/actions/workflows/test.yml/badge.svg)](https://github.com/moutasemalakkad/context-lift/actions/workflows/test.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

[Quickstart](docs/QUICKSTART.md) · [Documentation](docs/README.md) · [Examples](examples/README.md) · [Results](docs/CASE_STUDY.md)

<img src="docs/assets/demo.gif" alt="Terminal demo comparing SQL answers with and without one business rule." width="720">

*A toy warehouse with canned model answers; scoring and the paired test run locally.*

[View a still frame](docs/assets/demo-still.png) · [Run the demo](docs/QUICKSTART.md#try-the-toy-demo)

</div>

<a id="what-that-number-is"></a>

## Why contextlift?

An agent can read a schema and still miss what “revenue” means, which orders to
exclude, or how your team reports a rate. contextlift measures what those
written definitions change:

- **Compare answers:** run the same questions with and without context.
- **See the evidence:** accuracy lift, a paired confidence interval, and the
  questions context fixed or broke.
- **Find the next edit:** group failures and investigate the rules behind them.

Bring SQLite data or connect a warehouse and your agent. The
[full evaluation guide](docs/EVALUATE.md) covers setup, costs, and the review loop.

<a id="install"></a>

## Try it

Requires Python 3.10+, [uv](https://docs.astral.sh/uv/getting-started/installation/),
Git, and Make. Cloning and first-time setup need network access; the example
uses committed predictions and SQLite databases, with no API key or cloud calls.

```bash
git clone https://github.com/moutasemalakkad/context-lift
cd context-lift
make example-bird
```

The final comparison includes these results:

<!-- headline:example-bird -->
```
paired ablation on 39 questions (execution accuracy)
  without context :  30.8%  [18.6%, 46.4%]
  with context    :  61.5%  [45.9%, 75.1%]
  lift            : +30.8%  [+11.8%, +49.8%]
  context fixed   : 15 questions
  context broke   : 3 questions
  McNemar p       : 0.0075   significant at 0.05
  ruling          : REAL GAIN
```

**A gain with regressions:** 15 questions improved and 3 broke. This small
example uses one repetition per arm and no reserved holdout; it demonstrates
the workflow, not a new measurement of generalization. See the
[broken question IDs and qualifications](examples/bird/README.md#what-you-will-see).

Next: [finish the quickstart](docs/QUICKSTART.md) or follow the
[annotated walkthrough](docs/WALKTHROUGH.md).

## How it works

<picture>
  <source media="(max-width: 600px)" srcset="docs/assets/workflow-stacked.svg">
  <img src="docs/assets/workflow-wide.svg" alt="Prepare questions, establish a baseline, add context, compare answers, then inspect failures." width="860">
</picture>

<a id="what-gets-compared"></a>

An **arm** is the same questions answered under one condition. Compare SQL
results against a reviewed answer key, then compare the two arms. Different
SQL that returns the same answer can count as correct.

Use three repetitions per arm. Improve context on the working questions and
reserve a holdout for one final comparison. The
[methodology](docs/METHODOLOGY.md) explains the statistics and their limits.

Once you have a workspace, ask for the next step:

```bash
contextlift --workspace /path/to/evaluation next
```

<a id="everything-measured-so-far"></a>

## Measured results

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/results-dark.svg">
  <img src="docs/assets/results-light.svg" alt="Per-question accuracy lift: BIRD 3 databases +30.8 points, BIRD 11 databases +18.5, BEAVER +9.7; a BI model with no written descriptions +2.0, not proven. Intervals and qualifications follow in the results record." width="520">
</picture>

These studies measured gains from written context; the BI model without
written descriptions did not establish a gain. They use different question
sets and conditions, so their lift values are not a ranking of approaches.

[Full results, limitations, and reproduction commands](docs/CASE_STUDY.md) ·
[Chart data and sources](docs/assets/results.json)

<a id="repo-layout"></a>

## Documentation

| I want to… | Start here |
|---|---|
| Try a local example | [Quickstart](docs/QUICKSTART.md) |
| Evaluate my own data | [Evaluation guide](docs/EVALUATE.md) |
| Write useful business context | [Context-writing guide](docs/ONBOARDING.md) |
| Understand a verdict | [Methodology](docs/METHODOLOGY.md) |
| Browse all guides and research | [Documentation index](docs/README.md) |

For flags, use `contextlift --help` and `contextlift <command> --help`.
`contextlift --help-all` includes specialist commands.

## Contributing

```bash
make install
make test
```

After dependencies are installed, `make test` runs offline from a fresh clone
without credentials. See [Contributing](CONTRIBUTING.md) for development and
[Authoring](docs/AUTHORING.md) for adding a warehouse or agent.

Data handling: [Security and data](docs/SECURITY-AND-DATA.md).
Report vulnerabilities through [Security](SECURITY.md).

## License

Apache-2.0. Copyright 2026 Moutasem Akkad. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
