from pathlib import Path

import pytest

from abx_plugins.plugins.importers_browser import importer


def test_exhausted_provider_credits_explain_how_to_resume():
    # Real cabbage OpenCode failure, with session IDs and response headers removed.
    log = Path(__file__).parent / "fixtures" / "opencode-credit-exhausted.jsonl"
    assert importer.learning_failure(log) == (
        "OpenCode's model provider has no credits remaining. Add provider credits "
        "or select a funded provider in Agent settings, then run this importer again. "
        "The learned script and discovery progress are preserved."
    )


def test_preview_cannot_claim_more_items_without_emitting_a_sample():
    # Real cabbage output: the learner sampled ten items but emitted only a
    # success result. Account identity is redacted from the saved JSONL.
    output = Path(__file__).parent / "fixtures" / "importer-empty-preview.jsonl"
    request = {"action": "preview", "limit": 100}
    with pytest.raises(
        ValueError,
        match="Preview must emit sampled ImporterItem records",
    ):
        importer.validate_output(output.read_text(), request)


def test_broken_account_probe_cannot_request_a_new_login():
    # Real cabbage reply importer: an uncalled JS function yielded no account,
    # which was mistaken for expired authentication and disabled the source.
    output = Path(__file__).parent / "fixtures" / "importer-unverified-login.jsonl"
    with pytest.raises(
        ValueError,
        match="Needs-login results require observed authentication evidence",
    ):
        importer.validate_output(output.read_text(), {"action": "check", "limit": 100})


def test_first_import_cannot_skip_a_page_without_emitting_items():
    # Cabbage's learned script added the first page to its dedupe set before
    # buffering it, silently advancing past 100 unimported saved items.
    output = Path(__file__).parent / "fixtures" / "importer-empty-first-batch.jsonl"
    with pytest.raises(ValueError, match="First import batch cannot advance"):
        importer.validate_output(
            output.read_text(),
            {"action": "import", "checkpoint": {}, "limit": 100},
        )
