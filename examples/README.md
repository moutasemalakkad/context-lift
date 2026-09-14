# Examples, smallest first

| dir | what | needs |
|---|---|---|
| [demo/](demo) | score + gate + routing on a tiny generated SQLite DB (`make example`, `make example-routing`) | nothing |
| [bird/](bird) | public BIRD data: the small offline loop (`make example-bird`) and a fresh 11-database targeted-context result (`make example-bird-enterprise`) | nothing for either result tour; model key + ~1.5 GB download to rerun the large study |
| [kaggledbqa/](kaggledbqa) | context WORKING: two real Kaggle DBs where the dataset's own docs lift accuracy, one significantly (`make example-kaggledbqa`) | nothing |
| [thelook/](thelook/ANALYST_ASSESSMENT.md) | a quick public BigQuery e-commerce assessment with a held-out committed verdict | nothing to reproduce; BigQuery to rerun |
| [beaver/](beaver/QUICKSTART.md) | the recommended complex test: instant decision tour (`make example-beaver`), significant fresh-holdout lift, and [full reproduction](beaver/LATEST_RUNBOOK.md) | nothing for tour; gated download + model key to rerun |

Start with demo to see the report formats, kaggledbqa to watch context
actually move a number, bird to see the whole loop's reasoning, theLook for a
quick BigQuery smoke test, and BEAVER for the closest public proxy to a complex
enterprise warehouse.

The cross-benchmark conclusions—including negative TPC-DS results and why
selective context beat blanket context—are in
[`CONTEXT_RESEARCH.md`](CONTEXT_RESEARCH.md).
