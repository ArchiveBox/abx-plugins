def test_plugin_dependency_boundaries(plugin_dependency_violations) -> None:
    """Standalone runtimes cannot depend on a host or its optional adapters."""
    assert not plugin_dependency_violations, (
        "Plugin dependency boundary violations:\n"
        + "\n".join(plugin_dependency_violations)
    )
