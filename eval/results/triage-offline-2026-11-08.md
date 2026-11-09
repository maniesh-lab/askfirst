# Triage eval (offline), 2026-11-08

Provider `fake`, model `fake`, 29 graded issues (30 sandbox issues, one already triaged), 4342 tokens.

Offline means the fake keyword classifier, not a real model: it is the floor any real
model should beat, and it proves the pipeline end to end. The deterministic rules
(security, needs-info, stale) run for real either way.

| Metric | Value |
|---|---|
| issues | 29 |
| category_accuracy | 82.76% |
| priority_exact | 62.07% |
| priority_within_one | 96.55% |
| label_f1 | 72.41% |
| needs_info_precision | 100.00% |
| needs_info_recall | 33.33% |
| duplicate_precision | 100.00% |
| duplicate_recall | 50.00% |
| security_escalation_recall | 100.00% |
| unclassified_rate | 0.00% |
| already_triaged_skipped | 100.00% |

## Steadfast before/after

| | unclassified issues | category accuracy |
|---|---|---|
| max_attempts_1 | 6 | 65.52% |
| max_attempts_3 | 0 | 82.76% |

Offline: a provider that breaks every 3rd reply on purpose. With one attempt,
each broken reply loses issues; with three, Steadfast feeds the error back and
the retry succeeds. This shows the mechanism, not a real model's error rate.
