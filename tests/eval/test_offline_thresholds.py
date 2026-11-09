"""Offline evals as regression thresholds (fake LLM, no network). The browser-based detection
eval has its own test in tests/browser/test_detection_eval.py.

Numbers are floors set from the first offline runs (2026-11-08). The safety ones (invented-fact
catch rate, security escalation recall) must stay at 100%.
"""

from app.config import Settings
from app.runtime.llm import make_llm
from eval import run_drafting_eval as drafting
from eval import run_triage_eval as triage

TRIAGE_FLOORS = {
    "security_escalation_recall": 1.0,
    "already_triaged_skipped": 1.0,
    "category_accuracy": 0.80,
    "priority_within_one": 0.95,
    "label_f1": 0.70,
    "needs_info_precision": 1.0,
    "duplicate_precision": 1.0,
}


def test_planted_inventions_are_all_caught_and_clean_drafts_pass() -> None:
    forms = drafting.load_questions()
    assert len(forms) >= 6
    planted = drafting.planted_eval(forms)
    assert planted["catch_rate"] == 1.0, planted["missed"]
    clean = drafting.clean_eval(forms)
    assert clean["false_alarms"] == 0, clean["examples"]


async def test_drafts_with_inventions_never_reach_the_form() -> None:
    forms = drafting.load_questions()
    result = await drafting.steadfast_eval(forms)
    assert result["always_invents_left_empty"] == result["forms"]
    assert result["invents_once_recovered_after_retry"] == result["forms"]


async def test_offline_triage_meets_floors() -> None:
    results, failed = await triage.triage_once(make_llm(Settings(_env_file=None)))
    metrics = triage.score(results, triage.load_gold())
    low = {k: metrics[k] for k, floor in TRIAGE_FLOORS.items() if metrics[k] < floor}
    assert not low, f"below floor: {low}"
    assert failed == [] and metrics["unclassified_rate"] == 0.0


async def test_steadfast_retries_recover_flaky_output() -> None:
    gold = triage.load_gold()
    outcomes = {}
    for attempts in (1, 3):
        provider = triage.FlakyProvider(every=3)
        llm = make_llm(Settings(_env_file=None), provider=provider)
        results, failed = await triage.triage_once(llm, max_attempts=attempts)
        outcomes[attempts] = (len(failed), triage.score(results, gold)["category_accuracy"])
    assert outcomes[1][0] > 0  # one attempt: broken replies lose issues
    assert outcomes[3][0] == 0  # three: Steadfast feeds the error back and recovers
    assert outcomes[3][1] > outcomes[1][1]


async def test_live_evals_refuse_the_fake_model() -> None:
    # The offline test guard sets LLM_PROVIDER=fake: --live must stop, not quietly write a
    # "live" results file made by the fake model.
    import pytest

    with pytest.raises(SystemExit, match="needs a real model"):
        await triage.run(live=True, steadfast=False)
    with pytest.raises(SystemExit, match="needs a real model"):
        await drafting.live_eval(drafting.load_questions())
