#!/usr/bin/env bash
# Docker smoke test (used by CI): start the practice site, run the agent to the approval gate,
# check nothing was submitted, approve, check exactly one submission. Fake LLM, no network.
set -euo pipefail

# Compose fills ${...} from the repo-root .env too; these shell values win, so a Groq key or a
# webhook in .env can never turn the smoke test into real calls.
compose() {
  LLM_PROVIDER=fake GROQ_API_KEY= DISCORD_REVIEW_WEBHOOK_URL= docker compose --profile agent "$@"
}
count() { curl -fsS http://127.0.0.1:8800/api/submissions | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"; }

trap 'compose down -v --remove-orphans >/dev/null 2>&1 || true' EXIT
compose build
compose up -d --wait practice-site

out=$(compose run --rm -e LOG_LEVEL=WARNING agent fill site:practice/job_apply)
echo "$out"
run_id=$(echo "$out" | awk '/^run /{print $2; exit}' | tr -d ':')
echo "$out" | grep -q ": waiting" || { echo "expected the run to wait for review"; exit 1; }
[ "$(count)" = "0" ] || { echo "something was submitted before approval"; exit 1; }

compose run --rm -e LOG_LEVEL=WARNING agent review approve "$run_id" --reviewer ci
[ "$(count)" = "1" ] || { echo "expected exactly one submission after approval"; exit 1; }
echo "compose smoke test passed: $run_id waited, then submitted once"
