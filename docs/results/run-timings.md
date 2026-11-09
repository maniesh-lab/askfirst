# Run timings

Measured 2026-11-08 during Phase 9 with `python -m scripts.measure_runs` (median of
3 runs), on a laptop (Intel i5-13500H, 16 threads, Arch Linux), headless Chromium, **fake LLM**.

| Run | Median of 3 | Tokens (fake LLM) |
|---|---|---|
| Form: start to review gate (job form, 12 fields, 2 drafted) | 0.8 s | 442 (whole run) |
| Form: approve to submitted (replay + submit) | 1.0 s | (in the row above) |
| Triage: 30 issues, fetch to digest | 0.06 s | 4414 |

## What this means

- These are the agent's own overheads: browser, detection, mapping, checkpoints, rendering. A
  real model adds its response time on top: one drafting call per form step, and one
  classification call per 5 issues (6 calls for 30 issues), plus any Steadfast retries.
- With the fake LLM, "tokens" are word counts of the prompts and replies: a rough size, not a
  bill. The triage prompt carries the rubric and up to 5 issue bodies (cut to 3000 characters
  each, code blocks shortened), which is why it is ~10x the form's.
- "Approve to submitted" includes opening a fresh browser and replaying the fill, because the
  agent never keeps a browser open while it waits for a person (see docs/failure-handling.md).

## Still to measure (live, Boss)

- The same two runs with `LLM_PROVIDER=groq` (real latency and token counts): set
  `GROQ_API_KEY` and run `LLM_PROVIDER=groq python -m scripts.measure_runs --repeat 1`
  (about 2 drafting calls and 6 triage calls).
