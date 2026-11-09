"""The detection eval must not drop below the numbers from its first run (2026-11-08)."""

from eval.run_detection_eval import run_eval

# Set from the first run. Hidden and honeypot fields must always be left alone.
THRESHOLDS = {
    "field_recall": 1.0,
    "field_precision": 1.0,
    "label_exact": 1.0,
    "kind_accuracy": 1.0,
    "required_accuracy": 1.0,
    "mapping_accuracy": 1.0,
    "hidden_skipped": 1.0,
    "submit_found": 1.0,
}


async def test_detection_eval_meets_thresholds() -> None:
    metrics, total, _per = await run_eval()
    low = {k: v for k, v in metrics.items() if v < THRESHOLDS[k]}
    assert not low, f"below threshold: {low}; misses: {total.misses[:10]}"
