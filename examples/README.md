# Examples

[Documentation](../docs/README.md) / Examples

**Start with BIRD:** `make example-bird` runs the small SQLite example used in
the [Quickstart](../docs/QUICKSTART.md). It uses committed model predictions;
replaying a result does not generate a fresh measurement.

## Choose an example

Run these commands from the repository root. Initial cloning and environment
setup need network access; the local tours below do not call models or clouds.

| Example | Local command | What it shows | To generate fresh predictions |
|---|---|---|---|
| [BIRD, small](bird/README.md) | `make example-bird` | Gate, baseline, triage, context comparison; one repetition and no reserved holdout | See the BIRD README |
| [BIRD, enterprise](bird/ENTERPRISE_RUNBOOK.md) | `make example-bird-enterprise` | A recorded 11-database study, coverage limits, and regressions | Model key and benchmark download |
| [theLook](../docs/WALKTHROUGH.md) | `make example-thelook` | An annotated comparison on a public warehouse | BigQuery access and model backend |
| [BEAVER](beaver/QUICKSTART.md) | `make example-beaver` | A recorded enterprise-warehouse result and its guardrails | Gated data download and model key |
| [KaggleDBQA](kaggledbqa/README.md) | `make example-kaggledbqa` | Context effects on two small databases | See the dataset README |

## Reproduce a larger study

- BIRD: [enterprise runbook](bird/ENTERPRISE_RUNBOOK.md).
- BEAVER: [latest runbook](beaver/LATEST_RUNBOOK.md); the older
  [runbook](beaver/RUNBOOK.md) records an earlier campaign.
- theLook: [assessment](thelook/ANALYST_ASSESSMENT.md) and
  [context spec](thelook/spec/README.md).

## Templates and supporting experiments

- [Dataset documentation template](onboarding/TEMPLATE.md) and
  [completed example](onboarding/EXAMPLE-thelook.md).
- [Context research](CONTEXT_RESEARCH.md): cross-benchmark findings, including
  negative and inconclusive results.
- [BIRD perturbations](bird/perturbation/README.md): effects of changing context.
- [LookML arm](thelook/lookml-arm/README.md): extracting a semantic model.
- `examples/demo/`: tiny fixtures used by `make example` and
  `make example-routing` to show report formats.

[Documentation index](../docs/README.md) · Next: [Evaluate your data](../docs/EVALUATE.md)
