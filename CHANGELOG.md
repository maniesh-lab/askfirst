# Changelog

## 1.0.2 (2026-11-14)

### Security

- The browser now enforces the host allowlist itself: every host that isn't allowed goes to a
  dead proxy and WebRTC is kept off the network. Before, an `<img>`, script or `fetch()` that
  followed an open redirect on an allowed host could reach any host, and a backslash in a
  redirect (`http://evil\@ok/`) fooled the redirect check. Only GET and HEAD pass during filling.
- Honeypots hidden by common CSS tricks (an `opacity:0` wrapper, sr-only clipping, a zero-height
  box, far positioning, `clip-path`, transparent ink) counted as visible and were filled; hidden
  group members and options are no longer offered.
- An approval is tied to the reviewed address: approving with another `PRACTICE_BASE_URL` (or an
  edited site profile) no longer submits to a different site.
- `--all-clean` no longer approves runs with LLM-drafted answers; a blank CSV cell no longer falls
  back to the persona (a stock photo on a listing).
- Triage: a stale, already triaged security issue still got a close proposal (the 1.0.1 fix
  stopped at the rule result); issues labelled `type:security` or classified as security by the
  LLM are escalated at P0; the rules read the full body (code blocks and text after 3000
  characters included) and plural security words; the model can only add `area:` labels from
  the rubric's list; issue text can't close the prompt's input block; issue titles and summaries
  are plain text in the digest (no injected links or mention chips).
- The workflow gives its secrets only to the steps that use them; GitHub "next" links are only
  followed on GitHub's API host; page and issue text is printed without terminal control codes;
  site profiles can only name login settings; hidden input values (CSRF tokens) are no longer
  stored; the page check before submit also covers field names, hidden inputs and buttons.

### Fixed

- `review edit` can change fields on earlier steps of a multi-step form (the marketplace price).
- Triage never loses or repeats work after a partial failure: a failed state read fails the run
  (instead of starting over with a second log issue), a partly posted digest is posted again,
  an LLM outage keeps the window and exits 1, a write that timed out is "maybe applied" (never
  retried), two issues filed in the same second aren't each other's duplicate, a "please add
  info" comment isn't proposed again while `needs-info` is on, `drop=` only removes waiting
  proposals, and the step timeout makes a hung run fail and ping.
- A refused submit (nothing clicked) ends as failed, a failure after a run was done no longer
  marks it failed, run IDs can't collide, Excel's UTF-8 CSV reads correctly, and the remaining
  CLI errors print one line instead of a traceback.

## 1.0.1 (2026-11-13)

### Fixed

- Triage only trusts run-state comments from the repo's owner, members and collaborators. Before, anyone
  could post a hidden state block on the public log issue and hide issues from every later run.
- A triaged security issue that goes stale stays escalated, so it never gets a close proposal.
- `fill`, `review approve`, `fill-batch` and `runs continue` exit 1 when the run gave up, the
  submit failed, or it may have gone through (they exited 0 before).
- The CLI prints one line instead of a traceback for a host outside `ALLOWED_HOSTS` (`detect`,
  `plan`), a missing file, an unknown form or run, or a closed prompt. `--plan-hash` needs at
  least 8 characters.
- Interactive triage claims the run before acting on a decision, so of two people deciding at
  once only one acts. `runs continue` finishes a run whose apply step crashed and never repeats
  a write: an unsure one is listed under "Maybe applied" and the run ends as
  `needs_human_check`. A run finished by `runs continue` now ends as done (it stayed failed),
  and an edit that drops every proposal ends the run (it used to stay "waiting").
- The practice site's login no longer redirects to `/\host` (browsers treat it as another site).
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
