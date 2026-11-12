# Eval results

Every accuracy claim about askfirst points to a row here, and every row points to a results file
in `eval/results/` that a script wrote. Rerun any of them with the command in its row.

- **Date**: 2026-11-08
- **Commit**: first run in Phase 8; all three evals re-run after the mapping fix for
  free-text questions and gave exactly the same results files. (The git history was condensed to one commit per phase on 2026-11-09, so older commit hashes no longer exist.)
- **Model**: the Summary table is **offline**: the fake LLM provider (`LLM_PROVIDER=fake`),
  which answers from keyword rules. The live runs on Groq (`openai/gpt-oss-20b`, 2026-11-12)
  have their own table under "Live on Groq".

## Summary

| Area | Metric | Result | Sample | Results file |
|---|---|---|---|---|
| Form detection | fillable fields found (recall / precision) | 100% / 100% | 16 pages, 68 fields | `eval/results/detection-2026-11-08.md` |
| Form detection | label, kind, required correct | 100% each | 68 fields | same |
| Form mapping | field mapped to the right profile value (or left for a draft / a person) | 100% | 68 fields | same |
| Form safety | hidden fields and honeypots left alone (**must be 100%**) | 100% | 9 fields | same |
| Form safety | submit button found (exact text) | 100% | 16 pages | same |
| Drafting safety | planted invented facts caught (**must be 100%**) | 100% (112/112) | 14 kinds x 8 questions | `eval/results/drafting-2026-11-08.md` |
| Drafting | false alarms on drafts that only use profile facts | 0% (0/42) | 6 forms x 7 drafts | same |
| Drafting | a model that keeps inventing: field left empty and flagged | 6/6 | 6 forms | same |
| Drafting | a model that invents once: fixed by the Steadfast retry | 6/6 | 6 forms | same |
| Triage safety | security issues escalated (**must be 100%**) | 100% | 2 issues | `eval/results/triage-offline-2026-11-08.md` |
| Triage | category accuracy | 82.8% | 29 issues | same |
| Triage | priority exact / within one level | 62.1% / 96.6% | 29 issues | same |
| Triage | label F1 (type + priority labels) | 72.4% | 29 issues | same |
| Triage | needs-info precision / recall | 100% / 33.3% | 29 issues (3 need info) | same |
| Triage | duplicate precision / recall | 100% / 50% | 29 issues (2 duplicates) | same |
| Triage | already-triaged issue skipped | 100% | 1 issue | same |
| Steadfast | issues lost with 1 attempt vs 3 (flaky fake model) | 6 vs 0 | 29 issues | same |

Commands: `python -m eval.run_detection_eval`, `python -m eval.run_drafting_eval`,
`python -m eval.run_triage_eval --steadfast`.

## Live on Groq (2026-11-12)

Model `openai/gpt-oss-20b` on Groq's free tier, same 30 sandbox issues and 6 forms as above.

| Area | Metric | Offline | Live | Results file |
|---|---|---|---|---|
| Triage safety | security issues escalated (**must be 100%**) | 100% | 100% | `eval/results/triage-live-2026-11-12.md` |
| Triage | category accuracy | 82.8% | 93.1% | same |
| Triage | priority exact / within one level | 62.1% / 96.6% | 86.2% / 100% | same |
| Triage | label F1 (type + priority labels) | 72.4% | 89.7% | same |
| Triage | needs-info precision / recall | 100% / 33.3% | 100% / 66.7% | same |
| Triage | duplicate precision / recall | 100% / 50% | 100% / 100% | same |
| Triage | already-triaged issue skipped | 100% | 100% | same |
| Triage | unclassified issues | 0 | 0 | same |
| Steadfast | issues lost with 1 attempt vs 3 | 6 vs 0 (flaky fake) | 0 vs 0 | same |
| Drafting | forms whose drafts passed the fact check on the first try | n/a | 3/6 (50%) | `eval/results/drafting-2026-11-12.md` |
| Drafting | forms passed after Steadfast retries | n/a | 6/6 (100%) | same |

Cost: triage 32,350 tokens for one graded run (plus two more for the Steadfast rows), drafting
15 calls and 10,243 tokens. Commands: `python -m eval.run_triage_eval --live --steadfast` and
`python -m eval.run_drafting_eval --live` (both need `LLM_PROVIDER=groq` and `GROQ_API_KEY`).

What the live numbers say:

- **The real model beats the keyword floor everywhere**, and the rule-based safety rows stay at
  100%.
- **Steadfast on the real model:** Groq's replies were valid JSON every time, so 1 vs 3 attempts
  lost nothing (the small accuracy gap between those two runs is the model answering a little
  differently each time, not the retry). The retries that did matter were Groq's free-tier
  rate limit (8,000 tokens a minute): several calls got HTTP 429 and went through on the
  next attempt, which is why the triage eval took about 12 minutes.
- **Drafting:** three forms (job_apply, its variant and the survey) were rejected by the draft
  checks on their first replies and passed on the fourth attempt; the other three passed first
  time. The results file does not record which check rejected them. Either way a rejected draft
  never reaches the form.
- **Still a small sample** (one run, 29 issues, 6 forms). A second run would move these numbers
  by a few points.

## What these numbers mean (and don't)

- **Our own pages and issues.** The 16 form pages and 30 sandbox issues were written for this
  project, and the expected answers were checked by hand. A perfect detection score means the
  agent matches those answers; it is not a benchmark of websites it has never seen.
- **Detection expected files** were drafted from the detector's output and then corrected by
  hand, which makes a high score easier. The safety rows (hidden fields, submit button) are the
  ones that matter most.
- **The planted facts are our own list.** The fact checker is a strict rule (names, numbers,
  emails and URLs must appear in the profile or on the page), so it is built to catch them. The
  false-alarm row shows that strictness does not break ordinary profile-only answers here; with
  richer profiles it may flag more, which leaves the field empty for a person, never wrong.
- **Offline triage is a floor, not a claim.** The fake classifier is keyword rules. The
  deterministic rules (security escalation, needs-info, stale) run for real either way, which is
  why security recall is 100% even offline. A real model should beat the other numbers.
- **The Steadfast comparison is a demonstration.** The fake provider breaks every third reply on
  purpose. It shows that the retry loop works, not how often a real model gets the format wrong.
- **Small samples.** 2 security issues and 2 duplicates are enough to catch a regression, not
  to state a rate with confidence.

## In CI

`tests/eval/test_offline_thresholds.py` and `tests/browser/test_detection_eval.py` run the
offline evals on every push and fail if any number drops below the floors above (the safety
rows must stay at 100%).

Live runs are not in CI: they cost API calls and their numbers move a little from run to run.
With the fake model, `--live` stops with an error.
