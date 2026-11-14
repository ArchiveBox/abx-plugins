"""ArchiveBox routes for the screencast plugin's transient preview frames."""

import os
import re
import logging
from hashlib import sha256
from pathlib import Path

from functools import lru_cache
from django.apps import AppConfig as DjangoAppConfig


class _PreviewRequests(logging.Filter):
    path = r"(?:/api/v1/core/snapshot/[^/]+/screencast\.jpg|/api/v1/crawls/crawl/[^/]+/files/chrome_screencast/latest\.jpg)(?:\?[^ ]*)?"
    request = re.compile(rf'"GET {path} HTTP/[^\"]+" (?:2\d\d|30\d|404) .+')
    not_found = re.compile(rf"Not Found: {path}")

    def filter(self, record):
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) == 5
        ):
            _, method, path, version, status = record.args
            message = f'"{method} {path} HTTP/{version}" {status} 0'
        else:
            message = record.getMessage()
        return not (
            self.request.fullmatch(message) or self.not_found.fullmatch(message)
        )


_preview_requests = _PreviewRequests()


class AppConfig(DjangoAppConfig):
    name = "abx_plugins.plugins.chrome_screencast"
    label = "abx_chrome_screencast"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        for name in (
            "django.server",
            "django.request",
            "django.channels.server",
            "uvicorn.access",
            "daphne.access",
        ):
            logging.getLogger(name).addFilter(_preview_requests)


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
        return _snapshot_response(
            request,
            host.capture_context(request, snapshot_id=snapshot_id),
        )

    crawl_router = Router(tags=["Live previews"])

    @crawl_router.get(
        "/crawl/{crawl_id}/files/chrome_screencast/latest.jpg",
        auth=None,
        url_name="crawl_screencast",
    )
    def crawl_screencast(request: HttpRequest, crawl_id: str):
        return _crawl_response(
            request,
            host.capture_context(request, crawl_id=crawl_id),
        )

    return (("/core/", router), ("/crawls/", crawl_router))


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


def _crawl_response(request, context):
    from ninja.errors import HttpError

    path = "chrome_screencast/latest.jpg"
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
