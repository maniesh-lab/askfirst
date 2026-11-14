# Failure handling

What askfirst does when something outside its control goes wrong. The rule behind every row:
**when in doubt, stop and let a person decide**. Nothing is guessed, and nothing irreversible
happens because of a failure.

| What fails | What askfirst does | Proof |
|---|---|---|
| **Playwright times out** filling a field (element gone, page slow) | Tries once more, then flags the field (`fill failed`) in the review. The fill step never raises for one field, so the run goes on to the review gate and a person sees the flag. | `tests/browser/test_detect_and_guard.py::test_fill_timeout_is_flagged_not_fatal` (the field level; the graph path from fill to the gate is the one every form-run test takes) |
| **Playwright times out** loading a page | The run stops as `failed` with the error in the audit log. Nothing was submitted (submit only happens after approval). Start a new run. | `app/runtime/graphs.py` (`_run` turns any node error into a `failed` run); not tested with a real slow page. The same path is tested with a refused host: `tests/browser/test_form_run.py::test_host_not_allowed_fails_run_and_is_audited` |
| **The page changed** between review and submit (new fields, different form) | The page's schema hash no longer matches the approved plan, so the agent goes back to review instead of clicking. | `tests/browser/test_form_run.py::test_page_changed_after_approval_goes_back_to_review` |
| **The process crashes** while waiting for review | Nothing is lost: the run is checkpointed in SQLite. `review approve` in a new process replays the fill and submits once. | `tests/browser/test_form_run.py::test_restart_while_waiting_then_approve_in_new_process` |
| **The process crashes** right after the submit click | The run is claimed ("submitting") before the click, so it is never clicked again. If the process survives the error, the run is marked `needs_human_check` at once and the CLI says to check the site. If the process was killed, the run stays `submitting` until `python -m app.cli runs continue <run_id>`, which ends it as `needs_human_check` without clicking. | `tests/browser/test_form_run.py::test_crash_right_after_click_never_clicks_again`, `::test_failure_after_click_is_needs_human_check` |
| **Two people decide on the same triage run** at once (approve, reject or edit) | The runner claims the run (compare-and-set, waiting -> submitting) before the graph sees the decision. The second decision gets `ClaimLost` and changes nothing: no comments, closes, digest, state comment or checkpoint, and the run's status stays the first decision's. A decision for an old plan hands the claim back, so the run keeps waiting. | `tests/triage/test_triage_graph.py::test_second_of_two_racing_decisions_changes_nothing` |
| **The process crashes** while applying approved triage actions | Approving again is refused (the run is no longer waiting). `python -m app.cli runs continue <run_id>` carries on and skips every action the audit log shows as applied. An action that had started but not finished is not retried (its comment or close may have reached GitHub): the digest lists it under "Maybe applied (check by hand)" and the run ends as `needs_human_check`. Run `runs continue` once, after the process has stopped: a run still applying looks the same as a killed one. | `tests/triage/test_triage_graph.py::test_runs_continue_after_a_crash_never_repeats_a_write` |
| **The LLM is down** (outage, bad key) while drafting | Draft fields stay empty and are flagged `draft_failed` for the reviewer. Rule-based fields are filled as usual. | `tests/test_failure_behaviour.py::test_llm_down_drafts_are_left_empty_and_flagged` |
| **The LLM is down** during triage | Issues are marked "unclassified (needs a human)" in the digest. The rules still run, so security reports are escalated anyway. If not one issue could be classified, the digest still goes out but the window is not saved (the next run covers it again) and the run exits 1, which fires the failure ping. | `tests/test_failure_behaviour.py::test_llm_down_triage_still_runs_and_rules_still_escalate`, `tests/triage/test_triage_graph.py::test_llm_down_keeps_the_window_and_fails_the_run` |
| **The LLM answers badly** (invalid JSON, invented facts, labels not allowed) | Steadfast feeds the problem back and retries (3 attempts). Still bad: drafts stay empty and flagged; triage splits the batch, then marks the issue unclassified. | `tests/unit/test_llm.py`, `tests/triage/test_rules_and_classify.py`, `tests/browser/test_form_run.py::test_draft_failure_leaves_field_empty_and_flagged` |
| **GitHub rate limit** | A short wait (under a minute) is waited out; a longer one stops the run cleanly with `RateLimited`. The next scheduled run starts from the last saved window, so nothing is skipped. | `tests/triage/test_github.py::test_waits_on_short_secondary_limit_then_retries`, `::test_stops_cleanly_on_long_rate_limit` |
| **GitHub refuses a write** (4xx: permissions, validation) | That action is listed under "Failed (not applied)" in the digest with the error; the other actions and the digest still go out. | `tests/triage/test_triage_graph.py::test_a_failing_github_write_is_listed_and_the_rest_still_runs` |
| **A GitHub write may have landed** (timeout, lost connection, 5xx, or a duplicate close whose comment went out but the close failed) | Listed under "Maybe applied (check by hand)", never retried, and the run ends `needs_human_check`. | `tests/triage/test_triage_graph.py::test_a_write_that_may_have_landed_is_unsure_not_failed` |
| **The saved triage state can't be read** (GitHub error on the log issue) | The run fails (and pings Discord) instead of carrying on as a first run, which would use a 7-day window and start a second log issue. | `tests/triage/test_triage_graph.py::test_a_failed_state_read_fails_the_run_instead_of_starting_over` |
| **The form's address changed between review and approval** (another `PRACTICE_BASE_URL`, an edited site profile) | The approval is refused before the browser opens; nothing is submitted. Start a new run. | `tests/browser/test_form_run.py::test_approval_is_tied_to_the_reviewed_address` |
| **No GitHub token** | Locally: the run is read-only (no labels, no log issue, no saved window), and the CLI says so; nothing is listed as "failed". On GitHub Actions the workflow refuses to run (next row but one). | `tests/triage/test_triage_graph.py::test_real_runner_without_a_token_is_read_only`, `::test_triage_cli_warns_when_the_token_is_missing`, `::test_no_token_marks_label_actions_not_applied_not_failed` |
| **Discord 429** | Waits the `retry_after` Discord asks for (at most 60 s), up to 3 tries. | `tests/unit/test_notify.py::test_honours_429_retry_after_then_succeeds` |
| **Discord limits** (10 embeds, 6000 characters, field sizes) | The digest is split across messages within every limit, and capped at 4 messages with a "cut short" note pointing to the Actions run. | `tests/triage/test_digest_and_state.py::test_discord_limits_hold_for_a_huge_digest` |
| **Discord is down** | Each message's error is caught. If any part of the digest could not be posted, no state is saved (the next run covers the same window and posts again) and the CLI exits 1, which fires the workflow's failure ping. In `apply-labels` mode the labels already added stay, so the re-posted digest lists them as "triaged" rather than "applied automatically". | `tests/triage/test_triage_graph.py::test_digest_post_failure_keeps_the_run_and_saves_no_state` |
| **The triage workflow is not configured** (a secret or `TRIAGE_REPO` missing) | The first step names what is missing and fails the run, instead of going green with the digest only logged. GitHub emails the owner about failed scheduled runs. | `.github/workflows/triage.yml` ("Check configuration") |
| **No digest webhook on a local run that can write** (token set, `DISCORD_WEBHOOK_URL` not) | The CLI refuses (exit 2) and suggests `--dry-run`. Otherwise the digest would only be logged while the run still saved its window, so the next run would skip those issues. If only the review webhook is set, the digest counts as not posted and no state is saved. | `tests/triage/test_triage_graph.py::test_cli_refuses_a_writing_run_without_a_digest_webhook`, `::test_digest_with_no_digest_webhook_is_not_posted_and_saves_no_state` |
| **A scheduled run fails** | The workflow's last step posts "Triage run failed: <link>" to Discord, so a broken schedule is never silent. The triage step has its own 12-minute limit (the job has 15), so a run that hangs fails and pings rather than being cancelled silently. | `.github/workflows/triage.yml`, `tests/scripts/test_notify_failure.py` |
| **SQLite is locked** by another process (CLI and a run at once) | Waits up to 10 seconds for the lock instead of failing (`timeout` on the run index, `busy_timeout` on the checkpointer). | `tests/test_failure_behaviour.py::test_sqlite_locked_by_another_writer_waits_instead_of_failing` |
| **A secret shows up in an error** (a proxy echoing a token, a webhook URL in a reply) | Masked before it reaches logs, the audit log, the CLI or the checkpoint database. | `tests/test_secret_leaks.py` |

