.PHONY: help install test test-integration scan example example-routing example-kaggledbqa example-thelook example-thelook-seed example-beaver example-bird example-bird-enterprise clean

# Force public PyPI. The default environment here routes pip/uv through a
# private corp index (Airlock, us-python.pkg.dev) that 401s for this project
# and can't see public packages like pytest. --no-project on run/test avoids
# uv locking the optional cloud extras (bigquery/gemini) for the fast path.
INDEX := https://pypi.org/simple/
# Pin the interpreter. With another project's venv active, VIRTUAL_ENV wins and
# every target runs against it: `make example` died with "No module named
# contextlift" naming a python in an unrelated repo. Found in the field.
export VIRTUAL_ENV := $(CURDIR)/.venv

help: ## show available targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  make does things to the repo: install, test, clean, run a canned demo."
	@echo "  contextlift does things to a workspace: every stage of the loop."
	@echo "  \033[36mcontextlift --help\033[0m           every command"
	@echo "  \033[36mcontextlift --workspace DIR next\033[0m   where the loop stands, and the next command"

install: ## create .venv and install the package + dev deps (no cloud extras)
	uv venv --allow-existing
	uv pip install --index-url $(INDEX) -e ".[dev]"
	@echo ""
	@echo "  now run:  source .venv/bin/activate"
	@echo "  the make targets go through \`uv run\` and do not need it; the"
	@echo "  \`contextlift\` command does."

example-thelook:  ## recompute the committed thelook round, offline and free
	@python3 examples/thelook/reproduce.py

example-beaver:  ## tour the enterprise result and its guardrails (offline, free)
	@python3 examples/beaver/quickstart.py

golds-thelook:  ## rebuild the thelook gold set from the committed seed queries
	@python3 examples/thelook/make_golds.py

examples-thelook:  ## rebuild the thelook worked-example bank from the working questions (free)
	@.venv/bin/contextlift build-examples \
		--golds examples/thelook/golds.csv \
		--out examples/thelook/spec/examples/bank.jsonl

# `| .venv` is an order-only prerequisite: build the venv first if it is
# absent, and never rebuild this target just because the venv is newer.
#
# WHY IT IS HERE. The README says "make test runs green from a fresh clone
# with no credentials", and until 2026-09-12 that was false. `make test` alone
# on a fresh clone printed `error: Failed to spawn: pytest`, because
# --no-project uses the ambient .venv and nothing had created one. It worked
# on this machine forever, since .venv had existed since the first day. A
# fresh clone is the only place that bug is visible, which is the whole
# argument for testing from one.
#
# Fixed here rather than by adding "run make install first" to the README: a
# person who has to know the order is a person the tool has failed.
test: | .venv ## run the fast test suite (pure stdlib + fakes, no cloud)
	uv run --no-project pytest -q

.venv:
	$(MAKE) install

scan: ## refuse a customer string: no flag scans your working tree, TREE=1 scans every tracked file as it stands, SINCE=REF scans a range, ALL=1 scans all history
	uv run --no-project python scripts/scan_for_secrets.py $(if $(ALL),--all,)$(if $(TREE),--tree,)$(if $(SINCE),--since $(SINCE),)

test-integration: ## the live catalog tests; needs ADC, the [catalog] extra, and CONTEXTLIFT_CATALOG_PROJECT/_LOCATION/_TABLE (free)
	@test -n "$$CONTEXTLIFT_CATALOG_PROJECT" || { echo "set CONTEXTLIFT_CATALOG_PROJECT, CONTEXTLIFT_CATALOG_LOCATION and CONTEXTLIFT_CATALOG_TABLE (a catalogued dataset.table); needs Application Default Credentials"; exit 2; }
	uv run --no-project pytest -q tests/test_catalog_live.py

example: ## build the example SQLite DB and print score + gate reports
	uv run --no-project python examples/demo/build_db.py
	uv run --no-project python -m context_lift score \
		--db examples/demo/demo.db --predictions examples/demo/predictions.csv
	# `-` so make ignores the gate's non-zero exit: the example golds trip
	# defects on purpose, and the point is to print them, not fail the demo.
	-uv run --no-project python -m context_lift gate \
		--db examples/demo/demo.db --golds examples/demo/golds.csv

example-routing: ## score the routing example twice: without and with the catalog snap
	# Without a catalog the typo in r5 is a plain miss; with one it snaps and
	# the report says so. Both runs to show the flag changes the number.
	uv run --no-project python -m context_lift score-routing \
		--predictions examples/demo/routing_predictions.csv
	@echo
	uv run --no-project python -m context_lift score-routing \
		--predictions examples/demo/routing_predictions.csv \
		--catalog examples/demo/routing_catalog.txt

