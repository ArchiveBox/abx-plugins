import pytest


@pytest.fixture(scope="module")
def require_chrome_runtime():
    """Require chrome runtime prerequisites for integration tests."""
    from abx_pkg import NpmProvider, BinProviderOverrides, BinaryOverrides

    NpmProvider.model_rebuild(
        _types_namespace={
            'BinProviderOverrides': BinProviderOverrides,
            'BinaryOverrides': BinaryOverrides,
        }
    )

    try:
        NpmProvider()
    except Exception as exc:
        pytest.fail(f"Chrome integration prerequisites unavailable: {exc}")