## Known gaps

- A stale issue's close proposal is listed in one digest only (the run in which it went stale).
  If nobody acts on it, it is not proposed again; the `stale` label (in `apply-labels` mode)
  stays as the reminder.
- A `--dry-run` digest leaves out the labels it would have added automatically; the CLI table
  and `digest.json` still show every planned action with its verdict.
- A form run in unattended mode (no person to ask) records the submit as a proposal and ends as
  `done`, although nothing was sent. No CLI flag starts a form run unattended yet; the audit log
  says `proposal_recorded`.
- A Playwright crash in the middle of a multi-step form (marketplace listing) means starting a
  new run; there is no "resume from step 3". Nothing was submitted, so this is safe, just slower.
- GitHub can delay scheduled workflow runs, or skip them when its queue is busy. The triage
  window always starts at the last saved `window_end`, so a late or skipped run only makes the
  next digest cover a longer window.
- If a cron run and an interactive triage run overlap, whichever saves its state last wins, even
  with an older window. The next run then covers a longer window, which is safe, just slower.
- The "already posted" check only stops a second digest for the same `--since`. If a run posts
  its digest and then fails to save its state, the next run (or a re-run) posts that window
  again: a duplicate digest, never a lost one.
- Triage state is trusted from comments by OWNER, MEMBER or COLLABORATOR. On a personal repo
  those can all write; in an organisation a MEMBER or an outside COLLABORATOR may only have read
  access and could still post a state block.
- GitHub's secondary rate limit without a `Retry-After` header is treated as a refusal, so the
  run fails cleanly (and pings) rather than waiting a minute.
- On the form side, two decisions on the same waiting run within milliseconds of each other can
  in theory let a reject or edit carry on the other person's approval (the submit claim still
  allows one click, and only an approved plan hash can be submitted). Triage claims in the
  runner and doesn't have this window.
- A field that can't be filled stays in the plan, so the replay fails each time; `review edit`
  can change its value but not remove it. Start a new run (or fix the site profile).
- Limits of the browser's safety checks (2026-11-14 security review): a field hidden only by
  an element drawn on top of it isn't detected as hidden (every other usual hiding trick is); a
  "Next" button that triggers a state-changing GET runs during filling (only non-GET/HEAD
  requests are blocked); page script that changes values inside its own submit handler, after
  the read-back, isn't caught; an uploaded file's contents aren't part of the plan hash (its
  path is, and it is re-checked at replay).
- An approval token is checked against the plan hash, not the run ID: two runs of the same form
  with the same data have the same plan hash. Tokens are never stored or passed between runs, so
  this is theoretical.
- A flood of new issues can push a triage run past its time limit; no state is saved, so every
  later run tries the same window again (and pings Discord each time) until it fits.