example-kaggledbqa: ## context lift on two real Kaggle DBs, offline from committed predictions
	cd examples/kaggledbqa && uv run --no-project python prepare.py
	@for db in StudentMathScore GeoNuclearData; do \
		echo "=== $$db: baseline vs the dataset's own docs (3 reps pooled) ==="; \
		uv run --no-project python -m context_lift ablate \
		  --db examples/kaggledbqa/$$db.sqlite --timeout 60 --show \
		  --without-context examples/kaggledbqa/predictions/$$db/pred_base_rep1.csv \
		  --without-context examples/kaggledbqa/predictions/$$db/pred_base_rep2.csv \
		  --without-context examples/kaggledbqa/predictions/$$db/pred_base_rep3.csv \
		  --with-context examples/kaggledbqa/predictions/$$db/pred_docs_rep1.csv \
		  --with-context examples/kaggledbqa/predictions/$$db/pred_docs_rep2.csv \
		  --with-context examples/kaggledbqa/predictions/$$db/pred_docs_rep3.csv; \
	done

example-thelook-seed: ## run examples/thelook/queries.sql on BigQuery so the query log has analyst queries to mine (needs PROJECT=..., about 0.02 USD)
	python3 examples/thelook/run_queries.py --project $(PROJECT) examples/thelook/queries.sql

example-bird: ## the whole loop on public BIRD data, offline (no key, no network)
	uv run --no-project python examples/bird/run.py

example-bird-enterprise: ## tour the fresh 11-database BIRD result (offline, free)
	@python3 examples/bird/enterprise_quickstart.py

probe-glossary-tiers:  ## can a catalog term live under a team category? needs PROJECT= and ADC
	@test -n "$(PROJECT)" || { echo "usage: make probe-glossary-tiers PROJECT=your-project"; exit 1; }
	@uv run python scripts/probe_glossary_tiers.py --project $(PROJECT) \
		--out docs/evidence/glossary-tiers.json

answer-cost:  ## what the agent's own SQL cost, from the job ids a run records
	@test -n "$(RUN)" -a -n "$(PROJECT)" || { echo "usage: make answer-cost RUN=run-rows.csv PROJECT=your-project"; exit 1; }
	@uv run python scripts/answer_cost.py "$(RUN)" "$(PROJECT)"

classes-thelook:  ## what each class of context is worth, and what each costs (needs PROJECT= and ADC; RESUME=file.ckpt replays a paid run for free)
	@test -n "$(PROJECT)" || { echo "usage: make classes-thelook PROJECT=your-project [RESUME=/tmp/classes.ckpt]"; exit 1; }
	@uv run contextlift classes --spec examples/thelook/spec \
		--golds examples/thelook/golds.csv \
		--db bq://$(PROJECT) --project $(PROJECT) --cost --run \
		--budget $(or $(BUDGET),20) --yes \
		--checkpoint $(or $(RESUME),/tmp/classes.ckpt) \
		2>&1 | tee logs/classes-run.log

check-board:  ## check the planning board against the code (needs CHECKLIST=path)
	@test -n "$(CHECKLIST)" || { echo "usage: make check-board CHECKLIST=../kc_harness/docs/checklist.md"; exit 1; }
	@CONTEXTLIFT_CHECKLIST="$(CHECKLIST)" uv run pytest -q tests/test_checklist_matches_the_code.py

results-chart:  ## redraw the README's results chart from docs/assets/results.json
	@uv run python scripts/make_results_chart.py

demo-gif:  ## re-record the README's terminal demo (needs: brew install vhs ttyd ffmpeg)
	@command -v vhs >/dev/null || { echo "vhs not found: brew install vhs ttyd ffmpeg"; exit 1; }
	@PATH="$(PWD)/.venv/bin:$$PATH" vhs docs/assets/demo.tape
	@ffmpeg -hide_banner -loglevel error -y -i /tmp/contextlift-demo-full.gif -filter_complex 'crop=960:505:0:0,split[a][b];[a]palettegen[p];[b][p]paletteuse' -loop 0 docs/assets/demo.gif
	@$(MAKE) demo-still
	@ls -lh docs/assets/demo.gif | awk '{print "wrote docs/assets/demo.gif ("$$5")"}'

clean: ## remove venv, caches, and scratch example DBs
	rm -rf .venv .pytest_cache **/__pycache__ *.egg-info examples/*.db

.PHONY: demo-still
demo-still: ## extract a still frame from the README recording (needs ffmpeg)
	ffmpeg -hide_banner -loglevel error -y -ss 8 -i docs/assets/demo.gif -frames:v 1 docs/assets/demo-still.png

.PHONY: readme-workflow
readme-workflow: ## redraw the responsive README workflow SVGs
	python3 scripts/make_readme_workflow.py
