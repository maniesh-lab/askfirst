# Eval results

Every accuracy claim about askfirst points to a row here, and every row points to a results file
in `eval/results/` that a script wrote. Rerun any of them with the command in its row.

- **Date**: 2026-11-08
- **Commit**: first run in Phase 8; all three evals re-run after the mapping fix for
  free-text questions and gave exactly the same results files. (The git history was condensed to one commit per phase on 2026-11-09, so older commit hashes no longer exist.)
- **Model**: none of the numbers below used a real model. They are **offline**: the fake LLM
  provider (`LLM_PROVIDER=fake`), which answers from keyword rules. The live runs (Groq,
  `openai/gpt-oss-20b`) are listed under "Not measured yet".

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

## Not measured yet (need a live run by Boss)

| What | Command | Calls |
|---|---|---|
| Triage on Groq, plus Steadfast 1 vs 3 attempts on the real model | `python -m eval.run_triage_eval --live --steadfast` | about 18 |
| Drafting first-try vs after-retry pass rate on Groq | `python -m eval.run_drafting_eval --live` | 6 to 18 |

Both need `LLM_PROVIDER=groq` and `GROQ_API_KEY` (in `.env` or the shell); with the fake model
`--live` stops with an error. Each writes its own `eval/results/*-live-*` or adds a "Live"
section to the drafting report. Add
the rows to the table above with the model name and date when they exist.
