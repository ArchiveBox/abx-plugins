import pytest

from abx_plugins.plugins.base.importers import emit, read_records


@pytest.mark.parametrize(
    ("action", "status", "has_more", "checkpoint", "rejected"),
    [
        ("import", "succeeded", True, {"cursor": "page-1"}, True),
        ("import", "succeeded", True, {"cursor": "page-2"}, False),
        ("import", "succeeded", False, {"cursor": "page-1"}, False),
        ("preview", "succeeded", True, {"cursor": "page-1"}, False),
        ("import", "failed", True, {"cursor": "page-1"}, False),
        ("import", "needs_login", True, {"cursor": "page-1"}, False),
    ],
)
def test_continuation_requires_progress_at_the_shared_jsonl_boundary(
    capsys,
    action,
    status,
    has_more,
    checkpoint,
    rejected,
):
    # Facebook's live adapter returned success/more with its input checkpoint.
    # Validate this before returning from replay, not only when committing a run.
    request = {
        "action": action,
        "limit": 100,
        "checkpoint": {"cursor": "page-1"},
    }
    result = {
        "type": "ImporterResult",
        "status": status,
        "checkpoint": checkpoint,
        "has_more": has_more,
    }
    emit(result)
    lines = capsys.readouterr().out.splitlines()
    if rejected:
        with pytest.raises(ValueError, match="without advancing its checkpoint"):
            list(read_records(lines, request))
    else:
        assert list(read_records(lines, request)) == [result]
