import pytest

from abx_plugins.plugins.base.importers import emit, read_records


def test_delivered_ids_reject_real_rss_reemission(tmp_path):
    import json
    import subprocess
    import sys
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    # Exercise the real RSS CLI and static HTTP files, not a replacement adapter.
    (tmp_path / "feed.xml").write_text(
        '<rss version="2.0"><channel><title>Cursor validation</title>'
        + "".join(
            f"<item><guid>{i}</guid><link>/article-{i}</link></item>" for i in range(3)
        )
        + "</channel></rss>",
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(SimpleHTTPRequestHandler, directory=str(tmp_path)),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = {
        "version": 1,
        "action": "import",
        "feed": "feed",
        "limit": 2,
        "checkpoint": {},
        "settings": {
            "IMPORTER_RSS_URL": f"http://127.0.0.1:{server.server_port}/feed.xml",
        },
    }

    def discover(current):
        process = subprocess.run(
            [sys.executable, "-m", "abx_plugins.plugins.importer_rss.importer"],
            input=json.dumps(current),
            text=True,
            capture_output=True,
            check=True,
        )
        return process.stdout.splitlines()

    try:
        first = list(read_records(discover(request), request))
        assert len(first) == 3 and first[-1]["has_more"] is True
        delivered = [record["id"] for record in first[:-1]]
        # These are actual CLI results with a stale input cursor: the exact
        # contract violation seen during the live browser importer's repeat.
        stale = {**request, "delivered_ids": delivered}
        with pytest.raises(ValueError, match="already delivered"):
            list(read_records(discover(stale), stale))
        continuation = {**stale, "checkpoint": first[-1]["checkpoint"]}
        second = list(read_records(discover(continuation), continuation))
        assert len(second) == 2 and second[-1]["has_more"] is False
        assert second[0]["id"] not in delivered
        repeat = {
            **request,
            "checkpoint": second[-1]["checkpoint"],
            "delivered_ids": [second[0]["id"]],
        }
        completed = list(read_records(discover(repeat), repeat))
        assert len(completed) == 1 and completed[0]["has_more"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


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
