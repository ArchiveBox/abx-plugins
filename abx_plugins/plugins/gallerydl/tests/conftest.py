import pytest


@pytest.fixture(scope="session", autouse=True)
def ensure_chrome_test_prereqs():
    """Override root autouse Chrome prereq fixture for plugin-local tests."""
    return None
