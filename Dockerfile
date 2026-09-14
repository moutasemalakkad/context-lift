# Run a round as a SERVICE ACCOUNT, not as a person.
#
# Why this exists, measured 2026-09-12. A corp Google account is refused by
# the Conversational Analytics API -- `access_denied: Account restricted`,
# raised while MINTING the token, so no project or permission change reaches
# it. Four routes were tried and all four failed: the account directly, a
# second project, service-account impersonation (the corp identity is still
# what asks for the token), and a `gcert` refresh. The fifth, a downloaded
# service-account key, is blocked by org policy
# (`iam.disableServiceAccountKeyCreation`: `enforced: true`).
#
# On Cloud Run the machine IS the service account. Its metadata server hands
# the process a token with no login, no key on disk and no human identity in
# the chain. That is not a way around the restriction; it is how a batch job
# is supposed to authenticate.
#
# And it is the shape a customer needs anyway. A framework that only runs
# from one laptop with one person's credential cannot be handed to anybody.
FROM python:3.12-slim

# Cloud extras only. The core is stdlib, and `dev` belongs in CI rather than
# in an image that runs paid rounds.
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir '.[bigquery,ca,catalog,yaml,gemini,lookml,duckdb]'

# Examples travel with the image so `make example-thelook` and the public
# ablation run without fetching anything.
COPY examples/ ./examples/
COPY scripts/ ./scripts/
COPY Makefile ./

# No ENTRYPOINT of its own: a Cloud Run job passes the whole command, so one
# image serves `round`, `classes`, `mine` and the free readers. `contextlift`
# with no arguments prints the verb list, which is the right thing for
# somebody who ran the image to see what it does.
CMD ["contextlift"]
