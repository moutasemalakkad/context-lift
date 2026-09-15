# Examples

Start with **BIRD small**, the example in [guide stage 1](../docs/README.md#1-install-and-try-the-example).
Run the commands below from the repository root. Cloning and initial setup
need network access; replaying these committed results makes no model calls.

| Benchmark | Local command | What is verified |
|---|---|---|
| [BIRD](bird/README.md) | `make example-bird` | Executes committed SQLite queries and compares two arms |
| [BIRD enterprise](bird/README.md#enterprise-study) | `make example-bird-enterprise` | Checks the larger study's recorded summary |
| [BEAVER](beaver/README.md) | `make example-beaver` | Checks the recorded enterprise-study summary |
| [theLook](thelook/README.md) | `make example-thelook` | Recomputes a verdict from recorded answer-level outcomes |
| [KaggleDBQA](kaggledbqa/README.md) | `make example-kaggledbqa` | Executes a small public-data comparison |

Each benchmark README is its entry point for results, limitations, and full
reproduction. Replaying a receipt does not independently validate its SQL;
new generation may need data downloads, credentials, and a budget.

The [research archive](../docs/research/README.md) compares the findings.
To evaluate your own workload, continue the [guide](../docs/README.md#2-choose-a-workload).

## Supporting resources

- [Dataset template](onboarding/TEMPLATE.md) and [completed example](onboarding/EXAMPLE-thelook.md), used in guide stage 6.
- [BIRD perturbation artifacts](bird/perturbation/README.md), for the archived defect experiment.
- `examples/demo/`: tiny fixtures used by `make example` and `make example-routing`.
