#!/usr/bin/env bash
# Build the image and run one contextlift command as a SERVICE ACCOUNT.
#
#   deploy/cloud_run_job.sh build
#   deploy/cloud_run_job.sh run classes --spec examples/thelook/spec ...
#
# WHY THIS EXISTS, measured 2026-09-12. A corp Google account is refused by
# the Conversational Analytics API with `access_denied: Account restricted`,
# raised while minting the token. Five routes were tried:
#
#   the account directly                 Account restricted
#   a second project                     Account restricted (account, not project)
#   service-account impersonation        Account restricted (the corp identity
#                                        is still what asks for the token)
#   a gcert refresh                      Account restricted (OAuth, not LOAS)
#   a downloaded service-account key     org policy: enforced: true
#
# On Cloud Run the machine IS the service account. Its metadata server hands
# the process a token with no login, no key on disk and no human in the chain.
#
# It is also the shape a customer needs. A framework that runs only from one
# laptop with one person's credential cannot be handed to anybody, and a
# round that takes an hour should not die when a laptop sleeps.
set -euo pipefail

# Not `:?` here: running this with no arguments should print the help, not
# fail on a variable the reader has not been told about yet.
PROJECT="${CONTEXTLIFT_RUN_PROJECT:-}"
REGION="${CONTEXTLIFT_RUN_REGION:-us-central1}"
SA="${CONTEXTLIFT_RUN_SA:-}"
REPO="${CONTEXTLIFT_RUN_REPO:-contextlift}"

need() {
  [ -n "$PROJECT" ] || { echo "set CONTEXTLIFT_RUN_PROJECT" >&2; exit 2; }
  [ -n "$SA" ] || { echo "set CONTEXTLIFT_RUN_SA, e.g. runner@$PROJECT.iam.gserviceaccount.com" >&2; exit 2; }
}
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/contextlift:latest"
JOB="${CONTEXTLIFT_RUN_JOB:-contextlift-job}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "${1:-}" in

build)  # Cloud Build, so no local Docker daemon is needed. Reads
        # .dockerignore, which excludes _personal/ -- customer data must
        # never be built into an image.
  need
  gcloud artifacts repositories describe "$REPO" \
      --location="$REGION" --project="$PROJECT" >/dev/null 2>&1 || \
    gcloud artifacts repositories create "$REPO" \
      --repository-format=docker --location="$REGION" --project="$PROJECT" \
      --description="contextlift evaluation images"
  gcloud builds submit "$HERE" --tag "$IMAGE" --project="$PROJECT"
  ;;

run)    # Everything after `run` is the contextlift command line. One image
        # serves every verb, so this takes the command rather than wrapping
        # one of them.
  need
  shift
  [ $# -gt 0 ] || { echo "usage: $0 run <contextlift args...|script.py args...>" >&2; exit 2; }
  # A stage under scripts/ is as much a part of this pipeline as a CLI verb,
  # and several of them are the only way to run a step (spec_schema,
  # attribute_committed, regate_literals). So one job serves both: a first
  # argument ending in .py runs under python, anything else is a contextlift verb.
  case "$1" in
    *.py) CMD=python ;;
    *)    CMD=contextlift ;;
  esac
  gcloud run jobs describe "$JOB" --region="$REGION" --project="$PROJECT" \
      >/dev/null 2>&1 && VERB=update || VERB=create
  gcloud run jobs "$VERB" "$JOB" \
      --image="$IMAGE" --region="$REGION" --project="$PROJECT" \
      --service-account="$SA" \
      --task-timeout=3600s --max-retries=0 --memory=2Gi --cpu=2 \
      --set-env-vars=PYTHONUNBUFFERED=1 \
      --command="$CMD" --args="$(IFS=,; echo "$*")"
  gcloud run jobs execute "$JOB" --region="$REGION" --project="$PROJECT" --wait
  ;;

logs)
  gcloud logging read \
    "resource.type=cloud_run_job AND resource.labels.job_name=$JOB" \
    --project="$PROJECT" --limit="${2:-80}" --format='value(textPayload)'
  ;;

whoami) # Prove the job runs as the SA and not as a person. This is the whole
        # point of the exercise, so it gets its own verb.
  echo "would run as: $SA"
  gcloud run jobs describe "$JOB" --region="$REGION" --project="$PROJECT" \
      --format='value(template.template.serviceAccount)' 2>/dev/null \
    || echo "  (job not created yet)"
  ;;

*)
  # Every leading comment line, so the header cannot drift out of
  # range when the code above it moves.
  awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' \
      "${BASH_SOURCE[0]}"
  echo
  echo "verbs: build | run <args...> | logs [n] | whoami"
  echo
  echo "  CONTEXTLIFT_RUN_PROJECT  required"
  echo "  CONTEXTLIFT_RUN_SA       required"
  echo "  CONTEXTLIFT_RUN_REGION   default us-central1"
  echo "  CONTEXTLIFT_RUN_REPO     default contextlift"
  echo "  CONTEXTLIFT_RUN_JOB      default contextlift-job"
  ;;
esac
