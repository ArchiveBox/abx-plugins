"""ArchiveBox routes for the screencast plugin's transient preview frames."""

import os
import re
from hashlib import sha256
from pathlib import Path

from functools import lru_cache


def get_ignorable_url_patterns():
    return (
        re.compile(r"/api/v1/core/snapshot/.*/screencast\.jpg(?:\?.*)?"),
        re.compile(
            r"/api/v1/crawls/crawl/.*/files/chrome_screencast/latest\.jpg(?:\?.*)?",
        ),
    )


@lru_cache(maxsize=1)
def get_api_routers(host):
    from django.http import HttpRequest
    from ninja import Router

    router = Router(tags=["Live previews"])

    @router.get(
        "/snapshot/{snapshot_id}/screencast.jpg",
        auth=None,
        url_name="snapshot_screencast",
    )
    def snapshot_screencast(request: HttpRequest, snapshot_id: str):
        return _snapshot_response(request, host.snapshot_context(request, snapshot_id))

    return (("/core/", router),)


def _response(request, file, started_at=None):
    from django.http import FileResponse
    from ninja.errors import HttpError
    from abx_plugins.plugins.base.django import (
        RangedFileReader,
        _is_asgi_request,
        _stream_ranged_file_async,
    )

    if started_at is not None and os.fstat(file.fileno()).st_mtime < started_at:
        file.close()
        raise HttpError(404, "No frame for this capture yet")
    response = FileResponse(
        file,
        content_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, max-age=0, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Content-Type-Options": "nosniff",
        },
    )
    if _is_asgi_request(request):
        response.streaming_content = _stream_ranged_file_async(RangedFileReader(file))
    return response


def _started_at(request):
    from ninja.errors import HttpError

    try:
        return float(request.GET["after"]) if request.GET.get("after") else None
    except ValueError:
        raise HttpError(400, "Invalid screencast start time")


def _snapshot_response(request, context):
    from ninja.errors import HttpError

    started_at = _started_at(request)
    candidates = context["output_dirs"]
    live_root = Path(context["tmp_dir"]) / "chrome_screencast"
    for directory in candidates:
        try:
            frame = (
                live_root
                / sha256(os.path.abspath(directory).encode()).hexdigest()
                / "latest.jpg"
            ).open("rb")
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            continue
        if started_at is not None and os.fstat(frame.fileno()).st_mtime < started_at:
            frame.close()
            continue
        return _response(request, frame)
    raise HttpError(404, "No frame for this capture yet")


def serve_crawl_file(request, context, path):
    """Resolve the historic crawl preview URL after the host checks access."""
    from ninja.errors import HttpError

    if path != "chrome_screencast/latest.jpg":
        return None
    started_at = _started_at(request)
    live_root = Path(context["tmp_dir"]) / "chrome_screencast"
    for directory in context["output_dirs"]:
        try:
            file = (
                live_root
                / sha256(os.path.abspath(directory).encode()).hexdigest()
                / "latest.jpg"
            ).open("rb")
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            continue
        return _response(request, file, started_at)
    directories = [Path(path) for path in context["output_dirs"]]
    root = next(
        (directory for directory in directories if directory.exists()),
        directories[0],
    ).resolve()
    archived = (root / path).resolve()
    if not archived.is_relative_to(root):
        raise HttpError(404, "Crawl file not found")
    try:
        file = archived.open("rb")
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        raise HttpError(404, "Crawl file not found")
    return _response(request, file, started_at)
