from pathlib import Path

from abx_plugins.plugins.importers_browser import importer


def test_exhausted_provider_credits_explain_how_to_resume():
    # Real cabbage OpenCode failure, with session IDs and response headers removed.
    log = Path(__file__).parent / "fixtures" / "opencode-credit-exhausted.jsonl"
    assert importer.learning_failure(log) == (
        "OpenCode's model provider has no credits remaining. Add provider credits "
        "or select a funded provider in Agent settings, then run this importer again. "
        "The learned script and discovery progress are preserved."
    )
