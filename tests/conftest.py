"""Shared fixtures.

`enable_custom_integrations` is what lets Home Assistant find the component
under `custom_components/` during a test run.
"""

from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make the integration loadable in every test."""
    return
