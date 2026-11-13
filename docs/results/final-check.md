# Final end-to-end check

**Date**: 2026-11-08 · **Commit**: the end of Phase 10 (fresh `git clone` from GitHub) · **Machine**:
Arch Linux laptop, Python 3.14.8, Docker 29.9.2, Compose v5.6.1. (The git history was condensed to one commit per phase on 2026-11-09, so older commit hashes no longer exist.)

This is the local part of the final check. The live parts are all done now; see the end.

## What was run, and what happened

| Step | Command | Result |
|---|---|---|
| Fresh clone | `git clone https://github.com/maniesh-lab/askfirst.git` | at the end of Phase 10 |
| Clean install | `python3.14 -m venv .venv`, `pip install -e ".[dev,practice]"`, `python -m playwright install chromium` | ok (Playwright warns Arch isn't officially supported and uses its Ubuntu 24.04 build, which works) |
| Lint and format | `ruff check .`, `ruff format --check .` | clean (101 files) |
| Every test | `pytest` | **206 passed**, 0 failed, coverage **92%** of `app/` |
| Docker + compose, the whole loop | `./scripts/compose_smoke.sh` | **passed** in 25 s (images cached; see below) |
| CI on GitHub | the CI workflow on the Phase 10 pushes and earlier ones | green (lint, unit, browser, docker) |

What the compose run proves (Done-when #1 in miniature, on our practice site in Docker):

1. `docker compose up -d --wait practice-site` starts our practice site.
2. `docker compose run --rm agent fill site:practice/job_apply` fills the job form and stops:
   `run form-20261108-1244-637b: waiting`. The script then checks the site has **0
   submissions**.
3. That container exits. Approval happens in a **new container** (a new process, with only the
   SQLite files on the shared volume carrying the run over):
   `docker compose run --rm agent review approve form-20261108-1244-637b` ->
   `done, outcome: submitted, confirmation: JOB-37535A`.
4. The script checks the site now has **exactly 1 submission**.

So: it paused before submitting, a restart happened while it waited, and after approval it
submitted exactly once. The fake LLM was used, and the agent could only reach the practice-site
container (`ALLOWED_HOSTS=practice-site`).

## Live parts

- **Real form end-to-end on the hosted practice site (Done-when #1): done 2026-11-08**, see
  [real-form-run.md](real-form-run.md).
- **Scheduled triage run green (Done-when #2): done 2026-11-12**, see
  [schedule-proof.md](schedule-proof.md).
- **First manual triage run with the digest in Discord**: [live-triage-run.md](live-triage-run.md).
- **Live evals and timings on Groq**: [evals.md](evals.md), [run-timings.md](run-timings.md).
