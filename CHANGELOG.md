# Changelog

## Unreleased

### Fixed

- `detect` and `plan` print "refused: ..." and exit 2 for a host outside `ALLOWED_HOSTS`, instead
  of crashing with a traceback (`fill` already did this).
- Docs: the live drafting numbers count provider calls (Groq rate-limit retries included), not
  only rejected drafts; ARCHITECTURE lists the page text drafting really sends.

## 1.0.0 (2026-11-13)

First release: two agents on one shared runtime, both proven live.

### Added

- **Shared runtime** (`app/runtime/`): a policy that gives every action a registered risk
  (`read`, `reversible`, `irreversible`); a LangGraph approval gate; signed approval tokens tied to
  the reviewed plan, required by every irreversible action, with no bypass flag; SQLite checkpoints
  after every step; a run index; an append-only audit log per run; structlog JSON logs with
  `run_id` on every line and secrets masked; one LLM wrapper over
  [Steadfast](https://github.com/maniesh-lab/steadfast) 0.3 (schema checks, feedback retries,
  rate limits, an optional fallback provider) for Groq, OpenAI, Anthropic and an offline fake; a
  Discord notifier.
- **Form agent** (`app/browser/`): detects fields with Playwright, maps them to a profile by rule,
  has the LLM draft only the free-text answers (with a fact check), fills and reads back every
  value, writes a `review.html` report, and waits. After approval it replays the plan in a fresh
  browser, checks the page is unchanged and clicks submit once, even across a restart. Guards in
  the browser: a host allowlist (redirects included), a network guard that blocks every write
  except login and the approved submit, and a submit blocker. Also `detect`, `plan`, `fill-batch`
  and `review list/show/approve/edit/reject`.
- **Triage bot** (`app/triage/`): reads a repo's new and updated GitHub issues, applies the
  rubric's rules (security escalation, needs-info, stale), finds duplicate candidates, classifies
  with the LLM in batches of 5 (rules win), plans actions under the shared policy, posts a digest
  to Discord, and saves its window in a "Triage log (bot)" issue. Runs on GitHub Actions on
  weekdays at 02:23 UTC; comments and closing issues are only ever proposed.
- **Practice site** (`practice_site/`): our own FastAPI forms (job application, contact, survey,
  a marketplace listing behind a login), deployed on Render as the live test target.
- **Evals**: form detection (16 pages, 68 fields), drafting safety (112 planted facts) and triage
  (30 sandbox issues), offline in CI with floors, plus live runs on Groq.
- **Docker**: an agent image with Chromium and a compose setup with the practice site; a smoke
  test of the whole fill, wait, approve, submit-once loop runs in CI.
- **CI**: lint (ruff, actionlint), unit tests with pip-audit and Codecov, browser tests with
  Playwright traces on failure, the Docker smoke test; Dependabot for GitHub Actions.
- **Docs**: README, architecture, failure handling, and results for every claim: the real form
  run, the first live triage run, the schedule proof, evals and timings.

### Fixed since the first build

- The triage window is never saved when the digest didn't reach Discord, so the next run covers
  those issues again; a local run that can write but has no digest webhook is refused.
- Redirects can no longer take the browser off the allowed hosts: every hop is checked.
- The timing script labels tokens with the provider that was really used.
