"""Shared pytest setup: the `--run-live` switch for tests that call real services."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        help="also run tests marked `live` (real network and APIs; may cost money)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Live tests are skipped by default so a plain `pytest` (and CI) never calls a real API.
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="live test: run with --run-live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
