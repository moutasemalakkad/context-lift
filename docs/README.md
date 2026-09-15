<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com> -->

# Documentation

**New here?** [Try the offline example](QUICKSTART.md), then
[evaluate your own data](EVALUATE.md). You do not need to read every guide.

## Start here

| Guide | What you will get |
|---|---|
| [Quickstart](QUICKSTART.md) | One local example, expected output, and your next step |
| [Walkthrough](WALKTHROUGH.md) | An annotated public experiment, including its inconclusive first round |
| [Examples](../examples/README.md) | Small local demos and larger benchmark reproductions |

## Use your data

| Guide | What you will get |
|---|---|
| [Evaluate your data](EVALUATE.md) | SQLite and BigQuery setup, the measurement loop, and troubleshooting |
| [Write business context](ONBOARDING.md) | A dataset template and a checklist for reviewing rules |
| [Context sources and catalogs](CONTEXT-SOURCES.md) | Where rules, examples, descriptions, and terms belong |
| [Security and data](SECURITY-AND-DATA.md) | What each backend sends, stores, and requires access to |
| [Run identity and job labels](ATTRIBUTION.md) | How to identify framework calls in service logs |

## Understand results

| Guide | What you will get |
|---|---|
| [Methodology](METHODOLOGY.md) | Execution accuracy, gates, exclusions, intervals, and statistical limits |
| [Rule attribution](METHODOLOGY.md#6b-attribution-from-broke-3-to-this-rule-broke-it) | How to investigate which rule helped or hurt a question |
| [Practices and their evidence](PRACTICES.md) | Lessons from experiments, with the measurements behind each |

## Research and examples

These are records of particular runs. Use the evaluation guide for a new run;
historical costs, timings, and commands describe the conditions at the time.

| Record | Contents |
|---|---|
| [Case studies](CASE_STUDY.md) | Results, regressions, inconclusive findings, and committed evidence |
| [Field notes](FIELD-NOTES.md) | Operational findings moved out of the tutorials |
| [Context research](../examples/CONTEXT_RESEARCH.md) | Cross-benchmark experiments and conclusions |
| [Cold-run protocol](COLD-RUN.md) | How to evaluate a previously unseen dataset |

## Contribute

- [Contributing](../CONTRIBUTING.md): setup, tests, and documentation conventions.
- [Add a warehouse or agent](AUTHORING.md): executor and runner contracts.
- [Roadmap](ROADMAP.md): priorities and project boundaries.
- [Decision log](decisions.md): historical implementation decisions.
- [Assistant operating instructions](../AGENTS.md): the CLI workflow for AI assistants.

## Command reference

`contextlift --help` lists the main commands. `contextlift --help-all` includes
specialist commands; `contextlift <command> --help` describes that command's
inputs, outputs, and flags. The CLI is the canonical flag reference.

## Repository map

| Directory | Purpose |
|---|---|
| `src/context_lift/` | CLI and Python package |
| `tests/` | Tests and committed fixtures |
| `examples/` | Datasets, predictions, reproduction scripts, and runbooks |
| `docs/` | Guides and research records |
| `docs/assets/`, `docs/evidence/` | Visual assets and source evidence |
| `scripts/` | Supporting tools and visual generators |
| `deploy/` | Cloud Run deployment support |

[Back to the project](../README.md)
