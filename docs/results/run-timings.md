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

## Live on Groq (2026-11-12)

The same two runs with `LLM_PROVIDER=groq` (model `openai/gpt-oss-20b`, free tier), one run
each: `LLM_PROVIDER=groq GROQ_API_KEY=... python -m scripts.measure_runs --repeat 1`.

| Run | One run | Tokens (groq) |
|---|---|---|
| Form: start to review gate (job form, 12 fields, 2 drafted) | 8.0 s | 3356 (whole run) |
| Form: approve to submitted (replay + submit) | 0.8 s | (in the row above) |
| Triage: 30 issues, fetch to digest | 179.8 s | 27671 |

- **Form:** the model adds about 7 seconds before the review gate. Approve to submitted makes no
  LLM call (it replays the saved plan), so it stays under a second.
- **Triage** is slow on the free tier because of its rate limit (8,000 tokens a minute): 30
  issues need about 28,000 tokens, so the client has to wait for the limit to reset between
  calls. That is about 3.5 minutes of tokens at that rate, which matches the ~3 minutes here
  and the 142 s of the first live workflow run on GitHub. A paid tier would be limited by the
  model's response time instead.
- One run each, so these are rough, not medians.
