# Detection and mapping eval, 2026-11-08

16 fixture pages, 68 expected fillable fields, 9 hidden or honeypot fields that must be left alone.

Caveat: these are our own pages, and the expected files were drafted from the
detector's output and then checked by hand. A perfect score means the agent matches
those checked answers; it is not a benchmark of unseen websites.

| Metric | Value |
|---|---|
| field_recall | 100.00% |
| field_precision | 100.00% |
| label_exact | 100.00% |
| kind_accuracy | 100.00% |
| required_accuracy | 100.00% |
| mapping_accuracy | 100.00% |
| hidden_skipped | 100.00% |
| submit_found | 100.00% |

| Fixture | Expected | Matched | Mapping OK | Submit OK |
|---|---|---|---|---|
| contact | 4 | 4 | 4 | yes |
| edge_aria_labelledby | 6 | 6 | 6 | yes |
| edge_buttons | 2 | 2 | 2 | yes |
| edge_conditional | 2 | 2 | 2 | yes |
| edge_dates | 5 | 5 | 5 | yes |
| edge_disabled_readonly | 1 | 1 | 1 | yes |
| edge_fieldsets | 4 | 4 | 4 | yes |
| edge_two_forms | 4 | 4 | 4 | yes |
| job_apply | 12 | 12 | 12 | yes |
| job_apply_variant_b | 12 | 12 | 12 | yes |
| listing_step1 | 3 | 3 | 3 | yes |
| listing_step2 | 4 | 4 | 4 | yes |
| listing_step3 | 2 | 2 | 2 | yes |
| listing_step4 | 0 | 0 | 0 | yes |
| login | 2 | 2 | 2 | yes |
| survey | 5 | 5 | 5 | yes |

## Misses

- none
