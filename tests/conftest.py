"""Shared fixtures for CascadeUI tests."""

import pytest


@pytest.fixture(autouse=True)
def reset_state_store():
    """Reset the singleton StateStore between tests to prevent bleed.

    The store is cached in two places: StateStore._instance (class-level)
    and singleton._store_instance (module-level). Both must be cleared.
    """
    from cascadeui.state.store import StateStore
    from cascadeui.state import singleton

    StateStore._instance = None
    singleton._store_instance = None

    yield

    StateStore._instance = None
    singleton._store_instance = None
