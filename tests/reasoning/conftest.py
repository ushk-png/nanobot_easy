"""Shared fixtures for stepwise reasoning tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def fine_granularity(request, monkeypatch):
    """Bridge tests written for the fine S1-S7 layout keep it unless they opt out.

    With stage calls at effort 'low' the bridge picks compact granularity on its
    own; tests about compact behaviour use the ``auto_granularity`` marker.
    """

    if request.node.get_closest_marker("auto_granularity"):
        return
    monkeypatch.setattr(
        "nanobot.reasoning.bridge.procedure_granularity", lambda *_args, **_kwargs: "fine"
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "auto_granularity: let the bridge choose fine/compact granularity itself"
    )
