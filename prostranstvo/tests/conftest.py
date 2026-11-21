"""Shared capability checks for desktop integration tests."""

import pytest


@pytest.fixture(scope="session")
def isolated_executor():
    from app.worker import check

    if reason := check():
        pytest.skip(reason)
